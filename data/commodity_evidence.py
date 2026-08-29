"""Sina futures commodity-cycle evidence for the Type 5 strong-cycle gate.

Type 5 requires an independently observable strong-cycle attribute (5a)
before the bottom model applies.  Direct commodity industries whose
financial volatility alone is not conclusive (``现有财务波动不足以确认强周期
属性``) can be confirmed by the actual commodity price series.  This
adapter fetches the continuous main-contract daily closes from Sina
Futures (free, no auth, data to the latest session), computes the
five-year peak-to-trough swing as a reproducible 0..10 cycle-attribute
score, and binds a dated, code-bound evidence record per company so the
strict ``_type5_external_score`` path accepts it.

Scores are deliberately conservative and independent of the current
price level: a high swing confirms the industry is strongly cyclical
(even if the price is currently elevated); the bottom decision stays
with Type 5's own bottom-signal gate, which this adapter never touches.
"""

from __future__ import annotations

import json
import hashlib
import math
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from data.cache import SafeFileCache
from data.as_of import shanghai_today
from data.provider_http import read_bounded_response_bytes, thread_local_session

# One representative main-contract commodity per direct cyclical industry.
INDUSTRY_COMMODITY_SYMBOLS: dict[str, str] = {
    "STEEL": "RB0",  # 螺纹钢
    "NONFERROUS": "CU0",  # 沪铜
    "CHEMICAL": "MA0",  # 甲醇
    "BUILDING_MATERIAL": "FG0",  # 玻璃（水泥无期货，玻璃为建材代理）
    "OIL_GAS": "SC0",  # 原油
    "COAL": "JM0",  # 焦煤（动力煤主力连续已停，用焦煤）
}

SINA_DAILY_KLINE_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/InnerFuturesNewService.getDailyKLine"
)
SINA_REFERER = "https://finance.sina.com.cn/"
COMMODITY_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "commodity_cycle"
COMMODITY_CACHE_MODEL_ID = "commodity-cycle-sina-v2"
COMMODITY_CACHE_SCHEMA_VERSION = 2
COMMODITY_CACHE_TTL_SECONDS = 18 * 3600  # price data refresh daily after close
REQUEST_TIMEOUT = (15, 30)
REQUEST_ATTEMPTS = 3
REQUEST_BACKOFF_SECONDS = 3.0
# Five-year lookback approximates a full commodity cycle (250 trading days
# per year).
CYCLE_LOOKBACK_TRADING_DAYS = 1250
MIN_CYCLE_OBSERVATIONS = 500
MAX_KLINE_RESPONSE_BYTES = 3 * 1024 * 1024


class CommodityCycleError(RuntimeError):
    """A commodity source, cache or evidence contract failed."""


def _source_url(symbol: str) -> str:
    return f"{SINA_DAILY_KLINE_URL}?{urlencode({'symbol': symbol})}"


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _fetch_kline(symbol: str, *, session: Any = requests) -> tuple[list[dict[str, Any]], str]:
    """Fetch one symbol's daily closes from Sina with bounded retries."""
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = session.get(
                SINA_DAILY_KLINE_URL,
                params={"symbol": symbol},
                headers={"User-Agent": "Mozilla/5.0", "Referer": SINA_REFERER},
                timeout=REQUEST_TIMEOUT,
                stream=True,
            )
            try:
                response.raise_for_status()
                raw = read_bounded_response_bytes(response, MAX_KLINE_RESPONSE_BYTES)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            text = raw.decode("utf-8")
            start = text.find("([")
            end = text.rfind("])")
            if start < 0 or end <= start:
                raise CommodityCycleError(f"sina futures response is not a JSONP array: {symbol}")
            payload = json.loads(text[start + 1 : end + 1])
            if not isinstance(payload, list):
                raise CommodityCycleError(f"sina futures payload is not a list: {symbol}")
            by_date: dict[str, float] = {}
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                close = _finite(item.get("c"))
                trade_date = _parse_iso_date(item.get("d"))
                if close is None or trade_date is None:
                    continue
                if close <= 0 or trade_date > shanghai_today():
                    raise CommodityCycleError(f"sina futures returned an invalid bar: {symbol}")
                key = trade_date.isoformat()
                previous = by_date.get(key)
                if previous is not None and previous != close:
                    raise CommodityCycleError(f"sina futures returned conflicting duplicate bars: {symbol}")
                by_date[key] = close
            rows = [{"date": key, "close": by_date[key]} for key in sorted(by_date)]
            if len(rows) < MIN_CYCLE_OBSERVATIONS:
                raise CommodityCycleError(f"sina futures returned too few bars: {symbol}")
            return rows, hashlib.sha256(raw).hexdigest()
        except (requests.RequestException, CommodityCycleError, ValueError) as exc:
            last_error = exc
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(REQUEST_BACKOFF_SECONDS * (attempt + 1))
    raise CommodityCycleError(f"sina futures fetch failed for {symbol}: {last_error!r}")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _load_cached_kline(symbol: str, cache: SafeFileCache) -> tuple[list[dict[str, Any]], str] | None:
    loaded = cache.load()
    if not loaded.hit:
        return None
    value = loaded.value
    if (
        not isinstance(value, Mapping)
        or value.get("model_id") != COMMODITY_CACHE_MODEL_ID
        or value.get("symbol") != symbol
        or value.get("source_url") != _source_url(symbol)
        or not isinstance(value.get("source_sha256"), str)
        or len(value["source_sha256"]) != 64
        or not isinstance(value.get("bars"), list)
    ):
        return None
    bars = value["bars"]
    previous = ""
    if len(bars) < MIN_CYCLE_OBSERVATIONS:
        return None
    for row in bars:
        if not isinstance(row, Mapping) or set(row) != {"date", "close"}:
            return None
        parsed = _parse_iso_date(row.get("date"))
        close = _finite(row.get("close"))
        key = str(row.get("date") or "")
        if parsed is None or parsed > shanghai_today() or close is None or close <= 0 or key <= previous:
            return None
        previous = key
    return list(bars), value["source_sha256"]


