from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data.capex_evidence import resolve_capex_evidence
from data.cache import SafeCacheError, SafeFileCache
from data.market_coldness import EASTMONEY_CLIST_ENDPOINT, EASTMONEY_SOURCE
from data.snapshot import (
    MAX_QUOTE_RETRIEVAL_AGE_SECONDS,
    MAX_QUOTE_RETRIEVAL_SPAN_SECONDS,
    MAX_SOURCE_QUOTE_AGE_SECONDS,
    MAX_STALE_AGE_SECONDS,
    MarketSnapshotOutcome,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotGenerationConflict,
    SnapshotUnavailableError,
    get_market_snapshot,
    save_market_snapshot,
    validate_market_snapshot,
)


# 2026-07-15 12:00 Asia/Shanghai: Q1 is the latest filed interim period.
NOW = 1_784_088_000.0


def _cashflow_row(report_date: str, operating_cash_flow: float, capex: float) -> dict:
    value, provenance = resolve_capex_evidence(capex, None, report_date=report_date)
    return {
        "REPORT_DATE": report_date,
        "NETCASH_OPERATE": operating_cash_flow,
        "CONSTRUCT_LONG_ASSET": value,
        "CAPEX_PROVENANCE": provenance,
    }


def _quotes(count: int = 2) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": f"{index + 1:06d}",
                "name": f"样本{index + 1}",
                "market": "SZ",
                "price": 10.0 + index,
                "trade_price": 10.0 + index,
                "reference_price": 10.0 + index,
                "price_source": "last_trade",
                "quote_status": "trading",
                "retrieved_at": NOW,
                "quote_tick_time": "11:59:00",
                "source_trade_date": "2026-07-15",
                "pe": 12.0,
                "pb": 1.2,
                "market_cap": 1_000_000_000.0 + index,
            }
            for index in range(count)
        ]
    )


def _with_listing_evidence(quotes: pd.DataFrame, dates: list[str | None]) -> pd.DataFrame:
    result = quotes.copy()
    result["listing_date"] = dates
    result["listing_date_status"] = ["reported" if value is not None else "upstream_placeholder:f26" for value in dates]
    result["listing_date_source"] = EASTMONEY_SOURCE
    result["listing_date_source_url"] = EASTMONEY_CLIST_ENDPOINT
    result["listing_date_retrieved_at"] = datetime.fromtimestamp(NOW, tz=ZoneInfo("UTC")).isoformat()
    return result


def _financials(count: int = 2) -> dict[str, dict]:
    return {
        f"{index + 1:06d}": {
            "cashflow": [_cashflow_row(f"{year}-12-31", 10.0, 2.0) for year in range(2023, 2026)],
            "balance": [
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_ASSETS": 100.0,
                    "TOTAL_PARENT_EQUITY": 70.0,
                    "GOODWILL": 0.0,
                }
                for year in range(2022, 2026)
            ],
            "revenue_history": [
                {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": 50.0} for year in range(2022, 2026)
            ],
            "income_history": [
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_OPERATE_INCOME": 50.0,
                    "PARENT_NETPROFIT": 5.0,
                }
                for year in range(2022, 2026)
            ],
            "income_q1": [
                {
                    "REPORT_DATE": "2025-03-31",
                    "PARENT_NETPROFIT": 0.8,
                    "TOTAL_OPERATE_INCOME": 9.0,
                },
                {
                    "REPORT_DATE": "2026-03-31",
                    "PARENT_NETPROFIT": 1.0,
                    "TOTAL_OPERATE_INCOME": 10.0,
                },
            ],
            "cashflow_q1": [
                _cashflow_row("2025-03-31", 0.8, 0.2),
                _cashflow_row("2026-03-31", 1.0, 0.3),
            ],
            "income_interim": [
                {
                    "REPORT_DATE": "2025-03-31",
                    "PARENT_NETPROFIT": 0.8,
                    "TOTAL_OPERATE_INCOME": 9.0,
                },
                {
                    "REPORT_DATE": "2026-03-31",
                    "PARENT_NETPROFIT": 1.0,
                    "TOTAL_OPERATE_INCOME": 10.0,
                },
            ],
            "cashflow_interim": [
                {
                    **_cashflow_row("2025-03-31", 0.8, 0.2),
                    "OBTAIN_SUBSIDIARY_OTHER": 0.0,
                },
                {
                    **_cashflow_row("2026-03-31", 1.0, 0.3),
                    "OBTAIN_SUBSIDIARY_OTHER": 0.0,
                },
            ],
            "indicators": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "ROIC": 12.0,
                }
            ],
        }
        for index in range(count)
    }


