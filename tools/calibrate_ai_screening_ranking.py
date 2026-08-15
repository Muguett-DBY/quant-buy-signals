"""Turn the completed qualitative OpenCode review into a stable ranking artifact.

The first AI pass already contains a model-written summary, risks, and source
claims for every candidate.  This small calibration layer adds the numeric
ranking requested by the website without inventing new company facts.  The AI
opinion and the deterministic seven-type result are intentionally separate:
the latter adjusts confidence and score, but does not hard-block a strong AI
opinion on a near-threshold candidate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping
import re

from tools.ai_screening_contract import REVIEW_SCHEMA_VERSION, validate_review


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _deterministic_score(packet: Mapping[str, Any]) -> float:
    deterministic = packet.get("deterministic") if isinstance(packet.get("deterministic"), Mapping) else {}
    score = _number(deterministic.get("score"))
    if score is not None:
        return score
    upper = _number(deterministic.get("score_upper_bound"))
    return upper if upper is not None else 0.0


def _deterministic_status(packet: Mapping[str, Any]) -> str:
    deterministic = packet.get("deterministic") if isinstance(packet.get("deterministic"), Mapping) else {}
    return str(deterministic.get("status") or "insufficient_evidence")


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


def _claim_data_years(claim: Mapping[str, Any]) -> list[int]:
    """Extract report/data years from the claim text, not URL path or stock code."""
    statement = str(claim.get("statement") or "")
    years: set[int] = set()
    # Forecasts and management targets are useful risk context, but they are
    # not an actual report period.  Keep actual years from the same claim when
    # a sentence contains both (for example, "2025 annual ... 2026 target").
    forecast_markers = re.compile(
        r"预测|预期|预计|目标|规划|指引|未来|将|拟|展望|一致预期|forecast|guidance|target|expected",
        re.IGNORECASE,
    )
    for match in re.finditer(r"(?<!\d)(20(?:1[5-9]|2[0-9]))(?!\d)", statement):
        start = max(0, match.start() - 18)
        end = min(len(statement), match.end() + 18)
        context = statement[start:end]
        if forecast_markers.search(context):
            continue
        years.add(int(match.group(1)))
    return sorted(years)


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
    years = sorted({year for claim in claims if isinstance(claim, Mapping) for year in _claim_data_years(claim)})
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
        "note": f"主要事实只到 {latest} 年或更早，不能直接代表当前交易日状态",
    }


def _final_category(action: str) -> str:
    """Collapse the internal four-state action into the three user outcomes."""
    if action == "priority_buy":
        return "recommend_buy"
    if action in {"watchlist", "insufficient_evidence"}:
        return "observe"
    return "do_not_recommend"


def _calibrated_score(packet: Mapping[str, Any], verdict: str, market_as_of: str | None = None) -> float:
    base = _deterministic_score(packet)
    status = _deterministic_status(packet)
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
    status_penalty = {
        "triggered": 0.0,
        "conditional": 2.0,
        "observe": 4.0,
        "pending": 5.0,
        "insufficient_evidence": 6.0,
    }.get(status, 6.0)
    if model_score is not None:
        # The model score remains the ranking source.  Deterministic status
        # and provenance lower confidence in small, visible increments; they
        # do not turn a near-threshold AI buy opinion into an automatic avoid.
        score = model_score - source_penalty - status_penalty - float(freshness["penalty"])
        if str(source.get("ai_action") or "") == "avoid" or verdict == "misclassified":
            return round(max(0.0, min(49.0, score)), 1)
        return round(max(0.0, min(100.0, score)), 1)
    risk_flags = [str(value) for value in (source.get("risk_flags") or [])]
    risk_text = " ".join(risk_flags)
    penalty = min(
        16.0,
        len(risk_flags) * 1.5
        + sum(term in risk_text for term in ("现金流", "审计", "应收", "商誉", "诉讼", "周期")) * 2.0,
    )
    if verdict == "confirmed":
        score = max(70.0, min(99.0, 65.0 + base * 3.5 - penalty))
    elif verdict == "caution":
        score = max(50.0, min(76.0, 44.0 + base * 3.1 - penalty))
    elif verdict == "missed_candidate":
        score = max(50.0, min(69.0, 48.0 + base * 2.7 - penalty))
    elif verdict == "misclassified":
        score = max(8.0, 30.0 - base * 0.8 - penalty)
    else:
        score = max(20.0, min(58.0, 30.0 + base * 2.4 - penalty))

    return round(max(0.0, min(100.0, score - source_penalty - status_penalty - float(freshness["penalty"]))), 1)


def _review(packet: Mapping[str, Any], market_as_of: str | None = None) -> dict[str, Any]:
    source = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    verdict = str(source.get("verdict") or "needs_review")
    status = _deterministic_status(packet)
    freshness = _freshness(source, market_as_of)
    score = _calibrated_score(packet, verdict, market_as_of)
    web_verified = _web_search_verified(source)
    source_action = str(source.get("ai_action") or "")
    if source_action == "insufficient_evidence":
        # Unknown is not a negative investment conclusion.  Keep it in the
        # user-facing observe bucket even when the calibrated score is below
        # 50; otherwise the dashboard turns "资料不足" into "不建议".
        action = "watchlist"
    elif source_action == "watchlist":
        action = "watchlist"
    elif source_action == "avoid" or verdict == "misclassified":
        action = "avoid"
    elif (
        verdict == "confirmed"
        and source_action == "priority_buy"
        and score >= 60
        and freshness["status"] == "current_or_recent"
    ):
        action = "priority_buy"
    elif (
        verdict in {"confirmed", "caution", "missed_candidate"}
        and status
        in {
            "triggered",
            "observe",
            "conditional",
        }
        and score >= 50
    ):
        action = "watchlist"
    else:
        # Every candidate gets a final conservative decision. Missing web
        # provenance is not a third outcome that leaves the user without an
        # answer: attractive but unverified packets stay on the watchlist,
        # while weak or contradictory packets are marked avoid.
        action = "watchlist" if score >= 50 else "avoid"
    confidence = {"confirmed": "medium", "caution": "medium", "missed_candidate": "low"}.get(verdict, "low")
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
    strengths = [
        str(claim.get("statement") or "")[:240]
        for claim in claims
        if isinstance(claim, Mapping) and claim.get("support") == "supports"
    ][:4]
    risk_flags = [str(value)[:240] for value in (source.get("risk_flags") or []) if str(value).strip()][:8]
    if not risk_flags:
        risk_flags = ["当前排序沿用已完成的 AI 复核摘要，尚未对所有候选重新发起外部检索"]
    if freshness["status"] != "current_or_recent":
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
    # Make the provenance and deterministic state visible in every card.  A
    # missing HTTPS link lowers the score; it no longer suppresses an AI buy
    # opinion when the model itself has a confirmed view.
    source_quality = _source_quality(source)
    source_note = {
        "verified_https": "已完成联网搜索并找到 HTTPS 来源",
        "source_found": "已完成联网搜索并找到来源（未按 HTTPS 加分）",
        "searched_no_source": "已完成联网搜索但未找到可引用来源，分数已下调",
        "not_searched": "尚未完成联网搜索，分数已下调",
    }[source_quality]
    status_note = "确定性规则已触发" if status == "triggered" else f"确定性规则状态为 {status}，按接近达标口径扣分"
    summary = f"AI买入吸引力 {score:.1f} 分（{status_note}；{source_note}；{freshness['note']}）。{summary}"
    if source_quality not in {"verified_https", "source_found"} and source_note not in risk_flags:
        risk_flags.insert(0, source_note)
    public_verdict = verdict if verdict in {"confirmed", "caution", "misclassified", "missed_candidate"} else "caution"
    recommendation = "recommend_buy" if action == "priority_buy" else "do_not_recommend_buy"
    recommendation_label = (
        "建议买"
        if recommendation == "recommend_buy" and status == "triggered"
        else "建议买·接近达标"
        if recommendation == "recommend_buy"
        else "观察·需更新资料"
        if action == "watchlist" and freshness["status"] != "current_or_recent"
        else "观察"
        if action == "watchlist"
        else "不建议"
    )
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": str(packet.get("security_code") or ""),
        "type_key": str(packet.get("type_key") or ""),
        "verdict": public_verdict,
        "recommended_action": str(source.get("recommended_action") or "manual_review")
        if str(source.get("recommended_action") or "manual_review") in {"keep", "demote", "manual_review"}
        else "manual_review",
        "buy_attractiveness_score": score,
        "ai_action": action,
        "final_category": _final_category(action),
        "final_recommendation": recommendation,
        "recommendation_label": recommendation_label,
        "confidence": confidence,
        "summary": summary,
        "key_strengths": strengths,
        "risk_flags": risk_flags,
        "claims": claims[:12],
        "model": str(source.get("model") or "opencode-go/deepseek-v4-flash"),
        "effort": str(source.get("effort") or "max"),
        "web_search_performed": bool(source.get("web_search_performed") is True),
        "web_search_verified": web_verified,
        "freshness_status": freshness["status"],
        "freshness_years": freshness["years"],
        "freshness_penalty": freshness["penalty"],
        "freshness_note": freshness["note"],
    }


def calibrate(source_path: Path, output_path: Path) -> dict[str, int]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    packets = source.get("packets")
    if not isinstance(packets, list):
        raise ValueError("source packets are missing")
    output_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("packet is not an object")
        review = _review(packet, str(source.get("market_as_of") or ""))
        if validate_review(review):
            raise ValueError(f"calibrated review is invalid: {review['security_code']}/{review['type_key']}")
        output_packets.append({**packet, "ai_review": review})
    output = {
        **source,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "ranking_source": "opencode-go-web-search-review-calibrated-independent-buy-v7",
        "full_coverage_final_recommendation": True,
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
