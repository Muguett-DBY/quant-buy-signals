"""Strict, gap-only Sina financial-statement fallback.

Eastmoney remains the full-market primary source.  This adapter is deliberately
per-company and is called only for exact strict-TTM identities that remain
missing after the bulk fetch.  Network, schema and true-empty states stay
distinct; no failure is converted to a financial zero.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

from config import CONCURRENCY, REQUEST_TIMEOUT
from data.cache import SafeFileCache
from data.capex_evidence import (
    CAPEX_FIELD,
    SINA_FINANCIAL_URL,
    sina_reported_capex_provenance,
    validate_capex_provenance,
)


SINA_FINANCIAL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "application/json",
}
SINA_FINANCIAL_TIMEOUT = max(10, int(REQUEST_TIMEOUT))
SINA_FINANCIAL_NUM_PERIODS = 40
SINA_FINANCIAL_ADAPTER_VERSION = 2
SINA_FINANCIAL_CACHE_SCHEMA_VERSION = 2
SINA_FINANCIAL_CACHE_TTL_SECONDS = 24 * 60 * 60
SINA_FINANCIAL_NEGATIVE_CACHE_TTL_SECONDS = 15 * 60
SINA_RESPONSE_CHUNK_BYTES = 64 * 1024
MAX_SINA_FINANCIAL_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SINA_FINANCIAL_PERIODS = 48
MAX_SINA_FINANCIAL_ITEMS_PER_PERIOD = 512
MAX_ABS_FINANCIAL_VALUE = Decimal("10000000000000000")
DEFAULT_SINA_FINANCIAL_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "sina_financial"
SINA_FINANCIAL_MAX_WORKERS = max(1, min(int(CONCURRENCY), 4))
SINA_FINANCIAL_MAX_TARGET_REQUESTS = 128
SINA_FINANCIAL_REQUEST_SLOTS = BoundedSemaphore(SINA_FINANCIAL_MAX_WORKERS)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})
_SINA_HOST = "quotes.sina.cn"
_SINA_PATH = "/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_STATEMENTS = frozenset({"lrb", "llb", "fzb"})
_OFFICIAL_DISCLOSURE_SOURCES = frozenset(
    {
        "定期报告",
        "招股说明书/意向书",
        "招股说明书(申报稿)",
        "更正或补充",
    }
)
_CODE = re.compile(r"(?:[036]\d{5})")
_PERIOD = re.compile(r"\d{8}")
_REPORT_DATE = re.compile(r"\d{4}-(?:03-31|06-30|09-30|12-31)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CACHE_KEY_FIELDS = (
    "annual_report_date",
    "current_interim_report_date",
    "prior_interim_report_date",
    "period_basis",
    "cache_key",
)
_ITEM_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "lrb": {
        "BIZTOTINCO": ("TOTAL_OPERATE_INCOME", "营业总收入"),
        "BIZINCO": ("OPERATE_INCOME", "营业收入"),
        "PARENETP": ("PARENT_NETPROFIT", "归属于母公司所有者的净利润"),
        "NETPROFIT": ("NETPROFIT", "净利润"),
        "PERPROFIT": ("OPERATE_PROFIT", "营业利润"),
    },
    "llb": {
        "MANANETR": ("NETCASH_OPERATE", "经营活动产生的现金流量净额"),
        "ACQUASSETCASH": (CAPEX_FIELD, "购建固定资产、无形资产和其他长期资产所支付的现金"),
    },
    "fzb": {
        "TOTASSET": ("TOTAL_ASSETS", "资产总计"),
        "TOTLIAB": ("TOTAL_LIABILITIES", "负债合计"),
        "PARESHARRIGH": ("TOTAL_PARENT_EQUITY", "归属于母公司股东权益合计"),
        "MINYSHARRIGH": ("MINORITY_EQUITY", "少数股东权益"),
        "RIGHAGGR": ("TOTAL_EQUITY", "所有者权益(或股东权益)合计"),
    },
}
_REVENUE_FIELDS = ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")


class SinaFinancialError(RuntimeError):
    """Base error for one secondary-source request."""


class SinaFinancialSchemaError(SinaFinancialError):
    """The response did not satisfy the pinned provider contract."""


class SinaFinancialResourceLimitError(SinaFinancialError):
    """The response exceeded a local resource budget."""


@dataclass(frozen=True)
class SinaStatementResult:
    code: str
    statement: str
    status: str
    records: tuple[dict[str, Any], ...] = ()
    error: str = ""
    raw_sha256: str | None = None
    cache_hit: bool = False
    raw_response: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class FinancialFallbackOutcome:
    financials: dict[str, dict[str, Any]]
    diagnostic: Mapping[str, Any] = field(default_factory=dict)


def normalize_a_share_code(value: Any) -> str:
    if isinstance(value, bool):
        raise TypeError("A-share code must be a string")
    if not isinstance(value, str):
        raise TypeError("A-share code must be a string")
    code = value.strip()
    if _CODE.fullmatch(code) is None:
        raise ValueError("A-share code must be a canonical six-digit SH/SZ code")
    return code


def _statement(value: Any) -> str:
    if not isinstance(value, str) or value not in _STATEMENTS:
        raise ValueError("statement must be lrb or llb")
    return value


def _request_params(code: str, statement: str) -> dict[str, str]:
    prefix = "sh" if code.startswith("6") else "sz"
    return {
        "paperCode": f"{prefix}{code}",
        "source": statement,
        "type": "0",
        "page": "1",
        "num": str(SINA_FINANCIAL_NUM_PERIODS),
    }


def _normalized_contract(contract: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(contract, Mapping):
        raise TypeError("financial fallback contract must be a mapping")
    normalized: dict[str, str] = {}
    for key in _CACHE_KEY_FIELDS:
        value = contract.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"financial fallback contract {key} must be non-empty text")
            normalized[key] = value.strip()
    for key in ("annual_report_date", "current_interim_report_date", "prior_interim_report_date"):
        if key not in normalized or _REPORT_DATE.fullmatch(normalized[key]) is None:
            raise ValueError(f"financial fallback contract {key} is invalid")
    return normalized


def _cache_identity(code: str, statement: str, contract: Mapping[str, str]) -> str:
    payload = json.dumps(
        {
            "adapter_version": SINA_FINANCIAL_ADAPTER_VERSION,
            "code": code,
            "statement": statement,
            "contract": dict(contract),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _strict_json(raw: bytes) -> Any:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SinaFinancialSchemaError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise SinaFinancialSchemaError(f"non-finite JSON constant: {value}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except SinaFinancialSchemaError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SinaFinancialSchemaError("invalid JSON in Sina financial response") from exc


def _validate_final_url(url: Any, expected_params: Mapping[str, str]) -> None:
    if not isinstance(url, str) or not url:
        raise SinaFinancialSchemaError("Sina response omitted final URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _SINA_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != _SINA_PATH
        or parsed.fragment
    ):
        raise SinaFinancialSchemaError("Sina response final URL left the pinned HTTPS endpoint")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    expected = {key: [value] for key, value in expected_params.items()}
    if query != expected:
        raise SinaFinancialSchemaError("Sina response final URL changed the request identity")


def _bounded_response_bytes(response: Any) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    if content_type not in {"application/json", "text/json"}:
        raise SinaFinancialSchemaError("Sina financial response is not JSON")
    declared = headers.get("Content-Length")
    if declared not in (None, ""):
        try:
            declared_size = int(declared)
        except (TypeError, ValueError) as exc:
            raise SinaFinancialSchemaError("invalid Content-Length in Sina financial response") from exc
        if declared_size < 0:
            raise SinaFinancialSchemaError("negative Content-Length in Sina financial response")
        if declared_size > MAX_SINA_FINANCIAL_RESPONSE_BYTES:
            raise SinaFinancialResourceLimitError("Sina financial response exceeds byte limit")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise SinaFinancialSchemaError("Sina financial response is not streamable")
    chunks: list[bytes] = []
    received = 0
    for chunk in iterator(chunk_size=SINA_RESPONSE_CHUNK_BYTES):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise SinaFinancialSchemaError("Sina financial response yielded non-byte content")
        received += len(chunk)
        if received > MAX_SINA_FINANCIAL_RESPONSE_BYTES:
            raise SinaFinancialResourceLimitError("Sina financial response exceeds byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _finite_financial_number(value: Any, *, canonical_field: str) -> float | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "--"}):
        return None
    if isinstance(value, bool):
        raise SinaFinancialSchemaError(f"{canonical_field} is boolean")
    if isinstance(value, str) and "," in value:
        raise SinaFinancialSchemaError(f"{canonical_field} contains an ambiguous thousands separator")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SinaFinancialSchemaError(f"{canonical_field} is not numeric") from exc
    if not parsed.is_finite() or abs(parsed) > MAX_ABS_FINANCIAL_VALUE:
        raise SinaFinancialSchemaError(f"{canonical_field} is non-finite or outside the unit bound")
    if canonical_field in {*_REVENUE_FIELDS, CAPEX_FIELD} and parsed < 0:
        raise SinaFinancialSchemaError(f"{canonical_field} is negative")
    result = float(parsed)
    if not math.isfinite(result):
        raise SinaFinancialSchemaError(f"{canonical_field} is non-finite")
    return result


def _canonical_report_date(period: Any) -> str:
    if not isinstance(period, str) or _PERIOD.fullmatch(period) is None:
        raise SinaFinancialSchemaError("Sina financial period is not YYYYMMDD")
    text = f"{period[:4]}-{period[4:6]}-{period[6:]}"
    if _REPORT_DATE.fullmatch(text) is None:
        raise SinaFinancialSchemaError("Sina financial period is not a quarter end")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise SinaFinancialSchemaError("Sina financial period is not a real date") from exc
    return text


def _period_metadata(value: Any, *, report_date: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SinaFinancialSchemaError(f"Sina financial period {report_date} is not an object")
    report_type = value.get("rType")
    currency = value.get("rCurrency")
    data_source = value.get("data_source")
    publish_date = value.get("publish_date")
    update_time = value.get("update_time")
    if report_type != "合并期末" or currency != "CNY" or data_source not in _OFFICIAL_DISCLOSURE_SOURCES:
        raise SinaFinancialSchemaError(f"Sina financial period {report_date} has an unsupported statement basis")
    if not isinstance(publish_date, str) or re.fullmatch(r"\d{8}", publish_date) is None:
        raise SinaFinancialSchemaError(f"Sina financial period {report_date} has invalid publish_date")
    try:
        published = datetime.strptime(publish_date, "%Y%m%d").date()
    except ValueError as exc:
        raise SinaFinancialSchemaError(f"Sina financial period {report_date} has invalid publish_date") from exc
    if published > datetime.now(tz=_SHANGHAI).date():
        raise SinaFinancialSchemaError(f"Sina financial period {report_date} is future-published")
    if isinstance(update_time, bool) or not isinstance(update_time, int) or update_time <= 0:
        raise SinaFinancialSchemaError(f"Sina financial period {report_date} has invalid update_time")
    return {
        "report_type": report_type,
        "currency": currency,
        "data_source": data_source,
        "is_audit": value.get("is_audit"),
        "audit_opinion": value.get("audit_opinion"),
        "publish_date": publish_date,
        "update_time": update_time,
    }


def _parse_statement(raw: bytes, *, code: str, statement: str) -> SinaStatementResult:
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    payload = _strict_json(raw)
    if not isinstance(payload, Mapping):
        raise SinaFinancialSchemaError("Sina financial response root is not an object")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise SinaFinancialSchemaError("Sina financial response omitted result")
    status = result.get("status")
    if not isinstance(status, Mapping) or status.get("code") not in (0, "0"):
        raise SinaFinancialSchemaError("Sina financial response has a non-zero business status")
    data = result.get("data")
    if not isinstance(data, Mapping) or "report_list" not in data:
        raise SinaFinancialSchemaError("Sina financial response omitted report_list")
    report_list = data.get("report_list")
    if not isinstance(report_list, Mapping):
        raise SinaFinancialSchemaError("Sina financial report_list is not an object")
    if len(report_list) > MAX_SINA_FINANCIAL_PERIODS:
        raise SinaFinancialResourceLimitError("Sina financial response contains too many periods")
    if not report_list:
        if data.get("report_count") != 0:
            raise SinaFinancialSchemaError("empty Sina report_list has inconsistent report_count")
        return SinaStatementResult(
            code,
            statement,
            "true_empty",
            raw_sha256=raw_sha256,
            raw_response=raw,
        )

    records: list[dict[str, Any]] = []
    any_component = False
    for period in sorted(report_list, reverse=True):
        report_date = _canonical_report_date(period)
        period_object = report_list[period]
        metadata = _period_metadata(period_object, report_date=report_date)
        items = period_object.get("data")
        if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
            raise SinaFinancialSchemaError(f"Sina financial period {report_date} has invalid items")
        if len(items) > MAX_SINA_FINANCIAL_ITEMS_PER_PERIOD:
            raise SinaFinancialResourceLimitError("Sina financial period contains too many items")
        selected: dict[str, float] = {}
        field_sources: dict[str, dict[str, Any]] = {}
        seen_source_fields: set[str] = set()
        for item in items:
            source_field = item.get("item_field")
            if source_field not in _ITEM_FIELDS[statement]:
                continue
            if source_field in seen_source_fields:
                raise SinaFinancialSchemaError(f"duplicate Sina item_field {source_field} for {report_date}")
            seen_source_fields.add(source_field)
            canonical_field, expected_title = _ITEM_FIELDS[statement][source_field]
            if item.get("item_title") != expected_title or item.get("item_source") != statement:
                raise SinaFinancialSchemaError(f"Sina item contract changed for {source_field}")
            number = _finite_financial_number(item.get("item_value"), canonical_field=canonical_field)
            if number is None:
                continue
            selected[canonical_field] = number
            field_sources[canonical_field] = {
                "source_field": source_field,
                "source_title": expected_title,
                "source_value": str(item.get("item_value")),
            }
        if not selected:
            continue
        any_component = True
        query = _request_params(code, statement)
        provenance = {
            "adapter_version": SINA_FINANCIAL_ADAPTER_VERSION,
            "source_id": "sina_company_finance_2022",
            "source_url": SINA_FINANCIAL_URL,
            "source_query": query,
            "source_raw_sha256": raw_sha256,
            "security_code": code,
            "report_date": report_date,
            "statement": statement,
            "metadata": metadata,
            "field_sources": field_sources,
        }
        record: dict[str, Any] = {"REPORT_DATE": report_date, **selected, "SOURCE_PROVENANCE": provenance}
        if CAPEX_FIELD in selected and selected[CAPEX_FIELD] > 0:
            record["CAPEX_PROVENANCE"] = sina_reported_capex_provenance(
                selected[CAPEX_FIELD],
                report_date=report_date,
                security_code=code,
                source_raw_sha256=raw_sha256,
                publish_date=metadata["publish_date"],
                update_time=metadata["update_time"],
                report_type=metadata["report_type"],
                currency=metadata["currency"],
                request_num=SINA_FINANCIAL_NUM_PERIODS,
            )
        records.append(record)
    return SinaStatementResult(
        code,
        statement,
        "ok" if any_component else "missing_component",
        tuple(records),
        raw_sha256=raw_sha256,
        raw_response=raw,
    )


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if isinstance(error, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        return isinstance(status, int) and (status in _RETRYABLE_HTTP_STATUSES or 500 <= status <= 599)
    return False


class SinaFinancialClient:
    def __init__(
        self,
        *,
        cache_dir: str | Path = DEFAULT_SINA_FINANCIAL_CACHE_DIR,
        session_factory: Callable[[], Any] = requests.Session,
        max_workers: int = SINA_FINANCIAL_MAX_WORKERS,
        timeout: int | float = SINA_FINANCIAL_TIMEOUT,
        retries: int = 3,
        retry_delay: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
            raise ValueError("retries must be a positive integer")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or float(timeout) <= 0:
            raise ValueError("timeout must be positive")
        self.cache_dir = Path(cache_dir)
        self.session_factory = session_factory
        self.max_workers = min(max_workers, SINA_FINANCIAL_MAX_WORKERS)
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = float(retry_delay)
        self.sleeper = sleeper
        self._local = threading.local()
        self._sessions: list[Any] = []
        self._lock = threading.Lock()
        self._stats: Counter[str] = Counter()
        self._status_counts: Counter[str] = Counter()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self.session_factory()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def _close_sessions(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        self._local = threading.local()

    def _cache(self, code: str, statement: str, contract: Mapping[str, str]) -> SafeFileCache:
        identity = _cache_identity(code, statement, contract)
        return SafeFileCache(
            self.cache_dir / f"{identity}.json.gz",
            schema_version=SINA_FINANCIAL_CACHE_SCHEMA_VERSION,
            ttl=SINA_FINANCIAL_CACHE_TTL_SECONDS,
            max_uncompressed_bytes=8 * 1024 * 1024,
        )

    def _load_cache(
        self,
        code: str,
        statement: str,
        contract: Mapping[str, str],
    ) -> SinaStatementResult | None:
        loaded = self._cache(code, statement, contract).load()
        if not loaded.hit:
            with self._lock:
                self._stats["cache_misses"] += 1
                if loaded.reason not in {"", "not_found", "expired"}:
                    self._stats["cache_invalid"] += 1
            return None
        value = loaded.value
        try:
            if (
                not isinstance(value, Mapping)
                or value.get("adapter_version") != SINA_FINANCIAL_ADAPTER_VERSION
                or value.get("code") != code
                or value.get("statement") != statement
                or value.get("contract") != dict(contract)
                or value.get("status") not in {"ok", "missing_component", "true_empty"}
            ):
                raise SinaFinancialSchemaError("cached Sina financial contract mismatch")
            raw_hash = value.get("raw_sha256")
            if not isinstance(raw_hash, str) or _SHA256.fullmatch(raw_hash) is None:
                raise SinaFinancialSchemaError("cached Sina financial hash is invalid")
            raw_response = value.get("raw_response")
            if not isinstance(raw_response, bytes) or len(raw_response) > MAX_SINA_FINANCIAL_RESPONSE_BYTES:
                raise SinaFinancialSchemaError("cached Sina financial raw response is invalid")
            if hashlib.sha256(raw_response).hexdigest() != raw_hash:
                raise SinaFinancialSchemaError("cached Sina financial raw response hash mismatch")
            reparsed = _parse_statement(raw_response, code=code, statement=statement)
            if reparsed.status != value.get("status"):
                raise SinaFinancialSchemaError("cached Sina financial status differs from strict replay")
            result = SinaStatementResult(
                code,
                statement,
                reparsed.status,
                tuple(deepcopy(dict(record)) for record in reparsed.records),
                raw_sha256=raw_hash,
                cache_hit=True,
                raw_response=raw_response,
            )
        except (KeyError, SinaFinancialError, TypeError, ValueError):
            with self._lock:
                self._stats["cache_invalid"] += 1
            return None
        with self._lock:
            self._stats["cache_hits"] += 1
        return result

    def _save_cache(
        self,
        result: SinaStatementResult,
        contract: Mapping[str, str],
    ) -> None:
        if (
            result.status not in {"ok", "missing_component", "true_empty"}
            or result.raw_sha256 is None
            or result.raw_response is None
        ):
            return
        self._cache(result.code, result.statement, contract).save(
            {
                "adapter_version": SINA_FINANCIAL_ADAPTER_VERSION,
                "code": result.code,
                "statement": result.statement,
                "contract": dict(contract),
                "status": result.status,
                "raw_sha256": result.raw_sha256,
                "raw_response": result.raw_response,
                "retrieved_at": time.time(),
            },
            ttl=(
                SINA_FINANCIAL_CACHE_TTL_SECONDS if result.status == "ok" else SINA_FINANCIAL_NEGATIVE_CACHE_TTL_SECONDS
            ),
        )

    def _network_fetch(self, code: str, statement: str) -> SinaStatementResult:
        params = _request_params(code, statement)
        last_error: BaseException | None = None
        for attempt in range(self.retries):
            response = None
            try:
                with SINA_FINANCIAL_REQUEST_SLOTS:
                    with self._lock:
                        self._stats["network_requests"] += 1
                    response = self._session().get(
                        SINA_FINANCIAL_URL,
                        params=params,
                        headers=SINA_FINANCIAL_HEADERS,
                        timeout=self.timeout,
                        stream=True,
                    )
                    response.raise_for_status()
                    _validate_final_url(getattr(response, "url", None), params)
                    raw = _bounded_response_bytes(response)
                return _parse_statement(raw, code=code, statement=statement)
            except SinaFinancialResourceLimitError as exc:
                return SinaStatementResult(code, statement, "resource_limit", error=str(exc))
            except SinaFinancialSchemaError as exc:
                return SinaStatementResult(code, statement, "schema_drift", error=str(exc))
            except requests.RequestException as exc:
                last_error = exc
                if not _is_transient(exc) or attempt + 1 >= self.retries:
                    break
                self.sleeper(self.retry_delay * (attempt + 1))
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
        return SinaStatementResult(code, statement, "source_unavailable", error=str(last_error or "unknown error"))

    def _fetch_one(
        self,
        code: str,
        statement: str,
        contract: Mapping[str, str],
        *,
        force_refresh: bool,
    ) -> SinaStatementResult:
        if not force_refresh:
            cached = self._load_cache(code, statement, contract)
            if cached is not None:
                with self._lock:
                    self._status_counts[cached.status] += 1
                return cached
        result = self._network_fetch(code, statement)
        try:
            self._save_cache(result, contract)
        except Exception:
            # Cache persistence is an optimisation.  A fully validated live
            # response must remain usable if the runner cache is unavailable.
            with self._lock:
                self._stats["cache_write_errors"] += 1
        with self._lock:
            self._status_counts[result.status] += 1
        return result

    def fetch_one(
        self,
        code: Any,
        statement: Any,
        *,
        contract: Mapping[str, Any],
        force_refresh: bool = False,
    ) -> SinaStatementResult:
        normalized_code = normalize_a_share_code(code)
        normalized_statement = _statement(statement)
        normalized_contract = _normalized_contract(contract)
        try:
            return self._fetch_one(
                normalized_code,
                normalized_statement,
                normalized_contract,
                force_refresh=force_refresh,
            )
        finally:
            self._close_sessions()

    def fetch_many(
        self,
        requests_: Collection[tuple[Any, Any]],
        *,
        contract: Mapping[str, Any],
        force_refresh: bool = False,
    ) -> dict[tuple[str, str], SinaStatementResult]:
        normalized_contract = _normalized_contract(contract)
        requests_normalized = tuple(
            sorted({(normalize_a_share_code(code), _statement(statement)) for code, statement in requests_})
        )
        results: dict[tuple[str, str], SinaStatementResult] = {}
        try:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(requests_normalized)))) as executor:
                futures = {
                    executor.submit(
                        self._fetch_one,
                        code,
                        statement,
                        normalized_contract,
                        force_refresh=force_refresh,
                    ): (code, statement)
                    for code, statement in requests_normalized
                }
                for future in as_completed(futures):
                    identity = futures[future]
                    try:
                        results[identity] = future.result()
                    except Exception as exc:  # A worker failure remains isolated secondary-source telemetry.
                        results[identity] = SinaStatementResult(
                            identity[0], identity[1], "source_unavailable", error=f"{type(exc).__name__}: {exc}"
                        )
            return {identity: results[identity] for identity in requests_normalized}
        finally:
            self._close_sessions()

    def diagnostic(self) -> dict[str, Any]:
        with self._lock:
            return {
                **dict(sorted(self._stats.items())),
                "status_counts": dict(sorted(self._status_counts.items())),
                "max_workers": self.max_workers,
            }


def _finite(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _rows(company: Mapping[str, Any], dataset: str, report_date: str) -> list[Mapping[str, Any]]:
    value = company.get(dataset, [])
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, Mapping) and row.get("REPORT_DATE") == report_date]


def _field_missing(row: Mapping[str, Any] | None, fields: tuple[str, ...]) -> bool:
    return row is None or all(_finite(row.get(field)) is None for field in fields)


def _gap_plan(
    financials: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, str],
    codes: Collection[str] | None,
) -> tuple[tuple[tuple[str, str], ...], dict[str, list[str]], int]:
    population = sorted({normalize_a_share_code(code) for code in (codes if codes is not None else financials.keys())})
    requests_: set[tuple[str, str]] = set()
    revenue_codes: set[str] = set()
    fcff_codes: set[str] = set()
    gap_identities = 0
    revenue_targets = (
        ("revenue_history", contract["annual_report_date"]),
        ("income_interim", contract["current_interim_report_date"]),
        ("income_interim", contract["prior_interim_report_date"]),
    )
    cashflow_targets = (
        ("cashflow", contract["annual_report_date"]),
        ("cashflow_interim", contract["current_interim_report_date"]),
        ("cashflow_interim", contract["prior_interim_report_date"]),
    )
    for code in population:
        company = financials.get(code)
        if not isinstance(company, Mapping):
            company = {}
        for dataset, report_date in revenue_targets:
            matches = _rows(company, dataset, report_date)
            row = matches[0] if len(matches) == 1 else None
            if len(matches) != 1 or _field_missing(row, _REVENUE_FIELDS):
                gap_identities += 1
                revenue_codes.add(code)
                requests_.add((code, "lrb"))
        for dataset, report_date in cashflow_targets:
            matches = _rows(company, dataset, report_date)
            row = matches[0] if len(matches) == 1 else None
            ocf_missing = len(matches) != 1 or _field_missing(row, ("NETCASH_OPERATE",))
            capex_missing = len(matches) != 1 or _field_missing(row, (CAPEX_FIELD,))
            provenance_missing = True
            if row is not None and not capex_missing:
                provenance_missing = (
                    validate_capex_provenance(
                        row.get("CAPEX_PROVENANCE"),
                        expected_value=row.get(CAPEX_FIELD),
                        expected_report_date=report_date,
                        expected_security_code=code,
                    )
                    != "complete"
                )
            gap_identities += int(ocf_missing) + int(capex_missing or provenance_missing)
            if ocf_missing or capex_missing or provenance_missing:
                fcff_codes.add(code)
                requests_.add((code, "llb"))
    return (
        tuple(sorted(requests_)),
        {"revenue": sorted(revenue_codes), "fcff": sorted(fcff_codes)},
        gap_identities,
    )


def _mutable_period_row(company: dict[str, Any], dataset: str, report_date: str) -> dict[str, Any] | None:
    raw = company.setdefault(dataset, [])
    if isinstance(raw, Mapping):
        raw = [dict(raw)]
        company[dataset] = raw
    if not isinstance(raw, list):
        return None
    matches = [row for row in raw if isinstance(row, dict) and row.get("REPORT_DATE") == report_date]
    if len(matches) > 1:
        return None
    if matches:
        return matches[0]
    row: dict[str, Any] = {"REPORT_DATE": report_date}
    if dataset.endswith("interim"):
        row["period_end"] = report_date[5:]
    raw.append(row)
    raw.sort(key=lambda item: str(item.get("REPORT_DATE") or ""))
    return row


def _field_provenance(source: Mapping[str, Any], canonical_field: str) -> dict[str, Any]:
    provenance = deepcopy(dict(source.get("SOURCE_PROVENANCE", {})))
    fields = provenance.pop("field_sources", {})
    provenance["field"] = deepcopy(fields.get(canonical_field)) if isinstance(fields, Mapping) else None
    provenance["canonical_field"] = canonical_field
    return provenance


def backfill_strict_ttm_gaps(
    financials: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
    *,
    codes: Collection[str] | None = None,
    client: Any | None = None,
    force_refresh: bool = False,
    max_target_requests: int = SINA_FINANCIAL_MAX_TARGET_REQUESTS,
) -> FinancialFallbackOutcome:
    """Overlay only absent strict-TTM fields; preserve every finite primary fact."""

    started = time.monotonic()
    if isinstance(max_target_requests, bool) or not isinstance(max_target_requests, int) or max_target_requests < 1:
        raise ValueError("max_target_requests must be a positive integer")
    normalized_contract = _normalized_contract(contract)
    candidate_requests, target_codes_by_metric, before_gaps = _gap_plan(financials, normalized_contract, codes)
    requests_ = candidate_requests[:max_target_requests]
    skipped_requests = candidate_requests[max_target_requests:]
    active_client = client or SinaFinancialClient()
    results = (
        active_client.fetch_many(requests_, contract=normalized_contract, force_refresh=force_refresh)
        if requests_
        else {}
    )
    output: dict[str, dict[str, Any]] = dict(financials)
    cloned: set[str] = set()
    counters: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    conflict_codes: set[str] = set()
    filled_codes: set[str] = set()

    revenue_target_dataset = {
        normalized_contract["annual_report_date"]: "revenue_history",
        normalized_contract["current_interim_report_date"]: "income_interim",
        normalized_contract["prior_interim_report_date"]: "income_interim",
    }
    cashflow_target_dataset = {
        normalized_contract["annual_report_date"]: "cashflow",
        normalized_contract["current_interim_report_date"]: "cashflow_interim",
        normalized_contract["prior_interim_report_date"]: "cashflow_interim",
    }
    for (code, statement), result in sorted(results.items()):
        status_counts[result.status] += 1
        if result.status != "ok":
            continue
        if code not in cloned:
            current = output.get(code)
            output[code] = deepcopy(dict(current)) if isinstance(current, Mapping) else {}
            cloned.add(code)
        company = output[code]
        target_datasets = revenue_target_dataset if statement == "lrb" else cashflow_target_dataset
        for source in result.records:
            report_date = source.get("REPORT_DATE")
            dataset = target_datasets.get(report_date)
            if dataset is None:
                continue
            candidate_fields = _REVENUE_FIELDS if statement == "lrb" else ("NETCASH_OPERATE", CAPEX_FIELD)
            for canonical_field in candidate_fields:
                source_value = _finite(source.get(canonical_field))
                if source_value is None:
                    continue
                existing_matches = _rows(company, dataset, report_date)
                existing_row = existing_matches[0] if len(existing_matches) == 1 else None
                existing_value = _finite(existing_row.get(canonical_field)) if existing_row is not None else None
                if existing_value is not None:
                    if not math.isclose(existing_value, source_value, rel_tol=1e-10, abs_tol=0.02):
                        counters["conflicts"] += 1
                        conflict_codes.add(code)
                        continue
                    if canonical_field == CAPEX_FIELD and (
                        validate_capex_provenance(
                            existing_row.get("CAPEX_PROVENANCE"),
                            expected_value=existing_value,
                            expected_report_date=report_date,
                            expected_security_code=code,
                        )
                        != "complete"
                    ):
                        existing_row["CAPEX_PROVENANCE"] = deepcopy(source["CAPEX_PROVENANCE"])
                        counters["filled_provenance"] += 1
                        filled_codes.add(code)
                    continue
                if source_value == 0.0 and canonical_field in {*_REVENUE_FIELDS, CAPEX_FIELD}:
                    counters["unverified_zero"] += 1
                    continue
                target = _mutable_period_row(company, dataset, report_date)
                if target is None:
                    counters["conflicts"] += 1
                    conflict_codes.add(code)
                    continue
                target[canonical_field] = source_value
                if canonical_field == CAPEX_FIELD:
                    target["CAPEX_PROVENANCE"] = deepcopy(source["CAPEX_PROVENANCE"])
                else:
                    target[f"{canonical_field}_PROVENANCE"] = _field_provenance(source, canonical_field)
                counters["filled_fields"] += 1
                filled_codes.add(code)

    _remaining_requests, _remaining_codes, remaining_gaps = _gap_plan(output, normalized_contract, codes)
    client_diagnostic = active_client.diagnostic() if callable(getattr(active_client, "diagnostic", None)) else {}
    diagnostic: dict[str, Any] = {
        "adapter_version": SINA_FINANCIAL_ADAPTER_VERSION,
        "strategy": "eastmoney_bulk_primary_sina_gap_only_secondary",
        "candidate_requests": len(candidate_requests),
        "target_requests": len(requests_),
        "skipped_requests": len(skipped_requests),
        "budget_exhausted": bool(skipped_requests),
        "request_budget": max_target_requests,
        "target_codes": len({code for code, _statement_name in requests_}),
        "target_codes_by_metric": target_codes_by_metric,
        "gap_identities_before": before_gaps,
        "gap_identities_after": remaining_gaps,
        "filled_fields": counters["filled_fields"],
        "filled_provenance": counters["filled_provenance"],
        "filled_codes": sorted(filled_codes),
        "conflicts": counters["conflicts"],
        "conflict_codes": sorted(conflict_codes),
        "unverified_zero": counters["unverified_zero"],
        "status_counts": dict(sorted(status_counts.items())),
        "client": client_diagnostic,
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    return FinancialFallbackOutcome(output, diagnostic)


# --- Annual-history overlay (Patch 2026-08-10) ---------------------------------
#
# Eastmoney is still the primary.  The TTM overlay above fills exact-strict-TTM
# identities; this second overlay fills the ANNUAL trend series that Type 1/3/7
# scoring consumes (revenue_history, income_history, cashflow, balance) from the
# same Sina getFinanceReport2022 source (num=40 -> ~10 annual points).  It only
# writes fields that remain absent, never overwrites a finite primary value,
# never converts a missing CAPEX to zero, and carries the same provenance
# contract (data.capex_evidence) as the TTM overlay.

SINA_HISTORY_MAX_TARGET_CODES = 300
_MIN_ANNUAL_REVENUE_POINTS = 4
_MIN_ANNUAL_CASHFLOW_POINTS = 3
_MIN_ANNUAL_BALANCE_POINTS = 3
_HISTORY_STATEMENTS = ("lrb", "llb", "fzb")
_INCOME_HISTORY_FIELDS = ("PARENT_NETPROFIT", "NETPROFIT", "OPERATE_PROFIT")
_CASHFLOW_HISTORY_FIELDS = ("NETCASH_OPERATE", CAPEX_FIELD)
_BALANCE_HISTORY_FIELDS = (
    "TOTAL_ASSETS",
    "TOTAL_LIABILITIES",
    "TOTAL_EQUITY",
    "TOTAL_PARENT_EQUITY",
    "MINORITY_EQUITY",
)


def _annual_records(records: Collection[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only 12-31 annual observations, sorted ascending by report date."""
    return sorted(
        (dict(record) for record in records if str(record.get("REPORT_DATE") or "").endswith("-12-31")),
        key=lambda record: str(record.get("REPORT_DATE") or ""),
    )


