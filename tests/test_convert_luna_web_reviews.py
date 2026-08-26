from __future__ import annotations

import json

import pytest

from tools.convert_luna_web_reviews import _load_rows, _sanitize_reason_text, convert
from tools.ai_screening_contract import validate_review


def _queue() -> dict:
    return {
        "snapshot_generation": "a" * 16,
        "market_as_of": "2026-08-25",
        "queue_full_coverage": True,
        "candidate_total": 1,
        "type_pair_candidate_total": 1,
        "candidate_offset": 0,
        "packets": [
            {
                "security_code": "600000",
                "name": "测试公司",
                "type_key": "type1",
                "type_keys": ["type1"],
                "type_pair_count": 1,
                "candidate_types": [{"type_key": "type1"}],
            }
        ],
    }


def _row(score: int = 72, decision: str = "recommend_buy") -> dict:
    return {
        "code": "600000",
        "name": "测试公司",
        "index": 0,
        "decision": decision,
        "score": score,
        "summary": "2026年一季度收入和经营现金流保持正向，估值有缓冲，但行业需求仍需跟踪。",
        "buy_reasons": ["2026年一季度收入 120 亿元，经营现金流 18 亿元。"],
        "risks": ["行业需求波动可能压低利润。"],
        "financial_facts": [
            {"period": "2026Q1", "fact": "营业收入 120 亿元，归母净利润 9 亿元。"},
            {"period": "2025FY", "fact": "经营活动现金流 18 亿元，自由现金流 10 亿元。"},
        ],
        "sources": [
            {
                "url": "https://example.com/report",
                "title": "2026年一季报",
                "date": "2026-04-30",
                "key_facts": "2026年一季度收入和利润披露。",
            },
            {
                "url": "https://example.com/industry",
                "title": "行业公告",
                "date": "2026-08-20",
                "key_facts": "行业供需与竞争情况。",
            },
        ],
        "search_queries": ["600000 2026 一季报", "600000 行业竞争"],
        "research_as_of": "2026-08-26",
        "evidence_quality": "high",
    }


def test_convert_sets_generation_bound_luna_web_mode(tmp_path) -> None:
    input_path = tmp_path / "input.json"
    shard_path = tmp_path / "shard.jsonl"
    output_path = tmp_path / "merged.json"
    input_path.write_text(json.dumps(_queue(), ensure_ascii=False), encoding="utf-8")
    shard_path.write_text(json.dumps(_row(), ensure_ascii=False) + "\n", encoding="utf-8")

    result = convert(input_path, [shard_path], output_path)

    assert result == {"candidate_total": 1, "reviewed": 1, "recommend_buy": 1}
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["review_mode"] == "codex_luna_web_review"
    assert payload["full_coverage_final_recommendation"] is True
    review = payload["packets"][0]["ai_review"]
    assert review["model"] == "codex-luna-max"
    assert review["web_search_event_verified"] is True
    assert review["claims"][0]["source_ref"].startswith("https://")
    assert validate_review(review, require_readable_reason=True) == []


def test_convert_rejects_buy_score_in_observe_band(tmp_path) -> None:
    input_path = tmp_path / "input.json"
    shard_path = tmp_path / "shard.jsonl"
    input_path.write_text(json.dumps(_queue(), ensure_ascii=False), encoding="utf-8")
    bad = _row(score=69, decision="recommend_buy")
    shard_path.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="recommend_buy score"):
        convert(input_path, [shard_path], tmp_path / "merged.json")


def test_convert_rejects_duplicate_shard_index(tmp_path) -> None:
    first = _row()
    second = _row()
    second["code"] = "000001"
    second["name"] = "另一家公司"
    input_path = tmp_path / "input.json"
    shard_path = tmp_path / "shard.jsonl"
    input_path.write_text(json.dumps(_queue(), ensure_ascii=False), encoding="utf-8")
    shard_path.write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in (first, second)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate Luna review index"):
        convert(input_path, [shard_path], tmp_path / "merged.json")


def test_load_rows_allows_explicit_same_identity_correction(tmp_path) -> None:
    first = _row()
    correction = _row()
    correction["correction"] = True
    correction["summary"] = "修正版：来源已重新核对。"
    path = tmp_path / "shard.jsonl"
    path.write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in (first, correction)) + "\n",
        encoding="utf-8",
    )

    rows = _load_rows([path])

    assert rows["600000"]["correction"] is True
    assert rows["600000"]["summary"].startswith("修正版")


def test_sanitize_reason_text_removes_pool_labels_but_keeps_company_facts() -> None:
    value = "type2/type5仅是研究索引，不构成买入理由；2026H1收入同比增长32.6%。"

    result = _sanitize_reason_text(value)

    assert "type2" not in result.lower()
    assert "type5" not in result.lower()
    assert "研究索引" not in result
    assert "2026H1收入同比增长32.6%" in result
