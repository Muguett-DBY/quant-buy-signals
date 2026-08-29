"""Deterministic weekly market-beta estimation with an auditable source cache.

The module is intentionally independent from the scoring, DCF and UI layers.
It fetches Tencent forward-adjusted weekly closes for one Shanghai/Shenzhen
A-share, aligns them with the CSI 300, and estimates a fixed 156-return beta.
Network and data failures are returned as structured unavailable results;
they are never converted to a zero beta.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import numpy as np
import requests

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS, REQUEST_TIMEOUT
from data.as_of import shanghai_today
from data.cache import SafeCacheConflict, SafeCacheError, SafeFileCache
from data.provider_http import RequestRateLimiter, is_transient_request_error, retry_delay_seconds, thread_local_session


TENCENT_HISTORY_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_SOURCE = "Tencent Finance"
TENCENT_SOURCE_ID = "tencent_finance_qfq_weekly"
CSI300_CODE = "000300"
CSI300_SYMBOL = "sh000300"
LOOKBACK_WEEKLY_RETURNS = 156
REQUIRED_WEEKLY_PRICES = LOOKBACK_WEEKLY_RETURNS + 1
WINSOR_LOWER_QUANTILE = 0.01
WINSOR_UPPER_QUANTILE = 0.99
BLUME_RAW_WEIGHT = 2.0 / 3.0
RETURN_METHOD = "simple_weekly_close_to_close"

_FETCH_BAR_LIMIT = 220
_CACHE_SCHEMA_VERSION = 1
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
TENCENT_REQUEST_INTERVAL_SECONDS = 0.25
_TENCENT_RATE_LIMITER = RequestRateLimiter(TENCENT_REQUEST_INTERVAL_SECONDS)
_A_SHARE_CODE = re.compile(r"^[0-9]{6}$")
_TENCENT_SYMBOL = re.compile(r"^(?:sh|sz)[0-9]{6}$")
_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com/",
    "Accept": "application/json,text/plain,*/*",
}

DEFAULT_MARKET_HISTORY_CACHE_DIR = CACHE_DIRECTORY / "market_history"


class MarketHistoryError(RuntimeError):
    """A market-history transport or payload contract failed."""


@dataclass(frozen=True, order=True)
class WeeklyClose:
    """One normalized weekly close."""

    trade_date: date
    close: float

    def __post_init__(self) -> None:
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise TypeError("trade_date must be a date without a time component")
        if isinstance(self.close, bool):
            raise TypeError("weekly close must be numeric")
        try:
            value = float(self.close)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TypeError("weekly close must be numeric") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("weekly close must be finite and positive")
        object.__setattr__(self, "close", value)


@dataclass(frozen=True)
class MarketBetaEstimate:
    """Structured result for a fixed weekly market-beta estimate."""

    available: bool
    code: str
    benchmark_code: str
    as_of: str
    source: str
    source_url: str
    start_date: str | None
    end_date: str | None
    price_observations: int
    sample_size: int
    raw_beta: float | None
    blume_beta: float | None
    r_squared: float | None
    lookback_weeks: int
    winsor_lower_quantile: float
    winsor_upper_quantile: float
    return_method: str
    cache_key: str
    cache_hit: bool
    cache_diagnostic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WeeklyHistoryAdapter(Protocol):
    """Adapter boundary used by the orchestrator and fixed-sequence tests."""

    def fetch_weekly_closes(
        self,
        symbol: str,
        as_of: date,
        *,
        require_forward_adjusted: bool,
    ) -> list[WeeklyClose]: ...


def _error_label(exc: BaseException, *, limit: int = 180) -> str:
    message = " ".join(str(exc).split())
    label = type(exc).__name__
    return f"{label}:{message[:limit]}" if message else label


def _parse_as_of(value: date | str) -> date:
    if isinstance(value, datetime):
        raise TypeError("as_of must be a date or YYYY-MM-DD string")
    if isinstance(value, date):
        parsed = value
    else:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("as_of must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("as_of must be a valid calendar date") from exc
    if parsed > shanghai_today():
        raise ValueError("as_of cannot be in the future")
    return parsed


def _normalise_stock_code(value: Any) -> str:
    code = str(value).strip()
    if not _A_SHARE_CODE.fullmatch(code):
        raise ValueError("stock code must be exactly six digits")
    # This project intentionally excludes Beijing Stock Exchange securities.
    if not code.startswith(("0", "3", "6")):
        raise ValueError("only Shanghai/Shenzhen A-share codes are supported")
    return code


def _tencent_stock_symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _cache_key(code: str, as_of: date) -> str:
    return f"{TENCENT_SOURCE_ID}:v{_CACHE_SCHEMA_VERSION}:{code}:{as_of.isoformat()}"


def _cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{TENCENT_SOURCE_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _read_bounded_response(response: Any) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        text = str(declared).strip()
        if not re.fullmatch(r"0|[1-9][0-9]*", text):
            raise MarketHistoryError("Tencent response has invalid Content-Length")
        if int(text) > _MAX_RESPONSE_BYTES:
            raise MarketHistoryError("Tencent response exceeds byte limit")

    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        chunks: list[bytes] = []
        received = 0
        for chunk in iterator(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise MarketHistoryError("Tencent response yielded non-byte content")
            received += len(chunk)
            if received > _MAX_RESPONSE_BYTES:
                raise MarketHistoryError("Tencent response exceeds byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    content = getattr(response, "content", None)
    if not isinstance(content, bytes):
        raise MarketHistoryError("Tencent response does not expose a byte body")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise MarketHistoryError("Tencent response exceeds byte limit")
    return content


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MarketHistoryError(f"Tencent JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _decode_tencent_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                MarketHistoryError(f"Tencent JSON contains a non-finite number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketHistoryError("Tencent response is not valid UTF-8 JSON") from exc


def _validate_final_https_url(value: Any) -> None:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MarketHistoryError("Tencent history redirected outside credential-free HTTPS")


def _finite_payload_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise MarketHistoryError(f"Tencent {field} is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketHistoryError(f"Tencent {field} is not numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "finite and positive" if positive else "finite"
        raise MarketHistoryError(f"Tencent {field} must be {qualifier}")
    return number


def _parse_tencent_weekly_payload(
    payload: Any,
    symbol: str,
    as_of: date,
    *,
    require_forward_adjusted: bool,
    stock_adjustment: str = "qfq",
) -> list[WeeklyClose]:
    if not isinstance(payload, Mapping) or payload.get("code") != 0:
        raise MarketHistoryError("Tencent payload reports a non-zero status")
    data = payload.get("data")
    security = data.get(symbol) if isinstance(data, Mapping) else None
    if not isinstance(security, Mapping):
        raise MarketHistoryError("Tencent payload is missing the requested symbol")

    if require_forward_adjusted:
        rows = security.get(f"{stock_adjustment}week")
        if not isinstance(rows, list):
            label = "forward-adjusted" if stock_adjustment == "qfq" else "backward-adjusted"
            raise MarketHistoryError(f"Tencent payload is missing {label} weekly rows")
    else:
        rows = security.get("qfqweek")
        if not isinstance(rows, list):
            rows = security.get("week")
        if not isinstance(rows, list):
            raise MarketHistoryError("Tencent payload is missing benchmark weekly rows")

    bars: list[WeeklyClose] = []
    previous_date: date | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise MarketHistoryError("Tencent weekly row has an invalid shape")
        try:
            trade_date = date.fromisoformat(str(row[0]))
        except ValueError as exc:
            raise MarketHistoryError("Tencent weekly row has an invalid date") from exc
        if previous_date is not None and trade_date <= previous_date:
            raise MarketHistoryError("Tencent weekly dates are not strictly increasing")
        previous_date = trade_date

        open_price = _finite_payload_number(row[1], "weekly open", positive=True)
        close_price = _finite_payload_number(row[2], "weekly close", positive=True)
        high_price = _finite_payload_number(row[3], "weekly high", positive=True)
        low_price = _finite_payload_number(row[4], "weekly low", positive=True)
        volume = _finite_payload_number(row[5], "weekly volume")
        if volume < 0 or high_price < low_price:
            raise MarketHistoryError("Tencent weekly OHLC/volume relationship is invalid")
        tolerance = max(1e-8, high_price * 1e-8)
        if (
            open_price < low_price - tolerance
            or open_price > high_price + tolerance
            or close_price < low_price - tolerance
            or close_price > high_price + tolerance
        ):
            raise MarketHistoryError("Tencent weekly open/close falls outside low/high")
        if trade_date <= as_of:
            bars.append(WeeklyClose(trade_date, close_price))

    if not bars:
        raise MarketHistoryError("Tencent returned no weekly rows on or before as_of")
    return bars


class TencentWeeklyHistoryAdapter:
    """Strict adapter for Tencent's weekly K-line endpoint."""

    def __init__(
        self,
        *,
        http_client: Any = requests,
        timeout: int = REQUEST_TIMEOUT,
        retries: int = 3,
        retry_delay: float = 0.5,
        bar_limit: int = _FETCH_BAR_LIMIT,
        stock_adjustment: str = "qfq",
        rate_limiter: Any = _TENCENT_RATE_LIMITER,
    ):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ValueError("timeout must be positive")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
            raise ValueError("retries must be at least one")
        if (
            isinstance(retry_delay, bool)
            or not isinstance(retry_delay, (int, float))
            or not math.isfinite(float(retry_delay))
            or float(retry_delay) < 0
        ):
            raise ValueError("retry_delay must be finite and non-negative")
        if isinstance(bar_limit, bool) or not isinstance(bar_limit, int) or not 1 <= bar_limit <= 1_000:
            raise ValueError("bar_limit must be between 1 and 1000")
        if stock_adjustment not in {"qfq", "hfq"}:
            raise ValueError("stock_adjustment must be qfq or hfq")
        self.http_client = http_client
        self.timeout = float(timeout)
        self.retries = retries
        self.retry_delay = float(retry_delay)
        self.bar_limit = bar_limit
        self.stock_adjustment = stock_adjustment
        self.rate_limiter = rate_limiter

    def fetch_weekly_closes(
        self,
        symbol: str,
        as_of: date,
        *,
        require_forward_adjusted: bool,
    ) -> list[WeeklyClose]:
        if not isinstance(symbol, str) or not _TENCENT_SYMBOL.fullmatch(symbol):
            raise ValueError("Tencent symbol must be sh/sz plus six digits")
        if isinstance(as_of, datetime) or not isinstance(as_of, date):
            raise TypeError("as_of must be a date")

        request_value = f"{symbol},week,,{as_of.isoformat()},{self.bar_limit},{self.stock_adjustment}"
        http_client = thread_local_session() if self.http_client is requests else self.http_client
        last_error: Exception | None = None
        for attempt in range(self.retries):
            response = None
            delay = 0.0
            transient = False
            try:
                self.rate_limiter.acquire()
                response = http_client.get(
                    TENCENT_HISTORY_ENDPOINT,
                    params={"param": request_value},
                    headers=_TENCENT_HEADERS,
                    timeout=self.timeout,
                    stream=True,
                )
                response.raise_for_status()
                _validate_final_https_url(getattr(response, "url", TENCENT_HISTORY_ENDPOINT))
                raw = _read_bounded_response(response)
                payload = _decode_tencent_json(raw)
                return _parse_tencent_weekly_payload(
                    payload,
                    symbol,
                    as_of,
                    require_forward_adjusted=require_forward_adjusted,
                    stock_adjustment=self.stock_adjustment,
                )
            except MarketHistoryError:
                raise
            except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
                last_error = exc
                transient = is_transient_request_error(exc, response)
                delay = retry_delay_seconds(
                    response,
                    attempt=attempt,
                    base_seconds=self.retry_delay,
                )
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if not transient or attempt + 1 >= self.retries:
                break
            time.sleep(delay)
        raise MarketHistoryError(f"Tencent weekly fetch failed: {_error_label(last_error)}") from last_error


