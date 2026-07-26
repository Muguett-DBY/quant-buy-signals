from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from data.capex_evidence import resolve_capex_evidence
from data.financial_source_evidence import zero_capex_evidence
from data.market_coldness import (
    EASTMONEY_CLIST_ENDPOINT,
    EASTMONEY_SOURCE,
    MarketColdnessCoverage,
    MarketColdnessRecord,
    MarketColdnessSnapshot,
    MetricCoverage,
)
from engine.audit import (
    _audit_bear_case,
    _audit_patch4_evidence_valid,
    _audit_type5_bottom_evidence_errors,
    _audit_type5_market_replay,
    _audit_type7_ledger,
    _audit_validate_capex_provenance,
    _independent_checks,
    _markdown_cell,
    _scoring_replay_checks,
    _spreadsheet_safe_frame,
    audit_random_sample as _audit_random_sample,
    render_audit_markdown,
    write_audit_artifacts,
)
from engine.buy_screener import (
    TYPE_NAMES,
    TYPE_WEIGHTS,
    _build_bear_case,
    score_type7_quality_equity,
    screen_all_types,
)
from engine.dcf import ReportingPeriodContract
from engine.market_coldness import build_market_coldness_evidence
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
    ordered_codes = tuple(sorted(str(code) for code in codes))
    retrieved = "2026-03-31T08:20:00+00:00"
    records = tuple(
        MarketColdnessRecord(
            code=code,
            exchange="SZ",
            eastmoney_market_id=0,
            name=f"样本{code}",
            change_60d_pct=-10.0,
            change_ytd_pct=-10.0,
            turnover_rate_pct=1.5,
            volume_ratio=0.9,
            listing_date="2001-08-27",
            source_updated_at=retrieved,
            source=EASTMONEY_SOURCE,
            source_url=EASTMONEY_CLIST_ENDPOINT,
            retrieved_at=retrieved,
            upstream_fields={},
            missing_reasons={},
        )
        for code in ordered_codes
    )
    metrics = (
        "change_60d_pct",
        "change_ytd_pct",
        "turnover_rate_pct",
        "volume_ratio",
        "listing_date",
        "source_updated_at",
    )
    coverage = {metric: MetricCoverage(present=len(records), missing=0, coverage_rate=1.0) for metric in metrics}
    snapshot = MarketColdnessSnapshot(
        available=True,
        records=records,
        source=EASTMONEY_SOURCE,
        source_url=EASTMONEY_CLIST_ENDPOINT,
        retrieved_at=retrieved,
        total_expected=len(records),
        fetched_count=len(records),
        page_count=1,
        response_bytes=1_000,
        universe_coverage_rate=1.0,
        coverage=MarketColdnessCoverage(len(records), len(records), 1.0, coverage),
        cache_hit=False,
        cache_diagnostic="",
        reason="",
        failure=None,
    )
    return build_market_coldness_evidence(
        snapshot,
        as_of_session="2026-03-31",
        listed_quote_codes=ordered_codes,
        now=datetime(2026, 4, 1, 2, 0, tzinfo=timezone.utc),
        min_cross_section_records=len(records),
        min_board_turnover_records=len(records),
    )


def _cashflow_row(report_date: str, operating_cash_flow: float, capex: float) -> dict:
    value, provenance = resolve_capex_evidence(capex, None, report_date=report_date)
    return {
        "REPORT_DATE": report_date,
        "NETCASH_OPERATE": operating_cash_flow,
        "CONSTRUCT_LONG_ASSET": value,
        "CAPEX_PROVENANCE": provenance,
    }


def test_independent_patch4_evidence_requires_the_exact_pinned_announcement_identity():
    code = "300750"
    art_code = "AN202607140000000001"
    digest = "0123456789abcdef"
    evidence = {
        "source": "东方财富上市公司公告正文",
        "evidence_id": f"eastmoney-notice:{code}:{art_code}:sha256:{digest}",
        "url": f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html",
        "as_of": "2026-07-14",
        "summary": f"公告正文明确陈述：测试（正文SHA-256前16位：{digest}）",
    }
    allowed = {
        evidence["evidence_id"]: {
            "evidence_id": evidence["evidence_id"],
            "url": evidence["url"],
            "as_of": evidence["as_of"],
            "content_sha256": digest + "0" * 48,
        }
    }

    assert _audit_patch4_evidence_valid(
        evidence,
        code=code,
        as_of=date(2026, 7, 15),
        allowed_bindings=allowed,
    )
    assert not _audit_patch4_evidence_valid(evidence, code=code, as_of=date(2026, 7, 15))
    for field, invalid in (
        ("source", "伪造公告源"),
        ("evidence_id", f"malicious:{code}:{art_code}:sha256:{digest}"),
        ("url", f"https://example.test/notices/{code}/{art_code}.html"),
        ("summary", "公告正文明确陈述：测试"),
        ("as_of", "2026-7-14"),
    ):
        tampered = dict(evidence)
        tampered[field] = invalid
        assert not _audit_patch4_evidence_valid(
            tampered,
            code=code,
            as_of=date(2026, 7, 15),
            allowed_bindings=allowed,
        )


