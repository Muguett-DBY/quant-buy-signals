import hashlib
import io
import json
from copy import deepcopy
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from data.market_coldness import (
    MarketColdnessCoverage,
    MarketColdnessRecord,
    MarketColdnessSnapshot,
    MetricCoverage,
)
from engine.dcf import ReportingPeriodContract
from engine import buy_screener as bs
from engine import audit as audit_engine
from engine import quantitative_evidence as qe
from tools import run_full_audit


def test_audit_model_source_contract_pins_the_current_independent_patch6_and_patch7(monkeypatch):
    expected_patch6 = {
        "path_at_model_authoring": r"E:\模板汇总MD\补丁6· 公司三属性分类与三维度量化打分机制.md",
        "sha256": "dfade9961a182bfff67f95e2f8d55fd637cf8a15cedd44c12300b4f9c4c1549b",
    }
    expected_patch7 = {
        "path_at_model_authoring": (
            r"E:\模板汇总MD\补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md"
        ),
        "sha256": "69b6bbeaa44755b9935518c665bc1ac0cac5c473aaba5b106bdf0f9fc88beb6d",
    }

    assert audit_engine.AUDIT_SCHEMA_VERSION == 6
    assert audit_engine.TYPE7_SOURCE_DOCUMENTS["patch6"] == expected_patch6
    assert audit_engine.TYPE7_SOURCE_DOCUMENTS["patch7"] == expected_patch7
    assert set(audit_engine.TYPE7_SOURCE_DOCUMENTS) == {
        "template1",
        "template5",
        "patch5",
        "patch6",
        "patch7",
        "subsequent_addenda",
    }

    monkeypatch.setattr(
        audit_engine,
        "audit_state_hashes",
        lambda: {
            "code_sha256": "code",
            "rules_sha256": "rules",
            "industry_sha256": "industry",
            "dependency_manifest_sha256": "dependencies",
        },
    )
    monkeypatch.setattr(audit_engine, "_git_metadata", lambda: {"commit": None, "dirty": None})
    monkeypatch.setattr(audit_engine, "_runtime_versions", lambda: {})
    provenance = audit_engine._build_provenance(
        pd.DataFrame([{"code": "600519", "price": 1.0}]),
        {"600519": {}},
        ("600519",),
        snapshot_sha256=None,
        metadata=None,
    )

    assert provenance["audit_schema_version"] == 6
    assert provenance["patch6_source"] == expected_patch6
    assert provenance["patch7_source"] == expected_patch7
    assert provenance["type7_source_documents"]["patch7"] == expected_patch7


@pytest.fixture(autouse=True)
def _isolate_market_coldness_session_cache(monkeypatch):
    monkeypatch.setattr(run_full_audit, "load_market_coldness_session_snapshot", lambda _session: None)
    monkeypatch.setattr(
        run_full_audit,
        "archive_market_coldness_session_snapshot",
        lambda snapshot, _session: snapshot,
    )


def _reporting_period_contract_payload():
    return {
        "annual_report_date": "2025-12-31",
        "current_interim_report_date": "2026-03-31",
        "prior_interim_report_date": "2025-03-31",
        "period_basis": "FY_plus_current_YTD_minus_prior_YTD",
    }


@pytest.mark.parametrize(
    ("retrieved_at", "expected"),
    [
        ("2026-07-23T15:15:00+08:00", None),
        ("2026-07-23T15:30:00+08:00", None),
        ("2026-07-23T16:14:59+08:00", None),
        ("2026-07-23T16:15:00+08:00", date(2026, 7, 23)),
        ("2026-07-25T12:00:00+08:00", date(2026, 7, 24)),
    ],
)
def test_release_completed_session_independently_enforces_the_safe_close_boundary(retrieved_at, expected):
    assert run_full_audit._release_market_coldness_completed_session(datetime.fromisoformat(retrieved_at)) == expected


def _market_coldness_record(
    code: str,
    *,
    as_of_session: str = "2026-07-16",
    retrieved_at: str = "2026-07-16T08:20:00Z",
    raw_values=None,
    relative=None,
    source_updated_at=None,
):
    from datetime import date

    values = raw_values or {
        "change_60d_pct": -12.0,
        "change_ytd_pct": -8.0,
        "turnover_rate_pct": 1.0,
        "volume_ratio": 0.8,
    }
    relative = relative or {}
    available = [key for key in run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS if values[key] is not None]
    relative_context = {}
    for key in available:
        if key in relative:
            section_size = 1001
            equal_count = 1
            greater_count = round((relative[key] - 1.0) * (section_size - 1) / 8.0)
            lower_count = section_size - equal_count - greater_count
            relative_context[key] = {
                "section_size": section_size,
                "minimum_section_records": 1,
                "section_population": section_size,
                "source_present": section_size,
                "source_total": section_size,
                "lower_count": lower_count,
                "equal_count": equal_count,
            }
        else:
            relative_context[key] = {
                "section_size": 1,
                "minimum_section_records": 2,
                "section_population": 1,
                "source_present": 1,
                "source_total": 1,
                "lower_count": 0,
                "equal_count": 1,
            }
    absolute = {
        key: run_full_audit._market_coldness_interpolate(
            values[key], run_full_audit._MARKET_COLDNESS_ABSOLUTE_BANDS[key]
        )
        for key in available
    }
    metric_scores = {
        key: 0.8 * absolute[key] + 0.2 * relative[key] if key in relative else absolute[key] for key in available
    }
    session = date.fromisoformat(as_of_session)
    reliability = min(1.0, run_full_audit._market_coldness_business_days_ytd(session) / 60.0)
    weights = dict(run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS)
    weights["change_ytd_pct"] *= reliability
    raw_score = sum(metric_scores[key] * weights[key] for key in available) / sum(weights[key] for key in available)
    price_score = (
        metric_scores["change_60d_pct"] * weights["change_60d_pct"]
        + metric_scores["change_ytd_pct"] * weights["change_ytd_pct"]
    ) / (weights["change_60d_pct"] + weights["change_ytd_pct"])
    cap = 8.0 if values["volume_ratio"] is not None else 7.5
    caps = [f"evidence_cap={cap:.1f}"]
    if absolute["change_60d_pct"] <= 3.0:
        cap = min(cap, 3.0)
        caps.append("60d_hot_cap=3.0")
    elif price_score < 5.0:
        cap = min(cap, 4.9)
        caps.append("price_coldness_lt5_cap=4.9")
    elif price_score < 6.0:
        cap = min(cap, 6.9)
        caps.append("price_coldness_lt6_cap=6.9")
    score = round(max(1.0, min(cap, raw_score)), 1)
    volume_text = f"{values['volume_ratio']:.2f}" if values["volume_ratio"] is not None else "缺失"
    summary = (
        f"量价冷度;60日{values['change_60d_pct']:.1f}%;YTD{values['change_ytd_pct']:.1f}%;"
        f"换手{values['turnover_rate_pct']:.2f}%;量比{volume_text};上限{cap:.1f}"
    )
    return {
        "market_coldness_score": score,
        "market_coldness_score_evidence_level": "derived_proxy",
        "market_coldness_score_evidence": {
            "source": f"{run_full_audit.EASTMONEY_SOURCE}; {run_full_audit.EASTMONEY_CLIST_ENDPOINT}",
            "evidence_id": f"patch6-type2c-quantity-price-v1:{code}:{as_of_session.replace('-', '')}",
            "as_of": as_of_session,
            "summary": summary,
        },
        "components": {
            "schema_version": run_full_audit.MARKET_COLDNESS_EVIDENCE_SCHEMA_VERSION,
            "model_id": run_full_audit.MARKET_COLDNESS_MODEL_ID,
            "code": code,
            "source": run_full_audit.EASTMONEY_SOURCE,
            "raw_values": values,
            "absolute": {key: round(value, 6) for key, value in absolute.items()},
            "relative": {key: round(value, 6) for key, value in relative.items()},
            "relative_sample_sizes": {key: relative_context[key]["section_size"] for key in relative},
            "relative_context": relative_context,
            "metric_scores": {key: round(value, 6) for key, value in metric_scores.items()},
            "weights": {key: round(weights[key], 6) for key in available},
            "ytd_reliability": round(reliability, 6),
            "price_score": round(price_score, 6),
            "raw_score": round(raw_score, 6),
            "score_cap": cap,
            "caps": caps,
            "board": run_full_audit._market_coldness_board(code),
            "as_of_session": as_of_session,
            "source_url": run_full_audit.EASTMONEY_CLIST_ENDPOINT,
            "retrieved_at": retrieved_at,
            "source_updated_at": source_updated_at or f"{as_of_session}T07:34:00Z",
        },
    }


def _market_coldness_status(
    eligible_codes,
    evidence_codes=None,
    *,
    as_of_session="2026-07-16",
    retrieved_at="2026-07-16T08:20:00Z",
    not_applicable=None,
    data_gaps=None,
):
    eligible = set(eligible_codes)
    evidence = set(evidence_codes if evidence_codes is not None else eligible_codes)
    not_applicable = not_applicable or {}
    data_gaps = data_gaps or {}
    na_ledger = {
        reason: sorted(not_applicable.get(reason, ()))
        for reason in sorted(run_full_audit._MARKET_COLDNESS_NOT_APPLICABLE_REASONS)
    }
    gap_ledger = {reason: sorted(codes) for reason, codes in sorted(data_gaps.items())}
    na_codes = set().union(*(set(codes) for codes in na_ledger.values()))
    gap_codes = set().union(*(set(codes) for codes in gap_ledger.values())) if gap_ledger else set()
    applicable = eligible - na_codes
    return {
        "available": True,
        "evidence_available": bool(evidence),
        "evidence_reason": "available" if not gap_codes else "eligible_data_gaps",
        "model_id": run_full_audit.MARKET_COLDNESS_MODEL_ID,
        "source": run_full_audit.EASTMONEY_SOURCE,
        "source_url": run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        "retrieved_at": retrieved_at,
        "as_of_session": as_of_session,
        "eligible_evidence_count": len(evidence),
        "eligible_evidence_coverage": len(evidence) / len(eligible),
        "eligible_applicable_count": len(applicable),
        "eligible_applicable_evidence_coverage": len(evidence) / len(applicable) if applicable else 0.0,
        "eligible_not_applicable_count": len(na_codes),
        "eligible_not_applicable_codes_by_reason": na_ledger,
        "eligible_unscored_data_gap_count": len(gap_codes),
        "eligible_unscored_data_gap_codes_by_reason": gap_ledger,
    }


def _market_coldness_reference_artifact(
    listed_codes,
    *,
    as_of_session="2026-07-16",
    retrieved_at="2026-07-16T08:20:00Z",
    current_year=(),
    recent=(),
    zero_turnover=(),
    raw_values=None,
    source_updated_at=None,
):
    complete_listed = set(listed_codes)
    candidate = 600000
    while len(complete_listed) < run_full_audit.MIN_CROSS_SECTION_RECORDS + 100:
        complete_listed.add(f"{candidate:06d}")
        candidate += 1
    values = raw_values or {
        "change_60d_pct": -12.0,
        "change_ytd_pct": -8.0,
        "turnover_rate_pct": 1.0,
        "volume_ratio": 0.8,
    }
    rows = []
    source_update_epoch = int(
        datetime.fromisoformat((source_updated_at or f"{as_of_session}T07:34:00Z").replace("Z", "+00:00")).timestamp()
    )
    for code in sorted(complete_listed):
        listing_date = (
            "2026-01-02" if code in set(current_year) else "2026-04-01" if code in set(recent) else "2000-01-01"
        )
        turnover = 0.0 if code in set(zero_turnover) else values["turnover_rate_pct"]
        rows.append(
            [
                code,
                listing_date,
                values["change_60d_pct"],
                values["change_ytd_pct"],
                turnover,
                values["volume_ratio"],
                source_update_epoch,
            ]
        )
    return {
        "schema_version": run_full_audit._MARKET_COLDNESS_REFERENCE_ARTIFACT_SCHEMA_VERSION,
        "model_id": run_full_audit.MARKET_COLDNESS_MODEL_ID,
        "source": run_full_audit.EASTMONEY_SOURCE,
        "source_url": run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        "retrieved_at": retrieved_at,
        "as_of_session": as_of_session,
        "listed_codes": sorted(complete_listed),
        "source_record_count": len(rows),
        "records": rows,
    }


