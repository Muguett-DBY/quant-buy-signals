"""Strict whole-market evidence for Type-2 market coldness.

The source is Eastmoney's paginated ``push2`` quote list.  One acquisition
walks the complete Shanghai/Shenzhen A-share result set; it never performs a
request per security and it deliberately excludes the Beijing Stock Exchange.

Missing market observations remain ``None`` with an explicit upstream reason.
Transport, pagination and schema failures are exposed as a structured
unavailable snapshot instead of being converted into plausible zeroes.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS, CONCURRENCY, REQUEST_TIMEOUT
from data.cache import SafeCacheConflict, SafeCacheError, SafeFileCache


EASTMONEY_CLIST_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/clist/get"
EASTMONEY_SOURCE = "Eastmoney push2 clist"
EASTMONEY_SOURCE_ID = "eastmoney_push2_sh_sz_a_coldness"
EASTMONEY_UNIVERSE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
EASTMONEY_FIELDS = ("f12", "f13", "f14", "f24", "f25", "f8", "f10", "f26")

METRIC_SOURCE_FIELDS: Mapping[str, str] = {
    "change_60d_pct": "f24",
    "change_ytd_pct": "f25",
    "turnover_rate_pct": "f8",
    "volume_ratio": "f10",
    "listing_date": "f26",
}

# The public clist service currently caps a page at 100 rows even when a
# larger ``pz`` is requested.  Pinning the effective size makes every page
# length and the calculated final page independently verifiable.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_WORKERS = 10
DEFAULT_PAGE_WORKERS = max(1, min(int(CONCURRENCY), MAX_PAGE_WORKERS))
DEFAULT_MARKET_COLDNESS_CACHE_PATH = CACHE_DIRECTORY / "market_coldness" / "eastmoney_sh_sz_a.json.gz"
DEFAULT_MARKET_COLDNESS_SESSION_CACHE_DIRECTORY = CACHE_DIRECTORY / "market_coldness" / "sessions"
_SESSION_ARCHIVE_READY_TIME = datetime_time(15, 15)
_RECOVERY_TIMEOUT_FLOOR_SECONDS = 30.0
_RECOVERY_RETRIES = 2
_MAX_RECOVERY_PAGES = 5
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})

_CACHE_SCHEMA_VERSION = 1
_MAX_PAGE_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ACQUISITION_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_ROWS = 10_000
_MAX_PAGES = 64
_MAX_CACHE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_SIX_DIGIT_CODE = re.compile(r"[0-9]{6}")
_STRICT_UINT = re.compile(r"0|[1-9][0-9]*")
_CALLBACK_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_PLACEHOLDERS = {"", "-", "--"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    "Accept": "application/json,text/javascript,*/*;q=0.8",
}


class MarketColdnessError(RuntimeError):
    """The source response cannot prove a complete, valid market snapshot."""


class _MarketColdnessResourceLimitError(MarketColdnessError):
    """A fixed local byte, page or row budget was exceeded."""


class _MarketColdnessTransientTransportError(MarketColdnessError):
    """A page exhausted retries using only recoverable transport failures."""


class _AcquisitionByteBudget:
    """Thread-safe body-byte budget shared by every acquisition attempt."""

    def __init__(self, limit: int):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("acquisition byte limit must be a positive integer")
        self.limit = limit
        self._consumed = 0
        self._exhausted = False
        self._lock = Lock()

    def charge(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("acquisition byte charge must be a non-negative integer")
        with self._lock:
            if self._exhausted:
                raise _MarketColdnessResourceLimitError(
                    f"Eastmoney acquisition attempts already exceed byte limit: {self._consumed} > {self.limit}"
                )
            next_total = self._consumed + size
            self._consumed = next_total
            if next_total > self.limit:
                # The chunk has already been yielded by the HTTP client.  Latch
                # exhaustion while holding the lock so no concurrent reader
                # can accept another chunk after the first over-limit charge.
                self._exhausted = True
                raise _MarketColdnessResourceLimitError(
                    f"Eastmoney acquisition attempts exceed byte limit: {next_total} > {self.limit}"
                )

    def raise_if_exhausted(self) -> None:
        """Reject a new network attempt once no response-body budget remains."""

        with self._lock:
            if self._exhausted or self._consumed >= self.limit:
                self._exhausted = True
                comparator = ">" if self._consumed > self.limit else ">="
                raise _MarketColdnessResourceLimitError(
                    f"Eastmoney acquisition attempts reached byte limit: {self._consumed} {comparator} {self.limit}"
                )


def _is_transient_transport_error(exc: BaseException | None) -> bool:
    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return True
    if not isinstance(exc, requests.HTTPError):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status in _RETRYABLE_HTTP_STATUSES or 500 <= status <= 599)


@dataclass(frozen=True)
class MarketColdnessRecord:
    """One validated Shanghai/Shenzhen A-share coldness observation."""

    code: str
    exchange: str
    eastmoney_market_id: int
    name: str | None
    change_60d_pct: float | None
    change_ytd_pct: float | None
    turnover_rate_pct: float | None
    volume_ratio: float | None
    listing_date: str | None
    source: str
    source_url: str
    retrieved_at: str
    upstream_fields: Mapping[str, Any]
    missing_reasons: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["upstream_fields"] = dict(self.upstream_fields)
        value["missing_reasons"] = dict(self.missing_reasons)
        return value


@dataclass(frozen=True)
class MetricCoverage:
    present: int
    missing: int
    coverage_rate: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketColdnessCoverage:
    total_records: int
    complete_records: int
    complete_record_rate: float | None
    by_metric: Mapping[str, MetricCoverage]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_records": self.total_records,
            "complete_records": self.complete_records,
            "complete_record_rate": self.complete_record_rate,
            "by_metric": {name: value.to_dict() for name, value in self.by_metric.items()},
        }


@dataclass(frozen=True)
class MarketColdnessBatch:
    """Successful adapter output before cache orchestration."""

    records: tuple[MarketColdnessRecord, ...]
    retrieved_at: str
    total_expected: int
    page_count: int
    response_bytes: int


@dataclass(frozen=True)
class MarketColdnessSnapshot:
    """Available whole-market evidence or a structured acquisition failure."""

    available: bool
    records: tuple[MarketColdnessRecord, ...]
    source: str
    source_url: str
    retrieved_at: str | None
    total_expected: int | None
    fetched_count: int
    page_count: int
    response_bytes: int
    universe_coverage_rate: float | None
    coverage: MarketColdnessCoverage
    cache_hit: bool
    cache_diagnostic: str
    reason: str
    failure: Mapping[str, str] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "records": [record.to_dict() for record in self.records],
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "total_expected": self.total_expected,
            "fetched_count": self.fetched_count,
            "page_count": self.page_count,
            "response_bytes": self.response_bytes,
            "universe_coverage_rate": self.universe_coverage_rate,
            "coverage": self.coverage.to_dict(),
            "cache_hit": self.cache_hit,
            "cache_diagnostic": self.cache_diagnostic,
            "reason": self.reason,
            "failure": dict(self.failure) if self.failure is not None else None,
        }


class MarketColdnessAdapter(Protocol):
    """Adapter boundary used by the cache orchestrator and fixed tests."""

    def fetch_all(self) -> MarketColdnessBatch: ...


def _error_label(exc: BaseException, *, limit: int = 240) -> str:
    message = " ".join(str(exc).split())
    label = type(exc).__name__
    return f"{label}:{message[:limit]}" if message else label


def _strict_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise MarketColdnessError(f"Eastmoney {field} must be a non-negative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and _STRICT_UINT.fullmatch(value.strip()):
        result = int(value.strip())
    else:
        raise MarketColdnessError(f"Eastmoney {field} must be a non-negative integer")
    if result < 0:
        raise MarketColdnessError(f"Eastmoney {field} must be a non-negative integer")
    return result


def _json_constant_rejected(value: str) -> Any:
    raise ValueError(f"non-standard JSON number {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _decode_json_or_jsonp(raw: bytes, callback: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarketColdnessError("Eastmoney response is not UTF-8") from exc
    stripped = text.strip()
    if stripped.startswith("{"):
        payload_text = stripped
    else:
        if not _CALLBACK_NAME.fullmatch(callback):
            raise MarketColdnessError("invalid local JSONP callback name")
        prefix = f"{callback}("
        if not stripped.startswith(prefix):
            raise MarketColdnessError("Eastmoney JSONP callback does not match the request")
        suffix_trimmed = stripped[:-1] if stripped.endswith(";") else stripped
        if not suffix_trimmed.endswith(")"):
            raise MarketColdnessError("Eastmoney JSONP response has an invalid suffix")
        payload_text = suffix_trimmed[len(prefix) : -1]
    try:
        return json.loads(
            payload_text,
            parse_constant=_json_constant_rejected,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError) as exc:
        raise MarketColdnessError("Eastmoney response contains invalid JSON") from exc


def _read_bounded_response(
    response: Any,
    *,
    acquisition_budget: _AcquisitionByteBudget | None = None,
) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        text = str(declared).strip()
        if not _STRICT_UINT.fullmatch(text):
            raise MarketColdnessError("Eastmoney response has invalid Content-Length")
        if int(text) > _MAX_PAGE_RESPONSE_BYTES:
            raise _MarketColdnessResourceLimitError("Eastmoney page response exceeds byte limit")

    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        chunks: list[bytes] = []
        received = 0
        for chunk in iterator(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise MarketColdnessError("Eastmoney response yielded non-byte content")
            received += len(chunk)
            if acquisition_budget is not None:
                acquisition_budget.charge(len(chunk))
            if received > _MAX_PAGE_RESPONSE_BYTES:
                raise _MarketColdnessResourceLimitError("Eastmoney page response exceeds byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    content = getattr(response, "content", None)
    if not isinstance(content, bytes):
        raise MarketColdnessError("Eastmoney response does not expose a byte body")
    if acquisition_budget is not None:
        acquisition_budget.charge(len(content))
    if len(content) > _MAX_PAGE_RESPONSE_BYTES:
        raise _MarketColdnessResourceLimitError("Eastmoney page response exceeds byte limit")
    return content


def _optional_number(
    row: Mapping[str, Any],
    field: str,
    *,
    nonnegative: bool = False,
) -> tuple[float | None, str | None]:
    if field not in row:
        return None, f"upstream_field_absent:{field}"
    raw = row[field]
    if raw is None:
        return None, f"upstream_null:{field}"
    if isinstance(raw, str) and raw.strip() in _PLACEHOLDERS:
        return None, f"upstream_placeholder:{field}"
    if isinstance(raw, bool):
        raise MarketColdnessError(f"Eastmoney {field} must not be boolean")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketColdnessError(f"Eastmoney {field} must be numeric or an explicit missing value") from exc
    if not math.isfinite(value):
        raise MarketColdnessError(f"Eastmoney {field} must be finite")
    if nonnegative and value < 0:
        raise MarketColdnessError(f"Eastmoney {field} must be non-negative")
    return value, None


def _optional_listing_date(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    field = "f26"
    if field not in row:
        return None, f"upstream_field_absent:{field}"
    raw = row[field]
    if raw is None:
        return None, f"upstream_null:{field}"
    if isinstance(raw, str) and raw.strip() in _PLACEHOLDERS:
        return None, f"upstream_placeholder:{field}"
    if isinstance(raw, bool):
        raise MarketColdnessError("Eastmoney f26 must not be boolean")
    if isinstance(raw, int):
        text = str(raw)
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        raise MarketColdnessError("Eastmoney f26 must use YYYYMMDD")
    if not re.fullmatch(r"[0-9]{8}", text):
        raise MarketColdnessError("Eastmoney f26 must use YYYYMMDD")
    try:
        parsed = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as exc:
        raise MarketColdnessError("Eastmoney f26 is not a valid calendar date") from exc
    return parsed.isoformat(), None


def _parse_name(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    if "f14" not in row:
        return None, "upstream_field_absent:f14"
    raw = row["f14"]
    if raw is None or (isinstance(raw, str) and raw.strip() in _PLACEHOLDERS):
        return None, "upstream_missing:f14"
    if not isinstance(raw, str) or not raw.strip():
        raise MarketColdnessError("Eastmoney f14 must be a non-empty string or missing")
    return raw.strip(), None


def _parse_row(row: Any, retrieved_at: str) -> MarketColdnessRecord:
    if not isinstance(row, Mapping):
        raise MarketColdnessError("Eastmoney diff row must be an object")

    code = row.get("f12")
    if not isinstance(code, str) or not _SIX_DIGIT_CODE.fullmatch(code):
        raise MarketColdnessError("Eastmoney f12 must be a six-digit string")
    if code.startswith("6"):
        exchange = "SH"
        expected_market = 1
    elif code.startswith(("0", "3")):
        exchange = "SZ"
        expected_market = 0
    else:
        raise MarketColdnessError(f"non-Shanghai/Shenzhen A-share code in source universe: {code}")

    market = _strict_nonnegative_int(row.get("f13"), "f13")
    if market != expected_market:
        raise MarketColdnessError(
            f"Eastmoney market/code mismatch for {code}: f13={market}, expected={expected_market}"
        )

    name, name_reason = _parse_name(row)
    change_60d, change_60d_reason = _optional_number(row, "f24")
    change_ytd, change_ytd_reason = _optional_number(row, "f25")
    turnover, turnover_reason = _optional_number(row, "f8", nonnegative=True)
    volume_ratio, volume_ratio_reason = _optional_number(row, "f10", nonnegative=True)
    listing_date, listing_reason = _optional_listing_date(row)

    reasons = {
        key: value
        for key, value in (
            ("name", name_reason),
            ("change_60d_pct", change_60d_reason),
            ("change_ytd_pct", change_ytd_reason),
            ("turnover_rate_pct", turnover_reason),
            ("volume_ratio", volume_ratio_reason),
            ("listing_date", listing_reason),
        )
        if value is not None
    }
    # Preserve the source's actual field presence.  An absent key and an
    # explicitly null value are different evidence states and must survive a
    # cache round-trip.
    upstream = {field: row[field] for field in EASTMONEY_FIELDS if field in row}
    return MarketColdnessRecord(
        code=code,
        exchange=exchange,
        eastmoney_market_id=market,
        name=name,
        change_60d_pct=change_60d,
        change_ytd_pct=change_ytd,
        turnover_rate_pct=turnover,
        volume_ratio=volume_ratio,
        listing_date=listing_date,
        source=EASTMONEY_SOURCE,
        source_url=EASTMONEY_CLIST_ENDPOINT,
        retrieved_at=retrieved_at,
        upstream_fields=upstream,
        missing_reasons=reasons,
    )


def _utc_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketColdnessError("market-coldness clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_retrieved_at(value: Any) -> str:
    if not isinstance(value, str):
        raise MarketColdnessError("market-coldness retrieval timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketColdnessError("market-coldness retrieval timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketColdnessError("market-coldness retrieval timestamp lacks a timezone")
    return value


class EastmoneyMarketColdnessAdapter:
    """Strict paginated adapter for the complete Shanghai/Shenzhen universe."""

    def __init__(
        self,
        *,
        http_client: Any = requests,
        endpoint: str = EASTMONEY_CLIST_ENDPOINT,
        timeout: float = REQUEST_TIMEOUT,
        retries: int = 3,
        retry_delay: float = 0.5,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_workers: int = DEFAULT_PAGE_WORKERS,
        clock: Callable[[], datetime] | None = None,
    ):
        if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("timeout must be finite and positive")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
            raise ValueError("retries must be a positive integer")
        if isinstance(retry_delay, bool) or not math.isfinite(float(retry_delay)) or float(retry_delay) < 0:
            raise ValueError("retry_delay must be finite and non-negative")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1 or page_size > 5_000:
            raise ValueError("page_size must be an integer between 1 and 5000")
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= MAX_PAGE_WORKERS
        ):
            raise ValueError(f"max_workers must be an integer between 1 and {MAX_PAGE_WORKERS}")
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            raise ValueError("endpoint must be an HTTP(S) URL")
        self.http_client = http_client
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.retries = retries
        self.retry_delay = float(retry_delay)
        self.page_size = page_size
        self.max_workers = max_workers
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _request_page(
        self,
        page: int,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        acquisition_budget: _AcquisitionByteBudget | None = None,
    ) -> tuple[Mapping[str, Any], int]:
        # Eastmoney's public quote pages use jQuery-style JSONP callbacks; the
        # source intermittently closes otherwise equivalent requests that do
        # not follow this wire contract.
        callback = f"jQuery{time.time_ns()}{page}"
        params = {
            "pn": page,
            "pz": self.page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": EASTMONEY_UNIVERSE,
            "fields": ",".join(EASTMONEY_FIELDS),
            "cb": callback,
        }
        request_timeout = self.timeout if timeout is None else timeout
        request_retries = self.retries if retries is None else retries
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(float(request_timeout))
            or float(request_timeout) <= 0
            or isinstance(request_retries, bool)
            or not isinstance(request_retries, int)
            or request_retries < 1
        ):
            raise ValueError("page request timeout and retries must be positive")
        request_timeout = float(request_timeout)
        last_error: BaseException | None = None
        transient_only = True
        for attempt in range(request_retries):
            response = None
            try:
                if acquisition_budget is not None:
                    acquisition_budget.raise_if_exhausted()
                response = self.http_client.get(
                    self.endpoint,
                    params=params,
                    headers=_HEADERS,
                    timeout=request_timeout,
                    stream=True,
                )
                response.raise_for_status()
                raw = _read_bounded_response(response, acquisition_budget=acquisition_budget)
                payload = _decode_json_or_jsonp(raw, callback)
                if not isinstance(payload, Mapping):
                    raise MarketColdnessError("Eastmoney response root must be an object")
                rc = payload.get("rc")
                if isinstance(rc, bool) or rc != 0:
                    raise MarketColdnessError(f"Eastmoney rejected page {page}: rc={rc!r}")
                data = payload.get("data")
                if not isinstance(data, Mapping):
                    raise MarketColdnessError(f"Eastmoney page {page} is missing data")
                return data, len(raw)
            except _MarketColdnessResourceLimitError:
                raise
            except Exception as exc:
                last_error = exc
                if not _is_transient_transport_error(exc):
                    transient_only = False
                if attempt + 1 < request_retries and self.retry_delay > 0:
                    time.sleep(self.retry_delay * (attempt + 1))
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        if last_error is None:  # pragma: no cover - retries is validated as positive.
            raise MarketColdnessError(f"Eastmoney page {page} request failed")
        error_type = (
            _MarketColdnessTransientTransportError
            if transient_only and _is_transient_transport_error(last_error)
            else MarketColdnessError
        )
        raise error_type(
            f"Eastmoney page {page} failed after {request_retries} attempt(s): {_error_label(last_error)}"
        ) from last_error

    def fetch_all(self) -> MarketColdnessBatch:
        retrieved_at = _utc_timestamp(self.clock)
        recovery_timeout = max(self.timeout * 2, _RECOVERY_TIMEOUT_FLOOR_SECONDS)
        acquisition_budget = _AcquisitionByteBudget(_MAX_ACQUISITION_RESPONSE_BYTES)
        try:
            first, first_bytes = self._request_page(1, acquisition_budget=acquisition_budget)
        except _MarketColdnessTransientTransportError:
            try:
                first, first_bytes = self._request_page(
                    1,
                    timeout=recovery_timeout,
                    retries=_RECOVERY_RETRIES,
                    acquisition_budget=acquisition_budget,
                )
            except _MarketColdnessTransientTransportError as exc:
                raise MarketColdnessError(f"failed to recover Eastmoney page 1: {exc}") from exc
        total = _strict_nonnegative_int(first.get("total"), "total")
        if total == 0:
            raise MarketColdnessError("Eastmoney Shanghai/Shenzhen universe unexpectedly contains zero rows")
        if total > _MAX_TOTAL_ROWS:
            raise _MarketColdnessResourceLimitError(
                f"Eastmoney total row count exceeds limit: {total} > {_MAX_TOTAL_ROWS}"
            )
        page_count = (total + self.page_size - 1) // self.page_size
        if page_count > _MAX_PAGES:
            raise _MarketColdnessResourceLimitError(f"Eastmoney page count exceeds limit: {page_count} > {_MAX_PAGES}")

        pages: dict[int, tuple[Mapping[str, Any], int]] = {1: (first, first_bytes)}
        if page_count > 1:
            # The upstream hard-caps responses at 100 rows even when a larger
            # page size is requested.  Fetch the remaining bounded pages in
            # parallel, then validate and consume them in page order.  This
            # preserves the all-or-error identity contract while avoiding a
            # roughly one-request-latency penalty for every listed company
            # page during the daily post-close refresh.
            worker_count = min(self.max_workers, page_count - 1)
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(self._request_page, page, acquisition_budget=acquisition_budget): page
                    for page in range(2, page_count + 1)
                }
                transient_failures: dict[int, _MarketColdnessTransientTransportError] = {}
                for future in as_completed(futures):
                    page = futures[future]
                    try:
                        pages[page] = future.result()
                    except _MarketColdnessResourceLimitError:
                        for pending in futures:
                            pending.cancel()
                        raise
                    except _MarketColdnessTransientTransportError as exc:
                        transient_failures[page] = exc

            if len(transient_failures) > _MAX_RECOVERY_PAGES:
                failed_pages = sorted(transient_failures)
                raise MarketColdnessError(
                    f"Eastmoney parallel fetch failed on {len(failed_pages)} pages, "
                    f"above recovery limit {_MAX_RECOVERY_PAGES}: {failed_pages[:_MAX_RECOVERY_PAGES]}"
                ) from transient_failures[failed_pages[0]]

            for page in sorted(transient_failures):
                try:
                    pages[page] = self._request_page(
                        page,
                        timeout=recovery_timeout,
                        retries=_RECOVERY_RETRIES,
                        acquisition_budget=acquisition_budget,
                    )
                except _MarketColdnessResourceLimitError:
                    raise
                except _MarketColdnessTransientTransportError as exc:
                    raise MarketColdnessError(f"failed to recover Eastmoney page {page}: {exc}") from exc

        records: list[MarketColdnessRecord] = []
        seen: set[str] = set()
        response_bytes = 0
        for page in range(1, page_count + 1):
            data, byte_count = pages[page]
            response_bytes += byte_count
            if response_bytes > _MAX_ACQUISITION_RESPONSE_BYTES:
                raise _MarketColdnessResourceLimitError("Eastmoney acquisition exceeds aggregate byte limit")

            page_total = _strict_nonnegative_int(data.get("total"), "total")
            if page_total != total:
                raise MarketColdnessError(
                    f"Eastmoney total changed during pagination: first={total}, page_{page}={page_total}"
                )
            rows = data.get("diff")
            if not isinstance(rows, list):
                raise MarketColdnessError(f"Eastmoney page {page} diff must be a list")
            expected_rows = self.page_size if page < page_count else total - self.page_size * (page_count - 1)
            if len(rows) != expected_rows:
                raise MarketColdnessError(
                    f"Eastmoney page {page} row-count mismatch: expected={expected_rows}, received={len(rows)}"
                )
            for row in rows:
                record = _parse_row(row, retrieved_at)
                if record.code in seen:
                    raise MarketColdnessError(f"duplicate Eastmoney code across pages: {record.code}")
                seen.add(record.code)
                records.append(record)

        if len(records) != total:
            raise MarketColdnessError(
                f"Eastmoney fetched row count mismatch: expected={total}, received={len(records)}"
            )
        return MarketColdnessBatch(
            records=tuple(records),
            retrieved_at=retrieved_at,
            total_expected=total,
            page_count=page_count,
            response_bytes=response_bytes,
        )


def _coverage(records: tuple[MarketColdnessRecord, ...]) -> MarketColdnessCoverage:
    total = len(records)
    by_metric: dict[str, MetricCoverage] = {}
    for metric in METRIC_SOURCE_FIELDS:
        present = sum(getattr(record, metric) is not None for record in records)
        missing = total - present
        by_metric[metric] = MetricCoverage(
            present=present,
            missing=missing,
            coverage_rate=present / total if total else None,
        )
    complete = sum(all(getattr(record, metric) is not None for metric in METRIC_SOURCE_FIELDS) for record in records)
    return MarketColdnessCoverage(
        total_records=total,
        complete_records=complete,
        complete_record_rate=complete / total if total else None,
        by_metric=by_metric,
    )


def _available_snapshot(batch: MarketColdnessBatch) -> MarketColdnessSnapshot:
    _validate_retrieved_at(batch.retrieved_at)
    if (
        isinstance(batch.total_expected, bool)
        or not isinstance(batch.total_expected, int)
        or batch.total_expected < 1
        or batch.total_expected != len(batch.records)
    ):
        raise MarketColdnessError("adapter batch total does not match its records")
    if isinstance(batch.page_count, bool) or not isinstance(batch.page_count, int) or batch.page_count < 1:
        raise MarketColdnessError("adapter batch page count is invalid")
    if isinstance(batch.response_bytes, bool) or not isinstance(batch.response_bytes, int) or batch.response_bytes < 0:
        raise MarketColdnessError("adapter batch response byte count is invalid")
    codes = [record.code for record in batch.records]
    if len(set(codes)) != len(codes):
        raise MarketColdnessError("adapter batch contains duplicate codes")
    for record in batch.records:
        if record.source != EASTMONEY_SOURCE or record.source_url != EASTMONEY_CLIST_ENDPOINT:
            raise MarketColdnessError("adapter record source provenance is invalid")
        if record.retrieved_at != batch.retrieved_at:
            raise MarketColdnessError("adapter record retrieval timestamps are inconsistent")
        # Re-parse cached/upstream fields so a custom adapter cannot bypass the
        # exchange identity or numeric evidence contract.
        if _parse_row(record.upstream_fields, batch.retrieved_at) != record:
            raise MarketColdnessError(f"adapter record cannot be reproduced from upstream fields: {record.code}")
    return MarketColdnessSnapshot(
        available=True,
        records=batch.records,
        source=EASTMONEY_SOURCE,
        source_url=EASTMONEY_CLIST_ENDPOINT,
        retrieved_at=batch.retrieved_at,
        total_expected=batch.total_expected,
        fetched_count=len(batch.records),
        page_count=batch.page_count,
        response_bytes=batch.response_bytes,
        universe_coverage_rate=1.0,
        coverage=_coverage(batch.records),
        cache_hit=False,
        cache_diagnostic="",
        reason="",
        failure=None,
    )


def _empty_coverage() -> MarketColdnessCoverage:
    return MarketColdnessCoverage(
        total_records=0,
        complete_records=0,
        complete_record_rate=None,
        by_metric={metric: MetricCoverage(0, 0, None) for metric in METRIC_SOURCE_FIELDS},
    )


def _unavailable_snapshot(exc: BaseException, cache_diagnostic: str) -> MarketColdnessSnapshot:
    label = _error_label(exc)
    return MarketColdnessSnapshot(
        available=False,
        records=(),
        source=EASTMONEY_SOURCE,
        source_url=EASTMONEY_CLIST_ENDPOINT,
        retrieved_at=None,
        total_expected=None,
        fetched_count=0,
        page_count=0,
        response_bytes=0,
        universe_coverage_rate=None,
        coverage=_empty_coverage(),
        cache_hit=False,
        cache_diagnostic=cache_diagnostic,
        reason=f"source_unavailable:{label}",
        failure={"stage": "source_fetch", "kind": type(exc).__name__, "detail": " ".join(str(exc).split())[:240]},
    )


def _cache_contract() -> dict[str, Any]:
    return {
        "source_id": EASTMONEY_SOURCE_ID,
        "source_url": EASTMONEY_CLIST_ENDPOINT,
        "universe": EASTMONEY_UNIVERSE,
        "fields": list(EASTMONEY_FIELDS),
        "metric_source_fields": dict(METRIC_SOURCE_FIELDS),
        "cache_schema_version": _CACHE_SCHEMA_VERSION,
    }


def _record_cache_value(record: MarketColdnessRecord) -> dict[str, Any]:
    value = record.to_dict()
    value.pop("source")
    value.pop("source_url")
    value.pop("retrieved_at")
    return value


def _snapshot_cache_value(snapshot: MarketColdnessSnapshot) -> dict[str, Any]:
    if not snapshot.available or snapshot.retrieved_at is None or snapshot.total_expected is None:
        raise ValueError("only available market-coldness snapshots can be cached")
    return {
        "contract": _cache_contract(),
        "retrieved_at": snapshot.retrieved_at,
        "total_expected": snapshot.total_expected,
        "page_count": snapshot.page_count,
        "response_bytes": snapshot.response_bytes,
        "records": [_record_cache_value(record) for record in snapshot.records],
    }


def _snapshot_from_cache(value: Any) -> MarketColdnessSnapshot:
    expected_keys = {"contract", "retrieved_at", "total_expected", "page_count", "response_bytes", "records"}
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise MarketColdnessError("market-coldness cache payload has an invalid shape")
    if value.get("contract") != _cache_contract():
        raise MarketColdnessError("market-coldness cache contract mismatch")
    retrieved_at = _validate_retrieved_at(value.get("retrieved_at"))

    raw_records = value.get("records")
    if not isinstance(raw_records, list):
        raise MarketColdnessError("market-coldness cache records must be a list")
    records: list[MarketColdnessRecord] = []
    cache_record_keys = {
        "code",
        "exchange",
        "eastmoney_market_id",
        "name",
        "change_60d_pct",
        "change_ytd_pct",
        "turnover_rate_pct",
        "volume_ratio",
        "listing_date",
        "upstream_fields",
        "missing_reasons",
    }
    for item in raw_records:
        if not isinstance(item, Mapping) or set(item) != cache_record_keys:
            raise MarketColdnessError("market-coldness cached record has an invalid shape")
        upstream = item.get("upstream_fields")
        if not isinstance(upstream, Mapping):
            raise MarketColdnessError("market-coldness cached upstream fields are invalid")
        reconstructed = _parse_row(upstream, retrieved_at)
        expected = reconstructed.to_dict()
        expected.pop("source")
        expected.pop("source_url")
        expected.pop("retrieved_at")
        if dict(item) != expected:
            raise MarketColdnessError(f"market-coldness cached normalized record mismatch: {reconstructed.code}")
        records.append(reconstructed)

    batch = MarketColdnessBatch(
        records=tuple(records),
        retrieved_at=retrieved_at,
        total_expected=_strict_nonnegative_int(value.get("total_expected"), "cached total"),
        page_count=_strict_nonnegative_int(value.get("page_count"), "cached page count"),
        response_bytes=_strict_nonnegative_int(value.get("response_bytes"), "cached response bytes"),
    )
    return replace(_available_snapshot(batch), cache_hit=True, cache_diagnostic="hit")


def fetch_market_coldness_snapshot(
    *,
    adapter: MarketColdnessAdapter | None = None,
    cache_path: str | Path = DEFAULT_MARKET_COLDNESS_CACHE_PATH,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
    force_refresh: bool = False,
    allow_expired_cache: bool = False,
) -> MarketColdnessSnapshot:
    """Return one complete, cached Shanghai/Shenzhen market snapshot.

    A verified cache hit returns before constructing or calling the network
    adapter.  A source failure returns ``available=False`` and keeps every
    market metric unavailable; callers never need to interpret a zero sentinel.
    """

    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be a non-negative integer")
    if not isinstance(force_refresh, bool):
        raise ValueError("force_refresh must be boolean")
    if not isinstance(allow_expired_cache, bool):
        raise ValueError("allow_expired_cache must be boolean")

    cache: SafeFileCache | None = None
    initial_load = None
    cache_diagnostic = "disabled"
    if use_cache:
        cache = SafeFileCache(
            cache_path,
            schema_version=_CACHE_SCHEMA_VERSION,
            ttl=cache_ttl_seconds,
            max_uncompressed_bytes=_MAX_CACHE_UNCOMPRESSED_BYTES,
        )
        # A forced refresh still reads the existing generation metadata so
        # the subsequent compare-and-swap cannot overwrite a concurrent
        # winner.  It simply does not return the cached value early.
        initial_load = cache.load(allow_expired=force_refresh or allow_expired_cache)
        if initial_load.hit and not force_refresh:
            try:
                return _snapshot_from_cache(initial_load.value)
            except MarketColdnessError as exc:
                cache_diagnostic = f"invalid_hit:{_error_label(exc)}"
        elif initial_load.hit:
            cache_diagnostic = "forced_refresh"
        else:
            cache_diagnostic = f"miss:{initial_load.reason}"

    source_adapter = adapter if adapter is not None else EastmoneyMarketColdnessAdapter()
    try:
        snapshot = _available_snapshot(source_adapter.fetch_all())
    except Exception as exc:
        return _unavailable_snapshot(exc, cache_diagnostic)
    snapshot = replace(snapshot, cache_diagnostic=cache_diagnostic)
    if cache is None:
        return snapshot

    expected_hash = None
    if initial_load is not None and isinstance(initial_load.metadata, Mapping):
        candidate = initial_load.metadata.get("payload_sha256")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            expected_hash = candidate
    try:
        cache.compare_and_swap(
            _snapshot_cache_value(snapshot),
            expected_payload_sha256=expected_hash,
            allow_replace_invalid=True,
        )
        return replace(snapshot, cache_diagnostic=f"{cache_diagnostic};saved")
    except SafeCacheConflict:
        winner = cache.load()
        if winner.hit:
            try:
                return replace(_snapshot_from_cache(winner.value), cache_diagnostic="race_winner")
            except MarketColdnessError:
                pass
        return replace(snapshot, cache_diagnostic=f"{cache_diagnostic};write_conflict")
    except SafeCacheError as exc:
        return replace(snapshot, cache_diagnostic=f"{cache_diagnostic};write_failed:{_error_label(exc)}")


def market_coldness_session_cache_path(
    as_of_session: date | str,
    *,
    directory: str | Path = DEFAULT_MARKET_COLDNESS_SESSION_CACHE_DIRECTORY,
) -> Path:
    """Return the immutable cache path for one canonical Shanghai session."""

    if isinstance(as_of_session, datetime):
        raise ValueError("market-coldness session must be a date")
    if isinstance(as_of_session, date):
        session = as_of_session
    elif isinstance(as_of_session, str):
        try:
            session = date.fromisoformat(as_of_session)
        except ValueError as exc:
            raise ValueError("market-coldness session must be an ISO date") from exc
        if session.isoformat() != as_of_session:
            raise ValueError("market-coldness session must be an ISO date")
    else:
        raise ValueError("market-coldness session must be a date")
    return Path(directory) / f"eastmoney_sh_sz_a_{session.isoformat()}.json.gz"


def load_market_coldness_session_snapshot(
    as_of_session: date | str,
    *,
    directory: str | Path = DEFAULT_MARKET_COLDNESS_SESSION_CACHE_DIRECTORY,
) -> MarketColdnessSnapshot | None:
    """Load one immutable session generation without contacting the network."""

    path = market_coldness_session_cache_path(as_of_session, directory=directory)
    cache = SafeFileCache(
        path,
        schema_version=_CACHE_SCHEMA_VERSION,
        ttl=CACHE_TTL_SECONDS,
        max_uncompressed_bytes=_MAX_CACHE_UNCOMPRESSED_BYTES,
    )
    loaded = cache.load(allow_expired=True)
    if not loaded.hit:
        if path.exists():
            raise MarketColdnessError(f"invalid immutable market-coldness session cache: {loaded.reason}")
        return None
    snapshot = _snapshot_from_cache(loaded.value)
    if snapshot.retrieved_at is None:
        raise MarketColdnessError("immutable market-coldness session cache has no retrieval timestamp")
    session = as_of_session if isinstance(as_of_session, date) else date.fromisoformat(as_of_session)
    try:
        retrieved = datetime.fromisoformat(snapshot.retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketColdnessError("immutable market-coldness session cache has an invalid timestamp") from exc
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise MarketColdnessError("immutable market-coldness session cache timestamp lacks a timezone")
    retrieved_shanghai = retrieved.astimezone(ZoneInfo("Asia/Shanghai"))
    if retrieved_shanghai.date() != session:
        raise MarketColdnessError("immutable market-coldness session cache is bound to another session")
    if retrieved_shanghai.time().replace(tzinfo=None) < _SESSION_ARCHIVE_READY_TIME:
        raise MarketColdnessError("immutable market-coldness session cache was acquired before the session close")
    return replace(snapshot, cache_hit=True, cache_diagnostic="immutable_session_hit")


def archive_market_coldness_session_snapshot(
    snapshot: MarketColdnessSnapshot,
    as_of_session: date | str,
    *,
    directory: str | Path = DEFAULT_MARKET_COLDNESS_SESSION_CACHE_DIRECTORY,
) -> MarketColdnessSnapshot:
    """Persist one complete generation once; a differing rewrite is rejected."""

    if not snapshot.available or snapshot.retrieved_at is None:
        raise MarketColdnessError("cannot archive an unavailable market-coldness snapshot")
    path = market_coldness_session_cache_path(as_of_session, directory=directory)
    session = as_of_session if isinstance(as_of_session, date) else date.fromisoformat(as_of_session)
    try:
        retrieved = datetime.fromisoformat(snapshot.retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketColdnessError("market-coldness retrieval timestamp is invalid") from exc
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise MarketColdnessError("market-coldness retrieval timestamp lacks a timezone")
    retrieved_shanghai = retrieved.astimezone(ZoneInfo("Asia/Shanghai"))
    if retrieved_shanghai.date() != session:
        raise MarketColdnessError("market-coldness snapshot is bound to another session")
    if retrieved_shanghai.time().replace(tzinfo=None) < _SESSION_ARCHIVE_READY_TIME:
        raise MarketColdnessError("market-coldness snapshot was acquired before the session close")

    cache = SafeFileCache(
        path,
        schema_version=_CACHE_SCHEMA_VERSION,
        ttl=CACHE_TTL_SECONDS,
        max_uncompressed_bytes=_MAX_CACHE_UNCOMPRESSED_BYTES,
    )
    payload = _snapshot_cache_value(snapshot)
    loaded = cache.load(allow_expired=True)
    if loaded.hit:
        existing = _snapshot_from_cache(loaded.value)
        if _snapshot_cache_value(existing) != payload:
            raise MarketColdnessError("immutable market-coldness session cache already has a different generation")
        return replace(existing, cache_hit=True, cache_diagnostic="immutable_session_existing")
    if path.exists():
        raise MarketColdnessError(f"invalid immutable market-coldness session cache: {loaded.reason}")
    try:
        cache.compare_and_swap(payload, expected_payload_sha256=None, allow_replace_invalid=False)
    except SafeCacheConflict as exc:
        winner = load_market_coldness_session_snapshot(session, directory=directory)
        if winner is None or _snapshot_cache_value(winner) != payload:
            raise MarketColdnessError("immutable market-coldness session cache write conflict") from exc
        return winner
    return replace(snapshot, cache_hit=False, cache_diagnostic="immutable_session_saved")


__all__ = [
    "DEFAULT_MARKET_COLDNESS_CACHE_PATH",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_MARKET_COLDNESS_SESSION_CACHE_DIRECTORY",
    "EASTMONEY_CLIST_ENDPOINT",
    "EASTMONEY_FIELDS",
    "EASTMONEY_SOURCE",
    "EASTMONEY_UNIVERSE",
    "EastmoneyMarketColdnessAdapter",
    "METRIC_SOURCE_FIELDS",
    "MarketColdnessBatch",
    "MarketColdnessCoverage",
    "MarketColdnessError",
    "MarketColdnessRecord",
    "MarketColdnessSnapshot",
    "MetricCoverage",
    "archive_market_coldness_session_snapshot",
    "fetch_market_coldness_snapshot",
    "load_market_coldness_session_snapshot",
    "market_coldness_session_cache_path",
]
