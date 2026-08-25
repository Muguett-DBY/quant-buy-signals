"""Identity-bound text checks for the AI screening release boundary.

AI source snippets occasionally concatenate the next search result.  A
random six-digit number is not enough evidence of that failure (financial
amounts, dates, PDF hashes and industry indexes are common), so this module
only treats explicitly labelled or parenthesised A-share codes as company
identifiers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


# This deliberately excludes industry indexes (881xxx/88xxxx), random PDF
# path fragments and numeric financial facts.  The prefixes cover the listed
# A/B-share code families that can identify another company in a review.
_A_SHARE_CODE = r"(?:000|001|002|003|200|300|301|302|600|601|603|605|688|689)\d{3}"
_EXPLICIT_CODE_RE = re.compile(
    rf"(?:证券代码|股票代码|证券号|股票号|A股代码|代码)\s*[:：]?\s*(?P<label>{_A_SHARE_CODE})"
    rf"|(?<!\d)[（(]\s*(?P<paren>{_A_SHARE_CODE})\s*[）)]"
)
_SEGMENT_BREAK_RE = re.compile(r"(?:[-_=]{5,}|\.{3,}|…{2,})")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[。！？!?；;\n])|(?<=[.!?])\s+")


def explicit_company_codes(value: Any) -> set[str]:
    """Return explicit A-share identifiers in prose, never bare numbers."""

    text = str(value or "")
    return {
        match.group("label") or match.group("paren")
        for match in _EXPLICIT_CODE_RE.finditer(text)
        if match.group("label") or match.group("paren")
    }


def _clean_text(text: str, security_code: str) -> tuple[str, int, set[str]]:
    own_code = str(security_code or "").strip()
    removed_segments = 0
    removed_codes: set[str] = set()
    kept_segments: list[str] = []
    contaminated_tail = False
    # Search providers often put a horizontal separator before the next
    # result.  Split that first, then remove only sentences explicitly tied
    # to a different listed-company code.
    segments = _SEGMENT_BREAK_RE.split(text)
    for index, segment in enumerate(segments):
        segment_codes = explicit_company_codes(segment)
        other_segment_codes = segment_codes - {own_code}
        future_other_before_own = False
        for future_segment in segments[index + 1 :]:
            future_codes = explicit_company_codes(future_segment)
            if own_code in future_codes:
                break
            if future_codes:
                future_other_before_own = True
                break
        if contaminated_tail:
            if own_code in segment_codes:
                contaminated_tail = False
            else:
                removed_segments += 1
                removed_codes.update(other_segment_codes)
                continue
        if other_segment_codes and own_code not in segment_codes:
            removed_segments += 1
            removed_codes.update(other_segment_codes)
            contaminated_tail = True
            continue
        if future_other_before_own and own_code not in segment_codes:
            removed_segments += 1
            contaminated_tail = True
            continue
        sentences = [piece for piece in _SENTENCE_BREAK_RE.split(segment) if piece]
        kept_sentences: list[str] = []
        for sentence in sentences:
            sentence_codes = explicit_company_codes(sentence)
            if contaminated_tail:
                if own_code in sentence_codes:
                    contaminated_tail = False
                else:
                    removed_segments += 1
                    removed_codes.update(sentence_codes - {own_code})
                    continue
            other_codes = explicit_company_codes(sentence) - {own_code}
            if other_codes:
                removed_segments += 1
                removed_codes.update(other_codes)
                contaminated_tail = True
                continue
            kept_sentences.append(sentence)
        if kept_sentences:
            kept_segments.append("".join(kept_sentences))
        if future_other_before_own and own_code in segment_codes:
            contaminated_tail = True
    if removed_segments == 0:
        return text, 0, set()
    cleaned = "。".join(piece.strip(" 。；;\t") for piece in kept_segments if piece.strip(" 。；;\t"))
    return cleaned.strip(), removed_segments, removed_codes


def sanitise_review_identity(review: Mapping[str, Any], security_code: str) -> tuple[dict[str, Any], dict[str, int]]:
    """Copy a review while removing explicit cross-company prose.

    The returned counters are release metadata only.  Numeric facts and
    source URLs are retained; only text that identifies a different listed
    company is removed.
    """

    cleaned = dict(review)
    stats = {
        "removed_cross_company_claim_count": 0,
        "removed_cross_company_text_count": 0,
        "cleaned_cross_company_text_count": 0,
    }
    for field in ("summary",):
        value = review.get(field)
        if isinstance(value, str):
            new_value, removed, _ = _clean_text(value, security_code)
            if removed:
                stats["removed_cross_company_text_count"] += removed
                stats["cleaned_cross_company_text_count"] += 1
            cleaned[field] = new_value
    for field in ("key_strengths", "risk_flags", "quantitative_facts"):
        values = review.get(field)
        if not isinstance(values, list):
            continue
        new_values: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            new_value, removed, _ = _clean_text(value, security_code)
            if removed:
                stats["removed_cross_company_text_count"] += removed
                if new_value:
                    stats["cleaned_cross_company_text_count"] += 1
            if new_value:
                new_values.append(new_value)
        cleaned[field] = new_values
    claims = review.get("claims")
    if isinstance(claims, list):
        new_claims: list[dict[str, Any]] = []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            item = dict(claim)
            statement_removed = 0
            raw_context = item.get("source_context")
            context_codes = explicit_company_codes(raw_context) if isinstance(raw_context, str) else set()
            if context_codes - {security_code} and security_code not in context_codes:
                stats["removed_cross_company_claim_count"] += 1
                stats["removed_cross_company_text_count"] += 1
                continue
            statement = item.get("statement")
            if isinstance(statement, str):
                statement, statement_removed, _ = _clean_text(statement, security_code)
                if statement_removed:
                    stats["removed_cross_company_text_count"] += statement_removed
                    if statement:
                        stats["cleaned_cross_company_text_count"] += 1
            context = item.get("source_context")
            if isinstance(context, str):
                context, context_removed, _ = _clean_text(context, security_code)
                if context_removed:
                    stats["removed_cross_company_text_count"] += context_removed
                    if context:
                        stats["cleaned_cross_company_text_count"] += 1
            item["statement"] = statement
            item["source_context"] = context
            if statement:
                new_claims.append(item)
            elif statement_removed:
                stats["removed_cross_company_claim_count"] += 1
            elif not isinstance(claim.get("statement"), str):
                # Minimal legacy claims may carry only a source URL.  Keep
                # those bindings intact; the downstream reviewer decides
                # whether they are usable evidence.
                new_claims.append(item)
        cleaned["claims"] = new_claims
    return cleaned, stats


def sanitise_text(value: str, security_code: str) -> tuple[str, int, set[str]]:
    """Public single-field helper used by the full release audit."""

    return _clean_text(value, security_code)
