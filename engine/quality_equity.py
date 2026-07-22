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


MODEL_ID = "patch6-type7-quality-equity-v5"
SCHEMA_VERSION = 5
STRICT_THRESHOLD = 70.0
PATCH5_SAFETY_VETO = 8.0
MIN_CORE_COVERAGE = 0.80
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
        "mean(growth_sustainability,runway,revenue_growth)",
        ({"growth_sustainability", "runway", "revenue_growth"},),
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
        "mean(moat_durability,growth_sustainability,ROIC_spread)",
        ({"moat_durability", "growth_sustainability", "roic"},),
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
    "patch4_shareholder_culture_score",
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
    level = str(metric.get(f"{key}_evidence_level") or "primary")
    if level not in {"primary", "derived_proxy"}:
        return None, False, level
    return float(score), True, level


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


def _growth_rate(values: Any, years: Any, *, minimum: int = 3) -> float | None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    if not isinstance(years, Sequence) or isinstance(years, (str, bytes)) or len(values) != len(years):
        return None
    points: list[tuple[int, float]] = []
    for raw_year, raw_value in zip(years, values):
        value = _finite(raw_value)
        try:
            year = int(raw_year)
        except (TypeError, ValueError, OverflowError):
            continue
        if value is not None:
            points.append((year, value))
    points.sort()
    if len(points) < minimum:
        return None
    points = points[-max(minimum, min(5, len(points))) :]
    if any(current[0] - prior[0] != 1 for prior, current in zip(points, points[1:])):
        return None
    if points[0][1] <= 0 or points[-1][1] <= 0:
        return None
    return (points[-1][1] / points[0][1]) ** (1.0 / (points[-1][0] - points[0][0])) - 1.0


def _consecutive_year_count(metric: Mapping[str, Any]) -> int:
    year_sets: list[set[int]] = []
    for key in ("revenue_years", "net_profit_years"):
        raw = metric.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return 0
        years: set[int] = set()
        for value in raw:
            try:
                years.add(int(value))
            except (TypeError, ValueError, OverflowError):
                continue
        year_sets.append(years)
    common = sorted(set.intersection(*year_sets)) if year_sets else []
    longest = 0
    current = 0
    previous: int | None = None
    for year in common:
        current = current + 1 if previous is not None and year - previous == 1 else 1
        longest = max(longest, current)
        previous = year
    return longest


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
    for basis, current_key, median_key, growth_rate in (
        ("PE_TTM", "current_pe_ttm", "median_pe_ttm", earnings_growth_rate),
        ("PB_MRQ", "current_pb_mrq", "median_pb_mrq", book_value_growth_rate),
    ):
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


