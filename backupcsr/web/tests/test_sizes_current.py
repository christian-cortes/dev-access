"""Pruebas del tamaño cacheado del panel (app/sizes.py `current`).

`current` alimenta /api/summary y /api/jobs, así que no debe esperar a un escaneo
del NAS en curso (que `job_size`/`_warm_worker` hacen en segundo plano tomando el
lock del slug): si esperara, el portal se quedaba colgado durante los escaneos.
Aquí se comprueba que responde de inmediato aunque el lock esté tomado.

Corre con el python del venv del portal y usa un directorio temporal: **no** toca
/mnt/nas, /etc, /run/lock ni MySQL.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_sizes_current.py
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

TMP = Path(tempfile.mkdtemp(prefix="sizes-current-"))
os.environ["BACKUP_NAS"] = str(TMP / "nas")
os.environ["BACKUP_ETC"] = str(TMP / "etc")

from app import sizes  # noqa: E402

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
        print(f"FAIL {name} — {extra}")
    else:
        print(f"PASS {name}" + (f" — {extra}" if extra else ""))


JOB = {"slug": "demo", "enabled": True, "size_exclude": []}

# Con el lock libre y sin caché responde `pending` (aún no medido).
result = sizes.current(JOB)
check("sin caché: pending, sin bytes", result.get("pending") is True and result.get("bytes") is None,
      result)

# Con una medición en curso (lock tomado por el escaneo) `current` no debe bloquear:
# devuelve lo cacheado/pending en vez de esperar al NAS.
lock = sizes._lock_for("demo")
lock.acquire()
try:
    box = {}

    def _call():
        box["result"] = sizes.current(JOB)

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    check("lock tomado: current no se bloquea", not worker.is_alive(),
          "current quedó esperando a un escaneo en curso")
    check("lock tomado: responde pending", box.get("result", {}).get("pending") is True,
          box.get("result"))
finally:
    lock.release()

# Con caché fresca devuelve el valor medido.
sizes._cache["demo"] = {
    "bytes": 123, "files": 4, "truncated": False, "error": None, "_ts": time.time(),
}
result = sizes.current(JOB)
check("con caché: devuelve bytes", result.get("bytes") == 123 and result.get("cached") is True,
      result)

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