def test_independent_audit_accepts_committed_exchange_filed_zero_capex_evidence():
    code = "600854"
    report_date = "2026-03-31"
    official_evidence = zero_capex_evidence()[(code, report_date)]
    value, provenance = resolve_capex_evidence(
        None,
        None,
        report_date=report_date,
        security_code=code,
        official_evidence=official_evidence,
    )

    assert value == 0.0
    assert (
        _audit_validate_capex_provenance(
            provenance,
            expected_value=value,
            expected_report_date=report_date,
        )
        == "complete"
    )


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
    quotes["source_trade_date"] = "2026-03-31"
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
    assert first.provenance["market_coldness_evidence"]["sources"] == [
        f"{EASTMONEY_SOURCE}; {EASTMONEY_CLIST_ENDPOINT}"
    ]
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
    assert "dcf_skip_classifications" in payload
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


def _replayable_type7_history(code="000001", as_of="2026-07-15"):
    shareholder_span_days = 3_652
    shareholder_start_close = 100.0
    shareholder_end_close = shareholder_start_close * (1.18 ** (shareholder_span_days / 365.2425))
    return {
        "available": True,
        "code": code,
        "as_of": as_of,
        "model_id": "type7-market-history-v1",
        "shareholder_return": {
            "available": True,
            "method": "Tencent backward-adjusted weekly close total-return proxy",
            "target_years": 10,
            "start_date": "2016-07-15",
            "end_date": as_of,
            "observations": 521,
            "span_days": shareholder_span_days,
            "start_close_hfq": shareholder_start_close,
            "end_close_hfq": shareholder_end_close,
            "total_return": shareholder_end_close / shareholder_start_close - 1.0,
            "cagr": (shareholder_end_close / shareholder_start_close) ** (365.2425 / shareholder_span_days) - 1.0,
            "formula": "total=end_hfq/start_hfq-1;cagr=(end_hfq/start_hfq)^(365.2425/days)-1",
            "reason": "",
        },
        "valuation_history": {
            "available": True,
            "window_years": 5,
            "target_start_date": "2021-07-15",
            "start_date": "2021-07-15",
            "end_date": as_of,
            "row_count": 801,
            "span_days": 1_826,
            "start_delay_days": 0,
            "pe_observations": 800,
            "pb_observations": 800,
            "current_pe_ttm": 20.0,
            "median_pe_ttm": 25.0,
            "current_pb_mrq": 6.0,
            "median_pb_mrq": 7.0,
            "pe_percentile": 0.10,
            "pb_percentile": 0.12,
            "pe_distribution": {"values": [10.0, 25.0], "counts": [80, 720]},
            "pb_distribution": {"values": [5.0, 7.0], "counts": [96, 704]},
            "formula": "percentile=(count(x<current)+0.5*count(x=current))/historical_count",
            "reason": "",
        },
    }


def _replayable_type7_ledger():
    type1 = (
        False,
        5.0,
        {"1a": 5.0, "1b": 5.0, "1c": 5.0, "1d": 5.0},
        {"_status": "observe", "_evidence": "complete"},
    )
    outcome, ledger = score_type7_quality_equity(
        {"code": "000001", "industry": "SOFTWARE", "source_trade_date": "2026-07-15"},
        type1,
        _replayable_type7_history(),
        valuation_evidence_complete=True,
    )
    return outcome, ledger


