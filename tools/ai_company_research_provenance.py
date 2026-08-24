"""Strict provenance contract for generation-bound company research reviews.

The model runner is resumable and a final review file may legitimately contain
rows produced by two approved OpenCode Go profiles.  Model identity may vary;
the candidate universe, research facts, knowledge document, prompt protocol and
snapshot identity may not.  This module keeps that distinction explicit and
lets every downstream merge fail closed before an old or partial row is used.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import candidate_identity_sha256


PROVENANCE_SCHEMA_VERSION = 1
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# These are deliberately exact triples.  A similarly named provider/model or a
# lower reasoning effort is not an interchangeable production review profile.
ALLOWED_RESEARCH_PROFILES = frozenset(
    {
        (
            "opencode-go-muse/muse-spark-1.2-contributor",
            "opencode-go/muse-spark-1.2-contributor",
            "xhigh",
        ),
        (
            "opencode-go-deepseek-responses/deepseek-v4-flash",
            "opencode-go/deepseek-v4-flash",
            "max",
        ),
        (
            "opencode-go-anthropic/deepseek-v4-flash",
            "opencode-go/deepseek-v4-flash",
            "max",
        ),
        (
            "opencode-go/ox-alpha-free",
            "opencode-go/ox-alpha-free",
            "max",
        ),
        (
            "opencode/muse-spark-1.2-contributor-free",
            "opencode/muse-spark-1.2-contributor-free",
            "xhigh",
        ),
        (
            "opencode-go/deepseek-v4-flash",
            "opencode-go/deepseek-v4-flash",
            "max",
        ),
    }
)

_COMMON_FIELDS = (
    "snapshot_generation",
    "market_as_of",
    "research_as_of",
    "candidate_input_sha256",
    "candidate_identity_sha256",
    "candidate_universe_identity_sha256",
    "type_pair_candidate_identity_sha256",
    "research_sha256",
    "knowledge_sha256",
    "protocol_sha256",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not a UTF-8 JSON object: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value, raw


def _required_text(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is missing")
    return text


def _required_sha256(value: Any, *, label: str) -> str:
    digest = _required_text(value, label=label)
    if not _HEX_SHA256.fullmatch(digest):
        raise ValueError(f"{label} is not a lowercase SHA-256")
    return digest


def _type_keys(packet: Mapping[str, Any]) -> list[str]:
    raw = packet.get("type_keys")
    if isinstance(raw, list):
        values = [str(value or "").strip() for value in raw]
    else:
        candidates = packet.get("candidate_types")
        values = (
            [
                str(item.get("type_key") or item.get("type") or "").strip()
                for item in candidates
                if isinstance(item, Mapping)
            ]
            if isinstance(candidates, list)
            else [str(packet.get("type_key") or "").strip()]
        )
    if not values or any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError(f"candidate type-pair identity is invalid: {packet.get('security_code')}")
    return values


@dataclass(frozen=True)
class ResearchProvenanceContext:
    expected: dict[str, str]
    packets: dict[tuple[str, str], dict[str, Any]]


def load_research_provenance_context(
    input_path: Path,
    research_path: Path,
    knowledge_path: Path,
    protocol_path: Path,
    *,
    research_as_of: str,
) -> ResearchProvenanceContext:
    """Load and validate the immutable inputs for a full company review run."""

    payload, input_raw = _load_object(input_path, label="AI screening input")
    packets_value = payload.get("packets")
    if not isinstance(packets_value, list) or not packets_value:
        raise ValueError("AI screening input has no candidate packets")
    if not all(isinstance(packet, Mapping) for packet in packets_value):
        raise ValueError("AI screening input candidate packet must be an object")
    packets = [dict(packet) for packet in packets_value]

    generation = _required_text(payload.get("snapshot_generation"), label="snapshot_generation")
    market_as_of = _required_text(payload.get("market_as_of"), label="market_as_of")
    try:
        market_date = date.fromisoformat(market_as_of)
        research_date = date.fromisoformat(research_as_of)
    except ValueError as error:
        raise ValueError("market_as_of and research_as_of must be ISO dates") from error
    if research_date < market_date:
        raise ValueError("research_as_of cannot be earlier than market_as_of")

    computed_candidate_identity = candidate_identity_sha256(packets)
    declared_candidate_identity = _required_sha256(
        payload.get("candidate_identity_sha256"), label="candidate_identity_sha256"
    )
    if declared_candidate_identity != computed_candidate_identity:
        raise ValueError("candidate identity hash does not match candidate packets")
    universe_identity = _required_sha256(
        payload.get("candidate_universe_identity_sha256"), label="candidate_universe_identity_sha256"
    )
    type_pair_identity = _required_sha256(
        payload.get("type_pair_candidate_identity_sha256"),
        label="type_pair_candidate_identity_sha256",
    )
    if universe_identity != computed_candidate_identity or type_pair_identity != computed_candidate_identity:
        raise ValueError("full candidate/type-pair universe identity does not match candidate packets")

    company_count = len(packets)
    pair_count = sum(len(_type_keys(packet)) for packet in packets)
    if payload.get("queue_full_coverage") is not True:
        raise ValueError("company research provenance requires a full candidate queue")
    if payload.get("full_coverage_final_recommendation") is not True:
        raise ValueError("company research input is not declared as a full-coverage review")
    if int(payload.get("candidate_offset") or 0) != 0:
        raise ValueError("company research full-coverage queue has a non-zero offset")
    if int(payload.get("candidate_count") or -1) != company_count or int(
        payload.get("candidate_total") or -1
    ) != company_count:
        raise ValueError("company research candidate counts do not prove full coverage")
    if int(payload.get("type_pair_candidate_count") or -1) != pair_count or int(
        payload.get("type_pair_candidate_total") or -1
    ) != pair_count:
        raise ValueError("company research type-pair counts do not prove full coverage")

    packet_map: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_codes: set[str] = set()
    for packet in packets:
        code = _required_text(packet.get("security_code"), label="candidate security_code")
        primary_type = _required_text(packet.get("type_key"), label=f"candidate {code} type_key")
        identity = (code, primary_type)
        if identity in packet_map or code in candidate_codes:
            raise ValueError(f"duplicate candidate company identity: {code}")
        candidate_codes.add(code)
        packet_map[identity] = packet

    research, research_raw = _load_object(research_path, label="company research artifact")
    if research.get("artifact_kind") != "ai_company_research_facts":
        raise ValueError("company research artifact kind mismatch")
    if str(research.get("snapshot_generation") or "") != generation:
        raise ValueError("company research generation does not match candidate generation")
    if str(research.get("market_as_of") or "") != market_as_of:
        raise ValueError("company research market_as_of does not match candidate market_as_of")
    companies = research.get("companies")
    if not isinstance(companies, Mapping):
        raise ValueError("company research artifact has no companies mapping")
    if int(research.get("company_count") or -1) != len(companies):
        raise ValueError("company research artifact company_count mismatch")
    missing_research = sorted(candidate_codes - {str(code) for code in companies})
    if missing_research:
        raise ValueError(f"company research artifact is missing candidates: {missing_research[:8]}")
    for code in candidate_codes:
        company = companies.get(code)
        if not isinstance(company, Mapping):
            raise ValueError(f"company research row is not an object: {code}")
        declared_code = str(company.get("code") or "").strip()
        if declared_code and declared_code != code:
            raise ValueError(f"company research row code mismatch: {code}/{declared_code}")

    expected = {
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        "research_as_of": research_as_of,
        "candidate_input_sha256": _sha256_bytes(input_raw),
        "candidate_identity_sha256": computed_candidate_identity,
        "candidate_universe_identity_sha256": universe_identity,
        "type_pair_candidate_identity_sha256": type_pair_identity,
        "research_sha256": _sha256_bytes(research_raw),
        "knowledge_sha256": _sha256_bytes(knowledge_path.read_bytes()),
        "protocol_sha256": _sha256_bytes(protocol_path.read_bytes()),
    }
    return ResearchProvenanceContext(expected=expected, packets=packet_map)


def make_review_provenance(
    *,
    expected: Mapping[str, Any],
    run_identity: str,
    candidate_type_keys: list[str],
    reasonix_model: str,
    public_model: str,
    effort: str,
) -> dict[str, Any]:
    """Create the hidden provenance object that the runner attaches per row."""

    common = {field: _required_text(expected.get(field), label=field) for field in _COMMON_FIELDS}
    for field in _COMMON_FIELDS[3:]:
        _required_sha256(common[field], label=field)
    run_identity = _required_sha256(run_identity, label="run_identity")
    type_keys = [str(value or "").strip() for value in candidate_type_keys]
    if not type_keys or any(not value for value in type_keys) or len(type_keys) != len(set(type_keys)):
        raise ValueError("candidate_type_keys is invalid")
    profile = (reasonix_model, public_model, effort)
    if profile not in ALLOWED_RESEARCH_PROFILES:
        raise ValueError(f"unapproved company research profile: {profile}")
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        **common,
        "run_identity": run_identity,
        "candidate_type_keys": type_keys,
        "reasonix_model": reasonix_model,
        "public_model": public_model,
        "effort": effort,
    }


def validate_review_provenance(
    review: Mapping[str, Any],
    packet: Mapping[str, Any],
    context: ResearchProvenanceContext,
) -> None:
    """Reject a review not bound to the exact current research inputs."""

    provenance = review.get("_research_provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("company review is missing _research_provenance")
    if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("company review provenance schema mismatch")
    for field, expected_value in context.expected.items():
        if str(provenance.get(field) or "") != expected_value:
            raise ValueError(f"company review provenance {field} mismatch")
    if str(review.get("research_as_of") or "") != context.expected["research_as_of"]:
        raise ValueError("company review research_as_of does not match provenance")

    packet_code = str(packet.get("security_code") or "").strip()
    packet_type = str(packet.get("type_key") or "").strip()
    if str(review.get("security_code") or "").strip() != packet_code or str(
        review.get("type_key") or ""
    ).strip() != packet_type:
        raise ValueError("company review candidate identity does not match provenance packet")
    packet_name = str(packet.get("name") or "").strip()
    review_name = str(review.get("company_name") or review.get("name") or "").strip()
    if packet_name and review_name != packet_name:
        raise ValueError("company review name does not match provenance packet")

    profile = (
        str(provenance.get("reasonix_model") or ""),
        str(provenance.get("public_model") or ""),
        str(provenance.get("effort") or ""),
    )
    if profile not in ALLOWED_RESEARCH_PROFILES:
        raise ValueError(f"company review uses an unapproved research profile: {profile}")
    if str(review.get("retrieval_model") or "") != profile[0]:
        raise ValueError("company review retrieval_model does not match provenance")
    if str(review.get("model") or "") != profile[1]:
        raise ValueError("company review model does not match provenance")
    if str(review.get("effort") or "") != profile[2] or str(review.get("retrieval_effort") or "") != profile[2]:
        raise ValueError("company review effort does not match provenance")

    run_identity = _required_sha256(provenance.get("run_identity"), label="run_identity")
    if str(review.get("_research_run_identity") or "") != run_identity:
        raise ValueError("company review run identity does not match provenance")
    expected_types = _type_keys(packet)
    saved_types = review.get("_candidate_type_keys")
    provenance_types = provenance.get("candidate_type_keys")
    if saved_types != expected_types or provenance_types != expected_types:
        raise ValueError("company review candidate type-pair identity mismatch")


def validate_review_set_provenance(
    reviews: list[Mapping[str, Any]], context: ResearchProvenanceContext
) -> None:
    """Validate a complete set while allowing only the two approved profiles."""

    seen: set[tuple[str, str]] = set()
    for review in reviews:
        identity = (
            str(review.get("security_code") or "").strip(),
            str(review.get("type_key") or "").strip(),
        )
        packet = context.packets.get(identity)
        if packet is None:
            raise ValueError(f"company review is outside the current candidate universe: {identity}")
        if identity in seen:
            raise ValueError(f"duplicate company review identity: {identity}")
        seen.add(identity)
        validate_review_provenance(review, packet, context)
    missing = [identity for identity in context.packets if identity not in seen]
    if missing:
        raise ValueError(f"company research review set is incomplete: {missing[:8]}")
