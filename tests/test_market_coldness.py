from __future__ import annotations

import json
from datetime import date, datetime, timezone
import threading

import pytest
import requests

import data.market_coldness as market_coldness
from data.cache import SafeFileCache
from data.market_coldness import (
    EASTMONEY_FIELDS,
    EASTMONEY_SOURCE,
    EASTMONEY_UNIVERSE,
    EastmoneyMarketColdnessAdapter,
    MarketColdnessError,
    archive_market_coldness_session_snapshot,
    fetch_market_coldness_snapshot,
    load_market_coldness_session_snapshot,
    market_coldness_session_cache_path,
    market_coldness_completed_session,
)


FIXED_TIME = datetime(2026, 7, 16, 5, 10, 9, tzinfo=timezone.utc)
AFTER_CLOSE_TIME = datetime(2026, 7, 16, 8, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("retrieved_at", "expected"),
    [
        ("2026-07-23T02:11:06+08:00", date(2026, 7, 22)),
        ("2026-07-23T09:14:59+08:00", date(2026, 7, 22)),
        ("2026-07-23T09:15:00+08:00", None),
        ("2026-07-23T15:15:00+08:00", None),
        ("2026-07-23T15:30:00+08:00", None),
        ("2026-07-23T16:14:59+08:00", None),
        ("2026-07-23T16:15:00+08:00", date(2026, 7, 23)),
        ("2026-07-25T12:00:00+08:00", date(2026, 7, 24)),
        ("2026-02-16T12:00:00+08:00", date(2026, 2, 13)),
    ],
)
def test_completed_session_uses_exchange_calendar_and_strict_intraday_boundaries(retrieved_at, expected):
    assert market_coldness_completed_session(datetime.fromisoformat(retrieved_at)) == expected


def _row(code="600000", market=1, **overrides):
    value = {
        "f12": code,
        "f13": market,
        "f14": f"股票{code}",
        "f24": -12.5,
        "f25": -8.25,
        "f8": 0.71,
        "f10": 0.83,
        "f26": 19991110,
        "f124": int(datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc).timestamp()),
    }
    value.update(overrides)
    return value


def _page(total, rows):
    return {"rc": 0, "data": {"total": total, "diff": rows}}


class _FakeResponse:
    def __init__(self, payload=None, *, raw=None, declared_length=None):
        if raw is None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.content = raw
        self.headers = {
            "Content-Length": str(len(raw) if declared_length is None else declared_length),
        }
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class _StatusResponse(_FakeResponse):
    def __init__(self, status_code, *, retry_after=None):
        super().__init__(_page(1, [_row()]))
        self.status_code = status_code
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)

    def raise_for_status(self):
        raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _FakeHttpClient:
    def __init__(self, page_actions):
        self.page_actions = {
            page: list(actions) if isinstance(actions, list) else [actions] for page, actions in page_actions.items()
        }
        self.calls = []
        self.responses = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        page = kwargs["params"]["pn"]
        actions = self.page_actions.get(page)
        if not actions:
            raise AssertionError(f"unexpected request for page {page}")
        action = actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        response = action if isinstance(action, _FakeResponse) else _FakeResponse(action)
        self.responses.append(response)
        return response


def _adapter(client, *, page_size=2, retries=1, retry_delay=0, max_workers=10, clock=None):
    return EastmoneyMarketColdnessAdapter(
        http_client=client,
        page_size=page_size,
        retries=retries,
        retry_delay=retry_delay,
        max_workers=max_workers,
        clock=clock or (lambda: FIXED_TIME),
    )


