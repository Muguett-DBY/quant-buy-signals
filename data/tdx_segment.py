"""mootdx (通达信 TCP) business-segment fallback for Type 3 growth evidence.

The primary Eastmoney business-composition endpoint is rate-limited hard on
GitHub runner IPs.  The same per-company business-segment history (revenue by
industry / product / region) is served by the Tongdaxin TCP channel via the
``F10(name="经营分析")`` text block, which is not IP-rate-limited.  This module
parses that text into the exact ``_NORMALIZED_SEGMENT_FIELDS`` record schema so
the existing segment-evidence pipeline (``_build_segment_growth_sources`` and
friends in data/growth_evidence.py) can consume it unchanged.

The Tongdaxin F10 经营分析 block carries up to four report periods (two
annual 12-31 rows plus two interim rows).  Only the annual rows are emitted so
the records satisfy the cache/source contract (``-12-31`` report dates); a
company with fewer than ``MIN_SEGMENT_HISTORY_YEARS`` annual rows yields a
``partial`` status instead of ``unavailable``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

_SECUCODE = re.compile(r"^(\d{6})\.(SH|SZ)$")
_ITEM_LINE = re.compile(r"｜\s*([^｜]+?)\s*\((行业|产品|地区)\)\s*｜\s*([^｜]+?)\s*｜\s*([^｜]+?)\s*｜")
_CUTOFF = re.compile(r"【截止日期】(\d{4}-\d{2}-\d{2})")
_DIMENSION_BY_LABEL = {"行业": 1, "产品": 2, "地区": 3}

_ANNUAL_SUFFIX = "-12-31"
_MIN_ANNUAL_ROWS = 2


class TdxSegmentError(RuntimeError):
    """Base error for the Tongdaxin segment fallback."""


@dataclass(frozen=True)
class TdxSegmentOutcome:
    records: tuple[dict[str, Any], ...]
    periods: tuple[str, ...]
    raw_sha256: str | None = None
    diagnostic: str = ""

    def as_list(self) -> list[dict[str, Any]]:
        return list(self.records)


def _secucode(code: str) -> str:
    if not re.fullmatch(r"\d{6}", code):
        raise TdxSegmentError(f"invalid A-share code: {code}")
    return f"{code}.{'SH' if code.startswith('6') else 'SZ'}"


def _parse_money(value: str) -> Decimal | None:
    """Parse a Tongdaxin money cell like 18.9566亿 / 7299.1396万 / - / 0."""
    text = (value or "").strip().replace(",", "")
    if not text or text in {"-", "--", "——"}:
        return None
    multiplier = Decimal(1)
    if text.endswith("亿"):
        multiplier = Decimal("100000000")
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = Decimal("10000")
        text = text[:-1]
    elif text.endswith("元"):
        text = text[:-1]
    try:
        return Decimal(text) * multiplier
    except Exception:
        return None


def _parse_share(value: str) -> Decimal | None:
    text = (value or "").strip().replace("%", "").replace(",", "")
    if not text or text in {"-", "--", "——"}:
        return None
    try:
        return Decimal(text) / Decimal(100)
    except Exception:
        return None


def parse_f10_segments(text: str, code: str) -> TdxSegmentOutcome:
    """Extract annual business-segment rows from a F10 经营分析 block.

    The block contains several ``【截止日期】YYYY-MM-DD`` sections; each is a
    table of ``项目名(行业|产品|地区) 营业收入 收入比例 ...``.  Only the annual
    (12-31) sections are emitted, mirroring the Eastmoney source contract.
    """
    if not isinstance(text, str) or not text.strip():
        return TdxSegmentOutcome((), (), diagnostic="empty_f10_text")
    records: list[dict[str, Any]] = []
    periods: list[str] = []
    seen: set[tuple[str, int, str]] = set()
    secucode = _secucode(code)
    # Split on 【截止日期】 markers; the first chunk is the header.
    chunks = re.split(r"【截止日期】", text)
    for chunk in chunks[1:]:
        report_date = chunk[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date) or not report_date.endswith(_ANNUAL_SUFFIX):
            continue
        # Each item line: ｜name(维度)｜收入｜比例｜成本｜...｜
        for match in _ITEM_LINE.finditer(chunk):
            name_raw = match.group(1).strip()
            dimension_label = match.group(2)
            revenue_raw = match.group(3).strip()
            share_raw = match.group(4).strip()
            if name_raw in {"合计", ""}:
                continue
            dimension = _DIMENSION_BY_LABEL.get(dimension_label)
            if dimension is None:
                continue
            revenue = _parse_money(revenue_raw)
            if revenue is None:
                continue
            share = _parse_share(share_raw)
            identity = (report_date, dimension, name_raw.casefold())
            if identity in seen:
                continue
            seen.add(identity)
            records.append(
                {
                    "security_code": code,
                    "secucode": secucode,
                    "report_date": report_date,
                    "mainop_type": dimension,
                    "dimension": {1: "industry", 2: "product", 3: "region"}[dimension],
                    "item_name": name_raw,
                    "revenue": float(revenue),
                    "reported_share": float(share) if share is not None else None,
                    "rank": sum(1 for _ in records if False) + 1,  # replaced below
                }
            )
    # Assign deterministic per-(date, dimension) ranks in table order.
    ranked: list[dict[str, Any]] = []
    counts: dict[tuple[str, int], int] = {}
    for record in records:
        key = (record["report_date"], record["mainop_type"])
        counts[key] = counts.get(key, 0) + 1
        record["rank"] = counts[key]
        ranked.append(record)
    periods = sorted({record["report_date"] for record in ranked})
    if not ranked:
        return TdxSegmentOutcome((), periods, diagnostic="no_annual_segment_rows")
    return TdxSegmentOutcome(tuple(ranked), tuple(periods), diagnostic="")


def tdx_segment_records(records: tuple[dict[str, Any], ...] | list[dict[str, Any]], code: str) -> list[dict[str, Any]]:
    """Validate and normalise parsed records against the shared schema."""
    from data.growth_evidence import _validate_cached_segment_records

    today = date.today()
    as_of = today.replace(year=today.year - 1) if (today.month, today.day) < (5, 1) else today
    return _validate_cached_segment_records(list(records), code=code, as_of=as_of)


def _evidence_record(code: str, as_of: date, segment: dict[str, Any]) -> dict[str, Any]:
    """Wrap a segment evidence payload in the loader record contract.

    The external (acquisition/goodwill) component stays explicitly unavailable:
    Tongdaxin serves business composition but not the cash-flow/goodwill
    history, so ``available`` is False and the reason reflects both child
    statuses exactly as ``validate_growth_evidence_record`` demands.
    """
    from data.growth_evidence import (
        MODEL_ID,
        _unavailable_external_evidence,
        validate_growth_evidence_record,
    )

    external = _unavailable_external_evidence(code, as_of, "no external growth evidence")
    reasons: list[str] = []
    if segment.get("status") != "complete":
        reasons.append(f"segment:{segment.get('reason') or 'partial'}")
    if external.get("status") != "complete":
        reasons.append(f"external:{external.get('reason') or 'unavailable'}")
    record = {
        "available": False,
        "code": code,
        "as_of": as_of.isoformat(),
        "model_id": MODEL_ID,
        "external_growth_evidence": external,
        "segment_growth_sources": segment,
        "cache_hit": True,
        "cache_diagnostic": "tdx_f10_backfill",
        "reason": ";".join(reasons),
    }
    return validate_growth_evidence_record(record, code, as_of)


def _write_tdx_cache(code: str, as_of: date, records: list[dict[str, Any]]) -> None:
    """Persist parsed segment rows in the shared segment-cache format."""
    from data.cache import SafeFileCache
    from data.growth_evidence import (
        CACHE_SCHEMA_VERSION,
        CACHE_TTL_SECONDS,
        MAX_RESPONSE_BYTES,
        SEGMENT_CACHE_DIR,
        _segment_cache_contract,
        _segment_cache_path,
    )

    try:
        path = _segment_cache_path(code, as_of, SEGMENT_CACHE_DIR)
        cache = SafeFileCache(
            path,
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CACHE_TTL_SECONDS,
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        cache.save({"contract": _segment_cache_contract(code, as_of), "records": records})
    except Exception:
        # Cache persistence is an optimisation; the live evidence is already
        # returned to the caller.
        return


def _load_tdx_cache(code: str, as_of: date) -> dict[str, Any] | None:
    """Return a cached Tongdaxin segment evidence record, partial or complete.

    The cache is keyed by its capture ``as_of``; the build requests the closed
    market session (e.g. 2026-08-07) while the local backfill may have captured
    on a later day (e.g. 2026-08-10).  Scan the same 21-day reuse window the
    Eastmoney path uses so a same-fiscal-year capture is reused.
    """
    from data.cache import SafeFileCache
    from data.growth_evidence import (
        CACHE_SCHEMA_VERSION,
        CACHE_TTL_SECONDS,
        MAX_RESPONSE_BYTES,
        SEGMENT_CACHE_DIR,
        SEGMENT_CACHE_REUSE_DAYS,
        _build_segment_growth_sources,
        _latest_completed_annual_year,
        _segment_cache_contract,
        _segment_cache_index,
        _validate_cached_segment_records,
        _validate_segment_evidence,
    )

    try:
        indexed = _segment_cache_index(SEGMENT_CACHE_DIR)
        entries = list(indexed.get(code, ()))
        # Prefer the newest capture at-or-before the requested as_of (a
        # same-fiscal-year restatement after a later capture must not win);
        # fall back to newer captures only when no at-or-before capture is
        # usable (pass 1 returned nothing because it was empty or every
        # at-or-before entry failed the reuse window / fiscal-year gate).
        candidates = sorted(entries, key=lambda item: item[0], reverse=True)
        at_or_before = sorted([item for item in candidates if item[0] <= as_of], key=lambda item: item[0], reverse=True)
        ordered = at_or_before + [item for item in candidates if item[0] > as_of]
        seen_paths: set[str] = set()
        for source_as_of, path in ordered:
            if str(path) in seen_paths:
                continue
            seen_paths.add(str(path))
            age_days = (as_of - source_as_of).days
            # The capture may be older (reuse within 21 days) or newer (the
            # local backfill ran after the closed session the build re-scores);
            # either is fine as long as the fiscal year is unchanged.
            if age_days > SEGMENT_CACHE_REUSE_DAYS or _latest_completed_annual_year(
                source_as_of
            ) != _latest_completed_annual_year(as_of):
                continue
            cache = SafeFileCache(
                path,
                schema_version=CACHE_SCHEMA_VERSION,
                ttl=CACHE_TTL_SECONDS,
                max_uncompressed_bytes=MAX_RESPONSE_BYTES,
            )
            loaded = cache.load(allow_expired=True)
            if not loaded.hit:
                continue
            payload = loaded.value
            if (
                not isinstance(payload, Mapping)
                or set(payload) != {"contract", "records"}
                or payload.get("contract") != _segment_cache_contract(code, source_as_of)
            ):
                continue
            records = _validate_cached_segment_records(payload.get("records"), code=code, as_of=source_as_of)
            if not records:
                continue
            records = _validate_cached_segment_records(records, code=code, as_of=as_of)
            if not records:
                continue
            segment = _validate_segment_evidence(
                _build_segment_growth_sources(code, as_of, records),
                code=code,
                as_of=as_of,
            )
            if segment.get("status") not in {"complete", "partial"}:
                continue
            return _evidence_record(code, as_of, segment)
        return None
    except Exception:
        return None


def backfill_tdx_segments(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = 4,
) -> dict[str, dict[str, Any]]:
    """Fill segment evidence from the Tongdaxin TCP channel.

    ``requests_`` are the same growth-evidence request shapes the bounded
    loader produces (``code`` / ``as_of`` / ``revenue_records`` / ...).  For
    each company the F10 经营分析 block is parsed into annual segment rows and
    rebuilt through the shared ``_build_segment_growth_sources`` path.  A
    company with no usable rows is omitted (the caller keeps its existing
    record).  TCP does not rate-limit IPs the way Eastmoney HTTP does, so a
    bounded worker pool is safe.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from data.growth_evidence import (
        _build_segment_growth_sources,
        _parse_as_of,
        _validate_cached_segment_records,
        _validate_segment_evidence,
    )

    def fetch_one(request: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        code = str(request.get("code") or "")
        as_of = _parse_as_of(request.get("as_of"))
        if not code or as_of is None:
            return code, None
        # Cache-first: the Tongdaxin fallback persists partial captures too,
        # and a partial 2-year row is strictly better than the unavailable
        # state a rate-limited CI runner would re-fetch.
        cached = _load_tdx_cache(code, as_of)
        if cached is not None:
            return code, cached
        try:
            from mootdx.quotes import Quotes  # type: ignore[import-not-found]

            client = Quotes.factory(market="std", timeout=8)
            text = client.F10(symbol=code, name="经营分析")
        except Exception:
            # mootdx may be absent (CI) or the Tongdaxin TCP channel may be
            # unreachable from this network; the caller keeps its record.
            return code, None
        try:
            outcome = parse_f10_segments(text or "", code)
            if not outcome.records:
                return code, None
            records = _validate_cached_segment_records(outcome.as_list(), code=code, as_of=as_of)
            if not records:
                return code, None
            segment = _validate_segment_evidence(
                _build_segment_growth_sources(code, as_of, records),
                code=code,
                as_of=as_of,
            )
            if segment.get("status") == "unavailable":
                return code, None
            # Persist to the shared segment cache so a later CI run pre-warms
            # it from the uploaded archive and skips the TCP fetch.
            _write_tdx_cache(code, as_of, outcome.as_list())
            return code, _evidence_record(code, as_of, segment)
        except Exception:
            return code, None

    filled: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fetch_one, request) for request in requests_]
        for future in as_completed(futures):
            code, record = future.result()
            if record is not None:
                filled[code] = record
    return filled
