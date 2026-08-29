"""Bounded public-announcement evidence for the Patch 4 technology prerequisite.

The extractor deliberately fails closed.  It only turns a criterion into a
value when an Eastmoney announcement body states that fact directly.  Missing
words are never interpreted as ``False`` and grant/exercise-price text is never
treated as a share-price performance condition.

Announcement bodies are processed in memory.  The cache retains only document
identities, bounded excerpts, page/body hashes, diagnostics, and the validated
``type7_patch4_assessment`` contract; it never persists full announcement text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS
from data.as_of import shanghai_today
from data.cache import SafeCacheConflict, SafeCacheError, SafeFileCache
from data.provider_http import RequestRateLimiter, is_transient_request_error, retry_delay_seconds, thread_local_session
from engine.quality_equity import (
    PATCH4_MAX_EVIDENCE_AGE_DAYS,
    PATCH4_MODEL_ID,
    PATCH4_SCHEMA_VERSION,
    QualityEquityError,
    normalise_patch4_assessment,
)


MODEL_ID = "patch4-public-announcement-evidence-v2"
CACHE_SCHEMA_VERSION = 2
RULES_VERSION = "patch4-direct-explicit-facts-v2"

ANNOUNCEMENT_PAGE_PREFIX = "https://data.eastmoney.com/notices/stock/"
ANNOUNCEMENT_LIST_ENDPOINT = "https://np-anotice-stock.eastmoney.com/api/security/ann"
ANNOUNCEMENT_CONTENT_ENDPOINT = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
ANNOUNCEMENT_DETAIL_PREFIX = "https://data.eastmoney.com/notices/detail/"
PATCH4_EVIDENCE_CACHE_DIR = CACHE_DIRECTORY / "patch4_evidence"

PAGE_SIZE = 50
MAX_METADATA_PAGES = 6
MAX_METADATA_HITS = 10_000
MAX_DOCUMENTS = 24
MAX_BODY_PAGES_PER_DOCUMENT = 30
MAX_TOTAL_BODY_PAGES = 120
MAX_ATTACHMENTS_PER_DOCUMENT = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BODY_BYTES = 32 * 1024 * 1024
MAX_BODY_CHARACTERS_PER_DOCUMENT = 600_000
MAX_BATCH_COMPANIES = 1_000
MAX_WORKERS = 2
BATCH_SOURCE_FAILURE_LIMIT = 2
REQUEST_TIMEOUT = (15, 30)
REQUEST_ATTEMPTS = 2
REQUEST_INTERVAL_SECONDS = 0.20
RETRY_BACKOFF_SECONDS = 0.50

_A_SHARE_CODE = re.compile(r"^[036][0-9]{5}$")
_CANONICAL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ART_CODE = re.compile(r"^AN[0-9]{18}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NOTICE_DATE = re.compile(r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})(?:[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?)?$")
_RELEVANT_TITLE = re.compile(r"股权激励|员工持股|股票期权|限制性股票|激励计划|持股平台")
_PLAN_IDENTITY = re.compile(
    r"(?P<year>20[0-9]{2})年.{0,24}?"
    r"(?P<kind>限制性股票|股票期权|员工持股|股权激励)(?:激励)?计划"
)
_GENERIC_PLAN_IDENTITY = re.compile(r"(?P<year>20[0-9]{2})年(?:本|该)?(?:激励)?计划")
_PLAN_PHASE = re.compile(r"第(?:[一二三四五六七八九十]|[0-9]{1,2})+期")
_CANONICAL_PLAN_ID = re.compile(
    r"^20[0-9]{2}:(?:限制性股票|股票期权|员工持股|股权激励):"
    r"(?:未分期|第(?:[一二三四五六七八九十]|[0-9]{1,2})+期)$"
)
_SENTENCE_BREAK = re.compile(r"(?<=[。！？；])|\n+")
_SPACE = re.compile(r"\s+")
_PERCENT = re.compile(r"(?<![0-9])(?P<value>[0-9]{1,3}(?:\.[0-9]{1,6})?)(?![0-9.])\s*[%％]")
_SPECULATIVE_PERCENT = re.compile(
    r"不低于|不高于|不超过|至少|至多|上限|下限|以上|以下|约|左右|区间|范围|拟|预计|行权价格|"
    r"(?<!持股)(?<!激励)计划(?:持股|达到|覆盖|占|为)|(?:持股比例|覆盖率|覆盖比例)(?:拟|计划|预计)"
)
_CORE_ROLE = re.compile(r"核心(?:研发|技术)(?:人员|人才|骨干|团队)")
_OWNERSHIP = re.compile(r"持股(?:比例)?|持有(?:公司)?股份(?:比例)?")
_COMPANY_SHARE_DENOMINATOR = re.compile(
    r"(?:占|相当于|比例(?:为|达到))(?:本|该)?(?:上市)?公司(?:的)?"
    r"(?:总股本|股份总数|总股份|全部股份)|"
    r"(?:占|相当于|比例(?:为|达到))总股本"
)
_COVERAGE = re.compile(
    r"核心(?:研发|技术|人才|人员|骨干).{0,24}覆盖(?:率|比例)|"
    r"(?:覆盖|纳入)(?:的)?核心(?:研发|技术|人才|人员|骨干)(?:人数|人员)"
)
_CORE_TALENT_DENOMINATOR = re.compile(
    r"(?:占|占比(?:为|达到)?)(?:本|该)?(?:上市)?公司(?:全体)?"
    r"核心(?:研发|技术|人才|人员|骨干)(?:总人数|总数)|"
    r"(?:以|按)(?:本|该)?(?:上市)?公司(?:全体)?"
    r"核心(?:研发|技术|人才|人员|骨干)(?:总人数|总数)(?:为|作为)(?:统计)?分母"
)
_DECLARATION = re.compile(r"合计(?:为|达到)?|为|达到|占(?:比)?")
_RD_METRIC = re.compile(r"研发(?:投入|费用|强度|成果|考核指标)|专利(?:数量|数|申请|授权)|技术成果")
_PERFORMANCE = re.compile(r"考核|归属|解除限售|解锁|行权|业绩指标")
_MULTI_YEAR = re.compile(
    r"长期|连续(?:两|二|三|四|五|[2-5])年|(?:两|二|三|四|五|[2-5])年|20[0-9]{2}年.{0,24}20[0-9]{2}年"
)
_NEGATION = re.compile(
    r"不设置|未设置|不包含|未包含|不包括|未包括|不涉及|未涉及|"
    r"不纳入|未纳入|不作为|未获授|未授予|无权益|不与|未与"
)
_INCLUSION = re.compile(r"(?<!不)(?<!未)(?:包括|包含|涵盖|纳入)|激励对象(?:为|包括)|授予对象(?:为|包括)")
_FRONTLINE_RD = re.compile(r"一线(?:研发|技术)人员|研发一线人员|基层研发人员")
_EQUITY_PLAN = re.compile(r"股权激励|员工持股|限制性股票|股票期权|激励计划|授予|权益")
_EQUITY_RECIPIENT = re.compile(r"激励对象|授予对象|获授对象|员工持股计划参与(?:人|对象)")
_MIXED_OWNERSHIP_SUBJECT = re.compile(
    r"(?:及|和|与|、|等).{0,16}(?:董事|监事|高级管理人员|管理人员|其他员工|其他人员)|"
    r"(?:董事|监事|高级管理人员|管理人员|其他员工|其他人员).{0,16}(?:及|和|与|、|等)"
)
_PRICE_METRIC = re.compile(r"股价考核|股票价格考核|股价目标|股价表现")
_SHORT_HORIZON = re.compile(r"短期|一年|1年|12个月|当期|本年度|年度股价")
_GRANT_PRICE_ONLY = re.compile(r"授予价格|行权价格|回购价格|市场参考价|定价基准")
_METRIC_BINDING = re.compile(r"(?<!不)(?<!未)(?:包含|纳入|作为|绑定|挂钩|列入|设置(?:为)?)")
_COORDINATED_CLAUSE = re.compile(r"并|且|同时|另外|另将|另把|以及|随后|而")
_INACTIVE_PLAN = re.compile(
    r"(?:本次)?(?:终止实施|终止|停止实施|取消实施|撤销实施)(?:的)?[^，,；;。！？]{0,24}"
    r"(?:股权激励|员工持股|限制性股票|股票期权|激励)计划|"
    r"已(?:经)?(?:终止|停止实施|失效|作废|取消实施|取消|撤销实施|撤销|结束)(?:的|实施)?[^，,；;。！？]{0,16}"
    r"(?:股权激励|员工持股|限制性股票|股票期权|激励)计划|"
    r"(?:本|该)(?:股权激励|员工持股|激励)?计划[^，,；;。！？]{0,16}"
    r"(?:(?:已经|已|决定)?(?:终止实施|终止|停止实施|失效|作废|取消实施|撤销实施|不再实施|已经结束|已结束)|"
    r"正式废止|宣布结束)|"
    r"(?:决定|同意|现|已|已经)?(?:终止实施|终止|停止实施|取消实施|取消|撤销实施|撤销|不再实施)"
    r"(?:本次|本|该)(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划"
)
_CONDITIONAL_PLAN_TERMINATION = re.compile(
    r"(?:若|如果|如|一旦|当).{0,80}"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施|失效|作废)|"
    r"(?:出现|发生|存在)(?:下列|以下|上述).{0,24}"
    r"(?:情形|情况)(?:之一)?(?:时|则)|"
    r"(?:未满足|未达到|不符合).{0,40}(?:时|则)"
)
_PROCEDURAL_PLAN_TERMINATION = re.compile(
    r"(?:(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施).{0,24}"
    r"(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划|"
    r"(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划.{0,24}"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施))"
    r"(?:的|时|后)[，,]?(?:公司|董事会|股东大会)?(?:应当|应|须|需要|需|将由|由).{0,40}"
    r"(?:审议|决定|披露|公告|处理|注销)"
)
_NEGATED_PLAN_TERMINATION = re.compile(
    r"(?<!并非)(?<!不是)(?<!非)"
    r"(?:尚未|并未|并无|从未|未曾|未|不曾|没有|不会|无需|无须|不需要|不予|"
    r"(?<!不得)(?<!不能)不(?!得不|能不)|不得(?!不)|无权|否认)"
    r".{0,8}(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)"
    r".{0,24}(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划|"
    r"(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划.{0,16}"
    r"(?<!并非)(?<!不是)(?<!非)"
    r"(?:尚未|并未|并无|从未|未曾|未|不曾|没有|不会|无需|无须|不需要|不予|"
    r"(?<!不得)(?<!不能)不(?!得不|能不)|不得(?!不)|无权|否认)"
    r".{0,8}(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)|"
    r"(?<!并非)(?<!不是)(?<!非)(?:不存在|未发生).{0,32}"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施).{0,24}"
    r"(?:(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划|情形)"
)
_NON_FINAL_PLAN_TERMINATION = re.compile(
    r"(?:(?:公司|董事会|股东大会)(?:拟|计划|考虑|可能|或将|提议|提出|建议)|"
    r"(?:拟|考虑|可能|或将|提议|提出|建议|不排除))[^，,；;。！？]{0,16}"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)"
    r"[^。！？]{0,32}(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划|"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)"
    r"[^。！？]{0,24}(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划"
    r"[^。！？]{0,32}(?:尚待|有待|尚未|未获|未通过|被[^，,；;。！？]{0,8}否决|"
    r"否决|撤回|已经取消|议案取消|程序尚未启动|风险|可能性|传闻|消息|说法|报道)|"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)"
    r"[^。！？]{0,24}(?:风险|可能性|传闻|消息|说法|报道)[^。！？]{0,12}(?:不实|未证实|不准确)?|"
    r"(?:撤回|撤销|取消|否决)[^。！？]{0,16}(?:关于)?"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)[^。！？]{0,24}"
    r"(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划[^。！？]{0,16}议案|"
    r"(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划[^。！？]{0,12}"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)[^。！？]{0,16}"
    r"(?:程序|事项|议案)[^。！？]{0,12}(?:尚未启动|尚待|有待|未获|未通过|否决|取消)"
)
_EXPLICIT_PLAN_KIND = re.compile(r"限制性股票|股票期权|员工持股|股权激励")
_PLAN_KIND_REFERENCE = re.compile(r"(限制性股票|股票期权|员工持股|股权激励)(?:激励)?计划")
_OLD_PLAN_MARKER = re.compile(r"前期|旧(?:的)?|上一期|往期|此前|历史|原(?:股权激励|员工持股|激励)?计划")
_OLD_PLAN_REFERENCE = re.compile(
    r"(?:前期|旧(?:的)?|上一期|往期|此前|历史|原)"
    r"[^，,；;。！？]{0,12}(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划"
)
_DECISIVE_PLAN_ACTION = re.compile(
    r"(?:董事会|股东大会|公司)[^，,；;。！？]{0,24}"
    r"(?:审议通过[^，,；;。！？]{0,12})?(?:决定|同意)[^，,；;。！？]{0,16}"
    r"(?:终止实施|终止|停止实施|取消实施|撤销实施|不再实施)|"
    r"(?:本次|本|该)?(?:股权激励|员工持股|限制性股票|股票期权|激励)?计划"
    r"[^，,；;。！？]{0,8}(?:已经|已)(?:正式)?(?:终止实施|终止|停止实施|失效|作废|取消实施|撤销实施|不再实施)"
)
_BATCH_GLOBAL_SOURCE_FAILURE = re.compile(
    r"ConnectTimeout|ReadTimeout|ConnectionError|ProxyError|SSLError|"
    r"RemoteDisconnected|NameResolutionError|NewConnectionError|"
    r"HTTPError:[^:]{0,180}(?:403|429|5[0-9]{2})|"
    r"announcement response (?:redirected outside the pinned HTTPS endpoint|"
    r"URL does not match the requested identity|is not JSON-compatible content|"
    r"is not UTF-8|is not valid JSON|contains non-finite JSON)|"
    r"announcement payload is not an object|JSON contains a duplicate key|"
    r"announcement (?:metadata|body) (?:source reported failure|data is not an object)"
)

_CRITERIA = (
    "core_rd_ownership_pct",
    "esop_core_talent_coverage_pct",
    "long_term_rd_metrics",
    "frontline_rd_equity",
    "short_term_price_binding",
)
_PERCENT_CRITERIA = {"core_rd_ownership_pct", "esop_core_talent_coverage_pct"}
_JSON_CONTENT_TYPES = {"application/json", "text/plain"}


class Patch4EvidenceError(RuntimeError):
    """A source, response, identity, extraction, or cache contract failed."""


@dataclass(frozen=True)
class Patch4Evidence:
    available: bool
    code: str
    as_of: str
    model_id: str
    assessment: dict[str, Any] | None
    criteria: dict[str, dict[str, Any]]
    status: str
    documents: list[dict[str, Any]]
    cache_hit: bool
    cache_diagnostic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Announcement:
    art_code: str
    notice_date: str
    title: str


@dataclass(frozen=True)
class _Fact:
    key: str
    value: float | bool
    announcement: _Announcement
    detail_url: str
    content_sha256: str
    snippet: str


@dataclass
class _RequestBudget:
    body_pages: int = 0
    body_bytes: int = 0

    def consume(self, size: int) -> None:
        if self.body_pages >= MAX_TOTAL_BODY_PAGES:
            raise Patch4EvidenceError("announcement body-page budget exceeded")
        if size < 0 or self.body_bytes + size > MAX_TOTAL_BODY_BYTES:
            raise Patch4EvidenceError("announcement total body-byte budget exceeded")
        self.body_pages += 1
        self.body_bytes += size


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._suppressed += 1
        elif not self._suppressed and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._suppressed:
            self._suppressed -= 1
        elif not self._suppressed and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


_GLOBAL_RATE_LIMITER = RequestRateLimiter(REQUEST_INTERVAL_SECONDS)


def _error_label(exc: BaseException, *, limit: int = 180) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}:{message[:limit]}" if message else type(exc).__name__


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if not _A_SHARE_CODE.fullmatch(code):
        raise ValueError("Patch 4 code must be a Shanghai/Shenzhen six-digit code")
    return code


def _parse_as_of(value: date | str) -> date:
    if isinstance(value, datetime):
        raise TypeError("as_of must be a date without a time component")
    if isinstance(value, date):
        parsed = value
    else:
        if not isinstance(value, str) or not _CANONICAL_DATE.fullmatch(value):
            raise ValueError("as_of must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("as_of must be a valid calendar date") from exc
    if parsed > shanghai_today():
        raise ValueError("as_of cannot be in the future")
    return parsed


def _parse_notice_date(value: Any) -> date:
    if not isinstance(value, str):
        raise Patch4EvidenceError("announcement date is not text")
    match = _NOTICE_DATE.fullmatch(value.strip())
    if match is None:
        raise Patch4EvidenceError("announcement date format is invalid")
    try:
        return date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise Patch4EvidenceError("announcement date is invalid") from exc


def _clean_text(value: Any, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise Patch4EvidenceError(f"{label} is not text")
    text = _SPACE.sub(" ", value).strip()
    if not text or len(text) > limit or any(ord(character) < 32 for character in text):
        raise Patch4EvidenceError(f"{label} is invalid")
    return text


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Patch4EvidenceError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                Patch4EvidenceError(f"announcement response contains non-finite JSON: {value}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise Patch4EvidenceError("announcement response is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise Patch4EvidenceError("announcement response is not valid JSON") from exc


def _read_bounded_response(response: Any) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > MAX_RESPONSE_BYTES:
            raise Patch4EvidenceError("announcement response exceeds the declared byte limit")
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type not in _JSON_CONTENT_TYPES:
        raise Patch4EvidenceError("announcement response is not JSON-compatible content")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise Patch4EvidenceError("announcement response does not support bounded streaming")
    chunks: list[bytes] = []
    received = 0
    for chunk in iterator(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise Patch4EvidenceError("announcement response yielded non-byte content")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise Patch4EvidenceError("announcement response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_safe_https_url(value: Any, *, hostname: str | None = None, path: str | None = None) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname
        and (hostname is None or parsed.hostname.casefold() == hostname.casefold())
        and (path is None or parsed.path == path)
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _validate_final_url(value: Any, endpoint: str, expected_params: Mapping[str, Any]) -> None:
    expected = urlsplit(endpoint)
    if not _is_safe_https_url(value, hostname=expected.hostname, path=expected.path):
        raise Patch4EvidenceError("announcement response redirected outside the pinned HTTPS endpoint")
    parsed = urlsplit(str(value))
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    normalized = {key: [str(item)] for key, item in expected_params.items()}
    if query != normalized:
        raise Patch4EvidenceError("announcement response URL does not match the requested identity")


def _request_json(
    endpoint: str,
    params: Mapping[str, Any],
    *,
    session: Any,
    headers: Mapping[str, str],
    timeout: tuple[int, int],
    rate_limiter: Any,
) -> tuple[Mapping[str, Any], int]:
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        should_retry = False
        try:
            rate_limiter.acquire()
            response = session.get(
                endpoint,
                params=dict(params),
                headers=dict(headers),
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            _validate_final_url(getattr(response, "url", None), endpoint, params)
            raw = _read_bounded_response(response)
            payload = _decode_json(raw)
            if not isinstance(payload, Mapping):
                raise Patch4EvidenceError("announcement payload is not an object")
            return payload, len(raw)
        except Patch4EvidenceError:
            raise
        except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
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
                base_seconds=RETRY_BACKOFF_SECONDS,
            )
        )
    raise Patch4EvidenceError(f"announcement request failed: {_error_label(last_error or RuntimeError())}")


def _metadata_params(code: str, page_index: int) -> dict[str, Any]:
    return {
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
        "f_node": "5",
        "s_node": "9",
        "page_index": page_index,
        "page_size": PAGE_SIZE,
        "sr": -1,
    }


def _headers(code: str) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{ANNOUNCEMENT_PAGE_PREFIX}{code}.html",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
    }


def _validate_code_rows(value: Any, code: str) -> None:
    if not isinstance(value, list) or len(value) != 1:
        raise Patch4EvidenceError("announcement security-code bindings are invalid")
    matches = 0
    for item in value:
        if not isinstance(item, Mapping):
            raise Patch4EvidenceError("announcement security-code binding is not an object")
        stock_code = item.get("stock_code")
        market_code = item.get("market_code")
        short_name = item.get("short_name")
        if (
            not isinstance(stock_code, str)
            or not _A_SHARE_CODE.fullmatch(stock_code)
            or isinstance(market_code, bool)
            or not isinstance(market_code, (str, int))
            or not str(market_code).strip()
            or len(str(market_code)) > 20
            or not isinstance(short_name, str)
            or not short_name.strip()
            or len(short_name) > 100
        ):
            raise Patch4EvidenceError("announcement security-code binding fields are invalid")
        matches += int(stock_code == code)
    if matches != 1:
        raise Patch4EvidenceError("announcement is not uniquely bound to the requested security")


def _validate_columns(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise Patch4EvidenceError("announcement category bindings are invalid")
    for item in value:
        if not isinstance(item, Mapping):
            raise Patch4EvidenceError("announcement category binding is not an object")
        for field in ("column_code", "column_name"):
            text = item.get(field)
            if not isinstance(text, str) or not text.strip() or len(text) > 100:
                raise Patch4EvidenceError("announcement category binding fields are invalid")


def _validate_metadata_page(
    payload: Mapping[str, Any],
    *,
    code: str,
    requested_page: int,
) -> tuple[list[_Announcement], int]:
    success = payload.get("success")
    if isinstance(success, bool) or success != 1:
        raise Patch4EvidenceError("announcement metadata source reported failure")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise Patch4EvidenceError("announcement metadata data is not an object")
    total_hits = data.get("total_hits")
    page_index = data.get("page_index")
    page_size = data.get("page_size")
    rows = data.get("list")
    if (
        isinstance(total_hits, bool)
        or not isinstance(total_hits, int)
        or not 0 <= total_hits <= MAX_METADATA_HITS
        or page_index != requested_page
        or page_size != PAGE_SIZE
        or not isinstance(rows, list)
        or len(rows) > PAGE_SIZE
    ):
        raise Patch4EvidenceError("announcement metadata pagination is invalid")
    total_pages = math.ceil(total_hits / PAGE_SIZE)
    if requested_page <= total_pages:
        expected_rows = PAGE_SIZE if requested_page < total_pages else total_hits - PAGE_SIZE * (total_pages - 1)
        if len(rows) != expected_rows:
            raise Patch4EvidenceError("announcement metadata row count is inconsistent")
    elif rows:
        raise Patch4EvidenceError("announcement metadata returned rows beyond its declared pages")

    normalized: list[_Announcement] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise Patch4EvidenceError("announcement metadata row is not an object")
        art_code = row.get("art_code")
        if not isinstance(art_code, str) or not _ART_CODE.fullmatch(art_code):
            raise Patch4EvidenceError("announcement art code is invalid")
        published = _parse_notice_date(row.get("notice_date"))
        title = _clean_text(row.get("title"), label="announcement title", limit=500)
        _validate_code_rows(row.get("codes"), code)
        _validate_columns(row.get("columns"))
        normalized.append(_Announcement(art_code=art_code, notice_date=published.isoformat(), title=title))
    return normalized, total_pages


def _fetch_metadata(
    code: str,
    as_of: date,
    *,
    session: Any,
    timeout: tuple[int, int],
    rate_limiter: Any,
) -> list[_Announcement]:
    cutoff = as_of - timedelta(days=PATCH4_MAX_EVIDENCE_AGE_DAYS)
    seen: set[str] = set()
    relevant: list[_Announcement] = []
    previous_date: date | None = None
    total_pages: int | None = None
    reached_cutoff = False
    for page_index in range(1, MAX_METADATA_PAGES + 1):
        payload, _ = _request_json(
            ANNOUNCEMENT_LIST_ENDPOINT,
            _metadata_params(code, page_index),
            session=session,
            headers=_headers(code),
            timeout=timeout,
            rate_limiter=rate_limiter,
        )
        rows, declared_pages = _validate_metadata_page(payload, code=code, requested_page=page_index)
        if total_pages is None:
            total_pages = declared_pages
        elif total_pages != declared_pages:
            raise Patch4EvidenceError("announcement metadata pagination changed during acquisition")
        for row in rows:
            published = date.fromisoformat(row.notice_date)
            if previous_date is not None and published > previous_date:
                raise Patch4EvidenceError("announcement metadata is not in descending date order")
            previous_date = published
            if row.art_code in seen:
                raise Patch4EvidenceError("announcement metadata contains a duplicate art code")
            seen.add(row.art_code)
            if published < cutoff:
                reached_cutoff = True
                continue
            if published <= as_of and _RELEVANT_TITLE.search(row.title):
                relevant.append(row)
                if len(relevant) > MAX_DOCUMENTS:
                    raise Patch4EvidenceError("relevant announcement count exceeds the document limit")
        if page_index >= declared_pages or reached_cutoff:
            break
    else:  # pragma: no cover - the bounded range exits normally
        raise AssertionError("announcement metadata loop did not terminate")
    if total_pages and total_pages > MAX_METADATA_PAGES and not reached_cutoff:
        raise Patch4EvidenceError("announcement metadata scan is truncated by the page limit")
    return relevant


def _plan_identity(title: str) -> str | None:
    compact = _SPACE.sub("", title)
    matches = {(match.group("year"), match.group("kind")) for match in _PLAN_IDENTITY.finditer(compact)}
    if len(matches) != 1:
        return None
    phases = set(_PLAN_PHASE.findall(compact))
    if len(phases) > 1:
        return None
    year, kind = next(iter(matches))
    phase = next(iter(phases), "未分期")
    return f"{year}:{kind}:{phase}"


def _explicit_plan_references(text: str) -> set[tuple[str, str | None]]:
    compact = _SPACE.sub("", text)
    references: set[tuple[str, str | None]] = {
        (match.group("year"), match.group("kind")) for match in _PLAN_IDENTITY.finditer(compact)
    }
    references.update((match.group("year"), None) for match in _GENERIC_PLAN_IDENTITY.finditer(compact))
    return references


def _plan_kind_is_compatible(current_kind: str, referenced_kind: str | None) -> bool:
    if referenced_kind is None or referenced_kind == current_kind:
        return True
    if "员工持股" in {current_kind, referenced_kind}:
        return False
    return {current_kind, referenced_kind} != {"限制性股票", "股票期权"}


def _select_plan_group(
    announcements: Sequence[_Announcement],
) -> tuple[str, list[_Announcement]] | None:
    groups: dict[str, list[_Announcement]] = {}
    for announcement in announcements:
        plan_id = _plan_identity(announcement.title)
        if plan_id is not None:
            groups.setdefault(plan_id, []).append(announcement)
    if not groups:
        return None
    ranked = {
        plan_id: (
            int(plan_id[:4]),
            max(announcement.notice_date for announcement in members),
        )
        for plan_id, members in groups.items()
    }
    best_rank = max(ranked.values())
    selected_ids = [plan_id for plan_id, rank in ranked.items() if rank == best_rank]
    if len(selected_ids) != 1:
        return None
    selected_id = selected_ids[0]
    return selected_id, sorted(
        groups[selected_id],
        key=lambda announcement: (announcement.notice_date, announcement.art_code),
    )


def _plan_status(title: str, body: str, *, current_plan_id: str | None = None) -> str:
    current_plan_id = current_plan_id or _plan_identity(title)
    if _has_current_plan_termination(title, current_plan_id):
        return "inactive"
    for sentence in _sentences(body):
        if _has_current_plan_termination(sentence, current_plan_id):
            return "inactive"
    return "unrevoked"


def _masked_matches(text: str, pattern: re.Pattern[str]) -> str:
    """Preserve offsets while removing only the matched local clause.

    The denial/non-final patterns intentionally accept a little surrounding
    context, but that context must never consume a later, comma-separated
    decisive action in the same sentence.
    """

    characters = list(text)
    for match in pattern.finditer(text):
        clause_boundary = re.search(r"[，,；;。！？]", text[match.start() : match.end()])
        end = match.end() if clause_boundary is None else match.start() + clause_boundary.start()
        characters[match.start() : end] = " " * (end - match.start())
    return "".join(characters)


def _termination_targets_current_plan(
    text: str,
    match: re.Match[str],
    current_plan_id: str | None,
) -> bool:
    """Reject an explicit termination of a different historical plan."""

    if current_plan_id is None:
        return False
    current_year, current_kind, _phase = current_plan_id.split(":", 2)
    prefix = text[: match.start()]
    clause_start = max((prefix.rfind(marker) for marker in "，,；;。！？"), default=-1) + 1
    context = text[clause_start : match.end()]
    explicit_plan_refs = _explicit_plan_references(context)
    if any(
        year != current_year or not _plan_kind_is_compatible(current_kind, kind) for year, kind in explicit_plan_refs
    ):
        return False
    kinds = set(_EXPLICIT_PLAN_KIND.findall(context))
    if "员工持股" in kinds and current_kind != "员工持股":
        return False
    if current_kind == "员工持股" and kinds and "员工持股" not in kinds:
        return False
    if current_kind == "限制性股票" and "股票期权" in kinds and "限制性股票" not in kinds:
        return False
    if current_kind == "股票期权" and "限制性股票" in kinds and "股票期权" not in kinds:
        return False
    if _OLD_PLAN_MARKER.search(context) and not any(year == current_year for year, _kind in explicit_plan_refs):
        return False
    return True


def _sentence_can_describe_current_plan(sentence: str, current_plan_id: str) -> bool:
    """Fail closed when a scoring sentence explicitly names another plan."""

    current_year, current_kind, _phase = current_plan_id.split(":", 2)
    explicit_plan_refs = _explicit_plan_references(sentence)
    for year, kind in explicit_plan_refs:
        if year != current_year or not _plan_kind_is_compatible(current_kind, kind):
            return False
    referenced_kinds = {match.group(1) for match in _PLAN_KIND_REFERENCE.finditer(sentence)}
    if "员工持股" in referenced_kinds and current_kind != "员工持股":
        return False
    if current_kind == "员工持股" and referenced_kinds and "员工持股" not in referenced_kinds:
        return False
    if current_kind == "限制性股票" and "股票期权" in referenced_kinds and "限制性股票" not in referenced_kinds:
        return False
    if current_kind == "股票期权" and "限制性股票" in referenced_kinds and "股票期权" not in referenced_kinds:
        return False
    if _OLD_PLAN_REFERENCE.search(sentence) and not any(year == current_year for year, _kind in explicit_plan_refs):
        return False
    return True


def _has_current_plan_termination(text: str, current_plan_id: str | None) -> bool:
    candidate = _masked_matches(text, _NEGATED_PLAN_TERMINATION)
    candidate = _masked_matches(candidate, _NON_FINAL_PLAN_TERMINATION)
    matches = list(_INACTIVE_PLAN.finditer(candidate))
    if not matches:
        return False
    clauses = list(re.finditer(r"[^，,；;。！？]+", candidate))
    for match in matches:
        if not _termination_targets_current_plan(text, match, current_plan_id):
            continue
        clause_index = next(
            (index for index, clause in enumerate(clauses) if clause.start() <= match.start() < clause.end()),
            None,
        )
        if clause_index is None:
            continue
        clause = clauses[clause_index].group()
        if _CONDITIONAL_PLAN_TERMINATION.search(clause) or _PROCEDURAL_PLAN_TERMINATION.search(clause):
            continue
        neighborhood = candidate[
            clauses[max(0, clause_index - 1)].start() : clauses[min(len(clauses) - 1, clause_index + 1)].end()
        ]
        if _DECISIVE_PLAN_ACTION.search(clause) is None and (
            _CONDITIONAL_PLAN_TERMINATION.search(neighborhood) or _PROCEDURAL_PLAN_TERMINATION.search(neighborhood)
        ):
            continue
        return True
    return False


def _related_unidentified_termination_announcements(
    announcements: Sequence[_Announcement],
    selected_announcements: Sequence[_Announcement],
    current_plan_id: str,
) -> list[_Announcement]:
    """Attach only newer no-year notices that explicitly terminate the selected plan."""

    if not selected_announcements:
        return []
    earliest_selected_date = min(item.notice_date for item in selected_announcements)
    selected_art_codes = {item.art_code for item in selected_announcements}
    return sorted(
        [
            announcement
            for announcement in announcements
            if announcement.art_code not in selected_art_codes
            and announcement.notice_date >= earliest_selected_date
            and _plan_identity(announcement.title) is None
            and _has_current_plan_termination(announcement.title, current_plan_id)
        ],
        key=lambda announcement: (announcement.notice_date, announcement.art_code),
    )


def _content_params(art_code: str, page_index: int) -> dict[str, Any]:
    return {"art_code": art_code, "client_source": "web", "page_index": page_index}


def _detail_url(code: str, art_code: str) -> str:
    return f"{ANNOUNCEMENT_DETAIL_PREFIX}{code}/{art_code}.html"


def _content_headers(code: str, art_code: str) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": _detail_url(code, art_code),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
    }


def _validate_content_security(value: Any, code: str) -> None:
    if not isinstance(value, list) or len(value) != 1:
        raise Patch4EvidenceError("announcement body security bindings are invalid")
    matches = 0
    for item in value:
        if not isinstance(item, Mapping):
            raise Patch4EvidenceError("announcement body security binding is not an object")
        stock = item.get("stock")
        if not isinstance(stock, str) or not _A_SHARE_CODE.fullmatch(stock):
            raise Patch4EvidenceError("announcement body security code is invalid")
        matches += int(stock == code)
    if matches != 1:
        raise Patch4EvidenceError("announcement body is not uniquely bound to the requested security")


def _validate_attachment_url(value: str) -> None:
    parsed = urlsplit(value)
    if not _is_safe_https_url(value) or parsed.hostname.casefold() not in {"pdf.dfcfw.com", "data.eastmoney.com"}:
        raise Patch4EvidenceError("announcement attachment URL is not a pinned HTTPS source")


def _validate_attachments(data: Mapping[str, Any]) -> None:
    direct = data.get("attach_url_web")
    if direct not in (None, ""):
        if not isinstance(direct, str):
            raise Patch4EvidenceError("announcement attachment URL is not text")
        _validate_attachment_url(direct)
    attachments = data.get("attach_list")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS_PER_DOCUMENT:
        raise Patch4EvidenceError("announcement attachment count is invalid")
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            raise Patch4EvidenceError("announcement attachment is not an object")
        for key, value in attachment.items():
            if "url" in str(key).casefold() and value not in (None, ""):
                if not isinstance(value, str):
                    raise Patch4EvidenceError("announcement attachment URL is not text")
                _validate_attachment_url(value)


def _visible_body(value: Any) -> str:
    if not isinstance(value, str):
        raise Patch4EvidenceError("announcement body is not text")
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise Patch4EvidenceError("announcement body HTML is invalid") from exc
    text = "\n".join(_SPACE.sub(" ", part).strip() for part in "".join(parser.parts).splitlines())
    text = "\n".join(part for part in text.splitlines() if part)
    if not text:
        raise Patch4EvidenceError("announcement body is empty")
    return text


def _validate_content_page(
    payload: Mapping[str, Any],
    *,
    code: str,
    announcement: _Announcement,
    expected_page_size: int | None,
) -> tuple[str, int]:
    success = payload.get("success")
    if isinstance(success, bool) or success != 1:
        raise Patch4EvidenceError("announcement body source reported failure")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise Patch4EvidenceError("announcement body data is not an object")
    art_code = data.get("art_code")
    notice_date = _parse_notice_date(data.get("notice_date")).isoformat()
    notice_title = _clean_text(data.get("notice_title"), label="announcement body title", limit=500)
    page_size = data.get("page_size")
    if (
        art_code != announcement.art_code
        or notice_date != announcement.notice_date
        or notice_title != announcement.title
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MAX_BODY_PAGES_PER_DOCUMENT
        or (expected_page_size is not None and page_size != expected_page_size)
    ):
        raise Patch4EvidenceError("announcement body identity or page-size binding is invalid")
    _validate_content_security(data.get("security"), code)
    _validate_attachments(data)
    return _visible_body(data.get("notice_content")), page_size


def _fetch_document(
    code: str,
    announcement: _Announcement,
    *,
    session: Any,
    timeout: tuple[int, int],
    rate_limiter: Any,
    budget: _RequestBudget,
) -> tuple[str, dict[str, Any]]:
    pages: list[str] = []
    page_hashes: list[str] = []
    page_size: int | None = None
    page_index = 1
    while page_size is None or page_index <= page_size:
        if budget.body_pages >= MAX_TOTAL_BODY_PAGES:
            raise Patch4EvidenceError("announcement body-page budget exceeded")
        payload, raw_size = _request_json(
            ANNOUNCEMENT_CONTENT_ENDPOINT,
            _content_params(announcement.art_code, page_index),
            session=session,
            headers=_content_headers(code, announcement.art_code),
            timeout=timeout,
            rate_limiter=rate_limiter,
        )
        budget.consume(raw_size)
        body, declared_page_size = _validate_content_page(
            payload,
            code=code,
            announcement=announcement,
            expected_page_size=page_size,
        )
        page_size = declared_page_size
        page_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if page_hash in page_hashes:
            raise Patch4EvidenceError("announcement body pages contain duplicate content")
        pages.append(body)
        page_hashes.append(page_hash)
        page_index += 1
    combined = "\n\f\n".join(pages)
    if len(combined) > MAX_BODY_CHARACTERS_PER_DOCUMENT:
        raise Patch4EvidenceError("announcement body exceeds the character limit")
    content_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    document = {
        "art_code": announcement.art_code,
        "code": code,
        "as_of": announcement.notice_date,
        "title": announcement.title,
        "url": _detail_url(code, announcement.art_code),
        "page_size": page_size,
        "page_sha256": page_hashes,
        "content_sha256": content_hash,
        "content_length": len(combined),
    }
    return combined, document


def _sentences(body: str) -> list[str]:
    return [_SPACE.sub(" ", part).strip() for part in _SENTENCE_BREAK.split(body) if _SPACE.sub(" ", part).strip()]


def _one_explicit_percentage(
    sentence: str,
    anchor: re.Match[str],
    *,
    denominator: re.Pattern[str] | None = None,
) -> float | None:
    if _SPECULATIVE_PERCENT.search(sentence):
        return None
    matches = list(_PERCENT.finditer(sentence))
    if len(matches) != 1:
        return None
    percentage = matches[0]
    if not 0 <= percentage.start() - anchor.end() <= 40:
        return None
    between = sentence[anchor.end() : percentage.start()]
    if _DECLARATION.search(between) is None:
        return None
    if denominator is not None:
        denominator_matches = [
            match
            for match in denominator.finditer(sentence)
            if -4 <= match.start() - anchor.end() <= 4 and match.end() <= percentage.start()
        ]
        if not denominator_matches:
            return None
    value = float(percentage.group("value"))
    return value if 0 <= value <= 100 else None


def _nearby(first: re.Match[str], second: re.Match[str], *, distance: int = 32) -> bool:
    if first.end() <= second.start():
        return second.start() - first.end() <= distance
    if second.end() <= first.start():
        return first.start() - second.end() <= distance
    return True


def _explicit_relation(
    sentence: str, relation: re.Pattern[str], target: re.Pattern[str], *, distance: int = 32
) -> bool:
    for left in relation.finditer(sentence):
        for right in target.finditer(sentence):
            if not _nearby(left, right, distance=distance):
                continue
            gap_start = min(left.end(), right.end())
            gap_end = max(left.start(), right.start())
            if re.search(r"[，,；;。！？]", sentence[gap_start:gap_end]) is None:
                return True
    return False


def _bound_to_performance(sentence: str, metric: re.Pattern[str]) -> bool:
    """Require one direct metric-binding-performance phrase, not a nearby verb."""

    for clause in re.split(r"[，,；;。！？]", sentence):
        for metric_match in metric.finditer(clause):
            for performance_match in _PERFORMANCE.finditer(clause):
                if metric_match.end() <= performance_match.start():
                    between = clause[metric_match.end() : performance_match.start()]
                    metric_first = True
                elif performance_match.end() <= metric_match.start():
                    between = clause[performance_match.end() : metric_match.start()]
                    metric_first = False
                else:
                    # "股价考核" and "研发考核指标" contain the word
                    # "考核"; that overlap is not an independent binding.
                    continue
                if len(between) > 18 or _COORDINATED_CLAUSE.search(between):
                    continue
                for binding in _METRIC_BINDING.finditer(between):
                    before = between[: binding.start()]
                    after = between[binding.end() :]
                    if metric_first:
                        if len(before) <= 4 and len(after) <= 8:
                            return True
                    elif len(before) <= 8 and len(after) <= 4:
                        return True
    return False


def _frontline_equity_inclusion(sentence: str) -> bool:
    """Require the inclusion to name an equity-plan recipient in one clause."""

    for clause in re.split(r"[，,；;。！？]", sentence):
        recipient = _EQUITY_RECIPIENT.search(clause)
        frontline = _FRONTLINE_RD.search(clause)
        if (
            recipient
            and frontline
            and _EQUITY_PLAN.search(clause)
            and _nearby(recipient, frontline, distance=48)
            and _explicit_relation(clause, _INCLUSION, _FRONTLINE_RD)
        ):
            return True
    return False


def _frontline_equity_exclusion(sentence: str) -> bool:
    """Bind an exclusion to equity rights in the same punctuation clause."""

    return any(
        _EQUITY_PLAN.search(clause) and _explicit_relation(clause, _NEGATION, _FRONTLINE_RD)
        for clause in re.split(r"[，,；;。！？]", sentence)
    )


def _price_metric_negative_is_bound(sentence: str) -> bool:
    """Reject unrelated risk/training language that merely says "not set"."""

    for clause in re.split(r"[，,；;。！？]", sentence):
        price_matches = list(_PRICE_METRIC.finditer(clause))
        if not price_matches or not _explicit_relation(clause, _NEGATION, _PRICE_METRIC):
            continue
        if _EQUITY_PLAN.search(clause):
            return True
        for performance in _PERFORMANCE.finditer(clause):
            if all(performance.end() <= price.start() or performance.start() >= price.end() for price in price_matches):
                return True
    return False


def _classify_sentence(sentence: str) -> list[tuple[str, float | bool]]:
    facts: list[tuple[str, float | bool]] = []
    core = _CORE_ROLE.search(sentence)
    ownership = _OWNERSHIP.search(sentence)
    ownership_subject = (
        sentence[min(core.start(), ownership.start()) : max(core.end(), ownership.end())] if core and ownership else ""
    )
    if core and ownership and _nearby(core, ownership) and _MIXED_OWNERSHIP_SUBJECT.search(ownership_subject) is None:
        value = _one_explicit_percentage(
            sentence,
            ownership,
            denominator=_COMPANY_SHARE_DENOMINATOR,
        )
        if value is not None:
            facts.append(("core_rd_ownership_pct", value))
    coverage = _COVERAGE.search(sentence)
    if coverage:
        value = _one_explicit_percentage(
            sentence,
            coverage,
            denominator=_CORE_TALENT_DENOMINATOR,
        )
        if value is not None:
            facts.append(("esop_core_talent_coverage_pct", value))

    if _RD_METRIC.search(sentence) and _PERFORMANCE.search(sentence):
        negative = _explicit_relation(sentence, _NEGATION, _RD_METRIC)
        positive = (
            _explicit_relation(sentence, _MULTI_YEAR, _RD_METRIC, distance=48)
            and _bound_to_performance(sentence, _RD_METRIC)
            and not negative
        )
        if negative and not positive:
            facts.append(("long_term_rd_metrics", False))
        elif positive and not negative:
            facts.append(("long_term_rd_metrics", True))

    if _FRONTLINE_RD.search(sentence) and _EQUITY_PLAN.search(sentence):
        negative = _frontline_equity_exclusion(sentence)
        positive = _frontline_equity_inclusion(sentence)
        if negative and not positive:
            facts.append(("frontline_rd_equity", False))
        elif positive and not negative:
            facts.append(("frontline_rd_equity", True))

    # A grant/exercise/reference price describes consideration, not a vesting
    # condition.  It is ignored unless the same sentence explicitly names a
    # share-price performance metric.
    has_price_metric = _PRICE_METRIC.search(sentence) is not None
    if has_price_metric:
        negative = _price_metric_negative_is_bound(sentence)
        negative_is_bound_to_plan = negative
        positive = (
            _SHORT_HORIZON.search(sentence) is not None
            and _bound_to_performance(sentence, _PRICE_METRIC)
            and not negative
        )
        if negative_is_bound_to_plan and not positive:
            facts.append(("short_term_price_binding", False))
        elif positive and not negative and not (_GRANT_PRICE_ONLY.search(sentence) and sentence.count("股价") == 0):
            facts.append(("short_term_price_binding", True))
    return facts


def _bounded_snippet(sentence: str, *, limit: int = 260) -> str:
    clean = _SPACE.sub(" ", sentence).strip()
    return clean if len(clean) <= limit else f"{clean[: limit - 1]}…"


def _extract_facts(
    code: str,
    announcements: Sequence[_Announcement],
    bodies: Sequence[str],
    documents: Sequence[Mapping[str, Any]],
) -> list[_Fact]:
    if not announcements or len(announcements) != len(bodies) or len(announcements) != len(documents):
        return []
    plan_ids = {document.get("plan_id") for document in documents if isinstance(document, Mapping)}
    statuses = {document.get("plan_status") for document in documents if isinstance(document, Mapping)}
    if (
        len(plan_ids) != 1
        or None in plan_ids
        or not all(isinstance(plan_id, str) and _CANONICAL_PLAN_ID.fullmatch(plan_id) for plan_id in plan_ids)
        or "inactive" in statuses
        or statuses != {"unrevoked"}
    ):
        return []

    facts: list[_Fact] = []
    current_plan_id = next(iter(plan_ids))
    for announcement, body, document in zip(announcements, bodies, documents, strict=True):
        for sentence in _sentences(body):
            if not _sentence_can_describe_current_plan(sentence, current_plan_id):
                continue
            for key, value in _classify_sentence(sentence):
                facts.append(
                    _Fact(
                        key=key,
                        value=value,
                        announcement=announcement,
                        detail_url=_detail_url(code, announcement.art_code),
                        content_sha256=str(document["content_sha256"]),
                        snippet=_bounded_snippet(sentence),
                    )
                )
    return facts


def _fact_evidence(code: str, fact: _Fact) -> dict[str, str]:
    evidence_id = f"eastmoney-notice:{code}:{fact.announcement.art_code}:sha256:{fact.content_sha256[:16]}"
    return {
        "source": "东方财富上市公司公告正文",
        "evidence_id": evidence_id,
        "url": fact.detail_url,
        "as_of": fact.announcement.notice_date,
        "summary": f"公告正文明确陈述：{fact.snippet}（正文SHA-256前16位：{fact.content_sha256[:16]}）",
    }


def _build_atomic_result(
    code: str,
    as_of: date,
    facts: Sequence[_Fact],
    documents: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, str]:
    diagnostics: dict[str, dict[str, Any]] = {}
    criteria: dict[str, dict[str, Any]] = {}
    incomplete: list[str] = []
    for key in _CRITERIA:
        candidates = [fact for fact in facts if fact.key == key]
        if not candidates:
            diagnostics[key] = {
                "status": "unknown",
                "reason": "no_direct_explicit_statement",
                "value": None,
                "evidence_id": None,
                "documents_checked": len(documents),
            }
            incomplete.append(key)
            continue
        latest_date = max(fact.announcement.notice_date for fact in candidates)
        current_candidates = [fact for fact in candidates if fact.announcement.notice_date == latest_date]
        current_values = {fact.value for fact in current_candidates}
        if len(current_values) != 1:
            diagnostics[key] = {
                "status": "unknown",
                "reason": "conflicting_direct_statements",
                "value": None,
                "evidence_id": None,
                "documents_checked": len(documents),
            }
            incomplete.append(key)
            continue
        chosen = max(
            current_candidates,
            key=lambda fact: (fact.announcement.notice_date, fact.announcement.art_code),
        )
        evidence = _fact_evidence(code, chosen)
        value: float | bool = float(chosen.value) if key in _PERCENT_CRITERIA else bool(chosen.value)
        diagnostics[key] = {
            "status": "known",
            "reason": "direct_explicit_statement",
            "value": value,
            "evidence_id": evidence["evidence_id"],
            "documents_checked": len(documents),
        }
        criteria[key] = {"value": value, "evidence": evidence}
    if incomplete:
        return diagnostics, None, f"incomplete_atomic_facts:{','.join(incomplete)}"
    raw = {
        "schema_version": PATCH4_SCHEMA_VERSION,
        "model_id": PATCH4_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "criteria": criteria,
    }
    try:
        assessment = normalise_patch4_assessment(raw, security_code=code, as_of=as_of.isoformat())
    except QualityEquityError as exc:
        raise Patch4EvidenceError(f"derived Patch 4 assessment is invalid: {exc}") from exc
    return diagnostics, assessment, ""


def _unknown_diagnostics(reason: str, *, documents_checked: int = 0) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "status": "unknown",
            "reason": reason,
            "value": None,
            "evidence_id": None,
            "documents_checked": documents_checked,
        }
        for key in _CRITERIA
    }


def _validate_document(value: Any, *, code: str, as_of: date) -> dict[str, Any]:
    fields = {
        "art_code",
        "code",
        "as_of",
        "title",
        "url",
        "plan_id",
        "plan_status",
        "page_size",
        "page_sha256",
        "content_sha256",
        "content_length",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise Patch4EvidenceError("cached Patch 4 document schema is invalid")
    art_code = value.get("art_code")
    evidence_date = _parse_notice_date(value.get("as_of"))
    title = _clean_text(value.get("title"), label="cached announcement title", limit=500)
    page_size = value.get("page_size")
    page_hashes = value.get("page_sha256")
    content_hash = value.get("content_sha256")
    content_length = value.get("content_length")
    plan_id = value.get("plan_id")
    plan_status = value.get("plan_status")
    title_plan_id = _plan_identity(title)
    title_binds_selected_plan = bool(
        title_plan_id == plan_id
        or (
            title_plan_id is None
            and plan_status == "inactive"
            and isinstance(plan_id, str)
            and _has_current_plan_termination(title, plan_id)
        )
    )
    if (
        value.get("code") != code
        or not isinstance(art_code, str)
        or not _ART_CODE.fullmatch(art_code)
        or evidence_date > as_of
        or (as_of - evidence_date).days > PATCH4_MAX_EVIDENCE_AGE_DAYS
        or value.get("url") != _detail_url(code, art_code)
        or not isinstance(plan_id, str)
        or not _CANONICAL_PLAN_ID.fullmatch(plan_id)
        or not title_binds_selected_plan
        or plan_status not in {"unrevoked", "inactive"}
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MAX_BODY_PAGES_PER_DOCUMENT
        or not isinstance(page_hashes, list)
        or len(page_hashes) != page_size
        or len(set(page_hashes)) != len(page_hashes)
        or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in page_hashes)
        or not isinstance(content_hash, str)
        or not _SHA256.fullmatch(content_hash)
        or isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or not 1 <= content_length <= MAX_BODY_CHARACTERS_PER_DOCUMENT
    ):
        raise Patch4EvidenceError("cached Patch 4 document identity is invalid")
    return {
        "art_code": art_code,
        "code": code,
        "as_of": evidence_date.isoformat(),
        "title": title,
        "url": value["url"],
        "plan_id": plan_id,
        "plan_status": plan_status,
        "page_size": page_size,
        "page_sha256": list(page_hashes),
        "content_sha256": content_hash,
        "content_length": content_length,
    }


def _validate_diagnostics(
    value: Any,
    *,
    code: str,
    assessment: Mapping[str, Any] | None,
    document_count: int,
    allowed_evidence_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(_CRITERIA):
        raise Patch4EvidenceError("cached Patch 4 diagnostics schema is invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for key in _CRITERIA:
        item = value.get(key)
        if not isinstance(item, Mapping) or set(item) != {
            "status",
            "reason",
            "value",
            "evidence_id",
            "documents_checked",
        }:
            raise Patch4EvidenceError("cached Patch 4 diagnostic item is invalid")
        status = item.get("status")
        reason = item.get("reason")
        checked = item.get("documents_checked")
        if (
            status not in {"known", "unknown"}
            or not isinstance(reason, str)
            or not reason
            or isinstance(checked, bool)
            or not isinstance(checked, int)
            or not 0 <= checked <= document_count
        ):
            raise Patch4EvidenceError("cached Patch 4 diagnostic fields are invalid")
        raw_value = item.get("value")
        evidence_id = item.get("evidence_id")
        if status == "unknown":
            if raw_value is not None or evidence_id is not None:
                raise Patch4EvidenceError("unknown Patch 4 diagnostic contains a value")
        else:
            if key in _PERCENT_CRITERIA:
                valid_value = (
                    not isinstance(raw_value, bool)
                    and isinstance(raw_value, (int, float))
                    and math.isfinite(float(raw_value))
                    and 0 <= float(raw_value) <= 100
                )
            else:
                valid_value = isinstance(raw_value, bool)
            bound_id = bool(
                isinstance(evidence_id, str)
                and re.search(rf"(?:^|[^0-9]){re.escape(code)}(?:[^0-9]|$)", evidence_id)
                and evidence_id in allowed_evidence_ids
            )
            if not valid_value or not bound_id:
                raise Patch4EvidenceError("known Patch 4 diagnostic value or evidence binding is invalid")
            if assessment is not None:
                criterion = assessment["criteria"][key]
                expected_value = criterion["value"]
                expected_id = criterion["evidence"]["evidence_id"]
                if raw_value != expected_value or evidence_id != expected_id:
                    raise Patch4EvidenceError("known Patch 4 diagnostic is not bound to its assessment")
        normalized[key] = dict(item)
    return normalized


def _make_evidence(
    code: str,
    as_of: date,
    *,
    assessment: Any,
    diagnostics: Any,
    documents: Any,
    cache_hit: bool,
    cache_diagnostic: str,
    reason: str,
) -> Patch4Evidence:
    if not isinstance(documents, list) or len(documents) > MAX_DOCUMENTS:
        raise Patch4EvidenceError("Patch 4 document collection is invalid")
    normalized_documents = [_validate_document(item, code=code, as_of=as_of) for item in documents]
    art_codes = [item["art_code"] for item in normalized_documents]
    if len(set(art_codes)) != len(art_codes):
        raise Patch4EvidenceError("Patch 4 document collection contains duplicates")
    plan_ids = {item["plan_id"] for item in normalized_documents}
    if len(plan_ids) > 1:
        raise Patch4EvidenceError("Patch 4 document collection mixes different incentive plans")
    normalized_assessment: dict[str, Any] | None
    if assessment is None:
        normalized_assessment = None
    else:
        try:
            normalized_assessment = normalise_patch4_assessment(
                assessment,
                security_code=code,
                as_of=as_of.isoformat(),
            )
        except QualityEquityError as exc:
            raise Patch4EvidenceError(f"Patch 4 assessment contract is invalid: {exc}") from exc
    allowed_evidence_ids = {
        f"eastmoney-notice:{code}:{item['art_code']}:sha256:{item['content_sha256'][:16]}"
        for item in normalized_documents
    }
    document_by_evidence_id = {
        f"eastmoney-notice:{code}:{item['art_code']}:sha256:{item['content_sha256'][:16]}": item
        for item in normalized_documents
    }
    if normalized_assessment is not None:
        if len(plan_ids) != 1 or any(item["plan_status"] != "unrevoked" for item in normalized_documents):
            raise Patch4EvidenceError("Patch 4 assessment is not bound to one unrevoked plan")
        for criterion in normalized_assessment["criteria"].values():
            evidence = criterion["evidence"]
            document = document_by_evidence_id.get(evidence["evidence_id"])
            if (
                document is None
                or evidence["source"] != "东方财富上市公司公告正文"
                or evidence["url"] != document["url"]
                or evidence["as_of"] != document["as_of"]
                or f"正文SHA-256前16位：{document['content_sha256'][:16]}" not in evidence["summary"]
            ):
                raise Patch4EvidenceError("Patch 4 assessment evidence is not bound to a cached body hash")
    normalized_diagnostics = _validate_diagnostics(
        diagnostics,
        code=code,
        assessment=normalized_assessment,
        document_count=len(normalized_documents),
        allowed_evidence_ids=allowed_evidence_ids,
    )
    complete = all(item["status"] == "known" for item in normalized_diagnostics.values())
    if complete != (normalized_assessment is not None):
        raise Patch4EvidenceError("Patch 4 completeness and assessment disagree")
    if not isinstance(reason, str) or (complete and reason) or (not complete and not reason):
        raise Patch4EvidenceError("Patch 4 availability reason is invalid")
    return Patch4Evidence(
        available=complete,
        code=code,
        as_of=as_of.isoformat(),
        model_id=MODEL_ID,
        assessment=normalized_assessment,
        criteria=normalized_diagnostics,
        status="complete"
        if complete
        else ("source_unavailable" if reason.startswith(("source_unavailable:", "worker_failure:")) else "incomplete"),
        documents=normalized_documents,
        cache_hit=cache_hit,
        cache_diagnostic=cache_diagnostic,
        reason=reason,
    )


def validate_patch4_evidence_record(
    value: Any,
    code: str,
    as_of: date | str,
) -> dict[str, Any]:
    """Replay and normalize every identity, document, hash, and assessment binding."""

    normalized_code = _normalise_code(code)
    cutoff = _parse_as_of(as_of)
    fields = {
        "available",
        "code",
        "as_of",
        "model_id",
        "assessment",
        "criteria",
        "status",
        "documents",
        "cache_hit",
        "cache_diagnostic",
        "reason",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise Patch4EvidenceError("Patch 4 evidence record shape is invalid")
    cache_diagnostic = value.get("cache_diagnostic")
    reason = value.get("reason")
    if (
        value.get("code") != normalized_code
        or value.get("as_of") != cutoff.isoformat()
        or value.get("model_id") != MODEL_ID
        or not isinstance(value.get("available"), bool)
        or not isinstance(value.get("cache_hit"), bool)
        or not isinstance(cache_diagnostic, str)
        or len(cache_diagnostic) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in cache_diagnostic)
        or not isinstance(reason, str)
        or len(reason) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in reason)
    ):
        raise Patch4EvidenceError("Patch 4 evidence record identity or diagnostics are invalid")
    rebuilt = _make_evidence(
        normalized_code,
        cutoff,
        assessment=value.get("assessment"),
        diagnostics=value.get("criteria"),
        documents=value.get("documents"),
        cache_hit=value["cache_hit"],
        cache_diagnostic=cache_diagnostic,
        reason=reason,
    )
    normalized = rebuilt.to_dict()
    if (
        value.get("available") is not rebuilt.available
        or value.get("status") != rebuilt.status
        or (rebuilt.status == "source_unavailable" and rebuilt.cache_hit)
        or dict(value) != normalized
    ):
        raise Patch4EvidenceError("Patch 4 evidence record contradicts its replayed evidence")
    return normalized


def _cache_contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "rules_version": RULES_VERSION,
        "assessment_model_id": PATCH4_MODEL_ID,
        "assessment_schema_version": PATCH4_SCHEMA_VERSION,
        "code": code,
        "as_of": as_of.isoformat(),
        "list_source": ANNOUNCEMENT_LIST_ENDPOINT,
        "content_source": ANNOUNCEMENT_CONTENT_ENDPOINT,
        "page_size": PAGE_SIZE,
        "max_metadata_pages": MAX_METADATA_PAGES,
        "max_documents": MAX_DOCUMENTS,
        "max_body_pages_per_document": MAX_BODY_PAGES_PER_DOCUMENT,
        "max_total_body_pages": MAX_TOTAL_BODY_PAGES,
        "max_age_days": PATCH4_MAX_EVIDENCE_AGE_DAYS,
    }


def _cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{MODEL_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _from_cache(payload: Any, code: str, as_of: date) -> Patch4Evidence:
    if not isinstance(payload, Mapping) or set(payload) != {
        "contract",
        "assessment",
        "diagnostics",
        "documents",
        "reason",
    }:
        raise Patch4EvidenceError("Patch 4 cache payload shape is invalid")
    if payload.get("contract") != _cache_contract(code, as_of):
        raise Patch4EvidenceError("Patch 4 cache contract mismatch")
    result = _make_evidence(
        code,
        as_of,
        assessment=payload.get("assessment"),
        diagnostics=payload.get("diagnostics"),
        documents=payload.get("documents"),
        cache_hit=True,
        cache_diagnostic="hit",
        reason=payload.get("reason"),
    )
    if result.status == "source_unavailable":
        raise Patch4EvidenceError("transient Patch 4 failure cannot be replayed from cache")
    return result


def _validate_timeout(timeout: tuple[int, int]) -> None:
    if (
        not isinstance(timeout, tuple)
        or len(timeout) != 2
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 60
            for value in timeout
        )
    ):
        raise ValueError("timeout must contain positive connect/read seconds no greater than 60")


def _save_cache(
    cache: SafeFileCache,
    initial: Any,
    payload: Mapping[str, Any],
    result: Patch4Evidence,
    diagnostic: str,
) -> Patch4Evidence:
    expected_hash = None
    if initial is not None and isinstance(initial.metadata, Mapping):
        candidate = initial.metadata.get("payload_sha256")
        if isinstance(candidate, str) and _SHA256.fullmatch(candidate):
            expected_hash = candidate
    try:
        cache.compare_and_swap(
            payload,
            expected_payload_sha256=expected_hash,
            allow_replace_invalid=True,
        )
        return replace(result, cache_diagnostic=f"{diagnostic};saved")
    except SafeCacheConflict:
        winner = cache.load()
        if winner.hit:
            try:
                return replace(
                    _from_cache(winner.value, result.code, date.fromisoformat(result.as_of)),
                    cache_diagnostic="race_winner",
                )
            except Patch4EvidenceError:
                pass
        return replace(result, cache_diagnostic=f"{diagnostic};write_conflict")
    except SafeCacheError as exc:
        return replace(result, cache_diagnostic=f"{diagnostic};write_failed:{_error_label(exc)}")


def fetch_patch4_evidence(
    code: str,
    as_of: date | str,
    *,
    session: Any = requests,
    cache_dir: str | Path = PATCH4_EVIDENCE_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> Patch4Evidence:
    """Fetch and validate five atomic Patch 4 facts for one A-share security."""

    if session is requests:
        session = thread_local_session()
    normalized_code = _normalise_code(code)
    cutoff = _parse_as_of(as_of)
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
    _validate_timeout(timeout)

    cache: SafeFileCache | None = None
    initial = None
    diagnostic = "disabled"
    if use_cache:
        cache = SafeFileCache(
            _cache_path(normalized_code, cutoff, Path(cache_dir)),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=cache_ttl_seconds,
            max_uncompressed_bytes=4 * 1024 * 1024,
        )
        initial = cache.load()
        if initial.hit:
            try:
                return _from_cache(initial.value, normalized_code, cutoff)
            except Patch4EvidenceError as exc:
                diagnostic = f"invalid_hit:{_error_label(exc)}"
        else:
            diagnostic = f"miss:{initial.reason}"

    active_session = requests.Session() if session is requests else session
    owns_session = active_session is not session
    documents: list[dict[str, Any]] = []
    try:
        announcements = _fetch_metadata(
            normalized_code,
            cutoff,
            session=active_session,
            timeout=timeout,
            rate_limiter=rate_limiter,
        )
        selected_plan = _select_plan_group(announcements)
        if selected_plan is None:
            reason = "incomplete_plan_identity:no_unique_current_plan"
            diagnostics = _unknown_diagnostics(reason)
            assessment = None
        else:
            plan_id, selected_announcements = selected_plan
            selected_announcements = sorted(
                [
                    *selected_announcements,
                    *_related_unidentified_termination_announcements(
                        announcements,
                        selected_announcements,
                        plan_id,
                    ),
                ],
                key=lambda announcement: (announcement.notice_date, announcement.art_code),
            )
            bodies = []
            budget = _RequestBudget()
            for announcement in selected_announcements:
                body, document = _fetch_document(
                    normalized_code,
                    announcement,
                    session=active_session,
                    timeout=timeout,
                    rate_limiter=rate_limiter,
                    budget=budget,
                )
                document["plan_id"] = plan_id
                document["plan_status"] = _plan_status(
                    announcement.title,
                    body,
                    current_plan_id=plan_id,
                )
                bodies.append(body)
                documents.append(document)
            facts = _extract_facts(
                normalized_code,
                selected_announcements,
                bodies,
                documents,
            )
            diagnostics, assessment, reason = _build_atomic_result(
                normalized_code,
                cutoff,
                facts,
                documents,
            )
    except Exception as exc:
        reason = f"source_unavailable:{_error_label(exc)}"
        diagnostics = _unknown_diagnostics(reason, documents_checked=len(documents))
        assessment = None
    finally:
        if owns_session:
            active_session.close()

    result = _make_evidence(
        normalized_code,
        cutoff,
        assessment=assessment,
        diagnostics=diagnostics,
        documents=documents,
        cache_hit=False,
        cache_diagnostic=diagnostic,
        reason=reason,
    )
    if cache is None or result.status == "source_unavailable":
        return result
    payload = {
        "contract": _cache_contract(normalized_code, cutoff),
        "assessment": assessment,
        "diagnostics": diagnostics,
        "documents": documents,
        "reason": reason,
    }
    return _save_cache(cache, initial, payload, result, diagnostic)


def fetch_patch4_evidence_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = MAX_WORKERS,
    session_factory: Callable[[], Any] = requests.Session,
    cache_dir: str | Path = PATCH4_EVIDENCE_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
    progress_cb: Any = None,
) -> dict[str, dict[str, Any]]:
    """Fetch a deterministic, bounded Patch 4 batch with one session per worker."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("Patch 4 batch requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("Patch 4 batch exceeds the company limit")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_WORKERS:
        raise ValueError(f"max_workers must be between 1 and {MAX_WORKERS}")
    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    _validate_timeout(timeout)
    prepared: list[tuple[str, str]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {"code", "as_of"}:
            raise ValueError("Patch 4 batch request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_as_of(request.get("as_of")).isoformat()
        if code in seen:
            raise ValueError(f"Patch 4 batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, cutoff))
    prepared.sort()
    if not prepared:
        return {}

    def worker(code: str, cutoff: str) -> Patch4Evidence:
        session = session_factory()
        try:
            return fetch_patch4_evidence(
                code,
                cutoff,
                session=session,
                cache_dir=cache_dir,
                cache_ttl_seconds=cache_ttl_seconds,
                use_cache=use_cache,
                timeout=timeout,
                rate_limiter=rate_limiter,
            )
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    def unavailable(code: str, cutoff: str, reason: str) -> Patch4Evidence:
        return _make_evidence(
            code,
            date.fromisoformat(cutoff),
            assessment=None,
            diagnostics=_unknown_diagnostics(reason),
            documents=[],
            cache_hit=False,
            cache_diagnostic="",
            reason=reason,
        )

    def is_global_source_failure(result: Patch4Evidence) -> bool:
        return bool(result.status == "source_unavailable" and _BATCH_GLOBAL_SOURCE_FAILURE.search(result.reason))

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(prepared))) as executor:
        completed = 0
        for wave_start in range(0, len(prepared), max_workers):
            wave = prepared[wave_start : wave_start + max_workers]
            futures = [executor.submit(worker, code, cutoff) for code, cutoff in wave]
            wave_results: list[Patch4Evidence] = []
            for (code, cutoff), future in zip(wave, futures):
                try:
                    result = future.result()
                except Exception as exc:
                    reason = f"worker_failure:{_error_label(exc)}"
                    result = unavailable(code, cutoff, reason)
                wave_results.append(result)
                results[code] = result.to_dict()
                completed += 1
                if progress_cb:
                    progress_cb(completed, len(prepared))
            remaining_start = wave_start + len(wave)
            if (
                len(wave_results) >= BATCH_SOURCE_FAILURE_LIMIT
                and all(is_global_source_failure(result) for result in wave_results)
                and remaining_start < len(prepared)
            ):
                circuit_reason = f"source_unavailable:batch_circuit_open:{len(wave_results)}_source_failures"
                for code, cutoff in prepared[remaining_start:]:
                    results[code] = unavailable(code, cutoff, circuit_reason).to_dict()
                    completed += 1
                    if progress_cb:
                        progress_cb(completed, len(prepared))
                break
    return {code: results[code] for code, _ in prepared}


__all__ = [
    "ANNOUNCEMENT_CONTENT_ENDPOINT",
    "ANNOUNCEMENT_DETAIL_PREFIX",
    "ANNOUNCEMENT_LIST_ENDPOINT",
    "CACHE_SCHEMA_VERSION",
    "BATCH_SOURCE_FAILURE_LIMIT",
    "MAX_BATCH_COMPANIES",
    "MAX_BODY_PAGES_PER_DOCUMENT",
    "MAX_DOCUMENTS",
    "MAX_METADATA_PAGES",
    "MAX_RESPONSE_BYTES",
    "MAX_WORKERS",
    "MODEL_ID",
    "PAGE_SIZE",
    "PATCH4_EVIDENCE_CACHE_DIR",
    "Patch4Evidence",
    "Patch4EvidenceError",
    "fetch_patch4_evidence",
    "fetch_patch4_evidence_batch",
    "validate_patch4_evidence_record",
]
