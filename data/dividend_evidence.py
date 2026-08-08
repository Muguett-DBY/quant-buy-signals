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

import math
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from data.cache import SafeFileCache

EASTMONEY_DIVIDEND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DIVIDEND_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "dividend_history"
DIVIDEND_CACHE_MODEL_ID = "eastmoney-sharebonus-v1"
DIVIDEND_CACHE_SCHEMA_VERSION = 1
DIVIDEND_CACHE_TTL_SECONDS = 20 * 3600  # dividend announcements arrive intraday
REQUEST_TIMEOUT = (15, 30)
REQUEST_ATTEMPTS = 3
REQUEST_BACKOFF_SECONDS = 3.0
MAX_PAGE_SIZE = 50


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


def _fetch_dividend_rows(code: str, *, session: Any = requests) -> list[dict[str, Any]]:
    """Fetch Eastmoney dividend detail rows for one company with retries."""
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = session.get(
                EASTMONEY_DIVIDEND_URL,
                params={
                    "reportName": "RPT_SHAREBONUS_DET",
                    "columns": "ALL",
                    "filter": f'(SECURITY_CODE="{code}")',
                    "pageNumber": 1,
                    "pageSize": MAX_PAGE_SIZE,
                    "sortTypes": -1,
                    "sortColumns": "NOTICE_DATE",
                    "source": "WEB",
                    "client": "WEB",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            rows = ((payload.get("result") or {}).get("data")) or []
            if not isinstance(rows, list):
                raise DividendEvidenceError(f"eastmoney dividend payload is not a list: {code}")
            normalised: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                report_date = _parse_iso_date(row.get("REPORT_DATE"))
                cash_per_ten = _finite(row.get("PRETAX_BONUS_RMB"))
                payout_ratio = _finite(row.get("DIVIDENT_RATIO"))
                ex_date = _parse_iso_date(row.get("EX_DIVIDEND_DATE"))
                if report_date is None:
                    continue
                normalised.append(
                    {
                        "report_date": report_date.isoformat(),
                        "ex_dividend_date": ex_date.isoformat() if ex_date is not None else None,
                        "cash_per_ten_share": cash_per_ten,
                        "payout_ratio": payout_ratio,
                    }
                )
            normalised.sort(key=lambda row: row["report_date"])
            return normalised
        except (requests.RequestException, DividendEvidenceError, ValueError) as exc:
            last_error = exc
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(REQUEST_BACKOFF_SECONDS * (attempt + 1))
    raise DividendEvidenceError(f"eastmoney dividend fetch failed for {code}: {last_error!r}")


def _load_cached_rows(code: str, cache: SafeFileCache) -> list[dict[str, Any]] | None:
    loaded = cache.load()
    if not loaded.hit:
        return None
    value = loaded.value
    if (
        not isinstance(value, Mapping)
        or value.get("model_id") != DIVIDEND_CACHE_MODEL_ID
        or not isinstance(value.get("rows"), list)
    ):
        return None
    return value["rows"]


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
) -> None:
    if not rows:
        return
    cash_rows = [row for row in rows if row.get("cash_per_ten_share") is not None]
    if not cash_rows:
        return
    trailing_cash = 0.0
    for row in cash_rows:
        ex_date = _parse_iso_date(row.get("ex_dividend_date"))
        if ex_date is None:
            continue
        if cutoff - ex_date <= timedelta(days=365):
            trailing_cash += float(row["cash_per_ten_share"]) / 10.0
    if trailing_cash <= 0.0:
        trailing_cash = float(cash_rows[-1]["cash_per_ten_share"]) / 10.0
    latest_payout = None
    for row in reversed(cash_rows):
        if row.get("payout_ratio") is not None:
            latest_payout = float(row["payout_ratio"])
            break
    evidence_id = f"{DIVIDEND_CACHE_MODEL_ID}:{code}:{cutoff.strftime('%Y%m%d')}"
    evidence_by_code[code] = {
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

    ``trailing_cash_per_share`` sums the cash dividends whose ex-dividend
    date falls within the trailing twelve months before ``as_of`` (falling
    back to the latest single cash dividend when no ex-date is available),
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
                except DividendEvidenceError:
                    rows = []
                _bind_dividend_evidence(code, rows, cutoff, evidence_by_code)
    return evidence_by_code


__all__ = [
    "DIVIDEND_CACHE_DIR",
    "DIVIDEND_CACHE_MODEL_ID",
    "DividendEvidenceError",
    "load_dividend_evidence",
]
