from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from tools.audit_ai_screening_sources import source_semantic_projection_sha256
from tools.ai_screening_contract import candidate_identity_sha256, make_valuation_snapshot
from tools.validate_ai_screening_public import (
    MAX_PUBLIC_ARTIFACT_BYTES,
    validate_artifact,
    validate_artifact_file,
)


def _artifact() -> dict:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 60,
        "ai_action": "watchlist",
        "final_category": "observe",
        "final_recommendation": "do_not_recommend_buy",
        "recommendation_label": "观察",
        "confidence": "medium",
        "summary": "当前估值与经营质量仍需继续观察。",
        "key_strengths": ["估值与经营质量值得继续核验"],
        "risk_flags": ["仍需跟踪现金流变化"],
        "claims": [{"statement": "最新报告", "source_ref": "https://example.test/report"}],
        "model": "opencode-go/ox-alpha-free",
        "effort": "max",
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": True,
        "web_search_verified": True,
        "web_search_query_count": 1,
        "web_search_verified_claim_url_count": 1,
        "web_search_dropped_claim_url_count": 0,
    }
    value = {
        "schema_version": 2,
        "review_schema_version": 2,
        "artifact_kind": "ai_screening_overlay",
        "ai_is_advisory": True,
        "auto_buy_promotion": False,
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "candidate_offset": 0,
        "full_coverage_final_recommendation": True,
        "full_coverage_web_search": True,
        "review_mode": "opencode_web_review",
        "review_models": ["opencode-go/ox-alpha-free"],
        "review_efforts": ["max"],
        "candidate_total": 1,
        "reviewed_count": 1,
        "type_pair_candidate_total": 1,
        "type_pair_expected_total": 1,
        "type_pair_unique_company_count": 1,
        "type_pair_reviewed_count": 1,
        "type_pair_unreviewed_count": 0,
        "type_pair_needs_review_count": 0,
        "type_pair_verdict_counts": {
            "confirmed": 0,
            "caution": 1,
            "misclassified": 0,
            "missed_candidate": 0,
            "needs_review": 0,
        },
        "type_pair_web_search_attempted_count": 1,
        "type_pair_web_search_completed_count": 1,
        "type_pair_web_search_event_verified_count": 1,
        "type_pair_web_search_claim_urls_verified_count": 1,
        "type_pair_web_search_dropped_claim_url_count": 0,
        "attempted_review_count": 1,
        "unreviewed_candidate_count": 0,
        "attempted_needs_review_count": 0,
        "completed_review_count": 1,
        "pending_review_count": 0,
        "verdict_counts": {
            "confirmed": 0,
            "caution": 1,
            "misclassified": 0,
            "missed_candidate": 0,
            "needs_review": 0,
        },
        "ai_action_counts": {"priority_buy": 0, "watchlist": 1, "avoid": 0, "insufficient_evidence": 0},
        "final_category_counts": {"recommend_buy": 0, "observe": 1, "do_not_recommend": 0},
        "priority_buy_count": 0,
        "recommend_buy_count": 0,
        "watchlist_count": 1,
        "avoid_count": 0,
        "do_not_recommend_buy_count": 1,
        "insufficient_evidence_count": 0,
        "reviewed_without_web_search": 0,
        "web_search_attempted_count": 1,
        "web_search_event_verified_count": 1,
        "web_search_claim_urls_verified_count": 1,
        "web_search_completed_count": 1,
        "web_source_verified_count": 1,
        "web_search_dropped_claim_url_count": 0,
        "source_audit": {
            "available": True,
            "invalid_claim_url_count": 0,
            "invalid": 0,
            "failed": 0,
            "blocked": 0,
            "checked": 1,
            "ok": 1,
        },
        "packets": [
            {
                "security_code": "600000",
                "name": "浦发银行",
                "type_key": "type1",
                "type_keys": ["type1"],
                "type_pair_count": 1,
                "ai_rank": 1,
                "ai_review": review,
            }
        ],
    }
    identity = candidate_identity_sha256(value["packets"])
    value.update(
        {
            "candidate_identity_sha256": identity,
            "candidate_universe_identity_sha256": identity,
            "type_pair_candidate_identity_sha256": identity,
            "type_pair_universe_identity_sha256": identity,
        }
    )
    return value


