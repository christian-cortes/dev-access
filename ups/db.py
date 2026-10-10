"""SQLite storage for the UPS dashboard (raw samples, hourly rollups, events)."""

import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/ups.db")
RAW_RETENTION_DAYS = int(os.environ.get("RAW_RETENTION_DAYS", "30"))
HOURLY_RETENTION_DAYS = int(os.environ.get("HOURLY_RETENTION_DAYS", "365"))

# Span above which history is served from hourly rollups instead of raw samples.
HOURLY_THRESHOLD = 48 * 3600

# Stored metric -> presentation metadata (single source of truth for the schema)
METRICS = {
    "battery_charge": {"label": "Carga de bateria", "unit": "%", "color": "#34d399"},
    "battery_runtime": {"label": "Autonomia", "unit": "s", "color": "#60a5fa"},
    "ups_load": {"label": "Carga de la UPS", "unit": "%", "color": "#fbbf24"},
    "input_voltage": {"label": "Voltaje de entrada", "unit": "V", "color": "#a78bfa"},
    "battery_voltage": {"label": "Voltaje de bateria", "unit": "V", "color": "#22d3ee"},
}

SAMPLE_COLUMNS = list(METRICS.keys())

_local = threading.local()


def get_conn():
    """One reused connection per thread (WAL; pragmas set once)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    columns = ",\n                ".join(f"{metric} REAL" for metric in SAMPLE_COLUMNS)
    conn = get_conn()
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS samples(
            ts INTEGER PRIMARY KEY,
            {columns},
            status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
        CREATE TABLE IF NOT EXISTS hourly(
            ts INTEGER NOT NULL,
            metric TEXT NOT NULL,
            avg REAL,
            min REAL,
            max REAL,
            n INTEGER,
            PRIMARY KEY(ts, metric)
        );
        CREATE INDEX IF NOT EXISTS idx_hourly_metric_ts ON hourly(metric, ts);
        CREATE TABLE IF NOT EXISTS events(
            ts INTEGER PRIMARY KEY,
            from_status TEXT,
            to_status TEXT
        );
        """
    )
    conn.commit()


def insert_sample(ts, values, status):
    conn = get_conn()
    cols = ["ts"] + SAMPLE_COLUMNS + ["status"]
    params = [ts] + [values.get(c) for c in SAMPLE_COLUMNS] + [status]
    placeholders = ",".join("?" for _ in cols)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO samples({','.join(cols)}) VALUES({placeholders})",
            params,
        )


def record_event(ts, from_status, to_status):
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO events(ts, from_status, to_status) VALUES(?,?,?)",
            (ts, from_status, to_status),
        )


def last_status():
    conn = get_conn()
    row = conn.execute("SELECT status FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    return row["status"] if row else None


def history(metric, since_ts, until_ts):
    """Return [[ts, value], ...] from raw samples or hourly rollups."""
    if metric not in METRICS:
        return []
    conn = get_conn()
    if (until_ts - since_ts) > HOURLY_THRESHOLD:
        rows = conn.execute(
            "SELECT ts, avg AS v FROM hourly WHERE metric=? AND ts>=? AND ts<=? ORDER BY ts",
            (metric, since_ts, until_ts),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT ts, {metric} AS v FROM samples WHERE ts>=? AND ts<=? AND {metric} IS NOT NULL ORDER BY ts",
            (since_ts, until_ts),
        ).fetchall()
    return [[r["ts"], r["v"]] for r in rows]


def hourly_series(metric, since_ts, until_ts):
    """Return [[ts, avg, min, max, n], ...] from hourly rollups."""
    if metric not in METRICS:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, avg, min, max, n FROM hourly"
        " WHERE metric=? AND ts>=? AND ts<=? ORDER BY ts",
        (metric, since_ts, until_ts),
    ).fetchall()
    return [[r["ts"], r["avg"], r["min"], r["max"], r["n"]] for r in rows]


def first_ts():
    """Earliest stored timestamp across raw samples and hourly rollups."""
    conn = get_conn()
    candidates = []
    for query in ("SELECT MIN(ts) AS t FROM samples", "SELECT MIN(ts) AS t FROM hourly"):
        row = conn.execute(query).fetchone()
        if row and row["t"] is not None:
            candidates.append(row["t"])
    return min(candidates) if candidates else None


def events(since_ts):
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, from_status, to_status FROM events WHERE ts>=? ORDER BY ts DESC LIMIT 200",
        (since_ts,),
    ).fetchall()
    return [dict(r) for r in rows]


def rollup(now=None):
    """(Re)aggregate the last ~3h of raw samples into hourly rows."""
    now = now or int(time.time())
    cutoff = (now // 3600) * 3600 - 3 * 3600
    conn = get_conn()
    with conn:
        for metric in METRICS:
            conn.execute(
                f"""
                INSERT INTO hourly(ts, metric, avg, min, max, n)
                SELECT (ts/3600)*3600, ?, AVG({metric}), MIN({metric}), MAX({metric}), COUNT({metric})
                FROM samples
                WHERE {metric} IS NOT NULL AND ts >= ?
                GROUP BY (ts/3600)*3600
                ON CONFLICT(ts, metric) DO UPDATE SET
                    avg=excluded.avg, min=excluded.min, max=excluded.max, n=excluded.n
                """,
                (metric, cutoff),
            )


def prune(now=None):
    now = now or int(time.time())
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM samples WHERE ts < ?", (now - RAW_RETENTION_DAYS * 86400,))
        conn.execute("DELETE FROM hourly WHERE ts < ?", (now - HOURLY_RETENTION_DAYS * 86400,))
