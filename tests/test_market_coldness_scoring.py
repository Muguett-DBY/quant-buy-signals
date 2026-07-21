from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from data.market_coldness import (
    MarketColdnessCoverage,
    MarketColdnessRecord,
    MarketColdnessSnapshot,
    MetricCoverage,
)
from engine.market_coldness import (
    MARKET_COLDNESS_MODEL_ID,
    MAX_COLDNESS_SCORE,
    MAX_SCORE_WITHOUT_VOLUME_RATIO,
    MarketColdnessScoringError,
    build_market_coldness_evidence,
)


RETRIEVED = "2026-07-15T08:00:00+00:00"
NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)


def _record(
    code: str,
    *,
    change_60d_pct: float | None,
    change_ytd_pct: float | None,
    turnover_rate_pct: float | None,
    volume_ratio: float | None,
    listing_date: str | None = "2001-08-27",
    retrieved_at: str = RETRIEVED,
) -> MarketColdnessRecord:
    exchange = "SH" if code.startswith("6") else "SZ"
    return MarketColdnessRecord(
        code=code,
        exchange=exchange,
        eastmoney_market_id=1 if exchange == "SH" else 0,
        name=f"样本{code}",
        change_60d_pct=change_60d_pct,
        change_ytd_pct=change_ytd_pct,
        turnover_rate_pct=turnover_rate_pct,
        volume_ratio=volume_ratio,
        listing_date=listing_date,
        source="Eastmoney push2 clist",
        source_url="https://push2delay.eastmoney.com/api/qt/clist/get",
        retrieved_at=retrieved_at,
        upstream_fields={},
        missing_reasons={},
    )


def _snapshot(*records: MarketColdnessRecord) -> MarketColdnessSnapshot:
    metrics = ("change_60d_pct", "change_ytd_pct", "turnover_rate_pct", "volume_ratio", "listing_date")
    by_metric = {}
    for metric in metrics:
        present = sum(getattr(record, metric) is not None for record in records)
        by_metric[metric] = MetricCoverage(
            present=present,
            missing=len(records) - present,
            coverage_rate=present / len(records) if records else None,
        )
    complete = sum(all(getattr(record, metric) is not None for metric in metrics) for record in records)
    return MarketColdnessSnapshot(
        available=True,
        records=tuple(records),
        source="Eastmoney push2 clist",
        source_url="https://push2delay.eastmoney.com/api/qt/clist/get",
        retrieved_at=records[0].retrieved_at if records else RETRIEVED,
        total_expected=len(records),
        fetched_count=len(records),
        page_count=1,
        response_bytes=100,
        universe_coverage_rate=1.0,
        coverage=MarketColdnessCoverage(
            len(records),
            complete,
            complete / len(records) if records else None,
            by_metric,
        ),
        cache_hit=False,
        cache_diagnostic="",
        reason="",
        failure=None,
    )


def test_cold_neutral_and_hot_quantity_price_evidence_are_ordered_and_capped():
    snapshot = _snapshot(
        _record("600001", change_60d_pct=-35, change_ytd_pct=-45, turnover_rate_pct=0.3, volume_ratio=0.4),
        _record("600002", change_60d_pct=0, change_ytd_pct=0, turnover_rate_pct=3, volume_ratio=1.1),
        _record("000001", change_60d_pct=60, change_ytd_pct=80, turnover_rate_pct=30, volume_ratio=5),
    )

    evidence = build_market_coldness_evidence(
        snapshot,
        as_of_session="2026-07-15",
        now=NOW,
        min_cross_section_records=3,
        min_board_turnover_records=3,
    )

    cold = evidence["600001"]["market_coldness_score"]
    neutral = evidence["600002"]["market_coldness_score"]
    hot = evidence["000001"]["market_coldness_score"]
    assert hot < neutral < cold
    assert cold == MAX_COLDNESS_SCORE
    assert hot >= 1.0


def test_score_is_independent_of_valuation_and_has_traceable_dated_evidence():
    snapshot = _snapshot(
        _record("600519", change_60d_pct=-8, change_ytd_pct=-10, turnover_rate_pct=1.0, volume_ratio=0.8)
    )

    result = build_market_coldness_evidence(
        snapshot,
        as_of_session="2026-07-15",
        now=NOW,
        min_cross_section_records=1,
        min_board_turnover_records=1,
    )["600519"]

    assert set(result) == {"market_coldness_score", "market_coldness_score_evidence", "components"}
    metadata = result["market_coldness_score_evidence"]
    assert metadata["as_of"] == "2026-07-15"
    assert MARKET_COLDNESS_MODEL_ID in metadata["evidence_id"]
    assert "PE" not in metadata["summary"]
    assert "PB" not in metadata["summary"]
    assert "60日-8.0%" in metadata["summary"]


