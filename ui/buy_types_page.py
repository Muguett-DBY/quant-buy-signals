"""
七种买入类型量化筛选页
按钮触发数据加载 · 全部公司展示 · 展开看维度得分
"""

import hashlib
import inspect
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Mapping
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from data.public_presentation import public_reason_text

# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

TYPE_ORDER = ["type1", "type2", "type3", "type4", "type5", "type6", "type7"]
TYPE_NAMES = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ 高风险早期/困境型",
    "type7": "7️⃣ 优质股权型",
}
TYPE_DIMENSIONS = {
    "type1": [
        ("1a", "买入区深度", 0.30),
        ("1b", "价值陷阱排查", 0.35),
        ("1c", "安全边际厚度", 0.20),
        ("1d", "催化剂/回归动力", 0.15),
    ],
    "type2": [
        ("2a", "产业周期热度", 0.25),
        ("2b", "公司周期拐点", 0.30),
        ("2c", "市场周期冷度", 0.25),
        ("2d", "估值合理性", 0.20),
    ],
    "type3": [
        ("3a", "护城河支撑度", 0.25),
        ("3b", "增长质量", 0.20),
        ("3c", "资本回报率", 0.20),
        ("3d", "增长可持续性", 0.25),
        ("3e", "产业/股价泡沫", 0.10),
    ],
    "type4": [
        ("4a", "坡的长度", 0.25),
        ("4b", "雪的厚度", 0.25),
        ("4c", "护城河耐久度", 0.20),
        ("4d", "估值合理性", 0.15),
        ("4e", "产业泡沫防范", 0.08),
        ("4f", "股价泡沫防范", 0.07),
    ],
    "type5": [
        ("5a", "强周期属性", 0.35),
        ("5b", "底部信号", 0.25),
        ("5c", "抗周期能力", 0.20),
        ("5d", "上行弹性", 0.10),
        ("5e", "正常化盈利估值", 0.10),
    ],
    "type6": [
        ("6a", "产业爆发", 0.25),
        ("6b", "技术壁垒", 0.20),
        ("6c", "模式创新", 0.15),
        ("6d", "困境反转", 0.25),
        ("6e", "仓位风控", 0.15),
    ],
    "type7": [
        ("7a", "长期质量与回报", 1.0 / 3.0),
        ("7b", "产业质量与估值", 1.0 / 3.0),
        ("7c", "商业质量与安全边际", 1.0 / 3.0),
    ],
}

TYPE6_GLOBAL_RISK_NOTICE = (
    "高风险早期/困境型标的单只股票仓位不得超过5%，此类资产合计仓位不得超过15%；"
    "买入前必须明确并能承受判断错误时的最大损失。"
    "当前自动数据不能直接证明技术突破或商业模式创新；"
    "缺少可复算的结构化原始资料时不会据此给出买入信号，手填结论分数也不会参与买点判定。"
)

MAX_USER_EVIDENCE_BYTES = 1_000_000
MAX_USER_EVIDENCE_DEPTH = 32
MAX_USER_EVIDENCE_NODES = 50_000

_ANALYSIS_GENERATION_STATE_KEYS = (
    "buy_types_df",
    "leaders_df",
    "buy_types_timestamp",
    "buy_types_data_timestamp",
    "buy_types_retrieved_at",
    "buy_types_data_source",
    "buy_types_snapshot_validation",
    "buy_types_snapshot_warning",
    "buy_types_cache_diagnostic",
    "buy_types_analysis_quality",
    "buy_types_dcf_results",
    "buy_types_dcf_skip_reasons",
    "buy_types_dcf_skip_classifications",
    "buy_types_eligible_codes",
    "buy_types_analysis_exclusions",
    "buy_types_user_evidence",
    "buy_types_market_coldness_status",
    "buy_types_pipeline_issues",
    "buy_types_dcf_audit_frame",
    "buy_types_dcf_audit_csv",
    "buy_types_analysis_json",
    "buy_types_generation_identity",
    "buy_types_cache_restore_notice",
)

# These three are presentation exports, not analysis inputs.  The complete
# JSON export alone can exceed 250MB because it duplicates the full nested
# evidence tree already held by the score frame and DCF result mapping.  It
# must never be persisted as part of the start-up cache: the safe cache has a
# deliberately bounded decompression budget and an oversized export used to
# make an otherwise valid generation impossible to restore.
_PERSISTED_ANALYSIS_EXCLUDED_KEYS = {
    "leaders_df",
    "buy_types_dcf_audit_frame",
    "buy_types_dcf_audit_csv",
    "buy_types_analysis_json",
    "buy_types_cache_restore_notice",
}
_PERSISTED_ANALYSIS_STATE_KEYS = tuple(
    key for key in _ANALYSIS_GENERATION_STATE_KEYS if key not in _PERSISTED_ANALYSIS_EXCLUDED_KEYS
)
_ANALYSIS_CACHE_SCHEMA_VERSION = 2
_MAX_ANALYSIS_CACHE_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_ANALYSIS_CACHE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


def _fmt_score(v):
    """格式化评分显示"""
    if isinstance(v, bool):
        return None
    try:
        score = float(v)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _format_metric(value, *, digits: int = 1, scale: float = 1.0, suffix: str = "") -> str:
    """Format a UI metric without exposing technical missing-value markers."""
    number = _fmt_score(value)
    if number is None:
        return "暂无数据"
    return f"{number * scale:.{digits}f}{suffix}"


def _display_reason(value: object) -> str:
    """Defensively hide machine identifiers from all user-facing text."""
    return public_reason_text(value)


_FAILURE_MACHINE_TEXT = re.compile(
    r"(?:[A-Za-z]{3,}|[A-Za-z]:[\\/]|[A-Za-z0-9]+_[A-Za-z0-9_]+)",
)
_PIPELINE_STAGE_LABELS = {
    "identity": "标的身份校验",
    "input": "基础数据校验",
    "dcf": "估值计算",
    "valuation_evidence": "估值证据核验",
}
_PIPELINE_STAGE_MESSAGES = {
    "identity": "股票代码或记录身份存在冲突，已排除该条数据",
    "input": "价格、市值或财务输入不完整或无效",
    "dcf": "估值计算未能生成有效结果",
    "valuation_evidence": "估值结果与当前公司源数据不一致",
}
_DCF_SKIP_CATEGORY_LABELS = {
    "economic_not_applicable": "经济条件不适用",
    "source_missing": "数据源缺失",
    "inconsistent_source": "源数据矛盾",
    "model_unsupported": "估值模型暂不支持",
    "internal_error": "估值计算异常",
}
_DCF_SKIP_REASON_FALLBACKS = {
    "economic_not_applicable": "当前经济条件不适合使用该估值模型",
    "source_missing": "生成估值所需的数据暂时缺失",
    "inconsistent_source": "不同来源的数据存在矛盾，相关估值未被采用",
    "model_unsupported": "当前估值模型暂不支持该公司",
    "internal_error": "估值计算发生内部异常，相关结果未被采用",
}


def _public_failure_message(value: object, *, fallback: str) -> str:
    """Keep exception internals and old-cache machine text out of ordinary UI."""
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    display = _display_reason(text)
    if display != text or len(text) > 240 or _FAILURE_MACHINE_TEXT.search(text):
        return fallback
    return text


