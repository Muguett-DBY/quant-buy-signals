from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from engine.audit import _audit_type7_ledger
from engine.buy_screener import (
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_NOT_TRIGGERED,
    score_type7_quality_equity,
)
from engine.quality_equity import (
    RESEARCH_MAX_AGE_DAYS,
    RESEARCH_RECENT_AGE_DAYS,
    TYPE7_DIRECT_SCORE_KEYS,
    QualityEquityError,
    assess_quality_equity,
    decisive_score_upper_bounds,
    normalise_research_sources,
    research_metadata_precheck,
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
            "security_code": "600519",
            "company_name": "贵州茅台",
            "title": f"industry report {index}",
            "publisher": f"publisher {index}",
            "publisher_id": f"publisher-id-{index}",
            "url": f"https://example{index}.test/report",
            "as_of": "2026-07-16",
            "evidence_id": f"report-{index}",
        }
        for index in range(1, 4)
    ]


def _research_content_verification():
    sources = _research_sources()
    body_ids = sorted(source["evidence_id"] for source in sources)
    bodies = []
    for index, evidence_id in enumerate(body_ids, start=1):
        bodies.append(
            {
                "evidence_id": evidence_id,
                "content_sha256": f"{index:064x}",
                "content_length": 500 + index,
                "paragraph_count": 3,
                "structure_signals": ["analysis", "risk"],
                "fact_count": 2,
                "facts": [
                    {
                        "fact_key": "2026Q1:eps",
                        "period": "2026Q1",
                        "metric": "eps",
                        "unit": "CNY_PER_SHARE",
                        "value": (1.0, 3.0, 5.0)[index - 1],
                    },
                    {
                        "fact_key": "2026Q1:revenue",
                        "period": "2026Q1",
                        "metric": "revenue",
                        "unit": "CNY_100M",
                        "value": (35.50, 35.54, 50.0)[index - 1],
                    },
                ],
                "identity_checks": {
                    "code_in_body": True,
                    "name_in_body": True,
                    "detail_code": True,
                    "detail_name": True,
                    "detail_title": True,
                    "detail_publisher": True,
                    "detail_date": True,
                    "dom_json_body": True,
                },
            }
        )
    return {
        "model_id": "type7-report-body-crosscheck-v2",
        "code": "600519",
        "as_of": "2026-07-17",
        "passed": True,
        "required_bodies": 3,
        "attempted_bodies": 3,
        "verified_bodies": 3,
        "distinct_publishers": 3,
        "bodies": bodies,
        "cross_check": {
            "passed": True,
            "minimum_reports": 2,
            "fact_key": "2026Q1:revenue",
            "fact_unit": "CNY_100M",
            "consensus_value": 35.52,
            "supporting_evidence_ids": body_ids[:2],
            "max_relative_spread": 0.00112613,
        },
        "reason": "",
    }


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
    metric.update(_evidence("technology_score", score=6.0))
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


def test_type7_preserves_three_scores_but_metadata_does_not_impersonate_body_review():
    outcome, ledger = score_type7_quality_equity(_metric(), _type1(), _history())
    triggered, total, scores, reasons = outcome

    assert not triggered
    assert reasons["_status"] == STATUS_INSUFFICIENT_EVIDENCE
    assert all(scores[key] > 7.0 for key in ("7a", "7b", "7c"))
    assert total >= 7.0
    assert not ledger["triggered"]
    assert ledger["all_scores_strictly_above_70"]
    assert ledger["prerequisites"]["three_external_reports"]["passed"]
    assert not ledger["prerequisites"]["external_report_content_verification"]["passed"]
    assert (
        ledger["prerequisites"]["external_report_content_verification"]["reason"]
        == "report_body_verification_not_provided"
    )
    assert ledger["source_rule"] == "Template1>70 AND Template5>70 AND Patch5>70"
    assert ledger["research_request_needed"]
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
    assert ledger["research_request_needed"]
    assert not ledger["triggered"]


