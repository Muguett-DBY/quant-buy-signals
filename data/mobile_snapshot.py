"""Compact, checksummed market snapshots for read-only mobile clients.

The desktop analysis contains the complete financial input tree and DCF audit
ledger.  Publishing that object to a phone would be slow, costly, and expose
far more raw provider data than the client needs.  This module exports a
small catalogue for the whole market plus a separate detail file for actual
or conditional candidates.  It never performs an analysis itself.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import pandas as pd

from data.public_presentation import public_industry_name, public_reason_text
from engine.buy_screener import TYPE_NAMES, validate_screening_result


SNAPSHOT_SCHEMA_VERSION = 1
MAX_COMPRESSED_ASSET_BYTES = 8_000_000
MAX_UNCOMPRESSED_ASSET_BYTES = 16_000_000
MAX_PUBLIC_REASON_UTF16_UNITS = 200
CATALOG_FILENAME = "catalog-{generation}.json.gz"
SIGNALS_FILENAME = "signals-{generation}.json.gz"
SIGNATURE_FILENAME = "manifest-{generation}.sig"
MANIFEST_FILENAME = "manifest.json"
_TYPE_KEYS = tuple(f"type{number}" for number in range(1, 8))
_PUBLIC_META_REASON_KEYS = ("_scope", "_veto", "_missing", "_condition", "_downgrade", "_risk")
_SCORELESS_STATUSES = frozenset({"not_applicable", "insufficient_evidence"})
_STATUS_LABELS = {
    "triggered": "已触发",
    "conditional": "待确认，不是买入信号",
    "observe": "观察",
    "insufficient_evidence": "资料不足",
    "vetoed": "不符合硬条件",
    "not_triggered": "未触发",
    "not_applicable": "不适用",
    "blocked": "因市场状态被阻断",
}


class MobileSnapshotError(ValueError):
    """The completed analysis cannot safely be exported to a mobile client."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _json_safe(converted)
    return str(value)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    return code.zfill(6) if code.isdigit() and len(code) <= 6 else code


def _public_reason_text(value: Any) -> str:
    """Apply the shared plain-language boundary to mobile explanations."""
    text = public_reason_text(value)
    if len(text.encode("utf-16-le")) // 2 <= MAX_PUBLIC_REASON_UTF16_UNITS:
        return text

    # Android's String.length() counts UTF-16 code units rather than Unicode
    # code points.  Truncate on code-point boundaries and reserve one unit for
    # a visible ellipsis so every generated reason satisfies the exact client
    # contract, including text containing non-BMP emoji.
    remaining = MAX_PUBLIC_REASON_UTF16_UNITS - 1
    output: list[str] = []
    for character in text:
        units = len(character.encode("utf-16-le")) // 2
        if units > remaining:
            break
        output.append(character)
        remaining -= units
    return "".join(output).rstrip() + "…"


def _compact_type(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"status": "invalid", "score": None, "triggered": False, "reason": ""}
    status = str(payload.get("status") or "invalid")
    total = None if status in _SCORELESS_STATUSES else _finite(payload.get("total"))
    reasons = payload.get("reasons")
    public_reason = ""
    if isinstance(reasons, Mapping):
        public_reason = next(
            (
                _public_reason_text(reasons[key])
                for key in _PUBLIC_META_REASON_KEYS
                if isinstance(reasons.get(key), str) and _public_reason_text(reasons[key])
            ),
            "",
        )
        if not public_reason:
            sub_scores = payload.get("sub_scores")
            ranked_dimensions = (
                sorted(
                    (
                        (float(score), order, str(key))
                        for order, (key, raw_score) in enumerate(sub_scores.items())
                        if (score := _finite(raw_score)) is not None
                    )
                )
                if isinstance(sub_scores, Mapping)
                else []
            )
            public_reason = next(
                (
                    _public_reason_text(reasons[key])
                    for _score, _order, key in ranked_dimensions
                    if isinstance(reasons.get(key), str) and _public_reason_text(reasons[key])
                ),
                "",
            )
    return {
        "status": status,
        "score": round(total, 3) if total is not None else None,
        "triggered": payload.get("triggered") is True,
        "reason": public_reason,
    }