def _two_packet_artifact() -> dict:
    value = _artifact()
    second = copy.deepcopy(value["packets"][0])
    second.update(
        {
            "security_code": "000001",
            "name": "平安银行",
            "type_key": "type7",
            "type_keys": ["type7"],
            "ai_rank": 2,
        }
    )
    second["ai_review"].update(
        {
            "security_code": "000001",
            "type_key": "type7",
            "buy_attractiveness_score": 55,
        }
    )
    value["packets"].append(second)
    value.update(
        {
            "candidate_total": 2,
            "reviewed_count": 2,
            "type_pair_candidate_total": 2,
            "type_pair_expected_total": 2,
            "type_pair_unique_company_count": 2,
            "type_pair_reviewed_count": 2,
            "attempted_review_count": 2,
            "completed_review_count": 2,
            "type_pair_verdict_counts": {
                "confirmed": 0,
                "caution": 2,
                "misclassified": 0,
                "missed_candidate": 0,
                "needs_review": 0,
            },
            "verdict_counts": {
                "confirmed": 0,
                "caution": 2,
                "misclassified": 0,
                "missed_candidate": 0,
                "needs_review": 0,
            },
            "ai_action_counts": {
                "priority_buy": 0,
                "watchlist": 2,
                "avoid": 0,
                "insufficient_evidence": 0,
            },
            "final_category_counts": {
                "recommend_buy": 0,
                "observe": 2,
                "do_not_recommend": 0,
            },
            "watchlist_count": 2,
            "do_not_recommend_buy_count": 2,
            "web_search_attempted_count": 2,
            "web_search_event_verified_count": 2,
            "web_search_claim_urls_verified_count": 2,
            "web_search_completed_count": 2,
            "web_source_verified_count": 2,
            "type_pair_web_search_attempted_count": 2,
            "type_pair_web_search_completed_count": 2,
            "type_pair_web_search_event_verified_count": 2,
            "type_pair_web_search_claim_urls_verified_count": 2,
        }
    )
    value["source_audit"].update({"checked": 2, "ok": 2})
    identity = candidate_identity_sha256(value["packets"])
    for field in (
        "candidate_identity_sha256",
        "candidate_universe_identity_sha256",
        "type_pair_candidate_identity_sha256",
        "type_pair_universe_identity_sha256",
    ):
        value[field] = identity
    return value


def _bind_source_projection(value: dict) -> None:
    projection_sha256, projection_counts = source_semantic_projection_sha256(value)
    value["source_audit"].update(
        {
            "projection_sha256": projection_sha256,
            **projection_counts,
        }
    )


def test_public_validator_accepts_generation_bound_full_seed() -> None:
    result = validate_artifact(_artifact(), expected_generation="g1", expected_market_as_of="2026-08-21")
    assert result["candidate_total"] == 1
    assert result["searched"] == 1


def test_checked_in_seed_is_readable_and_bound_to_the_latest_close() -> None:
    path = Path("cloudflare/quant-dashboard/ai_screening_seed.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = validate_artifact_file(
        path,
        expected_generation="730c15f049a9bad9",
        expected_market_as_of="2026-08-26",
    )

    assert result["candidate_total"] == 994
    assert result["searched"] == 994
    assert result["actions"] == {"avoid": 311, "priority_buy": 10, "watchlist": 673}
    assert payload["review_mode"] == "codex_luna_web_review"
    assert payload["reviewed_without_web_search"] == 0
    assert payload["full_coverage_final_recommendation"] is True
    raw_search_metadata = re.compile(r"turn\w*(?:search|view)|\[wordlim:|Published:|Crawled:", re.IGNORECASE)
    for packet in payload["packets"]:
        review = packet["ai_review"]
        assert review["summary"].strip()
        assert review["quantitative_facts"]
        for field in ("summary", "key_strengths", "risk_flags", "quantitative_facts"):
            values = review.get(field, [])
            values = values if isinstance(values, list) else [values]
            assert not raw_search_metadata.search(" ".join(str(value) for value in values))


