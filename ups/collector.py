"""NUT poller: reads the UPS with `upsc` every POLL_INTERVAL seconds.

Stores raw samples in SQLite, detects status changes, and broadcasts the
latest summary to WebSocket subscribers (served by main.py).
"""

import asyncio
import json
import math
import os
import re
import time
import urllib.request

import db
import notify

UPS_HOST = os.environ.get("UPS_HOST", "192.168.0.224")
UPS_NAME = os.environ.get("UPS_NAME", "apc")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
UPSC_TIMEOUT = int(os.environ.get("UPSC_TIMEOUT", "10"))
SIMULATE = os.environ.get("SIMULATE", "0") == "1"
HTTP_FALLBACK = os.environ.get("HTTP_FALLBACK", "1") == "1"
UPS_CGI_URL = os.environ.get(
    "UPS_CGI_URL",
    f"http://{UPS_HOST}/cgi-bin/nut/upsstats.cgi?host={UPS_NAME}@localhost&treemode",
)
PORTAL_STATUS_URL = os.environ.get("PORTAL_STATUS_URL", "http://127.0.0.1:8088/api/status")
PORTAL_TIMEOUT = int(os.environ.get("PORTAL_TIMEOUT", "5"))
SERVICES_POLL = int(os.environ.get("SERVICES_POLL", "30"))

# Estado de servicios externos -> slug del catalogo de portal-api.
SERVICE_MAP = {"pve": "proxmox", "pbs": "backups", "storage": "copias"}

# NUT variable -> stored column
WATCH = {
    "battery.charge": "battery_charge",
    "battery.runtime": "battery_runtime",
    "ups.load": "ups_load",
    "input.voltage": "input_voltage",
    "battery.voltage": "battery_voltage",
}

STATUS_LABELS = {
    "OL": "En linea",
    "OB": "En bateria",
    "LB": "Bateria baja",
    "HB": "Bateria alta",
    "RB": "Reemplazar bateria",
    "CHRG": "Cargando",
    "DISCHRG": "Descargando",
    "BYPASS": "Bypass",
    "CAL": "Calibrando",
    "OFF": "Apagada",
    "ALARM": "Alarma",
    "TRIM": "Regulando (baja)",
    "BOOST": "Regulando (alta)",
    "OVER": "Sobrecarga",
    "FSD": "Apagado forzado",
}

_state = {"summary": None, "error": None, "error_ts": None, "source": None, "services": None}
_subscribers = set()
_last_status = None
_services_ts = 0.0


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def status_label(status):
    if not status:
        return "Desconocido"
    labels = [STATUS_LABELS.get(tok, tok) for tok in status.split()]
    return " / ".join(labels) if labels else status


def status_class(status):
    s = (status or "").upper()
    tokens = set(s.split())
    if tokens & {"OB", "LB", "HB", "RB", "FSD", "OFF", "ALARM", "OVER"}:
        return "bad"
    if tokens & {"BYPASS", "CAL", "TRIM", "BOOST"}:
        return "warn"
    return "ok"


def format_runtime(seconds):
    if seconds is None:
        return "--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


async def _run_upsc():
    proc = await asyncio.create_subprocess_exec(
        "upsc",
        f"{UPS_NAME}@{UPS_HOST}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=UPSC_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"upsc timeout ({UPSC_TIMEOUT}s)")
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip() or f"upsc exit {proc.returncode}")
    data = {}
    for line in out.decode(errors="replace").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


