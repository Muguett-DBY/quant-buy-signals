from __future__ import annotations

import json

from tools.ai_screening_contract import select_candidates, validate_review
from tools.build_ai_screening import build_input, merge_reviews
from tools.calibrate_ai_screening_ranking import _review
from tools.publish_ai_screening import _public_review, build_artifact
from tools.prepare_ai_screening_overlay import prepare


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
        "verdict": "caution",
        "recommended_action": "manual_review",
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


def test_calibrated_priority_requires_a_deterministic_trigger() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "claims": [{"source_ref": "https://example.test/report"}],
            "risk_flags": [],
            "web_search_performed": True,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    assert _review(source)["ai_action"] == "priority_buy"

    source["deterministic"]["status"] = "observe"
    observed = _review(source)
    assert observed["ai_action"] == "watchlist"
    assert observed["buy_attractiveness_score"] <= 78

    source["deterministic"]["status"] = "insufficient_evidence"
    unresolved = _review(source)
    assert unresolved["ai_action"] == "insufficient_evidence"
    assert unresolved["buy_attractiveness_score"] <= 64


def test_unsearched_confirmation_cannot_be_priority_and_searched_score_is_used() -> None:
    source = {
        "ai_review": {
            "verdict": "confirmed",
            "recommended_action": "keep",
            "claims": [],
            "risk_flags": [],
            "buy_attractiveness_score": 91,
        },
        "security_code": "600339",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
    }
    unsearched = _review(source)
    assert unsearched["ai_action"] == "watchlist"

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
    assert public["claims"][0]["source_ref"] == ""
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
    review_path = tmp_path / "reviews.jsonl"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "security_code": "600339",
                "type_key": "type1",
                "verdict": "caution",
                "recommended_action": "manual_review",
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
    assert value["packets"][0]["ai_review"]["verdict"] == "caution"
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
    assert artifact["ranking_version"] == "ai-buy-attractiveness-v3-web-gated"
    assert artifact["reviewed_count"] == 1
    assert artifact["packets"][0]["deterministic"]["status"] == "triggered"
    assert artifact["attempted_review_count"] == 1
    assert artifact["unreviewed_candidate_count"] == 0
    assert artifact["attempted_needs_review_count"] == 0
    assert artifact["completed_review_count"] == 1
    assert artifact["pending_review_count"] == 0
    assert artifact["packets"][0]["ai_rank"] == 1
    assert artifact["watchlist_count"] == 1


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
                "verdict": "caution",
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
        "attempted_needs_review": 0,
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
    assert artifact["unreviewed_candidate_count"] == 1
    assert artifact["attempted_needs_review_count"] == 1
    assert artifact["pending_review_count"] == 1
