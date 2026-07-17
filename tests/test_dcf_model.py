import math
import random

import pytest

from data.capex_evidence import resolve_capex_evidence
from engine.dcf import (
    ReportingPeriodContract,
    compute_wacc,
    dcf_valuation,
    dcf_valuation_fading_growth,
    extract_debt_and_cash,
    extract_fcf_from_cashflow,
    extract_net_debt,
    extract_revenue_cagr,
    reconstruct_ttm_fcff,
    reconstruct_ttm_revenue,
    relever_beta,
)


@pytest.fixture
def ttm_period_contract():
    return ReportingPeriodContract(
        annual_report_date="2025-12-31",
        current_interim_report_date="2026-06-30",
        prior_interim_report_date="2025-06-30",
    )


def _cashflow_row(report_date, operating_cash_flow, capex):
    value, provenance = resolve_capex_evidence(capex, None, report_date=report_date)
    return {
        "REPORT_DATE": report_date,
        "NETCASH_OPERATE": operating_cash_flow,
        "CONSTRUCT_LONG_ASSET": value,
        "CAPEX_PROVENANCE": provenance,
    }


def test_ttm_fcff_reconstructs_comparable_cumulative_periods_with_auditable_metadata(ttm_period_contract):
    annual = [
        {
            "REPORT_DATE": "2025-12-31",
            "NETCASH_OPERATE": 100.0,
            # Source APIs may report Capex as a negative cash-flow amount.
            "CONSTRUCT_LONG_ASSET": -20.0,
        }
    ]
    interim = [
        {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 60.0, "CONSTRUCT_LONG_ASSET": -15.0},
        {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 40.0, "CONSTRUCT_LONG_ASSET": 10.0},
    ]

    outcome = reconstruct_ttm_fcff(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "complete"
    assert outcome["value"] == pytest.approx(95.0)
    assert outcome["formula_version"] == "ttm_cfo_less_capex_v2"
    assert outcome["cash_flow_kind"] == "cfo_less_capex_proxy"
    assert outcome["period_basis"] == "FY_plus_current_YTD_minus_prior_YTD"
    assert outcome["period"] == {
        "basis": "FY_plus_current_YTD_minus_prior_YTD",
        "annual_report_date": "2025-12-31",
        "current_interim_report_date": "2026-06-30",
        "prior_interim_report_date": "2025-06-30",
    }
    assert outcome["unit"] == "CNY"
    assert outcome["components"]["annual"]["capex_raw"] == pytest.approx(-20.0)
    assert outcome["components"]["annual"]["capex_absolute"] == pytest.approx(20.0)
    assert outcome["components"]["current_interim"]["capex_absolute"] == pytest.approx(15.0)
    assert outcome["components"]["reconstructed_operating_cash_flow"] == pytest.approx(120.0)
    assert outcome["components"]["reconstructed_capex"] == pytest.approx(25.0)
    assert outcome["components"]["reconstructed_fcff"] == pytest.approx(95.0)


def test_ttm_revenue_reconstructs_fy_plus_current_ytd_less_prior_ytd(ttm_period_contract):
    annual = [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 1_000.0}]
    interim = [
        {"REPORT_DATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 600.0},
        {"REPORT_DATE": "2025-06-30", "TOTAL_OPERATE_INCOME": 450.0},
    ]

    outcome = reconstruct_ttm_revenue(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "complete"
    assert outcome["value"] == pytest.approx(1_150.0)
    assert outcome["formula_version"] == "ttm_revenue_v1"
    assert outcome["cash_flow_kind"] == "reported_revenue"
    assert outcome["period_basis"] == "FY_plus_current_YTD_minus_prior_YTD"
    assert outcome["unit"] == "CNY"
    assert outcome["components"]["annual"]["revenue"] == pytest.approx(1_000.0)
    assert outcome["components"]["current_interim"]["revenue"] == pytest.approx(600.0)
    assert outcome["components"]["prior_interim"]["revenue"] == pytest.approx(450.0)
    assert outcome["components"]["reconstructed_revenue"] == pytest.approx(1_150.0)


@pytest.mark.parametrize(
    "contract",
    [
        ReportingPeriodContract("2025-12-30", "2026-06-30", "2025-06-30"),
        ReportingPeriodContract("2025-12-31", "2026-06-30", "2025-03-31"),
        ReportingPeriodContract("2025-12-31", "2025-06-30", "2024-06-30"),
        ReportingPeriodContract("2025-12-31", "2026-05-31", "2025-05-31"),
        ReportingPeriodContract("2025-12-31T00:00:00", "2026-06-30", "2025-06-30"),
    ],
)
def test_ttm_reconstruction_rejects_invalid_or_mismatched_period_contracts(contract):
    fcff = reconstruct_ttm_fcff([], [], period_contract=contract)
    revenue = reconstruct_ttm_revenue([], [], period_contract=contract)

    assert fcff["status"] == "invalid_period_contract"
    assert revenue["status"] == "invalid_period_contract"
    assert fcff["value"] is None and revenue["value"] is None
    assert fcff["components"] and revenue["components"]


def test_ttm_reconstruction_reports_missing_component_without_falling_back_to_wrong_date(ttm_period_contract):
    annual = [{"REPORT_DATE": "2024-12-31", "NETCASH_OPERATE": 100.0, "CONSTRUCT_LONG_ASSET": 20.0}]
    interim = [
        {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 60.0, "CONSTRUCT_LONG_ASSET": 15.0},
        {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 40.0, "CONSTRUCT_LONG_ASSET": 10.0},
    ]

    outcome = reconstruct_ttm_fcff(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "missing_component"
    assert outcome["value"] is None
    assert outcome["components"]["annual"]["row_count"] == 0


def test_ttm_fcff_reports_missing_capex_instead_of_assuming_zero(ttm_period_contract):
    annual = [{"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 100.0, "CONSTRUCT_LONG_ASSET": 20.0}]
    interim = [
        {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 60.0},
        {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 40.0, "CONSTRUCT_LONG_ASSET": 10.0},
    ]

    outcome = reconstruct_ttm_fcff(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "missing_component"
    assert outcome["value"] is None
    assert outcome["components"]["current_interim"]["capex_absolute"] is None


def test_ttm_fcff_strict_mode_requires_and_revalidates_capex_provenance(ttm_period_contract):
    annual = [_cashflow_row("2025-12-31", 100.0, 20.0)]
    interim = [
        _cashflow_row("2026-06-30", 60.0, 15.0),
        _cashflow_row("2025-06-30", 40.0, 10.0),
    ]

    complete = reconstruct_ttm_fcff(
        annual,
        interim,
        period_contract=ttm_period_contract,
        require_capex_provenance=True,
    )
    assert complete["status"] == "complete"
    assert complete["components"]["annual"]["capex_provenance_status"] == "complete"

    missing = [dict(annual[0])]
    missing[0].pop("CAPEX_PROVENANCE")
    missing_outcome = reconstruct_ttm_fcff(
        missing,
        interim,
        period_contract=ttm_period_contract,
        require_capex_provenance=True,
    )
    assert missing_outcome["status"] == "missing_capex_provenance"

    tampered = [dict(annual[0])]
    tampered[0]["CAPEX_PROVENANCE"] = dict(tampered[0]["CAPEX_PROVENANCE"])
    tampered[0]["CAPEX_PROVENANCE"]["value"] = 21.0
    tampered_outcome = reconstruct_ttm_fcff(
        tampered,
        interim,
        period_contract=ttm_period_contract,
        require_capex_provenance=True,
    )
    assert tampered_outcome["status"] == "invalid_capex_provenance"


@pytest.mark.parametrize("duplicate_label", ["annual", "current_interim", "prior_interim"])
def test_ttm_reconstruction_rejects_duplicate_target_periods(ttm_period_contract, duplicate_label):
    annual = [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 1_000.0}]
    interim = [
        {"REPORT_DATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 600.0},
        {"REPORT_DATE": "2025-06-30", "TOTAL_OPERATE_INCOME": 450.0},
    ]
    if duplicate_label == "annual":
        annual.append(dict(annual[0]))
    elif duplicate_label == "current_interim":
        interim.append(dict(interim[0]))
    else:
        interim.append(dict(interim[1]))

    outcome = reconstruct_ttm_revenue(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "duplicate_period"
    assert outcome["value"] is None
    assert outcome["components"][duplicate_label]["row_count"] == 2


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("NETCASH_OPERATE", math.nan),
        ("NETCASH_OPERATE", math.inf),
        ("NETCASH_OPERATE", True),
        ("CONSTRUCT_LONG_ASSET", -math.inf),
    ],
)
def test_ttm_fcff_rejects_nonfinite_components_and_preserves_period_evidence(
    ttm_period_contract,
    field,
    bad_value,
):
    annual = [{"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 100.0, "CONSTRUCT_LONG_ASSET": 20.0}]
    interim = [
        {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 60.0, "CONSTRUCT_LONG_ASSET": 15.0},
        {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 40.0, "CONSTRUCT_LONG_ASSET": 10.0},
    ]
    interim[0][field] = bad_value

    outcome = reconstruct_ttm_fcff(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "nonfinite_component"
    assert outcome["value"] is None
    assert outcome["components"]["current_interim"]["report_date"] == "2026-06-30"


def test_ttm_revenue_distinguishes_missing_field_from_nonfinite_field(ttm_period_contract):
    annual = [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 1_000.0}]
    missing = [
        {"REPORT_DATE": "2026-06-30"},
        {"REPORT_DATE": "2025-06-30", "TOTAL_OPERATE_INCOME": 450.0},
    ]
    nonfinite = [
        {"REPORT_DATE": "2026-06-30", "TOTAL_OPERATE_INCOME": math.nan},
        {"REPORT_DATE": "2025-06-30", "TOTAL_OPERATE_INCOME": 450.0},
    ]

    missing_outcome = reconstruct_ttm_revenue(annual, missing, period_contract=ttm_period_contract)
    nonfinite_outcome = reconstruct_ttm_revenue(annual, nonfinite, period_contract=ttm_period_contract)

    assert missing_outcome["status"] == "missing_component"
    assert nonfinite_outcome["status"] == "nonfinite_component"
    assert missing_outcome["components"]["current_interim"]["revenue"] is None
    assert nonfinite_outcome["components"]["current_interim"]["revenue"] is None


def test_ttm_fcff_fails_closed_when_absolute_capex_reconstruction_is_negative(ttm_period_contract):
    annual = [{"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 100.0, "CONSTRUCT_LONG_ASSET": -10.0}]
    interim = [
        {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 60.0, "CONSTRUCT_LONG_ASSET": -2.0},
        {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 40.0, "CONSTRUCT_LONG_ASSET": -20.0},
    ]

    outcome = reconstruct_ttm_fcff(annual, interim, period_contract=ttm_period_contract)

    assert outcome["status"] == "negative_reconstructed_capex"
    assert outcome["value"] is None
    assert outcome["components"]["reconstructed_operating_cash_flow"] == pytest.approx(120.0)
    assert outcome["components"]["reconstructed_capex"] == pytest.approx(-8.0)


def test_terminal_value_grows_with_terminal_rate_not_forecast_rate():
    value = dcf_valuation(
        base_fcf=10.0,
        base_revenue=100.0,
        revenue_growth=0.20,
        wacc=0.10,
        terminal_g=0.02,
        shares_outstanding=1.0,
        forecast_years=1,
        fcf_margin_target=0.10,
    )

    year_one_fcf = 100.0 * 1.20 * 0.10
    expected = year_one_fcf / 1.10
    expected += (year_one_fcf * 1.02 / (0.10 - 0.02)) / 1.10
    assert value == pytest.approx(expected)


def test_explicit_margin_target_is_honoured_and_changes_value():
    common = dict(
        base_fcf=10.0,
        base_revenue=100.0,
        revenue_growth=0.0,
        wacc=0.10,
        terminal_g=0.0,
        shares_outstanding=1.0,
        forecast_years=3,
    )
    low = dcf_valuation(**common, fcf_margin_target=0.05)
    high = dcf_valuation(**common, fcf_margin_target=0.20)
    assert low is not None and high is not None
    assert high > low


def test_default_long_term_margin_never_inflates_a_low_margin_company():
    value = dcf_valuation(
        base_fcf=2.0,
        base_revenue=100.0,
        revenue_growth=0.0,
        wacc=0.10,
        terminal_g=0.0,
        shares_outstanding=1.0,
        forecast_years=2,
        margin_retention=0.60,
    )
    expected = 2.0 / 1.10 + 2.0 / 1.10**2
    expected += (2.0 / 0.10) / 1.10**2
    assert value == pytest.approx(expected)


def test_long_horizon_dcf_fades_growth_to_terminal_rate_instead_of_holding_it_constant():
    common = dict(
        base_fcf=10.0,
        base_revenue=100.0,
        revenue_growth=0.20,
        wacc=0.10,
        terminal_g=0.02,
        shares_outstanding=1.0,
        forecast_years=10,
        fcf_margin_target=0.10,
    )
    fading = dcf_valuation_fading_growth(**common)
    constant = dcf_valuation(**common)

    assert fading is not None and constant is not None
    assert fading < constant


def test_long_horizon_dcf_matches_explicit_faded_growth_formula():
    value = dcf_valuation_fading_growth(
        base_fcf=10.0,
        base_revenue=100.0,
        revenue_growth=0.10,
        wacc=0.12,
        terminal_g=0.02,
        shares_outstanding=2.0,
        net_debt=5.0,
        forecast_years=3,
        fcf_margin_target=0.10,
    )

    growth_path = [0.10, 0.06, 0.02]
    revenue = 100.0
    fcffs = []
    for growth in growth_path:
        revenue *= 1.0 + growth
        fcffs.append(revenue * 0.10)
    explicit = sum(fcf / 1.12**year for year, fcf in enumerate(fcffs, start=1))
    terminal = fcffs[-1] * 1.02 / (0.12 - 0.02) / 1.12**3
    expected = (explicit + terminal - 5.0) / 2.0

    assert value == pytest.approx(expected)


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_fcf": 0.0},
        {"base_fcf": -1.0},
        {"base_fcf": math.nan},
        {"base_revenue": math.inf},
        {"revenue_growth": -1.0},
        {"wacc": math.nan},
        {"wacc": 0.02, "terminal_g": 0.02},
        {"shares_outstanding": 0.0},
        {"shares_outstanding": math.inf},
        {"net_debt": math.nan},
        {"forecast_years": 0},
        {"forecast_years": 2.5},
        {"margin_retention": -0.1},
        {"fcf_margin_target": math.inf},
        {"revenue_growth": 1e308},
        {"wacc": 1e308},
    ],
)
def test_dcf_rejects_invalid_or_non_finite_inputs(overrides):
    params = dict(
        base_fcf=10.0,
        base_revenue=100.0,
        revenue_growth=0.05,
        wacc=0.10,
        terminal_g=0.02,
        shares_outstanding=10.0,
        net_debt=0.0,
        forecast_years=5,
        margin_retention=0.6,
    )
    params.update(overrides)
    assert dcf_valuation(**params) is None


def test_fcf_median_keeps_negative_and_zero_years():
    rows = [
        {"NETCASH_OPERATE": -90, "CONSTRUCT_LONG_ASSET": 10},
        {"NETCASH_OPERATE": 20, "CONSTRUCT_LONG_ASSET": 0},
        {"NETCASH_OPERATE": 0, "CONSTRUCT_LONG_ASSET": 10},
    ]
    assert extract_fcf_from_cashflow(rows) == pytest.approx(-10.0)


def test_fcf_skips_years_without_capex_instead_of_assuming_zero():
    rows = [
        {"NETCASH_OPERATE": 100},
        {"NETCASH_OPERATE": 50, "CONSTRUCT_LONG_ASSET": 20},
    ]
    assert extract_fcf_from_cashflow(rows) == pytest.approx(30.0)
    assert extract_fcf_from_cashflow([{"NETCASH_OPERATE": 100}]) is None


def test_fcf_normalisation_uses_recent_years_and_latest_value_during_persistent_decline():
    rows = [
        {
            "REPORT_DATE": f"{year}-12-31",
            "NETCASH_OPERATE": fcf + 5.0,
            "CONSTRUCT_LONG_ASSET": 5.0,
        }
        for year, fcf in zip(range(2021, 2026), (40.0, 30.0, 20.0, 10.0, 5.0))
    ]

    assert extract_fcf_from_cashflow(rows) == pytest.approx(5.0)


def test_fcf_normalisation_fails_closed_on_latest_loss_and_caps_old_median():
    def rows(values):
        return [
            {
                "REPORT_DATE": f"{2023 + index}-12-31",
                "NETCASH_OPERATE": value + 5.0,
                "CONSTRUCT_LONG_ASSET": 5.0,
            }
            for index, value in enumerate(values)
        ]

    assert extract_fcf_from_cashflow(rows([100.0, 110.0, -10.0])) == pytest.approx(-10.0)
    assert extract_fcf_from_cashflow(rows([100.0, 110.0, 10.0])) == pytest.approx(12.5)


def test_invalid_dated_cashflow_row_cannot_override_latest_fcff():
    rows = [
        {
            "REPORT_DATE": f"{year}-12-31",
            "NETCASH_OPERATE": fcf + 5.0,
            "CONSTRUCT_LONG_ASSET": 5.0,
        }
        for year, fcf in ((2023, 10.0), (2024, 20.0), (2025, 30.0))
    ]
    rows.append({"REPORT_DATE": "zzzzzzzzzz", "NETCASH_OPERATE": 1_005.0, "CONSTRUCT_LONG_ASSET": 5.0})

    assert extract_fcf_from_cashflow(rows) == pytest.approx(20.0)


def test_net_debt_uses_interest_bearing_debt_less_cash():
    row = {
        "REPORT_DATE": "2025-12-31",
        "SHORT_LOAN": 100.0,
        "LONG_LOAN": 250.0,
        "BONDS_PAYABLE": 50.0,
        "MONETARYFUNDS": 300.0,
        "TOTAL_LIABILITIES": 10_000.0,
    }
    debt, cash, debt_known = extract_debt_and_cash([row])
    assert (debt, cash, debt_known) == pytest.approx((400.0, 300.0, True))
    assert extract_net_debt([row]) == pytest.approx(100.0)


def test_industrial_net_debt_includes_short_bonds_but_excludes_financial_funding():
    row = {
        "REPORT_DATE": "2025-12-31",
        "SHORT_LOAN": 100.0,
        "SHORT_BONDS_PAYABLE": 50.0,
        "MONETARYFUNDS": 25.0,
        # These are operating funding sources for financial institutions and
        # must never leak into the industrial FCFF/net-debt model.
        "BORROW_FUNDS": 1_000.0,
        "CENTRAL_BANK_BORROWING": 2_000.0,
        "SUBORDINATED_BONDS_PAYABLE": 3_000.0,
    }
    debt, cash, known = extract_debt_and_cash([row])
    assert known is True
    assert debt == pytest.approx(150.0)
    assert cash == pytest.approx(25.0)
    assert extract_net_debt([row]) == pytest.approx(125.0)


def test_net_debt_does_not_create_fake_net_cash_when_debt_fields_absent():
    row = {"MONETARYFUNDS": 300.0, "TOTAL_LIABILITIES": 10_000.0}
    assert extract_net_debt([row]) == 0.0


def test_null_debt_placeholders_do_not_create_fake_net_cash():
    row = {
        "REPORT_DATE": "2025-12-31",
        "SHORT_LOAN": None,
        "SHORT_BONDS_PAYABLE": None,
        "LONG_LOAN": None,
        "BONDS_PAYABLE": None,
        "NONCURRENT_LIAB_1YEAR": None,
        "LEASE_LIAB": None,
        "MONETARYFUNDS": 300.0,
    }

    assert extract_debt_and_cash([row]) == pytest.approx((0.0, 300.0, False))
    assert extract_net_debt([row]) == 0.0


def test_reported_zero_debt_is_known_and_allows_real_net_cash():
    row = {
        "REPORT_DATE": "2025-12-31",
        "SHORT_LOAN": 0.0,
        "LONG_LOAN": 0.0,
        "MONETARYFUNDS": 300.0,
    }

    assert extract_debt_and_cash([row]) == pytest.approx((0.0, 300.0, True))
    assert extract_net_debt([row]) == pytest.approx(-300.0)


def test_wacc_uses_market_value_weights_and_tax_shield():
    value = compute_wacc(
        risk_free=0.025,
        beta=1.0,
        erp=0.06,
        debt=40.0,
        equity=60.0,
        pre_tax_cost_of_debt=0.055,
        tax_rate=0.25,
    )
    expected = 0.60 * 0.085 + 0.40 * 0.055 * 0.75
    assert value == pytest.approx(expected)


def test_unlevered_industry_beta_is_relevered_once_before_wacc():
    assert relever_beta(0.8, debt=40.0, equity=60.0, tax_rate=0.25) == pytest.approx(1.2)
    value = compute_wacc(
        risk_free=0.025,
        beta=None,
        erp=0.06,
        debt=40.0,
        equity=60.0,
        pre_tax_cost_of_debt=0.055,
        tax_rate=0.25,
        industry_unlevered_beta=0.8,
    )
    expected = 0.60 * (0.025 + 1.20 * 0.06) + 0.40 * 0.055 * 0.75
    assert value == pytest.approx(expected)


def test_wacc_conservatively_falls_back_to_cost_of_equity_without_structure():
    assert compute_wacc(risk_free=0.025, beta=1.0, erp=0.06) == pytest.approx(0.085)
    assert compute_wacc(risk_free=0.025, beta=1.0, erp=0.06, debt=10, equity=None) == pytest.approx(0.085)
    assert compute_wacc(
        risk_free=0.025,
        beta=None,
        erp=0.06,
        debt=None,
        equity=100,
        industry_unlevered_beta=1.0,
    ) == pytest.approx(0.085)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_wacc_rejects_invalid_beta(bad):
    assert compute_wacc(beta=bad) is None


def test_revenue_cagr_uses_elapsed_calendar_years():
    records = [
        {"REPORT_DATE": "2020-12-31", "TOTAL_OPERATE_INCOME": 100.0},
        {"REPORT_DATE": "2022-12-31", "TOTAL_OPERATE_INCOME": 121.0},
        {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 161.051},
    ]
    assert extract_revenue_cagr(records) == pytest.approx(0.10)


def test_dcf_economic_monotonicity_over_random_valid_inputs():
    rng = random.Random(20260715)
    for _ in range(200):
        revenue = rng.uniform(100.0, 10_000.0)
        margin = rng.uniform(0.02, 0.40)
        growth = rng.uniform(-0.05, 0.20)
        wacc = rng.uniform(0.08, 0.20)
        terminal_growth = rng.uniform(0.0, min(0.04, wacc - 0.02))
        common = dict(
            base_fcf=revenue * margin,
            base_revenue=revenue,
            revenue_growth=growth,
            terminal_g=terminal_growth,
            shares_outstanding=rng.uniform(10.0, 1_000.0),
            net_debt=rng.uniform(-1.0, 1.0),
            forecast_years=5,
            fcf_margin_target=margin,
        )
        base = dcf_valuation(**common, wacc=wacc)
        higher_discount = dcf_valuation(**common, wacc=wacc + 0.01)
        more_debt = dcf_valuation(**{**common, "net_debt": common["net_debt"] + 0.1}, wacc=wacc)
        higher_terminal_growth = dcf_valuation(
            **{**common, "terminal_g": min(wacc - 0.01, terminal_growth + 0.005)},
            wacc=wacc,
        )
        assert base is not None and higher_discount is not None and more_debt is not None
        assert higher_terminal_growth is not None
        assert higher_discount < base
        assert more_debt < base
        assert higher_terminal_growth > base
