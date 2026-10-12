"""Servicio central: login único, SSO verify, FCM/correo y preferencias por sitio."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from . import catalog, channels, config, db
from .auth import (issue_session, login_configured, require_user, session_user,
                   configured as sso_configured)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notify")


async def _maintenance():
    while True:
        try:
            await asyncio.to_thread(db.prune)
        except Exception:  # noqa: BLE001
            log.exception("falló la limpieza de eventos")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    if not sso_configured():
        log.error("AUTH_SECRET_KEY vacío: el SSO denegará el acceso (pon el mismo "
                  "valor que BACKUP_SECRET_KEY de copias).")
    if not login_configured():
        log.error("AUTH_GENERAL_USER/PASSWORD vacíos: el login central responderá 503.")
    task = asyncio.create_task(_maintenance())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CortexDev Notify", lifespan=lifespan)


# ------------------------------- health -----------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sso": sso_configured(),
        "login": login_configured(),
        "fcm": channels.fcm_ready(),
        "email": channels.mail_ready(),
    }


# ------------------------------ SSO verify --------------------------------
# Uso interno (nginx auth_request). No expone datos: solo 200/401.

@app.get("/api/auth/verify")
async def auth_verify(request: Request):
    if not sso_configured():
        # Falla en cerrado: sin secreto no se puede validar la cookie, así que se
        # deniega (503) en lugar de permitir el acceso anónimo a las webs.
        log.error("AUTH_SECRET_KEY vacío: verificación SSO denegada (503)")
        return Response(status_code=503)
    user = session_user(request)
    if not user:
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Auth-User": user["username"]})


# ------------------------------ SSO login ---------------------------------
# Login ÚNICO del índice: valida las credenciales generales (AUTH_GENERAL_*) y
# emite la cookie firmada con Domain=.cortexdev.win para que valga en copias,
# ups y demás entornos. Copias ya no tiene login propio.
#
# La página de origen no viaja en la URL: nginx la guarda en la cookie
# AUTH_NEXT_COOKIE al redirigir al login y el backend la resuelve aquí.

class LoginIn(BaseModel):
    username: str
    password: str


def _safe_next(value: str) -> str:
    """Destino tras el login: ruta relativa o host *.cortexdev.win, nunca externo.

    Descarta destinos que son subrecursos (el navegador pide favicon/JS/CSS sin
    sesión y no deben ser a donde vuelve el usuario tras entrar).
    """
    from urllib.parse import unquote, urlparse

    # El frontend puede escribir la cookie percent-encoded (encodeURIComponent).
    raw = unquote((value or "").strip())
    if not raw:
        return "/"

    def _es_asset(path: str) -> bool:
        limpio = (path or "").split("?", 1)[0].split("#", 1)[0].lower()
        return limpio.endswith((
            ".ico", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
            ".css", ".js", ".json", ".map", ".woff", ".woff2",
            ".ttf", ".eot", ".txt", ".xml",
        ))

    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return "/" if _es_asset(raw) else raw
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        if parsed.scheme == "https" and (host == "cortexdev.win" or host.endswith(".cortexdev.win")):
            return "/" if _es_asset(parsed.path) else raw
    except ValueError:
        pass
    return "/"


@app.post("/api/auth/login")
async def auth_login(payload: LoginIn, request: Request, response: Response):
    if not login_configured():
        raise HTTPException(status_code=503, detail="login central no configurado")
    from .auth import check_credentials

    if not check_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="usuario o contraseña inválidos")

    token = issue_session(config.AUTH_GENERAL_USER, config.AUTH_GENERAL_ROLE)
    response.set_cookie(
        config.AUTH_COOKIE,
        token,
        max_age=config.AUTH_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        domain=config.AUTH_COOKIE_DOMAIN or None,
    )
    next_url = _safe_next(request.cookies.get(config.AUTH_NEXT_COOKIE, ""))
    response.delete_cookie(
        config.AUTH_NEXT_COOKIE, path="/", domain=config.AUTH_COOKIE_DOMAIN or None
    )
    return {"username": config.AUTH_GENERAL_USER, "role": config.AUTH_GENERAL_ROLE, "next": next_url}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(config.AUTH_COOKIE, path="/", domain=config.AUTH_COOKIE_DOMAIN or None)
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="no autenticado")
    return user


# ------------------------------- push -------------------------------------

class SubscribeIn(BaseModel):
    area: str
    token: str
    platform: str = ""


class UnsubscribeIn(BaseModel):
    id: str


class PreferencesIn(BaseModel):
    area: str
    prefs: dict


class ScopeItem(BaseModel):
    scope: str
    enabled: bool = True


class ScopesIn(BaseModel):
    area: str
    scopes: list[ScopeItem] = []


class TestIn(BaseModel):
    area: str


@app.get("/api/push/fcm-config")
async def push_fcm_config():
    """Config pública de FCM (sin datos de usuario) para el service worker.

    El SW no puede depender de un endpoint autenticado: si falla queda inerte y
    no muestra notificaciones en segundo plano. Aquí solo van identificadores
    públicos y la VAPID key (no son secretos).
    """
    return {"firebase": config.FIREBASE_PUBLIC, "vapid_key": config.FIREBASE_VAPID_KEY}


@app.get("/api/push/config")
async def push_config(user: dict = Depends(require_user)):
    return {
        "firebase": config.FIREBASE_PUBLIC,
        "vapid_key": config.FIREBASE_VAPID_KEY,
        "areas": catalog.areas(),
        "login_url": config.LOGIN_URL,
        "user": user["username"],
    }


@app.post("/api/push/subscribe")
async def push_subscribe(payload: SubscribeIn, user: dict = Depends(require_user)):
    if payload.area not in catalog.AREAS:
        raise HTTPException(status_code=400, detail="area inválida")
    if len(payload.token) < 20 or len(payload.token) > 4096:
        raise HTTPException(status_code=400, detail="token inválido")
    sub_id = uuid.uuid4().hex
    db.upsert_subscription(sub_id, payload.area, payload.token, user["username"], payload.platform)
    return {"id": sub_id, "area": payload.area}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(payload: UnsubscribeIn, user: dict = Depends(require_user)):
    db.delete_subscription(payload.id, user["username"])
    return {"ok": True}


@app.get("/api/push/preferences")
async def push_preferences(user: dict = Depends(require_user)):
    return {
        "prefs": db.all_user_prefs(user["username"]),
        "scopes": {area: db.get_scope_prefs(user["username"], area) for area in catalog.AREAS},
    }


@app.put("/api/push/preferences")
async def push_set_preferences(payload: PreferencesIn, user: dict = Depends(require_user)):
    if payload.area not in catalog.AREAS:
        raise HTTPException(status_code=400, detail="area inválida")
    return {"prefs": db.set_prefs(user["username"], payload.area, payload.prefs or {})}


@app.put("/api/push/scopes")
async def push_set_scopes(payload: ScopesIn, user: dict = Depends(require_user)):
    """Activa/desactiva avisos por proyecto/tarea (solo on/off; el resto hereda del área)."""
    if payload.area not in catalog.AREAS:
        raise HTTPException(status_code=400, detail="area inválida")
    items = {
        item.scope.strip()[:128]: item.enabled
        for item in payload.scopes
        if item.scope and item.scope.strip()
    }
    return {"scopes": db.set_scope_prefs(user["username"], payload.area, items)}


@app.post("/api/push/test")
async def push_test(payload: TestIn, user: dict = Depends(require_user)):
    if payload.area not in catalog.AREAS:
        raise HTTPException(status_code=400, detail="area inválida")
    return await asyncio.to_thread(channels.dispatch_test, user["username"], payload.area)


# ------------------------- ingesta de eventos -----------------------------
# Solo para las apps (loopback + token). Nunca se expone vía nginx.

class EventIn(BaseModel):
    area: str
    event_key: str
    title: str
    body: str = ""
    level: str | None = None
    scope: str = ""
    url: str = ""
    dedupe_key: str = ""


@app.post("/api/events")
async def ingest_event(payload: EventIn, authorization: str = Header(default="")):
    if not config.INGEST_TOKEN:
        raise HTTPException(status_code=503, detail="ingesta deshabilitada")
    if not secrets.compare_digest(
            authorization.encode("utf-8"),
            f"Bearer {config.INGEST_TOKEN}".encode("utf-8")):
        raise HTTPException(status_code=401, detail="token inválido")
    if payload.area not in catalog.AREAS:
        raise HTTPException(status_code=400, detail="area inválida")
    meta = catalog.event_meta(payload.area, payload.event_key)
    if meta is None:
        raise HTTPException(status_code=400, detail="evento inválido")
    level = catalog.normalize_level(payload.level, meta["level"])
    if not catalog.meets_min(level, meta["level"]):
        level = meta["level"]  # el emisor puede subir, nunca bajar
    if payload.dedupe_key and await asyncio.to_thread(
            db.recent_dedupe,
            f"{payload.area}:{payload.event_key}:{payload.dedupe_key}",
            config.DEDUPE_WINDOW):
        return {"ok": True, "deduped": True}
    scope = (payload.scope or "").strip()[:128]
    event_id = await asyncio.to_thread(
        db.insert_event, payload.area, payload.event_key, level,
        payload.title, payload.body, payload.url,
        f"{payload.area}:{payload.event_key}:{payload.dedupe_key}" if payload.dedupe_key else "",
        scope,
    )
    event = {
        "id": event_id, "area": payload.area, "event_key": payload.event_key,
        "level": level, "title": payload.title, "body": payload.body, "url": payload.url,
        "scope": scope,
    }
    stats = await asyncio.to_thread(channels.dispatch, event)
    return {"ok": True, "id": event_id, "stats": stats}
