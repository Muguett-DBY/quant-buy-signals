"""Patch 7 (2026-08-04) cross-type trigger constraints.

The template's trigger-constraint rules add a post-gate on top of the seven
per-type triggers: a single-dimension trigger is not a complete buy point.
"""

import unittest

from data.mobile_snapshot import _compact_type
from engine.buy_screener import (
    PATCH7_GATE_VERSION,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_VETOED,
    TYPE_WEIGHTS,
    _DECISION_MARKET_CONTEXT,
    _DECISION_MISSING_DIMENSIONS_REASON,
    _DECISION_MISSING_REQUIREMENTS_REASON,
    _apply_patch7_total_gate,
    _decision_missing_requirements,
    _patch7_declining_industry,
    _patch7_long_term_decline,
    replay_buy_decision,
)

TYPE_KEYS = ["type1", "type2", "type3", "type4", "type5", "type6", "type7"]


def _outcome(triggered: bool = True, veto: str | None = None):
    reasons = {"_status": "triggered" if triggered else "not_triggered"}
    if veto:
        reasons["_veto"] = veto
    return (triggered, 7.0, {"x": 7.0}, reasons)


def _base(triggered: dict[str, bool], veto: dict[str, str] | None = None):
    veto = veto or {}
    return {key: _outcome(triggered.get(key, False), veto.get(key)) for key in TYPE_KEYS}


class Patch7RedLinesTest(unittest.TestCase):
    def test_long_term_decline_requires_three_negative_year_over_year_changes(self):
        metric = {
            "revenue_values": [100, 90, 80, 70],
            "revenue_years": [2022, 2023, 2024, 2025],
        }
        self.assertTrue(_patch7_long_term_decline(metric))

    def test_mixed_growth_is_not_a_red_line(self):
        metric = {
            "revenue_values": [100, 110, 105, 120],
            "revenue_years": [2022, 2023, 2024, 2025],
        }
        self.assertFalse(_patch7_long_term_decline(metric))

    def test_missing_revenue_history_never_triggers_the_red_line(self):
        self.assertFalse(_patch7_long_term_decline({"industry": "医药"}))

    def test_declining_industry_benchmark_is_a_red_line(self):
        metric = {"industry": "医药"}
        benchmarks = {"医药": {"median_cagr": -0.03}, "ALL": {}}
        self.assertTrue(_patch7_declining_industry(metric, benchmarks))

    def test_growing_industry_is_not_a_red_line(self):
        metric = {"industry": "医药"}
        benchmarks = {"医药": {"median_cagr": 0.05}, "ALL": {}}
        self.assertFalse(_patch7_declining_industry(metric, benchmarks))

    def test_missing_industry_benchmark_never_triggers_the_red_line(self):
        metric = {"industry": "医药"}
        self.assertFalse(_patch7_declining_industry(metric, {"ALL": {}}))