def _status_from_reference_artifact(artifact, eligible_codes):
    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible_codes,
        as_of_session=artifact["as_of_session"],
    )
    evidence = replay["eligible_evidence"]
    status = _market_coldness_status(
        eligible_codes,
        evidence,
        as_of_session=artifact["as_of_session"],
        retrieved_at=artifact["retrieved_at"],
        not_applicable=replay["eligible_not_applicable_codes_by_reason"],
        data_gaps=replay["eligible_unscored_data_gap_codes_by_reason"],
    )
    status["full_listed_evidence_count"] = len(replay["full_evidence"])
    status["reference_artifact_sha256"] = hashlib.sha256(
        run_full_audit._canonical_market_coldness_json(artifact)
    ).hexdigest()
    return evidence, status


def _builder_diagnostics(listed_codes, evidence_codes, *, not_applicable=None, data_gaps=None):
    not_applicable = not_applicable or {}
    data_gaps = data_gaps or {}
    reasons = {}
    for reason in (
        "listing_history_lt_120_days",
        "listed_in_current_year",
        "missing_listing_date",
        "missing_required_metric",
        "missing_source_record",
        "insufficient_reference_cross_section",
    ):
        codes = sorted(
            (not_applicable if reason in run_full_audit._MARKET_COLDNESS_NOT_APPLICABLE_REASONS else data_gaps).get(
                reason, ()
            )
        )
        reasons[reason] = {
            "classification": (
                "model_not_applicable"
                if reason in run_full_audit._MARKET_COLDNESS_NOT_APPLICABLE_REASONS
                else "data_missing"
            ),
            "count": len(codes),
            "codes": codes,
        }
    return {
        "evidence_available": bool(evidence_codes),
        "evidence_reason": "available" if evidence_codes else "no_eligible_records",
        "diagnostics_schema_version": run_full_audit.MARKET_COLDNESS_DIAGNOSTICS_SCHEMA_VERSION,
        "eligible_candidate_count": len(tuple(listed_codes)),
        "evidence_count": len(tuple(evidence_codes)),
        "unscored_codes_by_reason": reasons,
    }


def test_comparison_quality_prefers_candidate_baseline_then_active_generation():
    snapshot = SimpleNamespace(
        previous_analysis_quality={"score_rows": 90},
        analysis_quality={"score_rows": 100},
    )
    assert run_full_audit._comparison_quality(snapshot) == {"score_rows": 90}

    snapshot.previous_analysis_quality = {}
    assert run_full_audit._comparison_quality(snapshot) == {"score_rows": 100}

    snapshot.analysis_quality = "invalid"
    assert run_full_audit._comparison_quality(snapshot) is None


def test_requested_refresh_is_successful_only_for_a_network_candidate():
    assert run_full_audit._refresh_completed(False, "cache") is True
    assert run_full_audit._refresh_completed(True, "network") is True
    assert run_full_audit._refresh_completed(True, "cache") is False
    assert run_full_audit._refresh_completed(True, "stale_cache") is False


def _quantitative_record(
    key: str,
    level: str = "derived_proxy",
    *,
    code: str = "000001",
    missing_inputs: list[str] | None = None,
):
    # Reuse a record produced by the real formula engine instead of maintaining
    # a hand-written ledger that can silently drift whenever replay contracts
    # gain a new raw input.  The release tests own the canonical complete
    # full-market fixture and build it through ``derive_company_evidence``.
    from tests.test_release_zip import _quantitative_evidence_fixture

    payload = deepcopy(_quantitative_evidence_fixture(code)[key])
    if level == "derived_proxy":
        return payload
    if level == "not_applicable":
        return qe.derive_company_evidence(
            {
                "code": code,
                "industry": "BANK",
                "financial_indicator_as_of": "2025-12-31",
            },
            {},
        )[key]
    quality = payload["details"]["evidence_quality"]
    requested_missing = list(missing_inputs or ["missing_input"])
    if level == "partial":
        available = list(quality["available_inputs"])
        required = [*available, *requested_missing]
    elif level == "missing":
        available = []
        required = requested_missing
    elif level == "primary":
        payload["evidence_level"] = "primary"
        payload["evidence"]["source"] = "一手来源测试夹具"
        payload["evidence"]["evidence_id"] = (
            f"primary:{key}:{code}:{payload['evidence']['as_of'].replace('-', '')}:sha256:{'a' * 64}"
        )
        payload["details"] = {
            "basis": "dated_primary_source_score",
            "adapter_contract": "trusted-primary-adapter-v1",
            "source_summary": "经一手来源复核的量化分数",
            "source_evidence_id": payload["evidence"]["evidence_id"],
            "source_binding_sha256": "a" * 64,
            "evidence_quality": {
                "level": "primary",
                "input_coverage": 1.0,
                "required_inputs": ["primary_source_score"],
                "available_inputs": ["primary_source_score"],
                "missing_inputs": [],
            },
        }
        payload["evidence"]["summary"] = f"{key}={payload['score']:.1f};model={qe.MODEL_ID};evidence_level=primary"
        return payload
    else:
        raise ValueError(f"unsupported test evidence level: {level}")
    payload["evidence_level"] = level
    payload["details"]["evidence_quality"] = {
        "level": level,
        "input_coverage": round(len(available) / len(required), 3),
        "required_inputs": required,
        "available_inputs": available,
        "missing_inputs": requested_missing,
    }
    payload["evidence"]["summary"] = f"{key}={payload['score']:.1f};model={qe.MODEL_ID};evidence_level={level}"
    return payload


def _quantitative_evidence(level: str = "derived_proxy", *, code: str = "000001"):
    return {key: _quantitative_record(key, level, code=code) for key in run_full_audit._QUANTITATIVE_EVIDENCE_KEYS}


def _quantitative_attachment_fields(evidence):
    fields = {}
    for key, payload in evidence.items():
        if not isinstance(payload, Mapping):
            continue
        level = payload.get("evidence_level")
        fields[f"{key}_evidence_level"] = level
        if level in {"primary", "derived_proxy"}:
            fields[key] = payload.get("score")
            fields[f"{key}_evidence"] = payload.get("evidence")
    return fields


def _with_decision(type_key, payload):
    payload = deepcopy(payload)
    reasons = payload.setdefault("reasons", {})
    reasons.setdefault("_status", payload["status"])
    reasons.setdefault("_applicable", "yes" if payload["applicable"] else "no")
    reasons.setdefault("_evidence", "complete" if payload["evidence_complete"] else "incomplete")
    reasons.setdefault(
        "_decision_missing_dimensions",
        []
        if payload["evidence_complete"] or not payload["applicable"]
        else list(run_full_audit.TYPE_WEIGHTS[type_key]),
    )
    payload.setdefault("veto", bool(reasons.get("_veto")))
    payload.setdefault(
        "decision_market_context",
        {"tradable": True, "reference_price": False, "risk_status": ""},
    )
    payload["decision"] = bs.replay_buy_decision(type_key, payload)
    return payload


def _outcome_payload(type_key, outcome, *, ledger=None):
    triggered, total, sub_scores, reasons = outcome
    status = reasons["_status"]
    payload = {
        "triggered": triggered,
        "total": total,
        "sub_scores": sub_scores,
        "reasons": reasons,
        "veto": bool(reasons.get("_veto")),
        "status": status,
        "applicable": status != "not_applicable",
        "evidence_complete": reasons.get("_evidence") == "complete",
    }
    if ledger is not None:
        payload["ledger"] = ledger
    payload["decision_market_context"] = {
        "tradable": True,
        "reference_price": False,
        "risk_status": "",
    }
    payload["decision"] = bs.replay_buy_decision(type_key, payload)
    return payload


def _patch7_pending_payload():
    outcomes = {type_key: bs._not_applicable(type_key, "补丁7审计夹具不适用") for type_key in bs.TYPE_WEIGHTS}
    scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type3"]}
    outcomes["type3"] = bs._finish(
        "type3",
        scores,
        {key: "补丁7前已核验分项" for key in scores},
    )
    gated = bs._apply_patch7_total_gate(
        outcomes,
        {"price": 25.0},
        {"zone": "观察区", "bubble_warning": False, "dcf_points": {"optimistic": {}}},
        {"ALL": {}},
    )
    return _outcome_payload("type3", gated["type3"])


def _patch7_fact_context(*, price=25.0, optimistic_upper=None):
    company = {"price": price}
    optimistic = {} if optimistic_upper is None else {"upper": optimistic_upper}
    dcf_result = {"dcf_points": {"optimistic": optimistic}}
    return company, dcf_result


def _patch7_future_fcf_pending_payload():
    outcomes = {type_key: bs._not_applicable(type_key, "补丁7审计夹具不适用") for type_key in bs.TYPE_WEIGHTS}
    scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type3"]}
    outcomes["type3"] = bs._finish(
        "type3",
        scores,
        {key: "补丁7前已核验分项" for key in scores},
    )
    ledger = {"decision_gates": {"future_fcf": {"complete": False}}}
    gated = bs._apply_patch7_total_gate(
        outcomes,
        {"price": 25.0},
        {"zone": "观察区", "bubble_warning": False, "dcf_points": {"optimistic": {"upper": 30.0}}},
        {"ALL": {}},
        type7_ledger=ledger,
    )
    return _outcome_payload("type3", gated["type3"]), ledger


def _type6_proxy_and_position_gap_payload():
    scores = {"6a": 8.0, "6b": 4.0, "6c": 4.0, "6d": 8.0, "6e": 10.0}
    reasons = {
        **{key: "可复算公司证据" for key in scores},
        "_condition": "须确认实际仓位符合建议上限",
        "_status": bs.STATUS_INSUFFICIENT_EVIDENCE,
        "_applicable": "yes",
        "_evidence": "incomplete",
        "_decision_missing_dimensions": ["6b", "6c"],
    }
    return _with_decision(
        "type6",
        {
            "triggered": False,
            "total": 6.9,
            "sub_scores": scores,
            "reasons": reasons,
            "status": bs.STATUS_INSUFFICIENT_EVIDENCE,
            "applicable": True,
            "evidence_complete": False,
        },
    )


def _complete_framework_row(*, quantitative_evidence=...):
    row = {
        "code": "000001",
        "primary_type": "type1",
        "num_types": 1,
    }
    if quantitative_evidence is not ...:
        row["quantitative_evidence"] = quantitative_evidence
        if isinstance(quantitative_evidence, Mapping):
            levels = {
                key: payload.get("evidence_level")
                for key, payload in quantitative_evidence.items()
                if isinstance(payload, Mapping)
            }
            row.update(_quantitative_attachment_fields(quantitative_evidence))
            row["quantitative_evidence_levels"] = levels
            row["quantitative_evidence_status"] = (
                "complete"
                if levels and all(level in {"primary", "derived_proxy", "not_applicable"} for level in levels.values())
                else "missing"
                if levels and all(level == "missing" for level in levels.values())
                else "partial"
            )
    for type_key, dimensions in run_full_audit.TYPE_WEIGHTS.items():
        framework_score = 7.0 if type_key == "type1" else 6.0
        payload = {
            "status": "triggered" if type_key == "type1" else "not_triggered",
            "triggered": type_key == "type1",
            "veto": False,
            "applicable": True,
            "evidence_complete": True,
            "sub_scores": {dimension: framework_score for dimension in dimensions},
            "reasons": {},
        }
        if type_key == "type7":
            payload["ledger"] = {
                "model_id": "patch6-type7-classified-equity-v2",
                "classification": {"class_code": "W", "route_complete": True},
                "dimensions": {},
                "upper_bound": framework_score,
                "condition_failures": [],
                "veto": False,
            }
        row[type_key] = _with_decision(type_key, payload)
    return row


