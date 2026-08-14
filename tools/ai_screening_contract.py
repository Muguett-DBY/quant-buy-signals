"""Local AI-screening contract helpers.

This module deliberately keeps the deterministic seven-type result separate
from an optional AI review overlay.  It can be used by a local Reasonix batch
without putting model credentials in the Cloudflare worker.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REVIEW_SCHEMA_VERSION = 2
PLACEHOLDER_REVIEW_MODEL = "pending-local-opencode-go"
REVIEW_VERDICTS = frozenset({"confirmed", "caution", "misclassified", "missed_candidate", "needs_review"})
REVIEW_ACTIONS = frozenset({"keep", "demote", "manual_review"})
AI_ACTIONS = frozenset({"priority_buy", "watchlist", "avoid", "insufficient_evidence"})
AI_CONFIDENCE = frozenset({"high", "medium", "low"})
TYPE_KEYS = tuple(f"type{i}" for i in range(1, 8))


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


def validate_review(review: Mapping[str, Any]) -> list[str]:
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
    if str(review.get("confidence")) not in AI_CONFIDENCE:
        errors.append("confidence")
    if "web_search_performed" in review and not isinstance(review.get("web_search_performed"), bool):
        errors.append("web_search_performed")
    if "web_search_verified" in review and not isinstance(review.get("web_search_verified"), bool):
        errors.append("web_search_verified")
    for field in ("key_strengths", "risk_flags"):
        values = review.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            errors.append(field)
    claims = review.get("claims", [])
    if not isinstance(claims, list):
        errors.append("claims")
    else:
        for claim in claims:
            if not isinstance(claim, Mapping) or not claim.get("source_ref"):
                errors.append("claim_source_ref")
                break
    return errors
