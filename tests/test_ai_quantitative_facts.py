from __future__ import annotations

from tools.ai_quantitative_facts import has_numeric_fact, quantitative_facts
from tools.build_ai_screening import _compact_company
from tools.enrich_ai_screening_quantitative_facts import _packet_facts, enrich


def _tcl_company() -> dict:
    return {
        "code": "002668",
        "name": "TCL智家",
        "price": 9.25,
        "pe": 8.894,
        "pb": 2.62,
        "market_cap": 10028030709,
        "annual_history": [{"end_year": 2025}],
        "types": {
            "type1": {
                "score": 8.2,
                "status": "triggered",
                "reasons": {
                    "1a": "买入区内折价24%",
                    "1c": "自由现金流收益率17.5%;最新19.26亿",
                },
            },
            "type2": {
                "score": 7.1,
                "status": "triggered",
                "reasons": {"2a": "总营收加权增速4.5%，HHI=0.14"},
            },
        },
    }


def test_quantitative_facts_keep_period_and_units() -> None:
    facts = quantitative_facts(_tcl_company(), "type1", market_as_of="2026-08-21")
    assert facts[0] == "估值快照：股价 9.25 元；PE 8.89 倍；PB 2.62 倍；市值 100.28 亿元（交易日 2026-08-21）"
    assert "年度财务历史覆盖至 2025 年；年度数字不等同于预测" in facts
    assert all(not any(term in value.lower() for term in ("type", "确定性", "触发", "规则分数")) for value in facts)
    assert all(has_numeric_fact(value) for value in facts)


def test_packet_facts_are_company_level_and_independent_of_candidate_type() -> None:
    facts = _packet_facts(
        _tcl_company(),
        {"type_key": "type1", "type_keys": ["type1", "type2"]},
        "2026-08-21",
    )
    assert facts == quantitative_facts(_tcl_company(), "type7", market_as_of="2026-08-21")
    assert all("type" not in value.lower() and "确定性" not in value and "触发" not in value for value in facts)


def test_model_packet_keeps_neutral_company_facts() -> None:
    company = _tcl_company()
    company["buy_types"] = ["type1", "type2"]
    compact = _compact_company(company, "type1", market_as_of="2026-08-21")
    assert compact["quantitative_facts"] == quantitative_facts(company, "type1", market_as_of="2026-08-21")


def test_enrich_keeps_quantitative_facts_out_of_ai_strengths() -> None:
    payload = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "packets": [
            {
                "security_code": "002668",
                "type_key": "type1",
                "type_keys": ["type1", "type2"],
                "ai_review": {
                    "ai_action": "priority_buy",
                    "key_strengths": ["模型确认主营业务稳定"],
                    "quantitative_facts": ["确定性 type1：8.2 分，状态 triggered"],
                },
            }
        ],
    }
    context = {"generation": "g1", "market_as_of": "2026-08-21", "companies": {"002668": {"company": _tcl_company()}}}

    enriched, count, priority_with_two = enrich(payload, context)
    review = enriched["packets"][0]["ai_review"]

    assert count == 1
    assert priority_with_two == 1
    assert review["key_strengths"] == ["模型确认主营业务稳定"]
    assert review["quantitative_facts"] == quantitative_facts(_tcl_company(), "", market_as_of="2026-08-21")
    assert all(
        "确定性" not in value and "触发" not in value and "type" not in value.lower()
        for value in review["quantitative_facts"]
    )
