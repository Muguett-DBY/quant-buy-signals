"""Bounded, auditable research-report evidence for the Type 7 screen.

Eastmoney's metadata API identifies candidate reports.  The pinned public
detail pages are then read in memory and cross-checked; report prose is never
written to cache, audit output, mobile snapshots, or repository artifacts.
Only bounded identity/structure summaries, hashes, atomic numeric facts, and
one normalized cross-report fact consensus are retained.  Every source
failure fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
import hashlib
import html
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from config import CACHE_DIRECTORY, CACHE_TTL_SECONDS
from data.cache import SafeCacheConflict, SafeCacheError, SafeFileCache
from engine.quality_equity import (
    MAX_RESEARCH_SOURCES,
    MAX_RESEARCH_BODY_CHARACTERS,
    MAX_RESEARCH_BODY_FETCHES,
    MAX_RESEARCH_FACTS_PER_BODY,
    MIN_RESEARCH_SOURCES,
    MIN_CROSSCHECK_REPORTS,
    MIN_RESEARCH_BODY_CHARACTERS,
    MIN_RESEARCH_BODY_SOURCES,
    RESEARCH_CONTENT_IDENTITY_CHECKS,
    RESEARCH_CONTENT_MODEL_ID,
    RESEARCH_FACT_RELATIVE_TOLERANCE,
    RESEARCH_EVIDENCE_MODEL_ID,
    RESEARCH_MAX_AGE_DAYS,
    RESEARCH_RECENT_AGE_DAYS,
    QualityEquityError,
    normalise_research_content_verification,
    normalise_research_sources,
    research_metadata_precheck,
)


MODEL_ID = RESEARCH_EVIDENCE_MODEL_ID
EASTMONEY_REPORT_ENDPOINT = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_REPORT_PAGE = "https://data.eastmoney.com/report/stock.jshtml"
EASTMONEY_REPORT_DETAIL_PREFIX = "https://data.eastmoney.com/report/info/"
RESEARCH_REPORT_CACHE_DIR = CACHE_DIRECTORY / "research_reports"

PAGE_SIZE = 50
MAX_PAGES = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DETAIL_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_BATCH_COMPANIES = 2_000
MAX_WORKERS = 2
CACHE_SCHEMA_VERSION = 4
REQUEST_TIMEOUT = (15, 30)
REQUEST_ATTEMPTS = 2
REQUEST_INTERVAL_SECONDS = 0.25
RETRY_BACKOFF_SECONDS = 0.5

_A_SHARE_CODE = re.compile(r"^[036][0-9]{5}$")
_CANONICAL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_REPORT_ID = re.compile(r"^AP[0-9]{18}$")
_PUBLISHER_ID = re.compile(r"^[0-9]{8}$")
_REPORT_DATE = re.compile(r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})(?:[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?)?$")
_TOP_LEVEL_FIELDS = {"TotalPage", "currentYear", "data", "hits", "pageNo", "size"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": EASTMONEY_REPORT_PAGE,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "identity",
}


class ResearchReportError(RuntimeError):
    """The report source, response, cache, or identity contract failed."""


@dataclass(frozen=True)
class ResearchReportEvidence:
    available: bool
    code: str
    as_of: str
    model_id: str
    sources: list[dict[str, str]]
    distinct_publishers: int
    content_verification: dict[str, Any]
    cache_hit: bool
    cache_diagnostic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _RequestRateLimiter:
    """Reserve globally spaced request slots across batch worker threads."""

    def __init__(self, interval_seconds: float) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("request interval must be a non-negative finite number")
        self._interval_seconds = float(interval_seconds)
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval_seconds
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


_GLOBAL_RATE_LIMITER = _RequestRateLimiter(REQUEST_INTERVAL_SECONDS)


def _error_label(exc: BaseException, *, limit: int = 180) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}:{message[:limit]}" if message else type(exc).__name__


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if not _A_SHARE_CODE.fullmatch(code):
        raise ValueError("research-report code must be a Shanghai/Shenzhen six-digit code")
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
    if parsed > date.today():
        raise ValueError("as_of cannot be in the future")
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResearchReportError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ResearchReportError(f"report response contains non-finite JSON: {value}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise ResearchReportError("report response is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ResearchReportError("report response is not valid JSON") from exc


def _read_bounded_response(response: Any) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > MAX_RESPONSE_BYTES:
            raise ResearchReportError("report response exceeds the declared byte limit")
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ResearchReportError("report response is not JSON")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise ResearchReportError("report response does not support bounded streaming")
    chunks: list[bytes] = []
    received = 0
    for chunk in iterator(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ResearchReportError("report response yielded non-byte content")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise ResearchReportError("report response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_bounded_detail_response(response: Any) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    declared = headers.get("Content-Length") if hasattr(headers, "get") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > MAX_DETAIL_RESPONSE_BYTES:
            raise ResearchReportError("report detail exceeds the declared byte limit")
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "text/html":
        raise ResearchReportError("report detail is not HTML")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise ResearchReportError("report detail does not support bounded streaming")
    chunks: list[bytes] = []
    received = 0
    for chunk in iterator(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ResearchReportError("report detail yielded non-byte content")
        received += len(chunk)
        if received > MAX_DETAIL_RESPONSE_BYTES:
            raise ResearchReportError("report detail exceeds the byte limit")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8-sig").encode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResearchReportError("report detail is not UTF-8") from exc


class _ReportBodyParser(HTMLParser):
    """Extract only visible paragraph text from the pinned ``ctx-content`` div."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._content_depth = 0
        self._paragraph: list[str] | None = None
        self._suppressed_depth = 0
        self.paragraphs: list[str] = []
        self.content_divs = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._content_depth == 0 and tag == "div" and attributes.get("id") == "ctx-content":
            self._content_depth = 1
            self.content_divs += 1
            return
        if self._content_depth == 0:
            return
        if tag == "div":
            self._content_depth += 1
        if tag in {"script", "style"}:
            self._suppressed_depth += 1
        elif tag == "p":
            if self._paragraph is not None:
                raise ResearchReportError("report detail contains nested body paragraphs")
            self._paragraph = []
        elif tag == "br" and self._paragraph is not None:
            self._paragraph.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._content_depth == 0:
            return
        if tag in {"script", "style"} and self._suppressed_depth:
            self._suppressed_depth -= 1
        elif tag == "p" and self._paragraph is not None:
            paragraph = _normalise_body_text("".join(self._paragraph))
            if paragraph:
                self.paragraphs.append(paragraph)
            self._paragraph = None
        if tag == "div":
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._content_depth and not self._suppressed_depth and self._paragraph is not None:
            self._paragraph.append(data)