def _public_type_detail(payload: Any) -> dict[str, Any]:
    compact = _compact_type(payload)
    if not isinstance(payload, Mapping):
        return compact
    reasons = payload.get("reasons")
    public_reasons = {}
    if isinstance(reasons, Mapping):
        sub_scores = payload.get("sub_scores")
        public_keys = [
            *_PUBLIC_META_REASON_KEYS,
            *(str(key) for key in sub_scores if isinstance(sub_scores, Mapping)),
        ]
        public_reasons = dict.fromkeys(
            key for key in public_keys if isinstance(reasons.get(key), str) and _public_reason_text(reasons[key])
        )
        public_reasons = {key: _public_reason_text(reasons[key]) for key in public_reasons}
    sub_scores = payload.get("sub_scores")
    if compact["status"] in _SCORELESS_STATUSES:
        # Scorers retain a fixed numeric shape internally so independent audit
        # replay can validate every framework.  Those zero-valued placeholders
        # are not investment scores and must not cross the public/mobile
        # boundary for an inapplicable or evidence-incomplete framework.
        sub_scores = None
    compact.update(
        {
            "sub_scores": {
                str(key): round(score, 3) for key, raw in sub_scores.items() if (score := _finite(raw)) is not None
            }
            if isinstance(sub_scores, Mapping)
            else {},
            "reasons": public_reasons,
            "veto": payload.get("veto") is True,
        }
    )
    return compact


def _catalog_company(row: Mapping[str, Any]) -> dict[str, Any]:
    type_payloads = {type_key: _compact_type(row.get(type_key)) for type_key in _TYPE_KEYS}
    buy_types = [str(value) for value in row.get("buy_types", []) if str(value) in _TYPE_KEYS]
    conditional_types = [type_key for type_key, payload in type_payloads.items() if payload["status"] == "conditional"]
    diagnostic_score = _finite(row.get("diagnostic_score"))
    if diagnostic_score is None:
        diagnostic_score = _finite(row.get("max_score"))
    raw_industry = str(row.get("industry_code") or row.get("industry") or "").strip()
    return {
        "code": _normalise_code(row.get("code")),
        "name": str(row.get("name") or ""),
        # ``industry`` is the public display contract consumed by existing
        # Android clients.  Keep the model enum separately for diagnostics so
        # values such as ``ALCOHOL`` never become an end-user label.
        "industry": public_industry_name(row.get("industry"), explicit_name=row.get("industry_cn")),
        "industry_code": raw_industry,
        "price": _finite(row.get("price")),
        "pe": _finite(row.get("pe")),
        "pb": _finite(row.get("pb")),
        "market_cap": _finite(row.get("market_cap")),
        "buy_types": buy_types,
        "conditional_types": conditional_types,
        "primary_type": str(row.get("primary_type") or ""),
        "primary_label": str(row.get("primary_label") or ""),
        "diagnostic_type": str(row.get("diagnostic_type") or ""),
        "diagnostic_label": str(row.get("diagnostic_label") or ""),
        "diagnostic_score": diagnostic_score,
        "types": type_payloads,
    }


def _signal_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    catalog = _catalog_company(row)
    type_details = {type_key: _public_type_detail(row.get(type_key)) for type_key in _TYPE_KEYS}
    detail_lines = [f"{catalog['name']} {catalog['code']}"]
    for type_key in _TYPE_KEYS:
        payload = type_details[type_key]
        status = _STATUS_LABELS.get(str(payload.get("status")), "资料异常")
        score = payload.get("score")
        score_text = "" if score is None else f"，{float(score):.1f}分"
        detail_lines.append(f"{TYPE_NAMES[type_key]}：{status}{score_text}")
        reasons = payload.get("reasons")
        if isinstance(reasons, Mapping):
            for reason in dict.fromkeys(str(value).strip() for value in reasons.values() if str(value).strip()):
                detail_lines.append(f"  说明：{reason}")
    return {
        **catalog,
        "type_details": type_details,
        "detail_text": "\n".join(detail_lines),
    }


