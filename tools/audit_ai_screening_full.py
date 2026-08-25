"""Run a row-by-row audit of every published AI screening company.

This is a release audit, not a random sample.  It emits one result for every
company and fails the release summary if any identity, snapshot, score/action,
financial-gate, or explicit cross-company identity check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_identity import explicit_company_codes


_ACTIONS = {"priority_buy", "watchlist", "avoid", "insufficient_evidence"}
_CATEGORIES = {"recommend_buy", "observe", "do_not_recommend"}
_VERDICTS = {"confirmed", "caution", "misclassified", "missed_candidate", "needs_review"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _texts(review: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("summary", "key_strengths", "risk_flags", "quantitative_facts"):
        value = review.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    for claim in review.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        for field in ("statement", "source_context"):
            if isinstance(claim.get(field), str):
                values.append(claim[field])
    return values


def _audit_packet(
    packet: Mapping[str, Any],
    *,
    generation: str,
    market_as_of: str,
) -> dict[str, Any]:
    code = str(packet.get("security_code") or "")
    name = str(packet.get("name") or "")
    review = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    errors: list[str] = []
    identity_ok = bool(code and len(code) == 6 and code.isdigit() and name.strip())
    if not identity_ok:
        errors.append("packet_identity")
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    if context:
        if str(context.get("code") or "") != code:
            errors.append("context_code")
        if str(context.get("name") or "").strip() != name.strip():
            errors.append("context_name")
    for field, expected in (
        ("generation", generation),
        ("snapshot_generation", generation),
        ("market_as_of", market_as_of),
    ):
        if field in packet and str(packet.get(field) or "") != expected:
            errors.append(field)
    if str(review.get("security_code") or code) != code:
        errors.append("review_code")
    action = str(review.get("ai_action") or "")
    category = str(review.get("final_category") or "")
    score = _number(review.get("buy_attractiveness_score"))
    verdict = str(review.get("verdict") or "")
    score_ok = score is not None and 0 <= score <= 100
    if action not in _ACTIONS:
        errors.append("action")
    if category not in _CATEGORIES:
        errors.append("category")
    if verdict not in _VERDICTS:
        errors.append("verdict")
    expected_category = {
        "priority_buy": "recommend_buy",
        "watchlist": "observe",
        "avoid": "do_not_recommend",
        "insufficient_evidence": "observe",
    }.get(action)
    if expected_category and category != expected_category:
        errors.append("category_action")
    if action == "priority_buy" and (score is None or score < 70):
        errors.append("buy_score_band")
    if action == "watchlist" and (score is None or not 50 <= score < 70):
        errors.append("observe_score_band")
    if action in {"avoid", "insufficient_evidence"} and (score is None or score >= 50):
        errors.append("negative_score_band")
    if not isinstance(review.get("summary"), str) or len(review["summary"].strip()) < 8:
        errors.append("summary")
    if action in {"priority_buy", "watchlist"} and not any(
        isinstance(value, str) and value.strip() for value in review.get("key_strengths") or []
    ):
        errors.append("strengths")
    if action in {"priority_buy", "watchlist", "avoid"} and not any(
        isinstance(value, str) and value.strip() for value in review.get("risk_flags") or []
    ):
        errors.append("risks")
    adjustments = (
        review.get("calibration_adjustments") if isinstance(review.get("calibration_adjustments"), Mapping) else {}
    )
    adjusted_score = _number(adjustments.get("final_score"))
    if score is None or adjusted_score is None or abs(score - adjusted_score) > 0.11:
        errors.append("score_adjustment")
    quality = review.get("quality_gate") if isinstance(review.get("quality_gate"), Mapping) else {}
    hard_block = quality.get("hard_block") is True or adjustments.get("quality_hard_block") is True
    if action == "priority_buy" and hard_block:
        errors.append("buy_hard_block")
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), Mapping) else {}
    for field in ("pe", "pb"):
        context_value = _number(context.get(field))
        metric_value = _number(metrics.get(field))
        if context_value is not None and metric_value is not None and abs(context_value - metric_value) > 0.02:
            errors.append(f"{field}_mismatch")
    cross_company_codes = sorted(
        {other for text in _texts(review) for other in explicit_company_codes(text) if other != code}
    )
    if cross_company_codes:
        errors.append("cross_company_identity")
    freshness = str(review.get("freshness_status") or "")
    if freshness not in {"current_or_recent", "historical", "undated"}:
        errors.append("freshness")
    return {
        "security_code": code,
        "name": name,
        "type_key": str(packet.get("type_key") or ""),
        "action": action,
        "final_category": category,
        "score": score,
        "verdict": verdict,
        "quality_hard_block": hard_block,
        "cross_company_codes": cross_company_codes,
        "identity_ok": identity_ok,
        "score_ok": score_ok and not any(item.endswith("_score_band") for item in errors),
        "financial_facts_ok": not any(item.endswith("_mismatch") for item in errors),
        "review_ok": not errors,
        "errors": errors,
    }


def audit_artifact(
    artifact_path: Path,
    *,
    expected_generation: str | None = None,
    expected_market_as_of: str | None = None,
    expected_count: int | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    generation = str(artifact.get("snapshot_generation") or "")
    market_as_of = str(artifact.get("market_as_of") or "")
    if expected_generation and generation != expected_generation:
        raise ValueError(f"generation mismatch: {generation!r} != {expected_generation!r}")
    if expected_market_as_of and market_as_of != expected_market_as_of:
        raise ValueError(f"market_as_of mismatch: {market_as_of!r} != {expected_market_as_of!r}")
    packets = artifact.get("packets")
    if not isinstance(packets, list):
        raise ValueError("artifact packets are missing")
    rows = [_audit_packet(packet, generation=generation, market_as_of=market_as_of) for packet in packets]
    if expected_count is not None and len(rows) != expected_count:
        raise ValueError(f"packet count mismatch: {len(rows)} != {expected_count}")
    codes = [row["security_code"] for row in rows]
    duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
    for row in rows:
        if row["security_code"] in duplicate_codes:
            row["errors"].append("duplicate_company")
            row["review_ok"] = False
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    issue_count = sum(1 for row in rows if not row["review_ok"])
    result = {
        "artifact": str(artifact_path),
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        "review_count": len(rows),
        "issue_count": issue_count,
        "identity_pass_count": sum(row["identity_ok"] for row in rows),
        "score_pass_count": sum(row["score_ok"] for row in rows),
        "financial_fact_pass_count": sum(row["financial_facts_ok"] for row in rows),
        "action_counts": {
            action: sum(row["action"] == action for row in rows)
            for action in ("priority_buy", "watchlist", "avoid", "insufficient_evidence")
        },
        "cross_company_issue_count": sum("cross_company_identity" in row["errors"] for row in rows),
        "audit_sha256": digest,
        "rows": rows,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--expected-generation")
    parser.add_argument("--expected-market-as-of")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_artifact(
        args.artifact,
        expected_generation=args.expected_generation,
        expected_market_as_of=args.expected_market_as_of,
        expected_count=args.expected_count,
        output_path=args.output,
    )
    print(
        json.dumps({key: value for key, value in result.items() if key != "rows"}, ensure_ascii=False, sort_keys=True)
    )
    return 0 if result["issue_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
