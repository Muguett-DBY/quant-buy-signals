from __future__ import annotations

import json
import hashlib

import pytest

from tools.audit_ai_screening_sources import source_semantic_projection_sha256
from tools.ai_source_urls import claim_source_urls, iter_review_url_bindings, review_canonical_urls
from tools.ai_screening_contract import candidate_identity_sha256, make_valuation_snapshot, validate_review
from tools.apply_ai_screening_human_review import apply as apply_human_review
from tools.calibrate_ai_screening_ranking import _review, calibrate
from tools.build_ai_screening import build_input
from tools.publish_ai_screening import _public_review, build_artifact


def _external_review(action: str, score: float) -> dict:
    return {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": (
            "misclassified"
            if action == "avoid"
            else "needs_review"
            if action == "insufficient_evidence"
            else "confirmed"
        ),
        "recommended_action": (
            "demote" if action == "avoid" else "manual_review" if action == "insufficient_evidence" else "keep"
        ),
        "buy_attractiveness_score": score,
        "ai_action": action,
        "final_category": {
            "priority_buy": "recommend_buy",
            "watchlist": "observe",
            "avoid": "do_not_recommend",
            "insufficient_evidence": "observe",
        }[action],
        "confidence": "medium",
        "summary": "已复核估值、经营质量和主要风险，当前盈利与现金流支持结论。",
        "quantitative_facts": ["2025年度营业收入 120 亿元", "2025年度经营现金流 18 亿元"],
        "key_strengths": ["2025年度收入与经营现金流均保持正值"],
        "risk_flags": ["经营兑现仍需持续跟踪"],
        "claims": [
            {
                "statement": "2025年度营业收入 120 亿元",
                "source_ref": "https://example.test/report#revenue",
                "support": "supports",
            },
            {
                "statement": "2025年度经营现金流 18 亿元",
                "source_ref": "https://example.test/report#cashflow",
                "support": "supports",
            },
        ],
        "model": "opencode-go/ox-alpha-free",
    }


def _with_candidate_identity(payload: dict) -> dict:
    digest = candidate_identity_sha256(payload["packets"])
    payload["candidate_identity_sha256"] = digest
    payload["candidate_universe_identity_sha256"] = digest
    return payload


def _write_clean_source_audit(source, audit_path) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    projection_sha256, projection_counts = source_semantic_projection_sha256(payload)
    expected_urls = set()
    company_coverage = []
    source_bindings = []
    semantic_claim_count = 0
    for packet in payload.get("packets", []):
        review = packet.get("ai_review", {})
        expected_urls.update(review_canonical_urls(review))
        source_bindings.extend(
            iter_review_url_bindings(
                review,
                security_code=str(packet.get("security_code") or ""),
                name=str(packet.get("name") or packet.get("security_name") or ""),
                type_key=str(packet.get("type_key") or ""),
            )
        )
        findings = {
            str(finding.get("id") or ""): finding
            for finding in review.get("search_findings", [])
            if isinstance(finding, dict) and finding.get("id")
        }
        referenced_ids = set()
        company_semantic_count = 0
        company_claim_urls: set[str] = set()
        claim_ids_with_urls: set[str] = set()
        referenced_ids.update(
            finding_id
            for finding_id, finding in findings.items()
            if review_canonical_urls({"search_findings": [finding]})
        )
        for claim in review.get("claims", []):
            claim_urls = claim_source_urls(claim)
            company_semantic_count += len(claim_urls)
            company_claim_urls.update(claim_urls)
            if claim.get("search_finding_id"):
                referenced_ids.add(str(claim["search_finding_id"]))
                if claim_urls:
                    claim_ids_with_urls.add(str(claim["search_finding_id"]))
        finding_urls = {
            url for finding in findings.values() for url in review_canonical_urls({"search_findings": [finding]})
        }
        company_semantic_count += len(finding_urls - company_claim_urls)
        semantic_claim_count += company_semantic_count
        company_coverage.append(
            {
                "security_code": packet.get("security_code"),
                "name": packet.get("name", ""),
                "referenced_finding_ids": sorted(referenced_ids),
                "searched_no_source_finding_ids": sorted(
                    finding_id
                    for finding_id, finding in findings.items()
                    if not review_canonical_urls({"search_findings": [finding]})
                    and finding_id not in claim_ids_with_urls
                ),
                "semantic_claim_count": company_semantic_count,
                "semantic_passed_count": company_semantic_count,
                "semantic_failed_count": 0,
                "semantic_unverified_count": 0,
                "status": "pass",
            }
        )
    audit_path.write_text(
        json.dumps(
            {
                "audit_contract_version": 4,
                "merged_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "projection_sha256": projection_sha256,
                **projection_counts,
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "invalid_claim_url_count": 0,
                "audit_passed": True,
                "claim_count": sum(
                    len(packet.get("ai_review", {}).get("claims", [])) for packet in payload.get("packets", [])
                ),
                "semantic_claim_count": semantic_claim_count,
                "semantic_passed_count": semantic_claim_count,
                "semantic_failed_count": 0,
                "semantic_unverified_count": 0,
                "semantic_issue_count": 0,
                "semantic_html_date_checked_count": 1,
                "published_at_mismatch_count": 0,
                "report_period_after_publication_count": 0,
                "blocked_semantic_claim_count": 0,
                "canonical_urls": sorted(expected_urls),
                "source_bindings": source_bindings,
                "company_coverage": company_coverage,
                "checked": len(expected_urls),
                "ok": len(expected_urls),
                "failed": 0,
                "blocked": 0,
                "invalid": 0,
            }
        ),
        encoding="utf-8",
    )