def _type5_cycle_contract(*, code="000001", as_of="2026-07-15", industry="COAL"):
    symbol = "JM0"
    return {
        "schema_version": 1,
        "model_id": "type5-cycle-attributes-v1",
        "code": code,
        "as_of": as_of,
        "industry": industry,
        "route": "industry_commodity_proxy",
        "commodity_proxy": {
            "model_id": "commodity-cycle-sina-v2",
            "symbol": symbol,
            "evidence_id": f"commodity-cycle-sina-v2:{symbol}:{code}:{as_of.replace('-', '')}",
            "as_of": as_of,
            "source_url": (
                "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
                f"InnerFuturesNewService.getDailyKLine?symbol={symbol}"
            ),
            "source_sha256": "a" * 64,
        },
        "company_cycle": {
            "gross_margin_history": [0.42, 0.22, 0.31, 0.18],
            "gross_margin_years": [2022, 2023, 2024, 2025],
            "net_profit_history": [100.0, 20.0, 80.0, 30.0],
            "net_profit_years": [2022, 2023, 2024, 2025],
        },
    }


def _valuation_history_contract(*, end_date="2026-07-17", include_pe=False):
    start = date(2021, 7, 28)
    end = date.fromisoformat(end_date)
    return {
        "available": True,
        "window_years": 5,
        "target_start_date": "2021-07-28",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "row_count": 801,
        "span_days": (end - start).days,
        "start_delay_days": 0,
        "limited_history": False,
        "pe_observations": 800,
        "pb_observations": 800,
        "current_pe_ttm": 10.0 if include_pe else None,
        "median_pe_ttm": 12.0 if include_pe else None,
        "pe_percentile": 0.25 if include_pe else None,
        "current_pb_mrq": 1.2,
        "median_pb_mrq": 1.0,
        "pb_percentile": 0.75,
        "pe_distribution": {"values": [8.0, 12.0, 20.0], "counts": [200, 400, 200]},
        "pb_distribution": {"values": [0.8, 1.0, 1.5], "counts": [200, 400, 200]},
        "formula": "percentile=(count(x<current)+0.5*count(x=current))/historical_count",
    }


def test_analysis_coverage_summary_counts_triggers_statuses_and_evidence_levels():
    first_evidence = _quantitative_evidence()
    first_evidence["technology_score"] = _quantitative_record(
        "technology_score",
        "partial",
        missing_inputs=["patent_quality"],
    )
    second_evidence = _quantitative_evidence(code="000002")
    second_evidence["moat_score"] = _quantitative_record(
        "moat_score",
        "missing",
        code="000002",
        missing_inputs=["gross_margin_history"],
    )
    scores = pd.DataFrame(
        [
            {
                "code": "000001",
                "primary_type": "type2",
                "num_types": 2,
                "type1": {
                    "status": "triggered",
                    "triggered": True,
                    "applicable": True,
                    "evidence_complete": True,
                    "sub_scores": {key: 8.0 for key in run_full_audit.TYPE_WEIGHTS["type1"]},
                    "reasons": {},
                },
                "type2": {
                    "status": "triggered",
                    "triggered": True,
                    "applicable": True,
                    "evidence_complete": False,
                    "sub_scores": {key: 8.0 for key in run_full_audit.TYPE_WEIGHTS["type2"]},
                    "reasons": {"_missing": "缺独立估值证据"},
                },
                "quantitative_evidence": first_evidence,
                "quantitative_evidence_levels": {
                    key: payload["evidence_level"] for key, payload in first_evidence.items()
                },
                "quantitative_evidence_status": "partial",
                **_quantitative_attachment_fields(first_evidence),
            },
            {
                "code": "000002",
                "primary_type": None,
                "num_types": 0,
                "type1": {
                    "status": "not_applicable",
                    "triggered": False,
                    "applicable": False,
                    "evidence_complete": True,
                    "sub_scores": {key: 0.0 for key in run_full_audit.TYPE_WEIGHTS["type1"]},
                    "reasons": {},
                },
                "type2": {
                    "status": "vetoed",
                    "triggered": False,
                    "applicable": True,
                    "evidence_complete": False,
                    "sub_scores": {key: 2.0 for key in run_full_audit.TYPE_WEIGHTS["type2"]},
                    "reasons": {},
                },
                "quantitative_evidence": second_evidence,
                "quantitative_evidence_levels": {
                    key: payload["evidence_level"] for key, payload in second_evidence.items()
                },
                "quantitative_evidence_status": "partial",
                **_quantitative_attachment_fields(second_evidence),
            },
        ]
    )
    for index in scores.index:
        for type_key in ("type1", "type2"):
            scores.at[index, type_key] = _with_decision(type_key, scores.at[index, type_key])

    summary = run_full_audit._analysis_coverage_summary(scores)

    assert summary["candidate_companies"] == 1
    assert summary["total_framework_triggers"] == 2
    assert summary["framework_trigger_counts"]["type1"] == 1
    assert summary["framework_trigger_counts"]["type2"] == 1
    assert summary["primary_trigger_counts"] == {"type2": 1}
    assert summary["framework_status_counts"]["type1"] == {
        "not_applicable": 1,
        "triggered": 1,
    }
    assert summary["quantitative_evidence_level_counts"] == {
        "derived_proxy": 24,
        "missing": 1,
        "partial": 1,
    }
    assert summary["quantitative_metric_level_counts"]["technology_score"] == {
        "derived_proxy": 1,
        "partial": 1,
    }
    assert summary["quantitative_metric_level_counts"]["moat_score"] == {
        "derived_proxy": 1,
        "missing": 1,
    }
    assert summary["quantitative_missing_input_counts"]["technology_score"] == {"patent_quality": 1}
    assert summary["quantitative_missing_input_counts"]["moat_score"] == {"gross_margin_history": 1}
    assert summary["framework_evidence_contract"]["type1"]["valid_sub_scores"] == 2
    assert summary["framework_evidence_contract"]["type1"]["evidence_complete"] == 2
    assert summary["framework_evidence_contract"]["type2"]["applicable_evidence_complete"] == 0
    assert summary["framework_evidence_contract"]["type2"]["applicable_evidence_incomplete"] == 2
    assert summary["framework_evidence_contract"]["type2"]["incomplete_with_reason"] == 1
    assert summary["framework_evidence_contract"]["type2"]["incomplete_without_reason"] == 1
    assert summary["framework_evidence_contract"]["type2"]["incomplete_without_reason_examples"] == ["000002"]
    assert summary["quantitative_evidence_gap_examples"] == [
        {
            "code": "000001",
            "metric": "technology_score",
            "level": "partial",
            "missing_inputs": ["patent_quality"],
        },
        {
            "code": "000002",
            "metric": "moat_score",
            "level": "missing",
            "missing_inputs": ["gross_margin_history"],
        },
    ]
    assert summary["goal_readiness"] == {
        "all_framework_payloads_present": False,
        "all_sub_scores_valid": False,
        "all_applicable_frameworks_evidence_complete": False,
        "all_incomplete_frameworks_explained": False,
        "all_quantitative_evidence_records_valid": True,
        "no_missing_quantitative_evidence": False,
        "no_partial_quantitative_evidence": False,
        "all_decision_contracts_valid": False,
        "all_potential_candidates_visible": False,
        "all_candidate_recall_paths_safe": False,
        "artifact_integrity_ready": False,
        "candidate_visibility_ready": False,
        "candidate_recall_ready": False,
        "ideal_zero_gap_ready": False,
        "ready": False,
    }


def test_analysis_coverage_summary_proves_a_complete_seven_framework_contract():
    row = _complete_framework_row(quantitative_evidence=_quantitative_evidence())

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["goal_readiness"] == {
        "all_framework_payloads_present": True,
        "all_sub_scores_valid": True,
        "all_applicable_frameworks_evidence_complete": True,
        "all_incomplete_frameworks_explained": True,
        "all_quantitative_evidence_records_valid": True,
        "no_missing_quantitative_evidence": True,
        "no_partial_quantitative_evidence": True,
        "all_decision_contracts_valid": True,
        "all_potential_candidates_visible": True,
        "all_candidate_recall_paths_safe": True,
        "artifact_integrity_ready": True,
        "candidate_visibility_ready": True,
        "candidate_recall_ready": True,
        "ideal_zero_gap_ready": True,
        "ready": True,
    }
    assert summary["quantitative_evidence_contract"]["valid_rows"] == 1
    assert summary["quantitative_evidence_contract"]["invalid_rows"] == 0
    assert summary["quantitative_evidence_contract"]["expected_metrics_per_row"] == 13


def test_independent_audit_replays_type5_cycle_identity_and_company_corroboration():
    row = {
        "code": "000001",
        "source_trade_date": "2026-07-15",
        "industry": "COAL",
    }
    payload = {
        "status": "not_triggered",
        "sub_scores": {"5a": 7.0},
        "reasons": {"5a": "行业商品行情/公司毛利率/利润周期互证"},
        "cycle_evidence_mode": "automatic_replay",
        "cycle_evidence_contract": _type5_cycle_contract(),
    }

    assert audit_engine._audit_type5_cycle_evidence_errors("000001", row, payload) == []

    mutations = {
        "endpoint": lambda value: value["commodity_proxy"].update(source_url="https://example.test"),
        "identity": lambda value: value.update(code="000002"),
        "year_gap": lambda value: value["company_cycle"].update(gross_margin_years=[2021, 2023, 2024, 2025]),
        "no_profit_cycle": lambda value: value["company_cycle"].update(net_profit_history=[10.0, 20.0, 30.0, 40.0]),
    }
    for label, mutate in mutations.items():
        forged = deepcopy(payload)
        mutate(forged["cycle_evidence_contract"])
        errors = audit_engine._audit_type5_cycle_evidence_errors("000001", row, forged)
        assert errors == ["000001:type5:automatic cycle evidence replay mismatch"], label

    unverified = deepcopy(payload)
    unverified["cycle_evidence_mode"] = "trusted_external"
    unverified.pop("cycle_evidence_contract")
    assert audit_engine._audit_type5_cycle_evidence_errors("000001", row, unverified) == [
        "000001:type5:trusted external cycle evidence is not independently replayable"
    ]

    incomplete = deepcopy(payload)
    incomplete["cycle_evidence_mode"] = "incomplete"
    incomplete.pop("cycle_evidence_contract")
    assert audit_engine._audit_type5_cycle_evidence_errors("000001", row, incomplete) == [
        "000001:type5:unverified cycle evidence carries a nonzero score"
    ]


def test_independent_audit_accepts_stale_pb_only_but_never_stale_pe_ttm():
    as_of = date(2026, 7, 28)

    pb_only = audit_engine._audit_type7_valuation_history_replay(
        _valuation_history_contract(),
        as_of,
    )
    assert pb_only is not None
    assert set(pb_only) == {"pb"}

    assert (
        audit_engine._audit_type7_valuation_history_replay(
            _valuation_history_contract(include_pe=True),
            as_of,
        )
        is None
    )

    same_session = audit_engine._audit_type7_valuation_history_replay(
        _valuation_history_contract(end_date=as_of.isoformat(), include_pe=True),
        as_of,
    )
    assert same_session is not None
    assert set(same_session) == {"pe", "pb"}


