"""Histéresis de estado de los servicios del portal (puro, sin dependencias).

Un solo sondeo fallido no debe marcar un servicio como caído ni generar avisos:
se exige una racha de fallos para bajar y una racha de éxitos para subir. El
estado "comprometido" resultante se persiste en la meta del portal y es el que
dispara las notificaciones (ver backend/app/main.py).

`oks` es la lista de los últimos sondeos en orden MÁS NUEVO PRIMERO (True = ok).
`latest_state` es el estado del chequeo más reciente (online|auth|offline|unknown),
que sólo se usa para distinguir "online" de "auth" al recuperarse.
"""
from __future__ import annotations

import os

OFFLINE = "offline"
UNKNOWN = "unknown"
UP_STATES = ("online", "auth")
VALID = (OFFLINE, UNKNOWN) + UP_STATES


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# Fallos consecutivos para declarar caída; éxitos consecutivos para recuperar.
FAIL_THRESHOLD = _env_int("PORTAL_FAIL_THRESHOLD", 3)
OK_THRESHOLD = _env_int("PORTAL_OK_THRESHOLD", 2)


def _streak(oks, want: bool) -> int:
    count = 0
    for ok in oks:
        if bool(ok) == want:
            count += 1
        else:
            break
    return count


def _up_state(latest_state: str) -> str:
    return latest_state if latest_state in UP_STATES else "online"


def advance(committed: str, oks, latest_state: str) -> str:
    """Nuevo estado comprometido a partir del anterior y los últimos sondeos."""
    if committed not in VALID:
        committed = UNKNOWN
    down_streak = _streak(oks, False)
    up_streak = _streak(oks, True)

    if committed == OFFLINE:
        if up_streak >= OK_THRESHOLD:
            return _up_state(latest_state)
        return OFFLINE

    if committed == UNKNOWN:
        if down_streak >= FAIL_THRESHOLD:
            return OFFLINE
        if up_streak >= 1:
            return _up_state(latest_state)
        return UNKNOWN

    # committed está arriba (online/auth): sólo baja con la racha de fallos.
    if down_streak >= FAIL_THRESHOLD:
        return OFFLINE
    return committed
