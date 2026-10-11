"""Aplicación FastAPI del portal de copias (se ejecuta con uvicorn vía systemd)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from fastapi import FastAPI

from . import api, auth, catalog, config, db, logs, notify, sizes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backupcsr-web")


def _snapshot_sizes(jobs: list[dict]) -> None:
    """Guarda el tamaño de cada job al cerrar una corrida y, si falta, a diario."""
    if not config.SIZE_SNAPSHOT:
        return
    for job in jobs:
        job_id = job.get("id")
        if not job_id:
            row = db.query("SELECT id FROM jobs WHERE slug = %s", (job["slug"],), one=True)
            job_id = row["id"] if row else None
        if not job_id:
            continue
        last = db.query(
            """
            SELECT id, finished_at FROM runs
            WHERE job_id = %s AND finished_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (job_id,),
            one=True,
        )
        if last and not db.query(
            "SELECT id FROM size_snapshots WHERE run_id = %s LIMIT 1", (last["id"],), one=True
        ):
            size = sizes.job_size(job)
            if size.get("bytes") is not None:
                sizes.save_snapshot(job_id, size, run_id=last["id"], taken_at=last["finished_at"])
        cutoff = logs.to_naive(logs.now() - timedelta(hours=24))
        recent = db.query(
            "SELECT id FROM size_snapshots WHERE job_id = %s AND taken_at >= %s LIMIT 1",
            (job_id, cutoff),
            one=True,
        )
        if not recent:
            size = sizes.job_size(job)
            if size.get("bytes") is not None:
                sizes.save_snapshot(job_id, size)


async def _poll() -> None:
    while True:
        try:
            if db.available():
                jobs = api.effective_jobs()
                for job in jobs:
                    logs.store_runs(job, logs.parse_slug(job["slug"]))
                _snapshot_sizes(jobs)
                sizes.warm(jobs)
                # Aviso externo: fatal al abrirse el incidente y recuperación al resolverse.
                # Fuera del bucle de eventos: el correo hace I/O bloqueante (SMTP/sendmail).
                await asyncio.to_thread(notify.flush_immediate)
                await asyncio.to_thread(notify.flush_recovery)
                await asyncio.to_thread(notify.flush_push)
        except Exception:  # noqa: BLE001
            log.exception("falló el ciclo de sondeo")
        await asyncio.sleep(config.POLL_INTERVAL)


def _bootstrap() -> None:
    config.warn_if_insecure()
    if not db.available():
        log.warning(
            "MySQL no disponible al arrancar (%s); el portal queda en modo lectura "
            "desde el filesystem hasta que vuelva la BD",
            db.last_error(),
        )
        return
    try:
        db.init_schema()
        catalog.sync_jobs()
        sizes.warm(api.effective_jobs())
    except Exception:  # noqa: BLE001
        log.exception("no se pudo inicializar el esquema/catálogo")
        return
    try:
        if auth.user_count() == 0:
            if config.ADMIN_PASSWORD:
                auth.create_user(config.ADMIN_USER, config.ADMIN_PASSWORD, "admin")
                log.info("usuario admin '%s' creado", config.ADMIN_USER)
            else:
                log.warning(
                    "no hay usuarios y BACKUP_ADMIN_PASSWORD no está definida; "
                    "crea uno con: venv/bin/python -m app.cli create-admin"
                )
    except Exception:  # noqa: BLE001
        log.exception("no se pudo verificar/crear el usuario admin")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    _bootstrap()
    task = asyncio.create_task(_poll())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="CortexDev Backup API", lifespan=lifespan)
app.include_router(api.router)
