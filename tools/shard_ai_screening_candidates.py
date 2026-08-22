"""Split AI-screening candidates into company-safe, balanced JSONL shards.

All type pairs for one ``security_code`` stay together and adjacent.  Company
groups are assigned largest-first to the shard with the fewest type pairs;
ties use the shard index, making the allocation deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


Identity = tuple[str, str]


@dataclass(frozen=True)
class CompanyGroup:
    security_code: str
    first_index: int
    records: tuple[dict[str, Any], ...]


def _identity(record: Mapping[str, Any], *, path: Path, line_number: int) -> Identity:
    security_code = str(record.get("security_code") or "").strip()
    type_key = str(record.get("type_key") or "").strip()
    if not security_code or not type_key:
        raise ValueError(f"candidate identity is incomplete at {path}:{line_number}")
    return security_code, type_key


def _identity_sha256(identities: Sequence[Identity]) -> str:
    canonical = "".join(f"{security_code}\t{type_key}\n" for security_code, type_key in identities)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_groups(path: Path) -> tuple[list[Identity], list[CompanyGroup]]:
    identities: list[Identity] = []
    seen: set[Identity] = set()
    grouped: dict[str, list[dict[str, Any]]] = {}
    first_indices: dict[str, int] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid candidate JSON at {path}:{line_number}: {error.msg}") from error
        if not isinstance(record, dict):
            raise ValueError(f"candidate record must be an object at {path}:{line_number}")
        identity = _identity(record, path=path, line_number=line_number)
        if identity in seen:
            raise ValueError(f"duplicate candidate identity {identity} at {path}:{line_number}")
        seen.add(identity)
        identities.append(identity)
        security_code = identity[0]
        first_indices.setdefault(security_code, len(identities) - 1)
        grouped.setdefault(security_code, []).append(record)
    if not identities:
        raise ValueError("candidate JSONL is empty")
    groups = [CompanyGroup(code, first_indices[code], tuple(records)) for code, records in grouped.items()]
    groups.sort(key=lambda group: (-len(group.records), group.first_index, group.security_code))
    return identities, groups


def shard_candidates(candidates_path: Path, out_dir: Path, shard_count: int) -> dict[str, Any]:
    """Write balanced candidate shards and return their integrity manifest."""

    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    source_identities, groups = _load_groups(candidates_path)
    if shard_count > len(groups):
        raise ValueError(f"shard_count exceeds company count: {shard_count} > {len(groups)}")

    shard_groups: list[list[CompanyGroup]] = [[] for _ in range(shard_count)]
    shard_pair_counts = [0] * shard_count
    for group in groups:
        shard_index = min(range(shard_count), key=lambda index: (shard_pair_counts[index], index))
        shard_groups[shard_index].append(group)
        shard_pair_counts[shard_index] += len(group.records)

    out_dir.mkdir(parents=True, exist_ok=True)
    shard_manifest: list[dict[str, Any]] = []
    sharded_identities: list[Identity] = []
    for shard_index, assigned_groups in enumerate(shard_groups):
        records = [record for group in assigned_groups for record in group.records]
        identities = [(str(record["security_code"]).strip(), str(record["type_key"]).strip()) for record in records]
        sharded_identities.extend(identities)
        file_name = f"candidates-shard-{shard_index:03d}-of-{shard_count:03d}.jsonl"
        shard_path = out_dir / file_name
        shard_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        shard_manifest.append(
            {
                "shard_index": shard_index,
                "file": file_name,
                "company_count": len(assigned_groups),
                "type_pair_count": len(records),
                "ordered_identity_sha256": _identity_sha256(identities),
            }
        )

    if len(sharded_identities) != len(source_identities) or set(sharded_identities) != set(source_identities):
        raise ValueError("sharded candidate coverage does not match the source candidates")
    manifest = {
        "schema_version": 1,
        "source_file": candidates_path.name,
        "shard_count": shard_count,
        "company_count": len(groups),
        "type_pair_count": len(source_identities),
        "identity_sha256_format": "security_code\\ttype_key\\n",
        "ordered_identity_sha256": _identity_sha256(source_identities),
        "sharded_ordered_identity_sha256": _identity_sha256(sharded_identities),
        "shards": shard_manifest,
    }
    (out_dir / "candidate-shards-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    args = parser.parse_args(argv)
    manifest = shard_candidates(args.candidates, args.out_dir, args.shards)
    print(
        json.dumps(
            {
                "company_count": manifest["company_count"],
                "type_pair_count": manifest["type_pair_count"],
                "shard_count": manifest["shard_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
