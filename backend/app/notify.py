"""Publicación de eventos al servicio de notificaciones (notify)."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("portal-api")

NOTIFY_URL = os.environ.get("NOTIFY_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("NOTIFY_INGEST_TOKEN", "").strip()


def configured() -> bool:
    return bool(NOTIFY_URL and INGEST_TOKEN)


def publish(area: str, event_key: str, title: str, body: str = "",
            level: str | None = None, url: str = "", dedupe_key: str = "",
            scope: str = "") -> None:
    if not configured():
        return
    payload = {
        "area": area,
        "event_key": event_key,
        "title": title,
        "body": body,
        "url": url,
        "dedupe_key": dedupe_key,
    }
    if scope:
        payload["scope"] = scope
    if level:
        payload["level"] = level
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        NOTIFY_URL + "/api/events",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {INGEST_TOKEN}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception as exc:  # noqa: BLE001
        log.warning("no se pudo publicar evento %s: %s", event_key, exc)
