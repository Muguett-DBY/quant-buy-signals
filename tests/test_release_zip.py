from __future__ import annotations

import csv
from copy import deepcopy
from functools import lru_cache
import hashlib
import io
import json
import random
import stat
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from engine.audit import _RULE_FILES as _AUDIT_RULE_FILES
from engine.buy_screener import STATUS_INSUFFICIENT_EVIDENCE, score_type7_quality_equity
from engine.market_coldness import MARKET_COLDNESS_MODEL_ID
from engine.quality_equity import TYPE7_DIRECT_SCORE_KEYS
from tools.run_full_audit import (
    _canonical_market_coldness_json,
    _replay_market_coldness_reference_artifact,
)
from tools.verify_release_zip import (
    _REQUIRED_FILES,
    _RULE_FILES as _RELEASE_RULE_FILES,
    _desktop_launcher_errors,
    _git_tree_entries,
    _normalised_file_names,
    verify_release_zip,
)


_EXPECTED_RULE_FILES = {
    "config.py",
    "data/capex_evidence.py",
    "data/datacenter.py",
    "data/financial_indicator_evidence.py",
    "data/financial_source_evidence.py",
    "data/financial_balance_sheet_evidence.json",
    "data/financial_zero_capex_evidence.json",
    "data/financial_zero_revenue_evidence.json",
    "data/growth_evidence.py",
    "data/industry.py",
    "data/market_coldness.py",
    "data/market_history.py",
    "data/quality_history.py",
    "data/research_reports.py",
    "data/trading_calendar.py",
    "engine/buy_screener.py",
    "engine/dcf.py",
    "engine/market_coldness.py",
    "engine/quantitative_evidence.py",
    "engine/quality_equity.py",
    "engine/risk.py",
    "engine/scenarios.py",
    "engine/valuation_status.py",
    "tools/china_a_share_trading_calendar.json",
}
_LICENSE_BYTES = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()


def _verify(path):
    return verify_release_zip(str(path), repository=None)


def _hash_files(files, selected):
    digest = hashlib.sha256()
    for name in sorted(path for path in selected if path in files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _eligible_codes():
    return [f"{code:06d}" for code in range(1, 2201)] + [f"{code:06d}" for code in range(600001, 602001)]


@lru_cache(maxsize=1)
def _market_coldness_audit_fixture(analysis_codes: tuple[str, ...], eligible_codes: tuple[str, ...]):
    retrieved_at = "2026-07-15T08:05:00Z"
    artifact = {
        "schema_version": 1,
        "model_id": MARKET_COLDNESS_MODEL_ID,
        "source": "Eastmoney push2 clist",
        "source_url": "https://push2delay.eastmoney.com/api/qt/clist/get",
        "retrieved_at": retrieved_at,
        "as_of_session": "2026-07-15",
        "listed_codes": list(analysis_codes),
        "source_record_count": len(analysis_codes),
        "records": [
            [
                code,
                "2010-01-01",
                round(-30.0 + (index % 101) * 0.5, 2),
                round(-40.0 + (index % 121) * 0.6, 2),
                round(0.25 + (index % 80) * 0.1, 2),
                round(0.4 + (index % 40) * 0.05, 2),
            ]
            for index, code in enumerate(analysis_codes)
        ],
    }
    replay = _replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible_codes,
        as_of_session="2026-07-15",
    )
    evidence = replay["eligible_evidence"]
    not_applicable = replay["eligible_not_applicable_codes_by_reason"]
    data_gaps = replay["eligible_unscored_data_gap_codes_by_reason"]
    summary = {
        "provided": True,
        "evidence_count": len(evidence),
        "eligible_evidence_count": len(evidence),
        "eligible_evidence_coverage": len(evidence) / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_market_coldness_json(evidence)).hexdigest(),
        "sources": ["Eastmoney push2 clist; https://push2delay.eastmoney.com/api/qt/clist/get"],
        "as_of_sessions": ["2026-07-15"],
    }
    status = {
        "available": True,
        "evidence_available": True,
        "evidence_reason": "available",
        "model_id": MARKET_COLDNESS_MODEL_ID,
        "source": "Eastmoney push2 clist",
        "source_url": "https://push2delay.eastmoney.com/api/qt/clist/get",
        "retrieved_at": retrieved_at,
        "as_of_session": "2026-07-15",
        "reference_artifact_sha256": hashlib.sha256(_canonical_market_coldness_json(artifact)).hexdigest(),
        "full_listed_evidence_count": len(replay["full_evidence"]),
        "eligible_evidence_count": len(evidence),
        "eligible_evidence_coverage": len(evidence) / len(eligible_codes),
        "eligible_applicable_count": len(evidence),
        "eligible_applicable_evidence_coverage": 1.0,
        "eligible_not_applicable_count": sum(len(codes) for codes in not_applicable.values()),
        "eligible_not_applicable_codes_by_reason": not_applicable,
        "eligible_unscored_data_gap_count": sum(len(codes) for codes in data_gaps.values()),
        "eligible_unscored_data_gap_codes_by_reason": data_gaps,
    }
    return artifact, summary, status


_REPORTING_PERIOD_CONTRACT = {
    "annual_report_date": "2025-12-31",
    "current_interim_report_date": "2026-03-31",
    "prior_interim_report_date": "2025-03-31",
    "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
}


def _fixture_dcf_value(*, base_fcf, base_revenue, growth, wacc, terminal_g, shares, net_debt, retention):
    current_margin = base_fcf / base_revenue
    target_margin = min(current_margin, max(0.0, 0.04, current_margin * retention))
    explicit_pv = 0.0
    for year in range(1, 6):
        revenue = base_revenue * (1.0 + growth) ** year
        margin = current_margin + (target_margin - current_margin) * ((year - 1) / 4.0)
        discount = (1.0 + wacc) ** year
        explicit_pv += revenue * margin / discount
    terminal_fcf = revenue * (1.0 + terminal_g) * target_margin
    terminal_value = terminal_fcf / (wacc - terminal_g)
    return (explicit_pv + terminal_value / discount - net_debt) / shares


def _reported_capex_provenance(report_date, value):
    return {
        "schema_version": 1,
        "status": "complete",
        "evidence_label": "fact_source_reported",
        "value": value,
        "source_report": "RPT_DMSK_FN_CASHFLOW",
        "source_field": "CONSTRUCT_LONG_ASSET",
        "report_date": report_date,
        "formula": "source_reported",
        "derivation_method": None,
        "components": {"reported_value": value},
        "source_null_fields": [],
        "source_url": "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "source_query": {
            "report_name": "RPT_DMSK_FN_CASHFLOW",
            "report_date": report_date,
            "source": "WEB",
            "client": "PC",
        },
    }


def _strict_ttm_evidence(metric):
    period = {
        "basis": _REPORTING_PERIOD_CONTRACT["period_basis"],
        "annual_report_date": _REPORTING_PERIOD_CONTRACT["annual_report_date"],
        "current_interim_report_date": _REPORTING_PERIOD_CONTRACT["current_interim_report_date"],
        "prior_interim_report_date": _REPORTING_PERIOD_CONTRACT["prior_interim_report_date"],
    }
    if metric == "revenue":
        values = {
            "annual": ("2025-12-31", 1_000_000_000.0),
            "current_interim": ("2026-03-31", 300_000_000.0),
            "prior_interim": ("2025-03-31", 250_000_000.0),
        }
        components = {
            label: {
                "report_date": report_date,
                "row_count": 1,
                "revenue": value,
                "revenue_source_field": "TOTAL_OPERATE_INCOME",
            }
            for label, (report_date, value) in values.items()
        }
        value = 1_050_000_000.0
        components["reconstructed_revenue"] = value
        formula_version = "ttm_revenue_v1"
        cash_flow_kind = "reported_revenue"
    else:
        values = {
            "annual": ("2025-12-31", 180_000_000.0, -50_000_000.0),
            "current_interim": ("2026-03-31", 50_000_000.0, -15_000_000.0),
            "prior_interim": ("2025-03-31", 45_000_000.0, -10_000_000.0),
        }
        components = {
            label: {
                "report_date": report_date,
                "row_count": 1,
                "operating_cash_flow": operating_cash_flow,
                "operating_cash_flow_source_field": "NETCASH_OPERATE",
                "capex_raw": capex_raw,
                "capex_absolute": abs(capex_raw),
                "capex_source_field": "CONSTRUCT_LONG_ASSET",
                "capex_provenance": _reported_capex_provenance(report_date, capex_raw),
                "capex_provenance_status": "complete",
            }
            for label, (report_date, operating_cash_flow, capex_raw) in values.items()
        }
        components.update(
            {
                "reconstructed_operating_cash_flow": 185_000_000.0,
                "reconstructed_capex": 55_000_000.0,
                "reconstructed_fcff": 130_000_000.0,
            }
        )
        value = 130_000_000.0
        formula_version = "ttm_cfo_less_capex_v2"
        cash_flow_kind = "cfo_less_capex_proxy"
    return {
        "status": "complete",
        "value": value,
        "metric": metric,
        "formula_version": formula_version,
        "cash_flow_kind": cash_flow_kind,
        "period_basis": _REPORTING_PERIOD_CONTRACT["period_basis"],
        "period": period,
        "unit": "CNY",
        "components": components,
    }


def _valuation_result(code):
    base_fcf = 130_000_000.0
    base_revenue = 1_050_000_000.0
    shares = 100_000_000.0
    net_debt = 100_000_000.0
    params = {
        "pessimistic": {
            "growth": 0.0,
            "wacc_base": 0.09,
            "terminal_g": 0.0,
            "margin_retention": 0.7,
            "forecast_years": 5,
        },
        "neutral": {
            "growth": 0.02,
            "wacc_base": 0.08,
            "terminal_g": 0.01,
            "margin_retention": 0.8,
            "forecast_years": 5,
        },
        "optimistic": {
            "growth": 0.04,
            "wacc_base": 0.075,
            "terminal_g": 0.02,
            "margin_retention": 0.9,
            "forecast_years": 5,
        },
    }
    points = {}
    for scenario, scenario_params in params.items():
        common = {
            "base_fcf": base_fcf,
            "base_revenue": base_revenue,
            "growth": scenario_params["growth"],
            "terminal_g": scenario_params["terminal_g"],
            "shares": shares,
            "net_debt": net_debt,
            "retention": scenario_params["margin_retention"],
        }
        points[scenario] = {
            "lower": _fixture_dcf_value(wacc=scenario_params["wacc_base"] + 0.005, **common),
            "upper": _fixture_dcf_value(wacc=scenario_params["wacc_base"] - 0.005, **common),
        }
    buy_boundary = (points["pessimistic"]["upper"] + points["neutral"]["lower"]) / 2.0
    sell_boundary = (points["neutral"]["upper"] + points["optimistic"]["lower"]) / 2.0
    valuation_center = (points["neutral"]["lower"] + points["neutral"]["upper"]) / 2.0
    current_price = 10.0
    pessimistic_upper = points["pessimistic"]["upper"]
    return {
        "code": code,
        "name": f"样本{code}",
        "industry_code": "SOFTWARE",
        "current_price": current_price,
        "dcf_points": points,
        "explicit_forecast_years": 5,
        "zone": "买入区",
        "safety_score": "★★ 中度安全边际",
        "safety_margin_pct": round((pessimistic_upper - current_price) / pessimistic_upper * 100.0, 2),
        "bubble_warning": False,
        "mean1": buy_boundary,
        "mean2": sell_boundary,
        "valuation_center": valuation_center,
        "neutral_value_midpoint": valuation_center,
        "dcf_value_mean": buy_boundary,
        "dcf_value_mean_legacy_alias_of": "buy_zone_upper",
        "buy_zone_upper": buy_boundary,
        "sell_zone_lower": sell_boundary,
        "params": params,
        "base_wacc": 0.08,
        "base_fcf": base_fcf,
        "base_revenue": base_revenue,
        "valuation_input_basis": "strict_ttm",
        "base_revenue_basis": "strict_ttm_reported_revenue",
        "base_fcf_basis": "normalised_two_annual_plus_ttm_cfo_less_capex_proxy",
        "ttm_fcff_evidence": _strict_ttm_evidence("fcff"),
        "ttm_revenue_evidence": _strict_ttm_evidence("revenue"),
        "shares_outstanding": shares,
        "latest_fcff": 130_000_000.0,
        "recent_fcff": [120_000_000.0, 130_000_000.0, 130_000_000.0],
        "recent_fcff_periods": [
            {"kind": "annual", "report_date": "2024-12-31"},
            {"kind": "annual", "report_date": "2025-12-31"},
            {"kind": "ttm", "through_report_date": "2026-03-31"},
        ],
        "fcf_normalisation_basis": "recent_median",
        "fcf_normalisation_period_basis": "two_annual_plus_strict_ttm",
        "fcf_normalisation_period": {
            "period_set": "two_annual_plus_strict_ttm",
            "periods": [
                {"kind": "annual", "report_date": "2024-12-31"},
                {"kind": "annual", "report_date": "2025-12-31"},
                {"kind": "ttm", "through_report_date": "2026-03-31"},
            ],
            "normalisation_method": "recent_median",
            "cash_flow_kind": "cfo_less_capex_proxy",
            "formula_version": "ttm_cfo_less_capex_v2",
        },
        "base_fcf_adjustments": [],
        "normalisation_premium_cap": 1.25,
        "fcf_margin_ceiling": 0.20,
        "net_debt": net_debt,
        "tax_shield_rate": 0.0,
        "tax_shield_source": "taxable_profit_evidence_unavailable",
        "wacc_components": {
            "equity_weight": 1.0,
            "debt_weight": 0.0,
            "cost_of_equity": 0.08,
            "pre_tax_cost_of_debt": 0.05,
            "tax_shield_rate": 0.0,
        },
    }


