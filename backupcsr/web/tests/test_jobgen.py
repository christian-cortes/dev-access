"""Pruebas del generador de scripts de tareas (app/jobgen.py).

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_jobgen.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="jobgen-"))
os.environ["BACKUP_OPT"] = str(TMP / "opt")
os.environ["BACKUP_ETC"] = str(TMP / "etc")

from app import config, jobgen  # noqa: E402

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


FTP = {
    "slug": "acme-bd", "name": "Acme BD", "description": "dumps",
    "origin_type": "ftp", "cred_prefix": "ACME",
    "routes": [{"source": "taskManager/acme", "dest_rel": "acme/dumps"}],
    "exclusions": ["storage", "tmp/"],
}
text = jobgen.render_script(FTP)
check("ftp: job_init con el slug", 'job_init \'acme-bd\'' in text, text)
check("ftp: carga credenciales", "load_credentials" in text)
check("ftp: usa mirror_ftp con el prefijo", "mirror_ftp \"$ACME_HOST\" \"$ACME_USER\" \"$ACME_PASS\"" in text, text)
check("ftp: destino bajo el NAS", '"$BACKUPCSR_NAS_ROOT/acme/dumps"' in text)
check("ftp: exclusiones como argumentos citados",
      "'storage'" in text and "'tmp/'" in text, text)
check("ftp: cierre con log", 'log "=== fin acme-bd ==="' in text)
ok, err = jobgen.validate_script(text)
check("ftp: script válido (bash -n)", ok, err)

SFTP = {**FTP, "origin_type": "sftp", "key_name": "acme"}
text = jobgen.render_script(SFTP)
check("sftp: llave con el nombre indicado", '"$BACKUPCSR_KEYS/acme"' in text, text)
check("sftp: usa mirror_sftp", "mirror_sftp \"$ACME_HOST\"" in text, text)
check("sftp: puerto por defecto 22", '"${ACME_PORT:-22}"' in text, text)

SFTP_PASS = {**FTP, "origin_type": "sftp_pass"}
text = jobgen.render_script(SFTP_PASS)
check("sftp_pass: usa la contraseña", '"$ACME_PASS"' in text and "mirror_sftp_pass" in text, text)

# Multi-ruta y GATE_WAIT.
MULTI = {
    **FTP,
    "routes": [
        {"source": "/srv/a", "dest_rel": "acme/a"},
        {"source": "/srv/b", "dest_rel": "acme/b"},
    ],
    "exclusions": [],
    "gate_wait": 1800,
}
text = jobgen.render_script(MULTI)
check("multi-ruta: dos llamadas mirror", text.count("mirror_ftp") == 2, text)
check("multi-ruta: ambas rutas destino",
      '"$BACKUPCSR_NAS_ROOT/acme/a"' in text and '"$BACKUPCSR_NAS_ROOT/acme/b"' in text)
check("gate_wait escrito", 'GATE_WAIT="${GATE_WAIT:-1800}"' in text, text)

# ------------------------------------------------------------- válido/inválido
ok, _ = jobgen.validate_script("#!/bin/bash\necho ok\n")
check("validate: script válido", ok)
ok, _ = jobgen.validate_script("#!/bin/bash\nif [ 1 ; then\n")
check("validate: script inválido detectado", not ok)

# El nombre/descripción no deben poder romper el comentario '#': saltos de línea
# y control se colapsan a espacios (evita inyección de shell en el script root).
INJ = {**FTP, "name": "a\nid", "description": "x\necho pwn"}
text = jobgen.render_script(INJ)
exec_lines = [line.strip() for line in text.splitlines()]
check("nombre con salto de línea neutralizado", "id" not in exec_lines, text)
check("descripción con salto de línea neutralizada", "echo pwn" not in exec_lines, text)

# ------------------------------------------------------------------ parse_routes
SAMPLE = """#!/usr/bin/env bash
set -euo pipefail
. "${BACKUPCSR_LIB:-/opt/backupcsr/lib/common.sh}"
job_init "sample"
load_credentials
mirror_ftp "$H" "$U" "$P" \\
\t"taskManager/ruta56" "$BACKUPCSR_NAS_ROOT/ruta56" "storage"
mirror_sftp "$HOST" "${PORT:-22}" "$USER" "$BACKUPCSR_KEYS/google" \\
\t"/var/www/html/backups" "$BACKUPCSR_NAS_ROOT/google/backups"
log "=== fin sample ==="
"""
routes = jobgen.parse_routes(SAMPLE)
check("parse_routes: dos rutas", len(routes) == 2, routes)
check("parse_routes: primer origen", routes and routes[0]["source"] == "taskManager/ruta56", routes)
check("parse_routes: primer destino", routes and routes[0]["dest_rel"] == "ruta56", routes)
check("parse_routes: segundo destino", len(routes) > 1 and routes[1]["dest_rel"] == "google/backups", routes)

# ------------------------------------------------------------------ instalación
config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
path = jobgen.install_script("acme-bd", "#!/bin/bash\necho hola\n")
check("install_script escribe el archivo", Path(path).is_file())
check("install_script deja 0755", (os.stat(path).st_mode & 0o777) == 0o755)
check("remove_script borra", jobgen.remove_script("acme-bd") and not Path(path).exists())

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
