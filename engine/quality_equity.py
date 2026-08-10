"""Formula ledger for Patch 6 Type 7 (优质股权型).

Type 7 is not a fourth composite invented by the program.  The source rule is
an intersection: Template 1, Template 5 and Patch 5 must each score strictly
above 70.  This module preserves all three source weight systems, exposes every
proxy and prerequisite, and supplies upper bounds used to avoid needless
long-history network requests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import math
import re
from statistics import mean, median
from typing import Any
from urllib.parse import urlsplit

from data.quality_history import replay_valuation_distribution


MODEL_ID = "patch6-type7-quality-equity-v7"
SCHEMA_VERSION = 7
STRICT_THRESHOLD = 70.0
PATCH5_SAFETY_VETO = 8.0
MIN_CORE_COVERAGE = 0.80
PATCH4_MODEL_ID = "patch4-technology-shareholder-culture-v1"
PATCH4_SCHEMA_VERSION = 1
PATCH4_FORMULA_VERSION = "patch4-two-layer-weighted-v1"
PATCH4_MAX_EVIDENCE_AGE_DAYS = 1_095
MIN_RESEARCH_SOURCES = 3
RESEARCH_MAX_AGE_DAYS = 365
RESEARCH_RECENT_AGE_DAYS = 183
RESEARCH_EVIDENCE_MODEL_ID = "type7-research-report-content-v4"
RESEARCH_CONTENT_MODEL_ID = "type7-report-body-crosscheck-v2"
MIN_RESEARCH_BODY_SOURCES = 3
MIN_CROSSCHECK_REPORTS = 2
MIN_RESEARCH_BODY_CHARACTERS = 200
MAX_RESEARCH_BODY_CHARACTERS = 100_000
MAX_RESEARCH_BODY_FETCHES = 6
RESEARCH_FACT_RELATIVE_TOLERANCE = 0.02
MAX_RESEARCH_FACTS_PER_BODY = 32
MAX_RESEARCH_FACT_ABS_VALUE = 1_000_000_000.0
EXPECTED_RETURN_HORIZON_YEARS = 5
TERMINAL_PROFIT_HORIZON_YEARS = 10
TERMINAL_GROWTH_RATE = 0.03
FORECAST_GROWTH_FLOOR = -0.10
FORECAST_GROWTH_CAP = 0.20
LONG_HORIZON_HISTORY_MODEL_ID = "type7-market-history-v1"
FINANCIAL_EVIDENCE_MAX_AGE_DAYS = 550
TEN_YEAR_TARGET_DAYS = 3_652
TEN_YEAR_START_TOLERANCE_DAYS = 62
FIVE_YEAR_TARGET_DAYS = 1_826
FIVE_YEAR_START_TOLERANCE_DAYS = 62
VALUATION_MIN_OBSERVATIONS = 500
HISTORY_LATEST_MAX_AGE_DAYS = 21
SHAREHOLDER_RETURN_FORMULA = "total=end_hfq/start_hfq-1;cagr=(end_hfq/start_hfq)^(365.2425/days)-1"
VALUATION_PERCENTILE_FORMULA = "percentile=(count(x<current)+0.5*count(x=current))/historical_count"

_TEMPLATE1_ITEM_WEIGHTS = {f"t1_{index:02d}": 5.0 for index in range(1, 21)}
_TEMPLATE5_ITEM_WEIGHTS = {
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
}
_PATCH5_COMPONENT_WEIGHTS = {
    "p5_business": {"p5_b1": 5.0, "p5_b2": 5.0, "p5_b3": 5.0, "p5_b4": 5.0},
    "p5_moat": {"p5_m1": 8.0, "p5_m2": 6.0, "p5_m3": 6.0},
    "p5_culture": {"p5_c1": 6.0, "p5_c2": 5.0, "p5_c3": 5.0, "p5_c4": 4.0},
    "p5_industry": {"p5_i1": 8.0, "p5_i2": 6.0, "p5_i3": 6.0},
    "p5_safety": {"p5_s1": 8.0, "p5_s2": 6.0, "p5_s3": 6.0},
}
_TEMPLATE1_ITEM_CONTRACTS = {
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
_TEMPLATE5_ITEM_LABELS = {
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
_TEMPLATE_EVIDENCE_LEVELS = {
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
_PATCH5_SECTION_LABELS = {
    "p5_business": "商业模式",
    "p5_moat": "护城河",
    "p5_culture": "公司文化",
    "p5_industry": "产业兴衰",
    "p5_safety": "安全边际",
}
_PATCH5_COMPONENT_LABELS = {
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
_PATCH5_SOURCE_INPUT_COMPONENTS = {
    "p5_b1",
    "p5_b3",
    "p5_m2",
    "p5_m3",
    "p5_c3",
    "p5_c4",
    "p5_i3",
    "p5_s3",
}
_PATCH5_SOURCE_LEVELS = {"missing", "primary", "derived_proxy"}
_PATCH4_CRITERION_FIELDS = {
    "core_rd_ownership_pct",
    "esop_core_talent_coverage_pct",
    "long_term_rd_metrics",
    "frontline_rd_equity",
    "short_term_price_binding",
}
_PATCH4_EVIDENCE_FIELDS = {"source", "evidence_id", "url", "as_of", "summary"}
_PATCH4_COMPONENT_WEIGHTS = {
    "p4_defensive_fairness": 25.0,
    "p4_defensive_governance": 15.0,
    "p4_core_rd_ownership": 15.0,
    "p4_esop_coverage": 15.0,
    "p4_long_term_rd_link": 15.0,
    "p4_frontline_rd_equity": 10.0,
    "p4_short_term_binding": 5.0,
}
_PATCH4_COMPONENT_LABELS = {
    "p4_defensive_fairness": "大小股东公平",
    "p4_defensive_governance": "治理透明与分红约束",
    "p4_core_rd_ownership": "核心研发持股",
    "p4_esop_coverage": "核心人才持股覆盖",
    "p4_long_term_rd_link": "长期研发指标绑定",
    "p4_frontline_rd_equity": "一线研发权益",
    "p4_short_term_binding": "短期股价绑定防范",
}
_PREREQUISITE_KEYS = {
    "core_modules_80pct",
    "technology_patch4",
    "three_year_financials",
    "latest_quote_and_valuation",
    "three_external_reports",
    "external_report_content_verification",
    "ten_year_return_and_five_year_valuation",
}

TYPE7_DIRECT_SCORE_KEYS = (
    "business_clarity_score",
    "customer_stickiness_score",
    "employee_culture_score",
    "entry_barrier_score",
    "external_environment_score",
    "governance_score",
    "innovation_adaptability_score",
    "luxury_attribute_score",
    "pricing_power_score",
    "shareholder_fairness_score",
    "downside_protection_score",
)

RESEARCH_SOURCE_FIELDS = {
    "security_code",
    "company_name",
    "title",
    "publisher",
    "publisher_id",
    "url",
    "as_of",
    "evidence_id",
}
RESEARCH_CONTENT_BODY_FIELDS = {
    "evidence_id",
    "content_sha256",
    "content_length",
    "paragraph_count",
    "structure_signals",
    "fact_count",
    "facts",
    "identity_checks",
}
RESEARCH_CONTENT_FACT_FIELDS = {"fact_key", "period", "metric", "unit", "value"}
RESEARCH_CONTENT_IDENTITY_CHECKS = {
    "code_in_body",
    "name_in_body",
    "detail_code",
    "detail_name",
    "detail_title",
    "detail_publisher",
    "detail_date",
    "dom_json_body",
}
RESEARCH_CONTENT_SIGNALS = {
    "analysis",
    "event",
    "forecast",
    "investment_view",
    "risk",
}
RESEARCH_FACT_METRICS = {
    "adjusted_net_profit",
    "eps",
    "operating_cash_flow",
    "parent_net_profit",
    "revenue",
}
RESEARCH_FACT_UNITS = {"CNY_100M", "CNY_PER_SHARE"}
RESEARCH_FACT_UNIT_BY_METRIC = {
    "adjusted_net_profit": "CNY_100M",
    "eps": "CNY_PER_SHARE",
    "operating_cash_flow": "CNY_100M",
    "parent_net_profit": "CNY_100M",
    "revenue": "CNY_100M",
}
MAX_RESEARCH_SOURCES = 20
MAX_RESEARCH_TEXT = 300
_HTTPS = re.compile(r"^https://", re.IGNORECASE)
_A_SHARE_CODE = re.compile(r"^[036][0-9]{5}$")


class QualityEquityError(ValueError):
    """A Type 7 input or ledger invariant failed."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, lower: float = 0.0, upper: float = 10.0) -> float:
    return min(upper, max(lower, float(value)))


def _round_score(value: float) -> float:
    return round(_clip(value), 2)