def _history_gap_plan(
    financials: Mapping[str, Mapping[str, Any]],
    codes: Collection[str],
) -> list[str]:
    """Codes whose annual trend series are still too short to score Type 1/3/7.

    A code qualifies when any of its three annual series is below its minimum:
    revenue < 4 annual points (Type 3 CAGR / Type 1 structural decline),
    cashflow < 3 (Type 1 FCF / OCF history), balance < 3 (Type 7 ROE replay).
    """
    needs: list[str] = []
    for code in sorted(codes):
        company = financials.get(code)
        if not isinstance(company, Mapping):
            needs.append(code)
            continue
        if (
            len(_annual_records(company.get("revenue_history") or ())) < _MIN_ANNUAL_REVENUE_POINTS
            or len(_annual_records(company.get("cashflow") or ())) < _MIN_ANNUAL_CASHFLOW_POINTS
            or len(_annual_records(company.get("balance") or ())) < _MIN_ANNUAL_BALANCE_POINTS
        ):
            needs.append(code)
    return needs


def _overlay_history_fields(
    output: dict[str, dict[str, Any]],
    code: str,
    dataset: str,
    report_date: str,
    record: Mapping[str, Any],
    candidate_fields: Sequence[str],
    counters: Counter[str],
    conflict_codes: set[str],
    filled_codes: set[str],
    *,
    allow_unverified_zero: bool = False,
) -> None:
    """Overlay one record's fields onto one annual period, primary-first.

    A field that already carries a finite primary value is preserved; a
    disagreeing secondary value counts as a conflict and is dropped.  Zero
    revenue / zero CAPEX are not auto-confirmed (they may hide a missing
    value).  Written values carry provenance bound to the raw response.
    """
    company = output.setdefault(code, {})
    for canonical_field in candidate_fields:
        source_value = _finite(record.get(canonical_field))
        if source_value is None:
            continue
        existing_matches = _rows(company, dataset, report_date)
        existing_row = existing_matches[0] if len(existing_matches) == 1 else None
        existing_value = _finite(existing_row.get(canonical_field)) if existing_row is not None else None
        if existing_value is not None:
            if not math.isclose(existing_value, source_value, rel_tol=1e-10, abs_tol=0.02):
                counters["conflicts"] += 1
                conflict_codes.add(code)
                continue
            if canonical_field == CAPEX_FIELD and (
                validate_capex_provenance(
                    existing_row.get("CAPEX_PROVENANCE"),
                    expected_value=existing_value,
                    expected_report_date=report_date,
                    expected_security_code=code,
                )
                != "complete"
            ):
                existing_row["CAPEX_PROVENANCE"] = deepcopy(record.get("CAPEX_PROVENANCE") or {})
                counters["filled_provenance"] += 1
                filled_codes.add(code)
            continue
        if source_value == 0.0 and not allow_unverified_zero:
            counters["unverified_zero"] += 1
            continue
        target = _mutable_period_row(company, dataset, report_date)
        if target is None:
            counters["conflicts"] += 1
            conflict_codes.add(code)
            continue
        target[canonical_field] = source_value
        if canonical_field == CAPEX_FIELD:
            target["CAPEX_PROVENANCE"] = deepcopy(record.get("CAPEX_PROVENANCE") or {})
        else:
            target[f"{canonical_field}_PROVENANCE"] = _field_provenance(record, canonical_field)
        counters["filled_fields"] += 1
        filled_codes.add(code)


