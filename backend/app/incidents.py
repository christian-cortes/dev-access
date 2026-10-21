"""Consultas de incidentes y MTTR."""
from __future__ import annotations

from datetime import datetime, timezone

from . import db, windows


def _rows(query, params):
    with db.db() as conn:
        return conn.execute(query, params).fetchall()


def list_incidents(window="7d", limit=50, kind=None):
    windows.validate_window(window)
    limit = max(1, min(int(limit), 200))
    start = windows.start_iso(window)
    params = [start]
    kind_sql = ""
    if kind in ("service", "infra"):
        kind_sql = " AND i.kind = ?"
        params.append(kind)
    params.append(limit)
    rows = _rows(
        f"""
        SELECT i.id, i.service_id, i.started_at, i.ended_at, i.duration_s,
               i.kind, i.last_code, i.last_error, i.mass,
               s.slug, s.name
        FROM incidents i
        LEFT JOIN services s ON s.id = i.service_id
        WHERE (i.started_at >= ? OR i.ended_at IS NULL){kind_sql}
        ORDER BY i.started_at DESC
        LIMIT ?
        """,
        params,
    )
    return [dict(row) for row in rows]


def service_incidents(slug, limit=50):
    limit = max(1, min(int(limit), 200))
    rows = _rows(
        """
        SELECT i.id, i.started_at, i.ended_at, i.duration_s, i.last_code,
               i.last_error, i.mass
        FROM incidents i JOIN services s ON s.id = i.service_id
        WHERE s.slug = ? AND i.kind = 'service'
        ORDER BY i.started_at DESC LIMIT ?
        """,
        (slug, limit),
    )
    return [dict(row) for row in rows]


def summary(window="24h"):
    windows.validate_window(window)
    start = windows.start_iso(window)
    with db.db() as conn:
        open_row = conn.execute(
            "SELECT COUNT(*) AS n FROM incidents "
            "WHERE ended_at IS NULL AND kind = 'service'"
        ).fetchone()
        started_row = conn.execute(
            "SELECT COUNT(*) AS n FROM incidents "
            "WHERE started_at >= ? AND kind = 'service'",
            (start,),
        ).fetchone()
        mttr_row = conn.execute(
            "SELECT AVG(duration_s) AS avg_s, COUNT(*) AS n FROM incidents "
            "WHERE ended_at >= ? AND ended_at IS NOT NULL AND kind = 'service'",
            (start,),
        ).fetchone()
    mttr = round(mttr_row["avg_s"]) if mttr_row["avg_s"] is not None else None
    return {
        "incidents_open": open_row["n"],
        "incidents_24h": started_row["n"],
        "mttr_s": mttr,
        "mttr_sample_size": mttr_row["n"],
    }
