"""Run the production snapshot, full-market analysis, and fixed-seed audit."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from collections import Counter
import hashlib
import json
from pathlib import Path

import pandas as pd

from data.cache import SafeFileCache
from data.fetcher import DataFetcher
from data.market_coldness import fetch_market_coldness_snapshot
from data.quality_history import fetch_quality_history_batch
from data.snapshot import (
    DEFAULT_SNAPSHOT_PATH,
    SNAPSHOT_SCHEMA_VERSION,
    get_market_snapshot,
    save_market_snapshot,
)
from engine.audit import audit_random_sample, audit_state_hashes, write_audit_artifacts
from engine.dcf import ReportingPeriodContract
from engine.market_coldness import build_market_coldness_evidence
from engine.pipeline import run_market_analysis
from engine.valuation_status import DCF_SKIP_ECONOMIC_NOT_APPLICABLE


_STRICT_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_MARKET_COLDNESS_UNAVAILABLE_POLICY = "continue_with_insufficient_evidence"
_TYPE_KEYS = tuple(f"type{index}" for index in range(1, 8))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="fetch a new candidate instead of using a valid cache")
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=Path("audit"))
    return parser


def _comparison_quality(snapshot: object) -> dict | None:
    """Use the last promoted quality generation as the regression baseline."""
    for field in ("previous_analysis_quality", "analysis_quality"):
        value = getattr(snapshot, field, None)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return None


def _refresh_completed(force_refresh: bool, snapshot_source: object) -> bool:
    """A requested refresh succeeds only when the audited candidate is network data."""
    return not force_refresh or snapshot_source == "network"


def _analysis_coverage_summary(scores: pd.DataFrame) -> dict[str, object]:
    """Summarise full-universe triggers, statuses and evidence authority."""
    if not isinstance(scores, pd.DataFrame):
        raise TypeError("analysis scores must be a pandas DataFrame")
    framework_statuses: dict[str, dict[str, int]] = {}
    framework_triggers: dict[str, int] = {}
    for type_key in _TYPE_KEYS:
        statuses: Counter[str] = Counter()
        triggers = 0
        if type_key in scores:
            for payload in scores[type_key]:
                if not isinstance(payload, Mapping):
                    statuses["invalid_payload"] += 1
                    continue
                statuses[str(payload.get("status") or "missing_status")] += 1
                triggers += int(payload.get("triggered") is True)
        framework_statuses[type_key] = dict(sorted(statuses.items()))
        framework_triggers[type_key] = triggers

    primary_counts = Counter(
        str(value)
        for value in scores.get("primary_type", pd.Series(dtype="object"))
        if isinstance(value, str) and value
    )
    evidence_levels: Counter[str] = Counter()
    if "quantitative_evidence" in scores:
        for company_evidence in scores["quantitative_evidence"]:
            if not isinstance(company_evidence, Mapping):
                continue
            for payload in company_evidence.values():
                if isinstance(payload, Mapping):
                    evidence_levels[str(payload.get("evidence_level") or "missing_level")] += 1
    if "num_types" in scores:
        numeric_types = pd.to_numeric(scores["num_types"], errors="coerce").fillna(0)
        candidate_companies = int((numeric_types > 0).sum())
        total_framework_triggers = int(numeric_types.clip(lower=0).sum())
    else:
        candidate_companies = sum(
            any(
                isinstance(row.get(type_key), Mapping) and row[type_key].get("triggered") is True
                for type_key in _TYPE_KEYS
            )
            for row in scores.to_dict(orient="records")
        )
        total_framework_triggers = sum(framework_triggers.values())
    return {
        "candidate_companies": candidate_companies,
        "total_framework_triggers": total_framework_triggers,
        "framework_trigger_counts": framework_triggers,
        "primary_trigger_counts": dict(sorted(primary_counts.items())),
        "framework_status_counts": framework_statuses,
        "quantitative_evidence_level_counts": dict(sorted(evidence_levels.items())),
    }


def _non_economic_skip_details(classifications: object) -> dict[str, dict[str, str]]:
    """Expose every full-market data/model exception without dumping economic skips."""
    if not isinstance(classifications, Mapping):
        return {}
    details: dict[str, dict[str, str]] = {}
    for code, payload in classifications.items():
        if not isinstance(payload, Mapping):
            continue
        category = payload.get("category")
        reason = payload.get("reason")
        if (
            not isinstance(category, str)
            or not category
            or category == DCF_SKIP_ECONOMIC_NOT_APPLICABLE
            or not isinstance(reason, str)
            or not reason
        ):
            continue
        details.setdefault(category, {})[str(code)] = reason
    return {category: dict(sorted(code_reasons.items())) for category, code_reasons in sorted(details.items())}


def _snapshot_reporting_period_contract(snapshot: object) -> ReportingPeriodContract:
    """Freeze the schema-v6 snapshot period contract before any analysis runs."""
    validation = getattr(snapshot, "validation", None)
    raw_contract = validation.get("reporting_period_contract") if isinstance(validation, Mapping) else None
    if not isinstance(raw_contract, Mapping):
        raise RuntimeError("validated snapshot has no reporting_period_contract")
    if raw_contract.get("period_basis") != _STRICT_TTM_PERIOD_BASIS:
        raise RuntimeError("validated snapshot reporting_period_contract has an unsupported period basis")
    fields = (
        "annual_report_date",
        "current_interim_report_date",
        "prior_interim_report_date",
    )
    if any(not isinstance(raw_contract.get(field), str) for field in fields):
        raise RuntimeError("validated snapshot reporting_period_contract has invalid date fields")
    return ReportingPeriodContract(
        annual_report_date=raw_contract["annual_report_date"],
        current_interim_report_date=raw_contract["current_interim_report_date"],
        prior_interim_report_date=raw_contract["prior_interim_report_date"],
    )


def _load_market_coldness_evidence(
    snapshot: object,
    eligible_codes: Sequence[str],
    *,
    force_refresh: bool,
) -> tuple[dict[str, Mapping[str, object]], dict[str, object]]:
    """Acquire one SH/SZ batch; any unavailable state remains explicit and scoreless."""
    validation = getattr(snapshot, "validation", None)
    source_trade_dates = validation.get("trading_source_trade_dates") if isinstance(validation, Mapping) else None
    as_of_session = (
        source_trade_dates[0]
        if isinstance(source_trade_dates, list)
        and len(source_trade_dates) == 1
        and isinstance(source_trade_dates[0], str)
        and source_trade_dates[0]
        else None
    )
    eligible = set(eligible_codes)
    if as_of_session is None:
        return {}, {
            "available": False,
            "evidence_available": False,
            "source": None,
            "source_url": None,
            "retrieved_at": None,
            "as_of_session": None,
            "fetched_count": 0,
            "total_expected": None,
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "source_coverage": None,
            "cache_hit": False,
            "cache_diagnostic": None,
            "reason": "snapshot validation does not contain exactly one trading source_trade_date",
            "evidence_reason": "missing_bound_as_of_session",
            "unavailable_policy": _MARKET_COLDNESS_UNAVAILABLE_POLICY,
        }
    coldness_snapshot = None
    try:
        # Listing enrichment acquires the same validated whole-market source
        # batch during quote refresh. Reuse its safe cache here.
        coldness_snapshot = fetch_market_coldness_snapshot(force_refresh=False)
        evidence_diagnostics: dict[str, object] = {}
        evidence = build_market_coldness_evidence(
            coldness_snapshot,
            as_of_session=as_of_session,
            diagnostics=evidence_diagnostics,
        )
        eligible_count = len(eligible & set(evidence))
        eligible_coverage = eligible_count / len(eligible) if eligible else 0.0
        source_available = bool(coldness_snapshot.available)
        if not source_available:
            evidence_reason = "source_unavailable"
        elif not evidence:
            evidence_reason = str(evidence_diagnostics.get("evidence_reason") or "no_scoreable_evidence")
        elif eligible_count < len(eligible):
            evidence_reason = "partial_eligible_coverage"
        else:
            evidence_reason = "available"
        status: dict[str, object] = {
            "available": source_available,
            "evidence_available": bool(eligible_count),
            "source": coldness_snapshot.source,
            "source_url": coldness_snapshot.source_url,
            "retrieved_at": coldness_snapshot.retrieved_at,
            "as_of_session": as_of_session,
            "fetched_count": coldness_snapshot.fetched_count,
            "total_expected": coldness_snapshot.total_expected,
            "eligible_evidence_count": eligible_count,
            "eligible_evidence_coverage": eligible_coverage,
            "source_coverage": coldness_snapshot.coverage.to_dict(),
            "cache_hit": coldness_snapshot.cache_hit,
            "cache_diagnostic": coldness_snapshot.cache_diagnostic,
            "reason": coldness_snapshot.reason,
            "evidence_reason": evidence_reason,
            "unavailable_policy": _MARKET_COLDNESS_UNAVAILABLE_POLICY,
            "evidence_diagnostics": evidence_diagnostics,
        }
        return evidence, status
    except Exception as exc:
        return {}, {
            "available": False,
            "evidence_available": False,
            "source": getattr(coldness_snapshot, "source", None),
            "source_url": getattr(coldness_snapshot, "source_url", None),
            "retrieved_at": getattr(coldness_snapshot, "retrieved_at", None),
            "as_of_session": as_of_session,
            "fetched_count": getattr(coldness_snapshot, "fetched_count", 0),
            "total_expected": getattr(coldness_snapshot, "total_expected", None),
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "source_coverage": None,
            "cache_hit": getattr(coldness_snapshot, "cache_hit", False),
            "cache_diagnostic": getattr(coldness_snapshot, "cache_diagnostic", None),
            "reason": f"{type(exc).__name__}: {exc}",
            "evidence_reason": "validation_or_acquisition_error",
            "unavailable_policy": _MARKET_COLDNESS_UNAVAILABLE_POLICY,
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    starting_state = audit_state_hashes()
    cache = SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION)
    snapshot = get_market_snapshot(
        DataFetcher(
            enrich_listing_dates=True,
            force_reference_refresh=args.refresh,
        ),
        cache,
        force_refresh=args.refresh,
        persist_network=False,
    )
    reporting_period_contract = _snapshot_reporting_period_contract(snapshot)
    eligible = snapshot.eligible_codes
    market_coldness_evidence, market_coldness_status = _load_market_coldness_evidence(
        snapshot,
        eligible,
        force_refresh=args.refresh,
    )
    analysis = run_market_analysis(
        snapshot.analysis_quotes,
        snapshot.analysis_financials,
        eligible_codes=eligible,
        enforce_quality=True,
        expected_companies=len(eligible),
        previous_quality=_comparison_quality(snapshot),
        reporting_period_contract=reporting_period_contract,
        market_coldness_evidence=market_coldness_evidence,
        quality_history_loader=fetch_quality_history_batch,
    )
    active_payload_sha256 = snapshot.baseline_payload_sha256
    if snapshot.source == "network":
        saved = save_market_snapshot(
            cache,
            snapshot.quotes,
            snapshot.financials,
            data_timestamp=snapshot.data_timestamp,
            retrieved_at=snapshot.retrieved_at,
            analysis_quality=analysis.quality,
            expected_previous_timestamp=snapshot.baseline_timestamp,
            expected_previous_payload_sha256=snapshot.baseline_payload_sha256,
        )
        active_payload_sha256 = saved.get("payload_sha256")
    if not isinstance(active_payload_sha256, str):
        raise RuntimeError("active snapshot has no verified payload identity")

    snapshot_artifact = cache.read_bytes_if_payload(active_payload_sha256)
    snapshot_sha256 = hashlib.sha256(snapshot_artifact).hexdigest().upper()
    audit = audit_random_sample(
        snapshot.quotes,
        snapshot.financials,
        eligible_codes=eligible,
        seed=args.seed,
        sample_size=args.sample_size,
        snapshot_sha256=snapshot_sha256,
        provenance={
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_source": snapshot.source,
            "snapshot_payload_sha256": active_payload_sha256,
            "snapshot_artifact_bytes": len(snapshot_artifact),
            "validation": dict(snapshot.validation),
            "full_market_quality": dict(analysis.quality),
            "market_coldness": dict(market_coldness_status),
        },
        full_market_analysis=analysis,
        reporting_period_contract=reporting_period_contract,
        market_coldness_evidence=market_coldness_evidence,
        quality_history_evidence=analysis.quality_history_evidence,
    )
    ending_state = audit_state_hashes()
    provenance_state = {key: audit.provenance.get(key) for key in starting_state}
    if starting_state != ending_state or provenance_state != starting_state:
        raise RuntimeError("source, rules, industry data, or dependency manifests changed during audit")
    paths = write_audit_artifacts(audit, args.output_dir, data_timestamp=snapshot.data_timestamp)
    if audit_state_hashes() != ending_state:
        for path in paths.values():
            path.unlink(missing_ok=True)
        raise RuntimeError("source state changed while audit artifacts were being written")
    skip_classifications = getattr(analysis, "dcf_skip_classifications", {})
    skip_categories = (
        Counter(
            str(value.get("category") or "missing_category")
            for value in skip_classifications.values()
            if isinstance(value, Mapping)
        )
        if isinstance(skip_classifications, Mapping)
        else Counter()
    )
    summary = {
        "refresh_requested": bool(args.refresh),
        "refresh_completed": _refresh_completed(args.refresh, snapshot.source),
        "snapshot_source": snapshot.source,
        "snapshot_warning": getattr(snapshot, "warning", ""),
        "snapshot_cache_diagnostic": dict(getattr(snapshot, "cache_diagnostic", {}) or {}),
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "quotes": len(snapshot.quotes),
        "eligible": len(eligible),
        "score_rows": len(analysis.scores),
        "dcf_valid": len(analysis.dcf_results),
        "dcf_skipped": analysis.dcf_skipped,
        "dcf_skip_reason_counts": dict(sorted(Counter(analysis.dcf_skip_reasons.values()).items())),
        "dcf_skip_classification_counts": dict(sorted(skip_categories.items())),
        "dcf_non_economic_skip_details": _non_economic_skip_details(skip_classifications),
        "screening_coverage": _analysis_coverage_summary(analysis.scores),
        "pipeline_issues": len(analysis.issues),
        "analysis_quality": dict(analysis.quality),
        "market_coldness": dict(market_coldness_status),
        "random_sample_size": audit.sample_size,
        "random_engine_errors": list(audit.engine_invariant_errors),
        "random_scoring_replay_errors": list(audit.scoring_replay_errors),
        "random_valuation_replay_errors": list(audit.valuation_replay_errors),
        "random_independent_errors": list(audit.independent_errors),
        "snapshot_sha256": snapshot_sha256,
        "snapshot_payload_sha256": active_payload_sha256,
        "artifacts": {key: str(value) for key, value in paths.items()},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    # The production quality gate intentionally tolerates a very small issue
    # rate so the UI can retain a prior good generation during source outages.
    # A release audit is stricter: even an issue outside the sampled 100 rows
    # must fail the command and cannot be represented as a clean release.
    return 1 if not summary["refresh_completed"] or audit.invariant_errors or analysis.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
