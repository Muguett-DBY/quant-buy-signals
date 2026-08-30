"""Check that AI-screening claim URLs are real, reachable web resources.

This audit does not decide whether a claim is financially correct.  It verifies
the narrower release facts that can be checked mechanically: every claimed URL
is public HTTP(S), the resource is reachable, and web-search claims point to the
same finding URL and do not relabel an old HTML article as a newer disclosure.
Text-bearing PDFs are parsed with the project's pinned PyMuPDF dependency and
checked for company identity, report period and cited fact; malformed or
image-only PDFs remain explicitly unverified.
Contract v4 also hashes the exact claim/finding semantics that publication can
expose, so changing cited prose while retaining a URL cannot reuse an audit.
When a claim declares a report period, the source must prove that same period;
a coincidental company name, year or number is not sufficient.
V4 requires every cited numeric component to match its metric, report period
and units/signs. A V3 audit cannot be reused after these checks changed.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import gzip
import hashlib
import ipaddress
import io
import json
import math
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urljoin, urlparse

from tools.ai_source_urls import (
    canonical_urls,
    claim_source_urls,
    finding_source_url,
    is_deterministic_valuation_claim,
    is_search_provenance_claim,
)


_OFFICIAL_DOMAIN_SUFFIXES = (
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "hkexnews.hk",
)
_BLOCKED_HTTP_STATUSES = frozenset({401, 403, 407, 429, 456})
# A few Chinese disclosure mirrors return the non-standard 456 status while
# throttling a burst of otherwise valid requests.  Treat those responses as a
# transport retry, not as evidence that the cited filing is wrong.  The same
# bounded retry also covers transient gateway errors and timeouts.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 456, 500, 502, 503, 504})
_MAX_FETCH_ATTEMPTS = 3
_HOST_THROTTLE_SECONDS = 0.04
_host_throttle_lock = threading.Lock()
_host_next_request: dict[str, float] = {}
_dns_cache_lock = threading.Lock()
_dns_cache: dict[tuple[str, int], tuple[str, ...]] = {}
_dns_error_cache: dict[tuple[str, int], str] = {}
_REDIRECT_HTTP_STATUSES = frozenset({301, 302, 303, 307, 308})
# Keep PDF parsing bounded even when a reachable source is unexpectedly large
# or contains a very large number of pages.  The HTTP layer already limits the
# downloaded body; this cap additionally bounds decompressed text retained by
# PyMuPDF.
# Financial statements place the period summary and key facts near the front
# of the filing.  Keeping the semantic window bounded prevents one malformed
# or unusually large PDF from monopolising the full-source audit.
_MAX_PDF_TEXT_CHARS = 800_000
_MAX_PDF_BYTES = 12 * 1024 * 1024
# Annual reports can contain thousands of scanned annex pages.  The identity,
# period and headline financial facts used by this audit are in the opening
# statement/notes; cap extraction before a pathological annex monopolises the
# release job.  A bounded prefix is reported as unverified when it cannot
# prove the claim, never as a semantic pass.
_MAX_PDF_PAGES = 120
AUDIT_CONTRACT_VERSION = 4
_MALFORMED_PE_RE = re.compile(r"(?<![A-Za-z])tyPE(?=\s*[-+]?\d)")
_NEGATIVE_PE_RE = re.compile(r"(?<![A-Za-z])(?<!原始)(?<!原始 )PE\s*(-\d+(?:\.\d+)?)\s*(?:倍)?", re.IGNORECASE)
_DUPLICATE_PERCENT_SUFFIX_RE = re.compile(r"(百分点|期末口径)%")


def _public_text(value: Any, limit: int) -> str:
    """Apply the same scalar text projection used by publication."""

    return str(value or "").strip()[:limit]


def public_claim_statement(value: Any) -> str:
    """Project the exact readable claim text exposed by publication."""

    text = _public_text(value, 600)
    text = _MALFORMED_PE_RE.sub("PE", text)
    text = _NEGATIVE_PE_RE.sub(r"PE 不适用（原始 PE \1 倍）", text)
    return _DUPLICATE_PERCENT_SUFFIX_RE.sub(r"\1", text)


def _public_claim_source_fields(claim: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    """Return the source fields that survive ``publish_ai_screening``."""

    if is_deterministic_valuation_claim(claim):
        return "", "估值快照来自本代市场数据，不绑定新闻来源", []
    if is_search_provenance_claim(claim):
        return "", "搜索事件记录已单独保留，不将检索摘要当作财务事实", []
    raw_sources: list[str] = []
    singular_source = str(claim.get("source_ref") or "").strip()
    if singular_source:
        raw_sources.append(singular_source)
    source_refs = claim.get("source_refs")
    if isinstance(source_refs, list):
        raw_sources.extend(str(value).strip() for value in source_refs if str(value).strip())
    raw_source = raw_sources[0] if raw_sources else ""
    raw_context = str(claim.get("source_context") or "").strip()
    if not raw_source:
        raw_source = raw_context

    public_urls: list[str] = []
    candidates = raw_sources + ([raw_context] if raw_context and raw_context not in raw_sources else [])
    for candidate in candidates or ([raw_source] if raw_source else []):
        for url in canonical_urls(candidate):
            if url not in public_urls:
                public_urls.append(url)
    context = raw_context if raw_context else raw_source
    if not canonical_urls(context):
        context = _public_text(context, 240)
    return (public_urls[0] if public_urls else ""), context, public_urls


def public_source_semantic_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project exactly the claim/finding semantics that the public review keeps.

    This intentionally excludes scores and recommendation prose.  It binds the
    published company identity to every claim statement and search finding,
    including their canonical URLs, date/period metadata and source kinds.  The
    same function can be applied to a merged artifact or its public projection.
    """

    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("AI screening packets are missing")
    publish_search_findings = str(payload.get("review_mode") or "") == "opencode_native_company_research_review"
    companies: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("AI screening packet is not an object")
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            continue

        raw_findings = review.get("search_findings") if publish_search_findings else []
        findings = raw_findings if isinstance(raw_findings, list) else []
        finding_rows: list[dict[str, Any]] = []
        findings_by_id: dict[str, Mapping[str, Any]] = {}
        for finding_index, finding in enumerate(findings):
            if not isinstance(finding, Mapping):
                continue
            finding_id = _public_text(finding.get("id"), 120)
            if finding_id:
                findings_by_id[finding_id] = finding
            finding_rows.append(
                {
                    "finding_index": finding_index,
                    "id": finding_id,
                    "query": _public_text(finding.get("query"), 240),
                    "title": _public_text(finding.get("title"), 300),
                    "url": finding_source_url(finding) or None,
                    "published_at": _public_text(finding.get("published_at"), 32) or None,
                    "report_period": _public_text(finding.get("report_period"), 80) or None,
                    "finding": _public_text(finding.get("finding"), 600),
                    "stance": _public_text(finding.get("stance"), 16),
                    "source_kind": _public_text(finding.get("source_kind"), 48),
                    "source_quality": _public_text(finding.get("source_quality"), 32),
                }
            )

        raw_claims = review.get("claims")
        claims = raw_claims if isinstance(raw_claims, list) else []
        claim_rows: list[dict[str, Any]] = []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            # Search transcripts are retained as query metadata by the
            # review converter, but publication deliberately removes them
            # from the public claim graph. Keep the audit projection/counts
            # aligned with that public contract instead of treating a
            # non-fact transcript as an unbound financial claim.
            if is_search_provenance_claim(claim):
                continue
            source_ref, source_context, source_refs = _public_claim_source_fields(claim)
            finding_id = _public_text(claim.get("search_finding_id"), 120)
            linked_finding = findings_by_id.get(finding_id, {})
            claim_rows.append(
                {
                    # Publication removes search-provenance transcripts, so
                    # public claim indices are dense and independent of the
                    # raw converter row positions.
                    "claim_index": len(claim_rows),
                    "statement": public_claim_statement(claim.get("statement")),
                    "source_ref": source_ref,
                    "source_context": source_context,
                    "source_refs": source_refs,
                    "support": _public_text(claim.get("support"), 16),
                    "fact_id": _public_text(claim.get("fact_id"), 120),
                    "search_finding_id": finding_id,
                    "source_kind": _public_text(claim.get("source_kind"), 48),
                    "linked_published_at": _public_text(linked_finding.get("published_at"), 32) or None,
                    "linked_report_period": _public_text(linked_finding.get("report_period"), 80) or None,
                    "linked_source_kind": _public_text(linked_finding.get("source_kind"), 48),
                }
            )

        companies.append(
            {
                "security_code": _public_text(packet.get("security_code"), 16),
                "name": _public_text(packet.get("name"), 160),
                "type_key": _public_text(packet.get("type_key"), 16),
                "web_search_event_evidence": {
                    "queries": [
                        _public_text(value, 240)
                        for value in review.get("web_search_queries", [])
                        if _public_text(value, 240)
                    ],
                    "event_ids": [
                        _public_text(value, 160)
                        for value in review.get("web_search_event_ids", [])
                        if _public_text(value, 160)
                    ],
                    "event_log_sha256": [
                        _public_text(value, 64)
                        for value in review.get("web_search_event_log_sha256", [])
                        if _public_text(value, 64)
                    ],
                    "thread_ids": [
                        _public_text(value, 160)
                        for value in review.get("web_search_thread_ids", [])
                        if _public_text(value, 160)
                    ],
                },
                "claims": claim_rows,
                "search_findings": finding_rows,
            }
        )
    companies.sort(key=lambda item: (item["security_code"], item["type_key"], item["name"]))
    return {"companies": companies}


def source_semantic_projection_sha256(payload: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
    """Return the canonical public-source projection digest and comparable counts."""

    projection = public_source_semantic_projection(payload)
    canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    companies = projection["companies"]
    claims = [claim for company in companies for claim in company["claims"]]
    findings = [finding for company in companies for finding in company["search_findings"]]
    source_references = [url for claim in claims for url in claim["source_refs"]]
    source_references.extend(finding["url"] for finding in findings if finding["url"])
    counts = {
        "projection_company_count": len(companies),
        "projection_claim_count": len(claims),
        "projection_search_finding_count": len(findings),
        "projection_source_reference_count": len(source_references),
        "projection_unique_url_count": len(set(source_references)),
    }
    return hashlib.sha256(canonical).hexdigest(), counts


class _PublishedDateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_dates: list[str] = []
        self.time_dates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): str(value or "") for key, value in attrs}
        if tag.casefold() == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").casefold()
            if key in {"article:published_time", "datepublished", "pubdate", "publishdate"}:
                self.meta_dates.append(values.get("content", ""))
        elif tag.casefold() == "time" and values.get("datetime"):
            self.time_dates.append(values["datetime"])


