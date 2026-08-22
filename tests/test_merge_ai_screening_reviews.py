from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.merge_ai_screening_reviews import merge_review_shards


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _candidate(code: str, type_key: str) -> dict[str, object]:
    return {"security_code": code, "type_key": type_key, "name": f"公司{code}"}


def _review(code: str, type_key: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "security_code": code,
        "type_key": type_key,
        "verdict": "confirmed",
        "recommended_action": "keep",
        "buy_attractiveness_score": 65,
        "ai_action": "priority_buy",
        "confidence": "medium",
        "summary": "当前证据与类型规则一致。",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [],
    }


def test_merge_orders_multiple_shards_by_candidate_queue(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    shard_a = tmp_path / "part-a.jsonl"
    shard_b = tmp_path / "part-b.jsonl"
    output = tmp_path / "merged.jsonl"
    expected = [("600001", "type1"), ("000002", "type5"), ("300003", "type7")]
    _write_jsonl(candidates, [_candidate(*identity) for identity in expected])
    _write_jsonl(shard_a, [_review("300003", "type7"), _review("600001", "type1")])
    _write_jsonl(shard_b, [_review("000002", "type5")])

    assert merge_review_shards(candidates, [shard_a, shard_b], output) == 3
    merged = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [(item["security_code"], item["type_key"]) for item in merged] == expected
    assert output.read_bytes().endswith(b"\n")


def test_merge_rejects_duplicate_candidate_identity(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    shard = tmp_path / "part.jsonl"
    _write_jsonl(candidates, [_candidate("600001", "type1"), _candidate("600001", "type1")])
    _write_jsonl(shard, [_review("600001", "type1")])

    with pytest.raises(ValueError, match="duplicate candidate identity"):
        merge_review_shards(candidates, [shard], tmp_path / "merged.jsonl")


def test_merge_rejects_duplicate_review_across_shards(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    shard_a = tmp_path / "part-a.jsonl"
    shard_b = tmp_path / "part-b.jsonl"
    _write_jsonl(candidates, [_candidate("600001", "type1")])
    _write_jsonl(shard_a, [_review("600001", "type1")])
    _write_jsonl(shard_b, [_review("600001", "type1")])

    with pytest.raises(ValueError, match="duplicate review identity"):
        merge_review_shards(candidates, [shard_a, shard_b], tmp_path / "merged.jsonl")


@pytest.mark.parametrize(
    ("reviews", "error_fragment"),
    [
        ([_review("600001", "type1")], "missing=\\[\\('000002', 'type5'\\)\\]"),
        (
            [_review("600001", "type1"), _review("000002", "type5"), _review("300003", "type7")],
            "extra=\\[\\('300003', 'type7'\\)\\]",
        ),
    ],
)
def test_merge_rejects_missing_or_extra_reviews(
    tmp_path: Path, reviews: list[dict[str, object]], error_fragment: str
) -> None:
    candidates = tmp_path / "candidates.jsonl"
    shard = tmp_path / "part.jsonl"
    output = tmp_path / "merged.jsonl"
    _write_jsonl(candidates, [_candidate("600001", "type1"), _candidate("000002", "type5")])
    _write_jsonl(shard, reviews)

    with pytest.raises(ValueError, match=error_fragment):
        merge_review_shards(candidates, [shard], output)
    assert not output.exists()


def test_merge_rejects_invalid_review_before_writing(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    shard = tmp_path / "part.jsonl"
    output = tmp_path / "merged.jsonl"
    invalid = _review("600001", "type1")
    invalid["buy_attractiveness_score"] = 100
    invalid["ai_action"] = "avoid"
    _write_jsonl(candidates, [_candidate("600001", "type1")])
    _write_jsonl(shard, [invalid])

    with pytest.raises(ValueError, match="negative_score_band"):
        merge_review_shards(candidates, [shard], output)
    assert not output.exists()
