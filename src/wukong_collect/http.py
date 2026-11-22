from __future__ import annotations

import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class HttpClient:
    """Serial, throttled reads. Errors never include query strings or credentials."""

    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("network", config or {})
        self.timeout = float(cfg.get("timeout", 30))
        self.interval = float(cfg.get("min_interval", 1.2))
        self.retries = int(cfg.get("retries", 4))
        self.max_wait = float(cfg.get("max_retry_wait", 60))
        if self.timeout <= 0 or self.interval < 0 or self.retries < 0 or self.max_wait < 0:
            raise ValueError("network 的 timeout/min_interval/retries/max_retry_wait 配置无效")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.get("user_agent", "WukongAcademicCollector/0.1")
        proxy = os.environ.get("COLLECT_PROXY")
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.last_request = 0.0
        self.requests_made = 0

    def close(self):
        self.session.close()

    @staticmethod
    def retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                return None

    def get_json(self, url, params=None, headers=None):
        host = urlsplit(url).hostname or "API"
        for attempt in range(self.retries + 1):
            wait = self.interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.requests_made += 1
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException:
                if attempt < self.retries:
                    time.sleep(min(self.max_wait, 2 ** attempt + random.random()))
                    continue
                raise ApiError(f"{host} 连接失败；检查网络或 .env 中 COLLECT_PROXY") from None
            retry = response.status_code == 429 or response.status_code in {500, 502, 503, 504}
            if retry and attempt < self.retries:
                delay = self.retry_after(response.headers.get("Retry-After"))
                if delay is None and response.status_code == 429:
                    # Arctic Shift advertises its dynamic limit using these
                    # headers rather than Retry-After.
                    delay = self.retry_after(response.headers.get("X-RateLimit-Reset"))
                    if delay is None:
                        try:
                            reset_at = float(response.headers["X-RateLimit-Reset-At"])
                            delay = max(0.0, reset_at - time.time())
                        except (KeyError, TypeError, ValueError):
                            pass
                if delay is not None and delay > self.max_wait:
                    raise ApiError(f"{host} 要求稍后重试；断点已保留", response.status_code, "retry_later")
                time.sleep(delay if delay is not None else min(self.max_wait, 2 ** attempt + random.random()))
                continue
            try:
                payload = response.json()
            except ValueError:
                raise ApiError(f"{host} 返回非 JSON（HTTP {response.status_code}）", response.status_code, "invalid_json") from None
            if not response.ok:
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                errors = error.get("errors", []) if isinstance(error, dict) else []
                reason = str(errors[0].get("reason", "")) if errors and isinstance(errors[0], dict) else "http_error"
                if response.status_code == 422 and isinstance(error, str) and error.strip().casefold() in {
                    "timeout. maybe slow down a bit", "query timed out", "query timed out."
                }:
                    reason = "query_timeout"
                # Keep only a machine reason, never arbitrary server text that can echo secrets.
                reason = reason if re.fullmatch(r"[A-Za-z0-9_]{1,80}", reason) else "http_error"
                raise ApiError(f"{host}: HTTP {response.status_code} ({reason})", response.status_code, reason)
            return payload
        raise AssertionError("unreachable")
