"""Check that AI-screening claim URLs are real, reachable web resources.

This audit does not decide whether a claim is financially correct.  It verifies
the narrower release facts that can be checked mechanically: every claimed URL
is public HTTP(S), the resource is reachable, and web-search claims point to the
same finding URL and do not relabel an old HTML article as a newer disclosure.
Text-bearing PDFs are parsed with the project's pinned PyMuPDF dependency and
checked for company identity, report period and cited fact; malformed or
image-only PDFs remain explicitly unverified.
Contract v3 also hashes the exact claim/finding semantics that publication can
expose, so changing cited prose while retaining a URL cannot reuse an audit.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import re
import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urljoin, urlparse

from tools.ai_source_urls import (
    canonical_urls,
    claim_source_urls,
    finding_source_url,
)


_OFFICIAL_DOMAIN_SUFFIXES = (
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "hkexnews.hk",
)
_BLOCKED_HTTP_STATUSES = frozenset({401, 403, 407, 429})
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
AUDIT_CONTRACT_VERSION = 3


def _public_text(value: Any, limit: int) -> str:
    """Apply the same scalar text projection used by publication."""

    return str(value or "").strip()[:limit]


def _public_claim_source_fields(claim: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    """Return the source fields that survive ``publish_ai_screening``."""

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
        for claim_index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                continue
            source_ref, source_context, source_refs = _public_claim_source_fields(claim)
            finding_id = _public_text(claim.get("search_finding_id"), 120)
            linked_finding = findings_by_id.get(finding_id, {})
            claim_rows.append(
                {
                    "claim_index": claim_index,
                    "statement": _public_text(claim.get("statement"), 600),
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
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded == "title":
            self._title_depth += 1
        if folded in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
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


def _html_visible_text(body: bytes, content_type: str) -> tuple[str, str]:
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
        return "", ""
    visible = re.sub(r"\s+", "", "".join(parser.text_parts)).casefold()
    title = re.sub(r"\s+", "", "".join(parser.title_parts)).casefold()
    return visible, title


def _normalised_company_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).casefold()
    # These suffixes are legal-company boilerplate and are poor identity
    # anchors on disclosure pages.  Keep the original name as a fallback.
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "集团股份", "集团", "控股", "公司"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


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
    if (code and code in visible) or (normal_name and normal_name in visible):
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


def _is_industry_source_claim(claim: Mapping[str, Any], finding: Mapping[str, Any]) -> bool:
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
    return any(marker in context for marker in ("行业协会", "工业协会", "协会信息中心", "行业报告", "市场报告"))


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
    elif "q4" in raw or "h2" in raw or "年度" in raw or "年报" in raw:
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
    numbers: set[str] = set()
    for value in (claim.get("statement"), finding.get("finding")):
        for match in re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?", str(value or "")):
            raw = match.lstrip("+")
            unsigned = raw.lstrip("-")
            if unsigned.isdigit() and (1900 <= int(unsigned) <= 2100 or len(unsigned) == 6):
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
    r"(?P<sign>[+-]?)\s*(?P<number>\d+(?:[,.]\d+)?)\s*"
    r"(?P<unit>千亿元|百亿元|十亿元|亿元|千万元|百万元|十万元|万元|千元|百元|万元|元|"
    r"亿股|万股|千股|股|万吨|万件|万台|吨|件|台|%|％|倍)"
)
_NUMBER_UNIT_FACTORS = {
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
    "%": 1.0,
    "％": 1.0,
    "倍": 1.0,
}


def _number_unit_facts(values: Iterable[Any]) -> list[tuple[float, str]]:
    facts: list[tuple[float, str]] = []
    for value in values:
        for match in _NUMBER_WITH_UNIT_RE.finditer(str(value or "")):
            try:
                number = float(match.group("number").replace(",", ""))
            except ValueError:
                continue
            if not math.isfinite(number):
                continue
            sign = -1.0 if match.group("sign") == "-" else 1.0
            unit = match.group("unit")
            facts.append((sign * number * _NUMBER_UNIT_FACTORS[unit], unit))
    return facts


def _structured_number_match(
    text: str,
    numbers: set[str],
    *,
    claim: Mapping[str, Any] | None = None,
    finding: Mapping[str, Any] | None = None,
) -> bool:
    compact = re.sub(r"[,\s]", "", text)
    for number in numbers:
        token = re.sub(r"[,\s]", "", number)
        if token and token in compact:
            return True
        if "." in token:
            integer, fraction = token.split(".", 1)
            if fraction.rstrip("0") and f"{integer}.{fraction.rstrip('0')}" in compact:
                return True
    # Financial statements routinely switch between yuan, ten-thousand yuan
    # and hundred-million yuan.  Compare explicitly unit-tagged facts after
    # normalising their units, while retaining the exact-token fast path above.
    target_facts = _number_unit_facts(
        [
            (claim or {}).get("statement"),
            (finding or {}).get("finding"),
        ]
    )
    body_facts = _number_unit_facts([text])
    for target, target_unit in target_facts:
        for actual, actual_unit in body_facts:
            if target_unit in {"%", "％", "倍"} or actual_unit in {"%", "％", "倍"}:
                tolerance = max(0.05, abs(target) * 0.002)
            else:
                tolerance = max(1.0, abs(target) * 0.002)
            if abs(target - actual) <= tolerance:
                return True
    return False


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
    if not ((code and code in visible) or (normal_name and normal_name in visible)):
        return ["structured source body does not match company code or normalized company name"]
    period_match = _period_matches(text, _structured_period_tokens(claim, finding))
    number_match = _structured_number_match(
        visible,
        _claim_numbers(claim, finding),
        claim=claim,
        finding=finding,
    )
    field_match = any(field in visible for field in _structured_field_tokens(url))
    if not (period_match or number_match or field_match):
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
        for page in document:
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
        return ["PDF text does not match company code or normalized company name"]
    period_match = _period_matches(text, _structured_period_tokens(claim, finding))
    number_match = _structured_number_match(
        visible,
        _claim_numbers(claim, finding),
        claim=claim,
        finding=finding,
    )
    if not (period_match or number_match):
        return ["PDF text does not match report period or fact number"]
    return []


def _pdf_semantic_issues(
    body: bytes,
    *,
    security_code: str,
    name: str,
    claim: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> list[str]:
    """Check a PDF body; extraction failures remain explicitly unverified."""

    text, extraction_reason = _extract_pdf_text(body)
    if extraction_reason:
        return [f"PDF text extraction is unverified ({extraction_reason})"]
    return _pdf_text_semantic_issues(
        text,
        security_code=security_code,
        name=name,
        claim=claim,
        finding=finding,
    )


def _html_semantic_issues(
    body: bytes,
    content_type: str,
    *,
    security_code: str,
    name: str,
    report_period: Any,
    claim: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> list[str]:
    """Run the deliberately small HTML identity/content gate."""

    visible, title = _html_visible_text(body, content_type)
    if not visible:
        return ["HTML body has no visible text"]
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1).strip("\"'") if charset_match else "utf-8"
    try:
        raw_text = body.decode(charset, errors="replace").casefold()
    except LookupError:
        raw_text = body.decode("utf-8", errors="replace").casefold()
    first_text = f"{title}{visible[:600]}"
    if re.search(
        r"(?:<title[^>]*>\s*(?:404|page\s*not\s*found|not\s*found)|(?:404|page\s*not\s*found|not\s*found|页面不存在|内容不存在|找不到页面|链接失效))",
        first_text,
    ) or re.search(r"(?:id|class)\s*=\s*[\"'][^\"']*(?:404|not[-_ ]?found)[^\"']*[\"']", raw_text):
        return ["HTML appears to be a soft-404 page"]
    if re.search(
        r"(?:captcha|verify\s+you\s+are\s+human|人机验证|验证码|安全验证|<input[^>]+(?:captcha|验证码))",
        f"{first_text}{raw_text}",
    ):
        return ["HTML appears to be a login or CAPTCHA challenge"]
    if re.search(r"(?:登录|登陆|sign\s*in|log\s*in|login|authentication required)", title) or re.search(
        r"<form[^>]+(?:login|sign[-_ ]?in)", raw_text
    ):
        return ["HTML appears to be a login page"]

    code = re.sub(r"\s+", "", str(security_code or "")).casefold()
    normal_name = _normalised_company_name(name)
    if not ((code and code in visible) or (normal_name and normal_name in visible)):
        return ["HTML正文未匹配公司代码或规范化公司名"]

    period_match = _period_matches("".join((title, visible)), _report_period_tokens(report_period))
    numbers = _claim_numbers(claim, finding)
    number_match = _structured_number_match(
        visible,
        numbers,
        claim=claim,
        finding=finding,
    )
    if not period_match and not number_match:
        return ["HTML正文未匹配报告期或关键数字"]
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
    if not addresses:
        raise OSError(f"DNS returned no A/AAAA addresses for {host}")
    non_public = sorted(address for address in addresses if not ipaddress.ip_address(address).is_global)
    if non_public:
        raise UnsafeUrlError(f"DNS resolved to non-public address(es): {','.join(non_public)}")
    return sorted(addresses)


def _official_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _OFFICIAL_DOMAIN_SUFFIXES)


def _check_url(url: str, *, timeout: float, max_bytes: int, max_redirects: int = 5) -> dict[str, Any]:
    base = {"url": url, "official_market_domain": _official_domain(url)}
    # Source verification must observe the public origin directly.  A stale
    # workstation HTTP(S)_PROXY can otherwise turn a reachable filing into a
    # local timeout or proxy challenge, producing a false yellow warning.
    opener = urllib.request.build_opener(_NoRedirectHandler(), urllib.request.ProxyHandler({}))
    current_url = url
    redirect_count = 0
    while True:
        try:
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
                security_code=claim["security_code"],
                name=claim["name"],
                claim=claim["claim"],
                finding=claim["finding"],
                require_identity=not _is_industry_source_claim(claim["claim"], claim["finding"]),
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
            security_code=claim["security_code"],
            name=claim["name"],
            report_period=claim.get("report_period"),
            claim=claim["claim"],
            finding=finding,
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
    print(
        json.dumps(
            {key: report[key] for key in ("checked", "reachable", "blocked", "failed", "invalid", "audit_passed")},
            sort_keys=True,
        )
    )
    return 0 if report["audit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
