"""Catálogo de áreas y eventos notificables (fuente única para las UIs)."""
from __future__ import annotations

# level por defecto de cada evento; el emisor puede subirlo (nunca bajarlo).
# `scoped`: el área admite activar/desactivar avisos por proyecto/tarea (slug).
# `short`: nombre corto que precede al título de la notificación push.
# `icon`: favicon del sitio (URL absoluta y pública) que muestra la notificación.
AREAS = {
    "copias": {
        "title": "Copias de seguridad",
        "short": "Copias Cortex",
        "icon": "https://copias.cortexdev.win/favicon.png?v=2",
        "scoped": True,
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
        "short": "Portal Cortex",
        "icon": "https://index.cortexdev.win/favicons/cortexdev-32.png?v=2",
        "scoped": True,
        "events": {
            "service.down": {"title": "Servicio caído", "level": "critical"},
            "service.recovered": {"title": "Servicio recuperado", "level": "info"},
            "infra.down": {"title": "Infraestructura sin respuesta", "level": "critical"},
            "infra.recovered": {"title": "Infraestructura recuperada", "level": "info"},
        },
    },
    "ups": {
        "title": "UPS (energía)",
        "short": "UPS Cortex",
        "icon": "https://ups.cortexdev.win/favicon.png?v=2",
        "scoped": False,
        "events": {
            "ups.on_battery": {"title": "Corte de energía", "level": "warning"},
            "ups.low_battery": {"title": "Batería baja", "level": "critical"},
            "ups.restored": {"title": "Energía restaurada", "level": "info"},
            "ups.comm_lost": {"title": "Sin comunicación con la UPS", "level": "critical"},
            "ups.comm_restored": {"title": "Comunicación restaurada", "level": "info"},
        },
    },
    "manager": {
        "title": "Docker Manager",
        "short": "Docker Manager",
        "icon": "https://manager.cortexdev.win/favicon.png?v=2",
        "scoped": True,
        "events": {
            "deploy.failed": {"title": "Despliegue fallido", "level": "critical"},
            "deploy.ok": {"title": "Despliegue completado", "level": "info"},
            "rollback.done": {"title": "Rollback aplicado", "level": "warning"},
            "drift.detected": {"title": "Drift detectado", "level": "warning"},
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
            "short": area.get("short") or area["title"],
            "icon": area.get("icon") or "",
            "scoped": bool(area.get("scoped")),
            "events": [
                {"key": key, "title": meta["title"], "level": meta["level"]}
                for key, meta in area["events"].items()
            ],
        }
        for slug, area in AREAS.items()
    ]


def area_short(area: str) -> str:
    """Nombre corto del área para el título de las notificaciones."""
    block = AREAS.get(area) or {}
    return block.get("short") or block.get("title") or area


def area_icon(area: str) -> str:
    """URL absoluta del favicon del sitio (pública) para la notificación."""
    return (AREAS.get(area) or {}).get("icon") or ""


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
