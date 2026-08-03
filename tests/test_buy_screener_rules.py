import copy
import inspect
import math
import random
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

from data.capex_evidence import resolve_capex_evidence
from data.datacenter import RPT_MAIN_FINANCIAL_INDICATORS
from data.market_coldness import (
    MarketColdnessCoverage,
    MarketColdnessRecord,
    MarketColdnessSnapshot,
    MetricCoverage,
)
from data.quality_history import replay_valuation_distribution
from engine import buy_screener as bs
from engine import scenarios
from engine.market_coldness import build_market_coldness_evidence
from engine.quantitative_evidence import MIN_SECTOR_COMPANIES, derive_company_evidence
from engine.type7_patch6 import assess_patch6_type7


def score_evidence(key: str, code: str = "000001", as_of: str = "2026-07-15") -> dict[str, str]:
    normalized_code = str(code).zfill(6) if str(code).isdigit() else str(code)
    result = {
        "source": "unit-test-fixture",
        "evidence_id": f"fixture-{key}:{normalized_code}",
        "as_of": as_of,
    }
    return result


TTM_CONTRACT = bs.ReportingPeriodContract(
    annual_report_date="2025-12-31",
    current_interim_report_date="2026-03-31",
    prior_interim_report_date="2025-03-31",
)


def production_coldness_fields(
    *,
    code: str = "000001",
    as_of: str = "2026-07-15",
    target_score: float = 8.0,
) -> dict[str, object]:
    """Build the same replayable evidence envelope used by production."""

    if target_score <= 3.0:
        values = (60.0, 80.0, 30.0, 5.0)
    elif target_score < 6.0:
        values = (0.0, 0.0, 3.0, 1.1)
    elif target_score < 7.5:
        values = (-10.0, -10.0, 1.5, 0.9)
    else:
        values = (-35.0, -45.0, 0.3, 0.4)
    session = date.fromisoformat(as_of)
    retrieved = datetime(
        session.year,
        session.month,
        session.day,
        8,
        20,
        tzinfo=timezone.utc,
    )
    retrieved_text = retrieved.isoformat()
    exchange = "SH" if code.startswith("6") else "SZ"
    record = MarketColdnessRecord(
        code=code,
        exchange=exchange,
        eastmoney_market_id=1 if exchange == "SH" else 0,
        name=f"样本{code}",
        change_60d_pct=values[0],
        change_ytd_pct=values[1],
        turnover_rate_pct=values[2],
        volume_ratio=values[3],
        listing_date="2001-08-27",
        source_updated_at=retrieved_text,
        source="Eastmoney push2 clist",
        source_url="https://push2delay.eastmoney.com/api/qt/clist/get",
        retrieved_at=retrieved_text,
        upstream_fields={},
        missing_reasons={},
    )
    metrics = (
        "change_60d_pct",
        "change_ytd_pct",
        "turnover_rate_pct",
        "volume_ratio",
        "listing_date",
        "source_updated_at",
    )
    coverage = {metric: MetricCoverage(present=1, missing=0, coverage_rate=1.0) for metric in metrics}
    snapshot = MarketColdnessSnapshot(
        available=True,
        records=(record,),
        source=record.source,
        source_url=record.source_url,
        retrieved_at=retrieved_text,
        total_expected=1,
        fetched_count=1,
        page_count=1,
        response_bytes=100,
        universe_coverage_rate=1.0,
        coverage=MarketColdnessCoverage(1, 1, 1.0, coverage),
        cache_hit=False,
        cache_diagnostic="",
        reason="",
        failure=None,
    )
    produced = build_market_coldness_evidence(
        snapshot,
        as_of_session=session,
        listed_quote_codes=(code,),
        now=retrieved + timedelta(hours=10),
        min_cross_section_records=1,
        min_board_turnover_records=1,
    )[code]
    return {
        "market_coldness_score": produced["market_coldness_score"],
        "market_coldness_score_evidence_level": produced["market_coldness_score_evidence_level"],
        "market_coldness_score_evidence": produced["market_coldness_score_evidence"],
        "market_coldness_components": produced["components"],
    }


def _cashflow_row(report_date, operating_cash_flow, capex):
    value, provenance = resolve_capex_evidence(capex, None, report_date=report_date)
    return {
        "REPORT_DATE": report_date,
        "NETCASH_OPERATE": operating_cash_flow,
        "CONSTRUCT_LONG_ASSET": value,
        "CAPEX_PROVENANCE": provenance,
    }


def strict_ttm_source(*, prior_annual_fcf=10.0, annual_fcf=20.0, ttm_fcf=30.0, ttm_revenue=100.0):
    """Minimal exact FY-1/FY/TTM source rows for scorer provenance tests."""
    annual_cashflow = [
        _cashflow_row("2024-12-31", prior_annual_fcf, 0.0),
        _cashflow_row("2025-12-31", annual_fcf, 0.0),
    ]
    interim_cashflow = [
        _cashflow_row("2025-03-31", 0.0, 0.0),
        _cashflow_row("2026-03-31", ttm_fcf - annual_fcf, 0.0),
    ]
    annual_revenue = [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": ttm_revenue}]
    interim_income = [
        {"REPORT_DATE": "2025-03-31", "TOTAL_OPERATE_INCOME": 10.0},
        {"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": 10.0},
    ]
    return {
        "_ttm_revenue_history": annual_revenue,
        "_ttm_cashflow_history": annual_cashflow,
        "_ttm_income_interim": interim_income,
        "_ttm_cashflow_interim": interim_cashflow,
    }


def base_metrics(**overrides):
    metrics = {
        "code": "000001",
        "name": "样本",
        "industry": "SOFTWARE",
        "source_trade_date": "2026-07-15",
        "financial_indicator_as_of": "2025-12-31",
        "price": 50.0,
        "pe": 15.0,
        "pb": 1.0,
        "market_cap": 1e9,
        "revenue_values": [100.0, 115.0, 135.0, 160.0, 190.0],
        "revenue_years": [2021, 2022, 2023, 2024, 2025],
        "cagr_3yr": 0.18,
        "cagr_5yr": 0.17,
        "trend_growth": 0.18,
        "growth_1yr": 0.18,
        "growth_slope": 0.0,
        "growth_consistency": 0.2,
        "net_profit": 30.0,
        "net_profit_history": [12.0, 16.0, 20.0, 25.0, 30.0],
        "net_profit_years": [2021, 2022, 2023, 2024, 2025],
        "profit_1yr_change": 0.20,
        "profit_volatility": 0.2,
        "net_margin": 0.16,
        "margin_median_hist": 0.14,
        "margin_history": [0.12, 0.13, 0.14, 0.15, 0.16],
        "margin_years": [2021, 2022, 2023, 2024, 2025],
        "margin_trajectory": 0.20,
        "roe": 0.25,
        "debt_ratio": 0.25,
        "oper_cf": 40.0,
        "capex": 10.0,
        "free_cash_flow": 30.0,
        "fcf_history": [10.0, 20.0, 30.0],
        "fcf_years": [2023, 2024, 2025],
        "ocf_history": [13.0, 22.0, 40.0],
        "ocf_years": [2023, 2024, 2025],
        "adjusted_profit_ratio_history": [0.95, 0.96, 0.98],
        "adjusted_profit_ratio_years": [2023, 2024, 2025],
        "total_assets_history": [250.0, 275.0, 300.0],
        "total_assets_years": [2023, 2024, 2025],
        "interest_bearing_debt_ratio": 0.0333,
        "ocf_np_ratio": 1.3,
        "ocf_3yr_change": 0.20,
        "total_assets": 300.0,
        "total_liabilities": 75.0,
        "monetary_funds": 40.0,
        "interest_debt": 10.0,
        "q1_profit_warning": False,
        "ocf_q1_warning": False,
        "interim_revenue_warning": False,
        "interim_profit_warning": False,
        "interim_ocf_warning": False,
        "interim_yoy_basis": "same_period_yoy",
        "interim_revenue_yoy_basis": "same_period_yoy",
        "interim_profit_yoy_basis": "same_period_yoy",
        "interim_ocf_yoy_basis": "same_period_yoy",
        "interim_revenue_yoy": 0.10,
        "interim_profit_yoy": 0.10,
        "interim_ocf_yoy": 0.10,
        "interim_current_revenue": 110.0,
        "interim_current_profit": 11.0,
        "interim_current_ocf": 12.0,
        "interim_prior_revenue": 100.0,
        "interim_prior_profit": 10.0,
        "interim_prior_ocf": 10.0,
        "interim_revenue_pair_basis": "same_period_yoy",
        "interim_profit_pair_basis": "same_period_yoy",
        "interim_ocf_pair_basis": "same_period_yoy",
        "roic": None,
        "wacc": None,
        "roic_wacc_basis": None,
        "runway_score": None,
        "moat_score": None,
        "moat_durability_score": None,
        "industry_bubble_score": None,
        "industry_durability_score": None,
        "accounting_integrity_score": None,
        "technology_score": None,
        "business_model_score": None,
        "position_size_pct": None,
        "type6_portfolio_pct": None,
        "management_alignment_score": None,
        "catalyst_score": None,
        "market_coldness_score": 6.0,
        "growth_quality_score": None,
        "growth_sustainability_score": None,
        "type3_bubble_score": None,
        "cyclical_industry_score": None,
        **strict_ttm_source(),
    }
    metrics.update(overrides)
    if "revenue_values" in overrides and "revenue_years" not in overrides:
        metrics["revenue_years"] = list(range(2026 - len(overrides["revenue_values"]), 2026))
    if "net_profit_history" in overrides and "net_profit_years" not in overrides:
        metrics["net_profit_years"] = list(range(2026 - len(overrides["net_profit_history"]), 2026))
    if "margin_history" in overrides and "margin_years" not in overrides:
        metrics["margin_years"] = list(range(2026 - len(overrides["margin_history"]), 2026))
    if "fcf_history" in overrides and "fcf_years" not in overrides:
        metrics["fcf_years"] = list(range(2026 - len(overrides["fcf_history"]), 2026))
    if metrics.get("roic") is not None and metrics.get("wacc") is not None and "roic_wacc_basis" not in overrides:
        metrics["roic_wacc_basis"] = "NOPAT/平均投入资本代理"
    for key in bs.QUALITATIVE_SCORE_KEYS:
        if metrics.get(key) is not None and f"{key}_evidence" not in overrides:
            metrics[f"{key}_evidence"] = score_evidence(
                key,
                metrics["code"],
                str(metrics.get("source_trade_date") or "2026-07-15"),
            )
        if metrics.get(key) is not None and f"{key}_evidence_level" not in overrides:
            metrics[f"{key}_evidence_level"] = "derived_proxy"
    if metrics.get("market_coldness_score") is not None and not any(
        key in overrides
        for key in (
            "market_coldness_score_evidence",
            "market_coldness_score_evidence_level",
            "market_coldness_components",
        )
    ):
        try:
            metrics.update(
                production_coldness_fields(
                    code=metrics["code"],
                    as_of=str(metrics.get("source_trade_date") or "2026-07-15"),
                    target_score=float(metrics["market_coldness_score"]),
                )
            )
        except (KeyError, ValueError):
            # Some non-coldness tests deliberately use synthetic peer IDs or
            # non-trading dates.  Their generic fixture must not masquerade as
            # a valid production coldness record.
            pass
    return metrics


def primary_type6_metrics(**overrides):
    """Build a Type 6 fixture whose 6b/6c inputs are formal primary evidence."""

    metrics = base_metrics(**overrides)
    for key in ("technology_score", "business_model_score"):
        if metrics.get(key) is not None:
            metrics[f"{key}_evidence_level"] = "primary"
    return metrics


def complete_type4_metrics(**overrides):
    values = {
        "runway_score": 9.0,
        "moat_durability_score": 9.0,
        "industry_bubble_score": 8.0,
        "gross_margin": 0.40,
        "gross_margin_history": [0.38, 0.39, 0.40],
        "gross_margin_years": [2023, 2024, 2025],
        "gross_margin_cv": 0.03,
        "gross_margin_samples": 5,
        "gross_margin_trend": "stable",
        "roic": 0.20,
        "wacc": 0.08,
        "indicator_roic_history": [0.18, 0.19, 0.20],
        "indicator_roic_years": [2023, 2024, 2025],
    }
    values.update(overrides)
    return base_metrics(**values)


def complete_type3_metrics(**overrides):
    values = {
        "roic": 0.20,
        "wacc": 0.08,
        "moat_score": 9.0,
        "growth_quality_score": 9.0,
        "growth_sustainability_score": 9.0,
        "type3_bubble_score": 8.0,
    }
    values.update(overrides)
    return base_metrics(**values)


def complete_type1_metrics(**overrides):
    values = {
        "industry_durability_score": 9.0,
        "accounting_integrity_score": 9.0,
        "management_alignment_score": 9.0,
        "catalyst_score": 9.0,
    }
    values.update(overrides)
    return base_metrics(**values)


def benchmarks(**overrides):
    bucket = {
        "median_pe": 25.0,
        "median_pb": 2.0,
        "median_pb_count": 20,
        "median_roe": 0.12,
        "median_cagr": 0.10,
        "median_margin": 0.08,
        "median_debt": 0.45,
        "median_cagr_count": 20,
        "neutral_benchmark": 0.15,
    }
    bucket.update(overrides)
    return {"SOFTWARE": bucket, "ALL": dict(bucket)}


def trusted_type5_scores(*, code="000001", **scores):
    """Build scores as if the strict UI evidence boundary validated them."""
    result = {"_type5_external_validation_token": bs._TYPE5_EXTERNAL_VALIDATION_TOKEN}
    for key, value in scores.items():
        result[key] = value
        result[f"{key}_evidence"] = score_evidence(key, code)
        result[f"{key}_evidence_level"] = "primary"
    return result


def type5_history_evidence(
    *,
    code="000001",
    as_of="2026-07-17",
    pb_percentile=0.08,
    current_pb=0.95,
):
    observation_count = 800
    below_count = round(float(pb_percentile) * observation_count)
    if not math.isclose(
        below_count / observation_count,
        float(pb_percentile),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("test percentile must replay exactly at 800 observations")
    values = []
    counts = []
    if below_count:
        values.append(current_pb * 0.8)
        counts.append(below_count)
    if below_count < observation_count:
        values.append(current_pb * 1.2)
        counts.append(observation_count - below_count)
    distribution = {"values": values, "counts": counts}
    replay = replay_valuation_distribution(distribution, current_pb)
    assert replay is not None
    return {
        "available": True,
        "code": code,
        "as_of": as_of,
        "model_id": bs.LONG_HORIZON_HISTORY_MODEL_ID,
        "shareholder_return": {"available": True},
        "valuation_history": {
            "available": True,
            "window_years": 5,
            "span_days": 1_800,
            "start_delay_days": 1,
            "end_date": as_of,
            "pb_observations": observation_count,
            "pb_percentile": pb_percentile,
            "current_pb_mrq": current_pb,
            "median_pb_mrq": replay["median"],
            "pb_distribution": distribution,
            "formula": "percentile=(count(x<current)+0.5*count(x=current))/historical_count",
        },
        "sources": [{"name": "Eastmoney historical valuation", "url": "https://example.test/valuation"}],
    }


def type7_report_evidence(*, code="000001", as_of="2026-07-17"):
    sources = [
        {
            "security_code": code,
            "company_name": "测试公司",
            "title": f"研报{index}",
            "publisher": f"机构{index}",
            "publisher_id": f"eastmoney-org:{80_000_000 + index}",
            "url": f"https://data.eastmoney.com/report/info/AP2026071{index}0000000000.html",
            "as_of": as_of,
            "evidence_id": f"eastmoney:AP2026071{index}0000000000",
        }
        for index in range(1, 4)
    ]
    body_ids = sorted(source["evidence_id"] for source in sources)
    content_verification = {
        "model_id": "type7-report-body-crosscheck-v2",
        "code": code,
        "as_of": as_of,
        "passed": True,
        "required_bodies": 3,
        "attempted_bodies": 3,
        "verified_bodies": 3,
        "distinct_publishers": 3,
        "bodies": [
            {
                "evidence_id": evidence_id,
                "content_sha256": f"{index:064x}",
                "content_length": 500 + index,
                "paragraph_count": 3,
                "structure_signals": ["analysis", "risk"],
                "fact_count": 2,
                "facts": [
                    {
                        "fact_key": "2026Q1:eps",
                        "period": "2026Q1",
                        "metric": "eps",
                        "unit": "CNY_PER_SHARE",
                        "value": (1.0, 3.0, 5.0)[index - 1],
                    },
                    {
                        "fact_key": "2026Q1:revenue",
                        "period": "2026Q1",
                        "metric": "revenue",
                        "unit": "CNY_100M",
                        "value": (35.50, 35.54, 50.0)[index - 1],
                    },
                ],
                "identity_checks": {
                    "code_in_body": True,
                    "name_in_body": True,
                    "detail_code": True,
                    "detail_name": True,
                    "detail_title": True,
                    "detail_publisher": True,
                    "detail_date": True,
                    "dom_json_body": True,
                },
            }
            for index, evidence_id in enumerate(body_ids, start=1)
        ],
        "cross_check": {
            "passed": True,
            "minimum_reports": 2,
            "fact_key": "2026Q1:revenue",
            "fact_unit": "CNY_100M",
            "consensus_value": 35.52,
            "supporting_evidence_ids": body_ids[:2],
            "max_relative_spread": 0.00112613,
        },
        "reason": "",
    }
    return {
        "available": True,
        "code": code,
        "as_of": as_of,
        "model_id": bs.RESEARCH_EVIDENCE_MODEL_ID,
        "sources": sources,
        "distinct_publishers": 3,
        "content_verification": content_verification,
        "cache_hit": False,
        "cache_diagnostic": "disabled",
        "reason": "",
    }


def type5_coldness_fields(
    *,
    code="000001",
    as_of="2026-07-17",
    score=8.5,
    change_60d=-25.0,
    change_ytd=-30.0,
):
    target_score = score
    if max(change_60d, change_ytd) > 0:
        target_score = 2.0
    return production_coldness_fields(code=code, as_of=as_of, target_score=target_score)


def complete_type5_bottom_metrics(**overrides):
    values = {
        "industry": "COAL",
        "source_trade_date": "2026-07-17",
        "pb": 0.95,
        "market_cap": 500.0,
        "net_profit_history": [100.0, 50.0, 20.0, 40.0, 80.0, 120.0, 60.0, 90.0, 50.0, 30.0],
        "net_profit_years": list(range(2016, 2026)),
        "gross_margin_history": [0.42, 0.30, 0.18, 0.25, 0.38, 0.45, 0.29, 0.35, 0.23, 0.17],
        "gross_margin_years": list(range(2016, 2026)),
        **type5_coldness_fields(),
    }
    values.update(overrides)
    return base_metrics(**values)


def complete_dcf_evidence(*, current_price=50.0):
    buy_upper = 100.0
    sell_lower = 160.0
    zone = "买入区" if current_price <= buy_upper else "卖出区" if current_price >= sell_lower else "观察区"
    result = {
        "code": "000001",
        "current_price": current_price,
        "industry_code": "SOFTWARE",
        "dcf_points": {
            "pessimistic": {"lower": 60.0, "upper": 80.0},
            "neutral": {"lower": 120.0, "upper": 140.0},
            "optimistic": {"lower": 180.0, "upper": 200.0},
        },
        "buy_zone_upper": buy_upper,
        "sell_zone_lower": sell_lower,
        "zone": zone,
        "params": {
            "pessimistic": {
                "growth": 0.02,
                "wacc_base": 0.10,
                "terminal_g": 0.005,
                "margin_retention": 0.70,
                "forecast_years": 5,
            },
            "neutral": {
                "growth": 0.04,
                "wacc_base": 0.09,
                "terminal_g": 0.01,
                "margin_retention": 0.80,
                "forecast_years": 5,
            },
            "optimistic": {
                "growth": 0.06,
                "wacc_base": 0.08,
                "terminal_g": 0.015,
                "margin_retention": 0.90,
                "forecast_years": 5,
            },
        },
        "base_wacc": 0.082,
        "base_fcf": 20.0,
        "base_revenue": 100.0,
        "valuation_input_basis": "strict_ttm",
        "base_revenue_basis": "strict_ttm_reported_revenue",
        "base_fcf_basis": "normalised_two_annual_plus_ttm_cfo_less_capex_proxy",
        "shares_outstanding": 1.0,
        "latest_fcff": 30.0,
        "recent_fcff": [10.0, 20.0, 30.0],
        "recent_fcff_periods": [
            {"kind": "annual", "report_date": "2024-12-31"},
            {"kind": "annual", "report_date": "2025-12-31"},
            {"kind": "ttm", "through_report_date": "2026-03-31"},
        ],
        "fcf_normalisation_basis": "recent_median",
        "fcf_normalisation_period_basis": "two_annual_plus_strict_ttm",
        "normalisation_premium_cap": 1.25,
        "base_fcf_adjustments": [],
        "net_debt": 10.0,
        "industry_unlevered_beta": 0.8,
        "levered_beta": 1.0,
        "pre_tax_cost_of_debt": 0.05,
        "tax_shield_rate": 0.0,
        "beta_source": "industry_unlevered_relevered",
        "wacc_capital_structure": "market_equity_and_known_debt",
        "tax_shield_source": "taxable_profit_evidence_unavailable",
        "growth_evidence": "historical_cagr_and_trend",
        "model_risk_data_as_of": "2026-01-05",
        "explicit_forecast_years": 5,
        "long_horizon_forecast_years": 10,
        "long_horizon_growth_path": "linear_fade_from_scenario_growth_to_terminal_growth",
        "long_horizon_formula_version": 1,
        "wacc_components": {
            "equity_weight": 0.8,
            "debt_weight": 0.2,
            "cost_of_equity": 0.09,
            "pre_tax_cost_of_debt": 0.05,
            "tax_shield_rate": 0.0,
        },
    }
    source = strict_ttm_source()
    result["ttm_fcff_evidence"] = bs.reconstruct_ttm_fcff(
        source["_ttm_cashflow_history"],
        source["_ttm_cashflow_interim"],
        period_contract=TTM_CONTRACT,
    )
    result["ttm_revenue_evidence"] = bs.reconstruct_ttm_revenue(
        source["_ttm_revenue_history"],
        source["_ttm_income_interim"],
        period_contract=TTM_CONTRACT,
    )
    result["fcf_normalisation_period"] = {
        "period_set": "two_annual_plus_strict_ttm",
        "periods": result["recent_fcff_periods"],
        "normalisation_method": result["fcf_normalisation_basis"],
        "cash_flow_kind": result["ttm_fcff_evidence"]["cash_flow_kind"],
        "formula_version": result["ttm_fcff_evidence"]["formula_version"],
    }
    result["dcf_10y_points"] = {
        scenario: {
            "lower": bs.dcf_valuation_fading_growth(
                base_fcf=result["base_fcf"],
                base_revenue=result["base_revenue"],
                revenue_growth=params["growth"],
                wacc=params["wacc_base"] + bs.BAND_WACC_DELTA,
                terminal_g=params["terminal_g"],
                shares_outstanding=result["shares_outstanding"],
                net_debt=result["net_debt"],
                margin_retention=params["margin_retention"],
                forecast_years=10,
            ),
            "upper": bs.dcf_valuation_fading_growth(
                base_fcf=result["base_fcf"],
                base_revenue=result["base_revenue"],
                revenue_growth=params["growth"],
                wacc=params["wacc_base"] - bs.BAND_WACC_DELTA,
                terminal_g=params["terminal_g"],
                shares_outstanding=result["shares_outstanding"],
                net_debt=result["net_debt"],
                margin_retention=params["margin_retention"],
                forecast_years=10,
            ),
        }
        for scenario, params in result["params"].items()
    }
    return result


def complete_pb_evidence(*, current_price=50.0):
    equities = {year: 550.0 + (year - 2020) * 50.0 for year in range(2020, 2026)}
    data = {
        "balance": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "PARENT_EQUITY": equity,
                "TOTAL_EQUITY": equity + 20.0,
                "MINORITY_EQUITY": 20.0,
            }
            for year, equity in equities.items()
        ],
        "income_history": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "PARENT_NETPROFIT": ((equities[year - 1] + equities[year]) / 2.0) * 0.12,
            }
            for year in range(2021, 2026)
        ],
        "income_interim": [
            {"REPORT_DATE": "2025-06-30", "TOTAL_OPERATE_INCOME": 450.0, "PARENT_NETPROFIT": 45.0},
            {"REPORT_DATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 500.0, "PARENT_NETPROFIT": 50.0},
        ],
    }
    result = scenarios._run_financial_pb_valuation(
        "000001",
        "样本银行",
        current_price,
        data,
        10.0,
        None,
        "BANK",
    )
    assert result is not None
    return result