def _replayable_type5_bottom_contract():
    return {
        "schema_version": 1,
        "model_id": "type5-bottom-observables-v1",
        "code": "000001",
        "as_of": "2026-07-15",
        "quote_pb": 0.95,
        "valuation_history": {
            "available": True,
            "window_years": 5,
            "span_days": 1_800,
            "start_delay_days": 1,
            "end_date": "2026-07-15",
            "pb_observations": 800,
            "pb_percentile": 0.08,
            "current_pb_mrq": 0.95,
            "median_pb_mrq": 1.14,
            "pb_distribution": {"values": [0.76, 1.14], "counts": [64, 736]},
            "formula": "percentile=(count(x<current)+0.5*count(x=current))/historical_count",
        },
        "market_coldness_record": {
            "score": 8.5,
            "evidence_level": "derived_proxy",
            "evidence": {
                "source": "市场量价历史",
                "evidence_id": "market-coldness:000001:20260715",
                "as_of": "2026-07-15",
                "summary": "同日量价冷度",
            },
            "components": {
                "as_of_session": "2026-07-15",
                "raw_values": {
                    "change_60d_pct": -25.0,
                    "change_ytd_pct": -30.0,
                },
            },
        },
        "financial_cycle": {
            "gross_margin_history": [0.42, 0.30, 0.18, 0.25, 0.38, 0.45, 0.29, 0.35, 0.23, 0.17],
            "gross_margin_years": list(range(2016, 2026)),
            "net_profit_history": [100.0, 50.0, 20.0, 40.0, 80.0, 120.0, 60.0, 90.0, 50.0, 30.0],
            "net_profit_years": list(range(2016, 2026)),
        },
    }


def _replayable_type5_payload():
    return {
        "sub_scores": {"5a": 5.0, "5b": 9.6, "5c": 5.0, "5d": 5.0, "5e": 5.0},
        "reasons": {
            "5a": "审计夹具",
            "5b": "PB8%/0.95;冷8;毛10",
            "5c": "审计夹具",
            "5d": "审计夹具",
            "5e": "审计夹具",
            "_status": "observe",
            "_applicable": "yes",
            "_evidence": "complete",
        },
        "status": "observe",
        "bottom_evidence_mode": "automatic_replay",
        "bottom_evidence_contract": _replayable_type5_bottom_contract(),
    }


def _type5_market_record_002522_rounding_boundary():
    """Production-shaped record whose six-decimal ranks lose replay precision."""

    return {
        "score": 7.7,
        "evidence_level": "derived_proxy",
        "evidence": {
            "source": "Eastmoney push2 clist; https://push2delay.eastmoney.com/api/qt/clist/get",
            "evidence_id": "patch6-type2c-quantity-price-v1:002522:20260724",
            "as_of": "2026-07-24",
            "summary": "量价冷度;60日-28.7%;YTD-22.8%;换手2.38%;量比0.64;上限8.0",
        },
        "components": {
            "schema_version": 1,
            "model_id": "patch6-type2c-quantity-price-v1",
            "code": "002522",
            "source": "Eastmoney push2 clist",
            "raw_values": {
                "change_60d_pct": -28.74,
                "change_ytd_pct": -22.78,
                "turnover_rate_pct": 2.38,
                "volume_ratio": 0.64,
            },
            "absolute": {
                "change_60d_pct": 9.187,
                "change_ytd_pct": 8.278,
                "turnover_rate_pct": 6.12,
                "volume_ratio": 8.2,
            },
            "relative": {
                "change_60d_pct": 6.663244,
                "change_ytd_pct": 5.503003,
                "turnover_rate_pct": 3.890236,
                "volume_ratio": 5.449098,
            },
            "relative_sample_sizes": {
                "change_60d_pct": 5162,
                "change_ytd_pct": 5162,
                "turnover_rate_pct": 1486,
                "volume_ratio": 5158,
            },
            "relative_context": {
                "change_60d_pct": {
                    "section_size": 5162,
                    "minimum_section_records": 1000,
                    "section_population": 5162,
                    "source_present": 5539,
                    "source_total": 5542,
                    "lower_count": 1507,
                    "equal_count": 2,
                },
                "change_ytd_pct": {
                    "section_size": 5162,
                    "minimum_section_records": 1000,
                    "section_population": 5162,
                    "source_present": 5539,
                    "source_total": 5542,
                    "lower_count": 2256,
                    "equal_count": 1,
                },
                "turnover_rate_pct": {
                    "section_size": 1486,
                    "minimum_section_records": 200,
                    "section_population": 1486,
                    "source_present": 5542,
                    "source_total": 5542,
                    "lower_count": 948,
                    "equal_count": 2,
                },
                "volume_ratio": {
                    "section_size": 5158,
                    "minimum_section_records": 1000,
                    "section_population": 5162,
                    "source_present": 5197,
                    "source_total": 5542,
                    "lower_count": 2216,
                    "equal_count": 147,
                },
            },
            "metric_scores": {
                "change_60d_pct": 8.682249,
                "change_ytd_pct": 7.723001,
                "turnover_rate_pct": 5.674047,
                "volume_ratio": 7.64982,
            },
            "weights": {
                "change_60d_pct": 0.45,
                "change_ytd_pct": 0.25,
                "turnover_rate_pct": 0.2,
                "volume_ratio": 0.1,
            },
            "ytd_reliability": 1.0,
            "price_score": 8.33966,
            "raw_score": 7.737553,
            "score_cap": 8.0,
            "caps": ["evidence_cap=8.0"],
            "board": "SZ_MAIN",
            "retrieved_at": "2026-07-26T15:36:14Z",
            "as_of_session": "2026-07-24",
            "source_url": "https://push2delay.eastmoney.com/api/qt/clist/get",
            "source_updated_at": "2026-07-24T07:34:12Z",
        },
    }


