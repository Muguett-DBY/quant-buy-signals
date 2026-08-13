"""Compact, checksummed market snapshots for read-only mobile clients.

The desktop analysis contains the complete financial input tree and DCF audit
ledger.  Publishing that object to a phone would be slow, costly, and expose
far more raw provider data than the client needs.  This module exports a
small catalogue for the whole market plus a separate detail file for actual
or conditional candidates.  Applicable frameworks publish every verified
sub-score.  Any dimension named as missing by the bounded decision contract is
excluded from exact scores.  Its model estimate is published separately and
explicitly labelled as unconfirmed, so the website can show the quantified
diagnostic without turning an internal evidence placeholder into a verified
fact.  Incomplete totals are likewise omitted while their score interval
remains available in ``decision``.  The catalogue can therefore show unresolved
and decisively rejected candidates without mistaking either for complete scores
or buy signals.  It never performs an analysis itself.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import pandas as pd

from data.public_presentation import public_industry_name, public_reason_text
from engine.buy_screener import (
    DECISION_BASES,
    DECISION_MODEL_ID,
    DECISION_SCHEMA_VERSION,
    DECISION_VETO_STATES,
    TYPE_NAMES,
    TYPE_WEIGHTS,
    validate_screening_result,
)
from engine.type7_patch6 import MODEL_ID as _TYPE7_CURRENT_MODEL_ID


SNAPSHOT_SCHEMA_VERSION = 1
COMPANY_DETAIL_SCHEMA_VERSION = 2
COMPANY_DETAIL_SHARD_COUNT = 16
MAX_COMPRESSED_ASSET_BYTES = 8_000_000
# The catalogue grew past 24 MB once annual-report acquisition evidence
# (CNINFO) was added to the external growth records; the limit is a memory
# guard for small Android clients, so it is raised to 32 MB with headroom.
# Android clients must ship the matching limit (MarketRepository.java).
MAX_UNCOMPRESSED_ASSET_BYTES = 32_000_000
MAX_DETAIL_COMPRESSED_TOTAL = 48_000_000
MAX_DETAIL_UNCOMPRESSED_TOTAL = 144_000_000
MAX_PUBLIC_REASON_UTF16_UNITS = 200
MAX_PATCH7_GATE_UTF16_UNITS = 48
CATALOG_FILENAME = "catalog-{generation}.json.gz"
SIGNALS_FILENAME = "signals-{generation}.json.gz"
COMPANY_DETAIL_FILENAME = "company-details-{generation}-{shard_id}.json.gz"
SIGNATURE_FILENAME = "manifest-{generation}.sig"
MANIFEST_FILENAME = "manifest.json"
_TYPE_KEYS = tuple(f"type{number}" for number in range(1, 8))
_TYPE7_INPUT_NAMES = {
    "gross_margin_std_pp": "毛利率年度波动（百分点）",
    "observations": "可用年度观察数",
    "median_elasticity": "利润相对收入变化的弹性中位数",
    "mean_capex_revenue": "资本开支与收入比均值",
    "industry": "所属行业",
    "rd_intensity": "研发费用率",
    "cap": "该行业可计最高分",
    "gross_margin": "毛利率",
    "gross_margin_cv": "毛利率相对波动",
    "gross_outcome": "毛利率水平与稳定性分",
    "moat_score": "护城河证据分",
    "ocf_np_ratio": "经营现金流与净利润比",
    "fcf_positive_score": "自由现金流为正年份占比分",
    "N_sensitivity": "需求刚性分",
    "business_model_score": "商业模式证据分",
    "capex_revenue_median": "资本开支与收入比中位数",
    "sector_anchor": "行业稀缺性基准分",
    "history_years": "共同连续历史年数",
    "history_score": "历史厚度分",
    "durability_score": "护城河耐久证据分",
    "trend_growth": "长期趋势增速",
    "growth_score": "长期趋势增速映射分",
    "pricing_score": "定价权结果分",
    "runway_score": "增长空间证据分",
    "growth_sustainability_score": "增长可持续证据分",
    "margin_stability": "毛利率稳定分",
    "growth_consistency": "收入增速离散度",
    "profit_volatility": "利润波动率",
    "rd_intensity_score": "研发费用率映射分",
    "roic_score": "投入资本回报率减资金成本映射分",
    "stability_score": "经营稳定分",
    "cash_conversion": "现金转化分",
    "asset_light": "轻资产分",
    "technology_score": "技术证据分",
    "management_alignment_score": "管理层长期一致性证据分",
    "industry_durability_score": "行业长期空间证据分",
    "moat_durability_score": "护城河耐久证据分",
    "cycle_overlay_penalty": "周期叠加扣分",
    "debt_score": "资产负债表韧性分",
    "cycle_sensitivity": "周期敏感度分",
}
_TYPE7_FORMULAS = {
    ("W", "pricing_power"): "毛利率结果分与护城河证据分取平均；仅使用间接资料时最高9分",
    ("W", "fcf_conversion"): "经营现金流与净利润比按区间映射，再与自由现金流为正年份占比分取平均",
    ("W", "repeat_demand"): "需求刚性分与商业模式证据分取平均；仅使用间接资料时最高8分",
    ("W", "asset_light"): "资本开支与收入比中位数越低得分越高：5%约9分、15%约5分、25%约2分",
    ("W", "brand_mindshare"): "护城河证据分与毛利率结果分取平均；仅使用间接资料时最高8.5分",
    ("W", "network_switching"): "护城河证据分与商业模式证据分取平均；仅使用间接资料时最高8分",
    ("W", "license_scarcity"): "护城河证据分与行业稀缺性基准分取平均；仅使用间接资料时最高8分",
    ("W", "time_thickness"): "共同连续历史年数映射分与护城河耐久证据分取平均；仅使用间接资料时最高9分",
    ("W", "volume_price_space"): "长期趋势增速映射分与定价权结果分取平均",
    ("W", "category_expansion"): "增长空间证据分与增长可持续证据分取平均；仅使用间接资料时最高8分",
    ("W", "inflation_pass_through"): "定价权结果分与毛利率稳定分取平均；仅使用间接资料时最高8分",
    ("W", "certainty"): "收入增速离散度和利润波动率分别反向映射后取平均",
    ("T", "rd_conversion"): "研发费用率映射分、长期趋势增速映射分和资本回报映射分取平均",
    ("T", "revenue_quality"): "商业模式证据分、经营稳定分和现金转化分取平均；仅使用间接资料时最高8.5分",
    ("T", "declining_marginal_cost"): "毛利率结果分、轻资产分和资本回报映射分取平均；仅使用间接资料时最高9分",
    ("T", "cashflow_inflection"): "现金转化分、自由现金流为正年份占比分和增长分取平均，再扣除周期叠加0.5至2分",
    ("T", "patent_standard"): "技术证据分与研发费用率映射分取平均；未使用专利清单时最高8分",
    ("T", "talent_retention"): "管理层长期一致性证据分与技术证据分取平均；未使用员工留存原始资料时最高8分",
    ("T", "data_network"): "护城河、商业模式和技术证据分取平均，按行业属性最高8至9分",
    ("T", "platform_lockin"): "护城河证据分与商业模式证据分取平均；仅使用间接资料时最高9分",
    ("T", "s_curve_relay"): "增长分、增长空间证据分和技术证据分取平均；仅使用间接资料时最高8.5至9分",
    ("T", "tam_space"): "增长空间、行业长期空间和增长分取平均；仅使用间接资料时最高8.5分",
    ("T", "nonlinear_option"): "技术、研发强度和增长可持续证据分取平均；仅使用间接资料时最高8分",
    ("T", "disruption_resilience"): "护城河耐久证据分与经营稳定分取平均；仅使用间接资料时最高9分",
    ("C", "cost_curve"): "毛利率结果分、资本回报映射分和经营稳定分取平均；仅使用间接资料时最高8分",
    ("C", "integration_self_supply"): "商业模式证据分、现金转化分和资本强度纪律分取平均；仅使用间接资料时最高8分",
    ("C", "cash_conversion"): "经营现金流与净利润比映射分和自由现金流为正年份占比分取平均",
    ("C", "capacity_discipline"): "轻资产反向分、资产负债表韧性分和资本回报映射分取平均；仅使用间接资料时最高8分",
    ("C", "resource_scarcity"): "护城河证据分与资源行业基准分取平均；仅使用间接资料时最高8至9分",
    ("C", "cost_lead"): "毛利率结果分、资本回报映射分和毛利率稳定分取平均；仅使用间接资料时最高8分",
    ("C", "scale_location"): "护城河、商业模式和资本回报映射分取平均；仅使用间接资料时最高7分",
    ("C", "cycle_survival"): "共同连续历史年数、自由现金流为正年份占比和负债韧性分取平均",
    ("C", "low_cost_expansion"): "增长分、资本回报映射分和商业模式证据分取平均；仅使用间接资料时最高8分",
    ("C", "integration_gain"): "现金转化分、毛利率结果分和资本回报映射分取平均；仅使用间接资料时最高8分",
    ("C", "commodity_trend"): "行业长期空间证据分与公司趋势增长分取平均；仅使用间接资料时最高7分",
    ("C", "certainty"): "经营稳定分与周期敏感度反向分取平均；仅使用间接资料时最高7分",
}
_TYPE7_CLASSIFICATION_NAMES = {
    "C": ("强周期敏感度", "除非强科技已优先达到7分，否则本项达到7分时归入强周期。"),
    "T": ("强科技敏感度", "本项达到7分时优先归入强科技。"),
    "N": ("弱周期特征", "本项展示弱周期特征强弱；最终走弱周期路线的条件是强科技和强周期均未达到7分。"),
}
_TYPE7_CLASSIFICATION_COMPONENTS = {
    "c_margin_volatility": ("毛利率五年波动", "财报直接计算", False, False),
    "c_profit_elasticity": ("利润相对收入的弹性", "财报直接计算", False, False),
    "c_capex_intensity": ("资本开支强度", "财报直接计算", False, False),
    "c_commodity_driver": ("商品价格或产能驱动", "所属行业间接判断", True, False),
    "t_rd_intensity": ("研发费用率", "财报直接计算", False, False),
    "t_intangible_patent": ("专利或无形资产密集度", "所属行业间接判断", True, False),
    "t_iteration": ("技术或产品迭代速度", "所属行业间接判断", True, False),
    "t_platform": ("网络效应或平台生态", "经营与技术资料综合判断", False, True),
    "n_repeat": ("复购或必选需求", "所属行业间接判断", True, False),
    "n_macro_beta": ("低宏观敏感度", "财务稳定性间接判断", False, True),
    "n_pricing": ("定价能力", "毛利率表现间接判断", False, True),
    "n_mindshare": ("刚需或心智优势", "行业与护城河资料综合判断", True, True),
}
_DECISION_FIELDS = {
    "schema_version",
    "model_id",
    "decision_complete",
    "decision_basis",
    "score_lower_bound",
    "score_upper_bound",
    "veto_state",
    "potentially_triggerable",
    "missing_dimensions",
}
_PUBLIC_META_REASON_KEYS = ("_scope", "_veto", "_missing", "_condition", "_downgrade", "_risk")
_PUBLIC_DETAIL_META_REASON_KEYS = (
    *_PUBLIC_META_REASON_KEYS,
    "_blocked",
    "_adjustment",
    "_coverage",
    "_profile",
    "_score_quality",
    "_4f_formula",
)
_SCORELESS_STATUSES = frozenset({"not_applicable", "insufficient_evidence"})
_STATUS_LABELS = {
    "triggered": "已触发",
    "conditional": "待确认，不是买入信号",
    "observe": "观察",
    "insufficient_evidence": "资料不足",
    "vetoed": "不符合硬条件",
    "not_triggered": "未触发",
    "not_applicable": "不适用",
    "blocked": "因市场状态被阻断",
}


class MobileSnapshotError(ValueError):
    """The completed analysis cannot safely be exported to a mobile client."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _json_safe(converted)
    return str(value)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    return code.zfill(6) if code.isdigit() and len(code) <= 6 else code


