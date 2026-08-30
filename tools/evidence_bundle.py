"""Collect, bundle, verify, and import local market-evidence caches.

The network-facing ``collect`` command is intended for a trusted local machine.
The ``resolve`` / ``verify`` / ``import`` commands use only the Python standard
library so GitHub Actions can validate a local bundle before dependencies are
installed.  This tool deliberately has no upload or Cloudflare publishing
command: a runner remains responsible for scoring, signing, and publishing.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = ROOT / "data" / "cache"
DEFAULT_OUTPUT_DIR = ROOT / "build" / "evidence-bundle"
DEFAULT_CODES_FILE = DEFAULT_CACHE_ROOT / "tdx3d_gap_codes.json"

SCHEMA_VERSION = 1
MODEL_ID = "ds-dcf-evidence-bundle-v1"
POINTER_NAME = "evidence-cache-pointer.json"
ALLOWED_CACHE_DIRECTORIES = (
    "commodity_cycle",
    "dividend_history",
    "exchange_financials",
    "growth_evidence",
    "industry_history",
    "investor_relations",
    "market_coldness",
    "quality_history",
    "research_reports",
)

MAX_CODES = 6_000
MAX_POINTER_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 40 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 50_000

_CODE = re.compile(r"^[036][0-9]{5}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_NAME = re.compile(r"^evidence-cache-([0-9a-f]{64})\.zip$")
_MEMBER_NAME = re.compile(
    r"^data/cache/(commodity_cycle|dividend_history|exchange_financials|growth_evidence|industry_history|"
    r"investor_relations|market_coldness|quality_history|research_reports)/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.json\.gz$"
)


class EvidenceBundleError(RuntimeError):
    """A local evidence bundle or collection contract was violated."""


def _canonical_date(value: str) -> str:
    if not isinstance(value, str):
        raise EvidenceBundleError("as_of must be a canonical YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceBundleError("as_of must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise EvidenceBundleError("as_of must be a canonical YYYY-MM-DD string")
    return value


def _canonical_commit(value: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise EvidenceBundleError("source_commit must be a lowercase 40-character Git commit")
    return value


def _git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceBundleError("could not resolve source_commit; pass --source-commit explicitly") from exc
    return _canonical_commit(result.stdout.strip().casefold())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceBundleError(f"JSON contains duplicate property: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, maximum_bytes: int) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EvidenceBundleError(f"could not read {path}") from exc
    if size <= 0 or size > maximum_bytes:
        raise EvidenceBundleError(f"JSON file size is outside the allowed range: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceBundleError(f"JSON file is invalid: {path}") from exc


def load_codes(path: str | Path) -> tuple[str, ...]:
    """Load a strict, unique A-share code list from JSON."""

    value = _read_json(Path(path), maximum_bytes=1024 * 1024)
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise EvidenceBundleError("codes file must contain a JSON array")
    if not value or len(value) > MAX_CODES:
        raise EvidenceBundleError(f"codes file must contain between 1 and {MAX_CODES} companies")
    codes: list[str] = []
    seen: set[str] = set()
    for code in value:
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            raise EvidenceBundleError(f"invalid A-share code in codes file: {code!r}")
        if code in seen:
            raise EvidenceBundleError(f"duplicate A-share code in codes file: {code}")
        seen.add(code)
        codes.append(code)
    return tuple(sorted(codes))


def _parse_sources(value: str) -> tuple[str, ...]:
    sources = tuple(part.strip().casefold() for part in value.split(",") if part.strip())
    allowed = {"segment", "quality", "research"}
    if not sources or len(sources) != len(set(sources)) or not set(sources) <= allowed:
        raise EvidenceBundleError("sources must be a unique comma-separated subset of segment,quality,research")
    return sources


def _run_company_workers(
    codes: Sequence[str],
    worker: Any,
    *,
    max_workers: int,
) -> list[tuple[str, Any, BaseException | None]]:
    results: list[tuple[str, Any, BaseException | None]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as executor:
        future_to_code = {executor.submit(worker, code): code for code in codes}
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                results.append((code, future.result(), None))
            except Exception as exc:  # One upstream/company failure must not abort the local tranche.
                results.append((code, None, exc))
    return sorted(results, key=lambda item: item[0])


def _collect_segment_eastmoney(
    codes: Sequence[str],
    as_of: date,
    cache_root: Path,
    max_workers: int,
) -> dict[str, Any]:
    from data.growth_evidence import _fetch_segment_growth_sources

    cache_dir = cache_root / "growth_evidence"

    def fetch(code: str) -> tuple[dict[str, Any], bool, str]:
        return _fetch_segment_growth_sources(code, as_of, cache_dir=cache_dir, use_cache=True)

    counts = {"complete": 0, "partial": 0, "unavailable": 0, "worker_failure": 0, "cache_hit": 0}
    for _code, result, error in _run_company_workers(codes, fetch, max_workers=min(max_workers, 8)):
        if error is not None:
            counts["worker_failure"] += 1
            continue
        evidence, cache_hit, _diagnostic = result
        status = str(evidence.get("status") or "unavailable")
        counts[status if status in {"complete", "partial", "unavailable"} else "unavailable"] += 1
        counts["cache_hit"] += int(bool(cache_hit))
    return {"provider": "eastmoney", "requested": len(codes), **counts}


def _collect_segment_tdx(
    codes: Sequence[str],
    as_of: date,
    cache_root: Path,
    max_workers: int,
    batch_size: int,
) -> dict[str, Any]:
    if importlib.util.find_spec("mootdx") is None:
        raise EvidenceBundleError(
            "the local-only Tongdaxin provider requires mootdx; install it locally with `python -m pip install mootdx`"
        )

    import data.growth_evidence as growth_evidence
    from data.tdx_segment import backfill_tdx_segments

    previous_cache_dir = growth_evidence.SEGMENT_CACHE_DIR
    growth_evidence.SEGMENT_CACHE_DIR = cache_root / "growth_evidence"
    filled: dict[str, dict[str, Any]] = {}
    try:
        for offset in range(0, len(codes), batch_size):
            requests_ = [
                {
                    "code": code,
                    "as_of": as_of.isoformat(),
                    "revenue_records": [],
                    "goodwill_records": [],
                }
                for code in codes[offset : offset + batch_size]
            ]
            filled.update(backfill_tdx_segments(requests_, max_workers=min(max_workers, 6)))
    finally:
        growth_evidence.SEGMENT_CACHE_DIR = previous_cache_dir
    return {
        "provider": "tdx",
        "requested": len(codes),
        "filled": len(filled),
        "unavailable": len(codes) - len(filled),
    }


def _collect_quality(
    codes: Sequence[str],
    as_of: date,
    cache_root: Path,
    max_workers: int,
) -> dict[str, Any]:
    from data.quality_history import MAX_BATCH_COMPANIES, fetch_quality_history_batch

    cache_dir = cache_root / "quality_history"
    counts = {"available": 0, "unavailable": 0, "worker_failure": 0, "cache_hit": 0}
    for offset in range(0, len(codes), MAX_BATCH_COMPANIES):
        tranche = tuple(codes[offset : offset + MAX_BATCH_COMPANIES])
        requests_ = [{"code": code, "as_of": as_of.isoformat()} for code in tranche]
        results = fetch_quality_history_batch(
            requests_,
            max_workers=min(max_workers, 8),
            cache_dir=cache_dir,
        )
        for code in tranche:
            result = results.get(code)
            if not isinstance(result, Mapping):
                counts["worker_failure"] += 1
                continue
            counts["available" if result.get("available") is True else "unavailable"] += 1
            counts["cache_hit"] += int(result.get("cache_hit") is True)
    return {"requested": len(codes), **counts}


def _collect_research(
    codes: Sequence[str],
    as_of: date,
    cache_root: Path,
    max_workers: int,
) -> dict[str, Any]:
    from data.research_reports import fetch_research_reports

    cache_dir = cache_root / "research_reports"

    def fetch(code: str) -> Any:
        return fetch_research_reports(code, as_of, cache_dir=cache_dir, use_cache=True)

    counts = {"available": 0, "unavailable": 0, "worker_failure": 0, "cache_hit": 0}
    for _code, result, error in _run_company_workers(codes, fetch, max_workers=min(max_workers, 2)):
        if error is not None:
            counts["worker_failure"] += 1
            continue
        counts["available" if result.available else "unavailable"] += 1
        counts["cache_hit"] += int(bool(result.cache_hit))
    return {"requested": len(codes), **counts}


def collect_evidence(
    codes: Sequence[str],
    *,
    as_of: str,
    sources: Sequence[str],
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    segment_provider: str = "eastmoney",
    max_workers: int = 4,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Collect selected evidence sources into their production cache formats."""

    cutoff = date.fromisoformat(_canonical_date(as_of))
    if not codes or len(codes) > MAX_CODES or any(_CODE.fullmatch(code) is None for code in codes):
        raise EvidenceBundleError("codes must be a non-empty valid A-share sequence")
    if len(codes) != len(set(codes)):
        raise EvidenceBundleError("codes must not contain duplicates")
    normalized_sources = _parse_sources(",".join(sources))
    if segment_provider not in {"eastmoney", "tdx"}:
        raise EvidenceBundleError("segment_provider must be eastmoney or tdx")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
        raise EvidenceBundleError("max_workers must be between 1 and 8")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 200:
        raise EvidenceBundleError("batch_size must be between 1 and 200")

    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "model_id": MODEL_ID,
        "as_of": cutoff.isoformat(),
        "company_count": len(codes),
        "cache_root": str(root.resolve()),
        "sources": {},
    }
    ordered_codes = tuple(sorted(codes))
    if "segment" in normalized_sources:
        if segment_provider == "tdx":
            summary["sources"]["segment"] = _collect_segment_tdx(ordered_codes, cutoff, root, max_workers, batch_size)
        else:
            summary["sources"]["segment"] = _collect_segment_eastmoney(ordered_codes, cutoff, root, max_workers)
    if "quality" in normalized_sources:
        summary["sources"]["quality"] = _collect_quality(ordered_codes, cutoff, root, max_workers)
    if "research" in normalized_sources:
        summary["sources"]["research"] = _collect_research(ordered_codes, cutoff, root, max_workers)
    return summary