def test_missing_required_observation_is_not_converted_to_zero():
    snapshot = _snapshot(
        _record("600001", change_60d_pct=None, change_ytd_pct=-10, turnover_rate_pct=1.0, volume_ratio=0.8),
        _record("600002", change_60d_pct=-10, change_ytd_pct=None, turnover_rate_pct=1.0, volume_ratio=0.8),
        _record("600003", change_60d_pct=-10, change_ytd_pct=-10, turnover_rate_pct=None, volume_ratio=0.8),
    )

    assert (
        build_market_coldness_evidence(
            snapshot,
            as_of_session="2026-07-15",
            now=NOW,
            min_cross_section_records=1,
        )
        == {}
    )


def test_missing_optional_volume_ratio_renormalizes_weights_and_lowers_cap():
    snapshot = _snapshot(
        _record("600001", change_60d_pct=-80, change_ytd_pct=-90, turnover_rate_pct=0.1, volume_ratio=None)
    )

    result = build_market_coldness_evidence(
        snapshot,
        as_of_session="2026-07-15",
        now=NOW,
        min_cross_section_records=1,
        min_board_turnover_records=1,
    )["600001"]

    assert result["market_coldness_score"] == MAX_SCORE_WITHOUT_VOLUME_RATIO
    assert result["components"]["score_cap"] == MAX_SCORE_WITHOUT_VOLUME_RATIO
    assert "量比缺失" in result["market_coldness_score_evidence"]["summary"]


def test_recent_listing_has_insufficient_cycle_history():
    snapshot = _snapshot(
        _record(
            "600001",
            change_60d_pct=-20,
            change_ytd_pct=-20,
            turnover_rate_pct=1.0,
            volume_ratio=0.8,
            listing_date="2026-06-01",
        )
    )

    assert (
        build_market_coldness_evidence(
            snapshot,
            as_of_session="2026-07-15",
            now=NOW,
            min_cross_section_records=1,
        )
        == {}
    )


def test_full_market_future_source_only_listing_is_isolated_without_collapsing_listed_evidence():
    listed_records = tuple(
        _record(
            f"{600000 + index:06d}",
            change_60d_pct=-20,
            change_ytd_pct=-20,
            turnover_rate_pct=1.0,
            volume_ratio=0.8,
        )
        for index in range(1_000)
    )
    future_source_only = _record(
        "688806",
        change_60d_pct=None,
        change_ytd_pct=None,
        turnover_rate_pct=None,
        volume_ratio=None,
        listing_date="2026-08-03",
    )
    diagnostics = {}

    evidence = build_market_coldness_evidence(
        _snapshot(*listed_records, future_source_only),
        as_of_session="2026-07-15",
        listed_quote_codes=tuple(record.code for record in listed_records),
        now=NOW,
        min_cross_section_records=1_000,
        diagnostics=diagnostics,
    )

    assert len(evidence) == 1_000
    assert set(evidence) == {record.code for record in listed_records}
    assert "688806" not in evidence
    assert diagnostics["evidence_reason"] == "available"
    assert diagnostics["listed_quote_binding_count"] == 1_000
    assert diagnostics["isolated_future_listing_count"] == 1
    assert diagnostics["isolated_future_listing_codes"] == ["688806"]
    assert diagnostics["isolated_future_listing_codes_truncated"] is False
    assert diagnostics["excluded_unbound_source_record_count"] == 0


def test_future_listing_for_bound_listed_quote_still_fails_closed():
    future = _record(
        "688806",
        change_60d_pct=-20,
        change_ytd_pct=-20,
        turnover_rate_pct=1.0,
        volume_ratio=0.8,
        listing_date="2026-08-03",
    )

    with pytest.raises(MarketColdnessScoringError, match="future listing date for 688806"):
        build_market_coldness_evidence(
            _snapshot(future),
            as_of_session="2026-07-15",
            listed_quote_codes=("688806",),
            now=NOW,
            min_cross_section_records=1,
        )


