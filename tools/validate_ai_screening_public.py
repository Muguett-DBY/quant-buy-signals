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
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import (
    LOCAL_OPENCODE_MODELS,
    LOCAL_REVIEW_MODEL,
    NATIVE_WEB_REVIEW_MODE,
    NATIVE_WEB_REVIEW_MODEL,
    NATIVE_WEB_RETRIEVAL_MODEL,
    PARTIAL_SEARCH_REVIEW_MODES,
    validate_review,
)

_CODE_RE = re.compile(r"^[036]\d{5}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = ("priority_buy", "watchlist", "avoid", "insufficient_evidence")
_CATEGORIES = ("recommend_buy", "observe", "do_not_recommend")


def _int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is not a non-negative integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} exceeds {maximum}")
    return value


def _review_for_validation(review: Mapping[str, Any], packet: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(review)
    value.setdefault("schema_version", 2)
    value.setdefault("security_code", packet.get("security_code"))
    value.setdefault("type_key", packet.get("type_key"))
    return value


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
    candidate_total = _int(payload.get("candidate_total"), "candidate_total", maximum=2000)
    if candidate_total != len(packets) or _int(payload.get("reviewed_count"), "reviewed_count") != candidate_total:
        raise ValueError("AI screening company coverage is incomplete")
    if payload.get("candidate_offset") != 0 or payload.get("full_coverage_final_recommendation") is not True:
        raise ValueError("AI screening seed is not a full-coverage artifact")
    identity = str(payload.get("candidate_identity_sha256") or "")
    universe = str(payload.get("candidate_universe_identity_sha256") or "")
    if not _HASH_RE.fullmatch(identity) or identity != universe:
        raise ValueError("AI screening candidate identity hashes are invalid")
    pair_total = _int(payload.get("type_pair_candidate_total"), "type_pair_candidate_total")
    if pair_total != _int(payload.get("type_pair_expected_total"), "type_pair_expected_total"):
        raise ValueError("AI screening type-pair total does not match the candidate universe")
    if _int(payload.get("type_pair_reviewed_count"), "type_pair_reviewed_count") != pair_total:
        raise ValueError("AI screening type-pair coverage is incomplete")
    if _int(payload.get("type_pair_unreviewed_count"), "type_pair_unreviewed_count") != 0:
        raise ValueError("AI screening contains unreviewed type pairs")
    models = payload.get("review_models")
    efforts = payload.get("review_efforts")
    if not isinstance(models, list) or not models or not isinstance(efforts, list) or not efforts:
        raise ValueError("AI screening model/effort metadata is missing")
    review_mode = str(payload.get("review_mode") or "")
    if review_mode == "local_codex_review" and not (
        set(models) == {LOCAL_REVIEW_MODEL} or set(models) <= LOCAL_OPENCODE_MODELS
    ):
        raise ValueError("local AI screening seed uses an unexpected model")
    if review_mode == NATIVE_WEB_REVIEW_MODE and (
        set(models) != {NATIVE_WEB_REVIEW_MODEL} or set(efforts) != {"xhigh"}
    ):
        raise ValueError("native AI screening seed must use Muse Spark 1.2 xhigh")
    external_full = review_mode not in PARTIAL_SEARCH_REVIEW_MODES
    mixed_full = review_mode == "opencode_mixed_review"
    if external_full:
        source_audit = payload.get("source_audit")
        if not isinstance(source_audit, Mapping) or source_audit.get("available") is not True:
            raise ValueError("external full AI screening seed has no source audit")
        if _int(source_audit.get("invalid_claim_url_count"), "source_audit.invalid_claim_url_count") != 0:
            raise ValueError("external full AI screening seed has invalid claim URLs")
    elif mixed_full:
        source_audit = payload.get("source_audit")
        if not isinstance(source_audit, Mapping) or source_audit.get("available") is not True:
            raise ValueError("mixed full AI screening seed has no source audit")
        if _int(source_audit.get("invalid_claim_url_count"), "source_audit.invalid_claim_url_count") != 0:
            raise ValueError("mixed full AI screening seed has invalid claim URLs")
    seen_codes: set[str] = set()
    seen_ranks: set[int] = set()
    action_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    attempted = searched = events = claims = dropped = 0
    review_model_efforts: set[tuple[str, str]] = set()
    pair_sum = 0
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("AI screening packet is not an object")
        code = str(packet.get("security_code") or "")
        type_key = str(packet.get("type_key") or "")
        type_keys = packet.get("type_keys")
        pair_count = _int(packet.get("type_pair_count"), f"{code}.type_pair_count")
        rank = _int(packet.get("ai_rank"), f"{code}.ai_rank")
        if not _CODE_RE.fullmatch(code) or not re.fullmatch(r"type[1-7]", type_key):
            raise ValueError(f"AI screening packet identity is invalid: {code}/{type_key}")
        if code in seen_codes or rank in seen_ranks or rank < 1 or rank > candidate_total:
            raise ValueError(f"AI screening has duplicate company or rank: {code}/{rank}")
        if not isinstance(type_keys, list) or len(type_keys) != pair_count or type_key not in type_keys:
            raise ValueError(f"AI screening type-pair identity is invalid: {code}")
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            raise ValueError(f"AI screening review is missing: {code}")
        review_value = _review_for_validation(review, packet)
        errors = validate_review(review_value, require_readable_reason=True)
        if errors:
            raise ValueError(f"AI screening review is semantically invalid for {code}: {','.join(errors)}")
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
        review_model_efforts.add((str(review.get("model") or ""), str(review.get("effort") or "")))
        action = str(review.get("ai_action") or "")
        category = str(review.get("final_category") or "")
        action_counts[action] += 1
        category_counts[category] += 1
        attempted += 1
        if review.get("web_search_performed") is True:
            searched += 1
        if review.get("web_search_event_verified") is True:
            events += 1
        if review.get("web_search_claim_urls_verified") is True:
            claims += 1
        dropped += int(review.get("web_search_dropped_claim_url_count", 0) or 0)
        pair_sum += pair_count
        seen_codes.add(code)
        seen_ranks.add(rank)
    if pair_sum != pair_total or len(seen_ranks) != candidate_total:
        raise ValueError("AI screening type-pair or rank totals are inconsistent")
    if review_mode == "local_codex_review" and not review_model_efforts <= {
        ("opencode-go/ox-alpha-free", "max"),
        ("opencode-go/muse-spark-1.2-contributor", "xhigh"),
        (LOCAL_REVIEW_MODEL, "max"),
    }:
        raise ValueError("local OpenCode Go reviews must use Ox max or Muse Spark xhigh")
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
    if _int(payload.get("insufficient_evidence_count"), "insufficient_evidence_count") != 0:
        raise ValueError("AI screening still contains insufficient-evidence conclusions")
    if external_full:
        if searched != candidate_total or events != candidate_total or claims != candidate_total:
            raise ValueError("external full AI screening seed lacks per-company search proof")
        if payload.get("reviewed_without_web_search") != 0:
            raise ValueError("external full AI screening seed contains an unsearched review")
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
        "dropped_claim_urls": dropped,
        "actions": dict(sorted(action_counts.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--expected-market-as-of", required=True)
    args = parser.parse_args()
    payload = json.loads(args.artifact.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("AI screening seed must be a JSON object")
    print(
        json.dumps(
            validate_artifact(
                payload, expected_generation=args.expected_generation, expected_market_as_of=args.expected_market_as_of
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
