"""Eastmoney dividend evidence tests for the Type 7 gdN filter."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.dividend_evidence import load_dividend_evidence


class _FakeResponse:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"result": {"data": self._rows}}


class _FakeSession:
    def __init__(self, rows_by_code: dict[str, list[dict]]) -> None:
        self._rows = rows_by_code

    def get(self, url, *, params, headers, timeout):
        code = str(params.get("filter") or "").split('"')[1]
        return _FakeResponse(self._rows.get(code, []))


def test_load_dividend_evidence_computes_trailing_cash_and_binds_evidence(tmp_path):
    rows = {
        "600519": [
            {
                "REPORT_DATE": "2026-06-30",
                "EX_DIVIDEND_DATE": "2026-07-10",
                "PRETAX_BONUS_RMB": 276.0,  # 每10股派276元 = 每股27.6
                "DIVIDENT_RATIO": 0.021,
            },
            {
                "REPORT_DATE": "2025-12-31",
                "EX_DIVIDEND_DATE": "2026-01-15",
                "PRETAX_BONUS_RMB": 239.0,  # 每股23.9
                "DIVIDENT_RATIO": 0.019,
            },
            {
                "REPORT_DATE": "2025-06-30",
                "EX_DIVIDEND_DATE": "2025-07-20",
                "PRETAX_BONUS_RMB": 200.0,  # 超出12个月窗口
                "DIVIDENT_RATIO": 0.018,
            },
        ]
    }
    session = _FakeSession(rows)
    evidence = load_dividend_evidence(
        ["600519", "000001"],
        as_of="2026-07-31",
        cache_dir=tmp_path,
        session=session,
    )

    assert "000001" not in evidence  # 无分红记录
    record = evidence["600519"]
    assert record["trailing_cash_per_share"] == 27.6 + 23.9
    assert set(record["evidence"]) == {"source", "evidence_id", "as_of", "summary"}
    assert record["evidence"]["as_of"] == "2026-07-31"
    assert "600519" in record["evidence"]["evidence_id"]


def test_load_dividend_evidence_falls_back_to_latest_cash_when_no_ex_date(tmp_path):
    rows = {
        "600519": [
            {
                "REPORT_DATE": "2025-12-31",
                "EX_DIVIDEND_DATE": None,
                "PRETAX_BONUS_RMB": 240.0,
                "DIVIDENT_RATIO": None,
            }
        ]
    }
    session = _FakeSession(rows)
    evidence = load_dividend_evidence(["600519"], as_of="2026-07-31", cache_dir=tmp_path, session=session)
    assert evidence["600519"]["trailing_cash_per_share"] == 24.0
    assert evidence["600519"]["payout_ratio"] is None
