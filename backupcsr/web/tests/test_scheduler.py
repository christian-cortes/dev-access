"""Pruebas del planificador de copias (app/scheduler.py).

No ejecutan jobs reales ni tocan /mnt/nas, /etc o /run: se sustituye el lanzamiento
por un doble (`launch=`) y el socket por uno temporal en /tmp.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_scheduler.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="sched-"))
for name, value in {
    "BACKUP_NAS": TMP / "nas",
    "BACKUP_OPT": TMP / "opt",
    "BACKUP_LOG": TMP / "log",
    "BACKUP_ETC": TMP / "etc",
    "BACKUP_CRON": TMP / "cron" / "backupcsr",
    "BACKUP_CATALOG": TMP / "jobs.yml",
}.items():
    os.environ[name] = str(value)
Path(os.environ["BACKUP_NAS"]).mkdir(parents=True, exist_ok=True)

from app import config, scheduler, scheduler_client  # noqa: E402

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


def wait_running(sched, timeout=5.0):
    end = time.time() + timeout
    while sched.status()["running"] and time.time() < end:
        time.sleep(0.02)
    return sched.status()


class BlockingLaunch:
    """Lanzamiento doble: registra el arranque y espera hasta que se libera."""

    def __init__(self):
        self.started = []
        self.release = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, item):
        with self.lock:
            self.started.append((item["slug"], item["host"]))
        self.release.wait(5)
        return "ok", ""

    def slugs(self):
        with self.lock:
            return sorted(slug for slug, _ in self.started)


# --------------------------------------------------------------- helpers puros
check("parse_backoff normaliza la lista",
      scheduler.parse_backoff("300, 900,2700") == [300, 900, 2700],
      scheduler.parse_backoff("300, 900,2700"))
check("parse_backoff usa el default si está vacío",
      scheduler.parse_backoff("") == [300, 900, 2700])
check("parse_jitter acota a [0, 0.9]",
      scheduler.parse_jitter("0.2") == 0.2 and scheduler.parse_jitter("5") == 0.9)


# --------------------------------------------------- cupos global y por host
launch = BlockingLaunch()
sched = scheduler.Scheduler(state_path=TMP / "caps.json", max_jobs=2, max_per_host=1,
                            launch=launch)
sched.submit("a", action="run", trigger="cron", host="google")
sched.submit("b", action="run", trigger="cron", host="google")
sched.submit("c", action="run", trigger="cron", host="latino")
started = sched.tick()
check("arranca como máximo el cupo global (2)", started == 2, started)
time.sleep(0.1)
check("respeta 1 por host (google deja fuera a b)", launch.slugs() == ["a", "c"], launch.slugs())
check("b sigue pendiente por cupo de host", "b" in sched.queued_slugs())
launch.release.set()
wait_running(sched)
check("las dos terminan en done", len(sched.status()["done"]) == 2, sched.status()["done"])

# --------------------------------------------------------------- coalescing
launch2 = BlockingLaunch()
sched2 = scheduler.Scheduler(state_path=TMP / "coalesce.json", max_jobs=1, launch=launch2)
sched2.submit("a", action="run", trigger="cron", host="h")
info = sched2.submit("a", action="run", trigger="manual", host="h")
check("una ronda nueva sobre un pendiente se fusiona", info["coalesced"] is True, info)
with sched2._guard:
    check("el fusionado sube a prioridad manual",
          sched2._queue[0]["priority"] == 2 and sched2._queue[0]["trigger"] == "manual")
check("cancel quita el pendiente", sched2.cancel("a")["removed"] == 1)
check("acción inválida se rechaza", sched2.submit("a", action="nope")["ok"] is False)
check("slug con ruta se rechaza", sched2.submit("/tmp/evil")["ok"] is False
      and sched2.submit("a/b")["ok"] is False)

# una corrida real no se debe dejar caer detrás de un dry/size pendiente
sched2.submit("b", action="size", trigger="manual", host="h")
info = sched2.submit("b", action="run", trigger="cron", host="h")
with sched2._guard:
    merged = next(i for i in sched2._queue if i["slug"] == "b")
    check("un run pendiente sube por encima de size",
          info["coalesced"] and merged["action"] == "run", (info, merged["action"]))
info = sched2.submit("b", action="size", trigger="manual", host="h")
with sched2._guard:
    merged = next(i for i in sched2._queue if i["slug"] == "b")
    check("un size no degrada un run pendiente",
          info["coalesced"] and merged["action"] == "run", merged["action"])
sched2.cancel("b")

# ---------------------------------------- reintento no duplica un pendiente
sched_dup = scheduler.Scheduler(state_path=TMP / "dedupe.json", max_jobs=1,
                                launch=lambda item: ("ok", ""))
sched_dup.submit("d", action="run", trigger="cron", host="h")  # pendiente previo
running_item = sched_dup._norm({"action": "run", "host": "h"})
running_item["slug"] = "d"
with sched_dup._guard:
    sched_dup._running.append(running_item)
sched_dup._finish(running_item, "fallo", "transitorio")
with sched_dup._guard:
    same = [i for i in sched_dup._queue if i["slug"] == "d"]
    check("el reintento se fusiona y no duplica pendientes",
          len(same) == 1 and same[0]["attempts"] == 1, same)

# ------------------------------------------------- reintentos con backoff
now = [1000.0]
outcomes = []


def transient(_item):
    outcomes.append(1)
    return "fallo", "transitorio"


sched3 = scheduler.Scheduler(
    state_path=TMP / "retry.json", max_jobs=1, retry_max=2, backoff=[10, 20], jitter=0,
    launch=transient, clock=lambda: now[0],
)
sched3.submit("x", action="run", trigger="cron", host="h")
sched3.tick()
wait_running(sched3)
with sched3._guard:
    item = sched3._queue[0]
    check("el transitorio se reencola con backoff",
          item["attempts"] == 1 and item["not_before"] == 1010.0, item)
check("aún no toca reintentar", sched3.tick() == 0)
now[0] = 1011.0
sched3.tick()
wait_running(sched3)
with sched3._guard:
    item = sched3._queue[0]
    check("el segundo fallo vuelve a reencolar",
          item["attempts"] == 2 and item["not_before"] == 1031.0, item)
now[0] = 1031.0
sched3.tick()
wait_running(sched3)
with sched3._guard:
    check("al agotar los reintentos queda FALLO",
          len(sched3._queue) == 0 and sched3._done[-1]["result"] == "fallo",
          sched3._done)
check("se intentó 3 veces en total (2 reintentos)", len(outcomes) == 3, len(outcomes))

# --------------------------------------------------- ficha/host y clasificación
sched4 = scheduler.Scheduler(state_path=TMP / "host.json", launch=lambda item: ("ok", ""))
sched4._job = lambda slug: {"slug": slug, "origin_host": "google"}
host = sched4.submit("g", action="run", trigger="cron")
with sched4._guard:
    check("el host sale de la ficha si no se envía", sched4._queue[0]["host"] == "google", host)
sched4.submit("s", action="size", trigger="manual")
with sched4._guard:
    size_item = next(i for i in sched4._queue if i["slug"] == "s")
    check("medir tamaño no ocupa cupo de host", size_item["host"] == "")

# ------------------------------------------------- recuperación tras reinicio
state = TMP / "restart.json"
blocking = BlockingLaunch()
s1 = scheduler.Scheduler(state_path=state, max_jobs=1, launch=blocking)
s1.submit("z", action="run", trigger="cron", host="h")
s1.tick()
time.sleep(0.1)
s2 = scheduler.Scheduler(state_path=state, max_jobs=1, launch=lambda item: ("ok", ""))
check("al reiniciar se reencola la corrida interrumpida", "z" in s2.queued_slugs(),
      s2.queued_slugs())
s2.tick()
wait_running(s2)
check("la ronda recuperada termina OK",
      s2.status()["done"][-1]["result"] == "ok", s2.status()["done"])
blocking.release.set()

# ------------------------------------------------------------------ IPC socket
config.SCHEDULER_SOCKET = TMP / "scheduler.sock"
server = scheduler._Server(config.SCHEDULER_SOCKET, scheduler.Scheduler(
    state_path=TMP / "ipc.json", launch=lambda item: ("ok", "")))
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    res = scheduler_client.submit("net", action="run", trigger="cron")
    check("submit por socket responde ok", res.get("ok") is True, res)
    state_ipc = scheduler_client.status()
    check("status por socket lista el pendiente",
          any(i["slug"] == "net" for i in state_ipc["pending"]), state_ipc)
    check("cancel por socket funciona", scheduler_client.cancel("net")["removed"] == 1)
finally:
    server.shutdown()
    server.server_close()

# ------------------------------------ flock ocupado: espera sin gastar reintentos
sched5 = scheduler.Scheduler(
    state_path=TMP / "blocked.json", max_jobs=1, retry_max=2, backoff=[10], jitter=0,
    launch=lambda item: ("skipped", "contencion"), clock=lambda: 5000.0,
)
sched5.submit("q", action="run", trigger="cron", host="h")
sched5.tick()
wait_running(sched5)
with sched5._guard:
    item = sched5._queue[0]
    check("el flock ocupado espera sin gastar reintentos",
          item["attempts"] == 0 and item["skips"] == 1 and item["not_before"] == 5030.0,
          item)

# ------------------------------------------------- integración: job real de prueba
fake_dir = TMP / "opt" / "jobs"
fake_dir.mkdir(parents=True, exist_ok=True)
gate_file = TMP / "gate.txt"
script = fake_dir / "fake.sh"
script.write_text(
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    f'LOG="{os.environ["BACKUP_LOG"]}/fake.log"\n'
    'mkdir -p "$(dirname "$LOG")"\n'
    "ts() { date '+%Y-%m-%d %H:%M:%S'; }\n"
    "printf '%s [fake] === inicio fake (dry_run=%s trigger=%s) ===\\n' "
    '"$(ts)" "${DRY_RUN:-0}" "${BACKUP_TRIGGERED_BY:-cron}" >> "$LOG"\n'
    f'printf \'%s\' "${{MIRROR_GATE-<unset>}}" > "{gate_file}"\n'
    "printf '%s [fake] === fin fake ===\\n' \"$(ts)\" >> \"$LOG\"\n",
    encoding="utf-8",
)
s_int = scheduler.Scheduler(state_path=TMP / "int.json", max_jobs=1)
s_int._job = lambda slug: {"slug": slug, "lockfile": str(TMP / "fake.lock"), "origin_host": "h"}
s_int.submit("fake", action="run", trigger="cron")
s_int.tick()
end = time.time() + 10
while not s_int.status()["done"] and time.time() < end:
    time.sleep(0.05)
done = s_int.status()["done"]
check("integración: el job corre y cierra OK",
      bool(done) and done[-1]["result"] == "ok", done)
check("integración: el daemon lanza con MIRROR_GATE desactivado",
      gate_file.exists() and gate_file.read_text() == "", gate_file.read_text() if gate_file.exists() else "(sin archivo)")
check("integración: no queda cupo ocupado", s_int.status()["running"] == [])

# ------------------------------------------- timeout de ronda: libera cupo y reencola
prev_timeout = config.SCHEDULER_JOB_TIMEOUT
config.SCHEDULER_JOB_TIMEOUT = 1
try:
    slow = fake_dir / "slow.sh"
    slow.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nsleep 30\n", encoding="utf-8",
    )
    s_to = scheduler.Scheduler(state_path=TMP / "timeout.json", max_jobs=1,
                               retry_max=3, backoff=[100], jitter=0)
    s_to._job = lambda slug: {"slug": slug, "lockfile": str(TMP / "slow.lock"),
                              "origin_host": "h"}
    s_to.submit("slow", action="run", trigger="cron", host="h")
    s_to.tick()
    end = time.time() + 15
    while s_to.status()["running"] and time.time() < end:
        time.sleep(0.05)
    st = s_to.status()
    check("timeout: libera el cupo y reencola",
          st["running"] == [] and any(i["slug"] == "slow" for i in st["pending"]), st)
    with s_to._guard:
        item = next(i for i in s_to._queue if i["slug"] == "slow")
    check("timeout: gasta un reintento (transitorio)", item["attempts"] == 1, item)
finally:
    config.SCHEDULER_JOB_TIMEOUT = prev_timeout

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
