"""Bounded CNINFO investor-relations evidence acquisition.

The source is useful primary material about products, customers and operating
conditions, but every answer is a company statement.  Records produced here
are explicitly marked non-independent and are never converted into a score.
"""

from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Collection, Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests

from data.as_of import shanghai_today
from data.cache import SafeCacheError, SafeFileCache
from data.provider_http import (
    RequestRateLimiter,
    is_transient_request_error,
    read_bounded_response_bytes,
    retry_delay_seconds,
)


INVESTOR_RELATIONS_ADAPTER_VERSION = 1
CNINFO_IR_LOOKUP_URL = "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo"
CNINFO_IR_QUESTION_URL = "https://irm.cninfo.com.cn/newircs/company/question"
CNINFO_IR_PUBLIC_SEARCH_URL = "https://irm.cninfo.com.cn/ircs/search"
INVESTOR_RELATIONS_MAX_TARGET_CODES = 24
INVESTOR_RELATIONS_MAX_REQUESTS = 72

_CACHE_ROOT = Path(__file__).resolve().parent / "cache" / "investor_relations"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_LOOKBACK_DAYS = 183
_PAGE_SIZE = 20
_MAX_PAGES = 2
_MAX_EVIDENCE_PER_CODE = 8
_LOOKUP_MAX_BYTES = 512 * 1024
_QUESTION_MAX_BYTES = 4 * 1024 * 1024
_SUCCESS_CACHE_SECONDS = 30 * 24 * 60 * 60
_EMPTY_CACHE_SECONDS = 7 * 24 * 60 * 60
_MAX_CACHE_COVERAGE_AGE_DAYS = 30
_CACHE_MAX_UNCOMPRESSED_BYTES = 12 * 1024 * 1024


