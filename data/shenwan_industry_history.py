"""Official Shenwan industry history for point-in-time peer audits.

This module deliberately does **not** replace :mod:`data.industry`.  DS_DCF's
production industry taxonomy is a model input, while Shenwan's numeric
classification is a separate point-in-time research taxonomy.  Mixing the two
would create both semantic drift and look-ahead bias.  The records exposed here
are therefore limited to historical peer selection and explicit drift audits.

The full-market source is Shenwan Research's public ``.xls`` file.  CNINFO's
official historical-industry API is a bounded, per-code fallback when that
file is temporarily unavailable or does not yet include a newly listed code.
Both paths retain the exact response SHA-256 and reject identity, date,
classification-standard and duplicate-key violations.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import requests

from data.as_of import shanghai_today
from data.cache import SafeCacheError, SafeFileCache
from data.provider_http import RequestRateLimiter, read_bounded_response_bytes


SHENWAN_XLS_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
CNINFO_HISTORY_URL = "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
SHENWAN_XLS_SOURCE = "申万研究行业分类历史公开表"
CNINFO_SOURCE = "巨潮资讯历史行业分类 API"
MODEL_ID = "shenwan-industry-history-v1"
CACHE_SCHEMA_VERSION = 1
SHENWAN_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "industry_history"
SHENWAN_XLS_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CNINFO_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
CNINFO_EMPTY_CACHE_TTL_SECONDS = 15 * 60
MAX_XLS_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_CNINFO_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_XLS_CACHE_BYTES = 36 * 1024 * 1024
MAX_CNINFO_CACHE_BYTES = 4 * 1024 * 1024
MAX_XLS_RECORDS = 100_000
MAX_CNINFO_RECORDS_PER_CODE = 100
MAX_CNINFO_REQUESTS_PER_BATCH = 128
MIN_PRODUCTION_XLS_RECORDS = 10_000
MIN_PRODUCTION_XLS_COMPANIES = 4_000
MIN_PRODUCTION_XLS_L1_CODES = 20

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CODE_RE = re.compile(r"^\d{6}$")
_INDUSTRY_CODE_RE = re.compile(r"^\d{6}$")
_CNINFO_STANDARDS = {
    "008003": ("shenwan_current", "申银万国行业分类标准"),
    "008018": ("shenwan_legacy", "申银万国行业分类标准(旧)"),
}
_CLASSIFICATION_STANDARDS = {
    "shenwan_current",
    "shenwan_legacy",
    "shenwan_official_workbook",
}
_CNINFO_AES_KEY = b"1234567887654321"
_SOURCE_HEADERS = {
    "Accept": "application/vnd.ms-excel,application/octet-stream;q=0.9,*/*;q=0.1",
    "User-Agent": "DS-DCF/industry-history (+https://github.com/Muguett-DBY/quant-buy-signals)",
}
_CNINFO_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://webapi.cninfo.com.cn",
    "Referer": "https://webapi.cninfo.com.cn/",
    "User-Agent": "DS-DCF/industry-history (+https://github.com/Muguett-DBY/quant-buy-signals)",
}


class ShenwanIndustryHistoryError(RuntimeError):
    """An official industry-history source violated its data contract."""


@dataclass(frozen=True)
class ShenwanIndustryRecord:
    code: str
    effective_from: str
    industry_code: str
    l1_code: str
    l2_code: str
    classification_standard: str
    source_name: str
    source_url: str
    source_sha256: str
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "effective_from": self.effective_from,
            "industry_code": self.industry_code,
            "l1_code": self.l1_code,
            "l2_code": self.l2_code,
            "classification_standard": self.classification_standard,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "source_sha256": self.source_sha256,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ShenwanIndustryHistory:
    records: tuple[ShenwanIndustryRecord, ...]
    source_name: str
    source_url: str
    source_sha256: str
    cache_hit: bool = False


@dataclass(frozen=True)
class ShenwanIndustryBatch:
    histories: Mapping[str, ShenwanIndustryHistory]
    failures: Mapping[str, str]
    request_count: int


@dataclass(frozen=True)
class ShenwanIndustryResolution:
    records: tuple[ShenwanIndustryRecord, ...]
    requested_codes: tuple[str, ...]
    primary_source_available: bool
    fallback_codes: tuple[str, ...]
    unresolved_codes: tuple[str, ...]
    source_errors: Mapping[str, str]


def _normalise_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    text = text.zfill(6) if text.isdigit() else text
    if not _CODE_RE.fullmatch(text):
        raise ShenwanIndustryHistoryError("industry history contains an invalid company code")
    return text


def _normalise_industry_code(value: Any) -> str:
    text = str(value).strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.startswith("S"):
        text = text[1:]
    text = text.zfill(6) if text.isdigit() else text
    if not _INDUSTRY_CODE_RE.fullmatch(text):
        raise ShenwanIndustryHistoryError("industry history contains an invalid Shenwan industry code")
    return text


def _normalise_date(value: Any, *, field: str, allow_empty: bool = False) -> str | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        if allow_empty:
            return None
        raise ShenwanIndustryHistoryError(f"industry history {field} is missing")
    text = str(value).strip()
    if not text:
        if allow_empty:
            return None
        raise ShenwanIndustryHistoryError(f"industry history {field} is missing")
    try:
        parsed = pd.Timestamp(value).date()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShenwanIndustryHistoryError(f"industry history {field} is invalid") from exc
    return parsed.isoformat()


def _record(
    *,
    code: Any,
    effective_from: Any,
    industry_code: Any,
    classification_standard: str,
    source_name: str,
    source_url: str,
    source_sha256: str,
    updated_at: Any = None,
) -> ShenwanIndustryRecord:
    if not all(isinstance(value, str) for value in (classification_standard, source_name, source_url, source_sha256)):
        raise ShenwanIndustryHistoryError("industry history source identity is invalid")
    if classification_standard not in _CLASSIFICATION_STANDARDS:
        raise ShenwanIndustryHistoryError("industry history classification standard is invalid")
    if not source_name.strip():
        raise ShenwanIndustryHistoryError("industry history source name is missing")
    parsed_source_url = urlsplit(source_url)
    try:
        source_port = parsed_source_url.port
    except ValueError as exc:
        raise ShenwanIndustryHistoryError("industry history source URL is not a pinned HTTPS endpoint") from exc
    if (
        parsed_source_url.scheme != "https"
        or (parsed_source_url.hostname, parsed_source_url.path)
        not in {
            ("www.swsresearch.com", "/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"),
            ("webapi.cninfo.com.cn", "/api/stock/p_stock2110"),
        }
        or source_port not in {None, 443}
        or parsed_source_url.username
        or parsed_source_url.password
        or parsed_source_url.fragment
    ):
        raise ShenwanIndustryHistoryError("industry history source URL is not a pinned HTTPS endpoint")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ShenwanIndustryHistoryError("industry history source hash is invalid")
    normalized_industry = _normalise_industry_code(industry_code)
    effective = _normalise_date(effective_from, field="effective date")
    updated = _normalise_date(updated_at, field="update date", allow_empty=True)
    if updated is not None and updated < effective:
        raise ShenwanIndustryHistoryError("industry history update date precedes its effective date")
    return ShenwanIndustryRecord(
        code=_normalise_code(code),
        effective_from=str(effective),
        industry_code=normalized_industry,
        l1_code=normalized_industry[:2] + "0000",
        l2_code=normalized_industry[:4] + "00",
        classification_standard=classification_standard,
        source_name=source_name,
        source_url=source_url,
        source_sha256=source_sha256,
        updated_at=updated,
    )


def _validate_records(
    records: Iterable[ShenwanIndustryRecord],
    *,
    max_records: int,
    min_records: int = 0,
) -> tuple[ShenwanIndustryRecord, ...]:
    prepared = tuple(records)
    if len(prepared) < min_records:
        raise ShenwanIndustryHistoryError("industry history is unexpectedly sparse")
    if len(prepared) > max_records:
        raise ShenwanIndustryHistoryError("industry history exceeds its row limit")
    exact_keys: set[tuple[str, str, str, str]] = set()
    point_keys: dict[tuple[str, str, str], str] = {}
    for item in prepared:
        exact = (item.code, item.effective_from, item.classification_standard, item.industry_code)
        if exact in exact_keys:
            raise ShenwanIndustryHistoryError("industry history contains duplicate records")
        exact_keys.add(exact)
        point = (item.code, item.effective_from, item.classification_standard)
        prior = point_keys.setdefault(point, item.industry_code)
        if prior != item.industry_code:
            raise ShenwanIndustryHistoryError("industry history contains conflicting point-in-time classifications")
    return tuple(
        sorted(
            prepared,
            key=lambda item: (
                item.code,
                item.effective_from,
                item.classification_standard != "shenwan_current",
                item.industry_code,
            ),
        )
    )


def parse_shenwan_xls(
    raw: bytes,
    *,
    source_url: str = SHENWAN_XLS_URL,
    min_records: int = 1,
) -> ShenwanIndustryHistory:
    """Parse and fully validate Shenwan's public legacy-Excel workbook."""

    if not isinstance(raw, bytes) or not raw.startswith(_OLE2_MAGIC):
        raise ShenwanIndustryHistoryError("Shenwan response is not an OLE2 .xls workbook")
    if len(raw) > MAX_XLS_RESPONSE_BYTES:
        raise ShenwanIndustryHistoryError("Shenwan workbook exceeds its byte limit")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        frame = pd.read_excel(io.BytesIO(raw), engine="xlrd", dtype=object)
    except Exception as exc:
        raise ShenwanIndustryHistoryError("cannot parse Shenwan .xls workbook") from exc
    if not isinstance(frame, pd.DataFrame):
        raise ShenwanIndustryHistoryError("Shenwan workbook parser did not return a table")
    normalized_columns = [str(column).strip() for column in frame.columns]
    if len(set(normalized_columns)) != len(normalized_columns):
        raise ShenwanIndustryHistoryError("Shenwan workbook contains duplicate column names")
    columns = dict(zip(normalized_columns, frame.columns, strict=True))
    required = {"股票代码", "计入日期", "行业代码"}
    missing = required - set(columns)
    if missing:
        raise ShenwanIndustryHistoryError(f"Shenwan workbook is missing columns: {sorted(missing)}")
    update_column = columns.get("更新日期")
    records = []
    for _, row in frame.iterrows():
        code_value = row[columns["股票代码"]]
        industry_value = row[columns["行业代码"]]
        effective_value = row[columns["计入日期"]]
        if pd.isna(code_value) and pd.isna(industry_value) and pd.isna(effective_value):
            continue
        records.append(
            _record(
                code=code_value,
                effective_from=effective_value,
                industry_code=industry_value,
                classification_standard="shenwan_official_workbook",
                source_name=SHENWAN_XLS_SOURCE,
                source_url=source_url,
                source_sha256=digest,
                updated_at=row[update_column] if update_column is not None else None,
            )
        )
    validated = _validate_records(records, max_records=MAX_XLS_RECORDS, min_records=min_records)
    return ShenwanIndustryHistory(
        records=validated,
        source_name=SHENWAN_XLS_SOURCE,
        source_url=source_url,
        source_sha256=digest,
    )