def _public_reason_text(value: Any) -> str:
    """Apply the shared plain-language boundary to mobile explanations."""
    text = public_reason_text(value)
    if len(text.encode("utf-16-le")) // 2 <= MAX_PUBLIC_REASON_UTF16_UNITS:
        return text

    # Android's String.length() counts UTF-16 code units rather than Unicode
    # code points.  Truncate on code-point boundaries and reserve one unit for
    # a visible ellipsis so every generated reason satisfies the exact client
    # contract, including text containing non-BMP emoji.
    remaining = MAX_PUBLIC_REASON_UTF16_UNITS - 1
    output: list[str] = []
    for character in text:
        units = len(character.encode("utf-16-le")) // 2
        if units > remaining:
            break
        output.append(character)
        remaining -= units
    return "".join(output).rstrip() + "…"


def _public_patch7_gate(value: str, status: str) -> str:
    """Validate the compact Type 1 Patch 7 red-line audit summary."""

    text = public_reason_text(value)
    if text != value or len(text.encode("utf-16-le")) // 2 > MAX_PATCH7_GATE_UTF16_UNITS:
        raise MobileSnapshotError("type1 Patch 7 gate summary is malformed")
    parts = text.split("|")
    if len(parts) != 3 or parts[0] not in {"通过", "待补", "否决"}:
        raise MobileSnapshotError("type1 Patch 7 gate summary is malformed")
    sample = parts[1].removeprefix("行业样本") if parts[1].startswith("行业样本") else ""
    years = parts[2].removeprefix("营收") if parts[2].startswith("营收") else ""
    valid_sample = sample == "缺" or (sample.isdigit() and int(sample) > 0)
    year_values = years.split("/")
    valid_years = years == "缺" or (
        len(year_values) == 4
        and all(year.isdigit() and len(year) == 4 for year in year_values)
        and [int(year) for year in year_values] == list(range(int(year_values[0]), int(year_values[0]) + 4))
    )
    if not valid_sample or not valid_years:
        raise MobileSnapshotError("type1 Patch 7 gate summary is malformed")
    if (parts[0] == "待补" and status != "insufficient_evidence") or (parts[0] == "否决" and status != "vetoed"):
        raise MobileSnapshotError("type1 Patch 7 gate summary conflicts with status")
    return text


def _public_decision(payload: Any, type_key: str) -> dict[str, Any]:
    """Validate and retain the machine-readable candidate-bound contract."""

    if not isinstance(payload, Mapping) or set(payload) != _DECISION_FIELDS:
        raise MobileSnapshotError(f"{type_key} decision contract is missing or malformed")
    lower = _finite(payload.get("score_lower_bound"))
    upper = _finite(payload.get("score_upper_bound"))
    missing = payload.get("missing_dimensions")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != DECISION_SCHEMA_VERSION
        or payload.get("model_id") != DECISION_MODEL_ID
        or type(payload.get("decision_complete")) is not bool
        or payload.get("decision_basis") not in DECISION_BASES
        or payload.get("veto_state") not in DECISION_VETO_STATES
        or type(payload.get("potentially_triggerable")) is not bool
        or lower is None
        or upper is None
        or not 0.0 <= lower <= upper <= 10.0
        or not isinstance(missing, list)
        or len(missing) != len(set(missing))
        or any(item not in TYPE_WEIGHTS[type_key] for item in missing)
    ):
        raise MobileSnapshotError(f"{type_key} decision contract contains invalid fields")
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "model_id": DECISION_MODEL_ID,
        "decision_complete": payload["decision_complete"],
        "decision_basis": payload["decision_basis"],
        "score_lower_bound": round(lower, 3),
        "score_upper_bound": round(upper, 3),
        "veto_state": payload["veto_state"],
        "potentially_triggerable": payload["potentially_triggerable"],
        "missing_dimensions": list(missing),
    }


def _is_live_type6_position_confirmation(
    status: str,
    decision: Mapping[str, Any],
) -> bool:
    """Return whether Type 6 is awaiting a real investor position check."""

    missing_dimensions = decision.get("missing_dimensions")
    return (
        status == "conditional"
        and decision.get("decision_complete") is False
        and decision.get("potentially_triggerable") is True
        and isinstance(missing_dimensions, list)
        and "6e" in missing_dimensions
    )


def _is_type6_investor_action_only(decision: Mapping[str, Any]) -> bool:
    """Return whether Type 6 is missing only the investor's position input.

    The five company-side dimensions are still mathematically reproducible in
    this state.  The final action cannot be certified until the investor
    confirms the real position, so callers must publish it as a diagnostic
    rather than a buy score.
    """

    missing_dimensions = decision.get("missing_dimensions")
    return isinstance(missing_dimensions, list) and set(missing_dimensions) == {"6e"}


