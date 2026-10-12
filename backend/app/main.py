import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db, notify, probe
from . import status as status_logic

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
        await asyncio.to_thread(_report_transitions, db.latest_statuses())
        state.last_run = db.now_utc()
        db.set_meta("last_run", state.last_run)
        log.info("chequeo completado: %s servicios", len(entries))
    except Exception:
        log.exception("fallo el ciclo de chequeo")
    finally:
        state.refreshing = False


def _load_meta_map(key):
    raw = db.get_meta(key)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _report_transitions(statuses):
    """Publica caídas/recuperaciones con histéresis y agrupa las caídas masivas.

    El estado "comprometido" (con rachas) se persiste en `state_committed`, así
    que reinicios no re-notifican. Una recuperación solo se publica si antes se
    publicó la caída de ese servicio (`state_notified_offline`). Si caen muchos
    servicios a la vez (fallo de DNS/ingress) se emite un único `infra.down`.
    """
    committed = _load_meta_map("state_committed")
    notified = set(_load_meta_map("state_notified_offline"))
    mass_down = db.get_meta("state_mass_down") == "1"

    samples = db.recent_oks(max(status_logic.FAIL_THRESHOLD, status_logic.OK_THRESHOLD))
    services = {s["slug"]: s for s in state.catalog.get("services", [])}

    new_committed = {}
    down_slugs = []
    up_slugs = []
    for slug, st in statuses.items():
        latest = st.get("state") or "unknown"
        now = status_logic.advance(committed.get(slug, "unknown"), samples.get(slug, []), latest)
        new_committed[slug] = now
        old = committed.get(slug, "unknown")
        if now == "offline" and old != "offline":
            down_slugs.append(slug)
        elif old == "offline" and now in status_logic.UP_STATES:
            up_slugs.append(slug)

    offline_now = [slug for slug, state in new_committed.items() if state == "offline"]
    mass_min = max(1, int(os.environ.get("PORTAL_MASS_MIN", "3") or "3"))
    try:
        mass_ratio = float(os.environ.get("PORTAL_MASS_RATIO", "0.5"))
    except ValueError:
        mass_ratio = 0.5
    total = len(new_committed)
    threshold = max(mass_min, int(total * mass_ratio + 0.999)) if total else mass_min
    mass_now = len(offline_now) >= threshold

    def _publish_svc(event_key, slug, title, body):
        svc = services.get(slug) or {}
        notify.publish("index", event_key, title, body=body,
                       url=svc.get("url", ""), dedupe_key=slug, scope=slug)

    if mass_now and not mass_down:
        notify.publish(
            "index", "infra.down", "Infraestructura sin respuesta",
            body=f"{len(offline_now)} servicios no responden; posible fallo de DNS/proxy.",
            dedupe_key="infra",
        )
        mass_down = True
        # El aviso agregado cubre a los que están caídos ahora.
        notified.update(offline_now)
    elif mass_down and not mass_now:
        notify.publish(
            "index", "infra.recovered", "Infraestructura recuperada",
            body="Los servicios vuelven a responder.", dedupe_key="infra",
        )
        mass_down = False
        # Los que se recuperan aquí quedaron cubiertos por el aviso agregado;
        # los que sigan caídos conservan su "caída" para avisar su recuperación.
        notified.difference_update(up_slugs)
    elif mass_down:
        # Sigue la caída masiva: silencio por servicio. Los que se caen durante
        # la caída masiva NO se marcan como avisados (no hubo caída por servicio),
        # así no se emite luego una recuperación sin caída.
        notified.difference_update(up_slugs)
    else:
        for slug in down_slugs:
            st = statuses.get(slug) or {}
            body = st.get("error") or f"Sin respuesta ({st.get('code') or 'sin código'})."
            _publish_svc("service.down", slug, f"{services.get(slug, {}).get('name', slug)} no responde", body)
            notified.add(slug)
        for slug in up_slugs:
            if slug not in notified:
                continue
            _publish_svc(
                "service.recovered", slug,
                f"{services.get(slug, {}).get('name', slug)} recuperado",
                "El servicio vuelve a responder.",
            )
            notified.discard(slug)

    active_slugs = set(new_committed)
    db.set_meta("state_committed",
                json.dumps({s: v for s, v in new_committed.items() if s in active_slugs}))
    db.set_meta("state_notified_offline", json.dumps({s: 1 for s in sorted(notified & active_slugs)}))
    db.set_meta("state_mass_down", "1" if mass_down else "0")


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


@app.get("/api/scopes")
async def scopes():
    """Lista ligera de proyectos/servicios para configurar avisos por proyecto (área index)."""
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