def _normalise_body_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value).replace("\u200b", "").replace("\ufeff", "")
    return " ".join(text.split())


def _extract_zwinfo(page: str) -> Mapping[str, Any]:
    markers = list(re.finditer(r"\bvar\s+zwinfo\s*=\s*", page))
    if len(markers) != 1:
        raise ResearchReportError("report detail does not contain one canonical zwinfo object")
    try:
        payload, consumed = json.JSONDecoder(object_pairs_hook=_unique_json_object).raw_decode(
            page,
            markers[0].end(),
        )
    except (json.JSONDecodeError, ResearchReportError) as exc:
        raise ResearchReportError("report detail zwinfo is not strict JSON") from exc
    if not isinstance(payload, Mapping) or not page[consumed:].lstrip().startswith(";"):
        raise ResearchReportError("report detail zwinfo is malformed")
    return payload


_PERIOD_PATTERNS = (
    re.compile(r"(?P<year>20[0-9]{2}|[0-9]{2})\s*Q\s*(?P<quarter>[1-4])", re.IGNORECASE),
    re.compile(r"(?P<year>20[0-9]{2}|[0-9]{2})\s*年?\s*第?\s*(?P<quarter>[一二三四1-4])\s*(?:季度|季报)"),
)
_QUARTER_NUMBER = {"一": "1", "二": "2", "三": "3", "四": "4"}
_FACT_PATTERNS = {
    "adjusted_net_profit": re.compile(r"(?:扣非归母净利润|扣非净利润)"),
    "operating_cash_flow": re.compile(r"经营活动产生的现金流量净额|经营现金流(?:量)?净额"),
    "parent_net_profit": re.compile(r"归属于上市公司股东的净利润|归母净利润|归母净利"),
    "revenue": re.compile(r"营业总收入|营业收入|营收|公司实现收入|实现收入"),
    "eps": re.compile(r"(?:基本)?每股收益|EPS", re.IGNORECASE),
}
_FACT_VALUE = re.compile(r"^[^0-9+\-]{0,18}(?P<value>[+\-]?[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>亿元|万元|元/股)")
_FORECAST_WORDS = ("预计", "预测", "预期", "盈利预测", "目标价")


