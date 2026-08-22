"""Attach the selected rule fragments from the local Markdown knowledge base.

This is deliberately separate from candidate selection: it enriches an
already validated input packet without changing deterministic company data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from tools.ai_screening_contract import candidate_identity_sha256
from tools.build_ai_screening import _relevant_rules, _rule_chunks


def enrich(input_path: Path, rules_root: Path, output_dir: Path) -> dict[str, Any]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("packets"), list):
        raise ValueError("AI screening input packets are missing")
    chunks = _rule_chunks(rules_root)
    source_hashes: dict[str, str] = {}
    for packet in payload["packets"]:
        if not isinstance(packet, dict):
            raise ValueError("AI screening packet is not an object")
        type_key = str(packet.get("type_key") or "")
        if type_key not in {f"type{i}" for i in range(1, 8)}:
            raise ValueError(f"invalid candidate type: {type_key}")
        packet["rule_context"] = _relevant_rules(chunks, type_key)
        if not packet["rule_context"]:
            raise ValueError(f"no knowledge fragments selected for {type_key}")
        for item in packet["rule_context"]:
            source_id = item["source_id"]
            digest = item["source_sha256"]
            existing = source_hashes.setdefault(source_id, digest)
            if existing != digest:
                raise ValueError(f"rule source has conflicting hashes: {source_id}")
    payload["rules_root"] = str(rules_root)
    payload["rule_file_count"] = len(source_hashes)
    payload["rule_source_sha256"] = dict(sorted(source_hashes.items()))
    identity_digest = candidate_identity_sha256(payload["packets"])
    declared_identity_digest = str(payload.get("candidate_identity_sha256") or "")
    if declared_identity_digest and declared_identity_digest != identity_digest:
        raise ValueError("candidate identity hash changed during enrichment")
    payload["candidate_identity_sha256"] = identity_digest
    output_dir.mkdir(parents=True, exist_ok=True)
    input_out = output_dir / "ai-screening-input.json"
    candidates_out = output_dir / "ai-screening-candidates.jsonl"
    input_bytes = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    input_out.write_bytes(input_bytes)
    candidates_out.write_text(
        "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n" for packet in payload["packets"]),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": payload.get("schema_version"),
        "snapshot_generation": payload.get("snapshot_generation"),
        "market_as_of": payload.get("market_as_of"),
        "methodology_version": payload.get("methodology_version"),
        "candidate_count": payload.get("candidate_count", len(payload["packets"])),
        "candidate_total": payload.get("candidate_total", len(payload["packets"])),
        "candidate_offset": payload.get("candidate_offset", 0),
        "candidate_identity_sha256": payload["candidate_identity_sha256"],
        "candidate_universe_identity_sha256": payload.get("candidate_universe_identity_sha256"),
        "queue_full_coverage": payload.get("queue_full_coverage", False),
        "review_mode": payload.get("review_mode"),
        "full_coverage_final_recommendation": payload.get("full_coverage_final_recommendation", False),
        "rule_file_count": len(source_hashes),
        "rule_source_sha256": dict(sorted(source_hashes.items())),
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
    }
    (output_dir / "ai-screening-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--rules-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(enrich(args.input, args.rules_root, args.output_dir), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
