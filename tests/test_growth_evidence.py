from __future__ import annotations

import copy
from datetime import date
import json
import math

import pandas as pd
import pytest

from data import datacenter as dc
from data import growth_evidence as ge
from engine import buy_screener as bs
from engine import quantitative_evidence as qe


class _NoWait:
    def acquire(self):
        return None


class _FakeResponse:
    def __init__(
        self,
        payload,
        *,
        url=ge.EASTMONEY_BUSINESS_ENDPOINT,
        content_type="application/json; charset=utf-8",
        declared_length=None,
        raw=None,
    ):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if raw is None else raw
        self._chunks = [encoded]
        self.url = url
        self.headers = {"Content-Type": content_type}
        if declared_length is not None:
            self.headers["Content-Length"] = str(declared_length)
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _segment_row(
    year,
    item,
    revenue,
    share,
    *,
    code="600519",
    mainop_type=2,
    rank=1,
):
    return {
        "SECUCODE": f"{code}.{'SH' if code.startswith('6') else 'SZ'}",
        "SECURITY_CODE": code,
        "REPORT_DATE": f"{year}-12-31 00:00:00",
        "MAINOP_TYPE": mainop_type,
        "ITEM_NAME": item,
        "MAIN_BUSINESS_INCOME": revenue,
        "MBI_RATIO": share,
        "MAIN_BUSINESS_COST": None,
        "MBC_RATIO": None,
        "MAIN_BUSINESS_RPOFIT": None,
        "MBR_RATIO": None,
        "GROSS_RPOFIT_RATIO": None,
        "RANK": rank,
    }


def _segment_payload(*, code="600519", years=(2023, 2024, 2025)):
    values = {
        2016: (10.0, 10.0),
        2017: (15.0, 12.0),
        2018: (20.0, 15.0),
        2019: (25.0, 20.0),
        2020: (32.0, 25.0),
        2021: (40.0, 30.0),
        2022: (50.0, 35.0),
        2023: (60.0, 40.0),
        2024: (75.0, 45.0),
        2025: (90.0, 50.0),
    }
    rows = []
    for year in years:
        left, right = values[year]
        total = left + right
        rows.extend(
            [
                _segment_row(year, "产品甲", left, left / total, code=code, rank=1),
                _segment_row(year, "产品乙", right, right / total, code=code, rank=2),
            ]
        )
    return {"zyfw": [], "zygcfx": rows, "jyps": []}


def _financial_records(values):
    return [{"year": year, "value": value} for year, value in values]


def _acquisition_row(year, *, code="600519", total=10.0, capex=10.0, acquisition=None):
    row = {
        "SECURITY_CODE": code,
        "REPORT_DATE": f"{year}-12-31",
        "NOTICE_DATE": f"{year + 1}-04-30",
        "SOURCE_REPORT_NAME": dc.RPT_DETAILED_CASHFLOW,
        "SOURCE_REPORT_URL": ge.EASTMONEY_DATACENTER_URL,
        "TOTAL_INVEST_OUTFLOW": total,
        "TOTAL_INVEST_INFLOW": 0.0,
        "INVEST_NETCASH_OTHER": 0.0,
        "INVEST_NETCASH_BALANCE": 0.0,
        "NETCASH_INVEST": -total,
    }
    row.update({field: None for field in ge._EXTERNAL_COMPONENT_FIELDS})
    row[ge.CAPEX_FIELD] = capex
    row["OBTAIN_SUBSIDIARY_OTHER"] = acquisition
    return row


def _complete_inputs():
    revenues = _financial_records([(2021, 70.0), (2022, 85.0), (2023, 100.0), (2024, 120.0), (2025, 140.0)])
    goodwill = _financial_records([(2021, 1.0), (2022, 1.5), (2023, 2.0), (2024, 3.0), (2025, 4.0)])
    acquisitions = [_acquisition_row(year) for year in range(2021, 2026)]
    return revenues, goodwill, acquisitions


def test_fetch_growth_evidence_has_exact_top_level_contract_and_auditable_metrics():
    response = _FakeResponse(_segment_payload())
    session = _FakeSession([response])
    revenues, goodwill, acquisitions = _complete_inputs()

    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=session,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert set(result.to_dict()) == {
        "available",
        "code",
        "as_of",
        "model_id",
        "external_growth_evidence",
        "segment_growth_sources",
        "cache_hit",
        "cache_diagnostic",
        "reason",
    }
    assert result.available
    assert result.model_id == "type3-growth-evidence-v1"
    segment = result.segment_growth_sources
    assert segment["status"] == "complete"
    assert segment["dimension"] == "product"
    assert segment["history_years"] == [2023, 2024, 2025]
    assert segment["growth_source_count"] == 2
    assert 1.0 <= segment["effective_growth_source_count"] <= 2.0
    assert segment["positive_growth_share"] == pytest.approx(1.0)
    assert segment["revenue_hhi"] == pytest.approx((90 / 140) ** 2 + (50 / 140) ** 2)
    assert segment["aggregate_revenue_cagr"] == pytest.approx(math.sqrt(1.4) - 1)
    assert segment["matched_latest_share"] == pytest.approx(1.0)
    assert len(segment["segments"]) == 2
    assert all(record["security_code"] == "600519" for record in segment["records"])

    external = result.external_growth_evidence
    assert external["status"] == "complete"
    assert external["contract_scope"] == "aggregate_proxy_not_transaction_census"
    assert external["coverage_years"] == [2021, 2022, 2023, 2024, 2025]
    assert external["coverage_year_count"] == 5
    assert external["acquisition_cash_values"] == [0.0] * 5
    assert external["positive_goodwill_additions_to_revenue"] == pytest.approx(3.0 / 445.0)
    assert all(record["acquisition_value_basis"] == "derived_aggregate_identity_zero" for record in external["records"])
    assert "不提供逐笔并购交易清单" in external["limitations"]
    assert response.closed
    assert session.calls[0][0] == ge.EASTMONEY_BUSINESS_ENDPOINT
    assert session.calls[0][1]["params"] == {"code": "SH600519"}
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["allow_redirects"] is True


