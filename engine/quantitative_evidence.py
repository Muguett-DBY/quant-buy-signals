"""Observable, replayable evidence for Patch 6 qualitative dimensions.

Patch 6 contains several economically meaningful dimensions whose labels are
qualitative (moat, runway, technology, business-model innovation, catalyst),
but it also requires every score to be comparable and evidence-backed.  This
module does not pretend that financial statements prove a patent, a brand, or
management integrity.  Instead it measures the *observable economic outcomes*
that those claims must eventually produce: sustained excess returns, stable
and peer-leading margins, cash conversion, listed-peer revenue-share gains,
capital discipline, and operating leverage.

The result is a conservative production fallback.  A dated primary-source
score supplied by a research adapter still takes precedence.  Every formula
here is pure, deterministic, bounded to 0-10, and emits its inputs so an audit
can replay it without trusting the displayed score.
"""

from __future__ import annotations

import copy
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from statistics import median
from datetime import date
from typing import Any


MODEL_ID = "patch6-observable-outcomes-v1"
SOURCE_LABEL = "Eastmoney reported data; Patch6 observable-outcome formula v1"

# ``primary`` is reserved for a dated score supplied by an external research
# adapter.  This module emits the other three levels.  Keeping the level
# separate from the numeric diagnostic is intentional: a score calculated from
# defaults can still be useful for debugging, but must never acquire the same
# authority as a fully observed proxy merely because it is a finite number.
EVIDENCE_LEVELS = ("primary", "derived_proxy", "partial", "missing")

MIN_SECTOR_COMPANIES = 10
MIN_COMPARABLE_COVERAGE = 0.70
RUNWAY_TERMINAL_GROWTH = 0.02
# Object identity, rather than a serializable flag, marks records that passed
# ``data.growth_evidence.validate_growth_evidence_record`` at the production
# boundary.  A JSON/CSV/manual fin_map payload cannot forge this token.
TYPE3_GROWTH_VALIDATION_TOKEN = object()


class _SortedFinitePopulation(list[float]):
    """A JSON-compatible, exact leave-one-out view over sorted finite floats.

    Company contexts are public audit output, so their population fields must
    continue to serialize as ordinary JSON arrays.  Materialising twelve
    almost-identical arrays for every company, however, made a large sector an
    O(n^2) allocation problem before any scoring began.  This list subclass
    keeps one immutable tuple per sector population and maps one excluded
    logical index on demand.  ``json`` uses the overridden iterator, so the
    external JSON is byte-for-byte the same as a materialised list.

    The view preserves normal list mutation semantics by materialising itself
    on the first write.  Internal scoring never mutates a population and can
    therefore use exact O(1) medians/counts and O(log n) threshold breadths.
    """

    __slots__ = ("_base", "_excluded_index", "_materialized")

    def __init__(
        self,
        values: Sequence[float] = (),
        *,
        _base: tuple[float, ...] | None = None,
        _excluded_index: int | None = None,
    ) -> None:
        super().__init__()
        self._base = tuple(values) if _base is None else _base
        self._excluded_index = _excluded_index
        self._materialized = False

    @classmethod
    def from_sorted(cls, values: Sequence[float]) -> _SortedFinitePopulation:
        return cls(_base=tuple(values))

    def without_first(self, target: float | None) -> _SortedFinitePopulation:
        if self._materialized or self._excluded_index is not None:
            # Current production calls exclude only from a sector base.  Keep
            # the general case exact if a future caller excludes from a view.
            values = list(self)
            if target is not None:
                for index, value in enumerate(values):
                    if value == target:
                        values.pop(index)
                        break
            return type(self).from_sorted(values)
        excluded_index = None
        if target is not None:
            candidate = bisect_left(self._base, target)
            if candidate < len(self._base) and self._base[candidate] == target:
                # ``bisect_left`` is exactly the old sorted-list rule: among
                # duplicate equal values, remove only the first one.
                excluded_index = candidate
        return type(self)(
            _base=self._base,
            _excluded_index=excluded_index,
        )

    def _logical_to_base(self, index: int) -> int:
        length = len(self)
        if index < 0:
            index += length
        if index < 0 or index >= length:
            raise IndexError("list index out of range")
        excluded = self._excluded_index
        return index + 1 if excluded is not None and index >= excluded else index

    def _materialize(self) -> None:
        if self._materialized:
            return
        values = tuple(self)
        list.extend(self, values)
        self._base = ()
        self._excluded_index = None
        self._materialized = True

    def median(self) -> float | None:
        if self._materialized:
            return None
        length = len(self)
        if not length:
            return None
        middle = length // 2
        if length % 2:
            return float(self[middle])
        return float((self[middle - 1] + self[middle]) / 2.0)

    def count_at_most(self, threshold: float) -> int:
        if self._materialized:
            return sum(value <= threshold for value in self)
        boundary = bisect_right(self._base, threshold)
        excluded = self._excluded_index
        return boundary - int(excluded is not None and excluded < boundary)

    def count_at_least(self, threshold: float) -> int:
        if self._materialized:
            return sum(value >= threshold for value in self)
        boundary = bisect_left(self._base, threshold)
        excluded = self._excluded_index
        count = len(self._base) - boundary
        return count - int(excluded is not None and excluded >= boundary)

    def __len__(self) -> int:
        if self._materialized:
            return list.__len__(self)
        return len(self._base) - int(self._excluded_index is not None)

    def __iter__(self):  # type: ignore[no-untyped-def]
        if self._materialized:
            return list.__iter__(self)
        excluded = self._excluded_index
        if excluded is None:
            return iter(self._base)
        return (value for index, value in enumerate(self._base) if index != excluded)

    def __reversed__(self):  # type: ignore[no-untyped-def]
        return (self[index] for index in range(len(self) - 1, -1, -1))

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        if self._materialized:
            return list.__getitem__(self, index)
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        if not isinstance(index, int):
            raise TypeError("list indices must be integers or slices")
        return self._base[self._logical_to_base(index)]

    def __contains__(self, value: object) -> bool:
        if self._materialized:
            return list.__contains__(self, value)
        try:
            index = bisect_left(self, value)  # type: ignore[arg-type]
        except TypeError:
            return any(item == value for item in self)
        return index < len(self) and self[index] == value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, list):
            return False
        return len(self) == len(other) and all(left == right for left, right in zip(self, other))

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, list):
            return NotImplemented
        return list(self) < list(other)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, list):
            return NotImplemented
        return list(self) <= list(other)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, list):
            return NotImplemented
        return list(self) > list(other)

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, list):
            return NotImplemented
        return list(self) >= list(other)

    def __repr__(self) -> str:
        return repr(list(self))

    def copy(self) -> list[float]:
        return list(self)

    def __copy__(self):  # type: ignore[no-untyped-def]
        if self._materialized:
            result = type(self)()
            result._materialized = True
            list.extend(result, list.__iter__(self))
            return result
        return type(self)(
            _base=self._base,
            _excluded_index=self._excluded_index,
        )

    def __deepcopy__(self, memo):  # type: ignore[no-untyped-def]
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        if self._materialized:
            result = type(self)()
            result._materialized = True
            memo[id(self)] = result
            list.extend(
                result,
                (copy.deepcopy(value, memo) for value in list.__iter__(self)),
            )
            return result
        result = type(self)(
            _base=copy.deepcopy(self._base, memo),
            _excluded_index=self._excluded_index,
        )
        memo[id(self)] = result
        return result

    def __reduce_ex__(self, protocol: int):  # type: ignore[no-untyped-def]
        del protocol
        return (
            _restore_sorted_finite_population,
            (list(self), self._materialized),
        )

    def __add__(self, other: object):  # type: ignore[no-untyped-def]
        if not isinstance(other, list):
            return NotImplemented
        return list(self) + list(other)

    def __radd__(self, other: object):  # type: ignore[no-untyped-def]
        if not isinstance(other, list):
            return NotImplemented
        return list(other) + list(self)

    def __mul__(self, count: int):
        return list(self) * count

    def __rmul__(self, count: int):
        return count * list(self)

    def count(self, value: object) -> int:
        if self._materialized:
            return list.count(self, value)
        try:
            return bisect_right(self, value) - bisect_left(self, value)  # type: ignore[arg-type]
        except TypeError:
            return sum(item == value for item in self)

    def index(self, value: object, start: int = 0, stop: int | None = None) -> int:
        values = list(self)
        if stop is None:
            return values.index(value, start)
        return values.index(value, start, stop)

    def __setitem__(self, index, value) -> None:  # type: ignore[no-untyped-def]
        self._materialize()
        list.__setitem__(self, index, value)

    def __delitem__(self, index) -> None:  # type: ignore[no-untyped-def]
        self._materialize()
        list.__delitem__(self, index)

    def append(self, value: float) -> None:
        self._materialize()
        list.append(self, value)

    def extend(self, values) -> None:  # type: ignore[no-untyped-def]
        self._materialize()
        list.extend(self, values)

    def insert(self, index: int, value: float) -> None:
        self._materialize()
        list.insert(self, index, value)

    def pop(self, index: int = -1) -> float:
        self._materialize()
        return list.pop(self, index)

    def remove(self, value: float) -> None:
        self._materialize()
        list.remove(self, value)

    def clear(self) -> None:
        self._materialize()
        list.clear(self)

    def reverse(self) -> None:
        self._materialize()
        list.reverse(self)

    def sort(self, *, key=None, reverse: bool = False) -> None:  # type: ignore[no-untyped-def]
        self._materialize()
        list.sort(self, key=key, reverse=reverse)

    def __iadd__(self, values):  # type: ignore[no-untyped-def]
        self._materialize()
        list.__iadd__(self, values)
        return self

    def __imul__(self, count: int):  # type: ignore[no-untyped-def]
        self._materialize()
        list.__imul__(self, count)
        return self


def _restore_sorted_finite_population(
    values: Sequence[float],
    materialized: bool,
) -> _SortedFinitePopulation:
    result = _SortedFinitePopulation.from_sorted(values)
    if materialized:
        result._materialize()
    return result


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, lower: float = 0.0, upper: float = 10.0) -> float:
    return min(upper, max(lower, value))


def _round_score(value: float) -> float:
    return round(_clip(value), 1)


