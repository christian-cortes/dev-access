"""Configuración del portal de copias (env + rutas).

Todas las variables se leen de /etc/backupcsr/web.env (lo carga systemd vía
EnvironmentFile) o del entorno del proceso.
"""
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("backupcsr-web")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() == "1"


# --- Runtime de backupcsr (mismas rutas que install.sh del subsistema) ---
ETC_DIR = Path(os.environ.get("BACKUP_ETC", "/etc/backupcsr"))
OPT_DIR = Path(os.environ.get("BACKUP_OPT", "/opt/backupcsr"))
LOG_DIR = Path(os.environ.get("BACKUP_LOG", "/var/log/backupcsr"))
NAS_MOUNT = Path(os.environ.get("BACKUP_NAS", "/mnt/nas"))
CRON_FILE = Path(os.environ.get("BACKUP_CRON", "/etc/cron.d/backupcsr"))
JOBS_DIR = OPT_DIR / "jobs"

# --- Directorio del proyecto (app/..) ---
WEB_DIR = Path(os.environ.get("BACKUP_WEB_DIR", str(Path(__file__).resolve().parent.parent)))
CATALOG_PATH = Path(os.environ.get("BACKUP_CATALOG", str(WEB_DIR / "jobs.yml")))
SCHEMA_PATH = Path(os.environ.get("BACKUP_SCHEMA", str(WEB_DIR / "schema.sql")))

# --- Base de datos MySQL ---
DB_HOST = os.environ.get("BACKUP_DB_HOST", "127.0.0.1")
DB_PORT = _int("BACKUP_DB_PORT", 3306)
DB_NAME = os.environ.get("BACKUP_DB_NAME", "backupcsr")
DB_USER = os.environ.get("BACKUP_DB_USER", "backupcsr_web")
DB_PASSWORD = os.environ.get("BACKUP_DB_PASSWORD", "")
DB_CONNECT_TIMEOUT = _int("BACKUP_DB_CONNECT_TIMEOUT", 5)

# --- Sesión ---
SECRET_KEY = os.environ.get("BACKUP_SECRET_KEY", "").strip()
_SECRET_GENERATED = False
if not SECRET_KEY:
    # Sin clave persistente las sesiones no sobreviven reinicios; se avisa fuerte.
    SECRET_KEY = secrets.token_hex(32)
    _SECRET_GENERATED = True
SESSION_COOKIE = "backupcsr_session"
SESSION_MAX_AGE = _int("BACKUP_SESSION_MAX_AGE", 12 * 3600)
# Cookie Secure: 1 por defecto (todo va por https vía nginx). Poner 0 solo para
# pruebas directas contra http://127.0.0.1:8089.
COOKIE_SECURE = _flag("BACKUP_COOKIE_SECURE", "1")
# Dominio de la cookie de sesión. Con .cortexdev.win la sesión de copias vale
# para index.cortexdev.win y ups.cortexdev.win (SSO). Vacío = solo el host actual.
COOKIE_DOMAIN = os.environ.get("BACKUP_COOKIE_DOMAIN", ".cortexdev.win").strip()

# --- Notificaciones push (servicio notify) ---
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").rstrip("/")
NOTIFY_INGEST_TOKEN = os.environ.get("NOTIFY_INGEST_TOKEN", "").strip()

# --- Comportamiento ---
MANAGE_CRON = _flag("BACKUP_MANAGE_CRON")
TIMEZONE = os.environ.get("BACKUP_TZ", "America/Bogota")
POLL_INTERVAL = _int("BACKUP_POLL_INTERVAL", 30)
GRACE_MIN = _int("BACKUP_GRACE_MIN", 90)
MAX_RUN_FILES = _int("BACKUP_MAX_RUN_FILES", 5000)

