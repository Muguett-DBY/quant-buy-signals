from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import data.fetcher as fetcher


def _indicator_frame(*codes: str) -> pd.DataFrame:
    rows = []
    for index, code in enumerate(codes):
        year = 2024 + (index % 2)
        row = {
            "SECURITY_CODE": code,
            "SECUCODE": f"{code}.{'BJ' if code.startswith(('8', '9')) else 'SZ'}",
            "REPORT_DATE": f"{year}-12-31",
            "REPORT_TYPE": "年报",
            "REPORT_DATE_NAME": f"{year}年报",
            "REPORT_YEAR": str(year),
            "NOTICE_DATE": f"{year + 1}-04-30",
            "SOURCE_REPORT_NAME": "RPT_F10_FINANCE_MAINFINADATA",
        }
        row.update(
            {
                field: float(position + index)
                for position, field in enumerate(fetcher.MAIN_FINANCIAL_INDICATOR_METRICS, 1)
            }
        )
        row["TOTAL_SHARE"] = 1_000_000 + index
        row["STAFF_NUM"] = 100 + index
        rows.append(row)
    return pd.DataFrame(rows)


def quote_rows(page, count, *, prefix="sh"):
    return [
        {
            "code": f"{page:02d}{index:04d}",
            "name": f"stock-{page}-{index}",
            "symbol": f"{prefix}{page:02d}{index:04d}",
            "trade": "10",
            "per": "12",
            "pb": "1.5",
            "mktcap": "100",
        }
        for index in range(count)
    ]


def test_sina_parallel_collection_keeps_every_lower_page_before_short_tail(monkeypatch):
    calls = Counter()
    pages = {1: quote_rows(1, 2), 2: quote_rows(2, 2), 3: quote_rows(3, 1)}

    def fake_page(page, **_kwargs):
        calls[page] += 1
        return pages.get(page, [])

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 5)
    rows = fetcher._collect_sina_node("hs_a", max_workers=2, page_size=2, max_pages=6)
    assert [row["code"] for row in rows] == [
        "010000",
        "010001",
        "020000",
        "020001",
        "030000",
    ]
    assert calls[2] == 1
    assert calls[3] == 1


def test_sina_transient_empty_page_is_rechecked_not_treated_as_tail(monkeypatch):
    calls = Counter()

    def fake_page(page, **_kwargs):
        calls[page] += 1
        if page == 1:
            return quote_rows(1, 2)
        if page == 2:
            return [] if calls[page] == 1 else quote_rows(2, 2)
        if page == 3:
            return quote_rows(3, 1)
        return []

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 5)
    rows = fetcher._collect_sina_node("hs_a", max_workers=2, page_size=2, max_pages=6)
    assert len(rows) == 5
    assert calls[2] == 2


def test_sina_persistent_gap_before_nonempty_page_is_an_error(monkeypatch):
    def fake_page(page, **_kwargs):
        if page == 1:
            return quote_rows(1, 2)
        if page == 2:
            return []
        if page == 3:
            return quote_rows(3, 1)
        return []

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 5)
    with pytest.raises(fetcher.QuoteFetchError, match="page 2 expected"):
        fetcher._collect_sina_node("hs_a", max_workers=2, page_size=2, max_pages=6)


def test_sina_parallel_page_failure_gets_one_bounded_sequential_recovery(monkeypatch):
    calls = Counter()
    recovery_arguments = []

    def fake_page(page, **kwargs):
        calls[page] += 1
        if page == 2 and calls[page] == 1:
            raise fetcher._SinaTransientTransportError("parallel timeout")
        if page == 2:
            recovery_arguments.append((kwargs.get("timeout"), kwargs.get("retries")))
        return quote_rows(page, 1)

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 3)

    rows = fetcher._collect_sina_node("hs_a", max_workers=3, page_size=1, max_pages=6)

    assert [row["code"] for row in rows] == ["010000", "020000", "030000"]
    assert calls == Counter({2: 2, 1: 1, 3: 1})
    assert recovery_arguments == [(fetcher._SINA_RECOVERY_TIMEOUT, fetcher._SINA_RECOVERY_RETRIES)]


def test_sina_resource_limit_failure_is_never_retried(monkeypatch):
    calls = Counter()

    def fake_page(page, **_kwargs):
        calls[page] += 1
        if page == 2:
            raise fetcher._SinaResourceLimitError("too large")
        return quote_rows(page, 1)

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 3)

    with pytest.raises(fetcher.QuoteFetchError, match="too large"):
        fetcher._collect_sina_node("hs_a", max_workers=3, page_size=1, max_pages=6)

    assert calls[2] == 1


def test_sina_schema_failure_is_not_retried_by_collection_recovery(monkeypatch):
    calls = Counter()

    def fake_page(page, **_kwargs):
        calls[page] += 1
        if page == 2:
            raise fetcher.QuoteFetchError("invalid page schema")
        return quote_rows(page, 1)

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 3)

    with pytest.raises(fetcher.QuoteFetchError, match="invalid page schema"):
        fetcher._collect_sina_node("hs_a", max_workers=3, page_size=1, max_pages=6)

    assert calls[2] == 1


def test_sina_recovery_schema_failure_is_not_masked(monkeypatch):
    calls = Counter()

    def fake_page(page, **_kwargs):
        calls[page] += 1
        if page == 2 and calls[page] == 1:
            raise fetcher._SinaTransientTransportError("parallel timeout")
        if page == 2:
            raise fetcher.QuoteFetchError("invalid recovery schema")
        return quote_rows(page, 1)

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 3)

    with pytest.raises(fetcher.QuoteFetchError, match="invalid recovery schema"):
        fetcher._collect_sina_node("hs_a", max_workers=3, page_size=1, max_pages=6)

    assert calls[2] == 2


def test_sina_persistent_transport_recovery_stops_after_bounded_second_phase(monkeypatch):
    calls = Counter()

    def fake_page(page, **_kwargs):
        calls[page] += 1
        if page == 2:
            raise fetcher._SinaTransientTransportError("timed out")
        return quote_rows(page, 1)

    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 3)

    with pytest.raises(fetcher.QuoteFetchError, match="failed to recover Sina hs_a page 2"):
        fetcher._collect_sina_node("hs_a", max_workers=3, page_size=1, max_pages=6)

    assert calls[2] == 2


def test_sina_systemic_parallel_failure_does_not_amplify_retries(monkeypatch):
    calls = Counter()

    def fake_page(page, **_kwargs):
        calls[page] += 1
        raise fetcher._SinaTransientTransportError(f"page {page} unavailable")

    page_count = fetcher._MAX_SINA_RECOVERY_PAGES + 1
    monkeypatch.setattr(fetcher, "_sina_page", fake_page)
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: page_count)

    with pytest.raises(fetcher.QuoteFetchError, match="above recovery limit"):
        fetcher._collect_sina_node("hs_a", max_workers=page_count, page_size=1, max_pages=page_count)

    assert calls == Counter({page: 1 for page in range(1, page_count + 1)})


class FakeResponse:
    def __init__(self, text, status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class StreamingFakeResponse(FakeResponse):
    def __init__(self, chunks, *, headers=None, encoding="utf-8"):
        super().__init__("", headers=headers)
        self._chunks = chunks
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == fetcher._SINA_RESPONSE_CHUNK_BYTES
        yield from self._chunks

    def close(self):
        self.closed = True


def _classic_line(symbol: str, source_date: str = "2026-07-15", tick_time: str = "15:00:00") -> str:
    fields = ["name", *(["0"] * 29), source_date, tick_time]
    fields[2] = "9"
    fields[3] = "10"
    return f'var hq_str_{symbol}="{",".join(fields)}";'


def test_sina_page_uses_https_and_retries_http_status(monkeypatch):
    responses = [FakeResponse("rate limited", 429), FakeResponse("[]", 200)]
    urls = []
    request_kwargs = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fake_get(url, **kwargs):
        urls.append(url)
        request_kwargs.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    assert fetcher._sina_page(1, retries=2) == []
    assert len(urls) == 2
    assert all(url.startswith("https://") for url in urls)
    assert all(kwargs.get("stream") is True for kwargs in request_kwargs)


@pytest.mark.parametrize(
    "transport_error",
    [
        requests.ReadTimeout("timed out"),
        requests.ConnectionError("connection reset"),
        requests.exceptions.ChunkedEncodingError("truncated response"),
    ],
)
def test_sina_page_exhausted_transport_failures_are_typed_for_collection_recovery(monkeypatch, transport_error):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fail_transport(*_args, **_kwargs):
        raise transport_error

    monkeypatch.setattr(fetcher.requests, "get", fail_transport)

    with pytest.raises(fetcher._SinaTransientTransportError, match="after 3 attempts"):
        fetcher._sina_page(1, retries=3)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_sina_page_retryable_http_exhaustion_is_typed_for_collection_recovery(monkeypatch, status):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: FakeResponse("error", status))

    with pytest.raises(fetcher._SinaTransientTransportError, match=f"HTTP {status}"):
        fetcher._sina_page(1, retries=1)


