"""Run the production snapshot, full-market analysis, and fixed-seed audit."""

from __future__ import annotations

import argparse
import bisect
from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence
from collections import Counter
import hashlib
import json
import math
import re
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from data.cache import SafeFileCache
from data.fetcher import DataFetcher
from data.market_coldness import (
    EASTMONEY_CLIST_ENDPOINT,
    EASTMONEY_SOURCE,
    archive_market_coldness_session_snapshot,
    fetch_market_coldness_snapshot,
    load_market_coldness_session_snapshot,
)
from data.trading_calendar import a_share_trading_days_ytd, is_a_share_trading_day
from data.growth_evidence import fetch_growth_evidence_batch
from data.patch4_evidence import fetch_patch4_evidence_batch
from data.quality_history import fetch_quality_history_batch
from data.research_reports import fetch_research_reports_batch
from data.snapshot import (
    DEFAULT_SNAPSHOT_PATH,
    MAX_STALE_AGE_SECONDS,
    SNAPSHOT_SCHEMA_VERSION,
    get_market_snapshot,
    save_market_snapshot,
)
from engine.audit import audit_random_sample, audit_state_hashes, write_audit_artifacts
from engine.buy_screener import TYPE_WEIGHTS
from engine.dcf import ReportingPeriodContract
from engine.market_coldness import (
    MARKET_COLDNESS_DIAGNOSTICS_SCHEMA_VERSION,
    MARKET_COLDNESS_EVIDENCE_SCHEMA_VERSION,
    MARKET_COLDNESS_MODEL_ID,
    MAX_COLDNESS_SCORE,
    MAX_SCORE_WITHOUT_VOLUME_RATIO,
    MIN_BOARD_TURNOVER_RECORDS,
    MIN_CROSS_SECTION_RECORDS,
    MIN_LISTING_AGE_DAYS,
    MIN_SOURCE_FIELD_COVERAGE,
    build_market_coldness_evidence,
)
from engine.pipeline import run_market_analysis
from engine.quantitative_evidence import (
    MODEL_ID as QUANTITATIVE_EVIDENCE_MODEL_ID,
    validate_quantitative_evidence_record,
)
from engine.type7_patch6 import MODEL_ID as _PATCH6_TYPE7_MODEL_ID
from engine.valuation_status import DCF_SKIP_ECONOMIC_NOT_APPLICABLE


_STRICT_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_MARKET_COLDNESS_UNAVAILABLE_POLICY = "continue_with_insufficient_evidence"
_MARKET_COLDNESS_REFERENCE_ARTIFACT_SCHEMA_VERSION = 2
_MARKET_COLDNESS_INTRADAY_START_TIME = datetime_time(9, 15)
_MARKET_COLDNESS_DECISION_READY_TIME = datetime_time(16, 15)
_MARKET_COLDNESS_MAX_SOURCE_FUTURE_SKEW_SECONDS = 5 * 60
_MARKET_COLDNESS_NOT_APPLICABLE_REASONS = frozenset(
    {
        "listed_in_current_year",
        "listing_history_lt_120_days",
    }
)
_MARKET_COLDNESS_RAW_KEYS = frozenset(
    {
        "change_60d_pct",
        "change_ytd_pct",
        "turnover_rate_pct",
        "volume_ratio",
    }
)
_MARKET_COLDNESS_BASE_WEIGHTS = {
    "change_60d_pct": 0.45,
    "change_ytd_pct": 0.25,
    "turnover_rate_pct": 0.20,
    "volume_ratio": 0.10,
}
# Deliberately duplicated from the scoring engine.  This is an independent
# release replay: changing the scoring implementation without changing this
# contract makes the formal audit fail instead of silently blessing itself.
_MARKET_COLDNESS_ABSOLUTE_BANDS = {
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
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TYPE_KEYS = tuple(f"type{index}" for index in range(1, 8))
_QUANTITATIVE_EVIDENCE_KEYS = frozenset(
    {
        "industry_durability_score",
        "accounting_integrity_score",
        "management_alignment_score",
        "moat_score",
        "moat_durability_score",
        "growth_quality_score",
        "growth_sustainability_score",
        "runway_score",
        "industry_bubble_score",
        "type3_bubble_score",
        "catalyst_score",
        "technology_score",
        "business_model_score",
    }
)
_QUANTITATIVE_RECORD_LEVELS = frozenset({"primary", "derived_proxy", "partial", "missing"})
_QUANTITATIVE_EFFECTIVE_LEVELS = _QUANTITATIVE_RECORD_LEVELS
_DECISION_SCHEMA_VERSION = 1
_DECISION_MODEL_ID = "buy-decision-bounds-v1"
_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "model_id",
        "decision_complete",
        "decision_basis",
        "score_lower_bound",
        "score_upper_bound",
        "veto_state",
        "potentially_triggerable",
        "missing_dimensions",
    }
)
_DECISION_MISSING_REQUIREMENTS_REASON = "_decision_missing_requirements"
_DECISION_MISSING_REQUIREMENTS = (
    "patch7_current_price",
    "patch7_optimistic_upper",
)
_DECISION_PATCH7_PRICE_GATE_TYPES = frozenset({"type2", "type3", "type4", "type7"})
_DECISION_FACT_UNSET = object()
_DECISION_MARKET_CONTEXT_FIELDS = frozenset({"tradable", "reference_price", "risk_status"})
_DECISION_POTENTIAL_VETO_DIMENSIONS = {
    "type1": frozenset({"1a", "1b"}),
    "type2": frozenset({"2a", "2b", "2c"}),
    "type3": frozenset({"3a", "3d", "3e"}),
    "type4": frozenset({"4c", "4e", "4f"}),
    "type5": frozenset(),
    "type6": frozenset({"6a", "6b", "6c", "6d"}),
    "type7": frozenset({"7c"}),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="fetch a new candidate instead of using a valid cache")
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=Path("audit"))
    parser.add_argument(
        "--require-complete-evidence",
        action="store_true",
        help=(
            "fail unless every framework sub-score is structurally valid, every "
            "incomplete framework has a specific reason, and no quantitative "
            "evidence item is partial or missing"
        ),
    )
    return parser


def _comparison_quality(snapshot: object) -> dict | None:
    """Use the last promoted quality generation as the regression baseline."""
    for field in ("previous_analysis_quality", "analysis_quality"):
        value = getattr(snapshot, field, None)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return None


def _refresh_completed(force_refresh: bool, snapshot_source: object) -> bool:
    """A requested refresh succeeds only when the audited candidate is network data."""
    return not force_refresh or snapshot_source == "network"


def _snapshot_trade_session(snapshot: object) -> str | None:
    validation = getattr(snapshot, "validation", None)
    source_trade_dates = validation.get("trading_source_trade_dates") if isinstance(validation, Mapping) else None
    if (
        isinstance(source_trade_dates, list)
        and len(source_trade_dates) == 1
        and isinstance(source_trade_dates[0], str)
        and source_trade_dates[0]
    ):
        return source_trade_dates[0]
    return None


def _market_coldness_interpolate(value: float, bands: tuple[tuple[float, float], ...]) -> float:
    if value <= bands[0][0]:
        return bands[0][1]
    for (left_x, left_score), (right_x, right_score) in zip(bands, bands[1:]):
        if value <= right_x:
            return left_score + (value - left_x) / (right_x - left_x) * (right_score - left_score)
    return bands[-1][1]


def _market_coldness_business_days_ytd(session: date) -> int:
    # Kept as a private compatibility name for existing audit tests; the
    # implementation now counts pinned SSE/SZSE sessions, not weekdays.
    return a_share_trading_days_ytd(session)


def _release_market_coldness_completed_session(retrieved_at: datetime) -> date | None:
    """Independently derive the complete session for formal replay."""

    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        return None
    local = retrieved_at.astimezone(_SHANGHAI)
    local_time = local.time().replace(tzinfo=None)
    local_date = local.date()
    if is_a_share_trading_day(local_date):
        if local_time >= _MARKET_COLDNESS_DECISION_READY_TIME:
            return local_date
        if local_time >= _MARKET_COLDNESS_INTRADAY_START_TIME:
            return None
    candidate = local_date - timedelta(days=1)
    while not is_a_share_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _market_coldness_board(code: str) -> str:
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith("6"):
        return "SH_MAIN"
    return "SZ_MAIN"


def _market_coldness_rank_score(value: float, sorted_values: list[float]) -> float:
    count = len(sorted_values)
    if count < 2:
        return 5.0
    lower = bisect.bisect_left(sorted_values, value)
    upper = bisect.bisect_right(sorted_values, value)
    equal = upper - lower
    if equal == count:
        return 5.0
    greater = count - upper
    return max(1.0, min(9.0, 1.0 + 8.0 * (greater + 0.5 * (equal - 1)) / (count - 1)))


def _canonical_market_coldness_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _build_market_coldness_reference_artifact(
    snapshot: object,
    *,
    listed_codes: Sequence[str],
    as_of_session: str,
) -> dict[str, object]:
    """Capture every normalized source row needed for independent replay."""

    records = getattr(snapshot, "records", ())
    if not isinstance(records, tuple):
        raise RuntimeError("market-coldness source records are unavailable")
    rows: list[list[object]] = []
    for record in sorted(records, key=lambda item: str(getattr(item, "code", ""))):
        upstream = getattr(record, "upstream_fields", None)
        rows.append(
            [
                getattr(record, "code", None),
                getattr(record, "listing_date", None),
                getattr(record, "change_60d_pct", None),
                getattr(record, "change_ytd_pct", None),
                getattr(record, "turnover_rate_pct", None),
                getattr(record, "volume_ratio", None),
                upstream.get("f124") if isinstance(upstream, Mapping) else None,
            ]
        )
    artifact: dict[str, object] = {
        "schema_version": _MARKET_COLDNESS_REFERENCE_ARTIFACT_SCHEMA_VERSION,
        "model_id": MARKET_COLDNESS_MODEL_ID,
        "source": getattr(snapshot, "source", None),
        "source_url": getattr(snapshot, "source_url", None),
        "retrieved_at": getattr(snapshot, "retrieved_at", None),
        "as_of_session": as_of_session,
        "listed_codes": sorted(listed_codes),
        "source_record_count": len(rows),
        "records": rows,
    }
    # Constructing the artifact is intentionally separate from validating it;
    # the independent gate below must reject malformed normalized source data.
    return artifact


