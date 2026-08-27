from __future__ import annotations

import json

import pytest

from tools.convert_luna_web_reviews import (
    _claims,
    _financial_fact_bindings,
    _financial_fact_items,
    _inferred_fact_unit,
    _load_rows,
    _repair_fact_unit_mentions,
    _review,
    _sanitize_reason_text,
    convert,
)
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
    assert [item["period"] for item in review["financial_fact_bindings"]] == ["2026Q1", "2025FY"]
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


def test_financial_fact_projection_preserves_period_dates_units_and_source_edges() -> None:
    row = _row()
    second_source = row["sources"][1]["url"]
    fact_source = "https://example.com/financial-fact"
    row["financial_facts"] = [
        {
            "period": "2026H1",
            "date": "2026-06-30",
            "source_date": "2026-08-18",
            "metric": "营业收入",
            "value": 123.4,
            "unit": "亿元",
            "source_urls": [fact_source],
        },
        {
            "date": "2025-12-31",
            "metric": "经营现金流",
            "value": 10,
            "unit": "亿元",
            "source_index": 1,
        },
    ]

    normalized = _financial_fact_items(row)
    assert normalized[0]["period"] == "2026H1"
    assert normalized[0]["date"] == "2026-06-30"
    assert normalized[0]["source_date"] == "2026-08-18"
    assert "123.4亿元" in normalized[0]["fact"]
    assert normalized[0]["source_url"] == fact_source
    assert normalized[1]["source_url"] == second_source

    bindings = _financial_fact_bindings(row)
    assert bindings[0]["date"] == "2026-06-30"
    assert bindings[0]["period"] == "2026H1"
    assert bindings[0]["value"] == 123.4
    assert bindings[0]["unit"] == "亿元"
    assert bindings[0]["source_url"] == fact_source
    assert bindings[1]["source_url"] == second_source

    claims, _urls = _claims(row)
    value_claims = [claim for claim in claims if "123.4亿元" in claim["statement"]]
    assert len(value_claims) == 1
    assert value_claims[0]["source_ref"] == fact_source

    review = _review(_queue()["packets"][0], row, market_as_of="2026-08-25")
    assert review["financial_fact_bindings"][0]["value"] == 123.4
    assert review["financial_fact_bindings"][0]["source_url"] == fact_source


def test_financial_fact_source_cannot_cross_company() -> None:
    row = _row()
    row["financial_facts"][0]["source_url"] = "https://example.com/report?stockid=000001"

    with pytest.raises(ValueError, match="belongs to another company"):
        _claims(row)


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


def test_sanitize_reason_text_removes_trigger_and_threshold_labels() -> None:
    value = "类型1接近达标；类型2未触发；类型3未达标；类型4已触发。"

    result = _sanitize_reason_text(value)

    assert "类型1" not in result
    assert "类型2" not in result
    assert "类型3" not in result
    assert "类型4" not in result
    assert "接近达标" not in result
    assert "未触发" not in result
    assert "未达标" not in result
    assert "已触发" not in result
    assert "基本面尚未完全确认" in result
    assert "证据与安全边际不足" in result
    assert "尚未形成独立确认" in result
    assert "已进入研究范围" in result


def test_date_years_ignores_numeric_values_that_are_not_report_periods() -> None:
    from tools.convert_luna_web_reviews import _date_years

    row = {
        "financial_facts": [
            {"period": "2026H1", "fact": "收入2094.00万元，同比增长20.26%"},
            {"period": "2025FY", "fact": "经营现金流100000000元"},
        ],
        "sources": [{"date": "2026-08-26"}],
    }

    assert _date_years(row, 2026) == [2025, 2026]


def test_repair_fact_unit_mentions_corrects_obvious_tenfold_amount_error() -> None:
    row = _row()
    row["financial_facts"] = [
        {
            "date": "2026-06-30",
            "metric": "营业收入",
            "value": 3726842297.78,
            "unit": "元",
            "source_url": "https://example.com/603444-report",
        }
    ]

    repaired, records = _repair_fact_unit_mentions(
        "2026年上半年营业收入372.68亿元。",
        row,
        field="summary",
    )

    assert repaired == "2026年上半年营业收入37.27亿元。"
    assert records == [
        {
            "field": "summary",
            "metric": "营业收入",
            "period": "2026-06-30",
            "old": "372.68亿元",
            "new": "37.27亿元",
            "unit": "元",
            "source_url": "https://example.com/603444-report",
        }
    ]