def _native_company_review(*, code: str, model: str, effort: str, retrieval_model: str) -> dict:
    review = {
        **_external_review("watchlist", 60),
        "security_code": code,
        "model": model,
        "effort": effort,
        "ai_independent": True,
        "economic_category": "quality_equity",
        "score_components": {
            "risk_adjusted_expected_return": 60.0,
            "evidence_confidence": 83.3333333333,
        },
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": False,
        "research_source_urls_verified": True,
        "web_search_queries": [f"{code} 最新经营情况"],
        "web_search_verified_claim_urls": [],
        "retrieval_backend": "reasonix-native-server-web-search",
        "retrieval_model": retrieval_model,
        "retrieval_effort": effort,
        "native_search_completed": True,
        "official_fetch_completed": False,
        "research_as_of": "2026-08-23",
        "economic_profile": {
            "business_model": "通过主营产品销售获得收入与现金回报。",
            "business_model_source_quality": "current_primary",
            "business_model_uncertainty": "已由2026年半年度报告的一手业务口径核验。",
            "moat": "客户基础仍有价值，但竞争优势需持续验证。",
            "cycle": "需求与盈利处于需要继续观察的阶段。",
            "fcf_outlook": "结合资本开支与经营现金流判断股东现金回报。",
            "governance": "分红安排与资本配置需要同时核验。",
        },
        "valuation": {
            "method": "book_value_multiple",
            "as_of": "2026-08-21",
            "current_price": 12.34,
            "pe": 6.2,
            "pb": 0.55,
            "market_cap": 36_210_000_000.0,
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
            "basis": "结合当前股价、盈利倍数和悲观经营情景判断安全边际。",
        },
        "valuation_snapshot": make_valuation_snapshot(
            security_code=code,
            snapshot_generation="g1",
            market_as_of="2026-08-21",
            current_price=12.34,
            pe=6.2,
            pb=0.55,
            market_cap=36_210_000_000.0,
        ),
    }
    review["claims"][0].update(
        {
            "source_ref": "https://example.test/report#business",
            "search_finding_id": f"{code}-business",
            "source_kind": "company_ir",
        }
    )
    review["claims"][1].update(
        {
            "fact_id": f"{code}-cashflow",
            "source_kind": "company_ir",
        }
    )
    review["claims"].append(
        {
            "fact_id": f"{code}-valuation",
            "statement": "2025年度报告披露当前估值与股价。",
            "source_ref": "https://example.test/report#valuation",
            "source_kind": "company_ir",
            "support": "context",
        }
    )
    review["search_findings"] = [
        {
            "id": f"{code}-business",
            "query": f"{code} 最新经营情况",
            "title": "公司半年度报告",
            "url": "https://example.test/report#business",
            "published_at": "2026-08-20",
            "report_period": "2026H1",
            "finding": "主营业务和经营现金流仍需继续核验。",
            "stance": "neutral",
            "source_kind": "company_ir",
            "source_quality": "primary",
        }
    ]
    review["evidence_bindings"] = {
        "summary": {"fact_ids": [f"{code}-cashflow", f"{code}-valuation"], "search_finding_ids": []},
        "strengths": [{"fact_ids": [f"{code}-cashflow"], "search_finding_ids": []}],
        "risks": [{"fact_ids": [f"{code}-valuation"], "search_finding_ids": [f"{code}-business"]}],
        "economic_profile": {
            "business_model": {"fact_ids": [], "search_finding_ids": [f"{code}-business"]},
            "moat": {"fact_ids": [f"{code}-valuation"], "search_finding_ids": []},
            "cycle": {"fact_ids": [f"{code}-cashflow"], "search_finding_ids": []},
            "fcf_outlook": {"fact_ids": [f"{code}-cashflow"], "search_finding_ids": []},
            "governance": {"fact_ids": [f"{code}-valuation"], "search_finding_ids": []},
        },
        "valuation": {"fact_ids": [f"{code}-valuation"], "search_finding_ids": []},
    }
    review["valuation"].update(
        {
            "evidence_ids": [f"{code}-valuation"],
            "normalization_anchor": {
                "metric": "book_value_per_share",
                "years": [],
                "total": None,
                "share_count": None,
                "per_share": 20.0,
                "source_ref": "https://example.test/report#valuation",
            },
            "multiple_basis": {
                "metric": "pb",
                "value": 0.8,
                "source_ref": "https://example.test/report#valuation",
                "search_finding_id": None,
            },
        }
    )
    review["economic_profile"]["business_model_source_ids"] = [f"{code}-business"]
    return review


