"""Compatibility entry point for the local-only Tongdaxin segment collector.

Install ``mootdx`` only on the trusted local collector machine.  Production
GitHub runners intentionally do not install or execute this optional provider.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.evidence_bundle import DEFAULT_CACHE_ROOT, DEFAULT_CODES_FILE, main as evidence_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill segment evidence locally through Tongdaxin F10.")
    parser.add_argument("--as-of", required=True, help="closed-market evidence date (YYYY-MM-DD)")
    parser.add_argument("--codes-file", type=Path, default=DEFAULT_CODES_FILE)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args(argv)
    return evidence_main(
        [
            "collect",
            "--as-of",
            args.as_of,
            "--codes-file",
            str(args.codes_file),
            "--sources",
            "segment",
            "--segment-provider",
            "tdx",
            "--cache-root",
            str(args.cache_root),
            "--max-workers",
            str(args.max_workers),
            "--batch-size",
            str(args.batch_size),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