def _normalise_bars(values: Iterable[WeeklyClose], as_of: date) -> list[WeeklyClose]:
    bars: list[WeeklyClose] = []
    for value in values:
        if not isinstance(value, WeeklyClose):
            raise TypeError("weekly close sequences must contain WeeklyClose values")
        if value.trade_date <= as_of:
            bars.append(value)
    bars.sort(key=lambda item: item.trade_date)
    dates = [item.trade_date for item in bars]
    if len(dates) != len(set(dates)):
        raise ValueError("weekly close sequence contains duplicate dates")
    return bars


def _unavailable_estimate(
    *,
    code: str,
    as_of: date,
    source: str,
    source_url: str,
    reason: str,
    cache_key: str,
    cache_hit: bool = False,
    cache_diagnostic: str = "",
    aligned_dates: list[date] | None = None,
) -> MarketBetaEstimate:
    dates = aligned_dates or []
    return MarketBetaEstimate(
        available=False,
        code=code,
        benchmark_code=CSI300_CODE,
        as_of=as_of.isoformat(),
        source=source,
        source_url=source_url,
        start_date=dates[0].isoformat() if dates else None,
        end_date=dates[-1].isoformat() if dates else None,
        price_observations=len(dates),
        sample_size=max(0, len(dates) - 1),
        raw_beta=None,
        blume_beta=None,
        r_squared=None,
        lookback_weeks=LOOKBACK_WEEKLY_RETURNS,
        winsor_lower_quantile=WINSOR_LOWER_QUANTILE,
        winsor_upper_quantile=WINSOR_UPPER_QUANTILE,
        return_method=RETURN_METHOD,
        cache_key=cache_key,
        cache_hit=cache_hit,
        cache_diagnostic=cache_diagnostic,
        reason=reason,
    )