@pytest.mark.parametrize(
    ("action", "score", "error"),
    [
        ("priority_buy", 59, "priority_score_band"),
        ("watchlist", 100, "watchlist_score_band"),
        ("avoid", 100, "negative_score_band"),
        ("insufficient_evidence", 50, "negative_score_band"),
    ],
)
def test_score_bands_apply_to_external_models(action: str, score: float, error: str) -> None:
    assert error in validate_review(_external_review(action, score))


def test_calibration_bands_model_scores_to_the_final_action() -> None:
    packet = {
        "security_code": "600000",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
        "ai_review": {
            **_external_review("watchlist", 100),
            "claims": [
                {
                    "statement": "2025 年年度报告已披露",
                    "source_ref": "https://example.test/2025-report.pdf",
                    "support": "supports",
                }
            ],
            "web_search_performed": True,
        },
    }
    observed = _review(packet, "2026-08-21")
    assert observed["ai_action"] == "watchlist"
    assert observed["buy_attractiveness_score"] == 69.0
    assert validate_review(observed) == []

    packet["ai_review"] = {
        **packet["ai_review"],
        "verdict": "misclassified",
        "recommended_action": "demote",
        "ai_action": "avoid",
        "buy_attractiveness_score": 100,
    }
    avoided = _review(packet, "2026-08-21")
    assert avoided["ai_action"] == "avoid"
    assert avoided["buy_attractiveness_score"] == 49.0
    assert validate_review(avoided) == []


def test_local_unsearched_buy_without_source_linkage_is_not_publishable() -> None:
    packet = {
        "security_code": "600000",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
        "ai_review": {
            **_external_review("priority_buy", 84),
            "model": "opencode-go/muse-spark-1.2-contributor",
            "effort": "xhigh",
            "claims": [],
            "web_search_performed": False,
        },
    }
    observed = _review(packet, "2026-08-21")
    assert observed["ai_action"] == "priority_buy"
    assert observed["final_category"] == "recommend_buy"
    assert observed["buy_attractiveness_score"] >= 60
    assert observed["freshness_status"] == "undated"
    assert "priority_source_linked_research" in validate_review(observed)


def test_action_label_and_summary_cannot_contradict_the_decision() -> None:
    review = {
        **_external_review("priority_buy", 70),
        "recommendation_label": "观察",
        "summary": "当前列为观察，暂不建议买入。",
    }
    errors = validate_review(review)
    assert "recommendation_label_action_mismatch" in errors
    assert "summary_action_mismatch" in errors


@pytest.mark.parametrize(
    ("action", "verdict", "recommended_action", "expected_error"),
    [
        ("priority_buy", "caution", "keep", "action_verdict_mismatch"),
        ("priority_buy", "confirmed", "manual_review", "action_recommended_action_mismatch"),
        ("watchlist", "misclassified", "demote", "misclassified_decision_mismatch"),
        ("avoid", "needs_review", "manual_review", "needs_review_decision_mismatch"),
        ("insufficient_evidence", "confirmed", "keep", "action_verdict_mismatch"),
    ],
)
def test_action_verdict_and_review_action_must_form_one_decision(
    action: str,
    verdict: str,
    recommended_action: str,
    expected_error: str,
) -> None:
    score = 70 if action == "priority_buy" else 55 if action == "watchlist" else 40
    review = {
        **_external_review(action, score),
        "verdict": verdict,
        "recommended_action": recommended_action,
    }
    assert expected_error in validate_review(review)