def _replay_market_coldness_reference_artifact(
    artifact: object,
    *,
    eligible_codes: Sequence[str],
    as_of_session: str,
    min_cross_section_records: int = MIN_CROSS_SECTION_RECORDS,
    min_board_turnover_records: int = MIN_BOARD_TURNOVER_RECORDS,
) -> dict[str, object]:
    """Rebuild applicability, cross-sectional ranks and scores from raw rows."""

    fields = {
        "schema_version",
        "model_id",
        "source",
        "source_url",
        "retrieved_at",
        "as_of_session",
        "listed_codes",
        "source_record_count",
        "records",
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (min_cross_section_records, min_board_turnover_records)
    ):
        raise RuntimeError("market-coldness replay thresholds are invalid")
    if not isinstance(artifact, Mapping) or set(artifact) != fields:
        raise RuntimeError("market-coldness reference artifact has an invalid shape")
    if (
        artifact.get("schema_version") != _MARKET_COLDNESS_REFERENCE_ARTIFACT_SCHEMA_VERSION
        or artifact.get("model_id") != MARKET_COLDNESS_MODEL_ID
        or artifact.get("source") != EASTMONEY_SOURCE
        or artifact.get("source_url") != EASTMONEY_CLIST_ENDPOINT
        or artifact.get("as_of_session") != as_of_session
    ):
        raise RuntimeError("market-coldness reference artifact has invalid provenance")
    try:
        session = date.fromisoformat(as_of_session)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("market-coldness reference artifact has an invalid session") from exc
    if session.isoformat() != as_of_session:
        raise RuntimeError("market-coldness reference artifact has an invalid session")

    retrieved_at = artifact.get("retrieved_at")
    if not isinstance(retrieved_at, str):
        raise RuntimeError("market-coldness reference artifact has an invalid retrieval timestamp")
    try:
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("market-coldness reference artifact has an invalid retrieval timestamp") from exc
    completed_session = _release_market_coldness_completed_session(retrieved)
    if completed_session != session:
        raise RuntimeError("market-coldness reference artifact was not acquired after the session close")

    listed_raw = artifact.get("listed_codes")
    eligible = set(eligible_codes)
    if not isinstance(listed_raw, list) or any(
        not isinstance(code, str) or re.fullmatch(r"[036][0-9]{5}", code) is None for code in listed_raw
    ):
        raise RuntimeError("market-coldness reference artifact has an invalid listed universe")
    if listed_raw != sorted(listed_raw) or len(listed_raw) != len(set(listed_raw)) or not eligible.issubset(listed_raw):
        raise RuntimeError("market-coldness reference artifact has an invalid listed universe")
    listed = set(listed_raw)
    raw_rows = artifact.get("records")
    declared_source_count = artifact.get("source_record_count")
    if (
        not isinstance(raw_rows, list)
        or isinstance(declared_source_count, bool)
        or not isinstance(declared_source_count, int)
        or declared_source_count != len(raw_rows)
        or declared_source_count < len(listed)
    ):
        raise RuntimeError("market-coldness reference artifact has an invalid source count")

    source_records: dict[str, tuple[date | None, dict[str, float | None], str]] = {}
    prior_code = ""
    for row in raw_rows:
        if not isinstance(row, list) or len(row) != 7:
            raise RuntimeError("market-coldness reference artifact has an invalid source row")
        code, listing_value, change_60d, change_ytd, turnover, volume, source_update_epoch = row
        if (
            not isinstance(code, str)
            or re.fullmatch(r"[036][0-9]{5}", code) is None
            or code <= prior_code
            or code in source_records
        ):
            raise RuntimeError("market-coldness reference artifact has invalid source identities")
        prior_code = code
        if listing_value is None:
            listed_date = None
        elif isinstance(listing_value, str):
            try:
                listed_date = date.fromisoformat(listing_value)
            except ValueError as exc:
                raise RuntimeError(f"market-coldness reference artifact has an invalid listing date: {code}") from exc
            if listed_date.isoformat() != listing_value:
                raise RuntimeError(f"market-coldness reference artifact has an invalid listing date: {code}")
        else:
            raise RuntimeError(f"market-coldness reference artifact has an invalid listing date: {code}")
        values: dict[str, float | None] = {}
        for metric, raw_value in zip(
            _MARKET_COLDNESS_BASE_WEIGHTS,
            (change_60d, change_ytd, turnover, volume),
        ):
            if raw_value is None:
                values[metric] = None
                continue
            numeric = _finite_numeric(raw_value)
            if numeric is None:
                raise RuntimeError(f"market-coldness reference artifact has invalid raw data: {code}:{metric}")
            if metric in {"change_60d_pct", "change_ytd_pct"} and not -100.0 <= numeric <= 10_000.0:
                raise RuntimeError(f"market-coldness reference artifact has invalid raw data: {code}:{metric}")
            if metric in {"turnover_rate_pct", "volume_ratio"} and numeric < 0.0:
                raise RuntimeError(f"market-coldness reference artifact has invalid raw data: {code}:{metric}")
            values[metric] = numeric
        if (
            isinstance(source_update_epoch, bool)
            or not isinstance(source_update_epoch, int)
            or source_update_epoch <= 0
        ):
            raise RuntimeError(f"market-coldness reference artifact has no source update timestamp: {code}")
        try:
            source_updated = datetime.fromtimestamp(source_update_epoch, timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"market-coldness reference artifact has an invalid source update timestamp: {code}"
            ) from exc
        if source_updated > retrieved + timedelta(seconds=_MARKET_COLDNESS_MAX_SOURCE_FUTURE_SKEW_SECONDS):
            raise RuntimeError(f"market-coldness reference artifact has a future source update timestamp: {code}")
        if source_updated.astimezone(_SHANGHAI).date() != session:
            raise RuntimeError(f"market-coldness reference artifact source row belongs to another session: {code}")
        normalized_source_update = source_updated.isoformat(timespec="seconds").replace("+00:00", "Z")
        source_records[code] = (listed_date, values, normalized_source_update)

    if not listed.issubset(source_records):
        raise RuntimeError("market-coldness source does not cover the listed universe")

    source_count = len(source_records)
    source_present_counts = {
        metric: sum(values[metric] is not None for _listed_date, values, _updated in source_records.values())
        for metric in _MARKET_COLDNESS_BASE_WEIGHTS
    }
    source_coverages = {
        "listing_date": sum(listed_date is not None for listed_date, _values, _updated in source_records.values())
        / source_count,
        **{metric: source_present_counts[metric] / source_count for metric in _MARKET_COLDNESS_BASE_WEIGHTS},
    }
    for metric in (*_MARKET_COLDNESS_RAW_KEYS - {"volume_ratio"}, "listing_date"):
        if source_coverages[metric] < MIN_SOURCE_FIELD_COVERAGE:
            raise RuntimeError(f"market-coldness reference artifact has insufficient source coverage: {metric}")

    not_applicable: dict[str, list[str]] = {reason: [] for reason in sorted(_MARKET_COLDNESS_NOT_APPLICABLE_REASONS)}
    data_gaps: dict[str, list[str]] = {}
    bound_records: dict[str, tuple[date, dict[str, float | None], str]] = {}
    for code in sorted(listed):
        source = source_records.get(code)
        if source is None:
            data_gaps.setdefault("missing_source_record", []).append(code)
            continue
        listed_date, values, source_updated_at = source
        if listed_date is None:
            data_gaps.setdefault("missing_listing_date", []).append(code)
            continue
        if listed_date > session:
            raise RuntimeError(f"market-coldness reference artifact has a future listed row: {code}")
        if listed_date.year == session.year:
            not_applicable["listed_in_current_year"].append(code)
            continue
        if (session - listed_date).days < MIN_LISTING_AGE_DAYS:
            not_applicable["listing_history_lt_120_days"].append(code)
            continue
        if any(values[metric] is None for metric in ("change_60d_pct", "change_ytd_pct", "turnover_rate_pct")):
            data_gaps.setdefault("missing_required_metric", []).append(code)
            continue
        bound_records[code] = (listed_date, values, source_updated_at)

    if len(bound_records) < min_cross_section_records:
        data_gaps.setdefault("insufficient_reference_cross_section", []).extend(sorted(bound_records))
        bound_records = {}

    global_sections = {
        metric: sorted(
            value for _listed_date, values, _updated in bound_records.values() if (value := values[metric]) is not None
        )
        for metric in _MARKET_COLDNESS_BASE_WEIGHTS
    }
    board_counts = {
        board: sum(_market_coldness_board(code) == board for code in bound_records)
        for board in {"SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"}
    }
    board_turnover = {
        board: sorted(
            values["turnover_rate_pct"]
            for code, (_listed_date, values, _updated) in bound_records.items()
            if _market_coldness_board(code) == board and values["turnover_rate_pct"] is not None
        )
        for board in board_counts
    }
    ytd_reliability = min(1.0, _market_coldness_business_days_ytd(session) / 60.0)
    weights = dict(_MARKET_COLDNESS_BASE_WEIGHTS)
    weights["change_ytd_pct"] *= ytd_reliability
    full_evidence: dict[str, dict[str, object]] = {}
    for code, (_listed_date, values, source_updated_at) in bound_records.items():
        available = [metric for metric in _MARKET_COLDNESS_BASE_WEIGHTS if values[metric] is not None]
        absolute = {
            metric: _market_coldness_interpolate(values[metric], _MARKET_COLDNESS_ABSOLUTE_BANDS[metric])
            for metric in available
        }
        relative: dict[str, float] = {}
        relative_samples: dict[str, int] = {}
        relative_context: dict[str, dict[str, int]] = {}
        for metric in available:
            section = global_sections[metric]
            minimum = min_cross_section_records
            population = len(bound_records)
            if metric == "turnover_rate_pct":
                board = _market_coldness_board(code)
                if len(board_turnover[board]) >= min_board_turnover_records:
                    section = board_turnover[board]
                    minimum = min_board_turnover_records
                    population = board_counts[board]
            lower = bisect.bisect_left(section, values[metric])
            upper = bisect.bisect_right(section, values[metric])
            relative_context[metric] = {
                "section_size": len(section),
                "minimum_section_records": minimum,
                "section_population": population,
                "source_present": source_present_counts[metric],
                "source_total": source_count,
                "lower_count": lower,
                "equal_count": upper - lower,
            }
            reference_coverage = len(section) / population
            if (
                len(section) >= minimum
                and reference_coverage >= MIN_SOURCE_FIELD_COVERAGE
                and source_coverages[metric] >= MIN_SOURCE_FIELD_COVERAGE
            ):
                relative[metric] = _market_coldness_rank_score(values[metric], section)
                relative_samples[metric] = len(section)
        metric_scores = {
            metric: 0.8 * absolute[metric] + 0.2 * relative[metric] if metric in relative else absolute[metric]
            for metric in available
        }
        total_weight = sum(weights[metric] for metric in available)
        raw_score = sum(metric_scores[metric] * weights[metric] for metric in available) / total_weight
        price_weight = weights["change_60d_pct"] + weights["change_ytd_pct"]
        price_score = (
            metric_scores["change_60d_pct"] * weights["change_60d_pct"]
            + metric_scores["change_ytd_pct"] * weights["change_ytd_pct"]
        ) / price_weight
        score_cap = MAX_COLDNESS_SCORE if values["volume_ratio"] is not None else MAX_SCORE_WITHOUT_VOLUME_RATIO
        caps = [f"evidence_cap={score_cap:.1f}"]
        if absolute["change_60d_pct"] <= 3.0:
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
        full_evidence[code] = {
            "market_coldness_score": score,
            "market_coldness_score_evidence_level": "derived_proxy",
            "market_coldness_score_evidence": {
                "source": f"{EASTMONEY_SOURCE}; {EASTMONEY_CLIST_ENDPOINT}",
                "evidence_id": f"{MARKET_COLDNESS_MODEL_ID}:{code}:{session.strftime('%Y%m%d')}",
                "as_of": session.isoformat(),
                "summary": summary,
            },
            "components": {
                "schema_version": MARKET_COLDNESS_EVIDENCE_SCHEMA_VERSION,
                "model_id": MARKET_COLDNESS_MODEL_ID,
                "code": code,
                "source": EASTMONEY_SOURCE,
                "raw_values": values,
                "absolute": {key: round(value, 6) for key, value in absolute.items()},
                "relative": {key: round(value, 6) for key, value in relative.items()},
                "relative_sample_sizes": relative_samples,
                "relative_context": relative_context,
                "metric_scores": {key: round(value, 6) for key, value in metric_scores.items()},
                "weights": {key: round(weights[key], 6) for key in available},
                "ytd_reliability": round(ytd_reliability, 6),
                "price_score": round(price_score, 6),
                "raw_score": round(raw_score, 6),
                "score_cap": score_cap,
                "caps": caps,
                "board": _market_coldness_board(code),
                "retrieved_at": retrieved_at,
                "as_of_session": session.isoformat(),
                "source_url": EASTMONEY_CLIST_ENDPOINT,
                "source_updated_at": source_updated_at,
            },
        }

    eligible_not_applicable = {reason: sorted(eligible & set(codes)) for reason, codes in not_applicable.items()}
    eligible_data_gaps = {
        reason: sorted(eligible & set(codes)) for reason, codes in sorted(data_gaps.items()) if eligible & set(codes)
    }
    return {
        "full_evidence": full_evidence,
        "eligible_evidence": {code: full_evidence[code] for code in sorted(eligible & set(full_evidence))},
        "eligible_not_applicable_codes_by_reason": eligible_not_applicable,
        "eligible_unscored_data_gap_codes_by_reason": eligible_data_gaps,
    }


def _finite_numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _close_numeric(value: object, expected: float, *, tolerance: float = 1e-5) -> bool:
    observed = _finite_numeric(value)
    return observed is not None and math.isclose(observed, expected, rel_tol=0.0, abs_tol=tolerance)


