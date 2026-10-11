"""Canales de entrega: FCM (web push) y correo SMTP/sendmail."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import subprocess
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import catalog, config, db

log = logging.getLogger("notify")

try:  # import perezoso del SDK: el servicio arranca aunque no esté instalado
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:  # noqa: BLE001
    firebase_admin = None
    credentials = None
    messaging = None

_fb_app = None
_fb_tried = False

_INVALID_TOKEN_MARKERS = (
    "not-registered",
    "notregistered",
    "invalid-argument",
    "invalidregistration",
    "unregistered",
    "requested-entity-was-not-found",
)


def _fcm_app():
    global _fb_app, _fb_tried
    if _fb_app is not None:
        return _fb_app
    if _fb_tried:
        return None
    _fb_tried = True
    if not firebase_admin or not config.FIREBASE_SERVICE_ACCOUNT_PATH:
        log.warning("FCM no configurado (falta firebase-admin o FIREBASE_SERVICE_ACCOUNT_PATH)")
        return None
    if not os.path.exists(config.FIREBASE_SERVICE_ACCOUNT_PATH):
        log.warning("FIREBASE_SERVICE_ACCOUNT_PATH no existe: %s", config.FIREBASE_SERVICE_ACCOUNT_PATH)
        return None
    try:
        cred = credentials.Certificate(config.FIREBASE_SERVICE_ACCOUNT_PATH)
        _fb_app = firebase_admin.initialize_app(cred)
        log.info("FCM inicializado")
    except Exception:  # noqa: BLE001
        log.exception("no se pudo inicializar FCM")
        _fb_app = None
    return _fb_app


def fcm_ready() -> bool:
    return _fcm_app() is not None


def _is_invalid_token(error: str) -> bool:
    low = (error or "").lower()
    return any(marker in low for marker in _INVALID_TOKEN_MARKERS)


def send_fcm(token: str, title: str, body: str, url: str, level: str) -> tuple[str, str]:
    """Devuelve (status, error). status ∈ {sent, invalid, skipped, error}."""
    app = _fcm_app()
    if app is None:
        return "skipped", "FCM no configurado"
    webpush = messaging.WebpushConfig(
        notification=messaging.WebpushNotification(title=title, body=body, icon="/favicon.png"),
        fcm_options=messaging.WebpushFCMOptions(link=url) if url else None,
    )
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(title=title, body=body),
        data={"url": url or "", "level": level or ""},
        webpush=webpush,
    )
    try:
        messaging.send(message, app=app)
        return "sent", ""
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if _is_invalid_token(text):
            return "invalid", text
        log.warning("fallo FCM: %s", text)
        return "error", text


def mail_ready() -> bool:
    return bool(config.MAIL_TO)


def send_email(subject: str, body: str) -> tuple[str, str]:
    if not config.MAIL_TO:
        return "skipped", "correo no configurado"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.MAIL_FROM
    message["To"] = ", ".join(config.MAIL_TO)
    message.set_content(body)
    try:
        if config.SMTP_HOST:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
                if config.SMTP_TLS:
                    smtp.starttls(context=ssl.create_default_context())
                if config.SMTP_USER and config.SMTP_TLS:
                    smtp.login(config.SMTP_USER, config.SMTP_PASS)
                smtp.send_message(message)
        else:
            subprocess.run(["/usr/sbin/sendmail", "-t"], input=message.as_bytes(),
                           check=True, timeout=30)
    except (OSError, smtplib.SMTPException, subprocess.SubprocessError) as exc:
        log.warning("no se pudo enviar el correo de notificación: %s", exc)
        return "error", str(exc)
    return "sent", ""


# --- Preferencias / horario silencioso -------------------------------------

def _in_quiet(quiet: dict) -> bool:
    if not quiet or not quiet.get("enabled"):
        return False
    try:
        now = datetime.now(ZoneInfo(config.TZ)).strftime("%H:%M")
        start = str(quiet.get("start") or config.QUIET_START)
        end = str(quiet.get("end") or config.QUIET_END)
    except Exception:  # noqa: BLE001
        return False
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # ventana que cruza medianoche


def _allows(prefs: dict, event_key: str, level: str) -> bool:
    if not prefs.get("enabled", True):
        return False
    if prefs.get("events", {}).get(event_key) is False:
        return False
    return catalog.meets_min(level, prefs.get("min_level", "info"))


def dispatch(event: dict) -> dict:
    """Reparte un evento (ya insertado) por FCM y correo según preferencias."""
    area = event["area"]
    event_key = event["event_key"]
    level = event["level"]
    title = event["title"]
    body = event["body"]
    url = event.get("url") or ""
    event_id = event["id"]
    scope = event.get("scope") or ""
    stats = {"fcm_sent": 0, "fcm_skipped": 0, "email": "skipped"}

    scope_cache: dict[str, dict] = {}
    for sub in db.subscriptions_for_area(area):
        prefs = db.get_prefs(sub["username"], area)
        if not _allows(prefs, event_key, level):
            stats["fcm_skipped"] += 1
            continue
        if scope:
            allowed = scope_cache.get(sub["username"])
            if allowed is None:
                allowed = db.get_scope_prefs(sub["username"], area)
                scope_cache[sub["username"]] = allowed
            if allowed.get(scope, True) is False:
                stats["fcm_skipped"] += 1
                continue
        if "fcm" not in (prefs.get("channels") or ["fcm"]):
            stats["fcm_skipped"] += 1
            continue
        if _in_quiet(prefs.get("quiet") or {}):
            stats["fcm_skipped"] += 1
            continue
        status, error = send_fcm(sub["token"], title, body, url, level)
        db.record_delivery(event_id, sub["id"], "fcm", sub["token"][:24], status, error)
        if status == "invalid":
            db.delete_token(sub["token"])
        elif status == "sent":
            stats["fcm_sent"] += 1

    if mail_ready() and catalog.meets_min(level, "warning"):
        subject = f"[{area}] {title}"
        text = body + (f"\n\n{url}" if url else "")
        status, error = send_email(subject, text)
        db.record_delivery(event_id, "", "email", ", ".join(config.MAIL_TO), status, error)
        stats["email"] = status

    return stats


def dispatch_test(username: str, area: str) -> dict:
    """Envía una notificación de prueba a las suscripciones del usuario en el área."""
    sent = 0
    for sub in db.user_subscriptions(username):
        if sub["area"] != area:
            continue
        status, error = send_fcm(
            sub["token"], "Notificación de prueba",
            f"Prueba del área «{area}».", "", "info",
        )
        if status == "invalid":
            db.delete_token(sub["token"])
        elif status == "sent":
            sent += 1
    return {"sent": sent, "fcm_ready": fcm_ready()}
