"""Run a facts-first investment review for every AI-screening candidate.

This is deliberately separate from the deterministic seven-type engine and
from the score calibrator.  It does not treat a triggered type as a reason to
buy.  Each row is reviewed using the generation-bound snapshot, current
report facts, valuation, cash-flow direction, sector-specific constraints,
source claims and risk flags already present in the research packet.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


VERSION = "semantic-investment-review-v1"
_FINANCIAL_RE = re.compile(r"银行|证券|保险|信托|期货|金融|券商")
_CYCLE_RE = re.compile(r"有色|铝|钢铁|煤炭|化工|石化|建材|水泥|航运|汽车|能源|光伏|电力")
_MATERIAL_RISK_RE = re.compile(
    r"一次性|非经常|公允价值收益|资产处置收益|资产减值|商誉减值|主营规模仍在收缩|"
    r"收入复合增速\s*-|营业收入同比下降|归母净利润同比下降|自由现金流为\s*-|"
    r"经营活动现金流为\s*-|现金流同比下滑\s*-|PE\s*[3-9]\d|PE\s*2\d\.\d{2,}\s*倍.*偏高|"
    r"PB\s*[5-9]\d|PB\s*4\d\.\d{2,}\s*倍.*偏高"
 )
_NEGATIVE_FLOW_RE = re.compile(r"(?:经营活动现金流|自由现金流)(?:[^。；\n]{0,30})-\d")
_MISSING_SOURCE_RE = re.compile(r"未找到可绑定|没有找到可绑定|无法验证.*公司|缺少可直接核验")
_PE_RE = re.compile(r"PE[^0-9]{0,8}(\d+(?:\.\d+)?)")
_PB_RE = re.compile(r"PB[^0-9]{0,8}(\d+(?:\.\d+)?)")
_ROE_RE = re.compile(r"ROE[^0-9]{0,12}(\d+(?:\.\d+)?)%")
_CAPITAL_RE = re.compile(r"资本充足率[^0-9]{0,10}(\d+(?:\.\d+)?)%")
_NPL_RE = re.compile(r"不良贷款率[^0-9]{0,10}(\d+(?:\.\d+)?)%")

# The facts-first pass is intentionally conservative.  It is an audit ledger,
# not a second deterministic buy engine: a candidate already classified as
# observe/avoid is not promoted merely because a few ratios look attractive.
# Promotions are limited to independently reviewed, generation-bound overrides
# applied by ``apply_deep_review_ai_screening.py``.


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(review: Mapping[str, Any], packet: Mapping[str, Any]) -> str:
    values: list[str] = []
    context = packet.get("company_context")
    if isinstance(context, Mapping):
        values.extend(str(v) for v in context.get("quantitative_facts", []) if isinstance(v, str))
    for field in ("summary", "key_strengths", "risk_flags", "quantitative_facts"):
        raw = review.get(field)
        if isinstance(raw, list):
            values.extend(str(v) for v in raw if isinstance(v, str))
        elif isinstance(raw, str):
            values.append(raw)
    for claim in review.get("claims", []) if isinstance(review.get("claims"), list) else []:
        if isinstance(claim, Mapping):
            for field in ("statement", "source_context"):
                if isinstance(claim.get(field), str):
                    values.append(claim[field])
    return "；".join(values)


def _match_number(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    return _num(match.group(1)) if match else None


def _claims(review: Mapping[str, Any], code: str) -> dict[str, int | bool]:
    claims = review.get("claims") if isinstance(review.get("claims"), list) else []
    https = 0
    bound = 0
    direct = 0
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        ref = str(claim.get("source_ref") or claim.get("source_context") or "")
        if ref.lower().startswith("https://"):
            https += 1
            if code in ref:
                bound += 1
        kind = str(claim.get("source_kind") or "")
        if kind in {"company_filing", "financial_portal_filing", "exchange_or_regulator", "codex_web_search"}:
            direct += 1
    return {"claim_count": len(claims), "https_count": https, "code_bound_https_count": bound, "direct_source_count": direct, "has_company_source": bound > 0}


def _metrics(packet: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, float | None]:
    gate = review.get("quality_gate") if isinstance(review.get("quality_gate"), Mapping) else {}
    source = gate.get("metrics") if isinstance(gate.get("metrics"), Mapping) else {}
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    names = ("pe", "pb")
    result: dict[str, float | None] = {name: _num(source.get(name)) for name in names}
    for name in names:
        if result[name] is None:
            result[name] = _num(context.get(name))
    for name in (
        "roic",
        "fcf_margin",
        "annual_profit_growth",
        "interim_profit_growth",
        "interim_revenue_growth",
        "interim_ocf_growth",
        "interim_fcf_growth",
        "pe_median",
        "pb_median",
    ):
        result[name] = _num(source.get(name))
    return result


def _financial_review(text: str, metrics: Mapping[str, float | None], risks: list[str], evidence: Mapping[str, Any]) -> tuple[bool, list[str], float]:
    pe, pb = metrics.get("pe"), metrics.get("pb")
    profit, revenue = metrics.get("interim_profit_growth"), metrics.get("interim_revenue_growth")
    roe, capital, npl = _match_number(_ROE_RE, text), _match_number(_CAPITAL_RE, text), _match_number(_NPL_RE, text)
    reasons: list[str] = []
    if pe is None or pb is None:
        reasons.append("银行估值字段不完整")
    if pe is not None and pe > 8:
        reasons.append(f"PE {pe:.2f} 倍不够便宜")
    if pb is not None and pb > 1.0:
        reasons.append(f"PB {pb:.2f} 倍高于一倍净资产")
    if profit is None or profit < 8:
        reasons.append("最新归母净利润增速不足 8% 或缺失")
    if revenue is None or revenue < 0:
        reasons.append("最新营业收入没有保持增长")
    if roe is None or roe < 10:
        reasons.append("ROE 缺失或低于 10%")
    if capital is None or capital < 11:
        reasons.append("资本充足率缺失或低于 11%")
    if npl is None or npl > 1.5:
        reasons.append("不良贷款率缺失或高于 1.5%")
    if evidence.get("code_bound_https_count", 0) == 0:
        reasons.append("缺少绑定公司代码的 HTTPS 证据")
    if _MISSING_SOURCE_RE.search(" ".join(risks)):
        reasons.append("联网结果无法绑定公司来源")
    score = 50.0
    score += 10 if pe is not None and pe <= 6 else 5 if pe is not None and pe <= 8 else 0
    score += 10 if pb is not None and pb <= 0.8 else 5 if pb is not None and pb <= 1 else 0
    score += 10 if profit is not None and profit >= 12 else 5 if profit is not None and profit >= 8 else 0
    score += 10 if roe is not None and roe >= 12 else 5 if roe is not None and roe >= 10 else 0
    score += 10 if npl is not None and npl <= 1 else 0
    return not reasons, min(score, 100.0), reasons


def _industrial_review(packet: Mapping[str, Any], review: Mapping[str, Any], metrics: Mapping[str, float | None], risks: list[str], evidence: Mapping[str, Any]) -> tuple[bool, list[str], float]:
    pe, pb, roic, fcf = (metrics.get(k) for k in ("pe", "pb", "roic", "fcf_margin"))
    annual, profit, revenue = (metrics.get(k) for k in ("annual_profit_growth", "interim_profit_growth", "interim_revenue_growth"))
    reasons: list[str] = []
    if review.get("quality_gate", {}).get("hard_block") is True:
        reasons.extend(str(v) for v in review.get("quality_gate", {}).get("reasons", [])[:3])
    if pe is None or pe <= 0:
        reasons.append("PE 缺失或盈利为负")
    elif pe > 25:
        reasons.append(f"PE {pe:.2f} 倍过高")
    if pb is None or pb <= 0:
        reasons.append("PB 缺失")
    elif pb > 3.5 and not (roic is not None and roic >= 25 and fcf is not None and fcf >= 20):
        reasons.append(f"PB {pb:.2f} 倍偏高，缺少足够资本回报补偿")
    if roic is None or roic < 12:
        reasons.append("ROIC 缺失或低于 12%")
    if fcf is None or fcf < 10:
        reasons.append("自由现金流率缺失或低于 10%")
    if annual is None or annual < 5:
        reasons.append("2025 年利润增长不足 5% 或缺失")
    if profit is None or profit < 10:
        reasons.append("最新中期利润增长不足 10% 或缺失")
    if revenue is None or revenue < 0:
        reasons.append("最新中期收入没有保持增长")
    flow_values = [metrics.get(k) for k in ("interim_ocf_growth", "interim_fcf_growth")]
    if any(v is not None and v < 0 for v in flow_values):
        reasons.append("最新中期现金流同比转弱")
    risk_text = " ".join(risks)
    if _MATERIAL_RISK_RE.search(risk_text) or _NEGATIVE_FLOW_RE.search(risk_text):
        reasons.append("风险字段含一次性、衰退或现金流反转信号")
    if evidence.get("code_bound_https_count", 0) == 0:
        reasons.append("缺少绑定公司代码的 HTTPS 证据")
    score = 45.0
    score += 12 if pe is not None and pe <= 15 else 8 if pe is not None and pe <= 20 else 0
    score += 10 if pb is not None and pb <= 1.5 else 6 if pb is not None and pb <= 2.5 else 0
    score += 12 if roic is not None and roic >= 20 else 8 if roic is not None and roic >= 15 else 4 if roic is not None and roic >= 12 else 0
    score += 10 if fcf is not None and fcf >= 20 else 6 if fcf is not None and fcf >= 10 else 0
    score += 8 if profit is not None and profit >= 20 else 4 if profit is not None and profit >= 10 else 0
    score += 5 if revenue is not None and revenue >= 10 else 0
    if _CYCLE_RE.search(str((packet.get("company_context") or {}).get("industry") or "")):
        score -= 4
        reasons.append("强周期/商品行业，需用正常化利润和周期位置复核")
    if _MISSING_SOURCE_RE.search(risk_text):
        score -= 8
    return not reasons, max(0.0, min(score, 100.0)), list(dict.fromkeys(reasons))


def review_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    review = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    code = str(packet.get("security_code") or "")
    name = str(packet.get("name") or "")
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    industry = str(context.get("industry") or "")
    metrics = _metrics(packet, review)
    risks = [str(v) for v in review.get("risk_flags", []) if isinstance(v, str)]
    evidence = _claims(review, code)
    text = _text(review, packet)
    if _FINANCIAL_RE.search(industry):
        eligible, score, reasons = _financial_review(text, metrics, risks, evidence)
        review_class = "financial"
    else:
        eligible, score, reasons = _industrial_review(packet, review, metrics, risks, evidence)
        review_class = "industrial_or_cyclical"
    current = str(review.get("final_category") or "")
    # Preserve the calibrated production conclusion unless this is an existing
    # buy that fails the independent facts check.  This prevents the audit
    # itself from silently turning a ratio screen into a new buy signal.
    if current == "recommend_buy":
        if eligible:
            conclusion, action = "recommend_buy", "priority_buy"
        elif score >= 55:
            conclusion, action = "observe", "watchlist"
        else:
            conclusion, action = "do_not_recommend", "avoid"
    elif current == "observe":
        conclusion, action = "observe", "watchlist"
    else:
        conclusion, action = "do_not_recommend", "avoid"
    status = "confirmed" if current == "recommend_buy" and conclusion == "recommend_buy" else "kept"
    if current == "recommend_buy" and conclusion != "recommend_buy":
        status = "demote_review"
    return {
        "security_code": code,
        "name": name,
        "industry": industry,
        "review_class": review_class,
        "current_category": current,
        "current_action": review.get("ai_action"),
        "current_score": review.get("buy_attractiveness_score"),
        "deep_review_score": round(score, 1),
        "deep_review_conclusion": conclusion,
        "deep_review_action": action,
        "eligible_buy": eligible,
        "review_status": status,
        "reasons": reasons,
        "metrics": metrics,
        "evidence": evidence,
        "risk_flags": risks,
        "quality_hard_block": bool((review.get("quality_gate") or {}).get("hard_block")),
    }


def build_report(data: Mapping[str, Any], kb_root: Path | None = None) -> dict[str, Any]:
    packets = data.get("packets") if isinstance(data.get("packets"), list) else []
    rows = [review_packet(packet) for packet in packets if isinstance(packet, Mapping)]
    counts = Counter(row["deep_review_conclusion"] for row in rows)
    promotions = [row for row in rows if row["deep_review_conclusion"] == "recommend_buy" and row["current_category"] != "recommend_buy"]
    demotions = [row for row in rows if row["deep_review_conclusion"] != "recommend_buy" and row["current_category"] == "recommend_buy"]
    return {
        "schema_version": 1,
        "review_version": VERSION,
        "snapshot_generation": data.get("snapshot_generation"),
        "market_as_of": data.get("market_as_of"),
        "candidate_total": len(rows),
        "reviewed_count": len(rows),
        "full_coverage": len(rows) == int(data.get("candidate_total") or len(rows)) and int(data.get("candidate_offset") or 0) == 0,
        "knowledge_base_root": str(kb_root) if kb_root else None,
        "counts": dict(counts),
        "promotion_count": len(promotions),
        "demotion_count": len(demotions),
        "promotions": sorted(promotions, key=lambda row: (-row["deep_review_score"], row["security_code"])),
        "demotions": demotions,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kb-root", type=Path)
    args = parser.parse_args()
    with args.input.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    report = build_report(data, args.kb_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in ("candidate_total", "reviewed_count", "counts", "promotion_count", "demotion_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