@pytest.mark.parametrize(
    ("action", "summary"),
    [
        ("priority_buy", "综合判断，当前不建议买入，应继续等待。"),
        ("priority_buy", "估值尚可，但建议继续观望。"),
        ("priority_buy", "现阶段没有明确买点，也不值得配置。"),
        ("watchlist", "基本面优秀，强烈推荐买入。"),
        ("watchlist", "综合结论：买入，可以开始布局。"),
        ("avoid", "现阶段值得买入并分批建仓。"),
        ("avoid", "当前是明确买点，可逐步介入。"),
        ("insufficient_evidence", "AI 独立建议买入。"),
        ("priority_buy", "综合判断建议观察。"),
        ("priority_buy", "目前只宜观望。"),
        ("priority_buy", "建议不要买入。"),
        ("watchlist", "这家公司值得买。"),
        ("avoid", "当前可以买。"),
        ("insufficient_evidence", "AI 结论：建议买。"),
    ],
)
def test_obvious_current_recommendation_phrases_must_match_the_action(action: str, summary: str) -> None:
    score = 70 if action == "priority_buy" else 55 if action == "watchlist" else 40
    review = {**_external_review(action, score), "summary": summary}
    assert "summary_action_mismatch" in validate_review(review)


@pytest.mark.parametrize(
    ("action", "summary"),
    [
        ("priority_buy", "当前具备买入价值，但不建议追高买入；若现金流恶化则应回避。"),
        ("watchlist", "若价格回落至安全边际，可分批买入；当前继续观察。"),
        ("avoid", "券商历史上曾给出买入评级，但当前不建议买入。"),
        ("avoid", "券商建议买入，但本轮基于现金流恶化仍给出不建议结论。"),
        ("watchlist", "模型给出的建议买入区间低于现价，因此当前维持观察。"),
        ("priority_buy", "需要观察现金流和利润质量，但当前建议分批买入。"),
    ],
)
def test_conditional_or_risk_buy_language_is_not_mistaken_for_the_current_action(action: str, summary: str) -> None:
    score = 70 if action == "priority_buy" else 55 if action == "watchlist" else 40
    review = {**_external_review(action, score), "summary": summary}
    assert "summary_action_mismatch" not in validate_review(review)


def test_recommendation_label_cannot_hide_an_opposite_current_decision() -> None:
    review = {
        **_external_review("watchlist", 60),
        "recommendation_label": "观察·强烈推荐买入",
    }
    assert "recommendation_label_action_mismatch" in validate_review(review)


def test_strict_public_reason_requires_readable_summary_strength_and_risk() -> None:
    review = {
        **_external_review("priority_buy", 72),
        "summary": "当前估值和现金流共同提供安全边际。",
        "key_strengths": ["现金流改善"],
        "risk_flags": ["需求仍可能波动"],
    }
    assert validate_review(review, require_readable_reason=True) == []

    empty = {**review, "summary": "证据不足", "key_strengths": [], "risk_flags": [""]}
    errors = validate_review(empty, require_readable_reason=True)
    assert {
        "readable_summary_required",
        "readable_key_strengths_required",
        "readable_risk_flags_required",
    }.issubset(errors)


