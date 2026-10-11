"""SQLite del servicio de notificaciones: suscripciones, preferencias y eventos."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id          TEXT PRIMARY KEY,
    area        TEXT NOT NULL,
    token       TEXT NOT NULL,
    username    TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(area, token)
);

CREATE TABLE IF NOT EXISTS prefs (
    username    TEXT NOT NULL,
    area        TEXT NOT NULL,
    config      TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (username, area)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    area        TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    level       TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    dedupe_key  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_dedupe ON events(dedupe_key, created_at);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL,
    subscription_id TEXT NOT NULL DEFAULT '',
    channel         TEXT NOT NULL,
    target          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    error           TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        directory = os.path.dirname(config.DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def now() -> int:
    return int(time.time())


# --- Suscripciones ---------------------------------------------------------

def upsert_subscription(sub_id: str, area: str, token: str, username: str,
                        platform: str = "") -> None:
    ts = now()
    conn = get_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO subscriptions(id, area, token, username, platform, created_at, last_seen, disabled)
            VALUES(?,?,?,?,?,?,?,0)
            ON CONFLICT(area, token) DO UPDATE SET
                id=excluded.id, username=excluded.username, platform=excluded.platform,
                last_seen=excluded.last_seen, disabled=0
            """,
            (sub_id, area, token, username, platform, ts, ts),
        )


def delete_subscription(sub_id: str, username: str) -> None:
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM subscriptions WHERE id=? AND username=?", (sub_id, username))


def delete_token(token: str) -> None:
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM subscriptions WHERE token=?", (token,))


def subscriptions_for_area(area: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE area=? AND disabled=0", (area,)
    ).fetchall()
    return [dict(row) for row in rows]


def user_subscriptions(username: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE username=? AND disabled=0", (username,)
    ).fetchall()
    return [dict(row) for row in rows]


# --- Preferencias ----------------------------------------------------------

def default_prefs(area: str) -> dict:
    from . import catalog

    area_def = catalog.AREAS.get(area) or {"events": {}}
    return {
        "enabled": True,
        "events": {key: True for key in area_def["events"]},
        "min_level": "info",
        "channels": ["fcm", "email"],
        "quiet": {"enabled": config.DEFAULT_QUIET, "start": config.QUIET_START, "end": config.QUIET_END},
    }


def get_prefs(username: str, area: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT config FROM prefs WHERE username=? AND area=?", (username, area)
    ).fetchone()
    prefs = default_prefs(area)
    if row:
        try:
            stored = json.loads(row["config"])
        except (ValueError, TypeError):
            stored = {}
        if isinstance(stored, dict):
            prefs.update({k: v for k, v in stored.items() if k in ("enabled", "events", "min_level", "channels", "quiet")})
    return prefs


def set_prefs(username: str, area: str, prefs: dict) -> dict:
    base = get_prefs(username, area)
    for key in ("enabled", "events", "min_level", "channels", "quiet"):
        if key in prefs:
            base[key] = prefs[key]
    conn = get_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO prefs(username, area, config, updated_at) VALUES(?,?,?,?)
            ON CONFLICT(username, area) DO UPDATE SET config=excluded.config, updated_at=excluded.updated_at
            """,
            (username, area, json.dumps(base), now()),
        )
    return base


def all_user_prefs(username: str) -> dict:
    from . import catalog

    return {area: get_prefs(username, area) for area in catalog.AREAS}


# --- Eventos ---------------------------------------------------------------

def recent_dedupe(dedupe_key: str, window: int) -> bool:
    if not dedupe_key:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM events WHERE dedupe_key=? AND created_at>=? LIMIT 1",
        (dedupe_key, now() - window),
    ).fetchone()
    return row is not None


def insert_event(area: str, event_key: str, level: str, title: str, body: str,
                 url: str, dedupe_key: str) -> int:
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO events(area, event_key, level, title, body, url, dedupe_key, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (area, event_key, level, title, body, url, dedupe_key, now()),
        )
    return int(cur.lastrowid)


def record_delivery(event_id: int, subscription_id: str, channel: str, target: str,
                    status: str, error: str = "") -> None:
    conn = get_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO deliveries(event_id, subscription_id, channel, target, status, error, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (event_id, subscription_id, channel, target, status, error, now()),
        )


def prune(now_ts: int | None = None) -> None:
    ts = now_ts or now()
    cutoff = ts - config.EVENT_RETENTION_DAYS * 86400
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM deliveries WHERE event_id NOT IN (SELECT id FROM events)"
        )
