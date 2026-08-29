from __future__ import annotations

from datetime import date, timedelta

import pytest

from data.baostock_valuation import (
    BAOSTOCK_CACHE_SCHEMA_VERSION,
    BAOSTOCK_FIELDS,
    BAOSTOCK_MAX_NETWORK_QUERIES,
    BaostockValuationError,
    fetch_baostock_valuation_batch,
    load_baostock_valuation_cache_batch,
)
from data.cache import SafeFileCache


class _Status:
    def __init__(self, code: str = "0", message: str = "success") -> None:
        self.error_code = code
        self.error_msg = message


class _Query(_Status):
    def __init__(self, rows, *, fields=BAOSTOCK_FIELDS, code: str = "0") -> None:
        super().__init__(code, "success" if code == "0" else "failed")
        self.fields = list(fields)
        self._rows = list(rows)
        self._index = -1

    def next(self):
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self):
        return self._rows[self._index]


class _Api:
    def __init__(self, rows_by_code) -> None:
        self.rows_by_code = rows_by_code
        self.login_calls = 0
        self.logout_calls = 0
        self.queries = []

    def login(self):
        self.login_calls += 1
        return _Status()

    def logout(self):
        self.logout_calls += 1
        return _Status()

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.queries.append((code, fields, kwargs))
        return _Query(self.rows_by_code[code])


class _LogoutFailureApi(_Api):
    def logout(self):
        self.logout_calls += 1
        raise RuntimeError("logout transport failed")


def _row(code: str, trade_date: str, *, pe: str = "12.3", pb: str = "1.4", status: str = "1"):
    return [trade_date, code, "10.0", pe, pb, "2.0", "8.0", "0.50", status, "0"]


def test_baostock_batch_uses_one_session_and_reuses_validated_cache(tmp_path):
    api = _Api(
        {
            "sz.000001": [_row("sz.000001", "2026-08-27")],
            "sh.600519": [_row("sh.600519", "2026-08-28", pe="", pb="6.4")],
        }
    )
    requests = [
        {"code": "600519", "as_of": "2026-08-28"},
        {"code": "000001", "as_of": "2026-08-28"},
    ]

    result = fetch_baostock_valuation_batch(requests, cache_dir=tmp_path, api=api)

    assert list(result) == ["000001", "600519"]
    assert api.login_calls == api.logout_calls == 1
    assert [call[0] for call in api.queries] == ["sz.000001", "sh.600519"]
    assert result["600519"]["rows"] == [{"date": "2026-08-28", "pe_ttm": None, "pb_mrq": 6.4}]
    assert len(result["600519"]["source_sha256"]) == 64

    cached = fetch_baostock_valuation_batch(requests, cache_dir=tmp_path, api=_Api({}))
    assert all(record["cache_hit"] is True for record in cached.values())


def test_baostock_rejects_identity_mismatch_and_logs_out(tmp_path):
    api = _Api({"sh.600519": [_row("sh.600000", "2026-08-28")]})

    with pytest.raises(BaostockValuationError, match="identity mismatch"):
        fetch_baostock_valuation_batch(
            [{"code": "600519", "as_of": "2026-08-28"}],
            cache_dir=tmp_path,
            api=api,
            use_cache=False,
        )

    assert api.login_calls == api.logout_calls == 1


def test_baostock_skips_suspended_rows_but_hashes_the_complete_capture(tmp_path):
    api = _Api(
        {
            "sh.600519": [
                _row("sh.600519", "2026-08-27", status="0"),
                _row("sh.600519", "2026-08-28"),
            ]
        }
    )
    result = fetch_baostock_valuation_batch(
        [{"code": "600519", "as_of": "2026-08-28"}],
        cache_dir=tmp_path,
        api=api,
        use_cache=False,
    )

    assert result["600519"]["rows"] == [{"date": "2026-08-28", "pe_ttm": 12.3, "pb_mrq": 1.4}]


