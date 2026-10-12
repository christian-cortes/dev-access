"""Endpoints HTTP del portal de copias (prefijo /api)."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import auth, batch, catalog, config, cronfile, db, files, logs, runner, sizes

log = logging.getLogger("backupcsr-web")

router = APIRouter(prefix="/api")

CRON_FIELD_RE = re.compile(r"^[0-9*/,\-]+$")
DEST_REL_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
LOCKFILE_RE = re.compile(r"^/run/lock/[A-Za-z0-9._/-]+$")
JOB_FIELDS = (
    "slug",
    "name",
    "description",
    "origin_type",
    "origin_host",
    "source",
    "dest_rel",
    "lockfile",
    "enabled",
    "cron_minute",
    "cron_hour",
    "cron_dom",
    "cron_month",
    "cron_dow",
    "sort",
    "criticality",
    "owner",
    "retention_days",
    "tags",
    "size_exclude",
    "notes",
)
# Campos que cambian el disparo real: exigen BACKUP_MANAGE_CRON y reescriben el cron.
CRON_FIELDS = (
    "enabled",
    "lockfile",
    "cron_minute",
    "cron_hour",
    "cron_dom",
    "cron_month",
    "cron_dow",
)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
ROLES = ("admin", "viewer")
MIN_PASSWORD = 8


class LoginIn(BaseModel):
    username: str
    password: str


class RunIn(BaseModel):
    dry_run: bool = False
    retry: bool = False


class BatchIn(BaseModel):
    action: str
    slugs: list[str] = []


class JobPatch(BaseModel):
    # Programación: exige BACKUP_MANAGE_CRON (reescribe /etc/cron.d/backupcsr).
    enabled: bool | None = None
    lockfile: str | None = None
    cron_minute: str | None = None
    cron_hour: str | None = None
    cron_dom: str | None = None
    cron_month: str | None = None
    cron_dow: str | None = None
    # Ficha: solo exige rol admin.
    name: str | None = None
    description: str | None = None
    origin_type: str | None = None
    origin_host: str | None = None
    source: str | None = None
    dest_rel: str | None = None
    sort: int | None = None
    criticality: str | None = None
    owner: str | None = None
    retention_days: int | None = None
    tags: str | None = None
    size_exclude: str | None = None
    notes: str | None = None


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None


class PasswordIn(BaseModel):
    password: str


class SelfPasswordIn(BaseModel):
    current: str
    new: str


class FilesMkdirIn(BaseModel):
    path: str = ""
    name: str


class FilesRenameIn(BaseModel):
    path: str
    name: str


class FilesMoveIn(BaseModel):
    path: str
    dest: str = ""


class FilesDeleteIn(BaseModel):
    path: str
    confirm: str = ""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _schedule(job: dict) -> dict:
    return {
        "minute": job.get("cron_minute", "0"),
        "hour": job.get("cron_hour", "0"),
        "dom": job.get("cron_dom", "*"),
        "month": job.get("cron_month", "*"),
        "dow": job.get("cron_dow", "*"),
    }


def _same_schedule(a: dict, b: dict) -> bool:
    return all(
        str(a.get(field, "")) == str(b.get(field, ""))
        for field in ("minute", "hour", "dom", "month", "dow")
    )


def _effective_schedule(job: dict) -> dict:
    """Horario que realmente dispara: el del cron instalado; si no, el de la BD.

    El cron es la fuente de verdad para el estado y la próxima ejecución: es lo
    que cron ejecuta. La BD es el horario editable del portal y puede estar
    desviada (ver `schedule_drift`).
    """
    return job.get("installed_cron") or _schedule(job)


def _cron_valid_field(value: str) -> bool:
    return bool(CRON_FIELD_RE.match(value.strip()))


def _valid_dest_rel(value: str) -> bool:
    value = value.strip()
    if not value or len(value) > 255 or value.startswith("/"):
        return False
    if ".." in value.split("/"):
        return False
    return bool(DEST_REL_RE.match(value))


def _valid_lockfile(value: str) -> bool:
    if not value or len(value) > 255 or not value.startswith("/run/lock/"):
        return False
    # Sin '..' ni componentes vacíos: el lockfile se abre/crea como root, así que no
    # puede escapar de /run/lock ni colarse por rutas equivalentes en la unicidad.
    if any(part in ("", ".", "..") for part in value.split("/")[3:]):
        return False
    return bool(LOCKFILE_RE.match(value))


def _script_text(slug: str) -> str | None:
    """Contenido de /opt/backupcsr/jobs/<slug>.sh, o None si no existe/legible."""
    path = config.JOBS_DIR / f"{slug}.sh"
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _script_drift(job: dict, text: str | None) -> bool:
    """Aviso best-effort: algún token de source/dest_rel no aparece en el .sh.

    En tareas multi-ruta puede marcar de más: solo informa, no bloquea.
    """
    if not text:
        return False
    # Solo el código: las rutas citadas en comentarios (p. ej. la cabecera de
    # ruta56-web.sh) darían un falso "coincide" si el comando real cambiara.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    tokens = set()
    for value in (job.get("source"), job.get("dest_rel")):
        for token in re.split(r"[+\s]+", str(value or "")):
            token = token.strip()
            if token:
                tokens.add(token)
    return any(token not in code for token in tokens)


def effective_jobs() -> list[dict]:
    """Catálogo + overrides de la BD; si no hay BD, usa el cron instalado."""
    jobs = catalog.catalog_jobs()
    db_rows = {}
    if db.available():
        try:
            for row in db.query("SELECT * FROM jobs"):
                db_rows[row["slug"]] = row
        except Exception:  # noqa: BLE001
            log.exception("no se pudieron leer los jobs de la BD")
    cron_map = cronfile.parse()
    merged = []
    for job in jobs:
        slug = job["slug"]
        row = db_rows.get(slug)
        if row:
            for field in JOB_FIELDS:
                if field in row and row[field] is not None:
                    job[field] = row[field]
            job["id"] = row.get("id")
        elif slug in cron_map:
            cron = cron_map[slug]
            job["cron_minute"] = cron["minute"]
            job["cron_hour"] = cron["hour"]
            job["cron_dom"] = cron["dom"]
            job["cron_month"] = cron["month"]
            job["cron_dow"] = cron["dow"]
        # Horario realmente instalado en /etc/cron.d/backupcsr (fuente de verdad
        # del disparo); puede no coincidir con el de la BD.
        installed = cron_map.get(slug)
        if installed:
            job["installed_cron"] = {
                "minute": installed["minute"],
                "hour": installed["hour"],
                "dom": installed["dom"],
                "month": installed["month"],
                "dow": installed["dow"],
            }
        merged.append(job)
    for job in merged:
        # size_exclude viaja como lista al frontend aunque en la BD sea texto.
        job["size_exclude"] = catalog.split_list(job.get("size_exclude"))
        job["tags"] = catalog.join_list(job.get("tags"), 255)
    merged.sort(key=lambda item: item.get("sort", 100))
    return merged


def find_job(slug: str) -> dict | None:
    for job in effective_jobs():
        if job["slug"] == slug:
            return job
    return None


# <-- progreso ---------------------------------------------------------------

_PROGRESS_STATE = {
    "OK": "ok",
    "TARDE": "late",
    "PARCIAL": "partial",
    "OMITIDO": "omitted",
    "FALLO": "failed",
    "NUNCA": "never",
}


def _short_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def _typical_duration(runs: list[dict]) -> int | None:
    """Mediana de la duración de las últimas corridas OK: denominador del %."""
    values = []
    for run in runs:
        if run.get("status") != "OK":
            continue
        seconds = logs.duration_s(run)
        if seconds and seconds > 0:
            values.append(seconds)
    if not values:
        return None
    values = values[-10:]
    values.sort()
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) // 2


def _progress(job: dict, runs: list[dict], latest: dict | None, running: bool,
              status: str, next_dt: datetime | None, reference: datetime) -> dict:
    """Progreso de una tarea: estimado mientras corre, frescura/salud en reposo.

    No toca los scripts ni duplica listados remotos: el % en curso se estima con la
    duración típica del histórico y en reposo se muestra qué tan fresca está la copia
    respecto al próximo ciclo.
    """
    typical = _typical_duration(runs)
    if not job.get("enabled"):
        return {"state": "disabled", "percent": 0, "live": False,
                "typical_s": typical, "detail": "tarea deshabilitada"}
    # EN_CURSO manda aunque el lockfile no sea legible (p. ej. permisos): el log ya
    # dijo que arrancó y dentro de la gracia debe mostrarse el avance estimado.
    if (running or status == "EN_CURSO") and latest and latest.get("started_at"):
        started = logs.from_naive(latest["started_at"])
        elapsed = max(0, int((reference - started).total_seconds()))
        if typical:
            percent = min(99, round(elapsed * 100 / typical))
            detail = f"{_short_duration(elapsed)} de ~{_short_duration(typical)}"
        else:
            percent = None
            detail = "en curso (sin histórico de duración)"
        return {"state": "running", "percent": percent, "live": True,
                "typical_s": typical, "detail": detail}

    state = _PROGRESS_STATE.get(status, "never")
    if status in ("FALLO", "NUNCA"):
        return {"state": state, "percent": 0, "live": False, "typical_s": typical,
                "detail": "última ejecución falló" if status == "FALLO"
                          else "sin ejecuciones registradas"}

    if next_dt and latest and latest.get("started_at"):
        started = logs.from_naive(latest["started_at"])
        span = (next_dt - started).total_seconds()
        remain = (next_dt - reference).total_seconds()
        if span > 0:
            percent = max(0, min(100, round(remain * 100 / span)))
            detail = ("próxima en " + _short_duration(int(remain))) if remain >= 0 \
                else ("vencida hace " + _short_duration(int(-remain)))
            return {"state": state, "percent": percent, "live": False,
                    "typical_s": typical, "detail": detail}

    detail = {"ok": "al día", "late": "cierre anterior a lo esperado",
              "partial": "cierre con archivos con error",
              "omitted": "ronda omitida"}.get(state, "sin datos")
    return {"state": state, "percent": 0, "live": False, "typical_s": typical,
            "detail": detail}


def collect_jobs() -> list[dict]:
    reference = logs.now()
    result = []
    for job in effective_jobs():
        runs = logs.parse_slug(job["slug"])
        # La frescura se juzga con la última corrida real; una OMITIDO no la altera.
        latest = logs.effective_run(runs)
        omit = logs.latest_omit(runs)
        running = runner.is_running(job)
        # El estado se evalúa con el cron instalado (lo que dispara de verdad); la
        # BD solo aporta el horario editable y se compara para avisar del desvío.
        sched = _effective_schedule(job)
        status, detail = logs.evaluate(latest, sched, reference, running)
        if not job.get("enabled"):
            # Una tarea deshabilitada no "debe" correr: ni tarda ni falta, y así no
            # entra en los KPIs de fallos/sin datos ni en el banner de alertas.
            status, detail = "DESHABILITADA", "sin horario ni script (deshabilitada)"
        next_dt = logs.next_run(sched, reference) if job.get("enabled") else None
        size = sizes.current(job)
        result.append(
            {
                **{field: job.get(field) for field in JOB_FIELDS},
                "id": job.get("id"),
                "status": status,
                "status_detail": detail,
                "error_class": (latest or {}).get("error_class"),
                "running": running,
                "last_run": logs.run_to_dict(latest) if latest else None,
                "omitted": logs.run_to_dict(omit) if omit else None,
                "next_run": _iso(next_dt),
                "cron_effective": sched,
                "schedule_drift": bool(
                    job.get("installed_cron")
                    and not _same_schedule(job["installed_cron"], _schedule(job))
                ),
                "runs_total": len(runs),
                "size": size,
                "size_bytes": size.get("bytes"),
                "size_exclude": job.get("size_exclude") or [],
                "progress": _progress(job, runs, latest, running, status, next_dt, reference),
            }
        )
    return result


def _open_incidents() -> dict:
    """Incidentes abiertos por slug (error_class/consecutive), o {} sin BD.

    Sin `db.available()` previo: la consulta ya abre su conexión y, si la BD no
    responde, el `except` devuelve {} (evita un handshake redundante en /summary).
    """
    try:
        rows = db.query(
            "SELECT j.slug AS slug, i.error_class, i.consecutive, "
            "i.first_seen, i.last_seen, i.`count` "
            "FROM incidents i JOIN jobs j ON j.id = i.job_id "
            "WHERE i.resolved_at IS NULL"
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer los incidentes")
        return {}
    return {row["slug"]: row for row in rows}


def _raise_by_criticality(level: str, job: dict) -> str:
    """La criticidad del job sube la severidad, nunca la baja."""
    if level == "warning" and str(job.get("criticality") or "") == "alta":
        return "danger"
    return level


# Etiqueta corta de la clase de error (ver logs.py) para el detalle del aviso.
CLASS_LABELS = {
    "fatal": "Crítico",
    "transitorio": "Red",
    "contencion": "Cola",
    "parcial": "Parcial",
}


def _when(value) -> str | None:
    """Fecha legible en la zona del portal, o None si viene vacía."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    try:
        return logs.from_naive(value).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(value)


