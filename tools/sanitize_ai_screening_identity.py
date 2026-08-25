"""Remove cross-company source-snippet contamination from a calibrated queue."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.ai_screening_identity import sanitise_review_identity


def sanitise_artifact(source_path: Path, output_path: Path) -> dict[str, int]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("source packets are missing")
    counts = {
        "removed_cross_company_claim_count": 0,
        "removed_cross_company_text_count": 0,
        "cleaned_cross_company_text_count": 0,
    }
    output_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("packet is not an object")
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            raise ValueError(f"packet has no AI review: {packet.get('security_code')}")
        clean_review, review_counts = sanitise_review_identity(
            review,
            str(packet.get("security_code") or ""),
        )
        for key, value in review_counts.items():
            counts[key] += value
        clean_packet = dict(packet)
        clean_packet["ai_review"] = clean_review
        output_packets.append(clean_packet)
    sanitisation = dict(source.get("publication_sanitization") or {})
    sanitisation.setdefault("contract_version", 2)
    for key, value in counts.items():
        sanitisation[key] = int(sanitisation.get(key, 0) or 0) + value
    output = {**source, "publication_sanitization": sanitisation, "packets": output_packets}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"packet_count": len(output_packets), **counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(sanitise_artifact(args.source, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
