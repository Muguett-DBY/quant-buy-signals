"""Publish a compact, generation-bound AI screening overlay.

The overlay is advisory only.  It deliberately contains the deterministic
decision bounds and the model's auditable claims, but never a rule-context
payload that could be mistaken for a replacement calculation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from tools.audit_ai_screening_sources import (
    AUDIT_CONTRACT_VERSION,
    public_claim_statement,
    source_semantic_projection_sha256,
)
from tools.ai_screening_comparison import build_day_over_day
from tools.ai_source_urls import (
    canonical_urls,
    claim_source_urls,
    finding_source_url,
    is_deterministic_valuation_claim,
    is_search_provenance_claim,
    iter_review_url_bindings,
    review_canonical_urls,
)
from tools.ai_screening_contract import (
    ECONOMIC_PROFILE_FIELDS,
    LOCAL_REVIEW_MODELS,
    LOCAL_OPENCODE_MODELS,
    NATIVE_COMPANY_RESEARCH_REVIEW_MODE,
    NATIVE_WEB_REVIEW_MODE,
    NATIVE_WEB_REVIEW_MODEL,
    NATIVE_WEB_RETRIEVAL_MODEL,
    PARTIAL_SEARCH_REVIEW_MODES,
    PLACEHOLDER_REVIEW_MODEL,
    REVIEW_SCHEMA_VERSION,
    VALUATION_FIELDS,
    VALUATION_SCENARIOS,
    VALUATION_SCENARIO_FIELDS,
    candidate_identity_sha256,
    native_company_research_profile_matches,
    normalise_decision_text,
    validate_review,
    valuation_snapshot_errors,
)
from tools.atomic_io import atomic_write_bytes
from tools.ai_screening_narrative import build_human_explanation

ARTIFACT_SCHEMA_VERSION = 2
ARTIFACT_KIND = "ai_screening_overlay"
SOURCE_AUDIT_CONTRACT_VERSION = AUDIT_CONTRACT_VERSION
MAX_PUBLIC_ARTIFACT_BYTES = 32 * 1024 * 1024
RANKING_VERSION = "ai-buy-attractiveness-v9-score-first-action-banded"
_DETERMINISTIC_FIELDS = (
    "status",
    "score",
    "score_lower_bound",
    "score_upper_bound",
    "decision_basis",
    "decision_complete",
    "potentially_triggerable",
    "veto_state",
)
_ACTION_PRIORITY = {"priority_buy": 0, "watchlist": 1, "avoid": 2, "insufficient_evidence": 3}
_PUBLIC_ACTIONS = ("priority_buy", "watchlist", "avoid", "insufficient_evidence")
_PUBLIC_CATEGORIES = ("recommend_buy", "observe", "do_not_recommend")
_DUPLICATE_PERCENT_SUFFIX_RE = re.compile(r"(百分点|期末口径)%")


def _final_category(action: str) -> str:
    if action == "priority_buy":
        return "recommend_buy"
    if action in {"watchlist", "insufficient_evidence"}:
        return "observe"
    return "do_not_recommend"


def _normalise_claim_pe_text(value: Any) -> str:
    """Normalise PE labels without changing source-bound claim semantics."""

    return public_claim_statement(value)


def _normalise_negative_pe_text(value: Any) -> str:
    """Make loss-making PE readable while retaining the raw numeric value."""

    text = _normalise_claim_pe_text(value)
    # Change values such as ``+0.08个百分点`` and ``期末口径`` already carry
    # their own unit or are a comparison basis, not percentages.  The legacy
    # adapter appended ``%`` to both and produced visibly invalid facts like
    # ``同比 +0.08个百分点%``.  Keep this narrow so genuine ``上年末0.84%``
    # comparisons remain untouched.
    return _DUPLICATE_PERCENT_SUFFIX_RE.sub(r"\1", text)


def _normalise_review_text(value: Any, model: str, limit: int = 1200) -> str:
    """Normalise public prose while naming the retrieval backend accurately."""

    text = _normalise_negative_pe_text(_text(value, limit))
    if model == "codex-luna-max":
        text = text.replace("已完成原生搜索", "已完成逐家公司联网检索")
        text = text.replace("原生搜索", "联网搜索")
    return text


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _audit_count(audit: Mapping[str, Any], field: str) -> int:
    value = audit.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"source audit {field} is invalid")
    return value


def _source_audit_expectations(source: Mapping[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Build the canonical URL/finding view that publication must cover."""

    expected_urls: set[str] = set()
    companies: dict[str, dict[str, Any]] = {}
    packets = source.get("packets") if isinstance(source.get("packets"), list) else []
    for packet in packets:
        if not isinstance(packet, Mapping):
            continue
        code = _text(packet.get("security_code"), 32)
        name = _text(packet.get("name") or packet.get("security_name"), 160)
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            continue
        company = companies.setdefault(
            code,
            {
                "security_code": code,
                "name": name,
                "finding_urls": {},
                "searched_no_source": set(),
                "referenced_no_source": set(),
            },
        )
        expected_urls.update(review_canonical_urls(review))
        findings = review.get("search_findings") if isinstance(review.get("search_findings"), list) else []
        findings_by_id = {
            str(finding.get("id") or "").strip(): finding
            for finding in findings
            if isinstance(finding, Mapping) and str(finding.get("id") or "").strip()
        }
        for finding_id, finding in findings_by_id.items():
            finding_url = finding_source_url(finding)
            if finding_url:
                company["finding_urls"].setdefault(finding_id, set()).add(finding_url)
        claims = review.get("claims") if isinstance(review.get("claims"), list) else []
        claim_ids_with_urls: set[str] = set()
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            finding_id = _text(claim.get("search_finding_id"), 120)
            if not finding_id:
                continue
            urls = claim_source_urls(claim)
            if not urls:
                finding = findings_by_id.get(finding_id)
                finding_url = finding_source_url(finding) if finding else ""
                if finding_url:
                    company["finding_urls"].setdefault(finding_id, set()).add(finding_url)
                else:
                    company["searched_no_source"].add(finding_id)
                    company["referenced_no_source"].add(finding_id)
                continue
            claim_ids_with_urls.add(finding_id)
            company["finding_urls"].setdefault(finding_id, set()).update(urls)
            finding = findings_by_id.get(finding_id)
            if finding and finding_source_url(finding):
                company["finding_urls"][finding_id].add(finding_source_url(finding))
        for finding_id, finding in findings_by_id.items():
            if not finding_source_url(finding) and finding_id not in claim_ids_with_urls:
                company["searched_no_source"].add(finding_id)
    return expected_urls, companies


