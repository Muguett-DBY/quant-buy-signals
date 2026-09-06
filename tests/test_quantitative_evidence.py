from __future__ import annotations

import copy
import json
import math
from datetime import date

import pytest

from data.as_of import shanghai_today
import engine.quantitative_evidence as quantitative_evidence
from engine import buy_screener
from engine.quantitative_evidence import (
    EVIDENCE_LEVELS,
    MIN_COMPARABLE_COVERAGE,
    MIN_SECTOR_COMPANIES,
    MODEL_ID,
    build_company_contexts,
    build_sector_context,
    derive_company_evidence,
    enrich_metrics,
    validate_quantitative_evidence_record,
)


def _metric(code: str, **overrides: object) -> dict[str, object]:
    metric: dict[str, object] = {
        "code": code,
        "industry": "TEST",
        "financial_indicator_as_of": "2025-12-31",
        "revenue_years": [2022, 2023, 2024, 2025],
        "revenue_values": [90.0, 100.0, 110.0, 121.0],
        "revenue_latest": 121.0,
        "capex_years": [2022, 2023, 2024, 2025],
        "capex_history": [9.5, 10.0, 10.5, 11.0],
        "total_assets_years": [2022, 2023, 2024, 2025],
        "total_assets_history": [95.0, 100.0, 105.0, 110.0],
        "net_profit_years": [2022, 2023, 2024, 2025],
        "net_profit_history": [9.0, 10.0, 11.0, 12.0],
        "indicator_roic_years": [2022, 2023, 2024, 2025],
        "indicator_roic_history": [0.15, 0.16, 0.17, 0.18],
        "gross_margin_years": [2022, 2023, 2024, 2025],
        "gross_margin_history": [0.43, 0.44, 0.45, 0.45],
        "margin_history": [0.10, 0.105, 0.108, 0.11],
        "fcf_years": [2022, 2023, 2024, 2025],
        "fcf_history": [7.0, 8.0, 9.0, 10.0],
        "free_cash_flow": 10.0,
        "net_profit": 12.0,
        "gross_margin": 0.45,
        "gross_margin_cv": 0.03,
        "margin_trajectory": 0.02,
        "roic": 0.18,
        "wacc": 0.09,
        "rd_intensity": 0.05,
        "trend_growth": 0.10,
        "growth_slope": 0.01,
        "cagr_3yr": 0.10,
        "ocf_np_ratio": 0.95,
        "adjusted_profit_ratio": 0.96,
        "share_dilution_1yr": 0.0,
        "interest_bearing_debt_ratio": 0.20,
        "interim_revenue_yoy": 0.10,
        "interim_profit_yoy": 0.12,
        "interim_ocf_yoy": 0.15,
        "interim_current_revenue": 35.0,
        "interim_current_profit": 4.0,
        "interim_current_ocf": 4.5,
        "market_coldness_score": 5.0,
        "market_coldness_components": {"raw_values": {"change_60d_pct": 3.0, "change_ytd_pct": 5.0}},
    }
    metric.update(overrides)
    return metric


def _context() -> dict[str, object]:
    peers = [_metric(f"P{index}", rd_intensity=0.01 * (index + 1)) for index in range(MIN_SECTOR_COMPANIES)]
    return build_sector_context(peers)["TEST"]


def _insurance_metric(code: str = "INSURER", **overrides: object) -> dict[str, object]:
    years = list(range(2017, 2026))
    metric = _metric(
        code,
        industry="INSURANCE",
        indicator_roic_years=[],
        indicator_roic_history=[],
        gross_margin_years=[],
        gross_margin_history=[],
        gross_margin=None,
        gross_margin_cv=None,
        fcf_years=[],
        fcf_history=[],
        free_cash_flow=None,
        roic=None,
        wacc=None,
        solvency_adequacy_ratio_years=years,
        solvency_adequacy_ratio_history=[2.40, 2.35, 2.30, 2.25, 2.20, 2.15, 2.10, 2.05, 2.00],
        new_business_value_years=years,
        new_business_value_history=[20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 27.0, 30.0, 33.0],
        earned_premium_years=years,
        earned_premium_history=[100.0, 104.0, 108.0, 112.0, 116.0, 120.0, 125.0, 130.0, 136.0],
        life_surrender_rate_years=years,
        life_surrender_rate_history=[0.025, 0.024, 0.023, 0.022, 0.021, 0.020, 0.019, 0.018, 0.017],
        indicator_weighted_roe_years=years,
        indicator_weighted_roe_history=[0.12, 0.13, 0.12, 0.14, 0.13, 0.14, 0.15, 0.15, 0.16],
        adjusted_profit_ratio_years=years,
        adjusted_profit_ratio_history=[0.96, 0.97, 0.98, 0.98, 0.99, 0.99, 1.00, 0.99, 1.01],
    )
    metric.update(overrides)
    return metric


def test_insurance_moat_uses_sector_evidence_without_industrial_defaults() -> None:
    metric = _insurance_metric()

    _contexts, evidence_by_code = enrich_metrics([metric], {})

    moat = evidence_by_code["INSURER"]["moat_score"]
    durability = evidence_by_code["INSURER"]["moat_durability_score"]
    assert moat["evidence_level"] == "derived_proxy"
    assert moat["details"]["basis"] == "insurance_specific_not_industrial_roic_margin_or_fcff"
    assert moat["details"]["evidence_quality"]["missing_inputs"] == []
    assert set(moat["details"]["components"]) == {
        "solvency_resilience",
        "new_business_value_growth",
        "earned_premium_growth",
        "policyholder_retention",
        "weighted_roe",
        "adjusted_profit_quality",
    }
    assert durability["evidence_level"] == "derived_proxy"
    assert durability["details"]["basis"] == "insurance_specific_not_industrial_roic_margin_or_fcff"
    assert durability["details"]["durability_history_years"] == 9
    assert durability["details"]["history_cap"] == 6.0
    assert metric["moat_score"] == moat["score"]
    assert metric["moat_durability_score"] == durability["score"]


