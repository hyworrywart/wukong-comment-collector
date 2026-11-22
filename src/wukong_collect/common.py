from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any


def utc_iso(value: Any = None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time(), timezone.utc)
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.replace('.', '', 1).isdigit()):
        dt = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Naive dataset timestamps are interpreted as UTC, as documented in config.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def in_window(value: Any, config: dict) -> bool:
    if value in (None, ""):
        return False
    t = utc_iso(value)
    scope = config.get("collection", {})
    return (not scope.get("start") or t >= utc_iso(scope["start"])) and (
        not scope.get("end") or t < utc_iso(scope["end"]))


def job_key(platform: str, settings: dict) -> str:
    payload = json.dumps(settings, sort_keys=True, ensure_ascii=False, default=str)
    return platform + ":" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or int(value) < 1:
        raise ValueError(f"{name} 必须是正整数")
    return int(value)
