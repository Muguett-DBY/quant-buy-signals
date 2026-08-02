"""Strict, auditable Type 3 growth-evidence acquisition.

The segment adapter reads Eastmoney's annual main-business composition rows
and computes only reproducible product/industry/region metrics.  The external
growth adapter combines annual acquisition-cash, goodwill and revenue facts as
an aggregate proxy.  It never labels that proxy a transaction census and never
coerces an unresolved blank cash-flow cell to zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import requests

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS
from data.cache import SafeCacheConflict, SafeCacheError, SafeFileCache
from data.capex_evidence import CAPEX_FIELD, EASTMONEY_DATACENTER_URL, NON_CAPEX_OUTFLOW_FIELDS
from data.datacenter import DataFetchError, fetch_detailed_annual_cashflow_history


MODEL_ID = "type3-growth-evidence-v1"
SEGMENT_MODEL_ID = "type3-segment-growth-v1"
EXTERNAL_MODEL_ID = "type3-external-growth-aggregate-v1"
EXTERNAL_CACHE_MODEL_ID = "type3-external-growth-cache-v1"

EASTMONEY_BUSINESS_ENDPOINT = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
EASTMONEY_BUSINESS_PAGE = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/Index"
SEGMENT_CACHE_DIR = CACHE_DIRECTORY / "growth_evidence"

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_ROWS = 1_000
# The whole-market pipeline can have roughly five thousand eligible rows.
# Keep the public batch contract above the complete A-share universe so the
# scorer cannot fail merely because a refreshed evidence tranche is larger
# than the old 2,000-company ceiling.  The annual cash-flow source remains
# explicitly chunked below; the segment adapter uses a small worker pool
# plus its global rate limiter (verified against Eastmoney at 8 workers
# with no throttling and a 93% segment completion rate).
MAX_BATCH_COMPANIES = 6_000
_CASHFLOW_BATCH_COMPANIES = 100
MAX_WORKERS = 4
MAX_SEGMENT_HISTORY_YEARS = 10
MIN_SEGMENT_HISTORY_YEARS = 3
EXTERNAL_HISTORY_YEARS = 5
MIN_EXTERNAL_HISTORY_YEARS = 5
CACHE_SCHEMA_VERSION = 1
SEGMENT_CACHE_REUSE_DAYS = 21
# The annual cash-flow capture and the segment capture both remain safe to
# reuse only inside the same post-reporting window.  This is deliberately not
# a rolling TTL: crossing a completed annual-report cutoff always requires a
# fresh source capture.
EXTERNAL_CACHE_REUSE_DAYS = SEGMENT_CACHE_REUSE_DAYS
TYPE3_GROWTH_TRANSIENT_RETRY_DAYS = 1
TYPE3_GROWTH_STRUCTURAL_RETRY_DAYS = 7
TYPE3_GROWTH_RETRY_MODEL_ID = "type3-growth-retry-v1"
# Preserve market-wide coverage progress while guaranteeing that a continuous
# flow of never-seen companies cannot indefinitely starve due retries.
TYPE3_GROWTH_DUE_RETRY_RESERVE_RATIO = 0.20
REQUEST_TIMEOUT = (15, 30)
REQUEST_ATTEMPTS = 4
REQUEST_INTERVAL_SECONDS = 0.25
RETRY_BACKOFF_SECONDS = 2.0
# Eastmoney amounts are reported to fen precision.  The investing identity has
# at most nine component cells, so ten fen is a conservative absolute
# statement-rounding bound.  A relative tolerance would incorrectly treat
# tens of yuan as zero on a very large company.
_CURRENCY_IDENTITY_TOLERANCE = Decimal("0.10")

_A_SHARE_CODE = re.compile(r"^[036][0-9]{5}$")
_CANONICAL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_REPORT_DATE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"(?:[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?)?$"
)
_TOP_LEVEL_FIELDS = {"zyfw", "zygcfx", "jyps"}
_SEGMENT_ROW_FIELDS = {
    "SECUCODE",
    "SECURITY_CODE",
    "REPORT_DATE",
    "MAINOP_TYPE",
    "ITEM_NAME",
    "MAIN_BUSINESS_INCOME",
    "MBI_RATIO",
    "MAIN_BUSINESS_COST",
    "MBC_RATIO",
    "MAIN_BUSINESS_RPOFIT",
    "MBR_RATIO",
    "GROSS_RPOFIT_RATIO",
    "RANK",
}
_NORMALIZED_SEGMENT_FIELDS = {
    "security_code",
    "secucode",
    "report_date",
    "mainop_type",
    "dimension",
    "item_name",
    "revenue",
    "reported_share",
    "rank",
}
_DIMENSIONS = {1: "industry", 2: "product", 3: "region"}
_DIMENSION_PREFERENCE = (2, 1, 3)
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": EASTMONEY_BUSINESS_PAGE,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "identity",
}
_EXTERNAL_COMPONENT_FIELDS = (CAPEX_FIELD, *NON_CAPEX_OUTFLOW_FIELDS)
_ACQUISITION_FIELD = "OBTAIN_SUBSIDIARY_OTHER"
_NET_OUTFLOW_IDENTITY_FIELDS = (
    "TOTAL_INVEST_INFLOW",
    "INVEST_NETCASH_OTHER",
    "INVEST_NETCASH_BALANCE",
    "NETCASH_INVEST",
)
_TOP_EVIDENCE_FIELDS = {
    "available",
    "code",
    "as_of",
    "model_id",
    "external_growth_evidence",
    "segment_growth_sources",
    "cache_hit",
    "cache_diagnostic",
    "reason",
}
_SEGMENT_EVIDENCE_FIELDS = {
    "status",
    "source",
    "source_url",
    "evidence_id",
    "as_of",
    "security_code",
    "model_id",
    "contract_scope",
    "dimension",
    "history_years",
    "growth_source_count",
    "effective_growth_source_count",
    "positive_growth_share",
    "revenue_hhi",
    "top_segment_share",
    "matched_latest_share",
    "annual_revenue_latest",
    "source_row_count",
    "aggregate_revenue_cagr",
    "segments",
    "records",
    "reason",
}
_EXTERNAL_EVIDENCE_FIELDS = {
    "status",
    "source",
    "source_url",
    "evidence_id",
    "as_of",
    "security_code",
    "model_id",
    "contract_scope",
    "coverage_years",
    "coverage_year_count",
    "acquisition_cash_values",
    "acquisition_cash_to_revenue",
    "aggregate_acquisition_cash_to_revenue",
    "positive_goodwill_additions_to_revenue",
    "goodwill_to_revenue_latest",
    "goodwill_change_to_revenue",
    "records",
    "limitations",
    "reason",
}
_EXTERNAL_RECORD_FIELDS = {
    "year",
    "report_date",
    "revenue",
    "goodwill",
    "acquisition_cash",
    "acquisition_cash_to_revenue",
    "acquisition_value_basis",
    "acquisition_derivation",
    "source_report",
    "source_field",
    "source_url",
}
_SEGMENT_CACHE_STATE_FIELDS = {
    "segment_growth_sources",
    "source_as_of",
    "cache_diagnostic",
}
_EXTERNAL_CACHE_STATE_FIELDS = {
    "external_growth_evidence",
    "source_as_of",
    "cache_diagnostic",
}
_TYPE3_GROWTH_RETRY_STATE_FIELDS = {
    "model_id",
    "code",
    "last_attempt_as_of",
    "retry_class",
    "retry_after",
    "reason",
}
_TYPE3_GROWTH_RETRY_CLASSES = {"structural", "transient"}
_TYPE3_GROWTH_TRANSIENT_REASON_MARKERS = ("source_unavailable:", "worker_failure:")


class GrowthEvidenceError(RuntimeError):
    """A Type 3 source, response, cache or evidence contract failed."""


@dataclass(frozen=True)
class GrowthEvidence:
    available: bool
    code: str
    as_of: str
    model_id: str
    external_growth_evidence: dict[str, Any]
    segment_growth_sources: dict[str, Any]
    cache_hit: bool
    cache_diagnostic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _RequestRateLimiter:
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


_GLOBAL_RATE_LIMITER = _RequestRateLimiter(REQUEST_INTERVAL_SECONDS)


def _error_label(exc: BaseException, *, limit: int = 180) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}:{message[:limit]}" if message else type(exc).__name__


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if not _A_SHARE_CODE.fullmatch(code):
        raise ValueError("growth-evidence code must be a Shanghai/Shenzhen six-digit code")
    return code


def _parse_as_of(value: date | str) -> date:
    if isinstance(value, datetime):
        raise TypeError("as_of must be a date without a time component")
    if isinstance(value, date):
        parsed = value
    else:
        if not isinstance(value, str) or not _CANONICAL_DATE.fullmatch(value):
            raise ValueError("as_of must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("as_of must be a valid calendar date") from exc
    if parsed > date.today():
        raise ValueError("as_of cannot be in the future")
    return parsed


def _latest_completed_annual_year(as_of: date) -> int:
    return as_of.year - 1 if (as_of.month, as_of.day) >= (5, 1) else as_of.year - 2


def _market_code(code: str) -> str:
    return f"{'SH' if code.startswith('6') else 'SZ'}{code}"


def _secucode(code: str) -> str:
    return f"{code}.{'SH' if code.startswith('6') else 'SZ'}"


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GrowthEvidenceError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                GrowthEvidenceError(f"business response contains non-finite JSON: {value}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise GrowthEvidenceError("business response is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise GrowthEvidenceError("business response is not valid JSON") from exc


def _read_bounded_response(response: Any) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > MAX_RESPONSE_BYTES:
            raise GrowthEvidenceError("business response exceeds the declared byte limit")
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise GrowthEvidenceError("business response is not JSON")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise GrowthEvidenceError("business response does not support bounded streaming")
    chunks: list[bytes] = []
    received = 0
    for chunk in iterator(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise GrowthEvidenceError("business response yielded non-byte content")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise GrowthEvidenceError("business response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_final_url(value: Any) -> None:
    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise GrowthEvidenceError("business source redirected to an invalid URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "emweb.securities.eastmoney.com"
        or parsed.path != "/PC_HSF10/BusinessAnalysis/PageAjax"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise GrowthEvidenceError("business source redirected outside the pinned HTTPS endpoint")


def _finite_decimal(value: Any, *, field: str, nullable: bool = False) -> Decimal | None:
    if value is None or value == "":
        if nullable:
            return None
        raise GrowthEvidenceError(f"{field} is missing")
    if isinstance(value, bool):
        raise GrowthEvidenceError(f"{field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GrowthEvidenceError(f"{field} is not numeric") from exc
    if not parsed.is_finite():
        raise GrowthEvidenceError(f"{field} is not finite")
    return parsed


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise GrowthEvidenceError(f"{field} is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        parsed = int(value.strip())
    else:
        raise GrowthEvidenceError(f"{field} is invalid")
    if not 1 <= parsed <= MAX_SEGMENT_ROWS:
        raise GrowthEvidenceError(f"{field} is outside the accepted range")
    return parsed


def _normalise_text(value: Any, *, field: str, limit: int = 200) -> str:
    if not isinstance(value, str):
        raise GrowthEvidenceError(f"{field} is not text")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > limit
        or "\ufffd" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise GrowthEvidenceError(f"{field} contains invalid text")
    return normalized


def _parse_report_date(value: Any) -> date:
    if not isinstance(value, str):
        raise GrowthEvidenceError("segment report date is not text")
    match = _REPORT_DATE.fullmatch(value.strip())
    if match is None:
        raise GrowthEvidenceError("segment report date format is invalid")
    try:
        parsed = date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise GrowthEvidenceError("segment report date is invalid") from exc
    if parsed > date.today():
        raise GrowthEvidenceError("segment source contains a future report date")
    return parsed


def _normalise_segment_row(row: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(row, Mapping) or set(row) != _SEGMENT_ROW_FIELDS:
        raise GrowthEvidenceError("segment row has an unexpected schema")
    if row.get("SECURITY_CODE") != code or row.get("SECUCODE") != _secucode(code):
        raise GrowthEvidenceError("segment row security-code identity mismatch")
    report_date = _parse_report_date(row.get("REPORT_DATE"))
    mainop_type = _positive_int(row.get("MAINOP_TYPE"), field="MAINOP_TYPE")
    if mainop_type not in _DIMENSIONS:
        raise GrowthEvidenceError("segment MAINOP_TYPE is unsupported")
    item_name = _normalise_text(row.get("ITEM_NAME"), field="ITEM_NAME")
    rank = _positive_int(row.get("RANK"), field="RANK")
    revenue = _finite_decimal(row.get("MAIN_BUSINESS_INCOME"), field="MAIN_BUSINESS_INCOME", nullable=True)
    # Negative segment revenue is real accounting data, not corruption:
    # loss-making branches ("境外"), inter-region eliminations
    # ("地区间抵销"), returns and restructuring offsets all appear as
    # negative MAIN_BUSINESS_INCOME on Eastmoney's segment page.  Rejecting
    # the whole company on any negative row discarded healthy main
    # segments (万科, 平安银行, 浦发银行, ...).  Source-integrity is still
    # enforced downstream: the annual total must stay positive and the
    # reported shares must reconcile within 0.95..1.05, so a garbage
    # all-negative payload cannot pass as evidence.
    reported_share = _finite_decimal(row.get("MBI_RATIO"), field="MBI_RATIO", nullable=True)
    # Negative shares accompany negative segment revenue (see above) and are
    # equally legitimate.  A share above 1.05 remains a source-integrity
    # failure; the downstream annual reconciliation (0.95..1.05 sum) still
    # bounds the whole year.
    if reported_share is not None and reported_share > Decimal("1.05"):
        raise GrowthEvidenceError("segment reported revenue share exceeds 1.05")
    for field in (
        "MAIN_BUSINESS_COST",
        "MBC_RATIO",
        "MAIN_BUSINESS_RPOFIT",
        "MBR_RATIO",
        "GROSS_RPOFIT_RATIO",
    ):
        _finite_decimal(row.get(field), field=field, nullable=True)
    return {
        "security_code": code,
        "secucode": _secucode(code),
        "report_date": report_date.isoformat(),
        "mainop_type": mainop_type,
        "dimension": _DIMENSIONS[mainop_type],
        "item_name": item_name,
        "revenue": float(revenue) if revenue is not None else None,
        "reported_share": float(reported_share) if reported_share is not None else None,
        "rank": rank,
    }


def _validate_business_payload(payload: Any, *, code: str, as_of: date) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != _TOP_LEVEL_FIELDS:
        raise GrowthEvidenceError("business payload has an unexpected top-level schema")
    if not isinstance(payload.get("zyfw"), list) or not isinstance(payload.get("jyps"), list):
        raise GrowthEvidenceError("business payload metadata collections are invalid")
    rows = payload.get("zygcfx")
    if not isinstance(rows, list) or len(rows) > MAX_SEGMENT_ROWS:
        raise GrowthEvidenceError("business payload segment collection is invalid or oversized")
    normalized = [_normalise_segment_row(row, code=code) for row in rows]
    identities: set[tuple[str, int, str]] = set()
    rank_identities: set[tuple[str, int, int]] = set()
    for row in normalized:
        identity = (row["report_date"], row["mainop_type"], row["item_name"].casefold())
        rank_identity = (row["report_date"], row["mainop_type"], row["rank"])
        if identity in identities or rank_identity in rank_identities:
            raise GrowthEvidenceError("business payload contains duplicate segment identities")
        identities.add(identity)
        rank_identities.add(rank_identity)
    latest_year = _latest_completed_annual_year(as_of)
    return [
        row
        for row in normalized
        if row["report_date"].endswith("-12-31") and int(row["report_date"][:4]) <= latest_year
    ]


def _request_segment_rows(
    code: str,
    as_of: date,
    *,
    session: Any,
    timeout: tuple[int, int],
    rate_limiter: Any,
) -> list[dict[str, Any]]:
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        try:
            rate_limiter.acquire()
            response = session.get(
                EASTMONEY_BUSINESS_ENDPOINT,
                params={"code": _market_code(code)},
                headers=_HEADERS,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            _validate_final_url(getattr(response, "url", EASTMONEY_BUSINESS_ENDPOINT))
            return _validate_business_payload(_decode_json(_read_bounded_response(response)), code=code, as_of=as_of)
        except GrowthEvidenceError:
            raise
        except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
            last_error = exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise GrowthEvidenceError(f"Eastmoney business request failed: {_error_label(last_error or RuntimeError())}")


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_cached_segment_records(value: Any, *, code: str, as_of: date) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_SEGMENT_ROWS:
        raise GrowthEvidenceError("segment cache record collection is invalid")
    latest_year = _latest_completed_annual_year(as_of)
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, int, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _NORMALIZED_SEGMENT_FIELDS:
            raise GrowthEvidenceError("segment cache record schema is invalid")
        if item.get("security_code") != code or item.get("secucode") != _secucode(code):
            raise GrowthEvidenceError("segment cache record identity mismatch")
        report_date = _parse_report_date(item.get("report_date"))
        if not item.get("report_date", "").endswith("-12-31") or report_date.year > latest_year:
            raise GrowthEvidenceError("segment cache contains an out-of-window report date")
        mainop_type = _positive_int(item.get("mainop_type"), field="mainop_type")
        if item.get("dimension") != _DIMENSIONS.get(mainop_type):
            raise GrowthEvidenceError("segment cache dimension mismatch")
        item_name = _normalise_text(item.get("item_name"), field="item_name")
        revenue = _finite_decimal(item.get("revenue"), field="revenue", nullable=True)
        reported_share = _finite_decimal(item.get("reported_share"), field="reported_share", nullable=True)
        # Negative revenue/share cache records mirror the live-source rule
        # (loss-making branches, inter-region eliminations): they are valid
        # accounting rows, and the downstream annual reconciliation bounds
        # the whole year instead of rejecting the company.
        if reported_share is not None and reported_share > Decimal("1.05"):
            raise GrowthEvidenceError("segment cache share exceeds 1.05")
        rank = _positive_int(item.get("rank"), field="rank")
        identity = (report_date.isoformat(), mainop_type, item_name.casefold())
        if identity in identities:
            raise GrowthEvidenceError("segment cache contains duplicate identities")
        identities.add(identity)
        normalized.append(
            {
                "security_code": code,
                "secucode": _secucode(code),
                "report_date": report_date.isoformat(),
                "mainop_type": mainop_type,
                "dimension": _DIMENSIONS[mainop_type],
                "item_name": item_name,
                "revenue": float(revenue) if revenue is not None else None,
                "reported_share": float(reported_share) if reported_share is not None else None,
                "rank": rank,
            }
        )
    return normalized


def _dimension_history(
    records: list[dict[str, Any]],
    *,
    mainop_type: int,
    latest_year: int,
) -> tuple[list[int], list[dict[str, Any]], str]:
    relevant = [record for record in records if record["mainop_type"] == mainop_type]
    by_year: dict[int, list[dict[str, Any]]] = {}
    for record in relevant:
        by_year.setdefault(int(record["report_date"][:4]), []).append(record)
    history_years: list[int] = []
    for year in range(latest_year, latest_year - MAX_SEGMENT_HISTORY_YEARS, -1):
        year_rows = by_year.get(year)
        if not year_rows:
            break
        if any(row["revenue"] is None or row["reported_share"] is None for row in year_rows):
            break
        total_revenue = sum(float(row["revenue"]) for row in year_rows)
        reported_total = sum(float(row["reported_share"]) for row in year_rows)
        if total_revenue <= 0 or not 0.95 <= reported_total <= 1.05:
            break
        for row in year_rows:
            calculated = float(row["revenue"]) / total_revenue
            if abs(calculated - float(row["reported_share"])) > 0.03:
                return [], [], "reported_segment_share_does_not_reconcile"
        history_years.append(year)
    history_years.reverse()
    selected_years = set(history_years)
    selected = sorted(
        [record for record in relevant if int(record["report_date"][:4]) in selected_years],
        key=lambda item: (item["report_date"], item["rank"], item["item_name"]),
    )
    if len(history_years) < MIN_SEGMENT_HISTORY_YEARS:
        return history_years, selected, "fewer_than_three_consecutive_completed_annual_reports"
    return history_years, selected, ""


def _cagr(first: float, last: float, periods: int) -> float | None:
    if periods <= 0 or first <= 0 or last < 0:
        return None
    if last == 0:
        return -1.0
    value = (last / first) ** (1.0 / periods) - 1.0
    return value if math.isfinite(value) else None


def _build_segment_growth_sources(
    code: str,
    as_of: date,
    records: list[dict[str, Any]],
    annual_revenue: dict[int, float] | None = None,
    source_row_count: int | None = None,
) -> dict[str, Any]:
    if source_row_count is None:
        source_row_count = len(records)
    latest_year = _latest_completed_annual_year(as_of)
    selected_type = _DIMENSION_PREFERENCE[0]
    selected_years: list[int] = []
    selected_records: list[dict[str, Any]] = []
    annual_latest: float | None = None
    reason = "segment_source_returned_no_completed_annual_rows"
    best_count = -1
    for mainop_type in _DIMENSION_PREFERENCE:
        years, dimension_records, dimension_reason = _dimension_history(
            records,
            mainop_type=mainop_type,
            latest_year=latest_year,
        )
        if len(years) > best_count:
            selected_type = mainop_type
            selected_years = years
            selected_records = dimension_records
            reason = dimension_reason
            best_count = len(years)
        if len(years) >= MIN_SEGMENT_HISTORY_YEARS:
            selected_type = mainop_type
            selected_years = years
            selected_records = dimension_records
            reason = ""
            break

    segments: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {
        "growth_source_count": 0,
        "effective_growth_source_count": 0.0,
        "positive_growth_share": None,
        "revenue_hhi": None,
        "top_segment_share": None,
        "matched_latest_share": None,
        "aggregate_revenue_cagr": None,
    }
    if selected_years:
        first_year = selected_years[0]
        latest = selected_years[-1]
        annual_latest = (annual_revenue or {}).get(latest) if annual_revenue is not None else None
        latest_rows = [record for record in selected_records if int(record["report_date"][:4]) == latest]
        first_rows = {
            record["item_name"].casefold(): record
            for record in selected_records
            if int(record["report_date"][:4]) == first_year
        }
        latest_total = sum(float(record["revenue"]) for record in latest_rows)
        first_total = sum(float(record["revenue"]) for record in first_rows.values())
        for latest_row in latest_rows:
            identity = latest_row["item_name"].casefold()
            first_row = first_rows.get(identity)
            latest_revenue = float(latest_row["revenue"])
            latest_share = latest_revenue / latest_total
            first_revenue = float(first_row["revenue"]) if first_row is not None else None
            segment_cagr = (
                _cagr(first_revenue, latest_revenue, latest - first_year) if first_revenue is not None else None
            )
            segments.append(
                {
                    "item_name": latest_row["item_name"],
                    "first_year": first_year if first_row is not None else None,
                    "latest_year": latest,
                    "first_revenue": first_revenue,
                    "latest_revenue": latest_revenue,
                    "latest_revenue_share": latest_share,
                    "cagr": segment_cagr,
                    "match_status": (
                        "matched"
                        if first_row is not None and first_revenue > 0
                        else "zero_base"
                        if first_row is not None
                        else "new_or_renamed"
                    ),
                }
            )
        growing = [segment for segment in segments if segment["cagr"] is not None and float(segment["cagr"]) > 0]
        growth_contributions = [
            max(float(segment["latest_revenue"]) - float(segment["first_revenue"]), 0.0)
            for segment in segments
            if segment["first_revenue"] is not None
            and float(segment["first_revenue"]) > 0
            and float(segment["latest_revenue"]) > float(segment["first_revenue"])
        ]
        total_growth_contribution = sum(growth_contributions)
        effective_growth_source_count = (
            1.0 / sum((contribution / total_growth_contribution) ** 2 for contribution in growth_contributions)
            if total_growth_contribution > 0
            else 0.0
        )
        # Coverage of the latest annual report by the reported segments.
        # Matching against the filed annual revenue (instead of "first-year
        # segment names") avoids penalising legitimate renames/reorganisations:
        # a renamed segment still covers the same revenue.  The first-year
        # name match below remains only for per-segment CAGR attribution.
        if annual_latest is not None and annual_latest > 0:
            matched_latest_share = latest_total / annual_latest
        else:
            matched_latest_share = sum(
                float(segment["latest_revenue_share"])
                for segment in segments
                if segment["first_year"] is not None and float(segment["latest_revenue"]) > 0
            )
        metrics = {
            "growth_source_count": len(growing),
            "effective_growth_source_count": effective_growth_source_count,
            "positive_growth_share": sum(float(segment["latest_revenue_share"]) for segment in growing),
            "revenue_hhi": sum(float(segment["latest_revenue_share"]) ** 2 for segment in segments),
            "top_segment_share": max(
                (float(segment["latest_revenue_share"]) for segment in segments),
                default=None,
            ),
            "matched_latest_share": matched_latest_share,
            "aggregate_revenue_cagr": _cagr(
                first_total,
                latest_total,
                latest - first_year,
            ),
        }

    if source_row_count == 0:
        status = "unavailable"
    elif len(selected_years) < MIN_SEGMENT_HISTORY_YEARS:
        status = "partial"
    elif float(metrics["matched_latest_share"] or 0.0) < 0.95:
        status = "partial"
        reason = "latest_segment_identity_match_below_95_percent"
    else:
        status = "complete"
    evidence_payload = {
        "model_id": SEGMENT_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "dimension": _DIMENSIONS[selected_type],
        "history_years": selected_years,
        "metrics": metrics,
        "annual_revenue_latest": annual_latest,
        "source_row_count": source_row_count,
        "segments": segments,
        "records": selected_records,
    }
    return {
        "status": status,
        "source": "东方财富主营构成年度数据",
        "source_url": EASTMONEY_BUSINESS_ENDPOINT,
        "evidence_id": f"eastmoney-segments:{code}:{_canonical_hash(evidence_payload)}",
        "as_of": as_of.isoformat(),
        "security_code": code,
        "model_id": SEGMENT_MODEL_ID,
        "contract_scope": "annual_segment_revenue_history",
        "dimension": _DIMENSIONS[selected_type],
        "history_years": selected_years,
        "growth_source_count": metrics["growth_source_count"],
        "effective_growth_source_count": metrics["effective_growth_source_count"],
        "positive_growth_share": metrics["positive_growth_share"],
        "revenue_hhi": metrics["revenue_hhi"],
        "top_segment_share": metrics["top_segment_share"],
        "matched_latest_share": metrics["matched_latest_share"],
        "annual_revenue_latest": annual_latest,
        "source_row_count": source_row_count,
        "aggregate_revenue_cagr": metrics["aggregate_revenue_cagr"],
        "segments": segments,
        "records": selected_records,
        "reason": reason if status != "complete" else "",
    }


def _segment_cache_contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "model_id": SEGMENT_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "source": EASTMONEY_BUSINESS_ENDPOINT,
        "max_history_years": MAX_SEGMENT_HISTORY_YEARS,
        "min_history_years": MIN_SEGMENT_HISTORY_YEARS,
        "dimension_preference": list(_DIMENSION_PREFERENCE),
    }


def _segment_cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{SEGMENT_MODEL_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _segment_cache_index(cache_dir: Path) -> dict[str, list[tuple[date, Path]]]:
    """Index only canonical segment-cache filenames in one directory pass."""

    pattern = re.compile(rf"^{re.escape(SEGMENT_MODEL_ID)}_(?P<code>[036][0-9]{{5}})_(?P<as_of>[0-9]{{8}})\.json\.gz$")
    indexed: dict[str, list[tuple[date, Path]]] = {}
    try:
        paths = list(cache_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return indexed
    for path in paths:
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            source_as_of = datetime.strptime(match.group("as_of"), "%Y%m%d").date()
        except ValueError:
            continue
        indexed.setdefault(match.group("code"), []).append((source_as_of, path))
    for values in indexed.values():
        values.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return indexed


def _load_reusable_segment_cache(
    code: str,
    as_of: date,
    *,
    cache_dir: Path,
    cache_index: Mapping[str, Sequence[tuple[date, Path]]] | None = None,
    annual_revenue: dict[int, float] | None = None,
) -> dict[str, Any] | None:
    """Revalidate a source capture no more than 21 days old for ``as_of``.

    The cache filename and embedded contract bind the original capture to its
    historical ``as_of``.  Reuse never rewrites that file under today's date,
    so repeated rebasing cannot extend the source's 21-day freshness window.
    Both the original and current cutoffs are validated before the annual raw
    rows are rebuilt into current-date evidence.
    """

    indexed = _segment_cache_index(cache_dir) if cache_index is None else cache_index
    for source_as_of, path in indexed.get(code, ()):
        age_days = (as_of - source_as_of).days
        if (
            age_days < 0
            or age_days > SEGMENT_CACHE_REUSE_DAYS
            or _latest_completed_annual_year(source_as_of) != _latest_completed_annual_year(as_of)
        ):
            continue
        cache = SafeFileCache(
            path,
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CACHE_TTL_SECONDS,
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        loaded = cache.load(allow_expired=True)
        if not loaded.hit:
            continue
        try:
            payload = loaded.value
            if not isinstance(payload, Mapping) or set(payload) != {"contract", "records"}:
                raise GrowthEvidenceError("segment cache payload shape is invalid")
            if payload.get("contract") != _segment_cache_contract(code, source_as_of):
                raise GrowthEvidenceError("segment cache contract mismatch")
            source_records = _validate_cached_segment_records(
                payload.get("records"),
                code=code,
                as_of=source_as_of,
            )
            current_records = _validate_cached_segment_records(
                source_records,
                code=code,
                as_of=as_of,
            )
            evidence = _build_segment_growth_sources(code, as_of, current_records, annual_revenue=annual_revenue)
            _validate_segment_evidence(evidence, code=code, as_of=as_of)
            if evidence.get("status") != "complete":
                continue
        except GrowthEvidenceError:
            continue
        return {
            "segment_growth_sources": evidence,
            "source_as_of": source_as_of.isoformat(),
            "cache_diagnostic": ("hit" if source_as_of == as_of else f"reused_source_as_of:{source_as_of.isoformat()}"),
        }
    return None


def load_growth_evidence_cache_batch_state(
    requests_: Sequence[Mapping[str, Any]],
    *,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
) -> dict[str, dict[str, Any]]:
    """Return safely reusable segment evidence for a deterministic request set."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("growth-evidence cache requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("growth-evidence cache batch exceeds the company limit")
    prepared: list[tuple[str, date]] = []
    seen: set[str] = set()
    request_annual_revenue: dict[str, dict[int, float]] = {}
    for request in requests_:
        if not isinstance(request, Mapping) or "code" not in request or "as_of" not in request:
            raise ValueError("growth-evidence cache request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_as_of(request.get("as_of"))
        if code in seen:
            raise ValueError(f"growth-evidence cache batch contains duplicate code: {code}")
        seen.add(code)
        raw_revenue = request.get("revenue_records")
        if isinstance(raw_revenue, Sequence) and not isinstance(raw_revenue, (str, bytes)):
            try:
                request_annual_revenue[code] = _prepare_financial_records(
                    raw_revenue,
                    label="revenue_records",
                    nonnegative=True,
                    as_of=cutoff,
                )
            except (TypeError, ValueError, GrowthEvidenceError):
                request_annual_revenue[code] = {}
        prepared.append((code, cutoff))
    indexed = _segment_cache_index(Path(cache_dir))
    result: dict[str, dict[str, Any]] = {}
    for code, cutoff in sorted(prepared):
        state = _load_reusable_segment_cache(
            code,
            cutoff,
            cache_dir=Path(cache_dir),
            cache_index=indexed,
            annual_revenue=request_annual_revenue.get(code),
        )
        if state is not None:
            result[code] = state
    return result


def _external_financial_input_hash(
    revenue_records: Sequence[Mapping[str, Any]],
    goodwill_records: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
) -> str | None:
    """Bind reusable cash-flow evidence to the exact local annual inputs.

    The external proxy combines a remotely fetched cash-flow row with local
    revenue and goodwill histories.  Reusing only the remote row would make a
    later correction to either local annual series invisible.  Cache only a
    complete five-year input window and hash its normalized values instead.
    """

    revenues = _prepare_financial_records(
        revenue_records,
        label="revenue_records",
        nonnegative=True,
        as_of=as_of,
    )
    goodwill = _prepare_financial_records(
        goodwill_records,
        label="goodwill_records",
        nonnegative=True,
        as_of=as_of,
    )
    latest_year = _latest_completed_annual_year(as_of)
    years = list(range(latest_year - EXTERNAL_HISTORY_YEARS + 1, latest_year + 1))
    if any(year not in revenues or year not in goodwill for year in years):
        return None
    return _canonical_hash(
        {
            "model_id": EXTERNAL_MODEL_ID,
            "latest_completed_annual_year": latest_year,
            "financial_inputs": [
                {
                    "year": year,
                    "revenue": revenues[year],
                    "goodwill": goodwill[year],
                }
                for year in years
            ],
        }
    )


def _external_cache_contract(
    code: str,
    source_as_of: date,
    *,
    financial_inputs_sha256: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", financial_inputs_sha256) is None:
        raise GrowthEvidenceError("external cache financial-input hash is invalid")
    return {
        "model_id": EXTERNAL_CACHE_MODEL_ID,
        "external_model_id": EXTERNAL_MODEL_ID,
        "code": code,
        "source_as_of": source_as_of.isoformat(),
        "latest_completed_annual_year": _latest_completed_annual_year(source_as_of),
        "financial_inputs_sha256": financial_inputs_sha256,
        "source_url": EASTMONEY_DATACENTER_URL,
    }


def _external_cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{EXTERNAL_CACHE_MODEL_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _external_cache_index(cache_dir: Path) -> dict[str, list[tuple[date, Path]]]:
    """Index canonical external-evidence captures in one directory pass."""

    pattern = re.compile(
        rf"^{re.escape(EXTERNAL_CACHE_MODEL_ID)}_(?P<code>[036][0-9]{{5}})_(?P<as_of>[0-9]{{8}})\.json\.gz$"
    )
    indexed: dict[str, list[tuple[date, Path]]] = {}
    try:
        paths = list(cache_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return indexed
    for path in paths:
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            source_as_of = datetime.strptime(match.group("as_of"), "%Y%m%d").date()
        except ValueError:
            continue
        indexed.setdefault(match.group("code"), []).append((source_as_of, path))
    for values in indexed.values():
        values.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return indexed


def _rebase_external_growth_evidence(
    evidence: Any,
    *,
    code: str,
    source_as_of: date,
    as_of: date,
) -> dict[str, Any]:
    """Reissue a validated source capture under the current evidence cutoff."""

    source = _validate_external_evidence(evidence, code=code, as_of=source_as_of)
    if source["status"] != "complete":
        raise GrowthEvidenceError("external cache cannot reuse incomplete evidence")
    if source_as_of == as_of:
        return source
    rebased = dict(source)
    rebased["as_of"] = as_of.isoformat()
    identity_payload = {
        "model_id": EXTERNAL_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "coverage_years": source["coverage_years"],
        "records": source["records"],
    }
    rebased["evidence_id"] = f"eastmoney-external-growth:{code}:{_canonical_hash(identity_payload)}"
    return _validate_external_evidence(rebased, code=code, as_of=as_of)


def _load_reusable_external_cache(
    code: str,
    as_of: date,
    *,
    revenue_records: Sequence[Mapping[str, Any]],
    goodwill_records: Sequence[Mapping[str, Any]],
    cache_dir: Path,
    cache_index: Mapping[str, Sequence[tuple[date, Path]]] | None = None,
) -> dict[str, Any] | None:
    """Load a recent, input-bound complete external-growth source capture."""

    financial_inputs_sha256 = _external_financial_input_hash(
        revenue_records,
        goodwill_records,
        as_of=as_of,
    )
    if financial_inputs_sha256 is None:
        return None
    indexed = _external_cache_index(cache_dir) if cache_index is None else cache_index
    for source_as_of, path in indexed.get(code, ()):
        age_days = (as_of - source_as_of).days
        if (
            age_days < 0
            or age_days > EXTERNAL_CACHE_REUSE_DAYS
            or _latest_completed_annual_year(source_as_of) != _latest_completed_annual_year(as_of)
        ):
            continue
        cache = SafeFileCache(
            path,
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CACHE_TTL_SECONDS,
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        loaded = cache.load(allow_expired=True)
        if not loaded.hit:
            continue
        try:
            payload = loaded.value
            if not isinstance(payload, Mapping) or set(payload) != {"contract", "external_growth_evidence"}:
                raise GrowthEvidenceError("external cache payload shape is invalid")
            expected_contract = _external_cache_contract(
                code,
                source_as_of,
                financial_inputs_sha256=financial_inputs_sha256,
            )
            if payload.get("contract") != expected_contract:
                raise GrowthEvidenceError("external cache contract mismatch")
            evidence = _rebase_external_growth_evidence(
                payload.get("external_growth_evidence"),
                code=code,
                source_as_of=source_as_of,
                as_of=as_of,
            )
        except (GrowthEvidenceError, TypeError, ValueError):
            continue
        return {
            "external_growth_evidence": evidence,
            "source_as_of": source_as_of.isoformat(),
            "cache_diagnostic": ("hit" if source_as_of == as_of else f"reused_source_as_of:{source_as_of.isoformat()}"),
        }
    return None


def load_external_growth_evidence_cache_batch_state(
    requests_: Sequence[Mapping[str, Any]],
    *,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
) -> dict[str, dict[str, Any]]:
    """Return reusable complete external evidence for exact batch inputs.

    Missing goodwill is intentionally not inferred as zero here.  Such rows
    remain eligible for a later official-source refresh but cannot reuse a
    complete proxy that was bound to different financial inputs.
    """

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("external growth-evidence cache requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("external growth-evidence cache batch exceeds the company limit")
    prepared: list[tuple[str, date, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {
            "code",
            "as_of",
            "revenue_records",
            "goodwill_records",
        }:
            raise ValueError("external growth-evidence cache request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_as_of(request.get("as_of"))
        if code in seen:
            raise ValueError(f"external growth-evidence cache batch contains duplicate code: {code}")
        revenue_records = request.get("revenue_records")
        goodwill_records = request.get("goodwill_records")
        _prepare_financial_records(
            revenue_records,
            label="revenue_records",
            nonnegative=True,
            as_of=cutoff,
        )
        _prepare_financial_records(
            goodwill_records,
            label="goodwill_records",
            nonnegative=True,
            as_of=cutoff,
        )
        seen.add(code)
        prepared.append((code, cutoff, revenue_records, goodwill_records))
    indexed = _external_cache_index(Path(cache_dir))
    result: dict[str, dict[str, Any]] = {}
    for code, cutoff, revenue_records, goodwill_records in sorted(prepared):
        state = _load_reusable_external_cache(
            code,
            cutoff,
            revenue_records=revenue_records,
            goodwill_records=goodwill_records,
            cache_dir=Path(cache_dir),
            cache_index=indexed,
        )
        if state is not None:
            result[code] = state
    return result


def _save_external_growth_evidence_cache(
    code: str,
    as_of: date,
    *,
    revenue_records: Sequence[Mapping[str, Any]],
    goodwill_records: Sequence[Mapping[str, Any]],
    evidence: Any,
    cache_dir: Path,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
) -> bool:
    """Best-effort persistence of a complete, validated external capture."""

    try:
        financial_inputs_sha256 = _external_financial_input_hash(
            revenue_records,
            goodwill_records,
            as_of=as_of,
        )
        if financial_inputs_sha256 is None:
            return False
        normalized = _validate_external_evidence(evidence, code=code, as_of=as_of)
        if normalized["status"] != "complete":
            return False
        payload = {
            "contract": _external_cache_contract(
                code,
                as_of,
                financial_inputs_sha256=financial_inputs_sha256,
            ),
            "external_growth_evidence": normalized,
        }
        cache = SafeFileCache(
            _external_cache_path(code, as_of, cache_dir),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=cache_ttl_seconds,
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        cache.save(payload)
    except (GrowthEvidenceError, OSError, SafeCacheError, TypeError, ValueError):
        return False
    return True


def _prepare_growth_evidence_retry_requests(
    requests_: Sequence[Mapping[str, Any]],
) -> list[tuple[str, date]]:
    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("growth-evidence retry requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("growth-evidence retry batch exceeds the company limit")
    prepared: list[tuple[str, date]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or "code" not in request or "as_of" not in request:
            raise ValueError("growth-evidence retry request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_as_of(request.get("as_of"))
        if code in seen:
            raise ValueError(f"growth-evidence retry batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, cutoff))
    return prepared


def _type3_growth_retry_state_path(code: str, cache_dir: Path) -> Path:
    return cache_dir / f"{TYPE3_GROWTH_RETRY_MODEL_ID}_{code}.json.gz"


def _type3_growth_retry_state_index(cache_dir: Path) -> dict[str, Path]:
    """Index canonical retry-state files without traversing outside the cache."""

    pattern = re.compile(rf"^{re.escape(TYPE3_GROWTH_RETRY_MODEL_ID)}_(?P<code>[036][0-9]{{5}})\.json\.gz$")
    indexed: dict[str, Path] = {}
    try:
        paths = list(cache_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return indexed
    for path in paths:
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        try:
            if path.is_file():
                indexed[match.group("code")] = path
        except OSError:
            continue
    return indexed


def _parse_retry_calendar_date(value: Any, *, field: str) -> date:
    """Parse an audit date that may legitimately be in the future."""

    if not isinstance(value, str) or not _CANONICAL_DATE.fullmatch(value):
        raise GrowthEvidenceError(f"growth-evidence retry {field} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GrowthEvidenceError(f"growth-evidence retry {field} is invalid") from exc


def _type3_growth_retry_days(retry_class: str) -> int:
    if retry_class == "transient":
        return TYPE3_GROWTH_TRANSIENT_RETRY_DAYS
    if retry_class == "structural":
        return TYPE3_GROWTH_STRUCTURAL_RETRY_DAYS
    raise GrowthEvidenceError("growth-evidence retry class is invalid")


def _normalise_type3_growth_retry_reason(value: Any) -> str:
    if not isinstance(value, str):
        return "invalid_or_missing_growth_evidence_result"
    printable = "".join(" " if ord(character) < 32 or ord(character) == 127 else character for character in value)
    compact = " ".join(printable.split())
    if not compact:
        return "incomplete_growth_evidence_result"
    return compact[:500]


def _build_type3_growth_retry_state(
    code: str,
    attempt_as_of: date,
    *,
    retry_class: str,
    reason: Any,
) -> dict[str, Any]:
    delay_days = _type3_growth_retry_days(retry_class)
    return {
        "model_id": TYPE3_GROWTH_RETRY_MODEL_ID,
        "code": code,
        "last_attempt_as_of": attempt_as_of.isoformat(),
        "retry_class": retry_class,
        "retry_after": (attempt_as_of + timedelta(days=delay_days)).isoformat(),
        "reason": _normalise_type3_growth_retry_reason(reason),
    }


def _validate_type3_growth_retry_state(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _TYPE3_GROWTH_RETRY_STATE_FIELDS:
        raise GrowthEvidenceError("growth-evidence retry state shape is invalid")
    if value.get("model_id") != TYPE3_GROWTH_RETRY_MODEL_ID or value.get("code") != code:
        raise GrowthEvidenceError("growth-evidence retry state identity is invalid")
    retry_class = value.get("retry_class")
    if retry_class not in _TYPE3_GROWTH_RETRY_CLASSES:
        raise GrowthEvidenceError("growth-evidence retry class is invalid")
    attempt_as_of = _parse_retry_calendar_date(value.get("last_attempt_as_of"), field="last_attempt_as_of")
    retry_after = _parse_retry_calendar_date(value.get("retry_after"), field="retry_after")
    if retry_after != attempt_as_of + timedelta(days=_type3_growth_retry_days(retry_class)):
        raise GrowthEvidenceError("growth-evidence retry backoff is invalid")
    reason = value.get("reason")
    if (
        not isinstance(reason, str)
        or not reason
        or len(reason) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in reason)
    ):
        raise GrowthEvidenceError("growth-evidence retry reason is invalid")
    return {
        "model_id": TYPE3_GROWTH_RETRY_MODEL_ID,
        "code": code,
        "last_attempt_as_of": attempt_as_of.isoformat(),
        "retry_class": retry_class,
        "retry_after": retry_after.isoformat(),
        "reason": reason,
    }


def _type3_growth_retry_state_for_result(
    code: str,
    attempt_as_of: date,
    result: Any,
) -> dict[str, Any] | None:
    """Convert a completed network attempt into scheduling-only retry metadata."""

    if not isinstance(result, Mapping):
        return _build_type3_growth_retry_state(
            code,
            attempt_as_of,
            retry_class="transient",
            reason="invalid_or_missing_growth_evidence_result",
        )
    if (
        result.get("code") != code
        or result.get("as_of") != attempt_as_of.isoformat()
        or result.get("model_id") != MODEL_ID
        or not isinstance(result.get("available"), bool)
    ):
        return _build_type3_growth_retry_state(
            code,
            attempt_as_of,
            retry_class="transient",
            reason="invalid_or_missing_growth_evidence_result",
        )
    if result["available"]:
        return None
    reason = _normalise_type3_growth_retry_reason(result.get("reason"))
    retry_class = (
        "transient" if any(marker in reason for marker in _TYPE3_GROWTH_TRANSIENT_REASON_MARKERS) else "structural"
    )
    return _build_type3_growth_retry_state(
        code,
        attempt_as_of,
        retry_class=retry_class,
        reason=reason,
    )


def load_growth_evidence_retry_state_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
) -> dict[str, dict[str, Any]]:
    """Load validated Type 3 retry metadata without treating it as evidence.

    Retry files affect only bounded fetch scheduling.  They never supply a
    score, replace a source capture, or make an incomplete company appear
    complete.
    """

    prepared = _prepare_growth_evidence_retry_requests(requests_)
    directory = Path(cache_dir)
    indexed = _type3_growth_retry_state_index(directory)
    result: dict[str, dict[str, Any]] = {}
    for code, cutoff in sorted(prepared):
        path = indexed.get(code)
        if path is None:
            continue
        cache = SafeFileCache(
            path,
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CACHE_TTL_SECONDS,
            max_uncompressed_bytes=16 * 1024,
        )
        loaded = cache.load(allow_expired=True)
        if not loaded.hit:
            continue
        try:
            state = _validate_type3_growth_retry_state(loaded.value, code=code)
            if date.fromisoformat(state["last_attempt_as_of"]) > cutoff:
                continue
        except (GrowthEvidenceError, TypeError, ValueError):
            continue
        result[code] = state
    return result


def record_growth_evidence_retry_states(
    requests_: Sequence[Mapping[str, Any]],
    results: Mapping[str, Any],
    *,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
) -> dict[str, dict[str, Any]]:
    """Persist bounded-fetch retry metadata for selected network candidates.

    A valid complete result intentionally writes no retry record.  Failed
    transport/source attempts retry after one calendar day; evidence that is
    valid but incomplete retries after seven days.  Cache-write failures are
    best-effort because scheduling metadata must not invalidate the scored
    evidence returned by the pipeline.
    """

    if not isinstance(results, Mapping):
        raise TypeError("growth-evidence retry results must be a mapping")
    prepared = _prepare_growth_evidence_retry_requests(requests_)
    directory = Path(cache_dir)
    recorded: dict[str, dict[str, Any]] = {}
    for code, cutoff in prepared:
        state = _type3_growth_retry_state_for_result(code, cutoff, results.get(code))
        if state is None:
            continue
        cache = SafeFileCache(
            _type3_growth_retry_state_path(code, directory),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CACHE_TTL_SECONDS,
            max_uncompressed_bytes=16 * 1024,
        )
        try:
            cache.save(state)
        # Retry metadata is intentionally optional: directory creation and
        # serialization occur before SafeFileCache can wrap every failure, so
        # ordinary filesystem/value errors must not invalidate a score that
        # was already computed from independently validated evidence.
        except (OSError, SafeCacheError, TypeError, ValueError):
            continue
        recorded[code] = state
    return recorded


def _fetch_segment_growth_sources(
    code: str,
    as_of: date,
    *,
    session: Any = requests,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
    recent_cache_state: Mapping[str, Mapping[str, Any]] | None = None,
    annual_revenue: dict[int, float] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    if (
        not isinstance(timeout, tuple)
        or len(timeout) != 2
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 60
            for value in timeout
        )
    ):
        raise ValueError("timeout must contain positive connect/read seconds no greater than 60")

    if recent_cache_state is not None:
        state = recent_cache_state.get(code)
        if state is not None:
            if not isinstance(state, Mapping) or set(state) != _SEGMENT_CACHE_STATE_FIELDS:
                raise GrowthEvidenceError("preloaded segment cache state is invalid")
            evidence = _validate_segment_evidence(
                state.get("segment_growth_sources"),
                code=code,
                as_of=as_of,
            )
            diagnostic = state.get("cache_diagnostic")
            if not isinstance(diagnostic, str):
                raise GrowthEvidenceError("preloaded segment cache diagnostic is invalid")
            return evidence, True, diagnostic

    cache: SafeFileCache | None = None
    initial = None
    diagnostic = "disabled"
    if use_cache:
        cache = SafeFileCache(
            _segment_cache_path(code, as_of, Path(cache_dir)),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=cache_ttl_seconds,
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        initial = cache.load()
        if initial.hit:
            try:
                payload = initial.value
                if not isinstance(payload, Mapping) or set(payload) != {"contract", "records"}:
                    raise GrowthEvidenceError("segment cache payload shape is invalid")
                if payload.get("contract") != _segment_cache_contract(code, as_of):
                    raise GrowthEvidenceError("segment cache contract mismatch")
                records = _validate_cached_segment_records(payload.get("records"), code=code, as_of=as_of)
                evidence = _build_segment_growth_sources(code, as_of, records, annual_revenue=annual_revenue)
                if recent_cache_state is None or evidence.get("status") == "complete":
                    return evidence, True, "hit"
                diagnostic = "incomplete_hit_requires_refresh"
            except GrowthEvidenceError as exc:
                diagnostic = f"invalid_hit:{_error_label(exc)}"
        else:
            diagnostic = f"miss:{initial.reason}"
        if recent_cache_state is None:
            reusable = _load_reusable_segment_cache(
                code,
                as_of,
                cache_dir=Path(cache_dir),
            )
            if reusable is not None:
                return (
                    dict(reusable["segment_growth_sources"]),
                    True,
                    str(reusable["cache_diagnostic"]),
                )

    active_session = requests.Session() if session is requests else session
    owns_session = active_session is not session
    try:
        records = _request_segment_rows(
            code,
            as_of,
            session=active_session,
            timeout=timeout,
            rate_limiter=rate_limiter,
        )
        evidence = _build_segment_growth_sources(code, as_of, records, annual_revenue=annual_revenue)
    except Exception as exc:
        return (
            {
                "status": "unavailable",
                "source": "东方财富主营构成年度数据",
                "source_url": EASTMONEY_BUSINESS_ENDPOINT,
                "evidence_id": f"eastmoney-segments:{code}:unavailable",
                "as_of": as_of.isoformat(),
                "security_code": code,
                "model_id": SEGMENT_MODEL_ID,
                "contract_scope": "annual_segment_revenue_history",
                "dimension": None,
                "history_years": [],
                "growth_source_count": 0,
                "effective_growth_source_count": 0.0,
                "positive_growth_share": None,
                "revenue_hhi": None,
                "top_segment_share": None,
                "matched_latest_share": None,
                "annual_revenue_latest": None,
                "source_row_count": 0,
                "aggregate_revenue_cagr": None,
                "segments": [],
                "records": [],
                "reason": f"source_unavailable:{_error_label(exc)}",
            },
            False,
            diagnostic,
        )
    finally:
        if owns_session:
            active_session.close()

    if cache is None:
        return evidence, False, diagnostic
    payload = {"contract": _segment_cache_contract(code, as_of), "records": records}
    expected_hash = None
    if initial is not None and isinstance(initial.metadata, Mapping):
        candidate = initial.metadata.get("payload_sha256")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            expected_hash = candidate
    try:
        cache.compare_and_swap(
            payload,
            expected_payload_sha256=expected_hash,
            allow_replace_invalid=True,
        )
        return evidence, False, f"{diagnostic};saved"
    except SafeCacheConflict:
        winner = cache.load()
        if winner.hit:
            try:
                winner_payload = winner.value
                if (
                    isinstance(winner_payload, Mapping)
                    and set(winner_payload) == {"contract", "records"}
                    and winner_payload.get("contract") == _segment_cache_contract(code, as_of)
                ):
                    winner_records = _validate_cached_segment_records(
                        winner_payload.get("records"),
                        code=code,
                        as_of=as_of,
                    )
                    return _build_segment_growth_sources(code, as_of, winner_records), True, "race_winner"
            except GrowthEvidenceError:
                pass
        return evidence, False, f"{diagnostic};write_conflict"
    except SafeCacheError as exc:
        return evidence, False, f"{diagnostic};write_failed:{_error_label(exc)}"


def _prepare_financial_records(
    value: Any,
    *,
    label: str,
    nonnegative: bool,
    as_of: date,
) -> dict[int, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence")
    latest_year = _latest_completed_annual_year(as_of)
    prepared: dict[int, float] = {}
    for record in value:
        if not isinstance(record, Mapping) or set(record) != {"year", "value"}:
            raise ValueError(f"{label} record shape is invalid")
        year = record.get("year")
        if isinstance(year, bool) or not isinstance(year, int) or not 1990 <= year <= latest_year:
            raise ValueError(f"{label} year is invalid or after the completed annual cutoff")
        if year in prepared:
            raise ValueError(f"{label} contains duplicate years")
        parsed = _finite_decimal(record.get("value"), field=f"{label}.value")
        if parsed is None:  # Defensive if the decimal parser contract changes.
            raise GrowthEvidenceError(f"{label}.value is missing")
        if nonnegative and parsed < 0:
            raise ValueError(f"{label} value must be non-negative")
        if label == "revenue_records" and parsed <= 0:
            raise ValueError("revenue_records value must be positive")
        prepared[year] = float(parsed)
    return prepared


def _close_decimal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= _CURRENCY_IDENTITY_TOLERANCE


def _close_ratio_decimal(left: Decimal, right: Decimal) -> bool:
    scale = max(abs(left), abs(right), Decimal(1))
    return abs(left - right) <= scale * Decimal("1e-10")


def _cashflow_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    raw = row.get(field)
    if raw is None or raw == "" or (isinstance(raw, float) and math.isnan(raw)):
        return None
    parsed = _finite_decimal(raw, field=field)
    if parsed is None:  # Defensive if the decimal parser contract changes.
        raise GrowthEvidenceError(f"{field} is missing")
    return parsed


def _resolve_acquisition_cash(row: Mapping[str, Any]) -> tuple[float | None, str, dict[str, Any]]:
    reported = _cashflow_decimal(row, _ACQUISITION_FIELD)
    if reported is not None:
        if reported < 0:
            raise GrowthEvidenceError("reported acquisition cash-flow is negative")
        return float(reported), "source_reported", {"reported_value": float(reported)}

    total = _cashflow_decimal(row, "TOTAL_INVEST_OUTFLOW")
    total_basis = "source_reported_total_invest_outflow"
    net_identity_components: dict[str, float] = {}
    if total is None:
        net_values = {field: _cashflow_decimal(row, field) for field in _NET_OUTFLOW_IDENTITY_FIELDS}
        if any(value is None for value in net_values.values()):
            return None, "unresolved_null", {"reason": "aggregate_investing_cash_identity_is_incomplete"}
        net_identity_components = {field: float(value) for field, value in net_values.items() if value is not None}
        total = (
            net_values["TOTAL_INVEST_INFLOW"]
            + net_values["INVEST_NETCASH_OTHER"]
            + net_values["INVEST_NETCASH_BALANCE"]
            - net_values["NETCASH_INVEST"]
        )
        total_basis = "derived_from_investing_net_cash_identity"
    if total < 0:
        raise GrowthEvidenceError("total investment outflow is negative")

    component_values: dict[str, Decimal | None] = {}
    null_fields: list[str] = []
    for field in _EXTERNAL_COMPONENT_FIELDS:
        value = _cashflow_decimal(row, field)
        component_values[field] = value
        if value is None:
            null_fields.append(field)
            continue
        if value < 0:
            raise GrowthEvidenceError(f"investment outflow component is negative: {field}")
        component_values[field] = value
    known_sum = sum((value for value in component_values.values() if value is not None), Decimal(0))
    residual = total - known_sum
    if residual < 0 and not _close_decimal(residual, Decimal(0)):
        raise GrowthEvidenceError("investment outflow components exceed the aggregate")

    details = {
        "total_invest_outflow": float(total),
        "total_basis": total_basis,
        "known_component_sum": float(known_sum),
        "component_values": {
            field: (float(value) if value is not None else None) for field, value in component_values.items()
        },
        "net_identity_components": net_identity_components,
        "source_null_fields": sorted(null_fields),
        "rounding_tolerance_cny": float(_CURRENCY_IDENTITY_TOLERANCE),
    }
    if _close_decimal(residual, Decimal(0)):
        details["identity_residual"] = 0.0
        return 0.0, "derived_aggregate_identity_zero", details
    if null_fields == [_ACQUISITION_FIELD]:
        details["identity_residual"] = float(residual)
        return float(residual), "derived_aggregate_identity_residual", details
    details["identity_residual"] = float(residual)
    details["reason"] = "positive_residual_is_not_unique_to_acquisition_cash"
    return None, "unresolved_null", details


def _acquisition_records_by_year(
    value: Any,
    *,
    code: str,
    as_of: date,
) -> dict[int, dict[str, Any]]:
    if isinstance(value, pd.DataFrame):
        records = value.to_dict(orient="records")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        records = list(value)
    else:
        raise GrowthEvidenceError("acquisition cash-flow records are invalid")
    latest_year = _latest_completed_annual_year(as_of)
    prepared: dict[int, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise GrowthEvidenceError("acquisition cash-flow row is not an object")
        if str(record.get("SECURITY_CODE") or "").strip() != code:
            raise GrowthEvidenceError("acquisition cash-flow security-code identity mismatch")
        raw_date = str(record.get("REPORT_DATE") or "")[:10]
        if not _CANONICAL_DATE.fullmatch(raw_date) or not raw_date.endswith("-12-31"):
            raise GrowthEvidenceError("acquisition cash-flow report date is invalid")
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise GrowthEvidenceError("acquisition cash-flow report date is invalid") from exc
        if report_date.year > latest_year:
            raise GrowthEvidenceError("acquisition cash-flow row is after the completed annual cutoff")
        notice = record.get("NOTICE_DATE")
        if notice not in (None, "") and not (isinstance(notice, float) and math.isnan(notice)):
            notice_date = str(notice)[:10]
            if not _CANONICAL_DATE.fullmatch(notice_date):
                raise GrowthEvidenceError("acquisition cash-flow notice date is invalid")
            try:
                parsed_notice = date.fromisoformat(notice_date)
            except ValueError as exc:
                raise GrowthEvidenceError("acquisition cash-flow notice date is invalid") from exc
            if parsed_notice > as_of:
                raise GrowthEvidenceError("acquisition cash-flow row was not public by as_of")
        if report_date.year in prepared:
            raise GrowthEvidenceError("acquisition cash-flow contains duplicate annual rows")
        value_resolved, basis, derivation = _resolve_acquisition_cash(record)
        prepared[report_date.year] = {
            "report_date": report_date.isoformat(),
            "value": value_resolved,
            "value_basis": basis,
            "derivation": derivation,
            "source_report": str(record.get("SOURCE_REPORT_NAME") or "RPT_F10_FINANCE_GCASHFLOW"),
            "source_field": _ACQUISITION_FIELD,
            "source_url": str(record.get("SOURCE_REPORT_URL") or EASTMONEY_DATACENTER_URL),
        }
    return prepared


def _unavailable_external_evidence(code: str, as_of: date, reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "source": "东方财富年度现金流、年度商誉与营业收入",
        "source_url": EASTMONEY_DATACENTER_URL,
        "evidence_id": f"eastmoney-external-growth:{code}:unavailable",
        "as_of": as_of.isoformat(),
        "security_code": code,
        "model_id": EXTERNAL_MODEL_ID,
        "contract_scope": "aggregate_proxy_not_transaction_census",
        "coverage_years": [],
        "coverage_year_count": 0,
        "acquisition_cash_values": [],
        "acquisition_cash_to_revenue": [],
        "aggregate_acquisition_cash_to_revenue": None,
        "positive_goodwill_additions_to_revenue": None,
        "goodwill_to_revenue_latest": None,
        "goodwill_change_to_revenue": None,
        "records": [],
        "limitations": ["不提供逐笔并购交易清单", "不提供被收购业务收入占比"],
        "reason": reason,
    }


def build_external_growth_evidence(
    code: str,
    as_of: date | str,
    *,
    revenue_records: Sequence[Mapping[str, Any]],
    goodwill_records: Sequence[Mapping[str, Any]],
    acquisition_cashflow_records: Any,
) -> dict[str, Any]:
    """Build a bounded aggregate external-growth proxy from annual facts."""
    normalized_code = _normalise_code(code)
    cutoff = _parse_as_of(as_of)
    revenues = _prepare_financial_records(
        revenue_records,
        label="revenue_records",
        nonnegative=True,
        as_of=cutoff,
    )
    goodwill = _prepare_financial_records(
        goodwill_records,
        label="goodwill_records",
        nonnegative=True,
        as_of=cutoff,
    )
    acquisitions = _acquisition_records_by_year(
        acquisition_cashflow_records,
        code=normalized_code,
        as_of=cutoff,
    )
    latest_year = _latest_completed_annual_year(cutoff)
    coverage_years: list[int] = []
    for year in range(latest_year, latest_year - EXTERNAL_HISTORY_YEARS, -1):
        acquisition = acquisitions.get(year)
        if year not in revenues or year not in goodwill or acquisition is None or acquisition["value"] is None:
            break
        coverage_years.append(year)
    coverage_years.reverse()

    records: list[dict[str, Any]] = []
    for year in coverage_years:
        acquisition = acquisitions[year]
        revenue = revenues[year]
        records.append(
            {
                "year": year,
                "report_date": f"{year}-12-31",
                "revenue": revenue,
                "goodwill": goodwill[year],
                "acquisition_cash": acquisition["value"],
                "acquisition_cash_to_revenue": float(acquisition["value"]) / revenue,
                "acquisition_value_basis": acquisition["value_basis"],
                "acquisition_derivation": acquisition["derivation"],
                "source_report": acquisition["source_report"],
                "source_field": acquisition["source_field"],
                "source_url": acquisition["source_url"],
            }
        )
    # The source response is decoded through Python floats.  Rebuild every
    # record through the same Decimal-backed normalizer used by the public
    # validator before deriving the evidence id and summary.  Otherwise a
    # perfectly valid non-integer cash-flow ratio can differ by one binary
    # floating-point ulp between construction and validation, producing a
    # different content hash and falsely downgrading the whole Type 3 record.
    records = [_normalise_external_record(record, latest_year=latest_year) for record in records]
    status = "complete" if len(coverage_years) >= MIN_EXTERNAL_HISTORY_YEARS else "partial"
    acquisition_values = [float(record["acquisition_cash"]) for record in records]
    acquisition_ratios = [float(record["acquisition_cash_to_revenue"]) for record in records]
    goodwill_to_revenue_latest = float(records[-1]["goodwill"]) / float(records[-1]["revenue"]) if records else None
    goodwill_change_to_revenue = (
        (float(records[-1]["goodwill"]) - float(records[0]["goodwill"])) / float(records[-1]["revenue"])
        if len(records) >= 2
        else None
    )
    positive_goodwill_additions = (
        sum(
            max(
                float(current["goodwill"]) - float(previous["goodwill"]),
                0.0,
            )
            for previous, current in zip(records, records[1:])
        )
        if len(records) >= 2
        else None
    )
    positive_goodwill_additions_to_revenue = (
        positive_goodwill_additions / sum(float(record["revenue"]) for record in records[1:])
        if positive_goodwill_additions is not None
        else None
    )
    payload = {
        "model_id": EXTERNAL_MODEL_ID,
        "code": normalized_code,
        "as_of": cutoff.isoformat(),
        "coverage_years": coverage_years,
        "records": records,
    }
    return {
        "status": status,
        "source": "东方财富年度现金流、年度商誉与营业收入",
        "source_url": EASTMONEY_DATACENTER_URL,
        "evidence_id": f"eastmoney-external-growth:{normalized_code}:{_canonical_hash(payload)}",
        "as_of": cutoff.isoformat(),
        "security_code": normalized_code,
        "model_id": EXTERNAL_MODEL_ID,
        "contract_scope": "aggregate_proxy_not_transaction_census",
        "coverage_years": coverage_years,
        "coverage_year_count": len(coverage_years),
        "acquisition_cash_values": acquisition_values,
        "acquisition_cash_to_revenue": acquisition_ratios,
        "aggregate_acquisition_cash_to_revenue": (
            sum(acquisition_values) / sum(float(record["revenue"]) for record in records) if records else None
        ),
        "positive_goodwill_additions_to_revenue": positive_goodwill_additions_to_revenue,
        "goodwill_to_revenue_latest": goodwill_to_revenue_latest,
        "goodwill_change_to_revenue": goodwill_change_to_revenue,
        "records": records,
        "limitations": ["不提供逐笔并购交易清单", "不提供被收购业务收入占比"],
        "reason": (
            ""
            if status == "complete"
            else "fewer_than_five_consecutive_completed_years_with_resolved_acquisition_goodwill_and_revenue"
        ),
    }


def _assemble_growth_evidence(
    code: str,
    as_of: date,
    *,
    revenue_records: Sequence[Mapping[str, Any]],
    goodwill_records: Sequence[Mapping[str, Any]],
    acquisition_cashflow_records: Any,
    segment_growth_sources: dict[str, Any],
    cache_hit: bool,
    cache_diagnostic: str,
    acquisition_error: BaseException | None = None,
    preloaded_external_growth_evidence: Mapping[str, Any] | None = None,
) -> GrowthEvidence:
    if preloaded_external_growth_evidence is not None:
        try:
            external = _validate_external_evidence(
                preloaded_external_growth_evidence,
                code=code,
                as_of=as_of,
            )
        except Exception as exc:
            external = _unavailable_external_evidence(
                code,
                as_of,
                f"evidence_invalid:{_error_label(exc)}",
            )
    elif acquisition_error is None:
        try:
            external = build_external_growth_evidence(
                code,
                as_of,
                revenue_records=revenue_records,
                goodwill_records=goodwill_records,
                acquisition_cashflow_records=acquisition_cashflow_records,
            )
        except Exception as exc:
            external = _unavailable_external_evidence(
                code,
                as_of,
                f"evidence_invalid:{_error_label(exc)}",
            )
    else:
        external = _unavailable_external_evidence(
            code,
            as_of,
            f"source_unavailable:{_error_label(acquisition_error)}",
        )
    available = segment_growth_sources.get("status") == "complete" and external.get("status") == "complete"
    reasons = [
        f"segment:{segment_growth_sources.get('reason')}"
        for _ in [0]
        if segment_growth_sources.get("status") != "complete"
    ]
    if external.get("status") != "complete":
        reasons.append(f"external:{external.get('reason')}")
    result = GrowthEvidence(
        available=available,
        code=code,
        as_of=as_of.isoformat(),
        model_id=MODEL_ID,
        external_growth_evidence=external,
        segment_growth_sources=segment_growth_sources,
        cache_hit=cache_hit,
        cache_diagnostic=cache_diagnostic,
        reason=";".join(reasons),
    )
    return GrowthEvidence(
        **validate_growth_evidence_record(
            result.to_dict(),
            code,
            as_of,
        )
    )


def _equivalent_evidence(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        try:
            left_value = float(left)
            right_value = float(right)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(left_value)
            and math.isfinite(right_value)
            and math.isclose(left_value, right_value, rel_tol=1e-10, abs_tol=1e-12)
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_equivalent_evidence(left[key], right[key]) for key in left)
    if (
        isinstance(left, Sequence)
        and isinstance(right, Sequence)
        and not isinstance(left, (str, bytes))
        and not isinstance(right, (str, bytes))
    ):
        return len(left) == len(right) and all(
            _equivalent_evidence(left_item, right_item) for left_item, right_item in zip(left, right)
        )
    return type(left) is type(right) and left == right


def _validate_unavailable_segment(
    evidence: Mapping[str, Any],
    *,
    code: str,
    as_of: date,
) -> dict[str, Any]:
    if (
        set(evidence) != _SEGMENT_EVIDENCE_FIELDS
        or evidence.get("status") != "unavailable"
        or evidence.get("source") != "东方财富主营构成年度数据"
        or evidence.get("source_url") != EASTMONEY_BUSINESS_ENDPOINT
        or evidence.get("security_code") != code
        or evidence.get("as_of") != as_of.isoformat()
        or evidence.get("model_id") != SEGMENT_MODEL_ID
        or evidence.get("contract_scope") != "annual_segment_revenue_history"
        or evidence.get("dimension") is not None
        or evidence.get("history_years") != []
        or evidence.get("growth_source_count") != 0
        or evidence.get("effective_growth_source_count") != 0.0
        or evidence.get("positive_growth_share") is not None
        or evidence.get("revenue_hhi") is not None
        or evidence.get("top_segment_share") is not None
        or evidence.get("matched_latest_share") is not None
        or evidence.get("annual_revenue_latest") is not None
        or evidence.get("source_row_count") != 0
        or evidence.get("aggregate_revenue_cagr") is not None
        or evidence.get("segments") != []
        or evidence.get("records") != []
        or not isinstance(evidence.get("reason"), str)
        or not evidence.get("reason")
    ):
        raise GrowthEvidenceError("unavailable segment evidence is malformed")
    evidence_id = evidence.get("evidence_id")
    if (
        not isinstance(evidence_id, str)
        or not evidence_id.startswith(f"eastmoney-segments:{code}:")
        or len(evidence_id) > 200
    ):
        raise GrowthEvidenceError("unavailable segment evidence id is invalid")
    return dict(evidence)


def _validate_segment_evidence(
    evidence: Any,
    *,
    code: str,
    as_of: date,
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping) or set(evidence) != _SEGMENT_EVIDENCE_FIELDS:
        raise GrowthEvidenceError("segment evidence shape is invalid")
    if evidence.get("status") == "unavailable":
        return _validate_unavailable_segment(evidence, code=code, as_of=as_of)
    if evidence.get("status") not in {"complete", "partial"}:
        raise GrowthEvidenceError("segment evidence status is invalid")
    history_years = evidence.get("history_years")
    growth_source_count = evidence.get("growth_source_count")
    if (
        not isinstance(history_years, list)
        or any(isinstance(year, bool) or not isinstance(year, int) for year in history_years)
        or isinstance(growth_source_count, bool)
        or not isinstance(growth_source_count, int)
        or growth_source_count < 0
    ):
        raise GrowthEvidenceError("segment evidence integer fields are invalid")
    segments = evidence.get("segments")
    if not isinstance(segments, list):
        raise GrowthEvidenceError("segment evidence segment collection is invalid")
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise GrowthEvidenceError("segment evidence segment row is invalid")
        first_year = segment.get("first_year")
        latest_year = segment.get("latest_year")
        if (
            (first_year is not None and (isinstance(first_year, bool) or not isinstance(first_year, int)))
            or isinstance(latest_year, bool)
            or not isinstance(latest_year, int)
        ):
            raise GrowthEvidenceError("segment evidence segment years are invalid")
    records = _validate_cached_segment_records(
        evidence.get("records"),
        code=code,
        as_of=as_of,
    )
    annual_revenue_latest = evidence.get("annual_revenue_latest")
    if annual_revenue_latest is not None and not isinstance(annual_revenue_latest, bool):
        annual_revenue_latest = _finite_decimal(annual_revenue_latest, field="annual_revenue_latest")
        if annual_revenue_latest is not None:
            annual_revenue_latest = float(annual_revenue_latest)
    rebuilt = _build_segment_growth_sources(
        code,
        as_of,
        records,
        annual_revenue=(
            {int(evidence["history_years"][-1]): annual_revenue_latest}
            if annual_revenue_latest is not None
            and isinstance(evidence.get("history_years"), list)
            and evidence["history_years"]
            else None
        ),
        source_row_count=evidence.get("source_row_count"),
    )
    if not _equivalent_evidence(evidence, rebuilt):
        raise GrowthEvidenceError("segment evidence summary does not reproduce from its records")
    return rebuilt


def _validate_derivation(
    basis: Any,
    derivation: Any,
    *,
    acquisition_cash: float,
) -> dict[str, Any]:
    if not isinstance(derivation, Mapping):
        raise GrowthEvidenceError("acquisition derivation is not an object")
    if basis == "source_reported":
        if set(derivation) != {"reported_value"}:
            raise GrowthEvidenceError("reported acquisition provenance is malformed")
        reported = _finite_decimal(
            derivation.get("reported_value"),
            field="reported acquisition value",
        )
        if reported is None:
            raise GrowthEvidenceError("reported acquisition provenance value is missing")
        if not _close_decimal(reported, Decimal(str(acquisition_cash))):
            raise GrowthEvidenceError("reported acquisition provenance value mismatch")
        return {"reported_value": float(reported)}
    if basis not in {
        "derived_aggregate_identity_zero",
        "derived_aggregate_identity_residual",
    }:
        raise GrowthEvidenceError("acquisition value basis is invalid")
    required = {
        "total_invest_outflow",
        "total_basis",
        "known_component_sum",
        "component_values",
        "net_identity_components",
        "source_null_fields",
        "identity_residual",
        "rounding_tolerance_cny",
    }
    if set(derivation) != required:
        raise GrowthEvidenceError("derived acquisition provenance is malformed")
    total = _finite_decimal(derivation.get("total_invest_outflow"), field="total investment outflow")
    known = _finite_decimal(derivation.get("known_component_sum"), field="known component sum")
    residual = _finite_decimal(derivation.get("identity_residual"), field="identity residual")
    if total is None or known is None or residual is None:
        raise GrowthEvidenceError("derived acquisition provenance identity is incomplete")
    if total < 0 or known < 0 or residual < 0 or not _close_decimal(total - known, residual):
        raise GrowthEvidenceError("derived acquisition provenance identity does not close")
    total_basis = derivation.get("total_basis")
    if total_basis not in {
        "source_reported_total_invest_outflow",
        "derived_from_investing_net_cash_identity",
    }:
        raise GrowthEvidenceError("derived acquisition total basis is invalid")
    tolerance = _finite_decimal(
        derivation.get("rounding_tolerance_cny"),
        field="identity rounding tolerance",
    )
    if tolerance != _CURRENCY_IDENTITY_TOLERANCE:
        raise GrowthEvidenceError("derived acquisition rounding tolerance is invalid")
    raw_components = derivation.get("component_values")
    if not isinstance(raw_components, Mapping) or set(raw_components) != set(_EXTERNAL_COMPONENT_FIELDS):
        raise GrowthEvidenceError("derived acquisition component provenance is malformed")
    component_values: dict[str, Decimal | None] = {}
    for field in _EXTERNAL_COMPONENT_FIELDS:
        raw_value = raw_components.get(field)
        if raw_value is None:
            component_values[field] = None
            continue
        parsed = _finite_decimal(raw_value, field=f"derived component {field}")
        if parsed is None:
            raise GrowthEvidenceError("derived acquisition component is missing")
        if parsed < 0:
            raise GrowthEvidenceError("derived acquisition component is negative")
        component_values[field] = parsed
    reproduced_known = sum(
        (value for value in component_values.values() if value is not None),
        Decimal(0),
    )
    if not _close_decimal(known, reproduced_known):
        raise GrowthEvidenceError("derived acquisition known-component sum does not reproduce")
    raw_net_components = derivation.get("net_identity_components")
    if not isinstance(raw_net_components, Mapping):
        raise GrowthEvidenceError("derived acquisition net-identity provenance is malformed")
    if total_basis == "source_reported_total_invest_outflow":
        if raw_net_components:
            raise GrowthEvidenceError("reported total outflow must not contain derived net components")
    else:
        if set(raw_net_components) != set(_NET_OUTFLOW_IDENTITY_FIELDS):
            raise GrowthEvidenceError("derived total outflow net components are incomplete")
        net_components: dict[str, Decimal] = {}
        for field in _NET_OUTFLOW_IDENTITY_FIELDS:
            parsed = _finite_decimal(raw_net_components.get(field), field=f"net identity {field}")
            if parsed is None:
                raise GrowthEvidenceError("derived total outflow net component is missing")
            net_components[field] = parsed
        reproduced_total = (
            net_components["TOTAL_INVEST_INFLOW"]
            + net_components["INVEST_NETCASH_OTHER"]
            + net_components["INVEST_NETCASH_BALANCE"]
            - net_components["NETCASH_INVEST"]
        )
        if not _close_decimal(total, reproduced_total):
            raise GrowthEvidenceError("derived total investment outflow does not reproduce")
    null_fields = derivation.get("source_null_fields")
    if (
        not isinstance(null_fields, list)
        or not null_fields
        or null_fields != sorted(set(null_fields))
        or any(field not in _EXTERNAL_COMPONENT_FIELDS for field in null_fields)
        or _ACQUISITION_FIELD not in null_fields
        or set(null_fields) != {field for field, value in component_values.items() if value is None}
    ):
        raise GrowthEvidenceError("derived acquisition null-field provenance is invalid")
    if basis == "derived_aggregate_identity_zero":
        if acquisition_cash != 0.0 or not _close_decimal(residual, Decimal(0)):
            raise GrowthEvidenceError("derived-zero acquisition provenance is not zero")
    elif (
        null_fields != [_ACQUISITION_FIELD]
        or acquisition_cash <= 0
        or not _close_decimal(residual, Decimal(str(acquisition_cash)))
    ):
        raise GrowthEvidenceError("derived-residual acquisition provenance is not unique")
    return {
        "total_invest_outflow": float(total),
        "total_basis": total_basis,
        "known_component_sum": float(known),
        "component_values": {
            field: (float(value) if value is not None else None) for field, value in component_values.items()
        },
        "net_identity_components": dict(raw_net_components),
        "source_null_fields": null_fields,
        "identity_residual": float(residual),
        "rounding_tolerance_cny": float(_CURRENCY_IDENTITY_TOLERANCE),
    }


def _normalise_external_record(
    value: Any,
    *,
    latest_year: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EXTERNAL_RECORD_FIELDS:
        raise GrowthEvidenceError("external growth record shape is invalid")
    year = value.get("year")
    if (
        isinstance(year, bool)
        or not isinstance(year, int)
        or not 1990 <= year <= latest_year
        or value.get("report_date") != f"{year}-12-31"
    ):
        raise GrowthEvidenceError("external growth record year is invalid")
    revenue = _finite_decimal(value.get("revenue"), field="external revenue")
    goodwill = _finite_decimal(value.get("goodwill"), field="external goodwill")
    acquisition = _finite_decimal(value.get("acquisition_cash"), field="external acquisition cash")
    ratio = _finite_decimal(
        value.get("acquisition_cash_to_revenue"),
        field="external acquisition/revenue ratio",
    )
    if revenue is None or goodwill is None or acquisition is None or ratio is None:
        raise GrowthEvidenceError("external growth record contains an incomplete numeric value")
    if revenue <= 0 or goodwill < 0 or acquisition < 0:
        raise GrowthEvidenceError("external growth record contains an invalid signed value")
    calculated_ratio = acquisition / revenue
    if not _close_ratio_decimal(ratio, calculated_ratio):
        raise GrowthEvidenceError("external acquisition/revenue ratio does not reproduce")
    basis = value.get("acquisition_value_basis")
    derivation = _validate_derivation(
        basis,
        value.get("acquisition_derivation"),
        acquisition_cash=float(acquisition),
    )
    if (
        value.get("source_report") != "RPT_F10_FINANCE_GCASHFLOW"
        or value.get("source_field") != _ACQUISITION_FIELD
        or value.get("source_url") != EASTMONEY_DATACENTER_URL
    ):
        raise GrowthEvidenceError("external growth record source identity is invalid")
    return {
        "year": year,
        "report_date": f"{year}-12-31",
        "revenue": float(revenue),
        "goodwill": float(goodwill),
        "acquisition_cash": float(acquisition),
        "acquisition_cash_to_revenue": float(calculated_ratio),
        "acquisition_value_basis": basis,
        "acquisition_derivation": derivation,
        "source_report": "RPT_F10_FINANCE_GCASHFLOW",
        "source_field": _ACQUISITION_FIELD,
        "source_url": EASTMONEY_DATACENTER_URL,
    }


def _validate_unavailable_external(
    evidence: Mapping[str, Any],
    *,
    code: str,
    as_of: date,
) -> dict[str, Any]:
    expected = _unavailable_external_evidence(code, as_of, str(evidence.get("reason") or ""))
    expected["evidence_id"] = evidence.get("evidence_id")
    if (
        set(evidence) != _EXTERNAL_EVIDENCE_FIELDS
        or evidence.get("status") != "unavailable"
        or not isinstance(evidence.get("reason"), str)
        or not evidence.get("reason")
        or not _equivalent_evidence(evidence, expected)
    ):
        raise GrowthEvidenceError("unavailable external growth evidence is malformed")
    evidence_id = evidence.get("evidence_id")
    if (
        not isinstance(evidence_id, str)
        or not evidence_id.startswith(f"eastmoney-external-growth:{code}:")
        or len(evidence_id) > 200
    ):
        raise GrowthEvidenceError("unavailable external growth evidence id is invalid")
    return dict(evidence)


def _validate_external_evidence(
    evidence: Any,
    *,
    code: str,
    as_of: date,
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping) or set(evidence) != _EXTERNAL_EVIDENCE_FIELDS:
        raise GrowthEvidenceError("external growth evidence shape is invalid")
    if evidence.get("status") == "unavailable":
        return _validate_unavailable_external(evidence, code=code, as_of=as_of)
    if evidence.get("status") not in {"complete", "partial"}:
        raise GrowthEvidenceError("external growth evidence status is invalid")
    coverage_years = evidence.get("coverage_years")
    coverage_year_count = evidence.get("coverage_year_count")
    if (
        not isinstance(coverage_years, list)
        or any(isinstance(year, bool) or not isinstance(year, int) for year in coverage_years)
        or isinstance(coverage_year_count, bool)
        or not isinstance(coverage_year_count, int)
        or coverage_year_count != len(coverage_years)
    ):
        raise GrowthEvidenceError("external growth coverage integer fields are invalid")
    if (
        evidence.get("source") != "东方财富年度现金流、年度商誉与营业收入"
        or evidence.get("source_url") != EASTMONEY_DATACENTER_URL
        or evidence.get("security_code") != code
        or evidence.get("as_of") != as_of.isoformat()
        or evidence.get("model_id") != EXTERNAL_MODEL_ID
        or evidence.get("contract_scope") != "aggregate_proxy_not_transaction_census"
        or evidence.get("limitations") != ["不提供逐笔并购交易清单", "不提供被收购业务收入占比"]
    ):
        raise GrowthEvidenceError("external growth evidence identity is invalid")
    raw_records = evidence.get("records")
    if (
        not isinstance(raw_records, list)
        or len(raw_records) > EXTERNAL_HISTORY_YEARS
        or any(not isinstance(record, Mapping) for record in raw_records)
    ):
        raise GrowthEvidenceError("external growth record collection is invalid")
    latest_year = _latest_completed_annual_year(as_of)
    records = [_normalise_external_record(record, latest_year=latest_year) for record in raw_records]
    years = [record["year"] for record in records]
    expected_years = list(range(latest_year - len(years) + 1, latest_year + 1)) if years else []
    if years != expected_years or evidence.get("coverage_years") != years:
        raise GrowthEvidenceError("external growth coverage years are not consecutive through the cutoff")
    status = "complete" if len(years) >= MIN_EXTERNAL_HISTORY_YEARS else "partial"
    acquisition_values = [record["acquisition_cash"] for record in records]
    acquisition_ratios = [record["acquisition_cash_to_revenue"] for record in records]
    aggregate_ratio = sum(acquisition_values) / sum(record["revenue"] for record in records) if records else None
    latest_goodwill_ratio = records[-1]["goodwill"] / records[-1]["revenue"] if records else None
    goodwill_change_ratio = (
        (records[-1]["goodwill"] - records[0]["goodwill"]) / records[-1]["revenue"] if len(records) >= 2 else None
    )
    positive_goodwill_additions = (
        sum(max(current["goodwill"] - previous["goodwill"], 0.0) for previous, current in zip(records, records[1:]))
        if len(records) >= 2
        else None
    )
    positive_goodwill_additions_ratio = (
        positive_goodwill_additions / sum(record["revenue"] for record in records[1:])
        if positive_goodwill_additions is not None
        else None
    )
    payload = {
        "model_id": EXTERNAL_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "coverage_years": years,
        "records": records,
    }
    rebuilt = {
        "status": status,
        "source": "东方财富年度现金流、年度商誉与营业收入",
        "source_url": EASTMONEY_DATACENTER_URL,
        "evidence_id": f"eastmoney-external-growth:{code}:{_canonical_hash(payload)}",
        "as_of": as_of.isoformat(),
        "security_code": code,
        "model_id": EXTERNAL_MODEL_ID,
        "contract_scope": "aggregate_proxy_not_transaction_census",
        "coverage_years": years,
        "coverage_year_count": len(years),
        "acquisition_cash_values": acquisition_values,
        "acquisition_cash_to_revenue": acquisition_ratios,
        "aggregate_acquisition_cash_to_revenue": aggregate_ratio,
        "positive_goodwill_additions_to_revenue": positive_goodwill_additions_ratio,
        "goodwill_to_revenue_latest": latest_goodwill_ratio,
        "goodwill_change_to_revenue": goodwill_change_ratio,
        "records": records,
        "limitations": ["不提供逐笔并购交易清单", "不提供被收购业务收入占比"],
        "reason": (
            ""
            if status == "complete"
            else "fewer_than_five_consecutive_completed_years_with_resolved_acquisition_goodwill_and_revenue"
        ),
    }
    if not _equivalent_evidence(evidence, rebuilt):
        raise GrowthEvidenceError("external growth evidence summary does not reproduce from its records")
    return rebuilt


def validate_growth_evidence_record(
    evidence: Any,
    expected_code: str,
    expected_as_of: date | str,
) -> dict[str, Any]:
    """Recompute every derived field and return a normalized evidence copy."""
    code = _normalise_code(expected_code)
    as_of = _parse_as_of(expected_as_of)
    if not isinstance(evidence, Mapping) or set(evidence) != _TOP_EVIDENCE_FIELDS:
        raise GrowthEvidenceError("growth evidence top-level shape is invalid")
    if (
        evidence.get("code") != code
        or evidence.get("as_of") != as_of.isoformat()
        or evidence.get("model_id") != MODEL_ID
        or not isinstance(evidence.get("available"), bool)
        or not isinstance(evidence.get("cache_hit"), bool)
        or not isinstance(evidence.get("cache_diagnostic"), str)
        or len(evidence.get("cache_diagnostic")) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in evidence.get("cache_diagnostic"))
    ):
        raise GrowthEvidenceError("growth evidence identity or cache metadata is invalid")
    segment = _validate_segment_evidence(
        evidence.get("segment_growth_sources"),
        code=code,
        as_of=as_of,
    )
    external = _validate_external_evidence(
        evidence.get("external_growth_evidence"),
        code=code,
        as_of=as_of,
    )
    available = segment["status"] == "complete" and external["status"] == "complete"
    if evidence.get("available") is not available:
        raise GrowthEvidenceError("growth evidence available flag contradicts child statuses")
    reasons: list[str] = []
    if segment["status"] != "complete":
        reasons.append(f"segment:{segment['reason']}")
    if external["status"] != "complete":
        reasons.append(f"external:{external['reason']}")
    reason = ";".join(reasons)
    if evidence.get("reason") != reason:
        raise GrowthEvidenceError("growth evidence reason contradicts child statuses")
    return {
        "available": available,
        "code": code,
        "as_of": as_of.isoformat(),
        "model_id": MODEL_ID,
        "external_growth_evidence": external,
        "segment_growth_sources": segment,
        "cache_hit": evidence["cache_hit"],
        "cache_diagnostic": evidence["cache_diagnostic"],
        "reason": reason,
    }


def fetch_growth_evidence(
    code: str,
    as_of: date | str,
    *,
    revenue_records: Sequence[Mapping[str, Any]],
    goodwill_records: Sequence[Mapping[str, Any]],
    acquisition_cashflow_records: Any = None,
    session: Any = requests,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> GrowthEvidence:
    """Fetch and bind one company's Type 3 growth-evidence package."""
    normalized_code = _normalise_code(code)
    cutoff = _parse_as_of(as_of)
    _prepare_financial_records(
        revenue_records,
        label="revenue_records",
        nonnegative=True,
        as_of=cutoff,
    )
    _prepare_financial_records(
        goodwill_records,
        label="goodwill_records",
        nonnegative=True,
        as_of=cutoff,
    )
    segment, cache_hit, cache_diagnostic = _fetch_segment_growth_sources(
        normalized_code,
        cutoff,
        session=session,
        cache_dir=cache_dir,
        cache_ttl_seconds=cache_ttl_seconds,
        use_cache=use_cache,
        timeout=timeout,
        rate_limiter=rate_limiter,
        annual_revenue=dict(
            _prepare_financial_records(revenue_records, label="revenue_records", nonnegative=True, as_of=cutoff)
        ),
    )
    acquisition_error: BaseException | None = None
    if acquisition_cashflow_records is None:
        years = list(
            range(
                _latest_completed_annual_year(cutoff),
                _latest_completed_annual_year(cutoff) - EXTERNAL_HISTORY_YEARS,
                -1,
            )
        )
        try:
            acquisition_cashflow_records = fetch_detailed_annual_cashflow_history(
                years,
                codes=[normalized_code],
            )
        except DataFetchError as exc:
            acquisition_cashflow_records = []
            acquisition_error = exc
    return _assemble_growth_evidence(
        normalized_code,
        cutoff,
        revenue_records=revenue_records,
        goodwill_records=goodwill_records,
        acquisition_cashflow_records=acquisition_cashflow_records,
        segment_growth_sources=segment,
        cache_hit=cache_hit,
        cache_diagnostic=cache_diagnostic,
        acquisition_error=acquisition_error,
    )


def fetch_growth_evidence_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = MAX_WORKERS,
    progress_cb: Any = None,
    cache_dir: str | Path = SEGMENT_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Fetch a deterministic, bounded batch for exact Type 3 preflight candidates."""
    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("growth-evidence batch requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("growth-evidence batch exceeds the company limit")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_WORKERS:
        raise ValueError(f"max_workers must be between 1 and {MAX_WORKERS}")
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    prepared: list[tuple[str, date, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {
            "code",
            "as_of",
            "revenue_records",
            "goodwill_records",
        }:
            raise ValueError("growth-evidence batch request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_as_of(request.get("as_of"))
        if code in seen:
            raise ValueError(f"growth-evidence batch contains duplicate code: {code}")
        revenue_records = request.get("revenue_records")
        goodwill_records = request.get("goodwill_records")
        _prepare_financial_records(
            revenue_records,
            label="revenue_records",
            nonnegative=True,
            as_of=cutoff,
        )
        _prepare_financial_records(
            goodwill_records,
            label="goodwill_records",
            nonnegative=True,
            as_of=cutoff,
        )
        seen.add(code)
        prepared.append((code, cutoff, revenue_records, goodwill_records))
    prepared.sort(key=lambda item: item[0])
    if not prepared:
        return {}
    directory = Path(cache_dir)
    cache_state = load_growth_evidence_cache_batch_state(requests_, cache_dir=directory)
    external_cache_state = load_external_growth_evidence_cache_batch_state(requests_, cache_dir=directory)

    acquisition_by_code: dict[str, list[dict[str, Any]]] = {code: [] for code, *_ in prepared}
    acquisition_errors: dict[str, BaseException] = {}
    grouped: dict[tuple[int, ...], list[str]] = {}
    for code, cutoff, *_ in prepared:
        if code in external_cache_state:
            continue
        latest = _latest_completed_annual_year(cutoff)
        years = tuple(range(latest, latest - EXTERNAL_HISTORY_YEARS, -1))
        grouped.setdefault(years, []).append(code)
    for years, codes in grouped.items():
        # Eastmoney's code predicate is intentionally bounded to 100 symbols.
        # Passing a larger list would silently remove the predicate and fetch
        # the entire market for every annual period, which is both slow and
        # capable of triggering the source's rate limits.  Chunk explicitly so
        # the expanded whole-market evidence pass remains bounded and auditable.
        for offset in range(0, len(codes), _CASHFLOW_BATCH_COMPANIES):
            code_chunk = codes[offset : offset + _CASHFLOW_BATCH_COMPANIES]
            try:
                frame = fetch_detailed_annual_cashflow_history(list(years), codes=code_chunk)
                if not frame.empty:
                    frame_codes = frame["SECURITY_CODE"].astype(str).str.strip()
                    for code in code_chunk:
                        acquisition_by_code[code] = frame.loc[frame_codes.eq(code)].to_dict(orient="records")
            except Exception as exc:
                for code in code_chunk:
                    acquisition_errors[code] = exc

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(prepared))) as executor:
        futures = {
            executor.submit(
                _fetch_segment_growth_sources,
                code,
                cutoff,
                recent_cache_state=cache_state,
                cache_dir=directory,
                cache_ttl_seconds=cache_ttl_seconds,
            ): (code, cutoff, revenue_records, goodwill_records)
            for code, cutoff, revenue_records, goodwill_records in prepared
        }
        completed = 0
        for future in as_completed(futures):
            code, cutoff, revenue_records, goodwill_records = futures[future]
            try:
                segment, cache_hit, cache_diagnostic = future.result()
                external_state = external_cache_state.get(code)
                preloaded_external: Mapping[str, Any] | None = None
                if external_state is not None:
                    if not isinstance(external_state, Mapping) or set(external_state) != _EXTERNAL_CACHE_STATE_FIELDS:
                        raise GrowthEvidenceError("preloaded external cache state is invalid")
                    candidate = external_state.get("external_growth_evidence")
                    external_diagnostic = external_state.get("cache_diagnostic")
                    if not isinstance(candidate, Mapping) or not isinstance(external_diagnostic, str):
                        raise GrowthEvidenceError("preloaded external cache contents are invalid")
                    preloaded_external = candidate
                    cache_hit = True
                    cache_diagnostic = f"{cache_diagnostic};external:{external_diagnostic}"
                result = _assemble_growth_evidence(
                    code,
                    cutoff,
                    revenue_records=revenue_records,
                    goodwill_records=goodwill_records,
                    acquisition_cashflow_records=acquisition_by_code[code],
                    segment_growth_sources=segment,
                    cache_hit=cache_hit,
                    cache_diagnostic=cache_diagnostic,
                    acquisition_error=acquisition_errors.get(code),
                    preloaded_external_growth_evidence=preloaded_external,
                )
                if preloaded_external is None:
                    _save_external_growth_evidence_cache(
                        code,
                        cutoff,
                        revenue_records=revenue_records,
                        goodwill_records=goodwill_records,
                        evidence=result.external_growth_evidence,
                        cache_dir=directory,
                        cache_ttl_seconds=cache_ttl_seconds,
                    )
            except Exception as exc:
                segment = {
                    "status": "unavailable",
                    "source": "东方财富主营构成年度数据",
                    "source_url": EASTMONEY_BUSINESS_ENDPOINT,
                    "evidence_id": f"eastmoney-segments:{code}:worker-failure",
                    "as_of": cutoff.isoformat(),
                    "security_code": code,
                    "model_id": SEGMENT_MODEL_ID,
                    "contract_scope": "annual_segment_revenue_history",
                    "dimension": None,
                    "history_years": [],
                    "growth_source_count": 0,
                    "effective_growth_source_count": 0.0,
                    "positive_growth_share": None,
                    "revenue_hhi": None,
                    "top_segment_share": None,
                    "matched_latest_share": None,
                    "annual_revenue_latest": None,
                    "source_row_count": 0,
                    "aggregate_revenue_cagr": None,
                    "segments": [],
                    "records": [],
                    "reason": f"worker_failure:{_error_label(exc)}",
                }
                result = _assemble_growth_evidence(
                    code,
                    cutoff,
                    revenue_records=revenue_records,
                    goodwill_records=goodwill_records,
                    acquisition_cashflow_records=[],
                    segment_growth_sources=segment,
                    cache_hit=False,
                    cache_diagnostic="",
                    acquisition_error=exc,
                )
            results[code] = result.to_dict()
            completed += 1
            if progress_cb:
                progress_cb(completed, len(prepared))
    return {code: results[code] for code, *_ in prepared}


__all__ = [
    "EASTMONEY_BUSINESS_ENDPOINT",
    "EXTERNAL_CACHE_MODEL_ID",
    "EXTERNAL_CACHE_REUSE_DAYS",
    "EXTERNAL_MODEL_ID",
    "GrowthEvidence",
    "GrowthEvidenceError",
    "MODEL_ID",
    "SEGMENT_CACHE_REUSE_DAYS",
    "SEGMENT_MODEL_ID",
    "TYPE3_GROWTH_RETRY_MODEL_ID",
    "TYPE3_GROWTH_STRUCTURAL_RETRY_DAYS",
    "TYPE3_GROWTH_TRANSIENT_RETRY_DAYS",
    "build_external_growth_evidence",
    "fetch_growth_evidence",
    "fetch_growth_evidence_batch",
    "load_external_growth_evidence_cache_batch_state",
    "load_growth_evidence_cache_batch_state",
    "load_growth_evidence_retry_state_batch",
    "record_growth_evidence_retry_states",
    "validate_growth_evidence_record",
]