# --- Planificador de copias (backupcsr-scheduler) -------------------------------
# El daemon es el dueño de la concurrencia: cupos global y por host, cola persistente
# y reintentos de fallos transitorios. Con BACKUP_SCHEDULER=0 todo sigue como antes
# (cron directo + MIRROR_GATE serial) y el portal lanza los jobs directo.
SCHEDULER = _flag("BACKUP_SCHEDULER")
SCHEDULER_BIN = Path(os.environ.get("BACKUP_SCHEDULER_BIN", str(OPT_DIR / "bin" / "backupcsr-scheduler")))
SCHEDULER_SOCKET = Path(
    os.environ.get("BACKUP_SCHEDULER_SOCKET", "/run/backupcsr/scheduler.sock")
)
SCHEDULER_STATE = Path(
    os.environ.get("BACKUP_SCHEDULER_STATE", "/var/lib/backupcsr/scheduler.json")
)
SCHEDULER_MAX_JOBS = _int("BACKUP_MAX_JOBS", 2)
SCHEDULER_MAX_PER_HOST = _int("BACKUP_MAX_PER_HOST", 1)
SCHEDULER_RETRY_MAX = _int("BACKUP_RETRY_MAX", 3)
SCHEDULER_RETRY_BACKOFF = os.environ.get("BACKUP_RETRY_BACKOFF", "300,900,2700")
SCHEDULER_RETRY_JITTER = os.environ.get("BACKUP_RETRY_JITTER", "0.2")
SCHEDULER_POLL = _int("BACKUP_SCHEDULER_POLL", 2)

# --- Tamaños (GB por tarea) ---
SIZE_TTL = _int("BACKUP_SIZE_TTL", 1800)
SIZE_MAX_ENTRIES = _int("BACKUP_SIZE_MAX_ENTRIES", 500000)
SIZE_TIMEOUT = _int("BACKUP_SIZE_TIMEOUT", 120)
# Pausa entre jobs del refresco en segundo plano: recorrer el NAS (CIFS) de los seis
# seguidos es carga extra para un anfitrión con poca RAM.
SIZE_WARM_PAUSE = _int("BACKUP_SIZE_WARM_PAUSE", 2)
NAS_MIN_FREE_PCT = _int("BACKUP_NAS_MIN_FREE_PCT", 10)
SIZE_SNAPSHOT = _flag("BACKUP_SIZE_SNAPSHOT", "1")

# --- Gestión de archivos (pestaña Archivos: borrar, renombrar, mover, subir) ---
# Solo para administradores; se puede desactivar con BACKUP_MANAGE_FILES=0.
MANAGE_FILES = _flag("BACKUP_MANAGE_FILES", "1")
FILES_MAX_UPLOAD_MB = _int("BACKUP_FILES_MAX_UPLOAD_MB", 512)
# A partir de este tamaño el portal pide escribir el nombre para confirmar el borrado.
FILES_CONFIRM_MB = _int("BACKUP_FILES_CONFIRM_MB", 100)

# --- Alertas por correo (opcionales) ---
# Sin BACKUP_ALERT_MAIL_TO el aviso externo queda desactivado (el portal sigue
# mostrando la campana con las alertas). fatal -> correo inmediato; el resto va en
# el resumen diario (app.cli notify-digest). Con SMTP vacío se usa /usr/sbin/sendmail.
ALERT_MAIL_TO = os.environ.get("BACKUP_ALERT_MAIL_TO", "").strip()
ALERT_MAIL_FROM = os.environ.get("BACKUP_ALERT_MAIL_FROM", "backupcsr@localhost").strip()
ALERT_SMTP_HOST = os.environ.get("BACKUP_ALERT_SMTP_HOST", "").strip()
ALERT_SMTP_PORT = _int("BACKUP_ALERT_SMTP_PORT", 25)
ALERT_SMTP_USER = os.environ.get("BACKUP_ALERT_SMTP_USER", "").strip()
ALERT_SMTP_PASS = os.environ.get("BACKUP_ALERT_SMTP_PASS", "")
ALERT_SMTP_TLS = _flag("BACKUP_ALERT_SMTP_TLS", "0")

# --- Admin inicial ---
ADMIN_USER = os.environ.get("BACKUP_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("BACKUP_ADMIN_PASSWORD", "")


def warn_if_insecure() -> None:
    if _SECRET_GENERATED:
        log.warning(
            "BACKUP_SECRET_KEY no está definida: se generó una efímera. "
            "Las sesiones se invalidarán al reiniciar. Definirla en /etc/backupcsr/web.env."
        )
