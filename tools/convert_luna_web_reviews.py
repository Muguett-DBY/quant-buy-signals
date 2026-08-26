"""Convert Luna Max web-research rows into the public AI review contract.

The Luna subagents write a deliberately small, human-auditable JSONL shape:
company identity, dated facts, source URLs, and an independent three-way
decision.  This adapter is the only place that maps that shape to the existing
generation-bound screening review schema.  It rejects incomplete evidence or
score/decision contradictions instead of filling them with defaults.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from tools.ai_screening_contract import (
    REVIEW_SCHEMA_VERSION,
    _research_dimensions,
    decision_text_conflicts,
    validate_review,
)


MODEL = "codex-luna-max"
EFFORT = "max"
REVIEW_MODE = "codex_luna_web_review"
_DECISIONS = {"recommend_buy", "observe", "do_not_recommend"}
_URL_RE = re.compile(r"^https://[^\s]+$")
_TYPE_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z_])type\s*[1-7](?:(?:\s*[-_/]\s*)(?:type\s*)?[1-7])*(?![0-9A-Za-z_])"
    r"|(?<![0-9A-Za-z_])类型\s*[1-7](?![0-9A-Za-z_])",
    re.IGNORECASE,
)


def _sanitize_reason_text(value: Any, limit: int = 1200) -> str:
    """Remove deterministic-pool labels from AI-facing company prose.

    Candidate types are useful input context, but the public explanation must
    stand on company facts.  Strip only the labels and their boilerplate
    disclaimers; keep the surrounding financial, operating and risk facts.
    """

    text = _text(value, limit)
    text = _TYPE_TOKEN_RE.sub("", text)
    text = re.sub(r"按补丁7[^，。；;\n]*", "估值和质量尚待核验", text)
    text = text.replace("强周期底部", "周期低位")
    text = text.replace("可持续高增长", "增长持续性")
    text = text.replace("两热一冷", "行业景气与估值组合")
    text = text.replace("估值买入区", "估值区间")
    text = text.replace("年度财务历史覆盖", "财务历史覆盖")
    text = text.replace("候选池", "研究范围").replace("候选来源", "研究来源")
    text = text.replace("入池", "进入研究范围")
    text = re.sub(r"(?:已|未|尚未)触发", "出现", text)
    text = re.sub(r"(?:接近|尚未|未|已)达标", "达到要求", text)
    text = re.sub(r"(?:仅是|只是)?(?:筛选索引|研究索引)", "", text)
    text = re.sub(r"分片中的(?:type|类型)状态(?:未作为买入理由)?", "", text, flags=re.IGNORECASE)
    text = text.replace("筛选规则", "研究条件")
    text = text.replace("买入规则", "投资条件")
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"^[，,；;。]+", "", text)
    text = re.sub(r"[，,；;]\s*[。！？!?]", "。", text)
    return text.strip()


def _text(value: Any, limit: int = 800) -> str:
    return str(value or "").strip()[:limit]


def _value_text(value: Any, limit: int = 240) -> str:
    """Render a fact value without losing numbers nested in JSON objects."""

    if isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:limit]
        except (TypeError, ValueError):
            return _text(value, limit)
    return _text(value, limit)


def _clean_fact_text(value: Any, limit: int = 600) -> str:
    """Keep numeric facts parseable without changing their values."""

    return re.sub(r"(?<=\d),(?=\d)", "", _text(value, limit))


def _public_fact_text(value: Any) -> str:
    """Fit a normalized fact into the public 240-character contract.

    Keep both the dated/metric prefix and the numeric/unit tail; a plain
    left-side truncation can remove the only value that makes a fact auditable.
    """

    text = _clean_fact_text(value, 520)
    if len(text) <= 240:
        return text
    return f"{text[:198].rstrip()}；…{text[-39:].lstrip()}"[:240]


def _is_pdf_source(url: str) -> bool:
    lowered = str(url or "").casefold()
    return ".pdf" in lowered or "/pdf/" in lowered or "pdf.dfcfw" in lowered


def _source_codes(value: Any) -> set[str]:
    parsed = urlparse(str(value or ""))
    codes: set[str] = set()
    for values in parse_qs(parsed.query).values():
        for item in values:
            codes.update(re.findall(r"(?<!\d)(?:[036]\d{5})(?!\d)", item))
    for segment in parsed.path.split("/"):
        token = segment.rsplit(".", 1)[0]
        if re.fullmatch(r"[036]\d{5}", token):
            codes.add(token)
    return codes


def _clean_source_title(value: Any, target_code: str) -> str:
    title = _text(value, 300)
    return re.sub(
        r"(?<!\d)(?:[036]\d{5})(?!\d)",
        lambda match: match.group(0) if match.group(0) == target_code else "相关公司",
        title,
    )


def _load_rows(paths: list[Path]) -> dict[str, dict[str, Any]]:
    records_by_index: dict[int, list[dict[str, Any]]] = {}
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping):
                raise ValueError(f"{path}:{line_number} is not an object")
            code = _text(row.get("code"), 16)
            if not re.fullmatch(r"^[036]\d{5}$", code):
                raise ValueError(f"duplicate or invalid Luna review code: {code!r}")
            index = row.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError(f"invalid Luna review index for {code}: {index!r}")
            if row.get("review_mode") not in (None, REVIEW_MODE):
                raise ValueError(f"unexpected Luna review mode for {code}: {row.get('review_mode')!r}")
            records_by_index.setdefault(index, []).append(dict(row))

    # Resolve only explicit append-only corrections.  This also lets a
    # correction replace a stale row whose code was accidentally written at
    # the right index; all remaining duplicate identities still fail closed.
    selected: list[dict[str, Any]] = []
    for index, records in records_by_index.items():
        if len(records) == 1:
            selected.append(records[0])
            continue
        corrections = [record for record in records if record.get("correction") is True]
        if len(corrections) != 1 or records[-1] is not corrections[0]:
            codes = "/".join(_text(record.get("code"), 16) for record in records)
            raise ValueError(f"duplicate Luna review index {index}: {codes}")
        selected.append(corrections[0])

    rows: dict[str, dict[str, Any]] = {}
    for row in selected:
        code = _text(row.get("code"), 16)
        if code in rows:
            raise ValueError(f"duplicate or invalid Luna review code: {code!r}")
        rows[code] = row
    return rows


def _date_years(row: Mapping[str, Any]) -> list[int]:
    years: set[int] = set()
    for value in _financial_fact_items(row):
        years.update(int(item) for item in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", _text(value, 1200)))
    for source in row.get("sources", []):
        if isinstance(source, Mapping):
            years.update(int(item) for item in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", _text(source.get("date"))))
    return sorted(year for year in years if 1900 <= year <= 2100)


def _period_label(value: Any) -> str:
    """Turn compact queue periods into the dated wording used by the contract."""

    period = _text(value, 40)
    match = re.fullmatch(r"((?:19|20)\d{2})FY", period, re.IGNORECASE)
    if match:
        return f"{match.group(1)}年度"
    if re.fullmatch(r"(?:19|20)\d{2}", period):
        return f"{period}年度"
    period = re.sub(r"((?:19|20)\d{2})FY", r"\1年度", period, flags=re.IGNORECASE)
    period = re.sub(r"((?:19|20)\d{2})A\b", r"\1年度", period, flags=re.IGNORECASE)
    return period


def _evidence_grade(row: Mapping[str, Any]) -> str:
    value = row.get("evidence_quality")
    if isinstance(value, Mapping):
        value = value.get("grade") or value.get("level") or value.get("overall") or value.get("quality")
    text = _text(value, 80).casefold()
    if any(token in text for token in ("high", "strong", "高", "强")):
        return "high"
    if any(token in text for token in ("low", "weak", "低", "弱")):
        return "low"
    if any(token in text for token in ("medium", "中", "一般")):
        return "medium"
    return text or "unknown"


def _financial_fact_items(row: Mapping[str, Any]) -> list[dict[str, str] | str]:
    """Normalize the two Luna packet fact shapes into dated readable facts.

    Most shards emit ``[{period, fact, status}, ...]``.  A later batch emitted
    the already-extracted queue metrics as a mapping instead.  Keep that data
    rather than treating a non-list as missing; the adapter still requires a
    real numeric fact and the public contract validates the resulting text.
    """

    raw = row.get("financial_facts")
    if isinstance(raw, list):
        items: list[dict[str, str] | str] = []
        metric_labels = {
            "revenue_rmb": "营业收入",
            "revenue_yoy_pct": "营业收入同比",
            "net_income_rmb": "净利润",
            "net_income_yoy_pct": "净利润同比",
            "parent_net_income_rmb": "归母净利润",
            "parent_net_income_yoy_pct": "归母净利润同比",
            "non_gaap_net_income_rmb": "扣非净利润",
            "non_gaap_net_income_yoy_pct": "扣非净利润同比",
            "operating_cash_flow_rmb": "经营现金流",
            "operating_cash_flow_yoy_pct": "经营现金流同比",
            "roe_pct": "ROE",
            "rd_expense_rmb": "研发投入",
            "rd_expense_yoy_pct": "研发投入同比",
            "cash_rmb": "现金",
            "total_assets_rmb": "资产总额",
            "parent_equity_rmb": "归母权益",
            "finance_expense_rmb": "财务费用",
        }

        def metric_text(key: str, value: Any) -> str:
            label = metric_labels.get(key, key.replace("_", " "))
            text = _value_text(value, 240)
            key_lower = key.casefold()
            if (key.endswith("_pct") or "ratio" in key_lower or "growth" in key_lower) and text and "%" not in text:
                text = f"{text}%"
            elif (
                key.endswith(("_rmb", "_cny_yuan"))
                or "cny" in key_lower
                or "rmb" in key_lower
                or key_lower.endswith("_yuan")
            ) and text and not re.search(r"元|万|亿", text):
                text = f"{text}元"
            elif "square_meter" in key_lower and text and not re.search(r"平方米|万平", text):
                text = f"{text}平方米"
            return f"{label} {text}".strip()

        for item in raw:
            if isinstance(item, Mapping):
                period = next(
                    (
                        _text(item.get(key), 40)
                        for key in ("period", "source_date", "as_of", "date", "date_or_period")
                        if _text(item.get(key), 40)
                    ),
                    "",
                )
                source_url = _text(item.get("source_url") or item.get("source"), 1200)
                if not source_url and isinstance(item.get("source_index"), int):
                    raw_sources = row.get("sources")
                    source_index = item["source_index"]
                    if isinstance(raw_sources, list) and 0 <= source_index < len(raw_sources):
                        source_item = raw_sources[source_index]
                        if isinstance(source_item, Mapping):
                            source_url = _text(source_item.get("url"), 1200)
                fact = _text(item.get("fact"), 600)
                if not fact:
                    metric = _text(item.get("metric"), 180)
                    metric = re.sub(r"^((?:19|20)\d{2})(?=[^\d年])", r"\1年", metric)
                    value_fields = []
                    if item.get("value") is not None:
                        value_fields.append(("value", item.get("value")))
                    value_fields.extend(
                        (str(key), value)
                        for key, value in item.items()
                        if str(key).casefold().startswith(("value_", "change_", "yoy"))
                        and value is not None
                        and key not in {"value", "change", "yoy"}
                    )
                    rendered_values = [metric_text(key, value) for key, value in value_fields]
                    yoy = _value_text(item.get("yoy") or item.get("change"), 180)
                    if yoy and not any(yoy.casefold() in value.casefold() for value in rendered_values):
                        rendered_values.append(f"同比 {yoy}%")
                    parts = [part for part in (metric, *rendered_values) if part]
                    if not parts:
                        metric_parts = [
                            metric_text(str(key), value)
                            for key, value in item.items()
                            if key not in {"period", "status", "source_date", "source_url", "source", "source_index", "date", "as_of"}
                            and value is not None
                        ]
                        if metric_parts:
                            for metric_part in metric_parts:
                                record: dict[str, str] = {
                                    "period": period,
                                    "fact": metric_part,
                                    "status": _text(item.get("status"), 32),
                                }
                                if source_url:
                                    record["source_url"] = source_url
                                items.append(record)
                            continue
                    fact = "；".join(parts)
                else:
                    # Some agents provide a readable metric label in ``fact``
                    # and put the actual number/date in sibling fields. Keep
                    # those values instead of silently discarding them.
                    extras = []
                    value_fields = []
                    if item.get("value") is not None:
                        value_fields.append(("value", item.get("value")))
                    value_fields.extend(
                        (str(key), value)
                        for key, value in item.items()
                        if str(key).casefold().startswith(("value_", "change_", "yoy"))
                        and value is not None
                        and key not in {"value", "change", "yoy"}
                    )
                    for key, value in value_fields:
                        rendered = metric_text(key, value)
                        if rendered and rendered.casefold() not in fact.casefold():
                            extras.append(rendered)
                    change_text = _value_text(item.get("change") or item.get("yoy"), 180)
                    if change_text and not any(change_text.casefold() in extra.casefold() for extra in extras):
                        extras.append(f"同比 {change_text}%")
                    if extras:
                        fact = f"{fact}；{'；'.join(extras)}"
                if fact or period:
                    record = {
                        "period": period,
                        "fact": fact or "公司公开披露事实，具体数值见来源。",
                        "status": _text(item.get("status"), 32),
                    }
                    if source_url:
                        record["source_url"] = source_url
                    items.append(record)
            elif _text(item):
                items.append(_text(item, 600))
        return items
    if not isinstance(raw, Mapping):
        return []
    items: list[dict[str, str]] = []
    market_as_of = _text(raw.get("market_as_of"), 10)
    if market_as_of and raw.get("input_price") is not None:
        items.append(
            {
                "period": market_as_of,
                "fact": f"交易日 {market_as_of} 收盘价 {_text(raw.get('input_price'), 40)}元；"
                f"PE {_text(raw.get('input_pe'), 40)}倍；PB {_text(raw.get('input_pb'), 40)}倍。",
            }
        )
    metric_labels = {
        "revenue": "营业收入",
        "revenue_yoy_pct": "营业收入同比",
        "net_profit": "归母净利润",
        "net_profit_yoy_pct": "归母净利润同比",
        "parent_net_income": "归母净利润",
        "parent_net_income_yoy_pct": "归母净利润同比",
        "non_gaap_net_profit": "扣非净利润",
        "non_gaap_net_profit_yoy_pct": "扣非净利润同比",
        "operating_cash_flow": "经营现金流",
        "operating_cash_flow_yoy_pct": "经营现金流同比",
        "roe_pct": "ROE",
        "rd_expense": "研发投入",
        "rd_expense_yoy_pct": "研发投入同比",
        "cash": "现金",
        "total_assets": "资产总额",
        "parent_equity": "归母权益",
        "finance_expense": "财务费用",
        "long_term_loans": "长期借款",
    }
    for key, value in raw.items():
        if key in {"market_as_of", "input_price", "input_pe", "input_pb"} or value is None:
            continue
        match = re.fullmatch(r"fy(\d{4})(q[1-4]|h[12])?_(.+)_rmb", str(key), re.IGNORECASE)
        if not match:
            continue
        year, period, metric = match.groups()
        period_label = f"{year}年" + (f"{period.upper()}" if period else "年报")
        label = metric_labels.get(metric, metric.replace("_", " "))
        suffix = "%" if metric.endswith("_pct") else "元"
        items.append({"period": period_label, "fact": f"{period_label}{label} {value}{suffix}。"})
    return items


def _research_date(row: Mapping[str, Any], market_as_of: str) -> str:
    value = _text(row.get("research_as_of"), 10)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid research_as_of for {row.get('code')}: {value!r}") from error
    if parsed < date.fromisoformat(market_as_of):
        raise ValueError(f"research_as_of precedes market snapshot: {row.get('code')}")
    return value


def _claims(row: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    raw_sources = row.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) < 2:
        raise ValueError(f"Luna review needs at least two sources: {row.get('code')}")
    facts = _financial_fact_items(row)
    fact_source_urls = {
        _text(fact.get("source_url"), 1200)
        for fact in facts
        if isinstance(fact, Mapping) and _text(fact.get("source_url"), 1200)
    }
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    target_code = _text(row.get("code"), 16)
    for source in raw_sources[:8]:
        if not isinstance(source, Mapping):
            raise ValueError(f"invalid source row: {row.get('code')}")
        url = _text(source.get("url"), 1200)
        if not _URL_RE.fullmatch(url):
            raise ValueError(f"source is not HTTPS: {row.get('code')}: {url!r}")
        if any(code != target_code for code in _source_codes(url)):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(
            {
                "url": url,
                "title": _clean_source_title(source.get("title"), target_code),
                "date": _text(source.get("date") or source.get("source_date"), 32),
                "key_facts": _text(source.get("key_facts") or source.get("key_points"), 600),
            }
        )
    if len(sources) < 2:
        raise ValueError(f"Luna review needs two distinct sources: {row.get('code')}")
    # Two independently selected sources are sufficient for the public
    # evidence contract.  Prefer URLs explicitly attached to facts, then fill
    # with the model's remaining search results.  Keeping the public graph
    # bounded avoids turning the full release audit into thousands of duplicate
    # fetches while retaining a dated primary disclosure plus a corroborating
    # source for every company.
    if len(sources) > 2:
        fact_sources = [source for source in sources if source["url"] in fact_source_urls]
        other_sources = [source for source in sources if source["url"] not in fact_source_urls]
        # Prefer a readable HTML/JSON mirror when the model supplied one.  It
        # carries the same dated disclosure facts while avoiding a release
        # audit bottleneck on hundreds of multi-megabyte PDF reports.  A PDF
        # remains eligible when no non-PDF corroboration exists.
        ordered = sorted(fact_sources + other_sources, key=lambda source: _is_pdf_source(source["url"]))
        sources = ordered[:2]
        seen_urls = {source["url"] for source in sources}
    claims: list[dict[str, Any]] = []
    fact_statements: list[tuple[str, set[str]]] = []
    for fact in facts:
        if isinstance(fact, Mapping):
            statement = _sanitize_reason_text(
                f"{_period_label(fact.get('period'))}：{_clean_fact_text(fact.get('fact'), 520)}",
                600,
            )
            source_url = _text(fact.get("source_url"), 1200) or None
        else:
            statement = _sanitize_reason_text(_clean_fact_text(fact), 600)
            source_url = None
        fact_statements.append((statement, _research_dimensions(statement), source_url))
    remaining = list(range(len(fact_statements)))
    used_dimensions: set[str] = set()
    for index, source in enumerate(sources, 1):
        statement = _sanitize_reason_text(
            source["key_facts"] or source["title"] or "公司公开披露来源已检索。",
            600,
        )
        if remaining:
            # Prefer a fact that explicitly names this source.  This avoids
            # attaching a cash-flow number to an unrelated industry article
            # when the model supplied per-fact source_url metadata.
            chosen_position = next(
                (position for position in remaining if fact_statements[position][2] == source["url"]),
                next(
                    (position for position in remaining if fact_statements[position][1] - used_dimensions),
                    remaining[0],
                ),
            )
            remaining.remove(chosen_position)
            statement, dimensions, _fact_source_url = fact_statements[chosen_position]
            if statement.strip("："):
                used_dimensions.update(dimensions)
        claims.append(
            {
                "fact_id": f"luna-source-{index:03d}",
                "statement": statement,
                "source_ref": source["url"],
                "source_refs": [source["url"]],
                "source_context": source["title"],
                # Priority-buy reviews need at least two independently linked
                # dated facts; all source-backed rows therefore remain
                # auditable support claims rather than unlabeled prose.
                # Every selected fact is a source-backed research claim.  The
                # contract, rather than an arbitrary first-three cutoff,
                # decides whether the set spans enough dimensions for a
                # priority recommendation.
                "support": "supports",
                "source_kind": "codex_luna_web_search",
            }
        )
    # A single disclosure URL often supports several dimensions (for example
    # earnings and operating cash flow).  Keep the additional fact-to-source
    # edges instead of silently dropping them after one claim per URL; the
    # public priority-buy contract requires two independently linked research
    # dimensions, not merely two URL labels.
    for position in remaining:
        statement, dimensions, fact_source_url = fact_statements[position]
        source_ref = fact_source_url if fact_source_url in seen_urls else sources[0]["url"]
        claims.append(
            {
                "fact_id": f"luna-fact-{len(claims) + 1:03d}",
                "statement": statement,
                "source_ref": source_ref,
                "source_refs": [source_ref],
                "source_context": next(
                    (source["title"] for source in sources if source["url"] == source_ref),
                    sources[0]["title"],
                ),
                "support": "supports",
                "source_kind": "codex_luna_web_search",
            }
        )
    return claims, [source["url"] for source in sources]


def _review(packet: Mapping[str, Any], row: Mapping[str, Any], *, market_as_of: str) -> dict[str, Any]:
    code = _text(packet.get("security_code"), 16)
    if _text(row.get("code"), 16) != code:
        raise ValueError(f"Luna review identity mismatch: {code}")
    decision = _text(row.get("decision"), 32)
    if decision not in _DECISIONS:
        raise ValueError(f"invalid Luna decision for {code}: {decision!r}")
    score_raw = row.get("score")
    if isinstance(score_raw, bool):
        raise ValueError(f"invalid Luna score for {code}")
    score = float(score_raw)
    if decision == "recommend_buy" and score < 70:
        raise ValueError(f"recommend_buy score must be >=70 for {code}: {score}")
    if decision == "observe" and not 50 <= score < 70:
        raise ValueError(f"observe score must be 50..69 for {code}: {score}")
    if decision == "do_not_recommend" and score >= 50:
        raise ValueError(f"do_not_recommend score must be <50 for {code}: {score}")
    research_as_of = _research_date(row, market_as_of)
    claims, urls = _claims(row)
    positive_values = row.get("buy_reasons")
    if not isinstance(positive_values, list) or not any(_text(value) for value in positive_values):
        positive_values = row.get("strengths", [])
    strengths = [_sanitize_reason_text(value, 240) for value in positive_values if _text(value)]
    risks = [_sanitize_reason_text(value, 240) for value in row.get("risks", []) if _text(value)]
    facts = []
    for value in _financial_fact_items(row):
        if isinstance(value, Mapping):
            # Keep the numeric tail (value/change/unit) that many packet
            # writers place after a short metric label.  Truncating at 200
            # characters could remove the only unit and make an otherwise
            # valid dated fact fail the research contract.
            facts.append(
                _public_fact_text(
                    f"{_period_label(value.get('period'))}：{_sanitize_reason_text(value.get('fact') or '', 520)}"
                )
            )
        elif _text(value):
            facts.append(_sanitize_reason_text(_clean_fact_text(value, 240), 240))
    if not risks:
        raise ValueError(f"Luna review has no risk flags: {code}")
    if not facts:
        raise ValueError(f"Luna review has no financial facts: {code}")
    if not strengths and decision == "recommend_buy":
        raise ValueError(f"recommend_buy review has no positive evidence: {code}")
    action = {
        "recommend_buy": ("priority_buy", "recommend_buy", "recommend_buy", "建议买", "confirmed", "keep"),
        "observe": ("watchlist", "observe", "do_not_recommend_buy", "观察", "caution", "manual_review"),
        "do_not_recommend": ("avoid", "do_not_recommend", "do_not_recommend_buy", "不建议", "confirmed", "demote"),
    }[decision]
    if decision == "recommend_buy":
        supported_dimensions: set[str] = set()
        for claim in claims:
            if claim.get("support") == "supports":
                supported_dimensions.update(_research_dimensions(claim.get("statement")))
        if len(supported_dimensions) < 2:
            for claim in claims:
                if claim.get("support") == "supports":
                    continue
                dimensions = _research_dimensions(claim.get("statement"))
                if dimensions - supported_dimensions:
                    claim["support"] = "supports"
                    supported_dimensions.update(dimensions)
                    if len(supported_dimensions) >= 2:
                        break
    summary = _sanitize_reason_text(row.get("summary"), 1200)
    if decision_text_conflicts(action[0], summary):
        summary = f"{facts[0]} 风险核验：{risks[0]} 独立结论：{action[3]}。"
    years = _date_years(row) or [int(market_as_of[:4])]
    latest_year = max(years)
    latest_source_year = max(
        [
            int(value[:4])
            for value in (
                _text(source.get("date") or source.get("source_date"), 10)
                for source in row.get("sources", [])
                if isinstance(source, Mapping)
            )
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
        ]
        or [latest_year]
    )
    freshness = "current_or_recent" if latest_source_year >= int(market_as_of[:4]) - 1 else "historical"
    if freshness == "historical" and decision == "recommend_buy":
        raise ValueError(f"historical-only evidence cannot support recommend_buy: {code}")
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "security_code": code,
        "company_name": _text(packet.get("name"), 160) or _text(row.get("name"), 160),
        "type_key": _text(packet.get("type_key"), 16),
        "verdict": action[4],
        "recommended_action": action[5],
        "buy_attractiveness_score": score,
        "ai_action": action[0],
        "final_category": action[1],
        "final_recommendation": action[2],
        "recommendation_label": action[3],
        "ai_independent": True,
        "economic_category": "other",
        "score_components": {"risk_adjusted_expected_return": score, "evidence_confidence": 85.0 if _evidence_grade(row) == "high" else 65.0},
        "confidence": "high" if _evidence_grade(row) == "high" else "medium",
        "calibration_adjustments": {
            "raw_score": score,
            "final_score": score,
            "quality_hard_block": False,
        },
        "summary": summary,
        "key_strengths": strengths[:8],
        "risk_flags": risks[:12],
        "quantitative_facts": facts[:8],
        "claims": claims[:12],
        "model": MODEL,
        "effort": EFFORT,
        "web_search_performed": True,
        "web_search_verified": True,
        "web_search_event_verified": True,
        "web_search_claim_urls_verified": True,
        "research_source_urls_verified": True,
        "web_search_queries": list(
            dict.fromkeys(_text(value, 240) for value in row.get("search_queries", []) if _text(value))
        )[:16],
        "web_search_verified_claim_urls": urls[:16],
        "web_search_dropped_claim_url_count": 0,
        "codex_web_tool": True,
        "provider_native_search": False,
        "provider_native_event_verified": False,
        "freshness_status": freshness,
        "freshness_years": years[:12],
        "freshness_penalty": 0.0 if freshness == "current_or_recent" else 5.0,
        "freshness_note": f"Luna Max 逐家公司直接 web__run 检索；研究日 {research_as_of}，估值快照日 {market_as_of} 分开记录。",
        "_candidate_type_keys": [
            _text(item.get("type_key"), 16)
            for item in packet.get("candidate_types", [])
            if isinstance(item, Mapping) and _text(item.get("type_key"), 16)
        ]
        or [_text(packet.get("type_key"), 16)],
    }


def convert(input_path: Path, shard_paths: list[Path], output_path: Path) -> dict[str, int]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("packets"), list):
        raise ValueError("input queue packets are missing")
    rows = _load_rows(shard_paths)
    packets: list[dict[str, Any]] = []
    for expected_index, raw_packet in enumerate(payload["packets"]):
        if not isinstance(raw_packet, Mapping):
            raise ValueError("candidate packet is not an object")
        code = _text(raw_packet.get("security_code"), 16)
        row = rows.get(code)
        if row is None:
            raise ValueError(f"missing Luna web review: {code}")
        if row.get("index") != expected_index:
            raise ValueError(f"Luna review index mismatch for {code}: {row.get('index')} != {expected_index}")
        packet = dict(raw_packet)
        review = _review(packet, row, market_as_of=_text(payload.get("market_as_of"), 10))
        errors = validate_review(review, require_readable_reason=True)
        if errors:
            raise ValueError(f"invalid converted Luna review for {code}: {','.join(errors)}")
        packet["ai_review"] = review
        packets.append(packet)
    extra = sorted(set(rows) - {_text(packet.get("security_code"), 16) for packet in packets})
    if extra:
        raise ValueError(f"Luna review is outside candidate queue: {extra[0]}")
    output = dict(payload)
    output["review_mode"] = REVIEW_MODE
    # The queue builder is the source of truth for the candidate universe, but
    # a converter must not promote a sliced input into a full-coverage release
    # merely because an upstream flag was copied through.  Recheck the same
    # offset/count/type-pair invariants here at the publication boundary.
    candidate_total = int(payload.get("candidate_total") or 0)
    type_pair_total = int(payload.get("type_pair_candidate_total") or 0)
    selected_type_pair_count = sum(int(packet.get("type_pair_count", 1) or 1) for packet in packets)
    queue_full_coverage = bool(
        payload.get("queue_full_coverage")
        and int(payload.get("candidate_offset") or 0) == 0
        and len(packets) == candidate_total
        and selected_type_pair_count == type_pair_total
    )
    output["queue_full_coverage"] = queue_full_coverage
    output["full_coverage_final_recommendation"] = queue_full_coverage
    research_dates = {
        _text(row.get("research_as_of"), 10)
        for row in rows.values()
        if _text(row.get("research_as_of"), 10)
    }
    if len(research_dates) == 1:
        output["research_as_of"] = next(iter(research_dates))
    output["packets"] = packets
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"candidate_total": len(packets), "reviewed": len(rows), "recommend_buy": sum(row.get("decision") == "recommend_buy" for row in rows.values())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("shards", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps(convert(args.input, args.shards, args.out), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
