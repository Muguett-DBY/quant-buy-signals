"""Attach same-generation neutral company facts to public AI review cards.

This is deliberately a data-layer enrichment, not a second scoring pass.  It
keeps the model action, score, and model-written reasons unchanged while
making the separate quantitative-facts field auditable from the snapshot that
produced the candidate list.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from tools.ai_quantitative_facts import quantitative_facts


def _unique(values: list[Any], limit: int = 8) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text[:240])
        if len(result) >= limit:
            break
    return result


def _packet_facts(company: Mapping[str, Any], packet: Mapping[str, Any], market_as_of: str) -> list[str]:
    # ``packet`` is retained in the helper signature because callers from the
    # previous pair-aware enrichment pass still supply it.  The facts are now
    # company-level and must not vary with a candidate type or status.
    del packet
    return _unique(quantitative_facts(company, "", market_as_of=market_as_of))


def enrich(payload: Mapping[str, Any], context_payload: Mapping[str, Any]) -> dict[str, Any]:
    companies = context_payload.get("companies")
    if not isinstance(companies, Mapping):
        raise ValueError("quantitative context has no companies map")
    generation = str(payload.get("snapshot_generation") or "")
    context_generation = str(context_payload.get("generation") or context_payload.get("generation_id") or "")
    if generation and context_generation and generation != context_generation:
        raise ValueError(f"quantitative context generation mismatch: {generation} != {context_generation}")
    market_as_of = str(payload.get("market_as_of") or context_payload.get("market_as_of") or "")
    output = json.loads(json.dumps(payload, ensure_ascii=False))
    packets = output.get("packets")
    if not isinstance(packets, list):
        raise ValueError("AI screening payload has no packets")
    enriched = 0
    priority_with_two = 0
    for packet in packets:
        if not isinstance(packet, dict):
            raise ValueError("AI screening packet must be an object")
        code = str(packet.get("security_code") or "")
        entry = companies.get(code)
        company = entry.get("company") if isinstance(entry, Mapping) else None
        if not isinstance(company, Mapping):
            raise ValueError(f"quantitative context missing company {code}")
        facts = _packet_facts(company, packet, market_as_of)
        review = packet.get("ai_review")
        if not isinstance(review, dict):
            raise ValueError(f"AI review missing for {code}")
        # Rebuild this field from the same-generation snapshot.  Merging a
        # legacy/model-provided list could reintroduce type labels, statuses,
        # or rule scores into an otherwise neutral company-facts field.
        review["quantitative_facts"] = facts
        enriched += 1
        if (
            str(review.get("ai_action") or "") == "priority_buy"
            and sum(any(ch.isdigit() for ch in fact) for fact in facts) >= 2
        ):
            priority_with_two += 1
    return output, enriched, priority_with_two


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.artifact.read_text(encoding="utf-8"))
    context = json.loads(args.context.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or not isinstance(context, Mapping):
        raise ValueError("AI screening artifact and quantitative context must be JSON objects")
    output, enriched, priority_with_two = enrich(payload, context)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"enriched": enriched, "priority_buy_with_two_numeric_facts": priority_with_two}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