def test_validated_adapter_output_reaches_both_type3_quantitative_contracts():
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    external, segments = bs._type3_growth_components_from_evidence(
        result.to_dict(),
        code="600519",
        as_of="2026-07-17",
    )

    external_inputs = qe._external_growth_proxy_inputs(external)
    segment_inputs = qe._segment_growth_proxy_inputs(segments)
    assert external_inputs is not None
    assert external_inputs["acquisition_intensity"] == 0.0
    assert external_inputs["goodwill_change_to_revenue"] == pytest.approx(3.0 / 445.0)
    assert segment_inputs is not None
    assert segment_inputs["history_years"] == 3.0
    assert 1.0 <= segment_inputs["growth_source_count"] <= 2.0


def test_segment_history_preserves_a_full_ten_year_moutai_style_window():
    years = tuple(range(2016, 2026))
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload(years=years))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.available
    assert result.segment_growth_sources["history_years"] == list(years)
    assert len(result.segment_growth_sources["records"]) == 20
    first_segment = result.segment_growth_sources["segments"][0]
    assert first_segment["first_year"] == 2016
    assert first_segment["latest_year"] == 2025
    assert first_segment["cagr"] == pytest.approx((90.0 / 10.0) ** (1 / 9) - 1)


def test_effective_growth_source_count_does_not_turn_tiny_segments_into_three_sources():
    rows = []
    values = {
        2023: (100.0, 1.0, 1.0),
        2024: (150.0, 1.5, 1.5),
        2025: (200.0, 2.0, 2.0),
    }
    for year, year_values in values.items():
        total = sum(year_values)
        for rank, (name, revenue) in enumerate(
            zip(("核心", "微小一", "微小二"), year_values),
            start=1,
        ):
            rows.append(_segment_row(year, name, revenue, revenue / total, rank=rank))
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse({"zyfw": [], "zygcfx": rows, "jyps": []})]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    segment = result.segment_growth_sources
    assert segment["status"] == "complete"
    assert segment["growth_source_count"] == 3
    assert segment["effective_growth_source_count"] < 1.05


def test_segment_identity_rename_below_95_percent_remains_partial_instead_of_veto_evidence():
    rows = []
    for year in (2023, 2024):
        rows.extend(
            [
                _segment_row(year, "旧核心产品", 90.0 + 10 * (year - 2023), 0.9, rank=1),
                _segment_row(year, "稳定业务", 10.0, 0.1, rank=2),
            ]
        )
    rows.extend(
        [
            _segment_row(2025, "新核心产品名称", 120.0, 120 / 132, rank=1),
            _segment_row(2025, "稳定业务", 12.0, 12 / 132, rank=2),
        ]
    )
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse({"zyfw": [], "zygcfx": rows, "jyps": []})]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    segment = result.segment_growth_sources
    assert segment["status"] == "partial"
    assert segment["matched_latest_share"] < 0.95
    assert segment["reason"] == "latest_segment_identity_match_below_95_percent"
    assert not result.available


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record["segment_growth_sources"].__setitem__("growth_source_count", 99),
        lambda record: record["segment_growth_sources"].__setitem__("effective_growth_source_count", 99.0),
        lambda record: record["segment_growth_sources"].__setitem__("matched_latest_share", 0.5),
        lambda record: record["segment_growth_sources"].__setitem__("revenue_hhi", 0.01),
        lambda record: record["segment_growth_sources"].__setitem__(
            "positive_growth_share",
            0.01,
        ),
        lambda record: record["external_growth_evidence"].__setitem__(
            "aggregate_acquisition_cash_to_revenue",
            0.5,
        ),
        lambda record: record["external_growth_evidence"].__setitem__(
            "positive_goodwill_additions_to_revenue",
            0.5,
        ),
        lambda record: record["external_growth_evidence"].__setitem__(
            "coverage_year_count",
            4,
        ),
        lambda record: record["segment_growth_sources"]["history_years"].__setitem__(
            0,
            2023.0,
        ),
        lambda record: record["external_growth_evidence"].__setitem__(
            "coverage_year_count",
            True,
        ),
        lambda record: record.__setitem__("available", False),
    ],
)
def test_public_validator_recomputes_summaries_and_rejects_tampering(mutation):
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        use_cache=False,
        rate_limiter=_NoWait(),
    ).to_dict()
    tampered = copy.deepcopy(result)
    mutation(tampered)

    with pytest.raises(ge.GrowthEvidenceError):
        ge.validate_growth_evidence_record(
            tampered,
            "600519",
            "2026-07-17",
        )


