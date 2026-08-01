from __future__ import annotations

from copy import deepcopy

import pytest

from engine.buy_screener import score_type7_quality_equity
from engine.audit import (
    _audit_type7_ledger,
    _independent_type7_ledger_errors,
    _type7_valuation_binding_errors,
)
from engine.type7_patch6 import MODEL_ID, assess_patch6_type7, validate_patch6_type7_ledger
from tests.test_quality_equity import _history, _metric, _type1
from tools.verify_release_zip import _audit_type7_ledger_valid


def _complete_metric(*, industry: str = "ALCOHOL"):
    metric = _metric()
    metric.update(
        {
            "industry": industry,
            "listing_date": "2001-08-27",
            "gross_margin_history": [0.84, 0.85, 0.85, 0.86, 0.85],
            "gross_margin_years": [2021, 2022, 2023, 2024, 2025],
            "capex_history": [4, 5, 6, 7, 8],
            "capex_years": [2021, 2022, 2023, 2024, 2025],
            "ocf_np_ratio": 1.25,
        }
    )
    return metric


def _technology_route():
    return {
        "patch4_complete": True,
        "patch4_score": 7.0,
        "patch5_coverage": 0.80,
        "patch5_safety_complete": True,
        "patch5_safety_score": 12.0,
        "pb_history_complete": True,
        "pb_percentile": 0.10,
        "current_pb": 5.0,
    }


def _technology_turnaround_metric(*, latest_ocf_available: bool = True):
    metric = _complete_metric(industry="SOFTWARE")
    metric.update(
        {
            "financial_indicator_as_of": "2025-12-31",
            "rd_intensity": 0.15,
            "fcf_history": [-160, -130, -100, -60, -20],
            "fcf_years": [2021, 2022, 2023, 2024, 2025],
            # The 2026 observation is interim and must not replace the latest
            # complete annual OCF anchored to financial year 2025.
            "ocf_history": [12, 18, 30, 5] if latest_ocf_available else [12, 18, 5],
            "ocf_years": [2023, 2024, 2025, 2026] if latest_ocf_available else [2023, 2024, 2026],
        }
    )
    return metric


def _weak_route():
    return {
        "template5_valuation_items": {
            "t5_v1": {"complete": True, "points": 7.2},
            "t5_v2": {"complete": True, "points": 9.6},
            "t5_v3": {"complete": True, "points": 7.2},
        },
        "patch5_safety_complete": True,
        "patch5_safety_score": 12.0,
    }


def _cycle_route():
    return {
        "type5_applicable": True,
        "type5_cycle_complete": True,
        "type5_cycle_score": 8.0,
        "type5_bottom_complete": True,
        "type5_bottom_score": 8.0,
        "type5_survival_complete": True,
        "type5_survival_score": 8.0,
        "type5_upside_complete": True,
        "type5_upside_score": 8.0,
        "type5_valuation_complete": True,
        "type5_valuation_score": 6.0,
        "type5_evidence_complete": True,
        "type5_triggered": True,
        "type5_total": 7.8,
        "template25_complete": True,
        "template25_buy_zone_score": 9.0,
        "monetary_funds": 40.0,
        "interest_debt": 10.0,
        "net_debt": -30.0,
        "pb_history_complete": True,
        "pb_percentile": 0.10,
        "current_pb": 1.0,
    }


def _set_classification_component_score(ledger, group, key, score, *, inputs=None):
    classification = ledger["classification"]
    component = next(item for item in classification["components"][group] if item["key"] == key)
    previous = component["awarded_points"]
    component["awarded_points"] = score
    component["diagnostic_points"] = score
    component["upper_bound"] = score
    if inputs is not None:
        component["inputs"] = dict(inputs)
    classification["sensitivity_scores"][group] = round(
        classification["sensitivity_scores"][group] + score - previous,
        6,
    )
    classification["sensitivity_upper_bounds"][group] = round(
        classification["sensitivity_upper_bounds"][group] + score - previous,
        6,
    )
    class_label = {"W": "弱周期", "T": "强科技", "C": "强周期"}[classification["class_code"]]
    sensitivity = classification["sensitivity_scores"]
    classification["basis"] = (
        f"T={sensitivity['T']:.2f}, C={sensitivity['C']:.2f}, N={sensitivity['N']:.2f}; routed to {class_label}"
    )


def test_patch6_type7_routes_weak_cycle_and_replays_all_twelve_atomic_scores():
    ledger = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    assert ledger["model_id"] == MODEL_ID
    assert ledger["classification"]["class_code"] == "W"
    assert ledger["classification"]["sensitivity_scores"]["N"] >= 7
    assert all(len(ledger["dimensions"][key]["items"]) == 4 for key in ("BM", "MOAT", "G"))
    assert ledger["complete"]
    assert ledger["unrounded_mean"] > 7
    assert ledger["triggered"]
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


def test_patch6_type7_weak_cycle_requires_template5_valuation_and_patch5_safety_gate():
    weak_route = _weak_route()
    weak_route["patch5_safety_score"] = 7.999
    failed = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=weak_route,
    )
    missing = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence={},
    )
    one_item_missing_route = _weak_route()
    one_item_missing_route["template5_valuation_items"]["t5_v2"]["complete"] = False
    one_item_missing = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=one_item_missing_route,
    )

    assert failed["quality_certified"] is True
    assert failed["decision_gates"]["route_path"]["complete"] is True
    assert failed["decision_gates"]["route_path"]["passed"] is False
    assert failed["condition_failures"] == ["route_path"]
    assert failed["buy_ready"] is False
    assert validate_patch6_type7_ledger(failed) == []
    assert missing["decision_gates"]["route_path"]["complete"] is False
    assert "ROUTE.class_specific_path" in missing["missing_items"]
    assert validate_patch6_type7_ledger(missing) == []
    assert one_item_missing["decision_gates"]["route_path"]["complete"] is False
    assert validate_patch6_type7_ledger(one_item_missing) == []

    forged = deepcopy(failed)
    forged["decision_gates"]["route_path"]["inputs"]["template5_valuation_items"]["t5_v1"]["complete"] = False
    assert "classified route gate replay mismatch" in validate_patch6_type7_ledger(forged)


