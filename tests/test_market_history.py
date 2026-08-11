from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest
import requests

from data.cache import SafeFileCache
from data.market_history import (
    BLUME_RAW_WEIGHT,
    LOOKBACK_WEEKLY_RETURNS,
    MarketHistoryError,
    TencentWeeklyHistoryAdapter,
    WeeklyClose,
    calculate_weekly_market_beta,
    estimate_market_beta,
)


def _bars_from_returns(returns, *, start=date(2023, 6, 30), initial=100.0):
    prices = [float(initial)]
    for value in returns:
        prices.append(prices[-1] * (1.0 + float(value)))
    return [WeeklyClose(start + timedelta(days=7 * index), price) for index, price in enumerate(prices)]


def _linear_beta_bars(beta=1.5, alpha=0.001):
    benchmark_returns = np.linspace(-0.04, 0.04, LOOKBACK_WEEKLY_RETURNS)
    stock_returns = alpha + beta * benchmark_returns
    return _bars_from_returns(stock_returns), _bars_from_returns(benchmark_returns)


def test_pure_beta_uses_fixed_156_returns_independent_winsor_and_blume_adjustment():
    stock, benchmark = _linear_beta_bars(beta=1.5, alpha=0.001)
    as_of = stock[-1].trade_date

    result = calculate_weekly_market_beta(stock, benchmark, code="600519", as_of=as_of)

    assert result.available
    assert result.price_observations == 157
    assert result.sample_size == 156
    assert result.start_date == stock[0].trade_date.isoformat()
    assert result.end_date == as_of.isoformat()
    assert result.raw_beta == pytest.approx(1.5, abs=1e-11)
    assert result.blume_beta == pytest.approx(BLUME_RAW_WEIGHT * 1.5 + (1.0 - BLUME_RAW_WEIGHT), abs=1e-11)
    assert result.r_squared == pytest.approx(1.0, abs=1e-12)


def test_pure_beta_matches_manual_per_column_one_and_ninety_nine_percent_winsorization():
    benchmark_returns = np.sin(np.linspace(0.0, 9.0, LOOKBACK_WEEKLY_RETURNS)) * 0.025
    stock_returns = 0.001 + 0.8 * benchmark_returns
    stock_returns[10] = 0.45
    benchmark_returns[120] = -0.35
    stock = _bars_from_returns(stock_returns)
    benchmark = _bars_from_returns(benchmark_returns)

    result = calculate_weekly_market_beta(stock, benchmark, code="600519", as_of=stock[-1].trade_date)

    observed_stock = np.asarray([item.close for item in stock])
    observed_market = np.asarray([item.close for item in benchmark])
    returns = np.column_stack(
        (
            observed_stock[1:] / observed_stock[:-1] - 1.0,
            observed_market[1:] / observed_market[:-1] - 1.0,
        )
    )
    lower = np.quantile(returns, 0.01, axis=0, method="linear")
    upper = np.quantile(returns, 0.99, axis=0, method="linear")
    clipped = np.clip(returns, lower, upper)
    stock_centered = clipped[:, 0] - clipped[:, 0].mean()
    market_centered = clipped[:, 1] - clipped[:, 1].mean()
    expected_beta = float(np.dot(stock_centered, market_centered) / np.dot(market_centered, market_centered))

    assert result.available
    assert result.raw_beta == pytest.approx(expected_beta, abs=1e-12)


def test_pure_beta_does_not_turn_insufficient_or_zero_variance_evidence_into_zero():
    short_returns = np.linspace(-0.02, 0.02, LOOKBACK_WEEKLY_RETURNS - 1)
    short = _bars_from_returns(short_returns)
    insufficient = calculate_weekly_market_beta(short, short, code="600519", as_of=short[-1].trade_date)

    assert not insufficient.available
    assert insufficient.reason == "insufficient_aligned_prices:156/157"
    assert insufficient.raw_beta is None
    assert insufficient.blume_beta is None
    assert insufficient.r_squared is None

    stock, _ = _linear_beta_bars()
    flat_benchmark = _bars_from_returns(np.zeros(LOOKBACK_WEEKLY_RETURNS))
    zero_variance = calculate_weekly_market_beta(
        stock,
        flat_benchmark,
        code="600519",
        as_of=stock[-1].trade_date,
    )
    assert not zero_variance.available
    assert zero_variance.reason == "zero_benchmark_return_variance"
    assert zero_variance.raw_beta is None


