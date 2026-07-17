from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from data.capex_evidence import resolve_capex_evidence
from engine.audit import (
    _audit_bear_case,
    _independent_checks,
    _markdown_cell,
    _scoring_replay_checks,
    _spreadsheet_safe_frame,
    audit_random_sample as _audit_random_sample,
    render_audit_markdown,
    write_audit_artifacts,
)
from engine.buy_screener import TYPE_NAMES, TYPE_WEIGHTS, screen_all_types
from engine.dcf import ReportingPeriodContract
from engine.pipeline import PipelineIssue, run_market_analysis, validate_market_analysis_quality


def _reporting_period_contract() -> ReportingPeriodContract:
    return ReportingPeriodContract(
        annual_report_date="2025-12-31",
        current_interim_report_date="2026-03-31",
        prior_interim_report_date="2025-03-31",
    )


def audit_random_sample(*args, **kwargs):
    kwargs.setdefault("reporting_period_contract", _reporting_period_contract())
    return _audit_random_sample(*args, **kwargs)


def _market_coldness_evidence(codes):
    return {
        str(code): {
            "market_coldness_score": 6.0,
            "market_coldness_score_evidence": {
                "source": "whole-market-test-source",
                "evidence_id": f"coldness:{code}:20260331",
                "as_of": "2026-03-31",
                "summary": "whole-market fixture",
            },
        }
        for code in codes
    }


def _cashflow_row(report_date: str, operating_cash_flow: float, capex: float) -> dict:
    value, provenance = resolve_capex_evidence(capex, None, report_date=report_date)
    return {
        "REPORT_DATE": report_date,
        "NETCASH_OPERATE": operating_cash_flow,
        "CONSTRUCT_LONG_ASSET": value,
        "CAPEX_PROVENANCE": provenance,
    }


def _market(count: int = 5):
    quotes = []
    financials = {}
    for index in range(count):
        code = f"{index + 1:06d}"
        quotes.append(
            {
                "code": code,
                "name": f"样本{index + 1}",
                "market": "SZ",
                "price": 10.0 + index,
                "pe": 12.0 + index,
                "pb": 1.2,
                "market_cap": 1_000_000_000.0 + index * 100_000_000.0,
            }
        )
        financials[code] = {
            "revenue_history": [
                {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": 100.0 + (year - 2021) * 10}
                for year in range(2021, 2026)
            ],
            "income_history": [
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "PARENT_NETPROFIT": 10.0 + (year - 2021),
                    "OPERATE_PROFIT": 14.0 + (year - 2021),
                    "TOTAL_OPERATE_INCOME": 100.0 + (year - 2021) * 10,
                }
                for year in range(2021, 2026)
            ],
            "cashflow": [_cashflow_row(f"{year}-12-31", 18.0 + (year - 2023), 5.0) for year in range(2023, 2026)],
            "balance": [
                {
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_ASSETS": 100.0,
                    "TOTAL_LIABILITIES": 30.0,
                    "TOTAL_PARENT_EQUITY": 70.0,
                    "MONETARYFUNDS": 5.0,
                    "SHORT_LOAN": 2.0,
                    "LONG_LOAN": 1.0,
                    "BONDS_PAYABLE": 0.0,
                    "NONCURRENT_LIAB_1YEAR": 0.0,
                    "LEASE_LIAB": 0.0,
                }
                for year in range(2021, 2026)
            ],
            "income_q1": [],
            "cashflow_q1": [],
            "income_interim": [
                {
                    "REPORT_DATE": "2025-03-31",
                    "TOTAL_OPERATE_INCOME": 25.0,
                    "PARENT_NETPROFIT": 2.5,
                },
                {
                    "REPORT_DATE": "2026-03-31",
                    "TOTAL_OPERATE_INCOME": 27.5,
                    "PARENT_NETPROFIT": 2.75,
                },
            ],
            "cashflow_interim": [
                _cashflow_row("2025-03-31", 4.0, 1.0),
                _cashflow_row("2026-03-31", 4.4, 1.1),
            ],
            "indicators": [],
        }
    return pd.DataFrame(quotes), financials