def test_sina_page_permanent_http_error_is_not_typed_transient(monkeypatch):
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: FakeResponse("missing", 404))

    with pytest.raises(fetcher.QuoteFetchError, match="HTTP 404") as caught:
        fetcher._sina_page(1, retries=1)

    assert not isinstance(caught.value, fetcher._SinaTransientTransportError)


@pytest.mark.parametrize(
    "permanent_transport_error",
    [requests.exceptions.SSLError("certificate rejected"), requests.exceptions.ProxyError("proxy rejected")],
)
def test_sina_page_security_and_proxy_failures_are_not_typed_transient(monkeypatch, permanent_transport_error):
    def fail_transport(*_args, **_kwargs):
        raise permanent_transport_error

    monkeypatch.setattr(fetcher.requests, "get", fail_transport)

    with pytest.raises(fetcher.QuoteFetchError) as caught:
        fetcher._sina_page(1, retries=1)

    assert not isinstance(caught.value, fetcher._SinaTransientTransportError)


def test_sina_page_mixed_schema_and_transport_failures_are_not_typed_transient(monkeypatch):
    outcomes = [FakeResponse("not-json"), requests.ReadTimeout("timed out"), requests.ReadTimeout("timed out")]
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fake_get(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fetcher.requests, "get", fake_get)

    with pytest.raises(fetcher.QuoteFetchError, match="after 3 attempts") as caught:
        fetcher._sina_page(1, retries=3)

    assert not isinstance(caught.value, fetcher._SinaTransientTransportError)
    assert outcomes == []


def test_sina_retrieval_time_is_not_misrepresented_as_trade_date(monkeypatch):
    payload = '[{"code":"000001","name":"A","symbol":"sz000001","trade":"10","ticktime":"15:00:00"}]'
    response = FakeResponse(
        payload,
        headers={"Date": "Wed, 15 Jul 2026 04:00:00 GMT"},
    )
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: response)

    rows = fetcher._sina_page(1, retries=1)
    frame = fetcher._quotes_frame(rows)

    assert rows[0]["_retrieved_at"] == 1_784_088_000.0
    assert frame.loc[0, "quote_tick_time"] == "15:00:00"
    assert frame.loc[0, "source_trade_date"] is None
    assert frame.loc[0, "retrieved_at"] == rows[0]["_retrieved_at"]


def test_classic_sina_batch_preserves_commas_and_requires_exact_metadata(monkeypatch):
    requested_urls = []
    request_kwargs = []
    response = FakeResponse("\n".join((_classic_line("sz000001"), _classic_line("sh600000", tick_time="15:00:01"))))

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        request_kwargs.append(kwargs)
        return response

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "time", lambda: 123.0)
    result = fetcher._sina_classic_batch(["sz000001", "sh600000"], retries=1)

    assert result == {
        "sz000001": ("2026-07-15", "15:00:00", 10.0, 9.0, 123.0),
        "sh600000": ("2026-07-15", "15:00:01", 10.0, 9.0, 123.0),
    }
    assert requested_urls == ["https://hq.sinajs.cn/?list=sz000001,sh600000"]
    assert "%2C" not in requested_urls[0]
    assert request_kwargs[0].get("stream") is True

    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: FakeResponse(_classic_line("sz000001")))
    with pytest.raises(fetcher.QuoteFetchError, match="omitted source metadata"):
        fetcher._sina_classic_batch(["sz000001", "sh600000"], retries=1)


def test_complete_sina_snapshot_attaches_paired_source_date_and_time(monkeypatch):
    rows = [
        {
            "code": "000001",
            "name": "A",
            "symbol": "sz000001",
            "trade": "10",
            "settlement": "9",
            "per": "12",
            "pb": "1.5",
            "mktcap": "100",
            "ticktime": "14:59:00",
            "_retrieved_at": 123.0,
        }
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        fetcher,
        "_sina_trade_metadata",
        lambda symbols: {"sz000001": ("2026-07-15", "15:00:00", 10.0, 9.0, 124.0)},
    )

    frame = fetcher._get_sina_quotes_parallel()

    assert frame.loc[0, "source_trade_date"] == "2026-07-15"
    assert frame.loc[0, "quote_tick_time"] == "15:00:00"
    assert frame.loc[0, "retrieved_at"] == 124.0


def test_complete_sina_snapshot_drops_beijing_before_quote_parsing_and_metadata(monkeypatch):
    rows = [
        {
            # Deliberately malformed quote fields prove that a BJ source row is
            # removed before SH/SZ row-level parsing and metadata enrichment.
            "code": "920002",
            "name": "北交遥测",
            "symbol": "bj920002",
            "trade": "not-a-number",
            "settlement": "not-a-number",
            "per": "not-a-number",
            "pb": "not-a-number",
            "mktcap": "not-a-number",
        },
        {
            "code": "000001",
            "name": "深市样本",
            "symbol": "sz000001",
            "trade": "10",
            "settlement": "9",
            "per": "12",
            "pb": "1.5",
            "mktcap": "100",
        },
    ]
    requested_symbols = []
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        fetcher,
        "_sina_trade_metadata",
        lambda symbols: requested_symbols.extend(symbols) or {"sz000001": ("2026-07-15", "15:00:00", 10.0, 9.0, 124.0)},
    )

    frame = fetcher._get_sina_quotes_parallel()

    assert frame[["market", "code"]].to_records(index=False).tolist() == [("SZ", "000001")]
    assert requested_symbols == ["sz000001"]


@pytest.mark.parametrize("classic_price", [4.99, 20.01])
def test_sina_source_attachment_rejects_mismatched_price_generations(monkeypatch, classic_price):
    frame = fetcher._quotes_frame(
        [
            {
                "code": "000001",
                "name": "A",
                "symbol": "sz000001",
                "trade": "10",
                "settlement": "9",
                "per": "12",
                "pb": "1.5",
                "mktcap": "100",
            }
        ]
    )
    monkeypatch.setattr(
        fetcher,
        "_sina_trade_metadata",
        lambda _symbols: {
            "sz000001": ("2026-07-15", "15:00:00", classic_price, 9.0, 124.0),
        },
    )

    with pytest.raises(fetcher.QuoteFetchError, match="price generations disagree"):
        fetcher._attach_sina_source_metadata(frame)


@pytest.mark.parametrize(
    ("list_trade", "list_settlement", "classic_trade", "classic_close"),
    [("10", "9", 0.0, 9.0), ("0", "9", 10.0, 9.0)],
)
def test_sina_source_attachment_rejects_conflicting_trading_states(
    monkeypatch,
    list_trade,
    list_settlement,
    classic_trade,
    classic_close,
):
    frame = fetcher._quotes_frame(
        [
            {
                "code": "000001",
                "name": "A",
                "symbol": "sz000001",
                "trade": list_trade,
                "settlement": list_settlement,
            }
        ]
    )
    monkeypatch.setattr(
        fetcher,
        "_sina_trade_metadata",
        lambda _symbols: {
            "sz000001": ("2026-07-15", "15:00:00", classic_trade, classic_close, 124.0),
        },
    )

    with pytest.raises(fetcher.QuoteFetchError, match="trading states disagree"):
        fetcher._attach_sina_source_metadata(frame)


def test_zero_trade_quote_keeps_previous_close_but_is_not_marked_trading():
    rows = [
        {
            "code": "000001",
            "name": "停牌样本",
            "symbol": "sz000001",
            "trade": "0",
            "settlement": "9.5",
            "ticktime": "09:25:00",
            "_retrieved_at": 123.0,
        },
    ]

    frame = fetcher._quotes_frame(rows)

    assert frame["code"].tolist() == ["000001"]
    assert frame.loc[0, "price"] == 9.5
    assert frame.loc[0, "price_source"] == "previous_close"
    assert frame.loc[0, "quote_status"] == "suspended_or_no_trade"