def test_unresolved_acquisition_null_is_not_coerced_to_zero_and_keeps_evidence_partial():
    revenues, goodwill, acquisitions = _complete_inputs()
    acquisitions[-1] = _acquisition_row(2025, total=15.0, capex=10.0)

    evidence = ge.build_external_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
    )

    assert evidence["status"] == "partial"
    assert evidence["coverage_years"] == []
    assert evidence["acquisition_cash_values"] == []
    assert "fewer_than_five" in evidence["reason"]


def test_reported_and_uniquely_derived_acquisition_values_keep_provenance():
    revenues, goodwill, acquisitions = _complete_inputs()
    acquisitions[0] = _acquisition_row(2021, total=12.0, capex=10.0, acquisition=2.0)
    uniquely_derived = _acquisition_row(2022, total=13.0, capex=10.0)
    for field in ge._EXTERNAL_COMPONENT_FIELDS:
        if field not in {ge.CAPEX_FIELD, "OBTAIN_SUBSIDIARY_OTHER"}:
            uniquely_derived[field] = 0.0
    acquisitions[1] = uniquely_derived

    evidence = ge.build_external_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
    )

    assert evidence["status"] == "complete"
    assert evidence["acquisition_cash_values"] == [2.0, 3.0, 0.0, 0.0, 0.0]
    assert [record["acquisition_value_basis"] for record in evidence["records"]] == [
        "source_reported",
        "derived_aggregate_identity_residual",
        "derived_aggregate_identity_zero",
        "derived_aggregate_identity_zero",
        "derived_aggregate_identity_zero",
    ]
    derivation = evidence["records"][1]["acquisition_derivation"]
    assert set(derivation["component_values"]) == set(ge._EXTERNAL_COMPONENT_FIELDS)
    assert derivation["component_values"]["OBTAIN_SUBSIDIARY_OTHER"] is None
    assert derivation["component_values"][ge.CAPEX_FIELD] == 10.0
    assert derivation["rounding_tolerance_cny"] == 0.1


def test_decimal_cashflow_evidence_uses_the_same_canonical_hash_as_its_validator():
    """A decimal source amount must not be downgraded by a float-hash drift."""

    revenues = _financial_records([(year, 3.3 * (year - 2019)) for year in range(2021, 2026)])
    goodwill = _financial_records([(year, 0.33 * (year - 2020)) for year in range(2021, 2026)])
    acquisitions = []
    for year in range(2021, 2026):
        row = _acquisition_row(
            year,
            total=3.3 * (year - 2019) + 0.17,
            capex=3.3 * (year - 2019),
            acquisition=0.17,
        )
        acquisitions.append(row)

    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.available
    validated = ge.validate_growth_evidence_record(result.to_dict(), "600519", "2026-07-17")
    assert validated["external_growth_evidence"]["evidence_id"] == result.external_growth_evidence["evidence_id"]


def test_external_proxy_requires_five_years_and_allows_falling_goodwill():
    revenues, _goodwill, acquisitions = _complete_inputs()
    falling_goodwill = _financial_records([(2021, 10.0), (2022, 8.0), (2023, 6.0), (2024, 4.0), (2025, 2.0)])
    complete = ge.build_external_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=falling_goodwill,
        acquisition_cashflow_records=acquisitions,
    )
    assert complete["status"] == "complete"
    assert complete["coverage_year_count"] == 5
    assert complete["positive_goodwill_additions_to_revenue"] == 0.0
    assert complete["goodwill_change_to_revenue"] < 0

    four_years = ge.build_external_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues[1:],
        goodwill_records=falling_goodwill[1:],
        acquisition_cashflow_records=acquisitions[1:],
    )
    assert four_years["status"] == "partial"
    assert four_years["coverage_year_count"] == 4
    assert "fewer_than_five" in four_years["reason"]


