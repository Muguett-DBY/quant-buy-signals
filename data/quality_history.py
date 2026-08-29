"""Auditable long-horizon market evidence shared by screening models.

The shared contract provides an actual ten-year shareholder return and a
five-year historical valuation percentile.  Neither value may be inferred
from the current quote.  This module acquires the two independent histories
in a bounded, cached batch and returns structured unavailable states on
failure.  The persisted model identifier remains stable for replay
compatibility even as more than one screening model consumes the evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS, REQUEST_TIMEOUT
from data.as_of import shanghai_today
from data.cache import SafeCacheConflict, SafeCacheError, SafeFileCache
from data.market_history import (
    TENCENT_HISTORY_ENDPOINT,
    TENCENT_SOURCE,
    TencentWeeklyHistoryAdapter,
    WeeklyClose,
)
from data.provider_http import RequestRateLimiter, is_transient_request_error, retry_delay_seconds, thread_local_session


MODEL_ID = "type7-market-history-v1"
EASTMONEY_VALUATION_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_VALUATION_SOURCE = "Eastmoney historical valuation"
QUALITY_HISTORY_CACHE_DIR = CACHE_DIRECTORY / "quality_history"

TEN_YEAR_BAR_LIMIT = 620
TEN_YEAR_TARGET_DAYS = 3_652
TEN_YEAR_START_TOLERANCE_DAYS = 62
FIVE_YEAR_TARGET_DAYS = 1_826
FIVE_YEAR_START_TOLERANCE_DAYS = 62
VALUATION_MIN_OBSERVATIONS = 500
LATEST_MAX_AGE_DAYS = 21
VALUATION_PAGE_SIZE = 2_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_BATCH_COMPANIES = 2_000
CACHE_SCHEMA_VERSION = 1
REUSABLE_CACHE_MAX_AGE_DAYS = LATEST_MAX_AGE_DAYS
PARTIAL_STRUCTURAL_RETRY_DAYS = 7
PARTIAL_TRANSIENT_RETRY_DAYS = 1
VALUATION_REQUEST_ATTEMPTS = 3
VALUATION_REQUEST_INTERVAL_SECONDS = 0.25
VALUATION_RETRY_BACKOFF_SECONDS = 1.0
_VALUATION_RATE_LIMITER = RequestRateLimiter(VALUATION_REQUEST_INTERVAL_SECONDS)

_COMPONENT_SHAREHOLDER_RETURN = "shareholder_return"
_COMPONENT_VALUATION_HISTORY = "valuation_history"
_QUALITY_HISTORY_COMPONENTS = (
    _COMPONENT_SHAREHOLDER_RETURN,
    _COMPONENT_VALUATION_HISTORY,
)
_LEGACY_CACHE_PAYLOAD_FIELDS = {"contract", "weekly_bars", "valuation_rows"}
_COMPONENT_CACHE_PAYLOAD_FIELDS = {
    *_LEGACY_CACHE_PAYLOAD_FIELDS,
    "component_checked_as_of",
}

_A_SHARE_CODE = re.compile(r"^[036][0-9]{5}$")
_CANONICAL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
}


class QualityHistoryError(RuntimeError):
    """A long-horizon market-history source or payload contract failed."""


@dataclass(frozen=True)
class QualityHistoryEvidence:
    available: bool
    code: str
    as_of: str
    model_id: str
    shareholder_return: dict[str, Any]
    valuation_history: dict[str, Any]
    sources: list[dict[str, str]]
    cache_hit: bool
    cache_diagnostic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _QualityHistoryCacheCapture:
    evidence: QualityHistoryEvidence
    weekly_bars: list[WeeklyClose]
    valuation_rows: list[dict[str, Any]]
    component_checked_as_of: dict[str, date]


def _error_label(exc: BaseException, *, limit: int = 180) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}:{message[:limit]}" if message else type(exc).__name__


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if not _A_SHARE_CODE.fullmatch(code):
        raise ValueError("market-history code must be a Shanghai/Shenzhen six-digit code")
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
    if parsed > shanghai_today():
        raise ValueError("as_of cannot be in the future")
    return parsed


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualityHistoryError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                QualityHistoryError(f"{label} contains non-finite JSON: {value}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise QualityHistoryError(f"{label} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise QualityHistoryError(f"{label} is not valid JSON") from exc


def _read_bounded_response(response: Any, *, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > limit:
            raise QualityHistoryError("history response exceeds the declared byte limit")
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        chunks: list[bytes] = []
        received = 0
        for chunk in iterator(chunk_size=64 * 1024):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise QualityHistoryError("history response yielded non-byte content")
            received += len(chunk)
            if received > limit:
                raise QualityHistoryError("history response exceeds the byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    content = getattr(response, "content", None)
    if not isinstance(content, bytes) or len(content) > limit:
        raise QualityHistoryError("history response body is unavailable or too large")
    return content


def _validate_final_https_url(value: Any) -> None:
    parsed = urlsplit(str(value or ""))
    expected = urlsplit(EASTMONEY_VALUATION_ENDPOINT)
    try:
        port = parsed.port
    except ValueError as exc:
        raise QualityHistoryError("history source redirected outside the official endpoint") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != expected.hostname
        or parsed.path != expected.path
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise QualityHistoryError("history source redirected outside the official endpoint")


def _fetch_valuation_rows(
    code: str,
    as_of: date,
    *,
    session: Any = requests,
    timeout: int = REQUEST_TIMEOUT,
    rate_limiter: Any = _VALUATION_RATE_LIMITER,
) -> list[dict[str, Any]]:
    if session is requests:
        session = thread_local_session()
    start = _years_before(as_of, 5)
    params = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "SECURITY_CODE,PE_TTM,PB_MRQ,TRADE_DATE",
        "filter": f"(SECURITY_CODE=\"{code}\")(TRADE_DATE>='{start.isoformat()}')(TRADE_DATE<='{as_of.isoformat()}')",
        "pageNumber": 1,
        "pageSize": VALUATION_PAGE_SIZE,
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    last_error: BaseException | None = None
    payload: Any = None
    for attempt in range(VALUATION_REQUEST_ATTEMPTS):
        response = None
        retry_delay = 0.0
        transient = False
        try:
            rate_limiter.acquire()
            response = session.get(
                EASTMONEY_VALUATION_ENDPOINT,
                params=params,
                headers=_HEADERS,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            _validate_final_https_url(getattr(response, "url", EASTMONEY_VALUATION_ENDPOINT))
            payload = _decode_json(_read_bounded_response(response), label="Eastmoney valuation history")
            break
        except QualityHistoryError:
            raise
        except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
            last_error = exc
            transient = is_transient_request_error(exc, response)
            retry_delay = retry_delay_seconds(
                response,
                attempt=attempt,
                base_seconds=VALUATION_RETRY_BACKOFF_SECONDS,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not transient or attempt + 1 >= VALUATION_REQUEST_ATTEMPTS:
            raise QualityHistoryError(f"Eastmoney valuation request failed: {_error_label(last_error)}") from last_error
        time.sleep(retry_delay)

    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise QualityHistoryError("Eastmoney valuation payload reports failure")
    result = payload.get("result")
    rows = result.get("data") if isinstance(result, Mapping) else None
    count = result.get("count") if isinstance(result, Mapping) else None
    pages = result.get("pages") if isinstance(result, Mapping) else None
    if (
        not isinstance(rows, list)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or isinstance(pages, bool)
        or not isinstance(pages, int)
    ):
        raise QualityHistoryError("Eastmoney valuation payload has an invalid result shape")
    if count > VALUATION_PAGE_SIZE or pages != 1 or len(rows) != count:
        raise QualityHistoryError("Eastmoney valuation history was truncated")

    normalized: list[dict[str, Any]] = []
    seen_dates: set[date] = set()
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("SECURITY_CODE") or "") != code:
            raise QualityHistoryError("Eastmoney valuation row identity mismatch")
        raw_date = str(row.get("TRADE_DATE") or "")[:10]
        try:
            trade_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise QualityHistoryError("Eastmoney valuation row date is invalid") from exc
        if trade_date in seen_dates or not start <= trade_date <= as_of:
            raise QualityHistoryError("Eastmoney valuation dates are duplicate or outside the request window")
        seen_dates.add(trade_date)
        pe = _finite(row.get("PE_TTM"), positive=True)
        pb = _finite(row.get("PB_MRQ"), positive=True)
        normalized.append({"date": trade_date.isoformat(), "pe_ttm": pe, "pb_mrq": pb})
    normalized.sort(key=lambda item: item["date"])
    return normalized


def _serialize_bars(values: Iterable[WeeklyClose]) -> list[dict[str, Any]]:
    return [{"date": item.trade_date.isoformat(), "close": item.close} for item in values]


def _deserialize_bars(value: Any) -> list[WeeklyClose]:
    if not isinstance(value, list):
        raise QualityHistoryError("cached weekly bars are not a list")
    result: list[WeeklyClose] = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"date", "close"}:
            raise QualityHistoryError("cached weekly bar shape is invalid")
        try:
            result.append(WeeklyClose(date.fromisoformat(str(row["date"])), row["close"]))
        except (TypeError, ValueError) as exc:
            raise QualityHistoryError("cached weekly bar is invalid") from exc
    return result


def _normalise_valuation_rows(value: Any, code: str, as_of: date) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise QualityHistoryError("cached valuation rows are not a list")
    start = _years_before(as_of, 5)
    result: list[dict[str, Any]] = []
    seen: set[date] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"date", "pe_ttm", "pb_mrq"}:
            raise QualityHistoryError("cached valuation row shape is invalid")
        try:
            trade_date = date.fromisoformat(str(row["date"]))
        except ValueError as exc:
            raise QualityHistoryError("cached valuation row date is invalid") from exc
        if trade_date in seen or not start <= trade_date <= as_of:
            raise QualityHistoryError("cached valuation dates are duplicate or out of range")
        seen.add(trade_date)
        raw_pe = row.get("pe_ttm")
        raw_pb = row.get("pb_mrq")
        pe = None if raw_pe is None else _finite(raw_pe, positive=True)
        pb = None if raw_pb is None else _finite(raw_pb, positive=True)
        if (raw_pe is not None and pe is None) or (raw_pb is not None and pb is None):
            raise QualityHistoryError("cached valuation number is invalid")
        result.append({"date": trade_date.isoformat(), "pe_ttm": pe, "pb_mrq": pb})
    result.sort(key=lambda item: item["date"])
    return result


def _valuation_distribution(history: Sequence[float]) -> dict[str, list[Any]]:
    counts = Counter(float(value) for value in history)
    values = sorted(counts)
    return {
        "values": values,
        "counts": [int(counts[value]) for value in values],
    }


def replay_valuation_distribution(value: Any, current: Any) -> dict[str, float | int] | None:
    """Replay count, median and percentile from a compact raw-value distribution."""

    current_value = _finite(current, positive=True)
    if not isinstance(value, Mapping) or set(value) != {"values", "counts"} or current_value is None:
        return None
    values = value.get("values")
    counts = value.get("counts")
    if (
        not isinstance(values, list)
        or not isinstance(counts, list)
        or not values
        or len(values) != len(counts)
        or len(values) > VALUATION_PAGE_SIZE
    ):
        return None
    normalized_values: list[float] = []
    normalized_counts: list[int] = []
    for raw_value, raw_count in zip(values, counts):
        number = _finite(raw_value, positive=True)
        if (
            number is None
            or (normalized_values and number <= normalized_values[-1])
            or isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count <= 0
        ):
            return None
        normalized_values.append(number)
        normalized_counts.append(raw_count)
    observations = sum(normalized_counts)
    if observations <= 0 or observations > VALUATION_PAGE_SIZE:
        return None

    def ordered_value(index: int) -> float:
        consumed = 0
        for number, count in zip(normalized_values, normalized_counts):
            consumed += count
            if index < consumed:
                return number
        raise AssertionError("valuation distribution index exceeds observations")

    midpoint = observations // 2
    historical_median = (
        ordered_value(midpoint) if observations % 2 else (ordered_value(midpoint - 1) + ordered_value(midpoint)) / 2.0
    )
    below = sum(count for number, count in zip(normalized_values, normalized_counts) if number < current_value)
    equal = sum(count for number, count in zip(normalized_values, normalized_counts) if number == current_value)
    return {
        "observations": observations,
        "median": historical_median,
        "percentile": (below + 0.5 * equal) / observations,
    }


def _calculate_evidence(
    code: str,
    as_of: date,
    bars: Sequence[WeeklyClose],
    valuation_rows: Sequence[Mapping[str, Any]],
    *,
    cache_hit: bool,
    cache_diagnostic: str,
) -> QualityHistoryEvidence:
    normalized_bars = sorted((item for item in bars if item.trade_date <= as_of), key=lambda item: item.trade_date)
    if len({item.trade_date for item in normalized_bars}) != len(normalized_bars):
        raise QualityHistoryError("weekly history contains duplicate dates")
    target = _years_before(as_of, 10)
    start_bar = next((item for item in normalized_bars if item.trade_date >= target), None)
    end_bar = normalized_bars[-1] if normalized_bars else None
    shareholder: dict[str, Any] = {
        "available": False,
        "method": "Tencent backward-adjusted weekly close total-return proxy",
        "target_years": 10,
        "start_date": None,
        "end_date": None,
        "observations": len(normalized_bars),
        "total_return": None,
        "cagr": None,
        "reason": "",
    }
    if start_bar is None or end_bar is None:
        shareholder["reason"] = "missing_weekly_history"
    else:
        span_days = (end_bar.trade_date - start_bar.trade_date).days
        start_delay = (start_bar.trade_date - target).days
        latest_age = (as_of - end_bar.trade_date).days
        minimum_span = TEN_YEAR_TARGET_DAYS - TEN_YEAR_START_TOLERANCE_DAYS - LATEST_MAX_AGE_DAYS
        if span_days < minimum_span or not 0 <= start_delay <= TEN_YEAR_START_TOLERANCE_DAYS:
            shareholder["reason"] = "insufficient_ten_year_span"
        elif not 0 <= latest_age <= LATEST_MAX_AGE_DAYS:
            shareholder["reason"] = "stale_latest_weekly_price"
        else:
            ratio = end_bar.close / start_bar.close
            annualization = 365.2425 / span_days
            shareholder.update(
                {
                    "available": True,
                    "start_date": start_bar.trade_date.isoformat(),
                    "end_date": end_bar.trade_date.isoformat(),
                    "span_days": span_days,
                    "start_close_hfq": start_bar.close,
                    "end_close_hfq": end_bar.close,
                    "total_return": ratio - 1.0,
                    "cagr": ratio**annualization - 1.0,
                    "formula": "total=end_hfq/start_hfq-1;cagr=(end_hfq/start_hfq)^(365.2425/days)-1",
                    "reason": "",
                }
            )

    rows = sorted(valuation_rows, key=lambda item: str(item.get("date") or ""))
    latest = rows[-1] if rows else None
    valuation: dict[str, Any] = {
        "available": False,
        "window_years": 5,
        "target_start_date": _years_before(as_of, 5).isoformat(),
        "start_date": rows[0]["date"] if rows else None,
        "end_date": latest["date"] if latest else None,
        "row_count": len(rows),
        "pe_observations": 0,
        "pb_observations": 0,
        "current_pe_ttm": None,
        "current_pb_mrq": None,
        "median_pe_ttm": None,
        "median_pb_mrq": None,
        "pe_percentile": None,
        "pb_percentile": None,
        "pe_distribution": {"values": [], "counts": []},
        "pb_distribution": {"values": [], "counts": []},
        "reason": "",
        "limited_history": False,
    }
    if latest is None:
        valuation["reason"] = "missing_valuation_history"
    else:
        latest_date = date.fromisoformat(str(latest["date"]))
        first_date = date.fromisoformat(str(rows[0]["date"]))
        span_days = (latest_date - first_date).days
        start_delay = (first_date - _years_before(as_of, 5)).days
        latest_age = (as_of - latest_date).days
        prior = rows[:-1]
        pe_history = [value for row in prior if (value := _finite(row.get("pe_ttm"), positive=True)) is not None]
        pb_history = [value for row in prior if (value := _finite(row.get("pb_mrq"), positive=True)) is not None]
        current_pe = _finite(latest.get("pe_ttm"), positive=True)
        current_pb = _finite(latest.get("pb_mrq"), positive=True)
        pe_distribution = _valuation_distribution(pe_history)
        pb_distribution = _valuation_distribution(pb_history)
        pe_replay = replay_valuation_distribution(pe_distribution, current_pe)
        pb_replay = replay_valuation_distribution(pb_distribution, current_pb)
        pe_usable = len(pe_history) >= VALUATION_MIN_OBSERVATIONS and current_pe is not None
        pb_usable = len(pb_history) >= VALUATION_MIN_OBSERVATIONS and current_pb is not None
        pe_percentile = float(pe_replay["percentile"]) if pe_usable and pe_replay is not None else None
        pb_percentile = float(pb_replay["percentile"]) if pb_usable and pb_replay is not None else None
        sufficient_series = pe_usable or pb_usable
        valuation.update(
            {
                "span_days": span_days,
                "start_delay_days": start_delay,
                "pe_observations": len(pe_history),
                "pb_observations": len(pb_history),
                "current_pe_ttm": current_pe,
                "current_pb_mrq": current_pb,
                "median_pe_ttm": float(pe_replay["median"]) if pe_usable and pe_replay is not None else None,
                "median_pb_mrq": float(pb_replay["median"]) if pb_usable and pb_replay is not None else None,
                "pe_percentile": pe_percentile,
                "pb_percentile": pb_percentile,
                "pe_distribution": pe_distribution,
                "pb_distribution": pb_distribution,
                "formula": "percentile=(count(x<current)+0.5*count(x=current))/historical_count",
            }
        )
        if not 0 <= latest_age <= LATEST_MAX_AGE_DAYS:
            valuation["reason"] = "stale_latest_valuation"
        elif (
            not 0 <= start_delay <= FIVE_YEAR_START_TOLERANCE_DAYS
            or span_days < FIVE_YEAR_TARGET_DAYS - FIVE_YEAR_START_TOLERANCE_DAYS - LATEST_MAX_AGE_DAYS
        ):
            # Companies listed for less than five years cannot provide a full
            # five-year window.  When the available history still has enough
            # observations to estimate a meaningful percentile, mark the
            # record as limited history and keep it usable: the consumers
            # replay the actual window and the UI shows a "listed < X years"
            # note instead of dropping the dimension.
            #
            # The Eastmoney valuation fetch always starts at the 5y target, so
            # a row set that begins later than the tolerance is a company
            # listed within the window (recent listing) — its span is short.
            # A company whose early history is missing would show a full span
            # with a late start; that is not a recent listing and must stay
            # insufficient instead of being mislabelled "上市不足5年".
            if (
                span_days < FIVE_YEAR_TARGET_DAYS - FIVE_YEAR_START_TOLERANCE_DAYS - LATEST_MAX_AGE_DAYS
                and sufficient_series
                and (pe_percentile is not None or pb_percentile is not None)
            ) or (
                not 0 <= start_delay <= FIVE_YEAR_START_TOLERANCE_DAYS
                and sufficient_series
                and (pe_percentile is not None or pb_percentile is not None)
                and span_days < FIVE_YEAR_TARGET_DAYS + FIVE_YEAR_START_TOLERANCE_DAYS
            ):
                actual_span = (latest_date - first_date).days
                valuation.update(
                    {
                        "available": True,
                        "window_years": max(1.0, round(actual_span / 365.2425, 2)),
                        "target_start_date": first_date.isoformat(),
                        "span_days": actual_span,
                        "start_delay_days": 0,
                        "limited_history": True,
                        "reason": "",
                    }
                )
            else:
                valuation["reason"] = "insufficient_five_year_span"
        elif not sufficient_series or (pe_percentile is None and pb_percentile is None):
            valuation["reason"] = "insufficient_positive_valuation_observations"
        else:
            valuation["available"] = True

    missing = [
        label
        for label, record in (("shareholder_return", shareholder), ("valuation_history", valuation))
        if not record.get("available")
    ]
    return QualityHistoryEvidence(
        available=not missing,
        code=code,
        as_of=as_of.isoformat(),
        model_id=MODEL_ID,
        shareholder_return=shareholder,
        valuation_history=valuation,
        sources=[
            {"name": TENCENT_SOURCE, "url": TENCENT_HISTORY_ENDPOINT},
            {"name": EASTMONEY_VALUATION_SOURCE, "url": EASTMONEY_VALUATION_ENDPOINT},
        ],
        cache_hit=cache_hit,
        cache_diagnostic=cache_diagnostic,
        reason="" if not missing else "missing:" + ",".join(missing),
    )


def _cache_contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "weekly_source": TENCENT_HISTORY_ENDPOINT,
        "weekly_adjustment": "hfq",
        "weekly_bar_limit": TEN_YEAR_BAR_LIMIT,
        "valuation_source": EASTMONEY_VALUATION_ENDPOINT,
        "valuation_window_years": 5,
    }


def _cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{MODEL_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _reusable_cache_candidates(code: str, as_of: date, cache_dir: Path) -> list[tuple[date, Path]]:
    """Return recent, earlier cache generations newest-first.

    The persisted payload contains normalized source rows, not merely a final
    score.  Replaying a recent source capture against the requested cutoff is
    therefore safe while the ordinary latest-observation freshness contract
    remains satisfied.
    """

    candidates: list[tuple[date, Path]] = []
    # The filename contract is deterministic and the reuse horizon is short.
    # Probing those exact dates avoids rescanning a market-wide cache directory
    # once per company (quadratic directory-walk cost on Windows runners).
    for age_days in range(1, REUSABLE_CACHE_MAX_AGE_DAYS + 1):
        cached_as_of = date.fromordinal(as_of.toordinal() - age_days)
        path = _cache_path(code, cached_as_of, cache_dir)
        if path.is_file():
            candidates.append((cached_as_of, path))
    return candidates


def _component_checked_dates(payload: Mapping[str, Any], cached_as_of: date) -> dict[str, date]:
    raw = payload.get("component_checked_as_of")
    if raw is None:
        # Cache generations written before component-aware retry support
        # acquired both sources on the contract date.
        return {component: cached_as_of for component in _QUALITY_HISTORY_COMPONENTS}
    if not isinstance(raw, Mapping) or set(raw) != set(_QUALITY_HISTORY_COMPONENTS):
        raise QualityHistoryError("market-history cache component dates are invalid")
    result: dict[str, date] = {}
    for component in _QUALITY_HISTORY_COMPONENTS:
        value = raw.get(component)
        if not isinstance(value, str) or not _CANONICAL_DATE.fullmatch(value):
            raise QualityHistoryError("market-history cache component date is invalid")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise QualityHistoryError("market-history cache component date is invalid") from exc
        if parsed > cached_as_of:
            raise QualityHistoryError("market-history cache component date exceeds its contract")
        result[component] = parsed
    return result


def _capture_from_cache(
    payload: Any,
    code: str,
    *,
    cached_as_of: date,
    requested_as_of: date,
    diagnostic: str,
) -> _QualityHistoryCacheCapture:
    payload_fields = frozenset(payload) if isinstance(payload, Mapping) else frozenset()
    if not isinstance(payload, Mapping) or payload_fields not in {
        frozenset(_LEGACY_CACHE_PAYLOAD_FIELDS),
        frozenset(_COMPONENT_CACHE_PAYLOAD_FIELDS),
    }:
        raise QualityHistoryError("market-history cache payload shape is invalid")
    if payload.get("contract") != _cache_contract(code, cached_as_of):
        raise QualityHistoryError("market-history cache contract mismatch")
    bars = _deserialize_bars(payload.get("weekly_bars"))
    # Validate every persisted row against the contract that created it before
    # sliding the usable five-year window to the requested session.
    rows = _normalise_valuation_rows(payload.get("valuation_rows"), code, cached_as_of)
    requested_start = _years_before(requested_as_of, 5)
    rows = [row for row in rows if requested_start <= date.fromisoformat(str(row["date"])) <= requested_as_of]
    evidence = _calculate_evidence(
        code,
        requested_as_of,
        bars,
        rows,
        cache_hit=True,
        cache_diagnostic=diagnostic,
    )
    return _QualityHistoryCacheCapture(
        evidence=evidence,
        weekly_bars=bars,
        valuation_rows=rows,
        component_checked_as_of=_component_checked_dates(payload, cached_as_of),
    )


def _from_cache(payload: Any, code: str, as_of: date) -> QualityHistoryEvidence:
    return _capture_from_cache(
        payload,
        code,
        cached_as_of=as_of,
        requested_as_of=as_of,
        diagnostic="hit",
    ).evidence


def _from_recent_cache(
    payload: Any,
    code: str,
    *,
    cached_as_of: date,
    requested_as_of: date,
) -> QualityHistoryEvidence:
    return _capture_from_cache(
        payload,
        code,
        cached_as_of=cached_as_of,
        requested_as_of=requested_as_of,
        diagnostic=f"recent_source_capture:{cached_as_of.isoformat()}",
    ).evidence


def _partial_retry_days(record: Mapping[str, Any]) -> int:
    reason = str(record.get("reason") or "")
    if reason in {
        "missing_weekly_history",
        "stale_latest_weekly_price",
        "missing_valuation_history",
        "stale_latest_valuation",
    }:
        return PARTIAL_TRANSIENT_RETRY_DAYS
    return PARTIAL_STRUCTURAL_RETRY_DAYS


def _capture_refresh_components(
    capture: _QualityHistoryCacheCapture,
    requested_as_of: date,
) -> tuple[str, ...]:
    due: list[str] = []
    for component in _QUALITY_HISTORY_COMPONENTS:
        record = getattr(capture.evidence, component)
        if record.get("available") is True:
            continue
        checked_as_of = capture.component_checked_as_of[component]
        if (requested_as_of - checked_as_of).days >= _partial_retry_days(record):
            due.append(component)
    return tuple(due)


def _load_reusable_capture(
    code: str,
    as_of: date,
    *,
    cache_dir: Path,
    cache_ttl_seconds: int,
) -> _QualityHistoryCacheCapture | None:
    exact_path = _cache_path(code, as_of, cache_dir)
    exact = SafeFileCache(
        exact_path,
        schema_version=CACHE_SCHEMA_VERSION,
        ttl=int(cache_ttl_seconds),
        max_uncompressed_bytes=MAX_RESPONSE_BYTES,
    ).load()
    if exact.hit:
        try:
            return _capture_from_cache(
                exact.value,
                code,
                cached_as_of=as_of,
                requested_as_of=as_of,
                diagnostic="hit",
            )
        except QualityHistoryError:
            pass
    for cached_as_of, path in _reusable_cache_candidates(code, as_of, cache_dir):
        if path == exact_path:
            continue
        cached = SafeFileCache(
            path,
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=int(cache_ttl_seconds),
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        ).load(allow_expired=True)
        if not cached.hit:
            continue
        try:
            return _capture_from_cache(
                cached.value,
                code,
                cached_as_of=cached_as_of,
                requested_as_of=as_of,
                diagnostic=f"recent_source_capture:{cached_as_of.isoformat()}",
            )
        except QualityHistoryError:
            continue
    return None


def _load_reusable_cache(
    code: str,
    as_of: date,
    *,
    cache_dir: Path,
    cache_ttl_seconds: int,
) -> QualityHistoryEvidence | None:
    capture = _load_reusable_capture(
        code,
        as_of,
        cache_dir=cache_dir,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    return capture.evidence if capture is not None else None


def _weekly_component_rank(code: str, as_of: date, bars: Sequence[WeeklyClose]) -> tuple[int, int, int, int]:
    record = _calculate_evidence(
        code,
        as_of,
        bars,
        [],
        cache_hit=False,
        cache_diagnostic="component_rank",
    ).shareholder_return
    end_date = record.get("end_date")
    try:
        end_ordinal = date.fromisoformat(str(end_date)).toordinal() if end_date else 0
    except ValueError:
        end_ordinal = 0
    return (
        int(record.get("available") is True),
        int(record.get("span_days") or 0),
        end_ordinal,
        int(record.get("observations") or 0),
    )


def _valuation_component_rank(
    code: str,
    as_of: date,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[int, int, int, int, int]:
    record = _calculate_evidence(
        code,
        as_of,
        [],
        rows,
        cache_hit=False,
        cache_diagnostic="component_rank",
    ).valuation_history
    end_date = record.get("end_date")
    try:
        end_ordinal = date.fromisoformat(str(end_date)).toordinal() if end_date else 0
    except ValueError:
        end_ordinal = 0
    return (
        int(record.get("available") is True),
        int(record.get("span_days") or 0),
        max(int(record.get("pe_observations") or 0), int(record.get("pb_observations") or 0)),
        end_ordinal,
        int(record.get("row_count") or 0),
    )


def _prefer_weekly_bars(
    code: str,
    as_of: date,
    cached: Sequence[WeeklyClose],
    fresh: Sequence[WeeklyClose],
) -> list[WeeklyClose]:
    cached_list, fresh_list = list(cached), list(fresh)
    return (
        fresh_list
        if _weekly_component_rank(code, as_of, fresh_list) >= _weekly_component_rank(code, as_of, cached_list)
        else cached_list
    )


def _prefer_valuation_rows(
    code: str,
    as_of: date,
    cached: Sequence[Mapping[str, Any]],
    fresh: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    cached_list = [dict(row) for row in cached]
    fresh_list = [dict(row) for row in fresh]
    return (
        fresh_list
        if _valuation_component_rank(code, as_of, fresh_list) >= _valuation_component_rank(code, as_of, cached_list)
        else cached_list
    )


def fetch_quality_history(
    code: str,
    as_of: date | str,
    *,
    weekly_adapter: Any | None = None,
    valuation_session: Any = requests,
    cache_dir: str | Path = QUALITY_HISTORY_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
) -> QualityHistoryEvidence:
    """Fetch and replay one company's shared long-horizon market evidence."""

    normalized_code = _normalise_code(code)
    cutoff = _parse_as_of(as_of)
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    adapter = weekly_adapter or TencentWeeklyHistoryAdapter(
        bar_limit=TEN_YEAR_BAR_LIMIT,
        stock_adjustment="hfq",
    )
    cache: SafeFileCache | None = None
    capture: _QualityHistoryCacheCapture | None = None
    initial = None
    diagnostic = "disabled"
    refresh_components = tuple(_QUALITY_HISTORY_COMPONENTS)
    if use_cache:
        cache_root = Path(cache_dir)
        capture = _load_reusable_capture(
            normalized_code,
            cutoff,
            cache_dir=cache_root,
            cache_ttl_seconds=int(cache_ttl_seconds),
        )
        if capture is not None:
            refresh_components = _capture_refresh_components(capture, cutoff)
            if capture.evidence.available or not refresh_components:
                return capture.evidence
            diagnostic = "partial_retry:" + ",".join(refresh_components)
        cache = SafeFileCache(
            _cache_path(normalized_code, cutoff, cache_root),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=int(cache_ttl_seconds),
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        initial = cache.load()
        if initial.hit:
            try:
                exact_capture = _capture_from_cache(
                    initial.value,
                    normalized_code,
                    cached_as_of=cutoff,
                    requested_as_of=cutoff,
                    diagnostic="hit",
                )
                exact_refresh = _capture_refresh_components(exact_capture, cutoff)
                if exact_capture.evidence.available or not exact_refresh:
                    return exact_capture.evidence
                capture = exact_capture
                refresh_components = exact_refresh
                diagnostic = "partial_retry:" + ",".join(refresh_components)
            except QualityHistoryError as exc:
                diagnostic = f"invalid_hit:{_error_label(exc)}"
        elif capture is None:
            diagnostic = f"miss:{initial.reason}"

    bars = list(capture.weekly_bars) if capture is not None else []
    valuation_rows = [dict(row) for row in capture.valuation_rows] if capture is not None else []
    checked_dates = (
        dict(capture.component_checked_as_of)
        if capture is not None
        else {component: cutoff for component in _QUALITY_HISTORY_COMPONENTS}
    )
    source_errors: list[tuple[str, str]] = []
    if _COMPONENT_SHAREHOLDER_RETURN in refresh_components:
        checked_dates[_COMPONENT_SHAREHOLDER_RETURN] = cutoff
        try:
            fresh_bars = adapter.fetch_weekly_closes(
                ("sh" if normalized_code.startswith("6") else "sz") + normalized_code,
                cutoff,
                require_forward_adjusted=True,
            )
            bars = _prefer_weekly_bars(normalized_code, cutoff, bars, fresh_bars)
        except Exception as exc:
            source_errors.append((_COMPONENT_SHAREHOLDER_RETURN, _error_label(exc)))
    if _COMPONENT_VALUATION_HISTORY in refresh_components:
        checked_dates[_COMPONENT_VALUATION_HISTORY] = cutoff
        try:
            fresh_rows = _fetch_valuation_rows(normalized_code, cutoff, session=valuation_session)
            valuation_rows = _prefer_valuation_rows(
                normalized_code,
                cutoff,
                valuation_rows,
                fresh_rows,
            )
        except Exception as exc:
            source_errors.append((_COMPONENT_VALUATION_HISTORY, _error_label(exc)))

    result = _calculate_evidence(
        normalized_code,
        cutoff,
        bars,
        valuation_rows,
        cache_hit=False,
        cache_diagnostic=diagnostic,
    )
    if source_errors:
        result = replace(
            result,
            reason="source_unavailable:"
            + "|".join(f"{error};component={component}" for component, error in source_errors),
        )

    if cache is None or (not bars and not valuation_rows):
        return result
    payload = {
        "contract": _cache_contract(normalized_code, cutoff),
        "weekly_bars": _serialize_bars(bars),
        "valuation_rows": valuation_rows,
        "component_checked_as_of": {
            component: checked_dates[component].isoformat() for component in _QUALITY_HISTORY_COMPONENTS
        },
    }
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
        saved_state = "complete" if result.available else "partial"
        return replace(result, cache_diagnostic=f"{diagnostic};saved_{saved_state}")
    except SafeCacheConflict:
        winner = cache.load()
        if winner.hit:
            try:
                return replace(_from_cache(winner.value, normalized_code, cutoff), cache_diagnostic="race_winner")
            except QualityHistoryError:
                pass
        return replace(result, cache_diagnostic=f"{diagnostic};write_conflict")
    except SafeCacheError as exc:
        return replace(result, cache_diagnostic=f"{diagnostic};write_failed:{_error_label(exc)}")


def load_quality_history_cache_batch_state(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = 16,
    progress_cb: Any = None,
    cache_dir: str | Path = QUALITY_HISTORY_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Load reusable histories and identify partial records whose retry is due."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("market-history cache requests must be a sequence")
    if len(requests_) > 6_000:
        raise ValueError("market-history cache batch exceeds the company limit")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be between 1 and 32")
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    cache_root = Path(cache_dir)
    prepared: list[tuple[str, date]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {"code", "as_of"}:
            raise ValueError("market-history cache request shape is invalid")
        code = _normalise_code(request.get("code"))
        as_of = _parse_as_of(request.get("as_of"))
        if code in seen:
            raise ValueError(f"market-history cache batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, as_of))
    prepared.sort(key=lambda item: item[0])
    if not prepared:
        return {}, ()

    results: dict[str, dict[str, Any]] = {}
    refresh_due: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(int(max_workers), len(prepared))) as executor:
        future_to_item = {
            executor.submit(
                _load_reusable_capture,
                code,
                as_of,
                cache_dir=cache_root,
                cache_ttl_seconds=cache_ttl_seconds,
            ): (code, as_of)
            for code, as_of in prepared
        }
        completed = 0
        for future in as_completed(future_to_item):
            code, as_of = future_to_item[future]
            capture = future.result()
            if capture is not None:
                results[code] = capture.evidence.to_dict()
                if _capture_refresh_components(capture, as_of):
                    refresh_due.add(code)
            completed += 1
            if progress_cb:
                progress_cb(completed, len(prepared))
    ordered_results = {code: results[code] for code, _ in prepared if code in results}
    return ordered_results, tuple(code for code, _ in prepared if code in refresh_due)


def load_quality_history_cache_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = 16,
    progress_cb: Any = None,
    cache_dir: str | Path = QUALITY_HISTORY_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Load all currently reusable histories without performing network I/O."""

    results, _refresh_due = load_quality_history_cache_batch_state(
        requests_,
        max_workers=max_workers,
        progress_cb=progress_cb,
        cache_dir=cache_dir,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    return results


def fetch_quality_history_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = 8,
    progress_cb: Any = None,
) -> dict[str, dict[str, Any]]:
    """Fetch a deterministic, bounded batch for preflight-approved candidates."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("market-history batch requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("market-history batch exceeds the company limit")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be between 1 and 32")
    prepared: list[tuple[str, str]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {"code", "as_of"}:
            raise ValueError("market-history batch request shape is invalid")
        code = _normalise_code(request.get("code"))
        as_of = _parse_as_of(request.get("as_of")).isoformat()
        if code in seen:
            raise ValueError(f"market-history batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, as_of))
    prepared.sort()
    if not prepared:
        return {}

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(int(max_workers), len(prepared))) as executor:
        future_to_code = {executor.submit(fetch_quality_history, code, as_of): code for code, as_of in prepared}
        completed = 0
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                result = future.result()
            except Exception as exc:  # Defensive: one source bug must not erase the market generation.
                result = QualityHistoryEvidence(
                    available=False,
                    code=code,
                    as_of=next(as_of for candidate, as_of in prepared if candidate == code),
                    model_id=MODEL_ID,
                    shareholder_return={"available": False, "reason": "worker_failure"},
                    valuation_history={"available": False, "reason": "worker_failure"},
                    sources=[],
                    cache_hit=False,
                    cache_diagnostic="",
                    reason=f"worker_failure:{_error_label(exc)}",
                )
            results[code] = result.to_dict()
            completed += 1
            if progress_cb:
                progress_cb(completed, len(prepared))
    return {code: results[code] for code, _ in prepared}


__all__ = [
    "EASTMONEY_VALUATION_ENDPOINT",
    "MODEL_ID",
    "PARTIAL_STRUCTURAL_RETRY_DAYS",
    "PARTIAL_TRANSIENT_RETRY_DAYS",
    "QualityHistoryError",
    "QualityHistoryEvidence",
    "fetch_quality_history",
    "fetch_quality_history_batch",
    "load_quality_history_cache_batch",
    "load_quality_history_cache_batch_state",
    "replay_valuation_distribution",
]
