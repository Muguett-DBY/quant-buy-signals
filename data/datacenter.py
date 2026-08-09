"""Complete, validated Eastmoney Datacenter report downloads.

Every public fetch is all-or-error: a failed metadata request or missing page
raises :class:`DataFetchError` instead of returning a plausible partial frame.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Collection, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import CONCURRENCY, REQUEST_TIMEOUT
from data.capex_evidence import (
    CAPEX_FIELD,
    EASTMONEY_DATACENTER_URL,
    NON_CAPEX_OUTFLOW_FIELDS,
)
from data.cache import SafeFileCache


DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
_ANNUAL_BATCH_DATASETS = 4
# Eastmoney resets TLS connections when four annual datasets each fan out to
# five page workers (20 concurrent requests).  Twelve requests preserves most
# of the speed-up while avoiding the observed connection-reset cliff; the
# all-or-error batch below recovers only the small failed page subset.
_DATACENTER_WORKERS = max(1, min(int(CONCURRENCY) // _ANNUAL_BATCH_DATASETS, 3))
_DATACENTER_BATCH_WORKERS = max(1, min(_ANNUAL_BATCH_DATASETS, int(CONCURRENCY) // _DATACENTER_WORKERS))
# Annual and interim generations are independent and may overlap, but their
# nested pools must not recreate the observed connection-reset and DNS
# saturation cliffs.  Six streams still overlap pagination while leaving the
# resolver and Eastmoney edge enough headroom for a complete generation.
_DATACENTER_ACTIVE_REQUEST_LIMIT = max(1, min(int(CONCURRENCY), 6))
_DATACENTER_REQUEST_SLOTS = threading.BoundedSemaphore(_DATACENTER_ACTIVE_REQUEST_LIMIT)
_MAX_DATACENTER_PAGES = 128
_MAX_DATACENTER_ROWS = 50_000
_MAX_DATACENTER_RESPONSE_BYTES = 16 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_MAX_REMOTE_SECURITY_CODES = 100
_DATACENTER_RECOVERY_TIMEOUT = max(int(REQUEST_TIMEOUT) * 2, 30)
# Recovering one failed page is much cheaper than restarting every annual and
# interim report.  Eastmoney occasionally leaves a single page stalled for two
# consecutive 30-second reads, so give the bounded, sequential recovery path
# four attempts while keeping the parallel fast path at three attempts.
_DATACENTER_RECOVERY_RETRIES = 4
_DATACENTER_RECOVERY_RETRY_DELAY = 2.0
_MAX_DATACENTER_RECOVERY_PAGES = 3
_RETRYABLE_DATACENTER_HTTP_STATUSES = frozenset({408, 425, 429})
ANNUAL_HISTORY_YEARS = 10
DETAILED_ANNUAL_HISTORY_YEARS = 5
_SHANGHAI = ZoneInfo("Asia/Shanghai")
DATACENTER_REPORT_CACHE_ADAPTER_VERSION = 1
DATACENTER_REPORT_CACHE_SCHEMA_VERSION = 1
DATACENTER_REPORT_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "datacenter_reports"
_DATACENTER_RECENT_REPORT_TTL_SECONDS = 12 * 60 * 60
_DATACENTER_HISTORICAL_REPORT_TTL_SECONDS = 7 * 24 * 60 * 60
_DATACENTER_CACHE_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_DATACENTER_CACHE_STATS_LOCK = threading.Lock()
_DATACENTER_CACHE_STATS: Counter[str] = Counter()
_DATACENTER_CACHE_MISS_REASONS: Counter[str] = Counter()

RPT_INCOME = "RPT_DMSK_FN_INCOME"
RPT_CASHFLOW = "RPT_DMSK_FN_CASHFLOW"
RPT_DETAILED_CASHFLOW = "RPT_F10_FINANCE_GCASHFLOW"
RPT_BALANCE = "RPT_F10_FINANCE_GBALANCE"
RPT_BALANCE_BANK = "RPT_F10_FINANCE_BBALANCE"
RPT_BALANCE_INSURANCE = "RPT_F10_FINANCE_IBALANCE"
RPT_BALANCE_SECURITIES = "RPT_F10_FINANCE_SBALANCE"
RPT_MAIN_FINANCIAL_INDICATORS = "RPT_F10_FINANCE_MAINFINADATA"

GENERAL_MAIN_FINANCIAL_INDICATOR_METRICS = (
    "RDEXPEND",
    "ROIC",
    "ROEJQ",
    "XSMLL",
    "XSJLL",
    "TAXRATE",
    "TOTAL_SHARE",
    "STAFF_NUM",
    "KCFJCXSYJLR",
    "INTEREST_DEBT_RATIO",
)
FINANCIAL_SECTOR_INDICATOR_FIELDS: dict[str, tuple[str, ...]] = {
    "BANK": (
        "NET_INTEREST_MARGIN",
        "NET_INTEREST_SPREAD",
        "NEWCAPITALADER",
        "FIRST_ADEQUACY_RATIO",
        "NONPERLOAN",
        "LOAN_PROVISION_RATIO",
        "TOTALDEPOSITS",
        "GROSSLOANS",
        "LOAN_ADVANCES",
    ),
    "INSURANCE": (
        "SOLVENCY_AR",
        "NBV_RATE",
        "NBV_LIFE",
        "EARNED_PREMIUM",
        "SURRENDER_RATE_LIFE",
    ),
    "SECURITIES": (
        "CAPITAL_LEVERAGE_RATIO",
        "CAPITAL_PROVISIONS_SUM",
        "LIQUIDITY_COVERAGE_RATIO",
        "NET_CAPITAL_LIABILITIES",
        "PROPRIETARY_CAPITAL",
        "RISK_COVERAGE",
        "NET_FUNDING_RATIO",
    ),
}
MAIN_FINANCIAL_INDICATOR_METRICS = (
    *GENERAL_MAIN_FINANCIAL_INDICATOR_METRICS,
    *dict.fromkeys(field for fields in FINANCIAL_SECTOR_INDICATOR_FIELDS.values() for field in fields),
)
_MAIN_FINANCIAL_INDICATOR_PROVENANCE = (
    "SECURITY_CODE",
    "SECUCODE",
    "SECURITY_NAME_ABBR",
    "SECURITY_TYPE_CODE",
    "REPORT_DATE",
    "REPORT_TYPE",
    "REPORT_DATE_NAME",
    "REPORT_YEAR",
    "NOTICE_DATE",
)
_MAIN_FINANCIAL_INDICATOR_COLUMNS = ",".join((*_MAIN_FINANCIAL_INDICATOR_PROVENANCE, *MAIN_FINANCIAL_INDICATOR_METRICS))
_A_SHARE_SECURITY_TYPE = "058001001"

_DETAILED_CASHFLOW_METADATA = (
    "SECURITY_CODE",
    "SECUCODE",
    "SECURITY_NAME_ABBR",
    "SECURITY_TYPE_CODE",
    "REPORT_DATE",
    "REPORT_TYPE",
    "REPORT_DATE_NAME",
    "NOTICE_DATE",
    "UPDATE_DATE",
    "CURRENCY",
)
_DETAILED_INVESTMENT_OUTFLOW_COMPONENTS = (*NON_CAPEX_OUTFLOW_FIELDS,)
_DETAILED_INVESTMENT_FIELDS = (
    "TOTAL_INVEST_INFLOW",
    CAPEX_FIELD,
    *_DETAILED_INVESTMENT_OUTFLOW_COMPONENTS,
    "TOTAL_INVEST_OUTFLOW",
    "INVEST_NETCASH_OTHER",
    "INVEST_NETCASH_BALANCE",
    "NETCASH_INVEST",
)
_DETAILED_CASHFLOW_COLUMNS = ",".join((*_DETAILED_CASHFLOW_METADATA, *_DETAILED_INVESTMENT_FIELDS))
_INTERIM_REPORT_LABELS = {
    "03-31": ("一季报", "一季报"),
    "06-30": ("中报", "中报"),
    "09-30": ("三季报", "三季报"),
}
_REQUESTED_COLUMN_ALIAS_GROUPS = (
    frozenset({"TOTAL_PARENT_EQUITY", "PARENT_EQUITY", "TOTAL_EQUITY_PARENT", "EQUITY_PARENT"}),
    frozenset({"MINORITY_EQUITY", "MINORITY_INTEREST"}),
    frozenset({"LONG_LOAN", "LONG_TERM_LOAN"}),
    frozenset({"BONDS_PAYABLE", "BOND_PAYABLE"}),
    frozenset(
        {
            "NONCURRENT_LIAB_1YEAR",
            "NONCURRENT_LIABILITY_IN_1YEAR",
            "CURRENT_PORTION_NONCURRENT_LIAB",
        }
    ),
    frozenset({"LEASE_LIAB", "LEASE_LIABILITY"}),
    frozenset({"SHORT_BONDS_PAYABLE", "SHORT_BOND_PAYABLE"}),
    frozenset({"BORROW_FUNDS", "BORROW_FUND"}),
    frozenset({"CENTRAL_BANK_BORROWING", "LOAN_PBC"}),
    frozenset({"SUBORDINATED_BONDS_PAYABLE", "SUBBOND_PAYABLE"}),
)


class DataFetchError(RuntimeError):
    """A remote dataset could not be proven complete and valid."""


class _DataResourceLimitError(DataFetchError):
    """A response exceeded a fixed local resource budget and must not be retried."""


class _DataTransientTransportError(DataFetchError):
    """A page exhausted retries using only recoverable transport failures."""


@dataclass(frozen=True)
class _PageResult:
    page: int
    pages: int
    data: list[dict[str, Any]]
    count: int | None = None


def _is_transient_datacenter_transport_error(exc: BaseException | None) -> bool:
    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return True
    if not isinstance(exc, requests.HTTPError):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status in _RETRYABLE_DATACENTER_HTTP_STATUSES or 500 <= status <= 599)


def _strict_nonnegative_int(value: Any, *, field: str, report_name: str, page: int) -> int:
    if isinstance(value, bool):
        raise DataFetchError(f"invalid {field} metadata for {report_name} page {page}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"0|[1-9]\d*", value.strip()):
        parsed = int(value.strip())
    else:
        raise DataFetchError(f"invalid {field} metadata for {report_name} page {page}")
    if parsed < 0:
        raise DataFetchError(f"negative {field} metadata for {report_name} page {page}")
    return parsed


def _bounded_response_json(response: requests.Response) -> Any:
    """Decode one response without ever accepting more than the byte budget.

    ``stream=True`` keeps Requests from eagerly materialising an unbounded body.
    Both the declared wire size and the decoded body size are checked because a
    compressed or chunked response may not have a useful Content-Length header.
    The small fallback exists for response-like test doubles only; real Requests
    responses always expose ``iter_content``.
    """
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        try:
            declared_bytes = int(declared)
        except (TypeError, ValueError) as exc:
            raise DataFetchError("invalid Content-Length in Eastmoney response") from exc
        if declared_bytes < 0:
            raise DataFetchError("negative Content-Length in Eastmoney response")
        if declared_bytes > _MAX_DATACENTER_RESPONSE_BYTES:
            raise _DataResourceLimitError(
                f"Eastmoney response exceeds byte limit: {declared_bytes} > {_MAX_DATACENTER_RESPONSE_BYTES}"
            )

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        received = 0
        for chunk in iter_content(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise DataFetchError("Eastmoney response yielded non-byte content")
            received += len(chunk)
            if received > _MAX_DATACENTER_RESPONSE_BYTES:
                raise _DataResourceLimitError(
                    f"Eastmoney response exceeds byte limit: received more than {_MAX_DATACENTER_RESPONSE_BYTES}"
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise DataFetchError("invalid JSON in Eastmoney response") from exc

    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        if len(content) > _MAX_DATACENTER_RESPONSE_BYTES:
            raise _DataResourceLimitError(
                f"Eastmoney response exceeds byte limit: {len(content)} > {_MAX_DATACENTER_RESPONSE_BYTES}"
            )
        try:
            return json.loads(bytes(content))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise DataFetchError("invalid JSON in Eastmoney response") from exc

    try:
        payload = response.json()
    except RecursionError as exc:
        raise DataFetchError("invalid JSON in Eastmoney response") from exc
    try:
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DataFetchError("invalid JSON-compatible Eastmoney response") from exc
    if encoded_size > _MAX_DATACENTER_RESPONSE_BYTES:
        raise _DataResourceLimitError(
            f"Eastmoney response exceeds byte limit: {encoded_size} > {_MAX_DATACENTER_RESPONSE_BYTES}"
        )
    return payload


def _request_page(
    report_name: str,
    columns: str,
    page: int,
    page_size: int = 500,
    sort_col: str = "SECURITY_CODE",
    sort_order: int = 1,
    extra_filter: str = "",
    timeout: int = REQUEST_TIMEOUT,
    retries: int = 3,
    retry_delay: float = 0.5,
) -> _PageResult:
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page < 1
        or page_size < 1
    ):
        raise ValueError("page and page_size must be positive integers")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise ValueError("timeout must be positive")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
        raise ValueError("retries must be a positive integer")
    if (
        isinstance(retry_delay, bool)
        or not isinstance(retry_delay, (int, float))
        or not math.isfinite(float(retry_delay))
        or float(retry_delay) < 0
    ):
        raise ValueError("retry_delay must be finite and non-negative")
    params = {
        "reportName": report_name,
        "columns": columns,
        "pageNumber": page,
        "pageSize": page_size,
        "sortTypes": sort_order,
        "sortColumns": sort_col,
        "source": "WEB",
        "client": "PC",
    }
    if extra_filter:
        params["filter"] = extra_filter

    last_error: Exception | None = None
    transient_only = True
    attempts_used = 0
    for attempt in range(retries):
        attempts_used = attempt + 1
        try:
            with _DATACENTER_REQUEST_SLOTS:
                response = requests.get(
                    DC_URL,
                    params=params,
                    headers=DC_HEADERS,
                    timeout=timeout,
                    stream=True,
                )
                try:
                    response.raise_for_status()
                    payload = _bounded_response_json(response)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            if not isinstance(payload, dict) or payload.get("success") is not True:
                message = payload.get("message") if isinstance(payload, dict) else "non-object response"
                raise DataFetchError(f"Eastmoney rejected {report_name} page {page}: {message}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise DataFetchError(f"missing result metadata for {report_name} page {page}")
            raw_data = result.get("data")
            if raw_data is None:
                raw_data = []
            if not isinstance(raw_data, list) or any(not isinstance(row, dict) for row in raw_data):
                raise DataFetchError(f"invalid data schema for {report_name} page {page}")
            try:
                raw_pages = result["pages"]
                raw_count = result["count"]
            except KeyError as exc:
                raise DataFetchError(f"missing pagination metadata for {report_name} page {page}") from exc
            pages = _strict_nonnegative_int(raw_pages, field="page count", report_name=report_name, page=page)
            count = _strict_nonnegative_int(raw_count, field="row count", report_name=report_name, page=page)
            if pages < 0 or count < 0 or (raw_data and pages < page):
                raise DataFetchError(
                    f"inconsistent pagination for {report_name} page {page}: pages={pages}, count={count}"
                )
            if pages > _MAX_DATACENTER_PAGES:
                raise _DataResourceLimitError(
                    f"{report_name} page count exceeds limit: {pages} > {_MAX_DATACENTER_PAGES}"
                )
            if count > _MAX_DATACENTER_ROWS:
                raise _DataResourceLimitError(
                    f"{report_name} row count exceeds limit: {count} > {_MAX_DATACENTER_ROWS}"
                )
            expected_pages = 0 if count == 0 else (count + page_size - 1) // page_size
            if pages != expected_pages:
                raise DataFetchError(
                    f"page/count mismatch for {report_name}: pages={pages}, count={count}, page_size={page_size}"
                )
            if count == 0:
                expected_rows = 0
            elif page > pages:
                raise DataFetchError(f"requested page {page} exceeds declared page count {pages} for {report_name}")
            else:
                expected_rows = min(page_size, count - ((page - 1) * page_size))
            if len(raw_data) != expected_rows:
                raise DataFetchError(
                    f"page row-count mismatch for {report_name} page {page}: "
                    f"expected {expected_rows}, received {len(raw_data)}"
                )
            return _PageResult(page=page, pages=pages, data=raw_data, count=count)
        except _DataResourceLimitError:
            raise
        except (requests.RequestException, ValueError, DataFetchError) as exc:
            last_error = exc
            if not _is_transient_datacenter_transport_error(exc):
                transient_only = False
            if attempt + 1 < retries:
                time.sleep(float(retry_delay) * (attempt + 1))
    error_type = (
        _DataTransientTransportError
        if transient_only and _is_transient_datacenter_transport_error(last_error)
        else DataFetchError
    )
    raise error_type(
        f"failed to fetch {report_name} page {page} after {attempts_used} attempts: {last_error}"
    ) from last_error


def _fetch_page(
    report_name: str,
    columns: str,
    page: int,
    page_size: int = 500,
    sort_col: str = "SECURITY_CODE",
    sort_order: int = 1,
    extra_filter: str = "",
    timeout: int = REQUEST_TIMEOUT,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning the validated page rows."""
    return _request_page(
        report_name,
        columns,
        page,
        page_size,
        sort_col,
        sort_order,
        extra_filter,
        timeout,
    ).data


