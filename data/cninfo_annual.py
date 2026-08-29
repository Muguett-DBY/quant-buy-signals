"""CNINFO annual-report acquisition cash-flow evidence.

Eastmoney's annual cash-flow report omits the line item "取得子公司及其他
营业单位支付的现金净额", so growth-sustainability evidence (Type 3 3d,
Type 7 7c) is unavailable for most companies.  CNINFO publishes the full
annual report PDF for every A-share company; this module locates the
annual report, extracts that single line item from the cash-flow
statement, and caches the result.

Fail-closed contract: only an explicitly parsed numeric value (including a
dash meaning "no occurrence this year") is treated as evidence.  A missing
line, unreadable table layout or download failure yields ``available=False``
with a reason; values are never guessed.
"""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS
from data.as_of import shanghai_today
from data.cache import SafeFileCache
from data.provider_http import (
    is_transient_request_error,
    read_bounded_response_bytes,
    retry_delay_seconds,
    thread_local_session,
)

MODEL_ID = "cninfo-annual-acquisition-v2"
CACHE_SCHEMA_VERSION = 2
MAX_PDF_BYTES = 60 * 1024 * 1024
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_TEXT_WORDS = 2_000_000
REQUEST_ATTEMPTS = 3

CNINFO_TOP_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCE_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_PREFIX = "https://static.cninfo.com.cn/"
CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.cninfo.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
}
CNINFO_CACHE_DIR = CACHE_DIRECTORY / "cninfo_annual"

_A_SHARE_CODE = re.compile(r"^[036][0-9]{5}$")
_NUMERIC_TOKEN = re.compile(r"^\(?-?[0-9][0-9,]*(\.[0-9]+)?\)?$")
_ACQUISITION_LABEL = re.compile(r"(?:取得|购买|收购)子公司|acquisition.{0,40}subsidiari|acquisition.{0,40}business")
_EXPLICIT_ZERO_TOKENS = frozenset({"-", "–", "—", "―", "−", "－", "﹣"})
_UNIT_PATTERNS = [
    re.compile(r"单位[：:]?\s*[^。；;]{0,14}?(?:人民币)?(?P<unit>百万元|千元|万元|亿元)"),
    re.compile(r"金额单位均为(?:人民币)?(?P<unit>百万元|千元|万元|亿元)"),
    re.compile(r"以(?:人民币)?(?P<unit>百万元|千元|万元|亿元)列示"),
    re.compile(r"(?:金额)?单位[：:]?\s*(?P<unit>百万元|千元|万元|亿元)"),
    re.compile(r"in\s+(?:millions\s+of\s+)?RMB|in\s+RMB\s+(?P<unit>thousands)", re.I),
]
_ORG_ID_URL_SAFE = re.compile(r"[^0-9A-Za-z]+")


@dataclass(frozen=True)
class AnnualAcquisitionEvidence:
    code: str
    year: int
    available: bool
    acquisition_cashflow: float | None
    unit: str
    source_url: str
    source_sha256: str
    reason: str


class CninfoAnnualError(Exception):
    """The annual-report capture, cache, or parse contract failed."""


def _unit_for_page(page_units: Mapping[int, str], page_index: int) -> str:
    if page_index in page_units:
        return page_units[page_index]
    for previous in range(page_index - 1, max(-1, page_index - 6), -1):
        if previous in page_units:
            return page_units[previous]
    return "元"


def _parse_pdf_number(word: str) -> float:
    value = word.replace(",", "")
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
    parsed = float(value)
    return -parsed if negative else parsed


def _validate_source_url(url: str, *, expected_host: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host or parsed.username or parsed.password:
        raise CninfoAnnualError("CNINFO response redirected outside the trusted HTTPS origin")
    return url


def _read_bounded_response(response: Any, *, maximum_bytes: int) -> bytes:
    try:
        content = read_bounded_response_bytes(response, maximum_bytes)
    except ValueError as exc:
        raise CninfoAnnualError("CNINFO response exceeds the size contract") from exc
    if not content:
        raise CninfoAnnualError("CNINFO response returned empty content")
    return content


def _request_json(
    url: str,
    data: Mapping[str, Any] | None,
    *,
    session: Any = None,
    timeout: tuple[int, int] = (15, 30),
):
    client = session or thread_local_session()
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        try:
            response = client.post(url, data=data, headers=CNINFO_HEADERS, timeout=timeout, stream=True)
            response.raise_for_status()
            final_url = str(getattr(response, "url", None) or url)
            _validate_source_url(final_url, expected_host="www.cninfo.com.cn")
            payload = json.loads(_read_bounded_response(response, maximum_bytes=MAX_JSON_BYTES))
            break
        except (CninfoAnnualError, json.JSONDecodeError, OSError, ValueError) as exc:
            last_error = exc
            should_retry = isinstance(exc, OSError) or is_transient_request_error(exc, response)
        except Exception as exc:  # requests exceptions remain optional at import time
            last_error = exc
            should_retry = is_transient_request_error(exc, response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not should_retry or attempt + 1 >= REQUEST_ATTEMPTS:
            raise CninfoAnnualError(f"CNINFO request failed: {type(last_error).__name__}") from last_error
        import time

        time.sleep(retry_delay_seconds(response, attempt=attempt, base_seconds=2.0))
    else:  # pragma: no cover - the loop either breaks or raises
        raise CninfoAnnualError("CNINFO request failed")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping) and payload.get("announcements") is not None:
        return payload
    raise CninfoAnnualError("CNINFO response is not a JSON list or announcement payload")


def _resolve_org_id(code: str, *, session: Any = None) -> str:
    results = _request_json(CNINFO_TOP_SEARCH_URL, {"keyWord": code, "maxNum": 10}, session=session)
    for entry in results:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("code") or "") == code:
            org_id = str(entry.get("orgId") or "")
            if org_id:
                return org_id
    raise CninfoAnnualError(f"CNINFO org id not found for {code}")


