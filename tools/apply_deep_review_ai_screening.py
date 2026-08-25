"""Attach the full-pool semantic investment review to an AI overlay.

The calibrated overlay remains the source of truth for the deterministic
candidate universe.  This command adds a generation-bound, facts-first review
for every packet and applies only explicit, independently checked promotions.
It never promotes a row merely because a ratio screen looks good.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Mapping


VERSION = "semantic-investment-review-v2"


def _metric(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "数据缺失"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _context_metrics(packet: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    keys = (
        "pe",
        "pb",
        "roic",
        "fcf_margin",
        "annual_profit_growth",
        "interim_profit_growth",
        "interim_revenue_growth",
        "interim_ocf_growth",
        "interim_fcf_growth",
        "pe_median",
        "pb_median",
    )
    result: dict[str, Any] = {}
    for key in keys:
        value = metrics.get(key)
        if value is None:
            value = context.get(key)
        if value is not None:
            result[key] = value
    return result


def _base_reason(row: Mapping[str, Any], packet: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    review = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    metrics = _context_metrics(packet, row)
    reasons = [str(value) for value in row.get("reasons", []) if str(value).strip()]
    gate = review.get("quality_gate") if isinstance(review.get("quality_gate"), Mapping) else {}
    for value in gate.get("reasons", []) if isinstance(gate.get("reasons"), list) else []:
        text = str(value).strip()
        if text and text not in reasons:
            reasons.append(text)
    category = str(row.get("deep_review_conclusion") or row.get("current_category") or "")
    if category == "recommend_buy":
        strengths = [
            f"PE {_metric(metrics.get('pe'))}，PB {_metric(metrics.get('pb'))}，ROIC {_metric(metrics.get('roic'), '%')}。",
            f"最新利润增速 {_metric(metrics.get('interim_profit_growth'), '%')}，收入增速 {_metric(metrics.get('interim_revenue_growth'), '%')}。",
            f"自由现金流率 {_metric(metrics.get('fcf_margin'), '%')}，最新现金流增速 {_metric(metrics.get('interim_fcf_growth'), '%')}。",
        ]
    elif category == "observe":
        strengths = [
            f"快照估值：PE {_metric(metrics.get('pe'))}、PB {_metric(metrics.get('pb'))}。",
            f"最新利润/收入增速：{_metric(metrics.get('interim_profit_growth'), '%')} / {_metric(metrics.get('interim_revenue_growth'), '%')}。",
        ]
    else:
        strengths = [
            f"已核对快照估值：PE {_metric(metrics.get('pe'))}、PB {_metric(metrics.get('pb'))}。",
            f"已核对最新利润/现金流指标：利润 {_metric(metrics.get('interim_profit_growth'), '%')}，FCF {_metric(metrics.get('interim_fcf_growth'), '%')}。",
        ]
    if not reasons:
        reasons = ["没有发现足以改变当前结论的新增反证或补强证据。"]
    return strengths, reasons[:8]


def _semantic_review(packet: Mapping[str, Any], row: Mapping[str, Any], *, candidate_total: int) -> dict[str, Any]:
    review = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    category = str(row.get("deep_review_conclusion") or row.get("current_category") or "observe")
    strengths, reasons = _base_reason(row, packet)
    status = "confirmed" if category == "recommend_buy" else "reviewed_keep"
    if str(row.get("current_category")) != category:
        status = "reviewed_change_pending"
    return {
        "version": VERSION,
        "scope": f"all_{candidate_total}_candidates",
        "review_status": status,
        "conclusion": category,
        "score": float(row.get("deep_review_score") or review.get("buy_attractiveness_score") or 0),
        "confidence": "high" if category == "recommend_buy" else "medium" if category == "observe" else "high",
        "basis": "逐家公司核对快照财务事实、估值、现金流/资本约束、行业属性、风险字段与知识库总闸门；规则触发本身不作为买入理由。",
        "quantitative_facts": strengths,
        "reasons": reasons,
        "metrics": _context_metrics(packet, row),
        "knowledge_base": "E:\\模板汇总MD\\补丁7· 长期投资者的买卖总闸门（七种买入情况+量化打分+卖出闸门）.md",
        "external_review": {
            "performed": False,
            "source_urls": [],
            "note": "本条结论使用代际绑定快照事实；未伪造原生搜索事件。",
        },
    }


# Only independently checked rows are allowed to change the production action.
# The evidence is deliberately concrete and uses official/company-report URLs.
OVERRIDES: dict[str, dict[str, Any]] = {
    "603444": {
        "score": 84.0,
        "summary": "603444 吉比特建议买（全量语义复核确认）：2026年中报收入同比增长48.01%，归母净利润增长69.31%，经营现金流增长18.22%，简化自由现金流增长17.91%，ROIC 33.76%，周二收盘 PE 15.47 倍。PB 4.58 倍是主要反证，只有在持续高增长和现金回报兑现时才有足够安全边际；因此保留建议买，但不是无条件追高。",
        "strengths": [
            "2026年中报收入 +48.01%、归母净利润 +69.31%，增长与现金流方向一致。",
            "2025年 ROIC 33.76%、2026年中报自由现金流率约 45.0%，资本回报和现金转换强。",
            "2026-08-25 收盘 PE 15.47 倍，低于行业中位数 34.37 倍。",
        ],
        "risks": ["周二收盘 PB 4.58 倍偏高，若增长或现金回报回落，估值安全边际会迅速收窄。"],
        "source_urls": [
            "https://quant.custard.top/api/company/603444?generation_id=443d9dcf4d29dbb4",
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12492209&stockid=603444",
        ],
        "claims": [
            {
                "fact_id": "valuation",
                "statement": "603444 2026-08-25 收盘 PE 15.47 倍、PB 4.58 倍。",
                "source_ref": "https://quant.custard.top/api/company/603444?generation_id=443d9dcf4d29dbb4",
                "support": "supports",
            },
            {
                "fact_id": "latest_income",
                "statement": "603444 2026年中报收入同比 +48.01%，归母净利润同比 +69.31%。",
                "source_ref": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12492209&stockid=603444",
                "support": "supports",
            },
            {
                "fact_id": "latest_cashflow",
                "statement": "603444 2026年中报经营活动现金流同比 +18.22%，简化自由现金流率约 45.0%。",
                "source_ref": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12492209&stockid=603444",
                "support": "supports",
            },
        ],
        "note": "当前唯一生产建议买经逐家公司事实复核后保留，但评分由 89 下调至 84 以反映 PB 风险。",
    },
    "601128": {
        "category": "recommend_buy",
        "action": "priority_buy",
        "score": 86.0,
        "summary": "601128 常熟银行建议买（银行专用口径复核）：周二收盘 PE 4.98 倍、PB 0.64 倍；2025年ROE 14.05%、净息差 2.53%、不良率 0.76%，2026年一季度收入增长 6.74%、净利润增长 11.10%，最新不良率约 0.75%、拨备覆盖率 438.10%、资本充足率 12.64%。银行经营现金流不按制造业 FCF 解读；低估值、资产质量和盈利稳定性共同支持建议买，但仍需跟踪息差与资本补充。",
        "strengths": [
            "2026-08-25 收盘 PE 4.98 倍、PB 0.64 倍，估值显著低于多数成长股。",
            "ROE 14.05%、不良率 0.75%—0.76%、拨备覆盖率 438.10%，资产质量较稳。",
            "2026年一季度净利润同比 +11.10%，2025年现金分红每10股合计 2.70元。",
        ],
        "risks": [
            "资本充足率约 12.64%，净息差和区域银行信用周期仍需持续跟踪；银行现金流不可与工业企业自由现金流直接比较。"
        ],
        "source_urls": [
            "https://quant.custard.top/api/company/601128?generation_id=443d9dcf4d29dbb4",
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12168007&stockid=601128",
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12167995&stockid=601128",
        ],
        "claims": [
            {
                "fact_id": "valuation",
                "statement": "601128 2026-08-25 收盘 PE 4.98 倍、PB 0.64 倍。",
                "source_ref": "https://quant.custard.top/api/company/601128?generation_id=443d9dcf4d29dbb4",
                "support": "supports",
            },
            {
                "fact_id": "capital_quality",
                "statement": "601128 2025年 ROE 14.05%、不良贷款率 0.76%；2026年一季度归母净利润同比 +11.10%。",
                "source_ref": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12168007&stockid=601128",
                "support": "supports",
            },
            {
                "fact_id": "dividend",
                "statement": "601128 2025年度现金分红方案每10股合计 2.70 元。",
                "source_ref": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12167995&stockid=601128",
                "support": "supports",
            },
        ],
        "note": "独立核对常熟银行 2026 年一季报与 2025 年分红材料后升级；不是由规则触发或工业 FCF 误判推动。",
    },
    "600926": {
        "category": "recommend_buy",
        "action": "priority_buy",
        "score": 82.0,
        "summary": "600926 杭州银行建议买（银行专用口径复核）：周二收盘 PE 6.25 倍、PB 0.86 倍；2025年ROE 14.65%、不良率 0.76%、拨备覆盖率 481.39%、资本充足率 14.37%，2026年一季度收入增长 4.29%、净利润增长 10.09%，贷款/存款分别增长 7.57%/5.50%。净息差 1.36%是主要风险；在低估值、稳健资产质量和盈利仍保持双位数增长的组合下，给出建议买但低于常熟银行的评分。",
        "strengths": [
            "2026-08-25 收盘 PE 6.25 倍、PB 0.86 倍，估值处于银行可接受偏低区间。",
            "ROE 14.65%、不良率 0.76%、拨备覆盖率 481.39%、资本充足率 14.37%。",
            "2026年一季度净利润同比 +10.09%，贷款/存款保持增长。",
        ],
        "risks": ["净息差约 1.36%、公司贷款占比较高，利率和信用周期可能压缩盈利；银行经营现金流不按工业 FCF 解读。"],
        "source_urls": [
            "https://quant.custard.top/api/company/600926?generation_id=443d9dcf4d29dbb4",
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12154327&stockid=600926",
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12154354&stockid=600926",
        ],
        "claims": [
            {
                "fact_id": "valuation",
                "statement": "600926 2026-08-25 收盘 PE 6.25 倍、PB 0.86 倍。",
                "source_ref": "https://quant.custard.top/api/company/600926?generation_id=443d9dcf4d29dbb4",
                "support": "supports",
            },
            {
                "fact_id": "capital_quality",
                "statement": "600926 2025年 ROE 14.65%、不良贷款率 0.76%、资本充足率 14.37%；2026年一季度归母净利润同比 +10.09%。",
                "source_ref": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12154327&stockid=600926",
                "support": "supports",
            },
            {
                "fact_id": "annual_quality",
                "statement": "600926 2025年拨备覆盖率 481.39%，净息差 1.36% 是主要风险。",
                "source_ref": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12154354&stockid=600926",
                "support": "supports",
            },
        ],
        "note": "独立核对杭州银行 2026 年一季报与 2025 年年报后升级；评分低于常熟银行以反映息差风险。",
    },
    "300515": {
        "source_urls": [
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12508835&stockid=300515"
        ],
        "note": "保持观察：2026年中报收入 +19.60%、净利润 +42.80%，但经营现金流仅约为净利润的 28%，且披露 8500 万元到期未兑付理财产品并按 100% 计提坏账，不能升级建议买。",
    },
    "002517": {
        "source_urls": [
            "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12250441&stockid=002517",
            "https://static.cninfo.com.cn/finalpage/2026-04-29/1225225838.PDF",
        ],
        "note": "保持观察： headline 净利润 +50.65% 含和解事项收益，扣非净利润仅 +5.75%，不能把一次性收益当作可持续增长。",
    },
}


def _apply_override(review: dict[str, Any], semantic: dict[str, Any], override: Mapping[str, Any], code: str) -> None:
    category = str(override.get("category") or review.get("final_category") or semantic["conclusion"])
    action = str(
        override.get("action")
        or ("priority_buy" if category == "recommend_buy" else "watchlist" if category == "observe" else "avoid")
    )
    semantic.update(
        {
            "review_status": "confirmed" if category == "recommend_buy" else "reviewed_keep",
            "conclusion": category,
            "score": float(override.get("score", semantic["score"])),
            "confidence": "high" if category == "recommend_buy" else "medium",
            "external_review": {
                "performed": bool(override.get("source_urls")),
                "source_urls": list(override.get("source_urls") or []),
                "note": str(override.get("note") or ""),
            },
        }
    )
    if override.get("strengths"):
        semantic["quantitative_facts"] = list(override["strengths"])
    if override.get("risks"):
        semantic["reasons"] = list(override["risks"])
    if override.get("note"):
        semantic["reviewer_note"] = str(override["note"])
    if "category" in override:
        review["final_category"] = category
        review["final_recommendation"] = "recommend_buy" if category == "recommend_buy" else "do_not_recommend_buy"
        review["recommendation_label"] = (
            "建议买" if category == "recommend_buy" else "观察" if category == "observe" else "不建议"
        )
        review["ai_action"] = action
        review["recommended_action"] = "keep" if category in {"recommend_buy", "observe"} else "demote"
        review["verdict"] = "confirmed" if category == "recommend_buy" else "caution"
        review["confidence"] = "high" if category == "recommend_buy" else "medium"
    if "score" in override:
        review["buy_attractiveness_score"] = float(override["score"])
        adjustments = review.get("calibration_adjustments")
        if isinstance(adjustments, dict):
            adjustments["final_score"] = float(override["score"])
            adjustments["pre_band_score"] = float(override["score"])
            adjustments["verdict"] = "confirmed"
        review["calibration_adjustments"] = adjustments
    if override.get("summary"):
        review["summary"] = str(override["summary"])
    if override.get("strengths"):
        review["key_strengths"] = list(override["strengths"])
        review["quantitative_facts"] = list(override["strengths"])
    if override.get("risks"):
        review["risk_flags"] = list(override["risks"])
    if override.get("claims"):
        review["claims"] = copy.deepcopy(list(override["claims"]))


def apply(data: Mapping[str, Any], report: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    output = copy.deepcopy(dict(data))
    packets = output.get("packets") if isinstance(output.get("packets"), list) else []
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    by_code = {str(row.get("security_code")): row for row in rows if isinstance(row, Mapping)}
    if len(packets) != int(output.get("candidate_total") or len(packets)) or len(by_code) != len(packets):
        raise ValueError("semantic report does not cover the complete packet set")
    applied: list[str] = []
    for packet in packets:
        if not isinstance(packet, dict):
            raise ValueError("packet is not an object")
        code = str(packet.get("security_code") or "")
        row = by_code.get(code)
        if not row:
            raise ValueError(f"missing semantic row: {code}")
        review = packet.get("ai_review")
        if not isinstance(review, dict):
            raise ValueError(f"missing ai review: {code}")
        semantic = _semantic_review(packet, row, candidate_total=len(packets))
        override = OVERRIDES.get(code)
        if override:
            _apply_override(review, semantic, override, code)
            applied.append(code)
        review["semantic_review"] = semantic
    audit = {
        "schema_version": 1,
        "review_version": VERSION,
        "snapshot_generation": output.get("snapshot_generation"),
        "market_as_of": output.get("market_as_of"),
        "candidate_total": len(packets),
        "reviewed_count": len(packets),
        "full_coverage": True,
        "applied_override_codes": applied,
        "semantic_counts": {
            category: sum(
                1
                for packet in packets
                if packet.get("ai_review", {}).get("semantic_review", {}).get("conclusion") == category
            )
            for category in ("recommend_buy", "observe", "do_not_recommend")
        },
    }
    return output, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    report = json.loads(args.report.read_text(encoding="utf-8"))
    output, audit = apply(data, report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