def test_partial_queue_cannot_be_calibrated_or_published_as_full_coverage(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "web_search_performed": True,
    }
    packet = {
        "security_code": "600000",
        "name": "浦发银行",
        "type_key": "type1",
        "deterministic": {"status": "triggered", "score": 8.0},
        "ai_review": review,
    }
    partial = {
        "snapshot_generation": "g1",
        "market_as_of": "2026-08-21",
        "candidate_offset": 0,
        "candidate_count": 1,
        "candidate_total": 2,
        "full_coverage_final_recommendation": True,
        "review_mode": "opencode_web_review",
        "packets": [packet],
    }
    source = tmp_path / "partial.json"
    source.write_text(json.dumps(partial), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate identity hash|complete unique reviewed candidate queue"):
        calibrate(source, tmp_path / "calibrated.json")
    with pytest.raises(ValueError, match="complete candidate queue"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_human_review_downgrade_is_generation_bound_and_keeps_reason(tmp_path) -> None:
    source = tmp_path / "source.json"
    corrections = tmp_path / "corrections.json"
    output = tmp_path / "output.json"
    source.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "packets": [
                    {
                        "security_code": "600000",
                        "type_key": "type1",
                        "ai_review": {
                            **_external_review("priority_buy", 72),
                            "summary": "当前建议买入。",
                            "risk_flags": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    corrections.write_text(
        json.dumps(
            {
                "snapshot_generation": "g1",
                "market_as_of": "2026-08-21",
                "corrections": {"600000": {"action": "watchlist", "score": 55, "reason": "价格闸门未通过。"}},
            }
        ),
        encoding="utf-8",
    )
    assert apply_human_review(source, corrections, output) == {"changed_packets": 1, "changed_companies": 1}
    reviewed = json.loads(output.read_text(encoding="utf-8"))["packets"][0]["ai_review"]
    assert reviewed["ai_action"] == "watchlist"
    assert reviewed["final_category"] == "observe"
    assert reviewed["final_recommendation"] == "do_not_recommend_buy"
    assert "价格闸门未通过" in reviewed["summary"]


def test_full_coverage_publish_requires_model_and_effort_metadata(tmp_path) -> None:
    source = tmp_path / "full.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "local_codex_review",
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": _external_review("watchlist", 60),
                        }
                    ],
                }
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="review models and efforts"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


@pytest.mark.parametrize("review_mode", ["opencode_web_review", "codex_luna_web_review"])
def test_publish_retains_external_search_event_and_claim_binding_proof(tmp_path, review_mode) -> None:
    review = {
        **_external_review("watchlist", 60),
        "effort": "max",
        "summary": "2026 年最新报告与当前估值已经完成二次复核，结论为继续观察。",
        "key_strengths": ["2026 年最新报告显示主营业务保持稳定。"],
        "risk_flags": ["当前估值仍需保留安全边际，暂不提高仓位。"],
        "claims": [
            {
                "statement": "2026 年最新正式报告已经披露。",
                "source_ref": "https://example.test/2026-report.pdf",
            }
        ],
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": review_mode != "codex_luna_web_review",
        "research_source_urls_verified": False,
        "web_search_verified": review_mode != "codex_luna_web_review",
        "web_search_queries": ["600000 浦发银行 2026 最新报告"],
        "web_search_verified_claim_urls": ["https://example.test/2026-report.pdf"],
        "web_search_dropped_claim_url_count": 1,
        "freshness_status": "current_or_recent",
        "freshness_years": [2026],
        "freshness_penalty": 0,
    }
    source = tmp_path / "full-external.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": review_mode,
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": review,
                        }
                    ],
                }
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit_path = tmp_path / "source-audit.json"
    _write_clean_source_audit(source, audit_path)
    artifact = build_artifact(
        source,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
        source_audit_path=audit_path,
    )

    assert artifact["full_coverage_web_search"] is True
    assert artifact["type_pair_web_search_event_verified_count"] == 1
    assert artifact["type_pair_web_search_claim_urls_verified_count"] == 1
    assert artifact["type_pair_web_search_dropped_claim_url_count"] == 1
    assert artifact["web_search_event_verified_count"] == 1
    assert artifact["web_search_claim_urls_verified_count"] == 1
    assert artifact["web_search_dropped_claim_url_count"] == 1
    assert artifact["packets"][0]["ai_review"]["web_search_event_verified"] is True
    assert artifact["packets"][0]["ai_review"]["web_search_claim_urls_verified"] is True
    assert artifact["packets"][0]["ai_review"]["web_search_dropped_claim_url_count"] == 1
    if review_mode == "codex_luna_web_review":
        assert artifact["research_source_urls_verified_count"] == 1
        assert artifact["packets"][0]["ai_review"]["source_verification_status"] == "pass"


