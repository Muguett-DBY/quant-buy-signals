from __future__ import annotations

import hashlib
import json

import pytest

from tools.ai_screening_contract import (
    decision_text_conflicts,
    make_valuation_snapshot,
    native_company_research_profile_matches,
    normalise_decision_text,
    select_candidates,
    validate_review,
    valuation_snapshot_errors,
)
from tools.build_ai_screening import _relevant_rules, _rule_chunks, build_input, merge_reviews
from tools.calibrate_ai_screening_ranking import _action_safe_summary, _claim_url, _review
from tools.enrich_ai_screening_input import enrich
from tools.publish_ai_screening import _public_artifact_bytes, _public_review, build_artifact
from tools.prepare_ai_screening_overlay import prepare
from tools.run_ai_screening_batch import (
    _cohere_local_review,
    _extract_array,
    _extract_opencode_reviews,
    _extract_opencode_text,
    _normalise_model_review,
    _prompt as batch_prompt,
)


def test_valuation_snapshot_rejects_synchronised_valuation_tamper() -> None:
    review = {
        "security_code": "600000",
        "valuation": {
            "as_of": "2026-08-24",
            "current_price": 10.0,
            "pe": 8.0,
            "pb": 1.2,
            "market_cap": 50_000_000_000.0,
        },
    }
    review["valuation_snapshot"] = make_valuation_snapshot(
        security_code="600000",
        snapshot_generation="generation-1",
        market_as_of="2026-08-24",
        current_price=10.0,
        pe=8.0,
        pb=1.2,
        market_cap=50_000_000_000.0,
    )
    assert valuation_snapshot_errors(
        review,
        expected_snapshot_generation="generation-1",
        expected_market_as_of="2026-08-24",
    ) == []

    missing = dict(review)
    missing.pop("valuation_snapshot")
    assert valuation_snapshot_errors(missing) == ["valuation_snapshot"]

    tampered = json.loads(json.dumps(review))
    tampered["valuation"]["current_price"] = 11.0
    tampered["valuation_snapshot"]["current_price"] = 11.0
    errors = valuation_snapshot_errors(
        tampered,
        expected_snapshot_generation="generation-1",
        expected_market_as_of="2026-08-24",
    )

    assert errors == ["valuation_snapshot.canonical_sha256"]


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


@pytest.mark.parametrize(
    ("model", "effort", "backend", "retrieval_model", "retrieval_effort"),
    [
        (
            "opencode-go/ox-alpha-free",
            "max",
            "opencode-native-client-websearch",
            "opencode-go/ox-alpha-free",
            "max",
        ),
        (
            "opencode/muse-spark-1.2-contributor-free",
            "xhigh",
            "opencode-native-client-websearch",
            "opencode/muse-spark-1.2-contributor-free",
            "xhigh",
        ),
    ],
)
def test_opencode_native_company_profiles_require_exact_backend_and_effort(
    model: str,
    effort: str,
    backend: str,
    retrieval_model: str,
    retrieval_effort: str,
) -> None:
    review = {
        "model": model,
        "effort": effort,
        "retrieval_backend": backend,
        "retrieval_model": retrieval_model,
        "retrieval_effort": retrieval_effort,
        "native_search_completed": True,
    }

    assert native_company_research_profile_matches(review)
    assert not native_company_research_profile_matches({**review, "retrieval_effort": "high"})


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
        "summary": "当前盈利和现金流支持买入，但仍需跟踪需求波动。",
        "quantitative_facts": ["2025年度营业收入 120 亿元", "2025年度经营现金流 18 亿元"],
        "key_strengths": ["2025年度收入与现金流均保持正值"],
        "risk_flags": ["下游需求波动可能压低盈利"],
        "claims": [
            {
                "statement": "2025年度营业收入 120 亿元",
                "source_ref": "annual_report_2025:p42",
                "support": "supports",
            },
            {
                "statement": "2025年度经营现金流 18 亿元",
                "source_ref": "annual_report_2025:p55",
                "support": "supports",
            },
        ],
    }
    assert validate_review(review) == []
    assert "valuation" in validate_review(review, require_company_research_fields=True)

    insecure_claim = json.loads(json.dumps(review))
    insecure_claim["claims"][0]["source_ref"] = "http://example.test/financial"
    assert "claim_https_source_ref" in validate_review(
        insecure_claim,
        require_company_research_fields=True,
    )
    review["claims"] = [
        {"statement": "2025年度营业收入 120 亿元", "support": "supports"},
        {"statement": "2025年度经营现金流 18 亿元", "support": "supports"},
    ]
    assert "claim_source_ref" in validate_review(review)


