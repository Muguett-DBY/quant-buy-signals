from datetime import date
import json

import pytest

from data import trading_calendar
from data.trading_calendar import TradingCalendarError, a_share_trading_days_ytd, is_a_share_trading_day


def test_pinned_calendar_excludes_exchange_holidays_not_just_weekends():
    assert is_a_share_trading_day(date(2026, 2, 16)) is False
    assert is_a_share_trading_day(date(2026, 2, 24)) is True
    assert a_share_trading_days_ytd(date(2026, 2, 24)) == 31


def test_calendar_fails_closed_for_an_unpublished_year():
    with pytest.raises(TradingCalendarError, match="does not cover 2027"):
        a_share_trading_days_ytd(date(2027, 1, 4))


@pytest.mark.parametrize(
    "sources",
    [
        [{"exchange": "SSE", "url": "https://www.sse.com.cn/official-notice"}],
        [
            {"exchange": "SSE", "url": "https://www.sse.com.cn/notice-one"},
            {"exchange": "SSE", "url": "https://www.sse.com.cn/notice-two"},
        ],
        [
            {"exchange": "SSE", "url": "https://www.sse.com.cn/official-notice"},
            {"exchange": "SZSE", "url": "https://example.com/forged-notice"},
        ],
    ],
)
def test_calendar_requires_unique_official_provenance_from_both_exchanges(monkeypatch, tmp_path, sources):
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timezone": "Asia/Shanghai",
                "years": {"2026": {"sources": sources, "closure_periods": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(trading_calendar, "TRADING_CALENDAR_PATH", path)
    trading_calendar._calendar_years.cache_clear()
    try:
        with pytest.raises(TradingCalendarError, match="missing exchange provenance"):
            is_a_share_trading_day(date(2026, 1, 5))
    finally:
        trading_calendar._calendar_years.cache_clear()
