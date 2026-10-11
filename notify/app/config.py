"""Configuración del servicio de notificaciones (env + rutas)."""
from __future__ import annotations

import os
from pathlib import Path


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() == "1"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


# --- Rutas / base de datos ---
DB_PATH = os.environ.get("NOTIFY_DB_PATH", "/data/notify.db")

# --- SSO (cookie compartida) ---
# El secreto DEBE ser el mismo que BACKUP_SECRET_KEY de copias; el salt y el
# nombre de cookie deben coincidir con backupcsr/web/app/auth.py.
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "").strip()
AUTH_COOKIE = os.environ.get("AUTH_COOKIE", "backupcsr_session").strip() or "backupcsr_session"
AUTH_SALT = os.environ.get("AUTH_SALT", "backupcsr-session").strip() or "backupcsr-session"
AUTH_SESSION_MAX_AGE = _int("AUTH_SESSION_MAX_AGE", 12 * 3600)
AUTH_COOKIE_DOMAIN = os.environ.get("AUTH_COOKIE_DOMAIN", ".cortexdev.win").strip()
LOGIN_URL = os.environ.get("NOTIFY_LOGIN_URL", "https://index.cortexdev.win/login.html").strip()
# Cookie temporal donde nginx guarda la página de origen al mandar al login.
# El login la lee (backend) y responde a dónde volver; así la URL va limpia.
AUTH_NEXT_COOKIE = os.environ.get("AUTH_NEXT_COOKIE", "cortex_sso_next").strip() or "cortex_sso_next"

# --- Credenciales generales del login central (index.cortexdev.win) ---
# Son las credenciales ÚNICAS de acceso a todos los entornos (index, copias,
# ups...). Sustituyen al login propio de copias. Con AUTH_GENERAL_PASSWORD
# vacío el login queda deshabilitado (solo se puede validar la cookie ya emitida).
AUTH_GENERAL_USER = os.environ.get("AUTH_GENERAL_USER", "").strip()
AUTH_GENERAL_PASSWORD = os.environ.get("AUTH_GENERAL_PASSWORD", "")
AUTH_GENERAL_ROLE = os.environ.get("AUTH_GENERAL_ROLE", "admin").strip() or "admin"

# --- Ingesta interna de eventos ---
INGEST_TOKEN = os.environ.get("NOTIFY_INGEST_TOKEN", "").strip()

# --- Firebase (FCM web) ---
FIREBASE_SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
# Config pública que se entrega al navegador (no secreta).
FIREBASE_PUBLIC = {
    key: os.environ.get(env, "").strip()
    for key, env in (
        ("apiKey", "FIREBASE_API_KEY"),
        ("authDomain", "FIREBASE_AUTH_DOMAIN"),
        ("projectId", "FIREBASE_PROJECT_ID"),
        ("storageBucket", "FIREBASE_STORAGE_BUCKET"),
        ("messagingSenderId", "FIREBASE_MESSAGING_SENDER_ID"),
        ("appId", "FIREBASE_APP_ID"),
        ("measurementId", "FIREBASE_MEASUREMENT_ID"),
    )
    if os.environ.get(env, "").strip()
}
FIREBASE_VAPID_KEY = os.environ.get("FIREBASE_VAPID_KEY", "").strip()

# --- Correo (reutiliza la convención de copias) ---
MAIL_TO = _csv("NOTIFY_MAIL_TO") or _csv("BACKUP_ALERT_MAIL_TO")
MAIL_FROM = (os.environ.get("NOTIFY_MAIL_FROM") or os.environ.get("BACKUP_ALERT_MAIL_FROM")
             or "notify@localhost").strip()
SMTP_HOST = (os.environ.get("NOTIFY_SMTP_HOST") or os.environ.get("BACKUP_ALERT_SMTP_HOST") or "").strip()
SMTP_PORT = _int("NOTIFY_SMTP_PORT", _int("BACKUP_ALERT_SMTP_PORT", 25))
SMTP_USER = (os.environ.get("NOTIFY_SMTP_USER") or os.environ.get("BACKUP_ALERT_SMTP_USER") or "").strip()
SMTP_PASS = os.environ.get("NOTIFY_SMTP_PASS") or os.environ.get("BACKUP_ALERT_SMTP_PASS") or ""
SMTP_TLS = _flag("NOTIFY_SMTP_TLS", os.environ.get("BACKUP_ALERT_SMTP_TLS", "0"))

# --- Políticas de despacho ---
DEDUPE_WINDOW = _int("NOTIFY_DEDUPE_WINDOW", 900)   # segundos
EVENT_RETENTION_DAYS = _int("NOTIFY_EVENT_RETENTION_DAYS", 90)
DEFAULT_QUIET = _flag("NOTIFY_DEFAULT_QUIET", "0")  # horario silencioso por defecto
QUIET_START = os.environ.get("NOTIFY_QUIET_START", "22:00")
QUIET_END = os.environ.get("NOTIFY_QUIET_END", "07:00")
TZ = os.environ.get("NOTIFY_TZ", "America/Bogota")

LOG_DIR = Path(os.environ.get("NOTIFY_LOG_DIR", "/data"))