def test_patch6_type7_price_gate_is_not_waived_by_another_triggered_framework():
    only_type7_outcome, only_type7_ledger = score_type7_quality_equity(
        _complete_metric(),
        _type1(1.0),
        _history(),
        valuation_evidence_complete=True,
        other_type_triggered=False,
    )
    combined_outcome, combined_ledger = score_type7_quality_equity(
        _complete_metric(),
        _type1(1.0),
        _history(),
        valuation_evidence_complete=True,
        other_type_triggered=True,
    )

    assert only_type7_ledger["decision_gates"]["price_reasonableness"]["required"] is True
    assert only_type7_ledger["decision_gates"]["price_reasonableness"]["passed"] is False
    assert only_type7_ledger["quality_complete"] is True
    assert only_type7_ledger["quality_certified"] is True
    assert only_type7_ledger["buy_ready"] is False
    assert only_type7_outcome[0] is False
    assert only_type7_outcome[3]["_status"] == "conditional"
    assert combined_ledger["decision_gates"]["price_reasonableness"]["required"] is True
    assert combined_ledger["decision_gates"]["price_reasonableness"]["passed"] is False
    assert combined_ledger["quality_certified"] is True
    assert combined_ledger["buy_ready"] is False
    assert combined_outcome[0] is False
    assert combined_outcome[3]["_status"] == "conditional"
    assert validate_patch6_type7_ledger(combined_ledger) == []


def test_patch6_type7_technology_priority_and_proxy_provenance_are_explicit():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    assert ledger["classification"]["class_code"] == "T"
    assert ledger["classification"]["sensitivity_scores"]["T"] >= 7
    patent = next(item for item in ledger["dimensions"]["MOAT"]["items"] if item["key"] == "patent_standard")
    assert patent["evidence_level"] in {"primary", "derived_proxy"}
    assert "not primary patent" in patent["source_rule"]
    assert patent["proxy_cap"] is not None
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_incomplete_technology_fcf_window_replays_as_missing():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    metric["fcf_history"] = []
    metric["fcf_years"] = []

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    cashflow = next(item for item in ledger["dimensions"]["BM"]["items"] if item["key"] == "cashflow_inflection")
    assert ledger["classification"]["class_code"] == "T"
    assert cashflow["score"] == 0
    assert cashflow["evidence_level"] == "missing"
    assert validate_patch6_type7_ledger(ledger) == []


@pytest.mark.parametrize(
    ("industry", "component_key", "expected"),
    [
        ("STEEL", "c_commodity_driver", 1.0),
        ("CONST_MACHINERY", "c_commodity_driver", 0.5),
        ("SOFTWARE", "c_commodity_driver", 0.0),
        ("SOFTWARE", "t_intangible_patent", 1.0),
        ("MEDIA", "t_intangible_patent", 0.5),
        ("STEEL", "t_intangible_patent", 0.0),
        ("SOFTWARE", "t_iteration", 1.0),
        ("MEDIA", "t_iteration", 0.5),
        ("STEEL", "t_iteration", 0.0),
        ("ALCOHOL", "n_repeat", 2.0),
        ("RETAIL", "n_repeat", 1.0),
        ("STEEL", "n_repeat", 0.0),
    ],
)
def test_independent_type7_classification_proxy_industry_boundaries(industry, component_key, expected):
    metric = _complete_metric(industry=industry)
    if industry in {"SOFTWARE", "MEDIA"}:
        metric["rd_intensity"] = 0.15
    ledger = assess_patch6_type7(metric)
    component = next(
        item
        for records in ledger["classification"]["components"].values()
        for item in records
        if item["key"] == component_key
    )

    assert component["awarded_points"] == expected
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


@pytest.mark.parametrize(
    ("stability_score", "expected"),
    [(8.0, 1.0), (7.999999, 0.5), (5.0, 0.5), (4.999999, 0.0)],
)
def test_independent_type7_macro_beta_proxy_threshold_boundaries(stability_score, expected):
    ledger = assess_patch6_type7(_complete_metric())
    _set_classification_component_score(
        ledger,
        "N",
        "n_macro_beta",
        expected,
        inputs={"stability_score": stability_score},
    )

    assert validate_patch6_type7_ledger(ledger, expected_code="600519") == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


@pytest.mark.parametrize(
    ("industry", "group", "component_key", "legacy_score"),
    [
        ("STEEL", "C", "c_commodity_driver", 2.0),
        ("SOFTWARE", "T", "t_intangible_patent", 2.0),
        ("SOFTWARE", "T", "t_iteration", 2.0),
        ("ALCOHOL", "N", "n_repeat", 3.0),
        ("ALCOHOL", "N", "n_macro_beta", 2.0),
    ],
)
def test_independent_type7_rejects_legacy_high_proxy_score_forgery(
    industry,
    group,
    component_key,
    legacy_score,
):
    metric = _complete_metric(industry=industry)
    if industry == "SOFTWARE":
        metric["rd_intensity"] = 0.15
    forged = deepcopy(assess_patch6_type7(metric))
    _set_classification_component_score(forged, group, component_key, legacy_score)

    production_errors = validate_patch6_type7_ledger(forged, expected_code="600519")
    independent_errors = _independent_type7_ledger_errors(forged, expected_code="600519")

    assert any("classification" in error and "arithmetic invalid" in error for error in production_errors)
    assert f"independent {group}.{component_key} proxy source formula mismatch" in independent_errors


