"""Deterministic company-sample audits for the complete valuation/scoring chain."""

from __future__ import annotations

import json
import hashlib
import importlib.metadata
import math
import os
import platform
import random
import re
import shutil
import statistics

# Fixed-argv local Git provenance checks only; no shell is involved.
import subprocess  # nosec B404
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

import config as _production_config
from data.financial_source_evidence import FinancialSourceEvidenceError, zero_capex_evidence
from data.patch4_evidence import (
    MODEL_ID as PATCH4_PUBLIC_EVIDENCE_MODEL_ID,
    Patch4EvidenceError,
    validate_patch4_evidence_record,
)
from engine import buy_screener as _production_screener
from engine.buy_screener import validate_screening_result
from engine.dcf import ReportingPeriodContract
from engine.pipeline import (
    AnalysisQualityError,
    MarketAnalysisOutcome,
    PipelineIssue,
    compute_dcf_batch,
    run_market_analysis,
    validate_market_analysis_quality,
)
from engine.valuation_status import (
    DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
    DCF_SKIP_INCONSISTENT_SOURCE,
    DCF_SKIP_INTERNAL_ERROR,
    DCF_SKIP_MODEL_UNSUPPORTED,
    DCF_SKIP_SOURCE_MISSING,
    normalize_dcf_skip_classification,
)
from engine.type7_patch6 import MODEL_ID as PATCH6_TYPE7_MODEL_ID, validate_patch6_type7_ledger


def _normalise_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


@dataclass(frozen=True)
class RandomSampleAudit:
    seed: int
    sample_size: int
    sample_codes: tuple[str, ...]
    scores: pd.DataFrame
    dcf_results: Mapping[str, Mapping[str, Any]]
    dcf_skip_reasons: Mapping[str, str]
    dcf_skip_classifications: Mapping[str, Mapping[str, str]]
    pipeline_issues: tuple[PipelineIssue, ...]
    engine_invariant_errors: tuple[str, ...]
    scoring_replay_errors: tuple[str, ...]
    valuation_replay_errors: tuple[str, ...]
    independent_errors: tuple[str, ...]
    analysis_quality: Mapping[str, Any]
    provenance: Mapping[str, Any]
    eligible_universe_size: int

    @property
    def invariant_errors(self) -> tuple[str, ...]:
        """Backward-compatible aggregate; sources remain separately labelled."""
        return (
            self.engine_invariant_errors
            + self.scoring_replay_errors
            + self.valuation_replay_errors
            + self.independent_errors
        )


_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_CODE_DIRS = (_ROOT / "data", _ROOT / "desktop", _ROOT / "engine", _ROOT / "ui", _ROOT / "tools")
_AUDIT_WEIGHTS: dict[str, dict[str, float]] = {
    "type1": {"1a": 0.30, "1b": 0.35, "1c": 0.20, "1d": 0.15},
    "type2": {"2a": 0.25, "2b": 0.30, "2c": 0.25, "2d": 0.20},
    "type3": {"3a": 0.25, "3b": 0.20, "3c": 0.20, "3d": 0.25, "3e": 0.10},
    "type4": {"4a": 0.25, "4b": 0.25, "4c": 0.20, "4d": 0.15, "4e": 0.08, "4f": 0.07},
    "type5": {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10},
    "type6": {"6a": 0.25, "6b": 0.20, "6c": 0.15, "6d": 0.25, "6e": 0.15},
    "type7": {"7a": 1.0 / 3.0, "7b": 1.0 / 3.0, "7c": 1.0 / 3.0},
}
_AUDIT_PRIORITY = ("type1", "type2", "type5", "type3", "type4", "type6", "type7")
_AUDIT_NAMES = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ 高风险早期/困境型",
    "type7": "7️⃣ 优质股权型",
}
_AUDIT_QUALIFY_THRESHOLD = 7.0
_AUDIT_REASON_MAX_LENGTH = 48
_AUDIT_TYPE_STATUSES = {
    "triggered",
    "observe",
    "not_triggered",
    "vetoed",
    "conditional",
    "not_applicable",
    "insufficient_evidence",
    "blocked",
}
_AUDIT_NON_DIAGNOSTIC_STATUSES = {"not_applicable", "insufficient_evidence"}
_AUDIT_TYPE7_SCHEMA_VERSION = 7
_AUDIT_TYPE7_MODEL_ID = "patch6-type7-quality-equity-v7"
_AUDIT_TYPE7_SHAREHOLDER_RETURN_FORMULA = "total=end_hfq/start_hfq-1;cagr=(end_hfq/start_hfq)^(365.2425/days)-1"
_AUDIT_TYPE7_VALUATION_PERCENTILE_FORMULA = "percentile=(count(x<current)+0.5*count(x=current))/historical_count"
_AUDIT_TYPE7_TEN_YEAR_TARGET_DAYS = 3_652
_AUDIT_TYPE7_TEN_YEAR_START_TOLERANCE_DAYS = 62
_AUDIT_TYPE7_FIVE_YEAR_TARGET_DAYS = 1_826
_AUDIT_TYPE7_FIVE_YEAR_START_TOLERANCE_DAYS = 62
_AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS = 21
_AUDIT_TYPE7_VALUATION_MIN_OBSERVATIONS = 500
_AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS = 2_000
_AUDIT_TYPE5_BOTTOM_EVIDENCE_SCHEMA_VERSION = 1
_AUDIT_TYPE5_BOTTOM_EVIDENCE_MODEL_ID = "type5-bottom-observables-v1"
_AUDIT_TYPE5_HISTORY_MIN_SPAN_DAYS = 1_743
_AUDIT_TYPE5_HISTORY_MAX_START_DELAY_DAYS = 62
_AUDIT_TYPE5_HISTORY_MAX_LATEST_AGE_DAYS = 21
_AUDIT_TYPE5_MAX_COLDNESS_SCORE = 8.0
_AUDIT_TYPE5_MAX_COLDNESS_SCORE_WITHOUT_VOLUME = 7.5
_AUDIT_TYPE5_COLDNESS_EVIDENCE_SCHEMA_VERSION = 1
_AUDIT_TYPE5_COLDNESS_MODEL_ID = "patch6-type2c-quantity-price-v1"
_AUDIT_TYPE5_COLDNESS_SOURCE = "Eastmoney push2 clist"
_AUDIT_TYPE5_COLDNESS_SOURCE_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_AUDIT_TYPE5_COLDNESS_MIN_SOURCE_COVERAGE = 0.90
_AUDIT_TYPE5_COLDNESS_WEIGHTS = {
    "change_60d_pct": 0.45,
    "change_ytd_pct": 0.25,
    "turnover_rate_pct": 0.20,
    "volume_ratio": 0.10,
}
_AUDIT_TYPE5_COLDNESS_BANDS = {
    "change_60d_pct": (
        (-35.0, 9.5),
        (-25.0, 9.0),
        (-15.0, 8.0),
        (-8.0, 7.0),
        (0.0, 5.5),
        (10.0, 4.0),
        (20.0, 3.0),
        (35.0, 2.0),
        (60.0, 1.0),
    ),
    "change_ytd_pct": (
        (-45.0, 9.5),
        (-30.0, 9.0),
        (-20.0, 8.0),
        (-10.0, 7.0),
        (0.0, 5.5),
        (15.0, 4.5),
        (30.0, 3.0),
        (50.0, 2.0),
        (80.0, 1.0),
    ),
    "turnover_rate_pct": (
        (0.30, 9.0),
        (0.70, 8.0),
        (1.50, 7.0),
        (3.00, 5.5),
        (5.00, 4.5),
        (8.00, 3.5),
        (15.0, 2.0),
        (30.0, 1.0),
    ),
    "volume_ratio": (
        (0.40, 9.0),
        (0.70, 8.0),
        (0.90, 6.5),
        (1.10, 5.5),
        (1.50, 4.0),
        (2.50, 2.5),
        (5.00, 1.0),
    ),
}
_AUDIT_PATCH4_SCHEMA_VERSION = 1
_AUDIT_PATCH4_MODEL_ID = "patch4-technology-shareholder-culture-v1"
_AUDIT_PATCH4_FORMULA_VERSION = "patch4-two-layer-weighted-v1"
_AUDIT_PATCH4_MAX_EVIDENCE_AGE_DAYS = 1_095
_AUDIT_PATCH4_EVIDENCE_SOURCE = "东方财富上市公司公告正文"
_AUDIT_PATCH4_EVIDENCE_ID = re.compile(
    r"^eastmoney-notice:(?P<code>[036][0-9]{5}):"
    r"(?P<art_code>AN[0-9]{18}):sha256:(?P<digest>[0-9a-f]{16})$"
)
_AUDIT_PATCH4_DETAIL_PREFIX = "https://data.eastmoney.com/notices/detail/"
_AUDIT_PATCH4_COMPONENT_WEIGHTS = {
    "p4_defensive_fairness": 25.0,
    "p4_defensive_governance": 15.0,
    "p4_core_rd_ownership": 15.0,
    "p4_esop_coverage": 15.0,
    "p4_long_term_rd_link": 15.0,
    "p4_frontline_rd_equity": 10.0,
    "p4_short_term_binding": 5.0,
}
_AUDIT_PATCH4_COMPONENT_LABELS = {
    "p4_defensive_fairness": "大小股东公平",
    "p4_defensive_governance": "治理透明与分红约束",
    "p4_core_rd_ownership": "核心研发持股",
    "p4_esop_coverage": "核心人才持股覆盖",
    "p4_long_term_rd_link": "长期研发指标绑定",
    "p4_frontline_rd_equity": "一线研发权益",
    "p4_short_term_binding": "短期股价绑定防范",
}
_AUDIT_TYPE7_RESEARCH_MODEL_ID = "type7-research-report-content-v4"
_AUDIT_TYPE7_CONTENT_MODEL_ID = "type7-report-body-crosscheck-v2"
_AUDIT_TYPE7_RESEARCH_MAX_AGE_DAYS = 365
_AUDIT_TYPE7_RESEARCH_RECENT_AGE_DAYS = 183
_AUDIT_TYPE7_MIN_BODY_SOURCES = 3
_AUDIT_TYPE7_MIN_CROSSCHECK_REPORTS = 2
_AUDIT_TYPE7_MAX_BODY_FETCHES = 6
_AUDIT_TYPE7_MIN_BODY_CHARACTERS = 200
_AUDIT_TYPE7_MAX_BODY_CHARACTERS = 100_000
_AUDIT_TYPE7_FACT_RELATIVE_TOLERANCE = 0.02
_AUDIT_TYPE7_MAX_FACTS_PER_BODY = 32
_AUDIT_TYPE7_MAX_FACT_ABS_VALUE = 1_000_000_000.0
_AUDIT_TYPE7_CONTENT_SIGNALS = {"analysis", "event", "forecast", "investment_view", "risk"}
_AUDIT_TYPE7_CONTENT_IDENTITY_CHECKS = {
    "code_in_body",
    "name_in_body",
    "detail_code",
    "detail_name",
    "detail_title",
    "detail_publisher",
    "detail_date",
    "dom_json_body",
}
_AUDIT_TYPE7_FACT_METRICS = {
    "adjusted_net_profit",
    "eps",
    "operating_cash_flow",
    "parent_net_profit",
    "revenue",
}
_AUDIT_TYPE7_FACT_UNITS = {"CNY_100M", "CNY_PER_SHARE"}
_AUDIT_TYPE7_FACT_UNIT_BY_METRIC = {
    "adjusted_net_profit": "CNY_100M",
    "eps": "CNY_PER_SHARE",
    "operating_cash_flow": "CNY_100M",
    "parent_net_profit": "CNY_100M",
    "revenue": "CNY_100M",
}
_AUDIT_TYPE7_TEMPLATE_WEIGHTS = {
    "template1": {f"t1_{index:02d}": 5.0 for index in range(1, 21)},
    "template5": {
        "t5_i1": 12.0,
        "t5_i2": 9.0,
        "t5_i3": 9.0,
        "t5_q1": 14.0,
        "t5_q2": 12.0,
        "t5_q3": 8.0,
        "t5_q4": 6.0,
        "t5_v1": 9.0,
        "t5_v2": 12.0,
        "t5_v3": 9.0,
    },
}
_AUDIT_TYPE7_PATCH_WEIGHTS = {
    "p5_business": {"p5_b1": 5.0, "p5_b2": 5.0, "p5_b3": 5.0, "p5_b4": 5.0},
    "p5_moat": {"p5_m1": 8.0, "p5_m2": 6.0, "p5_m3": 6.0},
    "p5_culture": {"p5_c1": 6.0, "p5_c2": 5.0, "p5_c3": 5.0, "p5_c4": 4.0},
    "p5_industry": {"p5_i1": 8.0, "p5_i2": 6.0, "p5_i3": 6.0},
    "p5_safety": {"p5_s1": 8.0, "p5_s2": 6.0, "p5_s3": 6.0},
}
_AUDIT_TYPE7_TEMPLATE1_CONTRACTS = {
    "t1_01": ("未来生命周期", "mean(runway,industry_durability)", ({"runway", "industry"},)),
    "t1_02": (
        "成长潜力",
        "mean(runway,revenue_CAGR_score,profit_FCF_CAGR_score,growth_stability)",
        ({"runway", "revenue_growth", "profit_fcf_growth", "growth_stability"},),
    ),
    "t1_03": ("主营收入增长", "piecewise_linear(revenue_CAGR)", ({"rate"},)),
    "t1_04": ("扣非利润与FCF增长", "mean(profit_CAGR_score,FCF_CAGR_score)", ({"profit_cagr", "fcf_cagr"},)),
    "t1_05": ("商业模式", "business", ({"score"},)),
    "t1_06": ("财务健康", "mean(accounting,balance,ROIC_spread)", ({"accounting", "balance", "roic"},)),
    "t1_07": ("细分产业环境", "industry", ({"score"},)),
    "t1_08": ("股东权益公平", "shareholder", ({"dilution"},)),
    "t1_09": ("长期竞争优势", "mean(moat,moat_durability)", ({"moat", "durability"},)),
    "t1_10": ("文化与员工满意", "culture", ({"management_proxy"},)),
    "t1_11": ("成本控制", "mean(margin_stability,accounting)", ({"margin", "accounting"},)),
    "t1_12": ("资产劳动资金强度", "asset_light", ({"asset_turnover", "capex_intensity"},)),
    "t1_13": ("弱周期属性", "cyclicality", ({"profit_volatility", "growth_consistency"},)),
    "t1_14": ("垄断性与竞争地位", "mean(moat,industry_structure)", ({"moat", "industry"},)),
    "t1_15": (
        "长期财富积累",
        "mean(moat_durability,profit_FCF_CAGR_score,ROIC_spread,accounting)",
        ({"moat_durability", "profit_fcf_growth", "roic", "accounting"},),
    ),
    "t1_16": ("奢侈品属性", "luxury", ({"gross_margin", "proxy_cap"},)),
    "t1_17": ("顶级科技与创新", "technology", ({"score"},)),
    "t1_18": (
        "长期预期回报",
        "expected_return",
        (
            {"earnings_growth_rate", "book_value_growth_rate"},
            {
                "earnings_growth_rate",
                "book_value_growth_rate",
                "horizon_years",
                "annual_return",
                "valuation_inputs",
                "formula",
            },
        ),
    ),
    "t1_19": (
        "十年回报与远期利润",
        "mean(hfq_10y_CAGR_score,market_cap/projected_year10_profit_score)",
        ({"shareholder_return", "terminal_profit_projection"},),
    ),
    "t1_20": (
        "DCF价格位置",
        "dcf",
        ({"type1_1a", "validation_basis"},),
    ),
}
_AUDIT_TYPE7_TEMPLATE5_LABELS = {
    "t5_i1": "产业大周期",
    "t5_i2": "产业小周期",
    "t5_i3": "产业空间与格局",
    "t5_q1": "商业模式",
    "t5_q2": "长期护城河",
    "t5_q3": "治理与股东文化",
    "t5_q4": "财务健康",
    "t5_v1": "历史估值分位",
    "t5_v2": "绝对DCF估值",
    "t5_v3": "预期回报率",
}
_AUDIT_TYPE7_EVIDENCE_LEVELS = {
    "partial",
    "primary",
    "derived_proxy",
    "derived_proxy_capped",
    "reported_formula",
    "validated_nonfinancial_dcf",
    "historical_valuation_reversion_formula",
    "independent_market_history",
    "independent_market_history_plus_fading_growth_projection",
}
_AUDIT_TYPE7_PATCH_SECTION_LABELS = {
    "p5_business": "商业模式",
    "p5_moat": "护城河",
    "p5_culture": "公司文化",
    "p5_industry": "产业兴衰",
    "p5_safety": "安全边际",
}
_AUDIT_TYPE7_PATCH_COMPONENT_LABELS = {
    "p5_b1": "清晰度",
    "p5_b2": "可扩展性",
    "p5_b3": "黏性复购",
    "p5_b4": "资本效率",
    "p5_m1": "护城河强度",
    "p5_m2": "定价权",
    "p5_m3": "进入壁垒",
    "p5_c1": "管理诚信",
    "p5_c2": "激励一致",
    "p5_c3": "创新适应",
    "p5_c4": "治理透明",
    "p5_i1": "生命周期",
    "p5_i2": "竞争格局",
    "p5_i3": "外部环境",
    "p5_s1": "估值水平",
    "p5_s2": "财务稳健",
    "p5_s3": "下行保护",
}
_AUDIT_TYPE7_PATCH_SOURCE_INPUT_COMPONENTS = {
    "p5_b1",
    "p5_b3",
    "p5_m2",
    "p5_m3",
    "p5_c3",
    "p5_c4",
    "p5_i3",
    "p5_s3",
}
_AUDIT_TYPE7_PATCH_SOURCE_LEVELS = {"missing", "primary", "derived_proxy"}
_AUDIT_TYPE7_PREREQUISITES = {
    "core_modules_80pct",
    "technology_patch4",
    "three_year_financials",
    "latest_quote_and_valuation",
    "three_external_reports",
    "external_report_content_verification",
    "ten_year_return_and_five_year_valuation",
}
_AUDIT_BAND_WACC_DELTA = 0.005
_AUDIT_BUBBLE_RATIO = 1.20
_AUDIT_DEEP_SAFETY_RATIO = 0.80
_AUDIT_EQUITY_RISK_PREMIUM = 0.05799671740067751
_AUDIT_FCF_MARGIN_FLOOR = 0.0
_AUDIT_FCF_MARGIN_LONG_TERM = 0.04
_AUDIT_FORECAST_YEARS = 5
_AUDIT_RISK_FREE_RATE = 0.017406
_AUDIT_NORMALISATION_PREMIUM_CAP = 1.25
_AUDIT_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_AUDIT_TTM_FCFF_FORMULA_VERSION = "ttm_cfo_less_capex_v2"
_AUDIT_TTM_REVENUE_FORMULA_VERSION = "ttm_revenue_v1"
_AUDIT_TTM_SOURCE_UNIT = "CNY"
_AUDIT_NONQUALITY_MARGIN_FLOOR = 0.08
_AUDIT_NONQUALITY_MARGIN_CAP = 0.25
_AUDIT_NONQUALITY_MARGIN_MULTIPLIER = 3.0
_RULE_FILES = (
    _ROOT / "config.py",
    _ROOT / "engine" / "buy_screener.py",
    _ROOT / "engine" / "dcf.py",
    _ROOT / "engine" / "market_coldness.py",
    _ROOT / "engine" / "quality_equity.py",
    _ROOT / "engine" / "quantitative_evidence.py",
    _ROOT / "engine" / "risk.py",
    _ROOT / "engine" / "scenarios.py",
    _ROOT / "engine" / "type7_patch6.py",
    _ROOT / "engine" / "valuation_status.py",
    _ROOT / "data" / "capex_evidence.py",
    _ROOT / "data" / "datacenter.py",
    _ROOT / "data" / "financial_indicator_evidence.py",
    _ROOT / "data" / "financial_source_evidence.py",
    _ROOT / "data" / "financial_balance_sheet_evidence.json",
    _ROOT / "data" / "financial_zero_capex_evidence.json",
    _ROOT / "data" / "financial_zero_revenue_evidence.json",
    _ROOT / "data" / "growth_evidence.py",
    _ROOT / "data" / "industry.py",
    _ROOT / "data" / "market_coldness.py",
    _ROOT / "data" / "market_history.py",
    _ROOT / "data" / "patch4_evidence.py",
    _ROOT / "data" / "quality_history.py",
    _ROOT / "data" / "research_reports.py",
    _ROOT / "data" / "sina_financial.py",
    _ROOT / "data" / "trading_calendar.py",
    _ROOT / "tools" / "china_a_share_trading_calendar.json",
)
_INDUSTRY_FILES = (
    _ROOT / "data" / "industry.py",
    _ROOT / "data" / "industry_f10.json",
    _ROOT / "data" / "industry_em_map.json",
    _ROOT / "data" / "industry_capco_2025h2.json",
    _ROOT / "data" / "industry_exchange_new_listings_2026.json",
)
_DEPENDENCY_FILES = (
    _ROOT / "requirements-bootstrap.txt",
    _ROOT / "requirements.txt",
    _ROOT / "requirements-lock.txt",
    _ROOT / "requirements-test.txt",
    _ROOT / "requirements-dev.txt",
    _ROOT / "requirements-dev-lock.txt",
    _ROOT / "pyproject.toml",
)
PATCH6_SOURCE_PATH = r"E:\模板汇总MD\补丁6.md"
PATCH6_SOURCE_SHA256 = "aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6"
TYPE7_SOURCE_DOCUMENTS = {
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
        "path_at_model_authoring": PATCH6_SOURCE_PATH,
        "sha256": PATCH6_SOURCE_SHA256,
    },
    "subsequent_addenda": {
        "path_at_model_authoring": r"E:\模板汇总MD\后续附加补丁们.md",
        "sha256": "0dea9125bbe2039acf741ac997e62b53c49b6e3dc32e7d956ed96f9d7054b64f",
    },
}
RISK_PARAMETER_SOURCES = {
    "model_as_of": "2026-07-15",
    "risk_free_rate_as_of": "2026-07-15",
    "risk_free_rate_source": "ChinaBond China Government Bond Yield Curve 10Y",
    "risk_free_rate_source_url": ("https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN"),
    "equity_risk_premium_as_of": "2026-04-01",
    "equity_risk_premium_basis": "china_rating_based_total_erp",
    "equity_risk_premium_source_url": ("https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx"),
    "ctrypremApr26.xlsx": "2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e",
    "industry_data_as_of": "2026-01-05",
    "betaChina.xls": "ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9",
    "waccChina.xls": "525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6",
}


def _sha256_files(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in paths if item.is_file()), key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(_ROOT).as_posix()
        except ValueError:
            relative = path.as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def _input_snapshot_hash(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
) -> str:
    digest = hashlib.sha256()
    quote_rows = quotes.copy()
    quote_rows["code"] = quote_rows["code"].map(_normalise_code)
    for row in quote_rows.sort_values("code", kind="stable").to_dict(orient="records"):
        digest.update(_canonical_json(row))
        digest.update(b"\n")
    for raw_code in sorted(financials, key=_normalise_code):
        digest.update(_normalise_code(raw_code).encode("ascii", errors="ignore"))
        digest.update(b"\0")
        digest.update(_canonical_json(financials[raw_code]))
        digest.update(b"\n")
    return digest.hexdigest()