def _public_dcf_skip_category(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "原因尚未分类"
    return _DCF_SKIP_CATEGORY_LABELS.get(raw, "其他未生成原因")


def _public_dcf_skip_reason(value: object, *, category: object = None) -> str:
    raw = " ".join(str(value or "").split())
    category_key = str(category or "").strip()
    fallback = _DCF_SKIP_REASON_FALLBACKS.get(category_key, "估值未能生成有效结果")
    if not raw:
        return fallback
    display = _display_reason(raw)
    if display in {"可核验的财务与行业数据", "量价与换手数据"} and display != raw:
        return fallback
    return _public_failure_message(display, fallback=fallback)


def _public_dcf_audit_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Create a plain-language table while preserving the raw technical export."""
    if frame.empty:
        return frame.copy()
    public = frame.copy()
    raw_categories = public.get("跳过分类", pd.Series("", index=public.index)).fillna("").astype(str)
    raw_reasons = public.get("跳过原因", pd.Series("", index=public.index)).fillna("").astype(str)
    public["跳过分类"] = raw_categories.map(lambda value: "" if not value else _public_dcf_skip_category(value))
    public["跳过原因"] = [
        "" if not reason else _public_dcf_skip_reason(reason, category=category)
        for reason, category in zip(raw_reasons, raw_categories)
    ]
    return public.drop(columns=["估值参数（JSON）"], errors="ignore")


def _public_pipeline_issue_rows(issues: object) -> list[dict[str, str]]:
    """Translate pipeline diagnostics without exposing stages or exception text."""
    if not isinstance(issues, (list, tuple)):
        return []
    rows: list[dict[str, str]] = []
    stage_labels = set(_PIPELINE_STAGE_LABELS.values())
    for issue in issues:
        if isinstance(issue, Mapping):
            raw_code = issue.get("code", issue.get("代码"))
            raw_stage = issue.get("stage", issue.get("阶段"))
        else:
            raw_code = getattr(issue, "code", "")
            raw_stage = getattr(issue, "stage", "")
        code = _normalise_code(raw_code)
        stage = str(raw_stage or "").strip()
        if stage in stage_labels:
            stage_key = next(key for key, label in _PIPELINE_STAGE_LABELS.items() if label == stage)
        else:
            stage_key = stage
        rows.append(
            {
                "代码": code if re.fullmatch(r"[0-9]{6}", code) else "未知标的",
                "阶段": _PIPELINE_STAGE_LABELS.get(stage_key, "分析处理"),
                "错误": _PIPELINE_STAGE_MESSAGES.get(stage_key, "该条数据未完成可靠分析，已排除本次结果"),
            }
        )
    return rows


def _cache_diagnostic_rows(value: object) -> list[dict[str, str]]:
    """Summarise a cache diagnostic without displaying its internal contract."""
    if not isinstance(value, Mapping) or not value:
        return []
    diagnostic = json.dumps(value, ensure_ascii=True, default=str).lower()
    if any(marker in diagnostic for marker in ("error", "failed", "invalid", "mismatch", "corrupt", "rejected")):
        result = "缓存未通过完整性校验，未采用该缓存"
    elif any(marker in diagnostic for marker in ("stale", "expired")):
        result = "缓存已过期，未作为最新数据使用"
    elif any(marker in diagnostic for marker in ("miss", "not_found", "not found")):
        result = "未找到可用缓存，已尝试重新获取数据"
    elif any(marker in diagnostic for marker in ("hit", "fresh", "saved", "valid")):
        result = "已读取并校验本地缓存"
    else:
        result = "已完成本地缓存状态检查"
    return [{"检查项目": "本地数据缓存", "结果": result}]


_SCENARIO_DISPLAY_NAMES = {
    "pessimistic": "悲观",
    "neutral": "中性",
    "optimistic": "乐观",
}
_TYPE7_SCORE_DISPLAY_NAMES = {
    "template1": "长期质量与回报评分",
    "template5": "产业质量与估值评分",
    "patch5": "商业质量与安全边际评分",
}
_TYPE7_PREREQUISITE_DISPLAY_NAMES = {
    "core_modules_80pct": "全部必需子项完整，且核心质量资料覆盖至少80%",
    "technology_patch4": "技术类公司补充核验",
    "three_year_financials": "至少三年连续财务数据",
    "latest_quote_and_valuation": "最新行情与估值",
    "three_external_reports": "至少三份外部研究资料",
    "external_report_content_verification": "外部资料内容交叉核验",
    "ten_year_return_and_five_year_valuation": "十年回报与五年估值历史",
}


def _percentage_text(value: object, *, digits: int = 2) -> str:
    number = _fmt_score(value)
    return "暂无数据" if number is None else f"{number * 100:.{digits}f}%"


def _plain_number_text(value: object, *, digits: int = 2) -> str:
    number = _fmt_score(value)
    return "暂无数据" if number is None else f"{number:.{digits}f}"


def _dcf_parameter_rows(result: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build a Chinese, business-level view of valuation parameters.

    The exact machine ledger remains available in the explicitly labelled
    technical-audit download.  Ordinary stock detail deliberately exposes
    only inputs that a reader can interpret without knowing implementation
    field names or formulas.
    """
    params = result.get("params")
    if not isinstance(params, Mapping):
        return []
    financial_model = bool(result.get("_pb_valuation"))
    rows: list[dict[str, str]] = []
    for scenario, label in _SCENARIO_DISPLAY_NAMES.items():
        values = params.get(scenario)
        if not isinstance(values, Mapping):
            continue
        if financial_model:
            years = values.get("roe_years")
            year_text = (
                "、".join(str(year) for year in years) if isinstance(years, (list, tuple)) and years else "暂无数据"
            )
            rows.append(
                {
                    "情景": label,
                    "长期增长率": _percentage_text(values.get("growth")),
                    "情景净资产收益率": _percentage_text(values.get("scenario_roe")),
                    "股权成本": _percentage_text(values.get("cost_of_equity", values.get("wacc_base"))),
                    "每股净资产": _plain_number_text(values.get("bvps")),
                    "合理市净率区间": (
                        f"{_plain_number_text(values.get('pb_lower'))} 至 {_plain_number_text(values.get('pb_upper'))}"
                    ),
                    "净资产收益率取样年份": year_text,
                }
            )
        else:
            years = _fmt_score(values.get("forecast_years"))
            rows.append(
                {
                    "情景": label,
                    "预测期收入增长率": _percentage_text(values.get("growth")),
                    "基础折现率": _percentage_text(values.get("wacc_base")),
                    "永续增长率": _percentage_text(values.get("terminal_g")),
                    "利润率保持比例": _percentage_text(values.get("margin_retention")),
                    "显式预测期": "暂无数据" if years is None else f"{int(years)}年",
                }
            )
    return rows


def _type7_prerequisite_detail(key: str, record: Mapping[str, Any]) -> str:
    if key == "core_modules_80pct":
        actual = _fmt_score(record.get("actual"))
        required = _fmt_score(record.get("required"))
        coverage = (
            f"核心质量资料覆盖{actual * 100:.1f}%，要求至少{required * 100:.0f}%"
            if actual is not None and required is not None
            else ""
        )
        required_items_complete = record.get("required_items_complete")
        incomplete_items = record.get("incomplete_required_items")
        if required_items_complete is True:
            item_status = "全部必需子项资料完整"
        elif required_items_complete is False:
            count = len(incomplete_items) if isinstance(incomplete_items, (list, tuple)) else None
            item_status = "仍有必需子项资料不完整" if count is None else f"仍有{count}个必需子项资料不完整"
        else:
            item_status = ""
        if item_status and coverage:
            return f"{item_status}；{coverage}"
        if item_status or coverage:
            return item_status or coverage
    elif key == "technology_patch4":
        if record.get("applicable") is False:
            return "该公司不需要此项补充核验"
        score = _fmt_score(record.get("score"))
        status = str(record.get("validation_status") or "")
        if score is not None:
            return f"研发人员持股与长期激励核验得分{score:.1f}分"
        if status == "incomplete_replayable_assessment":
            return "公告已经读取，但核心研发持股、人才覆盖、长期考核等五项事实仍有缺口"
        return "尚未从公司公告中直接确认核心研发持股、人才覆盖和长期研发考核等五项事实"
    elif key == "three_year_financials":
        years = _fmt_score(record.get("consecutive_years"))
        if years is not None:
            return f"已有{int(years)}年连续财务数据"
    elif key == "latest_quote_and_valuation":
        as_of = str(record.get("as_of") or "").strip()
        if as_of:
            return f"数据日期{as_of}"
    elif key == "three_external_reports":
        sources = _fmt_score(record.get("source_count"))
        publishers = _fmt_score(record.get("distinct_publishers"))
        if sources is not None and publishers is not None:
            return f"已有{int(sources)}份资料，来自{int(publishers)}个不同发布方"
    elif key == "ten_year_return_and_five_year_valuation":
        as_of = str(record.get("as_of") or "").strip()
        if as_of:
            return f"历史数据截至{as_of}"
    return "已满足" if record.get("passed") is True else "尚未满足"


def _type7_ledger_summary(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Return the readable subset of a Type-7 audit ledger."""
    scores = ledger.get("scores")
    checks = ledger.get("strict_checks")
    score_rows: list[dict[str, str]] = []
    for key, label in _TYPE7_SCORE_DISPLAY_NAMES.items():
        score = _fmt_score(scores.get(key)) if isinstance(scores, Mapping) else None
        passed = checks.get(key) if isinstance(checks, Mapping) else None
        if not isinstance(passed, bool) and score is not None:
            passed = score > 70.0
        score_rows.append(
            {
                "评分体系": label,
                "百分制得分": "暂无数据" if score is None else f"{score:.2f}",
                "是否严格高于70分": "是" if passed is True else "否" if passed is False else "暂无数据",
            }
        )

    prerequisites = ledger.get("prerequisites")
    prerequisite_rows: list[dict[str, str]] = []
    if isinstance(prerequisites, Mapping):
        for key, label in _TYPE7_PREREQUISITE_DISPLAY_NAMES.items():
            record = prerequisites.get(key)
            if not isinstance(record, Mapping):
                continue
            prerequisite_rows.append(
                {
                    "前置核验": label,
                    "结果": "通过" if record.get("passed") is True else "未通过",
                    "说明": _type7_prerequisite_detail(key, record),
                }
            )

    safety_veto = ledger.get("safety_veto")
    patch5 = ledger.get("patch5")
    safety_score = _fmt_score(patch5.get("safety_margin_score")) if isinstance(patch5, Mapping) else None
    if isinstance(safety_veto, bool):
        safety_detail = (
            f"当前安全边际{safety_score:.2f}/20；低于8分即否决"
            if safety_score is not None
            else "安全边际低于8/20时否决"
        )
        prerequisite_rows.append(
            {
                "前置核验": "安全边际不得触发否决",
                "结果": "未通过" if safety_veto else "通过",
                "说明": safety_detail,
            }
        )

    if ledger.get("triggered") is True:
        conclusion = "三项百分制评分均严格高于70分，必需子项与全部前置核验通过，且安全边际未触发否决。"
    elif safety_veto is True:
        conclusion = "安全边际低于8/20，已触发否决，本类型不触发。"
    elif ledger.get("decisively_not_triggered") is True:
        conclusion = "即使补全当前缺失资料，至少一套评分仍无法严格超过70分。"
    elif ledger.get("prerequisites_complete") is False:
        conclusion = "必需子项或其他前置核验仍未通过，本类型暂不触发。"
    else:
        conclusion = "当前结果未满足三项百分制评分分别严格超过70分的触发条件。"
    return {
        "score_rows": score_rows,
        "prerequisite_rows": prerequisite_rows,
        "conclusion": conclusion,
    }


def _type_trigger_rule_text(type_key: str) -> str:
    if type_key == "type7":
        return (
            "触发条件：三项百分制评分必须分别严格高于70分，不能用平均分相互补偿；"
            "全部必需子项和其他前置核验必须通过，且安全边际不得低于8/20。"
        )
    return "触发条件：加权总分达到7.0分，并满足该类型全部附加条件且未触发一票否决。"


def _filter_stock_search(frame: pd.DataFrame, term: str) -> pd.DataFrame:
    """按字面值搜索代码或名称，用户输入永远不按正则解释。"""
    literal = str(term or "").strip()
    if not literal:
        return frame.copy()
    return frame[
        frame["code"].astype(str).str.contains(literal, case=False, regex=False, na=False)
        | frame["name"].astype(str).str.contains(literal, case=False, regex=False, na=False)
    ].copy()


def _normalise_code(value) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


def _is_sh_sz_analysis_code(value) -> bool:
    """Return whether *value* belongs to the supported Shanghai/Shenzhen universe."""
    code = _normalise_code(value)
    return len(code) == 6 and code.isdigit() and code.startswith(("0", "3", "6"))


def _industry_display_name(code: object, mapping: Mapping[str, str] | None = None) -> str:
    text = str(code or "").strip()
    if text == "DEFAULT":
        return "未分类（低置信度）"
    return str((mapping or {}).get(text, text or "未知"))


def _filter_type_selection(
    frame: pd.DataFrame,
    active_types: list[str],
    *,
    include_no_signal: bool,
    include_conditional: bool = False,
) -> pd.DataFrame:
    """Keep selected triggers and explicitly requested non-buy candidates.

    A company triggered only by a deselected framework must not reappear merely
    because ``include_no_signal`` is enabled.  A conditional candidate (for
    example, Type6 without a confirmed position size) remains separate from a
    buy signal even when it is displayed.
    """
    active = set(active_types)

    def keep(row: pd.Series) -> bool:
        value = row.get("buy_types", [])
        triggered = set(value) if isinstance(value, (list, tuple, set)) else set()
        conditional = {
            type_key
            for type_key in active
            if isinstance(row.get(type_key), Mapping) and _type_status(row[type_key]) == "conditional"
        }
        has_conditional = any(
            isinstance(row.get(type_key), Mapping) and _type_status(row[type_key]) == "conditional"
            for type_key in TYPE_ORDER
        )
        return (
            bool(triggered & active)
            or (include_conditional and bool(conditional))
            or (include_no_signal and not triggered and not has_conditional)
        )

    if "buy_types" not in frame:
        return frame.copy() if include_no_signal else frame.iloc[0:0].copy()
    return frame[frame.apply(keep, axis=1)].copy()


def _current_analysis_generation_identity() -> dict[str, object]:
    """Bind an in-memory result to every mutable production input.

    Streamlit preserves ``session_state`` across source hot reloads. Without
    this identity an old score frame can survive a rule/data-contract change
    and present a stale candidate count as if it were current.
    """
    from data.snapshot import SNAPSHOT_SCHEMA_VERSION
    from engine.audit import audit_state_hashes

    return {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        **audit_state_hashes(),
    }


def _artifact_sha256(path) -> str:
    """Hash an immutable cache artifact without decoding its large payload."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persistent_analysis_cache_path():
    from data.snapshot import DEFAULT_SNAPSHOT_PATH

    return DEFAULT_SNAPSHOT_PATH.with_name("analysis_generation.json.gz")


def _persistent_analysis_cache_enabled() -> bool:
    disabled = str(os.environ.get("DS_DCF_DISABLE_PERSISTENT_ANALYSIS_CACHE") or "").strip().lower()
    return disabled not in {"1", "true", "yes"} and "PYTEST_CURRENT_TEST" not in os.environ


def _save_persistent_analysis_state(state: Mapping[str, object]) -> str:
    """Persist only the default, source-bound last successful generation."""
    from data.cache import SafeFileCache
    from data.snapshot import DEFAULT_SNAPSHOT_PATH, MAX_STALE_AGE_SECONDS

    if not _persistent_analysis_cache_enabled():
        return "disabled"
    if state.get("buy_types_user_evidence"):
        return "custom_user_evidence_not_persisted"
    missing = sorted(set(_PERSISTED_ANALYSIS_STATE_KEYS) - set(state))
    if missing:
        raise ValueError(f"analysis state omitted persistent fields: {missing}")
    snapshot_artifact_sha256 = _artifact_sha256(DEFAULT_SNAPSHOT_PATH)
    identity = _current_analysis_generation_identity()
    payload = {
        "contract": {
            "schema_version": _ANALYSIS_CACHE_SCHEMA_VERSION,
            "snapshot_artifact_sha256": snapshot_artifact_sha256,
            "generation_identity": identity,
            "state_keys": list(_PERSISTED_ANALYSIS_STATE_KEYS),
        },
        "state": {key: state[key] for key in _PERSISTED_ANALYSIS_STATE_KEYS},
    }
    cache = SafeFileCache(
        _persistent_analysis_cache_path(),
        schema_version=_ANALYSIS_CACHE_SCHEMA_VERSION,
        ttl=MAX_STALE_AGE_SECONDS,
        max_uncompressed_bytes=_MAX_ANALYSIS_CACHE_UNCOMPRESSED_BYTES,
    )
    cache.save(payload)
    return "saved"


def _restore_persistent_analysis_state() -> tuple[bool, str]:
    """Restore a checksummed result only when code and snapshot bytes match."""
    from data.cache import SafeFileCache
    from data.snapshot import (
        DEFAULT_SNAPSHOT_PATH,
        MAX_FUTURE_SKEW_SECONDS,
        MAX_STALE_AGE_SECONDS,
    )
    from engine.buy_screener import validate_screening_result

    if not _persistent_analysis_cache_enabled():
        return False, "disabled"
    if not DEFAULT_SNAPSHOT_PATH.is_file():
        return False, "snapshot_not_found"
    path = _persistent_analysis_cache_path()
    try:
        if path.is_file() and path.stat().st_size > _MAX_ANALYSIS_CACHE_ARTIFACT_BYTES:
            return False, "artifact_size_limit_exceeded"
    except OSError as exc:
        return False, f"artifact_stat_error:{type(exc).__name__}"
    cache = SafeFileCache(
        path,
        schema_version=_ANALYSIS_CACHE_SCHEMA_VERSION,
        ttl=MAX_STALE_AGE_SECONDS,
        max_uncompressed_bytes=_MAX_ANALYSIS_CACHE_UNCOMPRESSED_BYTES,
    )
    loaded = cache.load()
    if not loaded.hit or not isinstance(loaded.value, Mapping):
        return False, f"miss:{loaded.reason}"
    payload = loaded.value
    if set(payload) != {"contract", "state"}:
        return False, "invalid_payload_shape"
    contract = payload.get("contract")
    state = payload.get("state")
    if not isinstance(contract, Mapping) or not isinstance(state, Mapping):
        return False, "invalid_payload_shape"
    expected_contract_keys = {
        "schema_version",
        "snapshot_artifact_sha256",
        "generation_identity",
        "state_keys",
    }
    if set(contract) != expected_contract_keys:
        return False, "invalid_contract_shape"
    if contract.get("schema_version") != _ANALYSIS_CACHE_SCHEMA_VERSION:
        return False, "contract_version_mismatch"
    if contract.get("state_keys") != list(_PERSISTED_ANALYSIS_STATE_KEYS):
        return False, "state_contract_mismatch"
    if set(state) != set(_PERSISTED_ANALYSIS_STATE_KEYS):
        return False, "state_keys_mismatch"
    current_identity = _current_analysis_generation_identity()
    if contract.get("generation_identity") != current_identity:
        return False, "generation_identity_mismatch"
    if state.get("buy_types_generation_identity") != current_identity:
        return False, "state_generation_identity_mismatch"
    snapshot_hash = _artifact_sha256(DEFAULT_SNAPSHOT_PATH)
    if contract.get("snapshot_artifact_sha256") != snapshot_hash:
        return False, "snapshot_artifact_mismatch"
    if state.get("buy_types_user_evidence"):
        return False, "custom_user_evidence_forbidden"

    generated_at = _fmt_score(state.get("buy_types_timestamp"))
    data_timestamp = _fmt_score(state.get("buy_types_data_timestamp"))
    now = time.time()
    if generated_at is None or data_timestamp is None:
        return False, "invalid_timestamps"
    if generated_at > now + MAX_FUTURE_SKEW_SECONDS or data_timestamp > now + MAX_FUTURE_SKEW_SECONDS:
        return False, "future_timestamp"
    if now - data_timestamp > MAX_STALE_AGE_SECONDS:
        return False, "stale_data_timestamp"

    frame = state.get("buy_types_df")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return False, "invalid_score_frame"
    codes = frame["code"].map(_normalise_code) if "code" in frame else pd.Series(dtype=object)
    if len(codes) != len(frame) or codes.eq("").any() or codes.duplicated().any():
        return False, "invalid_score_identities"
    eligible = state.get("buy_types_eligible_codes")
    if not isinstance(eligible, list) or set(map(_normalise_code, eligible)) != set(codes):
        return False, "eligible_identity_mismatch"
    invariant_errors = validate_screening_result(frame)
    if invariant_errors:
        return False, f"screening_invariant:{invariant_errors[0]}"
    restored = dict(state)
    restored["leaders_df"] = frame
    dcf_results = restored.get("buy_types_dcf_results")
    skip_reasons = restored.get("buy_types_dcf_skip_reasons")
    skip_classifications = restored.get("buy_types_dcf_skip_classifications")
    audit_frame = pd.DataFrame(
        _dcf_audit_rows(
            frame,
            dcf_results if isinstance(dcf_results, Mapping) else {},
            skip_reasons if isinstance(skip_reasons, Mapping) else {},
            skip_classifications=skip_classifications if isinstance(skip_classifications, Mapping) else {},
        )
    )
    restored["buy_types_dcf_audit_frame"] = audit_frame
    restored["buy_types_dcf_audit_csv"] = _spreadsheet_safe_csv(audit_frame)
    # Build the complete JSON only after the user explicitly requests a
    # download.  It is intentionally absent after a cache restore too.
    restored["buy_types_analysis_json"] = None
    restored["buy_types_cache_restore_notice"] = "已从与当前快照和规则严格绑定的上次成功分析缓存恢复。"
    st.session_state.update(restored)
    return True, "hit"


def _invalidate_stale_analysis_state() -> bool:
    """Fail closed when the last successful result belongs to another generation."""
    if not isinstance(st.session_state.get("buy_types_df"), pd.DataFrame):
        return False

    stored = st.session_state.get("buy_types_generation_identity")
    try:
        current = _current_analysis_generation_identity()
    except Exception:
        reason = "无法验证当前分析规则身份，请重新启动程序后再试。"
    else:
        if isinstance(stored, Mapping) and dict(stored) == current:
            return False
        reason = "代码、评分规则、行业数据、依赖或快照结构已经变化，旧分析结果已失效，请重新分析。"

    for key in _ANALYSIS_GENERATION_STATE_KEYS:
        st.session_state.pop(key, None)
    st.session_state["buy_types_refresh_error"] = reason
    return True


def _reset_buy_type_filters() -> None:
    """Restore every persistent filter to the same values as a fresh session."""
    defaults: dict[str, object] = {
        **{f"cb_{type_key}": True for type_key in TYPE_ORDER},
        "include_no_signal": False,
        "include_conditional": False,
        "selected_industries": [],
        "score_min": 0.0,
        "score_max": 10.0,
        "include_missing_metrics": True,
        "enable_pe_filter": False,
        "pe_min": -100.0,
        "pe_max": 10_000.0,
        "enable_pb_filter": False,
        "pb_min": -100.0,
        "pb_max": 200.0,
        "enable_roe_filter": False,
        "roe_min": -50.0,
        "roe_max": 100.0,
        "enable_debt_filter": False,
        "debt_min": -100.0,
        "debt_max": 500.0,
        "min_types": 0,
        "search_table": "",
    }
    st.session_state.update(defaults)


def _with_diagnostic_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """Derive the actual highest-scoring framework from the six payloads."""
    result = frame.copy()
    diagnostic_types: list[str | None] = []
    diagnostic_scores: list[float | None] = []
    diagnostic_labels: list[str] = []
    for _, row in result.iterrows():
        candidates: list[tuple[float, int, str]] = []
        for order, type_key in enumerate(TYPE_ORDER):
            payload = row.get(type_key, {})
            if isinstance(payload, Mapping) and _type_status(payload) in {
                "not_applicable",
                "insufficient_evidence",
            }:
                continue
            score = _fmt_score(payload.get("total")) if isinstance(payload, Mapping) else None
            if score is not None:
                candidates.append((score, -order, type_key))
        if candidates:
            score, _tie_breaker, type_key = max(candidates)
            diagnostic_types.append(type_key)
            diagnostic_scores.append(score)
            diagnostic_labels.append(TYPE_NAMES[type_key])
        else:
            diagnostic_types.append(None)
            diagnostic_scores.append(None)
            diagnostic_labels.append("")
    result["diagnostic_type"] = diagnostic_types
    result["diagnostic_score"] = pd.to_numeric(diagnostic_scores, errors="coerce")
    result["diagnostic_label"] = diagnostic_labels
    # Backward-compatible alias.  Its UI label always states that this is a
    # diagnostic maximum, not a buy signal.
    result["max_score"] = result["diagnostic_score"]
    return result


def _eligible_analysis_inputs(snapshot):
    """Return the validated analysis universe, with old snapshot compatibility."""
    eligible_codes = tuple(getattr(snapshot, "eligible_codes", ()) or ())
    if not eligible_codes and isinstance(getattr(snapshot, "validation", None), Mapping):
        eligible_codes = tuple(snapshot.validation.get("eligible_codes", ()) or ())
    eligible = {
        code for raw_code in eligible_codes if (code := _normalise_code(raw_code)) and _is_sh_sz_analysis_code(code)
    }

    analysis_quotes = getattr(snapshot, "analysis_quotes", None)
    analysis_financials = getattr(snapshot, "analysis_financials", None)
    if isinstance(analysis_quotes, pd.DataFrame) and isinstance(analysis_financials, Mapping):
        quotes = analysis_quotes.copy()
        financials = dict(analysis_financials)
    else:
        quotes = snapshot.quotes.copy()
        financials = dict(snapshot.financials)
        if not eligible:
            # Compatibility for old caches/tests, while retaining the product's
            # explicit SH/SZ-only boundary.  New validated snapshots always
            # expose eligible_codes and therefore do not need this fallback.
            eligible = {
                code
                for raw_code in quotes["code"]
                if (code := _normalise_code(raw_code)) and _is_sh_sz_analysis_code(code)
            }
    quote_codes = quotes["code"].map(_normalise_code)
    supported_quotes = quote_codes.map(_is_sh_sz_analysis_code)
    quotes = quotes.loc[quote_codes.isin(eligible) & supported_quotes].copy()
    quotes["code"] = quote_codes.loc[quotes.index]
    financials = {
        code: company
        for raw_code, company in financials.items()
        if (code := _normalise_code(raw_code)) in eligible and _is_sh_sz_analysis_code(code)
    }
    matched = tuple(sorted(set(quotes["code"]) & set(financials)))
    quotes = quotes[quotes["code"].isin(matched)].copy()
    financials = {code: financials[code] for code in matched}
    return quotes, financials, matched


def _snapshot_reporting_period_contract(snapshot):
    """Parse the validated snapshot contract without permitting an annual fallback."""
    from engine.dcf import ReportingPeriodContract, TTM_PERIOD_BASIS

    validation = getattr(snapshot, "validation", None)
    if not isinstance(validation, Mapping):
        raise ValueError("快照缺少校验元数据和严格TTM报告期契约，已拒绝年度数据回退")
    raw_contract = validation.get("reporting_period_contract")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("快照缺少严格TTM报告期契约，已拒绝年度数据回退")

    expected_fields = {
        "annual_report_date",
        "current_interim_report_date",
        "prior_interim_report_date",
        "period_basis",
    }
    if set(raw_contract) != expected_fields:
        raise ValueError("快照严格TTM报告期契约字段不完整或包含未知字段，已拒绝分析")
    if raw_contract.get("period_basis") != TTM_PERIOD_BASIS:
        raise ValueError("快照严格TTM报告期口径不受支持，已拒绝年度数据回退")

    date_fields = (
        "annual_report_date",
        "current_interim_report_date",
        "prior_interim_report_date",
    )
    if not all(isinstance(raw_contract.get(field), str) for field in date_fields):
        raise ValueError("快照严格TTM报告期日期无效，已拒绝分析")
    return ReportingPeriodContract(
        annual_report_date=raw_contract["annual_report_date"],
        current_interim_report_date=raw_contract["current_interim_report_date"],
        prior_interim_report_date=raw_contract["prior_interim_report_date"],
    )


def _merge_user_evidence(
    financials: Mapping[str, Mapping],
    payload: object,
    *,
    as_of: str | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Overlay only explicit portfolio constraints; research scores stay model-owned."""
    canonical: dict[str, dict] = {}
    for raw_code, company in financials.items():
        code = _normalise_code(raw_code)
        if not code or code in canonical or not isinstance(company, Mapping):
            raise ValueError(f"财务数据代码无效或重复: {raw_code}")
        canonical[code] = dict(company)
    if payload is None:
        return canonical, {}
    try:
        evidence_cutoff = date.fromisoformat(as_of) if isinstance(as_of, str) else date.today()
    except ValueError as exc:
        raise ValueError("外部证据截止日必须为有效的 YYYY-MM-DD") from exc
    if evidence_cutoff > date.today():
        raise ValueError("外部证据截止日不能晚于今天")
    if not isinstance(payload, Mapping):
        raise ValueError("外部证据 JSON 顶层必须是股票代码到证据对象的映射")
    allowed = {"position_size_pct", "type6_portfolio_pct"}
    normalised: dict[str, dict] = {}
    for raw_code, raw_entry in payload.items():
        code = _normalise_code(raw_code)
        if not code or code in normalised:
            raise ValueError(f"外部证据股票代码无效或归一化后重复: {raw_code}")
        if code not in canonical:
            raise ValueError(f"外部证据包含不在本代合法分析集合中的代码: {code}")
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"{code} 的外部证据必须是对象")
        unknown = sorted(str(key) for key in raw_entry if key not in allowed)
        if unknown:
            raise ValueError(f"{code} 包含不允许的外部证据字段: {', '.join(unknown[:5])}")
        clean: dict = {}
        for key in ("position_size_pct", "type6_portfolio_pct"):
            if key not in raw_entry:
                continue
            value = _fmt_score(raw_entry.get(key))
            if value is None or not 0 < value <= 100:
                raise ValueError(f"{code} 的 {key} 必须是0到100之间的有限百分数")
            clean[key] = value
        if not clean:
            raise ValueError(f"{code} 未包含任何受支持的外部证据字段")
        normalised[code] = clean
        canonical[code].update(clean)
    return canonical, normalised


def _parse_user_evidence_json(raw: bytes) -> Mapping:
    """Decode a bounded UTF-8 JSON evidence file before any network work starts."""
    if not isinstance(raw, bytes):
        raise ValueError("外部证据文件读取失败")
    if len(raw) > MAX_USER_EVIDENCE_BYTES:
        raise ValueError("外部证据文件不得超过1MB")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"外部证据 JSON 包含重复键: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"外部证据文件不是有效UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("外部证据 JSON 顶层必须是股票代码到证据对象的映射")

    nodes = 0
    stack = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_USER_EVIDENCE_NODES:
            raise ValueError("外部证据 JSON 结构节点过多")
        if depth > MAX_USER_EVIDENCE_DEPTH:
            raise ValueError("外部证据 JSON 嵌套层级过深")
        if isinstance(value, Mapping):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return payload


def _spreadsheet_safe_csv(frame: pd.DataFrame) -> bytes:
    """Serialize a display copy with spreadsheet formulas neutralised."""
    safe = frame.copy()

    def neutralise(value):
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if value[:1] in {"\t", "\r", "\n"} or stripped[:1] in {"=", "+", "-", "@"}:
            return "'" + value
        return value

    for column in safe.columns:
        safe[column] = safe[column].map(neutralise)
    return safe.to_csv(index=False, lineterminator="\n").encode("utf-8-sig")


def _supports_keyword(function, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _skip_reason_mapping(analysis) -> dict[str, str]:
    for field in ("dcf_skip_reasons", "dcf_skipped_reasons", "skipped_reasons"):
        value = getattr(analysis, field, None)
        if isinstance(value, Mapping):
            return {_normalise_code(code): str(reason or "证据不足") for code, reason in value.items()}
    return {}


def _skip_classification_mapping(analysis) -> dict[str, dict[str, str]]:
    value = getattr(analysis, "dcf_skip_classifications", None)
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, str]] = {}
    for code, payload in value.items():
        if not isinstance(payload, Mapping):
            continue
        category = payload.get("category")
        reason = payload.get("reason")
        if isinstance(category, str) and category and isinstance(reason, str) and reason:
            result[_normalise_code(code)] = {"category": category, "reason": reason}
    return result


def _build_successful_analysis_state(
    snapshot,
    analysis,
    quality,
    eligible_codes,
    *,
    user_evidence: Mapping[str, Mapping] | None = None,
    market_coldness_status: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Materialize every UI value before a network candidate is promoted."""
    scores = _with_diagnostic_fields(analysis.scores)
    dcf_results = getattr(analysis, "dcf_results", {})
    if not isinstance(dcf_results, Mapping):
        dcf_results = {}
    skip_reasons = _skip_reason_mapping(analysis)
    skip_classifications = _skip_classification_mapping(analysis)
    raw_cache_diagnostic = getattr(snapshot, "cache_diagnostic", {})
    cache_diagnostic = (
        dict(raw_cache_diagnostic)
        if isinstance(raw_cache_diagnostic, Mapping)
        else ({"message": str(raw_cache_diagnostic)} if raw_cache_diagnostic else {})
    )
    validation = snapshot.validation if isinstance(snapshot.validation, Mapping) else {}
    raw_exclusions = getattr(snapshot, "analysis_exclusions", None)
    if not isinstance(raw_exclusions, Mapping):
        raw_exclusions = next(
            (
                validation.get(key)
                for key in (
                    "analysis_exclusions",
                    "analysis_exclusion_reasons",
                    "exclusion_reasons",
                    "excluded_reasons",
                )
                if isinstance(validation.get(key), Mapping)
            ),
            {},
        )
    exclusion_labels = {
        "special_treatment": "ST/*ST/S类风险标的",
        "delisting": "退市或退市整理标的",
        "suspended_or_no_trade": "停牌或当日无成交，仅有昨收参考价",
        "incomplete_financial_evidence": "财报数据集交集未通过完整性边界",
        "stale_or_incomplete_current_financials": "最新应披露财报不完整或已过期",
        "invalid_market_cap": "缺少有效总市值，无法可靠计算估值与横截面指标",
        "unsupported_market": "北交所不在本产品可交易分析范围（仅分析沪深）",
        "unclassified_industry": "缺少可信行业分类，禁止套用默认Beta或行业基准",
        "not_eligible": "未通过自动分析边界",
    }
    exclusions = {
        _normalise_code(code): exclusion_labels.get(str(reason), "未通过自动分析边界")
        for code, reason in raw_exclusions.items()
    }
    raw_ineligible = getattr(snapshot, "ineligible_codes", ()) or validation.get("ineligible_codes", ())
    for code in raw_ineligible:
        exclusions.setdefault(_normalise_code(code), "未通过自动分析边界")
    analysis_timestamp = time.time()
    state: dict[str, object] = {
        "buy_types_df": scores,
        "leaders_df": scores,
        "buy_types_timestamp": analysis_timestamp,
        "buy_types_data_timestamp": snapshot.data_timestamp,
        "buy_types_retrieved_at": getattr(snapshot, "retrieved_at", None),
        "buy_types_data_source": snapshot.source,
        "buy_types_snapshot_validation": dict(snapshot.validation),
        "buy_types_snapshot_warning": (
            _public_failure_message(
                snapshot.warning,
                fallback="数据刷新未完成，已使用上一份通过校验的完整快照。",
            )
            if snapshot.warning
            else ""
        ),
        "buy_types_cache_diagnostic": cache_diagnostic,
        "buy_types_analysis_quality": quality,
        "buy_types_dcf_results": dict(dcf_results),
        "buy_types_dcf_skip_reasons": skip_reasons,
        "buy_types_dcf_skip_classifications": skip_classifications,
        "buy_types_eligible_codes": list(eligible_codes),
        "buy_types_analysis_exclusions": exclusions,
        "buy_types_user_evidence": dict(user_evidence or {}),
        "buy_types_market_coldness_status": dict(market_coldness_status or {}),
        "buy_types_pipeline_issues": _public_pipeline_issue_rows(analysis.issues),
        "buy_types_generation_identity": _current_analysis_generation_identity(),
    }
    # Streamlit executes closed expander bodies on every rerun.  Build the
    # compact valuation audit once, but defer the much larger JSON export
    # until the user explicitly asks for a download.
    audit_frame = pd.DataFrame(
        _dcf_audit_rows(
            scores,
            dcf_results,
            skip_reasons,
            skip_classifications=skip_classifications,
        )
    )
    state["buy_types_dcf_audit_frame"] = audit_frame
    state["buy_types_dcf_audit_csv"] = _spreadsheet_safe_csv(audit_frame)
    state["buy_types_analysis_json"] = None
    return state


def _json_safe(value):
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _json_safe(converted)
    return value


def _filter_numeric_range(
    frame: pd.DataFrame,
    column: str,
    lower: float,
    upper: float,
    *,
    include_missing: bool,
    scale: float = 1.0,
) -> pd.DataFrame:
    """Apply a finite numeric range while optionally retaining unavailable evidence."""
    if column not in frame.columns:
        return frame.copy() if include_missing else frame.iloc[0:0].copy()
    numeric = pd.to_numeric(frame[column], errors="coerce")
    finite = numeric.map(lambda value: bool(pd.notna(value) and math.isfinite(float(value))))
    within = finite & (numeric * scale >= lower) & (numeric * scale <= upper)
    if include_missing:
        within |= ~finite
    return frame[within].copy()


TYPE_STATUS_LABELS = {
    "triggered": "✅ 触发",
    "observe": "👀 观察",
    "not_triggered": "— 未触发",
    "vetoed": "🚫 否决",
    "conditional": "⚠️ 条件候选",
    "not_applicable": "➖ 不适用",
    "insufficient_evidence": "❓ 证据不足",
    "blocked": "⛔ 市场状态阻断",
}


def _type_status(info: Mapping[str, Any]) -> str:
    status = str(info.get("status") or "").strip()
    if status:
        return status
    if info.get("veto"):
        return "vetoed"
    return "triggered" if info.get("triggered") else "not_triggered"


def _type_status_label(type_key: str, status: str) -> str:
    """Use Patch6's Type5 phase language instead of a generic score label."""
    if type_key == "type5":
        if status == "triggered":
            return "✅ 最佳买点"
        if status in {"observe", "not_triggered", "conditional"}:
            return "👀 适用·谨慎相位"
    return TYPE_STATUS_LABELS.get(status, "— 未触发")


def _status_icon(*, triggered: bool, veto: bool, status: str | None = None) -> str:
    """Return a single status icon without conflating N/A or missing evidence with veto."""
    if status == "not_applicable":
        return "➖"
    if status == "insufficient_evidence":
        return "❓"
    if status == "conditional":
        return "⚠️"
    if status == "observe":
        return "👀"
    if status == "blocked":
        return "⛔"
    if veto:
        return "🚫"
    return "✅" if triggered else "⬜"


def _type_risk_notice(type_key: str, reasons) -> str:
    """Return the explicit Type6 position/max-loss statement for rendering."""
    if type_key != "type6" or not isinstance(reasons, dict):
        return ""
    return str(reasons.get("_risk", "")).strip()


def _bear_case_lines(row) -> list[str]:
    """Format the required three-point short-seller challenge from score evidence."""
    cases = row.get("bear_case", []) if hasattr(row, "get") else []
    if not isinstance(cases, list):
        return []
    lines: list[str] = []
    for item in cases[:3]:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension", "")).strip()
        score = _fmt_score(item.get("score"))
        reason = _display_reason(item.get("reason", ""))
        if dimension and score is not None and reason:
            lines.append(f"{dimension} {score:.1f}分：{reason}")
    return lines


def _diagnostic_type_label(row) -> str:
    """返回仅用于诊断的最高评分框架，不把它冒充为买入触发。"""
    diagnostic_type = row.get("diagnostic_type") if hasattr(row, "get") else None
    diagnostic_label = row.get("diagnostic_label") if hasattr(row, "get") else None
    if isinstance(diagnostic_label, str) and diagnostic_label.strip():
        return diagnostic_label.strip()
    if isinstance(diagnostic_type, str) and diagnostic_type in TYPE_NAMES:
        return TYPE_NAMES[diagnostic_type]
    return ""


def _format_snapshot_age(timestamp, *, now: float | None = None) -> str:
    """将快照时间戳格式化为稳定、可读的数据年龄。"""
    value = _fmt_score(timestamp)
    current = time.time() if now is None else _fmt_score(now)
    if value is None or current is None or value <= 0:
        return "未知"
    seconds = max(0, int(current - value))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时"
    return f"{hours // 24}天"


def _render_global_status(frame: pd.DataFrame) -> None:
    """在所有业务页显示同一份数据来源、覆盖率与错误状态。"""
    data_ts = st.session_state.get("buy_types_data_timestamp", 0)
    analysis_ts = st.session_state.get("buy_types_timestamp", 0)
    data_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data_ts)) if data_ts else "未知"
    analysis_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(analysis_ts)) if analysis_ts else "未知"
    source = st.session_state.get("buy_types_data_source", "unknown")
    source_label = {
        "cache": "完整缓存",
        "network": "新抓取并通过校验的数据",
        "stale_cache": "上一份完整快照",
    }.get(source, "未知来源")
    validation = st.session_state.get("buy_types_snapshot_validation", {})
    coverage_text = "财报覆盖率未知"
    if isinstance(validation, dict):
        coverage = _fmt_score(validation.get("financial_coverage"))
        matched = validation.get("matched_financials")
        # ``financial_coverage`` is defined on the investable SH/SZ source
        # universe.  Total quote rows may include legacy BJ telemetry, so using
        # ``quotes`` as the displayed denominator makes the count contradict
        # the percentage even when validation is correct.
        denominator = validation.get("analysis_market_quotes")
        if denominator is None:
            denominator = validation.get("quotes")
        if coverage is not None:
            counts = f"{matched}/{denominator}" if matched is not None and denominator is not None else ""
            coverage_text = f"财报覆盖 {counts} ({coverage * 100:.1f}%)".replace("  ", " ")
    st.caption(
        f"数据来源: {source_label} | 抓取完成时间: {data_time} "
        f"(距今 {_format_snapshot_age(data_ts)}) | {coverage_text} | "
        f"分析时间: {analysis_time} | 共 {len(frame)} 只股票"
    )

    retrieved_at = st.session_state.get("buy_types_retrieved_at")
    retrieved = _fmt_score(retrieved_at)
    if retrieved is not None and retrieved > 0:
        retrieved_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(retrieved))
        st.caption(f"行情获取时间: {retrieved_text}（距今 {_format_snapshot_age(retrieved)}）")
    st.caption("行情价格已与源交易日期和时刻成对校验；停牌标的仅保留昨收展示，不进入买入分析。")

    coverage_rows = []
    for type_key in TYPE_ORDER:
        statuses = Counter()
        if type_key in frame:
            for payload in frame[type_key]:
                if isinstance(payload, Mapping):
                    statuses[str(payload.get("status") or "invalid")] += 1
                else:
                    statuses["invalid"] += 1
        coverage_rows.append(
            {
                "买入情况": TYPE_NAMES[type_key],
                "已触发": statuses["triggered"],
                "待仓位/动作确认": statuses["conditional"],
                "观察": statuses["observe"],
                "待补证据": statuses["insufficient_evidence"],
                "已否决": statuses["vetoed"],
                "不适用": statuses["not_applicable"],
            }
        )
    with st.expander("七类覆盖与待补资料", expanded=False):
        st.caption(
            "“待补证据”不是公司不合格；表示当前自动数据不能证明该类规则。"
            "“待仓位/动作确认”表示公司条件已达标，但下单前仍须完成该类型的风控约束。"
        )
        st.dataframe(pd.DataFrame(coverage_rows), width="stretch", hide_index=True)
        coverage_by_type = {row["买入情况"]: row for row in coverage_rows}
        type3 = coverage_by_type.get(TYPE_NAMES["type3"], {})
        type6 = coverage_by_type.get(TYPE_NAMES["type6"], {})
        type7 = coverage_by_type.get(TYPE_NAMES["type7"], {})
        explanations: list[str] = []
        if not type3.get("已触发") and type3.get("待补证据"):
            explanations.append(
                "情况三会对仍有可能达标的公司自动读取连续五年的营业收入、商誉和并购现金流汇总代理，"
                "并读取最多十年的分产品或分地区收入历史。五年只是并购与商誉的评估窗口，不代表公司只有五年历史；"
                "若仍显示“待补证据”，说明源报表缺关键字段、年份不连续或业务分类无法可靠衔接，程序不会把空值当成0。"
            )
        if not type6.get("已触发") and type6.get("待仓位/动作确认"):
            explanations.append(
                "情况六的待确认候选需要填写单只仓位和同类组合占比后，才能判断是否可执行；未填写不会被算作买入。"
            )
        if not type7.get("已触发") and type7.get("待补证据"):
            explanations.append(
                "情况七要求三份独立外部研究资料、五年估值历史和三套账本同时达标；任一缺失都会保留为“待补证据”。"
            )
        if explanations:
            st.info("\n\n".join(explanations))

    if isinstance(validation, Mapping):
        current_period = validation.get("expected_interim_report_date")
        comparative_period = validation.get("previous_interim_report_date")
        current_coverage = validation.get("current_dataset_coverage")
        comparative_coverage = validation.get("comparative_interim_coverage")

        def _coverage_parts(value: object) -> list[str]:
            if not isinstance(value, Mapping):
                return []
            labels = {"income_interim": "利润表", "cashflow_interim": "现金流量表"}
            parts: list[str] = []
            for key in ("income_interim", "cashflow_interim"):
                coverage_value = _fmt_score(value.get(key))
                if coverage_value is not None:
                    parts.append(f"{labels[key]} {coverage_value * 100:.1f}%")
            return parts

        current_parts = _coverage_parts(current_coverage)
        comparative_parts = _coverage_parts(comparative_coverage)
        if current_period or comparative_period or current_parts or comparative_parts:
            period_text = f"当期 {current_period or '未知'} / 同比 {comparative_period or '未知'}"
            current_text = "、".join(current_parts) if current_parts else "未知"
            comparative_text = "、".join(comparative_parts) if comparative_parts else "未知"
            st.caption(f"中期财报期间: {period_text} | 当期覆盖: {current_text} | 同比覆盖: {comparative_text}")

        strict_ttm = validation.get("strict_ttm_source_coverage")
        if isinstance(strict_ttm, Mapping):
            ttm_parts: list[str] = []
            labels = {"revenue": "收入", "fcff": "自由现金流"}
            for metric in ("revenue", "fcff"):
                metric_coverage = strict_ttm.get(metric)
                if not isinstance(metric_coverage, Mapping):
                    continue
                complete = metric_coverage.get("complete")
                denominator = strict_ttm.get("denominator")
                coverage = _fmt_score(metric_coverage.get("coverage"))
                if isinstance(complete, int) and isinstance(denominator, int) and coverage is not None:
                    ttm_parts.append(f"{labels[metric]} {complete}/{denominator} ({coverage * 100:.1f}%)")
            if ttm_parts:
                st.caption("近十二个月数据覆盖（沪深非金融）: " + " | ".join(ttm_parts))

        comparative_missing = validation.get("comparative_missing_codes")
        if isinstance(comparative_missing, (list, tuple, set)) and comparative_missing:
            st.warning(
                f"{len(comparative_missing)} 只股票缺少同比中期财报证据；"
                "这些股票不进入当前估值和评分代，补齐同口径证据后才会重新纳入。"
            )

        industry_status = validation.get("industry_status")
        if isinstance(industry_status, Mapping) and industry_status.get("coverage_ok") is False:
            specific = _fmt_score(industry_status.get("specific_coverage"))
            market_coverage = industry_status.get("market_coverage", {})
            market_parts: list[str] = []
            if isinstance(market_coverage, Mapping):
                for market in ("SH", "SZ"):
                    payload = market_coverage.get(market)
                    coverage = _fmt_score(payload.get("specific_coverage")) if isinstance(payload, Mapping) else None
                    if coverage is not None:
                        market_parts.append(f"{market} {coverage * 100:.1f}%")
            overall = f"总体 {specific * 100:.1f}%" if specific is not None else "总体未知"
            details = "、".join(market_parts)
            st.warning(
                f"行业映射覆盖未达全部市场质量线：{overall}；{details}。"
                "未知行业不会继承全市场行业热度，依赖行业证据的评分会降级或否决。"
            )

    snapshot_warning = st.session_state.get("buy_types_snapshot_warning", "")
    if snapshot_warning:
        warning_detail = _public_failure_message(
            snapshot_warning,
            fallback="数据刷新未完成，已使用上一份通过校验的完整快照。",
        )
        st.warning(f"本次数据刷新失败，当前展示上一份通过校验的完整快照。原因：{warning_detail}")
    cache_restore_notice = st.session_state.get("buy_types_cache_restore_notice", "")
    if cache_restore_notice:
        st.info(cache_restore_notice)
    refresh_error = st.session_state.get("buy_types_refresh_error", "")
    if refresh_error:
        error_detail = _public_failure_message(
            refresh_error,
            fallback="本次处理未完成，已保留上一版结果。",
        )
        st.error(f"本次分析未替换上一版结果。原因：{error_detail}")
    pipeline_issues = _public_pipeline_issue_rows(st.session_state.get("buy_types_pipeline_issues", []))
    if pipeline_issues:
        st.warning(f"{len(pipeline_issues)} 条标的记录未完成可靠分析，均已明确排除。")
        with st.expander("分析排除明细"):
            st.dataframe(pd.DataFrame(pipeline_issues), width="stretch", hide_index=True)

    cache_diagnostic = st.session_state.get("buy_types_cache_diagnostic", {})
    cache_rows = _cache_diagnostic_rows(cache_diagnostic)
    if cache_rows:
        with st.expander("缓存诊断", expanded=False):
            st.dataframe(pd.DataFrame(cache_rows), width="stretch", hide_index=True)

    exclusions = st.session_state.get("buy_types_analysis_exclusions", {})
    if isinstance(exclusions, Mapping) and exclusions:
        st.info(f"{len(exclusions)} 只股票因财报完整性、风险状态或不可定价等边界未进入自动买入分析。")
        with st.expander("自动分析排除明细", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {"代码": _normalise_code(code), "排除原因": str(reason)}
                        for code, reason in sorted(exclusions.items())
                    ]
                ),
                width="stretch",
                hide_index=True,
            )