class InvestorRelationsError(RuntimeError):
    """CNINFO investor-relations evidence violated its source contract."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _strict_json(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise InvestorRelationsError(f"CNINFO IR JSON contains duplicate key: {key}")
            output[key] = value
        return output

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                InvestorRelationsError(f"CNINFO IR JSON contains non-finite constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvestorRelationsError("CNINFO IR response is not strict UTF-8 JSON") from exc


def _code(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{6}", text) else ""


def _validate_final_url(final_url: str, expected_url: str) -> None:
    final = urlsplit(final_url)
    expected = urlsplit(expected_url)
    try:
        port = final.port
    except ValueError as exc:
        raise InvestorRelationsError("CNINFO IR request returned an invalid final URL") from exc
    if (
        final.scheme != "https"
        or final.hostname != expected.hostname
        or final.path != expected.path
        or final.username is not None
        or final.password is not None
        or port not in (None, 443)
        or final.fragment
    ):
        raise InvestorRelationsError("CNINFO IR request redirected outside its fixed HTTPS endpoint")


def _clean_text(value: Any, *, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if any(ord(character) < 32 for character in text):
        raise InvestorRelationsError("CNINFO IR text contains control characters")
    return text[:maximum]


def _epoch_date(value: Any) -> date | None:
    if isinstance(value, bool):
        return None
    try:
        milliseconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if milliseconds < 946_684_800_000 or milliseconds > 4_102_444_800_000:
        return None
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).astimezone(_SHANGHAI).date()


class InvestorRelationsClient:
    """Serial-by-default client with a global request ceiling and raw replay cache."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        cache_dir: Path | str = _CACHE_ROOT,
        timeout: float = 20.0,
        retries: int = 3,
        request_limit: int = INVESTOR_RELATIONS_MAX_REQUESTS,
        request_interval: float = 0.15,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0 or retries < 1 or request_limit < 1:
            raise ValueError("investor-relations client limits must be positive")
        self._session = session or requests.Session()
        self._cache_dir = Path(cache_dir)
        self._timeout = float(timeout)
        self._retries = int(retries)
        self._request_limit = int(request_limit)
        self._limiter = RequestRateLimiter(request_interval)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._network_requests = 0
        self._cache_hits = 0

    def diagnostic(self) -> dict[str, int]:
        return {
            "network_requests": self._network_requests,
            "cache_hits": self._cache_hits,
            "request_limit": self._request_limit,
            "max_parallel_requests": 1,
        }

    def _cache_path(self, code: str) -> Path:
        return self._cache_dir / f"cninfo-ir-{code}.json.gz"

    def _network_post(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        last_error: BaseException | None = None
        for attempt in range(self._retries):
            if self._network_requests >= self._request_limit:
                raise InvestorRelationsError("CNINFO IR request hard limit exhausted")
            self._limiter.acquire()
            self._network_requests += 1
            response = None
            try:
                response = self._session.post(
                    url,
                    params=dict(params or {}),
                    data=dict(data or {}),
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://irm.cninfo.com.cn/newircs/"},
                    timeout=self._timeout,
                    stream=True,
                )
                response.raise_for_status()
                final_url = str(response.url)
                _validate_final_url(final_url, url)
                return read_bounded_response_bytes(response, max_bytes), final_url
            except (requests.RequestException, ValueError, InvestorRelationsError) as exc:
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
        raise InvestorRelationsError(f"CNINFO IR request failed: {type(last_error).__name__}") from last_error

    def load_cached(self, code: str, *, as_of: date) -> list[dict[str, Any]] | None:
        canonical = _code(code)
        if not canonical:
            raise ValueError("CNINFO IR cache lookup requires a canonical code")
        try:
            loaded = SafeFileCache(
                self._cache_path(canonical),
                schema_version=1,
                ttl=_SUCCESS_CACHE_SECONDS,
                max_uncompressed_bytes=_CACHE_MAX_UNCOMPRESSED_BYTES,
            ).load()
            if not loaded.hit or not isinstance(loaded.value, Mapping):
                return None
            payload = loaded.value
            if payload.get("schema_version") != 1 or payload.get("security_code") != canonical:
                return None
            covered_through = date.fromisoformat(str(payload.get("covered_through") or ""))
            if covered_through > as_of or (as_of - covered_through).days > _MAX_CACHE_COVERAGE_AGE_DAYS:
                return None
            fetched_at = datetime.fromisoformat(str(payload.get("fetched_at") or ""))
            if fetched_at.tzinfo is None:
                return None
            raw_responses = payload.get("raw_responses")
            if not isinstance(raw_responses, list) or not raw_responses:
                return None
            has_evidence = payload.get("has_evidence") is True
            maximum_age = _SUCCESS_CACHE_SECONDS if has_evidence else _EMPTY_CACHE_SECONDS
            age = (self._now().astimezone(timezone.utc) - fetched_at.astimezone(timezone.utc)).total_seconds()
            if age < 0 or age > maximum_age:
                return None
            decoded: list[tuple[str, bytes, str]] = []
            for item in raw_responses:
                if not isinstance(item, Mapping):
                    return None
                kind = str(item.get("kind") or "")
                raw = base64.b64decode(str(item.get("raw_base64") or ""), validate=True)
                if hashlib.sha256(raw).hexdigest() != item.get("raw_sha256"):
                    return None
                final_url = str(item.get("final_url") or "")
                expected = CNINFO_IR_LOOKUP_URL if kind == "lookup" else CNINFO_IR_QUESTION_URL
                _validate_final_url(final_url, expected)
                decoded.append((kind, raw, final_url))
            evidence = _replay_responses(canonical, as_of=covered_through, responses=decoded)
            if bool(evidence) is not has_evidence:
                return None
        except (OSError, ValueError, TypeError, base64.binascii.Error, InvestorRelationsError, SafeCacheError):
            return None
        self._cache_hits += 1
        return evidence

    def _save_cache(
        self,
        code: str,
        *,
        as_of: date,
        responses: list[tuple[str, bytes, str]],
        has_evidence: bool,
    ) -> None:
        payload = {
            "schema_version": 1,
            "security_code": code,
            "covered_through": as_of.isoformat(),
            "fetched_at": self._now().astimezone(timezone.utc).isoformat(),
            "has_evidence": has_evidence,
            "raw_responses": [
                {
                    "kind": kind,
                    "final_url": final_url,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "raw_base64": base64.b64encode(raw).decode("ascii"),
                }
                for kind, raw, final_url in responses
            ],
        }
        try:
            SafeFileCache(
                self._cache_path(code),
                schema_version=1,
                ttl=_SUCCESS_CACHE_SECONDS if has_evidence else _EMPTY_CACHE_SECONDS,
                max_uncompressed_bytes=_CACHE_MAX_UNCOMPRESSED_BYTES,
            ).save(payload)
        except SafeCacheError:
            pass

    def fetch(self, code: str, *, as_of: date, force_refresh: bool = False) -> list[dict[str, Any]]:
        canonical = _code(code)
        if not canonical.startswith(("0", "3")):
            raise ValueError("CNINFO IR acquisition supports Shenzhen A-share codes only")
        if not force_refresh:
            cached = self.load_cached(canonical, as_of=as_of)
            if cached is not None:
                return cached
        responses: list[tuple[str, bytes, str]] = []
        lookup_raw, lookup_url = self._network_post(
            CNINFO_IR_LOOKUP_URL,
            data={"keyWord": canonical},
            max_bytes=_LOOKUP_MAX_BYTES,
        )
        responses.append(("lookup", lookup_raw, lookup_url))
        secid = _parse_lookup(lookup_raw, canonical)
        start = as_of - timedelta(days=_LOOKBACK_DAYS)
        pages: list[bytes] = []
        total_pages = 1
        for page_number in range(1, _MAX_PAGES + 1):
            if page_number > total_pages:
                break
            params = {
                "_t": "1",
                "stockcode": canonical,
                "orgId": secid,
                "pageSize": str(_PAGE_SIZE),
                "pageNum": str(page_number),
                "keyWord": "",
                "startDay": start.isoformat(),
                "endDay": as_of.isoformat(),
            }
            raw, final_url = self._network_post(
                CNINFO_IR_QUESTION_URL,
                params=params,
                max_bytes=_QUESTION_MAX_BYTES,
            )
            responses.append((f"questions:{page_number}", raw, final_url))
            pages.append(raw)
            total_pages = _question_total_pages(raw, expected_page=page_number)
        evidence = _parse_question_pages(canonical, pages, as_of=as_of)
        self._save_cache(canonical, as_of=as_of, responses=responses, has_evidence=bool(evidence))
        return evidence


def _parse_lookup(raw: bytes, code: str) -> str:
    payload = _strict_json(raw)
    if not isinstance(payload, Mapping) or payload.get("statusCode") != 200:
        raise InvestorRelationsError("CNINFO IR company lookup failed")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) > 10:
        raise InvestorRelationsError("CNINFO IR company lookup has invalid rows")
    matches = [row for row in rows if isinstance(row, Mapping) and str(row.get("stockCode") or "") == code]
    if len(matches) != 1:
        raise InvestorRelationsError("CNINFO IR company lookup is not identity-unique")
    secid = str(matches[0].get("secid") or "").strip()
    if not re.fullmatch(r"gssz\d{7}", secid):
        raise InvestorRelationsError("CNINFO IR company lookup returned an invalid Shenzhen secid")
    return secid


