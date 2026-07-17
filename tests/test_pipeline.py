from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

import data.industry as industry
import engine.pipeline as pipeline
import engine.scenarios as scenarios
from data.capex_evidence import resolve_capex_evidence
from engine.dcf import ReportingPeriodContract, dcf_valuation
from engine.pipeline import (
    AnalysisQualityError,
    MarketAnalysisOutcome,
    PipelineIssue,
    compute_dcf_batch,
    run_market_analysis,
    validate_market_analysis_quality,
)


def _quotes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "平安银行",
                "market": "SZ",
                "price": 10.0,
                "pe": 5.0,
                "pb": 0.8,
                "market_cap": 1000.0,
            },
            {
                "code": "000002",
                "name": "万科A",
                "market": "SZ",
                "price": 5.0,
                "pe": 10.0,
                "pb": 0.6,
                "market_cap": 500.0,
            },
        ]
    )


def _reporting_period_contract() -> ReportingPeriodContract:
    return ReportingPeriodContract(
        annual_report_date="2025-12-31",
        current_interim_report_date="2026-03-31",
        prior_interim_report_date="2025-03-31",
    )


def _cashflow_row(report_date: str, operating_cash_flow: float, capex: float) -> dict:
    value, provenance = resolve_capex_evidence(capex, None, report_date=report_date)
    return {
        "REPORT_DATE": report_date,
        "NETCASH_OPERATE": operating_cash_flow,
        "CONSTRUCT_LONG_ASSET": value,
        "CAPEX_PROVENANCE": provenance,
    }


def _valuation_result(code: str, price: float, *, financial: bool = False) -> dict:
    if financial:
        cost = 0.10
        bvps = 1.0
        roes = {"pessimistic": 0.10, "neutral": 0.30, "optimistic": 0.50}
        params = {}
        points = {}
        for scenario, roe in roes.items():
            pb_lower = roe / (cost + 0.005)
            pb_upper = roe / (cost - 0.005)
            points[scenario] = {"lower": bvps * pb_lower, "upper": bvps * pb_upper}
            params[scenario] = {
                "growth": 0.0,
                "wacc_base": cost,
                "terminal_g": 0.0,
                "scenario_roe": roe,
                "cost_of_equity": cost,
                "bvps": bvps,
                "pb_lower": pb_lower,
                "pb_upper": pb_upper,
                "formula": "(normalised_roe - g) / (cost_of_equity - g)",
            }
    else:
        assumptions = {
            "pessimistic": (-0.02, 0.12, -0.02, 0.40),
            "neutral": (0.03, 0.10, 0.01, 0.60),
            "optimistic": (0.08, 0.08, 0.02, 0.85),
        }
        params = {}
        points = {}
        for scenario, (growth, wacc, terminal_g, retention) in assumptions.items():
            params[scenario] = {
                "growth": growth,
                "wacc_base": wacc,
                "terminal_g": terminal_g,
                "margin_retention": retention,
            }
            points[scenario] = {
                "lower": dcf_valuation(
                    1.0, 10.0, growth, wacc + 0.005, terminal_g, 100.0, 0.0, margin_retention=retention
                ),
                "upper": dcf_valuation(
                    1.0, 10.0, growth, wacc - 0.005, terminal_g, 100.0, 0.0, margin_retention=retention
                ),
            }
    buy_boundary = (points["pessimistic"]["upper"] + points["neutral"]["lower"]) / 2.0
    sell_boundary = (points["neutral"]["upper"] + points["optimistic"]["lower"]) / 2.0
    result = {
        "code": code,
        "current_price": price,
        "industry_code": "BANK" if financial else "SOFTWARE",
        "_pb_valuation": financial,
        "dcf_points": points,
        "buy_zone_upper": buy_boundary,
        "sell_zone_lower": sell_boundary,
        "zone": "买入区" if price <= buy_boundary else "卖出区" if price >= sell_boundary else "观察区",
        "base_wacc": 0.10,
        "shares_outstanding": 100.0,
        "params": params,
    }
    if not financial:
        result.update(
            {
                "base_fcf": 1.0,
                "base_revenue": 10.0,
                "net_debt": 0.0,
                "valuation_input_basis": "strict_ttm",
            }
        )
    return result


def test_financial_company_is_not_blocked_by_industrial_revenue_or_fcf_gate():
    calls = []

    def fake_dcf(**kwargs):
        calls.append(kwargs)
        return _valuation_result(kwargs["code"], kwargs["current_price"], financial=True)

    financials = {
        "000001": {
            "balance": [
                {"REPORT_DATE": "2023-12-31", "TOTAL_PARENT_EQUITY": 100.0},
                {"REPORT_DATE": "2024-12-31", "TOTAL_PARENT_EQUITY": 110.0},
                {"REPORT_DATE": "2025-12-31", "TOTAL_PARENT_EQUITY": 120.0},
            ],
            "income_history": [],
            "cashflow": [],
            "revenue_history": [],
        }
    }

    outcome = compute_dcf_batch(_quotes().iloc[:1], financials, dcf_runner=fake_dcf, max_workers=1)

    assert list(outcome.results) == ["000001"]
    assert calls[0]["revenue_history"] == []
    assert "_pre_cagr" not in calls[0]


