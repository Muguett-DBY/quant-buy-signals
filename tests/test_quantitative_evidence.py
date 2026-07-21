from __future__ import annotations

import copy
import json
import math
from datetime import date

import pytest

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
        "gross_margin_history": [0.43, 0.44, 0.45, 0.45],
        "margin_history": [0.10, 0.105, 0.108, 0.11],
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
    base = build_sector_context(metrics)["TEST"]
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
        assert date.fromisoformat(evidence["as_of"]) <= date.today()
        assert 0 < len(evidence["summary"]) <= 1_000
        assert f"evidence_level={payload['evidence_level']}" in evidence["summary"]
        assert not any(ord(character) < 32 for character in evidence["summary"])

        score, normalised = buy_screener._normalise_score_evidence(
            {key: payload["score"], f"{key}_evidence": evidence},
            key,
        )
        assert score == payload["score"]
        assert normalised == evidence


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


def test_enrichment_does_not_overwrite_an_existing_valid_score_and_evidence() -> None:
    authoritative_evidence = {
        "source": "2025 annual report research adapter",
        "evidence_id": "primary:moat:KEEP:20251231",
        "as_of": "2025-12-31",
        "summary": "Primary-source moat assessment",
    }
    target = _metric(
        "KEEP",
        moat_score=9.7,
        moat_score_evidence=copy.deepcopy(authoritative_evidence),
    )
    metrics = [target, *[_metric(f"P{index}") for index in range(MIN_SECTOR_COMPANIES)]]

    _contexts, evidence_by_code = enrich_metrics(metrics, {})

    assert target["moat_score"] == 9.7
    assert target["moat_score_evidence"] == authoritative_evidence
    assert target["moat_score_evidence_level"] == "primary"
    assert evidence_by_code["KEEP"]["moat_score"]["evidence"] != authoritative_evidence
    assert target["quantitative_evidence"] == evidence_by_code["KEEP"]
