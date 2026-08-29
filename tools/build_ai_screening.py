"""Build and merge a local, source-grounded AI screening packet.

The packet is intentionally smaller than the raw catalogue.  It preserves all
deterministic candidate types for one company and a compact summary of the
other types; the model is never allowed to rewrite the seven-type calculation.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import (
    REVIEW_SCHEMA_VERSION,
    candidate_identity_sha256,
    group_candidates_by_company,
    select_candidates,
    validate_review,
)
from tools.atomic_io import atomic_write_text
from tools.ai_quantitative_facts import quantitative_facts
from tools.ai_company_research_provenance import (
    load_research_provenance_context,
    validate_review_set_provenance,
)


_PATCH7_RULE_FILE = "补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md"
_TYPE_RULE_FILES = {
    "type1": ("第25模板.md",),
    "type2": ("第17模板.md",),
    "type3": ("第15模板.md",),
    "type4": ("第10模板.md",),
    "type5": ("第6模板.md", "第9模板.md"),
    "type6": ("第19模板.md",),
    "type7": (
        "补丁6· 公司三属性分类与三维度量化打分机制.md",
        "第1模板.md",
        "第5模板.md",
        "补丁5.md",
    ),
}
_PATCH7_SITUATION_MARKERS = {
    "type1": "情况一｜第25模板",
    "type2": "情况二｜第17模板",
    "type3": "情况三｜第15模板",
    "type4": "情况四｜第10模板",
    "type5": "情况五｜第6、第9模板",
    "type6": "情况六｜第19模板",
    "type7": "\n情况七\n",
}
_PATCH7_GUIDE_MARKER = "\n综合运用指南\n"
_PATCH7_TYPE5_APPENDIX_MARKER = "补丁7 方法论附录｜强周期产业底部估值特例与五维度操作清单。"
_PATCH7_SELL_GATE_MARKER = "附录：卖出闸门协议（四硬一软）"
_MAX_RULE_CHARS = 12_000
_FULL_COVERAGE_REVIEW_MODES = frozenset(
    {
        "local_codex_review",
        "opencode_web_review",
        "opencode_mixed_review",
        "opencode_native_web_search_review",
        "opencode_native_company_research_review",
        "codex_luna_web_review",
    }
)


def _load_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a JSON object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _line_number(text: str, offset: int) -> str:
    return str(text.count("\n", 0, offset) + 1)


def _patch7_chunks(path: Path, raw: bytes, text: str) -> list[dict[str, str]]:
    """Split Patch 7 by its semantic situation markers, not Markdown headings.

    The authoritative Patch 7 document has one Markdown heading for more than
    two thousand lines.  Treating that as one chunk and truncating its prefix
    hides every detailed situation rule from the model.  These stable section
    labels are part of the source document's own contract.
    """

    normalised = text.replace("\r\n", "\n").replace("\r", "\n")

    def marker_offset(marker: str) -> int:
        offset = normalised.find(marker)
        if offset < 0:
            raise ValueError(f"Patch 7 is missing required section marker: {marker.strip()}")
        return offset

    positions = {type_key: marker_offset(marker) for type_key, marker in _PATCH7_SITUATION_MARKERS.items()}
    guide = marker_offset(_PATCH7_GUIDE_MARKER)
    appendix = marker_offset(_PATCH7_TYPE5_APPENDIX_MARKER)
    sell_gate = marker_offset(_PATCH7_SELL_GATE_MARKER)
    ordered = [*(positions[type_key] for type_key in _TYPE_RULE_FILES), guide, appendix, sell_gate]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        raise ValueError("Patch 7 situation sections are missing or out of order")

    digest = _sha256_bytes(raw)

    def chunk(start: int, end: int, heading: str, scope: str) -> dict[str, str]:
        return {
            "source_id": path.name,
            "source_sha256": digest,
            "heading": heading,
            "line_start": _line_number(normalised, start),
            "scope": scope,
            "text": normalised[start:end].strip(),
        }

    chunks = [
        chunk(0, positions["type1"], "补丁7共同前提与总闸门", "common"),
        chunk(guide, appendix, "补丁7综合运用指南", "common"),
    ]
    type_keys = tuple(_TYPE_RULE_FILES)
    for index, type_key in enumerate(type_keys):
        end = positions[type_keys[index + 1]] if index + 1 < len(type_keys) else guide
        chunks.append(
            chunk(
                positions[type_key],
                end,
                _PATCH7_SITUATION_MARKERS[type_key].strip(),
                type_key,
            )
        )
    chunks.append(chunk(appendix, sell_gate, "补丁7强周期方法论附录", "type5"))
    return chunks


def _rule_chunks(root: Path) -> list[dict[str, str]]:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"rules root does not exist or is not a directory: {root}")
    chunks: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
        if path.name == _PATCH7_RULE_FILE:
            chunks.extend(_patch7_chunks(path, raw, text))
            continue
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
    if type_key not in _TYPE_RULE_FILES:
        raise ValueError(f"unsupported AI screening type: {type_key}")
    has_authoritative_patch7 = any(chunk["source_id"] == _PATCH7_RULE_FILE for chunk in chunks)
    if has_authoritative_patch7:
        required_files = set(_TYPE_RULE_FILES[type_key])
        selected = [
            chunk
            for chunk in chunks
            if chunk.get("scope") in {"common", type_key} or chunk["source_id"] in required_files
        ]
        present_files = {chunk["source_id"] for chunk in selected}
        missing_files = sorted(required_files - present_files)
        if missing_files:
            raise ValueError(f"rules root is missing authoritative {type_key} sources: {missing_files}")
    else:
        # Unit fixtures and deliberately small custom rule roots predate the
        # authoritative filename contract.  Prefer an exact type marker, then
        # retain the complete small rule set rather than guessing via generic
        # words such as "买入" or "证据".
        selected = [chunk for chunk in chunks if type_key in chunk["text"] or type_key in chunk["heading"]]
        if not selected:
            selected = chunks
    selected.sort(key=lambda chunk: (chunk["source_id"], int(chunk["line_start"])))
    return [{**chunk, "text": chunk["text"][:_MAX_RULE_CHARS]} for chunk in selected]


def _align_type7_score_bounds(type_key: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a complete Type 7 score and its zero-width bounds identical.

    Type 7 totals are intentionally kept at three decimal places.  The
    catalogue's legacy decision-bound serializer rounded both ends of a
    complete zero-width interval to one decimal place, so a value such as
    ``7.451`` was paired with ``[7.5, 7.5]``.  That interval is not a genuine
    uncertainty range: it is the exact score after display rounding.  The AI
    packet must therefore retain the exact score for all three fields while
    leaving real (non-zero-width or incomplete) bounds untouched.
    """

    result = dict(value)
    if type_key != "type7" or result.get("has_missing_dimensions") is True or result.get("bounded") is True:
        return result

    def number(raw: Any) -> float | None:
        if isinstance(raw, bool) or raw is None:
            return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    score = number(result.get("score"))
    lower = number(result.get("score_lower_bound"))
    upper = number(result.get("score_upper_bound"))
    if score is None or lower is None or upper is None or lower != upper:
        return result

    result["score_lower_bound"] = score
    result["score_upper_bound"] = score
    decision = result.get("decision")
    if isinstance(decision, Mapping):
        decision_copy = dict(decision)
        decision_copy["score_lower_bound"] = score
        decision_copy["score_upper_bound"] = score
        result["decision"] = decision_copy
    return result