def _linear(value: float | None, anchors: Sequence[tuple[float, float]], *, missing: float = 2.0) -> float:
    if value is None:
        return _round_score(missing)
    ordered = list(anchors)
    if value <= ordered[0][0]:
        return _round_score(ordered[0][1])
    if value >= ordered[-1][0]:
        return _round_score(ordered[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            if right_x == left_x:
                return _round_score(right_y)
            fraction = (value - left_x) / (right_x - left_x)
            return _round_score(left_y + fraction * (right_y - left_y))
    raise AssertionError("linear score anchors did not cover the input")


def _avg(*values: float | None, missing: float = 2.0) -> float:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return _round_score(mean(clean) if clean else missing)


def _parse_evidence_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def _verified_score(metric: Mapping[str, Any], key: str) -> tuple[float | None, bool, str]:
    score = _finite(metric.get(key))
    evidence = metric.get(f"{key}_evidence")
    if score is None or not 0 <= score <= 10 or not isinstance(evidence, Mapping):
        return None, False, "missing"
    required = {"source", "evidence_id", "as_of"}
    if not required.issubset(evidence) or set(evidence) - (required | {"summary"}):
        return None, False, "missing"
    if any(
        not isinstance(evidence.get(field), str)
        or not str(evidence.get(field)).strip()
        or len(str(evidence.get(field))) > MAX_RESEARCH_TEXT
        or any(ord(character) < 32 for character in str(evidence.get(field)))
        for field in required
    ):
        return None, False, "missing"
    summary = evidence.get("summary")
    if summary is not None and (
        not isinstance(summary, str) or len(summary) > 1_000 or any(ord(character) < 32 for character in summary)
    ):
        return None, False, "missing"
    evidence_date = _parse_evidence_date(evidence.get("as_of"))
    metric_as_of = _parse_evidence_date(metric.get("source_trade_date"))
    if evidence_date is None or metric_as_of is None or metric_as_of > date.today() or evidence_date > metric_as_of:
        return None, False, "missing"
    raw_level = metric.get(f"{key}_evidence_level")
    level = str(raw_level) if isinstance(raw_level, str) else "missing"
    if level not in {"primary", "derived_proxy"}:
        return None, False, level
    return float(score), True, level


def _patch4_evidence_id_is_bound(evidence_id: str, code: str) -> bool:
    """Require every Patch 4 fact identifier to carry the assessed security."""

    tokens = {token for token in re.split(r"[^0-9]+", evidence_id) if re.fullmatch(r"[0-9]{6}", token)}
    return code in tokens


def normalise_patch4_assessment(
    value: Any,
    *,
    security_code: str,
    as_of: str,
) -> dict[str, Any]:
    """Validate raw, source-bound Patch 4 facts before deriving any score.

    Patch 4 asks questions that cannot be inferred from ordinary financial
    statements: ownership by core researchers, ESOP coverage, and whether
    vesting conditions reward long-horizon R&D rather than a short-term share
    price.  The input contract therefore carries the atomic facts and their
    public-document identities; callers are never allowed to submit a finished
    0-10 score.
    """

    if not _A_SHARE_CODE.fullmatch(str(security_code or "")):
        raise QualityEquityError("Patch 4 security code is invalid")
    reference = _parse_evidence_date(as_of)
    if reference is None or reference > date.today():
        raise QualityEquityError("Patch 4 assessment date is invalid")
    expected_fields = {"schema_version", "model_id", "code", "as_of", "criteria"}
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise QualityEquityError("Patch 4 assessment schema is invalid")
    if (
        value.get("schema_version") != PATCH4_SCHEMA_VERSION
        or value.get("model_id") != PATCH4_MODEL_ID
        or value.get("code") != security_code
        or value.get("as_of") != as_of
    ):
        raise QualityEquityError("Patch 4 assessment identity is invalid")
    criteria = value.get("criteria")
    if not isinstance(criteria, Mapping) or set(criteria) != _PATCH4_CRITERION_FIELDS:
        raise QualityEquityError("Patch 4 assessment criteria are incomplete")

    normalized: dict[str, dict[str, Any]] = {}
    for key in sorted(_PATCH4_CRITERION_FIELDS):
        record = criteria.get(key)
        if not isinstance(record, Mapping) or set(record) != {"value", "evidence"}:
            raise QualityEquityError(f"Patch 4 criterion schema is invalid: {key}")
        raw_value = record.get("value")
        if key in {"core_rd_ownership_pct", "esop_core_talent_coverage_pct"}:
            number = _finite(raw_value)
            if (
                number is None
                or isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or not 0 <= number <= 100
            ):
                raise QualityEquityError(f"Patch 4 percentage is invalid: {key}")
            criterion_value: float | bool = round(number, 6)
        else:
            if not isinstance(raw_value, bool):
                raise QualityEquityError(f"Patch 4 boolean is invalid: {key}")
            criterion_value = raw_value

        evidence = record.get("evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != _PATCH4_EVIDENCE_FIELDS:
            raise QualityEquityError(f"Patch 4 evidence schema is invalid: {key}")
        if any(not isinstance(evidence.get(field), str) for field in _PATCH4_EVIDENCE_FIELDS):
            raise QualityEquityError(f"Patch 4 evidence fields must be strings: {key}")
        clean_evidence = {field: str(evidence[field]).strip() for field in _PATCH4_EVIDENCE_FIELDS}
        if any(
            not text or len(text) > 1_000 or any(ord(character) < 32 for character in text)
            for text in clean_evidence.values()
        ):
            raise QualityEquityError(f"Patch 4 evidence text is invalid: {key}")
        parsed = urlsplit(clean_evidence["url"])
        try:
            port = parsed.port
        except ValueError as exc:
            raise QualityEquityError(f"Patch 4 evidence URL is invalid: {key}") from exc
        evidence_date = _parse_evidence_date(clean_evidence["as_of"])
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.fragment)
            or evidence_date is None
            or evidence_date > reference
            or (reference - evidence_date).days > PATCH4_MAX_EVIDENCE_AGE_DAYS
            or not _patch4_evidence_id_is_bound(clean_evidence["evidence_id"], security_code)
        ):
            raise QualityEquityError(f"Patch 4 evidence is unbound, stale, or unsafe: {key}")
        normalized[key] = {"value": criterion_value, "evidence": clean_evidence}

    return {
        "schema_version": PATCH4_SCHEMA_VERSION,
        "model_id": PATCH4_MODEL_ID,
        "code": security_code,
        "as_of": as_of,
        "criteria": normalized,
    }


def _patch4_component(
    key: str,
    score: float,
    *,
    complete: bool,
    formula: str,
    inputs: Mapping[str, Any],
    evidence: Mapping[str, str] | None,
) -> dict[str, Any]:
    weight = _PATCH4_COMPONENT_WEIGHTS[key]
    normalized = _round_score(score)
    return {
        "key": key,
        "label": _PATCH4_COMPONENT_LABELS[key],
        "weight": weight,
        "score": normalized,
        "points": round(normalized * weight / 10.0, 4),
        "complete": bool(complete),
        "formula": formula,
        "inputs": dict(inputs),
        "evidence": dict(evidence) if evidence is not None else None,
    }


def _build_patch4_ledger(
    metric: Mapping[str, Any],
    values: Mapping[str, tuple[float, bool, str, Mapping[str, Any]]],
    *,
    code: str,
    as_of: str,
) -> dict[str, Any] | None:
    raw = metric.get("type7_patch4_assessment")
    if raw is None:
        return None
    facts = normalise_patch4_assessment(raw, security_code=code, as_of=as_of)
    criteria = facts["criteria"]

    fairness_score, fairness_complete, fairness_level, _ = values["shareholder"]
    governance_score, governance_complete, governance_level = _verified_score(metric, "governance_score")
    if governance_score is None:
        management_score, management_complete, management_level, _ = values["management"]
        governance_score = min(management_score, 7.0)
        governance_complete = management_complete
        governance_level = "derived_proxy" if management_complete else management_level

    ownership = criteria["core_rd_ownership_pct"]
    coverage = criteria["esop_core_talent_coverage_pct"]
    long_term = criteria["long_term_rd_metrics"]
    frontline = criteria["frontline_rd_equity"]
    short_term = criteria["short_term_price_binding"]
    ownership_score = _linear(
        float(ownership["value"]),
        [(0.0, 0.0), (1.0, 2.0), (3.0, 6.0), (5.0, 9.0), (5.000001, 10.0)],
    )
    coverage_score = _linear(
        float(coverage["value"]),
        [(0.0, 0.0), (10.0, 3.0), (20.0, 6.0), (30.0, 9.0), (30.000001, 10.0)],
    )
    components = [
        _patch4_component(
            "p4_defensive_fairness",
            fairness_score,
            complete=fairness_complete,
            formula="source_score(template1.t1_08)",
            inputs={"source_item": "template1.t1_08", "evidence_level": fairness_level},
            evidence=None,
        ),
        _patch4_component(
            "p4_defensive_governance",
            governance_score,
            complete=governance_complete,
            formula="verified_governance_or_capped_management_proxy",
            inputs={"source_item": "patch5.p5_c4", "evidence_level": governance_level},
            evidence=None,
        ),
        _patch4_component(
            "p4_core_rd_ownership",
            ownership_score,
            complete=True,
            formula="piecewise(core_rd_ownership_pct;5%+=10)",
            inputs={"value": ownership["value"], "unit": "percentage_points"},
            evidence=ownership["evidence"],
        ),
        _patch4_component(
            "p4_esop_coverage",
            coverage_score,
            complete=True,
            formula="piecewise(esop_core_talent_coverage_pct;30%+=10)",
            inputs={"value": coverage["value"], "unit": "percentage_points"},
            evidence=coverage["evidence"],
        ),
        _patch4_component(
            "p4_long_term_rd_link",
            10.0 if long_term["value"] else 0.0,
            complete=True,
            formula="10 if long_term_rd_metrics else 0",
            inputs={"value": long_term["value"]},
            evidence=long_term["evidence"],
        ),
        _patch4_component(
            "p4_frontline_rd_equity",
            10.0 if frontline["value"] else 0.0,
            complete=True,
            formula="10 if frontline_rd_equity else 0",
            inputs={"value": frontline["value"]},
            evidence=frontline["evidence"],
        ),
        _patch4_component(
            "p4_short_term_binding",
            0.0 if short_term["value"] else 10.0,
            complete=True,
            formula="0 if short_term_price_binding else 10",
            inputs={"value": short_term["value"]},
            evidence=short_term["evidence"],
        ),
    ]
    score = round(math.fsum(float(component["points"]) for component in components) / 10.0, 2)
    return {
        "schema_version": PATCH4_SCHEMA_VERSION,
        "model_id": PATCH4_MODEL_ID,
        "code": code,
        "as_of": as_of,
        "formula_version": PATCH4_FORMULA_VERSION,
        "score": score,
        "complete": all(bool(component["complete"]) for component in components),
        "components": components,
    }


def normalise_research_sources(
    value: Any,
    *,
    today: date | None = None,
    security_code: str | None = None,
    max_age_days: int = RESEARCH_MAX_AGE_DAYS,
) -> list[dict[str, str]]:
    """Validate the Patch 5 three-report prerequisite without fetching prose."""

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise QualityEquityError("type7_research_sources must be a list")
    if len(value) > MAX_RESEARCH_SOURCES:
        raise QualityEquityError("type7_research_sources exceeds the item limit")
    reference = today or date.today()
    if not isinstance(reference, date):
        raise QualityEquityError("Type 7 research reference date is invalid")
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or not 0 <= max_age_days <= 3_650:
        raise QualityEquityError("Type 7 research maximum age is invalid")
    expected_code = None if security_code is None else str(security_code).strip()
    if expected_code is not None and not _A_SHARE_CODE.fullmatch(expected_code):
        raise QualityEquityError("Type 7 research expected security code is invalid")
    normalized: list[dict[str, str]] = []
    identities: set[str] = set()
    urls: set[str] = set()
    security_codes: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != RESEARCH_SOURCE_FIELDS:
            raise QualityEquityError("each Type 7 research source must use the exact source schema")
        if any(not isinstance(raw.get(field), str) for field in RESEARCH_SOURCE_FIELDS):
            raise QualityEquityError("Type 7 research source fields must be strings")
        item = {field: str(raw.get(field) or "").strip() for field in RESEARCH_SOURCE_FIELDS}
        if any(
            not text or len(text) > MAX_RESEARCH_TEXT or any(ord(character) < 32 for character in text)
            for text in item.values()
        ):
            raise QualityEquityError("Type 7 research source text is empty or too long")
        parsed = urlsplit(item["url"])
        try:
            port = parsed.port
        except ValueError as exc:
            raise QualityEquityError("Type 7 research source URL contains an invalid port") from exc
        if (
            not _HTTPS.match(item["url"])
            or not parsed.hostname
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise QualityEquityError("Type 7 research source URL must be credential-free HTTPS")
        if not _A_SHARE_CODE.fullmatch(item["security_code"]):
            raise QualityEquityError("Type 7 research source security_code is invalid")
        if expected_code is not None and item["security_code"] != expected_code:
            raise QualityEquityError("Type 7 research source security_code does not match the company")
        as_of = _parse_evidence_date(item["as_of"])
        if as_of is None or as_of > reference:
            raise QualityEquityError("Type 7 research source as_of is invalid or in the future")
        if (reference - as_of).days > max_age_days:
            raise QualityEquityError("Type 7 research source is older than the permitted window")
        identity = item["evidence_id"].casefold()
        if identity in identities:
            raise QualityEquityError("Type 7 research sources contain duplicate evidence_id values")
        canonical_url = item["url"].casefold()
        if canonical_url in urls:
            raise QualityEquityError("Type 7 research sources contain duplicate report URLs")
        identities.add(identity)
        urls.add(canonical_url)
        security_codes.add(item["security_code"])
        normalized.append(item)
    if len(security_codes) > 1:
        raise QualityEquityError("Type 7 research sources contain multiple security codes")
    normalized.sort(key=lambda item: (item["as_of"], item["publisher_id"], item["publisher"], item["evidence_id"]))
    return normalized


def research_metadata_precheck(
    sources: Sequence[Mapping[str, str]],
    *,
    reference: date,
) -> dict[str, int | bool]:
    """Replay the report-metadata availability policy without claiming prose review."""

    if not isinstance(reference, date):
        raise QualityEquityError("Type 7 research reference date is invalid")
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise QualityEquityError("Type 7 research sources must be a sequence")
    publisher_ids: set[str] = set()
    recent_source_count = 0
    for source in sources:
        if not isinstance(source, Mapping):
            raise QualityEquityError("Type 7 research source is invalid")
        publisher_id = source.get("publisher_id")
        published = _parse_evidence_date(source.get("as_of"))
        if not isinstance(publisher_id, str) or not publisher_id.strip() or published is None:
            raise QualityEquityError("Type 7 research source metadata is invalid")
        age_days = (reference - published).days
        if age_days < 0 or age_days > RESEARCH_MAX_AGE_DAYS:
            raise QualityEquityError("Type 7 research source is outside the metadata window")
        publisher_ids.add(publisher_id.casefold())
        if age_days <= RESEARCH_RECENT_AGE_DAYS:
            recent_source_count += 1
    distinct_publishers = len(publisher_ids)
    return {
        "passed": bool(
            len(sources) >= MIN_RESEARCH_SOURCES
            and distinct_publishers >= MIN_RESEARCH_SOURCES
            and recent_source_count >= 1
        ),
        "source_count": len(sources),
        "distinct_publishers": distinct_publishers,
        "recent_source_count": recent_source_count,
    }


def _research_cross_check_from_bodies(bodies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Recompute the declared consensus solely from bounded atomic facts."""

    grouped: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for body in bodies:
        evidence_id = str(body["evidence_id"])
        for fact in body["facts"]:
            grouped.setdefault((fact["fact_key"], fact["unit"]), []).append((evidence_id, float(fact["value"])))
    candidates: list[tuple[int, float, str, str, float, list[str]]] = []
    for (fact_key, unit), observations in grouped.items():
        ordered = sorted(observations, key=lambda item: (item[1], item[0]))
        for left in range(len(ordered)):
            for right in range(left + MIN_CROSSCHECK_REPORTS, len(ordered) + 1):
                window = ordered[left:right]
                values = [value for _, value in window]
                center = math.fsum(values) / len(values)
                spread = (max(values) - min(values)) / max(abs(center), 1e-12)
                if spread <= RESEARCH_FACT_RELATIVE_TOLERANCE:
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
            "minimum_reports": MIN_CROSSCHECK_REPORTS,
            "fact_key": None,
            "fact_unit": None,
            "consensus_value": None,
            "supporting_evidence_ids": [],
            "max_relative_spread": None,
        }
    _, spread, fact_key, unit, center, evidence_ids = min(candidates)
    return {
        "passed": True,
        "minimum_reports": MIN_CROSSCHECK_REPORTS,
        "fact_key": fact_key,
        "fact_unit": unit,
        "consensus_value": round(center, 6),
        "supporting_evidence_ids": evidence_ids,
        "max_relative_spread": round(spread, 8),
    }


def normalise_research_content_verification(
    value: Any,
    *,
    sources: Sequence[Mapping[str, str]],
    security_code: str,
    as_of: str,
) -> dict[str, Any]:
    """Validate the bounded body-verification summary without accepting report prose."""

    expected_top_level = {
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
    if not isinstance(value, Mapping) or set(value) != expected_top_level:
        raise QualityEquityError("Type 7 report-content verification schema is invalid")
    if value.get("model_id") != RESEARCH_CONTENT_MODEL_ID:
        raise QualityEquityError("Type 7 report-content verification model is invalid")
    if value.get("code") != security_code or value.get("as_of") != as_of:
        raise QualityEquityError("Type 7 report-content verification identity is invalid")
    try:
        reference = date.fromisoformat(as_of)
    except (TypeError, ValueError) as exc:
        raise QualityEquityError("Type 7 report-content verification date is invalid") from exc
    if reference > date.today() or not _A_SHARE_CODE.fullmatch(security_code):
        raise QualityEquityError("Type 7 report-content verification identity is invalid")
    if not isinstance(value.get("passed"), bool) or value.get("required_bodies") != MIN_RESEARCH_BODY_SOURCES:
        raise QualityEquityError("Type 7 report-content verification decision is invalid")
    counts: dict[str, int] = {}
    for field in ("attempted_bodies", "verified_bodies", "distinct_publishers"):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise QualityEquityError("Type 7 report-content verification count is invalid")
        counts[field] = raw
    if (
        counts["attempted_bodies"] > MAX_RESEARCH_BODY_FETCHES
        or counts["verified_bodies"] > counts["attempted_bodies"]
        or counts["distinct_publishers"] > counts["verified_bodies"]
    ):
        raise QualityEquityError("Type 7 report-content verification count is inconsistent")
    reason = value.get("reason")
    if (
        not isinstance(reason, str)
        or len(reason) > MAX_RESEARCH_TEXT
        or any(ord(character) < 32 for character in reason)
    ):
        raise QualityEquityError("Type 7 report-content verification reason is invalid")

    normalized_sources = normalise_research_sources(
        sources,
        today=reference,
        security_code=security_code,
    )
    if counts["attempted_bodies"] > len(normalized_sources):
        raise QualityEquityError("Type 7 report-content attempt count exceeds source count")
    source_by_id = {source["evidence_id"]: source for source in normalized_sources}
    bodies = value.get("bodies")
    if not isinstance(bodies, list) or len(bodies) != counts["verified_bodies"]:
        raise QualityEquityError("Type 7 report-content body summaries are invalid")
    normalized_bodies: list[dict[str, Any]] = []
    body_hashes: set[str] = set()
    body_ids: set[str] = set()
    publisher_ids: set[str] = set()
    for body in bodies:
        if not isinstance(body, Mapping) or set(body) != RESEARCH_CONTENT_BODY_FIELDS:
            raise QualityEquityError("Type 7 report-content body summary schema is invalid")
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
            or not MIN_RESEARCH_BODY_CHARACTERS <= content_length <= MAX_RESEARCH_BODY_CHARACTERS
            or isinstance(paragraph_count, bool)
            or not isinstance(paragraph_count, int)
            or paragraph_count < 2
            or isinstance(fact_count, bool)
            or not isinstance(fact_count, int)
            or not isinstance(facts, list)
            or fact_count != len(facts)
            or len(facts) > MAX_RESEARCH_FACTS_PER_BODY
            or not isinstance(signals, list)
            or signals != sorted(set(signals))
            or not set(signals).issubset(RESEARCH_CONTENT_SIGNALS)
            or not signals
            or not isinstance(checks, Mapping)
            or set(checks) != RESEARCH_CONTENT_IDENTITY_CHECKS
            or any(checks.get(key) is not True for key in RESEARCH_CONTENT_IDENTITY_CHECKS)
        ):
            raise QualityEquityError("Type 7 report-content body summary is invalid")
        normalized_facts: list[dict[str, Any]] = []
        fact_identities: set[tuple[str, str]] = set()
        for fact in facts:
            if not isinstance(fact, Mapping) or set(fact) != RESEARCH_CONTENT_FACT_FIELDS:
                raise QualityEquityError("Type 7 report-content atomic fact schema is invalid")
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
                or metric not in RESEARCH_FACT_METRICS
                or fact_key != f"{period}:{metric}"
                or not isinstance(unit, str)
                or unit != RESEARCH_FACT_UNIT_BY_METRIC[metric]
                or isinstance(raw_fact_value, bool)
                or not isinstance(raw_fact_value, (int, float))
                or fact_value is None
                or abs(fact_value) > MAX_RESEARCH_FACT_ABS_VALUE
                or fact_value != round(fact_value, 6)
                or identity in fact_identities
            ):
                raise QualityEquityError("Type 7 report-content atomic fact is invalid")
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
            raise QualityEquityError("Type 7 report-content atomic facts are not canonical")
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
        raise QualityEquityError("Type 7 report-content body summaries are not canonical")

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
        or cross_check.get("minimum_reports") != MIN_CROSSCHECK_REPORTS
    ):
        raise QualityEquityError("Type 7 report-content cross-check schema is invalid")
    expected_cross_check = _research_cross_check_from_bodies(normalized_bodies)
    if dict(cross_check) != expected_cross_check:
        raise QualityEquityError("Type 7 report-content cross-check differs from atomic facts")

    expected_passed = bool(
        counts["verified_bodies"] >= MIN_RESEARCH_BODY_SOURCES
        and counts["distinct_publishers"] >= MIN_RESEARCH_BODY_SOURCES
        and expected_cross_check["passed"]
    )
    if value["passed"] is not expected_passed or bool(reason) is expected_passed:
        raise QualityEquityError("Type 7 report-content verification decision is inconsistent")
    return {
        "model_id": RESEARCH_CONTENT_MODEL_ID,
        "code": security_code,
        "as_of": as_of,
        "passed": expected_passed,
        "required_bodies": MIN_RESEARCH_BODY_SOURCES,
        "attempted_bodies": counts["attempted_bodies"],
        "verified_bodies": counts["verified_bodies"],
        "distinct_publishers": counts["distinct_publishers"],
        "bodies": normalized_bodies,
        "cross_check": expected_cross_check,
        "reason": reason,
    }


def _latest_complete_financial_year(metric: Mapping[str, Any]) -> int | None:
    """Return the annual endpoint justified by the quote/report snapshot."""

    trade_date = _parse_evidence_date(metric.get("source_trade_date"))
    financial_date = _parse_evidence_date(metric.get("financial_indicator_as_of"))
    today = date.today()
    if trade_date is not None and trade_date > today:
        return None
    if financial_date is not None:
        if (
            financial_date > today
            or (trade_date is not None and financial_date > trade_date)
            or (trade_date is not None and (trade_date - financial_date).days > FINANCIAL_EVIDENCE_MAX_AGE_DAYS)
        ):
            return None
        return (
            financial_date.year if (financial_date.month, financial_date.day) == (12, 31) else financial_date.year - 1
        )
    return trade_date.year - 1 if trade_date is not None else None


def _strict_annual_series(
    metric: Mapping[str, Any],
    values_key: str,
    years_key: str,
) -> dict[int, float] | None:
    """Require an exact, unique and finite year/value annual ledger."""

    values = metric.get(values_key)
    years = metric.get(years_key)
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not isinstance(years, Sequence)
        or isinstance(years, (str, bytes))
        or not values
        or len(values) != len(years)
    ):
        return None
    result: dict[int, float] = {}
    for raw_year, raw_value in zip(years, values):
        if isinstance(raw_year, bool):
            return None
        try:
            year = int(raw_year)
        except (TypeError, ValueError, OverflowError):
            return None
        value = _finite(raw_value)
        if not 1900 <= year <= 9999 or year in result or value is None:
            return None
        result[year] = value
    return result


def _recent_consecutive_years(years: set[int], expected_latest_year: int | None) -> list[int]:
    if not years or expected_latest_year is None or expected_latest_year not in years:
        return []
    result = [expected_latest_year]
    while result[0] - 1 in years:
        result.insert(0, result[0] - 1)
    return result


def _growth_rate(
    metric: Mapping[str, Any],
    values_key: str,
    years_key: str,
    *,
    minimum: int = 3,
) -> float | None:
    history = _strict_annual_series(metric, values_key, years_key)
    expected_latest_year = _latest_complete_financial_year(metric)
    if history is None:
        return None
    years = _recent_consecutive_years(set(history), expected_latest_year)
    if len(years) < minimum:
        return None
    # Type 7 is explicitly a long-horizon quality framework.  Use every
    # consecutive annual observation available up to the configured ten-year
    # financial window instead of silently truncating mature companies to five
    # observations.
    years = years[-max(minimum, min(10, len(years))) :]
    if history[years[0]] <= 0 or history[years[-1]] <= 0:
        return None
    return (history[years[-1]] / history[years[0]]) ** (1.0 / (years[-1] - years[0])) - 1.0


def _recent_history_ready(
    metric: Mapping[str, Any],
    values_key: str,
    years_key: str,
    *,
    minimum: int,
) -> bool:
    history = _strict_annual_series(metric, values_key, years_key)
    if history is None:
        return False
    return (
        len(
            _recent_consecutive_years(
                set(history),
                _latest_complete_financial_year(metric),
            )
        )
        >= minimum
    )


def _consecutive_year_count(metric: Mapping[str, Any]) -> int:
    revenue = _strict_annual_series(metric, "revenue_values", "revenue_years")
    profit = _strict_annual_series(metric, "net_profit_history", "net_profit_years")
    if revenue is None or profit is None:
        return 0
    common = set(revenue) & set(profit)
    return len(_recent_consecutive_years(common, _latest_complete_financial_year(metric)))


def _growth_score(rate: float | None) -> float:
    return _linear(rate, [(-0.15, 0), (0.0, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.35, 10)])


def _return_score(rate: float | None) -> float:
    return _linear(rate, [(-0.05, 0), (0.0, 1), (0.05, 4), (0.08, 6), (0.12, 8), (0.15, 9), (0.20, 10)])


def _forecast_growth_rate(*rates: float | None) -> float | None:
    """Return a conservative, bounded growth input shared by two source items."""

    clean = [float(value) for value in rates if value is not None and math.isfinite(float(value))]
    if not clean:
        return None
    return min(FORECAST_GROWTH_CAP, max(FORECAST_GROWTH_FLOOR, float(median(clean))))


def _terminal_profit_projection(
    latest_profit: float | None,
    market_cap: float | None,
    starting_growth: float | None,
) -> tuple[float, bool, dict[str, Any]]:
    """Quantify Template 1 item 19 with a ten-year fading-growth profit multiple."""

    if latest_profit is None or latest_profit <= 0 or market_cap is None or market_cap <= 0 or starting_growth is None:
        return (
            2.0,
            False,
            {
                "current_market_cap": market_cap,
                "latest_net_profit": latest_profit,
                "starting_growth": starting_growth,
            },
        )
    path = [
        starting_growth + (TERMINAL_GROWTH_RATE - starting_growth) * year / TERMINAL_PROFIT_HORIZON_YEARS
        for year in range(1, TERMINAL_PROFIT_HORIZON_YEARS + 1)
    ]
    projected_profit = latest_profit
    for growth in path:
        projected_profit *= 1.0 + growth
    if not math.isfinite(projected_profit) or projected_profit <= 0:
        return (
            2.0,
            False,
            {
                "current_market_cap": market_cap,
                "latest_net_profit": latest_profit,
                "starting_growth": starting_growth,
            },
        )
    terminal_multiple = market_cap / projected_profit
    score = _linear(
        terminal_multiple,
        [(5.0, 10.0), (10.0, 9.0), (15.0, 8.0), (20.0, 7.0), (30.0, 5.0), (50.0, 2.0), (80.0, 0.0)],
    )
    return (
        score,
        True,
        {
            "current_market_cap": market_cap,
            "latest_net_profit": latest_profit,
            "starting_growth": starting_growth,
            "terminal_growth": TERMINAL_GROWTH_RATE,
            "growth_path": [round(value, 8) for value in path],
            "projected_net_profit_year_10": projected_profit,
            "market_cap_to_year_10_profit": terminal_multiple,
        },
    )


def _valuation_reversion_return(
    earnings_growth_rate: float | None,
    book_value_growth_rate: float | None,
    valuation_history: Mapping[str, Any] | None,
) -> tuple[float, bool, dict[str, Any]]:
    """Estimate annual return from basis-matched growth and five-year multiple reversion."""

    if not isinstance(valuation_history, Mapping):
        return (
            2.0,
            False,
            {
                "earnings_growth_rate": earnings_growth_rate,
                "book_value_growth_rate": book_value_growth_rate,
            },
        )
    candidates: list[tuple[str, float, float, float, float]] = []
    for prefix, basis, current_key, median_key, growth_rate in (
        ("pe", "PE_TTM", "current_pe_ttm", "median_pe_ttm", earnings_growth_rate),
        ("pb", "PB_MRQ", "current_pb_mrq", "median_pb_mrq", book_value_growth_rate),
    ):
        if not _valid_valuation_series(valuation_history, prefix):
            continue
        current = _finite(valuation_history.get(current_key))
        target = _finite(valuation_history.get(median_key))
        if growth_rate is not None and current is not None and current > 0 and target is not None and target > 0:
            annual_return = (1.0 + growth_rate) * (target / current) ** (1.0 / EXPECTED_RETURN_HORIZON_YEARS) - 1.0
            if math.isfinite(annual_return):
                candidates.append((basis, annual_return, current, target, growth_rate))
    if not candidates:
        return (
            2.0,
            False,
            {
                "earnings_growth_rate": earnings_growth_rate,
                "book_value_growth_rate": book_value_growth_rate,
            },
        )
    # Template 5 allows PE or PB.  When both are present, use their median so
    # one extreme multiple cannot dominate the expected-return gate.
    expected = float(median(item[1] for item in candidates))
    return (
        _return_score(expected),
        True,
        {
            "earnings_growth_rate": earnings_growth_rate,
            "book_value_growth_rate": book_value_growth_rate,
            "horizon_years": EXPECTED_RETURN_HORIZON_YEARS,
            "annual_return": expected,
            "valuation_inputs": [
                {
                    "basis": basis,
                    "annual_return": value,
                    "current": current,
                    "target_median": target,
                    "growth_rate": growth_rate,
                }
                for basis, value, current, target, growth_rate in candidates
            ],
            "formula": "(1+basis_matched_growth)*(historical_median/current)^(1/5)-1",
        },
    )


def _balance_score(metric: Mapping[str, Any]) -> tuple[float, bool]:
    debt = _finite(metric.get("interest_bearing_debt_ratio"))
    if debt is None:
        debt = _finite(metric.get("debt_ratio"))
    score = _linear(debt, [(0.0, 10), (0.20, 9), (0.40, 7), (0.60, 4), (0.80, 1), (1.0, 0)])
    return score, debt is not None


def _roic_score(metric: Mapping[str, Any]) -> tuple[float, bool]:
    roic = _finite(metric.get("roic"))
    wacc = _finite(metric.get("wacc"))
    spread = roic - wacc if roic is not None and wacc is not None else None
    return _linear(spread, [(-0.05, 0), (0.0, 3), (0.03, 6), (0.08, 8), (0.15, 10)]), spread is not None


def _margin_score(metric: Mapping[str, Any]) -> tuple[float, bool]:
    gross = _finite(metric.get("gross_margin"))
    net = _finite(metric.get("net_margin"))
    cv = _finite(metric.get("gross_margin_cv"))
    absolute = _avg(
        _linear(gross, [(0.0, 0), (0.15, 3), (0.30, 6), (0.50, 8), (0.70, 10)]),
        _linear(net, [(-0.05, 0), (0.0, 2), (0.08, 5), (0.15, 7), (0.25, 9), (0.40, 10)]),
    )
    stability = _linear(cv, [(0.0, 10), (0.05, 9), (0.10, 7), (0.20, 4), (0.35, 1), (0.50, 0)])
    return _avg(absolute, stability), gross is not None and net is not None and cv is not None


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _limited_history_minimum_span(window_years: float | None) -> int:
    """Lower consistency bound for a recently-listed limited-history company.

    ``window_years`` is derived as ``round(span_days / 365.2425, 2)`` upstream,
    so a genuine record's span matches its declared window within rounding
    error; require 90% to reject forged window/span mismatches.
    """
    if window_years is None or not 1.0 <= window_years <= 5.0:
        return 0
    return max(1, int(round(window_years * 365.2425 * 0.90)))


def _history_integer(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _history_date(value: Any) -> date | None:
    parsed = _parse_evidence_date(value)
    return parsed if parsed is not None and parsed.isoformat() == value else None


def _valid_shareholder_return(value: Any, as_of: date) -> bool:
    """Replay the complete ten-year shareholder-return evidence contract."""

    if not isinstance(value, Mapping) or value.get("available") is not True:
        return False
    start = _history_date(value.get("start_date"))
    end = _history_date(value.get("end_date"))
    span_days = _history_integer(value.get("span_days"), minimum=1)
    observations = _history_integer(value.get("observations"), minimum=2)
    start_close = _finite(value.get("start_close_hfq"))
    end_close = _finite(value.get("end_close_hfq"))
    total_return = _finite(value.get("total_return"))
    cagr = _finite(value.get("cagr"))
    if (
        value.get("target_years") != 10
        or value.get("formula") != SHAREHOLDER_RETURN_FORMULA
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
    target = _years_before(as_of, 10)
    start_delay = (start - target).days
    latest_age = (as_of - end).days
    if (
        span_days != (end - start).days
        or span_days < TEN_YEAR_TARGET_DAYS - TEN_YEAR_START_TOLERANCE_DAYS - HISTORY_LATEST_MAX_AGE_DAYS
        or not 0 <= start_delay <= TEN_YEAR_START_TOLERANCE_DAYS
        or not 0 <= latest_age <= HISTORY_LATEST_MAX_AGE_DAYS
    ):
        return False
    ratio = end_close / start_close
    expected_total = ratio - 1.0
    expected_cagr = ratio ** (365.2425 / span_days) - 1.0
    return math.isclose(total_return, expected_total, rel_tol=1e-9, abs_tol=1e-9) and math.isclose(
        cagr,
        expected_cagr,
        rel_tol=1e-9,
        abs_tol=1e-9,
    )


def _valid_valuation_series(value: Any, prefix: str) -> bool:
    """Validate and replay one PE or PB side without trusting its sibling."""

    if not isinstance(value, Mapping) or prefix not in {"pe", "pb"}:
        return False
    row_count = _history_integer(value.get("row_count"), minimum=1)
    observations = _history_integer(value.get(f"{prefix}_observations"), minimum=0)
    current_key = "current_pe_ttm" if prefix == "pe" else "current_pb_mrq"
    median_key = "median_pe_ttm" if prefix == "pe" else "median_pb_mrq"
    current = _finite(value.get(current_key))
    historical_median = _finite(value.get(median_key))
    percentile = _finite(value.get(f"{prefix}_percentile"))
    replay = replay_valuation_distribution(value.get(f"{prefix}_distribution"), current)
    return bool(
        row_count is not None
        and observations is not None
        and observations >= VALUATION_MIN_OBSERVATIONS
        and row_count >= observations + 1
        and current is not None
        and current > 0
        and historical_median is not None
        and historical_median > 0
        and percentile is not None
        and 0 <= percentile <= 1
        and replay is not None
        and observations == replay["observations"]
        and math.isclose(historical_median, float(replay["median"]), rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(percentile, float(replay["percentile"]), rel_tol=0.0, abs_tol=1e-12)
    )


def _valid_valuation_history(value: Any, as_of: date) -> bool:
    """Validate the five-year window and at least one independently valid side."""

    if not isinstance(value, Mapping) or value.get("available") is not True:
        return False
    start = _history_date(value.get("start_date"))
    end = _history_date(value.get("end_date"))
    target_start = _history_date(value.get("target_start_date"))
    span_days = _history_integer(value.get("span_days"), minimum=1)
    start_delay = _history_integer(value.get("start_delay_days"), minimum=0)
    row_count = _history_integer(value.get("row_count"), minimum=1)
    window_years = _finite(value.get("window_years"))
    limited = bool(value.get("limited_history"))
    expected_target = _years_before(as_of, 5)
    minimum_span = (
        _limited_history_minimum_span(window_years)
        if limited and window_years is not None
        else FIVE_YEAR_TARGET_DAYS - FIVE_YEAR_START_TOLERANCE_DAYS - HISTORY_LATEST_MAX_AGE_DAYS
    )
    if (
        window_years is None
        or not 1.0 <= window_years <= 5.0
        or value.get("formula") != VALUATION_PERCENTILE_FORMULA
        or start is None
        or end is None
        or (not limited and target_start != expected_target)
        or span_days is None
        or start_delay is None
        or row_count is None
        or span_days != (end - start).days
        or (not limited and start_delay != (start - expected_target).days)
        or (limited and start_delay != 0)
        or span_days < minimum_span
        or (not limited and not 0 <= start_delay <= FIVE_YEAR_START_TOLERANCE_DAYS)
        or not 0 <= (as_of - end).days <= HISTORY_LATEST_MAX_AGE_DAYS
    ):
        return False

    return any(_valid_valuation_series(value, prefix) for prefix in ("pe", "pb"))


def _history_record(value: Any, code: str, as_of: str | None) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("available") is not True:
        return None
    if value.get("model_id") != LONG_HORIZON_HISTORY_MODEL_ID or str(value.get("code") or "") != code:
        return None
    reference = _history_date(as_of)
    record_as_of = str(value.get("as_of") or "")
    if reference is None or reference > date.today() or record_as_of != as_of:
        return None
    if not _valid_shareholder_return(value.get("shareholder_return"), reference):
        return None
    if not _valid_valuation_history(value.get("valuation_history"), reference):
        return None
    return value


def _item(
    key: str,
    label: str,
    weight: float,
    score: float,
    *,
    complete: bool,
    formula: str,
    inputs: Mapping[str, Any],
    evidence_level: str = "derived_proxy",
) -> dict[str, Any]:
    clean = _round_score(score)
    return {
        "key": key,
        "label": label,
        "weight": float(weight),
        "score": clean,
        "points": round(clean * float(weight) / 10.0, 4),
        "complete": bool(complete),
        "evidence_level": evidence_level if complete else "partial",
        "formula": formula,
        "inputs": dict(inputs),
    }


def _section(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total_weight = math.fsum(float(item["weight"]) for item in items)
    if not math.isclose(total_weight, 100.0, abs_tol=1e-9):
        raise QualityEquityError(f"section weights total {total_weight}, expected 100")
    score = round(math.fsum(float(item["points"]) for item in items), 2)
    coverage = math.fsum(float(item["weight"]) for item in items if item.get("complete")) / 100.0
    return {"score": score, "coverage": round(coverage, 4), "items": list(items)}


def _direct_or_proxy(
    metric: Mapping[str, Any],
    direct_key: str,
    proxy: Sequence[Any],
    *,
    proxy_cap: float | None = None,
) -> tuple[float, bool, str]:
    direct, direct_complete, direct_level = _verified_score(metric, direct_key)
    if direct is not None and direct_complete:
        return direct, True, direct_level
    if len(proxy) < 3:
        raise QualityEquityError("Type 7 proxy tuple is incomplete")
    value, complete, level = proxy[:3]
    value = 2.0 if value is None else value
    if proxy_cap is not None:
        value = min(value, proxy_cap)
    return _round_score(value), complete, level


def _template_inputs(
    metric: Mapping[str, Any],
    type1: tuple[bool, float, Mapping[str, Any], Mapping[str, Any]],
    history: Mapping[str, Any] | None,
    *,
    valuation_evidence_complete: bool,
) -> dict[str, tuple[float, bool, str, Mapping[str, Any]]]:
    if not isinstance(valuation_evidence_complete, bool):
        raise QualityEquityError("Type 7 valuation evidence status must be boolean")

    def verified(key: str) -> tuple[float, bool, str]:
        score, complete, level = _verified_score(metric, key)
        return (2.0 if score is None else score), complete, level

    business = verified("business_model_score")
    moat = verified("moat_score")
    moat_durability = verified("moat_durability_score")
    runway = verified("runway_score")
    industry = verified("industry_durability_score")
    accounting = verified("accounting_integrity_score")
    management = verified("management_alignment_score")
    technology = verified("technology_score")
    revenue_rate = _growth_rate(metric, "revenue_values", "revenue_years", minimum=5)
    profit_rate = _growth_rate(metric, "net_profit_history", "net_profit_years", minimum=5)
    fcf_rate = _growth_rate(metric, "fcf_history", "fcf_years", minimum=5)
    revenue_growth = _growth_score(revenue_rate)
    profit_fcf_growth = _avg(_growth_score(profit_rate), _growth_score(fcf_rate))
    balance, balance_complete = _balance_score(metric)
    roic, roic_complete = _roic_score(metric)
    margin, margin_complete = _margin_score(metric)

    dilution = _finite(metric.get("share_dilution_1yr"))
    dilution_score = _linear(dilution, [(-0.05, 10), (0.0, 10), (0.02, 8), (0.05, 5), (0.10, 2)])
    shareholder = _direct_or_proxy(
        metric,
        "shareholder_fairness_score",
        (_avg(management[0], dilution_score), management[1] and dilution is not None, "derived_proxy"),
    )
    culture = _direct_or_proxy(metric, "employee_culture_score", management, proxy_cap=6.0)

    volatility = _finite(metric.get("profit_volatility"))
    consistency = _finite(metric.get("growth_consistency"))
    growth_stability = _avg(
        _linear(volatility, [(0.0, 10), (0.20, 9), (0.50, 7), (1.0, 4), (2.0, 1)]),
        _linear(consistency, [(0.0, 10), (0.30, 8), (0.70, 6), (1.20, 3), (2.0, 1)]),
    )
    recent_trend_rate = (
        _finite(metric.get("trend_growth"))
        if _recent_history_ready(metric, "revenue_values", "revenue_years", minimum=3)
        else None
    )
    catalyst = _direct_or_proxy(
        metric,
        "catalyst_score",
        (
            _avg(
                _growth_score(recent_trend_rate),
                revenue_growth,
                profit_fcf_growth,
                growth_stability,
            ),
            recent_trend_rate is not None
            and revenue_rate is not None
            and profit_rate is not None
            and fcf_rate is not None
            and volatility is not None
            and consistency is not None,
            "derived_proxy",
        ),
        proxy_cap=7.0,
    )
    cyclicality = growth_stability
    if str(metric.get("industry") or "") in {
        "STEEL",
        "NONFERROUS",
        "CHEMICAL",
        "BUILDING_MATERIAL",
        "OIL_GAS",
        "COAL",
        "CONST_MACHINERY",
        "AGRICULTURE",
    }:
        cyclicality = min(cyclicality, 4.0)

    assets = _finite(metric.get("total_assets"))
    revenue = _finite(metric.get("revenue_latest"))
    capex = _finite(metric.get("capex"))
    asset_turnover = revenue / assets if revenue is not None and assets and assets > 0 else None
    capex_intensity = capex / revenue if capex is not None and revenue and revenue > 0 else None
    asset_light = _avg(
        _linear(asset_turnover, [(0.1, 1), (0.3, 3), (0.6, 6), (1.0, 8), (1.5, 10)]),
        _linear(capex_intensity, [(0.0, 10), (0.03, 9), (0.08, 7), (0.15, 4), (0.30, 1)]),
        roic,
    )

    gross = _finite(metric.get("gross_margin"))
    premium_proxy = _avg(
        _linear(gross, [(0.10, 0), (0.25, 2), (0.40, 4), (0.60, 6), (0.80, 8)]),
        moat[0],
    )
    luxury = _direct_or_proxy(
        metric,
        "luxury_attribute_score",
        (premium_proxy, gross is not None and moat[1], "derived_proxy_capped"),
        proxy_cap=6.0,
    )

    type1_scores = type1[2] if isinstance(type1[2], Mapping) else {}
    dcf_score = _finite(type1_scores.get("1a"))
    valuation_usable = valuation_evidence_complete and dcf_score is not None
    dcf_score = dcf_score if valuation_usable else 0.0

    source_trade_date = str(metric.get("source_trade_date") or "") or None
    history_record = _history_record(history, str(metric.get("code") or ""), source_trade_date)
    shareholder_history = history_record.get("shareholder_return") if history_record else None
    valuation_history = history_record.get("valuation_history") if history_record else None
    reference_date = _history_date(source_trade_date)
    shareholder_available = bool(
        reference_date is not None and _valid_shareholder_return(shareholder_history, reference_date)
    )
    valuation_available = bool(
        reference_date is not None and _valid_valuation_history(valuation_history, reference_date)
    )
    return_cagr = _finite(shareholder_history.get("cagr")) if shareholder_available else None
    return_10y = _return_score(return_cagr)
    percentiles = []
    valid_valuation_prefixes: set[str] = set()
    if valuation_available:
        for prefix, key in (("pe", "pe_percentile"), ("pb", "pb_percentile")):
            if not _valid_valuation_series(valuation_history, prefix):
                continue
            value = _finite(valuation_history.get(key))
            if value is not None and 0 <= value <= 1:
                percentiles.append(value)
                valid_valuation_prefixes.add(prefix)
    valuation_percentile = median(percentiles) if percentiles else None
    historical_valuation = _linear(
        valuation_percentile,
        [(0.0, 10), (0.10, 9), (0.30, 8), (0.50, 6.5), (0.70, 5), (0.90, 2), (1.0, 0)],
        missing=0,
    )
    forecast_growth = _forecast_growth_rate(revenue_rate, profit_rate, fcf_rate)
    total_equity_growth = _growth_rate(metric, "equity_history", "equity_years", minimum=5)
    share_dilution = _finite(metric.get("share_dilution_1yr"))
    per_share_book_growth = (
        (1.0 + total_equity_growth) / (1.0 + share_dilution) - 1.0
        if total_equity_growth is not None and share_dilution is not None and share_dilution > -1.0
        else None
    )
    book_value_growth = _forecast_growth_rate(per_share_book_growth)
    expected_return, expected_return_complete, expected_return_inputs = _valuation_reversion_return(
        forecast_growth,
        book_value_growth,
        valuation_history if isinstance(valuation_history, Mapping) else None,
    )
    latest_profit = _finite(metric.get("net_profit_latest"))
    if latest_profit is None:
        latest_profit = _finite(metric.get("net_profit"))
    terminal_profit_score, terminal_profit_complete, terminal_profit_inputs = _terminal_profit_projection(
        latest_profit,
        _finite(metric.get("market_cap")),
        forecast_growth,
    )
    return_and_terminal = _avg(return_10y, terminal_profit_score)
    shareholder_ledger_input = dict(shareholder_history) if isinstance(shareholder_history, Mapping) else {}
    shareholder_ledger_input["valuation_history_contract"] = (
        dict(valuation_history) if isinstance(valuation_history, Mapping) else {}
    )
    shareholder_ledger_input["annual_financial_history_contract"] = {
        "source_trade_date": metric.get("source_trade_date"),
        "financial_indicator_as_of": metric.get("financial_indicator_as_of"),
        "revenue_values": list(metric.get("revenue_values", []))
        if isinstance(metric.get("revenue_values"), Sequence)
        and not isinstance(metric.get("revenue_values"), (str, bytes))
        else [],
        "revenue_years": list(metric.get("revenue_years", []))
        if isinstance(metric.get("revenue_years"), Sequence)
        and not isinstance(metric.get("revenue_years"), (str, bytes))
        else [],
        "net_profit_history": list(metric.get("net_profit_history", []))
        if isinstance(metric.get("net_profit_history"), Sequence)
        and not isinstance(metric.get("net_profit_history"), (str, bytes))
        else [],
        "net_profit_years": list(metric.get("net_profit_years", []))
        if isinstance(metric.get("net_profit_years"), Sequence)
        and not isinstance(metric.get("net_profit_years"), (str, bytes))
        else [],
    }
    return_and_terminal_inputs = {
        "shareholder_return": shareholder_ledger_input,
        "terminal_profit_projection": terminal_profit_inputs,
    }

    return {
        "business": (*business, {"score": business[0]}),
        "moat": (*moat, {"score": moat[0]}),
        "moat_durability": (*moat_durability, {"score": moat_durability[0]}),
        "runway": (*runway, {"score": runway[0]}),
        "industry": (*industry, {"score": industry[0]}),
        "accounting": (*accounting, {"score": accounting[0]}),
        "management": (*management, {"score": management[0]}),
        "technology": (*technology, {"score": technology[0]}),
        "catalyst": (*catalyst, {"score": catalyst[0]}),
        "revenue_growth": (revenue_growth, revenue_rate is not None, "reported_formula", {"rate": revenue_rate}),
        "profit_fcf_growth": (
            profit_fcf_growth,
            profit_rate is not None and fcf_rate is not None,
            "reported_formula",
            {"profit_cagr": profit_rate, "fcf_cagr": fcf_rate},
        ),
        "balance": (balance, balance_complete, "reported_formula", {"score": balance}),
        "roic": (roic, roic_complete, "reported_formula", {"score": roic}),
        "margin": (margin, margin_complete, "reported_formula", {"score": margin}),
        "shareholder": (*shareholder, {"dilution": dilution}),
        "culture": (*culture, {"management_proxy": management[0]}),
        "cyclicality": (
            cyclicality,
            volatility is not None and consistency is not None,
            "reported_formula",
            {"profit_volatility": volatility, "growth_consistency": consistency},
        ),
        "growth_stability": (
            growth_stability,
            volatility is not None and consistency is not None,
            "reported_formula",
            {"profit_volatility": volatility, "growth_consistency": consistency},
        ),
        "asset_light": (
            asset_light,
            asset_turnover is not None and capex_intensity is not None and roic_complete,
            "reported_formula",
            {"asset_turnover": asset_turnover, "capex_intensity": capex_intensity},
        ),
        "luxury": (*luxury, {"gross_margin": gross, "proxy_cap": 6.0}),
        "dcf": (
            dcf_score,
            valuation_usable,
            "validated_nonfinancial_dcf",
            {
                "type1_1a": dcf_score,
                "validation_basis": "source_bound_nonfinancial_dcf",
            },
        ),
        "expected_return": (
            expected_return,
            expected_return_complete,
            "historical_valuation_reversion_formula",
            expected_return_inputs,
        ),
        "return_10y": (
            return_10y,
            shareholder_available and return_cagr is not None,
            "independent_market_history",
            dict(shareholder_history) if isinstance(shareholder_history, Mapping) else {},
        ),
        "return_and_terminal_profit": (
            return_and_terminal,
            shareholder_available and return_cagr is not None and terminal_profit_complete,
            "independent_market_history_plus_fading_growth_projection",
            return_and_terminal_inputs,
        ),
        "historical_valuation": (
            historical_valuation,
            valuation_available and bool(percentiles),
            "independent_market_history",
            {
                "combined_percentile": valuation_percentile,
                "pe_percentile": valuation_history.get("pe_percentile")
                if isinstance(valuation_history, Mapping) and "pe" in valid_valuation_prefixes
                else None,
                "pb_percentile": valuation_history.get("pb_percentile")
                if isinstance(valuation_history, Mapping) and "pb" in valid_valuation_prefixes
                else None,
            },
        ),
    }


def _make_template1(values: Mapping[str, tuple[float, bool, str, Mapping[str, Any]]]) -> dict[str, Any]:
    def record(key: str, label: str, source: str, formula: str | None = None) -> dict[str, Any]:
        score, complete, level, inputs = values[source]
        return _item(
            key,
            label,
            5.0,
            score,
            complete=complete,
            evidence_level=level,
            formula=formula or source,
            inputs=inputs,
        )

    lifecycle = _avg(values["runway"][0], values["industry"][0])
    growth_potential = _avg(
        values["runway"][0],
        values["revenue_growth"][0],
        values["profit_fcf_growth"][0],
        values["growth_stability"][0],
    )
    health = _avg(values["accounting"][0], values["balance"][0], values["roic"][0])
    advantage = _avg(values["moat"][0], values["moat_durability"][0])
    cost_control = _avg(values["margin"][0], values["accounting"][0])
    market_position = _avg(values["moat"][0], values["industry"][0])
    wealth = _avg(
        values["moat_durability"][0],
        values["profit_fcf_growth"][0],
        values["roic"][0],
        values["accounting"][0],
    )

    items = [
        _item(
            "t1_01",
            "未来生命周期",
            5,
            lifecycle,
            complete=values["runway"][1] and values["industry"][1],
            formula="mean(runway,industry_durability)",
            inputs={"runway": values["runway"][0], "industry": values["industry"][0]},
        ),
        _item(
            "t1_02",
            "成长潜力",
            5,
            growth_potential,
            complete=all(
                values[key][1] for key in ("runway", "revenue_growth", "profit_fcf_growth", "growth_stability")
            ),
            formula="mean(runway,revenue_CAGR_score,profit_FCF_CAGR_score,growth_stability)",
            inputs={
                key: values[key][0] for key in ("runway", "revenue_growth", "profit_fcf_growth", "growth_stability")
            },
        ),
        record("t1_03", "主营收入增长", "revenue_growth", "piecewise_linear(revenue_CAGR)"),
        record("t1_04", "扣非利润与FCF增长", "profit_fcf_growth", "mean(profit_CAGR_score,FCF_CAGR_score)"),
        record("t1_05", "商业模式", "business"),
        _item(
            "t1_06",
            "财务健康",
            5,
            health,
            complete=all(values[key][1] for key in ("accounting", "balance", "roic")),
            formula="mean(accounting,balance,ROIC_spread)",
            inputs={key: values[key][0] for key in ("accounting", "balance", "roic")},
        ),
        record("t1_07", "细分产业环境", "industry"),
        record("t1_08", "股东权益公平", "shareholder"),
        _item(
            "t1_09",
            "长期竞争优势",
            5,
            advantage,
            complete=values["moat"][1] and values["moat_durability"][1],
            formula="mean(moat,moat_durability)",
            inputs={"moat": values["moat"][0], "durability": values["moat_durability"][0]},
        ),
        record("t1_10", "文化与员工满意", "culture"),
        _item(
            "t1_11",
            "成本控制",
            5,
            cost_control,
            complete=values["margin"][1] and values["accounting"][1],
            formula="mean(margin_stability,accounting)",
            inputs={"margin": values["margin"][0], "accounting": values["accounting"][0]},
        ),
        record("t1_12", "资产劳动资金强度", "asset_light"),
        record("t1_13", "弱周期属性", "cyclicality"),
        _item(
            "t1_14",
            "垄断性与竞争地位",
            5,
            market_position,
            complete=values["moat"][1] and values["industry"][1],
            formula="mean(moat,industry_structure)",
            inputs={"moat": values["moat"][0], "industry": values["industry"][0]},
        ),
        _item(
            "t1_15",
            "长期财富积累",
            5,
            wealth,
            complete=all(values[key][1] for key in ("moat_durability", "profit_fcf_growth", "roic", "accounting")),
            formula="mean(moat_durability,profit_FCF_CAGR_score,ROIC_spread,accounting)",
            inputs={key: values[key][0] for key in ("moat_durability", "profit_fcf_growth", "roic", "accounting")},
        ),
        record("t1_16", "奢侈品属性", "luxury"),
        record("t1_17", "顶级科技与创新", "technology"),
        record("t1_18", "长期预期回报", "expected_return"),
        record(
            "t1_19",
            "十年回报与远期利润",
            "return_and_terminal_profit",
            "mean(hfq_10y_CAGR_score,market_cap/projected_year10_profit_score)",
        ),
        record("t1_20", "DCF价格位置", "dcf"),
    ]
    return _section(items)


def _make_template5(values: Mapping[str, tuple[float, bool, str, Mapping[str, Any]]]) -> dict[str, Any]:
    big_cycle = _avg(values["industry"][0], values["revenue_growth"][0])
    space = _avg(values["runway"][0], values["moat"][0])
    governance = _avg(values["shareholder"][0], values["management"][0])
    health = _avg(values["accounting"][0], values["balance"][0], values["roic"][0])
    specs = [
        ("t5_i1", "产业大周期", 12, big_cycle, values["industry"][1] and values["revenue_growth"][1]),
        ("t5_i2", "产业小周期", 9, values["catalyst"][0], values["catalyst"][1]),
        ("t5_i3", "产业空间与格局", 9, space, values["runway"][1] and values["moat"][1]),
        ("t5_q1", "商业模式", 14, values["business"][0], values["business"][1]),
        ("t5_q2", "长期护城河", 12, values["moat_durability"][0], values["moat_durability"][1]),
        ("t5_q3", "治理与股东文化", 8, governance, values["shareholder"][1] and values["management"][1]),
        (
            "t5_q4",
            "财务健康",
            6,
            health,
            all(values[key][1] for key in ("accounting", "balance", "roic")),
        ),
        (
            "t5_v1",
            "历史估值分位",
            9,
            values["historical_valuation"][0],
            values["historical_valuation"][1],
        ),
        ("t5_v2", "绝对DCF估值", 12, values["dcf"][0], values["dcf"][1]),
        ("t5_v3", "预期回报率", 9, values["expected_return"][0], values["expected_return"][1]),
    ]
    items = [
        _item(
            key,
            label,
            weight,
            score,
            complete=complete,
            formula="Template5_source_weight*observable_score",
            inputs={"normalized_score": score},
        )
        for key, label, weight, score, complete in specs
    ]
    return _section(items)


def _patch_component(
    key: str,
    label: str,
    maximum: float,
    score: float,
    complete: bool,
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _round_score(score)
    return {
        "key": key,
        "label": label,
        "max_points": float(maximum),
        "score": normalized,
        "points": round(normalized * float(maximum) / 10.0, 4),
        "complete": bool(complete),
        "formula": f"{maximum:g}*score/10",
        "inputs": dict(inputs),
    }


def _make_patch5(
    metric: Mapping[str, Any],
    values: Mapping[str, tuple[float, bool, str, Mapping[str, Any]]],
    *,
    technology_company: bool,
    patch4_ledger: Mapping[str, Any] | None,
) -> dict[str, Any]:
    clarity = _direct_or_proxy(metric, "business_clarity_score", values["business"], proxy_cap=7.0)
    scalability = (
        _avg(values["runway"][0], values["revenue_growth"][0], values["asset_light"][0]),
        values["runway"][1] and values["revenue_growth"][1] and values["asset_light"][1],
        "derived_proxy",
    )
    stickiness = _direct_or_proxy(
        metric,
        "customer_stickiness_score",
        (_avg(values["moat"][0], values["margin"][0]), values["moat"][1] and values["margin"][1], "derived_proxy"),
        proxy_cap=7.0,
    )
    pricing = _direct_or_proxy(
        metric,
        "pricing_power_score",
        (_avg(values["moat"][0], values["margin"][0]), values["moat"][1] and values["margin"][1], "derived_proxy"),
        proxy_cap=8.0,
    )
    barrier = _direct_or_proxy(metric, "entry_barrier_score", values["moat_durability"], proxy_cap=8.0)
    governance = _direct_or_proxy(metric, "governance_score", values["management"], proxy_cap=7.0)
    innovation = _direct_or_proxy(
        metric,
        "innovation_adaptability_score",
        (
            _avg(
                values["technology"][0],
                values["business"][0],
                values["catalyst"][0],
                values["runway"][0],
                values["revenue_growth"][0],
                values["profit_fcf_growth"][0],
                values["growth_stability"][0],
            ),
            all(
                values[key][1]
                for key in (
                    "technology",
                    "business",
                    "catalyst",
                    "runway",
                    "revenue_growth",
                    "profit_fcf_growth",
                    "growth_stability",
                )
            ),
            "derived_proxy",
        ),
        proxy_cap=8.0,
    )
    external = _direct_or_proxy(metric, "external_environment_score", values["industry"], proxy_cap=7.0)
    downside = _direct_or_proxy(
        metric,
        "downside_protection_score",
        (
            _avg(values["balance"][0], values["accounting"][0], values["dcf"][0]),
            values["balance"][1] and values["accounting"][1] and values["dcf"][1],
            "derived_proxy",
        ),
        proxy_cap=8.0,
    )
    incentive_alignment = (
        (
            _finite(patch4_ledger.get("score")) or 0.0,
            patch4_ledger.get("complete") is True,
        )
        if technology_company and isinstance(patch4_ledger, Mapping)
        else (2.0, False)
        if technology_company
        else (values["shareholder"][0], values["shareholder"][1])
    )

    sections = [
        {
            "key": "p5_business",
            "label": "商业模式",
            "max_points": 20.0,
            "components": [
                _patch_component("p5_b1", "清晰度", 5, clarity[0], clarity[1], {"source": clarity[2]}),
                _patch_component("p5_b2", "可扩展性", 5, scalability[0], scalability[1], {}),
                _patch_component("p5_b3", "黏性复购", 5, stickiness[0], stickiness[1], {"source": stickiness[2]}),
                _patch_component("p5_b4", "资本效率", 5, values["roic"][0], values["roic"][1], {}),
            ],
        },
        {
            "key": "p5_moat",
            "label": "护城河",
            "max_points": 20.0,
            "components": [
                _patch_component("p5_m1", "护城河强度", 8, values["moat"][0], values["moat"][1], {}),
                _patch_component("p5_m2", "定价权", 6, pricing[0], pricing[1], {"source": pricing[2]}),
                _patch_component("p5_m3", "进入壁垒", 6, barrier[0], barrier[1], {"source": barrier[2]}),
            ],
        },
        {
            "key": "p5_culture",
            "label": "公司文化",
            "max_points": 20.0,
            "components": [
                _patch_component("p5_c1", "管理诚信", 6, values["management"][0], values["management"][1], {}),
                _patch_component(
                    "p5_c2",
                    "激励一致",
                    5,
                    incentive_alignment[0],
                    incentive_alignment[1],
                    {},
                ),
                _patch_component("p5_c3", "创新适应", 5, innovation[0], innovation[1], {"source": innovation[2]}),
                _patch_component("p5_c4", "治理透明", 4, governance[0], governance[1], {"source": governance[2]}),
            ],
        },
        {
            "key": "p5_industry",
            "label": "产业兴衰",
            "max_points": 20.0,
            "components": [
                _patch_component("p5_i1", "生命周期", 8, values["runway"][0], values["runway"][1], {}),
                _patch_component("p5_i2", "竞争格局", 6, values["moat"][0], values["moat"][1], {}),
                _patch_component("p5_i3", "外部环境", 6, external[0], external[1], {"source": external[2]}),
            ],
        },
        {
            "key": "p5_safety",
            "label": "安全边际",
            "max_points": 20.0,
            "components": [
                _patch_component(
                    "p5_s1",
                    "估值水平",
                    8,
                    _avg(values["dcf"][0], values["historical_valuation"][0]),
                    values["dcf"][1] and values["historical_valuation"][1],
                    {},
                ),
                _patch_component(
                    "p5_s2",
                    "财务稳健",
                    6,
                    _avg(values["balance"][0], values["accounting"][0], values["roic"][0]),
                    values["balance"][1] and values["accounting"][1] and values["roic"][1],
                    {},
                ),
                _patch_component("p5_s3", "下行保护", 6, downside[0], downside[1], {"source": downside[2]}),
            ],
        },
    ]
    for section in sections:
        components = section["components"]
        section["points"] = round(math.fsum(float(component["points"]) for component in components), 4)
        section["complete"] = all(bool(component["complete"]) for component in components)
        if not math.isclose(
            math.fsum(float(component["max_points"]) for component in components),
            float(section["max_points"]),
            abs_tol=1e-9,
        ):
            raise QualityEquityError(f"Patch 5 section weight mismatch: {section['key']}")
    total = round(math.fsum(float(section["points"]) for section in sections), 2)
    coverage = math.fsum(float(section["max_points"]) for section in sections if section["complete"]) / 100.0
    safety = next(section for section in sections if section["key"] == "p5_safety")
    return {
        "score": total,
        "coverage": round(coverage, 4),
        "safety_margin_score": round(float(safety["points"]), 2),
        "safety_margin_complete": bool(safety["complete"]),
        "dimensions": sections,
    }


def decisive_score_upper_bounds(
    template1: Mapping[str, Any],
    template5: Mapping[str, Any],
    patch5: Mapping[str, Any],
) -> dict[str, float]:
    """Return score ceilings after replacing every incomplete item with its maximum.

    Unlike ``upper_bounds_without_history``, these ceilings are evidence-agnostic:
    every incomplete Template 1/5 item and every incomplete Patch 5 component is
    restored to its own declared maximum.  A ceiling at or below the strict
    threshold is therefore a conclusive failure, not an absence-of-data result.
    """

    def template_upper(section: Mapping[str, Any], label: str) -> float:
        score = _finite(section.get("score"))
        items = section.get("items")
        if score is None or not isinstance(items, list):
            raise QualityEquityError(f"{label} cannot be upper-bounded")
        terms: list[float] = []
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("complete"), bool):
                raise QualityEquityError(f"{label} item cannot be upper-bounded")
            weight = _finite(item.get("weight"))
            points = _finite(item.get("points"))
            if weight is None or points is None or weight < 0 or not 0 <= points <= weight:
                raise QualityEquityError(f"{label} item points are invalid")
            terms.append(points if item["complete"] else weight)
        return round(min(100.0, math.fsum(terms)), 2)

    patch_score = _finite(patch5.get("score"))
    dimensions = patch5.get("dimensions")
    if patch_score is None or not isinstance(dimensions, list):
        raise QualityEquityError("Patch 5 cannot be upper-bounded")
    patch_terms: list[float] = []
    for dimension in dimensions:
        components = dimension.get("components") if isinstance(dimension, Mapping) else None
        if not isinstance(components, list):
            raise QualityEquityError("Patch 5 component cannot be upper-bounded")
        for component in components:
            if not isinstance(component, Mapping) or not isinstance(component.get("complete"), bool):
                raise QualityEquityError("Patch 5 component cannot be upper-bounded")
            maximum = _finite(component.get("max_points"))
            points = _finite(component.get("points"))
            if maximum is None or points is None or maximum < 0 or not 0 <= points <= maximum:
                raise QualityEquityError("Patch 5 component points are invalid")
            patch_terms.append(points if component["complete"] else maximum)
    return {
        "template1": template_upper(template1, "Template 1"),
        "template5": template_upper(template5, "Template 5"),
        "patch5": round(min(100.0, math.fsum(patch_terms)), 2),
    }


def _incomplete_required_item_ids(
    template1: Mapping[str, Any],
    template5: Mapping[str, Any],
    patch5: Mapping[str, Any],
) -> list[str]:
    """List every required Type 7 source item that lacks complete evidence."""

    incomplete: list[str] = []
    template1_items = {
        item.get("key"): item
        for item in template1.get("items", [])
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    template5_items = {
        item.get("key"): item
        for item in template5.get("items", [])
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    for key in _TEMPLATE1_ITEM_WEIGHTS:
        if template1_items.get(key, {}).get("complete") is not True:
            incomplete.append(f"template1.{key}")
    for key in _TEMPLATE5_ITEM_WEIGHTS:
        if template5_items.get(key, {}).get("complete") is not True:
            incomplete.append(f"template5.{key}")

    patch_sections = {
        section.get("key"): section
        for section in patch5.get("dimensions", [])
        if isinstance(section, Mapping) and isinstance(section.get("key"), str)
    }
    for section_key, component_weights in _PATCH5_COMPONENT_WEIGHTS.items():
        section = patch_sections.get(section_key, {})
        components = {
            component.get("key"): component
            for component in section.get("components", [])
            if isinstance(component, Mapping) and isinstance(component.get("key"), str)
        }
        for component_key in component_weights:
            if components.get(component_key, {}).get("complete") is not True:
                incomplete.append(f"patch5.{section_key}.{component_key}")
    return incomplete


def assess_quality_equity(
    metric: Mapping[str, Any],
    type1_outcome: tuple[bool, float, Mapping[str, Any], Mapping[str, Any]],
    history_evidence: Mapping[str, Any] | None = None,
    *,
    valuation_evidence_complete: bool,
) -> dict[str, Any]:
    """Build a replayable Type 7 assessment from validated upstream evidence."""

    code = str(metric.get("code") or "")
    if not re.fullmatch(r"[036][0-9]{5}", code):
        raise QualityEquityError("Type 7 metric code is invalid")
    values = _template_inputs(
        metric,
        type1_outcome,
        history_evidence,
        valuation_evidence_complete=valuation_evidence_complete,
    )
    metric_as_of = _parse_evidence_date(metric.get("source_trade_date"))
    quote_date_complete = metric_as_of is not None and metric_as_of <= date.today()
    technology_score = values["technology"][0]
    technology_score_complete = values["technology"][1]
    rd_intensity = _finite(metric.get("rd_intensity"))
    if rd_intensity is not None and not 0 <= rd_intensity <= 1:
        rd_intensity = None
    # Patch 4 is waived only when both independent applicability inputs
    # affirmatively place the company below the technology thresholds.
    # Missing R&D intensity or an incomplete technology score cannot prove
    # that a company is non-technology.
    proven_non_technology = bool(
        rd_intensity is not None and rd_intensity < 0.05 and technology_score_complete and technology_score < 7.0
    )
    technology_company = not proven_non_technology
    patch4_ledger = (
        _build_patch4_ledger(
            metric,
            values,
            code=code,
            as_of=metric_as_of.isoformat() if metric_as_of is not None else "0001-01-01",
        )
        if technology_company
        else None
    )
    template1 = _make_template1(values)
    template5 = _make_template5(values)
    patch5 = _make_patch5(
        metric,
        values,
        technology_company=technology_company,
        patch4_ledger=patch4_ledger,
    )

    research = normalise_research_sources(
        metric.get("type7_research_sources"),
        today=metric_as_of if quote_date_complete else date.today(),
        security_code=code,
    )
    validated_history = _history_record(
        history_evidence,
        code,
        str(metric.get("source_trade_date") or "") or None,
    )
    metadata_precheck = research_metadata_precheck(
        research,
        reference=metric_as_of if metric_as_of is not None else date.today(),
    )
    content_as_of = metric_as_of.isoformat() if metric_as_of is not None else "0001-01-01"
    raw_content_verification = metric.get("type7_research_content_verification")
    if raw_content_verification is None:
        raw_content_verification = {
            "model_id": RESEARCH_CONTENT_MODEL_ID,
            "code": code,
            "as_of": content_as_of,
            "passed": False,
            "required_bodies": MIN_RESEARCH_BODY_SOURCES,
            "attempted_bodies": 0,
            "verified_bodies": 0,
            "distinct_publishers": 0,
            "bodies": [],
            "cross_check": {
                "passed": False,
                "minimum_reports": MIN_CROSSCHECK_REPORTS,
                "fact_key": None,
                "fact_unit": None,
                "consensus_value": None,
                "supporting_evidence_ids": [],
                "max_relative_spread": None,
            },
            "reason": "report_body_verification_not_provided",
        }
    content_verification = normalise_research_content_verification(
        raw_content_verification,
        sources=research,
        security_code=code,
        as_of=content_as_of,
    )
    financial_years = _consecutive_year_count(metric)
    patch4_complete = bool(isinstance(patch4_ledger, Mapping) and patch4_ledger.get("complete") is True)
    patch4_score = _finite(patch4_ledger.get("score")) if patch4_complete and patch4_ledger is not None else None
    patch4_status = (
        "not_applicable"
        if not technology_company
        else "validated_replayable_assessment"
        if patch4_complete
        else "incomplete_replayable_assessment"
        if patch4_ledger is not None
        else "missing_validated_patch4_assessment"
    )
    history_complete = values["return_10y"][1] and values["historical_valuation"][1]
    valuation_complete = values["dcf"][1]
    core_coverage = template1["coverage"]
    incomplete_required_items = _incomplete_required_item_ids(template1, template5, patch5)
    required_items_complete = not incomplete_required_items
    prerequisites = {
        "core_modules_80pct": {
            # Patch 5 requires at least 80% of the core analysis, not 100% of
            # every Template 1, Template 5 and Patch 5 item.  Missing items
            # remain visible and receive conservative scores as required by
            # the source template.
            "passed": core_coverage >= MIN_CORE_COVERAGE,
            "actual": core_coverage,
            "required": MIN_CORE_COVERAGE,
            "required_items_complete": required_items_complete,
            "incomplete_required_items": incomplete_required_items,
        },
        "technology_patch4": {
            "passed": not technology_company or patch4_complete,
            "applicable": technology_company,
            "score": patch4_score,
            "validation_status": patch4_status,
            "applicability": {
                "technology_score": technology_score,
                "technology_score_complete": technology_score_complete,
                "rd_intensity": rd_intensity,
                "rule": ("Patch4 waived only if reported_rd_intensity<0.05 AND validated_technology_score<7"),
            },
            "assessment": patch4_ledger,
        },
        "three_year_financials": {"passed": financial_years >= 3, "consecutive_years": financial_years},
        "latest_quote_and_valuation": {
            "passed": valuation_complete and quote_date_complete,
            "as_of": metric_as_of.isoformat() if metric_as_of is not None else None,
            "valuation_complete": valuation_complete,
            "validation_basis": "source_bound_nonfinancial_dcf",
        },
        "three_external_reports": {
            "passed": metadata_precheck["passed"],
            "check_type": "metadata_availability_precheck",
            "source_count": metadata_precheck["source_count"],
            "distinct_publishers": metadata_precheck["distinct_publishers"],
            "recent_source_count": metadata_precheck["recent_source_count"],
            "max_age_days": RESEARCH_MAX_AGE_DAYS,
            "recent_age_days": RESEARCH_RECENT_AGE_DAYS,
            "sources": research,
        },
        "external_report_content_verification": content_verification,
        "ten_year_return_and_five_year_valuation": {
            "passed": history_complete,
            "as_of": str(validated_history.get("as_of")) if validated_history is not None else None,
        },
    }
    scores = {
        "template1": float(template1["score"]),
        "template5": float(template5["score"]),
        "patch5": float(patch5["score"]),
    }
    # The source rule is expressed on the three 0-100 ledgers.  Compare those
    # exact published ledger scores before converting them to the 0-10 screen
    # diagnostics; otherwise 70.01..70.04 would be incorrectly rounded away.
    strict_checks = {key: value > STRICT_THRESHOLD for key, value in scores.items()}
    prerequisites_complete = all(bool(record["passed"]) for record in prerequisites.values())
    safety_veto = bool(patch5["safety_margin_complete"] and float(patch5["safety_margin_score"]) < PATCH5_SAFETY_VETO)
    decisive_upper_bounds = decisive_score_upper_bounds(template1, template5, patch5)
    decisively_not_triggered = any(value <= STRICT_THRESHOLD for value in decisive_upper_bounds.values())

    # Four template items and one Patch 5 component depend on long market
    # history.  Replace every one with its mathematical maximum; omitting even
    # one would create a false-negative preflight that never fetches evidence
    # for an otherwise viable Type 7 candidate.
    t1_expected_return_item = next(item for item in template1["items"] if item["key"] == "t1_18")
    t1_history_item = next(item for item in template1["items"] if item["key"] == "t1_19")
    t5_history_item = next(item for item in template5["items"] if item["key"] == "t5_v1")
    t5_expected_return_item = next(item for item in template5["items"] if item["key"] == "t5_v3")
    p5_safety = next(section for section in patch5["dimensions"] if section["key"] == "p5_safety")
    p5_valuation = next(item for item in p5_safety["components"] if item["key"] == "p5_s1")
    template1_upper = min(
        100.0,
        scores["template1"] - t1_expected_return_item["points"] - t1_history_item["points"] + 10.0,
    )
    template5_upper = min(
        100.0,
        scores["template5"] - t5_history_item["points"] - t5_expected_return_item["points"] + 18.0,
    )
    p5_valuation_upper = 8.0 * _avg(values["dcf"][0], 10.0) / 10.0
    patch5_upper = min(100.0, scores["patch5"] - p5_valuation["points"] + p5_valuation_upper)
    upper_bounds = {
        "template1": round(template1_upper, 2),
        "template5": round(template5_upper, 2),
        "patch5": round(patch5_upper, 2),
    }
    pre_history_prerequisites_complete = all(
        (core_coverage >= MIN_CORE_COVERAGE if key == "core_modules_80pct" else bool(record["passed"]))
        for key, record in prerequisites.items()
        if key
        not in {
            "three_external_reports",
            "external_report_content_verification",
            "ten_year_return_and_five_year_valuation",
        }
    )
    pre_research_prerequisites_complete = all(
        bool(record["passed"])
        for key, record in prerequisites.items()
        if key not in {"three_external_reports", "external_report_content_verification"}
    )
    research_request_needed = bool(
        (
            not prerequisites["three_external_reports"]["passed"]
            or not prerequisites["external_report_content_verification"]["passed"]
        )
        and pre_research_prerequisites_complete
        and not safety_veto
        and (not decisively_not_triggered or (len(scores) == 3 and min(scores.values()) >= 60.0))
    )
    history_request_needed = bool(
        not history_complete
        and pre_history_prerequisites_complete
        and not safety_veto
        and all(value > STRICT_THRESHOLD for value in upper_bounds.values())
        and not decisively_not_triggered
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "code": code,
        "source_rule": "Template1>70 AND Template5>70 AND Patch5>70",
        "strict_threshold": STRICT_THRESHOLD,
        "scores": scores,
        "strict_checks": strict_checks,
        "all_scores_strictly_above_70": all(strict_checks.values()),
        "prerequisites": prerequisites,
        "prerequisites_complete": prerequisites_complete,
        "safety_veto": safety_veto,
        "triggered": bool(all(strict_checks.values()) and prerequisites_complete and not safety_veto),
        "decisive_score_upper_bounds": decisive_upper_bounds,
        "decisively_not_triggered": decisively_not_triggered,
        "research_request_needed": research_request_needed,
        "history_request_needed": history_request_needed,
        "upper_bounds_without_history": upper_bounds,
        "template1": template1,
        "template5": template5,
        "patch5": patch5,
    }


def _template_item_contract(
    section_key: str,
    key: str,
    inputs: Mapping[str, Any],
) -> tuple[bool, float | None]:
    """Validate fixed metadata and replay only formulas supported by published inputs."""

    if section_key == "template5":
        if key not in _TEMPLATE5_ITEM_LABELS or set(inputs) != {"normalized_score"}:
            return False, None
        return True, _finite(inputs.get("normalized_score"))

    contract = _TEMPLATE1_ITEM_CONTRACTS.get(key)
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
        return (False, None) if any(value is None for value in values) else (True, _avg(*values))
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
        return True, _growth_score(_finite(raw))
    if key == "t1_04":
        raw_values = [inputs.get("profit_cagr"), inputs.get("fcf_cagr")]
        if any(value is not None and _finite(value) is None for value in raw_values):
            return False, None
        return True, _avg(*(_growth_score(_finite(value)) for value in raw_values))
    if key == "t1_18" and "annual_return" in inputs:
        return True, _return_score(_finite(inputs.get("annual_return")))
    return True, None


def _validate_patch4_ledger(
    assessment: Any,
    *,
    code: str,
    as_of: str,
    fairness_item: Mapping[str, Any],
    governance_component: Mapping[str, Any],
) -> list[str]:
    """Replay a published Patch 4 ledger from its atomic public facts."""

    errors: list[str] = []
    expected_top = {
        "schema_version",
        "model_id",
        "code",
        "as_of",
        "formula_version",
        "score",
        "complete",
        "components",
    }
    if not isinstance(assessment, Mapping) or set(assessment) != expected_top:
        return ["Patch 4 assessment structure invalid"]
    if (
        assessment.get("schema_version") != PATCH4_SCHEMA_VERSION
        or assessment.get("model_id") != PATCH4_MODEL_ID
        or assessment.get("formula_version") != PATCH4_FORMULA_VERSION
        or assessment.get("code") != code
        or assessment.get("as_of") != as_of
    ):
        errors.append("Patch 4 assessment identity invalid")
    components = assessment.get("components")
    if not isinstance(components, list) or len(components) != len(_PATCH4_COMPONENT_WEIGHTS):
        return errors + ["Patch 4 component structure invalid"]
    indexed: dict[str, Mapping[str, Any]] = {}
    for component in components:
        expected_fields = {
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
        if (
            not isinstance(component, Mapping)
            or set(component) != expected_fields
            or key not in _PATCH4_COMPONENT_WEIGHTS
            or key in indexed
        ):
            errors.append("Patch 4 component identity invalid")
            continue
        score = _finite(component.get("score"))
        points = _finite(component.get("points"))
        weight = _PATCH4_COMPONENT_WEIGHTS[key]
        if (
            score is None
            or not 0 <= score <= 10
            or points is None
            or not isinstance(component.get("complete"), bool)
            or component.get("label") != _PATCH4_COMPONENT_LABELS[key]
            or not math.isclose(float(component.get("weight", -1)), weight, abs_tol=1e-9)
            or not math.isclose(points, round(score * weight / 10.0, 4), abs_tol=0.0001)
            or not isinstance(component.get("formula"), str)
            or not isinstance(component.get("inputs"), Mapping)
        ):
            errors.append(f"Patch 4 component arithmetic invalid: {key}")
        indexed[key] = component
    if set(indexed) != set(_PATCH4_COMPONENT_WEIGHTS):
        return errors + ["Patch 4 component set invalid"]

    fairness = indexed["p4_defensive_fairness"]
    governance = indexed["p4_defensive_governance"]
    expected_fairness_inputs = {
        "source_item": "template1.t1_08",
        "evidence_level": fairness_item.get("evidence_level"),
    }
    # Patch 5 stores the direct/proxy source level but not the full underlying
    # evidence record.  The score and completeness bindings are independently
    # sufficient here; the source level remains constrained to the same small
    # vocabulary used by the published ledgers.
    if (
        fairness.get("formula") != "source_score(template1.t1_08)"
        or fairness.get("inputs") != expected_fairness_inputs
        or fairness.get("evidence") is not None
        or not math.isclose(
            float(fairness.get("score", -1)),
            float(fairness_item.get("score", -2)),
            abs_tol=0.0001,
        )
        or fairness.get("complete") is not fairness_item.get("complete")
    ):
        errors.append("Patch 4 defensive fairness binding invalid")
    governance_inputs = governance.get("inputs")
    if (
        governance.get("formula") != "verified_governance_or_capped_management_proxy"
        or not isinstance(governance_inputs, Mapping)
        or set(governance_inputs) != {"source_item", "evidence_level"}
        or governance_inputs.get("source_item") != "patch5.p5_c4"
        or governance_inputs.get("evidence_level") not in {"primary", "derived_proxy", "missing"}
        or governance.get("evidence") is not None
        or not math.isclose(
            float(governance.get("score", -1)),
            float(governance_component.get("score", -2)),
            abs_tol=0.0001,
        )
        or governance.get("complete") is not governance_component.get("complete")
    ):
        errors.append("Patch 4 defensive governance binding invalid")

    raw_keys = {
        "p4_core_rd_ownership": "core_rd_ownership_pct",
        "p4_esop_coverage": "esop_core_talent_coverage_pct",
        "p4_long_term_rd_link": "long_term_rd_metrics",
        "p4_frontline_rd_equity": "frontline_rd_equity",
        "p4_short_term_binding": "short_term_price_binding",
    }
    raw_criteria: dict[str, Any] = {}
    for component_key, criterion_key in raw_keys.items():
        component = indexed[component_key]
        inputs = component.get("inputs")
        evidence = component.get("evidence")
        if not isinstance(inputs, Mapping) or not isinstance(evidence, Mapping):
            errors.append(f"Patch 4 fact binding invalid: {component_key}")
            continue
        if criterion_key in {"core_rd_ownership_pct", "esop_core_talent_coverage_pct"}:
            if set(inputs) != {"value", "unit"} or inputs.get("unit") != "percentage_points":
                errors.append(f"Patch 4 percentage input invalid: {component_key}")
                continue
        elif set(inputs) != {"value"}:
            errors.append(f"Patch 4 boolean input invalid: {component_key}")
            continue
        raw_criteria[criterion_key] = {"value": inputs.get("value"), "evidence": evidence}
    raw = {
        "schema_version": PATCH4_SCHEMA_VERSION,
        "model_id": PATCH4_MODEL_ID,
        "code": code,
        "as_of": as_of,
        "criteria": raw_criteria,
    }
    try:
        normalized = normalise_patch4_assessment(raw, security_code=code, as_of=as_of)
    except QualityEquityError:
        normalized = None
        errors.append("Patch 4 atomic evidence invalid")
    if normalized is not None:
        criteria = normalized["criteria"]
        expected_scores = {
            "p4_core_rd_ownership": _linear(
                float(criteria["core_rd_ownership_pct"]["value"]),
                [(0.0, 0.0), (1.0, 2.0), (3.0, 6.0), (5.0, 9.0), (5.000001, 10.0)],
            ),
            "p4_esop_coverage": _linear(
                float(criteria["esop_core_talent_coverage_pct"]["value"]),
                [(0.0, 0.0), (10.0, 3.0), (20.0, 6.0), (30.0, 9.0), (30.000001, 10.0)],
            ),
            "p4_long_term_rd_link": 10.0 if criteria["long_term_rd_metrics"]["value"] else 0.0,
            "p4_frontline_rd_equity": 10.0 if criteria["frontline_rd_equity"]["value"] else 0.0,
            "p4_short_term_binding": 0.0 if criteria["short_term_price_binding"]["value"] else 10.0,
        }
        expected_formulas = {
            "p4_core_rd_ownership": "piecewise(core_rd_ownership_pct;5%+=10)",
            "p4_esop_coverage": "piecewise(esop_core_talent_coverage_pct;30%+=10)",
            "p4_long_term_rd_link": "10 if long_term_rd_metrics else 0",
            "p4_frontline_rd_equity": "10 if frontline_rd_equity else 0",
            "p4_short_term_binding": "0 if short_term_price_binding else 10",
        }
        for key, expected_score in expected_scores.items():
            component = indexed[key]
            if (
                not math.isclose(float(component.get("score", -1)), expected_score, abs_tol=0.0001)
                or component.get("complete") is not True
                or component.get("formula") != expected_formulas[key]
            ):
                errors.append(f"Patch 4 fact score mismatch: {key}")

    expected_score = round(math.fsum(float(component["points"]) for component in indexed.values()) / 10.0, 2)
    expected_complete = all(component.get("complete") is True for component in indexed.values())
    if (
        not math.isclose(float(assessment.get("score", -1)), expected_score, abs_tol=0.0001)
        or assessment.get("complete") is not expected_complete
    ):
        errors.append("Patch 4 total mismatch")
    return errors


def _validate_quality_equity_ledger_impl(ledger: Any) -> list[str]:
    """Independently replay Type 7 arithmetic and intersection semantics."""

    if not isinstance(ledger, Mapping):
        return ["ledger is not a mapping"]
    errors: list[str] = []
    expected_top_level = {
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
    if set(ledger) != expected_top_level:
        errors.append("ledger structure invalid")

    def close(actual: Any, expected: float, *, tolerance: float = 1e-9) -> bool:
        value = _finite(actual)
        return value is not None and math.isclose(value, expected, rel_tol=0.0, abs_tol=tolerance)

    def replay_template(section_key: str, expected_weights: Mapping[str, float]):
        section = ledger.get(section_key)
        if not isinstance(section, Mapping) or set(section) != {"score", "coverage", "items"}:
            errors.append(f"{section_key} structure invalid")
            return None, {}
        items = section.get("items")
        if not isinstance(items, list) or len(items) != len(expected_weights):
            errors.append(f"{section_key} structure invalid")
            return section, {}
        indexed: dict[str, Mapping[str, Any]] = {}
        point_values: dict[str, float] = {}
        complete_values: dict[str, bool] = {}
        for item in items:
            required = {
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
            if not isinstance(item, Mapping) or set(item) != required:
                errors.append(f"{section_key} item structure invalid")
                continue
            key = item.get("key")
            if not isinstance(key, str) or key in indexed or key not in expected_weights:
                errors.append(f"{section_key} item identity invalid")
                continue
            score = _finite(item.get("score"))
            weight = _finite(item.get("weight"))
            points = _finite(item.get("points"))
            complete = item.get("complete")
            inputs = item.get("inputs")
            if section_key == "template1":
                metadata = _TEMPLATE1_ITEM_CONTRACTS.get(key)
                expected_label = metadata[0] if metadata is not None else None
                expected_formula = metadata[1] if metadata is not None else None
            else:
                expected_label = _TEMPLATE5_ITEM_LABELS.get(key)
                expected_formula = "Template5_source_weight*observable_score"
            input_valid, replayed_score = (
                _template_item_contract(section_key, key, inputs) if isinstance(inputs, Mapping) else (False, None)
            )
            if (
                score is None
                or not 0 <= score <= 10
                or weight is None
                or not close(weight, expected_weights[key])
                or points is None
                or not close(points, round(score * expected_weights[key] / 10.0, 4))
                or not isinstance(complete, bool)
                or item.get("label") != expected_label
                or item.get("formula") != expected_formula
                or item.get("evidence_level") not in _TEMPLATE_EVIDENCE_LEVELS
                or not input_valid
            ):
                errors.append(f"{section_key} item arithmetic invalid")
            if replayed_score is not None and score is not None and not close(score, replayed_score, tolerance=0.0001):
                errors.append(f"{section_key} item input-score mismatch")
            if points is not None:
                point_values[key] = points
            if isinstance(complete, bool):
                complete_values[key] = complete
            indexed[key] = item
        if set(indexed) != set(expected_weights):
            errors.append(f"{section_key} item set invalid")
            return section, indexed
        if set(point_values) == set(expected_weights):
            replay = round(math.fsum(point_values.values()), 2)
            if not close(section.get("score"), replay, tolerance=0.0001):
                errors.append(f"{section_key} total mismatch")
        if set(complete_values) == set(expected_weights):
            coverage = round(
                math.fsum(expected_weights[key] for key, complete in complete_values.items() if complete) / 100.0,
                4,
            )
            if not close(section.get("coverage"), coverage, tolerance=0.0001):
                errors.append(f"{section_key} coverage mismatch")
        return section, indexed

    if (
        ledger.get("schema_version") != SCHEMA_VERSION
        or ledger.get("model_id") != MODEL_ID
        or not isinstance(ledger.get("code"), str)
        or not re.fullmatch(r"[036][0-9]{5}", ledger["code"])
        or ledger.get("source_rule") != "Template1>70 AND Template5>70 AND Patch5>70"
        or not close(ledger.get("strict_threshold"), STRICT_THRESHOLD)
    ):
        errors.append("model identity mismatch")

    template1, template1_items = replay_template("template1", _TEMPLATE1_ITEM_WEIGHTS)
    template5, template5_items = replay_template("template5", _TEMPLATE5_ITEM_WEIGHTS)

    patch5 = ledger.get("patch5")
    patch_sections: dict[str, Mapping[str, Any]] = {}
    if not isinstance(patch5, Mapping) or set(patch5) != {
        "score",
        "coverage",
        "safety_margin_score",
        "safety_margin_complete",
        "dimensions",
    }:
        errors.append("patch5 structure invalid")
    elif not isinstance(patch5.get("dimensions"), list) or len(patch5["dimensions"]) != 5:
        errors.append("patch5 structure invalid")
    else:
        section_point_values: dict[str, float] = {}
        section_complete_values: dict[str, bool] = {}
        for section in patch5["dimensions"]:
            if not isinstance(section, Mapping) or set(section) != {
                "key",
                "label",
                "max_points",
                "components",
                "points",
                "complete",
            }:
                errors.append("patch5 dimension structure invalid")
                continue
            key = section.get("key")
            expected_components = _PATCH5_COMPONENT_WEIGHTS.get(key) if isinstance(key, str) else None
            if expected_components is None or key in patch_sections:
                errors.append("patch5 dimension identity invalid")
                continue
            components = section.get("components")
            if (
                not close(section.get("max_points"), 20.0)
                or section.get("label") != _PATCH5_SECTION_LABELS.get(key)
                or not isinstance(components, list)
                or len(components) != len(expected_components)
                or not isinstance(section.get("complete"), bool)
            ):
                errors.append(f"patch5 {key} structure invalid")
                continue
            indexed_components: dict[str, Mapping[str, Any]] = {}
            component_point_values: dict[str, float] = {}
            component_complete_values: dict[str, bool] = {}
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
                    errors.append(f"patch5 {key} component structure invalid")
                    continue
                component_key = component.get("key")
                if (
                    not isinstance(component_key, str)
                    or component_key in indexed_components
                    or component_key not in expected_components
                ):
                    errors.append(f"patch5 {key} component identity invalid")
                    continue
                score = _finite(component.get("score"))
                points = _finite(component.get("points"))
                complete = component.get("complete")
                inputs = component.get("inputs")
                maximum = expected_components[component_key]
                expected_input_fields = {"source"} if component_key in _PATCH5_SOURCE_INPUT_COMPONENTS else set()
                inputs_valid = isinstance(inputs, Mapping) and set(inputs) == expected_input_fields
                if inputs_valid and expected_input_fields:
                    inputs_valid = inputs.get("source") in _PATCH5_SOURCE_LEVELS
                if (
                    score is None
                    or not 0 <= score <= 10
                    or not close(component.get("max_points"), maximum)
                    or points is None
                    or not close(points, round(score * maximum / 10.0, 4))
                    or not isinstance(complete, bool)
                    or component.get("label") != _PATCH5_COMPONENT_LABELS.get(component_key)
                    or component.get("formula") != f"{maximum:g}*score/10"
                    or not inputs_valid
                ):
                    errors.append(f"patch5 {key} component arithmetic invalid")
                if points is not None:
                    component_point_values[component_key] = points
                if isinstance(complete, bool):
                    component_complete_values[component_key] = complete
                indexed_components[component_key] = component
            if set(indexed_components) != set(expected_components):
                errors.append(f"patch5 {key} component set invalid")
                continue
            section_points = _finite(section.get("points"))
            if set(component_point_values) == set(expected_components):
                expected_points = round(math.fsum(component_point_values.values()), 4)
                if section_points is None or not close(section_points, expected_points, tolerance=0.0001):
                    errors.append(f"patch5 {key} points mismatch")
            elif section_points is None:
                errors.append(f"patch5 {key} points mismatch")
            if set(component_complete_values) == set(expected_components):
                expected_complete = all(component_complete_values.values())
                if section.get("complete") is not expected_complete:
                    errors.append(f"patch5 {key} completeness mismatch")
            if section_points is not None:
                section_point_values[key] = section_points
            if isinstance(section.get("complete"), bool):
                section_complete_values[key] = section["complete"]
            patch_sections[key] = section
        if set(patch_sections) != set(_PATCH5_COMPONENT_WEIGHTS):
            errors.append("patch5 dimension set invalid")
        else:
            replay = round(math.fsum(section_point_values.values()), 2) if len(section_point_values) == 5 else None
            coverage = (
                round(math.fsum(20.0 for complete in section_complete_values.values() if complete) / 100.0, 4)
                if len(section_complete_values) == 5
                else None
            )
            safety = patch_sections["p5_safety"]
            safety_points = _finite(safety.get("points"))
            if replay is None or not close(patch5.get("score"), replay, tolerance=0.0001):
                errors.append("patch5 total mismatch")
            if coverage is None or not close(patch5.get("coverage"), coverage, tolerance=0.0001):
                errors.append("patch5 coverage mismatch")
            if safety_points is None or not close(
                patch5.get("safety_margin_score"), round(safety_points, 2), tolerance=0.0001
            ):
                errors.append("patch5 safety score mismatch")
            if patch5.get("safety_margin_complete") is not safety["complete"]:
                errors.append("patch5 safety completeness mismatch")

    scores = ledger.get("scores")
    score_values: dict[str, float] = {}
    expected_sections = {"template1": template1, "template5": template5, "patch5": patch5}
    if not isinstance(scores, Mapping) or set(scores) != set(expected_sections):
        errors.append("score map invalid")
    else:
        for key, section in expected_sections.items():
            value = _finite(scores.get(key))
            section_score = _finite(section.get("score")) if isinstance(section, Mapping) else None
            if value is None or section_score is None or not close(value, section_score, tolerance=0.0001):
                errors.append(f"{key} published score mismatch")
            if value is not None:
                score_values[key] = value

    strict_checks = ledger.get("strict_checks")
    expected_strict = {key: value > STRICT_THRESHOLD for key, value in score_values.items()}
    if (
        len(expected_strict) != 3
        or not isinstance(strict_checks, Mapping)
        or set(strict_checks) != {"template1", "template5", "patch5"}
        or any(not isinstance(value, bool) for value in strict_checks.values())
        or dict(strict_checks) != expected_strict
    ):
        errors.append("strict threshold checks mismatch")
    expected_intersection = len(expected_strict) == 3 and all(expected_strict.values())
    if not isinstance(ledger.get("all_scores_strictly_above_70"), bool) or (
        ledger.get("all_scores_strictly_above_70") is not expected_intersection
    ):
        errors.append("strict threshold intersection mismatch")

    prerequisites = ledger.get("prerequisites")
    prerequisite_passes: dict[str, bool] = {}
    if not isinstance(prerequisites, Mapping) or set(prerequisites) != _PREREQUISITE_KEYS:
        errors.append("prerequisites structure invalid")
    else:
        for key, record in prerequisites.items():
            if not isinstance(record, Mapping) or not isinstance(record.get("passed"), bool):
                errors.append(f"prerequisite {key} invalid")
                continue
            prerequisite_passes[key] = record["passed"]
        core = prerequisites["core_modules_80pct"]
        core_actual = _finite(core.get("actual")) if isinstance(core, Mapping) else None
        expected_core = _finite(template1.get("coverage")) if isinstance(template1, Mapping) else None
        expected_incomplete_required_items = _incomplete_required_item_ids(
            template1 if isinstance(template1, Mapping) else {},
            template5 if isinstance(template5, Mapping) else {},
            patch5 if isinstance(patch5, Mapping) else {},
        )
        expected_required_items_complete = not expected_incomplete_required_items
        expected_core_passed = bool(core_actual is not None and core_actual >= MIN_CORE_COVERAGE)
        if (
            not isinstance(core, Mapping)
            or set(core)
            != {
                "passed",
                "actual",
                "required",
                "required_items_complete",
                "incomplete_required_items",
            }
            or core_actual is None
            or expected_core is None
            or not close(core_actual, expected_core, tolerance=0.0001)
            or not close(core.get("required"), MIN_CORE_COVERAGE)
            or core.get("required_items_complete") is not expected_required_items_complete
            or core.get("incomplete_required_items") != expected_incomplete_required_items
            or core["passed"] is not expected_core_passed
        ):
            errors.append("core coverage prerequisite mismatch")
        technology = prerequisites["technology_patch4"]
        technology_applicable = technology.get("applicable") if isinstance(technology, Mapping) else None
        applicability = technology.get("applicability") if isinstance(technology, Mapping) else None
        assessment = technology.get("assessment") if isinstance(technology, Mapping) else None
        template_technology = template1_items.get("t1_17", {})
        template_technology_score = _finite(template_technology.get("score"))
        template_technology_complete = template_technology.get("complete")
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
            and template_technology_complete is True
            and template_technology_score is not None
            and template_technology_score < 7.0
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
        patch4_as_of = (
            prerequisites.get("latest_quote_and_valuation", {}).get("as_of")
            if isinstance(prerequisites.get("latest_quote_and_valuation"), Mapping)
            else None
        )
        patch4_errors = (
            _validate_patch4_ledger(
                assessment,
                code=str(ledger.get("code") or ""),
                as_of=str(patch4_as_of or ""),
                fairness_item=template1_items.get("t1_08", {}),
                governance_component=governance_component,
            )
            if assessment is not None
            else []
        )
        expected_incentive_score = (
            patch4_score
            if expected_technology_applicable and patch4_score is not None
            else 2.0
            if expected_technology_applicable
            else _finite(template1_items.get("t1_08", {}).get("score"))
        )
        expected_incentive_complete = (
            patch4_complete
            if expected_technology_applicable
            else template1_items.get("t1_08", {}).get("complete") is True
        )
        if (
            not isinstance(technology, Mapping)
            or set(technology) != {"passed", "applicable", "score", "validation_status", "applicability", "assessment"}
            or not isinstance(technology_applicable, bool)
            or not isinstance(applicability, Mapping)
            or set(applicability) != {"technology_score", "technology_score_complete", "rd_intensity", "rule"}
            or applicability.get("rule")
            != "Patch4 waived only if reported_rd_intensity<0.05 AND validated_technology_score<7"
            or template_technology_score is None
            or published_technology_score is None
            or not close(published_technology_score, template_technology_score)
            or published_technology_complete is not template_technology_complete
            or (rd_intensity is not None and not 0 <= rd_intensity <= 1)
            or technology_applicable is not expected_technology_applicable
            or technology.get("score") != (patch4_score if patch4_complete else None)
            or technology.get("validation_status") != expected_technology_status
            or technology["passed"] is not (not expected_technology_applicable or patch4_complete)
            or bool(patch4_errors)
            or expected_incentive_score is None
            or not close(incentive_component.get("score"), expected_incentive_score)
            or incentive_component.get("complete") is not expected_incentive_complete
        ):
            errors.append("technology prerequisite mismatch")
        financials = prerequisites["three_year_financials"]
        years = financials.get("consecutive_years") if isinstance(financials, Mapping) else None
        financial_history_inputs = template1_items.get("t1_19", {}).get("inputs", {})
        shareholder_financial_inputs = (
            financial_history_inputs.get("shareholder_return")
            if isinstance(financial_history_inputs, Mapping)
            else None
        )
        annual_financial_contract = (
            shareholder_financial_inputs.get("annual_financial_history_contract")
            if isinstance(shareholder_financial_inputs, Mapping)
            else None
        )
        expected_financial_fields = {
            "source_trade_date",
            "financial_indicator_as_of",
            "revenue_values",
            "revenue_years",
            "net_profit_history",
            "net_profit_years",
        }
        replayed_financial_years = (
            _consecutive_year_count(annual_financial_contract)
            if isinstance(annual_financial_contract, Mapping)
            and set(annual_financial_contract) == expected_financial_fields
            else None
        )
        if (
            not isinstance(financials, Mapping)
            or set(financials) != {"passed", "consecutive_years"}
            or isinstance(years, bool)
            or not isinstance(years, int)
            or years < 0
            or replayed_financial_years is None
            or years != replayed_financial_years
            or financials["passed"] is not (years >= 3)
        ):
            errors.append("financial history prerequisite mismatch")
        valuation = prerequisites["latest_quote_and_valuation"]
        valuation_as_of = _parse_evidence_date(valuation.get("as_of")) if isinstance(valuation, Mapping) else None
        valuation_complete = valuation.get("valuation_complete") if isinstance(valuation, Mapping) else None
        expected_valuation_complete = bool(template1_items.get("t1_20", {}).get("complete"))
        expected_valuation_passed = bool(
            expected_valuation_complete and valuation_as_of is not None and valuation_as_of <= date.today()
        )
        if (
            not isinstance(valuation, Mapping)
            or set(valuation) != {"passed", "as_of", "valuation_complete", "validation_basis"}
            or valuation.get("validation_basis") != "source_bound_nonfinancial_dcf"
            or not isinstance(valuation_complete, bool)
            or valuation_complete is not expected_valuation_complete
            or valuation["passed"] is not expected_valuation_passed
        ):
            errors.append("valuation prerequisite mismatch")
        reports = prerequisites["three_external_reports"]
        report_sources = reports.get("sources") if isinstance(reports, Mapping) else None
        try:
            normalized_sources = normalise_research_sources(
                report_sources,
                today=valuation_as_of if valuation_as_of is not None else date.min,
                security_code=str(ledger.get("code") or ""),
            )
            replayed_metadata = research_metadata_precheck(
                normalized_sources,
                reference=valuation_as_of if valuation_as_of is not None else date.min,
            )
        except QualityEquityError:
            normalized_sources = None
            replayed_metadata = None
        expected_reports_passed = bool(
            isinstance(report_sources, list)
            and normalized_sources == report_sources
            and isinstance(replayed_metadata, Mapping)
            and replayed_metadata.get("passed") is True
        )
        if (
            not isinstance(reports, Mapping)
            or set(reports)
            != {
                "passed",
                "check_type",
                "source_count",
                "distinct_publishers",
                "recent_source_count",
                "max_age_days",
                "recent_age_days",
                "sources",
            }
            or not isinstance(report_sources, list)
            or reports.get("check_type") != "metadata_availability_precheck"
            or reports.get("max_age_days") != RESEARCH_MAX_AGE_DAYS
            or reports.get("recent_age_days") != RESEARCH_RECENT_AGE_DAYS
            or not isinstance(replayed_metadata, Mapping)
            or reports.get("source_count") != replayed_metadata.get("source_count")
            or reports.get("distinct_publishers") != replayed_metadata.get("distinct_publishers")
            or reports.get("recent_source_count") != replayed_metadata.get("recent_source_count")
            or reports["passed"] is not expected_reports_passed
        ):
            errors.append("external reports prerequisite mismatch")
        content_verification = prerequisites["external_report_content_verification"]
        try:
            normalized_content_verification = normalise_research_content_verification(
                content_verification,
                sources=normalized_sources if isinstance(normalized_sources, list) else [],
                security_code=str(ledger.get("code") or ""),
                as_of=valuation_as_of.isoformat() if valuation_as_of is not None else "0001-01-01",
            )
        except QualityEquityError:
            normalized_content_verification = None
        if normalized_content_verification != content_verification:
            errors.append("external report content prerequisite mismatch")
        history = prerequisites["ten_year_return_and_five_year_valuation"]
        t1_history_inputs = template1_items.get("t1_19", {}).get("inputs", {})
        shareholder_input = (
            t1_history_inputs.get("shareholder_return") if isinstance(t1_history_inputs, Mapping) else None
        )
        history_as_of = history.get("as_of") if isinstance(history, Mapping) else None
        history_date = _history_date(history_as_of)
        valuation_history_input = (
            shareholder_input.get("valuation_history_contract") if isinstance(shareholder_input, Mapping) else None
        )
        expected_history = bool(
            history_date is not None
            and _valid_shareholder_return(shareholder_input, history_date)
            and _valid_valuation_history(valuation_history_input, history_date)
            and template5_items.get("t5_v1", {}).get("complete") is True
        )
        if (
            not isinstance(history, Mapping)
            or set(history) != {"passed", "as_of"}
            or history["passed"] is not expected_history
            or (history_as_of is not None and history_date is None)
            or (
                history["passed"]
                and history_as_of != (valuation.get("as_of") if isinstance(valuation, Mapping) else None)
            )
        ):
            errors.append("market history prerequisite mismatch")

    expected_prerequisites_complete = len(prerequisite_passes) == len(_PREREQUISITE_KEYS) and all(
        prerequisite_passes.values()
    )
    if not isinstance(ledger.get("prerequisites_complete"), bool) or (
        ledger.get("prerequisites_complete") is not expected_prerequisites_complete
    ):
        errors.append("prerequisite intersection mismatch")
    safety_complete = bool(isinstance(patch5, Mapping) and patch5.get("safety_margin_complete") is True)
    safety_score = _finite(patch5.get("safety_margin_score")) if isinstance(patch5, Mapping) else None
    expected_safety_veto = bool(safety_complete and safety_score is not None and safety_score < PATCH5_SAFETY_VETO)
    if not isinstance(ledger.get("safety_veto"), bool) or ledger.get("safety_veto") is not expected_safety_veto:
        errors.append("safety veto mismatch")
    expected_trigger = expected_intersection and expected_prerequisites_complete and not expected_safety_veto
    if not isinstance(ledger.get("triggered"), bool) or ledger.get("triggered") is not expected_trigger:
        errors.append("trigger decision mismatch")

    expected_decisive_upper: dict[str, float] = {}
    if isinstance(template1, Mapping) and isinstance(template5, Mapping) and isinstance(patch5, Mapping):
        try:
            expected_decisive_upper = decisive_score_upper_bounds(template1, template5, patch5)
        except QualityEquityError:
            expected_decisive_upper = {}
    published_decisive_upper = ledger.get("decisive_score_upper_bounds")
    if (
        len(expected_decisive_upper) != 3
        or not isinstance(published_decisive_upper, Mapping)
        or set(published_decisive_upper) != set(expected_decisive_upper)
        or any(
            not close(published_decisive_upper.get(key), value, tolerance=0.0001)
            for key, value in expected_decisive_upper.items()
        )
    ):
        errors.append("decisive score upper bounds mismatch")
    expected_decisive_failure = bool(
        len(expected_decisive_upper) == 3
        and any(value <= STRICT_THRESHOLD for value in expected_decisive_upper.values())
    )
    if not isinstance(ledger.get("decisively_not_triggered"), bool) or (
        ledger.get("decisively_not_triggered") is not expected_decisive_failure
    ):
        errors.append("decisive failure decision mismatch")

    upper_bounds = ledger.get("upper_bounds_without_history")
    expected_upper: dict[str, float] = {}
    if (
        len(score_values) == 3
        and "t1_18" in template1_items
        and "t1_19" in template1_items
        and "t1_20" in template1_items
        and "t5_v1" in template5_items
        and "t5_v3" in template5_items
        and "p5_safety" in patch_sections
    ):
        safety_components = {
            item.get("key"): item
            for item in patch_sections["p5_safety"].get("components", [])
            if isinstance(item, Mapping)
        }
        valuation_component = safety_components.get("p5_s1")
        if isinstance(valuation_component, Mapping):
            dcf_score = _finite(template1_items["t1_20"].get("score"))
            if dcf_score is not None:
                expected_upper = {
                    "template1": round(
                        min(
                            100.0,
                            round(score_values["template1"], 2)
                            - float(template1_items["t1_18"]["points"])
                            - float(template1_items["t1_19"]["points"])
                            + 10.0,
                        ),
                        2,
                    ),
                    "template5": round(
                        min(
                            100.0,
                            round(score_values["template5"], 2)
                            - float(template5_items["t5_v1"]["points"])
                            - float(template5_items["t5_v3"]["points"])
                            + 18.0,
                        ),
                        2,
                    ),
                    "patch5": round(
                        min(
                            100.0,
                            round(score_values["patch5"], 2)
                            - float(valuation_component["points"])
                            + 8.0 * _avg(dcf_score, 10.0) / 10.0,
                        ),
                        2,
                    ),
                }
    if (
        len(expected_upper) != 3
        or not isinstance(upper_bounds, Mapping)
        or set(upper_bounds) != set(expected_upper)
        or any(not close(upper_bounds.get(key), value, tolerance=0.0001) for key, value in expected_upper.items())
    ):
        errors.append("history upper bounds mismatch")
    history_passed = prerequisite_passes.get("ten_year_return_and_five_year_valuation", False)
    history_request_core_coverage = _finite(template1.get("coverage")) if isinstance(template1, Mapping) else None
    history_request_core_ready = bool(
        history_request_core_coverage is not None and history_request_core_coverage >= MIN_CORE_COVERAGE
    )
    pre_history_passed = (
        len(prerequisite_passes) == len(_PREREQUISITE_KEYS)
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
    )
    expected_request = bool(
        not history_passed
        and pre_history_passed
        and not expected_safety_veto
        and len(expected_upper) == 3
        and all(value > STRICT_THRESHOLD for value in expected_upper.values())
        and not expected_decisive_failure
    )
    if not isinstance(ledger.get("history_request_needed"), bool) or (
        ledger.get("history_request_needed") is not expected_request
    ):
        errors.append("history request decision mismatch")
    pre_research_passed = len(prerequisite_passes) == len(_PREREQUISITE_KEYS) and all(
        value
        for key, value in prerequisite_passes.items()
        if key not in {"three_external_reports", "external_report_content_verification"}
    )
    expected_research_request = bool(
        (
            not prerequisite_passes.get("three_external_reports", False)
            or not prerequisite_passes.get("external_report_content_verification", False)
        )
        and pre_research_passed
        and not expected_safety_veto
        and (not expected_decisive_failure or (len(score_values) == 3 and min(score_values.values()) >= 60.0))
    )
    if not isinstance(ledger.get("research_request_needed"), bool) or (
        ledger.get("research_request_needed") is not expected_research_request
    ):
        errors.append("research request decision mismatch")
    return errors


def validate_quality_equity_ledger(ledger: Any) -> list[str]:
    """Fail closed for any malformed external ledger value."""

    try:
        return _validate_quality_equity_ledger_impl(ledger)
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        return ["ledger contains malformed values"]


__all__ = [
    "MAX_RESEARCH_SOURCES",
    "MIN_CORE_COVERAGE",
    "MIN_RESEARCH_SOURCES",
    "MODEL_ID",
    "PATCH4_FORMULA_VERSION",
    "PATCH4_MAX_EVIDENCE_AGE_DAYS",
    "PATCH4_MODEL_ID",
    "PATCH4_SCHEMA_VERSION",
    "PATCH5_SAFETY_VETO",
    "RESEARCH_MAX_AGE_DAYS",
    "RESEARCH_RECENT_AGE_DAYS",
    "RESEARCH_EVIDENCE_MODEL_ID",
    "RESEARCH_CONTENT_MODEL_ID",
    "MIN_RESEARCH_BODY_SOURCES",
    "MIN_CROSSCHECK_REPORTS",
    "MIN_RESEARCH_BODY_CHARACTERS",
    "MAX_RESEARCH_BODY_CHARACTERS",
    "MAX_RESEARCH_BODY_FETCHES",
    "MAX_RESEARCH_FACTS_PER_BODY",
    "MAX_RESEARCH_FACT_ABS_VALUE",
    "RESEARCH_FACT_RELATIVE_TOLERANCE",
    "RESEARCH_CONTENT_BODY_FIELDS",
    "RESEARCH_CONTENT_FACT_FIELDS",
    "RESEARCH_CONTENT_IDENTITY_CHECKS",
    "RESEARCH_CONTENT_SIGNALS",
    "RESEARCH_FACT_METRICS",
    "RESEARCH_FACT_UNITS",
    "RESEARCH_FACT_UNIT_BY_METRIC",
    "STRICT_THRESHOLD",
    "TYPE7_DIRECT_SCORE_KEYS",
    "QualityEquityError",
    "assess_quality_equity",
    "decisive_score_upper_bounds",
    "normalise_research_sources",
    "normalise_research_content_verification",
    "normalise_patch4_assessment",
    "research_metadata_precheck",
    "validate_quality_equity_ledger",
]