def _timestamp(year: int, month: int, day: int) -> float:
    return datetime(year, month, day, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def _move_quote_generation(quotes: pd.DataFrame, timestamp: float) -> pd.DataFrame:
    moved = quotes.copy()
    local = datetime.fromtimestamp(timestamp, tz=ZoneInfo("Asia/Shanghai"))
    moved["retrieved_at"] = timestamp
    moved["source_trade_date"] = local.strftime("%Y-%m-%d")
    moved["quote_tick_time"] = "11:59:00"
    return moved


def _set_interim_period(
    financials: dict[str, dict],
    current_report_date: str,
    previous_report_date: str,
) -> dict[str, dict]:
    updated = deepcopy(financials)
    for company in updated.values():
        company["income_interim"] = [
            {
                "REPORT_DATE": previous_report_date,
                "PARENT_NETPROFIT": 0.8,
                "TOTAL_OPERATE_INCOME": 9.0,
            },
            {
                "REPORT_DATE": current_report_date,
                "PARENT_NETPROFIT": 1.0,
                "TOTAL_OPERATE_INCOME": 10.0,
            },
        ]
        company["cashflow_interim"] = [
            {
                **_cashflow_row(previous_report_date, 0.8, 0.2),
                "OBTAIN_SUBSIDIARY_OTHER": 0.0,
            },
            {
                **_cashflow_row(current_report_date, 1.0, 0.3),
                "OBTAIN_SUBSIDIARY_OTHER": 0.0,
            },
        ]
    return updated


def _financials_for_codes(codes: list[str]) -> dict[str, dict]:
    values = list(_financials(len(codes)).values())
    return {code: deepcopy(value) for code, value in zip(codes, values)}


class _Fetcher:
    def __init__(self, quotes=None, financials=None, error: Exception | None = None):
        self.quotes = quotes
        self.financials = financials
        self.error = error
        self.quote_calls = 0
        self.financial_calls = 0

    def get_stock_list(self, *, include_hk: bool):
        assert include_hk is False
        self.quote_calls += 1
        if self.error:
            raise self.error
        return self.quotes

    def get_financials(self, *, codes):
        self.financial_calls += 1
        assert codes == self.quotes["code"].tolist()
        return self.financials


def test_snapshot_validation_rejects_duplicate_or_non_finite_quotes():
    quotes = _quotes()
    quotes.loc[1, "code"] = quotes.loc[0, "code"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_reports_observed_annual_history_profile_without_guessing_listing_age():
    validation = validate_market_snapshot(
        _quotes(),
        _financials(),
        min_quotes=2,
        min_financial_coverage=0.5,
    )

    profile = validation["annual_history_profile"]
    assert profile["population"] == "SH_SZ_quote_universe"
    assert "listing-date evidence" in profile["classification_limit"]
    assert {dataset: details["max_periods"] for dataset, details in profile["datasets"].items()} == {
        "revenue_history": 4,
        "income_history": 4,
        "cashflow": 3,
        "balance": 4,
        "indicators": 1,
    }
    assert profile["datasets"]["revenue_history"]["internal_gap_companies"] == 0


def test_snapshot_history_profile_uses_only_bound_listing_date_evidence_to_narrow_window():
    quotes = _with_listing_evidence(_quotes(), ["2024-01-01", "2000-01-01"])
    financials = _financials()
    for dataset in ("revenue_history", "income_history", "cashflow", "balance"):
        financials["000001"][dataset] = [
            row for row in financials["000001"][dataset] if str(row["REPORT_DATE"]).startswith(("2024", "2025"))
        ]

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=2,
        min_financial_coverage=0.5,
    )

    evidence = validation["listing_date_evidence"]
    profile = validation["annual_history_profile"]
    assert evidence["listing_date_coverage"] == 1.0
    assert profile["schema_version"] == 2
    assert profile["listing_date_evidence_count"] == 2
    revenue = profile["datasets"]["revenue_history"]
    assert revenue["missing_observations_without_listing_adjustment"] == 2
    assert revenue["listing_adjusted_missing_observations"] == 0
    assert revenue["pre_listing_observations_excluded"] == 2


def test_snapshot_rejects_listing_date_later_than_quote_trade_date():
    quotes = _with_listing_evidence(_quotes(), ["2026-07-16", "2000-01-01"])

    with pytest.raises(ValueError, match="later than the quote trade date"):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


@pytest.mark.parametrize(("column", "value", "message"), [("pe", 1e250, "PE outside"), ("pb", -1e250, "PB outside")])
def test_snapshot_rejects_extreme_but_finite_valuation_multiples(column, value, message):
    quotes = _quotes()
    quotes.loc[0, column] = value

    with pytest.raises(ValueError, match=message):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("price", 1e250, "price above"), ("market_cap", 9e15, "single-company market_cap")],
)
def test_snapshot_rejects_extreme_first_generation_price_or_market_cap(field, value, message):
    quotes = _quotes()
    quotes.loc[0, field] = value
    if field == "price":
        quotes.loc[0, ["trade_price", "reference_price"]] = value

    with pytest.raises(ValueError, match=message):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_rejects_financial_unit_explosions_and_equity_identity_conflicts():
    exploded = _financials()
    exploded["000001"]["income_history"][0]["PARENT_NETPROFIT"] = 1e250
    with pytest.raises(ValueError, match="source-integrity magnitude"):
        validate_market_snapshot(_quotes(), exploded, min_quotes=2, min_financial_coverage=0.5)

    contradictory = _financials()
    row = contradictory["000001"]["balance"][-1]
    row.update(
        {
            "TOTAL_ASSETS": 2_000.0,
            "TOTAL_PARENT_EQUITY": 1_500.0,
            "TOTAL_EQUITY": 100.0,
            "MINORITY_EQUITY": 10.0,
        }
    )
    with pytest.raises(ValueError, match=r"total=parent\+minority"):
        validate_market_snapshot(_quotes(), contradictory, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_accepts_extreme_debt_ratio_when_exact_components_prove_negative_equity():
    financials = _financials()
    financials["000001"]["balance"][-1].update(
        {
            "TOTAL_ASSETS": 10.0,
            "TOTAL_LIABILITIES": 210.0,
            "TOTAL_EQUITY": -200.0,
            "TOTAL_PARENT_EQUITY": -200.0,
            "MINORITY_EQUITY": 0.0,
            "DEBT_ASSET_RATIO": 2_100.0,
        }
    )

    validation = validate_market_snapshot(
        _quotes(),
        financials,
        min_quotes=2,
        min_financial_coverage=0.5,
    )

    assert validation["financials"] == 2


def test_snapshot_rejects_debt_ratio_that_disagrees_with_exact_components():
    financials = _financials()
    financials["000001"]["balance"][-1].update(
        {
            "TOTAL_ASSETS": 10.0,
            "TOTAL_LIABILITIES": 210.0,
            "TOTAL_EQUITY": -200.0,
            "TOTAL_PARENT_EQUITY": -200.0,
            "MINORITY_EQUITY": 0.0,
            "DEBT_ASSET_RATIO": 9_999.0,
        }
    )

    with pytest.raises(ValueError, match="inconsistent debt/asset ratio"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_accepts_zero_assets_when_liabilities_and_negative_equity_prove_the_identity():
    financials = _financials()
    financials["000001"]["balance"][0].update(
        {
            "TOTAL_ASSETS": 0.0,
            "TOTAL_LIABILITIES": 554_168_867.29,
            "TOTAL_EQUITY": -554_168_867.29,
            "TOTAL_PARENT_EQUITY": -554_168_867.29,
            "DEBT_ASSET_RATIO": None,
        }
    )

    validation = validate_market_snapshot(
        _quotes(),
        financials,
        min_quotes=2,
        min_financial_coverage=0.5,
    )

    assert validation["financials"] == 2


def test_snapshot_rejects_uninformative_all_zero_balance_placeholder():
    financials = _financials()
    financials["000001"]["balance"][0].update(
        {
            "TOTAL_ASSETS": 0.0,
            "TOTAL_LIABILITIES": 0.0,
            "TOTAL_EQUITY": 0.0,
            "TOTAL_PARENT_EQUITY": 0.0,
            "DEBT_ASSET_RATIO": None,
        }
    )

    with pytest.raises(ValueError, match="uninformative zero-assets row"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


@pytest.mark.parametrize(
    ("dataset", "key"),
    [
        ("balance", "MONETARYFUNDS"),
        ("balance", "SHORT_LOAN"),
        ("income_history", "OPERATE_PROFIT"),
        ("income_interim", "PARENT_NETPROFIT"),
        ("cashflow_interim", "NETCASH_OPERATE"),
        ("indicators", "NET_INTEREST_MARGIN"),
        ("indicators", "TOTALDEPOSITS"),
    ],
)
def test_snapshot_validates_every_financial_field_consumed_by_valuation_or_scoring(dataset, key):
    financials = _financials()
    financials["000001"][dataset][-1][key] = 1e250

    with pytest.raises(ValueError, match="source-integrity magnitude"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_preserves_finite_extreme_margin_from_near_zero_revenue():
    financials = _financials()
    financials["000001"]["indicators"][-1]["XSJLL"] = -5_135_637.80951632

    validation = validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)

    assert validation["eligible_codes"] == ["000001", "000002"]


@pytest.mark.parametrize("key", ["MONETARYFUNDS", "SHORT_LOAN", "LONG_LOAN"])
def test_snapshot_rejects_negative_cash_or_debt_that_downstream_would_silently_ignore(key):
    financials = _financials()
    financials["000001"]["balance"][-1][key] = -100_000_000.0

    with pytest.raises(ValueError, match="negative (cash|interest_bearing_debt)"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_explicitly_excludes_beijing_market_from_analysis():
    quotes = _quotes()
    quotes.loc[1, ["code", "name", "market"]] = ["800001", "北交样本", "BJ"]
    financials = _financials()
    financials["800001"] = financials.pop("000002")

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=1,
        min_financial_coverage=0.5,
    )

    assert validation["analysis_markets"] == ["SH", "SZ"]
    assert validation["analysis_market_codes"] == ["000001"]
    assert validation["analysis_ineligible_codes"] == []
    assert validation["unsupported_market_codes"] == ["800001"]
    assert "800001" not in validation["eligible_codes"]
    assert validation["analysis_exclusions"]["800001"] == "unsupported_market"


def test_beijing_financial_absence_cannot_fail_shenzhen_financial_coverage():
    quotes = _quotes()
    quotes.loc[1, ["code", "name", "market"]] = ["800001", "北交样本", "BJ"]
    financials = {"000001": _financials(1)["000001"]}

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=1,
        min_financial_coverage=1.0,
    )

    assert validation["analysis_market_quotes"] == 1
    assert validation["matched_financials"] == 1
    assert validation["financial_coverage"] == 1.0
    assert validation["dataset_coverage"] == {
        "revenue_history": 1.0,
        "income_history": 1.0,
        "cashflow": 1.0,
        "balance": 1.0,
        "indicators": 1.0,
        "income_interim": 1.0,
        "cashflow_interim": 1.0,
    }
    assert validation["unsupported_market_codes"] == ["800001"]


def test_beijing_quotes_do_not_enter_analysis_factor_coverage_or_distributions():
    analysis_quotes = _quotes()
    analysis_financials = _financials()
    baseline = validate_market_snapshot(
        analysis_quotes,
        analysis_financials,
        min_quotes=2,
        min_financial_coverage=1.0,
    )

    beijing_missing = analysis_quotes.iloc[0].copy()
    beijing_missing.update(
        {
            "code": "800001",
            "name": "北交缺失样本",
            "market": "BJ",
            "price": 500.0,
            "trade_price": 500.0,
            "reference_price": 500.0,
            "market_cap": None,
            "pe": None,
            "pb": None,
        }
    )
    beijing_outlier = analysis_quotes.iloc[0].copy()
    beijing_outlier.update(
        {
            "code": "900001",
            "name": "北交高值样本",
            "market": "BJ",
            "price": 1_000.0,
            "trade_price": 1_000.0,
            "reference_price": 1_000.0,
            "market_cap": 10_000_000_000_000.0,
            "pe": 1_000.0,
            "pb": 100.0,
        }
    )
    mixed_quotes = pd.concat(
        [analysis_quotes, pd.DataFrame([beijing_missing, beijing_outlier])],
        ignore_index=True,
    )

    mixed = validate_market_snapshot(
        mixed_quotes,
        analysis_financials,
        min_quotes=2,
        min_financial_coverage=1.0,
        min_market_cap_coverage=1.0,
        min_pe_coverage=1.0,
        min_pb_coverage=1.0,
    )

    for key in ("market_cap_coverage", "pe_coverage", "pb_coverage"):
        assert mixed[key] == baseline[key] == 1.0
    for key in ("market_cap_distribution", "price_distribution", "pe_distribution", "pb_distribution"):
        assert mixed[key] == baseline[key]

    # Source-level telemetry and rows remain intact even though BJ cannot
    # affect the SH/SZ analysis quality gates above.
    assert mixed["quotes"] == 4
    assert mixed["analysis_market_quotes"] == 2
    assert mixed["market_counts"] == {"BJ": 2, "SH": 0, "SZ": 2}
    assert mixed["quote_status_counts"] == {"trading": 2}
    assert mixed["price_source_counts"] == {"last_trade": 2}
    assert mixed["source_quote_status_counts"] == {"trading": 4}
    assert mixed["source_price_source_counts"] == {"last_trade": 4}
    assert mixed["unsupported_market_codes"] == ["800001", "900001"]


def test_malformed_beijing_telemetry_cannot_block_shanghai_shenzhen_quality_gates():
    analysis_quotes = _quotes()
    analysis_financials = _financials()
    baseline = validate_market_snapshot(
        analysis_quotes,
        analysis_financials,
        min_quotes=2,
        min_financial_coverage=1.0,
    )
    beijing = analysis_quotes.iloc[0].copy()
    beijing.update(
        {
            "code": "920002",
            "name": "*ST北交遥测",
            "market": "BJ",
            "price": None,
            "trade_price": -1.0,
            "reference_price": None,
            "price_source": "broken-source",
            "quote_status": "broken-status",
            "retrieved_at": -1.0,
            "quote_tick_time": "not-a-time",
            "source_trade_date": "not-a-date",
            "market_cap": 1e300,
            "pe": 1e300,
            "pb": 1e300,
        }
    )
    mixed_quotes = pd.concat([analysis_quotes, pd.DataFrame([beijing])], ignore_index=True)
    mixed_financials = dict(analysis_financials)
    mixed_financials["920002"] = "malformed source-only telemetry"

    mixed = validate_market_snapshot(
        mixed_quotes,
        mixed_financials,
        min_quotes=2,
        min_financial_coverage=1.0,
    )

    for key in (
        "financial_coverage",
        "market_cap_coverage",
        "pe_coverage",
        "pb_coverage",
        "market_cap_distribution",
        "price_distribution",
        "pe_distribution",
        "pb_distribution",
        "quote_status_counts",
        "price_source_counts",
        "retrieval_time_oldest",
        "retrieval_time_latest",
    ):
        assert mixed[key] == baseline[key]
    assert mixed["analysis_market_quotes"] == 2
    assert mixed["source_financials"] == 3
    assert mixed["financials"] == 2
    assert mixed["unsupported_market_codes"] == ["920002"]


def test_network_refresh_requests_financials_for_shanghai_shenzhen_only(tmp_path):
    quotes = _quotes()
    quotes.loc[0, ["code", "name", "market"]] = ["600519", "贵州茅台", "SH"]
    quotes.loc[1, ["code", "name", "market"]] = ["800001", "北交样本", "BJ"]
    financials = _financials_for_codes(["600519"])

    class CapturingFetcher:
        requested_codes = None

        def get_stock_list(self, *, include_hk):
            assert include_hk is False
            return quotes

        def get_financials(self, *, codes):
            self.requested_codes = list(codes)
            return financials

    fetcher = CapturingFetcher()
    outcome = get_market_snapshot(
        fetcher,
        SafeFileCache(tmp_path / "market.json.gz", ttl=3600),
        force_refresh=True,
        persist_network=False,
        min_quotes=1,
        min_financial_coverage=1.0,
        clock=lambda: NOW,
    )

    assert fetcher.requested_codes == ["600519"]
    assert outcome.validation["unsupported_market_codes"] == ["800001"]
    assert outcome.eligible_codes == ("600519",)


def test_network_refresh_runs_optional_gap_backfill_before_validation_and_publishes_diagnostics(tmp_path):
    quotes = _quotes().iloc[[0]].copy()
    quotes.loc[:, ["code", "name", "market"]] = ["600519", "sample", "SH"]
    financials = _financials_for_codes(["600519"])

    class GapAwareFetcher:
        requested_contract = None

        def get_stock_list(self, *, include_hk):
            assert include_hk is False
            return quotes

        def get_financials(self, *, codes):
            assert codes == ["600519"]
            return financials

        def backfill_financial_gaps(self, value, *, contract, codes):
            assert value is financials
            assert codes == ["600519"]
            self.requested_contract = dict(contract)
            return value

        def financial_publication_provenance(self):
            return {
                "primary_source": "eastmoney_datacenter_bulk",
                "sina_fallback": {"target_requests": 0, "filled_fields": 0},
            }

    fetcher = GapAwareFetcher()
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    outcome = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        persist_network=True,
        min_quotes=1,
        min_financial_coverage=1.0,
        clock=lambda: NOW,
    )

    assert fetcher.requested_contract == outcome.validation["reporting_period_contract"]
    assert outcome.validation["financial_fetch"]["sina_fallback"]["target_requests"] == 0
    cached = get_market_snapshot(
        object(),
        cache,
        force_refresh=False,
        min_quotes=1,
        min_financial_coverage=1.0,
        clock=lambda: NOW,
    )
    assert cached.source == "cache"
    assert cached.validation["financial_fetch"] == outcome.validation["financial_fetch"]


def test_refresh_financials_only_reuses_cached_quotes_but_refetches_financials(tmp_path):
    quotes = _quotes().iloc[[0]].copy()
    quotes.loc[:, ["code", "name", "market"]] = ["600519", "sample", "SH"]
    financials = _financials_for_codes(["600519"])

    class CapturingFetcher:
        stock_list_calls = 0
        financial_calls = 0

        def get_stock_list(self, *, include_hk):
            self.stock_list_calls += 1
            return quotes

        def get_financials(self, *, codes):
            self.financial_calls += 1
            return financials

        def backfill_financial_gaps(self, value, *, contract, codes):
            return value

        def financial_publication_provenance(self):
            return {
                "primary_source": "eastmoney_datacenter_bulk",
                "sina_fallback": {"target_requests": 0, "filled_fields": 0},
                "sina_history_overlay": {"target_codes": 1, "filled_fields": 5},
            }

    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    # 1) seed the cache with a normal refresh
    fetcher = CapturingFetcher()
    first = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        persist_network=True,
        min_quotes=1,
        min_financial_coverage=1.0,
        clock=lambda: NOW,
    )
    assert first.source == "network"

    # 2) refresh_financials_only reuses cached quotes, never re-fetches stock list
    fetcher2 = CapturingFetcher()
    second = get_market_snapshot(
        fetcher2,
        cache,
        force_refresh=False,
        refresh_financials_only=True,
        persist_network=False,
        min_quotes=1,
        min_financial_coverage=1.0,
        clock=lambda: NOW,
    )
    assert fetcher2.stock_list_calls == 0
    assert fetcher2.financial_calls == 1
    assert second.validation["financial_fetch"]["sina_history_overlay"]["filled_fields"] == 5
    assert second.source != "cache"  # re-scored, not the cached outcome