@pytest.mark.parametrize(
    "reason",
    [
        "TYPE1 已触发，规则分数较高",
        "确定性筛选已经达标",
        "第七种买入情况接近达标",
    ],
)
def test_ai_reason_fields_reject_rule_language(reason: str) -> None:
    review = {
        "schema_version": 2,
        "model": "opencode-go/deepseek-v4-flash",
        "effort": "max",
        "retrieval_backend": "reasonix-native-server-web-search",
        "retrieval_model": "opencode-go-deepseek-responses/deepseek-v4-flash",
        "retrieval_effort": "max",
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 55,
        "ai_action": "watchlist",
        "confidence": "medium",
        "summary": reason,
        "key_strengths": ["2025年度营业收入 120 亿元"],
        "risk_flags": ["需求仍可能波动"],
        "quantitative_facts": ["2025年度经营现金流 18 亿元"],
        "claims": [],
    }
    assert "rule_language_in_ai_reason" in validate_review(
        review,
        require_company_research_fields=True,
    )


def test_legacy_local_review_remains_compatible_with_rule_context() -> None:
    review = {
        "schema_version": 2,
        "model": "codex-local-review-v1",
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 55,
        "ai_action": "watchlist",
        "confidence": "medium",
        "summary": "TYPE1 已触发，规则分数较高",
        "key_strengths": ["2025年度营业收入 120 亿元"],
        "risk_flags": ["需求仍可能波动"],
        "quantitative_facts": ["2025年度经营现金流 18 亿元"],
        "claims": [],
    }

    assert "rule_language_in_ai_reason" not in validate_review(review)


def test_priority_buy_rejects_announcement_titles_as_quantitative_evidence() -> None:
    titles = [
        "公司于2026年8月18日披露2026年半年度报告",
        "公司于2026年8月18日披露2026年半年度报告摘要",
    ]
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "confirmed",
        "recommended_action": "keep",
        "buy_attractiveness_score": 72,
        "ai_action": "priority_buy",
        "confidence": "medium",
        "summary": "公司已发布最新报告，但正文财务数据尚未形成证据闭环。",
        "quantitative_facts": titles,
        "key_strengths": ["最新报告已经发布"],
        "risk_flags": ["报告正文中的经营数据仍待核验"],
        "claims": [
            {
                "statement": title,
                "source_ref": f"https://example.test/title-{index}",
                "support": "supports",
            }
            for index, title in enumerate(titles)
        ],
    }
    errors = validate_review(review)
    assert "priority_company_research_facts" in errors
    assert "priority_source_linked_research" in errors


def test_research_source_verification_requires_an_actual_claim_url() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 55,
        "ai_action": "watchlist",
        "confidence": "medium",
        "summary": "公司经营质量仍需继续观察。",
        "key_strengths": ["盈利能力具备进一步研究价值"],
        "risk_flags": ["现金流持续性仍需核验"],
        "claims": [{"statement": "2025年度营业收入 120 亿元"}],
        "research_source_urls_verified": True,
    }
    assert "research_sources_without_url" in validate_review(review)
    review["claims"][0]["source_ref"] = "https://example.test/financial-api"
    assert "research_sources_without_url" not in validate_review(review)


