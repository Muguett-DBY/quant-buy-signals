import json
import math
import random

import pytest

import engine.scenarios as scenarios
from data.capex_evidence import resolve_capex_evidence
from engine.dcf import ReportingPeriodContract


Q1_CONTRACT = ReportingPeriodContract(
    annual_report_date="2025-12-31",
    current_interim_report_date="2026-03-31",
    prior_interim_report_date="2025-03-31",
)


def _run_nonfinancial(*args, reporting_period_contract=Q1_CONTRACT, **kwargs):
    positional = list(args)
    financial_data = positional[3] if len(positional) > 3 else kwargs.get("financial_data")
    if isinstance(financial_data, dict):
        for dataset in ("cashflow", "cashflow_interim"):
            for row in financial_data.get(dataset, []):
                if "CONSTRUCT_LONG_ASSET" not in row or "CAPEX_PROVENANCE" in row:
                    continue
                value, provenance = resolve_capex_evidence(
                    row.get("CONSTRUCT_LONG_ASSET"),
                    None,
                    report_date=str(row.get("REPORT_DATE") or ""),
                )
                row["CONSTRUCT_LONG_ASSET"] = value
                row["CAPEX_PROVENANCE"] = provenance
    return scenarios.run_template25(
        *positional,
        reporting_period_contract=reporting_period_contract,
        **kwargs,
    )


def _run_test_override(*args, **kwargs):
    return scenarios.run_template25(*args, _test_override=True, **kwargs)


def _annual_revenue(values):
    start = 2026 - len(values)
    return [{"REPORT_DATE": f"{start + i}-12-31", "TOTAL_OPERATE_INCOME": value} for i, value in enumerate(values)]


def _nonfinancial_data():
    return {
        "cashflow": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "NETCASH_OPERATE": 20.0 + (year - 2021),
                "CONSTRUCT_LONG_ASSET": 5.0,
            }
            for year in range(2021, 2026)
        ],
        "balance": [
            {
                "REPORT_DATE": "2025-12-31",
                "TOTAL_EQUITY": 100.0,
                "SHORT_LOAN": 20.0,
                "MONETARYFUNDS": 10.0,
            }
        ],
        "income_history": _annual_revenue([100, 105, 110, 115, 120]),
        "income_interim": [
            {"REPORT_DATE": "2025-03-31", "TOTAL_OPERATE_INCOME": 25.0, "PARENT_NETPROFIT": 2.5},
            {"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": 27.5, "PARENT_NETPROFIT": 2.75},
        ],
        "cashflow_interim": [
            {"REPORT_DATE": "2025-03-31", "NETCASH_OPERATE": 4.0, "CONSTRUCT_LONG_ASSET": 1.0},
            {"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": 4.4, "CONSTRUCT_LONG_ASSET": 1.1},
        ],
    }


def _financial_data(use_minority=False):
    balances = []
    income = []
    equities = {year: 550.0 + (year - 2020) * 50.0 for year in range(2020, 2026)}
    for year, parent_equity in equities.items():
        row = {"REPORT_DATE": f"{year}-12-31"}
        if use_minority:
            row.update({"TOTAL_EQUITY": parent_equity + 200.0, "MINORITY_EQUITY": 200.0})
        else:
            row["PARENT_EQUITY"] = parent_equity
            row["TOTAL_EQUITY"] = parent_equity + 200.0
        balances.append(row)
        if year > 2020:
            average_equity = (equities[year - 1] + parent_equity) / 2
            income.append(
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "PARENT_NETPROFIT": average_equity * 0.12,
                }
            )
    return {
        "balance": balances,
        "income_history": income,
        "income_interim": [
            {"REPORT_DATE": "2025-03-31", "PARENT_NETPROFIT": 20.0},
            {"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": 22.0},
        ],
    }


def _market_beta(code="600519", *, blume_beta=0.90, r_squared=0.30):
    return {
        "available": True,
        "code": code,
        "benchmark_code": "000300",
        "as_of": "2026-07-15",
        "source": "Tencent Finance",
        "source_url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "start_date": "2023-06-30",
        "end_date": "2026-07-15",
        "price_observations": 157,
        "sample_size": 156,
        "raw_beta": 0.85,
        "blume_beta": blume_beta,
        "r_squared": r_squared,
        "cache_key": f"market:{code}:2026-07-15",
        "cache_hit": True,
        "reason": "",
    }


def _strict_ttm_case(cutoff, *, prior_revenue, current_revenue, prior_cfo, current_cfo, prior_capex, current_capex):
    data = _nonfinancial_data()
    prior_date = f"2025-{cutoff}"
    current_date = f"2026-{cutoff}"
    data["income_interim"] = [
        {"REPORT_DATE": prior_date, "TOTAL_OPERATE_INCOME": prior_revenue, "PARENT_NETPROFIT": 2.5},
        {"REPORT_DATE": current_date, "TOTAL_OPERATE_INCOME": current_revenue, "PARENT_NETPROFIT": 2.8},
    ]
    data["cashflow_interim"] = [
        {
            "REPORT_DATE": prior_date,
            "NETCASH_OPERATE": prior_cfo,
            "CONSTRUCT_LONG_ASSET": prior_capex,
        },
        {
            "REPORT_DATE": current_date,
            "NETCASH_OPERATE": current_cfo,
            "CONSTRUCT_LONG_ASSET": current_capex,
        },
    ]
    contract = ReportingPeriodContract(
        annual_report_date="2025-12-31",
        current_interim_report_date=current_date,
        prior_interim_report_date=prior_date,
    )
    return data, contract