def _incident_rows(inc: dict) -> list[dict]:
    """Detalle temporal de un incidente abierto (count/first_seen/last_seen)."""
    rows: list[dict] = []
    if inc.get("count"):
        rows.append({"label": "Incidencias", "value": str(int(inc["count"]))})
    first = _when(inc.get("first_seen"))
    if first:
        rows.append({"label": "Desde", "value": first})
    last = _when(inc.get("last_seen"))
    if last:
        rows.append({"label": "Última vez", "value": last})
    return rows


def _job_details(job: dict, inc: dict, cls: str | None) -> list[dict]:
    """Filas de detalle de una tarea para la campana."""
    rows: list[dict] = [
        {"label": "Estado", "value": job.get("status_detail") or job.get("status") or "—"},
    ]
    if cls:
        rows.append({"label": "Clase", "value": CLASS_LABELS.get(cls, cls)})
    if int(inc.get("consecutive") or 0):
        rows.append({"label": "Fallos consecutivos", "value": str(int(inc["consecutive"]))})
    rows.extend(_incident_rows(inc))
    last = (job.get("last_run") or {}).get("started_at")
    if last:
        rows.append({"label": "Última ejecución", "value": _when(last) or str(last)})
    if job.get("next_run"):
        rows.append({"label": "Próximo intento", "value": _when(job["next_run"]) or str(job["next_run"])})
    if job.get("criticality"):
        rows.append({"label": "Criticidad", "value": str(job["criticality"]).capitalize()})
    return rows


