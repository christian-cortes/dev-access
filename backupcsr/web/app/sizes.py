"""Tamaño almacenado por job en /mnt/nas y snapshots para tendencia.

El recorrido del NAS (CIFS) puede ser lento: nunca bloquea la petición HTTP. El
endpoint devuelve lo que haya en caché al instante y un refresco en segundo plano
(`warm`) recalcula cuando vence el TTL. `scan_dir` es iterativo, no sigue symlinks y
corta por número de entradas y por tiempo devolviendo un resultado parcial.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, files, logs

log = logging.getLogger("backupcsr-web")

_cache: dict[str, dict] = {}
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
_refresh_lock = threading.Lock()
_refreshing = False


def _lock_for(slug: str) -> threading.Lock:
    with _guard:
        lock = _locks.get(slug)
        if lock is None:
            lock = threading.Lock()
            _locks[slug] = lock
        return lock


def _disabled(job: dict) -> bool:
    """Un job deshabilitado no se mide: puede no tener destino todavía."""
    return not job.get("enabled", True)


# Sin medición (job deshabilitado). `disabled` lo distingue de "aún midiendo".
_UNMEASURED = {
    "bytes": None, "files": 0, "truncated": False, "error": None,
    "computed_at": None, "cached": False, "pending": False, "disabled": True,
}


def unmeasured() -> dict:
    return dict(_UNMEASURED)


def _anchor(job: dict) -> Path:
    dest = (job.get("dest_rel") or "").strip("/")
    base = config.NAS_MOUNT.resolve()
    root = (base / dest).resolve() if dest else base
    if root != base and base not in root.parents:
        raise ValueError("el destino queda fuera del NAS")
    return root


def _excludes(job: dict) -> set[str]:
    raw = job.get("size_exclude") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return {str(value).strip().strip("/") for value in raw if str(value).strip()}


def scan_dir(root: Path, exclude=(), max_entries: int | None = None,
             timeout: int | None = None) -> dict:
    """Suma `st_size` de los archivos bajo `root` (sin seguir symlinks).

    `exclude` solo se aplica a los hijos directos del ancla (p. ej. `storage` en
    ruta56) para no confundir el total con el de otro job que comparte el árbol.
    """
    limit = config.SIZE_MAX_ENTRIES if max_entries is None else max_entries
    deadline = time.monotonic() + (config.SIZE_TIMEOUT if timeout is None else timeout)
    total = 0
    count = 0
    truncated = False
    error: str | None = None
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if count >= limit or time.monotonic() > deadline:
                        truncated = True
                        stack = []
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if current == root and entry.name in exclude:
                                continue
                            stack.append(Path(entry.path))
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
    return {"bytes": total, "files": count, "truncated": truncated, "error": error}


def _compute(job: dict) -> dict:
    result = {"bytes": None, "files": 0, "truncated": False, "error": None}
    if not files.nas_ready():
        result["error"] = "el NAS no está montado"
        return result
    try:
        root = _anchor(job)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    if not root.exists():
        result["error"] = "el destino no existe"
        return result
    result.update(scan_dir(root, _excludes(job)))
    return result


def _public(data: dict) -> dict:
    stamp = data.get("_ts")
    computed = (
        datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
        if stamp
        else None
    )
    return {
        "bytes": data.get("bytes"),
        "files": data.get("files", 0),
        "truncated": bool(data.get("truncated")),
        "error": data.get("error"),
        "computed_at": computed,
    }


def current(job: dict) -> dict:
    """Tamaño cacheado del job; `pending=True` si aún no se ha medido.

    No espera a un escaneo en curso: `_warm_worker`/`job_size` hacen el recorrido
    del NAS en segundo plano tomando el lock del slug, y aquí responder con lo
    cacheado evita que /summary y /jobs se queden colgados durante esos escaneos.
    """
    if _disabled(job):
        return unmeasured()
    slug = job.get("slug") or ""
    lock = _lock_for(slug)
    if not lock.acquire(blocking=False):
        cached = _cache.get(slug)
    else:
        try:
            cached = _cache.get(slug)
        finally:
            lock.release()
    if not cached:
        return {
            "bytes": None, "files": 0, "truncated": False, "error": None,
            "computed_at": None, "cached": False, "pending": True,
        }
    return {**_public(cached), "cached": True, "pending": False}


def job_running(job: dict) -> bool:
    """¿El job tiene tomado su lockfile (lo mismo que usa cron/runner)?"""
    path = Path(job.get("lockfile") or f"/run/lock/backupcsr-{job.get('slug')}.lock")
    try:
        handle = open(path, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def job_size(job: dict, force: bool = False) -> dict:
    """Tamaño del job: caché si es fresco (< TTL) o recálculo bloqueante.

    Sin `force` no se recorre un árbol cuyo job está corriendo: el escaneo compite
    por el NAS (CIFS) y por la RAM con la propia copia y era parte de lo que dejaba
    al anfitrión sin memoria durante las corridas.
    """
    if _disabled(job):
        return unmeasured()
    slug = job.get("slug") or ""
    now = time.time()
    with _lock_for(slug):
        cached = _cache.get(slug)
        if cached and not force and (now - cached["_ts"]) < config.SIZE_TTL:
            return {**_public(cached), "cached": True, "pending": False}
        if not force and job_running(job):
            if cached:
                return {**_public(cached), "cached": True, "pending": False}
            return {
                "bytes": None, "files": 0, "truncated": False, "error": None,
                "computed_at": None, "cached": False, "pending": True,
            }
        computed = _compute(job)
        computed["_ts"] = now
        _cache[slug] = computed
        return {**_public(computed), "cached": False, "pending": False}


def invalidate(slug: str) -> None:
    """Olvida el tamaño medido (tras crear, renombrar, mover o borrar archivos)."""
    with _lock_for(slug):
        _cache.pop(slug, None)


def _warm_worker(jobs: list[dict], force: bool) -> None:
    global _refreshing
    try:
        last = len(jobs) - 1
        for index, job in enumerate(jobs):
            try:
                if _disabled(job):
                    log.debug("warm: %s está deshabilitado; se omite el escaneo", job.get("slug"))
                    continue
                if not force and job_running(job):
                    log.debug("warm: %s está corriendo; se omite el escaneo", job.get("slug"))
                    continue
                job_size(job, force=force)
            except Exception:  # noqa: BLE001
                log.exception("no se pudo medir el tamaño de %s", job.get("slug"))
            if index < last and config.SIZE_WARM_PAUSE > 0:
                time.sleep(config.SIZE_WARM_PAUSE)
    finally:
        with _refresh_lock:
            _refreshing = False


def warm(jobs: list[dict], force: bool = False) -> bool:
    """Lanza un refresco en segundo plano (no-op si ya hay uno en curso)."""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return False
        _refreshing = True
    threading.Thread(target=_warm_worker, args=(jobs, force), daemon=True).start()
    return True


def nas_stats() -> dict | None:
    if not files.nas_ready():
        return None
    try:
        stat = os.statvfs(config.NAS_MOUNT)
    except OSError:
        return None
    total = stat.f_frsize * stat.f_blocks
    free = stat.f_frsize * stat.f_bavail
    if total <= 0:
        return None
    used = total - free
    return {
        "total": total,
        "used": used,
        "free": free,
        "percent": round(used * 100 / total, 1),
    }


def save_snapshot(job_id: int, size: dict, run_id: int | None = None,
                  taken_at: datetime | None = None) -> bool:
    if not db.available() or size.get("bytes") is None:
        return False
    when = taken_at or logs.now()
    try:
        db.execute(
            """
            INSERT INTO size_snapshots (job_id, taken_at, bytes, files, run_id, truncated)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                bytes = VALUES(bytes), files = VALUES(files),
                run_id = VALUES(run_id), truncated = VALUES(truncated)
            """,
            (job_id, logs.to_naive(when), int(size.get("bytes") or 0),
             int(size.get("files") or 0), run_id, 1 if size.get("truncated") else 0),
        )
        return True
    except Exception:  # noqa: BLE001
        log.exception("no se pudo guardar el snapshot de tamaño (job_id=%s)", job_id)
        return False


def series(days: int = 30) -> list[dict]:
    """Snapshots de los últimos `days` días con el slug del job (para gráficos)."""
    if not db.available():
        return []
    days = max(1, min(int(days), 365))
    cutoff = logs.to_naive(logs.now() - timedelta(days=days))
    try:
        rows = db.query(
            """
            SELECT s.taken_at, s.bytes, s.files, s.run_id, j.slug AS job_slug
            FROM size_snapshots s JOIN jobs j ON j.id = s.job_id
            WHERE s.taken_at >= %s
            ORDER BY s.taken_at
            """,
            (cutoff,),
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo leer size_snapshots")
        return []
    return [
        {
            "taken_at": logs.from_naive(row["taken_at"]).isoformat() if row.get("taken_at") else None,
            "bytes": int(row.get("bytes") or 0),
            "files": int(row.get("files") or 0),
            "run_id": row.get("run_id"),
            "job_slug": row.get("job_slug"),
        }
        for row in rows
    ]


def _latest_total() -> int:
    try:
        row = db.query(
            """
            SELECT SUM(s.bytes) AS bytes
            FROM size_snapshots s
            JOIN (
                SELECT job_id, MAX(taken_at) AS taken_at
                FROM size_snapshots GROUP BY job_id
            ) latest ON latest.job_id = s.job_id AND latest.taken_at = s.taken_at
            """
        )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo leer el total de snapshots")
        return 0
    return int((row[0].get("bytes") or 0)) if row else 0


def growth(jobs: list[dict], windows=(7, 30)) -> dict:
    """Variación de bytes totales por ventana, comparando el snapshot más cercano."""
    if not db.available():
        return {}
    total_now = sum(int(current(job).get("bytes") or 0) for job in jobs)
    if not total_now:
        total_now = _latest_total()
    out: dict[str, dict] = {}
    for days in windows:
        cutoff = logs.to_naive(logs.now() - timedelta(days=days))
        try:
            row = db.query(
                """
                SELECT SUM(s.bytes) AS bytes
                FROM size_snapshots s
                JOIN (
                    SELECT job_id, MAX(taken_at) AS taken_at
                    FROM size_snapshots
                    WHERE taken_at <= %s
                    GROUP BY job_id
                ) latest ON latest.job_id = s.job_id AND latest.taken_at = s.taken_at
                """,
                (cutoff,),
            )
        except Exception:  # noqa: BLE001
            log.exception("no se pudo calcular el crecimiento a %s días", days)
            row = None
        before = int((row[0].get("bytes") or 0)) if row else 0
        delta = total_now - before
        out[f"d{days}"] = {
            "bytes": delta,
            "percent": round(delta * 100 / before, 1) if before else None,
            "baseline": before,
        }
    return out