def _financial_pb_valuation_result(code):
    shares = 100_000_000.0
    bvps = 8.0
    normalised_roe = 0.15
    cost_of_equity = 0.08
    scenario_inputs = {
        "pessimistic": (0.0, 0.12),
        "neutral": (0.01, normalised_roe),
        "optimistic": (0.02, 0.165),
    }
    params = {}
    points = {}
    for scenario, (growth, scenario_roe) in scenario_inputs.items():
        pb_lower = (scenario_roe - growth) / (cost_of_equity + 0.005 - growth)
        pb_upper = (scenario_roe - growth) / (cost_of_equity - 0.005 - growth)
        points[scenario] = {"lower": bvps * pb_lower, "upper": bvps * pb_upper}
        params[scenario] = {
            "growth": growth,
            "wacc_base": cost_of_equity,
            "terminal_g": growth,
            "margin_retention": None,
            "normalised_roe": normalised_roe,
            "scenario_roe": scenario_roe,
            "cost_of_equity": cost_of_equity,
            "pb_lower": pb_lower,
            "pb_upper": pb_upper,
            "bvps": bvps,
            "formula": "(normalised_roe - g) / (cost_of_equity - g)",
        }
    buy_boundary = (points["pessimistic"]["upper"] + points["neutral"]["lower"]) / 2.0
    sell_boundary = (points["neutral"]["upper"] + points["optimistic"]["lower"]) / 2.0
    valuation_center = (points["neutral"]["lower"] + points["neutral"]["upper"]) / 2.0
    return {
        "code": code,
        "name": f"样本{code}",
        "industry_code": "BANK",
        "current_price": 10.0,
        "dcf_points": points,
        "zone": "买入区",
        "safety_score": "★★ 中度安全边际",
        "safety_margin_pct": round(
            (points["pessimistic"]["upper"] - 10.0) / points["pessimistic"]["upper"] * 100.0,
            2,
        ),
        "bubble_warning": False,
        "mean1": buy_boundary,
        "mean2": sell_boundary,
        "valuation_center": valuation_center,
        "neutral_value_midpoint": valuation_center,
        "dcf_value_mean": buy_boundary,
        "dcf_value_mean_legacy_alias_of": "buy_zone_upper",
        "buy_zone_upper": buy_boundary,
        "sell_zone_lower": sell_boundary,
        "params": params,
        "base_wacc": cost_of_equity,
        "base_fcf": None,
        "base_revenue": None,
        "shares_outstanding": shares,
        "net_debt": None,
        "_pb_valuation": True,
        "normalised_roe": normalised_roe,
        "tax_shield_rate": 0.0,
        "tax_shield_source": "financial_operating_liabilities_excluded",
        "wacc_components": {
            "equity_weight": 1.0,
            "debt_weight": 0.0,
            "cost_of_equity": cost_of_equity,
            "pre_tax_cost_of_debt": None,
            "tax_shield_rate": 0.0,
        },
    }


def _fixture_type1_1a_from_valuation(result):
    price = float(result["current_price"])
    buy_upper = float(result["buy_zone_upper"])
    depth = (buy_upper - price) / buy_upper
    if depth > 0.20:
        return 9.5
    if depth >= 0.10:
        return 7.5
    if depth >= 0:
        return 5.5
    if depth >= -0.10:
        return 3.5
    return 1.5


def _set_fixture_type1_from_valuation(company, result):
    score = _fixture_type1_1a_from_valuation(result)
    payload = company["type1"]
    payload["sub_scores"]["1a"] = score
    payload["reasons"]["1a"] = "独立估值位置夹具"
    payload["total"] = round(
        payload["sub_scores"]["1a"] * 0.30
        + payload["sub_scores"]["1b"] * 0.35
        + payload["sub_scores"]["1c"] * 0.20
        + payload["sub_scores"]["1d"] * 0.15,
        1,
    )
    payload["triggered"] = False
    payload["veto"] = score <= 2.0
    payload["status"] = "vetoed" if payload["veto"] else "not_triggered"
    payload["applicable"] = True
    payload["evidence_complete"] = True
    payload["reasons"].update(
        {
            "_status": payload["status"],
            "_applicable": "yes",
            "_evidence": "complete",
        }
    )
    if payload["veto"]:
        payload["reasons"]["_veto"] = "买入区深度不足"
    else:
        payload["reasons"].pop("_veto", None)
    company["type1_score"] = payload["total"]


def _set_fixture_type1_from_skip(company, category):
    payload = company["type1"]
    payload["sub_scores"] = {key: 0.0 for key in ("1a", "1b", "1c", "1d")}
    payload["total"] = 0.0
    payload["triggered"] = False
    payload["veto"] = False
    payload["reasons"] = {key: "估值跳过夹具" for key in payload["sub_scores"]}
    if category in {"model_unsupported", "economic_not_applicable"}:
        payload.update(status="not_applicable", applicable=False, evidence_complete=True)
        payload["reasons"]["_scope"] = "估值模型不适用"
    elif category in {"source_missing", "inconsistent_source"}:
        payload.update(status="insufficient_evidence", applicable=True, evidence_complete=False)
        payload["reasons"]["_missing"] = "估值证据不足"
    else:
        payload.update(status="blocked", applicable=True, evidence_complete=False)
        payload["reasons"]["_blocked"] = "估值计算异常"
    payload["reasons"].update(
        {
            "_status": payload["status"],
            "_applicable": "yes" if payload["applicable"] else "no",
            "_evidence": "complete" if payload["evidence_complete"] else "incomplete",
        }
    )
    company["type1_score"] = 0.0


def _convert_first_valuation_to_financial_pb(payload):
    code = next(iter(payload["dcf_results"]))
    payload["dcf_results"][code] = _financial_pb_valuation_result(code)
    company = next(company for company in payload["companies"] if company["code"] == code)
    company["industry"] = "BANK"
    _set_fixture_type1_from_valuation(company, payload["dcf_results"][code])
    coverage = payload["provenance"]["caller_metadata"]["validation"]["strict_ttm_source_coverage"]
    coverage["denominator"] -= 1
    coverage["excluded_financial_codes"] = sorted([*coverage["excluded_financial_codes"], code])
    for metric in ("revenue", "fcff"):
        metric_coverage = coverage[metric]
        metric_coverage["complete_codes"].remove(code)
        metric_coverage["complete"] -= 1
        metric_coverage["status_counts"]["complete"] -= 1
        metric_coverage["coverage"] = 1.0
    _set_fixture_type7_ledger(
        company,
        valuation_evidence_complete=False,
        metric={
            "code": code,
            "industry": "BANK",
            "source_trade_date": "2026-07-15",
        },
    )
    _refresh_fixture_company_summary(company)


_TYPE7_FIXTURE_CACHE = {}


def _set_fixture_type7_ledger(company, *, valuation_evidence_complete, metric=None, history=None):
    code = company["code"]
    type1 = company["type1"]
    type1_outcome = (
        type1["triggered"],
        type1["total"],
        type1["sub_scores"],
        type1["reasons"],
    )
    source_metric = metric or {
        "code": code,
        "industry": company["industry"],
        "source_trade_date": "2026-07-15",
    }
    type1_1a = float(type1["sub_scores"]["1a"])
    cache_key = (valuation_evidence_complete, type1_1a) if metric is None and history is None else None
    cached = _TYPE7_FIXTURE_CACHE.get(cache_key) if cache_key is not None else None
    if cached is None:
        outcome, ledger = score_type7_quality_equity(
            source_metric,
            type1_outcome,
            history,
            valuation_evidence_complete=valuation_evidence_complete,
        )
        if cache_key is not None:
            _TYPE7_FIXTURE_CACHE[cache_key] = deepcopy((outcome, ledger))
    else:
        outcome, ledger = deepcopy(cached)
        ledger["code"] = code
        ledger["prerequisites"]["external_report_content_verification"]["code"] = code
    triggered, total, sub_scores, reasons = outcome
    company["type7_score"] = total
    company["type7"] = {
        "triggered": triggered,
        "total": total,
        "sub_scores": sub_scores,
        "reasons": reasons,
        "veto": bool(reasons.get("_veto")),
        "status": reasons["_status"],
        "applicable": reasons["_status"] != "not_applicable",
        "evidence_complete": reasons.get("_evidence") == "complete",
        "ledger": ledger,
    }


