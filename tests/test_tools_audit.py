import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.dcf import ReportingPeriodContract
from tools import run_full_audit


@pytest.fixture(autouse=True)
def _isolate_market_coldness_session_cache(monkeypatch):
    monkeypatch.setattr(run_full_audit, "load_market_coldness_session_snapshot", lambda _session: None)
    monkeypatch.setattr(
        run_full_audit,
        "archive_market_coldness_session_snapshot",
        lambda snapshot, _session: snapshot,
    )


def _reporting_period_contract_payload():
    return {
        "annual_report_date": "2025-12-31",
        "current_interim_report_date": "2026-03-31",
        "prior_interim_report_date": "2025-03-31",
        "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
    }


@pytest.mark.parametrize(
    ("retrieved_at", "expected"),
    [
        ("2026-07-23T15:15:00+08:00", None),
        ("2026-07-23T15:30:00+08:00", None),
        ("2026-07-23T16:14:59+08:00", None),
        ("2026-07-23T16:15:00+08:00", date(2026, 7, 23)),
        ("2026-07-25T12:00:00+08:00", date(2026, 7, 24)),
    ],
)
def test_release_completed_session_independently_enforces_the_safe_close_boundary(retrieved_at, expected):
    assert run_full_audit._release_market_coldness_completed_session(datetime.fromisoformat(retrieved_at)) == expected


def _market_coldness_record(
    code: str,
    *,
    as_of_session: str = "2026-07-16",
    retrieved_at: str = "2026-07-16T08:20:00Z",
    raw_values=None,
    relative=None,
    source_updated_at=None,
):
    from datetime import date

    values = raw_values or {
        "change_60d_pct": -12.0,
        "change_ytd_pct": -8.0,
        "turnover_rate_pct": 1.0,
        "volume_ratio": 0.8,
    }
    relative = relative or {}
    available = [key for key in run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS if values[key] is not None]
    absolute = {
        key: run_full_audit._market_coldness_interpolate(
            values[key], run_full_audit._MARKET_COLDNESS_ABSOLUTE_BANDS[key]
        )
        for key in available
    }
    metric_scores = {
        key: 0.8 * absolute[key] + 0.2 * relative[key] if key in relative else absolute[key] for key in available
    }
    session = date.fromisoformat(as_of_session)
    reliability = min(1.0, run_full_audit._market_coldness_business_days_ytd(session) / 60.0)
    weights = dict(run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS)
    weights["change_ytd_pct"] *= reliability
    raw_score = sum(metric_scores[key] * weights[key] for key in available) / sum(weights[key] for key in available)
    price_score = (
        metric_scores["change_60d_pct"] * weights["change_60d_pct"]
        + metric_scores["change_ytd_pct"] * weights["change_ytd_pct"]
    ) / (weights["change_60d_pct"] + weights["change_ytd_pct"])
    cap = 8.0 if values["volume_ratio"] is not None else 7.5
    caps = [f"evidence_cap={cap:.1f}"]
    if absolute["change_60d_pct"] <= 3.0:
        cap = min(cap, 3.0)
        caps.append("60d_hot_cap=3.0")
    elif price_score < 5.0:
        cap = min(cap, 4.9)
        caps.append("price_coldness_lt5_cap=4.9")
    elif price_score < 6.0:
        cap = min(cap, 6.9)
        caps.append("price_coldness_lt6_cap=6.9")
    score = round(max(1.0, min(cap, raw_score)), 1)
    volume_text = f"{values['volume_ratio']:.2f}" if values["volume_ratio"] is not None else "缺失"
    summary = (
        f"量价冷度;60日{values['change_60d_pct']:.1f}%;YTD{values['change_ytd_pct']:.1f}%;"
        f"换手{values['turnover_rate_pct']:.2f}%;量比{volume_text};上限{cap:.1f}"
    )
    return {
        "market_coldness_score": score,
        "market_coldness_score_evidence": {
            "source": f"{run_full_audit.EASTMONEY_SOURCE}; {run_full_audit.EASTMONEY_CLIST_ENDPOINT}",
            "evidence_id": f"patch6-type2c-quantity-price-v1:{code}:{as_of_session.replace('-', '')}",
            "as_of": as_of_session,
            "summary": summary,
        },
        "components": {
            "raw_values": values,
            "absolute": {key: round(value, 6) for key, value in absolute.items()},
            "relative": {key: round(value, 6) for key, value in relative.items()},
            "relative_sample_sizes": {key: 1000 for key in relative},
            "metric_scores": {key: round(value, 6) for key, value in metric_scores.items()},
            "weights": {key: round(weights[key], 6) for key in available},
            "ytd_reliability": round(reliability, 6),
            "price_score": round(price_score, 6),
            "raw_score": round(raw_score, 6),
            "score_cap": cap,
            "caps": caps,
            "board": run_full_audit._market_coldness_board(code),
            "as_of_session": as_of_session,
            "source_url": run_full_audit.EASTMONEY_CLIST_ENDPOINT,
            "retrieved_at": retrieved_at,
            "source_updated_at": source_updated_at or f"{as_of_session}T07:34:00Z",
        },
    }