def _validate_filtered_report_date(frame: pd.DataFrame, extra_filter: str) -> None:
    match = re.search(r"REPORT_DATE\s*=\s*['\"](\d{4}-\d{2}-\d{2})['\"]", extra_filter)
    if not match or frame.empty:
        return
    if "REPORT_DATE" not in frame.columns:
        raise DataFetchError("filtered report response omitted REPORT_DATE")
    expected = match.group(1)
    actual = frame["REPORT_DATE"].astype(str).str.slice(0, 10)
    invalid = actual.ne(expected)
    if invalid.any():
        examples = sorted(actual[invalid].dropna().unique().tolist())[:3]
        raise DataFetchError(f"report date mismatch: expected {expected}, got {examples}")


def _validate_requested_columns(
    frame: pd.DataFrame,
    columns: str,
    *,
    report_name: str,
) -> None:
    """Prove that Eastmoney honored every requested field.

    An all-null requested column is valid evidence that the source returned a
    blank fact.  A column absent from the response is different: accepting it
    would silently turn an API/schema failure into missing financial data.
    Known Eastmoney naming variants are accepted as equivalent.
    """
    requested = [column.strip() for column in str(columns).split(",") if column.strip()]
    if not requested or requested == ["ALL"]:
        return
    present = set(frame.columns)
    missing: list[str] = []
    for requested_column in requested:
        accepted = {requested_column}
        for aliases in _REQUESTED_COLUMN_ALIAS_GROUPS:
            if requested_column in aliases:
                accepted.update(aliases)
        if accepted.isdisjoint(present):
            missing.append(requested_column)
    if missing:
        raise DataFetchError(f"{report_name} response omitted requested columns: {sorted(missing)}")


