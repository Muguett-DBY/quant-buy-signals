from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.dcf import ReportingPeriodContract
from tools import run_full_audit


def _reporting_period_contract_payload():
    return {
        "annual_report_date": "2025-12-31",
        "current_interim_report_date": "2026-03-31",
        "prior_interim_report_date": "2025-03-31",
        "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
    }


def test_comparison_quality_prefers_candidate_baseline_then_active_generation():
    snapshot = SimpleNamespace(
        previous_analysis_quality={"score_rows": 90},
        analysis_quality={"score_rows": 100},
    )
    assert run_full_audit._comparison_quality(snapshot) == {"score_rows": 90}

    snapshot.previous_analysis_quality = {}
    assert run_full_audit._comparison_quality(snapshot) == {"score_rows": 100}

    snapshot.analysis_quality = "invalid"
    assert run_full_audit._comparison_quality(snapshot) is None


def test_requested_refresh_is_successful_only_for_a_network_candidate():
    assert run_full_audit._refresh_completed(False, "cache") is True
    assert run_full_audit._refresh_completed(True, "network") is True
    assert run_full_audit._refresh_completed(True, "cache") is False
    assert run_full_audit._refresh_completed(True, "stale_cache") is False


def test_analysis_coverage_summary_counts_triggers_statuses_and_evidence_levels():
    scores = pd.DataFrame(
        [
            {
                "primary_type": "type2",
                "num_types": 2,
                "type1": {"status": "triggered", "triggered": True},
                "type2": {"status": "triggered", "triggered": True},
                "quantitative_evidence": {
                    "moat_score": {"evidence_level": "derived_proxy"},
                    "technology_score": {"evidence_level": "partial"},
                },
            },
            {
                "primary_type": None,
                "num_types": 0,
                "type1": {"status": "not_applicable", "triggered": False},
                "type2": {"status": "vetoed", "triggered": False},
                "quantitative_evidence": {
                    "moat_score": {"evidence_level": "missing"},
                },
            },
        ]
    )

    summary = run_full_audit._analysis_coverage_summary(scores)

    assert summary["candidate_companies"] == 1
    assert summary["total_framework_triggers"] == 2
    assert summary["framework_trigger_counts"]["type1"] == 1
    assert summary["framework_trigger_counts"]["type2"] == 1
    assert summary["primary_trigger_counts"] == {"type2": 1}
    assert summary["framework_status_counts"]["type1"] == {
        "not_applicable": 1,
        "triggered": 1,
    }
    assert summary["quantitative_evidence_level_counts"] == {
        "derived_proxy": 1,
        "missing": 1,
        "partial": 1,
    }


def test_non_economic_skip_details_lists_every_data_and_model_exception_only():
    details = run_full_audit._non_economic_skip_details(
        {
            "000001": {"category": "economic_not_applicable", "reason": "ttm_fcff_nonpositive"},
            "000002": {"category": "source_missing", "reason": "ttm_fcff_missing_component"},
            "000003": {"category": "inconsistent_source", "reason": "negative_reconstructed_capex"},
            "000004": {"category": "model_unsupported", "reason": "financial_conglomerate"},
        }
    )

    assert details == {
        "inconsistent_source": {"000003": "negative_reconstructed_capex"},
        "model_unsupported": {"000004": "financial_conglomerate"},
        "source_missing": {"000002": "ttm_fcff_missing_component"},
    }


def test_market_coldness_loader_uses_single_validated_trade_date_and_one_bulk_snapshot(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        validation={"trading_source_trade_dates": ["2026-07-15"]},
    )
    coverage = SimpleNamespace(to_dict=lambda: {"total_records": 1})
    source_snapshot = SimpleNamespace(
        available=True,
        source="Eastmoney bulk test",
        source_url="https://example.test/bulk",
        retrieved_at="2026-07-15T08:00:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=True,
        cache_diagnostic="hit",
        reason="complete",
    )
    calls = []

    def fake_fetch(*, force_refresh):
        calls.append(("fetch", force_refresh))
        return source_snapshot

    evidence = {"000001": {"market_coldness_score": 6.0}}

    def fake_build(value, *, as_of_session, diagnostics):
        calls.append(("build", value, as_of_session))
        diagnostics.update({"evidence_available": True, "evidence_reason": "available"})
        return evidence

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    actual, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=False,
    )

    assert actual is evidence
    assert calls == [("fetch", False), ("build", source_snapshot, "2026-07-15")]
    assert status["available"] is True
    assert status["evidence_available"] is True
    assert status["source"] == "Eastmoney bulk test"
    assert status["eligible_evidence_coverage"] == 1.0


