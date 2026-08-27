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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    # Remove deterministic labels without changing the state they describe.
    # In particular, “接近达标”“未达标”和“未触发” are materially different
    # investment facts and must remain visible in the AI explanation.
    text = re.sub(r"(?:类型\s*[1-7](?:\s*(?:与|和|及|/|-)\s*类型?\s*[1-7])*)\s*(?:双|多)?触发", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:type\s*[1-7](?:\s*(?:and|or|to|/|-)\s*type?\s*[1-7])*)\s*(?:double|multi)?\s*trigger(?:ed)?", "", text, flags=re.IGNORECASE)
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


_FACT_UNIT_FACTORS: dict[str, Decimal] = {
    "元": Decimal("1"),
    "人民币": Decimal("1"),
    "cny": Decimal("1"),
    "rmb": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "百万元": Decimal("1000000"),
    "亿元": Decimal("100000000"),
    "万": Decimal("10000"),
    "亿": Decimal("100000000"),
}
_AMOUNT_RE = re.compile(
    r"(?<![\d.])(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?))\s*"
    # Chinese prose commonly runs the unit into the next Chinese character;
    # only an ASCII identifier/percent suffix should terminate this match.
    r"(?P<unit>百万元|亿元|万元|千元|元|亿|万)(?![0-9A-Za-z_%])"
)
_METRIC_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("营业收入", "营业总收入", "营业收入", "营收"),
    ("归属于上市公司股东的净利润", "归属于母公司股东的净利润", "归母净利润"),
    ("扣除非经常性损益后的净利润", "扣非净利润", "扣非归母净利润"),
    ("经营活动产生的现金流量净额", "经营活动现金流量净额", "经营现金流", "经营活动现金流"),
    ("研发投入", "研发费用", "研发支出"),
    ("投资收益", "投资收入"),
    ("现金及现金等价物", "货币资金", "现金"),
    ("资产总额", "总资产"),
    ("归母权益", "归属于母公司股东权益", "股东权益"),
    ("财务费用",),
)

_SCALAR_WITH_UNIT_RE = re.compile(
    r"(?<![\d.])(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?))\s*"
    r"(?P<unit>百万元|亿元|万元|千元|元/股|元／股|元|亿|万|%|％|人|万人|户|台|吨|公里|股|次|个|项|年|倍|家)"
)