def test_multi_page_whole_market_fetch_is_complete_provenanced_and_excludes_bj_filters():
    missing = _row("000001", 0, f25="-", f8=None, f10=0)
    missing.pop("f24")
    missing["f26"] = "--"
    client = _FakeHttpClient(
        {
            1: _page(3, [_row("600000", 1), missing]),
            2: _page(3, [_row("300001", 0, f26="20091030")]),
        }
    )

    batch = _adapter(client).fetch_all()

    assert batch.total_expected == 3
    assert batch.page_count == 2
    assert len(batch.records) == 3
    assert [record.code for record in batch.records] == ["600000", "000001", "300001"]
    assert {record.exchange for record in batch.records} == {"SH", "SZ"}
    assert all(record.source == EASTMONEY_SOURCE for record in batch.records)
    assert all(record.retrieved_at == "2026-07-16T05:10:09Z" for record in batch.records)
    assert len(client.calls) == 2  # pagination, never one request per stock
    for _, kwargs in client.calls:
        assert kwargs["stream"] is True
        assert kwargs["params"]["fs"] == EASTMONEY_UNIVERSE
        assert kwargs["params"]["fields"] == ",".join(EASTMONEY_FIELDS)
        assert "t:81" not in kwargs["params"]["fs"]
    assert all(response.closed for response in client.responses)

    record = batch.records[1]
    assert record.change_60d_pct is None
    assert record.change_ytd_pct is None
    assert record.turnover_rate_pct is None
    assert record.volume_ratio == 0.0  # a real upstream zero remains distinguishable from missing
    assert record.listing_date is None
    assert "f24" not in record.upstream_fields
    assert record.missing_reasons == {
        "change_60d_pct": "upstream_field_absent:f24",
        "change_ytd_pct": "upstream_placeholder:f25",
        "turnover_rate_pct": "upstream_null:f8",
        "listing_date": "upstream_placeholder:f26",
    }

    snapshot = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient({1: _page(1, [missing])})), use_cache=False
    )
    assert snapshot.available
    assert snapshot.universe_coverage_rate == 1.0
    assert snapshot.coverage.by_metric["change_60d_pct"].present == 0
    assert snapshot.coverage.by_metric["change_60d_pct"].missing == 1
    assert snapshot.coverage.by_metric["volume_ratio"].coverage_rate == 1.0
    assert snapshot.coverage.complete_records == 0


def test_remaining_pages_are_fetched_concurrently_but_consumed_in_page_order():
    barrier = threading.Barrier(3, timeout=2)
    lock = threading.Lock()
    active = 0
    peak = 0

    class _ConcurrentClient:
        def get(self, _url, **kwargs):
            nonlocal active, peak
            page = kwargs["params"]["pn"]
            if page == 1:
                return _FakeResponse(_page(4, [_row("600000")]))
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                return _FakeResponse(_page(4, [_row(f"00000{page - 1}", 0)]))
            finally:
                with lock:
                    active -= 1

    batch = _adapter(_ConcurrentClient(), page_size=1, max_workers=3).fetch_all()

    assert peak == 3
    assert [record.code for record in batch.records] == ["600000", "000001", "000002", "000003"]


@pytest.mark.parametrize(
    ("started", "completed"),
    [
        ("2026-07-23T09:14:59+08:00", "2026-07-23T09:15:00+08:00"),
        ("2026-07-23T16:14:59+08:00", "2026-07-23T16:15:00+08:00"),
    ],
)
def test_acquisition_crossing_a_session_decision_boundary_is_rejected(started, completed):
    times = iter((datetime.fromisoformat(started), datetime.fromisoformat(completed)))
    adapter = _adapter(
        _FakeHttpClient({1: _page(1, [_row()])}),
        clock=lambda: next(times),
    )

    with pytest.raises(MarketColdnessError, match="crossed a trading-session decision boundary"):
        adapter.fetch_all()


def test_first_page_transport_failure_gets_bounded_longer_recovery():
    client = _FakeHttpClient({1: [requests.ReadTimeout("slow"), _page(1, [_row()])]})

    batch = _adapter(client, retries=1).fetch_all()

    assert len(batch.records) == 1
    assert [kwargs["timeout"] for _, kwargs in client.calls] == [15.0, 30.0]


def test_parallel_page_transport_failure_gets_bounded_sequential_recovery():
    client = _FakeHttpClient(
        {
            1: _page(2, [_row("600000")]),
            2: [requests.ReadTimeout("slow"), _page(2, [_row("000001", 0)])],
        }
    )

    batch = _adapter(client, page_size=1, retries=1).fetch_all()

    assert [record.code for record in batch.records] == ["600000", "000001"]
    page_two_calls = [kwargs for _, kwargs in client.calls if kwargs["params"]["pn"] == 2]
    assert [kwargs["timeout"] for kwargs in page_two_calls] == [15.0, 30.0]


def test_parallel_page_schema_failure_is_not_retried_by_collection_recovery():
    client = _FakeHttpClient(
        {
            1: _page(2, [_row("600000")]),
            2: _FakeResponse(raw=b"{"),
        }
    )

    with pytest.raises(MarketColdnessError, match="invalid JSON"):
        _adapter(client, page_size=1, retries=1).fetch_all()

    assert sum(kwargs["params"]["pn"] == 2 for _, kwargs in client.calls) == 1