def test_market_coldness_unavailable_or_unbound_continues_without_invented_values(monkeypatch):
    no_session = SimpleNamespace(source="cache", validation={"trading_source_trade_dates": []})
    monkeypatch.setattr(
        run_full_audit,
        "fetch_market_coldness_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch without a bound session")),
    )

    evidence, status = run_full_audit._load_market_coldness_evidence(
        no_session,
        ("000001",),
        force_refresh=False,
    )

    assert evidence == {}
    assert status["available"] is False
    assert status["evidence_available"] is False
    assert status["eligible_evidence_count"] == 0
    assert "exactly one" in str(status["reason"])
    assert status["unavailable_policy"] == "continue_with_insufficient_evidence"

    source_unavailable = SimpleNamespace(
        source="cache",
        validation={"trading_source_trade_dates": ["2026-07-15"]},
    )
    unavailable_snapshot = SimpleNamespace(
        available=False,
        source="Eastmoney bulk test",
        source_url="https://example.test/bulk",
        retrieved_at=None,
        fetched_count=0,
        total_expected=None,
        coverage=SimpleNamespace(to_dict=lambda: {"total_records": 0}),
        cache_hit=False,
        cache_diagnostic="miss",
        reason="network unavailable",
    )
    monkeypatch.setattr(
        run_full_audit,
        "fetch_market_coldness_snapshot",
        lambda **_kwargs: unavailable_snapshot,
    )
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", lambda *_args, **_kwargs: {})

    evidence, status = run_full_audit._load_market_coldness_evidence(
        source_unavailable,
        ("000001",),
        force_refresh=False,
    )

    assert evidence == {}
    assert status["available"] is False
    assert status["evidence_reason"] == "source_unavailable"
    assert status["eligible_evidence_coverage"] == 0.0
    assert status["unavailable_policy"] == "continue_with_insufficient_evidence"


