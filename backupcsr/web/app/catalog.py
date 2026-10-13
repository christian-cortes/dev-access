"""Catálogo de jobs: lee jobs.yml y lo sincroniza con la tabla `jobs`.

Regla de sincronización (igual que backend/catalog.yml del portal):
- Los jobs nuevos se insertan con el horario y la ficha por defecto del YAML.
- Los existentes NO se pisan: la BD manda tras el alta (ni `enabled`/cron, ni la ficha).
  Los campos nuevos de la ficha (criticality, owner, retention_days, tags,
  size_exclude, notes) se siembran del YAML solo si están vacíos, para rellenar
  instalaciones que vienen de un esquema anterior sin pisar ediciones.
- Los jobs que ya no están en el YAML se desactivan (enabled=0), sin borrar histórico.
"""
from __future__ import annotations

import logging
import os
import re

import yaml

from . import config, db

log = logging.getLogger("backupcsr-web")

DEFAULT_CRON = {"minute": "20", "hour": "6-19", "dom": "*", "month": "*", "dow": "*"}

CRITICALITIES = ("alta", "media", "baja")
ORIGIN_TYPES = ("ftp", "sftp", "sftp_pass")

# Slug válido: minúsculas, dígitos y guiones; debe empezar/terminar alfanumérico.
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
SLUG_MAX = 64


def split_list(value) -> list[str]:
    """Normaliza lista de YAML/BD (lista, o texto separado por comas) a lista limpia."""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(",", " ").split()
    else:
        raw = list(value)
    return [str(item).strip() for item in raw if str(item).strip()]


def join_list(value, limit: int = 512) -> str:
    return ",".join(split_list(value))[:limit]


def load_catalog() -> dict:
    """Catálogo base (jobs.yml) fusionado con el overlay durable de /etc.

    El overlay (una tarea por archivo en JOBS_OVERLAY_DIR) añade/gana: es donde
    viven las tareas creadas desde el portal, que deben sobrevivir a un reinstall
    (install.sh sobrescribe /opt/backupcsr/web/jobs.yml).
    """
    base = {"settings": {}, "jobs": []}
    if config.CATALOG_PATH.exists():
        with open(config.CATALOG_PATH, "r", encoding="utf-8") as handle:
            base = yaml.safe_load(handle) or {"settings": {}, "jobs": []}
    else:
        log.error("no existe el catálogo de jobs en %s", config.CATALOG_PATH)
    base.setdefault("settings", {})
    base.setdefault("jobs", [])

    overlay = _load_overlay()
    if not overlay:
        return base
    merged: dict[str, dict] = {}
    for item in base["jobs"]:
        if isinstance(item, dict) and item.get("slug"):
            merged[item["slug"]] = item
    for item in overlay:
        merged[item["slug"]] = item
    base["jobs"] = list(merged.values())
    return base