def test_sh_sz_quote_without_trade_or_previous_close_fails_closed():
    rows = [
        {
            "code": "000002",
            "name": "无价格样本",
            "symbol": "sz000002",
            "trade": "0",
            "settlement": "0",
        }
    ]

    with pytest.raises(fetcher.QuoteFetchError, match="no defensible positive price"):
        fetcher._quotes_frame(rows)


def test_sina_count_is_https_validated_and_retried(monkeypatch):
    responses = [FakeResponse("unavailable", 503), FakeResponse('"5527"', 200)]
    urls = []
    request_kwargs = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fake_get(url, **kwargs):
        urls.append(url)
        request_kwargs.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    assert fetcher._sina_count("hs_a", retries=2) == 5527
    assert all(url.startswith("https://") for url in urls)
    assert all(kwargs.get("stream") is True for kwargs in request_kwargs)


def test_sina_count_gets_one_bounded_long_timeout_recovery_after_pure_timeouts(monkeypatch):
    outcomes = [
        requests.ReadTimeout("slow-1"),
        requests.ReadTimeout("slow-2"),
        requests.ReadTimeout("slow-3"),
        FakeResponse('"5528"'),
    ]
    timeouts = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fake_get(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    budget = fetcher._SinaAcquisitionByteBudget(fetcher._MAX_SINA_COUNT_ACQUISITION_BYTES)

    assert fetcher._sina_count_with_recovery("hs_a", budget) == 5528
    assert timeouts == [
        fetcher.REQUEST_TIMEOUT,
        fetcher.REQUEST_TIMEOUT,
        fetcher.REQUEST_TIMEOUT,
        fetcher._SINA_RECOVERY_TIMEOUT,
    ]
    assert outcomes == []


def test_sina_count_persistent_timeout_stops_after_bounded_second_phase(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fake_get(*_args, **kwargs):
        calls.append(kwargs["timeout"])
        raise requests.ReadTimeout("still slow")

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    budget = fetcher._SinaAcquisitionByteBudget(fetcher._MAX_SINA_COUNT_ACQUISITION_BYTES)

    with pytest.raises(fetcher.QuoteFetchError, match="failed to recover Sina hs_a row count"):
        fetcher._sina_count_with_recovery("hs_a", budget)

    assert calls == [
        fetcher.REQUEST_TIMEOUT,
        fetcher.REQUEST_TIMEOUT,
        fetcher.REQUEST_TIMEOUT,
        fetcher._SINA_RECOVERY_TIMEOUT,
        fetcher._SINA_RECOVERY_TIMEOUT,
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse("missing", 404),
        requests.exceptions.SSLError("certificate rejected"),
        requests.exceptions.ProxyError("proxy rejected"),
    ],
)
def test_sina_count_permanent_failure_never_enters_long_timeout_recovery(monkeypatch, outcome):
    calls = []

    def fake_get(*_args, **kwargs):
        calls.append(kwargs["timeout"])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    budget = fetcher._SinaAcquisitionByteBudget(fetcher._MAX_SINA_COUNT_ACQUISITION_BYTES)

    with pytest.raises(fetcher.QuoteFetchError) as caught:
        fetcher._sina_count_with_recovery("hs_a", budget)

    assert not isinstance(caught.value, fetcher._SinaTransientTransportError)
    assert calls == [fetcher.REQUEST_TIMEOUT]


def test_sina_count_mixed_transport_and_schema_failure_is_not_recovered(monkeypatch):
    outcomes = [requests.ReadTimeout("slow"), FakeResponse("not-json")]
    timeouts = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    def fake_get(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    budget = fetcher._SinaAcquisitionByteBudget(fetcher._MAX_SINA_COUNT_ACQUISITION_BYTES)

    with pytest.raises(fetcher.QuoteFetchError) as caught:
        fetcher._sina_count_with_recovery("hs_a", budget)

    assert not isinstance(caught.value, fetcher._SinaTransientTransportError)
    assert timeouts == [fetcher.REQUEST_TIMEOUT, fetcher.REQUEST_TIMEOUT]
    assert outcomes == []


@pytest.mark.parametrize("entrypoint", ["count", "page"])
def test_sina_deeply_nested_json_is_a_closed_permanent_source_failure(monkeypatch, entrypoint):
    payload = ("[" * 2_000 + "]" * 2_000).encode("ascii")
    response = StreamingFakeResponse([payload])
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(True)
        return response

    monkeypatch.setattr(fetcher.requests, "get", fake_get)

    if entrypoint == "count":
        budget = fetcher._SinaAcquisitionByteBudget(fetcher._MAX_SINA_COUNT_ACQUISITION_BYTES)
        with pytest.raises(fetcher.QuoteFetchError) as caught:
            fetcher._sina_count_with_recovery("hs_a", budget)
    else:
        with pytest.raises(fetcher.QuoteFetchError) as caught:
            fetcher._sina_page(1, retries=1)

    assert not isinstance(caught.value, fetcher._SinaTransientTransportError)
    assert calls == [True]
    assert response.closed


def test_sina_count_shared_body_budget_blocks_a_new_network_attempt(monkeypatch):
    calls = []
    response = FakeResponse('"5"')

    def fake_get(*_args, **_kwargs):
        calls.append(True)
        return response

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    budget = fetcher._SinaAcquisitionByteBudget(3)

    assert fetcher._sina_count("hs_a", retries=1, acquisition_budget=budget) == 5
    with pytest.raises(fetcher._SinaResourceLimitError, match="reached byte limit"):
        fetcher._sina_count("hs_a", retries=1, acquisition_budget=budget)
    assert calls == [True]


@pytest.mark.parametrize(
    "payload",
    ["{}", "true", '"-1"', '"not-a-count"', "3.9", '"3.9"', "1e3", '"1e3"', '"05527"', '"5５"'],
)
def test_sina_count_rejects_invalid_metadata(monkeypatch, payload):
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: FakeResponse(payload))
    with pytest.raises(fetcher.QuoteFetchError):
        fetcher._sina_count("hs_a", retries=1)


@pytest.mark.parametrize("entrypoint", ["count", "page", "classic"])
@pytest.mark.parametrize("mode", ["declared", "streamed"])
def test_sina_response_body_has_a_hard_byte_limit(monkeypatch, entrypoint, mode):
    monkeypatch.setattr(fetcher, "_MAX_SINA_RESPONSE_BYTES", 8)
    if entrypoint == "count":
        monkeypatch.setattr(fetcher, "_MAX_SINA_COUNT_RESPONSE_BYTES", 8)
    if mode == "declared":
        response = FakeResponse("[]", headers={"Content-Length": "9"})
    else:
        response = StreamingFakeResponse([b"[]", b" " * 7])
    calls = []

    def fake_get(*_args, **kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(fetcher.requests, "get", fake_get)

    with pytest.raises(fetcher.QuoteFetchError, match="byte limit"):
        if entrypoint == "count":
            fetcher._sina_count("hs_a", retries=3)
        elif entrypoint == "page":
            fetcher._sina_page(1, retries=3)
        else:
            fetcher._sina_classic_batch(["sz000001"], retries=3)

    assert len(calls) == 1
    assert calls[0].get("stream") is True
    if mode == "streamed":
        assert response.closed


def test_sina_page_rejects_non_list_and_missing_schema(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    for text in ('{"error":"blocked"}', '[{"code":"1"}]'):
        monkeypatch.setattr(fetcher.requests, "get", lambda *_args, _text=text, **_kwargs: FakeResponse(_text))
        with pytest.raises(fetcher.QuoteFetchError):
            fetcher._sina_page(1, retries=1)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            '[{"code":"000001","code":"000002","name":"A","symbol":"sz000001","trade":"10"}]',
            "duplicate object key",
        ),
        ('[{"code":"000001","name":"A","symbol":"sz000001","trade":NaN}]', "non-finite"),
        ('[{"code":"000001","name":"A","symbol":"sz000001","trade":Infinity}]', "non-finite"),
        ('[{"code":"000001","name":"A","symbol":"sz000001","trade":-Infinity}]', "non-finite"),
    ],
)
def test_sina_page_rejects_ambiguous_or_nonfinite_json(monkeypatch, payload, message):
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: FakeResponse(payload))

    with pytest.raises(fetcher.QuoteFetchError, match=message):
        fetcher._sina_page(1, retries=1)