def test_weekly_close_and_pure_calculator_reject_malformed_or_duplicate_observations():
    with pytest.raises(TypeError, match="date"):
        WeeklyClose("2026-07-15", 100.0)
    with pytest.raises(ValueError, match="positive"):
        WeeklyClose(date(2026, 7, 15), 0.0)

    stock, benchmark = _linear_beta_bars()
    duplicated = [*stock, stock[-1]]
    with pytest.raises(ValueError, match="duplicate"):
        calculate_weekly_market_beta(
            duplicated,
            benchmark,
            code="600519",
            as_of=stock[-1].trade_date,
        )


class _FakeResponse:
    def __init__(self, payload=None, *, raw=None, url="https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"):
        self.content = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.content))}
        self.closed = False
        self.url = url

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class _FakeHttpClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.responses = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = _FakeResponse(self.payload)
        self.responses.append(response)
        return response


class _SequenceHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class _NoWait:
    def acquire(self):
        return None


class _StatusResponse(_FakeResponse):
    def __init__(self, status, *, retry_after=None):
        super().__init__({"code": status})
        self.status_code = status
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)

    def raise_for_status(self):
        raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _tencent_payload(symbol="sh600519", *, key="qfqweek"):
    return {
        "code": 0,
        "msg": "",
        "data": {
            symbol: {
                key: [
                    ["2026-07-03", "100", "101", "102", "99", "12345"],
                    [
                        "2026-07-10",
                        "101",
                        "103",
                        "104",
                        "100",
                        "23456",
                        {"djr": "2026-07-09"},
                    ],
                ]
            }
        },
    }


def test_tencent_adapter_requests_qfq_weekly_contract_and_parses_only_normalized_closes():
    client = _FakeHttpClient(_tencent_payload())
    adapter = TencentWeeklyHistoryAdapter(http_client=client, retries=1)

    bars = adapter.fetch_weekly_closes(
        "sh600519",
        date(2026, 7, 15),
        require_forward_adjusted=True,
    )

    assert bars == [WeeklyClose(date(2026, 7, 3), 101.0), WeeklyClose(date(2026, 7, 10), 103.0)]
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url.endswith("/appstock/app/fqkline/get")
    assert kwargs["params"] == {"param": "sh600519,week,,2026-07-15,220,qfq"}
    assert kwargs["stream"] is True
    assert client.responses[0].closed


def test_tencent_adapter_honours_retry_after_and_stops_on_terminal_http(monkeypatch):
    busy = _StatusResponse(429, retry_after=5)
    winner = _FakeResponse(_tencent_payload())
    client = _SequenceHttpClient([busy, winner])
    sleeps = []
    monkeypatch.setattr("data.market_history.time.sleep", sleeps.append)
    adapter = TencentWeeklyHistoryAdapter(http_client=client, rate_limiter=_NoWait())

    bars = adapter.fetch_weekly_closes("sh600519", date(2026, 7, 15), require_forward_adjusted=True)

    assert len(bars) == 2
    assert sleeps == [5]
    assert busy.closed and winner.closed

    missing = _StatusResponse(404)
    terminal = _SequenceHttpClient([missing])
    adapter = TencentWeeklyHistoryAdapter(http_client=terminal, rate_limiter=_NoWait())
    with pytest.raises(MarketHistoryError, match="HTTP 404"):
        adapter.fetch_weekly_closes("sh600519", date(2026, 7, 15), require_forward_adjusted=True)
    assert len(terminal.calls) == 1
    assert missing.closed


