"""Official exchange-structured financial evidence and gap-only overlays.

Eastmoney remains the bulk primary and Sina remains the first narrow fallback.
This module uses the Shanghai Stock Exchange XBRL summary and Shenzhen Stock
Exchange periodic indicator workbooks only for fields that are still absent.
It never replaces a finite primary value.  Shenzhen's per-share operating cash
flow is retained as source evidence, but is deliberately not guessed into a
total cash-flow amount.
"""

from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
import requests

from data.as_of import shanghai_today
from data.cache import SafeCacheError, SafeFileCache
from data.provider_http import (
    RequestRateLimiter,
    is_transient_request_error,
    read_bounded_response_bytes,
    retry_delay_seconds,
)


EXCHANGE_FINANCIAL_ADAPTER_VERSION = 1
SSE_XBRL_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_REFERER = "https://www.sse.com.cn/disclosure/listedinfo/listedcompanies/"
SZSE_TOPIC_URL = "https://investor.szse.cn/market/subject/index.html"
SZSE_REFERER = "https://investor.szse.cn/market/subject/"
EXCHANGE_FINANCIAL_MAX_SSE_CODES = 32
EXCHANGE_FINANCIAL_MAX_SZSE_CODES = 448
EXCHANGE_FINANCIAL_MAX_REQUESTS = 72

_CACHE_ROOT = Path(__file__).resolve().parent / "cache" / "exchange_financials"
_SSE_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
_SZSE_INDEX_MAX_BYTES = 2 * 1024 * 1024
_SZSE_DOCX_MAX_BYTES = 12 * 1024 * 1024
_DOCX_XML_MAX_BYTES = 8 * 1024 * 1024
_DOCX_UNCOMPRESSED_MAX_BYTES = 24 * 1024 * 1024
_DOCX_MAX_MEMBERS = 160
_MAX_SSE_ROWS = 16
_MAX_SZSE_ROWS = 5_000
_CACHE_TTL_SECONDS = 12 * 60 * 60
_CACHE_MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
_IMMUTABLE_DOC_TTL_SECONDS = 30 * 24 * 60 * 60
_SSE_REPORT_PERIODS = {"5000": "12-31", "1000": "06-30"}
_SZSE_TITLE = re.compile(
    r"^深市(?P<board>主板|创业板)上市公司(?P<year>20\d{2})年"
    r"(?P<period>年报|中期|一季报|三季报)主要财务指标$"
)
_SZSE_PERIOD_END = {"年报": "12-31", "中期": "06-30", "一季报": "03-31", "三季报": "09-30"}
_SZSE_LINK = re.compile(
    r"var\s+curHref\s*=\s*['\"](?P<href>\./P\d+\.docx)['\"];"
    r"[\s\S]{0,360}?var\s+curTitle\s*=\s*['\"](?P<title>[^'\"]+)['\"];"
)
_SZSE_HEADERS = (
    "股票代码",
    "股票简称",
    "净利润（万元）",
    "每股收益（元）",
    "每股经营性现金流量（元）",
    "分配预案",
)
_W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


class ExchangeFinancialError(RuntimeError):
    """The official exchange source could not satisfy its strict contract."""