class _VisibleTextParser(HTMLParser):
    """Extract enough visible HTML text for a small source identity check."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.metadata_parts: list[str] = []
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"} and not self._skip_depth:
            self.text_parts.append("\n")
        if folded == "meta":
            values = {key.casefold(): str(value or "") for key, value in attrs}
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").casefold()
            if key in {"description", "keywords", "og:title", "og:description", "article:section"}:
                content = values.get("content", "").strip()
                if content:
                    self.metadata_parts.append(content)
        if folded == "title":
            self._title_depth += 1
        if folded in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded in {"td", "th"} and not self._skip_depth:
            self.text_parts.append("\t")
        if folded == "title":
            self._title_depth = max(0, self._title_depth - 1)
        if folded in {"script", "style", "noscript", "template"}:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self.title_parts.append(data)
        self.text_parts.append(data)


def _date_value(value: Any) -> date | None:
    text = str(value or "").strip()
    match = re.search(r"(?<!\d)(20\d{2})[-/]([01]\d)[-/]([0-3]\d)(?!\d)", text)
    if not match:
        match = re.search(r"(?<!\d)(20\d{2})([01]\d)([0-3]\d)(?:\d{4,6})?(?!\d)", text)
    if not match:
        match = re.search(r"(?<!\d)(20\d{2})年([01]?\d)月([0-3]?\d)日", text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _report_period_end(value: Any) -> date | None:
    text = str(value or "").strip()
    exact = _date_value(text)
    if exact:
        return exact
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", text)
    if not year_match:
        return None
    year = int(year_match.group(1))
    folded = text.casefold().replace(" ", "")
    if "q1" in folded or "一季" in folded:
        return date(year, 3, 31)
    if "h1" in folded or "上半年" in folded or "半年度" in folded or "中报" in folded or "q2" in folded:
        return date(year, 6, 30)
    if "q3" in folded or "三季" in folded:
        return date(year, 9, 30)
    if "h2" in folded or "q4" in folded or "年度" in folded or "年报" in folded:
        return date(year, 12, 31)
    if re.fullmatch(r"20\d{2}年?", folded):
        return date(year, 12, 31)
    month_match = re.search(r"20\d{2}[-/年]([01]?\d)(?:月)?$", folded)
    if month_match:
        month = int(month_match.group(1))
        next_month = date(year + (month == 12), month % 12 + 1, 1)
        return date.fromordinal(next_month.toordinal() - 1)
    return None


def _article_published_date(body: bytes, content_type: str) -> date | None:
    if "html" not in content_type.casefold():
        return None
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1).strip("\"'") if charset_match else "utf-8"
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")

    # Structured article metadata outranks dates in navigation and live widgets.
    json_ld_dates = re.findall(r'["\']datePublished["\']\s*:\s*["\']([^"\']+)', text, re.IGNORECASE)
    for raw in json_ld_dates:
        if parsed := _date_value(raw):
            return parsed

    parser = _PublishedDateParser()
    parser.feed(text)
    for raw in parser.meta_dates:
        if parsed := _date_value(raw):
            return parsed

    # AASTOCKS pages expose the article timestamp in ``newsDT`` while also
    # rendering today's date elsewhere.  It must win over generic <time> tags.
    news_dates = re.findall(r"\bnewsDT\s*=\s*[\"']([^\"']+)", text, re.IGNORECASE)
    for raw in news_dates:
        if parsed := _date_value(raw):
            return parsed

    for raw in parser.time_dates:
        if parsed := _date_value(raw):
            return parsed
    return None


def _html_visible_text(body: bytes, content_type: str) -> tuple[str, str, str]:
    """Return normalized visible text and title for HTML-only checks."""

    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1).strip("\"'") if charset_match else "utf-8"
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    parser = _VisibleTextParser()
    try:
        parser.feed(text)
    except Exception:  # pragma: no cover - malformed pages should fail closed below
        return "", "", ""
    visible = re.sub(r"[^\S\n]+", " ", "".join(parser.text_parts)).casefold()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).casefold()
    metadata = re.sub(r"\s+", " ", " ".join(parser.metadata_parts)).casefold()
    return visible, title, metadata


def _normalised_company_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).casefold()
    # These suffixes are legal-company boilerplate and are poor identity
    # anchors on disclosure pages.  Keep the original name as a fallback.
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "集团股份", "集团", "控股", "公司"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _security_code_in_text(text: str, code: str) -> bool:
    return bool(code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text))


def _pdf_company_identity_matches(text: str, security_code: str, name: str) -> bool:
    """Match issuer identity while allowing common abbreviated Chinese names.

    Disclosure PDFs often use the legal name (for example ``成都超纯应用材料``)
    while the quote feed uses the short name (``超纯应材``).  A bounded ordered
    character match after a two-character prefix keeps this useful alias case
    explicit without accepting an unrelated issuer that merely shares one
    character.
    """

    visible = re.sub(r"\s+", "", text).casefold()
    code = re.sub(r"\s+", "", str(security_code or "")).casefold()
    normal_name = _normalised_company_name(name)
    if _security_code_in_text(text, code) or (normal_name and normal_name in visible):
        return True
    if len(normal_name) < 3 or not all("\u4e00" <= char <= "\u9fff" for char in normal_name):
        return False
    prefix = normal_name[:2]
    start = visible.find(prefix)
    if start < 0:
        return False
    cursor = start + len(prefix)
    # Keep the alias search local to the issuer heading; a random sequence far
    # apart in an annual report is not an identity proof.
    for char in normal_name[2:]:
        position = visible.find(char, cursor, cursor + 9)
        if position < 0:
            return False
        cursor = position + 1
    return True


def _is_industry_source_claim(claim: Mapping[str, Any], finding: Mapping[str, Any], url: str = "") -> bool:
    """Return whether a cited document is an industry/market source.

    Industry reports are intentionally not required to contain the issuer's
    code or name.  They still must match the cited period or numeric fact.
    """

    context = " ".join(
        str(value or "")
        for value in (
            claim.get("source_context"),
            claim.get("source_kind"),
            claim.get("statement"),
            finding.get("finding"),
        )
    )
    if any(marker in context for marker in ("行业协会", "工业协会", "协会信息中心", "行业报告", "市场报告")):
        return True
    # Macro/statistical sources are valid evidence for industry prices,
    # production, sales and policy claims but will naturally not mention the
    # issuer's stock code.  Keep the exemption narrow: it applies only when
    # the prose itself is an industry observation and the host is an
    # identifiable public data publisher.
    host = str(urlparse(str(url or claim.get("source_ref") or "")).hostname or "").casefold()
    macro_hosts = (
        "stats.gov.cn",
        "gov.cn",
        "moa.gov.cn",
        "miit.gov.cn",
        "nea.gov.cn",
        "cif.mofcom.gov.cn",
        "cbmf.org",
        "cnfa.com.cn",
        "semi.org.cn",
    )
    industry_terms = ("行业", "市场", "价格", "销量", "产量", "出口", "进口", "政策", "协会", "宏观")
    return any(host == suffix or host.endswith("." + suffix) for suffix in macro_hosts) and any(
        term in context for term in industry_terms
    )


def _report_period_tokens(value: Any) -> set[str]:
    raw = str(value or "").strip().casefold().replace(" ", "")
    if not raw:
        return set()
    year_match = re.search(r"(20\d{2})", raw)
    if not year_match:
        return {raw}
    year = year_match.group(1)
    tokens = {raw, year}
    if "q1" in raw or "一季" in raw:
        tokens.update(
            {
                f"{year}q1",
                f"{year}年一季度",
                f"{year}年第一季度",
                f"{year}年3月31日",
                f"{year}-03-31",
                f"{year}0331",
            }
        )
    elif "q2" in raw or "h1" in raw or "上半年" in raw or "半年度" in raw or "中报" in raw:
        tokens.update(
            {
                f"{year}q2",
                f"{year}h1",
                f"{year}年上半年",
                f"{year}年半年度",
                f"{year}年中报",
                f"{year}年6月30日",
                f"{year}-06-30",
                f"{year}0630",
            }
        )
    elif "q3" in raw or "三季" in raw:
        tokens.update(
            {
                f"{year}q3",
                f"{year}年三季度",
                f"{year}年前三季度",
                f"{year}年9月30日",
                f"{year}-09-30",
                f"{year}0930",
            }
        )
    elif "q4" in raw or "h2" in raw or "下半年" in raw or "年度" in raw or "年报" in raw or "fy" in raw:
        tokens.update(
            {
                f"{year}q4",
                f"{year}h2",
                f"{year}年度",
                f"{year}年年报",
                f"{year}年12月31日",
                f"{year}-12-31",
                f"{year}1231",
            }
        )
    parsed = _date_value(raw)
    if parsed:
        tokens.update({parsed.isoformat(), f"{parsed.year}年{parsed.month}月{parsed.day}日"})
    raw_tokens = {token for token in tokens if token}
    return raw_tokens | {re.sub(r"[\s./-]", "", token) for token in raw_tokens}


def _claim_numbers(claim: Mapping[str, Any], finding: Mapping[str, Any]) -> set[str]:
    # The finding can contain unrelated figures; it must not rescue a wrong
    # number in the actual claim. Dates and issuer identifiers are not facts.
    text = str(claim.get("statement") or finding.get("finding") or "")
    text = re.sub(r"(?i)^[036]\d{5}(?=\s+20\d{2}(?:[-/年]|Q|H))", "", text)
    text = re.sub(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}|20\d{2}年\d{1,2}月(?:\d{1,2}日)?", "", text)
    text = re.sub(r"(?i)(?<![a-z])(?:q[1-4]|h[12])(?!\d)", "", text)
    text = re.sub(r"(?:代码\s*[：:]?\s*|[（(])[036]\d{5}[）)]?", "", text)
    numbers: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9])[-+]?\d+(?:,\d{3})*(?:\.\d+)?", text):
        raw = match.group().lstrip("+").replace(",", "")
        unsigned = raw.lstrip("-")
        if unsigned.isdigit() and 1900 <= int(unsigned) <= 2100:
            if not re.match(r"\s*(?:元|万|亿|%|％|倍|股|吨)", text[match.end() :]):
                continue
        numbers.add(raw)
    return numbers


def _source_text(body: bytes, content_type: str) -> str:
    """Decode a small JSON/text provenance body for the fact gate."""

    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1).strip("\"'") if charset_match else "utf-8"
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    if "json" in content_type.casefold():
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            # Keep the raw response so malformed JSON fails the same identity /
            # period-or-field gate instead of being silently accepted.
            return text
    return text


def _structured_period_tokens(claim: Mapping[str, Any], finding: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for value in (
        claim.get("report_period"),
        finding.get("report_period"),
        claim.get("statement"),
        finding.get("finding"),
    ):
        tokens.update(_report_period_tokens(value))
    return tokens


_REPORT_PERIOD_MARKER_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:"
    r"(?:年\s*)?(?:q(?P<q>[1-4])|h(?P<h>[12])|(?P<fy>fy))"
    r"|年?\s*(?P<quarter>第?[一二三四1-4]\s*季度|[一二三四1-4]\s*季报)"
    r"|年?\s*(?P<half>上半年|下半年|半年度|半年报|中报)"
    r"|年?\s*(?P<annual>年度报告?|年报|全年|年末)"
    r")",
    re.IGNORECASE,
)
_REPORT_PERIOD_RANGE_RE = re.compile(
    r"(?P<year>20\d{2})年?\s*"
    r"(?:1\s*月\s*1\s*[日号]?\s*)?"
    r"(?P<start>1[0-2]|[1-9])\s*(?:月\s*)?"
    r"(?:-|至|到|—|–|~|～)\s*"
    r"(?P<end>1[0-2]|[1-9])\s*月",
    re.IGNORECASE,
)
_FY_PREFIX_RE = re.compile(r"^fy(?P<year>20\d{2})$", re.IGNORECASE)
_EXPLICIT_DATE_TOKEN_RE = re.compile(
    r"(?<!\d)(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|20\d{2}年\d{1,2}月\d{1,2}日|20\d{6})(?!\d)"
)
_PUBLICATION_DATE_PREFIX_RE = re.compile(
    r"(?:披露日期?|发布日期?|公告日期|发布时间|发布于|发表于|刊登于|更新时间|更新于|"
    r"检索日期?|检索于|抓取日期?|抓取于|采集日期?|采集于|列示于|报道于|"
    r"disclosure\s+date|publication\s+date|published\s+(?:on|at)?|"
    r"posted\s+(?:on|at)?|updated\s+(?:on|at)?)$",
    re.IGNORECASE,
)


def _period_marker_ends(value: Any) -> list[date]:
    """Extract explicit quarter/half/year or month-range reporting periods."""

    text = str(value or "").strip().casefold().replace(" ", "")
    if not text:
        return []
    ends: list[date] = []
    if match := _FY_PREFIX_RE.fullmatch(text):
        ends.append(date(int(match.group("year")), 12, 31))
    for match in _REPORT_PERIOD_MARKER_RE.finditer(text):
        year = int(match.group("year"))
        quarter = match.group("q")
        if quarter is None and match.group("quarter"):
            token = match.group("quarter")
            quarter = next((str(index) for index, marker in enumerate("一二三四", 1) if marker in token), None)
            if quarter is None:
                digit = re.search(r"[1-4]", token)
                quarter = digit.group(0) if digit else None
        if quarter:
            month = int(quarter) * 3
            next_month = date(year + (month == 12), month % 12 + 1, 1)
            ends.append(date.fromordinal(next_month.toordinal() - 1))
            continue
        half = match.group("h")
        if half is None and match.group("half"):
            half = (
                "1" if any(marker in match.group("half") for marker in ("上半年", "半年度", "半年报", "中报")) else "2"
            )
        if half:
            ends.append(date(year, 6 if half == "1" else 12, 30 if half == "1" else 31))
            continue
        if match.group("fy") or match.group("annual"):
            ends.append(date(year, 12, 31))
    for match in _REPORT_PERIOD_RANGE_RE.finditer(text):
        year = int(match.group("year"))
        month = int(match.group("end"))
        next_month = date(year + (month == 12), month % 12 + 1, 1)
        ends.append(date.fromordinal(next_month.toordinal() - 1))
    return ends


def _specific_period_end(value: Any) -> date | None:
    """Return a period end while ignoring publication dates in source titles."""

    text = str(value or "").strip()
    if not text:
        return None
    # A report marker is authoritative over nearby publication dates, e.g.
    # ``2026年半年度报告（披露日期 2026-08-25）`` means 2026-06-30.
    marker_ends = _period_marker_ends(text)
    if marker_ends:
        return max(marker_ends)
    compact = re.sub(r"\s+", "", text).casefold()
    if re.fullmatch(r"20\d{2}年?", compact):
        return date(int(re.search(r"20\d{2}", compact).group(0)), 12, 31)
    for match in _EXPLICIT_DATE_TOKEN_RE.finditer(text):
        prefix = re.sub(r"[\s\u3000（(【\[]+$", "", text[: match.start()])
        if _PUBLICATION_DATE_PREFIX_RE.search(prefix):
            continue
        parsed = _date_value(match.group(0))
        if parsed:
            # A leading ISO date is commonly the period-end prefix in a fact
            # statement.  Dates embedded after publication labels are skipped;
            # non-quarter-end dates are treated as publication/as-of dates and
            # do not silently become a reporting-period assertion.
            if (match.start() == 0 or not prefix) and (parsed.month, parsed.day) in {
                (3, 31),
                (6, 30),
                (9, 30),
                (12, 31),
            }:
                return parsed
    return None


def _period_end_tokens(period: date) -> set[str]:
    """Return canonical body tokens for one reporting-period endpoint."""

    year = str(period.year)
    tokens = set(_report_period_tokens(period.isoformat()))
    if period.month == 3 and period.day == 31:
        tokens.update(_report_period_tokens(f"{year}Q1"))
    elif period.month == 6 and period.day == 30:
        tokens.update(_report_period_tokens(f"{year}H1"))
    elif period.month == 9 and period.day == 30:
        tokens.update(_report_period_tokens(f"{year}Q3"))
    elif period.month == 12 and period.day == 31:
        tokens.update(_report_period_tokens(f"{year}FY"))
        tokens.update(_report_period_tokens(f"{year}年度"))
    return tokens


def _period_marker_tokens(value: Any, period: date) -> set[str]:
    """Extract only the marker that names ``period``; ignore nearby dates."""

    text = str(value or "")
    tokens: set[str] = set()
    if _FY_PREFIX_RE.fullmatch(text.strip()):
        tokens.update(_report_period_tokens(text))
    for match in _REPORT_PERIOD_MARKER_RE.finditer(text):
        if period in _period_marker_ends(match.group(0)):
            tokens.update(_report_period_tokens(match.group(0)))
    for match in _REPORT_PERIOD_RANGE_RE.finditer(text):
        if period in _period_marker_ends(match.group(0)):
            tokens.add(match.group(0).casefold().replace(" ", ""))
    for match in _EXPLICIT_DATE_TOKEN_RE.finditer(text):
        if _date_value(match.group(0)) == period:
            tokens.update(_report_period_tokens(match.group(0)))
    compact = re.sub(r"\s+", "", text).casefold()
    if not tokens and compact == str(period.year):
        tokens.update(_report_period_tokens(f"{period.year}年度"))
    return tokens


def _is_filing_period_value(value: Any) -> bool:
    """Return whether a value names a quarter/half/year filing period."""

    text = str(value or "").strip()
    if not text:
        return False
    if _FY_PREFIX_RE.fullmatch(text) or _REPORT_PERIOD_MARKER_RE.search(text):
        return True
    for match in _REPORT_PERIOD_RANGE_RE.finditer(text):
        if int(match.group("end")) in {3, 6, 9, 12}:
            return True
    for match in _EXPLICIT_DATE_TOKEN_RE.finditer(text):
        parsed = _date_value(match.group(0))
        if parsed and (parsed.month, parsed.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            return True
    return False


def _specific_period_tokens(claim: Mapping[str, Any], finding: Mapping[str, Any]) -> set[str]:
    # Prefer the claim's own declared period.  Finding text is a fallback for
    # legacy rows that omitted it; unioning both lets a wrong-period finding
    # satisfy the body check for an otherwise explicit claim.
    primary = [claim.get("report_period"), claim.get("statement")]
    expected = next((period for value in primary if (period := _specific_period_end(value)) is not None), None)
    values = (
        primary
        if expected is not None
        else [
            claim.get("source_context"),
            finding.get("report_period"),
            finding.get("finding"),
        ]
    )
    tokens: set[str] = set()
    for value in values:
        period = _specific_period_end(value)
        if period is None or expected is not None and period != expected:
            continue
        tokens.update(_period_end_tokens(period))
        tokens.update(_period_marker_tokens(value, period))
    return tokens


def _source_context_period_issue(claim: Mapping[str, Any], finding: Mapping[str, Any]) -> str | None:
    """Reject a filing title/context that names a different reporting period."""

    expected = next(
        (
            period
            for value in (claim.get("report_period"), claim.get("statement"))
            if (period := _specific_period_end(value)) is not None
        ),
        None,
    )
    if expected is None or not any(
        _is_filing_period_value(value) for value in (claim.get("report_period"), claim.get("statement"))
    ):
        return None
    contexts = (
        ("claim source context", claim.get("source_context")),
        ("finding title", finding.get("title")),
        ("finding report period", finding.get("report_period")),
        ("finding text", finding.get("finding")),
    )
    for label, value in contexts:
        actual = _specific_period_end(value)
        if actual is not None and not _is_filing_period_value(value):
            continue
        # A later filing can legitimately quote an earlier comparative period;
        # an earlier filing cannot prove a claim about a later period.  Keep
        # the directionally safe check so historical facts in a current report
        # are not rejected while future-period misbindings still fail closed.
        if actual is not None and actual < expected:
            return (
                f"{label} names report period ending {actual.isoformat()}, "
                f"but the claim requires {expected.isoformat()}"
            )
    return None


def _period_matches(text: str, tokens: set[str]) -> bool:
    """Match a declared period without letting a bare year mask a mismatch."""

    if not tokens:
        return False
    compact_text = re.sub(r"[\s./-]", "", text).casefold()
    # ``_report_period_tokens`` intentionally includes a year fallback for
    # year-only claims.  Once a quarter/half/year-end/date token exists, use
    # those specific forms so a different period in the same year cannot pass.
    detailed = {token for token in tokens if not re.fullmatch(r"20\d{2}年?", str(token).casefold())}
    candidates = detailed or tokens
    return any(re.sub(r"[\s./-]", "", str(token)).casefold() in compact_text for token in candidates if token)


def _structured_field_tokens(url: str) -> set[str]:
    try:
        params = parse_qs(urlparse(url).query)
    except ValueError:
        return set()
    fields: set[str] = set()
    for value in params.get("columns", []):
        fields.update(
            token.casefold() for token in re.split(r"[,\s]+", value) if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token)
        )
    return fields


_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?<![\d.])(?P<sign>[+-]?)\s*(?P<number>\d+(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<unit>元/股|元／股|千亿元|百亿元|十亿元|亿元|千万元|百万元|十万元|万元|千元|百元|元|"
    r"亿股|万股|千股|股|万吨|万件|万台|吨|件|台|个百分点|百分点|%|％|倍)"
)
_NUMBER_UNIT_FACTORS = {
    "元/股": 1.0,
    "元／股": 1.0,
    "千亿元": 1e11,
    "百亿元": 1e10,
    "十亿元": 1e9,
    "亿元": 1e8,
    "千万元": 1e7,
    "百万元": 1e6,
    "十万元": 1e5,
    "万元": 1e4,
    "千元": 1e3,
    "百元": 1e2,
    "元": 1.0,
    "亿股": 1e8,
    "万股": 1e4,
    "千股": 1e3,
    "股": 1.0,
    "万吨": 1e4,
    "万件": 1e4,
    "万台": 1e4,
    "吨": 1.0,
    "件": 1.0,
    "台": 1.0,
    "个百分点": 1.0,
    "百分点": 1.0,
    "%": 1.0,
    "％": 1.0,
    "倍": 1.0,
}

# Do not let another row with the same amount prove a financial claim. Keep
# genuinely different fields (parent/consolidated profit, total/segment
# revenue, operating/free cash flow) separate even when their wording overlaps.
_FACT_METRIC_ALIASES = {
    "parent_profit": ("归属于上市公司股东的净利润", "归属于母公司股东的净利润", "归母净利润", "PARENT_NETPROFIT"),
    "adjusted_profit": (
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "扣非归母净利润",
        "扣非净利润",
        "DEDUCT_PARENT_NETPROFIT",
    ),
    "operating_cash": (
        "经营活动产生的现金流量净额",
        "经营活动现金流量净额",
        "经营现金流",
        "NETCASH_OPERATE",
        "operating_cash_flow",
    ),
    "free_cash": ("自由现金流", "free_cash_flow"),
    "cash_flow": ("现金流", "cash flow"),
    "overseas_revenue": ("海外收入", "海外营业收入", "境外收入", "境外营业收入"),
    "domestic_revenue": ("境内收入", "国内收入"),
    "other_revenue": ("其他业务收入", "其他收入"),
    "main_revenue": ("主营业务收入",),
    "revenue": ("营业总收入", "营业收入", "营收", "收入", "TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "REVENUE"),
    "profit": ("净利润", "NETPROFIT", "net_profit"),
    "operating_profit": ("营业利润", "OPERATE_PROFIT"),
    "gross_margin": ("毛利率", "SALE_GPR", "gross_margin"),
    "net_margin": ("净利率", "SALE_NPR", "net_margin"),
    "roe": ("净资产收益率", "ROE"),
    "eps": ("基本每股收益", "每股收益", "EPS"),
    "pe": ("市盈率", "PE"),
    "pb": ("市净率", "PB"),
    "assets": ("资产总额", "总资产", "TOTAL_ASSETS"),
    "liabilities": ("负债总额", "总负债", "TOTAL_LIABILITIES"),
    "receivables": ("应收账款", "ACCOUNTS_RECE"),
    "inventory": ("存货", "INVENTORY"),
    "cash": ("货币资金", "MONETARYFUNDS"),
    "capex": ("购建固定资产、无形资产和其他长期资产支付的现金", "资本开支", "CONSTRUCT_LONG_ASSET", "CAPEX"),
    "rd_expense": ("研发费用", "研发支出", "RDEXPENSE", "研发投入"),
    "npl": ("不良贷款率", "不良率"),
    "coverage": ("拨备覆盖率",),
    "capital": ("核心一级资本充足率",),
    "penetration": ("渗透率",),
    "price": ("价格", "股价"),
}
_FACT_METRIC_NAMES = {alias.casefold(): key for key, aliases in _FACT_METRIC_ALIASES.items() for alias in aliases}
_FACT_METRIC_RE = re.compile(
    "|".join(
        r"(?<![A-Za-z_])" + re.escape(alias) + r"(?![A-Za-z_])"
        if alias.isascii()
        else r"\s*".join(re.escape(char) for char in alias)
        for alias in sorted(_FACT_METRIC_NAMES, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
_TABLE_UNIT_RE = re.compile(
    r'(?:单位\s*[：:]?|["\']unit["\']\s*:\s*["\'])\s*(?:人民币\s*)?'
    r"(千亿元|百亿元|十亿元|亿元|千万元|百万元|十万元|万元|千元|百元|元|%|％|倍)",
    re.IGNORECASE,
)
_CNY_SOURCE_FIELDS = frozenset(
    {
        "total_operate_income",
        "operate_income",
        "parent_netprofit",
        "deduct_parent_netprofit",
        "netprofit",
        "netcash_operate",
        "operate_profit",
        "total_assets",
        "total_liabilities",
        "accounts_rece",
        "inventory",
        "monetaryfunds",
        "construct_long_asset",
    }
)
_RAW_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.+-])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])")


def _number_metric_context(text: str):
    """Return row lookup without joining neighbouring HTML/PDF numeric cells."""

    labels = list(_FACT_METRIC_RE.finditer(text))
    ends = [match.end() for match in labels]

    def context(position: int) -> tuple[str, str]:
        index = bisect_right(ends, position) - 1
        if index < 0:
            return "", ""
        label = labels[index]
        between = text[label.end() : position]
        if len(between) > 240 or "。" in between:
            return "", ""
        alias = re.sub(r"\s+", "", label.group()).casefold()
        # English multi-word labels retain their single space in the index.
        key = _FACT_METRIC_NAMES.get(alias, _FACT_METRIC_NAMES.get(label.group().casefold(), ""))
        return key, alias

    return context


def _structured_number_match(
    text: str,
    numbers: set[str],
    *,
    claim: Mapping[str, Any] | None = None,
    finding: Mapping[str, Any] | None = None,
) -> bool:
    claim_text = str((claim or {}).get("statement") or (finding or {}).get("finding") or "")
    target_matches = list(_NUMBER_WITH_UNIT_RE.finditer(claim_text))
    claim_metric = _number_metric_context(claim_text)
    body_metric = _number_metric_context(text)
    body_matches = list(_NUMBER_WITH_UNIT_RE.finditer(text))
    body_facts = [
        (
            float(match.group("sign") + match.group("number").replace(",", ""))
            * _NUMBER_UNIT_FACTORS[match.group("unit")],
            match.group("unit"),
            body_metric(match.start())[0],
        )
        for match in body_matches
    ]
    # Preserve offsets when masking inline units, so the remaining values can
    # only inherit their actual table header or a known typed source field.
    unitless_text = _NUMBER_WITH_UNIT_RE.sub(lambda match: " " * len(match.group()), text)
    headers = list(_TABLE_UNIT_RE.finditer(text))
    header_ends = [match.end() for match in headers]
    raw_facts: list[tuple[float, str]] = []
    for match in _RAW_NUMBER_RE.finditer(unitless_text):
        raw = float(match.group().replace(",", ""))
        metric, alias = body_metric(match.start())
        raw_facts.append((raw, metric))
        header_index = bisect_right(header_ends, match.start()) - 1
        unit = headers[header_index].group(1) if header_index >= 0 else "元" if alias in _CNY_SOURCE_FIELDS else ""
        if unit and metric:
            body_facts.append((raw * _NUMBER_UNIT_FACTORS[unit], unit, metric))

    def unit_family(unit: str) -> str:
        if unit in {"%", "％"}:
            return "%"
        if unit in {"个百分点", "百分点"}:
            return "百分点"
        if unit in {"元/股", "元／股"}:
            return "元/股"
        return unit[-1]  # 元、股、吨、件、台、倍 are distinct dimensions.

    # Check *every* explicit amount, including its sign and unit family. A
    # percentage cannot prove a PE multiple, and one correct amount cannot
    # prove the other amounts in a compound claim.
    for match in target_matches:
        raw_number = match.group("number").replace(",", "")
        raw_target = float(match.group("sign") + raw_number)
        target_unit = match.group("unit")
        scale = _NUMBER_UNIT_FACTORS[target_unit]
        target = raw_target * scale
        if not math.isfinite(target):
            return False
        decimals = len(raw_number.partition(".")[2])
        # Only allow rounding at the precision actually quoted, not a fixed
        # percentage error that becomes material on large financial amounts.
        tolerance = 0.000_001 if target == 0 else 0.5 * scale * 10 ** (-decimals)
        metric = claim_metric(match.start())[0]
        if not metric:
            return False
        if not any(
            unit_family(target_unit) == unit_family(actual_unit)
            and metric == actual_metric
            and (target < 0) == (actual < 0)
            and abs(target - actual) <= tolerance
            for actual, actual_unit, actual_metric in body_facts
        ):
            return False

    tagged_numbers = {(m.group("sign") + m.group("number")).lstrip("+").replace(",", "") for m in target_matches}
    untagged = numbers - tagged_numbers
    for number in untagged:
        token = float(number)
        metrics = {
            claim_metric(match.start())[0]
            for match in _RAW_NUMBER_RE.finditer(claim_text)
            if float(match.group().replace(",", "")) == token
        } - {""}
        if not metrics or not any(raw == token and metric in metrics for raw, metric in raw_facts):
            return False
    return bool(target_matches or numbers)


def _structured_source_issues(
    body: bytes,
    content_type: str,
    *,
    url: str,
    security_code: str,
    name: str,
    claim: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> list[str]:
    """Check JSON/text fact provenance without requiring an HTML page."""

    text = _source_text(body, content_type)
    visible = re.sub(r"\s+", "", text).casefold()
    if not visible:
        return ["structured source body is empty"]
    code = re.sub(r"\s+", "", str(security_code or "")).casefold()
    normal_name = _normalised_company_name(name)
    if not (_security_code_in_text(text, code) or (normal_name and normal_name in visible)):
        return ["structured source body does not match company code or normalized company name"]
    if period_issue := _source_context_period_issue(claim, finding):
        return [period_issue]
    period_tokens = _specific_period_tokens(claim, finding)
    period_match = _period_matches(text, period_tokens)
    numbers = _claim_numbers(claim, finding)
    number_match = _structured_number_match(
        text,
        numbers,
        claim=claim,
        finding=finding,
    )
    field_match = any(field in visible for field in _structured_field_tokens(url))
    if period_tokens and not period_match:
        return ["structured source body does not match the claimed report period"]
    if numbers and not number_match:
        return ["structured source body does not match the claimed fact number/field"]
    if not period_tokens and not numbers and not field_match:
        return ["structured source body does not match report period or fact number/field"]
    return []


def _import_pymupdf():
    """Load the project's pinned PDF parser lazily for source auditing."""

    try:
        import pymupdf
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"PyMuPDF is unavailable: {type(exc).__name__}") from exc
    return pymupdf


