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


def _calibrated_score(packet: Mapping[str, Any], verdict: str) -> float:
    base = _deterministic_score(packet)
    status = _deterministic_status(packet)
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
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
    return round(score, 1)


def _review(packet: Mapping[str, Any]) -> dict[str, Any]:
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    verdict = str(source.get("verdict") or "needs_review")
    status = _deterministic_status(packet)
    score = _calibrated_score(packet, verdict)
    if verdict == "confirmed" and status == "triggered" and score >= 70:
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
    elif verdict == "misclassified":
        action = "avoid"
    else:
        action = "insufficient_evidence"
    confidence = {"confirmed": "medium", "caution": "medium", "missed_candidate": "low"}.get(verdict, "low")
    claims = source.get("claims") if isinstance(source.get("claims"), list) else []
    strengths = [
        str(claim.get("statement") or "")[:240]
        for claim in claims
        if isinstance(claim, Mapping) and claim.get("support") == "supports"
    ][:4]
    risk_flags = [str(value)[:240] for value in (source.get("risk_flags") or []) if str(value).strip()][:8]
    if not risk_flags:
        risk_flags = ["当前排序沿用已完成的 AI 复核摘要，尚未对所有候选重新发起外部检索"]
    summary = str(source.get("summary") or "AI 已完成第一轮候选复核。")[:1200]
    summary = f"AI买入吸引力 {score:.1f} 分（由已完成的 OpenCode 复核结论校准排序）。{summary}"
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": str(packet.get("security_code") or ""),
        "type_key": str(packet.get("type_key") or ""),
        "verdict": verdict
        if verdict in {"confirmed", "caution", "misclassified", "missed_candidate", "needs_review"}
        else "needs_review",
        "recommended_action": str(source.get("recommended_action") or "manual_review"),
        "buy_attractiveness_score": score,
        "ai_action": action,
        "confidence": confidence,
        "summary": summary,
        "key_strengths": strengths,
        "risk_flags": risk_flags,
        "claims": claims[:12],
        "model": str(source.get("model") or "opencode-go/deepseek-v4-flash"),
        "effort": str(source.get("effort") or "max"),
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
        "ranking_source": "opencode-go-qualitative-review-calibrated",
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