@pytest.mark.parametrize("industry", sorted(quantitative_evidence.FINANCIAL_INDUSTRIES))
def test_financial_industrial_governance_proxies_are_explicitly_not_applicable(industry: str) -> None:
    metric = _metric("FIN-N-A", industry=industry)

    _contexts, evidence_by_code = enrich_metrics([metric], {})

    for key in quantitative_evidence.FINANCIAL_INDUSTRIAL_NOT_APPLICABLE_KEYS:
        record = evidence_by_code["FIN-N-A"][key]
        assert record["score"] is None
        assert record["evidence_level"] == "not_applicable"
        assert record["details"]["applicability"] == {
            "applicable": False,
            "rule": "financial-sector-specific-evidence-v1",
        }
        assert record["details"]["evidence_quality"]["missing_inputs"] == []
        assert key not in metric
        assert f"{key}_evidence" not in metric
        assert metric[f"{key}_evidence_level"] == "not_applicable"
        assert (
            validate_quantitative_evidence_record(
                record,
                key=key,
                code="FIN-N-A",
                industry=industry,
            )["score"]
            is None
        )
        with pytest.raises(ValueError, match="applicability binding"):
            validate_quantitative_evidence_record(
                record,
                key=key,
                code="FIN-N-A",
                industry="INDUSTRIAL",
            )
        with pytest.raises(ValueError, match="applicability binding"):
            validate_quantitative_evidence_record(
                record,
                key=key,
                code="OTHER-COMPANY",
                industry=industry,
            )


def test_single_undated_fcf_cannot_complete_accounting_management_or_moat() -> None:
    metric = _metric("TARGET", fcf_history=[10.0], fcf_years=[])

    evidence = derive_company_evidence(metric, _context())

    for key in ("accounting_integrity_score", "management_alignment_score", "moat_score"):
        assert evidence[key]["evidence_level"] != "derived_proxy"
        assert "free_cash_flow_history" in evidence[key]["details"]["evidence_quality"]["missing_inputs"]


def test_insurance_moat_fails_closed_when_one_sector_history_is_missing() -> None:
    metric = _insurance_metric(
        "INSURANCE-MISSING",
        life_surrender_rate_years=[],
        life_surrender_rate_history=[],
    )

    _contexts, evidence_by_code = enrich_metrics([metric], {})

    moat = evidence_by_code["INSURANCE-MISSING"]["moat_score"]
    durability = evidence_by_code["INSURANCE-MISSING"]["moat_durability_score"]
    assert moat["evidence_level"] == "partial"
    assert moat["details"]["evidence_quality"]["missing_inputs"] == ["life_surrender_rate_history"]
    assert durability["evidence_level"] == "partial"
    assert "moat_score" not in metric
    assert "moat_durability_score" not in metric


def test_sector_aggregate_requires_minimum_sample_and_exact_coverage_threshold() -> None:
    multiplier = math.ceil(MIN_SECTOR_COMPANIES / 7)
    cohort_count = 7 * multiplier
    population_count = 10 * multiplier
    complete = [_metric(f"C{index}") for index in range(cohort_count)]
    incomplete = [
        _metric(
            f"M{index}",
            revenue_years=[2024, 2025],
            revenue_values=[110.0, 121.0],
        )
        for index in range(population_count - cohort_count)
    ]

    exact_boundary = build_sector_context([*complete, *incomplete])["TEST"]["revenue"]

    assert MIN_COMPARABLE_COVERAGE == pytest.approx(0.70)
    assert exact_boundary["available"] is True
    assert exact_boundary["cohort_count"] == cohort_count
    assert exact_boundary["population_count"] == population_count
    assert exact_boundary["coverage"] == pytest.approx(MIN_COMPARABLE_COVERAGE)
    assert exact_boundary["cohort_codes"] == [f"C{index}" for index in range(cohort_count)]

    too_few = build_sector_context([_metric(f"S{index}") for index in range(MIN_SECTOR_COMPANIES - 1)])["TEST"][
        "revenue"
    ]
    assert too_few["available"] is False
    assert too_few["reason"] == "insufficient_comparable_cohort"
    assert too_few["coverage"] == pytest.approx(1.0)

    below_boundary = build_sector_context(
        [
            *complete,
            *incomplete,
            _metric("M-BELOW", revenue_years=[2024, 2025], revenue_values=[110.0, 121.0]),
        ]
    )["TEST"]["revenue"]

    assert below_boundary["available"] is False
    assert below_boundary["reason"] == "insufficient_comparable_cohort"
    assert below_boundary["cohort_count"] == cohort_count
    assert below_boundary["population_count"] == population_count + 1
    assert below_boundary["coverage"] == pytest.approx(cohort_count / (population_count + 1))


def test_company_context_rejects_peer_growth_from_an_old_reporting_window() -> None:
    trade_date = shanghai_today()
    expected_latest_year = trade_date.year - 1
    stale_peers = [
        _metric(
            f"STALE-{index}",
            financial_indicator_as_of="2010-12-31",
            source_trade_date=trade_date.isoformat(),
            revenue_years=[2008, 2009, 2010],
            revenue_values=[100.0, 150.0, 225.0],
            revenue_latest=225.0,
        )
        for index in range(MIN_SECTOR_COMPANIES)
    ]
    target = _metric(
        "CURRENT-TARGET",
        financial_indicator_as_of=f"{expected_latest_year}-12-31",
        source_trade_date=trade_date.isoformat(),
        revenue_years=[],
        revenue_values=[],
        revenue_latest=None,
    )

    context = build_company_contexts(
        [*stale_peers, target],
        target_codes={"CURRENT-TARGET"},
    )["CURRENT-TARGET"]
    revenue = context["revenue"]

    assert context["latest_complete_financial_year"] == expected_latest_year
    assert revenue["expected_latest_year"] == expected_latest_year
    assert revenue["years"] == [
        expected_latest_year - 2,
        expected_latest_year - 1,
        expected_latest_year,
    ]
    assert revenue["reporting_period_eligible_count"] == 0
    assert revenue["cohort_count"] == 0
    assert revenue["available"] is False
    assert revenue["reason"] == "stale_or_missing_reporting_period"
    assert context["aggregate_revenue_cagr"] is None
    assert context["aggregate_revenue_cagr_count"] == 0