def _extract_pdf_text(body: bytes) -> tuple[str, str | None]:
    """Extract bounded text from a PDF, returning an unverified reason on failure.

    A malformed or image-only PDF must not become a semantic pass.  Returning a
    separate reason lets the audit preserve the existing ``unverified`` status
    for such sources while treating a successfully parsed but mismatching PDF
    as a semantic failure.
    """

    try:
        pymupdf = _import_pymupdf()
        document = pymupdf.open(stream=body, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        return "", f"PDF cannot be parsed ({type(exc).__name__})"
    try:
        if document.page_count < 1:
            return "", "PDF has no pages"
        text_parts: list[str] = []
        text_length = 0
        for page_index, page in enumerate(document):
            if page_index >= _MAX_PDF_PAGES:
                break
            page_text = page.get_text("text")
            if not isinstance(page_text, str):
                page_text = str(page_text or "")
            if not page_text:
                continue
            remaining = _MAX_PDF_TEXT_CHARS - text_length
            if remaining <= 0:
                break
            text_parts.append(page_text[:remaining])
            text_length += min(len(page_text), remaining)
            if text_length >= _MAX_PDF_TEXT_CHARS:
                break
        text = "".join(text_parts)
        if not re.sub(r"\s+", "", text):
            return "", "PDF has no extractable text"
        return text, None
    except Exception as exc:  # noqa: BLE001
        return "", f"PDF text extraction failed ({type(exc).__name__})"
    finally:
        document.close()


def _pdf_text_semantic_issues(
    text: str,
    *,
    source_url: str = "",
    security_code: str,
    name: str,
    claim: Mapping[str, Any],
    finding: Mapping[str, Any],
    require_identity: bool = True,
) -> list[str]:
    """Run the identity, period and fact gate against extracted PDF text."""

    visible = re.sub(r"\s+", "", text).casefold()
    if not visible:
        return ["PDF body has no visible text"]
    if require_identity and not _pdf_company_identity_matches(text, security_code, name):
        # Exchange disclosure paths carry the issuer code even when the PDF
        # text layer drops the heading (a common outcome for generated
        # attachments).  Keep this fallback limited to official market hosts;
        # an arbitrary third-party URL must still prove identity in its body.
        code = re.sub(r"\s+", "", str(security_code or ""))
        official_url_identity = _official_domain(source_url) and _security_code_in_text(str(source_url or ""), code)
        if not official_url_identity:
            return ["PDF text does not match company code or normalized company name"]
    if period_issue := _source_context_period_issue(claim, finding):
        return [period_issue]
    period_tokens = _specific_period_tokens(claim, finding)
    period_match = _period_matches(text, period_tokens)
    numbers = _claim_numbers(claim, finding)
    number_match = _structured_number_match(
        text,
        numbers,
        claim=claim,
        finding=finding,
    )
    if period_tokens and not period_match:
        return ["PDF text does not match the claimed report period"]
    if numbers and not number_match:
        return ["PDF text does not match the claimed fact number"]
    if not period_tokens and not numbers:
        return ["PDF text does not expose a report period or fact number"]
    return []


def _pdf_semantic_issues(
    body: bytes,
    *,
    security_code: str,
    name: str,
    source_url: str = "",
    claim: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> list[str]:
    """Check a PDF body; extraction failures remain explicitly unverified."""

    text, extraction_reason = _extract_pdf_text(body)
    if extraction_reason:
        return [f"PDF text extraction is unverified ({extraction_reason})"]
    return _pdf_text_semantic_issues(
        text,
        source_url=source_url,
        security_code=security_code,
        name=name,
        claim=claim,
        finding=finding,
    )


def _html_semantic_issues(
    body: bytes,
    content_type: str,
    *,
    url: str = "",
    security_code: str,
    name: str,
    report_period: Any,
    claim: Mapping[str, Any],
    finding: Mapping[str, Any],
    require_identity: bool = True,
) -> list[str]:
    """Run the deliberately small HTML identity/content gate."""

    source_text, title, metadata = _html_visible_text(body, content_type)
    visible = re.sub(r"\s+", "", source_text)
    if not visible:
        return ["HTML body has no visible text"]
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1).strip("\"'") if charset_match else "utf-8"
    try:
        raw_text = body.decode(charset, errors="replace").casefold()
    except LookupError:
        raw_text = body.decode("utf-8", errors="replace").casefold()
    first_text = f"{title}{visible[:600]}"
    title_text = title
    challenge_markup = re.search(r"<(?:form|input)\b[^>]*(?:captcha|验证码|安全验证)", raw_text)
    if re.search(
        r"(?:404|page\s*not\s*found|not\s*found|页面不存在|内容不存在|找不到页面|链接失效)",
        title_text,
    ) or re.search(r"(?:id|class)\s*=\s*[\"'][^\"']*(?:404|not[-_ ]?found)[^\"']*[\"']", raw_text):
        return ["HTML appears to be a soft-404 page"]
    if re.search(
        r"(?:captcha|verify\s+you\s+are\s+human|人机验证|验证码|安全验证|<input[^>]+(?:captcha|验证码))",
        first_text,
    ):
        return ["HTML appears to be a login or CAPTCHA challenge"]
    if challenge_markup:
        return ["HTML appears to be a login or CAPTCHA challenge"]
    if re.search(r"(?:登录|登陆|sign\s*in|log\s*in|login|authentication required)", title) or re.search(
        r"<form[^>]+(?:login|sign[-_ ]?in)", raw_text
    ):
        return ["HTML appears to be a login page"]

    code = re.sub(r"\s+", "", str(security_code or "")).casefold()
    normal_name = _normalised_company_name(name)
    identity_text = f"{title} {metadata} {source_text}"
    compact_identity = re.sub(r"\s+", "", identity_text)
    if require_identity and not (
        _security_code_in_text(identity_text, code) or (normal_name and normal_name in compact_identity)
    ):
        official_url_identity = _official_domain(url) and _security_code_in_text(str(url or ""), code)
        if not official_url_identity:
            return ["HTML正文未匹配公司代码或规范化公司名"]

    if period_issue := _source_context_period_issue(claim, finding):
        return [period_issue]
    period_tokens = _report_period_tokens(report_period) if _specific_period_end(report_period) is not None else set()
    # Legacy Codex reviews did not carry a separate report_period field.  The
    # statement itself still declares the period, so use those tokens rather
    # than treating every HTML claim as undated.
    period_tokens.update(_specific_period_tokens(claim, finding))
    period_match = _period_matches("".join((title, metadata, visible)), period_tokens)
    numbers = _claim_numbers(claim, finding)
    number_match = _structured_number_match(
        source_text,
        numbers,
        claim=claim,
        finding=finding,
    )
    if period_tokens and not period_match:
        return ["HTML正文未匹配声明的报告期"]
    if numbers and not number_match:
        return ["HTML正文未匹配声明的关键数字"]
    if not period_tokens and not numbers:
        return ["HTML正文未提供可核验的报告期或关键数字"]
    return []


class UnsafeUrlError(ValueError):
    """Raised when a URL can reach a non-public network destination."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


def _public_http_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in url)
    ):
        return ""
    try:
        _ = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    return "" if not address.is_global else url


def _resolve_public_addresses(url: str) -> list[str]:
    public_url = _public_http_url(url)
    if not public_url:
        raise UnsafeUrlError("URL is not public HTTP(S)")
    parsed = urlparse(public_url)
    host = str(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cache_key = (host.casefold(), port)
    with _dns_cache_lock:
        cached = _dns_cache.get(cache_key)
        cached_error = _dns_error_cache.get(cache_key)
    if cached is not None:
        return list(cached)
    if cached_error:
        raise OSError(cached_error)
    try:
        addresses = {
            str(sockaddr[0]).split("%", 1)[0]
            for family, _socket_type, _protocol, _canonical_name, sockaddr in socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            if family in {socket.AF_INET, socket.AF_INET6} and sockaddr
        }
    except OSError as error:
        with _dns_cache_lock:
            _dns_error_cache[cache_key] = str(error)[:240]
        raise
    if not addresses:
        reason = f"DNS returned no A/AAAA addresses for {host}"
        with _dns_cache_lock:
            _dns_error_cache[cache_key] = reason
        raise OSError(reason)
    non_public = sorted(address for address in addresses if not ipaddress.ip_address(address).is_global)
    if non_public:
        reason = f"DNS resolved to non-public address(es): {','.join(non_public)}"
        with _dns_cache_lock:
            _dns_error_cache[cache_key] = reason
        raise UnsafeUrlError(reason)
    resolved = tuple(sorted(addresses))
    with _dns_cache_lock:
        _dns_cache[cache_key] = resolved
    return list(resolved)


def _official_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _OFFICIAL_DOMAIN_SUFFIXES)


def _throttle_host(url: str) -> None:
    """Keep a burst of source-audit requests from tripping mirror limits."""

    host = (urlparse(url).netloc or "").casefold()
    if not host:
        return
    with _host_throttle_lock:
        now = time.monotonic()
        next_request = _host_next_request.get(host, now)
        delay = max(0.0, next_request - now)
        _host_next_request[host] = max(now, next_request) + _HOST_THROTTLE_SECONDS
    if delay:
        time.sleep(delay)


def _retry_delay(attempt: int, response: Any = None) -> float:
    """Return a short bounded backoff for a transient origin response."""

    retry_after = ""
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            retry_after = str(headers.get("retry-after") or "").strip()
    try:
        delay = float(retry_after) if retry_after else 0.0
    except ValueError:
        delay = 0.0
    return min(2.0, max(delay, 0.12 * (2**attempt)))


def _check_url(url: str, *, timeout: float, max_bytes: int, max_redirects: int = 5) -> dict[str, Any]:
    base = {"url": url, "official_market_domain": _official_domain(url)}
    # Source verification must observe the public origin directly.  A stale
    # workstation HTTP(S)_PROXY can otherwise turn a reachable filing into a
    # local timeout or proxy challenge, producing a false yellow warning.
    opener = urllib.request.build_opener(_NoRedirectHandler(), urllib.request.ProxyHandler({}))
    current_url = url
    redirect_count = 0
    attempt = 0
    while True:
        try:
            _throttle_host(current_url)
            resolved_addresses = _resolve_public_addresses(current_url)
            request = urllib.request.Request(
                current_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; DS-DCF-source-audit/1.0)",
                    "Accept": "text/html,application/pdf,application/json;q=0.9,*/*;q=0.8",
                },
            )
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status or 200)
                content_type = str(response.headers.get("content-type") or "")[:160]
                is_pdf_response = "pdf" in content_type.casefold() or urlparse(current_url).path.casefold().endswith(
                    ".pdf"
                )
                read_limit = max(max_bytes, _MAX_PDF_BYTES) if is_pdf_response else max_bytes
                # Reading one extra byte lets us fail closed on a PDF that hit
                # the safety cap instead of accidentally parsing a recoverable
                # but incomplete prefix as a complete document.
                body = response.read(read_limit + 1 if is_pdf_response else read_limit)
                body_truncated = is_pdf_response and len(body) > read_limit
                if body_truncated:
                    body = body[:read_limit]
                # urllib deliberately does not decode Content-Encoding.  A
                # number of exchange/CDN disclosure endpoints label a gzip
                # compressed PDF as ``text/html``; parsing the compressed
                # bytes creates false identity/period warnings.  Decode with
                # the same bounded cap used for the origin body and keep the
                # truncation flag fail-closed for PDFs.
                content_encoding = str(response.headers.get("content-encoding") or "").casefold()
                if "gzip" in content_encoding or body[:2] == b"\x1f\x8b":
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                            decoded = compressed.read(read_limit + 1)
                        if len(decoded) > read_limit:
                            body_truncated = is_pdf_response
                            body = decoded[:read_limit]
                        else:
                            body = decoded
                    except (OSError, EOFError):
                        # Keep the original response; semantic verification
                        # will report an explicit unverified/non-text body.
                        pass
                return {
                    **base,
                    "result": "ok" if 200 <= status < 400 else "failed",
                    "reachability": "reachable" if 200 <= status < 400 else "failed",
                    "body_retrieved": 200 <= status < 400,
                    "status": status,
                    "final_url": current_url,
                    "redirect_count": redirect_count,
                    "resolved_addresses": resolved_addresses,
                    "content_type": content_type,
                    "bytes_checked": len(body),
                    "body_truncated": body_truncated,
                    # Consumed by ``audit`` and removed before serialization.
                    "_body": body,
                }
        except urllib.error.HTTPError as error:
            status = int(error.code)
            if status in _REDIRECT_HTTP_STATUSES:
                location = str(error.headers.get("location") or "").strip()
                error.close()
                if not location:
                    return {
                        **base,
                        "result": "failed",
                        "reachability": "failed",
                        "body_retrieved": False,
                        "status": status,
                        "error": "redirect response has no Location header",
                    }
                if redirect_count >= max_redirects:
                    return {
                        **base,
                        "result": "failed",
                        "reachability": "failed",
                        "body_retrieved": False,
                        "status": status,
                        "error": f"redirect limit exceeded ({max_redirects})",
                    }
                current_url = urljoin(current_url, location)
                redirect_count += 1
                attempt = 0
                continue
            if status in _TRANSIENT_HTTP_STATUSES and attempt < _MAX_FETCH_ATTEMPTS - 1:
                delay = _retry_delay(attempt, error)
                error.close()
                time.sleep(delay)
                attempt += 1
                continue
            result = "blocked" if status in _BLOCKED_HTTP_STATUSES else "failed"
            reason = str(error.reason or error)[:240]
            error.close()
            return {
                **base,
                "result": result,
                "reachability": result,
                "body_retrieved": False,
                "status": status,
                "error": reason,
            }
        except UnsafeUrlError as error:
            return {
                **base,
                "result": "invalid",
                "reachability": "invalid",
                "body_retrieved": False,
                "status": 0,
                "final_url": current_url,
                "error": str(error)[:240],
            }
        except (OSError, urllib.error.URLError, ValueError) as error:
            retryable_timeout = isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout))
            if retryable_timeout and attempt < 1:
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            return {
                **base,
                "result": "failed",
                "reachability": "failed",
                "body_retrieved": False,
                "status": 0,
                "error": str(error)[:240],
            }


def audit(
    merged_path: Path,
    output_path: Path,
    *,
    workers: int = 16,
    timeout: float = 15.0,
    max_bytes: int = 262_144,
) -> dict[str, Any]:
    # DNS answers are safe to reuse within one bounded audit run and avoid
    # resolving the same disclosure host once per URL.  Clear any values left
    # by unit tests or a previous in-process audit invocation.
    with _dns_cache_lock:
        _dns_cache.clear()
        _dns_error_cache.clear()
    merged_bytes = merged_path.read_bytes()
    payload = json.loads(merged_bytes.decode("utf-8"))
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("AI screening packets are missing")
    projection_sha256, projection_counts = source_semantic_projection_sha256(payload)
    references: dict[str, set[tuple[str, str]]] = {}
    detailed_bindings: dict[str, list[dict[str, Any]]] = {}
    semantic_claims: list[dict[str, Any]] = []
    semantic_issues: list[dict[str, str]] = []
    semantic_failed_keys: set[int] = set()
    semantic_unverified_keys: set[int] = set()
    # Avoid counting the same company's published source twice when a finding
    # is both selected by a claim and emitted in ``search_findings``.  The URL
    # remains bound to the company; different companies are never collapsed.
    semantic_claim_urls: set[tuple[str, str]] = set()
    claim_count = 0
    semantic_claim_count = 0
    invalid_claim_urls: list[dict[str, str]] = []
    company_states: dict[str, dict[str, Any]] = {}
    finding_ids_with_claim_urls: dict[str, set[str]] = {}

    def company_state(code: str, name: str) -> dict[str, Any]:
        state = company_states.setdefault(
            code,
            {
                "security_code": code,
                "name": name,
                "has_review": False,
                "type_keys": set(),
                "finding_ids": set(),
                "referenced_finding_ids": set(),
                "searched_no_source_finding_ids": set(),
                "referenced_no_source_finding_ids": set(),
                "semantic_keys": set(),
                "semantic_passed_keys": set(),
                "semantic_failed_keys": set(),
                "semantic_unverified_keys": set(),
                "urls": set(),
            },
        )
        if name and not state["name"]:
            state["name"] = name
        return state

    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("AI screening packet is not an object")
        packet_code = str(packet.get("security_code") or "").strip()
        packet_name = str(packet.get("name") or packet.get("security_name") or "").strip()
        packet_type_key = str(packet.get("type_key") or "").strip()
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            state = company_state(packet_code, packet_name)
            state["type_keys"].add(packet_type_key)
            continue
        code = packet_code
        name = packet_name
        type_key = packet_type_key
        state = company_state(code, name)
        state["has_review"] = True
        state["type_keys"].add(type_key)
        findings = review.get("search_findings") if isinstance(review.get("search_findings"), list) else []
        findings_by_id = {
            str(finding.get("id") or "").strip(): finding
            for finding in findings
            if isinstance(finding, Mapping) and str(finding.get("id") or "").strip()
        }
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            finding_id = str(finding.get("id") or "").strip()
            if finding_id:
                state["finding_ids"].add(finding_id)
        claims = review.get("claims") if isinstance(review.get("claims"), list) else []
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                continue
            if is_search_provenance_claim(claim):
                # This row is a query attestation, not a published fact. It
                # is intentionally omitted from the public semantic graph.
                continue
            claim_count += 1
            finding_id = str(claim.get("search_finding_id") or "").strip()
            finding = findings_by_id.get(finding_id)
            claim_urls = claim_source_urls(claim)
            if finding_id:
                state["referenced_finding_ids"].add(finding_id)
                # A finding without a URL is a completed search attempt, not
                # semantic evidence.  Keep it visible as searched_no_source.
                if not claim_urls:
                    if not finding or not finding_source_url(finding):
                        state["searched_no_source_finding_ids"].add(finding_id)
                        state["referenced_no_source_finding_ids"].add(finding_id)
                    continue
                finding_ids_with_claim_urls.setdefault(code, set()).add(finding_id)
                finding_url = finding_source_url(finding) if finding else ""
                for url in claim_urls:
                    public_url = _public_http_url(url)
                    binding = {
                        "security_code": code,
                        "name": name,
                        "type_key": type_key,
                        "claim_index": claim_index,
                        "search_finding_id": finding_id,
                        "url": url,
                        "kind": "claim",
                    }
                    detailed_bindings.setdefault(url, []).append(binding)
                    state["urls"].add(url)
                    semantic_key = semantic_claim_count
                    semantic_claim_count += 1
                    state["semantic_keys"].add(semantic_key)
                    semantic_claim_urls.add((code, url))
                    if not public_url:
                        references.setdefault(url, set()).add((code, type_key))
                        semantic_claims.append(
                            {
                                "key": semantic_key,
                                "security_code": code,
                                "name": name,
                                "type_key": type_key,
                                "claim_index": claim_index,
                                "finding_index": None,
                                "search_finding_id": finding_id,
                                "url": url,
                                "published_at": finding.get("published_at") if finding else None,
                                "report_period": finding.get("report_period") if finding else None,
                                "claim": claim,
                                "finding": finding or {},
                            }
                        )
                        continue
                    references.setdefault(url, set()).add((code, type_key))
                    if not finding or finding_url != url:
                        semantic_failed_keys.add(semantic_key)
                        state["semantic_failed_keys"].add(semantic_key)
                        semantic_issues.append(
                            {
                                "security_code": code,
                                "name": name,
                                "type_key": type_key,
                                "claim_index": str(claim_index),
                                "search_finding_id": finding_id,
                                "source": url,
                                "reason": "claim search_finding_id is missing or points to a different URL",
                            }
                        )
                        continue
                    semantic_claims.append(
                        {
                            "key": semantic_key,
                            "security_code": code,
                            "name": name,
                            "type_key": type_key,
                            "claim_index": claim_index,
                            "finding_index": None,
                            "search_finding_id": finding_id,
                            "url": url,
                            "published_at": finding.get("published_at"),
                            "report_period": finding.get("report_period"),
                            "claim": claim,
                            "finding": finding,
                        }
                    )
                continue
            # Structured/fact claims can legitimately use local evidence IDs,
            # but a cited URL is still a published source and must pass the
            # same semantic gate.  Do not let a local fact ID make a PDF or a
            # login page look like a semantic pass.
            if not claim_urls:
                continue
            for url in claim_urls:
                binding = {
                    "security_code": code,
                    "name": name,
                    "type_key": type_key,
                    "claim_index": claim_index,
                    "search_finding_id": finding_id,
                    "url": url,
                    "kind": "claim",
                }
                detailed_bindings.setdefault(url, []).append(binding)
                state["urls"].add(url)
                references.setdefault(url, set()).add((code, type_key))
                semantic_key = semantic_claim_count
                semantic_claim_count += 1
                state["semantic_keys"].add(semantic_key)
                semantic_claim_urls.add((code, url))
                semantic_claims.append(
                    {
                        "key": semantic_key,
                        "security_code": code,
                        "name": name,
                        "type_key": type_key,
                        "claim_index": claim_index,
                        "finding_index": None,
                        "search_finding_id": "",
                        "url": url,
                        "published_at": None,
                        "report_period": None,
                        "claim": claim,
                        "finding": {},
                    }
                )

        # Search findings are themselves published in native company research;
        # audit their URLs even when no prose claim selected that finding.
        for finding_index, finding in enumerate(findings):
            if not isinstance(finding, Mapping):
                continue
            url = finding_source_url(finding)
            if not url:
                finding_id = str(finding.get("id") or "").strip()
                if finding_id and finding_id not in finding_ids_with_claim_urls.get(code, set()):
                    state["searched_no_source_finding_ids"].add(finding_id)
                    if finding_id in state["referenced_finding_ids"]:
                        state["referenced_no_source_finding_ids"].add(finding_id)
                continue
            public_url = _public_http_url(url)
            finding_id = str(finding.get("id") or "").strip()
            if finding_id:
                # Search findings are part of native company research even
                # when no prose claim selected them; bind their published URL
                # to the company-level coverage contract as well.
                state["referenced_finding_ids"].add(finding_id)
            binding = {
                "security_code": code,
                "name": name,
                "type_key": type_key,
                "claim_index": None,
                "finding_index": finding_index,
                "search_finding_id": finding_id,
                "url": url,
                "kind": "search_finding",
            }
            detailed_bindings.setdefault(url, []).append(binding)
            state["urls"].add(url)
            if public_url:
                references.setdefault(public_url, set()).add((code, type_key))
            else:
                invalid_claim_urls.append(
                    {
                        "security_code": code,
                        "name": name,
                        "type_key": type_key,
                        "claim_index": "",
                        "search_finding_id": finding_id,
                        "source": url,
                        "reason": "URL is not public HTTP(S)",
                    }
                )
            references.setdefault(url, set()).add((code, type_key))
            if (code, url) not in semantic_claim_urls:
                semantic_key = semantic_claim_count
                semantic_claim_count += 1
                state["semantic_keys"].add(semantic_key)
                semantic_claim_urls.add((code, url))
                semantic_claims.append(
                    {
                        "key": semantic_key,
                        "security_code": code,
                        "name": name,
                        "type_key": type_key,
                        "claim_index": None,
                        "finding_index": finding_index,
                        "search_finding_id": finding_id,
                        "url": url,
                        "published_at": finding.get("published_at"),
                        "report_period": finding.get("report_period"),
                        "claim": {
                            "statement": finding.get("finding"),
                            "source_ref": url,
                        },
                        "finding": finding,
                    }
                )

    def checked_url(url: str) -> dict[str, Any]:
        result = _check_url(url, timeout=timeout, max_bytes=max_bytes)
        if not _public_http_url(url):
            result.update(
                {
                    "result": "invalid",
                    "reachability": "invalid",
                    "body_retrieved": False,
                    "status": 0,
                    "error": result.get("error") or "URL is not public HTTP(S)",
                }
            )
        return result

    # A full queue can contain thousands of large filing PDFs.  Consume the
    # mapped results as they arrive and move each body to disk immediately;
    # retaining ``list(executor.map(...))`` first would briefly hold every
    # 12MB safety-capped PDF in the Python heap.
    body_dir = Path(tempfile.mkdtemp(prefix="ds-dcf-source-audit-"))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        urls = sorted(references)
        url_iter = iter(enumerate(urls))
        futures: dict[Any, int] = {}

        def submit_next() -> None:
            try:
                index, url = next(url_iter)
            except StopIteration:
                return
            futures[executor.submit(checked_url, url)] = index

        for _ in range(min(len(urls), max(1, workers) * 2)):
            submit_next()
        while futures:
            completed, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in completed:
                index = futures.pop(future)
                result = future.result()
                body = result.pop("_body", None)
                if isinstance(body, bytes):
                    body_path = body_dir / f"{index}.bin"
                    body_path.write_bytes(body)
                    result["_body_path"] = str(body_path)
                results.append(result)
                submit_next()
    results.sort(key=lambda result: str(result.get("url") or ""))

    def load_body(result: Mapping[str, Any]) -> bytes | None:
        body = result.get("_body")
        if isinstance(body, bytes):
            return body
        body_path = result.get("_body_path")
        if not body_path:
            return None
        try:
            return Path(str(body_path)).read_bytes()
        except OSError:
            return None

    def release_body(result: dict[str, Any]) -> None:
        result.pop("_body", None)
        body_path = result.pop("_body_path", None)
        if body_path:
            try:
                Path(str(body_path)).unlink()
            except OSError:
                pass

    result_by_url = {str(result["url"]): result for result in results}
    published_at_mismatch_count = 0
    report_period_after_publication_count = 0
    blocked_semantic_claim_count = 0
    html_date_checked_count = 0
    # Keep downloaded bodies only until the last claim for that URL has been
    # semantically checked.  A full 994-company audit can contain thousands
    # of large PDF/HTML responses; retaining every body until serialization
    # needlessly grows the process into the gigabytes.
    last_semantic_position: dict[str, int] = {
        str(claim["url"]): position for position, claim in enumerate(semantic_claims)
    }
    previous_url: str | None = None
    previous_position = -1
    # PDF extraction is CPU-heavy for long annual reports.  Keep a small
    # bounded pipeline ahead of the semantic loop so several documents are
    # parsed concurrently without retaining every extracted text in memory.
    pdf_urls: list[str] = []
    seen_pdf_urls: set[str] = set()
    for claim in semantic_claims:
        url = str(claim["url"])
        if url in seen_pdf_urls:
            continue
        result = result_by_url[url]
        content_type = str(result.get("content_type") or "")
        is_pdf = "pdf" in content_type.casefold() or urlparse(url).path.casefold().endswith(".pdf")
        if result.get("result") == "ok" and not is_pdf and not content_type:
            body = load_body(result)
            is_pdf = isinstance(body, bytes) and body.lstrip().startswith(b"%PDF-")
        if result.get("result") == "ok" and is_pdf:
            seen_pdf_urls.add(url)
            pdf_urls.append(url)
    pdf_executor = ThreadPoolExecutor(max_workers=min(max(1, workers), 8)) if pdf_urls else None
    pdf_futures: dict[str, Any] = {}
    pdf_url_set = set(pdf_urls)
    pdf_text_cache: dict[str, tuple[str, str | None]] = {}
    pdf_remaining: dict[str, int] = {
        url: sum(1 for claim in semantic_claims if str(claim["url"]) == url) for url in pdf_urls
    }

    def schedule_pdf(url: str) -> None:
        """Queue one requested PDF, keeping the executor bounded and lazy."""

        if pdf_executor is None or url not in pdf_url_set:
            return
        if url in pdf_futures or url in pdf_text_cache:
            return
        if pdf_remaining.get(url, 0) <= 0:
            return
        body = load_body(result_by_url[url])
        pdf_futures[url] = pdf_executor.submit(_extract_pdf_text, body or b"")

    def prefetch_pdf_claims(position: int) -> None:
        """Prefetch only the next few semantic PDFs, never the whole queue."""

        seen: set[str] = set()
        for candidate in semantic_claims[position : position + 8]:
            url = str(candidate["url"])
            if url in seen:
                continue
            seen.add(url)
            schedule_pdf(url)

    def pdf_text_for(url: str) -> tuple[str, str | None]:
        cached = pdf_text_cache.get(url)
        if cached is not None:
            pdf_remaining[url] -= 1
            if pdf_remaining[url] <= 0:
                pdf_text_cache.pop(url, None)
            return cached
        schedule_pdf(url)
        future = pdf_futures.pop(url, None)
        if future is None:
            # A malformed or out-of-order semantic queue should not crash the
            # whole release audit.  Parse the requested body synchronously;
            # the resulting extraction error remains visible to the caller.
            extracted = _extract_pdf_text(load_body(result_by_url[url]) or b"")
        else:
            extracted = future.result()
        pdf_text_cache[url] = extracted
        pdf_remaining[url] -= 1
        if pdf_remaining[url] <= 0:
            pdf_text_cache.pop(url, None)
        return extracted

    for position, claim in enumerate(semantic_claims):
        prefetch_pdf_claims(position)
        if previous_url is not None and last_semantic_position.get(previous_url) == previous_position:
            release_body(result_by_url[previous_url])
        result = result_by_url[claim["url"]]
        # Mark this claim before any branch below can ``continue``.  The next
        # iteration will release this URL's body once it was its last claim.
        previous_url = str(claim["url"])
        previous_position = position
        common = {
            key: str(claim[key])
            for key in ("security_code", "name", "type_key", "claim_index", "finding_index", "search_finding_id")
        }
        if result.get("result") != "ok":
            semantic_failed_keys.add(int(claim["key"]))
            company_states[claim["security_code"]]["semantic_failed_keys"].add(int(claim["key"]))
            if result.get("result") == "blocked":
                blocked_semantic_claim_count += 1
            semantic_issues.append(
                {
                    **common,
                    "source": str(claim["url"]),
                    "reason": f"source body unavailable for semantic verification ({result.get('result')})",
                }
            )
            previous_url = str(claim["url"])
            previous_position = position
            continue
        content_type = str(result.get("content_type") or "")
        body = load_body(result)
        content_type_folded = content_type.casefold()
        is_pdf = "pdf" in content_type_folded or (isinstance(body, bytes) and body.lstrip().startswith(b"%PDF-"))
        is_html = "html" in content_type_folded and not is_pdf
        is_web_search_claim = bool(claim.get("search_finding_id") or claim.get("finding_index") is not None)
        if is_pdf and isinstance(body, bytes):
            if result.get("body_truncated"):
                semantic_unverified_keys.add(int(claim["key"]))
                company_states[claim["security_code"]]["semantic_unverified_keys"].add(int(claim["key"]))
                semantic_issues.append(
                    {
                        **common,
                        "source": str(claim["url"]),
                        "reason": (
                            "source is reachable but semantic verification is unverified "
                            "(non-HTML PDF body; download exceeded the PDF safety cap)"
                        ),
                    }
                )
                previous_url = str(claim["url"])
                previous_position = position
                continue
            pdf_text, pdf_extraction_reason = pdf_text_for(str(claim["url"]))
            if pdf_extraction_reason:
                semantic_unverified_keys.add(int(claim["key"]))
                company_states[claim["security_code"]]["semantic_unverified_keys"].add(int(claim["key"]))
                semantic_issues.append(
                    {
                        **common,
                        "source": str(claim["url"]),
                        "reason": (
                            "source is reachable but semantic verification is unverified "
                            f"(non-HTML PDF body; {pdf_extraction_reason})"
                        ),
                    }
                )
                previous_url = str(claim["url"])
                previous_position = position
                continue
            pdf_issues = _pdf_text_semantic_issues(
                pdf_text,
                source_url=str(claim["url"]),
                security_code=claim["security_code"],
                name=claim["name"],
                claim=claim["claim"],
                finding=claim["finding"],
                require_identity=not _is_industry_source_claim(claim["claim"], claim["finding"], str(claim["url"])),
            )
            if pdf_issues:
                semantic_failed_keys.add(int(claim["key"]))
                company_states[claim["security_code"]]["semantic_failed_keys"].add(int(claim["key"]))
                semantic_issues.extend(
                    {**common, "source": str(claim["url"]), "reason": reason} for reason in pdf_issues
                )
            continue
        if not is_html and not is_web_search_claim and isinstance(body, bytes):
            stripped = body.lstrip()
            is_structured_text = (
                "json" in content_type_folded
                or content_type_folded.startswith("text/")
                or "javascript" in content_type_folded
                or stripped.startswith((b"{", b"["))
            )
            if is_structured_text:
                structured_issues = _structured_source_issues(
                    body,
                    content_type,
                    url=str(claim["url"]),
                    security_code=claim["security_code"],
                    name=claim["name"],
                    claim=claim["claim"],
                    finding=claim["finding"],
                )
                if structured_issues:
                    semantic_failed_keys.add(int(claim["key"]))
                    company_states[claim["security_code"]]["semantic_failed_keys"].add(int(claim["key"]))
                    semantic_issues.extend(
                        {**common, "source": str(claim["url"]), "reason": reason} for reason in structured_issues
                    )
                continue
        if not is_html or not isinstance(body, bytes):
            semantic_unverified_keys.add(int(claim["key"]))
            company_states[claim["security_code"]]["semantic_unverified_keys"].add(int(claim["key"]))
            semantic_issues.append(
                {
                    **common,
                    "source": str(claim["url"]),
                    "reason": "source is reachable but semantic verification is unverified (non-HTML body)",
                }
            )
            continue
        finding = claim["finding"]
        html_issues = _html_semantic_issues(
            body,
            content_type,
            url=str(claim["url"]),
            security_code=claim["security_code"],
            name=claim["name"],
            report_period=claim.get("report_period"),
            claim=claim["claim"],
            finding=finding,
            require_identity=not _is_industry_source_claim(claim["claim"], finding, str(claim["url"])),
        )
        if html_issues:
            semantic_failed_keys.add(int(claim["key"]))
            company_states[claim["security_code"]]["semantic_failed_keys"].add(int(claim["key"]))
            semantic_issues.extend({**common, "source": str(claim["url"]), "reason": reason} for reason in html_issues)
        actual_date = _article_published_date(body, content_type) if is_html and isinstance(body, bytes) else None
        declared_date = _date_value(claim.get("published_at"))
        period_end = _report_period_end(claim.get("report_period"))
        if is_html and (declared_date or actual_date):
            html_date_checked_count += 1
        if is_html and declared_date and actual_date != declared_date:
            semantic_failed_keys.add(int(claim["key"]))
            company_states[claim["security_code"]]["semantic_failed_keys"].add(int(claim["key"]))
            published_at_mismatch_count += 1
            semantic_issues.append(
                {
                    **common,
                    "source": str(claim["url"]),
                    "reason": (
                        f"declared published_at {declared_date.isoformat()} does not match article date "
                        f"{actual_date.isoformat() if actual_date else 'unavailable'}"
                    ),
                }
            )
        if is_html and period_end and actual_date and period_end > actual_date:
            semantic_failed_keys.add(int(claim["key"]))
            company_states[claim["security_code"]]["semantic_failed_keys"].add(int(claim["key"]))
            report_period_after_publication_count += 1
            semantic_issues.append(
                {
                    **common,
                    "source": str(claim["url"]),
                    "reason": (
                        f"report period ends {period_end.isoformat()} after article date {actual_date.isoformat()}"
                    ),
                }
            )

        previous_url = str(claim["url"])
        previous_position = position

    if previous_url is not None and last_semantic_position.get(previous_url) == previous_position:
        release_body(result_by_url[previous_url])
    if pdf_executor is not None:
        pdf_executor.shutdown(wait=True)

    for result in results:
        result["canonical_url"] = result["url"]
        release_body(result)
        result["references"] = [
            {"security_code": code, "type_key": type_key} for code, type_key in sorted(references[result["url"]])
        ]
        result["bindings"] = detailed_bindings.get(result["url"], [])
        # The detailed issues are the authoritative per-claim status.  Keep a
        # compact URL-level status for operators without duplicating issue text.
        url_failed = result.get("result") != "ok" or any(
            issue.get("source") == result["url"] and "non-HTML" not in issue.get("reason", "")
            for issue in semantic_issues
        )
        url_unverified = any(
            issue.get("source") == result["url"] and "unverified" in issue.get("reason", "")
            for issue in semantic_issues
        )
        result["semantic_status"] = "failed" if url_failed else "unverified" if url_unverified else "pass"
        if result["result"] == "invalid":
            for binding in result.get("bindings", []):
                item: dict[str, Any] = {
                    "security_code": binding.get("security_code", ""),
                    "type_key": binding.get("type_key", ""),
                    "source": result["url"],
                    "reason": str(result.get("error") or "unsafe destination"),
                }
                if binding.get("name"):
                    item["name"] = binding["name"]
                if binding.get("claim_index") is not None and binding.get("search_finding_id"):
                    item["claim_index"] = binding["claim_index"]
                if binding.get("search_finding_id"):
                    item["search_finding_id"] = binding["search_finding_id"]
                invalid_claim_urls.append(item)
    counts = {key: sum(result["result"] == key for result in results) for key in ("ok", "failed", "blocked", "invalid")}
    semantic_failed_count = len(semantic_failed_keys)
    semantic_unverified_count = len(semantic_unverified_keys)
    for state in company_states.values():
        state["semantic_passed_keys"] = (
            state["semantic_keys"] - state["semantic_failed_keys"] - state["semantic_unverified_keys"]
        )
    company_coverage: list[dict[str, Any]] = []
    for code in sorted(company_states):
        state = company_states[code]
        referenced = sorted(state["referenced_finding_ids"])
        failed = len(state["semantic_failed_keys"])
        unverified = len(state["semantic_unverified_keys"])
        passed = len(state["semantic_passed_keys"])
        if not state["has_review"]:
            status = "no_review"
        elif failed:
            status = "failed"
        elif unverified:
            status = "unverified"
        elif state["searched_no_source_finding_ids"]:
            status = "searched_no_source"
        else:
            status = "pass"
        company_coverage.append(
            {
                "security_code": code,
                "name": state["name"],
                "has_review": state["has_review"],
                "type_keys": sorted(state["type_keys"]),
                "finding_count": len(state["finding_ids"]),
                "referenced_finding_ids": referenced,
                "searched_no_source_finding_ids": sorted(state["searched_no_source_finding_ids"]),
                "referenced_no_source_finding_ids": sorted(state["referenced_no_source_finding_ids"]),
                "canonical_url_count": len(state["urls"]),
                "semantic_claim_count": len(state["semantic_keys"]),
                "semantic_passed_count": passed,
                "semantic_failed_count": failed,
                "semantic_unverified_count": unverified,
                "all_referenced_findings_semantic_pass": (
                    bool(referenced)
                    and not state["referenced_no_source_finding_ids"]
                    and failed == 0
                    and unverified == 0
                ),
                "status": status,
            }
        )
    invalid_claim_urls = list(
        {
            (
                str(item.get("security_code") or ""),
                str(item.get("type_key") or ""),
                str(item.get("claim_index") or ""),
                str(item.get("search_finding_id") or ""),
                str(item.get("source") or ""),
                str(item.get("reason") or ""),
            ): item
            for item in invalid_claim_urls
        }.values()
    )
    audit_passed = (
        not invalid_claim_urls
        and counts["failed"] == 0
        and counts["invalid"] == 0
        and semantic_failed_count == 0
        and semantic_unverified_count == 0
    )
    report = {
        "audit_contract_version": AUDIT_CONTRACT_VERSION,
        "merged_sha256": hashlib.sha256(merged_bytes).hexdigest(),
        "projection_sha256": projection_sha256,
        **projection_counts,
        "snapshot_generation": payload.get("snapshot_generation"),
        "market_as_of": payload.get("market_as_of"),
        "checked": len(results),
        **counts,
        # ``ok`` remains in the report for the existing publish contract.
        # ``reachable`` is the explicit reachability name used by operators.
        "reachable": counts["ok"],
        "body_retrieved_count": sum(bool(result.get("body_retrieved")) for result in results),
        "canonical_urls": sorted(detailed_bindings),
        "audit_passed": audit_passed,
        "claim_count": claim_count,
        "semantic_claim_count": semantic_claim_count,
        "semantic_passed_count": semantic_claim_count - semantic_failed_count - semantic_unverified_count,
        "semantic_failed_count": semantic_failed_count,
        "semantic_unverified_count": semantic_unverified_count,
        "semantic_issue_count": len(semantic_issues),
        "semantic_html_date_checked_count": html_date_checked_count,
        "published_at_mismatch_count": published_at_mismatch_count,
        "report_period_after_publication_count": report_period_after_publication_count,
        "blocked_semantic_claim_count": blocked_semantic_claim_count,
        "invalid_claim_url_count": len(invalid_claim_urls),
        "official_market_domain_count": sum(bool(result["official_market_domain"]) for result in results),
        "invalid_claim_urls": invalid_claim_urls,
        "semantic_issues": semantic_issues,
        "source_bindings": [binding for url in sorted(detailed_bindings) for binding in detailed_bindings[url]],
        "company_coverage": company_coverage,
        "company_coverage_by_code": {item["security_code"]: item for item in company_coverage},
        "results": results,
    }
    shutil.rmtree(body_dir, ignore_errors=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-bytes", type=int, default=262_144)
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.max_bytes < 1:
        raise SystemExit("workers, timeout and max-bytes must be positive")
    report = audit(
        args.merged,
        args.output,
        workers=args.workers,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
    )
    summary = {key: report[key] for key in ("checked", "reachable", "blocked", "failed", "invalid", "audit_passed")}
    summary["affected_companies"] = [
        {
            "security_code": item.get("security_code"),
            "name": item.get("name"),
            "status": item.get("status"),
        }
        for item in report["company_coverage"]
        if item.get("status") not in {"pass", "searched_no_source"}
    ]
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if report["audit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