def _git_metadata() -> dict[str, Any]:
    result: dict[str, Any] = {"commit": None, "dirty": None}
    git_executable = shutil.which("git")
    if not git_executable:
        return result
    try:
        # ``git_executable`` is an absolute path and every argument is fixed.
        commit = subprocess.run(  # nosec B603
            [git_executable, "rev-parse", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(  # nosec B603
            [git_executable, "status", "--porcelain"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        result = {"commit": commit or None, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _runtime_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in ("numpy", "orjson", "pandas", "pillow", "plotly", "requests", "streamlit", "gitpython"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def audit_state_hashes() -> dict[str, str]:
    """Hash every mutable source/contract input used by an audit run."""
    production_files = [path for directory in _PRODUCTION_CODE_DIRS for path in directory.rglob("*.py")] + [
        _ROOT / "app.py",
        _ROOT / "config.py",
    ]
    return {
        "code_sha256": _sha256_files(production_files),
        "rules_sha256": _sha256_files(_RULE_FILES),
        "industry_sha256": _sha256_files(_INDUSTRY_FILES),
        "dependency_manifest_sha256": _sha256_files(_DEPENDENCY_FILES),
    }


def _build_provenance(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    eligible_codes: tuple[str, ...],
    *,
    snapshot_sha256: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state_hashes = audit_state_hashes()
    provenance = {
        "audit_schema_version": 5,
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "snapshot_content_sha256": _input_snapshot_hash(quotes, financials),
        "snapshot_artifact_sha256": snapshot_sha256,
        "eligible_universe_sha256": hashlib.sha256("\n".join(eligible_codes).encode("ascii")).hexdigest(),
        **state_hashes,
        "patch6_source": {
            "path_at_model_authoring": PATCH6_SOURCE_PATH,
            "sha256": PATCH6_SOURCE_SHA256,
        },
        "type7_source_documents": {key: dict(value) for key, value in TYPE7_SOURCE_DOCUMENTS.items()},
        "risk_parameter_sources": dict(RISK_PARAMETER_SOURCES),
        "scoring_verification_scope": {
            "same_source_replay": "recomputes every published field from reordered production inputs",
            "same_source_valuation_replay": "recomputes valuation existence, payloads, skip reasons and sampled issues",
            "independent_runtime_checks": "recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding",
            "business_rule_oracle": "fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py",
        },
        "git": _git_metadata(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": _runtime_versions(),
        },
    }
    if isinstance(metadata, Mapping):
        provenance["caller_metadata"] = dict(metadata)
    return provenance


def _analysis_skip_reasons(analysis: MarketAnalysisOutcome) -> dict[str, str]:
    for field in ("dcf_skip_reasons", "dcf_skipped_reasons", "skipped_reasons"):
        value = getattr(analysis, field, None)
        if isinstance(value, Mapping):
            return {_normalise_code(code): str(reason or "evidence unavailable") for code, reason in value.items()}
    return {}


def _analysis_skip_classifications(analysis: MarketAnalysisOutcome) -> dict[str, dict[str, str]]:
    value = getattr(analysis, "dcf_skip_classifications", None)
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, str]] = {}
    for raw_code, raw_classification in value.items():
        classification = normalize_dcf_skip_classification(raw_classification)
        if classification is not None:
            result[_normalise_code(raw_code)] = classification
    return result


def _market_coldness_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Record bulk evidence identity without embedding the whole market payload."""
    if evidence is None:
        return {
            "provided": False,
            "evidence_count": 0,
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "evidence_sha256": None,
            "sources": [],
            "as_of_sessions": [],
        }
    normalized: dict[str, Mapping[str, Any]] = {}
    sources: set[str] = set()
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not code or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("market_coldness_evidence contains an invalid or duplicate code payload")
        normalized[code] = payload
        source_evidence = payload.get("market_coldness_score_evidence")
        if isinstance(source_evidence, Mapping):
            source = source_evidence.get("source")
            as_of = source_evidence.get("as_of")
            if isinstance(source, str) and source.strip():
                sources.add(source.strip())
            if isinstance(as_of, str) and as_of.strip():
                as_of_sessions.add(as_of.strip())
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": True,
        "evidence_count": len(normalized),
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest(),
        "sources": sorted(sources),
        "as_of_sessions": sorted(as_of_sessions),
    }


def _quality_history_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Bind every acquired Type 7 long-history record without duplicating it."""
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise ValueError("quality_history_evidence must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    available = 0
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not re.fullmatch(r"[036][0-9]{5}", code) or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("quality_history_evidence contains an invalid or duplicate code payload")
        if str(payload.get("code") or "") != code or payload.get("model_id") != "type7-market-history-v1":
            raise ValueError("quality_history_evidence identity differs from its mapping key")
        normalized[code] = payload
        available += int(payload.get("available") is True)
        as_of = payload.get("as_of")
        if isinstance(as_of, str) and as_of:
            as_of_sessions.add(as_of)
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": bool(normalized),
        "evidence_count": len(normalized),
        "available_count": available,
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest() if normalized else None,
        "as_of_sessions": sorted(as_of_sessions),
    }


def _research_report_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Bind every acquired Type 7 report batch record without embedding it."""

    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise ValueError("research_report_evidence must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    available = 0
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not re.fullmatch(r"[036][0-9]{5}", code) or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("research_report_evidence contains an invalid or duplicate code payload")
        if str(payload.get("code") or "") != code or payload.get("model_id") != _AUDIT_TYPE7_RESEARCH_MODEL_ID:
            raise ValueError("research_report_evidence identity differs from its mapping key")
        normalized[code] = payload
        available += int(payload.get("available") is True)
        as_of = payload.get("as_of")
        if isinstance(as_of, str) and as_of:
            as_of_sessions.add(as_of)
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": bool(normalized),
        "evidence_count": len(normalized),
        "available_count": available,
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest() if normalized else None,
        "as_of_sessions": sorted(as_of_sessions),
    }


def _patch4_captured_binding_index(
    evidence: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Bind each assessment evidence ID to the exact captured announcement body."""

    index: dict[str, dict[str, dict[str, str]]] = {}
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not isinstance(payload, Mapping):
            raise ValueError("patch4_evidence contains a non-mapping record")
        try:
            normalized = validate_patch4_evidence_record(
                payload,
                code,
                str(payload.get("as_of") or ""),
            )
        except (Patch4EvidenceError, TypeError, ValueError) as exc:
            raise ValueError(f"patch4_evidence record is invalid:{code}") from exc
        assessment = normalized.get("assessment")
        if normalized.get("available") is not True or not isinstance(assessment, Mapping):
            continue
        documents = normalized.get("documents")
        if not isinstance(documents, list):
            raise ValueError(f"patch4_evidence documents are invalid:{code}")
        document_by_id = {
            (f"eastmoney-notice:{code}:{document['art_code']}:sha256:{document['content_sha256'][:16]}"): document
            for document in documents
            if isinstance(document, Mapping)
        }
        criteria = assessment.get("criteria")
        if not isinstance(criteria, Mapping):
            raise ValueError(f"patch4_evidence assessment criteria are invalid:{code}")
        company_bindings: dict[str, dict[str, str]] = {}
        for criterion in criteria.values():
            criterion_evidence = criterion.get("evidence") if isinstance(criterion, Mapping) else None
            evidence_id = criterion_evidence.get("evidence_id") if isinstance(criterion_evidence, Mapping) else None
            document = document_by_id.get(evidence_id)
            if not isinstance(evidence_id, str) or not isinstance(document, Mapping):
                raise ValueError(f"patch4_evidence assessment is not bound to a captured document:{code}")
            binding = {
                "evidence_id": evidence_id,
                "url": str(document["url"]),
                "as_of": str(document["as_of"]),
                "content_sha256": str(document["content_sha256"]),
            }
            existing = company_bindings.get(evidence_id)
            if existing is not None and existing != binding:
                raise ValueError(f"patch4_evidence binding is ambiguous:{code}")
            company_bindings[evidence_id] = binding
        if company_bindings:
            index[code] = dict(sorted(company_bindings.items()))
    return index


def _patch4_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Bind every requested Patch 4 announcement result, including unknowns."""

    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise ValueError("patch4_evidence must be a mapping")
    binding_index = _patch4_captured_binding_index(evidence)
    normalized: dict[str, Mapping[str, Any]] = {}
    available = 0
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not re.fullmatch(r"[036][0-9]{5}", code) or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("patch4_evidence contains an invalid or duplicate code payload")
        if (
            str(payload.get("code") or "") != code
            or payload.get("model_id") != PATCH4_PUBLIC_EVIDENCE_MODEL_ID
            or not isinstance(payload.get("available"), bool)
        ):
            raise ValueError("patch4_evidence identity differs from its mapping key")
        normalized[code] = payload
        available += int(payload["available"])
        as_of = payload.get("as_of")
        if isinstance(as_of, str) and as_of:
            as_of_sessions.add(as_of)
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": bool(normalized),
        "evidence_count": len(normalized),
        "available_count": available,
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest() if normalized else None,
        "as_of_sessions": sorted(as_of_sessions),
        "assessment_evidence_bindings": {
            code: list(bindings.values()) for code, bindings in sorted(binding_index.items())
        },
    }


def _type3_growth_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Bind every acquired Type 3 deep-evidence record without embedding it."""

    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise ValueError("type3_growth_evidence must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    available = 0
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not re.fullmatch(r"[036][0-9]{5}", code) or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("type3_growth_evidence contains an invalid or duplicate code payload")
        if str(payload.get("code") or "") != code or payload.get("model_id") != "type3-growth-evidence-v1":
            raise ValueError("type3_growth_evidence identity differs from its mapping key")
        normalized[code] = payload
        available += int(payload.get("available") is True)
        as_of = payload.get("as_of")
        if isinstance(as_of, str) and as_of:
            as_of_sessions.add(as_of)
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": bool(normalized),
        "evidence_count": len(normalized),
        "available_count": available,
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest() if normalized else None,
        "as_of_sessions": sorted(as_of_sessions),
    }


_AUDIT_SCENARIOS = ("pessimistic", "neutral", "optimistic")
_AUDIT_WACC_SHIFT = {"pessimistic": 0.010, "neutral": 0.0, "optimistic": -0.005}
_AUDIT_FINANCIAL_INDUSTRIES = {"BANK", "INSURANCE", "SECURITIES"}
_AUDIT_TYPE7_FINANCIAL_INDUSTRIES = _AUDIT_FINANCIAL_INDUSTRIES | {"FINANCIAL_OTHER"}
_AUDIT_PARENT_EQUITY_KEYS = (
    "PARENT_EQUITY",
    "TOTAL_PARENT_EQUITY",
    "TOTAL_EQUITY_ATTR_P",
    "EQUITY_ATTRIBUTABLE_TO_PARENT",
    "PARENT_NET_ASSETS",
    "归属于母公司股东权益",
)
_AUDIT_MINORITY_EQUITY_KEYS = (
    "MINORITY_EQUITY",
    "MINORITY_INTEREST",
    "少数股东权益",
)


def _finite(value: Any) -> float | None:
    """Return a finite non-boolean scalar; pandas missing values fail closed."""
    if value is None or isinstance(value, bool) or value is pd.NA or value is pd.NaT:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    # NumPy boolean scalars are legitimate dataframe scalars, but integers,
    # truthy strings and missing values must never pass as public booleans.
    if type(value).__name__ == "bool_" and type(value).__module__.startswith("numpy"):
        return bool(value)
    return None


def _optional_text(value: Any) -> str | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return str(value)


def _close(actual: Any, expected: float, *, rel_tol: float = 1e-8, abs_tol: float = 1e-9) -> bool:
    number = _finite(actual)
    return number is not None and math.isclose(number, expected, rel_tol=rel_tol, abs_tol=abs_tol)


def _audit_type7_history_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _audit_type7_history_integer(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _audit_type7_years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _audit_type7_shareholder_history_valid(value: Any, as_of: date) -> bool:
    """Independently replay the embedded ten-year total-return contract."""

    if not isinstance(value, Mapping) or value.get("available") is not True:
        return False
    start = _audit_type7_history_date(value.get("start_date"))
    end = _audit_type7_history_date(value.get("end_date"))
    span_days = _audit_type7_history_integer(value.get("span_days"), minimum=1)
    observations = _audit_type7_history_integer(value.get("observations"), minimum=2)
    start_close = _finite(value.get("start_close_hfq"))
    end_close = _finite(value.get("end_close_hfq"))
    total_return = _finite(value.get("total_return"))
    cagr = _finite(value.get("cagr"))
    if (
        value.get("target_years") != 10
        or value.get("formula") != _AUDIT_TYPE7_SHAREHOLDER_RETURN_FORMULA
        or start is None
        or end is None
        or span_days is None
        or observations is None
        or start_close is None
        or start_close <= 0
        or end_close is None
        or end_close <= 0
        or total_return is None
        or cagr is None
    ):
        return False
    target = _audit_type7_years_before(as_of, 10)
    if (
        span_days != (end - start).days
        or span_days
        < _AUDIT_TYPE7_TEN_YEAR_TARGET_DAYS
        - _AUDIT_TYPE7_TEN_YEAR_START_TOLERANCE_DAYS
        - _AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS
        or not 0 <= (start - target).days <= _AUDIT_TYPE7_TEN_YEAR_START_TOLERANCE_DAYS
        or not 0 <= (as_of - end).days <= _AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS
    ):
        return False
    ratio = end_close / start_close
    return math.isclose(total_return, ratio - 1.0, rel_tol=1e-9, abs_tol=1e-9) and math.isclose(
        cagr,
        ratio ** (365.2425 / span_days) - 1.0,
        rel_tol=1e-9,
        abs_tol=1e-9,
    )


def _audit_type7_replay_valuation_distribution(
    value: Any,
    current: Any,
) -> dict[str, float | int | None] | None:
    """Replay one compact weighted distribution without production helpers."""

    if not isinstance(value, Mapping) or set(value) != {"values", "counts"}:
        return None
    values = value.get("values")
    counts = value.get("counts")
    if (
        not isinstance(values, list)
        or not isinstance(counts, list)
        or len(values) != len(counts)
        or len(values) > _AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS
    ):
        return None
    clean_values: list[float] = []
    clean_counts: list[int] = []
    previous: float | None = None
    for raw_value, raw_count in zip(values, counts, strict=True):
        number = _finite(raw_value)
        if (
            number is None
            or number <= 0
            or (previous is not None and number <= previous)
            or isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count <= 0
        ):
            return None
        clean_values.append(number)
        clean_counts.append(raw_count)
        previous = number
    observations = sum(clean_counts)
    if observations > _AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS or (clean_values and observations <= 0):
        return None
    if not clean_values:
        return {"observations": 0, "median": None, "percentile": None}

    def value_at(rank: int) -> float:
        cumulative = 0
        for number, count in zip(clean_values, clean_counts, strict=True):
            cumulative += count
            if rank < cumulative:
                return number
        raise ValueError("weighted valuation rank exceeds the distribution")

    lower = value_at((observations - 1) // 2)
    upper = value_at(observations // 2)
    current_value = _finite(current)
    percentile = None
    if current_value is not None and current_value > 0:
        below = sum(count for number, count in zip(clean_values, clean_counts, strict=True) if number < current_value)
        equal = sum(count for number, count in zip(clean_values, clean_counts, strict=True) if number == current_value)
        percentile = (below + 0.5 * equal) / observations
    return {
        "observations": observations,
        "median": (lower + upper) / 2.0,
        "percentile": percentile,
    }


def _audit_type7_valuation_history_replay(
    value: Any,
    as_of: date,
) -> dict[str, dict[str, float | int]] | None:
    """Validate and replay both raw PE/PB distributions in an embedded contract."""

    if not isinstance(value, Mapping) or value.get("available") is not True:
        return None
    start = _audit_type7_history_date(value.get("start_date"))
    end = _audit_type7_history_date(value.get("end_date"))
    target_start = _audit_type7_history_date(value.get("target_start_date"))
    span_days = _audit_type7_history_integer(value.get("span_days"), minimum=1)
    start_delay = _audit_type7_history_integer(value.get("start_delay_days"), minimum=0)
    row_count = _audit_type7_history_integer(value.get("row_count"), minimum=1)
    expected_target = _audit_type7_years_before(as_of, 5)
    if (
        value.get("window_years") != 5
        or value.get("formula") != _AUDIT_TYPE7_VALUATION_PERCENTILE_FORMULA
        or start is None
        or end is None
        or target_start != expected_target
        or span_days is None
        or start_delay is None
        or row_count is None
        or row_count > _AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS
        or span_days != (end - start).days
        or start_delay != (start - expected_target).days
        or span_days
        < _AUDIT_TYPE7_FIVE_YEAR_TARGET_DAYS
        - _AUDIT_TYPE7_FIVE_YEAR_START_TOLERANCE_DAYS
        - _AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS
        or not 0 <= start_delay <= _AUDIT_TYPE7_FIVE_YEAR_START_TOLERANCE_DAYS
        or not 0 <= (as_of - end).days <= _AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS
    ):
        return None

    usable: dict[str, dict[str, float | int]] = {}
    for prefix, current_key, median_key in (
        ("pe", "current_pe_ttm", "median_pe_ttm"),
        ("pb", "current_pb_mrq", "median_pb_mrq"),
    ):
        observations = _audit_type7_history_integer(value.get(f"{prefix}_observations"), minimum=0)
        current = _finite(value.get(current_key))
        declared_median = _finite(value.get(median_key))
        declared_percentile = _finite(value.get(f"{prefix}_percentile"))
        replay = _audit_type7_replay_valuation_distribution(value.get(f"{prefix}_distribution"), current)
        if (
            observations is None
            or replay is None
            or observations != replay["observations"]
            or row_count < observations + 1
            or (current is not None and current <= 0)
        ):
            return None
        series_usable = bool(
            observations >= _AUDIT_TYPE7_VALUATION_MIN_OBSERVATIONS and current is not None and current > 0
        )
        if series_usable:
            replay_median = replay["median"]
            replay_percentile = replay["percentile"]
            if (
                declared_median is None
                or declared_median <= 0
                or declared_percentile is None
                or not 0 <= declared_percentile <= 1
                or replay_median is None
                or replay_percentile is None
                or not math.isclose(declared_median, float(replay_median), rel_tol=0.0, abs_tol=1e-9)
                or not math.isclose(
                    declared_percentile,
                    float(replay_percentile),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                return None
            usable[prefix] = {
                "observations": observations,
                "median": declared_median,
                "percentile": declared_percentile,
            }
        elif value.get(median_key) is not None or value.get(f"{prefix}_percentile") is not None:
            return None
    return usable or None


def _audit_type5_pb_bottom_score(percentile: float, current_pb: float) -> float:
    percentile_score = (
        10.0
        if percentile <= 0.10
        else 8.0
        if percentile <= 0.20
        else 6.0
        if percentile <= 0.30
        else 4.0
        if percentile <= 0.50
        else 2.0
    )
    absolute_score = (
        10.0
        if current_pb <= 1.0
        else 8.0
        if current_pb <= 1.2
        else 6.0
        if current_pb <= 1.5
        else 4.0
        if current_pb <= 2.0
        else 2.0
    )
    return min(percentile_score, absolute_score)


def _audit_type5_valuation_replay(value: Any, as_of: date) -> tuple[float, float] | None:
    if not isinstance(value, Mapping) or value.get("available") is not True:
        return None
    observations = _audit_type7_history_integer(value.get("pb_observations"), minimum=0)
    span_days = _audit_type7_history_integer(value.get("span_days"), minimum=0)
    start_delay = _audit_type7_history_integer(value.get("start_delay_days"), minimum=0)
    end = _audit_type7_history_date(value.get("end_date"))
    current_pb = _finite(value.get("current_pb_mrq"))
    declared_median = _finite(value.get("median_pb_mrq"))
    declared_percentile = _finite(value.get("pb_percentile"))
    replay = _audit_type7_replay_valuation_distribution(value.get("pb_distribution"), current_pb)
    if (
        value.get("window_years") != 5
        or value.get("formula") != _AUDIT_TYPE7_VALUATION_PERCENTILE_FORMULA
        or observations is None
        or not _AUDIT_TYPE7_VALUATION_MIN_OBSERVATIONS <= observations <= _AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS
        or span_days is None
        or span_days < _AUDIT_TYPE5_HISTORY_MIN_SPAN_DAYS
        or start_delay is None
        or start_delay > _AUDIT_TYPE5_HISTORY_MAX_START_DELAY_DAYS
        or end is None
        or not 0 <= (as_of - end).days <= _AUDIT_TYPE5_HISTORY_MAX_LATEST_AGE_DAYS
        or current_pb is None
        or current_pb <= 0
        or declared_median is None
        or declared_median <= 0
        or declared_percentile is None
        or not 0 <= declared_percentile <= 1
        or replay is None
        or replay["observations"] != observations
        or replay["median"] is None
        or replay["percentile"] is None
        or not math.isclose(declared_median, float(replay["median"]), rel_tol=0.0, abs_tol=1e-9)
        or not math.isclose(
            declared_percentile,
            float(replay["percentile"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        return None
    return declared_percentile, current_pb


def _audit_type5_coldness_interpolate(value: float, bands: Sequence[tuple[float, float]]) -> float:
    if value <= bands[0][0]:
        return bands[0][1]
    for (left_x, left_score), (right_x, right_score) in zip(bands, bands[1:]):
        if value <= right_x:
            fraction = (value - left_x) / (right_x - left_x)
            return left_score + fraction * (right_score - left_score)
    return bands[-1][1]


def _audit_type5_market_replay(value: Any, *, code: str, as_of: str) -> float | None:
    if not isinstance(value, Mapping) or set(value) != {
        "score",
        "evidence_level",
        "evidence",
        "components",
    }:
        return None
    score = _finite(value.get("score"))
    evidence = value.get("evidence")
    components = value.get("components")
    if (
        score is None
        or not 0 <= score <= 10
        or value.get("evidence_level") not in {"primary", "derived_proxy"}
        or not isinstance(evidence, Mapping)
        or not isinstance(components, Mapping)
    ):
        return None
    if set(evidence) - {"source", "evidence_id", "as_of", "summary"}:
        return None
    source = evidence.get("source")
    evidence_id = evidence.get("evidence_id")
    evidence_as_of = evidence.get("as_of")
    summary = evidence.get("summary")
    tokens = (
        {
            token.upper()
            for token in re.split(r"[^A-Za-z0-9._]+", evidence_id)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", token)
        }
        if isinstance(evidence_id, str)
        else set()
    )
    if (
        not isinstance(source, str)
        or not source.strip()
        or len(source.strip()) > 200
        or not isinstance(evidence_id, str)
        or not evidence_id.strip()
        or len(evidence_id.strip()) > 200
        or code.upper() not in tokens
        or evidence_as_of != as_of
        or (summary is not None and (not isinstance(summary, str) or len(summary.strip()) > 1_000))
        or components.get("as_of_session") != as_of
    ):
        return None
    raw_values = components.get("raw_values")
    if not isinstance(raw_values, Mapping):
        return None
    full_components = "absolute" in components
    if full_components:
        expected_component_fields = {
            "schema_version",
            "model_id",
            "code",
            "source",
            "raw_values",
            "absolute",
            "relative",
            "relative_sample_sizes",
            "relative_context",
            "metric_scores",
            "weights",
            "ytd_reliability",
            "price_score",
            "raw_score",
            "score_cap",
            "caps",
            "board",
            "retrieved_at",
            "as_of_session",
            "source_url",
            "source_updated_at",
        }
        expected_board = (
            "STAR"
            if code.startswith(("688", "689"))
            else "CHINEXT"
            if code.startswith(("300", "301"))
            else "SH_MAIN"
            if code.startswith("6")
            else "SZ_MAIN"
            if code.startswith(("0", "3"))
            else None
        )
        if (
            set(evidence) != {"source", "evidence_id", "as_of", "summary"}
            or set(components) != expected_component_fields
            or components.get("schema_version") != _AUDIT_TYPE5_COLDNESS_EVIDENCE_SCHEMA_VERSION
            or components.get("model_id") != _AUDIT_TYPE5_COLDNESS_MODEL_ID
            or components.get("code") != code
            or components.get("source") != _AUDIT_TYPE5_COLDNESS_SOURCE
            or components.get("source_url") != _AUDIT_TYPE5_COLDNESS_SOURCE_URL
            or components.get("as_of_session") != as_of
            or components.get("board") != expected_board
            or source != f"{_AUDIT_TYPE5_COLDNESS_SOURCE}; {_AUDIT_TYPE5_COLDNESS_SOURCE_URL}"
            or evidence_id != f"{_AUDIT_TYPE5_COLDNESS_MODEL_ID}:{code}:{as_of.replace('-', '')}"
        ):
            return None
    expected_raw_keys = (
        set(_AUDIT_TYPE5_COLDNESS_WEIGHTS)
        if full_components
        else {
            "change_60d_pct",
            "change_ytd_pct",
        }
    )
    if set(raw_values) != expected_raw_keys:
        return None
    values = {key: _finite(raw_values.get(key)) for key in expected_raw_keys}
    change_60d = values.get("change_60d_pct")
    change_ytd = values.get("change_ytd_pct")
    if change_60d is None or change_ytd is None or not -100 <= change_60d <= 1_000 or not -100 <= change_ytd <= 1_000:
        return None
    if full_components:
        if values.get("turnover_rate_pct") is None or values["turnover_rate_pct"] < 0:
            return None
        if raw_values.get("volume_ratio") is None:
            values["volume_ratio"] = None
        elif values.get("volume_ratio") is None or values["volume_ratio"] < 0:
            return None
        available = [key for key in _AUDIT_TYPE5_COLDNESS_WEIGHTS if values.get(key) is not None]
        absolute = components.get("absolute")
        relative = components.get("relative")
        relative_samples = components.get("relative_sample_sizes")
        relative_context = components.get("relative_context")
        metric_scores = components.get("metric_scores")
        weights = components.get("weights")
        if (
            not isinstance(absolute, Mapping)
            or set(absolute) != set(available)
            or not isinstance(relative, Mapping)
            or not isinstance(relative_samples, Mapping)
            or not isinstance(relative_context, Mapping)
            or set(relative_context) != set(available)
            or not isinstance(metric_scores, Mapping)
            or set(metric_scores) != set(available)
            or not isinstance(weights, Mapping)
            or set(weights) != set(available)
        ):
            return None
        expected_absolute = {
            key: _audit_type5_coldness_interpolate(float(values[key]), _AUDIT_TYPE5_COLDNESS_BANDS[key])
            for key in available
        }
        expected_relative: dict[str, float] = {}
        expected_relative_samples: dict[str, int] = {}
        context_fields = {
            "section_size",
            "minimum_section_records",
            "section_population",
            "source_present",
            "source_total",
            "lower_count",
            "equal_count",
        }
        for key in available:
            context = relative_context.get(key)
            if not isinstance(context, Mapping) or set(context) != context_fields:
                return None
            if any(isinstance(context[field], bool) or not isinstance(context[field], int) for field in context_fields):
                return None
            section_size = context["section_size"]
            minimum = context["minimum_section_records"]
            population = context["section_population"]
            source_present = context["source_present"]
            source_total = context["source_total"]
            lower = context["lower_count"]
            equal = context["equal_count"]
            if (
                minimum < 1
                or section_size < 1
                or population < section_size
                or source_total < source_present
                or source_present < 1
                or lower < 0
                or equal < 1
                or lower + equal > section_size
            ):
                return None
            relative_eligible = (
                section_size >= minimum
                and section_size / population >= _AUDIT_TYPE5_COLDNESS_MIN_SOURCE_COVERAGE
                and source_present / source_total >= _AUDIT_TYPE5_COLDNESS_MIN_SOURCE_COVERAGE
            )
            if relative_eligible:
                if section_size < 2 or equal == section_size:
                    rank_score = 5.0
                else:
                    greater = section_size - lower - equal
                    rank_score = 1.0 + 8.0 * (greater + 0.5 * (equal - 1)) / (section_size - 1)
                    rank_score = max(1.0, min(9.0, rank_score))
                expected_relative[key] = rank_score
                expected_relative_samples[key] = section_size
        if (
            set(relative) != set(expected_relative)
            or set(relative_samples) != set(expected_relative_samples)
            or any(
                not _close(relative.get(key), round(expected_relative[key], 6), rel_tol=0.0, abs_tol=1e-6)
                for key in expected_relative
            )
            or any(
                isinstance(relative_samples.get(key), bool)
                or relative_samples.get(key) != expected_relative_samples[key]
                for key in expected_relative
            )
        ):
            return None
        expected_metric_scores: dict[str, float] = {}
        for key in available:
            expected_metric_scores[key] = (
                0.8 * expected_absolute[key] + 0.2 * expected_relative[key]
                if key in expected_relative
                else expected_absolute[key]
            )
        ytd_reliability = _finite(components.get("ytd_reliability"))
        if ytd_reliability is None or not 0 <= ytd_reliability <= 1:
            return None
        expected_weights = dict(_AUDIT_TYPE5_COLDNESS_WEIGHTS)
        expected_weights["change_ytd_pct"] *= ytd_reliability
        if any(
            not _close(absolute.get(key), round(expected_absolute[key], 6), rel_tol=0.0, abs_tol=1e-6)
            or not _close(
                metric_scores.get(key),
                round(expected_metric_scores[key], 6),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            or not _close(weights.get(key), round(expected_weights[key], 6), rel_tol=0.0, abs_tol=1e-6)
            for key in available
        ):
            return None
        total_weight = math.fsum(expected_weights[key] for key in available)
        raw_score = math.fsum(expected_metric_scores[key] * expected_weights[key] for key in available) / total_weight
        price_weight = expected_weights["change_60d_pct"] + expected_weights["change_ytd_pct"]
        price_score = (
            expected_metric_scores["change_60d_pct"] * expected_weights["change_60d_pct"]
            + expected_metric_scores["change_ytd_pct"] * expected_weights["change_ytd_pct"]
        ) / price_weight
        score_cap = (
            _AUDIT_TYPE5_MAX_COLDNESS_SCORE
            if values.get("volume_ratio") is not None
            else _AUDIT_TYPE5_MAX_COLDNESS_SCORE_WITHOUT_VOLUME
        )
        caps = [f"evidence_cap={score_cap:.1f}"]
        if expected_absolute["change_60d_pct"] <= 3.0:
            score_cap = min(score_cap, 3.0)
            caps.append("60d_hot_cap=3.0")
        elif price_score < 5.0:
            score_cap = min(score_cap, 4.9)
            caps.append("price_coldness_lt5_cap=4.9")
        elif price_score < 6.0:
            score_cap = min(score_cap, 6.9)
            caps.append("price_coldness_lt6_cap=6.9")
        if (
            not _close(components.get("raw_score"), round(raw_score, 6), rel_tol=0.0, abs_tol=1e-6)
            or not _close(components.get("price_score"), round(price_score, 6), rel_tol=0.0, abs_tol=1e-6)
            or not _close(components.get("score_cap"), score_cap, rel_tol=0.0, abs_tol=1e-6)
            or components.get("caps") != caps
            or not math.isclose(score, round(max(1.0, min(score_cap, raw_score)), 1), rel_tol=0.0, abs_tol=1e-9)
        ):
            return None
    confirmed_decline = max(change_60d, change_ytd)
    drawdown_score = (
        10.0
        if confirmed_decline <= -30
        else 9.0
        if confirmed_decline <= -20
        else 7.0
        if confirmed_decline <= -10
        else 5.0
        if confirmed_decline <= -5
        else 3.0
        if confirmed_decline < 0
        else 1.0
    )
    return min(score, drawdown_score)


def _audit_type5_cycle_series(values: Any, years: Any, *, as_of: date) -> tuple[list[float], list[int]] | None:
    if not isinstance(values, list) or not isinstance(years, list) or len(values) != len(years):
        return None
    if not values and not years:
        return [], []
    if not 4 <= len(values) <= 10:
        return None
    clean_values: list[float] = []
    clean_years: list[int] = []
    for raw_value, raw_year in zip(values, years, strict=True):
        number = _finite(raw_value)
        if number is None or isinstance(raw_year, bool) or not isinstance(raw_year, int):
            return None
        clean_values.append(number)
        clean_years.append(raw_year)
    if (
        clean_years != sorted(set(clean_years))
        or any(current - prior != 1 for prior, current in zip(clean_years, clean_years[1:]))
        or clean_years[-1] != as_of.year - 1
    ):
        return None
    return clean_values, clean_years


def _audit_type5_cycle_low_score(values: Sequence[float]) -> float | None:
    spread = max(values) - min(values) if len(values) >= 4 else 0.0
    if spread <= 0:
        return None
    position = (values[-1] - min(values)) / spread
    return (
        10.0
        if position <= 0.10
        else 8.0
        if position <= 0.25
        else 6.0
        if position <= 0.40
        else 4.0
        if position <= 0.60
        else 2.0
    )


def _audit_type5_financial_replay(value: Any, *, as_of: date) -> tuple[float, str] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "gross_margin_history",
        "gross_margin_years",
        "net_profit_history",
        "net_profit_years",
    }:
        return None
    margins = _audit_type5_cycle_series(
        value.get("gross_margin_history"),
        value.get("gross_margin_years"),
        as_of=as_of,
    )
    profits = _audit_type5_cycle_series(
        value.get("net_profit_history"),
        value.get("net_profit_years"),
        as_of=as_of,
    )
    if margins is None or profits is None:
        return None
    candidates: list[tuple[float, str]] = []
    margin_values, _margin_years = margins
    if margin_values:
        changes = [current - prior for prior, current in zip(margin_values, margin_values[1:])]
        if (
            max(margin_values) - min(margin_values) > 0.15
            and any(change < 0 for change in changes)
            and any(change > 0 for change in changes)
        ):
            score = _audit_type5_cycle_low_score(margin_values)
            if score is not None:
                candidates.append((score, "毛"))
    profit_values, _profit_years = profits
    if profit_values:
        changes = [current - prior for prior, current in zip(profit_values, profit_values[1:])]
        scale = max(abs(float(statistics.median(profit_values))), 1.0)
        if (
            any(change < 0 for change in changes)
            and any(change > 0 for change in changes)
            and (max(profit_values) - min(profit_values)) / scale >= 0.50
        ):
            score = _audit_type5_cycle_low_score(profit_values)
            if score is not None:
                candidates.append((score, "利"))
    return max(candidates, default=None, key=lambda item: (item[0], item[1]))


def _audit_type5_bottom_contract_replay(
    contract: Any,
    *,
    code: str,
    as_of: Any,
    row_pb: Any,
) -> tuple[float, str] | None:
    if not isinstance(contract, Mapping) or set(contract) != {
        "schema_version",
        "model_id",
        "code",
        "as_of",
        "quote_pb",
        "valuation_history",
        "market_coldness_record",
        "financial_cycle",
    }:
        return None
    reference = _audit_type7_history_date(as_of)
    quote_pb = _finite(contract.get("quote_pb"))
    published_pb = _finite(row_pb)
    quote_binding_valid = (
        contract.get("quote_pb") is None
        and row_pb is None
        or quote_pb is not None
        and quote_pb > 0
        and published_pb is not None
        and published_pb > 0
        and math.isclose(quote_pb, published_pb, rel_tol=0.0, abs_tol=1e-9)
    )
    if (
        contract.get("schema_version") != _AUDIT_TYPE5_BOTTOM_EVIDENCE_SCHEMA_VERSION
        or contract.get("model_id") != _AUDIT_TYPE5_BOTTOM_EVIDENCE_MODEL_ID
        or contract.get("code") != code
        or contract.get("as_of") != as_of
        or reference is None
        or reference > date.today()
        or not quote_binding_valid
    ):
        return None
    valuation = _audit_type5_valuation_replay(contract.get("valuation_history"), reference)
    market_score = _audit_type5_market_replay(contract.get("market_coldness_record"), code=code, as_of=as_of)
    financial = _audit_type5_financial_replay(contract.get("financial_cycle"), as_of=reference)
    if valuation is None or market_score is None or financial is None:
        return None
    percentile, current_pb = valuation
    if quote_pb is not None and abs(quote_pb - current_pb) / max(quote_pb, current_pb) > 0.20:
        return None
    valuation_score = _audit_type5_pb_bottom_score(percentile, current_pb)
    financial_score, financial_basis = financial
    raw_score = 0.40 * valuation_score + 0.30 * market_score + 0.30 * financial_score
    resonant_sources = sum(score >= 6.0 for score in (valuation_score, market_score, financial_score))
    if resonant_sources < 2:
        raw_score = min(raw_score, 4.0)
    elif resonant_sources < 3:
        raw_score = min(raw_score, 6.0)
    score = round(raw_score, 1)
    reason = f"PB{percentile:.0%}/{current_pb:.2f};冷{market_score:.0f};{financial_basis}{financial_score:.0f}"
    return score, reason


def _audit_type5_bottom_evidence_errors(
    code: str,
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[str]:
    reasons = payload.get("reasons")
    sub_scores = payload.get("sub_scores")
    reason = reasons.get("5b") if isinstance(reasons, Mapping) else None
    mode = payload.get("bottom_evidence_mode")
    contract = payload.get("bottom_evidence_contract")
    if mode not in {"automatic_replay", "trusted_external", "incomplete", "not_applicable"}:
        return [f"{code}:type5:bottom evidence mode invalid"]
    status = payload.get("status")
    if (status == "not_applicable") != (mode == "not_applicable"):
        return [f"{code}:type5:bottom evidence mode differs from status"]
    if mode != "automatic_replay":
        return [] if contract is None else [f"{code}:type5:non-automatic path carries a bottom evidence contract"]
    if contract is None:
        return [f"{code}:type5:automatic bottom evidence contract missing"]
    replay = _audit_type5_bottom_contract_replay(
        contract,
        code=code,
        as_of=row.get("source_trade_date"),
        row_pb=row.get("pb"),
    )
    score = _finite(sub_scores.get("5b")) if isinstance(sub_scores, Mapping) else None
    if (
        replay is None
        or score is None
        or not math.isclose(score, replay[0], rel_tol=0.0, abs_tol=1e-9)
        or reason != replay[1]
    ):
        return [f"{code}:type5:automatic bottom evidence replay mismatch"]
    return []


def _audit_type7_linear(value: float | None, anchors: Sequence[tuple[float, float]], *, missing: float = 2.0) -> float:
    if value is None:
        return round(missing, 2)
    if value <= anchors[0][0]:
        return round(min(10.0, max(0.0, anchors[0][1])), 2)
    for (left_x, left_y), (right_x, right_y) in zip(anchors, anchors[1:]):
        if value <= right_x:
            fraction = (value - left_x) / (right_x - left_x)
            return round(min(10.0, max(0.0, left_y + fraction * (right_y - left_y))), 2)
    return round(min(10.0, max(0.0, anchors[-1][1])), 2)


def _audit_type7_average(values: Sequence[float]) -> float:
    return round(min(10.0, max(0.0, math.fsum(values) / len(values))), 2)


def _audit_type7_template_input_score(
    section_key: str,
    key: str,
    inputs: Mapping[str, Any],
) -> tuple[bool, float | None]:
    if section_key == "template5":
        if key not in _AUDIT_TYPE7_TEMPLATE5_LABELS or set(inputs) != {"normalized_score"}:
            return False, None
        return True, _finite(inputs.get("normalized_score"))
    contract = _AUDIT_TYPE7_TEMPLATE1_CONTRACTS.get(key)
    if contract is None or set(inputs) not in contract[2]:
        return False, None
    mean_inputs = {
        "t1_01": ("runway", "industry"),
        "t1_02": ("runway", "revenue_growth", "profit_fcf_growth", "growth_stability"),
        "t1_06": ("accounting", "balance", "roic"),
        "t1_09": ("moat", "durability"),
        "t1_11": ("margin", "accounting"),
        "t1_14": ("moat", "industry"),
        "t1_15": ("moat_durability", "profit_fcf_growth", "roic", "accounting"),
    }
    if key in mean_inputs:
        values = [_finite(inputs.get(field)) for field in mean_inputs[key]]
        return (False, None) if any(value is None for value in values) else (True, _audit_type7_average(values))
    if key in {"t1_05", "t1_07", "t1_17"}:
        return True, _finite(inputs.get("score"))
    if key == "t1_20":
        if inputs.get("validation_basis") != "source_bound_nonfinancial_dcf":
            return False, None
        return True, _finite(inputs.get("type1_1a"))
    if key == "t1_03":
        raw = inputs.get("rate")
        if raw is not None and _finite(raw) is None:
            return False, None
        return True, _audit_type7_linear(
            _finite(raw), [(-0.15, 0), (0.0, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.35, 10)]
        )
    if key == "t1_04":
        raw_values = [inputs.get("profit_cagr"), inputs.get("fcf_cagr")]
        if any(value is not None and _finite(value) is None for value in raw_values):
            return False, None
        scores = [
            _audit_type7_linear(_finite(value), [(-0.15, 0), (0.0, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.35, 10)])
            for value in raw_values
        ]
        return True, _audit_type7_average(scores)
    if key == "t1_18" and "annual_return" in inputs:
        return True, _audit_type7_linear(
            _finite(inputs.get("annual_return")),
            [(-0.05, 0), (0.0, 1), (0.05, 4), (0.08, 6), (0.12, 8), (0.15, 9), (0.20, 10)],
        )
    return True, None


def _audit_type7_cross_check_from_bodies(bodies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for body in bodies:
        evidence_id = str(body["evidence_id"])
        for fact in body["facts"]:
            grouped.setdefault((fact["fact_key"], fact["unit"]), []).append((evidence_id, float(fact["value"])))
    candidates: list[tuple[int, float, str, str, float, list[str]]] = []
    for (fact_key, unit), observations in grouped.items():
        ordered = sorted(observations, key=lambda item: (item[1], item[0]))
        for left in range(len(ordered)):
            for right in range(left + _AUDIT_TYPE7_MIN_CROSSCHECK_REPORTS, len(ordered) + 1):
                window = ordered[left:right]
                values = [value for _, value in window]
                center = math.fsum(values) / len(values)
                spread = (max(values) - min(values)) / max(abs(center), 1e-12)
                if spread <= _AUDIT_TYPE7_FACT_RELATIVE_TOLERANCE:
                    candidates.append(
                        (
                            -len(window),
                            spread,
                            fact_key,
                            unit,
                            center,
                            sorted(evidence_id for evidence_id, _ in window),
                        )
                    )
    if not candidates:
        return {
            "passed": False,
            "minimum_reports": _AUDIT_TYPE7_MIN_CROSSCHECK_REPORTS,
            "fact_key": None,
            "fact_unit": None,
            "consensus_value": None,
            "supporting_evidence_ids": [],
            "max_relative_spread": None,
        }
    _, spread, fact_key, unit, center, evidence_ids = min(candidates)
    return {
        "passed": True,
        "minimum_reports": _AUDIT_TYPE7_MIN_CROSSCHECK_REPORTS,
        "fact_key": fact_key,
        "fact_unit": unit,
        "consensus_value": round(center, 6),
        "supporting_evidence_ids": evidence_ids,
        "max_relative_spread": round(spread, 8),
    }


def _audit_type7_content_valid(
    value: Any,
    *,
    sources: Sequence[Mapping[str, str]],
    code: str,
    as_of: str,
) -> bool:
    """Independently validate the bounded report-body summary contract."""

    top_fields = {
        "model_id",
        "code",
        "as_of",
        "passed",
        "required_bodies",
        "attempted_bodies",
        "verified_bodies",
        "distinct_publishers",
        "bodies",
        "cross_check",
        "reason",
    }
    if not isinstance(value, Mapping) or set(value) != top_fields:
        return False
    if (
        value.get("model_id") != _AUDIT_TYPE7_CONTENT_MODEL_ID
        or value.get("code") != code
        or value.get("as_of") != as_of
        or not isinstance(value.get("passed"), bool)
        or value.get("required_bodies") != _AUDIT_TYPE7_MIN_BODY_SOURCES
    ):
        return False
    counts: dict[str, int] = {}
    for field in ("attempted_bodies", "verified_bodies", "distinct_publishers"):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return False
        counts[field] = raw
    if (
        counts["attempted_bodies"] > min(_AUDIT_TYPE7_MAX_BODY_FETCHES, len(sources))
        or counts["verified_bodies"] > counts["attempted_bodies"]
        or counts["distinct_publishers"] > counts["verified_bodies"]
    ):
        return False
    reason = value.get("reason")
    if not isinstance(reason, str) or len(reason) > 300 or any(ord(character) < 32 for character in reason):
        return False
    source_by_id = {source["evidence_id"]: source for source in sources}
    bodies = value.get("bodies")
    if not isinstance(bodies, list) or len(bodies) != counts["verified_bodies"]:
        return False
    body_fields = {
        "evidence_id",
        "content_sha256",
        "content_length",
        "paragraph_count",
        "structure_signals",
        "fact_count",
        "facts",
        "identity_checks",
    }
    normalized_bodies: list[dict[str, Any]] = []
    body_ids: set[str] = set()
    body_hashes: set[str] = set()
    publisher_ids: set[str] = set()
    for body in bodies:
        if not isinstance(body, Mapping) or set(body) != body_fields:
            return False
        evidence_id = body.get("evidence_id")
        source = source_by_id.get(evidence_id) if isinstance(evidence_id, str) else None
        digest = body.get("content_sha256")
        content_length = body.get("content_length")
        paragraph_count = body.get("paragraph_count")
        fact_count = body.get("fact_count")
        facts = body.get("facts")
        signals = body.get("structure_signals")
        checks = body.get("identity_checks")
        if (
            source is None
            or evidence_id in body_ids
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or digest in body_hashes
            or isinstance(content_length, bool)
            or not isinstance(content_length, int)
            or not _AUDIT_TYPE7_MIN_BODY_CHARACTERS <= content_length <= _AUDIT_TYPE7_MAX_BODY_CHARACTERS
            or isinstance(paragraph_count, bool)
            or not isinstance(paragraph_count, int)
            or paragraph_count < 2
            or isinstance(fact_count, bool)
            or not isinstance(fact_count, int)
            or not isinstance(facts, list)
            or fact_count != len(facts)
            or len(facts) > _AUDIT_TYPE7_MAX_FACTS_PER_BODY
            or not isinstance(signals, list)
            or signals != sorted(set(signals))
            or not signals
            or not set(signals).issubset(_AUDIT_TYPE7_CONTENT_SIGNALS)
            or not isinstance(checks, Mapping)
            or set(checks) != _AUDIT_TYPE7_CONTENT_IDENTITY_CHECKS
            or any(checks.get(key) is not True for key in _AUDIT_TYPE7_CONTENT_IDENTITY_CHECKS)
        ):
            return False
        normalized_facts: list[dict[str, Any]] = []
        fact_identities: set[tuple[str, str]] = set()
        for fact in facts:
            if not isinstance(fact, Mapping) or set(fact) != {"fact_key", "period", "metric", "unit", "value"}:
                return False
            fact_key = fact.get("fact_key")
            period = fact.get("period")
            metric = fact.get("metric")
            unit = fact.get("unit")
            raw_fact_value = fact.get("value")
            fact_value = _finite(raw_fact_value)
            identity = (str(fact_key), str(unit))
            if (
                not isinstance(fact_key, str)
                or not isinstance(period, str)
                or re.fullmatch(r"20[0-9]{2}Q[1-4]", period) is None
                or not isinstance(metric, str)
                or metric not in _AUDIT_TYPE7_FACT_METRICS
                or fact_key != f"{period}:{metric}"
                or not isinstance(unit, str)
                or unit != _AUDIT_TYPE7_FACT_UNIT_BY_METRIC[metric]
                or isinstance(raw_fact_value, bool)
                or not isinstance(raw_fact_value, (int, float))
                or fact_value is None
                or abs(fact_value) > _AUDIT_TYPE7_MAX_FACT_ABS_VALUE
                or fact_value != round(fact_value, 6)
                or identity in fact_identities
            ):
                return False
            fact_identities.add(identity)
            normalized_facts.append(
                {
                    "fact_key": fact_key,
                    "period": period,
                    "metric": metric,
                    "unit": unit,
                    "value": fact_value,
                }
            )
        normalized_facts.sort(key=lambda fact: (fact["fact_key"], fact["unit"]))
        if facts != normalized_facts:
            return False
        body_ids.add(evidence_id)
        body_hashes.add(digest)
        publisher_ids.add(source["publisher_id"].casefold())
        normalized_bodies.append(
            {
                "evidence_id": evidence_id,
                "content_sha256": digest,
                "content_length": content_length,
                "paragraph_count": paragraph_count,
                "structure_signals": list(signals),
                "fact_count": fact_count,
                "facts": normalized_facts,
                "identity_checks": dict(checks),
            }
        )
    normalized_bodies.sort(key=lambda item: item["evidence_id"])
    if bodies != normalized_bodies or counts["distinct_publishers"] != len(publisher_ids):
        return False
    cross_check = value.get("cross_check")
    cross_fields = {
        "passed",
        "minimum_reports",
        "fact_key",
        "fact_unit",
        "consensus_value",
        "supporting_evidence_ids",
        "max_relative_spread",
    }
    if (
        not isinstance(cross_check, Mapping)
        or set(cross_check) != cross_fields
        or not isinstance(cross_check.get("passed"), bool)
        or cross_check.get("minimum_reports") != _AUDIT_TYPE7_MIN_CROSSCHECK_REPORTS
    ):
        return False
    expected_cross_check = _audit_type7_cross_check_from_bodies(normalized_bodies)
    if dict(cross_check) != expected_cross_check:
        return False
    expected_passed = bool(
        counts["verified_bodies"] >= _AUDIT_TYPE7_MIN_BODY_SOURCES
        and counts["distinct_publishers"] >= _AUDIT_TYPE7_MIN_BODY_SOURCES
        and expected_cross_check["passed"]
    )
    return value["passed"] is expected_passed and bool(reason) is not expected_passed


def _audit_patch4_linear(value: float, anchors: Sequence[tuple[float, float]]) -> float:
    if value <= anchors[0][0]:
        return round(anchors[0][1], 2)
    if value >= anchors[-1][0]:
        return round(anchors[-1][1], 2)
    for (left_x, left_y), (right_x, right_y) in zip(anchors, anchors[1:]):
        if left_x <= value <= right_x:
            fraction = (value - left_x) / (right_x - left_x)
            return round(left_y + fraction * (right_y - left_y), 2)
    raise AssertionError("Patch 4 anchors do not cover value")


def _audit_patch4_evidence_valid(
    evidence: Any,
    *,
    code: str,
    as_of: date,
    allowed_bindings: Mapping[str, Mapping[str, str]] | None = None,
) -> bool:
    fields = {"source", "evidence_id", "url", "as_of", "summary"}
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != fields
        or any(not isinstance(evidence.get(field), str) for field in fields)
    ):
        return False
    clean = {field: str(evidence[field]).strip() for field in fields}
    if any(not text or len(text) > 1_000 or any(ord(character) < 32 for character in text) for text in clean.values()):
        return False
    match = _AUDIT_PATCH4_EVIDENCE_ID.fullmatch(clean["evidence_id"])
    try:
        evidence_date = date.fromisoformat(clean["as_of"])
    except (TypeError, ValueError):
        return False
    if evidence_date.isoformat() != clean["as_of"] or match is None:
        return False
    art_code = match.group("art_code")
    digest = match.group("digest")
    binding = allowed_bindings.get(clean["evidence_id"]) if isinstance(allowed_bindings, Mapping) else None
    return bool(
        isinstance(binding, Mapping)
        and set(binding) == {"evidence_id", "url", "as_of", "content_sha256"}
        and binding.get("evidence_id") == clean["evidence_id"]
        and binding.get("url") == clean["url"]
        and binding.get("as_of") == clean["as_of"]
        and isinstance(binding.get("content_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(binding["content_sha256"]))
        and str(binding["content_sha256"])[:16] == digest
        and clean["source"] == _AUDIT_PATCH4_EVIDENCE_SOURCE
        and match.group("code") == code
        and clean["url"] == f"{_AUDIT_PATCH4_DETAIL_PREFIX}{code}/{art_code}.html"
        and f"正文SHA-256前16位：{digest}" in clean["summary"]
        and evidence_date <= as_of
        and (as_of - evidence_date).days <= _AUDIT_PATCH4_MAX_EVIDENCE_AGE_DAYS
    )


def _audit_patch4_ledger_valid(
    assessment: Any,
    *,
    code: str,
    as_of: str,
    fairness_item: Mapping[str, Any],
    governance_component: Mapping[str, Any],
    allowed_bindings: Mapping[str, Mapping[str, str]] | None,
) -> bool:
    top_fields = {
        "schema_version",
        "model_id",
        "code",
        "as_of",
        "formula_version",
        "score",
        "complete",
        "components",
    }
    try:
        reference = date.fromisoformat(as_of)
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(assessment, Mapping)
        or set(assessment) != top_fields
        or assessment.get("schema_version") != _AUDIT_PATCH4_SCHEMA_VERSION
        or assessment.get("model_id") != _AUDIT_PATCH4_MODEL_ID
        or assessment.get("formula_version") != _AUDIT_PATCH4_FORMULA_VERSION
        or assessment.get("code") != code
        or assessment.get("as_of") != as_of
        or reference > date.today()
        or not isinstance(assessment.get("complete"), bool)
    ):
        return False
    components = assessment.get("components")
    if not isinstance(components, list) or len(components) != len(_AUDIT_PATCH4_COMPONENT_WEIGHTS):
        return False
    indexed: dict[str, Mapping[str, Any]] = {}
    for component in components:
        fields = {
            "key",
            "label",
            "weight",
            "score",
            "points",
            "complete",
            "formula",
            "inputs",
            "evidence",
        }
        key = component.get("key") if isinstance(component, Mapping) else None
        score = _finite(component.get("score")) if isinstance(component, Mapping) else None
        points = _finite(component.get("points")) if isinstance(component, Mapping) else None
        weight = _AUDIT_PATCH4_COMPONENT_WEIGHTS.get(key) if isinstance(key, str) else None
        if (
            not isinstance(component, Mapping)
            or set(component) != fields
            or weight is None
            or key in indexed
            or score is None
            or not 0 <= score <= 10
            or points is None
            or not isinstance(component.get("complete"), bool)
            or not isinstance(component.get("formula"), str)
            or not isinstance(component.get("inputs"), Mapping)
            or component.get("label") != _AUDIT_PATCH4_COMPONENT_LABELS[key]
            or not _close(component.get("weight"), weight, rel_tol=0.0)
            or not _close(points, round(score * weight / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
        ):
            return False
        indexed[key] = component
    if set(indexed) != set(_AUDIT_PATCH4_COMPONENT_WEIGHTS):
        return False

    fairness = indexed["p4_defensive_fairness"]
    governance = indexed["p4_defensive_governance"]
    fairness_score = _finite(fairness_item.get("score"))
    governance_score = _finite(governance_component.get("score"))
    if (
        fairness_score is None
        or governance_score is None
        or fairness.get("formula") != "source_score(template1.t1_08)"
        or fairness.get("inputs")
        != {
            "source_item": "template1.t1_08",
            "evidence_level": fairness_item.get("evidence_level"),
        }
        or fairness.get("evidence") is not None
        or not _close(fairness.get("score"), fairness_score, rel_tol=0.0, abs_tol=0.0001)
        or fairness.get("complete") is not fairness_item.get("complete")
    ):
        return False
    governance_inputs = governance.get("inputs")
    if (
        governance.get("formula") != "verified_governance_or_capped_management_proxy"
        or not isinstance(governance_inputs, Mapping)
        or set(governance_inputs) != {"source_item", "evidence_level"}
        or governance_inputs.get("source_item") != "patch5.p5_c4"
        or governance_inputs.get("evidence_level") not in {"primary", "derived_proxy", "missing"}
        or governance.get("evidence") is not None
        or not _close(governance.get("score"), governance_score, rel_tol=0.0, abs_tol=0.0001)
        or governance.get("complete") is not governance_component.get("complete")
    ):
        return False

    percentage_specs = {
        "p4_core_rd_ownership": (
            "piecewise(core_rd_ownership_pct;5%+=10)",
            [(0.0, 0.0), (1.0, 2.0), (3.0, 6.0), (5.0, 9.0), (5.000001, 10.0)],
        ),
        "p4_esop_coverage": (
            "piecewise(esop_core_talent_coverage_pct;30%+=10)",
            [(0.0, 0.0), (10.0, 3.0), (20.0, 6.0), (30.0, 9.0), (30.000001, 10.0)],
        ),
    }
    for key, (formula, anchors) in percentage_specs.items():
        component = indexed[key]
        inputs = component.get("inputs")
        value = _finite(inputs.get("value")) if isinstance(inputs, Mapping) else None
        if (
            not isinstance(inputs, Mapping)
            or set(inputs) != {"value", "unit"}
            or inputs.get("unit") != "percentage_points"
            or value is None
            or not 0 <= value <= 100
            or component.get("formula") != formula
            or component.get("complete") is not True
            or not _audit_patch4_evidence_valid(
                component.get("evidence"),
                code=code,
                as_of=reference,
                allowed_bindings=allowed_bindings,
            )
            or not _close(
                component.get("score"),
                _audit_patch4_linear(value, anchors),
                rel_tol=0.0,
                abs_tol=0.0001,
            )
        ):
            return False
    boolean_specs = {
        "p4_long_term_rd_link": ("10 if long_term_rd_metrics else 0", False),
        "p4_frontline_rd_equity": ("10 if frontline_rd_equity else 0", False),
        "p4_short_term_binding": ("0 if short_term_price_binding else 10", True),
    }
    for key, (formula, inverse) in boolean_specs.items():
        component = indexed[key]
        inputs = component.get("inputs")
        value = inputs.get("value") if isinstance(inputs, Mapping) else None
        expected_score = (0.0 if value else 10.0) if inverse else (10.0 if value else 0.0)
        if (
            not isinstance(inputs, Mapping)
            or set(inputs) != {"value"}
            or not isinstance(value, bool)
            or component.get("formula") != formula
            or component.get("complete") is not True
            or not _audit_patch4_evidence_valid(
                component.get("evidence"),
                code=code,
                as_of=reference,
                allowed_bindings=allowed_bindings,
            )
            or not _close(component.get("score"), expected_score, rel_tol=0.0, abs_tol=0.0001)
        ):
            return False
    expected_score = round(math.fsum(float(component["points"]) for component in indexed.values()) / 10.0, 2)
    expected_complete = all(component.get("complete") is True for component in indexed.values())
    return bool(
        _close(assessment.get("score"), expected_score, rel_tol=0.0, abs_tol=0.0001)
        and assessment.get("complete") is expected_complete
    )


def _audit_type7_ledger_impl(
    code: str,
    ledger: Any,
    status: Any,
    patch4_bindings: Mapping[str, Mapping[str, str]] | None,
) -> list[str]:
    """Replay the Type 7 source ledgers without calling its production module."""
    prefix = f"{code}:type7:"
    if not isinstance(ledger, Mapping):
        return [prefix + "ledger missing"]
    if status == "not_applicable":
        if (
            set(ledger) != {"schema_version", "model_id", "code", "applicable", "reason"}
            or ledger.get("schema_version") != _AUDIT_TYPE7_SCHEMA_VERSION
            or ledger.get("model_id") != _AUDIT_TYPE7_MODEL_ID
            or ledger.get("code") != code
            or ledger.get("applicable") is not False
            or not str(ledger.get("reason") or "").strip()
        ):
            return [prefix + "not-applicable ledger invalid"]
        return []

    errors: list[str] = []
    expected_ledger_fields = {
        "schema_version",
        "model_id",
        "code",
        "source_rule",
        "strict_threshold",
        "scores",
        "strict_checks",
        "all_scores_strictly_above_70",
        "prerequisites",
        "prerequisites_complete",
        "safety_veto",
        "triggered",
        "decisive_score_upper_bounds",
        "decisively_not_triggered",
        "research_request_needed",
        "history_request_needed",
        "upper_bounds_without_history",
        "template1",
        "template5",
        "patch5",
    }
    if set(ledger) != expected_ledger_fields:
        errors.append(prefix + "ledger structure invalid")
    if (
        ledger.get("schema_version") != _AUDIT_TYPE7_SCHEMA_VERSION
        or ledger.get("model_id") != _AUDIT_TYPE7_MODEL_ID
        or ledger.get("code") != code
        or ledger.get("source_rule") != "Template1>70 AND Template5>70 AND Patch5>70"
        or not _close(ledger.get("strict_threshold"), 70.0, rel_tol=0.0)
    ):
        errors.append(prefix + "model identity invalid")

    templates: dict[str, Mapping[str, Any]] = {}
    template_items: dict[str, dict[str, Mapping[str, Any]]] = {}
    for section_key, expected_weights in _AUDIT_TYPE7_TEMPLATE_WEIGHTS.items():
        section = ledger.get(section_key)
        items = section.get("items") if isinstance(section, Mapping) else None
        if (
            not isinstance(section, Mapping)
            or set(section) != {"score", "coverage", "items"}
            or not isinstance(items, list)
            or len(items) != len(expected_weights)
        ):
            errors.append(prefix + f"{section_key} structure invalid")
            continue
        indexed: dict[str, Mapping[str, Any]] = {}
        point_values: dict[str, float] = {}
        complete_values: dict[str, bool] = {}
        for item in items:
            required_item_fields = {
                "key",
                "label",
                "weight",
                "score",
                "points",
                "complete",
                "evidence_level",
                "formula",
                "inputs",
            }
            if not isinstance(item, Mapping) or set(item) != required_item_fields:
                errors.append(prefix + f"{section_key} item structure invalid")
                continue
            key = item.get("key") if isinstance(item, Mapping) else None
            if not isinstance(key, str) or key not in expected_weights or key in indexed:
                errors.append(prefix + f"{section_key} item identity invalid")
                continue
            score = _finite(item.get("score"))
            points = _finite(item.get("points"))
            complete = item.get("complete")
            inputs = item.get("inputs")
            weight = expected_weights[key]
            if section_key == "template1":
                contract = _AUDIT_TYPE7_TEMPLATE1_CONTRACTS.get(key)
                expected_label = contract[0] if contract is not None else None
                expected_formula = contract[1] if contract is not None else None
            else:
                expected_label = _AUDIT_TYPE7_TEMPLATE5_LABELS.get(key)
                expected_formula = "Template5_source_weight*observable_score"
            input_valid, replayed_score = (
                _audit_type7_template_input_score(section_key, key, inputs)
                if isinstance(inputs, Mapping)
                else (False, None)
            )
            if (
                score is None
                or not 0 <= score <= 10
                or points is None
                or not isinstance(complete, bool)
                or not _close(item.get("weight"), weight, rel_tol=0.0)
                or not _close(points, round(score * weight / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
                or item.get("label") != expected_label
                or item.get("formula") != expected_formula
                or item.get("evidence_level") not in _AUDIT_TYPE7_EVIDENCE_LEVELS
                or not input_valid
            ):
                errors.append(prefix + f"{section_key} item arithmetic invalid")
            if (
                replayed_score is not None
                and score is not None
                and not _close(score, replayed_score, rel_tol=0.0, abs_tol=0.0001)
            ):
                errors.append(prefix + f"{section_key} item input-score mismatch")
            if points is not None:
                point_values[key] = points
            if isinstance(complete, bool):
                complete_values[key] = complete
            indexed[key] = item
        if set(indexed) != set(expected_weights):
            errors.append(prefix + f"{section_key} item set invalid")
            continue
        if set(point_values) == set(expected_weights):
            expected_score = round(math.fsum(point_values.values()), 2)
            if not _close(section.get("score"), expected_score, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + f"{section_key} total mismatch")
        if set(complete_values) == set(expected_weights):
            expected_coverage = round(
                math.fsum(expected_weights[key] for key, complete in complete_values.items() if complete) / 100.0,
                4,
            )
            if not _close(section.get("coverage"), expected_coverage, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + f"{section_key} coverage mismatch")
        templates[section_key] = section
        template_items[section_key] = indexed

    patch5 = ledger.get("patch5")
    dimensions = patch5.get("dimensions") if isinstance(patch5, Mapping) else None
    patch_sections: dict[str, Mapping[str, Any]] = {}
    if (
        not isinstance(patch5, Mapping)
        or set(patch5) != {"score", "coverage", "safety_margin_score", "safety_margin_complete", "dimensions"}
        or not isinstance(dimensions, list)
        or len(dimensions) != 5
    ):
        errors.append(prefix + "patch5 structure invalid")
    else:
        section_point_values: dict[str, float] = {}
        section_complete_values: dict[str, bool] = {}
        for section in dimensions:
            key = section.get("key") if isinstance(section, Mapping) else None
            expected_weights = _AUDIT_TYPE7_PATCH_WEIGHTS.get(key) if isinstance(key, str) else None
            components = section.get("components") if isinstance(section, Mapping) else None
            if (
                expected_weights is None
                or key in patch_sections
                or not isinstance(section, Mapping)
                or set(section) != {"key", "label", "max_points", "components", "points", "complete"}
                or not isinstance(components, list)
                or len(components) != len(expected_weights)
                or not _close(section.get("max_points"), 20.0, rel_tol=0.0)
                or section.get("label") != _AUDIT_TYPE7_PATCH_SECTION_LABELS.get(key)
                or not isinstance(section.get("complete"), bool)
            ):
                errors.append(prefix + "patch5 dimension structure invalid")
                continue
            indexed: dict[str, Mapping[str, Any]] = {}
            component_point_values: dict[str, float] = {}
            component_complete_values: dict[str, bool] = {}
            for component in components:
                required_component_fields = {
                    "key",
                    "label",
                    "max_points",
                    "score",
                    "points",
                    "complete",
                    "formula",
                    "inputs",
                }
                if not isinstance(component, Mapping) or set(component) != required_component_fields:
                    errors.append(prefix + f"patch5 {key} component structure invalid")
                    continue
                component_key = component.get("key") if isinstance(component, Mapping) else None
                if (
                    not isinstance(component_key, str)
                    or component_key not in expected_weights
                    or component_key in indexed
                ):
                    errors.append(prefix + f"patch5 {key} component identity invalid")
                    continue
                score = _finite(component.get("score"))
                points = _finite(component.get("points"))
                complete = component.get("complete")
                inputs = component.get("inputs")
                maximum = expected_weights[component_key]
                expected_input_fields = (
                    {"source"} if component_key in _AUDIT_TYPE7_PATCH_SOURCE_INPUT_COMPONENTS else set()
                )
                inputs_valid = isinstance(inputs, Mapping) and set(inputs) == expected_input_fields
                if inputs_valid and expected_input_fields:
                    inputs_valid = inputs.get("source") in _AUDIT_TYPE7_PATCH_SOURCE_LEVELS
                if (
                    score is None
                    or not 0 <= score <= 10
                    or points is None
                    or not isinstance(complete, bool)
                    or not _close(component.get("max_points"), maximum, rel_tol=0.0)
                    or not _close(
                        points,
                        round(score * maximum / 10.0, 4),
                        rel_tol=0.0,
                        abs_tol=0.0001,
                    )
                    or component.get("label") != _AUDIT_TYPE7_PATCH_COMPONENT_LABELS.get(component_key)
                    or component.get("formula") != f"{maximum:g}*score/10"
                    or not inputs_valid
                ):
                    errors.append(prefix + f"patch5 {key} component arithmetic invalid")
                if points is not None:
                    component_point_values[component_key] = points
                if isinstance(complete, bool):
                    component_complete_values[component_key] = complete
                indexed[component_key] = component
            if set(indexed) != set(expected_weights):
                errors.append(prefix + f"patch5 {key} component set invalid")
                continue
            section_points = _finite(section.get("points"))
            if set(component_point_values) == set(expected_weights):
                expected_points = round(math.fsum(component_point_values.values()), 4)
                if section_points is None or not _close(section_points, expected_points, rel_tol=0.0, abs_tol=0.0001):
                    errors.append(prefix + f"patch5 {key} points mismatch")
            if set(component_complete_values) == set(expected_weights):
                expected_complete = all(component_complete_values.values())
                if section.get("complete") is not expected_complete:
                    errors.append(prefix + f"patch5 {key} completeness mismatch")
            if section_points is not None:
                section_point_values[key] = section_points
            if isinstance(section.get("complete"), bool):
                section_complete_values[key] = section["complete"]
            patch_sections[key] = section
        if set(patch_sections) != set(_AUDIT_TYPE7_PATCH_WEIGHTS):
            errors.append(prefix + "patch5 dimension set invalid")
        else:
            expected_score = (
                round(math.fsum(section_point_values.values()), 2) if len(section_point_values) == 5 else None
            )
            expected_coverage = (
                round(math.fsum(20.0 for complete in section_complete_values.values() if complete) / 100.0, 4)
                if len(section_complete_values) == 5
                else None
            )
            safety = patch_sections["p5_safety"]
            safety_points = _finite(safety.get("points"))
            if expected_score is None or not _close(patch5.get("score"), expected_score, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + "patch5 total mismatch")
            if expected_coverage is None or not _close(
                patch5.get("coverage"), expected_coverage, rel_tol=0.0, abs_tol=0.0001
            ):
                errors.append(prefix + "patch5 coverage mismatch")
            if (
                safety_points is None
                or not _close(
                    patch5.get("safety_margin_score"),
                    round(safety_points, 2),
                    rel_tol=0.0,
                    abs_tol=0.0001,
                )
                or patch5.get("safety_margin_complete") is not safety.get("complete")
            ):
                errors.append(prefix + "patch5 safety mismatch")

    scores = ledger.get("scores")
    score_values: dict[str, float] = {}
    expected_sections = {
        "template1": templates.get("template1"),
        "template5": templates.get("template5"),
        "patch5": patch5,
    }
    if not isinstance(scores, Mapping) or set(scores) != set(expected_sections):
        errors.append(prefix + "score map invalid")
    else:
        for key, section in expected_sections.items():
            value = _finite(scores.get(key))
            expected = _finite(section.get("score")) if isinstance(section, Mapping) else None
            if value is None or expected is None or not _close(value, expected, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + f"{key} score binding invalid")
            if value is not None:
                score_values[key] = value
    strict = {key: value > 70.0 for key, value in score_values.items()}
    strict_checks = ledger.get("strict_checks")
    intersection = len(strict) == 3 and all(strict.values())
    if (
        not isinstance(strict_checks, Mapping)
        or any(not isinstance(value, bool) for value in strict_checks.values())
        or dict(strict_checks) != strict
    ):
        errors.append(prefix + "strict checks mismatch")
    if ledger.get("all_scores_strictly_above_70") is not intersection:
        errors.append(prefix + "strict intersection mismatch")

    prerequisites = ledger.get("prerequisites")
    prerequisites_complete = False
    prerequisite_passes: dict[str, bool] = {}
    if not isinstance(prerequisites, Mapping) or set(prerequisites) != _AUDIT_TYPE7_PREREQUISITES:
        errors.append(prefix + "prerequisites invalid")
    else:
        passed: list[bool] = []
        for key, record in prerequisites.items():
            value = record.get("passed") if isinstance(record, Mapping) else None
            value = value if isinstance(value, bool) else None
            if value is None:
                errors.append(prefix + "prerequisite pass flag invalid")
            else:
                passed.append(value)
                prerequisite_passes[key] = value
        prerequisites_complete = len(passed) == len(_AUDIT_TYPE7_PREREQUISITES) and all(passed)
    if ledger.get("prerequisites_complete") is not prerequisites_complete:
        errors.append(prefix + "prerequisite intersection mismatch")

    quote_as_of: date | None = None
    template1_coverage: float | None = None
    incomplete_required_items: list[str] = []
    if isinstance(prerequisites, Mapping):
        core_prerequisite = prerequisites.get("core_modules_80pct")
        template1_coverage = (
            _finite(templates.get("template1", {}).get("coverage"))
            if isinstance(templates.get("template1"), Mapping)
            else None
        )
        core_actual = _finite(core_prerequisite.get("actual")) if isinstance(core_prerequisite, Mapping) else None
        incomplete_required_items = [
            f"{section_key}.{item_key}"
            for section_key in ("template1", "template5")
            for item_key in _AUDIT_TYPE7_TEMPLATE_WEIGHTS[section_key]
            if template_items.get(section_key, {}).get(item_key, {}).get("complete") is not True
        ]
        for section_key, component_weights in _AUDIT_TYPE7_PATCH_WEIGHTS.items():
            section = patch_sections.get(section_key, {})
            components = {
                component.get("key"): component
                for component in section.get("components", [])
                if isinstance(component, Mapping) and isinstance(component.get("key"), str)
            }
            incomplete_required_items.extend(
                f"patch5.{section_key}.{component_key}"
                for component_key in component_weights
                if components.get(component_key, {}).get("complete") is not True
            )
        required_items_complete = not incomplete_required_items
        expected_core_passed = bool(core_actual is not None and core_actual >= 0.80)
        if (
            not isinstance(core_prerequisite, Mapping)
            or set(core_prerequisite)
            != {
                "passed",
                "actual",
                "required",
                "required_items_complete",
                "incomplete_required_items",
            }
            or template1_coverage is None
            or core_actual is None
            or not _close(core_actual, template1_coverage, rel_tol=0.0, abs_tol=0.0001)
            or not _close(core_prerequisite.get("required"), 0.80, rel_tol=0.0)
            or core_prerequisite.get("required_items_complete") is not required_items_complete
            or core_prerequisite.get("incomplete_required_items") != incomplete_required_items
            or core_prerequisite.get("passed") is not expected_core_passed
        ):
            errors.append(prefix + "core coverage prerequisite mismatch")

        technology_prerequisite = prerequisites.get("technology_patch4")
        technology_item = template_items.get("template1", {}).get("t1_17", {})
        technology_score = _finite(technology_item.get("score")) if isinstance(technology_item, Mapping) else None
        technology_complete = technology_item.get("complete") if isinstance(technology_item, Mapping) else None
        technology_applicable = (
            technology_prerequisite.get("applicable") if isinstance(technology_prerequisite, Mapping) else None
        )
        applicability = (
            technology_prerequisite.get("applicability") if isinstance(technology_prerequisite, Mapping) else None
        )
        assessment = technology_prerequisite.get("assessment") if isinstance(technology_prerequisite, Mapping) else None
        rd_intensity = _finite(applicability.get("rd_intensity")) if isinstance(applicability, Mapping) else None
        published_technology_score = (
            _finite(applicability.get("technology_score")) if isinstance(applicability, Mapping) else None
        )
        published_technology_complete = (
            applicability.get("technology_score_complete") if isinstance(applicability, Mapping) else None
        )
        expected_technology_applicable = not bool(
            rd_intensity is not None
            and 0 <= rd_intensity < 0.05
            and technology_complete is True
            and technology_score is not None
            and technology_score < 7.0
        )
        patch4_score = _finite(assessment.get("score")) if isinstance(assessment, Mapping) else None
        patch4_complete = bool(isinstance(assessment, Mapping) and assessment.get("complete") is True)
        expected_technology_status = (
            "not_applicable"
            if not expected_technology_applicable
            else "validated_replayable_assessment"
            if patch4_complete
            else "incomplete_replayable_assessment"
            if assessment is not None
            else "missing_validated_patch4_assessment"
        )
        culture_components = {
            component.get("key"): component
            for component in patch_sections.get("p5_culture", {}).get("components", [])
            if isinstance(component, Mapping)
        }
        incentive_component = culture_components.get("p5_c2", {})
        governance_component = culture_components.get("p5_c4", {})
        raw_patch4_as_of = (
            prerequisites.get("latest_quote_and_valuation", {}).get("as_of")
            if isinstance(prerequisites.get("latest_quote_and_valuation"), Mapping)
            else None
        )
        patch4_valid = bool(
            assessment is None
            or _audit_patch4_ledger_valid(
                assessment,
                code=code,
                as_of=str(raw_patch4_as_of or ""),
                fairness_item=template_items.get("template1", {}).get("t1_08", {}),
                governance_component=governance_component,
                allowed_bindings=patch4_bindings,
            )
        )
        expected_incentive_score = (
            patch4_score
            if expected_technology_applicable and patch4_score is not None
            else 2.0
            if expected_technology_applicable
            else _finite(template_items.get("template1", {}).get("t1_08", {}).get("score"))
        )
        expected_incentive_complete = (
            patch4_complete
            if expected_technology_applicable
            else template_items.get("template1", {}).get("t1_08", {}).get("complete") is True
        )
        if (
            not isinstance(technology_prerequisite, Mapping)
            or set(technology_prerequisite)
            != {"passed", "applicable", "score", "validation_status", "applicability", "assessment"}
            or not isinstance(technology_applicable, bool)
            or not isinstance(applicability, Mapping)
            or set(applicability) != {"technology_score", "technology_score_complete", "rd_intensity", "rule"}
            or applicability.get("rule")
            != "Patch4 waived only if reported_rd_intensity<0.05 AND validated_technology_score<7"
            or technology_score is None
            or published_technology_score is None
            or not _close(published_technology_score, technology_score, rel_tol=0.0, abs_tol=0.0001)
            or published_technology_complete is not technology_complete
            or (rd_intensity is not None and not 0 <= rd_intensity <= 1)
            or technology_applicable is not expected_technology_applicable
            or technology_prerequisite.get("score") != (patch4_score if patch4_complete else None)
            or technology_prerequisite.get("validation_status") != expected_technology_status
            or technology_prerequisite.get("passed") is not (not expected_technology_applicable or patch4_complete)
            or not patch4_valid
            or expected_incentive_score is None
            or not _close(
                incentive_component.get("score"),
                expected_incentive_score,
                rel_tol=0.0,
                abs_tol=0.0001,
            )
            or incentive_component.get("complete") is not expected_incentive_complete
        ):
            errors.append(prefix + "technology prerequisite mismatch")

        financial_prerequisite = prerequisites.get("three_year_financials")
        consecutive_years = (
            financial_prerequisite.get("consecutive_years") if isinstance(financial_prerequisite, Mapping) else None
        )
        if (
            not isinstance(financial_prerequisite, Mapping)
            or set(financial_prerequisite) != {"passed", "consecutive_years"}
            or isinstance(consecutive_years, bool)
            or not isinstance(consecutive_years, int)
            or consecutive_years < 0
            or financial_prerequisite.get("passed") is not (consecutive_years >= 3)
        ):
            errors.append(prefix + "financial history prerequisite mismatch")

        valuation_prerequisite = prerequisites.get("latest_quote_and_valuation")
        t1_items = template_items.get("template1", {})
        expected_valuation_complete = t1_items.get("t1_20", {}).get("complete") is True
        if isinstance(valuation_prerequisite, Mapping):
            raw_as_of = valuation_prerequisite.get("as_of")
            try:
                quote_as_of = date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None
            except ValueError:
                quote_as_of = None
            expected_valuation_passed = bool(
                expected_valuation_complete and quote_as_of is not None and quote_as_of <= date.today()
            )
            if (
                set(valuation_prerequisite) != {"passed", "as_of", "valuation_complete", "validation_basis"}
                or valuation_prerequisite.get("validation_basis") != "source_bound_nonfinancial_dcf"
                or valuation_prerequisite.get("valuation_complete") is not expected_valuation_complete
                or valuation_prerequisite.get("passed") is not expected_valuation_passed
            ):
                errors.append(prefix + "valuation prerequisite mismatch")
        else:
            errors.append(prefix + "valuation prerequisite mismatch")

        report_prerequisite = prerequisites.get("three_external_reports")
        report_valid = isinstance(report_prerequisite, Mapping) and set(report_prerequisite) == {
            "passed",
            "check_type",
            "source_count",
            "distinct_publishers",
            "recent_source_count",
            "max_age_days",
            "recent_age_days",
            "sources",
        }
        sources = report_prerequisite.get("sources") if isinstance(report_prerequisite, Mapping) else None
        normalized_sources: list[dict[str, str]] = []
        identities: set[str] = set()
        urls: set[str] = set()
        if not isinstance(sources, list) or len(sources) > 20 or (sources and quote_as_of is None):
            report_valid = False
        else:
            for source in sources:
                fields = {
                    "security_code",
                    "company_name",
                    "title",
                    "publisher",
                    "publisher_id",
                    "url",
                    "as_of",
                    "evidence_id",
                }
                if (
                    not isinstance(source, Mapping)
                    or set(source) != fields
                    or any(not isinstance(source.get(field), str) for field in fields)
                ):
                    report_valid = False
                    break
                item = {field: str(source[field]).strip() for field in fields}
                try:
                    parsed = urlsplit(item["url"])
                    port = parsed.port
                    source_date = date.fromisoformat(item["as_of"])
                except (ValueError, TypeError):
                    report_valid = False
                    break
                identity = item["evidence_id"].casefold()
                canonical_url = item["url"].casefold()
                if (
                    any(
                        not text or len(text) > 300 or any(ord(character) < 32 for character in text)
                        for text in item.values()
                    )
                    or parsed.scheme.lower() != "https"
                    or not parsed.hostname
                    or port not in (None, 443)
                    or parsed.username is not None
                    or parsed.password is not None
                    or bool(parsed.fragment)
                    or item["security_code"] != code
                    or source_date > quote_as_of
                    or (quote_as_of - source_date).days > _AUDIT_TYPE7_RESEARCH_MAX_AGE_DAYS
                    or identity in identities
                    or canonical_url in urls
                ):
                    report_valid = False
                    break
                identities.add(identity)
                urls.add(canonical_url)
                normalized_sources.append(item)
        normalized_sources.sort(
            key=lambda item: (item["as_of"], item["publisher_id"], item["publisher"], item["evidence_id"])
        )
        publisher_count = len({item["publisher_id"].casefold() for item in normalized_sources})
        recent_source_count = (
            sum(
                (quote_as_of - date.fromisoformat(item["as_of"])).days <= _AUDIT_TYPE7_RESEARCH_RECENT_AGE_DAYS
                for item in normalized_sources
            )
            if quote_as_of is not None
            else 0
        )
        expected_report_pass = bool(
            report_valid and len(normalized_sources) >= 3 and publisher_count >= 3 and recent_source_count >= 1
        )
        if not (
            report_valid
            and normalized_sources == sources
            and report_prerequisite.get("check_type") == "metadata_availability_precheck"
            and report_prerequisite.get("source_count") == len(normalized_sources)
            and report_prerequisite.get("distinct_publishers") == publisher_count
            and report_prerequisite.get("recent_source_count") == recent_source_count
            and report_prerequisite.get("max_age_days") == _AUDIT_TYPE7_RESEARCH_MAX_AGE_DAYS
            and report_prerequisite.get("recent_age_days") == _AUDIT_TYPE7_RESEARCH_RECENT_AGE_DAYS
            and report_prerequisite.get("passed") is expected_report_pass
        ):
            errors.append(prefix + "external reports prerequisite mismatch")

        content_prerequisite = prerequisites.get("external_report_content_verification")
        content_as_of = quote_as_of.isoformat() if quote_as_of is not None else "0001-01-01"
        if not _audit_type7_content_valid(
            content_prerequisite,
            sources=normalized_sources,
            code=code,
            as_of=content_as_of,
        ):
            errors.append(prefix + "external report content prerequisite mismatch")

        history_prerequisite = prerequisites.get("ten_year_return_and_five_year_valuation")
        history_inputs = t1_items.get("t1_19", {}).get("inputs", {})
        shareholder_input = history_inputs.get("shareholder_return") if isinstance(history_inputs, Mapping) else None
        valuation_history_input = (
            shareholder_input.get("valuation_history_contract") if isinstance(shareholder_input, Mapping) else None
        )
        history_as_of = history_prerequisite.get("as_of") if isinstance(history_prerequisite, Mapping) else None
        history_date = _audit_type7_history_date(history_as_of)
        shareholder_history_valid = bool(
            history_date is not None and _audit_type7_shareholder_history_valid(shareholder_input, history_date)
        )
        valuation_history_replay = (
            _audit_type7_valuation_history_replay(valuation_history_input, history_date)
            if history_date is not None
            else None
        )
        t5_history_item = template_items.get("template5", {}).get("t5_v1", {})
        expected_history_pass = bool(
            shareholder_history_valid
            and valuation_history_replay is not None
            and t5_history_item.get("complete") is True
        )
        history_valid = isinstance(history_prerequisite, Mapping) and set(history_prerequisite) == {
            "passed",
            "as_of",
        }
        embedded_history_claimed = bool(
            isinstance(shareholder_input, Mapping)
            and (
                shareholder_input.get("available") is True
                or (isinstance(valuation_history_input, Mapping) and valuation_history_input.get("available") is True)
            )
        )
        if embedded_history_claimed and (not shareholder_history_valid or valuation_history_replay is None):
            errors.append(prefix + "raw market history replay mismatch")
        if valuation_history_replay is not None:
            combined_percentile = statistics.median(
                float(series["percentile"]) for series in valuation_history_replay.values()
            )
            expected_history_score = _audit_type7_linear(
                combined_percentile,
                [(0.0, 10), (0.10, 9), (0.30, 8), (0.50, 6.5), (0.70, 5), (0.90, 2), (1.0, 0)],
                missing=0,
            )
        else:
            expected_history_score = 0.0
        if (
            not isinstance(t5_history_item, Mapping)
            or t5_history_item.get("complete") is not (valuation_history_replay is not None)
            or not _close(
                t5_history_item.get("score"),
                expected_history_score,
                rel_tol=0.0,
                abs_tol=0.0001,
            )
        ):
            errors.append(prefix + "historical valuation item mismatch")
        if not (
            history_valid
            and history_prerequisite.get("passed") is expected_history_pass
            and (history_as_of is None or history_date is not None)
            and (
                not expected_history_pass
                or history_as_of
                == (valuation_prerequisite.get("as_of") if isinstance(valuation_prerequisite, Mapping) else None)
            )
        ):
            errors.append(prefix + "market history prerequisite mismatch")
    safety_complete = isinstance(patch5, Mapping) and patch5.get("safety_margin_complete") is True
    safety_score = _finite(patch5.get("safety_margin_score")) if isinstance(patch5, Mapping) else None
    safety_veto = bool(safety_complete and safety_score is not None and safety_score < 8.0)
    if ledger.get("safety_veto") is not safety_veto:
        errors.append(prefix + "safety veto mismatch")
    expected_trigger = intersection and prerequisites_complete and not safety_veto
    if ledger.get("triggered") is not expected_trigger:
        errors.append(prefix + "trigger mismatch")

    expected_decisive_upper: dict[str, float] = {}
    decisive_inputs_valid = all(
        _finite(item.get("points")) is not None and isinstance(item.get("complete"), bool)
        for section_items in template_items.values()
        for item in section_items.values()
    ) and all(
        _finite(component.get("points")) is not None and isinstance(component.get("complete"), bool)
        for section in patch_sections.values()
        for component in section.get("components", [])
        if isinstance(component, Mapping)
    )
    if (
        decisive_inputs_valid
        and set(template_items.get("template1", {})) == set(_AUDIT_TYPE7_TEMPLATE_WEIGHTS["template1"])
        and set(template_items.get("template5", {})) == set(_AUDIT_TYPE7_TEMPLATE_WEIGHTS["template5"])
        and set(patch_sections) == set(_AUDIT_TYPE7_PATCH_WEIGHTS)
    ):
        expected_decisive_upper = {
            "template1": round(
                min(
                    100.0,
                    math.fsum(
                        float(item["points"]) if item.get("complete") is True else expected_weight
                        for key, item in template_items["template1"].items()
                        for expected_weight in (_AUDIT_TYPE7_TEMPLATE_WEIGHTS["template1"][key],)
                    ),
                ),
                2,
            ),
            "template5": round(
                min(
                    100.0,
                    math.fsum(
                        float(item["points"]) if item.get("complete") is True else expected_weight
                        for key, item in template_items["template5"].items()
                        for expected_weight in (_AUDIT_TYPE7_TEMPLATE_WEIGHTS["template5"][key],)
                    ),
                ),
                2,
            ),
            "patch5": round(
                min(
                    100.0,
                    math.fsum(
                        float(component["points"])
                        if component.get("complete") is True
                        else _AUDIT_TYPE7_PATCH_WEIGHTS[section_key][str(component.get("key"))]
                        for section_key, section in patch_sections.items()
                        for component in section.get("components", [])
                        if isinstance(component, Mapping)
                    ),
                ),
                2,
            ),
        }
    published_decisive_upper = ledger.get("decisive_score_upper_bounds")
    if (
        len(expected_decisive_upper) != 3
        or not isinstance(published_decisive_upper, Mapping)
        or set(published_decisive_upper) != set(expected_decisive_upper)
        or any(
            not _close(published_decisive_upper.get(key), value, rel_tol=0.0, abs_tol=0.0001)
            for key, value in expected_decisive_upper.items()
        )
    ):
        errors.append(prefix + "decisive score upper bounds mismatch")
    expected_decisive_failure = bool(
        len(expected_decisive_upper) == 3 and any(value <= 70.0 for value in expected_decisive_upper.values())
    )
    if ledger.get("decisively_not_triggered") is not expected_decisive_failure:
        errors.append(prefix + "decisive failure decision mismatch")

    diagnostic_total = (
        round(sum(round(value / 10.0, 3) / 3.0 for value in score_values.values()), 1)
        if len(score_values) == 3
        else None
    )
    if status != "blocked" and diagnostic_total is not None:
        if expected_decisive_failure:
            expected_status = "not_triggered"
        elif safety_veto:
            expected_status = "vetoed"
        elif not prerequisites_complete:
            expected_status = "insufficient_evidence"
        elif expected_trigger:
            expected_status = "triggered"
        elif diagnostic_total >= 7.0 and not intersection:
            expected_status = "conditional"
        elif diagnostic_total >= 5.0:
            expected_status = "observe"
        else:
            expected_status = "not_triggered"
        if status != expected_status:
            errors.append(prefix + "status differs from independently replayed ledger")

    expected_upper: dict[str, float] = {}
    t1_items = template_items.get("template1", {})
    t5_items = template_items.get("template5", {})
    if (
        len(score_values) == 3
        and {"t1_18", "t1_19", "t1_20"}.issubset(t1_items)
        and {"t5_v1", "t5_v3"}.issubset(t5_items)
        and "p5_safety" in patch_sections
    ):
        safety_components = {
            component.get("key"): component
            for component in patch_sections["p5_safety"].get("components", [])
            if isinstance(component, Mapping)
        }
        valuation_component = safety_components.get("p5_s1")
        dcf_score = _finite(t1_items["t1_20"].get("score"))
        if isinstance(valuation_component, Mapping) and dcf_score is not None:
            expected_upper = {
                "template1": round(
                    min(
                        100.0,
                        round(score_values["template1"], 2)
                        - float(t1_items["t1_18"]["points"])
                        - float(t1_items["t1_19"]["points"])
                        + 10.0,
                    ),
                    2,
                ),
                "template5": round(
                    min(
                        100.0,
                        round(score_values["template5"], 2)
                        - float(t5_items["t5_v1"]["points"])
                        - float(t5_items["t5_v3"]["points"])
                        + 18.0,
                    ),
                    2,
                ),
                "patch5": round(
                    min(
                        100.0,
                        round(score_values["patch5"], 2)
                        - float(valuation_component["points"])
                        + 8.0 * round((dcf_score + 10.0) / 2.0, 2) / 10.0,
                    ),
                    2,
                ),
            }
    published_upper = ledger.get("upper_bounds_without_history")
    if (
        len(expected_upper) != 3
        or not isinstance(published_upper, Mapping)
        or set(published_upper) != set(expected_upper)
        or any(
            not _close(published_upper.get(key), value, rel_tol=0.0, abs_tol=0.0001)
            for key, value in expected_upper.items()
        )
    ):
        errors.append(prefix + "history upper bounds mismatch")
    history_request_core_ready = bool(template1_coverage is not None and template1_coverage >= 0.80)
    expected_request = bool(
        len(prerequisite_passes) == len(_AUDIT_TYPE7_PREREQUISITES)
        and not prerequisite_passes.get("ten_year_return_and_five_year_valuation", False)
        and history_request_core_ready
        and all(
            value
            for key, value in prerequisite_passes.items()
            if key
            not in {
                "core_modules_80pct",
                "three_external_reports",
                "external_report_content_verification",
                "ten_year_return_and_five_year_valuation",
            }
        )
        and not safety_veto
        and len(expected_upper) == 3
        and all(value > 70.0 for value in expected_upper.values())
        and not expected_decisive_failure
    )
    if ledger.get("history_request_needed") is not expected_request:
        errors.append(prefix + "history request decision mismatch")
    expected_research_request = bool(
        len(prerequisite_passes) == len(_AUDIT_TYPE7_PREREQUISITES)
        and (
            not prerequisite_passes.get("three_external_reports", False)
            or not prerequisite_passes.get("external_report_content_verification", False)
        )
        and all(
            value
            for key, value in prerequisite_passes.items()
            if key not in {"three_external_reports", "external_report_content_verification"}
        )
        and not safety_veto
        and (not expected_decisive_failure or (len(score_values) == 3 and min(score_values.values()) >= 60.0))
    )
    if ledger.get("research_request_needed") is not expected_research_request:
        errors.append(prefix + "research request decision mismatch")
    return errors


_INDEPENDENT_TYPE7_MODEL_ID = "patch6-type7-classified-equity-v2"
_INDEPENDENT_TYPE7_SOURCE_RULE = (
    "classify C/T/N; certify quality when arithmetic mean(BM,MOAT,G) strictly > 7; "
    "apply class-specific route and current-price gates for buy readiness"
)
_INDEPENDENT_TYPE7_CLASSIFICATION_RULE = "T>=7 -> T; else C>=7 -> C; else W (N is reported as weak-cycle evidence)"
_INDEPENDENT_TYPE7_TOP_FIELDS = {
    "schema_version",
    "model_id",
    "code",
    "as_of",
    "source_rule",
    "strict_threshold",
    "classification",
    "dimension_weights",
    "decision_gates",
    "dimensions",
    "scores",
    "unrounded_mean",
    "score",
    "upper_bound",
    "quality_complete",
    "quality_certified",
    "complete",
    "missing_items",
    "veto",
    "veto_dimensions",
    "condition_failures",
    "triggered",
    "buy_ready",
    "legacy_diagnostic",
    "history_request_needed",
    "research_request_needed",
}
_INDEPENDENT_TYPE7_CLASS_COMPONENTS = {
    "C": {
        "c_margin_volatility": 3.0,
        "c_profit_elasticity": 3.0,
        "c_capex_intensity": 2.0,
        "c_commodity_driver": 2.0,
    },
    "T": {
        "t_rd_intensity": 3.0,
        "t_intangible_patent": 2.0,
        "t_iteration": 2.0,
        "t_platform": 3.0,
    },
    "N": {
        "n_repeat": 3.0,
        "n_macro_beta": 2.0,
        "n_pricing": 3.0,
        "n_mindshare": 2.0,
    },
}
_INDEPENDENT_TYPE7_DIRECT_COMMODITY_INDUSTRIES = {
    "STEEL",
    "NONFERROUS",
    "CHEMICAL",
    "BUILDING_MATERIAL",
    "OIL_GAS",
    "COAL",
}
_INDEPENDENT_TYPE7_CYCLICAL_INDUSTRIES = _INDEPENDENT_TYPE7_DIRECT_COMMODITY_INDUSTRIES | {
    "CONST_MACHINERY",
    "AGRICULTURE",
    "TRANSPORT",
}
_INDEPENDENT_TYPE7_CORE_TECH_INDUSTRIES = {
    "SOFTWARE",
    "SEMICONDUCTOR",
    "BIO_PHARMA",
    "CHEM_PHARMA",
    "ELEC_COMPONENT",
    "TELECOM",
}
_INDEPENDENT_TYPE7_TECH_INDUSTRIES = _INDEPENDENT_TYPE7_CORE_TECH_INDUSTRIES | {
    "MEDICAL_SERVICE",
    "AUTO_VEHICLE",
    "AUTO_PARTS",
    "NEW_ENERGY_VEH",
    "INDUST_MACHINERY",
    "ELEC_EQUIP",
    "MEDIA",
}
_INDEPENDENT_TYPE7_ESSENTIAL_INDUSTRIES = {
    "ALCOHOL",
    "FOOD_BEV",
    "HOME_APPLIANCE",
    "TRAD_CN_MED",
    "CHEM_PHARMA",
    "BIO_PHARMA",
    "MEDICAL_SERVICE",
    "POWER_UTILITY",
    "TELECOM",
}
_INDEPENDENT_TYPE7_PROXY_COMPONENT_METADATA = {
    "c_commodity_driver": {
        "label": "商品价格/产能驱动",
        "formula": "industry proxy: direct commodity=1;broad cyclical=0.5;other=0 (source item max=2)",
        "source_rule": "industry proxy capped at half the source maximum; not primary product-price evidence",
        "input_keys": {"industry"},
    },
    "t_intangible_patent": {
        "label": "专利/无形资产密集",
        "formula": "industry proxy: core technology=1;technology=0.5;other=0 (source item max=2)",
        "source_rule": "industry proxy capped at half the source maximum; not patent evidence",
        "input_keys": {"industry"},
    },
    "t_iteration": {
        "label": "技术/产品迭代周期",
        "formula": "industry proxy: core technology=1;technology=0.5;other=0 (source item max=2)",
        "source_rule": "industry-cycle proxy capped at half the source maximum; not primary product-roadmap evidence",
        "input_keys": {"industry"},
    },
    "n_repeat": {
        "label": "复购/必选",
        "formula": "industry proxy: essential=2;repeat-consumption=1;other=0 (source item max=3)",
        "source_rule": "industry demand proxy below the source maximum; not primary purchase-frequency evidence",
        "input_keys": {"industry"},
    },
    "n_macro_beta": {
        "label": "低宏观敏感度",
        "formula": "stability proxy=1 if >=8;0.5 if >=5;else 0 (source item max=2)",
        "source_rule": "profit/growth stability proxy capped at half the source maximum; not measured GDP beta",
        "input_keys": {"stability_score"},
    },
}
_INDEPENDENT_TYPE7_ITEM_WEIGHTS = {
    "W": {
        "BM": {"pricing_power": 0.30, "fcf_conversion": 0.25, "repeat_demand": 0.25, "asset_light": 0.20},
        "MOAT": {"brand_mindshare": 0.30, "network_switching": 0.25, "license_scarcity": 0.20, "time_thickness": 0.25},
        "G": {
            "volume_price_space": 0.35,
            "category_expansion": 0.30,
            "inflation_pass_through": 0.20,
            "certainty": 0.15,
        },
    },
    "T": {
        "BM": {
            "rd_conversion": 0.30,
            "revenue_quality": 0.25,
            "declining_marginal_cost": 0.20,
            "cashflow_inflection": 0.25,
        },
        "MOAT": {"patent_standard": 0.25, "talent_retention": 0.20, "data_network": 0.25, "platform_lockin": 0.30},
        "G": {"s_curve_relay": 0.35, "tam_space": 0.30, "nonlinear_option": 0.20, "disruption_resilience": 0.15},
    },
    "C": {
        "BM": {
            "cost_curve": 0.35,
            "integration_self_supply": 0.25,
            "cash_conversion": 0.20,
            "capacity_discipline": 0.20,
        },
        "MOAT": {"resource_scarcity": 0.30, "cost_lead": 0.30, "scale_location": 0.20, "cycle_survival": 0.20},
        "G": {"low_cost_expansion": 0.35, "integration_gain": 0.30, "commodity_trend": 0.20, "certainty": 0.15},
    },
}
_INDEPENDENT_TYPE7_TYPE5_ROUTE_WEIGHTS = {
    "5a": 0.35,
    "5b": 0.25,
    "5c": 0.20,
    "5d": 0.10,
    "5e": 0.10,
}
_INDEPENDENT_TYPE7_C_ROUTE_INPUT_FIELDS = {
    "type5_applicable",
    "type5_cycle_complete",
    "type5_cycle_score",
    "type5_bottom_complete",
    "type5_bottom_score",
    "type5_survival_complete",
    "type5_survival_score",
    "type5_upside_complete",
    "type5_upside_score",
    "type5_valuation_complete",
    "type5_valuation_score",
    "type5_evidence_complete",
    "type5_triggered",
    "type5_total",
    "type5_replayed_total",
    "type5_replayed_triggered",
    "template25_complete",
    "template25_buy_zone_score",
    "monetary_funds",
    "interest_debt",
    "net_debt",
}
_INDEPENDENT_TYPE7_C_ROUTE_BASIS = "强周期：完整情况五与已扣净债的多情景估值路径"
_INDEPENDENT_TYPE7_C_ROUTE_RULE = "情况五证据完整且总分>=7，并明确用带息债务减货币资金核对净债"


def _independent_type7_proxy_component_value(key: str, inputs: Mapping[str, Any]) -> float | None:
    """Replay the deliberately low-capped classification proxies."""

    if key == "n_macro_beta":
        stability = _finite(inputs.get("stability_score"))
        return None if stability is None else 1.0 if stability >= 8.0 else 0.5 if stability >= 5.0 else 0.0

    industry = str(inputs.get("industry") or "")
    if not industry:
        return None
    if key == "c_commodity_driver":
        return (
            1.0
            if industry in _INDEPENDENT_TYPE7_DIRECT_COMMODITY_INDUSTRIES
            else 0.5
            if industry in _INDEPENDENT_TYPE7_CYCLICAL_INDUSTRIES
            else 0.0
        )
    if key in {"t_intangible_patent", "t_iteration"}:
        return (
            1.0
            if industry in _INDEPENDENT_TYPE7_CORE_TECH_INDUSTRIES
            else 0.5
            if industry in _INDEPENDENT_TYPE7_TECH_INDUSTRIES
            else 0.0
        )
    if key == "n_repeat":
        return (
            2.0
            if industry in _INDEPENDENT_TYPE7_ESSENTIAL_INDUSTRIES
            else 1.0
            if industry
            in {
                "RETAIL",
                "HOME_APPLIANCE",
            }
            else 0.0
        )
    raise KeyError(f"unsupported independent Type 7 proxy component: {key}")


def _independent_type7_decimal_scale(value: Any, digits: int) -> float | None:
    parsed = _finite(value)
    if parsed is None or not math.isclose(parsed, round(parsed, digits), abs_tol=1e-9):
        return None
    return parsed


def _independent_type7_pb_score(percentile: float | None) -> float | None:
    if percentile is None or not 0.0 <= percentile <= 1.0:
        return None
    return (
        10.0
        if percentile <= 0.10
        else 8.0
        if percentile <= 0.20
        else 6.0
        if percentile <= 0.30
        else 4.0
        if percentile <= 0.50
        else 2.0
    )


def _independent_type7_ledger_errors(ledger: Any, *, expected_code: str) -> list[str]:
    """Independently replay Type 7 arithmetic and decision gates.

    This oracle intentionally does not call the production Type 7 validator or
    any production scoring helper.  Production validation proves the input-to-
    atom formulas; this second implementation proves that the published atoms,
    class route and gates lead to exactly the published decision.
    """

    if not isinstance(ledger, Mapping):
        return ["independent ledger is not a mapping"]
    errors: list[str] = []
    if set(ledger) != _INDEPENDENT_TYPE7_TOP_FIELDS:
        errors.append("independent top-level structure mismatch")
    if (
        ledger.get("schema_version") != 2
        or ledger.get("model_id") != _INDEPENDENT_TYPE7_MODEL_ID
        or ledger.get("source_rule") != _INDEPENDENT_TYPE7_SOURCE_RULE
        or _finite(ledger.get("strict_threshold")) != 7.0
    ):
        errors.append("independent model identity mismatch")
    code = str(ledger.get("code") or "")
    raw_as_of = str(ledger.get("as_of") or "")
    try:
        parsed_as_of = date.fromisoformat(raw_as_of)
    except ValueError:
        parsed_as_of = None
    if (
        code != expected_code
        or not re.fullmatch(r"[036][0-9]{5}", code)
        or parsed_as_of is None
        or parsed_as_of > date.today()
    ):
        errors.append("independent company/date binding mismatch")

    classification = ledger.get("classification")
    class_fields = {
        "rule",
        "class_code",
        "class_label",
        "sensitivity_scores",
        "sensitivity_upper_bounds",
        "route_complete",
        "possible_classes",
        "missing_components",
        "secondary_features",
        "possible_secondary_features",
        "components",
        "basis",
    }
    class_labels = {"W": "弱周期", "T": "强科技", "C": "强周期"}
    replayed_sensitivity: dict[str, float] = {}
    replayed_sensitivity_upper: dict[str, float] = {}
    replayed_class_missing: list[str] = []
    if not isinstance(classification, Mapping) or set(classification) != class_fields:
        errors.append("independent classification structure mismatch")
        return errors
    components = classification.get("components")
    if not isinstance(components, Mapping) or set(components) != {"C", "T", "N"}:
        errors.append("independent classification components mismatch")
        return errors
    component_fields = {
        "key",
        "label",
        "max_points",
        "awarded_points",
        "diagnostic_points",
        "complete",
        "upper_bound",
        "evidence_level",
        "formula",
        "inputs",
        "evidence_refs",
        "missing_inputs",
        "source_rule",
    }
    for sensitivity_key, expected_components in _INDEPENDENT_TYPE7_CLASS_COMPONENTS.items():
        records = components.get(sensitivity_key)
        if not isinstance(records, list) or len(records) != 4:
            errors.append(f"independent {sensitivity_key} component count mismatch")
            continue
        indexed = {record.get("key"): record for record in records if isinstance(record, Mapping)}
        if len(indexed) != 4 or set(indexed) != set(expected_components):
            errors.append(f"independent {sensitivity_key} component keys mismatch")
            continue
        total = 0.0
        upper_total = 0.0
        for component_key, maximum in expected_components.items():
            record = indexed[component_key]
            awarded = _finite(record.get("awarded_points"))
            diagnostic = _finite(record.get("diagnostic_points"))
            upper = _finite(record.get("upper_bound"))
            complete = record.get("complete")
            missing_inputs = record.get("missing_inputs")
            if (
                set(record) != component_fields
                or _finite(record.get("max_points")) != maximum
                or awarded is None
                or diagnostic is None
                or upper is None
                or type(complete) is not bool
                or not isinstance(missing_inputs, list)
                or not 0.0 <= awarded <= upper <= maximum
                or not 0.0 <= diagnostic <= maximum
                or (
                    complete
                    and (
                        missing_inputs
                        or not math.isclose(awarded, diagnostic, abs_tol=1e-8)
                        or not math.isclose(upper, awarded, abs_tol=1e-8)
                    )
                )
                or (
                    not complete
                    and (not math.isclose(awarded, 0.0, abs_tol=1e-8) or not math.isclose(upper, maximum, abs_tol=1e-8))
                )
            ):
                errors.append(f"independent {sensitivity_key}.{component_key} arithmetic mismatch")
            proxy_metadata = _INDEPENDENT_TYPE7_PROXY_COMPONENT_METADATA.get(component_key)
            if proxy_metadata is not None:
                inputs = record.get("inputs")
                expected_proxy = (
                    _independent_type7_proxy_component_value(component_key, inputs)
                    if isinstance(inputs, Mapping) and set(inputs) == proxy_metadata["input_keys"]
                    else None
                )
                expected_proxy_missing = (
                    [name for name, value in inputs.items() if value is None] if isinstance(inputs, Mapping) else []
                )
                expected_proxy_complete = bool(expected_proxy is not None and not expected_proxy_missing)
                expected_proxy_awarded = float(expected_proxy) if expected_proxy_complete else 0.0
                expected_proxy_upper = expected_proxy_awarded if expected_proxy_complete else maximum
                expected_proxy_level = "derived_proxy" if expected_proxy_complete else "missing"
                if (
                    not isinstance(inputs, Mapping)
                    or set(inputs) != proxy_metadata["input_keys"]
                    or record.get("key") != component_key
                    or record.get("label") != proxy_metadata["label"]
                    or record.get("formula") != proxy_metadata["formula"]
                    or record.get("source_rule") != proxy_metadata["source_rule"]
                    or record.get("evidence_refs") != {}
                    or complete is not expected_proxy_complete
                    or record.get("evidence_level") != expected_proxy_level
                    or missing_inputs != expected_proxy_missing
                    or awarded is None
                    or diagnostic is None
                    or upper is None
                    or not math.isclose(awarded, expected_proxy_awarded, abs_tol=1e-8)
                    or not math.isclose(diagnostic, expected_proxy_awarded, abs_tol=1e-8)
                    or not math.isclose(upper, expected_proxy_upper, abs_tol=1e-8)
                ):
                    errors.append(f"independent {sensitivity_key}.{component_key} proxy source formula mismatch")
            total += awarded or 0.0
            upper_total += upper or 0.0
            if complete is not True:
                replayed_class_missing.append(f"{sensitivity_key}.{component_key}")
        replayed_sensitivity[sensitivity_key] = round(total, 6)
        replayed_sensitivity_upper[sensitivity_key] = round(upper_total, 6)
    sensitivity_scores = classification.get("sensitivity_scores")
    sensitivity_upper = classification.get("sensitivity_upper_bounds")
    if sensitivity_scores != replayed_sensitivity or sensitivity_upper != replayed_sensitivity_upper:
        errors.append("independent classification totals mismatch")
    expected_class = (
        "T" if replayed_sensitivity.get("T", 0.0) >= 7.0 else "C" if replayed_sensitivity.get("C", 0.0) >= 7.0 else "W"
    )
    if replayed_sensitivity.get("T", 0.0) >= 7.0:
        possible_classes = ["T"]
    elif replayed_sensitivity_upper.get("T", 10.0) >= 7.0:
        possible_classes = ["T"]
        if replayed_sensitivity_upper.get("C", 10.0) >= 7.0:
            possible_classes.append("C")
        if replayed_sensitivity.get("C", 0.0) < 7.0:
            possible_classes.append("W")
    elif replayed_sensitivity.get("C", 0.0) >= 7.0:
        possible_classes = ["C"]
    elif replayed_sensitivity_upper.get("C", 10.0) >= 7.0:
        possible_classes = ["C", "W"]
    else:
        possible_classes = ["W"]
    expected_secondary = [
        class_labels[key]
        for key, score_key in (("T", "T"), ("C", "C"), ("W", "N"))
        if key != expected_class and replayed_sensitivity.get(score_key, 0.0) >= 7.0
    ]
    expected_possible_secondary = [
        class_labels[key]
        for key, score_key in (("T", "T"), ("C", "C"), ("W", "N"))
        if key != expected_class
        and replayed_sensitivity.get(score_key, 0.0) < 7.0
        and replayed_sensitivity_upper.get(score_key, 0.0) >= 7.0
    ]
    expected_basis = (
        f"T={replayed_sensitivity.get('T', 0.0):.2f}, C={replayed_sensitivity.get('C', 0.0):.2f}, "
        f"N={replayed_sensitivity.get('N', 0.0):.2f}; routed to {class_labels[expected_class]}"
    )
    if (
        classification.get("rule") != _INDEPENDENT_TYPE7_CLASSIFICATION_RULE
        or classification.get("class_code") != expected_class
        or classification.get("class_label") != class_labels[expected_class]
        or classification.get("possible_classes") != possible_classes
        or classification.get("route_complete") is not (len(possible_classes) == 1)
        or classification.get("missing_components") != replayed_class_missing
        or classification.get("secondary_features") != expected_secondary
        or classification.get("possible_secondary_features") != expected_possible_secondary
        or classification.get("basis") != expected_basis
    ):
        errors.append("independent classification decision mismatch")

    dimensions = ledger.get("dimensions")
    published_scores = ledger.get("scores")
    if (
        not isinstance(dimensions, Mapping)
        or set(dimensions) != {"BM", "MOAT", "G"}
        or not isinstance(published_scores, Mapping)
        or set(published_scores) != {"BM", "MOAT", "G"}
    ):
        return errors + ["independent dimensions structure mismatch"]
    replayed_scores: dict[str, float] = {}
    replayed_upper: dict[str, float] = {}
    replayed_missing: list[str] = []
    item_fields = {
        "key",
        "label",
        "weight",
        "score",
        "points",
        "complete",
        "evidence_level",
        "proxy_cap",
        "formula",
        "inputs",
        "evidence_refs",
        "source_rule",
        "upper_bound",
        "missing_inputs",
    }
    for dimension in ("BM", "MOAT", "G"):
        section = dimensions[dimension]
        weights = _INDEPENDENT_TYPE7_ITEM_WEIGHTS[expected_class][dimension]
        if (
            not isinstance(section, Mapping)
            or set(section) != {"score", "upper_bound", "coverage", "complete", "items"}
            or not isinstance(section.get("items"), list)
        ):
            errors.append(f"independent {dimension} structure mismatch")
            continue
        indexed = {item.get("key"): item for item in section["items"] if isinstance(item, Mapping)}
        if len(section["items"]) != 4 or len(indexed) != 4 or set(indexed) != set(weights):
            errors.append(f"independent {dimension} item keys mismatch")
            continue
        total = 0.0
        upper_total = 0.0
        coverage = 0.0
        for item_key, weight in weights.items():
            item = indexed[item_key]
            score = _finite(item.get("score"))
            points = _finite(item.get("points"))
            upper = _finite(item.get("upper_bound"))
            complete = item.get("complete")
            if (
                set(item) != item_fields
                or _finite(item.get("weight")) != weight
                or score is None
                or points is None
                or upper is None
                or type(complete) is not bool
                or not 0.0 <= score <= upper <= 10.0
                or not math.isclose(points, score * weight, abs_tol=1e-8)
                or (complete and not math.isclose(upper, score, abs_tol=1e-8))
                or (not complete and not math.isclose(score, 0.0, abs_tol=1e-8))
            ):
                errors.append(f"independent {dimension}.{item_key} weighted arithmetic mismatch")
            total += (score or 0.0) * weight
            upper_total += (upper or 0.0) * weight
            if complete is True:
                coverage += weight
            else:
                replayed_missing.append(f"{dimension}.{item_key}")
        replayed_scores[dimension] = total
        replayed_upper[dimension] = upper_total
        if (
            _finite(section.get("score")) is None
            or not math.isclose(float(section["score"]), total, abs_tol=1e-8)
            or _finite(section.get("upper_bound")) is None
            or not math.isclose(float(section["upper_bound"]), upper_total, abs_tol=1e-8)
            or _finite(section.get("coverage")) is None
            or not math.isclose(float(section["coverage"]), coverage, abs_tol=1e-8)
            or section.get("complete") is not math.isclose(coverage, 1.0, abs_tol=1e-12)
            or _finite(published_scores.get(dimension)) is None
            or not math.isclose(float(published_scores[dimension]), total, abs_tol=1e-8)
        ):
            errors.append(f"independent {dimension} aggregate mismatch")
    if classification.get("route_complete") is not True:
        replayed_missing.extend(f"CLASSIFICATION.{key}" for key in replayed_class_missing)
    quality_missing = list(replayed_missing)

    gates = ledger.get("decision_gates")
    gate_missing: list[str] = []
    condition_failures: list[str] = []
    valuation_complete = False
    if not isinstance(gates, Mapping) or set(gates) != {
        "future_fcf",
        "gdN_investability",
        "route_path",
        "price_reasonableness",
    }:
        errors.append("independent decision gates structure mismatch")
    else:
        fcf = gates.get("future_fcf")
        if not isinstance(fcf, Mapping) or set(fcf) != {
            "class_code",
            "complete",
            "passed",
            "years",
            "values",
            "latest_financial_year",
            "positive_share",
            "latest_fcf",
            "recent_three_years",
            "recent_three_values",
            "improvement_periods",
            "improvement_amounts",
            "median_improvement",
            "latest_ocf_year",
            "latest_ocf",
            "estimated_years_to_positive",
            "durable_condition_passed",
            "turnaround_path_complete",
            "turnaround_path_passed",
            "matched_mode",
            "basis",
            "rule",
            "scope",
        }:
            errors.append("independent FCF gate structure mismatch")
        else:
            years = fcf.get("years")
            values = fcf.get("values")
            year_types_valid = bool(isinstance(years, list) and all(type(year) is int for year in years))
            valid_years = bool(
                year_types_valid and len(years) == len(set(years)) and years == sorted(years) and len(years) <= 5
            )
            parsed_values = [_finite(value) for value in values] if isinstance(values, list) else []
            valid_values = bool(
                isinstance(values, list)
                and len(values) == len(years or [])
                and all(value is not None for value in parsed_values)
            )
            consecutive = bool(
                valid_years
                and len(years) >= 3
                and all(current - previous == 1 for previous, current in zip(years, years[1:]))
            )
            latest_year = fcf.get("latest_financial_year")
            expected_complete = bool(
                valid_values and consecutive and type(latest_year) is int and years and years[-1] == latest_year
            )
            expected_share = (
                sum(float(value) > 0.0 for value in parsed_values) / len(parsed_values)
                if valid_values and parsed_values
                else None
            )
            expected_latest = float(parsed_values[-1]) if valid_values and parsed_values else None
            durable_passed = bool(
                expected_complete
                and expected_latest is not None
                and expected_latest > 0.0
                and expected_share is not None
                and expected_share >= 0.60
            )
            expected_recent_years = list(years[-3:]) if valid_years and len(years) >= 3 else []
            expected_recent_values = (
                [float(value) for value in parsed_values[-3:]] if valid_values and len(parsed_values) >= 3 else []
            )
            expected_improvements = [
                current - previous for previous, current in zip(expected_recent_values, expected_recent_values[1:])
            ]
            expected_periods = [
                f"{previous}年至{current}年"
                for previous, current in zip(expected_recent_years, expected_recent_years[1:])
            ]
            expected_median_improvement = (
                statistics.median(expected_improvements) if len(expected_improvements) == 2 else None
            )
            expected_estimated_years = (
                0.0
                if expected_latest is not None and expected_latest > 0.0
                else max(0.0, -expected_latest / expected_median_improvement)
                if expected_latest is not None
                and expected_median_improvement is not None
                and expected_median_improvement > 0.0
                else None
            )
            latest_ocf_year = fcf.get("latest_ocf_year")
            latest_ocf = _finite(fcf.get("latest_ocf"))
            ocf_pair_valid = bool(
                (latest_ocf_year is None and fcf.get("latest_ocf") is None)
                or (type(latest_ocf_year) is int and latest_ocf is not None)
            )
            ocf_current = bool(
                ocf_pair_valid
                and latest_ocf_year is not None
                and type(latest_year) is int
                and latest_ocf_year == latest_year
            )
            turnaround_complete = bool(expected_class == "T" and expected_complete and ocf_current)
            turnaround_passed = bool(
                turnaround_complete
                and len(expected_improvements) == 2
                and all(amount > 0.0 for amount in expected_improvements)
                and latest_ocf is not None
                and latest_ocf > 0.0
                and expected_estimated_years is not None
                and expected_estimated_years <= 2.0
            )
            if expected_class != "T":
                expected_gate_complete = expected_complete
                expected_passed = durable_passed
            elif durable_passed:
                expected_gate_complete = True
                expected_passed = True
            else:
                expected_gate_complete = turnaround_complete
                expected_passed = turnaround_passed

            if durable_passed:
                expected_mode = "耐久正自由现金流"
                expected_basis = "命中耐久正自由现金流：最新FCF为正，且当前连续3至5年FCF正值占比不低于60%。"
            elif turnaround_passed:
                expected_mode = "强科技清晰转正路径"
                expected_basis = (
                    "命中强科技清晰转正路径：最近3年FCF严格逐年改善，最新年度经营现金流为正，"
                    f"按最近两次改善额中位数线性外推约{expected_estimated_years:.2f}年转正。"
                )
            elif not expected_gate_complete:
                expected_mode = "证据不完整"
                expected_basis = (
                    f"证据不完整：耐久正FCF条件未通过，缺少{latest_year}年经营现金流。"
                    if expected_class == "T" and expected_complete and not ocf_current
                    else "证据不完整：缺少绑定最新完整财年的连续3至5年FCF。"
                )
            else:
                expected_mode = "未命中"
                expected_basis = (
                    "未命中：耐久正FCF条件和两年内清晰转正路径均未通过。"
                    if expected_class == "T"
                    else "未命中：最新FCF须为正，且当前连续3至5年FCF正值占比不低于60%。"
                )
            expected_rule = (
                "强科技：满足耐久正FCF，或最近3年FCF严格逐年改善、最新年度经营现金流为正，"
                "且按最近两次改善额中位数线性外推不超过2年转正"
                if expected_class == "T"
                else "最新FCF为正，且绑定最新完整财年的连续3至5年FCF正值占比不低于60%"
            )
            reported_recent_values = fcf.get("recent_three_values")
            parsed_recent_values = (
                [_finite(value) for value in reported_recent_values] if isinstance(reported_recent_values, list) else []
            )
            reported_improvements = fcf.get("improvement_amounts")
            parsed_improvements = (
                [_finite(value) for value in reported_improvements] if isinstance(reported_improvements, list) else []
            )
            reported_median = _finite(fcf.get("median_improvement"))
            reported_estimated = _finite(fcf.get("estimated_years_to_positive"))
            if (
                fcf.get("class_code") != expected_class
                or fcf.get("complete") is not expected_gate_complete
                or fcf.get("passed") is not expected_passed
                or fcf.get("durable_condition_passed") is not durable_passed
                or fcf.get("turnaround_path_complete") is not turnaround_complete
                or fcf.get("turnaround_path_passed") is not turnaround_passed
                or _finite(fcf.get("positive_share")) != expected_share
                or _finite(fcf.get("latest_fcf")) != expected_latest
                or fcf.get("recent_three_years") != expected_recent_years
                or len(parsed_recent_values) != len(expected_recent_values)
                or any(value is None for value in parsed_recent_values)
                or any(
                    actual is None or not math.isclose(actual, expected, abs_tol=1e-12)
                    for actual, expected in zip(parsed_recent_values, expected_recent_values)
                )
                or fcf.get("improvement_periods") != expected_periods
                or len(parsed_improvements) != len(expected_improvements)
                or any(value is None for value in parsed_improvements)
                or any(
                    actual is None or not math.isclose(actual, expected, abs_tol=1e-12)
                    for actual, expected in zip(parsed_improvements, expected_improvements)
                )
                or (expected_median_improvement is None) != (reported_median is None)
                or (
                    expected_median_improvement is not None
                    and reported_median is not None
                    and not math.isclose(reported_median, expected_median_improvement, abs_tol=1e-12)
                )
                or not ocf_pair_valid
                or (expected_estimated_years is None) != (reported_estimated is None)
                or (
                    expected_estimated_years is not None
                    and reported_estimated is not None
                    and not math.isclose(reported_estimated, expected_estimated_years, abs_tol=1e-12)
                )
                or fcf.get("matched_mode") != expected_mode
                or fcf.get("basis") != expected_basis
                or fcf.get("rule") != expected_rule
                or fcf.get("scope") != "补丁7未来自由现金流前置条件；历史事实与线性外推不构成预测保证"
            ):
                errors.append("independent FCF gate replay mismatch")
            if not expected_gate_complete:
                gate_missing.append("PRECONDITION.future_fcf")
            elif not expected_passed:
                condition_failures.append("future_fcf")

        gdN = gates.get("gdN_investability")
        classification_map = ledger.get("classification")
        gdn_class_code = (
            str(classification_map.get("class_code") or "") if isinstance(classification_map, Mapping) else ""
        )
        if (
            not isinstance(gdN, Mapping)
            or set(gdN)
            != {
                "complete",
                "passed",
                "required",
                "basis",
                "rule",
                "inputs",
                "missing_inputs",
            }
            or gdN.get("required") is not True
        ):
            errors.append("independent gdN filter gate structure mismatch")
        else:
            gdN_inputs = gdN.get("inputs")
            if not isinstance(gdN_inputs, Mapping):
                errors.append("independent gdN filter gate structure mismatch")
            else:
                g = _finite(gdN_inputs.get("g"))
                d = _finite(gdN_inputs.get("d"))
                rd = _finite(gdN_inputs.get("rd_intensity"))
                cycle_confirmed = gdN_inputs.get("cycle_confirmed") is True
                if g is None:
                    expected_complete_gdn = False
                    expected_passed_gdn = False
                else:
                    expected_complete_gdn = True
                    if g > 0 and d is not None and d > 0:
                        expected_passed_gdn = True
                    elif g > 0 and rd is not None and rd >= 0.03:
                        expected_passed_gdn = True
                    elif abs(g) <= 0.02 and d is not None and d >= 0.04:
                        expected_passed_gdn = True
                    elif gdn_class_code == "C" and cycle_confirmed:
                        expected_passed_gdn = True
                    else:
                        expected_passed_gdn = False
                if (
                    gdN.get("complete") is not expected_complete_gdn
                    or gdN.get("passed") is not expected_passed_gdn
                    or gdN.get("missing_inputs") != ([] if expected_complete_gdn else ["g"])
                ):
                    errors.append("independent gdN filter gate replay mismatch")
                if expected_complete_gdn is not True:
                    gate_missing.append("PRECONDITION.gdN_investability")
                elif expected_passed_gdn is not True:
                    condition_failures.append("gdN_investability")

        route = gates.get("route_path")
        price = gates.get("price_reasonableness")
        if (
            not isinstance(route, Mapping)
            or set(route) != {"class_code", "complete", "passed", "basis", "inputs", "rule"}
            or not isinstance(price, Mapping)
            or set(price)
            != {
                "class_code",
                "required",
                "source_evidence_complete",
                "complete",
                "passed",
                "buy_zone_score",
                "minimum_score",
                "basis",
                "inputs",
                "rule",
            }
        ):
            errors.append("independent route/price gate structure mismatch")
        else:
            route_inputs = route.get("inputs")
            route_complete = False
            route_passed = False
            if expected_class == "W" and isinstance(route_inputs, Mapping):
                valuation_items = route_inputs.get("template5_valuation_items")
                maxima = {"t5_v1": 9.0, "t5_v2": 12.0, "t5_v3": 9.0}
                valid_items = isinstance(valuation_items, Mapping) and set(valuation_items) == set(maxima)
                item_points: list[float] = []
                if valid_items:
                    for key, maximum in maxima.items():
                        item = valuation_items[key]
                        point = _finite(item.get("points")) if isinstance(item, Mapping) else None
                        if (
                            not isinstance(item, Mapping)
                            or set(item) != {"complete", "points"}
                            or item.get("complete") is not True
                            or point is None
                            or not 0.0 <= point <= maximum
                        ):
                            valid_items = False
                            break
                        item_points.append(point)
                expected_valuation_score = math.fsum(item_points) / 3.0 if valid_items else None
                route_complete = bool(
                    valid_items
                    and route_inputs.get("patch5_safety_complete") is True
                    and _finite(route_inputs.get("patch5_safety_score")) is not None
                )
                route_passed = bool(route_complete and float(route_inputs["patch5_safety_score"]) >= 8.0)
                if _finite(route_inputs.get("template5_valuation_score")) != expected_valuation_score:
                    errors.append("independent weak-cycle valuation aggregate mismatch")
            elif expected_class == "T" and isinstance(route_inputs, Mapping):
                route_complete = bool(
                    route_inputs.get("patch4_complete") is True
                    and _finite(route_inputs.get("patch4_score")) is not None
                    and _finite(route_inputs.get("patch5_coverage")) is not None
                    and route_inputs.get("patch5_safety_complete") is True
                    and _finite(route_inputs.get("patch5_safety_score")) is not None
                )
                route_passed = bool(
                    route_complete
                    and float(route_inputs["patch5_coverage"]) >= 0.80
                    and float(route_inputs["patch5_safety_score"]) >= 8.0
                )
            elif expected_class == "C" and isinstance(route_inputs, Mapping):
                input_structure_valid = set(route_inputs) == _INDEPENDENT_TYPE7_C_ROUTE_INPUT_FIELDS
                if not input_structure_valid:
                    errors.append("independent strong-cycle route input structure mismatch")
                score_fields = {
                    "5a": "type5_cycle_score",
                    "5b": "type5_bottom_score",
                    "5c": "type5_survival_score",
                    "5d": "type5_upside_score",
                    "5e": "type5_valuation_score",
                }
                complete_fields = {
                    "5a": "type5_cycle_complete",
                    "5b": "type5_bottom_complete",
                    "5c": "type5_survival_complete",
                    "5d": "type5_upside_complete",
                    "5e": "type5_valuation_complete",
                }
                type5_scores = {
                    key: _independent_type7_decimal_scale(route_inputs.get(field), 2)
                    for key, field in score_fields.items()
                }
                scores_valid = all(score is not None and 0.0 <= score <= 10.0 for score in type5_scores.values())
                replayed_total = (
                    round(
                        math.fsum(
                            float(type5_scores[key]) * weight
                            for key, weight in _INDEPENDENT_TYPE7_TYPE5_ROUTE_WEIGHTS.items()
                        ),
                        1,
                    )
                    if scores_valid
                    else None
                )
                all_dimensions_complete = all(route_inputs.get(field) is True for field in complete_fields.values())
                replayed_triggered = bool(
                    route_inputs.get("type5_applicable") is True
                    and route_inputs.get("type5_evidence_complete") is True
                    and all_dimensions_complete
                    and type5_scores["5a"] is not None
                    and type5_scores["5a"] >= 7.0
                    and replayed_total is not None
                    and replayed_total >= 7.0
                )
                published_total = _independent_type7_decimal_scale(route_inputs.get("type5_total"), 1)
                published_replayed_total = _independent_type7_decimal_scale(
                    route_inputs.get("type5_replayed_total"),
                    1,
                )
                monetary_funds = _finite(route_inputs.get("monetary_funds"))
                interest_debt = _finite(route_inputs.get("interest_debt"))
                net_debt = _finite(route_inputs.get("net_debt"))
                net_debt_consistent = bool(
                    monetary_funds is not None
                    and monetary_funds >= 0.0
                    and interest_debt is not None
                    and interest_debt >= 0.0
                    and net_debt is not None
                    and math.isclose(net_debt, interest_debt - monetary_funds, abs_tol=1e-6)
                )
                route_complete = bool(
                    input_structure_valid
                    and type(route_inputs.get("type5_applicable")) is bool
                    and all_dimensions_complete
                    and scores_valid
                    and route_inputs.get("type5_evidence_complete") is True
                    and type(route_inputs.get("type5_triggered")) is bool
                    and published_total is not None
                    and replayed_total is not None
                    and math.isclose(published_total, replayed_total, abs_tol=1e-9)
                    and route_inputs.get("type5_triggered") is replayed_triggered
                    and published_replayed_total is not None
                    and math.isclose(published_replayed_total, replayed_total, abs_tol=1e-9)
                    and type(route_inputs.get("type5_replayed_triggered")) is bool
                    and route_inputs.get("type5_replayed_triggered") is replayed_triggered
                    and route_inputs.get("template25_complete") is True
                    and _finite(route_inputs.get("template25_buy_zone_score")) is not None
                    and 0.0 <= float(route_inputs["template25_buy_zone_score"]) <= 10.0
                    and net_debt_consistent
                )
                route_passed = bool(route_complete and replayed_triggered)
                if (
                    route_inputs.get("type5_replayed_total") != replayed_total
                    or route_inputs.get("type5_replayed_triggered") is not replayed_triggered
                    or route.get("basis") != _INDEPENDENT_TYPE7_C_ROUTE_BASIS
                    or route.get("rule") != _INDEPENDENT_TYPE7_C_ROUTE_RULE
                ):
                    errors.append("independent strong-cycle Type 5 replay mismatch")
            if (
                route.get("class_code") != expected_class
                or route.get("complete") is not route_complete
                or route.get("passed") is not route_passed
            ):
                errors.append("independent classified route replay mismatch")
            if not route_complete:
                gate_missing.append("ROUTE.class_specific_path")
            elif not route_passed:
                condition_failures.append("route_path")

            price_inputs = price.get("inputs")
            required = price.get("required")
            source_complete = price.get("source_evidence_complete")
            expected_price_score = None
            expected_minimum = {"W": 3.0, "T": 8.0, "C": 8.0}[expected_class]
            if (
                expected_class == "W"
                and isinstance(price_inputs, Mapping)
                and set(price_inputs) == {"type1_buy_zone_score"}
            ):
                expected_price_score = _finite(price_inputs.get("type1_buy_zone_score"))
            elif (
                expected_class in {"T", "C"}
                and isinstance(price_inputs, Mapping)
                and set(price_inputs) == {"pb_percentile", "current_pb"}
            ):
                percentile = _finite(price_inputs.get("pb_percentile"))
                current_pb = _finite(price_inputs.get("current_pb"))
                expected_price_score = _independent_type7_pb_score(percentile)
                if (
                    expected_class == "C"
                    and expected_price_score is not None
                    and current_pb is not None
                    and current_pb > 0.0
                ):
                    absolute = (
                        10.0
                        if current_pb <= 1.0
                        else 8.0
                        if current_pb <= 1.2
                        else 6.0
                        if current_pb <= 1.5
                        else 4.0
                        if current_pb <= 2.0
                        else 2.0
                    )
                    expected_price_score = min(expected_price_score, absolute)
                elif expected_class == "C" and (current_pb is None or current_pb <= 0.0):
                    expected_price_score = None
            price_evidence_complete = bool(
                source_complete is True and expected_price_score is not None and 0.0 <= expected_price_score <= 10.0
            )
            valuation_complete = bool(required is False or price_evidence_complete)
            valuation_passed = bool(
                required is False or (price_evidence_complete and expected_price_score >= expected_minimum)
            )
            if (
                type(required) is not bool
                or type(source_complete) is not bool
                or price.get("class_code") != expected_class
                or _finite(price.get("minimum_score")) != expected_minimum
                or _finite(price.get("buy_zone_score")) != expected_price_score
                or price.get("complete") is not valuation_complete
                or price.get("passed") is not valuation_passed
            ):
                errors.append("independent price gate replay mismatch")
            if not valuation_complete:
                gate_missing.append("VALUATION.price_reasonableness")
            elif not valuation_passed:
                condition_failures.append("price_reasonableness")

    replayed_missing.extend(gate_missing)
    mean_score = math.fsum(replayed_scores.values()) / 3.0 if len(replayed_scores) == 3 else -1.0
    chosen_upper = math.fsum(replayed_upper.values()) / 3.0 if len(replayed_upper) == 3 else -1.0
    upper_bound = 10.0 if classification.get("route_complete") is not True else chosen_upper
    quality_complete = not quality_missing
    complete = not replayed_missing
    veto_dimensions = [
        dimension
        for dimension in ("BM", "MOAT")
        if expected_class == "C"
        and (
            (dimensions[dimension].get("complete") is True and replayed_scores.get(dimension, 10.0) < 5.0)
            or replayed_upper.get(dimension, 10.0) < 5.0
        )
    ]
    veto = bool(veto_dimensions)
    quality_certified = bool(quality_complete and mean_score > 7.0 and not veto)
    if (
        expected_class == "T"
        and quality_complete
        and any(replayed_scores.get(key, -1.0) < 7.0 for key in ("BM", "MOAT", "G"))
    ):
        condition_failures.append("technology_dimension_floor")
    trigger = bool(quality_certified and complete and not condition_failures)
    weights = ledger.get("dimension_weights")
    if (
        not isinstance(weights, Mapping)
        or set(weights) != {"BM", "MOAT", "G"}
        or any(
            _finite(weights.get(key)) is None or not math.isclose(float(weights[key]), 1 / 3, abs_tol=1e-15)
            for key in weights
        )
    ):
        errors.append("independent dimension weights mismatch")
    if (
        _finite(ledger.get("unrounded_mean")) is None
        or not math.isclose(float(ledger["unrounded_mean"]), mean_score, abs_tol=1e-8)
        or _finite(ledger.get("score")) is None
        or not math.isclose(float(ledger["score"]), round(mean_score, 3), abs_tol=1e-9)
        or _finite(ledger.get("upper_bound")) is None
        or not math.isclose(float(ledger["upper_bound"]), round(upper_bound, 3), abs_tol=1e-9)
    ):
        errors.append("independent mean/upper-bound mismatch")
    if (
        ledger.get("quality_complete") is not quality_complete
        or ledger.get("quality_certified") is not quality_certified
        or ledger.get("complete") is not complete
        or ledger.get("missing_items") != replayed_missing
        or ledger.get("veto") is not veto
        or ledger.get("veto_dimensions") != veto_dimensions
        or ledger.get("condition_failures") != condition_failures
        or ledger.get("triggered") is not trigger
        or ledger.get("buy_ready") is not trigger
    ):
        errors.append("independent Type 7 decision mismatch")
    expected_history_request = bool(expected_class in {"C", "T"} and not valuation_complete and upper_bound > 7.0)
    if (
        ledger.get("history_request_needed") is not expected_history_request
        or ledger.get("research_request_needed") is not False
    ):
        errors.append("independent evidence request mismatch")
    legacy = ledger.get("legacy_diagnostic")
    if (
        not isinstance(legacy, Mapping)
        or set(legacy)
        != {"model_id", "source_rule", "scores", "prerequisites_complete", "triggered", "decisive", "note"}
        or legacy.get("decisive") is not False
    ):
        errors.append("independent legacy diagnostic boundary mismatch")
    return errors


_TYPE7_SOURCE_REPLAY_SECTIONS = {
    "classification": "classification",
    "dimensions": "atomic dimensions",
    "decision_gates": "decision gates",
    "legacy_diagnostic": "legacy diagnostic",
}
_TYPE7_SOURCE_REPLAY_DERIVED_FIELDS = (
    "dimension_weights",
    "scores",
    "unrounded_mean",
    "score",
    "upper_bound",
    "quality_complete",
    "quality_certified",
    "complete",
    "missing_items",
    "veto",
    "veto_dimensions",
    "condition_failures",
    "triggered",
    "buy_ready",
    "history_request_needed",
    "research_request_needed",
)


def _independent_type7_metric_series(source: Mapping[str, Any], value_key: str, year_key: str) -> dict[int, float]:
    values = source.get(value_key)
    years = source.get(year_key)
    if not isinstance(values, (list, tuple)) or not isinstance(years, (list, tuple)) or len(values) != len(years):
        return {}
    result: dict[int, float] = {}
    for raw_year, raw_value in zip(years, values):
        value = _finite(raw_value)
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            continue
        if value is not None and 1900 <= year <= 2200:
            result[year] = value
    return result


def _type7_future_fcf_source_binding_errors(
    code: str,
    ledger: Mapping[str, Any],
    *,
    source_financial: Mapping[str, Any] | None = None,
    source_metric: Mapping[str, Any] | None = None,
) -> list[str]:
    """Bind FCF history and the latest OCF to raw annual source facts."""

    if source_financial is None and source_metric is None:
        return []
    gates = ledger.get("decision_gates")
    fcf_gate = gates.get("future_fcf") if isinstance(gates, Mapping) else None
    if not isinstance(fcf_gate, Mapping):
        return [f"{code}:type7:raw future-FCF source binding gate missing"]

    if source_metric is not None:
        if not isinstance(source_metric, Mapping):
            return [f"{code}:type7:raw Type 7 metric source is invalid"]
        fcf_by_year = _independent_type7_metric_series(source_metric, "fcf_history", "fcf_years")
        ocf_by_year = _independent_type7_metric_series(source_metric, "ocf_history", "ocf_years")
    else:
        if not isinstance(source_financial, Mapping):
            return [f"{code}:type7:raw financial source is invalid"]
        cashflow = source_financial.get("cashflow", [])
        operating_by_year = _annual_values(cashflow, ("NETCASH_OPERATE",))
        capex_by_year = _annual_values(cashflow, ("CONSTRUCT_LONG_ASSET",))
        fcf_by_year = {
            year: operating_by_year[year] - abs(capex_by_year[year])
            for year in set(operating_by_year) & set(capex_by_year)
        }
        ocf_by_year = operating_by_year

    expected_years = _audit_consecutive_suffix(sorted(fcf_by_year), maximum=5)
    expected_values = [fcf_by_year[year] for year in expected_years]
    published_values = fcf_gate.get("values")
    parsed_values = [_finite(value) for value in published_values] if isinstance(published_values, list) else []
    errors: list[str] = []
    if (
        fcf_gate.get("years") != expected_years
        or len(parsed_values) != len(expected_values)
        or any(value is None for value in parsed_values)
        or any(
            actual is None or not math.isclose(actual, expected, abs_tol=1e-8)
            for actual, expected in zip(parsed_values, expected_values)
        )
    ):
        errors.append(f"{code}:type7:future-FCF history differs from raw annual cash-flow evidence")

    latest_financial_year = fcf_gate.get("latest_financial_year")
    eligible_ocf_years = (
        [year for year in ocf_by_year if type(latest_financial_year) is int and year <= latest_financial_year]
        if ocf_by_year
        else []
    )
    expected_ocf_year = max(eligible_ocf_years) if eligible_ocf_years else None
    expected_ocf = ocf_by_year.get(expected_ocf_year) if expected_ocf_year is not None else None
    published_ocf = _finite(fcf_gate.get("latest_ocf"))
    if (
        fcf_gate.get("latest_ocf_year") != expected_ocf_year
        or (expected_ocf is None) != (published_ocf is None)
        or (
            expected_ocf is not None
            and published_ocf is not None
            and not math.isclose(published_ocf, expected_ocf, abs_tol=1e-8)
        )
    ):
        errors.append(f"{code}:type7:latest OCF differs from raw annual cash-flow evidence")
    return errors


def _type7_source_replay_binding_errors(
    code: str,
    ledger: Mapping[str, Any],
    status: Any,
    source_row: Mapping[str, Any] | None,
) -> list[str]:
    """Bind a published Type 7 ledger to a replay from captured raw inputs.

    This is intentionally a same-production source replay, not the independent
    formula oracle above.  Its job is to catch coordinated edits where all
    atoms, totals and decision flags remain internally self-consistent but no
    longer match the captured company financials and evidence.
    """

    if source_row is None:
        return []
    if not isinstance(source_row, Mapping) or _normalise_code(source_row.get("code")) != code:
        return [f"{code}:type7:raw-source replay row identity mismatch"]
    source_payload = source_row.get("type7")
    if not isinstance(source_payload, Mapping):
        return [f"{code}:type7:raw-source replay payload missing"]
    source_ledger = source_payload.get("ledger")
    if not isinstance(source_ledger, Mapping):
        return [f"{code}:type7:raw-source replay ledger missing"]

    errors: list[str] = []
    if source_payload.get("status") != status:
        errors.append(f"{code}:type7:status differs from raw-source replay")
    if (
        source_ledger.get("model_id") != PATCH6_TYPE7_MODEL_ID
        or source_ledger.get("code") != code
        or ledger.get("model_id") != source_ledger.get("model_id")
        or ledger.get("code") != source_ledger.get("code")
    ):
        errors.append(f"{code}:type7:raw-source replay ledger identity mismatch")
        return errors

    if status == "not_applicable" or source_payload.get("status") == "not_applicable":
        if _canonical_json(ledger) != _canonical_json(source_ledger):
            errors.append(f"{code}:type7:not-applicable ledger differs from raw-source replay")
        return errors

    identity_fields = ("schema_version", "model_id", "code", "as_of", "source_rule", "strict_threshold")
    if any(ledger.get(field) != source_ledger.get(field) for field in identity_fields):
        errors.append(f"{code}:type7:model/date identity differs from raw-source replay")
    for field, label in _TYPE7_SOURCE_REPLAY_SECTIONS.items():
        if _canonical_json(ledger.get(field)) != _canonical_json(source_ledger.get(field)):
            errors.append(f"{code}:type7:{label} differ from raw-source replay")
    if any(
        _canonical_json(ledger.get(field)) != _canonical_json(source_ledger.get(field))
        for field in _TYPE7_SOURCE_REPLAY_DERIVED_FIELDS
    ):
        errors.append(f"{code}:type7:derived decision differs from raw-source replay")
    return errors


def _audit_type7_ledger(
    code: str,
    ledger: Any,
    status: Any,
    *,
    patch4_bindings: Mapping[str, Mapping[str, str]] | None = None,
    source_row: Mapping[str, Any] | None = None,
    source_financial: Mapping[str, Any] | None = None,
    source_metric: Mapping[str, Any] | None = None,
) -> list[str]:
    """Fail closed when an exported Type 7 ledger contains hostile values."""

    try:
        if isinstance(ledger, Mapping) and ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
            source_errors = _type7_source_replay_binding_errors(code, ledger, status, source_row)
            if status == "not_applicable":
                if (
                    set(ledger) != {"schema_version", "model_id", "code", "applicable", "reason"}
                    or ledger.get("schema_version") != 2
                    or ledger.get("code") != code
                    or ledger.get("applicable") is not False
                    or not str(ledger.get("reason") or "").strip()
                ):
                    return [f"{code}:type7:not-applicable ledger invalid", *source_errors]
                return source_errors
            errors = [f"{code}:type7:{error}" for error in validate_patch6_type7_ledger(ledger, expected_code=code)]
            errors.extend(
                f"{code}:type7:{error}" for error in _independent_type7_ledger_errors(ledger, expected_code=code)
            )
            errors.extend(
                _type7_future_fcf_source_binding_errors(
                    code,
                    ledger,
                    source_financial=source_financial,
                    source_metric=source_metric,
                )
            )
            classification = ledger.get("classification")
            gates = ledger.get("decision_gates")
            route = gates.get("route_path") if isinstance(gates, Mapping) else None
            route_inputs = route.get("inputs") if isinstance(route, Mapping) else None
            if (
                isinstance(classification, Mapping)
                and classification.get("class_code") == "T"
                and isinstance(route_inputs, Mapping)
                and route_inputs.get("patch4_complete") is True
                and not patch4_bindings
            ):
                errors.append(f"{code}:type7:technology route lacks captured Patch 4 document binding")
            if status == "triggered" and ledger.get("triggered") is not True:
                errors.append(f"{code}:type7:status differs from classified Patch 6 ledger")
            if status != "triggered" and ledger.get("triggered") is True:
                errors.append(f"{code}:type7:status differs from classified Patch 6 ledger")
            errors.extend(source_errors)
            return errors
        return [f"{code}:type7:unsupported Type 7 ledger model; refresh required"]
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        return [f"{code}:type7:ledger contains malformed values"]


def _audit_compact_reason(value: Any) -> str:
    text = str(value or "数据不足").strip()
    return text[:_AUDIT_REASON_MAX_LENGTH]


def _audit_bear_case(type_key: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    weights = _AUDIT_WEIGHTS[type_key]
    raw_scores = payload.get("sub_scores")
    raw_reasons = payload.get("reasons")
    scores = raw_scores if isinstance(raw_scores, Mapping) else {}
    reasons = raw_reasons if isinstance(raw_reasons, Mapping) else {}
    clean_scores = {key: round(float(scores[key]), 2) for key in weights}
    minimum = min(clean_scores.values())
    result: list[dict[str, Any]] = []
    for meta_key in ("_veto", "_condition", "_downgrade"):
        if reasons.get(meta_key):
            result.append(
                {
                    "dimension": meta_key,
                    "score": minimum,
                    "reason": _audit_compact_reason(reasons[meta_key]),
                }
            )
            if len(result) == 3:
                return result
    order = {key: index for index, key in enumerate(weights)}
    ranked = sorted(weights, key=lambda key: (clean_scores[key], -weights[key], order[key]))
    for key in ranked:
        result.append(
            {
                "dimension": key,
                "score": clean_scores[key],
                "reason": _audit_compact_reason(reasons.get(key)),
            }
        )
        if len(result) == 3:
            break
    return result


def _audit_dcf_value(
    *,
    base_fcf: float,
    base_revenue: float,
    growth: float,
    wacc: float,
    terminal_growth: float,
    shares: float,
    net_debt: float,
    retention: float,
) -> float | None:
    """Independent scalar implementation of the two-stage FCFF formula."""
    values = (base_fcf, base_revenue, growth, wacc, terminal_growth, shares, net_debt, retention)
    if not all(math.isfinite(value) for value in values):
        return None
    if (
        base_fcf <= 0
        or base_revenue <= 0
        or shares <= 0
        or growth <= -1
        or wacc <= terminal_growth
        or terminal_growth <= -1
        or not 0 <= retention <= 1
    ):
        return None
    current_margin = base_fcf / base_revenue
    if not 0 < current_margin <= 1:
        return None
    target_margin = min(
        current_margin,
        max(_AUDIT_FCF_MARGIN_FLOOR, _AUDIT_FCF_MARGIN_LONG_TERM, current_margin * retention),
    )
    explicit_pv = 0.0
    final_revenue = 0.0
    final_discount = 0.0
    denominator = max(_AUDIT_FORECAST_YEARS - 1, 1)
    for year in range(1, _AUDIT_FORECAST_YEARS + 1):
        revenue = base_revenue * (1.0 + growth) ** year
        margin = current_margin + (target_margin - current_margin) * ((year - 1.0) / denominator)
        fcff = revenue * margin
        discount = (1.0 + wacc) ** year
        if not all(math.isfinite(value) for value in (revenue, margin, fcff, discount)) or discount <= 0:
            return None
        explicit_pv += fcff / discount
        final_revenue, final_discount = revenue, discount
    terminal_fcf = final_revenue * (1.0 + terminal_growth) * target_margin
    terminal_value = terminal_fcf / (wacc - terminal_growth)
    per_share = (explicit_pv + terminal_value / final_discount - net_debt) / shares
    return per_share if math.isfinite(per_share) and per_share > 0 else None


def _annual_values(records: Any, keys: tuple[str, ...]) -> dict[int, float]:
    rows = [records] if isinstance(records, Mapping) else records
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")
        if len(report_date) < 4 or not report_date[:4].isdigit():
            continue
        value = next((_finite(row.get(key)) for key in keys if _finite(row.get(key)) is not None), None)
        if value is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, value)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _equity_from_row(row: Mapping[str, Any]) -> float | None:
    parent = None
    for key in _AUDIT_PARENT_EQUITY_KEYS:
        value = _finite(row.get(key))
        if value is not None and value > 0:
            parent = value
            break
    total = _finite(row.get("TOTAL_EQUITY"))
    minority = next(
        (_finite(row.get(key)) for key in _AUDIT_MINORITY_EQUITY_KEYS if _finite(row.get(key)) is not None),
        None,
    )
    assets = _finite(row.get("TOTAL_ASSETS"))
    if parent is not None:
        if assets is not None and assets > 0 and parent > assets * 1.03 and (total is None or minority is None):
            return None
        if total is not None:
            if minority is None:
                if total <= 0 < parent or (total > 0 and parent > total * 1.03):
                    return None
            else:
                scale = max(abs(total), abs(parent) + abs(minority), 1.0)
                if abs(total - parent - minority) > scale * 0.03:
                    return None
        return parent
    if total is not None and minority is not None:
        attributable = total - minority
        if attributable > 0 and not (assets is not None and assets > 0 and total > assets * 1.03):
            return attributable
    return None


def _annual_equity(records: Any) -> dict[int, float]:
    rows = [records] if isinstance(records, Mapping) else records
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")
        equity = _equity_from_row(row)
        if len(report_date) < 4 or not report_date[:4].isdigit() or equity is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, equity)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _latest_equity(records: Any) -> float | None:
    rows = [records] if isinstance(records, Mapping) else records
    candidates: list[tuple[str, float]] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            equity = _equity_from_row(row)
            if equity is not None:
                candidates.append((str(row.get("REPORT_DATE") or ""), equity))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _audit_interim_rows(value: Any) -> list[Mapping[str, Any]]:
    rows = [value] if isinstance(value, Mapping) else value
    valid_rows: list[Mapping[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "").strip()
        try:
            datetime.strptime(report_date, "%Y-%m-%d")
        except ValueError:
            continue
        valid_rows.append(row)
    return sorted(
        valid_rows,
        key=lambda row: str(row.get("REPORT_DATE") or "")[:10],
    )


def _audit_same_period_yoy(
    rows: list[Mapping[str, Any]],
    keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    current, prior, pair_basis = _audit_same_period_pair(rows, keys)
    if pair_basis != "same_period_yoy" or current is None:
        return current, None, pair_basis
    if prior is None:
        return current, None, "invalid_same_period_comparator"
    if prior <= 0:
        basis = "same_period_turnaround" if current > 0 else "same_period_nonpositive_comparison"
        return current, None, basis
    yoy = current / prior - 1.0
    return current, yoy, "same_period_yoy" if math.isfinite(yoy) else "invalid_same_period_comparator"


def _audit_same_period_pair(
    rows: list[Mapping[str, Any]],
    keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    if not rows:
        return None, None, "missing_current_period"
    latest = rows[-1]
    report_date = str(latest.get("REPORT_DATE") or "")[:10]
    current = _first_finite(latest, keys)
    if len(report_date) != 10 or not report_date[:4].isdigit() or current is None:
        return current, None, "missing_current_period"
    prior_date = f"{int(report_date[:4]) - 1}{report_date[4:]}"
    prior_row = next(
        (row for row in rows if str(row.get("REPORT_DATE") or "")[:10] == prior_date),
        None,
    )
    if prior_row is None:
        return current, None, "missing_same_period_comparator"
    prior = _first_finite(prior_row, keys)
    if prior is None:
        return current, None, "invalid_same_period_comparator"
    return current, prior, "same_period_yoy"


def _audit_current_period_evidence(financial: Mapping[str, Any]) -> dict[str, Any]:
    income = _audit_interim_rows(financial.get("income_interim", []))
    cashflow = _audit_interim_rows(financial.get("cashflow_interim", []))
    revenue, revenue_yoy, revenue_basis = _audit_same_period_yoy(
        income,
        ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
    )
    profit, profit_yoy, profit_basis = _audit_same_period_yoy(income, ("PARENT_NETPROFIT",))
    ocf, ocf_yoy, ocf_basis = _audit_same_period_yoy(cashflow, ("NETCASH_OPERATE",))
    return {
        "report_date": str(income[-1].get("REPORT_DATE") or "")[:10] if income else None,
        "revenue": revenue,
        "revenue_yoy": revenue_yoy,
        "revenue_yoy_basis": revenue_basis,
        "profit": profit,
        "profit_yoy": profit_yoy,
        "profit_yoy_basis": profit_basis,
        "operating_cash_flow": ocf,
        "operating_cash_flow_yoy": ocf_yoy,
        "operating_cash_flow_yoy_basis": ocf_basis,
    }


def _audit_financial_source_errors(
    code: str,
    financial: Mapping[str, Any],
    expected_interim_report_date: str | None,
) -> list[str]:
    """Independently enforce per-company accounting and report-period identity."""
    errors: list[str] = []
    date_contracts = {
        "revenue_history": "12-31",
        "income_history": "12-31",
        "cashflow": "12-31",
        "balance": "12-31",
        "income_interim": None,
        "cashflow_interim": None,
    }
    for dataset, annual_period in date_contracts.items():
        values = financial.get(dataset, [])
        rows = [values] if isinstance(values, Mapping) else values
        for row in rows if isinstance(rows, (list, tuple)) else []:
            if not isinstance(row, Mapping):
                continue
            report_date = str(row.get("REPORT_DATE") or "").strip()
            try:
                datetime.strptime(report_date, "%Y-%m-%d")
            except ValueError:
                if any(key != "REPORT_DATE" and _finite(value) is not None for key, value in row.items()):
                    errors.append(f"{code}: {dataset} contains a critical value with an invalid report date")
                continue
            if annual_period is not None and not report_date.endswith(annual_period):
                errors.append(f"{code}: {dataset} contains a non-annual report period {report_date}")
    balances = financial.get("balance", [])
    balance_rows = [balances] if isinstance(balances, Mapping) else balances
    for row in balance_rows if isinstance(balance_rows, (list, tuple)) else []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        assets = _finite(row.get("TOTAL_ASSETS"))
        liabilities = _finite(row.get("TOTAL_LIABILITIES"))
        total = _finite(row.get("TOTAL_EQUITY"))
        parent = next(
            (_finite(row.get(key)) for key in _AUDIT_PARENT_EQUITY_KEYS if _finite(row.get(key)) is not None),
            None,
        )
        minority = next(
            (_finite(row.get(key)) for key in _AUDIT_MINORITY_EQUITY_KEYS if _finite(row.get(key)) is not None),
            None,
        )
        label = f"{code}:{report_date or 'unknown balance period'}"
        if assets is not None and assets <= 0:
            errors.append(f"{label}: total assets are non-positive")
        if liabilities is not None and liabilities < 0:
            errors.append(f"{label}: total liabilities are negative")
        if total is not None and total > 0 and assets is not None and total > assets * 1.03:
            errors.append(f"{label}: total equity exceeds total assets")
        if parent is not None and assets is not None and parent > assets * 1.03 and (total is None or minority is None):
            errors.append(f"{label}: attributable equity exceeds total assets")
        if parent is not None and total is not None:
            if minority is None:
                if total <= 0 < parent or (total > 0 and parent > total * 1.03):
                    errors.append(f"{label}: attributable equity exceeds total equity")
            else:
                scale = max(abs(total), abs(parent) + abs(minority), 1.0)
                if abs(total - parent - minority) > scale * 0.03:
                    errors.append(f"{label}: total=parent+minority equity identity fails")
        if assets is not None and liabilities is not None and total is not None:
            scale = max(abs(assets), abs(liabilities) + abs(total), 1.0)
            if abs(assets - liabilities - total) > scale * 0.03:
                errors.append(f"{label}: assets=liabilities+equity identity fails")

    income_rows = _audit_interim_rows(financial.get("income_interim", []))
    cashflow_rows = _audit_interim_rows(financial.get("cashflow_interim", []))
    if not income_rows or not cashflow_rows:
        errors.append(f"{code}: current comparable income/cash-flow evidence is missing")
        return errors
    income_date = str(income_rows[-1].get("REPORT_DATE") or "")[:10]
    cashflow_date = str(cashflow_rows[-1].get("REPORT_DATE") or "")[:10]
    if income_date != cashflow_date:
        errors.append(f"{code}: current income and cash-flow report periods differ")
        return errors
    try:
        parsed_current = datetime.strptime(income_date, "%Y-%m-%d")
    except ValueError:
        errors.append(f"{code}: current report date is invalid")
        return errors
    prior_date = f"{parsed_current.year - 1}{income_date[4:]}"
    for dataset, rows in (("income", income_rows), ("cash-flow", cashflow_rows)):
        dates = {str(row.get("REPORT_DATE") or "")[:10] for row in rows}
        if prior_date not in dates:
            errors.append(f"{code}: {dataset} lacks exact prior-year same-period evidence")
    if expected_interim_report_date is not None:
        if income_date != expected_interim_report_date:
            errors.append(
                f"{code}: current report period {income_date} differs from expected {expected_interim_report_date}"
            )
    else:
        annual_dates: list[str] = []
        for dataset in ("income_history", "revenue_history", "cashflow", "balance"):
            values = financial.get(dataset, [])
            rows = [values] if isinstance(values, Mapping) else values
            for row in rows if isinstance(rows, (list, tuple)) else []:
                if isinstance(row, Mapping):
                    value = str(row.get("REPORT_DATE") or "")[:10]
                    if value.endswith("-12-31"):
                        annual_dates.append(value)
        if annual_dates and income_date <= max(annual_dates):
            errors.append(f"{code}: current interim report is not newer than the latest annual report")
    return errors


def _audit_growth_cap(evidence: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    comparable = all(
        evidence.get(key) == "same_period_yoy"
        for key in ("revenue_yoy_basis", "profit_yoy_basis", "operating_cash_flow_yoy_basis")
    ) and all(evidence.get(key) is not None for key in ("revenue", "profit", "operating_cash_flow"))
    if not comparable:
        return (
            {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0},
            "missing_current_period_conservative_cap",
        )
    severe = any(
        (value := _finite(evidence.get(key))) is not None and value <= 0
        for key in ("revenue", "profit", "operating_cash_flow")
    ) or any(
        (value := _finite(evidence.get(key))) is not None and value <= -0.50
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    material = any(
        (value := _finite(evidence.get(key))) is not None and value <= -0.20
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    weakening = any(
        (value := _finite(evidence.get(key))) is not None and value < 0
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    if severe:
        return {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0}, "current_period_severe_deterioration"
    if material:
        return {"pessimistic": -0.10, "neutral": -0.05, "optimistic": 0.05}, "current_period_material_deterioration"
    if weakening:
        return {"pessimistic": -0.05, "neutral": 0.0, "optimistic": 0.10}, "current_period_weakening"
    return {key: 1.0 for key in _AUDIT_SCENARIOS}, "no_current_period_downward_cap"


_AUDIT_OPERATING_CASH_KEYS = (
    "NETCASH_OPERATE",
    "经营活动产生的现金流量净额",
)
_AUDIT_CAPEX_KEYS = (
    "CONSTRUCT_LONG_ASSET",
    "PAY_ACQ_CONST_FIASSETS",
    "购建固定资产无形资产和其他长期资产支付的现金",
)
_AUDIT_CAPEX_PROVENANCE_SCHEMA_VERSION = 1
_AUDIT_CAPEX_FIELD = "CONSTRUCT_LONG_ASSET"
_AUDIT_STANDARD_CASHFLOW_REPORT = "RPT_DMSK_FN_CASHFLOW"
_AUDIT_DETAILED_CASHFLOW_REPORT = "RPT_F10_FINANCE_GCASHFLOW"
_AUDIT_OFFICIAL_QUARTERLY_REPORT = "CNINFO_EXCHANGE_FILED_QUARTERLY_REPORT"
_AUDIT_OFFICIAL_ANNUAL_REPORT = "CNINFO_EXCHANGE_FILED_ANNUAL_REPORT"
_AUDIT_EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_AUDIT_SINA_FINANCIAL_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_AUDIT_SINA_CASHFLOW_REPORT = "SINA_COMPANY_FINANCE_2022_LLB"
_AUDIT_SINA_CAPEX_FIELD = "ACQUASSETCASH"
_AUDIT_NON_CAPEX_OUTFLOW_FIELDS = (
    "INVEST_PAY_CASH",
    "PLEDGE_LOAN_ADD",
    "OBTAIN_SUBSIDIARY_OTHER",
    "ADD_PLEDGE_TIMEDEPOSITS",
    "PAY_OTHER_INVEST",
    "INVEST_OUTFLOW_OTHER",
    "INVEST_OUTFLOW_BALANCE",
)
_AUDIT_DIRECT_DEBT_KEYS = (
    "INTEREST_BEARING_DEBT",
    "TOTAL_INTEREST_BEARING_DEBT",
    "有息负债",
)
_AUDIT_DEBT_COMPONENT_KEYS = (
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
_AUDIT_CASH_KEYS = (
    "MONETARYFUNDS",
    "CASH_AND_CASH_EQUIVALENTS",
    "CASH_EQUIVALENTS",
    "货币资金",
)


def _first_finite(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _finite(row.get(key))
        if value is not None:
            return value
    return None


def _audit_fcff_normalisation(
    records: Any,
) -> tuple[float | None, float | None, tuple[float, ...], str, list[float]]:
    rows = [records] if isinstance(records, Mapping) else records
    by_year: dict[int, tuple[bool, str, float]] = {}
    undated: list[float] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        operating = _first_finite(row, _AUDIT_OPERATING_CASH_KEYS)
        capex = _first_finite(row, _AUDIT_CAPEX_KEYS)
        if operating is None or capex is None:
            continue
        fcff = operating - abs(capex)
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        if len(report_date) >= 4 and report_date[:4].isdigit():
            year = int(report_date[:4])
            candidate = (report_date.endswith("12-31"), report_date, fcff)
            if year not in by_year or candidate[:2] > by_year[year][:2]:
                by_year[year] = candidate
        else:
            undated.append(fcff)
    annual = [by_year[year][2] for year in sorted(by_year)] if by_year else undated
    if not annual:
        return None, None, (), "missing_complete_annual_fcff_history", []
    recent = tuple(float(value) for value in annual[-3:])
    latest = recent[-1]
    median = statistics.median(recent)
    if latest <= 0:
        return latest, latest, recent, "latest_nonpositive_fail_closed", annual
    if len(recent) >= 3 and all(current <= previous for previous, current in zip(recent, recent[1:])):
        return latest, latest, recent, "latest_persistent_decline", annual
    premium_limit = latest * _AUDIT_NORMALISATION_PREMIUM_CAP
    normalised = min(median, premium_limit)
    basis = "recent_median" if median <= premium_limit else "latest_premium_cap"
    return float(normalised), latest, recent, basis, annual


def _audit_valid_reporting_period_contract(value: object) -> bool:
    """Validate the snapshot period contract without using production helpers."""
    if not isinstance(value, ReportingPeriodContract):
        return False
    raw_dates = (
        value.annual_report_date,
        value.current_interim_report_date,
        value.prior_interim_report_date,
    )
    if not all(isinstance(item, str) for item in raw_dates):
        return False
    try:
        annual, current, prior = (datetime.strptime(item, "%Y-%m-%d") for item in raw_dates)
    except ValueError:
        return False
    if tuple(item.strftime("%Y-%m-%d") for item in (annual, current, prior)) != raw_dates:
        return False
    return (
        (annual.month, annual.day) == (12, 31)
        and (current.month, current.day) in {(3, 31), (6, 30), (9, 30)}
        and (current.month, current.day) == (prior.month, prior.day)
        and prior.year == annual.year
        and current.year == annual.year + 1
    )


def _audit_ttm_shell(
    metric: str,
    contract: ReportingPeriodContract,
) -> dict[str, Any]:
    formula_version = _AUDIT_TTM_FCFF_FORMULA_VERSION if metric == "fcff" else _AUDIT_TTM_REVENUE_FORMULA_VERSION
    cash_flow_kind = "cfo_less_capex_proxy" if metric == "fcff" else "reported_revenue"
    period = {
        "basis": _AUDIT_TTM_PERIOD_BASIS,
        "annual_report_date": contract.annual_report_date,
        "current_interim_report_date": contract.current_interim_report_date,
        "prior_interim_report_date": contract.prior_interim_report_date,
    }
    return {
        "status": "invalid_period_contract",
        "value": None,
        "metric": metric,
        "formula_version": formula_version,
        "cash_flow_kind": cash_flow_kind,
        "period_basis": _AUDIT_TTM_PERIOD_BASIS,
        "period": period,
        "unit": _AUDIT_TTM_SOURCE_UNIT,
        "components": {
            "annual": {"report_date": contract.annual_report_date, "row_count": 0},
            "current_interim": {"report_date": contract.current_interim_report_date, "row_count": 0},
            "prior_interim": {"report_date": contract.prior_interim_report_date, "row_count": 0},
        },
    }


def _audit_canonical_report_date(row: Mapping[str, Any]) -> str | None:
    value = str(row.get("REPORT_DATE") or "").strip()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value if parsed.strftime("%Y-%m-%d") == value else None


def _audit_rows_at_report_date(records: Any, report_date: str) -> list[Mapping[str, Any]]:
    if not isinstance(records, (list, tuple)):
        return []
    return [row for row in records if isinstance(row, Mapping) and _audit_canonical_report_date(row) == report_date]


def _audit_strict_source_value(
    row: Mapping[str, Any],
    keys: tuple[str, ...],
) -> tuple[str, float | None, str | None]:
    present = [(key, row.get(key)) for key in keys if key in row and row.get(key) not in (None, "")]
    if not present:
        return "missing_component", None, None
    key, raw_value = present[0]
    value = _finite(raw_value)
    if value is None:
        return "nonfinite_component", None, key
    return "complete", value, key


def _audit_validate_capex_provenance(
    provenance: Any,
    *,
    expected_value: float,
    expected_report_date: str,
    expected_security_code: str | None = None,
) -> str:
    """Independently replay the schema-1 capex evidence contract."""
    if not isinstance(provenance, Mapping):
        return "missing_capex_provenance"
    if (
        provenance.get("schema_version") != _AUDIT_CAPEX_PROVENANCE_SCHEMA_VERSION
        or provenance.get("status") != "complete"
        or provenance.get("report_date") != expected_report_date
        or not _close(provenance.get("value"), expected_value, abs_tol=0.01)
    ):
        return "invalid_capex_provenance"

    label = provenance.get("evidence_label")
    source_report = provenance.get("source_report")
    components = provenance.get("components")
    if label == "fact_official_report_zero":
        expected_source_report = (
            _AUDIT_OFFICIAL_ANNUAL_REPORT
            if expected_report_date.endswith("-12-31")
            else _AUDIT_OFFICIAL_QUARTERLY_REPORT
        )
        code = provenance.get("security_code")
        if (
            not isinstance(code, str)
            or re.fullmatch(r"\d{6}", code) is None
            or source_report != expected_source_report
            or provenance.get("source_field") != _AUDIT_CAPEX_FIELD
            or provenance.get("formula") != "exchange_filed_statement_zero"
            or provenance.get("derivation_method") is not None
            or not _close(expected_value, 0.0, abs_tol=0.01)
            or not isinstance(components, Mapping)
            or not _close(components.get("reported_value"), 0.0, abs_tol=0.01)
        ):
            return "invalid_capex_provenance"
        try:
            committed = zero_capex_evidence().get((code, expected_report_date))
        except FinancialSourceEvidenceError:
            return "invalid_capex_provenance"
        if not isinstance(committed, Mapping):
            return "invalid_capex_provenance"
        for field in (
            "evidence_type",
            "source_document",
            "source_url",
            "source_sha256",
            "source_page",
            "source_statement",
        ):
            if provenance.get(field) != committed.get(field):
                return "invalid_capex_provenance"
        return "complete"

    if label == "fact_secondary_source_reported":
        code = provenance.get("security_code")
        source_hash = provenance.get("source_raw_sha256")
        query = provenance.get("source_query")
        metadata = provenance.get("source_metadata")
        if (
            expected_value <= 0
            or not isinstance(code, str)
            or re.fullmatch(r"[036]\d{5}", code) is None
            or (expected_security_code is not None and code != expected_security_code)
            or source_report != _AUDIT_SINA_CASHFLOW_REPORT
            or provenance.get("source_field") != _AUDIT_SINA_CAPEX_FIELD
            or provenance.get("canonical_field") != _AUDIT_CAPEX_FIELD
            or provenance.get("formula") != "source_reported"
            or provenance.get("derivation_method") is not None
            or provenance.get("source_url") != _AUDIT_SINA_FINANCIAL_URL
            or not isinstance(source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
            or len(set(source_hash)) < 8
            or not isinstance(query, Mapping)
            or not isinstance(metadata, Mapping)
            or not isinstance(components, Mapping)
        ):
            return "invalid_capex_provenance"
        prefix = "sh" if code.startswith("6") else "sz"
        request_num = query.get("num")
        if (
            query
            != {
                "paperCode": f"{prefix}{code}",
                "source": "llb",
                "type": "0",
                "page": "1",
                "num": request_num,
            }
            or not isinstance(request_num, str)
            or re.fullmatch(r"(?:[1-9]|1\d|20)", request_num) is None
            or metadata.get("report_type") != "合并期末"
            or metadata.get("currency") != "CNY"
            or not isinstance(metadata.get("publish_date"), str)
            or re.fullmatch(r"\d{8}", metadata["publish_date"]) is None
            or isinstance(metadata.get("update_time"), bool)
            or not isinstance(metadata.get("update_time"), int)
            or metadata["update_time"] <= 0
            or not _close(components.get("reported_value"), expected_value, abs_tol=0.01)
        ):
            return "invalid_capex_provenance"
        return "complete"

    if provenance.get("source_url") != _AUDIT_EASTMONEY_DATACENTER_URL:
        return "invalid_capex_provenance"
    if label == "fact_source_reported":
        if (
            source_report not in {_AUDIT_STANDARD_CASHFLOW_REPORT, _AUDIT_DETAILED_CASHFLOW_REPORT}
            or provenance.get("source_field") != _AUDIT_CAPEX_FIELD
            or provenance.get("formula") != "source_reported"
            or provenance.get("derivation_method") is not None
            or not isinstance(components, Mapping)
            or not _close(components.get("reported_value"), expected_value, abs_tol=0.01)
        ):
            return "invalid_capex_provenance"
        return "complete"

    if (
        label != "derived_calculation"
        or source_report != _AUDIT_DETAILED_CASHFLOW_REPORT
        or not _close(expected_value, 0.0, abs_tol=0.01)
        or not isinstance(components, Mapping)
    ):
        return "invalid_capex_provenance"
    non_capex = components.get("non_capex_outflows")
    if not isinstance(non_capex, Mapping) or set(non_capex) != set(_AUDIT_NON_CAPEX_OUTFLOW_FIELDS):
        return "invalid_capex_provenance"
    outflow_values = [_finite(non_capex.get(field)) for field in _AUDIT_NON_CAPEX_OUTFLOW_FIELDS]
    if any(value is None or value < 0 for value in outflow_values):
        return "invalid_capex_provenance"
    outflow_sum = sum(value for value in outflow_values if value is not None)

    method = provenance.get("derivation_method")
    if method == "detailed_outflow_residual_zero":
        total = _finite(components.get("total_invest_outflow"))
        declared_sum = _finite(components.get("non_capex_outflow_sum"))
        if (
            total is None
            or total < 0
            or declared_sum is None
            or not _close(declared_sum, outflow_sum, abs_tol=0.01)
            or not _close(total, outflow_sum, abs_tol=0.01)
        ):
            return "invalid_capex_provenance"
        return "complete"
    if method != "detailed_net_cash_identity_zero":
        return "invalid_capex_provenance"
    inflow = _finite(components.get("total_invest_inflow"))
    other = _finite(components.get("invest_netcash_other"))
    balance = _finite(components.get("invest_netcash_balance"))
    net = _finite(components.get("netcash_invest"))
    solved = _finite(components.get("solved_total_invest_outflow"))
    if None in {inflow, other, balance, net, solved}:
        return "invalid_capex_provenance"
    if not _close(inflow + other + balance - net, solved, abs_tol=0.01) or not _close(
        solved,
        0.0,
        abs_tol=0.01,
    ):
        return "invalid_capex_provenance"
    return "complete"


def _audit_reconstruct_ttm(
    metric: str,
    annual_records: Any,
    interim_records: Any,
    contract: ReportingPeriodContract,
    expected_security_code: str | None = None,
) -> dict[str, Any]:
    """Independently reconstruct the exact FY + YTD - prior-YTD payload."""
    result = _audit_ttm_shell(metric, contract)
    if not _audit_valid_reporting_period_contract(contract):
        return result
    targets = {
        "annual": (annual_records, contract.annual_report_date),
        "current_interim": (interim_records, contract.current_interim_report_date),
        "prior_interim": (interim_records, contract.prior_interim_report_date),
    }
    selected: dict[str, Mapping[str, Any]] = {}
    components = result["components"]
    for label, (records, report_date) in targets.items():
        matches = _audit_rows_at_report_date(records, report_date)
        components[label]["row_count"] = len(matches)
        if len(matches) > 1:
            result["status"] = "duplicate_period"
            return result
        if not matches:
            result["status"] = "missing_component"
            return result
        selected[label] = matches[0]

    if metric == "revenue":
        values: dict[str, float] = {}
        for label, row in selected.items():
            status, value, source_field = _audit_strict_source_value(
                row,
                ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
            )
            components[label].update({"revenue": value, "revenue_source_field": source_field})
            if status != "complete":
                result["status"] = status
                return result
            if value is None:
                result["status"] = "nonfinite_component"
                return result
            values[label] = value
        reconstructed = values["annual"] + values["current_interim"] - values["prior_interim"]
        if not math.isfinite(reconstructed):
            result["status"] = "nonfinite_component"
            return result
        components["reconstructed_revenue"] = reconstructed
        result.update({"status": "complete", "value": reconstructed})
        return result

    cfo_values: dict[str, float] = {}
    capex_values: dict[str, float] = {}
    for label, row in selected.items():
        cfo_status, cfo, cfo_field = _audit_strict_source_value(row, _AUDIT_OPERATING_CASH_KEYS)
        capex_status, raw_capex, capex_field = _audit_strict_source_value(row, _AUDIT_CAPEX_KEYS)
        components[label].update(
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
        provenance_status = _audit_validate_capex_provenance(
            row.get("CAPEX_PROVENANCE"),
            expected_value=raw_capex,
            expected_report_date=str(row.get("REPORT_DATE") or ""),
            expected_security_code=expected_security_code,
        )
        components[label]["capex_provenance_status"] = provenance_status
        if provenance_status != "complete":
            result["status"] = provenance_status
            return result
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
        result["status"] = "negative_reconstructed_capex"
        return result
    reconstructed_fcff = reconstructed_cfo - reconstructed_capex
    if not math.isfinite(reconstructed_fcff):
        result["status"] = "nonfinite_component"
        return result
    components["reconstructed_fcff"] = reconstructed_fcff
    result.update({"status": "complete", "value": reconstructed_fcff})
    return result


def _audit_ttm_fcff_normalisation(
    annual_cashflow: Any,
    ttm_fcff_evidence: Mapping[str, Any],
    contract: ReportingPeriodContract,
) -> tuple[float, float, tuple[float, float, float], str, list[dict[str, str]]] | None:
    """Independently normalise the exact FY-1, FY and strict-TTM sequence."""
    prior_annual_date = f"{int(contract.annual_report_date[:4]) - 1}-12-31"
    prior_rows = _audit_rows_at_report_date(annual_cashflow, prior_annual_date)
    if len(prior_rows) != 1:
        return None
    prior_cfo_status, prior_cfo, _ = _audit_strict_source_value(
        prior_rows[0],
        _AUDIT_OPERATING_CASH_KEYS,
    )
    prior_capex_status, prior_capex, _ = _audit_strict_source_value(
        prior_rows[0],
        _AUDIT_CAPEX_KEYS,
    )
    components = ttm_fcff_evidence.get("components")
    annual_component = components.get("annual") if isinstance(components, Mapping) else None
    if (
        prior_cfo_status != "complete"
        or prior_capex_status != "complete"
        or prior_cfo is None
        or prior_capex is None
        or not isinstance(annual_component, Mapping)
    ):
        return None
    annual_cfo = _finite(annual_component.get("operating_cash_flow"))
    annual_capex = _finite(annual_component.get("capex_absolute"))
    reconstructed_cfo = _finite(components.get("reconstructed_operating_cash_flow"))
    reconstructed_capex = _finite(components.get("reconstructed_capex"))
    if None in (annual_cfo, annual_capex, reconstructed_cfo, reconstructed_capex):
        return None
    recent = (
        prior_cfo - abs(prior_capex),
        annual_cfo - abs(annual_capex),
        reconstructed_cfo - reconstructed_capex,
    )
    latest = recent[-1]
    median = float(statistics.median(recent))
    if latest <= 0:
        normalised, basis = latest, "latest_nonpositive_fail_closed"
    elif all(current <= previous for previous, current in zip(recent, recent[1:])):
        normalised, basis = latest, "latest_persistent_decline"
    else:
        premium_limit = latest * _AUDIT_NORMALISATION_PREMIUM_CAP
        normalised = min(median, premium_limit)
        basis = "recent_median" if median <= premium_limit else "latest_premium_cap"
    periods = [
        {"kind": "annual", "report_date": prior_annual_date},
        {"kind": "annual", "report_date": contract.annual_report_date},
        {"kind": "ttm", "through_report_date": contract.current_interim_report_date},
    ]
    return float(normalised), latest, recent, basis, periods


def _audit_consecutive_suffix(years: list[int], maximum: int | None = None) -> list[int]:
    if not years:
        return []
    suffix = [years[-1]]
    for year in reversed(years[:-1]):
        if year != suffix[0] - 1:
            break
        suffix.insert(0, year)
    return suffix[-maximum:] if maximum is not None else suffix


def _audit_quality_evidence(financial: Mapping[str, Any], fcf_margin: float) -> bool:
    if not math.isfinite(fcf_margin) or fcf_margin < 0.12:
        return False
    current = _audit_current_period_evidence(financial)
    if (
        _finite(current.get("profit")) is None
        or float(current["profit"]) <= 0
        or _finite(current.get("operating_cash_flow")) is None
        or float(current["operating_cash_flow"]) <= 0
        or current.get("profit_yoy_basis") != "same_period_yoy"
        or float(current["profit_yoy"]) <= -0.20
        or current.get("operating_cash_flow_yoy_basis") != "same_period_yoy"
        or float(current["operating_cash_flow_yoy"]) <= -0.30
        or current.get("revenue_yoy_basis") != "same_period_yoy"
        or float(current["revenue_yoy"]) <= -0.15
    ):
        return False
    profits = _annual_values(financial.get("income_history", []), ("PARENT_NETPROFIT",))
    revenues = _annual_values(
        financial.get("income_history", []),
        ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
    )
    common = _audit_consecutive_suffix(sorted(set(profits) & set(revenues)), maximum=5)
    if len(common) < 4:
        return False
    margins = [profits[year] / revenues[year] for year in common if revenues[year] > 0]
    if len(margins) < 4 or sum(value > 0 for value in margins) / len(margins) < 0.8:
        return False
    median_margin = statistics.median(margins)
    mean_margin = statistics.fmean(margins)
    if (
        median_margin < 0.15
        or margins[-1] < median_margin * 0.70
        or mean_margin <= 0
        or statistics.pstdev(margins) / mean_margin > 0.30
    ):
        return False
    equity = _annual_equity(financial.get("balance", []))
    roe_years = _audit_consecutive_suffix(
        [year for year in common if year in equity and year - 1 in equity],
        maximum=5,
    )
    if len(roe_years) < 3:
        return False
    roes = [profits[year] / ((equity[year - 1] + equity[year]) / 2.0) for year in roe_years]
    if roes[-1] < 0.20 or statistics.median(roes) < 0.18:
        return False
    rows = (
        [financial.get("cashflow", [])]
        if isinstance(financial.get("cashflow"), Mapping)
        else financial.get("cashflow", [])
    )
    fcff_by_year: dict[int, tuple[bool, str, float]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        operating = _first_finite(row, _AUDIT_OPERATING_CASH_KEYS)
        capex = _first_finite(row, _AUDIT_CAPEX_KEYS)
        if len(report_date) < 4 or not report_date[:4].isdigit() or operating is None or capex is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, operating - abs(capex))
        if year not in fcff_by_year or candidate[:2] > fcff_by_year[year][:2]:
            fcff_by_year[year] = candidate
    fcff_years = _audit_consecutive_suffix(sorted(fcff_by_year), maximum=5)
    fcffs = [fcff_by_year[year][2] for year in fcff_years]
    if len(fcffs) < 3 or sum(value > 0 for value in fcffs) / len(fcffs) < (2.0 / 3.0):
        return False
    median_fcff = statistics.median(fcffs)
    return median_fcff > 0 and fcffs[-1] >= median_fcff * 0.40


def _audit_net_debt(records: Any) -> float:
    rows = [records] if isinstance(records, Mapping) else records
    usable = [row for row in (rows or []) if isinstance(row, Mapping)]
    if not usable:
        return 0.0
    latest = max(usable, key=lambda row: str(row.get("REPORT_DATE") or ""))
    debt_known = False
    direct = None
    for key in _AUDIT_DIRECT_DEBT_KEYS:
        if key in latest:
            value = _finite(latest.get(key))
            if value is not None and value >= 0:
                debt_known = True
                direct = value
                break
    debt = direct if direct is not None else 0.0
    if direct is None:
        for key in _AUDIT_DEBT_COMPONENT_KEYS:
            if key not in latest:
                continue
            value = _finite(latest.get(key))
            if value is not None and value >= 0:
                debt_known = True
                debt += value
    cash = 0.0
    for key in _AUDIT_CASH_KEYS:
        if key in latest:
            value = _finite(latest.get(key))
            if value is not None and value >= 0:
                cash = value
                break
    return debt - cash if debt_known else 0.0


def _valuation_contract_errors(
    code: str,
    row: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    quote: Mapping[str, Any] | None,
    financial: Mapping[str, Any] | None,
    reporting_period_contract: ReportingPeriodContract,
) -> list[str]:
    errors: list[str] = []
    if _normalise_code(result.get("code")) != code:
        errors.append(f"{code}: valuation code differs from score code")
    price = _finite(result.get("current_price"))
    row_price = _finite(row.get("price"))
    if price is None or price <= 0:
        errors.append(f"{code}: valuation current_price invalid")
        return errors
    if row_price is None or not _close(row_price, price):
        errors.append(f"{code}: valuation/score price mismatch")
    if quote is not None:
        quote_price = _finite(quote.get("price"))
        if quote_price is None or not _close(price, quote_price):
            errors.append(f"{code}: valuation/quote price mismatch")
        quote_name = str(quote.get("name") or "").strip()
        if quote_name and str(result.get("name") or "").strip() != quote_name:
            errors.append(f"{code}: valuation/quote name mismatch")
        quote_cap = _finite(quote.get("market_cap"))
        row_cap = _finite(row.get("market_cap"))
        if quote_cap is None or quote_cap <= 0:
            errors.append(f"{code}: quote market cap invalid")
        elif row_cap is None or not _close(row_cap, quote_cap):
            errors.append(f"{code}: score/quote market cap mismatch")
    industry = str(result.get("industry_code") or "")
    if not industry or industry != str(row.get("industry") or ""):
        errors.append(f"{code}: valuation/score industry mismatch")

    points = result.get("dcf_points")
    params = result.get("params")
    if not isinstance(points, Mapping) or set(points) != set(_AUDIT_SCENARIOS):
        errors.append(f"{code}: valuation scenarios missing or extra")
        return errors
    if not isinstance(params, Mapping) or set(params) != set(_AUDIT_SCENARIOS):
        errors.append(f"{code}: valuation parameters missing or extra")
        return errors

    parsed_points: dict[str, tuple[float, float]] = {}
    for scenario in _AUDIT_SCENARIOS:
        band = points.get(scenario)
        if not isinstance(band, Mapping):
            errors.append(f"{code}: {scenario} valuation band missing")
            continue
        lower, upper = _finite(band.get("lower")), _finite(band.get("upper"))
        if lower is None or upper is None or lower <= 0 or upper <= 0 or lower > upper:
            errors.append(f"{code}: {scenario} valuation band invalid")
            continue
        parsed_points[scenario] = (lower, upper)
    if len(parsed_points) != 3:
        return errors

    is_financial = result.get("_pb_valuation") is True
    if industry == "FINANCIAL_OTHER":
        errors.append(f"{code}: unsupported financial industry cannot have a valuation result")
    elif is_financial != (industry in _AUDIT_FINANCIAL_INDUSTRIES):
        errors.append(f"{code}: valuation model is inconsistent with industry")
    expected_tax_source = (
        "financial_operating_liabilities_excluded" if is_financial else "taxable_profit_evidence_unavailable"
    )
    if _finite(result.get("tax_shield_rate")) != 0.0 or result.get("tax_shield_source") != expected_tax_source:
        errors.append(f"{code}: debt tax shield is not supported by auditable tax evidence")
    shares = _finite(result.get("shares_outstanding"))
    if shares is None or shares <= 0:
        errors.append(f"{code}: valuation shares_outstanding invalid")
    elif quote is not None:
        quote_cap = _finite(quote.get("market_cap"))
        quote_price = _finite(quote.get("price"))
        if quote_cap is None or quote_price is None or quote_cap <= 0 or quote_price <= 0:
            errors.append(f"{code}: quote cannot reproduce shares_outstanding")
        elif not _close(shares, quote_cap / quote_price):
            errors.append(f"{code}: shares_outstanding does not match current quote")

    components = result.get("wacc_components")
    risk_parameters = result.get("risk_parameters")
    if not isinstance(risk_parameters, Mapping):
        errors.append(f"{code}: CAPM risk parameter evidence missing")
        capm_risk_free = _AUDIT_RISK_FREE_RATE
        capm_erp = _AUDIT_EQUITY_RISK_PREMIUM
    else:
        capm_risk_free = _finite(risk_parameters.get("risk_free_rate"))
        capm_erp = _finite(risk_parameters.get("equity_risk_premium"))
        if (
            capm_risk_free is None
            or capm_erp is None
            or not _close(capm_risk_free, _AUDIT_RISK_FREE_RATE, rel_tol=0.0)
            or not _close(capm_erp, _AUDIT_EQUITY_RISK_PREMIUM, rel_tol=0.0)
            or risk_parameters.get("risk_free_rate_as_of") != "2026-07-15"
            or risk_parameters.get("equity_risk_premium_as_of") != "2026-04-01"
            or risk_parameters.get("model_as_of") != "2026-07-15"
            or not str(risk_parameters.get("risk_free_rate_source_url") or "").startswith("https://")
            or not str(risk_parameters.get("equity_risk_premium_source_url") or "").startswith("https://")
        ):
            errors.append(f"{code}: CAPM risk parameter evidence invalid")
            capm_risk_free = _AUDIT_RISK_FREE_RATE
            capm_erp = _AUDIT_EQUITY_RISK_PREMIUM
    component_wacc: float | None = None
    if not isinstance(components, Mapping):
        errors.append(f"{code}: WACC components missing")
    else:
        equity_weight = _finite(components.get("equity_weight"))
        debt_weight = _finite(components.get("debt_weight"))
        cost_of_equity = _finite(components.get("cost_of_equity"))
        tax_shield = _finite(components.get("tax_shield_rate"))
        cost_of_debt = _finite(components.get("pre_tax_cost_of_debt"))
        if (
            equity_weight is None
            or debt_weight is None
            or cost_of_equity is None
            or tax_shield is None
            or not 0 <= equity_weight <= 1
            or not 0 <= debt_weight <= 1
            or not _close(equity_weight + debt_weight, 1.0)
            or cost_of_equity <= 0
            or tax_shield != 0.0
            or (debt_weight > 0 and (cost_of_debt is None or cost_of_debt < 0))
        ):
            errors.append(f"{code}: WACC components invalid")
        else:
            debt_cost = 0.0 if debt_weight == 0 else float(cost_of_debt)
            component_wacc = equity_weight * cost_of_equity + debt_weight * debt_cost * (1.0 - tax_shield)
            levered_beta = _finite(result.get("levered_beta"))
            if levered_beta is None or not _close(
                cost_of_equity,
                capm_risk_free + levered_beta * capm_erp,
            ):
                errors.append(f"{code}: cost of equity does not match CAPM")
            beta_u = _finite(result.get("industry_unlevered_beta"))
            if (
                not is_financial
                and beta_u is not None
                and str(result.get("beta_source") or "") == "industry_unlevered_relevered"
                and equity_weight > 0
            ):
                expected_beta = beta_u * (1.0 + (1.0 - tax_shield) * debt_weight / equity_weight)
                if levered_beta is None or not _close(levered_beta, expected_beta):
                    errors.append(f"{code}: levered beta does not match capital structure")
            beta_evidence = result.get("beta_evidence")
            if not isinstance(beta_evidence, Mapping):
                errors.append(f"{code}: beta evidence missing")
            elif str(result.get("beta_source") or "") == "industry_and_weekly_market_beta_blend":
                industry_beta = _finite(beta_evidence.get("industry_beta"))
                company_weight = _finite(beta_evidence.get("company_weight"))
                industry_weight = _finite(beta_evidence.get("industry_weight"))
                market_beta = _finite(beta_evidence.get("blume_beta"))
                r_squared = _finite(beta_evidence.get("r_squared"))
                sample_size = _finite(beta_evidence.get("sample_size"))
                if (
                    industry_beta is None
                    or company_weight is None
                    or industry_weight is None
                    or market_beta is None
                    or r_squared is None
                    or sample_size is None
                    or sample_size < 156
                    or not 0.05 <= r_squared <= 1.0
                    or not _close(company_weight, min(0.50, r_squared))
                    or not _close(company_weight + industry_weight, 1.0)
                    or levered_beta is None
                    or not _close(
                        levered_beta,
                        industry_weight * industry_beta + company_weight * market_beta,
                    )
                ):
                    errors.append(f"{code}: blended beta evidence cannot reproduce final beta")
            if not _close(result.get("base_wacc"), round(component_wacc, 4), rel_tol=0.0, abs_tol=5e-5):
                errors.append(f"{code}: base WACC does not match components")

    if is_financial:
        centers = tuple((parsed_points[key][0] + parsed_points[key][1]) / 2.0 for key in _AUDIT_SCENARIOS)
        if not centers[0] <= centers[1] <= centers[2]:
            errors.append(f"{code}: financial scenario centers are unordered")
        normalised_roe = _finite(result.get("normalised_roe"))
        evidence_years_raw = result.get("roe_evidence_years")
        evidence_years = list(evidence_years_raw) if isinstance(evidence_years_raw, (list, tuple)) else []
        if (
            len(evidence_years) < 3
            or len(evidence_years) > 5
            or any(not isinstance(year, int) or isinstance(year, bool) for year in evidence_years)
            or evidence_years != sorted(set(evidence_years))
            or any(current != previous + 1 for previous, current in zip(evidence_years, evidence_years[1:]))
        ):
            errors.append(f"{code}: financial ROE evidence years invalid")
        independently_computed_roes: list[float] = []
        latest_equity: float | None = None
        if financial is not None:
            annual_profit = _annual_values(financial.get("income_history", []), ("PARENT_NETPROFIT",))
            annual_equity = _annual_equity(financial.get("balance", []))
            latest_equity = _latest_equity(financial.get("balance", []))
            for year in evidence_years:
                if year not in annual_profit or year not in annual_equity or year - 1 not in annual_equity:
                    errors.append(f"{code}: financial ROE evidence is not attributable/reproducible")
                    independently_computed_roes = []
                    break
                independently_computed_roes.append(
                    annual_profit[year] / ((annual_equity[year - 1] + annual_equity[year]) / 2.0)
                )
            if independently_computed_roes:
                expected_roe = statistics.median(independently_computed_roes)
                if normalised_roe is None or not _close(normalised_roe, expected_roe):
                    errors.append(f"{code}: normalised ROE does not match attributable evidence")
                if evidence_years[-1] != max(annual_profit) or evidence_years[-1] != max(annual_equity):
                    errors.append(f"{code}: financial ROE evidence is not current")
                if annual_profit[evidence_years[-1]] <= 0 or independently_computed_roes[-1] <= 0:
                    errors.append(f"{code}: financial latest annual attributable result is non-positive")
            expected_current = _audit_current_period_evidence(financial)
            if _canonical_json(result.get("current_period_evidence")) != _canonical_json(expected_current):
                errors.append(f"{code}: financial current-period evidence mismatch")
            current_profit = _finite(expected_current.get("profit"))
            current_profit_yoy = _finite(expected_current.get("profit_yoy"))
            current_profit_basis = expected_current.get("profit_yoy_basis")
            if current_profit is None or current_profit <= 0:
                errors.append(f"{code}: financial current-period attributable profit is not P/B-eligible")
            elif current_profit_basis == "same_period_yoy":
                if current_profit_yoy is None or current_profit_yoy <= -0.30:
                    errors.append(f"{code}: financial current-period attributable profit is not P/B-eligible")
            elif current_profit_basis != "same_period_turnaround":
                errors.append(f"{code}: financial current-period attributable profit is not P/B-eligible")
        for scenario in _AUDIT_SCENARIOS:
            scenario_params = params[scenario]
            if not isinstance(scenario_params, Mapping):
                errors.append(f"{code}: {scenario} P/B parameters missing")
                continue
            growth = _finite(scenario_params.get("growth"))
            scenario_roe = _finite(scenario_params.get("scenario_roe"))
            cost = _finite(scenario_params.get("cost_of_equity"))
            bvps = _finite(scenario_params.get("bvps"))
            pb_lower = _finite(scenario_params.get("pb_lower"))
            pb_upper = _finite(scenario_params.get("pb_upper"))
            if None in (growth, scenario_roe, cost, bvps, pb_lower, pb_upper):
                errors.append(f"{code}: {scenario} P/B inputs invalid")
                continue
            if scenario_roe <= growth or cost - _AUDIT_BAND_WACC_DELTA <= growth or bvps <= 0:
                errors.append(f"{code}: {scenario} P/B denominator/input invalid")
                continue
            expected_pb_lower = (scenario_roe - growth) / (cost + _AUDIT_BAND_WACC_DELTA - growth)
            expected_pb_upper = (scenario_roe - growth) / (cost - _AUDIT_BAND_WACC_DELTA - growth)
            lower, upper = parsed_points[scenario]
            if not _close(pb_lower, expected_pb_lower) or not _close(pb_upper, expected_pb_upper):
                errors.append(f"{code}: {scenario} justified P/B multiple mismatch")
            if not _close(lower, bvps * expected_pb_lower) or not _close(upper, bvps * expected_pb_upper):
                errors.append(f"{code}: {scenario} justified P/B endpoint mismatch")
            if component_wacc is not None and (
                not _close(cost, component_wacc) or not _close(scenario_params.get("wacc_base"), component_wacc)
            ):
                errors.append(f"{code}: {scenario} financial cost of equity mismatch")
            if normalised_roe is None or not _close(scenario_params.get("normalised_roe"), normalised_roe):
                errors.append(f"{code}: {scenario} normalised ROE mismatch")
            if list(scenario_params.get("roe_years") or []) != evidence_years:
                errors.append(f"{code}: {scenario} ROE evidence years mismatch")
            if str(scenario_params.get("formula") or "") != "(normalised_roe - g) / (cost_of_equity - g)":
                errors.append(f"{code}: {scenario} P/B formula provenance mismatch")
            if shares is not None and latest_equity is not None and not _close(bvps, latest_equity / shares):
                errors.append(f"{code}: {scenario} BVPS does not match attributable equity")
        if independently_computed_roes and normalised_roe is not None:
            expected_scenario_roes = {
                "pessimistic": min(_quantile(independently_computed_roes, 0.25), normalised_roe * 0.8),
                "neutral": normalised_roe,
                "optimistic": max(
                    normalised_roe,
                    min(_quantile(independently_computed_roes, 0.75), normalised_roe * 1.1),
                ),
            }
            for scenario, expected_roe in expected_scenario_roes.items():
                if isinstance(params[scenario], Mapping) and not _close(
                    params[scenario].get("scenario_roe"), expected_roe
                ):
                    errors.append(f"{code}: {scenario} ROE scenario mismatch")
    else:
        current_growth_caps = {scenario: 1.0 for scenario in _AUDIT_SCENARIOS}
        current_growth_basis = "no_current_period_downward_cap"
        pessimistic, neutral, optimistic = (parsed_points[key] for key in _AUDIT_SCENARIOS)
        if not (pessimistic[1] <= neutral[0] <= neutral[1] <= optimistic[0]):
            errors.append(f"{code}: DCF scenario bands overlap or are unordered")
        base_fcf = _finite(result.get("base_fcf"))
        base_revenue = _finite(result.get("base_revenue"))
        net_debt = _finite(result.get("net_debt"))
        if None in (base_fcf, base_revenue, shares, net_debt):
            errors.append(f"{code}: DCF base inputs invalid")
        else:
            if financial is not None:
                if result.get("valuation_input_basis") != "strict_ttm":
                    errors.append(f"{code}: industrial valuation input basis is not strict TTM")
                if result.get("base_revenue_basis") != "strict_ttm_reported_revenue":
                    errors.append(f"{code}: industrial base revenue basis is not strict TTM")
                if result.get("base_fcf_basis") != "normalised_two_annual_plus_ttm_cfo_less_capex_proxy":
                    errors.append(f"{code}: industrial base FCFF basis is not strict TTM")

                expected_ttm_fcff = _audit_reconstruct_ttm(
                    "fcff",
                    financial.get("cashflow", []),
                    financial.get("cashflow_interim", []),
                    reporting_period_contract,
                    expected_security_code=code,
                )
                expected_ttm_revenue = _audit_reconstruct_ttm(
                    "revenue",
                    financial.get("revenue_history", []),
                    financial.get("income_interim", []),
                    reporting_period_contract,
                )
                if result.get("ttm_fcff_evidence") != expected_ttm_fcff:
                    errors.append(f"{code}: strict TTM FCFF provenance differs from source reconstruction")
                if result.get("ttm_revenue_evidence") != expected_ttm_revenue:
                    errors.append(f"{code}: strict TTM revenue provenance differs from source reconstruction")

                expected_revenue = (
                    _finite(expected_ttm_revenue.get("value"))
                    if expected_ttm_revenue.get("status") == "complete"
                    else None
                )
                if expected_revenue is None or expected_revenue <= 0:
                    errors.append(f"{code}: strict TTM revenue reconstruction is not positive and complete")
                elif not _close(base_revenue, expected_revenue):
                    errors.append(f"{code}: DCF base revenue does not match strict TTM company evidence")

                normalisation = (
                    _audit_ttm_fcff_normalisation(
                        financial.get("cashflow", []),
                        expected_ttm_fcff,
                        reporting_period_contract,
                    )
                    if expected_ttm_fcff.get("status") == "complete"
                    else None
                )
                normalised: float | None = None
                annual_fcffs = _audit_fcff_normalisation(financial.get("cashflow", []))[4]
                expected_adjustments: list[dict[str, Any]] = []
                if normalisation is None:
                    errors.append(f"{code}: strict FY-1/FY/TTM FCFF normalisation evidence is incomplete")
                else:
                    normalised, latest_fcff, recent_fcff, basis, periods = normalisation
                    if not _close(result.get("latest_fcff"), latest_fcff):
                        errors.append(f"{code}: latest FCFF does not match strict TTM reconstruction")
                    published_recent = result.get("recent_fcff")
                    if (
                        not isinstance(published_recent, (list, tuple))
                        or len(published_recent) != len(recent_fcff)
                        or any(not _close(actual, expected) for actual, expected in zip(published_recent, recent_fcff))
                    ):
                        errors.append(f"{code}: recent FCFF does not match FY-1/FY/TTM evidence")
                    if result.get("recent_fcff_periods") != periods:
                        errors.append(f"{code}: recent FCFF periods do not match strict TTM contract")
                    if str(result.get("fcf_normalisation_basis") or "") != basis:
                        errors.append(f"{code}: FCFF normalisation basis mismatch")
                    if result.get("fcf_normalisation_period_basis") != "two_annual_plus_strict_ttm":
                        errors.append(f"{code}: FCFF normalisation period basis is not strict TTM")
                    expected_period_detail = {
                        "period_set": "two_annual_plus_strict_ttm",
                        "periods": periods,
                        "normalisation_method": basis,
                        "cash_flow_kind": expected_ttm_fcff.get("cash_flow_kind"),
                        "formula_version": expected_ttm_fcff.get("formula_version"),
                    }
                    if result.get("fcf_normalisation_period") != expected_period_detail:
                        errors.append(f"{code}: FCFF normalisation period provenance mismatch")
                if not _close(
                    result.get("normalisation_premium_cap"),
                    _AUDIT_NORMALISATION_PREMIUM_CAP,
                    rel_tol=0.0,
                ):
                    errors.append(f"{code}: FCFF normalisation premium cap mismatch")

                expected_current = _audit_current_period_evidence(financial)
                current_growth_caps, current_growth_basis = _audit_growth_cap(expected_current)
                expected_published_current = {**expected_current, "growth_cap_basis": current_growth_basis}
                if _canonical_json(result.get("current_period_evidence")) != _canonical_json(
                    expected_published_current
                ):
                    errors.append(f"{code}: DCF current-period evidence mismatch")

                expected_fcf_pre_cap = normalised
                profits = _annual_values(
                    financial.get("income_history", []),
                    ("PARENT_NETPROFIT",),
                )
                ordered_profits = [profits[year] for year in sorted(profits)]
                if (
                    expected_fcf_pre_cap is not None
                    and len(ordered_profits) >= 3
                    and min(ordered_profits) < 0 < max(ordered_profits)
                ):
                    recent_profit = ordered_profits[-3:]
                    recovering = recent_profit[0] < recent_profit[1] < recent_profit[2] and recent_profit[2] > 0
                    if not recovering and len(annual_fcffs) >= 3:
                        p25 = _quantile(annual_fcffs, 0.25)
                        adjusted = min(expected_fcf_pre_cap, p25)
                        if adjusted != expected_fcf_pre_cap:
                            expected_adjustments.append(
                                {
                                    "kind": "mixed_profit_cycle_p25_cap",
                                    "before": expected_fcf_pre_cap,
                                    "limit": p25,
                                    "after": adjusted,
                                }
                            )
                        expected_fcf_pre_cap = adjusted
                published_quality = _strict_bool(result.get("quality_evidence"))
                expected_quality = (
                    _audit_quality_evidence(financial, expected_fcf_pre_cap / expected_revenue)
                    if expected_fcf_pre_cap is not None and expected_revenue is not None and expected_revenue > 0
                    else False
                )
                if published_quality is None or published_quality != expected_quality:
                    errors.append(f"{code}: quality flag does not match independent current evidence")
                industry_target = _finite(result.get("industry_fcf_margin_target"))
                margin_ceiling = _finite(result.get("fcf_margin_ceiling"))
                if industry_target is None or industry_target < 0 or margin_ceiling is None:
                    errors.append(f"{code}: FCFF margin-cap evidence invalid")
                else:
                    expected_ceiling = (
                        0.65
                        if expected_quality
                        else min(
                            _AUDIT_NONQUALITY_MARGIN_CAP,
                            max(
                                _AUDIT_NONQUALITY_MARGIN_FLOOR,
                                industry_target * _AUDIT_NONQUALITY_MARGIN_MULTIPLIER,
                            ),
                        )
                    )
                    if not _close(margin_ceiling, expected_ceiling):
                        errors.append(f"{code}: FCFF margin ceiling mismatch")
                    if expected_fcf_pre_cap is not None and expected_revenue is not None:
                        expected_fcf = expected_fcf_pre_cap
                        margin_limit = expected_revenue * expected_ceiling
                        if expected_fcf > margin_limit:
                            expected_adjustments.append(
                                {
                                    "kind": "fcf_margin_ceiling",
                                    "before": expected_fcf,
                                    "limit": margin_limit,
                                    "after": margin_limit,
                                }
                            )
                        expected_fcf = min(expected_fcf, margin_limit)
                        if not _close(base_fcf, expected_fcf):
                            errors.append(f"{code}: DCF base FCFF does not match current company evidence")
                if result.get("base_fcf_adjustments") != expected_adjustments:
                    errors.append(f"{code}: base FCFF adjustments differ from strict source normalisation")

                expected_net_debt = _audit_net_debt(financial.get("balance", []))
                if not _close(net_debt, expected_net_debt):
                    errors.append(f"{code}: DCF net debt does not match current company evidence")
            for scenario in _AUDIT_SCENARIOS:
                scenario_params = params[scenario]
                if not isinstance(scenario_params, Mapping):
                    errors.append(f"{code}: {scenario} DCF parameters missing")
                    continue
                growth = _finite(scenario_params.get("growth"))
                wacc_center = _finite(scenario_params.get("wacc_base"))
                terminal_growth = _finite(scenario_params.get("terminal_g"))
                retention = _finite(scenario_params.get("margin_retention"))
                if None in (growth, wacc_center, terminal_growth, retention):
                    errors.append(f"{code}: {scenario} DCF parameters invalid")
                    continue
                if growth > current_growth_caps[scenario] + 1e-12:
                    errors.append(f"{code}: {scenario} growth ignores current-period cap")
                if scenario_params.get("current_period_growth_cap_basis") != current_growth_basis:
                    errors.append(f"{code}: {scenario} current-period growth basis mismatch")
                if component_wacc is not None and not _close(wacc_center, component_wacc + _AUDIT_WACC_SHIFT[scenario]):
                    errors.append(f"{code}: {scenario} WACC shift mismatch")
                expected_lower = _audit_dcf_value(
                    base_fcf=base_fcf,
                    base_revenue=base_revenue,
                    growth=growth,
                    wacc=wacc_center + _AUDIT_BAND_WACC_DELTA,
                    terminal_growth=terminal_growth,
                    shares=shares,
                    net_debt=net_debt,
                    retention=retention,
                )
                expected_upper = _audit_dcf_value(
                    base_fcf=base_fcf,
                    base_revenue=base_revenue,
                    growth=growth,
                    wacc=wacc_center - _AUDIT_BAND_WACC_DELTA,
                    terminal_growth=terminal_growth,
                    shares=shares,
                    net_debt=net_debt,
                    retention=retention,
                )
                lower, upper = parsed_points[scenario]
                if expected_lower is None or not _close(lower, expected_lower):
                    errors.append(f"{code}: {scenario} DCF lower endpoint mismatch")
                if expected_upper is None or not _close(upper, expected_upper):
                    errors.append(f"{code}: {scenario} DCF upper endpoint mismatch")

    pessimistic_upper = parsed_points["pessimistic"][1]
    neutral_lower, neutral_upper = parsed_points["neutral"]
    optimistic_lower, optimistic_upper = parsed_points["optimistic"]
    buy_boundary = (pessimistic_upper + neutral_lower) / 2.0
    sell_boundary = (neutral_upper + optimistic_lower) / 2.0
    valuation_center = (neutral_lower + neutral_upper) / 2.0
    if buy_boundary > sell_boundary:
        errors.append(f"{code}: valuation boundaries are reversed")
    expected_zone = "买入区" if price <= buy_boundary else "卖出区" if price >= sell_boundary else "观察区"
    for field in ("mean1", "buy_zone_upper"):
        if not _close(result.get(field), buy_boundary):
            errors.append(f"{code}: {field} does not match scenario bands")
    if not _close(result.get("dcf_value_mean"), buy_boundary):
        errors.append(f"{code}: legacy dcf_value_mean does not match buy boundary")
    if result.get("dcf_value_mean_legacy_alias_of") != "buy_zone_upper":
        errors.append(f"{code}: legacy dcf_value_mean alias is not disclosed")
    for field in ("valuation_center", "neutral_value_midpoint"):
        if not _close(result.get(field), valuation_center):
            errors.append(f"{code}: {field} does not match neutral scenario midpoint")
    for field in ("mean2", "sell_zone_lower"):
        if not _close(result.get(field), sell_boundary):
            errors.append(f"{code}: {field} does not match scenario bands")
    if result.get("zone") != expected_zone:
        errors.append(f"{code}: valuation zone does not match price and boundaries")
    expected_margin = round((pessimistic_upper - price) / pessimistic_upper * 100.0, 2)
    if not _close(result.get("safety_margin_pct"), expected_margin, rel_tol=0.0, abs_tol=0.005):
        errors.append(f"{code}: safety margin mismatch")
    expected_safety = ""
    if expected_zone == "买入区":
        if price <= pessimistic_upper * _AUDIT_DEEP_SAFETY_RATIO:
            expected_safety = "★★★ 深度安全边际"
        elif price <= pessimistic_upper:
            expected_safety = "★★ 中度安全边际"
    if result.get("safety_score") != expected_safety:
        errors.append(f"{code}: safety score mismatch")
    expected_bubble = price >= optimistic_upper * _AUDIT_BUBBLE_RATIO
    actual_bubble = _strict_bool(result.get("bubble_warning"))
    if actual_bubble is None or actual_bubble != expected_bubble:
        errors.append(f"{code}: bubble warning mismatch")
    return errors


def _type1_valuation_binding_errors(
    code: str,
    row: Mapping[str, Any],
    *,
    expected_type1_1a: float | None,
    skip_classification: Mapping[str, str] | None,
) -> list[str]:
    """Bind Type 1 to either a valid valuation or its structured skip."""

    payload = row.get("type1")
    if not isinstance(payload, Mapping):
        return [f"{code}:type1: valuation binding payload missing"]
    sub_scores = payload.get("sub_scores")
    reasons = payload.get("reasons")
    if not isinstance(sub_scores, Mapping) or not isinstance(reasons, Mapping):
        return [f"{code}:type1: valuation binding scores or reasons missing"]
    score_1a = _finite(sub_scores.get("1a"))
    total = _finite(payload.get("total"))
    triggered = _strict_bool(payload.get("triggered"))
    veto = _strict_bool(payload.get("veto"))
    applicable = _strict_bool(payload.get("applicable"))
    evidence_complete = _strict_bool(payload.get("evidence_complete"))
    status = payload.get("status")

    if expected_type1_1a is not None:
        errors: list[str] = []
        if skip_classification is not None:
            errors.append(f"{code}:type1: valid valuation also has a skip classification")
        if applicable is not True or status == "not_applicable":
            errors.append(f"{code}:type1: valid valuation cannot be hidden as not applicable")
        if score_1a is None or not _close(score_1a, expected_type1_1a, rel_tol=0.0):
            errors.append(f"{code}:type1: 1a differs from independently replayed valuation position")
        if expected_type1_1a <= 2.0 and (veto is not True or status not in {"vetoed", "blocked"}):
            errors.append(f"{code}:type1: price-depth veto is missing")
        return errors

    classification = normalize_dcf_skip_classification(skip_classification)
    if classification is None:
        return [f"{code}:type1: no valid valuation or structured skip classification"]
    scores = [_finite(sub_scores.get(key)) for key in ("1a", "1b", "1c", "1d")]
    errors = []
    if any(score is None or not _close(score, 0.0, rel_tol=0.0) for score in scores):
        errors.append(f"{code}:type1: skipped valuation must have zero sub-scores")
    if total is None or not _close(total, 0.0, rel_tol=0.0):
        errors.append(f"{code}:type1: skipped valuation must have zero total")
    if triggered is not False:
        errors.append(f"{code}:type1: skipped valuation cannot trigger")

    category = classification["category"]
    if category in {DCF_SKIP_MODEL_UNSUPPORTED, DCF_SKIP_ECONOMIC_NOT_APPLICABLE}:
        expected = ("not_applicable", False, True, False)
    elif category in {DCF_SKIP_SOURCE_MISSING, DCF_SKIP_INCONSISTENT_SOURCE}:
        expected = ("insufficient_evidence", True, False, False)
    elif category == DCF_SKIP_INTERNAL_ERROR:
        expected = ("blocked", True, False, False)
        if not str(reasons.get("_blocked") or "").strip():
            errors.append(f"{code}:type1: internal valuation error lacks a blocked reason")
    else:  # pragma: no cover - normalize_dcf_skip_classification already rejects this.
        raise AssertionError(f"unhandled DCF skip category: {category}")
    expected_status, expected_applicable, expected_evidence, expected_veto = expected
    if (
        status != expected_status
        or applicable is not expected_applicable
        or evidence_complete is not expected_evidence
        or veto is not expected_veto
    ):
        errors.append(f"{code}:type1: state does not match structured valuation skip classification")
    return errors


def _type7_valuation_binding_errors(
    code: str,
    row: Mapping[str, Any],
    *,
    expected_type1_1a: float | None,
) -> list[str]:
    """Bind Type 7's DCF claim to this company's independently checked result."""

    errors: list[str] = []
    industry = str(row.get("industry") or "")
    payload = row.get("type7")
    if not isinstance(payload, Mapping):
        return [f"{code}:type7: valuation binding payload missing"]
    status = payload.get("status")
    applicable = _strict_bool(payload.get("applicable"))
    ledger = payload.get("ledger")
    is_financial = industry in _AUDIT_TYPE7_FINANCIAL_INDUSTRIES
    if is_financial:
        if status != "not_applicable" or applicable is not False:
            errors.append(f"{code}:type7: financial industry must be not applicable")
        if not isinstance(ledger, Mapping) or ledger.get("applicable") is not False:
            errors.append(f"{code}:type7: financial not-applicable ledger mismatch")
        return errors
    if status == "not_applicable" or applicable is not True:
        return [f"{code}:type7: non-financial industry cannot be not applicable"]
    if not isinstance(ledger, Mapping):
        return [f"{code}:type7: valuation binding ledger missing"]
    if ledger.get("model_id") != PATCH6_TYPE7_MODEL_ID:
        return [f"{code}:type7: unsupported Type 7 ledger model; refresh required"]

    validated_nonfinancial_dcf = expected_type1_1a is not None
    if ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
        classification = ledger.get("classification")
        gates = ledger.get("decision_gates")
        route = gates.get("route_path") if isinstance(gates, Mapping) else None
        valuation = gates.get("price_reasonableness") if isinstance(gates, Mapping) else None
        class_code = str(classification.get("class_code") or "") if isinstance(classification, Mapping) else ""
        if (
            class_code not in {"W", "C", "T"}
            or not isinstance(route, Mapping)
            or not isinstance(valuation, Mapping)
            or route.get("class_code") != class_code
            or valuation.get("class_code") != class_code
            or ledger.get("code") != code
            or ledger.get("as_of") != str(row.get("source_trade_date") or "")
        ):
            return [f"{code}:type7: classified valuation identity mismatch"]
        if class_code != "W":
            valuation_inputs = valuation.get("inputs")
            current_pb = _finite(valuation_inputs.get("current_pb")) if isinstance(valuation_inputs, Mapping) else None
            row_pb = _finite(row.get("pb"))
            source_complete = valuation.get("source_evidence_complete") is True
            price_required = valuation.get("required") is True
            if (
                not isinstance(valuation_inputs, Mapping)
                or set(valuation_inputs) != {"pb_percentile", "current_pb"}
                or type(valuation.get("required")) is not bool
                or valuation.get("complete") is not (source_complete or not price_required)
                or (
                    source_complete
                    and (
                        current_pb is None
                        or row_pb is None
                        or current_pb <= 0
                        or row_pb <= 0
                        or abs(current_pb - row_pb) / max(current_pb, row_pb) > 0.20
                    )
                )
            ):
                errors.append(f"{code}:type7: PB price gate is not bound to this company quote/history")
            if class_code == "C":
                route_inputs = route.get("inputs")
                type5 = row.get("type5")
                type5_scores = type5.get("sub_scores") if isinstance(type5, Mapping) else None
                type5_reasons = type5.get("reasons") if isinstance(type5, Mapping) else None
                missing = (
                    type5_reasons.get("_decision_missing_dimensions") if isinstance(type5_reasons, Mapping) else None
                )
                missing_set = set(missing) if isinstance(missing, list) else set()
                type5_status = str(type5.get("status") or "") if isinstance(type5, Mapping) else ""
                source_scores = {
                    key: _finite(type5_scores.get(key)) if isinstance(type5_scores, Mapping) else None
                    for key in _INDEPENDENT_TYPE7_TYPE5_ROUTE_WEIGHTS
                }
                source_total = _finite(type5.get("total")) if isinstance(type5, Mapping) else None
                source_triggered = _strict_bool(type5.get("triggered")) if isinstance(type5, Mapping) else None
                source_evidence_complete = (
                    _strict_bool(type5.get("evidence_complete")) if isinstance(type5, Mapping) else None
                )
                dimension_fields = {
                    "5a": ("type5_cycle_complete", "type5_cycle_score"),
                    "5b": ("type5_bottom_complete", "type5_bottom_score"),
                    "5c": ("type5_survival_complete", "type5_survival_score"),
                    "5d": ("type5_upside_complete", "type5_upside_score"),
                    "5e": ("type5_valuation_complete", "type5_valuation_score"),
                }
                expected_type5_binding: dict[str, Any] = {
                    "type5_applicable": type5_status != "not_applicable",
                    "type5_evidence_complete": source_evidence_complete is True,
                    "type5_triggered": source_triggered,
                    "type5_total": source_total,
                }
                for dimension, (complete_field, score_field) in dimension_fields.items():
                    expected_type5_binding[complete_field] = bool(
                        type5_status != "not_applicable"
                        and source_scores[dimension] is not None
                        and dimension not in missing_set
                    )
                    expected_type5_binding[score_field] = source_scores[dimension]
                replayed_total = (
                    round(
                        math.fsum(
                            float(source_scores[key]) * weight
                            for key, weight in _INDEPENDENT_TYPE7_TYPE5_ROUTE_WEIGHTS.items()
                        ),
                        1,
                    )
                    if all(value is not None for value in source_scores.values())
                    else None
                )
                replayed_triggered = bool(
                    expected_type5_binding["type5_applicable"] is True
                    and expected_type5_binding["type5_evidence_complete"] is True
                    and all(expected_type5_binding[field] is True for field, _score in dimension_fields.values())
                    and source_scores["5a"] is not None
                    and source_scores["5a"] >= 7.0
                    and replayed_total is not None
                    and replayed_total >= 7.0
                )
                expected_type5_binding["type5_replayed_total"] = replayed_total
                expected_type5_binding["type5_replayed_triggered"] = replayed_triggered
                if not isinstance(route_inputs, Mapping) or any(
                    _canonical_json(route_inputs.get(key)) != _canonical_json(expected)
                    for key, expected in expected_type5_binding.items()
                ):
                    errors.append(f"{code}:type7: strong-cycle route is not bound to Type 5 evidence")
            return errors

        type1 = row.get("type1")
        type1_scores = type1.get("sub_scores") if isinstance(type1, Mapping) else None
        type1_1a = _finite(type1_scores.get("1a")) if isinstance(type1_scores, Mapping) else None
        ledger_score = _finite(valuation.get("buy_zone_score")) if isinstance(valuation, Mapping) else None
        expected_score = expected_type1_1a if expected_type1_1a is not None else type1_1a
        price_required = valuation.get("required") is True if isinstance(valuation, Mapping) else True
        if (
            not isinstance(valuation, Mapping)
            or valuation.get("source_evidence_complete") is not validated_nonfinancial_dcf
            or type(valuation.get("required")) is not bool
            or valuation.get("complete") is not (validated_nonfinancial_dcf or not price_required)
            or type1_1a is None
            or ledger_score is None
            or expected_score is None
            or not _close(type1_1a, expected_score, rel_tol=0.0)
            or not _close(ledger_score, expected_score, rel_tol=0.0)
        ):
            errors.append(f"{code}:type7: price gate is not bound to validated Type 1 valuation")
        return errors

    raise AssertionError("canonical Type 7 valuation branch must return")


def _expected_type1_1a_from_dcf(result: Mapping[str, Any]) -> float | None:
    """Replay Type 1's price-position bucket from a validated DCF result."""

    price = _finite(result.get("current_price"))
    buy_upper = _finite(result.get("buy_zone_upper"))
    if price is None or price <= 0 or buy_upper is None or buy_upper <= 0:
        return None
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


def _independent_checks(
    scores: pd.DataFrame,
    sampled: tuple[str, ...],
    dcf_results: Mapping[str, Mapping[str, Any]],
    skip_reasons: Mapping[str, str],
    *,
    skip_classifications: Mapping[str, Mapping[str, str]] | None = None,
    quotes: pd.DataFrame | None = None,
    financials: Mapping[str, Mapping[str, Any]] | None = None,
    expected_interim_report_date: str | None = None,
    reporting_period_contract: ReportingPeriodContract | None = None,
    patch4_bindings: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
    source_replay_scores: pd.DataFrame | None = None,
) -> tuple[str, ...]:
    """Recompute fixed contracts and bind them to a captured-input replay.

    ``source_replay_scores`` is a same-production source-binding replay.  It is
    kept separate from the independent arithmetic and decision checks in this
    function so audit reports do not mislabel implementation replay as an
    independent scoring oracle.
    """
    if not _audit_valid_reporting_period_contract(reporting_period_contract):
        return ("independent: valid reporting_period_contract is required",)
    errors: list[str] = []
    if _production_screener.TYPE_WEIGHTS != _AUDIT_WEIGHTS:
        errors.append("independent: production type weights differ from fixed audit contract")
    if tuple(_production_screener.TYPE_PRIORITY) != _AUDIT_PRIORITY:
        errors.append("independent: production type priority differs from fixed audit contract")
    if _production_screener.TYPE_NAMES != _AUDIT_NAMES:
        errors.append("independent: production type names differ from fixed audit contract")
    if _finite(_production_screener.QUALIFY_THRESHOLD) != _AUDIT_QUALIFY_THRESHOLD:
        errors.append("independent: production qualification threshold differs from fixed audit contract")
    if _finite(_production_screener.EVIDENCE_MAX_LENGTH) != _AUDIT_REASON_MAX_LENGTH:
        errors.append("independent: production reason length differs from fixed audit contract")
    fixed_config = {
        "BAND_WACC_DELTA": _AUDIT_BAND_WACC_DELTA,
        "BUBBLE_RATIO": _AUDIT_BUBBLE_RATIO,
        "DEEP_SAFETY_RATIO": _AUDIT_DEEP_SAFETY_RATIO,
        "EQUITY_RISK_PREMIUM": _AUDIT_EQUITY_RISK_PREMIUM,
        "FCF_MARGIN_FLOOR": _AUDIT_FCF_MARGIN_FLOOR,
        "FCF_MARGIN_LONG_TERM": _AUDIT_FCF_MARGIN_LONG_TERM,
        "FORECAST_YEARS": _AUDIT_FORECAST_YEARS,
        "RISK_FREE_RATE": _AUDIT_RISK_FREE_RATE,
    }
    for name, expected in fixed_config.items():
        if not _close(getattr(_production_config, name, None), expected, rel_tol=0.0):
            errors.append(f"independent: production {name} differs from fixed audit contract")
    fixed_risk_metadata = {
        "MODEL_RISK_DATA_AS_OF": "2026-07-15",
        "RISK_FREE_RATE_AS_OF": "2026-07-15",
        "RISK_FREE_RATE_TENOR": "10Y",
        "RISK_FREE_RATE_SOURCE_URL": ("https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN"),
        "EQUITY_RISK_PREMIUM_AS_OF": "2026-04-01",
        "EQUITY_RISK_PREMIUM_BASIS": "china_rating_based_total_erp",
        "EQUITY_RISK_PREMIUM_SOURCE_URL": ("https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx"),
        "EQUITY_RISK_PREMIUM_SOURCE_SHA256": ("2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e"),
        "INDUSTRY_RISK_DATA_AS_OF": "2026-01-05",
        "INDUSTRY_BETA_SOURCE_SHA256": ("ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9"),
        "INDUSTRY_WACC_SOURCE_SHA256": ("525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6"),
    }
    for name, expected in fixed_risk_metadata.items():
        if getattr(_production_config, name, None) != expected:
            errors.append(f"independent: production {name} differs from fixed audit contract")
    if "code" not in scores:
        return ("independent: missing code column",)
    result_codes = scores["code"].map(_normalise_code).tolist()
    if len(result_codes) != len(set(result_codes)):
        errors.append("independent: duplicate result codes")
    if set(result_codes) != set(sampled):
        errors.append("independent: sampled/result code sets differ")

    quote_index: dict[str, Mapping[str, Any]] = {}
    if quotes is not None:
        if not isinstance(quotes, pd.DataFrame) or "code" not in quotes:
            errors.append("independent: quotes missing code column")
        else:
            for record in quotes.to_dict(orient="records"):
                code = _normalise_code(record.get("code"))
                if code in quote_index:
                    errors.append(f"independent: duplicate quote code {code}")
                quote_index[code] = record
    financial_index: dict[str, Mapping[str, Any]] = {}
    if financials is not None:
        if not isinstance(financials, Mapping):
            errors.append("independent: financials is not a mapping")
        else:
            for raw_code, record in financials.items():
                code = _normalise_code(raw_code)
                if code in financial_index:
                    errors.append(f"independent: duplicate financial code {code}")
                if isinstance(record, Mapping):
                    financial_index[code] = record

    source_replay_index: dict[str, Mapping[str, Any]] = {}
    if source_replay_scores is not None:
        if not isinstance(source_replay_scores, pd.DataFrame) or "code" not in source_replay_scores:
            errors.append("independent: raw-source scoring replay missing code column")
        else:
            for record in source_replay_scores.to_dict(orient="records"):
                code = _normalise_code(record.get("code"))
                if code in source_replay_index:
                    errors.append(f"independent: duplicate raw-source replay code {code}")
                source_replay_index[code] = record
            missing_replays = sorted(set(sampled) - set(source_replay_index))
            if missing_replays:
                errors.append(f"independent: raw-source replay missing sampled companies {missing_replays[:5]}")

    for code in sampled:
        financial = financial_index.get(code)
        if financial is not None:
            errors.extend(_audit_financial_source_errors(code, financial, expected_interim_report_date))

    for _row_index, row in scores.iterrows():
        code = _normalise_code(row.get("code"))
        triggered_types: list[str] = []
        totals: dict[str, float] = {}
        diagnostic_totals: dict[str, float] = {}
        valid_payloads: dict[str, Mapping[str, Any]] = {}
        for type_key, weights in _AUDIT_WEIGHTS.items():
            payload = row.get(type_key)
            if not isinstance(payload, Mapping):
                errors.append(f"{code}:{type_key}: payload missing")
                continue
            sub_scores = payload.get("sub_scores")
            reasons = payload.get("reasons")
            if not isinstance(sub_scores, Mapping) or set(sub_scores) != set(weights):
                errors.append(f"{code}:{type_key}: sub-score keys differ")
                continue
            if not isinstance(reasons, Mapping) or set(weights) - set(reasons):
                errors.append(f"{code}:{type_key}: reasons missing")
                continue
            if any(not str(key).startswith("_") for key in set(reasons) - set(weights)):
                errors.append(f"{code}:{type_key}: unknown reason keys")
            clean: dict[str, float] = {}
            for key in weights:
                value = _finite(sub_scores[key])
                if value is None:
                    errors.append(f"{code}:{type_key}:{key}: non-numeric score")
                    break
                if not 0 <= value <= 10:
                    errors.append(f"{code}:{type_key}:{key}: score outside 0..10")
                    break
                clean[key] = value
            if len(clean) != len(weights):
                continue
            for key in weights:
                reason = reasons.get(key)
                if reason is None or reason is pd.NA or not str(reason).strip():
                    errors.append(f"{code}:{type_key}:{key}: reason missing")
                elif len(str(reason)) > _AUDIT_REASON_MAX_LENGTH:
                    errors.append(f"{code}:{type_key}:{key}: reason too long")
            total_decimals = 3 if type_key == "type7" else 1
            raw_total = round(sum(clean[key] * weight for key, weight in weights.items()), total_decimals)
            expected_total = min(raw_total, 4.9) if type_key == "type3" and clean["3e"] <= 3 else raw_total
            actual_total = _finite(payload.get("total"))
            if actual_total is None:
                errors.append(f"{code}:{type_key}: total is non-numeric")
                continue
            if not 0 <= actual_total <= 10 or not _close(actual_total, expected_total, rel_tol=0.0):
                errors.append(f"{code}:{type_key}: total differs from fixed weighted score")
            if type_key == "type3":
                downgrade = bool(reasons.get("_downgrade"))
                if clean["3e"] <= 3 and raw_total > 4.9 and not downgrade:
                    errors.append(f"{code}:{type_key}: active bubble cap lacks downgrade reason")
                if downgrade and clean["3e"] > 3:
                    errors.append(f"{code}:{type_key}: downgrade reason exists without bubble cap")
            veto = _strict_bool(payload.get("veto"))
            triggered = _strict_bool(payload.get("triggered"))
            if veto is None:
                errors.append(f"{code}:{type_key}: veto is not boolean")
                veto = bool(payload.get("veto"))
            if triggered is None:
                errors.append(f"{code}:{type_key}: triggered is not boolean")
                triggered = bool(payload.get("triggered"))
            reason_veto = bool(reasons.get("_veto"))
            if veto != reason_veto:
                errors.append(f"{code}:{type_key}: veto differs from _veto reason")
            status = payload.get("status")
            applicable = _strict_bool(payload.get("applicable"))
            evidence_complete = _strict_bool(payload.get("evidence_complete"))
            if status not in _AUDIT_TYPE_STATUSES or status != reasons.get("_status"):
                errors.append(f"{code}:{type_key}: invalid or inconsistent status")
            if type_key == "type5":
                errors.extend(_audit_type5_bottom_evidence_errors(code, row, payload))
            if type_key == "type7":
                errors.extend(
                    _audit_type7_ledger(
                        code,
                        payload.get("ledger"),
                        status,
                        patch4_bindings=(patch4_bindings.get(code) if isinstance(patch4_bindings, Mapping) else None),
                        source_row=(source_replay_index.get(code) if source_replay_scores is not None else None),
                        source_financial=financial_index.get(code),
                    )
                )
                ledger = payload.get("ledger")
                if status != "not_applicable" and isinstance(ledger, Mapping):
                    source_scores = ledger.get("scores")
                    if isinstance(source_scores, Mapping):
                        source_values = {key: _finite(source_scores.get(key)) for key in ("BM", "MOAT", "G")}
                        expected_type7_scores = (
                            {
                                "7a": round(source_values["BM"], 3),
                                "7b": round(source_values["MOAT"], 3),
                                "7c": round(source_values["G"], 3),
                            }
                            if all(value is not None for value in source_values.values())
                            else None
                        )
                        if clean != expected_type7_scores:
                            errors.append(f"{code}:type7: diagnostics differ from source ledgers")
                    if status != "blocked" and triggered is not ledger.get("triggered"):
                        errors.append(f"{code}:type7: trigger differs from source ledgers")
            if applicable is None or applicable != (status != "not_applicable"):
                errors.append(f"{code}:{type_key}: applicable differs from status")
            expected_evidence_complete = reasons.get("_evidence") == "complete"
            if evidence_complete is None or evidence_complete != expected_evidence_complete:
                errors.append(f"{code}:{type_key}: evidence_complete differs from status")
            if status in _AUDIT_NON_DIAGNOSTIC_STATUSES and (reason_veto or triggered):
                errors.append(f"{code}:{type_key}: N/A or missing evidence leaks veto/trigger")
            if triggered != (status == "triggered"):
                errors.append(f"{code}:{type_key}: triggered differs from explicit status")
            if status == "triggered" and (
                actual_total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or bool(reasons.get("_condition"))
            ):
                errors.append(f"{code}:{type_key}: triggered status violates threshold/veto/condition")
            if status == "conditional" and (
                actual_total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or not bool(reasons.get("_condition"))
            ):
                errors.append(f"{code}:{type_key}: conditional status lacks its required condition")
            if status == "vetoed" and not reason_veto:
                errors.append(f"{code}:{type_key}: vetoed status lacks veto reason")
            if status == "blocked" and not (reason_veto or str(reasons.get("_blocked") or "").strip()):
                errors.append(f"{code}:{type_key}: blocked status lacks a block reason")
            if status == "observe" and not (5.0 <= actual_total < _AUDIT_QUALIFY_THRESHOLD) and not reason_veto:
                errors.append(f"{code}:{type_key}: observe status is outside its score band")
            type7_decisive_failure = bool(
                type_key == "type7"
                and isinstance(payload.get("ledger"), Mapping)
                and payload["ledger"].get("decisively_not_triggered") is True
            )
            if status == "not_triggered" and actual_total >= 5.0 and not reason_veto and not type7_decisive_failure:
                errors.append(f"{code}:{type_key}: not_triggered status is inside observe/trigger band")
            totals[type_key] = actual_total
            if status not in _AUDIT_NON_DIAGNOSTIC_STATUSES:
                diagnostic_totals[type_key] = actual_total
            valid_payloads[type_key] = payload
            if triggered:
                triggered_types.append(type_key)
            type_score = _finite(row.get(f"{type_key}_score"))
            if type_score is None or not _close(type_score, actual_total, rel_tol=0.0):
                errors.append(f"{code}: {type_key}_score differs from payload total")

        declared = row.get("buy_types", [])
        expected_types = [key for key in _AUDIT_PRIORITY if key in triggered_types]
        if not isinstance(declared, list) or declared != expected_types:
            errors.append(f"{code}: buy_types order/content differs from triggered payloads")
        num_types = _finite(row.get("num_types"))
        if num_types is None or num_types != len(expected_types):
            errors.append(f"{code}: num_types mismatch")
        primary = _optional_text(row.get("primary_type"))
        expected_primary = expected_types[0] if expected_types else None
        if primary != expected_primary:
            errors.append(f"{code}: primary_type does not match fixed priority")
        expected_primary_label = _AUDIT_NAMES[expected_primary] if expected_primary else "无触发（不买）"
        if row.get("primary_label") != expected_primary_label:
            errors.append(f"{code}: primary_label mismatch")
        expected_diagnostic = None
        if diagnostic_totals:
            maximum = max(diagnostic_totals.values())
            expected_diagnostic = next(key for key in _AUDIT_PRIORITY if diagnostic_totals.get(key) == maximum)
            published_maximum = _finite(row.get("max_score"))
            if published_maximum is None:
                errors.append(f"{code}: max_score is non-numeric")
            elif not _close(published_maximum, maximum, rel_tol=0.0):
                errors.append(f"{code}: max_score is not the diagnostic maximum")
            if row.get("diagnostic_type") != expected_diagnostic:
                errors.append(f"{code}: diagnostic_type is not the highest-scoring framework")
            if row.get("diagnostic_label") != _AUDIT_NAMES[expected_diagnostic]:
                errors.append(f"{code}: diagnostic_label mismatch")
        else:
            if _finite(row.get("max_score")) is not None:
                errors.append(f"{code}: max_score must be empty without a diagnostic framework")
            if _optional_text(row.get("diagnostic_type")) is not None:
                errors.append(f"{code}: diagnostic_type must be empty without applicable evidence")
            if row.get("diagnostic_label") != "无可完整诊断框架":
                errors.append(f"{code}: diagnostic_label must identify the absence of an applicable framework")

        cases = row.get("bear_case")
        if expected_diagnostic is None:
            if cases != []:
                errors.append(f"{code}: bear_case must be empty without a diagnostic framework")
        elif not isinstance(cases, list) or len(cases) != 3:
            errors.append(f"{code}: bear_case must contain three entries")
        elif expected_diagnostic in valid_payloads:
            expected_cases = _audit_bear_case(expected_diagnostic, valid_payloads[expected_diagnostic])
            if cases != expected_cases:
                errors.append(f"{code}: bear_case differs from independent deterministic ranking")
            for case in cases:
                if not isinstance(case, Mapping) or set(case) != {"dimension", "score", "reason"}:
                    errors.append(f"{code}: bear_case entry structure invalid")
                    break
                score = _finite(case.get("score"))
                reason = case.get("reason")
                if score is None or not 0 <= score <= 10:
                    errors.append(f"{code}: bear_case score invalid")
                if reason is None or not str(reason).strip() or len(str(reason)) > _AUDIT_REASON_MAX_LENGTH:
                    errors.append(f"{code}: bear_case reason invalid")

    normalized_results: dict[str, Mapping[str, Any]] = {}
    for raw_code, result in dcf_results.items():
        code = _normalise_code(raw_code)
        if code in normalized_results:
            errors.append(f"independent: duplicate valuation code {code}")
        if isinstance(result, Mapping):
            normalized_results[code] = result
    normalized_skips: dict[str, str] = {}
    for raw_code, reason in skip_reasons.items():
        code = _normalise_code(raw_code)
        if code in normalized_skips:
            errors.append(f"independent: duplicate skip code {code}")
        normalized_skips[code] = str(reason or "").strip()
    normalized_skip_classifications: dict[str, dict[str, str]] = {}
    for raw_code, value in (skip_classifications or {}).items():
        code = _normalise_code(raw_code)
        classification = normalize_dcf_skip_classification(value)
        if code in normalized_skip_classifications:
            errors.append(f"independent: duplicate skip classification code {code}")
        if classification is None:
            errors.append(f"{code}: invalid structured valuation skip classification")
            continue
        normalized_skip_classifications[code] = classification
    sampled_set = set(sampled)
    if set(normalized_results) - sampled_set:
        errors.append("independent: valuation results contain non-sampled codes")
    if set(normalized_skips) - sampled_set:
        errors.append("independent: skip reasons contain non-sampled codes")
    if set(normalized_skip_classifications) != set(normalized_skips):
        errors.append("independent: skip classification identities differ from skip reasons")
    for code in set(normalized_skip_classifications) & set(normalized_skips):
        if normalized_skip_classifications[code]["reason"] != normalized_skips[code]:
            errors.append(f"{code}: structured skip reason differs from legacy skip reason")
    expected_type1_1a: dict[str, float] = {}
    expected_nonfinancial_type1_1a: dict[str, float] = {}
    for code in sampled:
        result = normalized_results.get(code)
        if result is None:
            if not normalized_skips.get(code):
                errors.append(f"{code}: skipped valuation has no structured reason")
            continue
        if normalized_skips.get(code):
            errors.append(f"{code}: valuation result and skip reason both present")
        matches = scores.loc[scores["code"].map(_normalise_code) == code]
        if len(matches) != 1:
            continue
        if quotes is not None and code not in quote_index:
            errors.append(f"{code}: sampled company is absent from quote evidence")
        if financials is not None and code not in financial_index:
            errors.append(f"{code}: sampled company is absent from financial evidence")
        valuation_errors = _valuation_contract_errors(
            code,
            matches.iloc[0],
            result,
            quote=quote_index.get(code),
            financial=financial_index.get(code),
            reporting_period_contract=reporting_period_contract,
        )
        errors.extend(valuation_errors)
        if not valuation_errors:
            expected_score = _expected_type1_1a_from_dcf(result)
            if expected_score is None:
                errors.append(f"{code}: cannot replay Type 1 1a from validated DCF")
            else:
                expected_type1_1a[code] = expected_score
                if result.get("_pb_valuation") is not True:
                    expected_nonfinancial_type1_1a[code] = expected_score
    for _row_index, row in scores.iterrows():
        code = _normalise_code(row.get("code"))
        errors.extend(
            _type1_valuation_binding_errors(
                code,
                row,
                expected_type1_1a=expected_type1_1a.get(code),
                skip_classification=(
                    normalized_skip_classifications.get(code) if code not in expected_type1_1a else None
                ),
            )
        )
        errors.extend(
            _type7_valuation_binding_errors(
                code,
                row,
                expected_type1_1a=expected_nonfinancial_type1_1a.get(code),
            )
        )
    return tuple(errors)


def _scoring_replay_checks(
    published: pd.DataFrame,
    replayed: pd.DataFrame,
    sampled: tuple[str, ...],
) -> tuple[str, ...]:
    """Compare every published field with a reordered full-universe replay.

    This is deliberately labelled same-source: it proves deterministic raw
    input-to-subscore reproduction and catches payload corruption, while the
    independent checker and rule mutation tests retain their separate roles.
    """
    errors: list[str] = []
    if "code" not in published or "code" not in replayed:
        return ("scoring replay: missing code column",)
    published_index = {_normalise_code(row.get("code")): row for row in published.to_dict(orient="records")}
    replay_index = {_normalise_code(row.get("code")): row for row in replayed.to_dict(orient="records")}
    if set(published_index) != set(sampled):
        errors.append("scoring replay: published sampled identities differ")
    missing = sorted(set(sampled) - set(replay_index))
    if missing:
        errors.append(f"scoring replay: missing sampled companies {missing[:5]}")
    for code in sampled:
        if code not in published_index or code not in replay_index:
            continue
        if _canonical_json(published_index[code]) != _canonical_json(replay_index[code]):
            errors.append(f"{code}: every-field scoring replay differs from published result")
    return tuple(errors)


def _valuation_replay_checks(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    sampled: tuple[str, ...],
    published_results: Mapping[str, Mapping[str, Any]],
    published_skips: Mapping[str, str],
    published_issues: tuple[PipelineIssue, ...],
    *,
    published_skip_classifications: Mapping[str, Mapping[str, str]] | None = None,
    max_workers: int,
    reporting_period_contract: ReportingPeriodContract,
) -> tuple[str, ...]:
    """Same-source replay of valuation existence, payload, skip reason and issues."""
    replay = compute_dcf_batch(
        quotes,
        financials,
        eligible_codes=sampled,
        max_workers=max_workers,
        reporting_period_contract=reporting_period_contract,
    )
    errors: list[str] = []
    replay_results = {_normalise_code(code): result for code, result in replay.results.items()}
    replay_skips = {_normalise_code(code): str(reason) for code, reason in replay.skip_reasons.items()}
    replay_skip_classifications = {
        _normalise_code(code): classification for code, classification in replay.skip_classifications.items()
    }
    published_results = {_normalise_code(code): result for code, result in published_results.items()}
    published_skips = {_normalise_code(code): str(reason) for code, reason in published_skips.items()}
    published_skip_classifications = {
        _normalise_code(code): classification for code, classification in (published_skip_classifications or {}).items()
    }
    if set(replay_results) != set(published_results):
        errors.append("valuation replay: valid-result identities differ from published sample")
    if set(replay_skips) != set(published_skips):
        errors.append("valuation replay: skipped identities differ from published sample")
    if _canonical_json(replay_skip_classifications) != _canonical_json(published_skip_classifications):
        errors.append("valuation replay: structured skip classifications differ from published sample")
    for code in sampled:
        if code in replay_results and code in published_results:
            if _canonical_json(replay_results[code]) != _canonical_json(published_results[code]):
                errors.append(f"{code}: valuation replay payload differs from published result")
        if code in replay_skips and code in published_skips and replay_skips[code] != published_skips[code]:
            errors.append(f"{code}: valuation replay skip reason differs from published reason")
    replay_issues = sorted(
        (issue.code, issue.stage, issue.message)
        for issue in replay.issues
        if _normalise_code(issue.code) in set(sampled)
    )
    expected_issues = sorted((issue.code, issue.stage, issue.message) for issue in published_issues)
    if replay_issues != expected_issues:
        errors.append("valuation replay: pipeline issues differ from published sample")
    if replay.attempted != len(sampled) or len(replay_results) + replay.skipped != replay.attempted:
        errors.append("valuation replay: attempted/valid/skipped accounting mismatch")
    return tuple(errors)


def audit_random_sample(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    eligible_codes: Iterable[Any],
    seed: int = 20260715,
    sample_size: int = 100,
    max_workers: int = 8,
    snapshot_sha256: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    full_market_analysis: MarketAnalysisOutcome | None = None,
    reporting_period_contract: ReportingPeriodContract | None = None,
    market_coldness_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    quality_history_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    type3_growth_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    research_report_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    patch4_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> RandomSampleAudit:
    """Audit a fixed seed while preserving the production full-market benchmarks."""
    if not _audit_valid_reporting_period_contract(reporting_period_contract):
        raise ValueError("a valid reporting_period_contract is required for a production audit")
    if market_coldness_evidence is not None and not isinstance(market_coldness_evidence, Mapping):
        raise ValueError("market_coldness_evidence must be a mapping or None")
    if quality_history_evidence is not None and not isinstance(quality_history_evidence, Mapping):
        raise ValueError("quality_history_evidence must be a mapping or None")
    if type3_growth_evidence is not None and not isinstance(type3_growth_evidence, Mapping):
        raise ValueError("type3_growth_evidence must be a mapping or None")
    if research_report_evidence is not None and not isinstance(research_report_evidence, Mapping):
        raise ValueError("research_report_evidence must be a mapping or None")
    if patch4_evidence is not None and not isinstance(patch4_evidence, Mapping):
        raise ValueError("patch4_evidence must be a mapping or None")
    if not isinstance(quotes, pd.DataFrame) or not {"code", "market"} <= set(quotes.columns):
        raise ValueError("quotes must contain code and market columns")
    if not isinstance(financials, Mapping):
        raise ValueError("financials must be a mapping")

    quote_codes = quotes["code"].map(_normalise_code)
    duplicated = set(quote_codes[quote_codes.duplicated(keep=False)])
    if duplicated:
        raise ValueError(f"ambiguous quote codes: {sorted(duplicated)[:5]}")
    quote_index = {code: index for index, code in quote_codes.items()}

    financial_index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for raw_code, company in financials.items():
        code = _normalise_code(raw_code)
        if code in financial_index:
            raise ValueError(f"ambiguous financial code: {code}")
        if isinstance(company, Mapping):
            financial_index[code] = (str(raw_code), company)

    requested_eligible = {_normalise_code(code) for code in eligible_codes if _normalise_code(code)}
    if not requested_eligible:
        raise ValueError("eligible_codes must contain at least one validated company")
    available = set(quote_index) & set(financial_index)
    unknown = sorted(requested_eligible - available)
    if unknown:
        raise ValueError(f"eligible_codes are absent from quote/financial inputs: {unknown[:5]}")
    eligible = sorted(
        code
        for code in requested_eligible
        if str(quotes.at[quote_index[code], "market"]).strip().upper() in {"SH", "SZ"}
    )
    if not eligible:
        raise ValueError("eligible_codes must contain at least one SH/SZ company")
    count = int(sample_size)
    if count <= 0 or count > len(eligible):
        raise ValueError(f"sample_size must be between 1 and {len(eligible)}")
    # Deterministic reproducibility is the contract; this is not a secret or
    # adversarial lottery and therefore does not require cryptographic entropy.
    sampled = tuple(sorted(random.Random(int(seed)).sample(eligible, count)))  # nosec B311
    eligible_quotes = quotes.loc[[quote_index[code] for code in eligible]].copy()
    eligible_financials = {code: financial_index[code][1] for code in eligible}

    if full_market_analysis is None:
        analysis: MarketAnalysisOutcome = run_market_analysis(
            eligible_quotes,
            eligible_financials,
            eligible_codes=eligible,
            max_workers=max_workers,
            enforce_quality=True,
            expected_companies=len(eligible),
            reporting_period_contract=reporting_period_contract,
            market_coldness_evidence=market_coldness_evidence,
            quality_history_evidence=quality_history_evidence,
            type3_growth_evidence=type3_growth_evidence,
            research_report_evidence=research_report_evidence,
            patch4_evidence=patch4_evidence,
        )
    elif isinstance(full_market_analysis, MarketAnalysisOutcome):
        analysis = full_market_analysis
    else:
        raise TypeError("full_market_analysis must be a MarketAnalysisOutcome")

    if not isinstance(analysis.scores, pd.DataFrame) or "code" not in analysis.scores:
        raise ValueError("full-market analysis scores must contain a code column")
    full_score_codes = analysis.scores["code"].map(_normalise_code)
    if full_score_codes.duplicated().any() or set(full_score_codes) != set(eligible):
        raise ValueError("full-market analysis score identities differ from eligible_codes")
    try:
        revalidated_quality = validate_market_analysis_quality(
            analysis,
            expected_companies=len(eligible),
            expected_codes=eligible,
        )
    except (AnalysisQualityError, TypeError, ValueError) as exc:
        raise ValueError(f"full-market analysis failed quality revalidation: {exc}") from exc
    if _canonical_json(analysis.quality) != _canonical_json(revalidated_quality):
        raise ValueError("full-market analysis quality metadata differs from recomputed metrics")
    sample_mask = full_score_codes.isin(sampled)
    sampled_scores = analysis.scores.loc[sample_mask].copy().sort_values("code", kind="stable")
    normalized_results = {_normalise_code(code): result for code, result in analysis.dcf_results.items()}
    sampled_results = {code: normalized_results[code] for code in sampled if code in normalized_results}
    full_skip_reasons = _analysis_skip_reasons(analysis)
    sampled_skip_reasons = {code: full_skip_reasons[code] for code in sampled if code in full_skip_reasons}
    full_skip_classifications = _analysis_skip_classifications(analysis)
    captured_quality_history = (
        quality_history_evidence
        if quality_history_evidence is not None
        else getattr(analysis, "quality_history_evidence", {})
    )
    if not isinstance(captured_quality_history, Mapping):
        raise ValueError("full-market Type 7 quality history evidence is invalid")
    captured_research_reports = (
        research_report_evidence
        if research_report_evidence is not None
        else getattr(analysis, "research_report_evidence", {})
    )
    if not isinstance(captured_research_reports, Mapping):
        raise ValueError("full-market Type 7 research report evidence is invalid")
    captured_type3_growth = (
        type3_growth_evidence if type3_growth_evidence is not None else getattr(analysis, "type3_growth_evidence", {})
    )
    if not isinstance(captured_type3_growth, Mapping):
        raise ValueError("full-market Type 3 growth evidence is invalid")
    captured_patch4 = patch4_evidence if patch4_evidence is not None else getattr(analysis, "patch4_evidence", {})
    if not isinstance(captured_patch4, Mapping):
        raise ValueError("full-market Type 7 Patch 4 evidence is invalid")
    captured_patch4_bindings = _patch4_captured_binding_index(captured_patch4)
    sampled_skip_classifications = {
        code: full_skip_classifications[code] for code in sampled if code in full_skip_classifications
    }
    sampled_issues = tuple(issue for issue in analysis.issues if _normalise_code(issue.code) in set(sampled))

    valuation_replay_errors = _valuation_replay_checks(
        quotes.loc[[quote_index[code] for code in sampled]].copy(),
        {code: financial_index[code][1] for code in sampled},
        sampled,
        sampled_results,
        sampled_skip_reasons,
        sampled_issues,
        published_skip_classifications=sampled_skip_classifications,
        max_workers=max_workers,
        reporting_period_contract=reporting_period_contract,
    )

    # The engine's validator is retained and labelled as same-source.  A
    # separate implementation below recomputes the public contracts and DCF
    # ordering, so the report never presents self-validation as independent.
    engine_errors = tuple(validate_screening_result(sampled_scores))
    replayed_scores = _production_screener.screen_all_types(
        dict(reversed(list(eligible_financials.items()))),
        eligible_quotes.iloc[::-1].reset_index(drop=True),
        dcf_results=analysis.dcf_results,
        market_coldness_evidence=market_coldness_evidence,
        dcf_skip_classifications=full_skip_classifications,
        quality_history_evidence=captured_quality_history,
        type3_growth_evidence=captured_type3_growth,
        research_report_evidence=captured_research_reports,
        patch4_evidence=captured_patch4,
        output_codes=sampled,
    )
    replayed_sample = replayed_scores.loc[replayed_scores["code"].map(_normalise_code).isin(sampled)].copy()
    scoring_replay_errors = _scoring_replay_checks(sampled_scores, replayed_sample, sampled)
    expected_interim_report_date = None
    if isinstance(provenance, Mapping):
        validation_metadata = provenance.get("validation")
        if isinstance(validation_metadata, Mapping):
            value = validation_metadata.get("expected_interim_report_date")
            if isinstance(value, str) and value:
                expected_interim_report_date = value
    independent_errors = _independent_checks(
        sampled_scores,
        sampled,
        sampled_results,
        sampled_skip_reasons,
        skip_classifications=sampled_skip_classifications,
        quotes=quotes,
        financials=financials,
        expected_interim_report_date=expected_interim_report_date,
        reporting_period_contract=reporting_period_contract,
        patch4_bindings=captured_patch4_bindings,
        source_replay_scores=replayed_sample,
    )
    analysis_quality = getattr(analysis, "quality", {})
    if not isinstance(analysis_quality, Mapping):
        analysis_quality = {}
    audit_provenance = _build_provenance(
        quotes,
        financials,
        tuple(eligible),
        snapshot_sha256=snapshot_sha256,
        metadata=provenance,
    )
    audit_provenance["reporting_period_contract"] = {
        "annual_report_date": reporting_period_contract.annual_report_date,
        "current_interim_report_date": reporting_period_contract.current_interim_report_date,
        "prior_interim_report_date": reporting_period_contract.prior_interim_report_date,
        "period_basis": _AUDIT_TTM_PERIOD_BASIS,
    }
    audit_provenance["market_coldness_evidence"] = _market_coldness_evidence_provenance(
        market_coldness_evidence,
        tuple(eligible),
    )
    audit_provenance["quality_history_evidence"] = _quality_history_evidence_provenance(
        captured_quality_history,
        tuple(eligible),
    )
    audit_provenance["type3_growth_evidence"] = _type3_growth_evidence_provenance(
        captured_type3_growth,
        tuple(eligible),
    )
    audit_provenance["research_report_evidence"] = _research_report_evidence_provenance(
        captured_research_reports,
        tuple(eligible),
    )
    audit_provenance["patch4_evidence"] = _patch4_evidence_provenance(
        captured_patch4,
        tuple(eligible),
    )

    return RandomSampleAudit(
        int(seed),
        count,
        sampled,
        sampled_scores,
        sampled_results,
        sampled_skip_reasons,
        sampled_skip_classifications,
        sampled_issues,
        engine_errors,
        scoring_replay_errors,
        valuation_replay_errors,
        independent_errors,
        dict(analysis_quality),
        audit_provenance,
        len(eligible),
    )


def _markdown_cell(value: Any) -> str:
    safe = _json_safe(value)
    return str(safe if safe is not None else "").replace("|", "\\|").replace("\n", " ")


def render_audit_markdown(audit: RandomSampleAudit, *, data_timestamp: float) -> str:
    """Render a complete, human-readable summary with all sampled companies."""
    timestamp = datetime.fromtimestamp(float(data_timestamp), tz=timezone.utc).isoformat()
    signal_counts = {
        type_key: int(audit.scores[type_key].apply(lambda value: bool(value.get("triggered"))).sum())
        for type_key in _AUDIT_WEIGHTS
    }
    provenance = audit.provenance
    lines = [
        f"# 固定随机 {audit.sample_size} 家公司审计",
        "",
        f"- seed: `{audit.seed}`",
        f"- sample_size: `{audit.sample_size}`",
        f"- eligible_universe_size: `{audit.eligible_universe_size}`",
        f"- data_timestamp_utc: `{timestamp}`",
        f"- dcf_valid: `{len(audit.dcf_results)}`",
        f"- dcf_skipped_with_reason: `{len(audit.dcf_skip_reasons)}`",
        f"- pipeline_issues: `{len(audit.pipeline_issues)}`",
        f"- engine_self_check_errors: `{len(audit.engine_invariant_errors)}`",
        f"- same_source_scoring_replay_errors: `{len(audit.scoring_replay_errors)}`",
        f"- same_source_valuation_replay_errors: `{len(audit.valuation_replay_errors)}`",
        f"- independent_check_errors: `{len(audit.independent_errors)}`",
        f"- triggered_by_type: `{signal_counts}`",
        f"- snapshot_content_sha256: `{provenance.get('snapshot_content_sha256')}`",
        f"- snapshot_artifact_sha256: `{provenance.get('snapshot_artifact_sha256')}`",
        f"- code_sha256: `{provenance.get('code_sha256')}`",
        f"- rules_sha256: `{provenance.get('rules_sha256')}`",
        f"- dependency_manifest_sha256: `{provenance.get('dependency_manifest_sha256')}`",
        f"- industry_sha256: `{provenance.get('industry_sha256')}`",
        f"- patch6_source: `{provenance.get('patch6_source')}`",
        f"- type7_source_documents: `{provenance.get('type7_source_documents')}`",
        f"- risk_parameter_sources: `{provenance.get('risk_parameter_sources')}`",
        f"- scoring_verification_scope: `{provenance.get('scoring_verification_scope')}`",
        f"- git: `{provenance.get('git')}`",
        "",
    ]
    if audit.pipeline_issues:
        lines.extend(["## 管道问题", ""])
        lines.extend(
            f"- `{issue.code}` / `{issue.stage}`: {_markdown_cell(issue.message)}" for issue in audit.pipeline_issues
        )
        lines.append("")
    if audit.engine_invariant_errors:
        lines.extend(["## 引擎同源自检错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.engine_invariant_errors)
        lines.append("")
    if audit.scoring_replay_errors:
        lines.extend(["## 全字段评分同源重放错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.scoring_replay_errors)
        lines.append("")
    if audit.valuation_replay_errors:
        lines.extend(["## 估值同源重放错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.valuation_replay_errors)
        lines.append("")
    if audit.independent_errors:
        lines.extend(["## 独立重算错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.independent_errors)
        lines.append("")

    lines.extend(
        [
            "## 公司明细",
            "",
            "| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |",
            "|---|---|---|---|---|---:|---|---|---|",
        ]
    )
    for _, row in audit.scores.sort_values("code", kind="stable").iterrows():
        code = _normalise_code(row.get("code"))
        cases = row.get("bear_case", [])
        bear_text = "；".join(
            f"{case.get('dimension')} {case.get('score')}分:{case.get('reason')}"
            for case in cases
            if isinstance(case, Mapping)
        )
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    code,
                    row.get("name", ""),
                    row.get("industry", ""),
                    row.get("primary_label", ""),
                    row.get("diagnostic_label", ""),
                    row.get("max_score", ""),
                    ",".join(row.get("buy_types", [])),
                    (
                        "有效"
                        if code in audit.dcf_results
                        else f"跳过:{audit.dcf_skip_reasons.get(code, '无结构化原因')}"
                    ),
                    bear_text,
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_safe(value: Any) -> Any:
    """Recursively replace pandas/NumPy non-finite scalars with JSON null."""
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _json_safe(converted)
    return value


def _spreadsheet_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    safe = frame.copy()

    def neutralise(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if value[:1] in {"\t", "\r", "\n"} or stripped[:1] in {"=", "+", "-", "@"}:
            return "'" + value
        return value

    for column in safe.columns:
        safe[column] = safe[column].map(neutralise)
    return safe


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _flat_dcf_record(audit: RandomSampleAudit, code: str) -> dict[str, Any]:
    result = audit.dcf_results.get(code)
    if not isinstance(result, Mapping):
        classification = audit.dcf_skip_classifications.get(code, {})
        return {
            "DCF状态": "跳过",
            "DCF跳过分类": classification.get("category") if isinstance(classification, Mapping) else None,
            "DCF跳过原因": audit.dcf_skip_reasons.get(code, "无结构化原因"),
            "DCF区域": None,
            "DCF当前价": None,
            "DCF买入区上界": None,
            "DCF卖出区下界": None,
            "DCF安全边际%": None,
            "DCF基础WACC": None,
            "DCF基础FCF": None,
            "DCF参数JSON": "{}",
            "DCF_pessimistic_lower": None,
            "DCF_pessimistic_upper": None,
            "DCF_neutral_lower": None,
            "DCF_neutral_upper": None,
            "DCF_optimistic_lower": None,
            "DCF_optimistic_upper": None,
        }
    record: dict[str, Any] = {
        "DCF状态": "有效",
        "DCF跳过分类": "",
        "DCF跳过原因": "",
        "DCF区域": result.get("zone"),
        "DCF当前价": result.get("current_price"),
        "DCF买入区上界": result.get("buy_zone_upper"),
        "DCF卖出区下界": result.get("sell_zone_lower"),
        "DCF安全边际%": result.get("safety_margin_pct"),
        "DCF基础WACC": result.get("base_wacc"),
        "DCF基础FCF": result.get("base_fcf"),
        "DCF参数JSON": json.dumps(
            _json_safe(result.get("params", {})),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        ),
    }
    points = result.get("dcf_points", {})
    for scenario in ("pessimistic", "neutral", "optimistic"):
        band = points.get(scenario, {}) if isinstance(points, Mapping) else {}
        record[f"DCF_{scenario}_lower"] = band.get("lower") if isinstance(band, Mapping) else None
        record[f"DCF_{scenario}_upper"] = band.get("upper") if isinstance(band, Mapping) else None
    return record


def write_audit_artifacts(
    audit: RandomSampleAudit,
    output_dir: str | os.PathLike[str],
    *,
    data_timestamp: float,
) -> Mapping[str, Path]:
    """Write detailed JSON, flat CSV and human-readable Markdown atomically."""
    destination = Path(output_dir)
    stem = f"random{audit.sample_size}_audit_seed{audit.seed}"
    paths = {
        "json": destination / f"{stem}.json",
        "csv": destination / f"{stem}.csv",
        "markdown": destination / f"{stem}.md",
    }
    timestamp = datetime.fromtimestamp(float(data_timestamp), tz=timezone.utc).isoformat()
    payload = {
        "seed": audit.seed,
        "sample_size": audit.sample_size,
        "sample_codes": list(audit.sample_codes),
        "data_timestamp_utc": timestamp,
        "dcf_valid": len(audit.dcf_results),
        "eligible_universe_size": audit.eligible_universe_size,
        "provenance": dict(audit.provenance),
        "analysis_quality": dict(audit.analysis_quality),
        "dcf_results": dict(audit.dcf_results),
        "dcf_skip_reasons": dict(audit.dcf_skip_reasons),
        "dcf_skip_classifications": dict(audit.dcf_skip_classifications),
        "pipeline_issues": [
            {"code": issue.code, "stage": issue.stage, "message": issue.message} for issue in audit.pipeline_issues
        ],
        "engine_self_check_errors": list(audit.engine_invariant_errors),
        "same_source_scoring_replay_errors": list(audit.scoring_replay_errors),
        "same_source_valuation_replay_errors": list(audit.valuation_replay_errors),
        "independent_check_errors": list(audit.independent_errors),
        "invariant_errors": list(audit.invariant_errors),
        "companies": audit.scores.sort_values("code", kind="stable").to_dict(orient="records"),
    }
    flat_rows: list[dict[str, Any]] = []
    for _, row in audit.scores.sort_values("code", kind="stable").iterrows():
        code = _normalise_code(row.get("code"))
        record = {
            "代码": code,
            "名称": row.get("name"),
            "行业": row.get("industry"),
            "买入判定": row.get("primary_label"),
            "诊断框架": row.get("diagnostic_label"),
            "最高分": row.get("max_score"),
            "触发类型": ",".join(row.get("buy_types", [])),
            "DCF有效": code in audit.dcf_results,
            "空头漏洞": "；".join(
                f"{case.get('dimension')} {case.get('score')}分:{case.get('reason')}"
                for case in row.get("bear_case", [])
                if isinstance(case, Mapping)
            ),
        }
        for type_key in _AUDIT_WEIGHTS:
            payload_for_type = row.get(type_key, {})
            record[f"{type_key}总分"] = payload_for_type.get("total")
            record[f"{type_key}触发"] = bool(payload_for_type.get("triggered"))
            record[f"{type_key}否决"] = bool(payload_for_type.get("veto"))
            sub_scores = payload_for_type.get("sub_scores", {})
            reasons = payload_for_type.get("reasons", {})
            for dimension in _AUDIT_WEIGHTS[type_key]:
                record[f"{dimension}子分"] = sub_scores.get(dimension)
                record[f"{dimension}依据"] = reasons.get(dimension)
            record[f"{type_key}元信息JSON"] = json.dumps(
                {key: value for key, value in reasons.items() if str(key).startswith("_")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=_json_default,
            )
        record.update(_flat_dcf_record(audit, code))
        flat_rows.append(record)
    csv_text = _spreadsheet_safe_frame(pd.DataFrame(flat_rows)).to_csv(index=False, lineterminator="\n")
    markdown_text = render_audit_markdown(audit, data_timestamp=data_timestamp)
    payload["companion_artifacts_sha256"] = {
        "csv": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
        "markdown": hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
    }

    # Write the JSON manifest last.  An interrupted replacement can therefore
    # only leave mismatched companions, which the release verifier rejects.
    _atomic_write_text(paths["csv"], csv_text)
    _atomic_write_text(paths["markdown"], markdown_text)
    _atomic_write_text(
        paths["json"],
        json.dumps(
            _json_safe(payload),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=_json_default,
        )
        + "\n",
    )
    return paths