def test_type7_can_trigger_only_after_three_bodies_and_two_report_fact_consensus():
    metric = _metric()
    metric["type7_research_content_verification"] = _research_content_verification()

    outcome, ledger = score_type7_quality_equity(metric, _type1(), _history())

    assert outcome[0]
    assert ledger["triggered"]
    assert ledger["prerequisites_complete"]
    assert ledger["prerequisites"]["external_report_content_verification"]["passed"]
    assert ledger["prerequisites"]["external_report_content_verification"]["cross_check"] == {
        "passed": True,
        "minimum_reports": 2,
        "fact_key": "2026Q1:revenue",
        "fact_unit": "CNY_100M",
        "consensus_value": 35.52,
        "supporting_evidence_ids": ["report-1", "report-2"],
        "max_relative_spread": 0.00112613,
    }
    assert not ledger["research_request_needed"]
    assert validate_quality_equity_ledger(ledger) == []
    assert _audit_type7_ledger("600519", ledger, "triggered") == []
    assert _audit_type7_ledger_valid("600519", ledger, "triggered")


def test_type7_without_history_requests_only_candidates_whose_safe_upper_bound_can_pass():
    ledger = assess_quality_equity(_metric(), _type1(), None)
    assert ledger["history_request_needed"]
    assert not ledger["research_request_needed"]
    assert not ledger["prerequisites"]["ten_year_return_and_five_year_valuation"]["passed"]
    assert all(value > 70 for value in ledger["upper_bounds_without_history"].values())

    weak = _metric()
    weak.update(_evidence("business_model_score", score=0.0))
    weak.update(_evidence("moat_score", score=0.0))
    weak.update(_evidence("moat_durability_score", score=0.0))
    weak_ledger = assess_quality_equity(weak, _type1(score=2.0), None)
    assert weak_ledger["decisively_not_triggered"]
    assert not weak_ledger["history_request_needed"]
    assert not weak_ledger["research_request_needed"]


