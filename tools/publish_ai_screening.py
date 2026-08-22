"""Publish a compact, generation-bound AI screening overlay.

The overlay is advisory only.  It deliberately contains the deterministic
decision bounds and the model's auditable claims, but never a rule-context
payload that could be mistaken for a replacement calculation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import (
    LOCAL_REVIEW_MODEL,
    PARTIAL_SEARCH_REVIEW_MODES,
    PLACEHOLDER_REVIEW_MODEL,
    REVIEW_SCHEMA_VERSION,
    candidate_identity_sha256,
    validate_review,
)

ARTIFACT_SCHEMA_VERSION = 2
ARTIFACT_KIND = "ai_screening_overlay"
RANKING_VERSION = "ai-buy-attractiveness-v8-category-first-action-banded"
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
_ACTION_PRIORITY = {"priority_buy": 0, "watchlist": 1, "avoid": 2, "insufficient_evidence": 3}
_FRESHNESS_PRIORITY = {"current_or_recent": 0, "historical": 1, "undated": 2}
_PUBLIC_ACTIONS = ("priority_buy", "watchlist", "avoid", "insufficient_evidence")
_PUBLIC_CATEGORIES = ("recommend_buy", "observe", "do_not_recommend")


def _final_category(action: str) -> str:
    if action == "priority_buy":
        return "recommend_buy"
    if action in {"watchlist", "insufficient_evidence"}:
        return "observe"
    return "do_not_recommend"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _audit_count(audit: Mapping[str, Any], field: str) -> int:
    value = audit.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"source audit {field} is invalid")
    return value


def _validated_source_audit(
    audit_path: Path,
    *,
    merged_sha256: str,
    generation: str,
    market_as_of: str,
) -> dict[str, Any]:
    audit = _load(audit_path)
    if str(audit.get("merged_sha256") or "") != merged_sha256:
        raise ValueError("source audit does not match the merged AI screening file")
    if str(audit.get("snapshot_generation") or "") != generation:
        raise ValueError("source audit generation does not match the merged AI screening file")
    if str(audit.get("market_as_of") or "") != market_as_of:
        raise ValueError("source audit market_as_of does not match the merged AI screening file")
    invalid_count = _audit_count(audit, "invalid_claim_url_count")
    invalid_destination_count = _audit_count(audit, "invalid")
    if invalid_count != 0 or invalid_destination_count != 0:
        raise ValueError("source audit contains invalid or non-public claim URLs")
    checked = _audit_count(audit, "checked")
    ok = _audit_count(audit, "ok")
    failed = _audit_count(audit, "failed")
    blocked = _audit_count(audit, "blocked")
    if checked != ok + failed + blocked + invalid_destination_count:
        raise ValueError("source audit result counts are inconsistent")
    return {
        "available": True,
        "merged_sha256": merged_sha256,
        "invalid_claim_url_count": invalid_count,
        "checked": checked,
        "ok": ok,
        "failed": failed,
        "blocked": blocked,
    }


def _text(value: Any, limit: int = 800) -> str:
    return str(value or "").strip()[:limit]


def _public_review(review: Mapping[str, Any], *, require_readable_reason: bool = False) -> dict[str, Any]:
    errors = validate_review(review, require_readable_reason=require_readable_reason)
    if errors:
        raise ValueError(f"invalid AI review: {','.join(errors)}")
    claims: list[dict[str, str]] = []
    for claim in review.get("claims", []):
        if not isinstance(claim, Mapping):
            raise ValueError("AI claim must be an object")
        raw_source = _text(claim.get("source_ref"), 800)
        if not raw_source:
            # Some OpenCode tool responses put the returned URL in a separate
            # source_context field.  Reuse it only when it is an actual URL;
            # never manufacture a link from a search summary.
            raw_source = _text(claim.get("source_context"), 800)
        match = _URL_RE.search(raw_source)
        source_ref = ""
        if match and match.group(0).lower().startswith(("http://", "https://")):
            # Reasonix sometimes appends a Chinese explanation directly after
            # an otherwise valid URL.  Keep the URL's ASCII grammar and drop
            # that annotation so public links remain clickable.  HTTPS remains
            # a score bonus, not a hard requirement for an AI opinion.
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
    final_category = _text(review.get("final_category"), 32) or _final_category(action)
    if final_category not in {"recommend_buy", "observe", "do_not_recommend"}:
        raise ValueError("AI final_category is invalid")
    recommendation = _text(review.get("final_recommendation"), 32)
    if recommendation not in {"recommend_buy", "do_not_recommend_buy"}:
        recommendation = "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy"
    label = _text(review.get("recommendation_label"), 64)
    if not label:
        label = "建议买" if recommendation == "recommend_buy" else "观察" if action == "watchlist" else "不建议"
    return {
        "verdict": _text(review.get("verdict"), 32),
        "recommended_action": _text(review.get("recommended_action"), 32),
        "buy_attractiveness_score": float(review["buy_attractiveness_score"]),
        "ai_action": action,
        "final_category": final_category,
        "final_recommendation": recommendation,
        "recommendation_label": label,
        "ai_independent": bool(review.get("ai_independent", False)),
        "confidence": _text(review.get("confidence"), 16),
        "summary": _text(review.get("summary"), 1200),
        "key_strengths": [_text(item, 240) for item in review.get("key_strengths", [])[:8]],
        "risk_flags": [_text(item, 240) for item in review.get("risk_flags", [])[:12]],
        "claims": claims[:12],
        "model": _text(review.get("model"), 120),
        "effort": _text(review.get("effort"), 32),
        "web_search_performed": web_search_performed,
        "web_search_event_verified": review.get("web_search_event_verified") is True,
        "web_search_claim_urls_verified": review.get("web_search_claim_urls_verified") is True,
        "web_search_query_count": len(review.get("web_search_queries") or []),
        "web_search_verified_claim_url_count": len(review.get("web_search_verified_claim_urls") or []),
        "web_search_dropped_claim_url_count": int(review.get("web_search_dropped_claim_url_count", 0) or 0),
        "web_search_verified": web_search_verified,
        "freshness_status": _text(review.get("freshness_status"), 32) or "undated",
        "freshness_years": [int(year) for year in review.get("freshness_years", [])[:12]],
        "freshness_penalty": float(review.get("freshness_penalty", 0.0) or 0.0),
        "freshness_note": _text(review.get("freshness_note"), 180),
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


def _type_key_sort(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"type([1-7])", value)
    return (int(match.group(1)) if match else 99, value)


def _deduplicate_company_packets(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose one best AI opinion per company while retaining type coverage."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["security_code"], []).append(record)
    public_packets: list[dict[str, Any]] = []
    for code, candidates in grouped.items():
        winner = min(
            candidates,
            key=lambda record: (
                _ACTION_PRIORITY.get(record["ai_review"]["ai_action"], 9),
                -float(record["ai_review"]["buy_attractiveness_score"]),
                -int(bool(record["ai_review"].get("web_search_verified"))),
                _FRESHNESS_PRIORITY.get(record["ai_review"].get("freshness_status"), 9),
                record["type_key"],
            ),
        )
        type_keys = sorted((record["type_key"] for record in candidates), key=_type_key_sort)
        public_packets.append(
            {
                "security_code": code,
                "name": winner["name"],
                "type_key": winner["type_key"],
                "type_keys": type_keys,
                "type_pair_count": len(type_keys),
                "deterministic": winner["deterministic"],
                "ai_review": winner["ai_review"],
            }
        )
    return public_packets


def build_artifact(
    merged_path: Path,
    output_path: Path,
    *,
    expected_generation: str,
    expected_market_as_of: str,
    source_audit_path: Path | None = None,
) -> dict[str, Any]:
    merged_bytes = merged_path.read_bytes()
    merged_sha256 = hashlib.sha256(merged_bytes).hexdigest()
    source = json.loads(merged_bytes.decode("utf-8"))
    if not isinstance(source, dict):
        raise ValueError(f"expected JSON object: {merged_path}")
    generation = str(source.get("snapshot_generation") or "")
    market_as_of = str(source.get("market_as_of") or "")
    if generation != expected_generation:
        raise ValueError(f"generation mismatch: {generation!r} != {expected_generation!r}")
    if market_as_of != expected_market_as_of:
        raise ValueError(f"market_as_of mismatch: {market_as_of!r} != {expected_market_as_of!r}")
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("merged AI screening packets are missing")
    candidate_offset = int(source.get("candidate_offset", 0) or 0)
    source_candidate_count = int(source.get("candidate_count", len(packets)) or 0)
    source_candidate_total = int(source.get("candidate_total", len(packets)) or 0)
    identity_digest = candidate_identity_sha256(packet for packet in packets if isinstance(packet, Mapping))
    declared_identity_digest = str(source.get("candidate_identity_sha256") or "")
    universe_identity_digest = str(source.get("candidate_universe_identity_sha256") or "")
    if declared_identity_digest and declared_identity_digest != identity_digest:
        raise ValueError("candidate identity hash does not match the publication queue")
    company_records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    pair_verdicts: Counter[str] = Counter()
    pair_attempted_review_count = 0
    pair_unreviewed_candidate_count = 0
    pair_attempted_needs_review_count = 0
    pair_web_search_attempted_count = 0
    pair_web_search_completed_count = 0
    pair_web_search_event_verified_count = 0
    pair_web_search_claim_urls_verified_count = 0
    pair_web_search_dropped_claim_url_count = 0
    review_models: set[str] = set()
    review_efforts: set[str] = set()
    full_coverage = source.get("full_coverage_final_recommendation") is True
    review_mode = _text(source.get("review_mode"), 64) or "external_ai_review"
    if full_coverage and not (
        candidate_offset == 0
        and source_candidate_count == source_candidate_total == len(packets)
        and declared_identity_digest == identity_digest == universe_identity_digest
    ):
        raise ValueError("full-coverage artifact does not contain the complete candidate queue")
    if full_coverage and review_mode not in PARTIAL_SEARCH_REVIEW_MODES and source_audit_path is None:
        raise ValueError("external full-coverage artifact requires a bound source audit")
    if full_coverage and review_mode == "opencode_mixed_review" and source_audit_path is None:
        raise ValueError("mixed full-coverage artifact requires a bound source audit")
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
        # A prior public artifact is also a valid re-publish source.  Its
        # compact reviews intentionally omit the identity envelope; restore
        # that envelope from the packet before applying the same validator.
        review_for_validation = dict(review)
        review_for_validation.setdefault("schema_version", REVIEW_SCHEMA_VERSION)
        review_for_validation.setdefault("security_code", code)
        review_for_validation.setdefault("type_key", type_key)
        public_review = _public_review(review_for_validation, require_readable_reason=full_coverage)
        if full_coverage and public_review["ai_action"] == "insufficient_evidence":
            raise ValueError(f"full-coverage candidate has no final decision: {key}")
        pair_verdicts[public_review["verdict"]] += 1
        if public_review["model"] == PLACEHOLDER_REVIEW_MODEL:
            pair_unreviewed_candidate_count += 1
        else:
            pair_attempted_review_count += 1
            if public_review["model"]:
                review_models.add(public_review["model"])
            if public_review["effort"]:
                review_efforts.add(public_review["effort"])
            if public_review["web_search_performed"]:
                pair_web_search_attempted_count += 1
            if public_review["web_search_event_verified"]:
                pair_web_search_event_verified_count += 1
            if public_review["web_search_claim_urls_verified"]:
                pair_web_search_claim_urls_verified_count += 1
            pair_web_search_dropped_claim_url_count += public_review["web_search_dropped_claim_url_count"]
            if _public_web_verified(public_review):
                pair_web_search_completed_count += 1
            if public_review["verdict"] == "needs_review":
                pair_attempted_needs_review_count += 1
        company_records.append(
            {
                "security_code": code,
                "name": _text(packet.get("name"), 160),
                "type_key": type_key,
                "deterministic": _public_deterministic(packet),
                "ai_review": public_review,
            }
        )
    if full_coverage and pair_unreviewed_candidate_count:
        raise ValueError("full-coverage artifact contains placeholder reviews")
    if full_coverage and (not review_models or not review_efforts):
        raise ValueError("full-coverage artifact must declare its review models and efforts")
    if full_coverage and review_mode == "local_codex_review" and review_models != {LOCAL_REVIEW_MODEL}:
        raise ValueError("local full-coverage artifact must use the local Codex review model")
    if (
        full_coverage
        and review_mode not in PARTIAL_SEARCH_REVIEW_MODES
        and pair_web_search_attempted_count != len(packets)
    ):
        raise ValueError("external full-coverage artifact must search every company/type pair")
    if (
        full_coverage
        and review_mode not in PARTIAL_SEARCH_REVIEW_MODES
        and (
            pair_web_search_event_verified_count != len(packets)
            or pair_web_search_claim_urls_verified_count != len(packets)
        )
    ):
        raise ValueError("external full-coverage artifact must retain OpenCode event and claim-URL proof")
    public_packets = _deduplicate_company_packets(company_records)
    published_pair_total = sum(int(packet["type_pair_count"]) for packet in public_packets)
    if published_pair_total != len(company_records):
        raise ValueError("public company/type-pair coverage is inconsistent")
    if len({packet["security_code"] for packet in public_packets}) != len(public_packets):
        raise ValueError("public AI screening contains duplicate companies")
    verdicts: Counter[str] = Counter()
    attempted_review_count = 0
    unreviewed_candidate_count = 0
    attempted_needs_review_count = 0
    web_search_attempted_count = 0
    web_search_completed_count = 0
    web_search_event_verified_count = 0
    web_search_dropped_claim_url_count = 0
    web_search_claim_urls_verified_count = 0
    action_counts: Counter[str] = Counter()
    final_category_counts: Counter[str] = Counter()
    freshness_counts: Counter[str] = Counter()
    for packet in public_packets:
        review = packet["ai_review"]
        verdicts[review["verdict"]] += 1
        action_counts[review["ai_action"]] += 1
        final_category_counts[review["final_category"]] += 1
        freshness_counts[str(review.get("freshness_status") or "undated")] += 1
        if review["model"] == PLACEHOLDER_REVIEW_MODEL:
            unreviewed_candidate_count += 1
        else:
            attempted_review_count += 1
            if review["web_search_performed"]:
                web_search_attempted_count += 1
            if review["web_search_event_verified"]:
                web_search_event_verified_count += 1
            web_search_dropped_claim_url_count += review["web_search_dropped_claim_url_count"]
            if review["web_search_claim_urls_verified"]:
                web_search_claim_urls_verified_count += 1
            if _public_web_verified(review):
                web_search_completed_count += 1
            if review["verdict"] == "needs_review":
                attempted_needs_review_count += 1
    action_priority = _ACTION_PRIORITY
    public_packets.sort(
        key=lambda value: (
            action_priority.get(value["ai_review"]["ai_action"], 9),
            -float(value["ai_review"]["buy_attractiveness_score"]),
            value["security_code"],
            value["type_key"],
        )
    )
    for rank, packet in enumerate(public_packets, 1):
        packet["ai_rank"] = rank
    source_audit: dict[str, Any] = {"available": False}
    if source_audit_path:
        source_audit = _validated_source_audit(
            source_audit_path,
            merged_sha256=merged_sha256,
            generation=generation,
            market_as_of=market_as_of,
        )
    source_audit["web_search_completed"] = web_search_completed_count
    source_audit["web_search_attempted"] = web_search_attempted_count
    source_audit["web_search_event_verified"] = web_search_event_verified_count
    source_audit["web_search_claim_urls_verified"] = web_search_claim_urls_verified_count
    source_audit["web_source_verified"] = web_search_completed_count
    source_audit["reviewed_without_web_search"] = max(0, attempted_review_count - web_search_attempted_count)
    source_audit["type_pair_web_search_completed"] = pair_web_search_completed_count
    source_audit["type_pair_web_search_attempted"] = pair_web_search_attempted_count
    source_audit["type_pair_web_search_event_verified"] = pair_web_search_event_verified_count
    source_audit["type_pair_web_search_claim_urls_verified"] = pair_web_search_claim_urls_verified_count
    source_audit["type_pair_web_search_dropped_claim_urls"] = pair_web_search_dropped_claim_url_count
    rule_file_count = source.get("rule_file_count")
    rule_source_sha256 = source.get("rule_source_sha256")
    rules_root = _text(source.get("rules_root"), 240)
    if rule_file_count is not None and (not isinstance(rule_file_count, int) or rule_file_count < 1):
        raise ValueError("AI screening knowledge-base file count is invalid")
    if rule_source_sha256 is not None:
        if (
            not isinstance(rule_file_count, int)
            or not isinstance(rule_source_sha256, Mapping)
            or len(rule_source_sha256) != rule_file_count
        ):
            raise ValueError("AI screening knowledge-base manifest is incomplete")
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "ai_is_advisory": True,
        "auto_buy_promotion": False,
        "full_coverage_final_recommendation": full_coverage,
        "review_mode": review_mode,
        "review_models": sorted(review_models),
        "review_efforts": sorted(review_efforts),
        "full_coverage_web_search": bool(
            full_coverage
            and unreviewed_candidate_count == 0
            and attempted_review_count == len(public_packets)
            and web_search_attempted_count == len(public_packets)
            and web_search_event_verified_count == len(public_packets)
            and web_search_claim_urls_verified_count == len(public_packets)
        ),
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        "methodology_version": source.get("methodology_version"),
        "index_contract": source.get("index_contract"),
        # The AI reviews each company/type pair, but the public ranking is a
        # company list.  Keep both counts explicit so one company cannot be
        # shown several times while the audit still proves every pair ran.
        "candidate_total": len(public_packets),
        "candidate_identity_sha256": declared_identity_digest,
        "candidate_universe_identity_sha256": universe_identity_digest,
        "company_deduplication": "best_action_then_score",
        "type_pair_candidate_total": published_pair_total,
        "type_pair_expected_total": source_candidate_total,
        "type_pair_unique_company_count": len(public_packets),
        "type_pair_reviewed_count": pair_attempted_review_count,
        "type_pair_unreviewed_count": pair_unreviewed_candidate_count,
        "type_pair_needs_review_count": pair_attempted_needs_review_count,
        "type_pair_verdict_counts": dict(sorted(pair_verdicts.items())),
        "type_pair_web_search_attempted_count": pair_web_search_attempted_count,
        "type_pair_web_search_completed_count": pair_web_search_completed_count,
        "type_pair_web_search_event_verified_count": pair_web_search_event_verified_count,
        "type_pair_web_search_claim_urls_verified_count": pair_web_search_claim_urls_verified_count,
        "type_pair_web_search_dropped_claim_url_count": pair_web_search_dropped_claim_url_count,
        "candidate_offset": candidate_offset,
        "reviewed_count": len(public_packets),
        "attempted_review_count": attempted_review_count,
        "unreviewed_candidate_count": unreviewed_candidate_count,
        "attempted_needs_review_count": attempted_needs_review_count,
        "web_search_attempted_count": web_search_attempted_count,
        "web_search_event_verified_count": web_search_event_verified_count,
        "web_search_dropped_claim_url_count": web_search_dropped_claim_url_count,
        "web_search_claim_urls_verified_count": web_search_claim_urls_verified_count,
        "web_source_verified_count": web_search_completed_count,
        "web_search_completed_count": web_search_completed_count,
        "reviewed_without_web_search": max(0, attempted_review_count - web_search_attempted_count),
        # Keep the old field names as compatibility aliases.  They now mean
        # attempted versus not-yet-started, rather than verdict quality.
        "completed_review_count": attempted_review_count,
        "pending_review_count": unreviewed_candidate_count,
        "verdict_counts": dict(sorted(verdicts.items())),
        "ai_action_counts": {key: action_counts[key] for key in _PUBLIC_ACTIONS},
        "final_category_counts": {key: final_category_counts[key] for key in _PUBLIC_CATEGORIES},
        "priority_buy_count": action_counts["priority_buy"],
        "recommend_buy_count": action_counts["priority_buy"],
        "watchlist_count": action_counts["watchlist"],
        "avoid_count": action_counts["avoid"],
        "do_not_recommend_buy_count": action_counts["watchlist"]
        + action_counts["avoid"]
        + action_counts["insufficient_evidence"],
        "insufficient_evidence_count": action_counts["insufficient_evidence"],
        "ranking_version": RANKING_VERSION,
        "freshness_counts": dict(sorted(freshness_counts.items())),
        "ranking_source": _text(source.get("ranking_source"), 120),
        "source_audit": source_audit,
        "packets": public_packets,
    }
    if isinstance(rule_file_count, int) and isinstance(rule_source_sha256, Mapping):
        artifact["rules_root"] = rules_root
        artifact["rule_file_count"] = rule_file_count
        artifact["rule_source_sha256"] = dict(
            sorted((str(key), str(value)) for key, value in rule_source_sha256.items())
        )
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