def _save_cached_kline(symbol: str, bars: list[dict[str, Any]], source_sha256: str, cache: SafeFileCache) -> None:
    cache.save(
        {
            "model_id": COMMODITY_CACHE_MODEL_ID,
            "symbol": symbol,
            "source_url": _source_url(symbol),
            "source_sha256": source_sha256,
            "bars": bars,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _cycle_swing_score(closes: Sequence[float]) -> float:
    """Score the five-year peak-to-trough swing on a 0..10 scale.

    A real commodity industry swings more than 40% peak-to-trough over a
    full cycle; less than 25% is not strong-cycle evidence.  The score is
    intentionally independent of the current price level (the bottom gate is
    Type 5's own signal, not this attribute).
    """
    if len(closes) < MIN_CYCLE_OBSERVATIONS:
        return 0.0
    window = closes[-CYCLE_LOOKBACK_TRADING_DAYS:]
    low = min(window)
    high = max(window)
    if low <= 0:
        return 0.0
    swing = (high - low) / low
    if swing >= 1.00:
        return 10.0
    if swing >= 0.80:
        return 9.0
    if swing >= 0.60:
        return 8.0
    if swing >= 0.45:
        return 7.0
    if swing >= 0.30:
        return 5.0
    if swing >= 0.20:
        return 3.0
    return 1.0


def load_commodity_cycle_evidence(
    industry_by_code: Mapping[str, str],
    *,
    as_of: str,
    cache_dir: str | Path = COMMODITY_CACHE_DIR,
    session: Any = requests,
) -> dict[str, dict[str, Any]]:
    """Return dated, code-bound cycle-attribute evidence for direct-cyclical companies.

    One commodity series per industry is fetched once and reused for every
    company in that industry; each company gets its own code-bound evidence
    record so the strict Type 5 validator accepts it.
    """
    cutoff = date.fromisoformat(as_of)
    if session is requests:
        session = thread_local_session()
    by_industry: dict[str, list[str]] = {}
    for code, industry in industry_by_code.items():
        symbol = INDUSTRY_COMMODITY_SYMBOLS.get(str(industry))
        if symbol is not None:
            by_industry.setdefault(str(industry), []).append(code)
    if not by_industry:
        return {}

    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    evidence_by_code: dict[str, dict[str, Any]] = {}
    for industry, codes in sorted(by_industry.items()):
        symbol = INDUSTRY_COMMODITY_SYMBOLS[industry]
        cache = SafeFileCache(
            directory / f"{symbol}.json.gz",
            schema_version=COMMODITY_CACHE_SCHEMA_VERSION,
            ttl=COMMODITY_CACHE_TTL_SECONDS,
        )
        cached = _load_cached_kline(symbol, cache)
        if cached is None:
            bars, source_sha256 = _fetch_kline(symbol, session=session)
            _save_cached_kline(symbol, bars, source_sha256, cache)
        else:
            bars, source_sha256 = cached
        latest = _parse_iso_date(bars[-1]["date"]) if bars else None
        if latest is None or latest > cutoff:
            raise CommodityCycleError(
                f"commodity {symbol} latest session {latest} is after the analysis cutoff {as_of}"
            )
        closes = [float(row["close"]) for row in bars]
        score = _cycle_swing_score(closes)
        swing = (
            (max(closes[-CYCLE_LOOKBACK_TRADING_DAYS:]) - min(closes[-CYCLE_LOOKBACK_TRADING_DAYS:]))
            / min(closes[-CYCLE_LOOKBACK_TRADING_DAYS:])
            if len(closes) >= MIN_CYCLE_OBSERVATIONS and min(closes[-CYCLE_LOOKBACK_TRADING_DAYS:]) > 0
            else None
        )
        for code in codes:
            evidence_id = f"{COMMODITY_CACHE_MODEL_ID}:{symbol}:{code}:{cutoff.strftime('%Y%m%d')}"
            summary = f"{industry}商品{symbol}近五年振幅{swing:.0%}；cycle_attribute={score:.1f};model={COMMODITY_CACHE_MODEL_ID}"
            evidence_by_code[code] = {
                "score": score,
                "evidence": {
                    "source": f"新浪期货主力连续{symbol}",
                    "evidence_id": evidence_id,
                    "as_of": cutoff.isoformat(),
                    "summary": summary,
                    "source_url": _source_url(symbol),
                    "source_sha256": source_sha256,
                },
            }
    return evidence_by_code


__all__ = [
    "COMMODITY_CACHE_DIR",
    "COMMODITY_CACHE_MODEL_ID",
    "CommodityCycleError",
    "INDUSTRY_COMMODITY_SYMBOLS",
    "load_commodity_cycle_evidence",
]