def test_future_listing_without_independent_quote_binding_remains_strict():
    future = _record(
        "688806",
        change_60d_pct=-20,
        change_ytd_pct=-20,
        turnover_rate_pct=1.0,
        volume_ratio=0.8,
        listing_date="2026-08-03",
    )

    with pytest.raises(MarketColdnessScoringError, match="future listing date for 688806"):
        build_market_coldness_evidence(
            _snapshot(future),
            as_of_session="2026-07-15",
            now=NOW,
            min_cross_section_records=1,
        )


@pytest.mark.parametrize(
    "identity_override",
    [
        {"exchange": "SZ"},
        {"eastmoney_market_id": 0},
        {"eastmoney_market_id": 1.0},
    ],
)
def test_unbound_record_identity_mismatch_is_rejected_before_isolation(identity_override):
    listed = _record(
        "600001",
        change_60d_pct=-20,
        change_ytd_pct=-20,
        turnover_rate_pct=1.0,
        volume_ratio=0.8,
    )
    corrupt_future = replace(
        _record(
            "688806",
            change_60d_pct=-20,
            change_ytd_pct=-20,
            turnover_rate_pct=1.0,
            volume_ratio=0.8,
            listing_date="2026-08-03",
        ),
        **identity_override,
    )

    with pytest.raises(MarketColdnessScoringError, match="identity mismatch for 688806"):
        build_market_coldness_evidence(
            _snapshot(listed, corrupt_future),
            as_of_session="2026-07-15",
            listed_quote_codes=("600001",),
            now=NOW,
            min_cross_section_records=1,
        )


def test_source_only_diagnostics_are_bounded_and_past_rows_do_not_enter_cross_section():
    listed = _record(
        "600001",
        change_60d_pct=-20,
        change_ytd_pct=-20,
        turnover_rate_pct=1.0,
        volume_ratio=0.8,
    )
    past_source_only = _record(
        "600002",
        change_60d_pct=-20,
        change_ytd_pct=-20,
        turnover_rate_pct=1.0,
        volume_ratio=0.8,
    )
    future_source_only = tuple(
        _record(
            f"{688700 + index:06d}",
            change_60d_pct=-20,
            change_ytd_pct=-20,
            turnover_rate_pct=1.0,
            volume_ratio=0.8,
            listing_date="2026-08-03",
        )
        for index in range(25)
    )
    diagnostics = {}

    evidence = build_market_coldness_evidence(
        _snapshot(listed, past_source_only, *future_source_only),
        as_of_session="2026-07-15",
        listed_quote_codes=("600001",),
        now=NOW,
        min_cross_section_records=1,
        min_board_turnover_records=1,
        diagnostics=diagnostics,
    )

    assert set(evidence) == {"600001"}
    assert diagnostics["isolated_future_listing_count"] == 25
    assert len(diagnostics["isolated_future_listing_codes"]) == diagnostics["diagnostic_code_limit"] == 20
    assert diagnostics["isolated_future_listing_codes_truncated"] is True
    assert diagnostics["excluded_unbound_source_record_count"] == 1
    assert diagnostics["excluded_unbound_source_record_codes"] == ["600002"]
    assert diagnostics["excluded_unbound_source_record_codes_truncated"] is False


@pytest.mark.parametrize(
    "listed_quote_codes",
    [
        "600001",
        (),
        ("600001", "600001"),
        ("920001",),
        (600001,),
    ],
)
def test_listed_quote_binding_must_be_nonempty_unique_canonical_sh_sz_codes(listed_quote_codes):
    snapshot = _snapshot(
        _record("600001", change_60d_pct=-20, change_ytd_pct=-20, turnover_rate_pct=1.0, volume_ratio=0.8)
    )

    with pytest.raises(ValueError, match="listed_quote_codes"):
        build_market_coldness_evidence(
            snapshot,
            as_of_session="2026-07-15",
            listed_quote_codes=listed_quote_codes,
            now=NOW,
            min_cross_section_records=1,
        )


def test_unavailable_or_stale_snapshot_returns_no_evidence():
    available = _snapshot(
        _record("600001", change_60d_pct=-20, change_ytd_pct=-20, turnover_rate_pct=1.0, volume_ratio=0.8)
    )
    unavailable = replace(available, available=False, records=(), fetched_count=0, total_expected=None)
    stale_now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert build_market_coldness_evidence(unavailable, as_of_session="2026-07-15", now=NOW) == {}
    assert build_market_coldness_evidence(available, as_of_session="2026-07-15", now=stale_now) == {}