def _type_coverage(companies: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for type_key in _TYPE_KEYS:
        statuses = Counter(str(company["types"][type_key]["status"]) for company in companies)
        result[type_key] = {
            "triggered": int(statuses.get("triggered", 0)),
            "conditional": int(statuses.get("conditional", 0)),
            "observe": int(statuses.get("observe", 0)),
            "insufficient_evidence": int(statuses.get("insufficient_evidence", 0)),
            "vetoed": int(statuses.get("vetoed", 0)),
            "not_triggered": int(statuses.get("not_triggered", 0)),
            "not_applicable": int(statuses.get("not_applicable", 0)),
            "blocked": int(statuses.get("blocked", 0)),
        }
    return result


def build_mobile_snapshot(
    scores: pd.DataFrame,
    *,
    market_as_of: str,
    data_timestamp_utc: str,
    analysis_quality: Mapping[str, Any],
    dcf_results: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build portable manifest, catalogue, and candidate-detail payloads.

    ``scores`` is the fully validated production screen result.  No external
    source is contacted, so the function is deterministic and straightforward
    to exercise in release CI.
    """
    if not isinstance(scores, pd.DataFrame) or scores.empty:
        raise MobileSnapshotError("mobile snapshot requires a non-empty score frame")
    required = {"code", "buy_types", "primary_type", "diagnostic_type", "max_score", *_TYPE_KEYS}
    missing = sorted(required - set(scores.columns))
    if missing:
        raise MobileSnapshotError("score frame omits required fields: " + ",".join(missing))
    invariant_errors = validate_screening_result(scores)
    if invariant_errors:
        raise MobileSnapshotError("score frame invariant failed: " + invariant_errors[0])
    if not isinstance(market_as_of, str) or len(market_as_of) != 10:
        raise MobileSnapshotError("market_as_of must use YYYY-MM-DD")
    if not isinstance(data_timestamp_utc, str) or not data_timestamp_utc:
        raise MobileSnapshotError("data_timestamp_utc is required")
    if not isinstance(analysis_quality, Mapping) or analysis_quality.get("ok") is not True:
        raise MobileSnapshotError("analysis quality gate did not pass")

    # The mobile client intentionally receives only compact scores and public
    # explanations.  Full valuation ledgers remain in the desktop audit and
    # are not exposed merely because callers already have them in memory.
    del dcf_results
    raw_records = scores.to_dict(orient="records")
    companies = [_catalog_company(row) for row in raw_records]
    codes = [company["code"] for company in companies]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        raise MobileSnapshotError("mobile catalogue contains invalid or duplicate codes")
    companies.sort(key=lambda item: item["code"])
    coverage = _type_coverage(companies)
    signal_codes = {company["code"] for company in companies if company["buy_types"] or company["conditional_types"]}
    raw_rows = {_normalise_code(row.get("code")): row for row in raw_records}
    signals = [_signal_detail(raw_rows[code]) for code in sorted(signal_codes)]

    try:
        generated_at = datetime.fromisoformat(data_timestamp_utc).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError) as exc:
        raise MobileSnapshotError("data_timestamp_utc must be an ISO-8601 timestamp") from exc
    shared = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "product": "DS_DCF",
        "generated_at_utc": generated_at,
        "market_as_of": market_as_of,
        "data_timestamp_utc": data_timestamp_utc,
        "type_names": dict(TYPE_NAMES),
        "analysis_quality": _json_safe(dict(analysis_quality)),
        "provenance": _json_safe(dict(provenance or {})),
    }
    catalogue = {
        **shared,
        "coverage": _type_coverage(companies),
        "company_count": len(companies),
        "companies": companies,
    }
    triggered_company_count = sum(1 for company in companies if company["buy_types"])
    conditional_company_count = sum(1 for company in companies if company["conditional_types"])
    conditional_only_company_count = sum(
        1 for company in companies if company["conditional_types"] and not company["buy_types"]
    )
    candidate_detail_count = len(signals)
    signal_payload = {
        **shared,
        # A detailed record can represent either a real buy signal or a
        # conditional candidate.  Keep the two counts separate so a client
        # never presents a missing portfolio confirmation as a buy signal.
        "triggered_company_count": triggered_company_count,
        "conditional_company_count": conditional_company_count,
        "conditional_only_company_count": conditional_only_company_count,
        "candidate_detail_count": candidate_detail_count,
        "signals": signals,
    }
    generation = hashlib.sha256(
        _canonical_json_bytes(catalogue) + b"\0" + _canonical_json_bytes(signal_payload)
    ).hexdigest()[:16]
    manifest = {
        **shared,
        "catalogue": {"filename": CATALOG_FILENAME.format(generation=generation)},
        "signals": {"filename": SIGNALS_FILENAME.format(generation=generation)},
        "signature": {
            "filename": SIGNATURE_FILENAME.format(generation=generation),
            "algorithm": "ECDSA_P256_SHA256",
        },
        "summary": {
            "company_count": len(companies),
            "triggered_company_count": triggered_company_count,
            "conditional_company_count": conditional_company_count,
            "conditional_only_company_count": conditional_only_company_count,
            "candidate_detail_count": candidate_detail_count,
            "type_coverage": coverage,
        },
    }
    return manifest, catalogue, signal_payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _gzip_bytes(raw: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as zipped:
        zipped.write(raw)
    return output.getvalue()


def write_mobile_snapshot(
    output_dir: str | Path,
    scores: pd.DataFrame,
    *,
    market_as_of: str,
    data_timestamp_utc: str,
    analysis_quality: Mapping[str, Any],
    dcf_results: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an atomically replaceable mobile snapshot and return its manifest."""
    manifest, catalogue, signals = build_mobile_snapshot(
        scores,
        market_as_of=market_as_of,
        data_timestamp_utc=data_timestamp_utc,
        analysis_quality=analysis_quality,
        dcf_results=dcf_results,
        provenance=provenance,
    )
    output = Path(output_dir)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise MobileSnapshotError("mobile snapshot output directory must be absent or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    catalogue_raw = _canonical_json_bytes(catalogue)
    signals_raw = _canonical_json_bytes(signals)
    for label, raw in (("catalogue", catalogue_raw), ("signals", signals_raw)):
        if len(raw) > MAX_UNCOMPRESSED_ASSET_BYTES:
            raise MobileSnapshotError(
                f"{label} exceeds the Android uncompressed limit: {len(raw)} > {MAX_UNCOMPRESSED_ASSET_BYTES}"
            )
    catalogue_bytes = _gzip_bytes(catalogue_raw)
    signals_bytes = _gzip_bytes(signals_raw)
    for label, compressed in (("catalogue", catalogue_bytes), ("signals", signals_bytes)):
        if len(compressed) > MAX_COMPRESSED_ASSET_BYTES:
            raise MobileSnapshotError(
                f"{label} exceeds the Android download limit: {len(compressed)} > {MAX_COMPRESSED_ASSET_BYTES}"
            )
    manifest = dict(manifest)
    manifest["catalogue"].update({"sha256": hashlib.sha256(catalogue_bytes).hexdigest(), "size": len(catalogue_bytes)})
    manifest["signals"].update({"sha256": hashlib.sha256(signals_bytes).hexdigest(), "size": len(signals_bytes)})
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent))
    committed = False
    try:
        _atomic_write(staging / manifest["catalogue"]["filename"], catalogue_bytes)
        _atomic_write(staging / manifest["signals"]["filename"], signals_bytes)
        _atomic_write(staging / MANIFEST_FILENAME, _canonical_json_bytes(manifest) + b"\n")
        if output.exists():
            output.rmdir()
        os.replace(staging, output)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(staging, ignore_errors=True)
    return manifest


__all__ = [
    "CATALOG_FILENAME",
    "MANIFEST_FILENAME",
    "MobileSnapshotError",
    "SIGNATURE_FILENAME",
    "SIGNALS_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "build_mobile_snapshot",
    "write_mobile_snapshot",
]
