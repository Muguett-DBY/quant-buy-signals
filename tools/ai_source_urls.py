"""Small shared helpers for canonical AI-screening source URLs.

The audit and publication paths must inspect the same URLs.  In particular,
source context often contains a URL followed by a human explanation, while a
long report URL must never be shortened before it is audited or published.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit


_URL_START_RE = re.compile(r"https?://", re.IGNORECASE)
# Unescaped ASCII whitespace cannot be part of an HTTP URL.  Stop before
# prose while preserving encoded spaces (``%20``) inside the URL.
_URL_STOP_CHARS = frozenset(" <>\"'\ufffd\u3000\r\n\t")
_TRAILING_PUNCTUATION = frozenset(".,;:!?，。；：！？、")
_URL_PROSE_AFTER_FILE_RE = re.compile(r"\.(?:pdf|html?|aspx?|php|docx?|xlsx?|csv)(?:[?#][^\s]*)?$", re.IGNORECASE)


def _trim_url_token(value: str) -> str:
    """Trim prose punctuation without trimming valid URL characters."""

    token = value.strip()
    while token and token[-1] in _TRAILING_PUNCTUATION:
        token = token[:-1]
    # A closing parenthesis/bracket is prose punctuation when it is not
    # balanced by an opening one in the URL (common in ``URL（说明）``).
    pairs = (("(", ")"), ("[", "]"), ("{", "}"), ("（", "）"), ("【", "】"), ("《", "》"))
    changed = True
    while token and changed:
        changed = False
        for opening, closing in pairs:
            if token.endswith(closing) and token.count(closing) > token.count(opening):
                token = token[:-1].rstrip()
                changed = True
    return token


def _canonical_token(value: str) -> str:
    token = _trim_url_token(value)
    if not token:
        return ""
    try:
        parsed = urlsplit(token)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        port = parsed.port
    except ValueError:
        return ""

    # Keep path, query and fragment byte-for-byte intact.  Only URL syntax
    # which is case-insensitive is normalized, and default ports disappear so
    # the audit and publisher agree on one identity for the same resource.
    host = parsed.hostname.casefold()
    # Sina serves the same public bulletin mirror on ``money`` and ``vip``.
    # The latter intermittently returns a non-standard 456 throttle response
    # to parallel CI requests; the stable mirror keeps provenance identical
    # while making source verification reproducible.
    if host == "vip.stock.finance.sina.com.cn" and parsed.path.casefold().startswith("/corp/"):
        host = "money.finance.sina.com.cn"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None and port not in {80 if parsed.scheme.casefold() == "http" else 443}:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, parsed.query, parsed.fragment))


def canonical_urls(value: Any) -> list[str]:
    """Extract every complete HTTP(S) URL from one source field."""

    text = str(value or "")
    urls: list[str] = []
    for match in _URL_START_RE.finditer(text):
        tail = text[match.start() :]
        end = len(tail)
        for index, character in enumerate(tail):
            if character in _URL_STOP_CHARS:
                end = index
                break
            # Full-width punctuation usually introduces prose.  It is handled
            # here without imposing an arbitrary URL-length cap.
            if character in "（）【】《》「」『』，。；：！？、":
                end = index
                break
            # Some tool outputs append Chinese prose directly after a report
            # filename without a space or delimiter.  Only treat it as prose
            # when the URL already has a recognizable file suffix; a genuine
            # Unicode path such as ``/报告/摘要`` remains intact.
            if 0x2E80 <= ord(character) <= 0x9FFF and _URL_PROSE_AFTER_FILE_RE.search(tail[:index]):
                end = index
                break
        token = _canonical_token(tail[:end])
        if token and token not in urls:
            urls.append(token)
    return urls


def is_deterministic_valuation_claim(claim: Mapping[str, Any]) -> bool:
    """Return whether a claim is the generated close-price snapshot.

    The current price, PE, PB and market-cap sentence comes from the
    generation-bound market snapshot, not from a news page.  Treating an
    arbitrary article URL attached by a model as proof for that sentence is
    precisely what creates the noisy source warnings on quote pages.
    """

    text = " ".join(str(claim.get(key) or "") for key in ("statement", "source_context")).casefold()
    return (
        "candidate valuation snapshot" in text
        or "candidate snapshot" in text
        or "估值快照" in text
        or "候选估值" in text
    )


def is_search_provenance_claim(claim: Mapping[str, Any]) -> bool:
    """Return whether a claim is only a search-result transcript.

    A snippet such as ``web search evidence: ...`` records that a query was
    run, but it is not a financial fact and its landing page is often a
    JavaScript index rather than the cited filing.  Search-event metadata
    remains published separately; treating the transcript as a claim would
    create false semantic warnings and clutter the company card.
    """

    statement = str(claim.get("statement") or "").strip().casefold()
    return statement.startswith(("web search evidence", "search evidence", "检索摘要", "搜索摘要"))


def claim_source_urls(claim: Mapping[str, Any]) -> list[str]:
    """Collect URL values from every source field of one claim."""

    if is_deterministic_valuation_claim(claim) or is_search_provenance_claim(claim):
        return []
    values: list[Any] = [claim.get("source_ref"), claim.get("source_context")]
    source_refs = claim.get("source_refs")
    if isinstance(source_refs, list):
        values.extend(source_refs)
    urls: list[str] = []
    for value in values:
        for url in canonical_urls(value):
            if url not in urls:
                urls.append(url)
    return urls


def finding_source_url(finding: Mapping[str, Any]) -> str:
    """Return the canonical URL of a search finding, if it has one."""

    urls = canonical_urls(finding.get("url"))
    return urls[0] if urls else ""


def iter_review_url_bindings(
    review: Mapping[str, Any], *, security_code: str, name: str, type_key: str
) -> Iterator[dict[str, Any]]:
    """Yield URL identities used by claims and published search findings."""

    claims = review.get("claims") if isinstance(review.get("claims"), list) else []
    for claim_index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            continue
        finding_id = str(claim.get("search_finding_id") or "").strip()
        for url in claim_source_urls(claim):
            yield {
                "security_code": security_code,
                "name": name,
                "type_key": type_key,
                "claim_index": claim_index,
                "search_finding_id": finding_id,
                "url": url,
                "kind": "claim",
            }
    findings = review.get("search_findings") if isinstance(review.get("search_findings"), list) else []
    for finding_index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            continue
        url = finding_source_url(finding)
        if not url:
            continue
        yield {
            "security_code": security_code,
            "name": name,
            "type_key": type_key,
            "claim_index": None,
            "search_finding_id": str(finding.get("id") or "").strip(),
            "finding_index": finding_index,
            "url": url,
            "kind": "search_finding",
        }


def review_canonical_urls(review: Mapping[str, Any]) -> list[str]:
    """Return the exact URL set that can survive publication."""

    urls: list[str] = []
    for binding in iter_review_url_bindings(review, security_code="", name="", type_key=""):
        url = str(binding["url"])
        if url not in urls:
            urls.append(url)
    return urls