@pytest.mark.parametrize("field", ["formula", "source_rule"])
def test_independent_type7_rejects_proxy_rule_text_tampering(field):
    ledger = assess_patch6_type7(_complete_metric(industry="STEEL"))
    component = next(
        item for item in ledger["classification"]["components"]["C"] if item["key"] == "c_commodity_driver"
    )
    component[field] = "legacy uncapped industry proxy"

    assert "independent C.c_commodity_driver proxy source formula mismatch" in _independent_type7_ledger_errors(
        ledger,
        expected_code="600519",
    )


def test_independent_type7_rejects_classification_rule_text_tampering():
    ledger = assess_patch6_type7(_complete_metric())
    ledger["classification"]["rule"] = "legacy high-proxy routing rule"

    assert "independent classification decision mismatch" in _independent_type7_ledger_errors(
        ledger,
        expected_code="600519",
    )


def test_patch6_type7_technology_buy_gate_requires_each_dimension_at_least_seven():
    metric = _complete_metric(industry="SOFTWARE")
    metric.update(
        {
            "rd_intensity": 0.15,
            "moat_score": 5.0,
            # The capped industry proxies no longer provide two automatic
            # points each. Keep the company independently above the T>=7
            # classification boundary while leaving MOAT below seven.
            "business_model_score": 9.0,
            "management_alignment_score": 4.0,
            "technology_score": 6.0,
        }
    )
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    assert ledger["classification"]["class_code"] == "T"
    assert ledger["unrounded_mean"] > 7.0
    assert ledger["scores"]["MOAT"] < 7.0
    assert ledger["quality_certified"] is True
    assert ledger["condition_failures"] == ["technology_dimension_floor"]
    assert ledger["buy_ready"] is False
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []

    forged = deepcopy(ledger)
    forged["condition_failures"] = []
    forged["triggered"] = True
    forged["buy_ready"] = True
    assert "independent Type 7 decision mismatch" in _independent_type7_ledger_errors(
        forged,
        expected_code="600519",
    )


def test_patch6_type7_industry_and_rd_without_platform_or_business_evidence_is_not_strong_technology():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    for key in ("technology_score", "business_model_score"):
        metric.pop(key)
        metric.pop(f"{key}_evidence")
        metric.pop(f"{key}_evidence_level")

    ledger = assess_patch6_type7(metric, route_evidence=_technology_route())

    assert ledger["classification"]["sensitivity_scores"]["T"] == 5.0
    assert ledger["classification"]["class_code"] == "W"
    assert "T.t_platform" in ledger["classification"]["missing_components"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_technology_price_bottom_is_at_or_below_twenty_percentile():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    boundary_route = _technology_route()
    boundary_route["pb_percentile"] = 0.20
    outside_route = _technology_route()
    outside_route["pb_percentile"] = 0.200001

    boundary = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=boundary_route,
    )
    outside = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=outside_route,
    )

    assert boundary["decision_gates"]["price_reasonableness"]["buy_zone_score"] == 8.0
    assert boundary["decision_gates"]["price_reasonableness"]["passed"] is True
    assert outside["decision_gates"]["price_reasonableness"]["buy_zone_score"] == 6.0
    assert outside["decision_gates"]["price_reasonableness"]["passed"] is False
    assert outside["condition_failures"] == ["price_reasonableness"]
    assert validate_patch6_type7_ledger(boundary) == []
    assert validate_patch6_type7_ledger(outside) == []
    assert _independent_type7_ledger_errors(boundary, expected_code="600519") == []
    assert _independent_type7_ledger_errors(outside, expected_code="600519") == []


def test_patch6_type7_cycle_overlay_deducts_the_technology_cashflow_atom():
    metric = _complete_metric(industry="SOFTWARE")
    metric.update(
        {
            "rd_intensity": 0.15,
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 20, 15, 30, 20],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    assert ledger["classification"]["class_code"] == "T"
    assert "强周期" in ledger["classification"]["secondary_features"]
    cashflow = next(item for item in ledger["dimensions"]["BM"]["items"] if item["key"] == "cashflow_inflection")
    inputs = cashflow["inputs"]
    base = sum(inputs[key] for key in ("cash_conversion", "fcf_positive_score", "growth_score")) / 3
    expected_penalty = inputs["cycle_overlay_penalty"]
    assert expected_penalty > 0
    assert cashflow["score"] == pytest.approx(max(0.0, base - expected_penalty))
    assert validate_patch6_type7_ledger(ledger) == []


@pytest.mark.parametrize("mutation", ["proxy_cap", "evidence_level", "secondary_feature"])
def test_patch6_type7_validator_rejects_provenance_policy_tampering(mutation):
    ledger = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )
    forged = deepcopy(ledger)
    if mutation == "secondary_feature":
        forged["classification"]["secondary_features"] = ["强周期"]
    else:
        item = next(value for value in forged["dimensions"]["MOAT"]["items"] if value["key"] == "network_switching")
        if mutation == "proxy_cap":
            item["proxy_cap"] = 10.0
        else:
            item["evidence_level"] = "primary"

    assert validate_patch6_type7_ledger(forged)


