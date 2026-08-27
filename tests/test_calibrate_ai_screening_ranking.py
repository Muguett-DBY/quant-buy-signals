from __future__ import annotations

import pytest

from tools.calibrate_ai_screening_ranking import (
    _RELEASE_RULE_REASON_RE,
    _action_safe_reason,
    _action_safe_summary,
    _review,
)


def _legacy_packet(
    *,
    code: str,
    score: float,
    pe: float,
    pb: float,
    facts: list[str],
    summary: str = "公司经营事实已整理。",
) -> dict:
    return {
        "security_code": code,
        "type_key": "type7",
        "company_context": {"pe": pe, "pb": pb, "quantitative_facts": []},
        "ai_review": {
            "verdict": "confirmed",
            "ai_action": "priority_buy",
            "buy_attractiveness_score": score,
            "ai_independent": True,
            "summary": summary,
            "key_strengths": ["经营现金流与自由现金流均有披露。"],
            "risk_flags": [],
            "quantitative_facts": facts,
            "claims": [
                {
                    "statement": "2026年中报已披露经营事实。",
                    "source_ref": "https://example.test/report",
                    "support": "supports",
                }
            ],
            "model": "codex-luna-max",
            "effort": "max",
            "confidence": "medium",
            "web_search_performed": True,
            "web_search_verified": True,
        },
    }


@pytest.mark.parametrize(
    ("model", "effort", "retrieval_model"),
    [
        ("opencode-go/ox-alpha-free", "max", "opencode-go/ox-alpha-free"),
        (
            "opencode/muse-spark-1.2-contributor-free",
            "xhigh",
            "opencode/muse-spark-1.2-contributor-free",
        ),
    ],
)
def test_opencode_native_company_profiles_preserve_complete_evidence_graph(
    model: str,
    effort: str,
    retrieval_model: str,
) -> None:
    claims = [
        {
            "statement": f"2025年年报经营事实 {index}",
            "source_ref": f"https://example.test/report/{index}",
            "support": "supports",
        }
        for index in range(13)
    ]
    search_findings = [
        {
            "id": f"finding-{index}",
            "query": f"公司 经营事实 {index}",
            "url": f"https://example.test/report/{index}",
        }
        for index in range(13)
    ]
    evidence_bindings = {
        "summary": {
            "fact_ids": ["fact-1"],
            "search_finding_ids": ["finding-1"],
        }
    }
    source_review = {
        "verdict": "caution",
        "ai_action": "watchlist",
        "buy_attractiveness_score": 60,
        "ai_independent": True,
        "summary": "公司研究结论。",
        "key_strengths": ["主营业务与现金流相互印证。"],
        "risk_flags": [],
        "claims": claims,
        "model": model,
        "effort": effort,
        "retrieval_backend": "opencode-native-client-websearch",
        "retrieval_model": retrieval_model,
        "retrieval_effort": effort,
        "native_search_completed": True,
        "web_search_performed": True,
        "research_source_urls_verified": True,
        "research_as_of": "2026-08-24",
        "economic_profile": {"business_model": "主营产品销售。"},
        "valuation": {"method": "scenario_multiple"},
        "search_findings": search_findings,
        "evidence_bindings": evidence_bindings,
    }

    calibrated = _review(
        {
            "security_code": "600000",
            "type_key": "type1",
            "ai_review": source_review,
        },
        "2026-08-24",
    )

    assert calibrated["claims"] == claims
    assert calibrated["search_findings"] == search_findings
    assert calibrated["evidence_bindings"] == evidence_bindings
    assert calibrated["key_strengths"] == ["主营业务与现金流相互印证。"]
    assert calibrated["risk_flags"] == []
    assert calibrated["summary"] == "公司研究结论。"


def test_unfinished_opencode_search_does_not_impersonate_company_research() -> None:
    claims = [
        {
            "statement": f"2025年年报经营事实 {index}",
            "source_ref": f"https://example.test/report/{index}",
            "support": "supports",
        }
        for index in range(13)
    ]
    source_review = {
        "verdict": "caution",
        "ai_action": "watchlist",
        "buy_attractiveness_score": 60,
        "ai_independent": True,
        "summary": "尚未完成原生搜索。",
        "key_strengths": ["模型优势。"],
        "risk_flags": [],
        "claims": claims,
        "model": "opencode-go/ox-alpha-free",
        "effort": "max",
        "retrieval_backend": "opencode-native-client-websearch",
        "retrieval_model": "opencode-go/ox-alpha-free",
        "retrieval_effort": "max",
        "native_search_completed": False,
        "web_search_performed": True,
        "research_source_urls_verified": True,
        "research_as_of": "2026-08-24",
        "economic_profile": {"business_model": "主营产品销售。"},
        "valuation": {"method": "scenario_multiple"},
        "search_findings": [],
        "evidence_bindings": {},
    }

    calibrated = _review(
        {
            "security_code": "600000",
            "type_key": "type1",
            "ai_review": source_review,
        },
        "2026-08-24",
    )

    assert len(calibrated["claims"]) == 12
    assert calibrated["summary"].startswith("AI买入吸引力 ")


