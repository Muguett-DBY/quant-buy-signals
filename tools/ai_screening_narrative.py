"""Turn audited AI screening fields into a short, human-readable thesis.

The narrative is deliberately deterministic.  It does not invent a business
fact or a valuation number; it only groups the already reviewed strengths and
risks into plain-language themes.  Exact figures remain in the evidence
sections of the public artifact.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


_VALUATION_PREFIX_RE = re.compile(r"^(?:估值快照|当前估值|收盘估值)[：:]\s*[^；;。]*[；;。]?\s*")
_CONCLUSION_SUFFIX_RE = re.compile(r"(?:[；;。]\s*)?(?:独立结论|AI结论|当前结论)[：:].*$", re.IGNORECASE)
_RULE_TEXT_RE = re.compile(
    r"(?:\btype\s*[1-7]\b|类型\s*[1-7]|七种买入|确定性规则|候选池|规则(?:触发|达标|分数|状态)|"
    r"估值买入区|强周期底部|长坡厚雪|可持续高增长|两热一冷)",
    re.IGNORECASE,
)
_LOW_SIGNAL_RE = re.compile(
    r"(?:联网核验|搜索|来源|资料不足|证据不足|待核验|尚未确认|可在定期报告中|"
    r"后续可与正式财报|仅作估值线索|估值线索|不替代财报)",
    re.IGNORECASE,
)
_POSITIVE_RE = re.compile(r"(?:增长|改善|提升|增加|为正|稳定|领先|优势|支点|订单|分红|回购|回报)")
_NEGATIVE_RE = re.compile(r"(?:下降|下滑|为负|恶化|承压|压力|风险|不足|偏高|透支|波动|不确定|缺失|亏损)")


def _text(value: Any, limit: int = 360) -> str:
    return str(value or "").strip()[:limit]


def _clean_clause(value: Any) -> str:
    text = _text(value)
    text = _VALUATION_PREFIX_RE.sub("", text)
    text = _CONCLUSION_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip("；;。 ")
    return text


def _clauses(value: Any) -> list[str]:
    text = _clean_clause(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[；;。！？!?\n]+", text) if part.strip()]


def _is_low_signal(text: str) -> bool:
    return not text or bool(_RULE_TEXT_RE.search(text) or _LOW_SIGNAL_RE.search(text))


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _theme(text: str, *, risk: bool) -> str:
    """Map a reviewed clause to a semantic phrase without changing its stance."""

    if risk:
        if _has(text, ("应收", "回款", "赊销")):
            return "应收账款和回款占用仍需盯紧"
        if _has(text, ("客户集中", "区域集中", "单一客户", "大客户")):
            return "客户或区域集中度带来经营波动"
        if _has(text, ("现金流", "经营现金")) and _has(text, ("负", "下降", "下滑", "压力", "不足")):
            return "利润转成现金的能力仍需验证"
        if _has(text, ("库存", "存货")):
            return "库存和去化速度可能拖累现金回报"
        if _has(text, ("费用", "成本", "毛利率", "利润率")) and _has(text, ("上升", "增加", "下滑", "承压", "压力")):
            return "成本或费用压力可能侵蚀利润"
        if _has(text, ("周期", "需求", "景气", "价格战")):
            return "业务仍受行业周期和需求波动影响"
        if _has(text, ("PE", "PB", "估值", "价格", "安全边际")) and _has(
            text, ("高", "偏贵", "透支", "薄", "不足", "负")
        ):
            return "当前价格的安全边际不算厚"
        if _has(text, ("负债", "杠杆", "流动性", "偿债")):
            return "负债和流动性约束需要持续观察"
        if _has(text, ("商誉", "并购", "收购")):
            return "商誉或并购兑现增加不确定性"
        if _has(text, ("减值", "资产质量", "坏账")):
            return "资产减值或坏账风险需要留意"
        if _has(text, ("政策", "监管", "环保", "碳")):
            return "政策、监管或合规要求可能改变回报"
        if _has(text, ("汇率", "关税", "贸易", "海外")):
            return "海外业务面临汇率、关税或贸易变量"
        if _has(text, ("治理", "管理层", "内控")):
            return "治理和管理兑现仍需证据"
        if _has(text, ("利润", "净利", "毛利率", "盈利")) and _has(
            text, ("下降", "下滑", "承压", "波动", "亏损", "为负", "恶化", "低基数", "非经常")
        ):
            return "盈利质量仍在承压"
        if _has(text, ("收入", "营收")) and _has(text, ("下降", "下滑", "需求弱", "承压")):
            return "收入和需求端仍有压力"
        return "现金回报、盈利持续性或估值仍需验证"

    if (
        _has(text, ("收入", "营收"))
        and _has(text, ("利润", "净利"))
        and _has(text, ("现金流", "经营现金"))
        and _has(text, ("增长", "改善", "为正", "稳定"))
    ):
        return "收入、利润和经营现金流同步改善"
    if _has(text, ("海外收入", "海外业务", "海外销售", "出口")) and _has(text, ("增长", "提升", "占")):
        return "海外业务正在形成增长支点"
    if _has(text, ("现金流", "经营现金")) and _has(text, ("增长", "改善", "为正", "稳定")):
        return "经营现金流能够跟上经营改善"
    if _has(text, ("收入", "营收")) and _has(text, ("增长", "提升", "增加", "稳定")):
        return "收入保持增长并提供经营基础"
    if _has(text, ("净利润", "归母", "利润", "盈利")) and _has(text, ("增长", "提升", "增加", "稳定")):
        return "盈利能力正在改善"
    if _has(text, ("订单", "需求", "客户", "市场份额")) and _has(text, ("增长", "增加", "领先", "改善", "稳定")):
        return "订单、客户或市场份额提供了后续兑现线索"
    if _has(text, ("业务", "主营", "产品", "品牌", "渠道", "资源", "产业链", "产能", "一体化", "成本")) and not _has(
        text, ("下降", "下滑", "亏损", "承压", "压力")
    ):
        return "主营业务、资源或渠道形成了竞争基础"
    if _has(text, ("毛利率", "利润率", "ROE")) and _has(text, ("提升", "上升", "改善", "稳定", "较高")):
        return "盈利能力和资本回报保持改善"
    if _has(text, ("研发", "产品", "技术", "专利")):
        return "研发和产品能力提供了竞争优势线索"
    if _has(text, ("分红", "回购", "股东回报")):
        return "股东回报安排较明确"
    if _has(text, ("PE", "PB", "估值", "价格", "安全边际")) and _has(
        text, ("合理", "低估", "折价", "安全")
    ):
        return "当前价格没有明显透支经营预期"
    if _POSITIVE_RE.search(text) and not _NEGATIVE_RE.search(text):
        return "公司经营层面有可核验的改善信号"
    return ""


def _themes(values: Any, *, risk: bool, limit: int = 3) -> list[str]:
    if not isinstance(values, list):
        values = []
    result: list[str] = []
    for value in values:
        # Keep a multi-metric strength together before splitting punctuation.
        # A single reviewed fact often contains revenue, profit and cash-flow
        # changes separated by semicolons; treating each fragment separately
        # would make the explanation sound like a list instead of one thesis.
        whole = _clean_clause(value)
        if not risk and whole and not _is_low_signal(whole):
            combined = _theme(whole, risk=False)
            if combined == "收入、利润和经营现金流同步改善":
                result.append(combined)
                if len(result) >= limit:
                    return result
                continue
        for clause in _clauses(value):
            if _is_low_signal(clause):
                continue
            theme = _theme(clause, risk=risk)
            if theme and theme not in result:
                result.append(theme)
            if len(result) >= limit:
                return result
    return result


def _join(values: list[str], fallback: str) -> str:
    return "；".join(values) if values else fallback


def build_human_explanation(review: Mapping[str, Any], company_name: str = "该公司") -> dict[str, Any]:
    """Build the UI-facing human explanation from an audited review row."""

    name = _text(company_name, 160) or "该公司"
    action = _text(review.get("ai_action"), 32)
    strengths = _themes(review.get("key_strengths"), risk=False, limit=2)
    if not strengths:
        strengths = _themes(_clauses(review.get("summary")), risk=False, limit=2)
    risks = _themes(review.get("risk_flags"), risk=True, limit=2)
    if not risks:
        risks = _themes(_clauses(review.get("summary")), risk=True, limit=2)
    support_text = _join(strengths, "目前还没有形成足够清晰的竞争优势闭环")
    risk_text = _join(risks, "关键经营和估值问题还需要更多证据")

    if action == "priority_buy":
        thesis = f"{name}值得考虑的核心在于{support_text}。"
        why = f"支持买入的是{support_text}；需要盯住{risk_text}，因此只能按有前提的买入理解。"
        heading = "为什么建议买"
    elif action == "watchlist":
        thesis = f"{name}并非没有亮点，{support_text}，但现在还没有足够安全边际。"
        why = f"先观察一段时间，看看{risk_text}能否改善，再判断{support_text}能不能变成可持续回报。"
        heading = "为什么先观察"
    else:
        thesis = f"{name}当前不适合买入，关键障碍是{risk_text}。"
        why = f"目前最需要解决的是{risk_text}；即使有{support_text}，也不值得为尚未兑现的预期承担这个价格。"
        heading = "为什么不建议买"

    return {
        "heading": heading,
        "thesis": thesis,
        "why_this_action": why,
        "supporting_points": strengths,
        "watch_items": risks,
        "knowledge_base_note": (
            "本轮按《模板汇总MD》中的“好公司+好价格”、未来自由现金流、竞争优势、周期位置和治理检查框架组织判断；"
            "知识库是检查清单，不替代公司事实。"
        ),
    }
