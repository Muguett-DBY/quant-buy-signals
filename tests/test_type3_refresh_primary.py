from __future__ import annotations

import engine.buy_screener as bs


def _primary_attachment(key: str, score: float) -> tuple[dict, dict, dict]:
    evidence = {
        "source": "知识库定性研究",
        "evidence_id": f"primary:{key}:000006:20251231:sha256:" + "0" * 64,
        "as_of": "2025-12-31",
        "summary": "知识库锚点评分",
    }
    record = {
        "score": score,
        "evidence_level": "primary",
        "evidence": evidence,
        "details": {
            "basis": "dated_primary_source_score",
            "source_summary": "知识库锚点评分",
            "source_evidence_id": evidence["evidence_id"],
            "evidence_quality": {
                "level": "primary",
                "input_coverage": 1.0,
                "required_inputs": ["primary_source_score"],
                "available_inputs": ["primary_source_score"],
                "missing_inputs": [],
            },
        },
    }
    return evidence, record


def _refreshed_payload(level: str, score: float) -> dict:
    return {
        "score": score,
        "evidence_level": level,
        "evidence": {"source": "s", "evidence_id": "i", "as_of": "2025-12-31", "summary": "x"},
        "details": {},
    }


def test_refresh_keeps_validated_primary_attachment(monkeypatch) -> None:
    evidence, record = _primary_attachment("growth_quality_score", 6.5)
    metric = {
        "code": "000006",
        "industry": "REALESTATE",
        "source_trade_date": "2026-09-04",
        "revenue_years": [2022, 2023, 2024, 2025],
        "growth_quality_score": 6.5,
        "growth_quality_score_evidence": evidence,
        "growth_quality_score_evidence_level": "primary",
        "quantitative_evidence": {"growth_quality_score": record},
        "quantitative_evidence_levels": {"growth_quality_score": "primary"},
        "quantitative_evidence_status": "partial",
    }

    monkeypatch.setattr(
        bs,
        "derive_company_evidence",
        lambda metric, context, *, fallback_industry_growth=None: {
            "growth_quality_score": _refreshed_payload("partial", 3.0),
            "growth_sustainability_score": _refreshed_payload("derived_proxy", 4.0),
        },
    )

    bs._refresh_type3_quantitative_evidence(metric, {}, {})

    assert metric["growth_quality_score"] == 6.5
    assert metric["growth_quality_score_evidence_level"] == "primary"
    assert metric["quantitative_evidence_levels"]["growth_quality_score"] == "primary"
    assert metric["quantitative_evidence"]["growth_quality_score"] == record


def test_refresh_fills_non_primary_keys(monkeypatch) -> None:
    metric = {
        "code": "000006",
        "industry": "REALESTATE",
        "source_trade_date": "2026-09-04",
        "revenue_years": [2022, 2023, 2024, 2025],
        "quantitative_evidence": {},
        "quantitative_evidence_levels": {},
        "quantitative_evidence_status": "missing",
    }

    monkeypatch.setattr(
        bs,
        "derive_company_evidence",
        lambda metric, context, *, fallback_industry_growth=None: {
            "growth_quality_score": _refreshed_payload("derived_proxy", 3.0),
            "growth_sustainability_score": _refreshed_payload("derived_proxy", 4.0),
        },
    )

    bs._refresh_type3_quantitative_evidence(metric, {}, {})

    assert metric["growth_quality_score"] == 3.0
    assert metric["growth_quality_score_evidence_level"] == "derived_proxy"
    assert metric["growth_sustainability_score"] == 4.0
    assert metric["quantitative_evidence_levels"]["growth_sustainability_score"] == "derived_proxy"
