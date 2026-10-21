import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = "/data/portal.db"
CATALOG_PATH = os.environ.get("CATALOG_PATH", str(BASE_DIR / "catalog.yml"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
HOURLY_RETENTION_DAYS = int(os.environ.get("HOURLY_RETENTION_DAYS", "90"))
SCHEMA_VERSION = 3

DEFAULT_ACCEPT = [200, 301, 302, 303, 307, 308, 401, 403]

_db_path_override = None

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
    error       TEXT,
    state       TEXT
);

CREATE INDEX IF NOT EXISTS checks_service_ts ON checks(service_id, ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS checks_hourly (
    service_id INTEGER NOT NULL,
    hour_ts    TEXT NOT NULL,
    total      INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    latency_sum INTEGER NOT NULL DEFAULT 0,
    latency_max INTEGER,
    PRIMARY KEY (service_id, hour_ts)
);
CREATE INDEX IF NOT EXISTS checks_hourly_hour ON checks_hourly(hour_ts);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    service_id  INTEGER REFERENCES services(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration_s  INTEGER,
    kind        TEXT NOT NULL DEFAULT 'service',
    last_code   INTEGER,
    last_error  TEXT,
    mass        INTEGER DEFAULT 0,
    CHECK ((kind = 'service' AND service_id IS NOT NULL)
        OR (kind = 'infra' AND service_id IS NULL))
);
CREATE INDEX IF NOT EXISTS incidents_started ON incidents(started_at);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents(service_id, ended_at);
"""


def configure(path):
    """Fija la BD para tests o ejecuciones locales."""
    global _db_path_override
    _db_path_override = str(path) if path else None


def db_path():
    return _db_path_override or os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def connect():
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_info(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        if "state" not in _table_info(conn, "checks"):
            conn.execute("ALTER TABLE checks ADD COLUMN state TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS checks_ts ON checks(ts)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS checks_service_id_id ON checks(service_id, id DESC)"
        )
        conn.execute("PRAGMA user_version = 1")
        version = 1

    if version < 2:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checks_hourly (
                service_id INTEGER NOT NULL,
                hour_ts    TEXT NOT NULL,
                total      INTEGER NOT NULL DEFAULT 0,
                ok         INTEGER NOT NULL DEFAULT 0,
                latency_sum INTEGER NOT NULL DEFAULT 0,
                latency_max INTEGER,
                PRIMARY KEY (service_id, hour_ts)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS checks_hourly_hour ON checks_hourly(hour_ts)")
        conn.execute("PRAGMA user_version = 2")
        version = 2

    if version < 3:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id          INTEGER PRIMARY KEY,
                service_id  INTEGER REFERENCES services(id),
                started_at  TEXT NOT NULL,
                ended_at    TEXT,
                duration_s  INTEGER,
                kind        TEXT NOT NULL DEFAULT 'service',
                last_code   INTEGER,
                last_error  TEXT,
                mass        INTEGER DEFAULT 0,
                CHECK ((kind = 'service' AND service_id IS NOT NULL)
                    OR (kind = 'infra' AND service_id IS NULL))
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS incidents_started ON incidents(started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS incidents_open ON incidents(service_id, ended_at)")
        _close_duplicate_incidents(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_open_service "
            "ON incidents(service_id) WHERE ended_at IS NULL AND kind = 'service'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_open_infra "
            "ON incidents(kind) WHERE ended_at IS NULL AND kind = 'infra'"
        )
        conn.execute("PRAGMA user_version = 3")


def _close_duplicate_incidents(conn):
    rows = conn.execute(
        """
        SELECT service_id, kind, COUNT(*) AS n
        FROM incidents
        WHERE ended_at IS NULL
        GROUP BY service_id, kind
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in rows:
        if row["service_id"] is None:
            extra = conn.execute(
                "SELECT id, started_at FROM incidents WHERE ended_at IS NULL "
                "AND kind = ? ORDER BY id",
                (row["kind"],),
            ).fetchall()
        else:
            extra = conn.execute(
                "SELECT id, started_at FROM incidents WHERE ended_at IS NULL "
                "AND service_id = ? AND kind = ? ORDER BY id",
                (row["service_id"], row["kind"]),
            ).fetchall()
        for item in extra[1:]:
            _close_incident_row(conn, item["id"], item["started_at"], now_utc())


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def ping():
    with db() as conn:
        conn.execute("SELECT 1").fetchone()


def schema_version():
    with db() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


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


def record_checks(entries, ts=None):
    if not entries:
        return
    ts = ts or now_utc()
    values = []
    for entry in entries:
        if len(entry) == 6:
            sid, ok, code, latency, error, state = entry
        else:
            sid, ok, code, latency, error = entry
            state = None
        values.append((sid, ts, ok, code, latency, error, state))
    with db() as conn:
        conn.executemany(
            """
            INSERT INTO checks (service_id, ts, ok, code, latency_ms, error, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def _upsert_meta(conn, key, value):
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def set_meta(key, value):
    with db() as conn:
        _upsert_meta(conn, key, value)


def set_meta_map(values):
    with db() as conn:
        for key, value in values.items():
            _upsert_meta(conn, key, value)


def get_meta(key):
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get_meta_map(key):
    raw = get_meta(key)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def purge_old_checks(force=False):
    now = datetime.now(timezone.utc)
    last = parse_ts(get_meta("last_purge"))
    if not force and last and (now - last).total_seconds() < 3600:
        return False
    checks_cutoff = (now - timedelta(days=RETENTION_DAYS)).replace(microsecond=0).isoformat()
    hourly_cutoff = (
        now - timedelta(days=HOURLY_RETENTION_DAYS)
    ).replace(microsecond=0).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM checks WHERE ts < ?", (checks_cutoff,))
        conn.execute("DELETE FROM checks_hourly WHERE hour_ts < ?", (hourly_cutoff,))
        _upsert_meta(conn, "last_purge", now.replace(microsecond=0).isoformat())
    return True


def rollup_checks():
    """Actualiza checks_hourly una sola vez por check usando un watermark."""
    with db() as conn:
        max_row = conn.execute("SELECT MAX(id) AS max_id FROM checks").fetchone()
        max_id = max_row["max_id"] if max_row else None
        if max_id is None:
            return
        meta_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'hourly_last_check_id'"
        ).fetchone()
        watermark = None
        if meta_row and meta_row["value"]:
            try:
                watermark = int(meta_row["value"])
            except ValueError:
                watermark = None
        hourly_count = conn.execute("SELECT COUNT(*) AS n FROM checks_hourly").fetchone()["n"]

        if watermark is None:
            if hourly_count:
                _upsert_meta(conn, "hourly_last_check_id", str(max_id))
                return
            rows = conn.execute(
                """
                SELECT service_id,
                       substr(ts, 1, 13) || ':00:00+00:00' AS hour_ts,
                       COUNT(*) AS total,
                       SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok,
                       SUM(CASE WHEN ok = 1 THEN COALESCE(latency_ms, 0) ELSE 0 END)
                           AS latency_sum,
                       MAX(CASE WHEN ok = 1 THEN latency_ms ELSE NULL END) AS latency_max
                FROM checks
                GROUP BY service_id, hour_ts
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT service_id,
                       substr(ts, 1, 13) || ':00:00+00:00' AS hour_ts,
                       COUNT(*) AS total,
                       SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok,
                       SUM(CASE WHEN ok = 1 THEN COALESCE(latency_ms, 0) ELSE 0 END)
                           AS latency_sum,
                       MAX(CASE WHEN ok = 1 THEN latency_ms ELSE NULL END) AS latency_max
                FROM checks
                WHERE id > ?
                GROUP BY service_id, hour_ts
                """,
                (watermark,),
            ).fetchall()

        for row in rows:
            conn.execute(
                """
                INSERT INTO checks_hourly
                    (service_id, hour_ts, total, ok, latency_sum, latency_max)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_id, hour_ts) DO UPDATE SET
                    total = total + excluded.total,
                    ok = ok + excluded.ok,
                    latency_sum = latency_sum + excluded.latency_sum,
                    latency_max = CASE
                        WHEN excluded.latency_max IS NULL THEN latency_max
                        WHEN latency_max IS NULL THEN excluded.latency_max
                        ELSE MAX(latency_max, excluded.latency_max)
                    END
                """,
                (
                    row["service_id"],
                    row["hour_ts"],
                    row["total"],
                    row["ok"],
                    row["latency_sum"],
                    row["latency_max"],
                ),
            )
        _upsert_meta(conn, "hourly_last_check_id", str(max_id))


def recent_oks(per_service):
    """Últimos `per_service` resultados por servicio activo (más nuevo primero)."""
    per_service = max(1, int(per_service))
    with db() as conn:
        services = conn.execute("SELECT id, slug FROM services WHERE active = 1").fetchall()
        out = {}
        for row in services:
            samples = conn.execute(
                "SELECT ok FROM checks WHERE service_id = ? ORDER BY id DESC LIMIT ?",
                (row["id"], per_service),
            ).fetchall()
            out[row["slug"]] = [bool(sample["ok"]) for sample in samples]
    return out


def _classify(row):
    if row["ts"] is None:
        return "unknown"
    if not row["ok"]:
        return "offline"
    if row["code"] is not None and 200 <= row["code"] < 300:
        return "online"
    return "auth"


def latest_samples():
    """Estado crudo del último check por servicio, más uptime de 24 h."""
    uptime_since = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).replace(microsecond=0).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.id AS service_id, s.slug, s.name, s.url, s.category,
                   c.ts, c.ok, c.code, c.latency_ms, c.error, c.state
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
            WHERE c.ts >= ? AND s.active = 1
            GROUP BY s.slug
            """,
            (uptime_since,),
        ).fetchall()

    uptime = {row["slug"]: row["uptime"] for row in uptime_rows}
    statuses = {}
    for row in rows:
        raw = row["state"] if row["state"] in ("online", "auth", "offline") else _classify(row)
        pct = None
        if row["slug"] in uptime and uptime[row["slug"]] is not None:
            pct = round(uptime[row["slug"]] * 100, 1)
        statuses[row["slug"]] = {
            "service_id": row["service_id"],
            "slug": row["slug"],
            "name": row["name"],
            "url": row["url"],
            "state": raw,
            "last_state": raw,
            "state_since": None,
            "category": row["category"],
            "code": row["code"],
            "latency_ms": row["latency_ms"],
            "checked_at": row["ts"],
            "error": row["error"],
            "uptime_24h_pct": pct,
            "uptime_24h": pct,
        }
    return statuses


def current_statuses():
    samples = latest_samples()
    committed = get_meta_map("state_committed")
    since = get_meta_map("state_since")
    for slug, status in samples.items():
        status["state"] = committed.get(slug, status["last_state"] or "unknown")
        status["state_since"] = since.get(slug)
    return samples


def latest_statuses():
    return current_statuses()


def first_failure_ts(conn, service_id, limit=20):
    rows = conn.execute(
        "SELECT ts, ok FROM checks WHERE service_id = ? ORDER BY id DESC LIMIT ?",
        (service_id, limit),
    ).fetchall()
    if not rows or rows[0]["ok"]:
        return None
    earliest = rows[0]["ts"]
    for row in rows:
        if row["ok"]:
            break
        earliest = row["ts"]
    return earliest


def _close_incident_row(conn, incident_id, started_at, ended_at):
    start = parse_ts(started_at)
    end = parse_ts(ended_at)
    duration = None
    if start and end:
        duration = max(0, int((end - start).total_seconds()))
    conn.execute(
        "UPDATE incidents SET ended_at = ?, duration_s = ? WHERE id = ?",
        (ended_at, duration, incident_id),
    )


def apply_transition(committed, since, notified, mass, actions):
    """Persiste estado, incidents y flags en una sola transacción."""
    with db() as conn:
        for action in actions:
            kind = action[0]
            if kind == "open_service":
                _, service_id, started_at, code, error, mass_flag = action
                conn.execute(
                    """
                    INSERT OR IGNORE INTO incidents
                        (service_id, started_at, kind, last_code, last_error, mass)
                    VALUES (?, ?, 'service', ?, ?, ?)
                    """,
                    (service_id, started_at, code, error, 1 if mass_flag else 0),
                )
            elif kind == "close_service":
                _, service_id, ended_at = action
                row = conn.execute(
                    "SELECT id, started_at FROM incidents WHERE service_id = ? "
                    "AND ended_at IS NULL AND kind = 'service' ORDER BY id DESC LIMIT 1",
                    (service_id,),
                ).fetchone()
                if row:
                    _close_incident_row(conn, row["id"], row["started_at"], ended_at)
            elif kind == "open_infra":
                _, started_at = action
                conn.execute(
                    "INSERT OR IGNORE INTO incidents (started_at, kind) "
                    "VALUES (?, 'infra')",
                    (started_at,),
                )
            elif kind == "close_infra":
                _, ended_at = action
                row = conn.execute(
                    "SELECT id, started_at FROM incidents WHERE ended_at IS NULL "
                    "AND kind = 'infra' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    _close_incident_row(conn, row["id"], row["started_at"], ended_at)

        _upsert_meta(conn, "state_committed", json.dumps(committed, sort_keys=True))
        _upsert_meta(conn, "state_since", json.dumps(since, sort_keys=True))
        _upsert_meta(conn, "state_notified_offline", json.dumps(notified, sort_keys=True))
        _upsert_meta(conn, "state_mass_down", "1" if mass else "0")


def ensure_open_incidents(committed, since):
    now = now_utc()
    with db() as conn:
        for slug, state_value in committed.items():
            if state_value != "offline":
                continue
            service = conn.execute(
                "SELECT id FROM services WHERE slug = ? AND active = 1", (slug,)
            ).fetchone()
            if not service:
                continue
            open_row = conn.execute(
                "SELECT id FROM incidents WHERE service_id = ? AND ended_at IS NULL "
                "AND kind = 'service'",
                (service["id"],),
            ).fetchone()
            if open_row:
                continue
            started = since.get(slug) or first_failure_ts(conn, service["id"]) or now
            conn.execute(
                "INSERT OR IGNORE INTO incidents (service_id, started_at, kind) "
                "VALUES (?, ?, 'service')",
                (service["id"], started),
            )
