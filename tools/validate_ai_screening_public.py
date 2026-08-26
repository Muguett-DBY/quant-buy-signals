"""Validate a checked-in public AI-screening overlay before R2 upload.

The builder and publisher perform the full source-side audit.  This small
validator is the last, file-local gate in CI: it proves the seed still has a
complete generation-bound company ranking, the same semantic review matrix,
and the required per-company search proof after it has been copied into git.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import (
    CODEX_LUNA_WEB_REVIEW_MODE,
    LOCAL_OPENCODE_MODELS,
    LOCAL_REVIEW_MODELS,
    LOCAL_REVIEW_MODEL,
    NATIVE_COMPANY_RESEARCH_PROFILES,
    NATIVE_COMPANY_RESEARCH_REVIEW_MODE,
    NATIVE_WEB_REVIEW_MODE,
    NATIVE_WEB_REVIEW_MODEL,
    NATIVE_WEB_RETRIEVAL_MODEL,
    PARTIAL_SEARCH_REVIEW_MODES,
    candidate_identity_sha256,
    decision_text_conflicts,
    native_company_research_profile_matches,
    validate_review,
    valuation_snapshot_errors,
)
from tools.audit_ai_screening_sources import source_semantic_projection_sha256
from tools.ai_quantitative_facts import has_numeric_fact
from tools.ai_source_urls import (
    claim_source_urls,
    finding_source_url,
    iter_review_url_bindings,
    review_canonical_urls,
)

_CODE_RE = re.compile(r"^[036]\d{5}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TYPE_RE = re.compile(r"^type[1-7]$")
_ACTIONS = ("priority_buy", "watchlist", "avoid", "insufficient_evidence")
_CATEGORIES = ("recommend_buy", "observe", "do_not_recommend")
_VERDICTS = ("confirmed", "caution", "misclassified", "missed_candidate", "needs_review")
_ACTION_PRIORITY = {action: index for index, action in enumerate(_ACTIONS)}
MAX_PUBLIC_ARTIFACT_BYTES = 32 * 1024 * 1024


def _int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is not a non-negative integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} exceeds {maximum}")
    return value


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


def _native_source_packet_contract(
    packets: list[Any],
) -> tuple[set[str], Counter[tuple[Any, ...]], dict[str, dict[str, Any]]]:
    """Rebuild URL and finding bindings from the artifact being validated."""

    urls: set[str] = set()
    bindings: Counter[tuple[Any, ...]] = Counter()
    coverage: dict[str, dict[str, Any]] = {}
    for packet in packets:
        if not isinstance(packet, Mapping):
            continue
        code = str(packet.get("security_code") or "")
        name = str(packet.get("name") or packet.get("security_name") or "")
        type_key = str(packet.get("type_key") or "")
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            continue
        review_urls = review_canonical_urls(review)
        urls.update(review_urls)
        bindings.update(
            _source_binding_key(item)
            for item in iter_review_url_bindings(review, security_code=code, name=name, type_key=type_key)
        )
        findings = review.get("search_findings") if isinstance(review.get("search_findings"), list) else []
        findings_by_id = {
            str(finding.get("id") or "").strip(): finding
            for finding in findings
            if isinstance(finding, Mapping) and str(finding.get("id") or "").strip()
        }
        referenced: set[str] = set()
        searched_no_source: set[str] = set()
        referenced_no_source: set[str] = set()
        claim_ids_with_urls: set[str] = set()
        claims = review.get("claims") if isinstance(review.get("claims"), list) else []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            finding_id = str(claim.get("search_finding_id") or "").strip()
            claim_urls = claim_source_urls(claim)
            if not finding_id:
                continue
            referenced.add(finding_id)
            if claim_urls:
                claim_ids_with_urls.add(finding_id)
            elif not finding_source_url(findings_by_id.get(finding_id, {})):
                searched_no_source.add(finding_id)
                referenced_no_source.add(finding_id)
        for finding_id, finding in findings_by_id.items():
            if finding_source_url(finding):
                referenced.add(finding_id)
            elif finding_id not in claim_ids_with_urls:
                searched_no_source.add(finding_id)
                if finding_id in referenced:
                    referenced_no_source.add(finding_id)
        coverage[code] = {
            "referenced_finding_ids": referenced,
            "searched_no_source_finding_ids": searched_no_source,
            "referenced_no_source_finding_ids": referenced_no_source,
            "canonical_url_count": len(review_urls),
        }
    return urls, bindings, coverage


def _validate_native_company_source_audit(source_audit: Mapping[str, Any], packets: list[Any]) -> None:
    """Require the v3 audit matrix used by native-company publication."""

    if source_audit.get("audit_contract_version") != 3:
        raise ValueError("native company research requires source audit contract v3")
    if source_audit.get("audit_passed") is not True:
        raise ValueError("native company research source audit did not pass")
    projection_sha256, projection_counts = source_semantic_projection_sha256(
        {
            "review_mode": NATIVE_COMPANY_RESEARCH_REVIEW_MODE,
            "packets": packets,
        }
    )
    if str(source_audit.get("projection_sha256") or "") != projection_sha256:
        raise ValueError("native company research source audit semantic projection is stale")
    for field, expected in projection_counts.items():
        if _int(source_audit.get(field), f"source_audit.{field}") != expected:
            raise ValueError(f"native company research source audit {field} is stale")
    if _int(source_audit.get("claim_count"), "source_audit.claim_count") != projection_counts["projection_claim_count"]:
        raise ValueError("native company research source audit projection claim count is inconsistent")
    semantic_counts = {
        field: _int(source_audit.get(field), f"source_audit.{field}")
        for field in (
            "semantic_claim_count",
            "semantic_passed_count",
            "semantic_failed_count",
            "semantic_unverified_count",
        )
    }
    if semantic_counts["semantic_claim_count"] != (
        semantic_counts["semantic_passed_count"]
        + semantic_counts["semantic_failed_count"]
        + semantic_counts["semantic_unverified_count"]
    ):
        raise ValueError("native company research source audit semantic totals are inconsistent")
    if semantic_counts["semantic_failed_count"] or semantic_counts["semantic_unverified_count"]:
        raise ValueError("native company research source audit has non-passing semantic findings")
    canonical_urls = source_audit.get("canonical_urls")
    if (
        not isinstance(canonical_urls, list)
        or any(
            not isinstance(value, str) or not re.match(r"^https?://", value, re.IGNORECASE) or value != value.strip()
            for value in canonical_urls
        )
        or len(canonical_urls) != len(set(canonical_urls))
    ):
        raise ValueError("native company research source audit canonical URL set is invalid")
    if len(canonical_urls) != projection_counts["projection_unique_url_count"]:
        raise ValueError("native company research source audit projection URL count is inconsistent")
    actual_urls, actual_bindings, actual_coverage = _native_source_packet_contract(packets)
    if set(canonical_urls) != actual_urls:
        raise ValueError("native company research source audit canonical URLs do not match current packets")
    audit_bindings = source_audit.get("source_bindings")
    if not isinstance(audit_bindings, list) or any(not isinstance(item, Mapping) for item in audit_bindings):
        raise ValueError("native company research source audit source bindings are missing")
    if Counter(_source_binding_key(item) for item in audit_bindings) != actual_bindings:
        raise ValueError("native company research source audit bindings do not match current packets")
    coverage = source_audit.get("company_coverage")
    if not isinstance(coverage, list):
        raise ValueError("native company research source audit company coverage is missing")
    packet_codes = {str(packet.get("security_code") or "") for packet in packets if isinstance(packet, Mapping)}
    coverage_by_code: dict[str, Mapping[str, Any]] = {}
    for item in coverage:
        if not isinstance(item, Mapping):
            raise ValueError("native company research source audit company coverage is invalid")
        code = str(item.get("security_code") or "")
        if not code or code in coverage_by_code:
            raise ValueError("native company research source audit company coverage has duplicate codes")
        coverage_by_code[code] = item
    if set(coverage_by_code) != packet_codes:
        raise ValueError("native company research source audit company coverage is incomplete")
    coverage_semantic_total = 0
    for code, item in coverage_by_code.items():
        expected = actual_coverage.get(code)
        if expected is None:
            raise ValueError(f"native company research source audit packet coverage is missing: {code}")
        for field in (
            "referenced_finding_ids",
            "searched_no_source_finding_ids",
            "referenced_no_source_finding_ids",
        ):
            if not isinstance(item.get(field), list) or any(not isinstance(value, str) for value in item[field]):
                raise ValueError(f"native company research source audit {field} is invalid: {code}")
        if item["referenced_no_source_finding_ids"]:
            raise ValueError(f"native company research cited finding has no URL: {code}")
        for field in (
            "referenced_finding_ids",
            "searched_no_source_finding_ids",
            "referenced_no_source_finding_ids",
        ):
            if set(item[field]) != expected[field]:
                raise ValueError(f"native company research source audit finding bindings are stale: {code}")
        if item.get("canonical_url_count") != expected["canonical_url_count"]:
            raise ValueError(f"native company research source audit URL coverage is stale: {code}")
        company_counts = {
            field: _int(item.get(field), f"source_audit.company_coverage.{field}")
            for field in (
                "semantic_claim_count",
                "semantic_passed_count",
                "semantic_failed_count",
                "semantic_unverified_count",
            )
        }
        if (
            company_counts["semantic_claim_count"]
            != (
                company_counts["semantic_passed_count"]
                + company_counts["semantic_failed_count"]
                + company_counts["semantic_unverified_count"]
            )
            or company_counts["semantic_failed_count"]
            or company_counts["semantic_unverified_count"]
        ):
            raise ValueError(f"native company research source audit company semantic results failed: {code}")
        coverage_semantic_total += company_counts["semantic_claim_count"]
        status = str(item.get("status") or "")
        if status not in {"pass", "searched_no_source"}:
            raise ValueError(f"native company research source audit company status is not publishable: {code}")
        if item["referenced_finding_ids"] and item.get("all_referenced_findings_semantic_pass") is not True:
            raise ValueError(f"native company research source audit company findings did not all pass: {code}")
    if coverage_semantic_total != semantic_counts["semantic_claim_count"]:
        raise ValueError("native company research source audit company semantic totals are inconsistent")


def _review_for_validation(review: Mapping[str, Any], packet: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(review)
    value.setdefault("schema_version", 2)
    value.setdefault("security_code", packet.get("security_code"))
    value.setdefault("type_key", packet.get("type_key"))
    return value


def _publication_sort_key(packet: Mapping[str, Any]) -> tuple[float, int, str, str]:
    review = packet.get("ai_review")
    assert isinstance(review, Mapping)
    return (
        -float(review["buy_attractiveness_score"]),
        _ACTION_PRIORITY.get(str(review.get("ai_action") or ""), len(_ACTION_PRIORITY)),
        str(packet.get("security_code") or ""),
        str(packet.get("type_key") or ""),
    )


def _validate_decision_texts(review: Mapping[str, Any], *, code: str) -> None:
    action = str(review.get("ai_action") or "")
    fields: list[tuple[str, Any]] = [
        ("recommendation_label", review.get("recommendation_label")),
        ("summary", review.get("summary")),
    ]
    for field in ("key_strengths", "risk_flags"):
        values = review.get(field)
        if isinstance(values, list):
            fields.extend((f"{field}[{index}]", value) for index, value in enumerate(values))
    for field, value in fields:
        if isinstance(value, str) and decision_text_conflicts(action, value):
            raise ValueError(f"AI screening {field} conflicts with its conclusion for {code}")


def validate_artifact(
    payload: Mapping[str, Any], *, expected_generation: str, expected_market_as_of: str
) -> dict[str, Any]:
    if payload.get("schema_version") != 2 or payload.get("review_schema_version") != 2:
        raise ValueError("AI screening schema version is invalid")
    if payload.get("artifact_kind") != "ai_screening_overlay":
        raise ValueError("AI screening artifact kind is invalid")
    if payload.get("ai_is_advisory") is not True or payload.get("auto_buy_promotion") is not False:
        raise ValueError("AI screening advisory flags are invalid")
    if str(payload.get("snapshot_generation") or "") != expected_generation:
        raise ValueError("AI screening generation does not match the market snapshot")
    if str(payload.get("market_as_of") or "") != expected_market_as_of:
        raise ValueError("AI screening market date does not match the market snapshot")
    packets = payload.get("packets")
    if not isinstance(packets, list) or not packets:
        raise ValueError("AI screening packets are missing")
    if any(not isinstance(packet, Mapping) for packet in packets):
        raise ValueError("AI screening packet is not an object")
    candidate_total = _int(payload.get("candidate_total"), "candidate_total", maximum=2000)
    if candidate_total != len(packets) or _int(payload.get("reviewed_count"), "reviewed_count") != candidate_total:
        raise ValueError("AI screening company coverage is incomplete")
    if payload.get("candidate_offset") != 0 or payload.get("full_coverage_final_recommendation") is not True:
        raise ValueError("AI screening seed is not a full-coverage artifact")
    computed_identity = candidate_identity_sha256(packets)
    for field in (
        "candidate_identity_sha256",
        "candidate_universe_identity_sha256",
        "type_pair_candidate_identity_sha256",
        "type_pair_universe_identity_sha256",
    ):
        declared_identity = str(payload.get(field) or "")
        if not _HASH_RE.fullmatch(declared_identity) or declared_identity != computed_identity:
            raise ValueError(f"AI screening {field} does not match the public candidate universe")
    pair_total = _int(payload.get("type_pair_candidate_total"), "type_pair_candidate_total")
    if _int(payload.get("type_pair_unique_company_count"), "type_pair_unique_company_count") != candidate_total:
        raise ValueError("AI screening type-pair unique-company total is inconsistent")
    if pair_total != _int(payload.get("type_pair_expected_total"), "type_pair_expected_total"):
        raise ValueError("AI screening type-pair total does not match the candidate universe")
    if _int(payload.get("type_pair_reviewed_count"), "type_pair_reviewed_count") != pair_total:
        raise ValueError("AI screening type-pair coverage is incomplete")
    if _int(payload.get("type_pair_unreviewed_count"), "type_pair_unreviewed_count") != 0:
        raise ValueError("AI screening contains unreviewed type pairs")
    models = payload.get("review_models")
    efforts = payload.get("review_efforts")
    if (
        not isinstance(models, list)
        or not models
        or len(models) > 16
        or any(not isinstance(value, str) or not value.strip() or len(value) > 120 for value in models)
        or len(models) != len(set(models))
        or not isinstance(efforts, list)
        or not efforts
        or len(efforts) > 16
        or any(not isinstance(value, str) or not value.strip() or len(value) > 32 for value in efforts)
        or len(efforts) != len(set(efforts))
    ):
        raise ValueError("AI screening model/effort metadata is missing")
    review_mode = str(payload.get("review_mode") or "")
    if review_mode == "local_codex_review" and not (
        set(models) <= LOCAL_REVIEW_MODELS or set(models) <= LOCAL_OPENCODE_MODELS
    ):
        raise ValueError("local AI screening seed uses an unexpected model")
    native_review = review_mode in {NATIVE_WEB_REVIEW_MODE, NATIVE_COMPANY_RESEARCH_REVIEW_MODE}
    company_research_review = review_mode == NATIVE_COMPANY_RESEARCH_REVIEW_MODE
    research_as_of = ""
    if company_research_review:
        research_as_of = str(payload.get("research_as_of") or "")
        try:
            research_date = date.fromisoformat(research_as_of)
            market_date = date.fromisoformat(expected_market_as_of)
        except ValueError as error:
            raise ValueError("native company research date is invalid") from error
        if research_date < market_date:
            raise ValueError("native company research date precedes the market snapshot")
    if review_mode == NATIVE_WEB_REVIEW_MODE and (
        set(models) != {NATIVE_WEB_REVIEW_MODEL} or set(efforts) != {"xhigh"}
    ):
        raise ValueError("native AI screening seed must use Muse Spark 1.2 xhigh")
    if company_research_review:
        allowed_model_efforts = {(profile[0], profile[1]) for profile in NATIVE_COMPANY_RESEARCH_PROFILES}
        if not set(models) <= {model for model, _ in allowed_model_efforts} or not set(efforts) <= {
            effort for _, effort in allowed_model_efforts
        }:
            raise ValueError("native company research declares an unsupported model or effort")
    external_full = review_mode not in PARTIAL_SEARCH_REVIEW_MODES
    mixed_full = review_mode == "opencode_mixed_review"
    if external_full:
        source_audit = payload.get("source_audit")
        if not isinstance(source_audit, Mapping) or source_audit.get("available") is not True:
            raise ValueError("external full AI screening seed has no source audit")
        if _int(source_audit.get("invalid_claim_url_count"), "source_audit.invalid_claim_url_count") != 0:
            raise ValueError("external full AI screening seed has invalid claim URLs")
        failed = _int(source_audit.get("failed", 0), "source_audit.failed")
        network_warnings_allowed = review_mode == CODEX_LUNA_WEB_REVIEW_MODE and source_audit.get(
            "network_warnings_allowed"
        ) is True
        if failed != 0 and not network_warnings_allowed:
            raise ValueError("external full AI screening seed has unreachable claim URLs")
        for field in ("checked", "ok", "blocked"):
            if field in source_audit:
                _int(source_audit.get(field), f"source_audit.{field}")
        if "checked" in source_audit and _int(source_audit.get("checked"), "source_audit.checked") != (
            _int(source_audit.get("ok", 0), "source_audit.ok")
            + failed
            + _int(source_audit.get("blocked", 0), "source_audit.blocked")
            + _int(source_audit.get("invalid", 0), "source_audit.invalid")
        ):
            raise ValueError("external full AI screening source-audit totals are inconsistent")
        if (
            company_research_review
            and _int(source_audit.get("claim_count"), "source_audit.claim_count") < candidate_total
        ):
            raise ValueError("native company research source audit is incomplete")
        if company_research_review and source_audit.get("audit_passed") is not True:
            raise ValueError("native company research source audit did not pass")
        if company_research_review and (
            not _HASH_RE.fullmatch(str(source_audit.get("merged_sha256") or ""))
            or not _HASH_RE.fullmatch(str(source_audit.get("audit_sha256") or ""))
            or not all(field in source_audit for field in ("checked", "ok", "failed", "blocked"))
        ):
            raise ValueError("native company research source audit metadata is incomplete")
        if company_research_review:
            _validate_native_company_source_audit(source_audit, packets)
    elif mixed_full:
        source_audit = payload.get("source_audit")
        if not isinstance(source_audit, Mapping) or source_audit.get("available") is not True:
            raise ValueError("mixed full AI screening seed has no source audit")
        if _int(source_audit.get("invalid_claim_url_count"), "source_audit.invalid_claim_url_count") != 0:
            raise ValueError("mixed full AI screening seed has invalid claim URLs")
        if _int(source_audit.get("failed", 0), "source_audit.failed") != 0:
            raise ValueError("mixed full AI screening seed has unreachable claim URLs")
    seen_codes: set[str] = set()
    seen_ranks: set[int] = set()
    action_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    verdict_counts: Counter[str] = Counter()
    pair_verdict_counts: Counter[str] = Counter()
    attempted = searched = events = claims = research_sources = dropped = 0
    completed_searches = needs_review_count = 0
    pair_searched = pair_completed_searches = pair_events = pair_claims = pair_dropped = 0
    research_source_pairs = pair_needs_review_count = 0
    review_model_efforts: set[tuple[str, str]] = set()
    pair_sum = 0
    for position, packet in enumerate(packets, 1):
        code = str(packet.get("security_code") or "")
        type_key = str(packet.get("type_key") or "")
        type_keys = packet.get("type_keys")
        pair_count = _int(packet.get("type_pair_count"), f"{code}.type_pair_count")
        rank = _int(packet.get("ai_rank"), f"{code}.ai_rank")
        if not _CODE_RE.fullmatch(code) or not _TYPE_RE.fullmatch(type_key):
            raise ValueError(f"AI screening packet identity is invalid: {code}/{type_key}")
        if code in seen_codes or rank in seen_ranks or rank != position:
            raise ValueError(f"AI screening has duplicate company or rank: {code}/{rank}")
        if (
            not isinstance(type_keys, list)
            or len(type_keys) != pair_count
            or not type_keys
            or len(type_keys) != len(set(type_keys))
            or any(not isinstance(value, str) or not _TYPE_RE.fullmatch(value) for value in type_keys)
            or type_key not in type_keys
        ):
            raise ValueError(f"AI screening type-pair identity is invalid: {code}")
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            raise ValueError(f"AI screening review is missing: {code}")
        review_value = _review_for_validation(review, packet)
        errors = validate_review(
            review_value,
            require_readable_reason=True,
            require_company_research_fields=company_research_review,
        )
        if errors:
            raise ValueError(f"AI screening review is semantically invalid for {code}: {','.join(errors)}")
        if company_research_review:
            snapshot_errors = valuation_snapshot_errors(
                review_value,
                expected_security_code=code,
                expected_snapshot_generation=expected_generation,
                expected_market_as_of=expected_market_as_of,
            )
            if snapshot_errors:
                raise ValueError(f"AI screening valuation snapshot is invalid for {code}: {','.join(snapshot_errors)}")
        _validate_decision_texts(review, code=code)
        if company_research_review and str(review.get("research_as_of") or "") != research_as_of:
            raise ValueError(f"AI screening research date is inconsistent for {code}")
        if company_research_review:
            profile = review.get("economic_profile")
            assert isinstance(profile, Mapping)
            source_ids = profile.get("business_model_source_ids")
            business_sources = profile.get("business_model_sources")
            source_quality = str(profile.get("business_model_source_quality") or "")
            source_status = str(profile.get("business_model_source_status") or "")
            if source_status != ("searched_no_source" if source_quality == "not_found" else "source_found"):
                raise ValueError(f"AI screening business-model source status is invalid for {code}")
            if source_quality == "not_found" and str(review.get("ai_action") or "") == "priority_buy":
                raise ValueError(f"AI screening not_found company cannot be priority_buy: {code}")
            ids_empty_not_found = source_quality == "not_found" and source_ids in (None, [])
            if (
                not isinstance(source_ids, list)
                or not ids_empty_not_found
                and (
                    not source_ids
                    or len(source_ids) > 16
                    or any(not isinstance(value, str) or not value.strip() for value in source_ids)
                    or len(source_ids) != len(set(source_ids))
                )
                or not isinstance(business_sources, list)
                or len(business_sources) != len(source_ids or [])
            ):
                raise ValueError(f"AI screening business-model sources are invalid for {code}")
            claim_sources = {
                str(claim.get("search_finding_id") or ""): str(claim.get("source_ref") or "")
                for claim in review.get("claims", [])
                if isinstance(claim, Mapping) and str(claim.get("search_finding_id") or "")
            }
            if len(claim_sources) != sum(
                isinstance(claim, Mapping) and bool(str(claim.get("search_finding_id") or ""))
                for claim in review.get("claims", [])
            ):
                raise ValueError(f"AI screening has duplicate search-finding IDs for {code}")
            public_source_ids: list[str] = []
            for source in business_sources:
                if not isinstance(source, Mapping):
                    raise ValueError(f"AI screening business-model source is invalid for {code}")
                source_id = str(source.get("id") or "")
                source_ref = str(source.get("source_ref") or "")
                if (
                    not source_id
                    or source_id not in source_ids
                    or (
                        source_quality != "not_found"
                        and (source_id not in claim_sources or source_ref != claim_sources[source_id])
                    )
                    or (source_quality == "not_found" and source_ref)
                ):
                    raise ValueError(f"AI screening business-model source is unbound for {code}")
                public_source_ids.append(source_id)
            if set(public_source_ids) != set(source_ids) or len(public_source_ids) != len(set(public_source_ids)):
                raise ValueError(f"AI screening business-model source coverage is invalid for {code}")
            if source_quality != "not_found" and not any(
                str(source.get("source_ref") or "").lower().startswith("https://")
                for source in business_sources
                if isinstance(source, Mapping)
            ):
                raise ValueError(f"AI screening business-model source has no HTTPS proof for {code}")
        quantitative = review.get("quantitative_facts")
        if native_review:
            if not isinstance(quantitative, list) or not quantitative:
                raise ValueError(f"native AI screening review has no quantitative facts: {code}")
            if any(not has_numeric_fact(item) for item in quantitative):
                raise ValueError(f"native AI screening review contains a non-quantitative fact: {code}")
            if str(review.get("ai_action") or "") == "priority_buy":
                if sum(has_numeric_fact(item) for item in quantitative) < 2:
                    raise ValueError(f"native AI priority-buy review lacks two numeric facts: {code}")
        if str(review.get("model") or "") not in models or str(review.get("effort") or "") not in efforts:
            raise ValueError(f"AI screening review metadata is inconsistent for {code}")
        if review_mode == NATIVE_WEB_REVIEW_MODE and (
            review.get("retrieval_backend") != "reasonix-native-server-web-search"
            or review.get("retrieval_model") != NATIVE_WEB_RETRIEVAL_MODEL
            or review.get("retrieval_effort") != "xhigh"
            or review.get("native_search_completed") is not True
            or review.get("official_fetch_completed") is not True
        ):
            raise ValueError(f"native AI screening evidence metadata is invalid for {code}")
        if company_research_review and not native_company_research_profile_matches(review):
            raise ValueError(f"native company-research profile is invalid for {code}")
        review_model_efforts.add((str(review.get("model") or ""), str(review.get("effort") or "")))
        action = str(review.get("ai_action") or "")
        category = str(review.get("final_category") or "")
        verdict = str(review.get("verdict") or "")
        action_counts[action] += 1
        category_counts[category] += 1
        verdict_counts[verdict] += 1
        pair_verdict_counts[verdict] += pair_count
        attempted += 1
        if verdict == "needs_review":
            needs_review_count += 1
            pair_needs_review_count += pair_count
        if review.get("web_search_performed") is True:
            searched += 1
            pair_searched += pair_count
        if review.get("web_search_verified") is True:
            completed_searches += 1
            pair_completed_searches += pair_count
        if review.get("web_search_event_verified") is True:
            events += 1
            pair_events += pair_count
        if review.get("web_search_claim_urls_verified") is True:
            claims += 1
            pair_claims += pair_count
        if review.get("research_source_urls_verified") is True:
            research_sources += 1
            research_source_pairs += pair_count
        dropped += int(review.get("web_search_dropped_claim_url_count", 0) or 0)
        pair_dropped += int(review.get("web_search_dropped_claim_url_count", 0) or 0) * pair_count
        pair_sum += pair_count
        seen_codes.add(code)
        seen_ranks.add(rank)
    if packets != sorted(packets, key=_publication_sort_key):
        raise ValueError("AI screening packets are not in publication order")
    if pair_sum != pair_total or len(seen_ranks) != candidate_total:
        raise ValueError("AI screening type-pair or rank totals are inconsistent")
    scalar_counts = {
        "attempted_review_count": attempted,
        "unreviewed_candidate_count": candidate_total - attempted,
        "attempted_needs_review_count": needs_review_count,
        "completed_review_count": attempted,
        "pending_review_count": candidate_total - attempted,
        "type_pair_reviewed_count": pair_sum,
        "type_pair_unreviewed_count": pair_total - pair_sum,
        "type_pair_needs_review_count": pair_needs_review_count,
        "web_search_completed_count": completed_searches,
        "web_source_verified_count": completed_searches,
        "type_pair_web_search_attempted_count": pair_searched,
        "type_pair_web_search_completed_count": pair_completed_searches,
        "type_pair_web_search_event_verified_count": pair_events,
        "type_pair_web_search_claim_urls_verified_count": pair_claims,
        "type_pair_web_search_dropped_claim_url_count": pair_dropped,
    }
    for field, expected in scalar_counts.items():
        if _int(payload.get(field), field) != expected:
            raise ValueError(f"AI screening scalar count is inconsistent: {field}")
    for verdict in _VERDICTS:
        if (
            _int((payload.get("verdict_counts") or {}).get(verdict, 0), f"verdict_counts.{verdict}")
            != verdict_counts[verdict]
        ):
            raise ValueError(f"AI screening verdict count is inconsistent: {verdict}")
        if (
            _int(
                (payload.get("type_pair_verdict_counts") or {}).get(verdict, 0),
                f"type_pair_verdict_counts.{verdict}",
            )
            != pair_verdict_counts[verdict]
        ):
            raise ValueError(f"AI screening type-pair verdict count is inconsistent: {verdict}")
    if review_mode == "local_codex_review" and not review_model_efforts <= {
        ("opencode-go/ox-alpha-free", "max"),
        ("opencode-go/muse-spark-1.2-contributor", "xhigh"),
        (LOCAL_REVIEW_MODEL, "max"),
        ("codex-luna-max", "max"),
    }:
        raise ValueError("local OpenCode Go reviews must use Ox max or Muse Spark xhigh")
    if company_research_review:
        allowed_model_efforts = {(profile[0], profile[1]) for profile in NATIVE_COMPANY_RESEARCH_PROFILES}
        if not review_model_efforts <= allowed_model_efforts:
            raise ValueError("native company research contains an unsupported model profile")
        if set(models) != {model for model, _ in review_model_efforts} or set(efforts) != {
            effort for _, effort in review_model_efforts
        }:
            raise ValueError("native company research model metadata does not match its reviews")
    for action in _ACTIONS:
        if (
            _int((payload.get("ai_action_counts") or {}).get(action), f"ai_action_counts.{action}")
            != action_counts[action]
        ):
            raise ValueError(f"AI screening action count is inconsistent: {action}")
    for category in _CATEGORIES:
        if (
            _int((payload.get("final_category_counts") or {}).get(category), f"final_category_counts.{category}")
            != category_counts[category]
        ):
            raise ValueError(f"AI screening category count is inconsistent: {category}")
    declared_action_scalars = {
        "priority_buy_count": action_counts["priority_buy"],
        "recommend_buy_count": action_counts["priority_buy"],
        "watchlist_count": action_counts["watchlist"],
        "avoid_count": action_counts["avoid"],
        "do_not_recommend_buy_count": action_counts["watchlist"]
        + action_counts["avoid"]
        + action_counts["insufficient_evidence"],
    }
    for field, expected in declared_action_scalars.items():
        if _int(payload.get(field), field) != expected:
            raise ValueError(f"AI screening action scalar is inconsistent: {field}")
    if _int(payload.get("insufficient_evidence_count"), "insufficient_evidence_count") != 0:
        raise ValueError("AI screening still contains insufficient-evidence conclusions")
    if external_full:
        if searched != candidate_total or events != candidate_total:
            raise ValueError("external full AI screening seed lacks per-company search proof")
        if company_research_review:
            if research_sources != candidate_total or research_source_pairs != pair_total:
                raise ValueError("native company research lacks per-company source proof")
        elif claims != candidate_total:
            raise ValueError("legacy external AI screening seed lacks search-claim URL proof")
        if payload.get("reviewed_without_web_search") != 0:
            raise ValueError("external full AI screening seed contains an unsearched review")
        if payload.get("full_coverage_web_search") is not True:
            raise ValueError("external full AI screening seed does not declare full search coverage")
    else:
        if searched > candidate_total or events > candidate_total or claims > candidate_total:
            raise ValueError("AI screening search proof exceeds company coverage")
        if payload.get("reviewed_without_web_search") != candidate_total - searched:
            raise ValueError("AI screening unsearched-review total is inconsistent")
    if (
        payload.get("web_search_attempted_count") != searched
        or payload.get("web_search_event_verified_count") != events
        or payload.get("web_search_claim_urls_verified_count") != claims
    ):
        raise ValueError("AI screening search proof totals are inconsistent")
    declared_research_sources = payload.get("research_source_urls_verified_count")
    declared_pair_research_sources = payload.get("type_pair_research_source_urls_verified_count")
    if company_research_review or declared_research_sources is not None or declared_pair_research_sources is not None:
        if declared_research_sources != research_sources or declared_pair_research_sources != research_source_pairs:
            raise ValueError("AI screening research-source proof totals are inconsistent")
    expected_full_search = searched == candidate_total and events == candidate_total
    declared_full_search = payload.get("full_coverage_web_search")
    if (declared_full_search is not None and declared_full_search is not expected_full_search) or (
        company_research_review and declared_full_search is not True
    ):
        raise ValueError("AI screening full-search flag is inconsistent")
    if payload.get("web_search_dropped_claim_url_count") != dropped:
        raise ValueError("AI screening dropped-claim total is inconsistent")
    return {
        "generation": expected_generation,
        "market_as_of": expected_market_as_of,
        "candidate_total": candidate_total,
        "type_pair_total": pair_total,
        "searched": searched,
        "event_verified": events,
        "claim_urls_verified": claims,
        "research_source_urls_verified": research_sources,
        "dropped_claim_urls": dropped,
        "actions": dict(sorted(action_counts.items())),
    }


def validate_artifact_file(path: Path, *, expected_generation: str, expected_market_as_of: str) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1 or size > MAX_PUBLIC_ARTIFACT_BYTES:
        raise ValueError(f"AI screening seed size {size} is outside the 1..{MAX_PUBLIC_ARTIFACT_BYTES} byte limit")
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, Mapping):
        raise ValueError("AI screening seed must be a JSON object")
    return validate_artifact(
        payload,
        expected_generation=expected_generation,
        expected_market_as_of=expected_market_as_of,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--expected-market-as-of", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            validate_artifact_file(
                args.artifact,
                expected_generation=args.expected_generation,
                expected_market_as_of=args.expected_market_as_of,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