def _complete_industrial_company(scale: float) -> dict:
    return {
        "revenue_history": [
            {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": scale * value}
            for year, value in zip(range(2021, 2026), (100, 105, 110, 115, 120))
        ],
        "income_history": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "TOTAL_OPERATE_INCOME": scale * value,
                "PARENT_NETPROFIT": scale * value * 0.10,
                "OPERATE_PROFIT": scale * value * 0.12,
            }
            for year, value in zip(range(2021, 2026), (100, 105, 110, 115, 120))
        ],
        "cashflow": [_cashflow_row(f"{year}-12-31", scale * 20.0, scale * 5.0) for year in range(2021, 2026)],
        "balance": [
            {
                "REPORT_DATE": "2025-12-31",
                "TOTAL_ASSETS": scale * 200.0,
                "TOTAL_EQUITY": scale * 100.0,
                "TOTAL_LIABILITIES": scale * 100.0,
                "SHORT_LOAN": scale * 10.0,
                "MONETARYFUNDS": scale * 5.0,
            }
        ],
        "income_interim": [
            {
                "REPORT_DATE": "2025-03-31",
                "TOTAL_OPERATE_INCOME": scale * 25.0,
                "PARENT_NETPROFIT": scale * 2.5,
            },
            {
                "REPORT_DATE": "2026-03-31",
                "TOTAL_OPERATE_INCOME": scale * 27.5,
                "PARENT_NETPROFIT": scale * 2.75,
            },
        ],
        "cashflow_interim": [
            _cashflow_row("2025-03-31", scale * 4.0, scale * 1.0),
            _cashflow_row("2026-03-31", scale * 4.4, scale * 1.1),
        ],
    }


def _complete_financial_company(scale: float) -> dict:
    equities = {year: scale * (100.0 + (year - 2020) * 10.0) for year in range(2020, 2026)}
    return {
        "balance": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "TOTAL_ASSETS": equity * 2.0,
                "TOTAL_EQUITY": equity,
                "TOTAL_PARENT_EQUITY": equity,
                "MINORITY_EQUITY": 0.0,
            }
            for year, equity in equities.items()
        ],
        "income_history": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "PARENT_NETPROFIT": (equities[year - 1] + equities[year]) / 2.0 * (0.10 + scale * 0.01),
            }
            for year in range(2021, 2026)
        ],
        "income_interim": [
            {"REPORT_DATE": "2025-03-31", "PARENT_NETPROFIT": scale * 5.0},
            {"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": scale * 5.5},
        ],
    }


def test_full_market_valuation_source_binding_rejects_cross_company_payload_swaps(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    import data.industry as industry

    monkeypatch.setattr(industry, "classify_industry", lambda *_: "SOFTWARE")
    first_company = _complete_industrial_company(1.0)
    second_company = _complete_industrial_company(3.0)
    second = scenarios.run_template25(
        "000002",
        "第二家公司",
        10.0,
        second_company,
        second_company["revenue_history"],
        100.0,
        reporting_period_contract=_reporting_period_contract(),
    )
    assert second is not None
    swapped = deepcopy(second)
    swapped.update({"code": "000001", "name": "第一家公司", "current_price": 10.0})

    error = pipeline._valuation_source_error(
        "000001",
        swapped,
        {"code": "000001", "name": "第一家公司", "price": 10.0},
        first_company,
        100.0,
        reporting_period_contract=_reporting_period_contract(),
        strict_ttm_required=True,
    )
    assert error is not None and ("source" in error or "current company" in error)

    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "BANK")
    first_bank = _complete_financial_company(1.0)
    second_bank = _complete_financial_company(2.0)
    second_pb = scenarios.run_template25("000002", "第二家银行", 10.0, second_bank, [], 100.0)
    assert second_pb is not None
    swapped_pb = deepcopy(second_pb)
    swapped_pb.update({"code": "000001", "name": "第一家银行", "current_price": 10.0})
    error = pipeline._valuation_source_error(
        "000001",
        swapped_pb,
        {"code": "000001", "name": "第一家银行", "price": 10.0},
        first_bank,
        100.0,
    )
    assert error is not None and "current attributable equity" in error


def test_dcf_batch_captures_one_company_failure_without_hiding_it_or_aborting():
    def fake_dcf(**kwargs):
        if kwargs["code"] == "000001":
            raise RuntimeError("formula exploded")
        return _valuation_result(kwargs["code"], kwargs["current_price"])

    financials = {"000001": {}, "000002": {}}

    outcome = compute_dcf_batch(_quotes(), financials, dcf_runner=fake_dcf, max_workers=2)

    assert list(outcome.results) == ["000002"]
    assert outcome.attempted == 2
    assert outcome.skipped == 1
    assert len(outcome.issues) == 1
    assert outcome.issues[0].code == "000001"
    assert "formula exploded" in outcome.issues[0].message
    assert outcome.skip_reasons["000001"] == "valuation_exception:RuntimeError"


