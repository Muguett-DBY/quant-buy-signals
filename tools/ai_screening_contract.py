"""Local AI-screening contract helpers.

This module deliberately keeps the deterministic seven-type result separate
from an optional AI review overlay.  It can be used by a local Reasonix batch
without putting model credentials in the Cloudflare worker.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

REVIEW_SCHEMA_VERSION = 2
VALUATION_SNAPSHOT_CONTRACT_VERSION = 1
PLACEHOLDER_REVIEW_MODEL = "pending-local-opencode-go"
LOCAL_REVIEW_MODEL = "codex-local-review-v1"
CODEX_LUNA_REVIEW_MODEL = "codex-luna-max"
LOCAL_REVIEW_MODELS = frozenset({LOCAL_REVIEW_MODEL, CODEX_LUNA_REVIEW_MODEL})
LOCAL_OPENCODE_MODELS = frozenset(
    {
        "opencode-go/ox-alpha-free",
        "opencode-go/muse-spark-1.2-contributor",
        "opencode/muse-spark-1.2-contributor-free",
        "opencode-go/deepseek-v4-flash",
        "opencode-go-anthropic/deepseek-v4-flash",
    }
)
NATIVE_WEB_REVIEW_MODE = "opencode_native_web_search_review"
NATIVE_COMPANY_RESEARCH_REVIEW_MODE = "opencode_native_company_research_review"
NATIVE_WEB_REVIEW_MODEL = "opencode-go/muse-spark-1.2-contributor"
NATIVE_WEB_RETRIEVAL_MODEL = "opencode-go-muse/muse-spark-1.2-contributor"
NATIVE_RETRIEVAL_BACKEND = "reasonix-native-server-web-search"
OPENCODE_NATIVE_RETRIEVAL_BACKEND = "opencode-native-client-websearch"
NATIVE_COMPANY_RESEARCH_PROFILES = frozenset(
    {
        (
            "opencode-go/muse-spark-1.2-contributor",
            "xhigh",
            NATIVE_RETRIEVAL_BACKEND,
            "opencode-go-muse/muse-spark-1.2-contributor",
            "xhigh",
        ),
        (
            "opencode-go/deepseek-v4-flash",
            "max",
            NATIVE_RETRIEVAL_BACKEND,
            "opencode-go-deepseek-responses/deepseek-v4-flash",
            "max",
        ),
        (
            "opencode-go/deepseek-v4-flash",
            "max",
            NATIVE_RETRIEVAL_BACKEND,
            "opencode-go-anthropic/deepseek-v4-flash",
            "max",
        ),
        (
            "opencode-go/ox-alpha-free",
            "max",
            OPENCODE_NATIVE_RETRIEVAL_BACKEND,
            "opencode-go/ox-alpha-free",
            "max",
        ),
        (
            "opencode/muse-spark-1.2-contributor-free",
            "xhigh",
            OPENCODE_NATIVE_RETRIEVAL_BACKEND,
            "opencode/muse-spark-1.2-contributor-free",
            "xhigh",
        ),
        (
            "opencode-go/deepseek-v4-flash",
            "max",
            OPENCODE_NATIVE_RETRIEVAL_BACKEND,
            "opencode-go/deepseek-v4-flash",
            "max",
        ),
    }
)
PARTIAL_SEARCH_REVIEW_MODES = frozenset({"local_codex_review", "opencode_mixed_review"})
REVIEW_VERDICTS = frozenset({"confirmed", "caution", "misclassified", "missed_candidate", "needs_review"})
REVIEW_ACTIONS = frozenset({"keep", "demote", "manual_review"})
AI_ACTIONS = frozenset({"priority_buy", "watchlist", "avoid", "insufficient_evidence"})
FINAL_RECOMMENDATIONS = frozenset({"recommend_buy", "do_not_recommend_buy"})
AI_CONFIDENCE = frozenset({"high", "medium", "low"})
FRESHNESS_STATUSES = frozenset({"current_or_recent", "historical", "undated"})
TYPE_KEYS = tuple(f"type{i}" for i in range(1, 8))
ECONOMIC_CATEGORIES = frozenset(
    {
        "deep_value",
        "turnaround",
        "compounder",
        "cyclical",
        "growth",
        "venture",
        "quality_equity",
        "other",
    }
)
SCORE_COMPONENT_FIELDS = (
    "risk_adjusted_expected_return",
    "evidence_confidence",
)
ECONOMIC_PROFILE_FIELDS = (
    "business_model",
    "moat",
    "cycle",
    "fcf_outlook",
    "governance",
)
BUSINESS_MODEL_SOURCE_QUALITIES = frozenset({"current_primary", "stale_primary", "secondary_only", "not_found"})
BUSINESS_MODEL_UNCERTAINTY_RE = re.compile(
    r"未.{0,24}一手|无一手|只有二手|仅有二手|仅由.{0,16}二手|二手来源|二手研报|"
    r"尚待一手|待一手|无法核验|尚未核验|未.{0,8}核验|资料过时|业务口径待核验"
)
VALUATION_FIELDS = (
    "method",
    "as_of",
    "current_price",
    "pe",
    "pb",
    "market_cap",
    "scenarios",
    "margin_of_safety",
    "safety_margin_band",
    "basis",
)
VALUATION_EVIDENCE_FIELDS = (
    "evidence_ids",
    "normalization_anchor",
    "multiple_basis",
)
VALUATION_SCENARIOS = ("bear", "base", "bull")
VALUATION_SCENARIO_FIELDS = (
    "value_per_share",
    "upside_pct",
    "normalized_fcf_per_share",
    "discount_rate_pct",
    "terminal_growth_rate_pct",
    "equity_adjustment_per_share",
    "normalized_eps",
    "target_pe",
    "book_value_per_share",
    "target_pb",
)
GORDON_SCENARIO_FIELDS = (
    "normalized_fcf_per_share",
    "discount_rate_pct",
    "terminal_growth_rate_pct",
    "equity_adjustment_per_share",
)
RELIABLE_VALUATION_METHODS = frozenset({"gordon_fcf_per_share", "normalized_earnings_multiple", "book_value_multiple"})
LEGACY_VALUATION_METHODS = frozenset({"scenario_multiple", "multiple"})
UNAVAILABLE_VALUATION_METHODS = frozenset(
    {
        "not_reliably_estimable",
        "not_reliably_estimated",
        "unavailable",
        "none",
    }
)
VALUATION_SNAPSHOT_METRIC_FIELDS = (
    "current_price",
    "pe",
    "pb",
    "market_cap",
)


def _canonical_snapshot_number(value: Any) -> str | None:
    """Return one cross-runtime decimal token for valuation snapshot hashing."""

    number = _as_float(value)
    if number is None:
        return None
    text = f"{number:.8f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def valuation_snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    """Hash the immutable snapshot envelope, excluding its own digest."""

    canonical = {
        "contract_version": snapshot.get("contract_version"),
        "current_price": _canonical_snapshot_number(snapshot.get("current_price")),
        "market_as_of": str(snapshot.get("market_as_of") or ""),
        "market_cap": _canonical_snapshot_number(snapshot.get("market_cap")),
        "pb": _canonical_snapshot_number(snapshot.get("pb")),
        "pe": _canonical_snapshot_number(snapshot.get("pe")),
        "security_code": str(snapshot.get("security_code") or ""),
        "snapshot_generation": str(snapshot.get("snapshot_generation") or ""),
    }
    raw = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make_valuation_snapshot(
    *,
    security_code: str,
    snapshot_generation: str,
    market_as_of: str,
    current_price: Any,
    pe: Any,
    pb: Any,
    market_cap: Any,
) -> dict[str, Any]:
    """Seal the frozen research valuation inputs before ranking calibration."""

    snapshot: dict[str, Any] = {
        "contract_version": VALUATION_SNAPSHOT_CONTRACT_VERSION,
        "security_code": str(security_code or "").strip(),
        "snapshot_generation": str(snapshot_generation or "").strip(),
        "market_as_of": str(market_as_of or "").strip(),
        "current_price": _as_float(current_price),
        "pe": _as_float(pe),
        "pb": _as_float(pb),
        "market_cap": _as_float(market_cap),
    }
    snapshot["canonical_sha256"] = valuation_snapshot_sha256(snapshot)
    return snapshot


def valuation_snapshot_errors(
    review: Mapping[str, Any],
    *,
    expected_security_code: str | None = None,
    expected_snapshot_generation: str | None = None,
    expected_market_as_of: str | None = None,
) -> list[str]:
    """Validate the sealed snapshot and its exact binding to public valuation."""

    snapshot = review.get("valuation_snapshot")
    valuation = review.get("valuation")
    if not isinstance(snapshot, Mapping) or not isinstance(valuation, Mapping):
        return ["valuation_snapshot"]
    code = str(review.get("security_code") or "").strip()
    snapshot_code = str(snapshot.get("security_code") or "").strip()
    generation = str(snapshot.get("snapshot_generation") or "").strip()
    market_as_of = str(snapshot.get("market_as_of") or "").strip()
    errors: list[str] = []
    if snapshot.get("contract_version") != VALUATION_SNAPSHOT_CONTRACT_VERSION:
        errors.append("valuation_snapshot.contract_version")
    if (
        not snapshot_code
        or snapshot_code != code
        or expected_security_code is not None
        and snapshot_code != expected_security_code
    ):
        errors.append("valuation_snapshot.security_code")
    if not generation or expected_snapshot_generation is not None and generation != expected_snapshot_generation:
        errors.append("valuation_snapshot.snapshot_generation")
    if (
        _iso_date(market_as_of) is None
        or str(valuation.get("as_of") or "") != market_as_of
        or expected_market_as_of is not None
        and market_as_of != expected_market_as_of
    ):
        errors.append("valuation_snapshot.market_as_of")
    for field in VALUATION_SNAPSHOT_METRIC_FIELDS:
        snapshot_token = _canonical_snapshot_number(snapshot.get(field))
        valuation_token = _canonical_snapshot_number(valuation.get(field))
        if field == "current_price" and (snapshot_token is None or _as_float(snapshot.get(field)) <= 0):
            errors.append(f"valuation_snapshot.{field}")
        elif snapshot_token != valuation_token:
            errors.append(f"valuation_snapshot.{field}")
    declared_sha256 = str(snapshot.get("canonical_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", declared_sha256) or declared_sha256 != valuation_snapshot_sha256(snapshot):
        errors.append("valuation_snapshot.canonical_sha256")
    return list(dict.fromkeys(errors))


def native_company_research_profile_matches(review: Mapping[str, Any]) -> bool:
    """Return whether one review uses an exact audited native-search profile."""
    return (
        str(review.get("model") or ""),
        str(review.get("effort") or ""),
        str(review.get("retrieval_backend") or ""),
        str(review.get("retrieval_model") or ""),
        str(review.get("retrieval_effort") or ""),
    ) in NATIVE_COMPANY_RESEARCH_PROFILES and review.get("native_search_completed") is True


_ACTION_ALLOWED_VERDICTS = {
    "priority_buy": frozenset({"confirmed"}),
    "watchlist": frozenset({"confirmed", "caution", "missed_candidate", "needs_review"}),
    "avoid": frozenset({"confirmed", "caution", "misclassified"}),
    "insufficient_evidence": frozenset({"needs_review"}),
}
_ACTION_ALLOWED_REVIEW_ACTIONS = {
    "priority_buy": frozenset({"keep"}),
    "watchlist": frozenset({"keep", "demote", "manual_review"}),
    "avoid": frozenset({"demote", "manual_review"}),
    "insufficient_evidence": frozenset({"manual_review"}),
}
_SUMMARY_CLAUSE_RE = re.compile(r"[^。！？!?；;\n]+")
_CURRENT_BUY_RE = re.compile(
    r"(?:AI独立|当前|现在|现阶段|本轮|综合判断|结论为)?"
    r"(?:明确|强烈|积极|优先|仍然|依然|维持|直接)?"
    r"(?:建议|推荐)(?:立即|现在|当前|积极|优先|逢低|分批|逐步)?(?:买入|建仓|加仓|配置)"
    r"|(?:当前|现在|现阶段|本轮)?(?:可以|可|值得|适合|应当|应该)"
    r"(?:立即|现在|当前|逢低|分批|逐步)?(?:买入|建仓|加仓|配置)"
    r"|(?:当前|现在|现阶段)?(?:具备|存在)(?:买入|配置)(?:价值|机会)"
    r"|(?:当前|现在|现阶段)(?:属于|是|构成)(?:明确)?(?:买点|买入机会|建仓机会)"
    r"|(?:AI|本轮|最终|综合)?结论(?:为|是|：|:)?(?:建议)?(?:买入|建仓|配置)"
    r"|(?:建议|可以|可|适合)(?:开始|逐步|逢低|分批)?(?:布局|介入|低吸)"
    r"|(?:AI独立|当前|现在|现阶段|本轮|综合判断|结论为)?"
    r"(?:明确|强烈|积极|优先|仍然|依然|直接)?"
    r"(?:建议买|推荐买|值得买|可以买|可买|应该买|应买)(?!入|方|单)"
)
_CURRENT_NON_BUY_RE = re.compile(
    r"(?:当前)?结论(?:为|是|：|:)(?:观察|不建议(?:买|买入)?|暂不建议(?:买|买入)?)|"
    r"(?:当前|现在|现阶段|本轮|综合判断)?(?:明确|仍然|依然|暂时)?"
    r"(?:不建议|不推荐|不宜|不应|不适合)(?:立即|现在|当前|直接)?(?:买入|建仓|加仓|配置)"
    r"|(?:当前|现在|现阶段)?(?:尚不构成|不构成|并非|不是)(?:合适的)?(?:买点|买入机会|买入时点)"
    r"|(?:建议|应当|应该|宜)(?:继续)?(?:观望|等待|回避)"
    r"|(?:维持|列为|归入|仅作|应列入)(?:继续)?观察"
    r"|(?:暂不|暂缓|推迟|不宜|不应|不可|不适合)(?:立即|现在|当前|直接)?(?:买入|建仓|加仓|配置)"
    r"|(?:当前|现在|现阶段)?(?:暂不具备|尚不具备|不具备)(?:买入|配置)(?:价值|条件)"
    r"|(?:当前|现在|现阶段|本轮|目前)?(?:买入|建仓|配置)(?:逻辑|理由|条件)(?:尚未|未|还未|仍未)(?:成立|满足|确认)"
    r"|(?:暂时不|暂不|暂未|目前不|当前不)(?:考虑|参与|纳入)(?:买入|配置|投资|持仓|交易)?"
    r"|(?:当前|现在|现阶段|本轮|目前)?(?:不参与|不纳入|暂不纳入)(?:配置|投资|买入|持仓)"
    r"|(?:当前|现在|现阶段)?(?:没有|缺乏)(?:明确)?(?:买点|买入机会)"
    r"|(?:当前|现在|现阶段)?不值得(?:立即|现在|当前)?(?:买入|建仓|配置)"
    r"|(?:建议|应当|应该)(?:持币)?(?:继续)?观望"
    r"|(?:建议|应当|应该)(?:卖出|减仓|清仓)"
    r"|(?:建议|应当|应该)?(?:暂时|当前|现在)?(?:不要|别)"
    r"(?:立即|现在|当前|直接)?(?:买入|购买|购入|建仓|加仓|配置|买)(?!方|单)"
    r"|(?:建议|维持|列为|归入|暂列|只建议|仅建议)(?:继续)?(?:观察|观望)(?:为主|清单)?(?:$|[，,])"
    r"|(?:当前|现在|现阶段|本轮|目前)(?:仍|暂时|只|仅)?(?:建议)?(?:继续)?"
    r"(?:观察|观望|等待)(?:为主|清单)?(?:$|[，,])"
    r"|(?:当前|现在|现阶段|本轮|目前)?(?:继续)?持币观望"
)
_QUALIFIED_DECISION_PREFIX_RE = re.compile(
    r"(?:若|如果|待|一旦|除非|只有|前提(?:是|为)|条件(?:是|为)|"
    r"回落至?|跌至|低于|高于|改善后|确认后|触发后|满足后|兑现后|企稳后|完成后|之后|后再|再考虑)"
    r"[^，,。！？!?；;]{0,24}[，,]?$"
)
_NEGATED_BUY_PREFIX_RE = re.compile(
    r"(?:不|暂不|并不|并非|未|尚未|不能|不可|不构成|不足以|难以|避免|谨慎)"
    r"(?:据此|直接|立即|现在|当前)?$"
)
_ATTRIBUTED_BUY_PREFIX_RE = re.compile(
    r"(?:券商|机构|分析师|研报|媒体|第三方|(?:量化|确定性)?模型)(?:明确|强烈)?$"
    r"|(?:券商|机构|分析师|研报|媒体|第三方|(?:量化|确定性)?模型)(?:曾|此前)?"
    r"(?:给出|给予|给与|维持|上调至|评级为|判为|标注为|显示|认为|称)(?:的)?"
    r"[^，,。！？!?；;]{0,12}$"
)
_BUY_TERM_SUFFIX_RE = re.compile(r"^(?:价|价格|区间|线|阈值|条件|门槛|评级|点位)")
_READABLE_REASON_RE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
_REASON_PLACEHOLDERS = frozenset(
    {
        "无",
        "暂无",
        "未知",
        "待定",
        "待核验",
        "证据不足",
        "未提供",
        "暂无资料",
        "暂无信息",
        "暂无理由",
        "没有资料",
        "未提供理由",
        "ai已完成第一轮候选复核",
        "n/a",
        "na",
    }
)

# AI-facing investment reasons must describe the company, not repeat why the
# deterministic screener admitted it to the candidate pool.  Keep this list
# deliberately narrow: ordinary phrases such as "监管规则" are valid company
# risks, while type/status/score language belongs in the separate rule
# reference shown by the website.
_RULE_REASON_LEAK_RE = re.compile(
    r"\btype\s*[1-7]\b|类型\s*[1-7]|"
    r"第[一二三四五六七1-7](?:种|类)(?:买入)?(?:情况|类型)|"
    r"确定性(?:筛选|规则|评分|分数|状态)|"
    r"(?:筛选|买入|七类|模型)规则(?:分数|评分|状态|触发|达标|结果|候选)|"
    r"(?:候选|规则|筛选|类型|type).{0,8}(?:触发|达标)|"
    r"(?:触发|达标).{0,8}(?:候选|规则|筛选|类型|type)|候选池|入池|"
    r"\b(?:triggered|conditional|insufficient_evidence)\b",
    re.IGNORECASE,
)
_MINORITY_INTEREST_RE = re.compile(r"少数股东|少数权益|非全资|minority\s+interest", re.IGNORECASE)
_MATERIAL_MINORITY_INTEREST_RE = re.compile(
    r"(?:核心|主要|重要|主力|利润核心).{0,24}(?:子公司|业务).{0,48}(?:少数股东|少数权益|非全资)|"
    r"(?:少数股东|少数权益).{0,48}(?:核心|主要|重要|近半|重大|显著|大额|占比|占|归母口径|无法可靠|不能可靠|需打折)|"
    r"(?:归母口径|归属于母公司股东).{0,36}(?:现金流|自由现金流).{0,24}(?:无法|不能|需打折|不可靠)",
    re.IGNORECASE,
)
_MINORITY_INTEREST_NEGATION_RE = re.compile(
    r"(?:未发现|不存在|并无|没有|无).{0,12}(?:重大|显著|大额)?(?:少数股东|少数权益|非全资)",
    re.IGNORECASE,
)
_RESEARCH_PERIOD_RE = re.compile(
    r"(?:19|20)\d{2}(?:年|[-/.](?:0?[1-9]|1[0-2])(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?|"
    r"\s*(?:Q[1-4]|H[12]|一季(?:度)?|上半年|半年度|前三季(?:度)?|年度|年报|中报))"
    r"|交易日\s*(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}",
    re.IGNORECASE,
)
_RESEARCH_VALUE_UNIT_RE = re.compile(
    r"[-+]?\d+(?:\.\d+)?\s*(?:%|个百分点|bp(?:s)?|倍|元|万(?:元|股|台|吨|辆)?|"
    r"亿(?:元|股|台|吨|辆)?|股|台|吨|辆|家)",
    re.IGNORECASE,
)
_RESEARCH_DIMENSION_TERMS: dict[str, tuple[str, ...]] = {
    "valuation": ("pe", "pb", "ps", "市盈", "市净", "市销", "估值", "股价", "市值", "股息率", "折价"),
    "cashflow": ("现金流", "自由现金流", "经营现金", "资本开支", "fcf"),
    "earnings": ("营收", "收入", "净利", "利润", "毛利", "扣非", "roe", "roic", "eps"),
    "industry": ("市占率", "市场份额", "销量", "产量", "订单", "供需", "库存", "产能", "行业价格"),
    "governance": ("分红", "回购", "负债率", "应收", "商誉", "关联交易", "研发投入"),
}
_SUBSTANTIVE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?")


def candidate_identity_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    """Hash a canonical company/type candidate universe.

    A company-level AI packet may carry several deterministic candidate types.
    Expanding those types here keeps the original pair-level identity proof
    without forcing the model to emit several potentially contradictory
    opinions for one company.  The digest is canonical rather than rank/order
    dependent: a raw pair queue and its grouped company queue therefore prove
    the same admitted pairs with the same hash.
    """

    pairs: list[tuple[str, str]] = []
    for record in records:
        code = str(record.get("security_code") or record.get("code") or "").strip()
        type_keys: list[str] = []
        candidate_types = record.get("candidate_types")
        if isinstance(candidate_types, list):
            type_keys.extend(
                str(value.get("type_key") or value.get("type") or "").strip()
                for value in candidate_types
                if isinstance(value, Mapping)
            )
            declared_type_keys = record.get("type_keys")
            if isinstance(declared_type_keys, list):
                declared = list(dict.fromkeys(str(value).strip() for value in declared_type_keys))
                from_types = list(dict.fromkeys(type_keys))
                if set(declared) != set(from_types):
                    raise ValueError("candidate identity type_keys do not match candidate_types")
        elif isinstance(record.get("type_keys"), list):
            type_keys.extend(str(value).strip() for value in record["type_keys"])
        else:
            type_keys.append(str(record.get("type_key") or record.get("type") or "").strip())
        type_keys = list(dict.fromkeys(type_keys))
        if not code or not type_keys or any(type_key not in TYPE_KEYS for type_key in type_keys):
            raise ValueError("candidate identity is incomplete")
        type_keys.sort(key=TYPE_KEYS.index)
        for type_key in type_keys:
            pair = (code, type_key)
            if pair in pairs:
                raise ValueError(f"candidate identity contains duplicate company/type pair: {code}/{type_key}")
            pairs.append(pair)

    digest = hashlib.sha256()
    for code, type_key in sorted(pairs, key=lambda value: (value[0], TYPE_KEYS.index(value[1]))):
        digest.update(code.encode("utf-8"))
        digest.update(b"\0")
        digest.update(type_key.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _iter_types(company: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    raw = company.get("types") or company.get("type_results") or company.get("decisions")
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("type_key", str(key))
                yield item
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, Mapping):
                yield value


def _near_or_qualified(type_result: Mapping[str, Any]) -> bool:
    key = str(type_result.get("type_key") or type_result.get("type") or "")
    status = str(type_result.get("status") or "").lower()
    score = _as_float(type_result.get("score"))
    if score is None:
        score = _as_float(type_result.get("total_score"))
    decision = type_result.get("decision") if isinstance(type_result.get("decision"), Mapping) else {}
    lower = _as_float(type_result.get("lower"))
    if lower is None:
        lower = _as_float(type_result.get("score_lower_bound"))
    if lower is None:
        lower = _as_float(decision.get("score_lower_bound"))
    upper = _as_float(type_result.get("upper"))
    if upper is None:
        upper = _as_float(type_result.get("score_upper_bound"))
    if upper is None:
        upper = _as_float(decision.get("score_upper_bound"))
    triggered = bool(type_result.get("triggered")) or status in {"triggered", "qualified"}
    if triggered:
        return True
    if key == "type7" and score is not None and score >= 7:
        return True
    if status in {"conditional", "observe", "pending", "insufficient_evidence"}:
        if upper is not None and upper >= 7:
            return True
        if score is not None and score >= 6.5:
            return True
    return lower is not None and upper is not None and lower < 7 <= upper


def select_candidates(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Create stable company/type review pairs from a published snapshot."""
    raw = snapshot.get("companies") or snapshot.get("catalogue") or snapshot.get("rows") or []
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    selected: list[dict[str, Any]] = []
    for company in raw:
        if not isinstance(company, Mapping):
            continue
        code = str(company.get("code") or company.get("security_code") or "").strip()
        if not code:
            continue
        for result in _iter_types(company):
            if not _near_or_qualified(result):
                continue
            type_key = str(result.get("type_key") or result.get("type") or "").strip()
            if type_key not in TYPE_KEYS:
                continue
            selected.append(
                {
                    "schema_version": REVIEW_SCHEMA_VERSION,
                    "generation": snapshot.get("generation") or snapshot.get("generation_id"),
                    "market_as_of": snapshot.get("market_as_of"),
                    "security_code": code,
                    "name": company.get("name") or company.get("security_name"),
                    "type_key": type_key,
                    "deterministic": dict(result),
                    "company": dict(company),
                }
            )

    def priority(item: Mapping[str, Any]) -> tuple[int, float, str, str]:
        result = item["deterministic"]
        status = str(result.get("status") or "")
        decision = result.get("decision") if isinstance(result.get("decision"), Mapping) else {}
        score = _as_float(result.get("score"))
        upper = _as_float(result.get("score_upper_bound"))
        if upper is None:
            upper = _as_float(decision.get("score_upper_bound"))
        ranked = score if score is not None else (upper if upper is not None else -1.0)
        bucket = {
            "triggered": 0,
            "qualified": 0,
            "conditional": 1,
            "observe": 2,
            "pending": 3,
            "insufficient_evidence": 3,
        }.get(status, 4)
        return (bucket, -ranked, str(item["security_code"]), str(item["type_key"]))

    selected.sort(key=priority)
    return selected