def test_available_snapshot_count_or_identity_corruption_is_rejected():
    record = _record("600001", change_60d_pct=-20, change_ytd_pct=-20, turnover_rate_pct=1.0, volume_ratio=0.8)
    bad_count = replace(_snapshot(record), fetched_count=2)
    duplicate = _snapshot(record, record)

    with pytest.raises(MarketColdnessScoringError, match="count mismatch"):
        build_market_coldness_evidence(bad_count, as_of_session="2026-07-15", now=NOW)
    with pytest.raises(MarketColdnessScoringError, match="duplicate"):
        build_market_coldness_evidence(
            duplicate,
            as_of_session="2026-07-15",
            now=NOW,
            min_cross_section_records=1,
        )


def test_negative_activity_metric_is_rejected_instead_of_scored():
    snapshot = _snapshot(
        _record("600001", change_60d_pct=-20, change_ytd_pct=-20, turnover_rate_pct=-1, volume_ratio=0.8)
    )

    with pytest.raises(MarketColdnessScoringError, match="negative activity"):
        build_market_coldness_evidence(
            snapshot,
            as_of_session="2026-07-15",
            now=NOW,
            min_cross_section_records=1,
        )


def test_price_heat_guard_cannot_be_overridden_by_low_turnover_or_volume():
    snapshot = _snapshot(
        _record("600001", change_60d_pct=20, change_ytd_pct=30, turnover_rate_pct=0.3, volume_ratio=0.4)
    )

    result = build_market_coldness_evidence(
        snapshot,
        as_of_session="2026-07-15",
        now=NOW,
        min_cross_section_records=1,
        min_board_turnover_records=1,
    )["600001"]

    assert result["market_coldness_score"] <= 3.0
    assert "60d_hot_cap=3.0" in result["components"]["caps"]


def test_missing_listing_date_and_zero_turnover_never_become_cold_signals():
    missing_listing = _snapshot(
        _record(
            "600001",
            change_60d_pct=-30,
            change_ytd_pct=-30,
            turnover_rate_pct=0.2,
            volume_ratio=0.3,
            listing_date=None,
        )
    )
    zero_trade = _snapshot(
        _record("600001", change_60d_pct=-30, change_ytd_pct=-30, turnover_rate_pct=0, volume_ratio=0)
    )

    for snapshot in (missing_listing, zero_trade):
        assert (
            build_market_coldness_evidence(
                snapshot,
                as_of_session="2026-07-15",
                now=NOW,
                min_cross_section_records=1,
            )
            == {}
        )


def test_same_session_intraday_evidence_is_provisional_and_not_decision_eligible():
    intraday_retrieved = "2026-07-16T01:00:00+00:00"
    snapshot = _snapshot(
        _record(
            "600001",
            change_60d_pct=-20,
            change_ytd_pct=-20,
            turnover_rate_pct=1.0,
            volume_ratio=0.8,
            retrieved_at=intraday_retrieved,
        )
    )
    intraday = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)

    diagnostics = {}
    assert (
        build_market_coldness_evidence(
            snapshot,
            as_of_session="2026-07-16",
            now=intraday,
            min_cross_section_records=1,
            diagnostics=diagnostics,
        )
        == {}
    )
    assert diagnostics == {
        "evidence_available": False,
        "evidence_reason": "intraday_before_close",
        "decision_eligible_after": "15:15:00 Asia/Shanghai",
    }


def test_different_session_trading_activity_is_never_rebound_to_the_snapshot():
    snapshot = _snapshot(
        _record("600001", change_60d_pct=-20, change_ytd_pct=-20, turnover_rate_pct=1.0, volume_ratio=0.8)
    )
    diagnostics = {}

    assert (
        build_market_coldness_evidence(
            snapshot,
            as_of_session="2026-07-16",
            now=NOW,
            min_cross_section_records=1,
            diagnostics=diagnostics,
        )
        == {}
    )
    assert diagnostics["evidence_reason"] == "session_retrieval_mismatch"
    assert diagnostics["retrieval_session"] == "2026-07-15"
    assert diagnostics["requested_session"] == "2026-07-16"
