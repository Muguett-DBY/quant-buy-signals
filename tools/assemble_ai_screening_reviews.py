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
    claims = review.get("claims")
    if performed and event_verified and claims_verified:
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
            if str(review.get("model") or "") != expected_model:
                raise ValueError(
                    f"review {key} in {path.name} uses {review.get('model')!r}, expected {expected_model!r}"
                )
            # Paths are supplied in precedence order.  A verified search
            # result can replace a local result even if it appears later.
            previous = chosen.get(key)
            if previous is None or (previous[0] == "local" and kind == "search"):
                chosen[key] = (kind, review)

    missing = [key for key in expected if key not in chosen]
    if missing:
        preview = ", ".join(f"{code}/{type_key}" for code, type_key in missing[:12])
        raise ValueError(f"review queue is incomplete: {len(missing)} missing ({preview})")

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
    args = parser.parse_args()
    counts = assemble(args.candidates, args.reviews, args.out, expected_model=args.model)
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
