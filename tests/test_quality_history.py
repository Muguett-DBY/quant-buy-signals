from __future__ import annotations

from datetime import date, timedelta
import json
import math

import pytest

from data.market_history import TencentWeeklyHistoryAdapter, WeeklyClose
from data.quality_history import fetch_quality_history, fetch_quality_history_batch


class _WeeklyAdapter:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def fetch_weekly_closes(self, symbol, as_of, *, require_forward_adjusted):
        self.calls.append((symbol, as_of, require_forward_adjusted))
        return list(self.bars)


class _Response:
    def __init__(self, payload, url="https://datacenter-web.eastmoney.com/api/data/v1/get"):
        self.body = json.dumps(payload, separators=(",", ":")).encode()
        self.url = url
        self.headers = {"Content-Length": str(len(self.body))}
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _weekly_bars(as_of=date(2026, 7, 17), annual_return=0.12):
    first = as_of - timedelta(days=7 * 619)
    return [
        WeeklyClose(
            first + timedelta(days=7 * index),
            100.0 * (1.0 + annual_return) ** ((7 * index) / 365.2425),
        )
        for index in range(620)
    ]


def _valuation_payload(code="600519", as_of=date(2026, 7, 17)):
    start = as_of.replace(year=as_of.year - 5) + timedelta(days=1)
    rows = []
    current = start
    index = 0
    while current <= as_of:
        rows.append(
            {
                "SECURITY_CODE": code,
                "PE_TTM": 10.0 + index / 100.0,
                "PB_MRQ": 2.0 + index / 500.0,
                "TRADE_DATE": current.isoformat() + " 00:00:00",
            }
        )
        current += timedelta(days=1)
        index += 1
    rows[-1]["PE_TTM"] = 11.0
    rows[-1]["PB_MRQ"] = 2.1
    rows.reverse()
    return {"success": True, "message": "ok", "result": {"pages": 1, "count": len(rows), "data": rows}}


def test_quality_history_replays_ten_year_return_and_five_year_valuation():
    adapter = _WeeklyAdapter(_weekly_bars())
    response = _Response(_valuation_payload())
    result = fetch_quality_history(
        "600519",
        "2026-07-17",
        weekly_adapter=adapter,
        valuation_session=_Session(response),
        use_cache=False,
    )

    assert result.available
    assert result.shareholder_return["available"]
    assert math.isclose(result.shareholder_return["cagr"], 0.12, rel_tol=0.01)
    assert result.shareholder_return["start_close_hfq"] > 0
    assert result.valuation_history["available"]
    assert result.valuation_history["pe_observations"] >= 500
    assert result.valuation_history["median_pe_ttm"] > 0
    assert result.valuation_history["median_pb_mrq"] > 0
    assert result.valuation_history["start_delay_days"] <= 62
    assert 0 <= result.valuation_history["pe_percentile"] <= 0.10
    assert response.closed
    assert adapter.calls == [("sh600519", date(2026, 7, 17), True)]


@pytest.mark.parametrize(
    ("sparse_field", "median_key", "percentile_key", "complete_percentile_key"),
    [
        ("PB_MRQ", "median_pb_mrq", "pb_percentile", "pe_percentile"),
        ("PE_TTM", "median_pe_ttm", "pe_percentile", "pb_percentile"),
    ],
)
def test_sparse_valuation_basis_cannot_ride_on_the_other_basis_sample_count(
    sparse_field,
    median_key,
    percentile_key,
    complete_percentile_key,
):
    payload = _valuation_payload()
    for row in payload["result"]["data"]:
        row[sparse_field] = None
    payload["result"]["data"][0][sparse_field] = 1.0

    result = fetch_quality_history(
        "600519",
        "2026-07-17",
        weekly_adapter=_WeeklyAdapter(_weekly_bars()),
        valuation_session=_Session(_Response(payload)),
        use_cache=False,
    )

    assert result.available
    assert result.valuation_history[median_key] is None
    assert result.valuation_history[percentile_key] is None
    assert result.valuation_history[complete_percentile_key] is not None