def test_native_company_research_separates_search_event_from_financial_sources(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "model": "opencode-go/muse-spark-1.2-contributor",
        "effort": "xhigh",
        "ai_independent": True,
        "economic_category": "quality_equity",
        "score_components": {
            "risk_adjusted_expected_return": 60.0,
            "evidence_confidence": 83.3333333333,
        },
        "quantitative_facts": ["2025年度经营现金流 18 亿元"],
        "claims": [
            {
                "statement": "2025年度经营现金流 18 亿元",
                "source_ref": "https://example.test/financial-api/2025",
                "source_kind": "company_ir",
                "search_finding_id": "search-business",
                "support": "supports",
            },
            {
                "statement": "2025年度净现金流风险需要跟踪。",
                "source_ref": "https://example.test/financial-api/2025#risk",
                "source_kind": "company_ir",
                "fact_id": "cashflow-fact",
                "support": "contradicts",
            },
            {
                "statement": "2025年度报告披露当前估值与股价。",
                "source_ref": "https://example.test/financial-api/2025#valuation",
                "source_kind": "company_ir",
                "fact_id": "valuation-fact",
                "support": "context",
            },
        ],
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": False,
        "research_source_urls_verified": True,
        "web_search_queries": ["600000 浦发银行 最新经营情况"],
        "web_search_verified_claim_urls": [],
        "retrieval_backend": "reasonix-native-server-web-search",
        "retrieval_model": "opencode-go-muse/muse-spark-1.2-contributor",
        "retrieval_effort": "xhigh",
        "native_search_completed": True,
        "official_fetch_completed": False,
        "research_as_of": "2026-08-23",
        "search_findings": [
            {
                "id": "search-business",
                "query": "600000 浦发银行 最新经营情况",
                "title": "公司半年度报告",
                "url": "https://example.test/financial-api/2025",
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
            "business_model_source_quality": "current_primary",
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
                "source_ref": "https://example.test/financial-api/2025#valuation",
            },
            "multiple_basis": {
                "metric": "pb",
                "value": 0.8,
                "source_ref": "https://example.test/financial-api/2025#valuation",
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
    source = tmp_path / "company-research.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "opencode_native_company_research_review",
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": review,
                        }
                    ],
                }
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calibrated = tmp_path / "company-research-calibrated.json"
    calibrate(source, calibrated)
    calibrated_payload = json.loads(calibrated.read_text(encoding="utf-8"))
    calibrated_review = calibrated_payload["packets"][0]["ai_review"]
    assert calibrated_review["research_as_of"] == "2026-08-23"
    assert calibrated_review["economic_profile"] == review["economic_profile"]
    assert calibrated_review["valuation"] == review["valuation"]
    assert calibrated_review["ai_independent"] is True

    audit_path = tmp_path / "source-audit.json"
    _write_clean_source_audit(calibrated, audit_path)

    artifact = build_artifact(
        calibrated,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
        source_audit_path=audit_path,
    )

    assert artifact["full_coverage_web_search"] is True
    assert artifact["web_search_event_verified_count"] == 1
    assert artifact["web_search_claim_urls_verified_count"] == 0
    assert artifact["web_search_completed_count"] == 0
    assert artifact["research_source_urls_verified_count"] == 1
    assert artifact["type_pair_research_source_urls_verified_count"] == 1
    assert artifact["research_as_of"] == "2026-08-23"
    assert artifact["packets"][0]["ai_review"]["research_source_urls_verified"] is True
    assert artifact["packets"][0]["ai_review"]["web_search_verified"] is False
    assert artifact["packets"][0]["ai_review"]["research_as_of"] == "2026-08-23"
    assert artifact["packets"][0]["ai_review"]["economic_profile"]["business_model"].startswith("通过")
    assert artifact["packets"][0]["ai_review"]["economic_profile"]["business_model_source_quality"] == "current_primary"
    assert artifact["packets"][0]["ai_review"]["economic_profile"]["business_model_source_ids"] == ["search-business"]
    assert (
        artifact["packets"][0]["ai_review"]["economic_profile"]["business_model_sources"][0]["source_ref"]
        == "https://example.test/financial-api/2025"
    )