def test_dcf_results_are_deterministic_even_when_input_order_changes():
    def fake_dcf(**kwargs):
        return _valuation_result(kwargs["code"], kwargs["current_price"])

    reversed_quotes = _quotes().iloc[::-1].reset_index(drop=True)
    financials = {"000002": {}, "000001": {}}

    outcome = compute_dcf_batch(reversed_quotes, financials, dcf_runner=fake_dcf, max_workers=2)

    assert list(outcome.results) == ["000001", "000002"]


def test_batch_beta_path_is_opt_in_precomputed_only_and_never_fetches(monkeypatch):
    import data.market_history as market_history

    monkeypatch.setattr(
        market_history,
        "estimate_market_beta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("batch must not fetch beta")),
    )
    calls = []

    def fake_dcf(**kwargs):
        calls.append(kwargs)
        return _valuation_result(kwargs["code"], kwargs["current_price"])

    financials = {"000001": {}}
    compute_dcf_batch(_quotes().iloc[:1], financials, dcf_runner=fake_dcf, max_workers=1)
    assert "market_beta_estimate" not in calls[-1]

    estimate = {"available": False, "code": "000001", "reason": "fixture"}
    compute_dcf_batch(
        _quotes().iloc[:1],
        financials,
        dcf_runner=fake_dcf,
        max_workers=1,
        market_beta_estimates={"1": estimate, "830001": {"available": True}},
    )
    assert calls[-1]["market_beta_estimate"] is estimate
    assert set(pipeline._prepare_market_beta_estimates({"830001": object(), "600519": estimate})) == {"600519"}


def test_reporting_period_contract_is_validated_before_worker_construction(monkeypatch):
    class ExplodingExecutor:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("worker pool must not be constructed")

    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", ExplodingExecutor)
    with pytest.raises(ValueError, match="reporting_period_contract is required"):
        compute_dcf_batch(_quotes().iloc[[1]], {"000002": _complete_industrial_company(1.0)})
    with pytest.raises(ValueError, match="reporting_period_contract is required"):
        run_market_analysis(_quotes().iloc[[1]], {"000002": _complete_industrial_company(1.0)})
    with pytest.raises(ValueError, match="invalid or internally inconsistent"):
        compute_dcf_batch(
            _quotes().iloc[[1]],
            {"000002": _complete_industrial_company(1.0)},
            reporting_period_contract={
                "annual_report_date": "2025-12-31",
                "current_interim_report_date": "2026-03-31",
                "prior_interim_report_date": "2025-06-30",
            },
        )


def test_serializable_reporting_contract_is_frozen_and_forwarded_to_runner():
    captured = []

    def fake_dcf(**kwargs):
        captured.append(kwargs["reporting_period_contract"])
        return _valuation_result(kwargs["code"], kwargs["current_price"])

    contract_mapping = {
        "annual_report_date": "2025-12-31",
        "current_interim_report_date": "2026-03-31",
        "prior_interim_report_date": "2025-03-31",
    }
    compute_dcf_batch(
        _quotes().iloc[[1]],
        {"000002": {}},
        dcf_runner=fake_dcf,
        max_workers=1,
        reporting_period_contract=contract_mapping,
    )

    assert captured == [_reporting_period_contract()]
    assert isinstance(captured[0], ReportingPeriodContract)


def test_strict_ttm_production_results_are_deterministic_with_one_or_eight_workers(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "SOFTWARE")
    financials = {
        "000001": _complete_industrial_company(1.0),
        "000002": _complete_industrial_company(1.5),
    }

    serial = compute_dcf_batch(
        _quotes(),
        financials,
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )
    parallel = compute_dcf_batch(
        _quotes().iloc[::-1].reset_index(drop=True),
        dict(reversed(list(financials.items()))),
        max_workers=8,
        reporting_period_contract={
            "annual_report_date": "2025-12-31",
            "current_interim_report_date": "2026-03-31",
            "prior_interim_report_date": "2025-03-31",
        },
    )

    assert serial.results == parallel.results
    assert serial.skip_reasons == parallel.skip_reasons == {}
    assert serial.issues == parallel.issues == ()
    assert all(result["valuation_input_basis"] == "strict_ttm" for result in serial.results.values())


