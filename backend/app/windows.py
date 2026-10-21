from datetime import datetime, timedelta, timezone

WINDOWS = {"24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600}


def validate_window(value):
    if value not in WINDOWS:
        raise ValueError("ventana inválida")
    return value


def seconds(value):
    return WINDOWS[validate_window(value)]


def start_dt(value):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds(value))).replace(
        microsecond=0
    )


def start_iso(value):
    return start_dt(value).isoformat()


def label(seconds_value):
    if seconds_value >= 24 * 3600:
        days = round(seconds_value / 86400)
        return f"{days}d" if days != 1 else "24h"
    if seconds_value >= 3600:
        hours = max(1, round(seconds_value / 3600))
        return f"{hours}h"
    minutes = max(1, round(seconds_value / 60))
    return f"{minutes}m"