def _audit_payload(files):
    eligible_codes = _eligible_codes()
    sample_codes = sorted(random.Random(20260715).sample(eligible_codes, 100))
    type_dimensions = {
        "type1": ("1a", "1b", "1c", "1d"),
        "type2": ("2a", "2b", "2c", "2d"),
        "type3": ("3a", "3b", "3c", "3d", "3e"),
        "type4": ("4a", "4b", "4c", "4d", "4e", "4f"),
        "type5": ("5a", "5b", "5c", "5d", "5e"),
        "type6": ("6a", "6b", "6c", "6d", "6e"),
        "type7": ("7a", "7b", "7c"),
    }

    def company_row(code):
        row = {
            "code": code,
            "name": f"样本{code}",
            "industry": "SOFTWARE" if code in {sample_codes[0], sample_codes[-1]} else "BANK",
            "price": 10.0,
            "market_cap": 1_000_000_000.0,
            "buy_types": [],
            "num_types": 0,
            "primary_type": None,
            "primary_label": "无触发（不买）",
            "diagnostic_type": "type1",
            "diagnostic_label": "1️⃣ 估值买入区",
            "max_score": 0.0,
            "bear_case": [
                {"dimension": "1b", "score": 0.0, "reason": "审计夹具"},
                {"dimension": "1a", "score": 0.0, "reason": "审计夹具"},
                {"dimension": "1c", "score": 0.0, "reason": "审计夹具"},
            ],
        }
        for type_key, dimensions in type_dimensions.items():
            row[f"{type_key}_score"] = 0.0
            row[type_key] = {
                "triggered": False,
                "total": 0.0,
                "sub_scores": {dimension: 0.0 for dimension in dimensions},
                "reasons": {
                    **{dimension: "审计夹具" for dimension in dimensions},
                    "_status": "not_triggered",
                    "_applicable": "yes",
                    "_evidence": "complete",
                },
                "veto": False,
                "status": "not_triggered",
                "applicable": True,
                "evidence_complete": True,
            }
        type7_reason = "金融需专属优质股权模型"
        row["type7"] = {
            "triggered": False,
            "total": 0.0,
            "sub_scores": {dimension: 0.0 for dimension in type_dimensions["type7"]},
            "reasons": {
                **{dimension: type7_reason for dimension in type_dimensions["type7"]},
                "_scope": type7_reason,
                "_status": "not_applicable",
                "_applicable": "no",
                "_evidence": "complete",
            },
            "veto": False,
            "status": "not_applicable",
            "applicable": False,
            "evidence_complete": True,
            "ledger": {
                "schema_version": 5,
                "model_id": "patch6-type7-quality-equity-v5",
                "code": code,
                "applicable": False,
                "reason": type7_reason,
            },
        }
        return row

    code_files = {
        name
        for name in files
        if name in {"app.py", "config.py"}
        or (name.endswith(".py") and name.split("/", 1)[0] in {"data", "desktop", "engine", "ui", "tools"})
    }
    rule_files = _EXPECTED_RULE_FILES
    industry_files = {
        "data/industry.py",
        "data/industry_f10.json",
        "data/industry_em_map.json",
        "data/industry_capco_2025h2.json",
        "data/industry_exchange_new_listings_2026.json",
    }
    dependency_files = {
        "requirements-bootstrap.txt",
        "requirements.txt",
        "requirements-lock.txt",
        "requirements-test.txt",
        "requirements-dev.txt",
        "requirements-dev-lock.txt",
        "pyproject.toml",
    }
    quality = {
        "ok": True,
        "expected_companies": len(eligible_codes),
        "score_raw_rows": len(eligible_codes),
        "score_rows": len(eligible_codes),
        "score_coverage": 1.0,
        "dcf_attempted": len(eligible_codes),
        "dcf_attempt_coverage": 1.0,
        "dcf_valid": 2_500,
        "dcf_valid_coverage": 2_500 / len(eligible_codes),
        "dcf_skipped": len(eligible_codes) - 2_500,
        "pipeline_issues": 0,
        "pipeline_issue_rate": 0.0,
        "reasons": [],
    }
    analysis_codes = sorted(
        [f"{code:06d}" for code in range(1, 2201)]
        + [f"{code:06d}" for code in range(300001, 300694)]
        + [f"{code:06d}" for code in range(600001, 602308)]
    )
    ineligible_codes = sorted(set(analysis_codes) - set(eligible_codes))
    coldness_artifact, coldness_summary, coldness_status = deepcopy(
        _market_coldness_audit_fixture(tuple(analysis_codes), tuple(eligible_codes))
    )
    excluded_financial_codes = sorted({f"{code:06d}" for code in range(602001, 602031)} | set(sample_codes[1:-1]))
    nonfinancial_codes = sorted(set(analysis_codes) - set(excluded_financial_codes))
    strict_ttm_source_coverage = {
        "population": "SH_SZ_non_financial",
        "denominator": len(nonfinancial_codes),
        "evaluated": True,
        "excluded_financial_codes": excluded_financial_codes,
        "revenue": {
            "complete": len(nonfinancial_codes),
            "missing": 0,
            "coverage": 1.0,
            "status_counts": {"complete": len(nonfinancial_codes)},
            "complete_codes": list(nonfinancial_codes),
            "missing_codes_by_status": {},
        },
        "fcff": {
            "complete": len(nonfinancial_codes),
            "missing": 0,
            "coverage": 1.0,
            "status_counts": {"complete": len(nonfinancial_codes)},
            "complete_codes": list(nonfinancial_codes),
            "missing_codes_by_status": {},
        },
    }
    validation = {
        "analysis_markets": ["SH", "SZ"],
        "quotes": 5_527,
        "market_counts": {"SH": 2_307, "SZ": 2_893, "BJ": 327},
        "analysis_market_quotes": 5_200,
        "trading_quotes": 5_200,
        "trading_coverage": 1.0,
        "analysis_trading_quotes": 5_200,
        "analysis_trading_coverage": 1.0,
        "eligible_trading_quotes": len(eligible_codes),
        "eligible_trading_coverage": 1.0,
        "trading_source_trade_dates": ["2026-07-15"],
        "analysis_eligible_coverage": len(eligible_codes) / 5_200,
        "eligible_companies": len(eligible_codes),
        "analysis_market_codes": analysis_codes,
        "eligible_codes": eligible_codes,
        "analysis_ineligible_codes": ineligible_codes,
        "ineligible_codes": ineligible_codes,
        "unsupported_market_codes": [f"{code:06d}" for code in range(920001, 920328)],
        "listing_date_evidence": {
            "required": True,
            "reference_count": 5_200,
            "reference_coverage": 1.0,
            "listing_date_count": 5_200,
            "listing_date_coverage": 1.0,
            "missing_reference_codes": [],
            "missing_listing_date_codes": [],
            "status_counts": {"reported": 5_200},
            "source": "Eastmoney push2 clist",
            "source_url": "https://push2delay.eastmoney.com/api/qt/clist/get",
            "retrieved_at_oldest": 1_768_478_300.0,
            "retrieved_at_latest": 1_768_478_301.0,
        },
        "reporting_period_contract": dict(_REPORTING_PERIOD_CONTRACT),
        "supplemental_field_coverage": {
            "GOODWILL": 0.83,
            "OBTAIN_SUBSIDIARY_OTHER": 0.79,
        },
        "strict_ttm_source_coverage": strict_ttm_source_coverage,
    }
    payload = {
        "seed": 20260715,
        "sample_size": 100,
        "sample_codes": sample_codes,
        "data_timestamp_utc": "2026-07-15T11:59:00+00:00",
        "dcf_valid": 60,
        "eligible_universe_size": len(eligible_codes),
        "provenance": {
            "audit_schema_version": 3,
            "patch6_source": {
                "path_at_model_authoring": r"E:\模板汇总MD\补丁6.md",
                "sha256": "aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6",
            },
            "type7_source_documents": {
                "template1": {
                    "path_at_model_authoring": r"E:\模板汇总MD\第1模板.md",
                    "sha256": "98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d",
                },
                "template5": {
                    "path_at_model_authoring": r"E:\模板汇总MD\第5模板.md",
                    "sha256": "37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946",
                },
                "patch5": {
                    "path_at_model_authoring": r"E:\模板汇总MD\补丁5.md",
                    "sha256": "8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4",
                },
                "patch6": {
                    "path_at_model_authoring": r"E:\模板汇总MD\补丁6.md",
                    "sha256": "aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6",
                },
            },
            "generated_at_utc": "2026-07-15T12:00:00+00:00",
            "reporting_period_contract": dict(_REPORTING_PERIOD_CONTRACT),
            "snapshot_content_sha256": hashlib.sha256(b"fixture snapshot content").hexdigest(),
            "snapshot_artifact_sha256": hashlib.sha256(b"fixture snapshot artifact").hexdigest(),
            "eligible_universe_sha256": hashlib.sha256("\n".join(eligible_codes).encode("ascii")).hexdigest(),
            "type3_growth_evidence": {
                "provided": True,
                "evidence_count": 2,
                "available_count": 1,
                "eligible_evidence_count": 2,
                "eligible_evidence_coverage": 2 / len(eligible_codes),
                "evidence_sha256": hashlib.sha256(b"fixture Type 3 growth evidence").hexdigest(),
                "as_of_sessions": ["2026-07-15"],
            },
            "research_report_evidence": {
                "provided": True,
                "evidence_count": 3,
                "available_count": 2,
                "eligible_evidence_count": 3,
                "eligible_evidence_coverage": 3 / len(eligible_codes),
                "evidence_sha256": hashlib.sha256(b"fixture Type 7 research report evidence").hexdigest(),
                "as_of_sessions": ["2026-07-15"],
            },
            "market_coldness_evidence": coldness_summary,
            "caller_metadata": {
                "snapshot_schema_version": 8,
                "snapshot_source": "network",
                "snapshot_payload_sha256": hashlib.sha256(b"fixture snapshot payload").hexdigest(),
                "snapshot_artifact_bytes": 123,
                "validation": validation,
                "full_market_quality": quality,
                "market_coldness": coldness_status,
                "market_coldness_reference_artifact": coldness_artifact,
            },
            "git": {"commit": "a" * 40, "dirty": False},
            "code_sha256": _hash_files(files, code_files),
            "rules_sha256": _hash_files(files, rule_files),
            "industry_sha256": _hash_files(files, industry_files),
            "dependency_manifest_sha256": _hash_files(files, dependency_files),
            "runtime": {
                "python": "3.12.10",
                "packages": {
                    "numpy": "2.4.6",
                    "orjson": "3.11.9",
                    "pandas": "3.0.3",
                    "plotly": "6.9.0",
                    "pillow": "12.3.0",
                    "requests": "2.34.2",
                    "streamlit": "1.59.2",
                },
            },
        },
        "analysis_quality": quality,
        "companion_artifacts_sha256": {},
        "engine_self_check_errors": [],
        "same_source_scoring_replay_errors": [],
        "same_source_valuation_replay_errors": [],
        "independent_check_errors": [],
        "invariant_errors": [],
        "pipeline_issues": [],
        "dcf_results": {
            code: _valuation_result(code) if code == sample_codes[0] else _financial_pb_valuation_result(code)
            for code in sample_codes[:60]
        },
        "dcf_skip_reasons": {code: "fixture_skip" for code in sample_codes[60:]},
        "dcf_skip_classifications": {
            code: {"category": "economic_not_applicable", "reason": "fixture_skip"} for code in sample_codes[60:]
        },
        "companies": [company_row(code) for code in sample_codes],
    }
    company_by_code = {company["code"]: company for company in payload["companies"]}
    for code, result in payload["dcf_results"].items():
        _set_fixture_type1_from_valuation(company_by_code[code], result)
    for code, classification in payload["dcf_skip_classifications"].items():
        _set_fixture_type1_from_skip(company_by_code[code], classification["category"])
    first = company_by_code[sample_codes[0]]
    _set_fixture_type7_ledger(
        first,
        valuation_evidence_complete=True,
    )
    _set_fixture_type7_ledger(
        payload["companies"][-1],
        valuation_evidence_complete=False,
    )
    for company in payload["companies"]:
        _refresh_fixture_company_summary(company)
    return payload


