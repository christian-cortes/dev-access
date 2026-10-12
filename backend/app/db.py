import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("DB_PATH", "/data/portal.db")
CATALOG_PATH = os.environ.get("CATALOG_PATH", str(BASE_DIR / "catalog.yml"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))

DEFAULT_ACCEPT = [200, 301, 302, 303, 307, 308, 401, 403]

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    url         TEXT NOT NULL,
    probe_url   TEXT,
    method      TEXT DEFAULT 'GET',
    accept_codes TEXT,
    tech        TEXT,
    icon        TEXT,
    color       TEXT,
    badge       INTEGER DEFAULT 1,
    note        TEXT,
    sort        INTEGER DEFAULT 100,
    active      INTEGER DEFAULT 1,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    code        INTEGER,
    latency_ms  INTEGER,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS checks_service_ts ON checks(service_id, ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)


def ping():
    with db() as conn:
        conn.execute("SELECT 1").fetchone()


def load_catalog():
    with open(CATALOG_PATH, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def parse_codes(value):
    if not value:
        return list(DEFAULT_ACCEPT)
    if isinstance(value, (list, tuple, set)):
        return [int(code) for code in value]
    return [int(part) for part in str(value).split(",") if part.strip()]


def _service_values(service, defaults):
    accept = service.get("accept", defaults.get("default_accept"))
    tech = service.get("tech") or []
    return (
        service["slug"],
        service["name"],
        service["category"],
        service["url"],
        service.get("probe_url"),
        (service.get("method") or defaults.get("method") or "GET").upper(),
        ",".join(str(code) for code in parse_codes(accept)),
        ",".join(tech) if isinstance(tech, (list, tuple)) else str(tech),
        service.get("icon"),
        service.get("color"),
        1 if service.get("badge", True) else 0,
        service.get("note") or "",
        int(service.get("sort", 100)),
        1 if service.get("active", True) else 0,
        now_utc(),
    )


def sync_catalog(catalog):
    defaults = catalog.get("settings", {})
    slug_default = parse_codes(defaults.get("default_accept"))
    services = catalog.get("services", [])
    slugs = [service["slug"] for service in services]

    with db() as conn:
        for service in services:
            conn.execute(
                """
                INSERT INTO services (
                    slug, name, category, url, probe_url, method, accept_codes,
                    tech, icon, color, badge, note, sort, active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    url = excluded.url,
                    probe_url = excluded.probe_url,
                    method = excluded.method,
                    accept_codes = excluded.accept_codes,
                    tech = excluded.tech,
                    icon = excluded.icon,
                    color = excluded.color,
                    badge = excluded.badge,
                    note = excluded.note,
                    sort = excluded.sort,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                _service_values(service, {"method": "GET", "default_accept": slug_default}),
            )
        if slugs:
            placeholders = ",".join("?" for _ in slugs)
            conn.execute(
                f"UPDATE services SET active = 0 WHERE slug NOT IN ({placeholders})",
                slugs,
            )
        else:
            conn.execute("UPDATE services SET active = 0")


def active_services():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, slug, name, url, probe_url, method, accept_codes
            FROM services WHERE active = 1
            ORDER BY sort, id
            """
        ).fetchall()
    services = []
    for row in rows:
        service = dict(row)
        service["accept_set"] = parse_codes(service.pop("accept_codes"))
        services.append(service)
    return services


def record_checks(entries):
    if not entries:
        return
    ts = now_utc()
    with db() as conn:
        conn.executemany(
            """
            INSERT INTO checks (service_id, ts, ok, code, latency_ms, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(sid, ts, ok, code, latency, error) for sid, ok, code, latency, error in entries],
        )


def purge_old_checks():
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    ).replace(microsecond=0).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))


def recent_oks(per_service):
    """Últimos `per_service` resultados ok por servicio activo (más nuevo primero).

    Se usa para la histéresis: un solo fallo no debe marcar caída.
    """
    per_service = max(1, int(per_service))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT slug, ok FROM (
                SELECT s.slug AS slug, c.ok AS ok,
                       ROW_NUMBER() OVER (PARTITION BY c.service_id ORDER BY c.id DESC) AS rn
                FROM services s
                JOIN checks c ON c.service_id = s.id
                WHERE s.active = 1
            )
            WHERE rn <= ?
            ORDER BY slug, rn
            """,
            (per_service,),
        ).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["slug"], []).append(bool(row["ok"]))
    return out


def latest_statuses():
    uptime_since = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).replace(microsecond=0).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.slug, c.ts, c.ok, c.code, c.latency_ms, c.error
            FROM services s
            LEFT JOIN checks c ON c.id = (
                SELECT id FROM checks WHERE service_id = s.id ORDER BY id DESC LIMIT 1
            )
            WHERE s.active = 1
            """
        ).fetchall()
        uptime_rows = conn.execute(
            """
            SELECT s.slug, AVG(c.ok) AS uptime
            FROM checks c JOIN services s ON s.id = c.service_id
            WHERE c.ts >= ?
            GROUP BY s.slug
            """,
            (uptime_since,),
        ).fetchall()

    uptime = {row["slug"]: row["uptime"] for row in uptime_rows}
    statuses = {}
    for row in rows:
        if row["ts"] is None:
            state = "unknown"
        elif not row["ok"]:
            state = "offline"
        elif row["code"] is not None and 200 <= row["code"] < 300:
            state = "online"
        else:
            state = "auth"
        statuses[row["slug"]] = {
            "state": state,
            "code": row["code"],
            "latency_ms": row["latency_ms"],
            "checked_at": row["ts"],
            "error": row["error"],
            "uptime_24h": round(uptime[row["slug"]], 2) if row["slug"] in uptime else None,
        }
    return statuses


def set_meta(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def get_meta(key):
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