def _market_coldness_status(
    eligible_codes,
    evidence_codes=None,
    *,
    as_of_session="2026-07-16",
    retrieved_at="2026-07-16T08:20:00Z",
    not_applicable=None,
    data_gaps=None,
):
    eligible = set(eligible_codes)
    evidence = set(evidence_codes if evidence_codes is not None else eligible_codes)
    not_applicable = not_applicable or {}
    data_gaps = data_gaps or {}
    na_ledger = {
        reason: sorted(not_applicable.get(reason, ()))
        for reason in sorted(run_full_audit._MARKET_COLDNESS_NOT_APPLICABLE_REASONS)
    }
    gap_ledger = {reason: sorted(codes) for reason, codes in sorted(data_gaps.items())}
    na_codes = set().union(*(set(codes) for codes in na_ledger.values()))
    gap_codes = set().union(*(set(codes) for codes in gap_ledger.values())) if gap_ledger else set()
    applicable = eligible - na_codes
    return {
        "available": True,
        "evidence_available": bool(evidence),
        "evidence_reason": "available" if not gap_codes else "eligible_data_gaps",
        "model_id": run_full_audit.MARKET_COLDNESS_MODEL_ID,
        "source": run_full_audit.EASTMONEY_SOURCE,
        "source_url": run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        "retrieved_at": retrieved_at,
        "as_of_session": as_of_session,
        "eligible_evidence_count": len(evidence),
        "eligible_evidence_coverage": len(evidence) / len(eligible),
        "eligible_applicable_count": len(applicable),
        "eligible_applicable_evidence_coverage": len(evidence) / len(applicable) if applicable else 0.0,
        "eligible_not_applicable_count": len(na_codes),
        "eligible_not_applicable_codes_by_reason": na_ledger,
        "eligible_unscored_data_gap_count": len(gap_codes),
        "eligible_unscored_data_gap_codes_by_reason": gap_ledger,
    }


def _market_coldness_reference_artifact(
    listed_codes,
    *,
    as_of_session="2026-07-16",
    retrieved_at="2026-07-16T08:20:00Z",
    current_year=(),
    recent=(),
    zero_turnover=(),
    raw_values=None,
    source_updated_at=None,
):
    complete_listed = set(listed_codes)
    candidate = 600000
    while len(complete_listed) < run_full_audit.MIN_CROSS_SECTION_RECORDS + 100:
        complete_listed.add(f"{candidate:06d}")
        candidate += 1
    values = raw_values or {
        "change_60d_pct": -12.0,
        "change_ytd_pct": -8.0,
        "turnover_rate_pct": 1.0,
        "volume_ratio": 0.8,
    }
    rows = []
    source_update_epoch = int(
        datetime.fromisoformat((source_updated_at or f"{as_of_session}T07:34:00Z").replace("Z", "+00:00")).timestamp()
    )
    for code in sorted(complete_listed):
        listing_date = (
            "2026-01-02" if code in set(current_year) else "2026-04-01" if code in set(recent) else "2000-01-01"
        )
        turnover = 0.0 if code in set(zero_turnover) else values["turnover_rate_pct"]
        rows.append(
            [
                code,
                listing_date,
                values["change_60d_pct"],
                values["change_ytd_pct"],
                turnover,
                values["volume_ratio"],
                source_update_epoch,
            ]
        )
    return {
        "schema_version": run_full_audit._MARKET_COLDNESS_REFERENCE_ARTIFACT_SCHEMA_VERSION,
        "model_id": run_full_audit.MARKET_COLDNESS_MODEL_ID,
        "source": run_full_audit.EASTMONEY_SOURCE,
        "source_url": run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        "retrieved_at": retrieved_at,
        "as_of_session": as_of_session,
        "listed_codes": sorted(complete_listed),
        "source_record_count": len(rows),
        "records": rows,
    }