def _render_csv(payload):
    rows = []
    for company in payload["companies"]:
        code = company["code"]
        bear_text = "；".join(
            f"{item['dimension']} {item['score']}分:{item['reason']}" for item in company["bear_case"]
        )
        row = {
            "代码": code,
            "名称": company["name"],
            "行业": company["industry"],
            "买入判定": company["primary_label"],
            "诊断框架": company["diagnostic_label"],
            "最高分": company["max_score"],
            "触发类型": ",".join(company["buy_types"]),
            "DCF有效": code in payload["dcf_results"],
            "空头漏洞": bear_text,
        }
        for type_key in ("type1", "type2", "type3", "type4", "type5", "type6", "type7"):
            type_payload = company[type_key]
            row[f"{type_key}总分"] = type_payload["total"]
            row[f"{type_key}触发"] = type_payload["triggered"]
            row[f"{type_key}否决"] = type_payload["veto"]
            for dimension, score in type_payload["sub_scores"].items():
                row[f"{dimension}子分"] = score
                row[f"{dimension}依据"] = type_payload["reasons"][dimension]
            row[f"{type_key}元信息JSON"] = json.dumps(
                {key: value for key, value in type_payload["reasons"].items() if key.startswith("_")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        result = payload["dcf_results"].get(code)
        row.update(
            {
                "DCF状态": "有效" if result else "跳过",
                "DCF跳过原因": "" if result else payload["dcf_skip_reasons"][code],
                "DCF区域": result["zone"] if result else None,
                "DCF当前价": result["current_price"] if result else None,
                "DCF买入区上界": result["buy_zone_upper"] if result else None,
                "DCF卖出区下界": result["sell_zone_lower"] if result else None,
            }
        )
        rows.append(row)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _render_markdown(payload):
    signal_counts = {
        type_key: sum(bool(company[type_key]["triggered"]) for company in payload["companies"])
        for type_key in ("type1", "type2", "type3", "type4", "type5", "type6", "type7")
    }
    lines = [
        "# 固定随机 100 家公司审计",
        "",
        "- seed: `20260715`",
        "- sample_size: `100`",
        f"- eligible_universe_size: `{payload['eligible_universe_size']}`",
        f"- data_timestamp_utc: `{payload['data_timestamp_utc']}`",
        f"- dcf_valid: `{len(payload['dcf_results'])}`",
        f"- dcf_skipped_with_reason: `{len(payload['dcf_skip_reasons'])}`",
        "- pipeline_issues: `0`",
        "- engine_self_check_errors: `0`",
        "- same_source_scoring_replay_errors: `0`",
        "- same_source_valuation_replay_errors: `0`",
        "- independent_check_errors: `0`",
        f"- triggered_by_type: `{signal_counts}`",
        "",
        "## 公司明细",
        "",
        "| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |",
        "|---|---|---|---|---|---:|---|---|---|",
    ]
    for company in payload["companies"]:
        code = company["code"]
        bear_text = "；".join(
            f"{item['dimension']} {item['score']}分:{item['reason']}" for item in company["bear_case"]
        )
        dcf_text = "有效" if code in payload["dcf_results"] else f"跳过:{payload['dcf_skip_reasons'][code]}"
        lines.append(
            "| "
            + " | ".join(
                str(value if value is not None else "")
                for value in (
                    code,
                    company["name"],
                    company["industry"],
                    company["primary_label"],
                    company["diagnostic_label"],
                    company["max_score"],
                    ",".join(company["buy_types"]),
                    dcf_text,
                    bear_text,
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _locked_line(name, version):
    digest = hashlib.sha256(f"{name}=={version}".encode()).hexdigest()
    return f"{name}=={version} --hash=sha256:{digest}\n"


_SAFE_RUN_BAT = (
    b"@echo off\r\n"
    b"setlocal\r\n"
    b'set "VENV_PYTHON=%CD%\\.venv\\Scripts\\python.exe"\r\n'
    b'set "PIP_REQUIRE_VIRTUALENV=true"\r\n'
    b'if not defined PIP_INDEX_URL set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"\r\n'
    b'"%VENV_PYTHON%" -m pip install --require-hashes -r requirements-bootstrap.txt\r\n'
    b'"%VENV_PYTHON%" -m pip install --require-hashes -r requirements-lock.txt\r\n'
    b"exit /b 0\r\n"
)


def _write_minimal_release(
    path,
    *,
    unsafe=False,
    mutate_payload=None,
    rerender_companions=False,
    content_overrides=None,
    omit_files=(),
    extra_files=None,
):
    prefix = "DS_DCF-v11.1.0/"
    runtime_versions = {
        "numpy": "2.4.6",
        "orjson": "3.11.9",
        "pandas": "3.0.3",
        "plotly": "6.9.0",
        "pillow": "12.3.0",
        "requests": "2.34.2",
        "streamlit": "1.59.2",
    }
    files = {
        "LICENSE": _LICENSE_BYTES,
        "README.md": b"release\n",
        "run.bat": _SAFE_RUN_BAT,
        ".streamlit/config.toml": b"[server]\nmaxUploadSize = 1\n",
        "app.py": b"# app\n",
        "config.py": b"# config\n",
        "data/cache.py": b"# cache\n",
        "data/capex_evidence.py": b"# capex evidence\n",
        "data/datacenter.py": b"# datacenter\n",
        "data/financial_indicator_evidence.py": b"# financial indicator evidence\n",
        "data/financial_source_evidence.py": b"# financial source evidence\n",
        "data/financial_balance_sheet_evidence.json": b"{}\n",
        "data/financial_zero_capex_evidence.json": b"{}\n",
        "data/financial_zero_revenue_evidence.json": b"{}\n",
        "data/fetcher.py": b"# fetcher\n",
        "data/growth_evidence.py": b"# Type 3 growth evidence\n",
        "data/industry.py": b"# industry\n",
        "data/industry_f10.json": b"{}\n",
        "data/industry_em_map.json": b"{}\n",
        "data/industry_capco_2025h2.json": b"{}\n",
        "data/industry_exchange_new_listings_2026.json": b"{}\n",
        "data/market_coldness.py": b"# market coldness source\n",
        "data/market_history.py": b"# market history\n",
        "data/quality_history.py": b"# quality history\n",
        "data/research_reports.py": b"# Type 7 research report evidence\n",
        "data/snapshot.py": b"# snapshot\n",
        "data/trading_calendar.py": b"# pinned trading calendar\n",
        "desktop/__init__.py": b"\n",
        "desktop/launcher.py": b"# desktop launcher\n",
        "desktop/installer.py": b"# desktop installer\n",
        "desktop/updater.py": b"# desktop updater\n",
        "desktop/version.py": b"__version__ = '11.1.0'\n",
        "desktop/update_config.json": (
            b'{"manifest_url":"https://github.com/Muguett-DBY/quant-buy-signals/'
            b'releases/download/windows-app/update-manifest.json"}\n'
        ),
        "desktop/version_info.txt": b"# version info\n",
        "desktop/DS_DCF.spec": b"# pyinstaller spec\n",
        "desktop/DS_DCF_Installer.spec": b"# pyinstaller installer spec\n",
        "engine/audit.py": b"# audit\n",
        "engine/buy_screener.py": b"# rules\n",
        "engine/dcf.py": b"# dcf\n",
        "engine/market_coldness.py": b"# market coldness scoring\n",
        "engine/pipeline.py": b"# pipeline\n",
        "engine/quantitative_evidence.py": b"# quantitative evidence\n",
        "engine/quality_equity.py": b"# quality equity\n",
        "engine/risk.py": b"# risk\n",
        "engine/scenarios.py": b"# scenarios\n",
        "engine/valuation_status.py": b"# valuation status\n",
        "ui/buy_types_page.py": b"# buy page\n",
        "ui/leaders_page.py": b"# leaders page\n",
        "tools/__init__.py": b"",
        "tools/build_official_industry_source.py": b"# official industry source generator\n",
        "tools/build_desktop.py": b"# desktop builder\n",
        "tools/china_a_share_trading_calendar.json": b'{"schema_version":1}\n',
        "tools/run_full_audit.py": b"# audit cli\n",
        "tools/sign_desktop_update_manifest.ps1": b"# desktop manifest signer\n",
        "tools/verify_release_zip.py": b"# verifier\n",
        "requirements-bootstrap.txt": _locked_line("pip", "26.1.2").encode(),
        "requirements.txt": b"numpy==2.4.6\norjson==3.11.9\npandas==3.0.3\nplotly==6.9.0\npillow==12.3.0\nrequests==2.34.2\nstreamlit==1.59.2\n",
        "requirements-lock.txt": "".join(
            _locked_line(name, version) for name, version in runtime_versions.items()
        ).encode(),
        "requirements-test.txt": b"pytest==1\n",
        "requirements-dev.txt": b"build==1\n",
        "requirements-dev-lock.txt": (
            "".join(_locked_line(name, version) for name, version in runtime_versions.items())
            + _locked_line("build", "1.5.0")
        ).encode(),
        "pyproject.toml": (
            b"[project]\nname='ds-dcf'\nversion='11.1.0'\nrequires-python='>=3.11,<3.15'\n"
            b"license='LicenseRef-PolyForm-Noncommercial-1.0.0'\nlicense-files=['LICENSE']\n"
            b"dependencies=['numpy==2.4.6','orjson==3.11.9','pandas==3.0.3','plotly==6.9.0','pillow==12.3.0',"
            b"'requests==2.34.2','streamlit==1.59.2']\n"
        ),
    }
    payload = _audit_payload(files)
    files["audit/random100_audit_seed20260715.csv"] = _render_csv(payload)
    files["audit/random100_audit_seed20260715.md"] = _render_markdown(payload)
    payload["companion_artifacts_sha256"] = {
        "csv": hashlib.sha256(files["audit/random100_audit_seed20260715.csv"]).hexdigest(),
        "markdown": hashlib.sha256(files["audit/random100_audit_seed20260715.md"]).hexdigest(),
    }
    if mutate_payload is not None:
        mutate_payload(payload)
    if rerender_companions:
        files["audit/random100_audit_seed20260715.csv"] = _render_csv(payload)
        files["audit/random100_audit_seed20260715.md"] = _render_markdown(payload)
        payload["companion_artifacts_sha256"] = {
            "csv": hashlib.sha256(files["audit/random100_audit_seed20260715.csv"]).hexdigest(),
            "markdown": hashlib.sha256(files["audit/random100_audit_seed20260715.md"]).hexdigest(),
        }
    files["audit/random100_audit_seed20260715.json"] = (json.dumps(payload) + "\n").encode()
    if content_overrides:
        files.update(content_overrides)
    for name in omit_files:
        files.pop(name, None)
    if extra_files:
        files.update(extra_files)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(prefix + name, content)
        if unsafe:
            archive.writestr(prefix + "data/cache/market_snapshot.json.gz", b"secret runtime cache")


_TYPE_PRIORITY = ("type1", "type2", "type5", "type3", "type4", "type6", "type7")
_TYPE_LABELS = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ 高风险早期/困境型",
    "type7": "7️⃣ 优质股权型",
}
_TYPE_WEIGHTS = {
    "type1": {"1a": 0.30, "1b": 0.35, "1c": 0.20, "1d": 0.15},
    "type2": {"2a": 0.25, "2b": 0.30, "2c": 0.25, "2d": 0.20},
    "type3": {"3a": 0.25, "3b": 0.20, "3c": 0.20, "3d": 0.25, "3e": 0.10},
    "type4": {"4a": 0.25, "4b": 0.25, "4c": 0.20, "4d": 0.15, "4e": 0.08, "4f": 0.07},
    "type5": {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10},
    "type6": {"6a": 0.25, "6b": 0.20, "6c": 0.15, "6d": 0.25, "6e": 0.15},
    "type7": {"7a": 1.0 / 3.0, "7b": 1.0 / 3.0, "7c": 1.0 / 3.0},
}


def _refresh_fixture_company_summary(company):
    for type_key in _TYPE_PRIORITY:
        company[f"{type_key}_score"] = company[type_key]["total"]
    triggered_types = [type_key for type_key in _TYPE_PRIORITY if company[type_key]["triggered"]]
    company["buy_types"] = triggered_types
    company["num_types"] = len(triggered_types)
    primary = triggered_types[0] if triggered_types else None
    company["primary_type"] = primary
    company["primary_label"] = _TYPE_LABELS[primary] if primary else "无触发（不买）"
    diagnostic_types = [
        type_key
        for type_key in _TYPE_PRIORITY
        if company[type_key]["status"] not in {"not_applicable", "insufficient_evidence"}
    ]
    top_score = max((company[type_key]["total"] for type_key in diagnostic_types), default=None)
    diagnostic = next(
        (type_key for type_key in diagnostic_types if company[type_key]["total"] == top_score),
        None,
    )
    company["diagnostic_type"] = diagnostic
    company["diagnostic_label"] = _TYPE_LABELS[diagnostic] if diagnostic else "无可完整诊断框架"
    company["max_score"] = top_score
    if diagnostic is None:
        company["bear_case"] = []
        return
    payload = company[diagnostic]
    scores = payload["sub_scores"]
    reasons = payload["reasons"]
    minimum = min(scores.values())
    bear_case = []
    for meta_key in ("_veto", "_condition", "_downgrade"):
        if reasons.get(meta_key):
            bear_case.append({"dimension": meta_key, "score": minimum, "reason": reasons[meta_key]})
    order = {key: index for index, key in enumerate(_TYPE_WEIGHTS[diagnostic])}
    ranked = sorted(
        _TYPE_WEIGHTS[diagnostic],
        key=lambda key: (scores[key], -_TYPE_WEIGHTS[diagnostic][key], order[key]),
    )
    for dimension in ranked:
        if len(bear_case) == 3:
            break
        bear_case.append(
            {
                "dimension": dimension,
                "score": scores[dimension],
                "reason": reasons[dimension],
            }
        )
    company["bear_case"] = bear_case


def _set_all_scoring_statuses(payload, status, score):
    applicable = status != "not_applicable"
    evidence_complete = status != "insufficient_evidence"
    triggered = status == "triggered"
    veto = status in {"vetoed", "blocked"}
    for company in payload["companies"]:
        for type_key in ("type1", "type2", "type3", "type4", "type5", "type6"):
            if type_key == "type1" and (
                company["code"] not in payload["dcf_results"] or status in {"not_applicable", "insufficient_evidence"}
            ):
                continue
            type_payload = company[type_key]
            type_payload["sub_scores"] = {dimension: score for dimension in type_payload["sub_scores"]}
            type_payload["total"] = score
            type_payload["triggered"] = triggered
            type_payload["veto"] = veto
            type_payload["status"] = status
            type_payload["applicable"] = applicable
            type_payload["evidence_complete"] = evidence_complete
            reasons = {dimension: "状态契约夹具" for dimension in type_payload["sub_scores"]}
            if status == "conditional":
                reasons["_condition"] = "状态契约条件"
            if veto:
                reasons["_veto"] = "状态契约否决"
            reasons.update(
                {
                    "_status": status,
                    "_applicable": "yes" if applicable else "no",
                    "_evidence": "complete" if evidence_complete else "incomplete",
                }
            )
            type_payload["reasons"] = reasons
            company[f"{type_key}_score"] = score
        if company["code"] in payload["dcf_results"] and status not in {
            "not_applicable",
            "insufficient_evidence",
        }:
            expected_1a = _fixture_type1_1a_from_valuation(payload["dcf_results"][company["code"]])
            company["type1"]["sub_scores"]["1a"] = expected_1a
            company["type1"]["total"] = round(
                sum(company["type1"]["sub_scores"][key] * weight for key, weight in _TYPE_WEIGHTS["type1"].items()),
                1,
            )
    for company in payload["companies"]:
        if company["industry"] == "SOFTWARE":
            _set_fixture_type7_ledger(
                company,
                valuation_evidence_complete=company["code"] in payload["dcf_results"],
            )
        _refresh_fixture_company_summary(company)


def _install_applicable_type7_ledger(payload):
    company = payload["companies"][0]
    code = company["code"]

    def evidence(key, score=9.0):
        return {
            key: score,
            f"{key}_evidence": {
                "source": "release audit fixture",
                "evidence_id": f"fixture:{code}:{key}",
                "as_of": "2026-07-15",
                "summary": f"{key}={score}",
            },
            f"{key}_evidence_level": "primary",
        }

    metric = {
        "code": code,
        "industry": "SOFTWARE",
        "source_trade_date": "2026-07-15",
        "trend_growth": 0.16,
        "net_profit_history": [100, 120, 145, 175, 210],
        "net_profit_years": [2021, 2022, 2023, 2024, 2025],
        "fcf_history": [90, 108, 130, 158, 190],
        "fcf_years": [2021, 2022, 2023, 2024, 2025],
        "revenue_years": [2021, 2022, 2023, 2024, 2025],
        "equity_history": [100, 120, 144, 172.8, 207.36],
        "equity_years": [2021, 2022, 2023, 2024, 2025],
        "interest_bearing_debt_ratio": 0.05,
        "roic": 0.30,
        "wacc": 0.09,
        "gross_margin": 0.85,
        "net_margin": 0.45,
        "gross_margin_cv": 0.03,
        "share_dilution_1yr": 0.0,
        "profit_volatility": 0.18,
        "growth_consistency": 0.20,
        "total_assets": 300,
        "revenue_latest": 250,
        "net_profit_latest": 210,
        "market_cap": 3_000,
        "capex": 8,
        "rd_intensity": 0.01,
        "type7_research_sources": [
            {
                "security_code": code,
                "company_name": "测试公司",
                "title": f"industry report {index}",
                "publisher": f"publisher {index}",
                "publisher_id": f"publisher-id-{index}",
                "url": f"https://example{index}.test/report",
                "as_of": "2026-07-14",
                "evidence_id": f"report-{code}-{index}",
            }
            for index in range(1, 4)
        ],
    }
    for key in {
        "business_model_score",
        "moat_score",
        "moat_durability_score",
        "runway_score",
        "industry_durability_score",
        "accounting_integrity_score",
        "management_alignment_score",
        "technology_score",
        "catalyst_score",
        "growth_sustainability_score",
        *TYPE7_DIRECT_SCORE_KEYS,
    }:
        metric.update(evidence(key))
    metric.update(evidence("technology_score", score=6.0))
    type1 = company["type1"]
    type1_outcome = (
        type1["triggered"],
        type1["total"],
        type1["sub_scores"],
        type1["reasons"],
    )
    history = {
        "available": True,
        "code": code,
        "as_of": "2026-07-15",
        "model_id": "type7-market-history-v1",
        "shareholder_return": {"available": True, "cagr": 0.18, "total_return": 4.2},
        "valuation_history": {
            "available": True,
            "current_pe_ttm": 20.0,
            "median_pe_ttm": 25.0,
            "current_pb_mrq": 6.0,
            "median_pb_mrq": 7.0,
            "pe_percentile": 0.10,
            "pb_percentile": 0.12,
        },
    }
    outcome, ledger = score_type7_quality_equity(
        metric,
        type1_outcome,
        history,
        valuation_evidence_complete=True,
    )
    triggered, total, sub_scores, reasons = outcome
    assert reasons["_status"] == STATUS_INSUFFICIENT_EVIDENCE
    company["type7_score"] = total
    company["type7"] = {
        "triggered": triggered,
        "total": total,
        "sub_scores": sub_scores,
        "reasons": reasons,
        "veto": bool(reasons.get("_veto")),
        "status": reasons["_status"],
        "applicable": True,
        "evidence_complete": reasons.get("_evidence") == "complete",
        "ledger": ledger,
    }


def test_release_zip_verifier_accepts_a_clean_source_package(tmp_path):
    path = tmp_path / "release.zip"
    _write_minimal_release(path)

    assert _verify(path) == ()


def _forge_market_coldness_na_partition(payload):
    caller = payload["provenance"]["caller_metadata"]
    eligible_codes = caller["validation"]["eligible_codes"]
    artifact = caller["market_coldness_reference_artifact"]
    replay = _replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible_codes,
        as_of_session="2026-07-15",
    )
    retained_code = eligible_codes[0]
    forged_evidence = {retained_code: replay["eligible_evidence"][retained_code]}
    payload["provenance"]["market_coldness_evidence"].update(
        evidence_count=1,
        eligible_evidence_count=1,
        eligible_evidence_coverage=1 / len(eligible_codes),
        evidence_sha256=hashlib.sha256(_canonical_market_coldness_json(forged_evidence)).hexdigest(),
    )
    caller["market_coldness"].update(
        eligible_evidence_count=1,
        eligible_evidence_coverage=1 / len(eligible_codes),
        eligible_applicable_count=1,
        eligible_applicable_evidence_coverage=1.0,
        eligible_not_applicable_count=len(eligible_codes) - 1,
        eligible_not_applicable_codes_by_reason={
            "listed_in_current_year": eligible_codes[1:],
            "listing_history_lt_120_days": [],
        },
    )


def _forge_market_coldness_relative_rank(payload):
    caller = payload["provenance"]["caller_metadata"]
    eligible_codes = caller["validation"]["eligible_codes"]
    replay = _replay_market_coldness_reference_artifact(
        caller["market_coldness_reference_artifact"],
        eligible_codes=eligible_codes,
        as_of_session="2026-07-15",
    )
    forged_evidence = deepcopy(replay["eligible_evidence"])
    forged_evidence[eligible_codes[0]]["components"]["relative"]["turnover_rate_pct"] = 9.0
    payload["provenance"]["market_coldness_evidence"]["evidence_sha256"] = hashlib.sha256(
        _canonical_market_coldness_json(forged_evidence)
    ).hexdigest()


def _move_market_coldness_reference_before_close(payload):
    caller = payload["provenance"]["caller_metadata"]
    artifact = caller["market_coldness_reference_artifact"]
    artifact["retrieved_at"] = "2026-07-15T01:00:00Z"
    caller["market_coldness"].update(
        retrieved_at=artifact["retrieved_at"],
        reference_artifact_sha256=hashlib.sha256(_canonical_market_coldness_json(artifact)).hexdigest(),
    )


def _set_one_eligible_market_coldness_turnover_to_numeric_zero(payload):
    caller = payload["provenance"]["caller_metadata"]
    eligible_codes = caller["validation"]["eligible_codes"]
    artifact = caller["market_coldness_reference_artifact"]
    target = eligible_codes[0]
    row = next(item for item in artifact["records"] if item[0] == target)
    row[4] = 0.0
    replay = _replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible_codes,
        as_of_session="2026-07-15",
    )
    evidence = replay["eligible_evidence"]
    payload["provenance"]["market_coldness_evidence"]["evidence_sha256"] = hashlib.sha256(
        _canonical_market_coldness_json(evidence)
    ).hexdigest()
    caller["market_coldness"].update(
        reference_artifact_sha256=hashlib.sha256(_canonical_market_coldness_json(artifact)).hexdigest(),
        full_listed_evidence_count=len(replay["full_evidence"]),
        eligible_not_applicable_codes_by_reason=replay["eligible_not_applicable_codes_by_reason"],
        eligible_unscored_data_gap_codes_by_reason=replay["eligible_unscored_data_gap_codes_by_reason"],
    )


def test_release_zip_accepts_numeric_zero_turnover_as_replayed_market_coldness_evidence(tmp_path):
    path = tmp_path / "zero-turnover-market-coldness.zip"
    _write_minimal_release(
        path,
        mutate_payload=_set_one_eligible_market_coldness_turnover_to_numeric_zero,
        rerender_companions=True,
    )

    assert _verify(path) == ()


@pytest.mark.parametrize(
    "tamper",
    [
        _forge_market_coldness_na_partition,
        _forge_market_coldness_relative_rank,
        _move_market_coldness_reference_before_close,
    ],
)
def test_release_zip_verifier_replays_raw_market_coldness_instead_of_trusting_coordinated_claims(
    tmp_path,
    tamper,
):
    path = tmp_path / "forged-market-coldness.zip"
    _write_minimal_release(path, mutate_payload=tamper)

    assert any("market-coldness provenance" in error for error in _verify(path))


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    [
        (
            lambda payload: payload["provenance"]["caller_metadata"].update(snapshot_source="cache"),
            "identified, time-consistent market snapshot",
        ),
        (
            lambda payload: payload.update(data_timestamp_utc="2026-07-15T00:00:00+00:00"),
            "identified, time-consistent market snapshot",
        ),
        (
            lambda payload: payload["provenance"].pop("market_coldness_evidence"),
            "market-coldness provenance",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["market_coldness"].update(available=False),
            "market-coldness provenance",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["market_coldness"].update(
                retrieved_at="2026-07-14T08:05:00Z"
            ),
            "market-coldness provenance",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"].update(
                trading_quotes=5096,
                analysis_trading_quotes=5096,
                trading_coverage=0.98,
                analysis_trading_coverage=0.98,
            ),
            "99% same-session trading quote coverage",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"].update(
                eligible_trading_quotes=4116,
                eligible_trading_coverage=0.98,
            ),
            "99% same-session trading quote coverage",
        ),
    ],
)
def test_release_zip_verifier_requires_fresh_quote_and_market_coldness_proof(tmp_path, tamper, expected_error):
    path = tmp_path / "invalid-market-proof.zip"
    _write_minimal_release(path, mutate_payload=tamper)

    assert any(expected_error in error for error in _verify(path))