@pytest.mark.parametrize("payload", ["NaN", "Infinity", "-Infinity"])
def test_sina_count_rejects_nonfinite_json_constants(monkeypatch, payload):
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: FakeResponse(payload))

    with pytest.raises(fetcher.QuoteFetchError, match="non-finite"):
        fetcher._sina_count("hs_a", retries=1)


def test_sina_safety_limit_raises_instead_of_silently_truncating(monkeypatch):
    monkeypatch.setattr(fetcher, "_sina_page", lambda page, **_kwargs: quote_rows(page, 2))
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: 8)
    with pytest.raises(fetcher.QuoteFetchError, match="safety limit"):
        fetcher._collect_sina_node("hs_a", max_workers=2, page_size=2, max_pages=3)


def test_sina_collection_rejects_a_count_change_during_page_acquisition(monkeypatch):
    counts = iter((3, 4))
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: next(counts))
    monkeypatch.setattr(fetcher, "_sina_page", lambda page, **_kwargs: quote_rows(page, 1))

    with pytest.raises(fetcher.QuoteFetchError, match="row count changed during acquisition: 3 -> 4"):
        fetcher._collect_sina_node("hs_a", max_workers=3, page_size=1, max_pages=6)


def test_optional_sina_node_rechecks_an_initial_zero_count(monkeypatch):
    counts = iter((0, 1))
    monkeypatch.setattr(fetcher, "_sina_count", lambda _node, **_kwargs: next(counts))

    with pytest.raises(fetcher.QuoteFetchError, match="row count changed during acquisition: 0 -> 1"):
        fetcher._collect_sina_node("hk_ggt", allow_empty=True)


def test_sina_source_duplicate_is_rejected_before_zero_price_filtering(monkeypatch):
    rows = [
        {"code": "600001", "name": "A", "symbol": "sh600001", "trade": "10"},
        {"code": "600001", "name": "A duplicate", "symbol": "sh600001", "trade": "0", "settlement": "0"},
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)

    with pytest.raises(fetcher.QuoteFetchError, match="duplicate symbol/code identities"):
        fetcher._get_sina_quotes_parallel()


def test_sina_duplicate_beijing_source_row_is_rejected_even_when_price_is_zero(monkeypatch):
    rows = [
        {"code": "430047", "name": "BJ", "symbol": "bj430047", "trade": "0", "settlement": "0"},
        {"code": "430047", "name": "BJ duplicate", "symbol": "bj430047", "trade": "0", "settlement": "0"},
        {"code": "600001", "name": "A", "symbol": "sh600001", "trade": "10"},
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)

    with pytest.raises(fetcher.QuoteFetchError, match="duplicate symbol/code identities"):
        fetcher._get_sina_quotes_parallel()


def test_sina_source_must_preserve_requested_symbol_order(monkeypatch):
    rows = [
        {"code": "600002", "name": "B", "symbol": "sh600002", "trade": "10"},
        {"code": "600001", "name": "A", "symbol": "sh600001", "trade": "10"},
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)

    with pytest.raises(fetcher.QuoteFetchError, match="requested global symbol order"):
        fetcher._get_sina_quotes_parallel()


def test_sina_global_source_order_is_checked_before_beijing_exclusion(monkeypatch):
    rows = [
        {"code": "600001", "name": "A", "symbol": "sh600001", "trade": "10"},
        {"code": "430047", "name": "BJ", "symbol": "bj430047", "trade": "10"},
        {"code": "000001", "name": "B", "symbol": "sz000001", "trade": "10"},
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)

    with pytest.raises(fetcher.QuoteFetchError, match="requested global symbol order"):
        fetcher._get_sina_quotes_parallel()


def test_sina_beijing_prefix_cannot_hide_a_shanghai_identity(monkeypatch):
    rows = [
        {"code": "600001", "name": "mislabeled", "symbol": "bj600001", "trade": "10"},
        {"code": "000001", "name": "A", "symbol": "sz000001", "trade": "10"},
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)

    with pytest.raises(fetcher.QuoteFetchError, match="Beijing source row has an invalid code/symbol identity"):
        fetcher._get_sina_quotes_parallel()


@pytest.mark.parametrize(
    "row",
    [
        {"code": "000001", "name": "大写前缀", "symbol": "SZ000001", "trade": "10"},
        {"code": "000001", "name": "前后空格", "symbol": " sz000001", "trade": "10"},
        {"code": "０００００１", "name": "非ASCII代码", "symbol": "sz０００００１", "trade": "10"},
        {"code": "600001", "name": "市场错配", "symbol": "sz600001", "trade": "10"},
        {"code": "000001", "name": "代码错配", "symbol": "sz000002", "trade": "10"},
    ],
)
def test_sina_complete_source_requires_exact_ascii_code_symbol_identity(monkeypatch, row):
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: [row])

    with pytest.raises(fetcher.QuoteFetchError, match="canonical ASCII|invalid .*identity"):
        fetcher._get_sina_quotes_parallel()


