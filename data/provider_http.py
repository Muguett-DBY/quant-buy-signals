"""Small shared HTTP scheduling policy for batch data providers."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import threading
import time
from typing import Any, Callable

import requests


RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


class RequestRateLimiter:
    """Reserve globally spaced request slots across worker threads."""

    def __init__(self, interval_seconds: float) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("request interval must be a non-negative finite number")
        self._interval_seconds = float(interval_seconds)
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval_seconds
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def response_status_code(exc: BaseException, response: Any = None) -> int | None:
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_transient_request_error(exc: BaseException, response: Any = None) -> bool:
    """Return whether retrying the same request can reasonably succeed."""

    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return True
    if not isinstance(exc, requests.HTTPError):
        return False
    status = response_status_code(exc, response)
    return status is not None and (status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599)


def request_error_kind(exc: BaseException, response: Any = None) -> str:
    """Return one stable, non-sensitive label for operational diagnostics."""

    status = response_status_code(exc, response)
    if status is not None:
        return f"http_{status}"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return "chunked_response"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    return type(exc).__name__


def retry_delay_seconds(
    response: Any,
    *,
    attempt: int,
    base_seconds: float,
    maximum_seconds: float = 60.0,
    now: Callable[[], datetime] | None = None,
) -> float:
    """Use Retry-After when present, otherwise a bounded linear delay."""

    fallback = min(maximum_seconds, base_seconds * (attempt + 1))
    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return fallback
    value = str(raw).strip()
    if value.isdigit():
        return min(maximum_seconds, max(fallback, float(value)))
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = (now or (lambda: datetime.now(timezone.utc)))()
        return min(maximum_seconds, max(fallback, (retry_at - current).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return fallback
