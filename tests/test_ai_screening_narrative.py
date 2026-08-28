from __future__ import annotations

from tools.ai_screening_narrative import build_human_explanation


def test_human_explanation_puts_company_logic_before_valuation_dump() -> None:
    review = {
        "ai_action": "priority_buy",
        "summary": "估值快照：股价 10 元；PE 12 倍；公司收入增长，经营现金流改善；独立结论为recommend_buy",
        "key_strengths": ["收入和归母净利润同比增长，经营活动现金流量同步改善", "海外销售增长且占收入比重较高"],
        "risk_flags": ["应收账款占资产比重较高，回款需要跟踪", "工程机械行业仍有周期波动"],
    }

    explanation = build_human_explanation(review, "山推股份")

    assert explanation["heading"] == "为什么建议买"
    assert explanation["thesis"].startswith("山推股份值得考虑的核心")
    assert "估值快照" not in explanation["thesis"]
    assert "规则" not in explanation["why_this_action"]
    assert "候选" not in explanation["why_this_action"]
    assert explanation["supporting_points"]
    assert explanation["watch_items"]
    assert explanation["supporting_points"][0] == "收入、利润和经营现金流同步改善"
    assert "模板汇总MD" in explanation["knowledge_base_note"]


def test_human_explanation_uses_action_specific_language() -> None:
    for action, heading, marker in (
        ("watchlist", "为什么先观察", "先观察"),
        ("avoid", "为什么不建议买", "不适合买入"),
    ):
        explanation = build_human_explanation(
            {
                "ai_action": action,
                "summary": "经营现金流下降，当前价格安全边际不足",
                "key_strengths": ["收入保持增长"],
                "risk_flags": ["利润转成现金的能力仍需验证"],
            },
            "测试公司",
        )
        assert explanation["heading"] == heading
        assert marker in explanation["thesis"] or marker in explanation["why_this_action"]
        assert "候选" not in explanation["thesis"]