def test_independent_decision_replay_rejects_tampering_and_preserves_type3_theoretical_range():
    scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type3"]}
    payload = _outcome_payload(
        "type3",
        bs._finish(
            "type3",
            scores,
            {key: "部分可复算证据" for key in scores},
            evidence_complete=False,
            missing_dimensions=["3b"],
        ),
    )

    replayed = run_full_audit._independent_decision_replay("type3", payload)
    assert replayed is not None
    assert replayed["score_lower_bound"] == 6.4
    assert replayed["score_upper_bound"] == 8.4
    assert replayed["decision_basis"] == "unresolved_missing_evidence"

    payload["decision"]["score_upper_bound"] = 6.0
    assert run_full_audit._independent_decision_replay("type3", payload) is None


def test_independent_decision_replay_preserves_patch7_non_score_requirement_ledger():
    payload = _patch7_pending_payload()
    company, dcf_result = _patch7_fact_context()

    assert payload["reasons"]["_decision_missing_requirements"] == ["patch7_optimistic_upper"]
    assert payload["decision"]["missing_dimensions"] == []
    assert payload["decision"]["score_lower_bound"] == 8.0
    assert payload["decision"]["score_upper_bound"] == 8.0

    replayed = run_full_audit._independent_decision_replay(
        "type3",
        payload,
        company=company,
        dcf_result=dcf_result,
    )
    assert replayed is not None
    assert {key: replayed[key] for key in payload["decision"]} == payload["decision"]
    assert replayed["visible"] is True
    assert replayed["recall_safe"] is True

    row = _complete_framework_row(quantitative_evidence=_quantitative_evidence())
    row.update(company)
    row["type3"] = payload
    summary = run_full_audit._analysis_coverage_summary(
        pd.DataFrame([row]),
        {row["code"]: dcf_result},
    )
    contract = summary["framework_evidence_contract"]["type3"]
    assert contract["valid_decision"] == 1
    assert contract["invalid_decision"] == 0

    forged_summary = run_full_audit._analysis_coverage_summary(
        pd.DataFrame([row]),
        {row["code"]: {"dcf_points": {"optimistic": {"upper": 30.0}}}},
    )
    forged_contract = forged_summary["framework_evidence_contract"]["type3"]
    assert forged_contract["valid_decision"] == 0
    assert forged_contract["invalid_decision"] == 1

    markerless_row = deepcopy(row)
    markerless_row["type3"]["reasons"].pop("_decision_missing_requirements")
    markerless_summary = run_full_audit._analysis_coverage_summary(
        pd.DataFrame([markerless_row]),
        {row["code"]: dcf_result},
    )
    markerless_contract = markerless_summary["framework_evidence_contract"]["type3"]
    assert markerless_contract["valid_decision"] == 0
    assert markerless_contract["invalid_decision"] == 1


def test_independent_decision_replay_rejects_patch7_requirements_that_disagree_with_outer_facts():
    payload = _patch7_pending_payload()

    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company={"price": 25.0},
            dcf_result={"dcf_points": {"optimistic": {"upper": 30.0}}},
        )
        is None
    )

    payload = _patch7_pending_payload()
    payload["reasons"].pop("_decision_missing_requirements")
    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company={"price": 25.0},
            dcf_result={"dcf_points": {"optimistic": {}}},
        )
        is None
    )
    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company={"price": None},
            dcf_result={"dcf_points": {"optimistic": {}}},
        )
        is None
    )

    payload["reasons"]["_decision_missing_requirements"] = ["patch7_current_price"]
    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company={"price": 25.0},
            dcf_result={"dcf_points": {"optimistic": {"upper": 30.0}}},
        )
        is None
    )


def test_independent_decision_replay_binds_the_shared_future_fcf_requirement():
    payload, ledger = _patch7_future_fcf_pending_payload()
    company = {"price": 25.0, "type7": {"ledger": ledger}}
    dcf_result = {"dcf_points": {"optimistic": {"upper": 30.0}}}

    assert payload["reasons"]["_decision_missing_requirements"] == ["patch7_future_fcf"]
    assert run_full_audit._independent_decision_replay(
        "type3",
        payload,
        company=company,
        dcf_result=dcf_result,
    )

    resolved = deepcopy(company)
    resolved["type7"]["ledger"]["decision_gates"]["future_fcf"] = {
        "complete": True,
        "passed": True,
    }
    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company=resolved,
            dcf_result=dcf_result,
        )
        is None
    )


def test_independent_decision_replay_binds_a_patch7_future_fcf_veto():
    scores = {dimension: 8.0 for dimension in bs.TYPE_WEIGHTS["type3"]}
    reasons = {
        **{dimension: "补丁7前已核验分项" for dimension in scores},
        "_veto": "补丁7未来自由现金流前置条件未通过",
        "_decision_patch7_veto": "future_fcf",
        "_status": "vetoed",
        "_applicable": "yes",
        "_evidence": "complete",
        "_decision_missing_dimensions": [],
    }
    payload = {
        "triggered": False,
        "total": 8.0,
        "sub_scores": scores,
        "reasons": reasons,
        "veto": True,
        "status": "vetoed",
        "applicable": True,
        "evidence_complete": True,
        "decision_market_context": {"tradable": True, "reference_price": False, "risk_status": ""},
    }
    payload["decision"] = bs.replay_buy_decision("type3", payload)
    company = {
        "price": 10.0,
        "type7": {"ledger": {"decision_gates": {"future_fcf": {"complete": True, "passed": False}}}},
    }
    dcf_result = {"dcf_points": {"optimistic": {"upper": 30.0}}}

    assert run_full_audit._independent_decision_replay(
        "type3",
        payload,
        company=company,
        dcf_result=dcf_result,
    )
    resolved = deepcopy(company)
    resolved["type7"]["ledger"]["decision_gates"]["future_fcf"]["passed"] = True
    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company=resolved,
            dcf_result=dcf_result,
        )
        is None
    )


def test_independent_type6_replay_keeps_proxy_gaps_as_unresolved_evidence():
    payload = _type6_proxy_and_position_gap_payload()

    assert set(payload["decision"]["missing_dimensions"]) == {"6b", "6c", "6e"}
    assert payload["decision"]["decision_basis"] == "unresolved_missing_evidence"

    replayed = run_full_audit._independent_decision_replay("type6", payload)
    assert replayed is not None
    assert replayed["decision_basis"] == "unresolved_missing_evidence"
    assert {key: replayed[key] for key in payload["decision"]} == payload["decision"]


def test_independent_decision_replay_binds_type1_red_line_requirements_to_the_gate_audit():
    outcomes = {type_key: bs._not_applicable(type_key, "补丁7审计夹具不适用") for type_key in bs.TYPE_WEIGHTS}
    scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type1"]}
    outcomes["type1"] = bs._finish(
        "type1",
        scores,
        {key: "补丁7前已核验分项" for key in scores},
    )
    gated = bs._apply_patch7_total_gate(
        outcomes,
        {"price": 25.0, "industry": "缺少行业", "revenue_values": [], "revenue_years": []},
        {},
        {"ALL": {"median_cagr": 0.1, "median_cagr_count": 100}},
        type7_ledger={"decision_gates": {"future_fcf": {"complete": True, "passed": True}}},
    )
    payload = _outcome_payload("type1", gated["type1"])

    assert payload["reasons"]["_decision_missing_requirements"] == [
        "patch7_declining_industry",
        "patch7_long_term_operating_trend",
    ]
    assert run_full_audit._independent_decision_replay(
        "type1",
        payload,
        company={"price": 25.0},
        dcf_result={},
    )

    for removed_requirement in (
        "patch7_declining_industry",
        "patch7_long_term_operating_trend",
    ):
        forged = deepcopy(payload)
        forged["reasons"]["_decision_missing_requirements"].remove(removed_requirement)
        forged["decision"] = bs.replay_buy_decision("type1", forged)
        assert (
            run_full_audit._independent_decision_replay(
                "type1",
                forged,
                company={"price": 25.0},
                dcf_result={},
            )
            is None
        )

    payload["reasons"]["_patch7_gate"] = "通过|行业样本缺|营收缺"
    assert (
        run_full_audit._independent_decision_replay(
            "type1",
            payload,
            company={"price": 25.0},
            dcf_result={},
        )
        is None
    )


@pytest.mark.parametrize(
    "invalid_requirements",
    [
        [],
        ["patch7_current_price", "patch7_current_price"],
        ["unknown_requirement"],
        [1],
        "patch7_current_price",
        {"patch7_current_price": True},
    ],
)
def test_independent_decision_replay_rejects_malformed_patch7_requirement_ledgers(invalid_requirements):
    payload = _patch7_pending_payload()
    payload["reasons"]["_decision_missing_requirements"] = invalid_requirements
    company, dcf_result = _patch7_fact_context()

    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company=company,
            dcf_result=dcf_result,
        )
        is None
    )


@pytest.mark.parametrize(
    ("status", "applicable", "evidence_complete"),
    [
        ("not_applicable", False, False),
        ("insufficient_evidence", True, True),
        ("observe", True, False),
    ],
)
def test_independent_decision_replay_rejects_patch7_requirement_state_conflicts(
    status,
    applicable,
    evidence_complete,
):
    payload = _patch7_pending_payload()
    payload.update(
        status=status,
        applicable=applicable,
        evidence_complete=evidence_complete,
    )
    payload["reasons"].update(
        _status=status,
        _applicable="yes" if applicable else "no",
        _evidence="complete" if evidence_complete else "incomplete",
    )
    company, dcf_result = _patch7_fact_context()

    assert (
        run_full_audit._independent_decision_replay(
            "type3",
            payload,
            company=company,
            dcf_result=dcf_result,
        )
        is None
    )


def test_independent_decision_replay_handles_scope_exclusion_and_rejects_legacy_type7():
    excluded = _outcome_payload("type5", bs._not_applicable("type5", "不属于强周期公司"))
    excluded_replay = run_full_audit._independent_decision_replay("type5", excluded)
    assert excluded_replay is not None
    assert excluded_replay["decision_basis"] == "scope_exclusion"
    assert excluded_replay["score_upper_bound"] == 0.0

    scores = {"7a": 8.0, "7b": 8.0, "7c": 6.0}
    ledger = {
        "scores": {"template1": 80.0, "template5": 80.0, "patch5": 60.0},
        "decisive_score_upper_bounds": {"template1": 80.0, "template5": 80.0, "patch5": 70.0},
        "prerequisites_complete": False,
    }
    triggered, total, sub_scores, reasons = bs._finish(
        "type7",
        scores,
        {key: "部分质量证据" for key in scores},
        evidence_complete=False,
        missing_dimensions=["7c"],
    )
    quality = {
        "triggered": triggered,
        "total": total,
        "sub_scores": sub_scores,
        "reasons": reasons,
        "veto": bool(reasons.get("_veto")),
        "status": reasons["_status"],
        "applicable": True,
        "evidence_complete": False,
        "ledger": ledger,
        "decision_market_context": {
            "tradable": True,
            "reference_price": False,
            "risk_status": "",
        },
        # A shape-valid decision makes the independent replay reach the
        # classified-ledger guard instead of failing on an unrelated field.
        "decision": dict(excluded["decision"]),
    }
    with pytest.raises(ValueError, match="current classified ledger is required"):
        bs.replay_buy_decision("type7", quality)
    assert run_full_audit._independent_decision_replay("type7", quality) is None

    type1 = bs._finish(
        "type1",
        {"1a": 5.0, "1b": 8.0, "1c": 6.0, "1d": 5.0},
        {key: "审计夹具" for key in ("1a", "1b", "1c", "1d")},
    )
    current_outcome, current_ledger = bs.score_type7_quality_equity(
        {"code": "000001", "industry": "SOFTWARE", "source_trade_date": "2026-07-15"},
        type1,
        None,
        valuation_evidence_complete=True,
    )
    current = _outcome_payload("type7", current_outcome, ledger=current_ledger)
    current_replay = run_full_audit._independent_decision_replay("type7", current)
    assert current_replay is not None
    assert {key: current_replay[key] for key in current["decision"]} == current["decision"]
    assert current_replay["visible"] is True
    assert current_replay["recall_safe"] is True


