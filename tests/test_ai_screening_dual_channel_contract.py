from __future__ import annotations

import pytest

from tools.ai_screening_dual_channel_contract import (
    DO_NOT_RECOMMEND,
    OBSERVE,
    RECOMMEND_BUY,
    DualChannelContractError,
    RESEARCH_CHANNEL,
    SECOND_REVIEW_CHANNEL,
    validate_dual_channel_reviews,
)


def _review(
    code: str,
    *,
    channel: str,
    reviewer_id: str,
    action: str,
    confidence: str = "high",
    reason: str = "2025年度经营现金流为正，当前估值与盈利质量仍需结合风险判断。",
) -> dict:
    return {
        "security_code": code,
        "company_name": "测试公司" + code,
        "channel": channel,
        "reviewer_id": reviewer_id,
        "action": action,
        "confidence": confidence,
        "web_search_event_verified": True,
        "research_source_urls_verified": channel == RESEARCH_CHANNEL,
        "snapshot_generation": "generation-1",
        "market_as_of": "2026-08-24",
        "decision_reason": reason,
    }


def _join(research: list[dict], second: list[dict]) -> dict:
    return validate_dual_channel_reviews(
        research,
        second,
        expected_company_codes=["600000"],
        expected_generation="generation-1",
        expected_market_as_of="2026-08-24",
    )


def test_matching_high_confidence_buy_requires_consensus() -> None:
    result = _join(
        [_review("600000", channel=RESEARCH_CHANNEL, reviewer_id="researcher-1", action="recommend_buy")],
        [_review("600000", channel=SECOND_REVIEW_CHANNEL, reviewer_id="reviewer-1", action="recommend_buy")],
    )

    row = result["companies"][0]
    assert row["final_action"] == RECOMMEND_BUY
    assert row["final_category"] == RECOMMEND_BUY
    assert row["recommendation_label"] == "建议买"
    assert row["confidence"] == "high"
    assert row["conflict"] is False
    assert result["high_confidence_count"] == 1


@pytest.mark.parametrize(
    ("second_action", "expected"),
    [("observe", OBSERVE), ("do_not_recommend", DO_NOT_RECOMMEND)],
)
def test_disagreement_never_remains_a_buy(second_action: str, expected: str) -> None:
    result = _join(
        [_review("600000", channel=RESEARCH_CHANNEL, reviewer_id="researcher-1", action="recommend_buy")],
        [_review("600000", channel=SECOND_REVIEW_CHANNEL, reviewer_id="reviewer-1", action=second_action)],
    )

    row = result["companies"][0]
    assert row["final_action"] == expected
    assert row["confidence"] == "low"
    assert row["conflict"] is True
    assert "不一致" in row["decision_reason"]
    assert result["conflict_count"] == 1


def test_company_coverage_is_exact_and_duplicate_free() -> None:
    review = _review("600000", channel=RESEARCH_CHANNEL, reviewer_id="researcher-1", action="observe")
    second = _review("600000", channel=SECOND_REVIEW_CHANNEL, reviewer_id="reviewer-1", action="observe")
    with pytest.raises(DualChannelContractError, match="duplicate native_research"):
        validate_dual_channel_reviews(
            [review, dict(review)],
            [second],
            expected_company_codes=["600000"],
        )
    with pytest.raises(DualChannelContractError, match="coverage mismatch"):
        validate_dual_channel_reviews(
            [review],
            [second],
            expected_company_codes=["600000", "600001"],
        )


def test_deterministic_context_is_not_a_reason_but_rule_language_in_reason_fails() -> None:
    research = _review(
        "600000",
        channel=RESEARCH_CHANNEL,
        reviewer_id="researcher-1",
        action="observe",
        reason="经营现金流转弱，估值安全边际不足。",
    )
    research["deterministic"] = {"type_key": "type1", "status": "triggered", "score": 8.0}
    second = _review(
        "600000",
        channel=SECOND_REVIEW_CHANNEL,
        reviewer_id="reviewer-1",
        action="observe",
        reason="行业竞争和治理反证仍需继续跟踪。",
    )
    assert _join([research], [second])["companies"][0]["final_action"] == OBSERVE

    research["decision_reason"] = "因为 type1 已触发，所以建议买入。"
    with pytest.raises(DualChannelContractError, match="rule language"):
        _join([research], [second])


def test_search_proof_and_independence_are_required() -> None:
    research = _review("600000", channel=RESEARCH_CHANNEL, reviewer_id="same", action="observe")
    second = _review("600000", channel=SECOND_REVIEW_CHANNEL, reviewer_id="same", action="observe")
    with pytest.raises(DualChannelContractError, match="not independent"):
        _join([research], [second])

    second["reviewer_id"] = "reviewer-1"
    second["web_search_event_verified"] = False
    with pytest.raises(DualChannelContractError, match="native web-search proof"):
        _join([research], [second])


def test_insufficient_evidence_maps_to_observe_as_public_third_action() -> None:
    result = _join(
        [_review("600000", channel=RESEARCH_CHANNEL, reviewer_id="researcher-1", action="insufficient_evidence")],
        [_review("600000", channel=SECOND_REVIEW_CHANNEL, reviewer_id="reviewer-1", action="observe")],
    )
    assert result["companies"][0]["final_action"] == OBSERVE
    assert set(result["final_category_counts"]) == {DO_NOT_RECOMMEND, OBSERVE, RECOMMEND_BUY}