def _run_http():
    """Fallback: scrape the NUT data-tree CGI (read-only, no upsd exposure)."""
    with urllib.request.urlopen(UPS_CGI_URL, timeout=UPSC_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", "replace")
    data = {}
    for var, value in re.findall(r"<TD>([^<]+)</TD>\s*<TD>:</TD>\s*<TD>([^<]*)<br", html, re.I):
        data[var.strip()] = value.strip()
    if not data:
        raise RuntimeError("respuesta CGI vacia")
    return data


def _portal_services():
    """Estado de PVE/PBS/storage leido de portal-api (best-effort)."""
    fallback = {key: {"state": "unknown"} for key in SERVICE_MAP}
    try:
        with urllib.request.urlopen(PORTAL_STATUS_URL, timeout=PORTAL_TIMEOUT) as resp:
            payload = json.load(resp)
    except Exception:  # noqa: BLE001 - nunca romper el summary por portal-api
        return fallback
    statuses = payload.get("statuses") or {}
    out = {}
    for key, slug in SERVICE_MAP.items():
        info = statuses.get(slug) or {}
        out[key] = {
            "state": info.get("state", "unknown"),
            "code": info.get("code"),
            "latency_ms": info.get("latency_ms"),
        }
    return out


async def _refresh_services(force=False):
    global _services_ts
    now = time.time()
    if not force and (now - _services_ts) < SERVICES_POLL:
        return
    _services_ts = now
    _state["services"] = await asyncio.to_thread(_portal_services)


async def _read_ups():
    if SIMULATE:
        _state["source"] = "simulate"
        return _simulate()
    try:
        raw = await _run_upsc()
        _state["source"] = "nut"
        return raw
    except Exception:  # noqa: BLE001
        if not HTTP_FALLBACK:
            raise
        raw = await asyncio.to_thread(_run_http)
        _state["source"] = "http"
        return raw


def _simulate():
    """Synthetic data so the UI can be developed without touching the PVE."""
    period = 600.0
    phase = (time.time() % period) / period
    on_battery = 0.45 <= phase <= 0.72
    if on_battery:
        charge = 100 - (phase - 0.45) / 0.27 * 55
        status = "OB DISCHRG" if charge > 20 else "OB LB"
    else:
        charge = min(100.0, 45 + (phase % 0.45) / 0.45 * 55)
        status = "OL CHRG" if charge < 99 else "OL"
    runtime = max(120, charge * 32)
    load = 12 + 6 * math.sin(time.time() / 120)
    return {
        "ups.model": "Back-UPS RS 1500M2",
        "ups.mfr": "American Power Conversion",
        "ups.serial": "3B1948X28037",
        "ups.firmware": "962.f2 .D",
        "ups.status": status,
        "ups.load": f"{load:.0f}",
        "ups.realpower.nominal": "900",
        "battery.charge": f"{charge:.0f}",
        "battery.charge.low": "10",
        "battery.charge.warning": "50",
        "battery.runtime": f"{runtime:.0f}",
        "battery.runtime.low": "120",
        "battery.voltage": f"{26.0 + charge / 100:.1f}",
        "battery.voltage.nominal": "24.0",
        "input.voltage": f"{131.0 + math.sin(time.time() / 60):.1f}",
        "input.voltage.nominal": "120",
        "input.transfer.low": "78",
        "input.transfer.high": "150",
        "ups.beeper.status": "disabled",
        "ups.test.result": "No test initiated",
    }


def _build_summary(ts, raw, values):
    load = values.get("ups_load")
    realpower = _num(raw.get("ups.realpower.nominal")) or 900
    runtime = values.get("battery_runtime")
    return {
        "ts": ts,
        "model": raw.get("ups.model", "UPS"),
        "mfr": raw.get("ups.mfr"),
        "serial": raw.get("ups.serial"),
        "firmware": raw.get("ups.firmware"),
        "status": raw.get("ups.status", ""),
        "status_label": status_label(raw.get("ups.status")),
        "status_class": status_class(raw.get("ups.status")),
        "battery_charge": values.get("battery_charge"),
        "charge_warning": _num(raw.get("battery.charge.warning")) or 50,
        "charge_low": _num(raw.get("battery.charge.low")) or 10,
        "runtime_s": runtime,
        "runtime_human": format_runtime(runtime),
        "runtime_low": _num(raw.get("battery.runtime.low")) or 120,
        "load": load,
        "watts_est": round(load * realpower / 100, 1) if load is not None else None,
        "realpower_nominal": realpower,
        "input_voltage": values.get("input_voltage"),
        "transfer_low": _num(raw.get("input.transfer.low")),
        "transfer_high": _num(raw.get("input.transfer.high")),
        "battery_voltage": values.get("battery_voltage"),
        "battery_voltage_nominal": _num(raw.get("battery.voltage.nominal")),
        "input_voltage_nominal": _num(raw.get("input.voltage.nominal")),
        "beeper": raw.get("ups.beeper.status"),
        "test_result": raw.get("ups.test.result"),
        "driver_name": raw.get("driver.name"),
        "driver_version": raw.get("driver.version"),
        "usb_vendorid": raw.get("ups.vendorid"),
        "usb_productid": raw.get("ups.productid"),
        "services": _state.get("services") or {},
    }


def _broadcast(summary):
    for queue in list(_subscribers):
        try:
            queue.put_nowait(summary)
        except asyncio.QueueFull:
            pass


def subscribe():
    queue = asyncio.Queue(maxsize=10)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue):
    _subscribers.discard(queue)