def test_partial_source_failure_stays_visible_without_becoming_a_trigger():
    scores = {key: 8.0 for key in bs.TYPE_WEIGHTS["type4"]}
    payload = _outcome_payload(
        "type4",
        bs._finish(
            "type4",
            scores,
            {key: "公告数据源暂时不可用" for key in scores},
            evidence_complete=False,
            missing_dimensions=["4a"],
        ),
    )
    replayed = run_full_audit._independent_decision_replay("type4", payload)
    assert replayed is not None
    assert replayed["decision_complete"] is False
    assert replayed["potentially_triggerable"] is True
    assert replayed["visible"] is True
    assert payload["triggered"] is False


def test_independent_replay_mirrors_type4_minimum_runway_gate():
    scores = {"4a": 1.5, "4b": 10.0, "4c": 10.0, "4d": 10.0, "4e": 10.0, "4f": 10.0}
    payload = _outcome_payload(
        "type4",
        bs._finish(
            "type4",
            scores,
            {
                **{key: "完整可复核证据" for key in scores},
                "_condition": "坡长至少达到中坡（4a≥5）",
            },
            extra_condition=False,
        ),
    )

    replayed = run_full_audit._independent_decision_replay("type4", payload)

    assert replayed is not None
    assert replayed["potentially_triggerable"] is False
    assert replayed["decision_complete"] is True
    assert replayed["decision_basis"] == "full_evidence"
    assert {key: replayed[key] for key in payload["decision"]} == payload["decision"]


def test_independent_type7_replay_keeps_conservative_basis_when_weighted_upper_is_below_7():
    # Regression: the independent replay previously overwrote the weighted
    # upper-bound check (upper >= 7) for the classified Type 7 model and only
    # consulted ledger.upper_bound.  Real companies whose weighted decision
    # ceiling stays below 7 (e.g. 000978 with missing 7c) were then replayed
    # as potentially triggerable ("unresolved_missing_evidence") while
    # production correctly reported "conservative_upper_bound", failing the
    # whole-market screening evidence contract.
    ledger = {
        "model_id": bs.PATCH6_TYPE7_MODEL_ID,
        "dimensions": {"BM": {"upper_bound": 10.0}, "MOAT": {"upper_bound": 10.0}, "G": {"upper_bound": 10.0}},
        "classification": {"route_complete": False},
        "upper_bound": 10.0,
        "condition_failures": [],
        "quality_certified": False,
        "complete": False,
        "decision_gates": {},
    }
    payload = _outcome_payload(
        "type7",
        bs._finish(
            "type7",
            {"7a": 4.83, "7b": 3.63, "7c": 1.40},
            {"7a": "商业模式4.83", "7b": "护城河3.63", "7c": "长期成长1.40"},
            evidence_complete=False,
            missing_dimensions=["7c"],
        ),
        ledger=ledger,
    )
    assert payload["decision"]["decision_basis"] == "conservative_upper_bound"
    replayed = run_full_audit._independent_decision_replay("type7", payload)
    assert replayed is not None
    assert {key: replayed[key] for key in payload["decision"]} == payload["decision"]
    assert replayed["potentially_triggerable"] is False
    assert replayed["visible"] is True


def test_independent_type7_replay_applies_the_action_condition_basis():
    # Regression: production reports "conservative_upper_bound" when quality
    # is certified but a decision gate is still incomplete (001337 with
    # future_fcf failing), while the independent replay forced "full_evidence"
    # because all company evidence was complete.  The replay must mirror the
    # type7 action-condition branch.
    ledger = {
        "model_id": bs.PATCH6_TYPE7_MODEL_ID,
        "dimensions": {"BM": {"upper_bound": 10.0}, "MOAT": {"upper_bound": 10.0}, "G": {"upper_bound": 10.0}},
        "classification": {"route_complete": True},
        "upper_bound": 7.003,
        "condition_failures": ["future_fcf"],
        "quality_certified": True,
        "complete": False,
        "decision_gates": {
            "future_fcf": {"complete": True, "passed": False},
            "route_path": {"complete": False, "passed": False},
            "price_reasonableness": {"complete": False, "passed": False, "required": True},
        },
    }
    payload = {
        "triggered": False,
        "total": 7.003,
        "sub_scores": {"7a": 6.494, "7b": 7.398, "7c": 7.118},
        "reasons": {
            "7a": "强周期商业模式6.49",
            "7b": "强周期护城河7.40",
            "7c": "强周期长期成长7.12",
            "_status": "conditional",
            "_applicable": "yes",
            "_evidence": "complete",
            "_decision_missing_dimensions": [],
            "_quality_certified": "yes",
        },
        "veto": False,
        "status": "conditional",
        "applicable": True,
        "evidence_complete": True,
        "ledger": ledger,
        "decision_market_context": {"tradable": True, "reference_price": False, "risk_status": ""},
    }
    payload["decision"] = bs.replay_buy_decision("type7", payload)
    assert payload["decision"]["decision_basis"] == "conservative_upper_bound"
    replayed = run_full_audit._independent_decision_replay("type7", payload)
    assert replayed is not None
    assert {key: replayed[key] for key in payload["decision"]} == payload["decision"]
    assert replayed["potentially_triggerable"] is False


def test_independent_decision_replay_rejects_coordinated_status_and_veto_forgery():
    row = _complete_framework_row(quantitative_evidence=_quantitative_evidence())
    type5 = row["type5"]
    type5["sub_scores"] = {key: 8.0 for key in bs.TYPE_WEIGHTS["type5"]}
    type5["total"] = 8.0
    type5["status"] = "observe"
    type5["reasons"]["_status"] = "observe"
    type5["triggered"] = False
    type5["decision"] = bs.replay_buy_decision("type5", type5)
    assert run_full_audit._independent_decision_replay("type5", type5) is None

    type2 = row["type2"]
    type2["status"] = "vetoed"
    type2["reasons"]["_status"] = "vetoed"
    type2["reasons"]["_veto"] = "伪造否决"
    type2["veto"] = True
    type2["decision"] = bs.replay_buy_decision("type2", type2)
    assert run_full_audit._independent_decision_replay("type2", type2) is None


@pytest.mark.parametrize(
    ("quantitative_evidence", "expected_counter"),
    [
        (..., "missing_column"),
        ({}, "key_mismatch"),
        ("not-a-mapping", "non_mapping"),
        (
            {
                **_quantitative_evidence(),
                "moat_score": {
                    key: value for key, value in _quantitative_record("moat_score").items() if key != "evidence_level"
                },
            },
            "invalid_level",
        ),
        (
            {
                **_quantitative_evidence(),
                "moat_score": {
                    **_quantitative_record("moat_score"),
                    "evidence_level": "invented",
                },
            },
            "invalid_level",
        ),
    ],
)
def test_analysis_coverage_summary_rejects_missing_or_forged_quantitative_contract(
    quantitative_evidence,
    expected_counter,
):
    row = _complete_framework_row(quantitative_evidence=quantitative_evidence)

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"][expected_counter] == 1
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is False
    assert summary["goal_readiness"]["no_missing_quantitative_evidence"] is False
    assert summary["goal_readiness"]["no_partial_quantitative_evidence"] is False
    assert summary["goal_readiness"]["ready"] is False


def test_analysis_coverage_summary_rejects_level_only_quantitative_records():
    evidence = {key: {"evidence_level": "derived_proxy"} for key in run_full_audit._QUANTITATIVE_EVIDENCE_KEYS}
    row = _complete_framework_row(quantitative_evidence=evidence)

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"]["invalid_record"] == 13
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is False
    assert summary["goal_readiness"]["ready"] is False


def test_analysis_coverage_summary_treats_financial_industrial_metrics_as_not_applicable() -> None:
    evidence = _quantitative_evidence()
    for key in qe.FINANCIAL_INDUSTRIAL_NOT_APPLICABLE_KEYS:
        evidence[key] = _quantitative_record(key, "not_applicable")
    row = _complete_framework_row(quantitative_evidence=evidence)
    row["industry"] = "BANK"

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"]["valid_rows"] == 1
    assert summary["quantitative_evidence_level_counts"]["not_applicable"] == 2
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is True
    assert summary["goal_readiness"]["no_missing_quantitative_evidence"] is True
    assert summary["goal_readiness"]["no_partial_quantitative_evidence"] is True


def test_analysis_coverage_summary_rejects_quantitative_level_or_status_summary_mismatch():
    row = _complete_framework_row(quantitative_evidence=_quantitative_evidence())
    row["quantitative_evidence_levels"] = {
        **{key: "derived_proxy" for key in run_full_audit._QUANTITATIVE_EVIDENCE_KEYS},
        "moat_score": "partial",
    }
    row["quantitative_evidence_status"] = "partial"

    level_summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))
    assert level_summary["quantitative_evidence_contract"]["levels_mismatch"] == 1
    assert level_summary["goal_readiness"]["ready"] is False

    row["quantitative_evidence_levels"]["moat_score"] = "derived_proxy"
    row["quantitative_evidence_status"] = "partial"
    status_summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))
    assert status_summary["quantitative_evidence_contract"]["status_mismatch"] == 1
    assert status_summary["goal_readiness"]["ready"] is False


def test_analysis_coverage_summary_rejects_primary_summary_label_over_a_partial_record():
    evidence = _quantitative_evidence()
    evidence["moat_score"] = _quantitative_record(
        "moat_score",
        "partial",
        missing_inputs=["gross_margin_history"],
    )
    row = _complete_framework_row(quantitative_evidence=evidence)
    row["quantitative_evidence_levels"]["moat_score"] = "primary"
    row["quantitative_evidence_status"] = "complete"

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"]["valid_rows"] == 0
    assert summary["quantitative_evidence_contract"]["levels_mismatch"] == 1
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is False
    assert summary["goal_readiness"]["no_partial_quantitative_evidence"] is False
    assert summary["goal_readiness"]["ready"] is False


def test_analysis_coverage_summary_accepts_serialized_primary_with_sane_binding():
    """Efficiency-first: a shape-valid serialized primary record is accepted."""
    evidence = _quantitative_evidence()
    evidence["moat_score"] = _quantitative_record("moat_score", "primary")
    row = _complete_framework_row(quantitative_evidence=evidence)

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"]["valid_rows"] == 1
    assert summary["quantitative_evidence_contract"]["invalid_record"] == 0
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is True
    assert summary["goal_readiness"]["ready"] is True


def test_analysis_coverage_summary_rejects_an_internal_proxy_relabelled_as_primary():
    evidence = _quantitative_evidence()
    forged = evidence["moat_score"]
    forged["evidence_level"] = "primary"
    forged["evidence"]["summary"] = f"moat_score={forged['score']:.1f};model={qe.MODEL_ID};evidence_level=primary"
    forged["details"] = {
        "basis": "dated_primary_source_score",
        "source_summary": "derived_proxy result relabelled as primary",
        "evidence_quality": {
            "level": "primary",
            "input_coverage": 1.0,
            "required_inputs": ["primary_source_score"],
            "available_inputs": ["primary_source_score"],
            "missing_inputs": [],
        },
    }
    row = _complete_framework_row(quantitative_evidence=evidence)

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"]["invalid_record"] == 1
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is False
    assert summary["goal_readiness"]["ready"] is False


def test_analysis_coverage_summary_requires_quantitative_level_and_status_columns():
    row = _complete_framework_row(quantitative_evidence=_quantitative_evidence())
    row.pop("quantitative_evidence_levels")
    row.pop("quantitative_evidence_status")

    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame([row]))

    assert summary["quantitative_evidence_contract"]["summary_columns_missing"] == 1
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is False
    assert summary["goal_readiness"]["ready"] is False


