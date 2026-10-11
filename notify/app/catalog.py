"""Catálogo de áreas y eventos notificables (fuente única para las UIs)."""
from __future__ import annotations

# level por defecto de cada evento; el emisor puede subirlo (nunca bajarlo).
AREAS = {
    "copias": {
        "title": "Copias de seguridad",
        "events": {
            "incident.open": {"title": "Incidente abierto", "level": "critical"},
            "incident.recovered": {"title": "Incidente resuelto", "level": "info"},
            "schedule.drift": {"title": "Horario desviado", "level": "warning"},
            "nas.low": {"title": "NAS con poco espacio", "level": "warning"},
            "digest.daily": {"title": "Resumen diario", "level": "info"},
        },
    },
    "index": {
        "title": "Portal (estado de servicios)",
        "events": {
            "service.down": {"title": "Servicio caído", "level": "critical"},
            "service.recovered": {"title": "Servicio recuperado", "level": "info"},
        },
    },
    "ups": {
        "title": "UPS (energía)",
        "events": {
            "ups.on_battery": {"title": "Corte de energía", "level": "warning"},
            "ups.low_battery": {"title": "Batería baja", "level": "critical"},
            "ups.restored": {"title": "Energía restaurada", "level": "info"},
            "ups.comm_lost": {"title": "Sin comunicación con la UPS", "level": "critical"},
            "ups.comm_restored": {"title": "Comunicación restaurada", "level": "info"},
        },
    },
}

_LEVEL_RANK = {"info": 0, "warning": 1, "critical": 2}
_VALID_LEVELS = set(_LEVEL_RANK)


def areas() -> list[dict]:
    return [
        {
            "slug": slug,
            "title": area["title"],
            "events": [
                {"key": key, "title": meta["title"], "level": meta["level"]}
                for key, meta in area["events"].items()
            ],
        }
        for slug, area in AREAS.items()
    ]


def event_meta(area: str, event_key: str) -> dict | None:
    area_def = AREAS.get(area)
    if not area_def:
        return None
    return area_def["events"].get(event_key)


def normalize_level(value: str | None, default: str = "warning") -> str:
    level = (value or "").strip().lower()
    return level if level in _VALID_LEVELS else default


def meets_min(level: str, minimum: str) -> bool:
    return _LEVEL_RANK.get(level, 1) >= _LEVEL_RANK.get(minimum, 0)
