"""Eastmoney dividend-history evidence for the Type 7 gdN filter.

The gdN investability filter (后续附加补丁们, patch 13/15) requires a
positive dividend engine ``d``: companies must either pay dividends
(``g>0 且 d>0``), retain earnings into higher future growth (``g>0``
with strong R&D/capex), or be a net-net/high-dividend special situation
(``g≈0 但 d 高``).  This adapter fetches Eastmoney's dividend/distribution
detail (RPT_SHAREBONUS_DET) per company, computes per-share cash
dividends and the dividend-payout ratio, and binds dated code-specific
evidence records so the Type 7 gate can consume them.

Only cash dividends are counted (``每10股派现``); stock/capitalisation
transfers are ignored.  The most recent twelve months of ex-dividend
records form the trailing dividend yield when combined with the quote
price at the consumer side.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from data.cache import SafeFileCache
from data.provider_http import (
    is_transient_request_error,
    read_bounded_response_bytes,
    retry_delay_seconds,
    thread_local_session,
)

EASTMONEY_DIVIDEND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DIVIDEND_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "dividend_history"
DIVIDEND_CACHE_MODEL_ID = "eastmoney-sharebonus-v2"
DIVIDEND_CACHE_SCHEMA_VERSION = 2
DIVIDEND_CACHE_TTL_SECONDS = 20 * 3600  # dividend announcements arrive intraday
REQUEST_TIMEOUT = (15, 30)
REQUEST_ATTEMPTS = 3
REQUEST_BACKOFF_SECONDS = 3.0
MAX_PAGE_SIZE = 50
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class DividendEvidenceError(RuntimeError):
    """A dividend source or cache contract failed."""


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _bounded_json(response: Any) -> Mapping[str, Any]:
    if not hasattr(response, "iter_content"):
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise DividendEvidenceError("eastmoney dividend payload is not an object")
        return payload
    try:
        content = read_bounded_response_bytes(response, MAX_RESPONSE_BYTES)
    except ValueError as exc:
        raise DividendEvidenceError("eastmoney dividend response exceeds the size contract") from exc
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DividendEvidenceError("eastmoney dividend response is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise DividendEvidenceError("eastmoney dividend payload is not an object")
    return payload


def _fetch_dividend_page(code: str, page_number: int, *, session: Any) -> tuple[list[dict[str, Any]], int]:
    """Fetch Eastmoney dividend detail rows for one company."""
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        should_retry = False
        try:
            response = session.get(
                EASTMONEY_DIVIDEND_URL,
                params={
                    "reportName": "RPT_SHAREBONUS_DET",
                    "columns": "ALL",
                    "filter": f'(SECURITY_CODE="{code}")',
                    "pageNumber": page_number,
                    "pageSize": MAX_PAGE_SIZE,
                    "sortTypes": -1,
                    "sortColumns": "NOTICE_DATE",
                    "source": "WEB",
                    "client": "WEB",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
                timeout=REQUEST_TIMEOUT,
                stream=True,
            )
            response.raise_for_status()
            payload = _bounded_json(response)
            result = payload.get("result") or {}
            if not isinstance(result, Mapping):
                raise DividendEvidenceError(f"eastmoney dividend result is not an object: {code}")
            rows = result.get("data") or []
            if not isinstance(rows, list):
                raise DividendEvidenceError(f"eastmoney dividend payload is not a list: {code}")
            pages = result.get("pages") or 1
            if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= MAX_PAGES:
                raise DividendEvidenceError(f"eastmoney dividend page count is invalid: {code}")
            normalised: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise DividendEvidenceError(f"eastmoney dividend row is not an object: {code}")
                row_code = str(row.get("SECURITY_CODE") or code).strip()
                if row_code != code:
                    raise DividendEvidenceError(f"eastmoney dividend row identity mismatch: {code}")
                report_date = _parse_iso_date(row.get("REPORT_DATE"))
                cash_per_ten = _finite(row.get("PRETAX_BONUS_RMB"))
                payout_ratio = _finite(row.get("DIVIDENT_RATIO"))
                ex_date = _parse_iso_date(row.get("EX_DIVIDEND_DATE"))
                notice_date = _parse_iso_date(row.get("NOTICE_DATE"))
                if report_date is None:
                    raise DividendEvidenceError(f"eastmoney dividend report date is invalid: {code}")
                normalised.append(
                    {
                        "report_date": report_date.isoformat(),
                        "notice_date": notice_date.isoformat() if notice_date is not None else None,
                        "ex_dividend_date": ex_date.isoformat() if ex_date is not None else None,
                        "cash_per_ten_share": cash_per_ten,
                        "payout_ratio": payout_ratio,
                    }
                )
            normalised.sort(key=lambda row: row["report_date"])
            return normalised, pages
        except (requests.RequestException, DividendEvidenceError, ValueError) as exc:
            last_error = exc
            should_retry = is_transient_request_error(exc, response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not should_retry or attempt + 1 >= REQUEST_ATTEMPTS:
            break
        time.sleep(
            retry_delay_seconds(
                response,
                attempt=attempt,
                base_seconds=REQUEST_BACKOFF_SECONDS,
            )
        )
    raise DividendEvidenceError(f"eastmoney dividend fetch failed for {code}: {last_error!r}")


def _fetch_dividend_rows(code: str, *, session: Any = requests) -> list[dict[str, Any]]:
    if session is requests:
        session = thread_local_session()
    rows: list[dict[str, Any]] = []
    expected_pages = 1
    page_number = 1
    while page_number <= expected_pages:
        page_rows, pages = _fetch_dividend_page(code, page_number, session=session)
        if page_number == 1:
            expected_pages = pages
        elif pages != expected_pages:
            raise DividendEvidenceError(f"eastmoney dividend pagination drifted: {code}")
        rows.extend(page_rows)
        page_number += 1
    identities: set[tuple[Any, ...]] = set()
    for row in rows:
        identity = (
            row.get("report_date"),
            row.get("notice_date"),
            row.get("ex_dividend_date"),
            row.get("cash_per_ten_share"),
        )
        if identity in identities:
            raise DividendEvidenceError(f"eastmoney dividend contains duplicate rows: {code}")
        identities.add(identity)
    return sorted(rows, key=lambda row: (row["report_date"], row.get("notice_date") or ""))


def _load_cached_rows(code: str, cache: SafeFileCache) -> list[dict[str, Any]] | None:
    loaded = cache.load()
    if not loaded.hit:
        return None
    value = loaded.value
    if (
        not isinstance(value, Mapping)
        or value.get("model_id") != DIVIDEND_CACHE_MODEL_ID
        or value.get("code") != code
        or not isinstance(value.get("rows"), list)
    ):
        return None
    rows = value["rows"]
    for row in rows:
        if not isinstance(row, Mapping) or _parse_iso_date(row.get("report_date")) is None:
            return None
        for field in ("notice_date", "ex_dividend_date"):
            raw_date = row.get(field)
            if raw_date is not None and _parse_iso_date(raw_date) is None:
                return None
        for field in ("cash_per_ten_share", "payout_ratio"):
            raw_number = row.get(field)
            if raw_number is not None and _finite(raw_number) is None:
                return None
    return [dict(row) for row in rows]


def _save_cached_rows(code: str, rows: list[dict[str, Any]], cache: SafeFileCache) -> None:
    cache.save(
        {
            "model_id": DIVIDEND_CACHE_MODEL_ID,
            "code": code,
            "rows": rows,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _bind_dividend_evidence(
    code: str,
    rows: list[dict[str, Any]],
    cutoff: date,
    evidence_by_code: dict[str, dict[str, Any]],
    *,
    unavailable_reason: str = "",
) -> None:
    evidence_id = f"{DIVIDEND_CACHE_MODEL_ID}:{code}:{cutoff.strftime('%Y%m%d')}"
    if unavailable_reason:
        evidence_by_code[code] = {
            "status": "unavailable",
            "reason": unavailable_reason,
            "evidence": {
                "source": "东方财富分红送配明细",
                "evidence_id": evidence_id,
                "as_of": cutoff.isoformat(),
                "summary": f"分红资料不可用：{unavailable_reason};model={DIVIDEND_CACHE_MODEL_ID}",
            },
        }
        return
    if not rows:
        _bind_dividend_evidence(
            code,
            rows,
            cutoff,
            evidence_by_code,
            unavailable_reason="source_returned_no_rows",
        )
        return
    public_rows: list[dict[str, Any]] = []
    for row in rows:
        report_date = _parse_iso_date(row.get("report_date"))
        raw_notice_date = row.get("notice_date")
        notice_date = _parse_iso_date(raw_notice_date)
        if report_date is None or (raw_notice_date is not None and notice_date is None):
            _bind_dividend_evidence(
                code,
                [],
                cutoff,
                evidence_by_code,
                unavailable_reason="row_contract_invalid",
            )
            return
        if report_date <= cutoff and (notice_date is None or notice_date <= cutoff):
            public_rows.append(row)
    cash_rows = [row for row in public_rows if row.get("cash_per_ten_share") is not None]
    if not cash_rows:
        _bind_dividend_evidence(
            code,
            rows,
            cutoff,
            evidence_by_code,
            unavailable_reason="no_public_cash_dividend_fact_by_as_of",
        )
        return
    trailing_cash = 0.0
    for row in cash_rows:
        ex_date = _parse_iso_date(row.get("ex_dividend_date"))
        if ex_date is None:
            continue
        if timedelta(0) <= cutoff - ex_date <= timedelta(days=365):
            trailing_cash += float(row["cash_per_ten_share"]) / 10.0
    explicit_zero = any(
        float(row["cash_per_ten_share"]) == 0.0
        and (ex_date := _parse_iso_date(row.get("ex_dividend_date"))) is not None
        and timedelta(0) <= cutoff - ex_date <= timedelta(days=365)
        for row in cash_rows
    )
    if trailing_cash <= 0.0 and not explicit_zero:
        _bind_dividend_evidence(
            code,
            rows,
            cutoff,
            evidence_by_code,
            unavailable_reason="no_paid_cash_dividend_in_trailing_year",
        )
        return
    latest_payout = None
    for row in reversed(cash_rows):
        if row.get("payout_ratio") is not None:
            latest_payout = float(row["payout_ratio"])
            break
    evidence_by_code[code] = {
        "status": "available" if trailing_cash > 0 else "known_zero",
        "trailing_cash_per_share": trailing_cash,
        "payout_ratio": latest_payout,
        "evidence": {
            "source": "东方财富分红送配明细",
            "evidence_id": evidence_id,
            "as_of": cutoff.isoformat(),
            "summary": (
                f"近12个月每股派现{trailing_cash:.3f}元"
                + (f"，分红率{latest_payout:.0%}" if latest_payout is not None else "")
                + f";model={DIVIDEND_CACHE_MODEL_ID}"
            ),
        },
    }


def _fetch_and_cache_dividend_rows(
    code: str,
    cache: SafeFileCache,
    session: Any,
) -> list[dict[str, Any]]:
    rows = _fetch_dividend_rows(code, session=session)
    if rows:
        _save_cached_rows(code, rows, cache)
    return rows


def load_dividend_evidence(
    codes: Sequence[str],
    *,
    as_of: str,
    cache_dir: str | Path = DIVIDEND_CACHE_DIR,
    session: Any = requests,
    max_workers: int = 8,
) -> dict[str, dict[str, Any]]:
    """Return dated, code-bound dividend evidence for each requested company.

    ``trailing_cash_per_share`` sums only cash dividends whose ex-dividend
    date falls within the trailing twelve months before ``as_of``;
    ``payout_ratio`` is the latest reported payout ratio, and the evidence
    record binds the code and the cutoff date.

    Cache hits are read serially from the local cache; only companies whose
    cache generation is missing are fetched over the network, concurrently
    with up to ``max_workers`` threads.
    """
    cutoff = date.fromisoformat(as_of)
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    evidence_by_code: dict[str, dict[str, Any]] = {}
    ordered = sorted(set(codes))
    cache_by_code: dict[str, SafeFileCache] = {}
    pending: list[str] = []
    for code in ordered:
        cache = SafeFileCache(
            directory / f"{code}.json.gz",
            schema_version=DIVIDEND_CACHE_SCHEMA_VERSION,
            ttl=DIVIDEND_CACHE_TTL_SECONDS,
        )
        cache_by_code[code] = cache
        rows = _load_cached_rows(code, cache)
        if rows is not None:
            _bind_dividend_evidence(code, rows, cutoff, evidence_by_code)
        else:
            pending.append(code)

    if pending:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        workers = max(1, min(max_workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_and_cache_dividend_rows, code, cache_by_code[code], session): code
                for code in pending
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    rows = future.result()
                except DividendEvidenceError as exc:
                    _bind_dividend_evidence(
                        code,
                        [],
                        cutoff,
                        evidence_by_code,
                        unavailable_reason=str(exc),
                    )
                else:
                    _bind_dividend_evidence(code, rows, cutoff, evidence_by_code)
    return evidence_by_code


__all__ = [
    "DIVIDEND_CACHE_DIR",
    "DIVIDEND_CACHE_MODEL_ID",
    "DividendEvidenceError",
    "load_dividend_evidence",
]