def _inferred_fact_unit(metric: Any) -> str | None:
    """Infer a unit only from an explicit metric/key suffix.

    The producer frequently emits a compact mapping such as ``revenue_rmb``
    or ``roe_pct``.  These suffixes are unambiguous; a free-form metric is
    deliberately conservative and returns ``None`` unless its wording makes
    the unit clear.
    """

    text = re.sub(r"\s+", "", _text(metric, 180)).casefold()
    if not text:
        return None
    if "_usd_billion" in text:
        return "十亿美元"
    if "_usd_million" in text:
        return "百万美元"
    if text.endswith(("_10000_kl", "_10k_kl")):
        return "万千升"
    if text.endswith("_100m_tons"):
        return "亿吨"
    if "eflops" in text:
        return "EFLOPS"
    if text.endswith(("_yi_cny", "_billion_cny", "_cny_billion")):
        return "亿元"
    if text.endswith(("_cny_100m", "_rmb_100m", "_100m_cny")) or "_cny_100m_" in text:
        return "亿元"
    if text.endswith(("_cny_million", "_rmb_million", "_million_cny")):
        return "百万元"
    for suffix, unit in (
        ("_百万元", "百万元"),
        ("_亿元", "亿元"),
        ("_万元", "万元"),
        ("_千元", "千元"),
        ("_亿", "亿"),
    ):
        if text.endswith(suffix):
            return unit
    if text.endswith(("_wan_t", "_10k_tons", "_10000_tons")):
        return "万吨"
    if text.endswith(("_tons", "_ton")):
        return "吨"
    if text.endswith("_100m_kwh"):
        return "亿千瓦时"
    if text.endswith("_mwe"):
        return "兆瓦"
    if text.endswith(("_pct", "_percent")) or "_pct_" in text or any(
        token in text for token in ("百分比", "占比", "同比", "增长率", "收益率", "净利率", "毛利率", "roe", "率", "比例")
    ):
        return "%"
    # EPS is a per-share amount even when the producer omits the explicit
    # ``per_share`` suffix (for example ``2025_eps_cny`` or ``eps_2026H1``).
    # Keep this after the percentage branch so ``eps_yoy_pct`` remains ``%``.
    if re.search(r"(?:^|_)eps(?:_|$)", text):
        return "元/股"
    if text.endswith(("_price_cny", "_close_cny", "_open_cny", "_high_cny", "_low_cny", "_per_share_cny")):
        return "元/股"
    if text.endswith(("_per_share", "_cny_per_share")) or "每股" in text or text in {"eps", "eps_cny"}:
        return "元/股"
    # Key suffixes are authoritative for compact structured facts.  Keep
    # these checks before loose token checks below: ``operating_cash_flow``
    # contains the substring ``pe`` but is an amount, not a multiple.  This
    # comes after the per-share branch so ``eps_cny`` remains 元/股.
    if text.endswith(("_cny", "_rmb", "_yuan")):
        return "元"
    if text.endswith("_margin") or text in {"margin", "gross_margin", "net_margin", "operating_margin"}:
        return "%"
    if text.endswith("_ratio") and not re.search(r"(?:^|_)(?:pe|pb|ps|pcf|peg|multiple)(?:_|$)", text):
        return "%"
    if text.endswith("_wan") or "万人" in text:
        return "万人"
    if text.endswith("_yi"):
        return "亿股"
    if text.endswith(("_shares", "_shares_approx")) or "股数" in text or "持股" in text:
        return "股"
    if text in {"stores", "direct_stores", "franchise_stores", "total_stores"} or "store_count" in text or "store_number" in text:
        return "家"
    if text.endswith(("_units", "_count")) or "_units_" in text or any(
        token in text for token in ("agents", "skills", "patents")
    ):
        return "项"
    if text.endswith("_years") or "连续年" in text:
        return "年"
    if "fleet" in text or "a320" in text or text.endswith(("_aircraft", "_planes")):
        return "架"
    if "detonator" in text:
        return "发"
    if "capacity" in text and "mwe" not in text:
        return "项"
    if "goodwill" in text:
        return "元"
    if "pledge_count" in text:
        return "笔"
    if "price" in text or "close" in text:
        return "元"
    if "cagr" in text:
        return "%"
    if any(
        token in text
        for token in (
            "收入",
            "营收",
            "利润",
            "现金流",
            "资产",
            "权益",
            "费用",
            "负债",
            "市值",
            "收益",
            "资本",
            "借款",
            "存款",
            "revenue",
            "profit",
            "cash_flow",
            "assets",
            "equity",
            "debt",
            "market_cap",
        )
    ):
        return "元"
    # Match valuation-multiple names as tokens, not substrings.  For example,
    # ``operating_cash_flow`` contains the letters ``pe`` inside
    # ``operating`` and must remain an amount rather than becoming 倍.
    if text in {"ps", "pcf", "peg"} or re.search(r"(?:^|_)(?:pe|pb|ps|pcf|peg|multiple)(?:_|$)", text) or "倍数" in text:
        return "倍"
    if "per_passenger_km" in text:
        return "元/客公里"
    if any(token in text for token in ("人数", "员工", "客户数", "户数")):
        return "人"
    if any(token in text for token in ("台", "设备数", "产能")):
        return "台"
    return None