def test_release_zip_verifier_replays_an_applicable_type7_ledger_and_rejects_nested_tampering(tmp_path):
    clean = tmp_path / "applicable-type7.zip"
    _write_minimal_release(
        clean,
        mutate_payload=_install_applicable_type7_ledger,
        rerender_companions=True,
    )
    assert _verify(clean) == ()

    tampered = tmp_path / "tampered-applicable-type7.zip"

    def install_and_tamper(payload):
        _install_applicable_type7_ledger(payload)
        payload["companies"][0]["type7"]["ledger"]["strict_checks"]["template1"] = 1

    _write_minimal_release(
        tampered,
        mutate_payload=install_and_tamper,
        rerender_companions=True,
    )
    assert any("100 complete company rows" in error for error in _verify(tampered))


def test_release_zip_verifier_rejects_type7_claim_after_validated_dcf_is_removed(tmp_path):
    path = tmp_path / "type7-dcf-removed.zip"

    def remove_bound_dcf(payload):
        company = payload["companies"][0]
        code = company["code"]
        assert code in payload["dcf_results"]
        payload["dcf_results"].pop(code)
        payload["dcf_skip_reasons"][code] = "fixture_removed_dcf"
        payload["dcf_skip_classifications"][code] = {
            "category": "source_missing",
            "reason": "fixture_removed_dcf",
        }
        payload["dcf_valid"] = len(payload["dcf_results"])

    _write_minimal_release(
        path,
        mutate_payload=remove_bound_dcf,
        rerender_companions=True,
    )

    assert any("Type 1 or Type 7 evidence" in error for error in _verify(path))


def test_release_zip_verifier_rejects_nonfinancial_type7_not_applicable_claim(tmp_path):
    path = tmp_path / "nonfinancial-type7-not-applicable.zip"

    def hide_nonfinancial_type7(payload):
        company = payload["companies"][0]
        _set_fixture_type7_ledger(
            company,
            valuation_evidence_complete=False,
            metric={
                "code": company["code"],
                "industry": "BANK",
                "source_trade_date": "2026-07-15",
            },
        )

    _write_minimal_release(
        path,
        mutate_payload=hide_nonfinancial_type7,
        rerender_companions=True,
    )

    assert any("Type 1 or Type 7 evidence" in error for error in _verify(path))


