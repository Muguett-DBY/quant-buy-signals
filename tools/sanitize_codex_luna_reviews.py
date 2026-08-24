"""Remove search prose whose stock identifier is not bound to the candidate.

The Codex web search itself is retained as an attempted search.  A result with
another company's ``stockid`` is not exposed as a fact in the public overlay.
This is a small release-boundary check for independently produced review
shards; it does not invent a replacement source or alter the recommendation.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping


_STOCK_ID_RE = re.compile(r"stockid=(\d+)")
_SOURCE_MARKERS = (
    "联网事实（Codex web_search",
    "联网资料摘要：",
    "联网事实(Codex web_search",
)


def _wrong_stock_ids(value: Any, code: str) -> set[str]:
    return {match for match in _STOCK_ID_RE.findall(str(value or "")) if match != code}


def _without_unbound_source(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if not _wrong_stock_ids(text, code):
        return text
    for marker in _SOURCE_MARKERS:
        position = text.find(marker)
        if position >= 0:
            prefix = text[:position].rstrip(" ;，。")
            return f"{prefix}。联网检索已执行，但来源公司代码未通过归属校验，未将该页面作为本公司事实。"
    return "联网检索已执行，但来源公司代码未通过归属校验，未将该页面作为本公司事实。"


def _without_unbound_sources(value: Any, code: str) -> Any:
    if isinstance(value, list):
        return [_without_unbound_source(item, code) for item in value]
    return _without_unbound_source(value, code)


def sanitize(payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    result = dict(payload)
    packets = result.get("packets")
    if not isinstance(packets, list):
        raise ValueError("merged artifact packets are missing")
    changed = 0
    copied_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("merged artifact packet is not an object")
        copied_packet = dict(packet)
        review = packet.get("ai_review")
        if isinstance(review, Mapping):
            copied_review = dict(review)
            code = str(packet.get("security_code") or review.get("security_code") or "").strip()
            before = json.dumps(copied_review, ensure_ascii=False, sort_keys=True)
            for field in ("summary", "key_strengths", "risk_flags"):
                if field in copied_review:
                    copied_review[field] = _without_unbound_sources(copied_review[field], code)
            profile = copied_review.get("economic_profile")
            if isinstance(profile, Mapping):
                claims = copied_review.get("claims") if isinstance(copied_review.get("claims"), list) else []
                findings = copied_review.get("search_findings")
                finding_ids = {
                    str(item.get("id") or "")
                    for item in (findings if isinstance(findings, list) else [])
                    if isinstance(item, Mapping)
                }
                claim_ids = {str(item.get("search_finding_id") or "") for item in claims if isinstance(item, Mapping)}
                allowed_ids = {value for value in claim_ids | finding_ids if value}
                business_ids = profile.get("business_model_source_ids")
                if isinstance(business_ids, list) and any(str(value) not in allowed_ids for value in business_ids):
                    # A partial structured envelope is less safe than the
                    # legacy, source-free prose contract.  Keep the review's
                    # recommendation and facts, but omit the malformed
                    # business-model source graph from the public projection.
                    copied_review.pop("research_as_of", None)
                    copied_review.pop("economic_profile", None)
                    copied_review.pop("valuation", None)
                    copied_review.pop("valuation_snapshot", None)
            after = json.dumps(copied_review, ensure_ascii=False, sort_keys=True)
            if before != after:
                changed += 1
            copied_packet["ai_review"] = copied_review
        copied_packets.append(copied_packet)
    result["packets"] = copied_packets
    return result, changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    sanitized, changed = sanitize(payload)
    args.output.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"changed_reviews": changed, "packet_count": len(sanitized["packets"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