@pytest.mark.parametrize("beijing_code", ["430047", "830832", "870199", "920047"])
def test_sina_valid_beijing_stock_ranges_are_safely_excluded(monkeypatch, beijing_code):
    rows = [
        {"code": beijing_code, "name": "北交所样本", "symbol": f"bj{beijing_code}", "trade": "10"},
        {"code": "000001", "name": "A", "symbol": "sz000001", "trade": "10"},
    ]
    monkeypatch.setattr(fetcher, "_collect_sina_node", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(fetcher, "_attach_sina_source_metadata", lambda frame: frame)

    result = fetcher._get_sina_quotes_parallel()

    assert result["code"].tolist() == ["000001"]
    assert result["market"].tolist() == ["SZ"]


@pytest.mark.parametrize("beijing_code", ["000001", "300001", "600001", "880001", "900001"])
def test_sina_invalid_beijing_stock_ranges_are_rejected(monkeypatch, beijing_code):
    monkeypatch.setattr(
        fetcher,
        "_collect_sina_node",
        lambda *_args, **_kwargs: [
            {"code": beijing_code, "name": "伪造北交所", "symbol": f"bj{beijing_code}", "trade": "10"}
        ],
    )

    with pytest.raises(fetcher.QuoteFetchError, match="Beijing source row has an invalid code/symbol identity"):
        fetcher._get_sina_quotes_parallel()


def test_quote_frame_uses_the_same_strict_beijing_stock_ranges():
    valid = fetcher._quotes_frame([{"code": "430047", "name": "历史北交所", "symbol": "bj430047", "trade": "10"}])
    assert valid[["market", "code"]].to_records(index=False).tolist() == [("BJ", "430047")]

    with pytest.raises(fetcher.QuoteFetchError, match="quote market/code identities disagree"):
        fetcher._quotes_frame([{"code": "880001", "name": "股转号段", "symbol": "bj880001", "trade": "10"}])


def test_quote_identity_is_market_plus_code():
    rows = [
        {"code": "000001", "name": "A", "symbol": "sz000001", "trade": "1"},
        {"code": "000001", "name": "HK", "symbol": "hk000001", "trade": "2"},
    ]
    frame = fetcher._quotes_frame(rows)
    assert set(zip(frame["market"], frame["code"])) == {("SZ", "000001"), ("HK", "000001")}


@pytest.mark.parametrize(
    "row",
    [
        {"code": "600001", "name": "错配", "symbol": "sh600000", "trade": "10"},
        {"code": "1", "name": "非规范", "symbol": "sz000001", "trade": "10"},
        {"code": "０００００１", "name": "非ASCII数字", "symbol": "sz０００００１", "trade": "10"},
        {"code": "000001", "name": "市场错配", "symbol": "sh000001", "trade": "10"},
    ],
)
def test_quote_rows_bind_canonical_code_to_the_exact_source_symbol(row):
    with pytest.raises(fetcher.QuoteFetchError, match="canonical|identities disagree"):
        fetcher._quotes_frame([row])


def test_duplicate_identity_across_pages_is_rejected():
    rows = [
        {"code": "000001", "name": "A", "symbol": "sz000001", "trade": "1"},
        {"code": "000001", "name": "A-new", "symbol": "sz000001", "trade": "3"},
    ]
    with pytest.raises(fetcher.QuoteFetchError, match="duplicate quote identities"):
        fetcher._quotes_frame(rows)


def test_financial_merge_sorts_all_series_and_never_deletes_valid_annual_report():
    income = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "1",
                "TOTAL_OPERATE_INCOME": 200,
                "PARENT_NETPROFIT": 10,
                "OPERATE_PROFIT": 20,
                "REPORT_DATE": "2025-12-31",
            },
            {
                "SECURITY_CODE": "1",
                "TOTAL_OPERATE_INCOME": 100,
                "PARENT_NETPROFIT": 8,
                "OPERATE_PROFIT": 15,
                "REPORT_DATE": "2024-12-31",
            },
        ]
    )
    cashflow = pd.DataFrame(
        [
            {"SECURITY_CODE": "1", "NETCASH_OPERATE": 50, "REPORT_DATE": "2025-12-31"},
            {"SECURITY_CODE": "1", "NETCASH_OPERATE": 30, "REPORT_DATE": "2023-12-31"},
            {"SECURITY_CODE": "1", "NETCASH_OPERATE": 40, "REPORT_DATE": "2024-12-31"},
        ]
    )
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "1",
                "TOTAL_EQUITY": 100,
                "PARENT_EQUITY": 90,
                "MINORITY_EQUITY": 10,
                "GOODWILL": 12,
                "SHORT_LOAN": 5,
                "LONG_TERM_LOAN": 7,
                "BOND_PAYABLE": 9,
                "NONCURRENT_LIABILITY_IN_1YEAR": 3,
                "LEASE_LIABILITY": 2,
                "SHORT_BOND_PAYABLE": 4,
                "BORROW_FUND": 6,
                "LOAN_PBC": 8,
                "SUBBOND_PAYABLE": 10,
                "REPORT_DATE": "2025-12-31",
            },
            {"SECURITY_CODE": "1", "TOTAL_EQUITY": 80, "REPORT_DATE": "2024-12-31"},
        ]
    )
    q1 = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "1",
                "PARENT_NETPROFIT": 100,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    merged = fetcher._merge_financials(income, cashflow, balance, q1, pd.DataFrame())
    company = merged["000001"]
    assert [row["REPORT_DATE"] for row in company["revenue_history"]] == [
        "2024-12-31",
        "2025-12-31",
    ]
    assert [row["REPORT_DATE"] for row in company["cashflow"]] == [
        "2023-12-31",
        "2024-12-31",
        "2025-12-31",
    ]
    assert [row["REPORT_DATE"] for row in company["balance"]] == [
        "2024-12-31",
        "2025-12-31",
    ]
    latest = company["balance"][-1]
    assert latest["PARENT_EQUITY"] == 90
    assert latest["GOODWILL"] == 12
    assert latest["LONG_LOAN"] == 7
    assert latest["BONDS_PAYABLE"] == 9
    assert latest["NONCURRENT_LIAB_1YEAR"] == 3
    assert latest["LEASE_LIAB"] == 2
    assert latest["SHORT_BONDS_PAYABLE"] == 4
    assert latest["BORROW_FUNDS"] == 6
    assert latest["CENTRAL_BANK_BORROWING"] == 8
    assert latest["SUBORDINATED_BONDS_PAYABLE"] == 10


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("TOTAL_OPERATE_INCOME", "not-a-number"),
        ("PARENT_NETPROFIT", float("inf")),
        ("OPERATE_PROFIT", True),
    ],
)
def test_financial_merge_rejects_invalid_annual_income_values(field, value):
    row = {
        "SECURITY_CODE": "000001",
        "TOTAL_OPERATE_INCOME": 100.0,
        "PARENT_NETPROFIT": 10.0,
        "OPERATE_PROFIT": 20.0,
        "REPORT_DATE": "2025-12-31",
    }
    row[field] = value

    with pytest.raises(
        fetcher.DataFetchError,
        match=rf"annual income {field}.*000001.*2025-12-31",
    ):
        fetcher._merge_financials(pd.DataFrame([row]), pd.DataFrame(), pd.DataFrame())


def test_financial_merge_keeps_genuine_blank_annual_income_values_missing():
    company = fetcher._merge_financials(
        pd.DataFrame(
            [
                {
                    "SECURITY_CODE": "000001",
                    "TOTAL_OPERATE_INCOME": None,
                    "PARENT_NETPROFIT": float("nan"),
                    "OPERATE_PROFIT": "",
                    "REPORT_DATE": "2025-12-31",
                }
            ]
        ),
        pd.DataFrame(),
        pd.DataFrame(),
    )["000001"]

    assert company["revenue_history"] == []
    assert company["income_history"] == []


def test_financial_merge_rejects_invalid_goodwill():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "GOODWILL": "not-a-number",
                "REPORT_DATE": "2025-12-31",
            }
        ]
    )

    with pytest.raises(
        fetcher.DataFetchError,
        match=r"annual balance GOODWILL.*000001.*2025-12-31",
    ):
        fetcher._merge_financials(pd.DataFrame(), pd.DataFrame(), balance)


def test_financial_merge_keeps_genuine_blank_goodwill_missing():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "GOODWILL": None,
                "REPORT_DATE": "2025-12-31",
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        balance,
    )["000001"]["balance"][0]

    assert row["GOODWILL"] is None


def test_financial_merge_fills_only_an_officially_proven_zero_revenue(monkeypatch):
    income = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "600610",
                "TOTAL_OPERATE_INCOME": None,
                "PARENT_NETPROFIT": -5.0,
                "OPERATE_PROFIT": -4.0,
                "REPORT_DATE": "2018-12-31",
            }
        ]
    )
    evidence = {
        ("600610", "2018-12-31"): {
            "evidence_type": "exchange_filed_explicit_zero",
            "metric": "TOTAL_OPERATE_INCOME",
            "value": 0.0,
            "source_url": "https://static.cninfo.com.cn/finalpage/2019-06-28/1206403533.PDF",
            "source_sha256": "a" * 64,
            "source_page": 7,
        }
    }
    monkeypatch.setattr(fetcher, "zero_revenue_evidence", lambda: evidence)

    company = fetcher._merge_financials(income, pd.DataFrame(), pd.DataFrame())["600610"]

    assert company["revenue_history"] == [
        {
            "TOTAL_OPERATE_INCOME": 0.0,
            "TOTAL_OPERATE_INCOME_EVIDENCE": evidence[("600610", "2018-12-31")],
            "REPORT_DATE": "2018-12-31",
        }
    ]
    assert company["income_history"][0]["TOTAL_OPERATE_INCOME"] == 0.0
    assert company["income_history"][0]["TOTAL_OPERATE_INCOME_EVIDENCE"]["source_page"] == 7


def test_financial_merge_rejects_official_zero_evidence_that_conflicts_with_source(monkeypatch):
    income = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "600610",
                "TOTAL_OPERATE_INCOME": 1.0,
                "PARENT_NETPROFIT": -5.0,
                "REPORT_DATE": "2018-12-31",
            }
        ]
    )
    monkeypatch.setattr(
        fetcher,
        "zero_revenue_evidence",
        lambda: {("600610", "2018-12-31"): {"value": 0.0}},
    )

    with pytest.raises(fetcher.DataFetchError, match="conflicts with source value"):
        fetcher._merge_financials(income, pd.DataFrame(), pd.DataFrame())


def test_financial_merge_preserves_five_year_parent_equity_history_for_pb():
    years = range(2021, 2026)
    income = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "TOTAL_OPERATE_INCOME": 100 + year,
                "PARENT_NETPROFIT": 10 + year,
                "REPORT_DATE": f"{year}-12-31",
            }
            for year in years
        ]
    )
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "TOTAL_EQUITY": 1_000 + year,
                "TOTAL_PARENT_EQUITY": 800 + year,
                "MINORITY_EQUITY": 200,
                "REPORT_DATE": f"{year}-12-31",
            }
            for year in reversed(list(years))
        ]
    )
    merged = fetcher._merge_financials(income, pd.DataFrame(), balance, pd.DataFrame(), pd.DataFrame())
    history = merged["000001"]["balance"]
    assert len(history) == 5
    assert [row["REPORT_DATE"] for row in history] == [f"{year}-12-31" for year in years]
    assert all(row["PARENT_EQUITY"] is not None for row in history)
    assert all(row["PARENT_EQUITY"] != row["TOTAL_EQUITY"] for row in history)