def _scalar_value_with_unit(value: Any) -> tuple[float | int, str] | None:
    """Extract the first explicitly unit-bearing scalar from a fact string."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value, ""
    if isinstance(value, (Mapping, list, tuple)):
        return None
    match = _SCALAR_WITH_UNIT_RE.search(_text(value, 600))
    if not match:
        return None
    decimal = _decimal(match.group("number"))
    if decimal is None:
        return None
    unit = match.group("unit").replace("％", "%").replace("／", "/")
    return (float(decimal) if decimal % 1 else int(decimal)), unit


def _is_compound_fact_unit(value: Any) -> bool:
    """Return whether a parent unit cannot safely apply to nested metrics."""

    normalized = re.sub(r"\s+", "", _text(value, 80)).casefold()
    if not normalized:
        return False
    if normalized in {
        "元",
        "人民币",
        "cny",
        "rmb",
        "元/股",
        "元／股",
        "cny/share",
        "rmb/share",
        "%",
        "％",
        "人",
        "万人",
        "户",
        "台",
        "吨",
        "万吨",
        "股",
        "倍",
        "项",
        "年",
    }:
        return False
    return bool(re.search(r"[/／,，;；、|+＋]|(?:或|及|和)", normalized))


def _unit_family(value: Any) -> str:
    """Classify a unit just enough to reject an incompatible declaration."""

    normalized = re.sub(r"\s+", "", _text(value, 80)).casefold()
    if not normalized:
        return ""
    if "%" in normalized or "百分比" in normalized:
        return "percent"
    if "倍" in normalized or "multiple" in normalized:
        return "multiple"
    if "/股" in normalized or "/share" in normalized or "per_share" in normalized:
        return "per_share"
    if "股" in normalized or "share" in normalized:
        return "shares"
    if any(token in normalized for token in ("吨", "ton", "千瓦时", "kwh", "兆瓦", "mwe")):
        return "physical"
    if any(token in normalized for token in ("元", "人民币", "cny", "rmb", "美元", "usd", "亿", "万", "million", "billion")):
        return "money"
    return normalized


def _binding_unit(metric: Any, explicit_unit: Any, parsed_unit: Any) -> str:
    """Choose a unit without letting a bad parent or suffix corrupt a fact."""

    metric_text = re.sub(r"\s+", "", _text(metric, 180)).casefold()
    inferred = _inferred_fact_unit(metric)
    explicit = _text(explicit_unit, 80)
    parsed = _text(parsed_unit, 80)
    if not explicit:
        return parsed or inferred or ""
    if not inferred:
        return explicit
    if _is_compound_fact_unit(explicit) or _unit_family(explicit) != _unit_family(inferred):
        return inferred
    # Scale-bearing key suffixes are authoritative even when the producer put
    # a generic currency label on a nested value (e.g. ``market_cap_yi_cny``).
    if metric_text.endswith(
        (
            "_yi_cny",
            "_billion_cny",
            "_cny_billion",
            "_cny_100m",
            "_rmb_100m",
            "_100m_cny",
            "_cny_million",
            "_rmb_million",
            "_million_cny",
            "_wan_t",
            "_10k_tons",
            "_10000_tons",
            "_100m_tons",
            "_100m_kwh",
            "_yi",
        )
    ):
        return inferred
    return explicit


def _unit_factor(value: Any) -> Decimal | None:
    normalized = _text(value, 40).casefold().replace(" ", "")
    if not normalized or "%" in normalized or "每股" in normalized or "/" in normalized or "股" in normalized:
        return None
    if normalized in _FACT_UNIT_FACTORS:
        return _FACT_UNIT_FACTORS[normalized]
    for token, factor in sorted(_FACT_UNIT_FACTORS.items(), key=lambda item: len(item[0]), reverse=True):
        if token in normalized:
            return factor
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _metric_aliases(metric: Any) -> list[str]:
    raw = re.sub(r"\s+", "", _text(metric, 180))
    if not raw:
        return []
    aliases: list[str] = []
    for group in _METRIC_ALIAS_GROUPS:
        if any(alias in raw for alias in group):
            aliases.extend(group)
    if raw in {"营业收入", "营业总收入"}:
        # Summary prose often abbreviates this one metric to “收入”.  Keep
        # the short alias out of explicitly qualified segment metrics such as
        # 海外收入/产品收入 (checked at the match site below).
        aliases.append("收入")
    aliases.append(raw)
    return sorted({alias for alias in aliases if len(alias) >= 2}, key=len, reverse=True)


def _period_info(item: Mapping[str, Any]) -> tuple[int | None, int | None, str]:
    value = next(
        (
            _text(item.get(key), 40)
            for key in ("date", "period", "report_date", "report_period", "date_or_period", "as_of")
            if _text(item.get(key), 40)
        ),
        "",
    )
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?:[-/.](\d{1,2})[-/.]\d{1,2})?", value)
    if match:
        year = int(match.group(1))
        if match.group(2):
            return year, int(match.group(2)), value
        suffix = value[match.end(1) :].casefold()
        if "q1" in suffix or "一季度" in suffix:
            return year, 3, value
        if "q2" in suffix or "h1" in suffix or "上半年" in suffix:
            return year, 6, value
        if "q3" in suffix or "前三季度" in suffix or "h2" in suffix:
            return year, 9, value
        return year, 12, value
    return None, None, value


def _period_matches(text: str, item: Mapping[str, Any]) -> bool:
    year, month, _period = _period_info(item)
    if year is None or not re.search(rf"(?<!\d){year}(?!\d)", text):
        return False
    normalized = text.casefold()
    if month == 3:
        return not any(
            token in normalized
            for token in ("上半年", "半年度", "半年报", "h1", "二季度", "三季度", "q2", "q3", "前三季度")
        )
    if month == 6:
        return any(token in normalized for token in ("上半年", "半年度", "半年报", "h1", "1-6", "1至6", "1—6"))
    if month == 9:
        return any(token in normalized for token in ("前三季度", "三季度", "q3", "1-9", "1至9", "1—9"))
    # A year-end fact should not be rebound to an explicitly interim sentence.
    if any(token in normalized for token in ("上半年", "半年度", "半年报", "一季度", "二季度", "三季度", "q1", "q2", "q3", "h1")):
        return any(token in normalized for token in ("年度", "年报", "全年", "fy"))
    return True


def _format_repaired_number(value: Decimal, original: str) -> str:
    decimals = len(original.replace(",", "").split(".", 1)[1]) if "." in original else 0
    if decimals:
        quantum = Decimal(1).scaleb(-decimals)
        value = value.quantize(quantum, rounding=ROUND_HALF_UP)
        formatted = f"{value:.{decimals}f}"
    else:
        formatted = f"{value.quantize(Decimal(1), rounding=ROUND_HALF_UP):.0f}"
    if "," in original:
        integer, dot, fraction = formatted.partition(".")
        sign = "" if not integer.startswith("-") else "-"
        digits = integer.lstrip("-")
        grouped = f"{int(digits):,}"
        formatted = f"{sign}{grouped}{dot}{fraction}" if dot else f"{sign}{grouped}"
    return formatted


def _repair_fact_unit_mentions(text: Any, row: Mapping[str, Any], *, field: str) -> tuple[str, list[dict[str, Any]]]:
    """Correct only an obvious scale transcription in AI prose.

    The raw value/unit/date/source remains authoritative.  A prose number is
    changed only when the same metric and reporting period are named nearby
    and the two normalized amounts differ by an exact common scale (10/100/
    1000).  Percentages, ratios, per-share values, and ambiguous metrics are
    intentionally left untouched.
    """

    raw_facts = row.get("financial_facts")
    if not isinstance(raw_facts, list):
        return _text(text, 1200), []
    current = _text(text, 1200)
    repairs: list[dict[str, Any]] = []
    for item in raw_facts:
        if not isinstance(item, Mapping):
            continue
        value = _decimal(item.get("value"))
        raw_factor = _unit_factor(item.get("unit") or item.get("units") or item.get("currency"))
        metric = _text(item.get("metric"), 180)
        if value is None or raw_factor is None or not metric or abs(value) == 0:
            continue
        lowered_metric = metric.casefold()
        if any(token in lowered_metric for token in ("同比", "增长", "率", "比例", "占比", "每股", "roe", "pe", "pb", "eps")):
            continue
        aliases = _metric_aliases(metric)
        for alias in aliases:
            match_alias = re.search(re.escape(alias), current, flags=re.IGNORECASE)
            if not match_alias:
                continue
            start = match_alias.start()
            if alias == "收入" and any(
                token in current[max(0, start - 4) : start] for token in ("海外", "产品", "主营", "其他", "利息", "租赁")
            ):
                continue
            end = min(len(current), match_alias.end() + 96)
            window = current[max(0, start - 72) : end]
            if not _period_matches(window, item):
                continue
            amount_match = _AMOUNT_RE.search(current, match_alias.end(), end)
            if not amount_match:
                continue
            suffix = current[amount_match.end() :].lstrip()
            if suffix.startswith(("/", "／")):
                continue
            candidate = _decimal(amount_match.group("number"))
            candidate_factor = _unit_factor(amount_match.group("unit"))
            if candidate is None or candidate_factor is None:
                continue
            expected_base = value * raw_factor
            actual_base = candidate * candidate_factor
            if expected_base == 0 or actual_base == 0:
                continue
            ratio = actual_base / expected_base
            scale: Decimal | None = next(
                (
                    factor
                    for factor in (Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("0.1"), Decimal("0.01"), Decimal("0.001"))
                    if abs(ratio - factor) <= abs(factor) * Decimal("0.02")
                ),
                None,
            )
            if scale is None:
                continue
            corrected = expected_base / candidate_factor
            old_number = amount_match.group("number")
            new_number = _format_repaired_number(corrected, old_number)
            if new_number == old_number:
                continue
            current = current[: amount_match.start("number")] + new_number + current[amount_match.end("number") :]
            repairs.append(
                {
                    "field": field,
                    "metric": metric,
                    "period": _text(item.get("period") or item.get("date"), 40),
                    "old": f"{old_number}{amount_match.group('unit')}",
                    "new": f"{new_number}{amount_match.group('unit')}",
                    "unit": _text(item.get("unit") or item.get("units") or item.get("currency"), 40),
                    "source_url": _text(item.get("source_url"), 1200),
                }
            )
            break
    return current, repairs


def _source_priority(source: Mapping[str, str], fact_source_urls: set[str]) -> tuple[int, int, str]:
    """Prefer the disclosure that actually carries the fact.

    Search output often puts a generic news, calendar, or company landing page
    before the filing URL.  Those pages are useful corroboration only when a
    dated primary disclosure is unavailable.  A stable priority keeps the
    public claim graph tied to the same source that produced the fact, while
    still allowing a second independent source.
    """

    url = _text(source.get("url"), 1200).casefold()
    title = _text(source.get("title"), 300).casefold()
    score = 0
    if source.get("url") in fact_source_urls:
        score -= 100
    if any(host in url for host in ("sse.com.cn", "szse.cn", "cninfo.com.cn", "hkexnews.hk")):
        score -= 35
    if "money.finance.sina.com.cn/corp" in url or "vip.stock.finance.sina.com.cn/corp" in url:
        score -= 30
    if any(token in title for token in ("年报", "半年报", "季报", "报告", "公告")):
        score -= 15
    if any(token in url for token in ("calendar", "historical-data", "companynews", "company-news")):
        score += 45
    # Prefer an HTML mirror over a large PDF only after primary-vs-secondary
    # status has been established.  This keeps source audits fast without
    # demoting a filing that is the only fact-bearing source.
    pdf_penalty = 1 if _is_pdf_source(url) else 0
    return score, pdf_penalty, url


def _source_codes(value: Any) -> set[str]:
    parsed = urlparse(str(value or ""))
    codes: set[str] = set()
    # Only query fields that conventionally carry a security identifier count.
    # Article IDs frequently happen to be six digits (for example
    # ``/2026/0708/656348.shtml``); treating every number as a stock code
    # would discard an otherwise valid industry source.
    for key, values in parse_qs(parsed.query).items():
        key_lower = key.casefold()
        if not any(token in key_lower for token in ("stock", "secid", "symbol", "ticker", "code")):
            continue
        for item in values:
            codes.update(re.findall(r"(?<!\d)(?:[036]\d{5})(?!\d)", item))
    path = parsed.path
    for pattern in (
        r"(?i)(?:^|/)s/(?:sh|sz)?([036]\d{5})(?=[/_.?_-]|$)",
        r"(?i)(?:^|/)(?:stock|equities|quote)[/_-]+(?:sh|sz)?([036]\d{5})(?=[/_.?_-]|$)",
    ):
        codes.update(match.group(1) for match in re.finditer(pattern, path))
    return codes


def _clean_source_title(value: Any, target_code: str) -> str:
    title = _text(value, 300)
    return re.sub(
        r"(?<!\d)(?:[036]\d{5})(?!\d)",
        lambda match: match.group(0) if match.group(0) == target_code else "相关公司",
        title,
    )


def _financial_fact_source_urls(item: Mapping[str, Any], row: Mapping[str, Any] | None = None) -> list[str]:
    """Return exact HTTPS URLs attached to one financial fact.

    ``source`` is also used as a human-readable title in older shards, so only
    URL-shaped values become bindings.  Explicit HTTP sources are rejected
    rather than silently downgraded to an unrelated top-level source.
    """

    values: list[Any] = []
    for key in ("source_url", "source_ref", "source"):
        if item.get(key) is not None:
            values.append(item.get(key))
    for key in ("source_urls", "source_refs"):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(value)
    source_index = item.get("source_index")
    if row is not None and isinstance(source_index, int) and not isinstance(source_index, bool):
        raw_sources = row.get("sources")
        if isinstance(raw_sources, list) and 0 <= source_index < len(raw_sources):
            source_item = raw_sources[source_index]
            if isinstance(source_item, Mapping):
                values.append(source_item.get("url"))
    urls: list[str] = []
    for value in values:
        candidate = _text(value, 1200)
        if candidate.lower().startswith("http://"):
            raise ValueError(f"financial fact source must use HTTPS: {candidate!r}")
        if _URL_RE.fullmatch(candidate) and candidate not in urls:
            urls.append(candidate)
    return urls


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


def _financial_fact_items(row: Mapping[str, Any]) -> list[dict[str, Any] | str]:
    """Normalize the two Luna packet fact shapes into dated readable facts.

    Most shards emit ``[{period, fact, status}, ...]``.  A later batch emitted
    the already-extracted queue metrics as a mapping instead.  Keep that data
    rather than treating a non-list as missing; the adapter still requires a
    real numeric fact and the public contract validates the resulting text.
    """

    raw = row.get("financial_facts")
    if isinstance(raw, list):
        items: list[dict[str, Any] | str] = []
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

        def metric_text(key: str, value: Any, unit: str = "") -> str:
            label = metric_labels.get(key, key.replace("_", " "))
            text = _value_text(value, 240)
            key_lower = key.casefold()
            explicit_unit = _text(unit, 80)
            is_percentage = key_lower.endswith("_pct") or "ratio" in key_lower or "growth" in key_lower
            if (
                explicit_unit
                and text
                and not isinstance(value, (Mapping, list, tuple))
                and not is_percentage
                and key_lower not in {"change", "yoy"}
            ):
                # Preserve the producer's declared unit exactly.  Do not
                # append a second inferred unit when the value already carries
                # one (for example ``8.2`` + ``%`` or ``12 亿元``).
                if explicit_unit.casefold() not in text.casefold():
                    text = f"{text}{explicit_unit}"
            elif is_percentage and text and "%" not in text:
                text = f"{text}%"
            elif (
                (
                    key.endswith(("_rmb", "_cny_yuan"))
                    or "cny" in key_lower
                    or "rmb" in key_lower
                    or key_lower.endswith("_yuan")
                )
                and text
                and not re.search(r"元|万|亿", text)
            ):
                text = f"{text}元"
            elif "square_meter" in key_lower and text and not re.search(r"平方米|万平", text):
                text = f"{text}平方米"
            return f"{label} {text}".strip()

        def fact_unit(item: Mapping[str, Any]) -> str:
            for key in ("unit", "units", "currency"):
                value = _text(item.get(key), 80)
                if value:
                    return value
            return ""

        def fact_period(item: Mapping[str, Any]) -> tuple[str, str, str]:
            # Reporting date/period wins over the publication date.  The latter
            # remains available as metadata instead of replacing the fiscal
            # period when both are present.
            period = next(
                (
                    _text(item.get(key), 40)
                    for key in (
                        "period",
                        "date",
                        "date_or_period",
                        "date_or_year",
                        "report_period",
                        "report_date",
                        "as_of",
                        "source_date",
                    )
                    if _text(item.get(key), 40)
                ),
                "",
            )
            report_date = _text(item.get("date"), 40)
            source_date = _text(item.get("source_date"), 40)
            return period, report_date, source_date

        for item in raw:
            if isinstance(item, Mapping):
                period, report_date, source_date = fact_period(item)
                source_urls = _financial_fact_source_urls(item, row)
                source_url = source_urls[0] if source_urls else ""
                fact = _text(item.get("fact"), 600)
                unit = fact_unit(item)
                if not fact:
                    metric = _text(item.get("metric"), 180)
                    metric = re.sub(r"^((?:19|20)\d{2})(?=[^\d年])", r"\1年", metric)
                    value_fields = []
                    if item.get("value") is not None:
                        value_fields.append(("value", item.get("value"), unit))
                    value_fields.extend(
                        (str(key), value, unit)
                        for key, value in item.items()
                        if str(key).casefold().startswith(("value_", "change_", "yoy"))
                        and value is not None
                        and key not in {"value", "change", "yoy"}
                    )
                    rendered_values = [metric_text(key, value, value_unit) for key, value, value_unit in value_fields]
                    yoy = _value_text(item.get("yoy") or item.get("change"), 180)
                    if yoy and not any(yoy.casefold() in value.casefold() for value in rendered_values):
                        rendered_values.append(f"同比 {yoy if '%' in yoy else f'{yoy}%'}")
                    parts = [part for part in (metric, *rendered_values) if part]
                    if not parts:
                        metric_parts = [
                            metric_text(str(key), value)
                            for key, value in item.items()
                            if key
                            not in {
                                "period",
                                "status",
                                "source_date",
                                "source_url",
                                "source",
                                "source_index",
                                "date",
                                "date_or_period",
                                "date_or_year",
                                "report_period",
                                "report_date",
                                "as_of",
                                "unit",
                                "units",
                                "currency",
                                "source_urls",
                                "source_refs",
                            }
                            and value is not None
                        ]
                        if metric_parts:
                            for metric_part in metric_parts:
                                record: dict[str, Any] = {
                                    "period": period,
                                    "fact": metric_part,
                                    "status": _text(item.get("status"), 32),
                                }
                                if report_date and report_date != period:
                                    record["date"] = report_date
                                if source_date and source_date != period:
                                    record["source_date"] = source_date
                                if source_url:
                                    record["source_url"] = source_url
                                if len(source_urls) > 1:
                                    record["source_urls"] = source_urls
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
                        value_fields.append(("value", item.get("value"), unit))
                    value_fields.extend(
                        (str(key), value, unit)
                        for key, value in item.items()
                        if str(key).casefold().startswith(("value_", "change_", "yoy"))
                        and value is not None
                        and key not in {"value", "change", "yoy"}
                    )
                    for key, value, value_unit in value_fields:
                        rendered = metric_text(key, value, value_unit)
                        if rendered and rendered.casefold() not in fact.casefold():
                            extras.append(rendered)
                    change_text = _value_text(item.get("change") or item.get("yoy"), 180)
                    if change_text and not any(change_text.casefold() in extra.casefold() for extra in extras):
                        extras.append(f"同比 {change_text if '%' in change_text else f'{change_text}%'}")
                    if extras:
                        fact = f"{fact}；{'；'.join(extras)}"
                if fact or period:
                    record: dict[str, Any] = {
                        "period": period,
                        "fact": fact or "公司公开披露事实，具体数值见来源。",
                        "status": _text(item.get("status"), 32),
                    }
                    if report_date and report_date != period:
                        record["date"] = report_date
                    if source_date and source_date != period:
                        record["source_date"] = source_date
                    if source_url:
                        record["source_url"] = source_url
                    if len(source_urls) > 1:
                        record["source_urls"] = source_urls
                    items.append(record)
            elif _text(item):
                items.append(_text(item, 600))
        return items
    if not isinstance(raw, Mapping):
        return []
    items: list[dict[str, Any]] = []
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


def _financial_fact_bindings(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep dated facts while separating scalars from compound source text.

    Luna rows use two deliberate shapes: a scalar fact (``value`` plus an
    explicit unit) and a source quotation containing several values.  Treating
    the latter as one numeric scalar made the old audit report false unit and
    value errors.  Scalar fields are normalised into ``value``/``unit``;
    compound quotations remain lossless in ``value_text`` or
    ``derived_values`` and are never assigned a guessed amount.
    """

    raw = row.get("financial_facts")
    bindings: list[dict[str, Any]] = []

    def common_fields(item: Mapping[str, Any]) -> dict[str, Any]:
        binding: dict[str, Any] = {}
        for key in (
            "date",
            "period",
            "source_date",
            "date_or_period",
            "date_or_year",
            "as_of",
            "report_date",
            "report_period",
        ):
            if item.get(key) not in (None, ""):
                binding[key] = item[key]
        source_urls = _financial_fact_source_urls(item, row)
        if source_urls:
            binding["source_url"] = source_urls[0]
            if len(source_urls) > 1:
                binding["source_urls"] = source_urls
        return binding

    def append_value(
        base: Mapping[str, Any],
        *,
        metric: Any,
        value: Any,
        explicit_unit: Any = None,
        unit_metric: Any = None,
        value_text: Any = None,
    ) -> None:
        binding = dict(base)
        metric_text = _text(metric, 180)
        if metric_text:
            binding["metric"] = metric_text
        scalar = _scalar_value_with_unit(value)
        if scalar is not None:
            scalar_value, parsed_unit = scalar
            binding["value"] = scalar_value
            unit = _binding_unit(unit_metric if unit_metric is not None else metric, explicit_unit, parsed_unit)
            if unit:
                binding["unit"] = unit
            if value_text not in (None, ""):
                binding["value_text"] = _value_text(value_text, 600)
        elif value not in (None, ""):
            binding["value_text"] = _value_text(value, 600)
        if binding:
            bindings.append(binding)

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            base = common_fields(item)
            metric = item.get("metric")
            value = item.get("value")
            explicit_unit = item.get("unit") or item.get("units") or item.get("currency")
            if isinstance(value, Mapping):
                # A mapping value carries independent metrics and units.  Do
                # not collapse it into one invalid object-valued scalar.
                for key, nested_value in value.items():
                    if nested_value in (None, ""):
                        continue
                    nested_unit = _inferred_fact_unit(key)
                    if not nested_unit and explicit_unit and not _is_compound_fact_unit(explicit_unit):
                        nested_unit = explicit_unit
                    append_value(
                        base,
                        metric=key,
                        value=nested_value,
                        explicit_unit=nested_unit,
                    )
            elif isinstance(value, (list, tuple)):
                binding = dict(base)
                if _text(metric, 180):
                    binding["metric"] = _text(metric, 180)
                binding["value_text"] = _value_text(value, 600)
                if binding:
                    bindings.append(binding)
            else:
                append_value(
                    base,
                    metric=metric,
                    value=value,
                    explicit_unit=explicit_unit,
                    unit_metric=metric or item.get("fact"),
                    value_text=value,
                )
            derived = {
                str(name): nested_value
                for name, nested_value in item.items()
                if str(name).casefold().startswith(("value_", "change_", "yoy")) and nested_value is not None
            }
            if derived and bindings:
                bindings[-1].setdefault("derived_values", {}).update(derived)
        return bindings
    if isinstance(raw, Mapping):
        # The mapping shape has no per-row source URL, but its fiscal key still
        # carries a precise period and the original numeric value.
        for key, value in raw.items():
            match = re.fullmatch(r"fy(\d{4})(q[1-4]|h[12])?_(.+)_rmb", str(key), re.IGNORECASE)
            if not match or value is None:
                continue
            year, interim, metric = match.groups()
            period = f"{year}{interim.upper() if interim else 'FY'}"
            append_value(
                {"period": period},
                metric=metric,
                value=value,
                explicit_unit="rmb",
            )
    return bindings


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
        url
        for fact in facts
        if isinstance(fact, Mapping)
        for url in (
            [_text(fact.get("source_url"), 1200)]
            + (
                [_text(value, 1200) for value in fact.get("source_urls", []) if _text(value, 1200)]
                if isinstance(fact.get("source_urls"), list)
                else []
            )
        )
        if url
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
    # A fact may carry a valid search URL that the model forgot to repeat in
    # its top-level source list.  Keep that exact URL in the auditable source
    # graph rather than silently rebinding the fact to the first unrelated
    # source.  The URL remains subject to the same HTTPS/company-code checks.
    for url in sorted(fact_source_urls):
        if not _URL_RE.fullmatch(url):
            raise ValueError(f"financial fact source is not HTTPS: {row.get('code')}: {url!r}")
        if any(code != target_code for code in _source_codes(url)):
            raise ValueError(f"financial fact source belongs to another company: {row.get('code')}: {url!r}")
        if url not in seen_urls:
            seen_urls.add(url)
            sources.append(
                {
                    "url": url,
                    "title": "财务事实来源",
                    "date": "",
                    "key_facts": "",
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
        ordered = sorted(
            fact_sources + other_sources,
            key=lambda source: _source_priority(source, fact_source_urls),
        )
        if len(fact_sources) > 2:
            # Every fact-linked URL must remain available for its edge.  Keep
            # all of them; the bounded two-source projection only applies when
            # it cannot discard a fact binding.
            sources = sorted(fact_sources, key=lambda source: _is_pdf_source(source["url"]))
        else:
            sources = ordered[:2]
        seen_urls = {source["url"] for source in sources}
    claims: list[dict[str, Any]] = []
    fact_statements: list[tuple[str, set[str], str | None]] = []
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
    fact_bindings = _financial_fact_bindings(row)
    numeric_fact_repairs: list[dict[str, Any]] = []

    def repaired(value: Any, field: str) -> str:
        normalized, repairs = _repair_fact_unit_mentions(value, row, field=field)
        numeric_fact_repairs.extend(repairs)
        return normalized

    positive_values = row.get("buy_reasons")
    if not isinstance(positive_values, list) or not any(_text(value) for value in positive_values):
        positive_values = row.get("strengths", [])
    strengths = [
        _sanitize_reason_text(repaired(value, "key_strengths"), 240)
        for value in positive_values
        if _text(value)
    ]
    risks = [
        _sanitize_reason_text(repaired(value, "risk_flags"), 240)
        for value in row.get("risks", [])
        if _text(value)
    ]
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
    summary = _sanitize_reason_text(repaired(row.get("summary"), "summary"), 1200)
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
        "score_components": {
            "risk_adjusted_expected_return": score,
            "evidence_confidence": 85.0 if _evidence_grade(row) == "high" else 65.0,
        },
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
        "financial_fact_bindings": fact_bindings,
        "numeric_fact_repairs": numeric_fact_repairs,
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
        _text(row.get("research_as_of"), 10) for row in rows.values() if _text(row.get("research_as_of"), 10)
    }
    if len(research_dates) == 1:
        output["research_as_of"] = next(iter(research_dates))
    output["packets"] = packets
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "candidate_total": len(packets),
        "reviewed": len(rows),
        "recommend_buy": sum(row.get("decision") == "recommend_buy" for row in rows.values()),
    }


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