def _status_from_reference_artifact(artifact, eligible_codes):
    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible_codes,
        as_of_session=artifact["as_of_session"],
    )
    evidence = replay["eligible_evidence"]
    status = _market_coldness_status(
        eligible_codes,
        evidence,
        as_of_session=artifact["as_of_session"],
        retrieved_at=artifact["retrieved_at"],
        not_applicable=replay["eligible_not_applicable_codes_by_reason"],
        data_gaps=replay["eligible_unscored_data_gap_codes_by_reason"],
    )
    status["full_listed_evidence_count"] = len(replay["full_evidence"])
    status["reference_artifact_sha256"] = hashlib.sha256(
        run_full_audit._canonical_market_coldness_json(artifact)
    ).hexdigest()
    return evidence, status


def _builder_diagnostics(listed_codes, evidence_codes, *, not_applicable=None, data_gaps=None):
    not_applicable = not_applicable or {}
    data_gaps = data_gaps or {}
    reasons = {}
    for reason in (
        "listing_history_lt_120_days",
        "listed_in_current_year",
        "missing_listing_date",
        "missing_required_metric",
        "missing_source_record",
        "insufficient_reference_cross_section",
    ):
        codes = sorted(
            (not_applicable if reason in run_full_audit._MARKET_COLDNESS_NOT_APPLICABLE_REASONS else data_gaps).get(
                reason, ()
            )
        )
        reasons[reason] = {
            "classification": (
                "model_not_applicable"
                if reason in run_full_audit._MARKET_COLDNESS_NOT_APPLICABLE_REASONS
                else "data_missing"
            ),
            "count": len(codes),
            "codes": codes,
        }
    return {
        "evidence_available": bool(evidence_codes),
        "evidence_reason": "available" if evidence_codes else "no_eligible_records",
        "diagnostics_schema_version": run_full_audit.MARKET_COLDNESS_DIAGNOSTICS_SCHEMA_VERSION,
        "eligible_candidate_count": len(tuple(listed_codes)),
        "evidence_count": len(tuple(evidence_codes)),
        "unscored_codes_by_reason": reasons,
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
        quotes=pd.DataFrame(
            [
                {"code": "000001", "market": "SZ"},
                {"code": "000002", "market": "SZ"},
            ]
        ),
        validation={
            "trading_source_trade_dates": ["2026-07-15"],
            "analysis_market_codes": ["000001", "000002"],
        },
    )
    coverage = SimpleNamespace(to_dict=lambda: {"total_records": 1})
    source_snapshot = SimpleNamespace(
        available=True,
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-15T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=True,
        cache_diagnostic="hit",
        reason="complete",
    )
    calls = []

    def fake_fetch(*, force_refresh, allow_expired_cache):
        calls.append(("fetch", force_refresh, allow_expired_cache))
        return source_snapshot

    evidence = {
        "000001": {"market_coldness_score": 6.0},
        "000002": {"market_coldness_score": 5.0},
    }

    def fake_build(value, *, as_of_session, listed_quote_codes, diagnostics):
        calls.append(("build", value, as_of_session, listed_quote_codes))
        diagnostics.update(_builder_diagnostics(listed_quote_codes, evidence))
        return evidence

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    actual, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=False,
    )

    assert actual == {"000001": {"market_coldness_score": 6.0}}
    assert calls == [
        ("fetch", False, True),
        ("build", source_snapshot, "2026-07-15", ("000001", "000002")),
    ]
    assert status["available"] is True
    assert status["evidence_available"] is True
    assert status["source"] == run_full_audit.EASTMONEY_SOURCE
    assert status["eligible_evidence_coverage"] == 1.0
    assert status["full_listed_evidence_count"] == 2
    assert status["excluded_noneligible_evidence_count"] == 1
    assert status["excluded_noneligible_evidence_codes"] == ["000002"]