def _linear_score(value: float | None, anchors: Sequence[tuple[float, float]], *, missing: float = 2.0) -> float:
    """Piecewise-linear score through ordered ``(value, score)`` anchors."""
    if value is None:
        return _round_score(missing)
    # All formula tables are source-controlled in ascending order.  Re-sorting
    # the same anchors tens of thousands of times dominated full-market
    # scoring without adding any runtime safety.
    ordered = [(float(x), float(y)) for x, y in anchors]
    if value <= ordered[0][0]:
        return _round_score(ordered[0][1])
    if value >= ordered[-1][0]:
        return _round_score(ordered[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            if right_x == left_x:
                return _round_score(right_y)
            weight = (value - left_x) / (right_x - left_x)
            return _round_score(left_y + weight * (right_y - left_y))
    return _round_score(missing)


def _finite_sequence_count(value: Any) -> int:
    if isinstance(value, _SortedFinitePopulation) and not value._materialized:
        return len(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return 0
    return sum(_finite(item) is not None for item in value)


def _complete_status(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("status") == "complete"


def _external_growth_proxy_inputs(value: Any) -> dict[str, float] | None:
    """Return only a reproducible acquisition/goodwill proxy contract.

    A bare ``status=complete`` is deliberately insufficient.  Type 3's growth
    quality cannot be unlocked by metadata alone.  The automatic adapter must
    provide a clearly labelled five-year aggregate cash/goodwill proxy.  Exact
    acquisition-revenue attribution belongs in a separately validated primary
    score; this fallback never accepts an arbitrary transaction list as proof.
    """

    if not isinstance(value, Mapping) or value.get("status") != "complete":
        return None
    if value.get("contract_scope") != "aggregate_proxy_not_transaction_census":
        return None
    coverage_years = value.get("coverage_year_count")
    if isinstance(coverage_years, bool) or not isinstance(coverage_years, int):
        return None
    coverage_count = coverage_years
    raw_records = value.get("records")
    if (
        coverage_count < 5
        or not isinstance(raw_records, Sequence)
        or isinstance(raw_records, (str, bytes))
        or len(raw_records) != coverage_count
        or not all(isinstance(item, Mapping) for item in raw_records)
    ):
        return None
    years: list[int] = []
    revenues: list[float] = []
    goodwill: list[float] = []
    acquisitions: list[float] = []
    for record in raw_records:
        raw_year = record.get("year")
        revenue = _finite(record.get("revenue"))
        goodwill_value = _finite(record.get("goodwill"))
        acquisition = _finite(record.get("acquisition_cash"))
        if (
            isinstance(raw_year, bool)
            or not isinstance(raw_year, int)
            or revenue is None
            or revenue <= 0
            or goodwill_value is None
            or goodwill_value < 0
            or acquisition is None
            or acquisition < 0
        ):
            return None
        years.append(raw_year)
        revenues.append(revenue)
        goodwill.append(goodwill_value)
        acquisitions.append(acquisition)
    raw_as_of = value.get("as_of")
    try:
        as_of = date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None
    except ValueError:
        as_of = None
    if (
        years != sorted(set(years))
        or any(current - previous != 1 for previous, current in zip(years, years[1:]))
        or as_of is None
        or years[-1] > as_of.year
    ):
        return None
    acquisition_cash_ratio = sum(acquisitions) / sum(revenues)
    goodwill_ratio = goodwill[-1] / revenues[-1]
    goodwill_change_ratio = sum(
        max(current - previous, 0.0) for previous, current in zip(goodwill, goodwill[1:])
    ) / sum(revenues[1:])
    reported_acquisition_ratio = _finite(value.get("aggregate_acquisition_cash_to_revenue"))
    reported_goodwill_ratio = _finite(value.get("goodwill_to_revenue_latest"))
    reported_goodwill_additions = _finite(value.get("positive_goodwill_additions_to_revenue"))
    if (
        reported_acquisition_ratio is None
        or reported_goodwill_ratio is None
        or reported_goodwill_additions is None
        or not math.isclose(reported_acquisition_ratio, acquisition_cash_ratio, rel_tol=1e-10, abs_tol=1e-12)
        or not math.isclose(reported_goodwill_ratio, goodwill_ratio, rel_tol=1e-10, abs_tol=1e-12)
        or not math.isclose(reported_goodwill_additions, goodwill_change_ratio, rel_tol=1e-10, abs_tol=1e-12)
    ):
        return None
    return {
        "acquisition_intensity": acquisition_cash_ratio,
        "goodwill_to_revenue_latest": goodwill_ratio,
        "goodwill_change_to_revenue": goodwill_change_ratio,
    }


def _segment_growth_proxy_inputs(value: Any) -> dict[str, float] | None:
    """Return a strict, dated segment-growth summary suitable for Type 3."""

    if not isinstance(value, Mapping) or value.get("status") != "complete":
        return None
    raw_years = value.get("history_years")
    if not isinstance(raw_years, Sequence) or isinstance(raw_years, (str, bytes)):
        return None
    years: list[int] = []
    for raw_year in raw_years:
        if isinstance(raw_year, bool):
            return None
        try:
            year = int(raw_year)
        except (TypeError, ValueError, OverflowError):
            return None
        if not 1900 <= year <= 9999:
            return None
        years.append(year)
    raw_as_of = value.get("as_of")
    try:
        as_of = date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None
    except ValueError:
        as_of = None
    if (
        len(years) < 3
        or years != sorted(set(years))
        or any(current - previous != 1 for previous, current in zip(years, years[1:]))
        or as_of is None
        or years[-1] > as_of.year
    ):
        return None
    raw_source_count = value.get("growth_source_count")
    if isinstance(raw_source_count, bool) or not isinstance(raw_source_count, int):
        return None
    effective_source_count = _finite(value.get("effective_growth_source_count"))
    positive_growth_share = _finite(value.get("positive_growth_share"))
    revenue_hhi = _finite(value.get("revenue_hhi"))
    matched_latest_share = _finite(value.get("matched_latest_share"))
    raw_segments = value.get("segments")
    if (
        raw_source_count < 0
        or effective_source_count is None
        or not 0 <= effective_source_count <= max(raw_source_count, 1)
        or positive_growth_share is None
        or not 0 <= positive_growth_share <= 1
        or revenue_hhi is None
        or not 0 <= revenue_hhi <= 1
        or matched_latest_share is None
        or not 0.95 <= matched_latest_share <= 1
        or not isinstance(raw_segments, Sequence)
        or isinstance(raw_segments, (str, bytes))
        or not raw_segments
        or not all(isinstance(item, Mapping) for item in raw_segments)
    ):
        return None
    segment_shares: list[float] = []
    growing_share = 0.0
    reproduced_count = 0
    growth_contributions: list[float] = []
    reproduced_matched_share = 0.0
    for segment in raw_segments:
        share = _finite(segment.get("latest_revenue_share"))
        cagr = _finite(segment.get("cagr"))
        first_revenue = _finite(segment.get("first_revenue"))
        latest_revenue = _finite(segment.get("latest_revenue"))
        first_year = segment.get("first_year")
        latest_year = segment.get("latest_year")
        if (
            share is None
            or not 0 <= share <= 1
            or latest_revenue is None
            or latest_revenue < 0
            or isinstance(latest_year, bool)
            or not isinstance(latest_year, int)
            or latest_year != years[-1]
            or (first_year is not None and (isinstance(first_year, bool) or not isinstance(first_year, int)))
        ):
            return None
        segment_shares.append(share)
        if first_year is not None:
            reproduced_matched_share += share
        if cagr is not None and cagr > 0:
            reproduced_count += 1
            growing_share += share
        if first_revenue is not None and first_revenue > 0 and latest_revenue > first_revenue:
            growth_contributions.append(latest_revenue - first_revenue)
    reproduced_hhi = sum(share * share for share in segment_shares)
    total_contribution = sum(growth_contributions)
    reproduced_effective_count = (
        1.0 / sum((contribution / total_contribution) ** 2 for contribution in growth_contributions)
        if total_contribution > 0
        else 0.0
    )
    if (
        not math.isclose(sum(segment_shares), 1.0, rel_tol=0.0, abs_tol=1e-8)
        or raw_source_count != reproduced_count
        or not math.isclose(effective_source_count, reproduced_effective_count, rel_tol=1e-10, abs_tol=1e-12)
        or not math.isclose(positive_growth_share, growing_share, rel_tol=1e-10, abs_tol=1e-12)
        or not math.isclose(revenue_hhi, reproduced_hhi, rel_tol=1e-10, abs_tol=1e-12)
        or not math.isclose(matched_latest_share, reproduced_matched_share, rel_tol=1e-10, abs_tol=1e-12)
    ):
        return None
    return {
        "history_years": float(len(years)),
        "growth_source_count": effective_source_count,
        "raw_growth_source_count": float(raw_source_count),
        "positive_growth_share": positive_growth_share,
        "revenue_hhi": revenue_hhi,
        "matched_latest_share": matched_latest_share,
    }


def _quality_record(
    inputs: Mapping[str, bool],
    *,
    core_available: bool | None = None,
) -> dict[str, Any]:
    """Classify the authority of a derived score from explicit raw inputs.

    ``derived_proxy`` means every declared input for the observable proxy is
    present; it does *not* upgrade the proxy into proof of a qualitative claim.
    ``partial`` keeps a replayable diagnostic when only part of the formula is
    observed.  ``missing`` means the score has no usable core evidence and must
    not cross the production scoring boundary.
    """

    required = list(inputs)
    available = [name for name, present in inputs.items() if present]
    missing = [name for name, present in inputs.items() if not present]
    has_core = bool(available) if core_available is None else bool(core_available)
    if not has_core:
        level = "missing"
    elif not missing:
        level = "derived_proxy"
    else:
        level = "partial"
    return {
        "level": level,
        "input_coverage": round(len(available) / len(required), 3) if required else 0.0,
        "required_inputs": required,
        "available_inputs": available,
        "missing_inputs": missing,
    }


def _series(metric: Mapping[str, Any], values_key: str, years_key: str) -> dict[int, float]:
    cache_key = f"{values_key}|{years_key}"
    cache = metric.get("_quant_series_cache")
    if isinstance(cache, Mapping) and isinstance(cache.get(cache_key), dict):
        return cache[cache_key]  # type: ignore[return-value]
    values = metric.get(values_key)
    years = metric.get(years_key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return {}
    if not isinstance(years, Sequence) or isinstance(years, (str, bytes)) or len(values) != len(years):
        return {}
    result: dict[int, float] = {}
    for raw_year, raw_value in zip(years, values):
        if isinstance(raw_year, bool):
            continue
        try:
            year = int(raw_year)
        except (TypeError, ValueError, OverflowError):
            continue
        value = _finite(raw_value)
        if value is not None:
            result[year] = value
    if isinstance(cache, dict):
        cache[cache_key] = result
    return result


def _latest_consecutive_years(years: Collection[int]) -> list[int]:
    """Return the fully consecutive suffix ending at the newest valid year."""

    ordered = sorted(set(years))
    if not ordered:
        return []
    start = len(ordered) - 1
    while start > 0:
        if ordered[start] - ordered[start - 1] != 1:
            break
        start -= 1
    return ordered[start:]


def _latest_consecutive_year_count(years: Collection[int]) -> int:
    return len(_latest_consecutive_years(years))


def _cagr(first: float | None, last: float | None, elapsed: int) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or elapsed <= 0:
        return None
    value = (last / first) ** (1.0 / elapsed) - 1.0
    return value if math.isfinite(value) else None


def _growth_rates(series: Mapping[int, float]) -> list[float]:
    years = sorted(series)
    rates: list[float] = []
    for prior, current in zip(years, years[1:]):
        prior_value = series[prior]
        current_value = series[current]
        if current - prior == 1 and prior_value > 0:
            rates.append(current_value / prior_value - 1.0)
    return rates


def _positive_share(values: Sequence[Any]) -> float | None:
    clean = [value for value in (_finite(item) for item in values) if value is not None]
    return sum(value > 0 for value in clean) / len(clean) if clean else None


def _percentile_rank(value: float | None, population: Sequence[float]) -> float | None:
    clean: Sequence[float]
    if isinstance(population, _SortedFinitePopulation) and not population._materialized:
        clean = population
    else:
        clean = [number for number in population if math.isfinite(number)]
    if value is None or len(clean) < MIN_SECTOR_COMPANIES:
        return None
    if not isinstance(clean, _SortedFinitePopulation) and any(left > right for left, right in zip(clean, clean[1:])):
        clean.sort()
    lower = bisect_left(clean, value)
    upper = bisect_right(clean, value)
    return (lower + 0.5 * (upper - lower)) / len(clean)


def _cohort_aggregate(
    members: Sequence[Mapping[str, Any]],
    values_key: str,
    years_key: str,
    *,
    window: int,
    require_positive: bool,
) -> dict[str, Any]:
    histories = {str(member.get("code")): _series(member, values_key, years_key) for member in members}
    all_years = sorted({year for history in histories.values() for year in history})
    if len(all_years) < window:
        return {"available": False, "reason": "insufficient_years"}
    selected_years = all_years[-window:]
    if any(current - prior != 1 for prior, current in zip(selected_years, selected_years[1:])):
        return {"available": False, "reason": "nonconsecutive_years"}
    cohort: list[str] = []
    for code, history in histories.items():
        values = [history.get(year) for year in selected_years]
        if any(value is None for value in values):
            continue
        if require_positive and any(float(value) <= 0 for value in values if value is not None):
            continue
        if not require_positive and any(float(value) < 0 for value in values if value is not None):
            continue
        cohort.append(code)
    coverage = len(cohort) / max(len(members), 1)
    aggregates = {year: math.fsum(histories[code][year] for code in cohort) for year in selected_years}
    growth = _cagr(aggregates[selected_years[0]], aggregates[selected_years[-1]], window - 1) if cohort else None
    if len(cohort) < MIN_SECTOR_COMPANIES or coverage < MIN_COMPARABLE_COVERAGE:
        return {
            "available": False,
            "reason": "insufficient_comparable_cohort",
            "cohort_count": len(cohort),
            "population_count": len(members),
            "coverage": coverage,
            "years": selected_years,
            "aggregates": aggregates,
            "cagr": growth,
            "cohort_codes": cohort,
        }
    return {
        "available": growth is not None,
        "reason": "available" if growth is not None else "nonpositive_aggregate",
        "cohort_count": len(cohort),
        "population_count": len(members),
        "coverage": coverage,
        "years": selected_years,
        "aggregates": aggregates,
        "cagr": growth,
        "cohort_codes": cohort,
    }


def _cohort_ratio_aggregate(
    members: Sequence[Mapping[str, Any]],
    numerator_values_key: str,
    numerator_years_key: str,
    denominator_values_key: str,
    denominator_years_key: str,
    *,
    window: int,
    numerator_nonnegative: bool,
) -> dict[str, Any]:
    """Aggregate a ratio on one fixed, comparable peer cohort."""
    numerators = {
        str(member.get("code")): _series(member, numerator_values_key, numerator_years_key) for member in members
    }
    denominators = {
        str(member.get("code")): _series(member, denominator_values_key, denominator_years_key) for member in members
    }
    all_years = sorted(
        {year for code in numerators for year in set(numerators[code]) & set(denominators.get(code, {}))}
    )
    if len(all_years) < window:
        return {"available": False, "reason": "insufficient_years"}
    selected_years = all_years[-window:]
    if any(current - prior != 1 for prior, current in zip(selected_years, selected_years[1:])):
        return {"available": False, "reason": "nonconsecutive_years"}
    cohort: list[str] = []
    for code, numerator_history in numerators.items():
        denominator_history = denominators.get(code, {})
        numerator_values = [numerator_history.get(year) for year in selected_years]
        denominator_values = [denominator_history.get(year) for year in selected_years]
        if any(value is None for value in numerator_values + denominator_values):
            continue
        if numerator_nonnegative and any(float(value) < 0 for value in numerator_values if value is not None):
            continue
        if any(float(value) <= 0 for value in denominator_values if value is not None):
            continue
        cohort.append(code)
    coverage = len(cohort) / max(len(members), 1)
    numerator_aggregates = {year: math.fsum(numerators[code][year] for code in cohort) for year in selected_years}
    denominator_aggregates = {year: math.fsum(denominators[code][year] for code in cohort) for year in selected_years}
    ratios = {
        year: numerator_aggregates[year] / denominator_aggregates[year]
        for year in selected_years
        if denominator_aggregates[year] > 0
    }
    if len(cohort) < MIN_SECTOR_COMPANIES or coverage < MIN_COMPARABLE_COVERAGE:
        return {
            "available": False,
            "reason": "insufficient_comparable_cohort",
            "cohort_count": len(cohort),
            "population_count": len(members),
            "coverage": coverage,
            "years": selected_years,
            "numerator_aggregates": numerator_aggregates,
            "denominator_aggregates": denominator_aggregates,
            "ratios": ratios,
            "cohort_codes": cohort,
        }
    return {
        "available": True,
        "reason": "available",
        "cohort_count": len(cohort),
        "population_count": len(members),
        "coverage": coverage,
        "years": selected_years,
        "numerator_aggregates": numerator_aggregates,
        "denominator_aggregates": denominator_aggregates,
        "ratios": ratios,
        "cohort_codes": cohort,
    }


def _median(values: Sequence[Any]) -> float | None:
    if isinstance(values, _SortedFinitePopulation) and not values._materialized:
        return values.median()
    clean = [value for value in (_finite(item) for item in values) if value is not None]
    if not clean:
        return None
    if any(left > right for left, right in zip(clean, clean[1:])):
        clean.sort()
    middle = len(clean) // 2
    if len(clean) % 2:
        return float(clean[middle])
    return float((clean[middle - 1] + clean[middle]) / 2.0)


def _sector_price_context(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    returns_60d: list[float] = []
    returns_ytd: list[float] = []
    for member in members:
        components = member.get("market_coldness_components")
        raw = components.get("raw_values") if isinstance(components, Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        value_60d = _finite(raw.get("change_60d_pct"))
        value_ytd = _finite(raw.get("change_ytd_pct"))
        if value_60d is not None:
            returns_60d.append(value_60d)
        if value_ytd is not None:
            returns_ytd.append(value_ytd)
    coverage = len(returns_60d) / max(len(members), 1)
    returns_60d.sort()
    returns_ytd.sort()
    sorted_60d = _SortedFinitePopulation.from_sorted(returns_60d)
    sorted_ytd = _SortedFinitePopulation.from_sorted(returns_ytd)
    return {
        "sample_count": len(sorted_60d),
        "coverage": coverage,
        "returns_60d_population": sorted_60d,
        "returns_ytd_population": sorted_ytd,
        "median_60d_pct": _median(sorted_60d),
        "median_ytd_pct": _median(sorted_ytd),
        "hot_breadth_60d": (sorted_60d.count_at_least(30.0) / len(sorted_60d) if sorted_60d else None),
        "cold_breadth_60d": (sorted_60d.count_at_most(-20.0) / len(sorted_60d) if sorted_60d else None),
    }


def build_sector_context(metrics: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build comparable-cohort sector aggregates from the complete universe."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for metric in metrics:
        grouped[str(metric.get("industry") or "DEFAULT")].append(metric)

    contexts: dict[str, dict[str, Any]] = {}
    for industry, members in grouped.items():
        revenue = _cohort_aggregate(
            members,
            "revenue_values",
            "revenue_years",
            window=3,
            require_positive=True,
        )
        capex = _cohort_aggregate(
            members,
            "capex_history",
            "capex_years",
            window=3,
            require_positive=False,
        )
        assets = _cohort_aggregate(
            members,
            "total_assets_history",
            "total_assets_years",
            window=3,
            require_positive=True,
        )
        capex_intensity = _cohort_ratio_aggregate(
            members,
            "capex_history",
            "capex_years",
            "revenue_values",
            "revenue_years",
            window=4,
            numerator_nonnegative=True,
        )
        profit_margin = _cohort_ratio_aggregate(
            members,
            "net_profit_history",
            "net_profit_years",
            "revenue_values",
            "revenue_years",
            window=3,
            numerator_nonnegative=False,
        )
        gross_values = sorted(
            value for value in (_finite(member.get("gross_margin")) for member in members) if value is not None
        )
        gross_medians = sorted(
            value
            for value in (_median(member.get("gross_margin_history", [])) for member in members)
            if value is not None
        )
        roic_values = sorted(
            value for value in (_finite(member.get("roic")) for member in members) if value is not None
        )
        margin_trajectories = sorted(
            value for value in (_finite(member.get("margin_trajectory")) for member in members) if value is not None
        )
        rd_intensities = sorted(
            value for value in (_finite(member.get("rd_intensity")) for member in members) if value is not None
        )
        profit_values = sorted(
            value for value in (_finite(member.get("net_profit")) for member in members) if value is not None
        )
        fcf_values = sorted(
            value for value in (_finite(member.get("free_cash_flow")) for member in members) if value is not None
        )
        latest_revenues = sorted(
            value
            for value in (_finite(member.get("revenue_latest")) for member in members)
            if value is not None and value > 0
        )
        latest_revenue_growth = sorted(
            rates[-1]
            for member in members
            if (rates := _growth_rates(_series(member, "revenue_values", "revenue_years")))
        )
        latest_profit_growth = sorted(
            rates[-1]
            for member in members
            if (rates := _growth_rates(_series(member, "net_profit_history", "net_profit_years")))
        )
        interim_revenue_growth = sorted(
            value for value in (_finite(member.get("interim_revenue_yoy")) for member in members) if value is not None
        )
        interim_profit_growth = sorted(
            value for value in (_finite(member.get("interim_profit_yoy")) for member in members) if value is not None
        )
        gross_values = _SortedFinitePopulation.from_sorted(gross_values)
        gross_medians = _SortedFinitePopulation.from_sorted(gross_medians)
        roic_values = _SortedFinitePopulation.from_sorted(roic_values)
        margin_trajectories = _SortedFinitePopulation.from_sorted(margin_trajectories)
        rd_intensities = _SortedFinitePopulation.from_sorted(rd_intensities)
        profit_values = _SortedFinitePopulation.from_sorted(profit_values)
        fcf_values = _SortedFinitePopulation.from_sorted(fcf_values)
        latest_revenues = _SortedFinitePopulation.from_sorted(latest_revenues)
        latest_revenue_growth = _SortedFinitePopulation.from_sorted(latest_revenue_growth)
        latest_profit_growth = _SortedFinitePopulation.from_sorted(latest_profit_growth)
        interim_revenue_growth = _SortedFinitePopulation.from_sorted(interim_revenue_growth)
        interim_profit_growth = _SortedFinitePopulation.from_sorted(interim_profit_growth)
        total_revenue = math.fsum(latest_revenues) if latest_revenues else None
        revenue_shares = [value / total_revenue for value in latest_revenues] if total_revenue else []
        contexts[industry] = {
            "population_count": len(members),
            "peer_count": len(members),
            "revenue": revenue,
            "capex": capex,
            "assets": assets,
            "capex_intensity": capex_intensity,
            "profit_margin": profit_margin,
            "aggregate_revenue_cagr": _finite(revenue.get("cagr")),
            "aggregate_revenue_cagr_count": int(revenue.get("cohort_count") or 0),
            "aggregate_revenue_coverage": _finite(revenue.get("coverage")),
            "aggregate_capex_cagr": _finite(capex.get("cagr")),
            "aggregate_capex_cagr_count": int(capex.get("cohort_count") or 0),
            "aggregate_capex_coverage": _finite(capex.get("coverage")),
            "aggregate_assets_cagr": _finite(assets.get("cagr")),
            "aggregate_assets_cagr_count": int(assets.get("cohort_count") or 0),
            "aggregate_assets_coverage": _finite(assets.get("coverage")),
            "gross_margin_population": gross_values,
            "gross_margin_median_population": gross_medians,
            "roic_population": roic_values,
            "rd_intensity_population": rd_intensities,
            "revenue_latest_population": latest_revenues,
            "median_gross_margin": _median(gross_values),
            "median_roic": _median(roic_values),
            "median_margin_trajectory": _median(margin_trajectories),
            "margin_trajectory_population": margin_trajectories,
            "median_annual_revenue_growth": _median(latest_revenue_growth),
            "median_annual_profit_growth": _median(latest_profit_growth),
            "median_interim_revenue_yoy": _median(interim_revenue_growth),
            "median_interim_profit_yoy": _median(interim_profit_growth),
            "annual_revenue_growth_population": latest_revenue_growth,
            "annual_profit_growth_population": latest_profit_growth,
            "interim_revenue_yoy_population": interim_revenue_growth,
            "interim_profit_yoy_population": interim_profit_growth,
            "profit_population": profit_values,
            "fcf_population": fcf_values,
            "loss_share": (sum(value <= 0 for value in profit_values) / len(profit_values) if profit_values else None),
            "negative_fcf_share": (sum(value <= 0 for value in fcf_values) / len(fcf_values) if fcf_values else None),
            "listed_revenue_hhi": math.fsum(share * share for share in revenue_shares) if revenue_shares else None,
            "listed_revenue_top3_share": sum(sorted(revenue_shares, reverse=True)[:3]) if revenue_shares else None,
            "price": _sector_price_context(members),
        }
    return contexts


def _without_one(values: Sequence[float], target: float | None) -> list[float]:
    if isinstance(values, _SortedFinitePopulation):
        return values.without_first(target)
    result = list(values)
    if target is None:
        return result
    for index, value in enumerate(result):
        if value == target:
            result.pop(index)
            break
    return result


def _exclude_from_aggregate(
    aggregate: Mapping[str, Any],
    target: Mapping[str, Any],
    values_key: str,
    years_key: str,
    *,
    peer_count: int,
) -> dict[str, Any]:
    result = dict(aggregate)
    years = aggregate.get("years")
    cohort_codes = aggregate.get("cohort_codes")
    aggregates = aggregate.get("aggregates")
    if (
        not isinstance(years, Sequence)
        or isinstance(years, (str, bytes))
        or not isinstance(cohort_codes, Sequence)
        or isinstance(cohort_codes, (str, bytes))
        or not isinstance(aggregates, Mapping)
    ):
        result["population_count"] = peer_count
        result["available"] = False
        return result
    code = str(target.get("code") or "")
    cohort = [str(item) for item in cohort_codes]
    totals = {int(year): _finite(aggregates.get(year)) for year in years}
    if code in cohort:
        history = _series(target, values_key, years_key)
        if all(int(year) in history and totals.get(int(year)) is not None for year in years):
            totals = {
                int(year): float(totals[int(year)]) - history[int(year)]  # type: ignore[arg-type]
                for year in years
            }
            cohort.remove(code)
    coverage = len(cohort) / max(peer_count, 1)
    elapsed = int(years[-1]) - int(years[0]) if len(years) >= 2 else 0
    growth = _cagr(totals.get(int(years[0])), totals.get(int(years[-1])), elapsed)
    available = bool(len(cohort) >= MIN_SECTOR_COMPANIES and coverage >= MIN_COMPARABLE_COVERAGE and growth is not None)
    result.update(
        {
            "available": available,
            "reason": "available" if available else "insufficient_comparable_cohort",
            "cohort_count": len(cohort),
            "population_count": peer_count,
            "coverage": coverage,
            "aggregates": totals,
            "cagr": growth,
            "cohort_codes": cohort,
        }
    )
    return result


def _exclude_from_ratio_aggregate(
    aggregate: Mapping[str, Any],
    target: Mapping[str, Any],
    numerator_values_key: str,
    numerator_years_key: str,
    denominator_values_key: str,
    denominator_years_key: str,
    *,
    peer_count: int,
) -> dict[str, Any]:
    result = dict(aggregate)
    years = aggregate.get("years")
    cohort_codes = aggregate.get("cohort_codes")
    numerators = aggregate.get("numerator_aggregates")
    denominators = aggregate.get("denominator_aggregates")
    if (
        not isinstance(years, Sequence)
        or isinstance(years, (str, bytes))
        or not isinstance(cohort_codes, Sequence)
        or isinstance(cohort_codes, (str, bytes))
        or not isinstance(numerators, Mapping)
        or not isinstance(denominators, Mapping)
    ):
        result["population_count"] = peer_count
        result["available"] = False
        return result
    code = str(target.get("code") or "")
    cohort = [str(item) for item in cohort_codes]
    numerator_totals = {int(year): _finite(numerators.get(year)) for year in years}
    denominator_totals = {int(year): _finite(denominators.get(year)) for year in years}
    if code in cohort:
        numerator_history = _series(target, numerator_values_key, numerator_years_key)
        denominator_history = _series(target, denominator_values_key, denominator_years_key)
        if all(
            int(year) in numerator_history
            and int(year) in denominator_history
            and numerator_totals.get(int(year)) is not None
            and denominator_totals.get(int(year)) is not None
            for year in years
        ):
            numerator_totals = {
                int(year): float(numerator_totals[int(year)]) - numerator_history[int(year)]  # type: ignore[arg-type]
                for year in years
            }
            denominator_totals = {
                int(year): float(denominator_totals[int(year)]) - denominator_history[int(year)]  # type: ignore[arg-type]
                for year in years
            }
            cohort.remove(code)
    ratios = {
        int(year): float(numerator_totals[int(year)]) / float(denominator_totals[int(year)])
        for year in years
        if numerator_totals.get(int(year)) is not None
        and denominator_totals.get(int(year)) is not None
        and float(denominator_totals[int(year)]) > 0  # type: ignore[arg-type]
    }
    coverage = len(cohort) / max(peer_count, 1)
    available = bool(
        len(cohort) >= MIN_SECTOR_COMPANIES and coverage >= MIN_COMPARABLE_COVERAGE and len(ratios) == len(years)
    )
    result.update(
        {
            "available": available,
            "reason": "available" if available else "insufficient_comparable_cohort",
            "cohort_count": len(cohort),
            "population_count": peer_count,
            "coverage": coverage,
            "numerator_aggregates": numerator_totals,
            "denominator_aggregates": denominator_totals,
            "ratios": ratios,
            "cohort_codes": cohort,
        }
    )
    return result


def _target_market_value(target: Mapping[str, Any], key: str) -> float | None:
    components = target.get("market_coldness_components")
    raw = components.get("raw_values") if isinstance(components, Mapping) else None
    return _finite(raw.get(key)) if isinstance(raw, Mapping) else None


def _price_context_without_target(
    price: Mapping[str, Any], target: Mapping[str, Any], *, peer_count: int
) -> dict[str, Any]:
    returns_60d = _without_one(price.get("returns_60d_population", []), _target_market_value(target, "change_60d_pct"))
    returns_ytd = _without_one(price.get("returns_ytd_population", []), _target_market_value(target, "change_ytd_pct"))
    hot_count = (
        returns_60d.count_at_least(30.0)
        if isinstance(returns_60d, _SortedFinitePopulation)
        else sum(value >= 30.0 for value in returns_60d)
    )
    cold_count = (
        returns_60d.count_at_most(-20.0)
        if isinstance(returns_60d, _SortedFinitePopulation)
        else sum(value <= -20.0 for value in returns_60d)
    )
    return {
        "sample_count": len(returns_60d),
        "coverage": len(returns_60d) / max(peer_count, 1),
        "returns_60d_population": returns_60d,
        "returns_ytd_population": returns_ytd,
        "median_60d_pct": _median(returns_60d),
        "median_ytd_pct": _median(returns_ytd),
        "hot_breadth_60d": (hot_count / len(returns_60d) if returns_60d else None),
        "cold_breadth_60d": (cold_count / len(returns_60d) if returns_60d else None),
    }


def _context_without_target(base: Mapping[str, Any], target: Mapping[str, Any], *, peer_count: int) -> dict[str, Any]:
    context = dict(base)
    revenue = _exclude_from_aggregate(
        base.get("revenue", {}), target, "revenue_values", "revenue_years", peer_count=peer_count
    )
    capex = _exclude_from_aggregate(
        base.get("capex", {}), target, "capex_history", "capex_years", peer_count=peer_count
    )
    assets = _exclude_from_aggregate(
        base.get("assets", {}), target, "total_assets_history", "total_assets_years", peer_count=peer_count
    )
    capex_intensity = _exclude_from_ratio_aggregate(
        base.get("capex_intensity", {}),
        target,
        "capex_history",
        "capex_years",
        "revenue_values",
        "revenue_years",
        peer_count=peer_count,
    )
    profit_margin = _exclude_from_ratio_aggregate(
        base.get("profit_margin", {}),
        target,
        "net_profit_history",
        "net_profit_years",
        "revenue_values",
        "revenue_years",
        peer_count=peer_count,
    )
    population_specs = {
        "gross_margin_population": _finite(target.get("gross_margin")),
        "gross_margin_median_population": _median(target.get("gross_margin_history", [])),
        "roic_population": _finite(target.get("roic")),
        "rd_intensity_population": _finite(target.get("rd_intensity")),
        "revenue_latest_population": _finite(target.get("revenue_latest")),
        "margin_trajectory_population": _finite(target.get("margin_trajectory")),
        "annual_revenue_growth_population": (
            rates[-1] if (rates := _growth_rates(_series(target, "revenue_values", "revenue_years"))) else None
        ),
        "annual_profit_growth_population": (
            rates[-1] if (rates := _growth_rates(_series(target, "net_profit_history", "net_profit_years"))) else None
        ),
        "interim_revenue_yoy_population": _finite(target.get("interim_revenue_yoy")),
        "interim_profit_yoy_population": _finite(target.get("interim_profit_yoy")),
        "profit_population": _finite(target.get("net_profit")),
        "fcf_population": _finite(target.get("free_cash_flow")),
    }
    for key, target_value in population_specs.items():
        context[key] = _without_one(base.get(key, []), target_value)
    latest_revenues = context["revenue_latest_population"]
    total_revenue = math.fsum(latest_revenues) if latest_revenues else None
    revenue_shares = [value / total_revenue for value in latest_revenues] if total_revenue else []
    profit_values = context["profit_population"]
    fcf_values = context["fcf_population"]
    loss_count = (
        profit_values.count_at_most(0.0)
        if isinstance(profit_values, _SortedFinitePopulation)
        else sum(value <= 0 for value in profit_values)
    )
    negative_fcf_count = (
        fcf_values.count_at_most(0.0)
        if isinstance(fcf_values, _SortedFinitePopulation)
        else sum(value <= 0 for value in fcf_values)
    )
    context.update(
        {
            "population_count": peer_count,
            "peer_count": peer_count,
            "revenue": revenue,
            "capex": capex,
            "assets": assets,
            "capex_intensity": capex_intensity,
            "profit_margin": profit_margin,
            "aggregate_revenue_cagr": _finite(revenue.get("cagr")) if revenue.get("available") else None,
            "aggregate_revenue_cagr_count": int(revenue.get("cohort_count") or 0),
            "aggregate_revenue_coverage": _finite(revenue.get("coverage")),
            "aggregate_capex_cagr": _finite(capex.get("cagr")) if capex.get("available") else None,
            "aggregate_capex_cagr_count": int(capex.get("cohort_count") or 0),
            "aggregate_capex_coverage": _finite(capex.get("coverage")),
            "aggregate_assets_cagr": _finite(assets.get("cagr")) if assets.get("available") else None,
            "aggregate_assets_cagr_count": int(assets.get("cohort_count") or 0),
            "aggregate_assets_coverage": _finite(assets.get("coverage")),
            "median_gross_margin": _median(context["gross_margin_population"]),
            "median_roic": _median(context["roic_population"]),
            "median_margin_trajectory": _median(context["margin_trajectory_population"]),
            "median_annual_revenue_growth": _median(context["annual_revenue_growth_population"]),
            "median_annual_profit_growth": _median(context["annual_profit_growth_population"]),
            "median_interim_revenue_yoy": _median(context["interim_revenue_yoy_population"]),
            "median_interim_profit_yoy": _median(context["interim_profit_yoy_population"]),
            "loss_share": (loss_count / len(profit_values) if profit_values else None),
            "negative_fcf_share": (negative_fcf_count / len(fcf_values) if fcf_values else None),
            "listed_revenue_hhi": (math.fsum(share * share for share in revenue_shares) if revenue_shares else None),
            "listed_revenue_top3_share": (sum(sorted(revenue_shares, reverse=True)[:3]) if revenue_shares else None),
            "price": _price_context_without_target(base.get("price", {}), target, peer_count=peer_count),
        }
    )
    return context


def build_company_contexts(
    metrics: Sequence[Mapping[str, Any]],
    *,
    target_codes: Collection[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build exact leave-one-out peer contexts with one aggregation per sector.

    ``target_codes`` is a projection only: every sector base still uses the
    complete metric universe, but leave-one-out materialization is limited to
    targets that will actually be scored.  This keeps fixed-sample audits
    mathematically identical to production without deriving thousands of
    unused company contexts.
    """
    selected = None if target_codes is None else {str(code) for code in target_codes}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen_codes: set[str] = set()
    for metric in metrics:
        if isinstance(metric, dict):
            metric.setdefault("_quant_series_cache", {})
        code = str(metric.get("code") or "")
        if not code or code in seen_codes:
            raise ValueError(f"quantitative peer context requires unique non-empty code:{code}")
        seen_codes.add(code)
        grouped[str(metric.get("industry") or "DEFAULT")].append(metric)
    contexts: dict[str, dict[str, Any]] = {}
    for industry, members in grouped.items():
        base = build_sector_context(members).get(industry, {})
        peer_count = max(0, len(members) - 1)
        for target in members:
            code = str(target.get("code") or "")
            if selected is not None and code not in selected:
                continue
            context = _context_without_target(base, target, peer_count=peer_count)
            context["target_code"] = code
            context["target_excluded"] = True
            contexts[code] = context
    return contexts


def _company_share_change(metric: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[float | None, float | None]:
    revenue = context.get("revenue")
    if not isinstance(revenue, Mapping) or revenue.get("available") is not True:
        return None, None
    years = revenue.get("years")
    aggregates = revenue.get("aggregates")
    if not isinstance(years, Sequence) or len(years) < 2 or not isinstance(aggregates, Mapping):
        return None, None
    history = _series(metric, "revenue_values", "revenue_years")
    first_year, last_year = int(years[0]), int(years[-1])
    first_total = _finite(aggregates.get(first_year))
    last_total = _finite(aggregates.get(last_year))
    if (
        first_total is None
        or first_total <= 0
        or last_total is None
        or last_total <= 0
        or first_year not in history
        or last_year not in history
    ):
        return None, None
    first_share = history[first_year] / (first_total + history[first_year])
    last_share = history[last_year] / (last_total + history[last_year])
    return last_share, last_share - first_share


def _score_roic_spread(metric: Mapping[str, Any]) -> tuple[float, float | None]:
    roic = _finite(metric.get("roic"))
    wacc = _finite(metric.get("wacc"))
    spread = roic - wacc if roic is not None and wacc is not None else None
    score = _linear_score(
        spread,
        [(-0.05, 0), (0.0, 2), (0.02, 5), (0.05, 7), (0.10, 9), (0.15, 10)],
    )
    return score, spread


def _score_historical_roic_spread(metric: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    roic_history = _series(metric, "indicator_roic_history", "indicator_roic_years")
    recent_years = _latest_consecutive_years(set(roic_history))[-5:]
    history = [roic_history[year] for year in recent_years]
    wacc = _finite(metric.get("wacc"))
    spreads = [value - wacc for value in history] if wacc is not None else []
    spread_median = _median(spreads)
    score = _linear_score(
        spread_median,
        [(0.0, 1), (0.02, 3), (0.05, 5), (0.10, 7), (0.20, 9), (0.30, 10)],
    )
    positive_count = sum(spread > 0 for spread in spreads)
    if len(spreads) >= 3 and positive_count < 2:
        score = min(score, 3.0)
    return score, {
        "roic_wacc_spread_median": spread_median,
        "recent_roic_spread_history_count": len(spreads),
        "recent_roic_spread_history_years": recent_years,
        "recent_roic_spread_window_years": 5,
        "positive_spread_count": positive_count,
        "wacc": wacc,
    }


def _score_dilution(metric: Mapping[str, Any]) -> float:
    dilution = _finite(metric.get("share_dilution_1yr"))
    return _linear_score(dilution, [(-0.05, 10), (0.0, 10), (0.02, 8), (0.05, 5), (0.10, 2), (0.20, 0)])


def _score_cash_conversion(metric: Mapping[str, Any]) -> float:
    ratio = _finite(metric.get("ocf_np_ratio"))
    return _linear_score(ratio, [(-0.2, 0), (0.0, 1), (0.4, 3), (0.6, 6), (0.8, 8), (1.0, 10), (1.5, 10)])


def _score_adjusted_profit(metric: Mapping[str, Any]) -> float:
    ratio = _finite(metric.get("adjusted_profit_ratio"))
    if ratio is not None:
        ratio = min(ratio, 1.0)
    return _linear_score(ratio, [(0.0, 0), (0.5, 2), (0.7, 5), (0.85, 8), (0.95, 10)])


def _score_fcf_history(metric: Mapping[str, Any]) -> float:
    values = [value for value in (_finite(item) for item in metric.get("fcf_history", [])) if value is not None]
    if not values:
        return 1.0
    return _round_score(10.0 * sum(value > 0 for value in values[-3:]) / min(len(values), 3))


def _score_balance_discipline(metric: Mapping[str, Any]) -> float:
    ratio = _finite(metric.get("interest_bearing_debt_ratio"))
    if ratio is None:
        ratio = _finite(metric.get("debt_ratio"))
    return _linear_score(ratio, [(0.0, 10), (0.20, 10), (0.40, 7), (0.60, 4), (0.80, 1), (1.0, 0)])


def _score_growth_persistence(metric: Mapping[str, Any]) -> float:
    rates = _growth_rates(_series(metric, "revenue_values", "revenue_years"))
    if not rates:
        return 1.0
    positive = sum(rate > 0 for rate in rates) / len(rates)
    deep_declines = sum(rate <= -0.10 for rate in rates)
    return _round_score(positive * 10.0 - deep_declines * 2.0)


def _score_growth_trend(metric: Mapping[str, Any]) -> float:
    growth = _finite(metric.get("trend_growth"))
    slope = _finite(metric.get("growth_slope"))
    base = _linear_score(growth, [(-0.10, 0), (0.0, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.35, 10)])
    if slope is None:
        return _round_score(base - 1.0)
    # ``_linear_score`` is normally 0-10; these anchors intentionally carry a
    # signed adjustment, so interpolate it locally instead of clipping.
    if slope <= -0.15:
        adjustment = -3.0
    elif slope >= 0.15:
        adjustment = 2.0
    else:
        adjustment = -3.0 + (slope + 0.15) / 0.30 * 5.0
    return _round_score(base + adjustment)


def _score_industry_durability(
    context: Mapping[str, Any], fallback_growth: float | None
) -> tuple[float, dict[str, Any]]:
    growth = _finite(context.get("aggregate_revenue_cagr"))
    basis = "comparable_aggregate"
    if growth is None:
        growth = fallback_growth
        basis = "cross_section_median_fallback"
    growth_score = _linear_score(
        growth,
        [(-0.15, 0), (-0.10, 1), (-0.03, 3), (0.0, 4.5), (0.03, 6), (0.08, 8), (0.15, 9), (0.25, 10)],
    )
    loss_share = _finite(context.get("loss_share"))
    loss_score = _linear_score(loss_share, [(0.0, 10), (0.15, 8), (0.30, 6), (0.50, 3), (0.75, 0)])
    margin_trend = _finite(context.get("median_margin_trajectory"))
    margin_score = _linear_score(margin_trend, [(-0.30, 0), (-0.15, 2), (-0.05, 4), (0.0, 6), (0.10, 8), (0.25, 10)])
    score = _round_score(0.60 * growth_score + 0.20 * loss_score + 0.20 * margin_score)
    return score, {
        "basis": basis,
        "aggregate_revenue_cagr": growth,
        "loss_share": loss_share,
        "median_margin_trajectory": margin_trend,
        "components": {"growth": growth_score, "loss_health": loss_score, "margin_health": margin_score},
    }


def _score_accounting(metric: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    adjusted = _score_adjusted_profit(metric)
    cash = _score_cash_conversion(metric)
    fcf = _score_fcf_history(metric)
    dilution = _score_dilution(metric)
    score = _round_score(0.30 * adjusted + 0.35 * cash + 0.20 * fcf + 0.15 * dilution)
    return score, {
        "adjusted_profit_ratio": _finite(metric.get("adjusted_profit_ratio")),
        "ocf_to_net_profit": _finite(metric.get("ocf_np_ratio")),
        "positive_fcf_score": fcf,
        "share_dilution_1yr": _finite(metric.get("share_dilution_1yr")),
        "components": {"adjusted": adjusted, "cash": cash, "fcf": fcf, "dilution": dilution},
    }


def _score_management_outcomes(metric: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    dilution = _score_dilution(metric)
    spread_score, spread = _score_roic_spread(metric)
    fcf = _score_fcf_history(metric)
    balance = _score_balance_discipline(metric)
    score = _round_score(0.30 * dilution + 0.30 * spread_score + 0.20 * fcf + 0.20 * balance)
    return score, {
        "scope": "capital_allocation_outcomes_not_management_character",
        "roic_wacc_spread": spread,
        "share_dilution_1yr": _finite(metric.get("share_dilution_1yr")),
        "components": {"dilution": dilution, "excess_return": spread_score, "fcf": fcf, "balance": balance},
    }


def _score_moat(
    metric: Mapping[str, Any],
    context: Mapping[str, Any],
    accounting_score: float,
) -> tuple[float, dict[str, Any]]:
    spread_score, spread_details = _score_historical_roic_spread(metric)
    gross_history_by_year = _series(metric, "gross_margin_history", "gross_margin_years")
    recent_gross_years = _latest_consecutive_years(set(gross_history_by_year))[-5:]
    gross_history = [gross_history_by_year[year] for year in recent_gross_years]
    gross = _median(gross_history)
    peer_gross = _median(context.get("gross_margin_median_population", []))
    gross_advantage = gross - peer_gross if gross is not None and peer_gross is not None else None
    relative_margin = _linear_score(
        gross_advantage,
        [(-0.10, 1), (-0.05, 3), (0.0, 5), (0.05, 7), (0.10, 9), (0.20, 10)],
    )
    gross_cv = _finite(metric.get("gross_margin_cv"))
    stability_adjustment = (
        1.0 if gross_cv is not None and gross_cv <= 0.05 else 0.5 if gross_cv is not None and gross_cv <= 0.10 else 0.0
    )
    margin_power = _round_score(relative_margin + stability_adjustment)
    if gross_cv is not None and gross_cv > 0.30:
        margin_power = min(margin_power, 3.0)
    if len(gross_history) >= 3 and gross_history[-1] - gross_history[-3] < -0.05:
        margin_power = min(margin_power, 4.0)
    fcf_score = _score_fcf_history(metric)
    cash_outcome = _round_score(0.60 * accounting_score + 0.40 * fcf_score)
    latest_share, share_change = _company_share_change(metric, context)
    revenue_percentile = _percentile_rank(
        _finite(metric.get("revenue_latest")), context.get("revenue_latest_population", [])
    )
    share_score = _linear_score(
        revenue_percentile,
        [(0.0, 1), (0.25, 3), (0.50, 5), (0.75, 7), (0.90, 9), (0.98, 10)],
    )
    if share_change is not None and latest_share is not None and latest_share - share_change > 0:
        relative_share_change = share_change / (latest_share - share_change)
        if relative_share_change >= 0.10:
            share_score = min(10.0, share_score + 1.0)
        elif relative_share_change <= -0.15:
            share_score = max(0.0, share_score - 2.0)
    else:
        relative_share_change = None
    score = _round_score(0.35 * spread_score + 0.25 * margin_power + 0.20 * cash_outcome + 0.20 * share_score)
    recent_operating_evidence_years = min(
        5,
        _latest_consecutive_year_count(
            set(gross_history_by_year) & set(_series(metric, "indicator_roic_history", "indicator_roic_years"))
        ),
    )
    if recent_operating_evidence_years < 5:
        score = min(score, 6.0)
    return score, {
        "scope": "observable_economic_moat_outcomes_not_moat_mechanism",
        **spread_details,
        "gross_margin_5y_median": gross,
        "peer_gross_margin_median": peer_gross,
        "gross_margin_advantage": gross_advantage,
        "gross_margin_cv": gross_cv,
        "listed_peer_revenue_share": latest_share,
        "listed_peer_share_change": share_change,
        "listed_peer_relative_share_change": relative_share_change,
        "listed_peer_revenue_percentile": revenue_percentile,
        "recent_gross_margin_history_count": len(gross_history),
        "recent_gross_margin_history_years": recent_gross_years,
        "recent_operating_evidence_years": recent_operating_evidence_years,
        "recent_operating_window_years": 5,
        "components": {
            "excess_return": spread_score,
            "margin_power": margin_power,
            "cash_outcome": cash_outcome,
            "share_durability": share_score,
        },
    }


def _score_moat_durability(metric: Mapping[str, Any], moat_score: float) -> tuple[float, dict[str, Any]]:
    roic_history = _series(metric, "indicator_roic_history", "indicator_roic_years")
    gross_history = _series(metric, "gross_margin_history", "gross_margin_years")
    roic_positive = _positive_share(list(roic_history.values()))
    roic_history_score = _round_score(10.0 * roic_positive) if roic_positive is not None else 3.0
    margin_stability = _linear_score(
        _finite(metric.get("gross_margin_cv")),
        [(0.0, 10), (0.03, 9), (0.08, 7), (0.15, 4), (0.30, 1)],
    )
    persistence = _score_growth_persistence(metric)
    score = _round_score(0.65 * moat_score + 0.15 * roic_history_score + 0.10 * margin_stability + 0.10 * persistence)
    common_history_years = set(roic_history) & set(gross_history)
    history_count = _latest_consecutive_year_count(common_history_years)
    if history_count < 3:
        score = min(score, 2.0)
    elif history_count <= 5:
        score = min(score, 4.0)
    elif history_count <= 9:
        score = min(score, 6.0)
    return score, {
        "scope": "observable_moat_persistence",
        "roic_history_positive_share": roic_positive,
        "gross_margin_cv": _finite(metric.get("gross_margin_cv")),
        "durability_history_years": history_count,
        "history_count": history_count,
        "common_history_years": sorted(common_history_years),
        "history_cap": 2.0 if history_count < 3 else 4.0 if history_count <= 5 else 6.0 if history_count <= 9 else 10.0,
        "components": {
            "current_moat": moat_score,
            "roic_history": roic_history_score,
            "margin_stability": margin_stability,
            "revenue_persistence": persistence,
        },
    }


def _score_growth_quality(metric: Mapping[str, Any], accounting_score: float) -> tuple[float, dict[str, Any]]:
    dilution = _score_dilution(metric)
    balance = _score_balance_discipline(metric)
    revenue = _series(metric, "revenue_values", "revenue_years")
    assets = _series(metric, "total_assets_history", "total_assets_years")
    common = sorted(set(revenue) & set(assets))
    efficiency_delta = None
    if len(common) >= 3:
        selected = common[-3:]
        if all(current - prior == 1 for prior, current in zip(selected, selected[1:])):
            revenue_growth = _cagr(revenue[selected[0]], revenue[selected[-1]], len(selected) - 1)
            asset_growth = _cagr(assets[selected[0]], assets[selected[-1]], len(selected) - 1)
            if revenue_growth is not None and asset_growth is not None:
                efficiency_delta = revenue_growth - asset_growth
    efficiency = _linear_score(
        efficiency_delta,
        [(-0.20, 0), (-0.10, 2), (-0.03, 4), (0.0, 6), (0.05, 8), (0.15, 10)],
    )
    fcf = _score_fcf_history(metric)
    score_before_evidence_cap = _round_score(
        0.30 * accounting_score + 0.25 * dilution + 0.20 * balance + 0.15 * efficiency + 0.10 * fcf
    )
    score = score_before_evidence_cap
    external_growth = metric.get("external_growth_evidence")
    external_inputs = (
        _external_growth_proxy_inputs(external_growth)
        if metric.get("_type3_growth_validation_token") is TYPE3_GROWTH_VALIDATION_TOKEN
        else None
    )
    external_complete = external_inputs is not None
    external_cap = 10.0
    if external_inputs is None:
        # Cash, leverage and dilution can diagnose organic funding quality, but
        # they cannot prove the absence of acquisitions or goodwill growth.
        score = min(score, 6.0)
        external_cap = 6.0
    else:
        acquisition_intensity = external_inputs["acquisition_intensity"]
        goodwill_ratio = external_inputs["goodwill_to_revenue_latest"]
        goodwill_change = external_inputs["goodwill_change_to_revenue"]
        # Patch 6's 20%/30% bands are acquisition *revenue* shares.  The
        # automatic adapter measures cash spending and goodwill in a different
        # unit; abnormal values can reduce this conservative proxy, but low
        # values never prove that growth was purely organic.
        external_cap = 6.0
        if acquisition_intensity > 0.30 or goodwill_change > 0.30:
            external_cap = 4.0
        elif acquisition_intensity > 0.20 or goodwill_change > 0.20:
            external_cap = 4.5
        elif acquisition_intensity > 0.10 or goodwill_change > 0.10 or goodwill_ratio > 0.50:
            external_cap = 5.0
        elif acquisition_intensity > 0.05 or goodwill_change > 0.05 or goodwill_ratio > 0.30:
            external_cap = 5.5
        score = min(score, external_cap)
    return score, {
        "scope": "organic_funding_and_efficiency_proxy_not_ma_transaction_census",
        "revenue_minus_asset_cagr": efficiency_delta,
        "share_dilution_1yr": _finite(metric.get("share_dilution_1yr")),
        "external_growth_evidence_complete": external_complete,
        "score_before_evidence_cap": score_before_evidence_cap,
        "external_growth_score_cap": external_cap,
        "external_growth_proxy": external_inputs,
        "score_cap_without_acquisition_and_goodwill_evidence": 6.0,
        "claims_not_supported": ["exact_acquisition_revenue_share", "complete_transaction_census"],
        "components": {
            "accounting": accounting_score,
            "dilution": dilution,
            "balance": balance,
            "asset_efficiency": efficiency,
            "fcf": fcf,
        },
    }


def _score_growth_sustainability(
    metric: Mapping[str, Any],
    moat_score: float,
    industry_score: float,
) -> tuple[float, dict[str, Any]]:
    persistence = _score_growth_persistence(metric)
    trend = _score_growth_trend(metric)
    spread_score, spread = _score_roic_spread(metric)
    score_before_evidence_cap = _round_score(
        0.25 * persistence + 0.25 * trend + 0.25 * moat_score + 0.15 * industry_score + 0.10 * spread_score
    )
    score = score_before_evidence_cap
    segment_sources = metric.get("segment_growth_sources")
    segment_inputs = (
        _segment_growth_proxy_inputs(segment_sources)
        if metric.get("_type3_growth_validation_token") is TYPE3_GROWTH_VALIDATION_TOKEN
        else None
    )
    segment_complete = segment_inputs is not None
    segment_band_score = None
    if segment_inputs is None:
        # Aggregate statements cannot identify independent product/region
        # growth sources.  Keep a useful financial persistence diagnostic while
        # preventing it from masquerading as a complete Patch6 3d score.
        score = min(score, 4.0)
    else:
        source_count = segment_inputs["growth_source_count"]
        history_years = int(segment_inputs["history_years"])
        positive_growth_share = segment_inputs["positive_growth_share"]
        revenue_hhi = segment_inputs["revenue_hhi"]
        if source_count >= 4 and history_years >= 10:
            segment_band_score = 9.5
        elif source_count >= 3 and history_years >= 5:
            segment_band_score = 7.5
        elif source_count >= 2 and history_years >= 3:
            segment_band_score = 5.5
        elif source_count >= 1:
            segment_band_score = 3.5
        else:
            segment_band_score = 1.5
        # The Patch6 source-count/time-depth band is a hard ceiling.  Positive
        # growth breadth and concentration provide only a bounded within-band
        # adjustment, and weak aggregate persistence may reduce the result.
        # This preserves the explicit 3d<=3 veto for zero/one weak source.
        breadth_adjustment = (positive_growth_share - 0.5) * 1.0
        concentration_adjustment = (0.5 - revenue_hhi) * 0.5
        segment_band_score = _round_score(segment_band_score + breadth_adjustment + concentration_adjustment)
        score = min(score_before_evidence_cap, segment_band_score)
    return score, {
        "scope": "observable_growth_longevity_not_product_level_source_count",
        "trend_growth": _finite(metric.get("trend_growth")),
        "growth_slope": _finite(metric.get("growth_slope")),
        "roic_wacc_spread": spread,
        "segment_growth_sources_complete": segment_complete,
        "score_before_evidence_cap": score_before_evidence_cap,
        "segment_band_score": segment_band_score,
        "segment_growth_proxy": segment_inputs,
        "score_cap_without_segment_revenue_history": 4.0,
        "claims_not_supported": ["source_replicability", "unreported_segment_attribution"],
        "components": {
            "persistence": persistence,
            "trend": trend,
            "moat": moat_score,
            "industry": industry_score,
            "excess_return": spread_score,
        },
    }


def _runway_band_score(years: float) -> float:
    """Map an evidenced runway horizon to Patch6's exact five bands."""
    if years < 3.0:
        return 1.5
    if years < 5.0:
        return 3.5
    if years < 10.0:
        return 5.5
    if years <= 20.0:
        return 7.5
    return 9.5


def _score_runway(
    metric: Mapping[str, Any],
    context: Mapping[str, Any],
    moat_score: float,
    industry_score: float,
) -> tuple[float, dict[str, Any]]:
    growth = _finite(metric.get("trend_growth"))
    industry_growth = _finite(context.get("aggregate_revenue_cagr"))
    share, share_change = _company_share_change(metric, context)
    share_credit = min(0.05, max(0.0, share_change or 0.0))
    constrained_growth = growth
    if constrained_growth is not None and industry_growth is not None:
        constrained_growth = min(constrained_growth, industry_growth + share_credit)
    slope = _finite(metric.get("growth_slope"))
    if constrained_growth is None or constrained_growth <= RUNWAY_TERMINAL_GROWTH:
        years = 0.0
    else:
        fade = max(0.01, -(slope or 0.0))
        years = max(0.0, (constrained_growth - RUNWAY_TERMINAL_GROWTH) / fade)
    tam_years = _finite(metric.get("tam_runway_years"))
    if tam_years is not None:
        years = min(years, max(0.0, tam_years))
    score = _runway_band_score(years)
    revenue_history = _series(metric, "revenue_values", "revenue_years")
    history_count = _latest_consecutive_year_count(set(revenue_history))
    evidence_cap = 10.0
    if tam_years is None and history_count < 10:
        evidence_cap = 6.0
    if industry_growth is not None and industry_growth <= 0 and share is not None and share >= 0.40:
        evidence_cap = min(evidence_cap, 4.0)
    if growth is not None and growth <= 0:
        evidence_cap = min(evidence_cap, 3.0)
    score = min(score, evidence_cap)
    return score, {
        "scope": "financial_fade_horizon_not_tam_or_penetration_proof",
        "formula": "years=max(0,(min(company_g,peer_g+share_credit)-2%)/max(1%,-growth_slope))",
        "trend_growth": growth,
        "peer_aggregate_revenue_cagr": industry_growth,
        "listed_peer_revenue_share": share,
        "listed_peer_share_change": share_change,
        "share_growth_credit": share_credit,
        "constrained_growth": constrained_growth,
        "moat_score": moat_score,
        "industry_durability_score": industry_score,
        "growth_slope": slope,
        "observable_runway_years": years,
        "tam_runway_years": tam_years,
        "financial_history_years": history_count,
        "financial_history_periods": sorted(revenue_history),
        "evidence_cap": evidence_cap,
        "claims_not_supported": ["tam", "market_penetration"] if tam_years is None else [],
    }


def _score_industry_bubble(context: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    revenue_growth = _finite(context.get("aggregate_revenue_cagr"))
    asset_growth = _finite(context.get("aggregate_assets_cagr"))
    asset_gap = asset_growth - revenue_growth if asset_growth is not None and revenue_growth is not None else None
    supply_risk = _linear_score(
        asset_gap,
        [(-0.05, 0), (0.0, 1), (0.05, 3), (0.10, 6), (0.20, 8), (0.30, 10)],
        missing=5.0,
    )
    capex_intensity = context.get("capex_intensity")
    capex_ratios = capex_intensity.get("ratios") if isinstance(capex_intensity, Mapping) else None
    capex_acceleration = None
    if isinstance(capex_ratios, Mapping) and len(capex_ratios) >= 4:
        ordered_ratios = [_finite(capex_ratios[year]) for year in sorted(capex_ratios)]
        if all(value is not None for value in ordered_ratios):
            prior = math.fsum(ordered_ratios[:2]) / 2.0  # type: ignore[arg-type]
            recent = math.fsum(ordered_ratios[-2:]) / 2.0  # type: ignore[arg-type]
            if prior > 0:
                capex_acceleration = recent / prior - 1.0
    expansion_risk = _linear_score(
        capex_acceleration,
        [(0.0, 0), (0.20, 2), (0.50, 5), (1.0, 8), (2.0, 10)],
        missing=5.0,
    )
    profit_margin = context.get("profit_margin")
    margin_ratios = profit_margin.get("ratios") if isinstance(profit_margin, Mapping) else None
    margin_decline = None
    if isinstance(margin_ratios, Mapping) and len(margin_ratios) >= 2:
        ordered_margins = [_finite(margin_ratios[year]) for year in sorted(margin_ratios)]
        if all(value is not None for value in ordered_margins):
            margin_decline = max(0.0, ordered_margins[0] - ordered_margins[-1])  # type: ignore[operator]
    profit_squeeze_risk = _linear_score(
        margin_decline,
        [(0.0, 0), (0.02, 2), (0.05, 5), (0.10, 8), (0.15, 10)],
        missing=5.0,
    )
    loss_share = _finite(context.get("loss_share"))
    negative_fcf_share = _finite(context.get("negative_fcf_share"))
    pressure_share = max(value for value in (loss_share, negative_fcf_share, 0.0) if value is not None)
    pressure_risk = _linear_score(
        pressure_share,
        [(0.10, 0), (0.20, 2), (0.35, 5), (0.50, 7), (0.70, 9), (0.80, 10)],
    )
    bubble_risk = _round_score(
        0.35 * supply_risk + 0.25 * expansion_risk + 0.20 * profit_squeeze_risk + 0.20 * pressure_risk
    )
    score = _round_score(10.0 - bubble_risk)
    severe = [supply_risk, expansion_risk, profit_squeeze_risk, pressure_risk]
    if (
        asset_gap is not None
        and asset_gap >= 0.10
        and (
            (capex_acceleration is not None and capex_acceleration >= 0.50)
            or (margin_decline is not None and margin_decline >= 0.05)
        )
        and pressure_share >= 0.35
    ):
        score = min(score, 2.0)
    elif sum(component >= 6.0 for component in severe) >= 2:
        score = min(score, 4.0)
    return score, {
        "scope": "peer_aggregate_supply_and_profitability_anti_bubble_score",
        "aggregate_revenue_cagr": revenue_growth,
        "aggregate_assets_cagr": asset_growth,
        "assets_minus_revenue_growth": asset_gap,
        "capex_intensity_acceleration": capex_acceleration,
        "aggregate_margin_decline": margin_decline,
        "loss_share": loss_share,
        "negative_fcf_share": negative_fcf_share,
        "components": {
            "supply_mismatch_risk": supply_risk,
            "capex_acceleration_risk": expansion_risk,
            "profit_squeeze_risk": profit_squeeze_risk,
            "pressure_breadth_risk": pressure_risk,
            "bubble_risk": bubble_risk,
        },
        "target_excluded": context.get("target_excluded") is True,
        "peer_count": int(context.get("peer_count") or 0),
    }


def _score_type3_bubble(metric: Mapping[str, Any], industry_bubble_score: float) -> tuple[float, dict[str, Any]]:
    coldness = _finite(metric.get("market_coldness_score"))
    peg = _finite(metric.get("peg"))
    price_score = (
        coldness if coldness is not None else _linear_score(peg, [(0.0, 9), (0.8, 8), (1.2, 6), (2.0, 3), (3.0, 1)])
    )
    components = metric.get("market_coldness_components")
    raw_market = components.get("raw_values") if isinstance(components, Mapping) else None
    change_60d = _finite(raw_market.get("change_60d_pct")) if isinstance(raw_market, Mapping) else None
    change_ytd = _finite(raw_market.get("change_ytd_pct")) if isinstance(raw_market, Mapping) else None
    volume_ratio = _finite(raw_market.get("volume_ratio")) if isinstance(raw_market, Mapping) else None
    if change_ytd is not None and change_60d is not None:
        if change_ytd > 50 and change_60d > 25 and volume_ratio is not None and volume_ratio > 1.5:
            price_score = min(price_score, 3.0)
        elif change_ytd > 30 and change_60d > 15:
            price_score = min(price_score, 5.0)
    score = _round_score(min(industry_bubble_score, price_score))
    return score, {
        "scope": "weakest_of_industry_and_company_price_anti_bubble_evidence",
        "market_coldness_score": coldness,
        "peg": peg,
        "change_60d_pct": change_60d,
        "change_ytd_pct": change_ytd,
        "volume_ratio": volume_ratio,
        "components": {"industry": industry_bubble_score, "company_price": price_score},
    }


def _score_catalyst(metric: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    revenue_yoy = _finite(metric.get("interim_revenue_yoy"))
    profit_yoy = _finite(metric.get("interim_profit_yoy"))
    ocf_yoy = _finite(metric.get("interim_ocf_yoy"))
    annual_revenue_growth = _median(_growth_rates(_series(metric, "revenue_values", "revenue_years")))
    annual_profit_growth = _median(_growth_rates(_series(metric, "net_profit_history", "net_profit_years")))
    revenue_level = _linear_score(
        revenue_yoy,
        [(-0.10, 0), (-0.03, 2), (0.05, 5), (0.10, 7), (0.20, 9), (0.30, 10)],
        missing=0,
    )
    revenue_acceleration = (
        revenue_yoy - annual_revenue_growth if revenue_yoy is not None and annual_revenue_growth is not None else None
    )
    revenue_acceleration_score = _linear_score(
        revenue_acceleration,
        [(-0.10, 0), (-0.03, 2), (0.01, 5), (0.05, 7), (0.10, 9), (0.15, 10)],
        missing=0,
    )
    revenue = _round_score(0.50 * revenue_level + 0.50 * revenue_acceleration_score)
    profit_level = _linear_score(
        profit_yoy,
        [(-0.20, 0), (-0.05, 2), (0.10, 5), (0.20, 7), (0.40, 9), (0.80, 10)],
        missing=0,
    )
    profit_acceleration = (
        profit_yoy - annual_profit_growth if profit_yoy is not None and annual_profit_growth is not None else None
    )
    profit_acceleration_score = _linear_score(
        profit_acceleration,
        [(-0.20, 0), (-0.05, 2), (0.02, 5), (0.10, 7), (0.25, 9), (0.50, 10)],
        missing=0,
    )
    profit = _round_score(0.50 * profit_level + 0.50 * profit_acceleration_score)
    cash = _linear_score(
        ocf_yoy,
        [(-0.50, 0), (-0.20, 2), (0.0, 5), (0.20, 7), (0.50, 9), (1.0, 10)],
        missing=0,
    )
    if (_finite(metric.get("interim_current_ocf")) or 0.0) <= 0:
        cash = 0.0
    margin_history = [
        value for value in (_finite(item) for item in metric.get("margin_history", [])) if value is not None
    ]
    margin_trajectory = margin_history[-1] - float(median(margin_history[-4:-1])) if len(margin_history) >= 4 else None
    margin = _linear_score(
        margin_trajectory,
        [(-0.05, 0), (-0.02, 2), (0.0, 5), (0.01, 7), (0.03, 9), (0.05, 10)],
        missing=0,
    )
    company_score = _round_score(0.25 * revenue + 0.30 * profit + 0.25 * cash + 0.20 * margin)
    current_revenue = _finite(metric.get("interim_current_revenue"))
    current_profit = _finite(metric.get("interim_current_profit"))
    company_turn = bool(
        sum(component >= 6.0 for component in (revenue, profit, cash)) >= 2
        and current_revenue is not None
        and current_revenue > 0
        and current_profit is not None
        and current_profit > 0
        and revenue_yoy is not None
        and revenue_yoy >= -0.10
        and profit_yoy is not None
        and profit_yoy >= -0.10
    )
    industry_revenue_yoy = _finite(context.get("median_interim_revenue_yoy"))
    industry_profit_yoy = _finite(context.get("median_interim_profit_yoy"))
    industry_revenue_base = _finite(context.get("median_annual_revenue_growth"))
    industry_profit_base = _finite(context.get("median_annual_profit_growth"))
    industry_accelerations = [
        current - prior
        for current, prior in (
            (industry_revenue_yoy, industry_revenue_base),
            (industry_profit_yoy, industry_profit_base),
        )
        if current is not None and prior is not None
    ]
    industry_turn = bool(
        context.get("target_excluded") is True
        and int(context.get("peer_count") or 0) >= MIN_SECTOR_COMPANIES
        and industry_revenue_yoy is not None
        and industry_revenue_yoy > 0
        and industry_profit_yoy is not None
        and industry_profit_yoy > 0
        and industry_accelerations
        and max(industry_accelerations) >= 0.03
    )
    components = metric.get("market_coldness_components")
    raw_market = components.get("raw_values") if isinstance(components, Mapping) else None
    change_60d = _finite(raw_market.get("change_60d_pct")) if isinstance(raw_market, Mapping) else None
    volume_ratio = _finite(raw_market.get("volume_ratio")) if isinstance(raw_market, Mapping) else None
    peer_price = context.get("price") if isinstance(context.get("price"), Mapping) else {}
    peer_change_60d = _finite(peer_price.get("median_60d_pct"))
    price_confirmed = bool(
        change_60d is not None
        and change_60d > 0
        and peer_change_60d is not None
        and change_60d - peer_change_60d >= 5.0
        and volume_ratio is not None
        and 0.8 <= volume_ratio <= 2.0
        and int(peer_price.get("sample_count") or 0) >= MIN_SECTOR_COMPANIES
        and (_finite(peer_price.get("coverage")) or 0.0) >= MIN_COMPARABLE_COVERAGE
    )
    if not company_turn:
        score = min(4.0, _round_score(0.40 * company_score))
        band = "no_confirmed_company_turn"
    elif not industry_turn:
        score = _round_score(5.0 + _clip((company_score - 5.0) / 5.0, 0.0, 1.0))
        band = "company_turn_only"
    elif not price_confirmed:
        score = _round_score(7.0 + _clip((company_score - 5.0) / 5.0, 0.0, 1.0))
        band = "company_and_industry_turn"
    else:
        score = _round_score(9.0 + _clip((company_score - 7.0) / 3.0, 0.0, 1.0))
        band = "company_industry_and_price_confirmed"
    if all(value is None for value in (revenue_yoy, profit_yoy, ocf_yoy)):
        # Annual margin movement alone is not a current catalyst.  Without any
        # exact same-period interim comparison the observable catalyst score is
        # unavailable and must contribute no positive evidence.
        score = 0.0
        company_score = 0.0
        band = "no_interim_comparison"
    if metric.get("interim_profit_warning") or metric.get("interim_ocf_warning"):
        score = min(score, 3.0)
    return score, {
        "scope": "confirmed_operating_catalyst_only_no_unevidenced_event_claim",
        "interim_revenue_yoy": revenue_yoy,
        "interim_profit_yoy": profit_yoy,
        "interim_ocf_yoy": ocf_yoy,
        "margin_trajectory": margin_trajectory,
        "annual_revenue_growth_median": annual_revenue_growth,
        "annual_profit_growth_median": annual_profit_growth,
        "company_turn": company_turn,
        "industry_turn": industry_turn,
        "price_confirmed": price_confirmed,
        "decision_band": band,
        "company_composite": company_score,
        "components": {"revenue": revenue, "profit": profit, "cash": cash, "margin": margin},
        "industry": {
            "interim_revenue_yoy_median": industry_revenue_yoy,
            "interim_profit_yoy_median": industry_profit_yoy,
            "annual_revenue_growth_median": industry_revenue_base,
            "annual_profit_growth_median": industry_profit_base,
        },
        "market_confirmation": {
            "company_60d_pct": change_60d,
            "peer_median_60d_pct": peer_change_60d,
            "volume_ratio": volume_ratio,
        },
    }


def _score_technology(metric: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    intensity = _finite(metric.get("rd_intensity"))
    raw_sector_intensities = context.get("rd_intensity_population", [])
    sector_intensities = (
        raw_sector_intensities
        if isinstance(raw_sector_intensities, Sequence) and not isinstance(raw_sector_intensities, (str, bytes))
        else []
    )
    percentile = _percentile_rank(intensity, sector_intensities)
    absolute = _linear_score(intensity, [(0.0, 1), (0.01, 2), (0.03, 4), (0.06, 6), (0.10, 8), (0.20, 10)], missing=1)
    relative = 10.0 * percentile if percentile is not None else 2.0
    growth = _score_growth_trend(metric)
    spread, spread_value = _score_roic_spread(metric)
    gross = _finite(metric.get("gross_margin"))
    commercial = _round_score(
        0.45 * growth + 0.35 * spread + 0.20 * _linear_score(gross, [(0.0, 0), (0.20, 3), (0.40, 6), (0.70, 9)])
    )
    score_cap = 8.0 if intensity is not None else 4.0
    score = min(score_cap, _round_score(0.45 * absolute + 0.20 * relative + 0.35 * commercial))
    return score, {
        "scope": "rd_and_commercialisation_outcomes_not_patent_or_product_breakthrough_proof",
        "rd_intensity": intensity,
        "rd_sector_percentile": percentile,
        "roic_wacc_spread": spread_value,
        "components": {"rd_absolute": absolute, "rd_relative": relative, "commercial_outcome": commercial},
        "score_cap_without_primary_technology_evidence": 8.0,
        "score_cap_without_reported_rd_intensity": score_cap,
    }


def _score_business_model(metric: Mapping[str, Any], accounting_score: float) -> tuple[float, dict[str, Any]]:
    revenue = _series(metric, "revenue_values", "revenue_years")
    assets = _series(metric, "total_assets_history", "total_assets_years")
    common = sorted(set(revenue) & set(assets))
    operating_leverage = None
    if len(common) >= 3:
        years = common[-3:]
        revenue_growth = _cagr(revenue[years[0]], revenue[years[-1]], years[-1] - years[0])
        asset_growth = _cagr(assets[years[0]], assets[years[-1]], years[-1] - years[0])
        if revenue_growth is not None and asset_growth is not None:
            operating_leverage = revenue_growth - asset_growth
    leverage_score = _linear_score(
        operating_leverage,
        [(-0.20, 0), (-0.10, 2), (-0.03, 4), (0.0, 6), (0.05, 8), (0.15, 10)],
    )
    margin = _linear_score(
        _finite(metric.get("margin_trajectory")),
        [(-0.30, 0), (-0.10, 2), (-0.03, 4), (0.0, 6), (0.10, 8), (0.25, 10)],
    )
    dilution = _score_dilution(metric)
    score = min(8.0, _round_score(0.35 * leverage_score + 0.25 * margin + 0.25 * accounting_score + 0.15 * dilution))
    return score, {
        "scope": "scale_economics_proxy_not_business_model_novelty_claim",
        "revenue_minus_asset_cagr": operating_leverage,
        "margin_trajectory": _finite(metric.get("margin_trajectory")),
        "components": {
            "operating_leverage": leverage_score,
            "margin": margin,
            "accounting": accounting_score,
            "dilution": dilution,
        },
        "score_cap_without_primary_model_evidence": 8.0,
    }


def _has_common_consecutive_history(
    metric: Mapping[str, Any],
    left_values_key: str,
    left_years_key: str,
    right_values_key: str,
    right_years_key: str,
    *,
    minimum_years: int = 3,
) -> bool:
    left = _series(metric, left_values_key, left_years_key)
    right = _series(metric, right_values_key, right_years_key)
    common = sorted(set(left) & set(right))
    if len(common) < minimum_years:
        return False
    return any(
        all(current - prior == 1 for prior, current in zip(window, window[1:]))
        for start in range(len(common) - minimum_years + 1)
        for window in (common[start : start + minimum_years],)
    )


def _ratio_history_ready(context: Mapping[str, Any], key: str, minimum_count: int) -> bool:
    payload = context.get(key)
    ratios = payload.get("ratios") if isinstance(payload, Mapping) else None
    return isinstance(ratios, Mapping) and _finite_sequence_count(list(ratios.values())) >= minimum_count


def _build_evidence_qualities(
    metric: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    fallback_industry_growth: float | None,
) -> dict[str, dict[str, Any]]:
    """Declare the raw-input coverage behind every observable score.

    These declarations deliberately mirror the score formulas instead of
    inferring completeness from the final numeric value.  In particular, the
    default returned by ``_linear_score(None, ...)`` is never treated as an
    observed input.
    """

    def finite(key: str) -> bool:
        return _finite(metric.get(key)) is not None

    fcf_ready = _finite_sequence_count(metric.get("fcf_history")) > 0
    revenue_history = _series(metric, "revenue_values", "revenue_years")
    profit_history = _series(metric, "net_profit_history", "net_profit_years")
    roic_history = _series(metric, "indicator_roic_history", "indicator_roic_years")
    gross_history = _series(metric, "gross_margin_history", "gross_margin_years")
    gross_history_count = _latest_consecutive_year_count(set(gross_history))
    common_roic_gross_history_count = _latest_consecutive_year_count(set(roic_history) & set(gross_history))
    balance_ready = finite("interest_bearing_debt_ratio") or finite("debt_ratio")
    roic_wacc_ready = finite("roic") and finite("wacc")
    historical_roic_ready = _latest_consecutive_year_count(set(roic_history)) >= 3 and finite("wacc")
    revenue_asset_ready = _has_common_consecutive_history(
        metric,
        "revenue_values",
        "revenue_years",
        "total_assets_history",
        "total_assets_years",
    )
    latest_share, share_change = _company_share_change(metric, context)
    peer_count = int(context.get("peer_count") or 0)
    peer_gross_ready = _finite_sequence_count(context.get("gross_margin_median_population")) >= MIN_SECTOR_COMPANIES
    peer_revenue_scale_ready = (
        finite("revenue_latest")
        and _finite_sequence_count(context.get("revenue_latest_population")) >= MIN_SECTOR_COMPANIES
    )
    industry_growth_ready = (
        _finite(context.get("aggregate_revenue_cagr")) is not None or fallback_industry_growth is not None
    )

    qualities: dict[str, dict[str, Any]] = {}
    qualities["industry_durability_score"] = _quality_record(
        {
            "industry_revenue_growth": industry_growth_ready,
            "industry_loss_share": _finite(context.get("loss_share")) is not None,
            "industry_margin_trajectory": _finite(context.get("median_margin_trajectory")) is not None,
        }
    )
    qualities["accounting_integrity_score"] = _quality_record(
        {
            "adjusted_profit_ratio": finite("adjusted_profit_ratio"),
            "ocf_to_net_profit": finite("ocf_np_ratio"),
            "free_cash_flow_history": fcf_ready,
            "share_dilution": finite("share_dilution_1yr"),
        }
    )
    qualities["management_alignment_score"] = _quality_record(
        {
            "share_dilution": finite("share_dilution_1yr"),
            "roic_and_wacc": roic_wacc_ready,
            "free_cash_flow_history": fcf_ready,
            "balance_sheet_leverage": balance_ready,
        }
    )

    accounting_complete = qualities["accounting_integrity_score"]["level"] == "derived_proxy"
    qualities["moat_score"] = _quality_record(
        {
            "historical_roic_and_wacc": historical_roic_ready,
            "gross_margin_history": gross_history_count >= 3,
            "peer_gross_margin_history": peer_gross_ready,
            "accounting_outcomes": accounting_complete,
            "free_cash_flow_history": fcf_ready,
            "listed_peer_revenue_scale": peer_revenue_scale_ready,
        }
    )
    moat_complete = qualities["moat_score"]["level"] == "derived_proxy"
    qualities["moat_durability_score"] = _quality_record(
        {
            "current_moat_proxy": moat_complete,
            "roic_and_gross_margin_common_history": common_roic_gross_history_count >= 3,
            "gross_margin_stability": finite("gross_margin_cv"),
            "revenue_growth_history": _latest_consecutive_year_count(set(revenue_history)) >= 3,
        }
    )
    qualities["growth_quality_score"] = _quality_record(
        {
            "accounting_outcomes": accounting_complete,
            "share_dilution": finite("share_dilution_1yr"),
            "balance_sheet_leverage": balance_ready,
            "revenue_asset_history": revenue_asset_ready,
            "free_cash_flow_history": fcf_ready,
            "acquisition_cash_and_goodwill_history": (
                metric.get("_type3_growth_validation_token") is TYPE3_GROWTH_VALIDATION_TOKEN
                and _external_growth_proxy_inputs(metric.get("external_growth_evidence")) is not None
            ),
        }
    )
    industry_complete = qualities["industry_durability_score"]["level"] == "derived_proxy"
    qualities["growth_sustainability_score"] = _quality_record(
        {
            "revenue_growth_history": len(_growth_rates(revenue_history)) >= 2,
            "trend_growth": finite("trend_growth"),
            "growth_slope": finite("growth_slope"),
            "roic_and_wacc": roic_wacc_ready,
            "current_moat_proxy": moat_complete,
            "industry_durability_proxy": industry_complete,
            "segment_growth_sources": (
                metric.get("_type3_growth_validation_token") is TYPE3_GROWTH_VALIDATION_TOKEN
                and _segment_growth_proxy_inputs(metric.get("segment_growth_sources")) is not None
            ),
        }
    )
    runway_inputs = {
        "trend_growth": finite("trend_growth"),
        "growth_slope": finite("growth_slope"),
        "peer_industry_growth": _finite(context.get("aggregate_revenue_cagr")) is not None,
        "listed_peer_revenue_share": latest_share is not None and share_change is not None,
        "revenue_history": len(revenue_history) >= 3,
    }
    qualities["runway_score"] = _quality_record(
        runway_inputs,
        core_available=runway_inputs["trend_growth"],
    )
    bubble_inputs = {
        "peer_cohort": context.get("target_excluded") is True and peer_count >= MIN_SECTOR_COMPANIES,
        "industry_revenue_growth": _finite(context.get("aggregate_revenue_cagr")) is not None,
        "industry_asset_growth": _finite(context.get("aggregate_assets_cagr")) is not None,
        "capex_intensity_history": _ratio_history_ready(context, "capex_intensity", 4),
        "profit_margin_history": _ratio_history_ready(context, "profit_margin", 2),
        "industry_pressure_breadth": any(
            _finite(context.get(key)) is not None for key in ("loss_share", "negative_fcf_share")
        ),
    }
    qualities["industry_bubble_score"] = _quality_record(bubble_inputs)
    industry_bubble_complete = qualities["industry_bubble_score"]["level"] == "derived_proxy"
    qualities["type3_bubble_score"] = _quality_record(
        {
            "industry_anti_bubble_proxy": industry_bubble_complete,
            "company_price_anti_bubble": finite("market_coldness_score") or finite("peg"),
        }
    )

    revenue_yoy_ready = finite("interim_revenue_yoy")
    profit_yoy_ready = finite("interim_profit_yoy")
    ocf_yoy_ready = finite("interim_ocf_yoy")
    peer_price = context.get("price") if isinstance(context.get("price"), Mapping) else {}
    market_raw = metric.get("market_coldness_components")
    market_raw = market_raw.get("raw_values") if isinstance(market_raw, Mapping) else {}
    catalyst_inputs = {
        "interim_revenue_comparison": revenue_yoy_ready,
        "interim_profit_comparison": profit_yoy_ready,
        "interim_cash_flow_comparison": ocf_yoy_ready,
        "current_revenue_and_profit": finite("interim_current_revenue") and finite("interim_current_profit"),
        "annual_revenue_baseline": len(_growth_rates(revenue_history)) > 0,
        "annual_profit_baseline": len(_growth_rates(profit_history)) > 0,
        "peer_interim_and_annual_context": (
            peer_count >= MIN_SECTOR_COMPANIES
            and all(
                _finite(context.get(key)) is not None
                for key in (
                    "median_interim_revenue_yoy",
                    "median_interim_profit_yoy",
                    "median_annual_revenue_growth",
                    "median_annual_profit_growth",
                )
            )
        ),
        "market_confirmation_inputs": (
            isinstance(market_raw, Mapping)
            and _finite(market_raw.get("change_60d_pct")) is not None
            and _finite(market_raw.get("volume_ratio")) is not None
            and _finite(peer_price.get("median_60d_pct")) is not None
            and int(peer_price.get("sample_count") or 0) >= MIN_SECTOR_COMPANIES
        ),
    }
    qualities["catalyst_score"] = _quality_record(
        catalyst_inputs,
        core_available=any((revenue_yoy_ready, profit_yoy_ready, ocf_yoy_ready)),
    )
    qualities["technology_score"] = _quality_record(
        {
            "reported_rd_intensity": finite("rd_intensity"),
            "peer_rd_distribution": _finite_sequence_count(context.get("rd_intensity_population"))
            >= MIN_SECTOR_COMPANIES,
            "commercial_growth": finite("trend_growth") and finite("growth_slope"),
            "roic_and_wacc": roic_wacc_ready,
            "gross_margin": finite("gross_margin"),
        }
    )
    qualities["business_model_score"] = _quality_record(
        {
            "revenue_asset_history": revenue_asset_ready,
            "margin_trajectory": finite("margin_trajectory"),
            "accounting_outcomes": accounting_complete,
            "share_dilution": finite("share_dilution_1yr"),
        }
    )
    return qualities


def _evidence_record(
    *,
    code: str,
    key: str,
    score: float,
    as_of: str,
    details: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> dict[str, Any]:
    level = str(quality.get("level") or "missing")
    summary = f"{key}={score:.1f};model={MODEL_ID};evidence_level={level}"
    record_details = dict(details)
    record_details["evidence_quality"] = dict(quality)
    return {
        "score": score,
        "evidence_level": level,
        "evidence": {
            "source": SOURCE_LABEL,
            "evidence_id": f"{MODEL_ID}:{key}:{code}:{as_of.replace('-', '')}",
            "as_of": as_of,
            "summary": summary,
        },
        "details": record_details,
    }


def derive_company_evidence(
    metric: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    fallback_industry_growth: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Return every Patch 6 research score that can be derived observably."""
    code = str(metric.get("code") or "")
    candidate_dates: list[date] = []
    for raw_date in (
        metric.get("financial_indicator_as_of"),
        (
            metric.get("market_coldness_score_evidence", {}).get("as_of")
            if isinstance(metric.get("market_coldness_score_evidence"), Mapping)
            else None
        ),
    ):
        if isinstance(raw_date, str):
            try:
                candidate_dates.append(date.fromisoformat(raw_date))
            except ValueError:
                pass
    years = metric.get("revenue_years")
    if isinstance(years, Sequence) and not isinstance(years, (str, bytes)):
        valid_years = [int(year) for year in years if str(year).isdigit() and 1900 <= int(year) <= 9999]
        if valid_years:
            candidate_dates.append(date(max(valid_years), 12, 31))
    if not candidate_dates:
        raise ValueError(f"quantitative evidence has no valid as-of date:{code}")
    as_of = max(candidate_dates).isoformat()

    industry_score, industry_details = _score_industry_durability(context, fallback_industry_growth)
    accounting_score, accounting_details = _score_accounting(metric)
    management_score, management_details = _score_management_outcomes(metric)
    moat_score, moat_details = _score_moat(metric, context, accounting_score)
    moat_durability_score, moat_durability_details = _score_moat_durability(metric, moat_score)
    growth_quality_score, growth_quality_details = _score_growth_quality(metric, accounting_score)
    growth_sustainability_score, growth_sustainability_details = _score_growth_sustainability(
        metric, moat_score, industry_score
    )
    runway_score, runway_details = _score_runway(metric, context, moat_durability_score, industry_score)
    bubble_score, bubble_details = _score_industry_bubble(context)
    type3_bubble_score, type3_bubble_details = _score_type3_bubble(metric, bubble_score)
    catalyst_score, catalyst_details = _score_catalyst(metric, context)
    technology_score, technology_details = _score_technology(metric, context)
    business_model_score, business_model_details = _score_business_model(metric, accounting_score)

    values = {
        "industry_durability_score": (industry_score, industry_details),
        "accounting_integrity_score": (accounting_score, accounting_details),
        "management_alignment_score": (management_score, management_details),
        "moat_score": (moat_score, moat_details),
        "moat_durability_score": (moat_durability_score, moat_durability_details),
        "growth_quality_score": (growth_quality_score, growth_quality_details),
        "growth_sustainability_score": (growth_sustainability_score, growth_sustainability_details),
        "runway_score": (runway_score, runway_details),
        "industry_bubble_score": (bubble_score, bubble_details),
        "type3_bubble_score": (type3_bubble_score, type3_bubble_details),
        "catalyst_score": (catalyst_score, catalyst_details),
        "technology_score": (technology_score, technology_details),
        "business_model_score": (business_model_score, business_model_details),
    }
    qualities = _build_evidence_qualities(
        metric,
        context,
        fallback_industry_growth=fallback_industry_growth,
    )
    return {
        key: _evidence_record(
            code=code,
            key=key,
            score=score,
            as_of=as_of,
            details=details,
            quality=qualities[key],
        )
        for key, (score, details) in values.items()
    }


def enrich_metrics(
    metrics: Sequence[dict[str, Any]],
    benchmarks: Mapping[str, Mapping[str, Any]],
    *,
    target_codes: Collection[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Attach fallback scores without overwriting explicit primary evidence.

    Returns ``(sector_context, evidence_by_code)``.  ``metrics`` are mutated in
    place because the scoring boundary already owns these transient records.
    """
    selected = None if target_codes is None else {str(code) for code in target_codes}
    contexts = build_company_contexts(metrics, target_codes=selected)
    evidence_by_code: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        code = str(metric.get("code") or "")
        if selected is not None and code not in selected:
            continue
        industry = str(metric.get("industry") or "DEFAULT")
        company_benchmarks = benchmarks
        benchmark_selector = getattr(benchmarks, "for_code", None)
        if callable(benchmark_selector):
            selected_benchmarks = benchmark_selector(code)
            if isinstance(selected_benchmarks, Mapping):
                company_benchmarks = selected_benchmarks
        benchmark = company_benchmarks.get(industry, {}) if isinstance(company_benchmarks, Mapping) else {}
        fallback_growth = _finite(benchmark.get("median_cagr")) if isinstance(benchmark, Mapping) else None
        context = contexts.get(str(metric.get("code") or ""), {})
        try:
            evidence = derive_company_evidence(
                metric,
                context,
                fallback_industry_growth=fallback_growth,
            )
        except ValueError as exc:
            # A synthetic/unit-test row or quarantined source row can reach the
            # table boundary without any reporting date.  Do not stamp today's
            # date onto nonexistent evidence.  Production eligibility gates
            # require dated annual/interim records, so a real eligible company
            # must never take this branch.
            if not str(exc).startswith("quantitative evidence has no valid as-of date:"):
                raise
            metric["quantitative_evidence"] = {}
            metric["quantitative_evidence_status"] = "unavailable_no_reporting_date"
            metric["quantitative_evidence_levels"] = {}
            evidence_by_code[code] = {}
            continue
        evidence_by_code[code] = evidence
        effective_levels: dict[str, str] = {}
        for key, payload in evidence.items():
            # A dated primary/research adapter remains authoritative.  This
            # formula fills a production score only when every declared proxy
            # input is present.  Partial/missing diagnostics remain available
            # under ``quantitative_evidence`` but cannot masquerade as a normal
            # score merely because missing inputs produced a finite default.
            existing_score = _finite(metric.get(key))
            existing_evidence = metric.get(f"{key}_evidence")
            if existing_score is not None and isinstance(existing_evidence, Mapping):
                existing_level = str(metric.get(f"{key}_evidence_level") or "primary")
                effective_level = existing_level if existing_level in EVIDENCE_LEVELS else "primary"
            else:
                effective_level = str(payload.get("evidence_level") or "missing")
            if effective_level == "derived_proxy" and (
                existing_score is None or not isinstance(existing_evidence, Mapping)
            ):
                metric[key] = payload["score"]
                metric[f"{key}_evidence"] = payload["evidence"]
            metric[f"{key}_evidence_level"] = effective_level
            effective_levels[key] = effective_level
        metric["quantitative_evidence"] = evidence
        metric["quantitative_evidence_levels"] = effective_levels
        if effective_levels and all(level in {"primary", "derived_proxy"} for level in effective_levels.values()):
            metric["quantitative_evidence_status"] = "complete"
        elif effective_levels and all(level == "missing" for level in effective_levels.values()):
            metric["quantitative_evidence_status"] = "missing"
        else:
            metric["quantitative_evidence_status"] = "partial"
    return contexts, evidence_by_code


__all__ = [
    "MIN_COMPARABLE_COVERAGE",
    "MIN_SECTOR_COMPANIES",
    "MODEL_ID",
    "EVIDENCE_LEVELS",
    "build_company_contexts",
    "build_sector_context",
    "derive_company_evidence",
    "enrich_metrics",
]
