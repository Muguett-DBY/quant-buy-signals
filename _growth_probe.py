"""Local Eastmoney BusinessAnalysis API probe. Deleted after use."""
import sys
from pathlib import Path

sys.path.insert(0, r"E:\DEV\DS_DCF")

from data.cache import SafeFileCache
from data.fetcher import DataFetcher
from data.growth_evidence import fetch_growth_evidence
from data.snapshot import DEFAULT_SNAPSHOT_PATH, SNAPSHOT_SCHEMA_VERSION, get_market_snapshot

OUT = Path(r"C:\Users\12031\AppData\Local\Temp\ds-dcf-repro-out")
OUT.mkdir(parents=True, exist_ok=True)

CODES = sys.argv[1:] or ["600519", "000002", "000006", "000001"]


def log(msg: str) -> None:
    with open(OUT / "growth-probe.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


def main() -> int:
    cache = SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION)
    snap = get_market_snapshot(
        DataFetcher(enrich_listing_dates=True, force_reference_refresh=False),
        cache,
        force_refresh=False,
        persist_network=False,
    )
    fin = snap.analysis_financials
    for code in CODES:
        company = fin.get(code)
        if company is None:
            log(f"{code}: no financials")
            continue
        revenue_records = [
            {"year": int(str(r["REPORT_DATE"])[:4]), "value": float(r["TOTAL_OPERATE_INCOME"])}
            for r in (company.get("revenue_history") or [])
            if isinstance(r, dict) and r.get("REPORT_DATE") and r.get("TOTAL_OPERATE_INCOME") is not None
        ]
        goodwill_records = [
            {"year": int(str(r["REPORT_DATE"])[:4]), "value": float(r["GOODWILL"])}
            for r in (company.get("balance") or [])
            if isinstance(r, dict) and r.get("REPORT_DATE") and r.get("GOODWILL") is not None
        ]
        try:
            evidence = fetch_growth_evidence(
                code,
                "2026-07-31",
                revenue_records=revenue_records,
                goodwill_records=goodwill_records,
            )
            status = evidence.segment_growth_sources.get("status")
            reason = evidence.segment_growth_sources.get("reason", "")
            count = evidence.segment_growth_sources.get("growth_source_count")
            log(f"{code}: status={status} sources={count} reason={str(reason)[:160]}")
        except Exception as exc:  # noqa: BLE001
            log(f"{code}: EXCEPTION {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