def test_snapshot_rejects_legacy_quotes_without_trade_provenance():
    legacy = _quotes().drop(columns=["quote_status", "price_source", "retrieved_at"])

    with pytest.raises(ValueError, match="missing required columns"):
        validate_market_snapshot(legacy, _financials(), min_quotes=2, min_financial_coverage=0.5)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("price_source", "previous_close", "inconsistent"),
        ("trade_price", 0.0, "positive trade_price"),
        ("reference_price", 9.0, "price must equal"),
        ("retrieved_at", None, "retrieved_at"),
    ],
)
def test_snapshot_rejects_inconsistent_quote_provenance(column, value, message):
    quotes = _quotes()
    quotes.loc[0, column] = value

    with pytest.raises(ValueError, match=message):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_rejects_stale_or_mixed_quote_retrieval_generations():
    stale = _quotes()
    stale["retrieved_at"] = NOW - MAX_QUOTE_RETRIEVAL_AGE_SECONDS - 1
    with pytest.raises(ValueError, match="oldest quote retrieval"):
        validate_market_snapshot(
            stale,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_snapshot_rejects_a_generation_with_no_trading_quotes():
    quotes = _quotes(4)
    quotes["trade_price"] = 0.0
    quotes["quote_status"] = "suspended_or_no_trade"
    quotes["price_source"] = "previous_close"

    with pytest.raises(ValueError, match="trading quote coverage"):
        validate_market_snapshot(
            quotes,
            _financials(4),
            min_quotes=4,
            min_financial_coverage=0.5,
        )


def test_snapshot_rejects_critical_values_with_an_invalid_report_date():
    financials = _financials()
    financials["000001"]["income_interim"].append(
        {
            "REPORT_DATE": "zzzzzzzzzz",
            "TOTAL_OPERATE_INCOME": 999.0,
            "PARENT_NETPROFIT": 999.0,
        }
    )

    with pytest.raises(ValueError, match="invalid income_interim report date"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_snapshot_rejects_noncanonical_compact_report_dates():
    financials = _financials()
    financials["000001"]["income_interim"].append(
        {
            "REPORT_DATE": "20260331",
            "TOTAL_OPERATE_INCOME": 999.0,
            "PARENT_NETPROFIT": 999.0,
        }
    )

    with pytest.raises(ValueError, match="invalid income_interim report date"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_snapshot_rejects_invalid_dated_balance_cash_that_could_change_net_debt():
    financials = _financials()
    financials["000001"]["balance"].append(
        {
            "REPORT_DATE": "zzzzzzzzzz",
            "MONETARYFUNDS": 900_000_000.0,
            "SHORT_LOAN": 0.0,
        }
    )

    with pytest.raises(ValueError, match="invalid balance report date"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_snapshot_requires_fresh_parseable_source_quote_date_and_time():
    missing = _quotes()
    missing.loc[0, "source_trade_date"] = None
    with pytest.raises(ValueError, match="source_trade_date"):
        validate_market_snapshot(
            missing,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )

    malformed = _quotes()
    malformed.loc[0, "quote_tick_time"] = "25:00:00"
    with pytest.raises(ValueError, match="invalid source date/time"):
        validate_market_snapshot(
            malformed,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )

    compact = _quotes()
    compact.loc[0, "source_trade_date"] = "20260716"
    compact.loc[0, "quote_tick_time"] = "093000"
    with pytest.raises(ValueError, match="invalid source date/time"):
        validate_market_snapshot(
            compact,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )

    stale = _quotes()
    old = datetime.fromtimestamp(NOW - MAX_SOURCE_QUOTE_AGE_SECONDS - 1, tz=ZoneInfo("Asia/Shanghai"))
    stale["source_trade_date"] = old.strftime("%Y-%m-%d")
    stale["quote_tick_time"] = old.strftime("%H:%M:%S")
    with pytest.raises(ValueError, match="stale source quote"):
        validate_market_snapshot(
            stale,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )

    mixed_source_dates = _quotes()
    mixed_source_dates.loc[0, "source_trade_date"] = "2026-07-06"
    with pytest.raises(ValueError, match="multiple source trade dates"):
        validate_market_snapshot(
            mixed_source_dates,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )

    mixed = _quotes()
    mixed.loc[0, "retrieved_at"] = NOW - MAX_QUOTE_RETRIEVAL_SPAN_SECONDS - 1
    with pytest.raises(ValueError, match="retrieval span"):
        validate_market_snapshot(
            mixed,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_snapshot_schema_version_invalidates_schema7_cache_without_type3_fields(tmp_path):
    path = tmp_path / "market.json.gz"
    SafeFileCache(path, schema_version=7).save({"legacy": True})

    loaded = SafeFileCache(path, schema_version=SNAPSHOT_SCHEMA_VERSION).load()

    assert SNAPSHOT_SCHEMA_VERSION == 8
    assert loaded.hit is False
    assert loaded.reason == "schema_version_mismatch"


def test_fresh_schema4_snapshot_is_revalidated_and_atomically_migrated_without_network(tmp_path):
    path = tmp_path / "market.json.gz"
    quotes = _quotes()
    financials = _financials()
    legacy_payload = {
        "quotes": quotes,
        "financials": financials,
        "data_timestamp": NOW,
        "retrieved_at": NOW,
        "validation": {"legacy_schema": 4},
        "analysis_quality": {"source": "legacy-test"},
    }
    SafeFileCache(path, schema_version=4, ttl=3600).save(legacy_payload)
    active = SafeFileCache(path, schema_version=SNAPSHOT_SCHEMA_VERSION, ttl=3600)
    fetcher = _Fetcher(error=AssertionError("validated migration must avoid network"))

    outcome = get_market_snapshot(
        fetcher,
        active,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW,
    )

    assert outcome.source == "migrated_cache"
    assert "schema4 cache revalidated" in outcome.warning
    assert outcome.validation["reporting_period_contract"] == {
        "annual_report_date": "2025-12-31",
        "current_interim_report_date": "2026-03-31",
        "prior_interim_report_date": "2025-03-31",
        "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
    }
    assert outcome.analysis_quality == {"source": "legacy-test"}
    assert fetcher.quote_calls == fetcher.financial_calls == 0
    migrated = active.load(allow_expired=True)
    assert migrated.hit, migrated.reason
    assert migrated.value["validation"]["reporting_period_contract"] == outcome.validation["reporting_period_contract"]


def test_snapshot_rejects_legacy_all_empty_indicator_history():
    financials = _financials()
    for company in financials.values():
        company["indicators"] = []

    with pytest.raises(ValueError, match="indicators coverage"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=1.0,
            as_of_timestamp=NOW,
        )


def test_snapshot_requires_joint_financial_dataset_coverage_not_only_each_margin():
    quotes = _quotes(10)
    financials = _financials(10)
    annual_datasets = ("revenue_history", "income_history", "cashflow", "balance")
    for index, dataset in enumerate(annual_datasets):
        financials[f"{index + 1:06d}"].pop(dataset)
    financials["000005"].pop("income_interim")
    financials["000005"].pop("income_q1")
    financials["000006"].pop("cashflow_interim")
    financials["000006"].pop("cashflow_q1")

    with pytest.raises(ValueError, match="joint financial coverage"):
        validate_market_snapshot(
            quotes,
            financials,
            min_quotes=10,
            min_financial_coverage=0.90,
            as_of_timestamp=NOW,
        )


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("code", None, "code"),
        ("code", "ABC", "code"),
        ("market", None, "market"),
        ("market", "XXX", "market"),
        ("name", "", "name"),
    ],
)
def test_snapshot_validation_rejects_invalid_raw_a_share_identity(column, value, message):
    quotes = _quotes()
    quotes.loc[0, column] = value
    with pytest.raises(ValueError, match=message):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_validation_requires_recognized_usable_financial_fields():
    garbage = {code: {"garbage": [None]} for code in _quotes()["code"]}
    with pytest.raises(ValueError, match="coverage"):
        validate_market_snapshot(_quotes(), garbage, min_quotes=2, min_financial_coverage=0.5)


def test_optional_acquisition_fields_report_source_column_presence():
    validation = validate_market_snapshot(
        _quotes(),
        _financials(),
        min_quotes=2,
        min_financial_coverage=0.5,
        as_of_timestamp=NOW,
    )

    assert validation["supplemental_field_coverage"] == {
        "GOODWILL": 1.0,
        "OBTAIN_SUBSIDIARY_OTHER": 1.0,
    }


def test_snapshot_requires_each_core_financial_dataset_not_just_one_record_per_code():
    financials = _financials()
    for company in financials.values():
        company["cashflow"] = []

    with pytest.raises(ValueError, match="cashflow coverage"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_rejects_shallow_or_field_incomplete_histories():
    financials = _financials()
    for company in financials.values():
        company["revenue_history"] = company["revenue_history"][-1:]
        for record in company["cashflow"]:
            record.pop("CONSTRUCT_LONG_ASSET")

    with pytest.raises(ValueError, match="revenue_history coverage"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_rejects_structurally_complete_but_ancient_financials():
    financials = _financials()
    for company in financials.values():
        for dataset_name, dataset in company.items():
            if isinstance(dataset, list):
                month_day = "03-31" if dataset_name.endswith("_q1") or dataset_name.endswith("_interim") else "12-31"
                for index, record in enumerate(dataset):
                    record["REPORT_DATE"] = f"{1990 + index}-{month_day}"

    with pytest.raises(ValueError, match="current .* coverage"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_snapshot_excludes_each_company_missing_current_core_financials():
    quotes = _quotes(10)
    financials = _financials(10)
    stale = financials["000010"]
    for dataset_name in ("revenue_history", "income_history", "cashflow", "balance"):
        for index, record in enumerate(stale[dataset_name]):
            record["REPORT_DATE"] = f"{2010 + index}-12-31"

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=10,
        # One of the nine non-financial names lacks the exact TTM annual
        # period, so the strict non-financial TTM denominator is 8/9.
        min_financial_coverage=0.87,
        as_of_timestamp=NOW,
    )

    assert validation["current_dataset_coverage"]["revenue_history"] == pytest.approx(0.9)
    assert "000010" not in validation["eligible_codes"]
    assert validation["analysis_exclusions"]["000010"] == "stale_or_incomplete_current_financials"


def test_snapshot_excludes_each_company_without_positive_market_cap():
    quotes = _quotes(10)
    quotes.loc[9, "market_cap"] = None

    validation = validate_market_snapshot(
        quotes,
        _financials(10),
        min_quotes=10,
        min_financial_coverage=0.9,
        min_market_cap_coverage=0.9,
        as_of_timestamp=NOW,
    )

    assert "000010" not in validation["eligible_codes"]
    assert validation["analysis_exclusions"]["000010"] == "invalid_market_cap"


def test_snapshot_rejects_noncanonical_or_colliding_financial_identities():
    financials = _financials()
    financials["000001 "] = deepcopy(financials["000001"])

    with pytest.raises(ValueError, match="canonical six-digit"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )

    outcome = MarketSnapshotOutcome(
        _quotes(),
        financials,
        NOW,
        "test",
        validation={"eligible_codes": ["000001"]},
    )
    with pytest.raises(ValueError, match="non-canonical"):
        _ = outcome.analysis_financials


def test_snapshot_annual_depth_requires_distinct_consecutive_year_ends():
    financials = _financials()
    quarter_dates = ("2025-03-30", "2025-06-30", "2025-09-30", "2025-12-30")
    for company in financials.values():
        for dataset_name in ("revenue_history", "income_history", "cashflow", "balance"):
            for index, record in enumerate(company[dataset_name]):
                record["REPORT_DATE"] = quarter_dates[index]

    with pytest.raises(ValueError, match="revenue_history coverage"):
        validate_market_snapshot(_quotes(), financials, min_quotes=2, min_financial_coverage=0.5)


def test_snapshot_rejects_future_dates_even_on_an_incomplete_minority_record():
    financials = _financials(10)
    financials["000010"]["revenue_history"] = [{"REPORT_DATE": "2099-12-31", "TOTAL_OPERATE_INCOME": 1.0}]

    with pytest.raises(ValueError, match="future revenue_history"):
        validate_market_snapshot(
            _quotes(10),
            financials,
            min_quotes=10,
            min_financial_coverage=0.9,
            as_of_timestamp=NOW,
        )


def test_default_financial_coverage_does_not_silently_drop_a_large_market_slice():
    with pytest.raises(ValueError, match="coverage"):
        validate_market_snapshot(_quotes(10), _financials(8), min_quotes=10)

    quotes = _quotes()
    quotes.loc[0, "price"] = math.inf
    with pytest.raises(ValueError, match="price"):
        validate_market_snapshot(quotes, _financials(), min_quotes=2, min_financial_coverage=0.5)


def test_quote_factor_coverage_rejects_missing_cap_pe_or_pb_but_allows_negative_pe():
    quotes = _quotes(10)
    quotes.loc[:1, "market_cap"] = None
    with pytest.raises(ValueError, match="market_cap.*coverage"):
        validate_market_snapshot(quotes, _financials(10), min_quotes=10)

    quotes = _quotes(10)
    quotes.loc[:1, "pe"] = None
    with pytest.raises(ValueError, match="PE.*coverage"):
        validate_market_snapshot(quotes, _financials(10), min_quotes=10)

    quotes = _quotes(10)
    quotes.loc[:1, "pb"] = None
    with pytest.raises(ValueError, match="PB.*coverage"):
        validate_market_snapshot(quotes, _financials(10), min_quotes=10)

    quotes = _quotes(10)
    quotes["pe"] = -5.0
    validation = validate_market_snapshot(quotes, _financials(10), min_quotes=10)
    assert validation["pe_coverage"] == 1.0


def test_first_generation_ignores_a_legacy_beijing_market_minimum():
    quotes = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "沪样本",
                "market": "SH",
                "price": 10,
                "trade_price": 10,
                "reference_price": 10,
                "price_source": "last_trade",
                "quote_status": "trading",
                "retrieved_at": NOW,
                "quote_tick_time": "11:59:00",
                "source_trade_date": "2026-07-15",
                "pe": 1,
                "pb": 1,
                "market_cap": 1e9,
            },
            {
                "code": "000001",
                "name": "深样本",
                "market": "SZ",
                "price": 10,
                "trade_price": 10,
                "reference_price": 10,
                "price_source": "last_trade",
                "quote_status": "trading",
                "retrieved_at": NOW,
                "quote_tick_time": "11:59:00",
                "source_trade_date": "2026-07-15",
                "pe": 1,
                "pb": 1,
                "market_cap": 1e9,
            },
        ]
    )
    validation = validate_market_snapshot(
        quotes,
        _financials_for_codes(quotes["code"].tolist()),
        min_quotes=2,
        min_financial_coverage=0.5,
        min_market_counts={"SH": 1, "SZ": 1, "BJ": 100},
    )

    assert validation["analysis_market_quotes"] == 2
    assert validation["market_counts"] == {"BJ": 0, "SH": 1, "SZ": 1}


def test_analysis_universe_excludes_suspended_st_and_delisting_without_dropping_snapshot_rows():
    quotes = _quotes(4)
    quotes["quote_status"] = "trading"
    quotes["price_source"] = "last_trade"
    quotes.loc[0, ["code", "name"]] = ["000752", "*ST西发"]
    quotes.loc[1, "name"] = "样本退市"
    quotes.loc[2, ["quote_status", "price_source"]] = [
        "suspended_or_no_trade",
        "previous_close",
    ]
    quotes.loc[3, "name"] = "云计算软件"
    quotes.loc[2, "trade_price"] = 0.0
    financials = _financials_for_codes(quotes["code"].tolist())

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=4,
        min_financial_coverage=0.5,
        min_trading_quote_coverage=0.70,
        as_of_timestamp=NOW,
    )
    outcome = MarketSnapshotOutcome(quotes, financials, NOW, "test", validation=validation)

    assert len(outcome.quotes) == 4
    assert validation["excluded_risk_codes"] == {
        "000002": "delisting",
        "000752": "special_treatment",
    }
    assert validation["reference_priced_codes"] == ["000003"]
    assert validation["eligible_codes"] == ["000004"]
    assert validation["analysis_exclusions"] == {
        "000002": "delisting",
        "000003": "suspended_or_no_trade",
        "000752": "special_treatment",
    }
    assert outcome.analysis_quotes["code"].tolist() == ["000004"]


@pytest.mark.parametrize(
    ("as_of", "annual_report", "current_report", "previous_report"),
    [
        ((_timestamp(2026, 4, 30)), "2024-12-31", "2025-09-30", "2024-09-30"),
        ((_timestamp(2026, 5, 1)), "2025-12-31", "2026-03-31", "2025-03-31"),
        ((_timestamp(2026, 8, 31)), "2025-12-31", "2026-03-31", "2025-03-31"),
        ((_timestamp(2026, 9, 1)), "2025-12-31", "2026-06-30", "2025-06-30"),
        ((_timestamp(2026, 10, 31)), "2025-12-31", "2026-06-30", "2025-06-30"),
        ((_timestamp(2026, 11, 1)), "2025-12-31", "2026-09-30", "2025-09-30"),
    ],
)
def test_snapshot_interim_window_uses_shanghai_filing_boundaries(
    as_of,
    annual_report,
    current_report,
    previous_report,
):
    financials = _set_interim_period(_financials(), current_report, previous_report)
    quotes = _move_quote_generation(_quotes(), as_of)

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=2,
        min_financial_coverage=0.5,
        as_of_timestamp=as_of,
    )

    assert validation["expected_interim_report_date"] == current_report
    assert validation["previous_interim_report_date"] == previous_report
    assert validation["reporting_period_contract"] == {
        "annual_report_date": annual_report,
        "current_interim_report_date": current_report,
        "prior_interim_report_date": previous_report,
        "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
    }
    assert validation["comparative_interim_coverage"]["income_interim"] == 1.0
    assert validation["strict_ttm_source_coverage"]["revenue"]["coverage"] == 1.0
    assert validation["strict_ttm_source_coverage"]["fcff"]["coverage"] == 1.0