def _source_audit_bindings(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    packets = source.get("packets") if isinstance(source.get("packets"), list) else []
    for packet in packets:
        if not isinstance(packet, Mapping):
            continue
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            continue
        code = _text(packet.get("security_code"), 32)
        name = _text(packet.get("name") or packet.get("security_name"), 160)
        type_key = _text(packet.get("type_key"), 32)
        bindings.extend(iter_review_url_bindings(review, security_code=code, name=name, type_key=type_key))
    return bindings


def _source_binding_key(binding: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(binding.get("security_code") or ""),
        str(binding.get("name") or ""),
        str(binding.get("type_key") or ""),
        binding.get("claim_index"),
        binding.get("finding_index"),
        str(binding.get("search_finding_id") or ""),
        str(binding.get("url") or ""),
        str(binding.get("kind") or ""),
    )


def _validated_source_audit(
    audit_path: Path,
    *,
    merged_sha256: str,
    generation: str,
    market_as_of: str,
    required_claim_count: int = 0,
    expected_urls: set[str] | None = None,
    expected_companies: Mapping[str, Mapping[str, Any]] | None = None,
    expected_bindings: list[Mapping[str, Any]] | None = None,
    expected_projection_sha256: str | None = None,
    expected_projection_counts: Mapping[str, int] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes.decode("utf-8"))
    if not isinstance(audit, dict):
        raise ValueError(f"expected JSON object: {audit_path}")
    if str(audit.get("merged_sha256") or "") != merged_sha256:
        raise ValueError("source audit does not match the merged AI screening file")
    if str(audit.get("snapshot_generation") or "") != generation:
        raise ValueError("source audit generation does not match the merged AI screening file")
    if str(audit.get("market_as_of") or "") != market_as_of:
        raise ValueError("source audit market_as_of does not match the merged AI screening file")
    if audit.get("audit_contract_version") != SOURCE_AUDIT_CONTRACT_VERSION:
        raise ValueError("source audit contract version is obsolete")
    projection_sha256 = str(audit.get("projection_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", projection_sha256):
        raise ValueError("source audit projection SHA-256 is invalid")
    projection_count_fields = (
        "projection_company_count",
        "projection_claim_count",
        "projection_search_finding_count",
        "projection_source_reference_count",
        "projection_unique_url_count",
    )
    projection_counts = {field: _audit_count(audit, field) for field in projection_count_fields}
    if expected_projection_sha256 is not None and projection_sha256 != expected_projection_sha256:
        raise ValueError("source audit semantic projection does not match the merged AI screening file")
    if expected_projection_counts is not None and projection_counts != {
        field: expected_projection_counts.get(field) for field in projection_count_fields
    }:
        raise ValueError("source audit semantic projection counts do not match the merged AI screening file")
    invalid_count = _audit_count(audit, "invalid_claim_url_count")
    invalid_destination_count = _audit_count(audit, "invalid")
    if invalid_count != 0 or invalid_destination_count != 0:
        raise ValueError("source audit contains invalid or non-public claim URLs")
    checked = _audit_count(audit, "checked")
    ok = _audit_count(audit, "ok")
    failed = _audit_count(audit, "failed")
    blocked = _audit_count(audit, "blocked")
    if checked != ok + failed + blocked + invalid_destination_count:
        raise ValueError("source audit result counts are inconsistent")
    if failed != 0 or (strict and blocked != 0):
        raise ValueError("source audit contains unreachable claim URLs")
    claim_count = _audit_count(audit, "claim_count")
    if projection_counts["projection_claim_count"] != claim_count:
        raise ValueError("source audit projection claim count is inconsistent")
    if projection_counts["projection_source_reference_count"] < projection_counts["projection_unique_url_count"]:
        raise ValueError("source audit projection URL counts are inconsistent")
    semantic_claim_count = _audit_count(audit, "semantic_claim_count")
    semantic_passed_count = _audit_count(audit, "semantic_passed_count")
    semantic_failed_count = _audit_count(audit, "semantic_failed_count")
    semantic_unverified_count = (
        _audit_count(audit, "semantic_unverified_count") if "semantic_unverified_count" in audit else 0
    )
    if semantic_claim_count != semantic_passed_count + semantic_failed_count + semantic_unverified_count:
        raise ValueError("source audit semantic result counts are inconsistent")
    if semantic_failed_count != 0:
        raise ValueError("source audit contains semantic source mismatches")
    if strict and semantic_unverified_count != 0:
        raise ValueError("source audit contains unverified non-HTML sources")
    if strict and audit.get("audit_passed") is not True:
        raise ValueError("source audit did not pass")
    if required_claim_count:
        if claim_count < required_claim_count:
            raise ValueError("source audit does not cover every company research claim")
        if semantic_claim_count < required_claim_count:
            raise ValueError("source audit does not semantically cover every company research claim")
        if audit.get("audit_passed") is not True:
            raise ValueError("company-research source audit did not pass")
    canonical_urls_value = audit.get("canonical_urls")
    if isinstance(canonical_urls_value, list) and projection_counts["projection_unique_url_count"] != len(
        canonical_urls_value
    ):
        raise ValueError("source audit projection URL count does not match canonical URLs")
    if expected_urls is not None:
        if not isinstance(canonical_urls_value, list) or any(
            not isinstance(value, str) for value in canonical_urls_value
        ):
            if strict:
                raise ValueError("source audit canonical URL set is missing")
        elif set(canonical_urls_value) != expected_urls:
            raise ValueError("source audit canonical URL set does not match published sources")
    if strict and expected_bindings is not None:
        actual_bindings = audit.get("source_bindings")
        if not isinstance(actual_bindings, list) or any(not isinstance(item, Mapping) for item in actual_bindings):
            raise ValueError("source audit source bindings are missing")
        expected_keys = Counter(_source_binding_key(item) for item in expected_bindings)
        actual_keys = Counter(_source_binding_key(item) for item in actual_bindings)
        if actual_keys != expected_keys:
            raise ValueError("source audit source bindings do not match published sources")
    coverage = audit.get("company_coverage")
    if strict:
        if not isinstance(coverage, list):
            raise ValueError("source audit company coverage is missing")
        coverage_by_code = {
            str(item.get("security_code") or ""): item for item in coverage if isinstance(item, Mapping)
        }
        for code, expected in (expected_companies or {}).items():
            actual = coverage_by_code.get(code)
            if not actual:
                raise ValueError(f"source audit company coverage is missing: {code}")
            expected_ids = set(expected.get("finding_urls", {}))
            actual_ids = set(str(value) for value in actual.get("referenced_finding_ids", []) if str(value))
            if not expected_ids.issubset(actual_ids):
                raise ValueError(f"source audit company finding coverage is incomplete: {code}")
            expected_no_source = set(expected.get("searched_no_source", set()))
            actual_no_source = set(str(value) for value in actual.get("searched_no_source_finding_ids", []))
            if not expected_no_source.issubset(actual_no_source):
                raise ValueError(f"source audit searched_no_source coverage is incomplete: {code}")
            expected_referenced_no_source = set(expected.get("referenced_no_source", set()))
            actual_referenced_no_source = set(
                str(value) for value in actual.get("referenced_no_source_finding_ids", []) if str(value)
            )
            if not actual_referenced_no_source and expected_referenced_no_source:
                # Reports written before this field existed can still be
                # diagnosed from the two older coverage lists.
                actual_referenced_no_source = actual_no_source & actual_ids
            if not expected_referenced_no_source.issubset(actual_referenced_no_source):
                raise ValueError(f"source audit cited finding has no URL: {code}")
            if actual_referenced_no_source:
                raise ValueError(f"source audit cited finding has no URL: {code}")
            if (
                not isinstance(actual.get("semantic_claim_count"), int)
                or actual.get("semantic_claim_count") < 0
                or (
                    actual.get("semantic_failed_count") != 0
                    or actual.get("semantic_unverified_count") != 0
                    or actual.get("semantic_passed_count") != actual.get("semantic_claim_count")
                )
            ):
                raise ValueError(f"source audit company findings did not all semantically pass: {code}")
    return {
        "available": True,
        "audit_contract_version": SOURCE_AUDIT_CONTRACT_VERSION,
        "audit_passed": (
            failed == 0
            and invalid_count == 0
            and invalid_destination_count == 0
            and semantic_failed_count == 0
            and semantic_unverified_count == 0
        ),
        "audit_sha256": hashlib.sha256(audit_bytes).hexdigest(),
        "merged_sha256": merged_sha256,
        "projection_sha256": projection_sha256,
        **projection_counts,
        "invalid_claim_url_count": invalid_count,
        "checked": checked,
        "ok": ok,
        "failed": failed,
        "blocked": blocked,
        "claim_count": claim_count,
        "semantic_claim_count": semantic_claim_count,
        "semantic_passed_count": semantic_passed_count,
        "semantic_failed_count": semantic_failed_count,
        "semantic_unverified_count": semantic_unverified_count,
        "semantic_issue_count": _audit_count(audit, "semantic_issue_count"),
        "network_warnings_allowed": False,
        "release_status": "passed",
        "semantic_html_date_checked_count": _audit_count(audit, "semantic_html_date_checked_count"),
        "published_at_mismatch_count": _audit_count(audit, "published_at_mismatch_count"),
        "report_period_after_publication_count": _audit_count(audit, "report_period_after_publication_count"),
        "blocked_semantic_claim_count": _audit_count(audit, "blocked_semantic_claim_count"),
        "canonical_urls": sorted(str(value) for value in canonical_urls_value)
        if isinstance(canonical_urls_value, list)
        else None,
        "company_coverage": coverage if isinstance(coverage, list) else None,
        "company_source_issues": _source_issue_projection(audit),
        "source_bindings": audit.get("source_bindings") if isinstance(audit.get("source_bindings"), list) else None,
    }


def _text(value: Any, limit: int = 800) -> str:
    return str(value or "").strip()[:limit]


def _source_verification_metadata(
    source_audit: Mapping[str, Any], published_codes: set[str]
) -> tuple[dict[str, dict[str, Any]], int] | None:
    """Return per-company source status and the number of affected companies.

    ``company_coverage`` is the source audit's company-level projection.  Keep
    content mismatches as ``failed`` while exposing access/parse limitations as
    ``unverified``.  A missing/unknown status is deliberately surfaced as
    ``unverified`` rather than silently treated as a clean source.  The bounded
    issue details make the warning actionable in the website without copying
    the full audit report into every company card.
    """

    coverage = source_audit.get("company_coverage")
    if not isinstance(coverage, list):
        return None
    issue_projection = source_audit.get("company_source_issues")
    if not isinstance(issue_projection, Mapping):
        issue_projection = _source_issue_projection(source_audit)

    by_code: dict[str, dict[str, Any]] = {}
    for row in coverage:
        if not isinstance(row, Mapping):
            continue
        code = _text(row.get("security_code"), 32)
        if not code:
            continue
        semantic_failed = row.get("semantic_failed_count")
        semantic_unverified = row.get("semantic_unverified_count")
        semantic_failed = semantic_failed if isinstance(semantic_failed, int) and semantic_failed >= 0 else 0
        semantic_unverified = (
            semantic_unverified if isinstance(semantic_unverified, int) and semantic_unverified >= 0 else 0
        )
        status = _text(row.get("status"), 16).lower()
        if status not in {"pass", "failed", "unverified"}:
            status = "unverified"
        projected = issue_projection.get(code)
        projected = projected if isinstance(projected, Mapping) else {}
        issue_kinds = set(projected.get("issue_kinds", {}))
        if "content_mismatch" in issue_kinds:
            status = "failed"
        elif issue_kinds & {"access", "unverified"}:
            # A timeout, block page, or non-text response is not evidence that
            # the AI search was wrong.  Surface it as unresolved verification,
            # reserving the red failed state for an actual content mismatch.
            status = "unverified"
        elif status == "pass" and semantic_failed:
            status = "failed"
        elif status == "pass" and semantic_unverified:
            status = "unverified"
        issue_count = semantic_failed + semantic_unverified
        if status != "pass" and issue_count == 0:
            issue_count = 1
        metadata: dict[str, Any] = {"status": status, "issue_count": issue_count}
        details = projected.get("issues")
        if details:
            metadata["issues"] = list(details)
            metadata["issue_kinds"] = dict(sorted(projected["issue_kinds"].items()))
        by_code[code] = metadata

    affected_company_count = 0
    for code in published_codes:
        if by_code.get(code, {"status": "unverified"})["status"] != "pass":
            affected_company_count += 1
    return by_code, affected_company_count


def _source_issue_kind(reason: Any) -> str:
    lowered_reason = _text(reason, 320).casefold()
    if "unverified" in lowered_reason:
        return "unverified"
    # A dynamic HTML landing page often omits the issuer code, report period,
    # or exact fact even when the URL is the right filing/search result.  That
    # is a verification limitation, not proof of contradictory content.
    if (
        "html正文未匹配" in lowered_reason
        or "html body does not match" in lowered_reason
        or "html正文未找到" in lowered_reason
        or "pdf text does not match report period or fact number" in lowered_reason
        or "structured source body does not match report period or fact number/field" in lowered_reason
    ):
        return "unverified"
    if any(
        marker in lowered_reason
        for marker in (
            "body unavailable",
            "blocked",
            "captcha",
            "login",
            "soft-404",
            "no visible text",
            "timeout",
            "forbidden",
            # The direct-origin recheck can fail because the publisher cannot
            # retrieve the same body again.  That is unresolved access, not
            # proof that the claim contradicts the source.
            "source semantic verification failed in the prior direct-origin pass",
            "semantic verification failed in the prior direct-origin pass",
        )
    ):
        return "access"
    return "content_mismatch"


def _source_verification_summary(summary: Any, status: str) -> str:
    """Align the visible provenance sentence with the audited source status.

    The model review is allowed to say that it bound facts to sources, but it
    must not claim that those sources passed when the release audit marked the
    company as failed or unresolved.  Keep the original thesis intact and
    replace only the stale provenance clause.
    """

    text = _text(summary, 1200)
    if status == "pass":
        return text
    if status == "failed":
        note = "来源复核提示：部分引用未通过公司、期间或数字匹配，相关事实应以原始公告为准"
    else:
        note = "来源复核提示：部分引用因访问或正文解析限制暂未自动确认，不能单独作为事实依据"
    verified_clause = "公司财务事实来源已绑定并通过来源核验"
    if verified_clause in text:
        return text.replace(verified_clause, note, 1)
    stale_clause = "公司财务事实来源已绑定"
    if stale_clause in text:
        return text.replace(stale_clause, note, 1)
    if text:
        return f"{note}。{text}"
    return note + "。"


def _source_issue_projection(source_audit: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Build bounded, company-level source warning details for the public card."""

    semantic_issues = source_audit.get("semantic_issues")
    if not isinstance(semantic_issues, list):
        return {}
    details_by_code: dict[str, list[dict[str, Any]]] = {}
    keys_by_code: dict[str, set[tuple[str, str, str, str, str]]] = {}
    kinds_by_code: dict[str, Counter[str]] = {}
    for issue in semantic_issues:
        if not isinstance(issue, Mapping):
            continue
        code = _text(issue.get("security_code"), 32)
        if not code:
            continue
        kind = _source_issue_kind(issue.get("reason"))
        source_urls = canonical_urls(issue.get("source"))
        source_url = source_urls[0] if source_urls else ""
        claim_index = _text(issue.get("claim_index"), 24)
        finding_index = _text(issue.get("finding_index"), 24)
        finding_id = _text(issue.get("search_finding_id"), 120)
        issue_key = (claim_index, finding_index, finding_id, source_url, kind)
        keys = keys_by_code.setdefault(code, set())
        if issue_key in keys:
            continue
        keys.add(issue_key)
        kinds_by_code.setdefault(code, Counter())[kind] += 1
        details = details_by_code.setdefault(code, [])
        if len(details) >= 4:
            continue
        detail: dict[str, Any] = {"kind": kind, "reason": _text(issue.get("reason"), 320)}
        if source_url:
            detail["url"] = source_url
        for field, value in (
            ("claim_index", claim_index),
            ("finding_index", finding_index),
            ("search_finding_id", finding_id),
            ("type_key", _text(issue.get("type_key"), 32)),
        ):
            if value and value.casefold() != "none":
                detail[field] = value
        details.append(detail)
    return {
        code: {
            "issues": details_by_code.get(code, []),
            "issue_kinds": dict(sorted(kinds_by_code.get(code, Counter()).items())),
        }
        for code in sorted(kinds_by_code)
    }


def _source_text(value: Any, limit: int = 800) -> str:
    """Preserve complete URLs; only prose-only metadata gets a text cap."""

    urls = canonical_urls(value)
    return urls[0] if urls else _text(value, limit)


def _public_artifact_bytes(artifact: Mapping[str, Any]) -> bytes:
    output_bytes = (json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(output_bytes) > MAX_PUBLIC_ARTIFACT_BYTES:
        raise ValueError(f"public AI screening artifact exceeds {MAX_PUBLIC_ARTIFACT_BYTES} bytes")
    return output_bytes


def _public_company_research(review: Mapping[str, Any], public_claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the compact, validated company thesis safe for the public API."""

    if not all(field in review for field in ("research_as_of", "economic_profile", "valuation")):
        return {}
    profile = review["economic_profile"]
    valuation = review["valuation"]
    snapshot = review.get("valuation_snapshot")
    if not isinstance(profile, Mapping) or not isinstance(valuation, Mapping):
        return {}
    public_valuation: dict[str, Any] = {}
    for field in VALUATION_FIELDS:
        value = valuation.get(field)
        if field in {"method", "as_of", "basis", "safety_margin_band"}:
            limit = (
                1000 if field == "basis" else 80 if field == "method" else 16 if field == "safety_margin_band" else 10
            )
            public_valuation[field] = _text(value, limit)
        elif field == "scenarios":
            if value is None:
                public_valuation[field] = None
            elif isinstance(value, Mapping):
                public_scenarios: dict[str, dict[str, float]] = {}
                for scenario in VALUATION_SCENARIOS:
                    raw = value.get(scenario)
                    if not isinstance(raw, Mapping):
                        continue
                    public_scenarios[scenario] = {
                        item_field: float(raw[item_field])
                        for item_field in VALUATION_SCENARIO_FIELDS
                        if raw.get(item_field) not in (None, "")
                    }
                public_valuation[field] = public_scenarios
            else:
                public_valuation[field] = None
        elif value is None or value == "":
            public_valuation[field] = None
        else:
            public_valuation[field] = float(value)
    evidence_ids = valuation.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        public_valuation["evidence_ids"] = []
    else:
        public_valuation["evidence_ids"] = [_text(value, 120) for value in evidence_ids[:32]]

    anchor = valuation.get("normalization_anchor")
    if anchor is None:
        public_valuation["normalization_anchor"] = None
    elif isinstance(anchor, Mapping):
        public_valuation["normalization_anchor"] = {
            "metric": _text(anchor.get("metric"), 64),
            "years": [
                int(value) for value in (anchor.get("years") if isinstance(anchor.get("years"), list) else [])[:16]
            ],
            "total": float(anchor["total"]) if anchor.get("total") not in (None, "") else None,
            "share_count": float(anchor["share_count"]) if anchor.get("share_count") not in (None, "") else None,
            "per_share": float(anchor["per_share"]) if anchor.get("per_share") not in (None, "") else None,
            "source_ref": _source_text(anchor.get("source_ref"), 160),
        }
    else:
        raise ValueError("company research has invalid normalization_anchor")

    multiple_basis = valuation.get("multiple_basis")
    if multiple_basis is None:
        public_valuation["multiple_basis"] = None
    elif isinstance(multiple_basis, Mapping):
        public_valuation["multiple_basis"] = {
            "metric": _text(multiple_basis.get("metric"), 16),
            "value": float(multiple_basis["value"]) if multiple_basis.get("value") not in (None, "") else None,
            "source_ref": _source_text(multiple_basis.get("source_ref"), 800),
            "search_finding_id": _text(multiple_basis.get("search_finding_id"), 120) or None,
        }
    else:
        raise ValueError("company research has invalid multiple_basis")
    raw_source_ids = profile.get("business_model_source_ids")
    source_ids = [_text(value, 120) for value in raw_source_ids] if isinstance(raw_source_ids, list) else []
    source_quality = _text(profile.get("business_model_source_quality"), 32)
    if source_quality == "not_found":
        if source_ids:
            raise ValueError("not_found company research cannot cite business-model source IDs")
    elif (
        not source_ids
        or len(source_ids) > 16
        or any(not value for value in source_ids)
        or len(source_ids) != len(set(source_ids))
    ):
        raise ValueError("company research has invalid business-model source IDs")
    claim_rows = [
        (str(claim.get("search_finding_id") or ""), claim)
        for claim in public_claims
        if str(claim.get("search_finding_id") or "")
    ]
    claims_by_finding_id = dict(claim_rows)
    if len(claims_by_finding_id) != len(claim_rows):
        raise ValueError("company research has duplicate public search-finding IDs")
    finding_rows = [
        (str(finding.get("id") or ""), finding)
        for finding in review.get("search_findings", [])
        if isinstance(finding, Mapping) and str(finding.get("id") or "")
    ]
    findings_by_id = dict(finding_rows)
    if len(findings_by_id) != len(finding_rows):
        raise ValueError("company research has duplicate search-finding IDs")
    business_sources: list[dict[str, Any]] = []
    for source_id in source_ids:
        claim = claims_by_finding_id.get(source_id)
        if not claim:
            finding = findings_by_id.get(source_id)
            if source_quality != "not_found" or not finding:
                raise ValueError(f"business-model source is absent from public claims: {source_id}")
            claim = {
                "statement": finding.get("finding"),
                "source_ref": "",
                "source_kind": finding.get("source_kind"),
            }
        source_ref = _source_text(claim.get("source_ref"), 800)
        claim_urls = claim_source_urls(claim)
        if not canonical_urls(source_ref) and claim_urls:
            source_ref = claim_urls[0]
        business_sources.append(
            {
                "id": source_id,
                "statement": _text(claim.get("statement"), 600),
                "source_ref": source_ref,
                "source_kind": _text(claim.get("source_kind"), 48),
            }
        )
    return {
        "research_as_of": _text(review.get("research_as_of"), 10),
        "economic_profile": {
            **{field: _text(profile.get(field), 600) for field in ECONOMIC_PROFILE_FIELDS},
            "business_model_source_ids": source_ids,
            "business_model_sources": business_sources,
            "business_model_source_quality": source_quality,
            "business_model_source_status": "searched_no_source" if source_quality == "not_found" else "source_found",
            "business_model_uncertainty": _text(profile.get("business_model_uncertainty"), 600),
        },
        "valuation": public_valuation,
        "valuation_snapshot": {
            "contract_version": snapshot.get("contract_version"),
            "security_code": _text(snapshot.get("security_code"), 16),
            "snapshot_generation": _text(snapshot.get("snapshot_generation"), 80),
            "market_as_of": _text(snapshot.get("market_as_of"), 10),
            "current_price": float(snapshot["current_price"]),
            "pe": float(snapshot["pe"]) if snapshot.get("pe") is not None else None,
            "pb": float(snapshot["pb"]) if snapshot.get("pb") is not None else None,
            "market_cap": float(snapshot["market_cap"]) if snapshot.get("market_cap") is not None else None,
            "canonical_sha256": _text(snapshot.get("canonical_sha256"), 64),
        }
        if isinstance(snapshot, Mapping)
        else None,
    }


def _public_review(
    review: Mapping[str, Any],
    *,
    require_readable_reason: bool = False,
    claims_are_search_results: bool = True,
    require_company_research_fields: bool = False,
    company_name: str = "",
) -> dict[str, Any]:
    errors = validate_review(
        review,
        require_readable_reason=require_readable_reason,
        require_company_research_fields=require_company_research_fields,
    )
    if errors:
        raise ValueError(f"invalid AI review: {','.join(errors)}")
    claims: list[dict[str, Any]] = []
    for claim in review.get("claims", []):
        if not isinstance(claim, Mapping):
            raise ValueError("AI claim must be an object")
        if is_search_provenance_claim(claim):
            # The query/event attestation is retained in the review metadata;
            # a raw search-result transcript is not a company fact and should
            # not become a clickable evidence card or semantic-audit edge.
            continue
        valuation_snapshot_claim = is_deterministic_valuation_claim(claim)
        raw_sources: list[str] = []
        singular_source = "" if valuation_snapshot_claim else str(claim.get("source_ref") or "").strip()
        if singular_source:
            raw_sources.append(singular_source)
        if not valuation_snapshot_claim and isinstance(claim.get("source_refs"), list):
            raw_sources.extend(str(value).strip() for value in claim["source_refs"] if str(value).strip())
        raw_source = raw_sources[0] if raw_sources else ""
        raw_context = "" if valuation_snapshot_claim else str(claim.get("source_context") or "").strip()
        if not raw_source:
            # Some OpenCode tool responses put the returned URL in a separate
            # source_context field.  Reuse it only when it is an actual URL;
            # never manufacture a link from a search summary.
            raw_source = raw_context
        source_refs: list[str] = []
        candidates = raw_sources + ([raw_context] if raw_context and raw_context not in raw_sources else [])
        for candidate in candidates or ([raw_source] if raw_source else []):
            for source_ref in canonical_urls(candidate):
                if source_ref not in source_refs:
                    source_refs.append(source_ref)
        context_value = (
            "估值快照来自本代市场数据" if valuation_snapshot_claim else (raw_context if raw_context else raw_source)
        )
        context_value = context_value if canonical_urls(context_value) else _text(context_value, 240)
        public_claim: dict[str, Any] = {
            "statement": _normalise_claim_pe_text(_text(claim.get("statement"), 600)),
            "source_ref": source_refs[0] if source_refs else "",
            "source_context": context_value,
            "support": _text(claim.get("support"), 16),
        }
        fact_id = _text(claim.get("fact_id"), 120)
        if fact_id:
            public_claim["fact_id"] = fact_id
        search_finding_id = _text(claim.get("search_finding_id"), 120)
        source_kind = _text(claim.get("source_kind"), 48)
        if search_finding_id:
            public_claim["search_finding_id"] = search_finding_id
        if source_kind:
            public_claim["source_kind"] = source_kind
        if source_refs:
            public_claim["source_refs"] = source_refs
        claims.append(public_claim)
    web_search_performed = review.get("web_search_performed") is True
    has_https_claim = any(
        str(claim.get("source_ref") or "").lower().startswith("https://")
        for claim in claims
        if isinstance(claim, Mapping)
    )
    computed_web_verified = bool(claims_are_search_results and web_search_performed and has_https_claim)
    # An explicit false is an attestation that the tool event was not proven;
    # an explicit true still has to pass the HTTPS claim check.
    web_search_verified = False if review.get("web_search_verified") is False else computed_web_verified
    action = _text(review.get("ai_action"), 32)
    profile = review.get("economic_profile")
    if (
        isinstance(profile, Mapping)
        and _text(profile.get("business_model_source_quality"), 32) == "not_found"
        and action not in {"watchlist", "avoid", "insufficient_evidence"}
    ):
        raise ValueError("not_found company research is limited to observe or do_not_recommend")
    final_category = _text(review.get("final_category"), 32) or _final_category(action)
    if final_category not in {"recommend_buy", "observe", "do_not_recommend"}:
        raise ValueError("AI final_category is invalid")
    recommendation = _text(review.get("final_recommendation"), 32)
    if recommendation not in {"recommend_buy", "do_not_recommend_buy"}:
        recommendation = "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy"
    if (
        isinstance(profile, Mapping)
        and _text(profile.get("business_model_source_quality"), 32) == "not_found"
        and (final_category == "recommend_buy" or recommendation == "recommend_buy")
    ):
        raise ValueError("not_found company research cannot be published as a recommendation")
    label = _text(review.get("recommendation_label"), 64)
    if not label:
        label = "建议买" if recommendation == "recommend_buy" else "观察" if action == "watchlist" else "不建议"
    model = _text(review.get("model"), 120)
    summary = normalise_decision_text(_text(review.get("summary"), 1200))
    if action == "watchlist":
        summary = summary.replace("当前结论：不建议买", "当前结论：观察（暂不建议买）")
    summary = _normalise_review_text(summary, model, 1200)
    quantitative_facts = [_normalise_review_text(item, model, 240) for item in review.get("quantitative_facts", [])[:8]]
    fact_bindings: list[dict[str, Any]] = []
    # Financial bindings are the lossless audit trail.  Unlike prose lists,
    # they must not be truncated: a public row must expose every dated fact
    # that was checked by the release audit.
    for binding in review.get("financial_fact_bindings", []):
        if isinstance(binding, Mapping):
            fact_bindings.append(dict(binding))
    numeric_fact_repairs = [
        dict(item) for item in review.get("numeric_fact_repairs", [])[:32] if isinstance(item, Mapping)
    ]
    public_findings: list[dict[str, Any]] = []
    if require_company_research_fields:
        for finding in review.get("search_findings", []):
            if not isinstance(finding, Mapping):
                continue
            public_findings.append(
                {
                    "id": _text(finding.get("id"), 120),
                    "query": _text(finding.get("query"), 240),
                    "title": _text(finding.get("title"), 300),
                    "url": finding_source_url(finding) or None,
                    "published_at": _text(finding.get("published_at"), 32) or None,
                    "report_period": _text(finding.get("report_period"), 80) or None,
                    "finding": _text(finding.get("finding"), 600),
                    "stance": _text(finding.get("stance"), 16),
                    "source_kind": _text(finding.get("source_kind"), 48),
                    "source_quality": _text(finding.get("source_quality"), 32),
                }
            )
    public_review = {
        "verdict": _text(review.get("verdict"), 32),
        "recommended_action": _text(review.get("recommended_action"), 32),
        "buy_attractiveness_score": float(review["buy_attractiveness_score"]),
        "ai_action": action,
        "final_category": final_category,
        "final_recommendation": recommendation,
        "recommendation_label": label,
        "ai_independent": bool(review.get("ai_independent", False)),
        "confidence": _text(review.get("confidence"), 16),
        "summary": summary,
        # Keep the exact model summary for the audit trail, but expose a
        # separate deterministic, human-first explanation for the website.
        # It is derived only from the already reviewed strengths/risks.
        "human_explanation": build_human_explanation(
            review,
            _text(company_name, 160) or _text(review.get("company_name") or review.get("name"), 160) or "该公司",
        ),
        "quantitative_facts": quantitative_facts,
        "financial_fact_bindings": fact_bindings,
        "numeric_fact_repairs": numeric_fact_repairs,
        "key_strengths": [_normalise_review_text(item, model, 240) for item in review.get("key_strengths", [])[:8]],
        "risk_flags": [_normalise_review_text(item, model, 240) for item in review.get("risk_flags", [])[:12]],
        "claims": claims,
        "model": model,
        "effort": _text(review.get("effort"), 32),
        "retrieval_backend": _text(review.get("retrieval_backend"), 96),
        "retrieval_model": _text(review.get("retrieval_model"), 120),
        "retrieval_effort": _text(review.get("retrieval_effort"), 32),
        "native_search_completed": review.get("native_search_completed") is True,
        "official_fetch_completed": review.get("official_fetch_completed") is True,
        "web_search_performed": web_search_performed,
        "web_search_event_verified": review.get("web_search_event_verified") is True,
        "web_search_claim_urls_verified": review.get("web_search_claim_urls_verified") is True,
        "research_source_urls_verified": review.get("research_source_urls_verified") is True,
        "web_search_queries": [_text(value, 240) for value in review.get("web_search_queries", [])[:16]],
        "web_search_event_ids": [_text(value, 160) for value in review.get("web_search_event_ids", [])[:32]],
        "web_search_event_log_sha256": [
            _text(value, 64) for value in review.get("web_search_event_log_sha256", [])[:16]
        ],
        "web_search_thread_ids": [_text(value, 160) for value in review.get("web_search_thread_ids", [])[:16]],
        "web_search_query_count": len(review.get("web_search_queries") or []),
        "web_search_verified_claim_url_count": len(review.get("web_search_verified_claim_urls") or []),
        "web_search_dropped_claim_url_count": int(review.get("web_search_dropped_claim_url_count", 0) or 0),
        "web_search_verified": web_search_verified,
        "freshness_status": _text(review.get("freshness_status"), 32) or "undated",
        "freshness_years": [int(year) for year in review.get("freshness_years", [])[:12]],
        "freshness_penalty": float(review.get("freshness_penalty", 0.0) or 0.0),
        "freshness_note": _text(review.get("freshness_note"), 180),
        **_public_company_research(review, claims),
    }
    # Keep the independent calibration visible for the legacy/local review
    # path as well.  Without this envelope a 69/89/95 number looks like an
    # opaque model assertion and the user cannot see why valuation or current
    # cash-flow evidence changed the action.
    components = review.get("score_components")
    if isinstance(components, Mapping):
        public_review["score_components"] = {
            field: float(components[field])
            for field in ("risk_adjusted_expected_return", "evidence_confidence")
            if components.get(field) not in (None, "")
        }
    adjustments = review.get("calibration_adjustments")
    if isinstance(adjustments, Mapping):
        public_review["calibration_adjustments"] = {
            key: adjustments[key]
            for key in (
                "raw_score",
                "source_penalty",
                "freshness_penalty",
                "pre_band_score",
                "action_band_min",
                "action_band_max",
                "final_score",
                "source_quality",
                "freshness_status",
                "band_clamped",
                "verdict",
                "quality_gate_version",
                "quality_gate_applied",
                "quality_penalty",
                "quality_cap",
                "quality_hard_block",
                "quality_reasons",
                "quality_metrics",
            )
            if key in adjustments
        }
    quality_gate = review.get("quality_gate")
    if isinstance(quality_gate, Mapping):
        public_review["quality_gate"] = {
            "version": _text(quality_gate.get("version"), 80),
            "applied": quality_gate.get("applied") is True,
            "penalty": float(quality_gate.get("penalty", 0.0) or 0.0),
            "cap": float(quality_gate["cap"]) if quality_gate.get("cap") not in (None, "") else None,
            "hard_block": quality_gate.get("hard_block") is True,
            "reasons": [_text(value, 240) for value in quality_gate.get("reasons", [])[:8]],
            "metrics": {
                str(key): float(value) if isinstance(value, (int, float)) else value
                for key, value in (quality_gate.get("metrics") or {}).items()
                if value is not None
            },
        }
    semantic_review = review.get("semantic_review")
    if isinstance(semantic_review, Mapping):
        external = semantic_review.get("external_review")
        external_public = external if isinstance(external, Mapping) else {}
        public_review["semantic_review"] = {
            "version": _text(semantic_review.get("version"), 80),
            "scope": _text(semantic_review.get("scope"), 80),
            "review_status": _text(semantic_review.get("review_status"), 48),
            "conclusion": _text(semantic_review.get("conclusion"), 32),
            "score": float(semantic_review.get("score", 0.0) or 0.0),
            "confidence": _text(semantic_review.get("confidence"), 16),
            "basis": _text(semantic_review.get("basis"), 600),
            "quantitative_facts": [_text(item, 300) for item in semantic_review.get("quantitative_facts", [])[:8]],
            "reasons": [_text(item, 300) for item in semantic_review.get("reasons", [])[:8]],
            "metrics": {
                str(key): float(value) if isinstance(value, (int, float)) else value
                for key, value in (semantic_review.get("metrics") or {}).items()
                if value is not None
            },
            "knowledge_base": _text(semantic_review.get("knowledge_base"), 160),
            "reviewer_note": _text(semantic_review.get("reviewer_note"), 600),
            "external_review": {
                "performed": external_public.get("performed") is True,
                "source_urls": [
                    _text(value, 600)
                    for value in external_public.get("source_urls", [])[:4]
                    if isinstance(value, str) and value.startswith("https://")
                ],
                "note": _text(external_public.get("note"), 600),
            },
        }
    if require_company_research_fields:
        public_review.update(
            {
                "economic_category": _text(review.get("economic_category"), 32),
                "score_components": {
                    field: float(review["score_components"][field])
                    for field in ("risk_adjusted_expected_return", "evidence_confidence")
                },
                "calibration_adjustments": {
                    key: review["calibration_adjustments"][key]
                    for key in (
                        "raw_score",
                        "source_penalty",
                        "freshness_penalty",
                        "pre_band_score",
                        "action_band_min",
                        "action_band_max",
                        "final_score",
                        "source_quality",
                        "freshness_status",
                        "band_clamped",
                        "verdict",
                    )
                },
                "evidence_bindings": review.get("evidence_bindings"),
                "search_findings": public_findings,
            }
        )
    return public_review


def _public_web_verified(review: Mapping[str, Any]) -> bool:
    """Return whether a usable URL came from the search event itself."""
    return review.get("web_search_verified") is True


def _public_deterministic(packet: Mapping[str, Any]) -> dict[str, Any]:
    source = packet.get("deterministic")
    if not isinstance(source, Mapping):
        raise ValueError("candidate deterministic result is missing")
    decision = source.get("decision") if isinstance(source.get("decision"), Mapping) else {}
    result: dict[str, Any] = {}
    for field in _DETERMINISTIC_FIELDS:
        value = source.get(field)
        if value is None:
            value = decision.get(field)
        if value is not None:
            result[field] = value
    return result


def _type_key_sort(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"type([1-7])", value)
    return (int(match.group(1)) if match else 99, value)


def _candidate_type_keys(packet: Mapping[str, Any], *, primary_type_key: str) -> list[str]:
    candidate_types = packet.get("candidate_types")
    if isinstance(candidate_types, list) and candidate_types:
        type_keys = [
            _text(item.get("type_key") or item.get("type"), 16) for item in candidate_types if isinstance(item, Mapping)
        ]
        if len(type_keys) != len(candidate_types):
            raise ValueError("candidate_types must contain objects")
    elif isinstance(packet.get("type_keys"), list) and packet["type_keys"]:
        type_keys = [_text(value, 16) for value in packet["type_keys"]]
    else:
        type_keys = [primary_type_key]
    if (
        not type_keys
        or primary_type_key not in type_keys
        or len(type_keys) != len(set(type_keys))
        or any(not re.fullmatch(r"type[1-7]", value) for value in type_keys)
    ):
        raise ValueError("candidate type coverage is invalid")
    return sorted(type_keys, key=_type_key_sort)


def _public_candidate_types(packet: Mapping[str, Any], type_keys: list[str]) -> list[dict[str, Any]]:
    """Compact every deterministic candidate type when the queue carries it."""

    candidate_types = packet.get("candidate_types")
    if not isinstance(candidate_types, list) or not candidate_types:
        return []
    public: list[dict[str, Any]] = []
    for item in candidate_types:
        if not isinstance(item, Mapping):
            raise ValueError("candidate_types must contain objects")
        type_key = _text(item.get("type_key") or item.get("type"), 16)
        public.append(
            {
                "type_key": type_key,
                "deterministic": _public_deterministic({"deterministic": item.get("deterministic")}),
            }
        )
    public.sort(key=lambda value: _type_key_sort(value["type_key"]))
    if [value["type_key"] for value in public] != type_keys:
        raise ValueError("candidate_types do not match type_keys")
    return public


def build_artifact(
    merged_path: Path,
    output_path: Path,
    *,
    expected_generation: str,
    expected_market_as_of: str,
    source_audit_path: Path | None = None,
    previous_ai_path: Path | None = None,
) -> dict[str, Any]:
    merged_bytes = merged_path.read_bytes()
    merged_sha256 = hashlib.sha256(merged_bytes).hexdigest()
    source = json.loads(merged_bytes.decode("utf-8"))
    if not isinstance(source, dict):
        raise ValueError(f"expected JSON object: {merged_path}")
    generation = str(source.get("snapshot_generation") or "")
    market_as_of = str(source.get("market_as_of") or "")
    if generation != expected_generation:
        raise ValueError(f"generation mismatch: {generation!r} != {expected_generation!r}")
    if market_as_of != expected_market_as_of:
        raise ValueError(f"market_as_of mismatch: {market_as_of!r} != {expected_market_as_of!r}")
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("merged AI screening packets are missing")
    candidate_offset = int(source.get("candidate_offset", 0) or 0)
    source_candidate_count = int(source.get("candidate_count", len(packets)) or 0)
    source_candidate_total = int(source.get("candidate_total", len(packets)) or 0)
    source_pair_candidate_count = int(source.get("type_pair_candidate_count", source_candidate_count) or 0)
    source_pair_candidate_total = int(source.get("type_pair_candidate_total", source_candidate_total) or 0)
    source_pair_identity_digest = str(source.get("type_pair_candidate_identity_sha256") or "")
    identity_digest = candidate_identity_sha256(packet for packet in packets if isinstance(packet, Mapping))
    declared_identity_digest = str(source.get("candidate_identity_sha256") or "")
    universe_identity_digest = str(source.get("candidate_universe_identity_sha256") or "")
    if declared_identity_digest and declared_identity_digest != identity_digest:
        raise ValueError("candidate identity hash does not match the publication queue")
    company_records: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    queue_pair_count = 0
    pair_verdicts: Counter[str] = Counter()
    pair_attempted_review_count = 0
    pair_unreviewed_candidate_count = 0
    pair_attempted_needs_review_count = 0
    pair_web_search_attempted_count = 0
    pair_web_search_completed_count = 0
    pair_web_search_event_verified_count = 0
    pair_web_search_claim_urls_verified_count = 0
    pair_research_source_urls_verified_count = 0
    pair_web_search_dropped_claim_url_count = 0
    review_models: set[str] = set()
    review_efforts: set[str] = set()
    review_model_efforts: set[tuple[str, str]] = set()
    research_dates: set[str] = set()
    full_coverage = source.get("full_coverage_final_recommendation") is True
    review_mode = _text(source.get("review_mode"), 64) or "external_ai_review"
    if full_coverage and not (
        candidate_offset == 0
        and source_candidate_count == source_candidate_total == len(packets)
        and declared_identity_digest == identity_digest == universe_identity_digest
    ):
        raise ValueError("full-coverage artifact does not contain the complete candidate queue")
    if full_coverage and review_mode not in PARTIAL_SEARCH_REVIEW_MODES and source_audit_path is None:
        raise ValueError("external full-coverage artifact requires a bound source audit")
    if full_coverage and review_mode == "opencode_mixed_review" and source_audit_path is None:
        raise ValueError("mixed full-coverage artifact requires a bound source audit")
    merged_projection_sha256, merged_projection_counts = source_semantic_projection_sha256(source)
    source_audit: dict[str, Any] = {"available": False}
    if source_audit_path:
        expected_urls, expected_companies = _source_audit_expectations(source)
        source_audit = _validated_source_audit(
            source_audit_path,
            merged_sha256=merged_sha256,
            generation=generation,
            market_as_of=market_as_of,
            required_claim_count=(
                sum(isinstance(packet.get("ai_review"), Mapping) for packet in packets if isinstance(packet, Mapping))
                if review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE
                else 0
            ),
            expected_urls=expected_urls,
            expected_companies=expected_companies,
            expected_bindings=_source_audit_bindings(source),
            expected_projection_sha256=merged_projection_sha256,
            expected_projection_counts=merged_projection_counts,
            strict=full_coverage,
        )
    source_verification = _source_verification_metadata(
        source_audit, {str(packet.get("security_code")) for packet in packets if isinstance(packet, Mapping)}
    )
    verified_companies = source_verification[0] if source_verification else {}
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("candidate packet must be an object")
        code = _text(packet.get("security_code"), 16)
        type_key = _text(packet.get("type_key"), 16)
        if not code or not type_key or code in seen_codes:
            raise ValueError(f"duplicate or incomplete company candidate: {code!r}")
        seen_codes.add(code)
        type_keys = _candidate_type_keys(packet, primary_type_key=type_key)
        pair_count = len(type_keys)
        queue_pair_count += pair_count
        review = packet.get("ai_review")
        if review is None:
            if full_coverage:
                raise ValueError(f"full-coverage candidate has no AI review: {code}/{type_key}")
            pair_unreviewed_candidate_count += pair_count
            continue
        if not isinstance(review, Mapping):
            raise ValueError(f"AI review is not an object: {code}/{type_key}")
        # A prior public artifact is also a valid re-publish source.  Its
        # compact reviews intentionally omit the identity envelope; restore
        # that envelope from the packet before applying the same validator.
        review_for_validation = dict(review)
        review_for_validation.setdefault("schema_version", REVIEW_SCHEMA_VERSION)
        review_for_validation.setdefault("security_code", code)
        review_for_validation.setdefault("type_key", type_key)
        if review_mode == "codex_luna_web_review":
            source_passed = verified_companies.get(code, {}).get("status") == "pass"
            search_and_source_passed = source_passed and review_for_validation.get("web_search_event_verified") is True
            review_for_validation["research_source_urls_verified"] = source_passed
            review_for_validation["web_search_claim_urls_verified"] = search_and_source_passed
            review_for_validation["web_search_verified"] = search_and_source_passed
            review_for_validation["web_search_verified_claim_urls"] = (
                sorted({url for claim in review.get("claims", []) for url in claim_source_urls(claim)})[:16]
                if search_and_source_passed
                else []
            )
        if review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE:
            snapshot_errors = valuation_snapshot_errors(
                review_for_validation,
                expected_security_code=code,
                expected_snapshot_generation=generation,
                expected_market_as_of=market_as_of,
            )
            if snapshot_errors:
                raise ValueError(
                    f"native company research valuation snapshot is invalid: {code}:" + ",".join(snapshot_errors)
                )
        public_review = _public_review(
            review_for_validation,
            require_readable_reason=full_coverage,
            claims_are_search_results=review_mode != NATIVE_COMPANY_RESEARCH_REVIEW_MODE,
            require_company_research_fields=review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE,
            company_name=_text(packet.get("name") or packet.get("security_name"), 160),
        )
        if review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE:
            if (
                not public_review.get("research_as_of")
                or not isinstance(public_review.get("economic_profile"), Mapping)
                or not isinstance(public_review.get("valuation"), Mapping)
                or public_review["valuation"].get("as_of") != market_as_of
            ):
                raise ValueError(f"native company research envelope is incomplete: {code}")
            research_dates.add(str(public_review["research_as_of"]))
        if full_coverage and public_review["ai_action"] == "insufficient_evidence":
            raise ValueError(f"full-coverage candidate has no final decision: {code}/{type_key}")
        pair_verdicts[public_review["verdict"]] += pair_count
        if public_review["model"] == PLACEHOLDER_REVIEW_MODEL:
            pair_unreviewed_candidate_count += pair_count
        else:
            pair_attempted_review_count += pair_count
            if public_review["model"]:
                review_models.add(public_review["model"])
            if public_review["effort"]:
                review_efforts.add(public_review["effort"])
            if public_review["model"] and public_review["effort"]:
                review_model_efforts.add((public_review["model"], public_review["effort"]))
            if public_review["web_search_performed"]:
                pair_web_search_attempted_count += pair_count
            if public_review["web_search_event_verified"]:
                pair_web_search_event_verified_count += pair_count
            if public_review["web_search_claim_urls_verified"]:
                pair_web_search_claim_urls_verified_count += pair_count
            if public_review["research_source_urls_verified"]:
                pair_research_source_urls_verified_count += pair_count
            pair_web_search_dropped_claim_url_count += public_review["web_search_dropped_claim_url_count"] * pair_count
            if _public_web_verified(public_review):
                pair_web_search_completed_count += pair_count
            if public_review["verdict"] == "needs_review":
                pair_attempted_needs_review_count += pair_count
        company_record = {
            "security_code": code,
            "name": _text(packet.get("name"), 160),
            "type_key": type_key,
            "type_keys": type_keys,
            "type_pair_count": pair_count,
            "deterministic": _public_deterministic(packet),
            "ai_review": public_review,
        }
        public_candidate_types = _public_candidate_types(packet, type_keys)
        if public_candidate_types:
            company_record["candidate_types"] = public_candidate_types
        company_records.append(company_record)
    if source_pair_candidate_count != queue_pair_count:
        raise ValueError("type-pair candidate count does not match the publication queue")
    if full_coverage and source_pair_candidate_count != source_pair_candidate_total:
        raise ValueError("full-coverage artifact does not contain every candidate type pair")
    if full_coverage and pair_unreviewed_candidate_count:
        raise ValueError("full-coverage artifact contains placeholder reviews")
    if full_coverage and (not review_models or not review_efforts):
        raise ValueError("full-coverage artifact must declare its review models and efforts")
    if full_coverage and review_mode == NATIVE_WEB_REVIEW_MODE:
        if review_models != {NATIVE_WEB_REVIEW_MODEL} or review_efforts != {"xhigh"}:
            raise ValueError("native full-coverage artifact must use Muse Spark 1.2 xhigh reviews")
        for record in company_records:
            review = record["ai_review"]
            if (
                review.get("retrieval_backend") != "reasonix-native-server-web-search"
                or review.get("retrieval_model") != NATIVE_WEB_RETRIEVAL_MODEL
                or review.get("retrieval_effort") != "xhigh"
                or review.get("native_search_completed") is not True
                or review.get("official_fetch_completed") is not True
            ):
                raise ValueError(f"native full-coverage evidence metadata is incomplete: {record['security_code']}")
    if full_coverage and review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE:
        for record in company_records:
            review = record["ai_review"]
            if not native_company_research_profile_matches(review):
                raise ValueError(
                    f"native company-research model/evidence metadata is invalid: {record['security_code']}"
                )
    if review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE and len(research_dates) != 1:
        raise ValueError("native company research must use one shared research_as_of date")
    if (
        full_coverage
        and review_mode == "local_codex_review"
        and not (review_models <= LOCAL_REVIEW_MODELS or review_models <= LOCAL_OPENCODE_MODELS)
    ):
        raise ValueError("local full-coverage artifact must use the local Codex or OpenCode MAX review model")
    if (
        full_coverage
        and review_mode == "local_codex_review"
        and review_models & LOCAL_OPENCODE_MODELS
        and not review_model_efforts
        <= {
            ("opencode-go/ox-alpha-free", "max"),
            ("opencode-go/muse-spark-1.2-contributor", "xhigh"),
            ("opencode-go/muse-spark-1.3-contributor", "xhigh"),
        }
    ):
        raise ValueError("OpenCode Go local artifacts must use Ox max or Muse Spark xhigh reviews")
    if (
        full_coverage
        and review_mode not in PARTIAL_SEARCH_REVIEW_MODES
        and pair_web_search_attempted_count != queue_pair_count
    ):
        raise ValueError("external full-coverage artifact must search every company")
    if (
        full_coverage
        and review_mode not in PARTIAL_SEARCH_REVIEW_MODES
        and pair_web_search_event_verified_count != queue_pair_count
    ):
        raise ValueError("external full-coverage artifact must retain OpenCode search-event proof")
    if (
        full_coverage
        and review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE
        and pair_research_source_urls_verified_count != queue_pair_count
    ):
        raise ValueError("native company research must verify every company's research source URLs")
    if (
        full_coverage
        and review_mode not in {*PARTIAL_SEARCH_REVIEW_MODES, NATIVE_COMPANY_RESEARCH_REVIEW_MODE}
        and pair_web_search_claim_urls_verified_count != queue_pair_count
    ):
        raise ValueError("legacy external review must retain search-claim URL proof")
    public_packets = company_records
    published_pair_total = sum(int(packet["type_pair_count"]) for packet in public_packets)
    if published_pair_total > queue_pair_count or (full_coverage and published_pair_total != queue_pair_count):
        raise ValueError("public company/type-pair coverage is inconsistent")
    if len({packet["security_code"] for packet in public_packets}) != len(public_packets):
        raise ValueError("public AI screening contains duplicate companies")
    verdicts: Counter[str] = Counter()
    attempted_review_count = 0
    unreviewed_candidate_count = 0
    attempted_needs_review_count = 0
    web_search_attempted_count = 0
    web_search_completed_count = 0
    web_search_event_verified_count = 0
    web_search_dropped_claim_url_count = 0
    web_search_claim_urls_verified_count = 0
    research_source_urls_verified_count = 0
    action_counts: Counter[str] = Counter()
    final_category_counts: Counter[str] = Counter()
    freshness_counts: Counter[str] = Counter()
    for packet in public_packets:
        review = packet["ai_review"]
        verdicts[review["verdict"]] += 1
        action_counts[review["ai_action"]] += 1
        final_category_counts[review["final_category"]] += 1
        freshness_counts[str(review.get("freshness_status") or "undated")] += 1
        if review["model"] == PLACEHOLDER_REVIEW_MODEL:
            unreviewed_candidate_count += 1
        else:
            attempted_review_count += 1
            if review["web_search_performed"]:
                web_search_attempted_count += 1
            if review["web_search_event_verified"]:
                web_search_event_verified_count += 1
            web_search_dropped_claim_url_count += review["web_search_dropped_claim_url_count"]
            if review["web_search_claim_urls_verified"]:
                web_search_claim_urls_verified_count += 1
            if review["research_source_urls_verified"]:
                research_source_urls_verified_count += 1
            if _public_web_verified(review):
                web_search_completed_count += 1
            if review["verdict"] == "needs_review":
                attempted_needs_review_count += 1
    public_packets.sort(
        key=lambda value: (
            -float(value["ai_review"]["buy_attractiveness_score"]),
            _ACTION_PRIORITY.get(value["ai_review"]["ai_action"], 9),
            value["security_code"],
            value["type_key"],
        )
    )
    for rank, packet in enumerate(public_packets, 1):
        packet["ai_rank"] = rank
    public_projection_sha256, public_projection_counts = source_semantic_projection_sha256(
        {"review_mode": review_mode, "packets": public_packets}
    )
    if public_projection_sha256 != merged_projection_sha256 or public_projection_counts != merged_projection_counts:
        raise ValueError("public source semantic projection does not match the merged AI screening file")
    if source_verification is not None:
        coverage_by_code, affected_company_count = source_verification
        status_counts = Counter(
            coverage_by_code.get(str(packet["security_code"]), {"status": "unverified"})["status"]
            for packet in public_packets
        )
        if review_mode == "codex_luna_web_review" and (
            affected_company_count != 0
            or status_counts["pass"] != len(public_packets)
            or status_counts["failed"] != 0
            or status_counts["unverified"] != 0
        ):
            raise ValueError("Codex/Luna publication requires every company source verification to pass")
        for packet in public_packets:
            review = packet["ai_review"]
            verification = coverage_by_code.get(
                str(packet["security_code"]),
                {"status": "unverified", "issue_count": 1},
            )
            review["source_verification_status"] = verification["status"]
            review["source_verification_issue_count"] = verification["issue_count"]
            review["source_verification_issues"] = list(verification.get("issues", []))
            review["source_verification_issue_kinds"] = dict(verification.get("issue_kinds", {}))
            review["summary"] = _source_verification_summary(review.get("summary"), verification["status"])
            if verification["status"] != "pass" and review.get("confidence") == "high":
                review["confidence"] = "medium"
        source_audit["affected_company_count"] = affected_company_count
        source_audit["company_pass_count"] = status_counts["pass"]
        source_audit["company_failed_count"] = status_counts["failed"]
        source_audit["company_unverified_count"] = status_counts["unverified"]
    source_audit["web_search_completed"] = web_search_completed_count
    source_audit["web_search_attempted"] = web_search_attempted_count
    source_audit["web_search_event_verified"] = web_search_event_verified_count
    source_audit["web_search_claim_urls_verified"] = web_search_claim_urls_verified_count
    source_audit["research_source_urls_verified"] = research_source_urls_verified_count
    source_audit["web_source_verified"] = web_search_completed_count
    source_audit["reviewed_without_web_search"] = max(0, attempted_review_count - web_search_attempted_count)
    source_audit["type_pair_web_search_completed"] = pair_web_search_completed_count
    source_audit["type_pair_web_search_attempted"] = pair_web_search_attempted_count
    source_audit["type_pair_web_search_event_verified"] = pair_web_search_event_verified_count
    source_audit["type_pair_web_search_claim_urls_verified"] = pair_web_search_claim_urls_verified_count
    source_audit["type_pair_research_source_urls_verified"] = pair_research_source_urls_verified_count
    source_audit["type_pair_web_search_dropped_claim_urls"] = pair_web_search_dropped_claim_url_count
    rule_file_count = source.get("rule_file_count")
    rule_source_sha256 = source.get("rule_source_sha256")
    knowledge_base_file_count = source.get("knowledge_base_file_count")
    knowledge_base_source_sha256 = source.get("knowledge_base_source_sha256")
    rules_root = _text(source.get("rules_root"), 240)
    if rule_file_count is not None and (not isinstance(rule_file_count, int) or rule_file_count < 1):
        raise ValueError("AI screening knowledge-base file count is invalid")
    if rule_source_sha256 is not None:
        if (
            not isinstance(rule_file_count, int)
            or not isinstance(rule_source_sha256, Mapping)
            or len(rule_source_sha256) != rule_file_count
        ):
            raise ValueError("AI screening knowledge-base manifest is incomplete")
    if knowledge_base_file_count is not None or knowledge_base_source_sha256 is not None:
        if (
            not isinstance(knowledge_base_file_count, int)
            or knowledge_base_file_count < 1
            or not isinstance(knowledge_base_source_sha256, Mapping)
            or len(knowledge_base_source_sha256) != knowledge_base_file_count
        ):
            raise ValueError("AI screening full knowledge-base manifest is incomplete")
    excerpt_manifest = (
        dict(sorted((str(key), str(value)) for key, value in rule_source_sha256.items()))
        if isinstance(rule_source_sha256, Mapping)
        else None
    )
    library_manifest = (
        dict(sorted((str(key), str(value)) for key, value in knowledge_base_source_sha256.items()))
        if isinstance(knowledge_base_source_sha256, Mapping)
        else excerpt_manifest
    )
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "ai_is_advisory": True,
        "auto_buy_promotion": False,
        "full_coverage_final_recommendation": full_coverage,
        "review_mode": review_mode,
        "review_models": sorted(review_models),
        "review_efforts": sorted(review_efforts),
        "full_coverage_web_search": bool(
            full_coverage
            and unreviewed_candidate_count == 0
            and attempted_review_count == len(public_packets)
            and web_search_attempted_count == len(public_packets)
            and web_search_event_verified_count == len(public_packets)
        ),
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        **(
            {"research_as_of": next(iter(research_dates))}
            if research_dates
            else {"research_as_of": _text(source.get("research_as_of"), 10)}
            if _text(source.get("research_as_of"), 10)
            else {}
        ),
        "methodology_version": source.get("methodology_version"),
        "index_contract": source.get("index_contract"),
        # The AI emits one independent company opinion.  Candidate type pairs
        # remain an auditable admission trail, never competing opinions from
        # which publication may select the most bullish result.
        "candidate_total": len(public_packets),
        "candidate_identity_sha256": declared_identity_digest,
        "candidate_universe_identity_sha256": universe_identity_digest,
        "company_deduplication": "company_level_review",
        "review_granularity": "company",
        "candidate_source": "deterministic_rule_pool",
        "candidate_scope_policy": (
            "deterministic_triggered_or_near_threshold_only; rule state selects scope, never the AI verdict"
        ),
        "ai_decision_policy": (
            "independent_company_research_three_way; AI may upgrade a near-threshold candidate or downgrade a rule-triggered candidate"
        ),
        "knowledge_base_provenance": {
            "root": rules_root or None,
            "file_count": knowledge_base_file_count or rule_file_count,
            "source_sha256": library_manifest,
            "injected_excerpt_file_count": rule_file_count if isinstance(rule_file_count, int) else None,
            "injected_excerpt_source_sha256": excerpt_manifest,
            "role": "reference-library inventory and selected research excerpts; never a substitute for company facts",
            "candidate_context_excerpts": bool(
                any(isinstance(packet.get("rule_context"), list) for packet in packets if isinstance(packet, Mapping))
            ),
        },
        "type_pair_candidate_total": published_pair_total,
        "type_pair_candidate_identity_sha256": source_pair_identity_digest,
        "type_pair_expected_total": source_pair_candidate_total,
        "type_pair_universe_total": source_pair_candidate_total,
        "type_pair_universe_identity_sha256": source_pair_identity_digest,
        "type_pair_unique_company_count": len(public_packets),
        "type_pair_reviewed_count": pair_attempted_review_count,
        "type_pair_unreviewed_count": pair_unreviewed_candidate_count,
        "type_pair_needs_review_count": pair_attempted_needs_review_count,
        "type_pair_verdict_counts": dict(sorted(pair_verdicts.items())),
        "type_pair_web_search_attempted_count": pair_web_search_attempted_count,
        "type_pair_web_search_completed_count": pair_web_search_completed_count,
        "type_pair_web_search_event_verified_count": pair_web_search_event_verified_count,
        "type_pair_web_search_claim_urls_verified_count": pair_web_search_claim_urls_verified_count,
        "type_pair_research_source_urls_verified_count": pair_research_source_urls_verified_count,
        "type_pair_web_search_dropped_claim_url_count": pair_web_search_dropped_claim_url_count,
        "candidate_offset": candidate_offset,
        "reviewed_count": len(public_packets),
        "attempted_review_count": attempted_review_count,
        "unreviewed_candidate_count": unreviewed_candidate_count,
        "attempted_needs_review_count": attempted_needs_review_count,
        "web_search_attempted_count": web_search_attempted_count,
        "web_search_event_verified_count": web_search_event_verified_count,
        "web_search_dropped_claim_url_count": web_search_dropped_claim_url_count,
        "web_search_claim_urls_verified_count": web_search_claim_urls_verified_count,
        "research_source_urls_verified_count": research_source_urls_verified_count,
        "web_source_verified_count": web_search_completed_count,
        "web_search_completed_count": web_search_completed_count,
        "reviewed_without_web_search": max(0, attempted_review_count - web_search_attempted_count),
        # Keep the old field names as compatibility aliases.  They now mean
        # attempted versus not-yet-started, rather than verdict quality.
        "completed_review_count": attempted_review_count,
        "pending_review_count": unreviewed_candidate_count,
        "verdict_counts": dict(sorted(verdicts.items())),
        "ai_action_counts": {key: action_counts[key] for key in _PUBLIC_ACTIONS},
        "final_category_counts": {key: final_category_counts[key] for key in _PUBLIC_CATEGORIES},
        "priority_buy_count": action_counts["priority_buy"],
        "recommend_buy_count": action_counts["priority_buy"],
        "watchlist_count": action_counts["watchlist"],
        "avoid_count": action_counts["avoid"],
        "do_not_recommend_buy_count": action_counts["watchlist"]
        + action_counts["avoid"]
        + action_counts["insufficient_evidence"],
        "insufficient_evidence_count": action_counts["insufficient_evidence"],
        "ranking_version": RANKING_VERSION,
        "freshness_counts": dict(sorted(freshness_counts.items())),
        "ranking_source": _text(source.get("ranking_source"), 120),
        # Preserve the release-boundary sanitization ledger so the website
        # artifact records which search snippets/claims were discarded before
        # scoring.  This is audit metadata only; it never changes a verdict.
        **(
            {"publication_sanitization": {str(key): value for key, value in source["publication_sanitization"].items()}}
            if isinstance(source.get("publication_sanitization"), Mapping)
            else {}
        ),
        "source_audit": source_audit,
        "packets": public_packets,
    }
    if isinstance(rule_file_count, int) and isinstance(rule_source_sha256, Mapping):
        artifact["rules_root"] = rules_root
        artifact["rule_file_count"] = rule_file_count
        artifact["rule_source_sha256"] = dict(
            sorted((str(key), str(value)) for key, value in rule_source_sha256.items())
        )
    if previous_ai_path is not None:
        artifact["day_over_day"] = build_day_over_day(artifact, _load(previous_ai_path))
    output_bytes = _public_artifact_bytes(artifact)
    atomic_write_bytes(output_path, output_bytes)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--expected-market-as-of", required=True)
    parser.add_argument("--source-audit", type=Path)
    parser.add_argument("--previous-ai", type=Path)
    args = parser.parse_args()
    artifact = build_artifact(
        args.merged,
        args.output,
        expected_generation=args.expected_generation,
        expected_market_as_of=args.expected_market_as_of,
        source_audit_path=args.source_audit,
        previous_ai_path=args.previous_ai,
    )
    print(json.dumps({"artifact_kind": artifact["artifact_kind"], "reviewed_count": artifact["reviewed_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
