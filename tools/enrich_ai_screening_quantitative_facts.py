"""Attach same-generation deterministic figures to public AI review cards.

This is deliberately a data-layer enrichment, not a second scoring pass.  It
keeps the model action and score unchanged while making every explanation
auditable from the snapshot that produced the candidate list.
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
    type_key = str(packet.get("type_key") or company.get("primary_type") or "")
    type_keys = packet.get("type_keys")
    if not isinstance(type_keys, list):
        type_keys = [type_key]
    keys = [type_key, *[str(value) for value in type_keys if str(value) != type_key]]
    raw: list[str] = []
    for key in dict.fromkeys(keys):
        raw.extend(quantitative_facts(company, key, market_as_of=market_as_of))
    # Keep valuation first, then dimension-level figures from every triggered
    # or near-triggered type.  This makes a type1+type2 card show both its
    # discount/FCF evidence and the industry/price-temperature evidence.
    valuation = [value for value in raw if value.startswith("估值快照：")]
    dimensions = [value for value in raw if value.startswith("确定性 ") and "-" in value]
    scores = [value for value in raw if value.startswith("确定性 ") and "-" not in value]
    history = [value for value in raw if value.startswith("年度财务历史覆盖")]
    return _unique([*valuation, *dimensions, *scores, *history])


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
        old_facts = review.get("quantitative_facts") if isinstance(review.get("quantitative_facts"), list) else []
        facts = _unique([*facts, *old_facts])
        review["quantitative_facts"] = facts
        old_strengths = review.get("key_strengths") if isinstance(review.get("key_strengths"), list) else []
        review["key_strengths"] = _unique([*facts, *old_strengths], limit=8)
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