def test_independent_type5_market_replay_uses_unrounded_relative_context():
    record = _type5_market_record_002522_rounding_boundary()
    components = record["components"]
    rounded_rank_replay = sum(
        (0.8 * components["absolute"][metric] + 0.2 * components["relative"][metric]) * components["weights"][metric]
        for metric in components["metric_scores"]
    )

    # Replaying only the already-rounded rank/metric ledger crosses the sixth
    # decimal. The count ledger is the lossless source of the published score.
    assert round(rounded_rank_replay, 6) == 7.737554
    assert components["raw_score"] == 7.737553
    assert _audit_type5_market_replay(record, code="002522", as_of="2026-07-24") == 7.7


@pytest.mark.parametrize(
    "mutation",
    (
        lambda record: record["components"].pop("relative_context"),
        lambda record: record["components"]["relative_context"]["change_60d_pct"].update(lower_count=1508),
        lambda record: record["components"].update(schema_version=2),
        lambda record: record["components"].update(model_id="patch6-type2c-quantity-price-v2"),
        lambda record: record["components"].update(code="002523"),
        lambda record: record["components"].update(source="forged source"),
        lambda record: record["evidence"].update(source="forged source"),
    ),
)
def test_independent_type5_market_replay_rejects_context_and_identity_tampering(mutation):
    record = _type5_market_record_002522_rounding_boundary()
    mutation(record)

    assert _audit_type5_market_replay(record, code="002522", as_of="2026-07-24") is None


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.pop("bottom_evidence_contract"),
        lambda payload: payload["bottom_evidence_contract"]["valuation_history"].update(pb_percentile=0.09),
        lambda payload: payload["bottom_evidence_contract"]["valuation_history"].pop("pb_distribution"),
        lambda payload: payload["bottom_evidence_contract"]["market_coldness_record"]["components"][
            "raw_values"
        ].update(change_60d_pct=-5.0),
        lambda payload: payload["bottom_evidence_contract"]["financial_cycle"]["gross_margin_history"].pop(),
        lambda payload: payload["sub_scores"].update({"5b": 9.5}),
        lambda payload: payload["reasons"].update({"5b": "PB8%/0.95;冷9;毛10"}),
    ),
)
def test_independent_type5_audit_replays_automatic_bottom_raw_contract(mutation):
    row = {"source_trade_date": "2026-07-15", "pb": 0.95}
    payload = _replayable_type5_payload()
    assert _audit_type5_bottom_evidence_errors("000001", row, payload) == []

    mutation(payload)

    assert _audit_type5_bottom_evidence_errors("000001", row, payload)


@pytest.mark.parametrize("mode", ("trusted_external", "incomplete", "not_applicable"))
def test_independent_type5_audit_forbids_contracts_outside_automatic_path(mode):
    row = {"source_trade_date": "2026-07-15", "pb": 0.95}
    payload = _replayable_type5_payload()
    payload["bottom_evidence_mode"] = mode
    if mode == "not_applicable":
        payload["status"] = "not_applicable"

    assert _audit_type5_bottom_evidence_errors("000001", row, payload)


@pytest.mark.parametrize(
    ("mode", "status"),
    (
        ("trusted_external", "observe"),
        ("incomplete", "observe"),
        ("not_applicable", "not_applicable"),
    ),
)
def test_independent_type5_audit_does_not_require_automatic_contract_on_other_paths(mode, status):
    row = {"source_trade_date": "2026-07-15", "pb": 0.95}
    payload = _replayable_type5_payload()
    payload.pop("bottom_evidence_contract")
    payload["bottom_evidence_mode"] = mode
    payload["status"] = status

    assert _audit_type5_bottom_evidence_errors("000001", row, payload) == []