def reset_datacenter_fetch_diagnostics() -> None:
    """Reset process-local counters for one complete financial refresh."""

    with _DATACENTER_CACHE_STATS_LOCK:
        _DATACENTER_CACHE_STATS.clear()
        _DATACENTER_CACHE_MISS_REASONS.clear()


def get_datacenter_fetch_diagnostics() -> dict[str, Any]:
    with _DATACENTER_CACHE_STATS_LOCK:
        values = {
            key: int(_DATACENTER_CACHE_STATS.get(key, 0))
            for key in (
                "cache_hits",
                "cache_misses",
                "cache_invalid",
                "cache_write_errors",
                "network_queries",
                "rows_from_cache",
                "rows_from_network",
            )
        }
        values["miss_reasons"] = dict(sorted(_DATACENTER_CACHE_MISS_REASONS.items()))
        oldest = _DATACENTER_CACHE_STATS.get("oldest_cache_age_ms")
        values["oldest_cache_age_seconds"] = round(float(oldest) / 1000.0, 3) if oldest is not None else None
        return values


def _record_datacenter_stat(key: str, amount: int = 1) -> None:
    with _DATACENTER_CACHE_STATS_LOCK:
        _DATACENTER_CACHE_STATS[key] += int(amount)


def _datacenter_cache_contract(
    report_name: str,
    columns: str,
    extra_filter: str,
    page_size: int,
) -> dict[str, Any]:
    return {
        "adapter_version": DATACENTER_REPORT_CACHE_ADAPTER_VERSION,
        "endpoint": DC_URL,
        "report_name": str(report_name),
        "columns": str(columns),
        "filter": str(extra_filter),
        "page_size": int(page_size),
        "sort_column": "SECURITY_CODE",
        "sort_order": 1,
        "source": "WEB",
        "client": "PC",
    }


def _datacenter_report_cache_ttl(extra_filter: str, *, today: date | None = None) -> int:
    reference = today or _shanghai_today()
    match = re.search(r"REPORT_DATE\s*=\s*['\"](\d{4})-(\d{2})-(\d{2})['\"]", str(extra_filter))
    if match and int(match.group(1)) <= reference.year - 2:
        return _DATACENTER_HISTORICAL_REPORT_TTL_SECONDS
    return _DATACENTER_RECENT_REPORT_TTL_SECONDS


def _datacenter_report_cache(contract: Mapping[str, Any]) -> SafeFileCache:
    canonical = json.dumps(
        dict(contract),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = hashlib.sha256(canonical).hexdigest()
    return SafeFileCache(
        DATACENTER_REPORT_CACHE_DIR / f"{identity}.json.gz",
        schema_version=DATACENTER_REPORT_CACHE_SCHEMA_VERSION,
        ttl=_datacenter_report_cache_ttl(str(contract.get("filter") or "")),
        max_uncompressed_bytes=_DATACENTER_CACHE_MAX_UNCOMPRESSED_BYTES,
    )


def _validated_complete_report_frame(
    rows: Any,
    *,
    report_name: str,
    columns: str,
    extra_filter: str,
    expected_count: int,
) -> pd.DataFrame:
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        or isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
        or expected_count > _MAX_DATACENTER_ROWS
        or len(rows) != expected_count
    ):
        raise DataFetchError(f"{report_name} cached/assembled rows do not match the complete query count")
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "SECURITY_CODE" not in frame.columns:
        raise DataFetchError(f"{report_name} response omitted SECURITY_CODE")
    canonical_codes: list[str] = []
    for value in frame["SECURITY_CODE"].tolist():
        if isinstance(value, bool):
            raise DataFetchError(f"{report_name} response contains an invalid SECURITY_CODE")
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise DataFetchError(f"{report_name} response contains an invalid SECURITY_CODE")
            text = str(int(value))
        else:
            text = str(value).strip()
        if re.fullmatch(r"\d{1,6}", text) is None:
            raise DataFetchError(f"{report_name} response contains an invalid SECURITY_CODE")
        canonical_codes.append(text.zfill(6))
    frame["SECURITY_CODE"] = canonical_codes
    if "REPORT_DATE" in frame.columns:
        canonical_dates: list[str] = []
        for value in frame["REPORT_DATE"].tolist():
            text = str(value).strip()[:10]
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d")
            except ValueError as exc:
                raise DataFetchError(f"{report_name} response contains an invalid REPORT_DATE") from exc
            if parsed.strftime("%Y-%m-%d") != text:
                raise DataFetchError(f"{report_name} response contains an invalid REPORT_DATE")
            canonical_dates.append(text)
        frame["REPORT_DATE"] = canonical_dates
    _validate_requested_columns(frame, columns, report_name=report_name)
    _validate_filtered_report_date(frame, extra_filter)
    identity_columns = [column for column in ("SECURITY_CODE", "REPORT_DATE") if column in frame]
    if len(identity_columns) == 2 and frame.duplicated(identity_columns, keep=False).any():
        examples = (
            frame.loc[frame.duplicated(identity_columns, keep=False), identity_columns]
            .drop_duplicates()
            .head(5)
            .to_dict(orient="records")
        )
        raise DataFetchError(f"duplicate report identities across pages: {examples}")
    sort_columns = [column for column in ("SECURITY_CODE", "REPORT_DATE") if column in frame.columns]
    if sort_columns:
        frame = frame.sort_values(sort_columns, kind="stable").reset_index(drop=True)
    return frame


def _load_datacenter_report_cache(
    contract: Mapping[str, Any],
    *,
    report_name: str,
    columns: str,
    extra_filter: str,
) -> pd.DataFrame | None:
    loaded = _datacenter_report_cache(contract).load()
    if not loaded.hit:
        _record_datacenter_stat("cache_misses")
        with _DATACENTER_CACHE_STATS_LOCK:
            _DATACENTER_CACHE_MISS_REASONS[str(loaded.reason or "unknown")] += 1
        return None
    try:
        value = loaded.value
        if (
            not isinstance(value, Mapping)
            or value.get("adapter_version") != DATACENTER_REPORT_CACHE_ADAPTER_VERSION
            or value.get("contract") != dict(contract)
            or isinstance(value.get("retrieved_at"), bool)
            or not isinstance(value.get("retrieved_at"), (int, float))
            or not math.isfinite(float(value["retrieved_at"]))
            or float(value["retrieved_at"]) <= 0
        ):
            raise DataFetchError("datacenter report cache contract is invalid")
        frame = _validated_complete_report_frame(
            value.get("rows"),
            report_name=report_name,
            columns=columns,
            extra_filter=extra_filter,
            expected_count=value.get("row_count"),
        )
    except (DataFetchError, KeyError, TypeError, ValueError, OverflowError):
        _record_datacenter_stat("cache_invalid")
        return None
    age_ms = max(0, int((time.time() - float(value["retrieved_at"])) * 1000.0))
    with _DATACENTER_CACHE_STATS_LOCK:
        _DATACENTER_CACHE_STATS["cache_hits"] += 1
        _DATACENTER_CACHE_STATS["rows_from_cache"] += len(frame)
        _DATACENTER_CACHE_STATS["oldest_cache_age_ms"] = max(
            _DATACENTER_CACHE_STATS.get("oldest_cache_age_ms", 0), age_ms
        )
    return frame


def _save_datacenter_report_cache(
    contract: Mapping[str, Any],
    frame: pd.DataFrame,
) -> None:
    try:
        rows = frame.to_dict(orient="records")
        _datacenter_report_cache(contract).save(
            {
                "adapter_version": DATACENTER_REPORT_CACHE_ADAPTER_VERSION,
                "contract": dict(contract),
                "retrieved_at": time.time(),
                "row_count": len(rows),
                "rows": rows,
            },
            ttl=_datacenter_report_cache_ttl(str(contract.get("filter") or "")),
        )
    except Exception:
        # A cache is an optimization only; a fully validated network frame must
        # remain usable if the runner's cache volume is unavailable.
        _record_datacenter_stat("cache_write_errors")