def test_release_zip_verifier_rejects_coordinated_type1_and_type7_dcf_score_forgery(tmp_path):
    path = tmp_path / "type7-type1-coordinated-forgery.zip"

    def change_company_type1(payload):
        company = payload["companies"][0]
        company["type1"]["sub_scores"]["1a"] = 1.0
        company["type1"]["total"] = 0.3
        company["type1_score"] = 0.3
        company["max_score"] = 0.3
        company["bear_case"] = [
            {"dimension": "1b", "score": 0.0, "reason": "审计夹具"},
            {"dimension": "1c", "score": 0.0, "reason": "审计夹具"},
            {"dimension": "1d", "score": 0.0, "reason": "审计夹具"},
        ]
        _set_fixture_type7_ledger(company, valuation_evidence_complete=True)

    _write_minimal_release(
        path,
        mutate_payload=change_company_type1,
        rerender_companions=True,
    )

    assert any("Type 1 or Type 7 evidence" in error for error in _verify(path))


def test_release_zip_verifier_rejects_missing_structured_skip_classifications(tmp_path):
    path = tmp_path / "missing-skip-classifications.zip"

    _write_minimal_release(
        path,
        mutate_payload=lambda payload: payload.pop("dcf_skip_classifications"),
        rerender_companions=True,
    )

    assert any("skip classifications are missing" in error for error in _verify(path))


def test_release_zip_verifier_rejects_financial_pb_type1_position_mismatch(tmp_path):
    path = tmp_path / "financial-pb-type1-mismatch.zip"

    def forge_pb_position(payload):
        code = payload["sample_codes"][1]
        company = next(row for row in payload["companies"] if row["code"] == code)
        assert payload["dcf_results"][code]["_pb_valuation"] is True
        company["type1"]["sub_scores"]["1a"] = 7.5
        company["type1"]["total"] = 2.2
        _refresh_fixture_company_summary(company)

    _write_minimal_release(path, mutate_payload=forge_pb_position, rerender_companions=True)

    assert any("Type 1 or Type 7 evidence" in error for error in _verify(path))


def test_release_zip_verifier_rejects_valid_valuation_hidden_as_type1_not_applicable(tmp_path):
    path = tmp_path / "valid-valuation-hidden-as-na.zip"

    def hide_type1(payload):
        company = payload["companies"][0]
        type1 = company["type1"]
        type1.update(
            triggered=False,
            veto=False,
            status="not_applicable",
            applicable=False,
            evidence_complete=True,
        )
        type1["reasons"].pop("_veto", None)
        type1["reasons"].update(_status="not_applicable", _applicable="no", _evidence="complete")
        _refresh_fixture_company_summary(company)

    _write_minimal_release(path, mutate_payload=hide_type1, rerender_companions=True)

    assert any("Type 1 or Type 7 evidence" in error for error in _verify(path))


def test_release_zip_verifier_rejects_triggered_type1_after_valuation_skip(tmp_path):
    path = tmp_path / "skipped-valuation-triggered-type1.zip"

    def trigger_skipped_type1(payload):
        code = payload["sample_codes"][-2]
        company = next(row for row in payload["companies"] if row["code"] == code)
        type1 = company["type1"]
        type1.update(
            triggered=True,
            total=10.0,
            sub_scores={key: 10.0 for key in type1["sub_scores"]},
            veto=False,
            status="triggered",
            applicable=True,
            evidence_complete=True,
        )
        type1["reasons"] = {
            **{key: "伪造完整证据" for key in type1["sub_scores"]},
            "_status": "triggered",
            "_applicable": "yes",
            "_evidence": "complete",
        }
        _refresh_fixture_company_summary(company)

    _write_minimal_release(path, mutate_payload=trigger_skipped_type1, rerender_companions=True)

    assert any("Type 1 or Type 7 evidence" in error for error in _verify(path))


def test_release_zip_verifier_rejects_previous_type7_ledger_schema(tmp_path):
    path = tmp_path / "type7-schema-v4.zip"

    def downgrade_schema(payload):
        payload["companies"][0]["type7"]["ledger"]["schema_version"] = 4

    _write_minimal_release(path, mutate_payload=downgrade_schema, rerender_companions=True)

    assert any("100 complete company rows" in error for error in _verify(path))


@pytest.mark.parametrize(
    "invalid_config",
    [
        b'{"manifest_url":null}\n',
        b'{"manifest_url":"https://example.test/update.json"}\n',
        b'{"manifest_url":"https://github.com/Muguett-DBY/quant-buy-signals/'
        b'releases/download/windows-app/update-manifest.json","manifest_url":"https://example.test/forged.json"}\n',
        b'{"manifest_url":NaN}\n',
    ],
)
def test_release_zip_verifier_requires_one_strict_official_update_source(tmp_path, invalid_config):
    path = tmp_path / "invalid-update-source.zip"
    _write_minimal_release(
        path,
        content_overrides={"desktop/update_config.json": invalid_config},
    )

    assert any("desktop update configuration" in error for error in _verify(path))


@pytest.mark.parametrize(
    "invalid_audit",
    [
        b'{"seed":20260715,"seed":1}\n',
        b'{"seed":NaN}\n',
    ],
)
def test_release_zip_verifier_rejects_ambiguous_or_nonstandard_audit_json(tmp_path, invalid_audit):
    path = tmp_path / "invalid-audit-json.zip"
    _write_minimal_release(
        path,
        content_overrides={"audit/random100_audit_seed20260715.json": invalid_audit},
    )

    assert any("audit JSON is unreadable" in error for error in _verify(path))


def test_release_zip_verifier_accepts_financial_pb_without_industrial_ttm_fields(tmp_path):
    path = tmp_path / "financial-pb.zip"
    _write_minimal_release(
        path,
        mutate_payload=_convert_first_valuation_to_financial_pb,
        rerender_companions=True,
    )

    assert _verify(path) == ()


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    [
        (
            lambda payload: payload["provenance"]["caller_metadata"].update(snapshot_schema_version=4),
            "snapshot schema version is not 8",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"]["reporting_period_contract"].update(
                period_basis="annual_only"
            ),
            "reporting period contract",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"].pop("strict_ttm_source_coverage"),
            "strict TTM source coverage",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"].pop("analysis_market_codes"),
            "strict TTM source coverage",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"].pop("analysis_ineligible_codes"),
            "strict TTM source coverage",
        ),
        (
            lambda payload: payload["provenance"]["caller_metadata"]["validation"].pop("listing_date_evidence"),
            "listing-date provenance",
        ),
    ],
)
def test_release_zip_verifier_requires_schema8_contract_and_ttm_coverage(tmp_path, tamper, expected_error):
    path = tmp_path / "invalid-schema8-contract.zip"
    _write_minimal_release(path, mutate_payload=tamper)

    assert any(expected_error in error for error in _verify(path))


def test_release_zip_verifier_accepts_schema8_supplemental_coverage_boundaries(tmp_path):
    path = tmp_path / "schema8-supplemental-boundaries.zip"

    def mutate(payload):
        coverage = payload["provenance"]["caller_metadata"]["validation"]["supplemental_field_coverage"]
        coverage.update(GOODWILL=0.0, OBTAIN_SUBSIDIARY_OTHER=1.0)

    _write_minimal_release(path, mutate_payload=mutate)

    assert _verify(path) == ()


@pytest.mark.parametrize(
    "coverage",
    [
        {},
        {"GOODWILL": 0.5},
        {"GOODWILL": 0.5, "OBTAIN_SUBSIDIARY_OTHER": 0.5, "UNDECLARED": 0.5},
        {"GOODWILL": None, "OBTAIN_SUBSIDIARY_OTHER": 0.5},
        {"GOODWILL": True, "OBTAIN_SUBSIDIARY_OTHER": 0.5},
        {"GOODWILL": -0.01, "OBTAIN_SUBSIDIARY_OTHER": 0.5},
        {"GOODWILL": 0.5, "OBTAIN_SUBSIDIARY_OTHER": 1.01},
    ],
)
def test_release_zip_verifier_rejects_invalid_schema8_supplemental_coverage(tmp_path, coverage):
    path = tmp_path / "invalid-schema8-supplemental-coverage.zip"

    def mutate(payload):
        payload["provenance"]["caller_metadata"]["validation"]["supplemental_field_coverage"] = coverage

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("supplemental field coverage" in error for error in _verify(path))


def test_release_zip_verifier_rejects_internally_inconsistent_ttm_coverage(tmp_path):
    path = tmp_path / "invalid-ttm-coverage.zip"

    def tamper(payload):
        coverage = payload["provenance"]["caller_metadata"]["validation"]["strict_ttm_source_coverage"]
        coverage["fcff"]["coverage"] = 0.50

    _write_minimal_release(path, mutate_payload=tamper)

    assert any("strict TTM source coverage" in error for error in _verify(path))


def test_release_zip_verifier_binds_the_coldness_universe_to_the_ttm_population(tmp_path):
    path = tmp_path / "forged-coldness-universe.zip"

    def tamper(payload):
        validation = payload["provenance"]["caller_metadata"]["validation"]
        forged = list(validation["analysis_market_codes"])
        forged[0] = "688999"
        validation["analysis_market_codes"] = sorted(forged)

    _write_minimal_release(path, mutate_payload=tamper)

    assert any("strict TTM source coverage" in error for error in _verify(path))


def test_release_zip_verifier_accepts_canonical_empty_optional_evidence_summaries(tmp_path):
    path = tmp_path / "empty-optional-evidence-summaries.zip"

    def mutate(payload):
        empty = {
            "provided": False,
            "evidence_count": 0,
            "available_count": 0,
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "evidence_sha256": None,
            "as_of_sessions": [],
        }
        payload["provenance"]["type3_growth_evidence"] = dict(empty)
        payload["provenance"]["research_report_evidence"] = dict(empty)

    _write_minimal_release(path, mutate_payload=mutate)

    assert _verify(path) == ()


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("type3_growth_evidence", lambda summary: summary.pop("available_count")),
        ("type3_growth_evidence", lambda summary: summary.update(evidence_count=1)),
        ("type3_growth_evidence", lambda summary: summary.update(eligible_evidence_coverage=0.5)),
        ("research_report_evidence", lambda summary: summary.update(evidence_sha256="not-a-sha256")),
        ("research_report_evidence", lambda summary: summary.update(as_of_sessions=["not-a-date"])),
        ("research_report_evidence", lambda summary: summary.update(provided=False)),
    ],
)
def test_release_zip_verifier_rejects_invalid_optional_evidence_provenance(tmp_path, field, mutation):
    path = tmp_path / f"invalid-{field}.zip"

    def mutate(payload):
        mutation(payload["provenance"][field])

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("provenance summary" in error for error in _verify(path))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda result: result["ttm_fcff_evidence"].update(value=result["ttm_fcff_evidence"]["value"] + 1),
        lambda result: result["ttm_revenue_evidence"]["components"]["current_interim"].update(
            revenue=result["ttm_revenue_evidence"]["components"]["current_interim"]["revenue"] + 10_000
        ),
        lambda result: result.update(fcf_normalisation_basis="latest_persistent_decline"),
        lambda result: result.update(
            base_fcf_adjustments=[
                {
                    "kind": "fcf_margin_ceiling",
                    "before": result["base_fcf"],
                    "limit": result["base_fcf"] * 2,
                    "after": result["base_fcf"],
                }
            ]
        ),
        lambda result: result["dcf_points"]["neutral"].update(lower=result["dcf_points"]["neutral"]["lower"] + 1),
        lambda result: result["params"]["optimistic"].update(forecast_years=10),
    ],
)
def test_release_zip_verifier_replays_strict_ttm_lineage_and_five_year_endpoints(tmp_path, tamper):
    path = tmp_path / "forged-strict-ttm-valuation.zip"

    def mutate(payload):
        result = next(iter(payload["dcf_results"].values()))
        tamper(result)

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("complete valuation result or structured skip" in error for error in _verify(path))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda result: result["params"]["neutral"].update(pb_upper=result["params"]["neutral"]["pb_upper"] + 0.1),
        lambda result: result["dcf_points"]["optimistic"].update(upper=result["dcf_points"]["optimistic"]["upper"] + 1),
    ],
)
def test_release_zip_verifier_independently_replays_financial_pb_endpoints(tmp_path, tamper):
    path = tmp_path / "forged-financial-pb.zip"

    def mutate(payload):
        _convert_first_valuation_to_financial_pb(payload)
        result = next(iter(payload["dcf_results"].values()))
        tamper(result)

    _write_minimal_release(path, mutate_payload=mutate, rerender_companions=True)

    assert any("complete valuation result or structured skip" in error for error in _verify(path))


