"""Generador de scripts de copia (.sh) a partir de la ficha de una tarea.

Produce un script que usa las funciones de `lib/common.sh` (mirror_ftp /
mirror_sftp / mirror_sftp_pass) espejando cada ruta al NAS, con exclusiones glob
opcionales. El script se valida con `bash -n` y se instala en
/opt/backupcsr/jobs/<slug>.sh (0755, con respaldo previo).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

from . import config

log = logging.getLogger("backupcsr-web")


def shq(value: str) -> str:
    """Comillas simples para un literal de shell."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _var(prefix: str, suffix: str) -> str:
    return f'"${prefix}_{suffix}"'


def _mirror_call(job: dict, route: dict, exclusions: list[str]) -> str:
    origin_type = job.get("origin_type") or "ftp"
    prefix = job.get("cred_prefix") or "ORIGEN"
    source = str(route.get("source") or "").strip()
    dest_rel = str(route.get("dest_rel") or "").strip("/")
    local = f'"$BACKUPCSR_NAS_ROOT/{dest_rel}"'
    excl = "".join(" " + shq(item) for item in exclusions if item)

    if origin_type == "sftp":
        key_name = str(job.get("key_name") or "").strip("/")
        key = f'"$BACKUPCSR_KEYS/{key_name}"'
        call = (
            f"mirror_sftp {_var(prefix, 'HOST')} "
            f"\"${{{prefix}_PORT:-22}}\" {_var(prefix, 'USER')} {key} \\\n"
            f"\t{shq(source)} {local}{excl}"
        )
    elif origin_type == "sftp_pass":
        call = (
            f"mirror_sftp_pass {_var(prefix, 'HOST')} "
            f"\"${{{prefix}_PORT:-22}}\" {_var(prefix, 'USER')} {_var(prefix, 'PASS')} \\\n"
            f"\t{shq(source)} {local}{excl}"
        )
    else:
        call = (
            f"mirror_ftp {_var(prefix, 'HOST')} {_var(prefix, 'USER')} {_var(prefix, 'PASS')} \\\n"
            f"\t{shq(source)} {local}{excl}"
        )
    return call


def _comment(value: str) -> str:
    """Texto seguro para un comentario '#' (sin saltos de línea ni control)."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()


def render_script(job: dict) -> str:
    slug = job["slug"]
    routes = job.get("routes") or []
    exclusions = list(job.get("exclusions") or [])
    lines = [
        "#!/usr/bin/env bash",
        f"# {_comment(job.get('name') or slug)}: tarea creada desde el portal de copias.",
        f"# {_comment(job.get('description') or '')}".rstrip(),
    ]
    gate = job.get("gate_wait")
    if gate:
        lines.append(f'GATE_WAIT="${{GATE_WAIT:-{int(gate)}}}"')
    lines.append('set -euo pipefail')
    lines.append('. "${BACKUPCSR_LIB:-/opt/backupcsr/lib/common.sh}"')
    lines.append("")
    lines.append(f'job_init {shq(slug)}')
    lines.append("load_credentials")
    lines.append("")
    for route in routes:
        lines.append(_mirror_call(job, route, exclusions))
        lines.append("")
    lines.append(f'log "=== fin {slug} ==="')
    lines.append("")
    return "\n".join(lines)


def validate_script(text: str) -> tuple[bool, str]:
    """Comprueba la sintaxis con `bash -n`. Sin bash disponible, no bloquea."""
    if not shutil.which("bash"):
        return True, ""
    handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    try:
        result = subprocess.run(
            ["bash", "-n", handle.name], capture_output=True, text=True, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"no se pudo validar el script: {exc}"
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    if result.returncode != 0:
        return False, (result.stderr or "sintaxis bash inválida").strip()
    return True, ""


def _backup_script(slug: str) -> None:
    path = config.JOBS_DIR / f"{slug}.sh"
    if not path.is_file():
        return
    try:
        config.JOBS_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(config.JOBS_BACKUPS_DIR, 0o700)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, config.JOBS_BACKUPS_DIR / f"{slug}.{stamp}.sh")
    except OSError as exc:
        log.warning("no se pudo respaldar el script %s: %s", slug, exc)


def install_script(slug: str, text: str) -> str:
    """Instala /opt/backupcsr/jobs/<slug>.sh (atómico, 0755, con respaldo)."""
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _backup_script(slug)
    target = config.JOBS_DIR / f"{slug}.sh"
    tmp = config.JOBS_DIR / f".{slug}.sh.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    log.info("script instalado: %s", target)
    return str(target)


def remove_script(slug: str) -> bool:
    path = config.JOBS_DIR / f"{slug}.sh"
    if not path.is_file():
        return False
    _backup_script(slug)
    try:
        path.unlink()
    except OSError as exc:
        log.warning("no se pudo borrar el script %s: %s", path, exc)
        return False
    return True


_MIRROR_RE = re.compile(r"\bmirror_[a-z][a-z0-9_]*\b")
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_PREFIX_RE = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)_HOST\b")


def parse_prefix(text: str) -> str:
    """Prefijo de credenciales referenciado por el script ($PREFIJO_HOST), o ""."""
    match = _PREFIX_RE.search(text or "")
    return match.group(1) if match else ""


def parse_routes(text: str) -> list[dict]:
    """Reconstrucción best-effort de rutas desde un script existente (para clonar).

    No interpreta shell: junta las continuaciones con `\\`, busca llamadas mirror_*
    y toma como destino la cadena que contiene BACKUPCSR_NAS_ROOT y como origen la
    cadena anterior. En tareas atípicas puede fallar; es solo una precarga y el
    usuario siempre revisa/edita las rutas.
    """
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        logical.append(buffer.strip())

    routes: list[dict] = []
    for line in logical:
        if not _MIRROR_RE.search(line):
            continue
        tokens = _QUOTED_RE.findall(line)
        dest_idx = next(
            (i for i, token in enumerate(tokens) if "BACKUPCSR_NAS_ROOT" in token), None
        )
        if dest_idx is None or dest_idx == 0:
            continue
        dest_rel = tokens[dest_idx].split("BACKUPCSR_NAS_ROOT", 1)[-1].lstrip("/")
        routes.append({"source": tokens[dest_idx - 1], "dest_rel": dest_rel})
    return routes