def test_recovery_schema_failure_is_not_masked_as_transport():
    client = _FakeHttpClient(
        {
            1: _page(2, [_row("600000")]),
            2: [
                requests.ReadTimeout("slow"),
                _FakeResponse(raw=b"{"),
                _FakeResponse(raw=b"{"),
            ],
        }
    )

    with pytest.raises(MarketColdnessError, match="invalid JSON") as caught:
        _adapter(client, page_size=1, retries=1).fetch_all()

    assert not isinstance(caught.value, market_coldness._MarketColdnessTransientTransportError)
    assert sum(kwargs["params"]["pn"] == 2 for _, kwargs in client.calls) == 2


def test_persistent_page_transport_failure_stops_after_bounded_recovery():
    client = _FakeHttpClient(
        {
            1: _page(2, [_row("600000")]),
            2: [requests.ReadTimeout("slow") for _ in range(3)],
        }
    )

    with pytest.raises(MarketColdnessError, match="failed to recover Eastmoney page 2"):
        _adapter(client, page_size=1, retries=1).fetch_all()

    assert sum(kwargs["params"]["pn"] == 2 for _, kwargs in client.calls) == 3


def test_systemic_parallel_transport_failure_does_not_amplify_retries():
    page_count = market_coldness._MAX_RECOVERY_PAGES + 2
    actions = {1: _page(page_count, [_row("600000")])}
    actions.update({page: requests.ReadTimeout("slow") for page in range(2, page_count + 1)})
    client = _FakeHttpClient(actions)

    with pytest.raises(MarketColdnessError, match="above recovery limit"):
        _adapter(client, page_size=1, retries=1).fetch_all()

    call_counts = {
        page: sum(kwargs["params"]["pn"] == page for _, kwargs in client.calls) for page in range(1, page_count + 1)
    }
    assert call_counts == {page: 1 for page in range(1, page_count + 1)}


def test_missing_or_short_page_is_rejected_instead_of_returning_partial_market():
    client = _FakeHttpClient(
        {
            1: _page(3, [_row("600000"), _row("000001", 0)]),
            2: _page(3, []),
        }
    )

    with pytest.raises(MarketColdnessError, match=r"page 2 row-count mismatch: expected=1, received=0"):
        _adapter(client).fetch_all()


def test_total_must_remain_identical_on_every_page():
    client = _FakeHttpClient(
        {
            1: _page(3, [_row("600000"), _row("000001", 0)]),
            2: _page(4, [_row("300001", 0)]),
        }
    )

    with pytest.raises(MarketColdnessError, match="total changed during pagination"):
        _adapter(client).fetch_all()


def test_duplicate_code_across_pages_is_rejected_and_public_result_is_structured_failure():
    pages = {
        1: _page(3, [_row("600000"), _row("000001", 0)]),
        2: _page(3, [_row("600000")]),
    }
    with pytest.raises(MarketColdnessError, match="duplicate.*600000"):
        _adapter(_FakeHttpClient(pages)).fetch_all()

    result = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient(pages)),
        use_cache=False,
    )
    assert not result.available
    assert result.records == ()
    assert result.total_expected is None
    assert result.universe_coverage_rate is None
    assert result.failure["stage"] == "source_fetch"
    assert result.failure["kind"] == "MarketColdnessError"
    assert "duplicate" in result.reason


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_row("920001", 0), "non-Shanghai/Shenzhen"),
        (_row("830001", 0), "non-Shanghai/Shenzhen"),
        (_row("600000", 0), "market/code mismatch"),
        (_row("000001", 1), "market/code mismatch"),
    ],
)
def test_beijing_codes_and_market_identity_mismatches_are_rejected(row, message):
    with pytest.raises(MarketColdnessError, match=message):
        _adapter(_FakeHttpClient({1: _page(1, [row])})).fetch_all()


@pytest.mark.parametrize("bad_value", [True, float("nan"), float("inf"), float("-inf")])
def test_boolean_nan_and_infinity_are_never_accepted_as_market_metrics(bad_value):
    client = _FakeHttpClient({1: _page(1, [_row(f24=bad_value)])})

    with pytest.raises(MarketColdnessError):
        _adapter(client).fetch_all()