def test_structured_company_research_distinguishes_market_and_research_dates() -> None:
    review = {
        "schema_version": 2,
        "model": "opencode-go/deepseek-v4-flash",
        "effort": "max",
        "retrieval_backend": "reasonix-native-server-web-search",
        "retrieval_model": "opencode-go-deepseek-responses/deepseek-v4-flash",
        "retrieval_effort": "max",
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 58,
        "ai_action": "watchlist",
        "confidence": "medium",
        "summary": "当前结论：观察。现金流尚可，但价格安全边际仍需扩大。",
        "key_strengths": ["经营现金流持续为正"],
        "risk_flags": ["应收账款增长需要持续跟踪"],
        "claims": [{"statement": "2025年度经营现金流 18 亿元", "source_ref": "https://example.test/financial"}],
        "research_as_of": "2026-08-23",
        "economic_profile": {
            "business_model": "以企业金融与零售金融获取利差和手续费收入。",
            "business_model_source_ids": ["manual-business"],
            "business_model_source_quality": "current_primary",
            "business_model_uncertainty": "已由2026年半年度报告的一手业务分部口径核验。",
            "moat": "客户与网点基础仍有价值，但息差承压构成反证。",
            "cycle": "处于信用与净息差需要继续观察的阶段。",
            "fcf_outlook": "金融企业更适合结合资本充足率与分红能力判断现金回报。",
            "governance": "资本补充与分红安排需要同时核验。",
        },
        "valuation": {
            "method": "scenario_multiple",
            "as_of": "2026-08-21",
            "current_price": 12.34,
            "pe": 6.2,
            "pb": 0.55,
            "market_cap": 3621.0,
            "scenarios": {
                "bear": {"value_per_share": 11.0, "upside_pct": -10.8589951378},
                "base": {"value_per_share": 14.0, "upside_pct": 13.4521880065},
                "bull": {"value_per_share": 16.0, "upside_pct": 29.6596434360},
            },
            "margin_of_safety": -12.1818181818,
            "basis": "结合市净率、资产质量和悲观信用成本情景判断。",
        },
    }
    assert validate_review(review) == []

    too_early = {**review, "research_as_of": "2026-08-20"}
    assert "research_before_market_close" in validate_review(too_early)

    missing_profile = dict(review)
    missing_profile.pop("economic_profile")
    assert "company_research_envelope" in validate_review(
        missing_profile,
        require_company_research_fields=True,
    )

    leaked_profile = {**review, "economic_profile": {**review["economic_profile"], "moat": "type1 已触发"}}
    assert "economic_profile" in validate_review(
        leaked_profile,
        require_company_research_fields=True,
    )

    unsupported_source_quality = {
        **review,
        "economic_profile": {
            **review["economic_profile"],
            "business_model_source_quality": "probably_primary",
        },
    }
    assert "economic_profile_sources" in validate_review(unsupported_source_quality)

    secondary_without_uncertainty = {
        **review,
        "economic_profile": {
            **review["economic_profile"],
            "business_model_source_quality": "secondary_only",
            "business_model_uncertainty": "业务口径非常确定。",
        },
    }
    assert "economic_profile_sources" in validate_review(secondary_without_uncertainty)

    wrong_upside = {
        **review,
        "valuation": {
            **review["valuation"],
            "scenarios": {
                **review["valuation"]["scenarios"],
                "base": {"value_per_share": 14.0, "upside_pct": 99.0},
            },
        },
    }
    assert "valuation" in validate_review(wrong_upside)

    missing_scenarios = {**review, "valuation": {**review["valuation"], "scenarios": None}}
    assert "valuation" in validate_review(missing_scenarios)

    gordon = json.loads(json.dumps(review))
    price = 12.34
    gordon_values = {"bear": 1 / 0.12, "base": 1 / 0.10, "bull": 1 / 0.08}
    gordon["valuation"] = {
        "method": "gordon_fcf_per_share",
        "as_of": "2026-08-21",
        "current_price": price,
        "pe": 6.2,
        "pb": 0.55,
        "market_cap": 3621.0,
        "scenarios": {
            name: {
                "normalized_fcf_per_share": 1.0,
                "discount_rate_pct": discount,
                "terminal_growth_rate_pct": 0.0,
                "equity_adjustment_per_share": 0.0,
                "value_per_share": gordon_values[name],
                "upside_pct": (gordon_values[name] / price - 1) * 100,
            }
            for name, discount in (("bear", 12.0), ("base", 10.0), ("bull", 8.0))
        },
        "margin_of_safety": (gordon_values["bear"] - price) / gordon_values["bear"] * 100,
        "safety_margin_band": "negative",
        "basis": "以正常化自由现金流按悲观、中性、乐观三种假设估值。",
    }
    assert "valuation" not in validate_review(gordon, require_company_research_fields=True)

    invalid_gordon = json.loads(json.dumps(gordon))
    invalid_gordon["valuation"]["scenarios"]["base"]["equity_adjustment_per_share"] = 1.0
    assert "valuation" in validate_review(invalid_gordon, require_company_research_fields=True)

    non_monotonic_gordon = json.loads(json.dumps(gordon))
    non_monotonic_gordon["valuation"]["scenarios"]["bear"]["terminal_growth_rate_pct"] = 1.0
    assert "valuation" in validate_review(non_monotonic_gordon, require_company_research_fields=True)


