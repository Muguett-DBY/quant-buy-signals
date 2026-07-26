"""补丁6七种买入情况的量化评分引擎。

评分纪律：子项0..10分、按固定权重加总、显示总分达到7.0才可能触发；
任何一票否决优先于总分。缺少能够证明某项的原始数据时保守评分，绝不以
主观行业故事补分。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
import math
import re
from statistics import median
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    BAND_WACC_DELTA,
    DEFAULT_PRETAX_COST_OF_DEBT,
    DEFAULT_UNLEVERED_BETA,
    FORECAST_YEARS,
    INDUSTRY_PRETAX_COST_OF_DEBT,
    INDUSTRY_UNLEVERED_BETA,
    LONG_HORIZON_FORECAST_YEARS,
    MARGINAL_TAX_RATE,
)
from data.financial_indicator_evidence import derive_main_financial_indicator_evidence
from data.growth_evidence import GrowthEvidenceError, validate_growth_evidence_record
from data.industry import begin_industry_generation, classify_industries, classify_industry, get_industry_benchmark
from data.patch4_evidence import (
    MODEL_ID as PATCH4_PUBLIC_EVIDENCE_MODEL_ID,
    Patch4EvidenceError,
    validate_patch4_evidence_record,
)
from data.quality_history import replay_valuation_distribution
from engine.dcf import (
    MAX_NORMALISED_FCFF_PREMIUM,
    ReportingPeriodContract,
    compute_wacc,
    dcf_valuation,
    dcf_valuation_fading_growth,
    extract_debt_and_cash,
    reconstruct_ttm_fcff,
    reconstruct_ttm_revenue,
)
from engine.market_coldness import (
    MarketColdnessScoringError,
    validate_market_coldness_evidence_record,
)
from engine.quantitative_evidence import (
    EVIDENCE_LEVELS as QUANTITATIVE_EVIDENCE_LEVELS,
    MIN_COMPARABLE_COVERAGE,
    MIN_SECTOR_COMPANIES,
    MODEL_ID as QUANTITATIVE_EVIDENCE_MODEL_ID,
    SCORE_KEYS as QUANTITATIVE_SCORE_KEYS,
    TYPE3_GROWTH_VALIDATION_TOKEN,
    derive_company_evidence,
    enrich_metrics,
)
from engine.quality_equity import (
    MODEL_ID as QUALITY_EQUITY_MODEL_ID,
    PATCH5_SAFETY_VETO,
    RESEARCH_EVIDENCE_MODEL_ID,
    SCHEMA_VERSION as QUALITY_EQUITY_SCHEMA_VERSION,
    TYPE7_DIRECT_SCORE_KEYS,
    assess_quality_equity,
    normalise_patch4_assessment,
    normalise_research_content_verification,
    normalise_research_sources,
    research_metadata_precheck,
    validate_quality_equity_ledger,
)
from engine.valuation_status import (
    DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
    DCF_SKIP_INCONSISTENT_SOURCE,
    DCF_SKIP_INTERNAL_ERROR,
    DCF_SKIP_MODEL_UNSUPPORTED,
    DCF_SKIP_SOURCE_MISSING,
    normalize_dcf_skip_classification,
)


QUALIFY_THRESHOLD = 7.0
VETO_SCORE = 3.0
# Public cards keep each evidence sentence within 20 characters.  Compaction
# must end at a semantic separator where possible and always show an ellipsis;
# silently slicing through a percentage or unit makes the explanation false.
# Full values and formulas live in the structured valuation ledger.
EVIDENCE_MAX_LENGTH = 20

STATUS_TRIGGERED = "triggered"
STATUS_OBSERVE = "observe"
STATUS_NOT_TRIGGERED = "not_triggered"
STATUS_VETOED = "vetoed"
STATUS_CONDITIONAL = "conditional"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATUS_BLOCKED = "blocked"
TYPE_STATUSES = {
    STATUS_TRIGGERED,
    STATUS_OBSERVE,
    STATUS_NOT_TRIGGERED,
    STATUS_VETOED,
    STATUS_CONDITIONAL,
    STATUS_NOT_APPLICABLE,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_BLOCKED,
}

DECISION_SCHEMA_VERSION = 1
DECISION_MODEL_ID = "buy-decision-bounds-v1"
DECISION_BASES = frozenset(
    {
        "full_evidence",
        "scope_exclusion",
        "confirmed_veto",
        "conservative_upper_bound",
        "action_condition",
        "market_block",
        "unresolved_missing_evidence",
    }
)
DECISION_VETO_STATES = frozenset({"none", "possible", "confirmed"})
_DECISION_FIELDS = frozenset(
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
_DECISION_MISSING_DIMENSIONS_REASON = "_decision_missing_dimensions"
_DECISION_MARKET_BLOCK_REASON = "_decision_market_block"
_DECISION_MARKET_CONTEXT = "decision_market_context"
_DECISION_MARKET_CONTEXT_FIELDS = frozenset({"tradable", "reference_price", "risk_status"})
_POTENTIAL_VETO_DIMENSIONS = {
    "type1": frozenset({"1a", "1b"}),
    "type2": frozenset({"2a", "2b", "2c"}),
    "type3": frozenset({"3a", "3d", "3e"}),
    "type4": frozenset({"4c", "4e", "4f"}),
    # The authoritative Type 5 appendix currently has no post-applicability
    # hard veto.  Type 6 can be ruled out when fewer than two of 6a..6d can
    # reach five.  Type 7's safety veto lives in 7c.
    "type5": frozenset(),
    "type6": frozenset({"6a", "6b", "6c", "6d"}),
    "type7": frozenset({"7c"}),
}

TYPE_WEIGHTS: dict[str, dict[str, float]] = {
    "type1": {"1a": 0.30, "1b": 0.35, "1c": 0.20, "1d": 0.15},
    "type2": {"2a": 0.25, "2b": 0.30, "2c": 0.25, "2d": 0.20},
    "type3": {"3a": 0.25, "3b": 0.20, "3c": 0.20, "3d": 0.25, "3e": 0.10},
    "type4": {"4a": 0.25, "4b": 0.25, "4c": 0.20, "4d": 0.15, "4e": 0.08, "4f": 0.07},
    "type5": {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10},
    "type6": {"6a": 0.25, "6b": 0.20, "6c": 0.15, "6d": 0.25, "6e": 0.15},
    "type7": {"7a": 1.0 / 3.0, "7b": 1.0 / 3.0, "7c": 1.0 / 3.0},
}

TYPE_NAMES = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ 高风险早期/困境型",
    "type7": "7️⃣ 优质股权型",
}

# 补丁6原六类的本质属性优先级保持不变；新增Type7原文未给出
# 覆盖优先级，因此追加在后，避免改写既有主类型语义。
TYPE_PRIORITY = ["type1", "type2", "type5", "type3", "type4", "type6", "type7"]
_DEFAULT_INDUSTRY_CLASSIFIER = classify_industry

SUPPORTED_FINANCIAL_INDUSTRIES = {"BANK", "INSURANCE", "SECURITIES"}
FINANCIAL_INDUSTRIES = SUPPORTED_FINANCIAL_INDUSTRIES | {"FINANCIAL_OTHER"}
FINANCIAL_SECTOR_RULE_VERSION = "financial-sector-evidence-v1"
FINANCIAL_REGULATORY_SOURCES = {
    "BANK": "https://www.gov.cn/gongbao/2024/issue_11126/202401/content_6928796.html",
    "INSURANCE": "https://www.nfra.gov.cn/chinese/docfile/2022/5d6576679eaa45d3b4ea34fb7adb1254.pdf",
    "SECURITIES": "https://www.csrc.gov.cn/csrc/c106256/c1653957/content.shtml",
}
# Regulatory minima are hard failure boundaries.  Higher two-point bands are
# transparent screening assumptions, not claims that every institution has the
# same Pillar-2/systemic buffer or business risk.
FINANCIAL_REGULATORY_THRESHOLDS = {
    "BANK": {
        "capital_min": 0.08,
        "tier1_min": 0.06,
        "capital_screening_buffer": 0.105,
        "tier1_screening_buffer": 0.085,
    },
    "INSURANCE": {"solvency_min": 1.0, "solvency_screening_buffer": 1.5},
    "SECURITIES": {
        "risk_coverage_min": 1.0,
        "capital_leverage_min": 0.08,
        "liquidity_coverage_min": 1.0,
        "net_stable_funding_min": 1.0,
    },
}
STRONG_CYCLICAL_INDUSTRIES = {
    "STEEL",
    "NONFERROUS",
    "CHEMICAL",
    "BUILDING_MATERIAL",
    "OIL_GAS",
    "COAL",
    "CONST_MACHINERY",
    "AGRICULTURE",
}
# ``STRONG_CYCLICAL_INDUSTRIES`` 还服务于其他模板的保守杠杆约束，不能
# 直接拿来给情况五自动确认为强周期。情况五的适用门槛更严格：行业分类
# 只能提供“产品有公开大宗价格”的一项线索，仍须盈利/毛利率或人工原始
# 证据共同确认。运输、农业、新能源设备等大类混有大量非强周期公司，故
# 不允许仅凭宽泛行业标签自动进入情况五。
TYPE5_DIRECT_CYCLICAL_INDUSTRIES = {
    "STEEL",
    "NONFERROUS",
    "CHEMICAL",
    "BUILDING_MATERIAL",
    "OIL_GAS",
    "COAL",
}
# The persisted identifier predates Type5's reuse of the same source contract.
# Keep it stable for cache/replay compatibility while using consumer-neutral
# names in user-facing text.
LONG_HORIZON_HISTORY_MODEL_ID = "type7-market-history-v1"
TYPE5_PB_MIN_OBSERVATIONS = 500
TYPE5_HISTORY_MIN_SPAN_DAYS = 1_743
TYPE5_HISTORY_MAX_START_DELAY_DAYS = 62
TYPE5_HISTORY_MAX_LATEST_AGE_DAYS = 21
TYPE5_BOTTOM_EVIDENCE_SCHEMA_VERSION = 1
TYPE5_BOTTOM_EVIDENCE_MODEL_ID = "type5-bottom-observables-v1"
# 第19模板明确区分两类VC标的：高景气赛道不超过300亿元、平稳
# 产业反转不超过100亿元。旧值 ``30e8`` 只有30亿元，缩小了10倍。
TYPE6_GROWTH_MARKET_CAP_LIMIT = 300e8  # 300亿元
TYPE6_TURNAROUND_MARKET_CAP_LIMIT = 100e8  # 100亿元

QUALITATIVE_SCORE_KEYS = (
    "technology_score",
    "business_model_score",
    "runway_score",
    "moat_score",
    "moat_durability_score",
    "industry_bubble_score",
    "industry_durability_score",
    "accounting_integrity_score",
    "management_alignment_score",
    "catalyst_score",
    "market_coldness_score",
    "growth_quality_score",
    "growth_sustainability_score",
    "type3_bubble_score",
    "cyclical_industry_score",
    # 情况五的七类专用证据。每个分数都必须随附来源、证据编号和截止日；
    # 它们不会被自动财务代理补全，避免把单年 PE/PB 冒充周期底部证据。
    "type5_cycle_attribute_score",
    "type5_bottom_signal_score",
    "type5_survival_score",
    "type5_upside_elasticity_score",
    "type5_normalized_earnings_score",
) + TYPE7_DIRECT_SCORE_KEYS

_PARENT_EQUITY_KEYS = (
    "PARENT_EQUITY",
    "TOTAL_PARENT_EQUITY",
    "TOTAL_EQUITY_ATTR_P",
    "TOTAL_EQUITY_PARENT",
    "PARENT_HOLDER_EQUITY",
)
_MINORITY_EQUITY_KEYS = ("MINORITY_EQUITY", "MINORITY_INTEREST")
_EVIDENCE_ALLOWED_KEYS = {"source", "evidence_id", "as_of", "summary"}
_EVIDENCE_SOURCE_MAX_LENGTH = 200
_EVIDENCE_ID_MAX_LENGTH = 200
_EVIDENCE_SUMMARY_MAX_LENGTH = 1_000
_EVIDENCE_MAX_AGE_DAYS = 550
_FINANCIAL_EVIDENCE_MAX_AGE_DAYS = 550
_DCF_VALIDATION_CACHE_TOKEN = object()


class _ProcessValidationToken:
    """An unforgeable in-process marker that remains stable across safe copies."""

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self


_TYPE5_EXTERNAL_VALIDATION_TOKEN = _ProcessValidationToken()
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_today() -> date:
    """Return the A-share market calendar date, independent of host timezone."""
    return datetime.now(_SHANGHAI).date()


def _cached_dcf_validation(m: Mapping[str, Any], result: Any, key: str) -> Optional[bool]:
    """Read one transient validation result bound to these exact objects."""
    if not isinstance(result, Mapping):
        return None
    cache = m.get("_dcf_validation_cache")
    if (
        not isinstance(cache, Mapping)
        or cache.get("token") is not _DCF_VALIDATION_CACHE_TOKEN
        or cache.get("metric_id") != id(m)
        or cache.get("result_id") != id(result)
    ):
        return None
    value = cache.get(key)
    return value if isinstance(value, bool) else None


def _safe_float(value: Any) -> Optional[float]:
    """只接受有限实数，拒绝NaN/Inf和布尔值。"""
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _canonical_evidence_code(value: Any) -> str:
    """Normalize a security identity for evidence binding without guessing markets."""
    if value is None or isinstance(value, (bool, np.bool_)):
        return ""
    text = str(value).strip().upper()
    if re.fullmatch(r"\d{1,6}", text):
        return text.zfill(6)
    return text if re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", text) else ""


def _evidence_reference_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value.strip() else None


def _evidence_id_is_bound(evidence_id: str, expected_code: str) -> bool:
    if not expected_code:
        return True
    tokens = {
        token.upper()
        for token in re.split(r"[^A-Za-z0-9._]+", evidence_id)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", token)
    }
    # Evidence identifiers must contain the exact canonical security code.
    # Padding an arbitrary numeric token here allowed e.g. ``report-1`` to
    # impersonate the distinct A-share identity ``000001``.
    return expected_code.upper() in tokens


def _normalise_score_evidence(
    container: Mapping[str, Any],
    key: str,
    *,
    expected_code: Any = None,
    reference_date: Any = None,
    max_age_days: int = _EVIDENCE_MAX_AGE_DAYS,
) -> tuple[Optional[float], Optional[dict[str, str]]]:
    """Accept a qualitative score only with traceable, dated source metadata."""
    evidence_level = container.get(f"{key}_evidence_level")
    if evidence_level not in {"primary", "derived_proxy"}:
        # Partial/missing formula diagnostics remain replayable under
        # ``quantitative_evidence`` but are not production scores.  Enforce
        # that boundary here too.  Missing labels are not silently promoted
        # to primary evidence, and alternate callers cannot re-inject a
        # finite default-backed number after enrichment rejected it.
        return None, None
    score = _safe_float(container.get(key))
    evidence = container.get(f"{key}_evidence")
    if score is None or not 0 <= score <= 10 or not isinstance(evidence, Mapping):
        return None, None
    if set(evidence) - _EVIDENCE_ALLOWED_KEYS:
        return None, None
    source_raw = evidence.get("source")
    evidence_id_raw = evidence.get("evidence_id")
    as_of_raw = evidence.get("as_of")
    summary_raw = evidence.get("summary")
    if not all(isinstance(value, str) for value in (source_raw, evidence_id_raw, as_of_raw)):
        return None, None
    if summary_raw is not None and not isinstance(summary_raw, str):
        return None, None
    source = source_raw.strip()
    evidence_id = evidence_id_raw.strip()
    as_of = as_of_raw.strip()
    if (
        not source
        or not evidence_id
        or len(source) > _EVIDENCE_SOURCE_MAX_LENGTH
        or len(evidence_id) > _EVIDENCE_ID_MAX_LENGTH
        or any(ord(character) < 32 for character in source + evidence_id)
    ):
        return None, None
    try:
        evidence_date = date.fromisoformat(as_of)
    except (TypeError, ValueError):
        return None, None
    today = _shanghai_today()
    reference = _evidence_reference_date(reference_date) or today
    if (
        evidence_date.isoformat() != as_of
        or reference > today
        or evidence_date > today
        or evidence_date > reference
        or (reference - evidence_date).days > max_age_days
    ):
        return None, None
    expected = _canonical_evidence_code(expected_code)
    if expected and not _evidence_id_is_bound(evidence_id, expected):
        return None, None
    normalised = {"source": source, "evidence_id": evidence_id, "as_of": as_of}
    summary = summary_raw.strip() if isinstance(summary_raw, str) else ""
    if len(summary) > _EVIDENCE_SUMMARY_MAX_LENGTH or any(ord(character) < 32 for character in summary):
        return None, None
    if summary:
        normalised["summary"] = summary
    return score, normalised


def _verified_score(container: Mapping[str, Any], key: str) -> Optional[float]:
    return _normalise_score_evidence(
        container,
        key,
        expected_code=container.get("code"),
        reference_date=container.get("source_trade_date"),
    )[0]


def _verified_market_coldness_score(container: Mapping[str, Any]) -> Optional[float]:
    """Require a source-bound production ledger that independently replays."""

    reference = _evidence_reference_date(container.get("source_trade_date"))
    code = _canonical_evidence_code(container.get("code"))
    if reference is None or not code:
        return None
    record = {
        "market_coldness_score": container.get("market_coldness_score"),
        "market_coldness_score_evidence_level": container.get("market_coldness_score_evidence_level"),
        "market_coldness_score_evidence": container.get("market_coldness_score_evidence"),
        "components": container.get("market_coldness_components"),
    }
    try:
        return validate_market_coldness_evidence_record(
            record,
            expected_code=code,
            expected_session=reference,
        )
    except (MarketColdnessScoringError, TypeError, ValueError):
        return None


def _evidence_reason(container: Mapping[str, Any], key: str, fallback: str) -> str:
    """Return a short human-facing evidence description, never an internal ID.

    Evidence IDs remain in the JSON/audit record for replay, but identifiers
    such as ``patch6-observable-outcomes-v*`` are implementation details and
    are meaningless in an investment-screening page.
    """
    _score, evidence = _normalise_score_evidence(
        container,
        key,
        expected_code=container.get("code"),
        reference_date=container.get("source_trade_date"),
    )
    if evidence is None:
        return fallback
    evidence_id = str(evidence.get("evidence_id") or "")
    summary = str(evidence.get("summary") or "").strip()
    if evidence_id.startswith(f"{QUANTITATIVE_EVIDENCE_MODEL_ID}:"):
        automatic_reasons = {
            "accounting_integrity_score": "财务报表与现金流数据",
            "business_model_score": "经营效率与现金流数据",
            "catalyst_score": "财务趋势与同行数据",
            "growth_quality_score": "收入利润与现金流数据",
            "growth_sustainability_score": "增长趋势与行业数据",
            "industry_bubble_score": "行业营收与利润数据",
            "industry_durability_score": "行业营收与利润数据",
            "management_alignment_score": "股本与现金流数据",
            "moat_score": "盈利能力与同行数据",
            "moat_durability_score": "多年盈利稳定性数据",
            "runway_score": "增长趋势与同行数据",
            "technology_score": "研发与经营数据",
            "type3_bubble_score": "行业与估值数据",
        }
        return automatic_reasons.get(key, "可核验的财务与行业数据")
    # A researcher-supplied Chinese summary is useful on the page, but reject
    # machine syntax even if it was accidentally put into ``summary``.
    technical_marker = re.compile(
        r"(?:patch\d|observable|model=|evidence_level=|(?:^|[_:-])v\d|[a-z]+_[a-z]+)",
        re.IGNORECASE,
    )
    if summary and not technical_marker.search(summary):
        return _compact_reason(summary)
    source = str(evidence.get("source") or "").strip()
    if "东方财富" in source or "eastmoney" in source.lower():
        return "东方财富的可核验数据"
    return "已登记的外部证据"


def _normalise_structured_growth_evidence(
    value: Any,
    *,
    expected_code: Any,
    reference_date: Any,
    missing_label: str,
    content_keys: tuple[str, ...],
) -> dict[str, Any]:
    """Preserve structured research while preventing an untraceable complete flag."""
    if not isinstance(value, Mapping):
        return {"status": "missing", "missing": [missing_label]}
    result = dict(value)
    status = str(result.get("status") or "").strip().lower()
    if status in {"missing", "partial", "invalid"}:
        result["status"] = status
        return result
    if status != "complete":
        result["status"] = "invalid"
        result["validation_error"] = "证据状态无效"
        return result
    expected = _canonical_evidence_code(expected_code)
    supplied_code = _canonical_evidence_code(result.get("security_code"))
    if result.get("security_code") is not None and (not supplied_code or supplied_code != expected):
        result["status"] = "invalid"
        result["validation_error"] = "完整证据的证券代码不匹配"
        return result
    metadata = {key: result[key] for key in _EVIDENCE_ALLOWED_KEYS if key in result}
    _score, normalized = _normalise_score_evidence(
        {
            "structured_score": 1.0,
            "structured_score_evidence": metadata,
            "structured_score_evidence_level": "primary",
        },
        "structured_score",
        expected_code=expected_code,
        reference_date=reference_date,
    )
    if normalized is None:
        result["status"] = "invalid"
        result["validation_error"] = "完整证据缺少有效来源、证券绑定或日期"
        return result
    content_complete = any(
        isinstance(records, (list, tuple)) and bool(records) and all(isinstance(record, Mapping) for record in records)
        for key in content_keys
        if (records := result.get(key)) is not None
    )
    if not content_complete:
        result["status"] = "invalid"
        result["validation_error"] = "完整证据缺少可核验明细"
    return result


def _date_key(record: Mapping[str, Any]) -> str:
    return str(record.get("REPORT_DATE") or "")[:10]


class _ChronologicalRecords(list[dict[str, Any]]):
    """Internal marker for records already copied, filtered and sorted.

    Several metric helpers consume the same annual/interim dataset.  Returning
    an ordinary list made each helper rebuild and sort identical dictionaries.
    The marker remains fully list-compatible (including JSON serialization),
    while allowing subsequent private helper calls to reuse the validated
    chronological copy owned by this one ``extract_metrics`` invocation.
    """


def _sorted_records(value: Any) -> list[dict]:
    if isinstance(value, _ChronologicalRecords):
        return value
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        report_date = _date_key(item)
        if not (len(report_date) == 4 and report_date.isdigit()):
            try:
                if date.fromisoformat(report_date).isoformat() != report_date:
                    continue
            except ValueError:
                continue
        records.append(dict(item))
    records.sort(key=_date_key)
    return _ChronologicalRecords(records)


def _attributable_equity(row: Mapping[str, Any]) -> Optional[float]:
    """Return parent equity only; total equity alone is not an ROE proxy."""
    for key in _PARENT_EQUITY_KEYS:
        value = _safe_float(row.get(key))
        if value is not None and value > 0:
            return value
    total = _safe_float(row.get("TOTAL_EQUITY"))
    minority = next(
        (value for key in _MINORITY_EQUITY_KEYS if (value := _safe_float(row.get(key))) is not None),
        None,
    )
    if total is not None and total > 0 and minority is not None and 0 <= minority < total:
        return total - minority
    return None


def _annual_attributable_equity(records: Any) -> dict[int, float]:
    """Choose one attributable-equity observation per calendar year."""
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in _sorted_records(records):
        year = _report_year(row)
        equity = _attributable_equity(row)
        if year is None or equity is None:
            continue
        date = _date_key(row)
        candidate = (date.endswith("12-31"), date, equity)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _annual_invested_capital(records: Any) -> dict[int, float]:
    """Match consolidated operating profit with total equity + debt - cash."""
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in _sorted_records(records):
        year = _report_year(row)
        equity = _safe_float(row.get("TOTAL_EQUITY"))
        gross_debt, cash, debt_known = extract_debt_and_cash([dict(row)])
        if year is None or equity is None or equity <= 0 or not debt_known:
            continue
        capital = equity + gross_debt - cash
        if not math.isfinite(capital) or capital <= 0:
            continue
        date = _date_key(row)
        candidate = (date.endswith("12-31"), date, capital)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _compute_cagr(values: list[float]) -> Optional[float]:
    if len(values) < 2 or values[0] <= 0 or values[-1] <= 0:
        return None
    result = (values[-1] / values[0]) ** (1.0 / (len(values) - 1)) - 1.0
    return result if math.isfinite(result) else None


def _report_year(record: Mapping[str, Any]) -> Optional[int]:
    text = _date_key(record)
    try:
        year = int(text[:4])
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2200 else None


def _dated_cagr(points: list[tuple[int, float]]) -> Optional[float]:
    if len(points) < 2:
        return None
    first_year, first_value = points[0]
    last_year, last_value = points[-1]
    elapsed = last_year - first_year
    if elapsed <= 0 or first_value <= 0 or last_value <= 0:
        return None
    result = (last_value / first_value) ** (1.0 / elapsed) - 1.0
    return result if math.isfinite(result) else None


def _dated_growth_rates(points: list[tuple[int, float]]) -> list[float]:
    rates: list[float] = []
    for (prior_year, prior), (year, current) in zip(points, points[1:]):
        elapsed = year - prior_year
        if elapsed > 0 and prior > 0 and current > 0:
            rate = (current / prior) ** (1.0 / elapsed) - 1.0
            if math.isfinite(rate):
                rates.append(rate)
    return rates


def _window_points(points: list[tuple[int, float]], years: int) -> list[tuple[int, float]]:
    if not points:
        return []
    cutoff = points[-1][0] - (years - 1)
    return [point for point in points if point[0] >= cutoff]


def _years_are_consecutive(years: Any, count: int) -> bool:
    if not isinstance(years, (list, tuple)) or len(years) < count:
        return False
    recent = list(years[-count:])
    return all(
        not isinstance(year, (bool, np.bool_))
        and not isinstance(prior, (bool, np.bool_))
        and isinstance(year, (int, np.integer))
        and isinstance(prior, (int, np.integer))
        and int(year) - int(prior) == 1
        for prior, year in zip(recent, recent[1:])
    )


def _aligned_consecutive(values: Any, years: Any, count: int) -> bool:
    """Require a finite, one-to-one, ordered consecutive annual ledger."""
    if not isinstance(values, (list, tuple)) or not isinstance(years, (list, tuple)):
        return False
    if len(values) != len(years) or len(values) < count:
        return False
    normalized_years: list[int] = []
    for raw_year, raw_value in zip(years, values):
        if (
            isinstance(raw_year, (bool, np.bool_))
            or not isinstance(raw_year, (int, np.integer))
            or _safe_float(raw_value) is None
        ):
            return False
        normalized_years.append(int(raw_year))
    return normalized_years == sorted(set(normalized_years)) and _years_are_consecutive(normalized_years, count)


def _latest_complete_financial_year(m: Mapping[str, Any]) -> int | None:
    """Return the annual endpoint justified by the current report snapshot."""

    trade_date = _evidence_reference_date(m.get("source_trade_date"))
    financial_date = _evidence_reference_date(m.get("financial_indicator_as_of"))
    today = _shanghai_today()
    if trade_date is not None and trade_date > today:
        return None
    if financial_date is not None:
        if (
            financial_date > today
            or (trade_date is not None and financial_date > trade_date)
            or (trade_date is not None and (trade_date - financial_date).days > _FINANCIAL_EVIDENCE_MAX_AGE_DAYS)
        ):
            return None
        return (
            financial_date.year if (financial_date.month, financial_date.day) == (12, 31) else financial_date.year - 1
        )
    return trade_date.year - 1 if trade_date is not None else None


def _aligned_current_consecutive(
    m: Mapping[str, Any],
    values: Any,
    years: Any,
    count: int,
) -> bool:
    """Bind a decision window to the latest complete annual reporting period."""

    expected_year = _latest_complete_financial_year(m)
    return bool(
        expected_year is not None and _aligned_consecutive(values, years, count) and int(years[-1]) == expected_year
    )


def _financial_metric_points(m: Mapping[str, Any], metric_name: str) -> list[tuple[int, float]]:
    values = m.get(f"{metric_name}_history", [])
    years = m.get(f"{metric_name}_years", [])
    if not isinstance(values, (list, tuple)) or not isinstance(years, (list, tuple)):
        return []
    points: list[tuple[int, float]] = []
    for raw_year, raw_value in zip(years, values):
        value = _safe_float(raw_value)
        if isinstance(raw_year, bool) or value is None:
            return []
        try:
            year = int(raw_year)
        except (TypeError, ValueError, OverflowError):
            return []
        if year in {item[0] for item in points}:
            return []
        points.append((year, value))
    points.sort()
    expected_year = _latest_complete_financial_year(m)
    if not points or expected_year is None or points[-1][0] != expected_year:
        return []
    return points


def _financial_metric_pair(m: Mapping[str, Any], metric_name: str) -> tuple[float, float] | None:
    points = _financial_metric_points(m, metric_name)
    if len(points) < 2 or points[-1][0] - points[-2][0] != 1:
        return None
    return points[-2][1], points[-1][1]


def _financial_metric_growth(m: Mapping[str, Any], metric_name: str) -> float | None:
    pair = _financial_metric_pair(m, metric_name)
    if pair is None or pair[0] == 0:
        return None
    return (pair[1] - pair[0]) / abs(pair[0])


def _current_annual_change(
    m: Mapping[str, Any],
    values_key: str,
    years_key: str,
) -> float | None:
    """Recompute a one-year change only from the current bound annual pair."""

    values = m.get(values_key)
    years = m.get(years_key)
    if not _aligned_current_consecutive(m, values, years, 2):
        return None
    prior = _safe_float(values[-2])
    current = _safe_float(values[-1])
    if prior is None or current is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


def _score_higher_is_safer(value: float | None, *, strong: float, minimum: float) -> int | None:
    if value is None:
        return None
    return 2 if value >= strong else 1 if value >= minimum else 0


def _score_lower_is_safer(value: float | None, *, strong: float, maximum: float) -> int | None:
    if value is None:
        return None
    return 2 if value <= strong else 1 if value <= maximum else 0


def _first_finite(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _financial_regulatory_trap_points(m: Mapping[str, Any]) -> tuple[list[int], bool, str]:
    """Return five sector-native value-trap checks without industrial cash-flow proxies."""
    industry = str(m.get("industry") or "")
    points: list[int | None]
    if industry == "BANK":
        thresholds = FINANCIAL_REGULATORY_THRESHOLDS["BANK"]
        capital = _safe_float(m.get("capital_adequacy_ratio"))
        tier1 = _safe_float(m.get("tier1_capital_adequacy_ratio"))
        capital_point = (
            None
            if capital is None or tier1 is None
            else 2
            if capital >= thresholds["capital_screening_buffer"] and tier1 >= thresholds["tier1_screening_buffer"]
            else 1
            if capital >= thresholds["capital_min"] and tier1 >= thresholds["tier1_min"]
            else 0
        )
        points = [
            capital_point,
            _score_lower_is_safer(
                _safe_float(m.get("nonperforming_loan_ratio")),
                strong=0.015,
                maximum=0.03,
            ),
            _score_higher_is_safer(
                _safe_float(m.get("loan_provision_coverage_proxy")),
                strong=2.0,
                minimum=1.5,
            ),
            _score_higher_is_safer(
                _safe_float(m.get("net_interest_margin")),
                strong=0.018,
                minimum=0.012,
            ),
            _score_higher_is_safer(
                _first_finite(m.get("indicator_weighted_roe"), m.get("roe")),
                strong=0.10,
                minimum=0.06,
            ),
        ]
        label = "银行监管"
    elif industry == "INSURANCE":
        thresholds = FINANCIAL_REGULATORY_THRESHOLDS["INSURANCE"]
        nbv_growth = _financial_metric_growth(m, "new_business_value")
        points = [
            _score_higher_is_safer(
                _safe_float(m.get("solvency_adequacy_ratio")),
                strong=thresholds["solvency_screening_buffer"],
                minimum=thresholds["solvency_min"],
            ),
            _score_higher_is_safer(
                _safe_float(m.get("new_business_value_margin")),
                strong=0.20,
                minimum=0.10,
            ),
            None if nbv_growth is None else 2 if nbv_growth >= 0 else 1 if nbv_growth >= -0.20 else 0,
            _score_lower_is_safer(
                _safe_float(m.get("life_surrender_rate")),
                strong=0.02,
                maximum=0.04,
            ),
            _score_higher_is_safer(
                _first_finite(m.get("indicator_weighted_roe"), m.get("roe")),
                strong=0.10,
                minimum=0.05,
            ),
        ]
        label = "保险监管"
    elif industry == "SECURITIES":
        thresholds = FINANCIAL_REGULATORY_THRESHOLDS["SECURITIES"]
        points = [
            _score_higher_is_safer(
                _safe_float(m.get("risk_coverage_ratio")),
                strong=1.50,
                minimum=thresholds["risk_coverage_min"],
            ),
            _score_higher_is_safer(
                _safe_float(m.get("capital_leverage_ratio")),
                strong=0.12,
                minimum=thresholds["capital_leverage_min"],
            ),
            _score_higher_is_safer(
                _safe_float(m.get("liquidity_coverage_ratio")),
                strong=1.20,
                minimum=thresholds["liquidity_coverage_min"],
            ),
            _score_higher_is_safer(
                _safe_float(m.get("net_stable_funding_ratio")),
                strong=1.20,
                minimum=thresholds["net_stable_funding_min"],
            ),
            _score_higher_is_safer(
                _safe_float(m.get("net_capital_to_liabilities_ratio")),
                strong=0.12,
                minimum=0.08,
            ),
        ]
        label = "券商风控"
    else:
        return [], False, "金融监管证据不适用"
    complete = all(point is not None for point in points)
    clean = [int(point or 0) for point in points]
    reason = f"{label}满分{sum(point == 2 for point in clean)}项" if complete else f"{label}证据缺失"
    return clean, complete, reason


def _financial_hard_regulatory_breach(m: Mapping[str, Any]) -> bool:
    industry = str(m.get("industry") or "")
    if industry == "BANK":
        thresholds = FINANCIAL_REGULATORY_THRESHOLDS["BANK"]
        capital = _safe_float(m.get("capital_adequacy_ratio"))
        tier1 = _safe_float(m.get("tier1_capital_adequacy_ratio"))
        return bool(
            capital is not None
            and tier1 is not None
            and (capital < thresholds["capital_min"] or tier1 < thresholds["tier1_min"])
        )
    if industry == "INSURANCE":
        solvency = _safe_float(m.get("solvency_adequacy_ratio"))
        return bool(solvency is not None and solvency < FINANCIAL_REGULATORY_THRESHOLDS["INSURANCE"]["solvency_min"])
    if industry == "SECURITIES":
        thresholds = FINANCIAL_REGULATORY_THRESHOLDS["SECURITIES"]
        values = (
            (_safe_float(m.get("risk_coverage_ratio")), thresholds["risk_coverage_min"]),
            (_safe_float(m.get("capital_leverage_ratio")), thresholds["capital_leverage_min"]),
            (_safe_float(m.get("liquidity_coverage_ratio")), thresholds["liquidity_coverage_min"]),
            (_safe_float(m.get("net_stable_funding_ratio")), thresholds["net_stable_funding_min"]),
        )
        return all(value is not None for value, _minimum in values) and any(
            value < minimum for value, minimum in values if value is not None
        )
    return False


def _financial_catalyst_score(m: Mapping[str, Any]) -> tuple[float, bool, str]:
    """Score only consecutive, source-backed sector reversion signals."""
    industry = str(m.get("industry") or "")
    profit_growth = _safe_float(m.get("profit_1yr_change"))
    score = 0.0
    signals: list[str] = []
    if industry == "BANK":
        nim = _financial_metric_pair(m, "net_interest_margin")
        npl = _financial_metric_pair(m, "nonperforming_loan_ratio")
        capital = _financial_metric_pair(m, "capital_adequacy_ratio")
        complete = nim is not None and npl is not None and capital is not None and profit_growth is not None
        if nim is not None and nim[1] > nim[0]:
            score += 3.0
            signals.append("息差回升")
        if npl is not None and npl[1] < npl[0]:
            score += 2.0
            signals.append("不良下降")
        if capital is not None and capital[1] > capital[0]:
            score += 2.0
            signals.append("资本增厚")
    elif industry == "INSURANCE":
        nbv = _financial_metric_growth(m, "new_business_value")
        nbv_margin = _financial_metric_pair(m, "new_business_value_margin")
        solvency = _financial_metric_pair(m, "solvency_adequacy_ratio")
        surrender = _financial_metric_pair(m, "life_surrender_rate")
        complete = (
            nbv is not None
            and nbv_margin is not None
            and solvency is not None
            and surrender is not None
            and profit_growth is not None
        )
        if nbv is not None and nbv > 0:
            score += 3.0
            signals.append("新业务增")
        if nbv_margin is not None and nbv_margin[1] > nbv_margin[0]:
            score += 2.0
            signals.append("价值率升")
        if solvency is not None and solvency[1] >= solvency[0]:
            score += 1.0
            signals.append("偿付改善")
        if surrender is not None and surrender[1] < surrender[0]:
            score += 1.0
            signals.append("退保下降")
    elif industry == "SECURITIES":
        risk = _financial_metric_pair(m, "risk_coverage_ratio")
        leverage = _financial_metric_pair(m, "capital_leverage_ratio")
        liquidity = _financial_metric_pair(m, "liquidity_coverage_ratio")
        funding = _financial_metric_pair(m, "net_stable_funding_ratio")
        complete = (
            risk is not None
            and leverage is not None
            and liquidity is not None
            and funding is not None
            and profit_growth is not None
        )
        for pair, weight, label in (
            (risk, 2.0, "覆盖改善"),
            (leverage, 2.0, "杠杆改善"),
            (liquidity, 1.0, "流动性升"),
            (funding, 1.0, "稳定资金升"),
        ):
            if pair is not None and pair[1] > pair[0]:
                score += weight
                signals.append(label)
    else:
        return 0.0, False, "金融催化证据不适用"
    if profit_growth is not None and profit_growth > 0.10:
        score += 3.0
        signals.append("利润回升")
    elif profit_growth is not None and profit_growth > 0:
        score += 1.0
        signals.append("利润转正增")
    return min(10.0, score), complete, f"金融回归{len(signals)}项" if signals else "金融回归尚未出现"


def _same_period_pair(records: Any, keys: tuple[str, ...]) -> tuple[Optional[float], Optional[float], str]:
    """Return latest/prior values only for an exact prior-year period."""
    rows = _sorted_records(records)
    if not rows:
        return None, None, "missing_same_period_comparator"
    latest = rows[-1]
    latest_date = _date_key(latest)
    latest_year = _report_year(latest)
    latest_value = next(
        (value for key in keys if (value := _safe_float(latest.get(key))) is not None),
        None,
    )
    if latest_year is None or len(latest_date) < 10 or latest_value is None:
        return latest_value, None, "missing_same_period_comparator"
    for prior in reversed(rows[:-1]):
        prior_date = _date_key(prior)
        if _report_year(prior) == latest_year - 1 and len(prior_date) >= 10 and prior_date[4:10] == latest_date[4:10]:
            prior_value = next(
                (value for key in keys if (value := _safe_float(prior.get(key))) is not None),
                None,
            )
            if prior_value is None:
                return latest_value, None, "invalid_same_period_base"
            return latest_value, prior_value, "same_period_yoy"
    return latest_value, None, "missing_same_period_comparator"


def _same_period_yoy(records: Any, keys: tuple[str, ...]) -> tuple[Optional[float], str]:
    """Return YoY only for an exact prior-year interim period comparator."""
    latest_value, prior_value, basis = _same_period_pair(records, keys)
    if basis != "same_period_yoy" or latest_value is None or prior_value is None:
        return None, basis
    if prior_value == 0:
        return None, "invalid_same_period_base"
    return (latest_value - prior_value) / abs(prior_value), basis


def _growth_rates(values: list[float]) -> list[float]:
    rates: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0:
            rate = current / previous - 1.0
            if math.isfinite(rate):
                rates.append(rate)
    return rates


def _compute_growth_consistency(values: list[float]) -> Optional[float]:
    rates = _growth_rates(values)
    cagr = _compute_cagr(values)
    if len(rates) < 2 or cagr is None:
        return None
    if abs(cagr) < 0.001:
        return 2.0
    result = float(np.std(rates)) / abs(cagr)
    return result if math.isfinite(result) else None


def _growth_slope(values: list[float]) -> Optional[float]:
    rates = _growth_rates(values)
    if len(rates) < 2:
        return None
    result = float(np.polyfit(np.arange(len(rates), dtype=float), rates, 1)[0])
    return result if math.isfinite(result) else None


def _trend_adjusted_growth(values: list[float]) -> Optional[float]:
    """兼顾长短期且对减速保守，禁止直接取3/5年CAGR较高者。"""
    if len(values) < 2:
        return None
    cagr_long = _compute_cagr(values[-5:])
    cagr_recent = _compute_cagr(values[-3:]) if len(values) >= 3 else cagr_long
    if cagr_long is None:
        return cagr_recent
    if cagr_recent is None:
        return cagr_long
    # 减速时采用较低的近期值；加速时仍保留35%长期基线，避免短期尖峰。
    if cagr_recent < cagr_long:
        return cagr_recent
    return 0.65 * cagr_recent + 0.35 * cagr_long


def _combine_long_recent_growth(cagr_recent: Optional[float], cagr_long: Optional[float]) -> Optional[float]:
    if cagr_long is None:
        return cagr_recent
    if cagr_recent is None:
        return cagr_long
    if cagr_recent < cagr_long:
        return cagr_recent
    return 0.65 * cagr_recent + 0.35 * cagr_long


def _score_0_10(value: Any, thresholds: list[tuple[float, float]]) -> float:
    number = _safe_float(value)
    if number is None or not thresholds:
        return 0.0
    for index, (threshold, score) in enumerate(thresholds):
        if number <= threshold:
            if index == 0:
                return float(score)
            prior_threshold, prior_score = thresholds[index - 1]
            if threshold == prior_threshold:
                return float(score)
            ratio = (number - prior_threshold) / (threshold - prior_threshold)
            return float(prior_score + ratio * (score - prior_score))
    return float(thresholds[-1][1])


def _score_type1_fcf_yield(value: Any) -> float:
    """Map normalised FCFF yield to Patch6's exact 3/5/8/12% bands."""
    fcf_yield = _safe_float(value)
    if fcf_yield is None:
        return 0.0
    if fcf_yield < 0.03:
        return round(max(0.0, min(2.0, fcf_yield / 0.03 * 2.0)), 1)
    if fcf_yield < 0.05:
        return round(3.0 + (fcf_yield - 0.03) / 0.02, 1)
    if fcf_yield < 0.08:
        return round(5.0 + (fcf_yield - 0.05) / 0.03, 1)
    if fcf_yield <= 0.12:
        return round(7.0 + (fcf_yield - 0.08) / 0.04, 1)
    return round(min(10.0, 9.0 + (fcf_yield - 0.12) / 0.08), 1)