def test_random_audit_is_fixed_seed_and_input_order_independent():
    quotes, financials = _market()
    coldness = _market_coldness_evidence(financials)
    first = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        seed=20260715,
        sample_size=3,
        max_workers=1,
        market_coldness_evidence=coldness,
    )
    second = audit_random_sample(
        quotes.iloc[::-1].reset_index(drop=True),
        dict(reversed(list(financials.items()))),
        eligible_codes=financials,
        seed=20260715,
        sample_size=3,
        max_workers=2,
        market_coldness_evidence=coldness,
    )

    assert first.sample_codes == second.sample_codes
    assert first.scores["code"].tolist() == second.scores["code"].tolist()
    assert first.invariant_errors == second.invariant_errors == ()
    assert first.scoring_replay_errors == second.scoring_replay_errors == ()
    assert first.valuation_replay_errors == second.valuation_replay_errors == ()
    assert first.analysis_quality["score_coverage"] == 1.0
    assert first.analysis_quality["dcf_attempt_coverage"] == 1.0
    assert "dcf_valid_coverage" in first.analysis_quality
    assert "pipeline_issue_rate" in first.analysis_quality
    assert all(len(value) == 3 for value in first.scores["bear_case"])
    assert first.provenance["snapshot_content_sha256"] == second.provenance["snapshot_content_sha256"]
    assert first.provenance["reporting_period_contract"] == {
        **_reporting_period_contract().__dict__,
        "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
    }
    assert first.provenance["market_coldness_evidence"]["eligible_evidence_count"] == 5
    assert first.provenance["market_coldness_evidence"]["eligible_evidence_coverage"] == 1.0
    assert first.provenance["market_coldness_evidence"]["sources"] == ["whole-market-test-source"]
    assert len(first.provenance["market_coldness_evidence"]["evidence_sha256"]) == 64
    assert all(value is not None for value in first.scores["market_coldness_score"])


def test_every_field_scoring_replay_detects_coherent_subscore_tampering():
    quotes, financials = _market()
    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        seed=1,
        sample_size=5,
        max_workers=1,
    )
    published = audit.scores.copy(deep=True)
    index = published.index[published["code"] == "000002"][0]
    payload = deepcopy(published.at[index, "type1"])
    payload["sub_scores"]["1c"] = min(10.0, payload["sub_scores"]["1c"] + 1.0)
    payload["total"] = round(
        sum(payload["sub_scores"][key] * TYPE_WEIGHTS["type1"][key] for key in TYPE_WEIGHTS["type1"]),
        1,
    )
    published.at[index, "type1"] = payload
    published.at[index, "type1_score"] = payload["total"]

    errors = _scoring_replay_checks(published, audit.scores, audit.sample_codes)

    assert any("every-field scoring replay differs" in error for error in errors)


def test_audit_csv_boundary_neutralises_formula_cells():
    original = pd.DataFrame([{"名称": "+cmd|' /C calc'!A0", "依据": "@SUM(1,1)", "分数": 1.0}])

    safe = _spreadsheet_safe_frame(original)

    assert safe.loc[0, "名称"].startswith("'+")
    assert safe.loc[0, "依据"].startswith("'@")
    assert original.loc[0, "名称"].startswith("+")


def test_random_audit_builds_benchmarks_from_full_eligible_universe(monkeypatch):
    quotes, financials = _market(7)
    calls = []
    from engine import audit as audit_module

    original = audit_module.run_market_analysis

    def captured(quotes_arg, financials_arg, **kwargs):
        calls.append((len(quotes_arg), len(financials_arg), tuple(kwargs.get("eligible_codes", ()))))
        return original(quotes_arg, financials_arg, **kwargs)

    monkeypatch.setattr(audit_module, "run_market_analysis", captured)

    audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        seed=1,
        sample_size=3,
        max_workers=1,
    )

    assert calls == [(7, 7, tuple(sorted(financials)))]


def test_random_audit_rejects_an_impossible_sample_size():
    quotes, financials = _market(2)
    with pytest.raises(ValueError, match="sample_size"):
        audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=3)


def test_random_audit_fails_closed_without_a_reporting_period_contract():
    quotes, financials = _market(1)

    with pytest.raises(ValueError, match="reporting_period_contract is required"):
        _audit_random_sample(quotes, financials, eligible_codes=financials, sample_size=1)


