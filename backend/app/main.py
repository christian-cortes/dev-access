import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import catalog as catalog_mod
from . import db, incidents, notify, probe, stats, windows
from . import status as status_logic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("portal-api")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
POLL_ENABLED = os.environ.get("PORTAL_DISABLE_POLL") != "1"


class State:
    def __init__(self):
        self.catalog = {}
        self.categories = []
        self.last_run = None
        self.last_error = None
        self.refreshing = False
        self.next_run_at = None
        self.stats_cache = {}


state = State()


def _iso_now():
    return db.now_utc()


def _status_payload(status):
    return status or {
        "service_id": None,
        "slug": "",
        "name": "",
        "url": "",
        "state": "unknown",
        "last_state": "unknown",
        "state_since": None,
        "category": "",
        "code": None,
        "latency_ms": None,
        "checked_at": None,
        "error": None,
        "uptime_24h_pct": None,
        "uptime_24h": None,
    }


def _log_task(task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - el bucle ya registra; evita tareas mudas
        log.exception("tarea de sondeo falló")


async def run_checks(already_claimed=False):
    if not already_claimed:
        if state.refreshing:
            return None
        state.refreshing = True
    try:
        services = db.active_services()
        results = await probe.probe_all(services)
        entries = [
            (
                service["id"],
                result["ok"],
                result["code"],
                result["latency_ms"],
                result["error"],
                result["state"],
            )
            for service, result in zip(services, results)
        ]
        db.record_checks(entries)
        stats.record_entries(entries)
        db.rollup_checks()
        _report_transitions(db.latest_samples())
        state.stats_cache.clear()
        db.purge_old_checks()
        state.last_run = _iso_now()
        state.last_error = None
        db.set_meta("last_run", state.last_run)
        db.set_meta("last_error", "")
        log.info("chequeo completado: %s servicios", len(entries))
        return True
    except Exception as exc:  # noqa: BLE001 - el bucle decide el backoff
        state.last_error = f"{type(exc).__name__}: {exc}"[:200]
        try:
            db.set_meta("last_error", state.last_error)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo persistir last_error")
        log.exception("falló el ciclo de chequeo")
        return False
    finally:
        state.refreshing = False


def _report_transitions(statuses):
    """Publica caídas/recuperaciones y persiste estado e incidentes a la vez."""
    committed = db.get_meta_map("state_committed")
    since = db.get_meta_map("state_since")
    notified = set(db.get_meta_map("state_notified_offline"))
    mass_down = db.get_meta("state_mass_down") == "1"
    samples = db.recent_oks(max(status_logic.FAIL_THRESHOLD, status_logic.OK_THRESHOLD))
    services = {s["slug"]: s for s in state.catalog.get("services", [])}
    now = _iso_now()

    new_committed = {}
    new_since = {}
    down_slugs = []
    up_slugs = []
    for slug, st in statuses.items():
        old = committed.get(slug, "unknown")
        latest = st.get("last_state") or st.get("state") or "unknown"
        new = status_logic.advance(old, samples.get(slug, []), latest)
        new_committed[slug] = new
        if new != old:
            new_since[slug] = now
        else:
            new_since[slug] = since.get(slug) or st.get("checked_at") or now
        if new == "offline" and old != "offline":
            down_slugs.append(slug)
        elif old == "offline" and new in status_logic.UP_STATES:
            up_slugs.append(slug)

    offline_now = [slug for slug, value in new_committed.items() if value == "offline"]
    mass_min = max(1, int(os.environ.get("PORTAL_MASS_MIN", "3") or "3"))
    try:
        mass_ratio = float(os.environ.get("PORTAL_MASS_RATIO", "0.5"))
    except ValueError:
        mass_ratio = 0.5
    total = len(new_committed)
    threshold = max(mass_min, int(total * mass_ratio + 0.999)) if total else mass_min
    mass_now = len(offline_now) >= threshold

    actions = []
    for slug in down_slugs:
        st = statuses[slug]
        if st.get("service_id") is not None:
            actions.append(
                (
                    "open_service",
                    st["service_id"],
                    new_since[slug],
                    st.get("code"),
                    st.get("error"),
                    mass_now,
                )
            )
    for slug in up_slugs:
        st = statuses[slug]
        if st.get("service_id") is not None:
            actions.append(("close_service", st["service_id"], now))
    if mass_now and not mass_down:
        actions.append(("open_infra", now))
    elif mass_down and not mass_now:
        actions.append(("close_infra", now))

    events = []

    def publish(event_key, slug, title, body):
        service = services.get(slug) or {}
        events.append(
            (
                event_key,
                title,
                body,
                service.get("url", ""),
                slug,
                slug,
            )
        )

    if mass_now and not mass_down:
        events.append(
            (
                "infra.down",
                "Infraestructura sin respuesta",
                f"{len(offline_now)} servicios no responden; posible fallo de DNS/proxy.",
                "",
                "infra",
                None,
            )
        )
        mass_down = True
        notified.update(offline_now)
    elif mass_down and not mass_now:
        events.append(
            (
                "infra.recovered",
                "Infraestructura recuperada",
                "Los servicios vuelven a responder.",
                "",
                "infra",
                None,
            )
        )
        mass_down = False
        notified.difference_update(up_slugs)
    elif mass_down:
        notified.difference_update(up_slugs)
    else:
        for slug in down_slugs:
            st = statuses.get(slug) or {}
            publish(
                "service.down",
                slug,
                f"{services.get(slug, {}).get('name', slug)} no responde",
                st.get("error") or f"Sin respuesta ({st.get('code') or 'sin código'}).",
            )
            notified.add(slug)
        for slug in up_slugs:
            if slug not in notified:
                continue
            publish(
                "service.recovered",
                slug,
                f"{services.get(slug, {}).get('name', slug)} recuperado",
                "El servicio vuelve a responder.",
            )
            notified.discard(slug)

    active = set(new_committed)
    db.apply_transition(
        new_committed,
        new_since,
        {slug: 1 for slug in sorted(notified & active)},
        mass_now,
        actions,
    )
    for event_key, title, body, url, dedupe, scope in events:
        notify.publish(
            "index",
            event_key,
            title,
            body=body,
            url=url,
            dedupe_key=dedupe,
            scope=scope,
        )


async def _loop():
    backoff = 0
    while True:
        result = await run_checks()
        if result is None:
            await asyncio.sleep(1)
            continue
        if result:
            backoff = 0
            delay = POLL_INTERVAL
        else:
            backoff = POLL_INTERVAL if backoff == 0 else min(300, backoff * 2)
            delay = backoff
        state.next_run_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=delay)
        ).isoformat()
        await asyncio.sleep(delay)