def test_public_review_retains_clickable_business_model_source_identity() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 58,
        "ai_action": "watchlist",
        "confidence": "medium",
        "summary": "当前价格缺少足够安全边际，继续观察经营现金流。",
        "key_strengths": ["客户基础仍具价值"],
        "risk_flags": ["净息差仍然承压"],
        "claims": [
            {
                "statement": "2026年半年度报告披露企业金融与零售金融业务。",
                "source_ref": "https://example.test/company-report",
                "source_kind": "company_ir",
                "search_finding_id": "search-business",
            }
        ],
        "research_as_of": "2026-08-23",
        "economic_profile": {
            "business_model": "以企业金融与零售金融获取利差和手续费收入。",
            "business_model_source_ids": ["search-business"],
            "business_model_source_quality": "current_primary",
            "business_model_uncertainty": "已由2026年半年度报告的一手业务分部口径核验。",
            "moat": "客户与网点基础仍有价值，但息差承压构成反证。",
            "cycle": "处于信用与净息差需要继续观察的阶段。",
            "fcf_outlook": "结合资本充足率与分红能力判断股东现金回报。",
            "governance": "资本补充与分红安排需要同时核验。",
        },
        "valuation": {
            "method": "scenario_multiple",
            "as_of": "2026-08-21",
            "current_price": 12.34,
            "pe": 6.2,
            "pb": 0.55,
            "market_cap": 3621.0,
            "scenarios": {
                "bear": {"value_per_share": 11.0, "upside_pct": -10.8589951378},
                "base": {"value_per_share": 14.0, "upside_pct": 13.4521880065},
                "bull": {"value_per_share": 16.0, "upside_pct": 29.6596434360},
            },
            "margin_of_safety": -12.1818181818,
            "basis": "结合市净率、资产质量和悲观信用成本情景判断。",
        },
    }

    public = _public_review(review, claims_are_search_results=False)

    assert public["claims"][0]["search_finding_id"] == "search-business"
    assert public["economic_profile"]["business_model_source_ids"] == ["search-business"]
    assert public["economic_profile"]["business_model_sources"] == [
        {
            "id": "search-business",
            "statement": "2026年半年度报告披露企业金融与零售金融业务。",
            "source_ref": "https://example.test/company-report",
            "source_kind": "company_ir",
        }
    ]

    review["economic_profile"]["business_model_source_ids"] = [
        "search-business",
        "search-business",
    ]
    with pytest.raises(ValueError, match="invalid business-model source IDs"):
        _public_review(review, claims_are_search_results=False)


def test_public_artifact_serialisation_has_a_hard_size_limit(monkeypatch) -> None:
    monkeypatch.setattr("tools.publish_ai_screening.MAX_PUBLIC_ARTIFACT_BYTES", 32)

    with pytest.raises(ValueError, match="exceeds 32 bytes"):
        _public_artifact_bytes({"padding": "x" * 64})


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


