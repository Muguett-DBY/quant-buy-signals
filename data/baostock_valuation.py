"""Bounded Baostock fallback for five-year daily valuation history.

Baostock exposes a process-global TCP session.  The adapter therefore logs in
once and queries a deterministic batch serially under one lock; callers must
not submit individual Baostock queries to a thread pool.  It is a fallback for
an unavailable primary valuation component, never a field-level mixer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from threading import Lock
from typing import Any

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS
from data.as_of import shanghai_today
from data.cache import SafeFileCache


BAOSTOCK_SOURCE_NAME = "Baostock daily valuation history"
BAOSTOCK_SOURCE_URL = "https://www.baostock.com/mainContent?file=pythonAPI.md"
BAOSTOCK_MODEL_ID = "baostock-valuation-history-v1"
BAOSTOCK_CACHE_SCHEMA_VERSION = 2
BAOSTOCK_CACHE_DIR = CACHE_DIRECTORY / "quality_history"
BAOSTOCK_CACHE_TTL_SECONDS = max(int(CACHE_TTL_SECONDS), 24 * 3600)
BAOSTOCK_MAX_BATCH_COMPANIES = 2_000
BAOSTOCK_MAX_NETWORK_QUERIES = 256
BAOSTOCK_REUSABLE_MAX_AGE_DAYS = 14
BAOSTOCK_FIELDS = (
    "date",
    "code",
    "close",
    "peTTM",
    "pbMRQ",
    "psTTM",
    "pcfNcfTTM",
    "turn",
    "tradestatus",
    "isST",
)

_CODE = re.compile(r"^[036]\d{5}$")
_CANONICAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SESSION_LOCK = Lock()


class BaostockValuationError(RuntimeError):
    """The Baostock session, query or normalized payload was invalid."""


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if _CODE.fullmatch(code) is None:
        raise ValueError("Baostock valuation code must be a Shanghai/Shenzhen six-digit code")
    return code


def _parse_date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field} must not contain a time component")
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str) and _CANONICAL_DATE.fullmatch(value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be a valid calendar date") from exc
    else:
        raise ValueError(f"{field} must use YYYY-MM-DD")
    if parsed > shanghai_today():
        raise ValueError(f"{field} cannot be in the future")
    return parsed


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _provider_code(code: str) -> str:
    return ("sh." if code.startswith("6") else "sz.") + code


def _provider_multiple(value: str, *, field: str, row_index: int) -> float | None:
    """Parse one provider multiple without turning schema drift into a gap."""

    if value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BaostockValuationError(f"Baostock row {row_index} has invalid {field}") from exc
    if not math.isfinite(number):
        raise BaostockValuationError(f"Baostock row {row_index} has invalid {field}")
    return number if number > 0 else None


def _cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{BAOSTOCK_MODEL_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "model_id": BAOSTOCK_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "start_date": _years_before(as_of, 5).isoformat(),
        "frequency": "d",
        "adjustflag": "3",
        "fields": list(BAOSTOCK_FIELDS),
        "source_url": BAOSTOCK_SOURCE_URL,
    }


def _normalise_rows(raw_rows: Sequence[Any], code: str, as_of: date) -> tuple[list[dict[str, Any]], str]:
    start = _years_before(as_of, 5)
    expected_provider_code = _provider_code(code)
    normalized: list[dict[str, Any]] = []
    canonical_raw: list[list[str]] = []
    seen_dates: set[date] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != len(BAOSTOCK_FIELDS):
            raise BaostockValuationError(f"Baostock row {index} has an invalid shape")
        values = [str(value or "").strip() for value in raw]
        if values[1] != expected_provider_code:
            raise BaostockValuationError(f"Baostock row {index} identity mismatch")
        try:
            trade_date = date.fromisoformat(values[0])
        except ValueError as exc:
            raise BaostockValuationError(f"Baostock row {index} has an invalid date") from exc
        if not start <= trade_date <= as_of or trade_date in seen_dates:
            raise BaostockValuationError("Baostock valuation dates are duplicate or outside the requested window")
        if values[8] not in {"0", "1"} or values[9] not in {"0", "1"}:
            raise BaostockValuationError(f"Baostock row {index} has an invalid status")
        seen_dates.add(trade_date)
        canonical_raw.append(values)
        if values[8] != "1":
            continue
        normalized.append(
            {
                "date": trade_date.isoformat(),
                "pe_ttm": _provider_multiple(values[3], field="peTTM", row_index=index),
                "pb_mrq": _provider_multiple(values[4], field="pbMRQ", row_index=index),
            }
        )
    normalized.sort(key=lambda item: item["date"])
    digest = hashlib.sha256(
        json.dumps(
            {"contract": _contract(code, as_of), "raw_rows": canonical_raw},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized, digest


def _cache_record(code: str, as_of: date, cache_dir: Path, ttl_seconds: int) -> dict[str, Any] | None:
    loaded = SafeFileCache(
        _cache_path(code, as_of, cache_dir),
        schema_version=BAOSTOCK_CACHE_SCHEMA_VERSION,
        ttl=ttl_seconds,
    ).load()
    value = loaded.value
    if (
        not loaded.hit
        or not isinstance(value, Mapping)
        or set(value)
        != {
            "contract",
            "raw_rows",
            "source_sha256",
            "captured_at_utc",
        }
    ):
        return None
    if value.get("contract") != _contract(code, as_of):
        return None
    source_sha256 = value.get("source_sha256")
    raw_rows = value.get("raw_rows")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None or not isinstance(raw_rows, list):
        return None
    try:
        normalized, replayed_sha256 = _normalise_rows(raw_rows, code, as_of)
    except BaostockValuationError:
        return None
    if replayed_sha256 != source_sha256:
        return None
    return {
        "available": bool(normalized),
        "rows": normalized,
        "source_sha256": source_sha256,
        "source_url": BAOSTOCK_SOURCE_URL,
        "cache_hit": True,
        "reason": "" if normalized else "empty_history",
    }


def _recent_cache_record(
    code: str,
    requested_as_of: date,
    cache_dir: Path,
    ttl_seconds: int,
) -> dict[str, Any] | None:
    pattern = re.compile(rf"^{re.escape(BAOSTOCK_MODEL_ID)}_{re.escape(code)}_(?P<as_of>\d{{8}})\.json\.gz$")
    candidates: list[tuple[date, Path]] = []
    for path in cache_dir.glob(f"{BAOSTOCK_MODEL_ID}_{code}_*.json.gz"):
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        try:
            captured_as_of = datetime.strptime(match.group("as_of"), "%Y%m%d").date()
        except ValueError:
            continue
        age = (requested_as_of - captured_as_of).days
        if 0 <= age <= BAOSTOCK_REUSABLE_MAX_AGE_DAYS:
            candidates.append((captured_as_of, path))
    for captured_as_of, path in sorted(candidates, reverse=True):
        loaded = SafeFileCache(
            path,
            schema_version=BAOSTOCK_CACHE_SCHEMA_VERSION,
            ttl=ttl_seconds,
        ).load(allow_expired=True)
        if not loaded.hit or not isinstance(loaded.value, Mapping):
            continue
        value = loaded.value
        if value.get("contract") != _contract(code, captured_as_of):
            continue
        raw_rows = value.get("raw_rows")
        source_sha256 = value.get("source_sha256")
        if not isinstance(raw_rows, list) or not isinstance(source_sha256, str):
            continue
        try:
            rows, replayed_sha256 = _normalise_rows(raw_rows, code, captured_as_of)
        except BaostockValuationError:
            continue
        if replayed_sha256 != source_sha256:
            continue
        start = _years_before(requested_as_of, 5).isoformat()
        rows = [row for row in rows if start <= str(row.get("date") or "") <= requested_as_of.isoformat()]
        if not rows:
            continue
        return {
            "available": True,
            "rows": rows,
            "source_sha256": source_sha256,
            "source_url": BAOSTOCK_SOURCE_URL,
            "cache_hit": True,
            "cache_as_of": captured_as_of.isoformat(),
            "reason": "",
        }
    return None


def _save_record(
    code: str,
    as_of: date,
    raw_rows: list[list[str]],
    source_sha256: str,
    cache_dir: Path,
    ttl_seconds: int,
) -> None:
    SafeFileCache(
        _cache_path(code, as_of, cache_dir),
        schema_version=BAOSTOCK_CACHE_SCHEMA_VERSION,
        ttl=ttl_seconds,
    ).save(
        {
            "contract": _contract(code, as_of),
            "raw_rows": raw_rows,
            "source_sha256": source_sha256,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )


def _provider_error(result: Any, *, operation: str) -> BaostockValuationError | None:
    code = str(getattr(result, "error_code", "") or "")
    if code == "0":
        return None
    message = " ".join(str(getattr(result, "error_msg", "") or "").split())
    return BaostockValuationError(f"Baostock {operation} failed: {code}:{message[:160]}")


def fetch_baostock_valuation_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    cache_dir: str | Path = BAOSTOCK_CACHE_DIR,
    cache_ttl_seconds: int = BAOSTOCK_CACHE_TTL_SECONDS,
    api: Any | None = None,
    use_cache: bool = True,
) -> dict[str, dict[str, Any]]:
    """Fetch a deterministic batch using one process-global Baostock session."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("Baostock valuation requests must be a sequence")
    if len(requests_) > BAOSTOCK_MAX_BATCH_COMPANIES:
        raise ValueError("Baostock valuation batch exceeds the company limit")
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    prepared: list[tuple[str, date]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {"code", "as_of"}:
            raise ValueError("Baostock valuation request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_date(request.get("as_of"), field="as_of")
        if code in seen:
            raise ValueError(f"Baostock valuation batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, cutoff))
    prepared.sort(key=lambda item: item[0])
    if not prepared:
        return {}

    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    missing: list[tuple[str, date]] = []
    for code, cutoff in prepared:
        cached = _cache_record(code, cutoff, directory, cache_ttl_seconds) if use_cache else None
        if cached is None and use_cache:
            cached = _recent_cache_record(code, cutoff, directory, cache_ttl_seconds)
        if cached is None:
            missing.append((code, cutoff))
        else:
            results[code] = cached
    if not missing:
        return {code: results[code] for code, _ in prepared}

    deferred = missing[BAOSTOCK_MAX_NETWORK_QUERIES:]
    missing = missing[:BAOSTOCK_MAX_NETWORK_QUERIES]
    for code, _cutoff in deferred:
        results[code] = {
            "available": False,
            "rows": [],
            "source_url": BAOSTOCK_SOURCE_URL,
            "cache_hit": False,
            "reason": "network_budget_exhausted",
        }

    if api is None:
        try:
            import baostock as api  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise BaostockValuationError("Baostock 0.9.3 is not installed") from exc

    with _SESSION_LOCK:
        login = api.login()
        login_error = _provider_error(login, operation="login")
        if login_error is not None:
            raise login_error
        try:
            for code, cutoff in missing:
                query = api.query_history_k_data_plus(
                    _provider_code(code),
                    ",".join(BAOSTOCK_FIELDS),
                    start_date=_years_before(cutoff, 5).isoformat(),
                    end_date=cutoff.isoformat(),
                    frequency="d",
                    adjustflag="3",
                )
                query_error = _provider_error(query, operation=f"query {code}")
                if query_error is not None:
                    results[code] = {
                        "available": False,
                        "rows": [],
                        "source_url": BAOSTOCK_SOURCE_URL,
                        "cache_hit": False,
                        "reason": str(query_error),
                    }
                    continue
                fields = tuple(str(value) for value in getattr(query, "fields", ()))
                if fields != BAOSTOCK_FIELDS:
                    raise BaostockValuationError(f"Baostock query {code} returned an unexpected field contract")
                raw_rows: list[Any] = []
                while query.next():
                    raw_rows.append(query.get_row_data())
                final_error = _provider_error(query, operation=f"iterate {code}")
                if final_error is not None:
                    raise final_error
                rows, source_sha256 = _normalise_rows(raw_rows, code, cutoff)
                # A successful response with no usable trading rows can be a
                # temporary provider fault.  Do not preserve it for 24 hours.
                if rows:
                    _save_record(
                        code,
                        cutoff,
                        [[str(value or "").strip() for value in row] for row in raw_rows],
                        source_sha256,
                        directory,
                        cache_ttl_seconds,
                    )
                results[code] = {
                    "available": bool(rows),
                    "rows": rows,
                    "source_sha256": source_sha256,
                    "source_url": BAOSTOCK_SOURCE_URL,
                    "cache_hit": False,
                    "reason": "" if rows else "empty_history",
                }
        finally:
            # Never let a best-effort logout hide the query/validation error
            # that explains why the fallback was rejected.
            try:
                api.logout()
            except Exception:
                pass
    return {code: results[code] for code, _ in prepared}


def load_baostock_valuation_cache_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    cache_dir: str | Path = BAOSTOCK_CACHE_DIR,
    cache_ttl_seconds: int = BAOSTOCK_CACHE_TTL_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Replay reusable Baostock captures without performing network I/O."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("Baostock valuation cache requests must be a sequence")
    if len(requests_) > BAOSTOCK_MAX_BATCH_COMPANIES:
        raise ValueError("Baostock valuation cache batch exceeds the company limit")
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    prepared: list[tuple[str, date]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {"code", "as_of"}:
            raise ValueError("Baostock valuation cache request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_date(request.get("as_of"), field="as_of")
        if code in seen:
            raise ValueError(f"Baostock valuation cache batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, cutoff))
    prepared.sort(key=lambda item: item[0])
    if not prepared:
        return {}

    directory = Path(cache_dir)
    if not directory.exists():
        return {}
    results: dict[str, dict[str, Any]] = {}
    for code, cutoff in prepared:
        cached = _cache_record(code, cutoff, directory, cache_ttl_seconds)
        if cached is None:
            cached = _recent_cache_record(code, cutoff, directory, cache_ttl_seconds)
        if cached is not None and cached.get("available") is True:
            results[code] = cached
    return {code: results[code] for code, _ in prepared if code in results}


__all__ = [
    "BAOSTOCK_FIELDS",
    "BAOSTOCK_MAX_BATCH_COMPANIES",
    "BAOSTOCK_MAX_NETWORK_QUERIES",
    "BAOSTOCK_MODEL_ID",
    "BAOSTOCK_SOURCE_NAME",
    "BAOSTOCK_SOURCE_URL",
    "BaostockValuationError",
    "fetch_baostock_valuation_batch",
    "load_baostock_valuation_cache_batch",
]
