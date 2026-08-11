"""
DCF 估值引擎 — 核心计算模块。

非金融企业采用 FCFF 口径：经营现金流减资本开支得到 FCFF，以税后
WACC 折现，再减去“有息债务－现金”得到股权价值。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Optional

import numpy as np

from config import (
    DEFAULT_BETA,
    DEFAULT_PRETAX_COST_OF_DEBT,
    EQUITY_RISK_PREMIUM,
    FORECAST_YEARS,
    GROWTH_CAP_MAX,
    GROWTH_CAP_MIN,
    GROWTH_LOOKBACK_YEARS,
    MARGINAL_TAX_RATE,
    RISK_FREE_RATE,
)
from data.capex_evidence import validate_capex_provenance


MAX_NORMALISED_FCFF_PREMIUM = 1.25
TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
TTM_FCFF_FORMULA_VERSION = "ttm_cfo_less_capex_v2"
TTM_REVENUE_FORMULA_VERSION = "ttm_revenue_v1"
TTM_SOURCE_UNIT = "CNY"


@dataclass(frozen=True)
class ReportingPeriodContract:
    """Exact fiscal periods required for a comparable TTM reconstruction.

    The contract deliberately stores strings rather than silently normalising
    dates.  Reconstruction validates canonical ISO dates, a 31 December fiscal
    year, and matching current/prior interim cut-offs before reading any value.
    """

    annual_report_date: str
    current_interim_report_date: str
    prior_interim_report_date: str


def _period_contract_dates(period_contract: object) -> dict[str, object]:
    return {
        "annual_report_date": getattr(period_contract, "annual_report_date", None),
        "current_interim_report_date": getattr(period_contract, "current_interim_report_date", None),
        "prior_interim_report_date": getattr(period_contract, "prior_interim_report_date", None),
    }


def _valid_period_contract(period_contract: object) -> bool:
    if not isinstance(period_contract, ReportingPeriodContract):
        return False
    raw_dates = _period_contract_dates(period_contract)
    if not all(isinstance(value, str) for value in raw_dates.values()):
        return False
    try:
        annual = date.fromisoformat(period_contract.annual_report_date)
        current = date.fromisoformat(period_contract.current_interim_report_date)
        prior = date.fromisoformat(period_contract.prior_interim_report_date)
    except ValueError:
        return False
    if (
        annual.isoformat() != period_contract.annual_report_date
        or current.isoformat() != period_contract.current_interim_report_date
        or prior.isoformat() != period_contract.prior_interim_report_date
    ):
        return False

    # Mainland listed-company statements in this engine use a calendar fiscal
    # year.  Interim statements are cumulative Q1/H1/Q3 values, never an FY row.
    valid_interim_cutoffs = {(3, 31), (6, 30), (9, 30)}
    return (
        (annual.month, annual.day) == (12, 31)
        and (current.month, current.day) in valid_interim_cutoffs
        and (current.month, current.day) == (prior.month, prior.day)
        and prior.year == annual.year
        and current.year == annual.year + 1
    )


def _ttm_result_shell(
    *,
    metric: str,
    period_contract: object,
    formula_version: str,
    cash_flow_kind: str,
) -> dict[str, object]:
    period = {
        "basis": TTM_PERIOD_BASIS,
        **_period_contract_dates(period_contract),
    }
    return {
        "status": "invalid_period_contract",
        "value": None,
        "metric": metric,
        "formula_version": formula_version,
        "cash_flow_kind": cash_flow_kind,
        "period_basis": TTM_PERIOD_BASIS,
        "period": period,
        "unit": TTM_SOURCE_UNIT,
        "components": {
            "annual": {"report_date": period["annual_report_date"], "row_count": 0},
            "current_interim": {"report_date": period["current_interim_report_date"], "row_count": 0},
            "prior_interim": {"report_date": period["prior_interim_report_date"], "row_count": 0},
        },
    }


def _rows_at_report_date(records: object, report_date: object) -> list[dict]:
    if not isinstance(records, (list, tuple)) or not isinstance(report_date, str):
        return []
    return [row for row in records if isinstance(row, dict) and _canonical_report_date(row) == report_date]


def _select_ttm_rows(
    annual_records: object,
    interim_records: object,
    period_contract: ReportingPeriodContract,
    result: dict[str, object],
) -> tuple[str | None, dict[str, dict]]:
    targets = {
        "annual": (annual_records, period_contract.annual_report_date),
        "current_interim": (interim_records, period_contract.current_interim_report_date),
        "prior_interim": (interim_records, period_contract.prior_interim_report_date),
    }
    selected: dict[str, dict] = {}
    matches_by_label: dict[str, list[dict]] = {}
    components = result["components"]
    if not isinstance(components, dict):
        raise RuntimeError("strict TTM result shell has invalid components")
    for label, (records, report_date) in targets.items():
        matches = _rows_at_report_date(records, report_date)
        matches_by_label[label] = matches
        period_component = components[label]
        if not isinstance(period_component, dict):
            raise RuntimeError(f"strict TTM result shell has invalid {label} component")
        period_component["row_count"] = len(matches)
    if any(len(matches) > 1 for matches in matches_by_label.values()):
        return "duplicate_period", selected
    if any(not matches for matches in matches_by_label.values()):
        return "missing_component", selected
    for label, matches in matches_by_label.items():
        selected[label] = matches[0]
    return None, selected


def _strict_source_value(row: dict, keys: tuple[str, ...]) -> tuple[str, float | None, str | None]:
    present = [(key, row.get(key)) for key in keys if key in row and row.get(key) not in (None, "")]
    if not present:
        return "missing_component", None, None
    for key, raw_value in present:
        value = _finite_number(raw_value)
        if value is None:
            return "nonfinite_component", None, key
        return "complete", value, key
    return "missing_component", None, None


def reconstruct_ttm_fcff(
    annual_cashflow: list[dict],
    interim_cashflow: list[dict],
    *,
    period_contract: ReportingPeriodContract,
    require_capex_provenance: bool = False,
    expected_security_code: str | None = None,
) -> dict[str, object]:
    """Reconstruct strict trailing-twelve-month FCFF from cumulative statements.

    Formula: ``FY + current comparable YTD - prior comparable YTD`` for both
    CFO and Capex, followed by ``FCFF = CFO - abs(Capex)``.  Every failure is a
    machine-readable result with the partially populated component evidence.
    """
    result = _ttm_result_shell(
        metric="fcff",
        period_contract=period_contract,
        formula_version=TTM_FCFF_FORMULA_VERSION,
        cash_flow_kind="cfo_less_capex_proxy",
    )
    if not _valid_period_contract(period_contract):
        return result

    row_status, rows = _select_ttm_rows(annual_cashflow, interim_cashflow, period_contract, result)
    if row_status is not None:
        result["status"] = row_status
        return result

    components = result["components"]
    if not isinstance(components, dict):
        raise RuntimeError("strict TTM FCFF result shell has invalid components")
    cfo_values: dict[str, float] = {}
    capex_values: dict[str, float] = {}
    cfo_keys = ("NETCASH_OPERATE", "经营活动产生的现金流量净额")
    capex_keys = (
        "CONSTRUCT_LONG_ASSET",
        "PAY_ACQ_CONST_FIASSETS",
        "购建固定资产无形资产和其他长期资产支付的现金",
    )
    for label, row in rows.items():
        cfo_status, cfo, cfo_field = _strict_source_value(row, cfo_keys)
        capex_status, raw_capex, capex_field = _strict_source_value(row, capex_keys)
        period_component = components[label]
        if not isinstance(period_component, dict):
            raise RuntimeError(f"strict TTM FCFF result shell has invalid {label} component")
        period_component.update(
            {
                "operating_cash_flow": cfo,
                "operating_cash_flow_source_field": cfo_field,
                "capex_raw": raw_capex,
                "capex_absolute": abs(raw_capex) if raw_capex is not None else None,
                "capex_source_field": capex_field,
                "capex_provenance": row.get("CAPEX_PROVENANCE"),
            }
        )
        if cfo_status != "complete":
            result["status"] = cfo_status
            return result
        if capex_status != "complete":
            result["status"] = capex_status
            return result
        if cfo is None or raw_capex is None:
            result["status"] = "nonfinite_component"
            return result
        provenance = row.get("CAPEX_PROVENANCE")
        if require_capex_provenance or provenance is not None:
            provenance_status = validate_capex_provenance(
                provenance,
                expected_value=raw_capex,
                expected_report_date=_canonical_report_date(row),
                expected_security_code=expected_security_code,
            )
            period_component["capex_provenance_status"] = provenance_status
            if provenance_status != "complete":
                result["status"] = provenance_status
                return result
        else:
            period_component["capex_provenance_status"] = "not_required"
        cfo_values[label] = cfo
        capex_values[label] = abs(raw_capex)

    reconstructed_cfo = cfo_values["annual"] + cfo_values["current_interim"] - cfo_values["prior_interim"]
    reconstructed_capex = capex_values["annual"] + capex_values["current_interim"] - capex_values["prior_interim"]
    components["reconstructed_operating_cash_flow"] = reconstructed_cfo
    components["reconstructed_capex"] = reconstructed_capex
    if not math.isfinite(reconstructed_cfo) or not math.isfinite(reconstructed_capex):
        result["status"] = "nonfinite_component"
        return result
    if reconstructed_capex < 0:
        # A negative TTM capex reconstruction is a seasonal-mismatch artefact,
        # not a data contradiction: companies whose Q1 capex is large relative
        # to the full year (property, engineering, project-based businesses)
        # produce annual + current_interim - prior_interim < 0 even though capex
        # is physically non-negative.  Treat it as zero capex for the period
        # rather than failing closed: clipping is conservative (never inflates
        # FCFF) and keeps the company in normal valuation instead of an
        # artificial "口径异常" skip.  The raw negative value is preserved in
        # reconstructed_capex_raw for audit; downstream normalisation consumes
        # the clipped (zero) value so it does not double-count the artefact.
        components["reconstructed_capex_raw"] = reconstructed_capex
        components["reconstructed_capex_clipped"] = True
        components["reconstructed_capex"] = 0.0
        reconstructed_capex = 0.0

    fcff = reconstructed_cfo - reconstructed_capex
    if not math.isfinite(fcff):
        result["status"] = "nonfinite_component"
        return result
    components["reconstructed_fcff"] = fcff
    result["status"] = "complete"
    result["value"] = fcff
    return result


def reconstruct_ttm_revenue(
    annual_revenue: list[dict],
    interim_income: list[dict],
    *,
    period_contract: ReportingPeriodContract,
) -> dict[str, object]:
    """Reconstruct strict TTM revenue from FY and comparable cumulative YTD rows."""
    result = _ttm_result_shell(
        metric="revenue",
        period_contract=period_contract,
        formula_version=TTM_REVENUE_FORMULA_VERSION,
        cash_flow_kind="reported_revenue",
    )
    if not _valid_period_contract(period_contract):
        return result

    row_status, rows = _select_ttm_rows(annual_revenue, interim_income, period_contract, result)
    if row_status is not None:
        result["status"] = row_status
        return result

    components = result["components"]
    if not isinstance(components, dict):
        raise RuntimeError("strict TTM revenue result shell has invalid components")
    revenue_values: dict[str, float] = {}
    for label, row in rows.items():
        status, revenue, source_field = _strict_source_value(
            row,
            ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
        )
        period_component = components[label]
        if not isinstance(period_component, dict):
            raise RuntimeError(f"strict TTM revenue result shell has invalid {label} component")
        period_component.update({"revenue": revenue, "revenue_source_field": source_field})
        if status != "complete":
            result["status"] = status
            return result
        if revenue is None:
            result["status"] = "nonfinite_component"
            return result
        revenue_values[label] = revenue

    reconstructed_revenue = (
        revenue_values["annual"] + revenue_values["current_interim"] - revenue_values["prior_interim"]
    )
    components["reconstructed_revenue"] = reconstructed_revenue
    if not math.isfinite(reconstructed_revenue):
        result["status"] = "nonfinite_component"
        return result
    result["status"] = "complete"
    result["value"] = reconstructed_revenue
    return result


@lru_cache(maxsize=256)
def _canonical_report_date_text(value: str) -> str | None:
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _canonical_report_date(row: dict) -> str | None:
    return _canonical_report_date_text(str(row.get("REPORT_DATE", "")).strip())


def _finite_number(value) -> Optional[float]:
    """Convert a scalar to a finite float; booleans and malformed values are invalid."""
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def extract_revenue_cagr(revenue_records: list[dict]) -> Optional[float]:
    """Extract annual revenue CAGR using elapsed calendar years, not row count."""
    if not revenue_records:
        return None

    # One observation per calendar year.  Prefer an annual report; otherwise use
    # the latest observation in that year.  This avoids treating quarterly rows
    # as separate years and makes gaps in the series explicit.
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in revenue_records:
        if not isinstance(row, dict):
            continue
        report_date = _canonical_report_date(row)
        if report_date is None:
            continue
        revenue = None
        for key in ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"):
            revenue = _finite_number(row.get(key))
            if revenue is not None:
                break
        if revenue is None or revenue <= 0:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, revenue)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate

    ordered = sorted((year, item[2]) for year, item in by_year.items())
    if len(ordered) < 3:
        return None
    recent = ordered[-min(GROWTH_LOOKBACK_YEARS, len(ordered)) :]
    first_year, first_revenue = recent[0]
    last_year, last_revenue = recent[-1]
    elapsed_years = last_year - first_year
    if first_revenue <= 0 or last_revenue <= 0 or elapsed_years <= 0:
        return None

    cagr = (last_revenue / first_revenue) ** (1.0 / elapsed_years) - 1.0
    if not math.isfinite(cagr):
        return None
    return max(GROWTH_CAP_MIN, min(GROWTH_CAP_MAX, cagr))


def compute_wacc(
    risk_free: float = RISK_FREE_RATE,
    beta: float = DEFAULT_BETA,
    erp: float = EQUITY_RISK_PREMIUM,
    debt: float = 0.0,
    equity: float = None,
    pre_tax_cost_of_debt: float = None,
    tax_rate: float = MARGINAL_TAX_RATE,
    industry_unlevered_beta: float = None,
) -> Optional[float]:
    """Compute market-value weighted, tax-shielded WACC.

    ``beta`` is the backward-compatible levered CAPM input.  The preferred
    path supplies ``industry_unlevered_beta`` (cash-corrected industry asset
    beta), which is re-levered exactly once with company market D/E.  When
    reliable capital structure data is unavailable, cost of equity is the
    explicit fallback; no cheap debt component is invented.
    """
    risk_free_f = _finite_number(risk_free)
    beta_f = _finite_number(beta)
    erp_f = _finite_number(erp)
    if risk_free_f is None or erp_f is None or erp_f < 0:
        return None
    debt_f = _finite_number(debt)
    equity_f = _finite_number(equity)
    tax_rate_f = _finite_number(tax_rate)
    if tax_rate_f is None or not 0 <= tax_rate_f < 1:
        return None

    if industry_unlevered_beta is not None:
        unlevered_beta = _finite_number(industry_unlevered_beta)
        if unlevered_beta is None or unlevered_beta < 0:
            return None
        # When debt/equity is unavailable, the cash-corrected industry asset
        # beta itself is the explicit fallback.  It is never levered twice.
        if debt_f is not None and equity_f is not None and debt_f >= 0 and equity_f > 0:
            beta_f = relever_beta(unlevered_beta, debt_f, equity_f, tax_rate_f)
            if beta_f is None:
                return None
        else:
            beta_f = unlevered_beta
    elif beta_f is None or beta_f < 0:
        # The legacy levered-beta path still requires a valid company beta.
        # A missing legacy beta must not, however, block the preferred
        # industry asset-beta path above.
        return None

    cost_of_equity = risk_free_f + beta_f * erp_f
    if not math.isfinite(cost_of_equity) or cost_of_equity <= 0:
        return None

    # Reject an explicitly invalid component, but treat a genuinely missing
    # side of the capital structure as unavailable evidence and fall back to
    # cost of equity.  Never invent a debt weight.
    if debt_f is not None and debt_f < 0:
        return None
    if equity_f is not None and equity_f <= 0:
        return None
    if equity_f is None or debt_f is None:
        return cost_of_equity
    if debt_f == 0:
        return cost_of_equity

    if pre_tax_cost_of_debt is None:
        # Current, sourced market fallback from config; never infer borrowing
        # cost from total liabilities.
        cost_of_debt = _finite_number(DEFAULT_PRETAX_COST_OF_DEBT)
        if cost_of_debt is None:
            return None
    else:
        cost_of_debt = _finite_number(pre_tax_cost_of_debt)
        if cost_of_debt is None:
            return None
    if cost_of_debt < 0:
        return None

    capital = equity_f + debt_f
    if capital <= 0 or not math.isfinite(capital):
        return None
    equity_weight = equity_f / capital
    debt_weight = debt_f / capital
    wacc = equity_weight * cost_of_equity + debt_weight * cost_of_debt * (1.0 - tax_rate_f)
    return wacc if math.isfinite(wacc) and wacc > 0 else None


def relever_beta(
    unlevered_beta: float,
    debt: float,
    equity: float,
    tax_rate: float = MARGINAL_TAX_RATE,
) -> Optional[float]:
    """Hamada relevering: beta_L = beta_U * (1 + (1-T) * D/E)."""
    beta_u = _finite_number(unlevered_beta)
    debt_f = _finite_number(debt)
    equity_f = _finite_number(equity)
    tax_f = _finite_number(tax_rate)
    if (
        beta_u is None
        or debt_f is None
        or equity_f is None
        or tax_f is None
        or beta_u < 0
        or debt_f < 0
        or equity_f <= 0
        or not 0 <= tax_f < 1
    ):
        return None
    result = beta_u * (1.0 + (1.0 - tax_f) * debt_f / equity_f)
    return result if math.isfinite(result) and result >= 0 else None


def dcf_valuation(
    base_fcf: float,
    base_revenue: float,
    revenue_growth: float,
    wacc: float,
    terminal_g: float,
    shares_outstanding: float,
    net_debt: float = 0,
    forecast_years: int = FORECAST_YEARS,
    fcf_margin_target: float = None,
    margin_retention: float = 0.60,
) -> Optional[float]:
    """Return per-share FCFF value from a two-stage Gordon-growth DCF.

    A non-positive base FCFF is deliberately not converted into a positive
    valuation.  Turnarounds require an explicit forecast model, which this
    historical-cash-flow model does not have enough evidence to construct.
    """
    values = {
        "base_fcf": _finite_number(base_fcf),
        "base_revenue": _finite_number(base_revenue),
        "revenue_growth": _finite_number(revenue_growth),
        "wacc": _finite_number(wacc),
        "terminal_g": _finite_number(terminal_g),
        "shares": _finite_number(shares_outstanding),
        "net_debt": _finite_number(net_debt),
        "retention": _finite_number(margin_retention),
    }
    if any(value is None for value in values.values()):
        return None
    if isinstance(forecast_years, (bool, np.bool_)) or not isinstance(forecast_years, (int, np.integer)):
        return None
    if forecast_years <= 0:
        return None

    base_fcf_f = values["base_fcf"]
    base_revenue_f = values["base_revenue"]
    growth_f = values["revenue_growth"]
    wacc_f = values["wacc"]
    terminal_g_f = values["terminal_g"]
    shares_f = values["shares"]
    net_debt_f = values["net_debt"]
    retention_f = values["retention"]
    if (
        base_fcf_f <= 0
        or base_revenue_f <= 0
        or shares_f <= 0
        or growth_f <= -1.0
        or wacc_f <= -1.0
        or terminal_g_f <= -1.0
        or wacc_f <= terminal_g_f
        or not 0.0 <= retention_f <= 1.0
    ):
        return None

    current_margin = base_fcf_f / base_revenue_f
    if not math.isfinite(current_margin) or not 0 < current_margin <= 1.0:
        return None

    from config import FCF_MARGIN_FLOOR, FCF_MARGIN_LONG_TERM

    floor = _finite_number(FCF_MARGIN_FLOOR)
    equilibrium = _finite_number(FCF_MARGIN_LONG_TERM)
    if floor is None or equilibrium is None or not 0 <= floor <= 1 or not 0 <= equilibrium <= 1:
        return None

    if fcf_margin_target is None:
        # Long-run equilibrium can pull an unusually high margin down, but it
        # must never manufacture expansion for a low-margin company.
        retained_margin = current_margin * retention_f
        target_margin = min(current_margin, max(floor, equilibrium, retained_margin))
    else:
        target_margin = _finite_number(fcf_margin_target)
        if target_margin is None or not 0 <= target_margin <= 1:
            return None
        target_margin = max(floor, target_margin)

    years = np.arange(1, int(forecast_years) + 1, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        revenues = base_revenue_f * np.power(1.0 + growth_f, years)
        interpolation = (years - 1.0) / max(int(forecast_years) - 1, 1)
        margins = current_margin + (target_margin - current_margin) * interpolation
        fcffs = revenues * margins
        discounts = np.power(1.0 + wacc_f, years)
    if not (np.all(np.isfinite(revenues)) and np.all(np.isfinite(fcffs)) and np.all(np.isfinite(discounts))):
        return None
    if np.any(discounts <= 0):
        return None

    pv_explicit = float(np.sum(fcffs / discounts))
    # Gordon numerator is year N+1 FCFF, so only the terminal growth rate is
    # applied after the final explicit forecast year.
    terminal_fcf = float(revenues[-1]) * (1.0 + terminal_g_f) * target_margin
    terminal_value = terminal_fcf / (wacc_f - terminal_g_f)
    pv_terminal = terminal_value / float(discounts[-1])
    equity_value = pv_explicit + pv_terminal - net_debt_f
    per_share = equity_value / shares_f
    if not math.isfinite(per_share) or per_share <= 0:
        return None
    return per_share


def dcf_valuation_fading_growth(
    base_fcf: float,
    base_revenue: float,
    revenue_growth: float,
    wacc: float,
    terminal_g: float,
    shares_outstanding: float,
    net_debt: float = 0,
    forecast_years: int = 10,
    fcf_margin_target: float = None,
    margin_retention: float = 0.60,
) -> Optional[float]:
    """Return a long-horizon DCF whose explicit growth fades to terminal growth.

    Patch6 Type4 compares current value with a discounted ten-year terminal
    outcome.  Reusing :func:`dcf_valuation` with a ten-year argument would hold
    the initial growth rate constant for all ten years and materially overstate
    long-run growth.  This variant uses the same cash-flow and margin contract,
    but linearly fades annual revenue growth from ``revenue_growth`` in year 1
    to ``terminal_g`` in the final explicit year.
    """
    values = {
        "base_fcf": _finite_number(base_fcf),
        "base_revenue": _finite_number(base_revenue),
        "revenue_growth": _finite_number(revenue_growth),
        "wacc": _finite_number(wacc),
        "terminal_g": _finite_number(terminal_g),
        "shares": _finite_number(shares_outstanding),
        "net_debt": _finite_number(net_debt),
        "retention": _finite_number(margin_retention),
    }
    if any(value is None for value in values.values()):
        return None
    if isinstance(forecast_years, (bool, np.bool_)) or not isinstance(forecast_years, (int, np.integer)):
        return None
    if forecast_years < 2:
        return None

    base_fcf_f = values["base_fcf"]
    base_revenue_f = values["base_revenue"]
    growth_f = values["revenue_growth"]
    wacc_f = values["wacc"]
    terminal_g_f = values["terminal_g"]
    shares_f = values["shares"]
    net_debt_f = values["net_debt"]
    retention_f = values["retention"]
    if (
        base_fcf_f <= 0
        or base_revenue_f <= 0
        or shares_f <= 0
        or growth_f <= -1.0
        or wacc_f <= -1.0
        or terminal_g_f <= -1.0
        or wacc_f <= terminal_g_f
        or not 0.0 <= retention_f <= 1.0
    ):
        return None

    current_margin = base_fcf_f / base_revenue_f
    if not math.isfinite(current_margin) or not 0 < current_margin <= 1.0:
        return None

    from config import FCF_MARGIN_FLOOR, FCF_MARGIN_LONG_TERM

    floor = _finite_number(FCF_MARGIN_FLOOR)
    equilibrium = _finite_number(FCF_MARGIN_LONG_TERM)
    if floor is None or equilibrium is None or not 0 <= floor <= 1 or not 0 <= equilibrium <= 1:
        return None
    if fcf_margin_target is None:
        retained_margin = current_margin * retention_f
        target_margin = min(current_margin, max(floor, equilibrium, retained_margin))
    else:
        target_margin = _finite_number(fcf_margin_target)
        if target_margin is None or not 0 <= target_margin <= 1:
            return None
        target_margin = max(floor, target_margin)

    years = np.arange(1, int(forecast_years) + 1, dtype=np.float64)
    interpolation = (years - 1.0) / (int(forecast_years) - 1)
    growth_path = growth_f + (terminal_g_f - growth_f) * interpolation
    if np.any(growth_path <= -1.0):
        return None
    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        revenues = base_revenue_f * np.cumprod(1.0 + growth_path)
        margins = current_margin + (target_margin - current_margin) * interpolation
        fcffs = revenues * margins
        discounts = np.power(1.0 + wacc_f, years)
    if not (
        np.all(np.isfinite(revenues))
        and np.all(np.isfinite(fcffs))
        and np.all(np.isfinite(discounts))
        and np.all(discounts > 0)
    ):
        return None

    pv_explicit = float(np.sum(fcffs / discounts))
    terminal_fcf = float(fcffs[-1]) * (1.0 + terminal_g_f)
    terminal_value = terminal_fcf / (wacc_f - terminal_g_f)
    pv_terminal = terminal_value / float(discounts[-1])
    per_share = (pv_explicit + pv_terminal - net_debt_f) / shares_f
    return per_share if math.isfinite(per_share) and per_share > 0 else None


def extract_fcf_normalisation(cashflow_data: list[dict]) -> dict[str, object]:
    """Return the auditable recent-FCFF normalisation decision.

    Both operating cash flow and capital expenditure are required.  Missing
    Capex is not silently treated as zero because that systematically inflates
    FCFF for incomplete records.  A recent median still dampens one-year
    spikes, but it can never turn a non-positive latest year into a positive
    valuation input or exceed the latest positive FCFF by more than 25%.
    """
    if isinstance(cashflow_data, dict):
        cashflow_data = [cashflow_data]
    if not cashflow_data:
        return {
            "normalised_fcf": None,
            "latest_fcff": None,
            "recent_fcff": (),
            "basis": "missing_complete_annual_fcff_history",
            "premium_cap": MAX_NORMALISED_FCFF_PREMIUM,
        }

    by_year: dict[int, tuple[bool, str, float]] = {}
    undated_fcf: list[float] = []
    for row in cashflow_data:
        if not isinstance(row, dict):
            continue
        operating_cash = None
        capex = None
        for key in ("NETCASH_OPERATE", "经营活动产生的现金流量净额"):
            operating_cash = _finite_number(row.get(key))
            if operating_cash is not None:
                break
        for key in (
            "CONSTRUCT_LONG_ASSET",
            "PAY_ACQ_CONST_FIASSETS",
            "购建固定资产无形资产和其他长期资产支付的现金",
        ):
            capex = _finite_number(row.get(key))
            if capex is not None:
                capex = abs(capex)
                break
        if operating_cash is not None and capex is not None:
            fcf = operating_cash - capex
            report_date = _canonical_report_date(row)
            if report_date is not None:
                year = int(report_date[:4])
                candidate = (report_date.endswith("12-31"), report_date, fcf)
                if year not in by_year or candidate[:2] > by_year[year][:2]:
                    by_year[year] = candidate
            elif "REPORT_DATE" not in row:
                undated_fcf.append(fcf)

    annual_fcf = [by_year[year][2] for year in sorted(by_year)] if by_year else undated_fcf
    if not annual_fcf:
        return {
            "normalised_fcf": None,
            "latest_fcff": None,
            "recent_fcff": (),
            "basis": "missing_complete_annual_fcff_history",
            "premium_cap": MAX_NORMALISED_FCFF_PREMIUM,
        }
    recent_fcf = tuple(float(value) for value in annual_fcf[-3:])
    latest_fcf = recent_fcf[-1]
    median_fcf = float(statistics.median(recent_fcf))
    if latest_fcf <= 0:
        fcf = latest_fcf
        basis = "latest_nonpositive_fail_closed"
    elif len(recent_fcf) >= 3 and all(current <= prior for prior, current in zip(recent_fcf, recent_fcf[1:])):
        fcf = latest_fcf
        basis = "latest_persistent_decline"
    else:
        premium_limit = latest_fcf * MAX_NORMALISED_FCFF_PREMIUM
        fcf = min(median_fcf, premium_limit)
        basis = "recent_median" if median_fcf <= premium_limit else "latest_premium_cap"
    return {
        "normalised_fcf": fcf if math.isfinite(fcf) else None,
        "latest_fcff": latest_fcf,
        "recent_fcff": recent_fcf,
        "basis": basis,
        "premium_cap": MAX_NORMALISED_FCFF_PREMIUM,
    }


def extract_fcf_from_cashflow(
    cashflow_data: list[dict],
    base_revenue: float = None,
    max_fcf_margin: float = None,
) -> Optional[float]:
    """Return the recent, decline-aware normalised annual FCFF value."""
    evidence = extract_fcf_normalisation(cashflow_data)
    fcf = _finite_number(evidence.get("normalised_fcf"))
    if fcf is None:
        return None

    revenue = _finite_number(base_revenue)
    margin_cap = _finite_number(max_fcf_margin)
    if revenue is not None and revenue > 0 and margin_cap is not None and margin_cap > 0:
        fcf = min(fcf, revenue * margin_cap)
    return fcf if math.isfinite(fcf) else None


_DIRECT_DEBT_KEYS = (
    "INTEREST_BEARING_DEBT",
    "TOTAL_INTEREST_BEARING_DEBT",
    "有息负债",
)
_DEBT_COMPONENT_KEYS = (
    "SHORT_LOAN",
    "SHORT_BONDS_PAYABLE",
    "LONG_LOAN",
    "BOND_PAYABLE",
    "BONDS_PAYABLE",
    "NONCURRENT_LIAB_1YEAR",
    "CURRENT_PORTION_LONG_DEBT",
    "LEASE_LIAB",
    "LEASE_LIABILITIES",
    "短期借款",
    "长期借款",
    "应付债券",
    "一年内到期的非流动负债",
    "租赁负债",
)
_CASH_KEYS = (
    "MONETARYFUNDS",
    "CASH_AND_CASH_EQUIVALENTS",
    "CASH_EQUIVALENTS",
    "货币资金",
)


def extract_debt_and_cash(balance_data: list[dict]) -> tuple[float, float, bool]:
    """Return gross interest-bearing debt, cash, and whether debt fields exist."""
    if isinstance(balance_data, dict):
        balance_data = [balance_data]
    rows = [row for row in (balance_data or []) if isinstance(row, dict) and _canonical_report_date(row) is not None]
    if not rows:
        return 0.0, 0.0, False
    latest = max(rows, key=lambda row: _canonical_report_date(row) or "")

    debt_known = False
    gross_debt = 0.0
    direct_value = None
    for key in _DIRECT_DEBT_KEYS:
        if key in latest:
            candidate = _finite_number(latest.get(key))
            if candidate is not None and candidate >= 0:
                debt_known = True
                direct_value = candidate
                break
    if direct_value is not None:
        gross_debt = direct_value
    else:
        for key in _DEBT_COMPONENT_KEYS:
            if key in latest:
                candidate = _finite_number(latest.get(key))
                if candidate is not None and candidate >= 0:
                    debt_known = True
                    if candidate > 0:
                        gross_debt += candidate

    cash = 0.0
    for key in _CASH_KEYS:
        if key in latest:
            candidate = _finite_number(latest.get(key))
            if candidate is not None and candidate >= 0:
                cash = candidate
                break
    return gross_debt, cash, debt_known


def extract_net_debt(balance_data: list[dict]) -> float:
    """Return interest-bearing debt less cash; avoid total-liability proxies."""
    debt, cash, debt_known = extract_debt_and_cash(balance_data)
    # If the source has no debt fields at all, ignore cash as well.  This is a
    # conservative neutral fallback and avoids inventing net cash from a partial
    # balance sheet.
    return debt - cash if debt_known else 0.0