def test_repair_fact_unit_mentions_matches_short_revenue_alias_in_summary() -> None:
    row = _row()
    row["financial_facts"] = [
        {
            "date": "2026-06-30",
            "metric": "营业收入",
            "value": 3726842297.78,
            "unit": "元",
            "source_url": "https://example.com/603444-report",
        }
    ]

    repaired, records = _repair_fact_unit_mentions(
        "2026年上半年收入372.68亿元，利润仍需核验。",
        row,
        field="summary",
    )

    assert repaired == "2026年上半年收入37.27亿元，利润仍需核验。"
    assert records and records[0]["field"] == "summary"


def test_repair_fact_unit_mentions_does_not_rewrite_percentages() -> None:
    row = _row()
    row["financial_facts"] = [
        {
            "date": "2026-06-30",
            "metric": "营业收入同比增长率",
            "value": 48.01,
            "unit": "%",
            "source_url": "https://example.com/603444-report",
        }
    ]

    original = "2026年上半年营业收入同比增长率480.1%。"

    repaired, records = _repair_fact_unit_mentions(original, row, field="summary")

    assert repaired == original
    assert records == []


def test_review_applies_fact_scale_repairs_to_summary_strengths_and_risks() -> None:
    row = _row()
    row["summary"] = "2026年上半年收入372.68亿元。"
    row["buy_reasons"] = ["2026年上半年营业收入372.68亿元。"]
    row["risks"] = ["2026年上半年营业收入372.68亿元但仍需跟踪。"]
    row["financial_facts"] = [
        {
            "date": "2026-06-30",
            "metric": "营业收入",
            "value": 3726842297.78,
            "unit": "元",
            "source_url": "https://example.com/report",
        }
    ]

    review = _review(_queue()["packets"][0], row, market_as_of="2026-08-25")

    assert "37.27亿元" in review["summary"]
    assert "37.27亿元" in review["key_strengths"][0]
    assert "37.27亿元" in review["risk_flags"][0]
    assert {record["field"] for record in review["numeric_fact_repairs"]} == {
        "summary",
        "key_strengths",
        "risk_flags",
    }


def test_numeric_repair_handles_chinese_suffix_and_skips_per_share() -> None:
    row = _row()
    row["financial_facts"] = [
        {
            "date": "2026-06-30",
            "metric": "营业收入",
            "value": 3726842297.78,
            "unit": "元",
            "source_url": "https://example.com/fact",
        }
    ]
    repaired, records = _repair_fact_unit_mentions(
        "2026年上半年营业收入372.68亿元但仍需跟踪。", row, field="summary"
    )
    assert "37.27亿元" in repaired
    assert records[0]["old"] == "372.68亿元"
    summary_repaired, summary_records = _repair_fact_unit_mentions(
        "2026年上半年收入372.68亿元。", row, field="summary"
    )
    assert summary_repaired == "2026年上半年收入37.27亿元。"
    assert summary_records[0]["metric"] == "营业收入"
    untouched, no_records = _repair_fact_unit_mentions("营业收入372.68元/股。", row, field="summary")
    assert untouched == "营业收入372.68元/股。"
    assert no_records == []


def test_inferred_fact_units_use_key_suffixes_not_substrings() -> None:
    assert _inferred_fact_unit("operating_cash_flow_cny") == "元"
    assert _inferred_fact_unit("operating_cash_flow") == "元"
    assert _inferred_fact_unit("revenue_cny") == "元"
    assert _inferred_fact_unit("gross_margin") == "%"
    assert _inferred_fact_unit("eps_per_share") == "元/股"
    assert _inferred_fact_unit("2025_eps_cny") == "元/股"
    assert _inferred_fact_unit("eps_2026H1") == "元/股"
    assert _inferred_fact_unit("rd_ratio") == "%"
    assert _inferred_fact_unit("production_tons") == "吨"
    assert _inferred_fact_unit("pb_ratio") == "倍"


def test_nested_fact_mapping_uses_key_units_over_compound_parent_unit() -> None:
    row = {
        "financial_facts": [
            {
                "period": "2026H1",
                "unit": "元/%",
                "value": {
                    "gross_margin": 0.4,
                    "revenue_cny": 100,
                },
            }
        ]
    }

    bindings = _financial_fact_bindings(row)
    by_metric = {binding["metric"]: binding for binding in bindings}

    assert by_metric["gross_margin"]["value"] == 0.4
    assert by_metric["gross_margin"]["unit"] == "%"
    assert by_metric["revenue_cny"]["value"] == 100
    assert by_metric["revenue_cny"]["unit"] == "元"
    assert all(binding.get("unit") != "元/%" for binding in bindings)