def test_checked_in_desktop_launcher_preserves_secure_domestic_install_contract():
    launcher = (Path(__file__).resolve().parents[1] / "run.bat").read_bytes()
    without_crlf = launcher.replace(b"\r\n", b"")

    assert b"\n" not in without_crlf
    assert b"\r" not in without_crlf
    assert _desktop_launcher_errors(launcher) == ()


@pytest.mark.parametrize(
    ("launcher", "expected_error"),
    [
        (
            _SAFE_RUN_BAT.replace(b"if not defined PIP_INDEX_URL ", b""),
            "overridable Tsinghua HTTPS PIP_INDEX_URL",
        ),
        (
            _SAFE_RUN_BAT.replace(b" --require-hashes", b"", 1),
            "hash-lock requirements-bootstrap.txt",
        ),
        (
            _SAFE_RUN_BAT.replace(
                b" -r requirements-lock.txt",
                b" --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements-lock.txt",
            ),
            "trusted-host",
        ),
        (
            _SAFE_RUN_BAT.replace(
                b" -r requirements-lock.txt",
                b" --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-lock.txt",
            ),
            "honoring PIP_INDEX_URL",
        ),
        (
            _SAFE_RUN_BAT.replace(b'"%VENV_PYTHON%" -m pip install', b"python -m pip install", 1),
            "inside .venv",
        ),
    ],
)
def test_release_zip_verifier_rejects_weakened_desktop_installers(tmp_path, launcher, expected_error):
    path = tmp_path / "unsafe-installer.zip"
    _write_minimal_release(path, content_overrides={"run.bat": launcher})

    assert any(expected_error in error for error in _verify(path))


def test_audit_and_release_rule_hash_contracts_are_identical_and_required():
    root = Path(__file__).resolve().parents[1]
    audit_rule_files = {path.relative_to(root).as_posix() for path in _AUDIT_RULE_FILES}

    assert audit_rule_files == _RELEASE_RULE_FILES == _EXPECTED_RULE_FILES
    assert _EXPECTED_RULE_FILES <= _REQUIRED_FILES


@pytest.mark.parametrize(
    "module",
    (
        "data/growth_evidence.py",
        "data/research_reports.py",
        "engine/market_coldness.py",
    ),
)
def test_release_zip_verifier_rejects_tampered_new_rule_module(tmp_path, module):
    path = tmp_path / f"tampered-{Path(module).stem}-rule.zip"
    _write_minimal_release(
        path,
        content_overrides={module: b"# tampered after audit generation\n"},
    )

    assert any("rules_sha256" in error for error in _verify(path))


def test_release_zip_verifier_requires_all_new_quantitative_rule_modules(tmp_path):
    path = tmp_path / "missing-new-rule-modules.zip"
    omitted = (
        "data/capex_evidence.py",
        "data/financial_indicator_evidence.py",
        "data/growth_evidence.py",
        "data/market_coldness.py",
        "data/market_history.py",
        "data/research_reports.py",
        "engine/market_coldness.py",
        "engine/quantitative_evidence.py",
        "engine/risk.py",
        "engine/valuation_status.py",
    )
    _write_minimal_release(path, omit_files=omitted)

    errors = _verify(path)
    assert any("required release files are missing" in error for error in errors)


@pytest.mark.parametrize(
    ("status", "score"),
    [
        ("triggered", 7.0),
        ("conditional", 7.0),
        ("vetoed", 7.0),
        ("blocked", 7.0),
        ("observe", 5.0),
        ("not_triggered", 0.0),
        ("not_applicable", 0.0),
        ("insufficient_evidence", 0.0),
    ],
)
def test_release_zip_verifier_accepts_each_explicit_scoring_status(tmp_path, status, score):
    path = tmp_path / f"status-{status}.zip"

    def mutate(payload):
        _set_all_scoring_statuses(payload, status, score)

    _write_minimal_release(path, mutate_payload=mutate, rerender_companions=True)

    assert _verify(path) == ()


def test_release_zip_verifier_accepts_confirmed_veto_with_other_evidence_incomplete(tmp_path):
    path = tmp_path / "partial-evidence-confirmed-veto.zip"

    def mutate(payload):
        company = payload["companies"][0]
        type_payload = company["type1"]
        type_payload["status"] = "vetoed"
        type_payload["veto"] = True
        type_payload["evidence_complete"] = False
        type_payload["reasons"].update(
            {
                "_veto": "独立硬否决已确认",
                "_status": "vetoed",
                "_evidence": "incomplete",
            }
        )
        company["bear_case"] = [
            {"dimension": "_veto", "score": 0.0, "reason": "独立硬否决已确认"},
            {"dimension": "1b", "score": 0.0, "reason": "审计夹具"},
            {"dimension": "1c", "score": 0.0, "reason": "审计夹具"},
        ]

    _write_minimal_release(path, mutate_payload=mutate, rerender_companions=True)

    assert _verify(path) == ()


def test_release_zip_verifier_accepts_20_character_evidence_and_rejects_21(tmp_path):
    accepted = tmp_path / "evidence-20.zip"

    def set_20(payload):
        evidence = "证" * 20
        payload["companies"][0]["type1"]["reasons"]["1c"] = evidence
        payload["companies"][0]["bear_case"][1]["reason"] = evidence

    _write_minimal_release(accepted, mutate_payload=set_20, rerender_companions=True)
    assert _verify(accepted) == ()

    rejected = tmp_path / "evidence-21.zip"

    def set_21(payload):
        evidence = "证" * 21
        payload["companies"][0]["type1"]["reasons"]["1c"] = evidence
        payload["companies"][0]["bear_case"][1]["reason"] = evidence

    _write_minimal_release(rejected, mutate_payload=set_21, rerender_companions=True)
    assert any("100 complete company rows" in error for error in _verify(rejected))


@pytest.mark.parametrize("field", ["status", "applicable", "evidence_complete"])
def test_release_zip_verifier_requires_explicit_scoring_state_fields(tmp_path, field):
    path = tmp_path / f"missing-{field}.zip"

    def mutate(payload):
        payload["companies"][0]["type1"].pop(field)

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("100 complete company rows" in error for error in _verify(path))


@pytest.mark.parametrize(
    ("metadata_key", "invalid_value"),
    [("_status", "observe"), ("_applicable", "no"), ("_evidence", "incomplete")],
)
def test_release_zip_verifier_rejects_scoring_state_metadata_mismatch(tmp_path, metadata_key, invalid_value):
    path = tmp_path / f"metadata-{metadata_key}.zip"

    def mutate(payload):
        payload["companies"][0]["type1"]["reasons"][metadata_key] = invalid_value

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("100 complete company rows" in error for error in _verify(path))


@pytest.mark.parametrize(
    ("status", "leak"),
    [("not_applicable", "veto"), ("insufficient_evidence", "triggered")],
)
def test_release_zip_verifier_rejects_non_diagnostic_veto_or_trigger_leaks(tmp_path, status, leak):
    path = tmp_path / f"leak-{status}-{leak}.zip"

    def mutate(payload):
        type_payload = payload["companies"][0]["type1"]
        type_payload["status"] = status
        type_payload["applicable"] = status != "not_applicable"
        type_payload["evidence_complete"] = status != "insufficient_evidence"
        type_payload["reasons"]["_status"] = status
        type_payload["reasons"]["_applicable"] = "yes" if type_payload["applicable"] else "no"
        type_payload["reasons"]["_evidence"] = "complete" if type_payload["evidence_complete"] else "incomplete"
        if leak == "veto":
            type_payload["veto"] = True
            type_payload["reasons"]["_veto"] = "不得泄漏的否决"
        else:
            type_payload["triggered"] = True

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("100 complete company rows" in error for error in _verify(path))


@pytest.mark.parametrize("status", ["not_applicable", "insufficient_evidence"])
def test_release_zip_verifier_excludes_non_diagnostic_scores_from_maximum(tmp_path, status):
    path = tmp_path / f"excluded-maximum-{status}.zip"

    def mutate(payload):
        company = payload["companies"][0]
        type_payload = company["type2"]
        type_payload["sub_scores"] = {dimension: 10.0 for dimension in type_payload["sub_scores"]}
        type_payload["total"] = 10.0
        type_payload["status"] = status
        type_payload["applicable"] = status != "not_applicable"
        type_payload["evidence_complete"] = status != "insufficient_evidence"
        type_payload["reasons"]["_status"] = status
        type_payload["reasons"]["_applicable"] = "yes" if type_payload["applicable"] else "no"
        type_payload["reasons"]["_evidence"] = "complete" if type_payload["evidence_complete"] else "incomplete"
        company["type2_score"] = 10.0
        _refresh_fixture_company_summary(company)

    _write_minimal_release(path, mutate_payload=mutate, rerender_companions=True)

    assert _verify(path) == ()


@pytest.mark.parametrize(
    "case",
    [
        "triggered_below_threshold",
        "conditional_without_condition",
        "vetoed_without_veto",
        "blocked_without_veto",
        "observe_below_band",
        "not_triggered_inside_observe_band",
    ],
)
def test_release_zip_verifier_rejects_incoherent_scoring_status_semantics(tmp_path, case):
    path = tmp_path / f"incoherent-{case}.zip"

    def mutate(payload):
        company = payload["companies"][0]
        type_payload = company["type1"]
        status = {
            "triggered_below_threshold": "triggered",
            "conditional_without_condition": "conditional",
            "vetoed_without_veto": "vetoed",
            "blocked_without_veto": "blocked",
            "observe_below_band": "observe",
            "not_triggered_inside_observe_band": "not_triggered",
        }[case]
        type_payload["status"] = status
        type_payload["reasons"]["_status"] = status
        type_payload["triggered"] = status == "triggered"
        if case == "not_triggered_inside_observe_band":
            type_payload["sub_scores"] = {dimension: 5.0 for dimension in type_payload["sub_scores"]}
            type_payload["total"] = 5.0
            company["type1_score"] = 5.0

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("100 complete company rows" in error for error in _verify(path))


def test_release_checkout_line_endings_are_explicit_for_extensionless_placeholders():
    attributes = (Path(__file__).resolve().parents[1] / ".gitattributes").read_text(encoding="utf-8")

    assert ".gitkeep text eol=lf" in attributes.splitlines()


@pytest.mark.parametrize("unsafe_path", [b"unsafe\\path.py", b"unsafe?.py"])
def test_release_git_tree_parser_rejects_nonportable_paths(unsafe_path):
    raw_tree = b"100644 blob " + (b"0" * 40) + b"\t" + unsafe_path + b"\0"

    with pytest.raises(ValueError, match="unsafe path"):
        _git_tree_entries(raw_tree)


def test_release_zip_path_parser_rejects_windows_forbidden_characters():
    with pytest.raises(ValueError, match="unsafe archive path"):
        _normalised_file_names(["release/unsafe?.py"])


def test_release_zip_verifier_rejects_runtime_cache(tmp_path):
    path = tmp_path / "unsafe.zip"
    _write_minimal_release(path, unsafe=True)

    assert any("runtime cache" in error for error in _verify(path))


def test_release_zip_verifier_rejects_parent_traversal(tmp_path):
    path = tmp_path / "unsafe-path.zip"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("../outside.txt", "unsafe")

    assert any("unsafe archive path" in error for error in _verify(path))


def test_release_zip_verifier_rejects_company_rows_that_differ_from_sample_or_use_bj_codes(tmp_path):
    path = tmp_path / "bj-company.zip"

    def mutate(payload):
        payload["companies"][0]["code"] = "920001"

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("company identities" in error or "complete company" in error for error in _verify(path))


def test_release_zip_verifier_requires_complete_scoring_and_valuation_rows(tmp_path):
    incomplete_company = tmp_path / "incomplete-company.zip"

    def remove_score_payload(payload):
        payload["companies"][0].pop("type6")

    _write_minimal_release(incomplete_company, mutate_payload=remove_score_payload)
    assert any("100 complete company rows" in error for error in _verify(incomplete_company))

    incomplete_valuation = tmp_path / "incomplete-valuation.zip"

    def remove_valuation_status(payload):
        payload["dcf_skip_reasons"].pop(payload["sample_codes"][-1])

    _write_minimal_release(incomplete_valuation, mutate_payload=remove_valuation_status)
    assert any("complete valuation result or structured skip" in error for error in _verify(incomplete_valuation))


