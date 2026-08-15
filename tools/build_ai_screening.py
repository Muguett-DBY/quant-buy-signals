"""Build and merge a local, source-grounded AI screening packet.

The packet is intentionally smaller than the raw catalogue.  It preserves the
deterministic result for the reviewed type and a compact summary of the other
types; the model is never allowed to rewrite the seven-type calculation.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import REVIEW_SCHEMA_VERSION, select_candidates, validate_review


def _load_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a JSON object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rule_chunks(root: Path) -> list[dict[str, str]]:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"rules root does not exist or is not a directory: {root}")
    chunks: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
        current = ""
        buf: list[str] = []
        start = 1
        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith("#") and buf:
                body = "\n".join(buf).strip()
                if body:
                    chunks.append(
                        {
                            "source_id": path.name,
                            "source_sha256": _sha256_bytes(raw),
                            "heading": current,
                            "line_start": str(start),
                            "text": body,
                        }
                    )
                buf = []
                start = number
                current = line.lstrip("# ").strip()
            buf.append(line)
        if buf:
            body = "\n".join(buf).strip()
            if body:
                chunks.append(
                    {
                        "source_id": path.name,
                        "source_sha256": _sha256_bytes(raw),
                        "heading": current,
                        "line_start": str(start),
                        "text": body,
                    }
                )
    if not chunks:
        raise ValueError(f"rules root contains no Markdown knowledge: {root}")
    return chunks


def _relevant_rules(chunks: list[dict[str, str]], type_key: str) -> list[dict[str, str]]:
    terms = {
        type_key,
        "\u603b\u95f8\u95e8",
        "\u4e70\u5165",
        "\u8bc1\u636e",
        "\u5426\u51b3",
    }
    selected = [chunk for chunk in chunks if any(term in chunk["text"] or term in chunk["heading"] for term in terms)]
    selected.sort(key=lambda chunk: (chunk["source_id"], int(chunk["line_start"])))
    return [{**chunk, "text": chunk["text"][:1400]} for chunk in selected[:16]]


def _compact_company(company: Mapping[str, Any], selected_type: str) -> dict[str, Any]:
    """Keep the model packet small without hiding deterministic context."""
    fields = (
        "code",
        "name",
        "industry",
        "industry_code",
        "market_cap",
        "price",
        "pe",
        "pb",
        "diagnostic_score",
        "diagnostic_type",
        "buy_types",
        "conditional_types",
        "pending_types",
        "primary_type",
        "primary_label",
    )
    compact = {key: company.get(key) for key in fields if key in company}
    types = company.get("types") or company.get("type_results") or {}
    if isinstance(types, dict):
        other: dict[str, Any] = {}
        for key, value in types.items():
            if key == selected_type or not isinstance(value, dict):
                continue
            decision = value.get("decision") if isinstance(value.get("decision"), dict) else {}
            other[str(key)] = {
                "status": value.get("status"),
                "score": value.get("score"),
                "score_lower_bound": decision.get("score_lower_bound"),
                "score_upper_bound": decision.get("score_upper_bound"),
                "decision_basis": decision.get("decision_basis"),
                "veto_state": decision.get("veto_state"),
            }
        compact["other_type_summary"] = other
    return compact


def build_input(
    snapshot_path: Path,
    rules_root: Path,
    out_dir: Path,
    *,
    limit: int | None = None,
    offset: int = 0,
    market_as_of: str | None = None,
    generation: str | None = None,
) -> dict[str, Any]:
    snapshot = _load_json(snapshot_path)
    snapshot_generation = generation or snapshot.get("generation") or snapshot.get("generation_id")
    snapshot_market_as_of = market_as_of or snapshot.get("market_as_of")
    candidates = select_candidates(snapshot)
    if offset < 0:
        raise ValueError("offset must be non-negative")
    selected = candidates[offset:]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    chunks = _rule_chunks(rules_root)
    packets: list[dict[str, Any]] = []
    for candidate in selected:
        company = candidate.pop("company", {})
        packets.append(
            {
                **candidate,
                "rule_context": _relevant_rules(chunks, candidate["type_key"]),
                "company_context": _compact_company(company, candidate["type_key"]),
                "ai_review": None,
            }
        )
    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "snapshot_generation": snapshot_generation,
        "market_as_of": snapshot_market_as_of,
        "methodology_version": snapshot.get("methodology_version"),
        "index_contract": snapshot.get("index_contract"),
        "rules_root": str(rules_root),
        "rule_file_count": len({chunk["source_id"] for chunk in chunks}),
        "candidate_count": len(packets),
        "candidate_total": len(candidates),
        "candidate_offset": offset,
        "packets": packets,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = out_dir / "ai-screening-input.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "ai-screening-candidates.jsonl").write_text(
        "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n" for packet in packets),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "snapshot_generation": payload["snapshot_generation"],
        "market_as_of": payload["market_as_of"],
        "methodology_version": payload["methodology_version"],
        "index_contract": payload["index_contract"],
        "candidate_count": len(packets),
        "candidate_total": len(candidates),
        "candidate_offset": offset,
        "rule_file_count": payload["rule_file_count"],
        "input_sha256": _sha256_bytes(input_path.read_bytes()),
    }
    (out_dir / "ai-screening-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def merge_reviews(input_path: Path, review_path: Path, out_path: Path) -> None:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    reviews: dict[tuple[str, str], dict[str, Any]] = {}
    for line in review_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        review = json.loads(line)
        errors = validate_review(review)
        if errors:
            raise ValueError(f"invalid review: {','.join(errors)}")
        key = (str(review.get("security_code")), str(review.get("type_key")))
        reviews[key] = review
    for packet in payload.get("packets", []):
        key = (str(packet.get("security_code")), str(packet.get("type_key")))
        packet["ai_review"] = reviews.get(key)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--rules-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--market-as-of")
    parser.add_argument("--generation")
    parser.add_argument("--review-jsonl", type=Path)
    args = parser.parse_args()
    build_input(
        args.snapshot,
        args.rules_root,
        args.out,
        limit=args.limit,
        offset=args.offset,
        market_as_of=args.market_as_of,
        generation=args.generation,
    )
    if args.review_jsonl:
        merge_reviews(args.out / "ai-screening-input.json", args.review_jsonl, args.out / "ai-screening.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
