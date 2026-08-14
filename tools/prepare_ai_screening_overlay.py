"""Merge completed local reviews into a full candidate overlay.

Every selected deterministic candidate stays visible.  Candidates without a
completed OpenCode Go review receive an explicit ``needs_review`` placeholder;
they are never silently dropped from the website.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.ai_screening_contract import (
    PLACEHOLDER_REVIEW_MODEL,
    REVIEW_SCHEMA_VERSION,
    validate_review,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _review_map(paths: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    reviews: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        for review in _read_jsonl(path):
            errors = validate_review(review)
            if errors:
                raise ValueError(f"invalid review {path}: {','.join(errors)}")
            key = (str(review.get("security_code")), str(review.get("type_key")))
            reviews[key] = review
    return reviews


def _placeholder(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": packet.get("security_code"),
        "type_key": packet.get("type_key"),
        "verdict": "needs_review",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 0,
        "ai_action": "insufficient_evidence",
        "confidence": "low",
        "summary": "尚未完成本地 OpenCode Go 复核，确定性规则结果保持不变。",
        "key_strengths": [],
        "risk_flags": ["等待官方资料与反证核验"],
        "claims": [],
        "model": PLACEHOLDER_REVIEW_MODEL,
        "effort": "max",
    }


def prepare(input_path: Path, output_path: Path, review_paths: list[Path]) -> dict[str, int]:
    source = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or not isinstance(source.get("packets"), list):
        raise ValueError("AI screening input must contain packets")
    reviews = _review_map(review_paths)
    attempted = 0
    pending = 0
    attempted_needs_review = 0
    for packet in source["packets"]:
        key = (str(packet.get("security_code")), str(packet.get("type_key")))
        review = reviews.get(key) or _placeholder(packet)
        packet["ai_review"] = review
        if review.get("model") == PLACEHOLDER_REVIEW_MODEL:
            pending += 1
        else:
            attempted += 1
            if review["verdict"] == "needs_review":
                attempted_needs_review += 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "candidate_count": len(source["packets"]),
        "completed": attempted,
        "pending": pending,
        "attempted": attempted,
        "attempted_needs_review": attempted_needs_review,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(prepare(args.input, args.output, args.reviews), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
