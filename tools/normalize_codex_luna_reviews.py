"""Normalize a Codex/Luna independent shard to the public review contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def build(source: Path, output: Path) -> dict[str, int]:
    rows: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("review shard contains a non-object")
        provenance = value.get("search_provenance") if isinstance(value.get("search_provenance"), Mapping) else {}
        raw_findings = value.get("search_findings") if isinstance(value.get("search_findings"), list) else []
        query = str(provenance.get("query") or (raw_findings[0].get("query") if raw_findings else "")).strip()
        findings: list[dict[str, Any]] = []
        web_claims: list[dict[str, Any]] = []
        for index, finding in enumerate(raw_findings, 1):
            if not isinstance(finding, Mapping):
                continue
            url = str(finding.get("url") or "").strip()
            if not url.startswith("https://"):
                continue
            finding_id = f"codex-luna-search-{index:03d}"
            text = str(finding.get("snippet") or finding.get("title") or "已找到公司特定网页来源。").strip()
            public_finding = {
                "id": finding_id,
                "query": str(finding.get("query") or query),
                "title": str(finding.get("title") or "")[:300],
                "url": url,
                "published_at": None,
                "report_period": None,
                "finding": text[:600],
                "stance": "neutral",
                "source_kind": "codex_web_search",
            }
            findings.append(public_finding)
            web_claims.append(
                {
                    "search_finding_id": finding_id,
                    "source_context": public_finding["title"],
                    "source_ref": url,
                    "source_refs": [url],
                    "statement": text[:600],
                    "support": "supports",
                    "source_kind": "codex_web_search",
                }
            )
        prior_claims = value.get("claims") if isinstance(value.get("claims"), list) else []
        # The local Codex overlay keeps a compact, bounded claim set.  Native
        # company-research artifacts have a separate 32-claim contract; this
        # shard is deliberately the smaller local contract consumed by Pages.
        value["claims"] = [*prior_claims, *web_claims][:12]
        value["search_findings"] = findings
        value["web_search_performed"] = True
        value["web_search_verified"] = False
        value["web_search_event_verified"] = False
        value["web_search_claim_urls_verified"] = False
        value["web_search_queries"] = [query] if query else []
        value["web_search_verified_claim_urls"] = []
        value["web_search_dropped_claim_url_count"] = 0
        value["codex_web_tool"] = True
        value["provider_native_search"] = False
        value["provider_native_event_verified"] = False
        value["freshness_note"] = "量化快照交易日为 2026-08-24；Codex 网页检索于 2026-08-25，报告期与网页日期分开记录。"
        rows.append(value)
    codes = [str(row.get("security_code") or "") for row in rows]
    if not rows or len(set(codes)) != len(rows):
        raise ValueError("shard reviews must contain unique security codes")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return {
        "reviewed": len(rows),
        "searched": sum(row.get("web_search_performed") is True for row in rows),
        "with_company_sources": sum(bool(row.get("search_findings")) for row in rows),
        "recommend_buy": sum(row.get("ai_action") == "priority_buy" for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
