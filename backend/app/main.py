import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db, notify, probe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("portal-api")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
REFRESH_AFTER = int(os.environ.get("REFRESH_AFTER", "10"))


class State:
    def __init__(self):
        self.catalog = {}
        self.categories = []
        self.last_run = None
        self.refreshing = False


state = State()


def _stale():
    if not state.last_run:
        return True
    try:
        last = datetime.fromisoformat(state.last_run)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age > REFRESH_AFTER


async def run_checks():
    if state.refreshing:
        return
    state.refreshing = True
    try:
        services = db.active_services()
        results = await probe.probe_all(services)
        entries = [
            (service["id"], result["ok"], result["code"], result["latency_ms"], result["error"])
            for service, result in zip(services, results)
        ]
        db.record_checks(entries)
        db.purge_old_checks()
        _report_transitions(db.latest_statuses())
        state.last_run = db.now_utc()
        db.set_meta("last_run", state.last_run)
        log.info("chequeo completado: %s servicios", len(entries))
    except Exception:
        log.exception("fallo el ciclo de chequeo")
    finally:
        state.refreshing = False


def _report_transitions(statuses):
    """Publica caídas/recuperaciones comparando con el ciclo anterior."""
    raw = db.get_meta("notify_prev_states")
    try:
        prev = json.loads(raw) if raw else {}
    except ValueError:
        prev = {}
    services = {s["slug"]: s for s in state.catalog.get("services", [])}
    for slug, st in statuses.items():
        cur = st.get("state")
        old = prev.get(slug)
        if cur == old:
            continue
        svc = services.get(slug) or {}
        name = svc.get("name", slug)
        url = svc.get("url", "")
        if cur == "offline":
            notify.publish("index", "service.down", f"{name} no responde",
                           body=st.get("error") or f"Sin respuesta ({st.get('code') or 'sin código'}).",
                           url=url, dedupe_key=slug)
        elif old == "offline" and cur in ("online", "auth"):
            notify.publish("index", "service.recovered", f"{name} recuperado",
                           body="El servicio vuelve a responder.", url=url, dedupe_key=slug)
    db.set_meta("notify_prev_states",
                json.dumps({slug: st.get("state") for slug, st in statuses.items()}))


async def _loop():
    while True:
        await run_checks()
        await asyncio.sleep(POLL_INTERVAL)


def _maybe_refresh(refresh):
    if state.refreshing:
        return
    if refresh or _stale():
        asyncio.create_task(run_checks())


def _status_payload(status):
    return status or {
        "state": "unknown",
        "code": None,
        "latency_ms": None,
        "checked_at": None,
        "error": None,
        "uptime_24h": None,
    }


@contextlib.asynccontextmanager
async def lifespan(app):
    db.init_db()
    state.catalog = db.load_catalog()
    state.categories = state.catalog.get("categories", [])
    db.sync_catalog(state.catalog)
    state.last_run = db.get_meta("last_run")
    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CortexDev Portal API", lifespan=lifespan)


@app.get("/api/health")
async def health():
    try:
        db.ping()
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "error", "db": str(exc)})
    return {"status": "ok", "db": "ok", "last_run": state.last_run}


@app.get("/api/portal")
async def portal(refresh: int = 0):
    _maybe_refresh(refresh)
    statuses = db.latest_statuses()

    services = []
    for service in state.catalog.get("services", []):
        slug = service["slug"]
        tech = service.get("tech") or []
        services.append(
            {
                "slug": slug,
                "name": service["name"],
                "category": service["category"],
                "url": service["url"],
                "tech": tech if isinstance(tech, list) else [tech],
                "icon": service.get("icon"),
                "color": service.get("color"),
                "badge": bool(service.get("badge", True)),
                "note": service.get("note") or "",
                "status": _status_payload(statuses.get(slug)),
            }
        )

    categories = []
    for category in state.categories:
        slug = category["slug"]
        members = [service for service in services if service["category"] == slug]
        counts = {"online": 0, "auth": 0, "offline": 0, "unknown": 0}
        for service in members:
            counts[service["status"]["state"]] = counts.get(service["status"]["state"], 0) + 1
        categories.append(
            {
                "slug": slug,
                "title": category.get("title", slug),
                "description": category.get("description", ""),
                "url": category.get("url", "/"),
                "note_header": category.get("note_header", "Nota"),
                "count_label": category.get("count_label", "servicios"),
                "card_tech": category.get("card_tech", []),
                "count": len(members),
                "online": counts["online"] + counts["auth"],
                "offline": counts["offline"],
                "unknown": counts["unknown"],
                "state": (
                    "ok" if counts["offline"] == 0
                    else "degraded" if counts["online"] + counts["auth"] > 0
                    else "down"
                ),
            }
        )

    return {
        "generated_at": db.now_utc(),
        "last_run": state.last_run,
        "categories": categories,
        "services": services,
    }


@app.get("/api/status")
async def status(refresh: int = 0):
    _maybe_refresh(refresh)
    return {
        "generated_at": db.now_utc(),
        "last_run": state.last_run,
        "refreshing": state.refreshing,
        "statuses": db.latest_statuses(),
    }