def test_opencode_review_fragments_merge_by_packet_identity() -> None:
    first = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "needs_review",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 40,
        "ai_action": "watchlist",
        "final_category": "observe",
        "confidence": "low",
        "summary": "当前结论：观察。",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [],
    }
    second = dict(first, security_code="600001", type_key="type2")
    events = "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": json.dumps([first], ensure_ascii=False)}}),
            json.dumps({"type": "text", "part": {"text": json.dumps([second], ensure_ascii=False)}}),
        ]
    )
    reviews = _extract_opencode_reviews(events, {("600000", "type1"), ("600001", "type2")})
    assert {(row["security_code"], row["type_key"]) for row in reviews} == {
        ("600000", "type1"),
        ("600001", "type2"),
    }


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
    assert observed["buy_attractiveness_score"] == 72

    source["deterministic"]["status"] = "insufficient_evidence"
    unresolved = _review(source)
    assert unresolved["ai_action"] == "priority_buy"
    assert unresolved["final_recommendation"] == "recommend_buy"
    assert unresolved["final_category"] == "recommend_buy"
    assert unresolved["buy_attractiveness_score"] == observed["buy_attractiveness_score"]
    assert "确定性规则已触发" not in unresolved["summary"]
    assert "按接近达标口径扣分" not in unresolved["summary"]


def test_calibration_keeps_ai_score_independent_of_rule_status_and_score() -> None:
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
        "deterministic": {"status": "triggered", "score": 1.0},
    }

    triggered = _review(source)
    source["deterministic"] = {"status": "conditional", "score": 10.0}
    conditional = _review(source)

    assert triggered["buy_attractiveness_score"] == conditional["buy_attractiveness_score"] == 72
    assert conditional["ai_action"] == "priority_buy"
    assert conditional["ai_independent"] is True


def test_native_company_research_keeps_company_thesis_as_summary() -> None:
    source = {
        "ai_review": {
            "verdict": "caution",
            "recommended_action": "manual_review",
            "ai_action": "watchlist",
            "ai_independent": True,
            "buy_attractiveness_score": 58,
            "confidence": "high",
            "summary": "当前结论：观察。现金流尚可，但当前价格缺少足够安全边际。",
            "claims": [
                {
                    "statement": "2026年中报经营现金流11.81亿元",
                    "source_ref": "https://example.test/report",
                }
            ],
            "risk_flags": ["2026年中报净利润同比下降19.88%"],
            "web_search_performed": True,
            "native_search_completed": True,
            "model": "opencode-go/muse-spark-1.2-contributor",
            "effort": "xhigh",
            "retrieval_backend": "reasonix-native-server-web-search",
            "retrieval_model": "opencode-go-muse/muse-spark-1.2-contributor",
            "retrieval_effort": "xhigh",
        },
        "security_code": "002668",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 9.0},
    }

    review = _review(source, "2026-08-21")

    assert review["summary"].startswith("当前结论：观察。")
    assert "AI买入吸引力" not in review["summary"]
    assert review["confidence"] == "high"


def test_triggered_candidate_can_be_downgraded_by_the_ai_action() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 55,
            "claims": [{"source_ref": "https://example.test/report"}],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 10.0},
    }

    reviewed = _review(source)

    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["final_category"] == "observe"
    assert reviewed["buy_attractiveness_score"] == 55


def test_decision_text_collapses_repeated_negation_tokens() -> None:
    text = "当前暂不不不不不建议买入，纳入观察。"
    cleaned = normalise_decision_text(text)
    assert cleaned == "当前暂不建议买入，纳入观察。"
    assert "不不" not in cleaned
    assert decision_text_conflicts("watchlist", "不能据此直接建议买入。") is False
    assert decision_text_conflicts("watchlist", "当前建议买入。") is True
    assert decision_text_conflicts("priority_buy", "当前买入逻辑尚未成立，继续等待确认。") is True
    assert decision_text_conflicts("priority_buy", "暂时不考虑买入，等待下一季。") is True
    assert decision_text_conflicts("priority_buy", "当前不参与配置。") is True