def test_market_coldness_loader_rejects_builder_identity_outside_quote_boundary(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-16"], "analysis_market_codes": ["000001"]},
    )
    source_snapshot = SimpleNamespace(
        available=True,
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-16T08:20:00Z",
        fetched_count=1,
        total_expected=1,
        coverage=SimpleNamespace(to_dict=lambda: {"total_records": 1}),
        cache_hit=False,
        cache_diagnostic="test",
        reason="",
    )
    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", lambda **_kwargs: source_snapshot)
    monkeypatch.setattr(
        run_full_audit,
        "build_market_coldness_evidence",
        lambda *_args, **_kwargs: {"999999": {"market_coldness_score": 8.0}},
    )

    evidence, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=False,
    )

    assert evidence == {}
    assert status["available"] is False
    assert status["evidence_reason"] == "validation_or_acquisition_error"
    assert "outside the listed quote boundary" in status["reason"]


def test_fresh_publication_refetches_when_cached_coldness_belongs_to_another_session(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-16"], "analysis_market_codes": ["000001"]},
    )
    coverage = SimpleNamespace(to_dict=lambda: {"total_records": 1})
    stale = SimpleNamespace(
        available=True,
        source="old",
        source_url="https://example.test/old",
        retrieved_at="2026-07-15T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=True,
        cache_diagnostic="expired_hit",
        reason="",
    )
    fresh = SimpleNamespace(
        available=True,
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-16T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=False,
        cache_diagnostic="forced_refresh",
        reason="",
    )
    calls = []

    def fake_fetch(*, force_refresh, allow_expired_cache):
        calls.append(("fetch", force_refresh, allow_expired_cache))
        return fresh if force_refresh else stale

    def fake_build(value, *, as_of_session, listed_quote_codes, diagnostics):
        calls.append(("build", value.source, as_of_session, listed_quote_codes))
        if value is stale:
            diagnostics.update(
                {
                    "evidence_available": False,
                    "evidence_reason": "session_retrieval_mismatch",
                    "retrieval_session": "2026-07-15",
                    "requested_session": "2026-07-16",
                }
            )
            return {}
        result = {"000001": {"market_coldness_score": 6.0}}
        diagnostics.update(_builder_diagnostics(listed_quote_codes, result))
        return result

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    evidence, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=True,
    )

    assert evidence == {"000001": {"market_coldness_score": 6.0}}
    assert status["source"] == run_full_audit.EASTMONEY_SOURCE
    assert calls == [
        ("fetch", False, True),
        ("build", "old", "2026-07-16", ("000001",)),
        ("fetch", True, False),
        ("build", run_full_audit.EASTMONEY_SOURCE, "2026-07-16", ("000001",)),
    ]


def test_fresh_publication_does_not_refetch_coldness_that_is_newer_than_quotes(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-16"], "analysis_market_codes": ["000001"]},
    )
    newer = SimpleNamespace(
        available=True,
        source="newer",
        source_url="https://example.test/newer",
        retrieved_at="2026-07-17T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=SimpleNamespace(to_dict=lambda: {"total_records": 1}),
        cache_hit=False,
        cache_diagnostic="fresh",
        reason="",
    )
    fetch_calls = []

    def fake_fetch(*, force_refresh, allow_expired_cache):
        fetch_calls.append((force_refresh, allow_expired_cache))
        return newer

    def fake_build(_value, *, diagnostics, **_kwargs):
        diagnostics.update(
            {
                "evidence_available": False,
                "evidence_reason": "session_retrieval_mismatch",
                "retrieval_session": "2026-07-17",
                "requested_session": "2026-07-16",
            }
        )
        return {}

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    evidence, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=True,
    )

    assert evidence == {}
    assert status["evidence_reason"] == "session_retrieval_mismatch"
    assert fetch_calls == [(False, True)]


