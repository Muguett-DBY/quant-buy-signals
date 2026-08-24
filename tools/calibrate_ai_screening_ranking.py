"""Turn the completed qualitative OpenCode review into a stable ranking artifact.

The first AI pass already contains a model-written summary, risks, and source
claims for every candidate.  This small calibration layer adds the numeric
ranking requested by the website without inventing new company facts.  The AI
opinion and the deterministic seven-type result are intentionally separate:
the latter remains visible as candidate context, but never changes the AI
score or blocks an AI action.  Only source provenance and report freshness are
used as evidence-quality adjustments.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_contract import (
    LOCAL_REVIEW_MODEL,
    LOCAL_OPENCODE_MODELS,
    REVIEW_SCHEMA_VERSION,
    candidate_identity_sha256,
    decision_text_conflicts,
    native_company_research_profile_matches,
    normalise_decision_text,
    validate_review,
)
from tools.ai_quantitative_facts import has_numeric_fact


def _action_safe_summary(summary: str, action: str) -> str:
    """Prevent a downgraded card from retaining a current buy sentence."""
    summary = normalise_decision_text(summary)
    if action == "priority_buy":
        # A model may append a cautious phrase such as “优先观察标的” to an
        # otherwise affirmative conclusion.  Keep the risk caveat, but make
        # the displayed action unambiguous for the three-way UI.
        summary = summary.replace("优先观察标的", "优先候选标的").replace("观察标的", "候选标的")
    if action == "watchlist":
        summary = summary.replace("当前结论：不建议买", "当前结论：观察（暂不建议买）")
    if action == "priority_buy" or not decision_text_conflicts(action, summary):
        return summary
    for old, new in (
        ("当前建议买入", "当前不建议买入"),
        ("建议立即买入", "暂不建议立即买入"),
        ("建议买入", "不建议买入"),
        ("建议买", "不建议买"),
        ("值得买入", "暂不建议买入"),
        ("值得买", "暂不建议买"),
        ("可以买", "暂不构成买点"),
        ("可买", "暂不可买"),
    ):
        summary = summary.replace(old, new)
    summary = normalise_decision_text(summary)
    if decision_text_conflicts(action, summary):
        marker = summary.find("）。")
        prefix = summary[: marker + 2] if marker >= 0 else ""
        lead = (
            "当前结论：建议买。"
            if action == "priority_buy"
            else "当前结论：不建议。"
            if action == "avoid"
            else "当前结论：观察。"
        )
        return (
            prefix
            + lead
            + (
                "联网核验已完成，但现有证据不足以支持当前买入结论。"
                if action != "priority_buy"
                else "联网核验与候选逻辑一致，仍需持续跟踪反证。"
            )
        )
    return summary


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _web_search_verified(review: Mapping[str, Any]) -> bool:
    if review.get("web_search_performed") is not True:
        return False
    claims = review.get("claims") if isinstance(review.get("claims"), list) else []
    return any(_claim_url(claim).lower().startswith("https://") for claim in claims if isinstance(claim, Mapping))


def _source_quality(review: Mapping[str, Any]) -> str:
    """Classify provenance without turning transport into a verdict gate."""
    claims = review.get("claims") if isinstance(review.get("claims"), list) else []
    urls = [_claim_url(claim) for claim in claims if isinstance(claim, Mapping)]
    if any(url.lower().startswith("https://") for url in urls):
        return "verified_https"
    if any(url for url in urls):
        return "source_found"
    if review.get("web_search_performed") is True:
        return "searched_no_source"
    return "not_searched"


def _claim_url(claim: Mapping[str, Any]) -> str:
    for field in ("source_ref", "source_context"):
        raw = str(claim.get(field) or "")
        match = re.search(r"https?://[^\s)]+", raw, re.IGNORECASE)
        if match:
            ascii_url = re.match(r"[A-Za-z0-9:/?#\[\]@!$&'()*+,;=%._~\-]+", match.group(0))
            return (ascii_url.group(0) if ascii_url else "").rstrip(".,;")
    return ""


_FINANCIAL_FACT_DIMENSIONS = frozenset(
    {
        "balance_sheet",
        "capital_reinvestment",
        "capital_return",
        "cash_flow",
        "cashflow",
        "dividend",
        "earnings",
        "financial_forensics",
        "income",
        "income_statement",
        "operating",
        "operations",
        "profitability",
        "quality",
        "reinvestment",
        "returns",
        "revenue",
        "shareholder_returns",
    }
)
_MARKET_FACT_DIMENSIONS = frozenset(
    {
        "market",
        "market_data",
        "market_snapshot",
        "price",
        "technical",
        "trading",
        "valuation",
    }
)
_FINANCIAL_STATEMENT_MARKERS = re.compile(
    r"营业收入|营收|营业利润|归母净利润|扣非|净利润|盈利|亏损|毛利(?:率)?|净利率|"
    r"经营现金流|自由现金流|现金流|资产负债|负债率|总资产|净资产|应收|存货|商誉|"
    r"资本开支|资本支出|在建工程|投入资本|ROE|ROIC|分红|股利|派息|股息支付|"
    r"产量|销量|出货量|订单|年报|半年报|中报|季报|财报|报告期|财务|经营|运营|"
    r"revenue|profit|earnings|cash\s*flow|free\s*cash|balance\s*sheet|return\s+on\s+equity|"
    r"return\s+on\s+invested\s+capital|dividend|capex|annual\s+report|interim\s+report",
    re.IGNORECASE,
)
_MARKET_STATEMENT_MARKERS = re.compile(
    r"股价|收盘价|开盘价|成交价|交易价|市盈率|市净率|市销率|市值|估值|"
    r"\bPE\b|\bPB\b|\bPS\b|EV\s*/\s*EBITDA|share\s+price|market\s+cap|valuation",
    re.IGNORECASE,
)
_REPORT_PERIOD_MARKERS = re.compile(
    r"年度|年报|半年报|中报|一季报|三季报|季报|财报|报告期|"
    r"annual\s+report|interim\s+report|quarter(?:ly)?\s+report",
    re.IGNORECASE,
)
_FORECAST_MARKERS = re.compile(
    r"预测|预期|预计|目标|计划|规划|指引|未来|将|拟|展望|一致预期|"
    r"forecast|guidance|target|expected",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(20(?:1[5-9]|2[0-9]))(?!\d)")


def _fact_dimension_kind(claim: Mapping[str, Any]) -> str:
    raw_values = [
        claim.get(field)
        for field in ("dimension", "fact_dimension", "data_dimension", "metric_dimension")
        if claim.get(field) not in (None, "")
    ]
    if not raw_values:
        return "unknown"
    dimensions = {
        token
        for raw in raw_values
        for token in re.split(r"[,/|;\s]+", str(raw).strip().casefold().replace("-", "_"))
        if token
    }
    financial = bool(dimensions & _FINANCIAL_FACT_DIMENSIONS) or any(
        re.search(r"财务|盈利|利润|现金流|资产负债|资本回报|经营|运营", token)
        for token in dimensions
    )
    market = bool(dimensions & _MARKET_FACT_DIMENSIONS) or any(
        re.search(r"估值|股价|市值|交易", token) for token in dimensions
    )
    if financial and not market:
        return "financial"
    if market and not financial:
        return "market"
    return "unknown"


def _period_years(claim: Mapping[str, Any]) -> list[int]:
    years: set[int] = set()
    for field in ("period", "report_period", "fact_period", "statement_period"):
        value = claim.get(field)
        if value in (None, ""):
            continue
        years.update(int(match.group(1)) for match in _YEAR_RE.finditer(str(value)))
    return sorted(years)


def _publication_date_year(statement: str, match: re.Match[str]) -> bool:
    """Return true for a filing/publication date rather than its report period."""
    prefix = statement[max(0, match.start() - 8) : match.start()]
    suffix = statement[match.end() : min(len(statement), match.end() + 24)]
    calendar_date = re.match(r"(?:[-/.年]\d{1,2}){1,2}(?:日)?", suffix)
    return bool(
        calendar_date
        and (
            re.search(r"(?:公司)?于$|公告日期|披露日期|发布日期|发布于$", prefix)
            or re.search(r"披露|发布|公告", suffix)
        )
    )


def _statement_financial_years(statement: str) -> list[int]:
    """Infer actual financial periods while rejecting trading-date valuation facts."""
    years: set[int] = set()
    for match in _YEAR_RE.finditer(statement):
        start = max(0, match.start() - 24)
        end = min(len(statement), match.end() + 28)
        context = statement[start:end]
        after_year = statement[match.end() : min(len(statement), match.end() + 16)]
        explicit_report_period = bool(_REPORT_PERIOD_MARKERS.search(after_year))
        if (
            (_FORECAST_MARKERS.search(context) and not explicit_report_period)
            or _publication_date_year(statement, match)
        ):
            continue
        financial_matches = list(_FINANCIAL_STATEMENT_MARKERS.finditer(context))
        if not financial_matches and not explicit_report_period:
            continue
        market_matches = list(_MARKET_STATEMENT_MARKERS.finditer(context))
        if market_matches and not explicit_report_period:
            year_offset = match.start() - start
            nearest_financial = min(
                (abs((item.start() + item.end()) / 2 - year_offset) for item in financial_matches),
                default=float("inf"),
            )
            nearest_market = min(
                abs((item.start() + item.end()) / 2 - year_offset) for item in market_matches
            )
            if nearest_market <= nearest_financial:
                continue
        years.add(int(match.group(1)))
    return sorted(years)


def _claim_data_years(claim: Mapping[str, Any]) -> list[int]:
    """Extract actual financial report years, never market-snapshot years."""
    dimension_kind = _fact_dimension_kind(claim)
    if dimension_kind == "market":
        return []
    explicit_period_years = _period_years(claim)
    if dimension_kind == "financial" and explicit_period_years:
        return explicit_period_years
    statement = str(claim.get("statement") or "")
    inferred_years = _statement_financial_years(statement)
    if dimension_kind == "financial":
        return inferred_years
    # Legacy public claims have no structured dimension.  Their explicit
    # period is accepted only when the statement itself identifies a financial
    # or operating fact; an unexplained date remains conservatively undated.
    if inferred_years and explicit_period_years:
        return sorted(set(inferred_years) | set(explicit_period_years))
    return inferred_years


def _freshness(review: Mapping[str, Any], market_as_of: str | None) -> dict[str, Any]:
    """Describe whether the AI's cited facts cover the current review date.

    A 2024 annual report can remain useful historical context, but it must not
    silently look like current evidence for a 2026 snapshot.  This is a
    presentation/ranking signal, not a fabricated replacement for a new filing.
    """
    as_of_year_match = re.match(r"^(20\d{2})-\d{2}-\d{2}$", str(market_as_of or ""))
    if not as_of_year_match:
        return {
            "status": "current_or_recent",
            "years": [],
            "penalty": 0.0,
            "note": "未指定快照日期，保留原始排序",
        }
    current_year = int(as_of_year_match.group(1))
    recent_floor = current_year - 1
    claims = review.get("claims") if isinstance(review.get("claims"), list) else []
    # A future year can only be a forecast/target in a snapshot that has
    # already been published.  Keep it out of the public "actual report
    # period" field even when the surrounding Chinese text did not contain a
    # forecast marker that the lightweight parser recognised.
    years = sorted(
        {
            year
            for claim in claims
            if isinstance(claim, Mapping)
            for year in _claim_data_years(claim)
            if year <= current_year
        }
    )
    if not years:
        return {
            "status": "undated",
            "years": [],
            "penalty": 5.0,
            "note": f"未能确认覆盖 {current_year - 1}—{current_year} 年的实际报告期",
        }
    if max(years) >= recent_floor:
        latest = max(years)
        if latest == current_year:
            note = f"最新可识别实际报告期为 {latest} 年；更早年份仅作历史背景，仍应以最新正式报告为准"
        else:
            note = (
                f"最新可识别实际报告期为 {latest} 年，尚未确认 {current_year} 年实际报告期；"
                "更早年份仅作历史背景，仍应以最新正式报告为准"
            )
        return {
            "status": "current_or_recent",
            "years": years,
            "penalty": 0.0,
            "note": note,
        }
    latest = max(years)
    return {
        "status": "historical",
        "years": years,
        "penalty": 8.0,
        "note": f"最新可识别实际报告期为 {latest} 年，不能用当前交易日估值替代财报时效",
    }


def _final_category(action: str) -> str:
    """Collapse the internal four-state action into the three user outcomes."""
    if action == "priority_buy":
        return "recommend_buy"
    if action in {"watchlist", "insufficient_evidence"}:
        return "observe"
    return "do_not_recommend"


def _calibrated_score(packet: Mapping[str, Any], verdict: str, market_as_of: str | None = None) -> float:
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    source_quality = _source_quality(source)
    freshness = _freshness(source, market_as_of)
    model_score = _number(source.get("buy_attractiveness_score"))
    source_penalty = {
        "verified_https": 0.0,
        "source_found": 2.0,
        "searched_no_source": 5.0,
        "not_searched": 8.0,
    }[source_quality]
    if model_score is not None:
        # The model score remains the ranking source.  Candidate status is
        # deliberately absent here: a triggered rule may be downgraded by AI,
        # and a conditional/near-threshold candidate may be upgraded by AI.
        score = model_score - source_penalty - float(freshness["penalty"])
        if str(source.get("ai_action") or "") in {"avoid", "insufficient_evidence"} or verdict == "misclassified":
            return round(max(0.0, min(49.0, score)), 1)
        return round(max(0.0, min(100.0, score)), 1)
    risk_flags = [str(value) for value in (source.get("risk_flags") or [])]
    risk_text = " ".join(risk_flags)
    penalty = min(
        16.0,
        len(risk_flags) * 1.5
        + sum(term in risk_text for term in ("现金流", "审计", "应收", "商誉", "诉讼", "周期")) * 2.0,
    )
    # Reviews without an explicit model score still receive a stable
    # verdict-based fallback, but deterministic rule scores are not allowed
    # to manufacture or inflate an AI score.
    if verdict == "confirmed":
        score = max(50.0, min(99.0, 65.0 - penalty))
    elif verdict == "caution":
        score = max(40.0, min(76.0, 55.0 - penalty))
    elif verdict == "missed_candidate":
        score = max(40.0, min(69.0, 52.0 - penalty))
    elif verdict == "misclassified":
        score = max(8.0, 30.0 - penalty)
    else:
        score = max(20.0, min(58.0, 30.0 - penalty))

    return round(max(0.0, min(100.0, score - source_penalty - float(freshness["penalty"]))), 1)


def _calibration_adjustments(
    packet: Mapping[str, Any],
    *,
    verdict: str,
    action: str,
    final_score: float,
    market_as_of: str | None,
) -> dict[str, Any]:
    """Expose every deterministic adjustment applied after the model score."""

    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    source_quality = _source_quality(source)
    freshness = _freshness(source, market_as_of)
    raw_score = _number(source.get("buy_attractiveness_score"))
    source_penalty = {
        "verified_https": 0.0,
        "source_found": 2.0,
        "searched_no_source": 5.0,
        "not_searched": 8.0,
    }[source_quality]
    if raw_score is None:
        # Legacy fallback scores are not model scores.  Keep the value visible
        # as a fallback baseline without pretending it has model components.
        raw_score = None
        pre_band_score = None
    else:
        pre_band_score = round(raw_score - source_penalty - float(freshness["penalty"]), 1)
    if action == "priority_buy":
        band_min, band_max = 70.0, 100.0
    elif action == "watchlist":
        band_min, band_max = 50.0, 69.0
    else:
        band_min, band_max = 0.0, 49.0
    unclamped = pre_band_score if pre_band_score is not None else float(final_score)
    return {
        "raw_score": raw_score,
        "source_penalty": float(source_penalty),
        "freshness_penalty": float(freshness["penalty"]),
        "pre_band_score": pre_band_score if pre_band_score is not None else round(unclamped, 1),
        "action_band_min": band_min,
        "action_band_max": band_max,
        "final_score": round(float(final_score), 1),
        "source_quality": source_quality,
        "freshness_status": freshness["status"],
        "band_clamped": abs(float(final_score) - unclamped) > 0.05,
        "verdict": verdict,
    }


def _review(packet: Mapping[str, Any], market_as_of: str | None = None) -> dict[str, Any]:
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    native_company_research = native_company_research_profile_matches(source)
    verdict = str(source.get("verdict") or "needs_review")
    freshness = _freshness(source, market_as_of)
    score = _calibrated_score(packet, verdict, market_as_of)
    web_verified = _web_search_verified(source)
    source_action = str(source.get("ai_action") or "")
    if source_action == "avoid" or verdict == "misclassified":
        action = "avoid"
    elif (
        verdict == "confirmed"
        and source_action == "priority_buy"
        and score >= 70
        and (
            freshness["status"] == "current_or_recent"
            # Local OpenCode Go reviews are explicitly advisory and may be
            # unsearched; the snapshot facts still support an independent
            # buy opinion after the visible provenance/freshness penalty.
            # A native web-search review is different: if its claims do not
            # identify a current report period, keep it observable rather
            # than presenting an undated external source as a buy signal.
            or (
                str(source.get("model") or "") in LOCAL_OPENCODE_MODELS
                and not native_company_research
            )
        )
    ):
        action = "priority_buy"
    elif verdict in {"confirmed", "caution", "missed_candidate"} and score >= 50:
        action = "watchlist"
    else:
        # Every candidate gets a final conservative decision. Missing web
        # provenance is not a third outcome that leaves the user without an
        # answer: attractive but unverified packets stay on the watchlist,
        # while weak or contradictory packets are marked avoid.
        action = "watchlist" if score >= 50 else "avoid"
    # The displayed number and conclusion must speak the same language.  The
    # model's raw score remains the starting point, while the final action owns
    # the public score band.
    if action == "watchlist":
        score = max(50.0, min(score, 69.0))
    elif action in {"avoid", "insufficient_evidence"}:
        score = min(score, 49.0)
    calibration_adjustments = _calibration_adjustments(
        packet,
        verdict=verdict,
        action=action,
        final_score=score,
        market_as_of=market_as_of,
    )
    source_confidence = str(source.get("confidence") or "")
    confidence = (
        source_confidence
        if source_confidence in {"high", "medium", "low"}
        else {"confirmed": "medium", "caution": "medium", "missed_candidate": "low"}.get(
            verdict, "low"
        )
    )
    if _source_quality(source) not in {"verified_https", "source_found"}:
        confidence = "low"
    raw_claims = source.get("claims") if isinstance(source.get("claims"), list) else []
    # Legacy public cards may contain empty placeholder claims.  Do not copy
    # those into the new contract: a claim without a source is not evidence.
    claims = []
    for claim in raw_claims:
        if not isinstance(claim, Mapping):
            continue
        source_ref = _claim_url(claim)
        if not source_ref:
            continue
        claims.append({**claim, "source_ref": source_ref})
    claim_strengths = [
        str(claim.get("statement") or "")[:240]
        for claim in claims
        if isinstance(claim, Mapping) and claim.get("support") == "supports"
    ]
    model_strengths = [
        str(value)[:240] for value in (source.get("key_strengths") or []) if isinstance(value, str) and value.strip()
    ]
    raw_quantitative = source.get("quantitative_facts") if isinstance(source.get("quantitative_facts"), list) else []
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    context_quantitative = (
        context.get("quantitative_facts") if isinstance(context.get("quantitative_facts"), list) else []
    )
    quantitative_facts = list(
        dict.fromkeys(
            str(value)[:240]
            for value in [*raw_quantitative, *context_quantitative]
            if isinstance(value, str)
            and value.strip()
            and has_numeric_fact(value)
            and not re.search(
                r"\btype\s*[1-7]\b|确定性|触发|(?:筛选|买入|七类|模型)规则|"
                r"规则(?:分数|评分|得分|状态|已触发|未触发|达标)|接近达标",
                value,
                re.IGNORECASE,
            )
        )
    )[:8]
    # Quantitative facts are a separate company-facts field.  Keep model prose
    # and sourced claim statements in AI strengths so the two kinds of reasons
    # cannot be mistaken for one another.
    strengths = (
        model_strengths
        if native_company_research
        else list(dict.fromkeys([*model_strengths, *claim_strengths]))[:8]
    )
    risk_flags = [str(value)[:240] for value in (source.get("risk_flags") or []) if str(value).strip()][:8]
    if not risk_flags and not native_company_research:
        risk_flags = ["当前排序沿用已完成的 AI 复核摘要，尚未对所有候选重新发起外部检索"]
    if freshness["status"] != "current_or_recent" and not native_company_research:
        risk_flags.insert(0, f"资料时效：{freshness['note']}")
    # Rebuild the prefix from the current provenance state.  This also strips
    # the prefix emitted by an older calibration run, so replaying a legacy
    # seed cannot produce nested/double "AI买入吸引力" labels.
    summary = str(source.get("summary") or "AI 已完成第一轮候选复核。")
    legacy_prefixes = (
        "AI买入吸引力 ",
        "AI 买入吸引力 ",
    )
    while summary.startswith(legacy_prefixes):
        marker = summary.find("）。")
        if marker < 0:
            break
        summary = summary[marker + 2 :].lstrip()
    summary = summary[:1200]
    # Make evidence provenance visible in every card.  Candidate status is
    # intentionally absent from this prefix: it is rule context, not an AI
    # reason, and it does not affect the calibrated score.
    source_quality = _source_quality(source)
    local_codex_review = (
        str(source.get("model") or "") == LOCAL_REVIEW_MODEL
        and source.get("web_search_performed") is not True
    )
    source_note = (
        "本地全量复核（未逐家公司联网；事实来源绑定到当代研究包）"
        if local_codex_review
        else "已完成原生搜索，公司财务事实来源已绑定并通过来源核验"
        if source.get("research_source_urls_verified") is True
        else {
            "verified_https": "已完成联网搜索并找到 HTTPS 来源",
            "source_found": "已完成联网搜索并找到来源（未按 HTTPS 加分）",
            "searched_no_source": "已完成联网搜索但未找到可引用来源，分数已下调",
            "not_searched": "尚未完成联网搜索，分数已下调",
        }[source_quality]
    )
    independent_company_research = native_company_research and source.get("ai_independent") is True
    if not independent_company_research:
        summary = f"AI买入吸引力 {score:.1f} 分（{source_note}；{freshness['note']}）。{summary}"
    summary = _action_safe_summary(summary, action)
    if source_quality not in {"verified_https", "source_found"} and source_note not in risk_flags:
        risk_flags.insert(0, source_note)
    public_verdict = verdict if verdict in {"confirmed", "caution", "misclassified", "missed_candidate"} else "caution"
    recommendation = "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy"
    recommendation_label = (
        "建议买"
        if recommendation == "recommend_buy"
        else "观察·需更新资料"
        if action == "watchlist" and freshness["status"] != "current_or_recent"
        else "观察"
        if action == "watchlist"
        else "不建议"
    )
    recommended_action = "keep" if action == "priority_buy" else "demote" if action == "avoid" else "manual_review"
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": str(packet.get("security_code") or ""),
        "type_key": str(packet.get("type_key") or ""),
        "verdict": public_verdict,
        "recommended_action": recommended_action,
        "buy_attractiveness_score": score,
        **(
            {"economic_category": source.get("economic_category")}
            if "economic_category" in source
            else {}
        ),
        **(
            {"score_components": source.get("score_components")}
            if "score_components" in source
            else {}
        ),
        "calibration_adjustments": calibration_adjustments,
        "ai_action": action,
        "final_category": _final_category(action),
        "final_recommendation": recommendation,
        "recommendation_label": recommendation_label,
        "ai_independent": bool(source.get("ai_independent", True))
        and not (
            source_action == "priority_buy"
            and action != "priority_buy"
            and freshness["status"] != "current_or_recent"
        ),
        "confidence": confidence,
        "summary": summary,
        "key_strengths": strengths,
        **({"quantitative_facts": quantitative_facts} if quantitative_facts else {}),
        "risk_flags": risk_flags,
        "claims": (
            claims
            if native_company_research
            else claims[:12]
        ),
        "model": str(source.get("model") or "unknown-external-review"),
        "effort": str(source.get("effort") or "max"),
        "retrieval_backend": str(source.get("retrieval_backend") or ""),
        "retrieval_model": str(source.get("retrieval_model") or ""),
        "retrieval_effort": str(source.get("retrieval_effort") or ""),
        "native_search_completed": source.get("native_search_completed") is True,
        "official_fetch_completed": source.get("official_fetch_completed") is True,
        "web_search_performed": bool(source.get("web_search_performed") is True),
        "web_search_event_verified": bool(source.get("web_search_event_verified") is True),
        "web_search_claim_urls_verified": bool(source.get("web_search_claim_urls_verified") is True),
        "research_source_urls_verified": bool(source.get("research_source_urls_verified") is True),
        "web_search_queries": [
            str(value)[:240]
            for value in (source.get("web_search_queries") or [])
            if isinstance(value, str) and value.strip()
        ][:16],
        "web_search_verified_claim_urls": [
            str(value)[:800]
            for value in (source.get("web_search_verified_claim_urls") or [])
            if isinstance(value, str) and value.strip()
        ][:16],
        "web_search_dropped_claim_url_count": int(source.get("web_search_dropped_claim_url_count", 0) or 0),
        "web_search_verified": web_verified,
        "freshness_status": freshness["status"],
        "freshness_years": freshness["years"],
        "freshness_penalty": freshness["penalty"],
        "freshness_note": freshness["note"],
        **(
            {
                "research_as_of": source.get("research_as_of"),
                "economic_profile": source.get("economic_profile"),
                "valuation": source.get("valuation"),
                **(
                    {"valuation_snapshot": source.get("valuation_snapshot")}
                    if "valuation_snapshot" in source
                    else {}
                ),
                "search_findings": source.get("search_findings"),
                "evidence_bindings": source.get("evidence_bindings"),
            }
            if all(field in source for field in ("research_as_of", "economic_profile", "valuation"))
            else {}
        ),
    }


def calibrate(source_path: Path, output_path: Path) -> dict[str, int]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("source packets are missing")
    candidate_offset = int(source.get("candidate_offset", 0) or 0)
    candidate_count = int(source.get("candidate_count", len(packets)) or 0)
    candidate_total = int(source.get("candidate_total", len(packets)) or 0)
    identities = [
        (str(packet.get("security_code") or ""), str(packet.get("type_key") or ""))
        for packet in packets
        if isinstance(packet, Mapping)
    ]
    identity_digest = candidate_identity_sha256(packet for packet in packets if isinstance(packet, Mapping))
    declared_identity_digest = str(source.get("candidate_identity_sha256") or "")
    universe_identity_digest = str(source.get("candidate_universe_identity_sha256") or "")
    if declared_identity_digest and declared_identity_digest != identity_digest:
        raise ValueError("candidate identity hash does not match the reviewed packet queue")
    requested_full_coverage = source.get("full_coverage_final_recommendation") is True
    complete_queue = (
        candidate_offset == 0
        and candidate_count == candidate_total == len(packets)
        and len(identities) == len(packets)
        and len(set(identities)) == len(identities)
        and declared_identity_digest == identity_digest == universe_identity_digest
        and all(isinstance(packet.get("ai_review"), Mapping) for packet in packets if isinstance(packet, Mapping))
    )
    if requested_full_coverage and not complete_queue:
        raise ValueError("full-coverage calibration requires the complete unique reviewed candidate queue")

    review_mode = str(source.get("review_mode") or "")
    company_research_review = review_mode == "opencode_native_company_research_review"
    output_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("packet is not an object")
        review = _review(packet, str(source.get("market_as_of") or ""))
        if validate_review(
            review,
            require_company_research_fields=company_research_review,
        ):
            raise ValueError(f"calibrated review is invalid: {review['security_code']}/{review['type_key']}")
        output_packets.append({**packet, "ai_review": review})
    review_models = {
        str(packet.get("ai_review", {}).get("model") or "")
        for packet in output_packets
        if isinstance(packet.get("ai_review"), Mapping)
    }
    if review_mode == "local_codex_review":
        if review_models == {"opencode-go/ox-alpha-free"}:
            ranking_source = "opencode-zen-ox-alpha-free-max-local-review-v1"
        elif review_models == {"opencode-go/muse-spark-1.2-contributor"}:
            ranking_source = "opencode-go-muse-spark-1.2-xhigh-local-review-v1"
        elif review_models <= LOCAL_OPENCODE_MODELS:
            ranking_source = "opencode-go-local-max-review-v1"
        else:
            ranking_source = "local-codex-review-v1"
    else:
        ranking_source = "opencode-web-search-review-calibrated-independent-buy-v8"
    output = {
        **source,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "ranking_source": ranking_source,
        "full_coverage_final_recommendation": requested_full_coverage and complete_queue,
        "packets": output_packets,
    }
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"candidate_count": len(output_packets)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(calibrate(args.source, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