def latest():
    return _state["summary"]


def last_error():
    if _state["error"] is None:
        return None
    return {"message": _state["error"], "ts": _state["error_ts"]}


def source():
    return _state.get("source")


def _tokens(value):
    return {token for token in (value or "").upper().split() if token}


def _publish_status(prev, cur):
    """Publica transiciones de estado relevantes (corte, restauración, batería baja)."""
    before = _tokens(prev)
    after = _tokens(cur)
    on_before = bool(before & {"OB", "DISCHRG"})
    on_after = bool(after & {"OB", "DISCHRG"})
    if on_after and not on_before:
        notify.publish("ups.on_battery", "Corte de energía",
                       body="La UPS está funcionando con batería.", dedupe_key="battery")
    elif on_before and not on_after:
        notify.publish("ups.restored", "Energía restaurada",
                       body="La UPS vuelve a la red eléctrica.", dedupe_key="battery")
    if (after & {"LB"}) and not (before & {"LB"}):
        notify.publish("ups.low_battery", "Batería baja",
                       body="La UPS reporta batería baja.", dedupe_key="lb")


async def collect_once():
    global _last_status
    was_error = bool(_state.get("error"))
    try:
        raw = await _read_ups()
    except Exception as exc:  # noqa: BLE001 - best-effort polling
        _state["error"] = str(exc)
        _state["error_ts"] = int(time.time())
        notify.publish("ups.comm_lost", "Sin comunicación con la UPS",
                       body=f"No se pudo leer la UPS: {exc}", dedupe_key="comm")
        return None

    if was_error:
        notify.publish("ups.comm_restored", "Comunicación con la UPS restaurada",
                       body="La UPS vuelve a responder.", dedupe_key="comm")
    _state["error"] = None
    ts = int(time.time())
    values = {col: _num(raw.get(var)) for var, col in WATCH.items()}
    status = (raw.get("ups.status") or "").strip() or None
    await asyncio.to_thread(db.insert_sample, ts, values, status)

    if status and _last_status and status != _last_status:
        await asyncio.to_thread(db.record_event, ts, _last_status, status)
    if status:
        _publish_status(_last_status, status)
        _last_status = status

    summary = _build_summary(ts, raw, values)
    _state["summary"] = summary
    _broadcast(summary)
    return summary


async def run():
    global _last_status, _services_ts
    db.init_db()
    _last_status = db.last_status()
    await _refresh_services(force=True)
    cycle = 0
    while True:
        await collect_once()
        await _refresh_services()
        cycle += 1
        if cycle % 20 == 0:
            try:
                await asyncio.to_thread(db.rollup)
                await asyncio.to_thread(db.prune)
            except Exception as exc:  # noqa: BLE001
                _state["error"] = f"maintenance: {exc}"
                _state["error_ts"] = int(time.time())
        await asyncio.sleep(POLL_INTERVAL)