def _compact_company(
    company: Mapping[str, Any], selected_type: str | list[str], *, market_as_of: str | None = None
) -> dict[str, Any]:
    """Keep the model packet small without hiding deterministic context."""
    selected_types = [selected_type] if isinstance(selected_type, str) else list(dict.fromkeys(selected_type))
    selected_type_set = set(selected_types)
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
            if key in selected_type_set or not isinstance(value, dict):
                continue
            compact_value = _align_type7_score_bounds(str(key), value)
            decision = compact_value.get("decision") if isinstance(compact_value.get("decision"), dict) else {}
            other[str(key)] = {
                "status": compact_value.get("status"),
                "score": compact_value.get("score"),
                "score_lower_bound": compact_value.get("score_lower_bound", decision.get("score_lower_bound")),
                "score_upper_bound": compact_value.get("score_upper_bound", decision.get("score_upper_bound")),
                "decision_basis": decision.get("decision_basis"),
                "veto_state": decision.get("veto_state"),
            }
        compact["other_type_summary"] = other
    active_types = [
        *selected_types,
        *[str(value) for value in (company.get("buy_types") or []) if str(value)],
        *[str(value) for value in (company.get("conditional_types") or []) if str(value)],
    ]
    facts: list[str] = []
    for type_key in dict.fromkeys(active_types):
        facts.extend(quantitative_facts(company, type_key, market_as_of=market_as_of))
    facts = list(dict.fromkeys(facts))[:8]
    if facts:
        compact["quantitative_facts"] = facts
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
    review_mode: str | None = None,
) -> dict[str, Any]:
    snapshot = _load_json(snapshot_path)
    source_generation = str(snapshot.get("generation") or snapshot.get("generation_id") or "").strip()
    source_market_as_of = str(snapshot.get("market_as_of") or "").strip()
    requested_generation = str(generation or "").strip()
    requested_market_as_of = str(market_as_of or "").strip()
    if requested_generation and source_generation and requested_generation != source_generation:
        raise ValueError(f"generation override conflicts with snapshot: {requested_generation}/{source_generation}")
    if requested_market_as_of and source_market_as_of and requested_market_as_of != source_market_as_of:
        raise ValueError(
            f"market_as_of override conflicts with snapshot: {requested_market_as_of}/{source_market_as_of}"
        )
    snapshot_generation = requested_generation or source_generation
    snapshot_market_as_of = requested_market_as_of or source_market_as_of
    if not snapshot_generation or not snapshot_market_as_of:
        raise ValueError("snapshot must carry generation and market_as_of")
    candidate_pairs = [
        {
            **candidate,
            "deterministic": _align_type7_score_bounds(
                str(candidate.get("type_key") or ""), candidate["deterministic"]
            ),
        }
        for candidate in select_candidates(snapshot)
    ]
    type_pair_universe_identity_sha256 = candidate_identity_sha256(candidate_pairs)
    candidates = group_candidates_by_company(candidate_pairs)
    candidate_universe_sha256 = candidate_identity_sha256(candidates)
    if offset < 0:
        raise ValueError("offset must be non-negative")
    selected = candidates[offset:]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    chunks = _rule_chunks(rules_root)
    packets: list[dict[str, Any]] = []
    selected_rule_hashes: dict[str, str] = {}
    for raw_candidate in selected:
        candidate = dict(raw_candidate)
        company = candidate.pop("company", {})
        type_keys = candidate.get("type_keys")
        if not isinstance(type_keys, list) or not type_keys:
            type_keys = [candidate["type_key"]]
        rule_context: list[dict[str, str]] = []
        seen_rules: set[tuple[str, str, str]] = set()
        for type_key in type_keys:
            for rule in _relevant_rules(chunks, str(type_key)):
                identity = (rule["source_id"], rule["line_start"], rule["heading"])
                if identity in seen_rules:
                    continue
                seen_rules.add(identity)
                rule_context.append(rule)
        for rule in rule_context:
            source_id = rule["source_id"]
            digest = rule["source_sha256"]
            existing = selected_rule_hashes.setdefault(source_id, digest)
            if existing != digest:
                raise ValueError(f"rule source has conflicting hashes: {source_id}")
        packets.append(
            {
                **candidate,
                "generation": snapshot_generation,
                "market_as_of": snapshot_market_as_of,
                "rule_context": rule_context,
                "company_context": _compact_company(company, type_keys, market_as_of=snapshot_market_as_of),
                "ai_review": None,
            }
        )
    selected_type_pair_count = sum(int(packet.get("type_pair_count", 1) or 1) for packet in packets)
    queue_full_coverage = (
        bool(candidates)
        and offset == 0
        and len(packets) == len(candidates)
        and selected_type_pair_count == len(candidate_pairs)
    )
    candidate_identity_digest = candidate_identity_sha256(packets)
    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "snapshot_generation": snapshot_generation,
        "market_as_of": snapshot_market_as_of,
        "methodology_version": snapshot.get("methodology_version"),
        "index_contract": snapshot.get("index_contract"),
        "rules_root": str(rules_root),
        "rule_file_count": len(selected_rule_hashes),
        "rule_source_sha256": dict(sorted(selected_rule_hashes.items())),
        "candidate_count": len(packets),
        "candidate_total": len(candidates),
        "type_pair_candidate_count": selected_type_pair_count,
        "type_pair_candidate_total": len(candidate_pairs),
        "type_pair_candidate_identity_sha256": type_pair_universe_identity_sha256,
        "candidate_offset": offset,
        "candidate_identity_sha256": candidate_identity_digest,
        "candidate_universe_identity_sha256": candidate_universe_sha256,
        "queue_full_coverage": queue_full_coverage,
        "review_mode": review_mode,
        "full_coverage_final_recommendation": (review_mode in _FULL_COVERAGE_REVIEW_MODES and queue_full_coverage),
        "packets": packets,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = out_dir / "ai-screening-input.json"
    atomic_write_text(input_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    atomic_write_text(
        out_dir / "ai-screening-candidates.jsonl",
        "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n" for packet in packets),
    )
    manifest = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "snapshot_generation": payload["snapshot_generation"],
        "market_as_of": payload["market_as_of"],
        "methodology_version": payload["methodology_version"],
        "index_contract": payload["index_contract"],
        "candidate_count": len(packets),
        "candidate_total": len(candidates),
        "type_pair_candidate_count": payload["type_pair_candidate_count"],
        "type_pair_candidate_total": payload["type_pair_candidate_total"],
        "type_pair_candidate_identity_sha256": payload["type_pair_candidate_identity_sha256"],
        "candidate_offset": offset,
        "candidate_identity_sha256": payload["candidate_identity_sha256"],
        "candidate_universe_identity_sha256": payload["candidate_universe_identity_sha256"],
        "queue_full_coverage": payload["queue_full_coverage"],
        "review_mode": payload["review_mode"],
        "full_coverage_final_recommendation": payload["full_coverage_final_recommendation"],
        "rule_file_count": payload["rule_file_count"],
        "rule_source_sha256": payload["rule_source_sha256"],
        "input_sha256": _sha256_bytes(input_path.read_bytes()),
    }
    atomic_write_text(
        out_dir / "ai-screening-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return manifest


def merge_reviews(
    input_path: Path,
    review_path: Path,
    out_path: Path,
    *,
    research_path: Path | None = None,
    knowledge_path: Path | None = None,
    protocol_path: Path | None = None,
    research_as_of: str | None = None,
) -> None:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    company_research_review = payload.get("review_mode") == "opencode_native_company_research_review"
    if research_as_of and not company_research_review:
        payload["research_as_of"] = str(research_as_of)
    reviews: dict[tuple[str, str], dict[str, Any]] = {}
    for line in review_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        review = json.loads(line)
        # The merge step owns candidate identity and provenance continuity;
        # the publication validator owns the larger native-research envelope
        # (valuation snapshot, score components and evidence bindings).  Keep
        # this stage aligned with the JSONL shard mergers so a provenance test
        # can report its actual missing binding instead of a long secondary
        # schema list.  Native reviews are still rejected by
        # ``build_artifact``/``validate_ai_screening_public`` before release.
        if company_research_review and not isinstance(review.get("_research_provenance"), Mapping):
            raise ValueError("company review is missing _research_provenance")
        errors = validate_review(review)
        if errors:
            raise ValueError(f"invalid review: {','.join(errors)}")
        key = (str(review.get("security_code")), str(review.get("type_key")))
        if key in reviews:
            raise ValueError(f"duplicate review: {key}")
        reviews[key] = review
    packet_keys = {
        (str(packet.get("security_code")), str(packet.get("type_key"))) for packet in payload.get("packets", [])
    }
    extra = sorted(set(reviews) - packet_keys)
    if extra:
        raise ValueError(f"reviews outside candidate queue: {extra[:8]}")

    if payload.get("review_mode") == "opencode_native_company_research_review":
        if not all((research_path, knowledge_path, protocol_path, research_as_of)):
            raise ValueError(
                "company research merge requires research, knowledge, protocol and research_as_of provenance inputs"
            )
        context = load_research_provenance_context(
            input_path,
            research_path,  # type: ignore[arg-type]
            knowledge_path,  # type: ignore[arg-type]
            protocol_path,  # type: ignore[arg-type]
            research_as_of=str(research_as_of),
        )
        validate_review_set_provenance(list(reviews.values()), context)
    for packet in payload.get("packets", []):
        key = (str(packet.get("security_code")), str(packet.get("type_key")))
        packet["ai_review"] = reviews.get(key)
    if payload.get("full_coverage_final_recommendation") is True:
        missing = [
            (str(packet.get("security_code")), str(packet.get("type_key")))
            for packet in payload.get("packets", [])
            if packet.get("ai_review") is None
        ]
        if missing:
            raise ValueError(f"full-coverage candidates missing reviews: {missing[:3]}")
    atomic_write_text(out_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--rules-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--market-as-of")
    parser.add_argument("--generation")
    parser.add_argument("--review-mode")
    parser.add_argument("--review-jsonl", type=Path)
    parser.add_argument("--research", type=Path)
    parser.add_argument("--knowledge", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--research-as-of")
    args = parser.parse_args()
    build_input(
        args.snapshot,
        args.rules_root,
        args.out,
        limit=args.limit,
        offset=args.offset,
        market_as_of=args.market_as_of,
        generation=args.generation,
        review_mode=args.review_mode,
    )
    if args.review_jsonl:
        merge_reviews(
            args.out / "ai-screening-input.json",
            args.review_jsonl,
            args.out / "ai-screening.json",
            research_path=args.research,
            knowledge_path=args.knowledge,
            protocol_path=args.protocol,
            research_as_of=args.research_as_of,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