def test_checked_in_seed_contains_full_company_review_and_checked_promotions() -> None:
    payload = json.loads(Path("cloudflare/quant-dashboard/ai_screening_seed.json").read_text(encoding="utf-8"))
    packets = payload["packets"]
    assert len(packets) == 994
    assert all(packet["ai_review"]["model"] == "codex-luna-max" for packet in packets)
    assert all(packet["ai_review"]["effort"] == "max" for packet in packets)
    # Source repair may remove an unprovable auxiliary claim, but every
    # company must retain at least one independently bound fact.
    assert all(len(packet["ai_review"]["claims"]) >= 1 for packet in packets)
    assert {
        packet["security_code"] for packet in packets if packet["ai_review"]["final_category"] == "recommend_buy"
    } == {
        "603444",
        "300515",
        "000680",
        "601677",
        "002468",
        "002415",
        "600919",
        "601988",
        "002142",
        "601336",
    }
    assert all(packet["ai_review"]["web_search_event_verified"] is True for packet in packets)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: (
            value["packets"][0].update(security_code="600001"),
            value["packets"][0]["ai_review"].update(security_code="600001"),
        ),
        lambda value: (
            value["packets"][0].update(type_key="type2", type_keys=["type2"]),
            value["packets"][0]["ai_review"].update(type_key="type2"),
        ),
    ],
)
def test_public_validator_rejects_packet_identity_tampering(mutation) -> None:
    value = _artifact()
    mutation(value)

    with pytest.raises(ValueError, match="candidate_identity_sha256"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


@pytest.mark.parametrize(
    "field",
    [
        "candidate_identity_sha256",
        "candidate_universe_identity_sha256",
        "type_pair_candidate_identity_sha256",
        "type_pair_universe_identity_sha256",
    ],
)
def test_public_validator_rejects_each_stale_identity_digest(field: str) -> None:
    value = _artifact()
    value[field] = "0" * 64

    with pytest.raises(ValueError, match=field):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_public_validator_rejects_packets_out_of_publication_order() -> None:
    value = _two_packet_artifact()
    value["packets"].reverse()
    for rank, packet in enumerate(value["packets"], 1):
        packet["ai_rank"] = rank

    with pytest.raises(ValueError, match="publication order"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_public_validator_rejects_rank_not_matching_array_position() -> None:
    value = _two_packet_artifact()
    value["packets"][0]["ai_rank"] = 2
    value["packets"][1]["ai_rank"] = 1

    with pytest.raises(ValueError, match="rank"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recommendation_label", "建议买"),
        ("summary", "当前结论：建议买入。估值与经营质量均支持立即买入。"),
        ("key_strengths", ["当前结论：建议买入。经营质量足以支持立即买入。"]),
        ("risk_flags", ["当前结论：建议买入。风险不影响立即买入。"]),
    ],
)
def test_public_validator_rejects_conclusion_conflicts_in_all_reason_fields(field: str, value) -> None:
    artifact = _artifact()
    artifact["packets"][0]["ai_review"][field] = value

    with pytest.raises(ValueError, match="semantically invalid|conflicts with its conclusion"):
        validate_artifact(artifact, expected_generation="g1", expected_market_as_of="2026-08-21")


def _company_research_artifact() -> dict:
    value = _artifact()
    value["review_mode"] = "opencode_native_company_research_review"
    value["review_models"] = ["opencode-go/muse-spark-1.2-contributor"]
    value["review_efforts"] = ["xhigh"]
    value["full_coverage_web_search"] = True
    value["research_as_of"] = "2026-08-23"
    value["research_source_urls_verified_count"] = 1
    value["type_pair_research_source_urls_verified_count"] = 1
    value["source_audit"]["claim_count"] = 3
    value["source_audit"]["audit_passed"] = True
    value["source_audit"]["merged_sha256"] = "b" * 64
    value["source_audit"]["audit_sha256"] = "c" * 64
    value["source_audit"].update(
        {
            "audit_contract_version": 3,
            "semantic_claim_count": 3,
            "semantic_passed_count": 3,
            "semantic_failed_count": 0,
            "semantic_unverified_count": 0,
            "canonical_urls": [
                "https://example.test/company-report",
                "https://example.test/company-report#cashflow",
                "https://example.test/company-report#valuation",
            ],
            "source_bindings": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "claim_index": 0,
                    "search_finding_id": "search-business",
                    "url": "https://example.test/company-report",
                    "kind": "claim",
                },
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "claim_index": 1,
                    "search_finding_id": "",
                    "url": "https://example.test/company-report#cashflow",
                    "kind": "claim",
                },
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "claim_index": 2,
                    "search_finding_id": "",
                    "url": "https://example.test/company-report#valuation",
                    "kind": "claim",
                },
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "claim_index": None,
                    "finding_index": 0,
                    "search_finding_id": "search-business",
                    "url": "https://example.test/company-report",
                    "kind": "search_finding",
                },
            ],
            "company_coverage": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "referenced_finding_ids": ["search-business"],
                    "searched_no_source_finding_ids": [],
                    "referenced_no_source_finding_ids": [],
                    "canonical_url_count": 3,
                    "semantic_claim_count": 3,
                    "semantic_passed_count": 3,
                    "semantic_failed_count": 0,
                    "semantic_unverified_count": 0,
                    "all_referenced_findings_semantic_pass": True,
                    "status": "pass",
                }
            ],
        }
    )
    value["web_search_claim_urls_verified_count"] = 0
    value["web_search_completed_count"] = 0
    value["web_source_verified_count"] = 0
    value["type_pair_web_search_completed_count"] = 0
    value["type_pair_web_search_claim_urls_verified_count"] = 0
    review = value["packets"][0]["ai_review"]
    review.update(
        {
            "model": "opencode-go/muse-spark-1.2-contributor",
            "effort": "xhigh",
            "economic_category": "quality_equity",
            "score_components": {
                "risk_adjusted_expected_return": 60.0,
                "evidence_confidence": 83.3333333333,
            },
            "calibration_adjustments": {
                "raw_score": 60.0,
                "source_penalty": 0.0,
                "freshness_penalty": 0.0,
                "pre_band_score": 60.0,
                "action_band_min": 50.0,
                "action_band_max": 69.0,
                "final_score": 60.0,
                "source_quality": "verified_https",
                "freshness_status": "current_or_recent",
                "band_clamped": False,
                "verdict": "caution",
            },
            "quantitative_facts": ["2025年度经营现金流 18 亿元"],
            "retrieval_backend": "reasonix-native-server-web-search",
            "retrieval_model": "opencode-go-muse/muse-spark-1.2-contributor",
            "retrieval_effort": "xhigh",
            "native_search_completed": True,
            "official_fetch_completed": True,
            "web_search_claim_urls_verified": False,
            "web_search_verified": False,
            "web_search_verified_claim_url_count": 0,
            "research_source_urls_verified": True,
            "research_as_of": "2026-08-23",
            "claims": [
                {
                    "statement": "2026年半年度报告披露企业金融与零售金融业务。",
                    "source_ref": "https://example.test/company-report",
                    "source_kind": "company_ir",
                    "search_finding_id": "search-business",
                    "support": "supports",
                },
                {
                    "statement": "2025年度经营现金流 18 亿元。",
                    "source_ref": "https://example.test/company-report#cashflow",
                    "source_kind": "company_ir",
                    "fact_id": "cashflow-fact",
                    "support": "supports",
                },
                {
                    "statement": "2025年度报告披露当前估值与股价。",
                    "source_ref": "https://example.test/company-report#valuation",
                    "source_kind": "company_ir",
                    "fact_id": "valuation-fact",
                    "support": "context",
                },
            ],
            "search_findings": [
                {
                    "id": "search-business",
                    "query": "600000 浦发银行 最新经营情况",
                    "title": "公司半年度报告",
                    "url": "https://example.test/company-report",
                    "published_at": "2026-08-20",
                    "report_period": "2026H1",
                    "finding": "主营业务和经营现金流仍需继续核验。",
                    "stance": "neutral",
                    "source_kind": "company_ir",
                    "source_quality": "primary",
                }
            ],
            "evidence_bindings": {
                "summary": {"fact_ids": ["cashflow-fact", "valuation-fact"], "search_finding_ids": []},
                "strengths": [{"fact_ids": ["cashflow-fact"], "search_finding_ids": []}],
                "risks": [{"fact_ids": ["valuation-fact"], "search_finding_ids": ["search-business"]}],
                "economic_profile": {
                    "business_model": {"fact_ids": [], "search_finding_ids": ["search-business"]},
                    "moat": {"fact_ids": ["valuation-fact"], "search_finding_ids": []},
                    "cycle": {"fact_ids": ["cashflow-fact"], "search_finding_ids": []},
                    "fcf_outlook": {"fact_ids": ["cashflow-fact"], "search_finding_ids": []},
                    "governance": {"fact_ids": ["valuation-fact"], "search_finding_ids": []},
                },
                "valuation": {"fact_ids": ["valuation-fact"], "search_finding_ids": []},
            },
            "economic_profile": {
                "business_model": "通过企业金融与零售金融获取利差及手续费收入。",
                "business_model_source_ids": ["search-business"],
                "business_model_sources": [
                    {
                        "id": "search-business",
                        "statement": "2026年半年度报告披露企业金融与零售金融业务。",
                        "source_ref": "https://example.test/company-report",
                        "source_kind": "company_ir",
                    }
                ],
                "business_model_source_quality": "current_primary",
                "business_model_source_status": "source_found",
                "business_model_uncertainty": "已由2026年半年度报告的一手业务分部口径核验。",
                "moat": "客户基础仍有价值，但净息差下行构成反证。",
                "cycle": "信用成本与净息差处于需要继续观察的阶段。",
                "fcf_outlook": "结合资本充足率和分红能力判断股东现金回报。",
                "governance": "资本补充需求与分红安排需要同时核验。",
            },
            "valuation": {
                "method": "book_value_multiple",
                "as_of": "2026-08-21",
                "current_price": 12.34,
                "pe": 6.2,
                "pb": 0.55,
                "market_cap": 3621.0,
                "scenarios": {
                    "bear": {
                        "value_per_share": 11.2,
                        "upside_pct": -9.2382495948,
                        "book_value_per_share": 20.0,
                        "target_pb": 0.56,
                    },
                    "base": {
                        "value_per_share": 16.0,
                        "upside_pct": 29.6596434360,
                        "book_value_per_share": 20.0,
                        "target_pb": 0.8,
                    },
                    "bull": {
                        "value_per_share": 20.8,
                        "upside_pct": 68.5575364668,
                        "book_value_per_share": 20.0,
                        "target_pb": 1.04,
                    },
                },
                "margin_of_safety": -10.1785714286,
                "safety_margin_band": "negative",
                "basis": "结合市净率、资产质量与悲观信用成本情景判断安全边际。",
                "evidence_ids": ["valuation-fact"],
                "normalization_anchor": {
                    "metric": "book_value_per_share",
                    "years": [],
                    "total": None,
                    "share_count": None,
                    "per_share": 20.0,
                    "source_ref": "https://example.test/company-report#valuation",
                },
                "multiple_basis": {
                    "metric": "pb",
                    "value": 0.8,
                    "source_ref": "https://example.test/company-report#valuation",
                    "search_finding_id": None,
                },
            },
            "valuation_snapshot": make_valuation_snapshot(
                security_code="600000",
                snapshot_generation="g1",
                market_as_of="2026-08-21",
                current_price=12.34,
                pe=6.2,
                pb=0.55,
                market_cap=3621.0,
            ),
        }
    )
    _bind_source_projection(value)
    return value