def test_nonnegative_market_fields_reject_negative_values():
    for field in ("f8", "f10"):
        with pytest.raises(MarketColdnessError, match=f"{field} must be non-negative"):
            _adapter(_FakeHttpClient({1: _page(1, [_row(**{field: -0.01})])})).fetch_all()


@pytest.mark.parametrize("bad_value", [True, 0, -1, 1.5, "1784187600"])
def test_source_update_timestamp_requires_a_positive_integer_epoch(bad_value):
    with pytest.raises(MarketColdnessError, match="f124 must be a positive Unix timestamp"):
        _adapter(_FakeHttpClient({1: _page(1, [_row(f124=bad_value)])})).fetch_all()


def test_transport_timeout_is_retried_and_successful_response_is_closed():
    client = _FakeHttpClient({1: [requests.Timeout("slow"), _page(1, [_row()])]})

    batch = _adapter(client, retries=2).fetch_all()

    assert len(batch.records) == 1
    assert len(client.calls) == 2
    assert len(client.responses) == 1
    assert client.responses[0].closed


@pytest.mark.parametrize(
    "error",
    [
        requests.ReadTimeout("timed out"),
        requests.ConnectionError("connection reset"),
        requests.exceptions.ChunkedEncodingError("truncated response"),
    ],
)
def test_recoverable_transport_errors_are_classified_transient(error):
    assert market_coldness._is_transient_transport_error(error)


@pytest.mark.parametrize(
    "error",
    [requests.exceptions.SSLError("certificate rejected"), requests.exceptions.ProxyError("proxy rejected")],
)
def test_security_and_proxy_failures_are_not_classified_transient(error):
    assert not market_coldness._is_transient_transport_error(error)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(408, True), (425, True), (429, True), (500, True), (503, True), (404, False), (407, False), (499, False)],
)
def test_only_retryable_http_statuses_are_classified_transient(status, expected):
    response = requests.Response()
    response.status_code = status
    assert market_coldness._is_transient_transport_error(requests.HTTPError(response=response)) is expected


def test_schema_failure_is_terminal_even_when_transport_retries_remain():
    client = _FakeHttpClient(
        {
            1: [
                _FakeResponse(raw=b"not-json"),
                requests.ReadTimeout("slow"),
                requests.ReadTimeout("slow"),
            ]
        }
    )
    adapter = _adapter(client, retries=3)

    with pytest.raises(MarketColdnessError, match="failed after 1 attempt") as caught:
        adapter._request_page(1)

    assert not isinstance(caught.value, market_coldness._MarketColdnessTransientTransportError)
    assert len(client.calls) == 1


def test_declared_oversized_response_is_rejected_without_retry_and_closed():
    response = _FakeResponse(_page(1, [_row()]), declared_length=5 * 1024 * 1024)
    client = _FakeHttpClient({1: response})

    with pytest.raises(MarketColdnessError, match="byte limit"):
        _adapter(client, retries=3).fetch_all()

    assert len(client.calls) == 1
    assert response.closed


def test_invalid_response_body_is_not_retried_as_transport(monkeypatch):
    valid = _FakeResponse(_page(1, [_row()]))
    invalid = _FakeResponse(raw=b"{" + (b" " * (len(valid.content) - 1)))
    monkeypatch.setattr(market_coldness, "_MAX_ACQUISITION_RESPONSE_BYTES", len(valid.content) + 1)
    client = _FakeHttpClient({1: [invalid, valid]})

    with pytest.raises(MarketColdnessError, match="failed after 1 attempt"):
        _adapter(client, retries=2).fetch_all()

    assert len(client.calls) == 1
    assert invalid.closed and not valid.closed


def test_retry_after_controls_transient_http_retry(monkeypatch):
    busy = _StatusResponse(429, retry_after=6)
    client = _FakeHttpClient({1: [busy, _FakeResponse(_page(1, [_row()]))]})
    waits = []
    monkeypatch.setattr(market_coldness.time, "sleep", waits.append)

    batch = _adapter(client, retries=2, retry_delay=0.5).fetch_all()

    assert len(batch.records) == 1
    assert len(client.calls) == 2
    assert waits == [6.0]
    assert busy.closed