def test_segment_source_fails_closed_on_cross_code_duplicate_or_bad_share():
    revenues, goodwill, acquisitions = _complete_inputs()
    payloads = []
    cross_code = _segment_payload()
    cross_code["zygcfx"][-1]["SECURITY_CODE"] = "000001"
    payloads.append(cross_code)
    duplicate = _segment_payload()
    duplicate["zygcfx"].append(dict(duplicate["zygcfx"][-1]))
    payloads.append(duplicate)
    bad_share = _segment_payload()
    bad_share["zygcfx"][-1]["MBI_RATIO"] = 1.5
    payloads.append(bad_share)

    for payload in payloads:
        result = ge.fetch_growth_evidence(
            "600519",
            "2026-07-17",
            revenue_records=revenues,
            goodwill_records=goodwill,
            acquisition_cashflow_records=acquisitions,
            session=_FakeSession([_FakeResponse(payload)]),
            use_cache=False,
            rate_limiter=_NoWait(),
        )
        assert not result.available
        assert result.segment_growth_sources["status"] == "unavailable"
        assert result.segment_growth_sources["records"] == []


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            _FakeResponse(
                _segment_payload(),
                url="https://evil.example/PC_HSF10/BusinessAnalysis/PageAjax",
            ),
            "pinned HTTPS",
        ),
        (_FakeResponse(_segment_payload(), content_type="text/html"), "not JSON"),
        (
            _FakeResponse(
                _segment_payload(),
                declared_length=ge.MAX_RESPONSE_BYTES + 1,
            ),
            "byte limit",
        ),
    ],
)
def test_segment_source_rejects_redirect_mime_and_oversize(response, reason):
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([response]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert reason in result.segment_growth_sources["reason"]
    assert response.closed


def test_segment_source_rejects_duplicate_json_keys():
    raw = b'{"zyfw":[],"zygcfx":[],"zygcfx":[],"jyps":[]}'
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(None, raw=raw)]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert result.segment_growth_sources["status"] == "unavailable"
    assert "duplicate key" in result.segment_growth_sources["reason"]


def test_as_of_cutoff_excludes_not_yet_completed_annual_reports():
    response = _FakeResponse(_segment_payload(years=(2021, 2022, 2023, 2024, 2025)))
    revenues = _financial_records([(2021, 70.0), (2022, 85.0), (2023, 100.0)])
    goodwill = _financial_records([(2021, 1.0), (2022, 1.5), (2023, 2.0)])
    acquisitions = [_acquisition_row(year) for year in (2021, 2022, 2023)]

    result = ge.fetch_growth_evidence(
        "600519",
        "2025-04-30",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([response]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.segment_growth_sources["history_years"] == [2021, 2022, 2023]
    assert max(record["report_date"] for record in result.segment_growth_sources["records"]) == "2023-12-31"
    assert result.external_growth_evidence["coverage_years"] == [2021, 2022, 2023]
    assert result.external_growth_evidence["coverage_year_count"] == 3
    assert result.external_growth_evidence["status"] == "partial"


def test_segment_history_gap_is_explicit_partial_not_a_backshifted_complete_window():
    revenues, goodwill, acquisitions = _complete_inputs()
    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload(years=(2022, 2023, 2025)))]),
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert not result.available
    assert result.segment_growth_sources["status"] == "partial"
    assert result.segment_growth_sources["history_years"] == [2025]
    assert "fewer_than_three" in result.segment_growth_sources["reason"]


def test_segment_cache_is_validated_and_avoids_second_network_call(tmp_path):
    revenues, goodwill, acquisitions = _complete_inputs()
    first = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        cache_dir=tmp_path,
        cache_ttl_seconds=3_600,
        rate_limiter=_NoWait(),
    )
    second = ge.fetch_growth_evidence(
        "600519",
        "2026-07-17",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([]),
        cache_dir=tmp_path,
        cache_ttl_seconds=3_600,
        rate_limiter=_NoWait(),
    )

    assert first.available and second.available
    assert second.cache_hit
    assert second.cache_diagnostic == "hit"
    assert second.segment_growth_sources == first.segment_growth_sources


def test_segment_cache_reuses_21_day_raw_capture_and_revalidates_current_as_of(tmp_path):
    revenues, goodwill, acquisitions = _complete_inputs()
    captured = ge.fetch_growth_evidence(
        "600519",
        "2026-07-08",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        cache_dir=tmp_path,
        cache_ttl_seconds=1,
        rate_limiter=_NoWait(),
    )
    offline = _FakeSession([])

    rebased = ge.fetch_growth_evidence(
        "600519",
        "2026-07-29",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=offline,
        cache_dir=tmp_path,
        cache_ttl_seconds=1,
        rate_limiter=_NoWait(),
    )

    assert offline.calls == []
    assert rebased.available
    assert rebased.cache_hit
    assert rebased.cache_diagnostic == "reused_source_as_of:2026-07-08"
    assert rebased.as_of == "2026-07-29"
    assert rebased.segment_growth_sources["as_of"] == "2026-07-29"
    assert rebased.segment_growth_sources["annual_revenue_latest"] == pytest.approx(revenues[-1]["value"])
    assert rebased.segment_growth_sources["records"] == captured.segment_growth_sources["records"]
    assert rebased.segment_growth_sources["evidence_id"] != captured.segment_growth_sources["evidence_id"]