def test_local_priority_buy_with_non_buy_summary_is_downgraded() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600662",
        "type_key": "type1",
        "verdict": "confirmed",
        "recommended_action": "keep",
        "buy_attractiveness_score": 82,
        "ai_action": "priority_buy",
        "final_category": "recommend_buy",
        "confidence": "low",
        "summary": "当前结论：观察。证据不足，暂不形成买点。",
        "key_strengths": ["待核验"],
        "risk_flags": ["资料不足"],
        "claims": [],
    }
    reviewed = _cohere_local_review(review)
    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["verdict"] == "caution"
    assert reviewed["recommended_action"] == "manual_review"
    assert reviewed["buy_attractiveness_score"] == 69.0
    assert validate_review(reviewed) == []


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


def test_2026_market_valuation_cannot_make_2024_financials_current() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 80,
            "claims": [
                {
                    "statement": "截至2026-08-21股价9.25元，PE 8.89倍，市值215亿元",
                    "source_ref": "https://example.test/market",
                },
                {
                    "dimension": "profitability",
                    "period": "2024-12-31",
                    "statement": "2024年年报归母净利润5.12亿元",
                    "source_ref": "https://example.test/annual-report",
                },
            ],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "002668",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }

    reviewed = _review(source, "2026-08-21")

    assert reviewed["freshness_status"] == "historical"
    assert reviewed["freshness_years"] == [2024]
    assert reviewed["freshness_penalty"] == 8.0
    assert reviewed["ai_action"] == "watchlist"
    assert "最新可识别实际报告期为 2024 年" in reviewed["freshness_note"]
    assert "2026" not in reviewed["freshness_years"]


def test_2025_financial_report_is_current_despite_separate_2026_valuation() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": 80,
            "claims": [
                {
                    "dimension": "valuation",
                    "period": "2026-08-21",
                    "statement": "2026-08-21收盘价9.25元，市盈率8.89倍",
                    "source_ref": "https://example.test/market",
                },
                {
                    "statement": "2025年年报营业收入120亿元，经营现金流18亿元",
                    "source_ref": "https://example.test/annual-report",
                },
            ],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "002668",
        "type_key": "type1",
        "deterministic": {"status": "conditional", "score": 7.5},
    }

    reviewed = _review(source, "2026-08-21")

    assert reviewed["freshness_status"] == "current_or_recent"
    assert reviewed["freshness_years"] == [2025]
    assert reviewed["freshness_penalty"] == 0.0
    assert reviewed["ai_action"] == "priority_buy"


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


def test_sub_50_insufficient_or_watchlist_becomes_do_not_recommend() -> None:
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
    assert unresolved["ai_action"] == "avoid"
    assert unresolved["final_category"] == "do_not_recommend"

    source["ai_review"]["ai_action"] = "watchlist"
    source["ai_review"]["buy_attractiveness_score"] = 42
    observed = _review(source)
    assert observed["ai_action"] == "avoid"
    assert observed["final_category"] == "do_not_recommend"


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
                "source_refs": [
                    "https://example.test/report.pdf这是报告说明",
                    "https://example.test/report-appendix.pdf",
                ],
            }
        ],
        "web_search_performed": True,
    }
    public = _public_review(review)
    assert public["claims"][0]["source_ref"] == "https://example.test/report.pdf"
    assert public["claims"][0]["source_refs"] == [
        "https://example.test/report.pdf",
        "https://example.test/report-appendix.pdf",
    ]
    assert public["web_search_verified"] is True