def test_release_market_coldness_gate_requires_complete_applicable_coverage():
    eligible = tuple(f"{index:06d}" for index in range(100))
    artifact = _market_coldness_reference_artifact(
        eligible,
        current_year=(eligible[-1],),
    )
    evidence, status = _status_from_reference_artifact(artifact, eligible)

    assert (
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )
        == 0.99
    )

    evidence = dict(evidence)
    evidence.pop(eligible[98])
    status = _market_coldness_status(
        eligible,
        evidence,
        not_applicable={"listed_in_current_year": [eligible[-1]]},
        data_gaps={"missing_required_metric": [eligible[98]]},
    )
    with pytest.raises(RuntimeError, match="unexplained eligible data gaps"):
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_coordinated_not_applicable_forgery():
    eligible = tuple(f"{index:06d}" for index in range(100))
    artifact = _market_coldness_reference_artifact(eligible)
    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible,
        as_of_session="2026-07-16",
    )
    evidence = {eligible[0]: replay["eligible_evidence"][eligible[0]]}
    status = _market_coldness_status(
        eligible,
        evidence,
        not_applicable={"listed_in_current_year": eligible[1:]},
    )
    status["full_listed_evidence_count"] = len(replay["full_evidence"])
    status["reference_artifact_sha256"] = hashlib.sha256(
        run_full_audit._canonical_market_coldness_json(artifact)
    ).hexdigest()

    with pytest.raises(RuntimeError, match="applicability ledger differs from raw source evidence"):
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )


def test_market_coldness_replay_scores_numeric_zero_turnover_instead_of_marking_it_not_applicable():
    eligible = ("600001",)
    artifact = _market_coldness_reference_artifact(eligible, zero_turnover=eligible)

    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible,
        as_of_session="2026-07-16",
    )

    evidence = replay["eligible_evidence"]["600001"]
    assert evidence["components"]["raw_values"]["turnover_rate_pct"] == 0.0
    assert replay["eligible_not_applicable_codes_by_reason"] == {
        "listed_in_current_year": [],
        "listing_history_lt_120_days": [],
    }
    rebuilt_evidence, status = _status_from_reference_artifact(artifact, eligible)
    assert rebuilt_evidence == replay["eligible_evidence"]
    assert (
        run_full_audit._require_market_coldness_release_evidence(
            replay["eligible_evidence"],
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )
        == 1.0
    )


def test_release_market_coldness_gate_rebuilds_cross_sectional_relative_ranks():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    eligible = (listed[0],)
    artifact = _market_coldness_reference_artifact(listed)
    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible,
        as_of_session="2026-07-16",
    )
    forged = _market_coldness_record(
        listed[0],
        relative={metric: 9.0 for metric in run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS},
    )
    status = _market_coldness_status(eligible)
    status["full_listed_evidence_count"] = len(replay["full_evidence"])
    status["reference_artifact_sha256"] = hashlib.sha256(
        run_full_audit._canonical_market_coldness_json(artifact)
    ).hexdigest()

    with pytest.raises(RuntimeError, match="differs from independent full-universe replay"):
        run_full_audit._require_market_coldness_release_evidence(
            {listed[0]: forged},
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_preclose_reference_batch():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    artifact = _market_coldness_reference_artifact(
        listed,
        retrieved_at="2026-07-16T01:00:00Z",
    )
    with pytest.raises(RuntimeError, match="retrieval timestamp is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {listed[0]: _market_coldness_record(listed[0], retrieved_at="2026-07-16T01:00:00Z")},
            _market_coldness_status(
                (listed[0],),
                retrieved_at="2026-07-16T01:00:00Z",
            ),
            reference_artifact=artifact,
            eligible_codes=(listed[0],),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_accepts_next_day_preopen_previous_close_batch():
    eligible = ("600000",)
    artifact = _market_coldness_reference_artifact(
        eligible,
        as_of_session="2026-07-22",
        retrieved_at="2026-07-22T18:11:06Z",
    )
    evidence, status = _status_from_reference_artifact(artifact, eligible)

    assert (
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-22",
        )
        == 1.0
    )


def test_release_market_coldness_replay_rejects_stale_source_update_dates():
    artifact = _market_coldness_reference_artifact(
        ("600000",),
        as_of_session="2026-07-22",
        retrieved_at="2026-07-22T18:11:06Z",
        source_updated_at="2026-07-21T07:34:00Z",
    )

    with pytest.raises(RuntimeError, match="source row belongs to another session"):
        run_full_audit._replay_market_coldness_reference_artifact(
            artifact,
            eligible_codes=("600000",),
            as_of_session="2026-07-22",
        )


def test_release_market_coldness_gate_rejects_previous_close_after_next_auction_starts():
    eligible = ("600000",)
    artifact = _market_coldness_reference_artifact(
        eligible,
        as_of_session="2026-07-22",
        retrieved_at="2026-07-23T01:15:00Z",
    )
    evidence = {
        eligible[0]: _market_coldness_record(
            eligible[0],
            as_of_session="2026-07-22",
            retrieved_at="2026-07-23T01:15:00Z",
        )
    }
    status = _market_coldness_status(
        eligible,
        as_of_session="2026-07-22",
        retrieved_at="2026-07-23T01:15:00Z",
    )

    with pytest.raises(RuntimeError, match="retrieval timestamp is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-22",
        )


def test_market_coldness_replay_accepts_complete_source_rows_outside_the_listed_boundary():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    artifact = _market_coldness_reference_artifact(listed)
    baseline = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=(listed[0],),
        as_of_session="2026-07-16",
    )
    artifact["records"].append(
        [
            "300000",
            "2000-01-01",
            -90.0,
            -90.0,
            99.0,
            99.0,
            int(datetime.fromisoformat("2026-07-16T07:34:00+00:00").timestamp()),
        ]
    )
    artifact["records"].sort(key=lambda row: row[0])
    artifact["source_record_count"] += 1

    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=(listed[0],),
        as_of_session="2026-07-16",
    )

    assert replay["full_evidence"] == baseline["full_evidence"]
    assert replay["eligible_evidence"] == baseline["eligible_evidence"]


