"""Validated quote and financial-data orchestration."""

from __future__ import annotations

import json as _json
import math
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import pandas as pd
import requests

from config import CONCURRENCY, REQUEST_TIMEOUT
from data.capex_evidence import (
    CAPEX_FIELD,
    NON_CAPEX_OUTFLOW_FIELDS,
    CapexEvidenceConflictError,
    resolve_capex_evidence,
)
from data.financial_source_evidence import (
    FinancialSourceEvidenceError,
    balance_sheet_evidence,
    zero_capex_evidence,
    zero_revenue_evidence,
)

from data.datacenter import (
    DataFetchError,
    MAIN_FINANCIAL_INDICATOR_METRICS,
    fetch_all_financials_parallel,
    fetch_interim_financials_parallel,
)


SINA_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
)
SINA_CLASSIC_URL = "https://hq.sinajs.cn/"
SINA_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://vip.stock.finance.sina.com.cn/"}
SINA_CLASSIC_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
SINA_PAGE_SIZE = 100
SINA_CLASSIC_BATCH_SIZE = 200
_MAX_SINA_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_SINA_COUNT_RESPONSE_BYTES = 64 * 1024
_MAX_SINA_COUNT_ACQUISITION_BYTES = 128 * 1024
_SINA_RESPONSE_CHUNK_BYTES = 64 * 1024
_MIN_SINA_PRICE_RATIO = 0.5
_MAX_SINA_PRICE_RATIO = 2.0
_SINA_WORKERS = max(1, min(int(CONCURRENCY), 10))
_SINA_RECOVERY_TIMEOUT = max(int(REQUEST_TIMEOUT) * 2, 30)
_SINA_RECOVERY_RETRIES = 2
_MAX_SINA_RECOVERY_PAGES = 5
_RETRYABLE_SINA_HTTP_STATUSES = frozenset({408, 425, 429})
_SINA_CLASSIC_LINE = re.compile(r'var\s+hq_str_([a-z]{2}[0-9]{5,6})="([^"]*)";')
_SINA_BJ_STOCK_CODE = re.compile(r"(?:43|83|87|92)[0-9]{4}")

_QUOTE_COLUMNS = [
    "code",
    "name",
    "market",
    "price",
    "trade_price",
    "reference_price",
    "price_source",
    "quote_status",
    "retrieved_at",
    "quote_tick_time",
    "source_trade_date",
    "pe",
    "pb",
    "market_cap",
    "listing_date",
    "listing_date_status",
    "listing_date_source",
    "listing_date_source_url",
    "listing_date_retrieved_at",
]

_MIN_LISTING_REFERENCE_COVERAGE = 0.99
_MIN_LISTING_DATE_COVERAGE = 0.99
_MIN_ACTIVE_REFERENCE_REVERSE_COVERAGE = 0.99
_DETAILED_CASHFLOW_NUMERIC_FIELDS = frozenset(
    {
        "TOTAL_INVEST_INFLOW",
        CAPEX_FIELD,
        *NON_CAPEX_OUTFLOW_FIELDS,
        "TOTAL_INVEST_OUTFLOW",
        "INVEST_NETCASH_OTHER",
        "INVEST_NETCASH_BALANCE",
        "NETCASH_INVEST",
    }
)
_BALANCE_NUMERIC_FIELDS = frozenset(
    {
        "TOTAL_ASSETS",
        "TOTAL_LIABILITIES",
        "TOTAL_EQUITY",
        "TOTAL_PARENT_EQUITY",
        "PARENT_EQUITY",
        "TOTAL_EQUITY_PARENT",
        "EQUITY_PARENT",
        "MINORITY_EQUITY",
        "MINORITY_INTEREST",
        "GOODWILL",
        "DEBT_ASSET_RATIO",
        "MONETARYFUNDS",
        "SHORT_LOAN",
        "LONG_LOAN",
        "LONG_TERM_LOAN",
        "BONDS_PAYABLE",
        "BOND_PAYABLE",
        "NONCURRENT_LIAB_1YEAR",
        "NONCURRENT_LIABILITY_IN_1YEAR",
        "CURRENT_PORTION_NONCURRENT_LIAB",
        "LEASE_LIAB",
        "LEASE_LIABILITY",
        "SHORT_BONDS_PAYABLE",
        "SHORT_BOND_PAYABLE",
        "BORROW_FUNDS",
        "BORROW_FUND",
        "CENTRAL_BANK_BORROWING",
        "LOAN_PBC",
        "SUBORDINATED_BONDS_PAYABLE",
        "SUBBOND_PAYABLE",
    }
)


class QuoteFetchError(DataFetchError):
    """A quote snapshot could not be proven complete and valid."""


class _SinaResourceLimitError(QuoteFetchError):
    """A Sina response exceeded a fixed local resource budget."""


class _SinaTransientTransportError(QuoteFetchError):
    """A Sina request exhausted retries using only recoverable failures."""


class _SinaAcquisitionByteBudget:
    """Sequential response-body budget shared by Sina count probes."""

    def __init__(self, limit: int):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Sina acquisition byte limit must be a positive integer")
        self.limit = limit
        self._consumed = 0
        self._exhausted = False

    def charge(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Sina acquisition byte charge must be a non-negative integer")
        if self._exhausted:
            raise _SinaResourceLimitError("Sina acquisition response-byte budget is already exhausted")
        self._consumed += size
        if self._consumed > self.limit:
            self._exhausted = True
            raise _SinaResourceLimitError(
                f"Sina acquisition attempts exceed byte limit: {self._consumed} > {self.limit}"
            )

    def raise_if_exhausted(self) -> None:
        if self._exhausted or self._consumed >= self.limit:
            self._exhausted = True
            comparator = ">" if self._consumed > self.limit else ">="
            raise _SinaResourceLimitError(
                f"Sina acquisition attempts reached byte limit: {self._consumed} {comparator} {self.limit}"
            )


def _is_transient_sina_transport_error(exc: BaseException | None) -> bool:
    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return True
    if not isinstance(exc, requests.HTTPError):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status in _RETRYABLE_SINA_HTTP_STATUSES or 500 <= status <= 599)


def _bounded_sina_response_text(
    response: Any,
    *,
    max_bytes: int | None = None,
    acquisition_budget: _SinaAcquisitionByteBudget | None = None,
) -> str:
    """Read one response without materialising more than the byte budget."""
    if max_bytes is None:
        max_bytes = _MAX_SINA_RESPONSE_BYTES
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("Sina response byte limit must be a positive integer")
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        declared_text = str(declared).strip()
        if not re.fullmatch(r"0|[1-9][0-9]*", declared_text):
            raise QuoteFetchError("Sina response contains invalid Content-Length")
        declared_bytes = int(declared_text)
        if declared_bytes > max_bytes:
            raise _SinaResourceLimitError(f"Sina response exceeds byte limit: {declared_bytes} > {max_bytes}")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        received = 0
        for chunk in iter_content(chunk_size=_SINA_RESPONSE_CHUNK_BYTES):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise QuoteFetchError("Sina response yielded non-byte content")
            received += len(chunk)
            if acquisition_budget is not None:
                acquisition_budget.charge(len(chunk))
            if received > max_bytes:
                raise _SinaResourceLimitError(f"Sina response exceeds byte limit: received more than {max_bytes}")
            chunks.append(chunk)
        raw = b"".join(chunks)
        encoding = getattr(response, "encoding", None) or "utf-8"
        try:
            return raw.decode(str(encoding))
        except (LookupError, UnicodeDecodeError) as exc:
            raise QuoteFetchError("Sina response uses invalid or undecodable text encoding") from exc

    # Lightweight response doubles used by tests may expose only ``text``.
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise QuoteFetchError("Sina response does not expose a readable body")
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        actual_bytes = len(text.encode(str(encoding)))
    except (LookupError, UnicodeEncodeError) as exc:
        raise QuoteFetchError("Sina response uses invalid or unencodable text encoding") from exc
    if acquisition_budget is not None:
        acquisition_budget.charge(actual_bytes)
    if actual_bytes > max_bytes:
        raise _SinaResourceLimitError(f"Sina response exceeds byte limit: {actual_bytes} > {max_bytes}")
    return text


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _response_retrieved_at(response: Any) -> float:
    """Return the HTTP response time, or local receipt time.

    This is retrieval metadata, not the security's trade time. Sina supplies
    only a time-of-day tick field and no source trade date.
    """
    headers = getattr(response, "headers", None)
    raw_date = headers.get("Date") if hasattr(headers, "get") else None
    if raw_date:
        try:
            parsed = parsedate_to_datetime(str(raw_date))
            if parsed.tzinfo is not None:
                value = float(parsed.timestamp())
                if math.isfinite(value) and value > 0:
                    return value
        except (TypeError, ValueError, OverflowError):
            pass
    return float(time.time())


