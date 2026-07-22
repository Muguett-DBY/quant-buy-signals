"""Build one validated whole-market mobile snapshot after the A-share close.

This command is deliberately separate from the Windows release process.  A
failed or stale upstream refresh exits non-zero before any output is written,
so the hosting workflow keeps serving the last known-good mobile result.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from zoneinfo import ZoneInfo

from data.cache import SafeFileCache
from data.fetcher import DataFetcher
from data.growth_evidence import fetch_growth_evidence_batch
from data.quality_history import fetch_quality_history_batch
from data.research_reports import fetch_research_reports_batch
from data.snapshot import DEFAULT_SNAPSHOT_PATH, SNAPSHOT_SCHEMA_VERSION, get_market_snapshot, save_market_snapshot
from data.mobile_snapshot import write_mobile_snapshot
from data.market_coldness import MARKET_COLDNESS_DECISION_READY_TIME, archive_market_coldness_session_snapshot
from engine.audit import audit_state_hashes
from engine.pipeline import run_market_analysis
from tools.run_full_audit import (
    _comparison_quality,
    _load_market_coldness_evidence,
    _refresh_completed,
    _require_market_coldness_release_evidence,
    _snapshot_reporting_period_contract,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true", help="require a fresh validated market snapshot")
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
    return datetime.now(tz=ZoneInfo("Asia/Shanghai"))


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


def publish_mobile_snapshot(*, output_dir: str | Path, refresh: bool) -> dict[str, object]:
    """Run production analysis and atomically write a client-ready snapshot."""
    if refresh and _shanghai_now().time() < MARKET_COLDNESS_DECISION_READY_TIME:
        raise RuntimeError("post-close mobile publication is not allowed before 16:15 Asia/Shanghai")
    source_commit = _source_commit()
    starting_state = audit_state_hashes()
    cache = SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION)
    snapshot = get_market_snapshot(
        DataFetcher(enrich_listing_dates=True, force_reference_refresh=refresh),
        cache,
        force_refresh=refresh,
        persist_network=False,
    )
    if not _refresh_completed(refresh, getattr(snapshot, "source", None)):
        raise RuntimeError("fresh market refresh did not complete; retaining the published mobile snapshot")
    market_as_of = _market_as_of(snapshot)
    if refresh and market_as_of != _shanghai_today():
        raise RuntimeError(
            f"fresh snapshot session {market_as_of} is not today's Shanghai session; retaining published mobile data"
        )
    post_close_quote_coverage = _require_post_close_quotes(snapshot, market_as_of) if refresh else None
    reporting_period_contract = _snapshot_reporting_period_contract(snapshot)
    eligible_codes = tuple(getattr(snapshot, "eligible_codes", ()))
    if not eligible_codes:
        raise RuntimeError("validated snapshot has no eligible Shanghai/Shenzhen companies")
    coldness_reference_artifact: dict[str, object] = {}
    coldness_archive_candidates: list[object] = []
    coldness_evidence, coldness_status = _load_market_coldness_evidence(
        snapshot,
        eligible_codes,
        force_refresh=refresh,
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
    analysis = run_market_analysis(
        snapshot.analysis_quotes,
        snapshot.analysis_financials,
        eligible_codes=eligible_codes,
        enforce_quality=True,
        expected_companies=len(eligible_codes),
        previous_quality=_comparison_quality(snapshot),
        reporting_period_contract=reporting_period_contract,
        market_coldness_evidence=coldness_evidence,
        quality_history_loader=fetch_quality_history_batch,
        type3_growth_loader=fetch_growth_evidence_batch,
        research_report_loader=fetch_research_reports_batch,
    )
    if analysis.issues:
        raise RuntimeError(f"whole-market analysis contains {len(analysis.issues)} pipeline issues")
    if not isinstance(analysis.quality, Mapping) or analysis.quality.get("ok") is not True:
        raise RuntimeError("whole-market analysis quality gate did not pass")

    active_payload_sha256 = getattr(snapshot, "baseline_payload_sha256", None)
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
            "source_state": starting_state,
        },
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = publish_mobile_snapshot(output_dir=args.output_dir, refresh=bool(args.refresh))
    # GitHub's Windows runner may expose a cp1252 stdout even though the files
    # themselves are UTF-8. Keep the diagnostic log ASCII-only so a successful
    # publication cannot be turned into a failed job by Chinese display text.
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