def test_terminal_http_status_is_not_retried(monkeypatch):
    missing = _StatusResponse(404)
    spare = _FakeResponse(_page(1, [_row()]))
    client = _FakeHttpClient({1: [missing, spare]})
    waits = []
    monkeypatch.setattr(market_coldness.time, "sleep", waits.append)

    with pytest.raises(MarketColdnessError, match="failed after 1 attempt"):
        _adapter(client, retries=3, retry_delay=0.5).fetch_all()

    assert len(client.calls) == 1
    assert waits == []
    assert missing.closed and not spare.closed


def test_acquisition_byte_budget_latches_the_first_over_limit_chunk():
    budget = market_coldness._AcquisitionByteBudget(10)
    budget.charge(5)

    with pytest.raises(MarketColdnessError, match="11 > 10"):
        budget.charge(6)
    with pytest.raises(MarketColdnessError, match="already exceed.*11 > 10"):
        budget.charge(0)

    assert budget._consumed == 11
    assert budget._exhausted is True


def test_acquisition_byte_budget_preflight_stops_queued_page_requests(monkeypatch):
    page_count = 20
    first_response = _FakeResponse(_page(page_count, [_row("600000")]))
    remaining = {
        page: _FakeResponse(_page(page_count, [_row(f"{page - 1:06d}", 0)])) for page in range(2, page_count + 1)
    }
    monkeypatch.setattr(
        market_coldness,
        "_MAX_ACQUISITION_RESPONSE_BYTES",
        len(first_response.content) + 1,
    )
    client = _FakeHttpClient({1: first_response, **remaining})

    with pytest.raises(MarketColdnessError, match="byte limit"):
        _adapter(client, page_size=1, retries=1, max_workers=2).fetch_all()

    requested_pages = [kwargs["params"]["pn"] for _, kwargs in client.calls]
    assert requested_pages[0] == 1
    assert 2 <= len(requested_pages) <= 3
    assert set(requested_pages).issubset({1, 2, 3})


def test_cache_hit_replays_strict_records_without_calling_network(tmp_path):
    cache_path = tmp_path / "coldness.json.gz"
    first_client = _FakeHttpClient({1: _page(1, [_row()])})
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(first_client),
        cache_path=cache_path,
    )

    assert first.available
    assert not first.cache_hit
    assert first.cache_diagnostic.endswith(";saved")
    assert len(first_client.calls) == 1
    loaded = SafeFileCache(cache_path, schema_version=2, max_uncompressed_bytes=64 * 1024 * 1024).load()
    assert loaded.hit, loaded.reason
    assert loaded.value["contract"]["universe"] == EASTMONEY_UNIVERSE
    assert loaded.value["records"][0]["upstream_fields"]["f24"] == -12.5

    class _MustNotFetch:
        calls = 0

        def fetch_all(self):
            self.calls += 1
            raise AssertionError("cache hit must return before network")

    offline = _MustNotFetch()
    second = fetch_market_coldness_snapshot(adapter=offline, cache_path=cache_path)

    assert second.available
    assert second.cache_hit
    assert second.cache_diagnostic == "hit"
    assert second.records == first.records
    assert offline.calls == 0


def test_force_refresh_bypasses_hit_but_preserves_cache_cas(tmp_path):
    cache_path = tmp_path / "coldness.json.gz"
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient({1: _page(1, [_row("600000", 1)])}),
            clock=lambda: AFTER_CLOSE_TIME,
        ),
        cache_path=cache_path,
    )
    refresh_client = _FakeHttpClient({1: _page(1, [_row("000001", 0)])})

    refreshed = fetch_market_coldness_snapshot(
        adapter=_adapter(refresh_client),
        cache_path=cache_path,
        force_refresh=True,
    )
    replay = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient({})),
        cache_path=cache_path,
    )

    assert first.records[0].code == "600000"
    assert refreshed.records[0].code == "000001"
    assert len(refresh_client.calls) == 1
    assert refreshed.cache_diagnostic == "forced_refresh;saved"
    assert replay.cache_hit
    assert replay.records == refreshed.records


def test_expired_cache_can_be_replayed_explicitly_without_network(tmp_path):
    cache_path = tmp_path / "coldness.json.gz"
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient({1: _page(1, [_row()])})),
        cache_path=cache_path,
        cache_ttl_seconds=0,
    )

    class _MustNotFetch:
        def fetch_all(self):
            raise AssertionError("explicit historical replay must not call the network")

    replay = fetch_market_coldness_snapshot(
        adapter=_MustNotFetch(),
        cache_path=cache_path,
        cache_ttl_seconds=0,
        allow_expired_cache=True,
    )

    assert first.available
    assert replay.available
    assert replay.cache_hit
    assert replay.records == first.records


