"""Stable, machine-readable classifications for unavailable valuations.

The detailed reason says *what* failed.  The category says *what the failure
means* to downstream screening: missing evidence is not the same thing as a
model that does not apply to the company's current economics.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DCF_SKIP_SOURCE_MISSING = "source_missing"
DCF_SKIP_MODEL_UNSUPPORTED = "model_unsupported"
DCF_SKIP_ECONOMIC_NOT_APPLICABLE = "economic_not_applicable"
DCF_SKIP_INCONSISTENT_SOURCE = "inconsistent_source"
DCF_SKIP_INTERNAL_ERROR = "internal_error"

DCF_SKIP_CATEGORIES = frozenset(
    {
        DCF_SKIP_SOURCE_MISSING,
        DCF_SKIP_MODEL_UNSUPPORTED,
        DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
        DCF_SKIP_INCONSISTENT_SOURCE,
        DCF_SKIP_INTERNAL_ERROR,
    }
)


def make_dcf_skip_classification(category: str, reason: str) -> dict[str, str]:
    """Build one JSON-safe skip record and reject unstable ad-hoc categories."""
    normalized_category = str(category or "").strip()
    normalized_reason = str(reason or "").strip()
    if normalized_category not in DCF_SKIP_CATEGORIES:
        raise ValueError(f"unknown DCF skip category: {normalized_category}")
    if not normalized_reason:
        raise ValueError("DCF skip reason must not be blank")
    return {"category": normalized_category, "reason": normalized_reason}


def normalize_dcf_skip_classification(value: Any) -> dict[str, str] | None:
    """Validate a serialized skip record without silently inventing fields."""
    if not isinstance(value, Mapping) or set(value) != {"category", "reason"}:
        return None
    try:
        return make_dcf_skip_classification(str(value["category"]), str(value["reason"]))
    except (TypeError, ValueError):
        return None


__all__ = [
    "DCF_SKIP_CATEGORIES",
    "DCF_SKIP_ECONOMIC_NOT_APPLICABLE",
    "DCF_SKIP_INCONSISTENT_SOURCE",
    "DCF_SKIP_INTERNAL_ERROR",
    "DCF_SKIP_MODEL_UNSUPPORTED",
    "DCF_SKIP_SOURCE_MISSING",
    "make_dcf_skip_classification",
    "normalize_dcf_skip_classification",
]