def test_segment_cache_does_not_reuse_after_21_days_or_across_annual_cutoff(tmp_path):
    revenues, goodwill, acquisitions = _complete_inputs()
    ge.fetch_growth_evidence(
        "600519",
        "2026-07-08",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        cache_dir=tmp_path / "age",
        rate_limiter=_NoWait(),
    )
    after_window = _FakeSession([_FakeResponse(_segment_payload())])
    refreshed = ge.fetch_growth_evidence(
        "600519",
        "2026-07-30",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=after_window,
        cache_dir=tmp_path / "age",
        rate_limiter=_NoWait(),
    )
    assert len(after_window.calls) == 1
    assert not refreshed.cache_hit

    prior_revenues = _financial_records([(2020, 60.0), (2021, 70.0), (2022, 85.0), (2023, 100.0), (2024, 120.0)])
    prior_goodwill = _financial_records([(2020, 0.5), (2021, 1.0), (2022, 1.5), (2023, 2.0), (2024, 3.0)])
    prior_acquisitions = [_acquisition_row(year) for year in range(2020, 2025)]
    ge.fetch_growth_evidence(
        "600519",
        "2026-04-30",
        revenue_records=prior_revenues,
        goodwill_records=prior_goodwill,
        acquisition_cashflow_records=prior_acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload(years=(2022, 2023, 2024)))]),
        cache_dir=tmp_path / "cutoff",
        rate_limiter=_NoWait(),
    )
    state = ge.load_growth_evidence_cache_batch_state(
        [{"code": "600519", "as_of": "2026-05-01"}],
        cache_dir=tmp_path / "cutoff",
    )
    assert state == {}


def test_segment_cache_never_reuses_a_future_dated_capture(tmp_path):
    revenues, goodwill, acquisitions = _complete_inputs()
    ge.fetch_growth_evidence(
        "600519",
        "2026-07-30",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload())]),
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )
    older_session = _FakeSession([_FakeResponse(_segment_payload())])

    result = ge.fetch_growth_evidence(
        "600519",
        "2026-07-29",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=older_session,
        cache_dir=tmp_path,
        rate_limiter=_NoWait(),
    )

    assert len(older_session.calls) == 1
    assert not result.cache_hit


def test_historical_segment_reuse_excludes_failed_and_incomplete_captures(tmp_path):
    revenues, goodwill, acquisitions = _complete_inputs()
    incomplete = ge.fetch_growth_evidence(
        "600519",
        "2026-07-08",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([_FakeResponse(_segment_payload(years=(2024, 2025)))]),
        cache_dir=tmp_path / "incomplete",
        rate_limiter=_NoWait(),
    )
    assert incomplete.segment_growth_sources["status"] == "partial"
    assert (
        ge.load_growth_evidence_cache_batch_state(
            [{"code": "600519", "as_of": "2026-07-09"}],
            cache_dir=tmp_path / "incomplete",
        )
        == {}
    )
    refresh_session = _FakeSession([_FakeResponse(_segment_payload())])
    refreshed_segment, cache_hit, diagnostic = ge._fetch_segment_growth_sources(
        "600519",
        date.fromisoformat("2026-07-08"),
        session=refresh_session,
        cache_dir=tmp_path / "incomplete",
        rate_limiter=_NoWait(),
        recent_cache_state={},
    )
    assert len(refresh_session.calls) == 1
    assert refreshed_segment["status"] == "complete"
    assert not cache_hit
    assert diagnostic.endswith(";saved")

    failed = ge.fetch_growth_evidence(
        "600519",
        "2026-07-08",
        revenue_records=revenues,
        goodwill_records=goodwill,
        acquisition_cashflow_records=acquisitions,
        session=_FakeSession([]),
        cache_dir=tmp_path / "failed",
        rate_limiter=_NoWait(),
    )
    assert failed.segment_growth_sources["status"] == "unavailable"
    assert list((tmp_path / "failed").glob("*.json.gz")) == []
    assert (
        ge.load_growth_evidence_cache_batch_state(
            [{"code": "600519", "as_of": "2026-07-09"}],
            cache_dir=tmp_path / "failed",
        )
        == {}
    )


def test_type3_retry_state_persists_deterministic_transient_and_structural_backoff(tmp_path):
    initial_requests = [
        {"code": "600519", "as_of": "2025-07-29"},
        {"code": "000001", "as_of": "2025-07-29"},
    ]
    recorded = ge.record_growth_evidence_retry_states(
        initial_requests,
        {
            "600519": {
                "code": "600519",
                "as_of": "2025-07-29",
                "model_id": ge.MODEL_ID,
                "available": False,
                "reason": "segment:source_unavailable:Timeout",
            },
            "000001": {
                "code": "000001",
                "as_of": "2025-07-29",
                "model_id": ge.MODEL_ID,
                "available": False,
                "reason": "external:fewer_than_five_consecutive_completed_years",
            },
        },
        cache_dir=tmp_path,
    )

    assert recorded["600519"]["retry_class"] == "transient"
    assert recorded["600519"]["retry_after"] == "2025-07-30"
    assert recorded["000001"]["retry_class"] == "structural"
    assert recorded["000001"]["retry_after"] == "2025-08-05"
    resumed = ge.load_growth_evidence_retry_state_batch(
        [
            {"code": "600519", "as_of": "2025-07-30"},
            {"code": "000001", "as_of": "2025-07-30"},
        ],
        cache_dir=tmp_path,
    )
    assert resumed == recorded

    assert (
        ge.record_growth_evidence_retry_states(
            [{"code": "000002", "as_of": "2025-07-29"}],
            {
                "000002": {
                    "code": "000002",
                    "as_of": "2025-07-29",
                    "model_id": ge.MODEL_ID,
                    "available": True,
                    "reason": "",
                }
            },
            cache_dir=tmp_path,
        )
        == {}
    )
    assert (
        ge.load_growth_evidence_retry_state_batch(
            [{"code": "000002", "as_of": "2025-07-30"}],
            cache_dir=tmp_path,
        )
        == {}
    )


