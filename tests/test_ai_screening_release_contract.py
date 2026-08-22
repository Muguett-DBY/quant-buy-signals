from __future__ import annotations

import json
import hashlib

import pytest

from tools.ai_screening_contract import candidate_identity_sha256, validate_review
from tools.apply_ai_screening_human_review import apply as apply_human_review
from tools.calibrate_ai_screening_ranking import _review, calibrate
from tools.build_ai_screening import build_input
from tools.publish_ai_screening import build_artifact


def _external_review(action: str, score: float) -> dict:
    return {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": (
            "misclassified"
            if action == "avoid"
            else "needs_review"
            if action == "insufficient_evidence"
            else "confirmed"
        ),
        "recommended_action": (
            "demote" if action == "avoid" else "manual_review" if action == "insufficient_evidence" else "keep"
        ),
        "buy_attractiveness_score": score,
        "ai_action": action,
        "final_category": {
            "priority_buy": "recommend_buy",
            "watchlist": "observe",
            "avoid": "do_not_recommend",
            "insufficient_evidence": "observe",
        }[action],
        "confidence": "medium",
        "summary": "已复核估值、经营质量和主要风险，结论按当前动作执行。",
        "key_strengths": ["估值与规则基础可供复核"],
        "risk_flags": ["经营兑现仍需持续跟踪"],
        "claims": [],
        "model": "opencode-go/ox-alpha-free",
    }


def _with_candidate_identity(payload: dict) -> dict:
    digest = candidate_identity_sha256(payload["packets"])
    payload["candidate_identity_sha256"] = digest
    payload["candidate_universe_identity_sha256"] = digest
    return payload


