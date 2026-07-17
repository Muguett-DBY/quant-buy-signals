"""Template 25: three evidence-based valuation scenarios and safety zones."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Optional

from config import (
    BAND_WACC_DELTA,
    BUBBLE_RATIO,
    DEFAULT_PRETAX_COST_OF_DEBT,
    DEFAULT_UNLEVERED_BETA,
    DEEP_SAFETY_RATIO,
    FORECAST_YEARS,
    INDUSTRY_FINANCIAL_LEVERED_BETA,
    INDUSTRY_PRETAX_COST_OF_DEBT,
    INDUSTRY_UNLEVERED_BETA,
    LONG_HORIZON_FORECAST_YEARS,
    MARGINAL_TAX_RATE,
    SCENARIO_WACC_SHIFT,
    TERMINAL_GROWTH,
)
from data.industry import blend_scenario_growth, classify_industry
from engine.dcf import (
    MAX_NORMALISED_FCFF_PREMIUM,
    ReportingPeriodContract,
    compute_wacc,
    dcf_valuation,
    dcf_valuation_fading_growth,
    extract_debt_and_cash,
    extract_fcf_normalisation,
    extract_net_debt,
    extract_revenue_cagr,
    reconstruct_ttm_fcff,
    reconstruct_ttm_revenue,
    relever_beta,
)
from engine.risk import RiskParameterSet, blend_company_market_beta, resolve_risk_parameters


SCENARIOS = ["pessimistic", "neutral", "optimistic"]

# Excess margins mean-revert at different speeds.  Even a proven quality
# company is not assumed to preserve 100% of an exceptional margin forever.
MARGIN_RETENTION = {
    "pessimistic": 0.40,
    "neutral": 0.60,
    "optimistic": 0.85,
}
QUALITY_MARGIN_RETENTION = {
    "pessimistic": 0.70,
    "neutral": 0.80,
    "optimistic": 0.90,
}

_FINANCIAL_INDUSTRIES = ("BANK", "INSURANCE", "SECURITIES")
_UNSUPPORTED_FINANCIAL_INDUSTRIES = ("FINANCIAL_OTHER",)
NONQUALITY_FCF_MARGIN_FLOOR = 0.08
NONQUALITY_FCF_MARGIN_CAP = 0.25
NONQUALITY_INDUSTRY_MARGIN_MULTIPLIER = 3.0


def _as_finite(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _nonquality_fcf_margin_ceiling(industry_target: float) -> float:
    target = _as_finite(industry_target)
    scaled = (target if target is not None and target > 0 else 0.0) * NONQUALITY_INDUSTRY_MARGIN_MULTIPLIER
    return min(NONQUALITY_FCF_MARGIN_CAP, max(NONQUALITY_FCF_MARGIN_FLOOR, scaled))


@lru_cache(maxsize=256)
def _canonical_date_text(text: str) -> str | None:
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return text if parsed.isoformat() == text else None


def _canonical_report_date(row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    raw = row.get("REPORT_DATE")
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    text = str(raw or "").strip()[:10]
    return _canonical_date_text(text)


def _strict_cashflow_components(row: dict) -> tuple[float, float] | None:
    operating_cash = None
    capex = None
    for key in ("NETCASH_OPERATE", "经营活动产生的现金流量净额"):
        if key in row and row.get(key) not in (None, ""):
            operating_cash = _as_finite(row.get(key))
            break
    for key in (
        "CONSTRUCT_LONG_ASSET",
        "PAY_ACQ_CONST_FIASSETS",
        "购建固定资产无形资产和其他长期资产支付的现金",
    ):
        if key in row and row.get(key) not in (None, ""):
            raw_capex = _as_finite(row.get(key))
            capex = abs(raw_capex) if raw_capex is not None else None
            break
    if operating_cash is None or capex is None:
        return None
    return operating_cash, capex


def _ttm_fcf_normalisation(
    annual_cashflow: object,
    ttm_fcff_evidence: Mapping[str, Any],
    period_contract: ReportingPeriodContract,
) -> tuple[dict[str, object], list[dict[str, object]]] | None:
    """Normalise the exact FY-1/FY/TTM FCFF sequence with the existing policy."""
    if not isinstance(annual_cashflow, (list, tuple)):
        return None
    try:
        annual_year = int(period_contract.annual_report_date[:4])
    except (TypeError, ValueError):
        return None
    prior_annual_date = f"{annual_year - 1}-12-31"
    prior_matches = [
        row for row in annual_cashflow if isinstance(row, dict) and _canonical_report_date(row) == prior_annual_date
    ]
    if len(prior_matches) != 1:
        return None
    prior_components = _strict_cashflow_components(prior_matches[0])
    components = ttm_fcff_evidence.get("components")
    if prior_components is None or not isinstance(components, dict):
        return None

    annual_component = components.get("annual")
    if not isinstance(annual_component, dict):
        return None
    annual_operating = _as_finite(annual_component.get("operating_cash_flow"))
    annual_capex = _as_finite(annual_component.get("capex_absolute"))
    reconstructed_operating = _as_finite(components.get("reconstructed_operating_cash_flow"))
    reconstructed_capex = _as_finite(components.get("reconstructed_capex"))
    if any(value is None for value in (annual_operating, annual_capex, reconstructed_operating, reconstructed_capex)):
        return None

    prior_operating, prior_capex = prior_components
    strict_period_rows = [
        {
            "REPORT_DATE": prior_annual_date,
            "NETCASH_OPERATE": prior_operating,
            "CONSTRUCT_LONG_ASSET": prior_capex,
        },
        {
            "REPORT_DATE": period_contract.annual_report_date,
            "NETCASH_OPERATE": annual_operating,
            "CONSTRUCT_LONG_ASSET": annual_capex,
        },
        {
            "REPORT_DATE": period_contract.current_interim_report_date,
            "NETCASH_OPERATE": reconstructed_operating,
            "CONSTRUCT_LONG_ASSET": reconstructed_capex,
        },
    ]
    normalisation = extract_fcf_normalisation(strict_period_rows)
    recent = normalisation.get("recent_fcff")
    if not isinstance(recent, tuple) or len(recent) != 3:
        return None
    periods = [
        {
            "kind": "annual",
            "report_date": prior_annual_date,
        },
        {
            "kind": "annual",
            "report_date": period_contract.annual_report_date,
        },
        {
            "kind": "ttm",
            "through_report_date": period_contract.current_interim_report_date,
        },
    ]
    return normalisation, periods


def run_template25(
    code: str,
    name: str,
    current_price: float,
    financial_data: dict,
    revenue_history: list[dict],
    total_shares: float,
    beta: float = None,
    _pre_fcf: float = None,
    _pre_rev: float = None,
    _pre_quality: bool = None,
    _pre_cagr: float = None,
    _pre_net_debt: float = None,
    industry_unlevered_beta: float = None,
    pre_tax_cost_of_debt: float = None,
    market_beta_estimate: Any = None,
    risk_parameters: Mapping[str, Any] | RiskParameterSet | None = None,
    reporting_period_contract: ReportingPeriodContract | None = None,
    _test_override: bool = False,
) -> Optional[dict]:
    """Run a complete six-point DCF, or return ``None`` if evidence is invalid."""
    price = _as_finite(current_price)
    shares = _as_finite(total_shares)
    if price is None or shares is None or price <= 0 or shares <= 0:
        return None
    if not isinstance(financial_data, dict):
        return None
    if not isinstance(revenue_history, list):
        return None

    resolved_risk = resolve_risk_parameters(risk_parameters)

    balance_data = financial_data.get("balance", [])
    industry_code = classify_industry(str(code), str(name))

    # Financial firms are valued on attributable book equity.  They do not
    # need to pass an industrial FCFF/revenue gate first.
    if industry_code in _FINANCIAL_INDUSTRIES:
        return _run_financial_pb_valuation(
            code,
            name,
            price,
            financial_data,
            shares,
            beta,
            industry_code,
            market_beta_estimate,
            resolved_risk,
        )
    if industry_code in _UNSUPPORTED_FINANCIAL_INDUSTRIES:
        # Trusts, leasing and financial holdings require business-specific
        # balance-sheet/cash-flow models.  Industrial FCFF is not a safe proxy.
        return None

    cashflow_data = financial_data.get("cashflow", [])
    override_requested = _pre_fcf is not None or _pre_rev is not None
    if override_requested:
        # These legacy underscore-prefixed inputs exist only for focused model
        # tests.  Production evidence and test inputs must never be combined.
        if _test_override is not True or reporting_period_contract is not None or _pre_fcf is None or _pre_rev is None:
            return None
        base_revenue = _as_finite(_pre_rev)
        base_fcf = _as_finite(_pre_fcf)
        if base_revenue is None or base_revenue <= 0 or base_fcf is None or base_fcf <= 0:
            return None
        fcf_normalisation: dict[str, object] = {
            "normalised_fcf": base_fcf,
            "latest_fcff": base_fcf,
            "recent_fcff": (base_fcf,),
            "basis": "test_override",
            "premium_cap": MAX_NORMALISED_FCFF_PREMIUM,
        }
        valuation_input_basis = "test_override"
        base_revenue_basis = "test_override"
        base_fcf_basis = "test_override"
        ttm_fcff_evidence = None
        ttm_revenue_evidence = None
        recent_fcff_periods: list[dict[str, object]] = []
        fcf_normalisation_period = {
            "period_set": "test_override",
            "periods": [],
            "normalisation_method": "test_override",
            "cash_flow_kind": "test_override",
            "formula_version": None,
        }
    else:
        if not isinstance(reporting_period_contract, ReportingPeriodContract):
            return None
        interim_cashflow = financial_data.get("cashflow_interim", [])
        interim_income = financial_data.get("income_interim", [])
        ttm_fcff_evidence = reconstruct_ttm_fcff(
            cashflow_data,
            interim_cashflow,
            period_contract=reporting_period_contract,
            require_capex_provenance=True,
        )
        ttm_revenue_evidence = reconstruct_ttm_revenue(
            revenue_history,
            interim_income,
            period_contract=reporting_period_contract,
        )
        if ttm_fcff_evidence.get("status") != "complete" or ttm_revenue_evidence.get("status") != "complete":
            return None
        ttm_fcff = _as_finite(ttm_fcff_evidence.get("value"))
        base_revenue = _as_finite(ttm_revenue_evidence.get("value"))
        if ttm_fcff is None or ttm_fcff <= 0 or base_revenue is None or base_revenue <= 0:
            return None
        normalisation_result = _ttm_fcf_normalisation(
            cashflow_data,
            ttm_fcff_evidence,
            reporting_period_contract,
        )
        if normalisation_result is None:
            return None
        fcf_normalisation, recent_fcff_periods = normalisation_result
        base_fcf = _as_finite(fcf_normalisation.get("normalised_fcf"))
        if base_fcf is None or base_fcf <= 0:
            return None
        valuation_input_basis = "strict_ttm"
        base_revenue_basis = "strict_ttm_reported_revenue"
        base_fcf_basis = "normalised_two_annual_plus_ttm_cfo_less_capex_proxy"
        fcf_normalisation_period = {
            "period_set": "two_annual_plus_strict_ttm",
            "periods": recent_fcff_periods,
            "normalisation_method": fcf_normalisation.get("basis"),
            "cash_flow_kind": ttm_fcff_evidence.get("cash_flow_kind"),
            "formula_version": ttm_fcff_evidence.get("formula_version"),
        }
    base_fcf_adjustments: list[dict[str, object]] = []

    # Mixed profit cycles need positive, sequential recovery evidence.  If not,
    # the lower-quartile FCFF is used; a non-positive lower quartile means the
    # historical data cannot support a positive DCF.
    income_history = financial_data.get("income_history", [])
    if isinstance(income_history, dict):
        income_history = [income_history]
    ordered_profit = _annual_values(income_history, ("PARENT_NETPROFIT",))
    profit_values = [value for _, value in ordered_profit]
    if len(profit_values) >= 3 and min(profit_values) < 0 < max(profit_values):
        recent = profit_values[-3:]
        recovering = recent[0] < recent[1] < recent[2] and recent[2] > 0
        if not recovering:
            annual_fcffs = _annual_fcff_values(cashflow_data)
            if len(annual_fcffs) < 3:
                return None
            sorted_fcff = sorted(annual_fcffs)
            index = (len(sorted_fcff) - 1) * 0.25
            lower = int(index)
            upper = min(lower + 1, len(sorted_fcff) - 1)
            p25 = sorted_fcff[lower] + (sorted_fcff[upper] - sorted_fcff[lower]) * (index - lower)
            if p25 <= 0:
                return None
            adjusted_fcf = min(base_fcf, p25)
            if adjusted_fcf != base_fcf:
                base_fcf_adjustments.append(
                    {
                        "kind": "mixed_profit_cycle_p25_cap",
                        "before": base_fcf,
                        "limit": p25,
                        "after": adjusted_fcf,
                    }
                )
            base_fcf = adjusted_fcf

    from data.industry import get_industry_fcf_margin

    industry_target = _as_finite(get_industry_fcf_margin(industry_code)) or 0.0
    fcf_margin = base_fcf / base_revenue
    quality = (
        bool(_pre_quality) if _pre_quality is not None else _detect_quality(financial_data, fcf_margin)
    ) and _current_period_supports_quality(financial_data)
    if quality:
        # A high but evidenced margin is allowed; impossible working-capital
        # spikes are still capped.  Mean reversion remains in every scenario.
        margin_ceiling = 0.65
        margin_retention = QUALITY_MARGIN_RETENTION
    else:
        margin_ceiling = _nonquality_fcf_margin_ceiling(industry_target)
        margin_retention = MARGIN_RETENTION
    if fcf_margin > margin_ceiling:
        capped_fcf = base_revenue * margin_ceiling
        base_fcf_adjustments.append(
            {
                "kind": "fcf_margin_ceiling",
                "before": base_fcf,
                "limit": capped_fcf,
                "after": capped_fcf,
            }
        )
        base_fcf = capped_fcf
        fcf_margin = margin_ceiling

    net_debt = _pre_net_debt if _pre_net_debt is not None else extract_net_debt(balance_data)
    net_debt = _as_finite(net_debt)
    if net_debt is None:
        return None

    gross_debt, _cash, debt_known = extract_debt_and_cash(balance_data)
    market_equity = price * shares
    beta_u = _as_finite(industry_unlevered_beta)
    if beta_u is None:
        beta_u = _as_finite(INDUSTRY_UNLEVERED_BETA.get(industry_code, DEFAULT_UNLEVERED_BETA))
    debt_cost = _as_finite(pre_tax_cost_of_debt)
    if debt_cost is None:
        debt_cost = _as_finite(INDUSTRY_PRETAX_COST_OF_DEBT.get(industry_code, DEFAULT_PRETAX_COST_OF_DEBT))
    if beta_u is None or debt_cost is None:
        return None
    # The upstream contract does not include taxable profit, income-tax
    # expense, or cash-tax evidence.  Positive operating profit is before
    # interest and cannot prove that a debt tax shield is realizable.  Keep the
    # shield at zero until the snapshot contract carries auditable tax fields.
    tax_shield_rate = 0.0
    tax_shield_source = "taxable_profit_evidence_unavailable"
    industry_levered_beta = relever_beta(beta_u, gross_debt, market_equity, tax_shield_rate) if debt_known else beta_u
    beta_blend = blend_company_market_beta(
        code=code,
        industry_levered_beta=industry_levered_beta,
        industry_beta_role=("industry_unlevered_relevered" if debt_known else "industry_unlevered_asset_fallback"),
        market_beta_estimate=market_beta_estimate,
        explicit_company_beta=beta,
    )
    if beta_blend is None:
        return None
    levered_beta = beta_blend.final_beta
    beta_source = beta_blend.beta_source
    base_wacc = compute_wacc(
        risk_free=resolved_risk.risk_free_rate,
        beta=levered_beta,
        erp=resolved_risk.equity_risk_premium,
        debt=gross_debt,
        equity=market_equity if debt_known else None,
        pre_tax_cost_of_debt=debt_cost,
        tax_rate=tax_shield_rate,
    )
    if base_wacc is None:
        return None
    if levered_beta is None:
        return None
    if debt_known:
        capital = market_equity + gross_debt
        if capital <= 0:
            return None
        equity_weight = market_equity / capital
        debt_weight = gross_debt / capital
    else:
        equity_weight, debt_weight = 1.0, 0.0
    cost_of_equity = resolved_risk.risk_free_rate + levered_beta * resolved_risk.equity_risk_premium
    wacc_components = {
        "equity_weight": equity_weight,
        "debt_weight": debt_weight,
        "cost_of_equity": cost_of_equity,
        "pre_tax_cost_of_debt": debt_cost,
        "tax_shield_rate": tax_shield_rate,
    }

    if _pre_cagr is not None:
        base_cagr = _as_finite(_pre_cagr)
        growth_evidence = "precomputed_cagr" if base_cagr is not None else "missing_fallback_zero"
    else:
        base_cagr = _as_finite(extract_revenue_cagr(revenue_history))
        if base_cagr is not None:
            growth_evidence = "historical_cagr_and_trend"
        elif len(_annual_values(revenue_history, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"))) >= 2:
            growth_evidence = "limited_annual_history"
        else:
            growth_evidence = "missing_fallback_zero"
    if base_cagr is None:
        base_cagr = 0.0
    growth_rates = _derive_scenario_growth(revenue_history, base_cagr)
    # With no company growth evidence, an industry benchmark must not silently
    # turn the conservative zero fallback into a positive company forecast.
    blended = (
        growth_rates.copy()
        if growth_evidence == "missing_fallback_zero"
        else blend_scenario_growth(growth_rates.copy(), industry_code)
    )
    # A positive industry story may temper upside assumptions, but it cannot
    # reverse an observed company contraction into growth.
    if growth_rates["neutral"] < 0 and isinstance(blended, dict):
        blended = {
            scenario: min(
                _as_finite(blended.get(scenario))
                if _as_finite(blended.get(scenario)) is not None
                else growth_rates[scenario],
                growth_rates[scenario],
            )
            for scenario in SCENARIOS
        }
    growth_rates = _normalise_growth_rates(
        blended,
        growth_rates,
        allow_severe_decline=growth_rates["neutral"] < -0.10,
    )
    if growth_rates is None:
        return None
    growth_rates, current_period_evidence = _cap_growth_for_current_period(growth_rates, financial_data)

    structural_decline, decline_evidence = _detect_structural_decline(revenue_history, income_history)

    dcf_points: dict[str, dict[str, float]] = {}
    scenario_params: dict[str, dict] = {}
    for scenario in SCENARIOS:
        growth = growth_rates[scenario]
        wacc_center = base_wacc + SCENARIO_WACC_SHIFT[scenario]
        terminal_growth = TERMINAL_GROWTH[scenario]
        if structural_decline:
            terminal_growth = {
                "pessimistic": min(terminal_growth, -0.02),
                "neutral": min(terminal_growth, -0.01),
                "optimistic": min(terminal_growth, 0.0),
            }[scenario]
        upper = dcf_valuation(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=growth,
            wacc=wacc_center - BAND_WACC_DELTA,
            terminal_g=terminal_growth,
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=margin_retention[scenario],
            forecast_years=FORECAST_YEARS,
        )
        lower = dcf_valuation(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=growth,
            wacc=wacc_center + BAND_WACC_DELTA,
            terminal_g=terminal_growth,
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=margin_retention[scenario],
            forecast_years=FORECAST_YEARS,
        )
        if not _valid_positive_value(upper) or not _valid_positive_value(lower):
            return None
        dcf_points[scenario] = {"upper": float(upper), "lower": float(lower)}
        scenario_params[scenario] = {
            "growth": growth,
            "wacc_base": wacc_center,
            "terminal_g": terminal_growth,
            "margin_retention": margin_retention[scenario],
            "forecast_years": FORECAST_YEARS,
            "growth_evidence": growth_evidence,
            "current_period_growth_cap_basis": current_period_evidence["growth_cap_basis"],
            "structural_decline_evidence": decline_evidence,
        }

    if not _ordered_scenario_bands(dcf_points):
        return None

    # Patch6 Type4 needs a separately labelled ten-year terminal comparison.
    # Its growth path fades to the terminal rate; extending the five-year
    # constant-growth model would otherwise overstate a long runway.  Failure
    # here does not erase a valid Template25 result: Type4 validates this
    # evidence independently and fails closed when it is unavailable.
    dcf_10y_points: dict[str, dict[str, float]] | None = {}
    for scenario in SCENARIOS:
        scenario_param = scenario_params[scenario]
        long_upper = dcf_valuation_fading_growth(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=scenario_param["growth"],
            wacc=scenario_param["wacc_base"] - BAND_WACC_DELTA,
            terminal_g=scenario_param["terminal_g"],
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=scenario_param["margin_retention"],
            forecast_years=LONG_HORIZON_FORECAST_YEARS,
        )
        long_lower = dcf_valuation_fading_growth(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=scenario_param["growth"],
            wacc=scenario_param["wacc_base"] + BAND_WACC_DELTA,
            terminal_g=scenario_param["terminal_g"],
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=scenario_param["margin_retention"],
            forecast_years=LONG_HORIZON_FORECAST_YEARS,
        )
        if not _valid_positive_value(long_upper) or not _valid_positive_value(long_lower):
            dcf_10y_points = None
            break
        dcf_10y_points[scenario] = {"upper": float(long_upper), "lower": float(long_lower)}
    if dcf_10y_points is not None and not _ordered_scenario_bands(dcf_10y_points):
        dcf_10y_points = None

    pessimistic_upper = dcf_points["pessimistic"]["upper"]
    neutral_lower = dcf_points["neutral"]["lower"]
    neutral_upper = dcf_points["neutral"]["upper"]
    optimistic_lower = dcf_points["optimistic"]["lower"]
    mean1 = (pessimistic_upper + neutral_lower) / 2.0
    mean2 = (neutral_upper + optimistic_lower) / 2.0
    valuation_center = (neutral_lower + neutral_upper) / 2.0
    if not (_valid_positive_value(mean1) and _valid_positive_value(mean2) and mean1 <= mean2):
        return None

    if price <= mean1:
        zone = "买入区"
    elif price >= mean2:
        zone = "卖出区"
    else:
        zone = "观察区"

    safety_score, safety_margin_pct, bubble_warning = _safety_fields(
        price,
        zone,
        pessimistic_upper,
        dcf_points["optimistic"]["upper"],
    )
    return {
        "code": code,
        "name": name,
        "current_price": price,
        "dcf_points": dcf_points,
        "dcf_10y_points": dcf_10y_points,
        "explicit_forecast_years": FORECAST_YEARS,
        "long_horizon_forecast_years": LONG_HORIZON_FORECAST_YEARS,
        "long_horizon_growth_path": "linear_fade_from_scenario_growth_to_terminal_growth",
        "long_horizon_formula_version": 1,
        "zone": zone,
        "safety_score": safety_score,
        "safety_margin_pct": round(safety_margin_pct, 2),
        "bubble_warning": bubble_warning,
        "mean1": mean1,
        "mean2": mean2,
        "valuation_center": valuation_center,
        "neutral_value_midpoint": valuation_center,
        "dcf_value_mean": mean1,
        "dcf_value_mean_legacy_alias_of": "buy_zone_upper",
        "buy_zone_upper": mean1,
        "sell_zone_lower": mean2,
        "params": scenario_params,
        "base_wacc": round(base_wacc, 4),
        "base_fcf": base_fcf,
        "base_revenue": base_revenue,
        "valuation_input_basis": valuation_input_basis,
        "base_revenue_basis": base_revenue_basis,
        "base_fcf_basis": base_fcf_basis,
        "ttm_fcff_evidence": ttm_fcff_evidence,
        "ttm_revenue_evidence": ttm_revenue_evidence,
        "shares_outstanding": shares,
        "latest_fcff": fcf_normalisation.get("latest_fcff"),
        "recent_fcff": list(fcf_normalisation.get("recent_fcff", ())),
        "recent_fcff_periods": recent_fcff_periods,
        "fcf_normalisation_basis": fcf_normalisation.get("basis"),
        "fcf_normalisation_period_basis": (
            "test_override" if valuation_input_basis == "test_override" else "two_annual_plus_strict_ttm"
        ),
        "fcf_normalisation_period": fcf_normalisation_period,
        "base_fcf_adjustments": base_fcf_adjustments,
        "normalisation_premium_cap": MAX_NORMALISED_FCFF_PREMIUM,
        "fcf_margin_ceiling": margin_ceiling,
        "industry_fcf_margin_target": industry_target,
        "net_debt": net_debt,
        "industry_code": industry_code,
        "quality_evidence": quality,
        "wacc_capital_structure": "market_equity_and_known_debt" if debt_known else "cost_of_equity_fallback",
        "beta_source": beta_source,
        "wacc_components": wacc_components,
        "tax_shield_source": tax_shield_source,
        "growth_evidence": growth_evidence,
        "current_period_evidence": current_period_evidence,
        "industry_unlevered_beta": beta_u,
        "industry_levered_beta": industry_levered_beta,
        "levered_beta": levered_beta,
        "beta_evidence": beta_blend.to_dict(),
        "pre_tax_cost_of_debt": debt_cost,
        "marginal_tax_rate": MARGINAL_TAX_RATE,
        "tax_shield_rate": tax_shield_rate,
        "structural_decline": structural_decline,
        "structural_decline_evidence": decline_evidence,
        "model_risk_data_as_of": resolved_risk.model_as_of,
        "risk_parameters": resolved_risk.to_dict(),
    }


def _valid_positive_value(value) -> bool:
    number = _as_finite(value)
    return number is not None and number > 0


def _ordered_scenario_bands(points: dict) -> bool:
    try:
        pes_l = points["pessimistic"]["lower"]
        pes_u = points["pessimistic"]["upper"]
        neu_l = points["neutral"]["lower"]
        neu_u = points["neutral"]["upper"]
        opt_l = points["optimistic"]["lower"]
        opt_u = points["optimistic"]["upper"]
    except (KeyError, TypeError):
        return False
    values = (pes_l, pes_u, neu_l, neu_u, opt_l, opt_u)
    if not all(_valid_positive_value(value) for value in values):
        return False
    tolerance = max(values) * 1e-12
    return (
        pes_l <= pes_u + tolerance
        and pes_u <= neu_l + tolerance
        and neu_l <= neu_u + tolerance
        and neu_u <= opt_l + tolerance
        and opt_l <= opt_u + tolerance
    )


def _ordered_financial_scenario_bands(points: dict) -> bool:
    """Allow justified-P/B uncertainty bands to overlap without reversing scenarios.

    A shared cost-of-equity range naturally makes adjacent P/B bands overlap.
    Requiring complete non-overlap discarded otherwise valid financial firms.
    The central estimates and the two actionable thresholds must still be
    monotonically ordered, and every individual band must remain valid.
    """
    try:
        pes_l = float(points["pessimistic"]["lower"])
        pes_u = float(points["pessimistic"]["upper"])
        neu_l = float(points["neutral"]["lower"])
        neu_u = float(points["neutral"]["upper"])
        opt_l = float(points["optimistic"]["lower"])
        opt_u = float(points["optimistic"]["upper"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    values = (pes_l, pes_u, neu_l, neu_u, opt_l, opt_u)
    if not all(_valid_positive_value(value) for value in values):
        return False
    tolerance = max(values) * 1e-12
    midpoints = ((pes_l + pes_u) / 2.0, (neu_l + neu_u) / 2.0, (opt_l + opt_u) / 2.0)
    buy_boundary = (pes_u + neu_l) / 2.0
    sell_boundary = (neu_u + opt_l) / 2.0
    return (
        pes_l <= pes_u + tolerance
        and neu_l <= neu_u + tolerance
        and opt_l <= opt_u + tolerance
        and midpoints[0] <= midpoints[1] + tolerance
        and midpoints[1] <= midpoints[2] + tolerance
        and buy_boundary <= sell_boundary + tolerance
    )


def _safety_fields(price: float, zone: str, pessimistic_value: float, optimistic_value: float):
    safety_margin = (pessimistic_value - price) / pessimistic_value * 100.0
    safety_score = ""
    if zone == "买入区":
        if price <= pessimistic_value * DEEP_SAFETY_RATIO:
            safety_score = "★★★ 深度安全边际"
        elif price <= pessimistic_value:
            safety_score = "★★ 中度安全边际"
        # The buy boundary blends pessimistic and neutral values, so a price
        # may be inside the buy zone while still above the pessimistic value.
        # In that case the margin is correctly negative and must not receive a
        # contradictory safety star.
    return safety_score, safety_margin, price >= optimistic_value * BUBBLE_RATIO


_PARENT_EQUITY_KEYS = (
    "PARENT_EQUITY",
    "TOTAL_PARENT_EQUITY",
    "TOTAL_EQUITY_ATTR_P",
    "EQUITY_ATTRIBUTABLE_TO_PARENT",
    "PARENT_NET_ASSETS",
    "归属于母公司股东权益",
)
_MINORITY_EQUITY_KEYS = (
    "MINORITY_EQUITY",
    "MINORITY_INTEREST",
    "少数股东权益",
)


def _equity_from_row(row: dict, allow_total_fallback: bool = False) -> tuple[Optional[float], str]:
    if not isinstance(row, dict):
        return None, "missing"
    parent = None
    for key in _PARENT_EQUITY_KEYS:
        value = _as_finite(row.get(key))
        if value is not None and value > 0:
            parent = value
            break

    total = _as_finite(row.get("TOTAL_EQUITY"))
    minority = None
    for key in _MINORITY_EQUITY_KEYS:
        minority = _as_finite(row.get(key))
        if minority is not None:
            break
    assets = _as_finite(row.get("TOTAL_ASSETS"))
    if parent is not None:
        if assets is not None and assets > 0 and parent > assets * 1.03 and (total is None or minority is None):
            return None, "accounting_identity_conflict"
        if total is not None:
            if minority is None:
                if total <= 0 < parent or (total > 0 and parent > total * 1.03):
                    return None, "accounting_identity_conflict"
            else:
                scale = max(abs(total), abs(parent) + abs(minority), 1.0)
                if abs(total - parent - minority) > scale * 0.03:
                    return None, "accounting_identity_conflict"
        return parent, "parent_equity"
    if total is not None and minority is not None:
        attributable = total - minority
        if attributable > 0 and not (assets is not None and assets > 0 and total > assets * 1.03):
            return attributable, "total_less_minority"
    if allow_total_fallback and total is not None and total > 0:
        return total, "total_equity_fallback"
    return None, "missing"


def _extract_attributable_equity(
    balance_data,
    allow_total_fallback: bool = False,
) -> tuple[Optional[float], str]:
    if isinstance(balance_data, dict):
        balance_data = [balance_data]
    rows = []
    for row in balance_data or []:
        if not isinstance(row, dict):
            continue
        report_date = str(row.get("REPORT_DATE", "")).strip()
        if _canonical_date_text(report_date) is None:
            continue
        rows.append(row)
    if not rows:
        return None, "missing"
    latest = max(rows, key=lambda row: str(row.get("REPORT_DATE", "")))
    return _equity_from_row(latest, allow_total_fallback=allow_total_fallback)


def _annual_attributable_equity(balance_data) -> tuple[dict[int, float], str]:
    if isinstance(balance_data, dict):
        balance_data = [balance_data]
    by_year: dict[int, tuple[bool, str, float, str]] = {}
    for row in balance_data or []:
        if not isinstance(row, dict):
            continue
        date = str(row.get("REPORT_DATE", ""))
        if len(date) < 4 or not date[:4].isdigit():
            continue
        equity, basis = _equity_from_row(row, allow_total_fallback=False)
        if equity is None:
            continue
        year = int(date[:4])
        candidate = (date.endswith("12-31"), date, equity, basis)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    bases = {item[3] for item in by_year.values()}
    basis = next(iter(bases)) if len(bases) == 1 else "mixed_attributable_equity_sources"
    return {year: item[2] for year, item in by_year.items()}, basis


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _interim_rows(value) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    rows = []
    for row in value:
        if not isinstance(row, dict):
            continue
        report_date = str(row.get("REPORT_DATE", "")).strip()
        if _canonical_date_text(report_date) is None:
            continue
        rows.append(dict(row))
    return sorted(rows, key=lambda row: str(row.get("REPORT_DATE", "")))


def _same_period_yoy(rows: list[dict], keys: tuple[str, ...]) -> tuple[Optional[float], Optional[float], str]:
    if not rows:
        return None, None, "missing_current_period"
    latest = rows[-1]
    latest_date = str(latest.get("REPORT_DATE", ""))[:10]
    current = next((_as_finite(latest.get(key)) for key in keys if _as_finite(latest.get(key)) is not None), None)
    if len(latest_date) != 10 or not latest_date[:4].isdigit() or current is None:
        return current, None, "missing_current_period"
    prior_date = f"{int(latest_date[:4]) - 1}{latest_date[4:]}"
    prior_row = next((row for row in rows if str(row.get("REPORT_DATE", ""))[:10] == prior_date), None)
    if prior_row is None:
        return current, None, "missing_same_period_comparator"
    prior = next(
        (_as_finite(prior_row.get(key)) for key in keys if _as_finite(prior_row.get(key)) is not None),
        None,
    )
    if prior is None:
        return current, None, "invalid_same_period_comparator"
    if prior <= 0:
        basis = "same_period_turnaround" if current > 0 else "same_period_nonpositive_comparison"
        return current, None, basis
    yoy = current / prior - 1.0
    return (
        current,
        yoy,
        "same_period_yoy" if yoy is not None and math.isfinite(yoy) else "invalid_same_period_comparator",
    )


def _current_period_evidence(financial_data: dict) -> dict:
    income = _interim_rows(financial_data.get("income_interim", []))
    cashflow = _interim_rows(financial_data.get("cashflow_interim", []))
    revenue, revenue_yoy, revenue_basis = _same_period_yoy(
        income,
        ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
    )
    profit, profit_yoy, profit_basis = _same_period_yoy(income, ("PARENT_NETPROFIT",))
    operating_cash_flow, ocf_yoy, ocf_basis = _same_period_yoy(cashflow, ("NETCASH_OPERATE",))
    return {
        "report_date": str(income[-1].get("REPORT_DATE", ""))[:10] if income else None,
        "revenue": revenue,
        "revenue_yoy": revenue_yoy,
        "revenue_yoy_basis": revenue_basis,
        "profit": profit,
        "profit_yoy": profit_yoy,
        "profit_yoy_basis": profit_basis,
        "operating_cash_flow": operating_cash_flow,
        "operating_cash_flow_yoy": ocf_yoy,
        "operating_cash_flow_yoy_basis": ocf_basis,
    }


def _current_period_supports_quality(financial_data: dict) -> bool:
    evidence = _current_period_evidence(financial_data)
    if evidence["profit"] is None or evidence["profit"] <= 0:
        return False
    if evidence["operating_cash_flow"] is None or evidence["operating_cash_flow"] <= 0:
        return False
    if evidence["profit_yoy_basis"] != "same_period_yoy" or evidence["profit_yoy"] <= -0.20:
        return False
    if evidence["operating_cash_flow_yoy_basis"] != "same_period_yoy" or evidence["operating_cash_flow_yoy"] <= -0.30:
        return False
    revenue_yoy = evidence["revenue_yoy"]
    return evidence["revenue_yoy_basis"] == "same_period_yoy" and revenue_yoy > -0.15


def _cap_growth_for_current_period(growth_rates: dict, financial_data: dict) -> tuple[dict, dict]:
    """Apply only downward, ordered caps from the latest comparable period."""
    evidence = _current_period_evidence(financial_data)
    caps = {"pessimistic": 1.0, "neutral": 1.0, "optimistic": 1.0}
    comparable = all(
        evidence.get(key) == "same_period_yoy"
        for key in ("revenue_yoy_basis", "profit_yoy_basis", "operating_cash_flow_yoy_basis")
    ) and all(evidence.get(key) is not None for key in ("revenue", "profit", "operating_cash_flow"))
    if not comparable:
        caps = {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0}
        evidence["growth_cap_basis"] = "missing_current_period_conservative_cap"
        return (
            {scenario: min(float(growth_rates[scenario]), caps[scenario]) for scenario in SCENARIOS},
            evidence,
        )
    severe = any(
        evidence[key] is not None and evidence[key] <= 0 for key in ("revenue", "profit", "operating_cash_flow")
    ) or any(
        evidence[key] is not None and evidence[key] <= -0.50
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    material = any(
        evidence[key] is not None and evidence[key] <= -0.20
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    weakening = any(
        evidence[key] is not None and evidence[key] < 0
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    if severe:
        caps = {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0}
        evidence["growth_cap_basis"] = "current_period_severe_deterioration"
    elif material:
        caps = {"pessimistic": -0.10, "neutral": -0.05, "optimistic": 0.05}
        evidence["growth_cap_basis"] = "current_period_material_deterioration"
    elif weakening:
        caps = {"pessimistic": -0.05, "neutral": 0.0, "optimistic": 0.10}
        evidence["growth_cap_basis"] = "current_period_weakening"
    else:
        evidence["growth_cap_basis"] = "no_current_period_downward_cap"
    capped = {scenario: min(float(growth_rates[scenario]), caps[scenario]) for scenario in SCENARIOS}
    return capped, evidence


def _run_financial_pb_valuation(
    code,
    name,
    current_price,
    financial_data,
    total_shares,
    beta,
    industry_code,
    market_beta_estimate=None,
    resolved_risk: RiskParameterSet | None = None,
):
    """Fundamental justified-P/B valuation for financial institutions.

    P/B = (normalised ROE - g) / (cost of equity - g).  Industrial debt/WACC
    is intentionally not used because deposits and policy liabilities are part
    of a financial institution's operations rather than comparable financing.
    """
    if resolved_risk is None:
        resolved_risk = resolve_risk_parameters()
    balance_data = financial_data.get("balance", [])
    latest_equity, latest_basis = _extract_attributable_equity(balance_data, allow_total_fallback=False)
    annual_equity, equity_basis = _annual_attributable_equity(balance_data)
    annual_profit = dict(_annual_values(financial_data.get("income_history", []), ("PARENT_NETPROFIT",)))
    roe_years = sorted(year for year in annual_profit if year in annual_equity and year - 1 in annual_equity)
    if latest_equity is None or len(roe_years) < 3:
        return None
    if roe_years[-1] != max(annual_profit) or roe_years[-1] != max(annual_equity):
        return None
    consecutive_years = [roe_years[-1]]
    for year in reversed(roe_years[:-1]):
        if year != consecutive_years[0] - 1:
            break
        consecutive_years.insert(0, year)
    if len(consecutive_years) < 3:
        return None
    roe_years = consecutive_years[-5:]
    latest_roe_year = roe_years[-1]
    if annual_profit[latest_roe_year] <= 0:
        return None
    roes = []
    for year in roe_years:
        average_equity = (annual_equity[year - 1] + annual_equity[year]) / 2.0
        if average_equity > 0:
            roes.append(annual_profit[year] / average_equity)
    if len(roes) < 3 or any(not math.isfinite(roe) for roe in roes) or sum(roe > 0 for roe in roes) / len(roes) < 0.8:
        return None
    normalised_roe = statistics.median(roes)
    if normalised_roe <= 0 or roes[-1] <= 0:
        return None
    current_period_evidence = _current_period_evidence(financial_data)
    profit = current_period_evidence["profit"]
    profit_yoy = current_period_evidence["profit_yoy"]
    profit_basis = current_period_evidence["profit_yoy_basis"]
    if profit is None or profit <= 0:
        return None
    if profit_basis == "same_period_yoy":
        if profit_yoy is None or profit_yoy <= -0.30:
            return None
    elif profit_basis != "same_period_turnaround":
        return None

    bvps = latest_equity / total_shares
    if not _valid_positive_value(bvps):
        return None

    industry_financial_beta = _as_finite(INDUSTRY_FINANCIAL_LEVERED_BETA.get(industry_code))
    beta_blend = blend_company_market_beta(
        code=code,
        industry_levered_beta=industry_financial_beta,
        industry_beta_role="industry_financial_levered_beta",
        market_beta_estimate=market_beta_estimate,
        explicit_company_beta=beta,
    )
    if beta_blend is None:
        return None
    financial_beta = beta_blend.final_beta
    beta_source = beta_blend.beta_source
    cost_of_equity = compute_wacc(
        risk_free=resolved_risk.risk_free_rate,
        beta=financial_beta,
        erp=resolved_risk.equity_risk_premium,
    )
    if cost_of_equity is None:
        return None

    roe_scenarios = {
        "pessimistic": min(_quantile(roes, 0.25), normalised_roe * 0.80),
        "neutral": normalised_roe,
        # Optimism is deliberately limited to 10%, but must still represent a
        # genuinely better operating outcome than the neutral case.
        "optimistic": max(
            normalised_roe,
            min(_quantile(roes, 0.75), normalised_roe * 1.10),
        ),
    }
    recent_profit_years = sorted(annual_profit)[-4:]
    recent_profits = [annual_profit[year] for year in recent_profit_years]
    structural_profit_decline = (
        len(recent_profit_years) == 4
        and all(current == previous + 1 for previous, current in zip(recent_profit_years, recent_profit_years[1:]))
        and all(current < previous for previous, current in zip(recent_profits, recent_profits[1:]))
    )
    if structural_profit_decline:
        growth_targets = {"pessimistic": -0.02, "neutral": -0.01, "optimistic": 0.0}
        financial_growth_basis = "four_year_attributable_profit_decline"
    elif normalised_roe <= cost_of_equity:
        # When ROE is below the cost of equity, reinvestment destroys value.
        # In that regime a lower retention/growth rate is the better outcome;
        # preserving the usual 0%/1%/2% order would reverse scenario values.
        growth_targets = {
            "pessimistic": _as_finite(TERMINAL_GROWTH["optimistic"]) or 0.0,
            "neutral": _as_finite(TERMINAL_GROWTH["neutral"]) or 0.0,
            "optimistic": _as_finite(TERMINAL_GROWTH["pessimistic"]) or 0.0,
        }
        financial_growth_basis = "negative_roe_cost_spread_lower_retention_is_better"
    else:
        growth_targets = {scenario: _as_finite(TERMINAL_GROWTH[scenario]) or 0.0 for scenario in SCENARIOS}
        financial_growth_basis = "positive_roe_cost_spread"
    growth_scenarios = {
        scenario: min(
            max(-0.03, growth_targets[scenario]),
            roe_scenarios[scenario] * 0.50,
            cost_of_equity - 0.02,
        )
        for scenario in SCENARIOS
    }

    points = {}
    params = {}
    for scenario in SCENARIOS:
        scenario_roe = roe_scenarios[scenario]
        growth = growth_scenarios[scenario]
        high_discount_rate = cost_of_equity + BAND_WACC_DELTA
        low_discount_rate = cost_of_equity - BAND_WACC_DELTA
        if scenario_roe <= growth or low_discount_rate <= growth:
            return None
        pb_lower = (scenario_roe - growth) / (high_discount_rate - growth)
        pb_upper = (scenario_roe - growth) / (low_discount_rate - growth)
        if not (_valid_positive_value(pb_lower) and _valid_positive_value(pb_upper)):
            return None
        points[scenario] = {
            "upper": bvps * pb_upper,
            "lower": bvps * pb_lower,
        }
        params[scenario] = {
            "growth": growth,
            "wacc_base": cost_of_equity,
            "terminal_g": growth,
            "margin_retention": None,
            "normalised_roe": normalised_roe,
            "scenario_roe": scenario_roe,
            "cost_of_equity": cost_of_equity,
            "levered_beta": financial_beta,
            "pb_lower": pb_lower,
            "pb_upper": pb_upper,
            "formula": "(normalised_roe - g) / (cost_of_equity - g)",
            "roe_years": roe_years,
            "roe_basis": "average_begin_end_attributable_equity",
            "model_risk_data_as_of": resolved_risk.model_as_of,
            # Preserve the exact formula input for audit/recalculation.  UI
            # formatting may round it for display, but the model payload must
            # not make its own valuation endpoints irreproducible.
            "bvps": bvps,
            "equity_basis": equity_basis,
            "financial_growth_basis": financial_growth_basis,
            "structural_profit_decline": structural_profit_decline,
        }
    if not _ordered_financial_scenario_bands(points):
        return None

    mean1 = (points["pessimistic"]["upper"] + points["neutral"]["lower"]) / 2.0
    mean2 = (points["neutral"]["upper"] + points["optimistic"]["lower"]) / 2.0
    valuation_center = (points["neutral"]["lower"] + points["neutral"]["upper"]) / 2.0
    if current_price <= mean1:
        zone = "买入区"
    elif current_price >= mean2:
        zone = "卖出区"
    else:
        zone = "观察区"
    safety_score, safety_margin, bubble_warning = _safety_fields(
        current_price,
        zone,
        points["pessimistic"]["upper"],
        points["optimistic"]["upper"],
    )
    return {
        "code": code,
        "name": name,
        "current_price": current_price,
        "dcf_points": points,
        "zone": zone,
        "safety_score": safety_score,
        "safety_margin_pct": round(safety_margin, 2),
        "bubble_warning": bubble_warning,
        "mean1": mean1,
        "mean2": mean2,
        "valuation_center": valuation_center,
        "neutral_value_midpoint": valuation_center,
        "dcf_value_mean": mean1,
        "dcf_value_mean_legacy_alias_of": "buy_zone_upper",
        "buy_zone_upper": mean1,
        "sell_zone_lower": mean2,
        "params": params,
        "base_wacc": round(cost_of_equity, 4),
        "base_fcf": None,
        "base_revenue": None,
        "shares_outstanding": total_shares,
        "net_debt": None,
        "industry_code": industry_code,
        "_pb_valuation": True,
        "equity_basis": equity_basis if equity_basis != "missing" else latest_basis,
        "normalised_roe": normalised_roe,
        "financial_levered_beta": financial_beta,
        "levered_beta": financial_beta,
        "industry_levered_beta": industry_financial_beta,
        "industry_unlevered_beta": None,
        "beta_source": beta_source,
        "beta_evidence": beta_blend.to_dict(),
        "wacc_capital_structure": "financial_equity_cost_only",
        "wacc_components": {
            "equity_weight": 1.0,
            "debt_weight": 0.0,
            "cost_of_equity": cost_of_equity,
            "pre_tax_cost_of_debt": None,
            "tax_shield_rate": 0.0,
        },
        "tax_shield_source": "financial_operating_liabilities_excluded",
        "tax_shield_rate": 0.0,
        "roe_evidence_years": roe_years,
        "roe_basis": "average_begin_end_attributable_equity",
        "financial_growth_basis": financial_growth_basis,
        "structural_profit_decline": structural_profit_decline,
        "structural_profit_decline_years": recent_profit_years if structural_profit_decline else [],
        "model_risk_data_as_of": resolved_risk.model_as_of,
        "risk_parameters": resolved_risk.to_dict(),
        "current_period_evidence": current_period_evidence,
    }


def _annual_values(records, keys: tuple[str, ...]) -> list[tuple[int, float]]:
    if isinstance(records, dict):
        records = [records]
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in records or []:
        if not isinstance(row, dict):
            continue
        date = str(row.get("REPORT_DATE", ""))
        if len(date) < 4 or not date[:4].isdigit():
            continue
        value = None
        for key in keys:
            value = _as_finite(row.get(key))
            if value is not None:
                break
        if value is None:
            continue
        year = int(date[:4])
        candidate = (date.endswith("12-31"), date, value)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return sorted((year, item[2]) for year, item in by_year.items())


def _annual_fcff_points(cashflow_data) -> list[tuple[int, float]]:
    if isinstance(cashflow_data, dict):
        cashflow_data = [cashflow_data]
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in cashflow_data or []:
        if not isinstance(row, dict):
            continue
        report_date = str(row.get("REPORT_DATE", ""))
        if len(report_date) < 4 or not report_date[:4].isdigit():
            continue
        operating = _as_finite(row.get("NETCASH_OPERATE"))
        capex = _as_finite(row.get("CONSTRUCT_LONG_ASSET"))
        if operating is not None and capex is not None:
            year = int(report_date[:4])
            candidate = (report_date.endswith("12-31"), report_date, operating - abs(capex))
            if year not in by_year or candidate[:2] > by_year[year][:2]:
                by_year[year] = candidate
    return sorted((year, item[2]) for year, item in by_year.items())


def _annual_fcff_values(cashflow_data) -> list[float]:
    return [value for _, value in _annual_fcff_points(cashflow_data)]


def _consecutive_suffix(years: list[int], *, maximum: int | None = None) -> list[int]:
    if not years:
        return []
    suffix = [years[-1]]
    for year in reversed(years[:-1]):
        if year != suffix[0] - 1:
            break
        suffix.insert(0, year)
    return suffix[-maximum:] if maximum is not None else suffix


def _detect_quality(financial_data: dict, fcf_margin: float) -> bool:
    """Require multi-year profitability and cash-flow evidence for quality."""
    fcf_margin_f = _as_finite(fcf_margin)
    if not isinstance(financial_data, dict) or fcf_margin_f is None or fcf_margin_f < 0.12:
        return False
    if not _current_period_supports_quality(financial_data):
        return False
    annual_equity, _basis = _annual_attributable_equity(financial_data.get("balance", []))
    if len(annual_equity) < 2:
        return False

    income = financial_data.get("income_history", [])
    profits = dict(_annual_values(income, ("PARENT_NETPROFIT",)))
    revenues = dict(_annual_values(income, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")))
    common_years = sorted(set(profits).intersection(revenues))
    common_years = _consecutive_suffix(common_years, maximum=5)
    if len(common_years) < 4:
        return False
    margins = [profits[year] / revenues[year] for year in common_years if revenues[year] > 0]
    if len(margins) < 4 or any(not math.isfinite(value) for value in margins):
        return False
    if sum(value > 0 for value in margins) / len(margins) < 0.8:
        return False
    median_margin = statistics.median(margins)
    if median_margin < 0.15 or margins[-1] < median_margin * 0.70:
        return False
    mean_margin = statistics.fmean(margins)
    if mean_margin <= 0 or statistics.pstdev(margins) / mean_margin > 0.30:
        return False

    roe_years = _consecutive_suffix(
        [year for year in common_years if year in annual_equity and year - 1 in annual_equity],
        maximum=5,
    )
    if len(roe_years) < 3:
        return False
    roes = [profits[year] / ((annual_equity[year - 1] + annual_equity[year]) / 2.0) for year in roe_years]
    if any(not math.isfinite(value) for value in roes) or roes[-1] < 0.20 or statistics.median(roes) < 0.18:
        return False
    fcff_points = _annual_fcff_points(financial_data.get("cashflow", []))
    fcff_years = _consecutive_suffix([year for year, _ in fcff_points], maximum=5)
    fcff_map = dict(fcff_points)
    fcffs = [fcff_map[year] for year in fcff_years]
    # Retain zero and negative years.  One working-capital shock in a
    # multi-year quality history is allowed, but positive-only survivor bias is
    # not: at least two thirds of all observed FCFF years must be positive.
    if len(fcffs) < 3 or sum(value > 0 for value in fcffs) / len(fcffs) < (2.0 / 3.0):
        return False
    median_fcff = statistics.median(fcffs)
    return median_fcff > 0 and fcffs[-1] >= median_fcff * 0.40


def _detect_structural_decline(revenue_history, income_history) -> tuple[bool, Optional[str]]:
    """Identify multi-year company contraction without a sequential recovery."""
    revenues = _annual_values(revenue_history, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"))
    profits = dict(_annual_values(income_history, ("PARENT_NETPROFIT",)))
    revenue_map = dict(revenues)
    common_years = sorted(set(revenue_map).intersection(profits))
    if len(revenues) < 4:
        return False, None
    recent_revenue_points = revenues[-4:]
    recent_revenue_years = [year for year, _ in recent_revenue_points]
    if not all(current == previous + 1 for previous, current in zip(recent_revenue_years, recent_revenue_years[1:])):
        return False, None
    recent_revenue = [value for _, value in recent_revenue_points]
    revenue_decline = all(current < prior for prior, current in zip(recent_revenue, recent_revenue[1:]))
    if not revenue_decline:
        return False, None
    margin_decline = False
    if len(common_years) >= 4:
        recent_years = common_years[-4:]
        if recent_years != recent_revenue_years:
            recent_years = []
        margins = [profits[year] / revenue_map[year] for year in recent_years if revenue_map[year] > 0]
        margin_decline = len(margins) == 4 and all(current < prior for prior, current in zip(margins, margins[1:]))
    if margin_decline:
        return True, "revenue_and_margin_multi_year_decline"
    if revenue_decline:
        return True, "revenue_multi_year_decline"
    return False, None


def _derive_scenario_growth(revenue_history: list[dict], base_cagr: float) -> dict:
    """Derive ordered growth scenarios with recency and trend, not old maxima."""
    yearly = _annual_values(revenue_history, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"))
    annual_growth = []
    for (previous_year, previous), (current_year, current) in zip(yearly, yearly[1:]):
        elapsed = current_year - previous_year
        if previous > 0 and current > 0 and elapsed > 0:
            growth = (current / previous) ** (1.0 / elapsed) - 1.0
            if math.isfinite(growth):
                annual_growth.append(max(-0.50, min(0.50, growth)))

    # With no observable interval there is no evidence for either contraction
    # or recovery.  A zero base supplied by the caller is a missing-data
    # sentinel, not permission to manufacture a symmetric +/-5% range.
    if not annual_growth:
        return {scenario: 0.0 for scenario in SCENARIOS}

    # Two consecutive contractions of at least 20% are direct evidence of a
    # severely shrinking business.  The ordinary floors (-15%/-10%/-5%) would
    # otherwise manufacture an immediate recovery.  Preserve the observed
    # direction while allowing explicit, bounded mean reversion by scenario.
    if len(annual_growth) >= 2 and all(value <= -0.20 for value in annual_growth[-2:]):
        recent_contraction = (annual_growth[-2] + 2.0 * annual_growth[-1]) / 3.0
        rates = {
            "pessimistic": max(-0.50, min(-0.20, recent_contraction)),
            "neutral": max(-0.35, min(-0.15, recent_contraction * 0.75)),
            "optimistic": max(-0.20, min(-0.10, recent_contraction * 0.50)),
        }
        return _normalise_growth_rates(rates, rates, allow_severe_decline=True)

    base = _as_finite(base_cagr)
    if base is None:
        base = 0.0
    if len(annual_growth) >= 3:
        count = len(annual_growth)
        weights = list(range(1, count + 1))
        weighted_mean = sum(value * weight for value, weight in zip(annual_growth, weights)) / sum(weights)
        x_mean = (count - 1) / 2.0
        denominator = sum((index - x_mean) ** 2 for index in range(count))
        slope = (
            sum(
                (index - x_mean) * (value - statistics.fmean(annual_growth))
                for index, value in enumerate(annual_growth)
            )
            / denominator
            if denominator
            else 0.0
        )
        forward = weighted_mean + 0.5 * slope
        neutral = 0.85 * forward + 0.15 * base
        spread = max(0.03, min(0.12, statistics.pstdev(annual_growth)))
    else:
        weights = list(range(1, len(annual_growth) + 1))
        neutral = sum(value * weight for value, weight in zip(annual_growth, weights)) / sum(weights)
        spread = max(0.03, min(0.12, statistics.pstdev(annual_growth)))

    rates = {
        "pessimistic": max(-0.15, min(0.15, neutral - spread)),
        "neutral": max(-0.10, min(0.20, neutral)),
        "optimistic": max(-0.05, min(0.30, neutral + spread)),
    }
    return _normalise_growth_rates(rates, rates)


def _normalise_growth_rates(
    candidate: dict,
    fallback: dict,
    *,
    allow_severe_decline: bool = False,
) -> Optional[dict]:
    if not isinstance(candidate, dict):
        candidate = fallback
    values = []
    for scenario in SCENARIOS:
        value = _as_finite(candidate.get(scenario))
        if value is None:
            value = _as_finite(fallback.get(scenario))
        if value is None:
            return None
        values.append(value)
    # Sorting is an invariant repair after industry blending: scenario labels
    # must represent increasing economic outcomes.
    values.sort()
    bounds = (
        ((-0.50, 0.15), (-0.35, 0.20), (-0.20, 0.30))
        if allow_severe_decline
        else ((-0.15, 0.15), (-0.10, 0.20), (-0.05, 0.30))
    )
    bounded = [max(low, min(high, value)) for value, (low, high) in zip(values, bounds)]
    bounded[1] = max(bounded[0], bounded[1])
    bounded[2] = max(bounded[1], bounded[2])
    return dict(zip(SCENARIOS, bounded))


def _extract_yearly_revenues(revenue_history: list[dict]) -> list[float]:
    return [
        value for _, value in _annual_values(revenue_history, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")) if value > 0
    ]


def _extract_latest_revenue(revenue_history: list[dict]) -> Optional[float]:
    yearly = _annual_values(revenue_history, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"))
    if not yearly:
        return None
    latest = yearly[-1][1]
    return latest if latest > 0 else None