class Patch7TotalGateTest(unittest.TestCase):
    def test_gate_version_is_pinned(self):
        self.assertEqual(PATCH7_GATE_VERSION, "2026-08-04")

    def test_type1_alone_with_value_trap_veto_is_suppressed(self):
        outcomes = _base(
            {"type1": True},
            veto={"type1": "价值陷阱未排除"},
        )
        gated = _apply_patch7_total_gate(outcomes, {"price": 10.0}, {"zone": "买入区"}, {"ALL": {}})
        self.assertFalse(gated["type1"][0])
        self.assertIn("补丁7红线否决", str(gated["type1"][3].get("_veto")))

    def test_type1_alone_with_declining_industry_red_line_is_suppressed(self):
        outcomes = _base({"type1": True})
        benchmarks = {"医药": {"median_cagr": -0.05}, "ALL": {}}
        gated = _apply_patch7_total_gate(outcomes, {"price": 10.0, "industry": "医药"}, {"zone": "买入区"}, benchmarks)
        self.assertFalse(gated["type1"][0])
        self.assertIn("衰落产业", str(gated["type1"][3].get("_veto")))

    def test_type1_alone_without_red_lines_is_kept(self):
        outcomes = _base({"type1": True})
        benchmarks = {"医药": {"median_cagr": 0.05}, "ALL": {}}
        gated = _apply_patch7_total_gate(
            outcomes,
            {"price": 10.0, "industry": "医药", "revenue_values": [100, 110, 120], "revenue_years": [2023, 2024, 2025]},
            {"zone": "买入区"},
            benchmarks,
        )
        self.assertTrue(gated["type1"][0])

    def test_type2_alone_in_bubble_is_suppressed(self):
        outcomes = _base({"type2": True})
        dcf = {
            "zone": "观察区",
            "bubble_warning": True,
            "dcf_points": {"optimistic": {"upper": 20.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})
        self.assertFalse(gated["type2"][0])
        self.assertIn("泡沫线否决", str(gated["type2"][3].get("_veto")))

    def test_type2_type3_combination_outside_bubble_is_kept(self):
        outcomes = _base({"type2": True, "type3": True})
        dcf = {
            "zone": "观察区",
            "bubble_warning": False,
            "dcf_points": {"optimistic": {"upper": 30.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})
        self.assertTrue(gated["type2"][0])
        self.assertTrue(gated["type3"][0])

    def test_type2347_gate_waits_for_missing_price(self):
        outcomes = _base({"type2": True, "type3": True, "type4": True, "type7": True})
        dcf = {
            "zone": "观察区",
            "bubble_warning": False,
            "dcf_points": {"optimistic": {"upper": 30.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {}, dcf, {"ALL": {}})

        for key in ("type2", "type3", "type4", "type7"):
            with self.subTest(type_key=key):
                self.assertFalse(gated[key][0])
                self.assertEqual(gated[key][3]["_status"], STATUS_INSUFFICIENT_EVIDENCE)
                self.assertIn("当前股价", str(gated[key][3].get("_missing")))
                self.assertNotIn("乐观估值上界", str(gated[key][3].get("_missing")))
                self.assertEqual(
                    gated[key][3][_DECISION_MISSING_REQUIREMENTS_REASON],
                    ["patch7_current_price"],
                )
                self.assertNotIn("_veto", gated[key][3])

    def test_type2347_gate_waits_for_missing_optimistic_upper(self):
        outcomes = _base({"type2": True, "type3": True, "type4": True, "type7": True})
        dcf = {
            "zone": "观察区",
            "bubble_warning": False,
            "dcf_points": {"optimistic": {}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})

        for key in ("type2", "type3", "type4", "type7"):
            with self.subTest(type_key=key):
                self.assertFalse(gated[key][0])
                self.assertEqual(gated[key][3]["_status"], STATUS_INSUFFICIENT_EVIDENCE)
                self.assertNotIn("当前股价", str(gated[key][3].get("_missing")))
                self.assertIn("乐观估值上界", str(gated[key][3].get("_missing")))
                self.assertEqual(
                    gated[key][3][_DECISION_MISSING_REQUIREMENTS_REASON],
                    ["patch7_optimistic_upper"],
                )
                self.assertNotIn("_veto", gated[key][3])

    def test_explicit_bubble_warning_remains_a_veto_when_line_inputs_are_missing(self):
        outcomes = _base({"type2": True, "type7": True})
        dcf = {"zone": "卖出区", "bubble_warning": True, "dcf_points": {}}
        gated = _apply_patch7_total_gate(outcomes, {}, dcf, {"ALL": {}})

        for key in ("type2", "type7"):
            with self.subTest(type_key=key):
                self.assertFalse(gated[key][0])
                self.assertEqual(gated[key][3]["_status"], STATUS_VETOED)
                self.assertIn("泡沫线否决", str(gated[key][3].get("_veto")))

    def test_missing_patch7_requirement_keeps_verified_score_dimensions_exact(self):
        type_key = "type3"
        sub_scores = {key: 8.0 for key in TYPE_WEIGHTS[type_key]}
        outcomes = _base({})
        outcomes[type_key] = (
            True,
            8.0,
            sub_scores,
            {
                "_status": "triggered",
                "_applicable": "yes",
                "_evidence": "complete",
                _DECISION_MISSING_DIMENSIONS_REASON: [],
            },
        )
        gated = _apply_patch7_total_gate(
            outcomes,
            {"price": 25.0},
            {"zone": "观察区", "bubble_warning": False, "dcf_points": {"optimistic": {}}},
            {"ALL": {}},
        )
        triggered, total, exact_scores, reasons = gated[type_key]
        payload = {
            "triggered": triggered,
            "total": total,
            "sub_scores": exact_scores,
            "reasons": reasons,
            "status": reasons["_status"],
            "applicable": reasons["_applicable"] == "yes",
            "evidence_complete": reasons["_evidence"] == "complete",
            _DECISION_MARKET_CONTEXT: {
                "tradable": True,
                "reference_price": False,
                "risk_status": "",
            },
        }

        self.assertFalse(triggered)
        self.assertEqual(reasons["_status"], STATUS_INSUFFICIENT_EVIDENCE)
        self.assertEqual(
            reasons[_DECISION_MISSING_REQUIREMENTS_REASON],
            ["patch7_optimistic_upper"],
        )
        decision = replay_buy_decision(type_key, payload)
        self.assertFalse(decision["decision_complete"])
        self.assertEqual(decision["decision_basis"], "unresolved_missing_evidence")
        self.assertTrue(decision["potentially_triggerable"])
        self.assertEqual(decision["veto_state"], "none")
        self.assertEqual(decision["missing_dimensions"], [])
        self.assertEqual(decision["score_lower_bound"], 8.0)
        self.assertEqual(decision["score_upper_bound"], 8.0)

        payload["decision"] = decision
        compact = _compact_type(payload, type_key)
        self.assertEqual(compact["sub_scores"], sub_scores)
        self.assertNotIn("estimated_sub_scores", compact)
        self.assertIn("乐观估值上界", compact["reason"])

        fallback_payload = dict(payload)
        fallback_reasons = dict(reasons)
        fallback_reasons.pop(_DECISION_MISSING_REQUIREMENTS_REASON)
        fallback_payload["reasons"] = fallback_reasons
        fallback_payload.pop("decision")
        fallback = replay_buy_decision(type_key, fallback_payload)
        self.assertEqual(set(fallback["missing_dimensions"]), set(TYPE_WEIGHTS[type_key]))

    def test_decision_missing_requirements_reject_unknown_duplicate_or_malformed_values(self):
        invalid_values = (
            [],
            ["patch7_current_price", "patch7_current_price"],
            ["unknown_requirement"],
            [1],
            "patch7_current_price",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _decision_missing_requirements({_DECISION_MISSING_REQUIREMENTS_REASON: value})

    def test_replay_rejects_patch7_requirement_state_conflicts(self):
        scores = {key: 8.0 for key in TYPE_WEIGHTS["type3"]}
        cases = (
            ("not_applicable", "no", "incomplete"),
            (STATUS_INSUFFICIENT_EVIDENCE, "yes", "complete"),
            ("observe", "yes", "incomplete"),
        )
        for status, applicable, evidence in cases:
            with self.subTest(status=status, applicable=applicable, evidence=evidence):
                payload = {
                    "sub_scores": scores,
                    "reasons": {
                        "_status": status,
                        "_applicable": applicable,
                        "_evidence": evidence,
                        _DECISION_MISSING_DIMENSIONS_REASON: [],
                        _DECISION_MISSING_REQUIREMENTS_REASON: ["patch7_current_price"],
                    },
                }
                with self.assertRaises(ValueError):
                    replay_buy_decision("type3", payload)

    def test_type2_type7_combination_in_bubble_is_suppressed(self):
        outcomes = _base({"type2": True, "type7": True})
        dcf = {
            "zone": "卖出区",
            "bubble_warning": True,
            "dcf_points": {"optimistic": {"upper": 20.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})
        self.assertFalse(gated["type2"][0])
        self.assertFalse(gated["type7"][0])

    def test_type7_alone_at_or_below_optimistic_is_kept(self):
        outcomes = _base({"type7": True})
        dcf = {
            "zone": "观察区",
            "bubble_warning": False,
            "dcf_points": {"optimistic": {"upper": 30.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})
        self.assertTrue(gated["type7"][0])

    def test_type7_alone_waits_for_complete_price_evidence(self):
        cases = (
            ({}, {"dcf_points": {"optimistic": {"upper": 30.0}}}, "当前股价"),
            ({"price": 25.0}, {"dcf_points": {"optimistic": {}}}, "乐观估值上界"),
        )
        for metric, dcf, missing_label in cases:
            with self.subTest(missing=missing_label):
                gated = _apply_patch7_total_gate(_base({"type7": True}), metric, dcf, {"ALL": {}})
                self.assertFalse(gated["type7"][0])
                self.assertEqual(gated["type7"][3]["_status"], STATUS_INSUFFICIENT_EVIDENCE)
                self.assertIn(missing_label, str(gated["type7"][3].get("_missing")))
                self.assertNotIn("_veto", gated["type7"][3])

    def test_type7_alone_in_sell_zone_is_suppressed(self):
        outcomes = _base({"type7": True})
        dcf = {
            "zone": "卖出区",
            "bubble_warning": True,
            "dcf_points": {"optimistic": {"upper": 20.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})
        self.assertFalse(gated["type7"][0])
        self.assertIn("价格闸门", str(gated["type7"][3].get("_veto")))

    def test_type1_plus_type2_combination_is_outside_the_bubble_gate(self):
        outcomes = _base({"type1": True, "type2": True})
        dcf = {
            "zone": "观察区",
            "bubble_warning": True,
            "dcf_points": {"optimistic": {"upper": 20.0}},
        }
        gated = _apply_patch7_total_gate(outcomes, {"price": 25.0}, dcf, {"ALL": {}})
        self.assertTrue(gated["type1"][0])
        self.assertTrue(gated["type2"][0])

    def test_no_trigger_is_unchanged(self):
        outcomes = _base({})
        gated = _apply_patch7_total_gate(outcomes, {"price": 10.0}, {"zone": "买入区"}, {"ALL": {}})
        self.assertEqual(gated, outcomes)

    def test_type5_type6_are_outside_the_gates(self):
        outcomes = _base({"type5": True, "type6": True})
        dcf = {"zone": "卖出区", "bubble_warning": True, "dcf_points": {"optimistic": {"upper": 1.0}}}
        gated = _apply_patch7_total_gate(outcomes, {"price": 999.0}, dcf, {"ALL": {}})
        self.assertTrue(gated["type5"][0])
        self.assertTrue(gated["type6"][0])


if __name__ == "__main__":
    unittest.main()
