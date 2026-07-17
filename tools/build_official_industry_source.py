"""Build a deterministic CAPCO industry source from the published PDF.

This is a maintenance tool, not a runtime dependency.  The production
classifier consumes the generated JSON and therefore does not need a PDF
parser installed.  The source PDF itself is intentionally not committed; its
URL and SHA-256 digest are embedded in the generated payload.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import hashlib
import json
import re
from pathlib import Path
from typing import Any


_SECURITY_CODE = re.compile(r"\d{6}")
_DIVISION_CODE = re.compile(r"\d{2}")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_capco_tables(tables: Iterable[list[list[Any]]]) -> dict[str, dict[str, str]]:
    """Parse pdfplumber table rows and reject ambiguous or duplicate records."""
    records: dict[str, dict[str, str]] = {}
    for table in tables:
        for row in table:
            if not row or len(row) < 8:
                continue
            code = _clean(row[0])
            if not _SECURITY_CODE.fullmatch(code):
                continue
            record = {
                "name": _clean(row[1]),
                "section_code": _clean(row[2]),
                "section_name": _clean(row[3]),
                "subclass_code": _clean(row[4]),
                "subclass_name": _clean(row[5]),
                "division_code": _clean(row[6]),
                "division_name": _clean(row[7]),
            }
            if not record["name"] or not record["section_code"] or not record["section_name"]:
                raise ValueError(f"CAPCO row {code} has an incomplete identity")
            if not _DIVISION_CODE.fullmatch(record["division_code"]):
                raise ValueError(f"CAPCO row {code} has an invalid division code")
            previous = records.get(code)
            if previous is not None and previous != record:
                raise ValueError(f"CAPCO code {code} has conflicting duplicate rows")
            records[code] = record
    if len(records) < 5_000:
        raise ValueError(f"CAPCO source is implausibly small: {len(records)} records")
    return dict(sorted(records.items()))


def build_payload(
    pdf_path: Path,
    *,
    source_url: str,
    title: str,
    effective_period: str,
    published_date: str,
) -> dict[str, Any]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - maintenance environment only
        raise RuntimeError("pdfplumber is required to rebuild the CAPCO source") from exc

    source_bytes = pdf_path.read_bytes()
    tables: list[list[list[Any]]] = []
    with pdfplumber.open(pdf_path) as document:
        for page in document.pages:
            tables.extend(page.extract_tables())
        page_count = len(document.pages)
    records = parse_capco_tables(tables)
    return {
        "schema_version": 1,
        "source": {
            "authority": "中国上市公司协会",
            "title": title,
            "source_url": source_url,
            "effective_period": effective_period,
            "published_date": published_date,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "page_count": page_count,
            "record_count": len(records),
        },
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--effective-period", required=True)
    parser.add_argument("--published-date", required=True)
    args = parser.parse_args()
    payload = build_payload(
        args.pdf,
        source_url=args.source_url,
        title=args.title,
        effective_period=args.effective_period,
        published_date=args.published_date,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(payload['records'])} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
