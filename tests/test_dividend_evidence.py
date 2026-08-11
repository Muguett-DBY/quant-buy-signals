"""Eastmoney dividend evidence tests for the Type 7 gdN filter."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import dividend_evidence as dividend
from data.dividend_evidence import load_dividend_evidence


class _FakeResponse:
    def __init__(self, rows: list[dict], *, status: int = 200, retry_after: str | None = None) -> None:
        self._rows = rows
        self.status_code = status
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> dict:
        return {"result": {"data": self._rows}}

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, rows_by_code: dict[str, list[dict]]) -> None:
        self._rows = rows_by_code

    def get(self, url, *, params, headers, timeout):
        code = str(params.get("filter") or "").split('"')[1]
        return _FakeResponse(self._rows.get(code, []))


class _QueuedSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.served: list[_FakeResponse] = []
        self.calls = 0

    def get(self, url, *, params, headers, timeout):
        del url, params, headers, timeout
        self.calls += 1
        response = self.responses.pop(0)
        self.served.append(response)
        return response


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


def test_dividend_retry_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(dividend.time, "sleep", waits.append)
    session = _QueuedSession(
        [
            _FakeResponse([], status=429, retry_after="8"),
            _FakeResponse([{"REPORT_DATE": "2026-06-30", "PRETAX_BONUS_RMB": 10.0}]),
        ]
    )

    rows = dividend._fetch_dividend_rows("600519", session=session)

    assert len(rows) == 1
    assert session.calls == 2
    assert waits == [8.0]
    assert all(response.closed for response in session.served)


def test_dividend_terminal_http_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(dividend.time, "sleep", waits.append)
    session = _QueuedSession([_FakeResponse([], status=404), _FakeResponse([])])

    with pytest.raises(dividend.DividendEvidenceError, match="HTTP 404"):
        dividend._fetch_dividend_rows("600519", session=session)

    assert session.calls == 1
    assert waits == []
    assert session.served[0].closed
    assert not session.responses[0].closed