def test_tencent_adapter_requires_qfq_rows_for_stock_but_accepts_index_week_rows():
    stock_client = _FakeHttpClient(_tencent_payload(key="week"))
    stock_adapter = TencentWeeklyHistoryAdapter(http_client=stock_client, retries=1)
    with pytest.raises(MarketHistoryError, match="forward-adjusted"):
        stock_adapter.fetch_weekly_closes(
            "sh600519",
            date(2026, 7, 15),
            require_forward_adjusted=True,
        )

    index_client = _FakeHttpClient(_tencent_payload(symbol="sh000300", key="week"))
    index_adapter = TencentWeeklyHistoryAdapter(http_client=index_client, retries=1)
    bars = index_adapter.fetch_weekly_closes(
        "sh000300",
        date(2026, 7, 15),
        require_forward_adjusted=False,
    )
    assert len(bars) == 2


def test_tencent_adapter_rejects_invalid_ohlc_and_closes_every_response():
    payload = _tencent_payload()
    payload["data"]["sh600519"]["qfqweek"][0][2] = "200"  # close exceeds the declared high
    client = _FakeHttpClient(payload)
    adapter = TencentWeeklyHistoryAdapter(http_client=client, retries=1)

    with pytest.raises(MarketHistoryError, match="open/close"):
        adapter.fetch_weekly_closes(
            "sh600519",
            date(2026, 7, 15),
            require_forward_adjusted=True,
        )
    assert client.responses[0].closed


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"retries": 1.5}, "retries"),
        ({"bar_limit": 220.5}, "bar_limit"),
        ({"bar_limit": True}, "bar_limit"),
        ({"retry_delay": True}, "retry_delay"),
    ],
)
def test_tencent_adapter_rejects_coerced_boolean_or_fractional_contract_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TencentWeeklyHistoryAdapter(**kwargs)


def test_tencent_adapter_rejects_insecure_redirects_duplicate_keys_and_nonfinite_json():
    class RawClient:
        def __init__(self, raw, url):
            self.raw = raw
            self.url = url

        def get(self, *_args, **_kwargs):
            return _FakeResponse(raw=self.raw, url=self.url)

    valid = json.dumps(_tencent_payload(), separators=(",", ":")).encode()
    with pytest.raises(MarketHistoryError, match="HTTPS"):
        TencentWeeklyHistoryAdapter(
            http_client=RawClient(valid, "http://redirect.example.test/history"), retries=1
        ).fetch_weekly_closes("sh600519", date(2026, 7, 15), require_forward_adjusted=True)

    duplicate = valid.replace(b'"code":0', b'"code":0,"code":0', 1)
    with pytest.raises(MarketHistoryError, match="duplicate key"):
        TencentWeeklyHistoryAdapter(
            http_client=RawClient(duplicate, "https://example.test/history"), retries=1
        ).fetch_weekly_closes("sh600519", date(2026, 7, 15), require_forward_adjusted=True)

    nonfinite = valid.replace(b'"code":0', b'"code":NaN', 1)
    with pytest.raises(MarketHistoryError, match="non-finite"):
        TencentWeeklyHistoryAdapter(
            http_client=RawClient(nonfinite, "https://example.test/history"), retries=1
        ).fetch_weekly_closes("sh600519", date(2026, 7, 15), require_forward_adjusted=True)


class _FixedAdapter:
    def __init__(self, stock, benchmark, *, error=None):
        self.stock = stock
        self.benchmark = benchmark
        self.error = error
        self.calls = []

    def fetch_weekly_closes(self, symbol, as_of, *, require_forward_adjusted):
        self.calls.append((symbol, as_of, require_forward_adjusted))
        if self.error is not None:
            raise self.error
        return self.stock if require_forward_adjusted else self.benchmark


def test_orchestrator_returns_structured_unavailable_result_on_transport_failure(tmp_path):
    adapter = _FixedAdapter([], [], error=MarketHistoryError("offline"))

    result = estimate_market_beta(
        "600519",
        "2026-07-15",
        adapter=adapter,
        cache_dir=tmp_path,
        use_cache=False,
    )

    assert not result.available
    assert result.raw_beta is None
    assert result.blume_beta is None
    assert result.r_squared is None
    assert result.reason.startswith("source_unavailable:MarketHistoryError:offline")
    assert result.cache_diagnostic == "disabled"
    assert not list(tmp_path.iterdir())


