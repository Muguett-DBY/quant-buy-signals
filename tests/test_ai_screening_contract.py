from __future__ import annotations

import hashlib
import json

from tools.ai_screening_contract import (
    decision_text_conflicts,
    normalise_decision_text,
    select_candidates,
    validate_review,
)
from tools.build_ai_screening import _relevant_rules, _rule_chunks, build_input, merge_reviews
from tools.calibrate_ai_screening_ranking import _action_safe_summary, _claim_url, _review
from tools.enrich_ai_screening_input import enrich
from tools.publish_ai_screening import _public_review, build_artifact
from tools.prepare_ai_screening_overlay import prepare
from tools.run_ai_screening_batch import (
    _extract_array,
    _extract_opencode_text,
    _normalise_model_review,
    _prompt as batch_prompt,
)


def _snapshot() -> dict:
    return {
        "generation": "g1",
        "market_as_of": "2026-08-13",
        "companies": [
            {
                "code": "600339",
                "name": "中油工程",
                "types": {
                    "type1": {"status": "triggered", "score": 7.8},
                    "type7": {"status": "observe", "score": 7.2, "upper": 7.2},
                },
            },
            {
                "code": "000001",
                "name": "不应入选",
                "types": {"type1": {"status": "not_triggered", "score": 4.2}},
            },
        ],
    }


def _authoritative_rule_root(tmp_path):
    root = tmp_path / "rules"
    root.mkdir()
    patch7 = """# 补丁7 · 长期投资者的买卖总闸门
共同未来自由现金流前提与总闸门。
情况一｜第25模板：股价已明显进入买入区 + 价值陷阱排查
type1 专属规则。
情况二｜第17模板：两热一冷 + 估值合理或偏低
type2 专属规则。
情况三｜第15模板：具备可持续高增长的潜力
type3 专属规则。
情况四｜第10模板：长坡厚雪型，估值合理或偏低
type4 专属规则。
情况五｜第6、第9模板：强周期产业长波段操作
type5 专属规则。
情况六｜第19模板：VC风险投资标的属性
type6 专属规则。
情况七
type7 专属规则。
综合运用指南
全部类型共同执行证据纪律。
补丁7 方法论附录｜强周期产业底部估值特例与五维度操作清单。
type5 强周期附录。
附录：卖出闸门协议（四硬一软）
持有域不属于本次买入筛查。
"""
    files = {
        "补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md": patch7,
        "第25模板.md": "# 第25模板\ntype1 DCF 买入区原始规则。",
        "第17模板.md": "# 第17模板\ntype2 两热一冷原始规则。",
        "第15模板.md": "# 第15模板\ntype3 可持续高增长原始规则。",
        "第10模板.md": "# 第10模板\ntype4 长坡厚雪原始规则。",
        "第6模板.md": "# 第6模板\ntype5 强周期识别规则。",
        "第9模板.md": "# 第9模板\ntype5 周期陷阱规则。",
        "第19模板.md": "# 第19模板\ntype6 风险投资属性规则。",
        "补丁6· 公司三属性分类与三维度量化打分机制.md": "# 补丁6\ntype7 三属性规则。",
        "第1模板.md": "# 第1模板\ntype7 商业模式底层规则。",
        "第5模板.md": "# 第5模板\ntype7 质量与估值底层规则。",
        "补丁5.md": "# 补丁5\ntype7 安全边际底层规则。",
        "模板汇总.md": "# 历史汇总\n买入、证据、否决与所有 type1 type2 type3 type4 type5 type6 type7。",
        "模板汇总(1).md": "# 历史汇总副本\n买入、证据、否决。",
    }
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root, files


