"""Credenciales de los orígenes de copia (/etc/backupcsr/credentials.env).

El portal nunca devuelve valores de credenciales: solo lista los grupos por su
prefijo (p. ej. RUTA56) para reutilizarlos. Al crear un grupo nuevo, escribe las
líneas <PREFIX>_HOST/_PORT/_USER/_PASS de forma atómica (0600) sin tocar el resto
del archivo. La llave SFTP (nombre de archivo bajo /etc/backupcsr/keys/) no es un
secreto y viaja en la definición de la tarea, no aquí.
"""
from __future__ import annotations

import logging
import os
import re

from . import config

log = logging.getLogger("backupcsr-web")

PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_HOST_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)_HOST\s*=")


def valid_prefix(prefix: str) -> bool:
    return bool(prefix) and bool(PREFIX_RE.match(prefix))


def list_groups() -> list[dict]:
    """Grupos de credenciales por prefijo, sin filtrar ningún valor."""
    path = config.CREDENTIALS_FILE
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    prefixes = set()
    for line in text.splitlines():
        match = _HOST_RE.match(line)
        if match:
            prefixes.add(match.group(1))
    groups = []
    for prefix in sorted(prefixes):
        groups.append({
            "prefix": prefix,
            "has_pass": bool(re.search(rf"^\s*(?:export\s+)?{prefix}_PASS\s*=", text, re.M)),
        })
    return groups


def prefix_exists(prefix: str) -> bool:
    return any(group["prefix"] == prefix for group in list_groups())


def _set_line(lines: list[str], key: str, value: str) -> list[str]:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=.*$")
    new_line = f"{key}={value}"
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = new_line
            return lines
    lines.append(new_line)
    return lines


def _quote(value: str) -> str:
    """Siempre entre comillas simples ('' -> '\\''), sin importar el contenido.

    credentials.env se 'sourcea' como root al correr cada copia (`load_credentials`),
    así que cualquier metacarácter de shell (;, &, |, `, $, …) debe quedar literal.
    """
    return "'" + str(value).replace("'", "'\\''") + "'"


def save_group(prefix: str, host: str, port: str | None, user: str,
               password: str | None = None) -> None:
    """Añade/actualiza las variables de un grupo (atómico, 0600)."""
    if not valid_prefix(prefix):
        raise ValueError(f"prefijo de credenciales inválido: {prefix}")
    if not host:
        raise ValueError("el host no puede estar vacío")

    path = config.CREDENTIALS_FILE
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = [
            "# Credenciales de las copias de seguridad (editado por backupcsr-web).",
            "# Generado/actualizado por el portal; chmod 600 root:root.",
        ]
    lines = _set_line(lines, f"{prefix}_HOST", _quote(host))
    if port:
        lines = _set_line(lines, f"{prefix}_PORT", _quote(str(port)))
    if user:
        lines = _set_line(lines, f"{prefix}_USER", _quote(user))
    if password:
        lines = _set_line(lines, f"{prefix}_PASS", _quote(password))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        os.chown(path, 0, 0)
    except (OSError, PermissionError):
        pass
    log.info("credenciales guardadas para el grupo %s", prefix)