def _write_clean_source_audit(source, audit_path) -> None:
    audit_path.write_text(
        json.dumps(
            {
                "merged_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "invalid_claim_url_count": 0,
                "checked": 1,
                "ok": 1,
                "failed": 0,
                "blocked": 0,
                "invalid": 0,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("action", "score", "error"),
    [
        ("priority_buy", 59, "priority_score_band"),
        ("watchlist", 100, "watchlist_score_band"),
        ("avoid", 100, "negative_score_band"),
        ("insufficient_evidence", 50, "negative_score_band"),
    ],
)
def test_score_bands_apply_to_external_models(action: str, score: float, error: str) -> None:
    assert error in validate_review(_external_review(action, score))


def test_calibration_bands_model_scores_to_the_final_action() -> None:
    packet = {
        "security_code": "600000",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
        "ai_review": {
            **_external_review("watchlist", 100),
            "claims": [
                {
                    "statement": "2025 年年度报告已披露",
                    "source_ref": "https://example.test/2025-report.pdf",
                    "support": "supports",
                }
            ],
            "web_search_performed": True,
        },
    }
    observed = _review(packet, "2026-08-21")
    assert observed["ai_action"] == "watchlist"
    assert observed["buy_attractiveness_score"] == 69.0
    assert validate_review(observed) == []

    packet["ai_review"] = {
        **packet["ai_review"],
        "verdict": "misclassified",
        "recommended_action": "demote",
        "ai_action": "avoid",
        "buy_attractiveness_score": 100,
    }
    avoided = _review(packet, "2026-08-21")
    assert avoided["ai_action"] == "avoid"
    assert avoided["buy_attractiveness_score"] == 49.0
    assert validate_review(avoided) == []


def test_action_label_and_summary_cannot_contradict_the_decision() -> None:
    review = {
        **_external_review("priority_buy", 70),
        "recommendation_label": "观察",
        "summary": "当前列为观察，暂不建议买入。",
    }
    errors = validate_review(review)
    assert "recommendation_label_action_mismatch" in errors
    assert "summary_action_mismatch" in errors


@pytest.mark.parametrize(
    ("action", "verdict", "recommended_action", "expected_error"),
    [
        ("priority_buy", "caution", "keep", "action_verdict_mismatch"),
        ("priority_buy", "confirmed", "manual_review", "action_recommended_action_mismatch"),
        ("watchlist", "misclassified", "demote", "misclassified_decision_mismatch"),
        ("avoid", "needs_review", "manual_review", "needs_review_decision_mismatch"),
        ("insufficient_evidence", "confirmed", "keep", "action_verdict_mismatch"),
    ],
)
def test_action_verdict_and_review_action_must_form_one_decision(
    action: str,
    verdict: str,
    recommended_action: str,
    expected_error: str,
) -> None:
    score = 70 if action == "priority_buy" else 55 if action == "watchlist" else 40
    review = {
        **_external_review(action, score),
        "verdict": verdict,
        "recommended_action": recommended_action,
    }
    assert expected_error in validate_review(review)


@pytest.mark.parametrize(
    ("action", "summary"),
    [
        ("priority_buy", "综合判断，当前不建议买入，应继续等待。"),
        ("priority_buy", "估值尚可，但建议继续观望。"),
        ("priority_buy", "现阶段没有明确买点，也不值得配置。"),
        ("watchlist", "基本面优秀，强烈推荐买入。"),
        ("watchlist", "综合结论：买入，可以开始布局。"),
        ("avoid", "现阶段值得买入并分批建仓。"),
        ("avoid", "当前是明确买点，可逐步介入。"),
        ("insufficient_evidence", "AI 独立建议买入。"),
        ("priority_buy", "综合判断建议观察。"),
        ("priority_buy", "目前只宜观望。"),
        ("priority_buy", "建议不要买入。"),
        ("watchlist", "这家公司值得买。"),
        ("avoid", "当前可以买。"),
        ("insufficient_evidence", "AI 结论：建议买。"),
    ],
)
def test_obvious_current_recommendation_phrases_must_match_the_action(action: str, summary: str) -> None:
    score = 70 if action == "priority_buy" else 55 if action == "watchlist" else 40
    review = {**_external_review(action, score), "summary": summary}
    assert "summary_action_mismatch" in validate_review(review)


@pytest.mark.parametrize(
    ("action", "summary"),
    [
        ("priority_buy", "当前具备买入价值，但不建议追高买入；若现金流恶化则应回避。"),
        ("watchlist", "若价格回落至安全边际，可分批买入；当前继续观察。"),
        ("avoid", "券商历史上曾给出买入评级，但当前不建议买入。"),
        ("avoid", "券商建议买入，但本轮基于现金流恶化仍给出不建议结论。"),
        ("watchlist", "模型给出的建议买入区间低于现价，因此当前维持观察。"),
        ("priority_buy", "需要观察现金流和利润质量，但当前建议分批买入。"),
    ],
)
def test_conditional_or_risk_buy_language_is_not_mistaken_for_the_current_action(action: str, summary: str) -> None:
    score = 70 if action == "priority_buy" else 55 if action == "watchlist" else 40
    review = {**_external_review(action, score), "summary": summary}
    assert "summary_action_mismatch" not in validate_review(review)


def test_recommendation_label_cannot_hide_an_opposite_current_decision() -> None:
    review = {
        **_external_review("watchlist", 60),
        "recommendation_label": "观察·强烈推荐买入",
    }
    assert "recommendation_label_action_mismatch" in validate_review(review)


def test_strict_public_reason_requires_readable_summary_strength_and_risk() -> None:
    review = {
        **_external_review("priority_buy", 72),
        "summary": "当前估值和现金流共同提供安全边际。",
        "key_strengths": ["现金流改善"],
        "risk_flags": ["需求仍可能波动"],
    }
    assert validate_review(review, require_readable_reason=True) == []

    empty = {**review, "summary": "证据不足", "key_strengths": [], "risk_flags": [""]}
    errors = validate_review(empty, require_readable_reason=True)
    assert {
        "readable_summary_required",
        "readable_key_strengths_required",
        "readable_risk_flags_required",
    }.issubset(errors)


def test_partial_queue_cannot_be_calibrated_or_published_as_full_coverage(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "web_search_performed": True,
    }
    packet = {
        "security_code": "600000",
        "name": "浦发银行",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
        "ai_review": review,
    }
    partial = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "candidate_offset": 0,
        "candidate_count": 1,
        "candidate_total": 2,
        "full_coverage_final_recommendation": True,
        "review_mode": "opencode_web_review",
        "packets": [packet],
    }
    source = tmp_path / "partial.json"
    source.write_text(json.dumps(partial), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate identity hash|complete unique reviewed candidate queue"):
        calibrate(source, tmp_path / "calibrated.json")
    with pytest.raises(ValueError, match="complete candidate queue"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_human_review_downgrade_is_generation_bound_and_keeps_reason(tmp_path) -> None:
    source = tmp_path / "source.json"
    corrections = tmp_path / "corrections.json"
    output = tmp_path / "output.json"
    source.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "packets": [
                    {
                        "security_code": "600000",
                        "type_key": "type1",
                        "ai_review": {
                            **_external_review("priority_buy", 72),
                            "summary": "当前建议买入。",
                            "risk_flags": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    corrections.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "corrections": {"600000": {"action": "watchlist", "score": 55, "reason": "价格闸门未通过。"}},
            }
        ),
        encoding="utf-8",
    )
    assert apply_human_review(source, corrections, output) == {"changed_packets": 1, "changed_companies": 1}
    reviewed = json.loads(output.read_text(encoding="utf-8"))["packets"][0]["ai_review"]
    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["final_category"] == "observe"
    assert reviewed["final_recommendation"] == "do_not_recommend_buy"
    assert "价格闸门未通过" in reviewed["summary"]


def test_full_coverage_publish_requires_model_and_effort_metadata(tmp_path) -> None:
    source = tmp_path / "full.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "local_codex_review",
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": _external_review("watchlist", 60),
                        }
                    ],
                }
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="review models and efforts"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_publish_retains_external_search_event_and_claim_binding_proof(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "effort": "max",
        "summary": "2026 年最新报告与当前估值已经完成二次复核，结论为继续观察。",
        "key_strengths": ["2026 年最新报告显示主营业务保持稳定。"],
        "risk_flags": ["当前估值仍需保留安全边际，暂不提高仓位。"],
        "claims": [
            {
                "statement": "2026 年最新正式报告已经披露。",
                "source_ref": "https://example.test/2026-report.pdf",
            }
        ],
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": True,
        "web_search_queries": ["600000 浦发银行 2026 最新报告"],
        "web_search_verified_claim_urls": ["https://example.test/2026-report.pdf"],
        "web_search_dropped_claim_url_count": 1,
        "freshness_status": "current_or_recent",
        "freshness_years": [2026],
        "freshness_penalty": 0,
    }
    source = tmp_path / "full-external.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "opencode_web_review",
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": review,
                        }
                    ],
                }
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit_path = tmp_path / "source-audit.json"
    _write_clean_source_audit(source, audit_path)
    artifact = build_artifact(
        source,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
        source_audit_path=audit_path,
    )

    assert artifact["full_coverage_web_search"] is True
    assert artifact["type_pair_web_search_event_verified_count"] == 1
    assert artifact["type_pair_web_search_claim_urls_verified_count"] == 1
    assert artifact["type_pair_web_search_dropped_claim_url_count"] == 1
    assert artifact["web_search_event_verified_count"] == 1
    assert artifact["web_search_claim_urls_verified_count"] == 1
    assert artifact["web_search_dropped_claim_url_count"] == 1
    assert artifact["packets"][0]["ai_review"]["web_search_event_verified"] is True
    assert artifact["packets"][0]["ai_review"]["web_search_claim_urls_verified"] is True
    assert artifact["packets"][0]["ai_review"]["web_search_dropped_claim_url_count"] == 1