@pytest.mark.parametrize(
    "tamper",
    [
        "ttm_value",
        "ttm_period",
        "ttm_formula",
        "ttm_component",
        "ttm_revenue_value",
        "base_revenue",
        "base_fcf",
        "latest_fcff",
        "recent_fcff",
        "recent_period",
        "normalisation_period_basis",
        "normalisation_detail",
        "base_fcf_basis",
        "base_fcf_adjustments",
    ],
)
def test_strict_ttm_source_binding_rejects_every_material_payload_tamper(tamper):
    company = _complete_industrial_company(1.0)
    quote = _quotes().iloc[1].to_dict()
    outcome = compute_dcf_batch(
        _quotes().iloc[[1]],
        {"000002": company},
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )
    assert not outcome.issues and "000002" in outcome.results
    altered = deepcopy(outcome.results["000002"])
    if tamper == "ttm_value":
        altered["ttm_fcff_evidence"]["value"] += 1.0
    elif tamper == "ttm_period":
        altered["ttm_fcff_evidence"]["period"]["annual_report_date"] = "2024-12-31"
    elif tamper == "ttm_formula":
        altered["ttm_fcff_evidence"]["formula_version"] = "forged_formula"
    elif tamper == "ttm_component":
        altered["ttm_fcff_evidence"]["components"]["annual"]["operating_cash_flow"] += 1.0
    elif tamper == "ttm_revenue_value":
        altered["ttm_revenue_evidence"]["value"] += 1.0
    elif tamper == "base_revenue":
        altered["base_revenue"] += 1.0
    elif tamper == "base_fcf":
        altered["base_fcf"] += 1.0
    elif tamper == "latest_fcff":
        altered["latest_fcff"] += 1.0
    elif tamper == "recent_fcff":
        altered["recent_fcff"][0] += 1.0
    elif tamper == "recent_period":
        altered["recent_fcff_periods"][0]["report_date"] = "2023-12-31"
    elif tamper == "normalisation_period_basis":
        altered["fcf_normalisation_period_basis"] = "annual_only"
    elif tamper == "normalisation_detail":
        altered["fcf_normalisation_period"]["cash_flow_kind"] = "invented_fcff"
    elif tamper == "base_fcf_basis":
        altered["base_fcf_basis"] = "annual_fcff"
    elif tamper == "base_fcf_adjustments":
        altered["base_fcf_adjustments"] = []
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(tamper)

    error = pipeline._valuation_source_error(
        "000002",
        altered,
        quote,
        company,
        100.0,
        reporting_period_contract=_reporting_period_contract(),
        strict_ttm_required=True,
    )
    assert error is not None


@pytest.mark.parametrize(
    "expected_reason, mutation",
    [
        ("ttm_fcff_missing_component", "fcff_missing"),
        ("ttm_fcff_duplicate_period", "fcff_duplicate"),
        ("ttm_fcff_nonfinite_component", "fcff_nonfinite"),
        ("ttm_fcff_negative_reconstructed_capex", "fcff_negative_capex"),
        ("ttm_fcff_nonpositive", "fcff_nonpositive"),
        ("ttm_revenue_missing_component", "revenue_missing"),
        ("ttm_revenue_duplicate_period", "revenue_duplicate"),
        ("ttm_revenue_nonfinite_component", "revenue_nonfinite"),
        ("ttm_revenue_nonpositive", "revenue_nonpositive"),
        ("ttm_fcff_missing_prior_annual_component", "prior_fy_missing"),
    ],
)
def test_strict_ttm_skip_reasons_preserve_exact_reconstruction_status(expected_reason, mutation):
    company = _complete_industrial_company(1.0)
    if mutation == "fcff_missing":
        company["cashflow_interim"][1].pop("CONSTRUCT_LONG_ASSET")
    elif mutation == "fcff_duplicate":
        company["cashflow"].append(deepcopy(company["cashflow"][-1]))
    elif mutation == "fcff_nonfinite":
        company["cashflow"][-1]["NETCASH_OPERATE"] = float("inf")
    elif mutation == "fcff_negative_capex":
        company["cashflow_interim"][0] = _cashflow_row("2025-03-31", 4.0, 100.0)
    elif mutation == "fcff_nonpositive":
        company["cashflow"][-1] = _cashflow_row("2025-12-31", 1.0, 10.0)
    elif mutation == "revenue_missing":
        company["income_interim"][1].pop("TOTAL_OPERATE_INCOME")
    elif mutation == "revenue_duplicate":
        company["revenue_history"].append(deepcopy(company["revenue_history"][-1]))
    elif mutation == "revenue_nonfinite":
        company["income_interim"][1]["TOTAL_OPERATE_INCOME"] = float("inf")
    elif mutation == "revenue_nonpositive":
        company["revenue_history"][-1]["TOTAL_OPERATE_INCOME"] = 10.0
        company["income_interim"][0]["TOTAL_OPERATE_INCOME"] = 25.0
        company["income_interim"][1]["TOTAL_OPERATE_INCOME"] = 0.0
    elif mutation == "prior_fy_missing":
        company["cashflow"] = [row for row in company["cashflow"] if row["REPORT_DATE"] != "2024-12-31"]
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(mutation)

    outcome = compute_dcf_batch(
        _quotes().iloc[[1]],
        {"000002": company},
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )

    assert outcome.results == {}
    assert outcome.skip_reasons == {"000002": expected_reason}


def test_financial_pb_generation_requires_contract_but_never_uses_industrial_ttm(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "BANK")
    company = _complete_financial_company(1.0)

    outcome = compute_dcf_batch(
        _quotes().iloc[:1],
        {"000001": company},
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )

    assert list(outcome.results) == ["000001"]
    assert outcome.results["000001"]["_pb_valuation"] is True
    assert "ttm_fcff_evidence" not in outcome.results["000001"]
    assert not outcome.skip_reasons and not outcome.issues