def test_enrichment_uses_leave_one_out_peers_for_each_target() -> None:
    peers = [
        _metric(
            f"P{index}",
            revenue_values=[100.0, 100.0, 100.0, 100.0],
            revenue_latest=100.0,
            cagr_3yr=0.0,
            trend_growth=0.0,
        )
        for index in range(MIN_SECTOR_COMPANIES)
    ]
    target = _metric(
        "TARGET",
        revenue_values=[1.0, 2.0, 10.0, 10_000.0],
        revenue_latest=10_000.0,
        cagr_3yr=99.0,
        trend_growth=99.0,
    )
    peer_context = build_sector_context(peers)["TEST"]
    inclusive_context = build_sector_context([*peers, target])["TEST"]
    expected = derive_company_evidence(target, peer_context)["industry_durability_score"]["score"]
    self_contaminated = derive_company_evidence(target, inclusive_context)["industry_durability_score"]["score"]
    assert expected != self_contaminated, "fixture must detect target leakage into its own peer aggregate"

    working_metrics = copy.deepcopy([*peers, target])
    contexts, evidence_by_code = enrich_metrics(working_metrics, {})

    actual = evidence_by_code["TARGET"]["industry_durability_score"]["score"]
    assert actual == expected
    assert actual != self_contaminated
    assert contexts["TARGET"]["target_code"] == "TARGET"
    assert contexts["TARGET"]["target_excluded"] is True
    assert contexts["TARGET"]["peer_count"] == MIN_SECTOR_COMPANIES
    assert "TARGET" not in contexts["TARGET"]["revenue"]["cohort_codes"]


def test_enrichment_uses_leave_one_out_fallback_benchmark_for_each_target(monkeypatch) -> None:
    metrics = [
        _metric("A", cagr_3yr=0.10),
        _metric("B", cagr_3yr=0.20),
        _metric("C", cagr_3yr=1.00),
    ]
    benchmarks = buy_screener.build_sector_benchmarks(metrics)
    observed: dict[str, float | None] = {}

    def capture(metric, _context, *, fallback_industry_growth):
        observed[str(metric["code"])] = fallback_industry_growth
        return {}

    monkeypatch.setattr(quantitative_evidence, "derive_company_evidence", capture)

    enrich_metrics(metrics, benchmarks)

    assert observed["C"] == pytest.approx(0.15)
    assert observed["A"] == pytest.approx(0.60)


def test_projected_enrichment_uses_full_peer_base_but_only_materializes_requested_targets() -> None:
    peers = [_metric(f"P{index:02d}") for index in range(MIN_SECTOR_COMPANIES)]
    target = _metric("TARGET", revenue_values=[1.0, 2.0, 10.0, 10_000.0], revenue_latest=10_000.0)
    full_metrics = copy.deepcopy([*peers, target])
    projected_metrics = copy.deepcopy([*peers, target])

    full_contexts, full_evidence = enrich_metrics(full_metrics, {})
    projected_contexts, projected_evidence = enrich_metrics(
        projected_metrics,
        {},
        target_codes={"TARGET"},
    )

    assert set(projected_contexts) == {"TARGET"}
    assert set(projected_evidence) == {"TARGET"}
    assert projected_contexts["TARGET"] == full_contexts["TARGET"]
    assert projected_evidence["TARGET"] == full_evidence["TARGET"]
    assert "quantitative_evidence" not in projected_metrics[0]


def test_recent_moat_window_is_not_reported_as_total_company_history() -> None:
    years = list(range(2016, 2026))
    target = _metric(
        "TARGET",
        revenue_years=years,
        revenue_values=[100.0 * 1.05**index for index in range(10)],
        indicator_roic_years=years,
        indicator_roic_history=[0.18] * 10,
        gross_margin_years=years,
        gross_margin_history=[0.60] * 10,
    )
    evidence = derive_company_evidence(target, _context())

    recent = evidence["moat_score"]["details"]
    durability = evidence["moat_durability_score"]["details"]
    assert recent["recent_roic_spread_history_count"] == 5
    assert recent["recent_operating_evidence_years"] == 5
    assert recent["recent_operating_window_years"] == 5
    assert "history_count" not in recent
    assert durability["durability_history_years"] == 10
    assert durability["history_count"] == 10
    assert durability["history_cap"] == 10.0


def test_durability_and_runway_history_use_the_latest_consecutive_common_suffix() -> None:
    years = [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2024, 2025]
    target = _metric(
        "TARGET",
        revenue_years=years,
        revenue_values=[100.0 + index for index in range(len(years))],
        indicator_roic_years=years,
        indicator_roic_history=[0.18] * len(years),
        gross_margin_years=years,
        gross_margin_history=[0.60] * len(years),
    )

    evidence = derive_company_evidence(target, _context())
    moat = evidence["moat_score"]["details"]
    durability = evidence["moat_durability_score"]["details"]
    runway = evidence["runway_score"]["details"]

    assert moat["recent_roic_spread_history_years"] == [2024, 2025]
    assert moat["recent_gross_margin_history_years"] == [2024, 2025]
    assert moat["recent_operating_evidence_years"] == 2
    assert durability["common_history_years"] == years
    assert durability["durability_history_years"] == 2
    assert durability["history_cap"] == 2.0
    assert runway["financial_history_periods"] == years
    assert runway["financial_history_years"] == 2
    assert runway["evidence_cap"] == 6.0