def test_analysis_coverage_summary_never_accepts_an_empty_result():
    summary = run_full_audit._analysis_coverage_summary(pd.DataFrame())

    assert summary["goal_readiness"]["all_framework_payloads_present"] is False
    assert summary["goal_readiness"]["all_sub_scores_valid"] is False
    assert summary["goal_readiness"]["all_applicable_frameworks_evidence_complete"] is False
    assert summary["goal_readiness"]["all_quantitative_evidence_records_valid"] is False
    assert summary["goal_readiness"]["ready"] is False


def test_non_economic_skip_details_lists_every_data_and_model_exception_only():
    details = run_full_audit._non_economic_skip_details(
        {
            "000001": {"category": "economic_not_applicable", "reason": "ttm_fcff_nonpositive"},
            "000002": {"category": "source_missing", "reason": "ttm_fcff_missing_component"},
            "000003": {"category": "inconsistent_source", "reason": "negative_reconstructed_capex"},
            "000004": {"category": "model_unsupported", "reason": "financial_conglomerate"},
        }
    )

    assert details == {
        "inconsistent_source": {"000003": "negative_reconstructed_capex"},
        "model_unsupported": {"000004": "financial_conglomerate"},
        "source_missing": {"000002": "ttm_fcff_missing_component"},
    }


def test_market_coldness_loader_uses_single_validated_trade_date_and_one_bulk_snapshot(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame(
            [
                {"code": "000001", "market": "SZ"},
                {"code": "000002", "market": "SZ"},
            ]
        ),
        validation={
            "trading_source_trade_dates": ["2026-07-15"],
            "analysis_market_codes": ["000001", "000002"],
        },
    )
    coverage = SimpleNamespace(to_dict=lambda: {"total_records": 1})
    source_snapshot = SimpleNamespace(
        available=True,
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-15T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=True,
        cache_diagnostic="hit",
        reason="complete",
    )
    calls = []

    def fake_fetch(*, force_refresh, allow_expired_cache, cache_only=False):
        calls.append(("fetch", force_refresh, allow_expired_cache, cache_only))
        return source_snapshot

    evidence = {
        "000001": {"market_coldness_score": 6.0},
        "000002": {"market_coldness_score": 5.0},
    }

    def fake_build(value, *, as_of_session, listed_quote_codes, diagnostics):
        calls.append(("build", value, as_of_session, listed_quote_codes))
        diagnostics.update(_builder_diagnostics(listed_quote_codes, evidence))
        return evidence

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    actual, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=False,
        cache_only=True,
    )

    assert actual == {"000001": {"market_coldness_score": 6.0}}
    assert calls == [
        ("fetch", False, True, True),
        ("build", source_snapshot, "2026-07-15", ("000001", "000002")),
    ]
    assert status["available"] is True
    assert status["evidence_available"] is True
    assert status["source"] == run_full_audit.EASTMONEY_SOURCE
    assert status["eligible_evidence_coverage"] == 1.0
    assert status["full_listed_evidence_count"] == 2
    assert status["excluded_noneligible_evidence_count"] == 1
    assert status["excluded_noneligible_evidence_codes"] == ["000002"]


def test_market_coldness_loader_rejects_builder_identity_outside_quote_boundary(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-16"], "analysis_market_codes": ["000001"]},
    )
    source_snapshot = SimpleNamespace(
        available=True,
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-16T08:20:00Z",
        fetched_count=1,
        total_expected=1,
        coverage=SimpleNamespace(to_dict=lambda: {"total_records": 1}),
        cache_hit=False,
        cache_diagnostic="test",
        reason="",
    )
    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", lambda **_kwargs: source_snapshot)
    monkeypatch.setattr(
        run_full_audit,
        "build_market_coldness_evidence",
        lambda *_args, **_kwargs: {"999999": {"market_coldness_score": 8.0}},
    )

    evidence, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=False,
    )

    assert evidence == {}
    assert status["available"] is False
    assert status["evidence_reason"] == "validation_or_acquisition_error"
    assert "outside the listed quote boundary" in status["reason"]


def test_fresh_publication_refetches_when_cached_coldness_belongs_to_another_session(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-16"], "analysis_market_codes": ["000001"]},
    )
    coverage = SimpleNamespace(to_dict=lambda: {"total_records": 1})
    stale = SimpleNamespace(
        available=True,
        source="old",
        source_url="https://example.test/old",
        retrieved_at="2026-07-15T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=True,
        cache_diagnostic="expired_hit",
        reason="",
    )
    fresh = SimpleNamespace(
        available=True,
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at="2026-07-16T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=coverage,
        cache_hit=False,
        cache_diagnostic="forced_refresh",
        reason="",
    )
    calls = []

    def fake_fetch(*, force_refresh, allow_expired_cache, cache_only=False):
        calls.append(("fetch", force_refresh, allow_expired_cache, cache_only))
        return fresh if force_refresh else stale

    def fake_build(value, *, as_of_session, listed_quote_codes, diagnostics):
        calls.append(("build", value.source, as_of_session, listed_quote_codes))
        if value is stale:
            diagnostics.update(
                {
                    "evidence_available": False,
                    "evidence_reason": "session_retrieval_mismatch",
                    "retrieval_session": "2026-07-15",
                    "requested_session": "2026-07-16",
                }
            )
            return {}
        result = {"000001": {"market_coldness_score": 6.0}}
        diagnostics.update(_builder_diagnostics(listed_quote_codes, result))
        return result

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    evidence, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=True,
    )

    assert evidence == {"000001": {"market_coldness_score": 6.0}}
    assert status["source"] == run_full_audit.EASTMONEY_SOURCE
    assert calls == [
        ("fetch", False, True, False),
        ("build", "old", "2026-07-16", ("000001",)),
        ("fetch", True, False, False),
        ("build", run_full_audit.EASTMONEY_SOURCE, "2026-07-16", ("000001",)),
    ]


def test_fresh_publication_does_not_refetch_coldness_that_is_newer_than_quotes(monkeypatch):
    snapshot = SimpleNamespace(
        source="network",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-16"], "analysis_market_codes": ["000001"]},
    )
    newer = SimpleNamespace(
        available=True,
        source="newer",
        source_url="https://example.test/newer",
        retrieved_at="2026-07-17T08:20:00+00:00",
        fetched_count=1,
        total_expected=1,
        coverage=SimpleNamespace(to_dict=lambda: {"total_records": 1}),
        cache_hit=False,
        cache_diagnostic="fresh",
        reason="",
    )
    fetch_calls = []

    def fake_fetch(*, force_refresh, allow_expired_cache, cache_only=False):
        fetch_calls.append((force_refresh, allow_expired_cache, cache_only))
        return newer

    def fake_build(_value, *, diagnostics, **_kwargs):
        diagnostics.update(
            {
                "evidence_available": False,
                "evidence_reason": "session_retrieval_mismatch",
                "retrieval_session": "2026-07-17",
                "requested_session": "2026-07-16",
            }
        )
        return {}

    monkeypatch.setattr(run_full_audit, "fetch_market_coldness_snapshot", fake_fetch)
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", fake_build)

    evidence, status = run_full_audit._load_market_coldness_evidence(
        snapshot,
        ("000001",),
        force_refresh=True,
    )

    assert evidence == {}
    assert status["evidence_reason"] == "session_retrieval_mismatch"
    assert fetch_calls == [(False, True, False)]


def test_release_market_coldness_gate_requires_complete_applicable_coverage():
    eligible = tuple(f"{index:06d}" for index in range(100))
    artifact = _market_coldness_reference_artifact(
        eligible,
        current_year=(eligible[-1],),
    )
    evidence, status = _status_from_reference_artifact(artifact, eligible)

    assert (
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )
        == 0.99
    )

    evidence = dict(evidence)
    evidence.pop(eligible[98])
    status = _market_coldness_status(
        eligible,
        evidence,
        not_applicable={"listed_in_current_year": [eligible[-1]]},
        data_gaps={"missing_required_metric": [eligible[98]]},
    )
    with pytest.raises(RuntimeError, match="unexplained eligible data gaps"):
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_coordinated_not_applicable_forgery():
    eligible = tuple(f"{index:06d}" for index in range(100))
    artifact = _market_coldness_reference_artifact(eligible)
    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible,
        as_of_session="2026-07-16",
    )
    evidence = {eligible[0]: replay["eligible_evidence"][eligible[0]]}
    status = _market_coldness_status(
        eligible,
        evidence,
        not_applicable={"listed_in_current_year": eligible[1:]},
    )
    status["full_listed_evidence_count"] = len(replay["full_evidence"])
    status["reference_artifact_sha256"] = hashlib.sha256(
        run_full_audit._canonical_market_coldness_json(artifact)
    ).hexdigest()

    with pytest.raises(RuntimeError, match="applicability ledger differs from raw source evidence"):
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )


def test_market_coldness_replay_scores_numeric_zero_turnover_instead_of_marking_it_not_applicable():
    eligible = ("600001",)
    artifact = _market_coldness_reference_artifact(eligible, zero_turnover=eligible)

    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible,
        as_of_session="2026-07-16",
    )

    evidence = replay["eligible_evidence"]["600001"]
    assert evidence["components"]["raw_values"]["turnover_rate_pct"] == 0.0
    assert replay["eligible_not_applicable_codes_by_reason"] == {
        "listed_in_current_year": [],
        "listing_history_lt_120_days": [],
    }
    rebuilt_evidence, status = _status_from_reference_artifact(artifact, eligible)
    assert rebuilt_evidence == replay["eligible_evidence"]
    assert (
        run_full_audit._require_market_coldness_release_evidence(
            replay["eligible_evidence"],
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )
        == 1.0
    )


