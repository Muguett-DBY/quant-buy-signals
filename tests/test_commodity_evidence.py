"""Commodity-cycle evidence tests for the Type 5 strong-cycle gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.commodity_evidence import (
    INDUSTRY_COMMODITY_SYMBOLS,
    _cycle_swing_score,
    load_commodity_cycle_evidence,
)


class _FakeSession:
    def __init__(self, rows_by_symbol: dict[str, list[dict]]) -> None:
        self._rows = rows_by_symbol

    def get(self, url, *, params, headers, timeout):
        class _Response:
            def __init__(self, text_value: str) -> None:
                self.text = text_value

            def raise_for_status(self) -> None:
                return None

        symbol = str(params.get("symbol") or "")
        rows = self._rows.get(symbol, [])
        import json as _json

        payload = "var _=(" + _json.dumps(rows) + ");"
        return _Response(payload)


def _bars(closes: list[float], *, start_year: int = 2022) -> list[dict]:
    bars: list[dict] = []
    year = start_year
    month = 1
    day = 1
    for close in closes:
        bars.append({"d": f"{year:04d}-{month:02d}-{day:02d}", "c": str(close)})
        day += 1
        if day > 27:
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
    return bars


def test_cycle_swing_score_maps_peak_to_trough_swing():
    assert _cycle_swing_score([100.0] * 1200 + [220.0]) == 10.0
    assert _cycle_swing_score([100.0] * 1200 + [170.0]) == 8.0
    assert _cycle_swing_score([100.0] * 1200 + [150.0]) == 7.0
    assert _cycle_swing_score([100.0] * 1200 + [130.0]) == 5.0
    assert _cycle_swing_score([100.0] * 1200 + [110.0]) == 1.0
    assert _cycle_swing_score([100.0] * 100) == 0.0  # insufficient history


def test_commodity_symbols_cover_every_direct_cyclical_industry():
    assert set(INDUSTRY_COMMODITY_SYMBOLS) == {
        "STEEL",
        "NONFERROUS",
        "CHEMICAL",
        "BUILDING_MATERIAL",
        "OIL_GAS",
        "COAL",
    }


def test_load_commodity_cycle_evidence_binds_code_dated_records(tmp_path, monkeypatch):
    closes = [100.0 + 30.0 * ((index % 500) / 500.0) for index in range(1300)]  # ~30% swing
    closes += [190.0]  # raise the peak above the lookback window
    session = _FakeSession({"CU0": _bars(closes)})
    industry_by_code = {"000001": "NONFERROUS", "000002": "NONFERROUS", "000003": "SOFTWARE"}

    evidence = load_commodity_cycle_evidence(
        industry_by_code,
        as_of="2026-07-31",
        cache_dir=tmp_path,
        session=session,
    )

    assert set(evidence) == {"000001", "000002"}
    for code, record in evidence.items():
        assert 0 <= record["score"] <= 10
        assert set(record["evidence"]) == {"source", "evidence_id", "as_of", "summary"}
        assert record["evidence"]["as_of"] == "2026-07-31"
        assert code in record["evidence"]["evidence_id"]
        assert "新浪期货主力连续CU0" in record["evidence"]["source"]


def test_load_commodity_cycle_evidence_reuses_cache(tmp_path, monkeypatch):
    session = _FakeSession({"RB0": _bars([100.0] * 1300)})
    industry_by_code = {"000001": "STEEL"}
    kwargs = {"as_of": "2026-07-31", "cache_dir": tmp_path, "session": session}
    first = load_commodity_cycle_evidence(industry_by_code, **kwargs)
    second = load_commodity_cycle_evidence(industry_by_code, **kwargs)
    assert first == second
    assert (tmp_path / "RB0.json.gz").exists()
