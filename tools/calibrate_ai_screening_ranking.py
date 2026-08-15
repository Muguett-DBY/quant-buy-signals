"""Turn the completed qualitative OpenCode review into a stable ranking artifact.

The first AI pass already contains a model-written summary, risks, and source
claims for every candidate.  This small calibration layer adds the numeric
ranking requested by the website without inventing new company facts: it uses
the model verdict plus the deterministic score/bounds only.  A later research
pass can replace these calibrated fields packet by packet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping
import re

from tools.ai_screening_contract import REVIEW_SCHEMA_VERSION, validate_review


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _deterministic_score(packet: Mapping[str, Any]) -> float:
    deterministic = packet.get("deterministic") if isinstance(packet.get("deterministic"), Mapping) else {}
    score = _number(deterministic.get("score"))
    if score is not None:
        return score
    upper = _number(deterministic.get("score_upper_bound"))
    return upper if upper is not None else 0.0


def _deterministic_status(packet: Mapping[str, Any]) -> str:
    deterministic = packet.get("deterministic") if isinstance(packet.get("deterministic"), Mapping) else {}
    return str(deterministic.get("status") or "insufficient_evidence")


def _web_search_verified(review: Mapping[str, Any]) -> bool:
    if review.get("web_search_performed") is not True:
        return False
    claims = review.get("claims") if isinstance(review.get("claims"), list) else []
    return any(_claim_url(claim).lower().startswith("https://") for claim in claims if isinstance(claim, Mapping))


def _claim_url(claim: Mapping[str, Any]) -> str:
    for field in ("source_ref", "source_context"):
        raw = str(claim.get(field) or "")
        match = re.search(r"https?://[^\s)]+", raw, re.IGNORECASE)
        if match:
            ascii_url = re.match(r"[A-Za-z0-9:/?#\[\]@!$&'()*+,;=%._~\-]+", match.group(0))
            return (ascii_url.group(0) if ascii_url else "").rstrip(".,;")
    return ""


def _final_category(action: str) -> str:
    """Collapse the internal four-state action into the three user outcomes."""
    if action == "priority_buy":
        return "recommend_buy"
    if action in {"watchlist", "insufficient_evidence"}:
        return "observe"
    return "do_not_recommend"


def _calibrated_score(packet: Mapping[str, Any], verdict: str) -> float:
    base = _deterministic_score(packet)
    status = _deterministic_status(packet)
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    web_verified = _web_search_verified(source)
    model_score = _number(source.get("buy_attractiveness_score"))
    if web_verified and model_score is not None:
        # A source-backed second pass owns the ranking score.  The fallback
        # formula below is only for legacy/unsearched reviews and is never
        # allowed to manufacture a priority signal.
        if str(source.get("ai_action") or "") == "avoid" or verdict == "misclassified":
            return round(max(0.0, min(49.0, model_score)), 1)
        return round(max(0.0, min(100.0, model_score)), 1)
    risk_flags = [str(value) for value in (source.get("risk_flags") or [])]
    risk_text = " ".join(risk_flags)
    penalty = min(
        16.0,
        len(risk_flags) * 1.5
        + sum(term in risk_text for term in ("现金流", "审计", "应收", "商誉", "诉讼", "周期")) * 2.0,
    )
    if verdict == "confirmed":
        score = max(70.0, min(99.0, 65.0 + base * 3.5 - penalty))
    elif verdict == "caution":
        score = max(50.0, min(76.0, 44.0 + base * 3.1 - penalty))
    elif verdict == "missed_candidate":
        score = max(50.0, min(69.0, 48.0 + base * 2.7 - penalty))
    elif verdict == "misclassified":
        score = max(8.0, 30.0 - base * 0.8 - penalty)
    else:
        score = max(20.0, min(58.0, 30.0 + base * 2.4 - penalty))

    # A qualitative confirmation cannot turn an unresolved or merely
    # conditional deterministic result into a buy signal.  Keep the score
    # useful for ranking, but make its ceiling match the decision state.
    if status in {"observe", "conditional"}:
        score = min(score, 78.0)
    elif status != "triggered":
        score = min(score, 64.0)
    if not web_verified:
        # A model-only legacy review is useful for a conservative watch/avoid
        # decision, but it must not outrank source-backed recommendations.
        score = min(score, 64.0)
    return round(score, 1)


def _review(packet: Mapping[str, Any]) -> dict[str, Any]:
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    verdict = str(source.get("verdict") or "needs_review")
    status = _deterministic_status(packet)
    score = _calibrated_score(packet, verdict)
    web_verified = _web_search_verified(source)
    source_action = str(source.get("ai_action") or "")
    if source_action == "avoid" or verdict == "misclassified":
        action = "avoid"
    elif verdict == "confirmed" and status == "triggered" and score >= 70 and web_verified:
        action = "priority_buy"
    elif (
        verdict in {"confirmed", "caution", "missed_candidate"}
        and status
        in {
            "triggered",
            "observe",
            "conditional",
        }
        and score >= 50
    ):
        action = "watchlist"
    else:
        # Every candidate gets a final conservative decision. Missing web
        # provenance is not a third outcome that leaves the user without an
        # answer: attractive but unverified packets stay on the watchlist,
        # while weak or contradictory packets are marked avoid.
        action = "watchlist" if score >= 50 else "avoid"
    if verdict == "confirmed" and status == "triggered" and not web_verified:
        action = "watchlist"
    confidence = {"confirmed": "medium", "caution": "medium", "missed_candidate": "low"}.get(verdict, "low")
    if not web_verified:
        confidence = "low"
    raw_claims = source.get("claims") if isinstance(source.get("claims"), list) else []
    # Legacy public cards may contain empty placeholder claims.  Do not copy
    # those into the new contract: a claim without a source is not evidence.
    claims = []
    for claim in raw_claims:
        if not isinstance(claim, Mapping):
            continue
        source_ref = _claim_url(claim)
        if not source_ref:
            continue
        claims.append({**claim, "source_ref": source_ref})
    strengths = [
        str(claim.get("statement") or "")[:240]
        for claim in claims
        if isinstance(claim, Mapping) and claim.get("support") == "supports"
    ][:4]
    risk_flags = [str(value)[:240] for value in (source.get("risk_flags") or []) if str(value).strip()][:8]
    if not risk_flags:
        risk_flags = ["当前排序沿用已完成的 AI 复核摘要，尚未对所有候选重新发起外部检索"]
    # Rebuild the prefix from the current provenance state.  This also strips
    # the prefix emitted by an older calibration run, so replaying a legacy
    # seed cannot produce nested/double "AI买入吸引力" labels.
    summary = str(source.get("summary") or "AI 已完成第一轮候选复核。")
    legacy_prefixes = (
        "AI买入吸引力 ",
        "AI 买入吸引力 ",
    )
    while summary.startswith(legacy_prefixes):
        marker = summary.find("）。")
        if marker < 0:
            break
        summary = summary[marker + 2 :].lstrip()
    summary = summary[:1200]
    # Make the provenance state visible in every published card.  Legacy
    # reviews remain useful as context, but they are never presented as a
    # completed web check.
    if web_verified:
        summary = f"AI买入吸引力 {score:.1f} 分（已通过 OpenCode Go 联网资料核验）。{summary}"
    else:
        summary = f"AI买入吸引力 {score:.1f} 分（尚未完成本轮联网资料核验，不进入优先候选）。{summary}"
    no_web_reason = "\u672a\u5b8c\u6210\u672c\u8f6e\u8054\u7f51\u8d44\u6599\u6838\u9a8c\uff0c\u6682\u4e0d\u63a8\u8350\u76f4\u63a5\u4e70\u5165"
    if not web_verified and no_web_reason not in risk_flags:
        risk_flags.insert(0, no_web_reason)
    public_verdict = verdict if verdict in {"confirmed", "caution", "misclassified", "missed_candidate"} else "caution"
    recommendation = "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy"
    recommendation_label = (
        "\u63a8\u8350\u4e70\u5165\u5019\u9009"
        if recommendation == "recommend_buy"
        else "\u4e0d\u63a8\u8350\u73b0\u5728\u4e70\u5165"
    )
    # Keep the older summary text as context, but make the final decision and
    # the provenance limitation explicit to a normal reader.
    if not web_verified:
        summary = summary + " " + no_web_reason + "。"
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": str(packet.get("security_code") or ""),
        "type_key": str(packet.get("type_key") or ""),
        "verdict": public_verdict,
        "recommended_action": str(source.get("recommended_action") or "manual_review")
        if str(source.get("recommended_action") or "manual_review") in {"keep", "demote", "manual_review"}
        else "manual_review",
        "buy_attractiveness_score": score,
        "ai_action": action,
        "final_category": _final_category(action),
        "final_recommendation": recommendation,
        "recommendation_label": recommendation_label,
        "confidence": confidence,
        "summary": summary,
        "key_strengths": strengths,
        "risk_flags": risk_flags,
        "claims": claims[:12],
        "model": str(source.get("model") or "opencode-go/deepseek-v4-flash"),
        "effort": str(source.get("effort") or "max"),
        "web_search_performed": bool(source.get("web_search_performed") is True),
        "web_search_verified": web_verified,
    }


def calibrate(source_path: Path, output_path: Path) -> dict[str, int]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("source packets are missing")
    output_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("packet is not an object")
        review = _review(packet)
        if validate_review(review):
            raise ValueError(f"calibrated review is invalid: {review['security_code']}/{review['type_key']}")
        output_packets.append({**packet, "ai_review": review})
    output = {
        **source,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "ranking_source": "opencode-go-web-search-review-calibrated",
        "full_coverage_final_recommendation": True,
        "packets": output_packets,
    }
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"candidate_count": len(output_packets)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(calibrate(args.source, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