def _dcf_audit_rows(
    scores: pd.DataFrame,
    dcf_results: Mapping[str, Mapping],
    skip_reasons: Mapping[str, str],
    *,
    skip_classifications: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict]:
    """Flatten valuation bands and parameters for durable UI/export evidence."""
    score_by_code = {_normalise_code(row.get("code")): row for row in scores.to_dict(orient="records")}
    result_by_code = {_normalise_code(code): value for code, value in dcf_results.items() if isinstance(value, Mapping)}
    reasons = {_normalise_code(code): str(reason) for code, reason in skip_reasons.items()}
    classifications = {
        _normalise_code(code): value
        for code, value in (skip_classifications or {}).items()
        if isinstance(value, Mapping)
    }
    rows: list[dict] = []
    for code in sorted(score_by_code):
        score_row = score_by_code[code]
        result = result_by_code.get(code)
        is_financial = str(score_row.get("industry", "")) in {"BANK", "INSURANCE", "SECURITIES"}
        model = "净资产收益估值" if is_financial else "现金流估值"
        if result is None:
            classification = classifications.get(code, {})
            rows.append(
                {
                    "代码": code,
                    "名称": score_row.get("name", ""),
                    "估值模型": model,
                    "估值状态": "跳过",
                    "跳过分类": classification.get("category", "未返回结构化分类"),
                    "跳过原因": reasons.get(code, "未返回结构化原因"),
                }
            )
            continue
        model = "净资产收益估值" if bool(result.get("_pb_valuation")) else "现金流估值"
        record = {
            "代码": code,
            "名称": score_row.get("name", result.get("name", "")),
            "估值模型": model,
            "估值状态": "有效",
            "跳过分类": "",
            "跳过原因": "",
            "当前价": result.get("current_price"),
            "区域": result.get("zone"),
            "买入区上界": result.get("buy_zone_upper"),
            "卖出区下界": result.get("sell_zone_lower"),
            "安全边际%": result.get("safety_margin_pct"),
            "基础折现率": result.get("base_wacc"),
            "基础自由现金流": result.get("base_fcf"),
        }
        points = result.get("dcf_points", {})
        for scenario in ("pessimistic", "neutral", "optimistic"):
            band = points.get(scenario, {}) if isinstance(points, Mapping) else {}
            record[f"{scenario}_lower"] = band.get("lower") if isinstance(band, Mapping) else None
            record[f"{scenario}_upper"] = band.get("upper") if isinstance(band, Mapping) else None
        record["估值参数（JSON）"] = json.dumps(
            _json_safe(result.get("params", {})),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rows.append(record)
    return rows


def _analysis_export_json(
    frame: pd.DataFrame,
    *,
    context: Mapping[str, Any] | None = None,
) -> bytes:
    source = st.session_state if context is None else context
    dcf_results = source.get("buy_types_dcf_results", {})
    skip_reasons = source.get("buy_types_dcf_skip_reasons", {})
    skip_classifications = source.get("buy_types_dcf_skip_classifications", {})
    payload = {
        "generated_at": source.get("buy_types_timestamp", time.time()),
        "data_timestamp": source.get("buy_types_data_timestamp"),
        "retrieved_at": source.get("buy_types_retrieved_at"),
        "source": source.get("buy_types_data_source"),
        "snapshot_validation": source.get("buy_types_snapshot_validation", {}),
        "analysis_quality": source.get("buy_types_analysis_quality", {}),
        "generation_identity": source.get("buy_types_generation_identity", {}),
        "market_coldness_status": source.get("buy_types_market_coldness_status", {}),
        "user_evidence": source.get("buy_types_user_evidence", {}),
        "pipeline_issues": source.get("buy_types_pipeline_issues", []),
        "dcf_skip_reasons": skip_reasons,
        "dcf_skip_classifications": skip_classifications,
        "dcf_results": dcf_results,
        "scores": frame.sort_values("code", kind="stable").to_dict(orient="records"),
    }
    return (json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _market_coldness_status_message(status: object) -> tuple[str, str] | None:
    """Return a durable explanation when Type-2 coldness is incomplete."""
    if not isinstance(status, Mapping) or not status:
        return None
    evidence_available = status.get("evidence_available") is True
    reason = str(status.get("evidence_reason") or "")
    count = status.get("eligible_evidence_count")
    total = status.get("eligible_companies")
    coverage = _fmt_score(status.get("eligible_evidence_coverage"))
    if reason in {"intraday_before_close", "retrieval_before_close"}:
        return (
            "warning",
            "本代在交易日16:15前完成，两市15:30结束的盘后交易及延迟数据尚未完成稳定汇总；"
            "情况二（两热一冷）统一显示证据不足。当前“有买入信号”数量不包含可能依赖收盘冷度的公司，"
            "不代表全市场最终只有这些公司；请在16:15后刷新。",
        )
    if not evidence_available:
        detail = f"（{count}/{total}）" if isinstance(count, int) and isinstance(total, int) else ""
        return (
            "warning",
            f"本代情况二量价冷度证据不可用于自动触发{detail}，相关公司保留为“证据不足”而不是0分。"
            "当前“有买入信号”数量因此不是七类证据全部齐备时的最终数量。",
        )
    if coverage is not None and coverage < 1.0:
        return (
            "caption",
            f"情况二量价冷度证据覆盖率为{coverage:.1%}；未覆盖公司保持证据不足，不按0分处理。",
        )
    return None


def _render_analysis_evidence(frame: pd.DataFrame) -> None:
    """Persistently expose coverage, valuation bands, parameters and skips."""
    quality = st.session_state.get("buy_types_analysis_quality", {})
    if isinstance(quality, Mapping) and quality:
        score_coverage = _fmt_score(quality.get("score_coverage"))
        dcf_attempt = _fmt_score(quality.get("dcf_attempt_coverage"))
        dcf_valid = _fmt_score(quality.get("dcf_valid_coverage"))
        issue_rate = _fmt_score(quality.get("pipeline_issue_rate"))
        st.caption(
            "分析质量闸门："
            f"评分覆盖 {_format_metric(score_coverage, scale=100, suffix='%')} · "
            f"估值尝试覆盖 {_format_metric(dcf_attempt, scale=100, suffix='%')} · "
            f"有效估值覆盖 {_format_metric(dcf_valid, scale=100, suffix='%')} · "
            f"异常率 {_format_metric(issue_rate, scale=100, suffix='%')}"
        )

    coldness_status = st.session_state.get("buy_types_market_coldness_status", {})
    notice = _market_coldness_status_message(coldness_status)
    if notice is not None:
        level, message = notice
        if level == "warning":
            st.warning(message)
        else:
            st.caption(message)

    dcf_results = st.session_state.get("buy_types_dcf_results", {})
    if not isinstance(dcf_results, Mapping):
        dcf_results = {}
    skip_reasons = st.session_state.get("buy_types_dcf_skip_reasons", {})
    if not isinstance(skip_reasons, Mapping):
        skip_reasons = {}
    skip_classifications = st.session_state.get("buy_types_dcf_skip_classifications", {})
    if not isinstance(skip_classifications, Mapping):
        skip_classifications = {}
    cached_audit = st.session_state.get("buy_types_dcf_audit_frame")
    audit_frame = (
        cached_audit
        if isinstance(cached_audit, pd.DataFrame)
        else pd.DataFrame(
            _dcf_audit_rows(
                frame,
                dcf_results,
                skip_reasons,
                skip_classifications=skip_classifications,
            )
        )
    )
    with st.expander("🧾 估值区间与未生成原因", expanded=False):
        if audit_frame.empty:
            st.caption("本代没有估值审计记录。")
        else:
            valid_count = int((audit_frame["估值状态"] == "有效").sum())
            missing_reason_count = int((audit_frame.get("跳过原因") == "未返回结构化原因").sum())
            st.caption(
                f"有效 {valid_count} 只，跳过 {len(audit_frame) - valid_count} 只；"
                f"其中 {missing_reason_count} 只仍缺结构化原因。"
            )
            category_counts = Counter(
                str(payload.get("category"))
                for payload in skip_classifications.values()
                if isinstance(payload, Mapping) and payload.get("category")
            )
            if category_counts:
                breakdown = " · ".join(
                    f"{_public_dcf_skip_category(category)} {count} 只"
                    for category, count in sorted(category_counts.items())
                )
                st.caption(f"未生成估值分类：{breakdown}")
            st.dataframe(_public_dcf_audit_frame(audit_frame), width="stretch", hide_index=True, height=420)
            st.download_button(
                "下载估值技术审计 CSV（含原始字段）",
                st.session_state.get("buy_types_dcf_audit_csv")
                if isinstance(st.session_state.get("buy_types_dcf_audit_csv"), bytes)
                else _spreadsheet_safe_csv(audit_frame),
                "ds_dcf_valuation_audit.csv",
                "text/csv",
                key="download_dcf_audit_csv",
            )
        analysis_json = st.session_state.get("buy_types_analysis_json")
        if not isinstance(analysis_json, bytes):
            st.caption(
                "供技术审计的完整原始资料包含内部字段、模型标识和复算参数，仅用于核验；"
                "文件较大，按需生成，不会占用启动缓存。"
            )
            if st.button("准备供技术审计的完整原始资料（JSON）", key="prepare_full_analysis_json"):
                with st.spinner("正在生成技术审计资料…"):
                    st.session_state["buy_types_analysis_json"] = _analysis_export_json(
                        frame,
                        context=st.session_state,
                    )
                st.rerun()
        else:
            st.download_button(
                "下载供技术审计的完整原始资料（JSON）",
                analysis_json,
                "ds_dcf_analysis.json",
                "application/json",
                key="download_full_analysis_json",
            )


def _render_stock_dcf(code: str) -> None:
    normalized = _normalise_code(code)
    results = st.session_state.get("buy_types_dcf_results", {})
    result = results.get(normalized) if isinstance(results, Mapping) else None
    if not isinstance(result, Mapping):
        reasons = st.session_state.get("buy_types_dcf_skip_reasons", {})
        reason = reasons.get(normalized) if isinstance(reasons, Mapping) else None
        classifications = st.session_state.get("buy_types_dcf_skip_classifications", {})
        classification = classifications.get(normalized) if isinstance(classifications, Mapping) else None
        category = classification.get("category") if isinstance(classification, Mapping) else None
        st.caption(f"估值：未产生有效结果。原因：{_public_dcf_skip_reason(reason, category=category)}")
        return
    model = "金融公司净资产收益估值" if bool(result.get("_pb_valuation")) else "非金融公司现金流估值"
    with st.expander(f"{model}：三种情景的估值区间与主要参数", expanded=False):
        points = result.get("dcf_points", {})
        rows = []
        for scenario, label in (("pessimistic", "悲观"), ("neutral", "中性"), ("optimistic", "乐观")):
            band = points.get(scenario, {}) if isinstance(points, Mapping) else {}
            rows.append(
                {
                    "情景": label,
                    "下沿": band.get("lower") if isinstance(band, Mapping) else None,
                    "上沿": band.get("upper") if isinstance(band, Mapping) else None,
                }
            )
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        parameter_rows = _dcf_parameter_rows(result)
        if parameter_rows:
            st.caption("主要估值参数（已换算为便于阅读的中文口径）")
            st.dataframe(pd.DataFrame(parameter_rows), width="stretch", hide_index=True)
        st.caption("完整复算参数仅保留在“供技术审计”的原始资料下载中。")


def _render_type6_global_notice() -> None:
    st.warning(f"⚠️ 类型6全局风控：{TYPE6_GLOBAL_RISK_NOTICE}")


def _render_radar_chart(type_key, row):
    """渲染某个类型的雷达图"""
    import plotly.graph_objects as go

    info = row.get(type_key, {})
    if not info or not isinstance(info, dict):
        return
    if _type_status(info) in {"not_applicable", "insufficient_evidence"}:
        return
    sub_scores = info.get("sub_scores", {})
    dims = TYPE_DIMENSIONS.get(type_key, [])
    if not dims or not sub_scores:
        return
    labels = [name for _, name, _ in dims]
    values = []
    texts = []
    for key, _, _ in dims:
        value = _fmt_score(sub_scores.get(key))
        values.append(value if value is not None else 0.0)
        texts.append(f"{value:.2f}" if value is not None else "—")
    closed_labels = labels + [labels[0]]
    closed_values = values + [values[0]]
    closed_texts = texts + [texts[0]]

    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=closed_values,
            theta=closed_labels,
            fill="toself",
            line=dict(color="#1f77b4", width=2),
            text=closed_texts,
            mode="lines+markers+text",
            textposition="top center",
            textfont=dict(size=11),
            name="评分",
        )
    )
    fig.add_trace(
        go.Scatterpolar(
            r=[7.0] * len(closed_labels),
            theta=closed_labels,
            line=dict(color="red", dash="dash", width=1),
            name="每项须严格高于7分" if type_key == "type7" else "7分视觉参考（非子项门槛）",
        )
    )
    fig.update_layout(
        polar=dict(radialaxis=dict(range=[0, 10], tickvals=[0, 2, 4, 6, 8, 10])),
        height=320,
        margin=dict(l=30, r=30, t=30, b=30),
        showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")


def _render_judgment_matrix(row):
    """渲染判断矩阵 — 七种类型 × 总分/命中/否决 总览"""
    import plotly.graph_objects as go

    rows_data = []
    for t in TYPE_ORDER:
        info = row.get(t, {})
        if not info:
            continue
        sc = info.get("total", 0)
        triggered = info.get("triggered", False)
        status_code = _type_status(info)
        status = _type_status_label(t, status_code)
        score_text = (
            "不适用"
            if status_code == "not_applicable"
            else "证据不足"
            if status_code == "insufficient_evidence"
            else f"{sc:.1f}"
        )
        rows_data.append(
            {
                "类型": TYPE_NAMES.get(t, t),
                "总分": sc,
                "总分显示": score_text,
                "状态": status,
                "触发": "✓" if triggered else "",
            }
        )
    if not rows_data:
        return
    df_mat = pd.DataFrame(rows_data)
    df_mat = df_mat.sort_values("总分", ascending=False)
    # 迷你条形图
    fig = go.Figure()
    colors = ["#2ecc71" if r["状态"] == "✅ 触发" else "#95a5a6" for _, r in df_mat.iterrows()]
    fig.add_trace(
        go.Bar(
            x=df_mat["总分"],
            y=df_mat["类型"],
            orientation="h",
            marker=dict(color=colors),
            text=df_mat["总分显示"],
            textposition="inside",
            textfont=dict(color="white", size=12),
        )
    )
    fig.add_vline(x=7.0, line_dash="dash", line_color="red", annotation_text="7分参考线（非统一触发线）")
    fig.update_layout(height=250, margin=dict(l=10, r=10, t=10, b=10), xaxis=dict(range=[0, 10]), showlegend=False)
    st.plotly_chart(fig, width="stretch")


def _render_dimension_table(type_key, row):
    """渲染某个类型的子维度评分表"""
    info = row.get(type_key, {})
    if not info or not isinstance(info, dict):
        st.caption("无数据")
        return

    sub_scores = info.get("sub_scores", {})
    reasons = info.get("reasons", {})
    total = info.get("total", 0)
    status_code = _type_status(info)

    # 雷达图
    _render_radar_chart(type_key, row)
    st.caption(_type_trigger_rule_text(type_key))

    status = _type_status_label(type_key, status_code)
    total_text = (
        "不适用"
        if status_code == "not_applicable"
        else "证据不足"
        if status_code == "insufficient_evidence"
        else _format_metric(total)
    )
    if type_key == "type7":
        st.caption(f"三项折算平均分: **{total_text}/10**（仅供汇总，不是触发阈值） | 状态: {status}")
    else:
        st.caption(f"总分: **{total_text}/10** | 状态: {status} | 基础分数门槛: ≥7.0")

    dims = TYPE_DIMENSIONS.get(type_key, [])
    if not dims or not sub_scores:
        st.caption("无子维度数据")
        return

    rows_data = []
    for dim_key, dim_name, weight in dims:
        score = sub_scores.get(dim_key)
        score_f = _fmt_score(score)
        reason = _display_reason(reasons.get(dim_key, ""))
        rows_data.append(
            {
                "维度": dim_name,
                "评分": (
                    "不适用"
                    if status_code == "not_applicable"
                    else "证据不足"
                    if status_code == "insufficient_evidence"
                    else "暂无数据"
                    if score_f is None
                    else f"{score_f:.3f}"
                    if type_key == "type7"
                    else f"{score_f:.1f}"
                ),
                "权重": f"{weight * 100:.0f}%",
                "依据": reason if reason else "—",
            }
        )

    st.dataframe(pd.DataFrame(rows_data), width="stretch", hide_index=True)


# ═══════════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════════


def _make_narrative(type_key, total, subs, reasons, dims, *, triggered=False, veto=False, status=None):
    label = TYPE_NAMES.get(type_key, type_key)
    if status == "not_applicable":
        scope = _display_reason(reasons.get("_scope") or "该框架不适用")
        return f"{label}: 不适用。{scope}。"
    if status == "insufficient_evidence":
        missing = _display_reason(reasons.get("_missing") or "关键证据不足")
        return f"{label}: 证据不足。{missing}。"
    if type_key == "type5" and status == "triggered":
        trigger_word = "最佳买点"
    elif type_key == "type5" and status in {"observe", "not_triggered", "conditional"}:
        trigger_word = "适用·谨慎相位"
    elif status == "conditional":
        trigger_word = "条件候选，尚未满足执行纪律"
    elif status == "observe":
        trigger_word = "观察"
    elif status == "blocked":
        trigger_word = "市场状态阻断"
    elif veto:
        trigger_word = "一票否决"
    elif triggered:
        trigger_word = "触发买入信号"
    else:
        trigger_word = "未触发"
    parts = []
    for key, name, _weight in dims:
        v = _fmt_score(subs.get(key))
        r = _display_reason(reasons.get(key, ""))
        r_short = str(r)[:20]
        if v is None:
            parts.append(f"{name}无数据({r_short or '未提供评分'})")
        elif v >= 9:
            parts.append(f"{name}极优")
        elif v >= 7:
            parts.append(f"{name}良好")
        elif v >= 5:
            parts.append(f"{name}一般")
        elif v >= 3:
            parts.append(f"{name}偏弱")
        else:
            parts.append(f"{name}是短板({r_short})")
    total_text = _format_metric(total, digits=2)
    return f"{label}: {total_text}分，{trigger_word}。" + "；".join(parts) + "。"


def _render_stock_inline(row):
    """点击表格行后内嵌展示个股分析"""
    st.divider()
    name = row.get("name", "")
    price = row.get("price", 0)
    code = row.get("code", "")
    buy_types = row.get("buy_types", [])
    raw_industry = row.get("industry", "")
    cn = _industry_display_name(raw_industry, {str(raw_industry): row.get("industry_cn", raw_industry)})

    st.subheader(f"📋 {code} {name}")
    kpi_cols = st.columns(6)
    with kpi_cols[0]:
        st.metric("股价", _format_metric(price, digits=2))
    with kpi_cols[1]:
        st.metric("PE", _format_metric(row.get("pe")))
    with kpi_cols[2]:
        st.metric("PB", _format_metric(row.get("pb")))
    with kpi_cols[3]:
        st.metric("ROE", _format_metric(row.get("roe"), scale=100, suffix="%"))
    with kpi_cols[4]:
        st.metric("负债率", _format_metric(row.get("debt_ratio"), scale=100, suffix="%"))
    with kpi_cols[5]:
        st.metric("行业", cn)

    if buy_types:
        labels = " · ".join([TYPE_NAMES.get(t, t) for t in buy_types])
        st.success(f"🎯 命中: {labels}")
    else:
        st.info("未命中任何买入类型：不买。")
    diagnostic_label = _diagnostic_type_label(row)
    if diagnostic_label:
        st.caption(f"最高评分框架（仅用于诊断，不代表买入触发）：{diagnostic_label}")

    bear_lines = _bear_case_lines(row)
    if bear_lines:
        st.warning("🐻 空头复核——三个最致命漏洞：  \n" + "  \n".join(f"- {line}" for line in bear_lines))

    _render_stock_dcf(str(code))

    # 判断矩阵 + 各类型雷达图
    with st.expander("📐 判断矩阵（七类型总分排序）", expanded=True):
        _render_judgment_matrix(row)

    for t in TYPE_ORDER:
        td = row.get(t, {})
        if not td or not isinstance(td, dict):
            continue
        sc = td.get("total", 0)
        trig = td.get("triggered", False)
        veto = td.get("veto", False)
        subs = td.get("sub_scores", {})
        reasons = td.get("reasons", {})
        status_code = _type_status(td)
        dims = TYPE_DIMENSIONS.get(t, [])
        label = TYPE_NAMES.get(t, t)
        icon = _status_icon(triggered=trig, veto=veto, status=status_code)
        score_text = (
            "不适用"
            if status_code == "not_applicable"
            else "证据不足"
            if status_code == "insufficient_evidence"
            else f"{_format_metric(sc, digits=2)}分"
        )
        with st.expander(f"{icon} {label} — {score_text}", expanded=trig):
            narrative = _make_narrative(
                t,
                sc,
                subs,
                reasons,
                dims,
                triggered=trig,
                veto=veto,
                status=status_code,
            )
            if trig and not veto:
                st.success(narrative)
            elif status_code == "not_applicable":
                st.info(narrative)
            else:
                st.warning(narrative)
            risk_notice = _type_risk_notice(t, reasons)
            if risk_notice:
                st.error(f"仓位与最大损失约束：{risk_notice}")
            lines = []
            for key, name, wt in dims:
                v = subs.get(key, 0)
                r = _display_reason(reasons.get(key, ""))
                value_text = (
                    "不适用"
                    if status_code == "not_applicable"
                    else "证据不足"
                    if status_code == "insufficient_evidence"
                    else f"{_format_metric(v, digits=3 if t == 'type7' else 1)}分"
                )
                lines.append(f"  • {name}({key},权{wt * 100:.0f}%)={value_text} — {r}")
            st.caption("  \n".join(lines))
            if t == "type7" and isinstance(td.get("ledger"), Mapping):
                st.caption(_type_trigger_rule_text("type7"))
                summary = _type7_ledger_summary(td["ledger"])
                st.dataframe(pd.DataFrame(summary["score_rows"]), width="stretch", hide_index=True)
                st.caption(summary["conclusion"])
                if summary["prerequisite_rows"]:
                    with st.expander("查看前置证据核验", expanded=False):
                        st.dataframe(
                            pd.DataFrame(summary["prerequisite_rows"]),
                            width="stretch",
                            hide_index=True,
                        )
                st.caption("完整规则账本仅保留在“供技术审计”的原始资料下载中。")


def _run_full_analysis(*, force_refresh: bool, user_evidence_payload: object = None) -> bool:
    """生成候选快照，完成整条分析后才原子替换最后成功结果。"""
    from data.cache import SafeFileCache
    from data.fetcher import DataFetcher
    from data.growth_evidence import fetch_growth_evidence_batch
    from data.market_coldness import fetch_market_coldness_snapshot
    from data.patch4_evidence import fetch_patch4_evidence_batch
    from data.quality_history import fetch_quality_history_batch
    from data.research_reports import fetch_research_reports_batch
    from data.snapshot import (
        DEFAULT_SNAPSHOT_PATH,
        SNAPSHOT_SCHEMA_VERSION,
        SnapshotUnavailableError,
        get_market_snapshot,
        save_market_snapshot,
    )
    from engine.pipeline import run_market_analysis, validate_market_analysis_quality
    from engine.market_coldness import build_market_coldness_evidence

    try:
        with st.spinner("正在加载数据…"):
            snapshot = get_market_snapshot(
                DataFetcher(
                    enrich_listing_dates=True,
                    force_reference_refresh=force_refresh,
                ),
                force_refresh=force_refresh,
                persist_network=False,
            )
    except SnapshotUnavailableError:
        st.session_state["buy_types_refresh_error"] = "数据刷新失败，请稍后重试；已保留上一份通过校验的结果。"
        return False
    except Exception:
        st.session_state["buy_types_refresh_error"] = "数据加载失败，请稍后重试。"
        return False

    source_label = {
        "cache": "完整缓存",
        "network": "待提升的新抓取数据",
        "stale_cache": "上一份完整快照",
    }.get(snapshot.source, "未知来源")

    analysis_quotes, analysis_financials, eligible_codes = _eligible_analysis_inputs(snapshot)
    source_trade_dates = (
        snapshot.validation.get("trading_source_trade_dates", []) if isinstance(snapshot.validation, Mapping) else []
    )
    as_of_session = (
        source_trade_dates[0] if isinstance(source_trade_dates, list) and len(source_trade_dates) == 1 else None
    )
    try:
        reporting_period_contract = _snapshot_reporting_period_contract(snapshot)
    except (TypeError, ValueError):
        st.session_state["buy_types_refresh_error"] = "快照报告期校验未通过，已拒绝使用可能口径不一致的数据。"
        return False
    if user_evidence_payload is not None and as_of_session is None:
        st.session_state["buy_types_refresh_error"] = "外部证据无法绑定到唯一行情快照日，已拒绝分析"
        return False
    try:
        analysis_financials, user_evidence = _merge_user_evidence(
            analysis_financials,
            user_evidence_payload,
            as_of=as_of_session,
        )
    except ValueError:
        st.session_state["buy_types_refresh_error"] = "外部证据格式或日期校验未通过，未采用本次补充资料。"
        return False
    expected_companies = len(eligible_codes)
    if expected_companies < 1:
        st.session_state["buy_types_refresh_error"] = (
            "通过沪深市场、行业、交易状态及财报完整性边界的股票为 0，拒绝分析与晋级。"
        )
        return False
    st.toast(
        f"已从{source_label}加载 {len(snapshot.quotes)} 只行情；{expected_companies} 只沪深标的通过行业、交易与财报完整性边界进入分析",
        icon="✅",
    )

    market_coldness_evidence: dict[str, Mapping[str, Any]] = {}
    market_coldness_status: dict[str, Any] = {}
    try:
        evidence_diagnostics: dict[str, Any] = {}
        with st.spinner("正在加载沪深量价冷度证据…"):
            # A production DataFetcher has already acquired and validated this
            # batch alongside quotes. Reuse the just-written safe cache rather
            # than issuing a second whole-market request in the same run.
            coldness_snapshot = fetch_market_coldness_snapshot(force_refresh=False)
            market_coldness_evidence = build_market_coldness_evidence(
                coldness_snapshot,
                as_of_session=as_of_session,
                listed_quote_codes=tuple(snapshot.quotes["code"]),
                diagnostics=evidence_diagnostics,
            )
        eligible_coldness = len(set(eligible_codes) & set(market_coldness_evidence))
        eligible_coldness_coverage = eligible_coldness / expected_companies
        evidence_reason = str(
            evidence_diagnostics.get("evidence_reason")
            or ("available" if market_coldness_evidence else "no_scoreable_evidence")
        )
        market_coldness_status = {
            "available": bool(coldness_snapshot.available),
            "source": coldness_snapshot.source,
            "source_url": coldness_snapshot.source_url,
            "retrieved_at": coldness_snapshot.retrieved_at,
            "as_of_session": as_of_session,
            "fetched_count": coldness_snapshot.fetched_count,
            "total_expected": coldness_snapshot.total_expected,
            "eligible_evidence_count": eligible_coldness,
            "eligible_companies": expected_companies,
            "eligible_evidence_coverage": eligible_coldness_coverage,
            "evidence_available": bool(eligible_coldness),
            "evidence_reason": evidence_reason,
            "evidence_diagnostics": evidence_diagnostics,
            "source_coverage": coldness_snapshot.coverage.to_dict(),
            "cache_hit": coldness_snapshot.cache_hit,
            "cache_diagnostic": coldness_snapshot.cache_diagnostic,
            "reason": coldness_snapshot.reason,
        }
        notice = _market_coldness_status_message(market_coldness_status)
        if notice is not None:
            _level, message = notice
            st.warning(message)
    except Exception:
        market_coldness_status = {
            "available": False,
            "reason": "量价冷度资料未通过校验",
            "eligible_evidence_count": 0,
            "eligible_companies": expected_companies,
            "eligible_evidence_coverage": 0.0,
            "evidence_available": False,
            "evidence_reason": "validation_or_acquisition_error",
        }
        st.warning("市场冷度资料校验未通过；情况二会显示“证据不足”，不会按零分或买入信号处理。")

    dcf_status = st.status("估值中（非金融公司按现金流估值，金融公司按盈利能力和净资产估值）...", expanded=True)

    def dcf_cb(done, total):
        if done == total or done % 500 == 0:
            dcf_status.write(f"估值: {done}/{total}")

    score_status = st.status("指标提取与七类型评分中...", expanded=True)
    growth_status = st.status("可持续高增长型所需的分部与并购资料正在预筛选...", expanded=False)
    history_status = st.status("优质股权型所需的长期资料正在预筛选...", expanded=False)

    def score_cb(done, total):
        if done == total or done % 500 == 0:
            score_status.write(f"指标提取: {done}/{total}；七类型评分将在提取完成后执行")

    def quality_history_cb(done, total):
        if done == total or done % 20 == 0:
            history_status.update(label=f"优质股权型的十年回报与五年估值资料: {done}/{total}", expanded=True)

    def type3_growth_cb(done, total):
        if done == total or done % 10 == 0:
            growth_status.update(label=f"可持续高增长型的分部与并购资料: {done}/{total}", expanded=True)

    try:
        previous_quality = st.session_state.get("buy_types_analysis_quality")
        if not isinstance(previous_quality, Mapping) or not previous_quality:
            snapshot_previous = getattr(snapshot, "previous_analysis_quality", None)
            snapshot_active = getattr(snapshot, "analysis_quality", None)
            if isinstance(snapshot_previous, Mapping) and snapshot_previous:
                previous_quality = dict(snapshot_previous)
            elif isinstance(snapshot_active, Mapping) and snapshot_active:
                previous_quality = dict(snapshot_active)
        analysis_kwargs = {
            "dcf_progress_cb": dcf_cb,
            "score_progress_cb": score_cb,
            # This is deliberately unconditional.  An older pipeline that
            # cannot consume the generation-wide TTM contract must fail closed
            # instead of silently reverting to annual cash-flow inputs.
            "reporting_period_contract": reporting_period_contract,
        }
        if _supports_keyword(run_market_analysis, "eligible_codes"):
            analysis_kwargs["eligible_codes"] = eligible_codes
        if _supports_keyword(run_market_analysis, "enforce_quality"):
            analysis_kwargs["enforce_quality"] = True
        if _supports_keyword(run_market_analysis, "previous_quality"):
            analysis_kwargs["previous_quality"] = previous_quality
        if _supports_keyword(run_market_analysis, "expected_companies"):
            analysis_kwargs["expected_companies"] = expected_companies
        if _supports_keyword(run_market_analysis, "market_coldness_evidence"):
            analysis_kwargs["market_coldness_evidence"] = market_coldness_evidence
        if _supports_keyword(run_market_analysis, "quality_history_loader"):
            analysis_kwargs["quality_history_loader"] = fetch_quality_history_batch
        if _supports_keyword(run_market_analysis, "quality_history_progress_cb"):
            analysis_kwargs["quality_history_progress_cb"] = quality_history_cb
        if _supports_keyword(run_market_analysis, "type3_growth_loader"):
            analysis_kwargs["type3_growth_loader"] = fetch_growth_evidence_batch
        if _supports_keyword(run_market_analysis, "type3_growth_progress_cb"):
            analysis_kwargs["type3_growth_progress_cb"] = type3_growth_cb
        if _supports_keyword(run_market_analysis, "research_report_loader"):
            analysis_kwargs["research_report_loader"] = fetch_research_reports_batch
        if _supports_keyword(run_market_analysis, "patch4_loader"):
            analysis_kwargs["patch4_loader"] = fetch_patch4_evidence_batch
        analysis = run_market_analysis(
            analysis_quotes,
            analysis_financials,
            **analysis_kwargs,
        )
        if not isinstance(analysis.scores, pd.DataFrame) or analysis.scores.empty:
            raise RuntimeError("分析管道未产生任何股票评分")

        # This explicit UI-side gate prevents a merely non-empty, severely
        # degraded analysis from replacing the last-known-good generation.
        quality = dict(
            validate_market_analysis_quality(
                analysis,
                expected_companies=expected_companies,
                previous=previous_quality if isinstance(previous_quality, Mapping) else None,
            )
        )
        pipeline_quality = getattr(analysis, "quality", None)
        if isinstance(pipeline_quality, Mapping):
            quality.update(pipeline_quality)

    except Exception:
        dcf_status.update(label="估值/评分或快照提升失败", state="error")
        score_status.update(label="新结果未替换上一版", state="error")
        growth_status.update(label="可持续高增长型的深层资料未完成", state="error")
        history_status.update(label="优质股权型的长期资料未完成", state="error")
        st.session_state["buy_types_refresh_error"] = "估值或评分未通过完整性校验，新结果未替换上一版。"
        return False

    try:
        successful_state = _build_successful_analysis_state(
            snapshot,
            analysis,
            quality,
            eligible_codes,
            user_evidence=user_evidence,
            market_coldness_status=market_coldness_status,
        )
        # The candidate is promoted only after every durable UI/export value
        # has been materialized successfully.  CAS remains the final mutable
        # operation before the one-shot session-state replacement.
        if snapshot.source == "network":
            save_kwargs = {"data_timestamp": snapshot.data_timestamp}
            if _supports_keyword(save_market_snapshot, "expected_previous_timestamp"):
                save_kwargs["expected_previous_timestamp"] = getattr(snapshot, "baseline_timestamp", None)
            if _supports_keyword(save_market_snapshot, "expected_previous_payload_sha256"):
                save_kwargs["expected_previous_payload_sha256"] = getattr(
                    snapshot,
                    "baseline_payload_sha256",
                    None,
                )
            if _supports_keyword(save_market_snapshot, "analysis_quality"):
                save_kwargs["analysis_quality"] = quality
            if _supports_keyword(save_market_snapshot, "retrieved_at"):
                save_kwargs["retrieved_at"] = getattr(snapshot, "retrieved_at", None)
            save_market_snapshot(
                SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION),
                snapshot.quotes,
                snapshot.financials,
                **save_kwargs,
            )
        try:
            persistent_cache_status = _save_persistent_analysis_state(successful_state)
        except Exception as cache_exc:
            persistent_cache_status = f"write_failed:{type(cache_exc).__name__}:{cache_exc}"
            warning = _public_failure_message(
                successful_state.get("buy_types_snapshot_warning"),
                fallback="",
            )
            successful_state["buy_types_snapshot_warning"] = (
                f"{warning} 分析结果可正常使用，但本地快速启动缓存写入失败。".strip()
            )
        st.session_state["buy_types_analysis_cache_diagnostic"] = persistent_cache_status
        st.session_state.update(successful_state)
    except Exception:
        dcf_status.update(label="估值/评分或快照提升失败", state="error")
        score_status.update(label="新结果未替换上一版", state="error")
        growth_status.update(label="可持续高增长型的深层资料未完成", state="error")
        st.session_state["buy_types_refresh_error"] = "新结果保存或切换失败，已保留上一版结果。"
        return False

    dcf_status.update(
        label=(
            f"估值完成: {len(analysis.dcf_results)} 只有效结果 / "
            f"{analysis.dcf_attempted} 只尝试 / {analysis.dcf_skipped} 只未生成估值（详见分类）"
        ),
        state="complete",
    )
    score_status.update(label=f"指标提取与七类型评分完成: {len(analysis.scores)} 只", state="complete")
    growth_status.update(label="可持续高增长型的深层资料阶段完成（仅核验仍可能达标的公司）", state="complete")
    history_status.update(label="优质股权型的长期资料阶段完成（仅核验仍可能达标的公司）", state="complete")
    st.session_state.pop("buy_types_refresh_error", None)
    st.toast("✅ 分析完成，新一代数据已生效！", icon="🎉")
    return True