def test_baostock_does_not_cache_an_empty_provider_capture(tmp_path):
    request = [{"code": "600519", "as_of": "2026-08-28"}]
    first = _Api({"sh.600519": []})
    second = _Api({"sh.600519": [_row("sh.600519", "2026-08-28")]})

    assert fetch_baostock_valuation_batch(request, cache_dir=tmp_path, api=first)["600519"]["available"] is False
    refreshed = fetch_baostock_valuation_batch(request, cache_dir=tmp_path, api=second)

    assert second.queries
    assert refreshed["600519"]["available"] is True


def test_baostock_rejects_non_numeric_provider_multiples_without_masking_the_error_on_logout(tmp_path):
    api = _LogoutFailureApi({"sh.600519": [_row("sh.600519", "2026-08-28", pe="not-a-number")]})

    with pytest.raises(BaostockValuationError, match="invalid peTTM"):
        fetch_baostock_valuation_batch(
            [{"code": "600519", "as_of": "2026-08-28"}],
            cache_dir=tmp_path,
            api=api,
            use_cache=False,
        )


def test_baostock_replays_raw_cache_and_refetches_a_digest_mismatch(tmp_path):
    request = [{"code": "600519", "as_of": "2026-08-28"}]
    fetch_baostock_valuation_batch(
        request,
        cache_dir=tmp_path,
        api=_Api({"sh.600519": [_row("sh.600519", "2026-08-28", pe="12.3")]}),
    )
    cache_path = next(tmp_path.glob("*.json.gz"))
    cache = SafeFileCache(cache_path, schema_version=BAOSTOCK_CACHE_SCHEMA_VERSION, ttl=86_400)
    poisoned = dict(cache.load().value)
    poisoned["raw_rows"] = [_row("sh.600519", "2026-08-28", pe="999")]
    cache.save(poisoned)
    replacement = _Api({"sh.600519": [_row("sh.600519", "2026-08-28", pe="13.4")]})

    result = fetch_baostock_valuation_batch(request, cache_dir=tmp_path, api=replacement)

    assert replacement.queries
    assert result["600519"]["rows"][0]["pe_ttm"] == 13.4


def test_baostock_reuses_a_recent_validated_capture_for_the_sliding_window(tmp_path):
    old_request = [{"code": "600519", "as_of": "2026-08-27"}]
    fetch_baostock_valuation_batch(
        old_request,
        cache_dir=tmp_path,
        api=_Api({"sh.600519": [_row("sh.600519", "2026-08-27")]}),
    )
    no_network = _Api({})

    result = fetch_baostock_valuation_batch(
        [{"code": "600519", "as_of": "2026-08-28"}],
        cache_dir=tmp_path,
        api=no_network,
    )

    assert no_network.login_calls == 0
    assert result["600519"]["cache_hit"] is True
    assert result["600519"]["cache_as_of"] == "2026-08-27"

    replayed = load_baostock_valuation_cache_batch(
        [{"code": "600519", "as_of": "2026-08-28"}],
        cache_dir=tmp_path,
    )
    assert replayed["600519"]["rows"] == result["600519"]["rows"]
    assert replayed["600519"]["cache_hit"] is True


def test_baostock_applies_one_network_budget_after_reusing_cache(tmp_path):
    codes = [f"{index:06d}" for index in range(1, BAOSTOCK_MAX_NETWORK_QUERIES + 3)]
    api = _Api({f"sz.{code}": [_row(f"sz.{code}", "2026-08-28")] for code in codes[:BAOSTOCK_MAX_NETWORK_QUERIES]})

    result = fetch_baostock_valuation_batch(
        [{"code": code, "as_of": "2026-08-28"} for code in codes],
        cache_dir=tmp_path,
        api=api,
        use_cache=False,
    )

    assert len(api.queries) == BAOSTOCK_MAX_NETWORK_QUERIES
    assert result[codes[-2]]["reason"] == "network_budget_exhausted"
    assert result[codes[-1]]["reason"] == "network_budget_exhausted"