def _fetch_all_pages(
    report_name: str,
    columns: str,
    extra_filter: str = "",
    page_size: int = 500,
    max_workers: int = _DATACENTER_WORKERS,
) -> pd.DataFrame:
    """Fetch one complete report query using its first response as metadata."""
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or page_size < 1
        or max_workers < 1
    ):
        raise ValueError("page_size and max_workers must be positive integers")
    contract = _datacenter_cache_contract(report_name, columns, extra_filter, page_size)
    cached = _load_datacenter_report_cache(
        contract,
        report_name=report_name,
        columns=columns,
        extra_filter=extra_filter,
    )
    if cached is not None:
        return cached
    try:
        first = _request_page(
            report_name,
            columns,
            1,
            page_size,
            extra_filter=extra_filter,
        )
    except _DataTransientTransportError:
        try:
            first = _request_page(
                report_name,
                columns,
                1,
                page_size,
                extra_filter=extra_filter,
                timeout=_DATACENTER_RECOVERY_TIMEOUT,
                retries=_DATACENTER_RECOVERY_RETRIES,
                retry_delay=_DATACENTER_RECOVERY_RETRY_DELAY,
            )
        except _DataTransientTransportError as exc:
            raise DataFetchError(f"failed to recover {report_name} metadata page 1: {exc}") from exc
    if first.pages > _MAX_DATACENTER_PAGES:
        raise _DataResourceLimitError(
            f"{report_name} page count exceeds limit: {first.pages} > {_MAX_DATACENTER_PAGES}"
        )
    if first.count is None:
        raise DataFetchError(f"{report_name} page 1 omitted total row count")
    if first.count > _MAX_DATACENTER_ROWS:
        raise _DataResourceLimitError(f"{report_name} row count exceeds limit: {first.count} > {_MAX_DATACENTER_ROWS}")
    if first.pages == 0:
        if first.data or first.count != 0:
            raise DataFetchError(f"{report_name} returned inconsistent zero-page metadata")
        frame = _validated_complete_report_frame(
            [],
            report_name=report_name,
            columns=columns,
            extra_filter=extra_filter,
            expected_count=0,
        )
        _record_datacenter_stat("network_queries")
        # A transient provider/finality window can appear as a zero-row
        # full-market response.  Returning it preserves existing semantics,
        # but not caching it lets the bounded workflow retry recover.
        return frame
    if not first.data:
        raise DataFetchError(f"{report_name} page 1/{first.pages} is empty")

    pages: dict[int, list[dict[str, Any]]] = {1: first.data}
    remaining = range(2, first.pages + 1)
    transient_failures: dict[int, _DataTransientTransportError] = {}

    def retain_page(page: int, page_result: _PageResult) -> None:
        if page_result.page != page:
            raise DataFetchError(
                f"{report_name} response page identity changed during fetch: requested {page}, received {page_result.page}"
            )
        if page_result.pages != first.pages:
            raise DataFetchError(f"page-count changed during fetch: {first.pages} -> {page_result.pages}")
        if page_result.count != first.count:
            raise DataFetchError(f"row-count changed during fetch: {first.count} -> {page_result.count}")
        if not page_result.data:
            raise DataFetchError(f"{report_name} page {page}/{first.pages} is empty")
        pages[page] = page_result.data

    if first.pages > 1:
        worker_count = min(max_workers, _DATACENTER_WORKERS, first.pages - 1)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    _request_page,
                    report_name,
                    columns,
                    page,
                    page_size,
                    "SECURITY_CODE",
                    1,
                    extra_filter,
                ): page
                for page in remaining
            }
            for future in as_completed(futures):
                page = futures[future]
                try:
                    page_result = future.result()
                except _DataResourceLimitError:
                    for pending in futures:
                        pending.cancel()
                    raise
                except _DataTransientTransportError as exc:
                    transient_failures[page] = exc
                    continue
                except DataFetchError:
                    for pending in futures:
                        pending.cancel()
                    raise
                retain_page(page, page_result)

    if len(transient_failures) > _MAX_DATACENTER_RECOVERY_PAGES:
        failed_pages = sorted(transient_failures)
        raise DataFetchError(
            f"{report_name} parallel fetch failed on {len(failed_pages)} pages, "
            f"above recovery limit {_MAX_DATACENTER_RECOVERY_PAGES}: "
            f"{failed_pages[:_MAX_DATACENTER_RECOVERY_PAGES]}"
        ) from transient_failures[failed_pages[0]]

    # Retain every validated parallel page and recover only the small failed
    # subset.  Each recovered response must still match page 1's generation
    # metadata; the complete-frame checks below remain fail closed.
    for page in sorted(transient_failures):
        try:
            recovered = _request_page(
                report_name,
                columns,
                page,
                page_size,
                "SECURITY_CODE",
                1,
                extra_filter,
                timeout=_DATACENTER_RECOVERY_TIMEOUT,
                retries=_DATACENTER_RECOVERY_RETRIES,
                retry_delay=_DATACENTER_RECOVERY_RETRY_DELAY,
            )
        except _DataTransientTransportError as exc:
            raise DataFetchError(f"failed to recover {report_name} page {page}: {exc}") from exc
        retain_page(page, recovered)

    missing = sorted(set(range(1, first.pages + 1)) - pages.keys())
    if missing:
        raise DataFetchError(f"{report_name} missing pages: {missing}")
    ordered_rows = [row for page in range(1, first.pages + 1) for row in pages[page]]
    if len(ordered_rows) != first.count:
        raise DataFetchError(f"{report_name} expected {first.count} rows but received {len(ordered_rows)}")
    frame = _validated_complete_report_frame(
        ordered_rows,
        report_name=report_name,
        columns=columns,
        extra_filter=extra_filter,
        expected_count=first.count,
    )
    _record_datacenter_stat("network_queries")
    _record_datacenter_stat("rows_from_network", len(frame))
    _save_datacenter_report_cache(contract, frame)
    return frame


def _shanghai_today() -> date:
    """Freeze filing cut-offs to the market timezone, not the host machine."""
    return datetime.now(_SHANGHAI).date()


def _latest_completed_annual_year(today: date | None = None) -> int:
    """Latest year whose statutory A-share annual-report window has closed."""
    today = today or _shanghai_today()
    return today.year - 1 if (today.month, today.day) >= (5, 1) else today.year - 2


def _latest_available_q1_year(today: date | None = None) -> int:
    today = today or _shanghai_today()
    return today.year if (today.month, today.day) >= (5, 1) else today.year - 1


def _latest_available_interim_period(today: date | None = None) -> tuple[int, str]:
    """Return the latest interim period after its statutory filing window."""
    today = today or _shanghai_today()
    month_day = (today.month, today.day)
    if month_day >= (11, 1):
        return today.year, "09-30"
    if month_day >= (9, 1):
        return today.year, "06-30"
    if month_day >= (5, 1):
        return today.year, "03-31"
    return today.year - 1, "09-30"


def _required_frames(frames: list[pd.DataFrame], labels: list[str]) -> pd.DataFrame:
    missing = [label for frame, label in zip(frames, labels) if frame.empty]
    if missing:
        raise DataFetchError(f"required report queries returned no rows: {', '.join(missing)}")
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["SECURITY_CODE", "REPORT_DATE"], kind="stable")
        .reset_index(drop=True)
    )


def _normalize_security_codes(codes: Collection[str] | None) -> tuple[str, ...] | None:
    if codes is None:
        return None
    if isinstance(codes, (str, bytes)) or not isinstance(codes, Collection):
        raise ValueError("security codes must be a collection of six-digit strings")
    normalized: set[str] = set()
    for code in codes:
        if isinstance(code, bool) or not isinstance(code, str) or not re.fullmatch(r"\d{6}", code.strip()):
            raise ValueError("security codes must be a collection of six-digit strings")
        normalized.add(code.strip())
    return tuple(sorted(normalized))


def _security_code_filter(codes: Collection[str] | None) -> str:
    """Return a bounded, injection-safe Eastmoney code predicate."""
    normalized = _normalize_security_codes(codes)
    if not normalized or len(normalized) > _MAX_REMOTE_SECURITY_CODES:
        return ""
    quoted = ",".join(f'"{code}"' for code in normalized)
    return f"(SECURITY_CODE in ({quoted}))"


def _combine_period_frames(
    frames: list[pd.DataFrame],
    labels: list[str],
    *,
    codes: tuple[str, ...] | None,
    requested_columns: str,
    remote_filtered: bool = False,
) -> pd.DataFrame:
    if codes is None:
        return _required_frames(frames, labels)

    if remote_filtered:
        nonempty = [frame for frame in frames if not frame.empty]
        if nonempty:
            result = pd.concat(nonempty, ignore_index=True)
        else:
            columns = [column.strip() for column in requested_columns.split(",") if column.strip()]
            result = pd.DataFrame(columns=list(dict.fromkeys(columns)))
    else:
        # Large requested universes intentionally skip the remote predicate to
        # avoid oversized URLs.  They still represent a full-market query, so
        # retain the original all-period/all-report completeness requirement
        # before filtering the returned rows locally.
        result = _required_frames(frames, labels)
    if "SECURITY_CODE" not in result.columns:
        return result

    normalized_rows = result["SECURITY_CODE"].astype(str).str.strip().str.zfill(6)
    requested = set(codes)
    if remote_filtered:
        unexpected = sorted(set(normalized_rows) - requested)
        if unexpected:
            raise DataFetchError(f"filtered financial query returned unexpected security codes: {unexpected[:5]}")
    result = result.loc[normalized_rows.isin(requested)].copy()
    sort_columns = [column for column in ("SECURITY_CODE", "REPORT_DATE") if column in result.columns]
    if sort_columns:
        result = result.sort_values(sort_columns, kind="stable").reset_index(drop=True)
    return result