def test_audit_markdown_contains_reproducibility_and_every_sampled_company():
    quotes, financials = _market()
    audit = audit_random_sample(
        quotes, financials, eligible_codes=financials, seed=20260715, sample_size=3, max_workers=1
    )

    report = render_audit_markdown(audit, data_timestamp=123.0)

    assert "seed: `20260715`" in report
    assert "sample_size: `3`" in report
    for code in audit.sample_codes:
        assert code in report


def test_audit_markdown_renders_missing_numeric_values_as_empty_cells():
    assert _markdown_cell(None) == ""
    assert _markdown_cell(float("nan")) == ""
    assert _markdown_cell(pd.NA) == ""


def test_audit_artifacts_include_machine_and_human_readable_outputs(tmp_path):
    quotes, financials = _market()
    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        seed=20260715,
        sample_size=3,
        max_workers=1,
        snapshot_sha256="a" * 64,
    )

    paths = write_audit_artifacts(audit, tmp_path, data_timestamp=123.0)

    assert set(paths) == {"json", "csv", "markdown"}
    assert all(path.is_file() for path in paths.values())
    assert '"seed": 20260715' in paths["json"].read_text(encoding="utf-8")
    assert len(pd.read_csv(paths["csv"], dtype={"代码": str})) == 3
    assert "# 固定随机 3 家公司审计" in paths["markdown"].read_text(encoding="utf-8")
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["provenance"]["snapshot_artifact_sha256"] == "a" * 64
    assert len(payload["provenance"]["code_sha256"]) == 64
    assert len(payload["provenance"]["rules_sha256"]) == 64
    assert payload["companion_artifacts_sha256"]["csv"] == hashlib.sha256(paths["csv"].read_bytes()).hexdigest()
    assert (
        payload["companion_artifacts_sha256"]["markdown"] == hashlib.sha256(paths["markdown"].read_bytes()).hexdigest()
    )
    assert "dcf_results" in payload
    assert "dcf_skip_reasons" in payload
    csv = pd.read_csv(paths["csv"], dtype={"代码": str})
    assert {"1a子分", "1a依据", "DCF状态", "DCF参数JSON"} <= set(csv.columns)


def test_audit_json_normalizes_missing_numeric_values_to_null(tmp_path):
    quotes, financials = _market()
    quotes.loc[0, "pe"] = float("nan")
    quotes.loc[1, "pb"] = float("inf")
    audit = audit_random_sample(
        quotes, financials, eligible_codes=financials, seed=20260715, sample_size=5, max_workers=1
    )

    paths = write_audit_artifacts(audit, tmp_path, data_timestamp=123.0)
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))

    by_code = {company["code"]: company for company in payload["companies"]}
    assert by_code["000001"]["pe"] is None
    assert by_code["000002"]["pb"] is None


def test_random_audit_samples_only_explicitly_eligible_companies():
    quotes, financials = _market(5)

    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=["000002", "000004"],
        seed=1,
        sample_size=2,
        max_workers=1,
    )

    assert audit.sample_codes == ("000002", "000004")
    assert audit.eligible_universe_size == 2


def test_random_audit_defensively_excludes_bj_even_if_marked_eligible():
    quotes, financials = _market(5)
    quotes.loc[0, "market"] = "BJ"

    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        seed=1,
        sample_size=4,
        max_workers=1,
    )

    assert "000001" not in audit.sample_codes
    assert audit.eligible_universe_size == 4
    sampled_markets = quotes.set_index("code").loc[list(audit.sample_codes), "market"]
    assert set(sampled_markets) <= {"SH", "SZ"}


def test_random_audit_rejects_an_all_bj_eligible_universe():
    quotes, financials = _market(1)
    quotes.loc[0, "market"] = "BJ"

    with pytest.raises(ValueError, match="SH/SZ"):
        audit_random_sample(quotes, financials, eligible_codes=financials, sample_size=1)


def test_random_audit_requires_a_validated_eligible_universe():
    quotes, financials = _market(2)
    with pytest.raises(ValueError, match="eligible_codes"):
        audit_random_sample(quotes, financials, eligible_codes=[], sample_size=1)