@pytest.mark.parametrize("exception_type", [ge.SafeCacheError, OSError, TypeError])
def test_type3_retry_state_rejects_tampered_backoff_and_cache_write_failure(
    monkeypatch,
    tmp_path,
    exception_type,
):
    ge.SafeFileCache(
        ge._type3_growth_retry_state_path("000003", tmp_path),
        schema_version=ge.CACHE_SCHEMA_VERSION,
        ttl=ge.CACHE_TTL_SECONDS,
        max_uncompressed_bytes=16 * 1024,
    ).save(
        {
            "model_id": ge.TYPE3_GROWTH_RETRY_MODEL_ID,
            "code": "000003",
            "last_attempt_as_of": "2025-07-29",
            "retry_class": "structural",
            "retry_after": "2025-07-30",
            "reason": "fewer_than_five_consecutive_completed_years",
        }
    )
    assert (
        ge.load_growth_evidence_retry_state_batch(
            [{"code": "000003", "as_of": "2025-07-30"}],
            cache_dir=tmp_path,
        )
        == {}
    )

    class _FailingCache:
        def __init__(self, *_args, **_kwargs):
            pass

        def save(self, _value):
            raise exception_type("simulated cache write failure")

    monkeypatch.setattr(ge, "SafeFileCache", _FailingCache)
    assert (
        ge.record_growth_evidence_retry_states(
            [{"code": "000004", "as_of": "2025-07-29"}],
            {
                "000004": {
                    "code": "000004",
                    "as_of": "2025-07-29",
                    "model_id": ge.MODEL_ID,
                    "available": False,
                    "reason": "segment:source_unavailable:Timeout",
                }
            },
            cache_dir=tmp_path,
        )
        == {}
    )


def test_batch_contract_is_exact_sorted_bounded_and_fetches_one_cashflow_group(monkeypatch, tmp_path):
    assert ge.MAX_BATCH_COMPANIES >= 5_200
    calls = []
    segment_annual_revenue = {}

    def fake_cashflow(years, *, codes):
        calls.append((tuple(years), tuple(codes)))
        return pd.DataFrame([_acquisition_row(year, code=code) for code in codes for year in range(2021, 2026)])

    def fake_segment(code, cutoff, **kwargs):
        segment_annual_revenue[code] = kwargs.get("annual_revenue")
        payload = ge._validate_business_payload(
            _segment_payload(code=code),
            code=code,
            as_of=cutoff,
        )
        return ge._build_segment_growth_sources(code, cutoff, payload), False, "disabled"

    monkeypatch.setattr(ge, "fetch_detailed_annual_cashflow_history", fake_cashflow)
    monkeypatch.setattr(ge, "_fetch_segment_growth_sources", fake_segment)
    revenues, goodwill, _ = _complete_inputs()
    progress = []
    result = ge.fetch_growth_evidence_batch(
        [
            {
                "code": "600519",
                "as_of": "2026-07-17",
                "revenue_records": revenues,
                "goodwill_records": goodwill,
            },
            {
                "code": "000001",
                "as_of": "2026-07-17",
                "revenue_records": revenues,
                "goodwill_records": goodwill,
            },
        ],
        max_workers=2,
        progress_cb=lambda done, total: progress.append((done, total)),
        cache_dir=tmp_path,
    )

    assert list(result) == ["000001", "600519"]
    assert all(item["available"] for item in result.values())
    assert calls == [((2025, 2024, 2023, 2022, 2021), ("000001", "600519"))]
    expected_revenue = {int(record["year"]): float(record["value"]) for record in revenues}
    assert segment_annual_revenue == {"000001": expected_revenue, "600519": expected_revenue}
    assert sorted(progress) == [(1, 2), (2, 2)]
    with pytest.raises(ValueError, match="request shape"):
        ge.fetch_growth_evidence_batch(
            [
                {
                    "code": "600519",
                    "as_of": "2026-07-17",
                    "revenue_records": revenues,
                    "goodwill_records": goodwill,
                    "unexpected": True,
                }
            ]
        )
    with pytest.raises(ValueError, match="duplicate"):
        ge.fetch_growth_evidence_batch(
            [
                {
                    "code": "600519",
                    "as_of": "2026-07-17",
                    "revenue_records": revenues,
                    "goodwill_records": goodwill,
                },
                {
                    "code": "600519",
                    "as_of": "2026-07-17",
                    "revenue_records": revenues,
                    "goodwill_records": goodwill,
                },
            ]
        )


