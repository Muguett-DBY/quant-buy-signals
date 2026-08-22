"""Merge AI-screening review shards in deterministic candidate order.

Every candidate identity (``security_code``, ``type_key``) must occur exactly
once across the review shards.  The merged JSONL follows the candidates JSONL
order so downstream calibration and publication remain reproducible.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.ai_screening_contract import validate_review


Identity = tuple[str, str]


def _read_jsonl(path: Path, *, label: str) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid {label} JSON at {path}:{line_number}: {error.msg}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label} record must be an object at {path}:{line_number}")
        records.append((line_number, value))
    return records


def _identity(record: Mapping[str, Any], *, path: Path, line_number: int, label: str) -> Identity:
    security_code = str(record.get("security_code") or "").strip()
    type_key = str(record.get("type_key") or "").strip()
    if not security_code or not type_key:
        raise ValueError(f"{label} identity is incomplete at {path}:{line_number}")
    return security_code, type_key


def merge_review_shards(candidates_path: Path, review_paths: Sequence[Path], out_path: Path) -> int:
    """Validate and merge review shards, returning the written record count."""

    if not review_paths:
        raise ValueError("at least one review shard is required")

    candidate_order: list[Identity] = []
    candidate_locations: dict[Identity, tuple[Path, int]] = {}
    for line_number, candidate in _read_jsonl(candidates_path, label="candidate"):
        identity = _identity(candidate, path=candidates_path, line_number=line_number, label="candidate")
        if identity in candidate_locations:
            first_path, first_line = candidate_locations[identity]
            raise ValueError(
                f"duplicate candidate identity {identity} at {candidates_path}:{line_number}; "
                f"first seen at {first_path}:{first_line}"
            )
        candidate_locations[identity] = (candidates_path, line_number)
        candidate_order.append(identity)

    reviews: dict[Identity, dict[str, Any]] = {}
    review_locations: dict[Identity, tuple[Path, int]] = {}
    for review_path in review_paths:
        for line_number, review in _read_jsonl(review_path, label="review"):
            errors = validate_review(review)
            if errors:
                raise ValueError(f"invalid review at {review_path}:{line_number}: {','.join(errors)}")
            identity = _identity(review, path=review_path, line_number=line_number, label="review")
            if identity in review_locations:
                first_path, first_line = review_locations[identity]
                raise ValueError(
                    f"duplicate review identity {identity} at {review_path}:{line_number}; "
                    f"first seen at {first_path}:{first_line}"
                )
            review_locations[identity] = (review_path, line_number)
            reviews[identity] = review

    expected = set(candidate_order)
    actual = set(reviews)
    missing = [identity for identity in candidate_order if identity not in actual]
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(f"review identity mismatch: missing={missing} extra={extra}")

    ordered_reviews = [reviews[identity] for identity in candidate_order]
    if len(ordered_reviews) != len(candidate_order):
        raise ValueError(f"merged review count mismatch: expected={len(candidate_order)} actual={len(ordered_reviews)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n" for review in ordered_reviews),
        encoding="utf-8",
    )
    return len(ordered_reviews)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    count = merge_review_shards(args.candidates, args.reviews, args.out)
    print(json.dumps({"merged_review_count": count, "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