def _question_total_pages(raw: bytes, *, expected_page: int) -> int:
    payload = _strict_json(raw)
    if not isinstance(payload, Mapping):
        raise InvestorRelationsError("CNINFO IR question response is not an object")
    page_no = payload.get("pageNo")
    page_size = payload.get("pageSize")
    total = payload.get("total")
    total_pages = payload.get("totalPage")
    rows = payload.get("rows")
    if (
        page_no != expected_page
        or page_size != _PAGE_SIZE
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or total_pages < 0
        or not isinstance(rows, list)
        or len(rows) > _PAGE_SIZE
        or total_pages != (math.ceil(total / _PAGE_SIZE) if total else 0)
    ):
        raise InvestorRelationsError("CNINFO IR pagination contract changed")
    return total_pages


def _parse_question_pages(code: str, pages: Collection[bytes], *, as_of: date) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for expected_page, raw in enumerate(pages, start=1):
        _question_total_pages(raw, expected_page=expected_page)
        payload = _strict_json(raw)
        for row in payload["rows"]:
            if not isinstance(row, Mapping) or str(row.get("stockCode") or "") != code:
                raise InvestorRelationsError("CNINFO IR question row has a mismatched company identity")
            index_id = str(row.get("indexId") or "").strip()
            if not re.fullmatch(r"\d{10,24}", index_id) or index_id in seen_ids:
                raise InvestorRelationsError("CNINFO IR question identity is invalid or duplicated")
            seen_ids.add(index_id)
            answer = _clean_text(row.get("attachedContent"), maximum=2_000)
            question = _clean_text(row.get("mainContent"), maximum=1_000)
            answer_date = _epoch_date(row.get("updateDate"))
            if not answer or not question or answer_date is None or answer_date > as_of:
                continue
            output.append(
                {
                    "schema_version": 1,
                    "security_code": code,
                    "evidence_id": f"cninfo-ir:{code}:{index_id}",
                    "as_of": answer_date.isoformat(),
                    "question": question,
                    "company_answer": answer,
                    "source": "深交所互动易公司回复",
                    "source_url": f"{CNINFO_IR_PUBLIC_SEARCH_URL}?{urlencode({'keyword': code})}",
                    "source_raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "source_role": "company_statement",
                    "independent": False,
                    "use_for_automatic_score": False,
                }
            )
    output.sort(key=lambda item: (item["as_of"], item["evidence_id"]), reverse=True)
    return output[:_MAX_EVIDENCE_PER_CODE]