def test_external_cache_reuses_complete_batch_evidence_and_skips_cashflow_fetch(monkeypatch, tmp_path):
    calls = []

    def fake_cashflow(years, *, codes):
        calls.append((tuple(years), tuple(codes)))
        return pd.DataFrame([_acquisition_row(year, code=code) for code in codes for year in years])

    def fake_segment(code, cutoff, **_kwargs):
        payload = ge._validate_business_payload(
            _segment_payload(code=code),
            code=code,
            as_of=cutoff,
        )
        return ge._build_segment_growth_sources(code, cutoff, payload), False, "disabled"

    monkeypatch.setattr(ge, "fetch_detailed_annual_cashflow_history", fake_cashflow)
    monkeypatch.setattr(ge, "_fetch_segment_growth_sources", fake_segment)
    revenues, goodwill, _ = _complete_inputs()
    request = {
        "code": "600519",
        "as_of": "2026-07-17",
        "revenue_records": revenues,
        "goodwill_records": goodwill,
    }

    first = ge.fetch_growth_evidence_batch([request], max_workers=1, cache_dir=tmp_path)
    assert calls == [((2025, 2024, 2023, 2022, 2021), ("600519",))]
    assert first["600519"]["external_growth_evidence"]["status"] == "complete"

    calls.clear()
    second = ge.fetch_growth_evidence_batch([request], max_workers=1, cache_dir=tmp_path)

    assert calls == []
    assert second["600519"]["cache_hit"]
    assert second["600519"]["cache_diagnostic"] == "disabled;external:hit"
    assert second["600519"]["external_growth_evidence"] == first["600519"]["external_growth_evidence"]


def test_external_cache_rebases_only_identical_financial_inputs(monkeypatch, tmp_path):
    calls = []

    def fake_cashflow(years, *, codes):
        calls.append((tuple(years), tuple(codes)))
        return pd.DataFrame([_acquisition_row(year, code=code) for code in codes for year in years])

    def fake_segment(code, cutoff, **_kwargs):
        payload = ge._validate_business_payload(
            _segment_payload(code=code),
            code=code,
            as_of=cutoff,
        )
        return ge._build_segment_growth_sources(code, cutoff, payload), False, "disabled"

    monkeypatch.setattr(ge, "fetch_detailed_annual_cashflow_history", fake_cashflow)
    monkeypatch.setattr(ge, "_fetch_segment_growth_sources", fake_segment)
    revenues, goodwill, _ = _complete_inputs()
    initial = {
        "code": "600519",
        "as_of": "2026-07-08",
        "revenue_records": revenues,
        "goodwill_records": goodwill,
    }
    captured = ge.fetch_growth_evidence_batch([initial], max_workers=1, cache_dir=tmp_path)
    assert len(calls) == 1

    calls.clear()
    rebased = ge.fetch_growth_evidence_batch(
        [{**initial, "as_of": "2026-07-29"}],
        max_workers=1,
        cache_dir=tmp_path,
    )
    assert calls == []
    assert rebased["600519"]["cache_diagnostic"] == "disabled;external:reused_source_as_of:2026-07-08"
    assert rebased["600519"]["external_growth_evidence"]["as_of"] == "2026-07-29"
    assert (
        rebased["600519"]["external_growth_evidence"]["evidence_id"]
        != captured["600519"]["external_growth_evidence"]["evidence_id"]
    )

    changed_goodwill = copy.deepcopy(goodwill)
    changed_goodwill[-1]["value"] = 4.5
    calls.clear()
    refreshed = ge.fetch_growth_evidence_batch(
        [
            {
                "code": "600519",
                "as_of": "2026-07-29",
                "revenue_records": revenues,
                "goodwill_records": changed_goodwill,
            }
        ],
        max_workers=1,
        cache_dir=tmp_path,
    )
    assert calls == [((2025, 2024, 2023, 2022, 2021), ("600519",))]
    assert refreshed["600519"]["external_growth_evidence"]["records"][-1]["goodwill"] == 4.5


def _detailed_annual_row(report_date: str, *, code: str = "000001"):
    row = {
        "SECURITY_CODE": code,
        "SECUCODE": f"{code}.{'SH' if code.startswith('6') else 'SZ'}",
        "SECURITY_NAME_ABBR": f"样本{code}",
        "SECURITY_TYPE_CODE": "058001001",
        "REPORT_DATE": f"{report_date} 00:00:00",
        "REPORT_TYPE": "年报",
        "REPORT_DATE_NAME": f"{report_date[:4]}年报",
        "NOTICE_DATE": f"{int(report_date[:4]) + 1}-04-30 00:00:00",
        "UPDATE_DATE": f"{int(report_date[:4]) + 1}-04-30 00:00:00",
        "CURRENCY": "CNY",
        "NETCASH_OPERATE": None,
        "NETCASH_FINANCE": None,
    }
    row.update({field: None for field in dc._DETAILED_INVESTMENT_FIELDS})
    row["TOTAL_INVEST_OUTFLOW"] = 10.0
    return row


