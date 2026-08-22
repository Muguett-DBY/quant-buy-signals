"""Local AI-screening contract helpers.

This module deliberately keeps the deterministic seven-type result separate
from an optional AI review overlay.  It can be used by a local Reasonix batch
without putting model credentials in the Cloudflare worker.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

REVIEW_SCHEMA_VERSION = 2
PLACEHOLDER_REVIEW_MODEL = "pending-local-opencode-go"
LOCAL_REVIEW_MODEL = "codex-local-review-v1"
LOCAL_OPENCODE_MODELS = frozenset(
    {
        "opencode-go/ox-alpha-free",
        "opencode-go/muse-spark-1.2-contributor",
    }
)
NATIVE_WEB_REVIEW_MODE = "opencode_native_web_search_review"
NATIVE_WEB_REVIEW_MODEL = "opencode-go/muse-spark-1.2-contributor"
NATIVE_WEB_RETRIEVAL_MODEL = "opencode-go-muse/muse-spark-1.2-contributor"
PARTIAL_SEARCH_REVIEW_MODES = frozenset({"local_codex_review", "opencode_mixed_review"})
REVIEW_VERDICTS = frozenset({"confirmed", "caution", "misclassified", "missed_candidate", "needs_review"})
REVIEW_ACTIONS = frozenset({"keep", "demote", "manual_review"})
AI_ACTIONS = frozenset({"priority_buy", "watchlist", "avoid", "insufficient_evidence"})
FINAL_RECOMMENDATIONS = frozenset({"recommend_buy", "do_not_recommend_buy"})
AI_CONFIDENCE = frozenset({"high", "medium", "low"})
FRESHNESS_STATUSES = frozenset({"current_or_recent", "historical", "undated"})
TYPE_KEYS = tuple(f"type{i}" for i in range(1, 8))

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


def candidate_identity_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    """Hash an ordered company/type candidate universe."""

    digest = hashlib.sha256()
    for record in records:
        code = str(record.get("security_code") or record.get("code") or "").strip()
        type_key = str(record.get("type_key") or record.get("type") or "").strip()
        if not code or type_key not in TYPE_KEYS:
            raise ValueError("candidate identity is incomplete")
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


def validate_review(review: Mapping[str, Any], *, require_readable_reason: bool = False) -> list[str]:
    """Validate the second-pass ranking without trusting it to rewrite rules."""
    errors: list[str] = []
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
    if action == "priority_buy" and score is not None and score < 60:
        errors.append("priority_score_band")
    if action == "watchlist" and score is not None and score >= 70:
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
    if require_readable_reason:
        if not _readable_reason(summary, minimum=8):
            errors.append("readable_summary_required")
        for field in ("key_strengths", "risk_flags"):
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
    for field in ("web_search_event_verified", "web_search_claim_urls_verified"):
        if field in review and not isinstance(review.get(field), bool):
            errors.append(field)
    if review.get("web_search_event_verified") is True and review.get("web_search_performed") is not True:
        errors.append("web_search_event_without_search")
    if review.get("web_search_claim_urls_verified") is True and review.get("web_search_event_verified") is not True:
        errors.append("web_search_claims_without_event")
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
    claims = review.get("claims", [])
    if not isinstance(claims, list):
        errors.append("claims")
    else:
        for claim in claims:
            if not isinstance(claim, Mapping) or not (claim.get("source_ref") or claim.get("source_context")):
                errors.append("claim_source_ref")
                break
    return errors
