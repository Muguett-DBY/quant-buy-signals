"""Publish a compact, generation-bound AI screening overlay.

The overlay is advisory only.  It deliberately contains the deterministic
decision bounds and the model's auditable claims, but never a rule-context
payload that could be mistaken for a replacement calculation.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import (
    PLACEHOLDER_REVIEW_MODEL,
    REVIEW_SCHEMA_VERSION,
    validate_review,
)

ARTIFACT_SCHEMA_VERSION = 2
ARTIFACT_KIND = "ai_screening_overlay"
_DETERMINISTIC_FIELDS = (
    "status",
    "score",
    "score_lower_bound",
    "score_upper_bound",
    "decision_basis",
    "decision_complete",
    "potentially_triggerable",
    "veto_state",
)
_URL_RE = re.compile(r"https?://[^\s)）>]+", re.IGNORECASE)
_ASCII_URL_RE = re.compile(r"[A-Za-z0-9:/?#\[\]@!$&'()*+,;=%._~\-]+")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _text(value: Any, limit: int = 800) -> str:
    return str(value or "").strip()[:limit]


def _public_review(review: Mapping[str, Any]) -> dict[str, Any]:
    errors = validate_review(review)
    if errors:
        raise ValueError(f"invalid AI review: {','.join(errors)}")
    claims: list[dict[str, str]] = []
    for claim in review.get("claims", []):
        if not isinstance(claim, Mapping):
            raise ValueError("AI claim must be an object")
        raw_source = _text(claim.get("source_ref"), 800)
        match = _URL_RE.search(raw_source)
        source_ref = ""
        if match and match.group(0).lower().startswith("https://"):
            # Reasonix sometimes appends a Chinese explanation directly after
            # an otherwise valid URL.  Keep the URL's ASCII grammar and drop
            # that annotation so public links remain clickable and auditable.
            ascii_match = _ASCII_URL_RE.match(match.group(0))
            source_ref = ascii_match.group(0).rstrip(".,;，。；）") if ascii_match else ""
        claims.append(
            {
                "statement": _text(claim.get("statement"), 600),
                "source_ref": source_ref,
                "source_context": raw_source[:240],
            }
        )
    web_search_performed = review.get("web_search_performed") is True
    web_search_verified = bool(
        web_search_performed
        and any(
            str(claim.get("source_ref") or "").lower().startswith("https://")
            for claim in claims
            if isinstance(claim, Mapping)
        )
    )
    action = _text(review.get("ai_action"), 32)
    recommendation = _text(review.get("final_recommendation"), 32)
    if recommendation not in {"recommend_buy", "do_not_recommend_buy"}:
        recommendation = "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy"
    label = _text(review.get("recommendation_label"), 64)
    if not label:
        label = "推荐买入候选" if recommendation == "recommend_buy" else "不推荐现在买入"
    return {
        "verdict": _text(review.get("verdict"), 32),
        "recommended_action": _text(review.get("recommended_action"), 32),
        "buy_attractiveness_score": float(review["buy_attractiveness_score"]),
        "ai_action": action,
        "final_recommendation": recommendation,
        "recommendation_label": label,
        "confidence": _text(review.get("confidence"), 16),
        "summary": _text(review.get("summary"), 1200),
        "key_strengths": [_text(item, 240) for item in review.get("key_strengths", [])[:8]],
        "risk_flags": [_text(item, 240) for item in review.get("risk_flags", [])[:12]],
        "claims": claims[:12],
        "model": _text(review.get("model"), 120),
        "effort": _text(review.get("effort"), 32),
        "web_search_performed": web_search_performed,
        "web_search_verified": web_search_verified,
    }


def _public_web_verified(review: Mapping[str, Any]) -> bool:
    """Return true only when the published review has a usable HTTPS claim."""
    return bool(
        review.get("web_search_performed") is True
        and any(
            str(claim.get("source_ref") or "").lower().startswith("https://")
            for claim in review.get("claims", [])
            if isinstance(claim, Mapping)
        )
    )


def _public_deterministic(packet: Mapping[str, Any]) -> dict[str, Any]:
    source = packet.get("deterministic")
    if not isinstance(source, Mapping):
        raise ValueError("candidate deterministic result is missing")
    decision = source.get("decision") if isinstance(source.get("decision"), Mapping) else {}
    result: dict[str, Any] = {}
    for field in _DETERMINISTIC_FIELDS:
        value = source.get(field)
        if value is None:
            value = decision.get(field)
        if value is not None:
            result[field] = value
    return result


def build_artifact(
    merged_path: Path,
    output_path: Path,
    *,
    expected_generation: str,
    expected_market_as_of: str,
    source_audit_path: Path | None = None,
) -> dict[str, Any]:
    source = _load(merged_path)
    generation = str(source.get("snapshot_generation") or "")
    market_as_of = str(source.get("market_as_of") or "")
    if generation != expected_generation:
        raise ValueError(f"generation mismatch: {generation!r} != {expected_generation!r}")
    if market_as_of != expected_market_as_of:
        raise ValueError(f"market_as_of mismatch: {market_as_of!r} != {expected_market_as_of!r}")
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("merged AI screening packets are missing")
    public_packets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    verdicts: Counter[str] = Counter()
    attempted_review_count = 0
    unreviewed_candidate_count = 0
    attempted_needs_review_count = 0
    web_search_completed_count = 0
    action_counts: Counter[str] = Counter()
    full_coverage = source.get("full_coverage_final_recommendation") is True
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("candidate packet must be an object")
        code = _text(packet.get("security_code"), 16)
        type_key = _text(packet.get("type_key"), 16)
        key = (code, type_key)
        if not code or not type_key or key in seen:
            raise ValueError(f"duplicate or incomplete candidate: {key}")
        seen.add(key)
        review = packet.get("ai_review")
        if review is None:
            if full_coverage:
                raise ValueError(f"full-coverage candidate has no AI review: {key}")
            continue
        if not isinstance(review, Mapping):
            raise ValueError(f"AI review is not an object: {key}")
        public_review = _public_review(review)
        if full_coverage and public_review["ai_action"] == "insufficient_evidence":
            raise ValueError(f"full-coverage candidate has no final decision: {key}")
        verdicts[public_review["verdict"]] += 1
        action_counts[public_review["ai_action"]] += 1
        if public_review["model"] == PLACEHOLDER_REVIEW_MODEL:
            unreviewed_candidate_count += 1
        else:
            attempted_review_count += 1
            if _public_web_verified(public_review):
                web_search_completed_count += 1
            if public_review["verdict"] == "needs_review":
                attempted_needs_review_count += 1
        public_packets.append(
            {
                "security_code": code,
                "name": _text(packet.get("name"), 160),
                "type_key": type_key,
                "deterministic": _public_deterministic(packet),
                "ai_review": public_review,
            }
        )
    action_priority = {"priority_buy": 0, "watchlist": 1, "avoid": 2, "insufficient_evidence": 3}
    public_packets.sort(
        key=lambda value: (
            -float(value["ai_review"]["buy_attractiveness_score"]),
            action_priority.get(value["ai_review"]["ai_action"], 9),
            value["security_code"],
            value["type_key"],
        )
    )
    for rank, packet in enumerate(public_packets, 1):
        packet["ai_rank"] = rank
    source_audit: dict[str, Any] = {"available": False}
    if source_audit_path:
        audit = _load(source_audit_path)
        source_audit = {
            "available": True,
            "checked": int(audit.get("checked", 0) or 0),
            "ok": int(audit.get("ok", 0) or 0),
            "failed": int(audit.get("failed", 0) or 0),
            "blocked": int(audit.get("blocked", 0) or 0),
        }
    source_audit["web_search_completed"] = web_search_completed_count
    source_audit["reviewed_without_web_search"] = max(0, attempted_review_count - web_search_completed_count)
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "ai_is_advisory": True,
        "auto_buy_promotion": False,
        "full_coverage_final_recommendation": full_coverage,
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        "methodology_version": source.get("methodology_version"),
        "index_contract": source.get("index_contract"),
        "candidate_total": int(source.get("candidate_total", 0) or 0),
        "candidate_offset": int(source.get("candidate_offset", 0) or 0),
        "reviewed_count": len(public_packets),
        "attempted_review_count": attempted_review_count,
        "unreviewed_candidate_count": unreviewed_candidate_count,
        "attempted_needs_review_count": attempted_needs_review_count,
        "web_search_completed_count": web_search_completed_count,
        "reviewed_without_web_search": max(0, attempted_review_count - web_search_completed_count),
        # Keep the old field names as compatibility aliases.  They now mean
        # attempted versus not-yet-started, rather than verdict quality.
        "completed_review_count": attempted_review_count,
        "pending_review_count": unreviewed_candidate_count,
        "verdict_counts": dict(sorted(verdicts.items())),
        "ai_action_counts": dict(sorted(action_counts.items())),
        "priority_buy_count": action_counts["priority_buy"],
        "recommend_buy_count": action_counts["priority_buy"],
        "watchlist_count": action_counts["watchlist"],
        "avoid_count": action_counts["avoid"],
        "do_not_recommend_buy_count": action_counts["watchlist"]
        + action_counts["avoid"]
        + action_counts["insufficient_evidence"],
        "insufficient_evidence_count": action_counts["insufficient_evidence"],
        "ranking_version": "ai-buy-attractiveness-v3-web-gated",
        "ranking_source": _text(source.get("ranking_source"), 120),
        "source_audit": source_audit,
        "packets": public_packets,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--expected-market-as-of", required=True)
    parser.add_argument("--source-audit", type=Path)
    args = parser.parse_args()
    artifact = build_artifact(
        args.merged,
        args.output,
        expected_generation=args.expected_generation,
        expected_market_as_of=args.expected_market_as_of,
        source_audit_path=args.source_audit,
    )
    print(json.dumps({"artifact_kind": artifact["artifact_kind"], "reviewed_count": artifact["reviewed_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
