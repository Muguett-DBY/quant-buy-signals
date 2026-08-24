"""Build a generation-bound retrieval artifact from Codex web-search shards.

This module only joins real search envelopes to the frozen candidate queue.  It
does not turn a citation into a model claim and never marks the Codex tool as a
provider-native search event.  A later reviewer may use the findings to make a
recommendation; this artifact is the auditable retrieval layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_REF_RE = re.compile(r"turn\d+(?:search|view|fetch)\d+", re.IGNORECASE)
_BLOCK_SEPARATOR = "\n--------------------------------------------------------------------------------\n"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_kind(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("cninfo.com.cn") or host.endswith("sse.com.cn") or host.endswith("szse.cn"):
        return "exchange_or_regulator"
    if host.endswith("sina.com.cn") or host.endswith("eastmoney.com"):
        return "financial_portal_filing"
    if host.endswith("gov.cn"):
        return "government"
    if host.endswith("cnstock.com") or host.endswith("stcn.com"):
        return "financial_media"
    return "secondary_web_source"


def _finding_from_block(block: str, *, code: str, index: int) -> dict[str, Any] | None:
    urls = [url.rstrip(".,;:!?)]}>") for url in _URL_RE.findall(block)]
    urls = list(dict.fromkeys(url for url in urls if url.startswith("https://")))
    if not urls or code not in block:
        return None
    first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
    refs = list(dict.fromkeys(_REF_RE.findall(block)))
    return {
        "id": f"codex-search-{index:03d}",
        "title": first_line[:240],
        "url": urls[0],
        "source_kind": _source_kind(urls[0]),
        "codex_reference": refs[0] if refs else None,
        "source_urls": urls[:4],
        "finding": block[:1600].strip(),
    }


def _raw_findings(raw: str, *, code: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for index, block in enumerate(raw.split(_BLOCK_SEPARATOR), 1):
        finding = _finding_from_block(block, code=code, index=index)
        if finding:
            findings.append(finding)
    return findings[:8]


def _load_batches(path: Path) -> dict[int, dict[str, Any]]:
    chosen: dict[int, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("batch_index"), int):
            raise ValueError(f"invalid batch envelope at {path}:{line_number}")
        # A resumed writer can contain an earlier attempt and a later retry;
        # the last envelope is the one that will be reviewed.
        chosen[int(value["batch_index"])] = value
    return chosen


def build(
    candidates_path: Path,
    batches_path: Path,
    output_path: Path,
    *,
    extra_shards: list[Path] | None = None,
) -> dict[str, Any]:
    extra_shards = extra_shards or []
    input_payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    packets = input_payload.get("packets")
    if not isinstance(packets, list) or len(packets) != int(input_payload.get("candidate_total") or 0):
        raise ValueError("candidate queue is incomplete")
    batches = _load_batches(batches_path)
    extra: dict[str, dict[str, Any]] = {}
    for path in extra_shards:
        shard = json.loads(path.read_text(encoding="utf-8"))
        for record in shard.get("records", []):
            if isinstance(record, dict):
                extra[str(record.get("security_code") or "")] = record
    records: list[dict[str, Any]] = []
    missing_batches: list[int] = []
    for index, packet in enumerate(packets):
        code = str(packet.get("security_code") or "").strip()
        name = str(packet.get("name") or packet.get("company_context", {}).get("name") or code)
        if index >= 666:
            record = extra.get(code)
            if record is None:
                raise ValueError(f"missing external shard record: {index}/{code}")
            # Keep the shard's audited record but bind it to this frozen queue.
            record = dict(record)
            record["index"] = index
            records.append(record)
            continue
        batch_index = index // 4
        envelope = batches.get(batch_index)
        if envelope is None:
            missing_batches.append(batch_index)
            continue
        raw = str(envelope.get("raw_response") or "")
        findings = _raw_findings(raw, code=code)
        records.append(
            {
                "index": index,
                "security_code": code,
                "company_name": name,
                "query": next(
                    (str(q) for q in envelope.get("queries", []) if code in str(q)),
                    str(envelope.get("queries", [""])[0] if envelope.get("queries") else ""),
                ),
                "search_status": "found" if findings else "no_company_specific_result",
                "codex_web_search": True,
                "provider_native_search": False,
                "provider_native_event_verified": False,
                "result_count": len(findings),
                "findings": findings,
                "raw_excerpt": raw[:1800],
                "batch_index": batch_index,
                "captured_at": envelope.get("captured_at"),
            }
        )
    if missing_batches:
        raise ValueError(f"missing search batches: {sorted(set(missing_batches))[:12]}")
    records.sort(key=lambda value: int(value.get("index", 0)))
    if len(records) != len(packets) or len({str(v.get("security_code")) for v in records}) != len(packets):
        raise ValueError("retrieval artifact does not cover each company exactly once")
    body = {
        "artifact_kind": "codex_web_retrieval",
        "schema_version": 1,
        "snapshot_generation": input_payload.get("snapshot_generation"),
        "market_as_of": input_payload.get("market_as_of"),
        "candidate_total": len(packets),
        "candidate_identity_sha256": input_payload.get("candidate_identity_sha256"),
        "retrieval_backend": "codex-web-tool",
        "provider_native_search": False,
        "provider_native_event_verified": False,
        "query_policy": "one company-specific search per frozen candidate; no result reuse",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "company_search_count": len(records),
        "successful_company_searches": sum(bool(record.get("codex_web_search")) for record in records),
        "company_specific_result_count": sum(bool(record.get("findings")) for record in records),
        "company_specific_source_count": sum(len(record.get("findings") or []) for record in records),
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {
        "company_search_count": len(records),
        "company_specific_result_count": body["company_specific_result_count"],
        "company_specific_source_count": body["company_specific_source_count"],
        "sha256": _sha256_bytes(output_path.read_bytes()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--batches", type=Path, required=True)
    parser.add_argument("--extra-shard", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.candidates, args.batches, args.out, extra_shards=args.extra_shard), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
