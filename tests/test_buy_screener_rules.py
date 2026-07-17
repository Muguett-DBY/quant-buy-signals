import copy
import inspect
import math
import random
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd

from data.capex_evidence import resolve_capex_evidence
from data.datacenter import RPT_MAIN_FINANCIAL_INDICATORS
from engine import buy_screener as bs
from engine import scenarios


def score_evidence(key: str) -> dict[str, str]:
    result = {
        "source": "unit-test-fixture",
        "evidence_id": f"fixture-{key}",
        "as_of": "2026-07-15",
    }
    return result


TTM_CONTRACT = bs.ReportingPeriodContract(
    annual_report_date="2025-12-31",
    current_interim_report_date="2026-03-31",
    prior_interim_report_date="2025-03-31",
)


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
            metrics[f"{key}_evidence"] = score_evidence(key)
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
        "median_roe": 0.12,
        "median_cagr": 0.10,
        "median_margin": 0.08,
        "median_debt": 0.45,
        "median_cagr_count": 20,
        "neutral_benchmark": 0.15,
    }
    bucket.update(overrides)
    return {"SOFTWARE": bucket, "ALL": dict(bucket)}


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


class TestMetricExtraction(unittest.TestCase):
    def test_metric_extraction_preserves_exact_rows_for_strict_ttm_source_binding(self):
        source = strict_ttm_source()
        extracted = bs.extract_metrics(
            {
                "revenue_history": source["_ttm_revenue_history"],
                "cashflow": source["_ttm_cashflow_history"],
                "income_interim": source["_ttm_income_interim"],
                "cashflow_interim": source["_ttm_cashflow_interim"],
            },
            {"code": "000001", "name": "样本", "market": "SZ"},
            "SOFTWARE",
        )

        for key, expected in source.items():
            self.assertEqual(extracted[key], expected)

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
                "business_model_score": 7.0,
                "business_model_score_evidence": score_evidence("business_model_score"),
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )
        invalid = bs.extract_metrics(
            {
                "technology_score": 11.0,
                "technology_score_evidence": score_evidence("technology_score"),
                "business_model_score": -1.0,
                "business_model_score_evidence": score_evidence("business_model_score"),
            },
            {"code": "1", "name": "样本"},
            "SOFTWARE",
        )

        self.assertEqual(valid["technology_score"], 8.0)
        self.assertEqual(valid["business_model_score"], 7.0)
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
        self.assertEqual(scores["2c"], 10.0)
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
            peg=0.8,
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
                        peg=0.8,
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
                peg=0.8,
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
        self.assertIn("ROIC", reasons["3c"])
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
        m = base_metrics(profit_1yr_change=-0.15, growth_consistency=0.1)
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

    def test_type3_below_ten_percent_trend_is_not_applicable_not_zero_score_failure(self):
        outcome = bs.score_type3_sustainable_growth(
            base_metrics(trend_growth=0.0999),
            benchmarks(),
        )

        self.assertFalse(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_NOT_APPLICABLE)
        self.assertEqual(outcome[3]["_applicable"], "no")
        self.assertIn("不足10%", outcome[3]["_scope"])
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
        self.assertIn("中性终局", reasons["4d"])

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
            if type_key in {"type3", "type6"}:
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
        evidence = {
            "source": "量价模型",
            "evidence_id": "bank-cold-2025",
            "as_of": "2025-12-31",
            "summary": "独立市场冷度",
        }
        financial = base_metrics(
            industry="BANK",
            pb=0.70,
            market_coldness_score=8.0,
            market_coldness_score_evidence=evidence,
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

    def test_bank_type5_requires_real_cycle_and_regulatory_survival_evidence(self):
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

        self.assertTrue(outcome[0])
        self.assertEqual(outcome[3]["_status"], bs.STATUS_TRIGGERED)
        self.assertEqual(outcome[3]["_evidence"], "complete")
        self.assertGreaterEqual(outcome[2]["5a"], 7.0)
        self.assertEqual(outcome[2]["5c"], 10.0)

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

    def test_type5_patch6_cycle_stage_veto_requires_complete_history(self):
        m = base_metrics(
            industry="COAL",
            net_profit_history=[300, 50, 80, 120],
            pe=100,
            pb=0.8,
            interim_profit_yoy=-0.50,
        )
        triggered, _, scores, reasons = bs.score_type5_counter_cyclical(m, benchmarks())
        self.assertFalse(triggered)
        self.assertLessEqual(scores["5a"], 3.0)
        self.assertIn("_veto", reasons)
        self.assertEqual(reasons["_status"], bs.STATUS_VETOED)

    def test_type5_requires_5a_seven_and_5c_five(self):
        m = base_metrics(
            industry="COAL",
            net_profit_history=[100, 20, 30, 25, 30],
            pe=120,
            pb=1.4,
            net_margin=0.03,
            margin_median_hist=0.10,
            roe=0.10,
            free_cash_flow=20,
            fcf_history=[10, 15, 20],
            monetary_funds=50,
            total_assets=300,
        )
        triggered, total, scores, reasons = bs.score_type5_counter_cyclical(m, benchmarks())
        self.assertGreaterEqual(total, 7.0)
        self.assertLess(scores["5a"], 7.0)
        self.assertFalse(triggered)
        self.assertIn("_condition", reasons)

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
            base_metrics(
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
            base_metrics(
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
            base_metrics(
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
            base_metrics(
                market_cap=10e8,
                technology_score=10,
                business_model_score=10,
                net_profit_history=[-3, -1, 2],
                net_profit=2,
                net_margin=0.02,
            ),
            benchmarks(median_cagr=0.60, median_cagr_count=20),
        )

        self.assertEqual(scores["6e"], 10.0)
        self.assertFalse(triggered)
        self.assertIn("_condition", reasons)
        self.assertEqual(reasons["_status"], bs.STATUS_CONDITIONAL)

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
        self.assertIn("末1000.00元", type1[3]["1c"])
        self.assertNotIn("e+", type1[3]["1c"].lower())
        self.assertEqual(cyclical[2]["5e"], 9.0)

        inflated = dict(payload)
        inflated["base_fcf"] = 1_000.0
        rejected = bs.score_type1_dcf(metrics, inflated, benchmarks())
        self.assertFalse(rejected[0])
        self.assertEqual(rejected[1], 0.0)

    def test_exact_fifty_percent_current_decline_caps_scores_without_inventing_type4_type6_vetoes(self):
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
            "type5": lambda metric: bs.score_type5_counter_cyclical(
                base_metrics(
                    industry="COAL",
                    net_profit_history=[300.0, 50.0, 80.0, 120.0],
                    roe=0.12,
                    pb=0.8,
                    pe=120.0,
                    net_margin=0.03,
                    margin_median_hist=0.10,
                    monetary_funds=50.0,
                    total_assets=300.0,
                    **{f"interim_{metric}_yoy": -0.50},
                ),
                benchmarks(),
            ),
            "type6": lambda metric: bs.score_type6_vc(
                base_metrics(
                    market_cap=10e8,
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
        capped_items = {"type1": "1d", "type3": "3b", "type4": "4b", "type5": "5a", "type6": "6d"}
        expected_caps = {"type1": 1.0, "type3": 2.0, "type4": 2.0, "type5": 2.0, "type6": 2.0}
        expected_statuses = {
            "type1": bs.STATUS_OBSERVE,
            "type3": bs.STATUS_TRIGGERED,
            "type4": bs.STATUS_TRIGGERED,
            "type5": bs.STATUS_VETOED,
            "type6": bs.STATUS_TRIGGERED,
        }

        for metric in ("profit", "revenue", "ocf"):
            for type_key, build in type_builders.items():
                with self.subTest(metric=metric, type_key=type_key):
                    triggered, _total, scores, reasons = build(metric)
                    self.assertEqual(triggered, type_key in {"type3", "type4", "type6"})
                    self.assertLessEqual(scores[capped_items[type_key]], expected_caps[type_key])
                    self.assertEqual(reasons["_status"], expected_statuses[type_key])
                    if type_key in {"type1", "type3", "type4", "type6"}:
                        self.assertNotIn("_veto", reasons)
                    else:
                        self.assertIn("_veto", reasons)

    def test_current_period_caps_are_progressive_without_extra_type6_current_period_veto(self):
        for decline, type3_cap, type4_cap, type5_cap, type6_cap in (
            (-0.01, 5.0, 6.0, 6.0, 4.0),
            (-0.20, 4.0, 4.0, 4.0, 3.0),
            (-0.90, 2.0, 2.0, 2.0, 2.0),
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
                type5 = bs.score_type5_counter_cyclical(
                    base_metrics(
                        industry="COAL",
                        net_profit_history=[300.0, 50.0, 80.0, 120.0],
                        roe=0.12,
                        pb=0.8,
                        pe=120.0,
                        net_margin=0.03,
                        margin_median_hist=0.10,
                        monetary_funds=50.0,
                        total_assets=300.0,
                        interim_profit_yoy=decline,
                    ),
                    benchmarks(),
                )
                type6 = bs.score_type6_vc(
                    base_metrics(
                        market_cap=10e8,
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
                self.assertLessEqual(type5[2]["5a"], type5_cap)
                self.assertLessEqual(type6[2]["6d"], type6_cap)
                if decline == -0.90:
                    self.assertFalse(type5[0])
                    self.assertIn("_veto", type5[3])
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
        improving = base_metrics(
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
                    roe=0.12,
                    pb=0.8,
                    pe=120.0,
                    net_margin=0.03,
                    margin_median_hist=0.10,
                    monetary_funds=50.0,
                    total_assets=300.0,
                ),
                cyclical_benchmarks,
            ),
            "type6": bs.score_type6_vc(
                base_metrics(
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
            "type5": bs.score_type5_counter_cyclical(
                base_metrics(
                    industry="COAL",
                    net_profit_history=[300.0, 50.0, 80.0, 120.0],
                    roe=0.12,
                    pb=0.8,
                    pe=120.0,
                    net_margin=0.03,
                    margin_median_hist=0.10,
                    monetary_funds=50.0,
                    total_assets=300.0,
                    **current_loss,
                ),
                {"COAL": benchmarks()["SOFTWARE"], "ALL": benchmarks()["ALL"]},
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

        type5 = outcomes["type5"]
        self.assertFalse(type5[0])
        self.assertLessEqual(type5[2]["5a"], 2.0)
        self.assertLessEqual(type5[2]["5c"], 3.0)
        self.assertEqual(type5[3]["_veto"], "周期阶段不符合")
        self.assertIn("5a≥7且5c≥5", type5[3]["_condition"])

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
            self.assertTrue(all(row[key]["veto"] for key in bs.TYPE_WEIGHTS))

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

        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0}])
        evidence = {
            "000001": {
                "market_coldness_score": 7.4,
                "market_coldness_score_evidence": {
                    "source": "Eastmoney quantity-price model",
                    "evidence_id": "coldness:000001:20260716",
                    "as_of": date.today().isoformat(),
                    "summary": "60日/YTD/换手/量比",
                },
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

        self.assertEqual(captured["market_coldness_score"], 7.4)
        self.assertEqual(captured["market_coldness_score_evidence"]["evidence_id"], "coldness:000001:20260716")

    def test_invalid_bulk_market_coldness_evidence_is_rejected(self):
        quotes = pd.DataFrame([{"code": "1", "name": "甲", "price": 1.0}])
        invalid = {"000001": {"market_coldness_score": 7.0}}

        with (
            patch.object(bs, "classify_industry", return_value="SOFTWARE"),
            self.assertRaisesRegex(ValueError, "市场冷度证据无效"),
        ):
            bs.screen_all_types({"1": {}}, quotes, market_coldness_evidence=invalid)

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

        def fake_type7(_metric, _type1, history_evidence):
            type7_calls.append(history_evidence)
            ledger = {
                "history_request_needed": history_evidence is None,
                "loaded_marker": history_evidence is history,
                "scores": {"template1": 40.0, "template5": 40.0, "patch5": 40.0},
                "triggered": False,
            }
            return neutral_outcome("type7"), ledger

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
            patch.object(bs, "validate_quality_equity_ledger", return_value=[]),
        ):
            result = bs.screen_all_types({"1": {}}, quotes, quality_history_loader=loader)

        self.assertEqual(loader_calls, [([{"code": "000001", "as_of": "2026-07-17"}], None)])
        self.assertEqual(type7_calls, [None, history])
        self.assertTrue(result.iloc[0]["type7"]["ledger"]["loaded_marker"])

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

        def capture_type2(_metric, _benchmarks):
            return neutral_outcome("type2")

        quotes = pd.DataFrame(
            [
                {"code": "1", "name": "甲", "price": 1.0},
                {"code": "2", "name": "乙", "price": 2.0},
                {"code": "3", "name": "丙", "price": 3.0},
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
