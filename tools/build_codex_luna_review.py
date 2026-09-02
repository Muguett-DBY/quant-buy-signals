"""Build a full local Codex/Luna company review from the frozen research packet.

This path is intentionally independent of OpenCode, Reasonix and provider
quotas.  It does not claim that a web search happened: every public reason is
bound to the generation's dated research facts and their HTTPS source refs.
The deterministic seven-type admission result is used only as a queue input;
it is never copied into the company thesis shown to users.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import LOCAL_REVIEW_MODEL, REVIEW_SCHEMA_VERSION, validate_review


MARKET_DATE = "2026-08-24"
MODEL = LOCAL_REVIEW_MODEL
EFFORT = "max"
DEFAULT_KNOWLEDGE_PATH = Path("tools/ai_company_research_knowledge.md")
MIN_BUY_FCF_MARGIN = 0.03
MIN_BUY_FCF_HISTORY_AVERAGE = 0.0
MAX_BUY_PB_STRETCH_RATIO = 2.5
MAX_BUY_INTERIM_CASHFLOW_DECLINE = -10.0
MAX_BUY_INTERIM_REVENUE_DECLINE = -5.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fact_map(company: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        _text(item.get("id")): item
        for item in company.get("facts", [])
        if isinstance(item, Mapping) and _text(item.get("id"))
    }


def _fact(company: Mapping[str, Any], fact_id: str) -> Mapping[str, Any] | None:
    for item in company.get("facts", []):
        if isinstance(item, Mapping) and _text(item.get("id")) == fact_id:
            return item
    return None


def _statement(company: Mapping[str, Any], fact_id: str, fallback: str = "") -> str:
    item = _fact(company, fact_id)
    return _text(item.get("statement")) if item else fallback


def _source_refs(item: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(item, Mapping):
        return []
    raw = item.get("source_refs")
    refs = [str(value).strip() for value in raw if str(value).strip()] if isinstance(raw, list) else []
    singular = _text(item.get("source_ref"))
    if singular:
        refs.insert(0, singular)
    return list(dict.fromkeys(value for value in refs if value.startswith("https://")))


def _snapshot(company: Mapping[str, Any]) -> dict[str, float | None]:
    raw = company.get("industry_valuation_relative")
    raw = raw if isinstance(raw, Mapping) else {}
    snap = raw.get("company_snapshot")
    snap = snap if isinstance(snap, Mapping) else {}
    fields = raw.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}

    def value(name: str) -> float | None:
        direct = _number(snap.get(name))
        if direct is not None:
            return direct
        item = fields.get(name)
        return _number(item.get("value")) if isinstance(item, Mapping) else _number(item)

    return {
        "price": value("price"),
        "pe": value("pe"),
        "pb": value("pb"),
        "market_cap": value("market_cap"),
        "pe_median": value("pe_median"),
        "pb_median": value("pb_median"),
    }


def _annual(company: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = [item for item in company.get("annual_history", []) if isinstance(item, Mapping)]
    return sorted(rows, key=lambda item: int(item.get("year") or 0))


def _cagr(first: float | None, last: float | None, years: int = 2) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def _pct(value: float | None) -> str:
    return "未知" if value is None else f"{value * 100:.1f}%"


def _hundred_million(value: float | None) -> str:
    return "未知" if value is None else f"{value / 1e8:.2f}亿元"


def _reported_billion_yuan(value: float | None) -> str:
    """Format research-package interim values, which are already in 亿元."""
    return "未知" if value is None else f"{value:.2f}亿元"


def _latest_interim(company: Mapping[str, Any]) -> tuple[str, str]:
    income = _statement(company, "latest_income")
    cashflow = _statement(company, "latest_cashflow")
    return income, cashflow


def _cashflow_values(statement: str) -> tuple[float | None, float | None]:
    def value(label: str) -> float | None:
        match = re.search(label + r"\s*([-−+]?\d+(?:\.\d+)?)", statement)
        if not match:
            return None
        return _number(match.group(1).replace("−", "-"))

    return value("经营活动现金流净额"), value("(?:简化)?自由现金流")


def _interim_yoy(statement: str, metric: str) -> float | None:
    match = re.search(metric + r"[^\n]{0,32}?同比\s*([-−+]?\d+(?:\.\d+)?)%", statement)
    if not match:
        return None
    return _number(match.group(1).replace("−", "-"))


def _category(industry: str) -> str:
    if industry in {
        "化工",
        "工业机械",
        "建材",
        "有色金属",
        "钢铁",
        "煤炭",
        "石油天然气",
        "汽车整车",
        "汽车零部件",
        "工程机械",
        "电气设备",
        "交通运输",
    }:
        return "cyclical"
    if industry in {
        "半导体",
        "电子元器件",
        "软件互联网",
        "通信设备",
        "传媒游戏",
        "医疗服务",
        "化学制药",
        "生物制药",
        "中药",
        "专业技术服务",
    }:
        return "growth"
    if industry in {"银行", "证券", "保险", "房地产"}:
        return "quality_equity"
    if industry in {"食品饮料", "家电", "纺织服装", "轻工制造", "酿酒行业", "农林牧渔"}:
        return "compounder"
    return "other"


def _score_and_reasons(
    company: Mapping[str, Any], packet: Mapping[str, Any]
) -> tuple[float, str, list[str], list[str], list[str], str]:
    code = _text(packet.get("security_code"))
    name = _text(packet.get("name")) or _text(company.get("name")) or code
    industry = _text(company.get("industry"))
    category = _category(industry)
    snap = _snapshot(company)
    annual = _annual(company)
    latest = annual[-1] if annual else {}
    previous = annual[-2] if len(annual) >= 2 else {}
    first = annual[0] if annual else {}
    deterministic = packet.get("deterministic")
    deterministic = deterministic if isinstance(deterministic, Mapping) else {}
    candidate_status = _text(deterministic.get("status"))
    revenue = _number(latest.get("revenue"))
    profit = _number(latest.get("parent_net_profit"))
    ocf = _number(latest.get("operating_cashflow"))
    fcf = _number(latest.get("free_cashflow"))
    previous_profit = _number(previous.get("parent_net_profit"))
    revenue_cagr = _cagr(_number(first.get("revenue")), revenue)
    profit_cagr = _cagr(_number(first.get("parent_net_profit")), profit)
    profit_yoy = (
        (profit / previous_profit - 1) if profit is not None and previous_profit and previous_profit > 0 else None
    )
    fcf_positive = sum(1 for row in annual if (_number(row.get("free_cashflow")) or 0) > 0)
    annual_fcf_values = [
        value for value in (_number(row.get("free_cashflow")) for row in annual[-3:]) if value is not None
    ]
    fcf_history_average = sum(annual_fcf_values) / len(annual_fcf_values) if len(annual_fcf_values) == 3 else None
    fcf_history_ready = (
        fcf_history_average is not None
        and fcf_history_average > MIN_BUY_FCF_HISTORY_AVERAGE
        and sum(value > 0 for value in annual_fcf_values) >= 2
    )
    fcf_margin = fcf / revenue if fcf is not None and revenue and revenue > 0 else None
    fcf_quality_ready = fcf_margin is None or fcf_margin >= MIN_BUY_FCF_MARGIN
    pe = snap.get("pe")
    pb = snap.get("pb")
    pe_median = snap.get("pe_median")
    pb_median = snap.get("pb_median")
    pb_stretch_ratio = pb / pb_median if pb is not None and pb_median and pb_median > 0 else None

    score = 50.0
    strengths: list[str] = []
    risks: list[str] = []
    facts: list[str] = []

    valuation = _statement(company, "valuation")
    if valuation:
        facts.append(valuation)
    if pe is not None and pe > 0:
        if pe_median and pe < pe_median * 0.75:
            score += 13
            strengths.append(
                f"交易日 {MARKET_DATE} 的 PE {pe:.2f} 倍，低于同业中位数 {pe_median:.2f} 倍，价格端有缓冲。"
            )
        elif pe_median and pe <= pe_median * 1.1:
            score += 5
            strengths.append(f"交易日 {MARKET_DATE} 的 PE {pe:.2f} 倍接近同业中位数 {pe_median:.2f} 倍，估值不算极端。")
        elif pe_median:
            premium = pe / pe_median - 1
            score -= min(14, round(premium * 30, 1))
            risks.append(
                f"交易日 {MARKET_DATE} 的 PE {pe:.2f} 倍高于同业中位数 {pe_median:.2f} 倍约 {_pct(premium)}，安全边际偏薄。"
            )
        elif pe > 45:
            score -= 12
            risks.append(f"交易日 {MARKET_DATE} 的 PE {pe:.2f} 倍偏高，盈利兑现不足时安全边际有限。")
    elif pb is not None and pb > 0:
        if pb_median and pb < pb_median * 0.7:
            score += 9
            strengths.append(f"交易日 {MARKET_DATE} 的 PB {pb:.2f} 倍低于同业中位数 {pb_median:.2f} 倍。")
        elif pb > 5:
            score -= 10
            risks.append(f"交易日 {MARKET_DATE} 的 PB {pb:.2f} 倍较高，需要更强的资产回报来支撑。")
    else:
        score -= 8
        risks.append(f"交易日 {MARKET_DATE} 缺少可用 PE/PB，估值安全边际无法直接确认。")
    if pb_stretch_ratio is not None and pb_stretch_ratio > 2:
        score -= min(12, round((pb_stretch_ratio - 2) * 4, 1))
        risks.append(
            f"交易日 {MARKET_DATE} 的 PB {pb:.2f} 倍约为同业中位数 {pb_median:.2f} 倍的 {pb_stretch_ratio:.1f} 倍，资产估值溢价需要额外业务证据。"
        )

    if fcf is not None and fcf > 0 and ocf is not None and ocf > 0:
        score += 12
        strengths.append(
            f"2025 年经营现金流 {_hundred_million(ocf)}、简化自由现金流 {_hundred_million(fcf)} 均为正，现金转化尚可。"
        )
    elif fcf is not None and fcf <= 0:
        score -= 15
        risks.append(f"2025 年简化自由现金流 {_hundred_million(fcf)}，当前现金回报不足以支撑积极结论。")
    elif ocf is not None and ocf <= 0:
        score -= 14
        risks.append(f"2025 年经营现金流 {_hundred_million(ocf)} 为负，利润质量需要先解释。")
    if fcf_positive >= 3:
        score += 5
    elif fcf_positive == 0:
        score -= 7
    if fcf_margin is not None:
        facts.append(f"2025 年简化自由现金流率 {_pct(fcf_margin)}（自由现金流/营业收入）。")
        if fcf_margin < MIN_BUY_FCF_MARGIN:
            score -= 10
            risks.append(
                f"2025 年简化自由现金流率 {_pct(fcf_margin)} 低于 {MIN_BUY_FCF_MARGIN:.0%}，虽为正值但现金回报偏薄，不能支持积极买入结论。"
            )
    if fcf_history_average is not None:
        facts.append(f"2023—2025 年简化自由现金流均值 {_hundred_million(fcf_history_average)}。")
    if not fcf_history_ready:
        score -= 8
        risks.append("2023—2025 年自由现金流序列的均值不为正或完整性不足，不能仅凭最新一年转正确认买入。")

    if profit is not None and profit > 0:
        if profit_yoy is not None and profit_yoy > 0.15:
            score += 8
            strengths.append(f"2025 年归母净利润 {_hundred_million(profit)}，较 2024 年增长 {_pct(profit_yoy)}。")
        elif profit_yoy is not None and profit_yoy < -0.15:
            score -= 9
            risks.append(f"2025 年归母净利润 {_hundred_million(profit)}，较 2024 年下降 {_pct(profit_yoy)}。")
        elif profit_cagr is not None and profit_cagr > 0:
            score += 3
    else:
        score -= 18
        risks.append("2025 年归母净利润不为正，不能把低 PE 或资产折价当作买入理由。")
    if revenue_cagr is not None and revenue_cagr < -0.12:
        score -= 8
        risks.append(f"2023—2025 年收入复合增速 {_pct(revenue_cagr)}，主营规模仍在收缩。")
    elif revenue_cagr is not None and revenue_cagr > 0.12:
        score += 6
        strengths.append(f"2023—2025 年收入复合增速 {_pct(revenue_cagr)}，规模端仍有扩张。")

    quality = _statement(company, "capital_quality")
    roic: float | None = None
    if quality:
        facts.append(quality)
        match = re.search(r"ROIC[^0-9-]*([0-9]+(?:\.[0-9]+)?)%", quality)
        roic = float(match.group(1)) if match else None
        if roic is not None and roic >= 10:
            score += 7
            strengths.append(f"2025 年 ROIC {roic:.2f}%，资本回报达到可观察水平。")
        elif roic is not None and roic < 3:
            score -= 7
            risks.append(f"2025 年 ROIC {roic:.2f}%，再投资回报偏弱。")

    interim_income, interim_cash = _latest_interim(company)
    interim_cashflow_conflict = False
    interim_profit_decline: float | None = None
    interim_revenue_decline: float | None = None
    interim_ocf_decline: float | None = None
    interim_fcf_decline: float | None = None
    if interim_income:
        facts.append(interim_income)
        interim_profit_decline = _interim_yoy(interim_income, "归母净利润")
        interim_revenue_decline = _interim_yoy(interim_income, "营业收入")
        if interim_profit_decline is not None and interim_profit_decline <= -30:
            score -= 15
            risks.insert(
                0,
                f"最新可得中期归母净利润同比下降 {_pct(interim_profit_decline / 100)}，收入与盈利同时走弱时不宜追价。",
            )
        elif interim_profit_decline is not None and interim_profit_decline < 0:
            score -= 4
            risks.append(f"最新可得中期数据仍有同比下滑：{interim_income}。")
        elif interim_profit_decline is not None and interim_profit_decline >= 15:
            score += 4
            strengths.append(f"最新可得中期数据出现较快增长：{interim_income}。")
    if interim_cash:
        facts.append(interim_cash)
        interim_ocf, interim_fcf = _cashflow_values(interim_cash)
        interim_ocf_decline = _interim_yoy(interim_cash, "经营活动现金流净额")
        interim_fcf_decline = _interim_yoy(interim_cash, "自由现金流")
        if (interim_ocf is not None and interim_ocf < 0) or (interim_fcf is not None and interim_fcf < 0):
            interim_cashflow_conflict = True
            score -= 7
            if interim_ocf is not None and interim_ocf < 0:
                risks.append(
                    f"最新可得中期经营活动现金流为 {_reported_billion_yuan(interim_ocf)}，简化自由现金流为 {_reported_billion_yuan(interim_fcf)}，现金回报转弱。"
                )
            else:
                risks.append(
                    f"最新可得中期经营活动现金流仍为 {_reported_billion_yuan(interim_ocf)}，但资本开支后简化自由现金流为 {_reported_billion_yuan(interim_fcf)}，不满足当前买入的现金回报条件。"
                )
        cashflow_declines = [value for value in (interim_ocf_decline, interim_fcf_decline) if value is not None]
        if cashflow_declines and min(cashflow_declines) <= -30:
            score -= 8
            risks.append(
                f"最新可得中期现金流同比明显走弱：经营现金流 {_pct(interim_ocf_decline / 100) if interim_ocf_decline is not None else '未知'}、"
                f"自由现金流 {_pct(interim_fcf_decline / 100) if interim_fcf_decline is not None else '未知'}，不能仅凭年度现金流确认买入。"
            )
        elif cashflow_declines and min(cashflow_declines) < 0:
            decline = min(cashflow_declines)
            score -= min(6, round(abs(decline) / 5, 1))
            risks.append(
                f"最新可得中期现金流同比下滑：经营现金流 {_pct(interim_ocf_decline / 100) if interim_ocf_decline is not None else '未知'}、"
                f"自由现金流 {_pct(interim_fcf_decline / 100) if interim_fcf_decline is not None else '未知'}，现金趋势待验证。"
            )

    shareholder = company.get("shareholder_returns")
    missing_returns = shareholder.get("missing_fields", []) if isinstance(shareholder, Mapping) else []
    if isinstance(missing_returns, list) and missing_returns:
        score -= min(6, len(missing_returns))
        risks.append("研究包未形成完整的分红、回购或稀释历史，股东回报需要后续核验。")
    if not strengths:
        strengths.append("研究包保留了有报告期和来源的财务事实，但尚未形成足够强的正向证据闭环。")
    if not risks:
        risks.append("公司治理、行业竞争和未来现金流仍需持续跟踪，当前结论不是自动交易指令。")

    # The local reviewer may recommend independently, but only when cash flow,
    # profitability and valuation are simultaneously usable.  This avoids the
    # old failure mode of turning a high deterministic score into a buy signal.
    cycle_history_ready = category != "cyclical" or len(annual) >= 5
    if category == "cyclical" and not cycle_history_ready:
        risks.insert(0, "研究包仅有 2023—2025 三年年度序列，无法验证完整商品/产能周期，周期估值只能作为观察线索。")
    severe_recent_decline = (
        interim_profit_decline is not None
        and interim_profit_decline <= -30
        and interim_revenue_decline is not None
        and interim_revenue_decline <= -10
    )
    annual_or_interim_trend_stress = (
        interim_revenue_decline is not None and interim_revenue_decline <= MAX_BUY_INTERIM_REVENUE_DECLINE
    ) or (interim_profit_decline is not None and interim_profit_decline <= -5)
    cashflow_trend_stress = any(
        value is not None and value <= MAX_BUY_INTERIM_CASHFLOW_DECLINE
        for value in (interim_ocf_decline, interim_fcf_decline)
    )
    capital_return_ready = roic is not None and roic >= 5
    evidence_ready = (
        len(annual) >= 3
        and profit is not None
        and profit > 0
        and ocf is not None
        and ocf > 0
        and fcf is not None
        and fcf > 0
        and (pe is not None and pe > 0 or pb is not None and pb > 0)
        and not interim_cashflow_conflict
        and cycle_history_ready
        and not severe_recent_decline
        and not annual_or_interim_trend_stress
        and not cashflow_trend_stress
        and capital_return_ready
        and fcf_quality_ready
        and fcf_history_ready
        and candidate_status != "insufficient_evidence"
        and (pb_stretch_ratio is None or pb_stretch_ratio <= MAX_BUY_PB_STRETCH_RATIO)
    )
    if category == "quality_equity":
        evidence_ready = (
            evidence_ready
            and not missing_returns
            and pe is not None
            and pe_median is not None
            and pe <= pe_median * 1.1
        )
        risks.insert(
            0,
            "金融机构的经营现金流与资本开支不按普通企业自由现金流解读，且研究包未形成完整股东回报核验，暂不升级为买入。",
        )
    if category == "cyclical" and (pe is None or pe <= 0 or pe_median is None):
        evidence_ready = False
    # Keep headroom below a false-precision 100.  A score in the upper 80s or
    # 90s means the available facts are unusually aligned, not certainty.
    score = max(0.0, min(95.0, round(score, 1)))
    if severe_recent_decline:
        action = "avoid"
    elif score >= 85 and evidence_ready:
        action = "priority_buy"
    elif score >= 50:
        action = "watchlist"
    else:
        action = "avoid"
    if action == "watchlist":
        score = min(69.0, score)
    elif action == "avoid":
        score = min(49.0, score)
    else:
        score = max(70.0, score)
    if action == "priority_buy":
        summary_lead = "当前结论：建议买。"
    elif action == "watchlist":
        summary_lead = "当前结论：观察。"
    else:
        summary_lead = "当前结论：不建议。"
    current = (
        f"交易日 {MARKET_DATE} 股价 {snap['price']:.2f} 元"
        if snap.get("price") is not None
        else f"交易日 {MARKET_DATE} 价格未知"
    )
    valuation_text = f"PE {pe:.2f} 倍" if pe is not None else f"PB {pb:.2f} 倍" if pb is not None else "估值倍数缺失"
    basis = ("；".join(strengths[:2]) if strengths else "研究包未形成足够强的正向证据").rstrip("。；")
    counter = (risks[0] if risks else "仍需持续核验行业与治理风险").rstrip("。；")
    summary = (
        f"{summary_lead}{name}（{code}）属于{industry or '未知行业'}，{current}、{valuation_text}；"
        f"2025 年经营现金流 {_hundred_million(ocf)}、自由现金流 {_hundred_million(fcf)}，"
        f"核心依据：{basis}；主要反证：{counter}"
    )
    return score, action, strengths[:3], risks[:4], list(dict.fromkeys(facts))[:8], summary


def _review(packet: Mapping[str, Any], company: Mapping[str, Any]) -> dict[str, Any]:
    code = _text(packet.get("security_code"))
    score, action, strengths, risks, facts, summary = _score_and_reasons(company, packet)
    type_key = _text(packet.get("type_key"))
    category = _category(_text(company.get("industry")))
    action_map = {
        "priority_buy": ("recommend_buy", "recommend_buy", "建议买", "confirmed", "keep"),
        "watchlist": ("observe", "do_not_recommend_buy", "观察", "caution", "manual_review"),
        "avoid": ("do_not_recommend", "do_not_recommend_buy", "不建议", "confirmed", "demote"),
    }
    final_category, final_recommendation, label, verdict, rec_action = action_map[action]
    refs_by_id = {key: _source_refs(value) for key, value in _fact_map(company).items()}
    claims: list[dict[str, Any]] = []
    for statement in facts[:6]:
        item = next(
            (
                value
                for value in company.get("facts", [])
                if isinstance(value, Mapping) and _text(value.get("statement")) == statement
            ),
            None,
        )
        refs = refs_by_id.get(_text(item.get("id")) if isinstance(item, Mapping) else "", [])
        if not refs:
            continue
        claims.append(
            {
                "fact_id": _text(item.get("id")),
                "statement": statement,
                "source_ref": refs[0],
                "source_refs": refs[:8],
                "source_context": _text(item.get("source_kind")),
                "support": (
                    "supports"
                    if _text(item.get("id")) in {"valuation", "latest_cashflow", "annual_cashflow", "capital_quality"}
                    else "contradicts"
                    if statement in risks
                    else "context"
                ),
            }
        )
    years = sorted(
        {
            int(year)
            for item in company.get("facts", [])
            if isinstance(item, Mapping)
            for year in re.findall(r"(?:19|20)\d{2}", _text(item.get("period")))
        }
    )
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": code,
        "company_name": _text(packet.get("name")) or _text(company.get("name")) or code,
        "type_key": type_key,
        "verdict": verdict,
        "recommended_action": rec_action,
        "buy_attractiveness_score": score,
        "ai_action": action,
        "final_category": final_category,
        "final_recommendation": final_recommendation,
        "recommendation_label": label,
        "ai_independent": True,
        "economic_category": category,
        "score_components": {
            "risk_adjusted_expected_return": score,
            "evidence_confidence": max(35.0, min(95.0, score)),
        },
        "confidence": "medium" if action in {"priority_buy", "watchlist"} else "high",
        "summary": summary,
        "key_strengths": strengths,
        "risk_flags": risks,
        "quantitative_facts": facts,
        "claims": claims,
        "model": MODEL,
        "effort": EFFORT,
        "web_search_performed": False,
        "web_search_verified": False,
        "freshness_status": "current_or_recent",
        "freshness_years": years or [2025, 2026],
        "freshness_penalty": 0.0,
        "freshness_note": "本地复核只使用 2026-08-24 代快照研究包；未把公告日期冒充经营期间。",
        "_candidate_type_keys": [
            _text(item.get("type_key"))
            for item in packet.get("candidate_types", [])
            if isinstance(item, Mapping) and _text(item.get("type_key"))
        ]
        or [type_key],
    }


def _knowledge_metadata(knowledge_path: Path) -> dict[str, Any]:
    """Record the exact contract document without claiming model injection.

    This reviewer is deliberately facts-only: it does not call a model with
    the knowledge text.  Reading the file here still binds the generated
    metadata to the contract document selected for the run and prevents a
    stale hard-coded digest from looking like provenance.
    """

    knowledge_bytes = knowledge_path.read_bytes()
    if not knowledge_bytes.strip():
        raise ValueError(f"knowledge document is empty: {knowledge_path}")
    return {
        "knowledge_contract": (
            "facts-only local review; knowledge loaded for contract only, not injected into model reasoning"
        ),
        "knowledge_loaded_for_contract": True,
        "knowledge_used_for_model_reasoning": False,
        "knowledge_sha256": hashlib.sha256(knowledge_bytes).hexdigest(),
    }


def build(
    candidates_path: Path,
    research_path: Path,
    output_path: Path,
    knowledge_path: Path = DEFAULT_KNOWLEDGE_PATH,
) -> dict[str, Any]:
    knowledge_metadata = _knowledge_metadata(knowledge_path)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    research_payload = json.loads(research_path.read_text(encoding="utf-8"))
    companies = research_payload.get("companies", research_payload)
    if not isinstance(companies, Mapping):
        raise ValueError("research companies are missing")
    packets = candidates.get("packets")
    if not isinstance(packets, list) or not packets:
        raise ValueError("candidate packets are missing")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("candidate packet is not an object")
        code = _text(packet.get("security_code"))
        key = (code, _text(packet.get("type_key")))
        if key in seen:
            raise ValueError(f"duplicate candidate identity: {key}")
        seen.add(key)
        company = companies.get(code)
        if not isinstance(company, Mapping):
            raise ValueError(f"missing research company: {code}")
        row = _review(packet, company)
        validation_errors = validate_review(row)
        if validation_errors:
            errors.append(f"{code}:{','.join(validation_errors)}")
        rows.append(row)
    if errors:
        raise ValueError("invalid local reviews: " + "; ".join(errors[:8]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    counts = {
        action: sum(row["ai_action"] == action for row in rows) for action in ("priority_buy", "watchlist", "avoid")
    }
    return {
        "candidate_count": len(rows),
        "type_pair_count": sum(len(row.get("_candidate_type_keys") or []) for row in rows),
        "model": MODEL,
        "effort": EFFORT,
        "web_search_performed": 0,
        **counts,
        **knowledge_metadata,
        "input_sha256": hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    args = parser.parse_args()
    print(
        json.dumps(
            build(args.candidates, args.research, args.out, args.knowledge),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
