"""Remove search prose whose stock identifier is not bound to the candidate.

The Codex web search itself is retained as an attempted search.  A result with
another company's ``stockid`` is not exposed as a fact in the public overlay.
This is a small release-boundary check for independently produced review
shards; it does not invent a replacement source or alter the recommendation.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse


_STOCK_ID_RE = re.compile(r"stockid=(\d+)")
_SIX_DIGIT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_COMPANY_CODE_CONTEXT_RE = re.compile(
    r"(?:证券代码|股票代码|公司代码|证券简称|股票简称|代码|证券|股票)\s*[:：]?\s*"
    r"(?:[（(]\s*)?(\d{6})(?:\s*[）)])?"
    r"|[（(]\s*(\d{6})\s*[）)]"
    r"|\b(\d{6})\.(?:SH|SZ|BJ)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SEARCH_METADATA_RE = re.compile(
    r"\s*\(?\s*turn[\w-]+\s*"
    r"(?:\[wordlim:\s*[^\]]+\]\s*)?"
    r"(?:Published:\s*[^;；]*[;；]\s*)?"
    r"(?:Crawled:\s*[^;；]*[;；]\s*)?",
    re.IGNORECASE,
)
_SEARCH_METADATA_TOKEN_RE = re.compile(
    r"\bturn[\w-]+\s*(?:\[wordlim:\s*[^\]]+\]\s*)?"
    r"(?:Published:\s*[^;；]*[;；]\s*)?"
    r"(?:Crawled:\s*[^;；]*[;；]\s*)?",
    re.IGNORECASE,
)
_SEARCH_RESULT_DECISION_RE = re.compile(
    r"本次公司特定来源\s*\d+\s*条[。；;]?|结论\s*[:：]\s*(?:建议买|观察|不建议)[。；;]?"
)
_SEARCH_TRANSCRIPT_PREFIX_RE = re.compile(
    r"(?im)^\s*(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|20\d{2}年[^：:；;\n]{0,12})?\s*"
    r"[：:]?\s*(?:联网检索事实摘要|联网检索状态|联网资料摘要|检索摘要|搜索摘要)\s*[：:；;]?\s*(?:value\s*)?"
)
_SEARCH_TRANSCRIPT_SUFFIX_RE = re.compile(r"[。；;]\s*同上[，,]?\s*以来源原文及报告口径为准[。；;]?\s*$")
_FUTURE_DATE_RE = re.compile(r"(?<!\d)(20\d\d)[-/年](\d{1,2})(?:[-/月](\d{1,2})日?)?")
_SOURCE_MARKERS = (
    "联网事实（Codex web_search",
    "联网资料摘要：",
    "联网事实(Codex web_search",
    "公司公开资料：",
    "公司公开资料:",
    "公开资料：",
    "公开资料:",
    "公开资料摘要：",
    "公开资料摘要:",
)


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _future_dates(value: Any, cutoff: date | None) -> list[date]:
    if cutoff is None:
        return []
    result: list[date] = []
    for match in _FUTURE_DATE_RE.finditer(str(value or "")):
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))
        except ValueError:
            continue
        if parsed > cutoff:
            result.append(parsed)
    return result


def _without_search_metadata(value: Any) -> str:
    """Keep the useful snippet while removing raw tool citation bookkeeping."""

    text = str(value or "").strip()
    text = _SEARCH_METADATA_RE.sub(" ", text)
    text = _SEARCH_METADATA_TOKEN_RE.sub(" ", text)
    text = _SEARCH_RESULT_DECISION_RE.sub(" ", text)
    text = _SEARCH_TRANSCRIPT_PREFIX_RE.sub("", text)
    text = _SEARCH_TRANSCRIPT_SUFFIX_RE.sub("。", text)
    text = re.sub(
        r"\s*(?:[；;，,]\s*)?(?:同上\s*)?以来源原文及报告口径为准[。；;]?\s*$",
        "",
        text,
    )
    text = re.sub(r"(?:联网|公司)资料摘要\s*[:：]\s*", "公开资料：", text)
    text = re.sub(r"\s+([。；;，,])", r"\1", text)
    text = re.sub(r"([。；;])\s*([。；;])", r"\1", text)
    return text.strip(" \t\r\n；;，,")


def _without_future_source_prose(value: Any, cutoff: date | None) -> str:
    """Remove a search-result clause that exposes information after the snapshot."""

    text = _without_search_metadata(value)
    if not _future_dates(text, cutoff):
        return text
    marker_positions = [text.find(marker) for marker in _SOURCE_MARKERS]
    marker_positions = [position for position in marker_positions if position >= 0]
    if not marker_positions:
        return text
    marker = min(marker_positions)
    tail_positions = [
        position
        for marker_text in ("；主要风险：", ";主要风险：", "。主要风险：")
        for position in [text.find(marker_text, marker)]
        if position >= 0
    ]
    prefix = text[:marker].rstrip(" \t\r\n；;，,。")
    if tail_positions:
        return (prefix + text[min(tail_positions) :]).strip()
    return prefix or "快照日后的公告条目已剔除，未作为本次结论依据。"


def _wrong_stock_ids(value: Any, code: str) -> set[str]:
    return {match for match in _STOCK_ID_RE.findall(str(value or "")) if match != code}


def _candidate_urls(claim: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    source_ref = str(claim.get("source_ref") or "").strip()
    if source_ref:
        values.append(source_ref)
    source_refs = claim.get("source_refs")
    if isinstance(source_refs, list):
        values.extend(str(value).strip() for value in source_refs if str(value).strip())
    return values


def _wrong_sina_stock_ids(claim: Mapping[str, Any], code: str) -> set[str]:
    """Return Sina ``stockid`` values that are not the candidate code.

    Sina's bulletin URLs use the six-digit security code.  CFI's ``stockid``
    is an unrelated internal integer, so it must not be treated as a mismatch.
    """

    wrong: set[str] = set()
    for url in _candidate_urls(claim):
        host = urlparse(url).netloc.casefold()
        if not host.endswith("sina.com.cn"):
            continue
        query = parse_qs(urlparse(url).query)
        values = query.get("stockid", []) or _STOCK_ID_RE.findall(url)
        wrong.update(value for value in values if value and value != code)
    return wrong


def _numeric_host_codes(claim: Mapping[str, Any], code: str) -> set[str]:
    """Return an explicit security code embedded in a host name."""

    wrong: set[str] = set()
    for url in _candidate_urls(claim):
        host = urlparse(url).netloc.casefold()
        for label in host.split("."):
            if re.fullmatch(r"\d{6}", label) and label != code:
                wrong.add(label)
    return wrong


def _explicit_company_codes(value: Any) -> set[str]:
    """Extract stock-code-shaped tokens with company-code context.

    Dates and document identifiers are deliberately ignored.  This catches a
    report for ``共同药业(300966)`` accidentally attached to another packet,
    while leaving ordinary industry quantities and CFI internal IDs alone.
    """

    text = _URL_RE.sub(" ", str(value or ""))
    codes: set[str] = set()
    for match in _COMPANY_CODE_CONTEXT_RE.finditer(text):
        for group in match.groups():
            if group:
                codes.add(group)
    return codes


def _claim_wrong_company_codes(claim: Mapping[str, Any], code: str) -> set[str]:
    # The context line often contains the candidate's code by design.  The
    # claim statement itself must still be about that candidate; otherwise a
    # search result for a different issuer can pass through merely because
    # the surrounding search metadata names the requested code.
    explicit = _explicit_company_codes(claim.get("statement"))
    return {value for value in explicit if value != code}


def _claim_is_unbound(claim: Mapping[str, Any], code: str) -> bool:
    """Reject only source claims with a mechanically provable wrong owner."""

    if _wrong_sina_stock_ids(claim, code) or _numeric_host_codes(claim, code):
        return True
    other_codes = _claim_wrong_company_codes(claim, code)
    if other_codes and code not in _explicit_company_codes(claim.get("statement")):
        return True
    return False


def _without_unbound_source(value: Any, code: str, bad_tokens: set[str] | None = None) -> str:
    text = str(value or "").strip()
    tokens = {token for token in (bad_tokens or set()) if token}
    wrong_codes = _explicit_company_codes(text) - {code}
    bad_codes = {token for token in tokens if re.fullmatch(r"\d{6}", token)}
    has_bad_token = any(token in text for token in tokens)
    if (
        not _wrong_stock_ids(text, code)
        and not has_bad_token
        and not (wrong_codes & bad_codes and any(marker in text for marker in _SOURCE_MARKERS))
    ):
        return text
    for marker in _SOURCE_MARKERS:
        position = text.find(marker)
        if position >= 0:
            prefix = text[:position].rstrip(" ;，。；")
            return f"{prefix}。联网检索已执行，但来源公司代码未通过归属校验，未将该页面作为本公司事实。"
    return "联网检索已执行，但来源公司代码未通过归属校验，未将该页面作为本公司事实。"


def _without_unbound_sources(value: Any, code: str, bad_tokens: set[str] | None = None) -> Any:
    if isinstance(value, list):
        return [_without_unbound_source(item, code, bad_tokens) for item in value]
    return _without_unbound_source(value, code, bad_tokens)


def sanitize(payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    result = dict(payload)
    packets = result.get("packets")
    if not isinstance(packets, list):
        raise ValueError("merged artifact packets are missing")
    market_cutoff = _parse_iso_date(result.get("market_as_of"))
    changed = 0
    removed_claims = 0
    removed_http_refs = 0
    removed_future_claims = 0
    cleaned_search_metadata = 0
    copied_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("merged artifact packet is not an object")
        copied_packet = dict(packet)
        review = packet.get("ai_review")
        if isinstance(review, Mapping):
            copied_review = dict(review)
            code = str(packet.get("security_code") or review.get("security_code") or "").strip()
            before = json.dumps(copied_review, ensure_ascii=False, sort_keys=True)
            claims = copied_review.get("claims")
            bad_tokens: set[str] = set()
            if isinstance(claims, list):
                kept_claims: list[Any] = []
                for claim in claims:
                    if isinstance(claim, Mapping) and _future_dates(claim.get("statement"), market_cutoff):
                        removed_future_claims += 1
                        continue
                    if not isinstance(claim, Mapping) or not _claim_is_unbound(claim, code):
                        kept_claims.append(claim)
                        continue
                    removed_claims += 1
                    for value in _candidate_urls(claim):
                        bad_tokens.add(value)
                    bad_tokens.update(_claim_wrong_company_codes(claim, code))
                    bad_tokens.update(_wrong_sina_stock_ids(claim, code))
                    bad_tokens.update(_numeric_host_codes(claim, code))
                copied_review["claims"] = kept_claims
            if isinstance(copied_review.get("claims"), list):
                normalized_claims: list[Any] = []
                for claim in copied_review["claims"]:
                    if not isinstance(claim, Mapping):
                        normalized_claims.append(claim)
                        continue
                    refs = claim.get("source_refs")
                    if not isinstance(refs, list):
                        normalized_claims.append(claim)
                        continue
                    filtered = [
                        value
                        for value in refs
                        if not (isinstance(value, str) and value.strip().lower().startswith("http://"))
                    ]
                    if filtered != refs:
                        removed_http_refs += len(refs) - len(filtered)
                        copied_claim = dict(claim)
                        copied_claim["source_refs"] = filtered or [str(claim.get("source_ref") or "")]
                        normalized_claims.append(copied_claim)
                    else:
                        normalized_claims.append(claim)
                copied_review["claims"] = normalized_claims
            for field in ("summary", "key_strengths", "risk_flags"):
                if field in copied_review:
                    values = copied_review[field]
                    values = _without_unbound_sources(values, code, bad_tokens)
                    if isinstance(values, list):
                        cleaned: list[str] = []
                        for value in values:
                            before_value = str(value or "")
                            after_value = _without_future_source_prose(before_value, market_cutoff)
                            after_value = _without_search_metadata(after_value)
                            if after_value != before_value:
                                cleaned_search_metadata += 1
                            if after_value:
                                cleaned.append(after_value)
                        copied_review[field] = cleaned
                    else:
                        before_value = str(values or "")
                        after_value = _without_future_source_prose(before_value, market_cutoff)
                        after_value = _without_search_metadata(after_value)
                        if after_value != before_value:
                            cleaned_search_metadata += 1
                        copied_review[field] = after_value
            if "quantitative_facts" in copied_review:
                values = _without_unbound_sources(copied_review["quantitative_facts"], code, bad_tokens)
                if not isinstance(values, list):
                    values = [values]
                cleaned_facts: list[str] = []
                for value in values:
                    before_value = str(value or "")
                    after_value = _without_search_metadata(before_value)
                    if after_value != before_value:
                        cleaned_search_metadata += 1
                    if after_value:
                        cleaned_facts.append(after_value)
                copied_review["quantitative_facts"] = cleaned_facts
            sanitized_claims: list[Any] = []
            for claim in copied_review.get("claims", []):
                if not isinstance(claim, Mapping):
                    sanitized_claims.append(claim)
                    continue
                copied_claim = dict(claim)
                for field in ("statement", "source_context"):
                    if field in copied_claim and isinstance(copied_claim[field], str):
                        before_value = copied_claim[field]
                        after_value = _without_search_metadata(before_value)
                        if after_value != before_value:
                            cleaned_search_metadata += 1
                        copied_claim[field] = after_value
                sanitized_claims.append(copied_claim)
            copied_review["claims"] = sanitized_claims
            profile = copied_review.get("economic_profile")
            if isinstance(profile, Mapping):
                claims = copied_review.get("claims") if isinstance(copied_review.get("claims"), list) else []
                findings = copied_review.get("search_findings")
                finding_ids = {
                    str(item.get("id") or "")
                    for item in (findings if isinstance(findings, list) else [])
                    if isinstance(item, Mapping)
                }
                claim_ids = {str(item.get("search_finding_id") or "") for item in claims if isinstance(item, Mapping)}
                allowed_ids = {value for value in claim_ids | finding_ids if value}
                business_ids = profile.get("business_model_source_ids")
                if isinstance(business_ids, list) and any(str(value) not in allowed_ids for value in business_ids):
                    # A partial structured envelope is less safe than the
                    # legacy, source-free prose contract.  Keep the review's
                    # recommendation and facts, but omit the malformed
                    # business-model source graph from the public projection.
                    copied_review.pop("research_as_of", None)
                    copied_review.pop("economic_profile", None)
                    copied_review.pop("valuation", None)
                    copied_review.pop("valuation_snapshot", None)
            after = json.dumps(copied_review, ensure_ascii=False, sort_keys=True)
            if before != after:
                changed += 1
            copied_packet["ai_review"] = copied_review
        copied_packets.append(copied_packet)
    result["packets"] = copied_packets
    # Keep the public contract stable: callers historically receive the
    # changed-review count.  The command-line report exposes the detailed
    # release-boundary counters separately for audit logs.
    result["publication_sanitization"] = {
        "contract_version": 2,
        "removed_unbound_claim_count": removed_claims,
        "removed_http_source_ref_count": removed_http_refs,
        "removed_future_claim_count": removed_future_claims,
        "cleaned_search_metadata_count": cleaned_search_metadata,
    }
    return result, changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    sanitized, changed = sanitize(payload)
    args.output.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "changed_reviews": changed,
                "packet_count": len(sanitized["packets"]),
                "publication_sanitization": sanitized.get("publication_sanitization", {}),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