def test_type7_decisive_upper_bound_turns_known_failure_into_not_triggered():
    weak = _metric()
    for key in (
        "business_model_score",
        "moat_score",
        "moat_durability_score",
        "runway_score",
        "industry_durability_score",
    ):
        weak.update(_evidence(key, score=0.0))

    outcome, ledger = score_type7_quality_equity(weak, _type1(score=2.0), _history())

    assert ledger["decisively_not_triggered"]
    assert any(value <= 70 for value in ledger["decisive_score_upper_bounds"].values())
    assert outcome[3]["_status"] == STATUS_NOT_TRIGGERED
    assert outcome[3]["_evidence"] == "incomplete"
    assert not ledger["research_request_needed"]
    assert validate_quality_equity_ledger(ledger) == []
    assert _audit_type7_ledger("600519", ledger, STATUS_NOT_TRIGGERED) == []
    assert _audit_type7_ledger_valid("600519", ledger, STATUS_NOT_TRIGGERED)

    assert any(
        "status differs from independently replayed ledger" in error
        for error in _audit_type7_ledger("600519", ledger, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", ledger, STATUS_INSUFFICIENT_EVIDENCE)


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

    missing_reports = _metric()
    missing_reports["type7_research_sources"] = []
    research_ledger = assess_quality_equity(missing_reports, _type1(), _history())
    assert research_ledger["research_request_needed"]
    forged_research = deepcopy(research_ledger)
    forged_research["research_request_needed"] = False
    assert "research request decision mismatch" in validate_quality_equity_ledger(forged_research)
    assert any(
        "research request decision mismatch" in error
        for error in _audit_type7_ledger("600519", forged_research, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", forged_research, STATUS_INSUFFICIENT_EVIDENCE)

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
    ledger = assess_quality_equity(metric, _type1(), _history())
    assert ledger["prerequisites"]["technology_patch4"]["applicable"]
    assert not ledger["prerequisites"]["technology_patch4"]["passed"]
    assert ledger["prerequisites"]["technology_patch4"]["score"] is None
    assert ledger["prerequisites"]["technology_patch4"]["validation_status"] == "missing_validated_patch4_assessment"
    assert not ledger["triggered"]


def test_type7_technology_patch4_rejects_naked_score_and_generic_evidence_wrapper():
    metric = _metric()
    metric["rd_intensity"] = 0.08
    metric.update(_evidence("patch4_shareholder_culture_score", score=10.0))
    metric["patch4_assessment"] = {
        "model_id": "invented-patch4",
        "score": 10.0,
        "complete": True,
    }

    ledger = assess_quality_equity(metric, _type1(), _history())

    assert ledger["prerequisites"]["technology_patch4"] == {
        "passed": False,
        "applicable": True,
        "score": None,
        "validation_status": "missing_validated_patch4_assessment",
    }
    assert validate_quality_equity_ledger(ledger) == []


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

    wrong_company = deepcopy(_research_sources())
    wrong_company[0]["security_code"] = "000001"
    with pytest.raises(QualityEquityError, match="does not match"):
        normalise_research_sources(wrong_company, security_code="600519")

    stale = deepcopy(_research_sources())
    stale[0]["as_of"] = "2025-07-16"
    with pytest.raises(QualityEquityError, match="older"):
        normalise_research_sources(stale, today=date(2026, 7, 17))


def test_research_metadata_policy_replays_183_184_365_366_day_boundaries():
    reference = date(2026, 7, 17)

    def sources_with_ages(*ages):
        sources = _research_sources()
        for source, age in zip(sources, ages):
            source["as_of"] = date.fromordinal(reference.toordinal() - age).isoformat()
        return sources

    at_183 = normalise_research_sources(sources_with_ages(183, 365, 365), today=reference)
    assert research_metadata_precheck(at_183, reference=reference) == {
        "passed": True,
        "source_count": 3,
        "distinct_publishers": 3,
        "recent_source_count": 1,
    }

    at_184 = normalise_research_sources(sources_with_ages(184, 365, 365), today=reference)
    assert research_metadata_precheck(at_184, reference=reference)["passed"] is False
    assert research_metadata_precheck(at_184, reference=reference)["recent_source_count"] == 0

    assert RESEARCH_RECENT_AGE_DAYS == 183
    assert RESEARCH_MAX_AGE_DAYS == 365
    with pytest.raises(QualityEquityError, match="older"):
        normalise_research_sources(sources_with_ages(366, 1, 2), today=reference)

    duplicate_publishers = sources_with_ages(1, 2, 3)
    duplicate_publishers[1]["publisher_id"] = duplicate_publishers[0]["publisher_id"]
    assert not research_metadata_precheck(
        normalise_research_sources(duplicate_publishers, today=reference),
        reference=reference,
    )["passed"]


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
    forged["triggered"] = True
    errors = validate_quality_equity_ledger(forged)
    assert "template1 total mismatch" in errors
    assert "trigger decision mismatch" in errors


def test_decisive_upper_bounds_recompute_every_incomplete_item_and_component():
    ledger = assess_quality_equity(_metric(), _type1(), None)
    recomputed = decisive_score_upper_bounds(
        ledger["template1"],
        ledger["template5"],
        ledger["patch5"],
    )

    assert recomputed == ledger["decisive_score_upper_bounds"]
    template1_expected = sum(
        item["points"] if item["complete"] else item["weight"] for item in ledger["template1"]["items"]
    )
    template5_expected = sum(
        item["points"] if item["complete"] else item["weight"] for item in ledger["template5"]["items"]
    )
    patch5_expected = sum(
        component["points"] if component["complete"] else component["max_points"]
        for dimension in ledger["patch5"]["dimensions"]
        for component in dimension["components"]
    )
    assert recomputed == {
        "template1": round(min(100.0, template1_expected), 2),
        "template5": round(min(100.0, template5_expected), 2),
        "patch5": round(min(100.0, patch5_expected), 2),
    }


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


def _change_atomic_fact_key(ledger):
    facts = ledger["prerequisites"]["external_report_content_verification"]["bodies"][0]["facts"]
    fact = next(item for item in facts if item["metric"] == "revenue")
    fact["period"] = "2025Q4"
    fact["fact_key"] = "2025Q4:revenue"
    facts.sort(key=lambda item: (item["fact_key"], item["unit"]))


def _change_atomic_fact_unit(ledger):
    facts = ledger["prerequisites"]["external_report_content_verification"]["bodies"][0]["facts"]
    next(item for item in facts if item["metric"] == "revenue")["unit"] = "CNY_PER_SHARE"


def _change_atomic_fact_value(ledger):
    facts = ledger["prerequisites"]["external_report_content_verification"]["bodies"][0]["facts"]
    next(item for item in facts if item["metric"] == "revenue")["value"] = 40.0


def _reverse_atomic_fact_order(ledger):
    ledger["prerequisites"]["external_report_content_verification"]["bodies"][0]["facts"].reverse()


def _exceed_atomic_fact_limit(ledger):
    body = ledger["prerequisites"]["external_report_content_verification"]["bodies"][0]
    facts = []
    for index in range(33):
        year = 2020 + index // 4
        quarter = index % 4 + 1
        period = f"{year}Q{quarter}"
        facts.append(
            {
                "fact_key": f"{period}:revenue",
                "period": period,
                "metric": "revenue",
                "unit": "CNY_100M",
                "value": float(index + 1),
            }
        )
    facts.sort(key=lambda item: (item["fact_key"], item["unit"]))
    body["facts"] = facts
    body["fact_count"] = len(facts)


@pytest.mark.parametrize(
    "mutation",
    [
        _change_atomic_fact_key,
        _change_atomic_fact_unit,
        _change_atomic_fact_value,
        _reverse_atomic_fact_order,
        _exceed_atomic_fact_limit,
        lambda ledger: ledger["prerequisites"]["external_report_content_verification"]["bodies"][0].__setitem__(
            "fact_count", 999
        ),
        lambda ledger: ledger["prerequisites"]["external_report_content_verification"]["cross_check"].__setitem__(
            "max_relative_spread", 0.0
        ),
    ],
)
def test_type7_three_validators_recompute_cross_check_from_bounded_atomic_facts(mutation):
    metric = _metric()
    metric["type7_research_content_verification"] = _research_content_verification()
    forged = deepcopy(assess_quality_equity(metric, _type1(), _history()))
    mutation(forged)

    assert "external report content prerequisite mismatch" in validate_quality_equity_ledger(forged)
    assert "600519:type7:external report content prerequisite mismatch" in _audit_type7_ledger(
        "600519", forged, "triggered"
    )
    assert not _audit_type7_ledger_valid("600519", forged, "triggered")


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
        (
            lambda ledger: ledger["decisive_score_upper_bounds"].__setitem__("template1", 0),
            "decisive score upper bounds mismatch",
        ),
        (
            lambda ledger: ledger.__setitem__(
                "decisively_not_triggered",
                not ledger["decisively_not_triggered"],
            ),
            "decisive failure decision mismatch",
        ),
        (
            lambda ledger: ledger["prerequisites"]["external_report_content_verification"].__setitem__("passed", True),
            "external report content prerequisite mismatch",
        ),
        (lambda ledger: ledger.__setitem__("unvalidated_core_override", True), "ledger structure invalid"),
    ],
)
def test_type7_ledger_validator_replays_nested_components(mutation, expected_error):
    forged = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    mutation(forged)
    assert any(expected_error in error for error in validate_quality_equity_ledger(forged))


@pytest.mark.parametrize(
    "mutation, production_error, audit_error",
    [
        (
            lambda ledger: ledger["prerequisites"]["core_modules_80pct"].__setitem__(
                "passed",
                not ledger["prerequisites"]["core_modules_80pct"]["passed"],
            ),
            "core coverage prerequisite mismatch",
            "core coverage prerequisite mismatch",
        ),
        (
            lambda ledger: ledger["prerequisites"]["technology_patch4"].__setitem__(
                "validation_status",
                "forged",
            ),
            "technology prerequisite mismatch",
            "technology prerequisite mismatch",
        ),
        (
            lambda ledger: ledger["prerequisites"]["three_year_financials"].__setitem__(
                "consecutive_years",
                0,
            ),
            "financial history prerequisite mismatch",
            "financial history prerequisite mismatch",
        ),
        (
            lambda ledger: ledger["decisive_score_upper_bounds"].__setitem__("template1", 0),
            "decisive score upper bounds mismatch",
            "decisive score upper bounds mismatch",
        ),
        (
            lambda ledger: ledger.__setitem__(
                "decisively_not_triggered",
                not ledger["decisively_not_triggered"],
            ),
            "decisive failure decision mismatch",
            "decisive failure decision mismatch",
        ),
    ],
)
def test_type7_production_audit_and_release_validators_reject_decisive_forgery(
    mutation,
    production_error,
    audit_error,
):
    forged = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    mutation(forged)

    assert any(production_error in error for error in validate_quality_equity_ledger(forged))
    assert any(audit_error in error for error in _audit_type7_ledger("600519", forged, STATUS_INSUFFICIENT_EVIDENCE))
    assert not _audit_type7_ledger_valid("600519", forged, STATUS_INSUFFICIENT_EVIDENCE)


@pytest.mark.parametrize(
    "mutation, production_error, audit_error",
    [
        (
            lambda ledger: ledger["template1"]["items"][0].__setitem__("points", "not-a-number"),
            "template1 item arithmetic invalid",
            "template1 item arithmetic invalid",
        ),
        (
            lambda ledger: ledger["patch5"]["dimensions"][0]["components"][0].__setitem__("points", "not-a-number"),
            "patch5 p5_business component arithmetic invalid",
            "patch5 p5_business component arithmetic invalid",
        ),
        (
            lambda ledger: ledger["prerequisites"].__setitem__("latest_quote_and_valuation", None),
            "prerequisite latest_quote_and_valuation invalid",
            "prerequisite pass flag invalid",
        ),
    ],
)
def test_type7_validators_fail_closed_without_throwing(mutation, production_error, audit_error):
    forged = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    mutation(forged)

    assert any(production_error in error for error in validate_quality_equity_ledger(forged))
    assert any(audit_error in error for error in _audit_type7_ledger("600519", forged, STATUS_INSUFFICIENT_EVIDENCE))
    assert not _audit_type7_ledger_valid("600519", forged, STATUS_INSUFFICIENT_EVIDENCE)


@pytest.mark.parametrize(
    "mutation, production_error, audit_error",
    [
        (
            lambda ledger: ledger["template1"].__setitem__("unexpected", True),
            "template1 structure invalid",
            "template1 structure invalid",
        ),
        (
            lambda ledger: ledger["template1"]["items"][0].__setitem__("unexpected", True),
            "template1 item structure invalid",
            "template1 item structure invalid",
        ),
        (
            lambda ledger: ledger["template1"]["items"][0].__setitem__("label", ""),
            "template1 item arithmetic invalid",
            "template1 item arithmetic invalid",
        ),
        (
            lambda ledger: ledger["template1"]["items"][0].__setitem__("evidence_level", "invented"),
            "template1 item arithmetic invalid",
            "template1 item arithmetic invalid",
        ),
        (
            lambda ledger: ledger["template1"]["items"][0].__setitem__("formula", "forged"),
            "template1 item arithmetic invalid",
            "template1 item arithmetic invalid",
        ),
        (
            lambda ledger: ledger["template1"]["items"][0]["inputs"].__setitem__("runway", 0.0),
            "template1 item input-score mismatch",
            "template1 item input-score mismatch",
        ),
        (
            lambda ledger: ledger["patch5"].__setitem__("unexpected", True),
            "patch5 structure invalid",
            "patch5 structure invalid",
        ),
        (
            lambda ledger: ledger["patch5"]["dimensions"][0].__setitem__("label", ""),
            "patch5 p5_business structure invalid",
            "patch5 dimension structure invalid",
        ),
        (
            lambda ledger: ledger["patch5"]["dimensions"][0]["components"][0].__setitem__("formula", "forged"),
            "patch5 p5_business component arithmetic invalid",
            "patch5 p5_business component arithmetic invalid",
        ),
        (
            lambda ledger: ledger["patch5"]["dimensions"][0]["components"][0].__setitem__(
                "inputs", {"source": "forged"}
            ),
            "patch5 p5_business component arithmetic invalid",
            "patch5 p5_business component arithmetic invalid",
        ),
        (
            lambda ledger: ledger["strict_checks"].__setitem__("template1", 1),
            "strict threshold checks mismatch",
            "strict checks mismatch",
        ),
    ],
)
def test_type7_validators_share_exact_nested_contract(mutation, production_error, audit_error):
    forged = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    mutation(forged)

    assert any(production_error in error for error in validate_quality_equity_ledger(forged))
    assert any(audit_error in error for error in _audit_type7_ledger("600519", forged, STATUS_INSUFFICIENT_EVIDENCE))
    assert not _audit_type7_ledger_valid("600519", forged, STATUS_INSUFFICIENT_EVIDENCE)


def test_type7_technology_prerequisite_is_replayed_identically():
    literal_null = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    literal_null["prerequisites"]["technology_patch4"]["score"] = False

    assert "technology prerequisite mismatch" in validate_quality_equity_ledger(literal_null)
    assert any(
        "technology prerequisite mismatch" in error
        for error in _audit_type7_ledger("600519", literal_null, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", literal_null, STATUS_INSUFFICIENT_EVIDENCE)

    technology_metric = _metric()
    technology_metric.update(_evidence("technology_score", score=9.0))
    forged_scope = assess_quality_equity(technology_metric, _type1(), _history())
    forged_scope["prerequisites"]["technology_patch4"].update(
        {"passed": True, "applicable": False, "score": None, "validation_status": "not_applicable"}
    )

    assert "technology prerequisite mismatch" in validate_quality_equity_ledger(forged_scope)
    assert any(
        "technology prerequisite mismatch" in error
        for error in _audit_type7_ledger("600519", forged_scope, STATUS_INSUFFICIENT_EVIDENCE)
    )
    assert not _audit_type7_ledger_valid("600519", forged_scope, STATUS_INSUFFICIENT_EVIDENCE)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda ledger: ledger.__setitem__("strict_threshold", 70.0000000005),
        lambda ledger: ledger["template1"]["items"][0].__setitem__("weight", 5.0000000005),
        lambda ledger: ledger["patch5"]["dimensions"][0]["components"][0].__setitem__("max_points", 5.0000000005),
        lambda ledger: ledger["scores"].__setitem__("template1", ledger["scores"]["template1"] + 0.00005),
    ],
)
def test_type7_release_numeric_tolerance_matches_production_and_audit(mutation):
    ledger = deepcopy(assess_quality_equity(_metric(), _type1(), _history()))
    mutation(ledger)

    assert validate_quality_equity_ledger(ledger) == []
    assert _audit_type7_ledger("600519", ledger, STATUS_INSUFFICIENT_EVIDENCE) == []
    assert _audit_type7_ledger_valid("600519", ledger, STATUS_INSUFFICIENT_EVIDENCE)


def test_type7_decisive_upper_bound_is_order_stable_at_half_cent_values():
    ledger = assess_quality_equity(_metric(), _type1(), _history())
    item = next(item for item in ledger["template1"]["items"] if item["key"] == "t1_08")
    item.update({"score": 8.99, "points": 4.495})
    expected = decisive_score_upper_bounds(ledger["template1"], ledger["template5"], ledger["patch5"])

    ledger["template1"]["items"].reverse()
    ledger["template5"]["items"].reverse()
    ledger["patch5"]["dimensions"].reverse()
    for dimension in ledger["patch5"]["dimensions"]:
        dimension["components"].reverse()

    assert expected["template1"] == 89.28
    assert decisive_score_upper_bounds(ledger["template1"], ledger["template5"], ledger["patch5"]) == expected