def test_native_company_research_publish_accepts_muse_and_deepseek_profiles(tmp_path) -> None:
    packets = [
        {
            "security_code": "600000",
            "name": "浦发银行",
            "type_key": "type1",
            "deterministic": {"status": "triggered", "score": 8.0},
            "ai_review": _native_company_review(
                code="600000",
                model="opencode-go/muse-spark-1.2-contributor",
                effort="xhigh",
                retrieval_model="opencode-go-muse/muse-spark-1.2-contributor",
            ),
        },
        {
            "security_code": "000001",
            "name": "平安银行",
            "type_key": "type7",
            "deterministic": {"status": "conditional", "score": 6.0},
            "ai_review": _native_company_review(
                code="000001",
                model="opencode-go/deepseek-v4-flash",
                effort="max",
                retrieval_model="opencode-go-deepseek-responses/deepseek-v4-flash",
            ),
        },
    ]
    source = tmp_path / "mixed-company-research.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 2,
                    "candidate_total": 2,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "opencode_native_company_research_review",
                    "packets": packets,
                }
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calibrated = tmp_path / "mixed-company-research-calibrated.json"
    calibrate(source, calibrated)
    audit_path = tmp_path / "source-audit.json"
    _write_clean_source_audit(calibrated, audit_path)

    artifact = build_artifact(
        calibrated,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
        source_audit_path=audit_path,
    )

    assert artifact["review_models"] == [
        "opencode-go/deepseek-v4-flash",
        "opencode-go/muse-spark-1.2-contributor",
    ]
    assert artifact["review_efforts"] == ["max", "xhigh"]
    assert {(packet["ai_review"]["model"], packet["ai_review"]["effort"]) for packet in artifact["packets"]} == {
        ("opencode-go/muse-spark-1.2-contributor", "xhigh"),
        ("opencode-go/deepseek-v4-flash", "max"),
    }
    assert artifact["packets"][0]["ai_review"]["valuation"]["as_of"] == "2026-08-21"
    assert artifact["packets"][0]["ai_review"]["valuation"]["current_price"] == 12.34
    assert artifact["packets"][0]["ai_review"]["valuation"]["method"] == "book_value_multiple"
    assert artifact["packets"][0]["ai_review"]["valuation_snapshot"]["snapshot_generation"] == "g1"
    assert artifact["packets"][0]["ai_review"]["valuation"]["scenarios"]["bear"] == {
        "value_per_share": 11.2,
        "upside_pct": -9.2382495948,
        "book_value_per_share": 20.0,
        "target_pb": 0.56,
    }


def test_native_company_research_not_found_is_searched_no_source_and_not_buy() -> None:
    review = _native_company_review(
        code="600000",
        model="opencode-go/muse-spark-1.2-contributor",
        effort="xhigh",
        retrieval_model="opencode-go-muse/muse-spark-1.2-contributor",
    )
    review["ai_action"] = "watchlist"
    review["final_category"] = "observe"
    review["economic_profile"]["business_model"] = "主营业务尚未核验，暂不能形成可靠业务判断。"
    review["economic_profile"]["business_model_source_ids"] = []
    review["economic_profile"]["business_model_source_quality"] = "not_found"
    review["economic_profile"]["business_model_uncertainty"] = (
        "已完成联网搜索但未找到可引用的一手资料，主营业务尚未核验。"
    )
    review["claims"][0]["source_ref"] = ""
    review["claims"][0]["source_context"] = "搜索事件完成，但未找到可引用来源。"
    review["search_findings"][0]["url"] = None
    review["search_findings"][0]["source_kind"] = "not_found"

    public = _public_review(review, claims_are_search_results=False)

    profile = public["economic_profile"]
    assert profile["business_model_source_ids"] == []
    assert profile["business_model_sources"] == []
    assert profile["business_model_source_status"] == "searched_no_source"
    assert public["ai_action"] == "watchlist"


def test_external_full_coverage_publish_requires_bound_source_audit(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "effort": "max",
        "web_search_performed": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": True,
        "web_search_queries": ["600000 浦发银行 最新报告"],
        "web_search_verified_claim_urls": [],
    }
    payload = _with_candidate_identity(
        {
            "snapshot_generation": "g1",
            "market_as_of": "2026-08-21",
            "candidate_offset": 0,
            "candidate_count": 1,
            "candidate_total": 1,
            "full_coverage_final_recommendation": True,
            "review_mode": "opencode_web_review",
            "packets": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "deterministic": {"status": "triggered", "score": 8.0},
                    "ai_review": review,
                }
            ],
        }
    )
    source = tmp_path / "full-external-no-audit.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="requires a bound source audit"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_local_full_coverage_rejects_external_model_label(tmp_path) -> None:
    review = {**_external_review("watchlist", 60), "model": "external-model", "effort": "max"}
    payload = _with_candidate_identity(
        {
            "snapshot_generation": "g1",
            "market_as_of": "2026-08-21",
            "candidate_offset": 0,
            "candidate_count": 1,
            "candidate_total": 1,
            "full_coverage_final_recommendation": True,
            "review_mode": "local_codex_review",
            "packets": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "deterministic": {"status": "triggered", "score": 8.0},
                    "ai_review": review,
                }
            ],
        }
    )
    source = tmp_path / "local-wrong-model.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="local Codex or OpenCode MAX review model"):
        build_artifact(
            source,
            tmp_path / "public.json",
            expected_generation="g1",
            expected_market_as_of="2026-08-21",
        )