def test_rule_context_uses_authoritative_type_specific_sources(tmp_path) -> None:
    root, _ = _authoritative_rule_root(tmp_path)
    chunks = _rule_chunks(root)
    patch7 = "补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md"
    expected = {
        "type1": {patch7, "第25模板.md"},
        "type2": {patch7, "第17模板.md"},
        "type3": {patch7, "第15模板.md"},
        "type4": {patch7, "第10模板.md"},
        "type5": {patch7, "第6模板.md", "第9模板.md"},
        "type6": {patch7, "第19模板.md"},
        "type7": {
            patch7,
            "补丁6· 公司三属性分类与三维度量化打分机制.md",
            "第1模板.md",
            "第5模板.md",
            "补丁5.md",
        },
    }
    contexts = {}
    for type_key, source_ids in expected.items():
        context = _relevant_rules(chunks, type_key)
        contexts[type_key] = context
        assert {item["source_id"] for item in context} == source_ids
        assert all(item["source_id"] not in {"模板汇总.md", "模板汇总(1).md"} for item in context)
        patch7_scopes = {item.get("scope") for item in context if item["source_id"] == patch7}
        assert patch7_scopes == {"common", type_key}

    signatures = {
        tuple((item["source_id"], item["line_start"], item["heading"]) for item in context)
        for context in contexts.values()
    }
    assert len(signatures) == 7


def test_build_manifest_hashes_only_the_rules_actually_injected(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    root, _ = _authoritative_rule_root(tmp_path)
    out = tmp_path / "out"
    manifest = build_input(snapshot_path, root, out)
    patch7 = "补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md"
    injected = {
        patch7,
        "第25模板.md",
        "补丁6· 公司三属性分类与三维度量化打分机制.md",
        "第1模板.md",
        "第5模板.md",
        "补丁5.md",
    }
    assert manifest["rule_file_count"] == len(injected)
    assert set(manifest["rule_source_sha256"]) == injected
    assert manifest["rule_source_sha256"] == {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(injected)
    }
    payload = json.loads((out / "ai-screening-input.json").read_text(encoding="utf-8"))
    assert payload["rule_source_sha256"] == manifest["rule_source_sha256"]


def test_enrich_manifest_hashes_only_the_rules_actually_injected(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    root, _ = _authoritative_rule_root(tmp_path)
    initial = tmp_path / "initial"
    build_input(snapshot_path, root, initial)

    enriched = tmp_path / "enriched"
    manifest = enrich(initial / "ai-screening-input.json", root, enriched)
    payload = json.loads((enriched / "ai-screening-input.json").read_text(encoding="utf-8"))

    injected = {rule["source_id"] for packet in payload["packets"] for rule in packet["rule_context"]}
    assert injected
    assert "模板汇总.md" not in injected
    assert "模板汇总(1).md" not in injected
    assert manifest["rule_file_count"] == len(injected)
    assert set(manifest["rule_source_sha256"]) == injected
    assert payload["rule_source_sha256"] == manifest["rule_source_sha256"]


def test_selects_triggered_and_type7_boundary_pairs() -> None:
    rows = select_candidates(_snapshot())
    assert [(row["security_code"], row["type_key"]) for row in rows] == [
        ("600339", "type1"),
        ("600339", "type7"),
    ]


def test_review_requires_sources_and_keeps_verdict_bounded() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "confirmed",
        "recommended_action": "keep",
        "buy_attractiveness_score": 72,
        "ai_action": "priority_buy",
        "confidence": "medium",
        "key_strengths": ["估值有安全边际"],
        "risk_flags": [],
        "claims": [{"source_ref": "annual_report_2025:p42"}],
    }
    assert validate_review(review) == []
    review["claims"] = [{"statement": "no source"}]
    assert "claim_source_ref" in validate_review(review)


def test_batch_parser_skips_intermediate_json_arrays() -> None:
    intermediate = '[{"tool": "web_search", "queries": ["600339"]}]'
    final = json.dumps(
        [
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "confirmed",
                "recommended_action": "keep",
                "buy_attractiveness_score": 82,
                "ai_action": "priority_buy",
                "confidence": "medium",
            }
        ]
    )
    assert _extract_array(intermediate + "\n" + final)[0]["security_code"] == "600339"


def test_opencode_events_extract_final_text_and_normalise_action_verdict() -> None:
    events = "\n".join(
        [
            json.dumps({"type": "tool_use", "part": {"tool": "websearch"}}),
            json.dumps({"type": "text", "part": {"text": "[{}]"}}),
        ]
    )
    assert _extract_opencode_text(events) == "[{}]"
    review = {
        "verdict": "insufficient_evidence",
        "key_strengths": "成本优势",
        "risk_flags": "需求下滑",
        "claims": {"statement": "无来源"},
    }
    assert _normalise_model_review(review)["verdict"] == "needs_review"
    assert review["key_strengths"] == ["成本优势"]
    assert review["risk_flags"] == ["需求下滑"]
    assert review["claims"] == []


