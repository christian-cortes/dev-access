"""Pruebas del cálculo de progreso del panel (app/api.py `_progress`).

El progreso no toca los scripts de copia: en curso se estima con la duración típica
del histórico de `runs` y en reposo se muestra frescura/salud. Aquí se valida la
fórmula y que `/api/jobs` (collect_jobs) incluya el campo `progress` en cada tarea.

Corre con el python del venv del portal (necesita fastapi + yaml) y usa un directorio
temporal: **no** toca /mnt/nas, /etc, /run/lock ni MySQL.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_progress.py
"""
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import yaml

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="progress-"))
os.environ["BACKUP_NAS"] = str(TMP / "nas")
os.environ["BACKUP_OPT"] = str(TMP / "opt")
os.environ["BACKUP_LOG"] = str(TMP / "log")
os.environ["BACKUP_ETC"] = str(TMP / "etc")
os.environ["BACKUP_CRON"] = str(TMP / "cron" / "backupcsr")
os.environ["BACKUP_CATALOG"] = str(TMP / "jobs.yml")
Path(os.environ["BACKUP_NAS"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["BACKUP_LOG"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["BACKUP_CATALOG"]).write_text(
    yaml.safe_dump({
        "jobs": [{
            "slug": "demo", "name": "Demo", "enabled": True,
            "dest_rel": "demo", "lockfile": str(TMP / "locks" / "demo.lock"),
        }],
    }, allow_unicode=True),
    encoding="utf-8",
)

from app import api, logs  # noqa: E402

REF = logs.now()
USER = {"username": "tester", "role": "admin"}

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


def run(started, finished, status="OK"):
    return {"started_at": started, "finished_at": finished, "status": status}


# ------------------------------------------------- duración típica (mediana)
base = REF - timedelta(days=1)
runs = [run(base + timedelta(hours=i), base + timedelta(hours=i, minutes=10)) for i in range(3)]
check("típica: mediana de corridas OK", api._typical_duration(runs) == 600,
      api._typical_duration(runs))
check("típica: sin corridas OK es None",
      api._typical_duration([run(base, base + timedelta(minutes=5), status="FALLO")]) is None)

# ------------------------------------------------------------- en curso
running = run(REF - timedelta(minutes=5), None, status="EN_CURSO")
p = api._progress({"enabled": 1, "slug": "demo"}, runs, running, True, "EN_CURSO", None, REF)
check("en curso: estado running", p["state"] == "running" and p["live"] is True, p)
check("en curso: sin % real => percent None y cuenta el tiempo",
      p["percent"] is None and p["real"] is False and p["elapsed_s"] == 300, p)
check("en curso: el detalle lleva el tiempo transcurrido", p["detail"] == "en curso · 5m",
      p["detail"])
check("en curso: un % medido real se respeta",
      api._progress({"enabled": 1}, runs, {**running, "percent": 42}, True,
                    "EN_CURSO", None, REF)["percent"] == 42)
real = api._progress({"enabled": 1}, runs, {**running, "percent": 150}, True,
                     "EN_CURSO", None, REF)
check("en curso: % real acotado a 100 y marcado real",
      real["percent"] == 100 and real["real"] is True, real)
check("en curso: sin histórico => percent None, no bloquea el tiempo",
      api._progress({"enabled": 1}, [], running, True, "EN_CURSO", None, REF)["percent"] is None)
check("en curso: EN_CURSO sigue vivo aunque el lockfile no sea legible (running=False)",
      api._progress({"enabled": 1}, runs, running, False, "EN_CURSO", None, REF)["live"] is True)

# ------------------------------------------------------------- en reposo
job = {"enabled": 1, "slug": "demo"}
started = REF - timedelta(minutes=30)
latest = run(started, REF - timedelta(minutes=10))
next_dt = started + timedelta(hours=1)   # 30 min por venir de una ventana de 60 min
p = api._progress(job, [], latest, False, "OK", next_dt, REF)
check("reposo OK: frescura 50%", p["state"] == "ok" and p["percent"] == 50, p)
check("reposo OK: detalle próxima", "próxima en" in p["detail"], p["detail"])
overdue_started = REF - timedelta(hours=2)
overdue_latest = run(overdue_started, overdue_started + timedelta(minutes=10))
overdue = api._progress(job, [], overdue_latest, False, "TARDE",
                        REF - timedelta(hours=1), REF)
check("reposo TARDE: vencida", overdue["state"] == "late" and "vencida" in overdue["detail"], overdue)

check("fallo: 0%", api._progress(job, [], latest, False, "FALLO", next_dt, REF)["percent"] == 0)
check("deshabilitada: estado disabled",
      api._progress({"enabled": 0}, [], latest, False, "DESHABILITADA", None, REF)["state"] == "disabled")

# ------------------------------------------- wiring en collect_jobs (/api/jobs)
api.db.available = lambda: False
api.runner.is_running = lambda job: False
api.sizes.warm = lambda *args, **kwargs: False
collected = api.collect_jobs()
check("collect_jobs: una tarea con progress",
      len(collected) == 1 and "progress" in collected[0], collected)
check("collect_jobs: progress con las claves esperadas",
      set(collected[0]["progress"]) == {
          "state", "percent", "live", "real", "elapsed_s", "typical_s", "detail"},
      collected[0]["progress"])
check("collect_jobs: incluye id para el snapshot de tamaño del lote", "id" in collected[0],
      collected[0].keys())

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
