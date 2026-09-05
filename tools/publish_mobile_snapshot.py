"""Build one validated whole-market mobile snapshot after the A-share close.

This command is deliberately separate from the Windows release process.  A
failed or stale upstream refresh exits non-zero before any output is written,
so the hosting workflow keeps serving the last known-good mobile result.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from typing import Any
from datetime import date, datetime, time, timezone, timedelta
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess

import pandas as pd

from data.as_of import shanghai_now
from data.cache import SafeFileCache
from data.fetcher import DataFetcher
from data.growth_evidence import (
    EXTERNAL_HISTORY_YEARS,
    TYPE3_GROWTH_DUE_RETRY_RESERVE_RATIO,
    fetch_growth_evidence_batch,
    load_external_growth_evidence_cache_batch_state,
    load_growth_evidence_cache_batch_state,
    load_growth_evidence_retry_state_batch,
    record_growth_evidence_retry_states,
    _latest_completed_annual_year,
    _parse_as_of,
)
from data.patch4_evidence import fetch_patch4_evidence_batch
from data.commodity_evidence import CommodityCycleError, load_commodity_cycle_evidence
from data.quality_history import (
    fetch_quality_history_batch,
    load_quality_history_cache_batch_state,
)
from data.research_reports import fetch_research_reports_batch
from data.snapshot import DEFAULT_SNAPSHOT_PATH, SNAPSHOT_SCHEMA_VERSION, get_market_snapshot, save_market_snapshot
from data.mobile_snapshot import (
    COMPANY_DETAIL_SCHEMA_VERSION,
    COMPANY_DETAIL_SHARD_COUNT,
    write_mobile_snapshot,
)
from data.market_coldness import MARKET_COLDNESS_DECISION_READY_TIME, archive_market_coldness_session_snapshot
from engine.audit import audit_state_hashes
from engine.model_sources import public_model_source_contract
from engine.buy_screener import METHODOLOGY_VERSION
from engine.pipeline import run_market_analysis
from tools.run_full_audit import (
    _analysis_coverage_summary,
    _comparison_quality,
    _load_market_coldness_evidence,
    _refresh_completed,
    _require_market_coldness_release_evidence,
    _snapshot_reporting_period_contract,
)


_MOBILE_STRUCTURAL_EVIDENCE_GATES = (
    "artifact_integrity_ready",
    "candidate_visibility_ready",
    "candidate_recall_ready",
)
_QUALITY_HISTORY_BACKFILL_LIMIT = 2_000
# The prefill normally covers most of the market.  The decision-stage loader
# is cumulative across Type 2/5/7, so give Type 2's 2d first claim on a full
# thousand-company tranche.  This covers the current generation's roughly
# eight-hundred Type 2 valuation-history requests while keeping a hard upper
# bound on upstream requests; genuinely unavailable histories still remain
# explicit gaps.
_QUALITY_HISTORY_DECISION_BACKFILL_LIMIT = 1_000
_TYPE3_GROWTH_NETWORK_BACKFILL_LIMIT = 6_000
# The daily post-close build must publish on time, so the Type 3 network
# backfill (segment + annual cash-flow) is best-effort: within this budget it
# captures whatever Eastmoney's rate limits allow, and anything left over stays
# eligible for the next run through the retry/cache state (which accumulates).
# 15 minutes left the whole-market gap (type3/type7 segment evidence) largely
# unfilled on the first budgeted runs; 45 minutes covers the full market at
# Eastmoney's throttled rate, and later runs are cheap because the cache
# accumulates.
_TYPE3_GROWTH_NETWORK_TIME_BUDGET_SECONDS = 2700.0


def _refresh_failure_message(snapshot: object) -> str:
    """Keep the provider failure visible without flooding the workflow log."""

    warning = " ".join(str(getattr(snapshot, "warning", "") or "").split())
    suffix = f"; source warning: {warning[:500]}" if warning else ""
    return "fresh market refresh did not complete; retaining the published mobile snapshot" + suffix


def _require_company_detail_manifest(manifest: Mapping[str, object], expected_companies: int) -> None:
    details = manifest.get("company_details")
    if not isinstance(details, Mapping):
        raise RuntimeError("mobile publication omitted the company detail contract")
    partition = details.get("partition")
    shards = details.get("shards")
    catalogue = manifest.get("catalogue")
    catalogue_filename = str(catalogue.get("filename") or "") if isinstance(catalogue, Mapping) else ""
    generation_match = re.fullmatch(r"catalog-([0-9a-f]{16})\.json\.gz", catalogue_filename)
    expected_ids = [f"{index:02x}" for index in range(COMPANY_DETAIL_SHARD_COUNT)]
    if (
        details.get("schema_version") != COMPANY_DETAIL_SCHEMA_VERSION
        or details.get("record_schema") != "company_detail_v2"
        or details.get("company_count") != expected_companies
        or not isinstance(partition, Mapping)
        or partition.get("algorithm") != "sha256_code_first_nibble"
        or partition.get("shard_count") != COMPANY_DETAIL_SHARD_COUNT
        or details.get("root_algorithm") != "SHA256_CANONICAL_SHARD_INDEX_V1"
        or re.fullmatch(r"[0-9a-f]{64}", str(details.get("root_sha256") or "")) is None
        or not isinstance(shards, list)
        or len(shards) != COMPANY_DETAIL_SHARD_COUNT
        or generation_match is None
    ):
        raise RuntimeError("mobile publication emitted an invalid company detail contract")

    generation = generation_match.group(1)
    company_total = 0
    observed_ids: list[str] = []
    for shard in shards:
        if not isinstance(shard, Mapping):
            raise RuntimeError("mobile publication emitted invalid company detail shard metadata")
        shard_id = str(shard.get("id") or "")
        observed_ids.append(shard_id)
        company_count = shard.get("company_count")
        if not isinstance(company_count, int) or isinstance(company_count, bool) or company_count < 0:
            raise RuntimeError("mobile publication emitted invalid company detail shard metadata")
        company_total += company_count
        if (
            shard.get("filename") != f"company-details-{generation}-{shard_id}.json.gz"
            or re.fullmatch(r"[0-9a-f]{64}", str(shard.get("uncompressed_sha256") or "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(shard.get("sha256") or "")) is None
            or not isinstance(shard.get("size"), int)
            or int(shard["size"]) <= 0
            or not isinstance(shard.get("uncompressed_size"), int)
            or int(shard["uncompressed_size"]) <= 0
        ):
            raise RuntimeError("mobile publication emitted invalid company detail shard metadata")
    if observed_ids != expected_ids or company_total != expected_companies:
        raise RuntimeError("mobile publication company detail coverage is incomplete")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true", help="require a fresh validated market snapshot")
    parser.add_argument(
        "--reuse-evidence-only",
        action="store_true",
        help="re-score using validated company-evidence caches without per-company network backfills",
    )
    parser.add_argument(
        "--reference-manifest",
        type=Path,
        help="current published manifest required to check a cache-only rebuild for lost evidence",
    )
    parser.add_argument(
        "--force-financial-fallback-refresh",
        action="store_true",
        help="bypass only the bounded secondary financial-source cache",
    )
    parser.add_argument(
        "--refresh-financials-only",
        action="store_true",
        help="reuse the cached closed-session quotes but re-fetch financials and re-score",
    )
    parser.add_argument(
        "--qualitative-overlay",
        type=Path,
        help="optional dated primary qualitative scores merged into company financials before scoring",
    )
    return parser


def _market_as_of(snapshot: object) -> str:
    validation = getattr(snapshot, "validation", None)
    dates = validation.get("trading_source_trade_dates") if isinstance(validation, Mapping) else None
    if not isinstance(dates, list) or len(dates) != 1 or not isinstance(dates[0], str):
        raise RuntimeError("snapshot has no unique validated trading session")
    value = dates[0]
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("snapshot trading session is invalid") from exc
    return value


def _load_qualitative_overlay(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Load one dated primary qualitative record set keyed by company code.

    Each record must already carry ``{score, evidence_level, evidence}`` in the
    shape ``engine.buy_screener.extract_metrics`` validates; this loader only
    checks the outer envelope and leaves score-level fail-closed validation to
    the scoring boundary.
    """
    if not path.is_file():
        raise FileNotFoundError(f"qualitative overlay file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"qualitative overlay is not readable JSON: {path}") from exc
    if not isinstance(raw, Mapping) or not raw:
        raise RuntimeError("qualitative overlay must be a non-empty code mapping")
    overlay: dict[str, dict[str, dict[str, Any]]] = {}
    for code, keys in raw.items():
        canonical = str(code).strip()
        if not re.fullmatch(r"[036][0-9]{5}", canonical) or not isinstance(keys, Mapping) or not keys:
            raise RuntimeError(f"qualitative overlay record is invalid: {code}")
        overlay[canonical] = {str(key): dict(value) for key, value in keys.items() if isinstance(value, Mapping)}
        if not overlay[canonical]:
            raise RuntimeError(f"qualitative overlay record has no score keys: {code}")
    return overlay


def _require_replay_reference(
    reference: Mapping[str, Any] | None,
    snapshot: object,
    company_count: int,
) -> Mapping[str, Any]:
    if reference is None:
        raise RuntimeError("cache-only rebuild requires the current published reference manifest")
    provenance = reference.get("provenance", {})
    if (
        reference.get("market_as_of") != _market_as_of(snapshot)
        or reference.get("summary", {}).get("company_count") != company_count
        or provenance.get("snapshot_payload_sha256") != getattr(snapshot, "baseline_payload_sha256", None)
    ):
        raise RuntimeError("cache-only rebuild does not match the published session, universe, and raw snapshot")
    if not isinstance(provenance.get("quality_history_backfill", {}).get("available_companies"), int):
        raise RuntimeError("published reference has no comparable quality-history evidence coverage")
    if not isinstance(provenance.get("screening_coverage", {}).get("quantitative_missing_input_counts"), Mapping):
        raise RuntimeError("published reference has no comparable source-input coverage")
    return provenance


def _require_quality_history_replay(reference: Mapping[str, Any], current: Mapping[str, int]) -> None:
    before = reference["quality_history_backfill"]["available_companies"]
    after = current["available_companies"]
    if after < before:
        raise RuntimeError(
            f"cache-only rebuild lost usable quality histories ({before} -> {after}); "
            "restore the source caches before rebuilding; keeping the published generation"
        )
    print(f"EVIDENCE_REPLAY quality_history available {before} -> {after}", flush=True)


def _require_source_input_replay(reference: Mapping[str, Any], coverage: Mapping[str, Any]) -> None:
    before = reference["screening_coverage"]["quantitative_missing_input_counts"]
    after = coverage.get("quantitative_missing_input_counts", {})
    lost = []
    for metric, inputs in before.items():
        if metric not in after:
            raise RuntimeError(f"cache-only rebuild omitted comparable evidence metric: {metric}")
        # Only compare already-defined source inputs. New model requirements
        # and changed buy/observe decisions are not evidence regressions.
        for source_input, previous_count in inputs.items():
            current_count = after[metric].get(source_input, 0)
            if current_count > previous_count:
                lost.append(f"{metric}.{source_input}: {previous_count} -> {current_count} missing")
    if lost:
        raise RuntimeError("cache-only rebuild lost published source evidence: " + "; ".join(lost))
    print("EVIDENCE_REPLAY all comparable source-input coverage retained", flush=True)


def _utc_timestamp(value: object) -> str:
    if isinstance(value, bool):
        raise RuntimeError("snapshot data timestamp is invalid")
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("snapshot data timestamp is invalid") from exc
    if timestamp <= 0:
        raise RuntimeError("snapshot data timestamp is invalid")
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _shanghai_now() -> datetime:
    return shanghai_now()


def _shanghai_today() -> str:
    return _shanghai_now().date().isoformat()


def _source_commit() -> str:
    """Return the exact checked-out main revision bound into the signed manifest."""

    candidate = os.environ.get("GITHUB_SHA", "").strip().lower()
    if not candidate:
        git_executable = shutil.which("git")
        if git_executable is None:
            raise RuntimeError("mobile publication cannot determine its source Git commit")
        repository_root = Path(__file__).resolve().parents[1]
        try:
            worktree = subprocess.run(
                [git_executable, "status", "--porcelain", "--untracked-files=all"],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if worktree.stdout.strip():
                raise RuntimeError(
                    "local mobile publication requires a clean Git worktree so its signed source commit is exact"
                )
            completed = subprocess.run(
                [git_executable, "rev-parse", "HEAD"],  # nosec B603 - fixed local Git query
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("mobile publication cannot determine its source Git commit") from exc
        candidate = completed.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", candidate) is None:
        raise RuntimeError("mobile publication source Git commit is invalid")
    return candidate


def _require_post_close_quotes(snapshot: object, market_as_of: str) -> float:
    quotes = getattr(snapshot, "analysis_quotes", None)
    required_columns = {"code", "market", "quote_status", "source_trade_date", "quote_tick_time"}
    if quotes is None or not required_columns.issubset(getattr(quotes, "columns", ())):
        raise RuntimeError("fresh snapshot has no verifiable post-close quote timestamps")
    eligible_codes = tuple(getattr(snapshot, "eligible_codes", ()))
    codes = [str(code).strip() for code in quotes["code"]]
    if (
        not eligible_codes
        or len(codes) != len(set(codes))
        or set(codes) != set(eligible_codes)
        or not quotes["market"].astype(str).isin({"SH", "SZ"}).all()
    ):
        raise RuntimeError("fresh snapshot post-close rows do not match the eligible SH/SZ universe")
    statuses = quotes["quote_status"].astype(str)
    if not statuses.isin({"trading", "suspended_or_no_trade"}).all():
        raise RuntimeError("fresh snapshot has invalid quote trading states")
    trading = quotes.loc[statuses.eq("trading")]
    trading_coverage = len(trading) / len(quotes)
    if trading.empty or trading_coverage < 0.99:
        raise RuntimeError(
            f"trading quote coverage {trading_coverage:.1%} is below required 99.0%; "
            "too many companies may be using previous-close prices"
        )
    verified = 0
    for source_date, tick_value in zip(trading["source_trade_date"], trading["quote_tick_time"]):
        try:
            tick = time.fromisoformat(str(tick_value).strip())
        except (TypeError, ValueError):
            continue
        if str(source_date).strip() == market_as_of and tick >= time(15, 0):
            verified += 1
    # Use the complete eligible universe as the denominator.  Otherwise a
    # corrupted feed could label a large fraction as suspended and make its
    # remaining trading subset look perfectly post-close.
    coverage = verified / len(quotes)
    if coverage < 0.99:
        raise RuntimeError(
            f"post-close quote coverage {coverage:.1%} is below required 99.0%; "
            "the source may be replaying intraday prices"
        )
    return coverage


def _mobile_screening_coverage(
    scores: object,
    dcf_results: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Require replayable score records while publishing honest source gaps."""

    if not isinstance(scores, pd.DataFrame):
        raise RuntimeError("whole-market screening result is not a score frame")
    coverage = (
        _analysis_coverage_summary(scores, dcf_results)
        if dcf_results is not None
        else _analysis_coverage_summary(scores)
    )
    readiness = coverage.get("goal_readiness")
    if not isinstance(readiness, Mapping):
        raise RuntimeError("whole-market screening evidence contract is missing")
    failed = [gate for gate in _MOBILE_STRUCTURAL_EVIDENCE_GATES if readiness.get(gate) is not True]
    if failed:
        contracts = coverage.get("framework_evidence_contract")
        details: list[str] = []
        if isinstance(contracts, Mapping):
            for type_key, contract in contracts.items():
                if not isinstance(contract, Mapping):
                    continue
                flags = [
                    f"{field}={contract[field]}"
                    for field in (
                        "invalid_payload",
                        "invalid_applicability",
                        "incomplete_without_reason",
                        "invalid_sub_scores",
                        "invalid_decision",
                        "decision_hidden",
                        "recall_unsafe",
                        "invalid_evidence_complete",
                    )
                    if isinstance(contract.get(field), int) and contract[field] > 0
                ]
                if flags:
                    examples = (
                        contract.get("invalid_decision_examples")
                        or contract.get("incomplete_without_reason_examples")
                        or contract.get("invalid_sub_score_examples")
                        or []
                    )
                    details.append(f"{type_key}:{','.join(flags)} examples={list(examples)[:8]}")
        raise RuntimeError(
            "whole-market screening evidence contract failed: "
            + ",".join(failed)
            + (" [" + "; ".join(details) + "]" if details else "")
        )
    result = dict(coverage)
    result["publication_readiness"] = {
        "artifact_integrity_ready": True,
        "candidate_visibility_ready": True,
        "candidate_recall_ready": True,
        "ideal_zero_gap_ready": readiness.get("ideal_zero_gap_ready") is True,
    }
    return result


def _prepare_commodity_cycle_evidence(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    as_of: str,
    cache_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Bind Sina commodity-cycle attributes to direct-cyclical companies.

    The commodity source is an enhancement for the Type 5 strong-cycle gate:
    if the fetch or cache contract fails, publication continues with those
    companies keeping their current evidence-insufficient result instead of
    aborting the whole market refresh.
    """
    from data.industry import classify_industries

    industry_inputs = [
        (str(row["code"]).strip(), str(row.get("name") or ""))
        for _, row in quotes.iterrows()
        if str(row["code"]).strip() in financials
    ]
    industry_by_code = classify_industries(industry_inputs)
    try:
        evidence = load_commodity_cycle_evidence(
            industry_by_code,
            as_of=as_of,
            cache_only=cache_only,
        )
    except (CommodityCycleError, TypeError, ValueError, OSError) as exc:
        print(f"COMMODITY_CYCLE_DIAGNOSTIC unavailable; Type 5 commodity gate skipped: {exc!r}", flush=True)
        return {}
    return evidence


def _prepare_dividend_evidence(
    eligible_codes: Sequence[str],
    *,
    as_of: str,
    cache_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Bind Eastmoney dividend history to the whole universe for the gdN gate.

    Dividend data is an enhancement for the Type 7 gdN investability filter:
    if the source fails, publication continues with an explicit unavailable
    dividend state.  The gdN gate may still pass an independent R&D or verified
    strong-cycle route, but it never turns a network failure into d=0.
    """
    from data.dividend_evidence import DividendEvidenceError, load_dividend_evidence

    try:
        evidence = load_dividend_evidence(
            eligible_codes,
            as_of=as_of,
            cache_only=cache_only,
        )
    except (DividendEvidenceError, TypeError, ValueError, OSError) as exc:
        print(f"DIVIDEND_DIAGNOSTIC unavailable; gdN filter keeps d unknown: {exc!r}", flush=True)
        return {
            code: {
                "status": "unavailable",
                "reason": f"batch_failure:{type(exc).__name__}",
                "evidence": {
                    "source": "东方财富分红送配明细",
                    "evidence_id": f"eastmoney-sharebonus-v2:{code}:{as_of.replace('-', '')}",
                    "as_of": as_of,
                    "summary": "分红资料批量抓取失败，未把未知值当作零",
                },
            }
            for code in sorted(set(eligible_codes))
        }
    return evidence


def _prepare_quality_history_evidence(
    eligible_codes: Sequence[str],
    market_as_of: str,
    *,
    priority_codes: Sequence[str] = (),
    network_limit: int = _QUALITY_HISTORY_BACKFILL_LIMIT,
) -> tuple[dict[str, Mapping[str, object]], dict[str, int]]:
    """Reuse recent source captures and fill one bounded daily tranche.

    A full A-share history refresh requires roughly ten thousand upstream
    requests.  Loading every company in one burst is both unreliable and
    unfriendly to the public sources.  The daily publication therefore
    restores every still-fresh source capture and fetches the next stable
    tranche of missing companies.  Coverage accumulates across runs.
    """

    eligible = {str(code) for code in eligible_codes}
    ordered_codes: list[str] = []
    seen: set[str] = set()
    for code in priority_codes:
        normalized = str(code)
        if normalized in eligible and normalized not in seen:
            ordered_codes.append(normalized)
            seen.add(normalized)
    ordered_codes.extend(sorted(eligible - seen))
    requests_ = [{"code": code, "as_of": market_as_of} for code in ordered_codes]
    cached, refresh_due_codes = load_quality_history_cache_batch_state(requests_)
    refresh_due = set(refresh_due_codes)
    missing = [request for request in requests_ if request["code"] not in cached or request["code"] in refresh_due]
    tranche = missing[:network_limit]
    fetched = fetch_quality_history_batch(tranche) if tranche else {}
    combined: dict[str, Mapping[str, object]] = {
        str(code): dict(value) for code, value in cached.items() if isinstance(value, Mapping)
    }
    combined.update({str(code): dict(value) for code, value in fetched.items() if isinstance(value, Mapping)})
    available = sum(1 for value in combined.values() if value.get("available") is True)
    return combined, {
        "requested_companies": len(requests_),
        "reused_companies": len(cached),
        "network_tranche_companies": len(tranche),
        "returned_companies": len(combined),
        "available_companies": available,
        "remaining_companies": max(0, len(requests_) - available),
    }


def _quality_history_priority_codes(
    quotes: object,
    eligible_codes: Sequence[str],
) -> tuple[str, ...]:
    """Prioritise the most economically material companies for bounded backfill."""

    if not isinstance(quotes, pd.DataFrame) or not {"code", "market_cap"}.issubset(quotes.columns):
        return tuple()
    eligible = {str(code) for code in eligible_codes}
    ranked = quotes.loc[:, ["code", "market_cap"]].copy()
    ranked["code"] = ranked["code"].astype(str)
    ranked = ranked.loc[ranked["code"].isin(eligible)]
    ranked["market_cap"] = pd.to_numeric(ranked["market_cap"], errors="coerce")
    ranked = ranked.loc[ranked["market_cap"].gt(0)]
    ranked = ranked.sort_values(
        ["market_cap", "code"],
        ascending=[False, True],
        kind="mergesort",
    )
    return tuple(dict.fromkeys(ranked["code"].tolist()))


def _bounded_quality_history_loader(limit: int = _QUALITY_HISTORY_DECISION_BACKFILL_LIMIT):
    """Return a loader with one cumulative network budget for the whole analysis.

    ``screen_all_types`` may split a market-wide request into several 2,000-row
    calls.  Capping each call independently would therefore still allow almost
    the entire market to be fetched after the publication prefill.  This
    stateful adapter applies the limit across every call in one publication.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("quality-history decision backfill limit must be a non-negative integer")
    remaining = limit

    def load(requests: Sequence[Mapping[str, object]], *, progress_cb=None):
        nonlocal remaining
        prepared = list(requests)
        cached, refresh_due_codes = load_quality_history_cache_batch_state(prepared)
        refresh_due = set(refresh_due_codes)
        network_candidates = [
            request
            for request in prepared
            if str(request.get("code") or "") not in cached or str(request.get("code") or "") in refresh_due
        ]
        selected = network_candidates[:remaining]
        remaining -= len(selected)
        fetched = fetch_quality_history_batch(selected, progress_cb=progress_cb) if selected else {}
        combined = {str(code): dict(value) for code, value in cached.items() if isinstance(value, Mapping)}
        combined.update({str(code): dict(value) for code, value in fetched.items() if isinstance(value, Mapping)})
        return combined

    return load


_CNINFO_UNIT_MULTIPLIERS = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8}


def _load_cninfo_acquisition_batch(
    codes: Sequence[str],
    *,
    as_of: date,
    max_workers: int = 16,
    time_budget_seconds: float = 900.0,
) -> dict[str, dict[int, dict[str, object]]]:
    """Load audited annual-report acquisition cash-flow values in CNY.

    Only companies in the network tranche are loaded; cached CNINFO results
    (including explicit ``available=False`` outcomes) are free on later runs.
    Values are multiplied into CNY from the reported unit and kept positive
    (the annual-report line is a payment outflow).

    Best-effort by design: the daily post-close build must publish on time,
    so the batch stops after ``time_budget_seconds`` (default 15 minutes).
    Anything not captured stays eligible for a later run through the cache,
    which accumulates across runs (a 15-minute budget at 16 workers covers
    roughly the whole market on the first run).
    """

    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from data.cninfo_annual import fetch_annual_acquisition

    latest = _latest_completed_annual_year(as_of)
    years = list(range(latest, latest - EXTERNAL_HISTORY_YEARS, -1))
    tasks = [(code, year) for code in codes for year in years]
    by_code: dict[str, dict[int, dict[str, object]]] = {}
    deadline = time.monotonic() + max(0.0, time_budget_seconds)
    window_size = max_workers * 4
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for offset in range(0, len(tasks), window_size):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            window = tasks[offset : offset + window_size]
            futures = {pool.submit(fetch_annual_acquisition, code, year): (code, year) for code, year in window}
            try:
                for future in as_completed(futures, timeout=remaining):
                    code, year = futures[future]
                    try:
                        evidence = future.result()
                    except Exception:  # noqa: BLE001
                        continue
                    if not evidence.available or evidence.acquisition_cashflow is None:
                        continue
                    multiplier = _CNINFO_UNIT_MULTIPLIERS.get(evidence.unit, 1.0)
                    by_code.setdefault(code, {})[year] = {
                        "value_cny": abs(evidence.acquisition_cashflow) * multiplier,
                        "source_url": evidence.source_url,
                        "source_sha256": evidence.source_sha256,
                    }
            except TimeoutError:
                for future in futures:
                    future.cancel()
                break
    return by_code


def _bounded_type3_growth_loader(
    limit: int = _TYPE3_GROWTH_NETWORK_BACKFILL_LIMIT,
    *,
    cache_only: bool = False,
):
    """Reuse complete recent evidence and cap source work cumulatively.

    ``screen_all_types`` supplies requests in conclusion-relevance order.
    A cached segment alone still needs an annual cash-flow fetch, and a cached
    cash-flow proxy alone still needs a segment fetch.  Only the intersection
    of both independently validated caches is free; otherwise an apparently
    "cached" row could silently send an unbounded number of cash-flow queries
    every day.  Failed attempts retain scheduling-only retry metadata: unseen
    candidates keep their supplied priority while every bounded run reserves a
    deterministic slice for due retries ordered by oldest attempt.  Returning
    a subset is intentional: companies outside the tranche retain their
    explicit evidence-insufficient Type 3 result and are eligible for a later
    daily run.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("Type 3 growth backfill limit must be a non-negative integer")
    remaining = limit

    def load(requests: Sequence[Mapping[str, object]], *, progress_cb=None):
        nonlocal remaining
        prepared = list(requests)
        if cache_only:
            fetched = fetch_growth_evidence_batch(prepared, progress_cb=progress_cb, cache_only=True)
            fetched = _backfill_missing_segments(prepared, fetched, cache_only=True)
            counts = {
                "requested": len(prepared),
                "external_complete": sum(
                    item["external_growth_evidence"]["status"] == "complete" for item in fetched.values()
                ),
                "segment_complete": sum(
                    item["segment_growth_sources"]["status"] == "complete" for item in fetched.values()
                ),
            }
            print(f"EVIDENCE_REPLAY growth {json.dumps(counts, sort_keys=True)}", flush=True)
            return fetched
        cached_segments = load_growth_evidence_cache_batch_state(prepared)
        cached_external = load_external_growth_evidence_cache_batch_state(prepared)
        fully_cached_codes = set(cached_segments).intersection(cached_external)
        # A validated segment cache is independently useful: the segment
        # source (3d) and Type 7 category expansion only need it.  A missing
        # or retry-pending external (acquisition cash-flow) record must not
        # keep the whole company out of the batch and waste the segment
        # evidence, so segment-cached codes are always selected.  The batch
        # fetcher reuses the cached segment and only refetches the annual
        # cash-flow rows, which are cheap chunked requests.
        segment_cached_codes = set(cached_segments)
        retry_state = load_growth_evidence_retry_state_batch(prepared) if remaining else {}
        unseen: list[Mapping[str, object]] = []
        due_retries: list[tuple[str, int, str, Mapping[str, object]]] = []
        for position, request in enumerate(prepared):
            code = str(request.get("code") or "")
            if code in fully_cached_codes or code in segment_cached_codes:
                continue
            state = retry_state.get(code)
            if state is None:
                unseen.append(request)
                continue
            retry_after = state.get("retry_after")
            last_attempt = state.get("last_attempt_as_of")
            as_of = request.get("as_of")
            valid_retry_state = (
                isinstance(retry_after, str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", retry_after)
                and isinstance(last_attempt, str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", last_attempt)
                and isinstance(as_of, str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of)
            )
            if valid_retry_state and as_of >= retry_after:
                due_retries.append((last_attempt, position, code, request))
                continue
            if not valid_retry_state:
                unseen.append(request)
        due_retries.sort(key=lambda item: (item[0], item[1], item[2]))
        due_requests = [item[3] for item in due_retries]
        if due_requests and remaining:
            retry_slots = min(
                len(due_requests),
                max(1, math.ceil(remaining * TYPE3_GROWTH_DUE_RETRY_RESERVE_RATIO)),
            )
            unseen_slots = max(0, remaining - retry_slots)
            selected_network = [*unseen[:unseen_slots], *due_requests[:retry_slots]]
            if len(selected_network) < remaining:
                shortfall = remaining - len(selected_network)
                selected_network.extend(unseen[unseen_slots : unseen_slots + shortfall])
                shortfall = remaining - len(selected_network)
                if shortfall:
                    selected_network.extend(due_requests[retry_slots : retry_slots + shortfall])
        else:
            selected_network = unseen[:remaining]
        remaining -= len(selected_network)
        selected_codes = set(fully_cached_codes)
        selected_codes.update(segment_cached_codes)
        selected_codes.update(str(request.get("code") or "") for request in selected_network)
        selected = [request for request in prepared if str(request.get("code") or "") in selected_codes]
        if not selected:
            # Every requested code was skipped (cache miss + retry not yet due,
            # which happens when the Eastmoney source kept failing on a
            # rate-limited runner IP).  These are exactly the codes the
            # Tongdaxin TCP fallback exists for, so fill them from the TCP
            # cache/channel instead of returning an empty result that keeps
            # their Type 3 3d evidence missing.
            from data import tdx_segment as _tdx_segment

            return _tdx_segment.backfill_tdx_segments(prepared)
        cninfo_by_code: dict[str, dict[int, dict[str, object]]] = {}
        network_codes = [str(request.get("code") or "") for request in selected_network]
        if network_codes:
            as_of_value = next(
                (str(request.get("as_of") or "") for request in selected_network if request.get("as_of")),
                "",
            )
            if as_of_value:
                cninfo_by_code = _load_cninfo_acquisition_batch(
                    network_codes,
                    as_of=_parse_as_of(as_of_value),
                )
        fetched = fetch_growth_evidence_batch(
            selected,
            progress_cb=progress_cb,
            cninfo_acquisition_by_code=cninfo_by_code,
            time_budget_seconds=_TYPE3_GROWTH_NETWORK_TIME_BUDGET_SECONDS,
        )
        if selected_network:
            record_growth_evidence_retry_states(selected_network, fetched)
        return _backfill_missing_segments(prepared, fetched)

    return load


def _backfill_missing_segments(prepared, fetched, *, cache_only=False):
    """Use the secondary segment source without losing acquisition evidence."""

    missing = [
        request
        for request in prepared
        if (fetched.get(str(request.get("code") or ""), {}).get("segment_growth_sources") or {}).get("status")
        in {"unavailable", None}
    ]
    if not missing:
        return fetched
    from data import tdx_segment

    secondary = tdx_segment.backfill_tdx_segments(missing, cache_only=cache_only)
    for code, record in secondary.items():
        existing = fetched.get(str(code))
        if existing is None:
            fetched[str(code)] = record
            continue
        # The fallback has no acquisition/goodwill facts. Replace only the
        # segment child, keeping independently acquired external evidence.
        merged = dict(existing)
        segment = record["segment_growth_sources"]
        external = merged.get("external_growth_evidence") or {}
        merged["segment_growth_sources"] = segment
        merged["available"] = segment.get("status") == "complete" and external.get("status") == "complete"
        reasons = []
        if segment.get("status") != "complete":
            reasons.append(f"segment:{segment.get('reason') or 'partial'}")
        if external.get("status") != "complete":
            reasons.append(f"external:{external.get('reason') or 'unavailable'}")
        merged["reason"] = ";".join(reasons)
        merged["cache_diagnostic"] = "tdx_segment_merged_over_unavailable"
        fetched[str(code)] = merged
    return fetched


def _latest_closed_session_date(now_shanghai: datetime) -> date:
    """The most recently closed Shanghai trading session.

    Today after 16:15 (the post-close ready time), otherwise the previous
    weekday, skipping weekends.
    """

    session = now_shanghai.date()
    if now_shanghai.time() < MARKET_COLDNESS_DECISION_READY_TIME:
        session -= timedelta(days=1)
    while session.weekday() >= 5:
        session -= timedelta(days=1)
    return session


def publish_mobile_snapshot(
    *,
    output_dir: str | Path,
    refresh: bool,
    force_financial_fallback_refresh: bool = False,
    refresh_financials_only: bool = False,
    reuse_evidence_only: bool = False,
    reference_manifest: Mapping[str, Any] | None = None,
    qualitative_overlay_path: Path | None = None,
) -> dict[str, object]:
    """Run production analysis and atomically write a client-ready snapshot."""
    source_commit = _source_commit()
    starting_state = audit_state_hashes()
    cache = SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION)
    snapshot = get_market_snapshot(
        DataFetcher(
            enrich_listing_dates=True,
            force_reference_refresh=refresh,
            force_financial_fallback_refresh=force_financial_fallback_refresh,
        ),
        cache,
        force_refresh=refresh,
        refresh_financials_only=refresh_financials_only,
        allow_expired_cache=reuse_evidence_only,
        cache_only=reuse_evidence_only,
        persist_network=False,
    )
    requires_network_candidate = refresh or refresh_financials_only
    if not _refresh_completed(requires_network_candidate, getattr(snapshot, "source", None)):
        raise RuntimeError(_refresh_failure_message(snapshot))
    market_as_of = _market_as_of(snapshot)
    if refresh:
        now_shanghai = _shanghai_now()
        if (
            now_shanghai.time() < MARKET_COLDNESS_DECISION_READY_TIME
            and market_as_of != _latest_closed_session_date(now_shanghai).isoformat()
        ):
            raise RuntimeError(
                "post-close mobile publication is not allowed before 16:15 Asia/Shanghai "
                "unless it backfills the latest closed session"
            )
    if refresh and market_as_of != _latest_closed_session_date(_shanghai_now()).isoformat():
        raise RuntimeError(
            f"fresh snapshot session {market_as_of} is not the latest closed Shanghai session; retaining published mobile data"
        )
    post_close_quote_coverage = _require_post_close_quotes(snapshot, market_as_of) if refresh else None
    reporting_period_contract = _snapshot_reporting_period_contract(snapshot)
    eligible_codes = tuple(getattr(snapshot, "eligible_codes", ()))
    if not eligible_codes:
        raise RuntimeError("validated snapshot has no eligible Shanghai/Shenzhen companies")
    replay_reference = (
        _require_replay_reference(reference_manifest, snapshot, len(eligible_codes)) if reuse_evidence_only else None
    )
    print(f"MARKET_BUILD session={market_as_of} companies={len(eligible_codes)} loading coldness evidence", flush=True)
    coldness_reference_artifact: dict[str, object] = {}
    coldness_archive_candidates: list[object] = []
    coldness_evidence, coldness_status = _load_market_coldness_evidence(
        snapshot,
        eligible_codes,
        force_refresh=refresh,
        cache_only=reuse_evidence_only,
        allow_network_backfill=reuse_evidence_only,
        reference_artifact_out=coldness_reference_artifact,
        archive_candidate_out=coldness_archive_candidates,
    )
    _require_market_coldness_release_evidence(
        coldness_evidence,
        coldness_status,
        reference_artifact=coldness_reference_artifact,
        eligible_codes=eligible_codes,
        as_of_session=market_as_of,
    )
    if len(coldness_archive_candidates) != 1:
        raise RuntimeError("validated market-coldness evidence has no unique archive candidate")
    archive_market_coldness_session_snapshot(coldness_archive_candidates[0], market_as_of)
    print("MARKET_BUILD loading quality histories", flush=True)
    quality_history_evidence, quality_history_backfill = _prepare_quality_history_evidence(
        eligible_codes,
        market_as_of,
        priority_codes=_quality_history_priority_codes(snapshot.analysis_quotes, eligible_codes),
        network_limit=0 if reuse_evidence_only else _QUALITY_HISTORY_BACKFILL_LIMIT,
    )
    if replay_reference is not None:
        _require_quality_history_replay(replay_reference, quality_history_backfill)
    print("MARKET_BUILD loading commodity and dividend evidence", flush=True)
    commodity_cycle_evidence = _prepare_commodity_cycle_evidence(
        snapshot.analysis_quotes,
        snapshot.analysis_financials,
        as_of=market_as_of,
        cache_only=reuse_evidence_only,
    )
    dividend_evidence = _prepare_dividend_evidence(
        eligible_codes,
        as_of=market_as_of,
        cache_only=reuse_evidence_only,
    )
    analysis_financials: Mapping[str, Mapping[str, Any]] = snapshot.analysis_financials
    if qualitative_overlay_path is not None:
        overlay = _load_qualitative_overlay(qualitative_overlay_path)
        unknown = sorted(set(overlay) - set(analysis_financials))
        if unknown:
            raise ValueError(f"qualitative overlay contains codes outside the eligible universe:{unknown[:5]}")
        analysis_financials = {
            code: ({**dict(financial), **overlay[code]} if code in overlay else financial)
            for code, financial in analysis_financials.items()
        }
        applied = sum(1 for code in analysis_financials if code in overlay)
        print(f"MARKET_BUILD qualitative overlay merged for {applied} companies", flush=True)
    print("MARKET_BUILD scoring all companies and seven buy types", flush=True)
    analysis = run_market_analysis(
        snapshot.analysis_quotes,
        analysis_financials,
        eligible_codes=eligible_codes,
        enforce_quality=True,
        expected_companies=len(eligible_codes),
        previous_quality=_comparison_quality(snapshot),
        reporting_period_contract=reporting_period_contract,
        market_coldness_evidence=coldness_evidence,
        quality_history_evidence=quality_history_evidence,
        quality_history_loader=_bounded_quality_history_loader(
            limit=0 if reuse_evidence_only else _QUALITY_HISTORY_DECISION_BACKFILL_LIMIT
        ),
        type3_growth_loader=_bounded_type3_growth_loader(cache_only=reuse_evidence_only),
        research_report_loader=(
            partial(fetch_research_reports_batch, cache_only=True)
            if reuse_evidence_only
            else fetch_research_reports_batch
        ),
        patch4_loader=(
            partial(fetch_patch4_evidence_batch, cache_only=True)
            if reuse_evidence_only
            else fetch_patch4_evidence_batch
        ),
        commodity_cycle_evidence=commodity_cycle_evidence,
        dividend_evidence=dividend_evidence,
    )
    if analysis.issues:
        raise RuntimeError(f"whole-market analysis contains {len(analysis.issues)} pipeline issues")
    if not isinstance(analysis.quality, Mapping) or analysis.quality.get("ok") is not True:
        raise RuntimeError("whole-market analysis quality gate did not pass")
    print("MARKET_BUILD checking full-universe score and source coverage", flush=True)
    screening_coverage = _mobile_screening_coverage(analysis.scores, analysis.dcf_results)
    if replay_reference is not None:
        _require_source_input_replay(replay_reference, screening_coverage)

    active_payload_sha256 = getattr(snapshot, "baseline_payload_sha256", None)
    if snapshot.source == "network":
        saved = save_market_snapshot(
            cache,
            snapshot.quotes,
            snapshot.financials,
            data_timestamp=snapshot.data_timestamp,
            retrieved_at=snapshot.retrieved_at,
            analysis_quality=analysis.quality,
            financial_fetch_provenance=(
                snapshot.validation.get("financial_fetch")
                if isinstance(snapshot.validation.get("financial_fetch"), Mapping)
                else None
            ),
            expected_previous_timestamp=snapshot.baseline_timestamp,
            expected_previous_payload_sha256=snapshot.baseline_payload_sha256,
        )
        active_payload_sha256 = saved.get("payload_sha256")
    if not isinstance(active_payload_sha256, str):
        raise RuntimeError("active market snapshot has no verified payload identity")
    artifact = cache.read_bytes_if_payload(active_payload_sha256)
    ending_state = audit_state_hashes()
    if ending_state != starting_state:
        raise RuntimeError("source, rules, industry data, or dependencies changed during mobile publication")

    manifest = write_mobile_snapshot(
        output_dir,
        analysis.scores,
        market_as_of=market_as_of,
        data_timestamp_utc=_utc_timestamp(snapshot.data_timestamp),
        analysis_quality=analysis.quality,
        dcf_results=analysis.dcf_results,
        provenance={
            "source_commit": source_commit,
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_source": snapshot.source,
            "snapshot_payload_sha256": active_payload_sha256,
            "snapshot_artifact_sha256": hashlib.sha256(artifact).hexdigest(),
            "snapshot_validation": dict(snapshot.validation),
            "market_coldness": dict(coldness_status),
            "post_close_quote_coverage": post_close_quote_coverage,
            "screening_coverage": screening_coverage,
            "quality_history_backfill": quality_history_backfill,
            "company_evidence_mode": "cache_only" if reuse_evidence_only else "network_backfill",
            "source_state": starting_state,
            "methodology_version": METHODOLOGY_VERSION,
            "model_sources": public_model_source_contract(),
        },
    )
    _require_company_detail_manifest(manifest, len(eligible_codes))
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = publish_mobile_snapshot(
        output_dir=args.output_dir,
        refresh=bool(args.refresh),
        force_financial_fallback_refresh=bool(args.force_financial_fallback_refresh),
        refresh_financials_only=bool(args.refresh_financials_only),
        reuse_evidence_only=bool(args.reuse_evidence_only),
        reference_manifest=(
            json.loads(args.reference_manifest.read_text(encoding="utf-8"))
            if args.reference_manifest is not None
            else None
        ),
        qualitative_overlay_path=args.qualitative_overlay,
    )
    # GitHub's Windows runner may expose a cp1252 stdout even though the files
    # themselves are UTF-8. Keep the diagnostic log ASCII-only so a successful
    # publication cannot be turned into a failed job by Chinese display text.
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