def _compact_type(payload: Any, type_key: str) -> dict[str, Any]:
    def public_dimensions(
        value: Mapping[str, Any],
        status: str,
    ) -> tuple[dict[str, float], dict[str, str], dict[str, float], dict[str, str]]:
        # Not-applicable frameworks have no meaningful dimensions.  For an
        # evidence-incomplete framework, publish verified dimensions as exact
        # and contract-declared missing dimensions in a separate estimate map.
        if status == "not_applicable":
            return {}, {}, {}, {}
        raw_scores = value.get("sub_scores")
        raw_reasons = value.get("reasons")
        if not isinstance(raw_scores, Mapping):
            return {}, {}, {}, {}
        decision = value.get("decision")
        missing_dimensions = set(decision.get("missing_dimensions", [])) if isinstance(decision, Mapping) else set()
        scores: dict[str, float] = {}
        reasons: dict[str, str] = {}
        estimates: dict[str, float] = {}
        estimate_reasons: dict[str, str] = {}
        for dimension in TYPE_WEIGHTS[type_key]:
            score = _finite(raw_scores.get(dimension))
            if score is None:
                continue
            evidence = (
                _public_reason_text(raw_reasons[dimension])
                if isinstance(raw_reasons, Mapping) and isinstance(raw_reasons.get(dimension), str)
                else ""
            )
            if dimension in missing_dimensions:
                # 6e is an investor action, not an uncertain company fact.  It
                # receives dedicated position guidance below instead of a
                # misleading company-data estimate.
                if type_key != "type6" or dimension != "6e":
                    estimates[dimension] = round(score, 3)
                    estimate_reasons[dimension] = (
                        f"未确认估算，不用于触发；{evidence}" if evidence else "未确认估算，不用于触发"
                    )
                continue
            scores[dimension] = round(score, 3)
            if evidence:
                reasons[dimension] = evidence
        return scores, reasons, estimates, estimate_reasons

    if not isinstance(payload, Mapping):
        return {
            "status": "invalid",
            "score": None,
            "reason": "",
            "decision": None,
            "sub_scores": {},
            "sub_score_reasons": {},
        }
    status = str(payload.get("status") or "invalid")
    decision = _public_decision(payload.get("decision"), type_key)
    total = None if status in _SCORELESS_STATUSES or decision["missing_dimensions"] else _finite(payload.get("total"))
    reasons = payload.get("reasons")
    public_reason = ""
    if isinstance(reasons, Mapping):
        reason_keys = (
            ("_scope", "_condition", "_veto", "_missing", "_downgrade", "_risk")
            if status == "not_triggered"
            else _PUBLIC_META_REASON_KEYS
        )
        public_reason = next(
            (
                _public_reason_text(reasons[key])
                for key in reason_keys
                if isinstance(reasons.get(key), str) and _public_reason_text(reasons[key])
            ),
            "",
        )
        if not public_reason:
            sub_scores = payload.get("sub_scores")
            ranked_dimensions = (
                sorted(
                    (
                        (float(score), order, str(key))
                        for order, (key, raw_score) in enumerate(sub_scores.items())
                        if (score := _finite(raw_score)) is not None
                    )
                )
                if isinstance(sub_scores, Mapping)
                else []
            )
            public_reason = next(
                (
                    _public_reason_text(reasons[key])
                    for _score, _order, key in ranked_dimensions
                    if isinstance(reasons.get(key), str) and _public_reason_text(reasons[key])
                ),
                "",
            )
    sub_scores, sub_score_reasons, estimated_sub_scores, estimated_sub_score_reasons = public_dimensions(
        payload,
        status,
    )
    compact = {
        "status": status,
        "score": round(total, 3) if total is not None else None,
        "reason": public_reason,
        "decision": decision,
        # Keep these two booleans in the lightweight catalogue as well as the
        # detail shards.  The website needs them to distinguish an honest
        # scope exclusion from an applicable framework whose missing evidence
        # is currently bounded by a confirmed veto.
        "applicable": payload.get("applicable") is True,
        "evidence_complete": payload.get("evidence_complete") is True,
    }
    # Type 6's 6e is deliberately not inferred from public company data: it
    # depends on the investor's actual single-name and portfolio exposure.
    # Do not turn the calculated total into a signal, but do expose it as a
    # plainly labelled diagnostic so the quantified company-side work is not
    # hidden merely because the final position confirmation is absent.
    if type_key == "type6" and _is_type6_investor_action_only(decision):
        diagnostic_total = _finite(payload.get("total"))
        if diagnostic_total is not None:
            compact["diagnostic_score"] = round(diagnostic_total, 3)
            compact["diagnostic_score_note"] = "模型诊断分；未确认实际仓位，不构成买入信号"
    # Type 6's 6e is a portfolio action supplied by the investor, not an
    # unknown company fact.  Keep that distinction even before the company
    # reaches the conditional state: consumers must not inflate a missing
    # position confirmation into a source-data gap.  ``action_required``
    # below remains deliberately narrower and means that the action is live
    # right now.
    if type_key == "type6" and "6e" in decision["missing_dimensions"]:
        compact["investor_action_dimensions"] = ["6e"]
    if type_key == "type7":
        type7_ledger = payload.get("ledger")
        if isinstance(type7_ledger, Mapping) and type7_ledger.get("model_id") == _TYPE7_CURRENT_MODEL_ID:
            compact["quality_complete"] = type7_ledger.get("quality_complete") is True
            compact["quality_certified"] = type7_ledger.get("quality_certified") is True
            compact["buy_ready"] = type7_ledger.get("buy_ready") is True
    if isinstance(reasons, Mapping) and isinstance(reasons.get("_missing"), str):
        evidence_gap = _public_reason_text(reasons["_missing"])
        if evidence_gap and evidence_gap != public_reason:
            compact["evidence_gap"] = evidence_gap
    if type_key == "type1" and isinstance(reasons, Mapping) and "_patch7_gate" in reasons:
        patch7_gate = reasons.get("_patch7_gate")
        if not isinstance(patch7_gate, str):
            raise MobileSnapshotError("type1 Patch 7 gate summary is malformed")
        compact["patch7_gate"] = _public_patch7_gate(patch7_gate, status)
    # Empty maps are omitted from the all-company catalogue to keep the
    # bounded mobile asset small enough for the Android decompression limit.
    # The detail payload restores both maps as empty objects, so clients have
    # one stable shape when they open a company and can render "不适用" or
    # "资料不足" without mistaking omitted data for a zero score.
    if sub_scores:
        compact["sub_scores"] = sub_scores
    if sub_score_reasons:
        compact["sub_score_reasons"] = sub_score_reasons
    if estimated_sub_scores:
        compact["estimated_sub_scores"] = estimated_sub_scores
    if estimated_sub_score_reasons:
        compact["estimated_sub_score_reasons"] = estimated_sub_score_reasons
    if type_key == "type6" and _is_live_type6_position_confirmation(status, decision) and isinstance(reasons, Mapping):
        recommendation = _public_reason_text(reasons.get("6e"))
        worst_case = _public_reason_text(reasons.get("_risk"))
        compact["action_required"] = "position_confirmation"
        compact["position_guidance"] = {
            "recommendation": recommendation or "请确认计划仓位",
            "hard_caps": "硬上限：单票不超过5%，此类组合不超过15%",
            "worst_case_loss": worst_case or "请按最坏归零情景核对组合损失",
        }
    return compact


def _public_type_detail(payload: Any, type_key: str) -> dict[str, Any]:
    compact = _compact_type(payload, type_key)
    if not isinstance(payload, Mapping):
        return compact
    reasons = payload.get("reasons")
    public_reasons = {}
    if isinstance(reasons, Mapping):
        sub_scores = payload.get("sub_scores")
        public_keys = [
            *_PUBLIC_DETAIL_META_REASON_KEYS,
            *(str(key) for key in sub_scores if isinstance(sub_scores, Mapping)),
        ]
        public_reasons = dict.fromkeys(
            key for key in public_keys if isinstance(reasons.get(key), str) and _public_reason_text(reasons[key])
        )
        public_reasons = {key: _public_reason_text(reasons[key]) for key in public_reasons}
    compact.update(
        {
            "sub_scores": compact.get("sub_scores", {}),
            "sub_score_reasons": compact.get("sub_score_reasons", {}),
            "reasons": public_reasons,
            "veto": payload.get("veto") is True,
            "applicable": payload.get("applicable") is True,
            "evidence_complete": payload.get("evidence_complete") is True,
        }
    )
    if type_key == "type7":
        compact["method_detail"] = _public_type7_method_detail(payload)
    return compact


def _public_percent(value: Any, *, digits: int = 1) -> str:
    number = _finite(value)
    return "待补" if number is None else f"{number * 100:.{digits}f}%"


def _public_count(value: Any) -> str:
    number = _finite(value)
    return "待补" if number is None else str(max(0, int(round(number))))