def test_random_audit_independently_rejects_accounting_identity_conflicts():
    quotes, financials = _market(5)
    for row in financials["000002"]["balance"]:
        row.update({"TOTAL_ASSETS": 100.0, "TOTAL_LIABILITIES": 30.0, "TOTAL_EQUITY": 10.0})

    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        sample_size=5,
        max_workers=1,
    )

    assert any("assets=liabilities+equity identity fails" in error for error in audit.independent_errors)
    assert any("attributable equity exceeds total equity" in error for error in audit.independent_errors)


def test_random_audit_independently_rejects_stale_interim_periods():
    quotes, financials = _market(1)
    for dataset in ("income_interim", "cashflow_interim"):
        for row in financials["000001"][dataset]:
            row["REPORT_DATE"] = f"{int(row['REPORT_DATE'][:4]) - 5}{row['REPORT_DATE'][4:]}"

    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        sample_size=1,
        max_workers=1,
    )

    assert any("current interim report is not newer" in error for error in audit.independent_errors)


def test_random_audit_revalidates_precomputed_analysis_identity_and_quality():
    quotes, financials = _market(5)
    analysis = run_market_analysis(
        quotes,
        financials,
        eligible_codes=financials,
        enforce_quality=True,
        expected_companies=5,
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )
    sample_result = deepcopy(next(iter(analysis.dcf_results.values())))
    sample_result["code"] = "999999"
    tampered_identity = replace(
        analysis,
        dcf_results={**analysis.dcf_results, "999999": sample_result},
        issues=(*analysis.issues, PipelineIssue("999999", "forged", "outside universe")),
    )

    with pytest.raises(ValueError, match="quality revalidation"):
        audit_random_sample(
            quotes,
            financials,
            eligible_codes=financials,
            sample_size=5,
            max_workers=1,
            full_market_analysis=tampered_identity,
        )


def test_random_audit_valuation_replay_detects_a_coherent_forged_skip():
    quotes, financials = _market(5)
    analysis = run_market_analysis(
        quotes,
        financials,
        eligible_codes=financials,
        enforce_quality=True,
        expected_companies=5,
        max_workers=1,
        reporting_period_contract=_reporting_period_contract(),
    )
    omitted = sorted(analysis.dcf_results)[0]
    forged_results = {code: result for code, result in analysis.dcf_results.items() if code != omitted}
    forged_skips = {**analysis.dcf_skip_reasons, omitted: "forged_skip_despite_valid_evidence"}
    forged_scores = screen_all_types(financials, quotes, dcf_results=forged_results)
    forged = replace(
        analysis,
        scores=forged_scores,
        dcf_results=forged_results,
        dcf_skipped=analysis.dcf_skipped + 1,
        dcf_skip_reasons=forged_skips,
        quality={},
    )
    forged = replace(
        forged,
        quality=validate_market_analysis_quality(
            forged,
            expected_companies=5,
            expected_codes=financials,
        ),
    )

    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        sample_size=5,
        max_workers=1,
        full_market_analysis=forged,
    )

    assert any("valid-result identities differ" in error for error in audit.valuation_replay_errors)
    assert any("skipped identities differ" in error for error in audit.valuation_replay_errors)

    tampered_quality = replace(analysis, quality={"ok": True, "dcf_valid": 999})
    with pytest.raises(ValueError, match="quality metadata"):
        audit_random_sample(
            quotes,
            financials,
            eligible_codes=financials,
            sample_size=5,
            max_workers=1,
            full_market_analysis=tampered_quality,
        )


def _single_company_independent_errors(row, audit, quotes, financials, *, dcf_result=None):
    code = str(row["code"])
    result = audit.dcf_results[code] if dcf_result is None else dcf_result
    return _independent_checks(
        pd.DataFrame([row]),
        (code,),
        {code: result},
        {},
        quotes=quotes,
        financials=financials,
        reporting_period_contract=_reporting_period_contract(),
    )