def _find_annual_report_pdf(code: str, org_id: str, year: int, *, session: Any = None) -> str:
    """Return the adjunct URL of the annual report whose cover year is ``year``."""

    start = f"{year}-01-01"
    end = f"{year + 1}-12-31"
    results = _request_json(
        CNINFO_ANNOUNCE_URL,
        {
            "pageNum": 1,
            "pageSize": 30,
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": "category_ndbg_szsh",
            "trade": "",
            "seDate": f"{start}~{end}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
        session=session,
    )
    announcements = results.get("announcements") or []
    for announcement in announcements:
        if not isinstance(announcement, Mapping):
            continue
        title = str(announcement.get("announcementTitle") or "")
        adjunct = str(announcement.get("adjunctUrl") or "")
        if f"{year}年" in title and "摘要" not in title and adjunct.upper().endswith(".PDF"):
            return adjunct
    raise CninfoAnnualError(f"CNINFO annual report {year} not found for {code}")


def _download_pdf(adjunct_url: str, *, session: Any = None) -> tuple[bytes, str]:
    url = urljoin(CNINFO_PDF_PREFIX, adjunct_url.lstrip("/"))
    _validate_source_url(url, expected_host="static.cninfo.com.cn")
    client = session or thread_local_session()
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        try:
            response = client.get(url, headers=CNINFO_HEADERS, timeout=(30, 120), stream=True)
            response.raise_for_status()
            final_url = _validate_source_url(
                str(getattr(response, "url", None) or url), expected_host="static.cninfo.com.cn"
            )
            return _read_bounded_response(response, maximum_bytes=MAX_PDF_BYTES), final_url
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            should_retry = is_transient_request_error(exc, response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not should_retry or attempt + 1 >= REQUEST_ATTEMPTS:
            break
        import time

        time.sleep(retry_delay_seconds(response, attempt=attempt, base_seconds=3.0))
    raise CninfoAnnualError(f"CNINFO PDF download failed: {type(last_error).__name__}") from last_error


def _detect_unit(page_text: str) -> str:
    for pattern in _UNIT_PATTERNS:
        match = pattern.search(page_text)
        if match:
            unit = match.groupdict().get("unit")
            if unit:
                return unit
            # "in millions of RMB" matched without an explicit group.
            if "millions" in page_text[match.start() : match.end()].lower():
                return "百万元"
            return "元"
    return ""


def _import_fitz():
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # noqa: BLE001
        raise CninfoAnnualError(f"PyMuPDF is unavailable: {type(exc).__name__}") from exc
    return fitz


def _parse_acquisition_cashflow(pdf_bytes: bytes, code: str, year: int) -> tuple[float | None, str, str | None]:
    """Extract the acquisition line from the cash-flow statement.

    Returns ``(value, unit, reason)``.  ``unit`` is the reported monetary
    unit (元/千元/万元/百万元/亿元) or "" when unknown; ``reason`` is None on
    success and explains unavailable outcomes otherwise.
    """

    try:
        fitz = _import_fitz()
    except CninfoAnnualError as exc:
        raise exc
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise CninfoAnnualError(f"CNINFO PDF cannot be opened: {type(exc).__name__}") from exc
    try:
        if document.page_count < 1:
            raise CninfoAnnualError("CNINFO PDF has no pages")
        page_units: dict[int, str] = {}
        for page_index, page in enumerate(document):
            words = page.get_text("words")
            if not words:
                continue
            page_text = "".join(word[4] for word in words)
            if page_index not in page_units:
                detected = _detect_unit(page_text)
                if detected:
                    page_units[page_index] = detected
            # Merge words into lines by y-band.
            lines: list[tuple[float, float, str]] = []
            for _x0, y0, _x1, y1, word, *_rest in words:
                if not word.strip():
                    continue
                placed = False
                for index, (line_y0, line_y1, line_text) in enumerate(lines):
                    if y0 < line_y1 and y1 > line_y0:
                        lines[index] = (min(line_y0, y0), max(line_y1, y1), line_text + word)
                        placed = True
                        break
                if not placed:
                    lines.append((y0, y1, word))
            for y0, y1, line_text in lines:
                if not _ACQUISITION_LABEL.search(line_text):
                    continue
                if (
                    "支付" not in line_text
                    and "净额" not in line_text
                    and "payment" not in line_text.lower()
                    and "paid" not in line_text.lower()
                    and "acquisition" not in line_text.lower()
                ):
                    continue
                # Locate the label token extent, then collect numeric tokens
                # on the same line band to its right (本期发生额 column).
                label_end = 0.0
                for _x0, _wy0, x1, _wy1, word, *_rest in words:
                    if _wy0 < y1 and _wy1 > y0 and _ACQUISITION_LABEL.search(word):
                        label_end = max(label_end, x1)
                if label_end <= 0:
                    label_end = 0.0
                candidates: list[float] = []
                explicit_zero = False
                for x0, _wy0, _x1, _wy1, word, *_rest in words:
                    if not (_wy0 < y1 and _wy1 > y0 and x0 >= label_end - 2):
                        continue
                    token = word.strip()
                    if _NUMERIC_TOKEN.match(token):
                        candidates.append(_parse_pdf_number(token))
                    elif token in _EXPLICIT_ZERO_TOKENS:
                        explicit_zero = True
                if candidates:
                    return candidates[0], _unit_for_page(page_units, page_index), None
                if explicit_zero:
                    return 0.0, _unit_for_page(page_units, page_index), None
                return None, "", "acquisition_value_not_found"
        return None, "", "acquisition_line_not_found"
    finally:
        document.close()


def _cache_contract(code: str, year: int) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "code": code,
        "year": year,
        "source": CNINFO_ANNOUNCE_URL,
        "schema_version": CACHE_SCHEMA_VERSION,
    }


def _cache_path(code: str, year: int, cache_dir: Path) -> Path:
    return cache_dir / f"{MODEL_ID}_{code}_{year}.json.gz"


def fetch_annual_acquisition(
    code: str,
    year: int,
    *,
    session: Any = None,
    cache_dir: str | Path = CNINFO_CACHE_DIR,
    use_cache: bool = True,
) -> AnnualAcquisitionEvidence:
    """Fetch and verify the annual acquisition cash-flow line from CNINFO."""

    if isinstance(code, bool) or not isinstance(code, str) or not _A_SHARE_CODE.fullmatch(code):
        raise ValueError(f"invalid security code: {code!r}")
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= shanghai_today().year:
        raise ValueError(f"invalid report year: {year!r}")
    path = Path(cache_dir)
    if use_cache:
        cache = SafeFileCache(
            _cache_path(code, year, path),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=CACHE_TTL_SECONDS,
            max_uncompressed_bytes=1_000_000,
        )
        loaded = cache.load(allow_expired=True)
        if loaded.hit:
            try:
                payload = loaded.value
                if not isinstance(payload, Mapping) or set(payload) != {"contract", "evidence"}:
                    raise CninfoAnnualError("CNINFO cache payload shape is invalid")
                if payload.get("contract") != _cache_contract(code, year):
                    raise CninfoAnnualError("CNINFO cache contract mismatch")
                return AnnualAcquisitionEvidence(**payload["evidence"])
            except (CninfoAnnualError, TypeError, ValueError):
                pass
    try:
        org_id = _resolve_org_id(code, session=session)
        adjunct = _find_annual_report_pdf(code, org_id, year, session=session)
        pdf_bytes, source_url = _download_pdf(adjunct, session=session)
        value, unit, reason = _parse_acquisition_cashflow(pdf_bytes, code, year)
    except CninfoAnnualError as exc:
        evidence = AnnualAcquisitionEvidence(
            code=code,
            year=year,
            available=False,
            acquisition_cashflow=None,
            unit="",
            source_url="",
            source_sha256="",
            reason=str(exc),
        )
    else:
        evidence = AnnualAcquisitionEvidence(
            code=code,
            year=year,
            available=reason is None,
            acquisition_cashflow=value,
            unit=unit,
            source_url=source_url,
            source_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            reason=reason or "",
        )
    if use_cache:
        try:
            cache = SafeFileCache(
                _cache_path(code, year, path),
                schema_version=CACHE_SCHEMA_VERSION,
                ttl=CACHE_TTL_SECONDS,
                max_uncompressed_bytes=1_000_000,
            )
            cache.save(
                {
                    "contract": _cache_contract(code, year),
                    "evidence": {
                        "code": evidence.code,
                        "year": evidence.year,
                        "available": evidence.available,
                        "acquisition_cashflow": evidence.acquisition_cashflow,
                        "unit": evidence.unit,
                        "source_url": evidence.source_url,
                        "source_sha256": evidence.source_sha256,
                        "reason": evidence.reason,
                    },
                }
            )
        except Exception:  # noqa: BLE001
            pass
    return evidence


__all__ = [
    "CNINFO_CACHE_DIR",
    "MODEL_ID",
    "AnnualAcquisitionEvidence",
    "CninfoAnnualError",
    "fetch_annual_acquisition",
]
