from __future__ import annotations

import json
from pathlib import Path

from tools.ai_screening_identity import explicit_company_codes, sanitise_review_identity
from tools.audit_ai_screening_full import audit_artifact


def test_identity_parser_ignores_financial_numbers_and_industry_indexes() -> None:
    text = "2026-08-24 收盘价 12.70 元，现金 171205.8 万元，行业指数 881115；证券代码：002532。"
    assert explicit_company_codes(text) == {"002532"}


def test_identity_sanitiser_removes_appended_company_result() -> None:
    review = {
        "summary": "公司(002532) 2026 年中报。----- 大金重工(002487) 下一条结果。",
        "key_strengths": ["经营现金流为正"],
        "risk_flags": [],
        "claims": [
            {
                "statement": "天山铝业(002532) 经营现金流稳定。----- 大金重工(002487) 2026 年报告。",
                "source_context": "证券代码：002532",
                "source_ref": "https://example.test/002532",
            }
        ],
    }
    clean, stats = sanitise_review_identity(review, "002532")
    assert explicit_company_codes(clean["summary"]) == {"002532"}
    assert explicit_company_codes(clean["claims"][0]["statement"]) == {"002532"}
    assert "大金重工" not in clean["claims"][0]["statement"]
    assert stats["removed_cross_company_text_count"] >= 2


def test_identity_sanitiser_drops_claim_bound_to_another_company_context() -> None:
    clean, stats = sanitise_review_identity(
        {
            "claims": [
                {
                    "statement": "业绩承诺资产存在违约风险。",
                    "source_context": "证券代码：301281 证券简称：科源制药",
                    "source_ref": "https://example.test/300966",
                }
            ]
        },
        "300966",
    )
    assert clean["claims"] == []
    assert stats["removed_cross_company_claim_count"] == 1


def test_full_audit_emits_one_clean_row_per_company(tmp_path: Path) -> None:
    artifact = {
        "snapshot_generation": "0123456789abcdef",
        "market_as_of": "2026-08-24",
        "packets": [
            {
                "security_code": "002532",
                "name": "天山铝业",
                "type_key": "type1",
                "generation": "0123456789abcdef",
                "market_as_of": "2026-08-24",
                "company_context": {"code": "002532", "name": "天山铝业", "pe": 12.1, "pb": 1.83},
                "ai_review": {
                    "ai_action": "avoid",
                    "final_category": "do_not_recommend",
                    "buy_attractiveness_score": 40.0,
                    "verdict": "caution",
                    "summary": "公司估值与现金流风险需要回避。",
                    "key_strengths": [],
                    "risk_flags": ["现金流风险"],
                    "calibration_adjustments": {"final_score": 40.0},
                    "quality_gate": {"hard_block": True, "metrics": {"pe": 12.1, "pb": 1.83}},
                    "freshness_status": "current_or_recent",
                    "claims": [],
                },
            }
        ],
    }
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    result = audit_artifact(
        path, expected_generation="0123456789abcdef", expected_market_as_of="2026-08-24", expected_count=1
    )
    assert result["review_count"] == 1
    assert result["issue_count"] == 0
    assert len(result["rows"]) == 1


def test_full_audit_rejects_incompatible_financial_fact_unit(tmp_path: Path) -> None:
    artifact = {
        "snapshot_generation": "0123456789abcdef",
        "market_as_of": "2026-08-24",
        "packets": [
            {
                "security_code": "002532",
                "name": "天山铝业",
                "type_key": "type1",
                "generation": "0123456789abcdef",
                "market_as_of": "2026-08-24",
                "company_context": {"code": "002532", "name": "天山铝业"},
                "ai_review": {
                    "ai_action": "avoid",
                    "final_category": "do_not_recommend",
                    "buy_attractiveness_score": 40.0,
                    "verdict": "caution",
                    "summary": "公司估值与现金流风险需要回避。",
                    "key_strengths": [],
                    "risk_flags": ["现金流风险"],
                    "calibration_adjustments": {"final_score": 40.0},
                    "quality_gate": {"hard_block": True},
                    "freshness_status": "current_or_recent",
                    "claims": [],
                    "financial_fact_bindings": [
                        {
                            "metric": "operating_cash_flow_cny",
                            "value": 12.0,
                            "unit": "倍",
                            "period": "2026H1",
                            "source_url": "https://example.test/fact",
                        }
                    ],
                },
            }
        ],
    }
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    result = audit_artifact(path, expected_generation="0123456789abcdef", expected_market_as_of="2026-08-24")
    assert result["issue_count"] == 1
    assert result["financial_fact_unit_mismatch_count"] == 1
    assert "financial_fact_unit_mismatch" in result["rows"][0]["errors"]