def test_patch6_type7_requires_the_score_evidence_object_not_only_a_number_and_label():
    metric = _complete_metric()
    metric.pop("business_model_score_evidence")

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    assert not ledger["complete"]
    assert not ledger["triggered"]
    assert any(
        "business_model_score" in item["missing_inputs"]
        for section in ledger["dimensions"].values()
        for item in section["items"]
    )


@pytest.mark.parametrize("mutation", ["wrong_company", "stale", "formula", "source_rule"])
def test_patch6_type7_rejects_unbound_or_tampered_score_provenance(mutation):
    metric = _complete_metric()
    if mutation in {"wrong_company", "stale"}:
        evidence = metric["business_model_score_evidence"]
        if mutation == "wrong_company":
            evidence["evidence_id"] = "fixture:business_model_score:000001"
        else:
            evidence["as_of"] = "2000-01-01"
        ledger = assess_patch6_type7(
            metric,
            valuation_evidence_complete=True,
            valuation_score=9.0,
            route_evidence=_weak_route(),
        )
        assert ledger["quality_complete"] is False
        assert ledger["quality_certified"] is False
        assert ledger["triggered"] is False
        return

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )
    forged = deepcopy(ledger)
    item = forged["dimensions"]["BM"]["items"][0]
    item[mutation] = "任意但不可复核的说明"
    assert validate_patch6_type7_ledger(forged)


def test_patch6_type7_rejects_out_of_range_class_route_scores():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    route = _technology_route()
    route["patch4_score"] = 1e9

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=route,
    )

    assert ledger["decision_gates"]["route_path"]["complete"] is False
    assert ledger["triggered"] is False
    assert validate_patch6_type7_ledger(ledger) == []


