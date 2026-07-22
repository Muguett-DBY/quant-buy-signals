"""Repository-pinned Shanghai/Shenzhen trading-calendar helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TRADING_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "tools" / "china_a_share_trading_calendar.json"
_OFFICIAL_EXCHANGE_HOSTS = {
    "SSE": "www.sse.com.cn",
    "SZSE": "www.szse.cn",
}


class TradingCalendarError(ValueError):
    """Raised when the pinned exchange calendar cannot prove a session."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TradingCalendarError(f"duplicate trading-calendar key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise TradingCalendarError(f"invalid trading-calendar number: {value}")


@lru_cache(maxsize=1)
def _calendar_years() -> dict[int, tuple[tuple[date, date], ...]]:
    try:
        payload = json.loads(
            TRADING_CALENDAR_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TradingCalendarError("cannot read the pinned A-share trading calendar") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TradingCalendarError("unsupported A-share trading-calendar schema")
    if payload.get("timezone") != "Asia/Shanghai" or not isinstance(payload.get("years"), dict):
        raise TradingCalendarError("invalid A-share trading-calendar identity")

    result: dict[int, tuple[tuple[date, date], ...]] = {}
    for raw_year, raw_contract in payload["years"].items():
        if not isinstance(raw_year, str) or not raw_year.isascii() or not raw_year.isdigit() or len(raw_year) != 4:
            raise TradingCalendarError("invalid A-share trading-calendar year")
        year = int(raw_year)
        if not isinstance(raw_contract, dict) or not isinstance(raw_contract.get("sources"), list):
            raise TradingCalendarError(f"invalid A-share trading-calendar contract for {year}")
        sources = raw_contract["sources"]
        source_exchanges: set[str] = set()
        valid_sources = len(sources) == len(_OFFICIAL_EXCHANGE_HOSTS)
        for source in sources:
            if not isinstance(source, dict):
                valid_sources = False
                continue
            exchange = source.get("exchange")
            url = source.get("url")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (
                exchange not in _OFFICIAL_EXCHANGE_HOSTS
                or exchange in source_exchanges
                or parsed is None
                or parsed.scheme != "https"
                or parsed.hostname != _OFFICIAL_EXCHANGE_HOSTS[exchange]
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
            ):
                valid_sources = False
                continue
            source_exchanges.add(exchange)
        if not valid_sources or source_exchanges != set(_OFFICIAL_EXCHANGE_HOSTS):
            raise TradingCalendarError(f"missing exchange provenance for {year}")
        periods = raw_contract.get("closure_periods")
        if not isinstance(periods, list):
            raise TradingCalendarError(f"missing closure periods for {year}")
        closures: list[tuple[date, date]] = []
        for period in periods:
            if not isinstance(period, dict) or set(period) != {"name", "start", "end"}:
                raise TradingCalendarError(f"invalid closure period for {year}")
            try:
                start = date.fromisoformat(period["start"])
                end = date.fromisoformat(period["end"])
            except (TypeError, ValueError) as exc:
                raise TradingCalendarError(f"invalid closure date for {year}") from exc
            if start.year != year or end.year != year or end < start:
                raise TradingCalendarError(f"invalid closure range for {year}")
            if closures and start <= closures[-1][1]:
                raise TradingCalendarError(f"overlapping closure periods for {year}")
            closures.append((start, end))
        result[year] = tuple(closures)
    if not result:
        raise TradingCalendarError("A-share trading calendar has no supported years")
    return result


def is_a_share_trading_day(session: date) -> bool:
    if not isinstance(session, date) or isinstance(session, datetime):
        raise TypeError("session must be a date")
    closures = _calendar_years().get(session.year)
    if closures is None:
        raise TradingCalendarError(f"A-share trading calendar does not cover {session.year}")
    return session.weekday() < 5 and not any(start <= session <= end for start, end in closures)


def a_share_trading_days_ytd(session: date) -> int:
    """Return actual pinned exchange sessions from January 1 through ``session``."""

    if not isinstance(session, date) or isinstance(session, datetime):
        raise TypeError("session must be a date")
    current = date(session.year, 1, 1)
    count = 0
    while current <= session:
        count += int(is_a_share_trading_day(current))
        current += timedelta(days=1)
    return count


__all__ = [
    "TRADING_CALENDAR_PATH",
    "TradingCalendarError",
    "a_share_trading_days_ytd",
    "is_a_share_trading_day",
]
