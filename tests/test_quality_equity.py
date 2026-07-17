from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from engine.audit import _audit_type7_ledger
from engine.buy_screener import STATUS_INSUFFICIENT_EVIDENCE, STATUS_TRIGGERED, score_type7_quality_equity
from engine.quality_equity import (
    TYPE7_DIRECT_SCORE_KEYS,
    QualityEquityError,
    assess_quality_equity,
    normalise_research_sources,
    validate_quality_equity_ledger,
)
from tools.verify_release_zip import _audit_type7_ledger_valid


def _evidence(key, score=9.0):
    return {
        key: score,
        f"{key}_evidence": {
            "source": "audited fixture",
            "evidence_id": f"fixture:{key}",
            "as_of": "2026-07-17",
            "summary": f"{key}={score}",
        },
        f"{key}_evidence_level": "primary",
    }


def _research_sources():
    return [
        {
            "title": f"industry report {index}",
            "publisher": f"publisher {index}",
            "url": f"https://example{index}.test/report",
            "as_of": "2026-07-16",
            "evidence_id": f"report-{index}",
        }
        for index in range(1, 4)
    ]


def _metric():
    metric = {
        "code": "600519",
        "industry": "LIQUOR",
        "source_trade_date": "2026-07-17",
        "trend_growth": 0.16,
        "net_profit_history": [100, 120, 145, 175, 210],
        "net_profit_years": [2021, 2022, 2023, 2024, 2025],
        "fcf_history": [90, 108, 130, 158, 190],
        "fcf_years": [2021, 2022, 2023, 2024, 2025],
        "revenue_years": [2021, 2022, 2023, 2024, 2025],
        "equity_history": [100, 120, 144, 172.8, 207.36],
        "equity_years": [2021, 2022, 2023, 2024, 2025],
        "interest_bearing_debt_ratio": 0.05,
        "roic": 0.30,
        "wacc": 0.09,
        "gross_margin": 0.85,
        "net_margin": 0.45,
        "gross_margin_cv": 0.03,
        "share_dilution_1yr": 0.0,
        "profit_volatility": 0.18,
        "growth_consistency": 0.20,
        "total_assets": 300,
        "revenue_latest": 250,
        "net_profit_latest": 210,
        "market_cap": 3_000,
        "capex": 8,
        "rd_intensity": 0.01,
        "type7_research_sources": _research_sources(),
    }
    for key in (
        "business_model_score",
        "moat_score",
        "moat_durability_score",
        "runway_score",
        "industry_durability_score",
        "accounting_integrity_score",
        "management_alignment_score",
        "technology_score",
        "catalyst_score",
        "growth_sustainability_score",
    ) + TYPE7_DIRECT_SCORE_KEYS:
        metric.update(_evidence(key))
    return metric


def _type1(score=9.0):
    return (
        True,
        score,
        {"1a": score, "1b": score, "1c": score, "1d": score},
        {"_status": "triggered", "_evidence": "complete"},
    )


def _history():
    return {
        "available": True,
        "code": "600519",
        "as_of": "2026-07-17",
        "model_id": "type7-market-history-v1",
        "shareholder_return": {"available": True, "cagr": 0.18, "total_return": 4.2},
        "valuation_history": {
            "available": True,
            "current_pe_ttm": 20.0,
            "median_pe_ttm": 25.0,
            "current_pb_mrq": 6.0,
            "median_pb_mrq": 7.0,
            "pe_percentile": 0.10,
            "pb_percentile": 0.12,
        },
    }


def test_type7_preserves_three_independent_scores_and_triggers_only_the_intersection():
    outcome, ledger = score_type7_quality_equity(_metric(), _type1(), _history())
    triggered, total, scores, reasons = outcome

    assert triggered
    assert reasons["_status"] == STATUS_TRIGGERED
    assert all(scores[key] > 7.0 for key in ("7a", "7b", "7c"))
    assert total >= 7.0
    assert ledger["triggered"]
    assert ledger["source_rule"] == "Template1>70 AND Template5>70 AND Patch5>70"
    assert validate_quality_equity_ledger(ledger) == []
    assert len(ledger["template1"]["items"]) == 20
    assert sum(item["weight"] for item in ledger["template5"]["items"]) == 100
    assert ledger["patch5"]["safety_margin_score"] >= 8
    item19 = next(item for item in ledger["template1"]["items"] if item["key"] == "t1_19")
    projection = item19["inputs"]["terminal_profit_projection"]
    assert projection["projected_net_profit_year_10"] > _metric()["net_profit_latest"]
    assert projection["market_cap_to_year_10_profit"] > 0


