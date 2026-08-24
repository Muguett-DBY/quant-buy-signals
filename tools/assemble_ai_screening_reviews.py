"""Assemble a complete, generation-bound AI review queue from batch outputs.

The batch runner is intentionally resumable, so a refresh can have several
partial JSONL files.  This utility makes the final choice explicit: verified
OpenCode-search reviews win, and an unsearched Ox Alpha Free review is only a
declared local fallback.  It never fabricates search evidence or silently
accepts an incomplete queue.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.ai_company_research_provenance import (
    ResearchProvenanceContext,
    load_research_provenance_context,
    validate_review_provenance,
    validate_review_set_provenance,
)
from tools.ai_screening_contract import validate_review


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not an object")
        rows.append(value)
    return rows


def _identity(value: Mapping[str, Any]) -> tuple[str, str]:
    code = str(value.get("security_code") or "").strip()
    type_key = str(value.get("type_key") or "").strip()
    if not code or not type_key:
        raise ValueError("review identity is incomplete")
    return code, type_key


def _review_kind(review: Mapping[str, Any]) -> str:
    performed = review.get("web_search_performed") is True
    event_verified = review.get("web_search_event_verified") is True
    claims_verified = review.get("web_search_claim_urls_verified") is True
    research_sources_verified = review.get("research_source_urls_verified") is True
    claims = review.get("claims")
    company_research = isinstance(review.get("_research_provenance"), Mapping)
    if performed and event_verified and (claims_verified or (company_research and research_sources_verified)):
        queries = review.get("web_search_queries")
        if not isinstance(queries, list) or not queries:
            raise ValueError("search review has no bound search query")
        return "search"
    if not performed and not event_verified and not claims_verified and claims == []:
        return "local"
    raise ValueError("review has neither complete search proof nor the local fallback shape")


def assemble(
    candidates_path: Path,
    review_paths: list[Path],
    output_path: Path,
    *,
    expected_model: str = "opencode-go/ox-alpha-free",
    provenance_input_path: Path | None = None,
    research_path: Path | None = None,
    knowledge_path: Path | None = None,
    protocol_path: Path | None = None,
    research_as_of: str | None = None,
) -> dict[str, int]:
    candidates = _read_jsonl(candidates_path)
    expected: list[tuple[str, str]] = []
    expected_set: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = _identity(candidate)
        if key in expected_set:
            raise ValueError(f"duplicate candidate identity: {key}")
        expected.append(key)
        expected_set.add(key)

    provenance_context: ResearchProvenanceContext | None = None
    provenance_values = (
        provenance_input_path,
        research_path,
        knowledge_path,
        protocol_path,
        research_as_of,
    )
    if any(value is not None for value in provenance_values):
        if not all(value is not None for value in provenance_values):
            raise ValueError("complete company research provenance inputs are required")
        provenance_context = load_research_provenance_context(
            provenance_input_path,  # type: ignore[arg-type]
            research_path,  # type: ignore[arg-type]
            knowledge_path,  # type: ignore[arg-type]
            protocol_path,  # type: ignore[arg-type]
            research_as_of=str(research_as_of),
        )
        if expected_set != set(provenance_context.packets):
            raise ValueError("candidate JSONL does not match the provenance candidate universe")

    chosen: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for path in review_paths:
        for review in _read_jsonl(path):
            key = _identity(review)
            if key not in expected_set:
                continue
            errors = validate_review(review)
            if errors:
                raise ValueError(f"invalid review {key} in {path.name}: {','.join(errors)}")
            kind = _review_kind(review)
            has_provenance = isinstance(review.get("_research_provenance"), Mapping)
            if has_provenance and provenance_context is None:
                raise ValueError("company research review cannot be assembled without provenance inputs")
            if provenance_context is not None:
                validate_review_provenance(review, provenance_context.packets[key], provenance_context)
            elif str(review.get("model") or "") != expected_model:
                raise ValueError(
                    f"review {key} in {path.name} uses {review.get('model')!r}, expected {expected_model!r}"
                )
            # Paths are supplied in precedence order.  A verified search
            # result can replace a local result even if it appears later.
            previous = chosen.get(key)
            if previous is None or (previous[0] == "local" and kind == "search"):
                chosen[key] = (kind, review)
            elif previous[0] == "search" and kind == "search":
                raise ValueError(f"duplicate searched review identity: {key}")

    missing = [key for key in expected if key not in chosen]
    if missing:
        preview = ", ".join(f"{code}/{type_key}" for code, type_key in missing[:12])
        raise ValueError(f"review queue is incomplete: {len(missing)} missing ({preview})")
    if provenance_context is not None:
        validate_review_set_provenance([chosen[key][1] for key in expected], provenance_context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for key in expected:
            handle.write(json.dumps(chosen[key][1], ensure_ascii=False, sort_keys=True) + "\n")
    counts = {"candidate_total": len(expected), "search": 0, "local": 0}
    for kind, _review in chosen.values():
        counts[kind] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="opencode-go/ox-alpha-free")
    parser.add_argument("--provenance-input", type=Path)
    parser.add_argument("--research", type=Path)
    parser.add_argument("--knowledge", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--research-as-of")
    args = parser.parse_args()
    counts = assemble(
        args.candidates,
        args.reviews,
        args.out,
        expected_model=args.model,
        provenance_input_path=args.provenance_input,
        research_path=args.research,
        knowledge_path=args.knowledge,
        protocol_path=args.protocol,
        research_as_of=args.research_as_of,
    )
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
