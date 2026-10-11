"""SSO: credenciales generales + cookie firmada compartida entre entornos."""
from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

log = logging.getLogger("notify")

_serializer: URLSafeTimedSerializer | None = None


def _ser() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.AUTH_SECRET_KEY, salt=config.AUTH_SALT)
    return _serializer


def issue_session(username: str, role: str) -> str:
    """Emite la cookie firmada compartida (mismo serializer que la verifica)."""
    return _ser().dumps({"u": username, "r": role})


def configured() -> bool:
    return bool(config.AUTH_SECRET_KEY)


def login_configured() -> bool:
    """Login central operativo: hay secreto de cookie y credenciales generales."""
    return bool(config.AUTH_SECRET_KEY and config.AUTH_GENERAL_USER
                and config.AUTH_GENERAL_PASSWORD)


def check_credentials(username: str, password: str) -> bool:
    """Valida las credenciales generales del login central (comparación segura).

    Se comparan bytes utf-8: `compare_digest` sólo acepta str ASCII y lanza
    TypeError con caracteres no-ASCII (500 evitable).
    """
    if not login_configured():
        return False
    return (
        secrets.compare_digest((username or "").encode("utf-8"),
                               config.AUTH_GENERAL_USER.encode("utf-8"))
        and secrets.compare_digest((password or "").encode("utf-8"),
                                   config.AUTH_GENERAL_PASSWORD.encode("utf-8"))
    )


def read_session(token: str):
    if not token or not configured():
        return None
    try:
        return _ser().loads(token, max_age=config.AUTH_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def session_user(request: Request):
    data = read_session(request.cookies.get(config.AUTH_COOKIE, ""))
    if not data:
        return None
    return {"username": data.get("u") or "anonymous", "role": data.get("r") or "viewer"}


def require_user(request: Request) -> dict:
    """Dependencia de FastAPI: exige sesión.

    Falla en cerrado: si el SSO no está configurado (falta AUTH_SECRET_KEY) no
    se puede validar la cookie, así que se deniega (503) en vez de permitir el
    acceso anónimo a los endpoints protegidos.
    """
    if not configured():
        log.error("AUTH_SECRET_KEY vacío: endpoints protegidos denegados (503)")
        raise HTTPException(status_code=503, detail="SSO no configurado")
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="no autenticado")
    return user