def test_type7_missing_three_independent_reports_is_evidence_insufficient_not_a_zero_score():
    metric = _metric()
    metric["type7_research_sources"] = []
    outcome, ledger = score_type7_quality_equity(metric, _type1(), _history())

    assert not outcome[0]
    assert outcome[3]["_status"] == STATUS_INSUFFICIENT_EVIDENCE
    assert outcome[1] > 0
    assert not ledger["prerequisites"]["three_external_reports"]["passed"]
    assert not ledger["history_request_needed"]
    assert not ledger["triggered"]


def test_type7_without_history_requests_only_candidates_whose_safe_upper_bound_can_pass():
    ledger = assess_quality_equity(_metric(), _type1(), None)
    assert ledger["history_request_needed"]
    assert not ledger["prerequisites"]["ten_year_return_and_five_year_valuation"]["passed"]
    assert all(value > 70 for value in ledger["upper_bounds_without_history"].values())

    weak = _metric()
    weak.update(_evidence("business_model_score", score=0.0))
    weak.update(_evidence("moat_score", score=0.0))
    weak.update(_evidence("moat_durability_score", score=0.0))
    weak_ledger = assess_quality_equity(weak, _type1(score=2.0), None)
    assert not weak_ledger["history_request_needed"]


def test_type7_preflight_upper_bound_includes_both_history_based_expected_return_items():
    candidate = _metric()
    candidate.update(_evidence("catalyst_score", score=0.0))
    candidate.update(_evidence("business_model_score", score=0.0))

    ledger = assess_quality_equity(candidate, _type1(), None)

    # Without restoring t1_18 and t5_v3 to their theoretical maxima, the
    # Template 5 upper bound is only 63.77 and this viable candidate is never
    # allowed to fetch its five-year valuation history.
    assert ledger["upper_bounds_without_history"]["template5"] == 70.97
    assert all(value > 70 for value in ledger["upper_bounds_without_history"].values())
    assert ledger["history_request_needed"]
    assert validate_quality_equity_ledger(ledger) == []