@pytest.mark.parametrize(
    "missing_key",
    ["profit_volatility", "gross_margin_cv", "ocf_np_ratio", "gross_margin_history"],
)
def test_patch6_type7_composite_atoms_do_not_ignore_a_missing_required_input(missing_key):
    metric = _complete_metric()
    metric.pop(missing_key)

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    assert not ledger["complete"]
    assert not ledger["triggered"]
    assert ledger["missing_items"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_possible_cycle_overlay_keeps_the_dependent_technology_atom_incomplete():
    metric = _complete_metric(industry="SOFTWARE")
    metric.update(
        {
            "rd_intensity": 0.15,
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 20, 15, 30, 20],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    metric.pop("gross_margin_history")

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    assert ledger["classification"]["class_code"] == "T"
    assert "强周期" in ledger["classification"]["possible_secondary_features"]
    cashflow = next(item for item in ledger["dimensions"]["BM"]["items"] if item["key"] == "cashflow_inflection")
    assert cashflow["inputs"]["cycle_overlay_penalty"] is None
    assert not cashflow["complete"]
    assert cashflow["score"] == 0
    assert not ledger["triggered"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_strong_cycle_route_uses_class_specific_weights_and_veto():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_cycle_route(),
    )

    assert ledger["classification"]["class_code"] == "C"
    assert ledger["classification"]["sensitivity_scores"]["C"] >= 7
    assert [item["weight"] for item in ledger["dimensions"]["BM"]["items"]] == [0.35, 0.25, 0.20, 0.20]
    assert ledger["veto"] is (ledger["dimensions"]["BM"]["score"] < 5 or ledger["dimensions"]["MOAT"]["score"] < 5)
    assert validate_patch6_type7_ledger(ledger) == []


@pytest.mark.parametrize("missing_window", ["fcf", "capex"])
def test_patch6_type7_strong_cycle_incomplete_annual_windows_replay_as_missing(missing_window):
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    metric[f"{missing_window}_history"] = []
    metric[f"{missing_window}_years"] = []

    ledger = assess_patch6_type7(metric, route_evidence=_cycle_route())

    assert ledger["classification"]["class_code"] == "C"
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_strong_cycle_requires_full_type5_and_a_replayable_net_debt_bridge():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    incomplete_type5 = _cycle_route()
    incomplete_type5["type5_evidence_complete"] = False
    missing_bottom = _cycle_route()
    missing_bottom["type5_bottom_complete"] = False
    inconsistent_debt = _cycle_route()
    inconsistent_debt["net_debt"] = 0.0

    type5_gap = assess_patch6_type7(metric, route_evidence=incomplete_type5)
    bottom_gap = assess_patch6_type7(metric, route_evidence=missing_bottom)
    debt_gap = assess_patch6_type7(metric, route_evidence=inconsistent_debt)

    assert type5_gap["decision_gates"]["route_path"]["complete"] is False
    assert bottom_gap["decision_gates"]["route_path"]["complete"] is False
    assert debt_gap["decision_gates"]["route_path"]["complete"] is False
    assert "ROUTE.class_specific_path" in type5_gap["missing_items"]
    assert "ROUTE.class_specific_path" in debt_gap["missing_items"]
    assert validate_patch6_type7_ledger(type5_gap) == []
    assert validate_patch6_type7_ledger(bottom_gap) == []
    assert validate_patch6_type7_ledger(debt_gap) == []


def test_patch6_type7_strong_cycle_replays_all_five_type5_scores_and_trigger():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    route = _cycle_route()
    route["type5_bottom_score"] = 4.8
    route["type5_total"] = 7.0
    route["type5_triggered"] = True

    ledger = assess_patch6_type7(metric, route_evidence=route)
    gate = ledger["decision_gates"]["route_path"]

    assert gate["inputs"]["type5_replayed_total"] == 7.0
    assert gate["inputs"]["type5_replayed_triggered"] is True
    assert gate["complete"] is True
    assert gate["passed"] is True
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []

    forged_total = deepcopy(ledger)
    forged_total["decision_gates"]["route_path"]["inputs"]["type5_total"] = 8.0
    assert "classified route gate replay mismatch" in validate_patch6_type7_ledger(forged_total)

    forged_trigger = deepcopy(ledger)
    forged_trigger["decision_gates"]["route_path"]["inputs"]["type5_triggered"] = False
    assert "classified route gate replay mismatch" in validate_patch6_type7_ledger(forged_trigger)


def test_independent_type7_strong_cycle_rounds_raw_6_95_to_7_0():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    route = _cycle_route()
    route.update(
        {
            "type5_cycle_score": 7.0,
            "type5_bottom_score": 7.0,
            "type5_survival_score": 7.0,
            "type5_upside_score": 7.0,
            "type5_valuation_score": 6.5,
            "type5_total": 7.0,
            "type5_triggered": True,
        }
    )

    ledger = assess_patch6_type7(metric, route_evidence=route)
    gate = ledger["decision_gates"]["route_path"]

    assert sum(
        route[key] * weight
        for key, weight in {
            "type5_cycle_score": 0.35,
            "type5_bottom_score": 0.25,
            "type5_survival_score": 0.20,
            "type5_upside_score": 0.10,
            "type5_valuation_score": 0.10,
        }.items()
    ) == pytest.approx(6.95)
    assert gate["inputs"]["type5_replayed_total"] == 7.0
    assert gate["inputs"]["type5_replayed_triggered"] is True
    assert gate["complete"] is True
    assert gate["passed"] is True
    assert validate_patch6_type7_ledger(ledger, expected_code="600519") == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


def test_independent_type7_strong_cycle_rejects_more_than_two_score_decimals():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    route = _cycle_route()
    route["type5_bottom_score"] = 7.001
    route["type5_total"] = 7.6
    route["type5_triggered"] = True
    ledger = assess_patch6_type7(metric, route_evidence=route)

    assert validate_patch6_type7_ledger(ledger, expected_code="600519") == []
    assert "independent classified route replay mismatch" in _independent_type7_ledger_errors(
        ledger,
        expected_code="600519",
    )


def test_type7_strong_cycle_source_binding_rejects_coordinated_5b_5d_total_trigger_tampering():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    source_route = _cycle_route()
    source_route.update(
        {
            "type5_cycle_score": 7.0,
            "type5_bottom_score": 4.0,
            "type5_survival_score": 7.0,
            "type5_upside_score": 4.0,
            "type5_valuation_score": 6.5,
            "type5_total": 5.9,
            "type5_triggered": False,
        }
    )
    forged_route = deepcopy(source_route)
    forged_route.update(
        {
            "type5_bottom_score": 10.0,
            "type5_upside_score": 10.0,
            "type5_total": 8.0,
            "type5_triggered": True,
        }
    )
    forged = assess_patch6_type7(metric, route_evidence=forged_route)

    # The forged route is internally consistent, so ledger-only validation is
    # expected to pass. It must still differ from the company's original Type 5.
    assert validate_patch6_type7_ledger(forged, expected_code="600519") == []
    assert _independent_type7_ledger_errors(forged, expected_code="600519") == []
    row = {
        "industry": "NONFERROUS",
        "source_trade_date": forged["as_of"],
        "pb": 1.0,
        "type7": {
            "status": "triggered" if forged["triggered"] else "not_triggered",
            "applicable": True,
            "ledger": forged,
        },
        "type5": {
            "status": "not_triggered",
            "triggered": False,
            "evidence_complete": True,
            "total": 5.9,
            "sub_scores": {
                "5a": 7.0,
                "5b": 4.0,
                "5c": 7.0,
                "5d": 4.0,
                "5e": 6.5,
            },
            "reasons": {"_decision_missing_dimensions": []},
        },
    }

    errors = _type7_valuation_binding_errors("600519", row, expected_type1_1a=None)

    assert any("strong-cycle route is not bound to Type 5 evidence" in error for error in errors)


def test_independent_type7_strong_cycle_route_rejects_unknown_or_missing_inputs():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    ledger = assess_patch6_type7(metric, route_evidence=_cycle_route())
    forged = deepcopy(ledger)
    forged["decision_gates"]["route_path"]["inputs"]["legacy_type5_shortcut"] = True

    assert "independent strong-cycle route input structure mismatch" in _independent_type7_ledger_errors(
        forged,
        expected_code="600519",
    )


def test_patch6_type7_strong_cycle_near_book_value_definition_is_not_as_wide_as_pb_1_5():
    metric = _complete_metric(industry="NONFERROUS")
    metric.update(
        {
            "gross_margin_history": [0.10, 0.45, 0.12, 0.48, 0.15],
            "revenue_values": [100, 110, 121, 133.1, 146.41],
            "net_profit_history": [10, 14, 8, 18, 7],
            "capex_history": [35, 40, 45, 50, 55],
        }
    )
    near_book = _cycle_route()
    near_book["current_pb"] = 1.2
    too_wide = _cycle_route()
    too_wide["current_pb"] = 1.200001

    passed = assess_patch6_type7(metric, route_evidence=near_book)
    failed = assess_patch6_type7(metric, route_evidence=too_wide)

    assert passed["decision_gates"]["price_reasonableness"]["passed"] is True
    assert failed["decision_gates"]["price_reasonableness"]["passed"] is False


def test_patch6_type7_missing_input_publishes_upper_bound_and_cannot_trigger():
    metric = _complete_metric()
    metric["capex_history"] = []
    metric["capex_years"] = []
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
    )

    asset_light = next(item for item in ledger["dimensions"]["BM"]["items"] if item["key"] == "asset_light")
    assert not asset_light["complete"]
    assert asset_light["evidence_level"] == "missing"
    assert asset_light["upper_bound"] == 10
    assert "BM.asset_light" in ledger["missing_items"]
    assert not ledger["complete"]
    assert not ledger["triggered"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_wrapper_uses_classified_mean_and_keeps_old_rule_non_decisive():
    outcome, ledger = score_type7_quality_equity(
        _complete_metric(),
        _type1(),
        _history(),
        valuation_evidence_complete=True,
    )

    triggered, total, scores, reasons = outcome
    assert triggered
    assert total >= 7
    assert total == round(ledger["unrounded_mean"], 3)
    assert scores == {
        "7a": round(ledger["scores"]["BM"], 3),
        "7b": round(ledger["scores"]["MOAT"], 3),
        "7c": round(ledger["scores"]["G"], 3),
    }
    assert reasons["_status"] == "triggered"
    assert ledger["legacy_diagnostic"]["decisive"] is False
    assert ledger["legacy_diagnostic"]["source_rule"] == "Template1>70 AND Template5>70 AND Patch5>70"
    assert _audit_type7_ledger("600519", ledger, "triggered") == []
    assert _audit_type7_ledger_valid("600519", ledger, "triggered")


@pytest.mark.parametrize(
    "mutation",
    [
        "nan_score",
        "string_triggered",
        "unexpected_field",
        "note_tamper",
        "source_rule_tamper",
        "model_id_tamper",
        "decisive_tamper",
    ],
)
def test_patch6_type7_validator_rejects_legacy_diagnostic_boundary_tampering(mutation):
    _outcome, ledger = score_type7_quality_equity(
        _complete_metric(),
        _type1(),
        _history(),
        valuation_evidence_complete=True,
    )
    forged = deepcopy(ledger)
    legacy = forged["legacy_diagnostic"]
    if mutation == "nan_score":
        legacy["scores"]["template1"] = float("nan")
    elif mutation == "string_triggered":
        legacy["triggered"] = "False"
    elif mutation == "unexpected_field":
        legacy["arbitrary"] = "must not be accepted"
    elif mutation == "note_tamper":
        legacy["note"] = "旧评分仍可决定Type7触发"
    elif mutation == "source_rule_tamper":
        legacy["source_rule"] = "Template1>=70 OR Template5>=70 OR Patch5>=70"
    elif mutation == "model_id_tamper":
        legacy["model_id"] = "patch6-type7-quality-equity-v6"
    else:
        legacy["decisive"] = True

    assert "legacy diagnostic boundary invalid" in validate_patch6_type7_ledger(forged)


def test_patch6_type7_validator_rejects_atomic_score_tampering():
    ledger = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )
    forged = deepcopy(ledger)
    forged["dimensions"]["BM"]["items"][0]["score"] -= 1

    assert any("arithmetic invalid" in error for error in validate_patch6_type7_ledger(forged))


def test_patch6_type7_negative_free_cash_flow_is_a_known_failed_premise():
    metric = _complete_metric()
    metric["fcf_history"] = [-90, -80, -70, -60, -50]
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    assert ledger["decision_gates"]["future_fcf"]["complete"]
    assert not ledger["decision_gates"]["future_fcf"]["passed"]
    assert ledger["condition_failures"] == ["future_fcf"]
    assert not ledger["triggered"]
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


def test_patch6_type7_strong_technology_accepts_a_current_two_year_fcf_turnaround_path():
    ledger = assess_patch6_type7(
        _technology_turnaround_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    gate = ledger["decision_gates"]["future_fcf"]
    assert len(gate) == 23
    assert ledger["classification"]["class_code"] == "T"
    assert gate["complete"] is True
    assert gate["passed"] is True
    assert gate["matched_mode"] == "强科技清晰转正路径"
    assert gate["recent_three_years"] == [2023, 2024, 2025]
    assert gate["improvement_periods"] == ["2023年至2024年", "2024年至2025年"]
    assert gate["improvement_amounts"] == [40.0, 40.0]
    assert gate["median_improvement"] == 40.0
    assert gate["latest_ocf_year"] == 2025
    assert gate["latest_ocf"] == 30.0
    assert gate["estimated_years_to_positive"] == 0.5
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


def test_patch6_type7_strong_technology_turnaround_needs_latest_annual_ocf_evidence():
    ledger = assess_patch6_type7(
        _technology_turnaround_metric(latest_ocf_available=False),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )

    gate = ledger["decision_gates"]["future_fcf"]
    assert gate["complete"] is False
    assert gate["passed"] is False
    assert gate["matched_mode"] == "证据不完整"
    assert gate["latest_ocf_year"] == 2024
    assert gate["latest_ocf"] == 18.0
    assert "PRECONDITION.future_fcf" in ledger["missing_items"]
    assert "future_fcf" not in ledger["condition_failures"]
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


def test_patch6_type7_weak_cycle_cannot_use_the_technology_turnaround_exception():
    metric = _technology_turnaround_metric()
    metric["industry"] = "ALCOHOL"
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    gate = ledger["decision_gates"]["future_fcf"]
    assert ledger["classification"]["class_code"] == "W"
    assert gate["complete"] is True
    assert gate["passed"] is False
    assert gate["matched_mode"] == "未命中"
    assert ledger["condition_failures"] == ["future_fcf"]
    assert validate_patch6_type7_ledger(ledger) == []
    assert _independent_type7_ledger_errors(ledger, expected_code="600519") == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("improvement_amounts", [400.0, 400.0]),
        ("latest_ocf", -30.0),
        ("estimated_years_to_positive", 1.5),
        ("matched_mode", "耐久正自由现金流"),
        ("basis", "不可复算的主观判断"),
    ],
)
def test_patch6_type7_validator_rejects_future_fcf_turnaround_tampering(field, replacement):
    ledger = assess_patch6_type7(
        _technology_turnaround_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )
    forged = deepcopy(ledger)
    forged["decision_gates"]["future_fcf"][field] = replacement

    assert "future FCF gate replay mismatch" in validate_patch6_type7_ledger(forged)