def _forced_type2_row(row, score, *, triggered):
    changed = deepcopy(row)
    status = "triggered" if triggered else "observe" if 5.0 <= score < 7.0 else "not_triggered"
    reasons = {key: "审计构造证据" for key in TYPE_WEIGHTS["type2"]}
    reasons.update({"_status": status, "_applicable": "yes", "_evidence": "complete"})
    payload = {
        "triggered": triggered,
        "total": float(score),
        "sub_scores": {key: float(score) for key in TYPE_WEIGHTS["type2"]},
        "reasons": reasons,
        "veto": False,
        "status": status,
        "applicable": True,
        "evidence_complete": True,
    }
    changed["type2"] = payload
    changed["type2_score"] = float(score)
    changed["buy_types"] = ["type2"] if triggered else []
    changed["num_types"] = 1 if triggered else 0
    changed["primary_type"] = "type2" if triggered else None
    changed["primary_label"] = TYPE_NAMES["type2"] if triggered else "无触发（不买）"
    if score > max(float(changed[f"{key}_score"]) for key in TYPE_WEIGHTS if key != "type2"):
        changed["diagnostic_type"] = "type2"
        changed["diagnostic_label"] = TYPE_NAMES["type2"]
        changed["max_score"] = float(score)
        changed["bear_case"] = _audit_bear_case("type2", payload)
    return changed


def test_independent_audit_rejects_both_low_score_false_positive_and_qualifying_false_negative():
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    original = audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict()

    false_positive = _forced_type2_row(original, 6.7, triggered=True)
    errors = _single_company_independent_errors(false_positive, audit, quotes, financials)
    assert any("triggered status violates threshold/veto/condition" in error for error in errors)

    false_negative = _forced_type2_row(original, 8.0, triggered=False)
    errors = _single_company_independent_errors(false_negative, audit, quotes, financials)
    assert any("not_triggered status is inside observe/trigger band" in error for error in errors)


def test_independent_audit_does_not_add_non_patch6_type2_profit_veto():
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    original = audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict()
    forged = _forced_type2_row(original, 8.0, triggered=True)
    invalid_financials = deepcopy(financials)
    invalid_financials["000002"]["income_interim"][0]["PARENT_NETPROFIT"] = 0.0
    invalid_financials["000002"]["income_interim"][1]["PARENT_NETPROFIT"] = -1.0

    errors = _single_company_independent_errors(forged, audit, quotes, invalid_financials)

    assert not any("profit evidence" in error for error in errors)


def test_independent_audit_does_not_add_global_negative_current_revenue_veto():
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    original = audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict()
    forged = deepcopy(original)
    reasons = {key: "审计构造证据" for key in TYPE_WEIGHTS["type6"]}
    reasons.update({"_status": "triggered", "_applicable": "yes", "_evidence": "complete"})
    type6 = {
        "triggered": True,
        "total": 10.0,
        "sub_scores": {key: 10.0 for key in TYPE_WEIGHTS["type6"]},
        "reasons": reasons,
        "veto": False,
        "status": "triggered",
        "applicable": True,
        "evidence_complete": True,
    }
    forged["type6"] = type6
    forged["type6_score"] = 10.0
    forged["buy_types"] = ["type6"]
    forged["num_types"] = 1
    forged["primary_type"] = "type6"
    forged["primary_label"] = TYPE_NAMES["type6"]
    forged["diagnostic_type"] = "type6"
    forged["diagnostic_label"] = TYPE_NAMES["type6"]
    forged["max_score"] = 10.0
    forged["bear_case"] = _audit_bear_case("type6", type6)
    invalid_financials = deepcopy(financials)
    invalid_financials["000002"]["income_interim"][0]["TOTAL_OPERATE_INCOME"] = -2.0
    invalid_financials["000002"]["income_interim"][1]["TOTAL_OPERATE_INCOME"] = -1.0

    errors = _single_company_independent_errors(forged, audit, quotes, invalid_financials)

    assert not any("trigger contradicts negative exact same-period revenue" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("num_types", 99, "num_types mismatch"),
        ("primary_type", "type6", "primary_type does not match fixed priority"),
        ("primary_label", "错误标签", "primary_label mismatch"),
        ("diagnostic_type", "type6", "diagnostic_type is not the highest-scoring framework"),
        ("diagnostic_label", "错误标签", "diagnostic_label mismatch"),
        ("max_score", 9.9, "max_score is not the diagnostic maximum"),
    ],
)
def test_independent_audit_rejects_public_summary_mutations(field, bad_value, message):
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    row = audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict()
    row[field] = bad_value

    errors = _single_company_independent_errors(row, audit, quotes, financials)

    assert any(message in error for error in errors)