def test_public_review_preserves_complete_long_source_url() -> None:
    long_url = "https://example.test/" + ("long-segment-" * 100) + ".pdf?download=1"
    review = {
        "schema_version": 2,
        "security_code": "600339",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 60,
        "ai_action": "watchlist",
        "final_category": "observe",
        "confidence": "medium",
        "key_strengths": [],
        "risk_flags": [],
        "claims": [
            {
                "statement": "报告",
                "source_ref": "annual_report:2026:p42",
                "source_context": f"来源：{long_url}（正文）",
            }
        ],
    }

    public = _public_review(review)

    assert public["claims"][0]["source_ref"] == long_url
    assert public["claims"][0]["source_context"] == f"来源：{long_url}（正文）"


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
    assert manifest["candidate_count"] == 1
    assert manifest["type_pair_candidate_count"] == 2
    assert manifest["rule_file_count"] == 1
    enriched = json.loads((out / "ai-screening-input.json").read_text(encoding="utf-8"))
    assert enriched["packets"][0]["rule_context"]
    assert enriched["packets"][0]["type_keys"] == ["type1", "type7"]
    assert len(enriched["packets"][0]["candidate_types"]) == 2
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "caution",
                "recommended_action": "manual_review",
                "buy_attractiveness_score": 62,
                "ai_action": "watchlist",
                "confidence": "medium",
                "key_strengths": ["估值有安全边际"],
                "risk_flags": ["现金流仍需核验"],
                "claims": [{"source_ref": "annual_report_2025:p42"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    merged = out / "ai-screening.json"
    merge_reviews(out / "ai-screening-input.json", review_path, merged)
    value = json.loads(merged.read_text(encoding="utf-8"))
    assert value["packets"][0]["ai_review"]["verdict"] == "caution"
    assert len(value["packets"]) == 1


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
                "key_strengths": ["工程订单仍有盈利兑现空间"],
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
    assert artifact["ranking_version"] == "ai-buy-attractiveness-v9-score-first-action-banded"
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
    assert artifact["packets"][0]["type_keys"] == ["type1", "type7"]
    assert artifact["packets"][0]["type_pair_count"] == 2
    assert artifact["type_pair_candidate_total"] == 2
    assert artifact["type_pair_expected_total"] == 2
    assert artifact["watchlist_count"] == 1
    assert artifact["final_category_counts"] == {"recommend_buy": 0, "observe": 1, "do_not_recommend": 0}
    assert artifact["packets"][0]["ai_review"]["final_category"] == "observe"
    assert artifact["packets"][0]["ai_review"]["final_recommendation"] == "do_not_recommend_buy"
    assert artifact["do_not_recommend_buy_count"] == 1


def test_publish_ranks_final_score_before_action_category(tmp_path) -> None:
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
                        "name": "建议买且分数达标",
                        "type_key": "type1",
                        "deterministic": {"status": "observe", "score": 10},
                        "ai_review": {
                            **base,
                            "schema_version": 2,
                            "security_code": "600001",
                            "type_key": "type1",
                            "buy_attractiveness_score": 70,
                            "ai_action": "priority_buy",
                            "recommended_action": "keep",
                            "final_category": "recommend_buy",
                            "final_recommendation": "recommend_buy",
                            "summary": "当前估值与现金流提供安全边际，但需求仍可能波动。",
                            "quantitative_facts": [
                                "2025年度营业收入 120 亿元",
                                "2025年度经营现金流 18 亿元",
                            ],
                            "key_strengths": ["2025年度收入和经营现金流均保持正值"],
                            "risk_flags": ["下游需求波动可能压低盈利"],
                            "claims": [
                                {
                                    "statement": "2025年度营业收入 120 亿元",
                                    "source_ref": "https://example.test/report#revenue",
                                    "support": "supports",
                                },
                                {
                                    "statement": "2025年度经营现金流 18 亿元",
                                    "source_ref": "https://example.test/report#cashflow",
                                    "support": "supports",
                                },
                            ],
                        },
                    },
                    {
                        "security_code": "600002",
                        "name": "观察且分数低于建议买",
                        "type_key": "type1",
                        "deterministic": {"status": "triggered", "score": 7.8},
                        "ai_review": {
                            **base,
                            "schema_version": 2,
                            "security_code": "600002",
                            "type_key": "type1",
                            "buy_attractiveness_score": 69,
                            "ai_action": "watchlist",
                            "recommended_action": "manual_review",
                            "verdict": "caution",
                            "final_category": "observe",
                            "final_recommendation": "do_not_recommend_buy",
                            "summary": "当前估值和现金流仍需继续观察，但需求可能波动。",
                            "quantitative_facts": [
                                "2025年度营业收入 120 亿元",
                                "2025年度经营现金流 18 亿元",
                            ],
                            "key_strengths": ["2025年度收入和经营现金流均保持正值"],
                            "risk_flags": ["下游需求波动可能压低盈利"],
                            "claims": [
                                {
                                    "statement": "2025年度营业收入 120 亿元",
                                    "source_ref": "https://example.test/report#revenue",
                                    "support": "supports",
                                },
                                {
                                    "statement": "2025年度经营现金流 18 亿元",
                                    "source_ref": "https://example.test/report#cashflow",
                                    "support": "supports",
                                },
                            ],
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
    assert artifact["packets"][0]["security_code"] == "600001"
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
            "summary": "2025年度盈利和现金流共同支持买入，但工程订单兑现仍有波动风险。",
            "quantitative_facts": ["2025年度营业收入 120 亿元", "2025年度经营现金流 18 亿元"],
            "key_strengths": ["2025年度收入和经营现金流均保持正值"],
            "risk_flags": ["下游工程订单波动可能压低盈利"],
            "claims": [
                {
                    "statement": "2025年度营业收入 120 亿元",
                    "source_ref": "snapshot:600339:2025:revenue",
                    "support": "supports",
                },
                {
                    "statement": "2025年度经营现金流 18 亿元",
                    "source_ref": "snapshot:600339:2025:cashflow",
                    "support": "supports",
                },
            ],
            "model": "codex-local-review-v1",
            "effort": "max",
            "web_search_performed": False,
        }
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
                "key_strengths": ["工程订单值得进一步核验"],
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
    # One company-level review covers every admitted type pair without
    # manufacturing a second, potentially more optimistic opinion.
    assert artifact["candidate_total"] == 1
    assert artifact["unreviewed_candidate_count"] == 0
    assert artifact["type_pair_candidate_total"] == 2
    assert artifact["type_pair_unreviewed_count"] == 0
    assert artifact["type_pair_needs_review_count"] == 2
    assert artifact["attempted_needs_review_count"] == 1
    assert artifact["pending_review_count"] == 0


def test_publish_rejects_multiple_ai_opinions_for_one_company(tmp_path) -> None:
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
    with pytest.raises(ValueError, match="duplicate or incomplete company candidate"):
        build_artifact(
            merged,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-13",
        )


def test_publish_company_review_retains_every_candidate_type_pair(tmp_path) -> None:
    merged = tmp_path / "merged.json"
    merged.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-13",
                "candidate_count": 1,
                "candidate_total": 1,
                "type_pair_candidate_count": 2,
                "type_pair_candidate_total": 2,
                "packets": [
                    {
                        "security_code": "600339",
                        "name": "中油工程",
                        "type_key": "type1",
                        "type_keys": ["type1", "type7"],
                        "candidate_types": [
                            {
                                "type_key": "type1",
                                "deterministic": {"status": "triggered", "score": 7.8},
                            },
                            {
                                "type_key": "type7",
                                "deterministic": {"status": "observe", "score": 7.2},
                            },
                        ],
                        "deterministic": {"status": "triggered", "score": 7.8},
                        "ai_review": {
                            "schema_version": 2,
                            "security_code": "600339",
                            "type_key": "type1",
                            "verdict": "caution",
                            "recommended_action": "manual_review",
                            "buy_attractiveness_score": 62,
                            "ai_action": "watchlist",
                            "confidence": "medium",
                            "summary": "工程订单有盈利空间，但现金流仍需持续核验。",
                            "key_strengths": ["在手工程订单仍有兑现空间"],
                            "risk_flags": ["回款速度可能拖累经营现金流"],
                            "claims": [],
                            "model": "opencode-go/test",
                        },
                    }
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

    packet = artifact["packets"][0]
    assert artifact["review_granularity"] == "company"
    assert artifact["company_deduplication"] == "company_level_review"
    assert artifact["candidate_total"] == 1
    assert artifact["type_pair_candidate_total"] == 2
    assert artifact["type_pair_reviewed_count"] == 2
    assert packet["type_key"] == "type1"
    assert packet["type_keys"] == ["type1", "type7"]
    assert [item["type_key"] for item in packet["candidate_types"]] == ["type1", "type7"]
    assert packet["ai_review"]["ai_action"] == "watchlist"