def _public_type7_classification_evidence(key: str, inputs: Mapping[str, Any], *, complete: bool) -> str:
    """Explain one Type 7 classification component without internal rule labels."""

    industry = public_industry_name(inputs.get("industry")) if inputs.get("industry") else "行业资料待补"
    explanations = {
        "c_margin_volatility": (
            f"使用{_public_count(inputs.get('observations'))}个年度毛利率，年度波动标准差为"
            f"{_finite(inputs.get('gross_margin_std_pp')):.1f}个百分点。"
            if _finite(inputs.get("gross_margin_std_pp")) is not None
            else "需要连续年度毛利率，计算其年度波动标准差。"
        ),
        "c_profit_elasticity": (
            "把每年利润变化幅度除以收入变化幅度，再取绝对值的中位数；"
            + (
                f"当前为{_finite(inputs.get('median_elasticity')):.2f}，使用"
                f"{_public_count(inputs.get('observations'))}组年度变化。"
                if _finite(inputs.get("median_elasticity")) is not None
                else "当前缺少可对齐的年度收入与归母净利润变化。"
            )
        ),
        "c_capex_intensity": (
            f"使用{_public_count(inputs.get('observations'))}个年度，资本开支占收入平均为"
            f"{_public_percent(inputs.get('mean_capex_revenue'))}。"
            if _finite(inputs.get("mean_capex_revenue")) is not None
            else "需要同一年度的资本开支与营业收入，计算多年平均占比。"
        ),
        "c_commodity_driver": (
            f"按{industry}的商品价格和产能周期特征作间接判断；没有把行业标签当成公司产品价格原始数据。"
        ),
        "t_rd_intensity": (
            f"最新年度研发费用占营业收入{_public_percent(inputs.get('rd_intensity'))}。"
            if _finite(inputs.get("rd_intensity")) is not None
            else "需要最新年度研发费用和营业收入。"
        ),
        "t_intangible_patent": (f"按{industry}作间接判断；当前没有使用公司的专利清单或无形资产占比原始资料。"),
        "t_iteration": (f"按{industry}常见的技术与产品迭代速度作间接判断；当前没有使用公司的产品路线图。"),
        "t_platform": (
            "综合技术证据分"
            f"{_finite(inputs.get('technology_score')):.1f}、商业模式证据分"
            f"{_finite(inputs.get('business_model_score')):.1f}，并受该行业最高"
            f"{_finite(inputs.get('cap')):.1f}分限制。"
            if all(
                _finite(inputs.get(name)) is not None for name in ("technology_score", "business_model_score", "cap")
            )
            else "需要技术、商业模式及所属行业三方面资料共同判断。"
        ),
        "n_repeat": f"按{industry}的必选性与重复消费特征作间接判断；当前没有使用公司客户复购率原始资料。",
        "n_macro_beta": (
            f"以多年经营稳定分{_finite(inputs.get('stability_score')):.1f}作间接判断；这不是实测的利润与国内生产总值敏感系数。"
            if _finite(inputs.get("stability_score")) is not None
            else "需要多年利润与增长稳定性；当前没有实测的利润与国内生产总值敏感系数。"
        ),
        "n_pricing": (
            f"毛利率为{_public_percent(inputs.get('gross_margin'))}，毛利率相对波动为"
            f"{_public_percent(inputs.get('gross_margin_cv'))}；这是财务结果的间接证据，不等同于产品提价与销量原始研究。"
            if _finite(inputs.get("gross_margin")) is not None and _finite(inputs.get("gross_margin_cv")) is not None
            else "需要毛利率水平及多年波动数据；没有用单一行业标签代替定价能力。"
        ),
        "n_mindshare": (
            f"综合{industry}的稀缺性与护城河证据分"
            f"{_finite(inputs.get('moat_score')):.1f}；没有把它表述成消费者调查结论。"
            if _finite(inputs.get("moat_score")) is not None
            else f"综合{industry}的稀缺性与护城河资料；当前护城河证据仍待补。"
        ),
    }
    explanation = explanations.get(key, "说明该项实际使用的公开资料及计算依据。")
    return explanation if complete else f"资料尚未齐全；{explanation}"


def _public_type7_classification_scores(classification: Any) -> list[dict[str, Any]]:
    """Publish all C/T/N totals and their four human-readable components."""

    if not isinstance(classification, Mapping):
        return []
    raw_scores = classification.get("sensitivity_scores")
    raw_upper_bounds = classification.get("sensitivity_upper_bounds")
    raw_components = classification.get("components")
    if not all(isinstance(value, Mapping) for value in (raw_scores, raw_upper_bounds, raw_components)):
        return []
    chosen_code = str(classification.get("class_code") or "")
    selected_sensitivity = "N" if chosen_code == "W" else chosen_code
    output: list[dict[str, Any]] = []
    for sensitivity in ("C", "T", "N"):
        route_name, interpretation = _TYPE7_CLASSIFICATION_NAMES[sensitivity]
        records = raw_components.get(sensitivity)
        if not isinstance(records, list):
            continue
        items: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            key = str(record.get("key") or "")
            meta = _TYPE7_CLASSIFICATION_COMPONENTS.get(key)
            if meta is None:
                continue
            name, evidence_basis, uses_industry_proxy, uses_financial_proxy = meta
            complete = record.get("complete") is True
            evidence_level = str(record.get("evidence_level") or "")
            if key == "t_platform" and evidence_level == "primary":
                evidence_basis = "经核验的技术与商业资料"
                uses_financial_proxy = False
            elif key == "n_mindshare" and evidence_level == "primary":
                evidence_basis = "行业信息与经核验的护城河资料"
                uses_financial_proxy = False
            inputs = record.get("inputs") if isinstance(record.get("inputs"), Mapping) else {}
            awarded = _finite(record.get("awarded_points"))
            maximum = _finite(record.get("max_points"))
            upper = _finite(record.get("upper_bound"))
            missing_inputs = record.get("missing_inputs")
            items.append(
                {
                    "name": name,
                    "score": round(awarded, 3) if complete and awarded is not None else None,
                    "score_lower_bound": round(awarded, 3) if awarded is not None else 0.0,
                    "score_upper_bound": round(upper, 3) if upper is not None else maximum,
                    "max_score": round(maximum, 3) if maximum is not None else None,
                    "complete": complete,
                    "evidence_basis": evidence_basis,
                    "uses_industry_proxy": uses_industry_proxy,
                    "uses_financial_proxy": uses_financial_proxy,
                    "evidence_explanation": _public_type7_classification_evidence(key, inputs, complete=complete),
                    "missing_inputs": [_TYPE7_INPUT_NAMES.get(str(value), "所需数据") for value in missing_inputs]
                    if isinstance(missing_inputs, list)
                    else [],
                }
            )
        if len(items) != 4:
            continue
        complete = all(item["complete"] for item in items)
        score = _finite(raw_scores.get(sensitivity))
        upper = _finite(raw_upper_bounds.get(sensitivity))
        output.append(
            {
                "code": sensitivity,
                "name": route_name,
                "score": round(score, 3) if complete and score is not None else None,
                "score_lower_bound": round(score, 3) if score is not None else 0.0,
                "score_upper_bound": round(upper, 3) if upper is not None else 10.0,
                "max_score": 10.0,
                "complete": complete,
                "selected": sensitivity == selected_sensitivity,
                "threshold": "7分",
                "interpretation": interpretation,
                "items": items,
            }
        )
    return output


def _latest_consecutive_years(value: Any) -> list[int]:
    if not isinstance(value, list) or any(isinstance(year, bool) or not isinstance(year, int) for year in value):
        return []
    years = sorted(set(value))
    if not years:
        return []
    consecutive = [years[-1]]
    for year in reversed(years[:-1]):
        if year == consecutive[-1] - 1:
            consecutive.append(year)
        else:
            break
    return list(reversed(consecutive))


