"""Pruebas de las tareas deshabilitadas del catálogo (`jobs.yml`).

Una tarea `enabled: false` debe listarse en el portal pero **no** generar línea de cron,
**no** disparar alertas, **no** medirse (puede no tener destino todavía) y **no** poder
habilitarse si falta `/opt/backupcsr/jobs/<slug>.sh`.

Corre con el python del venv del portal (necesita fastapi + yaml) y usa un directorio
temporal: **no** toca /mnt/nas, /etc, /run/lock ni MySQL.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_jobs_disabled.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from fastapi import HTTPException

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="jobs-"))
os.environ["BACKUP_NAS"] = str(TMP / "nas")
os.environ["BACKUP_OPT"] = str(TMP / "opt")
os.environ["BACKUP_LOG"] = str(TMP / "log")
os.environ["BACKUP_ETC"] = str(TMP / "etc")
os.environ["BACKUP_CRON"] = str(TMP / "cron" / "backupcsr")
os.environ["BACKUP_CATALOG"] = str(TMP / "jobs.yml")
os.environ["BACKUP_MANAGE_CRON"] = "1"
Path(os.environ["BACKUP_NAS"]).mkdir(parents=True, exist_ok=True)

# Copia del catálogo real con los lockfiles movidos al temporal (no tocar /run/lock).
REAL_CATALOG = yaml.safe_load((WEB_DIR / "jobs.yml").read_text(encoding="utf-8"))
for item in REAL_CATALOG["jobs"]:
    item["lockfile"] = str(TMP / "locks" / f"backupcsr-{item['slug']}.lock")
Path(os.environ["BACKUP_CATALOG"]).write_text(
    yaml.safe_dump(REAL_CATALOG, allow_unicode=True, sort_keys=False), encoding="utf-8"
)

from app import api, catalog, config, cronfile, files, sizes  # noqa: E402

files.nas_ready = lambda: True
config.ETC_DIR.mkdir(parents=True, exist_ok=True)
cronfile.reload_cron = lambda: True
USER = {"username": "tester", "role": "admin"}

DISABLED = ["google-web", "ticware-bd", "ticware-web"]
ENABLED = ["google-bd", "latino-bd", "latino-web", "ruta56-bd", "ruta56-web", "gastro-bd", "enter-bd"]

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


def code(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except HTTPException as exc:
        return exc.status_code
    return None


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- catálogo real
raw = yaml.safe_load((WEB_DIR / "jobs.yml").read_text(encoding="utf-8"))["jobs"]
raw_disabled = {item["slug"]: item for item in raw if item.get("enabled") is False}
check("jobs.yml: las 3 deshabilitadas están registradas", sorted(raw_disabled) == DISABLED,
      sorted(raw_disabled))
check("jobs.yml: las 7 habilitadas no llevan enabled: false",
      [item["slug"] for item in raw if item.get("enabled") is False] == DISABLED)

jobs = {job["slug"]: job for job in catalog.catalog_jobs()}
check("catálogo: 10 tareas", sorted(jobs) == sorted(DISABLED + ENABLED), sorted(jobs))
for slug in DISABLED:
    check(f"{slug}: enabled=0", jobs.get(slug, {}).get("enabled") == 0)
for slug in ENABLED:
    check(f"{slug}: enabled=1", jobs.get(slug, {}).get("enabled") == 1)
check("deshabilitada: horario por defecto del YAML",
      jobs["ticware-bd"]["cron_minute"] == "20" and jobs["ticware-bd"]["cron_hour"] == "6-19")
check("deshabilitada: lockfile propio",
      jobs["ticware-bd"]["lockfile"].endswith("backupcsr-ticware-bd.lock"))
check("deshabilitada: describe por qué quedó fuera",
      "NAS" in jobs["ticware-bd"]["description"] and "OneDrive" in jobs["google-web"]["description"])
check("ficha: campos nuevos con defaults",
      jobs["ruta56-bd"]["criticality"] == "media" and jobs["ruta56-bd"]["owner"] == ""
      and jobs["ruta56-bd"]["tags"] == "" and jobs["ruta56-bd"]["notes"] == ""
      and jobs["ruta56-bd"]["retention_days"] is None)
check("ficha: size_exclude de ruta56-bd", jobs["ruta56-bd"]["size_exclude"] == ["storage"])

# ------------------------------------------------------------------- cron
rendered = cronfile.render(catalog.catalog_jobs())
check("cron: ninguna línea de las deshabilitadas", not any(slug in rendered for slug in DISABLED))
check("cron: las 7 habilitadas siguen", all(slug in rendered for slug in ENABLED))
config.CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
cronfile.write(catalog.catalog_jobs())
written = config.CRON_FILE.read_text(encoding="utf-8")
check("cron escrito: sin deshabilitadas", not any(slug in written for slug in DISABLED))
check("cron escrito: valida solo las habilitadas",
      cronfile.parse().keys() >= set(ENABLED))

# ------------------------------------------------------------------ panel
collected = {job["slug"]: job for job in api.collect_jobs()}
check("panel: lista las deshabilitadas", set(collected) >= set(DISABLED))
row = collected["ticware-web"]
check("panel: estado DESHABILITADA", row["status"] == "DESHABILITADA", row["status"])
check("panel: sin próxima ejecución", row["next_run"] is None)
check("panel: tamaño no medido", row["size"].get("disabled") is True and row["size_bytes"] is None)
check("panel: las habilitadas sí calculan próxima ejecución",
      collected["gastro-bd"]["next_run"] is not None)

# KPIs del panel: las deshabilitadas no cuentan como "fallos / sin datos".
sizes.warm = lambda *args, **kwargs: False
counts = run(api.summary(USER))["counts"]
check("resumen: 3 deshabilitadas de 10", counts.get("DESHABILITADA") == 3 and counts.get("total") == 10,
      counts)
check("resumen: NUNCA solo las 7 habilitadas", counts.get("NUNCA") == 7 and counts.get("habilitados") == 7,
      counts)

# --------------------------------------------------------------- alertas
def job_alerts(jobs_list):
    return [alert for alert in api.build_alerts(jobs_list) if alert.get("kind") == "job"]


fake = [{"slug": "ticware-bd", "name": "Ticware BD", "enabled": 0, "status": "FALLO",
         "status_detail": "x"}]
check("alertas: ignora la deshabilitada", job_alerts(fake) == [])
check("alertas: avisa si está habilitada",
      len(job_alerts([{**fake[0], "enabled": 1}])) == 1)
check("alertas: solo de tareas habilitadas",
      all(alert["slug"] not in DISABLED for alert in job_alerts(api.collect_jobs())),
      sorted({alert["slug"] for alert in job_alerts(api.collect_jobs())}))

# El aviso de cola ocupada (OMITIDO) se silencia hasta 3 rondas seguidas.
base = {"slug": "gastro-bd", "name": "Gastro BD", "enabled": 1, "status": "OK",
        "status_detail": "ok", "omitted_consecutive": 1}
check("alertas: una omisión aislada no alarma", job_alerts([base]) == [], job_alerts([base]))
contencion = [a for a in job_alerts([{**base, "omitted_consecutive": 3}])
              if a.get("class") == "contencion"]
check("alertas: 3 omisiones seguidas sí alarman", len(contencion) == 1, contencion)

# -------------------------------------------------------------- medición
files.nas_ready = lambda: True
anchor = Path(os.environ["BACKUP_NAS"]) / "ticware-bd" / "sub"
anchor.mkdir(parents=True)
(anchor / "a.bin").write_bytes(b"1234567")
job = {"slug": "ticware-bd", "dest_rel": "ticware-bd", "enabled": 0,
       "lockfile": str(TMP / "locks" / "measure.lock")}
sizes._cache.clear()
measured = sizes.job_size(job)
check("medición: deshabilitada no se mide",
      measured["disabled"] is True and measured["bytes"] is None, measured)
job["enabled"] = 1
measured = sizes.job_size(job)
check("medición: habilitada sí se mide", measured["bytes"] == 7 and measured["files"] == 1, measured)
sizes._cache.clear()

# --------------------------------------------------- habilitar exige script
api.db.available = lambda: True
api.db.execute = lambda *args, **kwargs: None
api.db.query = lambda *args, **kwargs: []
api.cronfile.write = lambda jobs_list: None
api.audit = lambda username, action, slug=None, detail=None: None

patch = api.JobPatch(enabled=True)
check("habilitar sin script: 409",
      code(lambda: run(api.update_job("ticware-bd", patch, USER))) == 409)
script = config.JOBS_DIR / "ticware-bd.sh"
script.parent.mkdir(parents=True, exist_ok=True)
script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
script.chmod(0o755)
check("habilitar con script: ok",
      run(api.update_job("ticware-bd", patch, USER)).get("ok") is True)
check("deshabilitar no exige script",
      run(api.update_job("latino-web", api.JobPatch(enabled=False), USER)).get("ok") is True)

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