def group_candidates_by_company(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse candidate type pairs into one stable AI packet per company.

    The first pair keeps the existing deterministic priority order and becomes
    the compatibility ``type_key``/``deterministic`` view.  Every admitted type
    remains available in ``candidate_types`` and in the pair identity hash, so
    publication can prove coverage without choosing the most bullish of several
    model opinions after the fact.
    """

    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in candidates:
        code = str(candidate.get("security_code") or candidate.get("code") or "").strip()
        type_key = str(candidate.get("type_key") or candidate.get("type") or "").strip()
        deterministic = candidate.get("deterministic")
        if not code or type_key not in TYPE_KEYS or not isinstance(deterministic, Mapping):
            raise ValueError("candidate company/type identity is incomplete")
        if code not in grouped:
            grouped[code] = {
                **dict(candidate),
                "security_code": code,
                "type_key": type_key,
                "deterministic": dict(deterministic),
                "candidate_types": [],
            }
            order.append(code)
        company = grouped[code]
        entries = company["candidate_types"]
        if any(str(entry.get("type_key") or "") == type_key for entry in entries):
            raise ValueError(f"duplicate candidate company/type pair: {code}/{type_key}")
        entries.append({"type_key": type_key, "deterministic": dict(deterministic)})

    output: list[dict[str, Any]] = []
    for code in order:
        candidate = grouped[code]
        entries = sorted(candidate["candidate_types"], key=lambda value: TYPE_KEYS.index(value["type_key"]))
        type_keys = [str(entry["type_key"]) for entry in entries]
        candidate["candidate_types"] = entries
        candidate["type_keys"] = type_keys
        candidate["type_pair_count"] = len(type_keys)
        output.append(candidate)
    return output


def _qualified_or_negated_buy(clause: str, start: int) -> bool:
    """Return whether a buy phrase describes a condition or a negation.

    A non-buy conclusion may still explain when a future purchase would become
    reasonable (``若价格回落，可分批买入``), or quote the absence of a buy
    case (``尚不足以建议买入``).  Those are useful risk boundaries rather
    than contradictory current recommendations.
    """

    prefix = clause[max(0, start - 32) : start]
    return bool(
        _QUALIFIED_DECISION_PREFIX_RE.search(prefix)
        or _NEGATED_BUY_PREFIX_RE.search(prefix)
        or _ATTRIBUTED_BUY_PREFIX_RE.search(prefix)
    )


def _has_unqualified_decision(text: str, pattern: re.Pattern[str], *, buy_decision: bool) -> bool:
    compact = re.sub(r"\s+", "", text)
    for clause_match in _SUMMARY_CLAUSE_RE.finditer(compact):
        clause = clause_match.group(0)
        for decision_match in pattern.finditer(clause):
            if buy_decision and _BUY_TERM_SUFFIX_RE.search(clause[decision_match.end() :]):
                continue
            if buy_decision and _qualified_or_negated_buy(clause, decision_match.start()):
                continue
            if not buy_decision and _QUALIFIED_DECISION_PREFIX_RE.search(
                clause[max(0, decision_match.start() - 32) : decision_match.start()]
            ):
                continue
            return True
    return False


def _decision_text_conflicts(action: str, text: str) -> bool:
    if action == "priority_buy":
        return _has_unqualified_decision(text, _CURRENT_NON_BUY_RE, buy_decision=False)
    if action in {"watchlist", "avoid", "insufficient_evidence"}:
        return _has_unqualified_decision(text, _CURRENT_BUY_RE, buy_decision=True)
    return False


def normalise_decision_text(text: str) -> str:
    """Collapse accidental repeated negation tokens in model-written prose.

    Some local model responses repeat the Chinese negation character while
    preserving the intended conclusion (for example ``暂不不不建议买入``).
    This is a presentation defect, not a new decision.  Keeping the cleanup
    here gives both the batch and calibration paths the same final-text rule.
    """

    return re.sub(r"不{2,}", "不", str(text or ""))


def decision_text_conflicts(action: str, text: str) -> bool:
    """Expose the contract's current-conclusion check to output cleaners."""

    return _decision_text_conflicts(str(action or ""), str(text or ""))


def _readable_reason(value: Any, *, minimum: int) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    normalised = re.sub(r"[\s，,。.!！?？；;：:、]+", "", text).casefold()
    if normalised in _REASON_PLACEHOLDERS:
        return False
    return len(_READABLE_REASON_RE.findall(text)) >= minimum


def has_material_minority_interest_risk(texts: Iterable[Any]) -> bool:
    """Return whether evidence makes consolidated FCF unsafe for parent holders."""

    for value in texts:
        for sentence in re.split(r"[。；;\n]", str(value or "")):
            if not _MINORITY_INTEREST_RE.search(sentence) or _MINORITY_INTEREST_NEGATION_RE.search(sentence):
                continue
            if _MATERIAL_MINORITY_INTEREST_RE.search(sentence):
                return True
            if re.search(r"(?:[2-4]\d(?:\.\d+)?)\s*%", sentence) and re.search(
                r"子公司|股权|利润|损益|现金流", sentence
            ):
                return True
    return False


_CURRENT_PERIOD_YEAR_RE = re.compile(
    r"(?:最新(?:一期|实际|可得|披露)?|当前(?:一期|实际|可得|报告期|财报|数据)|当期|最近一期)"
    r"[^。；;\n]{0,24}((?:19|20)\d{2})|"
    r"((?:19|20)\d{2})年?[^。；;\n]{0,18}(?:是|为)?"
    r"(?:最新(?:一期|实际)?|当前(?:一期|实际|报告期|财报|数据)|当期|最近一期)"
)


def stale_current_period_fields(review: Mapping[str, Any]) -> list[str]:
    """Find user prose that labels an older period as current/latest."""

    reference_years = [
        int(value) for value in review.get("freshness_years", []) if isinstance(value, int) and 1900 <= value <= 2100
    ]
    valuation = review.get("valuation")
    # A 2026 research/price date does not make the latest filed annual period
    # 2026.  Prefer the review's actual evidence years; use envelope dates only
    # for older artifacts that do not carry freshness_years at all.
    if not reference_years:
        for value in (
            review.get("research_as_of"),
            valuation.get("as_of") if isinstance(valuation, Mapping) else None,
        ):
            match = re.match(r"^((?:19|20)\d{2})-\d{2}-\d{2}$", str(value or ""))
            if match:
                reference_years.append(int(match.group(1)))
    if not reference_years:
        return []
    latest_year = max(reference_years)
    fields: list[tuple[str, Any]] = [("summary", review.get("summary"))]
    fields.extend((f"key_strengths[{index}]", value) for index, value in enumerate(review.get("key_strengths", [])))
    fields.extend((f"risk_flags[{index}]", value) for index, value in enumerate(review.get("risk_flags", [])))
    profile = review.get("economic_profile")
    if isinstance(profile, Mapping):
        fields.extend((f"economic_profile.{field}", profile.get(field)) for field in ECONOMIC_PROFILE_FIELDS)
    if isinstance(valuation, Mapping):
        fields.append(("valuation.basis", valuation.get("basis")))
    stale: list[str] = []
    for field, value in fields:
        for match in _CURRENT_PERIOD_YEAR_RE.finditer(str(value or "")):
            claimed_year = int(match.group(1) or match.group(2))
            if claimed_year < latest_year:
                stale.append(field)
                break
    return stale


def _research_dimensions(value: Any) -> set[str]:
    text = str(value or "").casefold()
    return {
        dimension
        for dimension, terms in _RESEARCH_DIMENSION_TERMS.items()
        if any(term.casefold() in text for term in terms)
    }


def _is_company_research_fact(value: Any, *, reject_rule_language: bool = True) -> bool:
    """Return whether a sentence contains a dated, dimensional numeric fact."""

    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(
        text
        and (not reject_rule_language or not _RULE_REASON_LEAK_RE.search(text))
        and _RESEARCH_PERIOD_RE.search(text)
        and _RESEARCH_VALUE_UNIT_RE.search(text)
        and _research_dimensions(text)
    )


def _claim_source_values(claim: Mapping[str, Any]) -> list[str]:
    """Collect singular and multi-source references without changing order."""
    values: list[str] = []
    singular = str(claim.get("source_ref") or "").strip()
    if singular:
        values.append(singular)
    raw = claim.get("source_refs")
    if isinstance(raw, list):
        values.extend(str(value).strip() for value in raw if str(value).strip())
    context = str(claim.get("source_context") or "").strip()
    if context:
        values.append(context)
    return list(dict.fromkeys(values))


def _claim_has_public_url(claim: Mapping[str, Any]) -> bool:
    return any(value.lower().startswith(("http://", "https://")) for value in _claim_source_values(claim))


def _substantive_numbers(value: Any) -> set[str]:
    numbers: set[str] = set()
    for match in _SUBSTANTIVE_NUMBER_RE.finditer(str(value or "")):
        raw = match.group(0).lstrip("+")
        unsigned = raw.lstrip("-")
        try:
            number = float(unsigned)
        except ValueError:
            continue
        # A report year or six-digit security code identifies the source but
        # does not prove any operating, valuation, or industry metric.
        if unsigned.isdigit() and (1900 <= int(number) <= 2100 or len(unsigned) == 6):
            continue
        numbers.add(raw)
    return numbers


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _company_research_envelope_errors(review: Mapping[str, Any], *, reject_rule_language: bool) -> list[str]:
    """Validate the structured company thesis published beside prose reasons.

    The fields are optional for legacy/local reviews, but once one is present
    the whole envelope must be present.  This prevents a weekend research date
    from being confused with the market-close valuation date and keeps rule
    admission language out of the independent company thesis.
    """

    fields = ("research_as_of", "economic_profile", "valuation")
    present = [field in review for field in fields]
    if not any(present):
        return []
    if not all(present):
        return ["company_research_envelope"]

    errors: list[str] = []
    research_as_of = _iso_date(review.get("research_as_of"))
    if research_as_of is None:
        errors.append("research_as_of")

    profile = review.get("economic_profile")
    if not isinstance(profile, Mapping):
        errors.append("economic_profile")
    else:
        for field in ECONOMIC_PROFILE_FIELDS:
            value = profile.get(field)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > 600
                or reject_rule_language
                and _RULE_REASON_LEAK_RE.search(value)
            ):
                errors.append("economic_profile")
                break
        source_quality = str(profile.get("business_model_source_quality") or "").strip()
        source_ids = profile.get("business_model_source_ids")
        uncertainty = profile.get("business_model_uncertainty")
        if (
            source_quality not in BUSINESS_MODEL_SOURCE_QUALITIES
            or not isinstance(uncertainty, str)
            or len(uncertainty.strip()) > 600
            or reject_rule_language
            and _RULE_REASON_LEAK_RE.search(uncertainty)
            or (
                source_quality != "current_primary"
                and not BUSINESS_MODEL_UNCERTAINTY_RE.search(f"{profile.get('business_model', '')} {uncertainty}")
            )
        ):
            errors.append("economic_profile_sources")
        if source_quality == "not_found":
            if source_ids not in (None, []) or str(review.get("ai_action") or "") == "priority_buy":
                errors.append("economic_profile_not_found_sources")
        elif (
            not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) > 16
            or any(not isinstance(value, str) or not value.strip() for value in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            errors.append("invalid business-model source IDs")

    valuation = review.get("valuation")
    valuation_as_of: date | None = None
    if not isinstance(valuation, Mapping):
        errors.append("valuation")
    else:
        valuation_as_of = _iso_date(valuation.get("as_of"))
        current_price = _as_float(valuation.get("current_price"))
        method = str(valuation.get("method") or "").strip().casefold()
        basis = valuation.get("basis")
        numeric_metrics = [
            _as_float(valuation.get(field))
            for field in ("pe", "pb", "market_cap", "margin_of_safety")
            if valuation.get(field) not in (None, "")
        ]
        invalid = (
            valuation_as_of is None
            or current_price is None
            or current_price <= 0
            or any(value is None for value in numeric_metrics)
            or not method
            or len(method) > 80
            or reject_rule_language
            and _RULE_REASON_LEAK_RE.search(method)
            or not isinstance(basis, str)
            or not basis.strip()
            or len(basis.strip()) > 1000
            or reject_rule_language
            and _RULE_REASON_LEAK_RE.search(basis)
        )
        scenarios = valuation.get("scenarios")
        margin = _as_float(valuation.get("margin_of_safety"))
        safety_band = valuation.get("safety_margin_band")
        if method in UNAVAILABLE_VALUATION_METHODS:
            invalid = invalid or scenarios not in (None, {}) or margin is not None or safety_band is not None
            if review.get("ai_action") == "priority_buy":
                invalid = True
        elif (
            method in RELIABLE_VALUATION_METHODS | LEGACY_VALUATION_METHODS
            and current_price is not None
            and current_price > 0
            and isinstance(scenarios, Mapping)
        ):
            if reject_rule_language and method in LEGACY_VALUATION_METHODS:
                invalid = True
            values: dict[str, float] = {}
            anchors: dict[str, float] = {}
            gordon_growths: dict[str, float] = {}
            gordon_discounts: dict[str, float] = {}
            multiple = valuation.get("multiple_basis")
            multiple_value = _as_float(multiple.get("value")) if isinstance(multiple, Mapping) else None
            normalization_anchor = valuation.get("normalization_anchor")
            anchor_per_share = (
                _as_float(normalization_anchor.get("per_share")) if isinstance(normalization_anchor, Mapping) else None
            )
            for scenario, multiple_factor in zip(VALUATION_SCENARIOS, (0.7, 1.0, 1.3), strict=True):
                item = scenarios.get(scenario)
                if not isinstance(item, Mapping):
                    invalid = True
                    continue
                value = _as_float(item.get("value_per_share"))
                upside = _as_float(item.get("upside_pct"))
                optional_numbers = [
                    _as_float(item.get(field))
                    for field in VALUATION_SCENARIO_FIELDS[2:]
                    if item.get(field) not in (None, "")
                ]
                if value is None or value <= 0 or upside is None or any(number is None for number in optional_numbers):
                    invalid = True
                    continue
                expected_upside = (value / current_price - 1.0) * 100.0
                if abs(upside - expected_upside) > max(0.35, abs(expected_upside) * 0.01):
                    invalid = True
                if method == "gordon_fcf_per_share":
                    inputs = [_as_float(item.get(field)) for field in GORDON_SCENARIO_FIELDS]
                    if any(number is None for number in inputs):
                        invalid = True
                    else:
                        cash_flow, discount, growth, adjustment = inputs
                        assert cash_flow is not None
                        assert discount is not None
                        assert growth is not None
                        assert adjustment is not None
                        invalid_gordon_inputs = (
                            cash_flow <= 0
                            or discount < 1
                            or growth <= -100
                            or discount <= growth
                            or reject_rule_language
                            and (
                                discount < 6
                                or discount > 20
                                or growth < -3
                                or growth > 5
                                or discount - growth < 3
                                or adjustment != 0.0
                            )
                        )
                        if invalid_gordon_inputs:
                            invalid = True
                        else:
                            expected_value = (
                                cash_flow * (1.0 + growth / 100.0) / ((discount - growth) / 100.0) + adjustment
                            )
                            if expected_value <= 0 or abs(value - expected_value) > max(
                                0.05, abs(expected_value) * 0.01
                            ):
                                invalid = True
                        anchors[scenario] = cash_flow
                        gordon_growths[scenario] = growth
                        gordon_discounts[scenario] = discount
                elif method == "normalized_earnings_multiple":
                    normalized_eps = _as_float(item.get("normalized_eps"))
                    target_pe = _as_float(item.get("target_pe"))
                    adjustment = _as_float(item.get("equity_adjustment_per_share"))
                    if (
                        normalized_eps is None
                        or normalized_eps <= 0
                        or target_pe is None
                        or target_pe <= 0
                        or adjustment != 0.0
                        or multiple_value is None
                        or multiple_value <= 0
                        or abs(target_pe - multiple_value * multiple_factor) > 0.01
                    ):
                        invalid = True
                    else:
                        expected_value = normalized_eps * target_pe
                        if abs(value - expected_value) > max(0.05, abs(expected_value) * 0.01):
                            invalid = True
                        anchors[scenario] = normalized_eps
                elif method == "book_value_multiple":
                    book_value = _as_float(item.get("book_value_per_share"))
                    target_pb = _as_float(item.get("target_pb"))
                    if (
                        book_value is None
                        or book_value <= 0
                        or target_pb is None
                        or target_pb <= 0
                        or multiple_value is None
                        or multiple_value <= 0
                        or abs(target_pb - multiple_value * multiple_factor) > 0.01
                        or anchor_per_share is None
                        or abs(book_value - anchor_per_share) > max(0.005, abs(anchor_per_share) * 0.01)
                    ):
                        invalid = True
                    else:
                        expected_value = book_value * target_pb
                        if abs(value - expected_value) > max(0.05, abs(expected_value) * 0.01):
                            invalid = True
                        anchors[scenario] = target_pb
                values[scenario] = value
            if all(scenario in values for scenario in VALUATION_SCENARIOS):
                if not values["bear"] <= values["base"] <= values["bull"]:
                    invalid = True
                if all(scenario in anchors for scenario in VALUATION_SCENARIOS) and not (
                    anchors["bear"] <= anchors["base"] + 0.005 and anchors["base"] <= anchors["bull"] + 0.005
                ):
                    invalid = True
                if (
                    reject_rule_language
                    and method == "gordon_fcf_per_share"
                    and (
                        not all(scenario in gordon_growths for scenario in VALUATION_SCENARIOS)
                        or not all(scenario in gordon_discounts for scenario in VALUATION_SCENARIOS)
                        or not (gordon_growths["bear"] <= gordon_growths["base"] <= gordon_growths["bull"])
                        or not (gordon_discounts["bear"] >= gordon_discounts["base"] >= gordon_discounts["bull"])
                    )
                ):
                    invalid = True
                expected_margin = (values["bear"] - current_price) / values["bear"] * 100.0
                if margin is None or abs(margin - expected_margin) > max(0.35, abs(expected_margin) * 0.01):
                    invalid = True
                expected_band = (
                    "deep"
                    if expected_margin >= 20
                    else "adequate"
                    if expected_margin >= 8
                    else "thin"
                    if expected_margin > 0
                    else "negative"
                )
                if method in RELIABLE_VALUATION_METHODS and safety_band != expected_band:
                    invalid = True
            if method == "gordon_fcf_per_share":
                research_texts = [
                    review.get("summary"),
                    *(review.get("key_strengths") or []),
                    *(review.get("risk_flags") or []),
                ]
                if isinstance(profile, Mapping):
                    research_texts.extend(profile.get(field) for field in ECONOMIC_PROFILE_FIELDS)
                research_texts.extend(
                    item.get("statement") for item in review.get("claims", []) if isinstance(item, Mapping)
                )
                research_texts.extend(
                    item.get("finding") for item in review.get("search_findings", []) if isinstance(item, Mapping)
                )
                if has_material_minority_interest_risk(research_texts):
                    invalid = True
        else:
            invalid = True
        if invalid:
            errors.append("valuation")
    if research_as_of is not None and valuation_as_of is not None and research_as_of < valuation_as_of:
        errors.append("research_before_market_close")
    return errors


def _priority_research_evidence_errors(review: Mapping[str, Any], *, reject_rule_language: bool) -> list[str]:
    quantitative = review.get("quantitative_facts")
    facts = quantitative if isinstance(quantitative, list) else []
    valid_facts = [
        str(value) for value in facts if _is_company_research_fact(value, reject_rule_language=reject_rule_language)
    ]
    fact_dimensions = set().union(*(_research_dimensions(value) for value in valid_facts)) if valid_facts else set()

    claims = review.get("claims")
    claim_rows = claims if isinstance(claims, list) else []
    supported_claims = [
        claim
        for claim in claim_rows
        if isinstance(claim, Mapping)
        and claim.get("support") == "supports"
        and _claim_source_values(claim)
        and _is_company_research_fact(claim.get("statement"), reject_rule_language=reject_rule_language)
    ]
    claim_dimensions = (
        set().union(*(_research_dimensions(claim.get("statement")) for claim in supported_claims))
        if supported_claims
        else set()
    )
    linked_fact_count = 0
    for fact in valid_facts:
        fact_numbers = _substantive_numbers(fact)
        fact_dimensions_for_row = _research_dimensions(fact)
        if any(
            fact_numbers & _substantive_numbers(claim.get("statement"))
            and fact_dimensions_for_row & _research_dimensions(claim.get("statement"))
            for claim in supported_claims
        ):
            linked_fact_count += 1

    errors: list[str] = []
    if len(valid_facts) < 2 or len(fact_dimensions) < 2:
        errors.append("priority_company_research_facts")
    if len(supported_claims) < 2 or len(claim_dimensions) < 2 or linked_fact_count < 2:
        errors.append("priority_source_linked_research")
    risks = review.get("risk_flags")
    if not isinstance(risks, list) or not any(_readable_reason(value, minimum=2) for value in risks):
        errors.append("priority_risk_required")
    return errors


def _company_research_public_errors(review: Mapping[str, Any], *, require_calibration: bool) -> list[str]:
    """Validate the company-research fields that must survive publication.

    The legacy web-search and local review formats intentionally remain
    smaller.  Native company research is different: its score, reasons and
    valuation are all bound to a finite fact/finding graph, so dropping that
    graph during calibration or publication would make the public opinion
    impossible to audit.
    """

    errors: list[str] = []
    category = str(review.get("economic_category") or "").strip().casefold()
    if category not in ECONOMIC_CATEGORIES:
        errors.append("economic_category")

    components = review.get("score_components")
    if not isinstance(components, Mapping):
        errors.append("score_components")
        components = {}
    component_values: dict[str, float] = {}
    for field in SCORE_COMPONENT_FIELDS:
        value = _as_float(components.get(field))
        if value is None or value < 0 or value > 100:
            errors.append(f"score_components.{field}")
        else:
            component_values[field] = value
    adjustments = review.get("calibration_adjustments")
    if require_calibration:
        if not isinstance(adjustments, Mapping):
            errors.append("calibration_adjustments")
            adjustments = {}
        required_adjustment_fields = (
            "raw_score",
            "source_penalty",
            "freshness_penalty",
            "pre_band_score",
            "action_band_min",
            "action_band_max",
            "final_score",
            "source_quality",
            "freshness_status",
            "band_clamped",
        )
        for field in required_adjustment_fields:
            if field not in adjustments:
                errors.append(f"calibration_adjustments.{field}")
        for field in (
            "raw_score",
            "source_penalty",
            "freshness_penalty",
            "pre_band_score",
            "action_band_min",
            "final_score",
        ):
            value = _as_float(adjustments.get(field))
            if value is None:
                errors.append(f"calibration_adjustments.{field}")
        band_max = adjustments.get("action_band_max")
        if band_max is not None and _as_float(band_max) is None:
            errors.append("calibration_adjustments.action_band_max")
        if not isinstance(adjustments.get("source_quality"), str) or not str(adjustments.get("source_quality")).strip():
            errors.append("calibration_adjustments.source_quality")
        if str(adjustments.get("freshness_status") or "") not in FRESHNESS_STATUSES:
            errors.append("calibration_adjustments.freshness_status")
        if not isinstance(adjustments.get("band_clamped"), bool):
            errors.append("calibration_adjustments.band_clamped")
        raw_score = _as_float(adjustments.get("raw_score"))
        source_quality = str(adjustments.get("source_quality") or "")
        expected_source_penalty = {
            "verified_https": 0.0,
            "source_found": 2.0,
            "searched_no_source": 5.0,
            "not_searched": 8.0,
        }.get(source_quality)
        declared_source_penalty = _as_float(adjustments.get("source_penalty"))
        if (
            expected_source_penalty is None
            or declared_source_penalty is None
            or abs(declared_source_penalty - expected_source_penalty) > 0.01
        ):
            errors.append("calibration_adjustments.source_penalty")
        freshness_status = str(adjustments.get("freshness_status") or "")
        expected_freshness_penalty = {
            "current_or_recent": 0.0,
            "historical": 8.0,
            "undated": 5.0,
        }.get(freshness_status)
        declared_freshness_penalty = _as_float(adjustments.get("freshness_penalty"))
        if (
            expected_freshness_penalty is None
            or declared_freshness_penalty is None
            or abs(declared_freshness_penalty - expected_freshness_penalty) > 0.01
            or "freshness_status" in review
            and str(review.get("freshness_status") or "") != freshness_status
            or "freshness_penalty" in review
            and _as_float(review.get("freshness_penalty")) is not None
            and abs(float(review["freshness_penalty"]) - declared_freshness_penalty) > 0.01
        ):
            errors.append("calibration_adjustments.freshness_penalty")
        pre_band_score = _as_float(adjustments.get("pre_band_score"))
        if (
            raw_score is not None
            and declared_source_penalty is not None
            and declared_freshness_penalty is not None
            and pre_band_score is not None
            and abs(pre_band_score - round(raw_score - declared_source_penalty - declared_freshness_penalty, 1)) > 0.11
        ):
            errors.append("calibration_adjustments.pre_band_score")
        final_score = _as_float(adjustments.get("final_score"))
        declared_score = _as_float(review.get("buy_attractiveness_score"))
        if (
            raw_score is not None
            and final_score is not None
            and declared_score is not None
            and abs(final_score - declared_score) > 0.11
        ):
            errors.append("calibration_adjustments.final_score_mismatch")
        action = str(review.get("ai_action") or "")
        expected_band = {
            "priority_buy": (70.0, 100.0),
            "watchlist": (50.0, 69.0),
            "avoid": (0.0, 49.0),
            "insufficient_evidence": (0.0, 49.0),
        }.get(action)
        declared_min = _as_float(adjustments.get("action_band_min"))
        declared_max = _as_float(adjustments.get("action_band_max"))
        if expected_band and (
            declared_min is None
            or declared_max is None
            or abs(declared_min - expected_band[0]) > 0.01
            or abs(declared_max - expected_band[1]) > 0.01
        ):
            errors.append("calibration_adjustments.action_band")
        if expected_band and pre_band_score is not None and final_score is not None:
            expected_final = round(min(expected_band[1], max(expected_band[0], pre_band_score)), 1)
            if abs(final_score - expected_final) > 0.11:
                errors.append("calibration_adjustments.final_score_math")
            band_clamped = adjustments.get("band_clamped")
            if not isinstance(band_clamped, bool) or band_clamped != (abs(final_score - pre_band_score) > 0.05):
                errors.append("calibration_adjustments.band_clamped")
    else:
        raw_score = _as_float(review.get("buy_attractiveness_score"))
    if component_values and raw_score is not None:
        expected = component_values["risk_adjusted_expected_return"]
        if abs(raw_score - expected) > 0.51:
            errors.append("score_components_formula")

    findings = review.get("search_findings")
    finding_ids: set[str] = set()
    if not isinstance(findings, list) or not findings or len(findings) > 16:
        errors.append("search_findings")
        findings = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            errors.append("search_findings")
            continue
        finding_id = str(finding.get("id") or "").strip()
        if not finding_id or finding_id in finding_ids:
            errors.append("search_findings.id")
        finding_ids.add(finding_id)
        if not str(finding.get("query") or "").strip() or not str(finding.get("finding") or "").strip():
            errors.append("search_findings.content")
        url = finding.get("url")
        if url not in (None, "") and not str(url).lower().startswith("https://"):
            errors.append("search_findings.url")

    claims = review.get("claims")
    fact_ids: set[str] = set()
    if not isinstance(claims, list):
        errors.append("claims")
        claims = []
    elif len(claims) > 32:
        errors.append("claims")
    for claim in claims:
        if not isinstance(claim, Mapping):
            errors.append("claim")
            continue
        source_ref = str(claim.get("source_ref") or "").strip()
        source_refs = claim.get("source_refs")
        if source_ref.lower().startswith("http://"):
            errors.append("claim_https_source_ref")
        if isinstance(source_refs, list) and any(
            isinstance(value, str) and value.strip().lower().startswith("http://") for value in source_refs
        ):
            errors.append("claim_https_source_refs")
        fact_id = str(claim.get("fact_id") or "").strip()
        search_id = str(claim.get("search_finding_id") or "").strip()
        if bool(fact_id) == bool(search_id):
            errors.append("claim_evidence_identity")
        if fact_id:
            if fact_id in fact_ids:
                errors.append("claim_fact_id")
            fact_ids.add(fact_id)
        if search_id and search_id not in finding_ids:
            errors.append("claim_search_finding_id")
        if fact_id and not str(claim.get("statement") or "").strip():
            errors.append("claim_statement")

    bindings = review.get("evidence_bindings")
    if not isinstance(bindings, Mapping):
        errors.append("evidence_bindings")
        bindings = {}

    def binding_ids(value: Any, field: str) -> tuple[list[str], list[str]]:
        if not isinstance(value, Mapping):
            errors.append(field)
            return [], []
        raw_facts = value.get("fact_ids")
        raw_search = value.get("search_finding_ids")
        if not isinstance(raw_facts, list) or not isinstance(raw_search, list):
            errors.append(field)
            return [], []
        facts = [str(item).strip() for item in raw_facts]
        searches = [str(item).strip() for item in raw_search]
        if (
            any(not item for item in [*facts, *searches])
            or len(facts) != len(set(facts))
            or len(searches) != len(set(searches))
            or set(facts) - fact_ids
            or set(searches) - finding_ids
        ):
            errors.append(field)
        return facts, searches

    binding_ids(bindings.get("summary"), "evidence_bindings.summary")
    strengths = bindings.get("strengths")
    risks = bindings.get("risks")
    key_strengths = review.get("key_strengths") if isinstance(review.get("key_strengths"), list) else []
    risk_flags = review.get("risk_flags") if isinstance(review.get("risk_flags"), list) else []
    if not isinstance(strengths, list) or len(strengths) != len(key_strengths):
        errors.append("evidence_bindings.strengths")
    else:
        for index, value in enumerate(strengths):
            binding_ids(value, f"evidence_bindings.strengths[{index}]")
    if not isinstance(risks, list) or len(risks) != len(risk_flags):
        errors.append("evidence_bindings.risks")
    else:
        for index, value in enumerate(risks):
            binding_ids(value, f"evidence_bindings.risks[{index}]")
    profile_bindings = bindings.get("economic_profile")
    if not isinstance(profile_bindings, Mapping):
        errors.append("evidence_bindings.economic_profile")
    else:
        for field in ECONOMIC_PROFILE_FIELDS:
            binding_ids(profile_bindings.get(field), f"evidence_bindings.economic_profile.{field}")

    valuation = review.get("valuation")
    if not isinstance(valuation, Mapping):
        errors.append("valuation")
    else:
        valuation_evidence = valuation.get("evidence_ids")
        valuation_evidence_ids = (
            [str(item).strip() for item in valuation_evidence] if isinstance(valuation_evidence, list) else []
        )
        if (
            not valuation_evidence_ids
            or any(not item for item in valuation_evidence_ids)
            or len(valuation_evidence_ids) != len(set(valuation_evidence_ids))
            or any(item not in fact_ids for item in valuation_evidence_ids)
        ):
            errors.append("valuation.evidence_ids")
        anchor = valuation.get("normalization_anchor")
        if anchor is not None and not isinstance(anchor, Mapping):
            errors.append("valuation.normalization_anchor")
        elif isinstance(anchor, Mapping):
            anchor_years = anchor.get("years")
            if (
                not isinstance(anchor.get("metric"), str)
                or not str(anchor.get("metric") or "").strip()
                or anchor_years is not None
                and (
                    not isinstance(anchor_years, list)
                    or any(
                        isinstance(year, bool) or not isinstance(year, int) or year < 1900 or year > 2100
                        for year in anchor_years
                    )
                )
                or any(
                    anchor.get(item) not in (None, "") and _as_float(anchor.get(item)) is None
                    for item in ("total", "share_count", "per_share")
                )
            ):
                errors.append("valuation.normalization_anchor")
        multiple_basis = valuation.get("multiple_basis")
        if multiple_basis is not None and not isinstance(multiple_basis, Mapping):
            errors.append("valuation.multiple_basis")
        elif isinstance(multiple_basis, Mapping):
            if (
                not isinstance(multiple_basis.get("metric"), str)
                or not str(multiple_basis.get("metric") or "").strip()
                or multiple_basis.get("value") not in (None, "")
                and _as_float(multiple_basis.get("value")) is None
            ):
                errors.append("valuation.multiple_basis")
        valuation_fact_ids, valuation_search_ids = binding_ids(bindings.get("valuation"), "evidence_bindings.valuation")
        if valuation_evidence_ids and set(valuation_fact_ids) != set(valuation_evidence_ids):
            errors.append("evidence_bindings.valuation")
        if isinstance(multiple_basis, Mapping):
            search_id = str(multiple_basis.get("search_finding_id") or "").strip()
            if search_id and search_id not in valuation_search_ids:
                errors.append("valuation.multiple_basis")
    return list(dict.fromkeys(errors))


def validate_review(
    review: Mapping[str, Any],
    *,
    require_readable_reason: bool = False,
    require_company_research_fields: bool = False,
    require_calibration: bool | None = None,
) -> list[str]:
    """Validate the second-pass ranking without trusting it to rewrite rules."""
    errors: list[str] = []
    # The same audited model profile is used by the older native-web overlay.
    # Strict company-thesis fields therefore belong to the explicit artifact
    # review mode/call site, not to an inference from model metadata alone.
    strict_company_research = require_company_research_fields
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("schema_version")
    if not str(review.get("security_code") or "").strip():
        errors.append("security_code")
    if str(review.get("type_key") or "") not in TYPE_KEYS:
        errors.append("type_key")
    if str(review.get("verdict")) not in REVIEW_VERDICTS:
        errors.append("verdict")
    if str(review.get("recommended_action")) not in REVIEW_ACTIONS:
        errors.append("recommended_action")
    score = _as_float(review.get("buy_attractiveness_score"))
    if score is None or score < 0 or score > 100:
        errors.append("buy_attractiveness_score")
    if str(review.get("ai_action")) not in AI_ACTIONS:
        errors.append("ai_action")
    if "final_recommendation" in review and str(review.get("final_recommendation")) not in FINAL_RECOMMENDATIONS:
        errors.append("final_recommendation")
    action = str(review.get("ai_action") or "")
    verdict = str(review.get("verdict") or "")
    recommended_action = str(review.get("recommended_action") or "")
    if action in AI_ACTIONS and verdict in REVIEW_VERDICTS:
        if verdict not in _ACTION_ALLOWED_VERDICTS[action]:
            errors.append("action_verdict_mismatch")
    if action in AI_ACTIONS and recommended_action in REVIEW_ACTIONS:
        if recommended_action not in _ACTION_ALLOWED_REVIEW_ACTIONS[action]:
            errors.append("action_recommended_action_mismatch")
    if verdict == "misclassified" and (action != "avoid" or recommended_action != "demote"):
        errors.append("misclassified_decision_mismatch")
    if verdict == "needs_review" and (
        action not in {"watchlist", "insufficient_evidence"} or recommended_action != "manual_review"
    ):
        errors.append("needs_review_decision_mismatch")
    expected_category = {
        "priority_buy": "recommend_buy",
        "watchlist": "observe",
        "avoid": "do_not_recommend",
        "insufficient_evidence": "observe",
    }.get(action)
    if "final_category" in review:
        if str(review.get("final_category")) not in {"recommend_buy", "observe", "do_not_recommend"}:
            errors.append("final_category")
        elif expected_category and str(review.get("final_category")) != expected_category:
            errors.append("final_category_action_mismatch")
    if "final_recommendation" in review and expected_category:
        expected_recommendation = "recommend_buy" if expected_category == "recommend_buy" else "do_not_recommend_buy"
        if str(review.get("final_recommendation")) != expected_recommendation:
            errors.append("final_recommendation_action_mismatch")
    # Score meaning is part of the public contract, regardless of which model
    # produced the opinion.  Without this gate an external model could publish
    # a 100-point "observe" or "do not recommend" card while still satisfying
    # the schema and category mapping.
    if action == "priority_buy" and score is not None and score < 70:
        errors.append("priority_score_band")
    if action == "watchlist" and score is not None and not 50 <= score < 70:
        errors.append("watchlist_score_band")
    if action in {"avoid", "insufficient_evidence"} and score is not None and score >= 50:
        errors.append("negative_score_band")
    if "recommendation_label" in review:
        label = review.get("recommendation_label")
        if not isinstance(label, str):
            errors.append("recommendation_label")
        else:
            expected_label_prefix = {
                "priority_buy": "建议买",
                "watchlist": "观察",
                "avoid": "不建议",
                "insufficient_evidence": "观察",
            }.get(action)
            if expected_label_prefix and not label.strip().startswith(expected_label_prefix):
                errors.append("recommendation_label_action_mismatch")
            elif _decision_text_conflicts(action, label):
                errors.append("recommendation_label_action_mismatch")
    summary = review.get("summary")
    if summary is not None and not isinstance(summary, str):
        errors.append("summary")
    elif isinstance(summary, str):
        if _decision_text_conflicts(action, summary):
            errors.append("summary_action_mismatch")
    reason_fields: list[Any] = [summary]
    for field in ("key_strengths", "risk_flags", "quantitative_facts"):
        values = review.get(field)
        if isinstance(values, list):
            reason_fields.extend(values)
    if strict_company_research and any(
        isinstance(value, str) and _RULE_REASON_LEAK_RE.search(value) for value in reason_fields
    ):
        errors.append("rule_language_in_ai_reason")
    if strict_company_research and stale_current_period_fields(review):
        errors.append("stale_current_period")
    if require_readable_reason:
        if not _readable_reason(summary, minimum=8):
            errors.append("readable_summary_required")
        required_reason_fields = (
            ("key_strengths", "risk_flags") if action in {"priority_buy", "watchlist"} else ("risk_flags",)
        )
        for field in required_reason_fields:
            values = review.get(field)
            if not isinstance(values, list) or not any(_readable_reason(value, minimum=2) for value in values):
                errors.append(f"readable_{field}_required")
    if "ai_independent" in review and not isinstance(review.get("ai_independent"), bool):
        errors.append("ai_independent")
    if str(review.get("confidence")) not in AI_CONFIDENCE:
        errors.append("confidence")
    if "web_search_performed" in review and not isinstance(review.get("web_search_performed"), bool):
        errors.append("web_search_performed")
    if "web_search_verified" in review and not isinstance(review.get("web_search_verified"), bool):
        errors.append("web_search_verified")
    for field in (
        "web_search_event_verified",
        "web_search_claim_urls_verified",
        "research_source_urls_verified",
    ):
        if field in review and not isinstance(review.get(field), bool):
            errors.append(field)
    if review.get("web_search_event_verified") is True and review.get("web_search_performed") is not True:
        errors.append("web_search_event_without_search")
    if review.get("web_search_claim_urls_verified") is True and review.get("web_search_event_verified") is not True:
        errors.append("web_search_claims_without_event")
    if review.get("research_source_urls_verified") is True:
        claims = review.get("claims")
        if not isinstance(claims, list) or not any(
            isinstance(claim, Mapping) and _claim_has_public_url(claim) for claim in claims
        ):
            errors.append("research_sources_without_url")
    for field, limit in (("web_search_queries", 16), ("web_search_verified_claim_urls", 16)):
        if field not in review:
            continue
        values = review.get(field)
        if (
            not isinstance(values, list)
            or len(values) > limit
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(set(values)) != len(values)
        ):
            errors.append(field)
    if "web_search_dropped_claim_url_count" in review:
        dropped = review.get("web_search_dropped_claim_url_count")
        if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
            errors.append("web_search_dropped_claim_url_count")
    if "freshness_status" in review and str(review.get("freshness_status")) not in FRESHNESS_STATUSES:
        errors.append("freshness_status")
    if "freshness_years" in review:
        years = review.get("freshness_years")
        if (
            not isinstance(years, list)
            or len(years) > 12
            or any(not isinstance(year, int) or year < 1900 or year > 2100 for year in years)
            or len(set(years)) != len(years)
        ):
            errors.append("freshness_years")
    if "freshness_penalty" in review:
        penalty = _as_float(review.get("freshness_penalty"))
        if penalty is None or penalty < 0 or penalty > 20:
            errors.append("freshness_penalty")
    if "freshness_note" in review and not isinstance(review.get("freshness_note"), str):
        errors.append("freshness_note")
    freshness_status = str(review.get("freshness_status") or "")
    if freshness_status in {"historical", "undated"}:
        local_unsearched_buy = (
            str(review.get("model") or "") in LOCAL_OPENCODE_MODELS and review.get("web_search_performed") is not True
        )
        if str(review.get("ai_action") or "") == "priority_buy" and not local_unsearched_buy:
            errors.append("stale_priority_buy")
        if str(review.get("final_category") or "") == "recommend_buy" and not local_unsearched_buy:
            errors.append("stale_recommend_buy")
        if str(review.get("final_recommendation") or "") == "recommend_buy" and not local_unsearched_buy:
            errors.append("stale_final_recommendation")
    for field in ("key_strengths", "risk_flags"):
        values = review.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            errors.append(field)
    if "quantitative_facts" in review:
        values = review.get("quantitative_facts")
        if (
            not isinstance(values, list)
            or len(values) < 1
            or len(values) > 8
            or any(not isinstance(value, str) or not value.strip() or len(value) > 240 for value in values)
            or len(set(values)) != len(values)
        ):
            errors.append("quantitative_facts")
    claims = review.get("claims", [])
    if not isinstance(claims, list):
        errors.append("claims")
    else:
        for claim in claims:
            if not isinstance(claim, Mapping) or not _claim_source_values(claim):
                errors.append("claim_source_ref")
                break
            if "source_refs" in claim:
                refs = claim.get("source_refs")
                if (
                    not isinstance(refs, list)
                    or not refs
                    or len(refs) > 16
                    or any(not isinstance(value, str) or not value.strip() for value in refs)
                    or len(set(refs)) != len(refs)
                    or str(claim.get("source_ref") or "") != refs[0]
                ):
                    errors.append("claim_source_refs")
                    break
    errors.extend(_company_research_envelope_errors(review, reject_rule_language=strict_company_research))
    if action == "priority_buy":
        errors.extend(_priority_research_evidence_errors(review, reject_rule_language=strict_company_research))
    if require_company_research_fields:
        errors.extend(valuation_snapshot_errors(review))
        errors.extend(
            _company_research_public_errors(
                review,
                require_calibration=(
                    require_company_research_fields if require_calibration is None else require_calibration
                ),
            )
        )
    return errors