def show():
    st.title("🎯 七类型量化诊断与买入信号")
    st.caption(
        "七种买入情况的量化筛选结果；诊断最高分不等于买入信号，"
        "只有明确触发且未被否决的类型才进入买入候选。本页面不构成投资建议。"
    )

    if not isinstance(st.session_state.get("buy_types_df"), pd.DataFrame):
        try:
            _restored, cache_diagnostic = _restore_persistent_analysis_state()
        except Exception as exc:
            cache_diagnostic = f"restore_failed:{type(exc).__name__}:{exc}"
        st.session_state["buy_types_analysis_cache_diagnostic"] = cache_diagnostic
    _invalidate_stale_analysis_state()
    has_last_good = isinstance(st.session_state.get("buy_types_df"), pd.DataFrame)
    force_refresh = bool(st.session_state.pop("force_data_refresh", False))
    user_evidence_payload: object = None
    evidence_input_error = ""
    with st.expander("🧾 高级功能：设置仓位约束（可选）", expanded=False):
        st.caption(
            "这里只接受你自己的单股仓位上限和同类组合仓位上限。公司、行业或估值的手填0–10分，"
            "以及无法由程序逐项复算的研究结论，一律拒绝且不能参与买点判定。"
            "缺少原始数据时程序会显示“证据不足”，不会用主观分数补齐。设置仅用于本次分析，"
            "不会改写市场数据。"
        )
        st.code(
            '{"000001":{"position_size_pct":3,"type6_portfolio_pct":10}}',
            language="json",
        )
        evidence_file = st.file_uploader(
            "选择仓位约束文件（JSON 格式，最大 1MB）",
            type=["json"],
            accept_multiple_files=False,
            key="buy_types_evidence_file",
        )
        if evidence_file is not None:
            try:
                user_evidence_payload = _parse_user_evidence_json(evidence_file.getvalue())
                st.success(f"已读取 {len(user_evidence_payload)} 只股票的候选证据；点击分析后再做逐字段严格校验。")
            except ValueError as exc:
                evidence_input_error = str(exc)
                st.error(evidence_input_error)
    col_left, _ = st.columns([1, 3])
    with col_left:
        if has_last_good:
            manual_analysis = st.button(
                "🔄 用当前快照重新分析",
                width="stretch",
                disabled=bool(evidence_input_error),
            )
        else:
            manual_analysis = st.button(
                "🚀 开始分析（仅分析沪深 A 股）",
                type="primary",
                width="stretch",
                disabled=bool(evidence_input_error),
            )

    if force_refresh or manual_analysis:
        if evidence_input_error:
            st.session_state["buy_types_refresh_error"] = _public_failure_message(
                evidence_input_error,
                fallback="外部证据格式或日期校验未通过，未采用本次补充资料。",
            )
        elif _run_full_analysis(
            force_refresh=force_refresh,
            user_evidence_payload=user_evidence_payload,
        ):
            st.rerun()
        has_last_good = isinstance(st.session_state.get("buy_types_df"), pd.DataFrame)

    if not has_last_good:
        refresh_error = st.session_state.get("buy_types_refresh_error", "")
        if refresh_error:
            st.error("上次分析未产生可用结果。请重新抓取数据后再试；若持续失败，请查看数据来源状态。")
        st.info("👆 点击上方按钮开始沪深 A 股分析和七种买入类型评分")
        st.markdown("""
        **分析内容：**
        1. 抓取沪深行情；源响应中如含北交所记录，会在进入分析快照前剔除
        2. 批量获取最新财报数据
        3. 对非金融公司按未来现金流估值；对银行、保险和券商按盈利能力和净资产估值
        4. 按七种买入情况逐维度评分（0-10分制；优质股权型同时保留三项百分制复算账本）
        5. 展示全部子维度得分 + 评分依据
        """)
        return

    df = _with_diagnostic_fields(st.session_state["buy_types_df"])
    _render_global_status(df)
    _render_type6_global_notice()
    _render_analysis_evidence(df)

    # ── 侧边栏筛选 ──
    with st.sidebar:
        st.header("🔎 筛选条件")
        st.button(
            "重置全部筛选",
            key="reset_buy_type_filters",
            on_click=_reset_buy_type_filters,
            width="stretch",
        )

        st.subheader("买入类型")
        type_checkboxes = {}
        for t in TYPE_ORDER:
            type_checkboxes[t] = st.checkbox(TYPE_NAMES[t], value=True, key=f"cb_{t}")
        include_no_signal = st.checkbox(
            "包含未触发股票",
            value=False,
            key="include_no_signal",
            help="关闭时，只展示至少命中一个已勾选买入类型的股票。",
        )
        include_conditional = st.checkbox(
            "包含待确认候选",
            value=False,
            key="include_conditional",
            help="仅展示尚缺仓位或操作确认的候选；它们不是买入信号。",
        )

        st.divider()
        st.subheader("行业")
        from data.industry import _INDUSTRY_RULES

        _IND_CODE_TO_CN = {c: n for c, n, _ in _INDUSTRY_RULES}
        all_industries = sorted(df["industry"].dropna().unique().tolist())
        selected_industries = st.multiselect(
            "选择行业（留空=全部）",
            options=all_industries,
            format_func=lambda x: _industry_display_name(x, _IND_CODE_TO_CN),
            default=[],
            key="selected_industries",
        )

        st.divider()
        st.subheader("评分 (0-10)")
        score_min = st.slider("最低评分", 0.0, 10.0, 0.0, 0.5, key="score_min")
        score_max = st.slider("最高评分", 0.0, 10.0, 10.0, 0.5, key="score_max")

        st.divider()
        include_missing_metrics = st.checkbox(
            "保留估值/财务指标缺失项",
            value=True,
            key="include_missing_metrics",
            help="仅对下方已启用的区间筛选生效；缺失指标不等于不合格。",
        )

        st.divider()
        st.subheader("PE")
        enable_pe_filter = st.checkbox("启用 PE 区间筛选", value=False, key="enable_pe_filter")
        pe_min = st.number_input(
            "最低PE", -1_000_000.0, 1_000_000.0, -100.0, 1.0, key="pe_min", disabled=not enable_pe_filter
        )
        pe_max = st.number_input(
            "最高PE", -1_000_000.0, 1_000_000.0, 10000.0, 10.0, key="pe_max", disabled=not enable_pe_filter
        )

        st.divider()
        st.subheader("PB")
        enable_pb_filter = st.checkbox("启用 PB 区间筛选", value=False, key="enable_pb_filter")
        pb_min = st.number_input(
            "最低PB", -1_000_000.0, 1_000_000.0, -100.0, 0.1, key="pb_min", disabled=not enable_pb_filter
        )
        pb_max = st.number_input(
            "最高PB", -1_000_000.0, 1_000_000.0, 200.0, 1.0, key="pb_max", disabled=not enable_pb_filter
        )

        st.divider()
        st.subheader("ROE (%)")
        enable_roe_filter = st.checkbox("启用 ROE 区间筛选", value=False, key="enable_roe_filter")
        roe_min = st.number_input(
            "最低ROE%", -1_000_000.0, 1_000_000.0, -50.0, 1.0, key="roe_min", disabled=not enable_roe_filter
        )
        roe_max = st.number_input(
            "最高ROE%", -1_000_000.0, 1_000_000.0, 100.0, 1.0, key="roe_max", disabled=not enable_roe_filter
        )

        st.divider()
        st.subheader("负债率 (%)")
        enable_debt_filter = st.checkbox("启用负债率区间筛选", value=False, key="enable_debt_filter")
        debt_min = st.number_input(
            "最低负债率%", -1_000_000.0, 1_000_000.0, -100.0, 5.0, key="debt_min", disabled=not enable_debt_filter
        )
        debt_max = st.number_input(
            "最高负债率%", -1_000_000.0, 1_000_000.0, 500.0, 10.0, key="debt_max", disabled=not enable_debt_filter
        )

        st.divider()
        st.subheader("最少命中类型数")
        min_types = st.slider("≥", 0, len(TYPE_ORDER), 0, key="min_types")

    # ── 应用筛选 ──
    display = df.copy()

    active_types = [t for t, checked in type_checkboxes.items() if checked]
    display = _filter_type_selection(
        display,
        active_types,
        include_no_signal=include_no_signal,
        include_conditional=include_conditional,
    )

    if selected_industries:
        display = display[display["industry"].isin(selected_industries)]
    diagnostic_scores = pd.to_numeric(display["diagnostic_score"], errors="coerce")
    display = display[(diagnostic_scores >= score_min) & (diagnostic_scores <= score_max)]

    for enabled, col_name, col_min, col_max, scale in [
        (enable_pe_filter, "pe", pe_min, pe_max, 1.0),
        (enable_pb_filter, "pb", pb_min, pb_max, 1.0),
        (enable_roe_filter, "roe", roe_min, roe_max, 100.0),
        (enable_debt_filter, "debt_ratio", debt_min, debt_max, 100.0),
    ]:
        if not enabled:
            continue
        display = _filter_numeric_range(
            display,
            col_name,
            col_min,
            col_max,
            include_missing=include_missing_metrics,
            scale=scale,
        )

    display = display[display["num_types"] >= min_types]

    # Search is part of the displayed result identity, so apply it before all
    # counts and summary metrics. Previously the table could show one row while
    # the headline still reported the pre-search count.
    search_term = st.text_input("🔎 搜索代码或公司名", placeholder="如 600519 或 茅台", key="search_table")
    if search_term:
        display = _filter_stock_search(display, search_term)

    # ── 统计 ──
    st.subheader("📊 筛选结果")
    total_with_signal = len(df[df["num_types"] > 0])
    total_filtered = len(display)

    mc1, mc2, mc3, mc4 = st.columns(4)
    with mc1:
        st.metric("筛选后显示", total_filtered)
    with mc2:
        st.metric("有买入信号", total_with_signal)
    with mc3:
        st.metric("平均诊断分", f"{display['diagnostic_score'].mean():.2f}" if not display.empty else "暂无数据")
    with mc4:
        st.metric("最高诊断分", f"{display['diagnostic_score'].max():.1f}" if not display.empty else "暂无数据")

    st.divider()

    if display.empty:
        st.info("没有符合当前筛选或搜索条件的股票，请重置或放宽条件。")
        return

    # ── 汇总表 ──
    st.subheader(f"📋 股票列表 ({total_filtered} 只)")

    disp_cols = {
        "code": "代码",
        "name": "名称",
        "industry_cn": "行业",
        "diagnostic_label": "诊断框架",
        "diagnostic_score": "诊断最高分",
        "num_types": "命中数",
        "price": "股价",
        "pe": "PE",
        "pb": "PB",
        "roe": "ROE",
        "debt_ratio": "负债率",
    }
    from data.industry import _INDUSTRY_RULES

    _IND_CN = {c: n for c, n, _ in _INDUSTRY_RULES}
    display = display.sort_values("diagnostic_score", ascending=False, kind="stable").copy()
    display["industry_cn"] = display["industry"].apply(lambda code: _industry_display_name(code, _IND_CN))
    avail_cols = [c for c in disp_cols if c in display.columns]
    show_df = display[avail_cols].rename(columns={c: disp_cols[c] for c in avail_cols}).copy()

    for c in show_df.columns:
        if c in ["PE", "PB", "股价"]:
            show_df[c] = show_df[c].apply(
                lambda x: f"{x:.1f}" if pd.notna(x) and isinstance(x, (int, float)) else str(x)
            )
        elif c in ["ROE", "负债率"]:
            show_df[c] = show_df[c].apply(
                lambda x: f"{x * 100:.1f}%" if pd.notna(x) and isinstance(x, (int, float)) else str(x)
            )
        elif c == "诊断最高分":
            show_df[c] = show_df[c].apply(
                lambda x: f"{x:.1f}" if pd.notna(x) and isinstance(x, (int, float)) else str(x)
            )

    st.caption(f"共 {len(show_df)} 只 · 点击行查看详细分析")
    selected_rows = st.dataframe(
        show_df,
        width="stretch",
        height=500,
        selection_mode="single-row",
        on_select="rerun",
        column_config={
            "代码": st.column_config.TextColumn(width="small"),
            "名称": st.column_config.TextColumn(width="medium"),
        },
        key="stock_table",
    )

    if (
        selected_rows is not None
        and hasattr(selected_rows, "selection")
        and selected_rows.selection
        and len(selected_rows.selection.rows) > 0
    ):
        sel_idx = selected_rows.selection.rows[0]
        if sel_idx < len(show_df):
            sel_code = str(show_df.iloc[sel_idx].get("代码", ""))
            row_data = display[display["code"].astype(str).str.strip() == sel_code]
            if not row_data.empty:
                _render_stock_inline(row_data.iloc[0])


if __name__ == "__main__":
    show()