def test_single_legacy_current_q1_cannot_pass_the_strict_ttm_market_gate_without_comparative():
    financials = _financials()
    for company in financials.values():
        company.pop("income_interim")
        company.pop("cashflow_interim")
        company["income_q1"] = company["income_q1"][-1:]
        company["cashflow_q1"] = company["cashflow_q1"][-1:]

    with pytest.raises(ValueError, match="strict TTM revenue source coverage"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=NOW,
        )


def test_q1_cannot_masquerade_as_current_h1_after_september_boundary():
    quotes = _move_quote_generation(_quotes(), _timestamp(2026, 9, 1))
    with pytest.raises(ValueError, match="current income_interim coverage"):
        validate_market_snapshot(
            quotes,
            _financials(),
            min_quotes=2,
            min_financial_coverage=0.5,
            as_of_timestamp=_timestamp(2026, 9, 1),
        )


def test_strict_ttm_coverage_excludes_financial_and_beijing_companies_from_denominator():
    quotes = _quotes(3)
    quotes.loc[0, "name"] = "平安银行"
    quotes.loc[2, ["code", "name", "market"]] = ["800001", "北交样本", "BJ"]
    financials = _financials_for_codes(["000001", "000002", "800001"])
    for code in ("000001", "800001"):
        for row in financials[code]["cashflow_interim"]:
            row.pop("CONSTRUCT_LONG_ASSET")

    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=2,
        min_financial_coverage=1.0,
        as_of_timestamp=NOW,
    )

    coverage = validation["strict_ttm_source_coverage"]
    assert coverage["population"] == "SH_SZ_non_financial"
    assert coverage["denominator"] == 1
    assert coverage["excluded_financial_codes"] == ["000001"]
    assert coverage["fcff"]["coverage"] == 1.0
    assert coverage["fcff"]["status_counts"] == {"complete": 1}
    assert validation["unsupported_market_codes"] == ["800001"]


