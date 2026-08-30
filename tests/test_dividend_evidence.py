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
        self.calls = 0

    def get(self, url, *, params, headers, timeout, stream):
        self.calls += 1
        assert stream is True
        code = str(params.get("filter") or "").split('"')[1]
        return _FakeResponse(self._rows.get(code, []))


class _QueuedSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.served: list[_FakeResponse] = []
        self.calls = 0

    def get(self, url, *, params, headers, timeout, stream):
        del url, params, headers, timeout
        assert stream is True
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

    assert evidence["000001"]["status"] == "unavailable"
    assert evidence["000001"]["reason"] == "source_returned_no_rows"
    record = evidence["600519"]
    assert record["status"] == "available"
    assert record["trailing_cash_per_share"] == 27.6 + 23.9
    assert set(record["evidence"]) == {"source", "evidence_id", "as_of", "summary"}
    assert record["evidence"]["as_of"] == "2026-07-31"
    assert "600519" in record["evidence"]["evidence_id"]


def test_load_dividend_evidence_does_not_treat_announced_but_unpaid_cash_as_paid(tmp_path):
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
    assert evidence["600519"]["status"] == "unavailable"
    assert evidence["600519"]["reason"] == "no_paid_cash_dividend_in_trailing_year"


def test_load_dividend_evidence_rejects_future_ex_date_as_trailing_cash(tmp_path):
    session = _FakeSession(
        {
            "600519": [
                {
                    "REPORT_DATE": "2026-06-30",
                    "NOTICE_DATE": "2026-07-20",
                    "EX_DIVIDEND_DATE": "2026-08-10",
                    "PRETAX_BONUS_RMB": 100.0,
                    "DIVIDENT_RATIO": 0.2,
                }
            ]
        }
    )

    evidence = load_dividend_evidence(["600519"], as_of="2026-07-31", cache_dir=tmp_path, session=session)

    assert evidence["600519"]["status"] == "unavailable"
    assert evidence["600519"]["reason"] == "no_paid_cash_dividend_in_trailing_year"


def test_load_dividend_evidence_preserves_explicit_paid_zero(tmp_path):
    session = _FakeSession(
        {
            "600519": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "NOTICE_DATE": "2026-04-01",
                    "EX_DIVIDEND_DATE": "2026-06-01",
                    "PRETAX_BONUS_RMB": 0.0,
                    "DIVIDENT_RATIO": 0.0,
                }
            ]
        }
    )

    evidence = load_dividend_evidence(["600519"], as_of="2026-07-31", cache_dir=tmp_path, session=session)

    assert evidence["600519"]["status"] == "known_zero"
    assert evidence["600519"]["trailing_cash_per_share"] == 0.0


def test_invalid_cached_row_is_refetched_instead_of_becoming_evidence(tmp_path):
    cache = dividend.SafeFileCache(
        tmp_path / "600519.json.gz",
        schema_version=dividend.DIVIDEND_CACHE_SCHEMA_VERSION,
        ttl=dividend.DIVIDEND_CACHE_TTL_SECONDS,
    )
    cache.save(
        {
            "model_id": dividend.DIVIDEND_CACHE_MODEL_ID,
            "code": "600519",
            "rows": [{"report_date": "invalid", "cash_per_ten_share": 99.0}],
        }
    )
    session = _FakeSession(
        {
            "600519": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "EX_DIVIDEND_DATE": "2026-06-01",
                    "PRETAX_BONUS_RMB": 10.0,
                    "DIVIDENT_RATIO": 0.1,
                }
            ]
        }
    )

    evidence = load_dividend_evidence(["600519"], as_of="2026-07-31", cache_dir=tmp_path, session=session)

    assert evidence["600519"]["status"] == "available"
    assert evidence["600519"]["trailing_cash_per_share"] == 1.0


def test_dividend_cache_only_miss_is_explicit_and_never_fetches(tmp_path):
    session = _FakeSession({"600519": []})

    evidence = load_dividend_evidence(
        ["600519"],
        as_of="2026-07-31",
        cache_dir=tmp_path,
        session=session,
        cache_only=True,
    )

    assert evidence["600519"]["status"] == "unavailable"
    assert evidence["600519"]["reason"] == "cache_miss"
    assert session.calls == 0


def test_dividend_cache_only_replays_expired_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(dividend, "DIVIDEND_CACHE_TTL_SECONDS", 0)
    rows = {
        "600519": [
            {
                "REPORT_DATE": "2025-12-31",
                "EX_DIVIDEND_DATE": "2026-06-01",
                "PRETAX_BONUS_RMB": 10.0,
                "DIVIDENT_RATIO": 0.1,
            }
        ]
    }
    initial = _FakeSession(rows)
    expected = load_dividend_evidence(
        ["600519"],
        as_of="2026-07-31",
        cache_dir=tmp_path,
        session=initial,
    )
    offline = _FakeSession({})

    replayed = load_dividend_evidence(
        ["600519"],
        as_of="2026-07-31",
        cache_dir=tmp_path,
        session=offline,
        cache_only=True,
    )

    assert replayed == expected
    assert initial.calls == 1
    assert offline.calls == 0


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