def test_moat_replay_does_not_claim_historical_spreads_without_wacc() -> None:
    years = list(range(2021, 2026))
    target = _metric(
        "NO-WACC",
        wacc=None,
        indicator_roic_years=years,
        indicator_roic_history=[0.18] * len(years),
        gross_margin_years=years,
        gross_margin_history=[0.60] * len(years),
    )

    payload = derive_company_evidence(target, _context())["moat_score"]
    details = payload["details"]

    assert details["recent_roic_spread_history_years"] == []
    assert details["recent_roic_spread_history_count"] == 0
    assert details["recent_operating_evidence_years"] == 0
    assert validate_quantitative_evidence_record(payload, key="moat_score", code="NO-WACC")["score"] == payload["score"]


def test_durability_history_requires_roic_and_gross_margin_in_the_same_latest_years() -> None:
    roic_years = list(range(2016, 2026))
    gross_years = [*range(2016, 2024), 2025]
    target = _metric(
        "TARGET",
        indicator_roic_years=roic_years,
        indicator_roic_history=[0.18] * len(roic_years),
        gross_margin_years=gross_years,
        gross_margin_history=[0.60] * len(gross_years),
    )

    durability = derive_company_evidence(target, _context())["moat_durability_score"]["details"]

    assert durability["common_history_years"] == gross_years
    assert durability["durability_history_years"] == 1
    assert durability["history_cap"] == 2.0


def test_sorted_population_view_matches_materialised_list_with_duplicate_removal_and_mutation() -> None:
    source = quantitative_evidence._SortedFinitePopulation.from_sorted([-20.0, -0.0, 0.0, 0.0, 30.0, 30.0, 50.0])

    for target in (-0.0, 30.0, 7.0, None):
        expected = list(source)
        if target is not None:
            for index, value in enumerate(expected):
                if value == target:
                    expected.pop(index)
                    break
        actual = quantitative_evidence._without_one(source, target)

        assert isinstance(actual, list)
        assert actual == expected
        assert expected == actual
        assert quantitative_evidence._median(actual) == quantitative_evidence._median(expected)
        assert actual.count_at_most(-20.0) == sum(value <= -20.0 for value in expected)
        assert actual.count_at_least(30.0) == sum(value >= 30.0 for value in expected)
        assert json.dumps(actual, separators=(",", ":")) == json.dumps(expected, separators=(",", ":"))
        cloned = copy.deepcopy(actual)
        assert cloned == expected
        assert json.dumps(cloned, separators=(",", ":")) == json.dumps(expected, separators=(",", ":"))

    mutable = quantitative_evidence._without_one(source, 0.0)
    expected_mutable = list(mutable)
    mutable.append(60.0)
    expected_mutable.append(60.0)
    mutable[0] = -25.0
    expected_mutable[0] = -25.0
    mutable.pop(2)
    expected_mutable.pop(2)
    assert mutable == expected_mutable
    assert json.dumps(mutable) == json.dumps(expected_mutable)