def test_batch_protocol_allows_independent_near_qualified_buy() -> None:
    prompt = batch_prompt(
        "协议片段",
        [{"security_code": "600339", "type_key": "type7", "rule_context": []}],
        require_web_search=True,
    )
    assert "Deterministic status is context" in prompt
    assert "near-qualified candidate" in prompt
    assert "priority_buy" in prompt
    assert "HTTPS is preferred" in prompt


def test_calibrated_priority_can_include_near_threshold_rule() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 72,
            "claims": [{"source_ref": "https://example.test/report"}],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    assert _review(source)["ai_action"] == "priority_buy"
    assert _review(source)["final_category"] == "recommend_buy"

    source["deterministic"]["status"] = "observe"
    observed = _review(source)
    assert observed["ai_action"] == "priority_buy"
    assert observed["final_category"] == "recommend_buy"
    assert observed["ai_independent"] is True
    assert observed["buy_attractiveness_score"] < 72

    source["deterministic"]["status"] = "insufficient_evidence"
    unresolved = _review(source)
    assert unresolved["ai_action"] == "priority_buy"
    assert unresolved["final_recommendation"] == "recommend_buy"
    assert unresolved["final_category"] == "recommend_buy"
    assert unresolved["buy_attractiveness_score"] < observed["buy_attractiveness_score"]


def test_decision_text_collapses_repeated_negation_tokens() -> None:
    text = "当前暂不不不不不建议买入，纳入观察。"
    cleaned = normalise_decision_text(text)
    assert cleaned == "当前暂不建议买入，纳入观察。"
    assert "不不" not in cleaned
    assert decision_text_conflicts("watchlist", "不能据此直接建议买入。") is False
    assert decision_text_conflicts("watchlist", "当前建议买入。") is True


def test_watchlist_summary_uses_observe_label_before_non_buy_qualification() -> None:
    summary = "当前结论：不建议买，此为AI独立判断，等待中报确认。"

    assert _action_safe_summary(summary, "watchlist") == "当前结论：观察（暂不建议买），此为AI独立判断，等待中报确认。"


def test_historical_claims_are_visible_and_cannot_remain_a_buy_recommendation() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 80,
            "claims": [{"statement": "2024年年报净利润和现金流保持增长", "source_ref": "https://example.test/report"}],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    reviewed = _review(source, "2026-08-14")
    assert reviewed["freshness_status"] == "historical"
    assert reviewed["freshness_years"] == [2024]
    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["ai_independent"] is False
    assert reviewed["final_category"] == "observe"
    assert "2024" in reviewed["freshness_note"]
    assert "资料时效" in reviewed["risk_flags"][0]


def test_recent_claim_keeps_a_confirmed_buy_opinion_eligible() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 80,
            "claims": [
                {"statement": "2025年年报及2026年一季报均支持盈利质量", "source_ref": "https://example.test/report"}
            ],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    reviewed = _review(source, "2026-08-14")
    assert reviewed["freshness_status"] == "current_or_recent"
    assert reviewed["ai_action"] == "priority_buy"
    assert reviewed["final_category"] == "recommend_buy"
    assert "最新可识别实际报告期为 2026 年" in reviewed["freshness_note"]


def test_forecast_year_does_not_count_as_current_report_evidence() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 80,
            "claims": [{"statement": "2025—2027年盈利预测目标为增长", "source_ref": "https://example.test/forecast"}],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    reviewed = _review(source, "2026-08-14")
    assert reviewed["freshness_status"] == "undated"
    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["final_category"] == "observe"


def test_future_years_are_not_published_as_actual_report_periods() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 80,
            "claims": [
                {
                    "statement": "2025年年报披露营业收入和现金流均改善。公司计划在2027年扩大产能",
                    "source_ref": "https://example.test/forecast",
                }
            ],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    reviewed = _review(source, "2026-08-14")
    assert reviewed["freshness_status"] == "current_or_recent"
    assert reviewed["freshness_years"] == [2025]
    assert "2027" not in reviewed["freshness_note"]