def test_financial_merge_replaces_mixed_002766_row_with_final_corrected_audit_lineage():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "002766",
                "REPORT_DATE": "2017-12-31",
                "TOTAL_ASSETS": 3_376_737_560.89,
                "TOTAL_LIABILITIES": 1_476_243_100.56,
                "TOTAL_EQUITY": 1_900_494_460.33,
                "TOTAL_PARENT_EQUITY": 1_559_788_411.30,
                "MINORITY_EQUITY": 19_838_702.97,
            }
        ]
    )

    company = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        balance,
        pd.DataFrame(),
        pd.DataFrame(),
    )["002766"]

    row = company["balance"][0]
    assert row["TOTAL_ASSETS"] == 3_660_176_992.98
    assert row["TOTAL_LIABILITIES"] == 2_012_750_505.12
    assert row["PARENT_EQUITY"] == 1_627_587_784.89
    assert row["TOTAL_PARENT_EQUITY"] == 1_627_587_784.89
    assert row["TOTAL_EQUITY"] == 1_647_426_487.86
    assert row["MINORITY_EQUITY"] == 19_838_702.97
    assert row["MONETARYFUNDS"] == 869_475_074.23
    assert row["SHORT_LOAN"] == 842_100_774.44
    assert row["DEBT_ASSET_RATIO"] == pytest.approx(2_012_750_505.12 / 3_660_176_992.98 * 100.0)
    assert row["REPORTED_PARENT_EQUITY"] == 1_559_788_411.30
    assert row["REPORTED_BALANCE_SHEET_VALUES"]["TOTAL_ASSETS"] == 3_376_737_560.89
    assert row["REPORTED_BALANCE_SHEET_VALUES"]["TOTAL_EQUITY"] == 1_900_494_460.33
    assert row["PARENT_EQUITY_SOURCE"] == "exchange_filed_official_override"
    assert row["BALANCE_SHEET_EVIDENCE"]["source_sha256"] == (
        "f142c1de83b2bc9f34ae5eb22a377ade5f2e2e23201a9526ac51eda482d3a8a6"
    )


def test_financial_merge_uses_audited_reverse_acquisition_comparator_for_600228():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "600228",
                "REPORT_DATE": "2020-12-31",
                "TOTAL_ASSETS": 1_083_606_330.96,
                "TOTAL_LIABILITIES": 266_334_531.46,
                "TOTAL_EQUITY": 817_271_799.50,
                "TOTAL_PARENT_EQUITY": 817_271_799.50,
                "MINORITY_EQUITY": 73_099_085.46,
                "MONETARYFUNDS": 888_088_128.13,
                "SHORT_LOAN": 10_000_000.0,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        balance,
        pd.DataFrame(),
        pd.DataFrame(),
    )["600228"]["balance"][0]

    assert row["TOTAL_ASSETS"] == 1_065_106_220.71
    assert row["TOTAL_LIABILITIES"] == 240_127_349.63
    assert row["TOTAL_EQUITY"] == 824_978_871.08
    assert row["PARENT_EQUITY"] == 824_978_871.08
    assert row["MINORITY_EQUITY"] == 0.0
    assert row["MONETARYFUNDS"] == 888_106_761.83
    assert row["SHORT_LOAN"] == 0.0
    assert "反向收购" in row["BALANCE_SHEET_EVIDENCE"]["reporting_basis"]
    assert row["REPORTED_BALANCE_SHEET_VALUES"]["MINORITY_EQUITY"] == 73_099_085.46


def test_financial_merge_does_not_expose_cached_balance_evidence_for_mutation():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "600228",
                "REPORT_DATE": "2020-12-31",
                "TOTAL_ASSETS": 1.0,
                "TOTAL_LIABILITIES": 0.0,
                "TOTAL_EQUITY": 1.0,
                "TOTAL_PARENT_EQUITY": 1.0,
                "MINORITY_EQUITY": 0.0,
            }
        ]
    )

    first = fetcher._merge_financials(pd.DataFrame(), pd.DataFrame(), balance)["600228"]["balance"][0]
    first["BALANCE_SHEET_EVIDENCE"]["canonical_values"]["TOTAL_ASSETS"] = 1.0
    first["BALANCE_SHEET_EVIDENCE"]["source_pages"].append(999)
    second = fetcher._merge_financials(pd.DataFrame(), pd.DataFrame(), balance)["600228"]["balance"][0]

    assert second["BALANCE_SHEET_EVIDENCE"]["canonical_values"]["TOTAL_ASSETS"] == 1_065_106_220.71
    assert second["BALANCE_SHEET_EVIDENCE"]["source_pages"] == [83, 84, 85]


def test_financial_merge_keeps_unreviewed_parent_equity_conflict_quarantined():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "002765",
                "REPORT_DATE": "2017-12-31",
                "TOTAL_EQUITY": 1_900_494_460.33,
                "TOTAL_PARENT_EQUITY": 1_559_788_411.30,
                "MINORITY_EQUITY": 19_838_702.97,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        balance,
        pd.DataFrame(),
        pd.DataFrame(),
    )["002765"]["balance"][0]

    assert row["PARENT_EQUITY"] is None
    assert row["REPORTED_PARENT_EQUITY"] == 1_559_788_411.30
    assert row["PARENT_EQUITY_SOURCE"] == "source_conflict_total_parent_minority"
    assert "BALANCE_SHEET_EVIDENCE" not in row


def test_financial_merge_derives_parent_equity_only_when_exact_components_are_nonconflicting():
    balance = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "REPORT_DATE": "2025-12-31",
                "TOTAL_EQUITY": 100.0,
                "MINORITY_EQUITY": 10.0,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        balance,
        pd.DataFrame(),
        pd.DataFrame(),
    )["000001"]["balance"][0]

    assert row["PARENT_EQUITY"] == 90.0
    assert row["REPORTED_PARENT_EQUITY"] is None
    assert row["PARENT_EQUITY_SOURCE"] == "derived_total_equity_minus_minority"


def test_financial_merge_preserves_current_and_prior_interim_without_calling_h1_q1():
    interim_income = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "TOTAL_OPERATE_INCOME": 80,
                "PARENT_NETPROFIT": 8,
                "REPORT_DATE": "2025-06-30",
            },
            {
                "SECURITY_CODE": "000001",
                "TOTAL_OPERATE_INCOME": 100,
                "PARENT_NETPROFIT": 11,
                "REPORT_DATE": "2026-06-30",
            },
        ]
    )
    interim_cashflow = pd.DataFrame(
        [
            {"SECURITY_CODE": "000001", "NETCASH_OPERATE": 7, "REPORT_DATE": "2025-06-30"},
            {"SECURITY_CODE": "000001", "NETCASH_OPERATE": 9, "REPORT_DATE": "2026-06-30"},
        ]
    )

    company = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        interim_income,
        interim_cashflow,
    )["000001"]

    assert [row["REPORT_DATE"] for row in company["income_interim"]] == [
        "2025-06-30",
        "2026-06-30",
    ]
    assert [row["REPORT_DATE"] for row in company["cashflow_interim"]] == [
        "2025-06-30",
        "2026-06-30",
    ]
    assert company["income_q1"] == []
    assert company["cashflow_q1"] == []


def test_financial_merge_derives_provenance_bound_zero_capex_from_detailed_identity():
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 7.0,
                # Real pandas frames represent this upstream blank as NaN.
                # The merge boundary must normalize it before asking the
                # detailed statement to prove a zero residual.
                "CONSTRUCT_LONG_ASSET": float("nan"),
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    detailed = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "REPORT_DATE": "2026-03-31",
                "CONSTRUCT_LONG_ASSET": None,
                "TOTAL_INVEST_OUTFLOW": 25.0,
                "INVEST_PAY_CASH": 10.0,
                "PAY_OTHER_INVEST": 15.0,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        cashflow_interim=interim_cashflow,
        detailed_cashflow_interim=detailed,
    )["000001"]["cashflow_interim"][0]

    assert row["CONSTRUCT_LONG_ASSET"] == 0.0
    assert row["CAPEX_PROVENANCE"]["evidence_label"] == "derived_calculation"
    assert row["CAPEX_PROVENANCE"]["derivation_method"] == "detailed_outflow_residual_zero"


