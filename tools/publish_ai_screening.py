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

from tools.ai_screening_contract import REVIEW_SCHEMA_VERSION, validate_review

ARTIFACT_SCHEMA_VERSION = 1
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
        source_ref = match.group(0).rstrip(".,;，。；）") if match else ""
        claims.append(
            {
                "statement": _text(claim.get("statement"), 600),
                "source_ref": source_ref,
                "source_context": raw_source[:240],
            }
        )
    return {
        "verdict": _text(review.get("verdict"), 32),
        "recommended_action": _text(review.get("recommended_action"), 32),
        "summary": _text(review.get("summary"), 1200),
        "risk_flags": [_text(item, 240) for item in review.get("risk_flags", [])[:12]],
        "claims": claims[:12],
        "model": _text(review.get("model"), 120),
        "effort": _text(review.get("effort"), 32),
    }


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
            continue
        if not isinstance(review, Mapping):
            raise ValueError(f"AI review is not an object: {key}")
        public_review = _public_review(review)
        verdicts[public_review["verdict"]] += 1
        public_packets.append(
            {
                "security_code": code,
                "name": _text(packet.get("name"), 160),
                "type_key": type_key,
                "deterministic": _public_deterministic(packet),
                "ai_review": public_review,
            }
        )
    public_packets.sort(key=lambda value: (value["security_code"], value["type_key"]))
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
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "ai_is_advisory": True,
        "auto_buy_promotion": False,
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        "methodology_version": source.get("methodology_version"),
        "index_contract": source.get("index_contract"),
        "candidate_total": int(source.get("candidate_total", 0) or 0),
        "candidate_offset": int(source.get("candidate_offset", 0) or 0),
        "reviewed_count": len(public_packets),
        "verdict_counts": dict(sorted(verdicts.items())),
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