def test_cache_only_replays_expired_cache_without_network(tmp_path):
    cache_path = tmp_path / "coldness.json.gz"
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient({1: _page(1, [_row()])})),
        cache_path=cache_path,
        cache_ttl_seconds=0,
    )

    class _MustNotFetch:
        calls = 0

        def fetch_all(self):
            self.calls += 1
            raise AssertionError("cache-only replay must not call the network")

    offline = _MustNotFetch()
    replay = fetch_market_coldness_snapshot(
        adapter=offline,
        cache_path=cache_path,
        cache_ttl_seconds=0,
        cache_only=True,
    )

    assert first.available
    assert replay.available
    assert replay.cache_hit
    assert replay.records == first.records
    assert offline.calls == 0


def test_cache_only_miss_returns_unavailable_without_network(tmp_path):
    class _MustNotFetch:
        calls = 0

        def fetch_all(self):
            self.calls += 1
            raise AssertionError("cache-only miss must not call the network")

    offline = _MustNotFetch()
    snapshot = fetch_market_coldness_snapshot(
        adapter=offline,
        cache_path=tmp_path / "missing.json.gz",
        cache_only=True,
    )

    assert snapshot.available is False
    assert snapshot.reason == "source_unavailable:MarketColdnessError:cache_only_miss"
    assert snapshot.failure["detail"] == "cache_only_miss"
    assert offline.calls == 0


def test_semantically_invalid_checksummed_cache_is_refetched(tmp_path):
    cache_path = tmp_path / "coldness.json.gz"
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient({1: _page(1, [_row()])})),
        cache_path=cache_path,
    )
    assert first.available
    SafeFileCache(cache_path, schema_version=2).save({"unexpected": []})

    client = _FakeHttpClient({1: _page(1, [_row("000001", 0)])})
    replacement = fetch_market_coldness_snapshot(
        adapter=_adapter(client),
        cache_path=cache_path,
    )

    assert replacement.available
    assert replacement.records[0].code == "000001"
    assert replacement.cache_diagnostic.startswith("invalid_hit:")
    assert replacement.cache_diagnostic.endswith(";saved")
    assert len(client.calls) == 1


def test_constructor_rejects_invalid_retry_and_page_contract_before_io():
    with pytest.raises(ValueError, match="retries"):
        EastmoneyMarketColdnessAdapter(retries=0)
    with pytest.raises(ValueError, match="page_size"):
        EastmoneyMarketColdnessAdapter(page_size=0)
    with pytest.raises(ValueError, match="max_workers"):
        EastmoneyMarketColdnessAdapter(max_workers=0)
    with pytest.raises(ValueError, match="timeout"):
        EastmoneyMarketColdnessAdapter(timeout=True)
    with pytest.raises(ValueError, match="allow_expired_cache"):
        fetch_market_coldness_snapshot(use_cache=False, allow_expired_cache=1)
    with pytest.raises(ValueError, match="cache_only"):
        fetch_market_coldness_snapshot(use_cache=False, cache_only=1)


def test_session_archive_replays_an_exact_generation_after_the_rolling_cache_advances(tmp_path):
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient({1: _page(1, [_row("600000", 1)])}),
            clock=lambda: AFTER_CLOSE_TIME,
        ),
        use_cache=False,
    )
    archive_market_coldness_session_snapshot(first, "2026-07-16", directory=tmp_path)

    later = fetch_market_coldness_snapshot(
        adapter=EastmoneyMarketColdnessAdapter(
            http_client=_FakeHttpClient({1: _page(1, [_row("000001", 0)])}),
            page_size=2,
            retries=1,
            retry_delay=0,
            clock=lambda: datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc),
        ),
        use_cache=False,
    )
    assert later.records != first.records

    replay = load_market_coldness_session_snapshot("2026-07-16", directory=tmp_path)
    assert replay is not None
    assert replay.records == first.records
    assert replay.cache_diagnostic == "immutable_session_hit"