def test_external_full_coverage_publish_requires_bound_source_audit(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "effort": "max",
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": True,
        "web_search_queries": ["600000 浦发银行 最新报告"],
        "web_search_verified_claim_urls": [],
    }
    payload = _with_candidate_identity(
        {
            "snapshot_generation": "g1",
            "market_as_of": "2026-08-21",
            "candidate_offset": 0,
            "candidate_count": 1,
            "candidate_total": 1,
            "full_coverage_final_recommendation": True,
            "review_mode": "opencode_web_review",
            "packets": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "deterministic": {"status": "triggered", "score": 8.0},
                    "ai_review": review,
                }
            ],
        }
    )
    source = tmp_path / "full-external-no-audit.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="requires a bound source audit"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_local_full_coverage_rejects_external_model_label(tmp_path) -> None:
    review = {**_external_review("watchlist", 60), "effort": "max"}
    payload = _with_candidate_identity(
        {
            "snapshot_generation": "g1",
            "market_as_of": "2026-08-21",
            "candidate_offset": 0,
            "candidate_count": 1,
            "candidate_total": 1,
            "full_coverage_final_recommendation": True,
            "review_mode": "local_codex_review",
            "packets": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "deterministic": {"status": "triggered", "score": 8.0},
                    "ai_review": review,
                }
            ],
        }
    )
    source = tmp_path / "local-wrong-model.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="local Codex review model"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_opencode_review_mode_marks_only_the_complete_queue_as_full(tmp_path) -> None:
    snapshot = {
        "generation": "g1",
        "market_as_of": "2026-08-21",
        "companies": [
            {
                "code": "600000",
                "name": "浦发银行",
                "types": {
                    "type1": {"status": "triggered", "score": 8.0},
                    "type2": {"status": "conditional", "score": 6.8, "score_upper_bound": 7.2},
                },
            }
        ],
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "rules.md").write_text("# type1\ntype1 rules\n# type2\ntype2 rules", encoding="utf-8")

    full = build_input(snapshot_path, rules, tmp_path / "full", review_mode="opencode_web_review")
    assert full["queue_full_coverage"] is True
    assert full["full_coverage_final_recommendation"] is True

    partial = build_input(
        snapshot_path,
        rules,
        tmp_path / "partial",
        review_mode="opencode_web_review",
        limit=1,
    )
    assert partial["queue_full_coverage"] is False
    assert partial["full_coverage_final_recommendation"] is False