def calculate_weekly_market_beta(
    stock_closes: Iterable[WeeklyClose],
    benchmark_closes: Iterable[WeeklyClose],
    *,
    code: str,
    as_of: date | str,
    source: str = TENCENT_SOURCE,
    source_url: str = TENCENT_HISTORY_ENDPOINT,
    cache_key: str | None = None,
) -> MarketBetaEstimate:
    """Estimate beta from exactly 156 aligned, independently winsorized returns.

    Returns are simple weekly close-to-close returns.  The stock and CSI 300
    return columns are independently clipped at their empirical 1st and 99th
    percentiles using NumPy's deterministic linear quantile interpolation.
    Raw beta is OLS covariance/variance with an intercept; the Blume adjustment
    is ``2/3 * raw_beta + 1/3``.
    """

    normalized_code = _normalise_stock_code(code)
    cutoff = _parse_as_of(as_of)
    key = cache_key or _cache_key(normalized_code, cutoff)
    stock = _normalise_bars(stock_closes, cutoff)
    benchmark = _normalise_bars(benchmark_closes, cutoff)
    stock_by_date = {item.trade_date: item.close for item in stock}
    benchmark_by_date = {item.trade_date: item.close for item in benchmark}
    aligned_dates = sorted(stock_by_date.keys() & benchmark_by_date.keys())
    if len(aligned_dates) < REQUIRED_WEEKLY_PRICES:
        return _unavailable_estimate(
            code=normalized_code,
            as_of=cutoff,
            source=source,
            source_url=source_url,
            reason=f"insufficient_aligned_prices:{len(aligned_dates)}/{REQUIRED_WEEKLY_PRICES}",
            cache_key=key,
            aligned_dates=aligned_dates,
        )

    sample_dates = aligned_dates[-REQUIRED_WEEKLY_PRICES:]
    stock_prices = np.asarray([stock_by_date[item] for item in sample_dates], dtype=np.float64)
    benchmark_prices = np.asarray([benchmark_by_date[item] for item in sample_dates], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        stock_returns = stock_prices[1:] / stock_prices[:-1] - 1.0
        benchmark_returns = benchmark_prices[1:] / benchmark_prices[:-1] - 1.0
    if not (np.all(np.isfinite(stock_returns)) and np.all(np.isfinite(benchmark_returns))):
        return _unavailable_estimate(
            code=normalized_code,
            as_of=cutoff,
            source=source,
            source_url=source_url,
            reason="non_finite_weekly_returns",
            cache_key=key,
            aligned_dates=sample_dates,
        )

    return_matrix = np.column_stack((stock_returns, benchmark_returns))
    lower = np.quantile(return_matrix, WINSOR_LOWER_QUANTILE, axis=0, method="linear")
    upper = np.quantile(return_matrix, WINSOR_UPPER_QUANTILE, axis=0, method="linear")
    winsorized = np.clip(return_matrix, lower, upper)
    stock_w = winsorized[:, 0]
    benchmark_w = winsorized[:, 1]
    stock_centered = stock_w - float(np.mean(stock_w))
    benchmark_centered = benchmark_w - float(np.mean(benchmark_w))
    market_sum_squares = float(np.dot(benchmark_centered, benchmark_centered))
    stock_sum_squares = float(np.dot(stock_centered, stock_centered))
    scale = max(float(np.dot(benchmark_w, benchmark_w)), 1.0)
    if market_sum_squares <= np.finfo(np.float64).eps * scale:
        return _unavailable_estimate(
            code=normalized_code,
            as_of=cutoff,
            source=source,
            source_url=source_url,
            reason="zero_benchmark_return_variance",
            cache_key=key,
            aligned_dates=sample_dates,
        )
    if stock_sum_squares <= np.finfo(np.float64).eps * max(float(np.dot(stock_w, stock_w)), 1.0):
        return _unavailable_estimate(
            code=normalized_code,
            as_of=cutoff,
            source=source,
            source_url=source_url,
            reason="zero_stock_return_variance",
            cache_key=key,
            aligned_dates=sample_dates,
        )

    raw_beta = float(np.dot(stock_centered, benchmark_centered) / market_sum_squares)
    intercept = float(np.mean(stock_w) - raw_beta * np.mean(benchmark_w))
    fitted = intercept + raw_beta * benchmark_w
    residual_sum_squares = float(np.dot(stock_w - fitted, stock_w - fitted))
    r_squared = 1.0 - residual_sum_squares / stock_sum_squares
    # OLS with an intercept is bounded to [0, 1]; clamp only floating-point dust.
    r_squared = min(1.0, max(0.0, float(r_squared)))
    blume_beta = BLUME_RAW_WEIGHT * raw_beta + (1.0 - BLUME_RAW_WEIGHT)
    if not all(math.isfinite(value) for value in (raw_beta, blume_beta, r_squared)):
        return _unavailable_estimate(
            code=normalized_code,
            as_of=cutoff,
            source=source,
            source_url=source_url,
            reason="non_finite_beta_result",
            cache_key=key,
            aligned_dates=sample_dates,
        )

    return MarketBetaEstimate(
        available=True,
        code=normalized_code,
        benchmark_code=CSI300_CODE,
        as_of=cutoff.isoformat(),
        source=source,
        source_url=source_url,
        start_date=sample_dates[0].isoformat(),
        end_date=sample_dates[-1].isoformat(),
        price_observations=REQUIRED_WEEKLY_PRICES,
        sample_size=LOOKBACK_WEEKLY_RETURNS,
        raw_beta=raw_beta,
        blume_beta=float(blume_beta),
        r_squared=r_squared,
        lookback_weeks=LOOKBACK_WEEKLY_RETURNS,
        winsor_lower_quantile=WINSOR_LOWER_QUANTILE,
        winsor_upper_quantile=WINSOR_UPPER_QUANTILE,
        return_method=RETURN_METHOD,
        cache_key=key,
        cache_hit=False,
        cache_diagnostic="",
        reason="",
    )


def _cache_contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "source_id": TENCENT_SOURCE_ID,
        "source_url": TENCENT_HISTORY_ENDPOINT,
        "code": code,
        "benchmark_code": CSI300_CODE,
        "as_of": as_of.isoformat(),
        "period": "weekly",
        "stock_adjustment": "qfq",
        "benchmark_adjustment": "index_unadjusted",
        "lookback_returns": LOOKBACK_WEEKLY_RETURNS,
        "return_method": RETURN_METHOD,
        "winsor_quantiles": [WINSOR_LOWER_QUANTILE, WINSOR_UPPER_QUANTILE],
        "blume_raw_weight": BLUME_RAW_WEIGHT,
    }


