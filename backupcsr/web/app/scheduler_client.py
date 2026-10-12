"""Cliente del socket Unix del planificador (app/scheduler.py).

Lo usan el portal (`app/runner.py`) y el CLI (`app/scheduler_cli.py`). Si el socket
no responde se levanta `SchedulerUnavailable`, que el llamador interpreta como
"planificador caído" para caer a la ejecución directa serial.
"""
from __future__ import annotations

import json
import socket

from . import config


class SchedulerUnavailable(ConnectionError):
    """El socket del planificador no está disponible."""


def request(payload: dict, timeout: float = 3.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(config.SCHEDULER_SOCKET))
        except OSError as exc:
            raise SchedulerUnavailable(str(exc)) from exc
        try:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError as exc:
            # timeout o corte tras conectar: se trata como planificador no disponible
            # para que el llamador pueda caer al modo degradado.
            raise SchedulerUnavailable(f"sin respuesta del planificador: {exc}") from exc
    finally:
        sock.close()
    try:
        return json.loads(data.decode("utf-8") or "{}")
    except ValueError:
        return {"ok": False, "error": "respuesta inválida del planificador"}


def submit(slug: str, action: str = "run", trigger: str = "manual",
           username: str = "", dry_run: bool = False) -> dict:
    return request({
        "op": "submit", "slug": slug, "action": action, "trigger": trigger,
        "username": username, "dry_run": dry_run,
    })


def status(timeout: float = 3.0) -> dict:
    return request({"op": "status"}, timeout=timeout)


def cancel(slug: str) -> dict:
    return request({"op": "cancel", "slug": slug})