def test_contract_rejects_stale_buy_fields() -> None:
    source = {
        "schema_version": 2,
        "security_code": "600339",
        "type_key": "type1",
        "verdict": "confirmed",
        "recommended_action": "keep",
        "buy_attractiveness_score": 80,
        "ai_action": "priority_buy",
        "final_category": "recommend_buy",
        "final_recommendation": "recommend_buy",
        "confidence": "high",
        "web_search_performed": True,
        "freshness_status": "historical",
        "freshness_years": [2024],
        "claims": [{"statement": "2024年年报", "source_ref": "https://example.test/report"}],
    }
    errors = validate_review(source)
    assert {"stale_priority_buy", "stale_recommend_buy", "stale_final_recommendation"}.issubset(errors)


def test_unsearched_confirmation_is_ranked_with_a_source_penalty() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "claims": [],
            "risk_flags": [],
            "buy_attractiveness_score": 91,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    unsearched = _review(source)
    assert unsearched["ai_action"] == "priority_buy"
    assert unsearched["buy_attractiveness_score"] == 83

    source["ai_review"].update(
        {
            "web_search_performed": True,
            "claims": [{"source_ref": "https://example.test/report"}],
        }
    )
    searched = _review(source)
    assert searched["ai_action"] == "priority_buy"
    assert searched["buy_attractiveness_score"] == 91


def test_source_backed_ai_avoid_overrides_a_triggered_rule() -> None:
    source = {
        "ai_review": {
            "verdict": "caution",
            "recommended_action": "demote",
            "ai_action": "avoid",
            "buy_attractiveness_score": 28,
            "claims": [{"source_ref": "https://example.test/report"}],
            "risk_flags": ["盈利崩塌"],
            "web_search_performed": True,
        },
        "security_code": "002790",
        "type_key": "type5",
        "deterministic": {"status": "triggered", "score": 8.5},
    }
    reviewed = _review(source)
    assert reviewed["ai_action"] == "avoid"
    assert reviewed["buy_attractiveness_score"] == 28


def test_insufficient_or_watchlist_never_becomes_do_not_recommend() -> None:
    source = {
        "ai_review": {
            "verdict": "needs_review",
            "recommended_action": "manual_review",
            "ai_action": "insufficient_evidence",
            "buy_attractiveness_score": 38,
            "claims": [],
            "risk_flags": ["资料未闭环"],
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 7.8},
    }
    unresolved = _review(source)
    assert unresolved["ai_action"] == "watchlist"
    assert unresolved["final_category"] == "observe"

    source["ai_review"]["ai_action"] = "watchlist"
    source["ai_review"]["buy_attractiveness_score"] = 42
    observed = _review(source)
    assert observed["ai_action"] == "watchlist"
    assert observed["final_category"] == "observe"


def test_public_review_strips_reasonix_annotation_from_source_url() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600339",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 60,
        "ai_action": "watchlist",
        "confidence": "medium",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [
            {
                "statement": "官方报告",
                "source_ref": "https://example.test/report.pdf这是报告说明",
            }
        ],
        "web_search_performed": True,
    }
    public = _public_review(review)
    assert public["claims"][0]["source_ref"] == "https://example.test/report.pdf"
    assert public["web_search_verified"] is True


def test_source_context_url_is_recovered_without_inventing_a_source() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600339",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 60,
        "ai_action": "watchlist",
        "confidence": "medium",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [
            {
                "statement": "报告",
                "source_ref": "",
                "source_context": "https://example.test/report.pdf（年报）",
            }
        ],
        "web_search_performed": True,
    }
    assert _claim_url(review["claims"][0]) == "https://example.test/report.pdf"
    assert _public_review(review)["web_search_verified"] is True


def test_public_review_drops_http_sources_and_cannot_claim_web_verification() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600339",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 60,
        "ai_action": "watchlist",
        "confidence": "medium",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [{"statement": "报告", "source_ref": "http://example.test/report"}],
        "web_search_performed": True,
        "web_search_verified": True,
    }
    public = _public_review(review)
    assert public["claims"][0]["source_ref"] == "http://example.test/report"
    assert public["web_search_verified"] is False