def _periods(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for pattern in _PERIOD_PATTERNS:
        for match in pattern.finditer(text):
            year = int(match.group("year"))
            if year < 100:
                year += 2000
            quarter = _QUARTER_NUMBER.get(match.group("quarter"), match.group("quarter"))
            found.append((match.start(), f"{year:04d}Q{quarter}"))
    return [period for _, period in sorted(found)]


def _extract_key_facts(paragraphs: Sequence[str]) -> dict[tuple[str, str], float]:
    facts: dict[tuple[str, str], float] = {}
    conflicts: set[tuple[str, str]] = set()
    for paragraph in paragraphs:
        current_period: str | None = None
        sentences = [segment.strip() for segment in re.split(r"[。；;!?！？]", paragraph) if segment.strip()]
        for sentence in sentences:
            sentence_periods = _periods(sentence)
            if sentence_periods:
                current_period = sentence_periods[-1]
            if current_period is None or any(word in sentence for word in _FORECAST_WORDS):
                continue
            for metric, pattern in _FACT_PATTERNS.items():
                for match in pattern.finditer(sentence):
                    value_match = _FACT_VALUE.match(sentence[match.end() : match.end() + 48])
                    if value_match is None:
                        continue
                    value = float(value_match.group("value"))
                    raw_unit = value_match.group("unit")
                    if raw_unit == "万元":
                        value /= 10_000.0
                        unit = "CNY_100M"
                    elif raw_unit == "亿元":
                        unit = "CNY_100M"
                    else:
                        unit = "CNY_PER_SHARE"
                    key = (f"{current_period}:{metric}", unit)
                    if key in facts and not math.isclose(facts[key], value, rel_tol=1e-9, abs_tol=1e-9):
                        conflicts.add(key)
                    else:
                        facts[key] = value
    for key in conflicts:
        facts.pop(key, None)
    return facts


def _atomic_fact_records(facts: Mapping[tuple[str, str], float]) -> list[dict[str, Any]]:
    if len(facts) > MAX_RESEARCH_FACTS_PER_BODY:
        raise ResearchReportError("report detail contains too many atomic facts")
    records: list[dict[str, Any]] = []
    for (fact_key, unit), value in facts.items():
        try:
            period, metric = fact_key.split(":", 1)
        except ValueError as exc:  # pragma: no cover - extractor creates the key
            raise ResearchReportError("report detail atomic fact key is invalid") from exc
        records.append(
            {
                "fact_key": fact_key,
                "period": period,
                "metric": metric,
                "unit": unit,
                "value": round(float(value), 6),
            }
        )
    records.sort(key=lambda fact: (fact["fact_key"], fact["unit"]))
    return records


def _cross_check_facts(facts_by_report: Mapping[str, Mapping[tuple[str, str], float]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for evidence_id, facts in facts_by_report.items():
        for key, value in facts.items():
            grouped.setdefault(key, []).append((evidence_id, value))
    candidates: list[tuple[int, float, str, str, float, list[str]]] = []
    for (fact_key, unit), observations in grouped.items():
        ordered = sorted(observations, key=lambda item: (item[1], item[0]))
        for left in range(len(ordered)):
            for right in range(left + MIN_CROSSCHECK_REPORTS, len(ordered) + 1):
                window = ordered[left:right]
                values = [value for _, value in window]
                center = math.fsum(values) / len(values)
                spread = (max(values) - min(values)) / max(abs(center), 1e-12)
                if spread <= RESEARCH_FACT_RELATIVE_TOLERANCE:
                    candidates.append(
                        (
                            -len(window),
                            spread,
                            fact_key,
                            unit,
                            center,
                            sorted(evidence_id for evidence_id, _ in window),
                        )
                    )
    if not candidates:
        return {
            "passed": False,
            "minimum_reports": MIN_CROSSCHECK_REPORTS,
            "fact_key": None,
            "fact_unit": None,
            "consensus_value": None,
            "supporting_evidence_ids": [],
            "max_relative_spread": None,
        }
    _, spread, fact_key, unit, center, evidence_ids = min(candidates)
    return {
        "passed": True,
        "minimum_reports": MIN_CROSSCHECK_REPORTS,
        "fact_key": fact_key,
        "fact_unit": unit,
        "consensus_value": round(center, 6),
        "supporting_evidence_ids": evidence_ids,
        "max_relative_spread": round(spread, 8),
    }


def _cross_check_body_summaries(bodies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Recompute consensus from the exact atomic facts retained in summaries."""

    facts_by_report: dict[str, dict[tuple[str, str], float]] = {}
    for body in bodies:
        evidence_id = str(body["evidence_id"])
        facts_by_report[evidence_id] = {
            (str(fact["fact_key"]), str(fact["unit"])): float(fact["value"]) for fact in body["facts"]
        }
    return _cross_check_facts(facts_by_report)


def _validate_final_url(value: Any) -> None:
    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ResearchReportError("report source redirected to an invalid URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "reportapi.eastmoney.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ResearchReportError("report source redirected outside the pinned HTTPS endpoint")


def _validate_detail_url(value: Any, report_id: str) -> None:
    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ResearchReportError("report detail redirected to an invalid URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "data.eastmoney.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/report/info/{report_id}.html"
    ):
        raise ResearchReportError("report detail redirected outside the pinned HTTPS page")


def _request_detail(
    source: Mapping[str, str],
    *,
    session: Any,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> bytes:
    evidence_id = str(source.get("evidence_id") or "")
    report_id = evidence_id.removeprefix("eastmoney:")
    expected_url = f"{EASTMONEY_REPORT_DETAIL_PREFIX}{report_id}.html"
    if not _REPORT_ID.fullmatch(report_id) or source.get("url") != expected_url:
        raise ResearchReportError("report detail identity is invalid")
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        try:
            rate_limiter.acquire()
            response = session.get(
                expected_url,
                headers={
                    "User-Agent": _HEADERS["User-Agent"],
                    "Referer": EASTMONEY_REPORT_PAGE,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                },
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            _validate_detail_url(getattr(response, "url", expected_url), report_id)
            return _read_bounded_detail_response(response)
        except ResearchReportError:
            raise
        except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
            last_error = exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise ResearchReportError(f"Eastmoney report-detail request failed: {_error_label(last_error or RuntimeError())}")


def _validate_detail_body(
    raw: bytes,
    source: Mapping[str, str],
) -> tuple[dict[str, Any], dict[tuple[str, str], float]]:
    try:
        page = raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - bounded reader already proves this
        raise ResearchReportError("report detail is not UTF-8") from exc
    parser = _ReportBodyParser()
    try:
        parser.feed(page)
        parser.close()
    except (ResearchReportError, ValueError) as exc:
        raise ResearchReportError(f"report detail HTML is malformed: {exc}") from exc
    if parser.content_divs != 1 or len(parser.paragraphs) < 2:
        raise ResearchReportError("report detail has no unique non-empty body")
    zwinfo = _extract_zwinfo(page)
    body = _normalise_body_text(" ".join(parser.paragraphs))
    json_body = _normalise_body_text(zwinfo.get("notice_content"))
    if body != json_body:
        raise ResearchReportError("report detail DOM and JSON bodies differ")
    if not MIN_RESEARCH_BODY_CHARACTERS <= len(body) <= MAX_RESEARCH_BODY_CHARACTERS:
        raise ResearchReportError("report detail body length is outside the bounded contract")

    code = str(source.get("security_code") or "")
    company_name = str(source.get("company_name") or "")
    evidence_id = str(source.get("evidence_id") or "")
    report_id = evidence_id.removeprefix("eastmoney:")
    security_rows = zwinfo.get("security")
    matching_security = []
    if isinstance(security_rows, list) and len(security_rows) <= 20:
        matching_security = [
            record
            for record in security_rows
            if isinstance(record, Mapping) and record.get("stock") == code and record.get("short_name") == company_name
        ]
    notice_date = zwinfo.get("notice_date")
    checks = {
        "code_in_body": code in body,
        "name_in_body": company_name in body,
        "detail_code": len(matching_security) == 1,
        "detail_name": zwinfo.get("short_name") == company_name,
        "detail_title": zwinfo.get("notice_title") == source.get("title"),
        "detail_publisher": zwinfo.get("source_sample_name") == source.get("publisher"),
        "detail_date": isinstance(notice_date, str) and notice_date[:10] == source.get("as_of"),
        "dom_json_body": True,
    }
    if zwinfo.get("info_code") != report_id or set(checks) != RESEARCH_CONTENT_IDENTITY_CHECKS:
        raise ResearchReportError("report detail stable identity differs from metadata")
    if any(value is not True for value in checks.values()):
        raise ResearchReportError("report detail company, date, title, or publisher differs from metadata")

    signal_markers = {
        "analysis": ("点评", "分析", "投资要点", "核心观点"),
        "event": ("事件", "事项"),
        "forecast": ("预计", "预测"),
        "investment_view": ("投资建议", "评级"),
        "risk": ("风险提示", "风险"),
    }
    signals = sorted(signal for signal, markers in signal_markers.items() if any(marker in body for marker in markers))
    if not signals:
        raise ResearchReportError("report detail body has no analytical structure signal")
    facts = _extract_key_facts(parser.paragraphs)
    atomic_facts = _atomic_fact_records(facts)
    summary = {
        "evidence_id": evidence_id,
        "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "content_length": len(body),
        "paragraph_count": len(parser.paragraphs),
        "structure_signals": signals,
        "fact_count": len(atomic_facts),
        "facts": atomic_facts,
        "identity_checks": checks,
    }
    return summary, facts


def _empty_content_verification(code: str, as_of: date, reason: str) -> dict[str, Any]:
    return {
        "model_id": RESEARCH_CONTENT_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "passed": False,
        "required_bodies": MIN_RESEARCH_BODY_SOURCES,
        "attempted_bodies": 0,
        "verified_bodies": 0,
        "distinct_publishers": 0,
        "bodies": [],
        "cross_check": {
            "passed": False,
            "minimum_reports": MIN_CROSSCHECK_REPORTS,
            "fact_key": None,
            "fact_unit": None,
            "consensus_value": None,
            "supporting_evidence_ids": [],
            "max_relative_spread": None,
        },
        "reason": reason,
    }


def _verify_report_bodies(
    sources: Sequence[Mapping[str, str]],
    *,
    code: str,
    as_of: date,
    session: Any,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> dict[str, Any]:
    metadata = research_metadata_precheck(sources, reference=as_of)
    if metadata["passed"] is not True:
        return _empty_content_verification(code, as_of, "metadata_prerequisite_failed")

    candidates = sorted(
        sources,
        key=lambda source: (source["as_of"], source["evidence_id"]),
        reverse=True,
    )[:MAX_RESEARCH_BODY_FETCHES]
    summaries: list[dict[str, Any]] = []
    body_hashes: set[str] = set()
    publishers: set[str] = set()
    attempted = 0
    cross_check = _cross_check_facts({})
    for source in candidates:
        attempted += 1
        try:
            raw = _request_detail(
                source,
                session=session,
                timeout=timeout,
                rate_limiter=rate_limiter,
            )
            summary, _facts = _validate_detail_body(raw, source)
        except ResearchReportError:
            continue
        if summary["content_sha256"] in body_hashes:
            continue
        publisher_id = source["publisher_id"].casefold()
        if publisher_id in publishers:
            continue
        body_hashes.add(summary["content_sha256"])
        publishers.add(publisher_id)
        summaries.append(summary)
        if len(summaries) >= MIN_RESEARCH_BODY_SOURCES:
            cross_check = _cross_check_body_summaries(summaries)
            if cross_check["passed"]:
                break
    summaries.sort(key=lambda item: item["evidence_id"])
    cross_check = _cross_check_body_summaries(summaries)
    passed = bool(
        len(summaries) >= MIN_RESEARCH_BODY_SOURCES
        and len(publishers) >= MIN_RESEARCH_BODY_SOURCES
        and cross_check["passed"]
    )
    if passed:
        reason = ""
    elif len(summaries) < MIN_RESEARCH_BODY_SOURCES:
        reason = "insufficient_verified_report_bodies"
    else:
        reason = "no_cross_report_fact_consensus"
    result = {
        "model_id": RESEARCH_CONTENT_MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "passed": passed,
        "required_bodies": MIN_RESEARCH_BODY_SOURCES,
        "attempted_bodies": attempted,
        "verified_bodies": len(summaries),
        "distinct_publishers": len(publishers),
        "bodies": summaries,
        "cross_check": cross_check,
        "reason": reason,
    }
    try:
        return normalise_research_content_verification(
            result,
            sources=sources,
            security_code=code,
            as_of=as_of.isoformat(),
        )
    except QualityEquityError as exc:  # Defensive boundary between acquisition and scoring.
        raise ResearchReportError(f"report-content verification summary is invalid: {exc}") from exc


def _page_params(code: str, as_of: date, page_number: int) -> dict[str, Any]:
    # These names and values mirror Eastmoney's own stock.js report-page
    # configuration.  ``code`` is a validated six-digit ASCII identifier; it
    # is passed through ``requests`` params so it cannot alter another field.
    return {
        "industryCode": "*",
        "pageSize": PAGE_SIZE,
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": (as_of - timedelta(days=RESEARCH_MAX_AGE_DAYS)).isoformat(),
        "endTime": as_of.isoformat(),
        "pageNo": page_number,
        "fields": "",
        "qType": 0,
        "orgCode": "",
        "code": code,
        "rcode": "",
    }


def _request_page(
    code: str,
    as_of: date,
    page_number: int,
    *,
    session: Any,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> Mapping[str, Any]:
    last_error: BaseException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        response = None
        try:
            rate_limiter.acquire()
            response = session.get(
                EASTMONEY_REPORT_ENDPOINT,
                params=_page_params(code, as_of, page_number),
                headers=_HEADERS,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            _validate_final_url(getattr(response, "url", EASTMONEY_REPORT_ENDPOINT))
            payload = _decode_json(_read_bounded_response(response))
            if not isinstance(payload, Mapping):
                raise ResearchReportError("report payload is not an object")
            return payload
        except ResearchReportError:
            raise
        except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
            last_error = exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise ResearchReportError(f"Eastmoney report request failed: {_error_label(last_error or RuntimeError())}")


def _validate_page(
    payload: Mapping[str, Any],
    *,
    code: str,
    as_of: date,
    requested_page: int,
) -> tuple[list[dict[str, str]], int]:
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise ResearchReportError("report payload has an unexpected top-level schema")
    hits = payload.get("hits")
    page_size = payload.get("size")
    total_pages = payload.get("TotalPage")
    page_number = payload.get("pageNo")
    current_year = payload.get("currentYear")
    rows = payload.get("data")
    for label, value in (
        ("hits", hits),
        ("size", page_size),
        ("total pages", total_pages),
        ("page number", page_number),
        ("current year", current_year),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResearchReportError(f"report payload {label} is invalid")
    # Eastmoney's ``size`` is the number of rows actually returned, not the
    # requested page capacity.  It is therefore smaller on a one-page result
    # (and on the final page of a multi-page result).
    if page_number != requested_page or not isinstance(rows, list) or len(rows) > PAGE_SIZE or page_size != len(rows):
        raise ResearchReportError("report payload pagination does not match the request")
    expected_pages = math.ceil(hits / PAGE_SIZE)
    if total_pages != expected_pages or (requested_page <= total_pages and not rows):
        raise ResearchReportError("report payload pagination totals are inconsistent")
    if requested_page <= total_pages:
        expected_rows = PAGE_SIZE if requested_page < total_pages else hits - PAGE_SIZE * (total_pages - 1)
        if len(rows) != expected_rows:
            raise ResearchReportError("report payload page row count is inconsistent")

    start = as_of - timedelta(days=RESEARCH_MAX_AGE_DAYS)
    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ResearchReportError("report row is not an object")
        security_code = row.get("stockCode")
        company_name = row.get("stockName")
        report_id = row.get("infoCode")
        publisher_id = row.get("orgCode")
        title = row.get("title")
        publisher = row.get("orgSName")
        raw_date = row.get("publishDate")
        if security_code != code:
            raise ResearchReportError("report row security-code identity mismatch")
        if not isinstance(report_id, str) or not _REPORT_ID.fullmatch(report_id):
            raise ResearchReportError("report row id is invalid")
        if not isinstance(publisher_id, str) or not _PUBLISHER_ID.fullmatch(publisher_id):
            raise ResearchReportError("report row publisher id is invalid")
        if (
            not isinstance(company_name, str)
            or not isinstance(title, str)
            or not isinstance(publisher, str)
            or not isinstance(raw_date, str)
        ):
            raise ResearchReportError("report row metadata types are invalid")
        date_match = _REPORT_DATE.fullmatch(raw_date)
        if date_match is None:
            raise ResearchReportError("report row date format is invalid")
        try:
            published = date.fromisoformat(date_match.group("date"))
        except ValueError as exc:
            raise ResearchReportError("report row date is invalid") from exc
        if not start <= published <= as_of:
            raise ResearchReportError("report row date is outside the requested recent window")
        normalized.append(
            {
                "security_code": code,
                "company_name": company_name.strip(),
                "title": title.strip(),
                "publisher": publisher.strip(),
                "publisher_id": f"eastmoney-org:{publisher_id}",
                "url": f"{EASTMONEY_REPORT_DETAIL_PREFIX}{report_id}.html",
                "as_of": published.isoformat(),
                "evidence_id": f"eastmoney:{report_id}",
            }
        )
    validated: list[dict[str, str]] = []
    try:
        for source in normalized:
            validated.extend(
                normalise_research_sources(
                    [source],
                    today=as_of,
                    security_code=code,
                    max_age_days=RESEARCH_MAX_AGE_DAYS,
                )
            )
    except QualityEquityError as exc:
        raise ResearchReportError(f"report row metadata validation failed: {exc}") from exc
    return validated, total_pages


def _fetch_recent_sources(
    code: str,
    as_of: date,
    *,
    session: Any = requests,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> list[dict[str, str]]:
    by_report: dict[str, dict[str, str]] = {}
    total_pages: int | None = None
    for page_number in range(1, MAX_PAGES + 1):
        payload = _request_page(
            code,
            as_of,
            page_number,
            session=session,
            timeout=timeout,
            rate_limiter=rate_limiter,
        )
        page_sources, declared_pages = _validate_page(
            payload,
            code=code,
            as_of=as_of,
            requested_page=page_number,
        )
        if total_pages is None:
            total_pages = declared_pages
        elif total_pages != declared_pages:
            raise ResearchReportError("report pagination changed during one acquisition")
        for source in page_sources:
            identity = source["evidence_id"].casefold()
            if identity in by_report:
                raise ResearchReportError("report pages contain duplicate report ids")
            by_report[identity] = source

        publisher_ids = {source["publisher_id"].casefold() for source in by_report.values()}
        has_recent = any(
            (as_of - date.fromisoformat(source["as_of"])).days <= RESEARCH_RECENT_AGE_DAYS
            for source in by_report.values()
        )
        if (len(publisher_ids) >= MIN_RESEARCH_SOURCES and has_recent) or page_number >= declared_pages:
            break
    else:  # pragma: no cover - the bounded range always exits normally
        raise AssertionError("report page loop did not terminate")

    # Keep the newest report from each institution.  This makes the downstream
    # three-source count exactly an independent-institution count rather than
    # a count of repeated notes from one broker.
    newest: dict[str, dict[str, str]] = {}
    for source in sorted(
        by_report.values(),
        key=lambda item: (item["as_of"], item["evidence_id"]),
        reverse=True,
    ):
        newest.setdefault(source["publisher_id"].casefold(), source)
    selected = list(newest.values())[:MAX_RESEARCH_SOURCES]
    return normalise_research_sources(
        selected,
        today=as_of,
        security_code=code,
        max_age_days=RESEARCH_MAX_AGE_DAYS,
    )


def _cache_contract(code: str, as_of: date) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "code": code,
        "as_of": as_of.isoformat(),
        "source": EASTMONEY_REPORT_ENDPOINT,
        "detail_source": EASTMONEY_REPORT_DETAIL_PREFIX,
        "content_model_id": RESEARCH_CONTENT_MODEL_ID,
        "page_size": PAGE_SIZE,
        "max_pages": MAX_PAGES,
        "max_age_days": RESEARCH_MAX_AGE_DAYS,
        "recent_age_days": RESEARCH_RECENT_AGE_DAYS,
        "required_bodies": MIN_RESEARCH_BODY_SOURCES,
        "minimum_crosscheck_reports": MIN_CROSSCHECK_REPORTS,
        "max_body_fetches": MAX_RESEARCH_BODY_FETCHES,
        "fact_relative_tolerance": RESEARCH_FACT_RELATIVE_TOLERANCE,
    }


def _cache_path(code: str, as_of: date, cache_dir: Path) -> Path:
    return cache_dir / f"{MODEL_ID}_{code}_{as_of.strftime('%Y%m%d')}.json.gz"


def _make_evidence(
    code: str,
    as_of: date,
    sources: Any,
    content_verification: Any,
    *,
    cache_hit: bool,
    cache_diagnostic: str,
) -> ResearchReportEvidence:
    try:
        normalized = normalise_research_sources(
            sources,
            today=as_of,
            security_code=code,
            max_age_days=RESEARCH_MAX_AGE_DAYS,
        )
    except QualityEquityError as exc:
        raise ResearchReportError(f"research-report evidence is invalid: {exc}") from exc
    metadata_precheck = research_metadata_precheck(normalized, reference=as_of)
    try:
        normalized_content = normalise_research_content_verification(
            content_verification,
            sources=normalized,
            security_code=code,
            as_of=as_of.isoformat(),
        )
    except QualityEquityError as exc:
        raise ResearchReportError(f"research-report content evidence is invalid: {exc}") from exc
    publisher_count = int(metadata_precheck["distinct_publishers"])
    available = bool(metadata_precheck["passed"] and normalized_content["passed"])
    if available:
        reason = ""
    elif int(metadata_precheck["recent_source_count"]) < 1:
        reason = "no_report_within_recent_window"
    elif metadata_precheck["passed"] is not True:
        reason = "insufficient_independent_report_metadata"
    else:
        reason = str(normalized_content["reason"])
    return ResearchReportEvidence(
        available=available,
        code=code,
        as_of=as_of.isoformat(),
        model_id=MODEL_ID,
        sources=normalized,
        distinct_publishers=publisher_count,
        content_verification=normalized_content,
        cache_hit=cache_hit,
        cache_diagnostic=cache_diagnostic,
        reason=reason,
    )


def _from_cache(payload: Any, code: str, as_of: date) -> ResearchReportEvidence:
    if not isinstance(payload, Mapping) or set(payload) != {"contract", "sources", "content_verification"}:
        raise ResearchReportError("research-report cache payload shape is invalid")
    if payload.get("contract") != _cache_contract(code, as_of):
        raise ResearchReportError("research-report cache contract mismatch")
    return _make_evidence(
        code,
        as_of,
        payload.get("sources"),
        payload.get("content_verification"),
        cache_hit=True,
        cache_diagnostic="hit",
    )


def fetch_research_reports(
    code: str,
    as_of: date | str,
    *,
    session: Any = requests,
    cache_dir: str | Path = RESEARCH_REPORT_CACHE_DIR,
    cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    use_cache: bool = True,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    rate_limiter: Any = _GLOBAL_RATE_LIMITER,
) -> ResearchReportEvidence:
    """Fetch metadata and verify at least three independently published report bodies."""

    normalized_code = _normalise_code(code)
    cutoff = _parse_as_of(as_of)
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must be non-negative")
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

    cache: SafeFileCache | None = None
    initial = None
    diagnostic = "disabled"
    if use_cache:
        cache = SafeFileCache(
            _cache_path(normalized_code, cutoff, Path(cache_dir)),
            schema_version=CACHE_SCHEMA_VERSION,
            ttl=cache_ttl_seconds,
            max_uncompressed_bytes=MAX_RESPONSE_BYTES,
        )
        initial = cache.load()
        if initial.hit:
            try:
                return _from_cache(initial.value, normalized_code, cutoff)
            except ResearchReportError as exc:
                diagnostic = f"invalid_hit:{_error_label(exc)}"
        else:
            diagnostic = f"miss:{initial.reason}"

    active_session = requests.Session() if session is requests else session
    owns_session = active_session is not session
    try:
        sources = _fetch_recent_sources(
            normalized_code,
            cutoff,
            session=active_session,
            timeout=timeout,
            rate_limiter=rate_limiter,
        )
        metadata_precheck = research_metadata_precheck(sources, reference=cutoff)
        content_verification = (
            _verify_report_bodies(
                sources,
                code=normalized_code,
                as_of=cutoff,
                session=active_session,
                timeout=timeout,
                rate_limiter=rate_limiter,
            )
            if metadata_precheck["passed"] is True
            else _empty_content_verification(normalized_code, cutoff, "metadata_prerequisite_failed")
        )
        result = _make_evidence(
            normalized_code,
            cutoff,
            sources,
            content_verification,
            cache_hit=False,
            cache_diagnostic=diagnostic,
        )
    except Exception as exc:
        unavailable = _make_evidence(
            normalized_code,
            cutoff,
            [],
            _empty_content_verification(normalized_code, cutoff, "source_unavailable"),
            cache_hit=False,
            cache_diagnostic=diagnostic,
        )
        return replace(unavailable, reason=f"source_unavailable:{_error_label(exc)}")
    finally:
        if owns_session:
            active_session.close()

    if cache is None:
        return result
    payload = {
        "contract": _cache_contract(normalized_code, cutoff),
        "sources": sources,
        "content_verification": content_verification,
    }
    expected_hash = None
    if initial is not None and isinstance(initial.metadata, Mapping):
        candidate = initial.metadata.get("payload_sha256")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
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
                return replace(_from_cache(winner.value, normalized_code, cutoff), cache_diagnostic="race_winner")
            except ResearchReportError:
                pass
        return replace(result, cache_diagnostic=f"{diagnostic};write_conflict")
    except SafeCacheError as exc:
        return replace(result, cache_diagnostic=f"{diagnostic};write_failed:{_error_label(exc)}")


def fetch_research_reports_batch(
    requests_: Sequence[Mapping[str, Any]],
    *,
    max_workers: int = MAX_WORKERS,
    progress_cb: Any = None,
) -> dict[str, dict[str, Any]]:
    """Fetch a deterministic, bounded Type 7 report-metadata batch."""

    if isinstance(requests_, (str, bytes)) or not isinstance(requests_, Sequence):
        raise TypeError("research-report batch requests must be a sequence")
    if len(requests_) > MAX_BATCH_COMPANIES:
        raise ValueError("research-report batch exceeds the company limit")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_WORKERS:
        raise ValueError(f"max_workers must be between 1 and {MAX_WORKERS}")
    prepared: list[tuple[str, str]] = []
    seen: set[str] = set()
    for request in requests_:
        if not isinstance(request, Mapping) or set(request) != {"code", "as_of"}:
            raise ValueError("research-report batch request shape is invalid")
        code = _normalise_code(request.get("code"))
        cutoff = _parse_as_of(request.get("as_of")).isoformat()
        if code in seen:
            raise ValueError(f"research-report batch contains duplicate code: {code}")
        seen.add(code)
        prepared.append((code, cutoff))
    prepared.sort()
    if not prepared:
        return {}

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(prepared))) as executor:
        future_to_request = {
            executor.submit(fetch_research_reports, code, cutoff): (code, cutoff) for code, cutoff in prepared
        }
        completed = 0
        for future in as_completed(future_to_request):
            code, cutoff = future_to_request[future]
            try:
                result = future.result()
            except Exception as exc:  # Defensive isolation for one company/source worker.
                result = ResearchReportEvidence(
                    available=False,
                    code=code,
                    as_of=cutoff,
                    model_id=MODEL_ID,
                    sources=[],
                    distinct_publishers=0,
                    content_verification=_empty_content_verification(
                        code,
                        date.fromisoformat(cutoff),
                        "worker_failure",
                    ),
                    cache_hit=False,
                    cache_diagnostic="",
                    reason=f"worker_failure:{_error_label(exc)}",
                )
            results[code] = result.to_dict()
            completed += 1
            if progress_cb:
                progress_cb(completed, len(prepared))
    return {code: results[code] for code, _ in prepared}


__all__ = [
    "EASTMONEY_REPORT_ENDPOINT",
    "EASTMONEY_REPORT_DETAIL_PREFIX",
    "MODEL_ID",
    "ResearchReportError",
    "ResearchReportEvidence",
    "fetch_research_reports",
    "fetch_research_reports_batch",
]