def _replay_responses(
    code: str,
    *,
    as_of: date,
    responses: Collection[tuple[str, bytes, str]],
) -> list[dict[str, Any]]:
    lookup = [raw for kind, raw, _url in responses if kind == "lookup"]
    pages = [
        (int(kind.split(":", 1)[1]), raw)
        for kind, raw, _url in responses
        if kind.startswith("questions:") and kind.split(":", 1)[1].isdigit()
    ]
    if len(lookup) != 1 or not pages:
        raise InvestorRelationsError("CNINFO IR cache omitted required raw responses")
    _parse_lookup(lookup[0], code)
    pages.sort()
    if [number for number, _raw in pages] != list(range(1, len(pages) + 1)):
        raise InvestorRelationsError("CNINFO IR cache question pages are not contiguous")
    return _parse_question_pages(code, [raw for _number, raw in pages], as_of=as_of)


def _missing_research_and_development(company: Mapping[str, Any]) -> bool:
    rows = company.get("indicators", [])
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, (list, tuple)):
        return True
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("RDEXPEND")
        if value is None or isinstance(value, bool):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed) and parsed >= 0:
            return False
    return True


def attach_investor_relations_evidence(
    financials: Mapping[str, Mapping[str, Any]],
    *,
    codes: Collection[str] | None = None,
    as_of: date,
    client: InvestorRelationsClient | None = None,
    force_refresh: bool = False,
    max_target_codes: int = INVESTOR_RELATIONS_MAX_TARGET_CODES,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Attach bounded company statements without deriving qualitative scores."""

    started = time.monotonic()
    if max_target_codes < 1:
        raise ValueError("investor-relations target limit must be positive")
    if as_of > shanghai_today():
        raise ValueError("investor-relations as_of cannot be in the future")
    population = sorted({_code(value) for value in (codes or financials.keys())} - {""})
    candidates = [
        code
        for code in population
        if code.startswith(("0", "3")) and code in financials and _missing_research_and_development(financials[code])
    ]
    active_client = client or InvestorRelationsClient()
    evidence_by_code: dict[str, list[dict[str, Any]]] = {}
    uncached: list[str] = []
    if not force_refresh:
        for code in candidates:
            cached = active_client.load_cached(code, as_of=as_of)
            if cached is None:
                uncached.append(code)
            else:
                evidence_by_code[code] = cached
    else:
        uncached = list(candidates)
    targets = uncached[:max_target_codes]
    status_counts: Counter[str] = Counter()
    for code in targets:
        try:
            evidence_by_code[code] = active_client.fetch(code, as_of=as_of, force_refresh=force_refresh)
            status_counts["ok" if evidence_by_code[code] else "true_empty"] += 1
        except InvestorRelationsError:
            status_counts["source_unavailable"] += 1

    output: dict[str, dict[str, Any]] = {code: deepcopy(dict(company)) for code, company in financials.items()}
    attached_codes: list[str] = []
    attached_items = 0
    for code, evidence in sorted(evidence_by_code.items()):
        if not evidence:
            continue
        output[code]["investor_relations_evidence"] = deepcopy(evidence)
        attached_codes.append(code)
        attached_items += len(evidence)
    diagnostic = {
        "adapter_version": INVESTOR_RELATIONS_ADAPTER_VERSION,
        "strategy": "cninfo_ir_company_statements_non_independent_no_automatic_score",
        "candidate_codes": len(candidates),
        "cached_codes": len(candidates) - len(uncached),
        "target_codes": len(targets),
        "skipped_uncached_codes": len(uncached) - len(targets),
        "attached_codes": attached_codes,
        "attached_items": attached_items,
        "status_counts": dict(sorted(status_counts.items())),
        "company_statement_only": True,
        "independent_evidence": False,
        "automatic_score_enabled": False,
        "client": active_client.diagnostic(),
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    return output, diagnostic


__all__ = [
    "INVESTOR_RELATIONS_ADAPTER_VERSION",
    "INVESTOR_RELATIONS_MAX_REQUESTS",
    "InvestorRelationsClient",
    "InvestorRelationsError",
    "attach_investor_relations_evidence",
]
