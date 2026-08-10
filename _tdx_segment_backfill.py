"""Local Tongdaxin segment backfill for Type 3 3d-gap companies.

Run on a residential IP where the Tongdaxin TCP channel is reachable, then
upload data/cache/growth_evidence/type3-segment-growth-v1_*.json.gz for the
new codes as segment-cache-latest.zip so CI pre-warms them.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from data.tdx_segment import backfill_tdx_segments  # noqa: E402

CODES_FILE = ROOT / "data" / "cache" / "tdx3d_gap_codes.json"
BATCH = 50


def main() -> None:
    codes = json.loads(CODES_FILE.read_text(encoding="utf-8"))
    print(f"[tdx-backfill] {len(codes)} codes", flush=True)
    total_filled = 0
    total_missing = 0
    for start in range(0, len(codes), BATCH):
        batch_codes = codes[start : start + BATCH]
        requests = [
            {
                "code": code,
                "as_of": "2026-08-10",
                "revenue_records": [{"year": 2025, "value": 1.0}],
                "goodwill_records": [],
            }
            for code in batch_codes
        ]
        t0 = time.time()
        filled = backfill_tdx_segments(requests, max_workers=6)
        elapsed = time.time() - t0
        total_filled += len(filled)
        total_missing += len(batch_codes) - len(filled)
        print(
            f"[tdx-backfill] batch {start//BATCH+1}: {len(filled)}/{len(batch_codes)} filled "
            f"in {elapsed:.1f}s (cumulative filled={total_filled} missing={total_missing})",
            flush=True,
        )
    print(f"[tdx-backfill] DONE filled={total_filled} missing={total_missing}", flush=True)


if __name__ == "__main__":
    main()
