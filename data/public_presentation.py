"""Plain-language presentation helpers for public desktop and mobile views.

Audit payloads intentionally retain machine-readable identifiers.  These
helpers define the separate boundary used by ordinary user-facing text.
"""

from __future__ import annotations

import re
from typing import Any

from data.industry import _INDUSTRY_RULES


_INDUSTRY_CODE_TO_CN = {code: name for code, name, _keywords in _INDUSTRY_RULES}
_CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
_PUBLIC_REASON_EXACT = {
    "missing_quote": "未取得有效行情",
    "invalid_price_or_market_cap": "当前价格或总市值无效",
    "invalid_derived_shares": "无法根据价格和总市值推算有效股本",
    "valuation_returned_non_mapping": "估值模型未返回可用结果",
    "valuation_evidence_invalid": "估值结果未通过源数据一致性核验",
    "mixed_profit_cycle_unsupported_by_fcff": "当前盈利周期不适合使用现金流估值",
    "nonpositive_pessimistic_equity_value": "悲观情景下的股权价值不为正",
    "internal_error": "计算过程发生内部异常，相关结果未被采用",
    "source_missing": "所需源数据暂时缺失",
    "inconsistent_source": "不同来源的数据存在矛盾",
    "model_unsupported": "当前估值模型暂不支持该公司",
    "economic_not_applicable": "当前经济条件不适用该估值模型",
}
_LEGACY_PUBLIC_TEXT_REPLACEMENTS = (
    ("第1模板", "长期质量与回报评分"),
    ("第5模板", "产业质量与估值评分"),
    ("补丁5安全边际", "安全边际"),
    ("补丁5", "商业质量与安全边际评分"),
    ("补丁6", "七类型规则"),
    ("模板25", "金融公司估值方法"),
    ("投入回报增长模板", "可持续高增长型"),
    ("小盘高风险模板", "小盘高风险型"),
    ("增长模板", "增长型"),
)
_FINANCE_TERM_REPLACEMENTS = (
    ("EV/EBITDA", "企业价值/息税折旧摊销前利润"),
    ("EV/Sales", "企业价值/营业收入"),
    ("FCFF", "企业自由现金流"),
    ("FCF", "自由现金流"),
    ("CAPEX", "资本开支"),
    ("Capex", "资本开支"),
    ("CFO", "经营现金流"),
    ("ROIC", "投入资本回报率"),
    ("WACC", "加权平均资本成本"),
    ("DCF", "现金流折现估值"),
    ("TTM", "最近十二个月"),
    ("CAGR", "年复合增速"),
    ("YTD", "年初至今"),
    ("PEG", "市盈率相对盈利增长比"),
    ("MRQ", "最近一季"),
    ("PE/PB/PS", "市盈率/市净率/市销率"),
    ("PE/PB", "市盈率/市净率"),
    ("P/E", "市盈率"),
    ("P/B", "市净率"),
    ("P/S", "市销率"),
    ("PE", "市盈率"),
    ("PB", "市净率"),
    ("PS", "市销率"),
)
_EVIDENCE_NOTE = re.compile(
    r"\s*[（(]\s*(?:证据|evidence)\s*[:：][^）)]*[）)]",
    flags=re.IGNORECASE,
)
_INTERNAL_REASON_IDENTIFIER = re.compile(
    r"""
    (?:
        \bpatch\d+(?:[-_][a-z0-9]+)+\b
        |\b(?:model_id|schema_version|derived_proxy|reported_formula|formula_version
             |validation_status|source_rule|evidence_level)\b
        |\b(?:model|schema|formula|proxy|validation|evidence)\s*=
        |\b(?:[a-z][a-z0-9]*_){1,}[a-z0-9]+\b
        |\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}-v\d+\b
        |\b[a-z_][a-z0-9_]*\s*[-+*/><=]\s*[a-z_(][a-z0-9_(]*
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def public_reason_text(value: Any) -> str:
    """Replace machine identifiers/formulas with a stable Chinese explanation."""
    text = str(value or "").strip()
    lowered = text.casefold()
    exact = _PUBLIC_REASON_EXACT.get(lowered)
    if exact:
        return exact
    if lowered.startswith("valuation_exception:"):
        return "估值计算发生内部异常，相关结果未被采用"
    for legacy, readable in _LEGACY_PUBLIC_TEXT_REPLACEMENTS:
        text = text.replace(legacy, readable)
    for finance_term, readable in _FINANCE_TERM_REPLACEMENTS:
        text = text.replace(finance_term, readable)
    lowered = text.casefold()

    def remove_internal_note(match: re.Match[str]) -> str:
        return "" if _INTERNAL_REASON_IDENTIFIER.search(match.group(0)) else match.group(0)

    cleaned = _EVIDENCE_NOTE.sub(remove_internal_note, text).strip().rstrip("；;，,")
    if cleaned != text and cleaned:
        return cleaned
    if _INTERNAL_REASON_IDENTIFIER.search(text):
        if "type2c" in lowered or "量价" in text:
            return "量价与换手数据"
        return "可核验的财务与行业数据"
    return text


def public_industry_name(value: Any, *, explicit_name: Any = None) -> str:
    """Return a Chinese display name without leaking internal industry enums."""
    supplied = str(explicit_name or "").strip()
    if supplied and _CHINESE_CHARACTER.search(supplied):
        return supplied

    raw = str(value or "").strip()
    if raw == "DEFAULT":
        return "未分类（低置信度）"
    mapped = _INDUSTRY_CODE_TO_CN.get(raw)
    if mapped:
        return mapped
    if raw and _CHINESE_CHARACTER.search(raw):
        return raw
    return "未分类（低置信度）" if raw else "未知"


__all__ = ["public_industry_name", "public_reason_text"]