@pytest.mark.parametrize(
    "mutation",
    (
        lambda valuation: valuation.update(pe_observations=799),
        lambda valuation: valuation.update(median_pb_mrq=7.1),
        lambda valuation: valuation.update(pe_percentile=0.11),
        lambda valuation: valuation.pop("pe_distribution"),
        lambda valuation: valuation.pop("pb_distribution"),
    ),
)
def test_independent_type7_audit_replays_raw_valuation_distributions(mutation):
    outcome, ledger = _replayable_type7_ledger()
    status = outcome[3]["_status"]
    assert _audit_type7_ledger("000001", ledger, status) == []
    forged = deepcopy(ledger)
    shareholder_input = next(item for item in forged["template1"]["items"] if item["key"] == "t1_19")["inputs"][
        "shareholder_return"
    ]
    valuation = shareholder_input["valuation_history_contract"]

    mutation(valuation)

    assert any("raw market history replay mismatch" in error for error in _audit_type7_ledger("000001", forged, status))


def test_independent_audit_binds_type7_valuation_to_the_actual_dcf_partition():
    quotes, financials = _market()
    audit = audit_random_sample(
        quotes,
        financials,
        eligible_codes=financials,
        seed=1,
        sample_size=5,
        max_workers=1,
    )
    code = next(
        code
        for code in sorted(audit.dcf_results)
        if "template1" in audit.scores.loc[audit.scores["code"] == code].iloc[0]["type7"]["ledger"]
    )
    row = audit.scores.loc[audit.scores["code"] == code].iloc[0].to_dict()

    errors = _independent_checks(
        pd.DataFrame([row]),
        (code,),
        {},
        {code: "fixture_removed_dcf"},
        quotes=quotes,
        financials=financials,
        reporting_period_contract=_reporting_period_contract(),
    )

    assert any("valuation prerequisite is not bound to validated DCF" in error for error in errors)


def test_independent_bear_case_rounds_subscores_like_production():
    payload = {
        "sub_scores": {"7a": 5.005, "7b": 5.004, "7c": 5.006},
        "reasons": {"7a": "甲", "7b": "乙", "7c": "丙"},
    }

    assert _audit_bear_case("type7", payload) == _build_bear_case("type7", payload)


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


def test_independent_audit_rejects_valid_valuation_hidden_as_type1_not_applicable():
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    code = next(iter(audit.dcf_results))
    row = deepcopy(audit.scores.loc[audit.scores["code"] == code].iloc[0].to_dict())
    type1 = row["type1"]
    type1.update(
        triggered=False,
        veto=False,
        status="not_applicable",
        applicable=False,
        evidence_complete=True,
    )
    type1["reasons"].pop("_veto", None)
    type1["reasons"].update(_status="not_applicable", _applicable="no", _evidence="complete")

    errors = _single_company_independent_errors(row, audit, quotes, financials)

    assert any("valid valuation cannot be hidden as not applicable" in error for error in errors)


def test_independent_audit_rejects_triggered_type1_after_structured_valuation_skip():
    quotes, financials = _market()
    audit = audit_random_sample(quotes, financials, eligible_codes=financials, seed=1, sample_size=5, max_workers=1)
    code = next(iter(audit.dcf_results))
    row = deepcopy(audit.scores.loc[audit.scores["code"] == code].iloc[0].to_dict())
    type1 = row["type1"]
    type1.update(
        triggered=True,
        total=10.0,
        sub_scores={key: 10.0 for key in TYPE_WEIGHTS["type1"]},
        veto=False,
        status="triggered",
        applicable=True,
        evidence_complete=True,
    )
    type1["reasons"] = {
        **{key: "伪造完整证据" for key in TYPE_WEIGHTS["type1"]},
        "_status": "triggered",
        "_applicable": "yes",
        "_evidence": "complete",
    }

    errors = _independent_checks(
        pd.DataFrame([row]),
        (code,),
        {},
        {code: "fixture_missing"},
        skip_classifications={
            code: {"category": "source_missing", "reason": "fixture_missing"},
        },
        quotes=quotes,
        financials=financials,
        reporting_period_contract=_reporting_period_contract(),
    )

    assert any("skipped valuation must have zero sub-scores" in error for error in errors)
    assert any("skipped valuation cannot trigger" in error for error in errors)


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