def test_optimized_company_contexts_are_exactly_json_equivalent_to_materialised_reference() -> None:
    def materialize(value: object) -> object:
        if isinstance(value, dict):
            return {key: materialize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [materialize(item) for item in value]
        return value

    metrics = [
        _metric(
            f"EDGE-{index:02d}",
            gross_margin=[0.30, 0.30, 0.40, None][index % 4],
            gross_margin_history=[0.20, 0.30, 0.30, 0.40 + (index % 3) * 0.01],
            roic=[0.10, 0.10, None][index % 3],
            rd_intensity=[0.01, 0.01, 0.03, None][index % 4],
            revenue_latest=[100.0, 100.0, 150.0, -1.0][index % 4],
            net_profit=[-1.0, 0.0, 2.0, None][index % 4],
            free_cash_flow=[-2.0, 0.0, 3.0, None][index % 4],
            margin_trajectory=[-0.02, 0.0, 0.0, 0.03][index % 4],
            interim_revenue_yoy=[-0.10, 0.0, 0.10, None][index % 4],
            interim_profit_yoy=[-0.20, 0.0, 0.20, None][index % 4],
            market_coldness_components={
                "raw_values": {
                    "change_60d_pct": [-20.0, -20.0, 30.0, 30.0, None][index % 5],
                    "change_ytd_pct": [-10.0, 0.0, 10.0, None][index % 4],
                }
            },
        )
        for index in range(MIN_SECTOR_COMPANIES + 3)
    ]

    optimized = build_company_contexts(metrics)
    latest_complete_year = quantitative_evidence._latest_complete_financial_year(metrics[0])
    base = build_sector_context(
        metrics,
        latest_complete_year=latest_complete_year,
        enforce_reporting_period_anchor=True,
    )["TEST"]
    plain_base = materialize(base)
    assert isinstance(plain_base, dict)
    peer_count = len(metrics) - 1

    for target in metrics:
        code = str(target["code"])
        reference = quantitative_evidence._context_without_target(
            plain_base,
            target,
            peer_count=peer_count,
        )
        reference["target_code"] = code
        reference["target_excluded"] = True
        reference["latest_complete_financial_year"] = latest_complete_year

        assert optimized[code] == reference

        optimized_json = json.dumps(
            optimized[code],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        reference_json = json.dumps(
            reference,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert optimized_json == reference_json
        assert derive_company_evidence(target, optimized[code]) == derive_company_evidence(
            target,
            reference,
        )


def test_missing_interim_inputs_never_create_a_positive_catalyst() -> None:
    metric = _metric(
        "NO-CATALYST",
        interim_revenue_yoy=None,
        interim_profit_yoy=None,
        interim_ocf_yoy=None,
        margin_trajectory=None,
    )

    payload = derive_company_evidence(metric, _context())["catalyst_score"]

    assert payload["score"] == 0.0
    assert payload["details"]["company_composite"] == 0.0
    assert payload["details"]["company_turn"] is False
    assert payload["details"]["industry_turn"] is False
    assert payload["details"]["price_confirmed"] is False
    assert payload["details"]["decision_band"] == "no_interim_comparison"


@pytest.mark.parametrize(
    ("runway_years", "expected_score"),
    [
        (2.999, 1.5),
        (3.0, 3.5),
        (4.999, 3.5),
        (5.0, 5.5),
        (9.999, 5.5),
        (10.0, 7.5),
        (20.0, 7.5),
        (20.001, 9.5),
    ],
)
def test_runway_score_respects_patch6_three_five_ten_twenty_year_bands(
    runway_years: float,
    expected_score: float,
) -> None:
    assert quantitative_evidence._runway_band_score(runway_years) == expected_score


def test_derived_runway_can_reach_the_over_twenty_year_band_with_tam_evidence() -> None:
    context = _context()
    context["aggregate_revenue_cagr"] = 0.50
    context["revenue"] = {"available": False}

    runway = derive_company_evidence(
        _metric("LONG-RUNWAY", trend_growth=0.40, growth_slope=0.0, tam_runway_years=25.0),
        context,
    )["runway_score"]

    assert runway["details"]["observable_runway_years"] == pytest.approx(25.0)
    assert runway["details"]["evidence_cap"] == 10.0
    assert runway["score"] == 9.5


def test_technology_score_without_reported_rd_is_capped_even_with_strong_commercial_outcomes() -> None:
    metric = _metric(
        "NO-RD",
        rd_intensity=None,
        trend_growth=0.50,
        growth_slope=0.30,
        roic=0.60,
        wacc=0.04,
        gross_margin=0.90,
    )

    technology = derive_company_evidence(metric, _context())["technology_score"]

    assert technology["score"] <= 4.0
    assert technology["details"]["rd_intensity"] is None
    assert technology["details"]["score_cap_without_reported_rd_intensity"] == 4.0


def test_industry_bubble_score_is_independent_of_target_company_pe() -> None:
    context = _context()
    cheap = derive_company_evidence(_metric("VALUATION", pe=1.0), context)["industry_bubble_score"]
    expensive = derive_company_evidence(_metric("VALUATION", pe=1_000.0), context)["industry_bubble_score"]

    assert cheap == expensive
    assert "pe" not in cheap["details"]
    assert "valuation" not in cheap["details"]
    assert set(cheap["details"]["components"]) == {
        "supply_mismatch_risk",
        "capex_acceleration_risk",
        "profit_squeeze_risk",
        "pressure_breadth_risk",
        "bubble_risk",
    }


@pytest.mark.parametrize("missing_field", ["loss_share", "negative_fcf_share"])
def test_industry_bubble_score_requires_both_industry_pressure_breadth_inputs(missing_field: str) -> None:
    target = _metric("BREADTH-GAP")
    peers = [_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]
    context = build_company_contexts([target, *peers])["BREADTH-GAP"]
    context[missing_field] = None

    evidence = derive_company_evidence(target, context)
    industry = evidence["industry_bubble_score"]
    combined = evidence["type3_bubble_score"]

    assert industry["evidence_level"] == "partial"
    assert combined["evidence_level"] == "missing"
    missing_input = {
        "loss_share": "industry_loss_share",
        "negative_fcf_share": "industry_negative_fcf_share",
    }[missing_field]
    assert missing_input in industry["details"]["evidence_quality"]["missing_inputs"]
    assert "industry_anti_bubble_proxy" in combined["details"]["evidence_quality"]["missing_inputs"]


def test_industry_bubble_score_requires_replayable_peer_samples_and_coverage() -> None:
    target = _metric("BREADTH-COVERAGE")
    peers = [_metric(f"P{index}", free_cash_flow=None if index == 0 else 10.0) for index in range(MIN_SECTOR_COMPANIES)]
    context = build_company_contexts([target, *peers])["BREADTH-COVERAGE"]

    bubble = derive_company_evidence(target, context)["industry_bubble_score"]

    assert bubble["evidence_level"] == "partial"
    assert "industry_negative_fcf_share" in bubble["details"]["evidence_quality"]["missing_inputs"]
    assert bubble["details"]["negative_fcf_sample_count"] == MIN_SECTOR_COMPANIES - 1
    assert bubble["details"]["negative_fcf_coverage"] == pytest.approx(
        (MIN_SECTOR_COMPANIES - 1) / MIN_SECTOR_COMPANIES
    )


def test_industry_bubble_score_rejects_a_share_that_does_not_replay_from_its_population() -> None:
    target = _metric("BREADTH-MISMATCH")
    peers = [_metric(f"P{index}", net_profit=-1.0) for index in range(MIN_SECTOR_COMPANIES)]
    context = build_company_contexts([target, *peers])["BREADTH-MISMATCH"]
    assert context["loss_share"] == 1.0
    context["loss_share"] = 0.0

    bubble = derive_company_evidence(target, context)["industry_bubble_score"]

    assert bubble["evidence_level"] == "partial"
    assert bubble["details"]["loss_share"] is None
    assert "industry_loss_share" in bubble["details"]["evidence_quality"]["missing_inputs"]


def test_industry_bubble_validator_replays_pressure_breadth_counts() -> None:
    target = _metric("BREADTH-TAMPER")
    peers = [_metric(f"P{index}", net_profit=-1.0) for index in range(MIN_SECTOR_COMPANIES)]
    context = build_company_contexts([target, *peers])["BREADTH-TAMPER"]
    bubble = copy.deepcopy(derive_company_evidence(target, context)["industry_bubble_score"])
    bubble["details"]["loss_count"] = 0

    with pytest.raises(ValueError, match="share does not replay"):
        validate_quantitative_evidence_record(
            bubble,
            key="industry_bubble_score",
            code="BREADTH-TAMPER",
        )


@pytest.mark.parametrize(
    ("overrides", "missing_input"),
    [
        ({"market_coldness_score": 9.0, "peg": None}, "company_quantity_price_anti_bubble"),
        ({"market_coldness_score": None, "peg": 0.5}, "company_quantity_price_anti_bubble"),
    ],
)
def test_type3_bubble_does_not_promote_a_naked_coldness_or_peg_proxy(
    overrides: dict[str, object],
    missing_input: str,
) -> None:
    target = _metric("000001", source_trade_date="2025-12-31", **overrides)
    peers = [_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]
    context = build_company_contexts([target, *peers])["000001"]

    evidence = derive_company_evidence(target, context)

    assert evidence["industry_bubble_score"]["evidence_level"] == "derived_proxy"
    assert evidence["type3_bubble_score"]["evidence_level"] == "partial"
    assert missing_input in evidence["type3_bubble_score"]["details"]["evidence_quality"]["missing_inputs"]


def test_all_derived_scores_emit_metadata_accepted_by_the_scoring_boundary() -> None:
    metric = _metric("META")
    derived = derive_company_evidence(metric, _context())

    assert derived
    for key, payload in derived.items():
        assert set(payload) == {"score", "evidence_level", "evidence", "details"}
        assert math.isfinite(payload["score"])
        assert 0.0 <= payload["score"] <= 10.0
        assert payload["evidence_level"] in EVIDENCE_LEVELS
        assert payload["evidence_level"] != "primary"

        quality = payload["details"]["evidence_quality"]
        assert set(quality) == {
            "level",
            "input_coverage",
            "required_inputs",
            "available_inputs",
            "missing_inputs",
        }
        assert quality["level"] == payload["evidence_level"]
        assert 0.0 <= quality["input_coverage"] <= 1.0
        assert set(quality["available_inputs"]).isdisjoint(quality["missing_inputs"])
        assert quality["required_inputs"] == [
            *quality["available_inputs"],
            *quality["missing_inputs"],
        ] or set(quality["required_inputs"]) == {
            *quality["available_inputs"],
            *quality["missing_inputs"],
        }

        evidence = payload["evidence"]
        assert set(evidence) == {"source", "evidence_id", "as_of", "summary"}
        assert evidence["source"].strip() == evidence["source"]
        assert 0 < len(evidence["source"]) <= 200
        assert 0 < len(evidence["evidence_id"]) <= 200
        assert MODEL_ID in evidence["evidence_id"]
        assert evidence["evidence_id"].startswith(f"{MODEL_ID}:{key}:META:")
        assert not any(ord(character) < 32 for character in evidence["source"] + evidence["evidence_id"])
        assert date.fromisoformat(evidence["as_of"]).isoformat() == evidence["as_of"]
        assert date.fromisoformat(evidence["as_of"]) <= shanghai_today()
        assert 0 < len(evidence["summary"]) <= 1_000
        assert f"evidence_level={payload['evidence_level']}" in evidence["summary"]
        assert not any(ord(character) < 32 for character in evidence["summary"])
        assert (
            quantitative_evidence.validate_quantitative_evidence_record(
                payload,
                key=key,
                code="META",
            )["score"]
            == payload["score"]
        )

        score, normalised = buy_screener._normalise_score_evidence(
            {
                key: payload["score"],
                f"{key}_evidence": evidence,
                f"{key}_evidence_level": payload["evidence_level"],
            },
            key,
        )
        if payload["evidence_level"] == "derived_proxy":
            assert score == payload["score"]
            assert normalised == evidence
        else:
            assert score is None
            assert normalised is None


def test_quantitative_validator_rejects_a_score_even_when_its_summary_is_tampered_to_match() -> None:
    payload = copy.deepcopy(derive_company_evidence(_metric("TAMPER"), _context())["accounting_integrity_score"])
    payload["score"] = 1.0
    payload["evidence"]["summary"] = (
        f"accounting_integrity_score=1.0;model={MODEL_ID};evidence_level={payload['evidence_level']}"
    )

    with pytest.raises(ValueError, match="does not replay"):
        quantitative_evidence.validate_quantitative_evidence_record(
            payload,
            key="accounting_integrity_score",
            code="TAMPER",
        )


@pytest.mark.parametrize(
    "key",
    [
        "industry_durability_score",
        "accounting_integrity_score",
        "management_alignment_score",
        "moat_score",
        "moat_durability_score",
        "growth_quality_score",
        "growth_sustainability_score",
        "industry_bubble_score",
        "type3_bubble_score",
        "catalyst_score",
        "technology_score",
        "business_model_score",
    ],
)
def test_quantitative_validator_rejects_synchronised_score_and_component_tampering(key: str) -> None:
    payload = copy.deepcopy(derive_company_evidence(_metric("SYNC-TAMPER"), _context())[key])
    for component in payload["details"]["components"]:
        payload["details"]["components"][component] = 0.0
    payload["score"] = 0.0
    payload["evidence"]["summary"] = f"{key}=0.0;model={MODEL_ID};evidence_level={payload['evidence_level']}"

    with pytest.raises(ValueError, match="component does not replay"):
        validate_quantitative_evidence_record(payload, key=key, code="SYNC-TAMPER")


def test_quantitative_validator_rejects_synchronised_runway_score_horizon_and_cap_tampering() -> None:
    key = "runway_score"
    payload = copy.deepcopy(derive_company_evidence(_metric("RUNWAY-TAMPER"), _context())[key])
    payload["details"]["observable_runway_years"] = 100.0
    payload["details"]["evidence_cap"] = 10.0
    payload["score"] = 9.5
    payload["evidence"]["summary"] = f"{key}=9.5;model={MODEL_ID};evidence_level={payload['evidence_level']}"

    with pytest.raises(ValueError, match="does not replay"):
        validate_quantitative_evidence_record(payload, key=key, code="RUNWAY-TAMPER")


@pytest.mark.parametrize(
    ("key", "cap_field", "forged_cap"),
    [
        ("technology_score", "score_cap_without_reported_rd_intensity", 10.0),
        ("moat_durability_score", "history_cap", 10.0),
    ],
)
def test_quantitative_validator_rejects_formula_cap_tampering(
    key: str,
    cap_field: str,
    forged_cap: float,
) -> None:
    payload = copy.deepcopy(derive_company_evidence(_metric("CAP-TAMPER"), _context())[key])
    payload["details"][cap_field] = forged_cap

    with pytest.raises(ValueError, match="does not replay"):
        validate_quantitative_evidence_record(payload, key=key, code="CAP-TAMPER")


def test_discontinuous_revenue_years_cannot_complete_runway_evidence() -> None:
    metric = _metric(
        "GAPPED-RUNWAY",
        revenue_years=[2020, 2023, 2025],
        revenue_values=[80.0, 100.0, 121.0],
    )

    runway = derive_company_evidence(metric, _context())["runway_score"]

    assert runway["details"]["financial_history_years"] == 1
    assert runway["evidence_level"] == "partial"
    assert "revenue_history" in runway["details"]["evidence_quality"]["missing_inputs"]


def test_old_consecutive_fcf_history_cannot_become_complete_under_a_new_trade_date() -> None:
    metric = _metric(
        "STALE-FCF",
        financial_indicator_as_of=None,
        source_trade_date=shanghai_today().isoformat(),
        fcf_years=[2008, 2009, 2010],
        fcf_history=[7.0, 8.0, 9.0],
    )

    accounting = derive_company_evidence(metric, _context())["accounting_integrity_score"]

    assert accounting["evidence"]["as_of"] == shanghai_today().isoformat()
    assert accounting["evidence_level"] == "partial"
    assert "free_cash_flow_history" in accounting["details"]["evidence_quality"]["missing_inputs"]


def test_stale_financial_as_of_is_not_masked_by_a_newer_trade_date() -> None:
    metric = _metric(
        "STALE-FINANCIAL-AS-OF",
        financial_indicator_as_of="2010-12-31",
        source_trade_date=shanghai_today().isoformat(),
        fcf_years=[2008, 2009, 2010],
        fcf_history=[7.0, 8.0, 9.0],
    )

    accounting = derive_company_evidence(metric, _context())["accounting_integrity_score"]

    assert accounting["evidence"]["as_of"] == "2010-12-31"
    assert accounting["evidence_level"] == "partial"
    assert "free_cash_flow_history" in accounting["details"]["evidence_quality"]["missing_inputs"]


@pytest.mark.parametrize(
    ("years", "values"),
    [
        ([2023, 2024, 2025], [7.0, 8.0]),
        ([2023, 2025, 2025], [7.0, 8.0, 9.0]),
        ([2023, 2024, 2025], [7.0, 8.0, float("inf")]),
    ],
)
def test_fcf_years_and_finite_values_must_form_a_one_to_one_annual_ledger(
    years: list[int],
    values: list[float],
) -> None:
    metric = _metric("INVALID-FCF-LEDGER", fcf_years=years, fcf_history=values)

    accounting = derive_company_evidence(metric, _context())["accounting_integrity_score"]

    assert accounting["evidence_level"] == "partial"
    assert "free_cash_flow_history" in accounting["details"]["evidence_quality"]["missing_inputs"]


def test_old_consecutive_revenue_asset_years_cannot_complete_latest_growth_quality() -> None:
    metric = _metric(
        "GAPPED-GROWTH-QUALITY",
        revenue_years=[2018, 2019, 2020, 2022, 2025],
        revenue_values=[70.0, 80.0, 90.0, 105.0, 121.0],
        total_assets_years=[2018, 2019, 2020, 2022, 2025],
        total_assets_history=[65.0, 75.0, 85.0, 100.0, 110.0],
    )

    growth_quality = derive_company_evidence(metric, _context())["growth_quality_score"]

    assert growth_quality["details"]["revenue_minus_asset_cagr"] is None
    assert growth_quality["evidence_level"] == "partial"
    assert "revenue_asset_history" in growth_quality["details"]["evidence_quality"]["missing_inputs"]


def test_explicit_proxy_score_is_rederived_without_being_upgraded_to_primary() -> None:
    target = _metric(
        "PROXY",
        moat_score=9.9,
        moat_score_evidence={
            "source": "旧代理结果",
            "evidence_id": "proxy:moat:PROXY:20251231",
            "as_of": "2025-12-31",
            "summary": "旧代理结果",
        },
        moat_score_evidence_level="derived_proxy",
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score_evidence_level"] == "derived_proxy"
    assert evidence_by_code["PROXY"]["moat_score"]["evidence_level"] == "derived_proxy"
    assert target["moat_score"] == evidence_by_code["PROXY"]["moat_score"]["score"]
    assert target["moat_score"] != 9.9


def test_unlabelled_internal_proxy_cannot_be_upgraded_to_primary() -> None:
    seed = _metric("UNLABELLED")
    proxy = derive_company_evidence(seed, _context())["moat_score"]
    target = _metric(
        "UNLABELLED",
        moat_score=proxy["score"],
        moat_score_evidence=copy.deepcopy(proxy["evidence"]),
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score_evidence_level"] == "derived_proxy"
    assert evidence_by_code["UNLABELLED"]["moat_score"]["evidence_level"] == "derived_proxy"
    assert target["moat_score_evidence"]["evidence_id"].startswith(f"{MODEL_ID}:moat_score:")


def test_relabelled_internal_proxy_is_rejected_as_primary() -> None:
    payload = derive_company_evidence(_metric("RELABEL"), _context())["moat_score"]
    payload = copy.deepcopy(payload)
    payload["evidence_level"] = "primary"
    payload["evidence"]["summary"] = f"moat_score={payload['score']:.1f};model={MODEL_ID};evidence_level=primary"
    payload["details"] = {
        "basis": "dated_primary_source_score",
        "source_summary": "derived_proxy result relabelled as primary",
        "evidence_quality": {
            "level": "primary",
            "input_coverage": 1.0,
            "required_inputs": ["primary_source_score"],
            "available_inputs": ["primary_source_score"],
            "missing_inputs": [],
        },
    }

    with pytest.raises(ValueError, match="primary quantitative evidence source binding"):
        validate_quantitative_evidence_record(payload, key="moat_score", code="RELABEL")


def test_invalid_primary_score_fails_closed_and_cannot_block_a_complete_proxy() -> None:
    target = _metric(
        "BAD-PRIMARY",
        moat_score=9.9,
        moat_score_evidence={
            "source": "伪造一手结果",
            "evidence_id": "primary:moat:OTHER:20251231",
            "as_of": "2025-12-31",
            "summary": "代码未绑定",
        },
        moat_score_evidence_level="primary",
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score_evidence_level"] == "derived_proxy"
    assert target["moat_score"] == evidence_by_code["BAD-PRIMARY"]["moat_score"]["score"]
    assert target["moat_score"] != 9.9


def test_missing_raw_inputs_remain_diagnostic_and_never_attach_as_complete_scores() -> None:
    metric: dict[str, object] = {
        "code": "NO-RAW",
        "industry": "EMPTY",
        "financial_indicator_as_of": "2025-12-31",
    }

    _contexts, evidence_by_code = enrich_metrics([metric], {})

    accounting = evidence_by_code["NO-RAW"]["accounting_integrity_score"]
    assert math.isfinite(accounting["score"]), "the diagnostic stays replayable"
    assert accounting["evidence_level"] == "missing"
    assert accounting["details"]["evidence_quality"]["input_coverage"] == 0.0
    assert "accounting_integrity_score" not in metric
    assert "accounting_integrity_score_evidence" not in metric
    assert metric["accounting_integrity_score_evidence_level"] == "missing"
    assert metric["quantitative_evidence_status"] == "missing"


def test_one_observed_component_is_partial_not_a_default_complete_score() -> None:
    metric: dict[str, object] = {
        "code": "ONE-INPUT",
        "industry": "EMPTY",
        "financial_indicator_as_of": "2025-12-31",
        "ocf_np_ratio": 0.95,
    }

    _contexts, evidence_by_code = enrich_metrics([metric], {})

    accounting = evidence_by_code["ONE-INPUT"]["accounting_integrity_score"]
    quality = accounting["details"]["evidence_quality"]
    assert accounting["evidence_level"] == "partial"
    assert quality["available_inputs"] == ["ocf_to_net_profit"]
    assert quality["input_coverage"] == pytest.approx(0.25)
    assert "accounting_integrity_score" not in metric
    assert "accounting_integrity_score_evidence" not in metric
    assert metric["accounting_integrity_score_evidence_level"] == "partial"
    assert metric["quantitative_evidence_status"] == "partial"


def test_fully_observed_proxy_is_attached_with_explicit_derived_level() -> None:
    target = _metric("ATTACH")
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    accounting = evidence_by_code["ATTACH"]["accounting_integrity_score"]
    assert accounting["evidence_level"] == "derived_proxy"
    assert target["accounting_integrity_score"] == accounting["score"]
    assert target["accounting_integrity_score_evidence"] == accounting["evidence"]
    assert target["accounting_integrity_score_evidence_level"] == "derived_proxy"


def test_enrichment_preserves_only_a_strictly_bound_trusted_primary_score() -> None:
    authoritative_evidence = {
        "source": "2025 annual report research adapter",
        "evidence_id": "primary:moat_score:KEEP:20251231:knowledge-base-overlay",
        "as_of": "2025-12-31",
        "summary": "Primary-source moat assessment",
    }
    target = _metric(
        "KEEP",
        moat_score=9.7,
        moat_score_evidence=copy.deepcopy(authoritative_evidence),
        moat_score_evidence_level="primary",
        _quantitative_primary_validation_token=quantitative_evidence.PRIMARY_EVIDENCE_VALIDATION_TOKEN,
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score"] == 9.7
    assert target["moat_score_evidence_level"] == "primary"
    assert evidence_by_code["KEEP"]["moat_score"]["evidence_level"] == "primary"
    assert evidence_by_code["KEEP"]["moat_score"]["evidence"]["evidence_id"] == authoritative_evidence["evidence_id"]
    assert evidence_by_code["KEEP"]["moat_score"]["details"]["basis"] == "dated_primary_source_score"
    assert evidence_by_code["KEEP"]["moat_score"]["details"]["source_summary"] == authoritative_evidence["summary"]
    assert (
        validate_quantitative_evidence_record(
            evidence_by_code["KEEP"]["moat_score"],
            key="moat_score",
            code="KEEP",
            primary_validation_token=quantitative_evidence.PRIMARY_EVIDENCE_VALIDATION_TOKEN,
        )["score"]
        == 9.7
    )
    assert (
        validate_quantitative_evidence_record(
            copy.deepcopy(evidence_by_code["KEEP"]["moat_score"]),
            key="moat_score",
            code="KEEP",
        )["score"]
        == 9.7
    )
    assert target["quantitative_evidence"] == evidence_by_code["KEEP"]


def test_well_formed_primary_without_a_trusted_adapter_token_is_preserved() -> None:
    """Efficiency-first: shape-valid primary scores survive without a token."""
    target = _metric(
        "UNTRUSTED",
        moat_score=9.9,
        moat_score_evidence={
            "source": "Plausible but unvalidated annual report adapter",
            "evidence_id": "primary:moat_score:UNTRUSTED:20251231:knowledge-base-overlay",
            "as_of": "2025-12-31",
            "summary": "Looks correctly bound and now survives without an adapter",
        },
        moat_score_evidence_level="primary",
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score_evidence_level"] == "primary"
    assert target["moat_score"] == 9.9
    assert evidence_by_code["UNTRUSTED"]["moat_score"]["evidence_level"] == "primary"


def test_stale_primary_evidence_is_preserved_with_a_trusted_adapter_token() -> None:
    """An old-but-dated primary score from the trusted adapter stays primary."""
    target = _metric(
        "STALE",
        moat_score=9.9,
        moat_score_evidence={
            "source": "Validated but older annual report adapter",
            "evidence_id": "primary:moat_score:STALE:20240101:knowledge-base-overlay",
            "as_of": "2024-01-01",
            "summary": "The source predates the primary evidence freshness window",
        },
        moat_score_evidence_level="primary",
        _quantitative_primary_validation_token=quantitative_evidence.PRIMARY_EVIDENCE_VALIDATION_TOKEN,
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score_evidence_level"] == "primary"
    assert target["moat_score"] == 9.9
    assert evidence_by_code["STALE"]["moat_score"]["evidence_level"] == "primary"
