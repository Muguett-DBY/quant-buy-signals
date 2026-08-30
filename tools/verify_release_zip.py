"""Fail closed when a DS_DCF desktop source archive contains unsafe or stale files."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import stat
import statistics

# Fixed-argv local Git provenance checks only; no shell is involved.
import subprocess  # nosec B404
import tomllib
from typing import Any
import unicodedata
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from data.as_of import shanghai_today
from engine.quantitative_evidence import (
    MODEL_ID as QUANTITATIVE_EVIDENCE_MODEL_ID,
    validate_quantitative_evidence_record,
)
from engine.audit import _independent_type7_ledger_errors
from engine.type7_patch6 import (
    MODEL_ID as PATCH6_TYPE7_MODEL_ID,
    SCHEMA_VERSION as PATCH6_TYPE7_SCHEMA_VERSION,
    validate_patch6_type7_ledger,
)
from tools.run_full_audit import (
    _canonical_market_coldness_json,
    _replay_market_coldness_reference_artifact,
)


_FORBIDDEN_PATH = re.compile(
    r"(?:^|/)(?:\.git|\.reasonix|\.agents|\.codex|\.playwright-cli|__pycache__|\.pytest_cache|\.ruff_cache|"
    r"\.mypy_cache|\.venv|venv|dist|build|htmlcov|[^/]+\.egg-info)(?:/|$)",
    re.IGNORECASE,
)
_FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pkl",
    ".pickle",
    ".parquet",
    ".log",
    ".tmp",
    ".bak",
    ".zip",
    ".7z",
    ".pem",
    ".key",
    ".der",
    ".pk8",
    ".pkcs8",
    ".p8",
    ".ppk",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)
_LF_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".scss",
    ".html",
    ".sh",
    ".ps1",
    ".md",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".xml",
    ".csv",
)
_LF_NAMES = {".gitignore", ".gitattributes", "license"}
_REQUIRED_FILES = {
    "LICENSE",
    "README.md",
    "run.bat",
    ".streamlit/config.toml",
    "app.py",
    "config.py",
    "data/cache.py",
    "data/baostock_valuation.py",
    "data/capex_evidence.py",
    "data/commodity_evidence.py",
    "data/datacenter.py",
    "data/financial_indicator_evidence.py",
    "data/financial_source_evidence.py",
    "data/financial_balance_sheet_evidence.json",
    "data/financial_zero_capex_evidence.json",
    "data/financial_zero_revenue_evidence.json",
    "data/fetcher.py",
    "data/exchange_financials.py",
    "data/growth_evidence.py",
    "data/industry.py",
    "data/industry_f10.json",
    "data/industry_em_map.json",
    "data/industry_capco_2025h2.json",
    "data/industry_exchange_new_listings_2026.json",
    "data/investor_relations.py",
    "data/market_coldness.py",
    "data/market_history.py",
    "data/nbs_commodity_evidence.py",
    "data/patch4_evidence.py",
    "data/quality_history.py",
    "data/research_reports.py",
    "data/snapshot.py",
    "data/sina_financial.py",
    "data/shenwan_industry_history.py",
    "data/trading_calendar.py",
    "engine/audit.py",
    "engine/buy_screener.py",
    "engine/dcf.py",
    "engine/market_coldness.py",
    "engine/pipeline.py",
    "engine/quantitative_evidence.py",
    "engine/quality_equity.py",
    "engine/risk.py",
    "engine/scenarios.py",
    "engine/type7_patch6.py",
    "engine/valuation_status.py",
    "ui/buy_types_page.py",
    "ui/leaders_page.py",
    "tools/__init__.py",
    "tools/build_official_industry_source.py",
    "tools/audit_shenwan_industry_history.py",
    "tools/build_desktop.py",
    "tools/china_a_share_trading_calendar.json",
    "tools/run_full_audit.py",
    "tools/sign_desktop_update_manifest.ps1",
    "tools/verify_release_zip.py",
    "desktop/__init__.py",
    "desktop/launcher.py",
    "desktop/installer.py",
    "desktop/updater.py",
    "desktop/version.py",
    "desktop/update_config.json",
    "desktop/version_info.txt",
    "desktop/DS_DCF.spec",
    "desktop/DS_DCF_Installer.spec",
    "requirements-bootstrap.txt",
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-test.txt",
    "requirements-dev.txt",
    "requirements-dev-lock.txt",
    "pyproject.toml",
    "audit/random100_audit_seed20260715.json",
    "audit/random100_audit_seed20260715.csv",
    "audit/random100_audit_seed20260715.md",
}
_AUDIT_JSON_PATH = "audit/random100_audit_seed20260715.json"
_AUDIT_CSV_PATH = "audit/random100_audit_seed20260715.csv"
_AUDIT_MARKDOWN_PATH = "audit/random100_audit_seed20260715.md"
_RELEASE_AUDIT_PATHS = {_AUDIT_JSON_PATH, _AUDIT_CSV_PATH, _AUDIT_MARKDOWN_PATH}
_EXPECTED_AUDIT_SEED = 20260715
_EXPECTED_PROJECT_LICENSE = "LicenseRef-PolyForm-Noncommercial-1.0.0"
_EXPECTED_LICENSE_SHA256 = "a7106a6f8ee245b6e8b0482b8eab8c874a8a40819c8718c92180e0ef3dad596c"
_EXPECTED_UPDATE_MANIFEST_URL = (
    "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/windows-app/update-manifest.json"
)
_EXPECTED_PATCH6_SOURCE = {
    "path_at_model_authoring": r"E:\模板汇总MD\补丁6· 公司三属性分类与三维度量化打分机制.md",
    "sha256": "dfade9961a182bfff67f95e2f8d55fd637cf8a15cedd44c12300b4f9c4c1549b",
}
_EXPECTED_PATCH7_SOURCE = {
    "path_at_model_authoring": r"E:\模板汇总MD\补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md",
    "sha256": "69b6bbeaa44755b9935518c665bc1ac0cac5c473aaba5b106bdf0f9fc88beb6d",
}
_EXPECTED_TYPE7_SOURCE_DOCUMENTS = {
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
    "patch6": dict(_EXPECTED_PATCH6_SOURCE),
    "patch7": dict(_EXPECTED_PATCH7_SOURCE),
    "subsequent_addenda": {
        "path_at_model_authoring": r"E:\模板汇总MD\后续附加补丁们.md",
        "sha256": "0dea9125bbe2039acf741ac997e62b53c49b6e3dc32e7d956ed96f9d7054b64f",
    },
}
_EXPECTED_DIRECT_DEPENDENCIES = {
    "baostock",
    "cryptography",
    "defusedxml",
    "numpy",
    "orjson",
    "pandas",
    "plotly",
    "pillow",
    "requests",
    "streamlit",
    "xlrd",
    "gitpython",
}
_SSH_PRIVATE_KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_AUDIT_TYPE_WEIGHTS = {
    "type1": {"1a": 0.30, "1b": 0.35, "1c": 0.20, "1d": 0.15},
    "type2": {"2a": 0.25, "2b": 0.30, "2c": 0.25, "2d": 0.20},
    "type3": {"3a": 0.25, "3b": 0.20, "3c": 0.20, "3d": 0.25, "3e": 0.10},
    "type4": {"4a": 0.25, "4b": 0.25, "4c": 0.20, "4d": 0.15, "4e": 0.08, "4f": 0.07},
    "type5": {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10},
    "type6": {"6a": 0.25, "6b": 0.20, "6c": 0.15, "6d": 0.25, "6e": 0.15},
    "type7": {"7a": 1.0 / 3.0, "7b": 1.0 / 3.0, "7c": 1.0 / 3.0},
}
_AUDIT_TYPE_DIMENSIONS = {type_key: set(weights) for type_key, weights in _AUDIT_TYPE_WEIGHTS.items()}
_AUDIT_QUANTITATIVE_EVIDENCE_KEYS = frozenset(
    {
        "industry_durability_score",
        "accounting_integrity_score",
        "management_alignment_score",
        "moat_score",
        "moat_durability_score",
        "growth_quality_score",
        "growth_sustainability_score",
        "runway_score",
        "industry_bubble_score",
        "type3_bubble_score",
        "catalyst_score",
        "technology_score",
        "business_model_score",
    }
)
_AUDIT_QUANTITATIVE_LEVELS = frozenset({"primary", "derived_proxy", "partial", "missing", "not_applicable"})
_AUDIT_COMPLETE_QUANTITATIVE_LEVELS = frozenset({"primary", "derived_proxy", "not_applicable"})
_AUDIT_SCORED_QUANTITATIVE_LEVELS = frozenset({"primary", "derived_proxy"})
_AUDIT_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "model_id",
        "decision_complete",
        "decision_basis",
        "score_lower_bound",
        "score_upper_bound",
        "veto_state",
        "potentially_triggerable",
        "missing_dimensions",
    }
)
_AUDIT_DECISION_MISSING_REQUIREMENTS_REASON = "_decision_missing_requirements"
_AUDIT_DECISION_MISSING_REQUIREMENTS = (
    "patch7_declining_industry",
    "patch7_long_term_operating_trend",
    "patch7_future_fcf",
    "patch7_current_price",
    "patch7_optimistic_upper",
)
_AUDIT_DECISION_PATCH7_VETO_REASON = "_decision_patch7_veto"
_AUDIT_DECISION_PATCH7_VETOES = frozenset({"type1_redline", "bubble_line", "type7_price", "future_fcf"})
_AUDIT_DECISION_PATCH7_PRICE_GATE_TYPES = frozenset({"type2", "type3", "type4", "type7"})
_AUDIT_DECISION_FACT_UNSET = object()
_AUDIT_DECISION_POTENTIAL_VETO_DIMENSIONS = {
    "type1": frozenset({"1a", "1b"}),
    "type2": frozenset({"2a", "2b", "2c"}),
    "type3": frozenset({"3a", "3d", "3e"}),
    "type4": frozenset({"4c", "4e", "4f"}),
    "type5": frozenset(),
    "type6": frozenset({"6a", "6b", "6c", "6d"}),
    "type7": frozenset({"7c"}),
}
_AUDIT_DECISION_MARKET_CONTEXT_FIELDS = frozenset({"tradable", "reference_price", "risk_status"})
_AUDIT_TYPE_PRIORITY = ("type1", "type2", "type5", "type3", "type4", "type6", "type7")
_AUDIT_TYPE_NAMES = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ 高风险早期/困境型",
    "type7": "7️⃣ 优质股权型",
}
_AUDIT_DCF_SKIP_CATEGORIES = frozenset(
    {
        "source_missing",
        "model_unsupported",
        "economic_not_applicable",
        "inconsistent_source",
        "internal_error",
    }
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(raw: bytes) -> Any:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


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
_AUDIT_TYPE7_FINANCIAL_INDUSTRIES = {"BANK", "INSURANCE", "SECURITIES", "FINANCIAL_OTHER"}
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
_MIN_RELEASE_ELIGIBLE_COMPANIES = 4_000
_MIN_RELEASE_MARKET_COUNTS = {"SH": 1_800, "SZ": 2_300}
_MIN_RELEASE_STRICT_TTM_SOURCE_COVERAGE = 0.90
_MIN_RELEASE_LISTING_REFERENCE_COVERAGE = 0.99
_MIN_RELEASE_LISTING_DATE_COVERAGE = 0.99
_LISTING_DATE_SOURCE = "Eastmoney push2 clist"
_LISTING_DATE_SOURCE_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_MARKET_COLDNESS_MODEL_ID = "patch6-type2c-quantity-price-v1"
_MARKET_COLDNESS_NOT_APPLICABLE_REASONS = {
    "listed_in_current_year",
    "listing_history_lt_120_days",
}
_MIN_RELEASE_TRADING_QUOTE_COVERAGE = 0.99
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_TTM_FCFF_FORMULA_VERSION = "ttm_cfo_less_capex_v2"
_TTM_REVENUE_FORMULA_VERSION = "ttm_revenue_v1"
_TTM_SOURCE_UNIT = "CNY"
_FCF_NORMALISATION_PREMIUM_CAP = 1.25
_DCF_FORECAST_YEARS = 5
_DCF_BAND_WACC_DELTA = 0.005
_DCF_MARGIN_FLOOR = 0.0
_DCF_MARGIN_LONG_TERM = 0.04
_DCF_WACC_SHIFT = {"pessimistic": 0.010, "neutral": 0.0, "optimistic": -0.005}
_DCF_TERMINAL_GROWTH = {"pessimistic": 0.0, "neutral": 0.01, "optimistic": 0.02}
_TTM_OPERATING_CASH_FIELDS = {"NETCASH_OPERATE", "经营活动产生的现金流量净额"}
_TTM_CAPEX_FIELDS = {
    "CONSTRUCT_LONG_ASSET",
    "PAY_ACQ_CONST_FIASSETS",
    "购建固定资产无形资产和其他长期资产支付的现金",
}
_TTM_REVENUE_FIELDS = {"TOTAL_OPERATE_INCOME", "OPERATE_INCOME"}
_CAPEX_PROVENANCE_SCHEMA_VERSION = 1
_STANDARD_CASHFLOW_REPORT = "RPT_DMSK_FN_CASHFLOW"
_DETAILED_CASHFLOW_REPORT = "RPT_F10_FINANCE_GCASHFLOW"
_EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SINA_FINANCIAL_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_SINA_CASHFLOW_REPORT = "SINA_COMPANY_FINANCE_2022_LLB"
_SINA_CAPEX_FIELD = "ACQUASSETCASH"
_NON_CAPEX_OUTFLOW_FIELDS = {
    "INVEST_PAY_CASH",
    "PLEDGE_LOAN_ADD",
    "OBTAIN_SUBSIDIARY_OTHER",
    "ADD_PLEDGE_TIMEDEPOSITS",
    "PAY_OTHER_INVEST",
    "INVEST_OUTFLOW_OTHER",
    "INVEST_OUTFLOW_BALANCE",
}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)[\"']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|private[_-]?key|token)"
    rb"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_./+=-]{16,})[\"']"
)
_SECRET_SCAN_SUFFIXES = {".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"}
_RULE_FILES = {
    "config.py",
    "data/capex_evidence.py",
    "data/baostock_valuation.py",
    "data/commodity_evidence.py",
    "data/datacenter.py",
    "data/exchange_financials.py",
    "data/financial_indicator_evidence.py",
    "data/financial_source_evidence.py",
    "data/financial_balance_sheet_evidence.json",
    "data/financial_zero_capex_evidence.json",
    "data/financial_zero_revenue_evidence.json",
    "data/growth_evidence.py",
    "data/industry.py",
    "data/investor_relations.py",
    "data/market_coldness.py",
    "data/market_history.py",
    "data/nbs_commodity_evidence.py",
    "data/patch4_evidence.py",
    "data/quality_history.py",
    "data/research_reports.py",
    "data/sina_financial.py",
    "data/shenwan_industry_history.py",
    "data/trading_calendar.py",
    "tools/china_a_share_trading_calendar.json",
    "engine/buy_screener.py",
    "engine/dcf.py",
    "engine/market_coldness.py",
    "engine/quantitative_evidence.py",
    "engine/quality_equity.py",
    "engine/risk.py",
    "engine/scenarios.py",
    "engine/type7_patch6.py",
    "engine/valuation_status.py",
}
_INDUSTRY_FILES = {
    "data/industry.py",
    "data/industry_f10.json",
    "data/industry_em_map.json",
    "data/industry_capco_2025h2.json",
    "data/industry_exchange_new_listings_2026.json",
    "data/shenwan_industry_history.py",
}
_DEPENDENCY_FILES = {
    "requirements-bootstrap.txt",
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-test.txt",
    "requirements-dev.txt",
    "requirements-dev-lock.txt",
    "pyproject.toml",
}
_TSINGHUA_PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _desktop_launcher_errors(content: bytes) -> tuple[str, ...]:
    """Validate the desktop install path without executing an untrusted batch file."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ("run.bat is not valid UTF-8 text",)

    errors: list[str] = []
    folded = text.casefold()
    mirror_pattern = re.compile(
        rf'^\s*if\s+not\s+defined\s+PIP_INDEX_URL\s+set\s+"PIP_INDEX_URL='
        rf'{re.escape(_TSINGHUA_PYPI_INDEX)}"\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    mirror_match = mirror_pattern.search(text)
    if mirror_match is None or folded.count("pip_index_url=") != 1:
        errors.append("run.bat does not provide an overridable Tsinghua HTTPS PIP_INDEX_URL default")

    require_venv_match = re.search(
        r'^\s*set\s+"PIP_REQUIRE_VIRTUALENV=true"\s*$',
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if require_venv_match is None:
        errors.append("run.bat does not require an active virtual environment for pip")

    if "--trusted-host" in folded or "pip_trusted_host" in folded:
        errors.append("run.bat weakens pip TLS verification with trusted-host")
    if "--index-url" in folded:
        errors.append("run.bat hard-codes --index-url instead of honoring PIP_INDEX_URL")

    install_matches: list[re.Match[str]] = []
    for lock_name in ("requirements-bootstrap.txt", "requirements-lock.txt"):
        install_pattern = re.compile(
            rf'^\s*"%VENV_PYTHON%"\s+-m\s+pip\s+install'
            rf"(?=[^\r\n]*\s--require-hashes(?:\s|$))"
            rf'(?=[^\r\n]*\s-r\s+"?{re.escape(lock_name)}"?(?:\s|$))'
            rf"[^\r\n]*\r?$",
            re.IGNORECASE | re.MULTILINE,
        )
        install_match = install_pattern.search(text)
        if install_match is None:
            errors.append(f"run.bat does not hash-lock {lock_name} installation inside .venv")
        else:
            install_matches.append(install_match)

    if install_matches:
        first_install = min(match.start() for match in install_matches)
        if mirror_match is not None and mirror_match.start() > first_install:
            errors.append("run.bat configures PIP_INDEX_URL after dependency installation")
        if require_venv_match is not None and require_venv_match.start() > first_install:
            errors.append("run.bat configures PIP_REQUIRE_VIRTUALENV after dependency installation")
    return tuple(errors)


def _normalised_file_names(names: list[str]) -> dict[str, str]:
    files = [name.replace("\\", "/") for name in names if name and not name.endswith("/")]
    first_parts = {PurePosixPath(name).parts[0] for name in files if PurePosixPath(name).parts}
    prefix = next(iter(first_parts)) if len(first_parts) == 1 and all("/" in name for name in files) else None
    result: dict[str, str] = {}
    portable_identities: dict[str, str] = {}
    for archive_name in files:
        path = PurePosixPath(archive_name)
        parts = path.parts[1:] if prefix is not None and path.parts[0] == prefix else path.parts
        for part in path.parts:
            if (
                not part
                or unicodedata.normalize("NFC", part) != part
                or part.endswith((" ", "."))
                or any(character in '<>:"|?*' for character in part)
                or any(ord(character) < 32 for character in part)
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            ):
                raise ValueError(f"unsafe archive path: {archive_name}")
        normalised = "/".join(parts)
        if normalised in result:
            raise ValueError(f"duplicate normalized archive path: {normalised}")
        portable_identity = normalised.casefold()
        if portable_identity in portable_identities:
            raise ValueError(
                f"case-insensitive archive path collision: {portable_identities[portable_identity]} and {normalised}"
            )
        result[normalised] = archive_name
        portable_identities[portable_identity] = normalised
    return result


def _hash_archive_files(archive: ZipFile, file_names: dict[str, str], selected: set[str]) -> str:
    digest = hashlib.sha256()
    for normalised in sorted(path for path in selected if path in file_names):
        digest.update(normalised.encode("utf-8"))
        digest.update(b"\0")
        digest.update(archive.read(file_names[normalised]))
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalise_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _locked_requirements(content: bytes) -> dict[str, str] | None:
    """Parse a pip-compile lock and require exact pins plus real SHA256 hashes."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    logical_lines: list[str] = []
    pending = ""
    for physical in text.splitlines():
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1].strip() + " "
            continue
        logical_lines.append((pending + stripped).strip())
        pending = ""
    if pending:
        return None

    result: dict[str, str] = {}
    for line in logical_lines:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;\\]+)(.*)", line)
        if match is None:
            return None
        name = _normalise_distribution_name(match.group(1))
        version = match.group(2)
        tail = match.group(3).strip()
        hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]{64})(?=\s|$)", tail)
        remainder = re.sub(r"--hash=sha256:[0-9a-fA-F]{64}(?=\s|$)", "", tail).strip()
        if not hashes or remainder or name in result or any(len(set(value.lower())) < 2 for value in hashes):
            return None
        result[name] = version
    return result or None


def _exact_project_dependencies(project: Mapping[str, Any]) -> dict[str, str] | None:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return None
    result: dict[str, str] = {}
    for dependency in dependencies:
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^\s;]+)\s*", str(dependency))
        if match is None:
            return None
        name = _normalise_distribution_name(match.group(1))
        if name in result:
            return None
        result[name] = match.group(2)
    return result


def _runtime_python_supported(runtime_version: object, requires_python: object) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", str(runtime_version))
    if match is None or not isinstance(requires_python, str):
        return False
    version = tuple(int(part) for part in match.groups())
    for raw_clause in requires_python.split(","):
        clause = raw_clause.strip()
        bound_match = re.fullmatch(r"(>=|<=|>|<|==)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", clause)
        if bound_match is None:
            return False
        operator = bound_match.group(1)
        bound = tuple(int(part or 0) for part in bound_match.groups()[1:])
        comparisons = {
            ">=": version >= bound,
            "<=": version <= bound,
            ">": version > bound,
            "<": version < bound,
            "==": version == bound,
        }
        if not comparisons[operator]:
            return False
    return True


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def _audit_type7_finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _audit_type7_limited_history_minimum_span(window_years: float | None) -> int:
    if window_years is None or not 1.0 <= window_years <= 5.0:
        return 0
    return max(1, int(round(window_years * 365.2425 * 0.90)))


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
    start_close = _finite_number(value.get("start_close_hfq"))
    end_close = _finite_number(value.get("end_close_hfq"))
    total_return = _finite_number(value.get("total_return"))
    cagr = _finite_number(value.get("cagr"))
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
        number = _finite_number(raw_value)
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
    current_value = _finite_number(current)
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
    window_years = _audit_type7_finite(value.get("window_years"))
    limited = bool(value.get("limited_history"))
    expected_target = _audit_type7_years_before(as_of, 5)
    if (
        window_years is None
        or not 1.0 <= window_years <= 5.0
        or value.get("formula") != _AUDIT_TYPE7_VALUATION_PERCENTILE_FORMULA
        or start is None
        or end is None
        or (not limited and target_start != expected_target)
        or span_days is None
        or start_delay is None
        or row_count is None
        or row_count > _AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS
        or span_days != (end - start).days
        or (not limited and start_delay != (start - expected_target).days)
        or (limited and start_delay != 0)
        or (
            span_days
            < (
                _audit_type7_limited_history_minimum_span(window_years)
                if limited
                else _AUDIT_TYPE7_FIVE_YEAR_TARGET_DAYS
                - _AUDIT_TYPE7_FIVE_YEAR_START_TOLERANCE_DAYS
                - _AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS
            )
        )
        or (not limited and not 0 <= start_delay <= _AUDIT_TYPE7_FIVE_YEAR_START_TOLERANCE_DAYS)
        or not 0 <= (as_of - end).days <= _AUDIT_TYPE7_HISTORY_LATEST_MAX_AGE_DAYS
    ):
        return None

    usable: dict[str, dict[str, float | int]] = {}
    for prefix, current_key, median_key in (
        ("pe", "current_pe_ttm", "median_pe_ttm"),
        ("pb", "current_pb_mrq", "median_pb_mrq"),
    ):
        observations = _audit_type7_history_integer(value.get(f"{prefix}_observations"), minimum=0)
        current = _finite_number(value.get(current_key))
        declared_median = _finite_number(value.get(median_key))
        declared_percentile = _finite_number(value.get(f"{prefix}_percentile"))
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
    current_pb = _finite_number(value.get("current_pb_mrq"))
    declared_median = _finite_number(value.get("median_pb_mrq"))
    declared_percentile = _finite_number(value.get("pb_percentile"))
    window_years = _audit_type7_finite(value.get("window_years"))
    limited = bool(value.get("limited_history"))
    replay = _audit_type7_replay_valuation_distribution(value.get("pb_distribution"), current_pb)
    minimum_span = (
        _audit_type7_limited_history_minimum_span(window_years)
        if limited and window_years is not None
        else _AUDIT_TYPE5_HISTORY_MIN_SPAN_DAYS
    )
    if (
        window_years is None
        or not 1.0 <= window_years <= 5.0
        or value.get("formula") != _AUDIT_TYPE7_VALUATION_PERCENTILE_FORMULA
        or observations is None
        or not _AUDIT_TYPE7_VALUATION_MIN_OBSERVATIONS <= observations <= _AUDIT_TYPE7_VALUATION_MAX_OBSERVATIONS
        or span_days is None
        or span_days < minimum_span
        or start_delay is None
        or (not limited and start_delay > _AUDIT_TYPE5_HISTORY_MAX_START_DELAY_DAYS)
        or (limited and start_delay != 0)
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


def _audit_type5_close(actual: Any, expected: float, *, tolerance: float = 1e-6) -> bool:
    value = _finite_number(actual)
    return value is not None and math.isclose(value, expected, rel_tol=0.0, abs_tol=tolerance)


def _audit_type5_market_replay(value: Any, *, code: str, as_of: str) -> float | None:
    if not isinstance(value, Mapping) or set(value) != {
        "score",
        "evidence_level",
        "evidence",
        "components",
    }:
        return None
    score = _finite_number(value.get("score"))
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
    values = {key: _finite_number(raw_values.get(key)) for key in expected_raw_keys}
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
                not _audit_type5_close(relative.get(key), round(expected_relative[key], 6)) for key in expected_relative
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
        ytd_reliability = _finite_number(components.get("ytd_reliability"))
        if ytd_reliability is None or not 0 <= ytd_reliability <= 1:
            return None
        expected_weights = dict(_AUDIT_TYPE5_COLDNESS_WEIGHTS)
        expected_weights["change_ytd_pct"] *= ytd_reliability
        if any(
            not _audit_type5_close(absolute.get(key), round(expected_absolute[key], 6))
            or not _audit_type5_close(metric_scores.get(key), round(expected_metric_scores[key], 6))
            or not _audit_type5_close(weights.get(key), round(expected_weights[key], 6))
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
            not _audit_type5_close(components.get("raw_score"), round(raw_score, 6))
            or not _audit_type5_close(components.get("price_score"), round(price_score, 6))
            or not _audit_type5_close(components.get("score_cap"), score_cap)
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
        number = _finite_number(raw_value)
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
    quote_pb = _finite_number(contract.get("quote_pb"))
    published_pb = _finite_number(row_pb)
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
        or reference > shanghai_today()
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


def _audit_type5_bottom_evidence_valid(
    code: str,
    company: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    reasons = payload.get("reasons")
    sub_scores = payload.get("sub_scores")
    reason = reasons.get("5b") if isinstance(reasons, Mapping) else None
    mode = payload.get("bottom_evidence_mode")
    contract = payload.get("bottom_evidence_contract")
    if mode not in {"automatic_replay", "trusted_external", "incomplete", "not_applicable"}:
        return False
    status = payload.get("status")
    if (status == "not_applicable") != (mode == "not_applicable"):
        return False
    if mode != "automatic_replay":
        return contract is None
    if contract is None:
        return False
    replay = _audit_type5_bottom_contract_replay(
        contract,
        code=code,
        as_of=company.get("source_trade_date"),
        row_pb=company.get("pb"),
    )
    score = _finite_number(sub_scores.get("5b")) if isinstance(sub_scores, Mapping) else None
    return bool(
        replay is not None
        and score is not None
        and math.isclose(score, replay[0], rel_tol=0.0, abs_tol=1e-9)
        and reason == replay[1]
    )


def _audit_type5_official_context_valid(company: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    context = payload.get("official_cycle_context")
    if context is None:
        return True
    fields = {
        "source",
        "source_url",
        "source_sha256",
        "published_at",
        "period_title",
        "product_name",
        "unit",
        "current_price_yuan",
        "change_yuan",
        "change_pct",
        "as_of",
        "score_effect",
    }
    if not isinstance(context, Mapping) or set(context) != fields:
        return False
    parsed = urlsplit(str(context.get("source_url") or ""))
    try:
        port = parsed.port
        published = date.fromisoformat(str(context.get("published_at") or "")[:10])
        cutoff = date.fromisoformat(str(company.get("source_trade_date") or ""))
    except (TypeError, ValueError):
        return False
    return bool(
        context.get("source") == "国家统计局流通领域重要生产资料市场价格"
        and context.get("as_of") == cutoff.isoformat()
        and context.get("score_effect") == "context_only"
        and published <= cutoff
        and parsed.scheme == "https"
        and parsed.hostname == "www.stats.gov.cn"
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and re.fullmatch(r"[0-9a-f]{64}", str(context.get("source_sha256") or "")) is not None
        and (_finite_number(context.get("current_price_yuan")) or 0.0) > 0
        and _finite_number(context.get("change_yuan")) is not None
        and _finite_number(context.get("change_pct")) is not None
        and bool(str(context.get("product_name") or "").strip())
        and bool(str(context.get("unit") or "").strip())
    )


def _type7_linear(value: float | None, anchors: Sequence[tuple[float, float]], *, missing: float = 2.0) -> float:
    if value is None:
        return round(missing, 2)
    if value <= anchors[0][0]:
        return round(min(10.0, max(0.0, anchors[0][1])), 2)
    for (left_x, left_y), (right_x, right_y) in zip(anchors, anchors[1:]):
        if value <= right_x:
            fraction = (value - left_x) / (right_x - left_x)
            return round(min(10.0, max(0.0, left_y + fraction * (right_y - left_y))), 2)
    return round(min(10.0, max(0.0, anchors[-1][1])), 2)


def _type7_average(values: Sequence[float]) -> float:
    return round(min(10.0, max(0.0, math.fsum(values) / len(values))), 2)


def _type7_template_input_score(
    section_key: str,
    key: str,
    inputs: Mapping[str, Any],
) -> tuple[bool, float | None]:
    if section_key == "template5":
        if key not in _AUDIT_TYPE7_TEMPLATE5_LABELS or set(inputs) != {"normalized_score"}:
            return False, None
        return True, _finite_number(inputs.get("normalized_score"))
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
        values = [_finite_number(inputs.get(field)) for field in mean_inputs[key]]
        return (False, None) if any(value is None for value in values) else (True, _type7_average(values))
    if key in {"t1_05", "t1_07", "t1_17"}:
        return True, _finite_number(inputs.get("score"))
    if key == "t1_20":
        if inputs.get("validation_basis") != "source_bound_nonfinancial_dcf":
            return False, None
        return True, _finite_number(inputs.get("type1_1a"))
    if key == "t1_03":
        raw = inputs.get("rate")
        if raw is not None and _finite_number(raw) is None:
            return False, None
        return True, _type7_linear(
            _finite_number(raw), [(-0.15, 0), (0.0, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.35, 10)]
        )
    if key == "t1_04":
        raw_values = [inputs.get("profit_cagr"), inputs.get("fcf_cagr")]
        if any(value is not None and _finite_number(value) is None for value in raw_values):
            return False, None
        scores = [
            _type7_linear(_finite_number(value), [(-0.15, 0), (0.0, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.35, 10)])
            for value in raw_values
        ]
        return True, _type7_average(scores)
    if key == "t1_18" and "annual_return" in inputs:
        return True, _type7_linear(
            _finite_number(inputs.get("annual_return")),
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
    """Independently validate bounded report-body summaries in a release."""

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
            fact_value = _finite_number(raw_fact_value)
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


def _audit_patch4_close(actual: Any, expected: float, *, tolerance: float = 0.0001) -> bool:
    value = _finite_number(actual)
    return value is not None and math.isclose(value, expected, rel_tol=0.0, abs_tol=tolerance)


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
        or reference > shanghai_today()
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
        score = _finite_number(component.get("score")) if isinstance(component, Mapping) else None
        points = _finite_number(component.get("points")) if isinstance(component, Mapping) else None
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
            or not _audit_patch4_close(component.get("weight"), weight)
            or not _audit_patch4_close(points, round(score * weight / 10.0, 4))
        ):
            return False
        indexed[key] = component
    if set(indexed) != set(_AUDIT_PATCH4_COMPONENT_WEIGHTS):
        return False

    fairness = indexed["p4_defensive_fairness"]
    governance = indexed["p4_defensive_governance"]
    fairness_score = _finite_number(fairness_item.get("score"))
    governance_score = _finite_number(governance_component.get("score"))
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
        or not _audit_patch4_close(fairness.get("score"), fairness_score)
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
        or not _audit_patch4_close(governance.get("score"), governance_score)
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
        value = _finite_number(inputs.get("value")) if isinstance(inputs, Mapping) else None
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
            or not _audit_patch4_close(component.get("score"), _audit_patch4_linear(value, anchors))
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
            or not _audit_patch4_close(component.get("score"), expected_score)
        ):
            return False
    expected_score = round(math.fsum(float(component["points"]) for component in indexed.values()) / 10.0, 2)
    expected_complete = all(component.get("complete") is True for component in indexed.values())
    return bool(
        _audit_patch4_close(assessment.get("score"), expected_score) and assessment.get("complete") is expected_complete
    )


def _audit_type7_ledger_valid_impl(
    code: str,
    ledger: Any,
    status: Any,
    patch4_bindings: Mapping[str, Mapping[str, str]] | None,
) -> bool:
    """Independently replay the Type 7 formula ledger in a release artifact."""

    def close(actual: Any, expected: float, *, tolerance: float = 0.0001) -> bool:
        value = _finite_number(actual)
        return value is not None and math.isclose(value, expected, rel_tol=0.0, abs_tol=tolerance)

    if not isinstance(ledger, Mapping):
        return False
    if status == "not_applicable":
        return bool(
            set(ledger) == {"schema_version", "model_id", "code", "applicable", "reason"}
            and ledger.get("schema_version") == _AUDIT_TYPE7_SCHEMA_VERSION
            and ledger.get("model_id") == _AUDIT_TYPE7_MODEL_ID
            and ledger.get("code") == code
            and ledger.get("applicable") is False
            and str(ledger.get("reason") or "").strip()
        )
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
    if set(ledger) != expected_ledger_fields or (
        ledger.get("schema_version") != _AUDIT_TYPE7_SCHEMA_VERSION
        or ledger.get("model_id") != _AUDIT_TYPE7_MODEL_ID
        or ledger.get("code") != code
        or ledger.get("source_rule") != "Template1>70 AND Template5>70 AND Patch5>70"
        or not close(ledger.get("strict_threshold"), 70.0, tolerance=1e-9)
    ):
        return False

    sections: dict[str, Mapping[str, Any]] = {}
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
            return False
        indexed: dict[str, Mapping[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {
                "key",
                "label",
                "weight",
                "score",
                "points",
                "complete",
                "evidence_level",
                "formula",
                "inputs",
            }:
                return False
            key = item.get("key") if isinstance(item, Mapping) else None
            if not isinstance(key, str) or key not in expected_weights or key in indexed:
                return False
            score = _finite_number(item.get("score"))
            weight = _finite_number(item.get("weight"))
            points = _finite_number(item.get("points"))
            inputs = item.get("inputs")
            if section_key == "template1":
                contract = _AUDIT_TYPE7_TEMPLATE1_CONTRACTS.get(key)
                expected_label = contract[0] if contract is not None else None
                expected_formula = contract[1] if contract is not None else None
            else:
                expected_label = _AUDIT_TYPE7_TEMPLATE5_LABELS.get(key)
                expected_formula = "Template5_source_weight*observable_score"
            input_valid, replayed_score = (
                _type7_template_input_score(section_key, key, inputs) if isinstance(inputs, Mapping) else (False, None)
            )
            if (
                score is None
                or not 0 <= score <= 10
                or weight is None
                or not close(weight, expected_weights[key], tolerance=1e-9)
                or points is None
                or not math.isclose(points, round(score * weight / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
                or not isinstance(item.get("complete"), bool)
                or item.get("label") != expected_label
                or item.get("formula") != expected_formula
                or item.get("evidence_level") not in _AUDIT_TYPE7_EVIDENCE_LEVELS
                or not input_valid
                or (replayed_score is not None and not close(score, replayed_score))
            ):
                return False
            indexed[key] = item
        if set(indexed) != set(expected_weights):
            return False
        expected_score = round(math.fsum(float(item["points"]) for item in indexed.values()), 2)
        expected_coverage = round(
            math.fsum(expected_weights[key] for key, item in indexed.items() if item["complete"]) / 100.0,
            4,
        )
        if not close(section.get("score"), expected_score) or not close(section.get("coverage"), expected_coverage):
            return False
        sections[section_key] = section
        template_items[section_key] = indexed

    patch5 = ledger.get("patch5")
    dimensions = patch5.get("dimensions") if isinstance(patch5, Mapping) else None
    if (
        not isinstance(patch5, Mapping)
        or set(patch5) != {"score", "coverage", "safety_margin_score", "safety_margin_complete", "dimensions"}
        or not isinstance(dimensions, list)
        or len(dimensions) != 5
    ):
        return False
    patch_sections: dict[str, Mapping[str, Any]] = {}
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
            or not close(section.get("max_points"), 20.0, tolerance=1e-9)
            or section.get("label") != _AUDIT_TYPE7_PATCH_SECTION_LABELS.get(key)
            or not isinstance(section.get("complete"), bool)
        ):
            return False
        indexed: dict[str, Mapping[str, Any]] = {}
        for component in components:
            if not isinstance(component, Mapping) or set(component) != {
                "key",
                "label",
                "max_points",
                "score",
                "points",
                "complete",
                "formula",
                "inputs",
            }:
                return False
            component_key = component.get("key") if isinstance(component, Mapping) else None
            if not isinstance(component_key, str) or component_key not in expected_weights or component_key in indexed:
                return False
            score = _finite_number(component.get("score"))
            maximum = expected_weights[component_key]
            points = _finite_number(component.get("points"))
            inputs = component.get("inputs")
            expected_input_fields = {"source"} if component_key in _AUDIT_TYPE7_PATCH_SOURCE_INPUT_COMPONENTS else set()
            inputs_valid = isinstance(inputs, Mapping) and set(inputs) == expected_input_fields
            if inputs_valid and expected_input_fields:
                inputs_valid = inputs.get("source") in _AUDIT_TYPE7_PATCH_SOURCE_LEVELS
            if (
                score is None
                or not 0 <= score <= 10
                or not close(component.get("max_points"), maximum, tolerance=1e-9)
                or points is None
                or not math.isclose(points, round(score * maximum / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
                or not isinstance(component.get("complete"), bool)
                or component.get("label") != _AUDIT_TYPE7_PATCH_COMPONENT_LABELS.get(component_key)
                or component.get("formula") != f"{maximum:g}*score/10"
                or not inputs_valid
            ):
                return False
            indexed[component_key] = component
        if set(indexed) != set(expected_weights):
            return False
        expected_points = round(math.fsum(float(item["points"]) for item in indexed.values()), 4)
        expected_complete = all(item["complete"] for item in indexed.values())
        if not close(section.get("points"), expected_points) or section.get("complete") is not expected_complete:
            return False
        patch_sections[key] = section
    if set(patch_sections) != set(_AUDIT_TYPE7_PATCH_WEIGHTS):
        return False
    expected_patch_score = round(math.fsum(float(section["points"]) for section in patch_sections.values()), 2)
    expected_patch_coverage = round(
        math.fsum(20.0 for section in patch_sections.values() if section["complete"]) / 100.0,
        4,
    )
    safety = patch_sections["p5_safety"]
    if (
        not close(patch5.get("score"), expected_patch_score)
        or not close(patch5.get("coverage"), expected_patch_coverage)
        or not close(patch5.get("safety_margin_score"), round(float(safety["points"]), 2))
        or patch5.get("safety_margin_complete") is not safety["complete"]
    ):
        return False

    scores = ledger.get("scores")
    expected_sections = {"template1": sections["template1"], "template5": sections["template5"], "patch5": patch5}
    if not isinstance(scores, Mapping) or set(scores) != set(expected_sections):
        return False
    score_values: dict[str, float] = {}
    for key, section in expected_sections.items():
        value = _finite_number(scores.get(key))
        section_score = _finite_number(section.get("score"))
        if value is None or section_score is None or not close(value, section_score):
            return False
        score_values[key] = value
    strict = {key: value > 70.0 for key, value in score_values.items()}
    strict_checks = ledger.get("strict_checks")
    if (
        not isinstance(strict_checks, Mapping)
        or any(not isinstance(value, bool) for value in strict_checks.values())
        or dict(strict_checks) != strict
        or ledger.get("all_scores_strictly_above_70") is not all(strict.values())
    ):
        return False
    prerequisites = ledger.get("prerequisites")
    if not isinstance(prerequisites, Mapping) or set(prerequisites) != _AUDIT_TYPE7_PREREQUISITES:
        return False
    pass_flags = {key: record.get("passed") for key, record in prerequisites.items() if isinstance(record, Mapping)}
    if len(pass_flags) != len(_AUDIT_TYPE7_PREREQUISITES) or any(
        not isinstance(value, bool) for value in pass_flags.values()
    ):
        return False

    core_prerequisite = prerequisites.get("core_modules_80pct")
    template1_coverage = _finite_number(sections["template1"].get("coverage"))
    core_actual = _finite_number(core_prerequisite.get("actual")) if isinstance(core_prerequisite, Mapping) else None
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
        or not close(core_actual, template1_coverage)
        or not close(core_prerequisite.get("required"), 0.80)
        or core_prerequisite.get("required_items_complete") is not required_items_complete
        or core_prerequisite.get("incomplete_required_items") != incomplete_required_items
        or core_prerequisite.get("passed") is not expected_core_passed
    ):
        return False

    technology_prerequisite = prerequisites.get("technology_patch4")
    technology_item = template_items["template1"].get("t1_17")
    technology_score = _finite_number(technology_item.get("score")) if isinstance(technology_item, Mapping) else None
    technology_complete = technology_item.get("complete") if isinstance(technology_item, Mapping) else None
    technology_applicable = (
        technology_prerequisite.get("applicable") if isinstance(technology_prerequisite, Mapping) else None
    )
    applicability = (
        technology_prerequisite.get("applicability") if isinstance(technology_prerequisite, Mapping) else None
    )
    assessment = technology_prerequisite.get("assessment") if isinstance(technology_prerequisite, Mapping) else None
    rd_intensity = _finite_number(applicability.get("rd_intensity")) if isinstance(applicability, Mapping) else None
    published_technology_score = (
        _finite_number(applicability.get("technology_score")) if isinstance(applicability, Mapping) else None
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
    patch4_score = _finite_number(assessment.get("score")) if isinstance(assessment, Mapping) else None
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
            fairness_item=template_items["template1"].get("t1_08", {}),
            governance_component=governance_component,
            allowed_bindings=patch4_bindings,
        )
    )
    expected_incentive_score = (
        patch4_score
        if expected_technology_applicable and patch4_score is not None
        else 2.0
        if expected_technology_applicable
        else _finite_number(template_items["template1"].get("t1_08", {}).get("score"))
    )
    expected_incentive_complete = (
        patch4_complete
        if expected_technology_applicable
        else template_items["template1"].get("t1_08", {}).get("complete") is True
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
        or not close(published_technology_score, technology_score)
        or published_technology_complete is not technology_complete
        or (rd_intensity is not None and not 0 <= rd_intensity <= 1)
        or technology_applicable is not expected_technology_applicable
        or technology_prerequisite.get("score") != (patch4_score if patch4_complete else None)
        or technology_prerequisite.get("validation_status") != expected_technology_status
        or technology_prerequisite.get("passed") is not (not expected_technology_applicable or patch4_complete)
        or not patch4_valid
        or expected_incentive_score is None
        or not close(incentive_component.get("score"), expected_incentive_score)
        or incentive_component.get("complete") is not expected_incentive_complete
    ):
        return False

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
        return False

    valuation_prerequisite = prerequisites.get("latest_quote_and_valuation")
    t1_items = template_items.get("template1", {})
    expected_valuation_complete = t1_items.get("t1_20", {}).get("complete") is True
    if not isinstance(valuation_prerequisite, Mapping):
        return False
    raw_quote_as_of = valuation_prerequisite.get("as_of")
    try:
        quote_as_of = date.fromisoformat(raw_quote_as_of) if isinstance(raw_quote_as_of, str) else None
    except ValueError:
        quote_as_of = None
    expected_valuation_passed = bool(
        expected_valuation_complete and quote_as_of is not None and quote_as_of <= shanghai_today()
    )
    if (
        set(valuation_prerequisite) != {"passed", "as_of", "valuation_complete", "validation_basis"}
        or valuation_prerequisite.get("validation_basis") != "source_bound_nonfinancial_dcf"
        or valuation_prerequisite.get("valuation_complete") is not expected_valuation_complete
        or valuation_prerequisite.get("passed") is not expected_valuation_passed
    ):
        return False

    report_prerequisite = prerequisites.get("three_external_reports")
    if not isinstance(report_prerequisite, Mapping) or set(report_prerequisite) != {
        "passed",
        "check_type",
        "source_count",
        "distinct_publishers",
        "recent_source_count",
        "max_age_days",
        "recent_age_days",
        "sources",
    }:
        return False
    sources = report_prerequisite.get("sources")
    if not isinstance(sources, list) or len(sources) > 20 or (sources and quote_as_of is None):
        return False
    normalized_sources: list[dict[str, str]] = []
    identities: set[str] = set()
    urls: set[str] = set()
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
            return False
        item = {field: str(source[field]).strip() for field in fields}
        try:
            parsed = urlsplit(item["url"])
            port = parsed.port
            source_date = date.fromisoformat(item["as_of"])
        except (ValueError, TypeError):
            return False
        identity = item["evidence_id"].casefold()
        canonical_url = item["url"].casefold()
        if (
            any(not text or len(text) > 300 or any(ord(character) < 32 for character in text) for text in item.values())
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
            return False
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
    expected_report_pass = len(normalized_sources) >= 3 and publisher_count >= 3 and recent_source_count >= 1
    if not (
        normalized_sources == sources
        and report_prerequisite.get("check_type") == "metadata_availability_precheck"
        and report_prerequisite.get("source_count") == len(normalized_sources)
        and report_prerequisite.get("distinct_publishers") == publisher_count
        and report_prerequisite.get("recent_source_count") == recent_source_count
        and report_prerequisite.get("max_age_days") == _AUDIT_TYPE7_RESEARCH_MAX_AGE_DAYS
        and report_prerequisite.get("recent_age_days") == _AUDIT_TYPE7_RESEARCH_RECENT_AGE_DAYS
        and report_prerequisite.get("passed") is expected_report_pass
    ):
        return False

    content_prerequisite = prerequisites.get("external_report_content_verification")
    content_as_of = quote_as_of.isoformat() if quote_as_of is not None else "0001-01-01"
    if not _audit_type7_content_valid(
        content_prerequisite,
        sources=normalized_sources,
        code=code,
        as_of=content_as_of,
    ):
        return False

    history_prerequisite = prerequisites.get("ten_year_return_and_five_year_valuation")
    history_inputs = t1_items.get("t1_19", {}).get("inputs", {})
    shareholder_input = history_inputs.get("shareholder_return") if isinstance(history_inputs, Mapping) else None
    valuation_history_input = (
        shareholder_input.get("valuation_history_contract") if isinstance(shareholder_input, Mapping) else None
    )
    if not isinstance(history_prerequisite, Mapping) or set(history_prerequisite) != {"passed", "as_of"}:
        return False
    history_as_of = history_prerequisite.get("as_of")
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
        shareholder_history_valid and valuation_history_replay is not None and t5_history_item.get("complete") is True
    )
    embedded_history_claimed = bool(
        isinstance(shareholder_input, Mapping)
        and (
            shareholder_input.get("available") is True
            or (isinstance(valuation_history_input, Mapping) and valuation_history_input.get("available") is True)
        )
    )
    if embedded_history_claimed and (not shareholder_history_valid or valuation_history_replay is None):
        return False
    if valuation_history_replay is not None:
        combined_percentile = statistics.median(
            float(series["percentile"]) for series in valuation_history_replay.values()
        )
        expected_history_score = _type7_linear(
            combined_percentile,
            [(0.0, 10), (0.10, 9), (0.30, 8), (0.50, 6.5), (0.70, 5), (0.90, 2), (1.0, 0)],
            missing=0,
        )
    else:
        expected_history_score = 0.0
    if (
        not isinstance(t5_history_item, Mapping)
        or t5_history_item.get("complete") is not (valuation_history_replay is not None)
        or not close(t5_history_item.get("score"), expected_history_score)
    ):
        return False
    if not (
        history_prerequisite.get("passed") is expected_history_pass
        and (history_as_of is None or history_date is not None)
        and (not expected_history_pass or history_as_of == raw_quote_as_of)
    ):
        return False

    prerequisites_complete = all(pass_flags.values())
    safety_score = _finite_number(patch5.get("safety_margin_score"))
    safety_veto = bool(safety["complete"] and safety_score is not None and safety_score < 8.0)
    expected_trigger = all(strict.values()) and prerequisites_complete and not safety_veto
    if not (
        ledger.get("prerequisites_complete") is prerequisites_complete
        and ledger.get("safety_veto") is safety_veto
        and ledger.get("triggered") is expected_trigger
    ):
        return False

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
        not isinstance(published_decisive_upper, Mapping)
        or set(published_decisive_upper) != set(expected_decisive_upper)
        or any(not close(published_decisive_upper.get(key), value) for key, value in expected_decisive_upper.items())
    ):
        return False
    expected_decisive_failure = any(value <= 70.0 for value in expected_decisive_upper.values())
    if ledger.get("decisively_not_triggered") is not expected_decisive_failure:
        return False

    diagnostic_total = round(sum(round(value / 10.0, 3) / 3.0 for value in score_values.values()), 1)
    if status != "blocked":
        if expected_decisive_failure:
            expected_status = "not_triggered"
        elif safety_veto:
            expected_status = "vetoed"
        elif not prerequisites_complete:
            expected_status = "insufficient_evidence"
        elif expected_trigger:
            expected_status = "triggered"
        elif diagnostic_total >= 7.0 and not all(strict.values()):
            expected_status = "conditional"
        elif diagnostic_total >= 5.0:
            expected_status = "observe"
        else:
            expected_status = "not_triggered"
        if status != expected_status:
            return False

    t1_items = template_items.get("template1", {})
    t5_items = template_items.get("template5", {})
    if not (
        {"t1_18", "t1_19", "t1_20"}.issubset(t1_items)
        and {"t5_v1", "t5_v3"}.issubset(t5_items)
        and "p5_safety" in patch_sections
    ):
        return False
    safety_components = {
        component.get("key"): component
        for component in patch_sections["p5_safety"].get("components", [])
        if isinstance(component, Mapping)
    }
    valuation_component = safety_components.get("p5_s1")
    dcf_score = _finite_number(t1_items["t1_20"].get("score"))
    if not isinstance(valuation_component, Mapping) or dcf_score is None:
        return False
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
        not isinstance(published_upper, Mapping)
        or set(published_upper) != set(expected_upper)
        or any(not close(published_upper.get(key), value) for key, value in expected_upper.items())
    ):
        return False
    history_request_core_ready = bool(template1_coverage is not None and template1_coverage >= 0.80)
    expected_request = bool(
        not pass_flags["ten_year_return_and_five_year_valuation"]
        and history_request_core_ready
        and all(
            value
            for key, value in pass_flags.items()
            if key
            not in {
                "core_modules_80pct",
                "three_external_reports",
                "external_report_content_verification",
                "ten_year_return_and_five_year_valuation",
            }
        )
        and not safety_veto
        and all(value > 70.0 for value in expected_upper.values())
        and not expected_decisive_failure
    )
    expected_research_request = bool(
        (not pass_flags["three_external_reports"] or not pass_flags["external_report_content_verification"])
        and all(
            value
            for key, value in pass_flags.items()
            if key not in {"three_external_reports", "external_report_content_verification"}
        )
        and not safety_veto
        and (not expected_decisive_failure or (len(score_values) == 3 and min(score_values.values()) >= 60.0))
    )
    return (
        ledger.get("history_request_needed") is expected_request
        and ledger.get("research_request_needed") is expected_research_request
    )


def _audit_type7_ledger_valid(
    code: str,
    ledger: Any,
    status: Any,
    *,
    patch4_bindings: Mapping[str, Mapping[str, str]] | None = None,
) -> bool:
    """Fail closed when release JSON contains hostile Type 7 values."""

    try:
        if isinstance(ledger, Mapping) and ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
            if status == "not_applicable":
                return bool(
                    set(ledger) == {"schema_version", "model_id", "code", "applicable", "reason"}
                    and ledger.get("schema_version") == PATCH6_TYPE7_SCHEMA_VERSION
                    and ledger.get("code") == code
                    and ledger.get("applicable") is False
                    and str(ledger.get("reason") or "").strip()
                )
            return (
                not validate_patch6_type7_ledger(ledger, expected_code=code)
                and not _independent_type7_ledger_errors(ledger, expected_code=code)
                and bool(ledger.get("triggered")) is (status == "triggered")
            )
        return False
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        return False


def _expected_audit_bear_case(
    type_key: str,
    scores: Mapping[str, float],
    reasons: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Independently replay the production report's two-decimal bear-case view."""

    weights = _AUDIT_TYPE_WEIGHTS[type_key]
    display_scores = {dimension: round(float(scores[dimension]), 2) for dimension in weights}
    minimum_score = min(display_scores.values())
    expected: list[dict[str, Any]] = []
    for meta_key in ("_veto", "_condition", "_downgrade"):
        if reasons.get(meta_key):
            expected.append(
                {
                    "dimension": meta_key,
                    "score": minimum_score,
                    "reason": str(reasons[meta_key]),
                }
            )
            if len(expected) == 3:
                return expected
    order = {key: index for index, key in enumerate(weights)}
    ranked = sorted(weights, key=lambda key: (display_scores[key], -weights[key], order[key]))
    for dimension in ranked:
        expected.append(
            {
                "dimension": dimension,
                "score": display_scores[dimension],
                "reason": str(reasons[dimension]),
            }
        )
        if len(expected) == 3:
            break
    return expected


def _audit_quantitative_evidence_valid(company: Mapping[str, Any], code: str) -> bool:
    """Replay all 13 records without converting honest gaps into scores."""

    records = company.get("quantitative_evidence")
    levels = company.get("quantitative_evidence_levels")
    if (
        not isinstance(records, Mapping)
        or set(records) != _AUDIT_QUANTITATIVE_EVIDENCE_KEYS
        or not isinstance(levels, Mapping)
        or set(levels) != _AUDIT_QUANTITATIVE_EVIDENCE_KEYS
    ):
        return False
    effective_levels: list[str] = []
    for key in sorted(_AUDIT_QUANTITATIVE_EVIDENCE_KEYS):
        record = records.get(key)
        level = levels.get(key)
        if (
            not isinstance(record, Mapping)
            or level not in _AUDIT_QUANTITATIVE_LEVELS
            or record.get("evidence_level") != level
            or company.get(f"{key}_evidence_level") != level
        ):
            return False
        try:
            normalized = validate_quantitative_evidence_record(
                record,
                key=key,
                code=code,
                industry=str(company.get("industry") or ""),
            )
        except (TypeError, ValueError):
            return False
        attached_score = _finite_number(company.get(key))
        attached_evidence = company.get(f"{key}_evidence")
        if level in _AUDIT_SCORED_QUANTITATIVE_LEVELS:
            if (
                attached_score is None
                or not math.isclose(attached_score, float(normalized["score"]), rel_tol=0.0, abs_tol=1e-12)
                or attached_evidence != normalized["evidence"]
            ):
                return False
        elif attached_score is not None or isinstance(attached_evidence, Mapping):
            return False
        effective_levels.append(str(level))
    expected_status = (
        "complete"
        if all(level in _AUDIT_COMPLETE_QUANTITATIVE_LEVELS for level in effective_levels)
        else "missing"
        if all(level == "missing" for level in effective_levels)
        else "partial"
    )
    return company.get("quantitative_evidence_status") == expected_status


def _audit_compact_decision_reason(value: Any) -> str:
    text = " ".join(str(value or "数据不足").split())
    if len(text) <= _AUDIT_REASON_MAX_LENGTH:
        return text
    best_boundary = ""
    for match in re.finditer(r"[；;。！？!?，,]", text):
        candidate = text[: match.start()].rstrip()
        if len(candidate) > _AUDIT_REASON_MAX_LENGTH - 1:
            break
        if candidate:
            best_boundary = candidate
    if best_boundary:
        return best_boundary + "…"
    prefix = text[: _AUDIT_REASON_MAX_LENGTH - 1].rstrip()
    word_boundary = prefix.rfind(" ")
    if word_boundary >= max(4, (_AUDIT_REASON_MAX_LENGTH - 1) // 2):
        prefix = prefix[:word_boundary].rstrip()
    return prefix + "…"


def _audit_decision_missing_requirements(reasons: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate the serialized Patch 7 post-gate requirement ledger."""

    raw = reasons.get(_AUDIT_DECISION_MISSING_REQUIREMENTS_REASON)
    if raw is None:
        return True, []
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(value, str) for value in raw)
        or len(raw) != len(set(raw))
        or set(raw) - set(_AUDIT_DECISION_MISSING_REQUIREMENTS)
    ):
        return False, []
    return True, [key for key in _AUDIT_DECISION_MISSING_REQUIREMENTS if key in raw]


def _audit_patch7_requirements_match(
    type_key: str,
    requirements: Sequence[str],
    *,
    reasons: Mapping[str, Any],
    company: Mapping[str, Any] | None,
    dcf_result: object,
) -> bool:
    """Bind serialized Patch 7 requirements to the audited company facts."""

    if not requirements:
        return True

    red_line_requirements = {
        "patch7_declining_industry",
        "patch7_long_term_operating_trend",
    }
    selected_red_lines = set(requirements).intersection(red_line_requirements)
    if selected_red_lines:
        gate = str(reasons.get("_patch7_gate") or "")
        expected_red_lines: set[str] = set()
        if "行业样本缺" in gate:
            expected_red_lines.add("patch7_declining_industry")
        if "营收缺" in gate:
            expected_red_lines.add("patch7_long_term_operating_trend")
        return bool(
            type_key == "type1"
            and set(requirements) == selected_red_lines == expected_red_lines
            and gate.startswith("待补|")
        )

    if "patch7_future_fcf" in requirements:
        if list(requirements) != ["patch7_future_fcf"] or not isinstance(company, Mapping):
            return False
        type7 = company.get("type7")
        ledger = type7.get("ledger") if isinstance(type7, Mapping) else None
        gates = ledger.get("decision_gates") if isinstance(ledger, Mapping) else None
        future_fcf = gates.get("future_fcf") if isinstance(gates, Mapping) else None
        return not (
            isinstance(future_fcf, Mapping)
            and future_fcf.get("complete") is True
            and isinstance(future_fcf.get("passed"), bool)
        )

    if type_key not in _AUDIT_DECISION_PATCH7_PRICE_GATE_TYPES:
        return False
    if not isinstance(company, Mapping) or dcf_result is _AUDIT_DECISION_FACT_UNSET:
        return False

    expected: list[str] = []
    if _finite_number(company.get("price")) is None:
        expected.append("patch7_current_price")
    dcf = dcf_result if isinstance(dcf_result, Mapping) else {}
    points = dcf.get("dcf_points") if isinstance(dcf.get("dcf_points"), Mapping) else {}
    optimistic = points.get("optimistic") if isinstance(points.get("optimistic"), Mapping) else {}
    if _finite_number(optimistic.get("upper")) is None:
        expected.append("patch7_optimistic_upper")
    return list(requirements) == expected


def _audit_patch7_veto_match(
    type_key: str,
    reasons: Mapping[str, Any],
    *,
    company: Mapping[str, Any] | None,
    dcf_result: object,
) -> tuple[bool, str | None]:
    """Bind a Patch 7 post-gate veto marker to release facts."""

    marker = reasons.get(_AUDIT_DECISION_PATCH7_VETO_REASON)
    if marker is None:
        return True, None
    if not isinstance(marker, str) or marker not in _AUDIT_DECISION_PATCH7_VETOES:
        return False, None
    veto = str(reasons.get("_veto") or "")
    if marker == "type1_redline":
        gate = str(reasons.get("_patch7_gate") or "")
        valid = type_key == "type1" and gate.startswith("否决|") and veto.startswith("补丁7红线否决：")
        return valid, marker if valid else None
    if not isinstance(company, Mapping):
        return False, None
    if marker == "future_fcf":
        type7 = company.get("type7")
        ledger = type7.get("ledger") if isinstance(type7, Mapping) else None
        gates = ledger.get("decision_gates") if isinstance(ledger, Mapping) else None
        future_fcf = gates.get("future_fcf") if isinstance(gates, Mapping) else None
        valid = bool(
            veto == "补丁7未来自由现金流前置条件未通过"
            and isinstance(future_fcf, Mapping)
            and future_fcf.get("complete") is True
            and future_fcf.get("passed") is False
        )
        return valid, marker if valid else None
    if dcf_result is _AUDIT_DECISION_FACT_UNSET or type_key not in _AUDIT_DECISION_PATCH7_PRICE_GATE_TYPES:
        return False, None
    price = _finite_number(company.get("price"))
    dcf = dcf_result if isinstance(dcf_result, Mapping) else {}
    points = dcf.get("dcf_points") if isinstance(dcf.get("dcf_points"), Mapping) else {}
    optimistic = points.get("optimistic") if isinstance(points.get("optimistic"), Mapping) else {}
    upper = _finite_number(optimistic.get("upper"))
    if marker == "bubble_line":
        valid = bool(
            veto == "补丁7泡沫线否决：价格高于乐观值120%"
            and (
                dcf.get("bubble_warning") is True or (price is not None and upper is not None and price >= upper * 1.2)
            )
        )
        return valid, marker if valid else None
    valid = bool(
        type_key == "type7"
        and veto == "补丁7价格闸门：股价未合理或低估"
        and ((price is not None and upper is not None and price > upper) or dcf.get("zone") == "卖出区")
    )
    return valid, marker if valid else None


def _audit_decision_contract_valid(
    type_key: str,
    payload: Mapping[str, Any],
    *,
    company: Mapping[str, Any] | None = None,
    dcf_result: object = _AUDIT_DECISION_FACT_UNSET,
) -> bool:
    """Duplicate the bounded decision model and compare all serialized fields."""

    weights = _AUDIT_TYPE_WEIGHTS[type_key]
    sub_scores = payload.get("sub_scores")
    reasons = payload.get("reasons")
    decision = payload.get("decision")
    if (
        not isinstance(sub_scores, Mapping)
        or set(sub_scores) != set(weights)
        or not isinstance(reasons, Mapping)
        or not isinstance(decision, Mapping)
        or set(decision) != _AUDIT_DECISION_FIELDS
    ):
        return False
    if type_key == "type7":
        ledger = payload.get("ledger")
        if not isinstance(ledger, Mapping) or ledger.get("model_id") != PATCH6_TYPE7_MODEL_ID:
            return False
    scores: dict[str, float] = {}
    for dimension in weights:
        value = _finite_number(sub_scores.get(dimension))
        if value is None or not 0.0 <= value <= 10.0:
            return False
        scores[dimension] = value

    status = reasons.get("_status")
    applicable_marker = reasons.get("_applicable")
    evidence_marker = reasons.get("_evidence")
    if applicable_marker not in {"yes", "no"} or evidence_marker not in {"complete", "incomplete"}:
        return False
    applicable = applicable_marker == "yes"
    evidence_complete = evidence_marker == "complete"
    requirements_valid, missing_requirements = _audit_decision_missing_requirements(reasons)
    patch7_veto_valid, patch7_veto = _audit_patch7_veto_match(
        type_key,
        reasons,
        company=company,
        dcf_result=dcf_result,
    )
    if (
        not requirements_valid
        or not patch7_veto_valid
        or (missing_requirements and (not applicable or evidence_complete or status != "insufficient_evidence"))
        or not _audit_patch7_requirements_match(
            type_key,
            missing_requirements,
            reasons=reasons,
            company=company,
            dcf_result=dcf_result,
        )
    ):
        return False
    raw_missing = reasons.get("_decision_missing_dimensions")
    if not isinstance(raw_missing, list):
        return False
    missing = [dimension for dimension in weights if dimension in raw_missing]
    if not evidence_complete and not missing and not missing_requirements:
        missing = list(weights)
    type6_position_input = bool(type_key == "type6" and reasons.get("_condition") == "须确认实际仓位符合建议上限")
    if type6_position_input and "6e" not in missing:
        missing.append("6e")
        missing = [dimension for dimension in weights if dimension in missing]
    action_condition = bool(type6_position_input and set(missing) == {"6e"})

    lower_dimensions = dict(scores)
    upper_dimensions = dict(scores)
    for dimension in missing:
        lower_dimensions[dimension] = 0.0
        upper_dimensions[dimension] = 10.0
    if type_key == "type7" and missing:
        ledger = payload.get("ledger")
        if isinstance(ledger, Mapping) and ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
            raw_dimensions = ledger.get("dimensions")
            mapping = {"7a": "BM", "7b": "MOAT", "7c": "G"}
            classification = ledger.get("classification")
            route_complete = isinstance(classification, Mapping) and classification.get("route_complete") is True
            if not isinstance(raw_dimensions, Mapping):
                return False
            for dimension in missing:
                if not route_complete:
                    upper_dimensions[dimension] = 10.0
                    continue
                section = raw_dimensions.get(mapping[dimension])
                value = _finite_number(section.get("upper_bound")) if isinstance(section, Mapping) else None
                if value is None or not 0.0 <= value <= 10.0:
                    return False
                upper_dimensions[dimension] = value
        else:
            raw_upper = ledger.get("decisive_score_upper_bounds") if isinstance(ledger, Mapping) else None
            mapping = {"7a": "template1", "7b": "template5", "7c": "patch5"}
            if not isinstance(raw_upper, Mapping):
                return False
            for dimension in missing:
                value = _finite_number(raw_upper.get(mapping[dimension]))
                if value is None or not 0.0 <= value <= 100.0:
                    return False
                upper_dimensions[dimension] = min(10.0, value / 10.0)

    lower = round(math.fsum(lower_dimensions[key] * weights[key] for key in weights), 1)
    upper = round(math.fsum(upper_dimensions[key] * weights[key] for key in weights), 1)
    if type_key == "type3" and "3e" not in missing and upper_dimensions["3e"] <= 3.0:
        lower = min(lower, 4.9)
        upper = min(upper, 4.9)

    if type_key == "type2":
        possible_veto = "2c" in missing
        if set(missing).intersection({"2a", "2b"}):
            lower_hot = math.fsum(0.0 if key in missing else upper_dimensions[key] for key in ("2a", "2b")) / 2.0
            possible_veto = possible_veto or lower_hot <= 4.0
        bounded_veto = False
    elif type_key == "type4":
        possible_veto = bool(
            "4c" in missing
            or (
                set(missing).intersection({"4e", "4f"})
                and (0.0 if "4e" in missing else upper_dimensions["4e"]) <= 3.0
                and (0.0 if "4f" in missing else upper_dimensions["4f"]) <= 3.0
            )
        )
        bounded_veto = False
    elif type_key == "type6":
        core = ("6a", "6b", "6c", "6d")
        known_high = sum(key not in missing and upper_dimensions[key] >= 5.0 for key in core)
        maximum_high = known_high + sum(key in missing for key in core)
        bounded_veto = maximum_high < 2
        possible_veto = not bounded_veto and known_high < 2
    elif (
        type_key == "type7"
        and isinstance(payload.get("ledger"), Mapping)
        and payload["ledger"].get("model_id") == PATCH6_TYPE7_MODEL_ID
    ):
        classification = payload["ledger"].get("classification")
        route_complete = isinstance(classification, Mapping) and classification.get("route_complete") is True
        class_code = str(classification.get("class_code") or "") if isinstance(classification, Mapping) else ""
        bounded_veto = False
        possible_veto = bool((not route_complete) or (class_code == "C" and set(missing).intersection({"7a", "7b"})))
    else:
        bounded_veto = False
        possible_veto = bool(set(missing).intersection(_AUDIT_DECISION_POTENTIAL_VETO_DIMENSIONS[type_key]))

    market_context = payload.get("decision_market_context")
    if (
        not isinstance(market_context, Mapping)
        or set(market_context) != _AUDIT_DECISION_MARKET_CONTEXT_FIELDS
        or (market_context.get("tradable") is not None and type(market_context.get("tradable")) is not bool)
        or type(market_context.get("reference_price")) is not bool
        or not isinstance(market_context.get("risk_status"), str)
    ):
        return False
    risk_status = str(market_context["risk_status"]).strip()
    market_block_reason = (
        "标的不可交易"
        if market_context["tradable"] is False
        else "仅参考价不得触发买入"
        if market_context["reference_price"]
        else f"风险状态:{risk_status}"
        if risk_status and risk_status.lower() not in {"正常", "normal", "active", "ok"}
        else None
    )
    market_blocked = market_block_reason is not None
    if bool(reasons.get("_decision_market_block")) != market_blocked or (
        market_blocked and reasons.get("_decision_market_block") != _audit_compact_decision_reason(market_block_reason)
    ):
        return False

    missing_set = set(missing)

    def known(key: str) -> bool:
        return key not in missing_set

    if type_key == "type1":
        confirmed_hard_veto = bool(
            (known("1a") and upper_dimensions["1a"] <= 2.0) or (known("1b") and upper_dimensions["1b"] <= 3.0)
        )
    elif type_key == "type2":
        confirmed_hard_veto = bool(
            (known("2a") and known("2b") and (upper_dimensions["2a"] + upper_dimensions["2b"]) / 2.0 <= 4.0)
            or (known("2c") and upper_dimensions["2c"] <= 3.0)
        )
    elif type_key == "type3":
        confirmed_hard_veto = bool(
            (known("3a") and upper_dimensions["3a"] <= 3.0) or (known("3d") and upper_dimensions["3d"] <= 3.0)
        )
    elif type_key == "type4":
        confirmed_hard_veto = bool(
            (known("4c") and upper_dimensions["4c"] <= 3.0)
            or (known("4e") and known("4f") and upper_dimensions["4e"] <= 3.0 and upper_dimensions["4f"] <= 3.0)
        )
    elif type_key == "type5":
        confirmed_hard_veto = False
    elif type_key == "type6":
        confirmed_hard_veto = bool(
            all(known(key) for key in ("6a", "6b", "6c", "6d"))
            and sum(upper_dimensions[key] >= 5.0 for key in ("6a", "6b", "6c", "6d")) < 2
        )
    else:
        ledger = payload.get("ledger")
        if isinstance(ledger, Mapping) and ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
            confirmed_hard_veto = ledger.get("veto") is True
        else:
            patch5 = ledger.get("patch5") if isinstance(ledger, Mapping) else None
            safety_score = _finite_number(patch5.get("safety_margin_score")) if isinstance(patch5, Mapping) else None
            confirmed_hard_veto = bool(
                isinstance(patch5, Mapping)
                and patch5.get("safety_margin_complete") is True
                and safety_score is not None
                and safety_score < 8.0
            )
    if patch7_veto is not None:
        confirmed_hard_veto = True
    if not applicable:
        lower = upper = 0.0
        complete, basis, veto_state, potential = True, "scope_exclusion", "none", False
        missing = []
    elif confirmed_hard_veto or bounded_veto:
        complete, basis, veto_state, potential = True, "confirmed_veto", "confirmed", False
    elif market_blocked:
        complete, basis, veto_state, potential = True, "market_block", "none", False
    else:
        veto_state = "possible" if possible_veto else "none"
        theoretical = upper >= _AUDIT_QUALIFY_THRESHOLD
        if type_key == "type1":
            theoretical = theoretical and ("1a" in missing or upper_dimensions["1a"] >= 5.0)
        elif type_key == "type2":
            hot_upper = (upper_dimensions["2a"] + upper_dimensions["2b"]) / 2.0
            valuation_possible = bool(
                upper_dimensions["2d"] >= 5.0
                or (hot_upper >= 7.0 and upper_dimensions["2c"] >= 7.0 and 4.0 <= upper_dimensions["2d"] <= 5.0)
            )
            theoretical = theoretical and valuation_possible
        elif type_key == "type4":
            theoretical = theoretical and upper_dimensions["4a"] >= 5.0
        elif type_key == "type6":
            theoretical = theoretical and sum(upper_dimensions[key] >= 5.0 for key in ("6a", "6b", "6c", "6d")) >= 2
            if "6e" not in missing:
                theoretical = theoretical and (upper_dimensions["6e"] >= 8.0 and not reasons.get("_condition"))
        elif type_key == "type7":
            ledger = payload.get("ledger")
            if isinstance(ledger, Mapping) and ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
                exact_upper = _finite_number(ledger.get("upper_bound"))
                failures = ledger.get("condition_failures")
                theoretical = theoretical and bool(
                    exact_upper is not None
                    and exact_upper > _AUDIT_QUALIFY_THRESHOLD
                    and isinstance(failures, list)
                    and not failures
                )
            else:
                theoretical = all(upper_dimensions[key] > _AUDIT_QUALIFY_THRESHOLD for key in weights)

        type7_action_condition = bool(
            type_key == "type7"
            and isinstance(ledger, Mapping)
            and ledger.get("quality_certified") is True
            and ledger.get("complete") is not True
            and any(
                isinstance(gate, Mapping) and gate.get("complete") is not True
                for gate in (
                    ledger.get("decision_gates", {}).values()
                    if isinstance(ledger.get("decision_gates"), Mapping)
                    else []
                )
            )
        )
        if action_condition or type7_action_condition:
            complete = not theoretical
            basis = "action_condition" if theoretical else "conservative_upper_bound"
            potential = theoretical
        elif evidence_complete:
            complete, basis, potential = True, "full_evidence", theoretical
        elif not theoretical:
            complete, basis, potential = True, "conservative_upper_bound", False
        else:
            complete, basis, potential = False, "unresolved_missing_evidence", True

    expected = {
        "schema_version": 1,
        "model_id": "buy-decision-bounds-v1",
        "decision_complete": complete,
        "decision_basis": basis,
        "score_lower_bound": lower,
        "score_upper_bound": upper,
        "veto_state": veto_state,
        "potentially_triggerable": potential,
        "missing_dimensions": missing,
    }
    for key, value in expected.items():
        actual = decision.get(key)
        if key in {"score_lower_bound", "score_upper_bound"}:
            numeric = _finite_number(actual)
            if numeric is None or not math.isclose(numeric, float(value), rel_tol=0.0, abs_tol=1e-12):
                return False
        elif actual != value:
            return False
    expected_trigger = bool(basis == "full_evidence" and potential)
    market_rewrite_veto = bool(market_blocked and status == "blocked" and not reasons.get("_blocked"))
    if (
        payload.get("triggered") is not expected_trigger
        or (bool(reasons.get("_veto")) and not confirmed_hard_veto and not market_rewrite_veto)
        or (status == "vetoed" and not confirmed_hard_veto)
    ):
        return False
    return bool(
        payload.get("status") == status
        and payload.get("applicable") is applicable
        and payload.get("evidence_complete") is evidence_complete
        and payload.get("veto") is bool(reasons.get("_veto"))
        and payload.get("triggered") is (status == "triggered")
    )


def _valid_full_market_screening_coverage(value: Any, eligible_universe_size: Any) -> bool:
    """Replay full-market integrity and visibility separately from ideal data."""

    if (
        isinstance(eligible_universe_size, bool)
        or not isinstance(eligible_universe_size, int)
        or eligible_universe_size <= 0
        or not isinstance(value, Mapping)
    ):
        return False
    readiness_keys = {
        "all_framework_payloads_present",
        "all_sub_scores_valid",
        "all_applicable_frameworks_evidence_complete",
        "all_incomplete_frameworks_explained",
        "all_quantitative_evidence_records_valid",
        "no_missing_quantitative_evidence",
        "no_partial_quantitative_evidence",
        "all_decision_contracts_valid",
        "all_potential_candidates_visible",
        "all_candidate_recall_paths_safe",
        "artifact_integrity_ready",
        "candidate_visibility_ready",
        "candidate_recall_ready",
        "ideal_zero_gap_ready",
        "ready",
    }
    readiness = value.get("goal_readiness")
    if not isinstance(readiness, Mapping) or set(readiness) != readiness_keys:
        return False
    framework_contract = value.get("framework_evidence_contract")
    if not isinstance(framework_contract, Mapping) or set(framework_contract) != set(_AUDIT_TYPE_WEIGHTS):
        return False
    for contract in framework_contract.values():
        decision_count_fields = (
            "decision_complete",
            "decision_incomplete",
            "decision_visible",
            "decision_hidden",
            "recall_safe",
            "recall_unsafe",
            "potentially_triggerable",
        )
        if not isinstance(contract, Mapping) or any(
            type(contract.get(field)) is not int or contract.get(field) < 0 for field in decision_count_fields
        ):
            return False
        if (
            contract.get("rows") != eligible_universe_size
            or contract.get("valid_sub_scores") != eligible_universe_size
            or contract.get("valid_decision") != eligible_universe_size
            or contract.get("decision_complete", 0) + contract.get("decision_incomplete", 0) != eligible_universe_size
            or contract.get("decision_visible", 0) + contract.get("decision_hidden", 0) != eligible_universe_size
            or contract.get("recall_safe", 0) + contract.get("recall_unsafe", 0) != eligible_universe_size
            or not 0 <= contract.get("potentially_triggerable") <= eligible_universe_size
            or any(
                contract.get(field) != 0
                for field in (
                    "invalid_payload",
                    "invalid_sub_scores",
                    "invalid_applicability",
                    "invalid_evidence_complete",
                    "incomplete_without_reason",
                    "invalid_decision",
                    "decision_hidden",
                    "recall_unsafe",
                )
            )
        ):
            return False
    contract = value.get("quantitative_evidence_contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("model_id") != QUANTITATIVE_EVIDENCE_MODEL_ID
        or contract.get("expected_metrics") != sorted(_AUDIT_QUANTITATIVE_EVIDENCE_KEYS)
        or contract.get("expected_metrics_per_row") != len(_AUDIT_QUANTITATIVE_EVIDENCE_KEYS)
        or contract.get("summary_columns_present") is not True
        or contract.get("rows") != eligible_universe_size
        or contract.get("valid_rows") != eligible_universe_size
        or contract.get("invalid_rows") != 0
        or contract.get("invalid_examples") != []
        or any(
            contract.get(field) != 0
            for field in (
                "missing_column",
                "non_mapping",
                "key_mismatch",
                "invalid_record",
                "invalid_level",
                "attachment_mismatch",
                "summary_columns_missing",
                "summary_columns_mismatch",
                "levels_mismatch",
                "status_mismatch",
            )
        )
    ):
        return False
    level_counts = value.get("quantitative_evidence_level_counts")
    if not isinstance(level_counts, Mapping) or any(
        key not in _AUDIT_QUANTITATIVE_LEVELS or isinstance(count, bool) or not isinstance(count, int) or count < 0
        for key, count in level_counts.items()
    ):
        return False
    if sum(level_counts.values()) != eligible_universe_size * len(_AUDIT_QUANTITATIVE_EVIDENCE_KEYS) or not isinstance(
        value.get("quantitative_evidence_gap_examples"), list
    ):
        return False

    all_applicable_complete = all(
        contract.get("applicable_evidence_incomplete") == 0 for contract in framework_contract.values()
    )
    no_missing = level_counts.get("missing", 0) == 0
    no_partial = level_counts.get("partial", 0) == 0
    expected = {
        "all_framework_payloads_present": True,
        "all_sub_scores_valid": True,
        "all_applicable_frameworks_evidence_complete": all_applicable_complete,
        "all_incomplete_frameworks_explained": True,
        "all_quantitative_evidence_records_valid": True,
        "no_missing_quantitative_evidence": no_missing,
        "no_partial_quantitative_evidence": no_partial,
        "all_decision_contracts_valid": True,
        "all_potential_candidates_visible": True,
        "all_candidate_recall_paths_safe": True,
        "artifact_integrity_ready": True,
        "candidate_visibility_ready": True,
        "candidate_recall_ready": True,
        "ideal_zero_gap_ready": all_applicable_complete and no_missing and no_partial,
        "ready": all_applicable_complete and no_missing and no_partial,
    }
    return bool(
        all(readiness.get(key) is expected_value for key, expected_value in expected.items())
        and readiness.get("artifact_integrity_ready") is True
        and readiness.get("candidate_visibility_ready") is True
    )


def _audit_company_codes(
    payload: Mapping[str, Any],
    *,
    patch4_bindings: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> list[str] | None:
    companies = payload.get("companies")
    dcf_results = payload.get("dcf_results")
    if not isinstance(companies, list) or len(companies) != 100 or not isinstance(dcf_results, Mapping):
        return None
    codes: list[str] = []
    for company in companies:
        if not isinstance(company, Mapping):
            return None
        code = str(company.get("code", ""))
        if re.fullmatch(r"[036][0-9]{5}", code) is None:
            return None
        required_fields = {
            "name",
            "industry",
            "source_trade_date",
            "price",
            "market_cap",
            "buy_types",
            "num_types",
            "primary_type",
            "primary_label",
            "diagnostic_type",
            "diagnostic_label",
            "max_score",
            "bear_case",
        }
        required_fields.update(_AUDIT_TYPE_DIMENSIONS)
        required_fields.update(f"{type_key}_score" for type_key in _AUDIT_TYPE_DIMENSIONS)
        if not required_fields.issubset(company):
            return None
        if not str(company.get("name") or "").strip() or str(company.get("industry") or "").strip() in {"", "DEFAULT"}:
            return None
        source_trade_date = _audit_type7_history_date(company.get("source_trade_date"))
        if source_trade_date is None or source_trade_date > shanghai_today():
            return None
        if (_finite_number(company.get("price")) or 0) <= 0 or (_finite_number(company.get("market_cap")) or 0) <= 0:
            return None
        if not _audit_quantitative_evidence_valid(company, code):
            return None

        triggered_set: set[str] = set()
        clean_sub_scores: dict[str, dict[str, float]] = {}
        clean_reasons: dict[str, Mapping[str, Any]] = {}
        diagnostic_totals: dict[str, float] = {}
        for type_key, dimensions in _AUDIT_TYPE_DIMENSIONS.items():
            type_payload = company.get(type_key)
            if not isinstance(type_payload, Mapping):
                return None
            if (
                not isinstance(type_payload.get("triggered"), bool)
                or not isinstance(type_payload.get("veto"), bool)
                or not isinstance(type_payload.get("applicable"), bool)
                or not isinstance(type_payload.get("evidence_complete"), bool)
            ):
                return None
            total = _finite_number(type_payload.get("total"))
            sub_scores = type_payload.get("sub_scores")
            reasons = type_payload.get("reasons")
            if (
                total is None
                or not 0 <= total <= 10
                or not isinstance(sub_scores, Mapping)
                or set(sub_scores) != dimensions
                or not isinstance(reasons, Mapping)
                or not dimensions.issubset(reasons)
                or any(not str(key).startswith("_") for key in set(reasons) - dimensions)
                or any(
                    (score := _finite_number(sub_scores.get(dimension))) is None or not 0 <= score <= 10
                    for dimension in dimensions
                )
                or any(
                    not str(reasons.get(dimension) or "").strip()
                    or len(str(reasons.get(dimension))) > _AUDIT_REASON_MAX_LENGTH
                    for dimension in dimensions
                )
                or any(
                    not str(value or "").strip() or len(str(value)) > _AUDIT_REASON_MAX_LENGTH
                    for key, value in reasons.items()
                    if key
                    not in {
                        "_status",
                        "_applicable",
                        "_evidence",
                        "_decision_missing_dimensions",
                        _AUDIT_DECISION_MISSING_REQUIREMENTS_REASON,
                    }
                )
            ):
                return None
            scores_for_type = {
                dimension: float(_finite_number(sub_scores[dimension])) for dimension in _AUDIT_TYPE_WEIGHTS[type_key]
            }
            total_decimals = 3 if type_key == "type7" else 1
            raw_total = round(
                sum(scores_for_type[dimension] * weight for dimension, weight in _AUDIT_TYPE_WEIGHTS[type_key].items()),
                total_decimals,
            )
            expected_total = min(raw_total, 4.9) if type_key == "type3" and scores_for_type["3e"] <= 3 else raw_total
            if (
                not math.isclose(total, expected_total, rel_tol=0.0, abs_tol=1e-9)
                or _finite_number(company.get(f"{type_key}_score")) != total
                or (
                    type_key == "type3"
                    and (
                        (scores_for_type["3e"] <= 3 and raw_total > 4.9 and not reasons.get("_downgrade"))
                        or (scores_for_type["3e"] > 3 and reasons.get("_downgrade"))
                    )
                )
            ):
                return None
            reason_veto = bool(reasons.get("_veto"))
            status = type_payload.get("status")
            applicable = type_payload["applicable"]
            evidence_complete = type_payload["evidence_complete"]
            triggered = type_payload["triggered"]
            condition = bool(reasons.get("_condition"))
            patch7_fact_context = bool(
                _AUDIT_DECISION_MISSING_REQUIREMENTS_REASON in reasons
                or str(reasons.get("_missing") or "").startswith("补丁7")
                or _AUDIT_DECISION_PATCH7_VETO_REASON in reasons
            )
            if not _audit_decision_contract_valid(
                type_key,
                type_payload,
                company=company if patch7_fact_context else None,
                dcf_result=(dcf_results.get(code) if patch7_fact_context else _AUDIT_DECISION_FACT_UNSET),
            ):
                return None
            if type_key == "type5":
                if not _audit_type5_bottom_evidence_valid(code, company, type_payload):
                    return None
                if not _audit_type5_official_context_valid(company, type_payload):
                    return None
            if type_key == "type7":
                ledger = type_payload.get("ledger")
                if not _audit_type7_ledger_valid(
                    code,
                    ledger,
                    status,
                    patch4_bindings=(patch4_bindings.get(code) if isinstance(patch4_bindings, Mapping) else None),
                ):
                    return None
                if status != "not_applicable" and isinstance(ledger, Mapping):
                    source_scores = ledger.get("scores")
                    if not isinstance(source_scores, Mapping):
                        return None
                    if ledger.get("model_id") == PATCH6_TYPE7_MODEL_ID:
                        source_values = {key: _finite_number(source_scores.get(key)) for key in ("BM", "MOAT", "G")}
                        if any(value is None for value in source_values.values()):
                            return None
                        expected_type7_scores = {
                            "7a": round(source_values["BM"], 3),
                            "7b": round(source_values["MOAT"], 3),
                            "7c": round(source_values["G"], 3),
                        }
                    else:
                        source_values = {
                            key: _finite_number(source_scores.get(key)) for key in ("template1", "template5", "patch5")
                        }
                        if any(value is None for value in source_values.values()):
                            return None
                        expected_type7_scores = {
                            "7a": round(source_values["template1"] / 10.0, 3),
                            "7b": round(source_values["template5"] / 10.0, 3),
                            "7c": round(source_values["patch5"] / 10.0, 3),
                        }
                    if scores_for_type != expected_type7_scores:
                        return None
                    if status != "blocked" and triggered is not ledger.get("triggered"):
                        return None
            if (
                type_payload["veto"] != reason_veto
                or status not in _AUDIT_TYPE_STATUSES
                or reasons.get("_status") != status
                or reasons.get("_applicable") != ("yes" if applicable else "no")
                or reasons.get("_evidence") != ("complete" if evidence_complete else "incomplete")
                or applicable != (status != "not_applicable")
                or evidence_complete != (reasons.get("_evidence") == "complete")
                or triggered != (status == "triggered")
            ):
                return None
            if status in _AUDIT_NON_DIAGNOSTIC_STATUSES and (reason_veto or triggered):
                return None
            if status == "triggered" and (total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or condition):
                return None
            if status == "triggered" and not evidence_complete:
                return None
            if status == "conditional" and (total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or not condition):
                return None
            if status == "vetoed" and not reason_veto:
                return None
            if status == "blocked" and not (reason_veto or str(reasons.get("_blocked") or "").strip()):
                return None
            if status == "observe" and (reason_veto or not 5.0 <= total < _AUDIT_QUALIFY_THRESHOLD):
                return None
            type7_decisive_failure = bool(
                type_key == "type7"
                and isinstance(type_payload.get("ledger"), Mapping)
                and type_payload["ledger"].get("decisively_not_triggered") is True
            )
            if status == "not_triggered" and (reason_veto or (total >= 5.0 and not type7_decisive_failure)):
                return None
            clean_sub_scores[type_key] = scores_for_type
            clean_reasons[type_key] = reasons
            if status not in _AUDIT_NON_DIAGNOSTIC_STATUSES:
                diagnostic_totals[type_key] = total
            if triggered:
                triggered_set.add(type_key)

        triggered_types = [type_key for type_key in _AUDIT_TYPE_PRIORITY if type_key in triggered_set]
        buy_types = company.get("buy_types")
        if buy_types != triggered_types or company.get("num_types") != len(triggered_types):
            return None
        expected_primary = triggered_types[0] if triggered_types else None
        expected_primary_label = _AUDIT_TYPE_NAMES[expected_primary] if expected_primary else "无触发（不买）"
        if company.get("primary_type") != expected_primary or company.get("primary_label") != expected_primary_label:
            return None
        expected_diagnostic: str | None = None
        if diagnostic_totals:
            maximum = max(diagnostic_totals.values())
            expected_diagnostic = next(
                type_key for type_key in _AUDIT_TYPE_PRIORITY if diagnostic_totals.get(type_key) == maximum
            )
            if (
                company.get("diagnostic_type") != expected_diagnostic
                or company.get("diagnostic_label") != _AUDIT_TYPE_NAMES[expected_diagnostic]
                or _finite_number(company.get("max_score")) != maximum
            ):
                return None
        elif (
            company.get("diagnostic_type") is not None
            or company.get("diagnostic_label") != "无可完整诊断框架"
            or company.get("max_score") is not None
        ):
            return None
        bear_case = company.get("bear_case")
        if expected_diagnostic is None:
            if bear_case != []:
                return None
            codes.append(code)
            continue
        if not isinstance(bear_case, list) or len(bear_case) != 3:
            return None
        expected_bear_case = _expected_audit_bear_case(
            expected_diagnostic,
            clean_sub_scores[expected_diagnostic],
            clean_reasons[expected_diagnostic],
        )
        if bear_case != expected_bear_case:
            return None
        codes.append(code)
    return codes


def _audit_type1_valuation_binding_valid(
    company: Mapping[str, Any],
    *,
    expected_type1_1a: float | None,
    skip_classification: Mapping[str, str] | None,
) -> bool:
    """Bind Type 1 to a checked valuation result or exact skip semantics."""

    payload = company.get("type1")
    if not isinstance(payload, Mapping):
        return False
    sub_scores = payload.get("sub_scores")
    reasons = payload.get("reasons")
    if not isinstance(sub_scores, Mapping) or not isinstance(reasons, Mapping):
        return False
    score_1a = _finite_number(sub_scores.get("1a"))
    total = _finite_number(payload.get("total"))
    status = payload.get("status")
    applicable = payload.get("applicable")
    evidence_complete = payload.get("evidence_complete")
    triggered = payload.get("triggered")
    veto = payload.get("veto")

    if expected_type1_1a is not None:
        return bool(
            skip_classification is None
            and applicable is True
            and status != "not_applicable"
            and score_1a is not None
            and math.isclose(score_1a, expected_type1_1a, rel_tol=0.0, abs_tol=1e-9)
            and (expected_type1_1a > 2.0 or (veto is True and status in {"vetoed", "blocked"}))
        )

    if (
        not isinstance(skip_classification, Mapping)
        or set(skip_classification) != {"category", "reason"}
        or not isinstance(skip_classification.get("category"), str)
        or skip_classification.get("category") not in _AUDIT_DCF_SKIP_CATEGORIES
        or not str(skip_classification.get("reason") or "").strip()
    ):
        return False
    scores = [_finite_number(sub_scores.get(key)) for key in ("1a", "1b", "1c", "1d")]
    if (
        any(score is None or not math.isclose(score, 0.0, rel_tol=0.0, abs_tol=1e-9) for score in scores)
        or total is None
        or not math.isclose(total, 0.0, rel_tol=0.0, abs_tol=1e-9)
        or triggered is not False
    ):
        return False
    category = skip_classification["category"]
    if category in {"model_unsupported", "economic_not_applicable"}:
        expected = ("not_applicable", False, True, False)
    elif category in {"source_missing", "inconsistent_source"}:
        expected = ("insufficient_evidence", True, False, False)
    else:
        expected = ("blocked", True, False, False)
        if not str(reasons.get("_blocked") or "").strip():
            return False
    return (status, applicable, evidence_complete, veto) == expected


def _audit_valuation_bindings_valid(
    companies: Any,
    *,
    expected_type1_1a: Mapping[str, float],
    expected_nonfinancial_type1_1a: Mapping[str, float],
    skip_classifications: Mapping[str, Mapping[str, str]],
    financial_codes: set[str],
) -> bool:
    """Cross-check Type 1 and Type 7 claims against independent valuations."""

    if not isinstance(companies, list):
        return False
    for company in companies:
        if not isinstance(company, Mapping):
            return False
        code = str(company.get("code") or "")
        industry = str(company.get("industry") or "")
        if not _audit_type1_valuation_binding_valid(
            company,
            expected_type1_1a=expected_type1_1a.get(code),
            skip_classification=(skip_classifications.get(code) if code not in expected_type1_1a else None),
        ):
            return False
        type7 = company.get("type7")
        if not isinstance(type7, Mapping):
            return False
        status = type7.get("status")
        applicable = type7.get("applicable")
        ledger = type7.get("ledger")
        is_financial = industry in _AUDIT_TYPE7_FINANCIAL_INDUSTRIES
        if is_financial != (code in financial_codes):
            return False
        if is_financial:
            if (
                status != "not_applicable"
                or applicable is not False
                or not isinstance(ledger, Mapping)
                or ledger.get("applicable") is not False
            ):
                return False
            continue
        if status == "not_applicable" or applicable is not True or not isinstance(ledger, Mapping):
            return False
        if ledger.get("model_id") != PATCH6_TYPE7_MODEL_ID:
            return False

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
                or ledger.get("as_of") != str(company.get("source_trade_date") or "")
            ):
                return False
            if class_code != "W":
                valuation_inputs = valuation.get("inputs")
                current_pb = (
                    _finite_number(valuation_inputs.get("current_pb"))
                    if isinstance(valuation_inputs, Mapping)
                    else None
                )
                company_pb = _finite_number(company.get("pb"))
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
                            or company_pb is None
                            or current_pb <= 0
                            or company_pb <= 0
                            or abs(current_pb - company_pb) / max(current_pb, company_pb) > 0.20
                        )
                    )
                ):
                    return False
                if class_code == "C":
                    route_inputs = route.get("inputs")
                    type5 = company.get("type5")
                    type5_scores = type5.get("sub_scores") if isinstance(type5, Mapping) else None
                    type5_reasons = type5.get("reasons") if isinstance(type5, Mapping) else None
                    missing = (
                        type5_reasons.get("_decision_missing_dimensions")
                        if isinstance(type5_reasons, Mapping)
                        else None
                    )
                    missing_set = set(missing) if isinstance(missing, list) else set()
                    type5_status = str(type5.get("status") or "") if isinstance(type5, Mapping) else ""
                    cycle_score = _finite_number(type5_scores.get("5a")) if isinstance(type5_scores, Mapping) else None
                    survival_score = (
                        _finite_number(type5_scores.get("5c")) if isinstance(type5_scores, Mapping) else None
                    )
                    valuation_score = (
                        _finite_number(type5_scores.get("5e")) if isinstance(type5_scores, Mapping) else None
                    )
                    expected_route_inputs = {
                        "type5_applicable": type5_status != "not_applicable",
                        "type5_cycle_complete": bool(
                            type5_status != "not_applicable" and cycle_score is not None and "5a" not in missing_set
                        ),
                        "type5_cycle_score": cycle_score,
                        "type5_survival_complete": bool(
                            type5_status != "not_applicable" and survival_score is not None and "5c" not in missing_set
                        ),
                        "type5_survival_score": survival_score,
                        "type5_valuation_complete": bool(
                            type5_status != "not_applicable" and valuation_score is not None and "5e" not in missing_set
                        ),
                        "type5_valuation_score": valuation_score,
                    }
                    if route_inputs != expected_route_inputs:
                        return False
                continue

            type1 = company.get("type1")
            type1_scores = type1.get("sub_scores") if isinstance(type1, Mapping) else None
            type1_1a = _finite_number(type1_scores.get("1a")) if isinstance(type1_scores, Mapping) else None
            ledger_score = _finite_number(valuation.get("buy_zone_score")) if isinstance(valuation, Mapping) else None
            expected_nonfinancial_score = expected_nonfinancial_type1_1a.get(code)
            expected_complete = expected_nonfinancial_score is not None
            expected_score = expected_nonfinancial_score if expected_nonfinancial_score is not None else type1_1a
            price_required = valuation.get("required") is True if isinstance(valuation, Mapping) else True
            if (
                not isinstance(valuation, Mapping)
                or valuation.get("source_evidence_complete") is not expected_complete
                or type(valuation.get("required")) is not bool
                or valuation.get("complete") is not (expected_complete or not price_required)
                or type1_1a is None
                or ledger_score is None
                or expected_score is None
                or not math.isclose(type1_1a, expected_score, rel_tol=0.0, abs_tol=1e-9)
                or not math.isclose(ledger_score, expected_score, rel_tol=0.0, abs_tol=1e-9)
            ):
                return False
            continue

        prerequisites = ledger.get("prerequisites")
        valuation = prerequisites.get("latest_quote_and_valuation") if isinstance(prerequisites, Mapping) else None
        template1 = ledger.get("template1")
        items = template1.get("items") if isinstance(template1, Mapping) else None
        if not isinstance(items, list):
            return False
        matching = [item for item in items if isinstance(item, Mapping) and item.get("key") == "t1_20"]
        if len(matching) != 1 or not isinstance(valuation, Mapping):
            return False
        t1_20 = matching[0]
        type1 = company.get("type1")
        type1_scores = type1.get("sub_scores") if isinstance(type1, Mapping) else None
        inputs = t1_20.get("inputs")
        type1_1a = _finite_number(type1_scores.get("1a")) if isinstance(type1_scores, Mapping) else None
        input_score = _finite_number(inputs.get("type1_1a")) if isinstance(inputs, Mapping) else None
        item_score = _finite_number(t1_20.get("score"))
        expected_nonfinancial_score = expected_nonfinancial_type1_1a.get(code)
        expected_complete = expected_nonfinancial_score is not None
        expected_level = "validated_nonfinancial_dcf" if expected_complete else "partial"
        expected_score = expected_nonfinancial_score if expected_nonfinancial_score is not None else 0.0
        if (
            valuation.get("valuation_complete") is not expected_complete
            or t1_20.get("complete") is not expected_complete
            or t1_20.get("evidence_level") != expected_level
            or type1_1a is None
            or input_score is None
            or item_score is None
            or not math.isclose(type1_1a, expected_score, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(input_score, expected_score, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(item_score, expected_score, rel_tol=0.0, abs_tol=1e-9)
        ):
            return False
    return True


def _expected_type1_1a_from_dcf(result: Mapping[str, Any]) -> float | None:
    """Replay Type 1's price-position bucket from a validated DCF result."""

    price = _finite_number(result.get("current_price"))
    buy_upper = _finite_number(result.get("buy_zone_upper"))
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


def _audit_csv_rows(content: bytes) -> list[dict[str, str]] | None:
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None or "代码" not in reader.fieldnames:
            return None
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error):
        return None
    if len(rows) != 100:
        return None
    codes = [str(row.get("代码", "")) for row in rows]
    if any(re.fullmatch(r"[036][0-9]{5}", code) is None for code in codes):
        return None
    return rows


def _csv_bool(value: object) -> bool | None:
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _spreadsheet_safe_text(value: object) -> str:
    text = str(value if value is not None else "")
    stripped = text.lstrip()
    if text[:1] in {"\t", "\r", "\n"} or stripped[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _audit_csv_matches_payload(
    rows: list[dict[str, str]],
    companies: list[Mapping[str, Any]],
    dcf_results: Mapping[str, Any],
    dcf_skip_reasons: Mapping[str, Any],
) -> bool:
    required_columns = {
        "代码",
        "名称",
        "行业",
        "买入判定",
        "诊断框架",
        "最高分",
        "触发类型",
        "DCF有效",
        "空头漏洞",
        "DCF状态",
        "DCF跳过原因",
        "DCF区域",
        "DCF当前价",
        "DCF买入区上界",
        "DCF卖出区下界",
    }
    for type_key, dimensions in _AUDIT_TYPE_DIMENSIONS.items():
        required_columns.update({f"{type_key}总分", f"{type_key}触发", f"{type_key}否决", f"{type_key}元信息JSON"})
        for dimension in dimensions:
            required_columns.update({f"{dimension}子分", f"{dimension}依据"})
    if not rows or not required_columns.issubset(rows[0]):
        return False

    company_by_code = {str(company.get("code")): company for company in companies}
    if [row.get("代码") for row in rows] != sorted(company_by_code):
        return False
    for row in rows:
        code = str(row.get("代码"))
        company = company_by_code.get(code)
        if company is None:
            return False
        bear_text = "；".join(
            f"{item.get('dimension')} {item.get('score')}分:{item.get('reason')}"
            for item in company.get("bear_case", [])
            if isinstance(item, Mapping)
        )
        fixed_text = {
            "名称": company.get("name"),
            "行业": company.get("industry"),
            "买入判定": company.get("primary_label"),
            "诊断框架": company.get("diagnostic_label"),
            "触发类型": ",".join(company.get("buy_types", [])),
            "空头漏洞": bear_text,
        }
        if any(row.get(column) != _spreadsheet_safe_text(value) for column, value in fixed_text.items()):
            return False
        if _finite_number(row.get("最高分")) != _finite_number(company.get("max_score")):
            return False

        for type_key, dimensions in _AUDIT_TYPE_DIMENSIONS.items():
            payload = company.get(type_key)
            if not isinstance(payload, Mapping):
                return False
            reasons = payload.get("reasons")
            sub_scores = payload.get("sub_scores")
            if not isinstance(reasons, Mapping) or not isinstance(sub_scores, Mapping):
                return False
            if (
                _finite_number(row.get(f"{type_key}总分")) != _finite_number(payload.get("total"))
                or _csv_bool(row.get(f"{type_key}触发")) != payload.get("triggered")
                or _csv_bool(row.get(f"{type_key}否决")) != payload.get("veto")
            ):
                return False
            expected_meta = json.dumps(
                {key: value for key, value in reasons.items() if str(key).startswith("_")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if row.get(f"{type_key}元信息JSON") != _spreadsheet_safe_text(expected_meta):
                return False
            for dimension in dimensions:
                if _finite_number(row.get(f"{dimension}子分")) != _finite_number(sub_scores.get(dimension)) or row.get(
                    f"{dimension}依据"
                ) != _spreadsheet_safe_text(reasons.get(dimension)):
                    return False

        has_result = code in dcf_results
        if _csv_bool(row.get("DCF有效")) != has_result:
            return False
        if has_result:
            result = dcf_results[code]
            if (
                row.get("DCF状态") != "有效"
                or row.get("DCF跳过原因") != ""
                or row.get("DCF区域") != str(result.get("zone"))
                or _finite_number(row.get("DCF当前价")) != _finite_number(result.get("current_price"))
                or _finite_number(row.get("DCF买入区上界")) != _finite_number(result.get("buy_zone_upper"))
                or _finite_number(row.get("DCF卖出区下界")) != _finite_number(result.get("sell_zone_lower"))
            ):
                return False
        elif row.get("DCF状态") != "跳过" or row.get("DCF跳过原因") != _spreadsheet_safe_text(
            dcf_skip_reasons.get(code)
        ):
            return False
    return True


def _markdown_cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _audit_markdown_matches_payload(
    text: str,
    payload: Mapping[str, Any],
    companies: list[Mapping[str, Any]],
    dcf_results: Mapping[str, Any],
    dcf_skip_reasons: Mapping[str, Any],
) -> bool:
    signal_counts = {
        type_key: sum(bool(company[type_key].get("triggered")) for company in companies)
        for type_key in _AUDIT_TYPE_WEIGHTS
    }
    required_markers = (
        "# 固定随机 100 家公司审计",
        f"- seed: `{_EXPECTED_AUDIT_SEED}`",
        "- sample_size: `100`",
        f"- eligible_universe_size: `{payload.get('eligible_universe_size')}`",
        f"- data_timestamp_utc: `{payload.get('data_timestamp_utc')}`",
        f"- dcf_valid: `{len(dcf_results)}`",
        f"- dcf_skipped_with_reason: `{len(dcf_skip_reasons)}`",
        "- pipeline_issues: `0`",
        "- engine_self_check_errors: `0`",
        "- same_source_scoring_replay_errors: `0`",
        "- same_source_valuation_replay_errors: `0`",
        "- independent_check_errors: `0`",
        f"- triggered_by_type: `{signal_counts}`",
        "## 公司明细",
    )
    if not all(marker in text for marker in required_markers):
        return False
    expected_rows: list[str] = []
    for company in sorted(companies, key=lambda item: str(item.get("code"))):
        code = str(company.get("code"))
        bear_text = "；".join(
            f"{item.get('dimension')} {item.get('score')}分:{item.get('reason')}"
            for item in company.get("bear_case", [])
            if isinstance(item, Mapping)
        )
        dcf_text = "有效" if code in dcf_results else f"跳过:{dcf_skip_reasons.get(code, '无结构化原因')}"
        expected_rows.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    code,
                    company.get("name"),
                    company.get("industry"),
                    company.get("primary_label"),
                    company.get("diagnostic_label"),
                    company.get("max_score"),
                    ",".join(company.get("buy_types", [])),
                    dcf_text,
                    bear_text,
                )
            )
            + " |"
        )
    detail_rows = [line for line in text.splitlines() if re.match(r"^\| [036][0-9]{5} \|", line)]
    return detail_rows == expected_rows


def _close_number(
    left: object,
    right: object,
    *,
    rel_tol: float = 1e-9,
    abs_tol: float = 1e-6,
) -> bool:
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    return (
        left_number is not None
        and right_number is not None
        and math.isclose(left_number, right_number, rel_tol=rel_tol, abs_tol=abs_tol)
    )


def _valid_reporting_period_contract(value: object) -> dict[str, str] | None:
    """Return one canonical calendar-year TTM contract, or fail closed."""
    required = {
        "annual_report_date",
        "current_interim_report_date",
        "prior_interim_report_date",
        "period_basis",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("period_basis") != _TTM_PERIOD_BASIS:
        return None
    raw_dates = tuple(
        value.get(field) for field in ("annual_report_date", "current_interim_report_date", "prior_interim_report_date")
    )
    if not all(isinstance(item, str) for item in raw_dates):
        return None
    try:
        annual, current, prior = (datetime.strptime(item, "%Y-%m-%d") for item in raw_dates)
    except ValueError:
        return None
    if tuple(item.strftime("%Y-%m-%d") for item in (annual, current, prior)) != raw_dates:
        return None
    if not (
        (annual.month, annual.day) == (12, 31)
        and (current.month, current.day) in {(3, 31), (6, 30), (9, 30)}
        and (current.month, current.day) == (prior.month, prior.day)
        and prior.year == annual.year
        and current.year == annual.year + 1
    ):
        return None
    return {key: str(value[key]) for key in required}


def _valid_supplemental_field_coverage(validation: Mapping[str, Any]) -> bool:
    """Require schema-8 field-presence telemetry without inventing a coverage floor."""

    coverage = validation.get("supplemental_field_coverage")
    required_fields = {"GOODWILL", "OBTAIN_SUBSIDIARY_OTHER"}
    if not isinstance(coverage, Mapping) or set(coverage) != required_fields:
        return False
    return all(
        (number := _finite_number(coverage.get(field))) is not None and 0.0 <= number <= 1.0
        for field in required_fields
    )


def _valid_optional_evidence_provenance(value: object, eligible_universe_size: object) -> bool:
    """Validate a compact deep-evidence summary without requiring any candidate count."""

    fields = {
        "provided",
        "evidence_count",
        "available_count",
        "eligible_evidence_count",
        "eligible_evidence_coverage",
        "evidence_sha256",
        "as_of_sessions",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or isinstance(eligible_universe_size, bool)
        or not isinstance(eligible_universe_size, int)
        or eligible_universe_size <= 0
        or not isinstance(value.get("provided"), bool)
    ):
        return False
    counts = tuple(value.get(field) for field in ("evidence_count", "available_count", "eligible_evidence_count"))
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
        return False
    evidence_count, available_count, eligible_count = counts
    coverage = _finite_number(value.get("eligible_evidence_coverage"))
    if (
        available_count > evidence_count
        or eligible_count > evidence_count
        or eligible_count > eligible_universe_size
        or coverage is None
        or not 0.0 <= coverage <= 1.0
        or not math.isclose(
            coverage,
            eligible_count / eligible_universe_size,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or value.get("provided") is not (evidence_count > 0)
    ):
        return False
    sessions = value.get("as_of_sessions")
    if (
        not isinstance(sessions, list)
        or any(not isinstance(session, str) for session in sessions)
        or sessions != sorted(set(sessions))
        or len(sessions) > evidence_count
    ):
        return False
    try:
        if any(date.fromisoformat(session).isoformat() != session for session in sessions):
            return False
    except ValueError:
        return False
    evidence_sha256 = value.get("evidence_sha256")
    if evidence_count == 0:
        return evidence_sha256 is None and sessions == []
    return (
        bool(sessions)
        and isinstance(evidence_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is not None
        and len(set(evidence_sha256)) >= 8
    )


def _patch4_bindings_from_provenance(
    value: object,
    eligible_universe_size: object,
) -> dict[str, dict[str, dict[str, str]]] | None:
    """Validate and index the exact announcement hashes used by Type 7 ledgers."""

    if not isinstance(value, Mapping) or "assessment_evidence_bindings" not in value:
        return None
    base_summary = dict(value)
    raw_bindings = base_summary.pop("assessment_evidence_bindings")
    if not _valid_optional_evidence_provenance(base_summary, eligible_universe_size):
        return None
    if not isinstance(raw_bindings, Mapping):
        return None
    evidence_count = int(base_summary["evidence_count"])
    if len(raw_bindings) > evidence_count:
        return None
    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for raw_code, records in raw_bindings.items():
        code = str(raw_code)
        if (
            re.fullmatch(r"[036][0-9]{5}", code) is None
            or code in normalized
            or not isinstance(records, list)
            or not records
        ):
            return None
        company: dict[str, dict[str, str]] = {}
        for record in records:
            if (
                not isinstance(record, Mapping)
                or set(record) != {"evidence_id", "url", "as_of", "content_sha256"}
                or any(not isinstance(record.get(field), str) for field in record)
            ):
                return None
            clean = {field: str(record[field]).strip() for field in record}
            match = _AUDIT_PATCH4_EVIDENCE_ID.fullmatch(clean["evidence_id"])
            try:
                evidence_date = date.fromisoformat(clean["as_of"])
            except ValueError:
                return None
            if (
                match is None
                or match.group("code") != code
                or clean["evidence_id"] in company
                or clean["url"] != f"{_AUDIT_PATCH4_DETAIL_PREFIX}{code}/{match.group('art_code')}.html"
                or evidence_date.isoformat() != clean["as_of"]
                or re.fullmatch(r"[0-9a-f]{64}", clean["content_sha256"]) is None
                or clean["content_sha256"][:16] != match.group("digest")
                or any(
                    not text or len(text) > 1_000 or any(ord(character) < 32 for character in text)
                    for text in clean.values()
                )
            ):
                return None
            company[clean["evidence_id"]] = clean
        if list(company) != sorted(company):
            return None
        normalized[code] = company
    return normalized


def _valid_release_quote_session(validation: Mapping[str, Any]) -> str | None:
    sessions = validation.get("trading_source_trade_dates")
    analysis_quotes = validation.get("analysis_market_quotes")
    trading_quotes = validation.get("analysis_trading_quotes")
    source_trading_quotes = validation.get("trading_quotes")
    coverage = _finite_number(validation.get("analysis_trading_coverage"))
    source_coverage = _finite_number(validation.get("trading_coverage"))
    eligible_companies = validation.get("eligible_companies")
    eligible_trading_quotes = validation.get("eligible_trading_quotes")
    eligible_trading_coverage = _finite_number(validation.get("eligible_trading_coverage"))
    if (
        not isinstance(sessions, list)
        or len(sessions) != 1
        or not isinstance(sessions[0], str)
        or isinstance(analysis_quotes, bool)
        or not isinstance(analysis_quotes, int)
        or analysis_quotes <= 0
        or isinstance(trading_quotes, bool)
        or not isinstance(trading_quotes, int)
        or not 0 < trading_quotes <= analysis_quotes
        or source_trading_quotes != trading_quotes
        or coverage is None
        or source_coverage is None
        or not math.isclose(coverage, trading_quotes / analysis_quotes, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(source_coverage, coverage, rel_tol=0.0, abs_tol=1e-12)
        or coverage < _MIN_RELEASE_TRADING_QUOTE_COVERAGE
        or isinstance(eligible_companies, bool)
        or not isinstance(eligible_companies, int)
        or eligible_companies <= 0
        or isinstance(eligible_trading_quotes, bool)
        or not isinstance(eligible_trading_quotes, int)
        or not 0 < eligible_trading_quotes <= eligible_companies
        or eligible_trading_coverage is None
        or not math.isclose(
            eligible_trading_coverage,
            eligible_trading_quotes / eligible_companies,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or eligible_trading_coverage < _MIN_RELEASE_TRADING_QUOTE_COVERAGE
    ):
        return None
    try:
        session = date.fromisoformat(sessions[0])
    except ValueError:
        return None
    return sessions[0] if session.isoformat() == sessions[0] else None


def _valid_market_coldness_provenance(
    summary: object,
    status: object,
    reference_artifact: object,
    *,
    eligible_codes: Sequence[str],
    trade_session: str,
    validation: Mapping[str, Any],
) -> bool:
    summary_fields = {
        "provided",
        "evidence_count",
        "eligible_evidence_count",
        "eligible_evidence_coverage",
        "evidence_sha256",
        "sources",
        "as_of_sessions",
    }
    if not isinstance(summary, Mapping) or set(summary) != summary_fields or not isinstance(status, Mapping):
        return False
    eligible = set(eligible_codes)
    if not eligible or len(eligible) != len(eligible_codes):
        return False
    analysis_population = _strict_ttm_analysis_population(validation)
    if analysis_population is None or validation.get("eligible_codes") != list(eligible_codes):
        return False
    listed_codes = sorted(analysis_population)
    if (
        len(listed_codes) != validation.get("analysis_market_quotes")
        or not isinstance(reference_artifact, Mapping)
        or reference_artifact.get("listed_codes") != listed_codes
    ):
        return False
    try:
        replay = _replay_market_coldness_reference_artifact(
            reference_artifact,
            eligible_codes=eligible_codes,
            as_of_session=trade_session,
        )
    except (RuntimeError, TypeError, ValueError, OverflowError):
        return False
    expected_evidence = replay.get("eligible_evidence")
    full_evidence = replay.get("full_evidence")
    if not isinstance(expected_evidence, Mapping) or not isinstance(full_evidence, Mapping):
        return False
    evidence_count = summary.get("evidence_count")
    eligible_evidence_count = summary.get("eligible_evidence_count")
    evidence_coverage = _finite_number(summary.get("eligible_evidence_coverage"))
    evidence_hash = summary.get("evidence_sha256")
    expected_source = f"{_LISTING_DATE_SOURCE}; {_LISTING_DATE_SOURCE_URL}"
    if (
        summary.get("provided") is not True
        or isinstance(evidence_count, bool)
        or not isinstance(evidence_count, int)
        or evidence_count <= 0
        or eligible_evidence_count != evidence_count
        or evidence_count > len(eligible)
        or evidence_coverage is None
        or not math.isclose(evidence_coverage, evidence_count / len(eligible), rel_tol=0.0, abs_tol=1e-12)
        or not isinstance(evidence_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None
        or len(set(evidence_hash)) < 8
        or evidence_hash != hashlib.sha256(_canonical_market_coldness_json(expected_evidence)).hexdigest()
        or summary.get("sources") != [expected_source]
        or summary.get("as_of_sessions") != [trade_session]
    ):
        return False

    retrieved_at = status.get("retrieved_at")
    parsed_retrieval = _parse_aware_utc(retrieved_at)
    if (
        status.get("available") is not True
        or status.get("evidence_available") is not True
        or status.get("evidence_reason") != "available"
        or status.get("model_id") != _MARKET_COLDNESS_MODEL_ID
        or status.get("source") != _LISTING_DATE_SOURCE
        or status.get("source_url") != _LISTING_DATE_SOURCE_URL
        or status.get("as_of_session") != trade_session
        or parsed_retrieval is None
        or retrieved_at != reference_artifact.get("retrieved_at")
        or status.get("eligible_evidence_count") != evidence_count
        or not _close_number(status.get("eligible_evidence_coverage"), evidence_coverage, rel_tol=0.0, abs_tol=1e-12)
        or status.get("reference_artifact_sha256")
        != hashlib.sha256(_canonical_market_coldness_json(reference_artifact)).hexdigest()
        or status.get("full_listed_evidence_count") != len(full_evidence)
    ):
        return False

    not_applicable = status.get("eligible_not_applicable_codes_by_reason")
    data_gaps = status.get("eligible_unscored_data_gap_codes_by_reason")
    if (
        not isinstance(not_applicable, Mapping)
        or set(not_applicable) != _MARKET_COLDNESS_NOT_APPLICABLE_REASONS
        or not_applicable != replay.get("eligible_not_applicable_codes_by_reason")
        or data_gaps != {}
        or data_gaps != replay.get("eligible_unscored_data_gap_codes_by_reason")
        or status.get("eligible_unscored_data_gap_count") != 0
    ):
        return False
    not_applicable_codes: set[str] = set()
    for reason in sorted(_MARKET_COLDNESS_NOT_APPLICABLE_REASONS):
        raw_codes = not_applicable.get(reason)
        if not isinstance(raw_codes, list) or raw_codes != sorted(set(raw_codes)):
            return False
        for code in raw_codes:
            if not isinstance(code, str) or code not in eligible or code in not_applicable_codes:
                return False
            not_applicable_codes.add(code)
    applicable_count = len(eligible) - len(not_applicable_codes)
    if (
        status.get("eligible_not_applicable_count") != len(not_applicable_codes)
        or status.get("eligible_applicable_count") != applicable_count
        or evidence_count != applicable_count
        or not _close_number(
            status.get("eligible_applicable_evidence_coverage"),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        return False
    return True


def _strict_ttm_analysis_population(validation: Mapping[str, Any]) -> set[str] | None:
    """Return the independently reconstructed SH/SZ population, if valid."""
    coverage = validation.get("strict_ttm_source_coverage")
    if not isinstance(coverage, Mapping):
        return None
    denominator = coverage.get("denominator")
    excluded = coverage.get("excluded_financial_codes")
    analysis_quotes = validation.get("analysis_market_quotes")
    if (
        coverage.get("population") != "SH_SZ_non_financial"
        or coverage.get("evaluated") is not True
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator <= 0
        or isinstance(analysis_quotes, bool)
        or not isinstance(analysis_quotes, int)
        or not isinstance(excluded, list)
        or excluded != sorted(set(str(code) for code in excluded))
        or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in excluded)
        or denominator + len(excluded) != analysis_quotes
    ):
        return None

    allowed_statuses = {
        "complete",
        "missing_component",
        "duplicate_period",
        "invalid_period_contract",
        "nonfinite_component",
        "implausible_unit",
        "seasonal_reconstruction_clipped",
    }
    adjusted_statuses = {"seasonal_reconstruction_clipped"}
    population_sets: list[set[str]] = []
    for metric in ("revenue", "fcff"):
        metric_coverage = coverage.get(metric)
        if not isinstance(metric_coverage, Mapping):
            return None
        complete = metric_coverage.get("complete")
        adjusted = metric_coverage.get("adjusted")
        usable = metric_coverage.get("usable")
        missing = metric_coverage.get("missing")
        ratio = _finite_number(metric_coverage.get("coverage"))
        status_counts = metric_coverage.get("status_counts")
        complete_codes = metric_coverage.get("complete_codes")
        adjusted_by_status = metric_coverage.get("adjusted_codes_by_status")
        missing_by_status = metric_coverage.get("missing_codes_by_status")
        if (
            isinstance(complete, bool)
            or not isinstance(complete, int)
            or isinstance(adjusted, bool)
            or not isinstance(adjusted, int)
            or isinstance(usable, bool)
            or not isinstance(usable, int)
            or isinstance(missing, bool)
            or not isinstance(missing, int)
            or complete < 0
            or adjusted < 0
            or usable < 0
            or missing < 0
            or usable != complete + adjusted
            or usable + missing != denominator
            or ratio is None
            or not math.isclose(ratio, usable / denominator, rel_tol=0.0, abs_tol=1e-12)
            or ratio < _MIN_RELEASE_STRICT_TTM_SOURCE_COVERAGE
            or not isinstance(status_counts, Mapping)
            or not isinstance(complete_codes, list)
            or not isinstance(adjusted_by_status, Mapping)
            or not isinstance(missing_by_status, Mapping)
        ):
            return None
        if (
            set(status_counts) - allowed_statuses
            or status_counts.get("complete") != complete
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in status_counts.values()
            )
            or sum(status_counts.values()) != denominator
            or complete_codes != sorted(set(str(code) for code in complete_codes))
            or len(complete_codes) != complete
            or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in complete_codes)
        ):
            return None
        expected_adjusted_statuses = {
            status for status, count in status_counts.items() if status in adjusted_statuses and count
        }
        if set(adjusted_by_status) != expected_adjusted_statuses:
            return None
        adjusted_codes: list[str] = []
        for status, codes in adjusted_by_status.items():
            if (
                status not in adjusted_statuses
                or not isinstance(codes, list)
                or codes != sorted(set(str(code) for code in codes))
                or len(codes) != status_counts.get(status)
                or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in codes)
            ):
                return None
            adjusted_codes.extend(str(code) for code in codes)
        if len(adjusted_codes) != adjusted or len(set(adjusted_codes)) != adjusted:
            return None
        expected_missing_statuses = {
            status
            for status, count in status_counts.items()
            if status != "complete" and status not in adjusted_statuses and count
        }
        if set(missing_by_status) != expected_missing_statuses:
            return None
        missing_codes: list[str] = []
        for status, codes in missing_by_status.items():
            if (
                status not in allowed_statuses - {"complete", *adjusted_statuses}
                or not isinstance(codes, list)
                or codes != sorted(set(str(code) for code in codes))
                or len(codes) != status_counts.get(status)
                or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in codes)
            ):
                return None
            missing_codes.extend(str(code) for code in codes)
        complete_set = {str(code) for code in complete_codes}
        adjusted_set = set(adjusted_codes)
        missing_set = set(missing_codes)
        if (
            len(missing_codes) != missing
            or len(missing_set) != missing
            or complete_set & (adjusted_set | missing_set)
            or adjusted_set & missing_set
            or (complete_set | adjusted_set | missing_set) & set(excluded)
            or len(complete_set | adjusted_set | missing_set) != denominator
        ):
            return None
        population_sets.append(complete_set | adjusted_set | missing_set)
    if population_sets[0] != population_sets[1]:
        return None
    eligible_codes = validation.get("eligible_codes")
    if not isinstance(eligible_codes, list):
        return None
    analysis_population = population_sets[0] | {str(code) for code in excluded}
    analysis_market_codes = validation.get("analysis_market_codes")
    analysis_ineligible_codes = validation.get("analysis_ineligible_codes")
    normalized_eligible = [str(code) for code in eligible_codes]
    if (
        analysis_market_codes != sorted(analysis_population)
        or normalized_eligible != sorted(set(normalized_eligible))
        or not set(normalized_eligible).issubset(analysis_population)
        or analysis_ineligible_codes != sorted(analysis_population - set(normalized_eligible))
    ):
        return None
    return analysis_population


def _valid_strict_ttm_source_coverage(validation: Mapping[str, Any]) -> bool:
    """Validate the schema-8 non-financial SH/SZ TTM population ledger."""

    return _strict_ttm_analysis_population(validation) is not None


def _valid_listing_date_evidence(validation: Mapping[str, Any]) -> bool:
    """Validate the schema-8 whole-market listing-date provenance ledger."""
    evidence = validation.get("listing_date_evidence")
    analysis_quotes = validation.get("analysis_market_quotes")
    if not isinstance(evidence, Mapping) or isinstance(analysis_quotes, bool) or not isinstance(analysis_quotes, int):
        return False
    reference_count = evidence.get("reference_count")
    listing_date_count = evidence.get("listing_date_count")
    reference_coverage = _finite_number(evidence.get("reference_coverage"))
    listing_date_coverage = _finite_number(evidence.get("listing_date_coverage"))
    missing_reference_codes = evidence.get("missing_reference_codes")
    missing_listing_date_codes = evidence.get("missing_listing_date_codes")
    status_counts = evidence.get("status_counts")
    retrieved_at_oldest = _finite_number(evidence.get("retrieved_at_oldest"))
    retrieved_at_latest = _finite_number(evidence.get("retrieved_at_latest"))
    if (
        evidence.get("required") is not True
        or analysis_quotes <= 0
        or isinstance(reference_count, bool)
        or not isinstance(reference_count, int)
        or isinstance(listing_date_count, bool)
        or not isinstance(listing_date_count, int)
        or not 0 <= listing_date_count <= reference_count <= analysis_quotes
        or reference_coverage is None
        or listing_date_coverage is None
        or not math.isclose(reference_coverage, reference_count / analysis_quotes, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(listing_date_coverage, listing_date_count / analysis_quotes, rel_tol=0.0, abs_tol=1e-12)
        or reference_coverage < _MIN_RELEASE_LISTING_REFERENCE_COVERAGE
        or listing_date_coverage < _MIN_RELEASE_LISTING_DATE_COVERAGE
        or evidence.get("source") != _LISTING_DATE_SOURCE
        or evidence.get("source_url") != _LISTING_DATE_SOURCE_URL
        or retrieved_at_oldest is None
        or retrieved_at_latest is None
        or retrieved_at_oldest <= 0
        or retrieved_at_latest < retrieved_at_oldest
        or not isinstance(missing_reference_codes, list)
        or not isinstance(missing_listing_date_codes, list)
        or not isinstance(status_counts, Mapping)
    ):
        return False
    missing_sets: list[set[str]] = []
    for raw_codes, expected_count in (
        (missing_reference_codes, analysis_quotes - reference_count),
        (missing_listing_date_codes, reference_count - listing_date_count),
    ):
        normalized = [str(code) for code in raw_codes]
        if (
            normalized != sorted(set(normalized))
            or len(normalized) != expected_count
            or any(re.fullmatch(r"[036][0-9]{5}", code) is None for code in normalized)
        ):
            return False
        missing_sets.append(set(normalized))
    if missing_sets[0] & missing_sets[1]:
        return False
    if (
        any(not isinstance(status, str) or not status.strip() for status in status_counts)
        or any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in status_counts.values())
        or sum(status_counts.values()) != reference_count
        or status_counts.get("reported") != listing_date_count
    ):
        return False
    return True


def _valid_capex_provenance(
    provenance: object,
    *,
    expected_value: float,
    expected_report_date: str,
    expected_security_code: str | None = None,
) -> bool:
    if not isinstance(provenance, Mapping):
        return False
    if (
        provenance.get("schema_version") != _CAPEX_PROVENANCE_SCHEMA_VERSION
        or provenance.get("status") != "complete"
        or provenance.get("report_date") != expected_report_date
        or not _close_number(provenance.get("value"), expected_value, abs_tol=0.01)
    ):
        return False
    components = provenance.get("components")
    if provenance.get("evidence_label") == "fact_secondary_source_reported":
        code = provenance.get("security_code")
        source_hash = provenance.get("source_raw_sha256")
        query = provenance.get("source_query")
        metadata = provenance.get("source_metadata")
        if (
            expected_value <= 0
            or not isinstance(code, str)
            or re.fullmatch(r"[036]\d{5}", code) is None
            or (expected_security_code is not None and code != expected_security_code)
            or provenance.get("source_report") != _SINA_CASHFLOW_REPORT
            or provenance.get("source_field") != _SINA_CAPEX_FIELD
            or provenance.get("canonical_field") != "CONSTRUCT_LONG_ASSET"
            or provenance.get("formula") != "source_reported"
            or provenance.get("derivation_method") is not None
            or provenance.get("source_url") != _SINA_FINANCIAL_URL
            or not isinstance(source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
            or len(set(source_hash)) < 8
            or not isinstance(query, Mapping)
            or not isinstance(metadata, Mapping)
            or not isinstance(components, Mapping)
        ):
            return False
        prefix = "sh" if code.startswith("6") else "sz"
        request_num = query.get("num")
        return bool(
            query
            == {
                "paperCode": f"{prefix}{code}",
                "source": "llb",
                "type": "0",
                "page": "1",
                "num": request_num,
            }
            and isinstance(request_num, str)
            and re.fullmatch(r"(?:[1-9]|1\d|20)", request_num) is not None
            and metadata.get("report_type") == "合并期末"
            and metadata.get("currency") == "CNY"
            and isinstance(metadata.get("publish_date"), str)
            and re.fullmatch(r"\d{8}", metadata["publish_date"]) is not None
            and not isinstance(metadata.get("update_time"), bool)
            and isinstance(metadata.get("update_time"), int)
            and metadata["update_time"] > 0
            and _close_number(components.get("reported_value"), expected_value, abs_tol=0.01)
        )
    if provenance.get("source_url") != _EASTMONEY_DATACENTER_URL:
        return False
    if provenance.get("evidence_label") == "fact_source_reported":
        return bool(
            provenance.get("source_report") in {_STANDARD_CASHFLOW_REPORT, _DETAILED_CASHFLOW_REPORT}
            and provenance.get("source_field") == "CONSTRUCT_LONG_ASSET"
            and provenance.get("formula") == "source_reported"
            and provenance.get("derivation_method") is None
            and isinstance(components, Mapping)
            and _close_number(components.get("reported_value"), expected_value, abs_tol=0.01)
        )
    if (
        provenance.get("evidence_label") != "derived_calculation"
        or provenance.get("source_report") != _DETAILED_CASHFLOW_REPORT
        or not _close_number(expected_value, 0.0, abs_tol=0.01)
        or not isinstance(components, Mapping)
    ):
        return False
    outflows = components.get("non_capex_outflows")
    if not isinstance(outflows, Mapping) or set(outflows) != _NON_CAPEX_OUTFLOW_FIELDS:
        return False
    values = [_finite_number(outflows.get(field)) for field in _NON_CAPEX_OUTFLOW_FIELDS]
    if any(value is None or value < 0 for value in values):
        return False
    outflow_sum = sum(value for value in values if value is not None)
    method = provenance.get("derivation_method")
    if method == "detailed_outflow_residual_zero":
        total = _finite_number(components.get("total_invest_outflow"))
        declared_sum = _finite_number(components.get("non_capex_outflow_sum"))
        return bool(
            total is not None
            and total >= 0
            and declared_sum is not None
            and _close_number(declared_sum, outflow_sum, abs_tol=0.01)
            and _close_number(total, outflow_sum, abs_tol=0.01)
        )
    if method != "detailed_net_cash_identity_zero":
        return False
    inflow = _finite_number(components.get("total_invest_inflow"))
    other = _finite_number(components.get("invest_netcash_other"))
    balance = _finite_number(components.get("invest_netcash_balance"))
    net = _finite_number(components.get("netcash_invest"))
    solved = _finite_number(components.get("solved_total_invest_outflow"))
    if None in {inflow, other, balance, net, solved}:
        return False
    return _close_number(inflow + other + balance - net, solved, abs_tol=0.01) and _close_number(
        solved,
        0.0,
        abs_tol=0.01,
    )


def _strict_ttm_evidence_value(
    evidence: object,
    *,
    metric: str,
    contract: Mapping[str, str],
    expected_security_code: str | None = None,
) -> float | None:
    """Recompute a complete FY + current-YTD - prior-YTD evidence payload."""
    if not isinstance(evidence, Mapping):
        return None
    expected_formula = _TTM_FCFF_FORMULA_VERSION if metric == "fcff" else _TTM_REVENUE_FORMULA_VERSION
    expected_kind = "cfo_less_capex_proxy" if metric == "fcff" else "reported_revenue"
    expected_keys = {
        "status",
        "value",
        "metric",
        "formula_version",
        "cash_flow_kind",
        "period_basis",
        "period",
        "unit",
        "components",
    }
    expected_period = {
        "basis": _TTM_PERIOD_BASIS,
        "annual_report_date": contract["annual_report_date"],
        "current_interim_report_date": contract["current_interim_report_date"],
        "prior_interim_report_date": contract["prior_interim_report_date"],
    }
    if (
        set(evidence) != expected_keys
        or evidence.get("status") != "complete"
        or evidence.get("metric") != metric
        or evidence.get("formula_version") != expected_formula
        or evidence.get("cash_flow_kind") != expected_kind
        or evidence.get("period_basis") != _TTM_PERIOD_BASIS
        or evidence.get("period") != expected_period
        or evidence.get("unit") != _TTM_SOURCE_UNIT
    ):
        return None
    components = evidence.get("components")
    if not isinstance(components, Mapping):
        return None
    labels = ("annual", "current_interim", "prior_interim")
    dates = {
        "annual": contract["annual_report_date"],
        "current_interim": contract["current_interim_report_date"],
        "prior_interim": contract["prior_interim_report_date"],
    }
    if metric == "revenue":
        if set(components) != {*labels, "reconstructed_revenue"}:
            return None
        values: dict[str, float] = {}
        for label in labels:
            component = components.get(label)
            if not isinstance(component, Mapping) or set(component) != {
                "report_date",
                "row_count",
                "revenue",
                "revenue_source_field",
            }:
                return None
            value = _finite_number(component.get("revenue"))
            if (
                component.get("report_date") != dates[label]
                or component.get("row_count") != 1
                or isinstance(component.get("row_count"), bool)
                or component.get("revenue_source_field") not in _TTM_REVENUE_FIELDS
                or value is None
            ):
                return None
            values[label] = value
        reconstructed = values["annual"] + values["current_interim"] - values["prior_interim"]
        if reconstructed <= 0 or not _close_number(components.get("reconstructed_revenue"), reconstructed):
            return None
    else:
        if set(components) != {
            *labels,
            "reconstructed_operating_cash_flow",
            "reconstructed_capex",
            "reconstructed_fcff",
        }:
            return None
        cfo_values: dict[str, float] = {}
        capex_values: dict[str, float] = {}
        for label in labels:
            component = components.get(label)
            if not isinstance(component, Mapping) or set(component) != {
                "report_date",
                "row_count",
                "operating_cash_flow",
                "operating_cash_flow_source_field",
                "capex_raw",
                "capex_absolute",
                "capex_source_field",
                "capex_provenance",
                "capex_provenance_status",
            }:
                return None
            cfo = _finite_number(component.get("operating_cash_flow"))
            capex_raw = _finite_number(component.get("capex_raw"))
            capex_absolute = _finite_number(component.get("capex_absolute"))
            if (
                component.get("report_date") != dates[label]
                or component.get("row_count") != 1
                or isinstance(component.get("row_count"), bool)
                or component.get("operating_cash_flow_source_field") not in _TTM_OPERATING_CASH_FIELDS
                or component.get("capex_source_field") not in _TTM_CAPEX_FIELDS
                or cfo is None
                or capex_raw is None
                or capex_absolute is None
                or not _close_number(capex_absolute, abs(capex_raw))
                or component.get("capex_provenance_status") != "complete"
                or not _valid_capex_provenance(
                    component.get("capex_provenance"),
                    expected_value=capex_raw,
                    expected_report_date=dates[label],
                    expected_security_code=expected_security_code,
                )
            ):
                return None
            cfo_values[label] = cfo
            capex_values[label] = capex_absolute
        reconstructed_cfo = cfo_values["annual"] + cfo_values["current_interim"] - cfo_values["prior_interim"]
        reconstructed_capex = capex_values["annual"] + capex_values["current_interim"] - capex_values["prior_interim"]
        reconstructed = reconstructed_cfo - reconstructed_capex
        if (
            reconstructed_capex < 0
            or reconstructed <= 0
            or not _close_number(components.get("reconstructed_operating_cash_flow"), reconstructed_cfo)
            or not _close_number(components.get("reconstructed_capex"), reconstructed_capex)
            or not _close_number(components.get("reconstructed_fcff"), reconstructed)
        ):
            return None
    return reconstructed if _close_number(evidence.get("value"), reconstructed) else None


def _valid_strict_ttm_valuation(
    result: Mapping[str, Any],
    contract: Mapping[str, str],
) -> bool:
    """Bind non-financial DCF inputs to strict TTM evidence and adjustment lineage."""
    if (
        result.get("valuation_input_basis") != "strict_ttm"
        or result.get("base_revenue_basis") != "strict_ttm_reported_revenue"
        or result.get("base_fcf_basis") != "normalised_two_annual_plus_ttm_cfo_less_capex_proxy"
        or result.get("fcf_normalisation_period_basis") != "two_annual_plus_strict_ttm"
    ):
        return False
    security_code = result.get("code")
    if not isinstance(security_code, str) or re.fullmatch(r"[036]\d{5}", security_code) is None:
        return False
    ttm_fcff = _strict_ttm_evidence_value(
        result.get("ttm_fcff_evidence"),
        metric="fcff",
        contract=contract,
        expected_security_code=security_code,
    )
    ttm_revenue = _strict_ttm_evidence_value(
        result.get("ttm_revenue_evidence"),
        metric="revenue",
        contract=contract,
    )
    if ttm_fcff is None or ttm_revenue is None or not _close_number(result.get("base_revenue"), ttm_revenue):
        return False

    recent_raw = result.get("recent_fcff")
    if not isinstance(recent_raw, list) or len(recent_raw) != 3:
        return False
    recent = tuple(_finite_number(value) for value in recent_raw)
    if any(value is None for value in recent):
        return False
    prior_fcff, annual_fcff, latest_fcff = recent
    fcff_components = result["ttm_fcff_evidence"]["components"]
    annual_component = fcff_components["annual"]
    expected_annual_fcff = annual_component["operating_cash_flow"] - annual_component["capex_absolute"]
    if (
        not _close_number(annual_fcff, expected_annual_fcff)
        or not _close_number(latest_fcff, ttm_fcff)
        or not _close_number(result.get("latest_fcff"), ttm_fcff)
    ):
        return False

    annual_year = int(contract["annual_report_date"][:4])
    periods = [
        {"kind": "annual", "report_date": f"{annual_year - 1}-12-31"},
        {"kind": "annual", "report_date": contract["annual_report_date"]},
        {"kind": "ttm", "through_report_date": contract["current_interim_report_date"]},
    ]
    if result.get("recent_fcff_periods") != periods:
        return False
    if latest_fcff <= 0:
        return False
    if annual_fcff <= prior_fcff and latest_fcff <= annual_fcff:
        normalised, basis = latest_fcff, "latest_persistent_decline"
    else:
        median = sorted((prior_fcff, annual_fcff, latest_fcff))[1]
        premium_limit = latest_fcff * _FCF_NORMALISATION_PREMIUM_CAP
        normalised = min(median, premium_limit)
        basis = "recent_median" if median <= premium_limit else "latest_premium_cap"
    if normalised <= 0 or result.get("fcf_normalisation_basis") != basis:
        return False
    expected_period_detail = {
        "period_set": "two_annual_plus_strict_ttm",
        "periods": periods,
        "normalisation_method": basis,
        "cash_flow_kind": "cfo_less_capex_proxy",
        "formula_version": _TTM_FCFF_FORMULA_VERSION,
    }
    if result.get("fcf_normalisation_period") != expected_period_detail or not _close_number(
        result.get("normalisation_premium_cap"),
        _FCF_NORMALISATION_PREMIUM_CAP,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        return False

    adjustments = result.get("base_fcf_adjustments")
    margin_ceiling = _finite_number(result.get("fcf_margin_ceiling"))
    if not isinstance(adjustments, list) or margin_ceiling is None or not 0 < margin_ceiling <= 1:
        return False
    allowed_order = {"mixed_profit_cycle_p25_cap": 0, "fcf_margin_ceiling": 1}
    seen: list[str] = []
    current = normalised
    for adjustment in adjustments:
        if not isinstance(adjustment, Mapping) or set(adjustment) != {"kind", "before", "limit", "after"}:
            return False
        kind = adjustment.get("kind")
        before = _finite_number(adjustment.get("before"))
        limit = _finite_number(adjustment.get("limit"))
        after = _finite_number(adjustment.get("after"))
        if (
            kind not in allowed_order
            or kind in seen
            or (seen and allowed_order[kind] <= allowed_order[seen[-1]])
            or before is None
            or limit is None
            or after is None
            or limit <= 0
            or not _close_number(before, current)
            or before <= limit
            or not _close_number(after, min(before, limit))
        ):
            return False
        if kind == "fcf_margin_ceiling" and not _close_number(limit, ttm_revenue * margin_ceiling):
            return False
        seen.append(str(kind))
        current = after
    margin_limit = ttm_revenue * margin_ceiling
    if "fcf_margin_ceiling" not in seen and current > margin_limit and not _close_number(current, margin_limit):
        return False
    return current > 0 and _close_number(result.get("base_fcf"), current)


def _independent_dcf_value(
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
    """Independent five-year two-stage FCFF calculation for the release gate."""
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
        max(_DCF_MARGIN_FLOOR, _DCF_MARGIN_LONG_TERM, current_margin * retention),
    )
    explicit_pv = 0.0
    final_revenue = 0.0
    final_discount = 0.0
    denominator = max(_DCF_FORECAST_YEARS - 1, 1)
    for year in range(1, _DCF_FORECAST_YEARS + 1):
        revenue = base_revenue * (1 + growth) ** year
        margin = current_margin + (target_margin - current_margin) * ((year - 1) / denominator)
        discount = (1 + wacc) ** year
        if discount <= 0 or not all(math.isfinite(value) for value in (revenue, margin, discount)):
            return None
        explicit_pv += revenue * margin / discount
        final_revenue, final_discount = revenue, discount
    terminal_fcf = final_revenue * (1 + terminal_growth) * target_margin
    terminal_value = terminal_fcf / (wacc - terminal_growth)
    per_share = (explicit_pv + terminal_value / final_discount - net_debt) / shares
    return per_share if math.isfinite(per_share) and per_share > 0 else None


def _valid_valuation_result(
    code: str,
    result: object,
    company: Mapping[str, Any],
    *,
    reporting_period_contract: Mapping[str, str] | None,
    excluded_financial_codes: set[str],
) -> bool:
    if not isinstance(result, Mapping):
        return False
    current_price = _finite_number(result.get("current_price"))
    buy_boundary = _finite_number(result.get("buy_zone_upper"))
    sell_boundary = _finite_number(result.get("sell_zone_lower"))
    base_wacc = _finite_number(result.get("base_wacc"))
    shares = _finite_number(result.get("shares_outstanding"))
    market_cap = _finite_number(company.get("market_cap"))
    is_pb = result.get("_pb_valuation") is True
    expected_tax_source = "financial_operating_liabilities_excluded" if is_pb else "taxable_profit_evidence_unavailable"
    wacc_components = result.get("wacc_components")
    if (
        str(result.get("code")) != code
        or result.get("name") != company.get("name")
        or result.get("industry_code") != company.get("industry")
        or current_price != _finite_number(company.get("price"))
        or current_price is None
        or current_price <= 0
        or buy_boundary is None
        or sell_boundary is None
        or buy_boundary <= 0
        or buy_boundary > sell_boundary
        or base_wacc is None
        or not 0 < base_wacc < 1
        or shares is None
        or shares <= 0
        or market_cap is None
        or market_cap <= 0
        or not math.isclose(
            shares,
            market_cap / current_price,
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
        or _finite_number(result.get("tax_shield_rate")) != 0.0
        or result.get("tax_shield_source") != expected_tax_source
        or not isinstance(wacc_components, Mapping)
        or _finite_number(wacc_components.get("tax_shield_rate")) != 0.0
        or is_pb != (code in excluded_financial_codes)
    ):
        return False
    equity_weight = _finite_number(wacc_components.get("equity_weight"))
    debt_weight = _finite_number(wacc_components.get("debt_weight"))
    cost_of_equity = _finite_number(wacc_components.get("cost_of_equity"))
    debt_cost = _finite_number(wacc_components.get("pre_tax_cost_of_debt"))
    if (
        equity_weight is None
        or debt_weight is None
        or cost_of_equity is None
        or not 0 <= equity_weight <= 1
        or not 0 <= debt_weight <= 1
        or not math.isclose(equity_weight + debt_weight, 1.0, rel_tol=0.0, abs_tol=1e-9)
        or cost_of_equity <= 0
        or (debt_weight > 0 and (debt_cost is None or debt_cost < 0))
    ):
        return False
    reconstructed_wacc = equity_weight * cost_of_equity + debt_weight * (debt_cost or 0.0)
    if not math.isclose(base_wacc, round(reconstructed_wacc, 4), rel_tol=0.0, abs_tol=5.1e-5):
        return False
    expected_zone = (
        "买入区" if current_price <= buy_boundary else "卖出区" if current_price >= sell_boundary else "观察区"
    )
    if result.get("zone") != expected_zone:
        return False

    points = result.get("dcf_points")
    params = result.get("params")
    scenarios = ("pessimistic", "neutral", "optimistic")
    if (
        not isinstance(points, Mapping)
        or set(points) != set(scenarios)
        or not isinstance(params, Mapping)
        or set(params) != set(scenarios)
    ):
        return False
    bands: list[tuple[float, float]] = []
    parsed_parameters: dict[str, Mapping[str, Any]] = {}
    for scenario in scenarios:
        band = points.get(scenario)
        parameters = params.get(scenario)
        if not isinstance(band, Mapping) or set(band) != {"lower", "upper"} or not isinstance(parameters, Mapping):
            return False
        lower = _finite_number(band.get("lower"))
        upper = _finite_number(band.get("upper"))
        if lower is None or upper is None or lower <= 0 or lower > upper:
            return False
        if any(_finite_number(parameters.get(field)) is None for field in ("growth", "wacc_base", "terminal_g")):
            return False
        if (is_pb and parameters.get("margin_retention") is not None) or (
            not is_pb and _finite_number(parameters.get("margin_retention")) is None
        ):
            return False
        bands.append((lower, upper))
        parsed_parameters[scenario] = parameters
    if is_pb:
        midpoints = [(lower + upper) / 2 for lower, upper in bands]
        if (
            not midpoints[0] <= midpoints[1] <= midpoints[2]
            or result.get("base_fcf") is not None
            or result.get("base_revenue") is not None
            or result.get("net_debt") is not None
        ):
            return False
        for scenario, (lower, upper) in zip(scenarios, bands):
            parameters = parsed_parameters[scenario]
            growth = _finite_number(parameters.get("growth"))
            cost = _finite_number(parameters.get("cost_of_equity"))
            wacc_center = _finite_number(parameters.get("wacc_base"))
            scenario_roe = _finite_number(parameters.get("scenario_roe"))
            normalised_roe = _finite_number(parameters.get("normalised_roe"))
            bvps = _finite_number(parameters.get("bvps"))
            pb_lower = _finite_number(parameters.get("pb_lower"))
            pb_upper = _finite_number(parameters.get("pb_upper"))
            if (
                None in (growth, cost, wacc_center, scenario_roe, normalised_roe, bvps, pb_lower, pb_upper)
                or cost <= growth
                or cost - _DCF_BAND_WACC_DELTA <= growth
                or scenario_roe <= growth
                or bvps <= 0
                or parameters.get("formula") != "(normalised_roe - g) / (cost_of_equity - g)"
                or not _close_number(wacc_center, cost)
                or not _close_number(cost, reconstructed_wacc)
                or not _close_number(parameters.get("terminal_g"), growth)
                or not _close_number(parameters.get("normalised_roe"), result.get("normalised_roe"))
            ):
                return False
            expected_pb_lower = (scenario_roe - growth) / (cost + _DCF_BAND_WACC_DELTA - growth)
            expected_pb_upper = (scenario_roe - growth) / (cost - _DCF_BAND_WACC_DELTA - growth)
            if (
                not _close_number(pb_lower, expected_pb_lower)
                or not _close_number(pb_upper, expected_pb_upper)
                or not _close_number(lower, bvps * expected_pb_lower)
                or not _close_number(upper, bvps * expected_pb_upper)
            ):
                return False
    else:
        flattened = [value for band in bands for value in band]
        if flattened != sorted(flattened) or (_finite_number(result.get("base_fcf")) or 0) <= 0:
            return False
        if (_finite_number(result.get("base_revenue")) or 0) <= 0:
            return False
        if reporting_period_contract is None or not _valid_strict_ttm_valuation(
            result,
            reporting_period_contract,
        ):
            return False
        base_fcf = _finite_number(result.get("base_fcf"))
        base_revenue = _finite_number(result.get("base_revenue"))
        net_debt = _finite_number(result.get("net_debt"))
        if (
            base_fcf is None
            or base_revenue is None
            or net_debt is None
            or result.get("explicit_forecast_years") != _DCF_FORECAST_YEARS
        ):
            return False
        for scenario, (lower, upper) in zip(scenarios, bands):
            parameters = parsed_parameters[scenario]
            growth = _finite_number(parameters.get("growth"))
            wacc_center = _finite_number(parameters.get("wacc_base"))
            terminal_growth = _finite_number(parameters.get("terminal_g"))
            retention = _finite_number(parameters.get("margin_retention"))
            if (
                None in (growth, wacc_center, terminal_growth, retention)
                or parameters.get("forecast_years") != _DCF_FORECAST_YEARS
                or not _close_number(
                    wacc_center,
                    reconstructed_wacc + _DCF_WACC_SHIFT[scenario],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                return False
            expected_lower = _independent_dcf_value(
                base_fcf=base_fcf,
                base_revenue=base_revenue,
                growth=growth,
                wacc=wacc_center + _DCF_BAND_WACC_DELTA,
                terminal_growth=terminal_growth,
                shares=shares,
                net_debt=net_debt,
                retention=retention,
            )
            expected_upper = _independent_dcf_value(
                base_fcf=base_fcf,
                base_revenue=base_revenue,
                growth=growth,
                wacc=wacc_center - _DCF_BAND_WACC_DELTA,
                terminal_growth=terminal_growth,
                shares=shares,
                net_debt=net_debt,
                retention=retention,
            )
            if (
                expected_lower is None
                or expected_upper is None
                or not _close_number(lower, expected_lower)
                or not _close_number(upper, expected_upper)
            ):
                return False
    expected_buy = (bands[0][1] + bands[1][0]) / 2
    expected_sell = (bands[1][1] + bands[2][0]) / 2
    expected_center = (bands[1][0] + bands[1][1]) / 2
    return (
        math.isclose(buy_boundary, expected_buy, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(sell_boundary, expected_sell, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(_finite_number(result.get("mean1")) or -1, expected_buy, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(_finite_number(result.get("mean2")) or -1, expected_sell, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(_finite_number(result.get("dcf_value_mean")) or -1, expected_buy, rel_tol=1e-9, abs_tol=1e-9)
        and result.get("dcf_value_mean_legacy_alias_of") == "buy_zone_upper"
        and math.isclose(
            _finite_number(result.get("valuation_center")) or -1,
            expected_center,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        and math.isclose(
            _finite_number(result.get("neutral_value_midpoint")) or -1,
            expected_center,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    )


def _parse_aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _git_tree_entries(raw_tree: bytes) -> dict[str, tuple[str, str, str]]:
    entries: dict[str, tuple[str, str, str]] = {}
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        if not separator:
            raise ValueError("invalid Git tree entry")
        mode, object_type, object_id = metadata.decode("ascii").split()
        relative = raw_path.decode("utf-8")
        path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or relative.startswith("/")
            or ".." in path.parts
            or any(
                not part
                or unicodedata.normalize("NFC", part) != part
                or part.endswith((" ", "."))
                or any(character in '<>:"|?*' for character in part)
                or any(ord(character) < 32 for character in part)
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
                for part in path.parts
            )
        ):
            raise ValueError(f"release Git tree contains an unsafe path: {relative!r}")
        entries[relative] = (mode, object_type, object_id)
    return entries


def _git_release_provenance_errors(
    commit: str,
    repository: str | Path,
    archive: ZipFile,
    file_names: Mapping[str, str],
) -> list[str]:
    repo = Path(repository).resolve()
    git_executable = shutil.which("git")
    if not git_executable:
        return ["Git executable is unavailable for release provenance verification"]

    def run(*arguments: str) -> str:
        # ``git_executable`` is an absolute path and every argument is fixed.
        return subprocess.run(  # nosec B603
            [git_executable, *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    def run_bytes(*arguments: str) -> bytes:
        return subprocess.run(  # nosec B603
            [git_executable, *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout

    try:
        if run("rev-parse", "--is-inside-work-tree") != "true":
            return ["release verification repository is not a Git work tree"]
        if run("status", "--porcelain"):
            return ["release verification repository is not clean"]
        run("cat-file", "-e", f"{commit}^{{commit}}")
        subprocess.run(  # nosec B603
            [git_executable, "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        changed = {
            line.replace("\\", "/")
            for line in run("diff", "--name-only", f"{commit}..HEAD", "--").splitlines()
            if line.strip()
        }
        descendant_count = int(run("rev-list", "--count", f"{commit}..HEAD"))
        raw_tree = run_bytes(
            "-c",
            "core.quotepath=false",
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "HEAD",
        )
        tree_entries = _git_tree_entries(raw_tree)
        tracked = set(tree_entries)
    except ValueError as exc:
        return [str(exc)]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ["audit Git commit cannot be verified as an ancestor in the clean release repository"]
    errors: list[str] = []
    if descendant_count != 1:
        errors.append("the audit Git commit must have exactly one release descendant")
    if _AUDIT_JSON_PATH not in changed or not changed.issubset(_RELEASE_AUDIT_PATHS):
        errors.append("changes after the audit Git commit are not limited to release audit artifacts with updated JSON")
    if tracked != set(file_names):
        errors.append("release ZIP file set does not exactly match the clean Git HEAD tree")
        return errors
    for relative in sorted(tracked):
        mode, object_type, object_id = tree_entries[relative]
        if mode != "100644" or object_type != "blob":
            errors.append(f"release Git tree contains an unsupported entry: {relative}")
            continue
        try:
            raw_attributes = run_bytes(
                "check-attr",
                "-z",
                "--source=HEAD",
                "text",
                "eol",
                "filter",
                "ident",
                "working-tree-encoding",
                "--",
                relative,
            )
            attribute_parts = raw_attributes.decode("utf-8").split("\0")
            if attribute_parts and attribute_parts[-1] == "":
                attribute_parts.pop()
            if len(attribute_parts) % 3:
                raise ValueError("invalid Git attribute response")
            attributes = {
                attribute_parts[index + 1]: attribute_parts[index + 2] for index in range(0, len(attribute_parts), 3)
            }
            source_bytes = run_bytes("cat-file", "blob", object_id)
            archive_bytes = archive.read(file_names[relative])
        except (KeyError, OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            errors.append(f"release ZIP cannot be byte-bound to Git HEAD: {relative}")
            continue
        unsupported_attributes = {
            name: value
            for name, value in attributes.items()
            if name in {"filter", "ident", "working-tree-encoding"} and value not in {"unspecified", "unset"}
        }
        if unsupported_attributes:
            errors.append(f"release Git tree uses unsupported checkout filters: {relative}")
            continue
        text_attribute = attributes.get("text", "unspecified")
        eol_attribute = attributes.get("eol", "unspecified")
        if text_attribute == "unset":
            if eol_attribute not in {"unspecified", "unset"}:
                errors.append(f"release binary file has a conflicting eol policy: {relative}")
                continue
        elif text_attribute != "set" or eol_attribute not in {"lf", "crlf"}:
            errors.append(f"release text file lacks a deterministic eol policy: {relative}")
            continue
        elif eol_attribute == "crlf":
            source_bytes = source_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        else:
            source_bytes = source_bytes.replace(b"\r\n", b"\n")
        if source_bytes != archive_bytes:
            errors.append(f"release ZIP content differs from clean Git HEAD: {relative}")
    return errors


def verify_release_zip(path: str, *, repository: str | Path | None = ".") -> tuple[str, ...]:
    errors: list[str] = []
    try:
        with ZipFile(path) as archive:
            for entry in archive.infolist():
                raw_name = entry.filename
                portable = raw_name.replace("\\", "/")
                raw_parts = PurePosixPath(portable).parts
                unix_mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if (
                    "\\" in raw_name
                    or portable.startswith("/")
                    or re.match(r"^[A-Za-z]:/", portable)
                    or ".." in raw_parts
                    or stat.S_ISLNK(unix_mode)
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    errors.append(f"unsafe archive path: {raw_name}")
            try:
                file_names = _normalised_file_names(archive.namelist())
            except ValueError as exc:
                return (str(exc),)
            for normalised, archive_name in file_names.items():
                parts = PurePosixPath(normalised).parts
                if not normalised or normalised.startswith("/") or ".." in parts:
                    errors.append(f"unsafe archive path: {archive_name}")
                    continue
                lower = normalised.lower()
                if _FORBIDDEN_PATH.search(normalised) or lower.endswith(_FORBIDDEN_SUFFIXES):
                    errors.append(f"forbidden release artifact: {normalised}")
                if lower.startswith("data/cache/") and lower != "data/cache/.gitkeep":
                    errors.append(f"runtime cache in release: {normalised}")
                basename = parts[-1].lower()
                if (
                    basename in {"coverage.xml", ".coverage", "cache.db", "secrets.toml"}
                    or basename.startswith(".coverage.")
                    or basename in _SSH_PRIVATE_KEY_NAMES
                    or basename.endswith(("-signing-private-key.properties", "-release-credentials.properties"))
                    or lower == "android/release.properties"
                ):
                    errors.append(f"runtime or secret file in release: {normalised}")
                if basename == ".env" or (
                    basename.startswith(".env.") and not basename.endswith((".example", ".sample", ".template"))
                ):
                    errors.append(f"environment secret file in release: {normalised}")
                if PurePosixPath(normalised).suffix.lower() in _SECRET_SCAN_SUFFIXES:
                    content = archive.read(archive_name)
                    if len(content) <= 10_000_000 and _SECRET_ASSIGNMENT.search(content):
                        errors.append(f"probable embedded credential in release: {normalised}")

            missing = sorted(_REQUIRED_FILES - set(file_names))
            if missing:
                errors.append(f"required release files are missing: {missing}")
            license_name = file_names.get("LICENSE")
            if (
                license_name is not None
                and hashlib.sha256(archive.read(license_name)).hexdigest() != _EXPECTED_LICENSE_SHA256
            ):
                errors.append("LICENSE does not match the approved PolyForm Noncommercial 1.0.0 terms")

            run_name = file_names.get("run.bat")
            if run_name is not None:
                run_bytes = archive.read(run_name)
                without_crlf = run_bytes.replace(b"\r\n", b"")
                if b"\n" in without_crlf or b"\r" in without_crlf or b"\r\n" not in run_bytes:
                    errors.append("run.bat is not consistently CRLF")
                errors.extend(_desktop_launcher_errors(run_bytes))

            for normalised, archive_name in file_names.items():
                if normalised.lower().endswith(_LF_SUFFIXES) or PurePosixPath(normalised).name.lower() in _LF_NAMES:
                    content = archive.read(archive_name)
                    if b"\r\n" in content or b"\r" in content:
                        errors.append(f"text file is not canonical LF: {normalised}")

            update_config_name = file_names.get("desktop/update_config.json")
            if update_config_name is not None:
                try:
                    update_config = _strict_json_loads(archive.read(update_config_name))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"desktop update configuration is unreadable: {exc}")
                except OSError as exc:
                    errors.append(f"desktop update configuration cannot be read: {exc}")
                else:
                    if update_config != {"manifest_url": _EXPECTED_UPDATE_MANIFEST_URL}:
                        errors.append("desktop update configuration does not use the official release manifest")

            audit_name = file_names.get(_AUDIT_JSON_PATH)
            if audit_name is not None:
                try:
                    payload = _strict_json_loads(archive.read(audit_name))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"audit JSON is unreadable: {exc}")
                except OSError as exc:
                    errors.append(f"audit JSON cannot be read: {exc}")
                else:
                    if not isinstance(payload, Mapping):
                        errors.append("audit JSON root is not an object")
                        payload = {}
                    provenance = payload.get("provenance", {})
                    provenance = provenance if isinstance(provenance, Mapping) else {}
                    caller = provenance.get("caller_metadata", {})
                    caller = caller if isinstance(caller, Mapping) else {}
                    git_state = provenance.get("git", {})
                    git_state = git_state if isinstance(git_state, Mapping) else {}
                    if provenance.get("audit_schema_version") != 6:
                        errors.append("audit schema version is not 6")
                    if provenance.get("patch6_source") != _EXPECTED_PATCH6_SOURCE:
                        errors.append("audit is not bound to the authoritative Patch 6 source hash")
                    if provenance.get("patch7_source") != _EXPECTED_PATCH7_SOURCE:
                        errors.append("audit is not bound to the authoritative Patch 7 source hash")
                    if provenance.get("type7_source_documents") != _EXPECTED_TYPE7_SOURCE_DOCUMENTS:
                        errors.append("audit is not bound to all authoritative Type 7 source hashes")
                    if caller.get("snapshot_schema_version") != 8:
                        errors.append("snapshot schema version is not 8")
                    strict_evidence_required = caller.get("strict_evidence_required")
                    screening_coverage = caller.get("screening_coverage")
                    if not isinstance(strict_evidence_required, bool) or not _valid_full_market_screening_coverage(
                        screening_coverage,
                        payload.get("eligible_universe_size"),
                    ):
                        errors.append("audit does not preserve full-market artifact integrity and candidate visibility")
                    elif (
                        strict_evidence_required
                        and isinstance(screening_coverage, Mapping)
                        and isinstance(screening_coverage.get("goal_readiness"), Mapping)
                        and screening_coverage["goal_readiness"].get("ready") is not True
                    ):
                        errors.append("audit requested strict ideal-zero-gap evidence but did not achieve it")
                    validation = caller.get("validation", {})
                    validation = validation if isinstance(validation, Mapping) else {}
                    reporting_period_contract = _valid_reporting_period_contract(
                        validation.get("reporting_period_contract")
                    )
                    if reporting_period_contract is None:
                        errors.append("snapshot reporting period contract is missing or invalid")
                    elif provenance.get("reporting_period_contract") != reporting_period_contract:
                        errors.append("audit reporting period contract differs from the schema-8 snapshot contract")
                    if not _valid_supplemental_field_coverage(validation):
                        errors.append("snapshot schema-8 supplemental field coverage ledger is missing or invalid")
                    if not _valid_listing_date_evidence(validation):
                        errors.append("snapshot listing-date provenance ledger is missing or invalid")
                    trade_session = _valid_release_quote_session(validation)
                    if trade_session is None:
                        errors.append("snapshot does not prove at least 99% same-session trading quote coverage")
                    strict_ttm_coverage_valid = _valid_strict_ttm_source_coverage(validation)
                    if not strict_ttm_coverage_valid:
                        errors.append("snapshot strict TTM source coverage ledger is missing or invalid")
                    coverage = validation.get("strict_ttm_source_coverage")
                    excluded_financial_codes = (
                        {str(code) for code in coverage.get("excluded_financial_codes", [])}
                        if strict_ttm_coverage_valid and isinstance(coverage, Mapping)
                        else set()
                    )
                    audit_commit = str(git_state.get("commit", ""))
                    if git_state.get("dirty") is not False or re.fullmatch(r"[0-9a-fA-F]{40}", audit_commit) is None:
                        errors.append("audit was not generated from a clean identified Git commit")
                    elif repository is not None:
                        errors.extend(_git_release_provenance_errors(audit_commit, repository, archive, file_names))
                    if payload.get("seed") != _EXPECTED_AUDIT_SEED:
                        errors.append(f"audit seed is not {_EXPECTED_AUDIT_SEED}")
                    raw_sample_codes = payload.get("sample_codes", [])
                    if (
                        payload.get("sample_size") != 100
                        or not isinstance(raw_sample_codes, list)
                        or len(raw_sample_codes) != 100
                    ):
                        errors.append("audit does not contain the required random sample of 100")
                    sample_codes = (
                        [str(code) for code in raw_sample_codes] if isinstance(raw_sample_codes, list) else []
                    )
                    if len(set(sample_codes)) != len(sample_codes) or any(
                        not re.fullmatch(r"[036][0-9]{5}", code) for code in sample_codes
                    ):
                        errors.append("audit sample is not a unique SH/SZ code set")
                    for field in (
                        "engine_self_check_errors",
                        "same_source_scoring_replay_errors",
                        "same_source_valuation_replay_errors",
                        "independent_check_errors",
                        "invariant_errors",
                        "pipeline_issues",
                    ):
                        if payload.get(field) != []:
                            errors.append(f"audit field {field} is not empty")
                    patch4_bindings = _patch4_bindings_from_provenance(
                        provenance.get("patch4_evidence"),
                        payload.get("eligible_universe_size"),
                    )
                    if patch4_bindings is None:
                        errors.append("audit Type 7 Patch 4 provenance bindings are missing or invalid")
                        patch4_bindings = {}
                    company_codes = _audit_company_codes(
                        payload,
                        patch4_bindings=patch4_bindings,
                    )
                    if company_codes is None:
                        errors.append("audit does not contain 100 complete company rows")
                    elif company_codes != sample_codes:
                        errors.append("audit company identities do not exactly match the ordered SH/SZ sample")
                    companies = payload.get("companies")
                    company_by_code = (
                        {str(company.get("code")): company for company in companies if isinstance(company, Mapping)}
                        if isinstance(companies, list)
                        else {}
                    )
                    dcf_results = payload.get("dcf_results")
                    dcf_skip_reasons = payload.get("dcf_skip_reasons")
                    dcf_skip_classifications = payload.get("dcf_skip_classifications")
                    if (
                        not isinstance(dcf_results, Mapping)
                        or not isinstance(dcf_skip_reasons, Mapping)
                        or not isinstance(dcf_skip_classifications, Mapping)
                    ):
                        errors.append("audit valuation results, skip reasons, or skip classifications are missing")
                    else:
                        result_codes = {str(code) for code in dcf_results}
                        skip_codes = {str(code) for code in dcf_skip_reasons}
                        normalized_skip_classifications: dict[str, Mapping[str, str]] = {}
                        skip_classifications_valid = True
                        for raw_code, classification in dcf_skip_classifications.items():
                            code = str(raw_code)
                            if (
                                code in normalized_skip_classifications
                                or not isinstance(classification, Mapping)
                                or set(classification) != {"category", "reason"}
                                or not isinstance(classification.get("category"), str)
                                or classification.get("category") not in _AUDIT_DCF_SKIP_CATEGORIES
                                or not isinstance(classification.get("reason"), str)
                                or not classification["reason"].strip()
                            ):
                                skip_classifications_valid = False
                                continue
                            normalized_skip_classifications[code] = {
                                "category": classification["category"],
                                "reason": classification["reason"],
                            }
                        expected_type1_1a: dict[str, float] = {}
                        expected_nonfinancial_type1_1a: dict[str, float] = {}
                        valuation_results_valid = True
                        for raw_code, result in dcf_results.items():
                            code = str(raw_code)
                            valid = code in company_by_code and _valid_valuation_result(
                                code,
                                result,
                                company_by_code[code],
                                reporting_period_contract=reporting_period_contract,
                                excluded_financial_codes=excluded_financial_codes,
                            )
                            valuation_results_valid = valuation_results_valid and valid
                            if valid and isinstance(result, Mapping):
                                expected_score = _expected_type1_1a_from_dcf(result)
                                if expected_score is None:
                                    valuation_results_valid = False
                                else:
                                    expected_type1_1a[code] = expected_score
                                    if result.get("_pb_valuation") is not True:
                                        expected_nonfinancial_type1_1a[code] = expected_score
                        classification_codes = set(normalized_skip_classifications)
                        valuation_partition_valid = (
                            not (result_codes & skip_codes)
                            and result_codes | skip_codes == set(sample_codes)
                            and classification_codes == skip_codes
                            and payload.get("dcf_valid") == len(result_codes)
                            and valuation_results_valid
                            and skip_classifications_valid
                            and all(str(reason or "").strip() for reason in dcf_skip_reasons.values())
                            and all(
                                normalized_skip_classifications[code]["reason"] == str(dcf_skip_reasons[code])
                                for code in classification_codes
                            )
                        )
                        if not valuation_partition_valid:
                            errors.append(
                                "audit does not provide one complete valuation result or structured skip per company"
                            )
                        if not _audit_valuation_bindings_valid(
                            companies,
                            expected_type1_1a=expected_type1_1a,
                            expected_nonfinancial_type1_1a=expected_nonfinancial_type1_1a,
                            skip_classifications=normalized_skip_classifications,
                            financial_codes=excluded_financial_codes,
                        ):
                            errors.append(
                                "audit Type 1 or Type 7 evidence is not bound to validated valuation outcomes"
                            )

                    eligible_codes_raw = validation.get("eligible_codes")
                    eligible_codes = (
                        [str(code) for code in eligible_codes_raw] if isinstance(eligible_codes_raw, list) else []
                    )
                    unsupported_codes_raw = validation.get("unsupported_market_codes")
                    unsupported_codes = (
                        {str(code) for code in unsupported_codes_raw}
                        if isinstance(unsupported_codes_raw, list)
                        else set()
                    )
                    market_counts = validation.get("market_counts")
                    market_counts = market_counts if isinstance(market_counts, Mapping) else {}
                    analysis_market_quotes = validation.get("analysis_market_quotes")
                    analysis_eligible_coverage = _finite_number(validation.get("analysis_eligible_coverage"))
                    eligible_universe_size = payload.get("eligible_universe_size")
                    for field, label in (
                        ("type3_growth_evidence", "Type 3 growth"),
                        ("research_report_evidence", "Type 7 research-report"),
                    ):
                        if not _valid_optional_evidence_provenance(
                            provenance.get(field),
                            eligible_universe_size,
                        ):
                            errors.append(f"audit {label} provenance summary is missing or invalid")
                    eligible_identity_valid = (
                        validation.get("analysis_markets") == ["SH", "SZ"]
                        and isinstance(eligible_universe_size, int)
                        and eligible_universe_size >= _MIN_RELEASE_ELIGIBLE_COMPANIES
                        and validation.get("eligible_companies") == eligible_universe_size
                        and len(eligible_codes) == eligible_universe_size
                        and eligible_codes == sorted(set(eligible_codes))
                        and all(re.fullmatch(r"[036][0-9]{5}", code) for code in eligible_codes)
                        and isinstance(unsupported_codes_raw, list)
                        and all(re.fullmatch(r"[89][0-9]{5}", code) for code in unsupported_codes)
                        and not (set(eligible_codes) & unsupported_codes)
                        and all(
                            isinstance(market_counts.get(market), int) and market_counts.get(market) >= minimum
                            for market, minimum in _MIN_RELEASE_MARKET_COUNTS.items()
                        )
                        and set(market_counts).issubset({"SH", "SZ", "BJ"})
                        and isinstance(market_counts.get("BJ", 0), int)
                        and market_counts.get("BJ", 0) >= 0
                        and analysis_market_quotes == market_counts.get("SH", 0) + market_counts.get("SZ", 0)
                        and validation.get("quotes")
                        == sum(int(market_counts.get(market, 0)) for market in ("SH", "SZ", "BJ"))
                        and analysis_eligible_coverage is not None
                        and math.isclose(
                            analysis_eligible_coverage,
                            eligible_universe_size / analysis_market_quotes,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                        and analysis_eligible_coverage >= 0.80
                        and len(unsupported_codes) == market_counts.get("BJ")
                        and provenance.get("eligible_universe_sha256")
                        == hashlib.sha256("\n".join(eligible_codes).encode("ascii")).hexdigest()
                    )
                    if not eligible_identity_valid:
                        errors.append(
                            "audit does not bind the complete analysis universe to unique SH/SZ eligible codes"
                        )
                    # The release contract checks one reproducible public sample;
                    # cryptographic unpredictability is neither required nor wanted.
                    elif sample_codes != sorted(
                        random.Random(_EXPECTED_AUDIT_SEED).sample(eligible_codes, 100)  # nosec B311
                    ):
                        errors.append(
                            "audit sample does not match the fixed-seed draw from the complete eligible universe"
                        )

                    if (
                        trade_session is not None
                        and eligible_identity_valid
                        and not _valid_market_coldness_provenance(
                            provenance.get("market_coldness_evidence"),
                            caller.get("market_coldness"),
                            caller.get("market_coldness_reference_artifact"),
                            eligible_codes=eligible_codes,
                            trade_session=trade_session,
                            validation=validation,
                        )
                    ):
                        errors.append(
                            "audit market-coldness provenance is missing, incomplete, or bound to another session"
                        )

                    generated_at = _parse_aware_utc(provenance.get("generated_at_utc"))
                    data_timestamp = _parse_aware_utc(payload.get("data_timestamp_utc"))
                    trade_date = date.fromisoformat(trade_session) if trade_session is not None else None
                    data_shanghai_date = data_timestamp.astimezone(_SHANGHAI).date() if data_timestamp else None
                    generated_shanghai_date = generated_at.astimezone(_SHANGHAI).date() if generated_at else None
                    generation_delay = (
                        (generated_at - data_timestamp).total_seconds()
                        if generated_at is not None and data_timestamp is not None
                        else None
                    )
                    snapshot_hashes = (
                        str(provenance.get("snapshot_content_sha256", "")).lower(),
                        str(provenance.get("snapshot_artifact_sha256", "")).lower(),
                        str(caller.get("snapshot_payload_sha256", "")).lower(),
                    )
                    snapshot_identity_valid = (
                        all(re.fullmatch(r"[0-9a-f]{64}", value) is not None for value in snapshot_hashes)
                        and len(set(snapshot_hashes)) == len(snapshot_hashes)
                        and all(len(set(value)) >= 8 for value in snapshot_hashes)
                        and isinstance(caller.get("snapshot_artifact_bytes"), int)
                        and caller.get("snapshot_artifact_bytes") > 0
                        and caller.get("snapshot_source") == "network"
                        and generated_at is not None
                        and data_timestamp is not None
                        and data_timestamp <= generated_at
                        and trade_date is not None
                        and data_shanghai_date is not None
                        and generated_shanghai_date is not None
                        and 0 <= (data_shanghai_date - trade_date).days <= 10
                        and 0 <= (generated_shanghai_date - data_shanghai_date).days <= 1
                        and generation_delay is not None
                        and 0 <= generation_delay <= 6 * 60 * 60
                    )
                    if not snapshot_identity_valid:
                        errors.append("audit is not bound to an identified, time-consistent market snapshot")

                    companion_hashes = payload.get("companion_artifacts_sha256", {})
                    companion_hashes = companion_hashes if isinstance(companion_hashes, Mapping) else {}
                    companion_contents: dict[str, bytes] = {}
                    for kind, companion_path in (("csv", _AUDIT_CSV_PATH), ("markdown", _AUDIT_MARKDOWN_PATH)):
                        companion_name = file_names.get(companion_path)
                        if companion_name is None:
                            continue
                        content = archive.read(companion_name)
                        companion_contents[kind] = content
                        expected = companion_hashes.get(kind)
                        if re.fullmatch(r"[0-9a-f]{64}", str(expected)) is None or expected != _sha256_bytes(content):
                            errors.append(f"audit {kind} companion does not match its JSON manifest hash")
                    csv_rows = _audit_csv_rows(companion_contents.get("csv", b""))
                    if (
                        csv_rows is None
                        or [row.get("代码") for row in csv_rows] != sample_codes
                        or not isinstance(companies, list)
                        or not isinstance(dcf_results, Mapping)
                        or not isinstance(dcf_skip_reasons, Mapping)
                        or not _audit_csv_matches_payload(csv_rows, companies, dcf_results, dcf_skip_reasons)
                    ):
                        errors.append("audit CSV does not semantically match all 100 JSON company rows")
                    try:
                        markdown_text = companion_contents.get("markdown", b"").decode("utf-8")
                    except UnicodeDecodeError:
                        markdown_text = ""
                    if (
                        not isinstance(companies, list)
                        or company_codes is None
                        or not isinstance(dcf_results, Mapping)
                        or not isinstance(dcf_skip_reasons, Mapping)
                        or not _audit_markdown_matches_payload(
                            markdown_text,
                            payload,
                            companies,
                            dcf_results,
                            dcf_skip_reasons,
                        )
                    ):
                        errors.append("audit Markdown does not semantically match all 100 JSON company rows")

                    analysis_quality = payload.get("analysis_quality", {})
                    full_market_quality = caller.get("full_market_quality", {})
                    if not isinstance(analysis_quality, Mapping) or not isinstance(full_market_quality, Mapping):
                        errors.append("audit full-market quality metadata is missing")
                    else:
                        if dict(analysis_quality) != dict(full_market_quality):
                            errors.append("audit full-market quality copies are inconsistent")
                        dcf_valid = analysis_quality.get("dcf_valid")
                        dcf_skipped = analysis_quality.get("dcf_skipped")
                        strict_quality = (
                            isinstance(eligible_universe_size, int)
                            and eligible_universe_size >= _MIN_RELEASE_ELIGIBLE_COMPANIES
                            and analysis_quality.get("ok") is True
                            and analysis_quality.get("expected_companies") == eligible_universe_size
                            and analysis_quality.get("score_raw_rows") == eligible_universe_size
                            and analysis_quality.get("score_rows") == eligible_universe_size
                            and analysis_quality.get("score_coverage") == 1.0
                            and analysis_quality.get("dcf_attempted") == eligible_universe_size
                            and analysis_quality.get("dcf_attempt_coverage") == 1.0
                            and analysis_quality.get("pipeline_issues") == 0
                            and analysis_quality.get("pipeline_issue_rate") == 0.0
                            and analysis_quality.get("reasons") == []
                            and isinstance(dcf_valid, int)
                            and isinstance(dcf_skipped, int)
                            and dcf_valid + dcf_skipped == eligible_universe_size
                        )
                        if not strict_quality:
                            errors.append("audit does not prove a zero-issue complete full-market analysis")

                    project_name = file_names.get("pyproject.toml")
                    project_table: Mapping[str, Any] = {}
                    if project_name is not None:
                        try:
                            pyproject = tomllib.loads(archive.read(project_name).decode("utf-8"))
                            candidate = pyproject.get("project", {})
                            project_table = candidate if isinstance(candidate, Mapping) else {}
                        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                            errors.append("pyproject.toml is unreadable")
                    if project_table.get("name") != "ds-dcf":
                        errors.append("project name is not ds-dcf")
                    version_name = file_names.get("desktop/version.py")
                    packaged_version = ""
                    if version_name is None:
                        errors.append("desktop/version.py is missing")
                    else:
                        try:
                            version_text = archive.read(version_name).decode("utf-8")
                        except UnicodeDecodeError:
                            errors.append("desktop/version.py is unreadable")
                        else:
                            match = re.search(
                                r"^__version__\s*=\s*['\"](\d+\.\d+\.\d+)['\"]\s*$",
                                version_text,
                                flags=re.MULTILINE,
                            )
                            if match is None:
                                errors.append("desktop/version.py has no valid semantic version")
                            else:
                                packaged_version = match.group(1)
                    if not packaged_version or project_table.get("version") != packaged_version:
                        errors.append("project version does not match desktop version")
                    if project_table.get("license") != _EXPECTED_PROJECT_LICENSE or project_table.get(
                        "license-files"
                    ) != ["LICENSE"]:
                        errors.append("project license is not PolyForm Noncommercial 1.0.0")
                    runtime = provenance.get("runtime", {})
                    runtime = runtime if isinstance(runtime, Mapping) else {}
                    if not _runtime_python_supported(runtime.get("python"), project_table.get("requires-python")):
                        errors.append("audit Python runtime is outside the packaged project support range")
                    direct_dependencies = _exact_project_dependencies(project_table)
                    runtime_packages = runtime.get("packages", {})
                    if (
                        direct_dependencies is None
                        or set(direct_dependencies) != _EXPECTED_DIRECT_DEPENDENCIES
                        or not isinstance(runtime_packages, Mapping)
                    ):
                        errors.append("project or audit runtime dependency metadata is invalid")
                    elif any(
                        str(runtime_packages.get(name, "")) != version for name, version in direct_dependencies.items()
                    ):
                        errors.append("audit runtime direct dependency versions differ from pyproject.toml")
                    lock_maps = {
                        lock_name: _locked_requirements(archive.read(file_names[lock_name]))
                        for lock_name in (
                            "requirements-bootstrap.txt",
                            "requirements-lock.txt",
                            "requirements-dev-lock.txt",
                        )
                        if lock_name in file_names
                    }
                    if len(lock_maps) != 3 or any(value is None for value in lock_maps.values()):
                        errors.append("dependency locks are not complete exact pins with valid SHA256 hashes")
                    elif direct_dependencies is None or any(
                        any(lock_maps[lock].get(name) != version for name, version in direct_dependencies.items())
                        for lock in ("requirements-lock.txt", "requirements-dev-lock.txt")
                    ):
                        errors.append("dependency locks do not contain every exact direct runtime dependency")

                    code_files = {
                        name
                        for name in file_names
                        if name in {"app.py", "config.py"}
                        or (
                            name.endswith(".py")
                            and name.split("/", 1)[0] in {"data", "desktop", "engine", "ui", "tools"}
                        )
                    }
                    expected_hashes = {
                        "code_sha256": _hash_archive_files(archive, file_names, code_files),
                        "rules_sha256": _hash_archive_files(archive, file_names, _RULE_FILES),
                        "industry_sha256": _hash_archive_files(archive, file_names, _INDUSTRY_FILES),
                        "dependency_manifest_sha256": _hash_archive_files(
                            archive,
                            file_names,
                            _DEPENDENCY_FILES,
                        ),
                    }
                    for field, expected in expected_hashes.items():
                        if provenance.get(field) != expected:
                            errors.append(f"audit {field} does not match release contents")
    except (BadZipFile, FileNotFoundError, OSError) as exc:
        return (f"release ZIP cannot be read: {exc}",)
    return tuple(errors)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path")
    parser.add_argument(
        "--repository",
        default=".",
        help="clean Git work tree used to prove the audit commit and audit-only descendant",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = args.zip_path
    errors = verify_release_zip(path, repository=args.repository)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"release ZIP verified: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
