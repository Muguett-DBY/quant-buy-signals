"""Build short, period-labelled company facts for AI screening cards.

These facts are a neutral snapshot of the company.  They are deliberately
separate from the deterministic candidate status and from the model's
investment opinion: a card may show the same price or annual-history fact for
any candidate type without implying that a rule was triggered or that a score
supports the AI conclusion.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


# A fact must contain an actual number.  Matching a bare unit (for example
# the ``年`` in a generic phrase) would let narrative boilerplate pass as a
# quantitative explanation.
_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:%|亿|万|元|倍|年)?")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _fmt(value: Any, digits: int = 2) -> str:
    number = _number(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _market_cap_yi(value: Any) -> str:
    number = _number(value)
    if number is None:
        return ""
    return f"{number / 100_000_000:.2f}".rstrip("0").rstrip(".")


def _annual_end_year(company: Mapping[str, Any]) -> int | None:
    history = company.get("annual_history")
    if not isinstance(history, list):
        return None
    years = [
        int(item.get("end_year", item.get("year")))
        for item in history
        if isinstance(item, Mapping) and isinstance(item.get("end_year", item.get("year")), int)
    ]
    return max(years) if years else None


def _unique(values: list[str], limit: int = 8) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text[:240])
        if len(result) >= limit:
            break
    return result


def quantitative_facts(
    company: Mapping[str, Any],
    type_key: str,
    *,
    market_as_of: str | None = None,
) -> list[str]:
    """Return neutral numeric facts already present in the company snapshot.

    ``type_key`` remains part of the call signature for packet-building
    compatibility, but it must not change the facts or appear in their text.
    Candidate status, rule dimensions, and rule scores belong to the separate
    rule context and are never copied into this field.
    """

    # The packet builder still passes the active type for compatibility with
    # older input artifacts.  Neutral facts intentionally do not depend on it.
    del type_key

    facts: list[str] = []
    as_of = str(market_as_of or "").strip()
    valuation: list[str] = []
    price = _fmt(company.get("price"))
    pe = _fmt(company.get("pe"))
    pb = _fmt(company.get("pb"))
    market_cap = _market_cap_yi(company.get("market_cap"))
    if price:
        valuation.append(f"股价 {price} 元")
    pe_number = _number(company.get("pe"))
    if pe_number is not None and pe_number > 0:
        valuation.append(f"PE {pe} 倍")
    elif pe_number is not None:
        valuation.append(f"PE 不适用（原始值 {pe}，盈利口径无有效倍数）")
    if pb:
        valuation.append(f"PB {pb} 倍")
    if market_cap:
        valuation.append(f"市值 {market_cap} 亿元")
    if valuation:
        suffix = f"（交易日 {as_of}）" if as_of else ""
        facts.append("估值快照：" + "；".join(valuation) + suffix)

    end_year = _annual_end_year(company)
    if end_year is not None:
        facts.append(f"年度财务历史覆盖至 {end_year} 年；年度数字不等同于预测")
    if not facts:
        facts.append(f"当前交易日 {as_of or '未标注'}：该公司详情没有可安全展示的量化字段")
    return _unique(facts)


def has_numeric_fact(value: Any) -> bool:
    return isinstance(value, str) and bool(_NUMBER_RE.search(value))