def test_financial_quality_gate_demotes_high_pb_buy() -> None:
    packet = _legacy_packet(
        code="605098",
        score=95,
        pe=20.74,
        pb=6.35,
        facts=[
            "605098 2025年：ROIC 29.39%；自由现金流率 50.7%",
            "605098 2026年中报累计口径：营业收入 4.28 亿元，同比 +24.44%；归母净利润 1.72 亿元，同比 +30.67%",
            "605098 2026年中报累计口径：经营活动现金流净额 2.22 亿元，同比 +226.50%；简化自由现金流 2.22 亿元，同比 +240.89%",
            "交易日估值：PE 20.74 倍；PB 6.35 倍约为同业中位数 3.08 倍的 2.1 倍",
        ],
    )

    review = _review(packet, "2026-08-24")

    assert review["ai_action"] == "watchlist"
    assert review["buy_attractiveness_score"] == 64.0
    assert review["quality_gate"]["hard_block"] is True
    assert any("PB 6.35" in reason for reason in review["quality_gate"]["reasons"])
    assert "当前结论：建议买" not in review["summary"]


@pytest.mark.parametrize(
    ("action", "source", "expected"),
    [
        ("watchlist", "公司独立复核为 recommend_buy。", "观察"),
        ("avoid", "公司独立结论为 observe。", "不建议买"),
        ("avoid", "最终判断为 watchlist。", "不建议买"),
        ("priority_buy", "独立结论为 do_not_recommend_buy。", "建议买"),
    ],
)
def test_action_safe_summary_rewrites_calibrated_english_conclusion(action: str, source: str, expected: str) -> None:
    result = _action_safe_summary(source, action)

    assert expected in result
    assert not any(
        token in result.casefold()
        for token in ("recommend_buy", "do_not_recommend_buy", "priority_buy", "watchlist", "observe")
    )


def test_action_safe_reason_rewrites_conflicting_english_label() -> None:
    result = _action_safe_reason("资产质量判断为 recommend_buy。", "avoid")

    assert "recommend_buy" not in result
    assert "不建议买" in result


def test_priority_buy_gate_summary_does_not_look_like_rule_status() -> None:
    review = _review(
        _legacy_packet(
            code="600919",
            score=95,
            pe=6.86,
            pb=0.82,
            facts=[
                "600919 2026年中报：营业收入 489.52 亿元，同比 +18.11%",
                "600919 2026年中报：归母净利润 218.76 亿元，同比 +18.09%",
                "600919 2026年中报：经营活动现金流净额 100.00 亿元，同比 +18.00%",
            ],
        ),
        "2026-08-27",
    )

    assert "均未形成独立否决" in review["summary"]
    assert "未触发独立否决" not in review["summary"]
    assert not _RELEASE_RULE_REASON_RE.search(review["summary"])


def test_financial_quality_gate_keeps_strong_current_cash_generator() -> None:
    packet = _legacy_packet(
        code="603444",
        score=95,
        pe=15.29,
        pb=4.53,
        facts=[
            "603444 2025年：ROIC 33.76%；自由现金流率 45.0%",
            "603444 2026年中报累计口径：营业收入 37.27 亿元，同比 +48.01%；归母净利润 10.92 亿元，同比 +69.31%",
            "603444 2026年中报累计口径：经营活动现金流净额 12.73 亿元，同比 +18.22%；简化自由现金流 12.68 亿元，同比 +17.91%",
            "交易日估值：PE 15.29 倍；PB 4.53 倍",
        ],
    )

    review = _review(packet, "2026-08-24")

    assert review["ai_action"] == "priority_buy"
    assert review["buy_attractiveness_score"] >= 70
    assert review["quality_gate"]["hard_block"] is False
    assert review["score_components"]["evidence_confidence"] < review["buy_attractiveness_score"]


def test_financial_quality_gate_recognises_h1_and_preserves_fact_bindings() -> None:
    packet = _legacy_packet(
        code="603444",
        score=95,
        pe=15.60,
        pb=4.62,
        facts=[
            "603444 2025年度：ROIC 33.76%；自由现金流率 45.0%",
            "603444 2026H1营业收入 37.27 亿元，同比 +48.01%；归母净利润 10.92 亿元，同比 +69.31%",
            "603444 2026H1经营活动现金流净额 12.73 亿元，同比 +18.22%；简化自由现金流 12.68 亿元，同比 +17.91%",
        ],
    )
    packet["ai_review"]["financial_fact_bindings"] = [
        {
            "metric": "revenue_cny",
            "value": 37.27,
            "unit": "亿元",
            "period": "2026H1",
            "source_url": "https://example.test/603444-report",
        }
    ]
    packet["ai_review"]["numeric_fact_repairs"] = [
        {
            "field": "revenue_cny",
            "old": "372.68亿元",
            "new": "37.27亿元",
            "source_url": "https://example.test/603444-report",
        }
    ]

    review = _review(packet, "2026-08-27")

    assert review["quality_gate"]["metrics"]["interim_profit_growth"] == 69.31
    assert review["quality_gate"]["metrics"]["interim_ocf_growth"] == 18.22
    assert review["financial_fact_bindings"] == packet["ai_review"]["financial_fact_bindings"]
    assert review["numeric_fact_repairs"] == packet["ai_review"]["numeric_fact_repairs"]


def test_financial_quality_gate_demotes_cash_flow_reversal() -> None:
    packet = _legacy_packet(
        code="603508",
        score=91.3,
        pe=14.66,
        pb=1.98,
        facts=[
            "603508 2025年：ROIC 12.02%；自由现金流率 43.0%",
            "603508 2026年中报累计口径：营业收入 7.29 亿元，同比 +5.73%；归母净利润 3.18 亿元，同比 +4.65%",
            "603508 2026年中报累计口径：经营活动现金流净额 3.59 亿元，同比 -5.95%；简化自由现金流 3.42 亿元，同比 -8.49%",
        ],
    )

    review = _review(packet, "2026-08-24")

    assert review["ai_action"] == "watchlist"
    assert review["buy_attractiveness_score"] == 64.0
    assert any("现金流同比下滑" in reason for reason in review["quality_gate"]["reasons"])
