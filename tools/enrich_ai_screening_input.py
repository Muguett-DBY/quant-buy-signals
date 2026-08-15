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

from tools.build_ai_screening import _relevant_rules, _rule_chunks


def enrich(input_path: Path, rules_root: Path, output_dir: Path) -> dict[str, Any]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("packets"), list):
        raise ValueError("AI screening input packets are missing")
    chunks = _rule_chunks(rules_root)
    source_hashes = {
        item["source_id"]: item["source_sha256"]
        for item in chunks
        if item.get("source_id") and item.get("source_sha256")
    }
    for packet in payload["packets"]:
        if not isinstance(packet, dict):
            raise ValueError("AI screening packet is not an object")
        type_key = str(packet.get("type_key") or "")
        if type_key not in {f"type{i}" for i in range(1, 8)}:
            raise ValueError(f"invalid candidate type: {type_key}")
        packet["rule_context"] = _relevant_rules(chunks, type_key)
        if not packet["rule_context"]:
            raise ValueError(f"no knowledge fragments selected for {type_key}")
    payload["rules_root"] = rules_root.name
    payload["rule_file_count"] = len(source_hashes)
    payload["rule_source_sha256"] = dict(sorted(source_hashes.items()))
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
        "candidate_total": len(payload["packets"]),
        "rule_file_count": len(source_hashes),
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