def test_financial_turnaround_is_not_misclassified_as_missing_source(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "BANK")
    company = _complete_financial_company(1.0)
    company["income_interim"] = [
        {"REPORT_DATE": "2025-03-31", "PARENT_NETPROFIT": -5.0},
        {"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": 5.5},
    ]

    outcome = compute_dcf_batch(
        _quotes().iloc[:1],
        {"000001": company},
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )

    assert list(outcome.results) == ["000001"]
    assert outcome.results["000001"]["current_period_evidence"]["profit_yoy_basis"] == "same_period_turnaround"
    assert not outcome.skip_reasons and not outcome.skip_classifications and not outcome.issues


def test_ambiguous_cross_market_bare_code_is_rejected_before_financial_matching():
    quotes = pd.concat(
        [
            _quotes().iloc[:1],
            pd.DataFrame(
                [
                    {
                        "code": "000001",
                        "name": "HK sample",
                        "market": "HK",
                        "price": 2.0,
                        "pe": 8.0,
                        "pb": 1.0,
                        "market_cap": 200.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )

    outcome = compute_dcf_batch(quotes, {"000001": {}}, dcf_runner=lambda **_: {})

    assert not outcome.results
    assert outcome.attempted == 0
    assert any(issue.stage == "identity" and "ambiguous" in issue.message for issue in outcome.issues)


def test_market_analysis_passes_visible_dcf_results_to_scoring(monkeypatch):
    captured = {}
    generations = []
    monkeypatch.setattr(industry, "begin_industry_generation", lambda: generations.append(True))

    def fake_dcf(**kwargs):
        captured["reporting_period_contract"] = kwargs["reporting_period_contract"]
        return _valuation_result(kwargs["code"], kwargs["current_price"])

    def fake_screen(financials, quotes, *, dcf_results, progress_cb):
        captured["financials"] = financials
        captured["quotes"] = quotes
        captured["dcf_results"] = dcf_results
        captured["progress_cb"] = progress_cb
        return pd.DataFrame([{"code": "000001"}])

    result = run_market_analysis(
        _quotes().iloc[:1],
        {"000001": {}},
        dcf_runner=fake_dcf,
        screen_runner=fake_screen,
        max_workers=1,
        reporting_period_contract={
            "annual_report_date": "2025-12-31",
            "current_interim_report_date": "2026-03-31",
            "prior_interim_report_date": "2025-03-31",
        },
    )

    assert result.scores["code"].tolist() == ["000001"]
    assert captured["dcf_results"]["000001"]["zone"] == "卖出区"
    assert captured["reporting_period_contract"] == _reporting_period_contract()
    assert generations == [True]


def test_market_analysis_default_valuation_path_exposes_only_strict_ttm_nonfinancial_results():
    captured = {}

    def fake_screen(financials, quotes, *, dcf_results, progress_cb):
        captured["dcf_results"] = dcf_results
        return pd.DataFrame([{"code": code} for code in financials])

    outcome = run_market_analysis(
        _quotes().iloc[[1]],
        {"000002": _complete_industrial_company(1.0)},
        screen_runner=fake_screen,
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )

    assert outcome.dcf_results == captured["dcf_results"]
    assert outcome.dcf_results["000002"]["valuation_input_basis"] == "strict_ttm"
    assert outcome.dcf_results["000002"]["recent_fcff_periods"][-1] == {
        "kind": "ttm",
        "through_report_date": "2026-03-31",
    }


def test_market_analysis_never_passes_ambiguous_identities_to_scoring():
    quotes = pd.concat(
        [
            _quotes().iloc[:1],
            pd.DataFrame(
                [
                    {
                        "code": "1",
                        "name": "duplicate",
                        "market": "SH",
                        "price": 9.0,
                        "pe": 8.0,
                        "pb": 1.0,
                        "market_cap": 900.0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    financials = {"1": {}, "000001": {}}
    captured = {}

    def fake_screen(financials, quotes, *, dcf_results, progress_cb):
        captured["financial_codes"] = list(financials)
        captured["quote_codes"] = quotes["code"].tolist()
        return pd.DataFrame(columns=["code"])

    outcome = run_market_analysis(
        quotes,
        financials,
        dcf_runner=lambda **_: {},
        screen_runner=fake_screen,
        max_workers=1,
    )

    assert captured["financial_codes"] == []
    assert captured["quote_codes"] == []
    assert any(issue.stage == "identity" for issue in outcome.issues)


def test_eligible_universe_constrains_both_dcf_and_scoring_branches():
    dcf_codes = []
    captured = {}

    def fake_dcf(**kwargs):
        dcf_codes.append(kwargs["code"])
        return _valuation_result(kwargs["code"], kwargs["current_price"])

    def fake_screen(financials, quotes, *, dcf_results, progress_cb):
        captured["financials"] = list(financials)
        captured["quotes"] = quotes["code"].tolist()
        captured["dcf"] = list(dcf_results)
        return pd.DataFrame([{"code": code} for code in financials])

    outcome = run_market_analysis(
        _quotes(),
        {"000001": {}, "000002": {}},
        eligible_codes=["000002"],
        dcf_runner=fake_dcf,
        screen_runner=fake_screen,
        max_workers=1,
    )

    assert dcf_codes == ["000002"]
    assert captured == {
        "financials": ["000002"],
        "quotes": ["000002"],
        "dcf": ["000002"],
    }
    assert outcome.scores["code"].tolist() == ["000002"]


@pytest.mark.parametrize(
    "quotes, financials, message",
    [
        (_quotes().iloc[:1], {"000001": {}, "000002": {}}, "missing quotes"),
        (_quotes(), {"000001": {}}, "missing financials"),
    ],
)
def test_eligible_universe_must_exist_in_both_canonical_inputs(quotes, financials, message):
    with pytest.raises(ValueError, match=message):
        run_market_analysis(
            quotes,
            financials,
            eligible_codes=["000001", "000002"],
            dcf_runner=lambda **kwargs: _valuation_result(kwargs["code"], kwargs["current_price"]),
            screen_runner=lambda financials, *_args, **_kwargs: pd.DataFrame([{"code": code} for code in financials]),
            enforce_quality=True,
            max_workers=1,
        )


def test_dcf_skip_reasons_explain_input_and_model_rejections():
    quotes = _quotes()
    quotes.loc[0, "market_cap"] = None
    outcome = compute_dcf_batch(
        quotes,
        {"000001": {}, "000002": {}},
        dcf_runner=lambda **_: None,
        max_workers=1,
    )

    assert outcome.attempted == 1
    assert outcome.skipped == 1
    assert outcome.skip_reasons == {
        "000001": "invalid_price_or_market_cap",
        "000002": "missing_positive_annual_revenue",
    }


def test_analysis_quality_gate_requires_substantial_score_and_dcf_coverage():
    outcome = MarketAnalysisOutcome(
        scores=pd.DataFrame([{"code": "000001"}, {"code": "000002"}]),
        dcf_results={"000001": _valuation_result("000001", 1.0)},
        issues=(),
        dcf_attempted=2,
        dcf_skipped=1,
        dcf_skip_reasons={"000002": "unit_test_skip"},
    )

    metrics = validate_market_analysis_quality(outcome, expected_companies=2)

    assert metrics["score_coverage"] == 1.0
    assert metrics["dcf_valid_coverage"] == 0.5


def test_analysis_quality_gate_rejects_zero_dcf_or_excessive_errors():
    zero_dcf = MarketAnalysisOutcome(
        scores=pd.DataFrame([{"code": f"{index:06d}"} for index in range(100)]),
        dcf_results={},
        issues=(),
        dcf_attempted=100,
        dcf_skipped=100,
        dcf_skip_reasons={f"{index:06d}": "unit_test_skip" for index in range(100)},
    )
    noisy = MarketAnalysisOutcome(
        scores=zero_dcf.scores,
        dcf_results={f"{index:06d}": _valuation_result(f"{index:06d}", 1.0) for index in range(30)},
        issues=(PipelineIssue("000001", "dcf", "bad"), PipelineIssue("000002", "dcf", "bad")),
        dcf_attempted=100,
        dcf_skipped=70,
        dcf_skip_reasons={f"{index:06d}": "unit_test_skip" for index in range(30, 100)},
    )

    try:
        validate_market_analysis_quality(zero_dcf, expected_companies=100)
    except ValueError as exc:
        assert "valid DCF coverage" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("zero DCF generation must fail")
    try:
        validate_market_analysis_quality(noisy, expected_companies=100)
    except ValueError as exc:
        assert "issue rate" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("noisy generation must fail")


def test_analysis_quality_gate_rejects_nonfinancial_annual_input_disguised_as_current_valuation():
    forged = _valuation_result("000001", 1.0)
    forged["valuation_input_basis"] = "annual_history"
    outcome = MarketAnalysisOutcome(
        scores=pd.DataFrame([{"code": "000001"}]),
        dcf_results={"000001": forged},
        issues=(),
        dcf_attempted=1,
        dcf_skipped=0,
        dcf_skip_reasons={},
    )

    with pytest.raises(AnalysisQualityError) as caught:
        validate_market_analysis_quality(outcome, expected_companies=1)

    assert any(
        reason["code"] == "valuation_payload_invalid" and "strict_ttm" in reason["message"]
        for reason in caught.value.reasons
    )


def test_analysis_quality_gate_rejects_large_relative_regression():
    outcome = MarketAnalysisOutcome(
        scores=pd.DataFrame([{"code": f"{index:06d}"} for index in range(100)]),
        dcf_results={f"{index:06d}": _valuation_result(f"{index:06d}", 1.0) for index in range(30)},
        issues=(),
        dcf_attempted=100,
        dcf_skipped=70,
        dcf_skip_reasons={f"{index:06d}": "unit_test_skip" for index in range(30, 100)},
    )

    try:
        validate_market_analysis_quality(
            outcome,
            expected_companies=100,
            previous={"score_rows": 100, "dcf_attempted": 100, "dcf_valid": 40},
        )
    except ValueError as exc:
        assert "relative dcf_valid" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("relative DCF regression must fail")


def test_production_analysis_call_enforces_zero_dcf_gate_with_structured_reasons():
    def fake_screen(financials, quotes, *, dcf_results, progress_cb):
        return pd.DataFrame([{"code": code} for code in financials])

    with pytest.raises(AnalysisQualityError) as caught:
        run_market_analysis(
            _quotes(),
            {"000001": {}, "000002": {}},
            dcf_runner=lambda **_: None,
            screen_runner=fake_screen,
            enforce_quality=True,
            max_workers=1,
        )

    assert not caught.value.metrics["ok"]
    assert any(reason["code"] == "dcf_valid_coverage_low" for reason in caught.value.reasons)


def test_production_analysis_call_enforces_relative_generation_gate():
    def fake_screen(financials, quotes, *, dcf_results, progress_cb):
        return pd.DataFrame([{"code": code} for code in financials])

    with pytest.raises(AnalysisQualityError) as caught:
        run_market_analysis(
            _quotes(),
            {"000001": {}, "000002": {}},
            dcf_runner=lambda **kwargs: _valuation_result(kwargs["code"], kwargs["current_price"]),
            screen_runner=fake_screen,
            enforce_quality=True,
            previous_quality={"score_rows": 3, "dcf_attempted": 3, "dcf_valid": 3},
            max_workers=1,
        )

    assert any(reason["code"] == "relative_analysis_regression" for reason in caught.value.reasons)


def test_production_analysis_rejects_expected_count_that_disagrees_with_identity_set():
    with pytest.raises(ValueError, match="does not match canonical analysis universe"):
        run_market_analysis(
            _quotes(),
            {"000001": {}, "000002": {}},
            dcf_runner=lambda **kwargs: _valuation_result(kwargs["code"], kwargs["current_price"]),
            screen_runner=lambda financials, *_args, **_kwargs: pd.DataFrame([{"code": code} for code in financials]),
            enforce_quality=True,
            expected_companies=3,
            max_workers=1,
        )


def test_quality_gate_counts_unique_codes_and_rejects_duplicate_score_rows():
    outcome = MarketAnalysisOutcome(
        scores=pd.DataFrame([{"code": "000001"}, {"code": "1"}]),
        dcf_results={
            "000001": _valuation_result("000001", 1.0),
            "000002": _valuation_result("000002", 1.0),
        },
        issues=(),
        dcf_attempted=2,
        dcf_skipped=0,
    )

    with pytest.raises(AnalysisQualityError) as caught:
        validate_market_analysis_quality(outcome, expected_companies=2)

    assert caught.value.metrics["score_raw_rows"] == 2
    assert caught.value.metrics["score_rows"] == 1
    assert any(reason["code"] == "score_identity_duplicate_code" for reason in caught.value.reasons)


def test_quality_gate_rejects_phantom_or_malformed_valuation_identities():
    scores = pd.DataFrame([{"code": f"{index:06d}"} for index in range(100)])
    phantom_results = {f"9{index:05d}": _valuation_result(f"9{index:05d}", 1.0) for index in range(25)}
    outcome = MarketAnalysisOutcome(
        scores=scores,
        dcf_results=phantom_results,
        issues=(),
        dcf_attempted=100,
        dcf_skipped=75,
        dcf_skip_reasons={f"{index:06d}": "unit_test_skip" for index in range(75)},
    )

    with pytest.raises(AnalysisQualityError) as caught:
        validate_market_analysis_quality(outcome, expected_companies=100)

    assert any(reason["code"] == "valuation_identity_extra_companies" for reason in caught.value.reasons)

    malformed = MarketAnalysisOutcome(
        scores=pd.DataFrame([{"code": "000001"}, {"code": "000002"}]),
        dcf_results={"000001": {}},
        issues=(),
        dcf_attempted=2,
        dcf_skipped=1,
        dcf_skip_reasons={"000002": "unit_test_skip"},
    )
    with pytest.raises(AnalysisQualityError) as malformed_error:
        validate_market_analysis_quality(malformed, expected_companies=2)
    assert any(reason["code"] == "valuation_payload_invalid" for reason in malformed_error.value.reasons)


def test_quality_gate_rejects_pipeline_issues_for_out_of_universe_companies():
    scores = pd.DataFrame([{"code": "000001"}])
    outcome = MarketAnalysisOutcome(
        scores=scores,
        dcf_results={},
        issues=(PipelineIssue("999999", "forged", "outside universe"),),
        dcf_attempted=0,
        dcf_skipped=0,
        dcf_skip_reasons={},
    )

    with pytest.raises(AnalysisQualityError) as caught:
        validate_market_analysis_quality(
            outcome,
            expected_companies=1,
            expected_codes={"000001"},
            min_dcf_attempt_coverage=0.0,
            min_dcf_valid_coverage=0.0,
            max_issue_rate=1.0,
        )

    assert any(reason["code"] == "pipeline_issue_extra_company" for reason in caught.value.reasons)


def test_compute_batch_rejects_empty_mapping_as_invalid_valuation_evidence():
    outcome = compute_dcf_batch(
        _quotes(),
        {"000001": {}, "000002": {}},
        dcf_runner=lambda **_kwargs: {},
        max_workers=1,
    )

    assert outcome.results == {}
    assert outcome.skipped == outcome.attempted == 2
    assert set(outcome.skip_reasons.values()) == {"valuation_evidence_invalid"}
    assert all(issue.stage == "valuation_evidence" for issue in outcome.issues)


def test_production_analysis_requires_exact_canonical_score_identity_set():
    def incomplete_screen(financials, quotes, *, dcf_results, progress_cb):
        return pd.DataFrame([{"code": "000001"}])

    with pytest.raises(AnalysisQualityError) as caught:
        run_market_analysis(
            _quotes(),
            {"000001": {}, "000002": {}},
            dcf_runner=lambda **kwargs: _valuation_result(kwargs["code"], kwargs["current_price"]),
            screen_runner=incomplete_screen,
            enforce_quality=True,
            max_workers=1,
        )

    assert any(reason["code"] == "score_identity_missing_companies" for reason in caught.value.reasons)
    assert caught.value.reasons[0].get("examples") == ["000002"]


@pytest.mark.parametrize(
    "invalid_history",
    [[], {"000001": "invalid"}],
)
def test_pipeline_rejects_malformed_preloaded_type7_history(invalid_history):
    with pytest.raises(TypeError, match="quality_history_evidence"):
        run_market_analysis(
            _quotes().iloc[[0]],
            {"000001": {}},
            dcf_runner=lambda **kwargs: _valuation_result(kwargs["code"], kwargs["current_price"]),
            screen_runner=lambda *_args, **_kwargs: pd.DataFrame([{"code": "000001"}]),
            quality_history_evidence=invalid_history,
            max_workers=1,
        )


def test_dcf_skip_reason_distinguishes_unrecovered_mixed_profit_cycle():
    company = {
        "revenue_history": [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 1000.0}],
        "income_history": [
            {"REPORT_DATE": "2023-12-31", "PARENT_NETPROFIT": -10.0},
            {"REPORT_DATE": "2024-12-31", "PARENT_NETPROFIT": 5.0},
            {"REPORT_DATE": "2025-12-31", "PARENT_NETPROFIT": -2.0},
        ],
        "cashflow": [
            {"REPORT_DATE": "2023-12-31", "NETCASH_OPERATE": 0.0, "CONSTRUCT_LONG_ASSET": 100.0},
            {"REPORT_DATE": "2024-12-31", "NETCASH_OPERATE": 110.0, "CONSTRUCT_LONG_ASSET": 10.0},
            {"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 110.0, "CONSTRUCT_LONG_ASSET": 10.0},
        ],
    }

    outcome = compute_dcf_batch(
        _quotes().iloc[[1]],
        {"000002": company},
        dcf_runner=lambda **_: None,
        max_workers=1,
    )

    assert outcome.skip_reasons["000002"] == "mixed_profit_cycle_unsupported_by_fcff"


def test_dcf_skip_reason_names_nonpositive_pessimistic_equity_value():
    company = {
        "revenue_history": [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 1000.0}],
        "income_history": [{"REPORT_DATE": f"{year}-12-31", "PARENT_NETPROFIT": 10.0} for year in range(2023, 2026)],
        "cashflow": [
            {"REPORT_DATE": f"{year}-12-31", "NETCASH_OPERATE": 20.0, "CONSTRUCT_LONG_ASSET": 5.0}
            for year in range(2023, 2026)
        ],
    }

    outcome = compute_dcf_batch(
        _quotes().iloc[[1]],
        {"000002": company},
        dcf_runner=lambda **_: None,
        max_workers=1,
    )

    assert outcome.skip_reasons["000002"] == "nonpositive_pessimistic_equity_value"


@pytest.mark.parametrize("max_workers", [True, 1.5, 0, 65])
def test_pipeline_public_entrypoints_reject_non_integer_or_unbounded_worker_counts(max_workers):
    with pytest.raises(ValueError, match="between 1 and 64"):
        compute_dcf_batch(
            _quotes().iloc[:1],
            {"000001": {}},
            dcf_runner=lambda **_kwargs: None,
            max_workers=max_workers,
        )
    with pytest.raises(ValueError, match="between 1 and 64"):
        run_market_analysis(
            _quotes().iloc[:1],
            {"000001": {}},
            dcf_runner=lambda **_kwargs: None,
            screen_runner=lambda *_args, **_kwargs: pd.DataFrame(),
            max_workers=max_workers,
        )


@pytest.mark.parametrize("expected_companies", [True, 1.5, -1])
def test_market_analysis_rejects_coerced_or_negative_expected_company_counts(expected_companies):
    with pytest.raises(ValueError, match="non-negative integer"):
        run_market_analysis(
            _quotes().iloc[:1],
            {"000001": {}},
            dcf_runner=lambda **_kwargs: None,
            screen_runner=lambda *_args, **_kwargs: pd.DataFrame(),
            expected_companies=expected_companies,
            max_workers=1,
        )