def test_market_coldness_replay_rejects_an_extra_row_masking_a_missing_listed_company():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    artifact = _market_coldness_reference_artifact(listed)
    missing_code = artifact["listed_codes"][0]
    artifact["records"] = [row for row in artifact["records"] if row[0] != missing_code]
    artifact["records"].append(
        [
            "688888",
            "2000-01-01",
            -12.0,
            -8.0,
            1.0,
            0.8,
            int(datetime.fromisoformat("2026-07-16T07:34:00+00:00").timestamp()),
        ]
    )
    artifact["records"].sort(key=lambda row: row[0])

    with pytest.raises(RuntimeError, match="source does not cover the listed universe"):
        run_full_audit._replay_market_coldness_reference_artifact(
            artifact,
            eligible_codes=(listed[0],),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_zero_or_inconsistent_evidence():
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-16",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        run_full_audit._require_market_coldness_release_evidence(
            {},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    status.update(evidence_available=True, eligible_evidence_count=1, eligible_evidence_coverage=1.0)
    with pytest.raises(RuntimeError, match="count or coverage is inconsistent"):
        run_full_audit._require_market_coldness_release_evidence(
            {},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    with pytest.raises(RuntimeError, match="unknown or duplicate identities"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": {}, "999999": {}},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({}, "score is invalid"),
        (None, "record is invalid"),
        (
            {
                **_market_coldness_record("000001"),
                "market_coldness_score": float("nan"),
            },
            "score is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "market_coldness_score_evidence": {
                    **_market_coldness_record("000001")["market_coldness_score_evidence"],
                    "as_of": "2026-07-15",
                },
            },
            "score provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "market_coldness_score_evidence": {
                    **_market_coldness_record("000001")["market_coldness_score_evidence"],
                    "evidence_id": "patch6-type2c-quantity-price-v1:000001:20260715",
                },
            },
            "score provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "retrieved_at": "2026-07-15T08:20:00Z",
                },
            },
            "component provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    key: value
                    for key, value in _market_coldness_record("000001")["components"].items()
                    if key != "raw_values"
                },
            },
            "component provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": None, "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -12.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": True, "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": float("nan"), "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -12.0, "change_ytd_pct": float("inf")},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -100.1, "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -12.0, "change_ytd_pct": 10_000.1},
                },
            },
            "raw evidence is invalid",
        ),
    ],
)
def test_release_market_coldness_gate_rejects_invalid_record_content(record, message):
    status = _market_coldness_status(("000001",))

    with pytest.raises(RuntimeError, match=message):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_independently_replays_hot_market_and_missing_volume_caps():
    hot = _market_coldness_record(
        "000001",
        raw_values={
            "change_60d_pct": 500.0,
            "change_ytd_pct": 500.0,
            "turnover_rate_pct": 20.0,
            "volume_ratio": 3.0,
        },
    )
    hot["market_coldness_score"] = 8.0
    with pytest.raises(RuntimeError, match="cap or final score replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": hot},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    without_volume = _market_coldness_record(
        "000001",
        raw_values={
            "change_60d_pct": -30.0,
            "change_ytd_pct": -35.0,
            "turnover_rate_pct": 0.5,
            "volume_ratio": None,
        },
    )
    assert without_volume["market_coldness_score"] <= 7.5
    without_volume["market_coldness_score"] = 8.0
    with pytest.raises(RuntimeError, match="cap or final score replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": without_volume},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_missing_activity_and_forged_arithmetic():
    missing_turnover = _market_coldness_record("000001")
    missing_turnover["components"]["raw_values"]["turnover_rate_pct"] = None
    with pytest.raises(RuntimeError, match="raw evidence is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": missing_turnover},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    forged = _market_coldness_record("000001")
    forged["components"]["metric_scores"]["change_60d_pct"] += 1.0
    with pytest.raises(RuntimeError, match="metric score replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": forged},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


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
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-15"], "analysis_market_codes": ["000001"]},
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