def test_missing_interim_capex_fails_closed_only_for_that_company_without_removing_scoring_eligibility():
    financials = _financials(3)
    financials["000002"]["cashflow_interim"][-1].pop("CONSTRUCT_LONG_ASSET")

    validation = validate_market_snapshot(
        _quotes(3),
        financials,
        min_quotes=3,
        min_financial_coverage=0.5,
        as_of_timestamp=NOW,
    )

    fcff = validation["strict_ttm_source_coverage"]["fcff"]
    assert fcff["coverage"] == 0.5
    assert fcff["status_counts"] == {"complete": 1, "missing_component": 1}
    assert fcff["missing_codes_by_status"] == {"missing_component": ["000002"]}
    assert "000002" in validation["financially_eligible_codes"]
    assert "000002" in validation["eligible_codes"]


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [
        (float("nan"), "nonfinite_component"),
        (1_000_000_000_000_000.0, "implausible_unit"),
        (10.0, "seasonal_reconstruction_clipped"),
    ],
)
def test_abnormal_interim_capex_has_a_deterministic_per_company_ttm_status(value, expected_status):
    financials = _financials(3)
    financials["000002"]["cashflow_interim"][0]["CONSTRUCT_LONG_ASSET"] = value
    if math.isfinite(value):
        _, provenance = resolve_capex_evidence(
            value,
            None,
            report_date="2025-03-31",
        )
        financials["000002"]["cashflow_interim"][0]["CAPEX_PROVENANCE"] = provenance

    validation = validate_market_snapshot(
        _quotes(3),
        financials,
        min_quotes=3,
        min_financial_coverage=0.5,
        as_of_timestamp=NOW,
    )

    fcff = validation["strict_ttm_source_coverage"]["fcff"]
    assert fcff["status_counts"] == {"complete": 1, expected_status: 1}
    if expected_status == "seasonal_reconstruction_clipped":
        assert fcff["complete"] == 1
        assert fcff["adjusted"] == 1
        assert fcff["usable"] == 2
        assert fcff["missing"] == 0
        assert fcff["coverage"] == 1.0
        assert fcff["adjusted_codes_by_status"] == {expected_status: ["000002"]}
        assert fcff["missing_codes_by_status"] == {}
    else:
        assert fcff["adjusted"] == 0
        assert fcff["usable"] == 1
        assert fcff["adjusted_codes_by_status"] == {}
        assert fcff["missing_codes_by_status"] == {expected_status: ["000002"]}