def test_quality_history_fails_closed_for_new_listing_and_truncated_valuation():
    short_bars = _weekly_bars()[-100:]
    payload = _valuation_payload()
    payload["result"]["count"] = 2_001
    result = fetch_quality_history(
        "600519",
        "2026-07-17",
        weekly_adapter=_WeeklyAdapter(short_bars),
        valuation_session=_Session(_Response(payload)),
        use_cache=False,
    )

    assert not result.available
    assert result.reason.startswith("source_unavailable:QualityHistoryError:")
    assert "truncated" in result.reason


def test_quality_history_does_not_label_a_four_point_six_year_window_as_five_years():
    payload = _valuation_payload()
    rows = payload["result"]["data"][:-140]
    payload["result"]["data"] = rows
    payload["result"]["count"] = len(rows)

    result = fetch_quality_history(
        "600519",
        "2026-07-17",
        weekly_adapter=_WeeklyAdapter(_weekly_bars()),
        valuation_session=_Session(_Response(payload)),
        use_cache=False,
    )

    assert not result.available
    assert result.valuation_history["reason"] == "insufficient_five_year_span"
    assert result.valuation_history["start_delay_days"] > 62


def test_tencent_adapter_can_request_hfq_without_changing_beta_default_contract():
    class _Http:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            payload = {
                "code": 0,
                "data": {"sh600519": {"hfqweek": [["2026-07-17", "10", "11", "12", "9", "100"]]}},
            }
            return _Response(payload, url=url)

    http = _Http()
    adapter = TencentWeeklyHistoryAdapter(http_client=http, retries=1, bar_limit=620, stock_adjustment="hfq")
    bars = adapter.fetch_weekly_closes("sh600519", date(2026, 7, 17), require_forward_adjusted=True)
    assert bars == [WeeklyClose(date(2026, 7, 17), 11)]
    assert http.calls[0][1]["params"] == {"param": "sh600519,week,,2026-07-17,620,hfq"}


def test_quality_history_batch_rejects_duplicates_before_work(monkeypatch):
    with pytest.raises(ValueError, match="duplicate code"):
        fetch_quality_history_batch(
            [
                {"code": "600519", "as_of": "2026-07-17"},
                {"code": "600519", "as_of": "2026-07-17"},
            ]
        )

    monkeypatch.setattr(
        "data.quality_history.fetch_quality_history",
        lambda code, as_of: type("Result", (), {"to_dict": lambda self: {"code": code, "as_of": as_of}})(),
    )
    result = fetch_quality_history_batch(
        [
            {"code": "600001", "as_of": "2026-07-17"},
            {"code": "000001", "as_of": "2026-07-17"},
        ],
        max_workers=2,
    )
    assert list(result) == ["000001", "600001"]


@pytest.mark.parametrize("max_workers", [True, 2.0, 0, -1])
def test_quality_history_batch_requires_a_positive_integer_worker_count(max_workers):
    with pytest.raises(ValueError, match="max_workers"):
        fetch_quality_history_batch(
            [{"code": "600519", "as_of": "2026-07-17"}],
            max_workers=max_workers,
        )


def test_quality_history_rejects_boolean_page_counts():
    payload = _valuation_payload()
    payload["result"]["pages"] = True
    result = fetch_quality_history(
        "600519",
        "2026-07-17",
        weekly_adapter=_WeeklyAdapter(_weekly_bars()),
        valuation_session=_Session(_Response(payload)),
        use_cache=False,
    )

    assert not result.available
    assert "invalid result shape" in result.reason


def test_quality_history_rejects_future_cutoffs_before_network_work():
    adapter = _WeeklyAdapter([])
    with pytest.raises(ValueError, match="future"):
        fetch_quality_history(
            "600519",
            date.today() + timedelta(days=1),
            weekly_adapter=adapter,
            use_cache=False,
        )
    assert adapter.calls == []