def backfill_history_gaps(
    financials: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
    *,
    codes: Collection[str] | None = None,
    client: Any | None = None,
    force_refresh: bool = False,
    max_target_codes: int = SINA_HISTORY_MAX_TARGET_CODES,
) -> FinancialFallbackOutcome:
    """Overlay missing annual trend series from Sina multi-period statements.

    ``codes`` restricts the candidate pool (gap-code lists); when omitted every
    code already present in ``financials`` is considered.  A per-run code
    budget keeps the secondary source load bounded; the rest are skipped and
    reported so the daily workflow rolls through them over successive builds.
    """

    started = time.monotonic()
    if isinstance(max_target_codes, bool) or not isinstance(max_target_codes, int) or max_target_codes < 1:
        raise ValueError("max_target_codes must be a positive integer")
    normalized_contract = _normalized_contract(contract)
    population = sorted(codes) if codes is not None else sorted(financials.keys())
    candidate_codes = _history_gap_plan(financials, population)
    target_codes = candidate_codes[:max_target_codes]
    skipped_codes = candidate_codes[max_target_codes:]
    requests_: list[tuple[str, str]] = [(code, statement) for code in target_codes for statement in _HISTORY_STATEMENTS]
    active_client = client or SinaFinancialClient()
    results = (
        active_client.fetch_many(requests_, contract=normalized_contract, force_refresh=force_refresh)
        if requests_
        else {}
    )
    output: dict[str, dict[str, Any]] = dict(financials)
    counters: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    conflict_codes: set[str] = set()
    filled_codes: set[str] = set()
    for code in target_codes:
        for statement in _HISTORY_STATEMENTS:
            result = results.get((code, statement))
            if result is None:
                continue
            status_counts[result.status] += 1
            if result.status != "ok":
                continue
            for record in _annual_records(result.records):
                report_date = record["REPORT_DATE"]
                if statement == "lrb":
                    _overlay_history_fields(
                        output,
                        code,
                        "revenue_history",
                        report_date,
                        record,
                        _REVENUE_FIELDS,
                        counters,
                        conflict_codes,
                        filled_codes,
                    )
                    _overlay_history_fields(
                        output,
                        code,
                        "income_history",
                        report_date,
                        record,
                        _INCOME_HISTORY_FIELDS,
                        counters,
                        conflict_codes,
                        filled_codes,
                    )
                elif statement == "llb":
                    _overlay_history_fields(
                        output,
                        code,
                        "cashflow",
                        report_date,
                        record,
                        _CASHFLOW_HISTORY_FIELDS,
                        counters,
                        conflict_codes,
                        filled_codes,
                    )
                else:  # fzb
                    _overlay_history_fields(
                        output,
                        code,
                        "balance",
                        report_date,
                        record,
                        _BALANCE_HISTORY_FIELDS,
                        counters,
                        conflict_codes,
                        filled_codes,
                    )
                    parent = record.get("TOTAL_PARENT_EQUITY")
                    if parent is not None:
                        aliased = dict(record)
                        aliased["PARENT_EQUITY"] = parent
                        _overlay_history_fields(
                            output,
                            code,
                            "balance",
                            report_date,
                            aliased,
                            ("PARENT_EQUITY",),
                            counters,
                            conflict_codes,
                            filled_codes,
                        )
    client_diagnostic = active_client.diagnostic() if callable(getattr(active_client, "diagnostic", None)) else {}
    diagnostic: dict[str, Any] = {
        "adapter_version": SINA_FINANCIAL_ADAPTER_VERSION,
        "strategy": "eastmoney_primary_sina_annual_history_secondary",
        "candidate_codes": len(candidate_codes),
        "target_codes": len(target_codes),
        "skipped_codes": len(skipped_codes),
        "budget_exhausted": bool(skipped_codes),
        "code_budget": max_target_codes,
        "requests": len(requests_),
        "filled_fields": counters["filled_fields"],
        "filled_provenance": counters["filled_provenance"],
        "filled_codes": sorted(filled_codes),
        "conflicts": counters["conflicts"],
        "conflict_codes": sorted(conflict_codes),
        "unverified_zero": counters["unverified_zero"],
        "status_counts": dict(sorted(status_counts.items())),
        "client": client_diagnostic,
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    return FinancialFallbackOutcome(output, diagnostic)