def _technology_turnaround_raw_financial():
    fcf_by_year = dict(zip(range(2021, 2026), [-160.0, -130.0, -100.0, -60.0, -20.0]))
    ocf_by_year = dict(zip(range(2021, 2026), [10.0, 10.0, 12.0, 18.0, 30.0]))
    return {
        "cashflow": [
            {
                "REPORT_DATE": f"{year}-12-31",
                "NETCASH_OPERATE": ocf_by_year[year],
                "CONSTRUCT_LONG_ASSET": ocf_by_year[year] - fcf_by_year[year],
            }
            for year in range(2021, 2026)
        ]
    }


def test_type7_audit_binds_future_fcf_and_latest_ocf_to_raw_sources():
    metric = _technology_turnaround_metric()
    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )
    audit_kwargs = {"patch4_bindings": {"validated-captured-document": {}}}

    assert (
        _audit_type7_ledger(
            "600519",
            ledger,
            "triggered",
            source_financial=_technology_turnaround_raw_financial(),
            **audit_kwargs,
        )
        == []
    )
    assert (
        _audit_type7_ledger(
            "600519",
            ledger,
            "triggered",
            source_metric=metric,
            **audit_kwargs,
        )
        == []
    )


def test_type7_audit_rejects_coordinated_latest_ocf_tampering_against_raw_financials():
    source = assess_patch6_type7(
        _technology_turnaround_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )
    forged = deepcopy(source)
    forged["decision_gates"]["future_fcf"]["latest_ocf"] = 300.0

    # OCF remains positive, so both ledger-only replays are internally valid.
    assert validate_patch6_type7_ledger(forged, expected_code="600519") == []
    assert _independent_type7_ledger_errors(forged, expected_code="600519") == []
    errors = _audit_type7_ledger(
        "600519",
        forged,
        "triggered",
        patch4_bindings={"validated-captured-document": {}},
        source_financial=_technology_turnaround_raw_financial(),
    )

    assert any("latest OCF differs from raw annual cash-flow evidence" in error for error in errors)