def test_local_full_coverage_accepts_opencode_ox_max_model(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "effort": "max",
        "web_search_performed": False,
        "web_search_event_verified": False,
        "web_search_claim_urls_verified": False,
    }
    source = tmp_path / "local-ox-max.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "local_codex_review",
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": review,
                        }
                    ],
                }
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact = build_artifact(
        source,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
    )
    assert artifact["review_models"] == ["opencode-go/ox-alpha-free"]
    assert artifact["review_efforts"] == ["max"]
    assert artifact["full_coverage_web_search"] is False


def test_local_full_coverage_accepts_muse_spark_xhigh_model(tmp_path) -> None:
    review = {
        **_external_review("watchlist", 60),
        "model": "opencode-go/muse-spark-1.2-contributor",
        "effort": "xhigh",
        "web_search_performed": False,
        "web_search_event_verified": False,
        "web_search_claim_urls_verified": False,
    }
    source = tmp_path / "local-muse-xhigh.json"
    source.write_text(
        json.dumps(
            _with_candidate_identity(
                {
                    "snapshot_generation": "g1",
                    "market_as_of": "2026-08-21",
                    "candidate_offset": 0,
                    "candidate_count": 1,
                    "candidate_total": 1,
                    "full_coverage_final_recommendation": True,
                    "review_mode": "local_codex_review",
                    "packets": [
                        {
                            "security_code": "600000",
                            "name": "浦发银行",
                            "type_key": "type1",
                            "deterministic": {"status": "triggered", "score": 8.0},
                            "ai_review": review,
                        }
                    ],
                }
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact = build_artifact(
        source,
        tmp_path / "public.json",
        expected_generation="g1",
        expected_market_as_of="2026-08-21",
    )
    assert artifact["review_models"] == ["opencode-go/muse-spark-1.2-contributor"]
    assert artifact["review_efforts"] == ["xhigh"]
    assert artifact["full_coverage_web_search"] is False


def test_local_full_coverage_accepts_mixed_ox_and_muse_efforts(tmp_path) -> None:
    ox = {**_external_review("watchlist", 60), "effort": "max", "web_search_performed": False}
    muse = {
        **_external_review("watchlist", 61),
        "security_code": "600001",
        "model": "opencode-go/muse-spark-1.2-contributor",
        "effort": "xhigh",
        "web_search_performed": False,
    }
    payload = _with_candidate_identity(
        {
            "snapshot_generation": "g1",
            "market_as_of": "2026-08-21",
            "candidate_offset": 0,
            "candidate_count": 2,
            "candidate_total": 2,
            "full_coverage_final_recommendation": True,
            "review_mode": "local_codex_review",
            "packets": [
                {
                    "security_code": "600000",
                    "name": "浦发银行",
                    "type_key": "type1",
                    "deterministic": {"status": "triggered", "score": 8.0},
                    "ai_review": ox,
                },
                {
                    "security_code": "600001",
                    "name": "第二家公司",
                    "type_key": "type1",
                    "deterministic": {"status": "triggered", "score": 8.0},
                    "ai_review": muse,
                },
            ],
        }
    )
    source = tmp_path / "local-mixed.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    artifact = build_artifact(
        source, tmp_path / "public.json", expected_generation="g1", expected_market_as_of="2026-08-21"
    )
    assert artifact["review_models"] == ["opencode-go/muse-spark-1.2-contributor", "opencode-go/ox-alpha-free"]
    assert artifact["review_efforts"] == ["max", "xhigh"]


def test_opencode_review_mode_marks_only_the_complete_queue_as_full(tmp_path) -> None:
    snapshot = {
        "generation": "g1",
        "market_as_of": "2026-08-21",
        "companies": [
            {
                "code": "600000",
                "name": "浦发银行",
                "types": {
                    "type1": {"status": "triggered", "score": 8.0},
                    "type2": {"status": "conditional", "score": 6.8, "score_upper_bound": 7.2},
                },
            },
            {
                "code": "600001",
                "name": "邯郸钢铁",
                "types": {"type1": {"status": "triggered", "score": 7.6}},
            },
        ],
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "rules.md").write_text("# type1\ntype1 rules\n# type2\ntype2 rules", encoding="utf-8")

    full = build_input(snapshot_path, rules, tmp_path / "full", review_mode="opencode_web_review")
    assert full["queue_full_coverage"] is True
    assert full["full_coverage_final_recommendation"] is True

    partial = build_input(
        snapshot_path,
        rules,
        tmp_path / "partial",
        review_mode="opencode_web_review",
        limit=1,
    )
    assert partial["queue_full_coverage"] is False
    assert partial["full_coverage_final_recommendation"] is False