def _notif(kind: str, level: str, text: str, *, slug: str | None = None,
           cls: str | None = None, consecutive: int = 0,
           details: list[dict] | None = None, extra: dict | None = None,
           ts: str | None = None) -> dict:
    """Aviso normalizado para la campana: id estable, severidad, fecha y detalle."""
    arm = f"{slug}:{cls}:" if slug else ""
    notif: dict = {
        "id": f"copias:{kind}:{arm}{level}",
        "severity": "critical" if level == "danger" else "warning",
        "level": level,
        "kind": kind,
        "text": text,
        "ts": ts or logs.now().strftime("%Y-%m-%d %H:%M"),
        "details": details or [],
    }
    if slug:
        notif["slug"] = slug
        notif["action"] = {"label": "Ver tarea", "slug": slug}
    if cls:
        notif["class"] = cls
    if consecutive:
        notif["consecutive"] = consecutive
    if extra:
        notif.update(extra)
    return notif


def build_alerts(jobs: list[dict]) -> list[dict]:
    """Avisos para el banner: jobs fallidos/parciales/omitidos, NAS lleno o no disponible.

    Severidad por clase: `fatal` → Crítico (danger); `transitorio` con debounce
    (>= 2 corridas no-OK consecutivas) → warning; `contencion` (ronda omitida) →
    warning; `parcial` → warning. La criticality del job puede subir el nivel.
    """
    alerts: list[dict] = []
    incidents = _open_incidents()
    for job in jobs:
        if not job.get("enabled"):
            continue
        slug = job.get("slug")
        inc = incidents.get(slug) or {}
        consecutive = int(inc.get("consecutive") or 0)
        status = job["status"]
        when = _when(inc.get("last_seen")) or _when((job.get("last_run") or {}).get("started_at"))
        if status == "FALLO":
            cls = inc.get("error_class") or job.get("error_class") or "fatal"
            if cls == "transitorio" and consecutive < 2:
                continue  # debounce: un fallo transitorio aislado no alerta
            level = _raise_by_criticality("danger" if cls == "fatal" else "warning", job)
            alerts.append(_notif(
                "job", level, f"{job['name']}: {job.get('status_detail') or 'falló'}",
                slug=slug, cls=cls, consecutive=consecutive, ts=when,
                details=_job_details(job, inc, cls),
            ))
        elif status == "PARCIAL":
            alerts.append(_notif(
                "job", _raise_by_criticality("warning", job),
                f"{job['name']}: {job.get('status_detail') or 'cierre parcial'}",
                slug=slug, cls="parcial", consecutive=consecutive, ts=when,
                details=_job_details(job, inc, "parcial"),
            ))
        elif job.get("omitted"):
            alerts.append(_notif(
                "job", "warning",
                f"{job['name']}: ronda omitida (cola ocupada); se reintenta en el próximo ciclo",
                slug=slug, cls="contencion", consecutive=consecutive, ts=when,
                details=_job_details(job, inc, "contencion"),
            ))
        elif status == "TARDE":
            alerts.append(_notif(
                "job", _raise_by_criticality("warning", job),
                f"{job['name']}: ejecución atrasada",
                slug=slug, ts=when, details=_job_details(job, inc, None),
            ))
        elif status == "NUNCA":
            alerts.append(_notif(
                "job", _raise_by_criticality("warning", job),
                f"{job['name']}: sin ejecuciones registradas",
                slug=slug, ts=when, details=_job_details(job, inc, None),
            ))
    if not files.nas_ready():
        alerts.append(_notif(
            "nas", "danger", f"El NAS no está montado en {config.NAS_MOUNT}",
            details=[{"label": "Ruta", "value": str(config.NAS_MOUNT)}],
        ))
    else:
        stats = sizes.nas_stats()
        if stats and stats["percent"] >= config.NAS_MIN_FREE_PCT:
            alerts.append(_notif(
                "nas", "warning", f"NAS al {stats['percent']}% de uso",
                details=[
                    {"label": "Uso", "value": f"{stats['percent']}%"},
                    {"label": "Umbral", "value": f"{config.NAS_MIN_FREE_PCT}%"},
                ],
            ))
    if not db.available():
        alerts.append(_notif(
            "db", "warning", "MySQL no disponible: modo lectura (sin edición ni historial)",
        ))
    skew = _clock_skew(jobs)
    if skew:
        alerts.append(_notif(
            "clock", "warning",
            f"Los logs van {skew} h por delante del reloj del portal: "
            "revisa BACKUP_TZ (¿UTC?) o la zona del host",
        ))
    # Una sola alerta agregada: el estado ya se evalúa con el cron instalado, así
    # que el desvío de la BD no es operativo, pero conviene saber que el portal y
    # el cron no dicen lo mismo (p. ej. tras un install.sh).
    drifted = [job for job in jobs if job.get("enabled") and job.get("schedule_drift")]
    if drifted and config.MANAGE_CRON:
        names = ", ".join(str(job.get("name") or job["slug"]) for job in drifted)
        alerts.append(_notif(
            "schedule", "warning",
            f"{len(drifted)} tarea(s) difieren del cron instalado ({names}); "
            "el estado se evalúa con el cron",
            details=[{"label": "Tareas", "value": names}],
            extra={"slugs": [job["slug"] for job in drifted]},
        ))
    return alerts


def _incident_dict(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "slug": row.get("job_slug"),
        "name": row.get("job_name"),
        "error_class": row.get("error_class"),
        "level": row.get("level"),
        "text": row.get("text"),
        "first_seen": _iso(logs.from_naive(row["first_seen"])) if row.get("first_seen") else None,
        "last_seen": _iso(logs.from_naive(row["last_seen"])) if row.get("last_seen") else None,
        "resolved_at": _iso(logs.from_naive(row["resolved_at"])) if row.get("resolved_at") else None,
        "count": int(row.get("count") or 0),
        "consecutive": int(row.get("consecutive") or 0),
    }