def test_type7_audit_rejects_coordinated_fcf_improvement_projection_tampering():
    source = assess_patch6_type7(
        _technology_turnaround_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_technology_route(),
    )
    forged = deepcopy(source)
    gate = forged["decision_gates"]["future_fcf"]
    gate["values"] = [-160.0, -130.0, -100.0, -50.0, -10.0]
    gate["latest_fcf"] = -10.0
    gate["recent_three_values"] = [-100.0, -50.0, -10.0]
    gate["improvement_amounts"] = [50.0, 40.0]
    gate["median_improvement"] = 45.0
    gate["estimated_years_to_positive"] = 10.0 / 45.0
    gate["basis"] = (
        "命中强科技清晰转正路径：最近3年FCF严格逐年改善，最新年度经营现金流为正，"
        "按最近两次改善额中位数线性外推约0.22年转正。"
    )

    # All derived gate fields were changed together, so ledger-only formula
    # replay is valid; captured raw FCF history must still disprove the edit.
    assert validate_patch6_type7_ledger(forged, expected_code="600519") == []
    assert _independent_type7_ledger_errors(forged, expected_code="600519") == []
    errors = _audit_type7_ledger(
        "600519",
        forged,
        "triggered",
        patch4_bindings={"validated-captured-document": {}},
        source_row={"code": "600519", "type7": {"status": "triggered", "ledger": source}},
        source_financial=_technology_turnaround_raw_financial(),
    )

    assert any("future-FCF history differs from raw annual cash-flow evidence" in error for error in errors)
    assert any("decision gates differ from raw-source replay" in error for error in errors)