def _history_record(value: Any, code: str, as_of: str | None) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("model_id") != "type7-market-history-v1" or str(value.get("code") or "") != code:
        return None
    if _parse_evidence_date(as_of) is None:
        return None
    record_as_of = str(value.get("as_of") or "")
    if record_as_of != as_of:
        return None
    if _parse_evidence_date(record_as_of) is None:
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
    catalyst = verified("catalyst_score")
    growth_sustainability = verified("growth_sustainability_score")

    revenue_rate = _finite(metric.get("trend_growth"))
    profit_rate = _growth_rate(metric.get("net_profit_history"), metric.get("net_profit_years"))
    fcf_rate = _growth_rate(metric.get("fcf_history"), metric.get("fcf_years"))
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
    cyclicality = _avg(
        _linear(volatility, [(0.0, 10), (0.20, 9), (0.50, 7), (1.0, 4), (2.0, 1)]),
        _linear(consistency, [(0.0, 10), (0.30, 8), (0.70, 6), (1.20, 3), (2.0, 1)]),
    )
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
    shareholder_available = isinstance(shareholder_history, Mapping) and shareholder_history.get("available") is True
    valuation_available = isinstance(valuation_history, Mapping) and valuation_history.get("available") is True
    return_cagr = _finite(shareholder_history.get("cagr")) if shareholder_available else None
    return_10y = _return_score(return_cagr)
    percentiles = []
    if valuation_available:
        for key in ("pe_percentile", "pb_percentile"):
            value = _finite(valuation_history.get(key))
            if value is not None and 0 <= value <= 1:
                percentiles.append(value)
    valuation_percentile = median(percentiles) if percentiles else None
    historical_valuation = _linear(
        valuation_percentile,
        [(0.0, 10), (0.10, 9), (0.30, 8), (0.50, 6.5), (0.70, 5), (0.90, 2), (1.0, 0)],
        missing=0,
    )
    forecast_growth = _forecast_growth_rate(revenue_rate, profit_rate, fcf_rate)
    total_equity_growth = _growth_rate(metric.get("equity_history"), metric.get("equity_years"))
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
    return_and_terminal_inputs = {
        "shareholder_return": dict(shareholder_history) if isinstance(shareholder_history, Mapping) else {},
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
        "growth_sustainability": (*growth_sustainability, {"score": growth_sustainability[0]}),
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
            shareholder_available,
            "independent_market_history",
            dict(shareholder_history) if isinstance(shareholder_history, Mapping) else {},
        ),
        "return_and_terminal_profit": (
            return_and_terminal,
            shareholder_available and terminal_profit_complete,
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
                if isinstance(valuation_history, Mapping)
                else None,
                "pb_percentile": valuation_history.get("pb_percentile")
                if isinstance(valuation_history, Mapping)
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
    growth_potential = _avg(values["growth_sustainability"][0], values["runway"][0], values["revenue_growth"][0])
    health = _avg(values["accounting"][0], values["balance"][0], values["roic"][0])
    advantage = _avg(values["moat"][0], values["moat_durability"][0])
    cost_control = _avg(values["margin"][0], values["accounting"][0])
    market_position = _avg(values["moat"][0], values["industry"][0])
    wealth = _avg(values["moat_durability"][0], values["growth_sustainability"][0], values["roic"][0])

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
            complete=all(values[key][1] for key in ("growth_sustainability", "runway", "revenue_growth")),
            formula="mean(growth_sustainability,runway,revenue_growth)",
            inputs={key: values[key][0] for key in ("growth_sustainability", "runway", "revenue_growth")},
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
            complete=all(values[key][1] for key in ("moat_durability", "growth_sustainability", "roic")),
            formula="mean(moat_durability,growth_sustainability,ROIC_spread)",
            inputs={key: values[key][0] for key in ("moat_durability", "growth_sustainability", "roic")},
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
    metric: Mapping[str, Any], values: Mapping[str, tuple[float, bool, str, Mapping[str, Any]]]
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
            _avg(values["technology"][0], values["growth_sustainability"][0]),
            values["technology"][1] and values["growth_sustainability"][1],
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
                _patch_component("p5_c2", "激励一致", 5, values["shareholder"][0], values["shareholder"][1], {}),
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
    template1 = _make_template1(values)
    template5 = _make_template5(values)
    patch5 = _make_patch5(metric, values)

    metric_as_of = _parse_evidence_date(metric.get("source_trade_date"))
    quote_date_complete = metric_as_of is not None and metric_as_of <= date.today()
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
    technology_score = values["technology"][0]
    rd_intensity = _finite(metric.get("rd_intensity"))
    technology_company = bool((rd_intensity is not None and rd_intensity >= 0.05) or technology_score >= 7.0)
    # No production Patch 4 ledger/validator exists yet.  A naked 0-10 field,
    # even with a generic evidence wrapper, cannot prove that Patch 4 was
    # actually applied.  Technology candidates therefore fail closed until a
    # structured, independently replayable Patch 4 assessment is introduced.
    patch4_score = None
    patch4_complete = False
    history_complete = values["return_10y"][1] and values["historical_valuation"][1]
    valuation_complete = values["dcf"][1]
    core_coverage = template1["coverage"]
    prerequisites = {
        "core_modules_80pct": {
            "passed": core_coverage >= MIN_CORE_COVERAGE,
            "actual": core_coverage,
            "required": MIN_CORE_COVERAGE,
        },
        "technology_patch4": {
            "passed": not technology_company or patch4_complete,
            "applicable": technology_company,
            "score": patch4_score,
            "validation_status": ("missing_validated_patch4_assessment" if technology_company else "not_applicable"),
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
        bool(record["passed"])
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
        and not decisively_not_triggered
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
        "t1_02": ("growth_sustainability", "runway", "revenue_growth"),
        "t1_06": ("accounting", "balance", "roic"),
        "t1_09": ("moat", "durability"),
        "t1_11": ("margin", "accounting"),
        "t1_14": ("moat", "industry"),
        "t1_15": ("moat_durability", "growth_sustainability", "roic"),
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
        if (
            not isinstance(core, Mapping)
            or set(core) != {"passed", "actual", "required"}
            or core_actual is None
            or expected_core is None
            or not close(core_actual, expected_core, tolerance=0.0001)
            or not close(core.get("required"), MIN_CORE_COVERAGE)
            or core["passed"] is not (core_actual >= MIN_CORE_COVERAGE)
        ):
            errors.append("core coverage prerequisite mismatch")
        technology = prerequisites["technology_patch4"]
        technology_applicable = technology.get("applicable") if isinstance(technology, Mapping) else None
        template_technology_score = _finite(template1_items.get("t1_17", {}).get("score"))
        expected_technology_status = (
            "missing_validated_patch4_assessment" if technology_applicable is True else "not_applicable"
        )
        if (
            not isinstance(technology, Mapping)
            or set(technology) != {"passed", "applicable", "score", "validation_status"}
            or not isinstance(technology_applicable, bool)
            or technology.get("score") is not None
            or technology.get("validation_status") != expected_technology_status
            or technology["passed"] is not (technology_applicable is False)
            or (
                template_technology_score is not None
                and template_technology_score >= 7.0
                and technology_applicable is not True
            )
        ):
            errors.append("technology prerequisite mismatch")
        financials = prerequisites["three_year_financials"]
        years = financials.get("consecutive_years") if isinstance(financials, Mapping) else None
        if (
            not isinstance(financials, Mapping)
            or set(financials) != {"passed", "consecutive_years"}
            or isinstance(years, bool)
            or not isinstance(years, int)
            or years < 0
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
        expected_history = bool(
            isinstance(shareholder_input, Mapping)
            and shareholder_input.get("available") is True
            and template5_items.get("t5_v1", {}).get("complete") is True
        )
        history_as_of = history.get("as_of") if isinstance(history, Mapping) else None
        history_date = _parse_evidence_date(history_as_of)
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
                            score_values["template1"]
                            - float(template1_items["t1_18"]["points"])
                            - float(template1_items["t1_19"]["points"])
                            + 10.0,
                        ),
                        2,
                    ),
                    "template5": round(
                        min(
                            100.0,
                            score_values["template5"]
                            - float(template5_items["t5_v1"]["points"])
                            - float(template5_items["t5_v3"]["points"])
                            + 18.0,
                        ),
                        2,
                    ),
                    "patch5": round(
                        min(
                            100.0,
                            score_values["patch5"]
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
    pre_history_passed = len(prerequisite_passes) == len(_PREREQUISITE_KEYS) and all(
        value
        for key, value in prerequisite_passes.items()
        if key
        not in {
            "three_external_reports",
            "external_report_content_verification",
            "ten_year_return_and_five_year_valuation",
        }
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
        and not expected_decisive_failure
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
    "research_metadata_precheck",
    "validate_quality_equity_ledger",
]
