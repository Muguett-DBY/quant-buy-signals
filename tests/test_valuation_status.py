from __future__ import annotations

import pandas as pd
import pytest

import data.industry as industry
import engine.buy_screener as buy_screener
from engine.pipeline import compute_dcf_batch, run_market_analysis
from engine.valuation_status import (
    DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
    DCF_SKIP_INCONSISTENT_SOURCE,
    DCF_SKIP_INTERNAL_ERROR,
    DCF_SKIP_MODEL_UNSUPPORTED,
    DCF_SKIP_SOURCE_MISSING,
    make_dcf_skip_classification,
    normalize_dcf_skip_classification,
)


def _quote() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "000002",
                "name": "测试公司",
                "market": "SZ",
                "price": 5.0,
                "market_cap": 500.0,
            }
        ]
    )


def test_dcf_skip_classification_is_strict_and_json_safe():
    classification = make_dcf_skip_classification(
        DCF_SKIP_SOURCE_MISSING,
        "missing_complete_annual_fcff_history",
    )

    assert classification == {
        "category": "source_missing",
        "reason": "missing_complete_annual_fcff_history",
    }
    assert normalize_dcf_skip_classification(classification) == classification
    assert normalize_dcf_skip_classification({"category": "invented", "reason": "x"}) is None
    assert normalize_dcf_skip_classification({"category": "source_missing"}) is None
    with pytest.raises(ValueError, match="unknown DCF skip category"):
        make_dcf_skip_classification("invented", "x")


@pytest.mark.parametrize(
    "company,expected_reason,expected_category",
    [
        ({}, "missing_positive_annual_revenue", DCF_SKIP_SOURCE_MISSING),
        (
            {
                "revenue_history": [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 100.0}],
                "cashflow": [
                    {
                        "REPORT_DATE": "2025-12-31",
                        "NETCASH_OPERATE": 1.0,
                        "CONSTRUCT_LONG_ASSET": 2.0,
                    }
                ],
            },
            "nonpositive_normalised_fcff",
            DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
        ),
    ],
)
def test_batch_distinguishes_missing_source_from_nonapplicable_economics(
    monkeypatch,
    company,
    expected_reason,
    expected_category,
):
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "SOFTWARE")

    outcome = compute_dcf_batch(
        _quote(),
        {"000002": company},
        dcf_runner=lambda **_: None,
        max_workers=1,
    )

    assert outcome.skip_reasons == {"000002": expected_reason}
    assert outcome.skip_classifications == {"000002": {"category": expected_category, "reason": expected_reason}}


def test_batch_classifies_unsupported_models_source_inconsistency_and_internal_errors(monkeypatch):
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "FINANCIAL_OTHER")
    unsupported = compute_dcf_batch(
        _quote(),
        {"000002": {}},
        dcf_runner=lambda **_: None,
        max_workers=1,
    )
    assert unsupported.skip_classifications["000002"]["category"] == DCF_SKIP_MODEL_UNSUPPORTED

    inconsistent = compute_dcf_batch(
        _quote(),
        {"000002": {}},
        dcf_runner=lambda **_: {},
        max_workers=1,
    )
    assert inconsistent.skip_classifications["000002"] == {
        "category": DCF_SKIP_INCONSISTENT_SOURCE,
        "reason": "valuation_evidence_invalid",
    }

    def explode(**_):
        raise RuntimeError("fixture")

    internal = compute_dcf_batch(
        _quote(),
        {"000002": {}},
        dcf_runner=explode,
        max_workers=1,
    )
    assert internal.skip_classifications["000002"] == {
        "category": DCF_SKIP_INTERNAL_ERROR,
        "reason": "valuation_exception:RuntimeError",
    }