def test_quality_history_batch_uses_baostock_only_for_an_unavailable_valuation(monkeypatch):
    from data import quality_history as qh

    class _Primary:
        def __init__(self, code: str, valuation_available: bool) -> None:
            self.code = code
            self.valuation_available = valuation_available

        def to_dict(self):
            return {
                "available": self.valuation_available,
                "code": self.code,
                "as_of": "2026-08-28",
                "model_id": qh.MODEL_ID,
                "shareholder_return": {"available": True},
                "valuation_history": {
                    "available": self.valuation_available,
                    "reason": "" if self.valuation_available else "missing_valuation_history",
                },
                "sources": [],
                "cache_hit": False,
                "cache_diagnostic": "primary",
                "reason": "" if self.valuation_available else "missing:valuation_history",
            }

    monkeypatch.setattr(qh, "fetch_quality_history", lambda code, _as_of: _Primary(code, code == "000001"))
    start = date(2021, 8, 28)
    rows = []
    cursor = start
    while cursor <= date(2026, 8, 28):
        if cursor.weekday() < 5:
            rows.append({"date": cursor.isoformat(), "pe_ttm": 10.0, "pb_mrq": 1.0})
        cursor += timedelta(days=1)
    captured = []

    def fallback(requests):
        captured.extend(requests)
        return {
            "600519": {
                "available": True,
                "rows": rows,
                "source_sha256": "a" * 64,
                "cache_hit": False,
            }
        }

    monkeypatch.setattr(qh, "fetch_baostock_valuation_batch", fallback)
    result = qh.fetch_quality_history_batch(
        [
            {"code": "000001", "as_of": "2026-08-28"},
            {"code": "600519", "as_of": "2026-08-28"},
        ],
        max_workers=2,
    )

    assert captured == [{"code": "600519", "as_of": "2026-08-28"}]
    assert result["600519"]["valuation_history"]["available"] is True
    assert result["600519"]["available"] is True
    assert result["000001"]["sources"] == []


def test_quality_history_cache_loader_replays_baostock_component(monkeypatch, tmp_path):
    from data import quality_history as qh

    evidence = qh.QualityHistoryEvidence(
        available=False,
        code="600519",
        as_of="2026-08-28",
        model_id=qh.MODEL_ID,
        shareholder_return={"available": True},
        valuation_history={"available": False, "reason": "missing_valuation_history"},
        sources=[],
        cache_hit=True,
        cache_diagnostic="hit",
        reason="missing:valuation_history",
    )
    capture = qh._QualityHistoryCacheCapture(
        evidence=evidence,
        weekly_bars=[],
        valuation_rows=[],
        component_checked_as_of={
            "shareholder_return": date(2026, 8, 28),
            "valuation_history": date(2026, 8, 28),
        },
    )
    monkeypatch.setattr(qh, "_load_reusable_capture", lambda *_args, **_kwargs: capture)
    start = date(2021, 8, 28)
    rows = []
    cursor = start
    while cursor <= date(2026, 8, 28):
        if cursor.weekday() < 5:
            rows.append({"date": cursor.isoformat(), "pe_ttm": 10.0, "pb_mrq": 1.0})
        cursor += timedelta(days=1)
    monkeypatch.setattr(
        qh,
        "load_baostock_valuation_cache_batch",
        lambda *_args, **_kwargs: {
            "600519": {
                "available": True,
                "rows": rows,
                "source_sha256": "a" * 64,
                "cache_hit": True,
            }
        },
    )

    result, due = qh.load_quality_history_cache_batch_state(
        [{"code": "600519", "as_of": "2026-08-28"}],
        cache_dir=tmp_path,
    )

    assert due == ()
    assert result["600519"]["available"] is True
    assert result["600519"]["valuation_history"]["available"] is True
    assert result["600519"]["sources"][0]["name"] == qh.BAOSTOCK_SOURCE_NAME
