"""Pruebas de la API de crear/editar/eliminar tareas (app/api.py).

Cubre el guard BACKUP_MANAGE_JOBS, la creación (script + overlay + BD + cron), la
vista previa, la edición, el borrado, la validación y las credenciales.

Corre con el python del venv (necesita fastapi + yaml) y usa un directorio temporal:
**no** toca /mnt/nas, /etc, /run/lock ni MySQL.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_jobs_create.py
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

TMP = Path(tempfile.mkdtemp(prefix="jobs-create-"))
os.environ["BACKUP_NAS"] = str(TMP / "nas")
os.environ["BACKUP_OPT"] = str(TMP / "opt")
os.environ["BACKUP_LOG"] = str(TMP / "log")
os.environ["BACKUP_ETC"] = str(TMP / "etc")
os.environ["BACKUP_CRON"] = str(TMP / "cron" / "backupcsr")
os.environ["BACKUP_CATALOG"] = str(TMP / "jobs.yml")
os.environ["BACKUP_JOBS_DIR"] = str(TMP / "etc" / "jobs.d")
os.environ["BACKUP_CREDENTIALS"] = str(TMP / "etc" / "credentials.env")
os.environ["BACKUP_MANAGE_CRON"] = "1"
os.environ["BACKUP_MANAGE_JOBS"] = "1"
Path(os.environ["BACKUP_NAS"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["BACKUP_ETC"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["BACKUP_CREDENTIALS"]).write_text(
    "RUTA56_HOST=162.240.175.168\nRUTA56_USER=wwruta\nRUTA56_PASS=secreto\n",
    encoding="utf-8",
)
Path(os.environ["BACKUP_CATALOG"]).write_text(
    yaml.safe_dump(
        {"settings": {"default_enabled": True}, "jobs": [
            {"slug": "base-bd", "name": "Base BD", "origin_type": "ftp",
             "origin_host": "base", "source": "taskManager/base", "dest_rel": "base",
             "lockfile": str(TMP / "locks" / "base.lock"),
             "cron": {"minute": "10", "hour": "6-19"}, "sort": 10},
        ]},
        allow_unicode=True, sort_keys=False,
    ),
    encoding="utf-8",
)

from app import api, catalog, config, cronfile, db  # noqa: E402

USER = {"username": "tester", "role": "admin"}
checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


def run(coro):
    return asyncio.run(coro)


def code(fn):
    try:
        fn()
    except HTTPException as exc:
        return exc.status_code
    return None


EXECUTED = []
WRITES = []


def capture(sql, params=None):
    EXECUTED.append((sql, params))
    return 1


db.available = lambda: True
db.execute = capture
db.query = lambda sql, params=None, one=False: ([] if not one else None)
api.audit = lambda *args, **kwargs: None
cronfile.reload_cron = lambda: True
api.cronfile.write = lambda jobs: WRITES.append(jobs)
api.logs.parse_slug = lambda slug: []
api.runner.is_running = lambda job: False
api.sizes.current = lambda job: {"bytes": None, "files": 0, "pending": False}


def definition(**overrides):
    data = dict(
        slug="acme-bd", name="Acme BD", description="dumps del portal",
        origin_type="ftp", cred_prefix="RUTA56", cred_mode="reuse",
        routes=[api.RouteIn(source="taskManager/acme", dest_rel="acme")],
        enabled=True, cron_minute="37", cron_hour="6-20",
        sort=55, criticality="alta",
    )
    data.update(overrides)
    return api.JobDefIn(**data)


# ------------------------------------------------------------------ crear
EXECUTED.clear()
WRITES.clear()
result = run(api.create_job(definition(), USER))
check("crear: ok", result.get("ok") is True, result)
check("crear: script instalado", (config.JOBS_DIR / "acme-bd.sh").is_file())
script = (config.JOBS_DIR / "acme-bd.sh").read_text(encoding="utf-8")
check("crear: script usa el prefijo de credenciales", "$RUTA56_HOST" in script, script)
check("crear: script espeja al destino", "$BACKUPCSR_NAS_ROOT/acme" in script, script)
check("crear: overlay guardado", catalog.is_managed("acme-bd"))
overlay = catalog.load_overlay("acme-bd")
check("crear: overlay conserva las rutas",
      overlay["routes"][0]["source"] == "taskManager/acme", overlay.get("routes"))
check("crear: overlay marca managed_by", overlay["managed_by"] == "portal")
check("crear: inserta en BD", any("INSERT INTO jobs" in sql for sql, _ in EXECUTED))
check("crear: reescribe el cron", len(WRITES) == 1, WRITES)
check("crear: el job aparece en el panel", result["job"].get("slug") == "acme-bd", result.get("job"))

EXECUTED.clear()
check("crear: slug duplicado 409", code(lambda: run(api.create_job(definition(), USER))) == 409)

# ------------------------------------------------------------------ vista previa
data = run(api.preview_job(definition(slug="otra-bd"), USER))
check("preview: devuelve el script", "mirror_ftp" in data.get("script", ""), data.get("script"))
check("preview: script válido", data.get("valid") is True, data)
check("preview: no instala nada", not (config.JOBS_DIR / "otra-bd.sh").exists())
check("preview: trae próximas ejecuciones", len(data.get("next", [])) == 5, data.get("next"))
check("preview: no crea overlay", not catalog.is_managed("otra-bd"))

# ------------------------------------------------------------------ validaciones
check("slug inválido 400",
      code(lambda: run(api.create_job(definition(slug="a_b"), USER))) == 400)
check("sin rutas 400",
      code(lambda: run(api.create_job(definition(slug="val-noroute", routes=[]), USER))) == 400)
check("destino absoluto 400",
      code(lambda: run(api.create_job(definition(
          slug="val-abs", routes=[api.RouteIn(source="a", dest_rel="/etc")]), USER))) == 400)
check("prefijo inexistente en reuse 400",
      code(lambda: run(api.create_job(definition(slug="val-prefix", cred_prefix="NOPE"), USER))) == 400)
check("origin_type inválido 400",
      code(lambda: run(api.create_job(definition(slug="val-type", origin_type="rsync"), USER))) == 400)
check("cron inválido 400",
      code(lambda: run(api.create_job(definition(slug="val-cron", cron_hour="25;rm"), USER))) == 400)
check("key_name con metacaracteres de shell 400",
      code(lambda: run(api.create_job(definition(slug="val-key", origin_type="sftp", key_name='x"; id'), USER))) == 400)
check("nombre con salto de línea se neutraliza (no 400)",
      run(api.preview_job(definition(slug="val-nl", name="a\nid"), USER)).get("valid") is True)

# credenciales nuevas
check("cred_mode new sin host 400",
      code(lambda: run(api.create_job(definition(
          slug="val-host", cred_mode="new", cred_prefix="ACME", host=""), USER))) == 400)
check("cred_mode new sin pass en ftp 400",
      code(lambda: run(api.create_job(definition(
          slug="val-pass", cred_mode="new", cred_prefix="ACME", host="h", password=""), USER))) == 400)

EXECUTED.clear()
CREATED = run(api.create_job(definition(
    slug="nuevo-bd", cred_mode="new", cred_prefix="ACME", host="10.0.0.9",
    port="22", user="backup", password="p@ss"), USER))
check("crear con credenciales nuevas: ok", CREATED.get("ok") is True, CREATED)
creds = Path(os.environ["BACKUP_CREDENTIALS"]).read_text(encoding="utf-8")
check("credenciales nuevas escritas", "ACME_HOST='10.0.0.9'" in creds, creds)
check("credenciales nuevas con contraseña", "ACME_PASS='p@ss'" in creds or "ACME_PASS=p@ss" in creds, creds)

# ------------------------------------------------------------------ editar
EXECUTED.clear()
WRITES.clear()
updated = run(api.update_job_def("acme-bd", definition(name="Acme BD v2"), USER))
check("editar: ok", updated.get("ok") is True, updated)
check("editar: overlay actualizado", catalog.load_overlay("acme-bd")["name"] == "Acme BD v2")
check("editar: reescribe el cron", len(WRITES) == 1, WRITES)
check("editar: job no gestionado 409",
      code(lambda: run(api.update_job_def("base-bd", definition(slug="base-bd"), USER))) == 409)

# ------------------------------------------------------------------ eliminar
EXECUTED.clear()
WRITES.clear()
deleted = run(api.delete_job_def("acme-bd", USER))
check("eliminar: ok", deleted.get("ok") is True, deleted)
check("eliminar: script borrado", not (config.JOBS_DIR / "acme-bd.sh").exists())
check("eliminar: overlay borrado", not catalog.is_managed("acme-bd"))
check("eliminar: deshabilita en BD",
      any("UPDATE jobs SET enabled = %s" in sql for sql, _ in EXECUTED), EXECUTED)
check("eliminar: reescribe el cron", len(WRITES) == 1, WRITES)
check("eliminar: job no gestionado 409",
      code(lambda: run(api.delete_job_def("base-bd", USER))) == 409)

# ------------------------------------------------------------------ guard del flag
config.MANAGE_JOBS = False
check("flag off: crear 409", code(lambda: run(api.create_job(definition(slug="x-bd"), USER))) == 409)
check("flag off: borrar 409", code(lambda: run(api.delete_job_def("nuevo-bd", USER))) == 409)
config.MANAGE_JOBS = True

groups = run(api.credential_groups(USER))["groups"]
check("grupos de credenciales sin valores",
      any(group["prefix"] == "RUTA56" for group in groups) and "162.240.175.168" not in str(groups),
      groups)

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