def test_independent_audit_accepts_explicit_no_applicable_framework_label():
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    row = deepcopy(audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict())
    for key in TYPE_WEIGHTS:
        payload = row[key]
        payload["triggered"] = False
        payload["status"] = "not_applicable"
        payload["applicable"] = False
        payload["evidence_complete"] = False
        payload["veto"] = False
        payload["reasons"].pop("_veto", None)
        payload["reasons"].update(
            {
                "_status": "not_applicable",
                "_applicable": "no",
                "_evidence": "not_applicable",
            }
        )
    row.update(
        {
            "buy_types": [],
            "num_types": 0,
            "primary_type": None,
            "primary_label": "无触发（不买）",
            "diagnostic_type": None,
            "diagnostic_label": "无可完整诊断框架",
            "max_score": None,
            "bear_case": [],
        }
    )

    errors = _single_company_independent_errors(row, audit, quotes, financials)

    assert not any("diagnostic_label" in error for error in errors)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda result: result.__setitem__("zone", "买入区"), "valuation zone does not match"),
        (lambda result: result.__setitem__("mean1", result["mean1"] * 2), "mean1 does not match"),
        (
            lambda result: result.__setitem__("valuation_center", result["valuation_center"] * 2),
            "valuation_center does not match neutral scenario midpoint",
        ),
        (
            lambda result: result.__setitem__("dcf_value_mean_legacy_alias_of", "valuation_center"),
            "legacy dcf_value_mean alias is not disclosed",
        ),
        (lambda result: result.__setitem__("base_wacc", result["base_wacc"] + 0.01), "base WACC"),
        (
            lambda result: result["risk_parameters"].__setitem__("risk_free_rate", 0.0),
            "CAPM risk parameter evidence invalid",
        ),
        (
            lambda result: result["ttm_fcff_evidence"].__setitem__("value", result["ttm_fcff_evidence"]["value"] * 2),
            "strict TTM FCFF provenance differs from source reconstruction",
        ),
        (
            lambda result: result.__setitem__("valuation_input_basis", "annual"),
            "valuation input basis is not strict TTM",
        ),
        (lambda result: result.pop("beta_evidence"), "beta evidence missing"),
        (
            lambda result: (
                result.__setitem__("tax_shield_rate", 0.25),
                result.__setitem__("tax_shield_source", "three_year_positive_operating_profit"),
                result["wacc_components"].__setitem__("tax_shield_rate", 0.25),
            ),
            "debt tax shield is not supported",
        ),
        (
            lambda result: result["dcf_points"]["neutral"].__setitem__(
                "upper", result["dcf_points"]["neutral"]["upper"] * 1.1
            ),
            "neutral DCF upper endpoint mismatch",
        ),
    ],
)
def test_independent_audit_rejects_valuation_mutations(mutation, message):
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    row = audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict()
    result = deepcopy(audit.dcf_results["000002"])
    mutation(result)

    errors = _single_company_independent_errors(row, audit, quotes, financials, dcf_result=result)

    assert any(message in error for error in errors)


def test_independent_audit_binds_financial_and_industrial_valuations_to_current_company_evidence():
    quotes, financials = _market()
    for record in financials["000001"]["income_history"]:
        record["PARENT_NETPROFIT"] *= 2
    for record in financials["000002"]["cashflow"]:
        record["NETCASH_OPERATE"] *= 2
    for record in financials["000002"]["revenue_history"]:
        record["TOTAL_OPERATE_INCOME"] *= 2
    for record in financials["000002"]["income_history"]:
        record["TOTAL_OPERATE_INCOME"] *= 2
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)

    wrong_financials = dict(financials)
    wrong_financials["000001"] = financials["000003"]
    financial_row = audit.scores.loc[audit.scores["code"] == "000001"].iloc[0].to_dict()
    errors = _single_company_independent_errors(financial_row, audit, quotes, wrong_financials)
    assert any("normalised ROE does not match attributable evidence" in error for error in errors)

    wrong_financials = dict(financials)
    wrong_financials["000002"] = financials["000003"]
    industrial_row = audit.scores.loc[audit.scores["code"] == "000002"].iloc[0].to_dict()
    errors = _single_company_independent_errors(industrial_row, audit, quotes, wrong_financials)
    assert any("base FCFF does not match current company evidence" in error for error in errors)
