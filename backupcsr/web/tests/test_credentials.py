"""Pruebas de la gestión de credenciales (app/credentials.py).

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_credentials.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="creds-"))
os.environ["BACKUP_ETC"] = str(TMP / "etc")
os.environ["BACKUP_CREDENTIALS"] = str(TMP / "etc" / "credentials.env")
Path(os.environ["BACKUP_ETC"]).mkdir(parents=True, exist_ok=True)

from app import config, credentials  # noqa: E402

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


config.CREDENTIALS_FILE.write_text(
    "# credenciales\nRUTA56_HOST=162.240.175.168\nRUTA56_USER=wwruta\n"
    "RUTA56_PASS='secreto con espacio'\n",
    encoding="utf-8",
)

groups = {group["prefix"]: group for group in credentials.list_groups()}
check("list_groups ve el prefijo", "RUTA56" in groups, groups)
check("list_groups detecta contraseña", groups["RUTA56"]["has_pass"] is True, groups)
check("list_groups no devuelve valores",
      all(isinstance(value, (str, bool)) for value in groups["RUTA56"].values())
      and "162.240.175.168" not in str(groups), groups)
check("prefix_exists RUTA56", credentials.prefix_exists("RUTA56"))
check("prefix_exists desconocido", not credentials.prefix_exists("ACME"))

check("prefijo válido", credentials.valid_prefix("ACME"))
check("prefijo con minúscula no", not credentials.valid_prefix("acme"))
check("prefijo empezando por número no", not credentials.valid_prefix("1ACME"))

credentials.save_group("ACME", "10.0.0.5", "22", "backup", "p@ss#1")
text = config.CREDENTIALS_FILE.read_text(encoding="utf-8")
check("save_group escribe el host", "ACME_HOST='10.0.0.5'" in text, text)
check("save_group escribe el puerto", "ACME_PORT='22'" in text)
check("save_group escribe el usuario", "ACME_USER='backup'" in text)
check("save_group cita la contraseña con caracteres especiales",
      "ACME_PASS='p@ss#1'" in text, text)
check("save_group conserva lo anterior", "RUTA56_HOST=162.240.175.168" in text)

credentials.save_group("ACME", "10.0.0.6", "22", "backup", "otra")
text = config.CREDENTIALS_FILE.read_text(encoding="utf-8")
check("save_group es idempotente (una sola línea)", text.count("ACME_HOST=") == 1, text)
check("save_group actualiza el valor", "ACME_HOST='10.0.0.6'" in text)
check("save_group conserva el comentario", text.startswith("# credenciales"))

mode = os.stat(config.CREDENTIALS_FILE).st_mode & 0o777
check("save_group deja 0600", mode == 0o600, oct(mode))

# Un valor con metacaracteres de shell debe quedar literal (el archivo se 'sourcea').
credentials.save_group("EVIL", "h; rm -rf /", None, "u$(id)", "p`id`")
text = config.CREDENTIALS_FILE.read_text(encoding="utf-8")
check("metacaracteres de shell quedan citados",
      "EVIL_HOST='h; rm -rf /'" in text and "EVIL_USER='u$(id)'" in text and "EVIL_PASS='p`id`'" in text,
      text)


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


check("prefijo inválido lanza",
      _raises(lambda: credentials.save_group("mal", "h", "1", "u", "p")))


shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