def _get_bench(benchmarks: Mapping[str, Mapping[str, Any]], industry: str, key: str, default: Any = None) -> Any:
    industry_bucket = {} if industry == "DEFAULT" else benchmarks.get(industry, {})
    for bucket in (industry_bucket, benchmarks.get("ALL", {})):
        value = _safe_float(bucket.get(key))
        if value is not None:
            return value
    return default


def _compact_reason(value: Any) -> str:
    text = " ".join(str(value or "数据不足").split())
    if len(text) <= EVIDENCE_MAX_LENGTH:
        return text

    content_limit = EVIDENCE_MAX_LENGTH - 1
    best_boundary = ""
    for match in re.finditer(r"[；;。！？!?，,]", text):
        candidate = text[: match.start()].rstrip()
        if len(candidate) > content_limit:
            break
        if candidate:
            best_boundary = candidate
    if best_boundary:
        return best_boundary + "…"

    prefix = text[:content_limit].rstrip()
    word_boundary = prefix.rfind(" ")
    if word_boundary >= max(4, content_limit // 2):
        prefix = prefix[:word_boundary].rstrip()
    return prefix + "…"


def _format_rmb(value: Any) -> str:
    """Format RMB evidence without ambiguous technical missing-value markers."""
    number = _safe_float(value)
    if number is None:
        return "暂无数据"
    absolute = abs(number)
    if absolute >= 1e8:
        return f"{number / 1e8:.2f}亿"
    if absolute >= 1e4:
        return f"{number / 1e4:.2f}万"
    return f"{number:.2f}元"


def _sanitize_scores(
    scores: Mapping[str, Any],
    expected: Mapping[str, float],
    *,
    decimals: int = 2,
) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in expected:
        value = _safe_float(scores.get(key))
        result[key] = round(min(10.0, max(0.0, value if value is not None else 0.0)), decimals)
    return result


def _weighted_total(scores: Mapping[str, Any], weights: Mapping[str, float], *, decimals: int = 2) -> float:
    clean = _sanitize_scores(scores, weights, decimals=decimals)
    return round(sum(clean[key] * weight for key, weight in weights.items()), 1)


def _finish(
    type_key: str,
    scores: Mapping[str, Any],
    reasons: Mapping[str, Any],
    *,
    veto: bool = False,
    total_cap: Optional[float] = None,
    extra_condition: bool = True,
    applicable: bool = True,
    evidence_complete: bool = True,
    status_override: Optional[str] = None,
    missing_dimensions: Sequence[str] | None = None,
) -> tuple[bool, float, dict, dict]:
    """统一清洗、加权、舍入与触发，保证显示值就是判定值。"""
    weights = TYPE_WEIGHTS[type_key]
    score_decimals = 3 if type_key == "type7" else 2
    missing_score_keys = [key for key in weights if _safe_float(scores.get(key)) is None]
    raw_reasons = dict(reasons)
    if missing_score_keys:
        # Public payloads keep a fixed numeric radar shape, so the final
        # serializer must still emit a number for every dimension.  Missing or
        # non-finite scorer output is therefore represented by a numeric zero,
        # but it must never retain a "complete" evidence state: downstream
        # consumers otherwise cannot distinguish "unknown" from a measured
        # zero-point failure.
        evidence_complete = False
        missing_labels = "/".join(missing_score_keys)
        raw_reasons.setdefault("_missing", f"{missing_labels}评分输入缺失")
        raw_reasons["_score_placeholder"] = f"{missing_labels}缺失以0占位"
    if not applicable or evidence_complete:
        declared_missing: list[str] = []
    else:
        requested_missing = list(missing_dimensions) if missing_dimensions is not None else missing_score_keys
        if not requested_missing:
            # Older callers can still supply the historical four-tuple.  An
            # incomplete result without a dimension ledger must fail closed:
            # every framework dimension remains unknown until a scorer
            # explicitly narrows the list.
            requested_missing = list(weights)
        unknown = set(requested_missing) - set(weights)
        if unknown:
            raise ValueError(f"{type_key} unknown missing dimensions: {sorted(unknown)}")
        declared_missing = [key for key in weights if key in requested_missing]
    raw_reasons[_DECISION_MISSING_DIMENSIONS_REASON] = declared_missing
    clean_scores = _sanitize_scores(scores, weights, decimals=score_decimals)
    clean_reasons = {key: _compact_reason(raw_reasons.get(key)) for key in weights}
    for key, value in raw_reasons.items():
        if key.startswith("_"):
            clean_reasons[key] = list(value) if key == _DECISION_MISSING_DIMENSIONS_REASON else _compact_reason(value)
    if type_key != "type7":
        if not applicable:
            clean_reasons["_score_quality"] = "模型不适用，0分不是结论"
        elif evidence_complete:
            clean_reasons["_score_quality"] = "完整证据评分"
        else:
            clean_reasons.setdefault("_missing", "关键评分证据不完整")
            clean_reasons["_score_quality"] = "缺失项以0占位" if missing_score_keys else "证据不足，分数仅供诊断"
    total = _weighted_total(clean_scores, weights, decimals=score_decimals)
    if total_cap is not None:
        total = min(total, round(float(total_cap), 1))
    qualifies = applicable and evidence_complete and total >= QUALIFY_THRESHOLD and not veto and extra_condition
    if status_override is not None:
        if status_override not in TYPE_STATUSES:
            raise ValueError(f"unknown type status: {status_override}")
        status = status_override
    elif not applicable:
        status = STATUS_NOT_APPLICABLE
    elif veto:
        # A separately evidenced hard veto remains actionable even if another
        # dimension is incomplete.  Scorers must pass ``veto=True`` only for a
        # confirmed Patch6 condition, never for a score manufactured by missing
        # evidence.
        status = STATUS_VETOED
    elif not evidence_complete:
        status = STATUS_INSUFFICIENT_EVIDENCE
    elif qualifies:
        status = STATUS_TRIGGERED
    elif total >= QUALIFY_THRESHOLD and not extra_condition and not veto:
        status = STATUS_CONDITIONAL
    elif total >= 5.0:
        status = STATUS_OBSERVE
    else:
        status = STATUS_NOT_TRIGGERED
    # “不适用/证据不足”描述的是框架适用性或数据质量，不能同时输出
    # 公司层面的一票否决。部分评分器会先基于现有子项形成 veto，随后
    # 才发现决定性证据缺失；在统一出口清除它，避免下游仅凭
    # ``_veto`` 把 N/A 误报成公司失败。
    if status in {STATUS_NOT_APPLICABLE, STATUS_INSUFFICIENT_EVIDENCE}:
        clean_reasons.pop("_veto", None)
    clean_reasons["_status"] = status
    clean_reasons["_applicable"] = "yes" if applicable else "no"
    clean_reasons["_evidence"] = "complete" if evidence_complete else "incomplete"
    return qualifies, total, clean_scores, clean_reasons


def _not_applicable(type_key: str, reason: str):
    scores = {key: 0.0 for key in TYPE_WEIGHTS[type_key]}
    reasons = {key: reason for key in TYPE_WEIGHTS[type_key]}
    reasons["_scope"] = reason
    return _finish(type_key, scores, reasons, applicable=False)


def _insufficient_evidence(type_key: str, reason: str):
    """Return a structurally complete result without pretending missing data is failure."""
    scores = {key: 0.0 for key in TYPE_WEIGHTS[type_key]}
    reasons = {key: reason for key in TYPE_WEIGHTS[type_key]}
    reasons["_missing"] = reason
    if type_key != "type7":
        reasons["_score_placeholder"] = "全部子分为缺失占位"
    return _finish(type_key, scores, reasons, evidence_complete=False)


def _valuation_skip_outcome(type_key: str, value: Any):
    """Translate a structured valuation skip without collapsing its semantics."""
    classification = normalize_dcf_skip_classification(value)
    if classification is None:
        return None
    category = classification["category"]
    if category == DCF_SKIP_MODEL_UNSUPPORTED:
        return _not_applicable(type_key, "估值模型不支持当前标的")
    if category == DCF_SKIP_ECONOMIC_NOT_APPLICABLE:
        return _not_applicable(type_key, "当前经济条件不适用估值")
    if category == DCF_SKIP_SOURCE_MISSING:
        return _insufficient_evidence(type_key, "估值源数据缺失")
    if category == DCF_SKIP_INCONSISTENT_SOURCE:
        return _insufficient_evidence(type_key, "估值来源口径不一致")
    if category == DCF_SKIP_INTERNAL_ERROR:
        scores = {key: 0.0 for key in TYPE_WEIGHTS[type_key]}
        reasons = {key: "估值计算异常" for key in TYPE_WEIGHTS[type_key]}
        reasons["_blocked"] = "估值计算异常"
        return _finish(
            type_key,
            scores,
            reasons,
            evidence_complete=False,
            status_override=STATUS_BLOCKED,
        )
    raise AssertionError(f"unhandled DCF skip category: {category}")


def extract_metrics(fin_data: Mapping[str, Any], quote_row: Mapping[str, Any], industry_code: str) -> dict[str, Any]:
    """按报告日期提取评分所需指标；任何非有限数都视为缺失。"""
    name = str(quote_row.get("name", "")).strip()
    raw_risk_status = quote_row.get("risk_status")
    risk_status = (
        ""
        if raw_risk_status is None or (not isinstance(raw_risk_status, str) and pd.isna(raw_risk_status))
        else str(raw_risk_status).strip()
    )
    upper_name = name.upper()
    if not risk_status:
        if name.endswith("退") or "退市" in name:
            risk_status = "delisting"
        elif re.match(r"^(?:S\*ST|\*ST|SST|ST)", upper_name):
            risk_status = "special_treatment"
    quote_status = str(quote_row.get("quote_status") or "").strip().lower()
    price_source = str(quote_row.get("price_source") or "").strip().lower()
    explicit_tradable = quote_row.get("tradable")
    tradable = (
        bool(explicit_tradable)
        if isinstance(explicit_tradable, (bool, np.bool_))
        else quote_status == "trading"
        if quote_status
        else None
    )
    reference_flags = (
        quote_row.get("is_reference_price"),
        quote_row.get("reference_price"),
        quote_row.get("price_is_reference"),
    )
    m: dict[str, Any] = {
        "code": str(quote_row.get("code", "")).strip(),
        "name": name,
        "industry": industry_code,
        "price": _safe_float(quote_row.get("price")),
        "pe": _safe_float(quote_row.get("pe")),
        "pb": _safe_float(quote_row.get("pb")),
        "market_cap": _safe_float(quote_row.get("market_cap")),
        # Eligibility normally removes these rows upstream.  Preserve the
        # flags so the scoring layer can still fail closed if one leaks in.
        "risk_status": risk_status,
        "tradable": tradable,
        "quote_status": quote_status,
        "price_source": price_source,
        "source_trade_date": str(quote_row.get("source_trade_date") or "").strip() or None,
        # Needed by evidence-completeness rules that lower the minimum annual
        # history only for genuinely recent listings.  The snapshot layer has
        # already validated the canonical YYYY-MM-DD value; preserving it here
        # avoids treating a mature company with missing history as a new IPO.
        "listing_date": str(quote_row.get("listing_date") or "").strip() or None,
        "reference_price": bool(
            any(isinstance(value, (bool, np.bool_)) and bool(value) for value in reference_flags)
            or quote_status in {"suspended_or_no_trade", "invalid_price"}
            or (price_source and price_source != "last_trade")
        ),
    }
    if m["pb"] is not None and m["pb"] <= 0:
        m["pb"] = None

    # The main-financial endpoint has mixed units and two historically easy to
    # misread fields (RDEXPEND is an amount; KCFJCXSYJLR is adjusted profit).
    # Keep the strict, provenance-rich derivation separate from the scorer and
    # expose only formula-backed latest values here.  An empty legacy snapshot
    # remains explicit missing evidence; malformed non-empty evidence raises so
    # the pipeline can quarantine that company instead of inventing a score.
    indicator_evidence: Mapping[str, Any] | None = None
    indicator_records = fin_data.get("indicators")
    empty_indicator_history = indicator_records is None or (
        isinstance(indicator_records, (list, tuple)) and not indicator_records
    )
    if not empty_indicator_history:
        code = str(m["code"] or "").strip()
        expected_code = code if re.fullmatch(r"\d{6}", code) else None
        raw_market = str(quote_row.get("market") or "").strip().upper()
        expected_market = raw_market if raw_market in {"SH", "SZ"} else None
        indicator_evidence = derive_main_financial_indicator_evidence(
            fin_data,
            expected_code=expected_code,
            expected_market=expected_market,
            industry_code=industry_code,
        )
    m["financial_indicator_evidence"] = (
        {
            "schema_version": indicator_evidence.get("schema_version"),
            "status": indicator_evidence.get("status"),
            "as_of": indicator_evidence.get("as_of"),
            "source_report": indicator_evidence.get("source_report"),
            "industry_code": indicator_evidence.get("industry_code"),
            "coverage": indicator_evidence.get("coverage"),
        }
        if isinstance(indicator_evidence, Mapping)
        else None
    )
    m["financial_indicator_status"] = (
        str(indicator_evidence.get("status")) if isinstance(indicator_evidence, Mapping) else "missing"
    )
    m["financial_indicator_as_of"] = (
        indicator_evidence.get("as_of") if isinstance(indicator_evidence, Mapping) else None
    )

    indicator_metrics = indicator_evidence.get("metrics", {}) if isinstance(indicator_evidence, Mapping) else {}

    def indicator_metric(name: str) -> Mapping[str, Any]:
        value = indicator_metrics.get(name) if isinstance(indicator_metrics, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    def indicator_latest(name: str) -> float | None:
        return _safe_float(indicator_metric(name).get("latest_value"))

    def indicator_history(name: str) -> tuple[list[float], list[int]]:
        points: list[tuple[int, float]] = []
        observations = indicator_metric(name).get("observations", [])
        if isinstance(observations, list):
            for observation in observations:
                if not isinstance(observation, Mapping):
                    continue
                report_date = str(observation.get("report_date") or "")
                value = _safe_float(observation.get("value"))
                if len(report_date) >= 4 and report_date[:4].isdigit() and value is not None:
                    points.append((int(report_date[:4]), value))
        points.sort()
        return [value for _, value in points], [year for year, _ in points]

    m["indicator_roic"] = indicator_latest("roic")
    m["indicator_weighted_roe"] = indicator_latest("weighted_roe")
    m["gross_margin"] = indicator_latest("gross_margin")
    m["indicator_net_margin"] = indicator_latest("net_margin")
    m["indicator_tax_rate"] = indicator_latest("tax_rate")
    m["rd_intensity"] = indicator_latest("rd_intensity")
    m["indicator_ocf_np_ratio"] = indicator_latest("operating_cashflow_to_net_profit")
    m["adjusted_profit_ratio"] = indicator_latest("adjusted_net_profit_to_net_profit")
    m["interest_bearing_debt_ratio"] = indicator_latest("interest_bearing_debt_ratio")
    for metric_name, output_prefix in (
        ("roic", "indicator_roic"),
        ("weighted_roe", "indicator_weighted_roe"),
        ("gross_margin", "gross_margin"),
        ("operating_cashflow_to_net_profit", "indicator_ocf_np"),
        ("adjusted_net_profit_to_net_profit", "adjusted_profit_ratio"),
        ("interest_bearing_debt_ratio", "interest_bearing_debt_ratio"),
    ):
        values, years = indicator_history(metric_name)
        m[f"{output_prefix}_history"] = values
        m[f"{output_prefix}_years"] = years
    gross_stability = indicator_metric("gross_margin").get("stability", {})
    m["gross_margin_cv"] = (
        _safe_float(gross_stability.get("coefficient_of_variation")) if isinstance(gross_stability, Mapping) else None
    )
    m["gross_margin_samples"] = (
        int(sample_count)
        if (sample_count := _safe_float(indicator_metric("gross_margin").get("sample_count"))) is not None
        and sample_count >= 0
        else 0
    )
    gross_trend = indicator_metric("gross_margin").get("trend", {})
    m["gross_margin_trend"] = (
        str(gross_trend.get("direction")) if isinstance(gross_trend, Mapping) else "insufficient_history"
    )

    sector_metric_names = {
        "BANK": (
            "net_interest_margin",
            "net_interest_spread",
            "capital_adequacy_ratio",
            "tier1_capital_adequacy_ratio",
            "nonperforming_loan_ratio",
            "loan_provision_ratio",
            "loan_provision_coverage_proxy",
            "total_deposits",
            "gross_loans",
            "loan_advances",
            "gross_loan_to_deposit_ratio",
        ),
        "INSURANCE": (
            "solvency_adequacy_ratio",
            "new_business_value_margin",
            "new_business_value",
            "earned_premium",
            "life_surrender_rate",
        ),
        "SECURITIES": (
            "capital_leverage_ratio",
            "capital_provisions_sum",
            "liquidity_coverage_ratio",
            "net_capital_to_liabilities_ratio",
            "proprietary_capital_ratio",
            "risk_coverage_ratio",
            "net_stable_funding_ratio",
        ),
    }
    applicable_sector_metrics = sector_metric_names.get(industry_code, ())
    m["financial_sector_evidence"] = None
    if applicable_sector_metrics:
        sector_evidence_metrics: dict[str, Any] = {}
        for metric_name in applicable_sector_metrics:
            metric = indicator_metric(metric_name)
            value = indicator_latest(metric_name)
            values, years = indicator_history(metric_name)
            m[metric_name] = value
            m[f"{metric_name}_history"] = values
            m[f"{metric_name}_years"] = years
            latest = metric.get("latest") if isinstance(metric, Mapping) else None
            sector_evidence_metrics[metric_name] = {
                "status": metric.get("status"),
                "latest_date": metric.get("latest_date"),
                "latest_value": value,
                "sample_count": metric.get("sample_count"),
                "formula": metric.get("formula"),
                "evidence_type": latest.get("evidence_type") if isinstance(latest, Mapping) else "missing",
                "evidence_label": latest.get("evidence_label") if isinstance(latest, Mapping) else "缺失",
                "source_report": latest.get("source_report") if isinstance(latest, Mapping) else None,
                "source_field": latest.get("source_field") if isinstance(latest, Mapping) else None,
            }
        m["financial_sector_evidence"] = {
            "schema_version": indicator_evidence.get("schema_version")
            if isinstance(indicator_evidence, Mapping)
            else None,
            "industry_code": industry_code,
            "as_of": indicator_evidence.get("as_of") if isinstance(indicator_evidence, Mapping) else None,
            "rule_version": FINANCIAL_SECTOR_RULE_VERSION,
            "regulatory_source": FINANCIAL_REGULATORY_SOURCES.get(industry_code),
            "threshold_assumptions": dict(FINANCIAL_REGULATORY_THRESHOLDS.get(industry_code, {})),
            "metrics": sector_evidence_metrics,
        }
        for metric_name in applicable_sector_metrics:
            pair = _financial_metric_pair(m, metric_name)
            m[f"{metric_name}_change"] = pair[1] - pair[0] if pair is not None else None
            m[f"{metric_name}_growth"] = _financial_metric_growth(m, metric_name)

    share_observations = indicator_metric("total_shares").get("observations", [])
    m["share_dilution_1yr"] = None
    if isinstance(share_observations, list) and len(share_observations) >= 2:
        prior_observation, latest_observation = share_observations[-2:]
        if isinstance(prior_observation, Mapping) and isinstance(latest_observation, Mapping):
            prior_shares = _safe_float(prior_observation.get("value"))
            latest_shares = _safe_float(latest_observation.get("value"))
            prior_date = str(prior_observation.get("report_date") or "")
            latest_date = str(latest_observation.get("report_date") or "")
            if (
                prior_shares is not None
                and prior_shares > 0
                and latest_shares is not None
                and len(prior_date) >= 4
                and len(latest_date) >= 4
                and prior_date[:4].isdigit()
                and latest_date[:4].isdigit()
                and int(latest_date[:4]) - int(prior_date[:4]) == 1
            ):
                m["share_dilution_1yr"] = latest_shares / prior_shares - 1.0

    revenue_history = _sorted_records(fin_data.get("revenue_history", []))
    # Preserve the exact canonical source rows used by the strict-TTM
    # reconstruction.  These private fields never enter the public screening
    # payload; they let the final scoring boundary independently reject a DCF
    # copied from another company/period or altered after pipeline validation.
    m["_ttm_revenue_history"] = revenue_history
    revenue_points = [
        (year, value)
        for row in revenue_history
        if (year := _report_year(row)) is not None
        and (value := _safe_float(row.get("TOTAL_OPERATE_INCOME"))) is not None
        and value > 0
    ]
    revenue_values = [value for _, value in revenue_points]
    m["revenue_values"] = revenue_values
    m["revenue_years"] = [year for year, _ in revenue_points]
    m["revenue_latest"] = revenue_values[-1] if revenue_values else None
    m["cagr_3yr"] = _dated_cagr(_window_points(revenue_points, 3))
    m["cagr_5yr"] = _dated_cagr(_window_points(revenue_points, 5))
    latest_rates = _dated_growth_rates(revenue_points[-2:])
    m["growth_1yr"] = (
        latest_rates[-1]
        if latest_rates and len(revenue_points) >= 2 and revenue_points[-1][0] - revenue_points[-2][0] == 1
        else None
    )
    dated_rates = _dated_growth_rates(revenue_points)
    m["growth_rates"] = dated_rates
    dated_cagr = _dated_cagr(revenue_points)
    m["growth_consistency"] = (
        float(np.std(dated_rates)) / abs(dated_cagr)
        if len(dated_rates) >= 2 and dated_cagr is not None and abs(dated_cagr) >= 0.001
        else 2.0
        if len(dated_rates) >= 2 and dated_cagr is not None
        else None
    )
    m["growth_slope"] = (
        float(np.polyfit(np.arange(len(dated_rates)), dated_rates, 1)[0]) if len(dated_rates) >= 2 else None
    )
    m["trend_growth"] = _combine_long_recent_growth(m["cagr_3yr"], m["cagr_5yr"])

    income_history = _sorted_records(fin_data.get("income_history", []))
    latest_income = income_history[-1] if income_history else {}
    m["net_profit"] = _safe_float(latest_income.get("PARENT_NETPROFIT"))
    m["operate_profit"] = _safe_float(latest_income.get("OPERATE_PROFIT"))
    profit_points = [
        (year, value)
        for row in income_history
        if (year := _report_year(row)) is not None and (value := _safe_float(row.get("PARENT_NETPROFIT"))) is not None
    ]
    m["net_profit_history"] = [value for _, value in profit_points]
    m["net_profit_years"] = [year for year, _ in profit_points]
    m["net_profit_latest"] = m["net_profit_history"][-1] if m["net_profit_history"] else None

    margin_points: list[tuple[int, float]] = []
    for row in income_history:
        year = _report_year(row)
        revenue = _safe_float(row.get("TOTAL_OPERATE_INCOME"))
        profit = _safe_float(row.get("PARENT_NETPROFIT"))
        if year is not None and revenue is not None and revenue > 0 and profit is not None:
            margin_points.append((year, profit / revenue))
    margins = [value for _, value in margin_points]
    m["margin_history"] = margins
    m["margin_years"] = [year for year, _ in margin_points]
    m["margin_median_hist"] = float(median(margins)) if margins else None
    income_revenue = _safe_float(latest_income.get("TOTAL_OPERATE_INCOME"))
    m["net_margin"] = m["net_profit"] / income_revenue if income_revenue and m["net_profit"] is not None else None
    m["operate_margin"] = (
        m["operate_profit"] / income_revenue if income_revenue and m["operate_profit"] is not None else None
    )

    cashflows = _sorted_records(fin_data.get("cashflow", []))
    m["_ttm_cashflow_history"] = cashflows
    is_financial = industry_code in FINANCIAL_INDUSTRIES
    fcf_history: list[tuple[str, float]] = []
    ocf_history: list[tuple[str, float]] = []
    capex_history: list[tuple[str, float]] = []
    if not is_financial:
        for row in cashflows:
            ocf = _safe_float(row.get("NETCASH_OPERATE"))
            capex = _safe_float(row.get("CONSTRUCT_LONG_ASSET"))
            if ocf is not None:
                ocf_history.append((_date_key(row), ocf))
            if capex is not None:
                capex_history.append((_date_key(row), abs(capex)))
            if ocf is not None and capex is not None:
                fcf_history.append((_date_key(row), ocf - abs(capex)))
        latest_cashflow = cashflows[-1] if cashflows else {}
        m["oper_cf"] = _safe_float(latest_cashflow.get("NETCASH_OPERATE"))
        raw_capex = _safe_float(latest_cashflow.get("CONSTRUCT_LONG_ASSET"))
        m["capex"] = abs(raw_capex) if raw_capex is not None else None
        m["free_cash_flow"] = m["oper_cf"] - m["capex"] if m["oper_cf"] is not None and m["capex"] is not None else None
        m["fcf_history"] = [value for _, value in fcf_history]
        m["fcf_years"] = [
            int(report_date[:4])
            for report_date, _ in fcf_history
            if len(report_date) >= 4 and report_date[:4].isdigit()
        ]
        m["capex_history"] = [value for _, value in capex_history]
        m["capex_years"] = [
            int(report_date[:4])
            for report_date, _ in capex_history
            if len(report_date) >= 4 and report_date[:4].isdigit()
        ]
        m["ocf_history"] = [value for _, value in ocf_history]
        m["ocf_years"] = [
            int(report_date[:4])
            for report_date, _ in ocf_history
            if len(report_date) >= 4 and report_date[:4].isdigit()
        ]
        recent_ocf = ocf_history[-3:]
        recent_ocf_years = [
            int(report_date[:4]) for report_date, _ in recent_ocf if len(report_date) >= 4 and report_date[:4].isdigit()
        ]
        if (
            len(recent_ocf) == 3
            and len(recent_ocf_years) == 3
            and _years_are_consecutive(recent_ocf_years, 3)
            and recent_ocf[0][1] != 0
        ):
            m["ocf_3yr_change"] = (recent_ocf[-1][1] - recent_ocf[0][1]) / abs(recent_ocf[0][1])
        else:
            m["ocf_3yr_change"] = None
    else:
        # Deposits and policy liabilities are operating inputs for financial
        # firms.  OCF-Capex, net debt, industrial ROIC and cash conversion are
        # therefore deliberately not constructed.
        m.update(
            {
                "oper_cf": None,
                "capex": None,
                "free_cash_flow": None,
                "fcf_history": [],
                "fcf_years": [],
                "capex_history": [],
                "capex_years": [],
                "ocf_history": [],
                "ocf_years": [],
                "ocf_3yr_change": None,
            }
        )

    balances = _sorted_records(fin_data.get("balance", []))
    latest_balance = balances[-1] if balances else {}
    m["total_assets"] = _safe_float(latest_balance.get("TOTAL_ASSETS"))
    m["total_liabilities"] = _safe_float(latest_balance.get("TOTAL_LIABILITIES"))
    m["total_equity"] = _safe_float(latest_balance.get("TOTAL_EQUITY"))
    parent_equity = _attributable_equity(latest_balance)
    m["parent_equity"] = parent_equity
    m["monetary_funds"] = _safe_float(latest_balance.get("MONETARYFUNDS"))
    m["short_borrow"] = _safe_float(latest_balance.get("SHORT_LOAN"))
    gross_debt, _cash, debt_known = extract_debt_and_cash(balances)
    m["interest_debt"] = gross_debt if debt_known and not is_financial else None
    asset_points = [
        (year, value)
        for row in balances
        if (year := _report_year(row)) is not None
        and (value := _safe_float(row.get("TOTAL_ASSETS"))) is not None
        and value > 0
    ]
    m["total_assets_history"] = [value for _, value in asset_points]
    m["total_assets_years"] = [year for year, _ in asset_points]
    goodwill_points = [
        (year, value)
        for row in balances
        if (year := _report_year(row)) is not None
        and (value := _safe_float(row.get("GOODWILL"))) is not None
        and value >= 0
    ]
    m["goodwill_history"] = [value for _, value in goodwill_points]
    m["goodwill_years"] = [year for year, _ in goodwill_points]
    m["goodwill_latest"] = goodwill_points[-1][1] if goodwill_points else None

    # 资产/负债可得时以同口径反算为准；否则API字段按百分数处理。
    assets, liabilities = m["total_assets"], m["total_liabilities"]
    raw_debt_ratio = _safe_float(latest_balance.get("DEBT_ASSET_RATIO"))
    if assets is not None and assets > 0 and liabilities is not None:
        m["debt_ratio"] = liabilities / assets
    elif raw_debt_ratio is not None:
        m["debt_ratio"] = raw_debt_ratio / 100.0
    else:
        m["debt_ratio"] = None
    if is_financial:
        m["debt_ratio"] = None

    annual_equity = _annual_attributable_equity(balances)
    profit_by_year = {
        year: value
        for row in income_history
        if (year := _report_year(row)) is not None and (value := _safe_float(row.get("PARENT_NETPROFIT"))) is not None
    }
    roe_history: list[float] = []
    roe_history_years: list[int] = []
    for year in sorted(profit_by_year):
        if year in annual_equity and year - 1 in annual_equity:
            average_equity = (annual_equity[year - 1] + annual_equity[year]) / 2.0
            if average_equity > 0:
                roe_history.append(profit_by_year[year] / average_equity)
                roe_history_years.append(year)
    m["roe_history"] = roe_history
    m["roe_history_years"] = roe_history_years
    latest_profit_year = max(profit_by_year) if profit_by_year else None
    if (
        latest_profit_year is not None
        and latest_profit_year in annual_equity
        and latest_profit_year - 1 in annual_equity
    ):
        average_equity = (annual_equity[latest_profit_year - 1] + annual_equity[latest_profit_year]) / 2.0
        m["roe"] = profit_by_year[latest_profit_year] / average_equity
        m["roe_basis"] = "average_begin_end_attributable_equity"
    else:
        m["roe"] = None
        m["roe_basis"] = "missing_average_attributable_equity"
    equity_years = sorted(annual_equity)
    m["equity_years"] = equity_years
    m["equity_history"] = [annual_equity[year] for year in equity_years]
    m["equity_growth"] = (
        annual_equity[equity_years[-1]] / annual_equity[equity_years[-2]] - 1.0
        if len(equity_years) >= 2 and annual_equity[equity_years[-2]] > 0
        else None
    )
    m["ocf_np_ratio"] = (
        m["oper_cf"] / m["net_profit"]
        if not is_financial and m["oper_cf"] is not None and m["net_profit"] is not None and m["net_profit"] > 0
        else None
    )
    m["ocf_np_ratio_basis"] = "annual_cashflow/parent_net_profit" if m["ocf_np_ratio"] is not None else None
    if not is_financial and m["indicator_ocf_np_ratio"] is not None:
        m["ocf_np_ratio"] = m["indicator_ocf_np_ratio"]
        m["ocf_np_ratio_basis"] = "RPT_CASHFLOW/RPT_INCOME_same_annual_period"

    # Dynamic interim data contains the current period and exact prior-year
    # comparator.  Legacy Q1 absolute values remain weak evidence only.
    income_interim = _sorted_records(fin_data.get("income_interim", []))
    cashflow_interim = _sorted_records(fin_data.get("cashflow_interim", []))
    m["_ttm_income_interim"] = income_interim
    m["_ttm_cashflow_interim"] = cashflow_interim
    m["interim_revenue_yoy"], revenue_yoy_basis = _same_period_yoy(
        income_interim, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")
    )
    m["interim_profit_yoy"], profit_yoy_basis = _same_period_yoy(income_interim, ("PARENT_NETPROFIT",))
    m["interim_ocf_yoy"], ocf_yoy_basis = _same_period_yoy(cashflow_interim, ("NETCASH_OPERATE",))
    m["interim_revenue_yoy_basis"] = revenue_yoy_basis
    m["interim_profit_yoy_basis"] = profit_yoy_basis
    m["interim_ocf_yoy_basis"] = ocf_yoy_basis
    latest_interim_cashflow = cashflow_interim[-1] if cashflow_interim else {}
    m["interim_acquisition_cashflow"] = _safe_float(latest_interim_cashflow.get("OBTAIN_SUBSIDIARY_OTHER"))
    m["interim_yoy_basis"] = (
        "same_period_yoy"
        if revenue_yoy_basis == "same_period_yoy" and profit_yoy_basis == "same_period_yoy"
        else "missing_same_period_comparator"
    )
    current_interim_revenue, prior_interim_revenue, revenue_pair_basis = _same_period_pair(
        income_interim, ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")
    )
    current_interim_profit, prior_interim_profit, profit_pair_basis = _same_period_pair(
        income_interim, ("PARENT_NETPROFIT",)
    )
    current_interim_ocf, prior_interim_ocf, ocf_pair_basis = _same_period_pair(cashflow_interim, ("NETCASH_OPERATE",))
    m["interim_current_revenue"] = current_interim_revenue
    m["interim_current_profit"] = current_interim_profit
    m["interim_current_ocf"] = current_interim_ocf
    m["interim_prior_revenue"] = prior_interim_revenue
    m["interim_prior_profit"] = prior_interim_profit
    m["interim_prior_ocf"] = prior_interim_ocf
    m["interim_revenue_pair_basis"] = revenue_pair_basis
    m["interim_profit_pair_basis"] = profit_pair_basis
    m["interim_ocf_pair_basis"] = ocf_pair_basis
    m["interim_revenue_warning"] = bool(
        revenue_pair_basis == "same_period_yoy" and current_interim_revenue is not None and current_interim_revenue < 0
    )
    m["interim_profit_warning"] = bool(
        profit_pair_basis == "same_period_yoy"
        and prior_interim_profit is not None
        and prior_interim_profit >= 0
        and current_interim_profit is not None
        and current_interim_profit < 0
    )
    m["interim_ocf_warning"] = bool(
        not is_financial
        and ocf_pair_basis == "same_period_yoy"
        and prior_interim_ocf is not None
        and prior_interim_ocf >= 0
        and current_interim_ocf is not None
        and current_interim_ocf < 0
    )

    # Q1只有当期绝对值时，绝不伪造“同比”或用Q1*4对全年。
    q1_income = _sorted_records(fin_data.get("income_q1", []))
    q1_cashflow = _sorted_records(fin_data.get("cashflow_q1", []))
    m["q1_net_profit"] = _safe_float(q1_income[-1].get("PARENT_NETPROFIT")) if q1_income else None
    m["q1_ocf"] = _safe_float(q1_cashflow[-1].get("NETCASH_OPERATE")) if q1_cashflow else None
    m["q1_profit_basis"] = "精确同报告期同比" if profit_yoy_basis == "same_period_yoy" else "单季绝对值无同比基准"
    m["q1_profit_warning"] = bool(
        profit_yoy_basis != "same_period_yoy"
        and m["net_profit"] is not None
        and m["net_profit"] > 0
        and m["q1_net_profit"] is not None
        and m["q1_net_profit"] < 0
    )
    m["ocf_q1_warning"] = bool(
        not is_financial
        and ocf_yoy_basis != "same_period_yoy"
        and m["oper_cf"] is not None
        and m["oper_cf"] > 0
        and m["q1_ocf"] is not None
        and m["q1_ocf"] < 0
    )

    # This is a deliberately partial supplement to the two growth-quality
    # inputs that were previously absent for every company.  Annual goodwill
    # and the latest reported acquisition cash-flow line are objective and
    # traceable, but neither identifies acquired revenue share or product
    # segment concentration.  They improve the review evidence and must not
    # relax Type3's complete-evidence gate on their own.
    supplied_external_growth = fin_data.get("external_growth_evidence")
    if isinstance(supplied_external_growth, Mapping):
        m["external_growth_evidence"] = _normalise_structured_growth_evidence(
            supplied_external_growth,
            expected_code=m.get("code"),
            reference_date=m.get("source_trade_date"),
            missing_label="完整并购与商誉来源",
            content_keys=("records", "transactions", "acquisitions", "goodwill_records"),
        )
    elif goodwill_points or m["interim_acquisition_cashflow"] is not None:
        goodwill_sources = [
            {
                "report_date": _date_key(row),
                "source_dataset": "东方财富年度资产负债表",
                "source_field": "GOODWILL",
            }
            for row in balances
            if _report_year(row) is not None and _safe_float(row.get("GOODWILL")) is not None
        ]
        acquisition_source = (
            {
                "report_date": _date_key(latest_interim_cashflow),
                "source_dataset": "东方财富当期现金流量表",
                "source_field": "OBTAIN_SUBSIDIARY_OTHER",
            }
            if m["interim_acquisition_cashflow"] is not None
            else None
        )
        m["external_growth_evidence"] = {
            "status": "partial",
            "source": "年度资产负债表商誉与当期现金流并购项目",
            "goodwill_years": list(m["goodwill_years"]),
            "goodwill_values": list(m["goodwill_history"]),
            "latest_interim_acquisition_cashflow": m["interim_acquisition_cashflow"],
            "goodwill_source_records": goodwill_sources,
            "acquisition_cashflow_source": acquisition_source,
            "missing": ["逐笔并购收入占比", "完整并购交易清单"],
        }
    else:
        m["external_growth_evidence"] = {"status": "missing", "missing": ["商誉与并购现金流来源"]}
    m["segment_growth_sources"] = _normalise_structured_growth_evidence(
        fin_data.get("segment_growth_sources"),
        expected_code=m.get("code"),
        reference_date=m.get("source_trade_date"),
        missing_label="分产品或分地区收入增长来源",
        content_keys=("segments", "records"),
    )

    profits = m["net_profit_history"]
    m["profit_1yr_change"] = (
        (profit_points[-1][1] - profit_points[-2][1]) / abs(profit_points[-2][1])
        if len(profit_points) >= 2 and profit_points[-1][0] - profit_points[-2][0] == 1 and profit_points[-2][1] != 0
        else None
    )
    m["rev_1yr_change"] = m["growth_1yr"]
    if len(profits) >= 3 and abs(float(np.mean(profits))) > 0:
        m["profit_volatility"] = float(np.std(profits)) / abs(float(np.mean(profits)))
    else:
        m["profit_volatility"] = None
    if _aligned_consecutive(margins, m.get("margin_years"), 3):
        recent_margins = margins[-3:]
        old = float(np.mean(recent_margins[:2]))
        recent = float(np.mean(recent_margins[-2:]))
        m["margin_trajectory"] = (recent - old) / abs(old) if abs(old) > 0.001 else None
    else:
        m["margin_trajectory"] = None

    recent_profit_points = profit_points[-3:]
    profit_comparable = (
        len(recent_profit_points) == 3
        and all(value > 0 for _, value in recent_profit_points)
        and all(
            current_year - prior_year == 1
            for (prior_year, _), (current_year, _) in zip(recent_profit_points, recent_profit_points[1:])
        )
        and recent_profit_points[-1][1] > recent_profit_points[-2][1]
    )
    profit_recent_cagr = _dated_cagr(recent_profit_points) if profit_comparable else None
    positive_profit_points = (
        profit_points[-5:] if len(profit_points) >= 3 and all(value > 0 for _, value in profit_points[-5:]) else []
    )
    profit_long_cagr = _dated_cagr(positive_profit_points)
    m["profit_trend_growth"] = (
        _combine_long_recent_growth(profit_recent_cagr, profit_long_cagr) if profit_comparable else None
    )
    m["peg"] = (
        m["pe"] / (m["profit_trend_growth"] * 100)
        if m["pe"] is not None and m["pe"] > 0 and m["profit_trend_growth"] is not None and m["profit_trend_growth"] > 0
        else None
    )
    m["peg_basis"] = (
        "recent_comparable_parent_profit_trend" if m["peg"] is not None else "insufficient_comparable_profit_trend"
    )

    m["roic"], m["wacc"], m["roic_wacc_basis"] = None, None, None
    m["wacc_tax_shield_source"] = None
    if not is_financial and latest_profit_year is not None:
        invested_capital = _annual_invested_capital(balances)
        current_ic = invested_capital.get(latest_profit_year)
        prior_ic = invested_capital.get(latest_profit_year - 1)
        if (
            m["operate_profit"] is not None
            and current_ic is not None
            and prior_ic is not None
            and current_ic + prior_ic > 0
        ):
            average_ic = (prior_ic + current_ic) / 2.0
            m["roic"] = m["operate_profit"] * (1.0 - MARGINAL_TAX_RATE) / average_ic
        market_equity = m["market_cap"]
        beta_u = _safe_float(INDUSTRY_UNLEVERED_BETA.get(industry_code, DEFAULT_UNLEVERED_BETA))
        debt_cost = _safe_float(INDUSTRY_PRETAX_COST_OF_DEBT.get(industry_code, DEFAULT_PRETAX_COST_OF_DEBT))
        # TAXRATE is now a dated annual main-financial field.  Use it only in a
        # conservative, economically valid range; otherwise the debt tax
        # shield continues to fail closed at zero.
        indicator_tax_rate = _safe_float(m.get("indicator_tax_rate"))
        if indicator_tax_rate is not None and 0 <= indicator_tax_rate <= 0.50:
            tax_shield_rate = indicator_tax_rate
            m["wacc_tax_shield_source"] = "Eastmoney年度TAXRATE"
        else:
            tax_shield_rate = 0.0
            m["wacc_tax_shield_source"] = "缺有效年度税率证据按0"
        if market_equity is not None and market_equity > 0 and beta_u is not None:
            m["wacc"] = compute_wacc(
                debt=gross_debt if debt_known else None,
                equity=market_equity,
                industry_unlevered_beta=beta_u,
                pre_tax_cost_of_debt=debt_cost,
                tax_rate=tax_shield_rate,
            )
        if m["roic"] is not None and m["wacc"] is not None:
            m["roic_wacc_basis"] = "NOPAT/平均投入资本代理"
        indicator_roic = _safe_float(m.get("indicator_roic"))
        if indicator_roic is not None and m["wacc"] is not None:
            m["calculated_roic_proxy"] = m["roic"]
            m["roic"] = indicator_roic
            m["roic_wacc_basis"] = "Eastmoney年度ROIC/公司资本结构WACC"

    # Qualitative dimensions require a dated, traceable evidence record.  A
    # naked 0-10 number is not evidence and must fail closed.
    for key in QUALITATIVE_SCORE_KEYS:
        evidence_score, evidence = _normalise_score_evidence(
            fin_data,
            key,
            expected_code=m.get("code"),
            reference_date=m.get("source_trade_date"),
        )
        m[key] = evidence_score
        m[f"{key}_evidence"] = evidence
        raw_level = fin_data.get(f"{key}_evidence_level")
        m[f"{key}_evidence_level"] = raw_level if raw_level in QUANTITATIVE_EVIDENCE_LEVELS else None
    if fin_data.get("_type5_external_validation_token") is _TYPE5_EXTERNAL_VALIDATION_TOKEN:
        # Preserve only the server-created marker.  Serialized snapshots
        # cannot create this identity, and it is never part of result output.
        m["_type5_external_validation_token"] = _TYPE5_EXTERNAL_VALIDATION_TOKEN
    m["type7_research_sources"] = normalise_research_sources(
        fin_data.get("type7_research_sources"),
        today=_evidence_reference_date(m.get("source_trade_date")) or _shanghai_today(),
        security_code=str(m.get("code") or ""),
    )
    # Patch 4 is an exact raw-fact contract, not another naked qualitative
    # score.  Preserve it for Type 7, whose validator binds every criterion to
    # the current company/date and independently replays the formula.
    m["type7_patch4_assessment"] = fin_data.get("type7_patch4_assessment")
    for key in ("position_size_pct", "type6_portfolio_pct"):
        value = _safe_float(fin_data.get(key))
        m[key] = value if value is not None and 0 < value <= 100 else None
    return m


class _SectorBenchmarks(dict[str, dict[str, Any]]):
    """Market benchmarks plus O(1) per-company leave-one-out views."""

    def __init__(self) -> None:
        super().__init__()
        self._leave_one_out: dict[str, dict[str, dict[str, Any]]] = {}

    def set_leave_one_out(self, values: Mapping[str, dict[str, dict[str, Any]]]) -> None:
        self._leave_one_out = dict(values)

    def for_code(self, code: Any) -> Mapping[str, Mapping[str, Any]]:
        return self._leave_one_out.get(str(code or "").strip(), self)


def _benchmarks_for_code(
    benchmarks: Mapping[str, Mapping[str, Any]],
    code: Any,
) -> Mapping[str, Mapping[str, Any]]:
    selector = getattr(benchmarks, "for_code", None)
    if callable(selector):
        selected = selector(code)
        if isinstance(selected, Mapping):
            return selected
    return benchmarks


def build_sector_benchmarks(metrics_list: list[dict]) -> dict[str, dict]:
    if not metrics_list:
        return {}
    frame = pd.DataFrame(metrics_list).reset_index(drop=True)
    columns = {
        "pe": "median_pe",
        "pb": "median_pb",
        "roe": "median_roe",
        "cagr_3yr": "median_cagr",
        "net_margin": "median_margin",
        "debt_ratio": "median_debt",
        "profit_1yr_change": "median_profit_change",
        "net_interest_margin_change": "median_nim_change",
        "nonperforming_loan_ratio_change": "median_npl_change",
        "capital_adequacy_ratio_change": "median_bank_capital_change",
        "new_business_value_growth": "median_nbv_growth",
        "new_business_value_margin_change": "median_nbv_margin_change",
        "solvency_adequacy_ratio_change": "median_solvency_change",
        "life_surrender_rate_change": "median_surrender_change",
        "risk_coverage_ratio_change": "median_risk_coverage_change",
        "capital_leverage_ratio_change": "median_capital_leverage_change",
        "liquidity_coverage_ratio_change": "median_liquidity_coverage_change",
        "net_stable_funding_ratio_change": "median_stable_funding_change",
    }

    def median_without(sorted_values: list[float], removed_position: int | None) -> tuple[float | None, int]:
        count = len(sorted_values) - (1 if removed_position is not None else 0)
        if count <= 0:
            return None, 0

        def remaining_value(position: int) -> float:
            source_position = (
                position + 1 if removed_position is not None and position >= removed_position else position
            )
            return sorted_values[source_position]

        middle = count // 2
        if count % 2:
            return remaining_value(middle), count
        return (remaining_value(middle - 1) + remaining_value(middle)) / 2.0, count

    benchmark_keys = ("pessimistic_floor", "neutral_benchmark", "optimistic_ceiling", "fcf_margin_target")
    leave_one_out_by_index: dict[int, dict[str, dict[str, Any]]] = {}
    benchmarks = _SectorBenchmarks()

    def add_bucket(label: str, bucket: pd.DataFrame) -> None:
        result: dict[str, Any] = {}
        per_index = {int(index): {} for index in bucket.index}
        for column, name in columns.items():
            if column not in bucket:
                continue
            pairs: list[tuple[float, int]] = []
            for index, raw_value in bucket[column].items():
                value = _safe_float(raw_value)
                if value is None or (column in {"pe", "pb"} and value <= 0):
                    continue
                pairs.append((value, int(index)))
            pairs.sort(key=lambda item: (item[0], item[1]))
            sorted_values = [value for value, _ in pairs]
            positions = {index: position for position, (_value, index) in enumerate(pairs)}
            full_median, full_count = median_without(sorted_values, None)
            if full_median is not None:
                result[name] = full_median
                result[f"{name}_count"] = full_count
            for index in per_index:
                loo_median, loo_count = median_without(sorted_values, positions.get(index))
                if loo_median is not None:
                    per_index[index][name] = loo_median
                    per_index[index][f"{name}_count"] = loo_count

        static_source = get_industry_benchmark("DEFAULT" if label == "ALL" else label)
        for key in benchmark_keys:
            value = _safe_float(static_source.get(key))
            if value is not None:
                result[key] = value
                for bucket_view in per_index.values():
                    bucket_view[key] = value
        benchmarks[label] = result
        for index, bucket_view in per_index.items():
            leave_one_out_by_index.setdefault(index, {})[label] = bucket_view

    if "industry" in frame:
        for industry, bucket in frame.groupby("industry", dropna=False):
            add_bucket(str(industry), bucket)
    add_bucket("ALL", frame)
    leave_one_out_by_code: dict[str, dict[str, dict[str, Any]]] = {}
    if "code" in frame:
        for index, raw_code in frame["code"].items():
            code = str(raw_code or "").strip()
            if code:
                leave_one_out_by_code[code] = leave_one_out_by_index.get(int(index), {})
    benchmarks.set_leave_one_out(leave_one_out_by_code)
    return benchmarks


def _normalise_recent_fcf(values: Any) -> tuple[Optional[float], Optional[float], str]:
    """Recompute the decline-aware FY-1/FY/TTM FCFF normalisation."""
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        return None, None, "missing_three_year_fcff"
    recent = [_safe_float(value) for value in values]
    if any(value is None for value in recent):
        return None, None, "invalid_three_year_fcff"
    first, middle, latest = recent  # type: ignore[misc]
    if latest <= 0:
        return latest, latest, "latest_nonpositive_fail_closed"
    if first >= middle >= latest:
        return latest, latest, "latest_persistent_decline"
    median_fcf = float(median(recent))
    premium_limit = latest * MAX_NORMALISED_FCFF_PREMIUM
    normalised = min(median_fcf, premium_limit)
    basis = "recent_median" if median_fcf <= premium_limit else "latest_premium_cap"
    return normalised, latest, basis


def _result_reporting_period_contract(result: Mapping[str, Any]) -> ReportingPeriodContract | None:
    """Read an exact period contract from the result's FCFF evidence."""
    evidence = result.get("ttm_fcff_evidence")
    period = evidence.get("period") if isinstance(evidence, Mapping) else None
    if not isinstance(period, Mapping):
        return None
    values = [
        period.get("annual_report_date"),
        period.get("current_interim_report_date"),
        period.get("prior_interim_report_date"),
    ]
    if not all(isinstance(value, str) for value in values):
        return None
    return ReportingPeriodContract(
        annual_report_date=values[0],
        current_interim_report_date=values[1],
        prior_interim_report_date=values[2],
    )


def _numbers_equal(left: Any, right: Any) -> bool:
    left_number = _safe_float(left)
    right_number = _safe_float(right)
    return (
        left_number is not None
        and right_number is not None
        and math.isclose(
            left_number,
            right_number,
            rel_tol=1e-9,
            abs_tol=max(1e-8, abs(right_number) * 1e-9),
        )
    )


def _valid_base_fcf_adjustment_chain(
    adjustments: Any,
    *,
    normalised_fcf: float,
    reported_base_fcf: float,
) -> bool:
    """Require every conservative post-normalisation cap to be explicit."""
    if not isinstance(adjustments, list):
        return False
    current = normalised_fcf
    allowed_kinds = {"mixed_profit_cycle_p25_cap", "fcf_margin_ceiling"}
    for adjustment in adjustments:
        if not isinstance(adjustment, Mapping) or set(adjustment) != {"kind", "before", "limit", "after"}:
            return False
        before = _safe_float(adjustment.get("before"))
        limit = _safe_float(adjustment.get("limit"))
        after = _safe_float(adjustment.get("after"))
        if (
            adjustment.get("kind") not in allowed_kinds
            or before is None
            or limit is None
            or after is None
            or limit <= 0
            or not _numbers_equal(before, current)
            or not _numbers_equal(after, min(before, limit))
            or after > before
        ):
            return False
        current = after
    return _numbers_equal(current, reported_base_fcf)


def _valid_fcf_normalisation_evidence(m: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """Bind strict TTM revenue/FCFF and FY-1/FY/TTM normalisation to raw rows."""
    if (
        result.get("valuation_input_basis") != "strict_ttm"
        or result.get("base_revenue_basis") != "strict_ttm_reported_revenue"
        or result.get("base_fcf_basis") != "normalised_two_annual_plus_ttm_cfo_less_capex_proxy"
        or result.get("fcf_normalisation_period_basis") != "two_annual_plus_strict_ttm"
    ):
        return False
    annual_revenue = m.get("_ttm_revenue_history")
    annual_cashflow = m.get("_ttm_cashflow_history")
    interim_income = m.get("_ttm_income_interim")
    interim_cashflow = m.get("_ttm_cashflow_interim")
    if not all(isinstance(rows, list) for rows in (annual_revenue, annual_cashflow, interim_income, interim_cashflow)):
        return False
    period_contract = _result_reporting_period_contract(result)
    if period_contract is None:
        return False

    expected_fcff = reconstruct_ttm_fcff(
        annual_cashflow,
        interim_cashflow,
        period_contract=period_contract,
        require_capex_provenance=True,
    )
    expected_revenue = reconstruct_ttm_revenue(
        annual_revenue,
        interim_income,
        period_contract=period_contract,
    )
    if (
        expected_fcff.get("status") != "complete"
        or expected_revenue.get("status") != "complete"
        or result.get("ttm_fcff_evidence") != expected_fcff
        or result.get("ttm_revenue_evidence") != expected_revenue
    ):
        return False
    ttm_fcff = _safe_float(expected_fcff.get("value"))
    ttm_revenue = _safe_float(expected_revenue.get("value"))
    if ttm_fcff is None or ttm_fcff <= 0 or ttm_revenue is None or ttm_revenue <= 0:
        return False

    # Reuse the scenario layer's exact period-selection policy so this final
    # gate cannot drift to three annual years or a different FY/TTM sequence.
    from engine.scenarios import _ttm_fcf_normalisation

    normalisation_result = _ttm_fcf_normalisation(annual_cashflow, expected_fcff, period_contract)
    if normalisation_result is None:
        return False
    normalisation, expected_periods = normalisation_result
    expected_recent = list(normalisation.get("recent_fcff", ()))
    actual_recent = result.get("recent_fcff")
    normalised_fcf = _safe_float(normalisation.get("normalised_fcf"))
    base_fcf = _safe_float(result.get("base_fcf"))
    premium_cap = _safe_float(result.get("normalisation_premium_cap"))
    expected_period_detail = {
        "period_set": "two_annual_plus_strict_ttm",
        "periods": expected_periods,
        "normalisation_method": normalisation.get("basis"),
        "cash_flow_kind": expected_fcff.get("cash_flow_kind"),
        "formula_version": expected_fcff.get("formula_version"),
    }
    if (
        len(expected_recent) != 3
        or not isinstance(actual_recent, (list, tuple))
        or len(actual_recent) != 3
        or any(not _numbers_equal(actual, expected) for actual, expected in zip(actual_recent, expected_recent))
        or normalised_fcf is None
        or normalised_fcf <= 0
        or base_fcf is None
        or base_fcf <= 0
        or not _numbers_equal(result.get("latest_fcff"), ttm_fcff)
        or not _numbers_equal(result.get("base_revenue"), ttm_revenue)
        or result.get("recent_fcff_periods") != expected_periods
        or result.get("fcf_normalisation_basis") != normalisation.get("basis")
        or result.get("fcf_normalisation_period") != expected_period_detail
        or premium_cap is None
        or not math.isclose(premium_cap, MAX_NORMALISED_FCFF_PREMIUM, rel_tol=0.0, abs_tol=1e-12)
        or not _valid_base_fcf_adjustment_chain(
            result.get("base_fcf_adjustments"),
            normalised_fcf=normalised_fcf,
            reported_base_fcf=base_fcf,
        )
    ):
        return False
    return True


def _conservative_metric_fcf(m: Mapping[str, Any]) -> tuple[Optional[float], Optional[float], str]:
    """Normalise metric FCFF without trusting a single latest-year value."""
    history = m.get("fcf_history")
    years = m.get("fcf_years")
    if not _aligned_consecutive(history, years, 3):
        return None, _safe_float(m.get("free_cash_flow")), "缺连续三年FCF"
    normalised, latest, _basis = _normalise_recent_fcf(history[-3:])
    raw_latest = _safe_float(m.get("free_cash_flow"))
    if (
        normalised is None
        or latest is None
        or raw_latest is None
        or not math.isclose(raw_latest, latest, rel_tol=1e-9, abs_tol=1e-8)
    ):
        return None, raw_latest, "FCF口径不一致"
    return normalised, latest, "三年归一FCF"


def _traceable_normalised_fcf(
    m: Mapping[str, Any],
    result: Optional[Mapping[str, Any]],
    *,
    result_valid: bool = False,
    allow_history_fallback: bool = False,
) -> tuple[Optional[float], Optional[float], str]:
    if result_valid and isinstance(result, Mapping):
        return _safe_float(result.get("base_fcf")), _safe_float(result.get("latest_fcff")), "TTM归一FCF"
    if allow_history_fallback:
        return _conservative_metric_fcf(m)
    return None, _safe_float(m.get("free_cash_flow")), "缺可追溯归一FCF"


def _same_period_metric_yoy(m: Mapping[str, Any], metric: str) -> Optional[float]:
    basis = m.get(f"interim_{metric}_yoy_basis")
    if basis is None:
        basis = m.get("interim_yoy_basis")
    if basis != "same_period_yoy":
        return None
    return _safe_float(m.get(f"interim_{metric}_yoy"))


def _latest_period_deterioration(
    m: Mapping[str, Any],
    *,
    include_ocf: bool = True,
    allow_improving_losses: bool = False,
) -> tuple[int, str]:
    """Return 0/1/2/3 for none/mild/material/severe current deterioration."""
    dimensions = [
        ("profit", "利润", "interim_profit_warning", "interim_current_profit"),
        ("revenue", "营收", "interim_revenue_warning", "interim_current_revenue"),
    ]
    if include_ocf:
        dimensions.insert(1, ("ocf", "经营现金流", "interim_ocf_warning", "interim_current_ocf"))
    candidates: list[tuple[int, int, str]] = []
    for priority, (metric, label, warning_key, current_key) in enumerate(dimensions):
        yoy = _same_period_metric_yoy(m, metric)
        current = _safe_float(m.get(current_key))
        if bool(m.get(warning_key)):
            candidates.append((3, -priority, f"最新同口径{label}转负"))
        elif current is not None and current < 0 and allow_improving_losses and metric in {"profit", "ocf"}:
            if yoy is None:
                candidates.append((3, -priority, f"最新报告期{label}为负且缺同口径比较"))
            elif yoy <= -0.50:
                candidates.append((3, -priority, f"最新同口径{label}降幅≥50%"))
            elif yoy <= -0.20:
                candidates.append((2, -priority, f"最新同口径{label}降幅≥20%"))
            elif yoy < 0:
                candidates.append((1, -priority, f"最新同口径{label}同比下降"))
            elif metric == "ocf":
                message = "最新经营现金流仍负但改善" if yoy > 0 else "最新经营现金流仍负持平"
                candidates.append((1, -priority, message))
        elif current is not None and current < 0:
            candidates.append((3, -priority, f"最新报告期{label}为负"))
        elif yoy is not None and yoy <= -0.50:
            candidates.append((3, -priority, f"最新同口径{label}降幅≥50%"))
        elif yoy is not None and yoy <= -0.20:
            candidates.append((2, -priority, f"最新同口径{label}降幅≥20%"))
        elif yoy is not None and yoy < 0:
            candidates.append((1, -priority, f"最新同口径{label}同比下降"))
    if bool(m.get("q1_profit_warning")):
        candidates.append((3, 0, "最新报告期利润转负"))
    if include_ocf and bool(m.get("ocf_q1_warning")):
        candidates.append((3, -1, "最新报告期经营现金流转负"))
    if not candidates:
        return 0, ""
    severity, _priority, reason = max(candidates)
    return severity, reason


def _valid_nonfinancial_dcf_evidence(m: Mapping[str, Any], result: Any) -> bool:
    """Accept only a complete, internally consistent and attributable DCF."""
    cached = _cached_dcf_validation(m, result, "nonfinancial")
    if cached is not None:
        return cached
    if not isinstance(result, Mapping):
        return False

    metric_code = _normalize_code(m.get("code"))
    result_code = _normalize_code(result.get("code"))
    metric_industry = str(m.get("industry") or "").strip()
    result_industry = str(result.get("industry_code") or "").strip()
    metric_price = _safe_float(m.get("price"))
    result_price = _safe_float(result.get("current_price"))
    if (
        not metric_code
        or result_code != metric_code
        or not metric_industry
        or result_industry != metric_industry
        or metric_price is None
        or result_price is None
        or metric_price <= 0
        or result_price <= 0
        or not math.isclose(metric_price, result_price, rel_tol=1e-6, abs_tol=1e-8)
    ):
        return False

    positive_inputs = {
        key: _safe_float(result.get(key))
        for key in ("buy_zone_upper", "sell_zone_lower", "base_wacc", "base_fcf", "base_revenue")
    }
    if any(value is None or value <= 0 for value in positive_inputs.values()):
        return False
    if _safe_float(result.get("net_debt")) is None or not _valid_fcf_normalisation_evidence(m, result):
        return False
    levered_beta = _safe_float(result.get("levered_beta"))
    unlevered_beta = _safe_float(result.get("industry_unlevered_beta"))
    debt_cost = _safe_float(result.get("pre_tax_cost_of_debt"))
    tax_shield = _safe_float(result.get("tax_shield_rate"))
    if (
        levered_beta is None
        or levered_beta < 0
        or unlevered_beta is None
        or unlevered_beta < 0
        or debt_cost is None
        or debt_cost < 0
        or tax_shield is None
        or not 0 <= tax_shield < 1
    ):
        return False

    for key in (
        "beta_source",
        "wacc_capital_structure",
        "tax_shield_source",
        "growth_evidence",
        "model_risk_data_as_of",
    ):
        if not str(result.get(key) or "").strip():
            return False
    try:
        model_data_date = date.fromisoformat(str(result["model_risk_data_as_of"]))
    except (TypeError, ValueError):
        return False
    if model_data_date > _shanghai_today():
        return False

    components = result.get("wacc_components")
    if not isinstance(components, Mapping):
        return False
    equity_weight = _safe_float(components.get("equity_weight"))
    debt_weight = _safe_float(components.get("debt_weight"))
    cost_of_equity = _safe_float(components.get("cost_of_equity"))
    component_debt_cost = _safe_float(components.get("pre_tax_cost_of_debt"))
    component_tax_shield = _safe_float(components.get("tax_shield_rate"))
    if (
        equity_weight is None
        or debt_weight is None
        or cost_of_equity is None
        or component_debt_cost is None
        or component_tax_shield is None
        or equity_weight < 0
        or debt_weight < 0
        or cost_of_equity <= 0
        or component_debt_cost < 0
        or not 0 <= component_tax_shield < 1
        or not math.isclose(equity_weight + debt_weight, 1.0, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(component_debt_cost, debt_cost, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(component_tax_shield, tax_shield, rel_tol=1e-9, abs_tol=1e-9)
    ):
        return False
    reconstructed_wacc = equity_weight * cost_of_equity + debt_weight * component_debt_cost * (
        1.0 - component_tax_shield
    )
    if not math.isclose(positive_inputs["base_wacc"], reconstructed_wacc, rel_tol=1e-4, abs_tol=5.1e-5):
        return False

    points = result.get("dcf_points")
    params = result.get("params")
    if not isinstance(points, Mapping) or not isinstance(params, Mapping):
        return False
    ordered_values: list[float] = []
    for scenario in ("pessimistic", "neutral", "optimistic"):
        band = points.get(scenario)
        scenario_params = params.get(scenario)
        if not isinstance(band, Mapping) or not isinstance(scenario_params, Mapping):
            return False
        lower = _safe_float(band.get("lower"))
        upper = _safe_float(band.get("upper"))
        growth = _safe_float(scenario_params.get("growth"))
        wacc = _safe_float(scenario_params.get("wacc_base"))
        terminal_growth = _safe_float(scenario_params.get("terminal_g"))
        retention = _safe_float(scenario_params.get("margin_retention"))
        if (
            lower is None
            or upper is None
            or lower <= 0
            or upper < lower
            or growth is None
            or growth <= -1
            or wacc is None
            or wacc <= 0
            or terminal_growth is None
            or terminal_growth <= -1
            or wacc <= terminal_growth
            or retention is None
            or not 0 <= retention <= 1
        ):
            return False
        ordered_values.extend((lower, upper))
    if ordered_values != sorted(ordered_values):
        return False

    buy_upper = positive_inputs["buy_zone_upper"]
    sell_lower = positive_inputs["sell_zone_lower"]
    expected_buy = (ordered_values[1] + ordered_values[2]) / 2.0
    expected_sell = (ordered_values[3] + ordered_values[4]) / 2.0
    if (
        buy_upper > sell_lower
        or not math.isclose(buy_upper, expected_buy, rel_tol=1e-9, abs_tol=1e-8)
        or not math.isclose(sell_lower, expected_sell, rel_tol=1e-9, abs_tol=1e-8)
    ):
        return False
    expected_zone = "买入区" if result_price <= buy_upper else "卖出区" if result_price >= sell_lower else "观察区"
    return str(result.get("zone") or "") == expected_zone


def _valid_long_horizon_dcf_evidence(m: Mapping[str, Any], result: Any) -> bool:
    """Validate Patch6 Type4's separately labelled ten-year DCF surface."""
    cached = _cached_dcf_validation(m, result, "long_horizon")
    if cached is not None:
        return cached
    if not _valid_nonfinancial_dcf_evidence(m, result) or not isinstance(result, Mapping):
        return False
    if (
        result.get("explicit_forecast_years") != FORECAST_YEARS
        or result.get("long_horizon_forecast_years") != LONG_HORIZON_FORECAST_YEARS
        or result.get("long_horizon_growth_path") != "linear_fade_from_scenario_growth_to_terminal_growth"
        or result.get("long_horizon_formula_version") != 1
    ):
        return False

    points = result.get("dcf_10y_points")
    params = result.get("params")
    base_fcf = _safe_float(result.get("base_fcf"))
    base_revenue = _safe_float(result.get("base_revenue"))
    shares = _safe_float(result.get("shares_outstanding"))
    net_debt = _safe_float(result.get("net_debt"))
    if (
        not isinstance(points, Mapping)
        or not isinstance(params, Mapping)
        or base_fcf is None
        or base_fcf <= 0
        or base_revenue is None
        or base_revenue <= 0
        or shares is None
        or shares <= 0
        or net_debt is None
    ):
        return False

    ordered_values: list[float] = []
    for scenario in ("pessimistic", "neutral", "optimistic"):
        band = points.get(scenario)
        scenario_params = params.get(scenario)
        if not isinstance(band, Mapping) or not isinstance(scenario_params, Mapping):
            return False
        lower = _safe_float(band.get("lower"))
        upper = _safe_float(band.get("upper"))
        growth = _safe_float(scenario_params.get("growth"))
        wacc = _safe_float(scenario_params.get("wacc_base"))
        terminal_growth = _safe_float(scenario_params.get("terminal_g"))
        retention = _safe_float(scenario_params.get("margin_retention"))
        if (
            lower is None
            or upper is None
            or lower <= 0
            or upper < lower
            or growth is None
            or wacc is None
            or terminal_growth is None
            or retention is None
            or scenario_params.get("forecast_years") != FORECAST_YEARS
        ):
            return False
        expected_upper = dcf_valuation_fading_growth(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=growth,
            wacc=wacc - BAND_WACC_DELTA,
            terminal_g=terminal_growth,
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=retention,
            forecast_years=LONG_HORIZON_FORECAST_YEARS,
        )
        expected_lower = dcf_valuation_fading_growth(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=growth,
            wacc=wacc + BAND_WACC_DELTA,
            terminal_g=terminal_growth,
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=retention,
            forecast_years=LONG_HORIZON_FORECAST_YEARS,
        )
        if (
            expected_lower is None
            or expected_upper is None
            or not math.isclose(lower, expected_lower, rel_tol=1e-9, abs_tol=1e-8)
            or not math.isclose(upper, expected_upper, rel_tol=1e-9, abs_tol=1e-8)
        ):
            return False
        ordered_values.extend((lower, upper))
    return ordered_values == sorted(ordered_values)


def _implied_price_growth_years(result: Mapping[str, Any], price: Any, *, max_years: int = 30) -> int | None:
    """Return the minimum high-growth runway needed to justify ``price``.

    Zero means a terminal-growth valuation already covers the price.  A value
    of ``max_years + 1`` means even thirty explicit growth years do not.  The
    helper is deliberately based on the audited optimistic scenario inputs:
    4d already owns the neutral valuation comparison, while 4f asks whether
    even an upside case needs an implausibly long high-growth runway.
    """
    current_price = _safe_float(price)
    base_fcf = _safe_float(result.get("base_fcf"))
    base_revenue = _safe_float(result.get("base_revenue"))
    shares = _safe_float(result.get("shares_outstanding"))
    net_debt = _safe_float(result.get("net_debt"))
    params = result.get("params")
    optimistic = params.get("optimistic") if isinstance(params, Mapping) else None
    if not isinstance(optimistic, Mapping):
        return None
    growth = _safe_float(optimistic.get("growth"))
    wacc_base = _safe_float(optimistic.get("wacc_base"))
    wacc = wacc_base - BAND_WACC_DELTA if wacc_base is not None else None
    terminal_growth = _safe_float(optimistic.get("terminal_g"))
    retention = _safe_float(optimistic.get("margin_retention"))
    if (
        current_price is None
        or current_price <= 0
        or base_fcf is None
        or base_fcf <= 0
        or base_revenue is None
        or base_revenue <= 0
        or shares is None
        or shares <= 0
        or net_debt is None
        or growth is None
        or wacc is None
        or terminal_growth is None
        or retention is None
        or max_years < 1
    ):
        return None
    conservative_growth = max(terminal_growth, min(growth, 0.25))

    def value_for(runway_years: int) -> float | None:
        explicit_years = max(1, runway_years)
        explicit_growth = terminal_growth if runway_years == 0 else conservative_growth
        return dcf_valuation(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=explicit_growth,
            wacc=wacc,
            terminal_g=terminal_growth,
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=retention,
            forecast_years=explicit_years,
        )

    zero_value = value_for(0)
    if zero_value is not None and zero_value >= current_price:
        return 0
    maximum_value = value_for(max_years)
    if maximum_value is None or maximum_value < current_price:
        return max_years + 1

    # With positive base FCF, growth constrained to at least terminal growth,
    # and WACC above terminal growth, extending the high-growth runway is
    # monotone non-decreasing.  Binary search preserves the exact minimum
    # integer result while replacing up to 31 full DCF evaluations with at
    # most seven.
    lower = 1
    upper = max_years
    while lower < upper:
        runway_years = (lower + upper) // 2
        value = dcf_valuation(
            base_fcf=base_fcf,
            base_revenue=base_revenue,
            revenue_growth=conservative_growth,
            wacc=wacc,
            terminal_g=terminal_growth,
            shares_outstanding=shares,
            net_debt=net_debt,
            margin_retention=retention,
            forecast_years=runway_years,
        )
        if value is not None and value >= current_price:
            upper = runway_years
        else:
            lower = runway_years + 1
    return lower


def _score_implied_growth_years(years: int | None) -> float:
    if years is None:
        return 0.0
    if years < 3:
        return 10.0
    if years < 5:
        return 8.0
    if years < 7:
        return 6.0
    if years < 10:
        return 4.0
    if years < 15:
        return 2.0
    return 0.0


def _valid_financial_pb_evidence(m: Mapping[str, Any], result: Any) -> bool:
    """Accept only a complete, attributable and formula-consistent justified-P/B result."""
    cached = _cached_dcf_validation(m, result, "financial_pb")
    if cached is not None:
        return cached
    if not isinstance(result, Mapping) or result.get("_pb_valuation") is not True:
        return False
    metric_code = _normalize_code(m.get("code"))
    result_code = _normalize_code(result.get("code"))
    metric_industry = str(m.get("industry") or "").strip()
    result_industry = str(result.get("industry_code") or "").strip()
    metric_price = _safe_float(m.get("price"))
    result_price = _safe_float(result.get("current_price"))
    if (
        metric_industry not in SUPPORTED_FINANCIAL_INDUSTRIES
        or result_industry != metric_industry
        or not metric_code
        or result_code != metric_code
        or metric_price is None
        or result_price is None
        or metric_price <= 0
        or result_price <= 0
        or not math.isclose(metric_price, result_price, rel_tol=1e-6, abs_tol=1e-8)
    ):
        return False

    for key in ("base_fcf", "base_revenue", "net_debt"):
        if key not in result or result.get(key) is not None:
            return False
    base_cost = _safe_float(result.get("base_wacc"))
    normalised_roe = _safe_float(result.get("normalised_roe"))
    financial_beta = _safe_float(result.get("financial_levered_beta"))
    levered_beta = _safe_float(result.get("levered_beta"))
    if (
        base_cost is None
        or base_cost <= 0
        or normalised_roe is None
        or normalised_roe <= 0
        or financial_beta is None
        or financial_beta < 0
        or levered_beta is None
        or not math.isclose(financial_beta, levered_beta, rel_tol=1e-12, abs_tol=1e-12)
        or result.get("industry_unlevered_beta") is not None
        or str(result.get("roe_basis") or "") != "average_begin_end_attributable_equity"
        or str(result.get("wacc_capital_structure") or "") != "financial_equity_cost_only"
        or str(result.get("tax_shield_source") or "") != "financial_operating_liabilities_excluded"
        or _safe_float(result.get("tax_shield_rate")) != 0.0
    ):
        return False
    for key in ("equity_basis", "beta_source", "financial_growth_basis", "model_risk_data_as_of"):
        if not str(result.get(key) or "").strip():
            return False
    if str(result.get("equity_basis")) not in {
        "parent_equity",
        "total_less_minority",
        "mixed_attributable_equity_sources",
    }:
        return False
    try:
        if date.fromisoformat(str(result["model_risk_data_as_of"])) > _shanghai_today():
            return False
    except (TypeError, ValueError):
        return False

    evidence_years = result.get("roe_evidence_years")
    if (
        not isinstance(evidence_years, (list, tuple))
        or len(evidence_years) < 3
        or any(isinstance(year, bool) or not isinstance(year, (int, np.integer)) for year in evidence_years)
        or any(current - prior != 1 for prior, current in zip(evidence_years, evidence_years[1:]))
    ):
        return False

    components = result.get("wacc_components")
    if not isinstance(components, Mapping):
        return False
    cost_of_equity = _safe_float(components.get("cost_of_equity"))
    if (
        _safe_float(components.get("equity_weight")) != 1.0
        or _safe_float(components.get("debt_weight")) != 0.0
        or cost_of_equity is None
        or cost_of_equity <= 0
        or components.get("pre_tax_cost_of_debt") is not None
        or _safe_float(components.get("tax_shield_rate")) != 0.0
        or not math.isclose(base_cost, cost_of_equity, rel_tol=1e-4, abs_tol=5.1e-5)
    ):
        return False

    points = result.get("dcf_points")
    params = result.get("params")
    if not isinstance(points, Mapping) or not isinstance(params, Mapping):
        return False
    bands: dict[str, tuple[float, float]] = {}
    midpoints: list[float] = []
    for scenario in ("pessimistic", "neutral", "optimistic"):
        band = points.get(scenario)
        scenario_params = params.get(scenario)
        if not isinstance(band, Mapping) or not isinstance(scenario_params, Mapping):
            return False
        lower = _safe_float(band.get("lower"))
        upper = _safe_float(band.get("upper"))
        growth = _safe_float(scenario_params.get("growth"))
        scenario_roe = _safe_float(scenario_params.get("scenario_roe"))
        scenario_cost = _safe_float(scenario_params.get("cost_of_equity"))
        scenario_wacc = _safe_float(scenario_params.get("wacc_base"))
        scenario_beta = _safe_float(scenario_params.get("levered_beta"))
        bvps = _safe_float(scenario_params.get("bvps"))
        pb_lower = _safe_float(scenario_params.get("pb_lower"))
        pb_upper = _safe_float(scenario_params.get("pb_upper"))
        scenario_normalised_roe = _safe_float(scenario_params.get("normalised_roe"))
        if (
            lower is None
            or upper is None
            or lower <= 0
            or upper < lower
            or growth is None
            or scenario_roe is None
            or scenario_cost is None
            or scenario_wacc is None
            or scenario_beta is None
            or bvps is None
            or bvps <= 0
            or pb_lower is None
            or pb_upper is None
            or pb_lower <= 0
            or pb_upper < pb_lower
            or scenario_normalised_roe is None
            or scenario_roe <= growth
            or scenario_cost - BAND_WACC_DELTA <= growth
            or scenario_params.get("margin_retention") is not None
            or str(scenario_params.get("formula") or "") != "(normalised_roe - g) / (cost_of_equity - g)"
            or str(scenario_params.get("roe_basis") or "") != "average_begin_end_attributable_equity"
            or list(scenario_params.get("roe_years") or []) != list(evidence_years)
            or str(scenario_params.get("equity_basis") or "") != str(result.get("equity_basis"))
            or str(scenario_params.get("financial_growth_basis") or "") != str(result.get("financial_growth_basis"))
            or str(scenario_params.get("model_risk_data_as_of") or "") != str(result.get("model_risk_data_as_of"))
            or not math.isclose(scenario_cost, cost_of_equity, rel_tol=1e-9, abs_tol=1e-12)
            or not math.isclose(scenario_wacc, cost_of_equity, rel_tol=1e-9, abs_tol=1e-12)
            or not math.isclose(scenario_beta, financial_beta, rel_tol=1e-9, abs_tol=1e-12)
            or not math.isclose(scenario_normalised_roe, normalised_roe, rel_tol=1e-9, abs_tol=1e-12)
        ):
            return False
        expected_pb_lower = (scenario_roe - growth) / (scenario_cost + BAND_WACC_DELTA - growth)
        expected_pb_upper = (scenario_roe - growth) / (scenario_cost - BAND_WACC_DELTA - growth)
        if (
            not math.isclose(pb_lower, expected_pb_lower, rel_tol=1e-9, abs_tol=1e-12)
            or not math.isclose(pb_upper, expected_pb_upper, rel_tol=1e-9, abs_tol=1e-12)
            or not math.isclose(lower, bvps * pb_lower, rel_tol=1e-9, abs_tol=1e-8)
            or not math.isclose(upper, bvps * pb_upper, rel_tol=1e-9, abs_tol=1e-8)
        ):
            return False
        bands[scenario] = (lower, upper)
        midpoints.append((lower + upper) / 2.0)
    if midpoints != sorted(midpoints):
        return False

    buy_upper = _safe_float(result.get("buy_zone_upper"))
    sell_lower = _safe_float(result.get("sell_zone_lower"))
    mean1 = _safe_float(result.get("mean1"))
    mean2 = _safe_float(result.get("mean2"))
    expected_buy = (bands["pessimistic"][1] + bands["neutral"][0]) / 2.0
    expected_sell = (bands["neutral"][1] + bands["optimistic"][0]) / 2.0
    if (
        buy_upper is None
        or sell_lower is None
        or mean1 is None
        or mean2 is None
        or buy_upper > sell_lower
        or not math.isclose(buy_upper, expected_buy, rel_tol=1e-9, abs_tol=1e-8)
        or not math.isclose(sell_lower, expected_sell, rel_tol=1e-9, abs_tol=1e-8)
        or not math.isclose(mean1, buy_upper, rel_tol=1e-9, abs_tol=1e-8)
        or not math.isclose(mean2, sell_lower, rel_tol=1e-9, abs_tol=1e-8)
    ):
        return False
    expected_zone = "买入区" if result_price <= buy_upper else "卖出区" if result_price >= sell_lower else "观察区"
    return str(result.get("zone") or "") == expected_zone


def _prepare_dcf_validation_cache(m: dict[str, Any], result: Any) -> None:
    """Validate each DCF surface once for the three Patch 6 consumers.

    Type 1, Type 4, Type 5 and Type 7 all consume the same source-bound valuation.
    Replaying strict TTM reconstruction and all six long-horizon DCF bands in
    every framework multiplied the dominant full-market CPU cost.  This cache
    is transient, bound to the exact metric/result objects and never exported.
    Direct score-function callers still validate normally.
    """
    if not isinstance(result, Mapping):
        return
    cache: dict[str, Any] = {
        "token": _DCF_VALIDATION_CACHE_TOKEN,
        "metric_id": id(m),
        "result_id": id(result),
    }
    m["_dcf_validation_cache"] = cache
    if str(m.get("industry") or "") in SUPPORTED_FINANCIAL_INDUSTRIES:
        cache["financial_pb"] = _valid_financial_pb_evidence(m, result)
        return
    cache["nonfinancial"] = _valid_nonfinancial_dcf_evidence(m, result)
    cache["long_horizon"] = _valid_long_horizon_dcf_evidence(m, result)


def score_type1_dcf(
    m: Mapping[str, Any],
    dcf_result: Optional[Mapping[str, Any]],
    benchmarks: Mapping[str, Mapping[str, Any]],
    dcf_skip_classification: Optional[Mapping[str, Any]] = None,
):
    """情况一：对应估值模型买入区、价值陷阱、安全边际和回归动力。"""
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    if str(m.get("industry", "")) == "FINANCIAL_OTHER":
        return _not_applicable("type1", "其他金融暂无专属估值与七类型模型")
    if dcf_result is None:
        skip_outcome = _valuation_skip_outcome("type1", dcf_skip_classification)
        if skip_outcome is not None:
            return skip_outcome
    buy_upper = _safe_float((dcf_result or {}).get("buy_zone_upper"))
    price = _safe_float(m.get("price"))
    if price is None:
        price = _safe_float((dcf_result or {}).get("current_price"))
    is_financial = str(m.get("industry", "")) in SUPPORTED_FINANCIAL_INDUSTRIES
    financial_evidence_valid = is_financial and _valid_financial_pb_evidence(m, dcf_result)
    nonfinancial_evidence_valid = not is_financial and _valid_nonfinancial_dcf_evidence(m, dcf_result)
    if is_financial and not financial_evidence_valid:
        return _insufficient_evidence("type1", "金融股合理市净率估值证据不完整")
    if not is_financial and not nonfinancial_evidence_valid:
        return _insufficient_evidence("type1", "非金融DCF证据不完整")
    in_buy_zone = bool(
        buy_upper is not None and buy_upper > 0 and price is not None and price > 0 and price <= buy_upper
    )
    if buy_upper is not None and buy_upper > 0 and price is not None and price > 0:
        depth = (buy_upper - price) / buy_upper
        if depth > 0.20:
            scores["1a"], reasons["1a"] = 9.5, f"买入区内折价{depth:.0%}"
        elif depth >= 0.10:
            scores["1a"], reasons["1a"] = 7.5, f"买入区内折价{depth:.0%}"
        elif depth >= 0:
            scores["1a"], reasons["1a"] = 5.5, f"刚进买入区{depth:.0%}"
        elif depth >= -0.10:
            scores["1a"], reasons["1a"] = 3.5, f"距买入区{abs(depth):.0%}"
        else:
            scores["1a"], reasons["1a"] = 1.5, f"远离买入区{abs(depth):.0%}"
    else:
        zone = str((dcf_result or {}).get("zone", ""))
        mapping = {"买入区": (5.0, "买入区但无上沿"), "观察区": (3.0, "处于观察区"), "卖出区": (1.0, "处于卖出区")}
        scores["1a"], reasons["1a"] = mapping.get(zone, (0.0, "无有效DCF结果"))
        in_buy_zone = zone == "买入区"

    # 1a只描述价格相对模型买入区的深度。监管质量、现金流和最新期恶化属于
    # 价值陷阱/现金流/催化剂证据，不能篡改1a并制造补丁6的一票否决。

    # 补丁6严格五项，每项0/1/2分。金融机构使用归母ROE、归母权益和
    # 利润稳定性，绝不把存款负债、OCF-Capex或净债务套入工业语义。
    trap_points: list[int] = []
    funds = _safe_float(m.get("monetary_funds"))
    interest_debt = _safe_float(m.get("interest_debt"))
    assets = _safe_float(m.get("total_assets"))
    trap_evidence_complete = True
    financial_hard_breach = False
    if is_financial:
        trap_points, trap_evidence_complete, financial_trap_reason = _financial_regulatory_trap_points(m)
        financial_hard_breach = trap_evidence_complete and _financial_hard_regulatory_breach(m)
    else:
        industry_durability = _verified_score(m, "industry_durability_score")
        industry_growth = _safe_float(_get_bench(benchmarks, str(m.get("industry", "")), "median_cagr"))
        industry_sample = _safe_float(_get_bench(benchmarks, str(m.get("industry", "")), "median_cagr_count"))
        if industry_durability is not None:
            trap_points.append(2 if industry_durability >= 7 else 1 if industry_durability >= 4 else 0)
        elif (
            industry_growth is not None
            and industry_sample is not None
            and industry_sample >= 10
            and industry_growth >= 0.03
        ):
            trap_points.append(2)
            trap_evidence_complete = False
        elif (
            industry_growth is not None
            and industry_sample is not None
            and industry_sample >= 10
            and industry_growth >= -0.03
        ):
            trap_points.append(1)
            trap_evidence_complete = False
        elif industry_growth is not None and industry_sample is not None and industry_sample >= 10:
            trap_points.append(0)
            trap_evidence_complete = False
        else:
            trap_points.append(0)
            trap_evidence_complete = False

        interest_debt_ratio = _safe_float(m.get("interest_bearing_debt_ratio"))
        if interest_debt_ratio is None and interest_debt is not None and assets is not None and assets > 0:
            interest_debt_ratio = interest_debt / assets
        raw_fcf_values = m.get("fcf_history", [])
        raw_fcf_values = list(raw_fcf_values) if isinstance(raw_fcf_values, (list, tuple)) else []
        parsed_fcf_values = [_safe_float(item) for item in raw_fcf_values]
        fcf_history_complete = bool(
            len(raw_fcf_values) == len(parsed_fcf_values)
            and all(value is not None for value in parsed_fcf_values)
            and _aligned_current_consecutive(
                m,
                raw_fcf_values,
                m.get("fcf_years"),
                3,
            )
        )
        fcf_values = [float(value) for value in parsed_fcf_values if value is not None] if fcf_history_complete else []
        median_fcf = float(median(fcf_values[-3:])) if len(fcf_values) >= 3 else None
        net_debt_to_fcf = (
            max(0.0, interest_debt - funds) / median_fcf
            if interest_debt is not None and funds is not None and median_fcf is not None and median_fcf > 0
            else None
        )
        if interest_debt_ratio is None or net_debt_to_fcf is None:
            leverage_point = 0
            trap_evidence_complete = False
        elif interest_debt_ratio > 0.60 or net_debt_to_fcf > 4.0:
            leverage_point = 0
        elif interest_debt_ratio > 0.40 or net_debt_to_fcf > 2.0:
            leverage_point = 1
        else:
            leverage_point = 2
        if str(m.get("industry", "")) in STRONG_CYCLICAL_INDUSTRIES and (
            funds is None or interest_debt is None or funds < interest_debt
        ):
            leverage_point = min(leverage_point, 1)
        trap_points.append(leverage_point)

        if funds is not None and interest_debt is not None and assets is not None and assets > 0:
            cash_ratio, borrow_ratio = funds / assets, interest_debt / assets
            point = (
                0
                if cash_ratio >= 0.15 and borrow_ratio >= 0.15
                else (2 if cash_ratio < 0.10 or borrow_ratio < 0.10 else 1)
            )
        else:
            point = 0
            trap_evidence_complete = False
        trap_points.append(point)

        profit_map = dict(zip(m.get("net_profit_years", []), m.get("net_profit_history", [])))
        ocf_map = dict(zip(m.get("ocf_years", []), m.get("ocf_history", [])))
        adjusted_map = dict(zip(m.get("adjusted_profit_ratio_years", []), m.get("adjusted_profit_ratio_history", [])))
        asset_map = dict(zip(m.get("total_assets_years", []), m.get("total_assets_history", [])))
        accounting_years = sorted(set(profit_map) & set(ocf_map) & set(adjusted_map) & set(asset_map))[-3:]
        accounting_complete = all(
            (
                _aligned_current_consecutive(
                    m,
                    m.get("net_profit_history", []),
                    m.get("net_profit_years"),
                    3,
                ),
                _aligned_current_consecutive(
                    m,
                    m.get("ocf_history", []),
                    m.get("ocf_years"),
                    3,
                ),
                _aligned_current_consecutive(
                    m,
                    m.get("adjusted_profit_ratio_history", []),
                    m.get("adjusted_profit_ratio_years"),
                    3,
                ),
                _aligned_current_consecutive(
                    m,
                    m.get("total_assets_history", []),
                    m.get("total_assets_years"),
                    3,
                ),
                len(accounting_years) == 3,
                all(current - prior == 1 for prior, current in zip(accounting_years, accounting_years[1:])),
            )
        )
        accounting_integrity = _verified_score(m, "accounting_integrity_score")
        if accounting_complete:
            total_profit = sum(_safe_float(profit_map[year]) or 0.0 for year in accounting_years)
            total_adjusted_profit = sum(
                (_safe_float(profit_map[year]) or 0.0) * (_safe_float(adjusted_map[year]) or 0.0)
                for year in accounting_years
            )
            total_ocf = sum(_safe_float(ocf_map[year]) or 0.0 for year in accounting_years)
            average_assets = float(np.mean([_safe_float(asset_map[year]) or 0.0 for year in accounting_years]))
            cash_conversion = total_ocf / total_adjusted_profit if total_adjusted_profit > 0 else None
            adjusted_share = total_adjusted_profit / total_profit if total_profit > 0 else None
            accrual_ratio = (total_adjusted_profit - total_ocf) / average_assets if average_assets > 0 else None
            if (
                cash_conversion is not None
                and adjusted_share is not None
                and accrual_ratio is not None
                and cash_conversion >= 0.90
                and adjusted_share >= 0.90
                and accrual_ratio <= 0.05
                and not m.get("ocf_q1_warning")
            ):
                accounting_point = 2
            elif (
                cash_conversion is not None
                and adjusted_share is not None
                and cash_conversion >= 0.60
                and adjusted_share >= 0.70
                and not m.get("ocf_q1_warning")
            ):
                accounting_point = 1
            else:
                accounting_point = 0
        else:
            accounting_point = 0
        if accounting_integrity is not None:
            accounting_point = 2 if accounting_integrity >= 7 else 1 if accounting_integrity >= 4 else 0
        else:
            trap_evidence_complete = False
        trap_points.append(accounting_point)

        management_alignment = _verified_score(m, "management_alignment_score")
        trap_points.append(
            2
            if management_alignment is not None and management_alignment >= 7
            else 1
            if management_alignment is not None and management_alignment >= 4
            else 0
        )
        if management_alignment is None:
            trap_evidence_complete = False
    scores["1b"] = float(sum(trap_points))
    governance_missing = not is_financial and _verified_score(m, "management_alignment_score") is None
    trap_research_missing = not is_financial and (
        _verified_score(m, "industry_durability_score") is None
        or _verified_score(m, "accounting_integrity_score") is None
    )
    if is_financial:
        reasons["1b"] = financial_trap_reason
        if financial_hard_breach:
            scores["1b"], reasons["1b"] = min(scores["1b"], 3.0), "低于监管最低线"
    elif governance_missing or trap_research_missing:
        reasons["1b"] = f"陷阱满分{sum(p == 2 for p in trap_points)}项;研究缺口"
    else:
        reasons["1b"] = f"五项满分{sum(p == 2 for p in trap_points)}项"

    fcf, latest_fcf, _fcf_basis = _traceable_normalised_fcf(
        m,
        dcf_result,
        result_valid=nonfinancial_evidence_valid,
    )
    market_cap = _safe_float(m.get("market_cap"))
    if is_financial and buy_upper is not None and buy_upper > 0 and price is not None and price > 0:
        safety = (buy_upper - price) / buy_upper
        scores["1c"] = _score_0_10(safety, [(-0.20, 0), (-0.10, 2), (0, 5), (0.10, 7), (0.25, 9), (0.40, 10)])
        reasons["1c"] = f"PB模型安全边际{safety:.1%}"
    elif fcf is not None and fcf > 0 and market_cap is not None and market_cap > 0:
        fcf_yield = fcf / market_cap
        scores["1c"] = _score_type1_fcf_yield(fcf_yield)
        reasons["1c"] = f"FCF{fcf_yield:.1%};末{_format_rmb(latest_fcf)}"
    elif fcf is not None:
        scores["1c"], reasons["1c"] = 0.0, "自由现金流非正"
    else:
        scores["1c"], reasons["1c"] = 0.0, ("缺金融股合理市净率估值结果" if is_financial else "缺资本开支数据")

    if is_financial:
        scores["1d"], catalyst_evidence_complete, reasons["1d"] = _financial_catalyst_score(m)
    else:
        catalyst = 0.0
        items: list[str] = []
        growth_1yr = _safe_float(m.get("growth_1yr"))
        cagr3 = _safe_float(m.get("cagr_3yr"))
        if growth_1yr is not None and cagr3 is not None and growth_1yr > max(cagr3 + 0.03, 0.05):
            catalyst += 3.0
            items.append("营收加速")
        profit_change = _safe_float(m.get("profit_1yr_change"))
        if profit_change is not None and profit_change > 0.10:
            catalyst += 3.0
            items.append("利润回升")
        interim_profit_yoy = _same_period_metric_yoy(m, "profit")
        if interim_profit_yoy is not None and interim_profit_yoy > 0.10:
            catalyst += 2.0
            items.append("最新同口径利润增")
        margins = m.get("margin_history", [])
        if (
            _aligned_current_consecutive(m, margins, m.get("margin_years"), 3)
            and margins[-1] > margins[-2] > margins[-3]
        ):
            catalyst += 2.0
            items.append("净利率连升")
        explicit_catalyst = _verified_score(m, "catalyst_score")
        catalyst_evidence_complete = explicit_catalyst is not None
        if explicit_catalyst is not None:
            scores["1d"] = explicit_catalyst
            reasons["1d"] = _evidence_reason(m, "catalyst_score", "催化剂证据不可追溯")
        else:
            scores["1d"] = min(6.0, catalyst)
            reasons["1d"] = f"财务回归弱代理{len(items)}项" if items else "催化事件证据缺失"

    latest_severity, latest_reason = _latest_period_deterioration(m, include_ocf=not is_financial)
    if latest_severity >= 3:
        scores["1d"], reasons["1d"] = min(scores["1d"], 1.0), latest_reason
    elif latest_severity == 2:
        scores["1d"], reasons["1d"] = min(scores["1d"], 3.0), latest_reason
    elif latest_severity == 1:
        scores["1d"], reasons["1d"] = min(scores["1d"], 6.0), latest_reason

    price_veto = scores["1a"] <= 2
    trap_veto = trap_evidence_complete and scores["1b"] <= 3
    veto = price_veto or trap_veto
    if scores["1a"] <= 2:
        reasons["_veto"] = "买入区深度不足"
    elif trap_veto:
        reasons["_veto"] = "价值陷阱未排除"
    if not in_buy_zone:
        reasons["_condition"] = "须进入模型买入区"
    missing_dimensions: list[str] = []
    missing_dimension_keys: list[str] = []
    if not trap_evidence_complete:
        missing_dimensions.append("价值陷阱")
        missing_dimension_keys.append("1b")
    if not catalyst_evidence_complete:
        missing_dimensions.append("回归催化")
        missing_dimension_keys.append("1d")
    if missing_dimensions:
        reasons["_missing"] = "缺" + "/".join(missing_dimensions) + "证据"
    return _finish(
        "type1",
        scores,
        reasons,
        veto=veto,
        extra_condition=in_buy_zone,
        evidence_complete=trap_evidence_complete and catalyst_evidence_complete,
        missing_dimensions=missing_dimension_keys,
    )


def _type2_company_turn_evidence(m: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether Type2 2b has a continuous, comparable operating chain.

    A mature company needs three consecutive annual observations for revenue,
    parent profit and net margin.  A genuinely recent listing may use two
    consecutive annual observations or an exact same-period interim revenue
    and parent-profit pair.  Listing age is never inferred from short history:
    without a canonical listing date, short annual data remains a source gap.
    Operating cash flow is useful corroboration in the score but is not an
    absolute completeness requirement because cash timing differs materially
    across otherwise comparable business models.
    """

    annual_three_year = all(
        (
            _aligned_current_consecutive(
                m,
                m.get("revenue_values", []),
                m.get("revenue_years"),
                3,
            ),
            _aligned_current_consecutive(
                m,
                m.get("net_profit_history", []),
                m.get("net_profit_years"),
                3,
            ),
            _aligned_current_consecutive(
                m,
                m.get("margin_history", []),
                m.get("margin_years"),
                3,
            ),
        )
    )
    if annual_three_year:
        return True, "公司3年连续财务数据"

    listing_date = _evidence_reference_date(m.get("listing_date"))
    reference_date = _evidence_reference_date(m.get("source_trade_date"))
    recent_listing = bool(
        listing_date is not None
        and reference_date is not None
        and reference_date >= listing_date
        and (reference_date - listing_date).days < 3 * 366
    )
    if not recent_listing:
        return False, "公司拐点连续数据不足"

    annual_two_year = all(
        (
            _aligned_current_consecutive(
                m,
                m.get("revenue_values", []),
                m.get("revenue_years"),
                2,
            ),
            _aligned_current_consecutive(
                m,
                m.get("net_profit_history", []),
                m.get("net_profit_years"),
                2,
            ),
            _aligned_current_consecutive(
                m,
                m.get("margin_history", []),
                m.get("margin_years"),
                2,
            ),
        )
    )
    if annual_two_year:
        return True, "上市后2年连续财务数据"

    interim_pair = all(
        (
            m.get("interim_revenue_pair_basis") == "same_period_yoy",
            m.get("interim_profit_pair_basis") == "same_period_yoy",
            _safe_float(m.get("interim_current_revenue")) is not None,
            _safe_float(m.get("interim_prior_revenue")) is not None,
            _safe_float(m.get("interim_current_profit")) is not None,
            _safe_float(m.get("interim_prior_profit")) is not None,
        )
    )
    if interim_pair:
        return True, "上市后同口径季报同比"
    return False, "新股缺2年或同口径季报"


def _score_type2_financial(m: Mapping[str, Any], benchmarks: Mapping[str, Mapping[str, Any]]):
    industry = str(m.get("industry") or "")
    if industry not in SUPPORTED_FINANCIAL_INDUSTRIES:
        return _not_applicable("type2", "其他金融暂无专属周期模型")
    bucket = benchmarks.get(industry, {})
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    minimum_sample = 3 if industry == "INSURANCE" else 5

    def benchmark(name: str) -> float | None:
        return _safe_float(bucket.get(name)) if isinstance(bucket, Mapping) else None

    def benchmark_ready(name: str) -> bool:
        count = benchmark(f"{name}_count")
        return benchmark(name) is not None and count is not None and count >= minimum_sample

    if industry == "BANK":
        required = ("median_nim_change", "median_npl_change", "median_profit_change")
        industry_ready = all(benchmark_ready(name) for name in required)
        nim = benchmark("median_nim_change")
        npl = benchmark("median_npl_change")
        profit = benchmark("median_profit_change")
        capital = benchmark("median_bank_capital_change")
        score = 2.0
        if nim is not None:
            score += 3.0 if nim > 0 else 1.5 if nim >= -0.0005 else 0.0
        if npl is not None:
            score += 2.0 if npl < 0 else 1.0 if npl <= 0.0005 else 0.0
        if profit is not None:
            score += 2.0 if profit > 0.05 else 1.0 if profit > 0 else 0.0
        if capital is not None and capital >= 0:
            score += 1.0
        reasons["2a"] = "银行业息差/不良周期"
    elif industry == "INSURANCE":
        required = ("median_nbv_growth", "median_solvency_change", "median_profit_change")
        industry_ready = all(benchmark_ready(name) for name in required)
        nbv = benchmark("median_nbv_growth")
        margin = benchmark("median_nbv_margin_change")
        solvency = benchmark("median_solvency_change")
        surrender = benchmark("median_surrender_change")
        profit = benchmark("median_profit_change")
        score = 2.0
        if nbv is not None:
            score += 3.0 if nbv > 0.10 else 1.5 if nbv > 0 else 0.0
        if margin is not None and margin > 0:
            score += 1.0
        if solvency is not None and solvency >= 0:
            score += 1.0
        if surrender is not None and surrender < 0:
            score += 1.0
        if profit is not None:
            score += 2.0 if profit > 0.10 else 1.0 if profit > 0 else 0.0
        reasons["2a"] = "保险业新业务周期"
    else:
        required = ("median_risk_coverage_change", "median_capital_leverage_change", "median_profit_change")
        industry_ready = all(benchmark_ready(name) for name in required)
        risk = benchmark("median_risk_coverage_change")
        leverage = benchmark("median_capital_leverage_change")
        liquidity = benchmark("median_liquidity_coverage_change")
        funding = benchmark("median_stable_funding_change")
        profit = benchmark("median_profit_change")
        score = 2.0
        if profit is not None:
            score += 4.0 if profit > 0.15 else 2.0 if profit > 0 else 0.0
        for value in (risk, leverage, liquidity, funding):
            if value is not None and value > 0:
                score += 1.0
        reasons["2a"] = "券商业绩/风控周期"
    scores["2a"] = min(10.0, score) if industry_ready else 2.0
    if not industry_ready:
        reasons["2a"] = "金融行业样本不足"

    scores["2b"], company_ready, reasons["2b"] = _financial_catalyst_score(m)

    coldness = _verified_market_coldness_score(m)
    market_coldness_missing = coldness is None
    if coldness is None:
        scores["2c"], reasons["2c"] = 0.0, "缺独立量价冷度证据"
    else:
        scores["2c"] = coldness
        reasons["2c"] = _evidence_reason(m, "market_coldness_score", "冷度证据不可追溯")

    pb = _safe_float(m.get("pb"))
    median_pb = benchmark("median_pb")
    median_pb_count = benchmark("median_pb_count")
    valuation_evidence_complete = bool(
        pb is not None
        and pb > 0
        and median_pb is not None
        and median_pb > 0
        and median_pb_count is not None
        and median_pb_count >= minimum_sample
    )
    if valuation_evidence_complete:
        pb_ratio = pb / median_pb
        scores["2d"] = _score_0_10(
            pb_ratio,
            [(0.50, 9), (0.75, 7.5), (1.10, 5.5), (1.50, 4), (2.00, 2), (3.00, 1)],
        )
        reasons["2d"] = f"金融PB/同行{pb_ratio:.1f}倍"
    else:
        scores["2d"], reasons["2d"] = 2.0, "金融PB同行证据不足"

    decision_scores = _sanitize_scores(scores, TYPE_WEIGHTS["type2"])
    hot_average = (decision_scores["2a"] + decision_scores["2b"]) / 2.0
    hot_veto = industry_ready and company_ready and hot_average <= 4.0
    cold_veto = not market_coldness_missing and scores["2c"] <= 3.0
    valuation_adjustment = hot_average >= 7 and scores["2c"] >= 7 and 4 <= scores["2d"] <= 5
    valuation_ready = scores["2d"] >= 5 or valuation_adjustment
    missing_dimensions: list[str] = []
    missing_dimension_keys: list[str] = []
    if not industry_ready:
        missing_dimensions.append("金融行业周期")
        missing_dimension_keys.append("2a")
    if not company_ready:
        missing_dimensions.append("金融公司拐点")
        missing_dimension_keys.append("2b")
    if market_coldness_missing:
        missing_dimensions.append("市场冷度")
        missing_dimension_keys.append("2c")
    if not valuation_evidence_complete:
        missing_dimensions.append("金融估值")
        missing_dimension_keys.append("2d")
    if missing_dimensions:
        reasons["_missing"] = "缺" + "/".join(missing_dimensions) + "证据"
    reasons["_coverage"] = "金融周期/公司/冷度/PB齐全" if not missing_dimensions else "金融四维证据存在缺口"
    if hot_veto:
        reasons["_veto"] = "金融两热平均须大于4"
    elif cold_veto:
        reasons["_veto"] = "市场周期不够冷"
    if not valuation_ready:
        reasons["_condition"] = "金融估值须合理"
    return _finish(
        "type2",
        scores,
        reasons,
        veto=hot_veto or cold_veto,
        extra_condition=valuation_ready,
        evidence_complete=bool(
            industry_ready and company_ready and not market_coldness_missing and valuation_evidence_complete
        ),
        missing_dimensions=missing_dimension_keys,
    )


def score_type2_two_hot_one_cold(m: Mapping[str, Any], benchmarks: Mapping[str, Mapping[str, Any]]):
    """情况二：产业热、公司拐点、市场冷和估值合理。"""
    if str(m.get("industry", "")) in FINANCIAL_INDUSTRIES:
        return _score_type2_financial(m, benchmarks)
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    industry = str(m.get("industry", ""))
    industry_bucket = {} if industry == "DEFAULT" else benchmarks.get(industry, {})
    peer_context = m.get("_quantitative_peer_context")
    peer_context = peer_context if isinstance(peer_context, Mapping) else {}
    aggregate_growth = _safe_float(peer_context.get("aggregate_revenue_cagr"))
    aggregate_sample = _safe_float(peer_context.get("aggregate_revenue_cagr_count"))
    aggregate_coverage = _safe_float(peer_context.get("aggregate_revenue_coverage"))
    aggregate_ready = bool(
        peer_context.get("target_excluded") is True
        and aggregate_growth is not None
        and aggregate_sample is not None
        and aggregate_sample >= MIN_SECTOR_COMPANIES
        and aggregate_coverage is not None
        and aggregate_coverage >= MIN_COMPARABLE_COVERAGE
    )
    fallback_growth = _safe_float(industry_bucket.get("median_cagr"))
    fallback_sample = _safe_float(industry_bucket.get("median_cagr_count"))
    growth = (
        aggregate_growth
        if aggregate_ready
        else fallback_growth
        if not peer_context and fallback_sample is not None and fallback_sample >= MIN_SECTOR_COMPANIES
        else None
    )
    industry_evidence_missing = growth is None
    if growth is None:
        scores["2a"], reasons["2a"] = 2.0, "产业增速无数据"
    else:
        scores["2a"] = _score_0_10(
            growth,
            [(-0.10, 0.5), (-0.08, 1.0), (0.0, 2.0), (0.05, 5.0), (0.12, 7.0), (0.25, 8.5), (0.50, 10.0)],
        )
        reasons["2a"] = f"产业聚合增速{growth:.1%}" if aggregate_ready else f"产业横截面增速{growth:.1%}"

    revenue = m.get("revenue_values", [])
    revenue_years = m.get("revenue_years", [])
    margins = m.get("margin_history", [])
    profits = m.get("net_profit_history", [])
    profit_years = m.get("net_profit_years", [])
    company_turn_evidence_complete, company_turn_evidence_basis = _type2_company_turn_evidence(m)
    points = 0.0
    signals: list[str] = []
    rates = _growth_rates(revenue[-3:]) if _aligned_current_consecutive(m, revenue, revenue_years, 3) else []
    if len(rates) == 2 and rates[-1] > 0.05 and rates[-1] > rates[-2] + 0.03:
        points += 3
        signals.append("营收加速")
    elif rates and rates[-1] > 0.05:
        points += 1
    margin_years = m.get("margin_years", [])
    if _aligned_current_consecutive(m, margins, margin_years, 3) and margins[-1] > margins[-2] > margins[-3]:
        points += 3
        signals.append("净利率连升")
    elif _aligned_current_consecutive(m, margins, margin_years, 2) and margins[-1] > margins[-2]:
        points += 1.5
    if _aligned_current_consecutive(m, profits, profit_years, 3) and profits[-1] > profits[-2] > profits[-3]:
        points += 2.5
        signals.append("利润连升")
    ocf_np = _safe_float(m.get("ocf_np_ratio"))
    if ocf_np is not None and ocf_np >= 0.7:
        points += 2
        signals.append("现金流支撑")
    elif ocf_np is not None and ocf_np >= 0.3:
        points += 1
    interim_revenue_yoy = _safe_float(m.get("interim_revenue_yoy"))
    interim_profit_yoy = _safe_float(m.get("interim_profit_yoy"))
    interim_ocf_yoy = _safe_float(m.get("interim_ocf_yoy"))
    if interim_revenue_yoy is not None and interim_revenue_yoy > 0.05:
        points += 1.0
        signals.append("最新同口径营收增")
    if interim_profit_yoy is not None and interim_profit_yoy > 0.10:
        points += 1.0
        signals.append("最新同口径利润增")
    if interim_ocf_yoy is not None and interim_ocf_yoy > 0:
        points += 0.5
    scores["2b"] = min(10.0, points)
    reasons["2b"] = f"拐点{len(signals)}项:" + "+".join(signals[:2]) if signals else "拐点证据不足"

    def cap_company_turn(limit: float, reason: str) -> None:
        if scores["2b"] > limit:
            scores["2b"], reasons["2b"] = limit, reason

    profit_change = _current_annual_change(m, "net_profit_history", "net_profit_years")
    if profit_change is not None and profit_change <= -0.50:
        cap_company_turn(2.0, "年度利润暴跌")
    elif profit_change is not None and profit_change < -0.20:
        cap_company_turn(4.0, "年度利润明显下滑")
    if m.get("interim_profit_warning") or (interim_profit_yoy is not None and interim_profit_yoy <= -0.50):
        cap_company_turn(2.0, "最新同口径利润恶化")
    elif m.get("q1_profit_warning"):
        cap_company_turn(3.0, "Q1利润已转负")
    elif interim_profit_yoy is not None and interim_profit_yoy <= -0.20:
        cap_company_turn(4.0, "最新同口径利润明显下滑")
    elif interim_profit_yoy is not None and interim_profit_yoy < 0:
        cap_company_turn(6.0, "最新同口径利润下滑,拐点封顶")
    if m.get("interim_ocf_warning") or (interim_ocf_yoy is not None and interim_ocf_yoy <= -0.50):
        cap_company_turn(2.0, "最新同口径现金恶化")
    elif m.get("ocf_q1_warning"):
        cap_company_turn(3.0, "Q1经营现金流转负")
    elif interim_ocf_yoy is not None and interim_ocf_yoy <= -0.20:
        cap_company_turn(4.0, "最新同口径经营现金流明显下滑")
    elif interim_ocf_yoy is not None and interim_ocf_yoy < 0:
        cap_company_turn(6.0, "最新同口径经营现金流下滑,拐点封顶")
    current_interim_revenue = _safe_float(m.get("interim_current_revenue"))
    if m.get("interim_revenue_warning") or (
        m.get("interim_revenue_pair_basis") == "same_period_yoy"
        and current_interim_revenue is not None
        and current_interim_revenue < 0
    ):
        cap_company_turn(2.0, "最新同口径营收为负")
    elif interim_revenue_yoy is not None and interim_revenue_yoy <= -0.20:
        cap_company_turn(4.0, "最新同口径营收明显下滑")
    elif interim_revenue_yoy is not None and interim_revenue_yoy < 0:
        cap_company_turn(6.0, "最新同口径营收下滑,拐点封顶")
    if m.get("interim_yoy_basis") != "same_period_yoy" and scores["2b"] > 6.0:
        exact_pairs = all(
            m.get(f"interim_{metric}_pair_basis") == "same_period_yoy" for metric in ("revenue", "profit")
        )
        zero_base = any(
            m.get(f"interim_{metric}_yoy_basis") == "invalid_same_period_base" for metric in ("revenue", "profit")
        )
        reason = "零基数无可比同比,拐点封顶" if exact_pairs and zero_base else "缺最新同口径报告期,拐点封顶"
        cap_company_turn(6.0, reason)

    explicit_coldness = _verified_market_coldness_score(m)
    market_coldness_missing = explicit_coldness is None
    if explicit_coldness is not None and 0 <= explicit_coldness <= 10:
        scores["2c"] = explicit_coldness
        reasons["2c"] = _evidence_reason(m, "market_coldness_score", "冷度证据不可追溯")
    else:
        # 2c是市场情绪/筹码周期，2d才是估值。用PE/PB同时给2c和2d
        # 打分会把同一证据重复加权，并制造“估值便宜=市场一定冷”的
        # 假信号。缺独立量价或人工可追溯证据时必须明确N/A。
        scores["2c"], reasons["2c"] = 0.0, "缺独立量价冷度证据"

    # 2d独立使用增长调整PE，缺失时再用同行PB。
    peg = _safe_float(m.get("peg"))
    if not _aligned_current_consecutive(m, profits, profit_years, 3):
        peg = None
    pb = _safe_float(m.get("pb"))
    median_pb = None if industry == "DEFAULT" else _safe_float(_get_bench(benchmarks, industry, "median_pb"))
    median_pb_count = (
        None if industry == "DEFAULT" else _safe_float(_get_bench(benchmarks, industry, "median_pb_count"))
    )
    if peg is not None and peg > 0:
        valuation_evidence_complete = True
        valuation_evidence_basis = "盈利趋势PEG"
        scores["2d"] = _score_0_10(
            peg,
            [(0.50, 10), (0.80, 9), (1.20, 7.5), (1.80, 5), (2.00, 4), (2.50, 3), (4.00, 1)],
        )
        reasons["2d"] = f"归母利润趋势PEG{peg:.1f}"
    elif (
        pb is not None
        and pb > 0
        and median_pb is not None
        and median_pb > 0
        and median_pb_count is not None
        and median_pb_count >= MIN_SECTOR_COMPANIES
    ):
        valuation_evidence_complete = True
        valuation_evidence_basis = "同行市净率"
        pb_ratio = pb / median_pb
        scores["2d"] = _score_0_10(
            pb_ratio,
            [(0.50, 9), (0.75, 7.5), (1.10, 5.5), (1.50, 4), (2.00, 2), (3.00, 1)],
        )
        reasons["2d"] = f"当前PB/行业{pb_ratio:.1f}倍"
    else:
        valuation_evidence_complete = False
        valuation_evidence_basis = "估值证据缺失"
        scores["2d"], reasons["2d"] = 2.0, "估值数据不足"

    # 否决边界必须使用最终对外展示的一位小数分数。否则插值得到的
    # 2.00000006 会在界面显示为2.0，却与2b=6.0一起绕过“平均<=4”
    # 的否决，导致可见证据与判定不一致。
    decision_scores = _sanitize_scores(scores, TYPE_WEIGHTS["type2"])
    hot_average = (decision_scores["2a"] + decision_scores["2b"]) / 2
    # 补丁6的原文否决条件是 ``(2a + 2b) / 2 <= 4``，并没有要求
    # 两项各自都大于4。旧实现把平均条件擅自收紧成逐项条件，会错杀
    # “产业刚回暖、公司强拐点”等典型错配机会。
    hot_dimensions_ready = hot_average > 4
    hot_veto = not industry_evidence_missing and company_turn_evidence_complete and not hot_dimensions_ready
    cold_veto = not market_coldness_missing and scores["2c"] <= 3
    veto = hot_veto or cold_veto
    missing_dimensions: list[str] = []
    missing_dimension_keys: list[str] = []
    if industry_evidence_missing:
        missing_dimensions.append("产业周期")
        missing_dimension_keys.append("2a")
    if not company_turn_evidence_complete:
        missing_dimensions.append("公司拐点")
        missing_dimension_keys.append("2b")
    if market_coldness_missing:
        missing_dimensions.append("市场冷度")
        missing_dimension_keys.append("2c")
    if not valuation_evidence_complete:
        missing_dimensions.append("估值")
        missing_dimension_keys.append("2d")
    if missing_dimensions:
        reasons["_missing"] = "缺" + "/".join(missing_dimensions) + "证据"
    reasons["_coverage"] = f"{company_turn_evidence_basis};{valuation_evidence_basis}"
    if hot_veto:
        reasons["_veto"] = "产业与公司热度平均须>4"
    elif cold_veto:
        reasons["_veto"] = "市场周期不够冷"
    # 估值必须合理（>=5）。补丁6只在“两热”和“冷”都很强时，
    # 允许4~5分的中性偏高估值；显著高估绝不能仅靠其他维度补回。
    valuation_adjustment = bool(
        valuation_evidence_complete and hot_average >= 7 and scores["2c"] >= 7 and 4 <= scores["2d"] <= 5
    )
    valuation_ready = bool(valuation_evidence_complete and (scores["2d"] >= 5 or valuation_adjustment))
    if valuation_adjustment:
        reasons["_adjustment"] = "强两热一冷允许中性估值"
    if not valuation_ready:
        reasons["_condition"] = "估值须合理或满足强周期修正"
    return _finish(
        "type2",
        scores,
        reasons,
        veto=veto,
        extra_condition=valuation_ready,
        evidence_complete=bool(
            not industry_evidence_missing
            and company_turn_evidence_complete
            and not market_coldness_missing
            and valuation_evidence_complete
        ),
        missing_dimensions=missing_dimension_keys,
    )


def score_type3_sustainable_growth(m: Mapping[str, Any], benchmarks: Mapping[str, Mapping[str, Any]]):
    """情况三：使用趋势调整增长，拒绝3/5年CAGR择高。"""
    if str(m.get("industry", "")) in FINANCIAL_INDUSTRIES:
        return _not_applicable("type3", "金融机构不适用可持续高增长型")
    revenue_values = list(m.get("revenue_values", []))
    revenue_years = m.get("revenue_years", [])
    if not _aligned_current_consecutive(m, revenue_values, revenue_years, 4):
        return _insufficient_evidence("type3", "缺截至最新完整财年的连续4年营收")
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    trend_growth = _trend_adjusted_growth(revenue_values)
    if trend_growth is None:
        return _insufficient_evidence("type3", "趋势增长证据缺失")
    if trend_growth < 0.10:
        return _not_applicable("type3", "趋势增速不足10%")

    roe = _safe_float(m.get("roe"))
    margin = _safe_float(m.get("margin_median_hist"))
    ocf_np = _safe_float(m.get("ocf_np_ratio"))
    explicit_moat = _verified_score(m, "moat_score")
    moat_evidence_complete = explicit_moat is not None
    if explicit_moat is not None and 0 <= explicit_moat <= 10:
        scores["3a"] = explicit_moat
        reasons["3a"] = _evidence_reason(m, "moat_score", "护城河证据不可追溯")
    else:
        moat_count = sum(
            (
                roe is not None and roe >= 0.20,
                margin is not None and margin >= 0.10,
                ocf_np is not None and ocf_np >= 0.70,
            )
        )
        proxy_moat_score = {0: 1.5, 1: 3.0, 2: 5.0, 3: 6.0}[moat_count]
        scores["3a"] = 5.0
        reasons["3a"] = f"财务护城河弱代理{moat_count}项"
        gross_margin = _safe_float(m.get("gross_margin"))
        gross_margin_cv = _safe_float(m.get("gross_margin_cv"))
        gross_margin_samples = _safe_float(m.get("gross_margin_samples"))
        indicator_roic = _safe_float(m.get("indicator_roic"))
        durable_quantitative_moat = bool(
            gross_margin is not None
            and gross_margin >= 0.25
            and gross_margin_cv is not None
            and gross_margin_cv <= 0.15
            and gross_margin_samples is not None
            and gross_margin_samples >= 3
            and indicator_roic is not None
            and indicator_roic >= 0.12
        )
        if durable_quantitative_moat:
            proxy_moat_score = min(6.0, proxy_moat_score + 1.0)
            reasons["3a"] = f"财务弱代理{proxy_moat_score:.0f}分;毛利{gross_margin:.1%},ROIC{indicator_roic:.1%}"

    debt = _safe_float(m.get("debt_ratio"))
    consistency = _safe_float(m.get("growth_consistency"))
    cash_score = (
        4.0
        if ocf_np is not None and ocf_np >= 0.9
        else 3.0
        if ocf_np is not None and ocf_np >= 0.6
        else 1.0
        if ocf_np is not None
        else 0.0
    )
    leverage_score = (
        3.0
        if debt is not None and debt < 0.20
        else 2.0
        if debt is not None and debt < 0.40
        else 1.0
        if debt is not None and debt < 0.60
        else 0.0
    )
    stability_score = (
        3.0
        if consistency is not None and consistency < 0.30
        else 2.0
        if consistency is not None and consistency < 0.50
        else 1.0
        if consistency is not None and consistency < 0.80
        else 0.0
    )
    explicit_growth_quality = _verified_score(m, "growth_quality_score")
    growth_quality_evidence_complete = explicit_growth_quality is not None
    if explicit_growth_quality is not None and 0 <= explicit_growth_quality <= 10:
        scores["3b"] = explicit_growth_quality
        reasons["3b"] = _evidence_reason(m, "growth_quality_score", "增长质量证据不可追溯")
    else:
        scores["3b"] = min(6.0, cash_score + leverage_score + stability_score)
        quality_evidence: list[str] = []
        adjusted_ratio = _safe_float(m.get("adjusted_profit_ratio"))
        if adjusted_ratio is not None:
            if 0.90 <= adjusted_ratio <= 1.10:
                scores["3b"] += 1.0
            elif 0.75 <= adjusted_ratio <= 1.25:
                scores["3b"] += 0.5
            elif adjusted_ratio < 0.50:
                scores["3b"] = min(scores["3b"], 4.0)
            quality_evidence.append(f"扣非/归母{adjusted_ratio:.2f}")
        dilution = _safe_float(m.get("share_dilution_1yr"))
        if dilution is not None:
            if dilution <= 0:
                scores["3b"] += 1.0
            elif dilution <= 0.02:
                scores["3b"] += 0.5
            elif dilution > 0.10:
                scores["3b"] = min(scores["3b"], 4.0)
            quality_evidence.append(f"股本同比{dilution:.1%}")
        scores["3b"] = min(8.0, scores["3b"])
        proxy_quality_score = scores["3b"]
        scores["3b"] = 5.0
        compact_quality = [item.replace("扣非/归母", "扣非").replace("股本同比", "稀释") for item in quality_evidence]
        reasons["3b"] = (
            f"质代{proxy_quality_score:.0f};" + "/".join(compact_quality)
            if compact_quality
            else f"质代{proxy_quality_score:.0f};缺并购拆分"
        )
    ocf_trend = _safe_float(m.get("ocf_3yr_change"))
    if ocf_trend is not None and ocf_trend <= -0.50:
        scores["3b"], reasons["3b"] = min(scores["3b"], 3.0), "经营现金流三年锐减"
    latest_severity, latest_reason = _latest_period_deterioration(m)
    if latest_severity >= 3:
        scores["3b"], reasons["3b"] = min(scores["3b"], 2.0), latest_reason
    elif latest_severity == 2:
        scores["3b"], reasons["3b"] = min(scores["3b"], 3.0), latest_reason
    elif latest_severity == 1:
        scores["3b"], reasons["3b"] = min(scores["3b"], 5.0), latest_reason

    # ROIC与WACC必须共享公司资本结构口径；缺失时不足以证明价值创造。
    roic, wacc = _safe_float(m.get("roic")), _safe_float(m.get("wacc"))
    basis_valid = m.get("roic_wacc_basis") in {
        "NOPAT/平均投入资本代理",
        "Eastmoney年度ROIC/公司资本结构WACC",
    }
    roic_wacc_missing = roic is None or wacc is None or not basis_valid
    if roic is not None and wacc is not None and basis_valid:
        spread = roic - wacc
        scores["3c"] = (
            9.5 if spread >= 0.10 else 7.5 if spread >= 0.05 else 5.5 if spread >= 0.02 else 3.5 if spread >= 0 else 1.5
        )
        reasons["3c"] = f"投入回报率减资金成本={spread:.1%}"
    else:
        # Missing evidence is unknown, not a confirmed 0-3 failure.  A neutral
        # display score keeps the radar shape stable while evidence_complete
        # below prevents this diagnostic from becoming a buy signal.
        scores["3c"], reasons["3c"] = 5.0, "缺同口径投入回报率和资金成本"

    explicit_sustainability = _verified_score(m, "growth_sustainability_score")
    sustainability_evidence_complete = explicit_sustainability is not None
    growth_slope = _safe_float(m.get("growth_slope"))
    if explicit_sustainability is not None:
        sustainable = explicit_sustainability
        reason_3d = _evidence_reason(m, "growth_sustainability_score", "增长纵深证据不可追溯")
    else:
        if consistency is not None and consistency < 0.30 and trend_growth >= 0.15:
            sustainable = 9.0
        elif consistency is not None and consistency < 0.50 and trend_growth >= 0.12:
            sustainable = 7.5
        elif consistency is not None and consistency < 0.80 and trend_growth >= 0.10:
            sustainable = 6.0
        else:
            sustainable = 4.0
        if growth_slope is not None and growth_slope < -0.05:
            sustainable = min(sustainable, 5.0)
            reason_3d = f"增速趋势减速{growth_slope:.1%}"
        else:
            reason_3d = f"趋势增速{trend_growth:.1%}"
        profit_change = _current_annual_change(m, "net_profit_history", "net_profit_years")
        if profit_change is not None and profit_change < -0.20:
            sustainable, reason_3d = min(sustainable, 2.0), "年度利润降幅超20%"
        elif profit_change is not None and profit_change < -0.10:
            sustainable, reason_3d = min(sustainable, 5.0), "年度利润下降10%+"
        revenues = m.get("revenue_values", [])
        if len(revenues) >= 2 and revenues[-1] < revenues[-2] * 0.90:
            sustainable, reason_3d = min(sustainable, 3.0), "营收同比下降10%+"
        if ocf_trend is not None and ocf_trend <= -0.70:
            sustainable, reason_3d = min(sustainable, 2.0), "经营现金流三年暴跌"
        if latest_severity >= 3:
            sustainable, reason_3d = min(sustainable, 2.0), latest_reason
        elif latest_severity == 2:
            sustainable, reason_3d = min(sustainable, 4.0), latest_reason
        elif latest_severity == 1:
            sustainable, reason_3d = min(sustainable, 5.0), latest_reason
        reason_3d = f"弱代理{sustainable:.1f}分:{reason_3d}"
        sustainable = 5.0
    scores["3d"], reasons["3d"] = sustainable, reason_3d

    pe = _safe_float(m.get("pe"))
    explicit_bubble = _verified_score(m, "type3_bubble_score")
    bubble_evidence_complete = explicit_bubble is not None
    industry = str(m.get("industry", ""))
    median_pe = None if industry == "DEFAULT" else _safe_float(_get_bench(benchmarks, industry, "median_pe"))
    if explicit_bubble is not None and 0 <= explicit_bubble <= 10:
        scores["3e"] = explicit_bubble
        reasons["3e"] = _evidence_reason(m, "type3_bubble_score", "产业股价泡沫证据不可追溯")
    elif pe is not None and pe > 0 and median_pe is not None and median_pe > 0:
        ratio = pe / median_pe
        scores["3e"] = 5.0
        reasons["3e"] = f"PE弱代理{ratio:.1f}倍;缺产业股价证据"
    else:
        scores["3e"], reasons["3e"] = 5.0, "产业股价泡沫证据缺失"

    moat_veto = moat_evidence_complete and scores["3a"] <= 3
    sustainability_veto = sustainability_evidence_complete and explicit_sustainability <= 3
    veto = moat_veto or sustainability_veto
    cap = 4.9 if bubble_evidence_complete and scores["3e"] <= 3 else None
    if moat_veto:
        reasons["_veto"] = "护城河证据不足"
    elif sustainability_veto:
        reasons["_veto"] = "增长不可持续"
    if cap is not None:
        reasons["_downgrade"] = "产业或股价泡沫风险"
    missing_dimensions: list[str] = []
    missing_dimension_keys: list[str] = []
    if not moat_evidence_complete:
        missing_dimensions.append("护城河")
        missing_dimension_keys.append("3a")
    if not growth_quality_evidence_complete:
        missing_dimensions.append("增长质量")
        missing_dimension_keys.append("3b")
    if roic_wacc_missing:
        missing_dimensions.append("投入回报")
        missing_dimension_keys.append("3c")
    if not sustainability_evidence_complete:
        missing_dimensions.append("增长持续性")
        missing_dimension_keys.append("3d")
    if not bubble_evidence_complete:
        missing_dimensions.append("泡沫")
        missing_dimension_keys.append("3e")
    if missing_dimensions:
        reasons["_missing"] = "缺" + "/".join(missing_dimensions) + "证据"
    return _finish(
        "type3",
        scores,
        reasons,
        veto=veto,
        total_cap=cap,
        evidence_complete=all(
            (
                moat_evidence_complete,
                growth_quality_evidence_complete,
                not roic_wacc_missing,
                sustainability_evidence_complete,
                bubble_evidence_complete,
            )
        ),
        missing_dimensions=missing_dimension_keys,
    )


def score_type4_long_runway(
    m: Mapping[str, Any],
    benchmarks: Mapping[str, Mapping[str, Any]],
    dcf_result: Optional[Mapping[str, Any]] = None,
    dcf_skip_classification: Optional[Mapping[str, Any]] = None,
):
    """情况四：长坡、厚雪、耐久护城河和双泡沫约束。"""
    if dcf_result is None:
        skip_outcome = _valuation_skip_outcome("type4", dcf_skip_classification)
        if skip_outcome is not None:
            return skip_outcome
    if str(m.get("industry", "")) in FINANCIAL_INDUSTRIES:
        return _not_applicable("type4", "金融机构暂无专属长坡厚雪模型")
    del benchmarks  # Type4 industry-bubble evidence must not fall back to peer PE.
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    valuation_evidence_valid = _valid_long_horizon_dcf_evidence(m, dcf_result)
    trend_growth = _safe_float(m.get("trend_growth"))
    explicit_runway = _verified_score(m, "runway_score")
    runway_complete = explicit_runway is not None
    if explicit_runway is not None and 0 <= explicit_runway <= 10:
        scores["4a"] = explicit_runway
        reasons["4a"] = _evidence_reason(m, "runway_score", "长坡证据不可追溯")
    elif trend_growth is not None and trend_growth < 0:
        scores["4a"] = _score_0_10(trend_growth, [(-0.30, 0), (-0.15, 1), (-0.05, 2), (0.0, 3)])
        reasons["4a"] = f"历史收缩代理{trend_growth:.1%}"
    elif trend_growth is None:
        scores["4a"], reasons["4a"] = 2.0, "坡长证据不足"
    else:
        # Historical company growth is only a runway proxy and is capped below
        # a fully evidenced qualitative assessment.
        scores["4a"] = min(
            6.0,
            _score_0_10(
                trend_growth,
                [(0.0, 3), (0.03, 4.5), (0.05, 5), (0.08, 6), (0.12, 6.5), (0.20, 7)],
            ),
        )
        reasons["4a"] = f"历史增速弱代理{trend_growth:.1%}"

    raw_margin_values = m.get("margin_history", [])
    raw_margin_values = list(raw_margin_values) if isinstance(raw_margin_values, (list, tuple)) else []
    parsed_margin_values = [_safe_float(item) for item in raw_margin_values]
    margin_values = [float(value) for value in parsed_margin_values if value is not None]
    raw_margin_years = m.get("margin_years", [])
    margin_years = list(raw_margin_years) if isinstance(raw_margin_years, (list, tuple)) else []
    net_margin_history_complete = len(margin_values) == len(raw_margin_values) and _aligned_current_consecutive(
        m, raw_margin_values, margin_years, 3
    )
    margin = float(median(margin_values[-3:])) if net_margin_history_complete else _safe_float(m.get("net_margin"))
    if margin is None:
        margin = _safe_float(m.get("margin_median_hist"))
    margin_score = (
        _score_0_10(margin, [(0.0, 1.0), (0.05, 4.0), (0.12, 6.0), (0.20, 8.0), (0.40, 10.0)])
        if margin is not None
        else 0.0
    )
    raw_gross_values = m.get("gross_margin_history", [])
    raw_gross_values = list(raw_gross_values) if isinstance(raw_gross_values, (list, tuple)) else []
    parsed_gross_values = [_safe_float(item) for item in raw_gross_values]
    gross_values = [float(value) for value in parsed_gross_values if value is not None]
    raw_gross_years = m.get("gross_margin_years", [])
    gross_years = list(raw_gross_years) if isinstance(raw_gross_years, (list, tuple)) else []
    gross_history_complete = len(gross_values) == len(raw_gross_values) and _aligned_current_consecutive(
        m, raw_gross_values, gross_years, 3
    )
    gross_cv = (
        float(np.std(gross_values[-3:]) / abs(np.mean(gross_values[-3:])))
        if gross_history_complete and abs(float(np.mean(gross_values[-3:]))) > 1e-12
        else _safe_float(m.get("gross_margin_cv"))
    )
    gross_samples = 3 if gross_history_complete else int(_safe_float(m.get("gross_margin_samples")) or 0)
    if gross_cv is None or gross_samples < 3:
        gross_score = 0.0
    elif gross_cv <= 0.05:
        gross_score = 10.0
    elif gross_cv <= 0.10:
        gross_score = 8.0
    elif gross_cv <= 0.20:
        gross_score = 6.0
    elif gross_cv <= 0.30:
        gross_score = 4.0
    else:
        gross_score = 2.0
    if str(m.get("gross_margin_trend") or "") == "decreasing":
        gross_score = max(0.0, gross_score - 2.0)

    raw_fcf_history = m.get("fcf_history", [])
    raw_profit_history = m.get("net_profit_history", [])
    raw_revenue_history = m.get("revenue_values", [])
    raw_fcf_values = list(raw_fcf_history) if isinstance(raw_fcf_history, (list, tuple)) else []
    raw_profit_values = list(raw_profit_history) if isinstance(raw_profit_history, (list, tuple)) else []
    raw_revenue_values = list(raw_revenue_history) if isinstance(raw_revenue_history, (list, tuple)) else []
    fcf_values = [_safe_float(value) for value in raw_fcf_values]
    profit_values = [_safe_float(value) for value in raw_profit_values]
    revenue_values = [_safe_float(value) for value in raw_revenue_values]
    raw_fcf_years = m.get("fcf_years", [])
    raw_profit_years = m.get("net_profit_years", [])
    raw_revenue_years = m.get("revenue_years", [])
    fcf_years = list(raw_fcf_years) if isinstance(raw_fcf_years, (list, tuple)) else []
    profit_years = list(raw_profit_years) if isinstance(raw_profit_years, (list, tuple)) else []
    revenue_years = list(raw_revenue_years) if isinstance(raw_revenue_years, (list, tuple)) else []
    fcf_map = {year: value for year, value in zip(fcf_years, fcf_values) if value is not None}
    profit_map = {year: value for year, value in zip(profit_years, profit_values) if value is not None}
    revenue_map = {year: value for year, value in zip(revenue_years, revenue_values) if value is not None}
    fcf_margin_years = sorted(set(fcf_map) & set(revenue_map))[-3:]
    fcf_margin_history_complete = (
        _aligned_current_consecutive(m, raw_fcf_values, fcf_years, 3)
        and _aligned_current_consecutive(m, raw_revenue_values, revenue_years, 3)
        and len(fcf_margin_years) == 3
        and all(current - prior == 1 for prior, current in zip(fcf_margin_years, fcf_margin_years[1:]))
        and fcf_margin_years[-1] == _latest_complete_financial_year(m)
        and all(revenue_map[year] > 0 for year in fcf_margin_years)
    )
    fcf_margins = (
        [fcf_map[year] / revenue_map[year] for year in fcf_margin_years] if fcf_margin_history_complete else []
    )
    fcf_margin = float(median(fcf_margins)) if fcf_margins else None
    cash_margin_score = (
        _score_0_10(fcf_margin, [(0.0, 1.0), (0.03, 4.0), (0.08, 6.0), (0.15, 8.0), (0.30, 10.0)])
        if fcf_margin is not None
        else 0.0
    )
    cash_years = sorted(set(fcf_map) & set(profit_map))[-3:]
    cash_history_complete = (
        _aligned_current_consecutive(m, raw_fcf_values, fcf_years, 3)
        and _aligned_current_consecutive(m, raw_profit_values, profit_years, 3)
        and len(cash_years) == 3
        and cash_years[-1] == _latest_complete_financial_year(m)
        and all(current - prior == 1 for prior, current in zip(cash_years, cash_years[1:]))
    )
    total_profit = sum(profit_map[year] for year in cash_years) if cash_history_complete else 0.0
    total_fcf = sum(fcf_map[year] for year in cash_years) if cash_history_complete else 0.0
    cash_conversion = total_fcf / total_profit if total_profit > 0 else None
    conversion_score = (
        _score_0_10(cash_conversion, [(0.0, 1.0), (0.50, 4.0), (0.80, 6.0), (1.00, 8.0), (1.50, 10.0)])
        if cash_conversion is not None
        else 0.0
    )
    cash_score = (cash_margin_score + conversion_score) / 2.0

    raw_roic_history = m.get("indicator_roic_history", [])
    raw_roic_values = list(raw_roic_history) if isinstance(raw_roic_history, (list, tuple)) else []
    roic_values = [_safe_float(item) for item in raw_roic_values]
    raw_roic_years = m.get("indicator_roic_years", [])
    roic_years = list(raw_roic_years) if isinstance(raw_roic_years, (list, tuple)) else []
    roic_map = {year: value for year, value in zip(roic_years, roic_values) if value is not None}
    recent_roic_years = sorted(roic_map)[-3:]
    roic_history_complete = (
        _aligned_current_consecutive(m, raw_roic_values, roic_years, 3)
        and len(recent_roic_years) == 3
        and recent_roic_years[-1] == _latest_complete_financial_year(m)
        and all(current - prior == 1 for prior, current in zip(recent_roic_years, recent_roic_years[1:]))
    )
    roic_history = [roic_map[year] for year in recent_roic_years] if roic_history_complete else []
    wacc = _safe_float(m.get("wacc"))
    roic_value = float(median(roic_history)) if roic_history_complete else _safe_float(m.get("roic"))
    roic_wacc_basis_valid = m.get("roic_wacc_basis") in {
        "NOPAT/平均投入资本代理",
        "Eastmoney年度ROIC/公司资本结构WACC",
    }
    roic_spread = roic_value - wacc if roic_value is not None and wacc is not None and roic_wacc_basis_valid else None
    roic_score = (
        _score_0_10(roic_spread, [(0.0, 1.0), (0.02, 3.0), (0.05, 5.0), (0.10, 7.0), (0.20, 9.0), (0.30, 10.0)])
        if roic_spread is not None
        else 0.0
    )
    if roic_history_complete and wacc is not None:
        positive_spreads = sum(value > wacc for value in roic_history)
        if positive_spreads == 3:
            roic_score = min(10.0, roic_score + 1.0)
        elif positive_spreads < 2 or roic_history[-1] <= wacc:
            roic_score = min(roic_score, 3.0)

    snow_complete = (
        net_margin_history_complete
        and gross_history_complete
        and fcf_margin_history_complete
        and cash_history_complete
        and cash_conversion is not None
        and roic_history_complete
        and wacc is not None
        and roic_wacc_basis_valid
    )
    scores["4b"] = round((margin_score + gross_score + cash_score + roic_score) / 4.0, 1)
    if (margin is not None and margin <= 0) or (cash_history_complete and total_fcf <= 0):
        scores["4b"] = min(scores["4b"], 2.0)
    reasons["4b"] = f"净{margin_score:.0f}/毛{gross_score:.0f}/现{cash_score:.0f}/ROIC{roic_score:.0f}"
    profit_change = _current_annual_change(m, "net_profit_history", "net_profit_years")
    if profit_change is not None and profit_change <= -0.50:
        scores["4b"], reasons["4b"] = min(scores["4b"], 2.0), "年度利润崩塌"
    latest_severity, latest_reason = _latest_period_deterioration(m)
    if latest_severity >= 3:
        scores["4b"], reasons["4b"] = min(scores["4b"], 2.0), latest_reason
    elif latest_severity == 2:
        scores["4b"], reasons["4b"] = min(scores["4b"], 4.0), latest_reason
    elif latest_severity == 1:
        scores["4b"], reasons["4b"] = min(scores["4b"], 6.0), latest_reason

    consistency = _safe_float(m.get("growth_consistency"))
    debt = _safe_float(m.get("debt_ratio"))
    roe = _safe_float(m.get("roe"))
    explicit_moat = _verified_score(m, "moat_durability_score")
    if explicit_moat is None:
        explicit_moat = _verified_score(m, "moat_score")
    moat_complete = explicit_moat is not None
    if explicit_moat is not None and 0 <= explicit_moat <= 10:
        scores["4c"] = explicit_moat
        evidence_key = (
            "moat_durability_score" if _verified_score(m, "moat_durability_score") is not None else "moat_score"
        )
        reasons["4c"] = _evidence_reason(m, evidence_key, "护城河证据不可追溯")
    else:
        moat_count = sum(
            (
                roe is not None and roe >= 0.15,
                margin is not None and margin >= 0.06,
                consistency is not None and consistency < 0.80,
                debt is not None and debt < 0.60,
            )
        )
        proxy_moat_score = {0: 2.0, 1: 4.0, 2: 5.0, 3: 6.0, 4: 6.0}[moat_count]
        scores["4c"] = 5.0
        reasons["4c"] = f"耐久财务弱代理{proxy_moat_score:.0f}分"

    price = _safe_float(m.get("price"))
    dcf_points = dcf_result.get("dcf_10y_points", {}) if valuation_evidence_valid else {}
    neutral = dcf_points.get("neutral", {}) if isinstance(dcf_points, Mapping) else {}
    neutral_lower = _safe_float(neutral.get("lower")) if isinstance(neutral, Mapping) else None
    neutral_upper = _safe_float(neutral.get("upper")) if isinstance(neutral, Mapping) else None
    neutral_value = (
        (neutral_lower + neutral_upper) / 2.0
        if neutral_lower is not None and neutral_upper is not None and neutral_lower > 0 and neutral_upper > 0
        else None
    )
    if price is not None and price > 0 and neutral_value is not None:
        terminal_ratio = price / neutral_value
        scores["4d"] = _score_0_10(
            terminal_ratio,
            [(0.50, 10), (0.75, 8), (1.00, 6), (1.25, 4), (1.75, 2), (2.50, 0)],
        )
        reasons["4d"] = f"价格/10年中性终局{terminal_ratio:.1f}倍"
    else:
        scores["4d"], reasons["4d"] = 2.0, "缺终局折现价值证据"

    explicit_bubble = _verified_score(m, "industry_bubble_score")
    bubble_complete = explicit_bubble is not None
    if explicit_bubble is not None and 0 <= explicit_bubble <= 10:
        scores["4e"] = explicit_bubble
        reasons["4e"] = _evidence_reason(m, "industry_bubble_score", "泡沫证据不可追溯")
    else:
        # Missing supply/demand evidence is unknown, not a confirmed bubble.
        # Keep a neutral diagnostic score and make the framework incomplete.
        scores["4e"], reasons["4e"] = 5.0, "产业泡沫证据缺失"

    implied_years = _implied_price_growth_years(dcf_result, price) if valuation_evidence_valid else None
    if implied_years is not None:
        scores["4f"] = _score_implied_growth_years(implied_years)
        reasons["4f"] = "乐观上沿透支30年+" if implied_years > 30 else f"乐观上沿透支{implied_years}年"
        reasons["_4f_formula"] = "opt_upper_v1"
    else:
        scores["4f"], reasons["4f"] = 2.0, "缺隐含增长年数证据"

    valuation_missing = not valuation_evidence_valid or neutral_value is None or implied_years is None
    evidence_complete = (
        runway_complete and snow_complete and moat_complete and bubble_complete and not valuation_missing
    )
    # 补丁6只规定4c以及4e+4f为一票否决。公司收缩、最新期恶化和
    # 估值缺失可以降低对应子项，但不能再偷偷添加第三、第四个否决项。
    moat_veto = moat_complete and scores["4c"] <= 3
    double_bubble_veto = bubble_complete and implied_years is not None and scores["4e"] <= 3 and scores["4f"] <= 3
    veto = moat_veto or double_bubble_veto
    if moat_veto:
        reasons["_veto"] = "持久护城河不足"
    elif double_bubble_veto:
        reasons["_veto"] = "产业与股价双泡沫"
    missing_contract = (
        (runway_complete, "4a", "坡长"),
        (snow_complete, "4b", "厚雪"),
        (moat_complete, "4c", "护城河"),
        (not valuation_missing, "4d", "10年估值"),
        (bubble_complete, "4e", "产业泡沫"),
        (not valuation_missing, "4f", "10年估值"),
    )
    missing_dimension_keys = [key for complete, key, _label in missing_contract if not complete]
    if not evidence_complete:
        missing_dimensions = list(dict.fromkeys(label for complete, _key, label in missing_contract if not complete))
        reasons["_missing"] = "缺" + "/".join(missing_dimensions) + "证据"
    return _finish(
        "type4",
        scores,
        reasons,
        veto=veto,
        evidence_complete=evidence_complete,
        missing_dimensions=missing_dimension_keys,
    )


def _is_strict_recovery(values: list[float], years: Any = None) -> bool:
    """至少连续两个年度改善，且累计改善有经济意义。"""
    cleaned = [_safe_float(value) for value in values]
    if len(cleaned) < 4 or any(value is None for value in cleaned[-4:]):
        return False
    if years is not None and not _aligned_consecutive(values, years, 4):
        return False
    prior, first, middle, latest = cleaned[-4:]  # type: ignore[misc]
    # 回升必须始于此前的下降/谷底；单调增长公司不是“周期复苏”。
    if not prior > first:
        return False
    if not (first < middle < latest):
        return False
    if latest >= prior:
        return False
    scale = max(abs(first), abs(middle), 1.0)
    return latest - first >= scale * 0.30


def _has_cycle_history(values: list[float], years: Any = None) -> bool:
    """要求历史同时出现下降、上升和足够振幅，排除普通成长股。"""
    raw = [_safe_float(item) for item in values]
    if len(raw) < 4 or any(value is None for value in raw):
        return False
    cleaned = [float(value) for value in raw if value is not None]
    if years is not None:
        if not isinstance(years, (list, tuple)) or len(years) != len(cleaned):
            return False
        points: list[tuple[int, float]] = []
        for raw_year, value in zip(years, cleaned):
            if isinstance(raw_year, (bool, np.bool_)) or not isinstance(raw_year, (int, np.integer)):
                return False
            points.append((int(raw_year), value))
        points.sort()
        if len({year for year, _ in points}) != len(points):
            return False
        suffix = [points[-1]]
        for point in reversed(points[:-1]):
            if suffix[0][0] - point[0] != 1:
                break
            suffix.insert(0, point)
        if len(suffix) < 4:
            return False
        cleaned = [value for _, value in suffix]
    changes = [current - prior for prior, current in zip(cleaned, cleaned[1:])]
    if not any(change < 0 for change in changes) or not any(change > 0 for change in changes):
        return False
    scale = max(abs(float(median(cleaned))), 1.0)
    return (max(cleaned) - min(cleaned)) / scale >= 0.50


def _score_type5_financial(m: Mapping[str, Any], benchmarks: Mapping[str, Mapping[str, Any]]):
    industry = str(m.get("industry") or "")
    if industry not in SUPPORTED_FINANCIAL_INDUSTRIES:
        return _not_applicable("type5", "其他金融暂无专属周期模型")
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    pb = _safe_float(m.get("pb"))
    profit_points: list[tuple[int, float]] = []
    raw_profit_years = m.get("net_profit_years", [])
    raw_profit_values = m.get("net_profit_history", [])
    if _aligned_current_consecutive(m, raw_profit_values, raw_profit_years, 4):
        for raw_year, raw_value in zip(raw_profit_years, raw_profit_values):
            parsed = _safe_float(raw_value)
            if isinstance(raw_year, (int, np.integer)) and parsed is not None:
                profit_points.append((int(raw_year), parsed))
    profit_points.sort()

    if industry == "BANK":
        driver_points = _financial_metric_points(m, "net_interest_margin")
        npl_pair = _financial_metric_pair(m, "nonperforming_loan_ratio")
        recent_pair = _financial_metric_pair(m, "net_interest_margin")
        values = [value for _, value in driver_points]
        changes = [current - prior for prior, current in zip(values, values[1:])]
        cycle_history = (
            _aligned_current_consecutive(
                m,
                m.get("net_interest_margin_history", []),
                m.get("net_interest_margin_years", []),
                4,
            )
            and len(values) >= 4
            and any(change < 0 for change in changes)
            and any(change > 0 for change in changes)
            and max(values) - min(values) >= max(abs(float(median(values))) * 0.10, 0.001)
        )
        recovering = bool(
            cycle_history
            and recent_pair is not None
            and recent_pair[1] > recent_pair[0]
            and npl_pair is not None
            and npl_pair[1] <= npl_pair[0]
        )
        trough = bool(values and values[-1] <= float(np.quantile(values, 0.25)))
        driver_label = "净息差"
    elif industry == "INSURANCE":
        driver_points = _financial_metric_points(m, "new_business_value")
        values = [value for _, value in driver_points]
        years = [year for year, _ in driver_points]
        cycle_history = bool(
            _aligned_current_consecutive(
                m,
                m.get("new_business_value_history", []),
                m.get("new_business_value_years", []),
                4,
            )
            and _has_cycle_history(values, years)
        )
        recovering = bool(cycle_history and _is_strict_recovery(values, years))
        positive_values = [value for value in values if value > 0]
        trough = bool(positive_values and values[-1] <= float(median(positive_values)) * 0.70)
        driver_label = "新业务价值"
    else:
        driver_points = profit_points
        values = [value for _, value in driver_points]
        years = [year for year, _ in driver_points]
        cycle_history = _has_cycle_history(values, years)
        recovering = bool(cycle_history and _is_strict_recovery(values, years))
        positive_values = [value for value in values if value > 0]
        trough = bool(positive_values and values[-1] <= max(positive_values) * 0.50)
        driver_label = "归母利润"

    if recovering:
        scores["5a"], reasons["5a"] = 9.0, f"{driver_label}连续回升"
    elif trough:
        scores["5a"], reasons["5a"] = 6.0, f"{driver_label}处周期低位"
    elif cycle_history:
        scores["5a"], reasons["5a"] = 4.0, f"{driver_label}周期未反转"
    else:
        scores["5a"], reasons["5a"] = 3.0, f"{driver_label}周期证据不足"

    flags = 0
    flags += int(pb is not None and pb < 1.0)
    flags += int(trough)
    flags += int(recovering)
    if industry == "BANK":
        npl = _safe_float(m.get("nonperforming_loan_ratio"))
        provision = _safe_float(m.get("loan_provision_coverage_proxy"))
        flags += int(npl is not None and npl <= 0.02)
        flags += int(provision is not None and provision >= 1.5)
    elif industry == "INSURANCE":
        solvency = _safe_float(m.get("solvency_adequacy_ratio"))
        surrender = _safe_float(m.get("life_surrender_rate"))
        flags += int(solvency is not None and solvency >= 1.5)
        flags += int(surrender is not None and surrender <= 0.04)
    else:
        risk = _safe_float(m.get("risk_coverage_ratio"))
        liquidity = _safe_float(m.get("liquidity_coverage_ratio"))
        flags += int(risk is not None and risk >= 1.5)
        flags += int(liquidity is not None and liquidity >= 1.2)
    scores["5b"] = 9.0 if flags >= 5 else 7.0 if flags >= 4 else 5.0 if flags >= 2 else 3.0
    reasons["5b"] = f"金融底部信号{flags}项"

    regulatory_points, regulatory_complete, regulatory_reason = _financial_regulatory_trap_points(m)
    scores["5c"] = float(sum(regulatory_points)) if regulatory_complete else 0.0
    reasons["5c"] = regulatory_reason if regulatory_complete else "金融监管字段缺失"
    thresholds = FINANCIAL_REGULATORY_THRESHOLDS[industry]
    hard_regulatory_breach = False
    if industry == "BANK":
        capital = _safe_float(m.get("capital_adequacy_ratio"))
        tier1 = _safe_float(m.get("tier1_capital_adequacy_ratio"))
        hard_regulatory_breach = bool(
            capital is not None
            and tier1 is not None
            and (capital < thresholds["capital_min"] or tier1 < thresholds["tier1_min"])
        )
    elif industry == "INSURANCE":
        solvency = _safe_float(m.get("solvency_adequacy_ratio"))
        hard_regulatory_breach = bool(solvency is not None and solvency < thresholds["solvency_min"])
    else:
        regulatory_values = (
            (_safe_float(m.get("risk_coverage_ratio")), thresholds["risk_coverage_min"]),
            (_safe_float(m.get("capital_leverage_ratio")), thresholds["capital_leverage_min"]),
            (_safe_float(m.get("liquidity_coverage_ratio")), thresholds["liquidity_coverage_min"]),
            (_safe_float(m.get("net_stable_funding_ratio")), thresholds["net_stable_funding_min"]),
        )
        hard_regulatory_breach = all(value is not None for value, _minimum in regulatory_values) and any(
            value < minimum for value, minimum in regulatory_values if value is not None
        )
    if hard_regulatory_breach:
        scores["5c"], reasons["5c"] = min(scores["5c"], 2.0), "低于监管最低线"

    if len(values) >= 4:
        scale = max(abs(float(median(values))), 1e-12)
        elasticity = (max(values) - min(values)) / scale
        if industry == "BANK":
            scores["5d"] = (
                9.0 if elasticity >= 0.40 else 7.0 if elasticity >= 0.25 else 5.0 if elasticity >= 0.10 else 2.0
            )
        else:
            scores["5d"] = (
                9.0 if elasticity >= 2.0 else 7.0 if elasticity >= 1.0 else 5.0 if elasticity >= 0.50 else 2.0
            )
        reasons["5d"] = f"金融周期振幅{elasticity:.1f}倍"
    else:
        scores["5d"], reasons["5d"] = 0.0, "金融周期历史不足"

    median_pb = _safe_float(_get_bench(benchmarks, industry, "median_pb"))
    if pb is not None and pb > 0 and median_pb is not None and median_pb > 0:
        relative_pb = pb / median_pb
        scores["5e"] = _score_0_10(
            relative_pb,
            [(0.50, 9), (0.75, 8), (1.00, 6), (1.25, 5), (1.50, 3), (2.00, 1)],
        )
        reasons["5e"] = f"金融PB/同行{relative_pb:.1f}倍"
    else:
        scores["5e"], reasons["5e"] = 0.0, "金融PB同行证据不足"

    evidence_complete = cycle_history and regulatory_complete and pb is not None and median_pb is not None
    veto = evidence_complete and (scores["5a"] <= 3 or scores["5c"] <= 3)
    forced = scores["5a"] >= 7 and scores["5c"] >= 5
    if not cycle_history:
        reasons["_missing"] = "金融周期历史不足"
    elif not regulatory_complete:
        reasons["_missing"] = "金融监管证据不足"
    if scores["5a"] <= 3 and evidence_complete:
        reasons["_veto"] = "金融周期阶段不符合"
    elif scores["5c"] <= 3 and evidence_complete:
        reasons["_veto"] = "金融监管缓冲不足"
    if not forced:
        reasons["_condition"] = "须周期回升且监管稳健"
    return _finish(
        "type5",
        scores,
        reasons,
        veto=veto,
        extra_condition=forced,
        evidence_complete=evidence_complete,
    )


def _score_type5_legacy_counter_cyclical(
    m: Mapping[str, Any],
    benchmarks: Mapping[str, Mapping[str, Any]],
    dcf_result: Optional[Mapping[str, Any]] = None,
):
    """Pre-2026-07-17 Type5 implementation, retained temporarily for migration review.

    The public scorer below implements the current Patch6 appendix.  Keeping
    this private function for one release cycle lets old audit payloads remain
    inspectable, but it is deliberately never called by production code.
    """
    if str(m.get("industry", "")) in FINANCIAL_INDUSTRIES:
        return _score_type5_financial(m, benchmarks)
    industry = str(m.get("industry", ""))
    explicit_cyclicality = _verified_score(m, "cyclical_industry_score")
    if industry not in STRONG_CYCLICAL_INDUSTRIES and (explicit_cyclicality is None or explicit_cyclicality < 7):
        return _not_applicable("type5", "缺强周期产业证据")
    del benchmarks  # 本类型当前只使用公司周期与资产口径。
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    pe, pb, roe = (_safe_float(m.get("pe")), _safe_float(m.get("pb")), _safe_float(m.get("roe")))
    profits = [value for value in (_safe_float(v) for v in m.get("net_profit_history", [])) if value is not None]
    profit_dates = m.get("net_profit_years")
    positive_profit_years = sum(value > 0 for value in profits)
    cycle_history = _has_cycle_history(profits, profit_dates)
    history_complete = len(profits) >= 4 and positive_profit_years >= 3 and cycle_history
    structural_veto = len(profits) < 4 or positive_profit_years < 3
    if not cycle_history:
        structural_veto = True
        reasons["_missing"] = "缺少强周期波动证据"
    if structural_veto:
        reasons.setdefault("_missing", "历史盈利年数不足")

    positive_profits = [value for value in profits if value > 0]
    cyclicality = (
        max(positive_profits) / min(positive_profits)
        if len(positive_profits) >= 3 and min(positive_profits) > 0
        else None
    )
    annual_recovering = (
        cycle_history
        and _is_strict_recovery(profits, profit_dates)
        and ((cyclicality is not None and cyclicality >= 3.0) or (len(profits) >= 3 and profits[-3] <= 0 < profits[-1]))
    )
    latest_severity, latest_reason = _latest_period_deterioration(m)
    recovering = annual_recovering and latest_severity == 0

    margin_now = _safe_float(m.get("net_margin"))
    margin_median = _safe_float(m.get("margin_median_hist"))
    trough = False
    if cycle_history and profits and max(profits) > 0:
        trough = float(np.mean(profits[-3:])) / max(profits) < 0.5
    if cycle_history and pe is not None and (pe <= 0 or pe > 50):
        trough = True
    if cycle_history and margin_now is not None and margin_median is not None and margin_median > 0:
        trough = trough or margin_now < margin_median * 0.60

    if recovering:
        scores["5a"], reasons["5a"] = 9.0, "利润连续两年回升"
    elif trough and pb is not None and pb < 1.0:
        scores["5a"], reasons["5a"] = 8.0, "盈利低谷且破净"
    elif trough:
        scores["5a"], reasons["5a"] = 6.0, "盈利低谷待确认"
    else:
        scores["5a"], reasons["5a"] = 3.0, "未处周期低迷后期"

    long_cagr = _safe_float(m.get("cagr_5yr"))
    if long_cagr is not None and long_cagr < -0.05:
        scores["5a"], reasons["5a"] = 2.0, "营收长期衰退"
        structural_veto = True
    if roe is not None and roe < 0.03:
        scores["5a"], reasons["5a"] = min(scores["5a"], 2.0), f"ROE仅{roe:.1%}"
        structural_veto = True
    elif roe is not None and roe < 0.08:
        scores["5a"], reasons["5a"] = min(scores["5a"], 6.0), f"ROE仅{roe:.1%}"
    if structural_veto:
        scores["5a"] = min(scores["5a"], 3.0)
    if latest_severity >= 3:
        scores["5a"], reasons["5a"] = min(scores["5a"], 2.0), latest_reason
    elif latest_severity == 2:
        scores["5a"], reasons["5a"] = min(scores["5a"], 4.0), latest_reason
    elif latest_severity == 1:
        scores["5a"], reasons["5a"] = min(scores["5a"], 6.0), latest_reason

    capex_history = [value for value in (_safe_float(v) for v in m.get("capex_history", [])) if value is not None]
    capex_contracting = (
        _aligned_consecutive(capex_history, m.get("capex_years"), 3)
        and capex_history[-3] > capex_history[-2] > capex_history[-1]
    )
    flags = sum(
        (
            pb is not None and pb < 1.2,
            trough,
            margin_now is not None and margin_median is not None and margin_now < margin_median * 0.50,
            pe is not None and pe > 100,
            recovering,
            capex_contracting,
        )
    )
    scores["5b"] = 10.0 if flags >= 5 else 8.0 if flags >= 4 else 6.0 if flags >= 2 else 4.0 if flags == 1 else 2.0
    reasons["5b"] = f"量化底部信号{flags}项"
    if scores["5a"] <= 3:
        scores["5b"] = min(scores["5b"], 4.0)

    debt = _safe_float(m.get("debt_ratio"))
    funds = _safe_float(m.get("monetary_funds"))
    assets = _safe_float(m.get("total_assets"))
    dcf_evidence_valid = _valid_nonfinancial_dcf_evidence(m, dcf_result)
    fcf, latest_fcf, fcf_basis = _traceable_normalised_fcf(
        m,
        dcf_result,
        result_valid=dcf_evidence_valid,
        allow_history_fallback=True,
    )
    survival_signals = sum(
        (
            debt is not None and debt < 0.50,
            funds is not None and assets is not None and assets > 0 and funds / assets >= 0.10,
            fcf is not None and fcf >= 0,
            positive_profit_years >= 3,
        )
    )
    scores["5c"] = {0: 2.0, 1: 4.0, 2: 6.0, 3: 8.0, 4: 9.5}[survival_signals]
    reasons["5c"] = (
        f"归一{_format_rmb(fcf)};最新{_format_rmb(latest_fcf)};抗周期{survival_signals}项"
        if fcf is not None and latest_fcf is not None
        else f"抗周期{survival_signals}项;{fcf_basis}"
    )
    if latest_severity >= 3:
        scores["5c"], reasons["5c"] = min(scores["5c"], 3.0), latest_reason
    elif latest_severity == 2:
        scores["5c"], reasons["5c"] = min(scores["5c"], 5.0), latest_reason

    if cyclicality is None:
        scores["5d"], reasons["5d"] = 3.0, "弹性历史不足"
    elif cyclicality >= 5:
        scores["5d"], reasons["5d"] = 9.0, f"峰谷弹性{cyclicality:.1f}倍"
    elif cyclicality >= 3:
        scores["5d"], reasons["5d"] = 7.0, f"峰谷弹性{cyclicality:.1f}倍"
    elif cyclicality >= 2:
        scores["5d"], reasons["5d"] = 5.0, f"峰谷弹性{cyclicality:.1f}倍"
    else:
        scores["5d"], reasons["5d"] = 2.0, "周期弹性不足"

    market_cap = _safe_float(m.get("market_cap"))
    fcf_yield = fcf / market_cap if fcf is not None and market_cap is not None and market_cap > 0 else None
    if pb is not None and 0 < pb < 1.0 and fcf_yield is not None and fcf_yield > 0.08:
        scores["5e"], reasons["5e"] = 10.0, f"归一FCF收益率{fcf_yield:.1%};最新{_format_rmb(latest_fcf)}"
    elif pb is not None and 0 < pb < 1.0:
        scores["5e"], reasons["5e"] = 9.0, f"PB{pb:.2f}破净"
    elif pb is not None and pb < 1.5:
        scores["5e"], reasons["5e"] = 7.0, f"PB{pb:.2f}低位"
    elif pb is not None and pb < 2.5:
        scores["5e"], reasons["5e"] = 5.0, f"PB{pb:.2f}中性"
    else:
        scores["5e"], reasons["5e"] = 2.0, "周期估值不便宜"

    # 补丁6的硬规则只有5a<=3、5c<=3，以及5a>=7且5c>=5的
    # 可买入门槛。最新期恶化已经进入5a/5c分数，不再重复添加否决项。
    veto = history_complete and (scores["5a"] <= 3 or scores["5c"] <= 3)
    forced = scores["5a"] >= 7 and scores["5c"] >= 5
    if scores["5a"] <= 3:
        reasons["_veto"] = "周期阶段不符合"
    elif scores["5c"] <= 3:
        reasons["_veto"] = "公司无法熬过低谷"
    if not forced:
        reasons["_condition"] = "须满足5a≥7且5c≥5"
    return _finish(
        "type5",
        scores,
        reasons,
        veto=veto,
        extra_condition=forced,
        evidence_complete=history_complete,
    )


def _type5_cycle_profit_history(m: Mapping[str, Any]) -> tuple[list[float], list[int]]:
    """Return only fully dated annual profit observations for normalisation."""
    raw_years = m.get("net_profit_years")
    raw_values = m.get("net_profit_history")
    if (
        not isinstance(raw_years, (list, tuple))
        or not isinstance(raw_values, (list, tuple))
        or len(raw_years) != len(raw_values)
    ):
        return [], []
    points: list[tuple[int, float]] = []
    for raw_year, raw_value in zip(raw_years, raw_values):
        value = _safe_float(raw_value)
        if isinstance(raw_year, (bool, np.bool_)) or not isinstance(raw_year, (int, np.integer)) or value is None:
            return [], []
        points.append((int(raw_year), value))
    points.sort()
    if len({year for year, _ in points}) != len(points):
        return [], []
    return [value for _, value in points], [year for year, _ in points]


def _type5_external_score(m: Mapping[str, Any], key: str) -> tuple[Optional[float], Optional[str]]:
    """Read one independently validated primary Type5 assessment.

    Generic score metadata is not a replayable Type5 model.  Serialized input
    therefore cannot promote an arbitrary ``derived_proxy`` value into one of
    the five decision dimensions.  A future structured Type5 source adapter
    may attach the process-local token only after validating its raw facts.
    """
    if (
        m.get("_type5_external_validation_token") is not _TYPE5_EXTERNAL_VALIDATION_TOKEN
        or m.get(f"{key}_evidence_level") != "primary"
    ):
        return None, None
    score = _verified_score(m, key)
    if score is None:
        return None, None
    return score, _evidence_reason(m, key, "已登记的外部证据")


def _type5_normalised_pe(m: Mapping[str, Any]) -> tuple[Optional[float], int]:
    """Use 5–10 consecutive annual profits, never the current single-year PE."""
    profits, years = _type5_cycle_profit_history(m)
    if len(profits) < 5 or not _aligned_current_consecutive(m, profits, years, 5):
        return None, 0
    selected_points = list(zip(years, profits))[-10:]
    consecutive_suffix = [selected_points[-1]]
    for point in reversed(selected_points[:-1]):
        if consecutive_suffix[0][0] - point[0] != 1:
            break
        consecutive_suffix.insert(0, point)
    if len(consecutive_suffix) < 5:
        return None, 0
    selected = [value for _, value in consecutive_suffix]
    normalised_profit = float(np.mean(selected))
    market_cap = _safe_float(m.get("market_cap"))
    if normalised_profit <= 0 or market_cap is None or market_cap <= 0:
        return None, len(selected)
    return market_cap / normalised_profit, len(selected)


def _type5_contract_number(value: Any) -> Optional[float]:
    """Parse one history-contract number without accepting booleans."""
    if isinstance(value, (bool, np.bool_)):
        return None
    return _safe_float(value)


def _type5_pb_bottom_score(pb_percentile: Any, current_pb: Any) -> Optional[float]:
    """Score a conjunctive five-year PB signal with exact public boundaries."""
    percentile = _type5_contract_number(pb_percentile)
    pb = _type5_contract_number(current_pb)
    if percentile is None or pb is None or not 0 <= percentile <= 1 or pb <= 0:
        return None
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
    absolute_score = 10.0 if pb <= 1.0 else 8.0 if pb <= 1.2 else 6.0 if pb <= 1.5 else 4.0 if pb <= 2.0 else 2.0
    # A low percentile at an objectively expensive PB (or the reverse) is not
    # a complete valuation-bottom signal.
    return min(percentile_score, absolute_score)


def _type5_pb_history_inputs(
    m: Mapping[str, Any],
    history_evidence: Optional[Mapping[str, Any]],
) -> Optional[tuple[float, float]]:
    """Validate and bind the shared five-year valuation-history record."""
    if not isinstance(history_evidence, Mapping):
        return None
    code = _canonical_evidence_code(m.get("code"))
    reference_date = _evidence_reference_date(m.get("source_trade_date"))
    if (
        not code
        or reference_date is None
        or history_evidence.get("model_id") != LONG_HORIZON_HISTORY_MODEL_ID
        or _canonical_evidence_code(history_evidence.get("code")) != code
        or history_evidence.get("as_of") != reference_date.isoformat()
    ):
        return None
    valuation = history_evidence.get("valuation_history")
    if not isinstance(valuation, Mapping) or valuation.get("available") is not True:
        return None
    observations = valuation.get("pb_observations")
    if (
        isinstance(observations, (bool, np.bool_))
        or not isinstance(observations, (int, np.integer))
        or int(observations) < TYPE5_PB_MIN_OBSERVATIONS
        or valuation.get("window_years") != 5
    ):
        return None
    span_days = _type5_contract_number(valuation.get("span_days"))
    start_delay = _type5_contract_number(valuation.get("start_delay_days"))
    end_date = _evidence_reference_date(valuation.get("end_date"))
    formula = valuation.get("formula")
    if (
        span_days is None
        or span_days < TYPE5_HISTORY_MIN_SPAN_DAYS
        or start_delay is None
        or not 0 <= start_delay <= TYPE5_HISTORY_MAX_START_DELAY_DAYS
        or end_date is None
        or not 0 <= (reference_date - end_date).days <= TYPE5_HISTORY_MAX_LATEST_AGE_DAYS
        or not isinstance(formula, str)
        or "percentile" not in formula
    ):
        return None
    percentile = _type5_contract_number(valuation.get("pb_percentile"))
    current_pb = _type5_contract_number(valuation.get("current_pb_mrq"))
    replay = replay_valuation_distribution(valuation.get("pb_distribution"), current_pb)
    declared_median = _type5_contract_number(valuation.get("median_pb_mrq"))
    if (
        percentile is None
        or current_pb is None
        or replay is None
        or observations != replay["observations"]
        or declared_median is None
        or not math.isclose(declared_median, float(replay["median"]), rel_tol=0.0, abs_tol=1e-9)
        or not math.isclose(percentile, float(replay["percentile"]), rel_tol=0.0, abs_tol=1e-12)
        or _type5_pb_bottom_score(percentile, current_pb) is None
    ):
        return None
    quote_pb_raw = m.get("pb")
    if quote_pb_raw is not None:
        quote_pb = _type5_contract_number(quote_pb_raw)
        if quote_pb is None or quote_pb <= 0:
            return None
        relative_gap = abs(quote_pb - current_pb) / max(quote_pb, current_pb)
        if relative_gap > 0.20:
            return None
    return percentile, current_pb


def _type5_market_bottom_score(m: Mapping[str, Any]) -> Optional[float]:
    """Use only same-session, security-bound coldness and price declines."""
    reference_date = _evidence_reference_date(m.get("source_trade_date"))
    score = _verified_market_coldness_score(m)
    components = m.get("market_coldness_components")
    if (
        reference_date is None
        or score is None
        or not isinstance(components, Mapping)
        or components.get("as_of_session") != reference_date.isoformat()
    ):
        return None
    raw_values = components.get("raw_values")
    if not isinstance(raw_values, Mapping):
        return None
    change_60d = _type5_contract_number(raw_values.get("change_60d_pct"))
    change_ytd = _type5_contract_number(raw_values.get("change_ytd_pct"))
    if change_60d is None or change_ytd is None or not -100 <= change_60d <= 1_000 or not -100 <= change_ytd <= 1_000:
        return None
    # ``max`` is the less-negative period: both 60-day and YTD performance
    # must be cold before the drawdown component can score highly.
    confirmed_decline = max(change_60d, change_ytd)
    drawdown_score = (
        10.0
        if confirmed_decline <= -30.0
        else 9.0
        if confirmed_decline <= -20.0
        else 7.0
        if confirmed_decline <= -10.0
        else 5.0
        if confirmed_decline <= -5.0
        else 3.0
        if confirmed_decline < 0
        else 1.0
    )
    return min(score, drawdown_score)


def _type5_consecutive_history(
    m: Mapping[str, Any],
    value_key: str,
    year_key: str,
) -> tuple[list[float], list[int]]:
    """Return the latest four-to-ten-year fully consecutive suffix."""
    raw_values = m.get(value_key)
    raw_years = m.get(year_key)
    if (
        not isinstance(raw_values, (list, tuple))
        or not isinstance(raw_years, (list, tuple))
        or len(raw_values) != len(raw_years)
    ):
        return [], []
    points: list[tuple[int, float]] = []
    for raw_year, raw_value in zip(raw_years, raw_values):
        if isinstance(raw_year, (bool, np.bool_)):
            return [], []
        value = _type5_contract_number(raw_value)
        if not isinstance(raw_year, (int, np.integer)) or value is None:
            return [], []
        points.append((int(raw_year), value))
    points.sort()
    if len({year for year, _ in points}) != len(points):
        return [], []
    suffix = [points[-1]] if points else []
    for point in reversed(points[:-1]):
        if suffix[0][0] - point[0] != 1 or len(suffix) >= 10:
            break
        suffix.insert(0, point)
    if len(suffix) < 4:
        return [], []
    expected_year = _latest_complete_financial_year(m)
    if expected_year is None or suffix[-1][0] != expected_year:
        return [], []
    return [value for _, value in suffix], [year for year, _ in suffix]


def _type5_cycle_low_score(values: list[float]) -> Optional[float]:
    if len(values) < 4:
        return None
    spread = max(values) - min(values)
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


def _type5_financial_bottom_score(m: Mapping[str, Any]) -> Optional[tuple[float, str]]:
    """Locate a cycle low from reported margin or profit history."""
    candidates: list[tuple[float, str]] = []
    margins, _margin_years = _type5_consecutive_history(
        m,
        "gross_margin_history",
        "gross_margin_years",
    )
    if margins:
        changes = [current - prior for prior, current in zip(margins, margins[1:])]
        if (
            max(margins) - min(margins) > 0.15
            and any(change < 0 for change in changes)
            and any(change > 0 for change in changes)
        ):
            margin_score = _type5_cycle_low_score(margins)
            if margin_score is not None:
                candidates.append((margin_score, "毛"))
    profits, profit_years = _type5_consecutive_history(
        m,
        "net_profit_history",
        "net_profit_years",
    )
    if profits and _has_cycle_history(profits, profit_years):
        profit_score = _type5_cycle_low_score(profits)
        if profit_score is not None:
            candidates.append((profit_score, "利"))
    return max(candidates, default=None, key=lambda item: (item[0], item[1]))


def _type5_automatic_bottom_score(
    m: Mapping[str, Any],
    history_evidence: Optional[Mapping[str, Any]],
) -> tuple[float, str, bool]:
    """Combine three independent, observable Type5 bottom-source families."""
    pb_inputs = _type5_pb_history_inputs(m, history_evidence)
    market_score = _type5_market_bottom_score(m)
    financial_signal = _type5_financial_bottom_score(m)
    missing = []
    if pb_inputs is None:
        missing.append("PB历史")
    if market_score is None:
        missing.append("冷度")
    if financial_signal is None:
        missing.append("财务周期")
    if missing:
        return 5.0, "缺" + "/".join(missing) + "证据", False

    percentile, current_pb = pb_inputs
    valuation_score = _type5_pb_bottom_score(percentile, current_pb)
    if valuation_score is None:  # Defensive; the bound parser already checked this.
        return 5.0, "缺PB历史证据", False
    financial_score, financial_basis = financial_signal
    raw_score = 0.40 * valuation_score + 0.30 * market_score + 0.30 * financial_score
    resonant_sources = sum(score >= 6.0 for score in (valuation_score, market_score, financial_score))
    # One cheap PB observation cannot masquerade as a bottom.  Two positive
    # families remain only a weak diagnostic; an automatic score above seven
    # requires all three independent families to resonate.
    if resonant_sources < 2:
        raw_score = min(raw_score, 4.0)
    elif resonant_sources < 3:
        raw_score = min(raw_score, 6.0)
    score = round(raw_score, 1)
    reason = f"PB{percentile:.0%}/{current_pb:.2f};冷{market_score:.0f};{financial_basis}{financial_score:.0f}"
    return score, reason, True


def _type5_automatic_bottom_contract(
    m: Mapping[str, Any],
    history_evidence: Optional[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """Publish the raw, bounded inputs needed to replay an automatic 5b score."""

    pb_inputs = _type5_pb_history_inputs(m, history_evidence)
    market_score = _type5_market_bottom_score(m)
    financial_signal = _type5_financial_bottom_score(m)
    if (
        pb_inputs is None
        or market_score is None
        or financial_signal is None
        or not isinstance(history_evidence, Mapping)
    ):
        return None
    valuation_history = history_evidence.get("valuation_history")
    market_evidence = m.get("market_coldness_score_evidence")
    market_components = m.get("market_coldness_components")
    if (
        not isinstance(valuation_history, Mapping)
        or not isinstance(market_evidence, Mapping)
        or not isinstance(market_components, Mapping)
    ):
        return None
    gross_margins, gross_margin_years = _type5_consecutive_history(
        m,
        "gross_margin_history",
        "gross_margin_years",
    )
    profits, profit_years = _type5_consecutive_history(
        m,
        "net_profit_history",
        "net_profit_years",
    )
    return {
        "schema_version": TYPE5_BOTTOM_EVIDENCE_SCHEMA_VERSION,
        "model_id": TYPE5_BOTTOM_EVIDENCE_MODEL_ID,
        "code": _canonical_evidence_code(m.get("code")),
        "as_of": str(m.get("source_trade_date") or ""),
        "quote_pb": _type5_contract_number(m.get("pb")),
        "valuation_history": dict(valuation_history),
        "market_coldness_record": {
            "score": _type5_contract_number(m.get("market_coldness_score")),
            "evidence_level": m.get("market_coldness_score_evidence_level"),
            "evidence": dict(market_evidence),
            "components": dict(market_components),
        },
        "financial_cycle": {
            "gross_margin_history": gross_margins,
            "gross_margin_years": gross_margin_years,
            "net_profit_history": profits,
            "net_profit_years": profit_years,
        },
    }


def replay_type5_bottom_evidence_contract(
    value: Any,
    *,
    expected_code: Any,
    expected_as_of: Any,
) -> Optional[dict[str, Any]]:
    """Replay one exported automatic 5b contract from its raw observables."""

    expected = {
        "schema_version",
        "model_id",
        "code",
        "as_of",
        "quote_pb",
        "valuation_history",
        "market_coldness_record",
        "financial_cycle",
    }
    code = _canonical_evidence_code(expected_code)
    as_of = str(expected_as_of or "")
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or value.get("schema_version") != TYPE5_BOTTOM_EVIDENCE_SCHEMA_VERSION
        or value.get("model_id") != TYPE5_BOTTOM_EVIDENCE_MODEL_ID
        or _canonical_evidence_code(value.get("code")) != code
        or value.get("as_of") != as_of
    ):
        return None
    valuation = value.get("valuation_history")
    market = value.get("market_coldness_record")
    financial = value.get("financial_cycle")
    if (
        not isinstance(valuation, Mapping)
        or not isinstance(market, Mapping)
        or set(market) != {"score", "evidence_level", "evidence", "components"}
        or not isinstance(market.get("evidence"), Mapping)
        or not isinstance(market.get("components"), Mapping)
        or not isinstance(financial, Mapping)
        or set(financial)
        != {
            "gross_margin_history",
            "gross_margin_years",
            "net_profit_history",
            "net_profit_years",
        }
        or any(
            not isinstance(financial.get(values_key), list)
            or not isinstance(financial.get(years_key), list)
            or len(financial[values_key]) != len(financial[years_key])
            for values_key, years_key in (
                ("gross_margin_history", "gross_margin_years"),
                ("net_profit_history", "net_profit_years"),
            )
        )
    ):
        return None
    quote_pb = value.get("quote_pb")
    if quote_pb is not None and (_type5_contract_number(quote_pb) is None or float(quote_pb) <= 0):
        return None
    metric = {
        "code": code,
        "source_trade_date": as_of,
        "pb": quote_pb,
        "market_coldness_score": market.get("score"),
        "market_coldness_score_evidence_level": market.get("evidence_level"),
        "market_coldness_score_evidence": dict(market["evidence"]),
        "market_coldness_components": dict(market["components"]),
        **{key: financial[key] for key in financial},
    }
    history = {
        "model_id": LONG_HORIZON_HISTORY_MODEL_ID,
        "code": code,
        "as_of": as_of,
        "valuation_history": dict(valuation),
    }
    score, reason, complete = _type5_automatic_bottom_score(metric, history)
    if not complete:
        return None
    return {"score": score, "reason": reason}


def score_type5_counter_cyclical(
    m: Mapping[str, Any],
    benchmarks: Mapping[str, Mapping[str, Any]],
    dcf_result: Optional[Mapping[str, Any]] = None,
    history_evidence: Optional[Mapping[str, Any]] = None,
):
    """Patch6 Type5: strong-cycle bottom overlay, not a generic recovery model.

    The 2026-07-17 appendix requires strong-cycle *attributes* in 5a before
    this framework applies.  It forbids using the current PE as a cycle-value
    signal and explicitly excludes banks, insurers, brokers and wide-moat
    weak-cycle companies.  The automatic path requires five-year PB history,
    independently bound market coldness and reported financial-cycle history.
    It never invents replacement cost, cash cost, utilisation or inventory.
    """
    del benchmarks, dcf_result
    industry = str(m.get("industry") or "")
    if industry in FINANCIAL_INDUSTRIES:
        return _not_applicable("type5", "金融机构不适用强周期底部模型")

    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    profits, profit_years = _type5_consecutive_history(
        m,
        "net_profit_history",
        "net_profit_years",
    )
    profit_cycle = _has_cycle_history(profits, profit_years)
    margins, margin_years = _type5_consecutive_history(
        m,
        "gross_margin_history",
        "gross_margin_years",
    )
    margin_swing = (
        len(margins) >= 4
        and _aligned_current_consecutive(m, margins, margin_years, 4)
        and max(margins) - min(margins) > 0.15
    )
    direct_commodity_industry = industry in TYPE5_DIRECT_CYCLICAL_INDUSTRIES

    cycle_score, cycle_reason = _type5_external_score(m, "type5_cycle_attribute_score")
    if cycle_score is None:
        # Compatibility with existing evidence files.  New imports should use
        # the more precise ``type5_cycle_attribute_score`` field.
        cycle_score, cycle_reason = _type5_external_score(m, "cyclical_industry_score")
    if cycle_score is not None:
        if cycle_score < 7.0:
            return _not_applicable("type5", "外部证据未确认强周期属性")
        scores["5a"] = cycle_score
        reasons["5a"] = cycle_reason or "外部证据确认强周期"
    elif direct_commodity_industry and margin_swing and profit_cycle:
        # This is the only automatic route: a narrow commodity-industry label
        # plus two independently observable cross-cycle outcomes.  It does
        # not claim to have observed a capacity-clearing event.
        scores["5a"] = 7.0
        reasons["5a"] = "大宗行业/毛利/利润周期"
    elif direct_commodity_industry:
        return _insufficient_evidence("type5", "强周期属性缺毛利或盈利历史")
    else:
        return _not_applicable("type5", "非强周期标的，适用其他框架")

    bottom_score, bottom_reason = _type5_external_score(m, "type5_bottom_signal_score")
    if bottom_score is None:
        scores["5b"], reasons["5b"], bottom_complete = _type5_automatic_bottom_score(
            m,
            history_evidence,
        )
    else:
        scores["5b"], reasons["5b"] = bottom_score, bottom_reason or "周期底部外部证据"
        bottom_complete = True

    survival_score, survival_reason = _type5_external_score(m, "type5_survival_score")
    if survival_score is None:
        debt_ratio = _safe_float(m.get("debt_ratio"))
        monetary_funds = _safe_float(m.get("monetary_funds"))
        interest_debt = _safe_float(m.get("interest_debt"))
        assets = _safe_float(m.get("total_assets"))
        raw_fcf_history = list(m.get("fcf_history", []))
        fcf_history = [value for value in (_safe_float(item) for item in raw_fcf_history) if value is not None]
        fcf_complete = len(fcf_history) == len(raw_fcf_history) and _aligned_current_consecutive(
            m,
            raw_fcf_history,
            m.get("fcf_years"),
            3,
        )
        signals = sum(
            (
                debt_ratio is not None and debt_ratio <= 0.50,
                monetary_funds is not None and interest_debt is not None and monetary_funds >= interest_debt,
                monetary_funds is not None and assets is not None and assets > 0 and monetary_funds / assets >= 0.10,
                fcf_complete and all(value >= 0 for value in fcf_history[-3:]),
            )
        )
        scores["5c"] = {0: 2.0, 1: 4.0, 2: 6.0, 3: 8.0, 4: 9.0}[signals]
        reasons["5c"] = f"资产负债表稳健{signals}项"
        survival_complete = bool(
            debt_ratio is not None
            and monetary_funds is not None
            and interest_debt is not None
            and assets is not None
            and assets > 0
            and fcf_complete
        )
    else:
        scores["5c"], reasons["5c"] = survival_score, survival_reason or "抗周期外部证据"
        survival_complete = True

    elasticity_score, elasticity_reason = _type5_external_score(m, "type5_upside_elasticity_score")
    if elasticity_score is None:
        positive_profits = [value for value in profits if value > 0]
        multiple = (
            max(positive_profits) / min(positive_profits)
            if len(positive_profits) >= 4 and min(positive_profits) > 0
            else None
        )
        if multiple is None:
            scores["5d"], reasons["5d"] = 2.0, "缺完整周期盈利历史"
        elif multiple >= 5.0:
            scores["5d"], reasons["5d"] = 6.0, f"历史利润振幅{multiple:.1f}倍"
        elif multiple >= 3.0:
            scores["5d"], reasons["5d"] = 5.0, f"历史利润振幅{multiple:.1f}倍"
        else:
            scores["5d"], reasons["5d"] = 3.0, "历史利润弹性偏弱"
        elasticity_complete = bool(profit_cycle and multiple is not None)
    else:
        scores["5d"], reasons["5d"] = elasticity_score, elasticity_reason or "上行弹性外部证据"
        elasticity_complete = True

    earnings_score, earnings_reason = _type5_external_score(m, "type5_normalized_earnings_score")
    if earnings_score is None:
        normalised_pe, years_used = _type5_normalised_pe(m)
        if normalised_pe is None:
            scores["5e"], reasons["5e"] = 2.0, "缺5年完整周期均利"
        elif normalised_pe <= 8.0:
            scores["5e"], reasons["5e"] = 9.0, f"{years_used}年均利PE{normalised_pe:.1f}倍"
        elif normalised_pe <= 12.0:
            scores["5e"], reasons["5e"] = 7.0, f"{years_used}年均利PE{normalised_pe:.1f}倍"
        elif normalised_pe <= 18.0:
            scores["5e"], reasons["5e"] = 5.0, f"{years_used}年均利PE{normalised_pe:.1f}倍"
        elif normalised_pe <= 25.0:
            scores["5e"], reasons["5e"] = 3.0, f"{years_used}年均利PE{normalised_pe:.1f}倍"
        else:
            scores["5e"], reasons["5e"] = 1.0, f"{years_used}年均利PE{normalised_pe:.1f}倍"
        earnings_complete = normalised_pe is not None and years_used >= 5
    else:
        scores["5e"], reasons["5e"] = earnings_score, earnings_reason or "正常化盈利外部证据"
        earnings_complete = True

    # 5a≥7 has already been enforced above.  Unlike the previous model,
    # Type5 has no “cycle stage ≤3” veto and no hard 5c≥5 trigger gate:
    # total≥7 is the appendix's sole buy-point decision after applicability.
    evidence_complete = all((bottom_complete, survival_complete, elasticity_complete, earnings_complete))
    missing_dimension_keys: list[str] = []
    if not evidence_complete:
        missing = []
        if not bottom_complete:
            missing.append("底部")
            missing_dimension_keys.append("5b")
        if not survival_complete:
            missing.append("抗压")
            missing_dimension_keys.append("5c")
        if not elasticity_complete:
            missing.append("上行弹性")
            missing_dimension_keys.append("5d")
        if not earnings_complete:
            missing.append("均利")
            missing_dimension_keys.append("5e")
        reasons["_missing"] = "缺" + "/".join(missing) + "证据"
    return _finish(
        "type5",
        scores,
        reasons,
        evidence_complete=evidence_complete,
        missing_dimensions=missing_dimension_keys,
    )


def _type5_history_request_needed(m: Mapping[str, Any], outcome: tuple) -> bool:
    """Request PB history only when it can still change the Type5 decision."""
    if not isinstance(outcome, tuple) or len(outcome) != 4:
        return False
    _triggered, _total, raw_scores, raw_reasons = outcome
    if not isinstance(raw_scores, Mapping) or not isinstance(raw_reasons, Mapping):
        return False
    cycle_score = _safe_float(raw_scores.get("5a"))
    if (
        raw_reasons.get("_status") != STATUS_INSUFFICIENT_EVIDENCE
        or raw_reasons.get("_missing") != "缺底部证据"
        or cycle_score is None
        or cycle_score < 7.0
        or _type5_market_bottom_score(m) is None
        or _type5_financial_bottom_score(m) is None
    ):
        return False
    upper_scores = dict(raw_scores)
    upper_scores["5b"] = 10.0
    return _weighted_total(upper_scores, TYPE_WEIGHTS["type5"]) >= QUALIFY_THRESHOLD


TYPE3_GROWTH_EVIDENCE_MODEL_ID = "type3-growth-evidence-v1"
_TYPE3_GROWTH_EVIDENCE_FIELDS = {
    "available",
    "code",
    "as_of",
    "model_id",
    "external_growth_evidence",
    "segment_growth_sources",
    "cache_hit",
    "cache_diagnostic",
    "reason",
}


def _type3_uncapped_partial_score(m: Mapping[str, Any], key: str) -> float | None:
    evidence = m.get("quantitative_evidence")
    payload = evidence.get(key) if isinstance(evidence, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("evidence_level") != "partial":
        return None
    details = payload.get("details")
    quality = details.get("evidence_quality") if isinstance(details, Mapping) else None
    if not isinstance(quality, Mapping):
        return None
    expected_missing = {
        "growth_quality_score": ["acquisition_cash_and_goodwill_history"],
        "growth_sustainability_score": ["segment_growth_sources"],
    }.get(key)
    if quality.get("missing_inputs") != expected_missing:
        return None
    return _safe_float(details.get("score_before_evidence_cap"))


def _type3_growth_request_needed(
    m: Mapping[str, Any],
    benchmarks: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Fetch deep growth evidence only when it can still create a signal."""

    quality_score = _type3_uncapped_partial_score(m, "growth_quality_score")
    sustainability_score = _type3_uncapped_partial_score(m, "growth_sustainability_score")
    if quality_score is None or sustainability_score is None:
        return False
    candidate = dict(m)
    quantitative = m.get("quantitative_evidence")
    if not isinstance(quantitative, Mapping):
        return False
    for key, score in (
        # The automatic acquisition adapter is an aggregate proxy, not a
        # transaction census, so its Patch6 3b score cannot exceed six.
        ("growth_quality_score", min(quality_score, 6.0)),
        # Segment history can raise the current capped diagnostic.  Ten is the
        # safe request upper bound; the loaded evidence later determines the
        # actual source-count/time-depth band.
        ("growth_sustainability_score", 10.0),
    ):
        payload = quantitative.get(key)
        evidence = payload.get("evidence") if isinstance(payload, Mapping) else None
        if not isinstance(evidence, Mapping):
            return False
        candidate[key] = score
        candidate[f"{key}_evidence"] = dict(evidence)
        candidate[f"{key}_evidence_level"] = "derived_proxy"
    outcome = score_type3_sustainable_growth(candidate, benchmarks)
    if not isinstance(outcome, tuple) or len(outcome) != 4:
        return False
    _triggered, total, _scores, reasons = outcome
    return (
        _safe_float(total) is not None
        and float(total) >= QUALIFY_THRESHOLD
        and isinstance(reasons, Mapping)
        and reasons.get("_status") not in {STATUS_NOT_APPLICABLE, STATUS_VETOED, STATUS_BLOCKED}
    )


def _type3_growth_request(m: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the exact local-history input required by the growth adapter."""

    def records(values_key: str, years_key: str, *, minimum: int) -> list[dict[str, Any]] | None:
        raw_values = m.get(values_key)
        raw_years = m.get(years_key)
        if (
            not isinstance(raw_values, (list, tuple))
            or not isinstance(raw_years, (list, tuple))
            or len(raw_values) != len(raw_years)
        ):
            return None
        by_year: dict[int, float] = {}
        for raw_year, raw_value in zip(raw_years, raw_values):
            if isinstance(raw_year, bool):
                return None
            try:
                year = int(raw_year)
            except (TypeError, ValueError, OverflowError):
                return None
            value = _safe_float(raw_value)
            if not 1900 <= year <= 9999 or value is None or value < 0 or year in by_year:
                return None
            by_year[year] = value
        ordered = sorted(by_year)
        if len(ordered) < minimum:
            return None
        recent = ordered[-minimum:]
        if any(current - previous != 1 for previous, current in zip(recent, recent[1:])):
            return None
        return [{"year": year, "value": by_year[year]} for year in ordered]

    revenue_records = records("revenue_values", "revenue_years", minimum=5)
    goodwill_records = records("goodwill_history", "goodwill_years", minimum=5)
    code = str(m.get("code") or "")
    as_of = str(m.get("source_trade_date") or "")
    if (
        revenue_records is None
        or goodwill_records is None
        or not re.fullmatch(r"[036][0-9]{5}", code)
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of)
    ):
        return None
    return {
        "code": code,
        "as_of": as_of,
        "revenue_records": revenue_records,
        "goodwill_records": goodwill_records,
    }


def _type3_growth_components_from_evidence(
    evidence: Any,
    *,
    code: str,
    as_of: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one loader record before it can influence Type 3."""

    try:
        normalized = validate_growth_evidence_record(evidence, code, as_of)
    except (GrowthEvidenceError, TypeError, ValueError) as exc:
        raise ValueError(f"可持续增长证据校验失败:{code}:{exc}") from exc
    if set(normalized) != _TYPE3_GROWTH_EVIDENCE_FIELDS:
        raise ValueError(f"可持续增长证据标准化结构无效:{code}")
    external = normalized.get("external_growth_evidence")
    segments = normalized.get("segment_growth_sources")
    if not isinstance(external, Mapping) or not isinstance(segments, Mapping):
        raise ValueError(f"可持续增长证据子项无效:{code}")
    return dict(external), dict(segments)


def _refresh_type3_quantitative_evidence(
    metric: dict[str, Any],
    context: Mapping[str, Any],
    benchmarks: Mapping[str, Mapping[str, Any]],
) -> None:
    """Recompute only the two Type 3 scores unlocked by deep evidence."""

    industry = str(metric.get("industry") or "DEFAULT")
    industry_benchmark = benchmarks.get(industry, {}) if isinstance(benchmarks, Mapping) else {}
    fallback_growth = (
        _safe_float(industry_benchmark.get("median_cagr")) if isinstance(industry_benchmark, Mapping) else None
    )
    refreshed = derive_company_evidence(
        metric,
        context,
        fallback_industry_growth=fallback_growth,
    )
    quantitative = metric.get("quantitative_evidence")
    if not isinstance(quantitative, dict):
        quantitative = {}
        metric["quantitative_evidence"] = quantitative
    levels = metric.get("quantitative_evidence_levels")
    if not isinstance(levels, dict):
        levels = {}
        metric["quantitative_evidence_levels"] = levels
    for key in ("growth_quality_score", "growth_sustainability_score"):
        payload = refreshed.get(key)
        if not isinstance(payload, Mapping):
            raise ValueError(f"可持续增长量化证据重算失败:{metric.get('code')}:{key}")
        record = dict(payload)
        quantitative[key] = record
        level = str(record.get("evidence_level") or "missing")
        levels[key] = level
        if level == "derived_proxy":
            metric[key] = record["score"]
            metric[f"{key}_evidence"] = record["evidence"]
            metric[f"{key}_evidence_level"] = level
    if levels and all(level in {"primary", "derived_proxy"} for level in levels.values()):
        metric["quantitative_evidence_status"] = "complete"
    elif levels and all(level == "missing" for level in levels.values()):
        metric["quantitative_evidence_status"] = "missing"
    else:
        metric["quantitative_evidence_status"] = "partial"


_RESEARCH_EVIDENCE_FIELDS = {
    "available",
    "code",
    "as_of",
    "model_id",
    "sources",
    "distinct_publishers",
    "content_verification",
    "cache_hit",
    "cache_diagnostic",
    "reason",
}

_PATCH4_EVIDENCE_FIELDS = {
    "available",
    "code",
    "as_of",
    "model_id",
    "assessment",
    "criteria",
    "status",
    "documents",
    "cache_hit",
    "cache_diagnostic",
    "reason",
}
_PATCH4_CRITERIA = {
    "core_rd_ownership_pct",
    "esop_core_talent_coverage_pct",
    "long_term_rd_metrics",
    "frontline_rd_equity",
    "short_term_price_binding",
}


def _type7_patch4_assessment_from_evidence(
    evidence: Any,
    *,
    code: str,
    as_of: str,
) -> dict[str, Any] | None:
    """Validate the fail-closed announcement record before it can affect Type 7."""

    try:
        evidence = validate_patch4_evidence_record(evidence, code, as_of)
    except (Patch4EvidenceError, TypeError, ValueError) as exc:
        raise ValueError(f"科技股股东文化公告证据校验失败:{code}:{exc}") from exc
    if not isinstance(evidence, Mapping) or set(evidence) != _PATCH4_EVIDENCE_FIELDS:
        raise ValueError(f"科技股股东文化公告证据结构无效:{code}")
    available = evidence.get("available")
    if (
        evidence.get("code") != code
        or evidence.get("as_of") != as_of
        or evidence.get("model_id") != PATCH4_PUBLIC_EVIDENCE_MODEL_ID
        or not isinstance(available, bool)
        or not isinstance(evidence.get("cache_hit"), bool)
        or evidence.get("status") not in {"complete", "incomplete", "source_unavailable"}
    ):
        raise ValueError(f"科技股股东文化公告证据身份无效:{code}")
    for key in ("cache_diagnostic", "reason"):
        text = evidence.get(key)
        if not isinstance(text, str) or len(text) > 500 or any(ord(character) < 32 for character in text):
            raise ValueError(f"科技股股东文化公告证据诊断字段无效:{code}")
    criteria = evidence.get("criteria")
    documents = evidence.get("documents")
    if not isinstance(criteria, Mapping) or set(criteria) != _PATCH4_CRITERIA or not isinstance(documents, list):
        raise ValueError(f"科技股股东文化公告证据明细无效:{code}")
    known = 0
    for key in sorted(_PATCH4_CRITERIA):
        item = criteria.get(key)
        if not isinstance(item, Mapping) or set(item) != {
            "status",
            "reason",
            "value",
            "evidence_id",
            "documents_checked",
        }:
            raise ValueError(f"科技股股东文化公告证据子项无效:{code}:{key}")
        if item.get("status") not in {"known", "unknown"} or not isinstance(item.get("reason"), str):
            raise ValueError(f"科技股股东文化公告证据子项状态无效:{code}:{key}")
        checked = item.get("documents_checked")
        if isinstance(checked, bool) or not isinstance(checked, int) or checked < 0 or checked > len(documents):
            raise ValueError(f"科技股股东文化公告证据核验数量无效:{code}:{key}")
        if item["status"] == "known":
            known += 1
        elif item.get("value") is not None or item.get("evidence_id") is not None:
            raise ValueError(f"科技股股东文化公告未知子项携带了结论:{code}:{key}")
    assessment = evidence.get("assessment")
    if available:
        if evidence.get("status") != "complete" or evidence.get("reason") or known != len(_PATCH4_CRITERIA):
            raise ValueError(f"科技股股东文化公告证据完整状态矛盾:{code}")
        try:
            return normalise_patch4_assessment(assessment, security_code=code, as_of=as_of)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"科技股股东文化公告结论校验失败:{code}:{exc}") from exc
    if (
        assessment is not None
        or evidence.get("status") == "complete"
        or not evidence.get("reason")
        or known == len(_PATCH4_CRITERIA)
    ):
        raise ValueError(f"科技股股东文化公告证据失败状态矛盾:{code}")
    return None


def _type7_patch4_request_needed(ledger: Mapping[str, Any]) -> bool:
    """Fetch Patch 4 only when it can still change a viable Type 7 decision."""

    prerequisites = ledger.get("prerequisites")
    upper_bounds = ledger.get("decisive_score_upper_bounds")
    if not isinstance(prerequisites, Mapping) or not isinstance(upper_bounds, Mapping):
        return False
    technology = prerequisites.get("technology_patch4")
    if (
        not isinstance(technology, Mapping)
        or technology.get("applicable") is not True
        or technology.get("passed") is not False
        or technology.get("validation_status") != "missing_validated_patch4_assessment"
        or ledger.get("safety_veto") is True
        or ledger.get("decisively_not_triggered") is True
    ):
        return False
    permanent_prerequisites = {
        "core_modules_80pct",
        "three_year_financials",
        "latest_quote_and_valuation",
    }
    if any(
        not isinstance(prerequisites.get(key), Mapping) or prerequisites[key].get("passed") is not True
        for key in permanent_prerequisites
    ):
        return False
    return set(upper_bounds) == {"template1", "template5", "patch5"} and all(
        (_safe_float(value) or 0.0) > 70.0 for value in upper_bounds.values()
    )


def _type7_research_sources_from_evidence(
    evidence: Any,
    *,
    code: str,
    as_of: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Validate metadata and bounded body summaries before they influence Type 7."""

    if not isinstance(evidence, Mapping) or set(evidence) != _RESEARCH_EVIDENCE_FIELDS:
        raise ValueError(f"优质股权研报元数据结构无效:{code}")
    if (
        evidence.get("code") != code
        or evidence.get("as_of") != as_of
        or evidence.get("model_id") != RESEARCH_EVIDENCE_MODEL_ID
        or not isinstance(evidence.get("available"), bool)
        or not isinstance(evidence.get("cache_hit"), bool)
    ):
        raise ValueError(f"优质股权研报元数据身份无效:{code}")
    for key in ("cache_diagnostic", "reason"):
        text = evidence.get(key)
        if not isinstance(text, str) or len(text) > 500 or any(ord(character) < 32 for character in text):
            raise ValueError(f"优质股权研报元数据诊断字段无效:{code}")
    try:
        reference = date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"优质股权研报元数据日期无效:{code}") from exc
    sources = normalise_research_sources(
        evidence.get("sources"),
        today=reference,
        security_code=code,
    )
    publisher_count = len({source["publisher_id"].casefold() for source in sources})
    published_count = evidence.get("distinct_publishers")
    if isinstance(published_count, bool) or not isinstance(published_count, int) or published_count != publisher_count:
        raise ValueError(f"优质股权研报元数据机构计数无效:{code}")
    metadata_precheck = research_metadata_precheck(sources, reference=reference)
    content_verification = normalise_research_content_verification(
        evidence.get("content_verification"),
        sources=sources,
        security_code=code,
        as_of=as_of,
    )
    expected_available = bool(metadata_precheck["passed"] and content_verification["passed"])
    if evidence["available"] is not expected_available:
        raise ValueError(f"优质股权研报元数据可用状态无效:{code}")
    if (expected_available and evidence["reason"]) or (not expected_available and not evidence["reason"]):
        raise ValueError(f"优质股权研报元数据失败原因无效:{code}")
    return sources, content_verification


def score_type6_vc(m: Mapping[str, Any], benchmarks: Mapping[str, Mapping[str, Any]]):
    """情况六：按第19模板区分300亿高景气与100亿反转两类标的。"""
    if str(m.get("industry", "")) in FINANCIAL_INDUSTRIES:
        return _not_applicable("type6", "金融机构不适用小盘高风险型")
    industry = str(m.get("industry", ""))
    market_cap = _safe_float(m.get("market_cap"))
    industry_bucket = {} if industry == "DEFAULT" else benchmarks.get(industry, {})
    peer_context = m.get("_quantitative_peer_context")
    peer_context = peer_context if isinstance(peer_context, Mapping) else {}
    aggregate_growth = _safe_float(peer_context.get("aggregate_revenue_cagr"))
    aggregate_sample = _safe_float(peer_context.get("aggregate_revenue_cagr_count"))
    aggregate_coverage = _safe_float(peer_context.get("aggregate_revenue_coverage"))
    aggregate_ready = bool(
        peer_context.get("target_excluded") is True
        and aggregate_growth is not None
        and aggregate_sample is not None
        and aggregate_sample >= MIN_SECTOR_COMPANIES
        and aggregate_coverage is not None
        and aggregate_coverage >= MIN_COMPARABLE_COVERAGE
    )
    fallback_growth = _safe_float(industry_bucket.get("median_cagr"))
    fallback_sample = _safe_float(industry_bucket.get("median_cagr_count"))
    growth = (
        aggregate_growth
        if aggregate_ready
        else fallback_growth
        if not peer_context and fallback_sample is not None and fallback_sample >= MIN_SECTOR_COMPANIES
        else None
    )

    if market_cap is None or market_cap <= 0:
        return _insufficient_evidence("type6", "市值缺失或非正,无法确认小盘范围")
    if growth is None:
        return _insufficient_evidence("type6", "产业增速样本不足,无法判定高景气或反转类型")

    growth_subtype = growth >= 0.08
    subtype = "高景气技术型" if growth_subtype else "平稳产业反转型"
    market_cap_limit = TYPE6_GROWTH_MARKET_CAP_LIMIT if growth_subtype else TYPE6_TURNAROUND_MARKET_CAP_LIMIT
    if market_cap > market_cap_limit:
        limit_label = "300亿元" if growth_subtype else "100亿元"
        return _not_applicable("type6", f"{subtype}市值超过{limit_label}")

    net_profit = _safe_float(m.get("net_profit"))
    net_margin = _safe_float(m.get("net_margin"))
    if net_profit is None:
        return _insufficient_evidence("type6", "净利润缺失,无法确认亏损或微利画像")
    if net_profit > 0 and net_margin is None:
        return _insufficient_evidence("type6", "盈利但净利率缺失,无法确认微利画像")
    vc_profit_profile = bool(
        net_profit is not None and (net_profit <= 0 or (net_margin is not None and net_margin <= 0.05))
    )
    if not vc_profit_profile:
        return _not_applicable("type6", "公司并非亏损或净利率≤5%的微利标的")

    scores: dict[str, float] = {}
    reasons: dict[str, str] = {"_profile": subtype}
    if growth is None:
        scores["6a"], reasons["6a"] = 2.0, "产业爆发证据不足"
    elif growth < 0:
        scores["6a"], reasons["6a"] = 1.0, f"产业增速{growth:.1%}"
    elif growth < 0.08:
        scores["6a"], reasons["6a"] = 3.0, f"产业增速{growth:.1%}"
    elif growth < 0.20:
        scores["6a"], reasons["6a"] = 6.0, f"产业增速{growth:.1%}"
    elif growth < 0.50:
        scores["6a"], reasons["6a"] = 8.0, f"产业高速{growth:.1%}"
    else:
        scores["6a"], reasons["6a"] = 10.0, f"产业爆发{growth:.1%}"

    technology_score = _verified_score(m, "technology_score")
    if technology_score is not None and 0 <= technology_score <= 10:
        scores["6b"] = technology_score
        reasons["6b"] = _evidence_reason(m, "technology_score", "技术证据不可追溯")
    else:
        scores["6b"], reasons["6b"] = 0.0, "无技术验证原始数据"

    model_score = _verified_score(m, "business_model_score")
    if model_score is not None and 0 <= model_score <= 10:
        scores["6c"] = model_score
        reasons["6c"] = _evidence_reason(m, "business_model_score", "模式证据不可追溯")
    else:
        scores["6c"], reasons["6c"] = 0.0, "无模式创新原始数据"

    raw_profits = m.get("net_profit_history", [])
    raw_profits = list(raw_profits) if isinstance(raw_profits, (list, tuple)) else []
    parsed_profits = [_safe_float(value) for value in raw_profits]
    profits = [float(value) for value in parsed_profits if value is not None]
    profit_years = m.get("net_profit_years", [])
    annual_profit_pair_complete = bool(
        len(profits) == len(raw_profits) and _aligned_current_consecutive(m, raw_profits, profit_years, 2)
    )
    profit_change = _current_annual_change(m, "net_profit_history", "net_profit_years")
    raw_margins = m.get("margin_history", [])
    raw_margins = list(raw_margins) if isinstance(raw_margins, (list, tuple)) else []
    parsed_margins = [_safe_float(value) for value in raw_margins]
    margins = [float(value) for value in parsed_margins if value is not None]
    margin_years = m.get("margin_years", [])
    annual_margin_complete = bool(
        len(margins) == len(raw_margins) and _aligned_current_consecutive(m, raw_margins, margin_years, 3)
    )
    interim_profit_yoy = _same_period_metric_yoy(m, "profit")
    turnaround_evidence_complete = bool(
        annual_profit_pair_complete
        or annual_margin_complete
        or profit_change is not None
        or interim_profit_yoy is not None
    )
    if (
        len(profits) == len(raw_profits)
        and _aligned_current_consecutive(m, raw_profits, profit_years, 3)
        and profits[-3] < profits[-2] < profits[-1]
    ):
        scores["6d"], reasons["6d"] = 8.0, "利润连续两年改善"
    elif annual_profit_pair_complete and profits[-2] < 0 < profits[-1]:
        scores["6d"], reasons["6d"] = 6.0, "最新年度扭亏"
    elif profit_change is not None and profit_change > 0.30:
        scores["6d"], reasons["6d"] = 6.0, f"利润改善{profit_change:.1%}"
    elif annual_margin_complete and margins[-3] < margins[-2] < margins[-1]:
        scores["6d"], reasons["6d"] = 6.0, "净利率连续改善"
    else:
        scores["6d"], reasons["6d"] = 3.0, "困境反转未验证"
    if scores["6d"] <= 3 and interim_profit_yoy is not None and interim_profit_yoy > 0.30:
        scores["6d"], reasons["6d"] = 5.0, "最新同口径利润改善"
    latest_severity, latest_reason = _latest_period_deterioration(m, allow_improving_losses=True)
    if latest_severity >= 3:
        scores["6d"], reasons["6d"] = min(scores["6d"], 2.0), latest_reason
    elif latest_severity == 2:
        scores["6d"], reasons["6d"] = min(scores["6d"], 3.0), latest_reason
    elif latest_severity == 1:
        scores["6d"], reasons["6d"] = min(scores["6d"], 4.0), latest_reason

    position = _safe_float(m.get("position_size_pct"))
    portfolio = _safe_float(m.get("type6_portfolio_pct"))
    recommended_single = 1.0 if growth_subtype else 3.0
    recommended_portfolio = 8.0 if growth_subtype else 15.0
    # 补丁6的统一硬上限是单票5%、VC组合15%；第19模板的1%/3%
    # 是更保守的子类型建议，展示但不覆盖补丁6硬规则。
    discipline_ready = bool(
        position is not None and portfolio is not None and 0 < position <= 5.0 and 0 < portfolio <= 15.0
    )
    if position is None or portfolio is None:
        # 6e描述的是投资动作而非公司属性。系统可以量化给出纪律上限，
        # 但在用户确认实际仓位之前只能是条件候选，不能伪装成已触发。
        scores["6e"] = 10.0 if growth_subtype else 9.0
        reasons["6e"] = f"建议单票≤{recommended_single:.0f}%,组合≤{recommended_portfolio:.0f}%"
        reasons["_condition"] = "须确认实际仓位符合建议上限"
    else:
        single_score = 10.0 if position <= 3 else 8.0 if position <= 5 else 5.0 if position <= 10 else 2.0
        portfolio_score = 10.0 if portfolio <= 10 else 8.0 if portfolio <= 15 else 4.0 if portfolio <= 25 else 1.0
        scores["6e"] = min(single_score, portfolio_score)
        reasons["6e"] = f"单票{position:.0f}%,组合{portfolio:.0f}%"
        if not discipline_ready:
            reasons["_condition"] = "须单票≤5%且高风险组合≤15%"
    risk_cap = position if position is not None and position > 0 else recommended_single
    reasons["_risk"] = f"最坏归零时组合最大损失≤{risk_cap:.0f}%"

    high_elements = sum(scores[key] >= 5 for key in ("6a", "6b", "6c", "6d"))
    evidence_complete = bool(technology_score is not None and model_score is not None and turnaround_evidence_complete)
    evidence_veto = evidence_complete and high_elements < 2
    if evidence_veto:
        reasons["_veto"] = f"仅{high_elements}项核心证据≥5"
    missing_dimensions: list[str] = []
    missing_dimension_keys: list[str] = []
    if technology_score is None:
        missing_dimensions.append("技术")
        missing_dimension_keys.append("6b")
    if model_score is None:
        missing_dimensions.append("商业模式")
        missing_dimension_keys.append("6c")
    if not turnaround_evidence_complete:
        missing_dimensions.append("反转历史")
        missing_dimension_keys.append("6d")
    if missing_dimensions:
        reasons["_missing"] = "缺" + "/".join(missing_dimensions) + "可追溯证据"
    return _finish(
        "type6",
        scores,
        reasons,
        veto=evidence_veto,
        extra_condition=discipline_ready,
        evidence_complete=evidence_complete,
        missing_dimensions=missing_dimension_keys,
    )


def _type7_missing_dimensions(ledger: Mapping[str, Any]) -> list[str]:
    """Map incomplete Type 7 source ledgers to the three public dimensions."""

    scores = ledger.get("scores")
    upper_bounds = ledger.get("decisive_score_upper_bounds")
    missing: list[str] = []
    if isinstance(scores, Mapping) and isinstance(upper_bounds, Mapping):
        for dimension, source_key in {
            "7a": "template1",
            "7b": "template5",
            "7c": "patch5",
        }.items():
            score = _safe_float(scores.get(source_key))
            upper = _safe_float(upper_bounds.get(source_key))
            if score is None or upper is None or upper > score + 1e-9:
                missing.append(dimension)
    else:
        missing = list(TYPE_WEIGHTS["type7"])

    if ledger.get("prerequisites_complete") is not True and not missing:
        # A prerequisite failure with no usable item-level interval cannot be
        # assigned to a narrower dimension.  Keep all three unresolved rather
        # than certifying a score whose source contract is incomplete.
        missing = list(TYPE_WEIGHTS["type7"])
    return missing


def score_type7_quality_equity(
    m: Mapping[str, Any],
    type1_outcome: tuple[bool, float, Mapping[str, Any], Mapping[str, Any]],
    history_evidence: Mapping[str, Any] | None = None,
    *,
    valuation_evidence_complete: bool,
) -> tuple[tuple[bool, float, dict, dict], dict[str, Any]]:
    """情况七：三项独立质量与估值评分必须分别严格超过70。"""

    industry = str(m.get("industry") or "")
    if industry in FINANCIAL_INDUSTRIES:
        reason = "金融需专属优质股权模型"
        return _not_applicable("type7", reason), {
            "schema_version": QUALITY_EQUITY_SCHEMA_VERSION,
            "model_id": QUALITY_EQUITY_MODEL_ID,
            "code": str(m.get("code") or ""),
            "applicable": False,
            "reason": reason,
        }

    ledger = assess_quality_equity(
        m,
        type1_outcome,
        history_evidence,
        valuation_evidence_complete=valuation_evidence_complete,
    )
    ledger_errors = validate_quality_equity_ledger(ledger)
    if ledger_errors:
        raise AssertionError("情况七量化账本不变量失败:" + ";".join(ledger_errors[:3]))
    source_scores = ledger["scores"]
    scores = {
        "7a": round(float(source_scores["template1"]) / 10.0, 3),
        "7b": round(float(source_scores["template5"]) / 10.0, 3),
        "7c": round(float(source_scores["patch5"]) / 10.0, 3),
    }
    safety = float(ledger["patch5"]["safety_margin_score"])
    reasons = {
        "7a": f"长期质量回报{source_scores['template1']:.2f}",
        "7b": f"产业质量估值{source_scores['template5']:.2f}",
        "7c": f"商业安全{source_scores['patch5']:.2f}；边际{safety:.1f}",
    }
    strict_pass = bool(ledger["all_scores_strictly_above_70"])
    decisive_failure = bool(ledger["decisively_not_triggered"])
    if decisive_failure:
        reasons["_condition"] = "补全全部缺失证据后仍至少一套不超过70"
    elif not strict_pass:
        reasons["_condition"] = "三套分数均须严格大于70"
    failed_prerequisites = [key for key, record in ledger["prerequisites"].items() if not bool(record.get("passed"))]
    if failed_prerequisites:
        patch4_source_status = str(m.get("_type7_patch4_evidence_status") or "")
        technology_missing = (
            "公告数据源暂时不可用"
            if patch4_source_status == "source_unavailable"
            else "公告未直接披露全部五项"
            if patch4_source_status == "incomplete"
            else "缺核心研发持股与长期激励资料"
        )
        labels = {
            "core_modules_80pct": "核心必需子项不完整或覆盖不足80%",
            "technology_patch4": technology_missing,
            "three_year_financials": "不足3年财报",
            "latest_quote_and_valuation": "缺最新估值",
            "three_external_reports": "外部研报可获取性预检不足",
            "external_report_content_verification": "研报正文尚未读取并交叉核验",
            "ten_year_return_and_five_year_valuation": "缺十年回报或估值史",
        }
        reasons["_missing"] = labels.get(failed_prerequisites[0], "优质股权前置证据不足")
    if ledger["safety_veto"]:
        reasons["_veto"] = "安全边际低于8/20"
    return (
        _finish(
            "type7",
            scores,
            reasons,
            veto=bool(ledger["safety_veto"]),
            extra_condition=strict_pass,
            evidence_complete=bool(ledger["prerequisites_complete"]),
            status_override=STATUS_NOT_TRIGGERED if decisive_failure and not ledger["safety_veto"] else None,
            missing_dimensions=_type7_missing_dimensions(ledger),
        ),
        ledger,
    )


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _canonicalize_mapping(mapping: Mapping[Any, Any], label: str) -> dict[str, Any]:
    """标准化映射键；任何归一化碰撞都显式失败，禁止静默覆盖。"""
    canonical: dict[str, Any] = {}
    for raw_code, value in mapping.items():
        code = _normalize_code(raw_code)
        if not code:
            raise ValueError(f"{label}代码为空")
        if code in canonical:
            raise ValueError(f"{label}代码归一化冲突:{code}")
        canonical[code] = value
    return canonical


def _market_trigger_block_reason(m: Mapping[str, Any]) -> Optional[str]:
    """Defensive final gate for non-tradable or reference-price records."""
    if m.get("tradable") is False:
        return "标的不可交易"
    if bool(m.get("reference_price")):
        return "仅参考价不得触发买入"
    status = str(m.get("risk_status") or "").strip()
    if status and status.lower() not in {"正常", "normal", "active", "ok"}:
        return f"风险状态:{status}"
    return None


def _decision_market_context(m: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the raw market-actionability inputs used by the decision replay.

    ``_status`` and ``_veto`` are conclusions and can be edited together with a
    serialized decision object.  The three fields below are the smallest source
    context from which the market gate can be independently reconstructed.
    """

    raw_tradable = m.get("tradable")
    tradable = bool(raw_tradable) if isinstance(raw_tradable, (bool, np.bool_)) else None
    return {
        "tradable": tradable,
        "reference_price": bool(m.get("reference_price")),
        "risk_status": str(m.get("risk_status") or "").strip(),
    }


def _decision_market_block_reason(payload: Mapping[str, Any]) -> Optional[str]:
    """Replay the market gate from raw context, never from status/veto flags."""

    context = payload.get(_DECISION_MARKET_CONTEXT)
    if context is None:
        # Direct scorer callers and historical in-memory fixtures do not have a
        # row-level quote context.  They can still replay company rules, but a
        # serialized market-block claim without this context is rejected by the
        # result validator below.
        return None
    if not isinstance(context, Mapping) or set(context) != _DECISION_MARKET_CONTEXT_FIELDS:
        raise ValueError("decision market context is malformed")
    tradable = context.get("tradable")
    reference_price = context.get("reference_price")
    risk_status = context.get("risk_status")
    if (tradable is not None and type(tradable) is not bool) or type(reference_price) is not bool:
        raise ValueError("decision market context types are invalid")
    if not isinstance(risk_status, str):
        raise ValueError("decision market risk status is invalid")
    return _market_trigger_block_reason(context)


def _decision_dimension_bounds(
    type_key: str,
    sub_scores: Mapping[str, Any],
    missing_dimensions: Sequence[str],
    ledger: Mapping[str, Any] | None,
) -> tuple[float, float, dict[str, float]]:
    """Replay conservative score bounds without treating placeholders as facts."""

    weights = TYPE_WEIGHTS[type_key]
    missing = set(missing_dimensions)
    lower_dimensions: dict[str, float] = {}
    upper_dimensions: dict[str, float] = {}
    type7_upper: dict[str, float] = {}
    if type_key == "type7" and isinstance(ledger, Mapping):
        raw_upper = ledger.get("decisive_score_upper_bounds")
        if isinstance(raw_upper, Mapping):
            for dimension, source_key in {
                "7a": "template1",
                "7b": "template5",
                "7c": "patch5",
            }.items():
                value = _safe_float(raw_upper.get(source_key))
                if value is not None and 0.0 <= value <= 100.0:
                    type7_upper[dimension] = min(10.0, value / 10.0)

    for dimension in weights:
        score = _safe_float(sub_scores.get(dimension))
        known_score = min(10.0, max(0.0, score if score is not None else 0.0))
        if dimension in missing:
            lower_dimensions[dimension] = 0.0
            # Type 7 has a replayed, component-by-component mathematical
            # ceiling.  The other frameworks deliberately use the full 0..10
            # theoretical range for every missing dimension.  In particular,
            # Type 3's data-adapter capability cap is not a model ceiling.
            upper_dimensions[dimension] = type7_upper.get(dimension, 10.0)
        else:
            lower_dimensions[dimension] = known_score
            upper_dimensions[dimension] = known_score

    lower = round(math.fsum(lower_dimensions[key] * weights[key] for key in weights), 1)
    upper = round(math.fsum(upper_dimensions[key] * weights[key] for key in weights), 1)
    # Patch 6 makes a confirmed Type 3 bubble score a hard diagnostic cap.
    # Apply it only when 3e itself is known; a missing 3e has a theoretical
    # range of 0..10 and cannot inherit the adapter's displayed placeholder.
    if type_key == "type3" and "3e" not in missing and upper_dimensions.get("3e", 10.0) <= 3.0:
        lower = min(lower, 4.9)
        upper = min(upper, 4.9)
    return lower, upper, upper_dimensions


def _decision_missing_dimensions(
    type_key: str,
    reasons: Mapping[str, Any],
    *,
    applicable: bool,
    evidence_complete: bool,
) -> list[str]:
    weights = TYPE_WEIGHTS[type_key]
    if not applicable:
        return []
    raw = reasons.get(_DECISION_MISSING_DIMENSIONS_REASON)
    if isinstance(raw, (list, tuple)):
        declared = [key for key in weights if key in raw]
    else:
        declared = []
    if not evidence_complete and not declared:
        declared = list(weights)
    if type_key == "type6" and reasons.get("_condition") == "须确认实际仓位符合建议上限" and "6e" not in declared:
        declared.append("6e")
    return [key for key in weights if key in declared]


def _decision_possible_veto(
    type_key: str,
    missing_dimensions: Sequence[str],
    upper_dimensions: Mapping[str, float],
) -> tuple[bool, bool]:
    """Return (possible veto, logically confirmed veto) from score intervals."""

    missing = set(missing_dimensions)
    if type_key == "type2":
        possible = "2c" in missing
        if missing.intersection({"2a", "2b"}):
            lower_hot = math.fsum(0.0 if key in missing else upper_dimensions[key] for key in ("2a", "2b")) / 2.0
            possible = possible or lower_hot <= 4.0
        return possible, False
    if type_key == "type4":
        possible_moat = "4c" in missing
        possible_double_bubble = bool(
            missing.intersection({"4e", "4f"})
            and (0.0 if "4e" in missing else upper_dimensions["4e"]) <= 3.0
            and (0.0 if "4f" in missing else upper_dimensions["4f"]) <= 3.0
        )
        return possible_moat or possible_double_bubble, False
    if type_key == "type6":
        core = ("6a", "6b", "6c", "6d")
        known_high = sum(key not in missing and upper_dimensions[key] >= 5.0 for key in core)
        missing_core = sum(key in missing for key in core)
        maximum_high = known_high + missing_core
        if maximum_high < 2:
            return False, True
        return known_high < 2, False
    return bool(missing.intersection(_POTENTIAL_VETO_DIMENSIONS[type_key])), False


def _decision_confirmed_hard_veto(
    type_key: str,
    missing_dimensions: Sequence[str],
    dimensions: Mapping[str, float],
    ledger: Mapping[str, Any] | None,
) -> bool:
    """Recompute every framework's source-proven hard veto from model inputs."""

    missing = set(missing_dimensions)

    def known(key: str) -> bool:
        return key not in missing

    if type_key == "type1":
        return bool((known("1a") and dimensions["1a"] <= 2.0) or (known("1b") and dimensions["1b"] <= VETO_SCORE))
    if type_key == "type2":
        hot_veto = bool(known("2a") and known("2b") and (dimensions["2a"] + dimensions["2b"]) / 2.0 <= 4.0)
        cold_veto = bool(known("2c") and dimensions["2c"] <= VETO_SCORE)
        return hot_veto or cold_veto
    if type_key == "type3":
        # 3e is a 4.9-point total cap, not a company-level veto.
        return bool(
            (known("3a") and dimensions["3a"] <= VETO_SCORE) or (known("3d") and dimensions["3d"] <= VETO_SCORE)
        )
    if type_key == "type4":
        moat_veto = bool(known("4c") and dimensions["4c"] <= VETO_SCORE)
        double_bubble_veto = bool(
            known("4e") and known("4f") and dimensions["4e"] <= VETO_SCORE and dimensions["4f"] <= VETO_SCORE
        )
        return moat_veto or double_bubble_veto
    if type_key == "type5":
        # The versioned, later Type 5 appendix has no post-applicability veto.
        return False
    if type_key == "type6":
        core = ("6a", "6b", "6c", "6d")
        return bool(all(known(key) for key in core) and sum(dimensions[key] >= 5.0 for key in core) < 2)
    if type_key == "type7":
        patch5 = ledger.get("patch5") if isinstance(ledger, Mapping) else None
        if not isinstance(patch5, Mapping) or patch5.get("safety_margin_complete") is not True:
            return False
        safety_score = _safe_float(patch5.get("safety_margin_score"))
        return bool(safety_score is not None and safety_score < PATCH5_SAFETY_VETO)
    raise ValueError(f"unknown decision type: {type_key}")


def _decision_theoretically_triggerable(
    type_key: str,
    *,
    upper: float,
    upper_dimensions: Mapping[str, float],
    missing_dimensions: Sequence[str],
    reasons: Mapping[str, Any],
) -> bool:
    """Return whether one assignment inside the evidence bounds can trigger."""

    if upper < QUALIFY_THRESHOLD:
        return False
    missing = set(missing_dimensions)
    if type_key == "type1":
        # Every measured in-zone score is at least five.  Missing 1a retains
        # the full theoretical range and can therefore still enter the zone.
        return bool("1a" in missing or upper_dimensions["1a"] >= 5.0)
    if type_key == "type2":
        hot_upper = (upper_dimensions["2a"] + upper_dimensions["2b"]) / 2.0
        return bool(
            upper_dimensions["2d"] >= 5.0
            or (hot_upper >= 7.0 and upper_dimensions["2c"] >= 7.0 and 4.0 <= upper_dimensions["2d"] <= 5.0)
        )
    if type_key == "type6":
        core = ("6a", "6b", "6c", "6d")
        if sum(upper_dimensions[key] >= 5.0 for key in core) < 2:
            return False
        if "6e" in missing:
            return True
        # A score below eight cannot satisfy the 5%/15% hard limits.  The
        # explicit condition also catches non-positive input, which otherwise
        # maps to a high numeric score through the display rubric.
        return bool(upper_dimensions["6e"] >= 8.0 and not reasons.get("_condition"))
    if type_key == "type7":
        return all(upper_dimensions[key] > QUALIFY_THRESHOLD for key in TYPE_WEIGHTS[type_key])
    return True


def replay_buy_decision(type_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Independently replay one framework's bounded buy/no-buy decision.

    The stored ``decision`` object is intentionally ignored.  Callers can
    therefore compare this replay with a serialized contract and detect any
    mutation of its bounds, basis, veto state or candidate visibility.
    """

    if type_key not in TYPE_WEIGHTS:
        raise ValueError(f"unknown decision type: {type_key}")
    sub_scores = payload.get("sub_scores")
    reasons = payload.get("reasons")
    if not isinstance(sub_scores, Mapping) or not isinstance(reasons, Mapping):
        raise ValueError(f"{type_key} decision source is incomplete")
    applicable_marker = reasons.get("_applicable")
    evidence_marker = reasons.get("_evidence")
    if applicable_marker not in {"yes", "no"}:
        raise ValueError(f"{type_key} applicability source is invalid")
    if evidence_marker not in {"complete", "incomplete"}:
        raise ValueError(f"{type_key} evidence source is invalid")
    applicable = applicable_marker == "yes"
    evidence_complete = evidence_marker == "complete"
    missing = _decision_missing_dimensions(
        type_key,
        reasons,
        applicable=applicable,
        evidence_complete=evidence_complete,
    )
    ledger = payload.get("ledger") if type_key == "type7" else None

    if not applicable:
        return {
            "schema_version": DECISION_SCHEMA_VERSION,
            "model_id": DECISION_MODEL_ID,
            "decision_complete": True,
            "decision_basis": "scope_exclusion",
            "score_lower_bound": 0.0,
            "score_upper_bound": 0.0,
            "veto_state": "none",
            "potentially_triggerable": False,
            "missing_dimensions": [],
        }

    lower, upper, upper_dimensions = _decision_dimension_bounds(type_key, sub_scores, missing, ledger)
    market_blocked = _decision_market_block_reason(payload) is not None
    confirmed_hard_veto = _decision_confirmed_hard_veto(
        type_key,
        missing,
        upper_dimensions,
        ledger,
    )
    possible_veto, bounded_veto = _decision_possible_veto(type_key, missing, upper_dimensions)

    if confirmed_hard_veto or bounded_veto:
        complete = True
        basis = "confirmed_veto"
        veto_state = "confirmed"
        potentially_triggerable = False
    elif market_blocked:
        complete = True
        basis = "market_block"
        veto_state = "none"
        potentially_triggerable = False
    else:
        veto_state = "possible" if possible_veto else "none"
        action_condition = bool(
            type_key == "type6" and reasons.get("_condition") == "须确认实际仓位符合建议上限" and "6e" in missing
        )
        theoretically_triggerable = _decision_theoretically_triggerable(
            type_key,
            upper=upper,
            upper_dimensions=upper_dimensions,
            missing_dimensions=missing,
            reasons=reasons,
        )

        if action_condition:
            if theoretically_triggerable:
                complete = False
                basis = "action_condition"
                # The company-side model is actionable only after the user
                # binds the real position.  Keep it visible only when both its
                # company rules and total can still mathematically pass.
                potentially_triggerable = True
            else:
                complete = True
                basis = "conservative_upper_bound"
                potentially_triggerable = False
        elif evidence_complete:
            complete = True
            basis = "full_evidence"
            potentially_triggerable = theoretically_triggerable
        elif not theoretically_triggerable:
            complete = True
            basis = "conservative_upper_bound"
            potentially_triggerable = False
        else:
            complete = False
            basis = "unresolved_missing_evidence"
            potentially_triggerable = True

    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "model_id": DECISION_MODEL_ID,
        "decision_complete": complete,
        "decision_basis": basis,
        "score_lower_bound": lower,
        "score_upper_bound": upper,
        "veto_state": veto_state,
        "potentially_triggerable": potentially_triggerable,
        "missing_dimensions": missing,
    }


def _decision_source_hard_veto(type_key: str, payload: Mapping[str, Any]) -> bool:
    """Return only a veto proven by known source dimensions (not a bound)."""

    sub_scores = payload.get("sub_scores")
    reasons = payload.get("reasons")
    if not isinstance(sub_scores, Mapping) or not isinstance(reasons, Mapping):
        raise ValueError(f"{type_key} decision source is incomplete")
    applicable_marker = reasons.get("_applicable")
    evidence_marker = reasons.get("_evidence")
    if applicable_marker not in {"yes", "no"} or evidence_marker not in {"complete", "incomplete"}:
        raise ValueError(f"{type_key} decision source markers are invalid")
    if applicable_marker == "no":
        return False
    missing = _decision_missing_dimensions(
        type_key,
        reasons,
        applicable=True,
        evidence_complete=evidence_marker == "complete",
    )
    ledger = payload.get("ledger") if type_key == "type7" else None
    _lower, _upper, upper_dimensions = _decision_dimension_bounds(type_key, sub_scores, missing, ledger)
    return _decision_confirmed_hard_veto(type_key, missing, upper_dimensions, ledger)


def _build_bear_case(primary_type: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """生成空头最致命的三项证据，顺序完全确定且不生成投资建议。"""
    weights = TYPE_WEIGHTS.get(primary_type)
    if not weights:
        return []
    sub_scores = _sanitize_scores(payload.get("sub_scores", {}), weights)
    raw_reasons = payload.get("reasons", {})
    reasons = raw_reasons if isinstance(raw_reasons, Mapping) else {}
    minimum_score = min(sub_scores.values()) if sub_scores else 0.0
    result: list[dict[str, Any]] = []
    for meta_key in ("_veto", "_condition", "_downgrade"):
        if reasons.get(meta_key):
            result.append(
                {
                    "dimension": meta_key,
                    "score": minimum_score,
                    "reason": _compact_reason(reasons[meta_key]),
                }
            )
            if len(result) == 3:
                return result
    order = {key: index for index, key in enumerate(weights)}
    ranked = sorted(weights, key=lambda key: (sub_scores[key], -weights[key], order[key]))
    for key in ranked:
        result.append(
            {
                "dimension": key,
                "score": sub_scores[key],
                "reason": _compact_reason(reasons.get(key)),
            }
        )
        if len(result) == 3:
            break
    return result


def validate_screening_result(result: pd.DataFrame) -> list[str]:
    """返回跨七类结果的不变量错误；空列表表示结构与算术一致。"""
    errors: list[str] = []
    required = {
        "code",
        "source_trade_date",
        "buy_types",
        "primary_type",
        "diagnostic_type",
        "max_score",
        "bear_case",
    }
    if not required.issubset(result.columns):
        return [f"缺字段:{','.join(sorted(required - set(result.columns)))}"]
    normalized_codes = [_normalize_code(code) for code in result["code"]]
    if any(not code for code in normalized_codes):
        errors.append("code为空")
    seen_codes: set[str] = set()
    duplicate_codes: set[str] = set()
    for code in normalized_codes:
        if code in seen_codes:
            duplicate_codes.add(code)
        seen_codes.add(code)
    if duplicate_codes:
        errors.append("code重复:" + ",".join(sorted(duplicate_codes)[:5]))
    for row_index, row in result.iterrows():
        type_totals: dict[str, float] = {}
        diagnostic_totals: dict[str, float] = {}
        triggered: list[str] = []
        for type_key, weights in TYPE_WEIGHTS.items():
            payload = row.get(type_key)
            if not isinstance(payload, Mapping):
                errors.append(f"{row_index}:{type_key}结构缺失")
                continue
            sub_scores = payload.get("sub_scores", {})
            reasons = payload.get("reasons", {})
            if set(sub_scores) != set(weights):
                errors.append(f"{row_index}:{type_key}子项不全")
                continue
            if set(weights) - set(reasons):
                errors.append(f"{row_index}:{type_key}理由不全")
            if any(_safe_float(value) is None or not 0 <= float(value) <= 10 for value in sub_scores.values()):
                errors.append(f"{row_index}:{type_key}分数非法")
            if any(len(str(reasons.get(key, ""))) > EVIDENCE_MAX_LENGTH for key in weights):
                errors.append(f"{row_index}:{type_key}理由过长")
            expected_total = _weighted_total(sub_scores, weights, decimals=3 if type_key == "type7" else 2)
            actual_total = _safe_float(payload.get("total"))
            # 补丁6：3e泡沫降级必须严格低于5.0。
            bubble_cap = (
                type_key == "type3" and sub_scores.get("3e", 10) <= 3 and actual_total == 4.9 and expected_total >= 4.9
            )
            if actual_total is None or not (math.isclose(actual_total, expected_total, abs_tol=1e-9) or bubble_cap):
                errors.append(f"{row_index}:{type_key}总分错误")
            reason_veto = bool(reasons.get("_veto"))
            if bool(payload.get("veto")) != reason_veto:
                errors.append(f"{row_index}:{type_key}否决字段错误")
            status = payload.get("status")
            if status not in TYPE_STATUSES or status != reasons.get("_status"):
                errors.append(f"{row_index}:{type_key}状态字段错误")
            try:
                market_block_reason = _decision_market_block_reason(payload)
            except (TypeError, ValueError) as exc:
                market_block_reason = None
                errors.append(f"{row_index}:{type_key}市场阻断来源错误:{exc}")
            published_market_marker = reasons.get(_DECISION_MARKET_BLOCK_REASON)
            if bool(published_market_marker) != bool(market_block_reason) or (
                market_block_reason is not None and published_market_marker != _compact_reason(market_block_reason)
            ):
                errors.append(f"{row_index}:{type_key}市场阻断标记错误")
            decision = payload.get("decision")
            decision_shape_valid = isinstance(decision, Mapping) and set(decision) == _DECISION_FIELDS
            if not decision_shape_valid:
                errors.append(f"{row_index}:{type_key}决策边界结构错误")
            else:
                assert isinstance(decision, Mapping)
                lower_bound = _safe_float(decision.get("score_lower_bound"))
                upper_bound = _safe_float(decision.get("score_upper_bound"))
                missing_decision_dimensions = decision.get("missing_dimensions")
                decision_types_valid = bool(
                    type(decision.get("schema_version")) is int
                    and decision.get("schema_version") == DECISION_SCHEMA_VERSION
                    and decision.get("model_id") == DECISION_MODEL_ID
                    and type(decision.get("decision_complete")) is bool
                    and decision.get("decision_basis") in DECISION_BASES
                    and type(decision.get("potentially_triggerable")) is bool
                    and decision.get("veto_state") in DECISION_VETO_STATES
                    and lower_bound is not None
                    and upper_bound is not None
                    and 0.0 <= lower_bound <= upper_bound <= 10.0
                    and isinstance(missing_decision_dimensions, list)
                    and len(missing_decision_dimensions) == len(set(missing_decision_dimensions))
                    and all(item in weights for item in missing_decision_dimensions)
                )
                if not decision_types_valid:
                    errors.append(f"{row_index}:{type_key}决策边界字段错误")
                try:
                    expected_decision = replay_buy_decision(type_key, payload)
                except (TypeError, ValueError) as exc:
                    errors.append(f"{row_index}:{type_key}决策边界无法重放:{exc}")
                else:
                    if dict(decision) != expected_decision:
                        errors.append(f"{row_index}:{type_key}决策边界重放错误")
                    expected_trigger = bool(
                        expected_decision["decision_basis"] == "full_evidence"
                        and expected_decision["potentially_triggerable"]
                    )
                    if bool(payload.get("triggered")) is not expected_trigger:
                        errors.append(f"{row_index}:{type_key}模型触发重放错误")
            try:
                source_hard_veto = _decision_source_hard_veto(type_key, payload)
            except (TypeError, ValueError) as exc:
                source_hard_veto = False
                errors.append(f"{row_index}:{type_key}否决条件无法重放:{exc}")
            market_rewrite_veto = bool(
                market_block_reason is not None and status == STATUS_BLOCKED and not reasons.get("_blocked")
            )
            if reason_veto and not source_hard_veto and not market_rewrite_veto:
                errors.append(f"{row_index}:{type_key}否决缺少模型依据")
            if status == STATUS_VETOED and not source_hard_veto:
                errors.append(f"{row_index}:{type_key}否决状态缺少模型依据")
            if type_key == "type5":
                bottom_mode = payload.get("bottom_evidence_mode")
                bottom_contract = payload.get("bottom_evidence_contract")
                if bottom_mode not in {
                    "automatic_replay",
                    "trusted_external",
                    "incomplete",
                    "not_applicable",
                }:
                    errors.append(f"{row_index}:type5底部证据模式错误")
                elif bottom_mode == "automatic_replay":
                    bottom_replay = replay_type5_bottom_evidence_contract(
                        bottom_contract,
                        expected_code=row.get("code"),
                        expected_as_of=row.get("source_trade_date"),
                    )
                    if (
                        bottom_replay is None
                        or not math.isclose(
                            float(sub_scores.get("5b", -1.0)),
                            float(bottom_replay["score"]),
                            abs_tol=1e-9,
                        )
                        or reasons.get("5b") != bottom_replay["reason"]
                    ):
                        errors.append(f"{row_index}:type5自动底部证据重放错误")
                elif bottom_contract is not None:
                    errors.append(f"{row_index}:type5非自动路径携带底部合同")
                if status == STATUS_NOT_APPLICABLE and bottom_mode != "not_applicable":
                    errors.append(f"{row_index}:type5不适用证据模式错误")
                if status != STATUS_NOT_APPLICABLE and bottom_mode == "not_applicable":
                    errors.append(f"{row_index}:type5适用证据模式错误")
            if type_key == "type7":
                ledger = payload.get("ledger")
                if status == STATUS_NOT_APPLICABLE:
                    if not isinstance(ledger, Mapping) or ledger.get("applicable") is not False:
                        errors.append(f"{row_index}:type7不适用账本错误")
                else:
                    ledger_errors = validate_quality_equity_ledger(ledger)
                    if ledger_errors:
                        errors.append(f"{row_index}:type7账本错误:{ledger_errors[0]}")
                    elif isinstance(ledger, Mapping):
                        source_scores = ledger.get("scores", {})
                        expected_sub_scores = {
                            "7a": round(float(source_scores["template1"]) / 10.0, 3),
                            "7b": round(float(source_scores["template5"]) / 10.0, 3),
                            "7c": round(float(source_scores["patch5"]) / 10.0, 3),
                        }
                        if sub_scores != expected_sub_scores:
                            errors.append(f"{row_index}:type7展示分与账本不一致")
                        if status != STATUS_BLOCKED and bool(payload.get("triggered")) != bool(ledger.get("triggered")):
                            errors.append(f"{row_index}:type7触发与账本不一致")
            if bool(payload.get("applicable")) != (status != STATUS_NOT_APPLICABLE):
                errors.append(f"{row_index}:{type_key}适用字段错误")
            reason_evidence_complete = reasons.get("_evidence") == "complete"
            if bool(payload.get("evidence_complete")) != reason_evidence_complete:
                errors.append(f"{row_index}:{type_key}证据字段错误")
            if status == STATUS_TRIGGERED and not reason_evidence_complete:
                errors.append(f"{row_index}:{type_key}证据不完整仍触发")
            if status in {STATUS_NOT_APPLICABLE, STATUS_INSUFFICIENT_EVIDENCE} and (
                reason_veto or payload.get("triggered")
            ):
                errors.append(f"{row_index}:{type_key}不适用或证据不足被误作否决")
            if bool(payload.get("triggered")) != (status == STATUS_TRIGGERED):
                errors.append(f"{row_index}:{type_key}触发状态不一致")
            if payload.get("triggered") and (reason_veto or payload.get("veto")):
                errors.append(f"{row_index}:{type_key}否决仍触发")
            if payload.get("triggered") and (actual_total is None or actual_total < QUALIFY_THRESHOLD):
                errors.append(f"{row_index}:{type_key}低分仍触发")
            if payload.get("triggered"):
                triggered.append(type_key)
            if actual_total is not None:
                type_totals[type_key] = actual_total
                if status not in {STATUS_NOT_APPLICABLE, STATUS_INSUFFICIENT_EVIDENCE}:
                    diagnostic_totals[type_key] = actual_total
        expected_types = [key for key in TYPE_PRIORITY if key in triggered]
        if row.get("buy_types") != expected_types:
            errors.append(f"{row_index}:buy_types错误")
        actual_max = _safe_float(row.get("max_score"))
        if diagnostic_totals:
            if actual_max is None or not math.isclose(actual_max, max(diagnostic_totals.values()), abs_tol=1e-9):
                errors.append(f"{row_index}:max_score错误")
        elif actual_max is not None:
            errors.append(f"{row_index}:max_score应为空")
        expected_primary = expected_types[0] if expected_types else None
        if diagnostic_totals:
            top = max(diagnostic_totals.values())
            expected_diagnostic = next(key for key in TYPE_PRIORITY if diagnostic_totals.get(key) == top)
        else:
            expected_diagnostic = None
        if row.get("primary_type") != expected_primary:
            errors.append(f"{row_index}:primary_type错误")
        if row.get("diagnostic_type") != expected_diagnostic:
            errors.append(f"{row_index}:diagnostic_type错误")
        bear_case = row.get("bear_case")
        primary_payload = row.get(expected_diagnostic) if expected_diagnostic else None
        if expected_diagnostic is None:
            if bear_case != []:
                errors.append(f"{row_index}:bear_case应为空")
            continue
        if not isinstance(bear_case, list) or len(bear_case) != 3:
            errors.append(f"{row_index}:bear_case数量错误")
        elif not isinstance(primary_payload, Mapping):
            errors.append(f"{row_index}:bear_case主类型缺失")
        else:
            expected_bear_case = _build_bear_case(expected_diagnostic, primary_payload)
            if bear_case != expected_bear_case:
                errors.append(f"{row_index}:bear_case排序错误")
            for item in bear_case:
                if not isinstance(item, Mapping) or set(item) != {"dimension", "score", "reason"}:
                    errors.append(f"{row_index}:bear_case结构错误")
                    break
                score = _safe_float(item.get("score"))
                if score is None or not 0 <= score <= 10:
                    errors.append(f"{row_index}:bear_case分数错误")
                    break
                if not item.get("reason") or len(str(item["reason"])) > EVIDENCE_MAX_LENGTH:
                    errors.append(f"{row_index}:bear_case理由错误")
                    break
    return errors


RESULT_COLUMNS = [
    "code",
    "name",
    "source_trade_date",
    "price",
    "pe",
    "pb",
    "market_cap",
    "industry",
    "cagr_3yr",
    "roe",
    "net_margin",
    "debt_ratio",
    "peg",
    "market_coldness_score",
    "market_coldness_evidence",
    "financial_sector_evidence",
    "quantitative_model_id",
    "quantitative_evidence",
    "quantitative_evidence_levels",
    "quantitative_evidence_status",
    *(
        field
        for evidence_key in QUANTITATIVE_SCORE_KEYS
        for field in (evidence_key, f"{evidence_key}_evidence", f"{evidence_key}_evidence_level")
    ),
    "buy_types",
    "num_types",
    "primary_type",
    "primary_label",
    "diagnostic_type",
    "diagnostic_label",
    "max_score",
    "bear_case",
    "type1_score",
    "type2_score",
    "type3_score",
    "type4_score",
    "type5_score",
    "type6_score",
    "type7_score",
    "type1",
    "type2",
    "type3",
    "type4",
    "type5",
    "type6",
    "type7",
]


def screen_all_types(
    fin_map: Mapping[str, Any],
    quotes_df: pd.DataFrame,
    dcf_results: Optional[Mapping[str, Any]] = None,
    progress_cb=None,
    market_coldness_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    dcf_skip_classifications: Optional[Mapping[str, Mapping[str, Any]]] = None,
    output_codes: Optional[Iterable[Any]] = None,
    quality_history_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    quality_history_loader=None,
    quality_history_progress_cb=None,
    type3_growth_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    type3_growth_loader=None,
    type3_growth_progress_cb=None,
    research_report_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    research_report_loader=None,
    research_report_progress_cb=None,
    patch4_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
    patch4_loader=None,
    patch4_progress_cb=None,
) -> pd.DataFrame:
    """对合格沪深股票评分；输出按代码稳定排序，并提供完整七类结构。

    ``output_codes`` is an audit-only projection.  Metrics, peer cohorts and
    sector benchmarks are still built from the complete ``fin_map`` universe,
    but only the requested identities are scored and returned.  This preserves
    the production benchmark contract without needlessly rescoring roughly
    five thousand companies during a fixed-sample replay.
    """
    if not fin_map or quotes_df is None or quotes_df.empty or "code" not in quotes_df:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    begin_industry_generation()
    dcf_results = dcf_results or {}
    canonical_fin = _canonicalize_mapping(fin_map, "财务")
    normalized_dcf = _canonicalize_mapping(dcf_results, "DCF")
    normalized_coldness = _canonicalize_mapping(market_coldness_evidence or {}, "市场冷度")
    normalized_quality_history = _canonicalize_mapping(quality_history_evidence or {}, "长期市场历史证据")
    normalized_type3_growth = _canonicalize_mapping(type3_growth_evidence or {}, "可持续增长证据")
    normalized_research_reports = _canonicalize_mapping(research_report_evidence or {}, "优质股权研报元数据")
    normalized_patch4 = _canonicalize_mapping(patch4_evidence or {}, "科技股股东文化公告证据")
    raw_dcf_skips = _canonicalize_mapping(dcf_skip_classifications or {}, "DCF跳过分类")
    normalized_dcf_skips: dict[str, dict[str, str]] = {}
    for code, value in raw_dcf_skips.items():
        classification = normalize_dcf_skip_classification(value)
        if classification is None:
            raise ValueError(f"DCF跳过分类无效:{code}")
        if code in normalized_dcf:
            raise ValueError(f"DCF结果与跳过分类冲突:{code}")
        normalized_dcf_skips[code] = classification
    quote_lookup: dict[str, pd.Series] = {}
    for _, quote in quotes_df.iterrows():
        code = _normalize_code(quote.get("code"))
        if not code:
            raise ValueError("行情代码为空")
        if code in quote_lookup:
            raise ValueError(f"行情代码归一化冲突:{code}")
        quote_lookup[code] = quote
    missing_quotes = sorted(set(canonical_fin) - set(quote_lookup))
    if missing_quotes:
        raise ValueError(f"财务全集中的公司缺少行情记录:{missing_quotes[:5]}")

    unknown_quality_history = sorted(set(normalized_quality_history) - set(canonical_fin))
    if unknown_quality_history:
        raise ValueError(f"长期市场历史证据包含不在财务全集中的代码:{unknown_quality_history[:5]}")
    for code, evidence in normalized_quality_history.items():
        if not isinstance(evidence, Mapping):
            raise ValueError(f"长期市场历史证据必须为映射:{code}")
    unknown_type3_growth = sorted(set(normalized_type3_growth) - set(canonical_fin))
    if unknown_type3_growth:
        raise ValueError(f"可持续增长证据包含不在财务全集中的代码:{unknown_type3_growth[:5]}")
    for code, evidence in normalized_type3_growth.items():
        if not isinstance(evidence, Mapping):
            raise ValueError(f"可持续增长证据必须为映射:{code}")
    unknown_research_reports = sorted(set(normalized_research_reports) - set(canonical_fin))
    if unknown_research_reports:
        raise ValueError(f"优质股权研报元数据包含不在财务全集中的代码:{unknown_research_reports[:5]}")
    for code, evidence in normalized_research_reports.items():
        if not isinstance(evidence, Mapping):
            raise ValueError(f"优质股权研报元数据必须为映射:{code}")
    unknown_patch4 = sorted(set(normalized_patch4) - set(canonical_fin))
    if unknown_patch4:
        raise ValueError(f"科技股股东文化公告证据包含不在财务全集中的代码:{unknown_patch4[:5]}")
    for code, evidence in normalized_patch4.items():
        if not isinstance(evidence, Mapping):
            raise ValueError(f"科技股股东文化公告证据必须为映射:{code}")

    metrics: list[dict[str, Any]] = []
    codes = sorted(canonical_fin)
    industry_inputs = [(code, str(quote_lookup[code].get("name", ""))) for code in codes if code in quote_lookup]
    industry_by_code = (
        classify_industries(industry_inputs)
        if classify_industry is _DEFAULT_INDUSTRY_CLASSIFIER
        else {code: classify_industry(code, name) for code, name in industry_inputs}
    )
    selected_codes: set[str] | None = None
    if output_codes is not None:
        if isinstance(output_codes, (str, bytes)):
            raise ValueError("输出代码必须是代码集合，不能是字符串")
        selected_codes = {_normalize_code(value) for value in output_codes}
        selected_codes.discard("")
        if not selected_codes:
            raise ValueError("输出代码不能为空")
        unknown_outputs = sorted(selected_codes - set(codes))
        if unknown_outputs:
            raise ValueError(f"输出代码不在财务全集中:{unknown_outputs[:5]}")
    for index, code in enumerate(codes, start=1):
        quote = quote_lookup.get(code)
        if quote is not None:
            industry = industry_by_code[code]
            metric = extract_metrics(canonical_fin[code], quote, industry)
            metric["code"] = code
            coldness = normalized_coldness.get(code)
            if coldness is not None:
                if not isinstance(coldness, Mapping):
                    raise ValueError(f"市场冷度记录必须为映射:{code}")
                try:
                    score = validate_market_coldness_evidence_record(
                        coldness,
                        expected_code=code,
                        expected_session=metric.get("source_trade_date"),
                    )
                except (MarketColdnessScoringError, TypeError, ValueError) as exc:
                    raise ValueError(f"市场冷度证据无效:{code}") from exc
                evidence = coldness.get("market_coldness_score_evidence")
                components = coldness.get("components")
                if not isinstance(evidence, Mapping) or not isinstance(components, Mapping):
                    raise ValueError(f"市场冷度证据无效:{code}")
                metric["market_coldness_score"] = score
                metric["market_coldness_score_evidence"] = dict(evidence)
                metric["market_coldness_score_evidence_level"] = coldness["market_coldness_score_evidence_level"]
                metric["market_coldness_components"] = dict(components)
            report_evidence = normalized_research_reports.get(code)
            if report_evidence is not None:
                as_of = str(metric.get("source_trade_date") or "")
                sources, content_verification = _type7_research_sources_from_evidence(
                    report_evidence,
                    code=code,
                    as_of=as_of,
                )
                metric["type7_research_sources"] = sources
                metric["type7_research_content_verification"] = content_verification
            growth_evidence = normalized_type3_growth.get(code)
            if growth_evidence is not None:
                as_of = str(metric.get("source_trade_date") or "")
                external, segments = _type3_growth_components_from_evidence(
                    growth_evidence,
                    code=code,
                    as_of=as_of,
                )
                metric["external_growth_evidence"] = external
                metric["segment_growth_sources"] = segments
                metric["_type3_growth_validation_token"] = TYPE3_GROWTH_VALIDATION_TOKEN
            patch4_record = normalized_patch4.get(code)
            if patch4_record is not None:
                as_of = str(metric.get("source_trade_date") or "")
                assessment = _type7_patch4_assessment_from_evidence(
                    patch4_record,
                    code=code,
                    as_of=as_of,
                )
                if assessment is None:
                    metric.pop("type7_patch4_assessment", None)
                else:
                    metric["type7_patch4_assessment"] = assessment
                metric["_type7_patch4_evidence_status"] = str(patch4_record.get("status") or "")
            else:
                # Production scoring accepts Patch 4 only through the
                # independently replayed announcement record.  A naked
                # assessment embedded in financial/user input is not trusted.
                metric.pop("type7_patch4_assessment", None)
            metrics.append(metric)
        if progress_cb:
            progress_cb(index, len(codes))
    if not metrics:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    benchmarks = build_sector_benchmarks(metrics)
    peer_contexts, _evidence_by_code = enrich_metrics(
        metrics,
        benchmarks,
        target_codes=selected_codes,
    )
    for metric in metrics:
        code = str(metric.get("code") or "")
        metric["_quantitative_peer_context"] = peer_contexts.get(code, {})
        if selected_codes is None or code in selected_codes:
            _prepare_dcf_validation_cache(metric, normalized_dcf.get(code))

    scored_metrics = (
        metrics if selected_codes is None else [metric for metric in metrics if metric["code"] in selected_codes]
    )

    base_outcomes_by_code: dict[str, dict[str, tuple]] = {}
    preliminary_type7_by_code: dict[str, tuple[tuple, Mapping[str, Any]]] = {}
    type7_valuation_evidence_by_code: dict[str, bool] = {}
    type3_growth_request_by_code: dict[str, dict[str, Any]] = {}
    research_request_by_code: dict[str, dict[str, str]] = {}
    patch4_request_by_code: dict[str, dict[str, str]] = {}
    metric_by_code = {str(metric["code"]): metric for metric in scored_metrics}
    for m in scored_metrics:
        code = str(m["code"])
        company_benchmarks = _benchmarks_for_code(benchmarks, code)
        base_outcomes = {
            "type1": score_type1_dcf(
                m,
                normalized_dcf.get(code),
                company_benchmarks,
                normalized_dcf_skips.get(code),
            ),
            "type2": score_type2_two_hot_one_cold(m, company_benchmarks),
            "type3": score_type3_sustainable_growth(m, company_benchmarks),
            "type4": score_type4_long_runway(
                m,
                company_benchmarks,
                normalized_dcf.get(code),
                normalized_dcf_skips.get(code),
            ),
            "type5": score_type5_counter_cyclical(
                m,
                company_benchmarks,
                normalized_dcf.get(code),
                normalized_quality_history.get(code),
            ),
            "type6": score_type6_vc(m, company_benchmarks),
        }
        base_outcomes_by_code[code] = base_outcomes
        type7_valuation_evidence_by_code[code] = bool(
            str(m.get("industry") or "") not in FINANCIAL_INDUSTRIES
            and _valid_nonfinancial_dcf_evidence(m, normalized_dcf.get(code))
        )
        preliminary_outcome, preliminary_ledger = score_type7_quality_equity(
            m,
            base_outcomes["type1"],
            normalized_quality_history.get(code),
            valuation_evidence_complete=type7_valuation_evidence_by_code[code],
        )
        preliminary_type7_by_code[code] = (preliminary_outcome, preliminary_ledger)
        as_of = str(m.get("source_trade_date") or "")
        if (
            code not in normalized_patch4
            and _type7_patch4_request_needed(preliminary_ledger)
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of)
        ):
            patch4_request_by_code[code] = {"code": code, "as_of": as_of}
        if code not in normalized_type3_growth and _type3_growth_request_needed(m, company_benchmarks):
            growth_request = _type3_growth_request(m)
            if growth_request is not None:
                type3_growth_request_by_code[code] = growth_request
    type3_growth_requests = [type3_growth_request_by_code[code] for code in sorted(type3_growth_request_by_code)]
    if type3_growth_loader is not None and type3_growth_requests:
        loaded = type3_growth_loader(type3_growth_requests, progress_cb=type3_growth_progress_cb)
        if not isinstance(loaded, Mapping):
            raise TypeError("可持续增长证据加载器必须返回代码映射")
        normalized_loaded = _canonicalize_mapping(loaded, "可持续增长加载结果")
        requested_codes = {request["code"] for request in type3_growth_requests}
        unexpected = sorted(set(normalized_loaded) - requested_codes)
        if unexpected:
            raise ValueError(f"可持续增长加载结果包含未请求代码:{unexpected[:5]}")
        missing = sorted(requested_codes - set(normalized_loaded))
        if missing:
            raise ValueError(f"可持续增长加载结果遗漏请求代码:{missing[:5]}")
        for code, evidence in normalized_loaded.items():
            metric = metric_by_code[code]
            as_of = str(metric.get("source_trade_date") or "")
            external, segments = _type3_growth_components_from_evidence(
                evidence,
                code=code,
                as_of=as_of,
            )
            metric["external_growth_evidence"] = external
            metric["segment_growth_sources"] = segments
            metric["_type3_growth_validation_token"] = TYPE3_GROWTH_VALIDATION_TOKEN
            normalized_type3_growth[code] = evidence
            context = metric.get("_quantitative_peer_context")
            if not isinstance(context, Mapping):
                raise ValueError(f"可持续增长同行上下文缺失:{code}")
            company_benchmarks = _benchmarks_for_code(benchmarks, code)
            _refresh_type3_quantitative_evidence(metric, context, company_benchmarks)
            base_outcomes_by_code[code]["type3"] = score_type3_sustainable_growth(
                metric,
                company_benchmarks,
            )

    patch4_requests = [patch4_request_by_code[code] for code in sorted(patch4_request_by_code)]
    if patch4_loader is not None and patch4_requests:
        loaded = patch4_loader(patch4_requests, progress_cb=patch4_progress_cb)
        if not isinstance(loaded, Mapping):
            raise TypeError("科技股股东文化公告证据加载器必须返回代码映射")
        normalized_loaded = _canonicalize_mapping(loaded, "科技股股东文化公告证据加载结果")
        requested_codes = {request["code"] for request in patch4_requests}
        unexpected = sorted(set(normalized_loaded) - requested_codes)
        if unexpected:
            raise ValueError(f"科技股股东文化公告证据加载结果包含未请求代码:{unexpected[:5]}")
        missing = sorted(requested_codes - set(normalized_loaded))
        if missing:
            raise ValueError(f"科技股股东文化公告证据加载结果遗漏请求代码:{missing[:5]}")
        for code, evidence in normalized_loaded.items():
            metric = metric_by_code[code]
            as_of = str(metric.get("source_trade_date") or "")
            assessment = _type7_patch4_assessment_from_evidence(
                evidence,
                code=code,
                as_of=as_of,
            )
            if assessment is None:
                metric.pop("type7_patch4_assessment", None)
            else:
                metric["type7_patch4_assessment"] = assessment
            metric["_type7_patch4_evidence_status"] = str(evidence.get("status") or "")
            normalized_patch4[code] = evidence
            base_outcomes = base_outcomes_by_code[code]
            preliminary_type7_by_code[code] = score_type7_quality_equity(
                metric,
                base_outcomes["type1"],
                normalized_quality_history.get(code),
                valuation_evidence_complete=type7_valuation_evidence_by_code[code],
            )

    history_request_by_code: dict[str, dict[str, str]] = {}
    for m in scored_metrics:
        code = str(m["code"])
        _preliminary_outcome, preliminary_ledger = preliminary_type7_by_code[code]
        as_of = str(m.get("source_trade_date") or "")
        if (
            (
                preliminary_ledger.get("history_request_needed") is True
                or _type5_history_request_needed(m, base_outcomes_by_code[code]["type5"])
            )
            and code not in normalized_quality_history
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of)
        ):
            history_request_by_code[code] = {"code": code, "as_of": as_of}
    history_requests = [history_request_by_code[code] for code in sorted(history_request_by_code)]
    newly_loaded_history_codes: set[str] = set()
    if quality_history_loader is not None and history_requests:
        loaded = quality_history_loader(history_requests, progress_cb=quality_history_progress_cb)
        if not isinstance(loaded, Mapping):
            raise TypeError("长期市场历史证据加载器必须返回代码映射")
        normalized_loaded = _canonicalize_mapping(loaded, "长期市场历史加载结果")
        requested_codes = {request["code"] for request in history_requests}
        unexpected = sorted(set(normalized_loaded) - requested_codes)
        if unexpected:
            raise ValueError(f"长期市场历史加载结果包含未请求代码:{unexpected[:5]}")
        for code, evidence in normalized_loaded.items():
            if not isinstance(evidence, Mapping):
                raise ValueError(f"长期市场历史证据必须为映射:{code}")
            normalized_quality_history[code] = evidence
            newly_loaded_history_codes.add(code)

    # Exact long-horizon history materially changes all three Type 7 ledgers.
    # Recompute it before deciding whether report metadata is worth requesting;
    # metadata is never fetched merely because a no-history optimistic ceiling
    # could pass.
    for code in sorted(newly_loaded_history_codes):
        metric = metric_by_code[code]
        company_benchmarks = _benchmarks_for_code(benchmarks, code)
        base_outcomes = base_outcomes_by_code[code]
        base_outcomes["type5"] = score_type5_counter_cyclical(
            metric,
            company_benchmarks,
            normalized_dcf.get(code),
            normalized_quality_history.get(code),
        )
        preliminary_type7_by_code[code] = score_type7_quality_equity(
            metric,
            base_outcomes["type1"],
            normalized_quality_history.get(code),
            valuation_evidence_complete=type7_valuation_evidence_by_code[code],
        )

    for metric in scored_metrics:
        code = str(metric["code"])
        _preliminary_outcome, preliminary_ledger = preliminary_type7_by_code[code]
        as_of = str(metric.get("source_trade_date") or "")
        if preliminary_ledger.get("research_request_needed") is True and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of
        ):
            research_request_by_code[code] = {"code": code, "as_of": as_of}

    research_requests = [research_request_by_code[code] for code in sorted(research_request_by_code)]
    if research_report_loader is not None and research_requests:
        loaded = research_report_loader(research_requests, progress_cb=research_report_progress_cb)
        if not isinstance(loaded, Mapping):
            raise TypeError("优质股权研报元数据加载器必须返回代码映射")
        normalized_loaded = _canonicalize_mapping(loaded, "优质股权研报元数据加载结果")
        requested_codes = {request["code"] for request in research_requests}
        unexpected = sorted(set(normalized_loaded) - requested_codes)
        if unexpected:
            raise ValueError(f"优质股权研报元数据加载结果包含未请求代码:{unexpected[:5]}")
        missing = sorted(requested_codes - set(normalized_loaded))
        if missing:
            raise ValueError(f"优质股权研报元数据加载结果遗漏请求代码:{missing[:5]}")
        for code, evidence in normalized_loaded.items():
            metric = metric_by_code[code]
            as_of = str(metric.get("source_trade_date") or "")
            sources, content_verification = _type7_research_sources_from_evidence(
                evidence,
                code=code,
                as_of=as_of,
            )
            metric["type7_research_sources"] = sources
            metric["type7_research_content_verification"] = content_verification
            normalized_research_reports[code] = evidence
            base_outcomes = base_outcomes_by_code[code]
            preliminary_type7_by_code[code] = score_type7_quality_equity(
                metric,
                base_outcomes["type1"],
                normalized_quality_history.get(code),
                valuation_evidence_complete=type7_valuation_evidence_by_code[code],
            )

    def score_one(m: Mapping[str, Any]) -> dict[str, Any]:
        code = str(m["code"])
        outcomes = dict(base_outcomes_by_code[code])
        type7_outcome, type7_ledger = preliminary_type7_by_code[code]
        outcomes["type7"] = type7_outcome
        market_context = _decision_market_context(m)
        market_block = _market_trigger_block_reason(m)
        if market_block:
            gated: dict[str, tuple] = {}
            for key, (_triggered, total, sub_scores, raw_reasons) in outcomes.items():
                reasons = dict(raw_reasons)
                reasons[_DECISION_MARKET_BLOCK_REASON] = _compact_reason(market_block)
                # A market-wide actionability block must not rewrite a model
                # that was already N/A, missing evidence, vetoed or blocked.
                # In particular, missing valuation data must remain missing
                # instead of being disguised as a company-level veto.
                if reasons.get("_status") in {
                    STATUS_NOT_APPLICABLE,
                    STATUS_INSUFFICIENT_EVIDENCE,
                    STATUS_VETOED,
                    STATUS_BLOCKED,
                }:
                    gated[key] = (False, total, sub_scores, reasons)
                    continue
                reasons["_veto"] = _compact_reason(market_block)
                reasons["_status"] = STATUS_BLOCKED
                gated[key] = (False, total, sub_scores, reasons)
            outcomes = gated
        # Custom audit/test runners may still return the historical four-tuple
        # without status metadata.  Normalize it once at this boundary so every
        # exported payload obeys the new explicit-state contract.
        normalized_outcomes: dict[str, tuple] = {}
        for key, (triggered, total, sub_scores, raw_reasons) in outcomes.items():
            reasons = dict(raw_reasons)
            if reasons.get("_status") not in TYPE_STATUSES:
                if triggered:
                    status = STATUS_TRIGGERED
                elif reasons.get("_veto"):
                    status = STATUS_VETOED
                elif total >= QUALIFY_THRESHOLD and reasons.get("_condition"):
                    status = STATUS_CONDITIONAL
                elif total >= 5.0:
                    status = STATUS_OBSERVE
                else:
                    status = STATUS_NOT_TRIGGERED
                reasons["_status"] = status
            reasons.setdefault("_applicable", "yes")
            reasons.setdefault("_evidence", "complete")
            normalized_outcomes[key] = (triggered, total, sub_scores, reasons)
        outcomes = normalized_outcomes
        qualifiers = [key for key in TYPE_PRIORITY if outcomes[key][0]]
        totals = {key: outcome[1] for key, outcome in outcomes.items()}
        diagnostic_keys = [
            key
            for key in TYPE_PRIORITY
            if outcomes[key][3].get("_status") not in {STATUS_NOT_APPLICABLE, STATUS_INSUFFICIENT_EVIDENCE}
        ]
        top_score = max((totals[key] for key in diagnostic_keys), default=None)
        diagnostic = next(key for key in diagnostic_keys if totals[key] == top_score) if top_score is not None else None
        primary = qualifiers[0] if qualifiers else None
        payloads: dict[str, dict] = {}
        for key, (triggered, total, sub_scores, reasons) in outcomes.items():
            status = str(reasons.get("_status") or STATUS_NOT_TRIGGERED)
            payloads[key] = {
                "triggered": bool(triggered),
                "total": total,
                "sub_scores": sub_scores,
                "reasons": reasons,
                "veto": bool(reasons.get("_veto")),
                "status": status,
                "applicable": status != STATUS_NOT_APPLICABLE,
                "evidence_complete": reasons.get("_evidence") == "complete",
                _DECISION_MARKET_CONTEXT: market_context,
            }
            if key == "type5":
                bottom_mode = "incomplete"
                if status == STATUS_NOT_APPLICABLE:
                    bottom_mode = "not_applicable"
                else:
                    direct_score, direct_reason = _type5_external_score(m, "type5_bottom_signal_score")
                    direct_reason = direct_reason or "周期底部外部证据"
                    direct_used = bool(
                        direct_score is not None
                        and math.isclose(float(sub_scores.get("5b", -1.0)), direct_score, abs_tol=1e-9)
                        and reasons.get("5b") == direct_reason
                    )
                    if direct_used:
                        bottom_mode = "trusted_external"
                    else:
                        bottom_contract = _type5_automatic_bottom_contract(
                            m,
                            normalized_quality_history.get(code),
                        )
                        bottom_replay = replay_type5_bottom_evidence_contract(
                            bottom_contract,
                            expected_code=code,
                            expected_as_of=m.get("source_trade_date"),
                        )
                        if (
                            bottom_replay is not None
                            and math.isclose(
                                float(sub_scores.get("5b", -1.0)),
                                float(bottom_replay["score"]),
                                abs_tol=1e-9,
                            )
                            and reasons.get("5b") == bottom_replay["reason"]
                        ):
                            bottom_mode = "automatic_replay"
                            payloads[key]["bottom_evidence_contract"] = bottom_contract
                payloads[key]["bottom_evidence_mode"] = bottom_mode
            if key == "type7":
                payloads[key]["ledger"] = type7_ledger
            payloads[key]["decision"] = replay_buy_decision(key, payloads[key])
        row = {
            "code": code,
            "name": m.get("name"),
            "source_trade_date": m.get("source_trade_date"),
            "price": m.get("price"),
            "pe": m.get("pe"),
            "pb": m.get("pb"),
            "market_cap": m.get("market_cap"),
            "industry": m.get("industry"),
            "cagr_3yr": m.get("cagr_3yr"),
            "roe": m.get("roe"),
            "net_margin": m.get("net_margin"),
            "debt_ratio": m.get("debt_ratio"),
            "peg": m.get("peg"),
            "market_coldness_score": m.get("market_coldness_score"),
            "market_coldness_evidence": m.get("market_coldness_score_evidence"),
            "financial_sector_evidence": m.get("financial_sector_evidence"),
            "quantitative_model_id": QUANTITATIVE_EVIDENCE_MODEL_ID,
            "quantitative_evidence": m.get("quantitative_evidence"),
            "quantitative_evidence_levels": m.get("quantitative_evidence_levels"),
            "quantitative_evidence_status": m.get("quantitative_evidence_status"),
            "buy_types": qualifiers,
            "num_types": len(qualifiers),
            "primary_type": primary,
            "primary_label": TYPE_NAMES[primary] if primary else "无触发（不买）",
            "diagnostic_type": diagnostic,
            "diagnostic_label": TYPE_NAMES[diagnostic] if diagnostic else "无可完整诊断框架",
            "max_score": top_score,
            "bear_case": _build_bear_case(diagnostic, payloads[diagnostic]) if diagnostic else [],
        }
        for evidence_key in QUANTITATIVE_SCORE_KEYS:
            row[evidence_key] = m.get(evidence_key)
            row[f"{evidence_key}_evidence"] = m.get(f"{evidence_key}_evidence")
            row[f"{evidence_key}_evidence_level"] = m.get(f"{evidence_key}_evidence_level")
        for key, total in totals.items():
            row[f"{key}_score"] = total
            row[key] = payloads[key]
        return row

    # These score functions are Python/GIL-bound.  A 4,986-company production
    # benchmark measured 32/8/1 worker pools at 66.67/66.55/64.44 seconds with
    # byte-identical output; constructing thousands of futures only adds
    # scheduling overhead.  Keep the deterministic direct loop.
    rows = [score_one(metric) for metric in scored_metrics]
    rows.sort(key=lambda row: _normalize_code(row["code"]))
    result = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    # Pandas otherwise coerces ``None`` to floating NaN when some rows have a
    # primary framework and others correctly mean "no triggered buy".
    result["primary_type"] = result["primary_type"].astype(object)
    result.loc[result["primary_type"].isna(), "primary_type"] = None
    result["diagnostic_type"] = result["diagnostic_type"].astype(object)
    result.loc[result["diagnostic_type"].isna(), "diagnostic_type"] = None
    invariant_errors = validate_screening_result(result)
    if invariant_errors:
        raise AssertionError("评分结果不变量失败: " + ";".join(invariant_errors[:5]))
    return result