def test_strict_ttm_source_market_gate_rejects_broad_missing_interim_capex():
    financials = _financials()
    financials["000002"]["cashflow_interim"][-1].pop("CONSTRUCT_LONG_ASSET")

    with pytest.raises(ValueError, match="strict TTM fcff source coverage"):
        validate_market_snapshot(
            _quotes(),
            financials,
            min_quotes=2,
            min_financial_coverage=1.0,
            as_of_timestamp=NOW,
        )


def test_valid_cache_hit_avoids_network(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW - 100,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    fetcher = _Fetcher(error=AssertionError("network should not run"))

    outcome = get_market_snapshot(
        fetcher,
        cache,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW,
    )

    assert outcome.source == "cache"
    assert outcome.data_timestamp == NOW - 100
    assert outcome.quotes["code"].tolist() == ["000001", "000002"]
    assert fetcher.quote_calls == 0
    assert outcome.baseline_payload_sha256 == cache.load(allow_expired=True).metadata["payload_sha256"]


def test_expired_routine_cache_can_be_replayed_within_hard_stale_limit(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=0)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW - 100,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    fetcher = _Fetcher(error=AssertionError("historical replay must not run the network"))

    outcome = get_market_snapshot(
        fetcher,
        cache,
        allow_expired_cache=True,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW,
    )

    assert outcome.source == "cache"
    assert outcome.data_timestamp == NOW - 100
    assert fetcher.quote_calls == 0


def test_expired_routine_cache_replay_still_enforces_hard_stale_limit(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=0)
    old_timestamp = NOW - MAX_STALE_AGE_SECONDS - 1
    save_market_snapshot(
        cache,
        _move_quote_generation(_quotes(), old_timestamp),
        _financials(),
        data_timestamp=old_timestamp,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=old_timestamp,
    )

    with pytest.raises(SnapshotUnavailableError, match="upstream down"):
        get_market_snapshot(
            _Fetcher(error=RuntimeError("upstream down")),
            cache,
            allow_expired_cache=True,
            min_quotes=2,
            min_financial_coverage=0.5,
            clock=lambda: NOW,
        )


def test_schema3_cache_requires_top_level_retrieval_identity(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    payload = deepcopy(cache.load(allow_expired=True).value)
    payload.pop("retrieved_at")
    cache.save(payload)

    with pytest.raises(SnapshotUnavailableError, match="top-level retrieved_at"):
        get_market_snapshot(
            _Fetcher(error=RuntimeError("upstream down")),
            cache,
            min_quotes=2,
            min_financial_coverage=0.5,
            clock=lambda: NOW,
        )


def test_cached_quote_freshness_is_measured_against_its_stored_generation(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    quotes = _quotes()
    quotes["retrieved_at"] = NOW - 60
    save_market_snapshot(
        cache,
        quotes,
        _financials(),
        data_timestamp=NOW,
        retrieved_at=NOW - 60,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )

    # This is much later than the quote-generation freshness bound, but still
    # inside the snapshot's explicit last-known-good age.  Revalidation must
    # compare quote retrieval to NOW (the stored generation), not wall time.
    later = NOW + MAX_QUOTE_RETRIEVAL_AGE_SECONDS + 600
    outcome = get_market_snapshot(
        _Fetcher(error=AssertionError("network should not run")),
        cache,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: later,
    )

    assert outcome.source == "cache"
    assert outcome.validation["retrieval_age_seconds"] == 60


def test_force_refresh_with_incomplete_data_preserves_last_good_snapshot(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW - 100,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    fetcher = _Fetcher(quotes=_quotes(1), financials={})

    outcome = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW,
    )

    assert outcome.source == "stale_cache"
    assert "refresh failed" in outcome.warning
    assert outcome.data_timestamp == NOW - 100
    reloaded = cache.load(allow_expired=True)
    assert reloaded.hit
    assert len(reloaded.value["quotes"]) == 2


def test_successful_network_snapshot_is_validated_then_saved(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    fetcher = _Fetcher(quotes=_quotes(), financials=_financials())

    outcome = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW,
    )

    assert outcome.source == "network"
    assert outcome.data_timestamp == NOW
    assert fetcher.quote_calls == fetcher.financial_calls == 1
    assert cache.load().hit


def test_no_cache_and_failed_refresh_is_explicit(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    fetcher = _Fetcher(error=RuntimeError("upstream down"))

    with pytest.raises(SnapshotUnavailableError, match="upstream down"):
        get_market_snapshot(
            fetcher,
            cache,
            min_quotes=2,
            min_financial_coverage=0.5,
            clock=lambda: NOW,
        )


def test_network_candidate_is_not_promoted_until_analysis_succeeds(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    updated_quotes = _quotes()
    updated_quotes["price"] += 1.0
    updated_quotes["reference_price"] += 1.0
    updated_quotes["trade_price"] += 1.0
    fetcher = _Fetcher(quotes=updated_quotes, financials=_financials())

    candidate = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        persist_network=False,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert candidate.source == "network"
    assert cache.load().value["data_timestamp"] == NOW
    save_market_snapshot(
        cache,
        candidate.quotes,
        candidate.financials,
        data_timestamp=candidate.data_timestamp,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW + 100,
        expected_previous_timestamp=candidate.baseline_timestamp,
        expected_previous_payload_sha256=candidate.baseline_payload_sha256,
    )
    assert cache.load().value["data_timestamp"] == NOW + 100


def test_fallback_rejects_snapshot_older_than_hard_stale_limit(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    old_timestamp = NOW - MAX_STALE_AGE_SECONDS - 1
    old_quotes = _move_quote_generation(_quotes(), old_timestamp)
    save_market_snapshot(
        cache,
        old_quotes,
        _financials(),
        data_timestamp=old_timestamp,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=old_timestamp,
    )

    with pytest.raises(SnapshotUnavailableError, match="upstream down"):
        get_market_snapshot(
            _Fetcher(error=RuntimeError("upstream down")),
            cache,
            force_refresh=True,
            min_quotes=2,
            min_financial_coverage=0.5,
            clock=lambda: NOW,
        )


def test_refresh_rejects_large_relative_drop_against_last_good_generation(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(10),
        _financials(10),
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    fetcher = _Fetcher(quotes=_quotes(8), financials=_financials(8))

    outcome = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "relative" in outcome.warning
    assert len(outcome.quotes) == 10


def test_relative_generation_ignores_disappearance_of_beijing_telemetry(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    analysis_quotes = _quotes(10)
    beijing_rows = []
    for index in range(20):
        row = analysis_quotes.iloc[0].copy()
        row.update(
            {
                "code": f"92{index:04d}",
                "name": f"北交遥测{index}",
                "market": "BJ",
            }
        )
        beijing_rows.append(row)
    baseline_quotes = pd.concat(
        [analysis_quotes, pd.DataFrame(beijing_rows)],
        ignore_index=True,
    )
    save_market_snapshot(
        cache,
        baseline_quotes,
        _financials(10),
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )

    outcome = get_market_snapshot(
        _Fetcher(
            quotes=_move_quote_generation(analysis_quotes, NOW + 100),
            financials=_financials(10),
        ),
        cache,
        force_refresh=True,
        persist_network=False,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "network"
    assert outcome.validation["quotes"] == 10
    assert outcome.validation["analysis_market_quotes"] == 10


def test_relative_generation_rejects_strict_ttm_fcff_coverage_regression(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(10),
        _financials(10),
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    degraded_financials = _financials(10)
    for code in ("000002", "000003"):
        degraded_financials[code]["cashflow_interim"][-1].pop("CONSTRUCT_LONG_ASSET")

    outcome = get_market_snapshot(
        _Fetcher(
            quotes=_move_quote_generation(_quotes(10), NOW + 100),
            financials=degraded_financials,
        ),
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "relative strict TTM fcff" in outcome.warning
    assert outcome.validation["strict_ttm_source_coverage"]["fcff"]["complete"] == 8


def test_refresh_rejects_a_large_relative_drop_in_trading_quotes(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(10),
        _financials(10),
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    degraded_quotes = _quotes(10)
    degraded_quotes.loc[:1, "trade_price"] = 0.0
    degraded_quotes.loc[:1, "quote_status"] = "suspended_or_no_trade"
    degraded_quotes.loc[:1, "price_source"] = "previous_close"
    fetcher = _Fetcher(quotes=degraded_quotes, financials=_financials(10))

    outcome = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "trading quote coverage" in outcome.warning
    assert len(outcome.quotes) == 10


def test_refresh_rejects_large_relative_drop_in_one_financial_dataset(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(10),
        _financials(10),
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    degraded = _financials(10)
    for code in ("000009", "000010"):
        degraded[code]["balance"] = []
    fetcher = _Fetcher(quotes=_quotes(10), financials=degraded)

    outcome = get_market_snapshot(
        fetcher,
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "balance" in outcome.warning


def test_snapshot_timestamp_rejects_extreme_future_values(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    with pytest.raises(ValueError, match="timestamp"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=1e308,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW,
        )


@pytest.mark.parametrize("legacy_kind", ["older_schema", "corrupt_bytes"])
def test_valid_network_generation_self_heals_unreadable_legacy_cache(tmp_path, legacy_kind):
    path = tmp_path / "market.json.gz"
    if legacy_kind == "older_schema":
        SafeFileCache(path, schema_version=2).save({"legacy": True})
    else:
        path.write_bytes(b"not-a-valid-safe-cache")
    cache = SafeFileCache(path, schema_version=SNAPSHOT_SCHEMA_VERSION, ttl=3600)

    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )

    loaded = cache.load(allow_expired=True)
    assert loaded.hit, loaded.reason
    assert loaded.value["data_timestamp"] == NOW


def test_legacy_migration_cas_rejects_an_interposed_old_schema_writer(tmp_path):
    path = tmp_path / "market.json.gz"
    SafeFileCache(path, schema_version=2).save({"legacy": "first"})

    class RacingMigrationCache(SafeFileCache):
        def compare_and_swap(self, value, **kwargs):
            SafeFileCache(self.path, schema_version=2).save({"legacy": "interposed"})
            return super().compare_and_swap(value, **kwargs)

    cache = RacingMigrationCache(path, schema_version=SNAPSHOT_SCHEMA_VERSION, ttl=3600)
    with pytest.raises(SnapshotGenerationConflict, match="between validation and promotion"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW,
        )

    active = SafeFileCache(path, schema_version=2).load(allow_expired=True)
    assert active.hit
    assert active.value == {"legacy": "interposed"}


def test_older_binary_refuses_to_replace_a_newer_snapshot_schema(tmp_path):
    path = tmp_path / "market.json.gz"
    SafeFileCache(path, schema_version=SNAPSHOT_SCHEMA_VERSION + 1).save({"future": True})
    cache = SafeFileCache(path, schema_version=SNAPSHOT_SCHEMA_VERSION, ttl=3600)

    with pytest.raises(SafeCacheError, match="schema_version_mismatch"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW,
        )


def test_snapshot_promotion_rejects_older_or_stale_cas_generation(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    active_hash = cache.load(allow_expired=True).metadata["payload_sha256"]

    with pytest.raises(SnapshotGenerationConflict, match="older"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW - 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW,
        )
    with pytest.raises(SnapshotGenerationConflict, match="active generation changed"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW + 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW + 1,
            expected_previous_timestamp=NOW - 50,
            expected_previous_payload_sha256=active_hash,
        )
    assert cache.load(allow_expired=True).value["data_timestamp"] == NOW


def test_snapshot_cas_rejects_same_timestamp_with_different_payload(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    changed = _quotes()
    changed[["price", "trade_price", "reference_price"]] = 20.0

    with pytest.raises(SnapshotGenerationConflict, match="same data_timestamp"):
        save_market_snapshot(
            cache,
            changed,
            _financials(),
            data_timestamp=NOW,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW,
        )


def test_snapshot_cas_detects_payload_aba_even_when_timestamp_is_unchanged(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    baseline = cache.load(allow_expired=True)
    baseline_hash = baseline.metadata["payload_sha256"]

    # Simulate an external writer that bypasses snapshot promotion but leaves
    # the same timestamp.  Timestamp-only CAS cannot distinguish this ABA.
    changed_active = deepcopy(baseline.value)
    changed_active["analysis_quality"] = {"writer": "other"}
    cache.save(changed_active)

    with pytest.raises(SnapshotGenerationConflict, match="payload changed"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW + 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW + 1,
            expected_previous_timestamp=NOW,
            expected_previous_payload_sha256=baseline_hash,
        )


def test_snapshot_cas_cannot_overwrite_writer_using_the_raw_cache_lock(tmp_path):
    class RacingCache(SafeFileCache):
        def compare_and_swap(self, value, **kwargs):
            active = self.load(allow_expired=True)
            if not active.hit:
                return super().compare_and_swap(value, **kwargs)
            newer = deepcopy(active.value)
            newer["data_timestamp"] = NOW + 200
            self.save(newer)
            return super().compare_and_swap(value, **kwargs)

    cache = RacingCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    active = cache.load(allow_expired=True)

    with pytest.raises(SnapshotGenerationConflict, match="between validation and promotion"):
        save_market_snapshot(
            cache,
            _move_quote_generation(_quotes(), NOW + 100),
            _financials(),
            data_timestamp=NOW + 100,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW + 100,
            expected_previous_timestamp=NOW,
            expected_previous_payload_sha256=active.metadata["payload_sha256"],
        )

    assert cache.load(allow_expired=True).value["data_timestamp"] == NOW + 200


def test_snapshot_cas_requires_both_tokens_for_an_existing_generation(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )

    with pytest.raises(ValueError, match="both expected_previous_timestamp"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW + 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW + 1,
            expected_previous_timestamp=NOW,
        )

    with pytest.raises(ValueError, match="both expected_previous_timestamp"):
        save_market_snapshot(
            cache,
            _quotes(),
            _financials(),
            data_timestamp=NOW + 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW + 1,
        )


def test_relative_generation_rejects_market_cap_unit_drift(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(10),
        _financials(10),
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    drifted = _quotes(10)
    drifted["market_cap"] *= 1_000

    outcome = get_market_snapshot(
        _Fetcher(quotes=drifted, financials=_financials(10)),
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "market_cap_distribution" in outcome.warning
    assert cache.load(allow_expired=True).value["data_timestamp"] == NOW


def test_relative_generation_rejects_financial_source_unit_drift(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    baseline_financials = _financials(10)
    save_market_snapshot(
        cache,
        _quotes(10),
        baseline_financials,
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    drifted = deepcopy(baseline_financials)
    for company in drifted.values():
        for records in company.values():
            if not isinstance(records, list):
                continue
            for row in records:
                if not isinstance(row, dict):
                    continue
                for key, value in tuple(row.items()):
                    if key not in {"REPORT_DATE", "CONSTRUCT_LONG_ASSET"} and isinstance(value, (int, float)):
                        row[key] = value * 3.0

    outcome = get_market_snapshot(
        _Fetcher(quotes=_move_quote_generation(_quotes(10), NOW + 100), financials=drifted),
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "financial_value_distributions" in outcome.warning
    assert cache.load(allow_expired=True).value["data_timestamp"] == NOW


def test_relative_generation_accepts_a_deliberate_annual_history_window_expansion(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    baseline_financials = _financials(10)
    save_market_snapshot(
        cache,
        _quotes(10),
        baseline_financials,
        data_timestamp=NOW,
        min_quotes=1,
        min_financial_coverage=0.5,
        now=NOW,
    )
    expanded = deepcopy(baseline_financials)
    for company in expanded.values():
        company["balance"] = [
            {
                "REPORT_DATE": f"{year}-12-31",
                "TOTAL_ASSETS": 10.0,
                "TOTAL_PARENT_EQUITY": 7.0,
            }
            for year in range(2016, 2022)
        ] + company["balance"]

    outcome = get_market_snapshot(
        _Fetcher(
            quotes=_move_quote_generation(_quotes(10), NOW + 100),
            financials=expanded,
        ),
        cache,
        force_refresh=True,
        min_quotes=1,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "network"
    assert outcome.validation["annual_history_profile"]["datasets"]["balance"]["max_periods"] == 10
    assert cache.load(allow_expired=True).value["data_timestamp"] == NOW + 100


def test_cached_current_period_is_revalidated_at_read_time_after_boundary(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    august_31 = _timestamp(2026, 8, 31)
    september_1 = _timestamp(2026, 9, 1)
    quotes = _move_quote_generation(_quotes(), august_31)
    save_market_snapshot(
        cache,
        quotes,
        _financials(),
        data_timestamp=august_31,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=august_31,
    )

    with pytest.raises(SnapshotUnavailableError, match="current income_interim coverage"):
        get_market_snapshot(
            _Fetcher(error=RuntimeError("upstream down")),
            cache,
            min_quotes=2,
            min_financial_coverage=0.5,
            clock=lambda: september_1,
        )


@pytest.mark.parametrize(
    (
        "previous_day",
        "boundary_day",
        "old_current_interim",
        "old_previous_interim",
        "new_current_interim",
        "new_previous_interim",
        "remove_latest_annual",
    ),
    [
        (
            _timestamp(2026, 4, 30),
            _timestamp(2026, 5, 1),
            "2025-09-30",
            "2024-09-30",
            "2026-03-31",
            "2025-03-31",
            True,
        ),
        (
            _timestamp(2026, 8, 31),
            _timestamp(2026, 9, 1),
            "2026-03-31",
            "2025-03-31",
            "2026-06-30",
            "2025-06-30",
            False,
        ),
        (
            _timestamp(2026, 10, 31),
            _timestamp(2026, 11, 1),
            "2026-06-30",
            "2025-06-30",
            "2026-09-30",
            "2025-09-30",
            False,
        ),
    ],
)
def test_invalid_prior_period_can_be_atomically_replaced_at_filing_boundaries(
    tmp_path,
    previous_day,
    boundary_day,
    old_current_interim,
    old_previous_interim,
    new_current_interim,
    new_previous_interim,
    remove_latest_annual,
):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    old_financials = _set_interim_period(
        _financials(),
        old_current_interim,
        old_previous_interim,
    )
    if remove_latest_annual:
        for company in old_financials.values():
            for dataset in ("cashflow", "balance", "revenue_history", "income_history"):
                company[dataset] = [
                    record for record in company[dataset] if not str(record["REPORT_DATE"]).startswith("2025-")
                ]
            company["balance"].insert(
                0,
                {
                    "REPORT_DATE": "2021-12-31",
                    "TOTAL_ASSETS": 100.0,
                    "TOTAL_PARENT_EQUITY": 70.0,
                },
            )
            company["cashflow"].insert(
                0,
                {
                    "REPORT_DATE": "2022-12-31",
                    "NETCASH_OPERATE": 10.0,
                    "CONSTRUCT_LONG_ASSET": 2.0,
                },
            )
    save_market_snapshot(
        cache,
        _move_quote_generation(_quotes(), previous_day),
        old_financials,
        data_timestamp=previous_day,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=previous_day,
    )
    old_hash = cache.load(allow_expired=True).metadata["payload_sha256"]
    new_financials = _set_interim_period(
        _financials(),
        new_current_interim,
        new_previous_interim,
    )

    outcome = get_market_snapshot(
        _Fetcher(
            quotes=_move_quote_generation(_quotes(), boundary_day),
            financials=new_financials,
        ),
        cache,
        force_refresh=True,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: boundary_day,
    )

    active = cache.load(allow_expired=True)
    assert outcome.source == "network"
    assert outcome.baseline_timestamp == previous_day
    assert outcome.baseline_payload_sha256 == old_hash
    assert active.value["data_timestamp"] == boundary_day
    assert active.metadata["payload_sha256"] != old_hash


def test_cache_failure_reason_is_preserved_in_unavailable_error(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)

    with pytest.raises(SnapshotUnavailableError) as caught:
        get_market_snapshot(
            _Fetcher(error=RuntimeError("upstream down")),
            cache,
            min_quotes=2,
            min_financial_coverage=0.5,
            clock=lambda: NOW,
        )

    message = str(caught.value)
    assert "cache=" in message
    assert "cache_load" in message


def test_quote_retrieval_regression_does_not_replace_newer_retrieval(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    baseline_quotes = _quotes()
    baseline_quotes["retrieved_at"] = NOW
    save_market_snapshot(
        cache,
        baseline_quotes,
        _financials(),
        data_timestamp=NOW,
        retrieved_at=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    older_quotes = _quotes()
    older_quotes["retrieved_at"] = NOW - 1_000

    outcome = get_market_snapshot(
        _Fetcher(quotes=older_quotes, financials=_financials()),
        cache,
        force_refresh=True,
        min_quotes=2,
        min_financial_coverage=0.5,
        clock=lambda: NOW + 100,
    )

    assert outcome.source == "stale_cache"
    assert "retrieval time regressed" in outcome.warning
    assert outcome.retrieved_at == NOW


def test_save_derives_retrieval_metadata_and_rejects_regression_under_cas(tmp_path):
    cache = SafeFileCache(tmp_path / "market.json.gz", ttl=3600)
    save_market_snapshot(
        cache,
        _quotes(),
        _financials(),
        data_timestamp=NOW,
        min_quotes=2,
        min_financial_coverage=0.5,
        now=NOW,
    )
    active = cache.load(allow_expired=True)

    with pytest.raises(ValueError, match="oldest validated quote retrieval"):
        save_market_snapshot(
            SafeFileCache(tmp_path / "other.json.gz", ttl=3600),
            _quotes(),
            _financials(),
            data_timestamp=NOW,
            retrieved_at=NOW - 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW,
        )

    regressed = _quotes()
    regressed["retrieved_at"] = NOW - 1_000
    with pytest.raises(SnapshotGenerationConflict, match="quote retrieval regressed"):
        save_market_snapshot(
            cache,
            regressed,
            _financials(),
            data_timestamp=NOW + 1,
            min_quotes=2,
            min_financial_coverage=0.5,
            now=NOW + 1,
            expected_previous_timestamp=NOW,
            expected_previous_payload_sha256=active.metadata["payload_sha256"],
        )