def test_financial_merge_preserves_acquisition_cashflow_separately_from_capex():
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": 2.0,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    detailed = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "REPORT_DATE": "2026-03-31",
                "CONSTRUCT_LONG_ASSET": 2.0,
                "OBTAIN_SUBSIDIARY_OTHER": -6.0,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        cashflow_interim=interim_cashflow,
        detailed_cashflow_interim=detailed,
    )["000001"]["cashflow_interim"][0]

    assert row["CONSTRUCT_LONG_ASSET"] == 2.0
    assert row["OBTAIN_SUBSIDIARY_OTHER"] == -6.0


def test_financial_merge_rejects_invalid_acquisition_cashflow():
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": 2.0,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    detailed = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "REPORT_DATE": "2026-03-31",
                "CONSTRUCT_LONG_ASSET": 2.0,
                "OBTAIN_SUBSIDIARY_OTHER": "not-a-number",
            }
        ]
    )

    with pytest.raises(
        fetcher.DataFetchError,
        match=r"detailed interim cash-flow OBTAIN_SUBSIDIARY_OTHER.*000001.*2026-03-31",
    ):
        fetcher._merge_financials(
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            cashflow_interim=interim_cashflow,
            detailed_cashflow_interim=detailed,
        )


def test_financial_merge_keeps_genuine_blank_acquisition_cashflow_missing():
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": 2.0,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    detailed = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "REPORT_DATE": "2026-03-31",
                "CONSTRUCT_LONG_ASSET": 2.0,
                "OBTAIN_SUBSIDIARY_OTHER": None,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        cashflow_interim=interim_cashflow,
        detailed_cashflow_interim=detailed,
    )["000001"]["cashflow_interim"][0]

    assert row["OBTAIN_SUBSIDIARY_OTHER"] is None


def test_financial_merge_keeps_unresolved_interim_capex_missing_with_reason():
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "603435",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": None,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    detailed = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "603435",
                "REPORT_DATE": "2026-03-31",
                "CONSTRUCT_LONG_ASSET": None,
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        cashflow_interim=interim_cashflow,
        detailed_cashflow_interim=detailed,
    )["603435"]["cashflow_interim"][0]

    assert row["CONSTRUCT_LONG_ASSET"] is None
    assert row["CAPEX_PROVENANCE"]["status"] == "missing"
    assert row["CAPEX_PROVENANCE"]["reason"] == "missing_detailed_component:TOTAL_INVEST_INFLOW"


def test_financial_merge_fills_only_exact_official_q1_zero_capex(monkeypatch):
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": None,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    evidence = {
        "evidence_type": "exchange_filed_statement_zero",
        "metric": "CONSTRUCT_LONG_ASSET",
        "value": 0.0,
        "source_document": "2026年第一季度报告",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-29/1225223641.PDF",
        "source_sha256": "a" * 64,
        "source_page": 11,
        "source_statement": "本期资本开支和投资活动现金流出小计均为空。",
    }
    monkeypatch.setattr(fetcher, "zero_capex_evidence", lambda: {("000001", "2026-03-31"): evidence})

    row = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        cashflow_interim=interim_cashflow,
    )["000001"]["cashflow_interim"][0]

    assert row["CONSTRUCT_LONG_ASSET"] == 0.0
    assert row["CAPEX_PROVENANCE"]["evidence_label"] == "fact_official_report_zero"
    assert row["CAPEX_PROVENANCE"]["source_page"] == 11


def test_financial_merge_fills_only_exact_official_annual_zero_capex(monkeypatch):
    annual_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000670",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": None,
                "REPORT_DATE": "2019-12-31",
            }
        ]
    )
    evidence = {
        "evidence_type": "exchange_filed_statement_zero",
        "metric": "CONSTRUCT_LONG_ASSET",
        "value": 0.0,
        "source_document": "2019年年度报告全文",
        "source_url": "https://static.cninfo.com.cn/finalpage/2020-04-28/1207638298.PDF",
        "source_sha256": "a" * 64,
        "source_page": 75,
        "source_statement": "合并现金流量表2019年资本开支为空。",
    }
    monkeypatch.setattr(fetcher, "zero_capex_evidence", lambda: {("000670", "2019-12-31"): evidence})

    row = fetcher._merge_financials(
        pd.DataFrame(),
        annual_cashflow,
        pd.DataFrame(),
    )["000670"]["cashflow"][0]

    assert row["CONSTRUCT_LONG_ASSET"] == 0.0
    assert row["CAPEX_PROVENANCE"]["evidence_label"] == "fact_official_report_zero"
    assert row["CAPEX_PROVENANCE"]["source_report"] == "CNINFO_EXCHANGE_FILED_ANNUAL_REPORT"


def test_financial_merge_rejects_official_zero_capex_conflicting_with_source(monkeypatch):
    interim_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": 1.0,
                "REPORT_DATE": "2026-03-31",
            }
        ]
    )
    monkeypatch.setattr(
        fetcher,
        "zero_capex_evidence",
        lambda: {("000001", "2026-03-31"): {"value": 0.0}},
    )

    with pytest.raises(fetcher.DataFetchError, match="official zero-capex evidence conflicts"):
        fetcher._merge_financials(
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            cashflow_interim=interim_cashflow,
        )


def test_financial_merge_rejects_official_annual_zero_capex_conflicting_with_source(monkeypatch):
    annual_cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000670",
                "NETCASH_OPERATE": 7.0,
                "CONSTRUCT_LONG_ASSET": 1.0,
                "REPORT_DATE": "2019-12-31",
            }
        ]
    )
    monkeypatch.setattr(
        fetcher,
        "zero_capex_evidence",
        lambda: {("000670", "2019-12-31"): {"value": 0.0}},
    )

    with pytest.raises(fetcher.DataFetchError, match="conflicts with annual source"):
        fetcher._merge_financials(
            pd.DataFrame(),
            annual_cashflow,
            pd.DataFrame(),
        )


def test_financial_merge_attaches_reported_provenance_to_annual_capex():
    cashflow = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "NETCASH_OPERATE": 50.0,
                "CONSTRUCT_LONG_ASSET": 12.0,
                "REPORT_DATE": "2025-12-31",
            }
        ]
    )

    row = fetcher._merge_financials(
        pd.DataFrame(),
        cashflow,
        pd.DataFrame(),
    )["000001"]["cashflow"][0]

    assert row["CONSTRUCT_LONG_ASSET"] == 12.0
    assert row["CAPEX_PROVENANCE"]["evidence_label"] == "fact_source_reported"


def test_financial_merge_preserves_sorted_auditable_indicator_history():
    indicators = _indicator_frame("000001", "000001").iloc[::-1].reset_index(drop=True)
    company = fetcher._merge_financials(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        indicators=indicators,
    )["000001"]

    assert [row["REPORT_DATE"] for row in company["indicators"]] == ["2024-12-31", "2025-12-31"]
    latest = company["indicators"][-1]
    assert latest["SOURCE_REPORT_NAME"] == "RPT_F10_FINANCE_MAINFINADATA"
    assert latest["REPORT_DATE_NAME"] == "2025年报"
    assert latest["ROIC"] == 3.0
    assert latest["TOTAL_SHARE"] == 1_000_001


def test_financial_merge_rejects_duplicate_indicator_identity():
    indicators = _indicator_frame("000001")
    indicators = pd.concat([indicators, indicators], ignore_index=True)

    with pytest.raises(fetcher.DataFetchError, match="duplicate indicator identities"):
        fetcher._merge_financials(
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            indicators=indicators,
        )


