"""Run a bounded point-in-time Shenwan classification drift audit."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
from typing import Any

from data.as_of import shanghai_today
from data.shenwan_industry_history import (
    MODEL_ID,
    ShenwanIndustryResolution,
    audit_shenwan_industry_drift,
    resolve_shenwan_industry_history,
)


def _codes(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = [line.strip() for line in raw.splitlines() if line.strip()]
    if not isinstance(payload, list) or not all(isinstance(value, str) for value in payload):
        raise ValueError("codes file must be a JSON string list or one code per line")
    if not payload or len(payload) > 6_000:
        raise ValueError("codes file must contain between 1 and 6000 codes")
    return payload


def build_audit_report(
    resolution: ShenwanIndustryResolution,
    *,
    from_as_of: str,
    to_as_of: str,
) -> dict[str, Any]:
    rows = audit_shenwan_industry_drift(
        resolution.records,
        resolution.requested_codes,
        from_as_of=from_as_of,
        to_as_of=to_as_of,
    )
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "model_id": MODEL_ID,
        "purpose": "point_in_time_peer_audit_only_no_model_taxonomy_override",
        "from_as_of": from_as_of,
        "to_as_of": to_as_of,
        "requested_count": len(resolution.requested_codes),
        "resolved_count": len(resolution.requested_codes) - len(resolution.unresolved_codes),
        "unresolved_codes": list(resolution.unresolved_codes),
        "primary_source_available": resolution.primary_source_available,
        "fallback_codes": list(resolution.fallback_codes),
        "source_errors": dict(resolution.source_errors),
        "status_counts": dict(sorted(status_counts.items())),
        "rows": list(rows),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes-file", type=Path, required=True)
    parser.add_argument("--from-as-of", required=True)
    parser.add_argument("--to-as-of", default=shanghai_today().isoformat())
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    codes = _codes(args.codes_file)
    resolution = resolve_shenwan_industry_history(codes, args.to_as_of)
    report = build_audit_report(
        resolution,
        from_as_of=args.from_as_of,
        to_as_of=args.to_as_of,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if not resolution.unresolved_codes else 2


if __name__ == "__main__":
    raise SystemExit(main())
