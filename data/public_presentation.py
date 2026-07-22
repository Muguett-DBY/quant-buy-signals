"""Plain-language presentation helpers for public desktop and mobile views.

Audit payloads intentionally retain machine-readable identifiers.  These
helpers define the separate boundary used by ordinary user-facing text.
"""

from __future__ import annotations

import re
from typing import Any

from data.industry import _INDUSTRY_RULES


_INDUSTRY_CODE_TO_CN = {code: name for code, name, _keywords in _INDUSTRY_RULES}
_CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
_INTERNAL_REASON_IDENTIFIER = re.compile(
    r"""
    (?:
        \bpatch\d+(?:[-_][a-z0-9]+)+\b
        |\b(?:model_id|schema_version|derived_proxy|reported_formula|formula_version
             |validation_status|source_rule|evidence_level)\b
        |\b(?:model|schema|formula|proxy|validation|evidence)\s*=
        |\b(?:[a-z][a-z0-9]*_){2,}[a-z0-9]+\b
        |\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}-v\d+\b
        |\b[a-z_][a-z0-9_]*\s*[-+*/><=]\s*[a-z_(][a-z0-9_(]*
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def public_reason_text(value: Any) -> str:
    """Replace machine identifiers/formulas with a stable Chinese explanation."""
    text = str(value or "").strip()
    lowered = text.casefold()
    if _INTERNAL_REASON_IDENTIFIER.search(text):
        if "type2c" in lowered or "量价" in text:
            return "量价与换手数据"
        return "可核验的财务与行业数据"
    return text


def public_industry_name(value: Any, *, explicit_name: Any = None) -> str:
    """Return a Chinese display name without leaking internal industry enums."""
    supplied = str(explicit_name or "").strip()
    if supplied and _CHINESE_CHARACTER.search(supplied):
        return supplied

    raw = str(value or "").strip()
    if raw == "DEFAULT":
        return "未分类（低置信度）"
    mapped = _INDUSTRY_CODE_TO_CN.get(raw)
    if mapped:
        return mapped
    if raw and _CHINESE_CHARACTER.search(raw):
        return raw
    return "未分类（低置信度）" if raw else "未知"


__all__ = ["public_industry_name", "public_reason_text"]
