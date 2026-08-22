"""Apply an auditable human review pass to a generation-bound AI queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def apply(source_path: Path, corrections_path: Path, output_path: Path) -> dict[str, int]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    corrections_payload = json.loads(corrections_path.read_text(encoding="utf-8"))
    if source.get("snapshot_generation") != corrections_payload.get("snapshot_generation"):
        raise ValueError("human review generation does not match the source queue")
    if source.get("market_as_of") != corrections_payload.get("market_as_of"):
        raise ValueError("human review market_as_of does not match the source queue")
    corrections = corrections_payload.get("corrections")
    if not isinstance(corrections, Mapping):
        raise ValueError("human review corrections are missing")
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("source packets are missing")
    changed = 0
    changed_codes: set[str] = set()
    for packet in packets:
        if not isinstance(packet, dict):
            raise ValueError("source packet is not an object")
        code = str(packet.get("security_code") or "")
        correction = corrections.get(code)
        review = packet.get("ai_review")
        if not isinstance(correction, Mapping) or not isinstance(review, dict):
            continue
        if review.get("ai_action") != "priority_buy":
            continue
        action = str(correction.get("action") or "")
        if action not in {"watchlist", "avoid"}:
            raise ValueError(f"unsupported human review action for {code}: {action}")
        score = float(correction.get("score"))
        review["buy_attractiveness_score"] = min(69.0, score) if action == "watchlist" else min(49.0, score)
        review["ai_action"] = action
        review["verdict"] = "caution" if action == "watchlist" else "misclassified"
        review["recommended_action"] = "manual_review" if action == "watchlist" else "demote"
        review["final_category"] = "observe" if action == "watchlist" else "do_not_recommend"
        review["final_recommendation"] = "do_not_recommend_buy"
        review["recommendation_label"] = "观察" if action == "watchlist" else "不建议"
        review["ai_independent"] = False
        reason = str(correction.get("reason") or "人工复核后降级")
        review["summary"] = f"人工复核结论：{reason} 原AI复核摘要：{str(review.get('summary') or '')}"
        risk_flags = review.get("risk_flags") if isinstance(review.get("risk_flags"), list) else []
        review["risk_flags"] = [reason, *[str(item) for item in risk_flags if str(item).strip()]][:12]
        changed += 1
        changed_codes.add(code)
    missing = sorted(set(str(code) for code in corrections) - changed_codes)
    if missing:
        raise ValueError(f"human review corrections did not match priority-buy packets: {missing}")
    output_path.write_text(json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"changed_packets": changed, "changed_companies": len(changed_codes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(apply(args.source, args.corrections, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
