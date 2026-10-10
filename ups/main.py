"""FastAPI app: UPS current state + history + WebSocket, and the static frontend."""

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import collector
import db

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
RANGES = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}


def _env_float(name, default=0.0):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


ENERGY_PRICE_PER_KWH = _env_float("ENERGY_PRICE_PER_KWH", 0.0)
ENERGY_CURRENCY = os.environ.get("ENERGY_CURRENCY", "COP").strip() or "COP"


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(collector.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="UPS Dashboard", lifespan=lifespan)


def _stale(ts):
    now = int(time.time())
    limit = max(3 * collector.POLL_INTERVAL, 90)
    return ts is None or (now - ts) > limit


@app.get("/healthz")
async def healthz():
    summary = collector.latest()
    ts = summary["ts"] if summary else None
    return {
        "status": "ok",
        "last_seen": ts,
        "age_s": (int(time.time()) - ts) if ts else None,
        "stale": _stale(ts),
        "last_error": collector.last_error(),
        "source": collector.source(),
        "simulate": collector.SIMULATE,
    }


@app.get("/api/meta")
async def meta():
    summary = collector.latest() or {}
    return {
        "metrics": [
            {"key": key, **info} for key, info in db.METRICS.items()
        ],
        "ranges": list(RANGES.keys()),
        "energy": {
            "price_per_kwh": ENERGY_PRICE_PER_KWH,
            "currency": ENERGY_CURRENCY,
        },
        "data_start": await asyncio.to_thread(db.first_ts),
        "thresholds": {
            "charge_warning": summary.get("charge_warning", 50),
            "charge_low": summary.get("charge_low", 10),
            "runtime_low": summary.get("runtime_low", 120),
        },
    }


@app.get("/api/summary")
async def api_summary():
    summary = collector.latest()
    if summary is None:
        return {"stale": True, "last_seen": None, "error": collector.last_error()}
    payload = dict(summary)
    payload["stale"] = _stale(summary["ts"])
    payload["last_seen"] = summary["ts"]
    payload["error"] = collector.last_error()
    return payload


@app.get("/api/history")
async def api_history(metric: str = Query(...), range: str = Query("24h")):
    if metric not in db.METRICS:
        raise HTTPException(status_code=400, detail="metrica invalida")
    if range not in RANGES:
        raise HTTPException(status_code=400, detail="rango invalido")
    until = int(time.time())
    since = until - RANGES[range]
    points = await asyncio.to_thread(db.history, metric, since, until)
    return {
        "metric": metric,
        "range": range,
        "resolution": "hourly" if (until - since) > db.HOURLY_THRESHOLD else "raw",
        "points": points,
    }


@app.get("/api/consumption/month")
async def api_consumption_month(since: int = Query(...), until: int = Query(...)):
    if until <= since:
        raise HTTPException(status_code=400, detail="rango invalido")
    points = await asyncio.to_thread(db.hourly_series, "ups_load", since, until)
    return {"metric": "ups_load", "since": since, "until": until, "points": points}


ON_BATTERY_TOKENS = {"OB", "DISCHRG", "LB"}


def _is_on_battery(status):
    return bool(set((status or "").upper().split()) & ON_BATTERY_TOKENS)


def _first_last_avg(metric, start, end):
    points = db.history(metric, start, end)
    values = [p[1] for p in points if p[1] is not None]
    if not values:
        return None, None, None
    return values[0], values[-1], sum(values) / len(values)


@app.get("/api/events")
async def api_events(range: str = Query("7d")):
    if range not in RANGES:
        raise HTTPException(status_code=400, detail="rango invalido")
    since = int(time.time()) - RANGES[range]
    rows = await asyncio.to_thread(db.events, since)
    for row in rows:
        row["to_class"] = collector.status_class(row.get("to_status"))
        row["from_class"] = collector.status_class(row.get("from_status"))
        row["to_label"] = collector.status_label(row.get("to_status"))
        row["from_label"] = collector.status_label(row.get("from_status"))
    return {"range": range, "events": rows}


@app.get("/api/power-events")
async def api_power_events(range: str = Query("30d")):
    if range not in RANGES:
        raise HTTPException(status_code=400, detail="rango invalido")
    now = int(time.time())
    since = now - RANGES[range]
    rows = await asyncio.to_thread(db.events, since)
    intervals = []
    open_start = None
    for row in reversed(rows):
        ts = row.get("ts")
        if _is_on_battery(row.get("to_status")) and not _is_on_battery(row.get("from_status")):
            if open_start is None:
                open_start = ts
        elif open_start is not None and not _is_on_battery(row.get("to_status")):
            intervals.append((open_start, ts))
            open_start = None
    if open_start is not None:
        intervals.append((open_start, None))

    events = []
    for started_at, ended_at in intervals:
        window_end = ended_at or now
        battery_start, battery_end, _ = await asyncio.to_thread(
            _first_last_avg, "battery_charge", started_at, window_end
        )
        _, _, load_avg = await asyncio.to_thread(
            _first_last_avg, "ups_load", started_at, window_end
        )
        events.append(
            {
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_s": window_end - started_at,
                "ongoing": ended_at is None,
                "battery_start": battery_start,
                "battery_end": battery_end,
                "load_avg_pct": load_avg,
            }
        )
    events.reverse()
    return {"range": range, "events": events}


@app.websocket("/api/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    queue = collector.subscribe()
    try:
        summary = collector.latest()
        if summary:
            await websocket.send_json(summary)
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        collector.unsubscribe(queue)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