def test_company_research_accepts_independent_sources_without_search_claim_binding() -> None:
    result = validate_artifact(
        _company_research_artifact(), expected_generation="g1", expected_market_as_of="2026-08-21"
    )
    assert result["event_verified"] == 1
    assert result["claim_urls_verified"] == 0
    assert result["research_source_urls_verified"] == 1


def test_company_research_rejects_packet_url_not_in_source_audit() -> None:
    value = _company_research_artifact()
    value["packets"][0]["ai_review"]["claims"][1]["source_ref"] = "https://example.test/forged"

    with pytest.raises(ValueError, match="semantic projection|canonical URLs"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


@pytest.mark.parametrize(
    ("collection", "field", "replacement"),
    [
        ("claims", "statement", "同一来源网址下被篡改的声明。"),
        ("search_findings", "finding", "同一来源网址下被篡改的搜索结论。"),
    ],
)
def test_company_research_rejects_stale_source_semantic_projection(
    collection: str, field: str, replacement: str
) -> None:
    value = _company_research_artifact()
    value["packets"][0]["ai_review"][collection][0][field] = replacement

    with pytest.raises(ValueError, match="semantic projection is stale"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_allows_unreferenced_searched_no_source() -> None:
    value = _company_research_artifact()
    value["packets"][0]["ai_review"]["search_findings"].append(
        {
            "id": "search-no-source",
            "query": "600000 浦发银行 业务口径",
            "title": "未找到可引用来源",
            "url": None,
            "published_at": None,
            "report_period": "2026H1",
            "finding": "已完成搜索但未找到可引用来源。",
            "stance": "neutral",
            "source_kind": "not_found",
            "source_quality": "not_found",
        }
    )
    coverage = value["source_audit"]["company_coverage"][0]
    coverage["searched_no_source_finding_ids"] = ["search-no-source"]
    coverage["status"] = "searched_no_source"
    _bind_source_projection(value)

    result = validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")

    assert result["candidate_total"] == 1


def test_company_research_rejects_legacy_source_audit_contract() -> None:
    value = _company_research_artifact()
    value["source_audit"].pop("audit_contract_version")

    with pytest.raises(ValueError, match="contract v3"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_accepts_native_search_without_official_fetch() -> None:
    value = _company_research_artifact()
    value["packets"][0]["ai_review"]["official_fetch_completed"] = False

    result = validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")

    assert result["event_verified"] == 1
    assert result["research_source_urls_verified"] == 1


def test_company_research_accepts_mixed_muse_and_deepseek_native_profiles() -> None:
    value = _company_research_artifact()
    second = copy.deepcopy(value["packets"][0])
    second.update(
        {
            "security_code": "000001",
            "name": "平安银行",
            "type_key": "type7",
            "type_keys": ["type7"],
            "ai_rank": 1,
        }
    )
    second_review = second["ai_review"]
    second_review.update(
        {
            "security_code": "000001",
            "type_key": "type7",
            "model": "opencode-go/deepseek-v4-flash",
            "effort": "max",
            "retrieval_model": "opencode-go-deepseek-responses/deepseek-v4-flash",
            "retrieval_effort": "max",
            "valuation_snapshot": make_valuation_snapshot(
                security_code="000001",
                snapshot_generation="g1",
                market_as_of="2026-08-21",
                current_price=12.34,
                pe=6.2,
                pb=0.55,
                market_cap=3621.0,
            ),
        }
    )
    value["packets"][0]["ai_rank"] = 2
    value["packets"] = [second, value["packets"][0]]
    value["review_models"] = [
        "opencode-go/deepseek-v4-flash",
        "opencode-go/muse-spark-1.2-contributor",
    ]
    value["review_efforts"] = ["max", "xhigh"]
    value["candidate_total"] = 2
    value["reviewed_count"] = 2
    value["type_pair_unique_company_count"] = 2
    value["type_pair_candidate_total"] = 2
    value["type_pair_expected_total"] = 2
    value["type_pair_reviewed_count"] = 2
    value["attempted_review_count"] = 2
    value["completed_review_count"] = 2
    value["type_pair_verdict_counts"]["caution"] = 2
    value["verdict_counts"]["caution"] = 2
    value["ai_action_counts"]["watchlist"] = 2
    value["final_category_counts"]["observe"] = 2
    value["watchlist_count"] = 2
    value["do_not_recommend_buy_count"] = 2
    value["web_search_attempted_count"] = 2
    value["web_search_event_verified_count"] = 2
    value["type_pair_web_search_attempted_count"] = 2
    value["type_pair_web_search_event_verified_count"] = 2
    value["research_source_urls_verified_count"] = 2
    value["type_pair_research_source_urls_verified_count"] = 2
    value["source_audit"]["claim_count"] = 6
    value["source_audit"]["semantic_claim_count"] = 6
    value["source_audit"]["semantic_passed_count"] = 6
    value["source_audit"]["company_coverage"] = value["source_audit"]["company_coverage"] + [
        {**copy.deepcopy(value["source_audit"]["company_coverage"][0]), "security_code": "000001", "name": "平安银行"}
    ]
    value["source_audit"]["source_bindings"] = [
        *value["source_audit"]["source_bindings"],
        *[
            {**binding, "security_code": "000001", "name": "平安银行", "type_key": "type7"}
            for binding in value["source_audit"]["source_bindings"]
        ],
    ]
    identity = candidate_identity_sha256(value["packets"])
    for field in (
        "candidate_identity_sha256",
        "candidate_universe_identity_sha256",
        "type_pair_candidate_identity_sha256",
        "type_pair_universe_identity_sha256",
    ):
        value[field] = identity
    _bind_source_projection(value)

    result = validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")

    assert result["candidate_total"] == 2
    assert result["searched"] == 2

    crossed = copy.deepcopy(value)
    crossed["packets"][0]["ai_review"]["retrieval_effort"] = "xhigh"
    with pytest.raises(ValueError, match="profile is invalid"):
        validate_artifact(crossed, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_rejects_unverified_research_sources() -> None:
    value = _company_research_artifact()
    value["research_source_urls_verified_count"] = 0
    value["type_pair_research_source_urls_verified_count"] = 0
    value["packets"][0]["ai_review"]["research_source_urls_verified"] = False
    with pytest.raises(ValueError, match="source proof"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_rejects_failed_source_audit() -> None:
    value = _company_research_artifact()
    value["source_audit"].update({"audit_passed": False, "failed": 1, "ok": 0})

    with pytest.raises(ValueError, match="unreachable"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_rejects_unbound_business_model_source() -> None:
    value = _company_research_artifact()
    value["packets"][0]["ai_review"]["economic_profile"]["business_model_sources"][0]["source_ref"] = (
        "https://example.test/forged"
    )

    with pytest.raises(ValueError, match="business-model source is unbound"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_rejects_dropped_or_tampered_valuation_scenarios() -> None:
    missing = _company_research_artifact()
    del missing["packets"][0]["ai_review"]["valuation"]["scenarios"]
    with pytest.raises(ValueError, match="semantically invalid"):
        validate_artifact(missing, expected_generation="g1", expected_market_as_of="2026-08-21")

    tampered = _company_research_artifact()
    tampered["packets"][0]["ai_review"]["valuation"]["scenarios"]["bear"]["upside_pct"] = 8.0
    with pytest.raises(ValueError, match="semantically invalid"):
        validate_artifact(tampered, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_company_research_rejects_synchronised_price_and_upside_tamper() -> None:
    value = _company_research_artifact()
    review = value["packets"][0]["ai_review"]
    valuation = review["valuation"]
    valuation["current_price"] = 13.0
    review["valuation_snapshot"]["current_price"] = 13.0
    for scenario in valuation["scenarios"].values():
        scenario["upside_pct"] = (scenario["value_per_share"] / 13.0 - 1.0) * 100.0
    valuation["margin_of_safety"] = (
        (valuation["scenarios"]["bear"]["value_per_share"] - 13.0)
        / valuation["scenarios"]["bear"]["value_per_share"]
        * 100.0
    )

    with pytest.raises(ValueError, match="valuation_snapshot|valuation snapshot"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_public_validator_rejects_stale_generation() -> None:
    with pytest.raises(ValueError, match="generation"):
        validate_artifact(_artifact(), expected_generation="g2", expected_market_as_of="2026-08-21")


def test_public_validator_rejects_missing_search_proof() -> None:
    value = _artifact()
    value["packets"][0]["ai_review"]["web_search_event_verified"] = False
    with pytest.raises(ValueError, match="search proof|semantically invalid"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(type_pair_unique_company_count=0),
        lambda value: value["packets"][0].update(type_keys=["type1", "type1"], type_pair_count=2),
        lambda value: value.update(attempted_review_count=0),
    ],
)
def test_public_validator_rejects_type_pair_or_scalar_count_tampering(mutation) -> None:
    value = _artifact()
    mutation(value)

    with pytest.raises(ValueError, match="type-pair|scalar|coverage"):
        validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")


def test_public_validator_accepts_mixed_local_review_with_explicit_audit() -> None:
    value = _artifact()
    value["review_mode"] = "opencode_mixed_review"
    value["full_coverage_web_search"] = False
    value["reviewed_without_web_search"] = 1
    value["web_search_attempted_count"] = 0
    value["web_search_event_verified_count"] = 0
    value["web_search_claim_urls_verified_count"] = 0
    value["web_search_completed_count"] = 0
    value["web_source_verified_count"] = 0
    value["type_pair_web_search_attempted_count"] = 0
    value["type_pair_web_search_completed_count"] = 0
    value["type_pair_web_search_event_verified_count"] = 0
    value["type_pair_web_search_claim_urls_verified_count"] = 0
    review = value["packets"][0]["ai_review"]
    review["claims"] = []
    review["web_search_performed"] = False
    review["web_search_event_verified"] = False
    review["web_search_claim_urls_verified"] = False
    review["web_search_verified"] = False
    review["web_search_query_count"] = 0
    review["web_search_verified_claim_url_count"] = 0
    assert validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")["searched"] == 0


def test_public_validator_accepts_muse_xhigh_local_review() -> None:
    value = _artifact()
    value["review_mode"] = "local_codex_review"
    value["full_coverage_web_search"] = False
    value["review_models"] = ["opencode-go/muse-spark-1.2-contributor"]
    value["review_efforts"] = ["xhigh"]
    value["reviewed_without_web_search"] = 1
    value["web_search_attempted_count"] = 0
    value["web_search_event_verified_count"] = 0
    value["web_search_claim_urls_verified_count"] = 0
    value["web_search_completed_count"] = 0
    value["web_source_verified_count"] = 0
    value["type_pair_web_search_attempted_count"] = 0
    value["type_pair_web_search_completed_count"] = 0
    value["type_pair_web_search_event_verified_count"] = 0
    value["type_pair_web_search_claim_urls_verified_count"] = 0
    value["source_audit"] = {"available": False}
    review = value["packets"][0]["ai_review"]
    review["model"] = "opencode-go/muse-spark-1.2-contributor"
    review["effort"] = "xhigh"
    review["claims"] = []
    review["web_search_performed"] = False
    review["web_search_event_verified"] = False
    review["web_search_claim_urls_verified"] = False
    review["web_search_verified"] = False
    review["web_search_query_count"] = 0
    review["web_search_verified_claim_url_count"] = 0
    assert validate_artifact(value, expected_generation="g1", expected_market_as_of="2026-08-21")["searched"] == 0


def test_public_validator_rejects_artifact_over_32_mib(tmp_path) -> None:
    artifact = tmp_path / "oversized.json"
    artifact.write_bytes(b"{" + b"x" * MAX_PUBLIC_ARTIFACT_BYTES + b"}")

    with pytest.raises(ValueError, match="byte limit"):
        validate_artifact_file(
            artifact,
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )
