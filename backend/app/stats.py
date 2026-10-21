"""Agregados operativos calculados desde checks_hourly."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from . import db, incidents, windows

_latencies = defaultdict(lambda: deque(maxlen=200))


def hydrate_latencies():
    _latencies.clear()
    with db.db() as conn:
        services = conn.execute("SELECT id FROM services WHERE active = 1").fetchall()
        for service in services:
            rows = conn.execute(
                "SELECT latency_ms FROM checks WHERE service_id = ? AND ok = 1 "
                "AND latency_ms IS NOT NULL ORDER BY id DESC LIMIT 200",
                (service["id"],),
            ).fetchall()
            for row in rows:
                _latencies[service["id"]].append(int(row["latency_ms"]))


def record_entries(entries):
    for entry in entries:
        if len(entry) == 6:
            service_id, ok, _, latency, _, _ = entry
        else:
            service_id, ok, _, latency, _ = entry
        if ok and latency is not None:
            _latencies[service_id].append(int(latency))


def _p95():
    values = sorted(value for samples in _latencies.values() for value in samples)
    if not values:
        return None
    index = max(0, math.ceil(0.95 * len(values)) - 1)
    return values[index]


def _pct(ok, total):
    return round(ok / total * 100, 1) if total else None


def _hour(value):
    return value.replace(minute=0, second=0, microsecond=0)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute(window, statuses=None):
    windows.validate_window(window)
    window_seconds = windows.seconds(window)
    start = windows.start_dt(window)
    start_iso = start.isoformat()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if statuses is None:
        statuses = db.current_statuses()

    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT h.service_id, h.hour_ts, h.total, h.ok, h.latency_sum,
                   s.category
            FROM checks_hourly h
            JOIN services s ON s.id = h.service_id
            WHERE h.hour_ts >= ? AND s.active = 1
            """,
            (start_iso,),
        ).fetchall()
        category_totals = conn.execute(
            "SELECT category, COUNT(*) AS n FROM services WHERE active = 1 "
            "GROUP BY category ORDER BY category"
        ).fetchall()

    total = 0
    ok_total = 0
    latency_sum = 0
    covered = set()
    per_hour = defaultdict(lambda: {"total": 0, "ok": 0, "latency_sum": 0})
    per_category = defaultdict(lambda: {"total": 0, "ok": 0, "latency_sum": 0})
    data_since = None

    for row in rows:
        total += row["total"]
        ok_total += row["ok"]
        latency_sum += row["latency_sum"]
        covered.add(row["service_id"])
        if data_since is None or row["hour_ts"] < data_since:
            data_since = row["hour_ts"]
        hour = per_hour[row["hour_ts"]]
        hour["total"] += row["total"]
        hour["ok"] += row["ok"]
        hour["latency_sum"] += row["latency_sum"]
        category = per_category[row["category"]]
        category["total"] += row["total"]
        category["ok"] += row["ok"]
        category["latency_sum"] += row["latency_sum"]

    up = sum(1 for value in statuses.values() if value["state"] in ("online", "auth"))
    down = sum(1 for value in statuses.values() if value["state"] == "offline")
    unknown = sum(
        1 for value in statuses.values() if value["state"] not in ("online", "auth", "offline")
    )
    total_services = len(statuses)

    series = []
    end_hour = _hour(now)
    bucket_count = max(1, int(window_seconds // 3600))
    cursor = end_hour - timedelta(hours=bucket_count - 1)
    while cursor <= end_hour:
        key = cursor.isoformat()
        point = per_hour.get(key)
        point_total = point["total"] if point else 0
        point_ok = point["ok"] if point else 0
        series.append(
            {
                "ts": key,
                "total": point_total,
                "ok": point_ok,
                "availability_pct": _pct(point_ok, point_total),
                "avg_latency_ms": (
                    round(point["latency_sum"] / point_ok)
                    if point and point_ok
                    else None
                ),
            }
        )
        cursor += timedelta(hours=1)

    categories = []
    for row in category_totals:
        slug = row["category"]
        cat = per_category.get(slug, {"total": 0, "ok": 0})
        cat_up = sum(
            1
            for value in statuses.values()
            if value.get("category") == slug
            and value["state"] in ("online", "auth")
        )
        categories.append(
            {
                "slug": slug,
                "total": row["n"],
                "up": cat_up,
                "availability_pct": _pct(cat["ok"], cat["total"]),
            }
        )

    incident_24 = incidents.summary("24h")
    incident_window = incidents.summary(window)
    effective = window_seconds
    if data_since:
        available = (now - _parse(data_since)).total_seconds()
        effective = max(0, min(window_seconds, available))
    last_check = max(
        (value["checked_at"] for value in statuses.values() if value.get("checked_at")),
        default=None,
    )
    age = None
    if last_check:
        parsed = _parse(last_check)
        if parsed:
            age = max(0, int((now - parsed).total_seconds()))

    summary = {
        "services_total": total_services,
        "services_up": up,
        "services_down": down,
        "services_unknown": unknown,
        "availability_pct": _pct(ok_total, total),
        "coverage_pct": round(len(covered) / total_services * 100, 1) if total_services else None,
        "avg_latency_ms": round(latency_sum / ok_total) if ok_total else None,
        "p95_latency_ms": _p95(),
        "incidents_open": incident_24["incidents_open"],
        "incidents_24h": incident_24["incidents_24h"],
        "incidents_window": incident_window["incidents_24h"],
        "mttr_s": incident_window["mttr_s"],
        "mttr_sample_size": incident_window["mttr_sample_size"],
        "last_check_at": last_check,
        "last_check_age_s": age,
    }
    return {
        "generated_at": now.isoformat(),
        "window": window,
        "window_effective": windows.label(effective) if data_since else None,
        "data_since": data_since,
        "summary": summary,
        "categories": categories,
        "series": series,
    }