def test_detailed_annual_cashflow_fetch_preserves_null_and_validates_annual_metadata(monkeypatch):
    calls = []

    def fake_all(report_name, columns, extra_filter, **_kwargs):
        report_date = extra_filter.split("'")[1]
        calls.append((report_name, columns, extra_filter))
        return pd.DataFrame([_detailed_annual_row(report_date)])

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)
    frame = dc.fetch_detailed_annual_cashflow_history([2025, 2024], codes=["000001"])

    assert [item[0] for item in calls] == [dc.RPT_DETAILED_CASHFLOW] * 2
    assert all('SECURITY_TYPE_CODE="058001001"' in item[2] for item in calls)
    assert all('SECURITY_CODE in ("000001")' in item[2] for item in calls)
    assert frame["REPORT_DATE"].tolist() == ["2024-12-31", "2025-12-31"]
    assert frame["OBTAIN_SUBSIDIARY_OTHER"].isna().all()
    assert frame["SOURCE_REPORT_NAME"].eq(dc.RPT_DETAILED_CASHFLOW).all()

    with pytest.raises(ValueError, match="must not contain duplicates"):
        dc.fetch_detailed_annual_cashflow_history([2025, 2025], codes=["000001"])
    with pytest.raises(ValueError, match="between 1990"):
        dc.fetch_detailed_annual_cashflow_history([date.today().year + 1], codes=["000001"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("REPORT_TYPE", "中报", "REPORT_TYPE"),
        ("REPORT_DATE_NAME", "2025中报", "REPORT_DATE_NAME"),
        ("CURRENCY", "USD", "non-CNY"),
        ("OBTAIN_SUBSIDIARY_OTHER", float("inf"), "non-finite"),
    ],
)
def test_detailed_annual_cashflow_rejects_incompatible_or_nonfinite_rows(field, value, message):
    row = _detailed_annual_row("2025-12-31")
    row[field] = value
    with pytest.raises(dc.DataFetchError, match=message):
        dc._validate_detailed_annual_cashflow(
            pd.DataFrame([row]),
            ["2025-12-31"],
        )


def test_growth_input_contract_rejects_future_duplicate_and_nonfinite_records_before_network():
    revenues, goodwill, acquisitions = _complete_inputs()
    session = _FakeSession([])
    with pytest.raises(ValueError, match="record shape"):
        ge.fetch_growth_evidence(
            "600519",
            "2026-07-17",
            revenue_records=[{"year": 2025, "value": 1.0, "extra": 1}],
            goodwill_records=goodwill,
            acquisition_cashflow_records=acquisitions,
            session=session,
            use_cache=False,
        )
    with pytest.raises((ValueError, ge.GrowthEvidenceError), match="duplicate"):
        ge.fetch_growth_evidence(
            "600519",
            "2026-07-17",
            revenue_records=[
                {"year": 2025, "value": 1.0},
                {"year": 2025, "value": 2.0},
            ],
            goodwill_records=goodwill,
            acquisition_cashflow_records=acquisitions,
            session=session,
            use_cache=False,
        )
    with pytest.raises((ValueError, ge.GrowthEvidenceError), match="finite"):
        ge.fetch_growth_evidence(
            "600519",
            "2026-07-17",
            revenue_records=[{"year": 2025, "value": float("inf")}],
            goodwill_records=goodwill,
            acquisition_cashflow_records=acquisitions,
            session=session,
            use_cache=False,
        )
    assert session.calls == []


def test_growth_date_parser_rejects_future_as_of():
    tomorrow_year = date.today().year + 1
    with pytest.raises(ValueError, match="future"):
        ge.fetch_growth_evidence_batch(
            [
                {
                    "code": "600519",
                    "as_of": f"{tomorrow_year}-01-01",
                    "revenue_records": [],
                    "goodwill_records": [],
                }
            ]
        )


def test_batch_time_budget_backfills_unavailable_records_without_key_error(monkeypatch, tmp_path):
    import time

    def fake_cashflow(years, *, codes):
        return pd.DataFrame([_acquisition_row(year, code=code) for code in codes for year in range(2021, 2026)])

    def slow_segment(code, cutoff, **_kwargs):
        time.sleep(0.5)
        payload = ge._validate_business_payload(
            _segment_payload(code=code),
            code=code,
            as_of=cutoff,
        )
        return ge._build_segment_growth_sources(code, cutoff, payload), False, "disabled"

    monkeypatch.setattr(ge, "fetch_detailed_annual_cashflow_history", fake_cashflow)
    monkeypatch.setattr(ge, "_fetch_segment_growth_sources", slow_segment)
    revenues, goodwill, _ = _complete_inputs()
    request = {
        "code": "600519",
        "as_of": "2026-07-17",
        "revenue_records": revenues,
        "goodwill_records": goodwill,
    }
    # The tiny budget forces the deadline to fire before the worker finishes;
    # the batch must still return a complete result with an explicit
    # time-budget record instead of raising KeyError.
    result = ge.fetch_growth_evidence_batch(
        [request],
        max_workers=1,
        cache_dir=tmp_path,
        time_budget_seconds=0.1,
    )
    assert list(result) == ["600519"]
    record = result["600519"]
    assert record["available"] is False
    assert record["segment_growth_sources"]["reason"] == "time_budget_exceeded"