def test_get_financials_filters_indicator_batch_to_requested_shanghai_shenzhen_codes(monkeypatch):
    indicators = _indicator_frame("000001", "920002", "600000")
    empty = pd.DataFrame()
    forwarded = []

    def annual(*, codes):
        forwarded.append(("annual", tuple(sorted(codes))))
        return empty.copy(), empty.copy(), empty.copy(), indicators.copy()

    def interim(*, codes):
        forwarded.append(("interim", tuple(sorted(codes))))
        return empty.copy(), empty.copy(), empty.copy()

    monkeypatch.setattr(
        fetcher,
        "fetch_all_financials_parallel",
        annual,
    )
    monkeypatch.setattr(
        fetcher,
        "fetch_interim_financials_parallel",
        interim,
    )

    result = fetcher.DataFetcher().get_financials(codes=["000001", "920002"])

    assert set(result) == {"000001"}
    assert result["000001"]["indicators"][0]["SOURCE_REPORT_NAME"] == "RPT_F10_FINANCE_MAINFINADATA"
    assert sorted(forwarded) == [
        ("annual", ("000001",)),
        ("interim", ("000001",)),
    ]


def test_get_financials_overlaps_independent_annual_and_interim_generations(monkeypatch):
    from threading import Barrier

    rendezvous = Barrier(2, timeout=5)
    empty = pd.DataFrame()

    def annual(**_kwargs):
        rendezvous.wait()
        return empty.copy(), empty.copy(), empty.copy(), empty.copy()

    def interim(**_kwargs):
        rendezvous.wait()
        return empty.copy(), empty.copy(), empty.copy()

    monkeypatch.setattr(fetcher, "fetch_all_financials_parallel", annual)
    monkeypatch.setattr(fetcher, "fetch_interim_financials_parallel", interim)

    assert fetcher.DataFetcher().get_financials(codes=["000001"]) == {}


def test_get_financials_with_only_beijing_codes_performs_no_financial_request(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher, "fetch_all_financials_parallel", lambda: calls.append(True))

    result = fetcher.DataFetcher().get_financials(codes=["920002", "830001"])

    assert result == {}
    assert calls == []


def test_data_fetcher_construction_has_no_sqlite_write_dependency():
    facade = fetcher.DataFetcher()
    assert not hasattr(facade, "cache")


def test_stock_list_defaults_to_a_shares_and_hk_codes_cannot_match_a_financials(monkeypatch):
    a_share = pd.DataFrame([{"code": "000001", "name": "A", "market": "SZ", "price": 1}])
    hk_share = pd.DataFrame([{"code": "000001", "name": "HK", "market": "HK", "price": 2}])
    hk_calls = []
    monkeypatch.setattr(fetcher, "_get_sina_quotes_parallel", lambda: a_share.copy())
    monkeypatch.setattr(
        fetcher,
        "_get_hk_stocks_via_tencent",
        lambda: hk_calls.append(True) or hk_share.copy(),
    )

    facade = fetcher.DataFetcher()
    default_quotes = facade.get_stock_list()
    assert not hk_calls
    assert default_quotes["code"].tolist() == ["000001"]
    assert default_quotes["financial_code"].tolist() == ["000001"]

    mixed = facade.get_stock_list(include_hk=True).set_index("market")
    assert mixed.loc["HK", "code"] == "HK:000001"
    assert mixed.loc["HK", "local_code"] == "000001"
    assert pd.isna(mixed.loc["HK", "financial_code"])
    assert not bool(mixed.loc["HK", "has_financials"])
    assert mixed.loc["SZ", "code"] == "000001"


def test_stock_list_defensively_removes_beijing_rows_from_an_older_quote_adapter(monkeypatch):
    rows = pd.DataFrame(
        [
            {"code": "000001", "name": "A", "market": "SZ", "price": 1},
            {"code": "920002", "name": "BJ", "market": "BJ", "price": 2},
        ]
    )
    monkeypatch.setattr(fetcher, "_get_sina_quotes_parallel", lambda: rows.copy())

    result = fetcher.DataFetcher().get_stock_list()

    assert result[["market", "code"]].to_records(index=False).tolist() == [("SZ", "000001")]
    assert result["financial_code"].tolist() == ["000001"]


def test_listing_date_evidence_is_bound_with_explicit_missing_status_and_high_coverage():
    from data.market_coldness import EASTMONEY_CLIST_ENDPOINT, EASTMONEY_SOURCE

    quotes = pd.DataFrame(
        [
            {
                "code": f"{600000 + index:06d}",
                "name": f"样本{index}",
                "market": "SH",
                "listing_date": None,
                "listing_date_status": None,
                "listing_date_source": None,
                "listing_date_source_url": None,
                "listing_date_retrieved_at": None,
                "source_trade_date": "2026-07-17",
            }
            for index in range(100)
        ]
    )
    records = tuple(
        SimpleNamespace(
            code=f"{600000 + index:06d}",
            listing_date=None if index == 99 else "2000-01-01",
            missing_reasons={"listing_date": "upstream_placeholder:f26"} if index == 99 else {},
            source=EASTMONEY_SOURCE,
            source_url=EASTMONEY_CLIST_ENDPOINT,
            retrieved_at="2026-07-17T00:00:00+00:00",
            turnover_rate_pct=1.0,
            volume_ratio=1.0,
        )
        for index in range(100)
    )

    result = fetcher._attach_listing_date_evidence(
        quotes,
        SimpleNamespace(available=True, records=records),
    )

    assert result["listing_date"].notna().sum() == 99
    assert result.iloc[-1]["listing_date_status"] == "upstream_placeholder:f26"
    assert result.iloc[-1]["listing_date_source_url"] == EASTMONEY_CLIST_ENDPOINT


def test_listing_date_enrichment_fails_closed_below_declared_date_coverage():
    from data.market_coldness import EASTMONEY_CLIST_ENDPOINT, EASTMONEY_SOURCE

    quotes = pd.DataFrame(
        [
            {
                "code": "600000",
                "market": "SH",
                "listing_date": None,
                "listing_date_status": None,
                "listing_date_source": None,
                "listing_date_source_url": None,
                "listing_date_retrieved_at": None,
                "source_trade_date": "2026-07-17",
            }
        ]
    )
    record = SimpleNamespace(
        code="600000",
        listing_date=None,
        missing_reasons={"listing_date": "upstream_placeholder:f26"},
        source=EASTMONEY_SOURCE,
        source_url=EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-17T00:00:00+00:00",
        turnover_rate_pct=1.0,
        volume_ratio=1.0,
    )

    with pytest.raises(fetcher.QuoteFetchError, match="listing-date coverage"):
        fetcher._attach_listing_date_evidence(
            quotes,
            SimpleNamespace(available=True, records=(record,)),
        )


def test_listing_date_enrichment_rejects_low_active_reference_reverse_coverage():
    quotes = pd.DataFrame(
        [
            {
                "code": f"{600000 + index:06d}",
                "market": "SH",
                "source_trade_date": "2026-07-17",
                "listing_date": None,
                "listing_date_status": None,
                "listing_date_source": None,
                "listing_date_source_url": None,
                "listing_date_retrieved_at": None,
            }
            for index in range(98)
        ]
    )
    records = tuple(
        SimpleNamespace(
            code=f"{600000 + index:06d}",
            listing_date="2000-01-01",
            missing_reasons={},
            source="reference",
            source_url="https://example.test/reference",
            retrieved_at="2026-07-17T00:00:00+00:00",
            turnover_rate_pct=1.0,
            volume_ratio=1.0,
        )
        for index in range(100)
    )

    with pytest.raises(fetcher.QuoteFetchError, match="reverse quote coverage 98.0% is below 99.0%"):
        fetcher._attach_listing_date_evidence(
            quotes,
            SimpleNamespace(available=True, records=records),
        )


def test_listing_date_reverse_coverage_excludes_explicit_future_listings():
    quotes = pd.DataFrame(
        [
            {
                "code": f"{600000 + index:06d}",
                "market": "SH",
                "source_trade_date": "2026-07-17",
                "listing_date": None,
                "listing_date_status": None,
                "listing_date_source": None,
                "listing_date_source_url": None,
                "listing_date_retrieved_at": None,
            }
            for index in range(99)
        ]
    )
    records = tuple(
        SimpleNamespace(
            code=f"{600000 + index:06d}",
            listing_date="2026-07-18" if index == 99 else "2000-01-01",
            missing_reasons={},
            source="reference",
            source_url="https://example.test/reference",
            retrieved_at="2026-07-17T00:00:00+00:00",
            turnover_rate_pct=1.0,
            volume_ratio=1.0,
        )
        for index in range(100)
    )

    result = fetcher._attach_listing_date_evidence(
        quotes,
        SimpleNamespace(available=True, records=records),
    )

    assert len(result) == 99
    assert result["listing_date_status"].eq("reported").all()