def _public_annual_history_ranges(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose only exact, internally consistent annual ranges already present in evidence."""

    quantitative = row.get("quantitative_evidence")
    if not isinstance(quantitative, Mapping):
        return []
    specifications = (
        (
            "runway_score",
            "financial_history_periods",
            "financial_history_years",
            "营业收入年度历史",
            "连续营业收入年度数据",
        ),
        (
            "moat_durability_score",
            "common_history_years",
            "durability_history_years",
            "耐久度共同历史",
            "投入资本回报率与毛利率共同具备的连续年度",
        ),
    )
    output: list[dict[str, Any]] = []
    for record_key, years_key, count_key, name, basis in specifications:
        record = quantitative.get(record_key)
        details = record.get("details") if isinstance(record, Mapping) else None
        if not isinstance(details, Mapping):
            continue
        years = _latest_consecutive_years(details.get(years_key))
        expected_count = details.get(count_key)
        if (
            not years
            or isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count != len(years)
        ):
            continue
        output.append(
            {
                "name": name,
                "start_year": years[0],
                "end_year": years[-1],
                "year_count": len(years),
                "display": f"{years[0]}–{years[-1]}，共{len(years)}年",
                "basis": basis,
            }
        )
    return output


def _public_type7_method_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the current Type 7 explanation for public company details.

    The full ledger deliberately stays private.  This projection exposes only
    human-readable scores, bounded normalized inputs, formulas and evidence
    states.  Model identifiers and provider payloads never leave the artifact.
    """

    ledger = payload.get("ledger")
    if not isinstance(ledger, Mapping) or ledger.get("model_id") != _TYPE7_CURRENT_MODEL_ID:
        return {
            "status": "outdated",
            "conclusion": "这份第七类结果使用的是旧数据格式，已停止参与判断；请刷新到最新市场数据。",
        }
    if ledger.get("applicable") is False:
        reason = _public_reason_text(ledger.get("reason"))
        return {
            "status": "not_applicable",
            "classification": "不适用",
            "dimensions": [],
            "gates": [],
            "veto": False,
            "failures": [],
            "conclusion": reason or "当前公司不适用第七类模型。",
        }

    classification = ledger.get("classification")
    class_code = str(classification.get("class_code") or "") if isinstance(classification, Mapping) else ""
    class_base_name = {"W": "弱周期", "T": "强科技", "C": "强周期"}.get(class_code, "归类资料不足")
    classification_complete = isinstance(classification, Mapping) and classification.get("route_complete") is True
    class_name = class_base_name if classification_complete else f"暂按{class_base_name}评估"
    possible_classifications = [
        {"W": "弱周期", "T": "强科技", "C": "强周期"}[value]
        for value in (classification.get("possible_classes", []) if isinstance(classification, Mapping) else [])
        if value in {"W", "T", "C"}
    ]
    dimension_names = {"BM": "商业模式", "MOAT": "护城河", "G": "长期成长"}
    evidence_names = {
        "primary": "直接资料",
        "validated_nonfinancial_dcf": "已核验估值资料",
        "reported_observable": "财报可复算数据",
        "derived_proxy": "根据财务表现间接判断",
        "partial": "部分资料",
        "missing": "资料缺失",
    }
    dimensions: list[dict[str, Any]] = []
    raw_dimensions = ledger.get("dimensions")
    for key, name in dimension_names.items():
        section = raw_dimensions.get(key) if isinstance(raw_dimensions, Mapping) else None
        items: list[dict[str, Any]] = []
        raw_items = section.get("items") if isinstance(section, Mapping) else None
        if isinstance(raw_items, list):
            for raw_item in raw_items[:4]:
                if not isinstance(raw_item, Mapping):
                    continue
                missing_inputs = raw_item.get("missing_inputs")
                adjustment_note = ""
                raw_inputs = raw_item.get("inputs")
                if (
                    class_code == "T"
                    and raw_item.get("key") == "cashflow_inflection"
                    and isinstance(raw_inputs, Mapping)
                    and (penalty := _finite(raw_inputs.get("cycle_overlay_penalty"))) is not None
                    and penalty > 0
                ):
                    adjustment_note = f"周期叠加扣减{penalty:.2f}分"
                complete_item = raw_item.get("complete") is True
                item_score = _finite(raw_item.get("score"))
                item_upper = _finite(raw_item.get("upper_bound"))
                weight = _finite(raw_item.get("weight"))
                input_rows: list[dict[str, Any]] = []
                if isinstance(raw_inputs, Mapping):
                    for input_key, input_value in raw_inputs.items():
                        if input_value is None:
                            public_value: Any = "待补"
                        elif (numeric_value := _finite(input_value)) is not None:
                            public_value = round(numeric_value, 6)
                        elif input_key == "industry":
                            public_value = public_industry_name(str(input_value))
                        else:
                            public_value = _public_reason_text(input_value)
                        input_rows.append(
                            {
                                "name": _TYPE7_INPUT_NAMES.get(str(input_key), "量化输入"),
                                "value": public_value,
                            }
                        )
                items.append(
                    {
                        "name": _public_reason_text(raw_item.get("label")) or "未命名子指标",
                        "score": round(item_score, 3) if complete_item and item_score is not None else None,
                        "score_lower_bound": round(item_score, 3) if item_score is not None else 0.0,
                        "score_upper_bound": round(item_upper, 3) if item_upper is not None else 10.0,
                        "weight_percent": round(weight * 100, 3) if weight is not None else None,
                        "weighted_contribution": (
                            round(item_score * weight, 3)
                            if complete_item and item_score is not None and weight is not None
                            else None
                        ),
                        "calculation": _TYPE7_FORMULAS.get(
                            (class_code, str(raw_item.get("key") or "")),
                            "按公开量化输入映射为0至10分",
                        ),
                        "score_cap": (
                            round(score_cap, 3)
                            if (score_cap := _finite(raw_item.get("proxy_cap"))) is not None
                            else None
                        ),
                        "inputs": input_rows,
                        "evidence": evidence_names.get(
                            str(raw_item.get("evidence_level") or "missing"),
                            "待核验资料",
                        ),
                        "complete": complete_item,
                        "missing_input_count": len(missing_inputs) if isinstance(missing_inputs, list) else 0,
                        "missing_inputs": [_TYPE7_INPUT_NAMES.get(str(value), "量化输入") for value in missing_inputs]
                        if isinstance(missing_inputs, list)
                        else [],
                        "adjustment_note": adjustment_note,
                    }
                )
        dimensions.append(
            {
                "name": name,
                "score": (
                    round(section_score, 3)
                    if isinstance(section, Mapping)
                    and section.get("complete") is True
                    and (section_score := _finite(section.get("score"))) is not None
                    else None
                ),
                "score_lower_bound": (
                    round(section_lower, 3)
                    if isinstance(section, Mapping) and (section_lower := _finite(section.get("score"))) is not None
                    else 0.0
                ),
                "score_upper_bound": (
                    round(section_upper, 3)
                    if isinstance(section, Mapping)
                    and (section_upper := _finite(section.get("upper_bound"))) is not None
                    else 10.0
                ),
                "coverage": (
                    round(coverage, 3)
                    if isinstance(section, Mapping) and (coverage := _finite(section.get("coverage"))) is not None
                    else None
                ),
                "complete": isinstance(section, Mapping) and section.get("complete") is True,
                "items": items,
            }
        )

    gate_names = {
        "future_fcf": "未来自由现金流前提",
        "gdN_investability": "gdN 可投滤网",
        "route_path": "按公司类别检查买点条件",
        "price_reasonableness": "第七类自身价格检查",
    }
    gates: list[dict[str, Any]] = []
    raw_gates = ledger.get("decision_gates")
    for key, name in gate_names.items():
        gate = raw_gates.get(key) if isinstance(raw_gates, Mapping) else None
        complete = isinstance(gate, Mapping) and gate.get("complete") is True
        passed = isinstance(gate, Mapping) and gate.get("passed") is True
        if key == "price_reasonableness" and isinstance(gate, Mapping) and gate.get("required") is not True:
            complete = False
            passed = False
        detail = ""
        if isinstance(gate, Mapping):
            if key == "future_fcf":
                published_basis = str(gate.get("basis") or "").strip()
                years = gate.get("years")
                positive_share = _finite(gate.get("positive_share"))
                latest_fcf = _finite(gate.get("latest_fcf"))
                if published_basis:
                    detail = published_basis
                elif complete and isinstance(years, list):
                    detail = (
                        f"连续{len(years)}个年度；自由现金流为正占比"
                        f"{positive_share * 100:.0f}%；最新年度{'为正' if latest_fcf is not None and latest_fcf > 0 else '未转正'}"
                        if positive_share is not None
                        else f"连续{len(years)}个年度；正值占比待核对"
                    )
                else:
                    detail = "需要截至最新财年的连续3至5年自由现金流数据"
            elif key == "route_path":
                detail = {
                    "W": "核对模板五的历史估值、自由现金流折现、预期回报三项，以及综合安全边际",
                    "T": "核对科技股长期激励、综合安全边际和历史估值资料",
                    "C": (
                        "第五类必须适用、证据完整、已经触发且总分不低于7分；"
                        "还要明确带息债务和货币资金，并用两者差额核对净债后再检查多情景估值"
                    ),
                }.get(class_code, "分类路径资料待核对")
            elif key == "gdN_investability":
                detail = str(gate.get("rule") or "gdN 可投滤网：g 增长引擎 × d 分红引擎 × N 时间")
            elif gate.get("required") is not True:
                detail = "这代数据曾错误省略第七类自身价格检查，请刷新到最新数据后再判断"
            else:
                price_inputs = gate.get("inputs") if isinstance(gate.get("inputs"), Mapping) else {}
                pb_percentile = _finite(price_inputs.get("pb_percentile"))
                current_pb = _finite(price_inputs.get("current_pb"))
                if class_code == "W":
                    detail = "价格位置分需不低于3分；即使其他买入情况已触发，本项也不能省略"
                elif class_code == "T":
                    actual = f"；当前为{pb_percentile * 100:.1f}%" if pb_percentile is not None else ""
                    detail = (
                        "近五年市净率分位需不高于20%"
                        + actual
                        + "。20%是程序对“处于历史底部区”的量化定义，不是模板原文中的固定数字"
                    )
                elif class_code == "C":
                    actual_parts = []
                    if current_pb is not None:
                        actual_parts.append(f"当前市净率{current_pb:.2f}")
                    if pb_percentile is not None:
                        actual_parts.append(f"近五年分位{pb_percentile * 100:.1f}%")
                    actual = "；" + "，".join(actual_parts) if actual_parts else ""
                    detail = (
                        "当前市净率需不高于1.20，且近五年市净率分位需不高于20%"
                        + actual
                        + "。1.20和20%是程序分别对“接近净资产”和“处于历史底部区”的量化定义"
                    )
                else:
                    detail = "本类别的价格资料待核对；其他买入情况不能免除第七类自身价格检查"
        gates.append(
            {
                "name": name,
                "status": ("通过" if complete and passed else "未通过" if complete else "待补资料"),
                "detail": detail or "暂无补充说明",
            }
        )
    if class_code == "T":
        technology_dimensions_complete = all(
            isinstance(raw_dimensions, Mapping)
            and isinstance(raw_dimensions.get(key), Mapping)
            and raw_dimensions[key].get("complete") is True
            for key in ("BM", "MOAT", "G")
        )
        technology_dimension_floor_passed = bool(
            technology_dimensions_complete
            and all(
                (score := _finite(raw_dimensions[key].get("score"))) is not None and score >= 7.0
                for key in ("BM", "MOAT", "G")
            )
        )
        gates.append(
            {
                "name": "强科技三维逐项门槛",
                "status": (
                    "通过"
                    if technology_dimension_floor_passed
                    else "未通过"
                    if technology_dimensions_complete
                    else "待补资料"
                ),
                "detail": "商业模式、护城河、长期成长三项必须各自不低于7分；高分项不能抵消低分项。",
            }
        )

    failure_names = {
        "future_fcf": "未来自由现金流前提未通过",
        "route_path": "本类别的买点条件未通过",
        "price_reasonableness": "价格合理性未通过",
        "technology_dimension_floor": "强科技商业模式、护城河、长期成长未全部达到7分",
    }
    failures = [failure_names[value] for value in ledger.get("condition_failures", []) if value in failure_names]
    veto_names = {"BM": "强周期商业模式低于5分", "MOAT": "强周期护城河低于5分"}
    failures.extend(veto_names[value] for value in ledger.get("veto_dimensions", []) if value in veto_names)
    quality_complete = ledger.get("quality_complete") is True
    mean_lower_bound = _finite(ledger.get("unrounded_mean"))
    mean_upper_bound = _finite(ledger.get("upper_bound"))
    mean_score = mean_lower_bound if quality_complete else None
    if ledger.get("triggered") is True:
        technology_note = "强科技三项也分别达到7分；" if class_code == "T" else ""
        conclusion = (
            "优质股权质量已达标：三项平均分严格大于7分；"
            + technology_note
            + "现金流、本类别买点条件、否决检查和第七类自身价格检查均通过。"
        )
    elif ledger.get("quality_certified") is True:
        action_gap = "；".join(failures) if failures else "买入前置资料尚未补齐"
        conclusion = f"优质股权质量已达标，但当前不构成第七类买点：{action_gap}。"
    elif failures:
        conclusion = "当前未触发：" + "；".join(failures)
    elif ledger.get("complete") is not True:
        conclusion = "必需子指标或前置资料尚未补齐，当前不触发。"
    else:
        conclusion = "三项平均分未严格大于7分，当前不触发。"
    return {
        "status": "current",
        "classification": class_name,
        "classification_complete": classification_complete,
        "possible_classifications": possible_classifications,
        "secondary_features": [
            value
            for value in (classification.get("secondary_features", []) if isinstance(classification, Mapping) else [])
            if value in {"弱周期", "强科技", "强周期"}
        ],
        "possible_secondary_features": [
            value
            for value in (
                classification.get("possible_secondary_features", []) if isinstance(classification, Mapping) else []
            )
            if value in {"弱周期", "强科技", "强周期"}
        ],
        "classification_scores": _public_type7_classification_scores(classification),
        "mean_score": round(mean_score, 3) if mean_score is not None else None,
        "mean_lower_bound": round(mean_lower_bound, 3) if mean_lower_bound is not None else 0.0,
        "mean_upper_bound": round(mean_upper_bound, 3) if mean_upper_bound is not None else 10.0,
        "quality_complete": quality_complete,
        "quality_certified": ledger.get("quality_certified") is True,
        "buy_ready": ledger.get("buy_ready") is True,
        "dimensions": dimensions,
        "gates": gates,
        "veto": ledger.get("veto") is True,
        "failures": failures,
        "conclusion": conclusion,
    }


def _catalog_company(row: Mapping[str, Any]) -> dict[str, Any]:
    type_payloads = {type_key: _compact_type(row.get(type_key), type_key) for type_key in _TYPE_KEYS}
    buy_types = [str(value) for value in row.get("buy_types", []) if str(value) in _TYPE_KEYS]
    conditional_types = [type_key for type_key, payload in type_payloads.items() if payload["status"] == "conditional"]
    # ``potentially_triggerable`` is true for actual triggers as well.  The
    # separate pending list contains only unresolved evidence candidates that
    # are neither a real signal nor the existing Type 6 action condition.
    pending_types = [
        type_key
        for type_key, payload in type_payloads.items()
        if isinstance(payload.get("decision"), Mapping)
        and payload["decision"].get("potentially_triggerable") is True
        and payload["status"] not in {"triggered", "conditional"}
    ]
    # The engine's diagnostic maximum may legitimately use an incomplete
    # framework as an internal triage hint.  Public clients cannot attach a
    # precise company-level score to that placeholder total.  Recompute the
    # public diagnostic maximum exclusively from compact types whose complete
    # evidence contract permits an exact score.
    diagnostic_candidates = [
        (float(payload["score"]), -order, type_key)
        for order, type_key in enumerate(_TYPE_KEYS)
        if (payload := type_payloads[type_key]).get("score") is not None
    ]
    if diagnostic_candidates:
        diagnostic_score, _tie_breaker, diagnostic_type = max(diagnostic_candidates)
        diagnostic_label = TYPE_NAMES[diagnostic_type]
    else:
        diagnostic_score, diagnostic_type, diagnostic_label = None, "", ""
    raw_industry = str(row.get("industry_code") or row.get("industry") or "").strip()
    return {
        "code": _normalise_code(row.get("code")),
        "name": str(row.get("name") or ""),
        # ``industry`` is the public display contract consumed by existing
        # Android clients.  Keep the model enum separately for diagnostics so
        # values such as ``ALCOHOL`` never become an end-user label.
        "industry": public_industry_name(row.get("industry"), explicit_name=row.get("industry_cn")),
        "industry_code": raw_industry,
        "price": _finite(row.get("price")),
        "pe": _finite(row.get("pe")),
        "pb": _finite(row.get("pb")),
        "market_cap": _finite(row.get("market_cap")),
        "buy_types": buy_types,
        "conditional_types": conditional_types,
        "pending_types": pending_types,
        "primary_type": str(row.get("primary_type") or ""),
        "primary_label": str(row.get("primary_label") or ""),
        "diagnostic_type": diagnostic_type,
        "diagnostic_label": diagnostic_label,
        "diagnostic_score": diagnostic_score,
        "types": type_payloads,
    }


def _signal_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    catalog = _catalog_company(row)
    type_details = {type_key: _public_type_detail(row.get(type_key), type_key) for type_key in _TYPE_KEYS}
    detail_lines = [f"{catalog['name']} {catalog['code']}"]
    for type_key in _TYPE_KEYS:
        payload = type_details[type_key]
        status = _STATUS_LABELS.get(str(payload.get("status")), "资料异常")
        score = payload.get("score")
        score_text = "" if score is None else f"，{float(score):.1f}分"
        detail_lines.append(f"{TYPE_NAMES[type_key]}：{status}{score_text}")
        reasons = payload.get("reasons")
        if isinstance(reasons, Mapping):
            for reason in dict.fromkeys(str(value).strip() for value in reasons.values() if str(value).strip()):
                detail_lines.append(f"  说明：{reason}")
    return {
        **catalog,
        "type_details": type_details,
        "detail_text": "\n".join(detail_lines),
    }


def _company_detail_shard_id(code: str) -> str:
    """Return the stable first-nibble SHA-256 partition for one company code."""

    digest = hashlib.sha256(code.encode("ascii")).digest()
    return f"{digest[0] >> 4:02x}"


def _company_detail_v2(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one validated score row into the public website detail contract."""

    catalog = _catalog_company(row)
    detail = {
        "schema_version": COMPANY_DETAIL_SCHEMA_VERSION,
        **{key: value for key, value in catalog.items() if key != "types"},
        "types": {type_key: _public_type_detail(row.get(type_key), type_key) for type_key in _TYPE_KEYS},
    }
    source_trade_date = str(row.get("source_trade_date") or "").strip()
    if source_trade_date and source_trade_date.casefold() not in {"nan", "nat", "none"}:
        detail["source_trade_date"] = source_trade_date
    annual_history = _public_annual_history_ranges(row)
    if annual_history:
        detail["annual_history"] = annual_history
    return detail


def _requires_position_confirmation(company: Mapping[str, Any]) -> bool:
    """Return true only for a live Type 6 position-confirmation action gate."""

    types = company.get("types")
    type6 = types.get("type6") if isinstance(types, Mapping) else None
    if not isinstance(type6, Mapping):
        return False
    decision = type6.get("decision")
    return (
        type6.get("action_required") == "position_confirmation"
        and isinstance(decision, Mapping)
        and _is_live_type6_position_confirmation(str(type6.get("status") or ""), decision)
    )


def _build_company_detail_shards(
    raw_records: Sequence[Mapping[str, Any]],
    *,
    generated_at_utc: str,
    market_as_of: str,
    data_timestamp_utc: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    shard_ids = tuple(f"{index:02x}" for index in range(COMPANY_DETAIL_SHARD_COUNT))
    companies_by_shard: dict[str, list[dict[str, Any]]] = {shard_id: [] for shard_id in shard_ids}
    for row in raw_records:
        detail = _company_detail_v2(row)
        shard_id = _company_detail_shard_id(detail["code"])
        companies_by_shard[shard_id].append(detail)

    payloads: dict[str, dict[str, Any]] = {}
    root_entries: list[dict[str, Any]] = []
    for shard_id in shard_ids:
        companies = sorted(companies_by_shard[shard_id], key=lambda item: item["code"])
        payload = {
            "schema_version": COMPANY_DETAIL_SCHEMA_VERSION,
            "record_schema": "company_detail_v2",
            "product": "DS_DCF",
            "generated_at_utc": generated_at_utc,
            "market_as_of": market_as_of,
            "data_timestamp_utc": data_timestamp_utc,
            "shard_id": shard_id,
            "company_count": len(companies),
            "companies": companies,
        }
        raw = _canonical_json_bytes(payload)
        payloads[shard_id] = payload
        root_entries.append(
            {
                "id": shard_id,
                "company_count": len(companies),
                "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    root_contract = {
        "schema_version": COMPANY_DETAIL_SCHEMA_VERSION,
        "record_schema": "company_detail_v2",
        "partition": {
            "algorithm": "sha256_code_first_nibble",
            "shard_count": COMPANY_DETAIL_SHARD_COUNT,
        },
        "shards": root_entries,
    }
    descriptor = {
        "schema_version": COMPANY_DETAIL_SCHEMA_VERSION,
        "record_schema": "company_detail_v2",
        "company_count": len(raw_records),
        "partition": dict(root_contract["partition"]),
        "root_algorithm": "SHA256_CANONICAL_SHARD_INDEX_V1",
        "root_sha256": hashlib.sha256(_canonical_json_bytes(root_contract)).hexdigest(),
        "shards": root_entries,
    }
    return payloads, descriptor


def _type_coverage(companies: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for type_key in _TYPE_KEYS:
        statuses = Counter(str(company["types"][type_key]["status"]) for company in companies)
        result[type_key] = {
            "triggered": int(statuses.get("triggered", 0)),
            "conditional": int(statuses.get("conditional", 0)),
            "observe": int(statuses.get("observe", 0)),
            "insufficient_evidence": int(statuses.get("insufficient_evidence", 0)),
            "vetoed": int(statuses.get("vetoed", 0)),
            "not_triggered": int(statuses.get("not_triggered", 0)),
            "not_applicable": int(statuses.get("not_applicable", 0)),
            "blocked": int(statuses.get("blocked", 0)),
        }
    return result


def _build_mobile_snapshot_bundle(
    scores: pd.DataFrame,
    *,
    market_as_of: str,
    data_timestamp_utc: str,
    analysis_quality: Mapping[str, Any],
    dcf_results: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Build portable manifest, catalogue, and candidate-detail payloads.

    ``scores`` is the fully validated production screen result.  No external
    source is contacted, so the function is deterministic and straightforward
    to exercise in release CI.
    """
    if not isinstance(scores, pd.DataFrame) or scores.empty:
        raise MobileSnapshotError("mobile snapshot requires a non-empty score frame")
    required = {"code", "buy_types", "primary_type", "diagnostic_type", "max_score", *_TYPE_KEYS}
    missing = sorted(required - set(scores.columns))
    if missing:
        raise MobileSnapshotError("score frame omits required fields: " + ",".join(missing))
    invariant_errors = validate_screening_result(scores)
    if invariant_errors:
        raise MobileSnapshotError("score frame invariant failed: " + invariant_errors[0])
    if not isinstance(market_as_of, str) or len(market_as_of) != 10:
        raise MobileSnapshotError("market_as_of must use YYYY-MM-DD")
    if not isinstance(data_timestamp_utc, str) or not data_timestamp_utc:
        raise MobileSnapshotError("data_timestamp_utc is required")
    if not isinstance(analysis_quality, Mapping) or analysis_quality.get("ok") is not True:
        raise MobileSnapshotError("analysis quality gate did not pass")

    # The mobile client intentionally receives only compact scores and public
    # explanations.  Full valuation ledgers remain in the desktop audit and
    # are not exposed merely because callers already have them in memory.
    del dcf_results
    raw_records = scores.to_dict(orient="records")
    companies = [_catalog_company(row) for row in raw_records]
    codes = [company["code"] for company in companies]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        raise MobileSnapshotError("mobile catalogue contains invalid or duplicate codes")
    companies.sort(key=lambda item: item["code"])
    coverage = _type_coverage(companies)
    type7_quality_certified_company_count = sum(
        1 for company in companies if company["types"]["type7"].get("quality_certified") is True
    )
    signal_codes = {company["code"] for company in companies if company["buy_types"] or company["conditional_types"]}
    raw_rows = {_normalise_code(row.get("code")): row for row in raw_records}
    signals = [_signal_detail(raw_rows[code]) for code in sorted(signal_codes)]

    try:
        generated_at = datetime.fromisoformat(data_timestamp_utc).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError) as exc:
        raise MobileSnapshotError("data_timestamp_utc must be an ISO-8601 timestamp") from exc
    shared = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "product": "DS_DCF",
        "capabilities": {
            "dimension_scores": True,
            "dimension_score_estimates": True,
            "decision_contract": True,
            "type7_method_detail": True,
        },
        "generated_at_utc": generated_at,
        "market_as_of": market_as_of,
        "data_timestamp_utc": data_timestamp_utc,
        "type_names": dict(TYPE_NAMES),
        "analysis_quality": _json_safe(dict(analysis_quality)),
        "provenance": _json_safe(dict(provenance or {})),
    }
    catalogue = {
        **shared,
        "coverage": _type_coverage(companies),
        "company_count": len(companies),
        "companies": companies,
    }
    triggered_company_count = sum(1 for company in companies if company["buy_types"])
    conditional_company_count = sum(1 for company in companies if company["conditional_types"])
    action_confirmation_company_count = sum(1 for company in companies if _requires_position_confirmation(company))
    conditional_only_company_count = sum(
        1 for company in companies if company["conditional_types"] and not company["buy_types"]
    )
    pending_company_count = sum(1 for company in companies if company["pending_types"])
    visible_candidate_company_count = sum(
        1 for company in companies if company["buy_types"] or company["conditional_types"] or company["pending_types"]
    )
    candidate_detail_count = len(signals)
    signal_payload = {
        **shared,
        # A detailed record can represent either a real buy signal or a
        # conditional candidate.  Keep the two counts separate so a client
        # never presents a missing portfolio confirmation as a buy signal.
        "triggered_company_count": triggered_company_count,
        "conditional_company_count": conditional_company_count,
        "action_confirmation_company_count": action_confirmation_company_count,
        "conditional_only_company_count": conditional_only_company_count,
        # Pending evidence candidates live in the all-company catalogue so
        # older 11.2 clients can keep accepting the historical ``signals``
        # array contract.  Version 11.3 reads this count and ``pending_types``.
        "pending_company_count": pending_company_count,
        "visible_candidate_company_count": visible_candidate_company_count,
        "candidate_detail_count": candidate_detail_count,
        "signals": signals,
    }
    detail_shards, detail_descriptor = _build_company_detail_shards(
        raw_records,
        generated_at_utc=generated_at,
        market_as_of=market_as_of,
        data_timestamp_utc=data_timestamp_utc,
    )
    generation = hashlib.sha256(
        _canonical_json_bytes(catalogue)
        + b"\0"
        + _canonical_json_bytes(signal_payload)
        + b"\0company-details-v2\0"
        + bytes.fromhex(detail_descriptor["root_sha256"])
    ).hexdigest()[:16]
    detail_descriptor = {
        **detail_descriptor,
        "shards": [
            {
                **entry,
                "filename": COMPANY_DETAIL_FILENAME.format(generation=generation, shard_id=entry["id"]),
            }
            for entry in detail_descriptor["shards"]
        ],
    }
    manifest = {
        **shared,
        "catalogue": {"filename": CATALOG_FILENAME.format(generation=generation)},
        "signals": {"filename": SIGNALS_FILENAME.format(generation=generation)},
        "company_details": detail_descriptor,
        "signature": {
            "filename": SIGNATURE_FILENAME.format(generation=generation),
            "algorithm": "ECDSA_P256_SHA256",
        },
        "summary": {
            "company_count": len(companies),
            "triggered_company_count": triggered_company_count,
            "conditional_company_count": conditional_company_count,
            "action_confirmation_company_count": action_confirmation_company_count,
            "conditional_only_company_count": conditional_only_company_count,
            "pending_company_count": pending_company_count,
            "visible_candidate_company_count": visible_candidate_company_count,
            "candidate_detail_count": candidate_detail_count,
            # Keep the established eight-status ``type_coverage`` contract
            # byte-shape compatible with already installed Android clients.
            # Type 7 quality certification is an overlay, not a ninth status.
            "type7_quality_certified_company_count": type7_quality_certified_company_count,
            "type_coverage": coverage,
        },
    }
    return manifest, catalogue, signal_payload, detail_shards