def test_type7_independent_audit_and_release_replay_reject_forged_history_preflight():
    ledger = assess_quality_equity(_metric(), _type1(), None)
    assert _audit_type7_ledger("600519", ledger, STATUS_INSUFFICIENT_EVIDENCE) == []
    assert _audit_type7_ledger_valid("600519", ledger, STATUS_INSUFFICIENT_EVIDENCE)

    forged_upper = deepcopy(ledger)
    forged_upper["upper_bounds_without_history"]["template5"] += 0.01
    assert any(
        "history upper bounds mismatch" in error
        for error in _audit_type7_ledger("600519", forged_upper, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", forged_upper, STATUS_INSUFFICIENT_EVIDENCE)

    forged_decision = deepcopy(ledger)
    forged_decision["history_request_needed"] = False
    assert any(
        "history request decision mismatch" in error
        for error in _audit_type7_ledger("600519", forged_decision, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", forged_decision, STATUS_INSUFFICIENT_EVIDENCE)

    forged_report_time = deepcopy(ledger)
    forged_report_time["prerequisites"]["three_external_reports"]["sources"][0]["as_of"] = "2026-07-18"
    assert "external reports prerequisite mismatch" in validate_quality_equity_ledger(forged_report_time)
    assert any(
        "external reports prerequisite mismatch" in error
        for error in _audit_type7_ledger("600519", forged_report_time, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", forged_report_time, STATUS_INSUFFICIENT_EVIDENCE)


def test_type7_technology_company_requires_patch4_culture_evidence():
    metric = _metric()
    metric["rd_intensity"] = 0.08
    metric.pop("patch4_shareholder_culture_score")
    metric.pop("patch4_shareholder_culture_score_evidence")
    metric.pop("patch4_shareholder_culture_score_evidence_level")
    ledger = assess_quality_equity(metric, _type1(), _history())
    assert ledger["prerequisites"]["technology_patch4"]["applicable"]
    assert not ledger["prerequisites"]["technology_patch4"]["passed"]
    assert not ledger["triggered"]


def test_research_source_validation_rejects_duplicate_or_insecure_metadata():
    duplicate = _research_sources()
    duplicate[1]["evidence_id"] = duplicate[0]["evidence_id"]
    with pytest.raises(QualityEquityError, match="duplicate"):
        normalise_research_sources(duplicate)

    insecure = deepcopy(_research_sources())
    insecure[0]["url"] = "http://example.test/report"
    with pytest.raises(QualityEquityError, match="HTTPS"):
        normalise_research_sources(insecure)

    duplicate_url = deepcopy(_research_sources())
    duplicate_url[1]["url"] = duplicate_url[0]["url"]
    with pytest.raises(QualityEquityError, match="duplicate report URLs"):
        normalise_research_sources(duplicate_url)

    with pytest.raises(QualityEquityError, match="future"):
        normalise_research_sources(_research_sources(), today=date(2025, 12, 31))


def test_type7_rejects_evidence_published_after_the_market_snapshot():
    metric = _metric()
    metric["source_trade_date"] = "2025-12-31"
    for source in metric["type7_research_sources"]:
        source["as_of"] = "2025-12-30"

    ledger = assess_quality_equity(metric, _type1(), _history())

    business = next(item for item in ledger["template1"]["items"] if item["key"] == "t1_05")
    assert not business["complete"]
    assert not ledger["prerequisites"]["ten_year_return_and_five_year_valuation"]["passed"]
    assert not ledger["triggered"]


def test_market_history_prerequisite_is_independent_of_terminal_profit_projection():
    metric = _metric()
    metric.pop("net_profit_latest")

    ledger = assess_quality_equity(metric, _type1(), _history())
    item19 = next(item for item in ledger["template1"]["items"] if item["key"] == "t1_19")

    assert not item19["complete"]
    assert ledger["prerequisites"]["ten_year_return_and_five_year_valuation"]["passed"]
    assert validate_quality_equity_ledger(ledger) == []


def test_pb_reversion_uses_book_value_growth_instead_of_earnings_growth():
    metric = _metric()
    metric["equity_history"] = [100, 105, 110.25, 115.7625, 121.550625]
    history = _history()
    history["valuation_history"].update(
        current_pe_ttm=None,
        median_pe_ttm=None,
        pe_percentile=None,
        current_pb_mrq=6.0,
        median_pb_mrq=6.0,
    )

    ledger = assess_quality_equity(metric, _type1(), history)
    expected_return = next(item for item in ledger["template1"]["items"] if item["key"] == "t1_18")
    valuation_input = expected_return["inputs"]["valuation_inputs"]

    assert len(valuation_input) == 1
    assert valuation_input[0]["basis"] == "PB_MRQ"
    assert valuation_input[0]["growth_rate"] == pytest.approx(0.05)
    assert valuation_input[0]["annual_return"] == pytest.approx(0.05)
    assert validate_quality_equity_ledger(ledger) == []

    metric.pop("share_dilution_1yr")
    incomplete = assess_quality_equity(metric, _type1(), history)
    incomplete_return = next(item for item in incomplete["template1"]["items"] if item["key"] == "t1_18")
    assert not incomplete_return["complete"]


def test_type7_ledger_validator_detects_forged_total_and_trigger():
    ledger = assess_quality_equity(_metric(), _type1(), _history())
    forged = deepcopy(ledger)
    forged["template1"]["score"] += 1
    forged["triggered"] = False
    errors = validate_quality_equity_ledger(forged)
    assert "template1 total mismatch" in errors
    assert "trigger decision mismatch" in errors


def test_type7_source_threshold_uses_unrounded_percent_scores(monkeypatch):
    baseline = assess_quality_equity(_metric(), _type1(), _history())
    template1 = deepcopy(baseline["template1"])
    template5 = deepcopy(baseline["template5"])
    patch5 = deepcopy(baseline["patch5"])
    template1["score"] = 70.01
    template5["score"] = 70.01
    patch5["score"] = 70.01
    monkeypatch.setattr("engine.quality_equity._make_template1", lambda _values: template1)
    monkeypatch.setattr("engine.quality_equity._make_template5", lambda _values: template5)
    monkeypatch.setattr("engine.quality_equity._make_patch5", lambda _metric, _values: patch5)

    ledger = assess_quality_equity(_metric(), _type1(), _history())
    assert ledger["scores"] == {"template1": 70.01, "template5": 70.01, "patch5": 70.01}
    assert ledger["strict_checks"] == {"template1": True, "template5": True, "patch5": True}


@pytest.mark.parametrize(
    "mutation, expected_error",
    [
        (
            lambda ledger: ledger["patch5"]["dimensions"][0]["components"][0].__setitem__("points", 0),
            "component arithmetic invalid",
        ),
        (lambda ledger: ledger["template5"].__setitem__("coverage", 0), "coverage mismatch"),
        (
            lambda ledger: ledger["prerequisites"]["three_external_reports"].__setitem__("passed", False),
            "external reports prerequisite mismatch",
        ),
    ],
)
def test_type7_ledger_validator_replays_nested_components(mutation, expected_error):
    forged = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    mutation(forged)
    assert any(expected_error in error for error in validate_quality_equity_ledger(forged))