@dataclass(frozen=True)
class ExchangeFinancialOutcome:
    financials: dict[str, dict[str, Any]]
    diagnostic: dict[str, Any]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _strict_json(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ExchangeFinancialError(f"exchange JSON contains duplicate key: {key}")
            output[key] = value
        return output

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ExchangeFinancialError(f"exchange JSON contains non-finite constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExchangeFinancialError("exchange response is not strict UTF-8 JSON") from exc


def _finite(value: Any) -> float | None:
    if value in (None, "", "-") or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _code(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{6}", text) else ""


def _safe_cache_key(namespace: str, identity: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    return f"{namespace}-{digest}.json.gz"


def _validate_final_url(final_url: str, expected_url: str) -> None:
    final = urlsplit(final_url)
    expected = urlsplit(expected_url)
    try:
        port = final.port
    except ValueError as exc:
        raise ExchangeFinancialError("exchange request returned an invalid final URL") from exc
    if (
        final.scheme != "https"
        or final.hostname != expected.hostname
        or final.path != expected.path
        or final.username is not None
        or final.password is not None
        or port not in (None, 443)
        or final.fragment
    ):
        raise ExchangeFinancialError("exchange request redirected outside its fixed HTTPS endpoint")


def _report_contract(contract: Mapping[str, Any]) -> dict[str, str]:
    required = ("annual_report_date", "current_interim_report_date", "prior_interim_report_date")
    normalized: dict[str, str] = {}
    for key in required:
        value = str(contract.get(key) or "").strip()
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"exchange financial contract has invalid {key}") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"exchange financial contract has invalid {key}")
        normalized[key] = value
    if not normalized["annual_report_date"].endswith("-12-31"):
        raise ValueError("exchange financial annual period must end on 12-31")
    current = normalized["current_interim_report_date"]
    prior = normalized["prior_interim_report_date"]
    if current[5:] != prior[5:] or int(current[:4]) != int(prior[:4]) + 1:
        raise ValueError("exchange financial interim periods are not comparable")
    return normalized


class ExchangeFinancialClient:
    """Bounded client for SSE XBRL JSON and SZSE official DOCX tables."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        cache_dir: Path | str = _CACHE_ROOT,
        timeout: float = 20.0,
        retries: int = 3,
        request_limit: int = EXCHANGE_FINANCIAL_MAX_REQUESTS,
        request_interval: float = 0.15,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        use_cache: bool = True,
    ) -> None:
        if timeout <= 0 or retries < 1 or request_limit < 1 or not isinstance(use_cache, bool):
            raise ValueError("exchange client limits must be positive")
        self._session = session or requests.Session()
        self._cache_dir = Path(cache_dir)
        self._timeout = float(timeout)
        self._retries = int(retries)
        self._request_limit = int(request_limit)
        self._limiter = RequestRateLimiter(request_interval)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._use_cache = use_cache
        self._network_requests = 0
        self._cache_hits = 0

    def diagnostic(self) -> dict[str, int]:
        return {
            "network_requests": self._network_requests,
            "cache_hits": self._cache_hits,
            "request_limit": self._request_limit,
        }

    def _load_cache(
        self,
        namespace: str,
        identity: Mapping[str, Any],
        *,
        max_age_seconds: int,
    ) -> tuple[bytes, str] | None:
        path = self._cache_dir / _safe_cache_key(namespace, identity)
        try:
            loaded = SafeFileCache(
                path,
                schema_version=1,
                ttl=max_age_seconds,
                max_uncompressed_bytes=_CACHE_MAX_UNCOMPRESSED_BYTES,
            ).load()
            if not loaded.hit:
                return None
            payload = loaded.value
            if not isinstance(payload, Mapping):
                return None
            if payload.get("schema_version") != 1 or payload.get("identity") != dict(identity):
                return None
            fetched_at = datetime.fromisoformat(str(payload.get("fetched_at") or ""))
            if fetched_at.tzinfo is None:
                return None
            age = (self._now().astimezone(timezone.utc) - fetched_at.astimezone(timezone.utc)).total_seconds()
            if age < 0 or age > max_age_seconds:
                return None
            raw = base64.b64decode(str(payload.get("raw_base64") or ""), validate=True)
            if hashlib.sha256(raw).hexdigest() != payload.get("raw_sha256"):
                return None
            final_url = str(payload.get("final_url") or "")
            if not final_url:
                return None
        except (OSError, ValueError, TypeError, base64.binascii.Error, ExchangeFinancialError, SafeCacheError):
            return None
        self._cache_hits += 1
        return raw, final_url

    def _save_cache(
        self,
        namespace: str,
        identity: Mapping[str, Any],
        *,
        raw: bytes,
        final_url: str,
    ) -> None:
        payload = {
            "schema_version": 1,
            "identity": dict(identity),
            "fetched_at": self._now().astimezone(timezone.utc).isoformat(),
            "final_url": final_url,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_base64": base64.b64encode(raw).decode("ascii"),
        }
        try:
            SafeFileCache(
                self._cache_dir / _safe_cache_key(namespace, identity),
                schema_version=1,
                ttl=_IMMUTABLE_DOC_TTL_SECONDS,
                max_uncompressed_bytes=_CACHE_MAX_UNCOMPRESSED_BYTES,
            ).save(payload)
        except SafeCacheError:
            pass

    def _network_get(self, url: str, *, params: Mapping[str, Any] | None, max_bytes: int) -> tuple[bytes, str]:
        last_error: BaseException | None = None
        for attempt in range(self._retries):
            if self._network_requests >= self._request_limit:
                raise ExchangeFinancialError("exchange request hard limit exhausted")
            self._limiter.acquire()
            self._network_requests += 1
            response = None
            try:
                referer = SSE_REFERER if urlsplit(url).hostname == "query.sse.com.cn" else SZSE_REFERER
                response = self._session.get(
                    url,
                    params=dict(params or {}),
                    headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
                    timeout=self._timeout,
                    stream=True,
                )
                response.raise_for_status()
                final_url = str(response.url)
                _validate_final_url(final_url, url)
                return read_bounded_response_bytes(response, max_bytes), final_url
            except (requests.RequestException, ValueError, ExchangeFinancialError) as exc:
                last_error = exc
                if attempt + 1 >= self._retries or not is_transient_request_error(exc, response):
                    break
                self._sleep(
                    retry_delay_seconds(
                        response,
                        attempt=attempt,
                        base_seconds=0.5,
                        maximum_seconds=5.0,
                    )
                )
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
        raise ExchangeFinancialError(f"exchange request failed: {type(last_error).__name__}") from last_error

    def _get(
        self,
        namespace: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        max_bytes: int,
        max_age_seconds: int = _CACHE_TTL_SECONDS,
        validator: Callable[[bytes, str], bool] | None = None,
    ) -> tuple[bytes, str]:
        identity = {"url": url, "params": dict(sorted((params or {}).items()))}
        cached = self._load_cache(namespace, identity, max_age_seconds=max_age_seconds) if self._use_cache else None
        if cached is not None:
            raw, final_url = cached
            try:
                _validate_final_url(final_url, url)
                reusable = validator(raw, final_url) if validator is not None else True
            except ExchangeFinancialError:
                cached = None
            else:
                if reusable:
                    return raw, final_url
        raw, final_url = self._network_get(url, params=params, max_bytes=max_bytes)
        cacheable = validator(raw, final_url) if validator is not None else True
        if self._use_cache and cacheable:
            self._save_cache(namespace, identity, raw=raw, final_url=final_url)
        return raw, final_url

    def fetch_sse(self, code: str, years: Collection[int]) -> list[dict[str, Any]]:
        canonical = _code(code)
        normalized_years = sorted({int(year) for year in years}, reverse=True)
        if not canonical.startswith("6") or not normalized_years or len(normalized_years) > 4:
            raise ValueError("SSE XBRL query requires one Shanghai code and one to four years")
        output: list[dict[str, Any]] = []
        identities: set[str] = set()
        # The endpoint accepts a comma-delimited value but currently returns
        # only the first year.  Query each year separately so completeness is
        # explicit and reflected in the hard request budget.
        for requested_year in normalized_years:
            params = {
                "sqlId": "COMMON_SSE_PL_XBRL_YJGL_XQ",
                "reportYear": str(requested_year),
                "stockId": canonical,
                "isPagination": "false",
            }
            raw, final_url = self._get(
                "sse-xbrl",
                SSE_XBRL_URL,
                params=params,
                max_bytes=_SSE_RESPONSE_MAX_BYTES,
                max_age_seconds=(
                    _IMMUTABLE_DOC_TTL_SECONDS if requested_year < shanghai_today().year else _CACHE_TTL_SECONDS
                ),
                validator=lambda raw_value, url_value, year=requested_year: bool(
                    _parse_sse_response(raw_value, url_value, canonical, year)
                ),
            )
            for record in _parse_sse_response(raw, final_url, canonical, requested_year):
                report_date = record["report_date"]
                if report_date in identities:
                    raise ExchangeFinancialError("SSE XBRL contains a duplicate code/period identity")
                identities.add(report_date)
                output.append(record)
        return sorted(output, key=lambda item: item["report_date"])

    def fetch_szse(
        self,
        report_dates: Collection[str],
        *,
        as_of: date,
        requested_codes: Collection[str],
    ) -> list[dict[str, Any]]:
        desired = {str(value) for value in report_dates}
        codes = {_code(value) for value in requested_codes}
        codes.discard("")
        if not desired or not codes:
            return []
        index_raw, _index_url = self._get(
            "szse-topic-index",
            SZSE_TOPIC_URL,
            max_bytes=_SZSE_INDEX_MAX_BYTES,
            validator=lambda raw_value, _url: bool(_discover_szse_documents(raw_value)),
        )
        documents = {
            identity: document
            for identity, document in _discover_szse_documents(index_raw).items()
            if identity[1] in desired
        }

        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for (board, report_date), (title, source_url) in sorted(documents.items()):
            raw, final_url = self._get(
                "szse-periodic-docx",
                source_url,
                max_bytes=_SZSE_DOCX_MAX_BYTES,
                max_age_seconds=_IMMUTABLE_DOC_TTL_SECONDS,
                validator=lambda raw_value, url_value, title_value=title, board_value=board, date_value=report_date: (
                    _parse_szse_docx(
                        raw_value,
                        source_url=url_value,
                        expected_title=title_value,
                        expected_board=board_value,
                        expected_report_date=date_value,
                        as_of=as_of,
                        requested_codes=codes,
                    )
                    is not None
                ),
            )
            records = _parse_szse_docx(
                raw,
                source_url=final_url,
                expected_title=title,
                expected_board=board,
                expected_report_date=report_date,
                as_of=as_of,
                requested_codes=codes,
            )
            for record in records:
                identity = (record["security_code"], record["report_date"])
                if identity in seen:
                    raise ExchangeFinancialError("SZSE documents contain a duplicate code/period identity")
                seen.add(identity)
                output.append(record)
        return sorted(output, key=lambda item: (item["security_code"], item["report_date"]))


def _parse_sse_response(
    raw: bytes,
    final_url: str,
    code: str,
    requested_year: int,
) -> list[dict[str, Any]]:
    payload = _strict_json(raw)
    if not isinstance(payload, Mapping):
        raise ExchangeFinancialError("SSE XBRL response is not an object")
    result = payload.get("result")
    page_help = payload.get("pageHelp")
    page_data = page_help.get("data") if isinstance(page_help, Mapping) else None
    if not isinstance(result, list) or not isinstance(page_data, list) or result != page_data:
        raise ExchangeFinancialError("SSE XBRL result/page data are incomplete or inconsistent")
    if len(result) > _MAX_SSE_ROWS:
        raise ExchangeFinancialError("SSE XBRL response exceeds its row limit")
    if isinstance(page_help, Mapping) and page_help.get("total") != len(result):
        raise ExchangeFinancialError("SSE XBRL total differs from returned rows")
    if str(payload.get("securityCode") or code).strip() != code:
        raise ExchangeFinancialError("SSE XBRL top-level security identity differs from request")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    output: list[dict[str, Any]] = []
    identities: set[str] = set()
    for row in result:
        if not isinstance(row, Mapping):
            raise ExchangeFinancialError("SSE XBRL contains a non-object row")
        if str(row.get("STOCK_ID") or "").strip() != code:
            raise ExchangeFinancialError("SSE XBRL row security identity differs from request")
        period_id = str(row.get("REPORT_PERIOD_ID") or "").strip()
        if not period_id:
            continue
        period_end = _SSE_REPORT_PERIODS.get(period_id)
        year_text = str(row.get("REPORT_YEAR") or "").strip()
        if period_end is None or not re.fullmatch(r"20\d{2}", year_text):
            raise ExchangeFinancialError("SSE XBRL row has an unsupported report period")
        if int(year_text) != requested_year:
            raise ExchangeFinancialError("SSE XBRL row year was not requested")
        report_date = f"{year_text}-{period_end}"
        if report_date in identities:
            raise ExchangeFinancialError("SSE XBRL contains a duplicate code/period identity")
        identities.add(report_date)
        values = _sse_values(row)
        if values:
            output.append(
                {
                    "security_code": code,
                    "report_date": report_date,
                    "source_kind": "sse_xbrl_summary",
                    "source_url": final_url,
                    "source_raw_sha256": raw_sha256,
                    "source_fields": values,
                    "company_statement": False,
                }
            )
    return output


def _discover_szse_documents(raw: bytes) -> dict[tuple[str, str], tuple[str, str]]:
    try:
        index_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExchangeFinancialError("SZSE topic page is not UTF-8") from exc
    documents: dict[tuple[str, str], tuple[str, str]] = {}
    for match in _SZSE_LINK.finditer(index_text):
        title = match.group("title").strip()
        parsed_title = _SZSE_TITLE.fullmatch(title)
        if parsed_title is None:
            continue
        report_date = f"{parsed_title.group('year')}-{_SZSE_PERIOD_END[parsed_title.group('period')]}"
        board = parsed_title.group("board")
        identity = (board, report_date)
        if identity in documents:
            raise ExchangeFinancialError("SZSE topic page contains a duplicate board/period document")
        source_url = urljoin(SZSE_TOPIC_URL, match.group("href")[2:])
        parsed_url = urlsplit(source_url)
        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ExchangeFinancialError("SZSE topic page contains an invalid document URL") from exc
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "investor.szse.cn"
            or port not in {None, 443}
            or not re.fullmatch(r"/market/subject/P\d+\.docx", parsed_url.path)
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ExchangeFinancialError("SZSE topic page contains an invalid document path")
        documents[identity] = (title, source_url)
    if not documents:
        raise ExchangeFinancialError("SZSE topic page contains no recognized financial documents")
    return documents


def _sse_values(row: Mapping[str, Any]) -> dict[str, float]:
    assets = _finite(row.get("S2010_0380"))
    parent_profit = _finite(row.get("S2090_0040"))
    operating_cash = _finite(row.get("S2090_0060"))
    values: dict[str, float] = {}
    if parent_profit is not None:
        candidate = round(parent_profit * 10_000.0, 2)
        if assets is None or abs(candidate) <= max(abs(assets) * 5.0, 1.0):
            values["PARENT_NETPROFIT"] = candidate
    if operating_cash is not None:
        candidate = round(operating_cash * 10_000.0, 2)
        if assets is None or abs(candidate) <= max(abs(assets) * 10.0, 1.0):
            values["NETCASH_OPERATE"] = candidate
    revenue = _finite(row.get("S2020_0010"))
    if revenue is not None and revenue >= 0:
        candidates = (revenue, round(revenue * 10_000.0, 2))
        plausible = [
            candidate
            for candidate in candidates
            if assets is not None and assets > 0 and 0.001 <= candidate / assets <= 10.0
        ]
        if len(plausible) == 1:
            values["TOTAL_OPERATE_INCOME"] = plausible[0]
    return values


def _docx_text_and_rows(raw: bytes) -> tuple[str, list[list[str]]]:
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > _DOCX_MAX_MEMBERS:
                raise ExchangeFinancialError("SZSE DOCX has too many archive members")
            if any(
                member.filename.startswith(("/", "\\")) or ".." in Path(member.filename.replace("\\", "/")).parts
                for member in members
            ):
                raise ExchangeFinancialError("SZSE DOCX contains an unsafe archive path")
            if sum(member.file_size for member in members) > _DOCX_UNCOMPRESSED_MAX_BYTES:
                raise ExchangeFinancialError("SZSE DOCX exceeds its uncompressed byte limit")
            document = archive.getinfo("word/document.xml")
            if document.file_size > _DOCX_XML_MAX_BYTES:
                raise ExchangeFinancialError("SZSE DOCX document XML exceeds its byte limit")
            xml = archive.read(document)
    except (BadZipFile, KeyError) as exc:
        raise ExchangeFinancialError("SZSE source is not a valid bounded DOCX") from exc
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ExchangeFinancialError("SZSE DOCX document XML is invalid") from exc
    all_text = "".join(node.text or "" for node in root.findall(".//w:t", _W_NS))
    rows: list[list[str]] = []
    for table_row in root.findall(".//w:tr", _W_NS):
        cells = [
            "".join(node.text or "" for node in cell.findall(".//w:t", _W_NS)).strip()
            for cell in table_row.findall("./w:tc", _W_NS)
        ]
        if cells:
            rows.append(cells)
        if len(rows) > _MAX_SZSE_ROWS:
            raise ExchangeFinancialError("SZSE DOCX exceeds its table row limit")
    return all_text, rows


def _parse_szse_docx(
    raw: bytes,
    *,
    source_url: str,
    expected_title: str,
    expected_board: str,
    expected_report_date: str,
    as_of: date,
    requested_codes: set[str],
) -> list[dict[str, Any]]:
    text, rows = _docx_text_and_rows(raw)
    if expected_title not in text:
        raise ExchangeFinancialError("SZSE DOCX title differs from the topic-page identity")
    cutoff_match = re.search(r"截至日期[：:]\s*(20\d{2}-\d{2}-\d{2})", text)
    if cutoff_match is None:
        raise ExchangeFinancialError("SZSE DOCX omitted its source cutoff date")
    cutoff = date.fromisoformat(cutoff_match.group(1))
    if cutoff > as_of:
        return []
    header_index = next((index for index, row in enumerate(rows) if tuple(row[:6]) == _SZSE_HEADERS), None)
    if header_index is None:
        raise ExchangeFinancialError("SZSE DOCX financial-table schema changed")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    for row in rows[header_index + 1 :]:
        if len(row) < 6:
            continue
        code = _code(row[0])
        if not code or code not in requested_codes:
            continue
        if expected_board == "创业板" and not code.startswith("3"):
            raise ExchangeFinancialError("SZSE growth-board document contains a mismatched requested code")
        if expected_board == "主板" and not code.startswith(("0", "2")):
            raise ExchangeFinancialError("SZSE main-board document contains a mismatched requested code")
        if code in seen:
            raise ExchangeFinancialError("SZSE DOCX contains a duplicate requested security")
        seen.add(code)
        name = row[1].strip()
        if not name or len(name) > 40:
            raise ExchangeFinancialError("SZSE DOCX contains an invalid company name")
        net_profit_wan = _finite(row[2])
        eps = _finite(row[3])
        operating_cash_per_share = _finite(row[4])
        fields: dict[str, float] = {}
        if net_profit_wan is not None:
            fields["PARENT_NETPROFIT"] = round(net_profit_wan * 10_000.0, 2)
        if not fields and eps is None and operating_cash_per_share is None:
            continue
        output.append(
            {
                "security_code": code,
                "company_name": name,
                "report_date": expected_report_date,
                "source_kind": "szse_periodic_financial_indicators",
                "source_url": source_url,
                "source_raw_sha256": raw_sha256,
                "source_cutoff": cutoff.isoformat(),
                "source_fields": fields,
                "per_share_indicators": {
                    "EPS": eps,
                    "OPERATING_CASH_FLOW_PER_SHARE": operating_cash_per_share,
                },
                "distribution_plan": row[5].strip()[:200],
                "company_statement": False,
            }
        )
    return output


def _records(company: Mapping[str, Any], dataset: str, report_date: str) -> list[Mapping[str, Any]]:
    rows = company.get(dataset, [])
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, (list, tuple)):
        return []
    return [
        row for row in rows if isinstance(row, Mapping) and str(row.get("REPORT_DATE") or "").strip() == report_date
    ]


def _source_can_fill(company: Mapping[str, Any], source_kind: str, report_dates: Collection[str]) -> bool:
    for report_date in report_dates:
        annual = report_date.endswith("-12-31")
        if source_kind == "sse":
            checks = (
                ("revenue_history" if annual else "income_interim", "TOTAL_OPERATE_INCOME"),
                ("income_history" if annual else "income_interim", "PARENT_NETPROFIT"),
                ("cashflow" if annual else "cashflow_interim", "NETCASH_OPERATE"),
            )
        else:
            checks = (("income_history" if annual else "income_interim", "PARENT_NETPROFIT"),)
        for dataset, field in checks:
            matches = _records(company, dataset, report_date)
            if len(matches) > 1:
                raise ExchangeFinancialError(f"duplicate financial identity before exchange overlay: {report_date}")
            if not matches or field not in matches[0] or matches[0].get(field) is None:
                return True
    return False


def _rotated_limit(values: list[str], limit: int, *, seed: int) -> list[str]:
    if len(values) <= limit:
        return values
    start = seed % len(values)
    rotated = values[start:] + values[:start]
    return rotated[:limit]


def _mutable_row(company: dict[str, Any], dataset: str, report_date: str) -> dict[str, Any]:
    rows = company.setdefault(dataset, [])
    if not isinstance(rows, list):
        raise ExchangeFinancialError(f"exchange overlay target {dataset} is not a list")
    matches = [row for row in rows if isinstance(row, dict) and str(row.get("REPORT_DATE") or "") == report_date]
    if len(matches) > 1:
        raise ExchangeFinancialError(f"duplicate financial identity before exchange overlay: {report_date}")
    if matches:
        return matches[0]
    row = {"REPORT_DATE": report_date}
    if not report_date.endswith("-12-31"):
        row["period_end"] = report_date[5:]
    rows.append(row)
    rows.sort(key=lambda value: str(value.get("REPORT_DATE") or ""))
    return row


def _provenance(record: Mapping[str, Any], source_field: str) -> dict[str, Any]:
    return {
        "adapter_version": EXCHANGE_FINANCIAL_ADAPTER_VERSION,
        "source_kind": record["source_kind"],
        "source_url": record["source_url"],
        "source_raw_sha256": record["source_raw_sha256"],
        "security_code": record["security_code"],
        "report_date": record["report_date"],
        "source_field": source_field,
        "company_statement": False,
    }


def _overlay_record(
    output: dict[str, dict[str, Any]],
    record: Mapping[str, Any],
    counters: Counter[str],
    conflicts: set[str],
) -> None:
    code = record["security_code"]
    company = output[code]
    report_date = record["report_date"]
    annual = report_date.endswith("-12-31")
    source_fields = record.get("source_fields")
    if not isinstance(source_fields, Mapping):
        raise ExchangeFinancialError("exchange record omitted source fields")
    targets: list[tuple[str, str, float]] = []
    for field, value in source_fields.items():
        if field == "TOTAL_OPERATE_INCOME":
            dataset = "revenue_history" if annual else "income_interim"
            targets.append((dataset, field, float(value)))
            if annual:
                targets.append(("income_history", field, float(value)))
        elif field == "PARENT_NETPROFIT":
            targets.append(("income_history" if annual else "income_interim", field, float(value)))
        elif field == "NETCASH_OPERATE":
            targets.append(("cashflow" if annual else "cashflow_interim", field, float(value)))
    filled_this_record = 0
    for dataset, field, value in targets:
        row = _mutable_row(company, dataset, report_date)
        if field in row and row.get(field) is not None:
            existing = _finite(row.get(field))
            if existing is None or not math.isclose(existing, value, rel_tol=1e-10, abs_tol=0.02):
                counters["conflicts"] += 1
                conflicts.add(code)
            continue
        row[field] = value
        row[f"{field}_PROVENANCE"] = _provenance(record, field)
        counters["filled_fields"] += 1
        filled_this_record += 1
    if filled_this_record:
        evidence = {key: deepcopy(record[key]) for key in record if key != "source_fields"}
        evidence["filled_source_fields"] = sorted(source_fields)
        existing_evidence = company.setdefault("exchange_structured_evidence", [])
        if not isinstance(existing_evidence, list):
            raise ExchangeFinancialError("exchange structured evidence target is not a list")
        identity = (evidence["source_kind"], evidence["report_date"], evidence["source_raw_sha256"])
        if not any(
            isinstance(item, Mapping)
            and (item.get("source_kind"), item.get("report_date"), item.get("source_raw_sha256")) == identity
            for item in existing_evidence
        ):
            existing_evidence.append(evidence)
            existing_evidence.sort(
                key=lambda item: (str(item.get("report_date") or ""), str(item.get("source_kind") or ""))
            )


def backfill_exchange_financial_gaps(
    financials: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
    *,
    codes: Collection[str] | None = None,
    as_of: date | None = None,
    client: ExchangeFinancialClient | None = None,
    max_sse_codes: int = EXCHANGE_FINANCIAL_MAX_SSE_CODES,
    max_szse_codes: int = EXCHANGE_FINANCIAL_MAX_SZSE_CODES,
    force_refresh: bool = False,
) -> ExchangeFinancialOutcome:
    """Fill only missing exchange-structured facts after primary/Sina sources."""

    started = time.monotonic()
    if max_sse_codes < 1 or max_szse_codes < 1 or not isinstance(force_refresh, bool):
        raise ValueError("exchange financial company limits must be positive")
    normalized_contract = _report_contract(contract)
    report_dates = tuple(normalized_contract.values())
    population = sorted({_code(value) for value in (codes or financials.keys())} - {""})
    current_as_of = as_of or shanghai_today()
    seed = current_as_of.toordinal()
    sse_candidates = [
        code
        for code in population
        if code.startswith("6") and code in financials and _source_can_fill(financials[code], "sse", report_dates)
    ]
    szse_candidates = [
        code
        for code in population
        if code.startswith(("0", "3"))
        and code in financials
        and _source_can_fill(financials[code], "szse", report_dates)
    ]
    sse_targets = _rotated_limit(sse_candidates, max_sse_codes, seed=seed)
    szse_targets = _rotated_limit(szse_candidates, max_szse_codes, seed=seed)
    active_client = client or ExchangeFinancialClient(use_cache=not force_refresh)
    source_records: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    years = {int(value[:4]) for value in report_dates}
    for code in sse_targets:
        try:
            source_records.extend(active_client.fetch_sse(code, years))
            status_counts["sse_ok"] += 1
        except ExchangeFinancialError:
            status_counts["sse_source_unavailable"] += 1
    if szse_targets:
        try:
            source_records.extend(
                active_client.fetch_szse(report_dates, as_of=current_as_of, requested_codes=szse_targets)
            )
            status_counts["szse_ok"] += 1
        except ExchangeFinancialError:
            status_counts["szse_source_unavailable"] += 1

    output: dict[str, dict[str, Any]] = {code: deepcopy(dict(company)) for code, company in financials.items()}
    counters: Counter[str] = Counter()
    conflicts: set[str] = set()
    filled_codes: set[str] = set()
    target_report_dates = set(report_dates)
    for record in source_records:
        code = record["security_code"]
        if code not in output:
            raise ExchangeFinancialError("exchange record escaped the requested financial population")
        # SSE returns every annual and half-year row for each requested year.
        # Only the three periods in this generation's reporting contract may
        # enter the canonical financial datasets; newer/older valid rows stay
        # available in the source cache for the next filing-window rollover.
        if record["report_date"] not in target_report_dates:
            counters["ignored_non_target_records"] += 1
            continue
        before = counters["filled_fields"]
        _overlay_record(output, record, counters, conflicts)
        if counters["filled_fields"] > before:
            filled_codes.add(code)
    diagnostic = {
        "adapter_version": EXCHANGE_FINANCIAL_ADAPTER_VERSION,
        "strategy": "eastmoney_then_sina_then_exchange_structured_gap_only",
        "candidate_codes": len(sse_candidates) + len(szse_candidates),
        "sse_candidate_codes": len(sse_candidates),
        "sse_target_codes": len(sse_targets),
        "sse_skipped_codes": len(sse_candidates) - len(sse_targets),
        "szse_candidate_codes": len(szse_candidates),
        "szse_target_codes": len(szse_targets),
        "szse_skipped_codes": len(szse_candidates) - len(szse_targets),
        "source_records": len(source_records),
        "ignored_non_target_records": counters["ignored_non_target_records"],
        "filled_fields": counters["filled_fields"],
        "filled_codes": sorted(filled_codes),
        "conflicts": counters["conflicts"],
        "conflict_codes": sorted(conflicts),
        "status_counts": dict(sorted(status_counts.items())),
        "client": active_client.diagnostic(),
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    return ExchangeFinancialOutcome(output, diagnostic)


__all__ = [
    "EXCHANGE_FINANCIAL_ADAPTER_VERSION",
    "EXCHANGE_FINANCIAL_MAX_REQUESTS",
    "ExchangeFinancialClient",
    "ExchangeFinancialError",
    "ExchangeFinancialOutcome",
    "backfill_exchange_financial_gaps",
]
