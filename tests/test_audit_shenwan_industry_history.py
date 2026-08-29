from __future__ import annotations

from data.shenwan_industry_history import (
    SHENWAN_XLS_URL,
    ShenwanIndustryRecord,
    ShenwanIndustryResolution,
)
from tools.audit_shenwan_industry_history import build_audit_report


def _record(code: str, effective: str, industry: str) -> ShenwanIndustryRecord:
    return ShenwanIndustryRecord(
        code=code,
        effective_from=effective,
        industry_code=industry,
        l1_code=industry[:2] + "0000",
        l2_code=industry[:4] + "00",
        classification_standard="shenwan_official_workbook",
        source_name="test",
        source_url=SHENWAN_XLS_URL,
        source_sha256="a" * 64,
    )


def test_history_audit_report_is_point_in_time_and_explicitly_non_overriding():
    resolution = ShenwanIndustryResolution(
        records=(
            _record("000001", "2014-02-21", "480101"),
            _record("000001", "2021-07-30", "480301"),
        ),
        requested_codes=("000001", "000002"),
        primary_source_available=False,
        fallback_codes=("000001", "000002"),
        unresolved_codes=("000002",),
        source_errors={"申万研究行业分类历史公开表": "ShenwanIndustryHistoryError"},
    )

    report = build_audit_report(
        resolution,
        from_as_of="2016-01-01",
        to_as_of="2026-08-28",
    )

    assert report["purpose"] == "point_in_time_peer_audit_only_no_model_taxonomy_override"
    assert report["resolved_count"] == 1
    assert report["status_counts"] == {"changed": 1, "missing_from": 1}
    assert report["rows"][0]["to_industry_code"] == "480301"