def _clock_skew(jobs: list[dict]) -> float | None:
    """Desfase en horas si las corridas parecen estar en el futuro (>10 min)."""
    reference = logs.now()
    worst = None
    for job in jobs:
        started = (job.get("last_run") or {}).get("started_at")
        if not started:
            continue
        try:
            when = datetime.fromisoformat(started)
        except ValueError:
            continue
        if when - reference > timedelta(minutes=10):
            worst = when if worst is None or when > worst else worst
    if worst is None:
        return None
    return round((worst - reference).total_seconds() / 3600, 1)


def audit(username: str, action: str, slug: str | None = None, detail: dict | None = None) -> None:
    if not db.available():
        return
    try:
        db.execute(
            "INSERT INTO audit_log (username, action, job_slug, detail_json) VALUES (%s, %s, %s, %s)",
            (username, action, slug, json.dumps(detail or {}, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo registrar auditoría")


def _require_db() -> None:
    if not db.available():
        raise HTTPException(status_code=503, detail="base de datos no disponible")


# ------------------------------- auth -------------------------------------


@router.post("/auth/login")
async def login(payload: LoginIn, response: Response):
    _require_db()
    user = auth.get_user(payload.username)
    if not user or not user.get("active") or not auth.verify_password(
        payload.password, user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="usuario o contraseña inválidos")
    token = auth.make_session(user["username"], user["role"])
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
        domain=config.COOKIE_DOMAIN or None,
    )
    return {"username": user["username"], "role": user["role"]}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(config.SESSION_COOKIE, path="/", domain=config.COOKIE_DOMAIN or None)
    return {"ok": True}


@router.get("/auth/me")
async def me(user: dict = Depends(auth.current_user)):
    return user


# ------------------------------ health ------------------------------------

# Cache de disponibilidad de MySQL para /health: el sondeo del portal (5 s) no debe
# rozar el timeout de conexión a la BD (evita parpadeos "caído/recuperado").
_db_cache = {"ts": 0.0, "ok": False}


def _db_available_cached(ttl: float = 5.0) -> bool:
    now = time.monotonic()
    if now - _db_cache["ts"] < ttl:
        return _db_cache["ok"]
    ok = db.available()
    _db_cache["ts"] = now
    _db_cache["ok"] = ok
    return ok


@router.get("/health")
async def health():
    db_ok = _db_available_cached()
    cron_ok = config.CRON_FILE.exists()
    nas_ok = files.nas_ready()
    status = "ok" if db_ok and cron_ok else "degraded"
    return {
        "status": status,
        "db": db_ok,
        "db_error": db.last_error(),
        "cron": cron_ok,
        "cron_path": str(config.CRON_FILE),
        "nas": nas_ok,
        "nas_path": str(config.NAS_MOUNT),
        "nas_stats": sizes.nas_stats(),
        "manage_cron": config.MANAGE_CRON,
        "time": _iso(logs.now()),
    }


# ------------------------------- jobs -------------------------------------


@router.get("/summary")
async def summary(user: dict = Depends(auth.current_user)):
    jobs = collect_jobs()
    counts = {
        "total": len(jobs), "OK": 0, "EN_CURSO": 0, "TARDE": 0, "FALLO": 0,
        "NUNCA": 0, "OMITIDO": 0, "PARCIAL": 0, "DESHABILITADA": 0,
    }
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    counts["habilitados"] = sum(1 for job in jobs if job.get("enabled"))
    bytes_total = sum(int(job["size_bytes"] or 0) for job in jobs)
    files_total = sum(int((job.get("size") or {}).get("files") or 0) for job in jobs)
    sizes.warm(effective_jobs())
    durations = [
        job["last_run"]["duration_s"]
        for job in jobs
        if job.get("last_run") and job["last_run"].get("duration_s")
    ]
    return {
        "counts": counts,
        "bytes_total": bytes_total,
        "files_total": files_total,
        "avg_duration_s": int(sum(durations) / len(durations)) if durations else None,
        "manage_cron": config.MANAGE_CRON,
        "db": db.available(),
        "nas": files.nas_ready(),
        "nas_stats": sizes.nas_stats(),
        "growth": sizes.growth(effective_jobs()),
        "alerts": build_alerts(jobs),
        "generated_at": _iso(logs.now()),
    }


@router.get("/jobs")
async def list_jobs(user: dict = Depends(auth.current_user)):
    sizes.warm(effective_jobs())
    return {
        "jobs": collect_jobs(),
        "manage_cron": config.MANAGE_CRON,
        "db": db.available(),
        "nas": files.nas_ready(),
        "nas_stats": sizes.nas_stats(),
        "generated_at": _iso(logs.now()),
    }


@router.get("/scopes")
async def list_scopes(user: dict = Depends(auth.current_user)):
    """Lista ligera de tareas (jobs activos) para configurar avisos push por tarea."""
    if not db.available():
        return {"scopes": []}
    try:
        rows = db.query(
            "SELECT slug, name, criticality FROM jobs WHERE enabled = 1 ORDER BY sort, name"
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer los jobs para la lista de scopes")
        return {"scopes": []}
    labels = {"alta": "Criticidad alta", "media": "Criticidad media", "baja": "Criticidad baja"}
    return {
        "scopes": [
            {
                "slug": row["slug"],
                "name": row["name"] or row["slug"],
                "group": row.get("criticality") or "",
                "group_title": labels.get(row.get("criticality") or "", ""),
            }
            for row in rows
        ]
    }


@router.get("/incidents")
async def list_incidents(limit: int = 50, user: dict = Depends(auth.current_user)):
    """Incidentes abiertos y últimos resueltos (historial de errores)."""
    limit = max(1, min(int(limit), 200))
    if not db.available():
        return {"open": [], "recent": [], "db": False}
    try:
        open_rows = db.query(
            "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
            "JOIN jobs j ON j.id = i.job_id WHERE i.resolved_at IS NULL "
            "ORDER BY i.last_seen DESC LIMIT %s",
            (limit,),
        )
        recent_rows = db.query(
            "SELECT i.*, j.slug AS job_slug, j.name AS job_name FROM incidents i "
            "JOIN jobs j ON j.id = i.job_id WHERE i.resolved_at IS NOT NULL "
            "ORDER BY i.resolved_at DESC LIMIT %s",
            (limit,),
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer los incidentes")
        return {"open": [], "recent": [], "db": True}
    return {
        "open": [_incident_dict(row) for row in open_rows],
        "recent": [_incident_dict(row) for row in recent_rows],
        "db": True,
    }


@router.post("/jobs/batch")
async def run_batch(payload: BatchIn, user: dict = Depends(auth.require_admin)):
    """Encola acciones masivas; el worker las ejecuta de a una (ver app/batch.py)."""
    action = (payload.action or "").strip()
    if action not in batch.ACTIONS:
        raise HTTPException(status_code=400, detail=f"acción inválida: {action}")
    slugs = [str(slug).strip() for slug in payload.slugs if str(slug).strip()]
    if not slugs:
        raise HTTPException(status_code=400, detail="no hay tareas seleccionadas")
    if len(slugs) > 50:
        raise HTTPException(status_code=400, detail="máximo 50 tareas por lote")
    collected = {job["slug"]: job for job in collect_jobs()}
    queued = batch.queued_slugs()
    jobs = []
    skipped = []
    seen = set()
    for slug in slugs:
        if slug in seen:
            skipped.append({"slug": slug, "reason": "duplicada"})
            continue
        seen.add(slug)
        job = collected.get(slug)
        if not job:
            skipped.append({"slug": slug, "reason": "no encontrada"})
            continue
        if slug in queued:
            skipped.append({"slug": slug, "reason": "ya está en cola"})
            continue
        if action == "retry" and job.get("status") != "FALLO":
            skipped.append({"slug": slug, "reason": "no está en FALLO"})
            continue
        if action in ("run", "dry", "retry") and job.get("running"):
            skipped.append({"slug": slug, "reason": "ya está corriendo"})
            continue
        jobs.append(job)
    if not jobs:
        return {"ok": False, "queued": [], "pending": [], "skipped": skipped,
                "message": "ninguna tarea aplicable"}
    info = batch.enqueue(action, jobs, user["username"])
    return {"ok": True, "skipped": skipped, **info}


@router.get("/jobs/batch")
async def batch_status(user: dict = Depends(auth.current_user)):
    """Estado del lote en curso: qué corre, qué queda pendiente y resultados."""
    return batch.status()


@router.get("/jobs/scheduler")
async def scheduler_status(user: dict = Depends(auth.current_user)):
    """Estado del planificador: cupos, cola y corridas (vacío si está apagado)."""
    try:
        from . import scheduler_client
        return scheduler_client.status()
    except Exception:  # noqa: BLE001
        return {"available": False}


@router.get("/jobs/{slug}")
async def job_detail(slug: str, tail: int = 200, user: dict = Depends(auth.current_user)):
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")
    runs = logs.parse_slug(slug)
    latest = logs.effective_run(runs)
    omit = logs.latest_omit(runs)
    running = runner.is_running(job)
    status, detail = logs.evaluate(latest, _schedule(job), logs.now(), running)
    return {
        "job": {
            **{field: job.get(field) for field in JOB_FIELDS},
            "status": status,
            "status_detail": detail,
            "error_class": (latest or {}).get("error_class"),
            "running": running,
            "omitted": logs.run_to_dict(omit) if omit else None,
            "next_run": _iso(logs.next_run(_schedule(job), logs.now())),
            "size": sizes.current(job),
        },
        "runs": [logs.run_to_dict(run) for run in runs[-20:]][::-1],
        "log": logs.tail_log(slug, tail),
    }


@router.get("/jobs/{slug}/script")
async def job_script(slug: str, user: dict = Depends(auth.current_user)):
    """Script real de la tarea (solo lectura) y si la ficha coincide con él."""
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")
    text = _script_text(slug)
    return {
        "job": slug,
        "path": str(config.JOBS_DIR / f"{slug}.sh"),
        "exists": text is not None,
        "text": text or "",
        "drift": _script_drift(job, text),
    }


@router.post("/jobs/{slug}/size/refresh")
def refresh_size(slug: str, user: dict = Depends(auth.require_admin)):
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")
    if not files.nas_ready():
        raise HTTPException(status_code=503, detail="el NAS no está montado")
    size = sizes.job_size(job, force=True)
    job_id = job.get("id")
    if job_id and size.get("bytes") is not None:
        sizes.save_snapshot(job_id, size)
    audit(user["username"], "size-refresh", slug)
    return {"ok": True, "size": size}


@router.post("/jobs/{slug}/run")
async def run_job(slug: str, payload: RunIn, user: dict = Depends(auth.require_admin)):
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")
    requested = "dry" if payload.dry_run else "run"
    queued = runner.submit(job, action=requested, trigger="manual", dry_run=payload.dry_run)
    if queued is not None:
        # El planificador puede fusionar con una ronda pendiente: se audita y responde
        # la acción EFECTIVA (p. ej. un dry que cae sobre un run pendiente no es dry).
        effective = queued.get("action") or requested
        dry = bool(queued.get("dry_run"))
        action = "run-dry" if dry else ("run-retry" if payload.retry or effective == "retry" else "run")
        audit(user["username"], action, slug)
        return {"ok": True, "message": "encolado", "dry_run": dry}
    ok, message = runner.start_direct(job, dry_run=payload.dry_run)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    action = "run-dry" if payload.dry_run else ("run-retry" if payload.retry else "run")
    audit(user["username"], action, slug)
    return {"ok": True, "message": message, "dry_run": payload.dry_run}


@router.patch("/jobs/{slug}")
async def update_job(slug: str, patch: JobPatch, user: dict = Depends(auth.require_admin)):
    _require_db()
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")

    provided = patch.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(status_code=400, detail="nada que actualizar")
    cron_part = {key: value for key, value in provided.items() if key in CRON_FIELDS}
    meta_part = {key: value for key, value in provided.items() if key not in CRON_FIELDS}
    if cron_part and not config.MANAGE_CRON:
        raise HTTPException(
            status_code=409,
            detail="gestión de cron deshabilitada (BACKUP_MANAGE_CRON=0)",
        )

    updates: dict = {}

    # --- Ficha: solo exige rol admin ---
    if "name" in meta_part:
        name = (meta_part["name"] or "").strip()
        if not name or len(name) > 128:
            raise HTTPException(status_code=400, detail="name inválido (1-128 caracteres)")
        updates["name"] = name
    if "description" in meta_part:
        description = (meta_part["description"] or "").strip()
        if len(description) > 255:
            raise HTTPException(status_code=400, detail="description supera 255 caracteres")
        updates["description"] = description
    if "origin_type" in meta_part:
        origin_type = (meta_part["origin_type"] or "").strip()
        if origin_type not in catalog.ORIGIN_TYPES:
            raise HTTPException(status_code=400, detail=f"origin_type inválido: {origin_type}")
        updates["origin_type"] = origin_type
    if "origin_host" in meta_part:
        origin_host = (meta_part["origin_host"] or "").strip()
        if len(origin_host) > 255:
            raise HTTPException(status_code=400, detail="origin_host supera 255 caracteres")
        updates["origin_host"] = origin_host
    if "source" in meta_part:
        source = (meta_part["source"] or "").strip()
        if len(source) > 255:
            raise HTTPException(status_code=400, detail="source supera 255 caracteres")
        updates["source"] = source
    if "dest_rel" in meta_part:
        dest_rel = (meta_part["dest_rel"] or "").strip()
        if not _valid_dest_rel(dest_rel):
            raise HTTPException(status_code=400, detail=f"dest_rel inválido: {dest_rel}")
        updates["dest_rel"] = dest_rel
    if "sort" in meta_part:
        if meta_part["sort"] is None:
            raise HTTPException(status_code=400, detail="sort no puede ser nulo")
        sort = int(meta_part["sort"])
        if not 0 <= sort <= 9999:
            raise HTTPException(status_code=400, detail="sort debe estar entre 0 y 9999")
        updates["sort"] = sort
    if "criticality" in meta_part:
        criticality = (meta_part["criticality"] or "").strip()
        if criticality not in catalog.CRITICALITIES:
            raise HTTPException(status_code=400, detail=f"criticality inválida: {criticality}")
        updates["criticality"] = criticality
    if "owner" in meta_part:
        owner = (meta_part["owner"] or "").strip()
        if len(owner) > 128:
            raise HTTPException(status_code=400, detail="owner supera 128 caracteres")
        updates["owner"] = owner
    if "retention_days" in meta_part:
        retention = meta_part["retention_days"]
        if retention is None:
            updates["retention_days"] = None
        elif int(retention) < 0:
            raise HTTPException(status_code=400, detail="retention_days no puede ser negativo")
        else:
            updates["retention_days"] = int(retention)
    if "tags" in meta_part:
        tags = ",".join(catalog.split_list(meta_part.get("tags")))
        if len(tags) > 255:
            raise HTTPException(status_code=400, detail="tags supera 255 caracteres")
        updates["tags"] = tags
    if "size_exclude" in meta_part:
        size_exclude = ",".join(catalog.split_list(meta_part.get("size_exclude")))
        if len(size_exclude) > 512:
            raise HTTPException(status_code=400, detail="size_exclude supera 512 caracteres")
        updates["size_exclude"] = size_exclude
    if "notes" in meta_part:
        notes = (meta_part["notes"] or "").strip()
        if len(notes) > 10000:
            raise HTTPException(status_code=400, detail="notes supera 10000 caracteres")
        updates["notes"] = notes

    # --- Programación: exige BACKUP_MANAGE_CRON (ya comprobado arriba) ---
    for field in ("cron_minute", "cron_hour", "cron_dom", "cron_month", "cron_dow"):
        if field in cron_part:
            value = cron_part[field] or ""
            if not _cron_valid_field(value):
                raise HTTPException(status_code=400, detail=f"{field} inválido: {value}")
            updates[field] = value.strip()
    if cron_part.get("enabled") is not None:
        enabled = bool(cron_part["enabled"])
        script = config.JOBS_DIR / f"{slug}.sh"
        if enabled and not script.exists():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"no se puede habilitar {slug}: falta {script} "
                    "(créalo e instálalo antes de darle horario)"
                ),
            )
        updates["enabled"] = 1 if enabled else 0
    if "lockfile" in cron_part:
        lockfile = (cron_part["lockfile"] or "").strip()
        if lockfile:
            if not _valid_lockfile(lockfile):
                raise HTTPException(
                    status_code=400,
                    detail="lockfile inválido: debe ser una ruta absoluta bajo /run/lock/",
                )
            row = db.query(
                "SELECT slug FROM jobs WHERE lockfile=%s AND slug<>%s",
                (lockfile, slug),
                one=True,
            )
            if row:
                raise HTTPException(status_code=409, detail=f"lockfile ya lo usa {row['slug']}")
            updates["lockfile"] = lockfile
        else:
            updates["lockfile"] = f"/run/lock/backupcsr-{slug}.lock"

    if not updates:
        raise HTTPException(status_code=400, detail="nada que actualizar")

    assignments = ", ".join(f"{field}=%s" for field in updates)
    db.execute(f"UPDATE jobs SET {assignments} WHERE slug=%s", (*updates.values(), slug))

    # Solo se reescribe el cron si cambió algo del disparo; la ficha no lo toca.
    if any(field in updates for field in CRON_FIELDS):
        cronfile.write(effective_jobs())
    audit(user["username"], "update-job", slug, updates)
    return {"ok": True, "job": next(j for j in collect_jobs() if j["slug"] == slug)}


@router.post("/jobs/{slug}/reset-schedule")
async def reset_schedule(slug: str, user: dict = Depends(auth.require_admin)):
    _require_db()
    if not config.MANAGE_CRON:
        raise HTTPException(
            status_code=409,
            detail="gestión de cron deshabilitada (BACKUP_MANAGE_CRON=0)",
        )
    try:
        catalog.reset_schedule(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail="job no encontrado") from None
    cronfile.write(effective_jobs())
    audit(user["username"], "reset-schedule", slug)
    return {"ok": True, "job": next(j for j in collect_jobs() if j["slug"] == slug)}


@router.get("/jobs/{slug}/files")
async def job_files(slug: str, path: str = "", user: dict = Depends(auth.current_user)):
    job, anchor = _job_anchor(slug)
    return {
        "job": slug,
        "manage": config.MANAGE_FILES,
        "running": sizes.job_running(job),
        "confirm_mb": config.FILES_CONFIRM_MB,
        "max_upload_mb": config.FILES_MAX_UPLOAD_MB,
        **files.list_dir(path, anchor=anchor),
    }


# ------------------------------ cron --------------------------------------


@router.get("/cron")
async def cron_status(user: dict = Depends(auth.current_user)):
    jobs = effective_jobs()
    return {
        "manage_cron": config.MANAGE_CRON,
        "path": str(config.CRON_FILE),
        "diff": cronfile.diff(jobs),
    }


@router.get("/jobs/{slug}/cron-preview")
async def cron_preview(
    slug: str,
    minute: str | None = None,
    hour: str | None = None,
    dom: str | None = None,
    month: str | None = None,
    dow: str | None = None,
    count: int = Query(5, ge=1, le=20),
    user: dict = Depends(auth.current_user),
):
    """Próximas ejecuciones de un horario propuesto (sin guardarlo)."""
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")
    schedule = _schedule(job)
    for field, value in (("minute", minute), ("hour", hour), ("dom", dom),
                         ("month", month), ("dow", dow)):
        if value is None:
            continue
        if not _cron_valid_field(value):
            raise HTTPException(status_code=400, detail=f"{field} inválido: {value}")
        schedule[field] = value.strip()
    return {
        "schedule": schedule,
        "next": [_iso(when) for when in logs.next_runs(schedule, count)],
    }


# ------------------------------- runs -------------------------------------


@router.get("/runs")
async def list_runs(
    job: str | None = None,
    status: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(auth.current_user),
):
    source = "db" if db.available() else "logs"
    return {"runs": _collect_runs(job, status, desde, hasta, limit, offset), "source": source}


@router.get("/runs/export")
async def export_runs(
    format: str = Query("csv", pattern="^(csv|json)$"),
    job: str | None = None,
    status: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    limit: int = Query(5000, ge=1, le=50000),
    user: dict = Depends(auth.current_user),
):
    runs = _collect_runs(job, status, desde, hasta, limit, 0)
    stamp = logs.now().strftime("%Y%m%d-%H%M")
    if format == "json":
        payload = json.dumps(
            {"generated_at": _iso(logs.now()), "count": len(runs), "runs": runs},
            ensure_ascii=False, indent=2,
        )
        return Response(
            payload, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="copias-{stamp}.json"'},
        )
    fields = [
        "job_slug", "started_at", "finished_at", "status", "dry_run", "duration_s",
        "size_bytes", "files_transferred", "files_removed", "error_class", "error_text",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for run in runs:
        writer.writerow({key: run.get(key) for key in fields})
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="copias-{stamp}.csv"'},
    )


def _parse_day(value: str | None, end: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return day + timedelta(days=1) if end else day


def _collect_runs(job: str | None, status: str | None, desde: str | None,
                  hasta: str | None, limit: int, offset: int) -> list[dict]:
    start = _parse_day(desde)
    end = _parse_day(hasta, end=True)
    if db.available():
        where = []
        params: list = []
        if job:
            where.append("j.slug = %s")
            params.append(job)
        if status:
            where.append("r.status = %s")
            params.append(status)
        if start:
            where.append("r.started_at >= %s")
            params.append(start)
        if end:
            where.append("r.started_at < %s")
            params.append(end)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = db.query(
            f"""
            SELECT r.*, j.slug AS job_slug, sz.size_bytes
            FROM runs r
            JOIN jobs j ON j.id = r.job_id
            LEFT JOIN (
                SELECT run_id, MAX(bytes) AS size_bytes
                FROM size_snapshots WHERE run_id IS NOT NULL GROUP BY run_id
            ) sz ON sz.run_id = r.id
            {clause}
            ORDER BY r.started_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        return [_run_row(row) for row in rows]

    collected = []
    for item in effective_jobs():
        if job and item["slug"] != job:
            continue
        for run in logs.parse_slug(item["slug"]):
            data = logs.run_to_dict(run)
            data["job_slug"] = item["slug"]
            if status and data["status"] != status:
                continue
            if start and (data.get("started_at") or "") < start.isoformat():
                continue
            if end and (data.get("started_at") or "") >= end.isoformat():
                continue
            collected.append(data)
    collected.sort(key=lambda run: run.get("started_at") or "", reverse=True)
    return collected[offset: offset + limit]


def _run_row(row: dict) -> dict:
    started = logs.from_naive(row["started_at"]) if row.get("started_at") else None
    finished = logs.from_naive(row["finished_at"]) if row.get("finished_at") else None
    duration = int((finished - started).total_seconds()) if started and finished else None
    if duration is not None and duration < 0:
        duration = None
    return {
        "id": row["id"],
        "job_slug": row.get("job_slug"),
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "status": row["status"],
        "dry_run": bool(row.get("dry_run")),
        "files_transferred": row.get("files_transferred", 0),
        "files_removed": row.get("files_removed", 0),
        "size_bytes": row.get("size_bytes"),
        "error_text": row.get("error_text"),
        "error_class": row.get("error_class") or None,
        "error_lines": logs.parse_error_lines(row.get("error_lines")),
        "exit_code": row.get("exit_code"),
        "triggered_by": row.get("triggered_by") or "cron",
        "log_path": row.get("log_path"),
        "duration_s": duration,
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: int, user: dict = Depends(auth.current_user)):
    _require_db()
    row = db.query(
        """
        SELECT r.*, j.slug AS job_slug, sz.size_bytes
        FROM runs r
        JOIN jobs j ON j.id = r.job_id
        LEFT JOIN (
            SELECT run_id, MAX(bytes) AS size_bytes
            FROM size_snapshots WHERE run_id IS NOT NULL GROUP BY run_id
        ) sz ON sz.run_id = r.id
        WHERE r.id = %s
        """,
        (run_id,),
        one=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="corrida no encontrada")
    files_rows = db.query(
        "SELECT path, action FROM run_files WHERE run_id = %s ORDER BY id", (run_id,)
    )
    return {
        "run": _run_row(row),
        "files": [{"path": item["path"], "action": item["action"]} for item in files_rows],
    }


@router.get("/runs/{run_id}/log")
async def run_log(run_id: int, tail: int = 500, user: dict = Depends(auth.current_user)):
    _require_db()
    row = db.query(
        "SELECT r.log_path, j.slug AS job_slug FROM runs r JOIN jobs j ON j.id=r.job_id WHERE r.id=%s",
        (run_id,),
        one=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="corrida no encontrada")
    return {"log": _read_log(row.get("job_slug"), row.get("log_path"), tail)}


def _read_log(slug: str | None, log_path: str | None, tail: int) -> str:
    base = config.LOG_DIR.resolve()
    candidates = []
    if log_path:
        candidates.append(Path(log_path))
    if slug:
        candidates.append(config.LOG_DIR / f"{slug}.log")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if base not in resolved.parents and resolved.parent != base:
            continue
        if resolved.exists() and resolved.is_file():
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            return "".join(lines[-max(1, min(tail, 5000)):])
    return ""


# ------------------------------ files -------------------------------------


def _job_anchor(slug: str) -> tuple[dict, Path]:
    """Job + carpeta ancla dentro del NAS (503 si el NAS no está montado)."""
    job = find_job(slug)
    if not job:
        raise HTTPException(status_code=404, detail="job no encontrado")
    if not files.nas_ready():
        raise HTTPException(
            status_code=503, detail=f"el NAS no está montado en {config.NAS_MOUNT}"
        )
    dest = (job.get("dest_rel") or "").strip("/")
    return job, (config.NAS_MOUNT / dest if dest else config.NAS_MOUNT)


def _files_writable(job: dict) -> None:
    """Requisitos para escribir en el NAS: gestión activa, BD (auditoría) y job parado."""
    if not config.MANAGE_FILES:
        raise HTTPException(
            status_code=409,
            detail="gestión de archivos deshabilitada (BACKUP_MANAGE_FILES=0)",
        )
    _require_db()
    if sizes.job_running(job):
        raise HTTPException(
            status_code=409,
            detail="la tarea de copia está corriendo: la gestión se bloquea hasta que termine",
        )


def _files_changed(job: dict, user: dict, action: str, detail: dict) -> None:
    """Tras una operación: el tamaño medido queda viejo y se registra la auditoría."""
    sizes.invalidate(job["slug"])
    audit(user["username"], action, job["slug"], detail)


@router.get("/files")
async def browse(path: str = "", user: dict = Depends(auth.current_user)):
    return files.list_dir(path)


@router.get("/jobs/{slug}/files/entry")
async def job_file_entry(slug: str, path: str = "", user: dict = Depends(auth.current_user)):
    """Datos de una entrada (tamaño, nº de archivos si es carpeta) para confirmar."""
    _job, anchor = _job_anchor(slug)
    return {"job": slug, "entry": files.entry_info(path, anchor)}


@router.get("/jobs/{slug}/files/download")
async def job_file_download(slug: str, path: str = "", user: dict = Depends(auth.current_user)):
    _job, anchor = _job_anchor(slug)
    item = files.download(path, anchor)
    audit(user["username"], "file-download", slug,
          {"path": item["rel"], "bytes": item["bytes"]})
    return FileResponse(item["path"], media_type="application/octet-stream",
                        filename=item["name"])


@router.post("/jobs/{slug}/files/mkdir")
async def job_file_mkdir(slug: str, payload: FilesMkdirIn,
                         user: dict = Depends(auth.require_admin)):
    job, anchor = _job_anchor(slug)
    _files_writable(job)
    result = files.make_dir(payload.path, anchor, payload.name)
    _files_changed(job, user, "file-mkdir", {"path": result["path"]})
    return {"ok": True, **result}


@router.post("/jobs/{slug}/files/rename")
async def job_file_rename(slug: str, payload: FilesRenameIn,
                          user: dict = Depends(auth.require_admin)):
    job, anchor = _job_anchor(slug)
    _files_writable(job)
    result = files.rename(payload.path, anchor, payload.name)
    _files_changed(job, user, "file-rename", {"path": payload.path, "name": payload.name})
    return {"ok": True, **result}


@router.post("/jobs/{slug}/files/move")
async def job_file_move(slug: str, payload: FilesMoveIn,
                        user: dict = Depends(auth.require_admin)):
    job, anchor = _job_anchor(slug)
    _files_writable(job)
    result = files.move(payload.path, anchor, payload.dest)
    _files_changed(job, user, "file-move", {"path": payload.path, "dest": payload.dest})
    return {"ok": True, **result}


@router.post("/jobs/{slug}/files/delete")
async def job_file_delete(slug: str, payload: FilesDeleteIn,
                          user: dict = Depends(auth.require_admin)):
    job, anchor = _job_anchor(slug)
    _files_writable(job)
    result = files.delete(payload.path, anchor, payload.confirm)
    _files_changed(job, user, "file-delete", {
        "path": result["path"], "type": result["type"],
        "bytes": result["bytes"], "entries": result["entries"],
    })
    return {"ok": True, **result}


@router.post("/jobs/{slug}/files/upload")
async def job_file_upload(
    slug: str,
    request: Request,
    path: str = Query(""),
    name: str = Query(...),
    overwrite: bool = Query(False),
    user: dict = Depends(auth.require_admin),
):
    """Sube un archivo con el cuerpo en crudo (sin multipart: no hace falta otra dependencia)."""
    job, anchor = _job_anchor(slug)
    _files_writable(job)
    declared = int(request.headers.get("content-length") or 0)
    if declared > max(1, config.FILES_MAX_UPLOAD_MB) * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"el archivo supera el máximo de {config.FILES_MAX_UPLOAD_MB} MB",
        )
    result = await files.save_upload(path, anchor, name, request.stream(), overwrite=overwrite)
    _files_changed(job, user, "file-upload", {"path": result["path"], "bytes": result["bytes"]})
    return {"ok": True, **result}


# ------------------------------ tamaños -----------------------------------


@router.get("/sizes")
async def size_series(
    days: int = Query(30, ge=1, le=365), user: dict = Depends(auth.current_user)
):
    return {"days": days, "series": sizes.series(days)}


# ------------------------------ usuarios ----------------------------------


def _validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise HTTPException(
            status_code=400,
            detail=f"la contraseña debe tener al menos {MIN_PASSWORD} caracteres",
        )


def _user_row(row: dict) -> dict:
    created = logs.from_naive(row["created_at"]) if row.get("created_at") else None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "active": bool(row.get("active")),
        "created_at": _iso(created),
    }


@router.get("/users")
async def list_users(user: dict = Depends(auth.require_admin)):
    _require_db()
    return {"users": [_user_row(row) for row in auth.list_users()]}


@router.post("/users")
async def create_user(payload: UserIn, user: dict = Depends(auth.require_admin)):
    _require_db()
    username = payload.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="usuario inválido (3-64: letras, números, . _ @ -)",
        )
    if payload.role not in ROLES:
        raise HTTPException(status_code=400, detail="rol inválido")
    _validate_password(payload.password)
    if auth.get_user(username):
        raise HTTPException(status_code=409, detail="el usuario ya existe")
    auth.create_user(username, payload.password, payload.role)
    audit(user["username"], "user-create", None, {"username": username, "role": payload.role})
    return {"ok": True}


@router.patch("/users/{username}")
async def update_user(username: str, patch: UserPatch,
                      user: dict = Depends(auth.require_admin)):
    _require_db()
    target = auth.get_user(username)
    if not target:
        raise HTTPException(status_code=404, detail="usuario no encontrado")
    last_admin = target.get("role") == "admin" and target.get("active") and auth.count_active_admins() <= 1
    if patch.role is not None:
        if patch.role not in ROLES:
            raise HTTPException(status_code=400, detail="rol inválido")
        if last_admin and patch.role != "admin":
            raise HTTPException(status_code=409, detail="no se puede degradar al último admin activo")
    if patch.active is not None:
        if not patch.active and username == user["username"]:
            raise HTTPException(status_code=409, detail="no puedes desactivar tu propia cuenta")
        if not patch.active and last_admin:
            raise HTTPException(status_code=409, detail="no se puede desactivar al último admin activo")
    if patch.role is not None:
        auth.set_role(username, patch.role)
    if patch.active is not None:
        auth.set_active(username, patch.active)
    audit(user["username"], "user-update", None, {
        "username": username, "role": patch.role, "active": patch.active,
    })
    return {"ok": True}


@router.post("/users/{username}/password")
async def reset_password(username: str, payload: PasswordIn,
                         user: dict = Depends(auth.require_admin)):
    _require_db()
    if not auth.get_user(username):
        raise HTTPException(status_code=404, detail="usuario no encontrado")
    _validate_password(payload.password)
    auth.set_password(username, payload.password)
    audit(user["username"], "user-password", None, {"username": username})
    return {"ok": True}


@router.post("/auth/password")
async def change_password(payload: SelfPasswordIn, user: dict = Depends(auth.current_user)):
    _require_db()
    row = auth.get_user(user["username"])
    if not row or not auth.verify_password(payload.current, row["password_hash"]):
        raise HTTPException(status_code=401, detail="contraseña actual incorrecta")
    _validate_password(payload.new)
    auth.set_password(user["username"], payload.new)
    audit(user["username"], "self-password")
    return {"ok": True}


# ------------------------------ auditoría ---------------------------------


def _parse_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


@router.get("/audit")
async def list_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    username: str | None = None,
    action: str | None = None,
    user: dict = Depends(auth.require_admin),
):
    _require_db()
    where = []
    params: list = []
    if username:
        where.append("username = %s")
        params.append(username)
    if action:
        where.append("action = %s")
        params.append(action)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = db.query(
        f"""
        SELECT id, username, action, job_slug, detail_json, created_at
        FROM audit_log
        {clause}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    return {"entries": [
        {
            "id": row["id"],
            "username": row["username"],
            "action": row["action"],
            "job_slug": row.get("job_slug"),
            "detail": _parse_json(row.get("detail_json")),
            "created_at": _iso(logs.from_naive(row["created_at"])) if row.get("created_at") else None,
        }
        for row in rows
    ]}