def test_cache_is_keyed_by_code_and_as_of_and_replays_bars_without_network(tmp_path):
    stock, benchmark = _linear_beta_bars(beta=1.25)
    cutoff = stock[-1].trade_date
    first_adapter = _FixedAdapter(stock, benchmark)

    first = estimate_market_beta(
        "600519",
        cutoff,
        adapter=first_adapter,
        cache_dir=tmp_path,
    )

    assert first.available
    assert not first.cache_hit
    assert first.cache_diagnostic.endswith(";saved")
    assert len(first_adapter.calls) == 2
    artifacts = list(tmp_path.glob("*.json.gz"))
    assert len(artifacts) == 1
    loaded = SafeFileCache(artifacts[0], schema_version=1, max_uncompressed_bytes=2 * 1024 * 1024).load()
    assert loaded.hit, loaded.reason
    assert loaded.value["contract"]["code"] == "600519"
    assert loaded.value["contract"]["as_of"] == cutoff.isoformat()

    offline_adapter = _FixedAdapter([], [], error=MarketHistoryError("must not be called"))
    second = estimate_market_beta(
        "600519",
        cutoff,
        adapter=offline_adapter,
        cache_dir=tmp_path,
    )

    assert second.available
    assert second.cache_hit
    assert second.cache_diagnostic == "hit"
    assert second.raw_beta == first.raw_beta
    assert second.blume_beta == first.blume_beta
    assert second.r_squared == first.r_squared
    assert offline_adapter.calls == []

    earlier_cutoff = cutoff - timedelta(days=7)
    earlier_adapter = _FixedAdapter(stock, benchmark)
    earlier = estimate_market_beta(
        "600519",
        earlier_cutoff,
        adapter=earlier_adapter,
        cache_dir=tmp_path,
    )
    assert not earlier.available  # 156 prices remain after the explicit cutoff.
    assert len(earlier_adapter.calls) == 2
    assert len(list(tmp_path.glob("*.json.gz"))) == 1  # unavailable evidence is not cached.


def test_semantically_invalid_but_checksummed_cache_is_refetched_and_replaced(tmp_path):
    stock, benchmark = _linear_beta_bars(beta=0.75)
    cutoff = stock[-1].trade_date
    first = estimate_market_beta(
        "600519",
        cutoff,
        adapter=_FixedAdapter(stock, benchmark),
        cache_dir=tmp_path,
    )
    assert first.available
    artifact = next(tmp_path.glob("*.json.gz"))

    SafeFileCache(artifact, schema_version=1).save({"unexpected": []})
    replacement_adapter = _FixedAdapter(stock, benchmark)
    replacement = estimate_market_beta(
        "600519",
        cutoff,
        adapter=replacement_adapter,
        cache_dir=tmp_path,
    )

    assert replacement.available
    assert not replacement.cache_hit
    assert replacement.cache_diagnostic.startswith("invalid_hit:")
    assert replacement.cache_diagnostic.endswith(";saved")
    assert len(replacement_adapter.calls) == 2


def test_public_orchestrator_rejects_beijing_and_noncanonical_codes_before_io(tmp_path):
    adapter = _FixedAdapter([], [], error=AssertionError("should not be called"))
    for code in ("920001", "830001", "60051", "sh600519"):
        with pytest.raises(ValueError):
            estimate_market_beta(code, "2026-07-15", adapter=adapter, cache_dir=tmp_path)
    assert adapter.calls == []


def test_public_orchestrator_rejects_future_cutoff_before_io(tmp_path):
    adapter = _FixedAdapter([], [], error=AssertionError("should not be called"))
    with pytest.raises(ValueError, match="future"):
        estimate_market_beta(
            "600519",
            date.today() + timedelta(days=1),
            adapter=adapter,
            cache_dir=tmp_path,
        )
    assert adapter.calls == []