def build_mobile_snapshot(
    scores: pd.DataFrame,
    *,
    market_as_of: str,
    data_timestamp_utc: str,
    analysis_quality: Mapping[str, Any],
    dcf_results: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build the backwards-compatible catalogue/signals view and signed manifest."""

    manifest, catalogue, signals, _detail_shards = _build_mobile_snapshot_bundle(
        scores,
        market_as_of=market_as_of,
        data_timestamp_utc=data_timestamp_utc,
        analysis_quality=analysis_quality,
        dcf_results=dcf_results,
        provenance=provenance,
    )
    return manifest, catalogue, signals


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _gzip_bytes(raw: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as zipped:
        zipped.write(raw)
    return output.getvalue()


def write_mobile_snapshot(
    output_dir: str | Path,
    scores: pd.DataFrame,
    *,
    market_as_of: str,
    data_timestamp_utc: str,
    analysis_quality: Mapping[str, Any],
    dcf_results: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an atomically replaceable mobile snapshot and return its manifest."""
    manifest, catalogue, signals, detail_shards = _build_mobile_snapshot_bundle(
        scores,
        market_as_of=market_as_of,
        data_timestamp_utc=data_timestamp_utc,
        analysis_quality=analysis_quality,
        dcf_results=dcf_results,
        provenance=provenance,
    )
    output = Path(output_dir)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise MobileSnapshotError("mobile snapshot output directory must be absent or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    catalogue_raw = _canonical_json_bytes(catalogue)
    signals_raw = _canonical_json_bytes(signals)
    detail_raw = {shard_id: _canonical_json_bytes(payload) for shard_id, payload in detail_shards.items()}
    detail_uncompressed_total = sum(len(raw) for raw in detail_raw.values())
    if detail_uncompressed_total > MAX_DETAIL_UNCOMPRESSED_TOTAL:
        raise MobileSnapshotError(
            "company detail shards exceed the uncompressed total limit: "
            f"{detail_uncompressed_total} > {MAX_DETAIL_UNCOMPRESSED_TOTAL}"
        )
    raw_assets = [
        ("catalogue", catalogue_raw),
        ("signals", signals_raw),
        *((f"company detail shard {shard_id}", raw) for shard_id, raw in detail_raw.items()),
    ]
    for label, raw in raw_assets:
        if len(raw) > MAX_UNCOMPRESSED_ASSET_BYTES:
            raise MobileSnapshotError(
                f"{label} exceeds the Android uncompressed limit: {len(raw)} > {MAX_UNCOMPRESSED_ASSET_BYTES}"
            )
    catalogue_bytes = _gzip_bytes(catalogue_raw)
    signals_bytes = _gzip_bytes(signals_raw)
    detail_bytes = {shard_id: _gzip_bytes(raw) for shard_id, raw in detail_raw.items()}
    detail_compressed_total = sum(len(compressed) for compressed in detail_bytes.values())
    if detail_compressed_total > MAX_DETAIL_COMPRESSED_TOTAL:
        raise MobileSnapshotError(
            "company detail shards exceed the download total limit: "
            f"{detail_compressed_total} > {MAX_DETAIL_COMPRESSED_TOTAL}"
        )
    compressed_assets = [
        ("catalogue", catalogue_bytes),
        ("signals", signals_bytes),
        *((f"company detail shard {shard_id}", compressed) for shard_id, compressed in detail_bytes.items()),
    ]
    for label, compressed in compressed_assets:
        if len(compressed) > MAX_COMPRESSED_ASSET_BYTES:
            raise MobileSnapshotError(
                f"{label} exceeds the Android download limit: {len(compressed)} > {MAX_COMPRESSED_ASSET_BYTES}"
            )
    manifest = dict(manifest)
    manifest["catalogue"].update(
        {
            "sha256": hashlib.sha256(catalogue_bytes).hexdigest(),
            "size": len(catalogue_bytes),
            "uncompressed_size": len(catalogue_raw),
        }
    )
    manifest["signals"].update(
        {
            "sha256": hashlib.sha256(signals_bytes).hexdigest(),
            "size": len(signals_bytes),
            "uncompressed_size": len(signals_raw),
        }
    )
    detail_metadata_by_id = {str(entry["id"]): entry for entry in manifest["company_details"]["shards"]}
    for shard_id, compressed in detail_bytes.items():
        metadata = detail_metadata_by_id[shard_id]
        raw = detail_raw[shard_id]
        if metadata["uncompressed_sha256"] != hashlib.sha256(raw).hexdigest():
            raise MobileSnapshotError(f"company detail shard {shard_id} root metadata is inconsistent")
        metadata.update(
            {
                "sha256": hashlib.sha256(compressed).hexdigest(),
                "size": len(compressed),
                "uncompressed_size": len(raw),
            }
        )
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent))
    committed = False
    try:
        _atomic_write(staging / manifest["catalogue"]["filename"], catalogue_bytes)
        _atomic_write(staging / manifest["signals"]["filename"], signals_bytes)
        for metadata in manifest["company_details"]["shards"]:
            shard_id = str(metadata["id"])
            _atomic_write(staging / str(metadata["filename"]), detail_bytes[shard_id])
        _atomic_write(staging / MANIFEST_FILENAME, _canonical_json_bytes(manifest) + b"\n")
        if output.exists():
            output.rmdir()
        os.replace(staging, output)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(staging, ignore_errors=True)
    return manifest


__all__ = [
    "CATALOG_FILENAME",
    "COMPANY_DETAIL_FILENAME",
    "COMPANY_DETAIL_SCHEMA_VERSION",
    "COMPANY_DETAIL_SHARD_COUNT",
    "MANIFEST_FILENAME",
    "MobileSnapshotError",
    "SIGNATURE_FILENAME",
    "SIGNALS_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "build_mobile_snapshot",
    "write_mobile_snapshot",
]