class TestFrameworkInvariants(unittest.TestCase):
    def test_patch6_weights_and_priority_are_exact(self):
        expected = {
            "type1": [0.30, 0.35, 0.20, 0.15],
            "type2": [0.25, 0.30, 0.25, 0.20],
            "type3": [0.25, 0.20, 0.20, 0.25, 0.10],
            "type4": [0.25, 0.25, 0.20, 0.15, 0.08, 0.07],
            "type5": [0.35, 0.25, 0.20, 0.10, 0.10],
            "type6": [0.25, 0.20, 0.15, 0.25, 0.15],
            "type7": [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0],
        }
        self.assertEqual(bs.QUALIFY_THRESHOLD, 7.0)
        self.assertEqual(bs.TYPE_PRIORITY, ["type1", "type2", "type5", "type3", "type4", "type6", "type7"])
        for key, weights in expected.items():
            self.assertEqual(list(bs.TYPE_WEIGHTS[key].values()), weights)
            self.assertAlmostEqual(sum(weights), 1.0)

    def test_non_finite_values_are_missing_not_high_scores(self):
        for value in (float("nan"), float("inf"), float("-inf"), np.float64(np.nan)):
            self.assertIsNone(bs._safe_float(value))
            self.assertEqual(bs._score_0_10(value, [(0, 0), (1, 10)]), 0.0)

    def test_finish_exposes_missing_score_placeholder_and_fails_closed(self):
        scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type1"]}
        scores["1b"] = float("nan")
        reasons = {key: "可复算证据" for key in scores}

        triggered, _total, cleaned, metadata = bs._finish("type1", scores, reasons)

        self.assertFalse(triggered)
        self.assertEqual(cleaned["1b"], 0.0)
        self.assertEqual(metadata["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(metadata["_evidence"], "incomplete")
        self.assertIn("1b", metadata["_missing"])
        self.assertIn("0占位", metadata["_score_placeholder"])
        self.assertEqual(metadata["_score_quality"], "缺失项以0占位")

    def test_displayed_rounding_is_used_for_trigger(self):
        scores = {key: 6.96 for key in bs.TYPE_WEIGHTS["type1"]}
        reasons = {key: "证据" for key in scores}
        triggered, total, _, _ = bs._finish("type1", scores, reasons)
        self.assertEqual(total, 7.0)
        self.assertTrue(triggered)

    def test_exact_seven_boundary_triggers_for_every_type_without_veto(self):
        for type_key, weights in bs.TYPE_WEIGHTS.items():
            scores = {key: 7.0 for key in weights}
            reasons = {key: "边界证据" for key in weights}
            triggered, total, _, _ = bs._finish(type_key, scores, reasons)
            self.assertEqual(total, 7.0, type_key)
            self.assertTrue(triggered, type_key)

    def test_veto_always_wins_and_reasons_are_compact(self):
        scores = {key: 10 for key in bs.TYPE_WEIGHTS["type1"]}
        reasons = {key: "这是一个超过二十个字符而且不应该完整输出的证据说明" for key in scores}
        reasons["_veto"] = "这是一个同样很长的一票否决原因"
        triggered, total, _, compact = bs._finish("type1", scores, reasons, veto=True)
        self.assertEqual(total, 10.0)
        self.assertFalse(triggered)
        self.assertTrue(all(len(text) <= bs.EVIDENCE_MAX_LENGTH for text in compact.values()))

    def test_compact_reason_ends_at_a_complete_evidence_segment_with_an_ellipsis(self):
        compact = bs._compact_reason(
            "量价冷度;60日19.7%;年内13.4%;换手2.1%;量比0.8;同行样本完整;估值历史完整;数据来源完整"
        )

        self.assertTrue(compact.endswith("…"))
        self.assertLessEqual(len(compact), bs.EVIDENCE_MAX_LENGTH)
        self.assertNotIn("估值历…", compact)

    def test_not_applicable_overrides_veto_but_confirmed_veto_overrides_other_missing_evidence(self):
        scores = {key: 1.0 for key in bs.TYPE_WEIGHTS["type4"]}
        reasons = {key: "部分证据" for key in scores}
        reasons["_veto"] = "已确认独立否决"

        not_applicable = bs._finish("type4", scores, reasons, veto=True, applicable=False)
        confirmed_veto = bs._finish("type4", scores, reasons, veto=True, evidence_complete=False)

        self.assertEqual(not_applicable[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertNotIn("_veto", not_applicable[3])
        self.assertEqual(confirmed_veto[3]["_status"], bs.STATUS_VETOED)
        self.assertEqual(confirmed_veto[3]["_evidence"], "incomplete")
        self.assertIn("_veto", confirmed_veto[3])
        self.assertIn("_missing", confirmed_veto[3])
        self.assertIn("仅供诊断", confirmed_veto[3]["_score_quality"])

    @staticmethod
    def _decision_payload(type_key, outcome, *, ledger=None, market_context=None):
        triggered, total, sub_scores, reasons = outcome
        status = reasons["_status"]
        payload = {
            "triggered": triggered,
            "total": total,
            "sub_scores": sub_scores,
            "reasons": reasons,
            "veto": bool(reasons.get("_veto")),
            "status": status,
            "applicable": status != bs.STATUS_NOT_APPLICABLE,
            "evidence_complete": reasons.get("_evidence") == "complete",
            bs._DECISION_MARKET_CONTEXT: market_context
            or {"tradable": True, "reference_price": False, "risk_status": "normal"},
        }
        if ledger is not None:
            payload["ledger"] = ledger
        return payload

    def test_decision_contract_scope_exclusion_has_zero_bounds(self):
        payload = self._decision_payload("type5", bs._not_applicable("type5", "不属于强周期公司"))

        decision = bs.replay_buy_decision("type5", payload)

        self.assertEqual(set(decision), bs._DECISION_FIELDS)
        self.assertEqual(decision["model_id"], "buy-decision-bounds-v1")
        self.assertTrue(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "scope_exclusion")
        self.assertEqual((decision["score_lower_bound"], decision["score_upper_bound"]), (0.0, 0.0))
        self.assertEqual(decision["veto_state"], "none")
        self.assertFalse(decision["potentially_triggerable"])
        self.assertEqual(decision["missing_dimensions"], [])

    def test_decision_contract_confirmed_veto_is_final_despite_other_missing_dimensions(self):
        scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type4"]}
        scores["4c"] = 2.0
        reasons = {key: "部分可复算证据" for key in scores}
        reasons["_veto"] = "护城河已确认不足"
        payload = self._decision_payload(
            "type4",
            bs._finish(
                "type4",
                scores,
                reasons,
                veto=True,
                evidence_complete=False,
                missing_dimensions=["4a", "4b"],
            ),
        )

        decision = bs.replay_buy_decision("type4", payload)

        self.assertTrue(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "confirmed_veto")
        self.assertEqual(decision["veto_state"], "confirmed")
        self.assertFalse(decision["potentially_triggerable"])
        self.assertEqual(decision["missing_dimensions"], ["4a", "4b"])

    def test_decision_contract_type3_missing_dimension_uses_theoretical_ten_not_adapter_cap(self):
        scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type3"]}
        payload = self._decision_payload(
            "type3",
            bs._finish(
                "type3",
                scores,
                {key: "部分可复算证据" for key in scores},
                evidence_complete=False,
                missing_dimensions=["3b"],
            ),
        )

        decision = bs.replay_buy_decision("type3", payload)

        self.assertEqual(decision["missing_dimensions"], ["3b"])
        self.assertEqual(decision["score_lower_bound"], 6.4)
        self.assertEqual(decision["score_upper_bound"], 8.4)
        self.assertFalse(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "unresolved_missing_evidence")
        self.assertTrue(decision["potentially_triggerable"])

    def test_decision_contract_conservative_upper_bound_can_finish_partial_no_buy(self):
        scores = {key: 0.0 for key in bs.TYPE_WEIGHTS["type1"]}
        scores["1a"] = 5.0
        scores["1b"] = 4.0
        payload = self._decision_payload(
            "type1",
            bs._finish(
                "type1",
                scores,
                {key: "已知低分证据" for key in scores},
                evidence_complete=False,
                missing_dimensions=["1d"],
            ),
        )

        decision = bs.replay_buy_decision("type1", payload)

        self.assertEqual((decision["score_lower_bound"], decision["score_upper_bound"]), (2.9, 4.4))
        self.assertTrue(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "conservative_upper_bound")
        self.assertFalse(decision["potentially_triggerable"])

    def test_decision_contract_type6_missing_position_is_an_explicit_action_condition(self):
        scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type6"]}
        scores["6e"] = 10.0
        reasons = {key: "可复算公司证据" for key in scores}
        reasons["_condition"] = "须确认实际仓位符合建议上限"
        payload = self._decision_payload(
            "type6",
            bs._finish("type6", scores, reasons, extra_condition=False),
        )

        decision = bs.replay_buy_decision("type6", payload)

        self.assertFalse(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "action_condition")
        self.assertEqual(decision["missing_dimensions"], ["6e"])
        self.assertEqual((decision["score_lower_bound"], decision["score_upper_bound"]), (6.8, 8.3))
        self.assertEqual(decision["veto_state"], "none")
        self.assertTrue(decision["potentially_triggerable"])

    def test_decision_contract_type6_action_input_cannot_rescue_an_unreachable_total(self):
        scores = {"6a": 5.0, "6b": 5.0, "6c": 0.0, "6d": 0.0, "6e": 10.0}
        reasons = {key: "可复算公司证据" for key in scores}
        reasons["_condition"] = "须确认实际仓位符合建议上限"
        payload = self._decision_payload(
            "type6",
            bs._finish("type6", scores, reasons, extra_condition=False),
        )

        decision = bs.replay_buy_decision("type6", payload)

        self.assertEqual((decision["score_lower_bound"], decision["score_upper_bound"]), (2.2, 3.8))
        self.assertTrue(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "conservative_upper_bound")
        self.assertFalse(decision["potentially_triggerable"])

    def test_decision_contract_type7_rejects_the_legacy_top_level_ledger(self):
        scores = {"7a": 8.0, "7b": 8.0, "7c": 6.0}
        ledger = {
            "scores": {"template1": 80.0, "template5": 80.0, "patch5": 60.0},
            "decisive_score_upper_bounds": {"template1": 80.0, "template5": 80.0, "patch5": 70.0},
            "prerequisites_complete": False,
        }
        payload = self._decision_payload(
            "type7",
            bs._finish(
                "type7",
                scores,
                {key: "部分质量证据" for key in scores},
                evidence_complete=False,
                missing_dimensions=["7c"],
            ),
            ledger=ledger,
        )

        with self.assertRaisesRegex(ValueError, "current classified ledger"):
            bs.replay_buy_decision("type7", payload)

    def test_decision_contract_market_block_is_not_misreported_as_a_company_veto(self):
        scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type1"]}
        outcome = bs._finish("type1", scores, {key: "完整证据" for key in scores})
        payload = self._decision_payload(
            "type1",
            outcome,
            market_context={"tradable": False, "reference_price": False, "risk_status": ""},
        )
        payload["reasons"]["_status"] = bs.STATUS_BLOCKED
        payload["reasons"]["_veto"] = "标的不可交易"
        payload["reasons"][bs._DECISION_MARKET_BLOCK_REASON] = "标的不可交易"
        payload["status"] = bs.STATUS_BLOCKED
        payload["triggered"] = False
        payload["veto"] = True

        decision = bs.replay_buy_decision("type1", payload)

        self.assertTrue(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "market_block")
        self.assertEqual(decision["veto_state"], "none")
        self.assertFalse(decision["potentially_triggerable"])

    def test_decision_contract_replays_hard_vetoes_without_trusting_status_or_veto_flags(self):
        cases = {
            "type1": ({"1a": 1.0, "1b": 8.0, "1c": 8.0, "1d": 8.0}, None),
            "type2": ({"2a": 8.0, "2b": 8.0, "2c": 3.0, "2d": 8.0}, None),
            "type3": ({"3a": 8.0, "3b": 8.0, "3c": 8.0, "3d": 3.0, "3e": 8.0}, None),
            "type4": ({"4a": 8.0, "4b": 8.0, "4c": 8.0, "4d": 8.0, "4e": 3.0, "4f": 3.0}, None),
            "type6": ({"6a": 6.0, "6b": 4.0, "6c": 4.0, "6d": 4.0, "6e": 8.0}, None),
        }

        for type_key, (scores, ledger) in cases.items():
            with self.subTest(type_key=type_key):
                outcome = bs._finish(type_key, scores, {key: "完整源证据" for key in scores})
                payload = self._decision_payload(type_key, outcome, ledger=ledger)
                decision = bs.replay_buy_decision(type_key, payload)

                self.assertEqual(decision["decision_basis"], "confirmed_veto")
                self.assertEqual(decision["veto_state"], "confirmed")
                self.assertFalse(decision["potentially_triggerable"])

        type5_scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type5"]}
        type5_payload = self._decision_payload(
            "type5",
            bs._finish("type5", type5_scores, {key: "附录完整证据" for key in type5_scores}),
        )
        type5_decision = bs.replay_buy_decision("type5", type5_payload)
        self.assertEqual(type5_decision["decision_basis"], "full_evidence")
        self.assertEqual(type5_decision["veto_state"], "none")
        self.assertTrue(type5_decision["potentially_triggerable"])

    def test_decision_contract_type3_bubble_is_a_total_cap_not_a_hard_veto(self):
        scores = {"3a": 9.0, "3b": 9.0, "3c": 9.0, "3d": 9.0, "3e": 3.0}
        payload = self._decision_payload(
            "type3",
            bs._finish("type3", scores, {key: "完整源证据" for key in scores}, total_cap=4.9),
        )

        decision = bs.replay_buy_decision("type3", payload)

        self.assertEqual((decision["score_lower_bound"], decision["score_upper_bound"]), (4.9, 4.9))
        self.assertEqual(decision["veto_state"], "none")
        self.assertEqual(decision["decision_basis"], "full_evidence")
        self.assertFalse(decision["potentially_triggerable"])

    def test_legacy_type7_safety_veto_is_diagnostic_and_cannot_veto_the_classified_model(self):
        ledger = {
            "scores": {"template1": 60.0, "template5": 80.0, "patch5": 60.0},
            "patch5": {
                "safety_margin_complete": True,
                "safety_margin_score": 7.0,
            },
            "all_scores_strictly_above_70": False,
            "decisively_not_triggered": True,
            "decisive_score_upper_bounds": {"template1": 60.0, "template5": 80.0, "patch5": 60.0},
            "prerequisites": {},
            "prerequisites_complete": True,
            "safety_veto": True,
        }
        returned_ledger = assess_patch6_type7({"code": "000001", "industry": "SOFTWARE"}, legacy_diagnostic=ledger)

        self.assertEqual(returned_ledger["model_id"], bs.PATCH6_TYPE7_MODEL_ID)
        self.assertEqual(returned_ledger["legacy_diagnostic"]["scores"], ledger["scores"])
        self.assertFalse(returned_ledger["legacy_diagnostic"]["decisive"])
        self.assertFalse(returned_ledger["veto"])

    def test_legacy_type7_strict_subscore_failure_does_not_decide_the_classified_model(self):
        ledger = {
            "scores": {
                "template1": 67.81,
                "template5": 72.24,
                "patch5": 68.48,
            },
            "patch5": {
                "safety_margin_complete": True,
                "safety_margin_score": 13.7,
            },
            "all_scores_strictly_above_70": False,
            # These pre-history upper bounds are deliberately above 70, which
            # reproduces the former path where a rounded 7.0 total became
            # ``conditional`` despite complete, already-known source scores.
            "decisively_not_triggered": False,
            "decisive_score_upper_bounds": {
                "template1": 80.0,
                "template5": 80.0,
                "patch5": 80.0,
            },
            "prerequisites": {
                "complete_contract": {"passed": True},
            },
            "prerequisites_complete": True,
            "safety_veto": False,
        }
        returned_ledger = assess_patch6_type7({"code": "600988", "industry": "GOLD"}, legacy_diagnostic=ledger)

        self.assertEqual(returned_ledger["model_id"], bs.PATCH6_TYPE7_MODEL_ID)
        self.assertEqual(returned_ledger["legacy_diagnostic"]["scores"], ledger["scores"])
        self.assertFalse(returned_ledger["legacy_diagnostic"]["decisive"])
        self.assertFalse(returned_ledger["triggered"])
        self.assertNotEqual(returned_ledger["scores"], ledger["scores"])

    def test_evidence_reason_never_exposes_internal_model_or_evidence_ids(self):
        automatic = {
            "runway_score": 8.0,
            "runway_score_evidence_level": "derived_proxy",
            "runway_score_evidence": {
                "source": "Eastmoney reported data; Patch6 observable-outcome formula v2",
                "evidence_id": "patch6-observable-outcomes-v2:runway_score:600519:20260717",
                "as_of": "2026-07-17",
                "summary": "runway_score=8.0;model=patch6-observable-outcomes-v2",
            },
        }
        manual = {
            "type5_bottom_signal_score": 8.0,
            "type5_bottom_signal_score_evidence_level": "primary",
            "type5_bottom_signal_score_evidence": {
                "source": "行业协会",
                "evidence_id": "bottom-2025-01",
                "as_of": "2025-12-31",
                "summary": "PB分位与库存去化",
            },
        }

        automatic_reason = bs._evidence_reason(automatic, "runway_score", "fallback")
        manual_reason = bs._evidence_reason(manual, "type5_bottom_signal_score", "fallback")

        self.assertEqual(automatic_reason, "增长趋势与同行数据")
        self.assertEqual(manual_reason, "PB分位与库存去化")
        self.assertNotIn("patch6", automatic_reason + manual_reason)


class TestMetricExtraction(unittest.TestCase):
    def test_evidence_id_does_not_zero_pad_an_unrelated_numeric_token_into_the_security_code(self):
        extracted = bs.extract_metrics(
            {
                "moat_score": 9.0,
                "moat_score_evidence_level": "primary",
                "moat_score_evidence": {
                    "source": "研究记录",
                    "evidence_id": "report-1",
                    "as_of": "2026-07-17",
                    "summary": "研究结论",
                },
            },
            {
                "code": "000001",
                "name": "样本",
                "market": "SZ",
                "source_trade_date": "2026-07-17",
            },
            "SOFTWARE",
        )

        self.assertIsNone(extracted["moat_score"])
        self.assertIsNone(extracted["moat_score_evidence"])

    def test_metric_extraction_preserves_explicit_quantitative_evidence_level(self):
        extracted = bs.extract_metrics(
            {
                "moat_score": 7.5,
                "moat_score_evidence": {
                    "source": "可复算代理",
                    "evidence_id": "proxy:moat:000001:20260717",
                    "as_of": "2026-07-17",
                    "summary": "代理结果",
                },
                "moat_score_evidence_level": "derived_proxy",
            },
            {
                "code": "000001",
                "name": "样本",
                "market": "SZ",
                "source_trade_date": "2026-07-17",
            },
            "SOFTWARE",
        )

        self.assertEqual(extracted["moat_score"], 7.5)
        self.assertEqual(extracted["moat_score_evidence_level"], "derived_proxy")

    def test_metric_extraction_does_not_invent_a_primary_evidence_level(self):
        extracted = bs.extract_metrics(
            {
                "moat_score": 7.5,
                "moat_score_evidence": {
                    "source": "未标注来源",
                    "evidence_id": "unlabelled:moat:000001:20260717",
                    "as_of": "2026-07-17",
                    "summary": "未标注结果",
                },
            },
            {
                "code": "000001",
                "name": "样本",
                "market": "SZ",
                "source_trade_date": "2026-07-17",
            },
            "SOFTWARE",
        )

        self.assertIsNone(extracted["moat_score_evidence_level"])

    def test_metric_extraction_rejects_an_unlabelled_type7_direct_score(self):
        extracted = bs.extract_metrics(
            {
                "business_clarity_score": 9.5,
                "business_clarity_score_evidence": {
                    "source": "未标注研究来源",
                    "evidence_id": "research:business-clarity:000001:20260717",
                    "as_of": "2026-07-17",
                    "summary": "业务清晰度研究分",
                },
            },
            {
                "code": "000001",
                "name": "样本",
                "market": "SZ",
                "source_trade_date": "2026-07-17",
            },
            "SOFTWARE",
        )

        self.assertIsNone(extracted["business_clarity_score"])
        self.assertIsNone(extracted["business_clarity_score_evidence"])
        self.assertIsNone(extracted["business_clarity_score_evidence_level"])

    def test_metric_extraction_preserves_exact_rows_for_strict_ttm_source_binding(self):
        source = strict_ttm_source()
        extracted = bs.extract_metrics(
            {
                "revenue_history": source["_ttm_revenue_history"],
                "cashflow": source["_ttm_cashflow_history"],
                "income_interim": source["_ttm_income_interim"],
                "cashflow_interim": source["_ttm_cashflow_interim"],
            },
            {
                "code": "000001",
                "name": "样本",
                "market": "SZ",
                "listing_date": "2024-01-10",
                "source_trade_date": "2026-07-22",
            },
            "SOFTWARE",
        )

        for key, expected in source.items():
            self.assertEqual(extracted[key], expected)
        self.assertEqual(extracted["listing_date"], "2024-01-10")

    def test_main_financial_indicators_feed_formula_backed_metrics_without_unit_confusion(self):
        indicators = []
        for year, roic, rd_expense, shares in (
            (2024, 18.0, 4.0, 100.0),
            (2025, 20.0, 5.0, 101.0),
        ):
            indicators.append(
                {
                    "SECUCODE": "600519.SH",
                    "REPORT_DATE": f"{year}-12-31",
                    "REPORT_TYPE": "年报",
                    "REPORT_DATE_NAME": f"{year}年报",
                    "REPORT_YEAR": str(year),
                    "NOTICE_DATE": f"{year + 1}-04-01",
                    "SOURCE_REPORT_NAME": RPT_MAIN_FINANCIAL_INDICATORS,
                    "RDEXPEND": rd_expense,
                    "ROIC": roic,
                    "ROEJQ": 22.0,
                    "XSMLL": 40.0,
                    "XSJLL": 10.0,
                    "TAXRATE": 25.0,
                    "TOTAL_SHARE": shares,
                    "STAFF_NUM": 1_000,
                    "KCFJCXSYJLR": 9.5,
                    "INTEREST_DEBT_RATIO": 8.0,
                }
            )
        fin = {
            "indicators": indicators,
            "revenue_history": [
                {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 90.0},
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 100.0},
            ],
            "income_history": [
                {
                    "REPORT_DATE": "2024-12-31",
                    "TOTAL_OPERATE_INCOME": 90.0,
                    "PARENT_NETPROFIT": 9.0,
                    "OPERATE_PROFIT": 12.0,
                },
                {
                    "REPORT_DATE": "2025-12-31",
                    "TOTAL_OPERATE_INCOME": 100.0,
                    "PARENT_NETPROFIT": 10.0,
                    "OPERATE_PROFIT": 14.0,
                },
            ],
            "cashflow": [
                {
                    "REPORT_DATE": "2024-12-31",
                    "NETCASH_OPERATE": 10.0,
                    "CONSTRUCT_LONG_ASSET": 2.0,
                },
                {
                    "REPORT_DATE": "2025-12-31",
                    "NETCASH_OPERATE": 12.0,
                    "CONSTRUCT_LONG_ASSET": 2.0,
                },
            ],
            "balance": [
                {
                    "REPORT_DATE": "2024-12-31",
                    "TOTAL_ASSETS": 120.0,
                    "TOTAL_LIABILITIES": 20.0,
                    "TOTAL_EQUITY": 100.0,
                    "PARENT_EQUITY": 100.0,
                    "SHORT_LOAN": 10.0,
                    "MONETARYFUNDS": 5.0,
                },
                {
                    "REPORT_DATE": "2025-12-31",
                    "TOTAL_ASSETS": 135.0,
                    "TOTAL_LIABILITIES": 25.0,
                    "TOTAL_EQUITY": 110.0,
                    "PARENT_EQUITY": 110.0,
                    "SHORT_LOAN": 10.0,
                    "MONETARYFUNDS": 5.0,
                },
            ],
        }

        result = bs.extract_metrics(
            fin,
            {"code": "600519", "name": "样本", "market": "SH", "market_cap": 1_000.0},
            "SOFTWARE",
        )

        self.assertEqual(result["financial_indicator_status"], "complete")
        self.assertEqual(result["financial_indicator_as_of"], "2025-12-31")
        self.assertAlmostEqual(result["rd_intensity"], 0.05)
        self.assertAlmostEqual(result["gross_margin"], 0.40)
        self.assertAlmostEqual(result["adjusted_profit_ratio"], 0.95)
        self.assertAlmostEqual(result["ocf_np_ratio"], 1.20)
        self.assertAlmostEqual(result["share_dilution_1yr"], 0.01)
        self.assertAlmostEqual(result["roic"], 0.20)
        self.assertEqual(result["roic_wacc_basis"], "Eastmoney年度ROIC/公司资本结构WACC")
        self.assertEqual(result["wacc_tax_shield_source"], "Eastmoney年度TAXRATE")

    def test_bank_indicator_evidence_is_extracted_without_industrial_cashflow_semantics(self):
        indicators = []
        for year, nim, npl, capital in ((2024, 1.80, 1.10, 12.0), (2025, 1.90, 1.00, 12.5)):
            row = {
                "SECUCODE": "600036.SH",
                "REPORT_DATE": f"{year}-12-31",
                "REPORT_TYPE": "年报",
                "REPORT_DATE_NAME": f"{year}年报",
                "REPORT_YEAR": str(year),
                "NOTICE_DATE": f"{year + 1}-04-01",
                "SOURCE_REPORT_NAME": RPT_MAIN_FINANCIAL_INDICATORS,
                "RDEXPEND": None,
                "ROIC": None,
                "ROEJQ": 12.0,
                "XSMLL": None,
                "XSJLL": None,
                "TAXRATE": 25.0,
                "TOTAL_SHARE": 100.0,
                "STAFF_NUM": 1_000,
                "KCFJCXSYJLR": 10.0,
                "INTEREST_DEBT_RATIO": None,
                "NET_INTEREST_MARGIN": nim,
                "NET_INTEREST_SPREAD": nim - 0.1,
                "NEWCAPITALADER": capital,
                "FIRST_ADEQUACY_RATIO": capital - 2.0,
                "NONPERLOAN": npl,
                "LOAN_PROVISION_RATIO": 3.0,
                "TOTALDEPOSITS": 1_000.0,
                "GROSSLOANS": 700.0,
                "LOAN_ADVANCES": 680.0,
            }
            indicators.append(row)
        result = bs.extract_metrics(
            {
                "indicators": indicators,
                "income_history": [
                    {"REPORT_DATE": "2024-12-31", "PARENT_NETPROFIT": 10.0},
                    {"REPORT_DATE": "2025-12-31", "PARENT_NETPROFIT": 12.0},
                ],
                "balance": [
                    {"REPORT_DATE": "2024-12-31", "PARENT_EQUITY": 90.0},
                    {"REPORT_DATE": "2025-12-31", "PARENT_EQUITY": 100.0},
                ],
                "cashflow": [{"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 999.0, "CONSTRUCT_LONG_ASSET": 1.0}],
            },
            {"code": "600036", "name": "样本银行", "market": "SH", "market_cap": 1_000.0},
            "BANK",
        )

        self.assertAlmostEqual(result["net_interest_margin"], 0.019)
        self.assertAlmostEqual(result["nonperforming_loan_ratio"], 0.01)
        self.assertAlmostEqual(result["loan_provision_coverage_proxy"], 3.0)
        self.assertEqual(result["net_interest_margin_years"], [2024, 2025])
        self.assertEqual(
            result["financial_sector_evidence"]["metrics"]["net_interest_margin"]["evidence_type"],
            "provider_standardized",
        )
        self.assertEqual(result["financial_sector_evidence"]["rule_version"], bs.FINANCIAL_SECTOR_RULE_VERSION)
        self.assertIsNone(result["free_cash_flow"])
        self.assertIsNone(result["roic"])

    def test_cashflow_is_sorted_and_fcf_subtracts_capex(self):
        fin = {
            "revenue_history": [
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 120},
                {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 100},
            ],
            "income_history": [
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 120, "PARENT_NETPROFIT": 12},
                {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10},
            ],
            "cashflow": [
                {"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 50, "CONSTRUCT_LONG_ASSET": 20},
                {"REPORT_DATE": "2023-12-31", "NETCASH_OPERATE": 10, "CONSTRUCT_LONG_ASSET": 3},
                {"REPORT_DATE": "2024-12-31", "NETCASH_OPERATE": 30, "CONSTRUCT_LONG_ASSET": 8},
            ],
        }
        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")
        self.assertEqual(result["oper_cf"], 50.0)
        self.assertEqual(result["free_cash_flow"], 30.0)
        self.assertEqual(result["fcf_history"], [7.0, 22.0, 30.0])

    def test_growth_review_uses_reported_goodwill_but_keeps_ma_evidence_partial(self):
        result = bs.extract_metrics(
            {
                "balance": [
                    {"REPORT_DATE": "2024-12-31", "GOODWILL": 0.0},
                    {"REPORT_DATE": "2025-12-31", "GOODWILL": 12.0},
                ],
                "cashflow_interim": [
                    {"REPORT_DATE": "2026-03-31", "OBTAIN_SUBSIDIARY_OTHER": -5.0},
                ],
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertEqual(result["goodwill_years"], [2024, 2025])
        self.assertEqual(result["goodwill_history"], [0.0, 12.0])
        self.assertEqual(result["interim_acquisition_cashflow"], -5.0)
        self.assertEqual(result["external_growth_evidence"]["status"], "partial")
        self.assertIn("逐笔并购收入占比", result["external_growth_evidence"]["missing"])
        self.assertEqual(
            result["external_growth_evidence"]["goodwill_source_records"][-1],
            {
                "report_date": "2025-12-31",
                "source_dataset": "东方财富年度资产负债表",
                "source_field": "GOODWILL",
            },
        )
        self.assertEqual(
            result["external_growth_evidence"]["acquisition_cashflow_source"],
            {
                "report_date": "2026-03-31",
                "source_dataset": "东方财富当期现金流量表",
                "source_field": "OBTAIN_SUBSIDIARY_OTHER",
            },
        )

    def test_growth_source_evidence_survives_metric_extraction_with_source_metadata(self):
        external_growth = {
            "status": "complete",
            "source": "上市公司年报及并购公告",
            "evidence_id": "acquisition-census:000001:20260718",
            "as_of": "2026-07-18",
            "security_code": "000001",
            "records": [{"report_date": "2025-12-31", "goodwill": 12.0}],
        }
        segment_growth = {
            "status": "complete",
            "source": "上市公司年报分部信息",
            "evidence_id": "segment-growth:000001:20260718",
            "as_of": "2026-07-18",
            "security_code": "000001",
            "segments": [{"name": "核心产品", "revenue_growth": 0.20}],
        }

        result = bs.extract_metrics(
            {
                "external_growth_evidence": external_growth,
                "segment_growth_sources": segment_growth,
            },
            {
                "code": "000001",
                "name": "样本",
                "source_trade_date": "2026-07-18",
            },
            "SOFTWARE",
        )

        self.assertEqual(result["external_growth_evidence"], external_growth)
        self.assertEqual(result["segment_growth_sources"], segment_growth)

    def test_untraceable_complete_growth_sources_are_not_treated_as_complete(self):
        result = bs.extract_metrics(
            {
                "external_growth_evidence": {"status": "complete"},
                "segment_growth_sources": {"status": "complete"},
            },
            {
                "code": "000001",
                "name": "样本",
                "source_trade_date": "2026-07-18",
            },
            "SOFTWARE",
        )

        self.assertNotEqual(result["external_growth_evidence"]["status"], "complete")
        self.assertNotEqual(result["segment_growth_sources"]["status"], "complete")

        metadata_only = bs.extract_metrics(
            {
                "external_growth_evidence": {
                    "status": "complete",
                    "source": "上市公司公告",
                    "evidence_id": "acquisition-census:000001:20260718",
                    "as_of": "2026-07-18",
                },
                "segment_growth_sources": {
                    "status": "complete",
                    "source": "上市公司年报",
                    "evidence_id": "segment-growth:000001:20260718",
                    "as_of": "2026-07-18",
                },
            },
            {
                "code": "000001",
                "name": "样本",
                "source_trade_date": "2026-07-18",
            },
            "SOFTWARE",
        )

        self.assertNotEqual(metadata_only["external_growth_evidence"]["status"], "complete")
        self.assertNotEqual(metadata_only["segment_growth_sources"]["status"], "complete")

    def test_q1_warning_is_computed_after_assignment_without_fake_yoy(self):
        fin = {
            "revenue_history": [
                {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 100},
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 120},
            ],
            "income_history": [
                {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10},
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 120, "PARENT_NETPROFIT": 12},
            ],
            "cashflow": [{"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 20, "CONSTRUCT_LONG_ASSET": 5}],
            "income_q1": [{"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": -1}],
            "cashflow_q1": [{"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": -2}],
        }
        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")
        self.assertTrue(result["q1_profit_warning"])
        self.assertTrue(result["ocf_q1_warning"])
        self.assertAlmostEqual(result["profit_1yr_change"], 0.20)
        self.assertEqual(result["q1_profit_basis"], "单季绝对值无同比基准")

    def test_explicit_type6_evidence_survives_metric_extraction_but_invalid_scores_do_not(self):
        valid = bs.extract_metrics(
            {
                "technology_score": 8.0,
                "technology_score_evidence": score_evidence("technology_score"),
                "technology_score_evidence_level": "primary",
                "business_model_score": 7.0,
                "business_model_score_evidence": score_evidence("business_model_score"),
                "business_model_score_evidence_level": "primary",
                "industry_early_stage_confirmed": True,
                "industry_early_stage_evidence": score_evidence("industry_early_stage_score"),
                "industry_early_stage_evidence_level": "primary",
                "position_size_pct": 0.0,
                "type6_portfolio_pct": 20.0,
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )
        invalid = bs.extract_metrics(
            {
                "technology_score": 11.0,
                "technology_score_evidence": score_evidence("technology_score"),
                "technology_score_evidence_level": "primary",
                "business_model_score": -1.0,
                "business_model_score_evidence": score_evidence("business_model_score"),
                "business_model_score_evidence_level": "primary",
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertEqual(valid["technology_score"], 8.0)
        self.assertEqual(valid["business_model_score"], 7.0)
        self.assertTrue(valid["industry_early_stage_confirmed"])
        self.assertEqual(valid["industry_early_stage_evidence_level"], "primary")
        self.assertEqual(valid["position_size_pct"], 0.0)
        self.assertEqual(valid["type6_portfolio_pct"], 20.0)
        self.assertIsNone(invalid["technology_score"])
        self.assertIsNone(invalid["business_model_score"])

    def test_debt_ratio_percent_and_parent_equity(self):
        fin = {
            "revenue_history": [{"REPORT_DATE": "2025", "TOTAL_OPERATE_INCOME": 100}],
            "income_history": [{"REPORT_DATE": "2025", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10}],
            "balance": [{"REPORT_DATE": "2025", "DEBT_ASSET_RATIO": 4.0, "TOTAL_EQUITY": 200, "PARENT_EQUITY": 100}],
        }
        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")
        self.assertEqual(result["debt_ratio"], 0.04)
        self.assertIsNone(result["roe"])
        self.assertEqual(result["roe_basis"], "missing_average_attributable_equity")

    def test_interest_debt_uses_all_ordinary_debt_components(self):
        fin = {
            "balance": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "SHORT_LOAN": 10.0,
                    "SHORT_BONDS_PAYABLE": 5.0,
                    "LONG_LOAN": 20.0,
                    "BONDS_PAYABLE": 30.0,
                    "NONCURRENT_LIAB_1YEAR": 4.0,
                    "LEASE_LIAB": 1.0,
                    "BORROW_FUNDS": 1000.0,
                    "CENTRAL_BANK_BORROWING": 1000.0,
                    "MONETARYFUNDS": 15.0,
                }
            ]
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")

        self.assertEqual(result["interest_debt"], 70.0)
        self.assertEqual(result["monetary_funds"], 15.0)

    def test_trend_growth_penalizes_deceleration(self):
        # 年增速由约50%降到10%，不得挑较高的长期CAGR。
        values = [100.0, 150.0, 195.0, 224.25, 246.675]
        recent = bs._compute_cagr(values[-3:])
        long = bs._compute_cagr(values)
        trend = bs._trend_adjusted_growth(values)
        self.assertLess(recent, long)
        self.assertEqual(trend, recent)

    def test_missing_calendar_years_are_annualized_not_counted_as_one_year(self):
        fin = {
            "revenue_history": [
                {"REPORT_DATE": "2021-12-31", "TOTAL_OPERATE_INCOME": 100},
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 146.41},
            ],
            "income_history": [
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 200, "PARENT_NETPROFIT": 20},
            ],
        }
        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")
        self.assertAlmostEqual(result["cagr_5yr"], 0.10, places=6)
        self.assertIsNone(result["growth_1yr"])
        self.assertAlmostEqual(result["net_margin"], 0.10)

    def test_roe_uses_average_attributable_equity_for_the_matching_year(self):
        fin = {
            "income_history": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "TOTAL_OPERATE_INCOME": 200,
                    "PARENT_NETPROFIT": 40,
                    "OPERATE_PROFIT": 50,
                },
            ],
            "balance": [
                {
                    "REPORT_DATE": "2024-12-31",
                    "PARENT_EQUITY": 100,
                    "TOTAL_EQUITY": 200,
                    "SHORT_LOAN": 0,
                    "MONETARYFUNDS": 0,
                },
                {
                    "REPORT_DATE": "2025-12-31",
                    "PARENT_EQUITY": 300,
                    "TOTAL_EQUITY": 400,
                    "SHORT_LOAN": 0,
                    "MONETARYFUNDS": 0,
                },
            ],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本", "market_cap": 1_000}, "SOFTWARE")

        self.assertEqual(result["roe"], 0.20)
        self.assertEqual(result["roe_basis"], "average_begin_end_attributable_equity")

    def test_cashflow_metrics_use_absolute_capex_recent_three_and_positive_profit_only(self):
        fin = {
            "income_history": [
                {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": -10},
            ],
            "cashflow": [
                {"REPORT_DATE": f"{year}-12-31", "NETCASH_OPERATE": ocf, "CONSTRUCT_LONG_ASSET": capex}
                for year, ocf, capex in (
                    (2021, 10, 1),
                    (2022, 100, 1),
                    (2023, 100, 1),
                    (2024, 100, 1),
                    (2025, 40, -5),
                )
            ],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")

        self.assertEqual(result["free_cash_flow"], 35.0)
        self.assertEqual(result["fcf_history"][-1], 35.0)
        self.assertEqual(result["ocf_3yr_change"], -0.60)
        self.assertIsNone(result["ocf_np_ratio"])

    def test_ocf_three_year_change_rejects_a_gap_in_the_latest_fiscal_years(self):
        fin = {
            "cashflow": [
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "NETCASH_OPERATE": ocf,
                    "CONSTRUCT_LONG_ASSET": 0,
                }
                for year, ocf in ((2021, 100), (2023, 120), (2024, 180))
            ],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")

        self.assertIsNone(result["ocf_3yr_change"])

    def test_margin_trajectory_uses_only_the_validated_recent_consecutive_window(self):
        fin = {
            "income_history": [
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_OPERATE_INCOME": 100,
                    "PARENT_NETPROFIT": profit,
                }
                for year, profit in (
                    (2010, 90),
                    (2011, 90),
                    (2023, 10),
                    (2024, 20),
                    (2025, 30),
                )
            ],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")

        self.assertAlmostEqual(result["margin_trajectory"], (0.25 - 0.15) / 0.15)

    def test_financial_metrics_do_not_construct_industrial_fcf_or_roic(self):
        fin = {
            "income_history": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "PARENT_NETPROFIT": 10,
                    "OPERATE_PROFIT": 12,
                    "TOTAL_OPERATE_INCOME": 100,
                },
            ],
            "cashflow": [
                {"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 100, "CONSTRUCT_LONG_ASSET": 1},
            ],
            "balance": [
                {"REPORT_DATE": "2024-12-31", "PARENT_EQUITY": 90},
                {"REPORT_DATE": "2025-12-31", "PARENT_EQUITY": 110},
            ],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "银行"}, "BANK")

        self.assertIsNone(result["oper_cf"])
        self.assertIsNone(result["free_cash_flow"])
        self.assertIsNone(result["ocf_np_ratio"])
        self.assertIsNone(result["roic"])
        self.assertIsNone(result["wacc"])

    def test_nonfinancial_roic_and_wacc_share_invested_capital_basis(self):
        fin = {
            "income_history": [
                {
                    "REPORT_DATE": "2025-12-31",
                    "PARENT_NETPROFIT": 20,
                    "OPERATE_PROFIT": 30,
                    "TOTAL_OPERATE_INCOME": 200,
                },
            ],
            "balance": [
                {"REPORT_DATE": "2024-12-31", "TOTAL_EQUITY": 100, "SHORT_LOAN": 40, "MONETARYFUNDS": 10},
                {"REPORT_DATE": "2025-12-31", "TOTAL_EQUITY": 140, "SHORT_LOAN": 60, "MONETARYFUNDS": 20},
            ],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本", "market_cap": 1_000}, "SOFTWARE")

        expected_roic = 30 * 0.75 / (((100 + 40 - 10) + (140 + 60 - 20)) / 2)
        self.assertEqual(result["roic"], expected_roic)
        self.assertIsNotNone(result["wacc"])
        self.assertEqual(result["roic_wacc_basis"], "NOPAT/平均投入资本代理")

    def test_peg_uses_comparable_positive_parent_profit_not_revenue_growth(self):
        def extract(profits):
            years = range(2023, 2026)
            fin = {
                "revenue_history": [
                    {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": revenue}
                    for year, revenue in zip(years, [100, 150, 225])
                ],
                "income_history": [
                    {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": revenue, "PARENT_NETPROFIT": profit}
                    for year, revenue, profit in zip(years, [100, 150, 225], profits)
                ],
            }
            return bs.extract_metrics(fin, {"code": "1", "name": "样本", "pe": 15}, "SOFTWARE")

        improving = extract([10, 12, 15])
        reversing = extract([10, 15, 12])
        self.assertIsNotNone(improving["peg"])
        self.assertEqual(improving["peg_basis"], "recent_comparable_parent_profit_trend")
        self.assertIsNone(reversing["peg"])

    def test_dynamic_interim_requires_exact_same_period_comparator(self):
        fin = {
            "income_interim": [
                {"REPORT_DATE": "2025-06-30", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10},
                {"REPORT_DATE": "2026-06-30", "TOTAL_OPERATE_INCOME": 120, "PARENT_NETPROFIT": 15},
            ],
            "cashflow_interim": [
                {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 20},
                {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": 30},
            ],
        }
        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")
        self.assertAlmostEqual(result["interim_revenue_yoy"], 0.20)
        self.assertAlmostEqual(result["interim_profit_yoy"], 0.50)
        self.assertAlmostEqual(result["interim_ocf_yoy"], 0.50)

        missing = bs.extract_metrics(
            {"income_interim": [fin["income_interim"][-1]]},
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )
        self.assertIsNone(missing["interim_profit_yoy"])
        self.assertEqual(missing["interim_yoy_basis"], "missing_same_period_comparator")

    def test_zero_base_exact_pairs_are_preserved_without_fabricated_yoy(self):
        financial = {
            "income_interim": [
                {"REPORT_DATE": "2025-03-31", "TOTAL_OPERATE_INCOME": 0.0, "PARENT_NETPROFIT": 0.0},
                {"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": -1.0, "PARENT_NETPROFIT": -1.0},
            ],
            "cashflow_interim": [
                {"REPORT_DATE": "2025-03-31", "NETCASH_OPERATE": 0.0},
                {"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": -1.0},
            ],
        }

        result = bs.extract_metrics(financial, {"code": "1", "name": "样本"}, "SOFTWARE")

        for metric in ("revenue", "profit", "ocf"):
            self.assertEqual(result[f"interim_prior_{metric}"], 0.0)
            self.assertEqual(result[f"interim_current_{metric}"], -1.0)
            self.assertEqual(result[f"interim_{metric}_pair_basis"], "same_period_yoy")
            self.assertIsNone(result[f"interim_{metric}_yoy"])
            self.assertEqual(result[f"interim_{metric}_yoy_basis"], "invalid_same_period_base")
            self.assertTrue(result[f"interim_{metric}_warning"])

    def test_same_period_negative_losses_improving_do_not_trigger_absolute_q1_veto(self):
        fin = {
            "income_interim": [
                {
                    "REPORT_DATE": "2025-03-31",
                    "PARENT_NETPROFIT": -20,
                    "TOTAL_OPERATE_INCOME": 80,
                },
                {
                    "REPORT_DATE": "2026-03-31",
                    "PARENT_NETPROFIT": -10,
                    "TOTAL_OPERATE_INCOME": 100,
                },
            ],
            "cashflow_interim": [
                {"REPORT_DATE": "2025-03-31", "NETCASH_OPERATE": -30},
                {"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": -10},
            ],
            "income_q1": [{"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": -10}],
            "cashflow_q1": [{"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": -10}],
        }

        result = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")

        self.assertEqual(result["interim_yoy_basis"], "same_period_yoy")
        self.assertAlmostEqual(result["interim_profit_yoy"], 0.5)
        self.assertAlmostEqual(result["interim_ocf_yoy"], 2 / 3)
        self.assertFalse(result["q1_profit_warning"])
        self.assertFalse(result["ocf_q1_warning"])
        self.assertEqual(bs._latest_period_deterioration(result)[0], 3)
        severity, reason = bs._latest_period_deterioration(result, allow_improving_losses=True)
        self.assertEqual(severity, 1)
        self.assertIn("现金流仍负但改善", reason)

    def test_same_period_revenue_that_remains_negative_is_always_flagged(self):
        result = bs.extract_metrics(
            {
                "income_interim": [
                    {"REPORT_DATE": "2025-03-31", "PARENT_NETPROFIT": 1.0, "TOTAL_OPERATE_INCOME": -2.0},
                    {"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": 2.0, "TOTAL_OPERATE_INCOME": -1.0},
                ]
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertEqual(result["interim_revenue_yoy"], 0.5)
        self.assertTrue(result["interim_revenue_warning"])

    def test_ocf_only_comparator_cannot_claim_complete_interim_yoy_evidence(self):
        fin = {
            "income_interim": [
                {
                    "REPORT_DATE": "2026-03-31",
                    "PARENT_NETPROFIT": 10,
                    "TOTAL_OPERATE_INCOME": 100,
                }
            ],
            "cashflow_interim": [
                {"REPORT_DATE": "2025-03-31", "NETCASH_OPERATE": 10},
                {"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": 20},
            ],
        }

        extracted = bs.extract_metrics(fin, {"code": "1", "name": "样本"}, "SOFTWARE")
        metric = base_metrics(
            interim_yoy_basis=extracted["interim_yoy_basis"],
            interim_revenue_yoy=extracted["interim_revenue_yoy"],
            interim_profit_yoy=extracted["interim_profit_yoy"],
            interim_ocf_yoy=extracted["interim_ocf_yoy"],
        )
        outcome = bs.score_type2_two_hot_one_cold(metric, benchmarks())

        self.assertEqual(extracted["interim_ocf_yoy_basis"], "same_period_yoy")
        self.assertEqual(extracted["interim_yoy_basis"], "missing_same_period_comparator")
        self.assertLessEqual(outcome[2]["2b"], 6.0)

    def test_naked_qualitative_scores_are_rejected_without_traceable_metadata(self):
        result = bs.extract_metrics(
            {"technology_score": 9.0, "management_alignment_score": 8.0},
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertIsNone(result["technology_score"])
        self.assertIsNone(result["management_alignment_score"])

    def test_non_production_evidence_levels_cannot_be_reinjected_as_scores(self):
        for level in ("partial", "missing", "unknown"):
            result = bs.extract_metrics(
                {
                    "technology_score": 9.0,
                    "technology_score_evidence": {
                        "source": "formula diagnostic",
                        "evidence_id": "technology-diagnostic-1",
                        "as_of": "2025-12-31",
                    },
                    "technology_score_evidence_level": level,
                },
                {"code": "1", "name": "样本"},
                "SOFTWARE",
            )

            self.assertIsNone(result["technology_score"])
            self.assertIsNone(result["technology_score_evidence"])

    def test_future_dated_qualitative_evidence_is_rejected(self):
        result = bs.extract_metrics(
            {
                "technology_score": 9.0,
                "technology_score_evidence": {
                    "source": "future-source",
                    "evidence_id": "future-1",
                    "as_of": "2999-01-01",
                },
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertIsNone(result["technology_score"])

    def test_qualitative_evidence_uses_shanghai_market_date(self):
        container = {
            "technology_score": 9.0,
            "technology_score_evidence_level": "primary",
            "technology_score_evidence": {
                "source": "market-date-source",
                "evidence_id": "market-date-1",
                "as_of": "2026-07-16",
            },
        }

        with patch.object(bs, "_shanghai_today", return_value=date(2026, 7, 16)):
            score, evidence = bs._normalise_score_evidence(container, "technology_score")
        self.assertEqual(score, 9.0)
        self.assertEqual(evidence["as_of"], "2026-07-16")

        with patch.object(bs, "_shanghai_today", return_value=date(2026, 7, 15)):
            score, evidence = bs._normalise_score_evidence(container, "technology_score")
        self.assertIsNone(score)
        self.assertIsNone(evidence)

    def test_qualitative_evidence_is_bound_to_the_security_and_expires(self):
        container = {
            "code": "000001",
            "source_trade_date": "2026-07-18",
            "technology_score": 9.0,
            "technology_score_evidence_level": "primary",
            "technology_score_evidence": {
                "source": "上市公司公告",
                "evidence_id": "technology:600519:20260718",
                "as_of": "2026-07-18",
            },
        }

        with patch.object(bs, "_shanghai_today", return_value=date(2026, 7, 20)):
            self.assertIsNone(bs._verified_score(container, "technology_score"))

            container["technology_score_evidence"] = {
                "source": "上市公司公告",
                "evidence_id": "technology:000001:20240101",
                "as_of": "2024-01-01",
            }
            self.assertIsNone(bs._verified_score(container, "technology_score"))

            container["technology_score_evidence"] = {
                "source": "上市公司公告",
                "evidence_id": "technology:000001:20260718",
                "as_of": "2026-07-18",
            }
            self.assertEqual(bs._verified_score(container, "technology_score"), 9.0)

    def test_qualitative_evidence_rejects_unknown_fields_controls_and_oversized_text(self):
        base = score_evidence("technology_score")
        for mutation in (
            {**base, "unknown": "x"},
            {**base, "summary": "unsafe\nformula"},
            {**base, "source": "x" * 201},
        ):
            score, evidence = bs._normalise_score_evidence(
                {"technology_score": 8.0, "technology_score_evidence": mutation},
                "technology_score",
            )
            self.assertIsNone(score)
            self.assertIsNone(evidence)

    def test_latest_period_warnings_bind_to_exact_prior_year_period(self):
        result = bs.extract_metrics(
            {
                "income_interim": [
                    {
                        "REPORT_DATE": "2025-06-30",
                        "TOTAL_OPERATE_INCOME": 100.0,
                        "PARENT_NETPROFIT": 10.0,
                    },
                    {
                        "REPORT_DATE": "2026-03-31",
                        "TOTAL_OPERATE_INCOME": -2.0,
                        "PARENT_NETPROFIT": -2.0,
                    },
                    {
                        "REPORT_DATE": "2026-06-30",
                        "TOTAL_OPERATE_INCOME": -1.0,
                        "PARENT_NETPROFIT": -1.0,
                    },
                ],
                "cashflow_interim": [
                    {"REPORT_DATE": "2025-06-30", "NETCASH_OPERATE": 20.0},
                    {"REPORT_DATE": "2026-03-31", "NETCASH_OPERATE": -5.0},
                    {"REPORT_DATE": "2026-06-30", "NETCASH_OPERATE": -1.0},
                ],
                "cashflow": [
                    {
                        "REPORT_DATE": f"{year}-12-31",
                        "NETCASH_OPERATE": ocf,
                        "CONSTRUCT_LONG_ASSET": capex,
                    }
                    for year, ocf, capex in ((2023, 20, 10), (2024, 30, 10), (2025, 40, 10))
                ],
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertTrue(result["interim_revenue_warning"])
        self.assertTrue(result["interim_profit_warning"])
        self.assertTrue(result["interim_ocf_warning"])
        self.assertEqual(result["interim_current_profit"], -1.0)
        self.assertEqual(result["interim_current_ocf"], -1.0)
        self.assertEqual(result["fcf_history"], [10.0, 20.0, 30.0])
        self.assertEqual(result["fcf_years"], [2023, 2024, 2025])

        without_comparator = bs.extract_metrics(
            {"income_interim": [{"REPORT_DATE": "2026-06-30", "PARENT_NETPROFIT": -1.0}]},
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )
        self.assertEqual(without_comparator["interim_current_profit"], -1.0)
        self.assertEqual(without_comparator["interim_profit_yoy_basis"], "missing_same_period_comparator")


class TestTypeRules(unittest.TestCase):
    def test_nonfinancial_dcf_requires_source_bound_strict_ttm_provenance(self):
        metrics = base_metrics()
        payload = complete_dcf_evidence()
        self.assertTrue(bs._valid_nonfinancial_dcf_evidence(metrics, payload))

        mutations = {
            "annual_basis": lambda value: value.update(valuation_input_basis="annual"),
            "revenue_basis": lambda value: value.update(base_revenue_basis="latest_annual"),
            "fcf_basis": lambda value: value.update(base_fcf_basis="annual_median"),
            "ttm_fcff_component": lambda value: value["ttm_fcff_evidence"]["components"].update(
                reconstructed_fcff=31.0
            ),
            "ttm_revenue_value": lambda value: value["ttm_revenue_evidence"].update(value=101.0),
            "latest_fcff": lambda value: value.update(latest_fcff=31.0),
            "recent_fcff": lambda value: value.update(recent_fcff=[10.0, 20.0, 31.0]),
            "recent_period": lambda value: value["recent_fcff_periods"][2].update(through_report_date="2026-06-30"),
            "normalisation_period": lambda value: value["fcf_normalisation_period"].update(
                period_set="three_annual_years"
            ),
            "undisclosed_base_cap": lambda value: value.update(base_fcf=19.0),
            "forged_adjustment": lambda value: value.update(
                base_fcf=19.0,
                base_fcf_adjustments=[{"kind": "invented_cap", "before": 20.0, "limit": 19.0, "after": 19.0}],
            ),
        }
        for name, mutate in mutations.items():
            altered = copy.deepcopy(payload)
            mutate(altered)
            with self.subTest(name=name):
                self.assertFalse(bs._valid_nonfinancial_dcf_evidence(metrics, altered))

        missing_source = dict(metrics)
        missing_source.pop("_ttm_cashflow_interim")
        self.assertFalse(bs._valid_nonfinancial_dcf_evidence(missing_source, payload))

    def test_transient_dcf_validation_cache_is_exact_object_bound(self):
        metrics = base_metrics()
        payload = complete_dcf_evidence()

        bs._prepare_dcf_validation_cache(metrics, payload)

        self.assertTrue(bs._valid_nonfinancial_dcf_evidence(metrics, payload))
        self.assertTrue(bs._valid_long_horizon_dcf_evidence(metrics, payload))
        altered = copy.deepcopy(payload)
        altered["base_fcf"] = float(altered["base_fcf"]) * 2.0
        self.assertFalse(bs._valid_nonfinancial_dcf_evidence(metrics, altered))

    def test_type1_low_roe_does_not_rewrite_price_depth_or_invent_a_1a_veto(self):
        m = complete_type1_metrics(roe=0.01)
        triggered, _, scores, reasons = bs.score_type1_dcf(m, complete_dcf_evidence(), benchmarks())
        self.assertEqual(scores["1a"], 9.5)
        self.assertTrue(triggered)
        self.assertNotIn("_veto", reasons)

    def test_type1_price_depth_uses_patch6_inclusive_ten_and_twenty_percent_boundaries(self):
        for price, expected in ((110.0, 3.5), (100.0, 5.5), (90.0, 7.5), (80.0, 7.5), (79.99, 9.5)):
            with self.subTest(price=price):
                result = complete_dcf_evidence(current_price=price)
                result["buy_zone_upper"] = 100.0
                result["sell_zone_lower"] = 160.0
                result["zone"] = "买入区" if price <= 100.0 else "观察区"
                outcome = bs.score_type1_dcf(
                    base_metrics(price=price, management_alignment_score=8.0),
                    result,
                    benchmarks(),
                )
                self.assertEqual(outcome[2]["1a"], expected)

    def test_type1_fcf_yield_uses_patch6_exact_band_boundaries(self):
        expected = {0.03: 3.0, 0.05: 5.0, 0.08: 7.0, 0.12: 8.0, 0.1201: 9.0}
        for fcf_yield, score in expected.items():
            with self.subTest(fcf_yield=fcf_yield):
                self.assertEqual(bs._score_type1_fcf_yield(fcf_yield), score)

    def test_type1_old_fcf_history_cannot_complete_the_current_value_trap_check(self):
        metric = complete_type1_metrics(
            fcf_history=[20.0, 25.0, 30.0],
            fcf_years=[2008, 2009, 2010],
        )

        triggered, _total, _scores, reasons = bs.score_type1_dcf(
            metric,
            complete_dcf_evidence(),
            benchmarks(),
        )

        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("价值陷阱", reasons["_missing"])

    def test_type1_observation_zone_never_triggers_buy_type(self):
        m = base_metrics(price=109.0, free_cash_flow=30.0, market_cap=100.0, management_alignment_score=8.0)
        triggered, total, scores, reasons = bs.score_type1_dcf(
            m, complete_dcf_evidence(current_price=109.0), benchmarks()
        )

        self.assertGreaterEqual(total, 7.0)
        self.assertEqual(scores["1a"], 3.5)
        self.assertFalse(triggered)
        self.assertIn("_condition", reasons)

    def test_type1_missing_governance_is_not_counted_as_management_alignment(self):
        missing = bs.score_type1_dcf(
            base_metrics(management_alignment_score=None),
            complete_dcf_evidence(),
            benchmarks(),
        )
        evidenced = bs.score_type1_dcf(
            base_metrics(management_alignment_score=8.0),
            complete_dcf_evidence(),
            benchmarks(),
        )
        self.assertEqual(evidenced[2]["1b"] - missing[2]["1b"], 2.0)
        self.assertIn("研究缺口", missing[3]["1b"])
        self.assertNotIn("估值折价", missing[3]["1d"])
        self.assertEqual(missing[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("价值陷阱", missing[3]["_missing"])
        self.assertIn("回归催化", missing[3]["_missing"])
        self.assertNotIn("_veto", missing[3])

    def test_type1_confirmed_price_veto_survives_missing_governance(self):
        outcome = bs.score_type1_dcf(
            base_metrics(price=200.0, management_alignment_score=None),
            complete_dcf_evidence(current_price=200.0),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_VETOED)
        self.assertEqual(outcome[3]["_evidence"], "incomplete")
        self.assertEqual(outcome[3]["_veto"], "买入区深度不足")
        self.assertIn("价值陷阱", outcome[3]["_missing"])
        self.assertIn("回归催化", outcome[3]["_missing"])

    def test_financial_type1_requires_a_justified_pb_result(self):
        for payload in (
            {"buy_zone_upper": 100.0},
            {"buy_zone_upper": 100.0, "_pb_valuation": True},
        ):
            with self.subTest(payload=payload):
                outcome = bs.score_type1_dcf(base_metrics(industry="BANK"), payload, benchmarks())
                self.assertFalse(outcome[0])
                self.assertEqual(outcome[1], 0.0)
                self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
                self.assertNotIn("_veto", outcome[3])

    def test_financial_type1_rejects_tampered_pb_formula_or_attribution(self):
        financial = base_metrics(industry="BANK")
        for field, value in (("code", "000002"), ("current_price", 49.0)):
            payload = complete_pb_evidence()
            payload[field] = value
            with self.subTest(field=field):
                self.assertFalse(bs.score_type1_dcf(financial, payload, benchmarks())[0])
        tampered_formula = complete_pb_evidence()
        tampered_formula["params"]["neutral"]["pb_lower"] *= 1.1
        self.assertFalse(bs.score_type1_dcf(financial, tampered_formula, benchmarks())[0])

    def test_financial_type1_confirmed_price_veto_survives_missing_regulatory_trap_evidence(self):
        outcome = bs.score_type1_dcf(
            base_metrics(industry="BANK", price=500.0),
            complete_pb_evidence(current_price=500.0),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_VETOED)
        self.assertEqual(outcome[3]["_evidence"], "incomplete")
        self.assertEqual(outcome[3]["_veto"], "买入区深度不足")
        self.assertIn("价值陷阱", outcome[3]["_missing"])

    def test_type1_uses_fcf_not_ocf_and_type4_requires_terminal_value(self):
        m = base_metrics(oper_cf=100.0, free_cash_flow=-10.0)
        _, _, scores1, _ = bs.score_type1_dcf(m, complete_dcf_evidence(), benchmarks())
        _, _, scores4, _ = bs.score_type4_long_runway(m, benchmarks())
        self.assertEqual(scores1["1c"], 0.0)
        self.assertEqual(scores4["4f"], 2.0)

    def test_type1_nonfinancial_rejects_partial_or_untraceable_dcf_payloads(self):
        m = base_metrics()
        partial_payloads = (
            {"zone": "买入区"},
            {"buy_zone_upper": 100.0},
            {"buy_zone_upper": 100.0, "current_price": 50.0},
        )

        for payload in partial_payloads:
            with self.subTest(payload=payload):
                triggered, total, scores, reasons = bs.score_type1_dcf(m, payload, benchmarks())
                self.assertFalse(triggered)
                self.assertEqual(total, 0.0)
                self.assertTrue(all(score == 0.0 for score in scores.values()))
                self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
                self.assertNotIn("_veto", reasons)

        wrong_company = complete_dcf_evidence()
        wrong_company["code"] = "000002"
        self.assertFalse(bs.score_type1_dcf(m, wrong_company, benchmarks())[0])

    def test_type2_adjustment_never_jumps_total_to_seven(self):
        m = base_metrics(
            pe=32.5,
            peg=2.0,
            revenue_values=[100, 106, 120],
            margin_history=[0.10, 0.11, 0.12],
            net_profit_history=[10, 11, 12],
            ocf_np_ratio=0.5,
            profit_1yr_change=0.09,
        )
        bench = benchmarks(neutral_benchmark=0.15, median_pe=25.0)
        triggered, total, scores, _ = bs.score_type2_two_hot_one_cold(m, bench)
        self.assertEqual(scores["2d"], 4.0)
        self.assertEqual(total, bs._weighted_total(scores, bs.TYPE_WEIGHTS["type2"]))
        self.assertFalse(triggered)

    def test_type7_public_total_preserves_the_strict_three_decimal_boundary(self):
        scores = {"7a": 7.001, "7b": 7.001, "7c": 7.001}
        reasons = {key: "严格阈值边界" for key in scores}

        triggered, total, published_scores, published_reasons = bs._finish(
            "type7",
            scores,
            reasons,
            extra_condition=True,
        )

        self.assertTrue(triggered)
        self.assertEqual(total, 7.001)
        self.assertEqual(published_scores, scores)
        self.assertEqual(published_reasons["_status"], bs.STATUS_TRIGGERED)
        self.assertEqual(
            bs._weighted_total(scores, bs.TYPE_WEIGHTS["type7"], decimals=3, total_decimals=3),
            7.001,
        )

    def test_type2_significant_overvaluation_cannot_trigger_despite_three_strong_cycles(self):
        m = base_metrics(
            peg=4.0,
            revenue_values=[100, 110, 140],
            margin_history=[0.10, 0.12, 0.16],
            net_profit_history=[10, 15, 25],
            ocf_np_ratio=1.2,
            market_coldness_score=10.0,
        )

        triggered, total, scores, reasons = bs.score_type2_two_hot_one_cold(
            m,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertGreaterEqual(total, 7.0)
        self.assertEqual(scores["2a"], 10.0)
        self.assertEqual(scores["2c"], 8.0)
        self.assertLessEqual(scores["2d"], 2.0)
        self.assertFalse(triggered)
        self.assertIn("_condition", reasons)

    def test_type2_strong_cycle_adjustment_allows_four_point_valuation_without_score_jump(self):
        m = base_metrics(
            peg=2.0,
            revenue_values=[100, 110, 140],
            margin_history=[0.10, 0.12, 0.16],
            net_profit_history=[10, 15, 25],
            ocf_np_ratio=1.2,
            market_coldness_score=10.0,
        )

        triggered, total, scores, reasons = bs.score_type2_two_hot_one_cold(
            m,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertEqual(scores["2d"], 4.0)
        self.assertEqual(total, bs._weighted_total(scores, bs.TYPE_WEIGHTS["type2"]))
        self.assertTrue(triggered)
        self.assertIn("_adjustment", reasons)

    def test_type2_uses_patch6_average_hot_dimension_veto(self):
        m = base_metrics(
            revenue_values=[100, 80, 120, 160],
            margin_history=[0.05, 0.03, 0.08, 0.12],
            net_profit_history=[10, 4, 12, 30],
            ocf_np_ratio=1.2,
            peg=0.5,
            market_coldness_score=10,
        )
        bench = benchmarks(median_cagr=-0.012, median_cagr_count=50, median_pe=25)

        triggered, total, scores, reasons = bs.score_type2_two_hot_one_cold(m, bench)

        self.assertGreaterEqual(total, 7.0)
        self.assertLessEqual(scores["2a"], 2.0)
        self.assertGreater((scores["2a"] + scores["2b"]) / 2, 4.0)
        self.assertTrue(triggered)
        self.assertNotIn("_veto", reasons)

    def test_type2_patch6_average_boundary_uses_the_displayed_one_decimal_scores(self):
        metric = base_metrics(
            revenue_values=[100, 110, 120],
            margin_history=[0.10, 0.10, 0.10],
            net_profit_history=[10, 11, 12],
            ocf_np_ratio=1.2,
            interim_revenue_yoy=0.0,
            market_coldness_score=10.0,
            peg=0.5,
        )

        exact = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.0, median_cagr_count=20),
        )
        displayed_above = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.002, median_cagr_count=20),
        )

        self.assertEqual((exact[2]["2a"] + exact[2]["2b"]) / 2, 4.0)
        self.assertEqual(exact[3]["_status"], bs.STATUS_VETOED)
        self.assertIn("平均须>4", exact[3]["_veto"])
        self.assertGreater((displayed_above[2]["2a"] + displayed_above[2]["2b"]) / 2, 4.0)
        self.assertNotIn("_veto", displayed_above[3])

    def test_type2_valuation_boundary_uses_the_displayed_one_decimal_score(self):
        metric = base_metrics(
            peg=None,
            pb=2.467,
            revenue_values=[100, 110, 140],
            margin_history=[0.10, 0.12, 0.16],
            net_profit_history=[10, 15, 25],
            ocf_np_ratio=1.2,
            market_coldness_score=10.0,
        )

        outcome = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50, median_pb=2.0, median_pb_count=50),
        )
        payload = {
            "triggered": outcome[0],
            "total": outcome[1],
            "sub_scores": outcome[2],
            "reasons": outcome[3],
            "status": outcome[3]["_status"],
            "veto": bool(outcome[3].get("_veto")),
        }

        self.assertEqual(outcome[2]["2d"], 5.0)
        self.assertTrue(outcome[0])
        self.assertTrue(bs.replay_buy_decision("type2", payload)["potentially_triggerable"])

    def test_type2_industry_growth_score_is_continuous_at_five_percent(self):
        low = benchmarks(median_cagr=0.05 - 1e-6, median_cagr_count=50)
        high = benchmarks(median_cagr=0.05 + 1e-6, median_cagr_count=50)

        low_score = bs.score_type2_two_hot_one_cold(base_metrics(), low)[2]["2a"]
        high_score = bs.score_type2_two_hot_one_cold(base_metrics(), high)[2]["2a"]

        self.assertLess(abs(high_score - low_score), 0.01)

    def test_type2_market_coldness_and_valuation_use_independent_evidence(self):
        bench = benchmarks(median_pe=25.0, median_pb=2.0)
        _, _, pe_low, _ = bs.score_type2_two_hot_one_cold(
            base_metrics(pe=10.0, pb=2.0, peg=None, market_coldness_score=7.0), bench
        )
        _, _, pe_high, _ = bs.score_type2_two_hot_one_cold(
            base_metrics(pe=50.0, pb=2.0, peg=None, market_coldness_score=7.0), bench
        )
        _, _, value_low, _ = bs.score_type2_two_hot_one_cold(
            base_metrics(pe=10.0, pb=1.0, peg=None, market_coldness_score=7.0), bench
        )
        _, _, value_high, _ = bs.score_type2_two_hot_one_cold(
            base_metrics(pe=10.0, pb=4.0, peg=None, market_coldness_score=7.0), bench
        )
        _, _, missing, missing_reasons = bs.score_type2_two_hot_one_cold(
            base_metrics(pe=10.0, pb=1.0, peg=None, market_coldness_score=None), bench
        )

        self.assertEqual(pe_low["2c"], pe_high["2c"])
        self.assertEqual(pe_low["2d"], pe_high["2d"])
        self.assertEqual(value_low["2c"], value_high["2c"])
        self.assertGreater(value_low["2d"], value_high["2d"])
        self.assertEqual(missing["2c"], 0.0)
        self.assertEqual(missing_reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("PE", missing_reasons["2c"])

    def test_type2_deducts_for_fragmented_competition_via_industry_hhi(self):
        # 补丁7 情况二附加项：竞争格局恶劣扣1-2分。行业营收 HHI<0.20 扣1分，
        # <0.10 扣2分。
        bench = benchmarks(median_cagr=0.20, median_cagr_count=50)
        concentrated = base_metrics(revenue_values=[100.0, 100.0, 100.0, 100.0, 100.0])
        concentrated["_quantitative_peer_context"] = {
            "target_excluded": True,
            "aggregate_revenue_cagr": 0.20,
            "aggregate_revenue_cagr_count": 50,
            "aggregate_revenue_coverage": 0.9,
            "industry_revenue_hhi": 0.55,
        }
        fragmented = base_metrics(revenue_values=[100.0, 100.0, 100.0, 100.0, 100.0])
        fragmented["_quantitative_peer_context"] = {
            "target_excluded": True,
            "aggregate_revenue_cagr": 0.20,
            "aggregate_revenue_cagr_count": 50,
            "aggregate_revenue_coverage": 0.9,
            "industry_revenue_hhi": 0.05,
        }
        moderate = base_metrics(revenue_values=[100.0, 100.0, 100.0, 100.0, 100.0])
        moderate["_quantitative_peer_context"] = {
            "target_excluded": True,
            "aggregate_revenue_cagr": 0.20,
            "aggregate_revenue_cagr_count": 50,
            "aggregate_revenue_coverage": 0.9,
            "industry_revenue_hhi": 0.12,
        }
        high_score = bs.score_type2_two_hot_one_cold(concentrated, bench)[2]["2a"]
        moderate_score = bs.score_type2_two_hot_one_cold(moderate, bench)[2]["2a"]
        low_score = bs.score_type2_two_hot_one_cold(fragmented, bench)[2]["2a"]
        self.assertGreater(high_score, moderate_score)
        self.assertGreater(moderate_score, low_score)
        self.assertAlmostEqual(high_score - moderate_score, 1.0, places=9)
        self.assertAlmostEqual(moderate_score - low_score, 1.0, places=9)

    def test_type1_dividend_catalyst_adds_to_the_financial_proxy(self):
        # 补丁7 情况一 1d 弱催化剂(回购/分红)：持续分红是价格回归动力。
        plain = base_metrics()
        with_dividend = base_metrics(trailing_cash_per_share=2.0)  # 股息率 4%（price=50）
        _, _, plain_scores, plain_reasons = bs.score_type1_dcf(plain, complete_dcf_evidence(), benchmarks())
        _, _, dividend_scores, dividend_reasons = bs.score_type1_dcf(
            with_dividend, complete_dcf_evidence(), benchmarks()
        )
        self.assertGreater(dividend_scores["1d"], plain_scores["1d"])
        self.assertIn("分红率", dividend_reasons["1d"])

    def test_type2_rejects_market_coldness_from_a_different_trading_session(self):
        metric = base_metrics(
            source_trade_date="2026-07-15",
            market_coldness_score=10.0,
            market_coldness_score_evidence=score_evidence(
                "market_coldness_score",
                as_of="2025-07-15",
            ),
        )

        triggered, _total, scores, reasons = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertFalse(triggered)
        self.assertEqual(scores["2c"], 0.0)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_type2_rejects_same_session_derived_proxy_without_replayable_components(self):
        metric = base_metrics(
            source_trade_date="2026-07-15",
            market_coldness_score=8.0,
            market_coldness_score_evidence_level="derived_proxy",
            market_coldness_score_evidence={
                "source": "任意量价代理",
                "evidence_id": "forged:000001:20260715",
                "as_of": "2026-07-15",
                "summary": "同日伪造分数",
            },
        )

        triggered, _total, scores, reasons = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertFalse(triggered)
        self.assertEqual(scores["2c"], 0.0)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_type2_pb_fallback_requires_a_real_peer_sample_count(self):
        triggered, _total, scores, reasons = bs.score_type2_two_hot_one_cold(
            base_metrics(peg=None, pb=1.0),
            benchmarks(median_pb=2.0, median_pb_count=None),
        )

        self.assertFalse(triggered)
        self.assertEqual(scores["2d"], 2.0)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("估值", reasons["_missing"])

    def test_type2_missing_company_chain_is_not_a_complete_zero_or_veto(self):
        metric = base_metrics(
            revenue_values=[100.0, 120.0],
            revenue_years=[2024, 2025],
            net_profit_history=[10.0, 12.0],
            net_profit_years=[2024, 2025],
            margin_history=[0.10, 0.12],
            margin_years=[2024, 2025],
            listing_date=None,
            source_trade_date="2026-07-22",
            market_coldness_score=10.0,
            peg=0.5,
        )

        triggered, _total, _scores, reasons = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(reasons["_evidence"], "incomplete")
        self.assertIn("公司拐点", reasons["_missing"])
        self.assertNotIn("_veto", reasons)

    def test_type2_recent_listing_uses_explicit_short_history_contract(self):
        two_year = base_metrics(
            listing_date="2024-01-10",
            source_trade_date="2026-07-22",
            revenue_values=[100.0, 120.0],
            revenue_years=[2024, 2025],
            net_profit_history=[10.0, 12.0],
            net_profit_years=[2024, 2025],
            margin_history=[0.10, 0.12],
            margin_years=[2024, 2025],
        )
        quarterly = base_metrics(
            listing_date="2026-01-10",
            source_trade_date="2026-07-22",
            revenue_values=[],
            revenue_years=[],
            net_profit_history=[],
            net_profit_years=[],
            margin_history=[],
            margin_years=[],
        )

        self.assertEqual(bs._type2_company_turn_evidence(two_year), (True, "上市后2年连续财务数据"))
        self.assertEqual(bs._type2_company_turn_evidence(quarterly), (True, "上市后同口径季报同比"))

    def test_type2_missing_valuation_is_diagnostic_not_a_measured_two_point_score(self):
        outcome = bs.score_type2_two_hot_one_cold(
            base_metrics(
                peg=None,
                pb=None,
                market_coldness_score=10.0,
            ),
            benchmarks(median_cagr=0.50, median_cagr_count=50, median_pb=2.0),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[2]["2d"], 2.0)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(outcome[3]["_evidence"], "incomplete")
        self.assertIn("估值", outcome[3]["_missing"])
        self.assertIn("仅供诊断", outcome[3]["_score_quality"])

    def test_current_industry_contraction_is_not_replaced_by_long_term_story_growth(self):
        bench = benchmarks(
            median_cagr=-0.08,
            median_cagr_count=50,
            neutral_benchmark=0.20,
        )

        _, _, type2_scores, type2_reasons = bs.score_type2_two_hot_one_cold(
            base_metrics(growth_1yr=0.50, cagr_3yr=0.50), bench
        )
        _, _, type6_scores, type6_reasons = bs.score_type6_vc(base_metrics(net_profit=-1.0, net_margin=-0.01), bench)

        self.assertEqual(type2_scores["2a"], 1.0)
        self.assertIn("-8.0%", type2_reasons["2a"])
        self.assertEqual(type6_scores["6a"], 1.0)
        self.assertIn("-8.0%", type6_reasons["6a"])

    def test_current_industry_growth_requires_a_minimum_cross_section_sample(self):
        bench = benchmarks(median_cagr=0.50, median_cagr_count=4)

        _, _, type2_scores, _ = bs.score_type2_two_hot_one_cold(base_metrics(), bench)
        _, _, type6_scores, type6_reasons = bs.score_type6_vc(base_metrics(net_profit=-1.0, net_margin=-0.01), bench)

        self.assertEqual(type2_scores["2a"], 2.0)
        self.assertEqual(type6_scores["6a"], 0.0)
        self.assertEqual(type6_reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_default_industry_never_inherits_cross_market_heat_or_valuation(self):
        default = base_metrics(industry="DEFAULT", market_coldness_score=None, peg=None)
        bench = {
            "DEFAULT": {"median_cagr": 0.9, "median_cagr_count": 100, "median_pe": 100, "median_pb": 10},
            "ALL": {"median_cagr": 0.8, "median_cagr_count": 100, "median_pe": 100, "median_pb": 10},
        }

        type2 = bs.score_type2_two_hot_one_cold(default, bench)
        type6 = bs.score_type6_vc(
            base_metrics(industry="DEFAULT", market_coldness_score=None, peg=None, net_profit=-1.0, net_margin=-0.01),
            bench,
        )

        self.assertEqual(type2[2]["2a"], 2.0)
        self.assertEqual(type2[2]["2c"], 0.0)
        self.assertEqual(type2[2]["2d"], 2.0)
        self.assertFalse(type2[0])
        self.assertEqual(type6[2]["6a"], 0.0)
        self.assertEqual(type6[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_type2_q1_cash_warning_caps_company_turn(self):
        m = base_metrics(ocf_q1_warning=True)
        _, _, scores, _ = bs.score_type2_two_hot_one_cold(m, benchmarks())
        self.assertLessEqual(scores["2b"], 3.0)

    def test_type2_latest_same_period_profit_decline_caps_company_score_without_extra_veto(self):
        for decline, expected_cap in ((-0.01, 6.0), (-0.20, 4.0), (-0.499, 4.0)):
            with self.subTest(decline=decline):
                m = base_metrics(
                    interim_profit_yoy=decline,
                    interim_current_profit=10.0 * (1.0 + decline),
                    interim_prior_profit=10.0,
                    interim_revenue_yoy=0.08,
                    interim_ocf_yoy=0.10,
                    market_coldness_score=10.0,
                    peg=0.8,
                )
                triggered, _, scores, reasons = bs.score_type2_two_hot_one_cold(
                    m,
                    benchmarks(median_cagr=0.25, median_cagr_count=50),
                )

                self.assertLessEqual(scores["2b"], expected_cap)
                self.assertTrue(triggered)
                self.assertEqual(reasons["_status"], bs.STATUS_TRIGGERED)
                self.assertNotIn("_condition", reasons)

    def test_type2_invalid_zero_base_and_current_loss_cannot_confirm_company_turn(self):
        metric = base_metrics(
            revenue_values=[100, 110, 140],
            margin_history=[0.10, 0.12, 0.16],
            net_profit_history=[10, 15, 25],
            ocf_np_ratio=1.2,
            market_coldness_score=10.0,
            peg=0.5,
            interim_current_profit=-1.0,
            interim_prior_profit=0.0,
            interim_profit_yoy=None,
            interim_profit_yoy_basis="invalid_same_period_base",
            interim_profit_pair_basis="same_period_yoy",
            interim_profit_warning=True,
            interim_yoy_basis="missing_same_period_comparator",
        )

        triggered, total, _scores, reasons = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertGreaterEqual(total, 7.0)
        self.assertTrue(triggered)
        self.assertLessEqual(_scores["2b"], 2.0)
        self.assertEqual(reasons["_status"], bs.STATUS_TRIGGERED)

    def test_type2_exact_pair_handles_zero_and_negative_bases_without_fabricating_yoy(self):
        common = {
            "revenue_values": [100, 110, 140],
            "margin_history": [0.10, 0.12, 0.16],
            "net_profit_history": [10, 15, 25],
            "ocf_np_ratio": 1.2,
            "market_coldness_score": 10.0,
            "peg": 0.8,
        }
        bench = benchmarks(median_cagr=0.50, median_cagr_count=50)

        zero_to_profit = base_metrics(
            **common,
            interim_current_profit=1.0,
            interim_prior_profit=0.0,
            interim_profit_yoy=None,
            interim_profit_yoy_basis="invalid_same_period_base",
            interim_profit_pair_basis="same_period_yoy",
            interim_yoy_basis="missing_same_period_comparator",
        )
        outcome = bs.score_type2_two_hot_one_cold(zero_to_profit, bench)
        self.assertTrue(outcome[0])
        self.assertLessEqual(outcome[2]["2b"], 6.0)
        self.assertIn("零基数无可比同比", outcome[3]["2b"])
        self.assertNotIn("_condition", outcome[3])

        narrowing_loss = base_metrics(
            **common,
            interim_current_profit=-1.0,
            interim_prior_profit=-2.0,
            interim_profit_yoy=0.50,
            interim_profit_yoy_basis="same_period_yoy",
            interim_profit_pair_basis="same_period_yoy",
        )
        self.assertTrue(bs.score_type2_two_hot_one_cold(narrowing_loss, bench)[0])

        missing_comparator = base_metrics(
            **common,
            interim_current_profit=1.0,
            interim_prior_profit=None,
            interim_profit_yoy=None,
            interim_profit_yoy_basis="missing_same_period_comparator",
            interim_profit_pair_basis="missing_same_period_comparator",
            interim_yoy_basis="missing_same_period_comparator",
        )
        missing_outcome = bs.score_type2_two_hot_one_cold(missing_comparator, bench)
        self.assertTrue(missing_outcome[0])
        self.assertIn("缺最新同口径报告期", missing_outcome[3]["2b"])

    def test_type2_zero_to_negative_revenue_or_ocf_is_a_company_turn_veto(self):
        bench = benchmarks(median_cagr=0.50, median_cagr_count=50)
        for metric in ("revenue", "ocf"):
            with self.subTest(metric=metric):
                values = {
                    f"interim_current_{metric}": -1.0,
                    f"interim_prior_{metric}": 0.0,
                    f"interim_{metric}_yoy": None,
                    f"interim_{metric}_yoy_basis": "invalid_same_period_base",
                    f"interim_{metric}_pair_basis": "same_period_yoy",
                    f"interim_{metric}_warning": True,
                }
                outcome = bs.score_type2_two_hot_one_cold(
                    base_metrics(
                        revenue_values=[100, 110, 140],
                        margin_history=[0.10, 0.12, 0.16],
                        net_profit_history=[10, 15, 25],
                        ocf_np_ratio=1.2,
                        market_coldness_score=10.0,
                        peg=0.5,
                        **values,
                    ),
                    bench,
                )
                self.assertTrue(outcome[0])
                self.assertLessEqual(outcome[2]["2b"], 2.0)

    def test_type2_still_negative_revenue_cannot_be_misread_as_positive_growth(self):
        outcome = bs.score_type2_two_hot_one_cold(
            base_metrics(
                revenue_values=[100, 110, 140],
                margin_history=[0.10, 0.12, 0.16],
                net_profit_history=[10, 15, 25],
                ocf_np_ratio=1.2,
                market_coldness_score=10.0,
                peg=0.5,
                interim_current_revenue=-1.0,
                interim_prior_revenue=-2.0,
                interim_revenue_yoy=0.50,
                interim_revenue_yoy_basis="same_period_yoy",
                interim_revenue_pair_basis="same_period_yoy",
            ),
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )

        self.assertTrue(outcome[0])
        self.assertLessEqual(outcome[2]["2b"], 2.0)
        self.assertIn("最新同口径营收为负", outcome[3]["2b"])

    def test_type2_market_not_cold_is_vetoed(self):
        triggered, _, scores, reasons = bs.score_type2_two_hot_one_cold(
            base_metrics(market_coldness_score=2.0), benchmarks(median_pe=25)
        )
        self.assertLessEqual(scores["2c"], 3.0)
        self.assertFalse(triggered)
        self.assertIn("_veto", reasons)

    def test_type3_uses_real_roic_wacc_only(self):
        m = base_metrics(roic=None, wacc=None)
        triggered, _, scores, reasons = bs.score_type3_sustainable_growth(m, benchmarks())
        self.assertFalse(triggered)
        self.assertEqual(scores["3c"], 5.0)
        self.assertIn("投入回报率", reasons["3c"])
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("_veto", reasons)
        m["roic"], m["wacc"] = 0.20, 0.08
        m["roic_wacc_basis"] = "NOPAT/平均投入资本代理"
        _, _, scores, _ = bs.score_type3_sustainable_growth(m, benchmarks())
        self.assertEqual(scores["3c"], 9.5)

        m["roic_wacc_basis"] = "mismatched_parent_equity_basis"
        triggered, _, scores, reasons = bs.score_type3_sustainable_growth(m, benchmarks())
        self.assertFalse(triggered)
        self.assertEqual(scores["3c"], 5.0)
        self.assertIn("口径", reasons["3c"])
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("_veto", reasons)

    def test_type3_can_trigger_from_complete_traceable_automatic_evidence(self):
        external_growth = {
            "status": "complete",
            "source": "上市公司年报及并购公告",
            "evidence_id": "acquisition-census:000001:20260718",
            "as_of": "2026-07-18",
            "security_code": "000001",
            "contract_scope": "aggregate_proxy_not_transaction_census",
            "coverage_year_count": 5,
            "aggregate_acquisition_cash_to_revenue": 0.0,
            "positive_goodwill_additions_to_revenue": 0.0,
            "goodwill_to_revenue_latest": 0.0,
            "goodwill_change_to_revenue": 0.0,
            "records": [
                {
                    "year": year,
                    "report_date": f"{year}-12-31",
                    "revenue": float(100 + (year - 2021) * 20),
                    "goodwill": 0.0,
                    "acquisition_cash": 0.0,
                }
                for year in range(2021, 2026)
            ],
        }
        segment_growth = {
            "status": "complete",
            "source": "上市公司年报分部信息",
            "evidence_id": "segment-growth:000001:20260718",
            "as_of": "2026-07-18",
            "security_code": "000001",
            "history_years": [2021, 2022, 2023, 2024, 2025],
            "growth_source_count": 3,
            "effective_growth_source_count": 3.0,
            "positive_growth_share": 1.0,
            "revenue_hhi": 1.0 / 3.0,
            "matched_latest_share": 1.0,
            "segments": [
                {
                    "item_name": name,
                    "first_year": 2021,
                    "latest_year": 2025,
                    "first_revenue": 10.0,
                    "latest_revenue": 30.0,
                    "latest_revenue_share": 1.0 / 3.0,
                    "cagr": 3**0.25 - 1,
                }
                for name in ("核心产品", "海外业务", "新业务")
            ],
        }
        extracted = bs.extract_metrics(
            {
                "external_growth_evidence": external_growth,
                "segment_growth_sources": segment_growth,
            },
            {
                "code": "000001",
                "name": "目标公司",
                "source_trade_date": "2026-07-18",
            },
            "SOFTWARE",
        )

        def rich_metric(code):
            metric = base_metrics(
                code=code,
                industry="SOFTWARE",
                source_trade_date="2026-07-17",
                revenue_values=[100.0, 125.0, 156.0, 195.0],
                revenue_years=[2022, 2023, 2024, 2025],
                cagr_3yr=0.20,
                cagr_5yr=0.20,
                trend_growth=0.20,
                growth_slope=0.01,
                growth_consistency=0.10,
                revenue_latest=195.0,
                net_profit_history=[20.0, 27.0, 36.0, 48.0],
                net_profit_years=[2022, 2023, 2024, 2025],
                net_profit=48.0,
                total_assets_history=[100.0, 110.0, 120.0, 130.0],
                total_assets_years=[2022, 2023, 2024, 2025],
                indicator_roic_history=[0.20, 0.22, 0.24, 0.25],
                indicator_roic_years=[2022, 2023, 2024, 2025],
                gross_margin_history=[0.45, 0.46, 0.47, 0.48],
                gross_margin_years=[2022, 2023, 2024, 2025],
                gross_margin=0.48,
                gross_margin_cv=0.03,
                fcf_history=[15.0, 21.0, 29.0, 40.0],
                fcf_years=[2022, 2023, 2024, 2025],
                free_cash_flow=40.0,
                capex_history=[5.0, 6.0, 7.0, 8.0],
                capex_years=[2022, 2023, 2024, 2025],
                capex=8.0,
                adjusted_profit_ratio=0.98,
                share_dilution_1yr=0.0,
                interest_bearing_debt_ratio=0.05,
                margin_trajectory=0.05,
                roic=0.25,
                wacc=0.08,
                roic_wacc_basis="Eastmoney年度ROIC/公司资本结构WACC",
                market_coldness_score=8.0,
                peg=0.8,
            )
            metric["financial_indicator_as_of"] = "2025-12-31"
            metric["source_trade_date"] = "2026-07-17"
            return metric

        target = rich_metric("000001")
        target["external_growth_evidence"] = extracted["external_growth_evidence"]
        target["segment_growth_sources"] = extracted["segment_growth_sources"]
        target["_type3_growth_validation_token"] = bs.TYPE3_GROWTH_VALIDATION_TOKEN
        peers = [rich_metric(f"P{index:02d}") for index in range(MIN_SECTOR_COMPANIES)]
        universe = [*peers, target]

        bs.enrich_metrics(
            universe,
            bs.build_sector_benchmarks(universe),
            target_codes={"000001"},
        )
        outcome = bs.score_type3_sustainable_growth(target, bs.build_sector_benchmarks(universe))

        self.assertEqual(target["growth_quality_score_evidence_level"], "derived_proxy")
        self.assertEqual(target["growth_sustainability_score_evidence_level"], "derived_proxy")
        self.assertLessEqual(target["growth_sustainability_score"], 8.0)
        self.assertTrue(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_TRIGGERED)

    def test_type3_negative_roic_spread_is_a_low_score_not_an_extra_patch6_veto(self):
        triggered, _total, scores, reasons = bs.score_type3_sustainable_growth(
            complete_type3_metrics(
                roic=0.04,
                wacc=0.08,
            ),
            benchmarks(),
        )

        self.assertEqual(scores["3c"], 1.5)
        self.assertNotIn("_veto", reasons)
        self.assertEqual(reasons["_status"], bs.STATUS_TRIGGERED)
        self.assertTrue(triggered)

    def test_type3_confirmed_moat_veto_survives_missing_roic_evidence(self):
        triggered, _total, _scores, reasons = bs.score_type3_sustainable_growth(
            base_metrics(roic=None, wacc=None, moat_score=2.0),
            benchmarks(),
        )

        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], bs.STATUS_VETOED)
        self.assertEqual(reasons["_evidence"], "incomplete")
        self.assertEqual(reasons["_veto"], "护城河证据不足")
        self.assertIn("投入回报", reasons["_missing"])

    def test_type3_accepts_dated_mainfinance_roic_and_uses_quality_evidence(self):
        metric = base_metrics(
            roic=0.20,
            wacc=0.08,
            roic_wacc_basis="Eastmoney年度ROIC/公司资本结构WACC",
            indicator_roic=0.20,
            gross_margin=0.40,
            gross_margin_cv=0.05,
            gross_margin_samples=4,
            adjusted_profit_ratio=0.98,
            share_dilution_1yr=0.0,
        )

        _triggered, _total, scores, reasons = bs.score_type3_sustainable_growth(metric, benchmarks())

        self.assertEqual(scores["3c"], 9.5)
        self.assertEqual(scores["3b"], 5.0)
        self.assertIn("扣非", reasons["3b"])
        self.assertIn("稀释", reasons["3b"])
        self.assertEqual(scores["3a"], 5.0)

    def test_type3_bad_profit_trend_is_not_overwritten_by_consistency(self):
        m = base_metrics(
            net_profit_history=[12.0, 16.0, 20.0, 30.0, 25.5],
            growth_consistency=0.1,
        )
        _, _, scores, reasons = bs.score_type3_sustainable_growth(m, benchmarks())
        self.assertLessEqual(scores["3d"], 5.0)
        self.assertIn("下降", reasons["3d"])

    def test_type3_moat_veto_and_bubble_cap(self):
        triggered, _, scores, reasons = bs.score_type3_sustainable_growth(
            base_metrics(roe=0.05, margin_median_hist=0.02, ocf_np_ratio=0.2), benchmarks()
        )
        self.assertEqual(scores["3a"], 5.0)
        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("_veto", reasons)
        triggered, total, scores, reasons = bs.score_type3_sustainable_growth(
            base_metrics(pe=100, roic=0.25, wacc=0.08), benchmarks(median_pe=20)
        )
        self.assertEqual(scores["3e"], 5.0)
        self.assertFalse(triggered)
        self.assertNotEqual(total, 4.9)
        self.assertNotIn("_downgrade", reasons)

        triggered, total, scores, reasons = bs.score_type3_sustainable_growth(
            complete_type3_metrics(type3_bubble_score=2.0),
            benchmarks(),
        )
        self.assertFalse(triggered)
        self.assertLessEqual(scores["3e"], 3.0)
        self.assertEqual(total, 4.9)
        self.assertIn("_downgrade", reasons)

    def test_type3_below_ten_percent_trend_is_scored_instead_of_marked_not_applicable(self):
        outcome = bs.score_type3_sustainable_growth(
            complete_type3_metrics(
                revenue_values=[100.0, 104.0, 108.0, 113.0, 118.0],
                trend_growth=0.0999,
            ),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_applicable"], "yes")
        self.assertNotIn("_scope", outcome[3])
        self.assertEqual(outcome[2]["3d"], 9.0)
        self.assertGreaterEqual(outcome[1], bs.QUALIFY_THRESHOLD)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_CONDITIONAL)
        self.assertIn("低于10%高增长门槛", outcome[3]["_condition"])
        self.assertNotIn("_veto", outcome[3])

    def test_type3_missing_trend_is_insufficient_not_not_applicable(self):
        outcome = bs.score_type3_sustainable_growth(
            base_metrics(trend_growth=None, revenue_values=[]),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(outcome[3]["_evidence"], "incomplete")
        self.assertNotIn("_veto", outcome[3])

    def test_type3_sparse_revenue_years_cannot_prove_sustainable_growth(self):
        outcome = bs.score_type3_sustainable_growth(
            complete_type3_metrics(
                revenue_values=[100.0, 180.0],
                revenue_years=[2021, 2024],
                trend_growth=0.20,
            ),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("连续4年营收", outcome[3]["_missing"])

    def test_type3_confirmed_sustainability_veto_survives_other_missing_evidence(self):
        outcome = bs.score_type3_sustainable_growth(
            complete_type3_metrics(
                growth_sustainability_score=2.0,
                roic=None,
                wacc=None,
            ),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_VETOED)
        self.assertEqual(outcome[3]["_evidence"], "incomplete")
        self.assertEqual(outcome[3]["_veto"], "增长不可持续")
        self.assertIn("投入回报", outcome[3]["_missing"])

    def test_type3_weak_qualitative_proxies_are_capped(self):
        triggered, _, scores, reasons = bs.score_type3_sustainable_growth(
            base_metrics(roic=0.20, wacc=0.08), benchmarks()
        )
        self.assertFalse(triggered)
        self.assertLessEqual(scores["3a"], 6.0)
        self.assertLessEqual(scores["3b"], 6.0)
        self.assertLessEqual(scores["3e"], 6.0)
        self.assertIn("弱代理", reasons["3a"])
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("_veto", reasons)

    def test_type4_valuation_and_price_bubble_use_dcf_terminal_values(self):
        dcf = complete_dcf_evidence(current_price=60.0)
        _, _, scores, reasons = bs.score_type4_long_runway(complete_type4_metrics(price=60.0), benchmarks(), dcf)
        self.assertGreater(scores["4d"], 6.0)
        self.assertGreater(scores["4f"], 8.0)
        self.assertIn("10年中性估值", reasons["4d"])

    def test_type4_financial_company_is_explicitly_not_applicable(self):
        outcome = bs.score_type4_long_runway(
            base_metrics(industry="INSURANCE"),
            benchmarks(),
        )

        self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertEqual(outcome[3]["_applicable"], "no")
        self.assertEqual(outcome[3]["_evidence"], "complete")

    def test_type4_cannot_trigger_without_a_complete_dcf_terminal_value(self):
        triggered, total, scores, reasons = bs.score_type4_long_runway(
            base_metrics(),
            benchmarks(),
            None,
        )

        self.assertFalse(triggered)
        self.assertEqual(scores["4d"], 2.0)
        self.assertEqual(scores["4f"], 2.0)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("_veto", reasons)
        self.assertTrue(math.isfinite(total))

        partial = {
            "dcf_points": {
                "neutral": {"lower": 100.0, "upper": 120.0},
                "optimistic": {"lower": 150.0, "upper": 180.0},
            }
        }
        triggered, _, scores, reasons = bs.score_type4_long_runway(
            base_metrics(),
            benchmarks(),
            partial,
        )
        self.assertFalse(triggered)
        self.assertEqual(scores["4d"], 2.0)
        self.assertEqual(scores["4f"], 2.0)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("_veto", reasons)

    def test_financial_type1_uses_pb_evidence_and_other_templates_fail_closed(self):
        financial = base_metrics(
            industry="BANK",
            price=50.0,
            roe=0.15,
            roe_history=[0.12, 0.13, 0.15],
            equity_growth=0.08,
            net_profit_history=[10, 11, 13, 15],
            free_cash_flow=None,
            oper_cf=None,
            ocf_np_ratio=None,
        )
        type1 = bs.score_type1_dcf(financial, complete_pb_evidence(), benchmarks())
        self.assertFalse(type1[0])
        self.assertIn("PB模型", type1[3]["1c"])
        self.assertEqual(type1[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(type1[3]["_evidence"], "incomplete")
        self.assertIn("监管证据缺失", type1[3]["1b"])
        self.assertNotIn("_veto", type1[3])
        for type_key, outcome in zip(
            ("type2", "type3", "type4", "type5", "type6"),
            (
                bs.score_type2_two_hot_one_cold(financial, benchmarks()),
                bs.score_type3_sustainable_growth(financial, benchmarks()),
                bs.score_type4_long_runway(financial, benchmarks()),
                bs.score_type5_counter_cyclical(financial, benchmarks()),
                bs.score_type6_vc(financial, benchmarks()),
            ),
        ):
            self.assertFalse(outcome[0], type_key)
            if type_key in {"type3", "type4", "type5", "type6"}:
                self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)
            else:
                self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
            self.assertNotIn("_veto", outcome[3], type_key)

    def test_complete_bank_regulatory_and_reversion_evidence_can_support_type1(self):
        financial = base_metrics(
            industry="BANK",
            price=1.0,
            indicator_weighted_roe=0.12,
            capital_adequacy_ratio=0.125,
            tier1_capital_adequacy_ratio=0.105,
            nonperforming_loan_ratio=0.010,
            loan_provision_coverage_proxy=3.0,
            net_interest_margin=0.019,
            net_interest_margin_history=[0.018, 0.019],
            net_interest_margin_years=[2024, 2025],
            nonperforming_loan_ratio_history=[0.011, 0.010],
            nonperforming_loan_ratio_years=[2024, 2025],
            capital_adequacy_ratio_history=[0.120, 0.125],
            capital_adequacy_ratio_years=[2024, 2025],
            profit_1yr_change=0.20,
            free_cash_flow=None,
            oper_cf=None,
            ocf_np_ratio=None,
        )

        outcome = bs.score_type1_dcf(financial, complete_pb_evidence(current_price=1.0), benchmarks())

        self.assertTrue(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertEqual(outcome[3]["_evidence"], "complete")
        self.assertEqual(outcome[2]["1b"], 10.0)
        self.assertIn("银行监管", outcome[3]["1b"])

    def test_confirmed_bank_capital_breach_is_a_real_veto_not_missing_evidence(self):
        financial = base_metrics(
            industry="BANK",
            price=1.0,
            indicator_weighted_roe=0.12,
            capital_adequacy_ratio=0.07,
            tier1_capital_adequacy_ratio=0.05,
            nonperforming_loan_ratio=0.010,
            loan_provision_coverage_proxy=3.0,
            net_interest_margin=0.019,
            net_interest_margin_history=[0.018, 0.019],
            net_interest_margin_years=[2024, 2025],
            nonperforming_loan_ratio_history=[0.011, 0.010],
            nonperforming_loan_ratio_years=[2024, 2025],
            capital_adequacy_ratio_history=[0.075, 0.070],
            capital_adequacy_ratio_years=[2024, 2025],
            profit_1yr_change=0.20,
        )

        outcome = bs.score_type1_dcf(financial, complete_pb_evidence(current_price=1.0), benchmarks())

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_VETOED)
        self.assertEqual(outcome[3]["_evidence"], "complete")
        self.assertEqual(outcome[2]["1b"], 3.0)
        self.assertIn("监管最低", outcome[3]["1b"])

    def test_bank_type2_uses_sector_cycle_company_reversion_and_independent_coldness(self):
        financial = base_metrics(
            industry="BANK",
            pb=0.70,
            market_coldness_score=8.0,
            net_interest_margin_history=[0.018, 0.019],
            net_interest_margin_years=[2024, 2025],
            nonperforming_loan_ratio_history=[0.011, 0.010],
            nonperforming_loan_ratio_years=[2024, 2025],
            capital_adequacy_ratio_history=[0.120, 0.125],
            capital_adequacy_ratio_years=[2024, 2025],
            profit_1yr_change=0.20,
        )
        sector_benchmarks = {
            "BANK": {
                "median_nim_change": 0.0005,
                "median_nim_change_count": 20,
                "median_npl_change": -0.0002,
                "median_npl_change_count": 20,
                "median_profit_change": 0.08,
                "median_profit_change_count": 20,
                "median_bank_capital_change": 0.001,
                "median_bank_capital_change_count": 20,
                "median_pb": 1.0,
                "median_pb_count": 20,
            }
        }

        outcome = bs.score_type2_two_hot_one_cold(financial, sector_benchmarks)

        self.assertTrue(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertEqual(outcome[3]["_evidence"], "complete")
        self.assertIn("银行业", outcome[3]["2a"])
        self.assertIn("金融回归", outcome[3]["2b"])

    def test_bank_type2_missing_peer_valuation_is_incomplete_even_with_other_three_dimensions(self):
        financial = base_metrics(
            industry="BANK",
            pb=None,
            market_coldness_score=8.0,
            net_interest_margin_history=[0.018, 0.019],
            net_interest_margin_years=[2024, 2025],
            nonperforming_loan_ratio_history=[0.011, 0.010],
            nonperforming_loan_ratio_years=[2024, 2025],
            capital_adequacy_ratio_history=[0.120, 0.125],
            capital_adequacy_ratio_years=[2024, 2025],
            profit_1yr_change=0.20,
        )
        sector_benchmarks = {
            "BANK": {
                "median_nim_change": 0.0005,
                "median_nim_change_count": 20,
                "median_npl_change": -0.0002,
                "median_npl_change_count": 20,
                "median_profit_change": 0.08,
                "median_profit_change_count": 20,
                "median_bank_capital_change": 0.001,
                "median_bank_capital_change_count": 20,
                "median_pb": 1.0,
                "median_pb_count": 20,
            }
        }

        outcome = bs.score_type2_two_hot_one_cold(financial, sector_benchmarks)

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[2]["2d"], 2.0)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("金融估值", outcome[3]["_missing"])

    def test_financial_institutions_are_not_forced_into_type5_strong_cycle_model(self):
        financial = base_metrics(
            industry="BANK",
            pb=0.70,
            indicator_weighted_roe=0.12,
            capital_adequacy_ratio=0.125,
            tier1_capital_adequacy_ratio=0.105,
            nonperforming_loan_ratio=0.010,
            loan_provision_coverage_proxy=3.0,
            net_interest_margin=0.018,
            net_interest_margin_history=[0.020, 0.018, 0.017, 0.018],
            net_interest_margin_years=[2022, 2023, 2024, 2025],
            nonperforming_loan_ratio_history=[0.011, 0.010],
            nonperforming_loan_ratio_years=[2024, 2025],
        )

        outcome = bs.score_type5_counter_cyclical(financial, {"BANK": {"median_pb": 1.0}})

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertIn("金融机构", outcome[3]["_scope"])

    def test_insurance_and_securities_regulatory_checks_use_native_fields(self):
        insurance = base_metrics(
            industry="INSURANCE",
            indicator_weighted_roe=0.12,
            solvency_adequacy_ratio=1.80,
            new_business_value_margin=0.25,
            new_business_value_history=[100.0, 110.0],
            new_business_value_years=[2024, 2025],
            life_surrender_rate=0.015,
        )
        securities = base_metrics(
            industry="SECURITIES",
            risk_coverage_ratio=1.80,
            capital_leverage_ratio=0.13,
            liquidity_coverage_ratio=1.30,
            net_stable_funding_ratio=1.25,
            net_capital_to_liabilities_ratio=0.15,
        )

        insurance_points, insurance_complete, _reason = bs._financial_regulatory_trap_points(insurance)
        securities_points, securities_complete, _reason = bs._financial_regulatory_trap_points(securities)

        self.assertTrue(insurance_complete)
        self.assertEqual(sum(insurance_points), 10)
        self.assertTrue(securities_complete)
        self.assertEqual(sum(securities_points), 10)
        self.assertFalse(bs._financial_hard_regulatory_breach(insurance))
        self.assertFalse(bs._financial_hard_regulatory_breach(securities))

        insurance["solvency_adequacy_ratio"] = 0.99
        securities["liquidity_coverage_ratio"] = 0.99
        self.assertTrue(bs._financial_hard_regulatory_breach(insurance))
        self.assertTrue(bs._financial_hard_regulatory_breach(securities))

    def test_missing_financial_regulatory_field_is_unknown_not_a_confirmed_failure(self):
        incomplete = base_metrics(
            industry="SECURITIES",
            risk_coverage_ratio=2.0,
            capital_leverage_ratio=0.15,
            liquidity_coverage_ratio=None,
            net_stable_funding_ratio=1.3,
            net_capital_to_liabilities_ratio=0.2,
        )

        points, complete, reason = bs._financial_regulatory_trap_points(incomplete)

        self.assertFalse(complete)
        self.assertEqual(len(points), 5)
        self.assertIn("证据缺失", reason)
        self.assertFalse(bs._financial_hard_regulatory_breach(incomplete))

    def test_unsupported_other_financial_cannot_trigger_any_template(self):
        financial = base_metrics(
            industry="FINANCIAL_OTHER",
            market_cap=1e8,
            market_coldness_score=10.0,
            cyclical_industry_score=10.0,
            technology_score=10.0,
            business_model_score=10.0,
        )
        outcomes = (
            bs.score_type1_dcf(financial, complete_dcf_evidence(), benchmarks()),
            bs.score_type2_two_hot_one_cold(financial, benchmarks()),
            bs.score_type3_sustainable_growth(financial, benchmarks()),
            bs.score_type4_long_runway(financial, benchmarks(), complete_dcf_evidence()),
            bs.score_type5_counter_cyclical(financial, benchmarks()),
            bs.score_type6_vc(financial, benchmarks()),
        )

        self.assertTrue(all(not outcome[0] for outcome in outcomes))
        self.assertTrue(
            all(
                outcome[3]["_status"] in {bs.STATUS_NOT_APPLICABLE, bs.STATUS_INSUFFICIENT_EVIDENCE}
                for outcome in outcomes
            )
        )

    def test_type4_missing_growth_does_not_crash(self):
        m = base_metrics(trend_growth=None, cagr_3yr=None, cagr_5yr=None)
        _, _, scores, _ = bs.score_type4_long_runway(m, benchmarks(neutral_benchmark=None))
        self.assertEqual(scores["4a"], 2.0)

    def test_type4_static_industry_story_cannot_override_company_contraction(self):
        m = base_metrics(trend_growth=-0.25, cagr_3yr=-0.25, cagr_5yr=-0.20)

        triggered, _, scores, reasons = bs.score_type4_long_runway(m, benchmarks(neutral_benchmark=0.20))

        self.assertLessEqual(scores["4a"], 3.0)
        self.assertFalse(triggered)
        self.assertIn(reasons["_status"], {bs.STATUS_NOT_TRIGGERED, bs.STATUS_INSUFFICIENT_EVIDENCE})

    def test_type4_moat_veto(self):
        triggered, _, scores, reasons = bs.score_type4_long_runway(
            complete_type4_metrics(
                moat_durability_score=2.0,
                roe=0.05,
                net_margin=0.02,
                margin_median_hist=0.02,
                growth_consistency=2.0,
                debt_ratio=0.80,
            ),
            benchmarks(),
            complete_dcf_evidence(),
        )
        self.assertLessEqual(scores["4c"], 3.0)
        self.assertFalse(triggered)
        self.assertIn("_veto", reasons)

    def test_type4_moat_durability_reason_shows_verified_history_period(self):
        years = list(range(2016, 2026))
        metric = complete_type4_metrics(
            indicator_roic_history=[0.20] * len(years),
            indicator_roic_years=years,
            gross_margin_history=[0.40] * len(years),
            gross_margin_years=years,
            adjusted_profit_ratio=0.98,
            share_dilution_1yr=0.0,
            revenue_latest=190.0,
        )
        quantitative = derive_company_evidence(
            metric,
            {
                "gross_margin_median_population": [0.30] * MIN_SECTOR_COMPANIES,
                "revenue_latest_population": [100.0] * MIN_SECTOR_COMPANIES,
            },
        )
        durability = quantitative["moat_durability_score"]
        self.assertEqual(durability["evidence_level"], "derived_proxy")
        self.assertEqual(durability["details"]["durability_history_years"], 10)
        self.assertEqual(durability["details"]["common_history_years"], years)
        metric.update(
            {
                "quantitative_evidence": {"moat_durability_score": durability},
                "moat_durability_score": durability["score"],
                "moat_durability_score_evidence": durability["evidence"],
                "moat_durability_score_evidence_level": durability["evidence_level"],
            }
        )

        _triggered, _total, scores, reasons = bs.score_type4_long_runway(
            metric,
            benchmarks(),
            complete_dcf_evidence(),
        )

        self.assertEqual(scores["4c"], durability["score"])
        self.assertEqual(reasons["4c"], "ROIC与毛利率共同连续10年，覆盖2016–2025年")
        self.assertNotIn("patch", reasons["4c"].lower())

    def test_type4_moat_durability_reason_keeps_generic_fallback_without_verified_ledger(self):
        metric = complete_type4_metrics()
        metric["moat_durability_score_evidence"] = {
            "source": "test quantitative evidence",
            "evidence_id": (f"{bs.QUANTITATIVE_EVIDENCE_MODEL_ID}:moat_durability_score:000001:20260715"),
            "as_of": "2026-07-15",
        }
        _triggered, _total, _scores, reasons = bs.score_type4_long_runway(
            metric,
            benchmarks(),
            complete_dcf_evidence(),
        )

        self.assertEqual(reasons["4c"], "多年盈利稳定性数据")

    def test_type4_moat_durability_reason_hides_tampered_history_ledger(self):
        years = list(range(2016, 2026))
        metric = complete_type4_metrics(
            indicator_roic_history=[0.20] * len(years),
            indicator_roic_years=years,
            gross_margin_history=[0.40] * len(years),
            gross_margin_years=years,
            adjusted_profit_ratio=0.98,
            share_dilution_1yr=0.0,
            revenue_latest=190.0,
        )
        durability = derive_company_evidence(
            metric,
            {
                "gross_margin_median_population": [0.30] * MIN_SECTOR_COMPANIES,
                "revenue_latest_population": [100.0] * MIN_SECTOR_COMPANIES,
            },
        )["moat_durability_score"]
        tampered = copy.deepcopy(durability)
        tampered["details"]["common_history_years"] = [2016, 2018, *range(2019, 2026)]
        metric.update(
            {
                "quantitative_evidence": {"moat_durability_score": tampered},
                "moat_durability_score": durability["score"],
                "moat_durability_score_evidence": durability["evidence"],
                "moat_durability_score_evidence_level": durability["evidence_level"],
            }
        )

        _triggered, _total, _scores, reasons = bs.score_type4_long_runway(
            metric,
            benchmarks(),
            complete_dcf_evidence(),
        )

        self.assertEqual(reasons["4c"], "多年盈利稳定性数据")

    def test_type4_current_moat_cannot_substitute_for_missing_durability_evidence(self):
        metric = complete_type4_metrics(moat_score=9.0)
        metric["moat_durability_score"] = None
        metric["moat_durability_score_evidence"] = None
        metric["moat_durability_score_evidence_level"] = None

        triggered, _total, scores, reasons = bs.score_type4_long_runway(
            metric,
            benchmarks(),
            complete_dcf_evidence(),
        )

        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("4c", reasons["_decision_missing_dimensions"])
        self.assertLessEqual(scores["4c"], 5.0)
        self.assertIn("缺多年耐久证据", reasons["4c"])

    def test_type4_missing_qualitative_evidence_is_unknown_not_a_company_veto(self):
        triggered, _, scores, reasons = bs.score_type4_long_runway(
            base_metrics(roe=0.05, trend_growth=-0.10, pe=200.0),
            benchmarks(median_pe=5.0),
            complete_dcf_evidence(),
        )

        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(scores["4c"], 5.0)
        self.assertEqual(scores["4e"], 5.0)
        self.assertNotIn("_veto", reasons)
        self.assertIn("产业泡沫", reasons["_missing"])

    def test_type4_confirmed_moat_veto_survives_other_missing_dimensions(self):
        outcome = bs.score_type4_long_runway(
            base_metrics(moat_durability_score=2.0),
            benchmarks(),
            complete_dcf_evidence(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_VETOED)
        self.assertEqual(outcome[3]["_evidence"], "incomplete")
        self.assertEqual(outcome[3]["_veto"], "持久护城河不足")

    def test_type4_snow_requires_aligned_three_year_fcf_margin_and_roic_history(self):
        mutations = (
            {"fcf_years": [2022, 2024, 2025]},
            {"indicator_roic_years": [2022, 2024, 2025]},
            {"gross_margin_years": [2022, 2024, 2025]},
            {"margin_years": [2021, 2022, 2023, 2025, 2027]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                outcome = bs.score_type4_long_runway(
                    complete_type4_metrics(**mutation),
                    benchmarks(),
                    complete_dcf_evidence(),
                )

                self.assertFalse(outcome[0])
                self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
                self.assertIn("厚雪", outcome[3]["_missing"])
                self.assertNotIn("_veto", outcome[3])

    def test_type4_snow_rejects_roic_and_wacc_with_an_unbound_calculation_basis(self):
        outcome = bs.score_type4_long_runway(
            complete_type4_metrics(roic_wacc_basis="unrelated-basis"),
            benchmarks(),
            complete_dcf_evidence(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("厚雪", outcome[3]["_missing"])

    def test_type4_industry_bubble_score_never_falls_back_to_company_or_peer_pe(self):
        low_pe = bs.score_type4_long_runway(base_metrics(pe=5.0), benchmarks(median_pe=50.0), complete_dcf_evidence())
        high_pe = bs.score_type4_long_runway(base_metrics(pe=500.0), benchmarks(median_pe=5.0), complete_dcf_evidence())

        self.assertEqual(low_pe[2]["4e"], 5.0)
        self.assertEqual(high_pe[2]["4e"], 5.0)
        self.assertEqual(low_pe[3]["4e"], high_pe[3]["4e"])

    def test_type4_implied_growth_runway_is_monotonic_with_price(self):
        low = complete_dcf_evidence(current_price=50.0)
        high = complete_dcf_evidence(current_price=250.0)

        low_years = bs._implied_price_growth_years(low, 50.0)
        high_years = bs._implied_price_growth_years(high, 250.0)

        self.assertIsNotNone(low_years)
        self.assertIsNotNone(high_years)
        self.assertGreaterEqual(high_years, low_years)
        self.assertLessEqual(bs._score_implied_growth_years(high_years), bs._score_implied_growth_years(low_years))

    def test_type4_implied_growth_binary_search_matches_exhaustive_integer_search(self):
        payload = complete_dcf_evidence(current_price=300.0)
        optimistic = payload["params"]["optimistic"]
        growth = max(optimistic["terminal_g"], min(optimistic["growth"], 0.25))

        def value_for(years):
            return bs.dcf_valuation(
                base_fcf=payload["base_fcf"],
                base_revenue=payload["base_revenue"],
                revenue_growth=optimistic["terminal_g"] if years == 0 else growth,
                wacc=optimistic["wacc_base"] - bs.BAND_WACC_DELTA,
                terminal_g=optimistic["terminal_g"],
                shares_outstanding=payload["shares_outstanding"],
                net_debt=payload["net_debt"],
                margin_retention=optimistic["margin_retention"],
                forecast_years=max(1, years),
            )

        values = [value_for(years) for years in range(31)]
        targets = [values[index] for index in (0, 1, 2, 7, 15, 30)] + [values[-1] * 1.01]
        for target in targets:
            with self.subTest(target=target):
                expected = next((index for index, value in enumerate(values) if value >= target), 31)
                self.assertEqual(bs._implied_price_growth_years(payload, target), expected)

    def test_type4_implied_growth_runway_uses_optimistic_not_neutral_inputs(self):
        baseline = complete_dcf_evidence(current_price=300.0)
        neutral_only = copy.deepcopy(baseline)
        neutral_only["params"]["neutral"]["growth"] = -0.20
        optimistic_faster = copy.deepcopy(baseline)
        optimistic_faster["params"]["optimistic"]["growth"] = 0.12

        baseline_years = bs._implied_price_growth_years(baseline, 300.0)
        neutral_years = bs._implied_price_growth_years(neutral_only, 300.0)
        faster_years = bs._implied_price_growth_years(optimistic_faster, 300.0)

        self.assertEqual(neutral_years, baseline_years)
        self.assertLessEqual(faster_years, baseline_years)

    def test_type4_reverse_dcf_uses_optimistic_upper_discount_rate(self):
        payload = complete_dcf_evidence(current_price=300.0)
        with patch.object(bs, "dcf_valuation", return_value=0.0) as valuation:
            self.assertEqual(bs._implied_price_growth_years(payload, 300.0), 31)

        first_call = valuation.call_args_list[0].kwargs
        self.assertEqual(
            first_call["wacc"],
            payload["params"]["optimistic"]["wacc_base"] - bs.BAND_WACC_DELTA,
        )

    def test_type4_implied_growth_year_score_has_exact_patch6_boundaries(self):
        self.assertEqual(bs._score_implied_growth_years(2), 10.0)
        self.assertEqual(bs._score_implied_growth_years(3), 8.0)
        self.assertEqual(bs._score_implied_growth_years(5), 6.0)
        self.assertEqual(bs._score_implied_growth_years(7), 4.0)
        self.assertEqual(bs._score_implied_growth_years(10), 2.0)
        self.assertEqual(bs._score_implied_growth_years(15), 0.0)

    def test_type5_recovery_requires_monotonic_trend(self):
        self.assertTrue(bs._is_strict_recovery([100, 90, 30, 45, 70]))
        self.assertFalse(bs._is_strict_recovery([100, 30, 70, 40, 80]))
        self.assertFalse(bs._is_strict_recovery([10, 20, 30, 45, 70]))
        self.assertFalse(bs._is_strict_recovery([300, 90, 200, 400]))
        self.assertTrue(bs._has_cycle_history([100, 60, 30, 45, 70]))
        self.assertFalse(bs._has_cycle_history([10, 20, 30, 45, 70]))

    def test_type5_normalised_pe_never_averages_across_a_year_gap(self):
        metric = base_metrics(
            market_cap=100.0,
            net_profit_history=[100.0] * 5 + [10.0] * 5,
            net_profit_years=[2011, 2012, 2013, 2014, 2015, 2021, 2022, 2023, 2024, 2025],
        )

        normalised_pe, years_used = bs._type5_normalised_pe(metric)

        self.assertEqual(years_used, 5)
        self.assertEqual(normalised_pe, 10.0)

    def test_type5_complete_nonpositive_cycle_average_is_adverse_not_missing(self):
        metric = base_metrics(
            market_cap=100.0,
            net_profit_history=[-100.0, -50.0, -20.0, -10.0, -5.0],
            net_profit_years=[2021, 2022, 2023, 2024, 2025],
            **trusted_type5_scores(
                type5_cycle_attribute_score=8.0,
                type5_bottom_signal_score=8.0,
                type5_survival_score=8.0,
                type5_upside_elasticity_score=8.0,
            ),
        )

        outcome = bs.score_type5_counter_cyclical(metric, benchmarks())

        self.assertEqual(outcome[2]["5e"], 1.0)
        self.assertEqual(outcome[3]["5e"], "5年周期平均利润非正")
        self.assertNotIn("5e", outcome[3]["_decision_missing_dimensions"])

    def test_type5_pb_signal_has_exact_percentile_and_absolute_boundaries(self):
        cases = (
            ((0.10, 1.0), 10.0),
            ((0.100001, 1.0), 8.0),
            ((0.20, 1.2), 8.0),
            ((0.200001, 1.2), 6.0),
            ((0.30, 1.5), 6.0),
            ((0.300001, 1.5), 4.0),
            ((0.08, 2.01), 2.0),
            ((0.80, 0.80), 2.0),
        )
        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                self.assertEqual(bs._type5_pb_bottom_score(*inputs), expected)
        for invalid in ((True, 1.0), (0.1, False), (-0.01, 1.0), (0.1, 0.0)):
            with self.subTest(invalid=invalid):
                self.assertIsNone(bs._type5_pb_bottom_score(*invalid))

    def test_type5_automatic_bottom_can_trigger_only_with_three_bound_sources(self):
        metric = complete_type5_bottom_metrics()
        history = type5_history_evidence(
            code=metric["code"],
            as_of=metric["source_trade_date"],
        )
        history["available"] = False
        history["shareholder_return"] = {"available": False, "reason": "insufficient_ten_year_span"}

        triggered, total, scores, reasons = bs.score_type5_counter_cyclical(
            metric,
            benchmarks(),
            history_evidence=history,
        )

        self.assertTrue(triggered)
        self.assertGreaterEqual(total, 7.0)
        self.assertEqual(scores["5b"], 9.4)
        self.assertEqual(reasons["_status"], bs.STATUS_TRIGGERED)
        self.assertEqual(reasons["_evidence"], "complete")
        self.assertIn("PB8%/0.95", reasons["5b"])
        self.assertNotIn("成本", reasons["5b"])
        self.assertNotIn("库存", reasons["5b"])
        contract = bs._type5_automatic_bottom_contract(metric, history)
        self.assertEqual(
            set(contract),
            {
                "schema_version",
                "model_id",
                "code",
                "as_of",
                "quote_pb",
                "valuation_history",
                "market_coldness_record",
                "financial_cycle",
            },
        )
        self.assertEqual(contract["schema_version"], bs.TYPE5_BOTTOM_EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(contract["model_id"], bs.TYPE5_BOTTOM_EVIDENCE_MODEL_ID)
        self.assertEqual(contract["code"], "000001")
        self.assertEqual(contract["as_of"], metric["source_trade_date"])
        self.assertEqual(contract["valuation_history"], history["valuation_history"])
        self.assertEqual(
            contract["market_coldness_record"]["components"],
            metric["market_coldness_components"],
        )
        self.assertEqual(
            contract["financial_cycle"]["net_profit_history"],
            metric["net_profit_history"],
        )
        self.assertEqual(
            bs.replay_type5_bottom_evidence_contract(
                contract,
                expected_code=metric["code"],
                expected_as_of=metric["source_trade_date"],
            ),
            {"score": scores["5b"], "reason": reasons["5b"]},
        )

        mutations = {
            "identity": lambda value: value.update(code="000002"),
            "valuation_summary": lambda value: value["valuation_history"].update(pb_percentile=0.90),
            "valuation_distribution": lambda value: value["valuation_history"]["pb_distribution"]["counts"].pop(),
            "market_session": lambda value: value["market_coldness_record"]["components"].update(
                as_of_session="2026-07-16"
            ),
            "market_drawdown": lambda value: value["market_coldness_record"]["components"]["raw_values"].update(
                change_60d_pct=20.0,
                change_ytd_pct=20.0,
            ),
            "financial_cycle": lambda value: value["financial_cycle"]["net_profit_history"].pop(),
        }
        expected_replay = {"score": scores["5b"], "reason": reasons["5b"]}
        for label, mutate in mutations.items():
            forged = copy.deepcopy(contract)
            mutate(forged)
            with self.subTest(contract_mutation=label):
                self.assertNotEqual(
                    bs.replay_type5_bottom_evidence_contract(
                        forged,
                        expected_code=metric["code"],
                        expected_as_of=metric["source_trade_date"],
                    ),
                    expected_replay,
                )

    def test_type5_missing_coldness_keeps_bottom_evidence_incomplete(self):
        metric = complete_type5_bottom_metrics(
            market_coldness_score=None,
            market_coldness_score_evidence=None,
            market_coldness_components=None,
        )

        triggered, _total, scores, reasons = bs.score_type5_counter_cyclical(
            metric,
            benchmarks(),
            history_evidence=type5_history_evidence(),
        )

        self.assertFalse(triggered)
        self.assertEqual(scores["5b"], 5.0)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(reasons["_evidence"], "incomplete")
        self.assertIn("冷度", reasons["5b"])

    def test_type5_single_pb_signal_is_capped_without_price_and_financial_resonance(self):
        metric = complete_type5_bottom_metrics(
            net_profit_history=[100.0, 50.0, 20.0, 40.0, 80.0, 60.0, 30.0, 50.0, 90.0, 120.0],
            gross_margin_history=[0.42, 0.30, 0.18, 0.25, 0.38, 0.29, 0.20, 0.28, 0.35, 0.45],
            **type5_coldness_fields(score=2.0, change_60d=10.0, change_ytd=15.0),
        )

        triggered, total, scores, reasons = bs.score_type5_counter_cyclical(
            metric,
            benchmarks(),
            history_evidence=type5_history_evidence(),
        )

        self.assertFalse(triggered)
        self.assertLess(total, 7.0)
        self.assertEqual(scores["5b"], 4.0)
        self.assertEqual(reasons["_evidence"], "complete")

    def test_type2_production_path_requires_company_specific_five_year_valuation_history(self):
        metric = base_metrics(
            revenue_values=[100, 110, 140],
            margin_history=[0.10, 0.12, 0.16],
            net_profit_history=[10, 15, 25],
            ocf_np_ratio=1.2,
            market_coldness_score=10.0,
            peg=0.5,
        )
        metric["_type2_history_evidence"] = None
        missing = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )
        self.assertEqual(missing[2]["2d"], 2.0)
        self.assertEqual(missing[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("估值", missing[3]["_missing"])

        history = type5_history_evidence(
            code=metric["code"],
            as_of=metric["source_trade_date"],
        )
        metric["_type2_history_evidence"] = history
        complete = bs.score_type2_two_hot_one_cold(
            metric,
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )
        self.assertGreaterEqual(complete[2]["2d"], 9.0)
        self.assertIn("自身五年PB分位", complete[3]["2d"])

    def test_type2_history_preflight_fills_2d_even_when_it_cannot_change_the_decision(self):
        weak_with_other_gaps = bs._finish(
            "type2",
            {"2a": 1.0, "2b": 1.0, "2c": 1.0, "2d": 2.0},
            {"2a": "弱产业", "2b": "弱公司", "2c": "市场不冷", "2d": "缺公司自身五年PE/PB分位"},
            evidence_complete=False,
            missing_dimensions=["2a", "2d"],
        )
        confirmed_veto_with_valuation_gap = bs._finish(
            "type2",
            {"2a": 1.0, "2b": 1.0, "2c": 8.0, "2d": 2.0},
            {"2a": "产业弱", "2b": "公司弱", "2c": "市场冷", "2d": "缺公司自身五年PE/PB分位"},
            veto=True,
            evidence_complete=False,
            missing_dimensions=["2d"],
        )
        complete = bs._finish(
            "type2",
            {"2a": 5.0, "2b": 5.0, "2c": 5.0, "2d": 5.0},
            {"2a": "产业", "2b": "公司", "2c": "市场", "2d": "估值"},
        )
        not_applicable = bs._finish(
            "type2",
            {"2a": 0.0, "2b": 0.0, "2c": 0.0, "2d": 0.0},
            {"_scope": "框架不适用"},
            applicable=False,
        )

        self.assertTrue(bs._type2_history_request_needed(weak_with_other_gaps))
        self.assertTrue(bs._type2_history_request_needed(confirmed_veto_with_valuation_gap))
        self.assertFalse(bs._type2_history_request_needed(complete))
        self.assertFalse(bs._type2_history_request_needed(not_applicable))

    def test_quality_history_loader_is_batched_without_dropping_or_duplicating_codes(self):
        requests = [{"code": str(index).zfill(6), "as_of": "2026-07-17"} for index in range(1, 4_503)]
        calls = []

        def loader(batch, *, progress_cb):
            calls.append((list(batch), progress_cb))
            return {item["code"]: {"code": item["code"]} for item in batch}

        marker = object()
        loaded = bs._load_quality_history_batches(loader, requests, progress_cb=marker)

        self.assertEqual([len(batch) for batch, _progress in calls], [2_000, 2_000, 502])
        self.assertTrue(all(progress is marker for _batch, progress in calls))
        self.assertEqual(len(loaded), len(requests))
        self.assertEqual(set(loaded), {item["code"] for item in requests})

    def test_reused_valuation_distribution_is_rebased_to_the_current_quote(self):
        metric = base_metrics(
            source_trade_date="2026-07-28",
            pb=1.20,
        )
        history = type5_history_evidence(
            code=metric["code"],
            as_of="2026-07-28",
            pb_percentile=0.08,
            current_pb=0.95,
        )
        history["valuation_history"]["end_date"] = "2026-07-17"

        rebased = bs._rebase_quality_history_to_current_quote(metric, history)

        self.assertIsNot(rebased, history)
        valuation = rebased["valuation_history"]
        self.assertEqual(valuation["current_pb_mrq"], 1.20)
        self.assertEqual(valuation["pb_percentile"], 1.0)
        self.assertEqual(valuation["current_valuation_date"], "2026-07-28")
        self.assertEqual(valuation["current_valuation_source"], "validated_closing_quote")
        self.assertEqual(history["valuation_history"]["current_pb_mrq"], 0.95)
        self.assertEqual(history["valuation_history"]["pb_percentile"], 0.08)

    def test_type5_rejects_history_bound_to_another_security_or_date(self):
        metric = complete_type5_bottom_metrics()
        for field, value in (("code", "000002"), ("as_of", "2026-07-16")):
            history = type5_history_evidence()
            history[field] = value
            with self.subTest(field=field):
                triggered, _total, scores, reasons = bs.score_type5_counter_cyclical(
                    metric,
                    benchmarks(),
                    history_evidence=history,
                )
                self.assertFalse(triggered)
                self.assertEqual(scores["5b"], 5.0)
                self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_type5_recomputes_pb_percentile_from_the_raw_distribution(self):
        metric = complete_type5_bottom_metrics()
        history = type5_history_evidence()
        history["valuation_history"]["pb_percentile"] = 0.90

        outcome = bs.score_type5_counter_cyclical(
            metric,
            benchmarks(),
            history_evidence=history,
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[2]["5b"], 5.0)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_type5_history_preflight_requires_decision_reachability(self):
        viable = complete_type5_bottom_metrics()
        viable_outcome = bs.score_type5_counter_cyclical(viable, benchmarks())
        impossible = complete_type5_bottom_metrics(
            **trusted_type5_scores(
                type5_cycle_attribute_score=7.0,
                type5_survival_score=0.0,
                type5_upside_elasticity_score=0.0,
                type5_normalized_earnings_score=0.0,
            ),
        )
        impossible_outcome = bs.score_type5_counter_cyclical(impossible, benchmarks())
        non_cycle = base_metrics(industry="SOFTWARE")
        non_cycle_outcome = bs.score_type5_counter_cyclical(non_cycle, benchmarks())

        self.assertTrue(bs._type5_history_request_needed(viable, viable_outcome))
        self.assertFalse(bs._type5_history_request_needed(impossible, impossible_outcome))
        self.assertFalse(bs._type5_history_request_needed(non_cycle, non_cycle_outcome))

    def test_type5_requires_strong_cycle_attributes_and_never_uses_cycle_stage_veto(self):
        m = base_metrics(
            industry="COAL",
            net_profit_history=[300, 50, 80, 120, 70],
            gross_margin_history=[0.42, 0.22, 0.10, 0.31, 0.20],
            gross_margin_years=[2021, 2022, 2023, 2024, 2025],
            pe=100,
            pb=0.8,
            interim_profit_yoy=-0.50,
        )
        triggered, _, scores, reasons = bs.score_type5_counter_cyclical(m, benchmarks())
        self.assertFalse(triggered)
        self.assertEqual(scores["5a"], 7.0)
        self.assertNotIn("_veto", reasons)
        self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("证据", reasons["_missing"])

    def test_type5_ancient_gap_cannot_create_a_current_cycle(self):
        outcome = bs.score_type5_counter_cyclical(
            base_metrics(
                industry="COAL",
                net_profit_history=[1000.0, 100.0, 110.0, 105.0, 108.0],
                net_profit_years=[2010, 2022, 2023, 2024, 2025],
                gross_margin_history=[0.50, 0.20, 0.21, 0.205, 0.208],
                gross_margin_years=[2010, 2022, 2023, 2024, 2025],
                type5_bottom_signal_score=9.0,
                type5_survival_score=9.0,
                type5_upside_elasticity_score=9.0,
                type5_normalized_earnings_score=9.0,
            ),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("强周期属性", outcome[3]["_missing"])

    def test_type5_complete_but_low_volatility_history_is_not_called_missing_history(self):
        outcome = bs.score_type5_counter_cyclical(
            base_metrics(
                industry="COAL",
                net_profit_history=[100.0, 102.0, 104.0, 106.0, 108.0],
                net_profit_years=[2021, 2022, 2023, 2024, 2025],
                gross_margin_history=[0.20, 0.21, 0.22, 0.21, 0.20],
                gross_margin_years=[2021, 2022, 2023, 2024, 2025],
            ),
            benchmarks(),
        )

        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("财务波动不足", outcome[3]["_missing"])
        self.assertNotIn("历史不完整", outcome[3]["_missing"])

    def test_type5_uses_total_after_5a_without_an_extra_5c_hard_gate(self):
        m = base_metrics(
            industry="COAL",
            **trusted_type5_scores(
                type5_cycle_attribute_score=8.0,
                type5_bottom_signal_score=10.0,
                type5_survival_score=1.0,
                type5_upside_elasticity_score=10.0,
                type5_normalized_earnings_score=10.0,
            ),
        )
        triggered, total, scores, reasons = bs.score_type5_counter_cyclical(m, benchmarks())
        self.assertGreaterEqual(total, 7.0)
        self.assertTrue(triggered)
        self.assertEqual(scores["5c"], 1.0)
        self.assertNotIn("_veto", reasons)
        self.assertEqual(reasons["_status"], bs.STATUS_TRIGGERED)

    def test_type5_accepts_complete_financial_history_for_survival_elasticity_and_normalised_earnings(self):
        profits = [100.0, 50.0, 20.0, 40.0, 80.0, 120.0, 60.0, 30.0, 50.0, 90.0]
        m = complete_type5_bottom_metrics(
            market_cap=500.0,
            net_profit_history=profits,
            net_profit_years=list(range(2016, 2026)),
            gross_margin_history=[0.42, 0.30, 0.18, 0.25, 0.38, 0.45, 0.29, 0.16, 0.23, 0.35],
            gross_margin_years=list(range(2016, 2026)),
        )

        triggered, total, scores, reasons = bs.score_type5_counter_cyclical(
            m,
            benchmarks(),
            history_evidence=type5_history_evidence(),
        )

        self.assertTrue(triggered)
        self.assertGreaterEqual(total, 7.0)
        self.assertGreaterEqual(scores["5c"], 8.0)
        self.assertGreaterEqual(scores["5d"], 6.0)
        self.assertGreaterEqual(scores["5e"], 7.0)
        self.assertNotIn("_missing", reasons)

    def test_type5_rejects_serialized_generic_scores_without_the_strict_validation_boundary(self):
        outcome = bs.score_type5_counter_cyclical(
            base_metrics(
                industry="SOFTWARE",
                type5_cycle_attribute_score=10.0,
                type5_bottom_signal_score=10.0,
                type5_survival_score=10.0,
                type5_upside_elasticity_score=10.0,
                type5_normalized_earnings_score=10.0,
            ),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)

    def test_type5_rejects_non_cyclical_industry_even_when_company_numbers_look_cyclical(self):
        outcome = bs.score_type5_counter_cyclical(
            base_metrics(
                industry="SOFTWARE",
                net_profit_history=[300, 50, 80, 120],
                roe=0.12,
                pb=0.8,
            ),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[1], 0.0)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertIn("强周期", outcome[3]["_scope"])

    def test_type6_market_cap_boundary_and_complete_structure(self):
        for market_cap in (None, -1, 0):
            triggered, _, scores, reasons = bs.score_type6_vc(
                base_metrics(market_cap=market_cap, net_profit=-1, net_margin=-0.01), benchmarks()
            )
            self.assertFalse(triggered)
            self.assertEqual(set(scores), set(bs.TYPE_WEIGHTS["type6"]))
            self.assertEqual(reasons["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
            self.assertNotIn("_veto", reasons)
        growth_oversize = bs.score_type6_vc(
            base_metrics(market_cap=300e8 + 1, net_profit=-1, net_margin=-0.01),
            benchmarks(median_cagr=0.20, median_cagr_count=20),
        )
        turnaround_oversize = bs.score_type6_vc(
            base_metrics(market_cap=100e8 + 1, net_profit=-1, net_margin=-0.01),
            benchmarks(median_cagr=0.05, median_cagr_count=20),
        )
        self.assertEqual(growth_oversize[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertEqual(turnaround_oversize[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        triggered, _, _, reasons = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=300e8,
                technology_score=8,
                business_model_score=8,
                net_profit_history=[-3, -1, 2],
                net_profit=2,
                net_margin=0.02,
                trend_growth=0.30,
                position_size_pct=4,
                type6_portfolio_pct=12,
            ),
            benchmarks(median_cagr=0.30, median_cagr_count=20),
        )
        self.assertTrue(triggered)
        self.assertNotIn("_condition", reasons)
        turnaround_at_cap = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=100e8,
                technology_score=10,
                business_model_score=10,
                net_profit_history=[-3, -1, 2],
                net_profit=2,
                net_margin=0.02,
                position_size_pct=3,
                type6_portfolio_pct=10,
            ),
            benchmarks(median_cagr=0.05, median_cagr_count=20),
        )
        self.assertTrue(turnaround_at_cap[0])
        self.assertEqual(turnaround_at_cap[3]["_profile"], "平稳产业反转型")
        self.assertNotIn("_scope", turnaround_at_cap[3])
        _, _, scores, reasons = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=300e8,
                technology_score=8,
                business_model_score=8,
                net_profit_history=[-3, -1, 2],
                net_profit=2,
                net_margin=0.02,
                trend_growth=0.30,
                position_size_pct=4,
                type6_portfolio_pct=12,
            ),
            benchmarks(median_cagr=0.30, median_cagr_count=20),
        )
        self.assertIn("组合12%", reasons["6e"])
        self.assertIn("最大损失≤4%", reasons["_risk"])

        _, _, invalid_scores, _ = bs.score_type6_vc(
            base_metrics(technology_score=100, business_model_score=-5), benchmarks()
        )
        self.assertEqual(invalid_scores["6b"], 0.0)
        self.assertEqual(invalid_scores["6c"], 0.0)

    def test_type6_missing_user_position_never_becomes_automatic_buy(self):
        triggered, _, scores, reasons = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=10e8,
                technology_score=10,
                business_model_score=10,
                net_profit_history=[-3, -1, 2],
                net_profit=2,
                net_margin=0.02,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )

        self.assertEqual(scores["6e"], 9.0)
        self.assertFalse(triggered)
        self.assertIn("_condition", reasons)
        self.assertEqual(reasons["_status"], bs.STATUS_CONDITIONAL)

    def test_type6_proxy_technology_and_model_scores_cap_at_six_and_can_trigger(self):
        outcome = bs.score_type6_vc(
            base_metrics(
                market_cap=10e8,
                technology_score=9.0,
                business_model_score=8.0,
                trend_growth=0.30,
                net_profit_history=[-3.0, -1.0, 2.0],
                net_profit=2.0,
                net_margin=0.02,
                position_size_pct=3.0,
                type6_portfolio_pct=10.0,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )

        # A complete observable proxy is a reproducible quantitative score
        # (same spirit as 6a's 8-point ceiling): it caps at 6, below primary,
        # but counts as complete evidence so the framework can actually
        # trigger.  The explicit position gate (6e) remains a separate
        # action-confirmation step before any buy.
        self.assertTrue(outcome[0])
        self.assertEqual(outcome[2]["6b"], 6.0)
        self.assertEqual(outcome[2]["6c"], 6.0)
        self.assertIn("模型代理证据", outcome[3]["6b"])
        self.assertIn("模型代理证据", outcome[3]["6c"])
        self.assertEqual(outcome[3]["_decision_missing_dimensions"], [])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_TRIGGERED)

    def test_type6_primary_technology_and_model_scores_remain_formal_scores(self):
        outcome = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=10e8,
                technology_score=9.0,
                business_model_score=8.0,
                trend_growth=0.30,
                net_profit_history=[-3.0, -1.0, 2.0],
                net_profit=2.0,
                net_margin=0.02,
                position_size_pct=3.0,
                type6_portfolio_pct=10.0,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )

        self.assertTrue(outcome[0])
        self.assertEqual(outcome[2]["6b"], 9.0)
        self.assertEqual(outcome[2]["6c"], 8.0)
        self.assertNotIn("技术", outcome[3].get("_missing", ""))
        self.assertNotIn("商业模式", outcome[3].get("_missing", ""))

    def test_type6_high_growth_subtype_requires_both_industry_and_company_growth(self):
        cases = (
            (0.19, 0.50, "行业增速19.0%低于20%"),
            (0.20, 0.299, "公司趋势增速29.9%低于30%"),
            (0.20, None, "公司趋势增速缺失"),
        )
        for industry_growth, company_growth, expected_reason in cases:
            with self.subTest(industry_growth=industry_growth, company_growth=company_growth):
                outcome = bs.score_type6_vc(
                    primary_type6_metrics(
                        market_cap=100e8 + 1,
                        technology_score=10.0,
                        business_model_score=10.0,
                        trend_growth=company_growth,
                        net_profit=-1.0,
                        net_margin=-0.01,
                    ),
                    benchmarks(median_cagr=industry_growth, median_cagr_count=20),
                )
                self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)
                self.assertIn(expected_reason, outcome[3]["_scope"])

        high_growth = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=300e8,
                technology_score=10.0,
                business_model_score=10.0,
                trend_growth=0.30,
                net_profit_history=[-3.0, -1.0, 2.0],
                net_profit=2.0,
                net_margin=0.02,
                position_size_pct=3.0,
                type6_portfolio_pct=10.0,
            ),
            benchmarks(median_cagr=0.20, median_cagr_count=20),
        )
        self.assertEqual(high_growth[3]["_profile"], "高景气技术型")
        self.assertTrue(high_growth[0])

    def test_type6_ten_point_industry_score_requires_primary_early_stage_evidence(self):
        metric = primary_type6_metrics(
            market_cap=10e8,
            technology_score=10.0,
            business_model_score=10.0,
            trend_growth=0.30,
            net_profit_history=[-3.0, -1.0, 2.0],
            net_profit=2.0,
            net_margin=0.02,
            position_size_pct=3.0,
            type6_portfolio_pct=10.0,
        )
        without_early_stage = bs.score_type6_vc(
            metric,
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertEqual(without_early_stage[2]["6a"], 8.0)
        self.assertIn("缺产业初期原始证据", without_early_stage[3]["6a"])

        metric.update(
            industry_early_stage_confirmed=True,
            industry_early_stage_evidence_level="primary",
            industry_early_stage_evidence=score_evidence("industry_early_stage_score"),
        )
        with_early_stage = bs.score_type6_vc(
            metric,
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertEqual(with_early_stage[2]["6a"], 10.0)
        self.assertIn("产业初期原始证据已确认", with_early_stage[3]["6a"])

    def test_type6_known_position_violation_is_never_hidden_by_another_missing_input(self):
        base = {
            "market_cap": 10e8,
            "technology_score": 10.0,
            "business_model_score": 10.0,
            "trend_growth": 0.30,
            "net_profit_history": [-3.0, -1.0, 2.0],
            "net_profit": 2.0,
            "net_margin": 0.02,
        }
        cases = (
            (
                {"position_size_pct": 6.0, "type6_portfolio_pct": None},
                "单票仓位6%不在0%至5%范围内",
                "缺实际高风险组合仓位",
            ),
            (
                {"position_size_pct": None, "type6_portfolio_pct": 20.0},
                "高风险组合仓位20%不在0%至15%范围内",
                "缺实际单票仓位",
            ),
            ({"position_size_pct": 4.0, "type6_portfolio_pct": 3.0}, "单票仓位4%超过高风险组合仓位3%", None),
            ({"position_size_pct": 0.0, "type6_portfolio_pct": 10.0}, "单票仓位0%不在0%至5%范围内", None),
        )
        for positions, violation, missing in cases:
            with self.subTest(positions=positions):
                outcome = bs.score_type6_vc(
                    primary_type6_metrics(**base, **positions),
                    benchmarks(median_cagr=0.60, median_cagr_count=20),
                )
                self.assertFalse(outcome[0])
                self.assertEqual(outcome[2]["6e"], 0.0)
                self.assertIn(violation, outcome[3]["6e"])
                if missing is not None:
                    self.assertIn(missing, outcome[3]["6e"])
                self.assertEqual(outcome[3]["_condition"], "实际仓位违反强制风控条件")

        valid = bs.score_type6_vc(
            primary_type6_metrics(**base, position_size_pct=5.0, type6_portfolio_pct=15.0),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertTrue(valid[0])
        self.assertNotIn("_condition", valid[3])

    def test_type6_unknown_profit_profile_is_insufficient_not_not_applicable(self):
        cases = (
            base_metrics(market_cap=10e8, net_profit=None, net_margin=None),
            base_metrics(market_cap=10e8, net_profit=1.0, net_margin=None),
        )
        for metric in cases:
            with self.subTest(net_profit=metric["net_profit"]):
                outcome = bs.score_type6_vc(
                    metric,
                    benchmarks(median_cagr=0.20, median_cagr_count=20),
                )
                self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
                self.assertEqual(outcome[3]["_applicable"], "yes")
                self.assertEqual(outcome[3]["_evidence"], "incomplete")

    def test_type6_missing_turnaround_history_is_not_a_measured_three_point_failure(self):
        outcome = bs.score_type6_vc(
            base_metrics(
                market_cap=10e8,
                technology_score=10.0,
                business_model_score=10.0,
                net_profit=-1.0,
                net_margin=-0.01,
                net_profit_history=[],
                net_profit_years=[],
                margin_history=[],
                margin_years=[],
                profit_1yr_change=None,
                interim_profit_yoy=None,
                interim_current_profit=None,
                interim_prior_profit=None,
                interim_profit_pair_basis="missing_same_period_comparator",
                position_size_pct=1.0,
                type6_portfolio_pct=8.0,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[2]["6d"], 3.0)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("反转历史", outcome[3]["_missing"])
        self.assertIn("仅供诊断", outcome[3]["_score_quality"])

    def test_type6_turnaround_profile_cannot_trigger_while_profit_is_still_deteriorating(self):
        outcome = bs.score_type6_vc(
            primary_type6_metrics(
                market_cap=10e8,
                trend_growth=0.20,
                technology_score=10.0,
                business_model_score=10.0,
                net_profit=-3.0,
                net_margin=-0.03,
                net_profit_history=[-1.0, -2.0, -3.0],
                interim_current_profit=5.0,
                interim_prior_profit=10.0,
                interim_profit_yoy=-0.50,
                position_size_pct=3.0,
                type6_portfolio_pct=10.0,
            ),
            benchmarks(median_cagr=0.20, median_cagr_count=20),
        )

        self.assertFalse(outcome[0])
        self.assertGreaterEqual(outcome[1], 7.0)
        self.assertEqual(outcome[2]["6d"], 2.0)
        self.assertEqual(outcome[3]["_profile"], "平稳产业反转型")
        self.assertEqual(outcome[3]["_status"], bs.STATUS_CONDITIONAL)
        self.assertIn("困境反转证据达到5分", outcome[3]["_condition"])

    def test_type6_requires_loss_or_microprofit_and_consecutive_recovery_years(self):
        profitable = bs.score_type6_vc(
            base_metrics(
                market_cap=10e8,
                technology_score=10,
                business_model_score=10,
                position_size_pct=3,
                type6_portfolio_pct=10,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        gapped = bs.score_type6_vc(
            base_metrics(
                market_cap=10e8,
                technology_score=6,
                business_model_score=None,
                net_profit=-1,
                net_margin=-0.01,
                net_profit_history=[-5, -3, -1],
                net_profit_years=[2021, 2023, 2025],
                margin_history=[-0.05, -0.03, -0.01],
                margin_years=[2021, 2023, 2025],
                position_size_pct=3,
                type6_portfolio_pct=10,
            ),
            benchmarks(median_cagr=0.10, median_cagr_count=20),
        )

        self.assertFalse(profitable[0])
        self.assertEqual(profitable[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertIn("微利", profitable[3]["_scope"])
        self.assertEqual(gapped[2]["6d"], 3.0)

    def test_type2_through_type6_cannot_use_old_consecutive_windows_as_current_evidence(self):
        stale_years = {
            "revenue_years": [2008, 2009, 2010, 2011, 2012],
            "net_profit_years": [2008, 2009, 2010, 2011, 2012],
            "margin_years": [2008, 2009, 2010, 2011, 2012],
        }
        type2 = bs.score_type2_two_hot_one_cold(
            base_metrics(**stale_years, market_coldness_score=10.0),
            benchmarks(median_cagr=0.50, median_cagr_count=50),
        )
        self.assertFalse(type2[0])
        self.assertEqual(type2[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("公司拐点", type2[3]["_missing"])

        type3 = bs.score_type3_sustainable_growth(
            complete_type3_metrics(**stale_years),
            benchmarks(),
        )
        self.assertFalse(type3[0])
        self.assertEqual(type3[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("最新完整财年", type3[3]["_missing"])

        type4 = bs.score_type4_long_runway(
            complete_type4_metrics(
                **stale_years,
                fcf_years=[2010, 2011, 2012],
                gross_margin_years=[2010, 2011, 2012],
                indicator_roic_years=[2010, 2011, 2012],
            ),
            benchmarks(),
            complete_dcf_evidence(),
        )
        self.assertFalse(type4[0])
        self.assertEqual(type4[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("厚雪", type4[3]["_missing"])

        stale_type5 = complete_type5_bottom_metrics(
            net_profit_years=list(range(2006, 2016)),
            gross_margin_years=list(range(2006, 2016)),
        )
        type5 = bs.score_type5_counter_cyclical(
            stale_type5,
            benchmarks(),
            history_evidence=type5_history_evidence(),
        )
        self.assertFalse(type5[0])
        self.assertEqual(type5[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("强周期属性", type5[3]["_missing"])

        type6 = bs.score_type6_vc(
            base_metrics(
                market_cap=10e8,
                technology_score=10.0,
                business_model_score=10.0,
                net_profit=-1.0,
                net_margin=-0.01,
                net_profit_history=[-5.0, -3.0, -1.0],
                net_profit_years=[2010, 2011, 2012],
                margin_history=[-0.05, -0.03, -0.01],
                margin_years=[2010, 2011, 2012],
                interim_profit_yoy=None,
                interim_current_profit=None,
                interim_prior_profit=None,
                interim_profit_pair_basis="missing_same_period_comparator",
                position_size_pct=1.0,
                type6_portfolio_pct=8.0,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertFalse(type6[0])
        self.assertEqual(type6[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertIn("反转历史", type6[3]["_missing"])

    def test_financial_indicator_report_date_selects_the_current_complete_annual_year(self):
        metric = base_metrics(
            source_trade_date="2026-03-01",
            financial_indicator_as_of="2024-12-31",
            revenue_values=[100.0, 120.0, 140.0],
            revenue_years=[2022, 2023, 2024],
        )

        self.assertTrue(
            bs._aligned_current_consecutive(
                metric,
                metric["revenue_values"],
                metric["revenue_years"],
                3,
            )
        )
        metric["financial_indicator_as_of"] = None
        self.assertFalse(
            bs._aligned_current_consecutive(
                metric,
                metric["revenue_values"],
                metric["revenue_years"],
                3,
            )
        )

    def test_traceable_normalised_fcf_prevents_latest_year_spike_from_inflating_scores(self):
        spike_source = strict_ttm_source(prior_annual_fcf=10.0, annual_fcf=10.0, ttm_fcf=1_000.0)
        metrics = base_metrics(
            free_cash_flow=1_000.0,
            fcf_history=[10.0, 10.0, 1_000.0],
            market_cap=200.0,
            management_alignment_score=8.0,
            **spike_source,
        )
        payload = complete_dcf_evidence()
        payload.update(
            {
                "base_fcf": 10.0,
                "latest_fcff": 1_000.0,
                "recent_fcff": [10.0, 10.0, 1_000.0],
                "fcf_normalisation_basis": "recent_median",
            }
        )
        payload["ttm_fcff_evidence"] = bs.reconstruct_ttm_fcff(
            spike_source["_ttm_cashflow_history"],
            spike_source["_ttm_cashflow_interim"],
            period_contract=TTM_CONTRACT,
        )

        type1 = bs.score_type1_dcf(metrics, payload, benchmarks())
        cyclical = bs.score_type5_counter_cyclical(
            base_metrics(
                industry="COAL",
                net_profit_history=[300.0, 50.0, 80.0, 120.0],
                roe=0.12,
                pb=0.8,
                pe=120.0,
                net_margin=0.03,
                margin_median_hist=0.10,
                free_cash_flow=1_000.0,
                fcf_history=[10.0, 10.0, 1_000.0],
                market_cap=200.0,
                monetary_funds=50.0,
                total_assets=300.0,
            ),
            benchmarks(),
        )

        self.assertEqual(type1[2]["1c"], 5.0)
        self.assertIn("最新1000.00元", type1[3]["1c"])
        self.assertNotIn("e+", type1[3]["1c"].lower())
        self.assertEqual(cyclical[2]["5e"], 0.0)
        self.assertEqual(cyclical[3]["_status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("FCF", cyclical[3]["5e"])

        inflated = dict(payload)
        inflated["base_fcf"] = 1_000.0
        rejected = bs.score_type1_dcf(metrics, inflated, benchmarks())
        self.assertFalse(rejected[0])
        self.assertEqual(rejected[1], 0.0)

    def test_exact_fifty_percent_current_decline_caps_scores_without_inventing_vetoes(self):
        type_builders = {
            "type1": lambda metric: bs.score_type1_dcf(
                complete_type1_metrics(**{f"interim_{metric}_yoy": -0.50}),
                complete_dcf_evidence(),
                benchmarks(),
            ),
            "type3": lambda metric: bs.score_type3_sustainable_growth(
                complete_type3_metrics(
                    **{f"interim_{metric}_yoy": -0.50},
                ),
                benchmarks(),
            ),
            "type4": lambda metric: bs.score_type4_long_runway(
                complete_type4_metrics(
                    **{f"interim_{metric}_yoy": -0.50},
                ),
                benchmarks(),
                complete_dcf_evidence(),
            ),
            "type6": lambda metric: bs.score_type6_vc(
                primary_type6_metrics(
                    market_cap=10e8,
                    trend_growth=0.30,
                    technology_score=10.0,
                    business_model_score=10.0,
                    net_profit_history=[-3.0, -1.0, 2.0],
                    net_profit=2.0,
                    net_margin=0.02,
                    position_size_pct=3.0,
                    type6_portfolio_pct=10.0,
                    **{f"interim_{metric}_yoy": -0.50},
                ),
                benchmarks(median_cagr=0.60, median_cagr_count=20),
            ),
        }
        capped_items = {"type1": "1d", "type3": "3b", "type4": "4b", "type6": "6d"}
        expected_caps = {"type1": 1.0, "type3": 2.0, "type4": 2.0, "type6": 2.0}
        expected_statuses = {
            "type1": bs.STATUS_OBSERVE,
            "type3": bs.STATUS_TRIGGERED,
            "type4": bs.STATUS_TRIGGERED,
            "type6": bs.STATUS_TRIGGERED,
        }

        for metric in ("profit", "revenue", "ocf"):
            for type_key, build in type_builders.items():
                with self.subTest(metric=metric, type_key=type_key):
                    triggered, _total, scores, reasons = build(metric)
                    self.assertEqual(triggered, type_key in {"type3", "type4", "type6"})
                    self.assertLessEqual(scores[capped_items[type_key]], expected_caps[type_key])
                    self.assertEqual(reasons["_status"], expected_statuses[type_key])
                    self.assertNotIn("_veto", reasons)

    def test_current_period_caps_are_progressive_without_extra_type6_current_period_veto(self):
        for decline, type3_cap, type4_cap, type6_cap in (
            (-0.01, 5.0, 6.0, 4.0),
            (-0.20, 4.0, 4.0, 3.0),
            (-0.90, 2.0, 2.0, 2.0),
        ):
            with self.subTest(decline=decline):
                type3 = bs.score_type3_sustainable_growth(
                    complete_type3_metrics(
                        interim_profit_yoy=decline,
                    ),
                    benchmarks(),
                )
                type4 = bs.score_type4_long_runway(
                    complete_type4_metrics(
                        interim_profit_yoy=decline,
                    ),
                    benchmarks(),
                    complete_dcf_evidence(),
                )
                type6 = bs.score_type6_vc(
                    primary_type6_metrics(
                        market_cap=10e8,
                        trend_growth=0.30,
                        technology_score=10.0,
                        business_model_score=10.0,
                        net_profit_history=[-3.0, -1.0, 2.0],
                        net_profit=2.0,
                        net_margin=0.02,
                        position_size_pct=3.0,
                        type6_portfolio_pct=10.0,
                        interim_profit_yoy=decline,
                    ),
                    benchmarks(median_cagr=0.60, median_cagr_count=20),
                )
                self.assertLessEqual(type3[2]["3b"], type3_cap)
                self.assertLessEqual(type4[2]["4b"], type4_cap)
                self.assertLessEqual(type6[2]["6d"], type6_cap)
                if decline == -0.90:
                    self.assertTrue(type6[0])
                    self.assertEqual(type6[3]["_status"], bs.STATUS_TRIGGERED)
                    self.assertNotIn("_veto", type6[3])

    def test_type3_current_operating_cash_flow_turn_negative_caps_quality_without_inventing_a_veto(self):
        outcome = bs.score_type3_sustainable_growth(
            complete_type3_metrics(
                interim_current_ocf=-1.0,
                interim_ocf_warning=False,
                interim_ocf_yoy=None,
                interim_ocf_yoy_basis="missing_same_period_comparator",
            ),
            benchmarks(),
        )

        self.assertTrue(outcome[0])
        self.assertLessEqual(outcome[2]["3b"], 2.0)
        self.assertEqual(outcome[2]["3d"], 9.0)
        self.assertEqual(outcome[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertNotIn("_veto", outcome[3])

    def test_type6_allows_exact_same_period_losses_and_cash_burn_that_are_improving(self):
        improving = primary_type6_metrics(
            market_cap=10e8,
            trend_growth=0.30,
            technology_score=10.0,
            business_model_score=10.0,
            net_profit_history=[-9.0, -6.0, -3.0],
            net_profit=-3.0,
            net_margin=-0.03,
            profit_1yr_change=0.50,
            position_size_pct=3.0,
            type6_portfolio_pct=10.0,
            interim_current_profit=-1.0,
            interim_profit_yoy=0.50,
            interim_current_ocf=1.0,
            interim_ocf_yoy=2 / 3,
        )

        triggered, total, scores, reasons = bs.score_type6_vc(
            improving,
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )

        self.assertTrue(triggered)
        self.assertGreaterEqual(total, 7.0)
        self.assertEqual(scores["6d"], 8.0)
        self.assertNotIn("_veto", reasons)

        cash_burn = dict(improving)
        cash_burn["interim_current_ocf"] = -1.0
        cash_burn["interim_prior_ocf"] = -3.0
        burn_outcome = bs.score_type6_vc(cash_burn, benchmarks(median_cagr=0.60, median_cagr_count=20))
        self.assertTrue(burn_outcome[0])
        self.assertLessEqual(burn_outcome[2]["6d"], 4.0)
        self.assertIn("现金流仍负但改善", burn_outcome[3]["6d"])
        self.assertNotIn("_veto", burn_outcome[3])

        flat_burn = dict(cash_burn)
        flat_burn["interim_prior_ocf"] = -1.0
        flat_burn["interim_ocf_yoy"] = 0.0
        flat_outcome = bs.score_type6_vc(flat_burn, benchmarks(median_cagr=0.60, median_cagr_count=20))
        self.assertTrue(flat_outcome[0])
        self.assertLessEqual(flat_outcome[2]["6d"], 4.0)
        self.assertIn("现金流仍负持平", flat_outcome[3]["6d"])

        unconfirmed_cash = dict(cash_burn)
        unconfirmed_cash["interim_ocf_yoy"] = None
        unconfirmed_cash["interim_ocf_yoy_basis"] = "invalid_same_period_base"
        unconfirmed_cash["interim_prior_ocf"] = None
        unconfirmed_outcome = bs.score_type6_vc(
            unconfirmed_cash,
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertTrue(unconfirmed_outcome[0])
        self.assertLessEqual(unconfirmed_outcome[2]["6d"], 2.0)
        self.assertIn("经营现金流", unconfirmed_outcome[3]["6d"])
        self.assertNotIn("_veto", unconfirmed_outcome[3])

        collapsing_cash = dict(improving)
        collapsing_cash["interim_current_ocf"] = -3.0
        collapsing_cash["interim_prior_ocf"] = -2.0
        collapsing_cash["interim_ocf_yoy"] = -0.50
        collapsing_outcome = bs.score_type6_vc(
            collapsing_cash,
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertTrue(collapsing_outcome[0])
        self.assertLessEqual(collapsing_outcome[2]["6d"], 2.0)
        self.assertNotIn("_veto", collapsing_outcome[3])

        impossible_revenue = dict(improving)
        impossible_revenue["interim_current_revenue"] = -1.0
        impossible_revenue["interim_revenue_yoy"] = 0.50
        impossible_outcome = bs.score_type6_vc(
            impossible_revenue,
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )
        self.assertTrue(impossible_outcome[0])
        self.assertLessEqual(impossible_outcome[2]["6d"], 2.0)
        self.assertIn("营收为负", impossible_outcome[3]["6d"])
        self.assertNotIn("_veto", impossible_outcome[3])

    def test_all_six_frameworks_have_deterministic_positive_trigger_canaries(self):
        hot_benchmarks = benchmarks(median_cagr=0.50, median_cagr_count=50)
        cyclical_benchmarks = {
            "COAL": dict(benchmarks()["SOFTWARE"], median_cagr=0.10),
            "ALL": benchmarks()["ALL"],
        }
        outcomes = {
            "type1": bs.score_type1_dcf(
                complete_type1_metrics(market_cap=200.0),
                complete_dcf_evidence(),
                hot_benchmarks,
            ),
            "type2": bs.score_type2_two_hot_one_cold(
                base_metrics(
                    revenue_values=[100, 110, 140],
                    margin_history=[0.10, 0.12, 0.16],
                    net_profit_history=[10, 15, 25],
                    ocf_np_ratio=1.2,
                    market_coldness_score=10.0,
                    peg=0.8,
                ),
                hot_benchmarks,
            ),
            "type3": bs.score_type3_sustainable_growth(
                complete_type3_metrics(),
                hot_benchmarks,
            ),
            "type4": bs.score_type4_long_runway(
                complete_type4_metrics(),
                hot_benchmarks,
                complete_dcf_evidence(),
            ),
            "type5": bs.score_type5_counter_cyclical(
                base_metrics(
                    industry="COAL",
                    net_profit_history=[300.0, 50.0, 80.0, 120.0],
                    gross_margin_history=[0.42, 0.21, 0.10, 0.30],
                    gross_margin_years=[2022, 2023, 2024, 2025],
                    roe=0.12,
                    pb=0.8,
                    pe=120.0,
                    net_margin=0.03,
                    margin_median_hist=0.10,
                    monetary_funds=50.0,
                    total_assets=300.0,
                    **trusted_type5_scores(
                        type5_bottom_signal_score=8.0,
                        type5_survival_score=8.0,
                        type5_upside_elasticity_score=8.0,
                        type5_normalized_earnings_score=8.0,
                    ),
                ),
                cyclical_benchmarks,
            ),
            "type6": bs.score_type6_vc(
                primary_type6_metrics(
                    market_cap=10e8,
                    technology_score=10.0,
                    business_model_score=10.0,
                    net_profit_history=[-9.0, -6.0, -3.0],
                    net_profit=-3.0,
                    net_margin=-0.03,
                    profit_1yr_change=0.50,
                    position_size_pct=3.0,
                    type6_portfolio_pct=10.0,
                    interim_current_profit=-1.0,
                    interim_prior_profit=-2.0,
                    interim_profit_yoy=0.50,
                ),
                hot_benchmarks,
            ),
        }

        for type_key, (triggered, total, _scores, reasons) in outcomes.items():
            with self.subTest(type_key=type_key):
                self.assertTrue(triggered)
                self.assertGreaterEqual(total, bs.QUALIFY_THRESHOLD)
                self.assertNotIn("_veto", reasons)
                self.assertNotIn("_condition", reasons)

    def test_improving_current_loss_caps_all_types_but_only_patch6_vetoes_are_reported(self):
        current_loss = {
            "interim_current_profit": -1.0,
            "interim_prior_profit": -2.0,
            "interim_profit_yoy": 0.50,
        }
        bench = benchmarks(median_cagr=0.50, median_cagr_count=50)
        outcomes = {
            "type1": bs.score_type1_dcf(
                complete_type1_metrics(market_cap=200.0, **current_loss),
                complete_dcf_evidence(),
                bench,
            ),
            "type3": bs.score_type3_sustainable_growth(
                complete_type3_metrics(**current_loss),
                bench,
            ),
            "type4": bs.score_type4_long_runway(
                complete_type4_metrics(
                    **current_loss,
                ),
                bench,
                complete_dcf_evidence(),
            ),
        }

        type1 = outcomes["type1"]
        self.assertTrue(type1[0])
        self.assertLessEqual(type1[2]["1d"], 1.0)
        self.assertEqual(type1[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertNotIn("_veto", type1[3])

        type3 = outcomes["type3"]
        self.assertTrue(type3[0])
        self.assertEqual(type3[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertNotIn("_veto", type3[3])

        type4 = outcomes["type4"]
        self.assertTrue(type4[0])
        self.assertLessEqual(type4[2]["4b"], 2.0)
        self.assertEqual(type4[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertNotIn("_veto", type4[3])

    def test_score_explanations_do_not_call_arbitrary_current_period_a_midyear_report(self):
        self.assertNotIn("中报", inspect.getsource(bs))

    def test_all_six_types_return_exact_subscore_shape_and_short_reasons(self):
        m = base_metrics(technology_score=8, business_model_score=8)
        outcomes = [
            bs.score_type1_dcf(m, complete_dcf_evidence(), benchmarks()),
            bs.score_type2_two_hot_one_cold(m, benchmarks()),
            bs.score_type3_sustainable_growth(m, benchmarks()),
            bs.score_type4_long_runway(m, benchmarks()),
            bs.score_type5_counter_cyclical(m, benchmarks()),
            bs.score_type6_vc(m, benchmarks()),
        ]
        for type_key, (_, total, scores, reasons) in zip(bs.TYPE_WEIGHTS, outcomes):
            self.assertEqual(set(scores), set(bs.TYPE_WEIGHTS[type_key]))
            self.assertEqual(total, bs._weighted_total(scores, bs.TYPE_WEIGHTS[type_key]))
            self.assertTrue(all(math.isfinite(value) and 0 <= value <= 10 for value in scores.values()))
            self.assertTrue(all(len(reasons[key]) <= bs.EVIDENCE_MAX_LENGTH for key in scores))

    def test_randomized_boundaries_never_emit_nonfinite_scores(self):
        rng = random.Random(7715)
        options = [None, -1.0, 0.0, 0.01, 0.05, 0.2, 1.0, 10.0, float("nan"), float("inf")]
        bench = {"ALL": {"median_pe": 25, "median_margin": 0.08, "neutral_benchmark": 0.08}}
        scorers = (
            lambda m: bs.score_type1_dcf(m, {}, bench),
            lambda m: bs.score_type2_two_hot_one_cold(m, bench),
            lambda m: bs.score_type3_sustainable_growth(m, bench),
            lambda m: bs.score_type4_long_runway(m, bench),
            lambda m: bs.score_type5_counter_cyclical(m, bench),
            lambda m: bs.score_type6_vc(m, bench),
        )
        for _ in range(250):
            profits = [rng.uniform(-100, 200) for _ in range(rng.randrange(0, 6))]
            m = base_metrics(
                **{
                    key: rng.choice(options)
                    for key in (
                        "price",
                        "pe",
                        "pb",
                        "market_cap",
                        "cagr_3yr",
                        "cagr_5yr",
                        "trend_growth",
                        "growth_1yr",
                        "growth_slope",
                        "growth_consistency",
                        "profit_1yr_change",
                        "profit_volatility",
                        "net_profit",
                        "net_margin",
                        "margin_median_hist",
                        "margin_trajectory",
                        "roe",
                        "debt_ratio",
                        "oper_cf",
                        "free_cash_flow",
                        "ocf_np_ratio",
                        "ocf_3yr_change",
                        "total_assets",
                        "monetary_funds",
                        "interest_debt",
                        "roic",
                        "wacc",
                    )
                }
            )
            m["net_profit_history"] = profits
            m["revenue_values"] = [abs(value) + 1 for value in profits]
            m["margin_history"] = [rng.uniform(-1, 1) for _ in profits]
            for type_key, scorer in zip(bs.TYPE_WEIGHTS, scorers):
                triggered, total, scores, reasons = scorer(m)
                self.assertTrue(math.isfinite(total), type_key)
                self.assertEqual(set(scores), set(bs.TYPE_WEIGHTS[type_key]))
                self.assertTrue(all(math.isfinite(value) and 0 <= value <= 10 for value in scores.values()), type_key)
                self.assertFalse(triggered and "_veto" in reasons, type_key)


class TestMarketScreen(unittest.TestCase):
    def test_full_table_invariant_never_labels_na_or_insufficient_rows_as_vetoed(self):
        outcomes = {}
        for type_key, weights in bs.TYPE_WEIGHTS.items():
            scores = {key: 1.0 for key in weights}
            reasons = {key: "部分证据" for key in weights}
            if type_key == "type6":
                reasons["_veto"] = "不得泄漏到N/A状态"
                outcomes[type_key] = bs._finish(
                    type_key,
                    scores,
                    reasons,
                    veto=True,
                    applicable=False,
                )
            else:
                outcomes[type_key] = bs._finish(
                    type_key,
                    scores,
                    reasons,
                    veto=False,
                    evidence_complete=False,
                )

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=outcomes["type1"]),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=outcomes["type2"]),
            patch.object(bs, "score_type3_sustainable_growth", return_value=outcomes["type3"]),
            patch.object(bs, "score_type4_long_runway", return_value=outcomes["type4"]),
            patch.object(bs, "score_type5_counter_cyclical", return_value=outcomes["type5"]),
            patch.object(bs, "score_type6_vc", return_value=outcomes["type6"]),
        ):
            result = bs.screen_all_types({"1": {}}, quotes)

        self.assertEqual(bs.validate_screening_result(result), [])
        self.assertIsNone(result.iloc[0]["diagnostic_type"])
        self.assertIsNone(result.iloc[0]["max_score"])
        for type_key in bs.TYPE_WEIGHTS:
            payload = result.iloc[0][type_key]
            self.assertFalse(payload["veto"], type_key)
            self.assertFalse(payload["triggered"], type_key)
            self.assertNotIn("_veto", payload["reasons"], type_key)

    def test_nontradable_risk_and_reference_quotes_keep_scores_but_never_trigger(self):
        def outcome(type_key):
            weights = bs.TYPE_WEIGHTS[type_key]
            return True, 8.0, {key: 8.0 for key in weights}, {key: "证据" for key in weights}

        outcomes = {key: outcome(key) for key in bs.TYPE_WEIGHTS}
        quotes = pd.DataFrame(
            [
                {"code": "1", "name": "甲", "price": 1, "tradable": False},
                {"code": "2", "name": "乙", "price": 1, "risk_status": "ST"},
                {"code": "3", "name": "丙", "price": 1, "is_reference_price": True},
                {
                    "code": "4",
                    "name": "丁",
                    "price": 1,
                    "trade_price": 0,
                    "reference_price": 1,
                    "quote_status": "suspended_or_no_trade",
                    "price_source": "previous_close",
                },
            ]
        )
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=outcomes["type1"]),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=outcomes["type2"]),
            patch.object(bs, "score_type3_sustainable_growth", return_value=outcomes["type3"]),
            patch.object(bs, "score_type4_long_runway", return_value=outcomes["type4"]),
            patch.object(bs, "score_type5_counter_cyclical", return_value=outcomes["type5"]),
            patch.object(bs, "score_type6_vc", return_value=outcomes["type6"]),
        ):
            result = bs.screen_all_types({"1": {}, "2": {}, "3": {}, "4": {}}, quotes)

        self.assertTrue(all(types == [] for types in result["buy_types"]))
        for _, row in result.iterrows():
            self.assertTrue(all(row[key]["veto"] for key in ("type1", "type2", "type3", "type4", "type5", "type6")))
            self.assertFalse(row["type7"]["veto"])
            self.assertEqual(row["type7"]["status"], bs.STATUS_INSUFFICIENT_EVIDENCE)

    def test_market_block_preserves_existing_na_missing_veto_and_internal_block_states(self):
        def complete_scores(type_key):
            return {key: 8.0 for key in bs.TYPE_WEIGHTS[type_key]}

        def complete_reasons(type_key):
            return {key: "证据" for key in bs.TYPE_WEIGHTS[type_key]}

        outcomes = {
            "type1": bs._not_applicable("type1", "模型不适用"),
            "type2": bs._insufficient_evidence("type2", "证据缺失"),
            "type3": bs._finish(
                "type3",
                {**complete_scores("type3"), "3a": 2.0},
                {**complete_reasons("type3"), "_veto": "公司否决"},
                veto=True,
            ),
            "type4": bs._valuation_skip_outcome(
                "type4",
                {"category": "internal_error", "reason": "fixture_internal_error"},
            ),
            "type5": bs._finish("type5", complete_scores("type5"), complete_reasons("type5")),
            "type6": bs._finish("type6", complete_scores("type6"), complete_reasons("type6")),
        }
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "tradable": False}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=outcomes["type1"]),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=outcomes["type2"]),
            patch.object(bs, "score_type3_sustainable_growth", return_value=outcomes["type3"]),
            patch.object(bs, "score_type4_long_runway", return_value=outcomes["type4"]),
            patch.object(bs, "score_type5_counter_cyclical", return_value=outcomes["type5"]),
            patch.object(bs, "score_type6_vc", return_value=outcomes["type6"]),
        ):
            row = bs.screen_all_types({"1": {}}, quotes).iloc[0]

        self.assertEqual(row["type1"]["status"], bs.STATUS_NOT_APPLICABLE)
        self.assertEqual(row["type2"]["status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(row["type3"]["status"], bs.STATUS_VETOED)
        self.assertEqual(row["type4"]["status"], bs.STATUS_BLOCKED)
        self.assertNotIn("_veto", row["type4"]["reasons"])
        self.assertEqual(row["type5"]["status"], bs.STATUS_BLOCKED)
        self.assertEqual(row["type6"]["status"], bs.STATUS_BLOCKED)
        self.assertEqual(row["type2"]["decision"]["decision_basis"], "market_block")
        self.assertTrue(row["type2"]["decision"]["decision_complete"])
        self.assertFalse(row["type2"]["decision"]["potentially_triggerable"])

    def test_normalized_financial_code_collision_is_rejected(self):
        quotes = pd.DataFrame([{"code": "000001", "name": "甲", "price": 1}])
        with self.assertRaisesRegex(ValueError, "财务.*000001"):
            bs.screen_all_types({"1": {}, "000001": {}}, quotes)

    def test_normalized_quote_and_dcf_collisions_are_rejected(self):
        duplicate_quotes = pd.DataFrame(
            [
                {"code": "1", "name": "甲", "price": 1},
                {"code": "000001", "name": "甲", "price": 1},
            ]
        )
        with self.assertRaisesRegex(ValueError, "行情.*000001"):
            bs.screen_all_types({"1": {}}, duplicate_quotes)
        quotes = pd.DataFrame([{"code": "000001", "name": "甲", "price": 1}])
        with self.assertRaisesRegex(ValueError, "DCF.*000001"):
            bs.screen_all_types({"1": {}}, quotes, dcf_results={"1": {}, "000001": {}})

    def test_financial_company_without_a_quote_is_rejected_instead_of_silently_dropped(self):
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1}])

        with self.assertRaisesRegex(ValueError, "财务全集.*缺少行情.*000002"):
            bs.screen_all_types({"1": {}, "2": {}}, quotes)

    def test_bulk_market_coldness_evidence_is_injected_before_type2_scoring(self):
        captured = {}

        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        def capture_type2(metric, _benchmarks):
            captured.update(metric)
            return neutral_outcome("type2")

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": "2026-07-24"}])
        fields = production_coldness_fields(as_of="2026-07-24", target_score=8.0)
        evidence = {
            "000001": {
                "market_coldness_score": fields["market_coldness_score"],
                "market_coldness_score_evidence_level": fields["market_coldness_score_evidence_level"],
                "market_coldness_score_evidence": fields["market_coldness_score_evidence"],
                "components": fields["market_coldness_components"],
            }
        }
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", side_effect=capture_type2),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
        ):
            bs.screen_all_types({"1": {}}, quotes, market_coldness_evidence=evidence)

        self.assertEqual(captured["market_coldness_score"], 8.0)
        self.assertEqual(
            captured["market_coldness_score_evidence"]["evidence_id"],
            "patch6-type2c-quantity-price-v1:000001:20260724",
        )
        self.assertEqual(captured["market_coldness_score_evidence_level"], "derived_proxy")

    def test_invalid_bulk_market_coldness_evidence_is_rejected(self):
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0}])
        invalid = {"000001": {"market_coldness_score": 7.0}}

        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            self.assertRaisesRegex(ValueError, "市场冷度证据无效"),
        ):
            bs.screen_all_types({"1": {}}, quotes, market_coldness_evidence=invalid)

    def test_type3_preflight_builds_and_loads_deep_evidence_for_a_viable_candidate(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        metric = complete_type3_metrics(
            code="000001",
            source_trade_date="2026-07-17",
            goodwill_history=[0.0, 0.0, 0.0, 0.0, 0.0],
            goodwill_years=[2021, 2022, 2023, 2024, 2025],
        )
        for key in ("growth_quality_score", "growth_sustainability_score"):
            metric.pop(key, None)
            metric.pop(f"{key}_evidence", None)
            metric.pop(f"{key}_evidence_level", None)

        partial_evidence = {
            "growth_quality_score": {
                "score": 5.0,
                "evidence_level": "partial",
                "evidence": score_evidence("growth_quality_score"),
                "details": {
                    "score_before_evidence_cap": 9.0,
                    "evidence_quality": {
                        "missing_inputs": ["acquisition_cash_and_goodwill_history"],
                    },
                },
            },
            "growth_sustainability_score": {
                "score": 5.0,
                "evidence_level": "partial",
                "evidence": score_evidence("growth_sustainability_score"),
                "details": {
                    "score_before_evidence_cap": 9.0,
                    "evidence_quality": {
                        "missing_inputs": ["segment_growth_sources"],
                    },
                },
            },
        }

        def fake_enrich(metrics, _benchmarks, *, target_codes):
            self.assertIsNone(target_codes)
            metrics[0]["quantitative_evidence"] = copy.deepcopy(partial_evidence)
            metrics[0]["quantitative_evidence_levels"] = {key: "partial" for key in partial_evidence}
            return {"000001": {}}, {"000001": copy.deepcopy(partial_evidence)}

        loader_calls = []
        loaded_record = {"code": "000001", "marker": "deep-growth-loaded"}

        def loader(requests, *, progress_cb):
            loader_calls.append((requests, progress_cb))
            return {"000001": loaded_record}

        def accept_loaded_growth(_evidence, *, code, as_of):
            self.assertEqual((code, as_of), ("000001", "2026-07-17"))
            return {"status": "complete"}, {"status": "complete"}

        def refresh_growth(metric_, _context, _benchmarks):
            for key, score in (("growth_quality_score", 8.0), ("growth_sustainability_score", 9.0)):
                metric_[key] = score
                metric_[f"{key}_evidence"] = score_evidence(key)
                metric_[f"{key}_evidence_level"] = "derived_proxy"

        def fake_type7(
            _metric,
            _type1,
            _history,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "research_request_needed": False,
                "history_request_needed": False,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }

        quotes = pd.DataFrame(
            [
                {
                    "code": "1",
                    "name": "高增长样本",
                    "price": 50.0,
                    "source_trade_date": "2026-07-17",
                }
            ]
        )
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "extract_metrics", side_effect=lambda *_args, **_kwargs: copy.deepcopy(metric)),
            patch.object(bs, "enrich_metrics", side_effect=fake_enrich),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
            patch.object(bs, "_type3_growth_components_from_evidence", side_effect=accept_loaded_growth),
            patch.object(bs, "_refresh_type3_quantitative_evidence", side_effect=refresh_growth),
        ):
            result = bs.screen_all_types(
                {"1": {}},
                quotes,
                type3_growth_loader=loader,
            )
            deferred = bs.screen_all_types(
                {"1": {}},
                quotes,
                type3_growth_loader=lambda _requests, *, progress_cb: {},
            )

        self.assertEqual(len(loader_calls), 1)
        requests, progress = loader_calls[0]
        self.assertIsNone(progress)
        self.assertEqual(
            requests,
            [
                {
                    "code": "000001",
                    "as_of": "2026-07-17",
                    "revenue_records": [
                        {"year": year, "value": value}
                        for year, value in zip(
                            [2021, 2022, 2023, 2024, 2025],
                            [100.0, 115.0, 135.0, 160.0, 190.0],
                        )
                    ],
                    "goodwill_records": [{"year": year, "value": 0.0} for year in [2021, 2022, 2023, 2024, 2025]],
                }
            ],
        )
        self.assertNotEqual(result.iloc[0]["type3"]["status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        row = result.iloc[0]
        self.assertEqual(row["growth_quality_score"], 8.0)
        self.assertEqual(row["growth_quality_score_evidence"], score_evidence("growth_quality_score"))
        self.assertEqual(row["growth_quality_score_evidence_level"], "derived_proxy")
        self.assertEqual(row["growth_sustainability_score"], 9.0)
        self.assertEqual(row["growth_sustainability_score_evidence"], score_evidence("growth_sustainability_score"))
        self.assertEqual(row["growth_sustainability_score_evidence_level"], "derived_proxy")
        self.assertEqual(
            deferred.iloc[0]["type3"]["status"],
            bs.STATUS_INSUFFICIENT_EVIDENCE,
        )

    def test_type3_growth_request_keeps_segment_load_when_goodwill_history_is_missing(self):
        metric = complete_type3_metrics(
            code="000001",
            source_trade_date="2026-07-17",
            goodwill_history=[],
            goodwill_years=[],
        )

        request = bs._type3_growth_request(metric)

        self.assertIsNotNone(request)
        self.assertEqual(request["goodwill_records"], [])
        self.assertEqual(len(request["revenue_records"]), 5)

    def test_type3_growth_request_keeps_four_year_entry_coverage_for_segment_evidence(self):
        metric = complete_type3_metrics(
            code="000001",
            source_trade_date="2026-07-17",
            revenue_values=[100.0, 115.0, 135.0, 160.0],
            revenue_years=[2022, 2023, 2024, 2025],
            goodwill_history=[],
            goodwill_years=[],
        )

        request = bs._type3_growth_request(metric)

        self.assertIsNotNone(request)
        self.assertEqual(
            request["revenue_records"],
            [
                {"year": 2022, "value": 100.0},
                {"year": 2023, "value": 115.0},
                {"year": 2024, "value": 135.0},
                {"year": 2025, "value": 160.0},
            ],
        )
        self.assertEqual(request["goodwill_records"], [])

    def test_type3_growth_preflight_requests_either_single_deep_evidence_gap(self):
        def partial(key, missing_input):
            return {
                "score": 5.0,
                "evidence_level": "partial",
                "evidence": score_evidence(key),
                "details": {
                    "score_before_evidence_cap": 8.0,
                    "evidence_quality": {"missing_inputs": [missing_input]},
                },
            }

        complete = {
            "score": 8.0,
            "evidence_level": "derived_proxy",
            "evidence": score_evidence("complete"),
            "details": {},
        }
        eligible_outcome = bs._finish(
            "type3",
            {key: 6.0 for key in bs.TYPE_WEIGHTS["type3"]},
            {key: "证据" for key in bs.TYPE_WEIGHTS["type3"]},
        )
        cases = (
            (
                "growth_quality_score",
                partial("growth_quality_score", "acquisition_cash_and_goodwill_history"),
                "growth_sustainability_score",
                complete,
            ),
            (
                "growth_sustainability_score",
                partial("growth_sustainability_score", "segment_growth_sources"),
                "growth_quality_score",
                complete,
            ),
        )
        for missing_key, missing_payload, complete_key, complete_payload in cases:
            metric = {
                "quantitative_evidence": {
                    missing_key: missing_payload,
                    complete_key: complete_payload,
                }
            }
            with (
                self.subTest(missing_key=missing_key),
                patch.object(
                    bs,
                    "score_type3_sustainable_growth",
                    return_value=eligible_outcome,
                ),
            ):
                self.assertTrue(bs._type3_growth_request_needed(metric, {}))

    def test_type3_growth_priority_puts_conclusion_unlocks_before_diagnostics(self):
        metric = {
            "quantitative_evidence": {
                "growth_quality_score": {
                    "evidence_level": "partial",
                    "details": {
                        "score_before_evidence_cap": 8.0,
                        "evidence_quality": {
                            "missing_inputs": ["acquisition_cash_and_goodwill_history"],
                        },
                    },
                },
                "growth_sustainability_score": {
                    "evidence_level": "partial",
                    "details": {
                        "score_before_evidence_cap": 9.0,
                        "evidence_quality": {
                            "missing_inputs": ["segment_growth_sources"],
                        },
                    },
                },
            }
        }
        scores = {key: 5.0 for key in bs.TYPE_WEIGHTS["type3"]}
        conclusive = bs._finish(
            "type3",
            scores,
            {key: "证据" for key in scores},
            evidence_complete=False,
            missing_dimensions=["3b", "3d"],
        )
        diagnostic_only = bs._finish(
            "type3",
            scores,
            {key: "证据" for key in scores},
            evidence_complete=False,
            missing_dimensions=["3a", "3b", "3d", "3e"],
        )

        conclusive_priority = bs._type3_growth_request_priority(metric, conclusive)
        diagnostic_priority = bs._type3_growth_request_priority(metric, diagnostic_only)

        self.assertLess(conclusive_priority, diagnostic_priority)
        self.assertEqual(conclusive_priority[:2], (0, 0))
        self.assertEqual(diagnostic_priority[:2], (1, 2))

    def test_type7_dcf_flag_is_independent_from_type1_overall_evidence(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        incomplete_type1 = bs._finish(
            "type1",
            {key: 4.0 for key in bs.TYPE_WEIGHTS["type1"]},
            {**{key: "测试证据" for key in bs.TYPE_WEIGHTS["type1"]}, "_missing": "仅催化剂证据不足"},
            evidence_complete=False,
        )
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": "2026-07-17"}])
        for expected in (True, False):
            captured = []

            def fake_type7(
                _metric,
                _type1,
                _history,
                *,
                valuation_evidence_complete,
                type5_outcome=None,
                other_type_triggered=False,
                captured=captured,
            ):
                captured.append(valuation_evidence_complete)
                return bs._not_applicable("type7", "测试桩不评价第七类"), {
                    "applicable": False,
                    "history_request_needed": False,
                    "research_request_needed": False,
                    "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                    "triggered": False,
                }

            with (
                self.subTest(validated_dcf=expected),
                patch.object(bs, "classify_industry", return_value="SOFTWARE"),
                patch.object(
                    bs,
                    "extract_metrics",
                    return_value={"industry": "SOFTWARE", "source_trade_date": "2026-07-17"},
                ),
                patch.object(bs, "enrich_metrics", return_value=({}, {})),
                patch.object(bs, "score_type1_dcf", return_value=incomplete_type1),
                patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
                patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
                patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
                patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
                patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
                patch.object(bs, "_valid_nonfinancial_dcf_evidence", return_value=expected),
                patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
            ):
                bs.screen_all_types({"1": {}}, quotes)

            self.assertEqual(captured, [expected])

    def test_type7_history_loader_runs_only_after_preflight_requests_the_candidate(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        history = {
            "available": True,
            "code": "000001",
            "as_of": "2026-07-17",
            "model_id": "type7-market-history-v1",
        }
        type7_calls = []

        def fake_type7(
            _metric,
            _type1,
            history_evidence,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            type7_calls.append(history_evidence)
            ledger = {
                "applicable": False,
                "history_request_needed": history_evidence is None,
                "loaded_marker": history_evidence is history,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }
            return bs._not_applicable("type7", "测试桩不评价第七类"), ledger

        loader_calls = []

        def loader(requests, *, progress_cb):
            loader_calls.append((requests, progress_cb))
            return {"000001": history}

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": "2026-07-17"}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            result = bs.screen_all_types({"1": {}}, quotes, quality_history_loader=loader)

        self.assertEqual(loader_calls, [([{"code": "000001", "as_of": "2026-07-17"}], None)])
        self.assertEqual(type7_calls, [None, history])
        self.assertTrue(result.iloc[0]["type7"]["ledger"]["loaded_marker"])

    def test_partial_history_record_does_not_block_a_component_refresh_request(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        partial = {
            "available": False,
            "code": "000001",
            "as_of": "2026-07-17",
            "model_id": "type7-market-history-v1",
            "shareholder_return": {"available": False},
            "valuation_history": {"available": True},
        }
        complete = {
            **partial,
            "available": True,
            "shareholder_return": {"available": True},
        }
        type7_calls = []

        def fake_type7(
            _metric,
            _type1,
            history_evidence,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            type7_calls.append(history_evidence)
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "history_request_needed": history_evidence is partial,
                "research_request_needed": False,
                "loaded_marker": history_evidence is complete,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }

        loader_calls = []

        def loader(requests, *, progress_cb):
            loader_calls.append((requests, progress_cb))
            return {"000001": complete}

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": "2026-07-17"}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            result = bs.screen_all_types(
                {"1": {}},
                quotes,
                quality_history_evidence={"000001": partial},
                quality_history_loader=loader,
            )

        request = [{"code": "000001", "as_of": "2026-07-17"}]
        self.assertEqual(loader_calls, [(request, None)])
        self.assertEqual(type7_calls, [partial, complete])
        self.assertTrue(result.iloc[0]["type7"]["ledger"]["loaded_marker"])

    def test_type7_patch4_loader_replays_bound_announcement_before_history_or_reports(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        code = "000001"
        as_of = "2026-07-17"
        evidence_date = "2026-07-16"
        art_code = "AN" + "1" * 18
        content_hash = "a" * 64
        detail_url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
        evidence_id = f"eastmoney-notice:{code}:{art_code}:sha256:{content_hash[:16]}"

        def criterion(value):
            return {
                "value": value,
                "evidence": {
                    "source": "东方财富上市公司公告正文",
                    "evidence_id": evidence_id,
                    "url": detail_url,
                    "as_of": evidence_date,
                    "summary": f"公告正文明确陈述，正文SHA-256前16位：{content_hash[:16]}",
                },
            }

        values = {
            "core_rd_ownership_pct": 6.0,
            "esop_core_talent_coverage_pct": 40.0,
            "long_term_rd_metrics": True,
            "frontline_rd_equity": True,
            "short_term_price_binding": False,
        }
        assessment = {
            "schema_version": 1,
            "model_id": "patch4-technology-shareholder-culture-v1",
            "code": code,
            "as_of": as_of,
            "criteria": {key: criterion(value) for key, value in values.items()},
        }
        record = {
            "available": True,
            "code": code,
            "as_of": as_of,
            "model_id": "patch4-public-announcement-evidence-v2",
            "assessment": assessment,
            "criteria": {
                key: {
                    "status": "known",
                    "reason": "direct_explicit_statement",
                    "value": value,
                    "evidence_id": evidence_id,
                    "documents_checked": 1,
                }
                for key, value in values.items()
            },
            "status": "complete",
            "documents": [
                {
                    "art_code": art_code,
                    "code": code,
                    "as_of": evidence_date,
                    "title": "测试公司2026年限制性股票激励计划公告",
                    "url": detail_url,
                    "plan_id": "2026:限制性股票:未分期",
                    "plan_status": "unrevoked",
                    "page_size": 1,
                    "page_sha256": ["b" * 64],
                    "content_sha256": content_hash,
                    "content_length": 500,
                }
            ],
            "cache_hit": False,
            "cache_diagnostic": "disabled",
            "reason": "",
        }
        type7_calls = []

        def fake_type7(
            metric,
            _type1,
            _history,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            loaded = metric.get("type7_patch4_assessment") == assessment
            type7_calls.append(loaded)
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "model_id": bs.PATCH6_TYPE7_MODEL_ID,
                "classification": {
                    "class_code": "T",
                    "route_complete": True,
                },
                "decision_gates": {
                    "route_path": {
                        "inputs": {
                            "patch4_complete": loaded,
                        },
                    },
                },
                "upper_bound": 9.0,
                "veto": False,
                "history_request_needed": False,
                "research_request_needed": False,
                "loaded_marker": loaded,
                "triggered": False,
            }

        loader_calls = []

        def loader(requests, *, progress_cb):
            loader_calls.append((requests, progress_cb))
            return {code: record}

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": as_of}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(
                bs,
                "extract_metrics",
                return_value={
                    "industry": "SOFTWARE",
                    "source_trade_date": as_of,
                    "type7_patch4_assessment": {"untrusted": True},
                },
            ),
            patch.object(bs, "enrich_metrics", return_value=({}, {})),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            result = bs.screen_all_types({"1": {}}, quotes, patch4_loader=loader)

        request = [{"code": code, "as_of": as_of}]
        self.assertEqual(loader_calls, [(request, None)])
        self.assertEqual(type7_calls, [False, True])
        self.assertTrue(result.iloc[0]["type7"]["ledger"]["loaded_marker"])

    def test_type7_loads_exact_history_before_report_metadata_and_preserves_both_records(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        history = {"available": True, "code": "000001", "as_of": "2026-07-17"}
        report = type7_report_evidence()
        calls = []

        def fake_type7(
            metric,
            _type1,
            history_evidence,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            report_ready = len(metric.get("type7_research_sources", [])) == 3
            calls.append((report_ready, history_evidence is history))
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "research_request_needed": history_evidence is history and not report_ready,
                "history_request_needed": history_evidence is None,
                "loaded_marker": report_ready and history_evidence is history,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }

        report_calls = []
        history_calls = []
        loader_order = []

        def report_loader(requests, *, progress_cb):
            loader_order.append("report_metadata")
            report_calls.append((requests, progress_cb))
            return {"000001": report}

        def history_loader(requests, *, progress_cb):
            loader_order.append("market_history")
            history_calls.append((requests, progress_cb))
            return {"000001": history}

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": "2026-07-17"}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            result = bs.screen_all_types(
                {"1": {}},
                quotes,
                research_report_loader=report_loader,
                quality_history_loader=history_loader,
            )

        request = [{"code": "000001", "as_of": "2026-07-17"}]
        self.assertEqual(report_calls, [(request, None)])
        self.assertEqual(history_calls, [(request, None)])
        self.assertEqual(loader_order, ["market_history", "report_metadata"])
        self.assertEqual(calls, [(False, False), (False, True), (True, True)])
        self.assertTrue(result.iloc[0]["type7"]["ledger"]["loaded_marker"])

    def test_type7_skips_report_metadata_network_when_exact_history_is_decisive(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        history = {"available": True, "code": "000001", "as_of": "2026-07-17"}
        type7_calls = []

        def fake_type7(
            _metric,
            _type1,
            history_evidence,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            type7_calls.append(history_evidence)
            exact_history_loaded = history_evidence is history
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "research_request_needed": False,
                "history_request_needed": not exact_history_loaded,
                "decisively_not_triggered": exact_history_loaded,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }

        history_calls = []
        report_calls = []

        def history_loader(requests, *, progress_cb):
            history_calls.append((requests, progress_cb))
            return {"000001": history}

        def report_loader(requests, *, progress_cb):
            report_calls.append((requests, progress_cb))
            raise AssertionError("decisive candidate must not fetch report metadata")

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0, "source_trade_date": "2026-07-17"}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            bs.screen_all_types(
                {"1": {}},
                quotes,
                research_report_loader=report_loader,
                quality_history_loader=history_loader,
            )

        request = [{"code": "000001", "as_of": "2026-07-17"}]
        self.assertEqual(history_calls, [(request, None)])
        self.assertEqual(report_calls, [])
        self.assertEqual(type7_calls, [None, history])

    def test_type7_research_loader_batches_more_than_two_thousand_companies_without_loss(self):
        requests = [{"code": f"{index:06d}", "as_of": "2026-07-17"} for index in range(1, 2_002)]
        batch_sizes = []

        def loader(batch, *, progress_cb):
            self.assertIs(progress_cb, self)
            batch_sizes.append(len(batch))
            return {request["code"]: {"code": request["code"]} for request in batch}

        loaded = bs._load_research_report_batches(loader, requests, progress_cb=self)

        self.assertEqual(batch_sizes, [2_000, 1])
        self.assertEqual(len(loaded), 2_001)
        self.assertEqual(set(loaded), {request["code"] for request in requests})

    def test_type5_shared_history_loader_is_reachable_and_skips_impossible_or_non_cycle_names(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        metrics = {
            "000001": complete_type5_bottom_metrics(),
            "000002": complete_type5_bottom_metrics(
                code="000002",
                **trusted_type5_scores(
                    code="000002",
                    type5_cycle_attribute_score=7.0,
                    type5_survival_score=0.0,
                    type5_upside_elasticity_score=0.0,
                    type5_normalized_earnings_score=0.0,
                ),
                **type5_coldness_fields(code="000002"),
            ),
            "000003": complete_type5_bottom_metrics(
                code="000003",
                industry="SOFTWARE",
                **type5_coldness_fields(code="000003"),
            ),
        }

        def coldness_record(code):
            fields = type5_coldness_fields(code=code)
            return {
                "market_coldness_score": fields["market_coldness_score"],
                "market_coldness_score_evidence_level": fields["market_coldness_score_evidence_level"],
                "market_coldness_score_evidence": fields["market_coldness_score_evidence"],
                "components": fields["market_coldness_components"],
            }

        loader_calls = []

        def loader(requests, *, progress_cb):
            loader_calls.append((requests, progress_cb))
            return {"000001": type5_history_evidence()}

        def fake_type7(
            _metric,
            _type1,
            _history,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "history_request_needed": False,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }

        quotes = pd.DataFrame(
            [
                {
                    "code": code,
                    "name": f"样本{code}",
                    "price": 1.0,
                    "source_trade_date": "2026-07-17",
                }
                for code in ("1", "2", "3")
            ]
        )
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(
                bs,
                "extract_metrics",
                side_effect=lambda _fin, quote, _industry: copy.deepcopy(metrics[str(quote["code"]).zfill(6)]),
            ),
            patch.object(bs, "enrich_metrics", return_value=({}, {})),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            result = bs.screen_all_types(
                {"1": {}, "2": {}, "3": {}},
                quotes,
                market_coldness_evidence={code: coldness_record(code) for code in ("000001", "000002", "000003")},
                quality_history_loader=loader,
            )

        self.assertEqual(loader_calls, [([{"code": "000001", "as_of": "2026-07-17"}], None)])
        by_code = result.set_index("code")
        self.assertEqual(by_code.loc["000001", "type5"]["status"], bs.STATUS_TRIGGERED)
        self.assertEqual(by_code.loc["000001", "source_trade_date"], "2026-07-17")
        self.assertEqual(
            by_code.loc["000001", "type5"]["bottom_evidence_contract"]["model_id"],
            bs.TYPE5_BOTTOM_EVIDENCE_MODEL_ID,
        )
        self.assertEqual(by_code.loc["000001", "type5"]["bottom_evidence_mode"], "automatic_replay")
        self.assertEqual(by_code.loc["000002", "type5"]["status"], bs.STATUS_INSUFFICIENT_EVIDENCE)
        self.assertNotIn("bottom_evidence_contract", by_code.loc["000002", "type5"])
        self.assertEqual(by_code.loc["000002", "type5"]["bottom_evidence_mode"], "incomplete")
        self.assertEqual(by_code.loc["000003", "type5"]["status"], bs.STATUS_NOT_APPLICABLE)
        self.assertEqual(by_code.loc["000003", "type5"]["bottom_evidence_mode"], "not_applicable")

    def test_type7_is_not_scored_twice_when_preflight_history_does_not_change(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        type7_calls = []

        def fake_type7(
            _metric,
            _type1,
            history_evidence,
            *,
            valuation_evidence_complete,
            type5_outcome=None,
            other_type_triggered=False,
        ):
            self.assertIsInstance(valuation_evidence_complete, bool)
            type7_calls.append(history_evidence)
            return bs._not_applicable("type7", "测试桩不评价第七类"), {
                "applicable": False,
                "history_request_needed": False,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=neutral_outcome("type2")),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
            patch.object(bs, "score_type7_quality_equity", side_effect=fake_type7),
        ):
            bs.screen_all_types({"1": {}}, quotes)

        self.assertEqual(type7_calls, [None])

    def test_preloaded_type7_history_rejects_unknown_codes_and_non_mapping_records(self):
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0}])
        cases = [
            ({"600001": {}}, "不在财务全集"),
            ({"000001": "invalid"}, "必须为映射"),
        ]
        for evidence, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                bs.screen_all_types({"1": {}}, quotes, quality_history_evidence=evidence)

    def test_bear_case_is_three_deterministic_evidence_driven_items(self):
        payload = {
            "sub_scores": {"1a": 4.0, "1b": 4.0, "1c": 2.0, "1d": 2.0},
            "reasons": {
                "1a": "价格证据",
                "1b": "陷阱证据",
                "1c": "现金流偏弱",
                "1d": "催化剂不足",
                "_veto": "价值陷阱未排除",
            },
        }
        result = bs._build_bear_case("type1", payload)
        self.assertEqual(
            result,
            [
                {"dimension": "_veto", "score": 2.0, "reason": "价值陷阱未排除"},
                {"dimension": "1c", "score": 2.0, "reason": "现金流偏弱"},
                {"dimension": "1d", "score": 2.0, "reason": "催化剂不足"},
            ],
        )
        tied = bs._build_bear_case(
            "type1",
            {
                "sub_scores": {key: 5.0 for key in bs.TYPE_WEIGHTS["type1"]},
                "reasons": {key: key for key in bs.TYPE_WEIGHTS["type1"]},
            },
        )
        self.assertEqual([item["dimension"] for item in tied], ["1b", "1a", "1c"])

    def test_negative_pe_is_excluded_from_benchmark(self):
        metrics = [
            {"industry": "A", "pe": -100, "pb": 1, "roe": 0.1, "cagr_3yr": 0.1, "net_margin": 0.1, "debt_ratio": 0.2},
            {"industry": "A", "pe": 20, "pb": 2, "roe": 0.2, "cagr_3yr": 0.2, "net_margin": 0.2, "debt_ratio": 0.3},
        ]
        result = bs.build_sector_benchmarks(metrics)
        self.assertEqual(result["A"]["median_pe"], 20.0)

    def test_company_benchmark_view_excludes_its_own_relative_values(self):
        metrics = [
            {
                "code": "000001",
                "industry": "BANK",
                "pe": 10.0,
                "pb": 1.0,
                "cagr_3yr": 0.10,
                "net_margin": 0.10,
                "net_interest_margin_change": 0.001,
                "nonperforming_loan_ratio_change": -0.003,
                "capital_adequacy_ratio_change": 0.01,
                "new_business_value_growth": 0.10,
                "risk_coverage_ratio_change": 0.001,
                "profit_1yr_change": 0.10,
            },
            {
                "code": "000002",
                "industry": "BANK",
                "pe": 20.0,
                "pb": 2.0,
                "cagr_3yr": 0.20,
                "net_margin": 0.20,
                "net_interest_margin_change": 0.002,
                "nonperforming_loan_ratio_change": -0.002,
                "capital_adequacy_ratio_change": 0.02,
                "new_business_value_growth": 0.20,
                "risk_coverage_ratio_change": 0.002,
                "profit_1yr_change": 0.20,
            },
            {
                "code": "000003",
                "industry": "BANK",
                "pe": 100.0,
                "pb": 10.0,
                "cagr_3yr": 1.00,
                "net_margin": 1.00,
                "net_interest_margin_change": 0.010,
                "nonperforming_loan_ratio_change": 0.010,
                "capital_adequacy_ratio_change": 0.10,
                "new_business_value_growth": 1.00,
                "risk_coverage_ratio_change": 0.010,
                "profit_1yr_change": 1.00,
            },
        ]

        benchmarks = bs.build_sector_benchmarks(metrics)
        target_view = bs._benchmarks_for_code(benchmarks, "000003")

        self.assertEqual(benchmarks["BANK"]["median_pe"], 20.0)
        self.assertEqual(target_view["BANK"]["median_pe"], 15.0)
        self.assertEqual(target_view["BANK"]["median_pb"], 1.5)
        self.assertAlmostEqual(target_view["BANK"]["median_cagr"], 0.15)
        self.assertAlmostEqual(target_view["BANK"]["median_margin"], 0.15)
        self.assertAlmostEqual(target_view["BANK"]["median_nim_change"], 0.0015)
        self.assertAlmostEqual(target_view["BANK"]["median_npl_change"], -0.0025)
        self.assertAlmostEqual(target_view["BANK"]["median_bank_capital_change"], 0.015)
        self.assertAlmostEqual(target_view["BANK"]["median_nbv_growth"], 0.15)
        self.assertAlmostEqual(target_view["BANK"]["median_risk_coverage_change"], 0.0015)
        self.assertAlmostEqual(target_view["BANK"]["median_profit_change"], 0.15)
        self.assertEqual(target_view["BANK"]["median_pe_count"], 2)
        self.assertEqual(target_view["ALL"]["median_pe"], 15.0)

    def test_empty_input_returns_stable_empty_frame(self):
        result = bs.screen_all_types({}, pd.DataFrame())
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), bs.RESULT_COLUMNS)

    def test_output_projection_scores_only_requested_codes_but_keeps_full_benchmarks(self):
        def neutral_outcome(type_key):
            return bs._finish(
                type_key,
                {key: 4.0 for key in bs.TYPE_WEIGHTS[type_key]},
                {key: "测试证据" for key in bs.TYPE_WEIGHTS[type_key]},
            )

        benchmark_population_sizes = []

        original_build_benchmarks = bs.build_sector_benchmarks

        def capture_benchmark_population(metrics):
            benchmark_population_sizes.append(len(metrics))
            return original_build_benchmarks(metrics)

        captured_type2_medians = []

        def capture_type2(_metric, _benchmarks):
            captured_type2_medians.append(_benchmarks["SOFTWARE"]["median_pe"])
            return neutral_outcome("type2")

        quotes = pd.DataFrame(
            [
                {"code": "1", "name": "甲", "price": 1.0, "pe": 10.0},
                {"code": "2", "name": "乙", "price": 2.0, "pe": 100.0},
                {"code": "3", "name": "丙", "price": 3.0, "pe": 30.0},
            ]
        )
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "build_sector_benchmarks", side_effect=capture_benchmark_population),
            patch.object(bs, "score_type1_dcf", return_value=neutral_outcome("type1")),
            patch.object(bs, "score_type2_two_hot_one_cold", side_effect=capture_type2),
            patch.object(bs, "score_type3_sustainable_growth", return_value=neutral_outcome("type3")),
            patch.object(bs, "score_type4_long_runway", return_value=neutral_outcome("type4")),
            patch.object(bs, "score_type5_counter_cyclical", return_value=neutral_outcome("type5")),
            patch.object(bs, "score_type6_vc", return_value=neutral_outcome("type6")),
        ):
            result = bs.screen_all_types(
                {"1": {}, "2": {}, "3": {}},
                quotes,
                output_codes=["2"],
            )

        self.assertEqual(result["code"].tolist(), ["000002"])
        self.assertEqual(benchmark_population_sizes, [3])
        self.assertEqual(captured_type2_medians, [20.0])

        with self.assertRaisesRegex(ValueError, "不在财务全集"):
            bs.screen_all_types({"1": {}}, quotes.iloc[:1], output_codes=["2"])

    def test_priority_true_max_and_deterministic_order(self):
        def outcome(type_key, total, triggered):
            weights = bs.TYPE_WEIGHTS[type_key]
            return triggered, total, {key: total for key in weights}, {key: "证据" for key in weights}

        score_outcomes = {
            "type1": outcome("type1", 7.0, True),
            "type2": outcome("type2", 5.0, False),
            "type3": outcome("type3", 9.0, True),
            "type4": outcome("type4", 6.0, False),
            "type5": outcome("type5", 8.0, True),
            "type6": outcome("type6", 4.0, False),
        }
        quotes = pd.DataFrame(
            [
                {"code": "2", "name": "乙", "price": 2},
                {"code": "1", "name": "甲", "price": 1},
            ]
        )
        fin_map = {"2": {}, "1": {}}
        with (
            patch.object(bs, "begin_industry_generation") as begin_generation,
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=score_outcomes["type1"]),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=score_outcomes["type2"]),
            patch.object(bs, "score_type3_sustainable_growth", return_value=score_outcomes["type3"]),
            patch.object(bs, "score_type4_long_runway", return_value=score_outcomes["type4"]),
            patch.object(bs, "score_type5_counter_cyclical", return_value=score_outcomes["type5"]),
            patch.object(bs, "score_type6_vc", return_value=score_outcomes["type6"]),
        ):
            result = bs.screen_all_types(fin_map, quotes)
        self.assertEqual(result["code"].tolist(), ["000001", "000002"])
        self.assertEqual(result["primary_type"].tolist(), ["type1", "type1"])
        self.assertEqual(result["diagnostic_type"].tolist(), ["type3", "type3"])
        self.assertEqual(result["max_score"].tolist(), [9.0, 9.0])
        self.assertEqual(result.iloc[0]["buy_types"], ["type1", "type5", "type3"])
        self.assertTrue(all(len(items) == 3 for items in result["bear_case"]))
        self.assertEqual(bs.validate_screening_result(result), [])
        for type_key in bs.TYPE_WEIGHTS:
            payload = result.iloc[0][type_key]
            self.assertEqual(set(payload["decision"]), bs._DECISION_FIELDS)
            self.assertEqual(payload["decision"], bs.replay_buy_decision(type_key, payload))
        tampered = copy.deepcopy(result)
        tampered.iloc[0]["type3"]["decision"]["score_upper_bound"] = 0.0
        self.assertTrue(any("type3决策边界" in error for error in bs.validate_screening_result(tampered)))

        suppressed = copy.deepcopy(result)
        suppressed_type5 = suppressed.iloc[0]["type5"]
        suppressed_type5["reasons"]["_status"] = bs.STATUS_OBSERVE
        suppressed_type5["status"] = bs.STATUS_OBSERVE
        suppressed_type5["triggered"] = False
        suppressed_type5["decision"] = bs.replay_buy_decision("type5", suppressed_type5)
        suppressed.at[suppressed.index[0], "buy_types"] = ["type1", "type3"]
        self.assertTrue(any("type5模型触发重放错误" in error for error in bs.validate_screening_result(suppressed)))

        fake_veto = copy.deepcopy(result)
        fake_veto_type2 = fake_veto.iloc[0]["type2"]
        fake_veto_type2["reasons"]["_status"] = bs.STATUS_VETOED
        fake_veto_type2["reasons"]["_veto"] = "伪造公司否决"
        fake_veto_type2["status"] = bs.STATUS_VETOED
        fake_veto_type2["veto"] = True
        fake_veto_type2["decision"] = bs.replay_buy_decision("type2", fake_veto_type2)
        self.assertTrue(any("type2否决缺少模型依据" in error for error in bs.validate_screening_result(fake_veto)))

        fake_market_block = copy.deepcopy(result)
        fake_market_type2 = fake_market_block.iloc[0]["type2"]
        fake_market_type2["reasons"]["_status"] = bs.STATUS_BLOCKED
        fake_market_type2["reasons"]["_veto"] = "伪造市场阻断"
        fake_market_type2["reasons"][bs._DECISION_MARKET_BLOCK_REASON] = "伪造市场阻断"
        fake_market_type2["status"] = bs.STATUS_BLOCKED
        fake_market_type2["veto"] = True
        fake_market_type2["decision"] = bs.replay_buy_decision("type2", fake_market_type2)
        self.assertTrue(
            any("type2市场阻断标记错误" in error for error in bs.validate_screening_result(fake_market_block))
        )

        begin_generation.assert_called_once_with()
        duplicated = pd.concat([result, result.iloc[[0]]], ignore_index=True)
        self.assertTrue(any("code重复" in error for error in bs.validate_screening_result(duplicated)))

    def test_no_trigger_means_no_primary_buy_framework_but_keeps_diagnostic_context(self):
        def outcome(type_key, total):
            weights = bs.TYPE_WEIGHTS[type_key]
            return False, total, {key: total for key in weights}, {key: "证据" for key in weights}

        score_outcomes = {
            "type1": outcome("type1", 4.0),
            "type2": outcome("type2", 6.0),
            "type3": outcome("type3", 5.0),
            "type4": outcome("type4", 6.0),
            "type5": outcome("type5", 3.0),
            "type6": outcome("type6", 2.0),
        }
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1}])
        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            patch.object(bs, "score_type1_dcf", return_value=score_outcomes["type1"]),
            patch.object(bs, "score_type2_two_hot_one_cold", return_value=score_outcomes["type2"]),
            patch.object(bs, "score_type3_sustainable_growth", return_value=score_outcomes["type3"]),
            patch.object(bs, "score_type4_long_runway", return_value=score_outcomes["type4"]),
            patch.object(bs, "score_type5_counter_cyclical", return_value=score_outcomes["type5"]),
            patch.object(bs, "score_type6_vc", return_value=score_outcomes["type6"]),
        ):
            result = bs.screen_all_types({"1": {}}, quotes)

        row = result.iloc[0]
        self.assertEqual(row["buy_types"], [])
        self.assertIsNone(row["primary_type"])
        self.assertEqual(row["primary_label"], "无触发（不买）")
        self.assertEqual(row["diagnostic_type"], "type2")
        self.assertEqual(row["max_score"], 6.0)
        self.assertEqual(bs.validate_screening_result(result), [])


if __name__ == "__main__":
    unittest.main()