def test_session_archive_uses_a_schema_versioned_path_without_touching_a_legacy_file(tmp_path):
    legacy_path = tmp_path / "eastmoney_sh_sz_a_2026-07-16.json.gz"
    SafeFileCache(legacy_path, schema_version=1).save({"legacy_schema": 1})
    legacy_bytes = legacy_path.read_bytes()
    current_path = market_coldness_session_cache_path("2026-07-16", directory=tmp_path)
    assert current_path.name == "eastmoney_sh_sz_a_2026-07-16.v2.json.gz"
    assert current_path != legacy_path

    snapshot = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient({1: _page(1, [_row("600000", 1)])}),
            clock=lambda: AFTER_CLOSE_TIME,
        ),
        use_cache=False,
    )
    archived = archive_market_coldness_session_snapshot(snapshot, "2026-07-16", directory=tmp_path)
    replay = load_market_coldness_session_snapshot("2026-07-16", directory=tmp_path)

    assert current_path.exists()
    assert legacy_path.read_bytes() == legacy_bytes
    assert archived.records == snapshot.records
    assert replay is not None
    assert replay.records == snapshot.records


def test_session_archive_accepts_next_trading_day_preopen_for_the_previous_close(tmp_path):
    overnight = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient(
                {
                    1: _page(
                        1,
                        [
                            _row(
                                "600000",
                                1,
                                f124=int(datetime(2026, 7, 22, 7, 34, tzinfo=timezone.utc).timestamp()),
                            )
                        ],
                    )
                }
            ),
            clock=lambda: datetime(2026, 7, 22, 18, 11, 6, tzinfo=timezone.utc),
        ),
        use_cache=False,
    )

    archived = archive_market_coldness_session_snapshot(overnight, "2026-07-22", directory=tmp_path)
    replay = load_market_coldness_session_snapshot("2026-07-22", directory=tmp_path)

    assert archived.records == overnight.records
    assert replay is not None
    assert replay.records == overnight.records


def test_session_archive_rejects_source_rows_from_an_older_session(tmp_path):
    stale_source = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient(
                {
                    1: _page(
                        1,
                        [
                            _row(
                                "600000",
                                1,
                                f124=int(datetime(2026, 7, 21, 7, 34, tzinfo=timezone.utc).timestamp()),
                            )
                        ],
                    )
                }
            ),
            clock=lambda: datetime(2026, 7, 22, 18, 11, 6, tzinfo=timezone.utc),
        ),
        use_cache=False,
    )

    with pytest.raises(MarketColdnessError, match="source rows are bound to another session"):
        archive_market_coldness_session_snapshot(stale_source, "2026-07-22", directory=tmp_path)


@pytest.mark.parametrize(
    "retrieved_at",
    [
        datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 7, 15, tzinfo=timezone.utc),
    ],
)
def test_session_archive_rejects_a_generation_after_the_next_session_starts(tmp_path, retrieved_at):
    snapshot = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient({1: _page(1, [_row("600000", 1)])}),
            clock=lambda: retrieved_at,
        ),
        use_cache=False,
    )

    with pytest.raises(MarketColdnessError, match="bound to another session"):
        archive_market_coldness_session_snapshot(snapshot, "2026-07-22", directory=tmp_path)


def test_session_archive_rejects_a_different_rewrite_for_the_same_session(tmp_path):
    first = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient({1: _page(1, [_row("600000", 1)])}),
            clock=lambda: AFTER_CLOSE_TIME,
        ),
        use_cache=False,
    )
    different = fetch_market_coldness_snapshot(
        adapter=_adapter(
            _FakeHttpClient({1: _page(1, [_row("000001", 0)])}),
            clock=lambda: AFTER_CLOSE_TIME,
        ),
        use_cache=False,
    )
    archive_market_coldness_session_snapshot(first, "2026-07-16", directory=tmp_path)

    with pytest.raises(MarketColdnessError, match="different generation"):
        archive_market_coldness_session_snapshot(different, "2026-07-16", directory=tmp_path)


def test_session_archive_rejects_an_intraday_generation(tmp_path):
    intraday = fetch_market_coldness_snapshot(
        adapter=_adapter(_FakeHttpClient({1: _page(1, [_row("600000", 1)])})),
        use_cache=False,
    )

    with pytest.raises(MarketColdnessError, match="before the session close"):
        archive_market_coldness_session_snapshot(intraday, "2026-07-16", directory=tmp_path)