def _serialize_bars(values: Iterable[WeeklyClose]) -> list[dict[str, Any]]:
    return [{"date": item.trade_date.isoformat(), "close": item.close} for item in values]


def _deserialize_bars(value: Any, label: str) -> list[WeeklyClose]:
    if not isinstance(value, list):
        raise MarketHistoryError(f"cached {label} bars are not a list")
    bars: list[WeeklyClose] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"date", "close"}:
            raise MarketHistoryError(f"cached {label} bar has an invalid shape")
        try:
            trade_date = date.fromisoformat(str(item["date"]))
            bars.append(WeeklyClose(trade_date, item["close"]))
        except (TypeError, ValueError) as exc:
            raise MarketHistoryError(f"cached {label} bar is invalid") from exc
    return bars


def _cached_estimate(payload: Any, code: str, as_of: date, cache_key: str) -> MarketBetaEstimate:
    if not isinstance(payload, Mapping) or set(payload) != {"contract", "stock_bars", "benchmark_bars"}:
        raise MarketHistoryError("market-history cache payload has an invalid shape")
    if payload.get("contract") != _cache_contract(code, as_of):
        raise MarketHistoryError("market-history cache contract mismatch")
    stock = _deserialize_bars(payload.get("stock_bars"), "stock")
    benchmark = _deserialize_bars(payload.get("benchmark_bars"), "benchmark")
    result = calculate_weekly_market_beta(
        stock,
        benchmark,
        code=code,
        as_of=as_of,
        cache_key=cache_key,
    )
    if not result.available:
        raise MarketHistoryError(f"cached market history cannot reproduce beta: {result.reason}")
    return replace(result, cache_hit=True, cache_diagnostic="hit")


