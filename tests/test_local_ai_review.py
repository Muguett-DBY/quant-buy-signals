from tools.ai_screening_contract import validate_review
from tools.build_local_ai_review import _review


def _packet(*, status="insufficient_evidence", score=10.0, upper=10.0, veto="possible", reason=""):
    return {
        "security_code": "600000",
        "name": "测试公司",
        "type_key": "type7",
        "deterministic": {
            "status": status,
            "score": score,
            "score_upper_bound": upper,
            "veto_state": veto,
            "reason": reason,
        },
    }


def test_local_negative_conclusion_never_inherits_rule_upper_bound() -> None:
    reviewed = _review(_packet())
    assert reviewed["ai_action"] == "avoid"
    assert reviewed["final_category"] == "do_not_recommend"
    assert reviewed["buy_attractiveness_score"] == 40.0
    assert reviewed["buy_attractiveness_score"] < 50
    assert validate_review(reviewed) == []


def test_local_watchlist_score_stays_below_buy_band() -> None:
    reviewed = _review(_packet(status="triggered", veto="none", reason="最新报告仍待核对"))
    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["final_category"] == "observe"
    assert reviewed["buy_attractiveness_score"] == 69.0
    assert reviewed["buy_attractiveness_score"] < 70
    assert validate_review(reviewed) == []


def test_local_contract_rejects_high_score_avoid() -> None:
    review = _review(_packet())
    review["buy_attractiveness_score"] = 100
    assert "local_negative_score_band" in validate_review(review)