def _strict_final_url(response: Any, expected_url: str) -> None:
    actual = str(getattr(response, "url", expected_url) or expected_url)
    expected = urlsplit(expected_url)
    final = urlsplit(actual)
    try:
        final_port = final.port
    except ValueError as exc:
        raise ShenwanIndustryHistoryError("industry source redirected outside its pinned HTTPS endpoint") from exc
    if (
        final.scheme != "https"
        or final.hostname != expected.hostname
        or final.path != expected.path
        or final_port not in {None, 443}
        or final.username
        or final.password
        or final.fragment
    ):
        raise ShenwanIndustryHistoryError("industry source redirected outside its pinned HTTPS endpoint")


def _history_cache_payload(raw: bytes, contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": dict(contract),
        "raw_response_base64": base64.b64encode(raw).decode("ascii"),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _history_from_cache(payload: Any, contract: Mapping[str, Any]) -> ShenwanIndustryHistory:
    expected_fields = {"contract", "raw_response_base64", "source_sha256"}
    if not isinstance(payload, Mapping) or set(payload) != expected_fields or payload.get("contract") != contract:
        raise ShenwanIndustryHistoryError("industry-history cache contract mismatch")
    source_sha = str(payload.get("source_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
        raise ShenwanIndustryHistoryError("industry-history cache source hash is invalid")
    try:
        raw = base64.b64decode(str(payload.get("raw_response_base64") or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ShenwanIndustryHistoryError("industry-history cache raw response is invalid") from exc
    if hashlib.sha256(raw).hexdigest() != source_sha:
        raise ShenwanIndustryHistoryError("industry-history cache raw response hash differs")
    source_format = contract.get("source_format")
    if source_format == "xls":
        history = parse_shenwan_xls(
            raw,
            source_url=str(contract.get("source_url") or ""),
            min_records=int(contract.get("minimum_records") or 0),
        )
    elif source_format == "json":
        try:
            cutoff = date.fromisoformat(str(contract.get("as_of") or ""))
        except ValueError as exc:
            raise ShenwanIndustryHistoryError("industry-history cache cutoff is invalid") from exc
        history = _parse_cninfo_response(
            raw,
            requested_code=_normalise_code(contract.get("code")),
            as_of=cutoff,
            source_url=str(contract.get("source_url") or ""),
        )
    else:
        raise ShenwanIndustryHistoryError("industry-history cache source format is invalid")
    if history.source_sha256 != source_sha:
        raise ShenwanIndustryHistoryError("industry-history cache replay hash differs")
    if int(contract.get("minimum_records") or 0) >= MIN_PRODUCTION_XLS_RECORDS:
        if (
            len({record.code for record in history.records}) < MIN_PRODUCTION_XLS_COMPANIES
            or len({record.l1_code for record in history.records}) < MIN_PRODUCTION_XLS_L1_CODES
        ):
            raise ShenwanIndustryHistoryError("industry-history cache fails its market-coverage contract")
    return replace(history, cache_hit=True)


def fetch_shenwan_industry_history(
    *,
    session: Any = requests,
    cache_dir: str | Path = SHENWAN_CACHE_DIR,
    cache_ttl_seconds: int = SHENWAN_XLS_CACHE_TTL_SECONDS,
    use_cache: bool = True,
    min_records: int = MIN_PRODUCTION_XLS_RECORDS,
) -> ShenwanIndustryHistory:
    """Fetch the official full-market workbook with a verified persistent cache."""

    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be a non-negative integer")
    contract = {
        "model_id": MODEL_ID,
        "source_url": SHENWAN_XLS_URL,
        "source_format": "xls",
        "minimum_records": min_records,
    }
    cache = SafeFileCache(
        Path(cache_dir) / f"{MODEL_ID}_official-xls.json.gz",
        schema_version=CACHE_SCHEMA_VERSION,
        ttl=cache_ttl_seconds,
        max_uncompressed_bytes=MAX_XLS_CACHE_BYTES,
    )
    if use_cache:
        loaded = cache.load()
        if loaded.hit:
            try:
                return _history_from_cache(loaded.value, contract)
            except ShenwanIndustryHistoryError:
                pass
    try:
        response = session.get(SHENWAN_XLS_URL, headers=_SOURCE_HEADERS, timeout=(5.0, 45.0), stream=True)
        try:
            response.raise_for_status()
            _strict_final_url(response, SHENWAN_XLS_URL)
            raw = read_bounded_response_bytes(response, MAX_XLS_RESPONSE_BYTES)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        history = parse_shenwan_xls(raw, min_records=min_records)
        if min_records >= MIN_PRODUCTION_XLS_RECORDS:
            company_count = len({record.code for record in history.records})
            l1_count = len({record.l1_code for record in history.records})
            if company_count < MIN_PRODUCTION_XLS_COMPANIES or l1_count < MIN_PRODUCTION_XLS_L1_CODES:
                raise ShenwanIndustryHistoryError("Shenwan workbook fails its market-coverage contract")
    except ShenwanIndustryHistoryError:
        raise
    except (OSError, ValueError, requests.RequestException) as exc:
        raise ShenwanIndustryHistoryError("cannot fetch official Shenwan industry workbook") from exc
    if use_cache:
        try:
            cache.save(_history_cache_payload(raw, contract))
        except SafeCacheError:
            pass
    return history


def _cninfo_enckey(now: int | None = None) -> str:
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - locked runtime dependency
        raise ShenwanIndustryHistoryError("cryptography is required for CNINFO industry history") from exc
    epoch = int(time.time()) if now is None else int(now)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(str(epoch).encode("ascii")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_CNINFO_AES_KEY), modes.CBC(_CNINFO_AES_KEY)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _parse_cninfo_response(
    raw: bytes,
    *,
    requested_code: str,
    as_of: date,
    source_url: str = CNINFO_HISTORY_URL,
) -> ShenwanIndustryHistory:
    digest = hashlib.sha256(raw).hexdigest()

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ShenwanIndustryHistoryError(f"CNINFO industry response contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ShenwanIndustryHistoryError(f"CNINFO industry response contains non-finite JSON: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShenwanIndustryHistoryError("CNINFO industry response is not UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ShenwanIndustryHistoryError("CNINFO industry response root is invalid")
    raw_records = payload.get("records")
    total = payload.get("total")
    if (
        not isinstance(raw_records, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total != len(raw_records)
    ):
        raise ShenwanIndustryHistoryError("CNINFO industry response count is inconsistent")
    if len(raw_records) > MAX_CNINFO_RECORDS_PER_CODE:
        raise ShenwanIndustryHistoryError("CNINFO industry response exceeds its row limit")
    records: list[ShenwanIndustryRecord] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            raise ShenwanIndustryHistoryError("CNINFO industry record is invalid")
        try:
            code = _normalise_code(raw_record.get("SECCODE"))
        except ShenwanIndustryHistoryError as exc:
            raise ShenwanIndustryHistoryError("CNINFO industry record has an invalid company identity") from exc
        if code != requested_code:
            raise ShenwanIndustryHistoryError("CNINFO industry response contains another company")
        standard_code = str(raw_record.get("F001V") or "").strip()
        if standard_code not in _CNINFO_STANDARDS:
            continue
        classification_standard, expected_standard_name = _CNINFO_STANDARDS[standard_code]
        if str(raw_record.get("F002V") or "").strip() != expected_standard_name:
            raise ShenwanIndustryHistoryError("CNINFO Shenwan classification-standard identity changed")
        if str(raw_record.get("F008C") or "").strip() not in {"0", "1"}:
            raise ShenwanIndustryHistoryError("CNINFO Shenwan latest-record flag is invalid")
        effective = _normalise_date(raw_record.get("VARYDATE"), field="effective date")
        if date.fromisoformat(str(effective)) > as_of:
            raise ShenwanIndustryHistoryError("CNINFO industry response exceeds the requested as-of date")
        records.append(
            _record(
                code=code,
                effective_from=effective,
                industry_code=raw_record.get("F003V"),
                classification_standard=classification_standard,
                source_name=CNINFO_SOURCE,
                source_url=source_url,
                source_sha256=digest,
            )
        )
    validated = _validate_records(records, max_records=MAX_CNINFO_RECORDS_PER_CODE)
    return ShenwanIndustryHistory(
        records=validated,
        source_name=CNINFO_SOURCE,
        source_url=source_url,
        source_sha256=digest,
    )


def _cninfo_contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "source_url": CNINFO_HISTORY_URL,
        "source_format": "json",
        "code": code,
        "as_of": as_of.isoformat(),
        "classification_standard_codes": sorted(_CNINFO_STANDARDS),
    }


def _cninfo_cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{MODEL_ID}_cninfo_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def fetch_cninfo_industry_history_batch(
    codes: Iterable[str],
    as_of: date | str,
    *,
    session: Any = requests,
    cache_dir: str | Path = SHENWAN_CACHE_DIR,
    use_cache: bool = True,
    request_limit: int = MAX_CNINFO_REQUESTS_PER_BATCH,
    enckey_factory: Callable[[], str] = _cninfo_enckey,
    rate_limiter: RequestRateLimiter | None = None,
) -> ShenwanIndustryBatch:
    """Fetch bounded per-code CNINFO fallbacks without hiding partial failures."""

    try:
        cutoff = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date") from exc
    if not isinstance(cutoff, date):
        raise TypeError("as_of must be a date or ISO date string")
    if cutoff > shanghai_today():
        raise ValueError("as_of cannot be in the future")
    if (
        isinstance(request_limit, bool)
        or not isinstance(request_limit, int)
        or not 0 <= request_limit <= MAX_CNINFO_REQUESTS_PER_BATCH
    ):
        raise ValueError(f"request_limit must be between 0 and {MAX_CNINFO_REQUESTS_PER_BATCH}")
    normalized_input = tuple(_normalise_code(code) for code in codes)
    if len(normalized_input) != len(set(normalized_input)):
        raise ShenwanIndustryHistoryError("CNINFO industry batch contains duplicate company codes")
    normalized_codes = tuple(sorted(normalized_input))
    cache_root = Path(cache_dir)
    histories: dict[str, ShenwanIndustryHistory] = {}
    failures: dict[str, str] = {}
    unresolved: list[str] = []
    for code in normalized_codes:
        contract = _cninfo_contract(code, cutoff)
        cache = SafeFileCache(
            _cninfo_cache_path(code, cutoff, cache_root),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CNINFO_CACHE_TTL_SECONDS,
            max_uncompressed_bytes=MAX_CNINFO_CACHE_BYTES,
        )
        if use_cache:
            loaded = cache.load()
            if loaded.hit:
                try:
                    histories[code] = _history_from_cache(loaded.value, contract)
                    continue
                except ShenwanIndustryHistoryError:
                    pass
        unresolved.append(code)
    if len(unresolved) > request_limit:
        raise ShenwanIndustryHistoryError(
            f"CNINFO fallback requires {len(unresolved)} requests, exceeding the hard limit {request_limit}"
        )
    limiter = rate_limiter or RequestRateLimiter(0.05)
    request_count = 0
    for code in unresolved:
        request_count += 1
        limiter.acquire()
        contract = _cninfo_contract(code, cutoff)
        try:
            response = session.get(
                CNINFO_HISTORY_URL,
                params={"scode": code, "sdate": "", "edate": cutoff.isoformat()},
                headers={**_CNINFO_HEADERS, "Accept-Enckey": enckey_factory()},
                timeout=(5.0, 20.0),
                stream=True,
            )
            try:
                response.raise_for_status()
                _strict_final_url(response, CNINFO_HISTORY_URL)
                raw = read_bounded_response_bytes(response, MAX_CNINFO_RESPONSE_BYTES)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            history = _parse_cninfo_response(raw, requested_code=code, as_of=cutoff)
            histories[code] = history
            if use_cache:
                cache = SafeFileCache(
                    _cninfo_cache_path(code, cutoff, cache_root),
                    schema_version=CACHE_SCHEMA_VERSION,
                    ttl=CNINFO_CACHE_TTL_SECONDS,
                    max_uncompressed_bytes=MAX_CNINFO_CACHE_BYTES,
                )
                try:
                    cache.save(
                        _history_cache_payload(raw, contract),
                        ttl=(CNINFO_CACHE_TTL_SECONDS if history.records else CNINFO_EMPTY_CACHE_TTL_SECONDS),
                    )
                except SafeCacheError:
                    pass
        except (OSError, ValueError, requests.RequestException, ShenwanIndustryHistoryError) as exc:
            failures[code] = exc.__class__.__name__
    return ShenwanIndustryBatch(
        histories=dict(sorted(histories.items())),
        failures=dict(sorted(failures.items())),
        request_count=request_count,
    )


def shenwan_industry_as_of(
    records: Sequence[ShenwanIndustryRecord],
    code: str,
    as_of: date | str,
) -> ShenwanIndustryRecord | None:
    """Return the last classification effective on or before ``as_of``."""

    normalized_code = _normalise_code(code)
    cutoff = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if not isinstance(cutoff, date):
        raise TypeError("as_of must be a date or ISO date string")
    eligible = [
        record
        for record in records
        if record.code == normalized_code and date.fromisoformat(record.effective_from) <= cutoff
    ]
    if not eligible:
        return None
    # Prefer the current CNINFO standard over the legacy standard only when two
    # records share an effective date.  Ordinary chronological history remains
    # untouched.
    return max(
        eligible,
        key=lambda record: (
            record.effective_from,
            record.classification_standard == "shenwan_current",
            record.classification_standard == "shenwan_official_workbook",
        ),
    )


def resolve_shenwan_industry_history(
    codes: Iterable[str],
    as_of: date | str,
    *,
    xls_loader: Callable[[], ShenwanIndustryHistory] = fetch_shenwan_industry_history,
    cninfo_loader: Callable[[Iterable[str], date | str], ShenwanIndustryBatch] = fetch_cninfo_industry_history_batch,
) -> ShenwanIndustryResolution:
    """Resolve requested histories, using CNINFO only for exact XLS gaps."""

    normalized_input = tuple(_normalise_code(code) for code in codes)
    if len(normalized_input) != len(set(normalized_input)):
        raise ShenwanIndustryHistoryError("industry-history resolution contains duplicate company codes")
    requested = tuple(sorted(normalized_input))
    try:
        cutoff = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date") from exc
    if not isinstance(cutoff, date):
        raise TypeError("as_of must be a date or ISO date string")
    if cutoff > shanghai_today():
        raise ValueError("as_of cannot be in the future")
    source_errors: dict[str, str] = {}
    primary_available = False
    records: list[ShenwanIndustryRecord] = []
    try:
        primary = xls_loader()
        records.extend(primary.records)
        primary_available = True
    except (OSError, requests.RequestException, ShenwanIndustryHistoryError) as exc:
        source_errors[SHENWAN_XLS_SOURCE] = exc.__class__.__name__
    available_codes = {record.code for record in records if date.fromisoformat(record.effective_from) <= cutoff}
    fallback_codes = tuple(code for code in requested if code not in available_codes)
    if fallback_codes:
        fallback = cninfo_loader(fallback_codes, cutoff)
        for history in fallback.histories.values():
            records.extend(history.records)
        source_errors.update({f"{CNINFO_SOURCE}:{code}": reason for code, reason in fallback.failures.items()})
    resolved_codes = {record.code for record in records if date.fromisoformat(record.effective_from) <= cutoff}
    unresolved = tuple(code for code in requested if code not in resolved_codes)
    selected_records = [record for record in records if record.code in requested]
    validated = _validate_records(selected_records, max_records=MAX_XLS_RECORDS)
    return ShenwanIndustryResolution(
        records=validated,
        requested_codes=requested,
        primary_source_available=primary_available,
        fallback_codes=fallback_codes,
        unresolved_codes=unresolved,
        source_errors=dict(sorted(source_errors.items())),
    )


def audit_shenwan_industry_drift(
    records: Sequence[ShenwanIndustryRecord],
    codes: Iterable[str],
    *,
    from_as_of: date | str,
    to_as_of: date | str,
) -> tuple[dict[str, Any], ...]:
    """Compare two point-in-time Shenwan classifications without model remapping."""

    start = date.fromisoformat(from_as_of) if isinstance(from_as_of, str) else from_as_of
    end = date.fromisoformat(to_as_of) if isinstance(to_as_of, str) else to_as_of
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("industry drift audit dates are invalid")
    rows: list[dict[str, Any]] = []
    for code in sorted({_normalise_code(item) for item in codes}):
        before = shenwan_industry_as_of(records, code, start)
        after = shenwan_industry_as_of(records, code, end)
        if before is None:
            status = "missing_from"
        elif after is None:
            status = "missing_to"
        elif before.industry_code == after.industry_code:
            status = "unchanged"
        else:
            status = "changed"
        rows.append(
            {
                "code": code,
                "from_as_of": start.isoformat(),
                "to_as_of": end.isoformat(),
                "status": status,
                "from_industry_code": before.industry_code if before else None,
                "to_industry_code": after.industry_code if after else None,
                "from_l1_code": before.l1_code if before else None,
                "to_l1_code": after.l1_code if after else None,
                "from_effective_from": before.effective_from if before else None,
                "to_effective_from": after.effective_from if after else None,
            }
        )
    return tuple(rows)


__all__ = [
    "CNINFO_HISTORY_URL",
    "MAX_CNINFO_REQUESTS_PER_BATCH",
    "MODEL_ID",
    "SHENWAN_XLS_URL",
    "ShenwanIndustryBatch",
    "ShenwanIndustryHistory",
    "ShenwanIndustryHistoryError",
    "ShenwanIndustryRecord",
    "ShenwanIndustryResolution",
    "audit_shenwan_industry_drift",
    "fetch_cninfo_industry_history_batch",
    "fetch_shenwan_industry_history",
    "parse_shenwan_xls",
    "resolve_shenwan_industry_history",
    "shenwan_industry_as_of",
]
