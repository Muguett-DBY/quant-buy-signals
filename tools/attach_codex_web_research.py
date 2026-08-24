"""Attach real Codex web-search findings to the local review queue.

The local reviewer remains explicitly non-independent: this command does not
pretend that a web citation is an LLM judgement or a provider-native event. It
does, however, replace rule-only prose with concrete snapshot facts, searched
company evidence, and an explicit no-source warning.  Buy actions are never
upgraded by retrieval alone; a buy is demoted when the company-specific search
returned no usable source.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping


_RULE_RE = re.compile(
    r"\btype\s*[1-7]\b|类型\s*[1-7]|第[一二三四五六七1-7](?:种|类)(?:买入)?(?:情况|类型)|"
    r"确定性(?:筛选|规则|评分|分数|状态)|(?:筛选|买入|七类|模型)规则(?:分数|评分|状态|触发|达标|结果|候选池)|"
    r"(?:候选|规则|筛选|类型|type).{0,8}(?:触发|达标)|(?:触发|达标).{0,8}(?:候选|规则|筛选|类型|type)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_NEGATIVE_RE = re.compile(
    r"亏损|净利润下降|收入下降|现金流为负|经营现金流为负|处罚|诉讼|问询|减持|质押|商誉|产能过剩|风险", re.IGNORECASE
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"non-object row in {path}")
            rows.append(value)
    return rows


def _clean(text: Any, limit: int = 260) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = _URL_RE.sub("", value)
    value = value.replace("cite", "").replace("", "")
    return value[:limit]


def _finding_row(finding: Mapping[str, Any], query: str, index: int) -> dict[str, Any]:
    url = str(finding.get("url") or "").strip()
    return {
        "id": str(finding.get("id") or f"codex-search-{index:03d}"),
        "query": query,
        "title": _clean(finding.get("title"), 300),
        "url": url if url.startswith("https://") else None,
        "published_at": finding.get("published_at"),
        "report_period": finding.get("report_period"),
        "finding": _clean(finding.get("finding"), 600) or "已完成公司特定检索，但摘要没有可引用正文。",
        "stance": "neutral",
        "source_kind": str(finding.get("source_kind") or "secondary_web_source"),
    }


def _first_finding_text(findings: list[Mapping[str, Any]]) -> str:
    for finding in findings:
        text = _clean(finding.get("finding"), 220)
        if text:
            return text
    return ""


def _facts(review: Mapping[str, Any]) -> list[str]:
    values = review.get("quantitative_facts")
    if not isinstance(values, list):
        return []
    return [str(value).strip()[:240] for value in values if str(value).strip()][:4]


def _attach(review: Mapping[str, Any], retrieval: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(review)
    code = str(result.get("security_code") or retrieval.get("security_code") or "")
    query = str(retrieval.get("query") or "").strip()
    raw_findings = retrieval.get("findings") if isinstance(retrieval.get("findings"), list) else []
    findings = [
        _finding_row(item, query, index) for index, item in enumerate(raw_findings[:8], 1) if isinstance(item, Mapping)
    ]
    findings = [item for item in findings if item["finding"]]
    facts = _facts(result)
    action = str(result.get("ai_action") or "avoid")
    # A retrieval miss cannot support a high-confidence buy.  It is a
    # demotion, never an upgrade, and the score remains inside its action band.
    if action == "priority_buy" and not findings:
        action = "watchlist"
        result["ai_action"] = action
        result["final_category"] = "observe"
        result["final_recommendation"] = "do_not_recommend_buy"
        result["recommendation_label"] = "观察（暂不建议买）"
        result["recommended_action"] = "manual_review"
        result["verdict"] = "caution"
        result["buy_attractiveness_score"] = 69.0
        result["confidence"] = "low"
    else:
        result["buy_attractiveness_score"] = float(result.get("buy_attractiveness_score") or 0.0)

    web_fact = _first_finding_text(findings)
    if findings:
        summary_bits = ["联网资料摘要：" + web_fact]
        if len(findings) > 1:
            summary_bits.append(f"本次公司特定来源 {len(findings)} 条。")
    else:
        summary_bits = [
            "本次 Codex 搜索已执行，但没有找到可绑定的公司特定 HTTPS 来源；不把其他公司的页面当作本公司事实。"
        ]
    if facts:
        summary_bits.insert(0, "量化快照：" + "；".join(facts[:2]))
    label = str(
        result.get("recommendation_label")
        or ("建议买" if action == "priority_buy" else "观察" if action == "watchlist" else "不建议")
    )
    summary_bits.append("结论：" + label + "。")
    result["summary"] = "".join(summary_bits)[:1200]
    strengths = facts[:2]
    if web_fact:
        strengths.append("公司公开资料：" + web_fact)
    result["key_strengths"] = strengths[:6] or ["已完成公司代码定向联网检索。"]
    risks = ["网页检索结果不能替代财报原文和持续跟踪。"]
    if not findings:
        risks.insert(0, "未找到可绑定的公司特定 HTTPS 来源，当前结论保守处理。")
    elif _NEGATIVE_RE.search(web_fact):
        risks.insert(0, "联网摘要包含经营或治理风险词，需回到公告原文核对影响。")
    result["risk_flags"] = risks[:6]
    result["search_findings"] = findings
    web_claims = [
        {
            "statement": finding["finding"],
            "source_ref": finding["url"] or "",
            "source_refs": [finding["url"]] if finding["url"] else [],
            "source_context": finding["title"],
            "support": "supports",
            "search_finding_id": finding["id"],
            "source_kind": finding["source_kind"],
        }
        for finding in findings
        if finding.get("url")
    ][:8]
    prior_claims = result.get("claims") if isinstance(result.get("claims"), list) else []
    result["claims"] = [*prior_claims, *web_claims][:32]
    result["web_search_performed"] = True
    result["web_search_verified"] = False
    result["web_search_event_verified"] = False
    result["web_search_claim_urls_verified"] = False
    result["web_search_queries"] = [query] if query else []
    result["web_search_verified_claim_urls"] = []
    result["web_search_dropped_claim_url_count"] = 0
    result["freshness_note"] = "量化快照交易日为 2026-08-24；网页检索于 2026-08-25，网页发布日期与报告期分开记录。"
    result["ai_independent"] = False
    result["codex_web_tool"] = True
    result["provider_native_search"] = False
    result["provider_native_event_verified"] = False
    # Never let inherited local prose leak the deterministic rule labels.
    for field in ("summary", "key_strengths", "risk_flags"):
        values = result[field] if isinstance(result[field], list) else [result[field]]
        if any(_RULE_RE.search(str(value)) for value in values):
            raise ValueError(f"rule-language leak after web attachment: {code}/{field}")
    return result


def build(reviews_path: Path, retrieval_path: Path, output_path: Path) -> dict[str, int]:
    reviews = _read_jsonl(reviews_path)
    payload = json.loads(retrieval_path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != int(payload.get("candidate_total") or 0):
        raise ValueError("retrieval records are incomplete")
    by_code = {str(row.get("security_code") or ""): row for row in records if isinstance(row, Mapping)}
    if len(by_code) != len(records):
        raise ValueError("duplicate retrieval company")
    output: list[dict[str, Any]] = []
    for review in reviews:
        code = str(review.get("security_code") or "")
        retrieval = by_code.get(code)
        if retrieval is None:
            raise ValueError(f"review has no retrieval record: {code}")
        output.append(_attach(review, retrieval))
    if len(output) != len(by_code) or len({str(row.get("security_code")) for row in output}) != len(output):
        raise ValueError("review/retrieval coverage mismatch")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in output), encoding="utf-8"
    )
    return {
        "reviewed": len(output),
        "searched": sum(row.get("web_search_performed") is True for row in output),
        "with_sources": sum(bool(row.get("claims")) for row in output),
        "buy": sum(row.get("ai_action") == "priority_buy" for row in output),
        "observe": sum(row.get("ai_action") == "watchlist" for row in output),
        "avoid": sum(row.get("ai_action") == "avoid" for row in output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.reviews, args.retrieval, args.out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