@pytest.mark.parametrize("stdout_encoding", ["cp1252", "gbk"])
def test_cached_full_audit_uses_active_quality_as_regression_baseline(monkeypatch, tmp_path, stdout_encoding):
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

    def fake_cache(*_args, **kwargs):
        calls["cache_kwargs"] = kwargs
        return FakeCache()

    monkeypatch.setattr(run_full_audit, "SafeFileCache", fake_cache)
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    coldness_evidence = {
        "000001": _market_coldness_record(
            "000001",
            as_of_session="2026-07-15",
            retrieved_at="2026-07-15T08:20:00Z",
        )
    }
    coldness_status = _market_coldness_status(
        ("000001",),
        as_of_session="2026-07-15",
        retrieved_at="2026-07-15T08:20:00Z",
    )

    archive_candidate = SimpleNamespace(available=True)

    def fake_coldness(
        snapshot_arg,
        eligible_arg,
        *,
        force_refresh,
        reference_artifact_out,
        archive_candidate_out,
    ):
        assert snapshot_arg is snapshot
        assert tuple(eligible_arg) == ("000001",)
        assert force_refresh is False
        reference_artifact_out.update({"fixture": True})
        archive_candidate_out.append(archive_candidate)
        return coldness_evidence, coldness_status

    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", fake_coldness)

    def fake_coldness_gate(*_args, **_kwargs):
        calls["coldness_gate_passed"] = True
        return 1.0

    def fake_coldness_archive(candidate, session):
        assert calls.get("coldness_gate_passed") is True
        assert candidate is archive_candidate
        assert session == "2026-07-15"
        calls["coldness_archived"] = True
        return candidate

    monkeypatch.setattr(run_full_audit, "_require_market_coldness_release_evidence", fake_coldness_gate)
    monkeypatch.setattr(run_full_audit, "archive_market_coldness_session_snapshot", fake_coldness_archive)

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
        lambda *_args, **_kwargs: {"json": Path("审计😀.json")},
    )
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding=stdout_encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    result = run_full_audit.main(["--sample-size", "1", "--output-dir", str(tmp_path / "audit")])

    assert result == 0
    assert calls["cache_kwargs"]["ttl"] == run_full_audit.MAX_STALE_AGE_SECONDS
    assert calls["analysis_kwargs"]["previous_quality"] == active_quality
    assert calls["analysis_kwargs"]["enforce_quality"] is True
    assert calls["analysis_kwargs"]["expected_companies"] == 1
    expected_contract = ReportingPeriodContract("2025-12-31", "2026-03-31", "2025-03-31")
    assert calls["analysis_kwargs"]["reporting_period_contract"] == expected_contract
    assert calls["analysis_kwargs"]["market_coldness_evidence"] is coldness_evidence
    assert calls["coldness_archived"] is True
    assert calls["analysis_kwargs"]["quality_history_loader"] is run_full_audit.fetch_quality_history_batch
    assert calls["analysis_kwargs"]["research_report_loader"] is run_full_audit.fetch_research_reports_batch
    assert calls["audit_kwargs"]["provenance"]["full_market_quality"] == quality
    assert calls["audit_kwargs"]["provenance"]["market_coldness"] == coldness_status
    assert calls["audit_kwargs"]["full_market_analysis"] is analysis
    assert calls["audit_kwargs"]["reporting_period_contract"] == expected_contract
    assert calls["audit_kwargs"]["market_coldness_evidence"] is coldness_evidence
    assert calls["audit_kwargs"]["quality_history_evidence"] == {}
    assert calls["audit_kwargs"]["research_report_evidence"] == {}
    assert len(calls["audit_kwargs"]["snapshot_sha256"]) == 64
    stdout.flush()
    output = json.loads(stdout_bytes.getvalue().decode(stdout_encoding))
    assert output["refresh_requested"] is False
    assert output["refresh_completed"] is True
    assert output["snapshot_source"] == "cache"
    assert output["snapshot_warning"] == ""
    assert output["market_coldness"]["eligible_evidence_coverage"] == 1.0
    assert output["artifacts"]["json"] == "审计😀.json"

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


