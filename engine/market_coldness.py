"""Quantitative, traceable Type-2 market-coldness evidence.

Patch 6 separates market sentiment/cycle coldness (2c) from valuation (2d),
so this model consumes only price and trading-activity evidence.  The public
fields cannot prove media despair, analyst capitulation or a ten-year
valuation trough; automated scores therefore never enter the 9-10 band.

Missing observations, young listings, zero-trade anomalies and incomplete
whole-market batches produce no decision score.  They are never converted to
zero and never become artificial "extreme cold" evidence.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Mapping, MutableMapping
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from data.market_coldness import MarketColdnessRecord, MarketColdnessSnapshot


MARKET_COLDNESS_MODEL_ID = "patch6-type2c-quantity-price-v1"
MAX_COLDNESS_SCORE = 8.0
MAX_SCORE_WITHOUT_VOLUME_RATIO = 7.5
MIN_LISTING_AGE_DAYS = 120
MAX_RETRIEVAL_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_SESSION_AGE_DAYS = 10
MAX_FUTURE_SKEW_SECONDS = 5 * 60
MIN_SOURCE_FIELD_COVERAGE = 0.90
MIN_CROSS_SECTION_RECORDS = 1_000
MIN_BOARD_TURNOVER_RECORDS = 200
DIAGNOSTIC_CODE_LIMIT = 20

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SH_SZ_CODE = re.compile(r"[036][0-9]{5}")

_ABSOLUTE_BANDS: Mapping[str, tuple[tuple[float, float], ...]] = {
    "change_60d_pct": (
        (-35.0, 9.5),
        (-25.0, 9.0),
        (-15.0, 8.0),
        (-8.0, 7.0),
        (0.0, 5.5),
        (10.0, 4.0),
        (20.0, 3.0),
        (35.0, 2.0),
        (60.0, 1.0),
    ),
    "change_ytd_pct": (
        (-45.0, 9.5),
        (-30.0, 9.0),
        (-20.0, 8.0),
        (-10.0, 7.0),
        (0.0, 5.5),
        (15.0, 4.5),
        (30.0, 3.0),
        (50.0, 2.0),
        (80.0, 1.0),
    ),
    "turnover_rate_pct": (
        (0.30, 9.0),
        (0.70, 8.0),
        (1.50, 7.0),
        (3.00, 5.5),
        (5.00, 4.5),
        (8.00, 3.5),
        (15.0, 2.0),
        (30.0, 1.0),
    ),
    "volume_ratio": (
        (0.40, 9.0),
        (0.70, 8.0),
        (0.90, 6.5),
        (1.10, 5.5),
        (1.50, 4.0),
        (2.50, 2.5),
        (5.00, 1.0),
    ),
}

_BASE_WEIGHTS: Mapping[str, float] = {
    "change_60d_pct": 0.45,
    "change_ytd_pct": 0.25,
    "turnover_rate_pct": 0.20,
    "volume_ratio": 0.10,
}
_REQUIRED_METRICS = ("change_60d_pct", "change_ytd_pct", "turnover_rate_pct")


class MarketColdnessScoringError(ValueError):
    """A supposedly available coldness snapshot violates the score contract."""


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _interpolate(value: float, bands: tuple[tuple[float, float], ...]) -> float:
    if value <= bands[0][0]:
        return bands[0][1]
    for (left_x, left_score), (right_x, right_score) in zip(bands, bands[1:]):
        if value <= right_x:
            width = right_x - left_x
            if width <= 0:
                raise MarketColdnessScoringError("market-coldness bands are not strictly increasing")
            fraction = (value - left_x) / width
            return left_score + fraction * (right_score - left_score)
    return bands[-1][1]


def _parse_retrieved_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MarketColdnessScoringError("market-coldness retrieval timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketColdnessScoringError("market-coldness retrieval timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketColdnessScoringError("market-coldness retrieval timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_session(value: date | str | None) -> date | None:
    if isinstance(value, datetime):
        raise ValueError("as_of_session must be a date or ISO date string")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("as_of_session must be an ISO date") from exc
        if parsed.isoformat() != value.strip():
            raise ValueError("as_of_session must be an ISO date")
        return parsed
    if value is None:
        return None
    raise ValueError("as_of_session must be a date or ISO date string")


def _rank_score(value: float, sorted_values: list[float]) -> float:
    """Return 1..9: unique lowest=9, unique highest=1, all ties=5."""
    count = len(sorted_values)
    if count < 2:
        return 5.0
    lower = bisect.bisect_left(sorted_values, value)
    upper = bisect.bisect_right(sorted_values, value)
    equal = upper - lower
    if equal == count:
        return 5.0
    greater = count - upper
    score = 1.0 + 8.0 * (greater + 0.5 * (equal - 1)) / (count - 1)
    return max(1.0, min(9.0, score))


def _board(code: str) -> str:
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith("6"):
        return "SH_MAIN"
    if code.startswith(("0", "3")):
        return "SZ_MAIN"
    raise MarketColdnessScoringError(f"unsupported A-share board code: {code}")


def _listing_date(record: MarketColdnessRecord) -> date | None:
    if record.listing_date is None:
        return None
    try:
        listed = date.fromisoformat(record.listing_date)
    except ValueError as exc:
        raise MarketColdnessScoringError(f"invalid listing date for {record.code}") from exc
    if listed.isoformat() != record.listing_date:
        raise MarketColdnessScoringError(f"invalid listing date for {record.code}")
    return listed


def _validate_record_identity(record: MarketColdnessRecord) -> None:
    code = record.code
    if not isinstance(code, str) or _SH_SZ_CODE.fullmatch(code) is None:
        raise MarketColdnessScoringError(f"unsupported A-share identity: {code!r}")
    expected_exchange = "SH" if code.startswith("6") else "SZ"
    expected_market_id = 1 if expected_exchange == "SH" else 0
    if (
        record.exchange != expected_exchange
        or isinstance(record.eastmoney_market_id, bool)
        or not isinstance(record.eastmoney_market_id, int)
        or record.eastmoney_market_id != expected_market_id
    ):
        raise MarketColdnessScoringError(
            f"market-coldness identity mismatch for {code}: "
            f"exchange={record.exchange!r}, market_id={record.eastmoney_market_id!r}"
        )


def _bound_listed_quote_codes(values: Iterable[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError("listed_quote_codes must be an iterable of canonical SH/SZ codes")
    seen: set[str] = set()
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError("listed_quote_codes must be an iterable of canonical SH/SZ codes") from exc
    for raw_code in iterator:
        if not isinstance(raw_code, str) or _SH_SZ_CODE.fullmatch(raw_code) is None:
            raise ValueError("listed_quote_codes must contain only canonical SH/SZ codes")
        if raw_code in seen:
            raise ValueError(f"listed_quote_codes contains a duplicate code: {raw_code}")
        seen.add(raw_code)
    if not seen:
        raise ValueError("listed_quote_codes must contain at least one canonical SH/SZ code")
    return frozenset(seen)


def _bounded_code_diagnostics(
    listed_quote_codes: frozenset[str] | None,
    isolated_future_listing_codes: list[str],
    excluded_unbound_source_codes: list[str],
) -> dict[str, Any]:
    isolated = sorted(isolated_future_listing_codes)
    excluded = sorted(excluded_unbound_source_codes)
    return {
        "listed_quote_binding_count": len(listed_quote_codes) if listed_quote_codes is not None else None,
        "isolated_future_listing_count": len(isolated),
        "isolated_future_listing_codes": isolated[:DIAGNOSTIC_CODE_LIMIT],
        "isolated_future_listing_codes_truncated": len(isolated) > DIAGNOSTIC_CODE_LIMIT,
        "excluded_unbound_source_record_count": len(excluded),
        "excluded_unbound_source_record_codes": excluded[:DIAGNOSTIC_CODE_LIMIT],
        "excluded_unbound_source_record_codes_truncated": len(excluded) > DIAGNOSTIC_CODE_LIMIT,
        "diagnostic_code_limit": DIAGNOSTIC_CODE_LIMIT,
    }


def _business_days_ytd(session: date) -> int:
    current = date(session.year, 1, 1)
    result = 0
    while current <= session:
        result += current.weekday() < 5
        current = current.fromordinal(current.toordinal() + 1)
    return result


def _source_metric_coverage(snapshot: MarketColdnessSnapshot, metric: str) -> float | None:
    coverage = snapshot.coverage.by_metric.get(metric)
    return _finite_number(coverage.coverage_rate) if coverage is not None else None


def _validated_values(record: MarketColdnessRecord) -> dict[str, float | None]:
    values = {metric: _finite_number(getattr(record, metric)) for metric in _BASE_WEIGHTS}
    for metric in _REQUIRED_METRICS:
        if values[metric] is None:
            return values
    for metric in ("change_60d_pct", "change_ytd_pct"):
        value = values[metric]
        if value is not None and (value < -100.0 or value > 10_000.0):
            raise MarketColdnessScoringError(f"implausible return metric for {record.code}:{metric}")
    turnover = values["turnover_rate_pct"]
    volume = values["volume_ratio"]
    if turnover is not None and turnover < 0 or volume is not None and volume < 0:
        raise MarketColdnessScoringError(f"negative activity metric for {record.code}")
    return values


def build_market_coldness_evidence(
    snapshot: MarketColdnessSnapshot,
    *,
    as_of_session: date | str | None,
    listed_quote_codes: Iterable[str] | None = None,
    now: datetime | None = None,
    min_cross_section_records: int = MIN_CROSS_SECTION_RECORDS,
    min_board_turnover_records: int = MIN_BOARD_TURNOVER_RECORDS,
    diagnostics: MutableMapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build per-company 2c evidence from one verified full-market batch.

    ``as_of_session`` and ``listed_quote_codes`` must come from the same
    independently validated quote snapshot.  Eastmoney can include announced
    securities before their listing day; a future-dated source row is isolated
    only when its code is absent from that independent listed-code set.  A
    future date for a bound listed code remains a hard validation error.

    Omitting ``listed_quote_codes`` preserves the legacy strict behaviour:
    every future listing date fails closed because there is no independent
    identity boundary that can prove the row is merely not yet listed.
    """

    if diagnostics is not None:
        diagnostics.clear()

    def unavailable(reason: str, **details: Any) -> dict[str, dict[str, Any]]:
        if diagnostics is not None:
            diagnostics.update(
                {
                    "evidence_available": False,
                    "evidence_reason": reason,
                    **details,
                }
            )
        return {}

    for name, value in (
        ("min_cross_section_records", min_cross_section_records),
        ("min_board_turnover_records", min_board_turnover_records),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    bound_codes = _bound_listed_quote_codes(listed_quote_codes)
    session = _parse_session(as_of_session)
    if not snapshot.available:
        return unavailable("source_unavailable")
    if session is None:
        return unavailable("missing_bound_as_of_session")
    if snapshot.universe_coverage_rate != 1.0:
        return unavailable("incomplete_universe")
    if snapshot.fetched_count != len(snapshot.records) or snapshot.total_expected != len(snapshot.records):
        raise MarketColdnessScoringError("market-coldness snapshot count mismatch")
    if snapshot.coverage.total_records != len(snapshot.records):
        raise MarketColdnessScoringError("market-coldness coverage count mismatch")
    seen_source_codes: set[str] = set()
    for record in snapshot.records:
        _validate_record_identity(record)
        if record.code in seen_source_codes:
            raise MarketColdnessScoringError(f"duplicate market-coldness code: {record.code}")
        seen_source_codes.add(record.code)
    for metric in (*_REQUIRED_METRICS, "listing_date"):
        coverage = _source_metric_coverage(snapshot, metric)
        if coverage is None or coverage < MIN_SOURCE_FIELD_COVERAGE:
            return unavailable(
                "insufficient_source_metric_coverage",
                deficient_metric=metric,
                metric_coverage=coverage,
            )

    retrieved = _parse_retrieved_at(snapshot.retrieved_at)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (current - retrieved).total_seconds()
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS or age_seconds > MAX_RETRIEVAL_AGE_SECONDS:
        return unavailable("stale_or_future_retrieval", retrieval_age_seconds=age_seconds)
    retrieved_shanghai = retrieved.astimezone(_SHANGHAI)
    current_shanghai = current.astimezone(_SHANGHAI)
    if session != retrieved_shanghai.date():
        return unavailable(
            "session_retrieval_mismatch",
            retrieval_session=retrieved_shanghai.date().isoformat(),
            requested_session=session.isoformat(),
        )
    if session > current_shanghai.date() or (current_shanghai.date() - session).days > MAX_SESSION_AGE_DAYS:
        return unavailable("session_current_date_mismatch")
    # Intraday f8/f10 are incomplete.  A same-day snapshot can be diagnostic,
    # but it must not create an automatic Patch-6 trigger before the close.
    if session == current_shanghai.date() and current_shanghai.time() < time(15, 15):
        return unavailable(
            "intraday_before_close",
            decision_eligible_after="15:15:00 Asia/Shanghai",
        )

    records_by_code: dict[str, tuple[MarketColdnessRecord, date | None, dict[str, float | None]]] = {}
    reference_records: list[tuple[MarketColdnessRecord, dict[str, float | None]]] = []
    isolated_future_listing_codes: list[str] = []
    excluded_unbound_source_codes: list[str] = []
    for record in snapshot.records:
        listed = _listing_date(record)
        if listed is not None and listed > session:
            if bound_codes is not None and record.code not in bound_codes:
                isolated_future_listing_codes.append(record.code)
                continue
            raise MarketColdnessScoringError(f"future listing date for {record.code}")
        if bound_codes is not None and record.code not in bound_codes:
            excluded_unbound_source_codes.append(record.code)
            continue
        values = _validated_values(record)
        records_by_code[record.code] = (record, listed, values)
        if (
            listed is None
            or (session - listed).days < MIN_LISTING_AGE_DAYS
            or listed.year == session.year
            or any(values[metric] is None for metric in _REQUIRED_METRICS)
            or values["turnover_rate_pct"] == 0
            or (values["volume_ratio"] == 0 and values["turnover_rate_pct"] == 0)
        ):
            continue
        reference_records.append((record, values))

    if len(reference_records) < min_cross_section_records:
        return unavailable(
            "insufficient_reference_cross_section",
            reference_records=len(reference_records),
            minimum_reference_records=min_cross_section_records,
            **_bounded_code_diagnostics(
                bound_codes,
                isolated_future_listing_codes,
                excluded_unbound_source_codes,
            ),
        )

    global_sections: dict[str, list[float]] = {}
    for metric in _BASE_WEIGHTS:
        global_sections[metric] = sorted(
            value for _record, values in reference_records if (value := values[metric]) is not None
        )
    board_turnover_sections: dict[str, list[float]] = {}
    board_record_counts: dict[str, int] = {}
    for board in {"SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"}:
        board_record_counts[board] = sum(_board(record.code) == board for record, _values in reference_records)
        board_turnover_sections[board] = sorted(
            value
            for record, values in reference_records
            if _board(record.code) == board and (value := values["turnover_rate_pct"]) is not None
        )

    ytd_reliability = min(1.0, _business_days_ytd(session) / 60.0)
    weights = dict(_BASE_WEIGHTS)
    weights["change_ytd_pct"] *= ytd_reliability
    result: dict[str, dict[str, Any]] = {}
    for code, (_record, listed, values) in records_by_code.items():
        if (
            listed is None
            or (session - listed).days < MIN_LISTING_AGE_DAYS
            or listed.year == session.year
            or any(values[metric] is None for metric in _REQUIRED_METRICS)
            or values["turnover_rate_pct"] == 0
        ):
            continue
        available_metrics = [metric for metric, value in values.items() if value is not None]
        absolute_components = {
            metric: _interpolate(values[metric], _ABSOLUTE_BANDS[metric])  # type: ignore[arg-type]
            for metric in available_metrics
        }
        relative_components: dict[str, float] = {}
        relative_sample_sizes: dict[str, int] = {}
        for metric in available_metrics:
            section = global_sections[metric]
            minimum_section_records = min_cross_section_records
            section_population = len(reference_records)
            if metric == "turnover_rate_pct":
                board = _board(code)
                board_section = board_turnover_sections[board]
                if len(board_section) >= min_board_turnover_records:
                    section = board_section
                    minimum_section_records = min_board_turnover_records
                    section_population = board_record_counts[board]
            reference_coverage = len(section) / section_population
            source_coverage = _source_metric_coverage(snapshot, metric)
            if (
                len(section) >= minimum_section_records
                and reference_coverage >= MIN_SOURCE_FIELD_COVERAGE
                and source_coverage is not None
                and source_coverage >= MIN_SOURCE_FIELD_COVERAGE
            ):
                relative_components[metric] = _rank_score(values[metric], section)  # type: ignore[arg-type]
                relative_sample_sizes[metric] = len(section)

        metric_scores = {
            metric: (
                0.80 * absolute_components[metric] + 0.20 * relative_components[metric]
                if metric in relative_components
                else absolute_components[metric]
            )
            for metric in available_metrics
        }
        total_weight = sum(weights[metric] for metric in available_metrics)
        raw_score = sum(metric_scores[metric] * weights[metric] for metric in available_metrics) / total_weight
        price_weight = weights["change_60d_pct"] + weights["change_ytd_pct"]
        price_score = (
            metric_scores["change_60d_pct"] * weights["change_60d_pct"]
            + metric_scores["change_ytd_pct"] * weights["change_ytd_pct"]
        ) / price_weight

        score_cap = MAX_COLDNESS_SCORE if values["volume_ratio"] is not None else MAX_SCORE_WITHOUT_VOLUME_RATIO
        caps: list[str] = [f"evidence_cap={score_cap:.1f}"]
        if absolute_components["change_60d_pct"] <= 3.0:
            score_cap = min(score_cap, 3.0)
            caps.append("60d_hot_cap=3.0")
        elif price_score < 5.0:
            score_cap = min(score_cap, 4.9)
            caps.append("price_coldness_lt5_cap=4.9")
        elif price_score < 6.0:
            score_cap = min(score_cap, 6.9)
            caps.append("price_coldness_lt6_cap=6.9")
        score = round(max(1.0, min(score_cap, raw_score)), 1)

        volume_text = f"{values['volume_ratio']:.2f}" if values["volume_ratio"] is not None else "缺失"
        summary = (
            f"量价冷度;60日{values['change_60d_pct']:.1f}%;YTD{values['change_ytd_pct']:.1f}%;"
            f"换手{values['turnover_rate_pct']:.2f}%;量比{volume_text};上限{score_cap:.1f}"
        )
        evidence_id = f"{MARKET_COLDNESS_MODEL_ID}:{code}:{session.strftime('%Y%m%d')}"
        result[code] = {
            "market_coldness_score": score,
            "market_coldness_score_evidence": {
                "source": f"{snapshot.source}; {snapshot.source_url}",
                "evidence_id": evidence_id,
                "as_of": session.isoformat(),
                "summary": summary,
            },
            "components": {
                "raw_values": values,
                "absolute": {key: round(value, 6) for key, value in absolute_components.items()},
                "relative": {key: round(value, 6) for key, value in relative_components.items()},
                "relative_sample_sizes": relative_sample_sizes,
                "metric_scores": {key: round(value, 6) for key, value in metric_scores.items()},
                "weights": {key: round(weights[key], 6) for key in available_metrics},
                "ytd_reliability": round(ytd_reliability, 6),
                "price_score": round(price_score, 6),
                "raw_score": round(raw_score, 6),
                "score_cap": score_cap,
                "caps": caps,
                "board": _board(code),
                "retrieved_at": snapshot.retrieved_at,
                "as_of_session": session.isoformat(),
                "source_url": snapshot.source_url,
            },
        }
    if diagnostics is not None:
        diagnostics.update(
            {
                "evidence_available": bool(result),
                "evidence_reason": "available" if result else "no_eligible_records",
                "evidence_count": len(result),
                **_bounded_code_diagnostics(
                    bound_codes,
                    isolated_future_listing_codes,
                    excluded_unbound_source_codes,
                ),
            }
        )
    return result


__all__ = [
    "MARKET_COLDNESS_MODEL_ID",
    "MAX_COLDNESS_SCORE",
    "MAX_SCORE_WITHOUT_VOLUME_RATIO",
    "MarketColdnessScoringError",
    "build_market_coldness_evidence",
]