def estimate_market_beta(
    code: str,
    as_of: date | str,
    *,
    adapter: WeeklyHistoryAdapter | None = None,
    cache_dir: str | Path = DEFAULT_MARKET_HISTORY_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
) -> MarketBetaEstimate:
    """Fetch/cache weekly closes and return a structured beta estimate.

    Cache identity is the normalized stock code plus the explicit ``as_of``
    date.  The cache stores normalized source bars, not just the final beta;
    every hit therefore replays the pure calculation.  ``SafeFileCache``
    supplies a checksummed schema, deterministic gzip metadata, cross-process
    locking and ``os.replace`` atomic publication.
    """

    normalized_code = _normalise_stock_code(code)
    cutoff = _parse_as_of(as_of)
    if isinstance(cache_ttl_seconds, bool) or int(cache_ttl_seconds) < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    key = _cache_key(normalized_code, cutoff)
    history_adapter = adapter or TencentWeeklyHistoryAdapter()

    cache: SafeFileCache | None = None
    initial_load = None
    cache_diagnostic = "disabled"
    if use_cache:
        root = Path(cache_dir)
        cache = SafeFileCache(
            _cache_path(normalized_code, cutoff, root),
            schema_version=_CACHE_SCHEMA_VERSION,
            ttl=int(cache_ttl_seconds),
            max_uncompressed_bytes=_MAX_RESPONSE_BYTES,
        )
        initial_load = cache.load()
        if initial_load.hit:
            try:
                return _cached_estimate(initial_load.value, normalized_code, cutoff, key)
            except MarketHistoryError as exc:
                cache_diagnostic = f"invalid_hit:{_error_label(exc)}"
        else:
            cache_diagnostic = f"miss:{initial_load.reason}"

    try:
        stock_bars = history_adapter.fetch_weekly_closes(
            _tencent_stock_symbol(normalized_code),
            cutoff,
            require_forward_adjusted=True,
        )
        benchmark_bars = history_adapter.fetch_weekly_closes(
            CSI300_SYMBOL,
            cutoff,
            require_forward_adjusted=False,
        )
        result = calculate_weekly_market_beta(
            stock_bars,
            benchmark_bars,
            code=normalized_code,
            as_of=cutoff,
            cache_key=key,
        )
    except Exception as exc:
        return _unavailable_estimate(
            code=normalized_code,
            as_of=cutoff,
            source=TENCENT_SOURCE,
            source_url=TENCENT_HISTORY_ENDPOINT,
            reason=f"source_unavailable:{_error_label(exc)}",
            cache_key=key,
            cache_diagnostic=cache_diagnostic,
        )

    result = replace(result, cache_diagnostic=cache_diagnostic)
    if not result.available or cache is None:
        return result

    payload = {
        "contract": _cache_contract(normalized_code, cutoff),
        "stock_bars": _serialize_bars(stock_bars),
        "benchmark_bars": _serialize_bars(benchmark_bars),
    }
    expected_hash = None
    if initial_load is not None and isinstance(initial_load.metadata, Mapping):
        candidate = initial_load.metadata.get("payload_sha256")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            expected_hash = candidate
    try:
        cache.compare_and_swap(
            payload,
            expected_payload_sha256=expected_hash,
            allow_replace_invalid=True,
        )
        return replace(result, cache_diagnostic=f"{cache_diagnostic};saved")
    except SafeCacheConflict:
        winner = cache.load()
        if winner.hit:
            try:
                cached = _cached_estimate(winner.value, normalized_code, cutoff, key)
                return replace(cached, cache_diagnostic="race_winner")
            except MarketHistoryError:
                pass
        return replace(result, cache_diagnostic=f"{cache_diagnostic};write_conflict")
    except SafeCacheError as exc:
        return replace(result, cache_diagnostic=f"{cache_diagnostic};write_failed:{_error_label(exc)}")


__all__ = [
    "BLUME_RAW_WEIGHT",
    "CSI300_CODE",
    "DEFAULT_MARKET_HISTORY_CACHE_DIR",
    "LOOKBACK_WEEKLY_RETURNS",
    "MarketBetaEstimate",
    "MarketHistoryError",
    "RETURN_METHOD",
    "TENCENT_HISTORY_ENDPOINT",
    "TENCENT_SOURCE",
    "TencentWeeklyHistoryAdapter",
    "WINSOR_LOWER_QUANTILE",
    "WINSOR_UPPER_QUANTILE",
    "WeeklyClose",
    "WeeklyHistoryAdapter",
    "calculate_weekly_market_beta",
    "estimate_market_beta",
]