def test_build_and_merge_review_artifacts(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "patch7.md").write_text("# type1\n总闸门和证据规则", encoding="utf-8")
    out = tmp_path / "out"
    manifest = build_input(snapshot_path, rules, out)
    assert manifest["candidate_count"] == 2
    assert manifest["rule_file_count"] == 1
    enriched = json.loads((out / "ai-screening-input.json").read_text(encoding="utf-8"))
    assert enriched["packets"][0]["rule_context"]
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "confirmed",
                "recommended_action": "keep",
                "buy_attractiveness_score": 72,
                "ai_action": "priority_buy",
                "confidence": "medium",
                "key_strengths": ["估值有安全边际"],
                "risk_flags": [],
                "claims": [{"source_ref": "annual_report_2025:p42"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    merged = out / "ai-screening.json"
    merge_reviews(out / "ai-screening-input.json", review_path, merged)
    value = json.loads(merged.read_text(encoding="utf-8"))
    assert value["packets"][0]["ai_review"]["verdict"] == "confirmed"
    assert value["packets"][1]["ai_review"] is None


def test_publish_artifact_is_generation_bound_and_advisory(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "patch7.md").write_text("# type1\n规则", encoding="utf-8")
    out = tmp_path / "out"
    build_input(snapshot_path, rules, out)
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "caution",
                "recommended_action": "manual_review",
                "buy_attractiveness_score": 64,
                "ai_action": "watchlist",
                "confidence": "medium",
                "key_strengths": ["规则分数较高"],
                "risk_flags": ["现金流需核验", "单期现金流波动"],
                "summary": "现金流需要人工核验",
                "claims": [{"statement": "报告披露", "source_ref": "https://example.test/report"}],
                "model": "opencode-go/deepseek-v4-flash",
                "effort": "max",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    merged = out / "ai-screening.json"
    merge_reviews(out / "ai-screening-input.json", review_path, merged)
    artifact_path = tmp_path / "public.json"
    artifact = build_artifact(
        merged,
        artifact_path,
        expected_generation="g1",
        expected_market_as_of="2026-08-13",
    )
    assert artifact["ai_is_advisory"] is True
    assert artifact["auto_buy_promotion"] is False
    assert artifact["schema_version"] == 2
    assert artifact["ranking_version"] == "ai-buy-attractiveness-v8-category-first-action-banded"
    assert artifact["review_models"] == ["opencode-go/deepseek-v4-flash"]
    assert artifact["review_efforts"] == ["max"]
    assert artifact["reviewed_count"] == 1
    assert artifact["packets"][0]["deterministic"]["status"] == "triggered"
    assert artifact["attempted_review_count"] == 1
    assert artifact["unreviewed_candidate_count"] == 0
    assert artifact["attempted_needs_review_count"] == 0
    assert artifact["completed_review_count"] == 1
    assert artifact["pending_review_count"] == 0
    assert artifact["packets"][0]["ai_rank"] == 1
    assert artifact["packets"][0]["type_keys"] == ["type1"]
    assert artifact["packets"][0]["type_pair_count"] == 1
    assert artifact["type_pair_candidate_total"] == 1
    assert artifact["type_pair_expected_total"] == 2
    assert artifact["watchlist_count"] == 1
    assert artifact["final_category_counts"] == {"recommend_buy": 0, "observe": 1, "do_not_recommend": 0}
    assert artifact["packets"][0]["ai_review"]["final_category"] == "observe"
    assert artifact["packets"][0]["ai_review"]["final_recommendation"] == "do_not_recommend_buy"
    assert artifact["do_not_recommend_buy_count"] == 1


def test_publish_ranks_buy_category_before_higher_raw_negative_score(tmp_path) -> None:
    merged = tmp_path / "merged.json"
    base = {
        "schema_version": 2,
        "verdict": "confirmed",
        "recommended_action": "keep",
        "confidence": "medium",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [],
        "model": "opencode-go/test",
    }
    merged.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-13",
                "packets": [
                    {
                        "security_code": "600001",
                        "name": "不建议但原始分很高",
                        "type_key": "type1",
                        "deterministic": {"status": "observe", "score": 10},
                        "ai_review": {
                            **base,
                            "schema_version": 2,
                            "security_code": "600001",
                            "type_key": "type1",
                            "buy_attractiveness_score": 49,
                            "ai_action": "avoid",
                            "recommended_action": "demote",
                            "final_category": "do_not_recommend",
                            "final_recommendation": "do_not_recommend_buy",
                        },
                    },
                    {
                        "security_code": "600002",
                        "name": "建议买",
                        "type_key": "type1",
                        "deterministic": {"status": "triggered", "score": 7.8},
                        "ai_review": {
                            **base,
                            "schema_version": 2,
                            "security_code": "600002",
                            "type_key": "type1",
                            "buy_attractiveness_score": 70,
                            "ai_action": "priority_buy",
                            "final_category": "recommend_buy",
                            "final_recommendation": "recommend_buy",
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact = build_artifact(
        merged,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-13",
    )
    assert artifact["packets"][0]["security_code"] == "600002"
    assert artifact["packets"][0]["ai_review"]["final_category"] == "recommend_buy"


def test_local_codex_review_mode_is_full_pair_coverage_without_fake_web_search(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "patch7.md").write_text("# type1\n规则", encoding="utf-8")
    out = tmp_path / "out"
    manifest = build_input(snapshot_path, rules, out, review_mode="local_codex_review")
    assert manifest["full_coverage_final_recommendation"] is True
    reviews = [
        {
            "schema_version": 2,
            "security_code": "600339",
            "type_key": "type1",
            "verdict": "confirmed",
            "recommended_action": "keep",
            "buy_attractiveness_score": 82,
            "ai_action": "priority_buy",
            "confidence": "medium",
            "summary": "本地复核确认规则分数较高，同时保留最新报告复核风险。",
            "key_strengths": ["规则候选分数较高"],
            "risk_flags": ["仍需人工复核最新报告"],
            "claims": [],
            "model": "codex-local-review-v1",
            "effort": "max",
            "web_search_performed": False,
        },
        {
            "schema_version": 2,
            "security_code": "600339",
            "type_key": "type7",
            "verdict": "caution",
            "recommended_action": "manual_review",
            "buy_attractiveness_score": 65,
            "ai_action": "watchlist",
            "confidence": "low",
            "summary": "规则基础仍可保留，但外部资料尚未完成逐家公司核对。",
            "key_strengths": ["规则结果仍接近阈值"],
            "risk_flags": ["尚未完成逐家公司联网搜索"],
            "claims": [],
            "model": "codex-local-review-v1",
            "effort": "max",
            "web_search_performed": False,
        },
    ]
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in reviews), encoding="utf-8")
    merged = out / "ai-screening.json"
    merge_reviews(out / "ai-screening-input.json", review_path, merged)
    artifact = build_artifact(
        merged,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-13",
    )
    assert artifact["review_mode"] == "local_codex_review"
    assert artifact["review_models"] == ["codex-local-review-v1"]
    assert artifact["review_efforts"] == ["max"]
    assert artifact["full_coverage_final_recommendation"] is True
    assert artifact["type_pair_reviewed_count"] == artifact["type_pair_candidate_total"] == 2
    assert artifact["type_pair_unreviewed_count"] == 0
    assert artifact["full_coverage_web_search"] is False
    assert artifact["reviewed_without_web_search"] == artifact["candidate_total"]


def test_prepare_overlay_keeps_unreviewed_candidates_visible(tmp_path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "packets": [
                    {"security_code": "600339", "type_key": "type1"},
                    {"security_code": "000001", "type_key": "type2"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "needs_review",
                "recommended_action": "manual_review",
                "buy_attractiveness_score": 0,
                "ai_action": "insufficient_evidence",
                "confidence": "low",
                "key_strengths": [],
                "risk_flags": ["等待资料"],
                "claims": [{"source_ref": "https://example.test/report"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "merged.json"
    result = prepare(input_path, output_path, [review_path])
    value = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == {
        "candidate_count": 2,
        "completed": 1,
        "pending": 1,
        "attempted": 1,
        "attempted_needs_review": 1,
    }
    assert len(value["packets"]) == 2
    assert value["packets"][1]["ai_review"]["verdict"] == "needs_review"


def test_attempted_evidence_shortfall_is_not_counted_as_unreviewed(tmp_path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "patch7.md").write_text("# type1\n规则", encoding="utf-8")
    out = tmp_path / "out"
    build_input(snapshot_path, rules, out)
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "needs_review",
                "recommended_action": "manual_review",
                "buy_attractiveness_score": 42,
                "ai_action": "insufficient_evidence",
                "confidence": "low",
                "key_strengths": ["规则结果值得进一步核验"],
                "risk_flags": ["模型无法确认关键事实"],
                "summary": "模型已尝试，但证据不足",
                "claims": [{"source_ref": "https://example.test/report"}],
                "model": "opencode-go/deepseek-v4-flash",
                "effort": "max",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    merged = out / "ai-screening.json"
    prepare(out / "ai-screening-input.json", merged, [review_path])
    artifact = build_artifact(
        merged,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-13",
    )
    assert artifact["attempted_review_count"] == 1
    # The public artifact is one row per company.  The pending type7 pair is
    # retained in the pair audit, but it must not create a second company card.
    assert artifact["candidate_total"] == 1
    assert artifact["unreviewed_candidate_count"] == 0
    assert artifact["type_pair_candidate_total"] == 2
    assert artifact["type_pair_unreviewed_count"] == 1
    assert artifact["attempted_needs_review_count"] == 1
    assert artifact["pending_review_count"] == 0


def test_publish_artifact_deduplicates_company_but_retains_type_pair_audit(tmp_path) -> None:
    merged = tmp_path / "merged.json"
    merged.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-13",
                "candidate_total": 2,
                "packets": [
                    {
                        "security_code": "600339",
                        "name": "中油工程",
                        "type_key": "type1",
                        "deterministic": {"status": "triggered", "score": 7.8},
                        "ai_review": {
                            "schema_version": 2,
                            "security_code": "600339",
                            "type_key": "type1",
                            "verdict": "caution",
                            "recommended_action": "manual_review",
                            "buy_attractiveness_score": 65,
                            "ai_action": "watchlist",
                            "confidence": "medium",
                            "key_strengths": ["现金流仍需核验"],
                            "risk_flags": [],
                            "claims": [{"source_ref": "https://example.test/type1"}],
                            "model": "opencode-go/deepseek-v4-flash",
                        },
                    },
                    {
                        "security_code": "600339",
                        "name": "中油工程",
                        "type_key": "type7",
                        "deterministic": {"status": "triggered", "score": 8.1},
                        "ai_review": {
                            "schema_version": 2,
                            "security_code": "600339",
                            "type_key": "type7",
                            "verdict": "confirmed",
                            "recommended_action": "keep",
                            "buy_attractiveness_score": 82,
                            "ai_action": "priority_buy",
                            "confidence": "high",
                            "key_strengths": ["估值与增长匹配"],
                            "risk_flags": [],
                            "claims": [{"source_ref": "https://example.test/type7"}],
                            "model": "opencode-go/deepseek-v4-flash",
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact = build_artifact(
        merged,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-13",
    )
    assert artifact["candidate_total"] == 1
    assert artifact["reviewed_count"] == 1
    assert artifact["type_pair_candidate_total"] == 2
    assert artifact["type_pair_reviewed_count"] == 2
    packet = artifact["packets"][0]
    assert packet["type_key"] == "type7"
    assert packet["type_keys"] == ["type1", "type7"]
    assert packet["type_pair_count"] == 2
    assert sum(item["type_pair_count"] for item in artifact["packets"]) == artifact["type_pair_candidate_total"]
    assert len({item["security_code"] for item in artifact["packets"]}) == artifact["candidate_total"]
    assert [item["ai_rank"] for item in artifact["packets"]] == list(range(1, artifact["candidate_total"] + 1))
    assert packet["ai_review"]["ai_action"] == "priority_buy"
