from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.shard_ai_screening_candidates import shard_candidates


def _candidate(code: str, type_key: str) -> dict[str, str]:
    return {"security_code": code, "type_key": type_key, "name": f"公司{code}"}


def _write_jsonl(path: Path, records: list[dict[str, str]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _identity_sha256(identities: list[tuple[str, str]]) -> str:
    canonical = "".join(f"{code}\t{type_key}\n" for code, type_key in identities)
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_shards_keep_company_pairs_adjacent_and_balance_pair_counts(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    records = [
        _candidate("A", "type1"),
        _candidate("B", "type1"),
        _candidate("A", "type2"),
        _candidate("C", "type1"),
        _candidate("B", "type2"),
        _candidate("A", "type3"),
        _candidate("D", "type1"),
        _candidate("C", "type2"),
    ]
    _write_jsonl(candidates, records)

    manifest = shard_candidates(candidates, tmp_path / "out", 2)

    assert manifest["company_count"] == 4
    assert manifest["type_pair_count"] == 8
    assert [shard["type_pair_count"] for shard in manifest["shards"]] == [4, 4]
    assert [shard["company_count"] for shard in manifest["shards"]] == [2, 2]
    all_identities: list[tuple[str, str]] = []
    code_to_shard: dict[str, int] = {}
    for shard in manifest["shards"]:
        shard_records = _read_jsonl(tmp_path / "out" / shard["file"])
        shard_codes = [record["security_code"] for record in shard_records]
        for code in set(shard_codes):
            positions = [index for index, value in enumerate(shard_codes) if value == code]
            assert positions == list(range(min(positions), max(positions) + 1))
            assert code not in code_to_shard
            code_to_shard[code] = shard["shard_index"]
        identities = [(record["security_code"], record["type_key"]) for record in shard_records]
        all_identities.extend(identities)
        assert shard["ordered_identity_sha256"] == _identity_sha256(identities)
    assert set(all_identities) == {(record["security_code"], record["type_key"]) for record in records}
    source_identities = [(record["security_code"], record["type_key"]) for record in records]
    assert manifest["ordered_identity_sha256"] == _identity_sha256(source_identities)
    assert json.loads((tmp_path / "out" / "candidate-shards-manifest.json").read_text()) == manifest


def test_sharding_is_deterministic(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    _write_jsonl(
        candidates,
        [
            _candidate("A", "type1"),
            _candidate("A", "type2"),
            _candidate("B", "type1"),
            _candidate("C", "type1"),
        ],
    )

    first = shard_candidates(candidates, tmp_path / "first", 2)
    second = shard_candidates(candidates, tmp_path / "second", 2)

    assert first == second
    for shard in first["shards"]:
        assert (tmp_path / "first" / shard["file"]).read_bytes() == (tmp_path / "second" / shard["file"]).read_bytes()


def test_shards_reject_duplicate_company_type_identity(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    _write_jsonl(
        candidates,
        [_candidate("A", "type1"), _candidate("B", "type1"), _candidate("A", "type1")],
    )

    with pytest.raises(ValueError, match="duplicate candidate identity"):
        shard_candidates(candidates, tmp_path / "out", 2)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "records",
    [
        [{"security_code": "A", "type_key": ""}],
        [{"security_code": "", "type_key": "type1"}],
    ],
)
def test_shards_reject_incomplete_identity(tmp_path: Path, records: list[dict[str, str]]) -> None:
    candidates = tmp_path / "candidates.jsonl"
    _write_jsonl(candidates, records)

    with pytest.raises(ValueError, match="identity is incomplete"):
        shard_candidates(candidates, tmp_path / "out", 1)


def test_shards_reject_more_shards_than_companies(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    _write_jsonl(candidates, [_candidate("A", "type1"), _candidate("B", "type1")])

    with pytest.raises(ValueError, match="exceeds company count"):
        shard_candidates(candidates, tmp_path / "out", 3)