def test_fresh_full_audit_stops_before_analysis_when_coldness_coverage_is_zero(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(
        source="network",
        eligible_codes=("000001",),
        validation={
            "trading_source_trade_dates": ["2026-07-16"],
            "reporting_period_contract": _reporting_period_contract_payload(),
        },
    )
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-16",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", lambda *_args, **_kwargs: ({}, status))
    monkeypatch.setattr(
        run_full_audit,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )
    monkeypatch.setattr(
        run_full_audit,
        "save_market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot must not be saved")),
    )

    output_dir = tmp_path / "audit"
    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        run_full_audit.main(["--refresh", "--output-dir", str(output_dir)])
    assert not output_dir.exists()


def test_cached_full_audit_also_stops_before_analysis_when_coldness_is_unbound(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(
        source="cache",
        eligible_codes=("000001",),
        validation={
            "trading_source_trade_dates": ["2026-07-16"],
            "reporting_period_contract": _reporting_period_contract_payload(),
        },
    )
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-16",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    output_dir = tmp_path / "audit"
    output_dir.mkdir()
    marker = output_dir / "existing.json"
    marker.write_text("last-known-good", encoding="utf-8")
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", lambda *_args, **_kwargs: ({}, status))
    monkeypatch.setattr(
        run_full_audit,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )

    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        run_full_audit.main(["--output-dir", str(output_dir)])

    assert marker.read_text(encoding="utf-8") == "last-known-good"


def test_forced_full_audit_preserves_existing_artifacts_when_quotes_fall_back_to_cache(monkeypatch, tmp_path):
    output_dir = tmp_path / "audit"
    output_dir.mkdir()
    original = {}
    for suffix, content in (("json", b"old-json"), ("csv", b"old-csv"), ("md", b"old-markdown")):
        path = output_dir / f"random100_audit_seed20260715.{suffix}"
        path.write_bytes(content)
        original[path] = content

    source_warning = "refresh failed: DataFetchError: Eastmoney page 3 timed out after 3 attempts"
    snapshot = SimpleNamespace(source="stale_cache", warning=source_warning)
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        run_full_audit,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )
    monkeypatch.setattr(
        run_full_audit,
        "save_market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot must not be saved")),
    )
    monkeypatch.setattr(
        run_full_audit,
        "write_audit_artifacts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("audit artifacts must not be written")),
    )

    with pytest.raises(RuntimeError, match="existing audit artifacts were preserved") as exc_info:
        run_full_audit.main(["--refresh", "--output-dir", str(output_dir)])
    assert source_warning in str(exc_info.value)
    assert {path: path.read_bytes() for path in original} == original