def test_cached_full_audit_uses_active_quality_as_regression_baseline(monkeypatch, tmp_path, capsys):
    snapshot_path = tmp_path / "market_snapshot.json.gz"
    snapshot_path.write_bytes(b"validated-snapshot")
    quotes = pd.DataFrame([{"code": "000001"}])
    financials = {"000001": {}}
    active_quality = {"score_rows": 1, "dcf_attempted": 1, "dcf_valid": 1}
    snapshot = SimpleNamespace(
        eligible_codes=("000001",),
        analysis_quotes=quotes,
        analysis_financials=financials,
        previous_analysis_quality={},
        analysis_quality=active_quality,
        source="cache",
        quotes=quotes,
        financials=financials,
        data_timestamp=123.0,
        baseline_payload_sha256="b" * 64,
        validation={
            "eligible_codes": ["000001"],
            "reporting_period_contract": _reporting_period_contract_payload(),
            "trading_source_trade_dates": ["2026-07-15"],
        },
    )
    quality = {
        "score_rows": 1,
        "dcf_attempted": 1,
        "dcf_valid": 1,
        "score_coverage": 1.0,
        "dcf_attempt_coverage": 1.0,
        "dcf_valid_coverage": 1.0,
        "pipeline_issue_rate": 0.0,
    }
    analysis = SimpleNamespace(
        scores=quotes,
        dcf_results={"000001": {}},
        dcf_skipped=0,
        dcf_skip_reasons={},
        issues=(),
        quality=quality,
        quality_history_evidence={},
    )
    calls = {}

    monkeypatch.setattr(run_full_audit, "DEFAULT_SNAPSHOT_PATH", snapshot_path)

    class FakeCache:
        def read_bytes_if_payload(self, expected):
            assert expected == "b" * 64
            return b"validated-snapshot"

    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: FakeCache())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    coldness_evidence = {
        "000001": {
            "market_coldness_score": 6.0,
            "market_coldness_score_evidence": {
                "source": "whole-market-test-source",
                "evidence_id": "coldness:000001:20260715",
                "as_of": "2026-07-15",
            },
        }
    }
    coldness_status = {
        "available": True,
        "evidence_available": True,
        "source": "whole-market-test-source",
        "eligible_evidence_count": 1,
        "eligible_evidence_coverage": 1.0,
    }

    def fake_coldness(snapshot_arg, eligible_arg, *, force_refresh):
        assert snapshot_arg is snapshot
        assert tuple(eligible_arg) == ("000001",)
        assert force_refresh is False
        return coldness_evidence, coldness_status

    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", fake_coldness)

    def fake_analysis(*args, **kwargs):
        calls["analysis_args"] = args
        calls["analysis_kwargs"] = kwargs
        return analysis

    monkeypatch.setattr(run_full_audit, "run_market_analysis", fake_analysis)
    monkeypatch.setattr(
        run_full_audit,
        "save_market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache input must not be promoted")),
    )

    state = {
        "code_sha256": "1" * 64,
        "rules_sha256": "2" * 64,
        "industry_sha256": "3" * 64,
        "dependency_manifest_sha256": "4" * 64,
    }
    monkeypatch.setattr(run_full_audit, "audit_state_hashes", lambda: dict(state))
    audit = SimpleNamespace(
        sample_size=1,
        engine_invariant_errors=(),
        scoring_replay_errors=(),
        valuation_replay_errors=(),
        independent_errors=(),
        invariant_errors=(),
        provenance=dict(state),
    )

    def fake_audit(*args, **kwargs):
        calls["audit_args"] = args
        calls["audit_kwargs"] = kwargs
        return audit

    monkeypatch.setattr(run_full_audit, "audit_random_sample", fake_audit)
    monkeypatch.setattr(
        run_full_audit,
        "write_audit_artifacts",
        lambda *_args, **_kwargs: {"json": Path("audit.json")},
    )

    result = run_full_audit.main(["--sample-size", "1", "--output-dir", str(tmp_path / "audit")])

    assert result == 0
    assert calls["analysis_kwargs"]["previous_quality"] == active_quality
    assert calls["analysis_kwargs"]["enforce_quality"] is True
    assert calls["analysis_kwargs"]["expected_companies"] == 1
    expected_contract = ReportingPeriodContract("2025-12-31", "2026-03-31", "2025-03-31")
    assert calls["analysis_kwargs"]["reporting_period_contract"] == expected_contract
    assert calls["analysis_kwargs"]["market_coldness_evidence"] is coldness_evidence
    assert calls["analysis_kwargs"]["quality_history_loader"] is run_full_audit.fetch_quality_history_batch
    assert calls["audit_kwargs"]["provenance"]["full_market_quality"] == quality
    assert calls["audit_kwargs"]["provenance"]["market_coldness"] == coldness_status
    assert calls["audit_kwargs"]["full_market_analysis"] is analysis
    assert calls["audit_kwargs"]["reporting_period_contract"] == expected_contract
    assert calls["audit_kwargs"]["market_coldness_evidence"] is coldness_evidence
    assert calls["audit_kwargs"]["quality_history_evidence"] == {}
    assert len(calls["audit_kwargs"]["snapshot_sha256"]) == 64
    output = capsys.readouterr().out
    assert '"refresh_requested": false' in output
    assert '"refresh_completed": true' in output
    assert '"snapshot_source": "cache"' in output
    assert '"snapshot_warning": ""' in output
    assert '"market_coldness"' in output
    assert '"eligible_evidence_coverage": 1.0' in output

    analysis.issues = (SimpleNamespace(code="000001", stage="valuation", message="failed"),)
    assert run_full_audit.main(["--sample-size", "1", "--output-dir", str(tmp_path / "audit")]) == 1


def test_full_audit_fails_closed_when_snapshot_has_no_reporting_period_contract(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(validation={}, source="cache")
    monkeypatch.setattr(run_full_audit, "DEFAULT_SNAPSHOT_PATH", tmp_path / "snapshot.json.gz")
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)

    with pytest.raises(RuntimeError, match="no reporting_period_contract"):
        run_full_audit.main([])
