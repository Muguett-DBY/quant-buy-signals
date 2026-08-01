"""Local full-market Type 7 decision-replay scan on the cached snapshot. Deleted after use."""
import os
import sys
from pathlib import Path

sys.path.insert(0, r"E:\DEV\DS_DCF")

from data.cache import SafeFileCache
from data.fetcher import DataFetcher
from data.quality_history import load_quality_history_cache_batch_state
from data.snapshot import DEFAULT_SNAPSHOT_PATH, SNAPSHOT_SCHEMA_VERSION, get_market_snapshot
from engine.buy_screener import screen_all_types

import tools.publish_mobile_snapshot as pms
from tools.run_full_audit import _analysis_coverage_summary

OUT = Path(r"C:\Users\12031\AppData\Local\Temp\ds-dcf-repro-out")
OUT.mkdir(parents=True, exist_ok=True)
BATCH = 500


def log(msg: str) -> None:
    with open(OUT / "repro-scan.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


def empty_loader(*_args, **_kwargs):
    return {}


def main() -> int:
    cache = SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION)
    snapshot = get_market_snapshot(
        DataFetcher(enrich_listing_dates=True, force_reference_refresh=False),
        cache,
        force_refresh=False,
        persist_network=False,
    )
    market_as_of = pms._market_as_of(snapshot)
    eligible_codes = tuple(getattr(snapshot, "eligible_codes", ()))
    log(f"snapshot market_as_of={market_as_of} eligible={len(eligible_codes)}")
    coldness_evidence, _ = pms._load_market_coldness_evidence(
        snapshot, eligible_codes, force_refresh=False
    )
    log(f"coldness={len(coldness_evidence)}")
    frames = []
    for start in range(0, len(eligible_codes), BATCH):
        batch = eligible_codes[start : start + BATCH]
        quality_history_evidence, _ = load_quality_history_cache_batch_state(
            [{"code": code, "as_of": market_as_of} for code in batch]
        )
        frame = screen_all_types(
            snapshot.analysis_financials,
            snapshot.analysis_quotes,
            dcf_results={},
            output_codes=set(batch),
            market_coldness_evidence=coldness_evidence,
            quality_history_evidence=quality_history_evidence,
            quality_history_loader=pms._bounded_quality_history_loader(),
            type3_growth_loader=empty_loader,
            research_report_loader=empty_loader,
            patch4_loader=pms.fetch_patch4_evidence_batch,
        )
        frames.append(frame)
        log(f"batch at {start} done rows={len(frame)}")
    import pandas as pd

    scores = pd.concat(frames, ignore_index=True)
    log(f"total rows={len(scores)}")
    coverage = _analysis_coverage_summary(scores)
    readiness = coverage.get("goal_readiness")
    log(f"gates: {readiness}")
    contract = coverage.get("framework_evidence_contract")
    if isinstance(contract, dict):
        t7 = contract.get("type7")
        if isinstance(t7, dict):
            log("type7 contract: " + ", ".join(f"{k}={v}" for k, v in t7.items() if isinstance(v, int)))
            log("type7 invalid_decision_examples: " + str(t7.get("invalid_decision_examples")))
            log("type7 incomplete_without_reason_examples: " + str(t7.get("incomplete_without_reason_examples")))
        for key in ("type1", "type2", "type3", "type4", "type5", "type6"):
            c = contract.get(key)
            if isinstance(c, dict):
                bad = {k: v for k, v in c.items() if isinstance(v, int) and v > 0 and k not in ("rows", "applicable", "not_applicable", "evidence_complete", "evidence_incomplete", "incomplete_with_reason", "valid_sub_scores", "valid_decision", "decision_complete", "decision_incomplete", "potentially_triggerable", "decision_visible", "recall_safe", "applicable_evidence_complete")}
                if bad:
                    log(f"{key} bad: {bad} examples={c.get('invalid_decision_examples') or c.get('incomplete_without_reason_examples')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