@pytest.mark.parametrize(
    "category,expected_status,expected_applicable,expected_evidence",
    [
        (DCF_SKIP_SOURCE_MISSING, buy_screener.STATUS_INSUFFICIENT_EVIDENCE, "yes", "incomplete"),
        (
            DCF_SKIP_INCONSISTENT_SOURCE,
            buy_screener.STATUS_INSUFFICIENT_EVIDENCE,
            "yes",
            "incomplete",
        ),
        (DCF_SKIP_MODEL_UNSUPPORTED, buy_screener.STATUS_NOT_APPLICABLE, "no", "complete"),
        (
            DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
            buy_screener.STATUS_NOT_APPLICABLE,
            "no",
            "complete",
        ),
        (DCF_SKIP_INTERNAL_ERROR, buy_screener.STATUS_BLOCKED, "yes", "incomplete"),
    ],
)
def test_type1_and_type4_preserve_structural_valuation_skip_semantics(
    category,
    expected_status,
    expected_applicable,
    expected_evidence,
):
    classification = make_dcf_skip_classification(category, "fixture_reason")
    metric = {"industry": "SOFTWARE"}

    outcomes = (
        buy_screener.score_type1_dcf(metric, None, {}, classification),
        buy_screener.score_type4_long_runway(metric, {}, None, classification),
    )

    for triggered, total, scores, reasons in outcomes:
        assert triggered is False
        assert total == 0.0
        assert set(scores.values()) == {0.0}
        assert reasons["_status"] == expected_status
        assert reasons["_applicable"] == expected_applicable
        assert reasons["_evidence"] == expected_evidence
        assert "_veto" not in reasons
        if category in {DCF_SKIP_MODEL_UNSUPPORTED, DCF_SKIP_ECONOMIC_NOT_APPLICABLE}:
            assert "_missing" not in reasons
            assert "不适用" in reasons["_scope"] or "不支持" in reasons["_scope"]


def test_market_analysis_forwards_structured_skips_to_default_scorer(monkeypatch):
    captured = {}
    monkeypatch.setattr(industry, "classify_industry", lambda *_: "SOFTWARE")

    def capture_screen(
        financials,
        quotes,
        *,
        dcf_results,
        progress_cb,
        market_coldness_evidence,
        dcf_skip_classifications,
        quality_history_evidence,
        quality_history_loader,
        quality_history_progress_cb,
        type3_growth_evidence,
        type3_growth_loader,
        type3_growth_progress_cb,
        research_report_evidence,
        research_report_loader,
        research_report_progress_cb,
        patch4_evidence,
        patch4_loader,
        patch4_progress_cb,
    ):
        captured["dcf_results"] = dcf_results
        captured["dcf_skip_classifications"] = dcf_skip_classifications
        captured["quality_history_evidence"] = quality_history_evidence
        captured["quality_history_loader"] = quality_history_loader
        captured["quality_history_progress_cb"] = quality_history_progress_cb
        captured["type3_growth_evidence"] = type3_growth_evidence
        captured["type3_growth_loader"] = type3_growth_loader
        captured["type3_growth_progress_cb"] = type3_growth_progress_cb
        captured["research_report_evidence"] = research_report_evidence
        captured["research_report_loader"] = research_report_loader
        captured["research_report_progress_cb"] = research_report_progress_cb
        captured["patch4_evidence"] = patch4_evidence
        captured["patch4_loader"] = patch4_loader
        captured["patch4_progress_cb"] = patch4_progress_cb
        return pd.DataFrame([{"code": code} for code in financials])

    monkeypatch.setattr(buy_screener, "screen_all_types", capture_screen)
    outcome = run_market_analysis(
        _quote(),
        {"000002": {}},
        dcf_runner=lambda **_: None,
        max_workers=1,
    )

    expected = {
        "000002": {
            "category": DCF_SKIP_SOURCE_MISSING,
            "reason": "missing_positive_annual_revenue",
        }
    }
    assert outcome.dcf_skip_classifications == expected
    assert captured["dcf_skip_classifications"] == expected
    assert captured["dcf_results"] == {}
    assert captured["type3_growth_evidence"] is None
    assert captured["type3_growth_loader"] is None
    assert captured["research_report_evidence"] is None
    assert captured["research_report_loader"] is None
    assert captured["patch4_evidence"] is None
    assert captured["patch4_loader"] is None


def test_screen_rejects_malformed_or_conflicting_skip_classifications():
    with pytest.raises(ValueError, match="DCF跳过分类无效"):
        buy_screener.screen_all_types(
            {"000002": {}},
            _quote(),
            dcf_skip_classifications={"000002": {"category": "invented", "reason": "x"}},
        )

    with pytest.raises(ValueError, match="DCF结果与跳过分类冲突"):
        buy_screener.screen_all_types(
            {"000002": {}},
            _quote(),
            dcf_results={"000002": {}},
            dcf_skip_classifications={"000002": make_dcf_skip_classification(DCF_SKIP_SOURCE_MISSING, "fixture")},
        )
