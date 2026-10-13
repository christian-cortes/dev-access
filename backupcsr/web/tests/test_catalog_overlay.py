"""Pruebas del overlay de tareas en /etc/backupcsr/jobs.d (app/catalog.py).

El overlay es donde el portal guarda las tareas que crea; debe fusionarse con
jobs.yml, sobrevivir sin BD y no ser deshabilitado por `sync_jobs`.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_catalog_overlay.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="overlay-"))
os.environ["BACKUP_OPT"] = str(TMP / "opt")
os.environ["BACKUP_ETC"] = str(TMP / "etc")
os.environ["BACKUP_CATALOG"] = str(TMP / "jobs.yml")
os.environ["BACKUP_JOBS_DIR"] = str(TMP / "etc" / "jobs.d")

base = {
    "settings": {"timezone": "America/Bogota", "default_enabled": True},
    "jobs": [
        {"slug": "base-bd", "name": "Base BD", "origin_type": "ftp",
         "source": "taskManager/base", "dest_rel": "base",
         "cron": {"minute": "10", "hour": "6-19"}, "sort": 10},
    ],
}
Path(os.environ["BACKUP_CATALOG"]).write_text(
    yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
)

from app import catalog, config  # noqa: E402

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


# --------------------------------------------------------------- validación de slug
check("slug válido", catalog.valid_slug("acme-bd"))
check("slug con mayúsculas no", not catalog.valid_slug("Acme"))
check("slug con guion final no", not catalog.valid_slug("acme-"))
check("slug con espacio no", not catalog.valid_slug("acme bd"))

# ------------------------------------------------------------------- overlay
job = {
    "slug": "acme-bd", "name": "Acme BD", "description": "tarea del portal",
    "origin_type": "sftp", "cred_prefix": "ACME", "origin_host": "acme",
    "key_name": "acme", "source": "/srv/dumps", "dest_rel": "acme/dumps",
    "lockfile": "/run/lock/backupcsr-acme-bd.lock", "enabled": 1,
    "cron_minute": "33", "cron_hour": "7-18",
    "cron_dom": "*", "cron_month": "*", "cron_dow": "*",
    "sort": 5, "criticality": "alta", "owner": "Infra", "retention_days": 30,
    "tags": "db, critico", "size_exclude": "", "notes": "portal",
    "routes": [{"source": "/srv/dumps", "dest_rel": "acme/dumps"}],
}
catalog.save_overlay(job)
check("overlay escrito", catalog.is_managed("acme-bd"))
check("load_overlay devuelve las rutas",
      catalog.load_overlay("acme-bd")["routes"][0]["dest_rel"] == "acme/dumps")
check("load_overlay marca managed_by=portal",
      catalog.load_overlay("acme-bd")["managed_by"] == "portal")

jobs = {item["slug"]: item for item in catalog.catalog_jobs()}
check("catálogo fusiona base + overlay", sorted(jobs) == ["acme-bd", "base-bd"], sorted(jobs))
check("overlay: se conservan el nombre y la criticidad",
      jobs["acme-bd"]["name"] == "Acme BD" and jobs["acme-bd"]["criticality"] == "alta")
check("overlay: horario aplanado se respeta",
      jobs["acme-bd"]["cron_minute"] == "33" and jobs["acme-bd"]["cron_hour"] == "7-18",
      (jobs["acme-bd"]["cron_minute"], jobs["acme-bd"]["cron_hour"]))
check("base: horario anidado sigue funcionando",
      jobs["base-bd"]["cron_minute"] == "10" and jobs["base-bd"]["cron_hour"] == "6-19")
check("overlay: managed_by llega al catálogo", jobs["acme-bd"]["managed_by"] == "portal")
check("base: managed_by vacío", jobs["base-bd"]["managed_by"] == "")

# ------------------------------------------------- sync no deshabilita el overlay
EXECUTED = []


def capture(sql, params=None):
    EXECUTED.append((sql, params))
    return 1


catalog.db.execute = capture
catalog.sync_jobs()
last_sql, last_params = EXECUTED[-1]
check("sync: el UPDATE de deshabilitados incluye el slug del overlay",
      "enabled = 0" in last_sql and "acme-bd" in (last_params or []), last_params)

# --------------------------------------------------------- borrado del overlay
check("delete_overlay borra", catalog.delete_overlay("acme-bd"))
check("tras borrar no está manageado", not catalog.is_managed("acme-bd"))
check("delete_overlay idempotente", catalog.delete_overlay("acme-bd") is False)

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