def test_release_zip_verifier_binds_csv_and_markdown_to_json_manifest(tmp_path):
    path = tmp_path / "forged-companions.zip"
    _write_minimal_release(
        path,
        content_overrides={
            "audit/random100_audit_seed20260715.csv": b"forged\n",
            "audit/random100_audit_seed20260715.md": b"# forged\n",
        },
    )

    errors = _verify(path)
    assert any("csv companion" in error for error in errors)
    assert any("markdown companion" in error for error in errors)


def test_release_zip_verifier_rejects_nonzero_full_market_pipeline_issues(tmp_path):
    path = tmp_path / "pipeline-issues.zip"

    def mutate(payload):
        quality = dict(payload["analysis_quality"])
        quality["pipeline_issues"] = 1
        quality["pipeline_issue_rate"] = 0.01
        payload["analysis_quality"] = quality
        payload["provenance"]["caller_metadata"]["full_market_quality"] = dict(quality)

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("zero-issue complete full-market" in error for error in _verify(path))


def test_release_zip_verifier_rejects_invalid_git_seed_and_runtime_provenance(tmp_path):
    path = tmp_path / "invalid-provenance.zip"

    def mutate(payload):
        payload["seed"] = 1
        payload["provenance"]["git"]["commit"] = "not-a-real-commit"
        payload["provenance"]["runtime"]["python"] = "3.15.0"
        payload["provenance"]["runtime"]["packages"]["pandas"] = "0.0.0"

    _write_minimal_release(path, mutate_payload=mutate)

    errors = _verify(path)
    assert any("clean identified Git commit" in error for error in errors)
    assert any("audit seed" in error for error in errors)
    assert any("Python runtime" in error for error in errors)
    assert any("direct dependency versions" in error for error in errors)


def test_release_zip_verifier_rejects_missing_core_runtime_files(tmp_path):
    path = tmp_path / "missing-core.zip"
    _write_minimal_release(path, omit_files=("app.py", "engine/dcf.py", "data/industry_f10.json"))

    assert any("required release files are missing" in error for error in _verify(path))


def test_release_zip_verifier_rejects_internal_archives_build_residue_and_private_keys(tmp_path):
    forbidden = {
        ".playwright-cli/page.yml": b"internal\n",
        "ds_dcf.egg-info/PKG-INFO": b"build\n",
        ".coverage.worker": b"coverage\n",
        "nested/release.zip": b"zip\n",
        "secrets/id_rsa": b"private\n",
        "secrets/client.p12": b"private\n",
    }
    for index, (name, content) in enumerate(forbidden.items()):
        path = tmp_path / f"forbidden-{index}.zip"
        _write_minimal_release(path, extra_files={name: content})

        assert _verify(path), name


@pytest.mark.parametrize(
    ("name", "expected_error"),
    [
        ("secrets/client.der", "forbidden release artifact"),
        ("secrets/client.pk8", "forbidden release artifact"),
        ("secrets/client.pkcs8", "forbidden release artifact"),
        ("secrets/client.p8", "forbidden release artifact"),
        ("secrets/client.ppk", "forbidden release artifact"),
        ("secrets/android.jks", "forbidden release artifact"),
        ("secrets/android.keystore", "forbidden release artifact"),
        ("secrets/desktop-signing-private-key.properties", "runtime or secret file"),
        ("secrets/android-release-credentials.properties", "runtime or secret file"),
        ("android/release.properties", "runtime or secret file"),
    ],
)
def test_release_zip_verifier_independently_rejects_signing_material_formats(tmp_path, name, expected_error):
    path = tmp_path / (name.replace("/", "-").replace(".", "-") + ".zip")
    _write_minimal_release(path, extra_files={name: b"private signing material\n"})

    assert any(expected_error in error for error in _verify(path))


def test_release_zip_verifier_requires_full_sh_sz_universe_and_snapshot_identity(tmp_path):
    path = tmp_path / "invalid-universe.zip"

    def mutate(payload):
        caller = payload["provenance"]["caller_metadata"]
        caller["validation"]["analysis_markets"] = ["BJ", "SH", "SZ"]
        caller["validation"]["eligible_codes"][0] = "920001"
        payload["provenance"].pop("snapshot_content_sha256")
        caller.pop("snapshot_payload_sha256")
        payload["data_timestamp_utc"] = "not-a-time"

    _write_minimal_release(path, mutate_payload=mutate)

    errors = _verify(path)
    assert any("complete analysis universe" in error for error in errors)
    assert any("identified, time-consistent market snapshot" in error for error in errors)


def test_release_zip_verifier_recomputes_fixed_seed_sample(tmp_path):
    path = tmp_path / "forged-sample.zip"

    def mutate(payload):
        payload["sample_codes"] = list(reversed(payload["sample_codes"]))

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("fixed-seed draw" in error for error in _verify(path))


def test_release_zip_verifier_recomputes_weighted_scores_and_valuation_structure(tmp_path):
    forged_score = tmp_path / "forged-score.zip"

    def mutate_score(payload):
        payload["companies"][0]["type1"]["sub_scores"]["1a"] = 10.0

    _write_minimal_release(forged_score, mutate_payload=mutate_score)
    assert any("100 complete company rows" in error for error in _verify(forged_score))

    forged_valuation = tmp_path / "forged-valuation.zip"

    def mutate_valuation(payload):
        first_code = next(iter(payload["dcf_results"]))
        payload["dcf_results"][first_code] = {"zone": "观察区"}

    _write_minimal_release(forged_valuation, mutate_payload=mutate_valuation)
    assert any("complete valuation result or structured skip" in error for error in _verify(forged_valuation))


def test_release_zip_verifier_rejects_superficial_snapshot_hashes(tmp_path):
    path = tmp_path / "superficial-hashes.zip"

    def mutate(payload):
        provenance = payload["provenance"]
        provenance["snapshot_content_sha256"] = "a" * 64
        provenance["snapshot_artifact_sha256"] = "b" * 64
        provenance["caller_metadata"]["snapshot_payload_sha256"] = "c" * 64

    _write_minimal_release(path, mutate_payload=mutate)

    assert any("identified, time-consistent market snapshot" in error for error in _verify(path))


def test_release_zip_verifier_rejects_case_collisions_symlinks_and_embedded_credentials(tmp_path):
    collision = tmp_path / "case-collision.zip"
    _write_minimal_release(collision, extra_files={"APP.PY": b"# collision\n"})
    assert any("case-insensitive archive path collision" in error for error in _verify(collision))

    clean = tmp_path / "clean.zip"
    symlink = tmp_path / "symlink.zip"
    _write_minimal_release(clean)
    with ZipFile(clean) as source, ZipFile(symlink, "w", compression=ZIP_DEFLATED) as target:
        for entry in source.infolist():
            target.writestr(entry, source.read(entry))
        link = ZipInfo("DS_DCF-v11.1.0/link-to-outside")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(link, "../../outside")
    assert any("unsafe archive path" in error for error in _verify(symlink))

    credential = tmp_path / "credential.zip"
    secret = ("release-" + "token-" + "x" * 24).encode()
    _write_minimal_release(credential, extra_files={"credentials.json": b'{"token":"' + secret + b'"}\n'})
    assert any("embedded credential" in error for error in _verify(credential))


def test_release_zip_verifier_binds_approved_license_text(tmp_path):
    path = tmp_path / "tampered-license.zip"
    _write_minimal_release(path, content_overrides={"LICENSE": b"not the license\n"})

    assert any("approved PolyForm" in error for error in _verify(path))


def test_release_zip_verifier_parses_lock_semantics_and_companion_semantics(tmp_path):
    invalid_lock = tmp_path / "invalid-lock.zip"
    _write_minimal_release(
        invalid_lock,
        content_overrides={"requirements-lock.txt": b"numpy==2.4.6 --hash=sha256:00\n"},
    )
    assert any("dependency locks" in error for error in _verify(invalid_lock))

    forged_csv = ("代码\n" + "\n".join(sorted(random.Random(20260715).sample(_eligible_codes(), 100))) + "\n").encode()
    forged_markdown = (
        "# 固定随机 100 家公司审计\n\n- seed: `20260715`\n- sample_size: `100`\n\n## 公司明细\n"
    ).encode()
    companions = tmp_path / "semantic-companions.zip"

    def bind_forged_companions(payload):
        payload["companion_artifacts_sha256"] = {
            "csv": hashlib.sha256(forged_csv).hexdigest(),
            "markdown": hashlib.sha256(forged_markdown).hexdigest(),
        }

    _write_minimal_release(
        companions,
        mutate_payload=bind_forged_companions,
        content_overrides={
            "audit/random100_audit_seed20260715.csv": forged_csv,
            "audit/random100_audit_seed20260715.md": forged_markdown,
        },
    )
    errors = _verify(companions)
    assert any("CSV does not semantically match" in error for error in errors)
    assert any("Markdown does not semantically match" in error for error in errors)


def test_release_zip_verifier_proves_real_audit_commit_and_audit_only_descendant(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args):
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release-test@example.invalid")
    filtered_checkout_files = {
        ".gitattributes": (
            b"* text=auto\n"
            b"*.py text eol=lf\n"
            b"*.md text eol=lf\n"
            b"*.json text eol=lf\n"
            b"*.csv text eol=lf\n"
            b"*.txt text eol=lf\n"
            b"*.toml text eol=lf\n"
            b"*.spec text eol=lf\n"
            b"*.ps1 text eol=lf\n"
            b".gitattributes text eol=lf\n"
            b"LICENSE text eol=lf\n"
            b"*.bat text eol=crlf\n"
            b"*.filtertest text eol=crlf\n"
            b"*.bin -text\n"
        ),
        "checkout.filtertest": b"release bytes\r\n",
        "payload.bin": b"\x00\n",
    }
    baseline_release = tmp_path / "baseline-release.zip"
    _write_minimal_release(baseline_release, extra_files=filtered_checkout_files)
    with ZipFile(baseline_release) as archive:
        for entry in archive.infolist():
            relative = "/".join(entry.filename.replace("\\", "/").split("/")[1:])
            if not relative or (relative.startswith("audit/") and not relative.endswith(".csv")):
                continue
            destination = repository.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(entry))
    git("add", ".")
    git("commit", "-m", "code baseline")
    audit_commit = git("rev-parse", "HEAD")

    release = tmp_path / "git-provenance.zip"

    def bind_commit(payload):
        payload["provenance"]["git"]["commit"] = audit_commit

    _write_minimal_release(
        release,
        mutate_payload=bind_commit,
        extra_files=filtered_checkout_files,
    )
    with ZipFile(release) as archive:
        for entry in archive.infolist():
            relative = "/".join(entry.filename.replace("\\", "/").split("/")[1:])
            if not relative.startswith("audit/"):
                continue
            destination = repository.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(entry))
    git("add", "audit")
    git("commit", "-m", "release audit")

    assert verify_release_zip(str(release), repository=repository) == ()

    changed_source = tmp_path / "changed-source.zip"
    _write_minimal_release(
        changed_source,
        mutate_payload=bind_commit,
        content_overrides={"app.py": b"# tampered app\n"},
        extra_files=filtered_checkout_files,
    )
    assert any(
        "differs from clean Git HEAD" in error
        for error in verify_release_zip(str(changed_source), repository=repository)
    )

    forged = tmp_path / "forged-git-provenance.zip"
    _write_minimal_release(forged)
    assert any("cannot be verified" in error for error in verify_release_zip(str(forged), repository=repository))

    repository.joinpath(".gitattributes").write_text(
        "*.bat text eol=crlf\n*.filtertest text=auto\n*.bin -text eol=crlf\n",
        encoding="utf-8",
    )
    git("add", ".gitattributes")
    git("update-index", "--chmod=+x", "app.py")
    git("commit", "-m", "introduce nonportable release entries")
    portability_errors = verify_release_zip(str(release), repository=repository)
    assert any(
        "release text file lacks a deterministic eol policy: checkout.filtertest" in error
        for error in portability_errors
    )
    assert any("release binary file has a conflicting eol policy: payload.bin" in error for error in portability_errors)
    assert any("release Git tree contains an unsupported entry: app.py" in error for error in portability_errors)