def test_financial_pb_runs_before_revenue_and_fcf_gates(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    result = scenarios.run_template25(
        code="000001",
        name="银行",
        current_price=8.0,
        financial_data=_financial_data(),
        revenue_history=[],
        total_shares=100.0,
    )
    assert result is not None
    assert result["_pb_valuation"] is True
    assert result["params"]["neutral"]["bvps"] == pytest.approx(8.0)
    assert result["equity_basis"] == "parent_equity"


def test_financial_pb_never_invokes_ttm_reconstruction(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")

    def unexpected_ttm(*_args, **_kwargs):
        raise AssertionError("financial P/B must return before the industrial TTM gate")

    monkeypatch.setattr(scenarios, "reconstruct_ttm_fcff", unexpected_ttm)
    monkeypatch.setattr(scenarios, "reconstruct_ttm_revenue", unexpected_ttm)

    result = scenarios.run_template25("000001", "银行", 8.0, _financial_data(), [], 100.0)

    assert result is not None
    assert result["_pb_valuation"] is True


@pytest.mark.parametrize(
    (
        "cutoff",
        "prior_revenue",
        "current_revenue",
        "prior_cfo",
        "current_cfo",
        "prior_capex",
        "current_capex",
        "expected_revenue",
        "expected_ttm_fcff",
    ),
    [
        ("03-31", 25.0, 30.0, 5.0, 6.0, 1.2, 1.5, 131.0, 19.7),
        ("06-30", 60.0, 70.0, 11.0, 13.0, 2.6, 3.0, 136.0, 20.6),
        ("09-30", 90.0, 105.0, 17.0, 20.0, 3.5, 4.0, 141.0, 21.5),
    ],
)
def test_nonfinancial_uses_strict_q1_h1_q3_ttm_inputs_and_three_period_normalisation(
    monkeypatch,
    cutoff,
    prior_revenue,
    current_revenue,
    prior_cfo,
    current_cfo,
    prior_capex,
    current_capex,
    expected_revenue,
    expected_ttm_fcff,
):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data, contract = _strict_ttm_case(
        cutoff,
        prior_revenue=prior_revenue,
        current_revenue=current_revenue,
        prior_cfo=prior_cfo,
        current_cfo=current_cfo,
        prior_capex=prior_capex,
        current_capex=current_capex,
    )

    result = _run_nonfinancial(
        "000001",
        "样本公司",
        10.0,
        data,
        _annual_revenue([100.0, 108.0, 115.0, 121.0, 126.0]),
        10.0,
        reporting_period_contract=contract,
    )

    assert result is not None
    assert result["valuation_input_basis"] == "strict_ttm"
    assert result["base_revenue"] == pytest.approx(expected_revenue)
    assert result["base_revenue_basis"] == "strict_ttm_reported_revenue"
    assert result["ttm_revenue_evidence"]["value"] == pytest.approx(expected_revenue)
    assert result["ttm_fcff_evidence"]["value"] == pytest.approx(expected_ttm_fcff)
    assert result["ttm_fcff_evidence"]["cash_flow_kind"] == "cfo_less_capex_proxy"
    assert result["latest_fcff"] == pytest.approx(expected_ttm_fcff)
    assert result["recent_fcff"] == pytest.approx([18.0, 19.0, expected_ttm_fcff])
    assert result["fcf_normalisation_basis"] == "recent_median"
    assert result["base_fcf_adjustments"][0]["kind"] == "fcf_margin_ceiling"
    assert result["base_fcf_adjustments"][0]["before"] == pytest.approx(19.0)
    assert result["base_fcf"] == pytest.approx(expected_revenue * result["fcf_margin_ceiling"])
    assert result["base_fcf_basis"] == "normalised_two_annual_plus_ttm_cfo_less_capex_proxy"
    assert result["fcf_normalisation_period_basis"] == "two_annual_plus_strict_ttm"
    assert result["recent_fcff_periods"] == [
        {"kind": "annual", "report_date": "2024-12-31"},
        {"kind": "annual", "report_date": "2025-12-31"},
        {"kind": "ttm", "through_report_date": contract.current_interim_report_date},
    ]


@pytest.mark.parametrize("missing_source", ["fcff", "revenue"])
def test_nonfinancial_ttm_failure_never_falls_back_to_complete_annual_history(monkeypatch, missing_source):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    data = _nonfinancial_data()
    if missing_source == "fcff":
        data["cashflow_interim"][-1].pop("CONSTRUCT_LONG_ASSET")
    else:
        data["income_interim"][-1].pop("TOTAL_OPERATE_INCOME")

    result = _run_nonfinancial(
        "000001",
        "样本公司",
        10.0,
        data,
        _annual_revenue([100.0, 108.0, 115.0, 121.0, 126.0]),
        10.0,
    )

    assert result is None


@pytest.mark.parametrize("nonpositive_source", ["fcff", "revenue"])
def test_nonfinancial_rejects_nonpositive_ttm_values_without_annual_fallback(monkeypatch, nonpositive_source):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    data = _nonfinancial_data()
    if nonpositive_source == "fcff":
        data["cashflow_interim"][0]["NETCASH_OPERATE"] = 100.0
        data["cashflow_interim"][1]["NETCASH_OPERATE"] = 0.0
    else:
        data["income_interim"][0]["TOTAL_OPERATE_INCOME"] = 200.0
        data["income_interim"][1]["TOTAL_OPERATE_INCOME"] = 1.0

    result = _run_nonfinancial(
        "000001",
        "样本公司",
        10.0,
        data,
        _annual_revenue([100.0, 108.0, 115.0, 121.0, 126.0]),
        10.0,
    )

    assert result is None


def test_nonfinancial_requires_fy_minus_one_for_strict_three_period_normalisation(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    data = _nonfinancial_data()
    data["cashflow"] = [row for row in data["cashflow"] if row["REPORT_DATE"] != "2024-12-31"]

    result = _run_nonfinancial(
        "000001",
        "样本公司",
        10.0,
        data,
        _annual_revenue([100.0, 108.0, 115.0, 121.0, 126.0]),
        10.0,
    )

    assert result is None


def test_nonfinancial_strict_ttm_result_is_json_serializable(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    result = _run_nonfinancial(
        "000001",
        "样本公司",
        10.0,
        _nonfinancial_data(),
        _annual_revenue([100.0, 108.0, 115.0, 121.0, 126.0]),
        10.0,
    )

    assert result is not None
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert '"valuation_input_basis": "strict_ttm"' in payload


def test_private_valuation_overrides_require_explicit_test_mode_and_cannot_mix_with_ttm_contract(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    args = (
        "000001",
        "样本公司",
        10.0,
        _nonfinancial_data(),
        _annual_revenue([100.0, 108.0, 115.0, 121.0, 126.0]),
        10.0,
    )

    assert scenarios.run_template25(*args, _pre_fcf=15.0, _pre_rev=100.0) is None
    assert (
        scenarios.run_template25(
            *args,
            _pre_fcf=15.0,
            _pre_rev=100.0,
            _test_override=True,
            reporting_period_contract=Q1_CONTRACT,
        )
        is None
    )
    override = scenarios.run_template25(
        *args,
        _pre_fcf=15.0,
        _pre_rev=100.0,
        _test_override=True,
    )
    assert override is not None
    assert override["valuation_input_basis"] == "test_override"
    assert override["base_fcf_basis"] == "test_override"


def test_unsupported_other_financial_never_falls_into_industrial_fcff(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "FINANCIAL_OTHER")
    data = _nonfinancial_data()

    assert (
        scenarios.run_template25(
            "000563",
            "信托公司",
            10.0,
            data,
            _annual_revenue([100, 105, 110, 115]),
            10.0,
        )
        is None
    )


def test_financial_pb_subtracts_minority_equity_when_parent_field_absent(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "INSURANCE")
    result = scenarios.run_template25(
        code="601000",
        name="保险",
        current_price=8.0,
        financial_data=_financial_data(use_minority=True),
        revenue_history=[],
        total_shares=100.0,
    )
    assert result is not None
    assert result["params"]["neutral"]["bvps"] == pytest.approx(8.0)
    assert result["equity_basis"] == "total_less_minority"


def test_financial_pb_does_not_invent_optimistic_roe_improvement(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "INSURANCE")

    result = scenarios.run_template25("601000", "保险", 8.0, _financial_data(use_minority=True), [], 100.0)

    assert result is not None
    assert result["params"]["optimistic"]["scenario_roe"] == pytest.approx(result["params"]["neutral"]["scenario_roe"])
    assert result["financial_growth_basis"] == "negative_roe_cost_spread_lower_retention_is_better"
    assert result["params"]["optimistic"]["growth"] < result["params"]["neutral"]["growth"]


def test_financial_pb_accepts_exact_same_period_turnaround_without_fake_yoy(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    data = _financial_data()
    data["income_interim"] = [
        {"REPORT_DATE": "2025-03-31", "PARENT_NETPROFIT": -20.0},
        {"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": 22.0},
    ]

    result = scenarios.run_template25("000001", "银行", 8.0, data, [], 100.0)

    assert result is not None
    assert result["current_period_evidence"]["profit"] == 22.0
    assert result["current_period_evidence"]["profit_yoy"] is None
    assert result["current_period_evidence"]["profit_yoy_basis"] == "same_period_turnaround"


def test_financial_pb_uses_negative_growth_for_multi_year_profit_decline(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    data = _financial_data()
    profits = {2021: 120.0, 2022: 110.0, 2023: 100.0, 2024: 90.0, 2025: 80.0}
    for row in data["income_history"]:
        row["PARENT_NETPROFIT"] = profits[int(row["REPORT_DATE"][:4])]

    result = scenarios.run_template25("000001", "银行", 8.0, data, [], 100.0)

    assert result is not None
    assert result["structural_profit_decline"] is True
    assert [result["params"][key]["growth"] for key in scenarios.SCENARIOS] == [-0.02, -0.01, 0.0]


def test_financial_pb_does_not_treat_gapped_profit_years_as_consecutive_decline(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    data = _financial_data()
    data["income_history"] = [
        {"REPORT_DATE": f"{year}-12-31", "PARENT_NETPROFIT": profit}
        for year, profit in ((2010, 20.0), (2023, 15.0), (2024, 10.0), (2025, 5.0))
    ]

    result = scenarios.run_template25("000001", "银行", 8.0, data, [], 100.0)

    assert result is not None
    assert result["roe_evidence_years"] == [2023, 2024, 2025]
    assert result["structural_profit_decline"] is False
    assert result["financial_growth_basis"] != "four_year_attributable_profit_decline"


def test_financial_pb_requires_attributable_equity_and_multi_year_roe(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    total_only = {
        "balance": [{"REPORT_DATE": "2025-12-31", "TOTAL_EQUITY": 1000.0}],
        "income_history": [{"REPORT_DATE": "2025-12-31", "PARENT_NETPROFIT": 100.0}],
    }
    assert scenarios.run_template25("1", "银行", 8.0, total_only, [], 100.0) is None


def test_financial_pb_rejects_latest_annual_loss_current_collapse_and_equity_conflict(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")

    annual_loss = _financial_data()
    annual_loss["income_history"][-1]["PARENT_NETPROFIT"] = -1_000.0
    assert scenarios.run_template25("000001", "银行", 8.0, annual_loss, [], 100.0) is None

    current_collapse = _financial_data()
    current_collapse["income_interim"][-1]["PARENT_NETPROFIT"] = 1.0
    assert scenarios.run_template25("000001", "银行", 8.0, current_collapse, [], 100.0) is None

    contradictory = _financial_data()
    contradictory["balance"][-1].update({"PARENT_EQUITY": 1_500.0, "TOTAL_EQUITY": 100.0, "MINORITY_EQUITY": 10.0})
    assert scenarios.run_template25("000001", "银行", 8.0, contradictory, [], 100.0) is None


def test_financial_pb_rejects_nonconsecutive_or_stale_roe_history(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    nonconsecutive = _financial_data()
    nonconsecutive["income_history"] = [
        row for row in nonconsecutive["income_history"] if not row["REPORT_DATE"].startswith("2024-")
    ]
    assert scenarios.run_template25("1", "银行", 8.0, nonconsecutive, [], 100.0) is None

    stale = _financial_data()
    stale["income_history"] = stale["income_history"][:-1]
    assert scenarios.run_template25("1", "银行", 8.0, stale, [], 100.0) is None


def test_financial_pb_exposes_justified_pb_formula_inputs(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    result = scenarios.run_template25("1", "银行", 8.0, _financial_data(), [], 100.0)
    assert result is not None
    neutral = result["params"]["neutral"]
    expected_pb = (neutral["scenario_roe"] - neutral["growth"]) / (neutral["cost_of_equity"] - neutral["growth"])
    assert neutral["normalised_roe"] == pytest.approx(0.12)
    assert neutral["roe_basis"] == "average_begin_end_attributable_equity"
    assert neutral["formula"].startswith("(normalised_roe")
    assert neutral["pb_lower"] < expected_pb < neutral["pb_upper"]


def test_financial_operating_liabilities_do_not_enter_industrial_wacc(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    baseline = _financial_data()
    leveraged = _financial_data()
    for row in leveraged["balance"]:
        row.update({"SHORT_LOAN": 1_000_000.0, "LONG_LOAN": 1_000_000.0})
    first = scenarios.run_template25("1", "银行", 8.0, baseline, [], 100.0)
    second = scenarios.run_template25("1", "银行", 8.0, leveraged, [], 100.0)
    assert first is not None and second is not None
    assert first["base_wacc"] == second["base_wacc"]
    assert first["dcf_points"] == second["dcf_points"]


def test_buy_zone_above_pessimistic_value_does_not_claim_positive_safety_margin():
    score, margin, _bubble = scenarios._safety_fields(
        price=90.0,
        zone="买入区",
        pessimistic_value=80.0,
        optimistic_value=150.0,
    )

    assert margin < 0
    assert score == ""


@pytest.mark.parametrize(
    "price,shares", [(0.0, 100.0), (-1.0, 100.0), (math.nan, 100.0), (10.0, 0.0), (10.0, math.inf)]
)
def test_template_rejects_invalid_price_or_shares(monkeypatch, price, shares):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    assert _run_nonfinancial("1", "x", price, _nonfinancial_data(), _annual_revenue([100, 105, 110]), shares) is None


def test_six_point_valuation_is_all_or_nothing(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    calls = iter([10.0, 9.0, None, 11.0, 14.0, 13.0])
    monkeypatch.setattr(scenarios, "dcf_valuation", lambda **_: next(calls))
    result = _run_nonfinancial("1", "x", 10.0, _nonfinancial_data(), _annual_revenue([100, 105, 110, 115]), 10.0)
    assert result is None


def test_scenario_bands_are_ordered_and_regions_do_not_overlap(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    result = _run_nonfinancial("1", "x", 10.0, _nonfinancial_data(), _annual_revenue([100, 108, 115, 121, 126]), 10.0)
    assert result is not None
    points = result["dcf_points"]
    assert points["pessimistic"]["lower"] <= points["pessimistic"]["upper"]
    assert points["pessimistic"]["upper"] <= points["neutral"]["lower"]
    assert points["neutral"]["lower"] <= points["neutral"]["upper"]
    assert points["neutral"]["upper"] <= points["optimistic"]["lower"]
    assert points["optimistic"]["lower"] <= points["optimistic"]["upper"]
    assert result["buy_zone_upper"] <= result["sell_zone_lower"]


def test_nonfinancial_result_separates_five_year_template25_from_ten_year_type4_dcf(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)

    result = _run_nonfinancial("1", "x", 10.0, _nonfinancial_data(), _annual_revenue([100, 108, 115, 121, 126]), 10.0)

    assert result is not None
    assert result["explicit_forecast_years"] == 5
    assert result["long_horizon_forecast_years"] == 10
    assert all(params["forecast_years"] == 5 for params in result["params"].values())
    long_points = result["dcf_10y_points"]
    assert long_points is not None
    ordered = [
        long_points[scenario][edge]
        for scenario in ("pessimistic", "neutral", "optimistic")
        for edge in ("lower", "upper")
    ]
    assert ordered == sorted(ordered)
    assert long_points != result["dcf_points"]


def test_declining_history_is_not_replaced_by_positive_five_percent_growth():
    history = _annual_revenue([150, 140, 125, 110, 100])
    rates = scenarios._derive_scenario_growth(history, base_cagr=-0.08)
    assert rates["pessimistic"] <= rates["neutral"] <= rates["optimistic"]
    assert rates["neutral"] < 0


def test_severe_repeated_contraction_is_not_rewritten_as_near_term_recovery():
    history = _annual_revenue([160, 80, 40, 20])

    rates = scenarios._derive_scenario_growth(history, base_cagr=-0.50)

    assert rates == {
        "pessimistic": pytest.approx(-0.50),
        "neutral": pytest.approx(-0.35),
        "optimistic": pytest.approx(-0.20),
    }


def test_recent_slowdown_reduces_neutral_growth_despite_old_high_growth():
    slowing = _annual_revenue([100, 140, 168, 184.8, 194.04])  # 40%, 20%, 10%, 5%
    steady = _annual_revenue([100, 112, 125.44, 140.49, 157.35])  # about 12% each year
    slowing_rate = scenarios._derive_scenario_growth(slowing, base_cagr=0.18)["neutral"]
    steady_rate = scenarios._derive_scenario_growth(steady, base_cagr=0.12)["neutral"]
    assert slowing_rate < steady_rate


def test_scenario_growth_annualises_gaps_in_revenue_history():
    history = [
        {"REPORT_DATE": "2019-12-31", "TOTAL_OPERATE_INCOME": 100.0},
        {"REPORT_DATE": "2021-12-31", "TOTAL_OPERATE_INCOME": 121.0},
        {"REPORT_DATE": "2023-12-31", "TOTAL_OPERATE_INCOME": 146.41},
        {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 177.1561},
    ]
    rates = scenarios._derive_scenario_growth(history, base_cagr=0.10)
    assert rates["neutral"] == pytest.approx(0.10)


def _quality_financial_data(stable=True):
    revenues = [100, 108, 117, 126, 136]
    margins = [0.30, 0.31, 0.30, 0.32, 0.31] if stable else [0.30, -0.10, 0.50, -0.20, 0.31]
    income = []
    cashflow = []
    for i, (revenue, margin) in enumerate(zip(revenues, margins), start=2021):
        income.append(
            {
                "REPORT_DATE": f"{i}-12-31",
                "TOTAL_OPERATE_INCOME": revenue,
                "PARENT_NETPROFIT": revenue * margin,
            }
        )
        cashflow.append(
            {
                "REPORT_DATE": f"{i}-12-31",
                "NETCASH_OPERATE": revenue * 0.30,
                "CONSTRUCT_LONG_ASSET": revenue * 0.05,
            }
        )
    balances = [
        {
            "REPORT_DATE": f"{year}-12-31",
            "PARENT_EQUITY": 90.0 + (year - 2020) * 2.0,
            "TOTAL_EQUITY": 90.0 + (year - 2020) * 2.0,
        }
        for year in range(2020, 2026)
    ]
    return {
        "income_history": income,
        "cashflow": cashflow,
        "balance": balances,
        "income_interim": [
            {
                "REPORT_DATE": "2025-03-31",
                "TOTAL_OPERATE_INCOME": 30.0,
                "PARENT_NETPROFIT": 9.0,
            },
            {
                "REPORT_DATE": "2026-03-31",
                "TOTAL_OPERATE_INCOME": 33.0,
                "PARENT_NETPROFIT": 10.0,
            },
        ],
        "cashflow_interim": [
            {"REPORT_DATE": "2025-03-31", "NETCASH_OPERATE": 9.0, "CONSTRUCT_LONG_ASSET": 1.5},
            {"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": 10.0, "CONSTRUCT_LONG_ASSET": 1.7},
        ],
    }


def test_quality_requires_multi_year_stable_profit_and_cashflow():
    assert scenarios._detect_quality(_quality_financial_data(stable=True), fcf_margin=0.25)
    assert not scenarios._detect_quality(_quality_financial_data(stable=False), fcf_margin=0.25)


def test_current_period_deterioration_removes_quality_and_caps_dcf_growth(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    stable = _quality_financial_data(stable=True)
    baseline = _run_nonfinancial(
        "000001",
        "高质量公司",
        10.0,
        stable,
        _annual_revenue([100, 120, 144, 173, 207]),
        10.0,
    )
    assert baseline is not None and baseline["quality_evidence"] is True

    collapsed = _quality_financial_data(stable=True)
    collapsed["income_interim"][-1].update({"TOTAL_OPERATE_INCOME": 3.0, "PARENT_NETPROFIT": -2.0})
    collapsed["cashflow_interim"][-1]["NETCASH_OPERATE"] = -2.0
    stressed = _run_nonfinancial(
        "000001",
        "高质量公司",
        10.0,
        collapsed,
        _annual_revenue([100, 120, 144, 173, 207]),
        10.0,
    )

    assert scenarios._detect_quality(collapsed, fcf_margin=0.25) is False
    assert stressed is not None
    assert stressed["quality_evidence"] is False
    assert stressed["current_period_evidence"]["growth_cap_basis"] == "current_period_severe_deterioration"
    assert [stressed["params"][name]["growth"] for name in scenarios.SCENARIOS] == pytest.approx([-0.20, -0.10, 0.0])
    assert stressed["dcf_points"] != baseline["dcf_points"]


def test_invalid_or_missing_current_period_cannot_remove_the_conservative_growth_cap():
    data = _nonfinancial_data()
    data["income_interim"] = [{"REPORT_DATE": "zzzzzzzzzz", "TOTAL_OPERATE_INCOME": 999.0, "PARENT_NETPROFIT": 999.0}]
    data["cashflow_interim"] = [{"REPORT_DATE": "zzzzzzzzzz", "NETCASH_OPERATE": 999.0}]

    capped, evidence = scenarios._cap_growth_for_current_period(
        {"pessimistic": 0.05, "neutral": 0.10, "optimistic": 0.20},
        data,
    )

    assert capped == {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0}
    assert evidence["report_date"] is None
    assert evidence["growth_cap_basis"] == "missing_current_period_conservative_cap"


def test_quality_uses_average_equity_and_does_not_drop_one_bad_fcf_year():
    data = _quality_financial_data(stable=True)
    data["cashflow"][1]["NETCASH_OPERATE"] = 0.0
    data["cashflow"][1]["CONSTRUCT_LONG_ASSET"] = 5.0

    assert scenarios._detect_quality(data, fcf_margin=0.25)


def test_quality_requires_distinct_consecutive_recent_years():
    gapped = _quality_financial_data(stable=True)
    for record, year in zip(gapped["income_history"], (2010, 2023, 2024, 2025, 2025)):
        record["REPORT_DATE"] = f"{year}-12-31"
    assert not scenarios._detect_quality(gapped, fcf_margin=0.25)

    duplicate_cashflow = _quality_financial_data(stable=True)
    for record in duplicate_cashflow["cashflow"]:
        record["REPORT_DATE"] = "2025-12-31"
    assert not scenarios._detect_quality(duplicate_cashflow, fcf_margin=0.25)


def test_structural_decline_requires_four_consecutive_recent_years():
    revenues = [
        {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": value}
        for year, value in ((2010, 1000.0), (2023, 900.0), (2024, 800.0), (2025, 700.0))
    ]
    profits = [
        {"REPORT_DATE": f"{year}-12-31", "PARENT_NETPROFIT": value}
        for year, value in ((2010, 100.0), (2023, 80.0), (2024, 60.0), (2025, 40.0))
    ]

    assert scenarios._detect_structural_decline(revenues, profits) == (False, None)


def test_quality_company_retains_margin_slowly_but_not_forever(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BEVERAGE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data = _quality_financial_data(stable=True)
    result = _run_nonfinancial("600519", "高质量公司", 100.0, data, data["income_history"], 10.0)
    assert result is not None
    retentions = [result["params"][name]["margin_retention"] for name in scenarios.SCENARIOS]
    assert 0 < min(retentions) <= max(retentions) < 1.0


def test_evidenced_quality_margin_is_not_forced_down_to_generic_industry_cap(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "ALCOHOL")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data = _quality_financial_data(stable=True)
    result = _run_test_override(
        "600519",
        "长期高质量公司",
        100.0,
        data,
        data["income_history"],
        10.0,
        _pre_fcf=60.0,
        _pre_rev=100.0,
        _pre_quality=True,
    )
    assert result is not None
    assert result["base_fcf"] == pytest.approx(60.0)


@pytest.mark.parametrize(
    ("industry_target", "expected"),
    [(0.03, 0.09), (0.0, 0.08), (0.10, 0.25)],
)
def test_nonquality_margin_ceiling_preserves_industry_signal(industry_target, expected):
    assert scenarios._nonquality_fcf_margin_ceiling(industry_target) == pytest.approx(expected)


def test_nonquality_company_uses_conservative_industry_margin_ceiling(monkeypatch):
    import data.industry as industry

    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "DEFAULT")
    monkeypatch.setattr(industry, "get_industry_fcf_margin", lambda _code: 0.03)
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data = _nonfinancial_data()
    result = _run_test_override(
        "000001",
        "非品质公司",
        10.0,
        data,
        _annual_revenue([100.0, 100.0, 100.0, 100.0, 100.0]),
        10.0,
        _pre_fcf=50.0,
        _pre_rev=100.0,
        _pre_quality=False,
    )

    assert result is not None
    assert result["base_fcf"] == pytest.approx(9.0)
    assert result["fcf_margin_ceiling"] == pytest.approx(0.09)


def test_template_wacc_uses_available_capital_structure(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    result = _run_nonfinancial("1", "x", 10.0, _nonfinancial_data(), _annual_revenue([100, 105, 110, 115]), 10.0)
    assert result is not None
    # Market equity=100, debt=20: industry asset beta is re-levered exactly once.
    assert result["levered_beta"] > result["industry_unlevered_beta"]
    assert result["wacc_capital_structure"] == "market_equity_and_known_debt"
    assert result["beta_source"] == "industry_unlevered_relevered"
    assert result["wacc_components"]["equity_weight"] == pytest.approx(100 / 120)
    assert result["wacc_components"]["debt_weight"] == pytest.approx(20 / 120)
    assert result["tax_shield_rate"] == 0.0
    assert result["tax_shield_source"] == "taxable_profit_evidence_unavailable"


def test_positive_operating_profit_does_not_fabricate_tax_shield_without_tax_evidence(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data = _nonfinancial_data()
    data["income_history"] = [
        {
            "REPORT_DATE": f"{year}-12-31",
            "TOTAL_OPERATE_INCOME": 100.0,
            "OPERATE_PROFIT": 20.0,
            "PARENT_NETPROFIT": profit,
        }
        for year, profit in zip(range(2022, 2026), (10.0, -2.0, 8.0, 9.0))
    ]

    result = _run_nonfinancial("1", "x", 10.0, data, _annual_revenue([100, 105, 110, 115]), 10.0)

    assert result is not None
    assert result["tax_shield_rate"] == 0.0
    assert result["tax_shield_source"] == "taxable_profit_evidence_unavailable"


def test_explicit_beta_is_company_levered_beta_and_is_not_silently_ignored(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    low = _run_nonfinancial(
        "1",
        "x",
        10.0,
        _nonfinancial_data(),
        _annual_revenue([100, 105, 110, 115]),
        10.0,
        beta=0.5,
    )
    high = _run_nonfinancial(
        "1",
        "x",
        10.0,
        _nonfinancial_data(),
        _annual_revenue([100, 105, 110, 115]),
        10.0,
        beta=2.0,
    )
    assert low is not None and high is not None
    assert low["beta_source"] == high["beta_source"] == "explicit_company_levered_beta"
    assert low["levered_beta"] == pytest.approx(0.5)
    assert high["levered_beta"] == pytest.approx(2.0)
    assert low["base_wacc"] < high["base_wacc"]


def test_precomputed_weekly_beta_is_transparently_blended_without_replacing_industry_prior(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "ALCOHOL")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    result = _run_nonfinancial(
        "600519",
        "样本公司",
        10.0,
        _nonfinancial_data(),
        _annual_revenue([100, 105, 110, 115]),
        10.0,
        market_beta_estimate=_market_beta(),
    )

    assert result is not None
    evidence = result["beta_evidence"]
    assert result["beta_source"] == "industry_and_weekly_market_beta_blend"
    assert evidence["company_weight"] == pytest.approx(0.30)
    assert result["levered_beta"] == pytest.approx(evidence["industry_beta"] * 0.70 + evidence["blume_beta"] * 0.30)
    risk = result["risk_parameters"]
    assert result["wacc_components"]["cost_of_equity"] == pytest.approx(
        risk["risk_free_rate"] + result["levered_beta"] * risk["equity_risk_premium"]
    )
    assert result["model_risk_data_as_of"] == risk["model_as_of"]


def test_low_r_squared_weekly_beta_keeps_industry_beta(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "ALCOHOL")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    result = _run_nonfinancial(
        "600519",
        "样本公司",
        10.0,
        _nonfinancial_data(),
        _annual_revenue([100, 105, 110, 115]),
        10.0,
        market_beta_estimate=_market_beta(r_squared=0.01),
    )

    assert result is not None
    assert result["beta_evidence"]["status"] == "industry_only_low_r_squared"
    assert result["levered_beta"] == pytest.approx(result["industry_levered_beta"])


def test_financial_pb_blends_precomputed_weekly_beta_with_financial_industry_prior(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "BANK")
    result = scenarios.run_template25(
        "000001",
        "样本银行",
        8.0,
        _financial_data(),
        [],
        100.0,
        market_beta_estimate=_market_beta(code="000001", blume_beta=1.0, r_squared=0.20),
    )

    assert result is not None
    evidence = result["beta_evidence"]
    assert result["beta_source"] == "industry_and_weekly_market_beta_blend"
    assert evidence["industry_beta_role"] == "industry_financial_levered_beta"
    assert evidence["company_weight"] == pytest.approx(0.20)
    assert result["financial_levered_beta"] == pytest.approx(
        evidence["industry_beta"] * 0.80 + evidence["blume_beta"] * 0.20
    )


def test_valuation_center_is_neutral_midpoint_and_old_mean_field_is_labeled_legacy(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    result = _run_nonfinancial(
        "000001", "样本公司", 10.0, _nonfinancial_data(), _annual_revenue([100, 105, 110, 115]), 10.0
    )

    assert result is not None
    neutral = result["dcf_points"]["neutral"]
    assert result["valuation_center"] == pytest.approx((neutral["lower"] + neutral["upper"]) / 2.0)
    assert result["neutral_value_midpoint"] == pytest.approx(result["valuation_center"])
    assert result["dcf_value_mean"] == pytest.approx(result["buy_zone_upper"])
    assert result["dcf_value_mean_legacy_alias_of"] == "buy_zone_upper"


def test_mixed_profit_without_recovery_requires_three_fcf_years(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    data = _nonfinancial_data()
    data["income_history"] = [
        {"REPORT_DATE": f"{year}-12-31", "PARENT_NETPROFIT": profit, "TOTAL_OPERATE_INCOME": 100.0}
        for year, profit in zip(range(2021, 2026), [-10, 20, -5, 10, -1])
    ]
    data["cashflow"] = data["cashflow"][-2:]

    result = _run_nonfinancial("1", "x", 10.0, data, _annual_revenue([100, 100, 100, 100, 100]), 10.0)

    assert result is None


def test_zone_and_exported_boundaries_use_the_same_unrounded_value(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    values = iter([10.003, 9.0, 12.0, 10.009, 14.0, 13.0])
    monkeypatch.setattr(scenarios, "dcf_valuation", lambda **_: next(values))

    result = _run_nonfinancial("1", "x", 10.008, _nonfinancial_data(), _annual_revenue([100, 105, 110]), 10.0)

    assert result is not None
    assert result["buy_zone_upper"] == pytest.approx(10.006)
    assert result["zone"] == "观察区"


def test_revenue_only_structural_decline_blocks_positive_industry_and_terminal_growth(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(
        scenarios,
        "blend_scenario_growth",
        lambda _values, _industry: {name: 0.20 for name in scenarios.SCENARIOS},
    )
    values = iter([10.0, 9.0, 12.0, 11.0, 14.0, 13.0])
    monkeypatch.setattr(scenarios, "dcf_valuation", lambda **_: next(values))
    history = _annual_revenue([200, 170, 140, 110, 80])
    data = _nonfinancial_data()
    data["income_history"] = [{**row, "PARENT_NETPROFIT": row["TOTAL_OPERATE_INCOME"] * 0.10} for row in history]

    result = _run_test_override("1", "decliner", 1.0, data, history, 10.0, _pre_fcf=10.0, _pre_rev=80.0)

    assert result is not None
    assert result["structural_decline"] is True
    assert result["structural_decline_evidence"] == "revenue_multi_year_decline"
    assert result["params"]["neutral"]["growth"] <= -0.15
    assert result["params"]["optimistic"]["growth"] <= -0.10
    assert all(result["params"][name]["terminal_g"] <= 0 for name in scenarios.SCENARIOS)


def test_industry_asset_beta_changes_cost_of_capital(monkeypatch):
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data = _nonfinancial_data()
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "POWER_UTILITY")
    utility = _run_nonfinancial("1", "utility", 10.0, data, _annual_revenue([100, 105, 110, 115]), 10.0)
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    software = _run_nonfinancial("1", "software", 10.0, data, _annual_revenue([100, 105, 110, 115]), 10.0)
    assert utility is not None and software is not None
    assert utility["industry_unlevered_beta"] < software["industry_unlevered_beta"]
    assert utility["base_wacc"] < software["base_wacc"]


def test_missing_growth_evidence_is_explicit_and_keeps_zero_neutral_fallback(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    data = _nonfinancial_data()
    result = _run_test_override(
        "1",
        "x",
        10.0,
        data,
        [],
        10.0,
        _pre_rev=100.0,
        _pre_fcf=15.0,
    )
    assert result is not None
    assert result["growth_evidence"] == "missing_fallback_zero"
    assert result["params"]["neutral"]["growth"] == pytest.approx(0.0)


def test_no_growth_history_does_not_invent_an_optimistic_recovery():
    rates = scenarios._derive_scenario_growth([], base_cagr=0.0)
    assert rates == {
        "pessimistic": pytest.approx(0.0),
        "neutral": pytest.approx(0.0),
        "optimistic": pytest.approx(0.0),
    }


def test_limited_real_history_drives_growth_instead_of_using_missing_fallback(monkeypatch):
    monkeypatch.setattr(scenarios, "classify_industry", lambda *_: "SOFTWARE")
    monkeypatch.setattr(scenarios, "blend_scenario_growth", lambda values, _: values)
    data = _nonfinancial_data()
    history = _annual_revenue([100.0, 110.0])
    result = _run_nonfinancial("1", "x", 10.0, data, history, 10.0)
    assert result is not None
    assert result["growth_evidence"] == "limited_annual_history"
    assert result["params"]["neutral"]["growth"] == pytest.approx(0.10)


def test_growth_normalisation_is_ordered_for_random_blended_inputs():
    rng = random.Random(20260715)
    fallback = {"pessimistic": -0.05, "neutral": 0.0, "optimistic": 0.05}
    for _ in range(500):
        candidate = {scenario: rng.uniform(-1.0, 1.0) for scenario in scenarios.SCENARIOS}
        result = scenarios._normalise_growth_rates(candidate, fallback)
        assert result is not None
        values = [result[scenario] for scenario in scenarios.SCENARIOS]
        assert values == sorted(values)
        assert -0.15 <= values[0] <= 0.15
        assert -0.10 <= values[1] <= 0.20
        assert -0.05 <= values[2] <= 0.30