def _load_overlay() -> list[dict]:
    """Items del overlay /etc/backupcsr/jobs.d/*.yml (ignora los ilegibles)."""
    directory = config.JOBS_OVERLAY_DIR
    if not directory.exists():
        return []
    items: list[dict] = []
    for path in sorted(directory.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("overlay de tareas ilegible %s: %s", path, exc)
            continue
        item = data.get("job") if isinstance(data, dict) and "job" in data else data
        if isinstance(item, dict) and item.get("slug"):
            item.setdefault("managed_by", "portal")
            items.append(item)
    return items


def overlay_path(slug: str):
    return config.JOBS_OVERLAY_DIR / f"{slug}.yml"


def valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= SLUG_MAX and bool(SLUG_RE.match(slug))


def load_overlay(slug: str) -> dict | None:
    """Definición completa (incluidas `routes`) de una tarea del portal, o None."""
    path = overlay_path(slug)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    item = data.get("job") if isinstance(data, dict) and "job" in data else data
    if isinstance(item, dict):
        item.setdefault("managed_by", "portal")
        return item
    return None


def is_managed(slug: str) -> bool:
    return overlay_path(slug).is_file()


def save_overlay(item: dict) -> None:
    """Escribe la definición de una tarea del portal de forma atómica (0600)."""
    slug = item["slug"]
    if not valid_slug(slug):
        raise ValueError(f"slug inválido: {slug}")
    directory = config.JOBS_OVERLAY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    payload = {"job": {**item, "managed_by": "portal"}}
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    tmp = directory / f".{slug}.yml.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, overlay_path(slug))
    log.info("overlay de tarea guardado: %s", slug)


def delete_overlay(slug: str) -> bool:
    path = overlay_path(slug)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        log.warning("no se pudo borrar el overlay %s: %s", path, exc)
        return False
    return True


def _size_exclude(item: dict) -> list[str]:
    """Patrones (nombres de hijo directo del destino) a excluir del cálculo de tamaño."""
    return [value.strip("/") for value in split_list(item.get("size_exclude"))]


def catalog_jobs() -> list[dict]:
    """Jobs del YAML normalizados a la forma de la tabla `jobs` (sin BD)."""
    catalog = load_catalog()
    defaults = catalog.get("settings", {})
    out = []
    for item in catalog.get("jobs", []):
        cron = {**DEFAULT_CRON}
        nested = item.get("cron")
        if isinstance(nested, dict):
            cron.update(nested)
        # El overlay guarda el horario aplanado (cron_minute, cron_hour…): manda sobre
        # el `cron` anidado y sobre los valores por defecto.
        for key, field in (("minute", "cron_minute"), ("hour", "cron_hour"),
                           ("dom", "cron_dom"), ("month", "cron_month"), ("dow", "cron_dow")):
            if item.get(field) is not None:
                cron[key] = item[field]
        out.append(
            {
                "slug": item["slug"],
                "name": item.get("name", item["slug"]),
                "description": item.get("description", "") or "",
                "origin_type": item.get("origin_type", "ftp"),
                "origin_host": item.get("origin_host", "") or "",
                "source": item.get("source", "") or "",
                "dest_rel": item.get("dest_rel", "") or "",
                "lockfile": item.get("lockfile", "") or "",
                "enabled": int(item.get("enabled", defaults.get("default_enabled", True))),
                "cron_minute": str(cron["minute"]),
                "cron_hour": str(cron["hour"]),
                "cron_dom": str(cron.get("dom", "*")),
                "cron_month": str(cron.get("month", "*")),
                "cron_dow": str(cron.get("dow", "*")),
                "sort": int(item.get("sort", 100)),
                "size_exclude": _size_exclude(item),
                "criticality": str(item.get("criticality", "media") or "media"),
                "owner": item.get("owner", "") or "",
                "retention_days": item.get("retention_days"),
                "tags": join_list(item.get("tags"), 255),
                "notes": item.get("notes", "") or "",
                "routes": _routes(item),
                "managed_by": item.get("managed_by") or "",
            }
        )
    return out


def _routes(item: dict) -> list[dict]:
    """Rutas origen→destino de una tarea; si no vienen, se deriva source/dest_rel."""
    raw = item.get("routes")
    out: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("source"):
                out.append({
                    "source": str(entry.get("source", "")).strip(),
                    "dest_rel": str(entry.get("dest_rel", "")).strip(),
                })
    if out:
        return out
    source = str(item.get("source", "") or "").strip()
    dest = str(item.get("dest_rel", "") or "").strip()
    if source and dest:
        # Tareas multi-ruta del repo aplanan el origen con " + ": no se puede
        # reconstruir la correspondencia exacta, así que se deja como una sola ruta.
        return [{"source": source, "dest_rel": dest}]
    return []


def sync_jobs() -> int:
    jobs = catalog_jobs()
    if not jobs:
        return 0
    slugs = [job["slug"] for job in jobs]
    for job in jobs:
        params = {**job, "size_exclude": join_list(job.get("size_exclude"))}
        db.execute(
            """
            INSERT INTO jobs (
                slug, name, description, origin_type, origin_host, source, dest_rel, lockfile,
                enabled, cron_minute, cron_hour, cron_dom, cron_month, cron_dow, sort,
                criticality, owner, retention_days, tags, size_exclude, notes
            ) VALUES (
                %(slug)s, %(name)s, %(description)s, %(origin_type)s, %(origin_host)s, %(source)s,
                %(dest_rel)s, %(lockfile)s, %(enabled)s, %(cron_minute)s, %(cron_hour)s,
                %(cron_dom)s, %(cron_month)s, %(cron_dow)s, %(sort)s,
                %(criticality)s, %(owner)s, %(retention_days)s, %(tags)s,
                %(size_exclude)s, %(notes)s
            )
            ON DUPLICATE KEY UPDATE
                origin_host = COALESCE(NULLIF(origin_host, ''), VALUES(origin_host)),
                criticality = COALESCE(NULLIF(criticality, ''), VALUES(criticality)),
                owner = COALESCE(NULLIF(owner, ''), VALUES(owner)),
                retention_days = COALESCE(retention_days, VALUES(retention_days)),
                tags = COALESCE(NULLIF(tags, ''), VALUES(tags)),
                size_exclude = COALESCE(NULLIF(size_exclude, ''), VALUES(size_exclude)),
                notes = COALESCE(NULLIF(notes, ''), VALUES(notes))
            """,
            params,
        )
    placeholders = ",".join(["%s"] * len(slugs))
    db.execute(f"UPDATE jobs SET enabled = 0 WHERE slug NOT IN ({placeholders})", slugs)
    log.info("catálogo sincronizado: %s jobs", len(jobs))
    return len(jobs)


def upsert_job(job: dict) -> None:
    """Inserta o actualiza por completo la fila de BD de una tarea del portal.

    A diferencia de `sync_jobs` (que no pisa ediciones), este camino SÍ sobrescribe
    todos los campos: es la definición autoritativa que el portal acaba de validar.
    """
    params = {**job, "size_exclude": join_list(job.get("size_exclude"), 512)}
    db.execute(
        """
        INSERT INTO jobs (
            slug, name, description, origin_type, origin_host, source, dest_rel, lockfile,
            enabled, cron_minute, cron_hour, cron_dom, cron_month, cron_dow, sort,
            criticality, owner, retention_days, tags, size_exclude, notes
        ) VALUES (
            %(slug)s, %(name)s, %(description)s, %(origin_type)s, %(origin_host)s, %(source)s,
            %(dest_rel)s, %(lockfile)s, %(enabled)s, %(cron_minute)s, %(cron_hour)s,
            %(cron_dom)s, %(cron_month)s, %(cron_dow)s, %(sort)s,
            %(criticality)s, %(owner)s, %(retention_days)s, %(tags)s,
            %(size_exclude)s, %(notes)s
        )
        ON DUPLICATE KEY UPDATE
            name = VALUES(name), description = VALUES(description),
            origin_type = VALUES(origin_type), origin_host = VALUES(origin_host),
            source = VALUES(source), dest_rel = VALUES(dest_rel), lockfile = VALUES(lockfile),
            enabled = VALUES(enabled), cron_minute = VALUES(cron_minute),
            cron_hour = VALUES(cron_hour), cron_dom = VALUES(cron_dom),
            cron_month = VALUES(cron_month), cron_dow = VALUES(cron_dow),
            sort = VALUES(sort), criticality = VALUES(criticality), owner = VALUES(owner),
            retention_days = VALUES(retention_days), tags = VALUES(tags),
            size_exclude = VALUES(size_exclude), notes = VALUES(notes)
        """,
        params,
    )


def set_enabled(slug: str, enabled: bool) -> None:
    db.execute("UPDATE jobs SET enabled = %s WHERE slug = %s", (1 if enabled else 0, slug))


def reset_schedule(slug: str) -> None:
    """Restaura el horario por defecto del YAML para un job."""
    for job in catalog_jobs():
        if job["slug"] == slug:
            db.execute(
                """
                UPDATE jobs SET cron_minute=%(cron_minute)s, cron_hour=%(cron_hour)s,
                    cron_dom=%(cron_dom)s, cron_month=%(cron_month)s, cron_dow=%(cron_dow)s
                WHERE slug=%(slug)s
                """,
                job,
            )
            return
    raise KeyError(slug)