def test_patch6_type7_uses_current_fcf_suffix_without_penalizing_an_older_gap():
    metric = _complete_metric()
    metric["financial_indicator_as_of"] = "2025-12-31"
    metric["fcf_history"] = [-30, -10, 90, 108, 130, 158, 190]
    metric["fcf_years"] = [2018, 2019, 2021, 2022, 2023, 2024, 2025]

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    fcf_conversion = next(item for item in ledger["dimensions"]["BM"]["items"] if item["key"] == "fcf_conversion")
    assert fcf_conversion["complete"] is True
    assert fcf_conversion["inputs"]["fcf_positive_score"] == 10.0
    assert fcf_conversion["inputs"]["fcf_used_years"] == [2021, 2022, 2023, 2024, 2025]
    assert fcf_conversion["inputs"]["latest_financial_year"] == 2025
    assert ledger["decision_gates"]["future_fcf"]["years"] == [2021, 2022, 2023, 2024, 2025]
    assert ledger["decision_gates"]["future_fcf"]["complete"] is True
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_rejects_capex_history_that_stops_before_latest_financial_year():
    metric = _complete_metric()
    metric["financial_indicator_as_of"] = "2025-12-31"
    metric["capex_history"] = [4, 5, 6]
    metric["capex_years"] = [2021, 2022, 2023]

    ledger = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    asset_light = next(item for item in ledger["dimensions"]["BM"]["items"] if item["key"] == "asset_light")
    assert asset_light["complete"] is False
    assert asset_light["inputs"]["capex_revenue_used_years"] == [2021, 2022, 2023]
    assert asset_light["inputs"]["latest_financial_year"] == 2025
    assert asset_light["inputs"]["annual_window_current"] is False
    assert "BM.asset_light" in ledger["missing_items"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_missing_valuation_is_not_filled_with_a_zero_score():
    ledger = assess_patch6_type7(_complete_metric(), route_evidence=_weak_route())

    price_gate = ledger["decision_gates"]["price_reasonableness"]
    assert price_gate["buy_zone_score"] is None
    assert not price_gate["complete"]
    assert "VALUATION.price_reasonableness" in ledger["missing_items"]
    assert not ledger["triggered"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_technology_requires_patch4_patch5_path_and_pb_history():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    ledger = assess_patch6_type7(metric, route_evidence={})

    assert ledger["classification"]["class_code"] == "T"
    assert not ledger["decision_gates"]["route_path"]["complete"]
    assert not ledger["decision_gates"]["price_reasonableness"]["complete"]
    assert ledger["history_request_needed"] is (ledger["upper_bound"] > 7)
    assert not ledger["triggered"]
    assert validate_patch6_type7_ledger(ledger) == []


def test_patch6_type7_validator_rejects_cross_company_or_date_reuse():
    ledger = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )

    errors = validate_patch6_type7_ledger(
        ledger,
        expected_code="000001",
        expected_as_of="2026-07-16",
    )
    assert "company/date binding mismatch" in errors


def test_patch6_type7_validator_rejects_coordinated_route_flag_tampering():
    ledger = assess_patch6_type7(
        _complete_metric(),
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence=_weak_route(),
    )
    forged = deepcopy(ledger)
    forged["decision_gates"]["price_reasonableness"]["passed"] = False
    forged["condition_failures"] = ["price_reasonableness"]
    forged["triggered"] = False

    assert "valuation gate replay mismatch" in validate_patch6_type7_ledger(forged)


def test_type7_audit_binds_coordinated_route_edits_to_the_raw_source_replay():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    source = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence={
            "patch5_coverage": 0.80,
            "patch5_safety_complete": True,
            "patch5_safety_score": 12.0,
            "pb_history_complete": True,
            "pb_percentile": 0.10,
            "current_pb": 5.0,
        },
    )
    forged = deepcopy(source)
    route = forged["decision_gates"]["route_path"]
    route["inputs"]["patch4_complete"] = True
    route["inputs"]["patch4_score"] = 7.0
    route["complete"] = True
    route["passed"] = True
    forged["missing_items"] = []
    forged["complete"] = True
    forged["triggered"] = True
    forged["buy_ready"] = True

    # Both self-validation implementations accept the coordinated ledger edit;
    # only a replay from the captured company inputs can disprove its premise.
    assert validate_patch6_type7_ledger(forged, expected_code="600519") == []
    assert _independent_type7_ledger_errors(forged, expected_code="600519") == []
    errors = _audit_type7_ledger(
        "600519",
        forged,
        "triggered",
        source_row={
            "code": "600519",
            "type7": {"status": "insufficient_evidence", "ledger": source},
        },
    )

    assert any("decision gates differ from raw-source replay" in error for error in errors)


def test_type7_audit_binds_coordinated_atomic_edits_to_the_raw_source_replay():
    metric = _complete_metric(industry="SOFTWARE")
    metric["rd_intensity"] = 0.15
    source = assess_patch6_type7(
        metric,
        valuation_evidence_complete=True,
        valuation_score=9.0,
        route_evidence={
            "patch5_coverage": 0.80,
            "patch5_safety_complete": True,
            "patch5_safety_score": 12.0,
            "pb_history_complete": True,
            "pb_percentile": 0.10,
            "current_pb": 5.0,
        },
    )
    source_row = {
        "code": "600519",
        "type7": {"status": "insufficient_evidence", "ledger": source},
    }
    assert _audit_type7_ledger("600519", source, "insufficient_evidence", source_row=source_row) == []

    forged = deepcopy(source)
    atom = next(item for item in forged["dimensions"]["BM"]["items"] if item["key"] == "rd_conversion")
    atom["inputs"] = {key: 10.0 for key in atom["inputs"]}
    atom["score"] = 10.0
    atom["points"] = 10.0 * atom["weight"]
    atom["upper_bound"] = 10.0
    bm_score = sum(item["points"] for item in forged["dimensions"]["BM"]["items"])
    forged["dimensions"]["BM"]["score"] = bm_score
    forged["dimensions"]["BM"]["upper_bound"] = bm_score
    forged["scores"]["BM"] = bm_score
    forged["unrounded_mean"] = sum(forged["scores"].values()) / 3.0
    forged["score"] = round(forged["unrounded_mean"], 3)
    forged["upper_bound"] = round(
        sum(forged["dimensions"][dimension]["upper_bound"] for dimension in ("BM", "MOAT", "G")) / 3.0,
        3,
    )

    assert validate_patch6_type7_ledger(forged, expected_code="600519") == []
    assert _independent_type7_ledger_errors(forged, expected_code="600519") == []
    errors = _audit_type7_ledger(
        "600519",
        forged,
        "insufficient_evidence",
        source_row=source_row,
    )

    assert any("atomic dimensions differ from raw-source replay" in error for error in errors)