def _market_coldness_status_partition(
    status: Mapping[str, object],
    eligible: set[str],
    evidence_codes: set[str],
) -> tuple[set[str], set[str]]:
    raw_not_applicable = status.get("eligible_not_applicable_codes_by_reason")
    raw_data_gaps = status.get("eligible_unscored_data_gap_codes_by_reason")
    if not isinstance(raw_not_applicable, Mapping) or set(raw_not_applicable) != set(
        _MARKET_COLDNESS_NOT_APPLICABLE_REASONS
    ):
        raise RuntimeError("release market-coldness applicability ledger is missing or invalid")
    if not isinstance(raw_data_gaps, Mapping):
        raise RuntimeError("release market-coldness data-gap ledger is missing or invalid")

    def collect(payload: Mapping[object, object], *, allowed_reasons: frozenset[str] | None) -> set[str]:
        collected: set[str] = set()
        for reason, raw_codes in payload.items():
            if not isinstance(reason, str) or (allowed_reasons is not None and reason not in allowed_reasons):
                raise RuntimeError("release market-coldness applicability ledger is missing or invalid")
            if not isinstance(raw_codes, list) or raw_codes != sorted(raw_codes):
                raise RuntimeError("release market-coldness applicability ledger is missing or invalid")
            for code in raw_codes:
                if (
                    not isinstance(code, str)
                    or re.fullmatch(r"[036][0-9]{5}", code) is None
                    or code not in eligible
                    or code in collected
                ):
                    raise RuntimeError("release market-coldness applicability ledger is missing or invalid")
                collected.add(code)
        return collected

    not_applicable = collect(raw_not_applicable, allowed_reasons=_MARKET_COLDNESS_NOT_APPLICABLE_REASONS)
    data_gaps = collect(raw_data_gaps, allowed_reasons=None)
    if not_applicable & data_gaps or evidence_codes & (not_applicable | data_gaps):
        raise RuntimeError("release market-coldness applicability ledger overlaps scored identities")
    if evidence_codes | not_applicable | data_gaps != eligible:
        raise RuntimeError("release market-coldness applicability ledger does not partition the eligible universe")

    declared_not_applicable = status.get("eligible_not_applicable_count")
    declared_data_gaps = status.get("eligible_unscored_data_gap_count")
    declared_applicable = status.get("eligible_applicable_count")
    declared_applicable_coverage = _finite_numeric(status.get("eligible_applicable_evidence_coverage"))
    applicable = eligible - not_applicable
    applicable_coverage = len(evidence_codes) / len(applicable) if applicable else 0.0
    if (
        isinstance(declared_not_applicable, bool)
        or declared_not_applicable != len(not_applicable)
        or isinstance(declared_data_gaps, bool)
        or declared_data_gaps != len(data_gaps)
        or isinstance(declared_applicable, bool)
        or declared_applicable != len(applicable)
        or declared_applicable_coverage is None
        or not math.isclose(declared_applicable_coverage, applicable_coverage, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise RuntimeError("release market-coldness applicability counts are inconsistent")
    if data_gaps:
        raise RuntimeError(f"release market-coldness has {len(data_gaps)} unexplained eligible data gaps")
    if applicable != evidence_codes or applicable_coverage != 1.0:
        raise RuntimeError("release market-coldness does not cover every applicable eligible company")
    return not_applicable, data_gaps


def _require_market_coldness_record(
    code: str,
    record: object,
    *,
    parsed_session: date,
    retrieved_at: str,
) -> None:
    if not isinstance(record, Mapping):
        raise RuntimeError(f"release market-coldness evidence record is invalid: {code}")
    score = _finite_numeric(record.get("market_coldness_score"))
    if score is None or not 1.0 <= score <= MAX_COLDNESS_SCORE:
        raise RuntimeError(f"release market-coldness score is invalid: {code}")
    expected_record_keys = {
        "market_coldness_score",
        "market_coldness_score_evidence_level",
        "market_coldness_score_evidence",
        "components",
    }
    if set(record) != expected_record_keys or record.get("market_coldness_score_evidence_level") != "derived_proxy":
        raise RuntimeError(f"release market-coldness evidence record is invalid: {code}")
    metadata = record.get("market_coldness_score_evidence")
    components = record.get("components")
    expected_id = f"{MARKET_COLDNESS_MODEL_ID}:{code}:{parsed_session.strftime('%Y%m%d')}"
    expected_source = f"{EASTMONEY_SOURCE}; {EASTMONEY_CLIST_ENDPOINT}"
    if not isinstance(metadata, Mapping) or set(metadata) != {"source", "evidence_id", "as_of", "summary"}:
        raise RuntimeError(f"release market-coldness score provenance is invalid: {code}")
    if (
        metadata.get("source") != expected_source
        or metadata.get("evidence_id") != expected_id
        or metadata.get("as_of") != parsed_session.isoformat()
        or not isinstance(metadata.get("summary"), str)
        or not metadata["summary"].strip()
    ):
        raise RuntimeError(f"release market-coldness score provenance is invalid: {code}")
    expected_component_keys = {
        "schema_version",
        "model_id",
        "code",
        "source",
        "raw_values",
        "absolute",
        "relative",
        "relative_sample_sizes",
        "relative_context",
        "metric_scores",
        "weights",
        "ytd_reliability",
        "price_score",
        "raw_score",
        "score_cap",
        "caps",
        "board",
        "retrieved_at",
        "as_of_session",
        "source_url",
        "source_updated_at",
    }
    if (
        not isinstance(components, Mapping)
        or set(components) != expected_component_keys
        or components.get("schema_version") != MARKET_COLDNESS_EVIDENCE_SCHEMA_VERSION
        or components.get("model_id") != MARKET_COLDNESS_MODEL_ID
        or components.get("code") != code
        or components.get("source") != EASTMONEY_SOURCE
        or components.get("as_of_session") != parsed_session.isoformat()
        or components.get("source_url") != EASTMONEY_CLIST_ENDPOINT
        or components.get("retrieved_at") != retrieved_at
        or components.get("board") != _market_coldness_board(code)
    ):
        raise RuntimeError(f"release market-coldness component provenance is invalid: {code}")
    source_updated_at = components.get("source_updated_at")
    try:
        parsed_source_update = datetime.fromisoformat(str(source_updated_at).replace("Z", "+00:00"))
        parsed_retrieval = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"release market-coldness source timestamp is invalid: {code}") from exc
    if (
        not isinstance(source_updated_at, str)
        or parsed_source_update.tzinfo is None
        or parsed_source_update.utcoffset() is None
        or parsed_retrieval.tzinfo is None
        or parsed_retrieval.utcoffset() is None
        or parsed_source_update.astimezone(_SHANGHAI).date() != parsed_session
        or parsed_source_update > parsed_retrieval + timedelta(seconds=_MARKET_COLDNESS_MAX_SOURCE_FUTURE_SKEW_SECONDS)
    ):
        raise RuntimeError(f"release market-coldness source timestamp is invalid: {code}")

    raw_values = components.get("raw_values")
    if not isinstance(raw_values, Mapping) or set(raw_values) != _MARKET_COLDNESS_RAW_KEYS:
        raise RuntimeError(f"release market-coldness raw evidence is invalid: {code}")
    values: dict[str, float | None] = {}
    for metric in _MARKET_COLDNESS_RAW_KEYS:
        raw_value = raw_values.get(metric)
        if metric == "volume_ratio" and raw_value is None:
            values[metric] = None
            continue
        value = _finite_numeric(raw_value)
        if value is None:
            raise RuntimeError(f"release market-coldness raw evidence is invalid: {code}:{metric}")
        if metric in {"change_60d_pct", "change_ytd_pct"} and not -100.0 <= value <= 10_000.0:
            raise RuntimeError(f"release market-coldness raw evidence is invalid: {code}:{metric}")
        if metric == "turnover_rate_pct" and value < 0.0:
            raise RuntimeError(f"release market-coldness raw evidence is invalid: {code}:{metric}")
        if metric == "volume_ratio" and value < 0.0:
            raise RuntimeError(f"release market-coldness raw evidence is invalid: {code}:{metric}")
        values[metric] = value

    available = [metric for metric in _MARKET_COLDNESS_BASE_WEIGHTS if values[metric] is not None]
    absolute = components.get("absolute")
    relative = components.get("relative")
    relative_samples = components.get("relative_sample_sizes")
    relative_context = components.get("relative_context")
    metric_scores = components.get("metric_scores")
    weights = components.get("weights")
    if (
        not isinstance(absolute, Mapping)
        or set(absolute) != set(available)
        or not isinstance(relative, Mapping)
        or not isinstance(relative_samples, Mapping)
        or not isinstance(relative_context, Mapping)
        or set(relative_context) != set(available)
        or not isinstance(metric_scores, Mapping)
        or set(metric_scores) != set(available)
        or not isinstance(weights, Mapping)
        or set(weights) != set(available)
    ):
        raise RuntimeError(f"release market-coldness score components are invalid: {code}")

    context_keys = {
        "section_size",
        "minimum_section_records",
        "section_population",
        "source_present",
        "source_total",
        "lower_count",
        "equal_count",
    }
    expected_relative: dict[str, float] = {}
    expected_relative_samples: dict[str, int] = {}
    for metric in available:
        context = relative_context.get(metric)
        if not isinstance(context, Mapping) or set(context) != context_keys:
            raise RuntimeError(f"release market-coldness relative context is invalid: {code}:{metric}")
        if any(isinstance(context[field], bool) or not isinstance(context[field], int) for field in context_keys):
            raise RuntimeError(f"release market-coldness relative counts are invalid: {code}:{metric}")
        section_size = context["section_size"]
        minimum = context["minimum_section_records"]
        population = context["section_population"]
        source_present = context["source_present"]
        source_total = context["source_total"]
        lower = context["lower_count"]
        equal = context["equal_count"]
        if (
            minimum < 1
            or section_size < 1
            or population < section_size
            or source_total < source_present
            or source_present < 1
            or lower < 0
            or equal < 1
            or lower + equal > section_size
        ):
            raise RuntimeError(f"release market-coldness relative counts are invalid: {code}:{metric}")
        eligible = (
            section_size >= minimum
            and section_size / population >= MIN_SOURCE_FIELD_COVERAGE
            and source_present / source_total >= MIN_SOURCE_FIELD_COVERAGE
        )
        if eligible:
            if section_size < 2 or equal == section_size:
                rank_score = 5.0
            else:
                greater = section_size - lower - equal
                rank_score = 1.0 + 8.0 * (greater + 0.5 * (equal - 1)) / (section_size - 1)
                rank_score = max(1.0, min(9.0, rank_score))
            expected_relative[metric] = rank_score
            expected_relative_samples[metric] = section_size
    if (
        set(relative) != set(expected_relative)
        or set(relative_samples) != set(expected_relative_samples)
        or any(
            not _close_numeric(relative.get(metric), round(value, 6), tolerance=5e-7)
            for metric, value in expected_relative.items()
        )
        or any(
            isinstance(relative_samples.get(metric), bool) or relative_samples.get(metric) != sample_size
            for metric, sample_size in expected_relative_samples.items()
        )
    ):
        raise RuntimeError(f"release market-coldness relative component replay failed: {code}")

    expected_absolute = {
        metric: _market_coldness_interpolate(values[metric], _MARKET_COLDNESS_ABSOLUTE_BANDS[metric])
        for metric in available
    }
    if any(not _close_numeric(absolute.get(metric), round(value, 6)) for metric, value in expected_absolute.items()):
        raise RuntimeError(f"release market-coldness absolute component replay failed: {code}")
    expected_metric_scores = {
        metric: (
            0.8 * expected_absolute[metric] + 0.2 * expected_relative[metric]
            if metric in expected_relative
            else expected_absolute[metric]
        )
        for metric in available
    }
    if any(
        not _close_numeric(metric_scores.get(metric), round(value, 6))
        for metric, value in expected_metric_scores.items()
    ):
        raise RuntimeError(f"release market-coldness metric score replay failed: {code}")

    ytd_reliability = min(1.0, _market_coldness_business_days_ytd(parsed_session) / 60.0)
    expected_weights = dict(_MARKET_COLDNESS_BASE_WEIGHTS)
    expected_weights["change_ytd_pct"] *= ytd_reliability
    if not _close_numeric(components.get("ytd_reliability"), round(ytd_reliability, 6)) or any(
        not _close_numeric(weights.get(metric), round(expected_weights[metric], 6)) for metric in available
    ):
        raise RuntimeError(f"release market-coldness weight replay failed: {code}")
    total_weight = sum(expected_weights[metric] for metric in available)
    raw_score = sum(expected_metric_scores[metric] * expected_weights[metric] for metric in available) / total_weight
    price_weight = expected_weights["change_60d_pct"] + expected_weights["change_ytd_pct"]
    price_score = (
        expected_metric_scores["change_60d_pct"] * expected_weights["change_60d_pct"]
        + expected_metric_scores["change_ytd_pct"] * expected_weights["change_ytd_pct"]
    ) / price_weight
    if not _close_numeric(components.get("raw_score"), round(raw_score, 6)) or not _close_numeric(
        components.get("price_score"), round(price_score, 6)
    ):
        raise RuntimeError(f"release market-coldness aggregate score replay failed: {code}")

    score_cap = MAX_COLDNESS_SCORE if values["volume_ratio"] is not None else MAX_SCORE_WITHOUT_VOLUME_RATIO
    caps = [f"evidence_cap={score_cap:.1f}"]
    if expected_absolute["change_60d_pct"] <= 3.0:
        score_cap = min(score_cap, 3.0)
        caps.append("60d_hot_cap=3.0")
    elif price_score < 5.0:
        score_cap = min(score_cap, 4.9)
        caps.append("price_coldness_lt5_cap=4.9")
    elif price_score < 6.0:
        score_cap = min(score_cap, 6.9)
        caps.append("price_coldness_lt6_cap=6.9")
    expected_score = round(max(1.0, min(score_cap, raw_score)), 1)
    if (
        not _close_numeric(components.get("score_cap"), score_cap)
        or components.get("caps") != caps
        or not math.isclose(score, expected_score, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise RuntimeError(f"release market-coldness cap or final score replay failed: {code}")
    volume_text = f"{values['volume_ratio']:.2f}" if values["volume_ratio"] is not None else "缺失"
    expected_summary = (
        f"量价冷度;60日{values['change_60d_pct']:.1f}%;YTD{values['change_ytd_pct']:.1f}%;"
        f"换手{values['turnover_rate_pct']:.2f}%;量比{volume_text};上限{score_cap:.1f}"
    )
    if metadata.get("summary") != expected_summary:
        raise RuntimeError(f"release market-coldness score summary replay failed: {code}")


def _require_market_coldness_release_evidence(
    evidence: Mapping[str, object],
    status: Mapping[str, object],
    *,
    reference_artifact: Mapping[str, object] | None = None,
    eligible_codes: Sequence[str],
    as_of_session: str | None,
    min_cross_section_records: int = MIN_CROSS_SECTION_RECORDS,
    min_board_turnover_records: int = MIN_BOARD_TURNOVER_RECORDS,
) -> float:
    """Reject a release generation whose market-coldness input is incomplete."""

    if any(not isinstance(code, str) or re.fullmatch(r"[036][0-9]{5}", code) is None for code in eligible_codes):
        raise RuntimeError("release market-coldness gate received an invalid eligible universe")
    eligible = set(eligible_codes)
    if not eligible or len(eligible) != len(eligible_codes):
        raise RuntimeError("release market-coldness gate received an invalid eligible universe")
    if not isinstance(evidence, Mapping) or not isinstance(status, Mapping):
        raise RuntimeError("release market-coldness evidence has an invalid shape")
    if any(not isinstance(code, str) or code != code.strip() for code in evidence):
        raise RuntimeError("release market-coldness evidence contains unknown or duplicate identities")
    evidence_codes = set(evidence)
    if len(evidence_codes) != len(evidence) or not evidence_codes.issubset(eligible):
        raise RuntimeError("release market-coldness evidence contains unknown or duplicate identities")
    observed = len(evidence_codes)
    declared_count = status.get("eligible_evidence_count")
    declared_coverage = status.get("eligible_evidence_coverage")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != observed
        or isinstance(declared_coverage, bool)
        or not isinstance(declared_coverage, (int, float))
        or not math.isfinite(float(declared_coverage))
    ):
        raise RuntimeError("release market-coldness evidence count or coverage is inconsistent")
    coverage = observed / len(eligible)
    if not math.isclose(float(declared_coverage), coverage, rel_tol=0.0, abs_tol=1e-12):
        raise RuntimeError("release market-coldness evidence count and coverage disagree")
    reason = str(status.get("evidence_reason") or "unknown")
    if status.get("available") is not True or status.get("evidence_available") is not True:
        raise RuntimeError(f"release market-coldness evidence is unavailable: {reason}")
    if not isinstance(as_of_session, str) or status.get("as_of_session") != as_of_session:
        raise RuntimeError("release market-coldness evidence is bound to a different trading session")
    try:
        parsed_session = date.fromisoformat(as_of_session)
    except ValueError as exc:
        raise RuntimeError("release market-coldness evidence has an invalid trading session") from exc
    if parsed_session.isoformat() != as_of_session:
        raise RuntimeError("release market-coldness evidence has an invalid trading session")

    source = status.get("source")
    source_url = status.get("source_url")
    retrieved_at = status.get("retrieved_at")
    if (
        source != EASTMONEY_SOURCE
        or source_url != EASTMONEY_CLIST_ENDPOINT
        or status.get("model_id") != MARKET_COLDNESS_MODEL_ID
    ):
        raise RuntimeError("release market-coldness source provenance is incomplete")
    if not isinstance(retrieved_at, str):
        raise RuntimeError("release market-coldness retrieval timestamp is invalid")
    try:
        parsed_retrieval = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("release market-coldness retrieval timestamp is invalid") from exc
    if (
        parsed_retrieval.tzinfo is None
        or parsed_retrieval.utcoffset() is None
        or _release_market_coldness_completed_session(parsed_retrieval) != parsed_session
    ):
        raise RuntimeError("release market-coldness retrieval timestamp is invalid")
    _market_coldness_status_partition(status, eligible, evidence_codes)
    for code, record in evidence.items():
        _require_market_coldness_record(
            code,
            record,
            parsed_session=parsed_session,
            retrieved_at=retrieved_at,
        )
    replay = _replay_market_coldness_reference_artifact(
        reference_artifact,
        eligible_codes=eligible_codes,
        as_of_session=as_of_session,
        min_cross_section_records=min_cross_section_records,
        min_board_turnover_records=min_board_turnover_records,
    )
    expected_evidence = replay["eligible_evidence"]
    if (
        status.get("eligible_not_applicable_codes_by_reason") != replay["eligible_not_applicable_codes_by_reason"]
        or status.get("eligible_unscored_data_gap_codes_by_reason")
        != replay["eligible_unscored_data_gap_codes_by_reason"]
    ):
        raise RuntimeError("release market-coldness applicability ledger differs from raw source evidence")
    if _canonical_market_coldness_json(evidence) != _canonical_market_coldness_json(expected_evidence):
        raise RuntimeError("release market-coldness evidence differs from independent full-universe replay")
    artifact_hash = hashlib.sha256(_canonical_market_coldness_json(reference_artifact)).hexdigest()
    if status.get("reference_artifact_sha256") != artifact_hash or status.get("full_listed_evidence_count") != len(
        replay["full_evidence"]
    ):
        raise RuntimeError("release market-coldness reference artifact identity is inconsistent")
    return coverage


def _decision_reason(value: object) -> str:
    text = " ".join(str(value or "数据不足").split())
    if len(text) <= 20:
        return text
    best_boundary = ""
    for match in re.finditer(r"[；;。！？!?，,]", text):
        candidate = text[: match.start()].rstrip()
        if len(candidate) > 19:
            break
        if candidate:
            best_boundary = candidate
    if best_boundary:
        return best_boundary + "…"
    prefix = text[:19].rstrip()
    word_boundary = prefix.rfind(" ")
    if word_boundary >= 9:
        prefix = prefix[:word_boundary].rstrip()
    return prefix + "…"


def _independent_decision_missing_requirements(reasons: Mapping[str, object]) -> tuple[bool, list[str]]:
    """Validate the private Patch 7 post-gate requirement ledger.

    The audit deliberately duplicates the producer contract.  Missing means
    the historical decision format; a present ledger must be non-empty,
    duplicate-free, string-only, and limited to the pinned requirement keys.
    """

    raw = reasons.get(_DECISION_MISSING_REQUIREMENTS_REASON)
    if raw is None:
        return True, []
    if (
        not isinstance(raw, (list, tuple))
        or not raw
        or any(not isinstance(value, str) for value in raw)
        or len(raw) != len(set(raw))
        or set(raw) - set(_DECISION_MISSING_REQUIREMENTS)
    ):
        return False, []
    return True, [key for key in _DECISION_MISSING_REQUIREMENTS if key in raw]


def _independent_patch7_requirements_match(
    type_key: str,
    requirements: Sequence[str],
    *,
    company: Mapping[str, object] | None,
    dcf_result: object,
) -> bool:
    """Bind Patch 7's private requirement ledger to the audited outer facts."""

    if type_key not in _DECISION_PATCH7_PRICE_GATE_TYPES:
        return not requirements
    if not isinstance(company, Mapping) or dcf_result is _DECISION_FACT_UNSET:
        return not requirements

    expected: list[str] = []
    if _finite_numeric(company.get("price")) is None:
        expected.append("patch7_current_price")
    dcf = dcf_result if isinstance(dcf_result, Mapping) else {}
    points = dcf.get("dcf_points") if isinstance(dcf.get("dcf_points"), Mapping) else {}
    optimistic = points.get("optimistic") if isinstance(points.get("optimistic"), Mapping) else {}
    if _finite_numeric(optimistic.get("upper")) is None:
        expected.append("patch7_optimistic_upper")
    return list(requirements) == expected


def _independent_decision_replay(
    type_key: str,
    payload: Mapping[str, object],
    *,
    company: Mapping[str, object] | None = None,
    dcf_result: object = _DECISION_FACT_UNSET,
) -> dict[str, object] | None:
    """Independently reconstruct one published buy/no-buy decision contract.

    This intentionally duplicates the public scoring boundary rather than
    importing the producer helper.  A scoring-code change must therefore also
    update this release replay or the audit fails closed.
    """

    weights = TYPE_WEIGHTS.get(type_key)
    reasons = payload.get("reasons")
    sub_scores = payload.get("sub_scores")
    decision = payload.get("decision")
    if (
        weights is None
        or not isinstance(reasons, Mapping)
        or not isinstance(sub_scores, Mapping)
        or set(sub_scores) != set(weights)
        or not isinstance(decision, Mapping)
        or set(decision) != _DECISION_FIELDS
    ):
        return None
    if type_key == "type7":
        ledger = payload.get("ledger")
        if not isinstance(ledger, Mapping) or ledger.get("model_id") != _PATCH6_TYPE7_MODEL_ID:
            return None
    raw_missing = reasons.get("_decision_missing_dimensions")
    if not isinstance(raw_missing, (list, tuple)) or any(not isinstance(value, str) for value in raw_missing):
        return None
    if len(raw_missing) != len(set(raw_missing)) or set(raw_missing) - set(weights):
        return None
    missing = [dimension for dimension in weights if dimension in raw_missing]
    clean_scores: dict[str, float] = {}
    for dimension in weights:
        score = _finite_numeric(sub_scores.get(dimension))
        if score is None or not 0.0 <= score <= 10.0:
            # An evidence-missing dimension legitimately has no scored value;
            # its bounds are derived from the missing list instead.
            if dimension in missing:
                continue
            return None
        clean_scores[dimension] = score
    status = reasons.get("_status")
    applicable_marker = reasons.get("_applicable")
    evidence_marker = reasons.get("_evidence")
    if applicable_marker not in {"yes", "no"} or evidence_marker not in {"complete", "incomplete"}:
        return None
    applicable = applicable_marker == "yes"
    evidence_complete = evidence_marker == "complete"
    requirements_valid, missing_requirements = _independent_decision_missing_requirements(reasons)
    if (
        not requirements_valid
        or (missing_requirements and (not applicable or evidence_complete or status != "insufficient_evidence"))
        or not _independent_patch7_requirements_match(
            type_key,
            missing_requirements,
            company=company,
            dcf_result=dcf_result,
        )
    ):
        return None
    if not evidence_complete and not missing and not missing_requirements:
        missing = list(weights)
    # Type 6's position size is an action input, not company evidence.  It is
    # nevertheless an unresolved decision dimension until the user confirms
    # the actual single-name and portfolio exposure.
    type6_action_condition = bool(type_key == "type6" and reasons.get("_condition") == "须确认实际仓位符合建议上限")
    type3_condition_failed = bool(
        type_key == "type3"
        and isinstance(reasons.get("_condition"), str)
        and "低于10%高增长门槛" in reasons["_condition"]
    )
    if type6_action_condition and "6e" not in missing:
        missing.append("6e")

    lower_scores = dict(clean_scores)
    upper_scores = dict(clean_scores)
    for dimension in missing:
        lower_scores[dimension] = 0.0
        upper_scores[dimension] = 10.0

    type7_trigger_possible: bool | None = None
    type7_new_model = False
    if type_key == "type7" and missing:
        ledger = payload.get("ledger")
        type7_new_model = isinstance(ledger, Mapping) and ledger.get("model_id") == _PATCH6_TYPE7_MODEL_ID
        if type7_new_model:
            sections = ledger.get("dimensions")
            classification = ledger.get("classification")
            if not isinstance(sections, Mapping) or not isinstance(classification, Mapping):
                return None
            mapping = {"7a": "BM", "7b": "MOAT", "7c": "G"}
            if classification.get("route_complete") is not True:
                for dimension in missing:
                    upper_scores[dimension] = 10.0
            else:
                for dimension in missing:
                    section = sections.get(mapping[dimension])
                    raw_upper = _finite_numeric(section.get("upper_bound")) if isinstance(section, Mapping) else None
                    if raw_upper is None or not 0.0 <= raw_upper <= 10.0:
                        return None
                    upper_scores[dimension] = raw_upper
            exact_upper = _finite_numeric(ledger.get("upper_bound"))
            type7_trigger_possible = exact_upper is not None and exact_upper > 7.0
        else:
            upper_ledger = ledger.get("decisive_score_upper_bounds") if isinstance(ledger, Mapping) else None
            mapping = {"7a": "template1", "7b": "template5", "7c": "patch5"}
            if not isinstance(upper_ledger, Mapping) or set(upper_ledger) != set(mapping.values()):
                return None
            for dimension in missing:
                raw_upper = _finite_numeric(upper_ledger.get(mapping[dimension]))
                if raw_upper is None or not 0.0 <= raw_upper <= 100.0:
                    return None
                upper_scores[dimension] = raw_upper / 10.0
            type7_trigger_possible = all(upper_scores[dimension] > 7.0 for dimension in weights)
    elif type_key == "type7":
        ledger = payload.get("ledger")
        type7_new_model = isinstance(ledger, Mapping) and ledger.get("model_id") == _PATCH6_TYPE7_MODEL_ID

    lower = round(sum(lower_scores[key] * weight for key, weight in weights.items()), 1)
    upper = round(sum(upper_scores[key] * weight for key, weight in weights.items()), 1)
    # Patch 6's Type 3 downgrade applies only when 3e is known.  A missing 3b
    # remains theoretically capable of scoring 10; the fetch adapter's
    # narrower operational cap must never become a decision bound.
    if type_key == "type3" and "3e" not in missing and clean_scores["3e"] <= 3.0:
        lower = min(lower, 4.9)
        upper = min(upper, 4.9)

    triggered = payload.get("triggered")
    published_applicable = payload.get("applicable")
    published_evidence_complete = payload.get("evidence_complete")
    published_veto = payload.get("veto")
    published_status = payload.get("status")
    if (
        not all(
            isinstance(value, bool)
            for value in (published_applicable, published_evidence_complete, triggered, published_veto)
        )
        or published_status != status
        or published_applicable != applicable
        or published_evidence_complete != evidence_complete
        or published_veto != bool(reasons.get("_veto"))
        or triggered != (status == "triggered")
    ):
        import os

        if os.environ.get("DS_DCF_DEBUG_REPLAY") and payload.get("code") in {
            "300760",
            "600285",
        }:
            print(
                "DEBUG_REPLAY",
                payload.get("code"),
                {
                    "published_applicable": published_applicable,
                    "published_evidence_complete": published_evidence_complete,
                    "triggered": triggered,
                    "published_veto": published_veto,
                    "published_status": published_status,
                    "reasons_status": status,
                    "reasons_veto": reasons.get("_veto"),
                    "reasons_keys": sorted(reasons.keys()),
                    "payload_keys": sorted(payload.keys()),
                },
                flush=True,
            )
        return None

    market_context = payload.get("decision_market_context")
    if (
        not isinstance(market_context, Mapping)
        or set(market_context) != _DECISION_MARKET_CONTEXT_FIELDS
        or (market_context.get("tradable") is not None and type(market_context.get("tradable")) is not bool)
        or type(market_context.get("reference_price")) is not bool
        or not isinstance(market_context.get("risk_status"), str)
    ):
        return None
    risk_status = str(market_context["risk_status"]).strip()
    market_block_reason = (
        "标的不可交易"
        if market_context["tradable"] is False
        else "仅参考价不得触发买入"
        if market_context["reference_price"]
        else f"风险状态:{risk_status}"
        if risk_status and risk_status.lower() not in {"正常", "normal", "active", "ok"}
        else None
    )
    market_block = market_block_reason is not None
    if bool(reasons.get("_decision_market_block")) != market_block or (
        market_block and reasons.get("_decision_market_block") != _decision_reason(market_block_reason)
    ):
        return None
    missing_set = set(missing)
    bounded_veto = False
    if type_key == "type2":
        possible_veto = "2c" in missing_set
        if missing_set.intersection({"2a", "2b"}):
            lower_hot = sum(0.0 if key in missing_set else upper_scores[key] for key in ("2a", "2b")) / 2.0
            possible_veto = possible_veto or lower_hot <= 4.0
    elif type_key == "type4":
        possible_moat = "4c" in missing_set
        possible_double_bubble = bool(
            missing_set.intersection({"4e", "4f"})
            and (0.0 if "4e" in missing_set else upper_scores["4e"]) <= 3.0
            and (0.0 if "4f" in missing_set else upper_scores["4f"]) <= 3.0
        )
        possible_veto = possible_moat or possible_double_bubble
    elif type_key == "type6":
        core = ("6a", "6b", "6c", "6d")
        known_high = sum(key not in missing_set and upper_scores[key] >= 5.0 for key in core)
        maximum_high = known_high + sum(key in missing_set for key in core)
        bounded_veto = maximum_high < 2
        possible_veto = not bounded_veto and known_high < 2
    elif type_key == "type7" and type7_new_model:
        ledger = payload.get("ledger")
        classification = ledger.get("classification") if isinstance(ledger, Mapping) else None
        possible_veto = bool(
            not isinstance(classification, Mapping)
            or classification.get("route_complete") is not True
            or (classification.get("class_code") == "C" and missing_set.intersection({"7a", "7b"}))
        )
    else:
        possible_veto = bool(missing_set.intersection(_DECISION_POTENTIAL_VETO_DIMENSIONS[type_key]))

    def known(key: str) -> bool:
        return key not in missing_set

    if type_key == "type1":
        confirmed_hard_veto = bool(
            (known("1a") and upper_scores["1a"] <= 2.0) or (known("1b") and upper_scores["1b"] <= 3.0)
        )
    elif type_key == "type2":
        confirmed_hard_veto = bool(
            (known("2a") and known("2b") and (upper_scores["2a"] + upper_scores["2b"]) / 2.0 <= 4.0)
            or (known("2c") and upper_scores["2c"] <= 3.0)
        )
    elif type_key == "type3":
        confirmed_hard_veto = bool(
            (known("3a") and upper_scores["3a"] <= 3.0) or (known("3d") and upper_scores["3d"] <= 3.0)
        )
    elif type_key == "type4":
        confirmed_hard_veto = bool(
            (known("4c") and upper_scores["4c"] <= 3.0)
            or (known("4e") and known("4f") and upper_scores["4e"] <= 3.0 and upper_scores["4f"] <= 3.0)
        )
    elif type_key == "type5":
        confirmed_hard_veto = False
    elif type_key == "type6":
        confirmed_hard_veto = bool(
            all(known(key) for key in ("6a", "6b", "6c", "6d"))
            and sum(upper_scores[key] >= 5.0 for key in ("6a", "6b", "6c", "6d")) < 2
        )
    elif type_key == "type7" and type7_new_model:
        ledger = payload.get("ledger")
        confirmed_hard_veto = bool(isinstance(ledger, Mapping) and ledger.get("veto") is True)
    else:
        ledger = payload.get("ledger")
        patch5 = ledger.get("patch5") if isinstance(ledger, Mapping) else None
        safety_score = _finite_numeric(patch5.get("safety_margin_score")) if isinstance(patch5, Mapping) else None
        confirmed_hard_veto = bool(
            isinstance(patch5, Mapping)
            and patch5.get("safety_margin_complete") is True
            and safety_score is not None
            and safety_score < 8.0
        )

    if not applicable:
        expected_complete = True
        expected_basis = "scope_exclusion"
        lower = upper = 0.0
        veto_state = "none"
        expected_potential = False
    elif confirmed_hard_veto or bounded_veto:
        expected_complete = True
        expected_basis = "confirmed_veto"
        veto_state = "confirmed"
        expected_potential = False
    elif market_block:
        expected_complete = True
        expected_basis = "market_block"
        veto_state = "none"
        expected_potential = False
    else:
        veto_state = "possible" if possible_veto else "none"
        theoretical_possible = bool(upper >= 7.0)
        if type_key == "type1":
            theoretical_possible = theoretical_possible and ("1a" in missing or upper_scores["1a"] >= 5.0)
        elif type_key == "type2":
            hot_upper = (upper_scores["2a"] + upper_scores["2b"]) / 2.0
            valuation_possible = bool(
                upper_scores["2d"] >= 5.0
                or (hot_upper >= 7.0 and upper_scores["2c"] >= 7.0 and 4.0 <= upper_scores["2d"] <= 5.0)
            )
            theoretical_possible = theoretical_possible and valuation_possible
        elif type_key == "type6":
            theoretical_possible = theoretical_possible and (
                sum(upper_scores[key] >= 5.0 for key in ("6a", "6b", "6c", "6d")) >= 2
            )
            if "6e" not in missing:
                theoretical_possible = theoretical_possible and (
                    upper_scores["6e"] >= 8.0 and not reasons.get("_condition")
                )
        elif type_key == "type7":
            if type7_new_model:
                ledger = payload.get("ledger")
                exact_upper = _finite_numeric(ledger.get("upper_bound")) if isinstance(ledger, Mapping) else None
                condition_failures = ledger.get("condition_failures") if isinstance(ledger, Mapping) else None
                theoretical_possible = bool(
                    upper >= 7.0
                    and exact_upper is not None
                    and exact_upper > 7.0
                    and isinstance(condition_failures, list)
                    and not condition_failures
                )
            else:
                theoretical_possible = (
                    bool(type7_trigger_possible)
                    if type7_trigger_possible is not None
                    else all(upper_scores[dimension] > 7.0 for dimension in weights)
                )

        type7_action_condition = bool(
            type_key == "type7"
            and isinstance(ledger, Mapping)
            and ledger.get("quality_certified") is True
            and ledger.get("complete") is not True
            and any(
                isinstance(gate, Mapping) and gate.get("complete") is not True
                for gate in (
                    ledger.get("decision_gates", {}).values()
                    if isinstance(ledger.get("decision_gates"), Mapping)
                    else []
                )
            )
        )
        if type6_action_condition or type7_action_condition or type3_condition_failed:
            expected_complete = not theoretical_possible
            expected_basis = "action_condition" if theoretical_possible else "conservative_upper_bound"
            expected_potential = theoretical_possible
        elif evidence_complete:
            expected_complete = True
            expected_basis = "full_evidence"
            expected_potential = theoretical_possible
        elif not theoretical_possible:
            expected_complete = True
            expected_basis = "conservative_upper_bound"
            expected_potential = False
        else:
            expected_complete = False
            expected_basis = "unresolved_missing_evidence"
            expected_potential = True

    # Patch 7 (2026-08-04) cross-type gates are model post-gates; a veto
    # written by _apply_patch7_total_gate suppresses the trigger and is itself
    # the replay's model basis (a confirmed post-gate veto).
    if str(reasons.get("_veto") or "").startswith("补丁7"):
        expected_potential = False
        expected_complete = True
        expected_basis = "confirmed_veto"
        veto_state = "confirmed"
        confirmed_hard_veto = True

    expected = {
        "schema_version": _DECISION_SCHEMA_VERSION,
        "model_id": _DECISION_MODEL_ID,
        "decision_complete": expected_complete,
        "decision_basis": expected_basis,
        "score_lower_bound": lower,
        "score_upper_bound": upper,
        "veto_state": veto_state,
        "potentially_triggerable": expected_potential,
        "missing_dimensions": missing,
    }
    for key, expected_value in expected.items():
        actual = decision.get(key)
        if key in {"score_lower_bound", "score_upper_bound"}:
            numeric = _finite_numeric(actual)
            if numeric is None or not math.isclose(numeric, float(expected_value), rel_tol=0.0, abs_tol=1e-12):
                return None
        elif actual != expected_value:
            return None
    expected_trigger = bool(expected_basis == "full_evidence" and expected_potential)
    market_rewrite_veto = bool(market_block and status == "blocked" and not reasons.get("_blocked"))
    if (
        triggered is not expected_trigger
        or (bool(reasons.get("_veto")) and not confirmed_hard_veto and not market_rewrite_veto)
        or (status == "vetoed" and not confirmed_hard_veto)
    ):
        return None

    visible = bool(
        not expected_potential or expected_basis in {"full_evidence", "action_condition", "unresolved_missing_evidence"}
    )
    recall_safe = bool(
        visible and (not triggered or (evidence_complete and expected_complete and expected_basis == "full_evidence"))
    )
    return {
        **expected,
        "visible": visible,
        "recall_safe": recall_safe,
    }


def _analysis_coverage_summary(
    scores: pd.DataFrame,
    dcf_results: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Summarise full-universe triggers, statuses and evidence authority."""
    if not isinstance(scores, pd.DataFrame):
        raise TypeError("analysis scores must be a pandas DataFrame")
    if dcf_results is not None and not isinstance(dcf_results, Mapping):
        raise TypeError("analysis DCF results must be a code mapping or None")
    framework_statuses: dict[str, dict[str, int]] = {}
    framework_triggers: dict[str, int] = {}
    framework_evidence: dict[str, dict[str, object]] = {}
    company_rows = scores.to_dict(orient="records")
    codes = [str(row.get("code", f"row:{index}")) for index, row in enumerate(company_rows)]
    for type_key in _TYPE_KEYS:
        statuses: Counter[str] = Counter()
        triggers = 0
        contract = Counter()
        incomplete_without_reason: list[str] = []
        invalid_sub_scores: list[str] = []
        invalid_decisions: list[str] = []
        if type_key in scores:
            for code, company, payload in zip(codes, company_rows, scores[type_key], strict=True):
                if not isinstance(payload, Mapping):
                    statuses["invalid_payload"] += 1
                    contract["invalid_payload"] += 1
                    continue
                statuses[str(payload.get("status") or "missing_status")] += 1
                triggers += int(payload.get("triggered") is True)
                applicable = payload.get("applicable")
                evidence_complete = payload.get("evidence_complete")
                contract["rows"] += 1
                if applicable is True:
                    contract["applicable"] += 1
                elif applicable is False:
                    contract["not_applicable"] += 1
                else:
                    contract["invalid_applicability"] += 1
                if evidence_complete is True:
                    contract["evidence_complete"] += 1
                    if applicable is True:
                        contract["applicable_evidence_complete"] += 1
                elif evidence_complete is False:
                    contract["evidence_incomplete"] += 1
                    if applicable is True:
                        contract["applicable_evidence_incomplete"] += 1
                    reasons = payload.get("reasons")
                    missing_reason = reasons.get("_missing") if isinstance(reasons, Mapping) else None
                    if isinstance(missing_reason, str) and missing_reason.strip():
                        contract["incomplete_with_reason"] += 1
                    else:
                        contract["incomplete_without_reason"] += 1
                        if len(incomplete_without_reason) < 10:
                            incomplete_without_reason.append(code)
                else:
                    contract["invalid_evidence_complete"] += 1

                sub_scores = payload.get("sub_scores")
                expected_dimensions = set(TYPE_WEIGHTS[type_key])
                valid_sub_scores = (
                    isinstance(sub_scores, Mapping)
                    and set(sub_scores) == expected_dimensions
                    and all(
                        not isinstance(value, bool)
                        and isinstance(value, (int, float))
                        and math.isfinite(float(value))
                        and 0.0 <= float(value) <= 10.0
                        for value in sub_scores.values()
                    )
                )
                if valid_sub_scores:
                    contract["valid_sub_scores"] += 1
                else:
                    contract["invalid_sub_scores"] += 1
                    if len(invalid_sub_scores) < 10:
                        invalid_sub_scores.append(code)
                decision_reasons = payload.get("reasons")
                patch7_fact_context = bool(
                    isinstance(decision_reasons, Mapping)
                    and (
                        _DECISION_MISSING_REQUIREMENTS_REASON in decision_reasons
                        or str(decision_reasons.get("_missing") or "").startswith("补丁7")
                    )
                )
                replayed_decision = _independent_decision_replay(
                    type_key,
                    payload,
                    company=company if patch7_fact_context else None,
                    dcf_result=(
                        dcf_results.get(code)
                        if patch7_fact_context and dcf_results is not None
                        else _DECISION_FACT_UNSET
                    ),
                )
                if replayed_decision is None:
                    import os

                    if os.environ.get("DS_DCF_DEBUG_REPLAY") and code in {"300760", "600285"}:
                        print(
                            "DEBUG_REPLAY",
                            code,
                            type_key,
                            json.dumps(
                                {
                                    "payload_keys": sorted(payload.keys()),
                                    "reasons_keys": sorted((payload.get("reasons") or {}).keys()),
                                    "reasons": payload.get("reasons"),
                                    "applicable": payload.get("applicable"),
                                    "evidence_complete": payload.get("evidence_complete"),
                                    "triggered": payload.get("triggered"),
                                    "veto": payload.get("veto"),
                                    "status": payload.get("status"),
                                    "sub_scores_keys": sorted((payload.get("sub_scores") or {}).keys()),
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            flush=True,
                        )
                    contract["invalid_decision"] += 1
                    if len(invalid_decisions) < 10:
                        invalid_decisions.append(code)
                else:
                    contract["valid_decision"] += 1
                    contract[
                        "decision_complete" if replayed_decision["decision_complete"] else "decision_incomplete"
                    ] += 1
                    if replayed_decision["potentially_triggerable"]:
                        contract["potentially_triggerable"] += 1
                    if replayed_decision["visible"]:
                        contract["decision_visible"] += 1
                    else:
                        contract["decision_hidden"] += 1
                    if replayed_decision["recall_safe"]:
                        contract["recall_safe"] += 1
                    else:
                        contract["recall_unsafe"] += 1
        framework_statuses[type_key] = dict(sorted(statuses.items()))
        framework_triggers[type_key] = triggers
        contract_fields = (
            "rows",
            "applicable",
            "not_applicable",
            "invalid_applicability",
            "evidence_complete",
            "evidence_incomplete",
            "invalid_evidence_complete",
            "applicable_evidence_complete",
            "applicable_evidence_incomplete",
            "incomplete_with_reason",
            "incomplete_without_reason",
            "valid_sub_scores",
            "invalid_sub_scores",
            "valid_decision",
            "invalid_decision",
            "decision_complete",
            "decision_incomplete",
            "potentially_triggerable",
            "decision_visible",
            "decision_hidden",
            "recall_safe",
            "recall_unsafe",
            "invalid_payload",
        )
        framework_evidence[type_key] = {
            **{field: int(contract.get(field, 0)) for field in contract_fields},
            "incomplete_without_reason_examples": incomplete_without_reason,
            "invalid_sub_score_examples": invalid_sub_scores,
            "invalid_decision_examples": invalid_decisions,
        }

    primary_counts = Counter(
        str(value)
        for value in scores.get("primary_type", pd.Series(dtype="object"))
        if isinstance(value, str) and value
    )
    evidence_levels: Counter[str] = Counter()
    quantitative_metric_level_counts: dict[str, Counter[str]] = {
        key: Counter() for key in sorted(_QUANTITATIVE_EVIDENCE_KEYS)
    }
    quantitative_missing_input_counts: dict[str, Counter[str]] = {
        key: Counter() for key in sorted(_QUANTITATIVE_EVIDENCE_KEYS)
    }
    quantitative_contract = Counter()
    quantitative_invalid_examples: list[dict[str, str]] = []
    quantitative_evidence_gap_examples: list[dict[str, object]] = []
    has_quantitative_column = "quantitative_evidence" in scores
    has_levels_column = "quantitative_evidence_levels" in scores
    has_status_column = "quantitative_evidence_status" in scores
    quantitative_summary_columns_present = has_levels_column and has_status_column
    for index, code in enumerate(codes):
        quantitative_contract["rows"] += 1
        invalid_reasons: list[str] = []
        score_row = scores.iloc[index]
        company_evidence = score_row.get("quantitative_evidence") if has_quantitative_column else None
        if not has_quantitative_column:
            quantitative_contract["missing_column"] += 1
            invalid_reasons.append("缺quantitative_evidence列")
        elif not isinstance(company_evidence, Mapping):
            quantitative_contract["non_mapping"] += 1
            invalid_reasons.append("量化证据不是映射")
        else:
            actual_keys = set(company_evidence)
            if actual_keys != _QUANTITATIVE_EVIDENCE_KEYS:
                quantitative_contract["key_mismatch"] += 1
                invalid_reasons.append("量化子指标集合不完整")

            payload_levels: dict[str, str] = {}
            for evidence_key in sorted(_QUANTITATIVE_EVIDENCE_KEYS & actual_keys):
                payload = company_evidence.get(evidence_key)
                if not isinstance(payload, Mapping):
                    quantitative_contract["invalid_record"] += 1
                    invalid_reasons.append(f"{evidence_key}记录不是映射")
                    continue
                raw_level = payload.get("evidence_level")
                if not isinstance(raw_level, str) or raw_level not in _QUANTITATIVE_RECORD_LEVELS:
                    quantitative_contract["invalid_level"] += 1
                    invalid_reasons.append(f"{evidence_key}证据等级无效")
                    continue
                try:
                    normalized_record = validate_quantitative_evidence_record(
                        payload,
                        key=evidence_key,
                        code=code,
                    )
                except (TypeError, ValueError) as exc:
                    quantitative_contract["invalid_record"] += 1
                    invalid_reasons.append(f"{evidence_key}记录合同无效:{exc}")
                    continue
                attached_level = score_row.get(f"{evidence_key}_evidence_level")
                attached_score = _finite_numeric(score_row.get(evidence_key))
                attached_evidence = score_row.get(f"{evidence_key}_evidence")
                attachment_valid = bool(
                    attached_level == raw_level
                    and (
                        (
                            raw_level in {"primary", "derived_proxy"}
                            and attached_score is not None
                            and math.isclose(
                                attached_score,
                                float(normalized_record["score"]),
                                rel_tol=0.0,
                                abs_tol=1e-12,
                            )
                            and attached_evidence == normalized_record["evidence"]
                        )
                        or (
                            raw_level in {"partial", "missing"}
                            and attached_score is None
                            and not isinstance(attached_evidence, Mapping)
                        )
                    )
                )
                if not attachment_valid:
                    quantitative_contract["attachment_mismatch"] += 1
                    invalid_reasons.append(f"{evidence_key}发布分数未绑定量化证据")
                    continue
                payload_levels[evidence_key] = raw_level

            if not quantitative_summary_columns_present:
                if has_levels_column or has_status_column:
                    quantitative_contract["summary_columns_mismatch"] += 1
                    invalid_reasons.append("量化证据等级与状态列未成对出现")
                else:
                    quantitative_contract["summary_columns_missing"] += 1
                    invalid_reasons.append("缺量化证据等级与状态列")
            else:
                published_levels = scores.iloc[index].get("quantitative_evidence_levels")
                published_status = scores.iloc[index].get("quantitative_evidence_status")
                levels_valid = bool(
                    isinstance(published_levels, Mapping)
                    and set(published_levels) == _QUANTITATIVE_EVIDENCE_KEYS
                    and all(
                        isinstance(level, str) and level in _QUANTITATIVE_EFFECTIVE_LEVELS
                        for level in published_levels.values()
                    )
                )
                levels_match_records = bool(
                    levels_valid
                    and all(
                        effective_level == payload_levels.get(key) for key, effective_level in published_levels.items()
                    )
                )
                if not levels_valid or not levels_match_records:
                    quantitative_contract["levels_mismatch"] += 1
                    invalid_reasons.append("量化证据等级汇总与子指标不一致")
                effective_levels = dict(published_levels) if levels_valid else {}
                expected_status = (
                    "complete"
                    if effective_levels
                    and all(level in {"primary", "derived_proxy"} for level in effective_levels.values())
                    else "missing"
                    if effective_levels and all(level == "missing" for level in effective_levels.values())
                    else "partial"
                )
                if published_status != expected_status:
                    quantitative_contract["status_mismatch"] += 1
                    invalid_reasons.append("量化证据状态与子指标不一致")

                if levels_valid:
                    evidence_levels.update(effective_levels.values())
                    for evidence_key in sorted(_QUANTITATIVE_EVIDENCE_KEYS):
                        level = effective_levels[evidence_key]
                        quantitative_metric_level_counts[evidence_key][level] += 1
                        if level not in {"partial", "missing"}:
                            continue
                        payload = company_evidence.get(evidence_key)
                        raw_missing_inputs = payload.get("missing_inputs") if isinstance(payload, Mapping) else None
                        details = payload.get("details") if isinstance(payload, Mapping) else None
                        quality = details.get("evidence_quality") if isinstance(details, Mapping) else None
                        if not isinstance(raw_missing_inputs, list) and isinstance(quality, Mapping):
                            raw_missing_inputs = quality.get("missing_inputs")
                        normalized_missing_inputs = (
                            [str(value) for value in raw_missing_inputs] if isinstance(raw_missing_inputs, list) else []
                        )
                        quantitative_missing_input_counts[evidence_key].update(normalized_missing_inputs)
                        if len(quantitative_evidence_gap_examples) < 25:
                            quantitative_evidence_gap_examples.append(
                                {
                                    "code": code,
                                    "metric": evidence_key,
                                    "level": level,
                                    "missing_inputs": normalized_missing_inputs,
                                }
                            )

        if invalid_reasons:
            quantitative_contract["invalid_rows"] += 1
            if len(quantitative_invalid_examples) < 25:
                quantitative_invalid_examples.append(
                    {
                        "code": code,
                        "reason": "；".join(dict.fromkeys(invalid_reasons)),
                    }
                )
        else:
            quantitative_contract["valid_rows"] += 1
    if "num_types" in scores:
        numeric_types = pd.to_numeric(scores["num_types"], errors="coerce").fillna(0)
        candidate_companies = int((numeric_types > 0).sum())
        total_framework_triggers = int(numeric_types.clip(lower=0).sum())
    else:
        candidate_companies = sum(
            any(
                isinstance(row.get(type_key), Mapping) and row[type_key].get("triggered") is True
                for type_key in _TYPE_KEYS
            )
            for row in scores.to_dict(orient="records")
        )
        total_framework_triggers = sum(framework_triggers.values())
    score_rows = len(scores)
    all_framework_payloads_present = score_rows > 0 and all(
        int(contract.get("rows", 0)) == score_rows and int(contract.get("invalid_payload", 0)) == 0
        for contract in framework_evidence.values()
    )
    all_sub_scores_valid = all_framework_payloads_present and all(
        int(contract.get("valid_sub_scores", 0)) == score_rows
        and int(contract.get("invalid_payload", 0)) == 0
        and int(contract.get("invalid_sub_scores", 0)) == 0
        for contract in framework_evidence.values()
    )
    all_applicable_frameworks_evidence_complete = all_framework_payloads_present and all(
        int(contract.get("applicable_evidence_incomplete", 0)) == 0
        and int(contract.get("invalid_evidence_complete", 0)) == 0
        for contract in framework_evidence.values()
    )
    all_incomplete_frameworks_explained = all(
        int(contract.get("incomplete_without_reason", 0)) == 0
        and int(contract.get("invalid_evidence_complete", 0)) == 0
        and int(contract.get("invalid_applicability", 0)) == 0
        for contract in framework_evidence.values()
    )
    all_decision_contracts_valid = all_framework_payloads_present and all(
        int(contract.get("valid_decision", 0)) == score_rows and int(contract.get("invalid_decision", 0)) == 0
        for contract in framework_evidence.values()
    )
    all_potential_candidates_visible = all_decision_contracts_valid and all(
        int(contract.get("decision_hidden", 0)) == 0 for contract in framework_evidence.values()
    )
    all_candidate_recall_paths_safe = all_decision_contracts_valid and all(
        int(contract.get("recall_unsafe", 0)) == 0 for contract in framework_evidence.values()
    )
    all_quantitative_evidence_records_valid = bool(
        score_rows > 0
        and int(quantitative_contract.get("valid_rows", 0)) == score_rows
        and int(quantitative_contract.get("invalid_rows", 0)) == 0
    )
    no_missing_quantitative_evidence = bool(
        all_quantitative_evidence_records_valid and evidence_levels.get("missing", 0) == 0
    )
    no_partial_quantitative_evidence = bool(
        all_quantitative_evidence_records_valid and evidence_levels.get("partial", 0) == 0
    )
    artifact_integrity_ready = bool(
        all_framework_payloads_present
        and all_sub_scores_valid
        and all_incomplete_frameworks_explained
        and all_quantitative_evidence_records_valid
        and all_decision_contracts_valid
    )
    candidate_visibility_ready = bool(artifact_integrity_ready and all_potential_candidates_visible)
    candidate_recall_ready = bool(
        artifact_integrity_ready and candidate_visibility_ready and all_candidate_recall_paths_safe
    )
    ideal_zero_gap_ready = bool(
        artifact_integrity_ready
        and candidate_visibility_ready
        and candidate_recall_ready
        and all_applicable_frameworks_evidence_complete
        and no_missing_quantitative_evidence
        and no_partial_quantitative_evidence
    )
    goal_readiness = {
        "all_framework_payloads_present": all_framework_payloads_present,
        "all_sub_scores_valid": all_sub_scores_valid,
        "all_applicable_frameworks_evidence_complete": all_applicable_frameworks_evidence_complete,
        "all_incomplete_frameworks_explained": all_incomplete_frameworks_explained,
        "all_quantitative_evidence_records_valid": all_quantitative_evidence_records_valid,
        "no_missing_quantitative_evidence": no_missing_quantitative_evidence,
        "no_partial_quantitative_evidence": no_partial_quantitative_evidence,
        "all_decision_contracts_valid": all_decision_contracts_valid,
        "all_potential_candidates_visible": all_potential_candidates_visible,
        "all_candidate_recall_paths_safe": all_candidate_recall_paths_safe,
        "artifact_integrity_ready": artifact_integrity_ready,
        "candidate_visibility_ready": candidate_visibility_ready,
        "candidate_recall_ready": candidate_recall_ready,
        "ideal_zero_gap_ready": ideal_zero_gap_ready,
    }
    # ``ready`` intentionally remains the strict ideal-data goal used by
    # ``--require-complete-evidence``.  Normal publication gates use the split
    # integrity/visibility fields above.
    goal_readiness["ready"] = ideal_zero_gap_ready
    return {
        "candidate_companies": candidate_companies,
        "total_framework_triggers": total_framework_triggers,
        "framework_trigger_counts": framework_triggers,
        "primary_trigger_counts": dict(sorted(primary_counts.items())),
        "framework_status_counts": framework_statuses,
        "framework_evidence_contract": framework_evidence,
        "quantitative_evidence_level_counts": dict(sorted(evidence_levels.items())),
        "quantitative_metric_level_counts": {
            key: dict(sorted(counts.items())) for key, counts in quantitative_metric_level_counts.items()
        },
        "quantitative_missing_input_counts": {
            key: dict(sorted(counts.items())) for key, counts in quantitative_missing_input_counts.items()
        },
        "quantitative_evidence_contract": {
            "model_id": QUANTITATIVE_EVIDENCE_MODEL_ID,
            "expected_metrics": sorted(_QUANTITATIVE_EVIDENCE_KEYS),
            "expected_metrics_per_row": len(_QUANTITATIVE_EVIDENCE_KEYS),
            "summary_columns_present": quantitative_summary_columns_present,
            **{
                key: int(quantitative_contract.get(key, 0))
                for key in (
                    "rows",
                    "valid_rows",
                    "invalid_rows",
                    "missing_column",
                    "non_mapping",
                    "key_mismatch",
                    "invalid_record",
                    "invalid_level",
                    "attachment_mismatch",
                    "summary_columns_missing",
                    "summary_columns_mismatch",
                    "levels_mismatch",
                    "status_mismatch",
                )
            },
            "invalid_examples": quantitative_invalid_examples,
        },
        "quantitative_evidence_gap_examples": quantitative_evidence_gap_examples,
        "goal_readiness": goal_readiness,
    }


def _non_economic_skip_details(classifications: object) -> dict[str, dict[str, str]]:
    """Expose every full-market data/model exception without dumping economic skips."""
    if not isinstance(classifications, Mapping):
        return {}
    details: dict[str, dict[str, str]] = {}
    for code, payload in classifications.items():
        if not isinstance(payload, Mapping):
            continue
        category = payload.get("category")
        reason = payload.get("reason")
        if (
            not isinstance(category, str)
            or not category
            or category == DCF_SKIP_ECONOMIC_NOT_APPLICABLE
            or not isinstance(reason, str)
            or not reason
        ):
            continue
        details.setdefault(category, {})[str(code)] = reason
    return {category: dict(sorted(code_reasons.items())) for category, code_reasons in sorted(details.items())}


def _snapshot_reporting_period_contract(snapshot: object) -> ReportingPeriodContract:
    """Freeze the schema-v6 snapshot period contract before any analysis runs."""
    validation = getattr(snapshot, "validation", None)
    raw_contract = validation.get("reporting_period_contract") if isinstance(validation, Mapping) else None
    if not isinstance(raw_contract, Mapping):
        raise RuntimeError("validated snapshot has no reporting_period_contract")
    if raw_contract.get("period_basis") != _STRICT_TTM_PERIOD_BASIS:
        raise RuntimeError("validated snapshot reporting_period_contract has an unsupported period basis")
    fields = (
        "annual_report_date",
        "current_interim_report_date",
        "prior_interim_report_date",
    )
    if any(not isinstance(raw_contract.get(field), str) for field in fields):
        raise RuntimeError("validated snapshot reporting_period_contract has invalid date fields")
    return ReportingPeriodContract(
        annual_report_date=raw_contract["annual_report_date"],
        current_interim_report_date=raw_contract["current_interim_report_date"],
        prior_interim_report_date=raw_contract["prior_interim_report_date"],
    )


def _load_market_coldness_evidence(
    snapshot: object,
    eligible_codes: Sequence[str],
    *,
    force_refresh: bool,
    reference_artifact_out: MutableMapping[str, object] | None = None,
    archive_candidate_out: MutableSequence[object] | None = None,
) -> tuple[dict[str, Mapping[str, object]], dict[str, object]]:
    """Acquire one SH/SZ batch; any unavailable state remains explicit and scoreless."""
    if reference_artifact_out is not None:
        reference_artifact_out.clear()
    if archive_candidate_out is not None:
        archive_candidate_out.clear()
    as_of_session = _snapshot_trade_session(snapshot)
    eligible = set(eligible_codes)
    if as_of_session is None:
        return {}, {
            "available": False,
            "evidence_available": False,
            "source": None,
            "source_url": None,
            "retrieved_at": None,
            "as_of_session": None,
            "fetched_count": 0,
            "total_expected": None,
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "source_coverage": None,
            "cache_hit": False,
            "cache_diagnostic": None,
            "reason": "snapshot validation does not contain exactly one trading source_trade_date",
            "evidence_reason": "missing_bound_as_of_session",
            "unavailable_policy": _MARKET_COLDNESS_UNAVAILABLE_POLICY,
        }
    snapshot_validation = getattr(snapshot, "validation", {})
    raw_listed_codes = tuple(
        snapshot_validation.get("analysis_market_codes", ()) if isinstance(snapshot_validation, Mapping) else ()
    )
    quote_frame = getattr(snapshot, "quotes", pd.DataFrame())
    quoted_analysis_codes = (
        set(
            quote_frame.loc[
                quote_frame["market"].astype(str).str.upper().isin({"SH", "SZ"}),
                "code",
            ]
        )
        if isinstance(quote_frame, pd.DataFrame) and {"code", "market"}.issubset(quote_frame.columns)
        else set()
    )
    if (
        not raw_listed_codes
        or len(raw_listed_codes) != len(set(raw_listed_codes))
        or any(not isinstance(code, str) or re.fullmatch(r"[036][0-9]{5}", code) is None for code in raw_listed_codes)
        or set(raw_listed_codes) != quoted_analysis_codes
        or not eligible.issubset(raw_listed_codes)
    ):
        raise RuntimeError("validated snapshot has an invalid listed-code boundary for market coldness")
    coldness_snapshot = None
    try:
        coldness_snapshot = load_market_coldness_session_snapshot(as_of_session)
        # Listing enrichment acquires the same validated whole-market source
        # batch during quote refresh. Reuse its safe cache here.
        # Historical audits must reuse the exact session's validated cache,
        # even after its routine acquisition TTL expires.  A different day's
        # turnover/volume ratio can never be rebound to this snapshot.
        if coldness_snapshot is None:
            coldness_snapshot = fetch_market_coldness_snapshot(
                force_refresh=False,
                allow_expired_cache=True,
            )
        evidence_diagnostics: dict[str, object] = {}
        evidence = build_market_coldness_evidence(
            coldness_snapshot,
            as_of_session=as_of_session,
            listed_quote_codes=raw_listed_codes,
            diagnostics=evidence_diagnostics,
        )
        should_refetch = evidence_diagnostics.get("evidence_reason") in {
            "stale_or_future_retrieval",
            "retrieval_before_close",
            "intraday_before_close",
        }
        if evidence_diagnostics.get("evidence_reason") == "session_retrieval_mismatch":
            retrieval_session = evidence_diagnostics.get("retrieval_session")
            requested_session = evidence_diagnostics.get("requested_session")
            # A source batch older than the bound quote session may be refreshed.
            # A newer source batch cannot recreate yesterday's evidence, so
            # repeatedly fetching it would only amplify a cross-generation race.
            try:
                retrieval_date = date.fromisoformat(retrieval_session) if isinstance(retrieval_session, str) else None
                requested_date = date.fromisoformat(requested_session) if isinstance(requested_session, str) else None
            except ValueError:
                retrieval_date = requested_date = None
            should_refetch = bool(
                retrieval_date is not None and requested_date is not None and retrieval_date < requested_date
            )
        if force_refresh and should_refetch:
            coldness_snapshot = fetch_market_coldness_snapshot(
                force_refresh=True,
                allow_expired_cache=False,
            )
            evidence_diagnostics = {}
            evidence = build_market_coldness_evidence(
                coldness_snapshot,
                as_of_session=as_of_session,
                listed_quote_codes=raw_listed_codes,
                diagnostics=evidence_diagnostics,
            )
        raw_evidence_codes = set(evidence)
        if any(
            not isinstance(code, str) or re.fullmatch(r"[036][0-9]{5}", code) is None for code in raw_evidence_codes
        ) or not raw_evidence_codes.issubset(raw_listed_codes):
            raise RuntimeError("market-coldness builder returned an identity outside the listed quote boundary")
        projected_evidence = {code: evidence[code] for code in sorted(eligible & raw_evidence_codes)}
        reference_artifact = _build_market_coldness_reference_artifact(
            coldness_snapshot,
            listed_codes=raw_listed_codes,
            as_of_session=as_of_session,
        )
        if reference_artifact_out is not None:
            reference_artifact_out.update(reference_artifact)
        eligible_count = len(projected_evidence)
        eligible_coverage = eligible_count / len(eligible) if eligible else 0.0
        not_applicable_by_reason = {reason: [] for reason in sorted(_MARKET_COLDNESS_NOT_APPLICABLE_REASONS)}
        data_gap_by_reason: dict[str, list[str]] = {}
        has_partition_diagnostics = evidence_diagnostics.get("diagnostics_schema_version") is not None
        if projected_evidence or has_partition_diagnostics:
            if (
                evidence_diagnostics.get("diagnostics_schema_version") != MARKET_COLDNESS_DIAGNOSTICS_SCHEMA_VERSION
                or evidence_diagnostics.get("eligible_candidate_count") != len(raw_listed_codes)
                or evidence_diagnostics.get("evidence_count") != len(evidence)
            ):
                raise RuntimeError("market-coldness builder diagnostics are incomplete or inconsistent")
            reasons = evidence_diagnostics.get("unscored_codes_by_reason")
            if not isinstance(reasons, Mapping):
                raise RuntimeError("market-coldness builder did not classify every unscored company")
            for reason, payload in reasons.items():
                if (
                    not isinstance(reason, str)
                    or not isinstance(payload, Mapping)
                    or set(payload) != {"classification", "count", "codes"}
                    or not isinstance(payload.get("codes"), list)
                    or payload["codes"] != sorted(payload["codes"])
                    or payload.get("count") != len(payload["codes"])
                ):
                    raise RuntimeError("market-coldness builder returned an invalid unscored classification")
                selected = sorted(eligible & set(payload["codes"]))
                classification = payload.get("classification")
                if classification == "model_not_applicable" and reason in _MARKET_COLDNESS_NOT_APPLICABLE_REASONS:
                    not_applicable_by_reason[reason] = selected
                elif classification == "data_missing":
                    if selected:
                        data_gap_by_reason[reason] = selected
                else:
                    raise RuntimeError("market-coldness builder returned an unsupported unscored classification")
        else:
            diagnostic_reason = str(evidence_diagnostics.get("evidence_reason") or "unclassified_data_gap")
            data_gap_by_reason[diagnostic_reason] = sorted(eligible)
        not_applicable_codes = set().union(*(set(codes) for codes in not_applicable_by_reason.values()))
        data_gap_codes = (
            set().union(*(set(codes) for codes in data_gap_by_reason.values())) if data_gap_by_reason else set()
        )
        if (
            set(projected_evidence) & (not_applicable_codes | data_gap_codes)
            or not_applicable_codes & data_gap_codes
            or set(projected_evidence) | not_applicable_codes | data_gap_codes != eligible
        ):
            raise RuntimeError("market-coldness eligible evidence and exclusions do not form a complete partition")
        applicable_count = len(eligible) - len(not_applicable_codes)
        applicable_coverage = eligible_count / applicable_count if applicable_count else 0.0
        excluded_noneligible = sorted(raw_evidence_codes - eligible)
        source_available = bool(coldness_snapshot.available)
        if not source_available:
            evidence_reason = "source_unavailable"
        elif data_gap_codes:
            diagnostic_reason = str(evidence_diagnostics.get("evidence_reason") or "eligible_data_gaps")
            evidence_reason = diagnostic_reason if diagnostic_reason != "available" else "eligible_data_gaps"
        elif not projected_evidence:
            evidence_reason = str(evidence_diagnostics.get("evidence_reason") or "no_scoreable_evidence")
        else:
            evidence_reason = "available"
        status: dict[str, object] = {
            "available": source_available,
            "evidence_available": bool(eligible_count),
            "model_id": MARKET_COLDNESS_MODEL_ID,
            "source": coldness_snapshot.source,
            "source_url": coldness_snapshot.source_url,
            "retrieved_at": coldness_snapshot.retrieved_at,
            "as_of_session": as_of_session,
            "fetched_count": coldness_snapshot.fetched_count,
            "total_expected": coldness_snapshot.total_expected,
            "eligible_evidence_count": eligible_count,
            "eligible_evidence_coverage": eligible_coverage,
            "eligible_applicable_count": applicable_count,
            "eligible_applicable_evidence_coverage": applicable_coverage,
            "eligible_not_applicable_count": len(not_applicable_codes),
            "eligible_not_applicable_codes_by_reason": not_applicable_by_reason,
            "eligible_unscored_data_gap_count": len(data_gap_codes),
            "eligible_unscored_data_gap_codes_by_reason": data_gap_by_reason,
            "full_listed_evidence_count": len(evidence),
            "reference_artifact_sha256": hashlib.sha256(
                _canonical_market_coldness_json(reference_artifact)
            ).hexdigest(),
            "excluded_noneligible_evidence_count": len(excluded_noneligible),
            "excluded_noneligible_evidence_codes": excluded_noneligible[:20],
            "excluded_noneligible_evidence_codes_truncated": len(excluded_noneligible) > 20,
            "source_coverage": coldness_snapshot.coverage.to_dict(),
            "cache_hit": coldness_snapshot.cache_hit,
            "cache_diagnostic": coldness_snapshot.cache_diagnostic,
            "reason": coldness_snapshot.reason,
            "evidence_reason": evidence_reason,
            "unavailable_policy": _MARKET_COLDNESS_UNAVAILABLE_POLICY,
            "evidence_diagnostics": evidence_diagnostics,
        }
        if (
            archive_candidate_out is not None
            and source_available
            and not data_gap_codes
            and evidence_reason == "available"
        ):
            # Persistence is deliberately deferred until the independent
            # full-universe release replay succeeds in the caller.  Otherwise
            # one incomplete raw batch could poison the immutable session cache
            # before the missing listed company is detected.
            archive_candidate_out.append(coldness_snapshot)
        return projected_evidence, status
    except Exception as exc:
        if reference_artifact_out is not None:
            reference_artifact_out.clear()
        if archive_candidate_out is not None:
            archive_candidate_out.clear()
        return {}, {
            "available": False,
            "evidence_available": False,
            "model_id": MARKET_COLDNESS_MODEL_ID,
            "source": getattr(coldness_snapshot, "source", None),
            "source_url": getattr(coldness_snapshot, "source_url", None),
            "retrieved_at": getattr(coldness_snapshot, "retrieved_at", None),
            "as_of_session": as_of_session,
            "fetched_count": getattr(coldness_snapshot, "fetched_count", 0),
            "total_expected": getattr(coldness_snapshot, "total_expected", None),
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "eligible_applicable_count": len(eligible),
            "eligible_applicable_evidence_coverage": 0.0,
            "eligible_not_applicable_count": 0,
            "eligible_not_applicable_codes_by_reason": {
                reason: [] for reason in sorted(_MARKET_COLDNESS_NOT_APPLICABLE_REASONS)
            },
            "eligible_unscored_data_gap_count": len(eligible),
            "eligible_unscored_data_gap_codes_by_reason": {"validation_or_acquisition_error": sorted(eligible)},
            "source_coverage": None,
            "cache_hit": getattr(coldness_snapshot, "cache_hit", False),
            "cache_diagnostic": getattr(coldness_snapshot, "cache_diagnostic", None),
            "reason": f"{type(exc).__name__}: {exc}",
            "evidence_reason": "validation_or_acquisition_error",
            "unavailable_policy": _MARKET_COLDNESS_UNAVAILABLE_POLICY,
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    starting_state = audit_state_hashes()
    # A non-refresh audit is a deterministic replay of the latest still-valid
    # promoted generation.  Its cache TTL therefore matches the snapshot's
    # explicit stale limit instead of the UI's short routine-refresh interval.
    cache = SafeFileCache(
        DEFAULT_SNAPSHOT_PATH,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        ttl=MAX_STALE_AGE_SECONDS,
    )
    snapshot = get_market_snapshot(
        DataFetcher(
            enrich_listing_dates=True,
            force_reference_refresh=args.refresh,
        ),
        cache,
        force_refresh=args.refresh,
        allow_expired_cache=not args.refresh,
        persist_network=False,
    )
    if not _refresh_completed(args.refresh, getattr(snapshot, "source", None)):
        warning = " ".join(str(getattr(snapshot, "warning", "") or "").split())
        warning_detail = f"; source warning: {warning}" if warning else ""
        raise RuntimeError(
            "fresh market refresh did not complete; existing audit artifacts were preserved" + warning_detail
        )
    reporting_period_contract = _snapshot_reporting_period_contract(snapshot)
    eligible = snapshot.eligible_codes
    market_coldness_reference_artifact: dict[str, object] = {}
    market_coldness_archive_candidates: list[object] = []
    market_coldness_evidence, market_coldness_status = _load_market_coldness_evidence(
        snapshot,
        eligible,
        force_refresh=args.refresh,
        reference_artifact_out=market_coldness_reference_artifact,
        archive_candidate_out=market_coldness_archive_candidates,
    )
    _require_market_coldness_release_evidence(
        market_coldness_evidence,
        market_coldness_status,
        reference_artifact=market_coldness_reference_artifact,
        eligible_codes=eligible,
        as_of_session=_snapshot_trade_session(snapshot),
    )
    if len(market_coldness_archive_candidates) != 1:
        raise RuntimeError("validated market-coldness evidence has no unique archive candidate")
    archive_market_coldness_session_snapshot(
        market_coldness_archive_candidates[0],
        _snapshot_trade_session(snapshot),
    )
    analysis = run_market_analysis(
        snapshot.analysis_quotes,
        snapshot.analysis_financials,
        eligible_codes=eligible,
        enforce_quality=True,
        expected_companies=len(eligible),
        previous_quality=_comparison_quality(snapshot),
        reporting_period_contract=reporting_period_contract,
        market_coldness_evidence=market_coldness_evidence,
        quality_history_loader=fetch_quality_history_batch,
        type3_growth_loader=fetch_growth_evidence_batch,
        research_report_loader=fetch_research_reports_batch,
        patch4_loader=fetch_patch4_evidence_batch,
    )
    screening_coverage = _analysis_coverage_summary(analysis.scores, analysis.dcf_results)
    if args.require_complete_evidence and not bool(screening_coverage["goal_readiness"]["ready"]):
        print(
            json.dumps(
                {
                    "refresh_requested": bool(args.refresh),
                    "refresh_completed": _refresh_completed(args.refresh, snapshot.source),
                    "snapshot_source": snapshot.source,
                    "quotes": len(snapshot.quotes),
                    "eligible": len(eligible),
                    "score_rows": len(analysis.scores),
                    "screening_coverage": screening_coverage,
                    "pipeline_issues": len(analysis.issues),
                    "analysis_quality": dict(analysis.quality),
                    "strict_evidence_gate": "failed_before_snapshot_promotion_or_audit_write",
                },
                ensure_ascii=True,
                indent=2,
                default=str,
            )
        )
        return 1
    active_payload_sha256 = snapshot.baseline_payload_sha256
    if snapshot.source == "network":
        saved = save_market_snapshot(
            cache,
            snapshot.quotes,
            snapshot.financials,
            data_timestamp=snapshot.data_timestamp,
            retrieved_at=snapshot.retrieved_at,
            analysis_quality=analysis.quality,
            expected_previous_timestamp=snapshot.baseline_timestamp,
            expected_previous_payload_sha256=snapshot.baseline_payload_sha256,
        )
        active_payload_sha256 = saved.get("payload_sha256")
    if not isinstance(active_payload_sha256, str):
        raise RuntimeError("active snapshot has no verified payload identity")

    snapshot_artifact = cache.read_bytes_if_payload(active_payload_sha256)
    snapshot_sha256 = hashlib.sha256(snapshot_artifact).hexdigest().upper()
    audit = audit_random_sample(
        snapshot.quotes,
        snapshot.financials,
        eligible_codes=eligible,
        seed=args.seed,
        sample_size=args.sample_size,
        snapshot_sha256=snapshot_sha256,
        provenance={
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_source": snapshot.source,
            "snapshot_payload_sha256": active_payload_sha256,
            "snapshot_artifact_bytes": len(snapshot_artifact),
            "validation": dict(snapshot.validation),
            "full_market_quality": dict(analysis.quality),
            "market_coldness": dict(market_coldness_status),
            "market_coldness_reference_artifact": market_coldness_reference_artifact,
            "strict_evidence_required": bool(args.require_complete_evidence),
            "screening_coverage": screening_coverage,
        },
        full_market_analysis=analysis,
        reporting_period_contract=reporting_period_contract,
        market_coldness_evidence=market_coldness_evidence,
        quality_history_evidence=analysis.quality_history_evidence,
        type3_growth_evidence=getattr(analysis, "type3_growth_evidence", {}),
        research_report_evidence=getattr(analysis, "research_report_evidence", {}),
        patch4_evidence=getattr(analysis, "patch4_evidence", {}),
    )
    ending_state = audit_state_hashes()
    provenance_state = {key: audit.provenance.get(key) for key in starting_state}
    if starting_state != ending_state or provenance_state != starting_state:
        raise RuntimeError("source, rules, industry data, or dependency manifests changed during audit")
    paths = write_audit_artifacts(audit, args.output_dir, data_timestamp=snapshot.data_timestamp)
    if audit_state_hashes() != ending_state:
        for path in paths.values():
            path.unlink(missing_ok=True)
        raise RuntimeError("source state changed while audit artifacts were being written")
    skip_classifications = getattr(analysis, "dcf_skip_classifications", {})
    skip_categories = (
        Counter(
            str(value.get("category") or "missing_category")
            for value in skip_classifications.values()
            if isinstance(value, Mapping)
        )
        if isinstance(skip_classifications, Mapping)
        else Counter()
    )
    summary = {
        "refresh_requested": bool(args.refresh),
        "refresh_completed": _refresh_completed(args.refresh, snapshot.source),
        "snapshot_source": snapshot.source,
        "snapshot_warning": getattr(snapshot, "warning", ""),
        "snapshot_cache_diagnostic": dict(getattr(snapshot, "cache_diagnostic", {}) or {}),
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "quotes": len(snapshot.quotes),
        "eligible": len(eligible),
        "score_rows": len(analysis.scores),
        "dcf_valid": len(analysis.dcf_results),
        "dcf_skipped": analysis.dcf_skipped,
        "dcf_skip_reason_counts": dict(sorted(Counter(analysis.dcf_skip_reasons.values()).items())),
        "dcf_skip_classification_counts": dict(sorted(skip_categories.items())),
        "dcf_non_economic_skip_details": _non_economic_skip_details(skip_classifications),
        "screening_coverage": screening_coverage,
        "pipeline_issues": len(analysis.issues),
        "analysis_quality": dict(analysis.quality),
        "market_coldness": dict(market_coldness_status),
        "random_sample_size": audit.sample_size,
        "random_engine_errors": list(audit.engine_invariant_errors),
        "random_scoring_replay_errors": list(audit.scoring_replay_errors),
        "random_valuation_replay_errors": list(audit.valuation_replay_errors),
        "random_independent_errors": list(audit.independent_errors),
        "snapshot_sha256": snapshot_sha256,
        "snapshot_payload_sha256": active_payload_sha256,
        "artifacts": {key: str(value) for key, value in paths.items()},
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, default=str))
    # The production quality gate intentionally tolerates a very small issue
    # rate so the UI can retain a prior good generation during source outages.
    # A release audit is stricter: even an issue outside the sampled 100 rows
    # must fail the command and cannot be represented as a clean release.
    evidence_ready = bool(summary["screening_coverage"]["goal_readiness"]["ready"])
    return (
        1
        if (
            not summary["refresh_completed"]
            or audit.invariant_errors
            or analysis.issues
            or (args.require_complete_evidence and not evidence_ready)
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