def test_release_market_coldness_gate_rebuilds_cross_sectional_relative_ranks():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    eligible = (listed[0],)
    artifact = _market_coldness_reference_artifact(listed)
    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=eligible,
        as_of_session="2026-07-16",
    )
    forged = _market_coldness_record(
        listed[0],
        relative={metric: 9.0 for metric in run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS},
    )
    status = _market_coldness_status(eligible)
    status["full_listed_evidence_count"] = len(replay["full_evidence"])
    status["reference_artifact_sha256"] = hashlib.sha256(
        run_full_audit._canonical_market_coldness_json(artifact)
    ).hexdigest()

    with pytest.raises(RuntimeError, match="differs from independent full-universe replay"):
        run_full_audit._require_market_coldness_release_evidence(
            {listed[0]: forged},
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_preclose_reference_batch():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    artifact = _market_coldness_reference_artifact(
        listed,
        retrieved_at="2026-07-16T01:00:00Z",
    )
    with pytest.raises(RuntimeError, match="retrieval timestamp is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {listed[0]: _market_coldness_record(listed[0], retrieved_at="2026-07-16T01:00:00Z")},
            _market_coldness_status(
                (listed[0],),
                retrieved_at="2026-07-16T01:00:00Z",
            ),
            reference_artifact=artifact,
            eligible_codes=(listed[0],),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_accepts_next_day_preopen_previous_close_batch():
    eligible = ("600000",)
    artifact = _market_coldness_reference_artifact(
        eligible,
        as_of_session="2026-07-22",
        retrieved_at="2026-07-22T18:11:06Z",
    )
    evidence, status = _status_from_reference_artifact(artifact, eligible)

    assert (
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-22",
        )
        == 1.0
    )


def test_release_market_coldness_replay_rejects_stale_source_update_dates():
    artifact = _market_coldness_reference_artifact(
        ("600000",),
        as_of_session="2026-07-22",
        retrieved_at="2026-07-22T18:11:06Z",
        source_updated_at="2026-07-21T07:34:00Z",
    )

    with pytest.raises(RuntimeError, match="source row belongs to another session"):
        run_full_audit._replay_market_coldness_reference_artifact(
            artifact,
            eligible_codes=("600000",),
            as_of_session="2026-07-22",
        )


def test_release_market_coldness_gate_rejects_previous_close_after_next_auction_starts():
    eligible = ("600000",)
    artifact = _market_coldness_reference_artifact(
        eligible,
        as_of_session="2026-07-22",
        retrieved_at="2026-07-23T01:15:00Z",
    )
    evidence = {
        eligible[0]: _market_coldness_record(
            eligible[0],
            as_of_session="2026-07-22",
            retrieved_at="2026-07-23T01:15:00Z",
        )
    }
    status = _market_coldness_status(
        eligible,
        as_of_session="2026-07-22",
        retrieved_at="2026-07-23T01:15:00Z",
    )

    with pytest.raises(RuntimeError, match="retrieval timestamp is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            evidence,
            status,
            reference_artifact=artifact,
            eligible_codes=eligible,
            as_of_session="2026-07-22",
        )


def test_market_coldness_replay_accepts_complete_source_rows_outside_the_listed_boundary():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    artifact = _market_coldness_reference_artifact(listed)
    baseline = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=(listed[0],),
        as_of_session="2026-07-16",
    )
    artifact["records"].append(
        [
            "300000",
            "2000-01-01",
            -90.0,
            -90.0,
            99.0,
            99.0,
            int(datetime.fromisoformat("2026-07-16T07:34:00+00:00").timestamp()),
        ]
    )
    artifact["records"].sort(key=lambda row: row[0])
    artifact["source_record_count"] += 1

    replay = run_full_audit._replay_market_coldness_reference_artifact(
        artifact,
        eligible_codes=(listed[0],),
        as_of_session="2026-07-16",
    )

    assert set(replay["full_evidence"]) == set(baseline["full_evidence"])
    for code, replayed_record in replay["full_evidence"].items():
        baseline_record = baseline["full_evidence"][code]
        replayed_without_context = deepcopy(replayed_record)
        baseline_without_context = deepcopy(baseline_record)
        replayed_context = replayed_without_context["components"].pop("relative_context")
        baseline_context = baseline_without_context["components"].pop("relative_context")
        assert replayed_without_context == baseline_without_context
        for metric in replayed_context:
            replayed_counts = dict(replayed_context[metric])
            baseline_counts = dict(baseline_context[metric])
            assert replayed_counts.pop("source_present") == baseline_counts.pop("source_present") + 1
            assert replayed_counts.pop("source_total") == baseline_counts.pop("source_total") + 1
            assert replayed_counts == baseline_counts


def test_market_coldness_replay_rejects_an_extra_row_masking_a_missing_listed_company():
    listed = tuple(f"{600000 + index:06d}" for index in range(50))
    artifact = _market_coldness_reference_artifact(listed)
    missing_code = artifact["listed_codes"][0]
    artifact["records"] = [row for row in artifact["records"] if row[0] != missing_code]
    artifact["records"].append(
        [
            "688888",
            "2000-01-01",
            -12.0,
            -8.0,
            1.0,
            0.8,
            int(datetime.fromisoformat("2026-07-16T07:34:00+00:00").timestamp()),
        ]
    )
    artifact["records"].sort(key=lambda row: row[0])

    with pytest.raises(RuntimeError, match="source does not cover the listed universe"):
        run_full_audit._replay_market_coldness_reference_artifact(
            artifact,
            eligible_codes=(listed[0],),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_zero_or_inconsistent_evidence():
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-16",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        run_full_audit._require_market_coldness_release_evidence(
            {},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    status.update(evidence_available=True, eligible_evidence_count=1, eligible_evidence_coverage=1.0)
    with pytest.raises(RuntimeError, match="count or coverage is inconsistent"):
        run_full_audit._require_market_coldness_release_evidence(
            {},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    with pytest.raises(RuntimeError, match="unknown or duplicate identities"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": {}, "999999": {}},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({}, "score is invalid"),
        (None, "record is invalid"),
        (
            {
                **_market_coldness_record("000001"),
                "market_coldness_score": float("nan"),
            },
            "score is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "market_coldness_score_evidence": {
                    **_market_coldness_record("000001")["market_coldness_score_evidence"],
                    "as_of": "2026-07-15",
                },
            },
            "score provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "market_coldness_score_evidence": {
                    **_market_coldness_record("000001")["market_coldness_score_evidence"],
                    "evidence_id": "patch6-type2c-quantity-price-v1:000001:20260715",
                },
            },
            "score provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "retrieved_at": "2026-07-15T08:20:00Z",
                },
            },
            "component provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    key: value
                    for key, value in _market_coldness_record("000001")["components"].items()
                    if key != "raw_values"
                },
            },
            "component provenance is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": None, "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -12.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": True, "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": float("nan"), "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -12.0, "change_ytd_pct": float("inf")},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -100.1, "change_ytd_pct": -8.0},
                },
            },
            "raw evidence is invalid",
        ),
        (
            {
                **_market_coldness_record("000001"),
                "components": {
                    **_market_coldness_record("000001")["components"],
                    "raw_values": {"change_60d_pct": -12.0, "change_ytd_pct": 10_000.1},
                },
            },
            "raw evidence is invalid",
        ),
    ],
)
def test_release_market_coldness_gate_rejects_invalid_record_content(record, message):
    status = _market_coldness_status(("000001",))

    with pytest.raises(RuntimeError, match=message):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            status,
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("schema_version", 999),
        ("model_id", "forged-market-coldness-model"),
        ("code", "000002"),
        ("source", "forged-source"),
    ],
)
def test_release_market_coldness_gate_rejects_forged_component_identity(field, forged_value):
    record = _market_coldness_record("000001")
    record["components"][field] = forged_value

    with pytest.raises(RuntimeError, match="component provenance is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_deleted_identity_and_relative_context():
    for missing_field in ("schema_version", "relative_context"):
        record = _market_coldness_record("000001")
        record["components"].pop(missing_field)
        with pytest.raises(RuntimeError, match="component provenance is invalid"):
            run_full_audit._require_market_coldness_release_evidence(
                {"000001": record},
                _market_coldness_status(("000001",)),
                eligible_codes=("000001",),
                as_of_session="2026-07-16",
            )

    record = _market_coldness_record("000001")
    record["components"]["relative_context"]["change_60d_pct"].pop("equal_count")
    with pytest.raises(RuntimeError, match="relative context is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("section_size", 0),
        ("section_population", 0),
        ("source_present", 2),
        ("source_total", 0),
        ("lower_count", -1),
        ("equal_count", 0),
    ],
)
def test_release_market_coldness_gate_rejects_invalid_relative_context_counts(field, forged_value):
    record = _market_coldness_record("000001")
    record["components"]["relative_context"]["change_60d_pct"][field] = forged_value

    with pytest.raises(RuntimeError, match="relative counts are invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_recomputes_relative_rank_and_sample_size_from_context():
    record = _market_coldness_record(
        "000001",
        relative={metric: 9.0 for metric in run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS},
    )
    record["components"]["relative_context"]["change_60d_pct"]["lower_count"] = 1
    with pytest.raises(RuntimeError, match="relative component replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    record = _market_coldness_record(
        "000001",
        relative={metric: 9.0 for metric in run_full_audit._MARKET_COLDNESS_BASE_WEIGHTS},
    )
    record["components"]["relative_sample_sizes"]["change_60d_pct"] += 1
    with pytest.raises(RuntimeError, match="relative component replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": record},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_record_gate_accepts_real_builder_output():
    retrieved_at = "2026-07-16T08:20:00Z"
    source_record = MarketColdnessRecord(
        code="600519",
        exchange="SH",
        eastmoney_market_id=1,
        name="贵州茅台",
        change_60d_pct=-12.0,
        change_ytd_pct=-8.0,
        turnover_rate_pct=1.0,
        volume_ratio=0.8,
        listing_date="2001-08-27",
        source_updated_at="2026-07-16T07:34:00Z",
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at=retrieved_at,
        upstream_fields={},
        missing_reasons={},
    )
    metrics = (
        "change_60d_pct",
        "change_ytd_pct",
        "turnover_rate_pct",
        "volume_ratio",
        "listing_date",
        "source_updated_at",
    )
    snapshot = MarketColdnessSnapshot(
        available=True,
        records=(source_record,),
        source=run_full_audit.EASTMONEY_SOURCE,
        source_url=run_full_audit.EASTMONEY_CLIST_ENDPOINT,
        retrieved_at=retrieved_at,
        total_expected=1,
        fetched_count=1,
        page_count=1,
        response_bytes=100,
        universe_coverage_rate=1.0,
        coverage=MarketColdnessCoverage(
            total_records=1,
            complete_records=1,
            complete_record_rate=1.0,
            by_metric={metric: MetricCoverage(present=1, missing=0, coverage_rate=1.0) for metric in metrics},
        ),
        cache_hit=False,
        cache_diagnostic="",
        reason="",
        failure=None,
    )
    record = run_full_audit.build_market_coldness_evidence(
        snapshot,
        as_of_session="2026-07-16",
        now=datetime(2026, 7, 16, 8, 25, tzinfo=timezone.utc),
        min_cross_section_records=1,
        min_board_turnover_records=1,
    )["600519"]

    run_full_audit._require_market_coldness_record(
        "600519",
        record,
        parsed_session=date(2026, 7, 16),
        retrieved_at=retrieved_at,
    )


def test_release_market_coldness_gate_independently_replays_hot_market_and_missing_volume_caps():
    hot = _market_coldness_record(
        "000001",
        raw_values={
            "change_60d_pct": 500.0,
            "change_ytd_pct": 500.0,
            "turnover_rate_pct": 20.0,
            "volume_ratio": 3.0,
        },
    )
    hot["market_coldness_score"] = 8.0
    with pytest.raises(RuntimeError, match="cap or final score replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": hot},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    without_volume = _market_coldness_record(
        "000001",
        raw_values={
            "change_60d_pct": -30.0,
            "change_ytd_pct": -35.0,
            "turnover_rate_pct": 0.5,
            "volume_ratio": None,
        },
    )
    assert without_volume["market_coldness_score"] <= 7.5
    without_volume["market_coldness_score"] = 8.0
    with pytest.raises(RuntimeError, match="cap or final score replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": without_volume},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_release_market_coldness_gate_rejects_missing_activity_and_forged_arithmetic():
    missing_turnover = _market_coldness_record("000001")
    missing_turnover["components"]["raw_values"]["turnover_rate_pct"] = None
    with pytest.raises(RuntimeError, match="raw evidence is invalid"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": missing_turnover},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )

    forged = _market_coldness_record("000001")
    forged["components"]["metric_scores"]["change_60d_pct"] += 1.0
    with pytest.raises(RuntimeError, match="metric score replay failed"):
        run_full_audit._require_market_coldness_release_evidence(
            {"000001": forged},
            _market_coldness_status(("000001",)),
            eligible_codes=("000001",),
            as_of_session="2026-07-16",
        )


def test_market_coldness_unavailable_or_unbound_continues_without_invented_values(monkeypatch):
    no_session = SimpleNamespace(source="cache", validation={"trading_source_trade_dates": []})
    monkeypatch.setattr(
        run_full_audit,
        "fetch_market_coldness_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch without a bound session")),
    )

    evidence, status = run_full_audit._load_market_coldness_evidence(
        no_session,
        ("000001",),
        force_refresh=False,
    )

    assert evidence == {}
    assert status["available"] is False
    assert status["evidence_available"] is False
    assert status["eligible_evidence_count"] == 0
    assert "exactly one" in str(status["reason"])
    assert status["unavailable_policy"] == "continue_with_insufficient_evidence"

    source_unavailable = SimpleNamespace(
        source="cache",
        quotes=pd.DataFrame([{"code": "000001", "market": "SZ"}]),
        validation={"trading_source_trade_dates": ["2026-07-15"], "analysis_market_codes": ["000001"]},
    )
    unavailable_snapshot = SimpleNamespace(
        available=False,
        source="Eastmoney bulk test",
        source_url="https://example.test/bulk",
        retrieved_at=None,
        fetched_count=0,
        total_expected=None,
        coverage=SimpleNamespace(to_dict=lambda: {"total_records": 0}),
        cache_hit=False,
        cache_diagnostic="miss",
        reason="network unavailable",
    )
    monkeypatch.setattr(
        run_full_audit,
        "fetch_market_coldness_snapshot",
        lambda **_kwargs: unavailable_snapshot,
    )
    monkeypatch.setattr(run_full_audit, "build_market_coldness_evidence", lambda *_args, **_kwargs: {})

    evidence, status = run_full_audit._load_market_coldness_evidence(
        source_unavailable,
        ("000001",),
        force_refresh=False,
    )

    assert evidence == {}
    assert status["available"] is False
    assert status["evidence_reason"] == "source_unavailable"
    assert status["eligible_evidence_coverage"] == 0.0
    assert status["unavailable_policy"] == "continue_with_insufficient_evidence"