def _get_stats(window):
    windows.validate_window(window)
    cached = state.stats_cache.get(window)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    payload = stats.compute(window, db.current_statuses())
    ttl = POLL_INTERVAL if window == "24h" else 60
    state.stats_cache[window] = (now + ttl, payload)
    return payload


@contextlib.asynccontextmanager
async def lifespan(app):
    db.init_db()
    raw_catalog = catalog_mod.load_catalog(db.CATALOG_PATH)
    state.catalog = catalog_mod.public_catalog(raw_catalog)
    state.categories = state.catalog.get("categories", [])
    db.sync_catalog(raw_catalog)
    state.last_run = db.get_meta("last_run")
    state.last_error = db.get_meta("last_error") or None
    state.next_run_at = _iso_now()
    stats.hydrate_latencies()
    db.rollup_checks()
    db.ensure_open_incidents(
        db.get_meta_map("state_committed"), db.get_meta_map("state_since")
    )
    task = None
    if POLL_ENABLED:
        task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="CortexDev Portal API", lifespan=lifespan)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        if request.url.path == "/api/catalog":
            response.headers.setdefault(
                "Cache-Control", "private, max-age=60, must-revalidate"
            )
        else:
            response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health():
    try:
        db.ping()
        version = db.schema_version()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "error", "db": str(exc)})
    return {
        "status": "ok",
        "db": "ok",
        "last_run": state.last_run,
        "last_error": state.last_error,
        "schema_version": version,
        "refreshing": state.refreshing,
        "next_check_at": state.next_run_at,
    }


@app.get("/api/catalog")
async def catalog(request: Request):
    body = json.dumps(
        state.catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=60, must-revalidate",
        "Vary": "Cookie",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@app.get("/api/portal")
async def portal():
    statuses = db.current_statuses()
    summary = _get_stats("24h")["summary"]
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
            counts[service["status"]["state"]] = (
                counts.get(service["status"]["state"], 0) + 1
            )
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
                    "ok"
                    if counts["offline"] == 0
                    else "degraded"
                    if counts["online"] + counts["auth"] > 0
                    else "down"
                ),
            }
        )

    return {
        "generated_at": _iso_now(),
        "last_run": state.last_run,
        "summary": summary,
        "categories": categories,
        "services": services,
    }


@app.get("/api/status")
async def status():
    payload = _get_stats("24h")
    return {
        "generated_at": _iso_now(),
        "last_run": state.last_run,
        "last_error": state.last_error,
        "refreshing": state.refreshing,
        "refresh_ms": POLL_INTERVAL * 1000,
        "next_check_at": state.next_run_at,
        "summary": payload["summary"],
        "window_effective": payload["window_effective"],
        "series": payload["series"],
        "statuses": db.current_statuses(),
    }


@app.get("/api/stats")
async def stats_endpoint(window: str = Query("24h")):
    try:
        return _get_stats(window)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/incidents")
async def incidents_endpoint(
    window: str = Query("7d"), limit: int = Query(50), kind: str | None = None
):
    try:
        return {"incidents": incidents.list_incidents(window, limit, kind)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/services/{slug}/incidents")
async def service_incidents(slug: str, limit: int = Query(50)):
    return {
        "slug": slug,
        "incidents": incidents.service_incidents(slug, limit),
    }


@app.post("/api/refresh")
async def refresh():
    if state.refreshing:
        return JSONResponse(
            status_code=202,
            content={
                "accepted": False,
                "last_run": state.last_run,
                "refreshing": True,
            },
        )
    state.refreshing = True
    task = asyncio.create_task(run_checks(already_claimed=True))
    task.add_done_callback(_log_task)
    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "last_run": state.last_run,
            "refreshing": True,
        },
    )


@app.get("/api/scopes")
async def scopes():
    titles = {c["slug"]: c.get("title", c["slug"]) for c in state.categories}
    return {
        "scopes": [
            {
                "slug": service["slug"],
                "name": service["name"],
                "group": service.get("category") or "",
                "group_title": titles.get(service.get("category") or "", ""),
            }
            for service in state.catalog.get("services", [])
        ]
    }
