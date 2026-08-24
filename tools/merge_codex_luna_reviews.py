"""Replace legacy reviews with independently reviewed Codex/Luna shard rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"review shard row is not an object: {path}")
        code = str(value.get("security_code") or "").strip()
        if not code or code in rows:
            raise ValueError(f"duplicate or missing shard security code: {path}:{code}")
        rows[code] = value
    return rows


def merge(input_path: Path, shard_paths: list[Path], output_path: Path) -> dict[str, int]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("merged artifact packets are missing")
    replacements: dict[str, dict[str, Any]] = {}
    for path in shard_paths:
        for code, review in _load_jsonl(path).items():
            if code in replacements:
                raise ValueError(f"shard overlap for security code: {code}")
            replacements[code] = review
    replaced = 0
    copied_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, dict):
            raise ValueError("merged artifact packet is not an object")
        copied = dict(packet)
        code = str(packet.get("security_code") or "").strip()
        replacement = replacements.get(code)
        if replacement is not None:
            review = dict(replacement)
            review["security_code"] = code
            review["type_key"] = str(packet.get("type_key") or review.get("type_key") or "")
            review["snapshot_generation"] = str(
                payload.get("snapshot_generation") or review.get("snapshot_generation") or ""
            )
            review["market_as_of"] = str(payload.get("market_as_of") or review.get("market_as_of") or "")
            copied["ai_review"] = review
            replaced += 1
        copied_packets.append(copied)
    missing = sorted(set(replacements) - {str(packet.get("security_code") or "") for packet in packets})
    if missing:
        raise ValueError(f"shard code is not in candidate queue: {missing[0]}")
    payload["packets"] = copied_packets
    payload["review_mode"] = "local_codex_review"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"packet_count": len(packets), "replacement_count": replaced, "shard_count": len(replacements)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("shards", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps(merge(args.input, args.shards, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