@pytest.mark.parametrize("stdout_encoding", ["cp1252", "gbk"])
def test_cached_full_audit_uses_active_quality_as_regression_baseline(monkeypatch, tmp_path, stdout_encoding):
    snapshot_path = tmp_path / "market_snapshot.json.gz"
    snapshot_path.write_bytes(b"validated-snapshot")
    quotes = pd.DataFrame([{"code": "000001"}])
    financials = {"000001": {}}
    active_quality = {"score_rows": 1, "dcf_attempted": 1, "dcf_valid": 1}
    snapshot = SimpleNamespace(
        eligible_codes=("000001",),
        analysis_quotes=quotes,
        analysis_financials=financials,
        previous_analysis_quality={},
        analysis_quality=active_quality,
        source="cache",
        quotes=quotes,
        financials=financials,
        data_timestamp=123.0,
        baseline_payload_sha256="b" * 64,
        validation={
            "eligible_codes": ["000001"],
            "reporting_period_contract": _reporting_period_contract_payload(),
            "trading_source_trade_dates": ["2026-07-15"],
        },
    )
    quality = {
        "score_rows": 1,
        "dcf_attempted": 1,
        "dcf_valid": 1,
        "score_coverage": 1.0,
        "dcf_attempt_coverage": 1.0,
        "dcf_valid_coverage": 1.0,
        "pipeline_issue_rate": 0.0,
    }
    analysis = SimpleNamespace(
        scores=quotes,
        dcf_results={"000001": {}},
        dcf_skipped=0,
        dcf_skip_reasons={},
        issues=(),
        quality=quality,
        quality_history_evidence={},
        type3_growth_evidence={},
        research_report_evidence={},
        patch4_evidence={},
    )
    calls = {}

    monkeypatch.setattr(run_full_audit, "DEFAULT_SNAPSHOT_PATH", snapshot_path)

    class FakeCache:
        def read_bytes_if_payload(self, expected):
            assert expected == "b" * 64
            return b"validated-snapshot"

    def fake_cache(*_args, **kwargs):
        calls["cache_kwargs"] = kwargs
        return FakeCache()

    monkeypatch.setattr(run_full_audit, "SafeFileCache", fake_cache)
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    coldness_evidence = {
        "000001": _market_coldness_record(
            "000001",
            as_of_session="2026-07-15",
            retrieved_at="2026-07-15T08:20:00Z",
        )
    }
    coldness_status = _market_coldness_status(
        ("000001",),
        as_of_session="2026-07-15",
        retrieved_at="2026-07-15T08:20:00Z",
    )

    archive_candidate = SimpleNamespace(available=True)

    def fake_coldness(
        snapshot_arg,
        eligible_arg,
        *,
        force_refresh,
        reference_artifact_out,
        archive_candidate_out,
    ):
        assert snapshot_arg is snapshot
        assert tuple(eligible_arg) == ("000001",)
        assert force_refresh is False
        reference_artifact_out.update({"fixture": True})
        archive_candidate_out.append(archive_candidate)
        return coldness_evidence, coldness_status

    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", fake_coldness)

    def fake_coldness_gate(*_args, **_kwargs):
        calls["coldness_gate_passed"] = True
        return 1.0

    def fake_coldness_archive(candidate, session):
        assert calls.get("coldness_gate_passed") is True
        assert candidate is archive_candidate
        assert session == "2026-07-15"
        calls["coldness_archived"] = True
        return candidate

    monkeypatch.setattr(run_full_audit, "_require_market_coldness_release_evidence", fake_coldness_gate)
    monkeypatch.setattr(run_full_audit, "archive_market_coldness_session_snapshot", fake_coldness_archive)

    def fake_analysis(*args, **kwargs):
        calls["analysis_args"] = args
        calls["analysis_kwargs"] = kwargs
        return analysis

    monkeypatch.setattr(run_full_audit, "run_market_analysis", fake_analysis)
    monkeypatch.setattr(
        run_full_audit,
        "save_market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache input must not be promoted")),
    )

    state = {
        "code_sha256": "1" * 64,
        "rules_sha256": "2" * 64,
        "industry_sha256": "3" * 64,
        "dependency_manifest_sha256": "4" * 64,
    }
    monkeypatch.setattr(run_full_audit, "audit_state_hashes", lambda: dict(state))
    audit = SimpleNamespace(
        sample_size=1,
        engine_invariant_errors=(),
        scoring_replay_errors=(),
        valuation_replay_errors=(),
        independent_errors=(),
        invariant_errors=(),
        provenance=dict(state),
    )

    def fake_audit(*args, **kwargs):
        calls["audit_args"] = args
        calls["audit_kwargs"] = kwargs
        return audit

    monkeypatch.setattr(run_full_audit, "audit_random_sample", fake_audit)
    monkeypatch.setattr(
        run_full_audit,
        "write_audit_artifacts",
        lambda *_args, **_kwargs: {"json": Path("审计😀.json")},
    )
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding=stdout_encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    result = run_full_audit.main(["--sample-size", "1", "--output-dir", str(tmp_path / "audit")])

    assert result == 0
    assert calls["cache_kwargs"]["ttl"] == run_full_audit.MAX_STALE_AGE_SECONDS
    assert calls["analysis_kwargs"]["previous_quality"] == active_quality
    assert calls["analysis_kwargs"]["enforce_quality"] is True
    assert calls["analysis_kwargs"]["expected_companies"] == 1
    expected_contract = ReportingPeriodContract("2025-12-31", "2026-03-31", "2025-03-31")
    assert calls["analysis_kwargs"]["reporting_period_contract"] == expected_contract
    assert calls["analysis_kwargs"]["market_coldness_evidence"] is coldness_evidence
    assert calls["coldness_archived"] is True
    assert calls["analysis_kwargs"]["quality_history_loader"] is run_full_audit.fetch_quality_history_batch
    assert calls["analysis_kwargs"]["research_report_loader"] is run_full_audit.fetch_research_reports_batch
    assert calls["analysis_kwargs"]["patch4_loader"] is run_full_audit.fetch_patch4_evidence_batch
    assert calls["audit_kwargs"]["provenance"]["full_market_quality"] == quality
    assert calls["audit_kwargs"]["provenance"]["market_coldness"] == coldness_status
    assert calls["audit_kwargs"]["full_market_analysis"] is analysis
    assert calls["audit_kwargs"]["reporting_period_contract"] == expected_contract
    assert calls["audit_kwargs"]["market_coldness_evidence"] is coldness_evidence
    assert calls["audit_kwargs"]["quality_history_evidence"] == {}
    assert calls["audit_kwargs"]["research_report_evidence"] == {}
    assert calls["audit_kwargs"]["patch4_evidence"] == {}
    assert len(calls["audit_kwargs"]["snapshot_sha256"]) == 64
    stdout.flush()
    output = json.loads(stdout_bytes.getvalue().decode(stdout_encoding))
    assert output["refresh_requested"] is False
    assert output["refresh_completed"] is True
    assert output["snapshot_source"] == "cache"
    assert output["snapshot_warning"] == ""
    assert output["market_coldness"]["eligible_evidence_coverage"] == 1.0
    assert output["artifacts"]["json"] == "审计😀.json"

    analysis.issues = (SimpleNamespace(code="000001", stage="valuation", message="failed"),)
    assert run_full_audit.main(["--sample-size", "1", "--output-dir", str(tmp_path / "audit")]) == 1


def test_full_audit_fails_closed_when_snapshot_has_no_reporting_period_contract(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(validation={}, source="cache")
    monkeypatch.setattr(run_full_audit, "DEFAULT_SNAPSHOT_PATH", tmp_path / "snapshot.json.gz")
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)

    with pytest.raises(RuntimeError, match="no reporting_period_contract"):
        run_full_audit.main([])


def test_fresh_full_audit_stops_before_analysis_when_coldness_coverage_is_zero(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(
        source="network",
        eligible_codes=("000001",),
        validation={
            "trading_source_trade_dates": ["2026-07-16"],
            "reporting_period_contract": _reporting_period_contract_payload(),
        },
    )
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-16",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", lambda *_args, **_kwargs: ({}, status))
    monkeypatch.setattr(
        run_full_audit,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )
    monkeypatch.setattr(
        run_full_audit,
        "save_market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot must not be saved")),
    )

    output_dir = tmp_path / "audit"
    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        run_full_audit.main(["--refresh", "--output-dir", str(output_dir)])
    assert not output_dir.exists()


def test_cached_full_audit_also_stops_before_analysis_when_coldness_is_unbound(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(
        source="cache",
        eligible_codes=("000001",),
        validation={
            "trading_source_trade_dates": ["2026-07-16"],
            "reporting_period_contract": _reporting_period_contract_payload(),
        },
    )
    status = {
        "available": True,
        "evidence_available": False,
        "evidence_reason": "session_retrieval_mismatch",
        "as_of_session": "2026-07-16",
        "eligible_evidence_count": 0,
        "eligible_evidence_coverage": 0.0,
    }
    output_dir = tmp_path / "audit"
    output_dir.mkdir()
    marker = output_dir / "existing.json"
    marker.write_text("last-known-good", encoding="utf-8")
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(run_full_audit, "_load_market_coldness_evidence", lambda *_args, **_kwargs: ({}, status))
    monkeypatch.setattr(
        run_full_audit,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )

    with pytest.raises(RuntimeError, match="unavailable: session_retrieval_mismatch"):
        run_full_audit.main(["--output-dir", str(output_dir)])

    assert marker.read_text(encoding="utf-8") == "last-known-good"


def test_forced_full_audit_preserves_existing_artifacts_when_quotes_fall_back_to_cache(monkeypatch, tmp_path):
    output_dir = tmp_path / "audit"
    output_dir.mkdir()
    original = {}
    for suffix, content in (("json", b"old-json"), ("csv", b"old-csv"), ("md", b"old-markdown")):
        path = output_dir / f"random100_audit_seed20260715.{suffix}"
        path.write_bytes(content)
        original[path] = content

    source_warning = "refresh failed: DataFetchError: Eastmoney page 3 timed out after 3 attempts"
    snapshot = SimpleNamespace(source="stale_cache", warning=source_warning)
    monkeypatch.setattr(run_full_audit, "SafeFileCache", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "DataFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(run_full_audit, "get_market_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        run_full_audit,
        "run_market_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not start")),
    )
    monkeypatch.setattr(
        run_full_audit,
        "save_market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot must not be saved")),
    )
    monkeypatch.setattr(
        run_full_audit,
        "write_audit_artifacts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("audit artifacts must not be written")),
    )

    with pytest.raises(RuntimeError, match="existing audit artifacts were preserved") as exc_info:
        run_full_audit.main(["--refresh", "--output-dir", str(output_dir)])
    assert source_warning in str(exc_info.value)
    assert {path: path.read_bytes() for path in original} == original
