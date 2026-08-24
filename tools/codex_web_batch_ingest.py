"""Persist Codex web-search batch envelopes for a resumable local run.

The Codex ``web__run`` tool is interactive, so the runner feeds one JSON line
per completed batch through stdin.  This small writer keeps the raw response,
query list, and timestamp without interpreting or fabricating provider-native
events.  It is intentionally retrieval-only; a separate verifier must turn
these envelopes into a publishable review artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _write_line(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "codex-web-batches.jsonl"
    count = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("batch envelope must be an object")
        if not isinstance(value.get("batch_index"), int):
            raise ValueError("batch_index must be an integer")
        if not isinstance(value.get("queries"), list) or not value["queries"]:
            raise ValueError("queries must be a non-empty list")
        if "raw_response" not in value:
            raise ValueError("raw_response is required")
        _write_line(path, value)
        count += 1
        print(json.dumps({"accepted": count, "batch_index": value["batch_index"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