def _strict_sina_json_loads(text: str) -> Any:
    """Decode Sina JSON without accepting duplicate keys or non-finite constants."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise QuoteFetchError(f"Sina JSON contains a duplicate object key: {key}")
            result[key] = value
        return result

    def reject_nonfinite_constant(value: str) -> None:
        raise QuoteFetchError(f"Sina JSON contains a non-finite numeric constant: {value}")

    return _json.loads(
        text,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite_constant,
    )


def _sina_count(
    node: str,
    *,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = 3,
    acquisition_budget: _SinaAcquisitionByteBudget | None = None,
) -> int:
    """Return Sina's authoritative raw row count for a market node."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
        or isinstance(retries, bool)
        or not isinstance(retries, int)
        or retries < 1
    ):
        raise ValueError("Sina count timeout and retries must be positive")
    last_error: Exception | None = None
    transient_only = True
    attempts_used = 0
    for attempt in range(retries):
        attempts_used = attempt + 1
        try:
            if acquisition_budget is not None:
                acquisition_budget.raise_if_exhausted()
            response = requests.get(
                SINA_COUNT_URL,
                params={"node": node},
                headers=SINA_H,
                timeout=timeout,
                stream=True,
            )
            try:
                response.raise_for_status()
                payload = _strict_sina_json_loads(
                    _bounded_sina_response_text(
                        response,
                        max_bytes=_MAX_SINA_COUNT_RESPONSE_BYTES,
                        acquisition_budget=acquisition_budget,
                    )
                )
            finally:
                _close_response(response)
            if payload == []:  # Sina's representation for an empty/unsupported optional node
                return 0
            if isinstance(payload, bool):
                raise ValueError("boolean count")
            if isinstance(payload, int):
                count = payload
            elif isinstance(payload, str) and re.fullmatch(r"0|[1-9][0-9]*", payload):
                count = int(payload)
            else:
                raise ValueError("count is not a canonical non-negative integer")
            if count < 0:
                raise ValueError("negative count")
            return count
        except _SinaResourceLimitError:
            raise
        except (
            requests.RequestException,
            _json.JSONDecodeError,
            QuoteFetchError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            last_error = exc
            transient = _is_transient_sina_transport_error(exc)
            if not transient:
                transient_only = False
            if attempt + 1 < retries and transient:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
    error_type = (
        _SinaTransientTransportError
        if transient_only and _is_transient_sina_transport_error(last_error)
        else QuoteFetchError
    )
    raise error_type(
        f"failed to fetch Sina {node} row count after {attempts_used} attempts: {last_error}"
    ) from last_error


def _sina_count_with_recovery(node: str, acquisition_budget: _SinaAcquisitionByteBudget) -> int:
    """Fetch one count, extending only a purely transient initial failure."""

    try:
        return _sina_count(node, acquisition_budget=acquisition_budget)
    except _SinaTransientTransportError:
        try:
            return _sina_count(
                node,
                timeout=_SINA_RECOVERY_TIMEOUT,
                retries=_SINA_RECOVERY_RETRIES,
                acquisition_budget=acquisition_budget,
            )
        except _SinaTransientTransportError as exc:
            raise QuoteFetchError(f"failed to recover Sina {node} row count: {exc}") from exc


def _sina_page(
    page: int,
    *,
    node: str = "hs_a",
    page_size: int = SINA_PAGE_SIZE,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = 3,
) -> list[dict[str, Any]]:
    """Fetch and validate one Sina quote page.

    A valid empty JSON list is a pagination boundary. Transport, HTTP, JSON and
    schema failures raise after retrying and are never converted into an empty
    boundary page.
    """
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    params = {
        "page": page,
        "num": page_size,
        "sort": "symbol",
        "asc": 1,
        "node": node,
        "symbol": "",
        "_s_r_a": "auto",
    }
    last_error: Exception | None = None
    transient_only = True
    for attempt in range(retries):
        try:
            response = requests.get(SINA_URL, params=params, headers=SINA_H, timeout=timeout, stream=True)
            try:
                response.raise_for_status()
                response_text = _bounded_sina_response_text(response)
                retrieved_at = _response_retrieved_at(response)
            finally:
                _close_response(response)
            payload = _strict_sina_json_loads(response_text)
            if not isinstance(payload, list):
                raise QuoteFetchError(f"Sina {node} page {page} returned non-list JSON")
            if any(not isinstance(row, dict) for row in payload):
                raise QuoteFetchError(f"Sina {node} page {page} contains non-object rows")
            for row in payload:
                missing = {"code", "name", "symbol", "trade"} - row.keys()
                if missing:
                    raise QuoteFetchError(f"Sina {node} page {page} row omitted {sorted(missing)}")
                # Sina exposes only a time-of-day in each quote row.  Preserve
                # the HTTP retrieval time separately rather than pretending
                # it is the security's last-trade timestamp.
                row["_retrieved_at"] = retrieved_at
            return payload
        except _SinaResourceLimitError:
            raise
        except (requests.RequestException, _json.JSONDecodeError, QuoteFetchError, RecursionError) as exc:
            last_error = exc
            if not _is_transient_sina_transport_error(exc):
                transient_only = False
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    error_type = (
        _SinaTransientTransportError
        if transient_only and _is_transient_sina_transport_error(last_error)
        else QuoteFetchError
    )
    raise error_type(f"failed to fetch Sina {node} page {page} after {retries} attempts: {last_error}") from last_error


def _collect_sina_node(
    node: str,
    *,
    max_workers: int = _SINA_WORKERS,
    page_size: int = SINA_PAGE_SIZE,
    max_pages: int = 200,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    """Collect exactly the row count advertised by Sina, in page order."""
    if max_workers < 1 or max_pages < 1:
        raise ValueError("max_workers and max_pages must be positive")
    count_budget = _SinaAcquisitionByteBudget(_MAX_SINA_COUNT_ACQUISITION_BYTES)
    expected_count = _sina_count_with_recovery(node, count_budget)
    if expected_count == 0:
        ending_count = _sina_count_with_recovery(node, count_budget)
        if ending_count != expected_count:
            raise QuoteFetchError(
                f"Sina {node} row count changed during acquisition: {expected_count} -> {ending_count}"
            )
        if allow_empty:
            return []
        raise QuoteFetchError(f"Sina {node} advertises an empty snapshot")
    expected_pages = (expected_count + page_size - 1) // page_size
    if expected_pages > max_pages:
        raise QuoteFetchError(f"Sina {node} requires {expected_pages} pages, above safety limit {max_pages}")

    with ThreadPoolExecutor(max_workers=min(max_workers, expected_pages)) as executor:
        futures = {
            executor.submit(_sina_page, page, node=node, page_size=page_size): page
            for page in range(1, expected_pages + 1)
        }
        page_rows: dict[int, list[dict[str, Any]]] = {}
        transient_failures: dict[int, _SinaTransientTransportError] = {}
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_rows[page] = future.result()
            except _SinaResourceLimitError:
                # A response that exceeds the local safety budget is not a
                # transient transport failure and must remain fail-fast.
                raise
            except _SinaTransientTransportError as exc:
                transient_failures[page] = exc

    if len(transient_failures) > _MAX_SINA_RECOVERY_PAGES:
        failed_pages = sorted(transient_failures)
        raise QuoteFetchError(
            f"Sina {node} parallel fetch failed on {len(failed_pages)} pages, "
            f"above recovery limit {_MAX_SINA_RECOVERY_PAGES}: {failed_pages[:_MAX_SINA_RECOVERY_PAGES]}"
        ) from transient_failures[failed_pages[0]]

    # A busy page may time out while the initial generation is fetched with
    # bounded concurrency.  Retry the small failed subset sequentially with a
    # longer timeout.  Completeness, schema, duplicate-identity and
    # price-generation checks below remain unchanged and fail closed.
    for page in sorted(transient_failures):
        try:
            page_rows[page] = _sina_page(
                page,
                node=node,
                page_size=page_size,
                timeout=_SINA_RECOVERY_TIMEOUT,
                retries=_SINA_RECOVERY_RETRIES,
            )
        except _SinaResourceLimitError:
            raise
        except _SinaTransientTransportError as exc:
            raise QuoteFetchError(f"failed to recover Sina {node} page {page} after the parallel fetch: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for page in range(1, expected_pages + 1):
        expected_size = page_size if page < expected_pages else expected_count - page_size * (page - 1)
        current = page_rows[page]
        if len(current) != expected_size:
            current = _sina_page(page, node=node, page_size=page_size)
        if len(current) != expected_size:
            raise QuoteFetchError(f"Sina {node} page {page} expected {expected_size} rows, got {len(current)}")
        rows.extend(current)
    if len(rows) != expected_count:
        raise QuoteFetchError(f"Sina {node} expected {expected_count} rows, received {len(rows)}")
    ending_count = _sina_count_with_recovery(node, count_budget)
    if ending_count != expected_count:
        raise QuoteFetchError(f"Sina {node} row count changed during acquisition: {expected_count} -> {ending_count}")
    return rows


def _market_from_symbol(symbol: Any) -> str:
    text = str(symbol).strip().lower()
    for prefix, market in (("sh", "SH"), ("sz", "SZ"), ("bj", "BJ"), ("hk", "HK")):
        if text.startswith(prefix):
            return market
    raise QuoteFetchError(f"unknown Sina market symbol: {symbol!r}")


def _quotes_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_QUOTE_COLUMNS)
    frame = pd.DataFrame(rows)
    required = {"code", "name", "symbol", "trade"}
    missing = required - set(frame.columns)
    if missing:
        raise QuoteFetchError(f"quote snapshot omitted columns: {sorted(missing)}")
    frame["market"] = frame["symbol"].map(_market_from_symbol)
    frame["code"] = frame["code"].astype(str).str.strip()
    if not frame["code"].map(lambda value: bool(re.fullmatch(r"[0-9]{6}", value))).all():
        raise QuoteFetchError("quote snapshot contains non-canonical stock codes")
    symbols = frame["symbol"].map(lambda value: str(value).strip().lower())
    expected_symbols = frame["market"].str.lower() + frame["code"]
    mismatched_symbols = ~symbols.eq(expected_symbols)
    if mismatched_symbols.any():
        examples = frame.loc[mismatched_symbols, ["code", "symbol"]].head(5).to_dict(orient="records")
        raise QuoteFetchError(f"quote code/symbol identities disagree: {examples}")
    market_code_mismatch = (
        ((frame["market"] == "SH") & ~frame["code"].str.startswith("6"))
        | ((frame["market"] == "SZ") & ~frame["code"].str.startswith(("0", "3")))
        | (
            (frame["market"] == "BJ")
            & ~frame["code"].map(lambda value: _SINA_BJ_STOCK_CODE.fullmatch(value) is not None)
        )
    )
    if market_code_mismatch.any():
        examples = frame.loc[market_code_mismatch, ["market", "code", "symbol"]].head(5).to_dict(orient="records")
        raise QuoteFetchError(f"quote market/code identities disagree: {examples}")
    frame = frame.rename(columns={"trade": "trade_price", "per": "pe", "mktcap": "market_cap"})
    for column in ("trade_price", "settlement", "pe", "pb", "market_cap"):
        if column not in frame.columns:
            frame[column] = None
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    trade_is_positive = frame["trade_price"].map(
        lambda value: bool(pd.notna(value) and math.isfinite(float(value)) and float(value) > 0)
    )
    close_is_positive = frame["settlement"].map(
        lambda value: bool(pd.notna(value) and math.isfinite(float(value)) and float(value) > 0)
    )
    missing_a_share_price = frame["market"].isin({"SH", "SZ"}) & ~trade_is_positive & ~close_is_positive
    if missing_a_share_price.any():
        examples = frame.loc[missing_a_share_price, ["market", "code", "symbol"]].head(5).to_dict(orient="records")
        raise QuoteFetchError(f"Sina SH/SZ source rows contain no defensible positive price: {examples}")
    frame["reference_price"] = frame["trade_price"].where(trade_is_positive)
    use_previous_close = ~trade_is_positive & close_is_positive
    frame.loc[use_previous_close, "reference_price"] = frame.loc[use_previous_close, "settlement"]
    frame["price"] = frame["reference_price"]
    frame["price_source"] = "unavailable"
    frame.loc[trade_is_positive, "price_source"] = "last_trade"
    frame.loc[use_previous_close, "price_source"] = "previous_close"
    frame["quote_status"] = "invalid_price"
    frame.loc[trade_is_positive, "quote_status"] = "trading"
    frame.loc[use_previous_close, "quote_status"] = "suspended_or_no_trade"
    if "_retrieved_at" in frame.columns:
        frame["retrieved_at"] = pd.to_numeric(frame["_retrieved_at"], errors="coerce")
    else:
        frame["retrieved_at"] = None
    if "ticktime" in frame.columns:
        frame["quote_tick_time"] = frame["ticktime"].map(lambda value: "" if pd.isna(value) else str(value).strip())
    else:
        frame["quote_tick_time"] = ""
    # A weekend fetch must not turn a stale settlement into a same-day trade.
    frame["source_trade_date"] = None
    # Listing evidence is attached from a separately validated whole-market
    # reference batch.  Keep the columns explicit even for callers that opt
    # out of enrichment and for unsupported optional markets.
    frame["listing_date"] = None
    frame["listing_date_status"] = None
    frame["listing_date_source"] = None
    frame["listing_date_source_url"] = None
    frame["listing_date_retrieved_at"] = None
    frame["market_cap"] = frame["market_cap"] * 10_000
    # Keep zero-trade and suspended securities when the source supplies a
    # positive previous close.  SH/SZ rows without either price have already
    # failed closed above; this filter applies only to optional quote markets.
    frame = frame[frame["reference_price"].notna() & frame["reference_price"].gt(0)]
    duplicate_identity = frame.duplicated(subset=["market", "code"], keep=False)
    if duplicate_identity.any():
        examples = frame.loc[duplicate_identity, ["market", "code"]].drop_duplicates().head(5).to_dict(orient="records")
        raise QuoteFetchError(f"duplicate quote identities across pages: {examples}")
    frame = frame.sort_values(["market", "code"], kind="stable").reset_index(drop=True)
    return frame[_QUOTE_COLUMNS]


def _attach_listing_date_evidence(frame: pd.DataFrame, snapshot: Any) -> pd.DataFrame:
    """Bind independently validated listing evidence to one SH/SZ quote batch.

    A missing upstream listing date remains an explicit unavailable state.  It
    is never guessed from the first observed financial report.  Whole-market
    production refreshes fail closed when either reference-identity coverage
    or actual listing-date coverage falls below the declared floor.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise QuoteFetchError("cannot attach listing evidence to an empty quote batch")
    if not bool(getattr(snapshot, "available", False)):
        reason = str(getattr(snapshot, "reason", "reference unavailable"))
        raise QuoteFetchError(f"listing-date reference is unavailable: {reason}")
    records = getattr(snapshot, "records", None)
    if not isinstance(records, tuple):
        raise QuoteFetchError("listing-date reference records have an invalid shape")

    analysis_mask = frame["market"].map(lambda value: str(value).strip().upper()).isin({"SH", "SZ"})
    if not analysis_mask.any():
        raise QuoteFetchError("listing-date enrichment requires Shanghai/Shenzhen quote rows")
    if "source_trade_date" not in frame.columns:
        raise QuoteFetchError("listing-date enrichment requires a quote source trading session")
    raw_sessions = frame.loc[analysis_mask, "source_trade_date"].tolist()
    if any(
        not isinstance(value, str)
        or value != value.strip()
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None
        for value in raw_sessions
    ):
        raise QuoteFetchError("listing-date enrichment received an invalid quote source trading session")
    source_sessions = set(raw_sessions)
    if len(source_sessions) != 1:
        raise QuoteFetchError("listing-date enrichment received mixed quote source trading sessions")
    source_session = next(iter(source_sessions))
    try:
        source_session_date = datetime.strptime(source_session, "%Y-%m-%d").date()
    except ValueError as exc:
        raise QuoteFetchError("listing-date enrichment received an invalid quote source trading session") from exc

    by_code: dict[str, Any] = {}
    parsed_listing_dates: dict[str, Any] = {}
    for record in records:
        code = str(getattr(record, "code", "")).strip()
        if not re.fullmatch(r"[0-9]{6}", code) or not code.startswith(("6", "0", "3")) or code in by_code:
            raise QuoteFetchError("listing-date reference contains an invalid or duplicate identity")
        raw_listing_date = getattr(record, "listing_date", None)
        parsed_listing_date = None
        if raw_listing_date is not None:
            if (
                not isinstance(raw_listing_date, str)
                or raw_listing_date != raw_listing_date.strip()
                or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw_listing_date) is None
            ):
                raise QuoteFetchError("listing-date reference contains a non-canonical listing date")
            try:
                parsed_listing_date = datetime.strptime(raw_listing_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise QuoteFetchError("listing-date reference contains an invalid listing date") from exc
        by_code[code] = record
        parsed_listing_dates[code] = parsed_listing_date

    result = frame.copy()
    matched = 0
    dated = 0
    for index, row in result.iterrows():
        if str(row.get("market") or "").strip().upper() not in {"SH", "SZ"}:
            continue
        code = str(row.get("code") or "").strip()
        record = by_code.get(code)
        if record is None:
            continue
        matched += 1
        listing_date = getattr(record, "listing_date", None)
        if parsed_listing_dates[code] is not None and parsed_listing_dates[code] > source_session_date:
            raise QuoteFetchError(f"quote identity {code} is bound to a future listing date")
        missing_reasons = getattr(record, "missing_reasons", {})
        if listing_date is None:
            reason = missing_reasons.get("listing_date") if isinstance(missing_reasons, Mapping) else None
            status = str(reason or "upstream_listing_date_unavailable")
        else:
            dated += 1
            status = "reported"
        result.at[index, "listing_date"] = listing_date
        result.at[index, "listing_date_status"] = status
        result.at[index, "listing_date_source"] = getattr(record, "source", None)
        result.at[index, "listing_date_source_url"] = getattr(record, "source_url", None)
        result.at[index, "listing_date_retrieved_at"] = getattr(record, "retrieved_at", None)

    analysis_count = int(result["market"].isin({"SH", "SZ"}).sum())
    reference_coverage = matched / max(analysis_count, 1)
    listing_date_coverage = dated / max(analysis_count, 1)
    if reference_coverage < _MIN_LISTING_REFERENCE_COVERAGE:
        raise QuoteFetchError(
            f"listing-date reference identity coverage {reference_coverage:.1%} is below "
            f"{_MIN_LISTING_REFERENCE_COVERAGE:.1%}"
        )
    if listing_date_coverage < _MIN_LISTING_DATE_COVERAGE:
        raise QuoteFetchError(
            f"listing-date coverage {listing_date_coverage:.1%} is below {_MIN_LISTING_DATE_COVERAGE:.1%}"
        )

    # Eastmoney's broad reference universe includes historical delisted
    # identities.  A positive current turnover rate or volume ratio is the
    # independently observable signal that a non-future reference row should
    # also exist in the same-session Sina quote generation.  This reverse
    # check catches wholesale omissions without pretending stale historical
    # rows are currently listed securities.
    active_reference_codes: set[str] = set()
    for code, record in by_code.items():
        listing_date = parsed_listing_dates[code]
        if listing_date is not None and listing_date > source_session_date:
            continue
        activity_values: list[float] = []
        for field in ("turnover_rate_pct", "volume_ratio"):
            raw_value = getattr(record, field, None)
            if raw_value is None:
                continue
            if isinstance(raw_value, bool):
                raise QuoteFetchError(f"listing-date reference contains an invalid {field} value")
            try:
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise QuoteFetchError(f"listing-date reference contains an invalid {field} value") from exc
            if not math.isfinite(value) or value < 0:
                raise QuoteFetchError(f"listing-date reference contains an invalid {field} value")
            activity_values.append(value)
        if any(value > 0 for value in activity_values):
            active_reference_codes.add(code)
    if not active_reference_codes:
        raise QuoteFetchError("listing-date reference contains no current active quote identities")
    quote_codes = set(result.loc[analysis_mask, "code"].map(lambda value: str(value).strip()))
    reverse_coverage = len(active_reference_codes & quote_codes) / len(active_reference_codes)
    if reverse_coverage < _MIN_ACTIVE_REFERENCE_REVERSE_COVERAGE:
        raise QuoteFetchError(
            f"active listing-reference reverse quote coverage {reverse_coverage:.1%} is below "
            f"{_MIN_ACTIVE_REFERENCE_REVERSE_COVERAGE:.1%}"
        )
    return result


def _sina_classic_batch(
    symbols: list[str] | tuple[str, ...],
    *,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = 3,
) -> dict[str, tuple[str, str, float, float, float]]:
    """Return paired price/date/time/retrieval evidence for one exact batch."""
    expected = tuple(str(symbol).strip().lower() for symbol in symbols)
    if not expected or len(expected) > SINA_CLASSIC_BATCH_SIZE:
        raise ValueError(f"classic Sina batch must contain 1..{SINA_CLASSIC_BATCH_SIZE} symbols")
    if len(set(expected)) != len(expected) or any(not re.fullmatch(r"[a-z]{2}[0-9]{5,6}", item) for item in expected):
        raise ValueError("classic Sina symbols must be unique canonical market symbols")

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(
                f"{SINA_CLASSIC_URL}?list={','.join(expected)}",
                headers=SINA_CLASSIC_H,
                timeout=timeout,
                stream=True,
            )
            try:
                response.raise_for_status()
                response_text = _bounded_sina_response_text(response)
                retrieved_at = _response_retrieved_at(response)
            finally:
                _close_response(response)
            observed: dict[str, tuple[str, str, float, float, float]] = {}
            for match in _SINA_CLASSIC_LINE.finditer(response_text):
                symbol = match.group(1).lower()
                if symbol not in expected:
                    raise QuoteFetchError(f"classic Sina returned an unrequested symbol: {symbol}")
                if symbol in observed:
                    raise QuoteFetchError(f"classic Sina returned duplicate symbol metadata: {symbol}")
                fields = match.group(2).split(",")
                if len(fields) <= 31:
                    raise QuoteFetchError(f"classic Sina metadata is incomplete for {symbol}")
                source_date, tick_time = fields[30].strip(), fields[31].strip()
                try:
                    datetime.strptime(source_date, "%Y-%m-%d")
                    datetime.strptime(tick_time, "%H:%M:%S")
                    previous_close = float(fields[2])
                    trade_price = float(fields[3])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise QuoteFetchError(f"classic Sina returned invalid price/date/time for {symbol}") from exc
                if not all(math.isfinite(value) and value >= 0 for value in (previous_close, trade_price)):
                    raise QuoteFetchError(f"classic Sina returned non-finite or negative price for {symbol}")
                if trade_price <= 0 and previous_close <= 0:
                    raise QuoteFetchError(f"classic Sina returned no defensible price for {symbol}")
                observed[symbol] = (source_date, tick_time, trade_price, previous_close, retrieved_at)
            missing = sorted(set(expected) - set(observed))
            if missing:
                raise QuoteFetchError(f"classic Sina omitted source metadata for {missing[:5]}")
            return observed
        except _SinaResourceLimitError:
            raise
        except (requests.RequestException, QuoteFetchError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    raise QuoteFetchError(
        f"failed to fetch classic Sina source metadata after {retries} attempts: {last_error}"
    ) from last_error


def _sina_trade_metadata(
    symbols: list[str] | tuple[str, ...],
    *,
    max_workers: int = _SINA_WORKERS,
    batch_size: int = SINA_CLASSIC_BATCH_SIZE,
) -> dict[str, tuple[str, str, float, float, float]]:
    """Fetch exact-coverage, paired source date/time metadata in bounded batches."""
    canonical = tuple(str(symbol).strip().lower() for symbol in symbols)
    if max_workers < 1 or batch_size < 1 or batch_size > SINA_CLASSIC_BATCH_SIZE:
        raise ValueError("invalid classic Sina worker or batch size")
    if len(set(canonical)) != len(canonical):
        raise QuoteFetchError("duplicate symbols requested from classic Sina")
    batches = [canonical[index : index + batch_size] for index in range(0, len(canonical), batch_size)]
    if not batches:
        return {}
    observed: dict[str, tuple[str, str, float, float, float]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
        futures = {executor.submit(_sina_classic_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            current = future.result()
            overlap = set(observed) & set(current)
            if overlap:
                raise QuoteFetchError(f"duplicate classic Sina metadata across batches: {sorted(overlap)[:5]}")
            observed.update(current)
    if set(observed) != set(canonical):
        raise QuoteFetchError("classic Sina source metadata identities differ from requested quotes")
    return observed


def _attach_sina_source_metadata(frame: pd.DataFrame) -> pd.DataFrame:
    """Anchor prices and timestamps to the same classic Sina response."""
    symbols = [f"{str(market).lower()}{str(code).strip()}" for market, code in zip(frame["market"], frame["code"])]
    metadata = _sina_trade_metadata(symbols)
    enriched = frame.copy()
    old_reference = pd.to_numeric(enriched["reference_price"], errors="coerce")
    # Reuse the finite-price classification already established by
    # ``_quotes_frame``; a textual or infinite source value must not be
    # reinterpreted as an observed trade merely because ``value > 0``.
    list_trading = enriched["quote_status"].eq("trading")
    trade_prices = pd.Series([metadata[symbol][2] for symbol in symbols], index=enriched.index, dtype=float)
    previous_closes = pd.Series([metadata[symbol][3] for symbol in symbols], index=enriched.index, dtype=float)
    classic_trading = trade_prices.gt(0)
    state_conflict = list_trading.ne(classic_trading)
    if state_conflict.any():
        examples = [
            {
                "symbol": symbol,
                "list_trading": bool(list_state),
                "classic_trading": bool(classic_state),
            }
            for symbol, list_state, classic_state, conflict in zip(
                symbols,
                list_trading,
                classic_trading,
                state_conflict,
            )
            if conflict
        ][:5]
        raise QuoteFetchError(f"Sina list/classic trading states disagree: {examples}")
    new_reference = trade_prices.where(trade_prices.gt(0), previous_closes)
    price_ratio = new_reference / old_reference
    ratio_valid = price_ratio.map(
        lambda value: bool(
            pd.notna(value)
            and math.isfinite(float(value))
            and _MIN_SINA_PRICE_RATIO <= float(value) <= _MAX_SINA_PRICE_RATIO
        )
    )
    if not ratio_valid.all():
        examples = [
            {
                "symbol": symbol,
                "list_price": float(old_price),
                "classic_price": float(classic_price),
                "ratio": float(ratio),
            }
            for symbol, old_price, classic_price, ratio, valid in zip(
                symbols,
                old_reference,
                new_reference,
                price_ratio,
                ratio_valid,
            )
            if not valid
        ][:5]
        raise QuoteFetchError(f"Sina list/classic price generations disagree: {examples}")
    for column in ("pe", "pb", "market_cap"):
        enriched[column] = pd.to_numeric(enriched[column], errors="coerce") * price_ratio
    enriched["trade_price"] = trade_prices
    enriched["reference_price"] = new_reference
    enriched["price"] = new_reference
    trading = classic_trading
    enriched["price_source"] = trading.map({True: "last_trade", False: "previous_close"})
    enriched["quote_status"] = trading.map({True: "trading", False: "suspended_or_no_trade"})
    enriched["retrieved_at"] = [metadata[symbol][4] for symbol in symbols]
    enriched["source_trade_date"] = [metadata[symbol][0] for symbol in symbols]
    enriched["quote_tick_time"] = [metadata[symbol][1] for symbol in symbols]
    return enriched


def _validated_sina_analysis_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the complete hs_a identity generation before excluding BJ rows."""

    source_symbols: list[str] = []
    source_codes: list[str] = []
    analysis_rows: list[dict[str, Any]] = []
    for row in source_rows:
        raw_symbol = row.get("symbol")
        raw_code = row.get("code")
        if not isinstance(raw_code, str) or re.fullmatch(r"[0-9]{6}", raw_code) is None:
            raise QuoteFetchError("Sina source rows contain a non-canonical ASCII stock code")
        if not isinstance(raw_symbol, str) or re.fullmatch(r"(?:bj|sh|sz)[0-9]{6}", raw_symbol) is None:
            raise QuoteFetchError("Sina source rows contain a non-canonical ASCII market symbol")
        market = raw_symbol[:2]
        if raw_symbol != f"{market}{raw_code}":
            if market == "bj":
                raise QuoteFetchError("Sina Beijing source row has an invalid code/symbol identity")
            raise QuoteFetchError("Sina SH/SZ source row has an invalid code/symbol identity")
        if market == "bj":
            if _SINA_BJ_STOCK_CODE.fullmatch(raw_code) is None:
                raise QuoteFetchError("Sina Beijing source row has an invalid code/symbol identity")
        elif market == "sh":
            if not raw_code.startswith("6"):
                raise QuoteFetchError("Sina SH/SZ source row has an invalid market/code identity")
            analysis_rows.append(row)
        elif market == "sz":
            if not raw_code.startswith(("0", "3")):
                raise QuoteFetchError("Sina SH/SZ source row has an invalid market/code identity")
            analysis_rows.append(row)
        source_symbols.append(raw_symbol)
        source_codes.append(raw_code)

    if len(set(source_symbols)) != len(source_symbols) or len(set(source_codes)) != len(source_codes):
        raise QuoteFetchError("Sina source rows contain duplicate symbol/code identities before market filtering")
    if source_symbols != sorted(source_symbols):
        raise QuoteFetchError("Sina source rows are not in the requested global symbol order")
    return analysis_rows


def _get_sina_quotes_parallel(max_workers: int = _SINA_WORKERS) -> pd.DataFrame:
    """Fetch one complete Shanghai/Shenzhen quote snapshot.

    Sina's ``hs_a`` node also contains Beijing securities.  Page/count
    validation must still run against the complete source response, but BJ
    rows are outside this product's universe and are removed before row-level
    quote parsing and classic-Sina metadata enrichment.  Their identities are
    still validated first so a forged ``bj`` prefix cannot hide an SH/SZ row.
    """
    source_rows = _collect_sina_node("hs_a", max_workers=max_workers)
    analysis_rows = _validated_sina_analysis_rows(source_rows)
    frame = _quotes_frame(analysis_rows)
    if frame.empty:
        raise QuoteFetchError("Sina SH/SZ snapshot contains no positive-price rows")
    if not frame["market"].isin({"SH", "SZ"}).all():
        raise QuoteFetchError("Sina hs_a response contained a non-SH/SZ symbol after BJ exclusion")
    frame = _attach_sina_source_metadata(frame)
    print(f"[Sina] Quotes: {len(frame)} stocks")
    return frame


def _safe_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _first_value(row: pd.Series, *names: str) -> Any:
    for name in names:
        if name in row.index:
            value = _safe_value(row.get(name))
            if value is not None:
                return value
    return None


def _finite_float(value: Any) -> float | None:
    value = _safe_value(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _strict_optional_financial_float(
    value: Any,
    *,
    dataset: str,
    field: str,
    code: str,
    report_date: str,
) -> float | None:
    """Normalize a source numeric while distinguishing blanks from corruption."""
    value = _safe_value(value)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise DataFetchError(f"{dataset} {field} contains an invalid numeric value for {code} {report_date}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataFetchError(f"{dataset} {field} contains an invalid numeric value for {code} {report_date}") from exc
    if not math.isfinite(parsed):
        raise DataFetchError(f"{dataset} {field} contains a non-finite numeric value for {code} {report_date}")
    return parsed


def _normalized_numeric_row(
    row: pd.Series,
    fields: set[str] | frozenset[str],
    *,
    dataset: str,
    code: str,
    report_date: str,
) -> pd.Series:
    result = row.copy()
    for field in fields.intersection(row.index):
        result[field] = _strict_optional_financial_float(
            row.get(field),
            dataset=dataset,
            field=field,
            code=code,
            report_date=report_date,
        )
    return result


def _same_financial_amount(left: Any, right: Any) -> bool:
    left_number = _finite_float(left)
    right_number = _finite_float(right)
    if left_number is None or right_number is None:
        return False
    scale = max(abs(left_number), abs(right_number), 1.0)
    return abs(left_number - right_number) <= max(0.02, scale * 1e-12)


def _canonical_parent_equity(row: pd.Series) -> tuple[Any, Any, str]:
    """Return canonical parent equity while preserving source conflicts.

    ``TOTAL_EQUITY = parent equity + minority equity`` is an exact accounting
    identity.  Older restated Eastmoney rows occasionally mix incompatible
    statement generations.  Inventing a value from conflicting components is
    unsafe, but rejecting the entire market snapshot loses nine otherwise
    usable annual observations.  Quarantine only that canonical observation
    and retain the reported value plus an explicit status for diagnostics.
    """
    reported = _first_value(
        row,
        "TOTAL_PARENT_EQUITY",
        "PARENT_EQUITY",
        "TOTAL_EQUITY_PARENT",
        "EQUITY_PARENT",
    )
    total = _first_value(row, "TOTAL_EQUITY")
    minority = _first_value(row, "MINORITY_EQUITY", "MINORITY_INTEREST")
    reported_number = _finite_float(reported)
    total_number = _finite_float(total)
    minority_number = _finite_float(minority)
    if reported is not None:
        if reported_number is not None and total_number is not None and minority_number is not None:
            scale = max(abs(total_number), abs(reported_number) + abs(minority_number), 1.0)
            if abs(total_number - reported_number - minority_number) > scale * 0.03:
                return None, reported, "source_conflict_total_parent_minority"
        return reported, reported, "reported"
    if total_number is not None and minority_number is not None:
        return total_number - minority_number, None, "derived_total_equity_minus_minority"
    return None, None, "missing"


def _code(value: Any) -> str:
    value = _safe_value(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value)).zfill(6)
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


def _is_analysis_financial_code(value: Any) -> bool:
    """Return whether a canonical code belongs to the SH/SZ product scope."""
    code = _code(value)
    return bool(re.fullmatch(r"[0-9]{6}", code)) and code.startswith(("6", "0", "3"))


def _report_date(value: Any) -> str:
    value = _safe_value(value)
    return "" if value is None else str(value)[:10]


def _deduplicate_and_sort(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    without_date: list[dict[str, Any]] = []
    for record in records:
        report_date = _report_date(record.get("REPORT_DATE"))
        record["REPORT_DATE"] = report_date
        if report_date:
            by_date[report_date] = record
        else:
            without_date.append(record)
    return without_date + [by_date[key] for key in sorted(by_date)]


def _apply_official_zero_revenue_evidence(
    result: dict[str, dict[str, Any]],
    covered_income_identities: set[tuple[str, str]],
) -> None:
    """Replace only source nulls backed by committed exchange documents."""
    try:
        evidence_by_identity = zero_revenue_evidence()
    except FinancialSourceEvidenceError as exc:
        raise DataFetchError(f"official zero-revenue evidence is invalid: {exc}") from exc

    for (code, report_date), evidence in evidence_by_identity.items():
        if (code, report_date) not in covered_income_identities:
            continue
        company = result.get(code)
        if not isinstance(company, dict):
            raise DataFetchError(f"official zero-revenue evidence has no matching company row: {code} {report_date}")
        income_rows = company.get("income_history")
        if not isinstance(income_rows, list):
            raise DataFetchError(f"official zero-revenue evidence has no income history: {code} {report_date}")
        matching_income = [row for row in income_rows if _report_date(row.get("REPORT_DATE")) == report_date]
        if len(matching_income) != 1:
            raise DataFetchError(
                f"official zero-revenue evidence requires one matching income row: {code} {report_date}"
            )

        evidence_payload = dict(evidence)
        existing_value = _finite_float(matching_income[0].get("TOTAL_OPERATE_INCOME"))
        if existing_value is not None and existing_value != 0.0:
            raise DataFetchError(f"official zero-revenue evidence conflicts with source value: {code} {report_date}")
        matching_income[0]["TOTAL_OPERATE_INCOME"] = 0.0
        matching_income[0]["TOTAL_OPERATE_INCOME_EVIDENCE"] = evidence_payload

        revenue_rows = company.setdefault("revenue_history", [])
        matching_revenue = [row for row in revenue_rows if _report_date(row.get("REPORT_DATE")) == report_date]
        if len(matching_revenue) > 1:
            raise DataFetchError(f"duplicate revenue identity before official evidence: {code} {report_date}")
        if matching_revenue:
            source_value = _finite_float(matching_revenue[0].get("TOTAL_OPERATE_INCOME"))
            if source_value != 0.0:
                raise DataFetchError(
                    f"official zero-revenue evidence conflicts with revenue history: {code} {report_date}"
                )
            matching_revenue[0]["TOTAL_OPERATE_INCOME_EVIDENCE"] = evidence_payload
        else:
            revenue_rows.append(
                {
                    "TOTAL_OPERATE_INCOME": 0.0,
                    "TOTAL_OPERATE_INCOME_EVIDENCE": evidence_payload,
                    "REPORT_DATE": report_date,
                }
            )


def _merge_financials(
    income: pd.DataFrame,
    cashflow: pd.DataFrame,
    balance: pd.DataFrame,
    income_interim: pd.DataFrame | None = None,
    cashflow_interim: pd.DataFrame | None = None,
    indicators: pd.DataFrame | None = None,
    detailed_cashflow_interim: pd.DataFrame | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge reports without guessing that valid annual rows are fake."""
    result: dict[str, dict[str, Any]] = {}

    try:
        official_capex_by_identity = zero_capex_evidence()
    except FinancialSourceEvidenceError as exc:
        raise DataFetchError(f"official zero-capex evidence is invalid: {exc}") from exc
    try:
        official_balance_sheet_by_identity = balance_sheet_evidence()
    except FinancialSourceEvidenceError as exc:
        raise DataFetchError(f"official balance-sheet evidence is invalid: {exc}") from exc

    detailed_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    if detailed_cashflow_interim is not None and not detailed_cashflow_interim.empty:
        required = {"SECURITY_CODE", "REPORT_DATE", CAPEX_FIELD}
        missing = sorted(required - set(detailed_cashflow_interim.columns))
        if missing:
            raise DataFetchError(f"detailed cash-flow mapping omitted required columns: {missing}")
        for _, row in detailed_cashflow_interim.iterrows():
            identity = (_code(row.get("SECURITY_CODE")), _report_date(row.get("REPORT_DATE")))
            if not identity[0] or not identity[1]:
                raise DataFetchError("detailed cash-flow mapping contains an empty identity")
            if identity in detailed_by_identity:
                raise DataFetchError(f"duplicate detailed cash-flow identity during merge: {identity}")
            normalized_row = _normalized_numeric_row(
                row,
                _DETAILED_CASHFLOW_NUMERIC_FIELDS,
                dataset="detailed interim cash-flow",
                code=identity[0],
                report_date=identity[1],
            )
            detailed_by_identity[identity] = {
                str(field): _safe_value(value) for field, value in normalized_row.to_dict().items()
            }

    if not cashflow.empty and "SECURITY_CODE" in cashflow.columns:
        for _, row in cashflow.iterrows():
            code = _code(row["SECURITY_CODE"])
            report_date = _report_date(row.get("REPORT_DATE"))
            identity = (code, report_date)
            netcash_operate = _strict_optional_financial_float(
                row.get("NETCASH_OPERATE"),
                dataset="annual cash-flow",
                field="NETCASH_OPERATE",
                code=code,
                report_date=report_date,
            )
            reported_capex = _strict_optional_financial_float(
                row.get(CAPEX_FIELD),
                dataset="annual cash-flow",
                field=CAPEX_FIELD,
                code=code,
                report_date=report_date,
            )
            try:
                capex, capex_provenance = resolve_capex_evidence(
                    reported_capex,
                    None,
                    report_date=report_date,
                    security_code=code,
                    official_evidence=official_capex_by_identity.get(identity),
                )
            except CapexEvidenceConflictError as exc:
                raise DataFetchError(
                    f"official zero-capex evidence conflicts with annual source for {code} {report_date}: {exc}"
                ) from exc
            result.setdefault(code, {}).setdefault("cashflow", []).append(
                {
                    "NETCASH_OPERATE": netcash_operate,
                    CAPEX_FIELD: capex,
                    "CAPEX_PROVENANCE": capex_provenance,
                    "REPORT_DATE": report_date,
                }
            )

    if not balance.empty and "SECURITY_CODE" in balance.columns:
        for _, row in balance.iterrows():
            code = _code(row["SECURITY_CODE"])
            report_date = _report_date(row.get("REPORT_DATE"))
            row = _normalized_numeric_row(
                row,
                _BALANCE_NUMERIC_FIELDS,
                dataset="annual balance",
                code=code,
                report_date=report_date,
            )
            parent_equity, reported_parent_equity, parent_equity_source = _canonical_parent_equity(row)
            reported_balance_values = {
                "TOTAL_ASSETS": _safe_value(row.get("TOTAL_ASSETS")),
                "TOTAL_LIABILITIES": _safe_value(row.get("TOTAL_LIABILITIES")),
                "TOTAL_EQUITY": _safe_value(row.get("TOTAL_EQUITY")),
                "TOTAL_PARENT_EQUITY": reported_parent_equity,
                "MINORITY_EQUITY": _first_value(row, "MINORITY_EQUITY", "MINORITY_INTEREST"),
                "GOODWILL": _safe_value(row.get("GOODWILL")),
                "MONETARYFUNDS": _safe_value(row.get("MONETARYFUNDS")),
                "SHORT_LOAN": _safe_value(row.get("SHORT_LOAN")),
                "LONG_LOAN": _first_value(row, "LONG_LOAN", "LONG_TERM_LOAN"),
                "BONDS_PAYABLE": _first_value(row, "BONDS_PAYABLE", "BOND_PAYABLE"),
                "NONCURRENT_LIAB_1YEAR": _first_value(
                    row,
                    "NONCURRENT_LIAB_1YEAR",
                    "NONCURRENT_LIABILITY_IN_1YEAR",
                    "CURRENT_PORTION_NONCURRENT_LIAB",
                ),
                "LEASE_LIAB": _first_value(row, "LEASE_LIAB", "LEASE_LIABILITY"),
                "SHORT_BONDS_PAYABLE": _first_value(row, "SHORT_BONDS_PAYABLE", "SHORT_BOND_PAYABLE"),
                "BORROW_FUNDS": _first_value(row, "BORROW_FUNDS", "BORROW_FUND"),
                "CENTRAL_BANK_BORROWING": _first_value(row, "CENTRAL_BANK_BORROWING", "LOAN_PBC"),
                "SUBORDINATED_BONDS_PAYABLE": _first_value(row, "SUBORDINATED_BONDS_PAYABLE", "SUBBOND_PAYABLE"),
            }
            balance_values = dict(reported_balance_values)
            balance_source_evidence = official_balance_sheet_by_identity.get((code, report_date))
            if balance_source_evidence is not None:
                official_values = balance_source_evidence["canonical_values"]
                source_confirmed = all(
                    _same_financial_amount(reported_balance_values.get(field), value)
                    for field, value in official_values.items()
                )
                balance_values.update(official_values)
                parent_equity = official_values["TOTAL_PARENT_EQUITY"]
                parent_equity_source = (
                    "exchange_filed_official_confirmed" if source_confirmed else "exchange_filed_official_override"
                )
                debt_asset_ratio = official_values["TOTAL_LIABILITIES"] / official_values["TOTAL_ASSETS"] * 100.0
            else:
                debt_asset_ratio = _safe_value(row.get("DEBT_ASSET_RATIO"))

            balance_record = {
                "TOTAL_ASSETS": balance_values["TOTAL_ASSETS"],
                "TOTAL_LIABILITIES": balance_values["TOTAL_LIABILITIES"],
                "TOTAL_EQUITY": balance_values["TOTAL_EQUITY"],
                "TOTAL_PARENT_EQUITY": parent_equity,
                "PARENT_EQUITY": parent_equity,
                "REPORTED_PARENT_EQUITY": reported_parent_equity,
                "PARENT_EQUITY_SOURCE": parent_equity_source,
                "MINORITY_EQUITY": balance_values["MINORITY_EQUITY"],
                "GOODWILL": balance_values["GOODWILL"],
                "DEBT_ASSET_RATIO": debt_asset_ratio,
                "MONETARYFUNDS": balance_values["MONETARYFUNDS"],
                "SHORT_LOAN": balance_values["SHORT_LOAN"],
                "LONG_LOAN": balance_values["LONG_LOAN"],
                "BONDS_PAYABLE": balance_values["BONDS_PAYABLE"],
                "NONCURRENT_LIAB_1YEAR": balance_values["NONCURRENT_LIAB_1YEAR"],
                "LEASE_LIAB": balance_values["LEASE_LIAB"],
                "SHORT_BONDS_PAYABLE": balance_values["SHORT_BONDS_PAYABLE"],
                "BORROW_FUNDS": balance_values["BORROW_FUNDS"],
                "CENTRAL_BANK_BORROWING": balance_values["CENTRAL_BANK_BORROWING"],
                "SUBORDINATED_BONDS_PAYABLE": balance_values["SUBORDINATED_BONDS_PAYABLE"],
                "REPORT_DATE": report_date,
            }
            if balance_source_evidence is not None:
                balance_record["REPORTED_BALANCE_SHEET_VALUES"] = reported_balance_values
                balance_evidence_payload = dict(balance_source_evidence)
                balance_evidence_payload["canonical_values"] = dict(balance_source_evidence["canonical_values"])
                balance_evidence_payload["source_pages"] = list(balance_source_evidence["source_pages"])
                balance_record["BALANCE_SHEET_EVIDENCE"] = balance_evidence_payload
            result.setdefault(code, {}).setdefault("balance", []).append(balance_record)

    covered_income_identities: set[tuple[str, str]] = set()
    if not income.empty and "SECURITY_CODE" in income.columns:
        for _, row in income.iterrows():
            code = _code(row["SECURITY_CODE"])
            company = result.setdefault(code, {})
            company.setdefault("revenue_history", [])
            company.setdefault("income_history", [])
            report_date = _report_date(row.get("REPORT_DATE"))
            covered_income_identities.add((code, report_date))
            revenue = _strict_optional_financial_float(
                row.get("TOTAL_OPERATE_INCOME"),
                dataset="annual income",
                field="TOTAL_OPERATE_INCOME",
                code=code,
                report_date=report_date,
            )
            net_profit = _strict_optional_financial_float(
                row.get("PARENT_NETPROFIT"),
                dataset="annual income",
                field="PARENT_NETPROFIT",
                code=code,
                report_date=report_date,
            )
            operate_profit = _strict_optional_financial_float(
                row.get("OPERATE_PROFIT"),
                dataset="annual income",
                field="OPERATE_PROFIT",
                code=code,
                report_date=report_date,
            )
            income_record: dict[str, Any] = {"REPORT_DATE": report_date}
            for key, value in (
                ("TOTAL_OPERATE_INCOME", revenue),
                ("PARENT_NETPROFIT", net_profit),
                ("OPERATE_PROFIT", operate_profit),
            ):
                if value is not None:
                    income_record[key] = value
            if revenue is not None:
                company["revenue_history"].append({"TOTAL_OPERATE_INCOME": revenue, "REPORT_DATE": report_date})
            if len(income_record) > 1:
                company["income_history"].append(income_record)

    _apply_official_zero_revenue_evidence(result, covered_income_identities)

    if income_interim is not None and not income_interim.empty and "SECURITY_CODE" in income_interim.columns:
        for _, row in income_interim.iterrows():
            code = _code(row["SECURITY_CODE"])
            report_date = _report_date(row.get("REPORT_DATE"))
            record = {
                "TOTAL_OPERATE_INCOME": _strict_optional_financial_float(
                    row.get("TOTAL_OPERATE_INCOME"),
                    dataset="interim income",
                    field="TOTAL_OPERATE_INCOME",
                    code=code,
                    report_date=report_date,
                ),
                "PARENT_NETPROFIT": _strict_optional_financial_float(
                    row.get("PARENT_NETPROFIT"),
                    dataset="interim income",
                    field="PARENT_NETPROFIT",
                    code=code,
                    report_date=report_date,
                ),
                "REPORT_DATE": report_date,
                "period_end": report_date[5:] if len(report_date) == 10 else "",
            }
            company = result.setdefault(code, {})
            company.setdefault("income_interim", []).append(record)
            if report_date.endswith("-03-31"):
                company.setdefault("income_q1", []).append(dict(record))
    if cashflow_interim is not None and not cashflow_interim.empty and "SECURITY_CODE" in cashflow_interim.columns:
        for _, row in cashflow_interim.iterrows():
            code = _code(row["SECURITY_CODE"])
            report_date = _report_date(row.get("REPORT_DATE"))
            identity = (code, report_date)
            netcash_operate = _strict_optional_financial_float(
                row.get("NETCASH_OPERATE"),
                dataset="interim cash-flow",
                field="NETCASH_OPERATE",
                code=code,
                report_date=report_date,
            )
            reported_capex = _strict_optional_financial_float(
                row.get(CAPEX_FIELD),
                dataset="interim cash-flow",
                field=CAPEX_FIELD,
                code=code,
                report_date=report_date,
            )
            try:
                capex, capex_provenance = resolve_capex_evidence(
                    reported_capex,
                    detailed_by_identity.get(identity),
                    report_date=report_date,
                    security_code=code,
                    official_evidence=official_capex_by_identity.get(identity),
                )
            except CapexEvidenceConflictError as exc:
                raise DataFetchError(
                    f"official zero-capex evidence conflicts with source: {code} {report_date}"
                ) from exc
            record = {
                "NETCASH_OPERATE": netcash_operate,
                CAPEX_FIELD: capex,
                "CAPEX_PROVENANCE": capex_provenance,
                # The detailed F10 cash-flow report is the source of this
                # line item.  It is reported as evidence only; a current
                # period cash outflow cannot prove a historical M&A census.
                "OBTAIN_SUBSIDIARY_OTHER": _safe_value(
                    detailed_by_identity.get(identity, {}).get("OBTAIN_SUBSIDIARY_OTHER")
                ),
                "REPORT_DATE": report_date,
                "period_end": report_date[5:] if len(report_date) == 10 else "",
            }
            company = result.setdefault(code, {})
            company.setdefault("cashflow_interim", []).append(record)
            if report_date.endswith("-03-31"):
                company.setdefault("cashflow_q1", []).append(dict(record))

    if indicators is not None and not indicators.empty:
        required = {
            "SECURITY_CODE",
            "SECUCODE",
            "REPORT_DATE",
            "REPORT_TYPE",
            "REPORT_DATE_NAME",
            "REPORT_YEAR",
            "NOTICE_DATE",
            "SOURCE_REPORT_NAME",
            *MAIN_FINANCIAL_INDICATOR_METRICS,
        }
        missing = sorted(required - set(indicators.columns))
        if missing:
            raise DataFetchError(f"indicator mapping omitted required columns: {missing}")
        identities = indicators.assign(
            _CODE=indicators["SECURITY_CODE"].map(_code),
            _REPORT_DATE=indicators["REPORT_DATE"].map(_report_date),
        )
        duplicate = identities.duplicated(["_CODE", "_REPORT_DATE"], keep=False)
        if duplicate.any():
            examples = (
                identities.loc[duplicate, ["_CODE", "_REPORT_DATE"]].drop_duplicates().head(5).to_dict(orient="records")
            )
            raise DataFetchError(f"duplicate indicator identities during financial merge: {examples}")
        for _, row in indicators.iterrows():
            code = _code(row["SECURITY_CODE"])
            report_date = _report_date(row.get("REPORT_DATE"))
            if not code or not report_date:
                raise DataFetchError("indicator mapping contains an empty company identity or report date")
            record = {
                "SECUCODE": str(_safe_value(row.get("SECUCODE")) or "").strip(),
                "REPORT_DATE": report_date,
                "REPORT_TYPE": str(_safe_value(row.get("REPORT_TYPE")) or "").strip(),
                "REPORT_DATE_NAME": str(_safe_value(row.get("REPORT_DATE_NAME")) or "").strip(),
                "REPORT_YEAR": str(_safe_value(row.get("REPORT_YEAR")) or "").strip(),
                "NOTICE_DATE": _report_date(row.get("NOTICE_DATE")),
                "SOURCE_REPORT_NAME": str(_safe_value(row.get("SOURCE_REPORT_NAME")) or "").strip(),
            }
            for field in MAIN_FINANCIAL_INDICATOR_METRICS:
                record[field] = _strict_optional_financial_float(
                    row.get(field),
                    dataset="annual indicators",
                    field=field,
                    code=code,
                    report_date=report_date,
                )
            result.setdefault(code, {}).setdefault("indicators", []).append(record)

    list_fields = (
        "cashflow",
        "balance",
        "revenue_history",
        "income_history",
        "income_interim",
        "cashflow_interim",
        "income_q1",
        "cashflow_q1",
        "indicators",
    )
    for company in result.values():
        company.setdefault("indicators", [])
        for field in list_fields:
            company[field] = _deduplicate_and_sort(company.get(field, []))
    return result


def _get_hk_stocks_via_tencent(max_workers: int = _SINA_WORKERS) -> pd.DataFrame:
    """Compatibility name: fetch the complete Sina ``hk_ggt`` node.

    The previous implementation neither used Tencent quotes nor paginated the
    Sina fallback. Keeping the public name avoids breaking external imports.
    """
    frame = _quotes_frame(_collect_sina_node("hk_ggt", max_workers=max_workers, allow_empty=True))
    if not frame.empty and frame["market"].ne("HK").any():
        raise QuoteFetchError("hk_ggt response contained non-HK symbols")
    print(f"[HK-Sina] Quotes: {len(frame)} stocks")
    return frame


class DataFetcher:
    """Unified, write-free data-fetching facade."""

    def __init__(
        self,
        *,
        enrich_listing_dates: bool = False,
        force_reference_refresh: bool = False,
    ) -> None:
        if not isinstance(enrich_listing_dates, bool) or not isinstance(force_reference_refresh, bool):
            raise ValueError("listing-date fetch options must be boolean")
        self.enrich_listing_dates = enrich_listing_dates
        self.force_reference_refresh = force_reference_refresh

    def get_stock_list(self, include_hk: bool = False) -> pd.DataFrame:
        """Return SH/SZ quotes and, only when requested, quote-only HK rows.

        HK rows use a composite ``code`` such as ``HK:00700`` so a downstream
        bare-code lookup can never attach A-share financials to an HK quote.
        ``local_code`` retains the exchange-local code and ``financial_code``
        is null for markets unsupported by the financial-report pipeline.
        """
        print("[Fetcher] Loading stock quotes...")
        started = time.time()
        if self.enrich_listing_dates:
            # Quote and listing-date sources are independent network batches;
            # run them concurrently so the stronger evidence contract does not
            # add their latencies serially.
            from data.market_coldness import fetch_market_coldness_snapshot

            with ThreadPoolExecutor(max_workers=2) as executor:
                quote_future = executor.submit(_get_sina_quotes_parallel)
                reference_future = executor.submit(
                    fetch_market_coldness_snapshot,
                    force_refresh=self.force_reference_refresh,
                )
                a_shares = quote_future.result()
                listing_reference = reference_future.result()
            a_shares = _attach_listing_date_evidence(a_shares, listing_reference)
        else:
            a_shares = _get_sina_quotes_parallel()
        if "market" not in a_shares:
            raise QuoteFetchError("A-share quote source omitted market identity")
        a_shares = a_shares[a_shares["market"].isin({"SH", "SZ"})].copy()
        if a_shares.empty:
            raise QuoteFetchError("A-share quote source returned no Shanghai/Shenzhen rows")
        frames = [a_shares]
        if include_hk:
            frames.append(_get_hk_stocks_via_tencent())
        quotes = pd.concat(frames, ignore_index=True)
        if quotes.duplicated(subset=["market", "code"], keep=False).any():
            raise QuoteFetchError("duplicate market/code identities across quote sources")
        quotes = quotes.sort_values(["market", "code"], kind="stable").reset_index(drop=True)
        quotes["local_code"] = quotes["code"]
        supports_financials = quotes["market"].isin({"SH", "SZ"})
        quotes["financial_code"] = quotes["code"].where(supports_financials, None)
        quotes["has_financials"] = supports_financials
        quotes.loc[~supports_financials, "code"] = (
            quotes.loc[~supports_financials, "market"] + ":" + quotes.loc[~supports_financials, "local_code"]
        )
        print(f"[Fetcher] Quotes done: {len(quotes)} stocks in {time.time() - started:.1f}s")
        return quotes

    def get_financials(self, codes: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """Fetch annual reports plus the latest interim and prior-year comparable."""
        print("[Fetcher] Loading financials from Eastmoney Datacenter...")
        started = time.time()
        requested_codes = None
        if codes is not None:
            requested_codes = {_code(code) for code in codes if _is_analysis_financial_code(code)}
            if not requested_codes:
                print("[Fetcher] Financials done: 0 stocks (no SH/SZ codes requested)")
                return {}

        # Annual history and current/prior interim statements are independent
        # all-or-error generations.  Fetch them together; the datacenter layer
        # enforces one process-wide active-request ceiling below the upstream
        # connection-reset threshold.
        with ThreadPoolExecutor(max_workers=2) as executor:
            fetch_kwargs = {} if requested_codes is None else {"codes": tuple(sorted(requested_codes))}
            annual_future = executor.submit(fetch_all_financials_parallel, **fetch_kwargs)
            interim_future = executor.submit(fetch_interim_financials_parallel, **fetch_kwargs)
            income, cashflow, balance, indicators = annual_future.result()
            income_interim, cashflow_interim, detailed_cashflow_interim = interim_future.result()

        datasets = [
            income,
            cashflow,
            balance,
            indicators,
            income_interim,
            cashflow_interim,
            detailed_cashflow_interim,
        ]
        for index, frame in enumerate(datasets):
            if not frame.empty:
                normalized = frame["SECURITY_CODE"].map(_code)
                in_scope = normalized.map(_is_analysis_financial_code)
                if requested_codes is not None:
                    in_scope &= normalized.isin(requested_codes)
                datasets[index] = frame[in_scope]
        (
            income,
            cashflow,
            balance,
            indicators,
            income_interim,
            cashflow_interim,
            detailed_cashflow_interim,
        ) = datasets

        result = _merge_financials(
            income,
            cashflow,
            balance,
            income_interim,
            cashflow_interim,
            indicators,
            detailed_cashflow_interim,
        )
        print(f"[Fetcher] Financials done: {len(result)} stocks in {time.time() - started:.1f}s")
        return result
