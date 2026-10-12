"""Planificador de copias con paralelismo controlado (backupcsr-scheduler).

Es el dueño de la concurrencia: mantiene una cola persistente, limita cuántas copias
corren a la vez (global y por host de origen) y reintenta los fallos transitorios con
backoff en vez de marcarlos como FALLO de inmediato. cron y el portal encolan por el
socket Unix (`app/scheduler_client.py`); el daemon lanza los scripts reales con
`MIRROR_GATE=""` (el gate serial queda solo como respaldo para ejecuciones directas).

Uso: `python -m app.scheduler` (unidad systemd backupcsr-scheduler.service).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import re
import signal
import socketserver
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from . import config, logs, runner

log = logging.getLogger("backupcsr-scheduler")

ACTIONS = ("run", "dry", "retry", "size")
RETRYABLE = ("run", "dry", "retry")
DONE_KEEP = 50
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Precedencia al fusionar dos solicitudes del mismo slug: una corrida real (run/retry)
# nunca se deja caer detrás de un dry/size pendiente.
_ACTION_RANK = {"size": 1, "dry": 2, "retry": 3, "run": 3}


def parse_backoff(spec: str | None) -> list[int]:
    out: list[int] = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(float(part))
        except ValueError:
            continue
        if value > 0:
            out.append(value)
    return out or [300, 900, 2700]


def parse_jitter(spec: str | None) -> float:
    try:
        value = float(spec)
    except (TypeError, ValueError):
        return 0.2
    return max(0.0, min(0.9, value))


class Scheduler:
    """Cola en memoria respaldada por un JSON atómico; ejecuta cada job en su hilo."""

    def __init__(self, *, max_jobs=None, max_per_host=None, retry_max=None,
                 backoff=None, jitter=None, state_path=None, clock=time.time,
                 launch=None):
        self.max_jobs = max_jobs if max_jobs is not None else config.SCHEDULER_MAX_JOBS
        self.max_per_host = (
            max_per_host if max_per_host is not None else config.SCHEDULER_MAX_PER_HOST
        )
        self.retry_max = retry_max if retry_max is not None else config.SCHEDULER_RETRY_MAX
        self.backoff = backoff or parse_backoff(config.SCHEDULER_RETRY_BACKOFF)
        self.jitter = jitter if jitter is not None else parse_jitter(config.SCHEDULER_RETRY_JITTER)
        self.state_path = Path(state_path or config.SCHEDULER_STATE)
        self.clock = clock
        self._launch = launch  # hook de pruebas: (item) -> (outcome, error_class)
        # Cuando el flock del job está ocupado (corrida externa u huérfana) se espera
        # en tramos cortos sin gastar reintentos, hasta este tope.
        self.blocked_delay = 30.0
        self.max_skips = 60
        self.catalog_ttl = 10.0

        self._guard = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._queue: list[dict] = []
        self._running: list[dict] = []
        self._done: list[dict] = []
        self._jobs_cache: dict[str, dict] = {}
        self._jobs_cache_at = 0.0
        self._load()

    # ------------------------------------------------------------- persistencia
    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._queue = [self._norm(item) for item in data.get("queue", [])]
        self._done = list(data.get("done", []))[-DONE_KEEP:]
        interrupted = [self._norm(item) for item in data.get("running", [])]
        if interrupted:
            now = self.clock()
            for item in interrupted:
                item["not_before"] = now
            self._queue.extend(interrupted)
            log.warning(
                "reinicio: se reencolan %s corrida(s) interrumpida(s): %s",
                len(interrupted), [item["slug"] for item in interrupted],
            )
        self._save()

    def _norm(self, item: dict) -> dict:
        return {
            "slug": str(item.get("slug") or ""),
            "action": item.get("action") if item.get("action") in ACTIONS else "run",
            "trigger": "manual" if item.get("trigger") == "manual" else "cron",
            "username": str(item.get("username") or ""),
            "host": str(item.get("host") or ""),
            "dry_run": bool(item.get("dry_run")),
            "attempts": int(item.get("attempts") or 0),
            "skips": int(item.get("skips") or 0),
            "priority": int(item.get("priority") or 0),
            "not_before": float(item.get("not_before") or 0.0),
            "enqueued_at": float(item.get("enqueued_at") or self.clock()),
            "started_at": item.get("started_at"),
        }

    def _save(self) -> None:
        data = {
            "version": 1,
            "queue": self._queue,
            "running": self._running,
            "done": self._done[-DONE_KEEP:],
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("no se pudo guardar el estado del planificador: %s", exc)

    # ------------------------------------------------------------------ consulta
    def _priority(self, trigger: str) -> int:
        return 2 if trigger == "manual" else 1

    def _position(self, slug: str) -> int | None:
        for index, item in enumerate(self._sorted_queue(), start=1):
            if item["slug"] == slug:
                return index
        return None

    def _sorted_queue(self) -> list[dict]:
        return sorted(
            self._queue,
            key=lambda item: (-item["priority"], item["not_before"], item["enqueued_at"]),
        )

    def _host_for(self, slug: str, action: str) -> str:
        if action == "size":
            return ""
        job = self._job(slug)
        value = str((job or {}).get("origin_host") or "").strip()
        if value:
            return value
        # Sin host declarado no se puede agrupar: se aísla por slug para no frenar
        # a otras tareas, y se avisa para corregir el catálogo.
        log.warning("job %s sin origin_host: cupo por host desactivado para él", slug)
        return f"slug:{slug}"

    def _catalog(self) -> dict[str, dict]:
        """Catálogo slug->job con TTL corto: `effective_jobs()` lee YAML+BD+cron."""
        now = self.clock()
        if self._jobs_cache and now - self._jobs_cache_at < self.catalog_ttl:
            return self._jobs_cache
        try:
            from . import api
            jobs = {job["slug"]: job for job in api.effective_jobs()}
        except Exception:  # noqa: BLE001
            log.exception("no se pudo cargar el catálogo de jobs")
            jobs = self._jobs_cache
        self._jobs_cache, self._jobs_cache_at = jobs, now
        return jobs

    def _job(self, slug: str) -> dict | None:
        return self._catalog().get(slug)

    def status(self) -> dict:
        with self._guard:
            return {
                "available": True,
                "max_jobs": self.max_jobs,
                "max_per_host": self.max_per_host,
                "running": [
                    {key: item.get(key) for key in
                     ("slug", "action", "trigger", "host", "attempts", "started_at")}
                    for item in self._running
                ],
                "pending": [
                    {key: item.get(key) for key in
                     ("slug", "action", "trigger", "host", "attempts", "not_before", "priority")}
                    for item in self._sorted_queue()
                ],
                "done": list(self._done[-20:]),
                "active": bool(self._running or self._queue),
            }

    def queued_slugs(self) -> set[str]:
        with self._guard:
            return {item["slug"] for item in self._queue}

    # -------------------------------------------------------------------- encolar
    def submit(self, slug: str, action: str = "run", trigger: str = "cron",
               username: str = "", host: str = "", dry_run: bool = False) -> dict:
        slug = str(slug or "").strip()
        if not slug:
            return {"ok": False, "error": "slug vacío"}
        if not SLUG_RE.match(slug):
            return {"ok": False, "error": f"slug inválido: {slug}"}
        if action not in ACTIONS:
            return {"ok": False, "error": f"acción inválida: {action}"}
        trigger = "manual" if trigger == "manual" else "cron"
        if not host:
            host = self._host_for(slug, action)  # fuera del lock: toca YAML/BD/cron
        with self._guard:
            pending = next((item for item in self._queue if item["slug"] == slug), None)
            if pending is not None:
                # Nunca fusionar en silencio una corrida real detrás de un dry/size
                # ni al revés: gana la acción de mayor precedencia.
                if _ACTION_RANK[action] > _ACTION_RANK[pending["action"]]:
                    pending["action"] = action
                    pending["dry_run"] = dry_run
                    if action != "size":
                        pending["host"] = host or pending["host"]
                pending["priority"] = max(pending["priority"], self._priority(trigger))
                if trigger == "manual":
                    pending["trigger"] = "manual"
                if username:
                    pending["username"] = username
                self._save()
                return {"ok": True, "coalesced": True, "state": "pending",
                        "action": pending["action"], "dry_run": pending["dry_run"],
                        "position": self._position(slug), "queue": len(self._queue)}
            running = any(item["slug"] == slug for item in self._running)
            item = self._norm({
                "action": action, "trigger": trigger, "username": username, "host": host,
                "dry_run": dry_run, "priority": self._priority(trigger),
                "not_before": self.clock(), "enqueued_at": self.clock(),
            })
            item["slug"] = slug
            self._queue.append(item)
            self._save()
        self._wake.set()
        return {"ok": True, "coalesced": False, "state": "running" if running else "pending",
                "queued_after_running": running, "action": item["action"],
                "dry_run": item["dry_run"], "position": self._position(slug),
                "queue": len(self._queue)}

    def cancel(self, slug: str) -> dict:
        slug = str(slug or "").strip()
        with self._guard:
            before = len(self._queue)
            self._queue = [item for item in self._queue if item["slug"] != slug]
            removed = before - len(self._queue)
            if removed:
                self._save()
        return {"ok": True, "removed": removed, "slug": slug}

    # ------------------------------------------------------------------ ejecutar
    def tick(self, now: float | None = None) -> int:
        """Lanza todas las tareas que quepan en los cupos. Devuelve cuántas arrancó."""
        now = self.clock() if now is None else now
        starts: list[dict] = []
        with self._guard:
            hosts = Counter(item["host"] for item in self._running if item.get("host"))
            for item in self._sorted_queue():
                if len(self._running) >= self.max_jobs:
                    break
                if item["not_before"] > now:
                    continue
                host = item.get("host") or ""
                if host and hosts[host] >= self.max_per_host:
                    continue
                self._queue.remove(item)
                item["started_at"] = now
                self._running.append(item)
                if host:
                    hosts[host] += 1
                starts.append(item)
            if starts:
                self._save()
        for item in starts:
            threading.Thread(
                target=self._run_item, args=(item,),
                name=f"backupcsr-job-{item['slug']}", daemon=True,
            ).start()
        return len(starts)

    def _run_item(self, item: dict) -> None:
        try:
            outcome, error_class = self._execute(item)
        except Exception:  # noqa: BLE001
            log.exception("el planificador falló ejecutando %s", item.get("slug"))
            outcome, error_class = "error", "fatal"
        self._finish(item, outcome, error_class)

    def _execute(self, item: dict) -> tuple[str, str]:
        if self._launch is not None:
            return self._launch(item)
        if item["action"] == "size":
            return self._run_size(item)
        return self._run_script(item)

    def _run_script(self, item: dict) -> tuple[str, str]:
        slug = item["slug"]
        job = self._job(slug)
        script = (config.JOBS_DIR / f"{slug}.sh").resolve()
        if script.parent != config.JOBS_DIR.resolve() or not script.is_file():
            log.error("script del job %s fuera de %s o inexistente: %s",
                      slug, config.JOBS_DIR, script)
            return "error", "fatal"
        handle = None
        try:
            handle = open(runner.lock_path(job or {"slug": slug}), "a+")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if handle is not None:
                handle.close()
            log.warning("job %s: el flock está ocupado (ejecución externa)", slug)
            return "skipped", "contencion"
        dry = item["action"] == "dry" or item.get("dry_run")
        env = os.environ.copy()
        env["MIRROR_GATE"] = ""  # el daemon ya controla la concurrencia
        env["DRY_RUN"] = "1" if dry else "0"
        env["BACKUP_TRIGGERED_BY"] = "manual" if item["trigger"] == "manual" else "cron"
        timeout = config.SCHEDULER_JOB_TIMEOUT or None
        timed_out = False
        try:
            # start_new_session: el job (y lftp) forman su propio grupo para poder
            # matarlos juntos si se excede el timeout. El hijo hereda el fd del flock:
            # si el daemon muere, el lock sigue tomado hasta que el job termine y no
            # se solapa un segundo mirror.
            process = subprocess.Popen(
                ["/bin/bash", str(script)], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(handle.fileno(),),
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(process)
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()
        if timed_out:
            log.error("job %s: excedió el tiempo máximo (%ss); se reencola como transitorio",
                      slug, config.SCHEDULER_JOB_TIMEOUT)
            return "fallo", "transitorio"
        return self._classify(slug)

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        """Mata el grupo del job (SIGTERM y, si no cede, SIGKILL)."""
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("job pid %s no terminó tras SIGKILL", process.pid)

    def _classify(self, slug: str) -> tuple[str, str]:
        run = logs.latest_run(slug)
        if not run:
            return "error", "fatal"
        status = run.get("status")
        error_class = run.get("error_class") or ""
        if status == "OK":
            return "ok", error_class
        if status == "PARCIAL":
            return "parcial", error_class or "parcial"
        if status == "OMITIDO":
            return "omitido", error_class or "contencion"
        if not error_class:
            error_class = logs.classify_error(run.get("error_text"))
        return "fallo", error_class

    def _run_size(self, item: dict) -> tuple[str, str]:
        from . import files, sizes
        job = self._job(item["slug"])
        if job is None:
            return "error", "fatal"
        if not files.nas_ready():
            log.warning("size de %s: el NAS no está montado", item["slug"])
            return "error", "fatal"
        try:
            size = sizes.job_size(job, force=True)
        except Exception:  # noqa: BLE001
            log.exception("size de %s falló", item["slug"])
            return "error", "fatal"
        job_id = job.get("id")
        if job_id and size.get("bytes") is not None:
            try:
                sizes.save_snapshot(job_id, size)
            except Exception:  # noqa: BLE001
                log.exception("no se pudo guardar el snapshot de %s", item["slug"])
        return "ok", ""

    def _delay(self, attempt: int) -> float:
        index = min(max(attempt, 0), len(self.backoff) - 1)
        base = self.backoff[index]
        factor = 1 + random.uniform(-self.jitter, self.jitter)
        return max(1.0, base * factor)

    def _finish(self, item: dict, outcome: str, error_class: str) -> None:
        requeue = False
        with self._guard:
            self._running = [entry for entry in self._running if entry is not item]
            # "bloqueado" = el flock lo tiene una corrida externa (cron/manual o un job
            # huérfano tras un reinicio): se espera sin gastar reintentos.
            blocked = outcome in ("omitido", "skipped")
            transient = blocked or (outcome == "fallo" and error_class == "transitorio")
            wait_ok = blocked and item.get("skips", 0) < self.max_skips
            retry_ok = (not blocked and item["action"] in RETRYABLE
                        and item["attempts"] < self.retry_max)
            if transient and (wait_ok or retry_ok):
                if blocked:
                    item["skips"] = item.get("skips", 0) + 1
                    delay = self.blocked_delay
                else:
                    delay = self._delay(item["attempts"])
                    item["attempts"] += 1
                item["not_before"] = self.clock() + delay
                # Un solo pendiente por slug: si ya hay otro (p. ej. una corrida real
                # solicitada mientras este corría), se fusiona y no se ejecuta dos veces.
                existing = next(
                    (i for i in self._queue if i["slug"] == item["slug"]), None
                )
                if existing is not None:
                    existing["attempts"] = max(existing["attempts"], item["attempts"])
                    existing["skips"] = max(existing.get("skips", 0), item.get("skips", 0))
                    existing["priority"] = max(existing["priority"], item["priority"])
                    existing["not_before"] = min(existing["not_before"], item["not_before"])
                else:
                    self._queue.append(item)
                requeue = True
                result = "reintento"
                log.info("job %s: %s, %s en %.0fs",
                         item["slug"], outcome,
                         f"espera {item['skips']}/{self.max_skips}" if blocked
                         else f"reintento {item['attempts']}/{self.retry_max}", delay)
            else:
                result = outcome
                self._done.append({
                    "slug": item["slug"], "result": result,
                    "attempts": item["attempts"], "finished_at": self.clock(),
                })
                self._done = self._done[-DONE_KEEP:]
                log.info("job %s: %s (intentos=%s)", item["slug"], result, item["attempts"])
            self._save()
        if requeue:
            self._wake.set()

    # --------------------------------------------------------------------- loop
    def run_loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(config.SCHEDULER_POLL)
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, scheduler: Scheduler):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o660)
        self.scheduler = scheduler

    def dispatch(self, req: dict) -> dict:
        op = str(req.get("op") or "")
        try:
            if op == "submit":
                return self.scheduler.submit(
                    str(req.get("slug") or ""), action=str(req.get("action") or "run"),
                    trigger=str(req.get("trigger") or "cron"),
                    username=str(req.get("username") or ""), host=str(req.get("host") or ""),
                    dry_run=bool(req.get("dry_run")),
                )
            if op == "status":
                return self.scheduler.status()
            if op == "cancel":
                return self.scheduler.cancel(str(req.get("slug") or ""))
            return {"ok": False, "error": f"op desconocida: {op}"}
        except Exception:  # noqa: BLE001
            log.exception("error atendiendo la operación %s", op)
            return {"ok": False, "error": "error interno"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65536)
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            response = {"ok": False, "error": "json inválido"}
        else:
            response = self.server.dispatch(req)
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    scheduler = Scheduler()
    server = _Server(config.SCHEDULER_SOCKET, scheduler)
    threading.Thread(target=server.serve_forever, name="backupcsr-ipc", daemon=True).start()
    log.info(
        "planificador en %s (socket %s, max_jobs=%s, max_per_host=%s)",
        config.SCHEDULER_STATE, config.SCHEDULER_SOCKET,
        scheduler.max_jobs, scheduler.max_per_host,
    )

    def _shutdown(signum, frame):  # noqa: ANN001
        log.info("señal %s: cerrando planificador", signum)
        scheduler.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    scheduler.run_loop()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