def fetch_income_history(
    years: list[int] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch complete annual income history, oldest-to-newest per company."""
    if years is None:
        latest = _latest_completed_annual_year()
        years = list(range(latest, latest - ANNUAL_HISTORY_YEARS, -1))
    if not years:
        return pd.DataFrame()
    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    columns = "SECURITY_CODE,SECURITY_NAME_ABBR,TOTAL_OPERATE_INCOME,OPERATE_PROFIT,PARENT_NETPROFIT,REPORT_DATE"
    frames = [_fetch_all_pages(RPT_INCOME, columns, f"(REPORT_DATE='{year}-12-31'){code_filter}") for year in years]
    return _combine_period_frames(
        frames,
        [str(year) for year in years],
        codes=normalized_codes,
        requested_columns=columns,
        remote_filtered=bool(code_filter),
    )


def fetch_latest_cashflow(year: int | None = None) -> pd.DataFrame:
    year = _latest_completed_annual_year() if year is None else int(year)
    columns = "SECURITY_CODE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET,REPORT_DATE"
    frame = _fetch_all_pages(RPT_CASHFLOW, columns, f"(REPORT_DATE='{year}-12-31')")
    if frame.empty:
        raise DataFetchError(f"cashflow {year} returned no rows")
    return frame


def fetch_cashflow_history(
    years: list[int] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch complete annual cash-flow history, oldest-to-newest per company."""
    if years is None:
        latest = _latest_completed_annual_year()
        years = list(range(latest, latest - ANNUAL_HISTORY_YEARS, -1))
    if not years:
        return pd.DataFrame()
    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    columns = "SECURITY_CODE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET,REPORT_DATE"
    frames = [_fetch_all_pages(RPT_CASHFLOW, columns, f"(REPORT_DATE='{year}-12-31'){code_filter}") for year in years]
    return _combine_period_frames(
        frames,
        [str(year) for year in years],
        codes=normalized_codes,
        requested_columns=columns,
        remote_filtered=bool(code_filter),
    )


def _validate_main_financial_indicator_history(frame: pd.DataFrame, years: list[int]) -> pd.DataFrame:
    """Validate and normalize the auditable annual main-financial report."""
    required = {*_MAIN_FINANCIAL_INDICATOR_PROVENANCE, *MAIN_FINANCIAL_INDICATOR_METRICS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFetchError(f"{RPT_MAIN_FINANCIAL_INDICATORS} response omitted columns: {missing}")

    result = frame.loc[:, [*_MAIN_FINANCIAL_INDICATOR_PROVENANCE, *MAIN_FINANCIAL_INDICATOR_METRICS]].copy()
    if result.empty:
        result["SOURCE_REPORT_NAME"] = RPT_MAIN_FINANCIAL_INDICATORS
        return result
    result["REPORT_DATE"] = result["REPORT_DATE"].astype(str).str.slice(0, 10)
    expected_dates = {f"{year}-12-31" for year in years}
    invalid_dates = ~result["REPORT_DATE"].isin(expected_dates)
    if invalid_dates.any():
        examples = sorted(result.loc[invalid_dates, "REPORT_DATE"].unique().tolist())[:5]
        raise DataFetchError(f"annual indicator response contains unexpected report dates: {examples}")

    expected_year = result["REPORT_DATE"].str.slice(0, 4)
    report_year = result["REPORT_YEAR"].astype(str).str.strip()
    report_type = result["REPORT_TYPE"].astype(str).str.strip()
    report_name = result["REPORT_DATE_NAME"].astype(str).str.strip()
    if report_year.ne(expected_year).any():
        raise DataFetchError("annual indicator REPORT_YEAR differs from REPORT_DATE")
    if report_type.ne("年报").any():
        raise DataFetchError("annual indicator query returned a non-annual REPORT_TYPE")
    if report_name.ne(expected_year + "年报").any():
        raise DataFetchError("annual indicator REPORT_DATE_NAME differs from its report period")
    if result["SECURITY_TYPE_CODE"].astype(str).str.strip().ne(_A_SHARE_SECURITY_TYPE).any():
        raise DataFetchError("annual indicator query returned a non-A-share security type")

    identity_source = result[["SECURITY_CODE", "SECURITY_NAME_ABBR", "SECUCODE"]]
    if identity_source.isna().any(axis=None):
        raise DataFetchError("annual indicator response contains an invalid security identity")
    codes = result["SECURITY_CODE"].astype(str).str.strip()
    names = result["SECURITY_NAME_ABBR"].astype(str).str.strip()
    secucodes = result["SECUCODE"].astype(str).str.strip()
    secucode_parts = secucodes.str.rsplit(".", n=1, expand=True)
    if (
        codes.eq("").any()
        or names.eq("").any()
        or secucode_parts.shape[1] != 2
        or secucode_parts[0].ne(codes).any()
        or not secucode_parts[1].isin({"SH", "SZ", "BJ"}).all()
    ):
        raise DataFetchError("annual indicator response contains an invalid security identity")

    notices = result["NOTICE_DATE"]
    present_notices = notices.notna() & notices.astype(str).str.strip().ne("")
    if present_notices.any():
        parsed_notices = pd.to_datetime(notices[present_notices], errors="coerce")
        if parsed_notices.isna().any():
            raise DataFetchError("annual indicator response contains an invalid NOTICE_DATE")
        result.loc[present_notices, "NOTICE_DATE"] = notices[present_notices].astype(str).str.slice(0, 10)

    for column in MAIN_FINANCIAL_INDICATOR_METRICS:
        source = result[column]
        numeric = pd.to_numeric(source, errors="coerce")
        booleans = source.map(lambda value: isinstance(value, bool))
        invalid = source.notna() & (numeric.isna() | booleans)
        finite = numeric.dropna().map(lambda value: math.isfinite(float(value)))
        if invalid.any() or not finite.all():
            raise DataFetchError(f"annual indicator {column} contains a non-finite or non-numeric value")
        result[column] = numeric

    for column in (
        "RDEXPEND",
        "TOTAL_SHARE",
        "STAFF_NUM",
        "TOTALDEPOSITS",
        "GROSSLOANS",
        "LOAN_ADVANCES",
        "CAPITAL_PROVISIONS_SUM",
        "EARNED_PREMIUM",
    ):
        numeric = result[column].dropna()
        if (numeric < 0).any():
            raise DataFetchError(f"annual indicator {column} contains a negative value")
    for column in ("TOTAL_SHARE", "STAFF_NUM"):
        numeric = result[column].dropna()
        if numeric.map(lambda value: not float(value).is_integer()).any():
            raise DataFetchError(f"annual indicator {column} contains a fractional count")
    if result[list(MAIN_FINANCIAL_INDICATOR_METRICS)].isna().all(axis=1).any():
        raise DataFetchError("annual indicator response contains a row without any requested metric")

    duplicate = result.duplicated(["SECURITY_CODE", "REPORT_DATE"], keep=False)
    if duplicate.any():
        examples = (
            result.loc[duplicate, ["SECURITY_CODE", "REPORT_DATE"]].drop_duplicates().head(5).to_dict(orient="records")
        )
        raise DataFetchError(f"duplicate annual indicator identities: {examples}")
    result["SOURCE_REPORT_NAME"] = RPT_MAIN_FINANCIAL_INDICATORS
    return result.sort_values(["SECURITY_CODE", "REPORT_DATE"], kind="stable").reset_index(drop=True)


def fetch_main_financial_indicator_history(
    years: list[int] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch ten complete annual histories from Eastmoney's main-financial report."""
    if years is None:
        latest = _latest_completed_annual_year()
        years = list(range(latest, latest - ANNUAL_HISTORY_YEARS, -1))
    if not years:
        return pd.DataFrame()
    if any(isinstance(year, bool) or not isinstance(year, int) for year in years):
        raise ValueError("indicator years must be integers")
    if len(set(years)) != len(years):
        raise ValueError("indicator years must not contain duplicates")
    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    frames = [
        _fetch_all_pages(
            RPT_MAIN_FINANCIAL_INDICATORS,
            _MAIN_FINANCIAL_INDICATOR_COLUMNS,
            (f"(REPORT_DATE='{year}-12-31')(SECURITY_TYPE_CODE=\"{_A_SHARE_SECURITY_TYPE}\"){code_filter}"),
        )
        for year in years
    ]
    frame = _combine_period_frames(
        frames,
        [f"main financial indicators {year}" for year in years],
        codes=normalized_codes,
        requested_columns=_MAIN_FINANCIAL_INDICATOR_COLUMNS,
        remote_filtered=bool(code_filter),
    )
    return _validate_main_financial_indicator_history(frame, years)


_BALANCE_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "TOTAL_PARENT_EQUITY": (
        "TOTAL_PARENT_EQUITY",
        "PARENT_EQUITY",
        "TOTAL_EQUITY_PARENT",
        "EQUITY_PARENT",
    ),
    "MINORITY_EQUITY": ("MINORITY_EQUITY", "MINORITY_INTEREST"),
    "LONG_LOAN": ("LONG_LOAN", "LONG_TERM_LOAN"),
    "BONDS_PAYABLE": ("BONDS_PAYABLE", "BOND_PAYABLE"),
    "NONCURRENT_LIAB_1YEAR": (
        "NONCURRENT_LIAB_1YEAR",
        "NONCURRENT_LIABILITY_IN_1YEAR",
        "CURRENT_PORTION_NONCURRENT_LIAB",
    ),
    "LEASE_LIAB": ("LEASE_LIAB", "LEASE_LIABILITY"),
    "SHORT_BONDS_PAYABLE": ("SHORT_BONDS_PAYABLE", "SHORT_BOND_PAYABLE"),
    "BORROW_FUNDS": ("BORROW_FUNDS", "BORROW_FUND"),
    "CENTRAL_BANK_BORROWING": ("CENTRAL_BANK_BORROWING", "LOAN_PBC"),
    "SUBORDINATED_BONDS_PAYABLE": (
        "SUBORDINATED_BONDS_PAYABLE",
        "SUBBOND_PAYABLE",
    ),
}

_BALANCE_COMMON_COLUMNS = (
    "SECURITY_CODE,SECUCODE,SECURITY_TYPE_CODE,REPORT_DATE,TOTAL_ASSETS,"
    "TOTAL_LIABILITIES,TOTAL_EQUITY,TOTAL_PARENT_EQUITY,MINORITY_EQUITY,GOODWILL"
)
_BALANCE_REPORT_COLUMNS = {
    RPT_BALANCE: (
        _BALANCE_COMMON_COLUMNS + ",MONETARYFUNDS,SHORT_LOAN,LONG_LOAN,BOND_PAYABLE,"
        "NONCURRENT_LIAB_1YEAR,LEASE_LIAB,SHORT_BOND_PAYABLE"
    ),
    # Bank balance sheets do not expose corporate cash/loan line items.
    RPT_BALANCE_BANK: (_BALANCE_COMMON_COLUMNS + ",BOND_PAYABLE,LEASE_LIAB,BORROW_FUND,LOAN_PBC,SUBBOND_PAYABLE"),
    RPT_BALANCE_INSURANCE: (_BALANCE_COMMON_COLUMNS + ",MONETARYFUNDS,SHORT_LOAN,LONG_LOAN,BOND_PAYABLE,LEASE_LIAB"),
    RPT_BALANCE_SECURITIES: (
        _BALANCE_COMMON_COLUMNS + ",MONETARYFUNDS,SHORT_LOAN,LONG_LOAN,BOND_PAYABLE,LEASE_LIAB,BORROW_FUND"
    ),
}


def _add_canonical_balance_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for canonical, aliases in _BALANCE_CANONICAL_ALIASES.items():
        present = [alias for alias in aliases if alias in frame.columns]
        if not present:
            frame[canonical] = None
            continue
        combined = frame[present[0]]
        for alias in present[1:]:
            combined = combined.combine_first(frame[alias])
        frame[canonical] = combined
    for required in (
        "TOTAL_ASSETS",
        "TOTAL_LIABILITIES",
        "TOTAL_EQUITY",
        "DEBT_ASSET_RATIO",
        "MONETARYFUNDS",
        "SHORT_LOAN",
    ):
        if required not in frame.columns:
            frame[required] = None
    # The full F10 reports do not expose the summary-table ratio. This is an
    # exact derivation from the same consolidated totals, not an estimate.
    assets = pd.to_numeric(frame["TOTAL_ASSETS"], errors="coerce")
    liabilities = pd.to_numeric(frame["TOTAL_LIABILITIES"], errors="coerce")
    derived_ratio = (liabilities / assets.where(assets.ne(0))) * 100
    existing_ratio = pd.to_numeric(frame["DEBT_ASSET_RATIO"], errors="coerce")
    frame["DEBT_ASSET_RATIO"] = existing_ratio.fillna(derived_ratio)
    return frame


def fetch_balance_history(
    years: list[int] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch ten complete annual balance snapshots for every A-share org type.

    Eastmoney separates general companies, banks, insurers and securities
    firms into four full-balance reports. All four are required for every year;
    this preserves reported parent equity and never substitutes total equity.
    """
    if years is None:
        latest = _latest_completed_annual_year()
        years = list(range(latest, latest - ANNUAL_HISTORY_YEARS, -1))
    if not years:
        return pd.DataFrame()
    normalized_codes = _normalize_security_codes(codes)
    all_requested_columns = ",".join(_BALANCE_REPORT_COLUMNS.values())
    yearly_frames: list[pd.DataFrame] = []
    resolved_single_report: str | None = None

    def fetch_full_year(year: int) -> tuple[pd.DataFrame, list[tuple[str, pd.DataFrame]]]:
        report_frames: list[pd.DataFrame] = []
        labels: list[str] = []
        base_filter = f"(REPORT_DATE='{year}-12-31')(SECURITY_TYPE_CODE=\"{_A_SHARE_SECURITY_TYPE}\")"
        by_report: list[tuple[str, pd.DataFrame]] = []
        for report_name, columns in _BALANCE_REPORT_COLUMNS.items():
            report_frame = _fetch_all_pages(report_name, columns, base_filter)
            report_frames.append(report_frame)
            by_report.append((report_name, report_frame))
            labels.append(f"{report_name}:{year}")
        complete = _required_frames(report_frames, labels)
        filtered = _combine_period_frames(
            [complete],
            [str(year)],
            codes=normalized_codes,
            requested_columns=all_requested_columns,
        )
        return filtered, by_report

    for year in years:
        if normalized_codes is None or len(normalized_codes) != 1:
            filtered, _ = fetch_full_year(year)
            yearly_frames.append(filtered)
            continue

        code = normalized_codes[0]
        code_filter = _security_code_filter(normalized_codes)
        candidate_reports = (
            [resolved_single_report] if resolved_single_report is not None else list(_BALANCE_REPORT_COLUMNS)
        )
        filtered_frame: pd.DataFrame | None = None
        filtered_errors: list[DataFetchError] = []
        for report_name in candidate_reports:
            if report_name is None:
                continue
            columns = _BALANCE_REPORT_COLUMNS[report_name]
            report_filter = (
                f"(REPORT_DATE='{year}-12-31')(SECURITY_TYPE_CODE=\"{_A_SHARE_SECURITY_TYPE}\"){code_filter}"
            )
            try:
                candidate = _fetch_all_pages(report_name, columns, report_filter)
            except DataFetchError as exc:
                filtered_errors.append(exc)
                continue
            candidate = _combine_period_frames(
                [candidate],
                [f"{report_name}:{year}"],
                codes=normalized_codes,
                requested_columns=columns,
                remote_filtered=True,
            )
            if not candidate.empty:
                resolved_single_report = report_name
                filtered_frame = candidate
                break
            if resolved_single_report is not None:
                filtered_frame = candidate
                break

        if filtered_frame is None:
            if filtered_errors:
                # Some F10 organization-specific reports reject a code that
                # belongs to another report type.  If no filtered report has
                # proved the row, fall back to the complete four-report year
                # rather than mistaking an upstream error for a missing fact.
                filtered_frame, by_report = fetch_full_year(year)
                for report_name, report_frame in by_report:
                    codes_in_report = report_frame["SECURITY_CODE"].astype(str).str.strip().str.zfill(6)
                    if codes_in_report.eq(code).any():
                        resolved_single_report = report_name
                        break
            else:
                filtered_frame = _combine_period_frames(
                    [],
                    [str(year)],
                    codes=normalized_codes,
                    requested_columns=all_requested_columns,
                    remote_filtered=True,
                )
        yearly_frames.append(filtered_frame)

    frame = _combine_period_frames(
        yearly_frames,
        [str(year) for year in years],
        codes=normalized_codes,
        requested_columns=all_requested_columns,
        remote_filtered=normalized_codes is not None and len(normalized_codes) == 1,
    )
    duplicate = frame.duplicated(["SECURITY_CODE", "REPORT_DATE"], keep=False)
    if duplicate.any():
        examples = (
            frame.loc[duplicate, ["SECURITY_CODE", "REPORT_DATE"]].drop_duplicates().head(5).to_dict(orient="records")
        )
        raise DataFetchError(f"duplicate balance rows across report types: {examples}")
    return _add_canonical_balance_columns(frame)


def fetch_latest_balance(year: int | None = None) -> pd.DataFrame:
    year = _latest_completed_annual_year() if year is None else int(year)
    return fetch_balance_history([year])


def fetch_latest_q1_income(year: int | None = None) -> pd.DataFrame:
    year = _latest_available_q1_year() if year is None else int(year)
    columns = "SECURITY_CODE,SECURITY_NAME_ABBR,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,REPORT_DATE"
    frame = _fetch_all_pages(RPT_INCOME, columns, f"(REPORT_DATE='{year}-03-31')")
    if frame.empty:
        raise DataFetchError(f"income Q1 {year} returned no rows")
    return frame


def fetch_latest_q1_cashflow(year: int | None = None) -> pd.DataFrame:
    year = _latest_available_q1_year() if year is None else int(year)
    columns = "SECURITY_CODE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET,REPORT_DATE"
    frame = _fetch_all_pages(RPT_CASHFLOW, columns, f"(REPORT_DATE='{year}-03-31')")
    if frame.empty:
        raise DataFetchError(f"cashflow Q1 {year} returned no rows")
    return frame


def fetch_interim_income_comparables(
    period: tuple[int, str] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch the latest filed interim income period and prior-year comparable."""
    year, period_end = _latest_available_interim_period() if period is None else period
    year = int(year)
    if period_end not in {"03-31", "06-30", "09-30"}:
        raise ValueError("interim period_end must be 03-31, 06-30, or 09-30")
    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    columns = "SECURITY_CODE,SECURITY_NAME_ABBR,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,REPORT_DATE"
    years = [year - 1, year]
    frames = [
        _fetch_all_pages(RPT_INCOME, columns, f"(REPORT_DATE='{item}-{period_end}'){code_filter}") for item in years
    ]
    return _combine_period_frames(
        frames,
        [f"income interim {item}-{period_end}" for item in years],
        codes=normalized_codes,
        requested_columns=columns,
        remote_filtered=bool(code_filter),
    )


def fetch_interim_cashflow_comparables(
    period: tuple[int, str] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch the latest filed interim cash-flow period and prior-year comparable."""
    year, period_end = _latest_available_interim_period() if period is None else period
    year = int(year)
    if period_end not in {"03-31", "06-30", "09-30"}:
        raise ValueError("interim period_end must be 03-31, 06-30, or 09-30")
    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    columns = "SECURITY_CODE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET,REPORT_DATE"
    years = [year - 1, year]
    frames = [
        _fetch_all_pages(RPT_CASHFLOW, columns, f"(REPORT_DATE='{item}-{period_end}'){code_filter}") for item in years
    ]
    return _combine_period_frames(
        frames,
        [f"cashflow interim {item}-{period_end}" for item in years],
        codes=normalized_codes,
        requested_columns=columns,
        remote_filtered=bool(code_filter),
    )


def _validate_detailed_interim_cashflow(
    frame: pd.DataFrame,
    expected_dates: list[str],
    *,
    require_all_dates: bool = True,
) -> pd.DataFrame:
    """Validate detailed investing-cash evidence without coercing blanks to zero."""
    required = {*_DETAILED_CASHFLOW_METADATA, *_DETAILED_INVESTMENT_FIELDS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFetchError(f"{RPT_DETAILED_CASHFLOW} response omitted columns: {missing}")

    result = frame.loc[:, [*_DETAILED_CASHFLOW_METADATA, *_DETAILED_INVESTMENT_FIELDS]].copy()
    if result.empty:
        result["SOURCE_REPORT_NAME"] = RPT_DETAILED_CASHFLOW
        result["SOURCE_REPORT_URL"] = EASTMONEY_DATACENTER_URL
        return result
    result["REPORT_DATE"] = result["REPORT_DATE"].astype(str).str.slice(0, 10)
    expected_set = set(expected_dates)
    actual_dates = set(result["REPORT_DATE"].tolist())
    if not actual_dates.issubset(expected_set):
        examples = sorted(actual_dates - expected_set)[:5]
        raise DataFetchError(f"detailed interim cash-flow contains unexpected report dates: {examples}")
    absent_dates = [report_date for report_date in expected_dates if report_date not in actual_dates]
    if require_all_dates and absent_dates:
        raise DataFetchError(f"detailed interim cash-flow omitted required report dates: {absent_dates}")

    expected_type = result["REPORT_DATE"].map(lambda report_date: _INTERIM_REPORT_LABELS[report_date[5:]][0])
    expected_name = result["REPORT_DATE"].str.slice(0, 4) + result["REPORT_DATE"].map(
        lambda report_date: _INTERIM_REPORT_LABELS[report_date[5:]][1]
    )
    if result["REPORT_TYPE"].astype(str).str.strip().ne(expected_type).any():
        raise DataFetchError("detailed interim cash-flow query returned an incompatible REPORT_TYPE")
    if result["REPORT_DATE_NAME"].astype(str).str.strip().ne(expected_name).any():
        raise DataFetchError("detailed interim cash-flow REPORT_DATE_NAME differs from its period")
    if result["SECURITY_TYPE_CODE"].astype(str).str.strip().ne(_A_SHARE_SECURITY_TYPE).any():
        raise DataFetchError("detailed interim cash-flow query returned a non-A-share security type")
    if result["CURRENCY"].astype(str).str.strip().ne("CNY").any():
        raise DataFetchError("detailed interim cash-flow query returned a non-CNY statement")

    identity_source = result[["SECURITY_CODE", "SECURITY_NAME_ABBR", "SECUCODE"]]
    if identity_source.isna().any(axis=None):
        raise DataFetchError("detailed interim cash-flow contains an invalid security identity")
    codes = result["SECURITY_CODE"].astype(str).str.strip()
    names = result["SECURITY_NAME_ABBR"].astype(str).str.strip()
    secucodes = result["SECUCODE"].astype(str).str.strip()
    secucode_parts = secucodes.str.rsplit(".", n=1, expand=True)
    if (
        codes.eq("").any()
        or names.eq("").any()
        or secucode_parts.shape[1] != 2
        or secucode_parts[0].ne(codes).any()
        or not secucode_parts[1].isin({"SH", "SZ", "BJ"}).all()
    ):
        raise DataFetchError("detailed interim cash-flow contains an invalid security identity")

    for column in ("NOTICE_DATE", "UPDATE_DATE"):
        source = result[column]
        present = source.notna() & source.astype(str).str.strip().ne("")
        if present.any():
            parsed = pd.to_datetime(source[present], errors="coerce")
            if parsed.isna().any():
                raise DataFetchError(f"detailed interim cash-flow contains an invalid {column}")
            result.loc[present, column] = source[present].astype(str).str.slice(0, 10)

    for column in _DETAILED_INVESTMENT_FIELDS:
        source = result[column]
        numeric = pd.to_numeric(source, errors="coerce")
        booleans = source.map(lambda value: isinstance(value, bool))
        invalid = source.notna() & (numeric.isna() | booleans)
        finite = numeric.dropna().map(lambda value: math.isfinite(float(value)))
        if invalid.any() or not finite.all():
            raise DataFetchError(f"detailed interim cash-flow {column} contains a non-finite value")
        result[column] = numeric

    duplicate = result.duplicated(["SECURITY_CODE", "REPORT_DATE"], keep=False)
    if duplicate.any():
        examples = (
            result.loc[duplicate, ["SECURITY_CODE", "REPORT_DATE"]].drop_duplicates().head(5).to_dict(orient="records")
        )
        raise DataFetchError(f"duplicate detailed interim cash-flow identities: {examples}")
    result["SOURCE_REPORT_NAME"] = RPT_DETAILED_CASHFLOW
    result["SOURCE_REPORT_URL"] = EASTMONEY_DATACENTER_URL
    return result.sort_values(["SECURITY_CODE", "REPORT_DATE"], kind="stable").reset_index(drop=True)


def fetch_detailed_interim_cashflow_comparables(
    period: tuple[int, str] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch detailed current/prior YTD statements used only for capex evidence."""
    year, period_end = _latest_available_interim_period() if period is None else period
    year = int(year)
    if period_end not in _INTERIM_REPORT_LABELS:
        raise ValueError("interim period_end must be 03-31, 06-30, or 09-30")
    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    years = [year - 1, year]
    expected_dates = [f"{item}-{period_end}" for item in years]
    frames = [
        _fetch_all_pages(
            RPT_DETAILED_CASHFLOW,
            _DETAILED_CASHFLOW_COLUMNS,
            (f"(REPORT_DATE='{report_date}')(SECURITY_TYPE_CODE=\"{_A_SHARE_SECURITY_TYPE}\"){code_filter}"),
        )
        for report_date in expected_dates
    ]
    frame = _combine_period_frames(
        frames,
        [f"detailed cashflow interim {report_date}" for report_date in expected_dates],
        codes=normalized_codes,
        requested_columns=_DETAILED_CASHFLOW_COLUMNS,
        remote_filtered=bool(code_filter),
    )
    return _validate_detailed_interim_cashflow(
        frame,
        expected_dates,
        require_all_dates=normalized_codes is None,
    )


def _validate_detailed_annual_cashflow(
    frame: pd.DataFrame,
    expected_dates: list[str],
    *,
    require_all_dates: bool = True,
) -> pd.DataFrame:
    """Validate detailed annual investing-cash evidence without imputing blanks."""
    required = {*_DETAILED_CASHFLOW_METADATA, *_DETAILED_INVESTMENT_FIELDS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFetchError(f"{RPT_DETAILED_CASHFLOW} response omitted columns: {missing}")

    result = frame.loc[:, [*_DETAILED_CASHFLOW_METADATA, *_DETAILED_INVESTMENT_FIELDS]].copy()
    if result.empty:
        result["SOURCE_REPORT_NAME"] = RPT_DETAILED_CASHFLOW
        result["SOURCE_REPORT_URL"] = EASTMONEY_DATACENTER_URL
        return result
    result["REPORT_DATE"] = result["REPORT_DATE"].astype(str).str.slice(0, 10)
    expected_set = set(expected_dates)
    actual_dates = set(result["REPORT_DATE"].tolist())
    if not actual_dates.issubset(expected_set):
        examples = sorted(actual_dates - expected_set)[:5]
        raise DataFetchError(f"detailed annual cash-flow contains unexpected report dates: {examples}")
    absent_dates = [report_date for report_date in expected_dates if report_date not in actual_dates]
    if require_all_dates and absent_dates:
        raise DataFetchError(f"detailed annual cash-flow omitted required report dates: {absent_dates}")

    expected_name = result["REPORT_DATE"].str.slice(0, 4) + "年报"
    if result["REPORT_TYPE"].astype(str).str.strip().ne("年报").any():
        raise DataFetchError("detailed annual cash-flow query returned an incompatible REPORT_TYPE")
    if result["REPORT_DATE_NAME"].astype(str).str.strip().ne(expected_name).any():
        raise DataFetchError("detailed annual cash-flow REPORT_DATE_NAME differs from its period")
    if result["SECURITY_TYPE_CODE"].astype(str).str.strip().ne(_A_SHARE_SECURITY_TYPE).any():
        raise DataFetchError("detailed annual cash-flow query returned a non-A-share security type")
    if result["CURRENCY"].astype(str).str.strip().ne("CNY").any():
        raise DataFetchError("detailed annual cash-flow query returned a non-CNY statement")

    identity_source = result[["SECURITY_CODE", "SECURITY_NAME_ABBR", "SECUCODE"]]
    if identity_source.isna().any(axis=None):
        raise DataFetchError("detailed annual cash-flow contains an invalid security identity")
    codes = result["SECURITY_CODE"].astype(str).str.strip()
    names = result["SECURITY_NAME_ABBR"].astype(str).str.strip()
    secucodes = result["SECUCODE"].astype(str).str.strip()
    secucode_parts = secucodes.str.rsplit(".", n=1, expand=True)
    if (
        codes.eq("").any()
        or names.eq("").any()
        or secucode_parts.shape[1] != 2
        or secucode_parts[0].ne(codes).any()
        or not secucode_parts[1].isin({"SH", "SZ", "BJ"}).all()
    ):
        raise DataFetchError("detailed annual cash-flow contains an invalid security identity")

    for column in ("NOTICE_DATE", "UPDATE_DATE"):
        source = result[column]
        present = source.notna() & source.astype(str).str.strip().ne("")
        if present.any():
            parsed = pd.to_datetime(source[present], errors="coerce")
            if parsed.isna().any():
                raise DataFetchError(f"detailed annual cash-flow contains an invalid {column}")
            result.loc[present, column] = source[present].astype(str).str.slice(0, 10)

    for column in _DETAILED_INVESTMENT_FIELDS:
        source = result[column]
        numeric = pd.to_numeric(source, errors="coerce")
        booleans = source.map(lambda value: isinstance(value, bool))
        invalid = source.notna() & (numeric.isna() | booleans)
        finite = numeric.dropna().map(lambda value: math.isfinite(float(value)))
        if invalid.any() or not finite.all():
            raise DataFetchError(f"detailed annual cash-flow {column} contains a non-finite value")
        result[column] = numeric

    duplicate = result.duplicated(["SECURITY_CODE", "REPORT_DATE"], keep=False)
    if duplicate.any():
        examples = (
            result.loc[duplicate, ["SECURITY_CODE", "REPORT_DATE"]].drop_duplicates().head(5).to_dict(orient="records")
        )
        raise DataFetchError(f"duplicate detailed annual cash-flow identities: {examples}")
    result["SOURCE_REPORT_NAME"] = RPT_DETAILED_CASHFLOW
    result["SOURCE_REPORT_URL"] = EASTMONEY_DATACENTER_URL
    return result.sort_values(["SECURITY_CODE", "REPORT_DATE"], kind="stable").reset_index(drop=True)


def fetch_detailed_annual_cashflow_history(
    years: list[int] | None = None,
    *,
    codes: Collection[str] | None = None,
) -> pd.DataFrame:
    """Fetch detailed annual investing-cash rows for bounded Type 3 evidence.

    Blank line items remain ``NaN``/``None``.  This function does not infer
    that an unreported acquisition cash-flow value is zero.
    """
    if years is None:
        latest = _latest_completed_annual_year()
        years = list(range(latest, latest - DETAILED_ANNUAL_HISTORY_YEARS, -1))
    if isinstance(years, (str, bytes)) or not isinstance(years, list):
        raise ValueError("detailed annual years must be a list of integers")
    latest_completed = _latest_completed_annual_year()
    if any(
        isinstance(year, bool) or not isinstance(year, int) or not 1990 <= year <= latest_completed for year in years
    ):
        raise ValueError(f"detailed annual years must be integers between 1990 and {latest_completed}")
    if len(set(years)) != len(years):
        raise ValueError("detailed annual years must not contain duplicates")
    if not years:
        return pd.DataFrame()

    normalized_codes = _normalize_security_codes(codes)
    code_filter = _security_code_filter(normalized_codes)
    expected_dates = [f"{year}-12-31" for year in years]
    frames = [
        _fetch_all_pages(
            RPT_DETAILED_CASHFLOW,
            _DETAILED_CASHFLOW_COLUMNS,
            (f"(REPORT_DATE='{report_date}')(SECURITY_TYPE_CODE=\"{_A_SHARE_SECURITY_TYPE}\"){code_filter}"),
        )
        for report_date in expected_dates
    ]
    frame = _combine_period_frames(
        frames,
        [f"detailed cashflow annual {report_date}" for report_date in expected_dates],
        codes=normalized_codes,
        requested_columns=_DETAILED_CASHFLOW_COLUMNS,
        remote_filtered=bool(code_filter),
    )
    return _validate_detailed_annual_cashflow(
        frame,
        expected_dates,
        require_all_dates=normalized_codes is None,
    )


def fetch_interim_financials_parallel(
    *,
    codes: Collection[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return one coherent income/compact-cashflow/detailed-cashflow batch."""
    normalized_codes = _normalize_security_codes(codes)
    if normalized_codes is None:
        fetchers = {
            "income_interim": fetch_interim_income_comparables,
            "cashflow_interim": fetch_interim_cashflow_comparables,
            "detailed_cashflow_interim": fetch_detailed_interim_cashflow_comparables,
        }
    else:
        fetchers = {
            "income_interim": lambda: fetch_interim_income_comparables(codes=normalized_codes),
            "cashflow_interim": lambda: fetch_interim_cashflow_comparables(codes=normalized_codes),
            "detailed_cashflow_interim": lambda: fetch_detailed_interim_cashflow_comparables(codes=normalized_codes),
        }
    worker_count = max(1, min(len(fetchers), int(CONCURRENCY) // _DATACENTER_WORKERS))
    results: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(fetch): label for label, fetch in fetchers.items()}
        for future in as_completed(futures):
            label = futures[future]
            try:
                results[label] = future.result()
            except DataFetchError as exc:
                for pending in futures:
                    pending.cancel()
                raise DataFetchError(f"interim financial refresh failed for {label}: {exc}") from exc
    return (
        results["income_interim"],
        results["cashflow_interim"],
        results["detailed_cashflow_interim"],
    )


def fetch_all_financials_parallel(
    *,
    codes: Collection[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return one all-or-error annual financial refresh batch.

    Income, cash flow, balance sheets and main-financial indicators start in
    the same batch. The outer and page pools share a fixed concurrency budget,
    so nested pagination cannot multiply request pressure beyond CONCURRENCY.
    """
    normalized_codes = _normalize_security_codes(codes)
    if normalized_codes is None:
        fetchers = {
            "income": fetch_income_history,
            "cashflow": fetch_cashflow_history,
            "balance": fetch_balance_history,
            "indicators": fetch_main_financial_indicator_history,
        }
    else:
        fetchers = {
            "income": lambda: fetch_income_history(codes=normalized_codes),
            "cashflow": lambda: fetch_cashflow_history(codes=normalized_codes),
            "balance": lambda: fetch_balance_history(codes=normalized_codes),
            "indicators": lambda: fetch_main_financial_indicator_history(codes=normalized_codes),
        }
    results: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=_DATACENTER_BATCH_WORKERS) as executor:
        futures = {executor.submit(fetch): label for label, fetch in fetchers.items()}
        for future in as_completed(futures):
            label = futures[future]
            try:
                results[label] = future.result()
            except DataFetchError as exc:
                for pending in futures:
                    pending.cancel()
                raise DataFetchError(f"annual financial refresh failed for {label}: {exc}") from exc
    return results["income"], results["cashflow"], results["balance"], results["indicators"]
