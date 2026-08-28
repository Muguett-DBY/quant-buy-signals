from __future__ import annotations

from tools.ai_screening_comparison import build_day_over_day


def _packet(code: str, name: str, category: str, score: float, reason: str = "") -> dict:
    return {
        "security_code": code,
        "name": name,
        "ai_review": {
            "final_category": category,
            "buy_attractiveness_score": score,
            "human_explanation": {"why_this_action": reason or "今日按公司事实重新形成结论。"},
        },
    }


def test_day_over_day_reports_upgrades_downgrades_new_and_removed() -> None:
    previous = {
        "snapshot_generation": "old-generation",
        "market_as_of": "2026-08-27",
        "packets": [
            _packet("000001", "甲公司", "observe", 55),
            _packet("000002", "乙公司", "recommend_buy", 80),
            _packet("000003", "丙公司", "observe", 60),
        ],
    }
    current = {
        "snapshot_generation": "new-generation",
        "market_as_of": "2026-08-28",
        "packets": [
            _packet("000001", "甲公司", "recommend_buy", 72, "经营改善已能和价格条件相互印证。"),
            _packet("000002", "乙公司", "do_not_recommend", 45),
            _packet("000004", "丁公司", "observe", 61),
        ],
    }

    comparison = build_day_over_day(current, previous)
    by_code = {item["security_code"]: item for item in comparison["changes"]}

    assert comparison["available"] is True
    assert comparison["matched_count"] == 2
    assert comparison["upgraded_to_recommend_buy_count"] == 1
    assert comparison["downgraded_from_recommend_buy_count"] == 1
    assert comparison["new_candidate_count"] == 1
    assert comparison["removed_candidate_count"] == 1
    assert by_code["000001"]["direction"] == "upgraded"
    assert by_code["000001"]["reason"].endswith("经营改善已能和价格条件相互印证。")
    assert by_code["000003"]["direction"] == "left_candidate_pool"
