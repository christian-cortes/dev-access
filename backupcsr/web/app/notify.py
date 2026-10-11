"""Aviso por correo de incidentes de copias (opcional).

Sin `BACKUP_ALERT_MAIL_TO` todo es no-op: el portal sigue mostrando la campana.
- `flush_immediate()`: correo al abrir un incidente `danger` (fatal).
- `flush_recovery()`: correo al resolverse un incidente.
- `send_digest()`: resumen de las últimas 24 h (fatal/transitorio/parcial/omitido),
  como máximo una vez al día salvo `force=True`.

Los destinatarios y el transporte salen de /etc/backupcsr/web.env (ver config.py).
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import subprocess
import time
import urllib.request
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

from . import config, db, logs

log = logging.getLogger("backupcsr-web")

_DIGEST_STATE = Path("/run/backupcsr-digest.date")
# Tras un fallo de envío no se reintenta en cada sondeo (30 s): se espera este margen.
_RETRY_AFTER = 300.0
_next_attempt = 0.0


def _backing_off() -> bool:
    return time.monotonic() < _next_attempt


def _send_failed() -> None:
    global _next_attempt
    _next_attempt = time.monotonic() + _RETRY_AFTER


def _send_ok() -> None:
    global _next_attempt
    _next_attempt = 0.0


def enabled() -> bool:
    return bool(config.ALERT_MAIL_TO)


def _send(subject: str, body: str) -> bool:
    """Envía un correo por SMTP configurado o por el sendmail del sistema."""
    if not enabled() or _backing_off():
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.ALERT_MAIL_FROM
    message["To"] = config.ALERT_MAIL_TO
    message.set_content(body)
    try:
        if config.ALERT_SMTP_HOST:
            # STARTTLS con verificación real del certificado: el contexto por defecto
            # de smtplib.starttls() no valida (CERT_NONE), lo que expondría las
            # credenciales ante un MITM.
            with smtplib.SMTP(config.ALERT_SMTP_HOST, config.ALERT_SMTP_PORT, timeout=20) as smtp:
                if config.ALERT_SMTP_TLS:
                    smtp.starttls(context=ssl.create_default_context())
                if config.ALERT_SMTP_USER:
                    if config.ALERT_SMTP_TLS:
                        smtp.login(config.ALERT_SMTP_USER, config.ALERT_SMTP_PASS)
                    else:
                        log.warning(
                            "ALERT_SMTP_USER definido sin ALERT_SMTP_TLS: "
                            "no se envían credenciales en claro"
                        )
                smtp.send_message(message)
        else:
            subprocess.run(
                ["/usr/sbin/sendmail", "-t"],
                input=message.as_bytes(),
                check=True,
                timeout=30,
            )
    except (OSError, smtplib.SMTPException, subprocess.SubprocessError) as exc:
        log.warning("no se pudo enviar el correo de alertas: %s", exc)
        _send_failed()
        return False
    _send_ok()
    return True


def _stamp(value) -> str:
    if not value:
        return "—"
    return logs.from_naive(value).strftime("%Y-%m-%d %H:%M:%S")


def _incident_body(row: dict, heading: str) -> str:
    lines = [
        f"{heading}",
        "",
        f"Tarea:      {row.get('job_name') or ''} ({row.get('job_slug') or ''})",
        f"Clase:      {row.get('error_class') or '—'}",
        f"Severidad:  {'Crítico' if row.get('level') == 'danger' else 'Aviso'}",
        f"Veces:      {int(row.get('count') or 0)} (consecutivas: {int(row.get('consecutive') or 0)})",
        f"Primera:    {_stamp(row.get('first_seen'))}",
        f"Última:     {_stamp(row.get('last_seen'))}",
        "",
        str(row.get("text") or ""),
    ]
    extra = logs.parse_error_lines(row.get("error_lines"))
    if extra:
        lines.append("")
        lines.append("Líneas del log:")
        lines += [f"  {line}" for line in extra]
    lines += ["", "Portal: https://copias.cortexdev.win"]
    return "\n".join(lines)


def _open_danger() -> list[dict]:
    return db.query(
        "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
        "JOIN jobs j ON j.id = i.job_id "
        "WHERE i.resolved_at IS NULL AND i.`level` = 'danger' AND i.notified_at IS NULL "
        "ORDER BY i.last_seen"
    )


def _unnotified_resolved() -> list[dict]:
    return db.query(
        "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
        "JOIN jobs j ON j.id = i.job_id "
        "WHERE i.resolved_at IS NOT NULL AND i.resolved_notified_at IS NULL "
        "ORDER BY i.resolved_at"
    )


def flush_immediate() -> int:
    """Envía los incidentes abiertos de severidad Crítico aún no notificados."""
    if not enabled() or not db.available():
        return 0
    try:
        rows = _open_danger()
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer incidentes para el aviso inmediato")
        return 0
    sent = 0
    for row in rows:
        name = row.get("job_name") or row.get("job_slug") or "tarea"
        subject = f"[copias] FALLO {name}: {row.get('error_class') or 'fatal'}"
        if _send(subject, _incident_body(row, "Incidente abierto")):
            db.execute(
                "UPDATE incidents SET notified_at = %s WHERE id = %s",
                (logs.to_naive(logs.now()), row["id"]),
            )
            sent += 1
    return sent


def flush_recovery() -> int:
    """Envía los incidentes resueltos aún no notificados."""
    if not enabled() or not db.available():
        return 0
    try:
        rows = _unnotified_resolved()
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer incidentes para el aviso de recuperación")
        return 0
    sent = 0
    for row in rows:
        name = row.get("job_name") or row.get("job_slug") or "tarea"
        subject = f"[copias] RECUPERADO {name}"
        body = _incident_body(row, "Incidente resuelto")
        body += f"\n\nResuelto:   {_stamp(row.get('resolved_at'))}"
        if _send(subject, body):
            db.execute(
                "UPDATE incidents SET resolved_notified_at = %s WHERE id = %s",
                (logs.to_naive(logs.now()), row["id"]),
            )
            sent += 1
    return sent


# --- Push (servicio notify) -------------------------------------------------

_pushed_open: set = set()
_pushed_resolved: set = set()


def push_enabled() -> bool:
    return bool(config.NOTIFY_URL and config.NOTIFY_INGEST_TOKEN)


def _publish(event_key: str, title: str, body: str, dedupe_key: str, scope: str = "") -> None:
    payload = {
        "area": "copias",
        "event_key": event_key,
        "title": title,
        "body": body,
        "url": "https://copias.cortexdev.win",
        "dedupe_key": dedupe_key,
    }
    if scope:
        payload["scope"] = scope
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        config.NOTIFY_URL + "/api/events",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.NOTIFY_INGEST_TOKEN}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception as exc:  # noqa: BLE001
        log.warning("no se pudo publicar el evento %s: %s", event_key, exc)


def _open_critical() -> list[dict]:
    return db.query(
        "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
        "JOIN jobs j ON j.id = i.job_id "
        "WHERE i.resolved_at IS NULL AND i.`level` = 'danger' ORDER BY i.last_seen"
    )


def _recent_resolved() -> list[dict]:
    since = logs.to_naive(logs.now() - timedelta(minutes=15))
    return db.query(
        "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
        "JOIN jobs j ON j.id = i.job_id "
        "WHERE i.resolved_at IS NOT NULL AND i.resolved_at >= %s ORDER BY i.resolved_at",
        (since,),
    )


def flush_push() -> int:
    """Publica a notify los incidentes críticos abiertos y los resueltos recientes.

    El estado de 'ya enviado' vive en memoria y lo refuerza el dedupe del servicio
    notify, así que no necesita columnas nuevas en la tabla de incidentes.
    """
    if not push_enabled() or not db.available():
        return 0
    count = 0
    try:
        opened = _open_critical()
    except Exception:  # noqa: BLE001
        opened = []
    for row in opened:
        if row["id"] in _pushed_open:
            continue
        name = row.get("job_name") or row.get("job_slug") or "tarea"
        _publish("incident.open", f"{name}: falló",
                 _incident_body(row, "Incidente abierto"), f"incident-{row['id']}",
                 scope=row.get("job_slug") or "")
        _pushed_open.add(row["id"])
        count += 1
    try:
        resolved = _recent_resolved()
    except Exception:  # noqa: BLE001
        resolved = []
    for row in resolved:
        if row["id"] in _pushed_resolved:
            continue
        name = row.get("job_name") or row.get("job_slug") or "tarea"
        _publish("incident.recovered", f"{name}: recuperado",
                 _incident_body(row, "Incidente resuelto"), f"incident-{row['id']}-ok",
                 scope=row.get("job_slug") or "")
        _pushed_resolved.add(row["id"])
        count += 1
    return count


def _digest_rows() -> tuple[list[dict], list[dict], list[dict]]:
    since = logs.to_naive(logs.now() - timedelta(hours=24))
    active = db.query(
        "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
        "JOIN jobs j ON j.id = i.job_id "
        "WHERE i.resolved_at IS NULL OR i.last_seen >= %s "
        "ORDER BY i.`level` DESC, i.last_seen DESC",
        (since,),
    )
    omitted = db.query(
        "SELECT j.slug AS job_slug, j.name AS job_name, COUNT(*) AS n "
        "FROM runs r JOIN jobs j ON j.id = r.job_id "
        "WHERE r.status = 'OMITIDO' AND r.started_at >= %s GROUP BY j.slug, j.name",
        (since,),
    )
    partial = db.query(
        "SELECT j.slug AS job_slug, j.name AS job_name, COUNT(*) AS n "
        "FROM runs r JOIN jobs j ON j.id = r.job_id "
        "WHERE r.status = 'PARCIAL' AND r.started_at >= %s GROUP BY j.slug, j.name",
        (since,),
    )
    return active, omitted, partial


def _digest_body(active, omitted, partial) -> str:
    lines = ["Resumen de copias de las últimas 24 h", ""]
    if active:
        lines.append("Incidentes:")
        for row in active:
            estado = "abierto" if not row.get("resolved_at") else "resuelto"
            lines.append(
                f"  - {row.get('job_name') or row.get('job_slug')}: "
                f"{row.get('error_class') or '?'} ({estado}, x{int(row.get('count') or 0)})"
            )
    else:
        lines.append("Sin incidentes.")
    if omitted:
        lines.append("")
        lines.append("Rondas omitidas por cola ocupada:")
        lines += [f"  - {row.get('job_name') or row.get('job_slug')}: {int(row.get('n') or 0)}" for row in omitted]
    if partial:
        lines.append("")
        lines.append("Cierres parciales (archivos con error):")
        lines += [f"  - {row.get('job_name') or row.get('job_slug')}: {int(row.get('n') or 0)}" for row in partial]
    lines += ["", "Portal: https://copias.cortexdev.win"]
    return "\n".join(lines)


def send_digest(force: bool = False) -> int:
    """Envía el resumen diario.

    Devuelve 1 si se envió, 0 si no aplica (desactivado o ya enviado hoy) y -1 si
    el envío falló.
    """
    if not enabled() or not db.available():
        return 0
    today = logs.now().strftime("%Y-%m-%d")
    if not force:
        try:
            if _DIGEST_STATE.read_text(encoding="utf-8").strip() == today:
                return 0
        except OSError:
            pass
    try:
        active, omitted, partial = _digest_rows()
    except Exception:  # noqa: BLE001
        log.exception("no se pudo construir el resumen de copias")
        return -1
    if not _send("[copias] Resumen diario", _digest_body(active, omitted, partial)):
        return -1
    try:
        _DIGEST_STATE.write_text(today, encoding="utf-8")
    except OSError as exc:
        log.warning("no se pudo marcar el resumen diario como enviado: %s", exc)
    return 1
