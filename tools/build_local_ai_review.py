"""Create a transparent local second-pass review from a validated snapshot.

This is deliberately conservative: it never invents company facts or URLs.
It reviews every deterministic candidate pair, keeps the deterministic result
visible, and reserves ``priority_buy`` for high-scoring, non-vetoed candidates
without an obvious warning reason.  External web evidence can be added later
for the small priority set; it is never fabricated here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import LOCAL_REVIEW_MODEL, REVIEW_SCHEMA_VERSION


_WARNING_WORDS = ("负", "下降", "高", "不足", "缺", "不完整", "待", "风险", "否决")


def _buy_score_for_action(raw_score: float, action: str) -> float:
    """Keep the displayed score meaningful as a *buy* attractiveness score.

    The deterministic rule upper bound is not an AI buy recommendation.  A
    veto, unresolved evidence, or a cautionary result must not inherit a
    100-point rule upper bound and appear above an actual buy candidate.
    """
    raw = max(0.0, min(100.0, raw_score))
    if action == "priority_buy":
        return raw
    if action == "watchlist":
        return min(69.0, raw * 0.70)
    if action == "avoid":
        return min(49.0, raw * 0.40)
    return min(49.0, raw * 0.30)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _review(packet: Mapping[str, Any], overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    deterministic = packet.get("deterministic") if isinstance(packet.get("deterministic"), Mapping) else {}
    decision = deterministic.get("decision") if isinstance(deterministic.get("decision"), Mapping) else {}
    status = str(deterministic.get("status") or "unknown")
    type_key = str(packet.get("type_key") or "")
    score = _number(deterministic.get("score"))
    upper = _number(deterministic.get("score_upper_bound"))
    if upper is None:
        upper = _number(decision.get("score_upper_bound"))
    veto = str(deterministic.get("veto_state") or decision.get("veto_state") or "none")
    reason = str(deterministic.get("reason") or "")
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    code = str(packet.get("security_code") or "")
    name = str(packet.get("name") or context.get("name") or code)
    pe = _number(context.get("pe"))
    warning = any(word in reason for word in _WARNING_WORDS)

    # A priority opinion is intentionally narrower than a deterministic trigger.
    # Cyclical type5 names need a higher score and a non-extreme current PE.
    priority = (
        status == "triggered"
        and veto == "none"
        and score is not None
        and (
            (type_key == "type5" and score >= 8.0 and (pe is None or pe <= 35))
            or (type_key != "type5" and score >= 7.6)
        )
        and not warning
    )
    near = (
        status in {"conditional", "observe", "pending", "insufficient_evidence"}
        and upper is not None
        and upper >= 7
        and (score is None or score >= 6.0)
        and veto == "none"
    )
    if veto != "none" or status == "vetoed":
        action, verdict, label, rec_action = "avoid", "confirmed", "不建议", "demote"
    elif priority:
        action, verdict, label, rec_action = "priority_buy", "confirmed", "建议买", "keep"
    elif near or status == "triggered":
        action, verdict, label, rec_action = "watchlist", "caution", "观察", "manual_review"
    else:
        action, verdict, label, rec_action = "avoid", "confirmed", "不建议", "demote"

    rule_score = score if score is not None else upper if upper is not None else 0.0
    raw_buy_score = max(0.0, min(100.0, 50.0 + (rule_score - 5.0) * 10.0))
    score_value = _buy_score_for_action(raw_buy_score, action)
    strengths = [f"{type_key}确定性状态为{status}"]
    if score is not None:
        strengths.append(f"确定性评分{score:.2f}")
    risks = ["本轮结论基于今日快照与本地模板规则，未把外部网页事实当作已核验事实"]
    if reason:
        risks.insert(0, reason)
    summary = (
        f"{name}（{code}）的{type_key}确定性状态为{status}，"
        f"规则分数为{rule_score:.2f}。"
        f"本地二次复核给出“{label}”：保留规则结果，不新增未经核实的公司事实。"
    )
    result = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": code,
        "type_key": type_key,
        "verdict": verdict,
        "recommended_action": rec_action,
        "buy_attractiveness_score": round(score_value, 1),
        "ai_action": action,
        "final_category": "recommend_buy"
        if action == "priority_buy"
        else "observe"
        if action == "watchlist"
        else "do_not_recommend",
        "final_recommendation": "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy",
        "recommendation_label": label,
        "ai_independent": False,
        "confidence": "medium" if priority else "low",
        "summary": summary,
        "key_strengths": strengths[:3],
        "risk_flags": risks[:4],
        "claims": [],
        "model": LOCAL_REVIEW_MODEL,
        "effort": "max",
        "web_search_performed": False,
        "web_search_verified": False,
        "freshness_status": "current_or_recent",
        "freshness_years": [2026],
        "freshness_penalty": 0.0,
        "freshness_note": "基于 2026-08-20 已验证闭市快照；未把网页发布时间当作报告期。",
    }
    override = None
    if isinstance(overrides, Mapping):
        override = overrides.get(f"{code}:{type_key}") or overrides.get(code)
    override_score_supplied = isinstance(override, Mapping) and "buy_attractiveness_score" in override
    if isinstance(override, Mapping):
        for key in (
            "verdict",
            "recommended_action",
            "buy_attractiveness_score",
            "ai_action",
            "final_category",
            "final_recommendation",
            "recommendation_label",
            "ai_independent",
            "confidence",
            "summary",
            "key_strengths",
            "risk_flags",
            "claims",
            "web_search_performed",
            "web_search_verified",
            "freshness_status",
            "freshness_years",
            "freshness_penalty",
            "freshness_note",
        ):
            if key in override:
                result[key] = override[key]
    # Overrides are allowed to add human-reviewed facts, but cannot create a
    # contradictory score/category pair.  Re-apply the same score band after
    # an override so a stale manual score can never publish as 100 + 不建议.
    action = str(result.get("ai_action") or "avoid")
    expected_category = {
        "priority_buy": "recommend_buy",
        "watchlist": "observe",
        "avoid": "do_not_recommend",
        "insufficient_evidence": "observe",
    }.get(action)
    if expected_category is None:
        raise ValueError(f"unsupported local AI action: {action}")
    if result.get("final_category") not in (None, "", expected_category):
        raise ValueError(f"local AI action/category mismatch: {code}:{type_key}")
    result["final_category"] = expected_category
    expected_recommendation = "recommend_buy" if expected_category == "recommend_buy" else "do_not_recommend_buy"
    if result.get("final_recommendation") not in (None, "", expected_recommendation):
        raise ValueError(f"local AI recommendation mismatch: {code}:{type_key}")
    result["final_recommendation"] = expected_recommendation
    score_number = _number(result.get("buy_attractiveness_score"))
    if score_number is None:
        raise ValueError(f"local AI score is invalid: {code}:{type_key}")
    if override_score_supplied:
        result["buy_attractiveness_score"] = round(_buy_score_for_action(score_number, action), 1)
    else:
        result["buy_attractiveness_score"] = round(score_number, 1)
    return result


def build(input_path: Path, output_path: Path, overrides_path: Path | None = None) -> dict[str, int]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("input packets are missing")
    overrides: Mapping[str, Any] = {}
    if overrides_path:
        raw_overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        if not isinstance(raw_overrides, Mapping):
            raise ValueError("overrides must be an object")
        overrides = raw_overrides
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("packet is not an object")
        key = (str(packet.get("security_code") or ""), str(packet.get("type_key") or ""))
        if key in seen:
            raise ValueError(f"duplicate packet: {key}")
        seen.add(key)
        output.append(_review(packet, overrides))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in output), encoding="utf-8"
    )
    return {"candidate_count": len(output), "priority_buy": sum(item["ai_action"] == "priority_buy" for item in output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overrides", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.input, args.output, args.overrides), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
