"""Contract helpers for a native research pass and an independent review pass.

The existing public overlay stores one flattened ``ai_review``.  This module
defines the smaller company-level contract needed before that flattening:
both channels must cover the same companies exactly once, both must carry
native-search proof, and a disagreement may never remain a buy recommendation.

Deterministic type/status fields are accepted as context, but are deliberately
excluded from the decision-reason scan.  They can therefore be shown beside a
final opinion without becoming an AI buy rationale.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

RESEARCH_CHANNEL = "native_research"
SECOND_REVIEW_CHANNEL = "independent_review"

RECOMMEND_BUY = "recommend_buy"
OBSERVE = "observe"
DO_NOT_RECOMMEND = "do_not_recommend"
FINAL_ACTIONS = frozenset({RECOMMEND_BUY, OBSERVE, DO_NOT_RECOMMEND})

_CODE_RE = re.compile(r"^[0-9]{6}$")
_RULE_REASON_RE = re.compile(
    r"\btype\s*[1-7]\b|类型\s*[1-7]|"
    r"第[一二三四五六七1-7](?:种|类)(?:买入)?(?:情况|类型)|"
    r"确定性(?:筛选|规则|评分|分数|状态)|"
    r"(?:筛选|买入|七类|模型)规则(?:分数|评分|状态|触发|达标|结果|候选)|"
    r"(?:候选|规则|筛选|类型|type).{0,8}(?:触发|达标)|"
    r"(?:触发|达标).{0,8}(?:候选|规则|筛选|类型|type)|候选池|入池|"
    r"\b(?:triggered|conditional|insufficient_evidence)\b",
    re.IGNORECASE,
)
_REASON_FIELDS = (
    "summary",
    "reason",
    "decision_reason",
    "final_reason",
    "justification",
    "key_strengths",
    "risk_flags",
    "quantitative_facts",
    "reasons",
    "economic_profile",
    "valuation",
    "claims",
    "search_findings",
)
_ACTION_MAP = {
    "recommend_buy": RECOMMEND_BUY,
    "priority_buy": RECOMMEND_BUY,
    "建议买": RECOMMEND_BUY,
    "recommend": RECOMMEND_BUY,
    "observe": OBSERVE,
    "watchlist": OBSERVE,
    "insufficient_evidence": OBSERVE,
    "观察": OBSERVE,
    "do_not_recommend": DO_NOT_RECOMMEND,
    "do_not_recommend_buy": DO_NOT_RECOMMEND,
    "avoid": DO_NOT_RECOMMEND,
    "不建议": DO_NOT_RECOMMEND,
}
_ACTION_LABELS = {RECOMMEND_BUY: "建议买", OBSERVE: "观察", DO_NOT_RECOMMEND: "不建议"}
_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class DualChannelContractError(ValueError):
    """Raised when the two company-level channels cannot be joined safely."""


def _code(value: Any) -> str:
    code = str(value or "").strip()
    if not _CODE_RE.fullmatch(code):
        raise DualChannelContractError(f"invalid company security_code: {code!r}")
    return code


def _name(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "").casefold()).strip()


def _action(review: Mapping[str, Any]) -> str:
    for field in ("final_action", "final_category", "action", "ai_action"):
        raw = str(review.get(field) or "").strip()
        if raw:
            normalized = _ACTION_MAP.get(raw.casefold(), _ACTION_MAP.get(raw))
            if normalized:
                return normalized
            break
    raise DualChannelContractError(f"review has no supported final action: {review.get('security_code')!r}")


def _reason_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value
        return
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _reason_values(child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for child in value:
            yield from _reason_values(child)


def _decision_reason_text(review: Mapping[str, Any]) -> list[str]:
    """Return only decision-bearing text; deterministic context is not scanned."""

    values: list[str] = []
    for field in _REASON_FIELDS:
        values.extend(_reason_values(review.get(field)))
    return values


def _validate_review(
    review: Mapping[str, Any],
    *,
    expected_channel: str,
    expected_generation: str | None,
    expected_market_as_of: str | None,
) -> tuple[str, str, str, str]:
    code = _code(review.get("security_code"))
    name = str(review.get("company_name") or review.get("name") or "").strip()
    if not name:
        raise DualChannelContractError(f"company name is missing: {code}")
    if str(review.get("channel") or "") != expected_channel:
        raise DualChannelContractError(f"wrong channel for {code}")
    reviewer_id = str(review.get("reviewer_id") or "").strip()
    if not reviewer_id:
        raise DualChannelContractError(f"reviewer_id is missing: {code}")
    if review.get("web_search_event_verified") is not True:
        raise DualChannelContractError(f"native web-search proof is missing: {code}")
    if expected_channel == RESEARCH_CHANNEL and review.get("research_source_urls_verified") is not True:
        raise DualChannelContractError(f"research source proof is missing: {code}")
    confidence = str(review.get("confidence") or "").strip().casefold()
    if confidence not in _CONFIDENCE_LEVELS:
        raise DualChannelContractError(f"invalid confidence for {code}")
    action = _action(review)
    if action not in FINAL_ACTIONS:
        raise DualChannelContractError(f"invalid final action for {code}")
    if expected_generation is not None and str(review.get("snapshot_generation") or "") != expected_generation:
        raise DualChannelContractError(f"generation mismatch for {code}")
    if expected_market_as_of is not None and str(review.get("market_as_of") or "") != expected_market_as_of:
        raise DualChannelContractError(f"market_as_of mismatch for {code}")
    reason_values = _decision_reason_text(review)
    if not reason_values:
        raise DualChannelContractError(f"decision reason is missing: {code}")
    if any(_RULE_REASON_RE.search(value) for value in reason_values):
        raise DualChannelContractError(f"deterministic rule language leaked into AI reason: {code}")
    return code, name, action, confidence


def _index_reviews(
    reviews: Sequence[Mapping[str, Any]],
    *,
    expected_channel: str,
    expected_generation: str | None,
    expected_market_as_of: str | None,
) -> dict[str, tuple[str, str, str, Mapping[str, Any]]]:
    indexed: dict[str, tuple[str, str, str, Mapping[str, Any]]] = {}
    for review in reviews:
        if not isinstance(review, Mapping):
            raise DualChannelContractError(f"{expected_channel} review is not an object")
        code, name, action, confidence = _validate_review(
            review,
            expected_channel=expected_channel,
            expected_generation=expected_generation,
            expected_market_as_of=expected_market_as_of,
        )
        if code in indexed:
            raise DualChannelContractError(f"duplicate {expected_channel} review: {code}")
        indexed[code] = (name, action, confidence, review)
    if not indexed:
        raise DualChannelContractError(f"{expected_channel} review channel is empty")
    return indexed


def _conservative_action(first: str, second: str) -> str:
    """Keep the strongest negative opinion when channels disagree."""

    return min((first, second), key=lambda value: {RECOMMEND_BUY: 2, OBSERVE: 1, DO_NOT_RECOMMEND: 0}[value])


def _reason(review: Mapping[str, Any]) -> str:
    for field in ("decision_reason", "final_reason", "summary", "reason", "justification"):
        value = str(review.get(field) or "").strip()
        if value:
            return value
    values = _decision_reason_text(review)
    return values[0] if values else ""


def validate_dual_channel_reviews(
    research_reviews: Sequence[Mapping[str, Any]],
    second_reviews: Sequence[Mapping[str, Any]],
    *,
    expected_company_codes: Iterable[str] | None = None,
    expected_generation: str | None = None,
    expected_market_as_of: str | None = None,
) -> dict[str, Any]:
    """Join two complete company-level channels and produce three-way actions.

    A disagreement is always a non-buy outcome.  ``recommend_buy`` is emitted
    only when both channels independently agree.  ``confidence=high`` is
    emitted only for that agreement with two high-confidence channel inputs;
    the helper never upgrades a low-quality or conflicting review.
    """

    research = _index_reviews(
        research_reviews,
        expected_channel=RESEARCH_CHANNEL,
        expected_generation=expected_generation,
        expected_market_as_of=expected_market_as_of,
    )
    second = _index_reviews(
        second_reviews,
        expected_channel=SECOND_REVIEW_CHANNEL,
        expected_generation=expected_generation,
        expected_market_as_of=expected_market_as_of,
    )
    expected = {_code(code) for code in expected_company_codes} if expected_company_codes is not None else set(research)
    if set(research) != expected or set(second) != expected:
        raise DualChannelContractError(
            "company coverage mismatch: "
            f"expected={sorted(expected)} research={sorted(research)} second={sorted(second)}"
        )
    final_reviews: list[dict[str, Any]] = []
    conflict_count = 0
    high_confidence_count = 0
    for code in sorted(expected):
        research_name, research_action, research_confidence, research_review = research[code]
        second_name, second_action, second_confidence, second_review = second[code]
        if _name(research_name) != _name(second_name):
            raise DualChannelContractError(f"company name mismatch between channels: {code}")
        research_reviewer = str(research_review.get("reviewer_id") or "").strip()
        second_reviewer = str(second_review.get("reviewer_id") or "").strip()
        if research_reviewer == second_reviewer:
            raise DualChannelContractError(f"research and second reviewer are not independent: {code}")
        conflict = research_action != second_action
        if conflict:
            conflict_count += 1
        final_action = research_action if not conflict else _conservative_action(research_action, second_action)
        if conflict:
            confidence = "low"
        elif research_confidence == second_confidence == "high":
            confidence = "high"
        elif _CONFIDENCE_RANK[research_confidence] >= 1 and _CONFIDENCE_RANK[second_confidence] >= 1:
            confidence = "medium"
        else:
            confidence = "low"
        if confidence == "high":
            high_confidence_count += 1
        reason = f"研究通道：{_reason(research_review)}；独立复核：{_reason(second_review)}"
        if conflict:
            reason = f"通道结论不一致，已保守降级为{_ACTION_LABELS[final_action]}。{reason}"
        final_reviews.append(
            {
                "security_code": code,
                "company_name": research_name,
                "final_action": final_action,
                "final_category": final_action,
                "recommendation_label": _ACTION_LABELS[final_action],
                "confidence": confidence,
                "conflict": conflict,
                "research_action": research_action,
                "second_reviewer_action": second_action,
                "research_reviewer_id": research_reviewer,
                "second_reviewer_id": second_reviewer,
                "decision_reason": reason,
            }
        )
    counts = Counter(item["final_action"] for item in final_reviews)
    return {
        "candidate_company_total": len(expected),
        "research_reviewed_count": len(research),
        "second_reviewed_count": len(second),
        "conflict_count": conflict_count,
        "consensus_count": len(expected) - conflict_count,
        "high_confidence_count": high_confidence_count,
        "final_category_counts": {action: counts[action] for action in sorted(FINAL_ACTIONS)},
        "companies": final_reviews,
    }