def _member_path(directory: str, filename: str) -> str:
    member = PurePosixPath("data", "cache", directory, filename).as_posix()
    if _MEMBER_NAME.fullmatch(member) is None:
        raise EvidenceBundleError(f"cache filename is outside the bundle whitelist: {member}")
    return member


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bundle_evidence(
    *,
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    as_of: str,
    source_commit: str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Create a deterministic immutable ZIP and its mutable pointer manifest."""

    cutoff = _canonical_date(as_of)
    commit = _canonical_commit(source_commit) if source_commit is not None else _git_head()
    cache = Path(cache_root)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    files: list[tuple[str, Path]] = []
    for directory in ALLOWED_CACHE_DIRECTORIES:
        source_dir = cache / directory
        if not source_dir.exists():
            continue
        if not source_dir.is_dir() or source_dir.is_symlink():
            raise EvidenceBundleError(f"cache source is not a regular directory: {source_dir}")
        for source in source_dir.glob("*.json.gz"):
            if not source.is_file() or source.is_symlink():
                raise EvidenceBundleError(f"cache member is not a regular file: {source}")
            files.append((_member_path(directory, source.name), source))
    files.sort(key=lambda item: item[0])
    if not files:
        raise EvidenceBundleError("no whitelisted evidence cache files were found")
    if len(files) > MAX_MEMBERS:
        raise EvidenceBundleError("evidence bundle exceeds the member-count limit")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".evidence-cache-", suffix=".zip.tmp", dir=output)
    os.close(descriptor)
    temporary = Path(temporary_name)
    members: list[dict[str, Any]] = []
    total_size = 0
    try:
        # Cache members are already gzip-compressed; storing them avoids a
        # second CPU-heavy compression pass and keeps the bundle reproducible.
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for member_path, source in files:
                payload = source.read_bytes()
                size = len(payload)
                if size <= 0 or size > MAX_MEMBER_BYTES:
                    raise EvidenceBundleError(f"cache member size is outside the allowed range: {member_path}")
                total_size += size
                if total_size > MAX_TOTAL_MEMBER_BYTES:
                    raise EvidenceBundleError("evidence bundle exceeds the total member-size limit")
                digest = hashlib.sha256(payload).hexdigest()
                info = zipfile.ZipInfo(member_path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = (0o100644 & 0xFFFF) << 16
                archive.writestr(info, payload, compress_type=zipfile.ZIP_STORED)
                members.append({"path": member_path, "size": size, "sha256": digest})
        archive_size = temporary.stat().st_size
        if archive_size <= 0 or archive_size > MAX_BUNDLE_BYTES:
            raise EvidenceBundleError("evidence ZIP size is outside the allowed range")
        archive_sha256 = _sha256_file(temporary)
        bundle_name = f"evidence-cache-{archive_sha256}.zip"
        bundle_path = output / bundle_name
        if bundle_path.exists():
            if bundle_path.stat().st_size != archive_size or _sha256_file(bundle_path) != archive_sha256:
                raise EvidenceBundleError(f"existing content-addressed bundle is inconsistent: {bundle_path}")
            temporary.unlink()
        else:
            os.replace(temporary, bundle_path)
    finally:
        temporary.unlink(missing_ok=True)

    pointer = {
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "source_commit": commit,
        "as_of": cutoff,
        "bundle": {
            "path": bundle_name,
            "size": archive_size,
            "sha256": archive_sha256,
            "member_count": len(members),
        },
        "members": members,
    }
    pointer_path = output / POINTER_NAME
    pointer_payload = (json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    _atomic_write(pointer_path, pointer_payload)
    return bundle_path, pointer_path, pointer


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise EvidenceBundleError(f"{label} fields do not match the contract")


def _positive_int(value: Any, *, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise EvidenceBundleError(f"{label} is outside the allowed range")
    return value


def _validate_pointer_value(
    value: Any,
    *,
    expected_source_commit: str | None = None,
    expected_as_of: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceBundleError("pointer manifest must be a JSON object")
    _require_exact_fields(
        value,
        {"schema_version", "model_id", "source_commit", "as_of", "bundle", "members"},
        "pointer manifest",
    )
    if value.get("schema_version") != SCHEMA_VERSION or value.get("model_id") != MODEL_ID:
        raise EvidenceBundleError("pointer manifest schema/model is unsupported")
    commit = _canonical_commit(value.get("source_commit"))
    cutoff = _canonical_date(value.get("as_of"))
    if expected_source_commit is not None and commit != _canonical_commit(expected_source_commit):
        raise EvidenceBundleError("pointer source_commit does not match the expected revision")
    if expected_as_of is not None and cutoff != _canonical_date(expected_as_of):
        raise EvidenceBundleError("pointer as_of does not match the expected date")

    bundle = value.get("bundle")
    if not isinstance(bundle, Mapping):
        raise EvidenceBundleError("pointer bundle must be an object")
    _require_exact_fields(bundle, {"path", "size", "sha256", "member_count"}, "pointer bundle")
    bundle_name = bundle.get("path")
    bundle_sha256 = bundle.get("sha256")
    if not isinstance(bundle_name, str) or (match := _BUNDLE_NAME.fullmatch(bundle_name)) is None:
        raise EvidenceBundleError("pointer bundle path is not a content-addressed ZIP filename")
    if not isinstance(bundle_sha256, str) or _SHA256.fullmatch(bundle_sha256) is None:
        raise EvidenceBundleError("pointer bundle sha256 is invalid")
    if match.group(1) != bundle_sha256:
        raise EvidenceBundleError("pointer bundle filename and sha256 disagree")
    _positive_int(bundle.get("size"), maximum=MAX_BUNDLE_BYTES, label="pointer bundle size")
    member_count = _positive_int(bundle.get("member_count"), maximum=MAX_MEMBERS, label="pointer member_count")

    raw_members = value.get("members")
    if not isinstance(raw_members, list) or len(raw_members) != member_count:
        raise EvidenceBundleError("pointer members do not match member_count")
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_size = 0
    for raw_member in raw_members:
        if not isinstance(raw_member, Mapping):
            raise EvidenceBundleError("pointer member must be an object")
        _require_exact_fields(raw_member, {"path", "size", "sha256"}, "pointer member")
        path = raw_member.get("path")
        sha256 = raw_member.get("sha256")
        if not isinstance(path, str) or _MEMBER_NAME.fullmatch(path) is None:
            raise EvidenceBundleError(f"pointer member path is outside the whitelist: {path!r}")
        if path in seen:
            raise EvidenceBundleError(f"pointer contains duplicate member: {path}")
        seen.add(path)
        size = _positive_int(raw_member.get("size"), maximum=MAX_MEMBER_BYTES, label=f"member size for {path}")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise EvidenceBundleError(f"member sha256 is invalid: {path}")
        total_size += size
        if total_size > MAX_TOTAL_MEMBER_BYTES:
            raise EvidenceBundleError("pointer exceeds the total member-size limit")
        members.append({"path": path, "size": size, "sha256": sha256})
    if [member["path"] for member in members] != sorted(seen):
        raise EvidenceBundleError("pointer members must be in canonical path order")
    return {
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "source_commit": commit,
        "as_of": cutoff,
        "bundle": {
            "path": bundle_name,
            "size": bundle["size"],
            "sha256": bundle_sha256,
            "member_count": member_count,
        },
        "members": members,
    }


def load_pointer(
    pointer_path: str | Path,
    *,
    expected_source_commit: str | None = None,
    expected_as_of: str | None = None,
) -> dict[str, Any]:
    """Read and validate a pointer without trusting its bundle filename yet."""

    value = _read_json(Path(pointer_path), maximum_bytes=MAX_POINTER_BYTES)
    return _validate_pointer_value(
        value,
        expected_source_commit=expected_source_commit,
        expected_as_of=expected_as_of,
    )


def resolve_pointer(
    pointer_path: str | Path,
    *,
    expected_source_commit: str | None = None,
    expected_as_of: str | None = None,
) -> str:
    """Return the already-whitelisted immutable ZIP filename from a pointer."""

    pointer = load_pointer(
        pointer_path,
        expected_source_commit=expected_source_commit,
        expected_as_of=expected_as_of,
    )
    return str(pointer["bundle"]["path"])


def _verify_archive(pointer: Mapping[str, Any], bundle_path: Path, *, extraction_root: Path | None = None) -> None:
    expected_bundle = pointer["bundle"]
    try:
        bundle_size = bundle_path.stat().st_size
    except OSError as exc:
        raise EvidenceBundleError(f"could not read evidence bundle: {bundle_path}") from exc
    if bundle_path.name != expected_bundle["path"]:
        raise EvidenceBundleError("bundle filename does not match the pointer")
    if bundle_size != expected_bundle["size"] or _sha256_file(bundle_path) != expected_bundle["sha256"]:
        raise EvidenceBundleError("bundle size or sha256 does not match the pointer")

    expected_members = pointer["members"]
    try:
        with zipfile.ZipFile(bundle_path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if names != [member["path"] for member in expected_members] or len(names) != len(set(names)):
                raise EvidenceBundleError("ZIP members do not exactly match the pointer")
            for info, member in zip(infos, expected_members, strict=True):
                if info.is_dir() or info.flag_bits & 0x1 or info.compress_type != zipfile.ZIP_STORED:
                    raise EvidenceBundleError(f"ZIP member has forbidden attributes: {info.filename}")
                if info.file_size != member["size"]:
                    raise EvidenceBundleError(f"ZIP member size does not match the pointer: {info.filename}")
                destination = extraction_root / PurePosixPath(info.filename) if extraction_root is not None else None
                if destination is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                observed = 0
                first_bytes = b""
                with archive.open(info, "r") as source:
                    target = destination.open("wb") if destination is not None else None
                    try:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            if len(first_bytes) < 2:
                                first_bytes += chunk[: 2 - len(first_bytes)]
                            observed += len(chunk)
                            if observed > member["size"]:
                                raise EvidenceBundleError(f"ZIP member exceeds its declared size: {info.filename}")
                            digest.update(chunk)
                            if target is not None:
                                target.write(chunk)
                    finally:
                        if target is not None:
                            target.close()
                if observed != member["size"] or digest.hexdigest() != member["sha256"]:
                    raise EvidenceBundleError(f"ZIP member digest does not match the pointer: {info.filename}")
                if first_bytes != b"\x1f\x8b":
                    raise EvidenceBundleError(f"cache member is not gzip data: {info.filename}")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, EvidenceBundleError):
            raise
        raise EvidenceBundleError("evidence bundle is not a valid bounded ZIP") from exc


def verify_evidence_bundle(
    pointer_path: str | Path,
    bundle_path: str | Path,
    *,
    expected_source_commit: str | None = None,
    expected_as_of: str | None = None,
) -> dict[str, Any]:
    """Verify pointer, archive digest, member whitelist, sizes, and hashes."""

    pointer = load_pointer(
        pointer_path,
        expected_source_commit=expected_source_commit,
        expected_as_of=expected_as_of,
    )
    _verify_archive(pointer, Path(bundle_path))
    return pointer


def import_evidence_bundle(
    pointer_path: str | Path,
    bundle_path: str | Path,
    *,
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    expected_source_commit: str | None = None,
    expected_as_of: str | None = None,
) -> dict[str, Any]:
    """Verify into a temporary whitelist tree, then atomically merge each file."""

    pointer = load_pointer(
        pointer_path,
        expected_source_commit=expected_source_commit,
        expected_as_of=expected_as_of,
    )
    target_root = Path(cache_root)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    imported = 0
    with tempfile.TemporaryDirectory(prefix=".evidence-import-", dir=target_root.parent) as temporary_name:
        staging_root = Path(temporary_name)
        _verify_archive(pointer, Path(bundle_path), extraction_root=staging_root)
        for member in pointer["members"]:
            relative = PurePosixPath(member["path"])
            source = staging_root.joinpath(*relative.parts)
            destination = target_root.joinpath(*relative.parts[2:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            imported += 1
    return {
        "model_id": MODEL_ID,
        "source_commit": pointer["source_commit"],
        "as_of": pointer["as_of"],
        "bundle": pointer["bundle"]["path"],
        "imported_members": imported,
    }


def _json_print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect local evidence and exchange it with CI as a verified content-addressed bundle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="collect selected evidence into production cache files")
    collect_parser.add_argument("--as-of", required=True, help="closed-market evidence date (YYYY-MM-DD)")
    collect_parser.add_argument("--codes-file", type=Path, required=True, help="JSON array of six-digit A-share codes")
    collect_parser.add_argument("--sources", default="segment,quality,research")
    collect_parser.add_argument("--segment-provider", choices=("eastmoney", "tdx"), default="eastmoney")
    collect_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    collect_parser.add_argument("--max-workers", type=int, default=4)
    collect_parser.add_argument("--batch-size", type=int, default=50, help="Tongdaxin batch size (1-200)")

    bundle_parser = subparsers.add_parser("bundle", help="build an immutable ZIP and pointer manifest")
    bundle_parser.add_argument("--as-of", required=True)
    bundle_parser.add_argument("--source-commit")
    bundle_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    bundle_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)

    for command, help_text in (
        ("resolve", "validate a pointer and print its whitelisted immutable ZIP filename"),
        ("verify", "verify a pointer and every ZIP member without importing"),
        ("import", "verify and atomically merge a bundle into the evidence cache"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("--pointer", type=Path, required=True)
        child.add_argument("--expected-source-commit")
        child.add_argument("--expected-as-of")
        if command != "resolve":
            child.add_argument("--bundle", type=Path, required=True)
        if command == "import":
            child.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            summary = collect_evidence(
                load_codes(args.codes_file),
                as_of=args.as_of,
                sources=_parse_sources(args.sources),
                cache_root=args.cache_root,
                segment_provider=args.segment_provider,
                max_workers=args.max_workers,
                batch_size=args.batch_size,
            )
            _json_print(summary)
        elif args.command == "bundle":
            bundle_path, pointer_path, pointer = bundle_evidence(
                cache_root=args.cache_root,
                output_dir=args.output_dir,
                as_of=args.as_of,
                source_commit=args.source_commit,
            )
            _json_print(
                {
                    "bundle": str(bundle_path.resolve()),
                    "pointer": str(pointer_path.resolve()),
                    "sha256": pointer["bundle"]["sha256"],
                    "member_count": pointer["bundle"]["member_count"],
                }
            )
        elif args.command == "resolve":
            print(
                resolve_pointer(
                    args.pointer,
                    expected_source_commit=args.expected_source_commit,
                    expected_as_of=args.expected_as_of,
                )
            )
        elif args.command == "verify":
            pointer = verify_evidence_bundle(
                args.pointer,
                args.bundle,
                expected_source_commit=args.expected_source_commit,
                expected_as_of=args.expected_as_of,
            )
            _json_print(
                {
                    "bundle": pointer["bundle"]["path"],
                    "member_count": pointer["bundle"]["member_count"],
                    "verified": True,
                }
            )
        elif args.command == "import":
            _json_print(
                import_evidence_bundle(
                    args.pointer,
                    args.bundle,
                    cache_root=args.cache_root,
                    expected_source_commit=args.expected_source_commit,
                    expected_as_of=args.expected_as_of,
                )
            )
        else:  # pragma: no cover - argparse owns the command set.
            raise EvidenceBundleError(f"unsupported command: {args.command}")
    except EvidenceBundleError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
