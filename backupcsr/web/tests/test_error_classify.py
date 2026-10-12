"""Regresión de la clasificación de errores y estados de `app.logs`.

Cubre el contrato de líneas del job (lib/common.sh): cierre parcial, ronda OMITIDA,
fallo transitorio con debounce y fallo fatal. No toca NAS, MySQL, /etc ni /run/lock:

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_error_classify.py
"""
import os
import sys
import tempfile
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

LOG_DIR = Path(tempfile.mkdtemp(prefix="backupcsr-test-logs-"))
os.environ["BACKUP_LOG"] = str(LOG_DIR)
os.environ["BACKUP_TZ"] = "America/Bogota"

from app import logs  # noqa: E402

checks = 0
failures = 0


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


SAMPLE = """\
2026-09-26 10:00:00 [demo] === inicio demo (dry_run=0 nice=15 trigger=manual) ===
2026-09-26 10:00:01 [demo] Transferring file `a.txt'
2026-09-26 10:00:02 [demo] AVISO: 3 archivos con error
2026-09-26 10:00:03 [demo] === fin demo ===
2026-09-26 11:00:00 [demo] === inicio demo (dry_run=0 nice=15 trigger=cron) ===
2026-09-26 11:00:01 [demo] AVISO: cola ocupada: sin turno tras 900s esperando /run/lock/backupcsr-gate.lock
2026-09-26 11:00:01 [demo] OMITIDO: otro job tiene la cola; se reintenta en el próximo ciclo
2026-09-26 11:00:02 [demo] PROCESO: exit=75
2026-09-26 12:00:00 [demo] === inicio demo (dry_run=0 nice=15 trigger=cron) ===
2026-09-26 12:00:01 [demo] mirror: Fatal error: max-retries exceeded
2026-09-26 12:00:01 [demo] AVISO: reintento 2/3 por error transitorio (espera 30s)
2026-09-26 12:00:05 [demo] ERROR: lftp agotó 3 intento(s) por error transitorio [transitorio]
2026-09-26 12:00:06 [demo] PROCESO: exit=1
2026-09-26 13:00:00 [demo] === inicio demo (dry_run=0 nice=15 trigger=cron) ===
2026-09-26 13:00:01 [demo] ERROR: el NAS no está montado en /mnt/nas
2026-09-26 13:00:01 [demo] PROCESO: exit=1
"""
(LOG_DIR / "demo.log").write_text(SAMPLE, encoding="utf-8")

runs = logs.parse_slug("demo")
check("se detectan 4 corridas", len(runs) == 4, len(runs))

partial, omit, transient, fatal = runs

check("parcial: estado PARCIAL", partial["status"] == "PARCIAL", partial["status"])
check("parcial: clase parcial", partial["error_class"] == "parcial", partial["error_class"])
check("parcial: cuenta archivos", partial["partial_files"] == 3, partial["partial_files"])
check("parcial: trigger manual", partial["triggered_by"] == "manual", partial["triggered_by"])
check("parcial: cierra", partial["_finished"] is True)

check("omitido: estado OMITIDO", omit["status"] == "OMITIDO", omit["status"])
check("omitido: clase contencion", omit["error_class"] == "contencion", omit["error_class"])
check("omitido: exit 75", omit["exit_code"] == 75, omit["exit_code"])
check("omitido: NO es fallo", logs.evaluate(omit, {"minute": "0", "hour": "*"}, logs.now(), False)[0] == "OMITIDO")

check("transitorio: estado FALLO", transient["status"] == "FALLO", transient["status"])
check("transitorio: clase transitorio", transient["error_class"] == "transitorio", transient["error_class"])
check("transitorio: exit 1", transient["exit_code"] == 1, transient["exit_code"])

check("fatal: estado FALLO", fatal["status"] == "FALLO", fatal["status"])
check("fatal: clase fatal", fatal["error_class"] == "fatal", fatal["error_class"])
check("fatal: conserva líneas", len(fatal["error_lines"]) >= 1, fatal["error_lines"])

# effective_run ignora la OMITIDO; latest_omit la devuelve si es la última.
check("effective_run salta OMITIDO", logs.effective_run(runs[:2]) is partial)
check("latest_omit detecta la última", logs.latest_omit(runs[:2]) is omit)
check("latest_omit vacío si la última no es OMITIDO", logs.latest_omit([partial]) is None)

# consecutive_omits: rondas OMITIDO seguidas al final (umbral del banner).
check("consecutive_omits cuenta la última", logs.consecutive_omits(runs[:2]) == 1,
      logs.consecutive_omits(runs[:2]))
check("consecutive_omits cero sin OMITIDO al final",
      logs.consecutive_omits(runs[:1]) == 0)
check("consecutive_omits acumula seguidas",
      logs.consecutive_omits([omit, omit, omit]) == 3)
check("consecutive_omits se corta al ver una real",
      logs.consecutive_omits([partial, omit, omit]) == 2)

# classify_error por marcador y heurística de red.
check("classify [transitorio]", logs.classify_error("ERROR: x [transitorio]") == "transitorio")
check("classify timeout", logs.classify_error("Connection timed out") == "transitorio")
check("classify auth", logs.classify_error("Permission denied") == "fatal")

print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
