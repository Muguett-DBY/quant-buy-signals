from __future__ import annotations

import pytest

from tools.validate_ai_screening_public import validate_artifact


def _artifact() -> dict:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 60,
        "ai_action": "watchlist",
        "final_category": "observe",
        "final_recommendation": "do_not_recommend_buy",
        "recommendation_label": "观察",
        "confidence": "medium",
        "summary": "当前估值与经营质量仍需继续观察。",
        "key_strengths": ["规则候选分数较高"],
        "risk_flags": ["仍需跟踪现金流变化"],
        "claims": [{"statement": "最新报告", "source_ref": "https://example.test/report"}],
        "model": "opencode-go/ox-alpha-free",
        "effort": "max",
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": True,
        "web_search_verified": True,
        "web_search_query_count": 1,
        "web_search_verified_claim_url_count": 1,
        "web_search_dropped_claim_url_count": 0,
    }
    return {
        "schema_version": 2,
        "review_schema_version": 2,
        "artifact_kind": "ai_screening_overlay",
        "ai_is_advisory": True,
        "auto_buy_promotion": False,
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "candidate_offset": 0,
        "full_coverage_final_recommendation": True,
        "review_mode": "opencode_web_review",
        "review_models": ["opencode-go/ox-alpha-free"],
        "review_efforts": ["max"],
        "candidate_total": 1,
        "reviewed_count": 1,
        "candidate_identity_sha256": "a" * 64,
        "candidate_universe_identity_sha256": "a" * 64,
        "type_pair_candidate_total": 1,
        "type_pair_expected_total": 1,
        "type_pair_reviewed_count": 1,
        "type_pair_unreviewed_count": 0,
        "ai_action_counts": {"priority_buy": 0, "watchlist": 1, "avoid": 0, "insufficient_evidence": 0},
        "final_category_counts": {"recommend_buy": 0, "observe": 1, "do_not_recommend": 0},
        "insufficient_evidence_count": 0,
        "reviewed_without_web_search": 0,
        "web_search_attempted_count": 1,
        "web_search_event_verified_count": 1,
        "web_search_claim_urls_verified_count": 1,
        "web_search_dropped_claim_url_count": 0,
        "source_audit": {"available": True, "invalid_claim_url_count": 0},
        "packets": [
            {
                "security_code": "600000",
                "name": "浦发银行",
                "type_key": "type1",
                "type_keys": ["type1"],
                "type_pair_count": 1,
                "ai_rank": 1,
                "ai_review": review,
            }
        ],
    }


def test_public_validator_accepts_generation_bound_full_seed() -> None:
    result = validate_artifact(_artifact(), expected_generation="g1", expected_market_as_of="2026-08-21")
    assert result["candidate_total"] == 1
    assert result["searched"] == 1


def test_public_validator_rejects_stale_generation() -> None:
    with pytest.raises(ValueError, match="generation"):
        validate_artifact(_artifact(), expected_generation="g2", expected_market_as_of="2026-08-21")


def test_public_validator_rejects_missing_search_proof() -> None:
    value = _artifact()
    value["packets"][0]["ai_review"]["web_search_event_verified"] = False
    with pytest.raises(ValueError, match="search proof|semantically invalid"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_public_validator_accepts_mixed_local_review_with_explicit_audit() -> None:
    value = _artifact()
    value["review_mode"] = "opencode_mixed_review"
    value["reviewed_without_web_search"] = 1
    value["web_search_attempted_count"] = 0
    value["web_search_event_verified_count"] = 0
    value["web_search_claim_urls_verified_count"] = 0
    review = value["packets"][0]["ai_review"]
    review["claims"] = []
    review["web_search_performed"] = False
    review["web_search_event_verified"] = False
    review["web_search_claim_urls_verified"] = False
    assert validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")["searched"] == 0
