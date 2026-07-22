from __future__ import annotations

import io
import json
import sys

import pandas as pd
import pytest

from data.cache import SafeFileCache
from tools import audit_snapshot_history


DATASETS = audit_snapshot_history.ANNUAL_DATASETS


def _rows(years, field="VALUE"):
    return [{"REPORT_DATE": f"{year}-12-31", field: 1.0} for year in years]


def _company(years_by_dataset):
    return {dataset: _rows(years_by_dataset.get(dataset, ())) for dataset in DATASETS}


def _payload():
    complete = tuple(range(2021, 2026))
    moutai = _company(
        {
            "revenue_history": complete,
            "income_history": (2021, 2022, 2024, 2025),
            "cashflow": (2023, 2024, 2025),
            "balance": (2016, 2017, 2018, 2019, 2020),
            "indicators": (),
        }
    )
    moutai["revenue_history"].extend(
        [
            {"REPORT_DATE": "2025-12-31", "VALUE": 2.0},
            {"REPORT_DATE": "2025-09-30", "VALUE": 3.0},
            {"REPORT_DATE": "not-a-date", "VALUE": 4.0},
        ]
    )
    recent = _company({dataset: (2023, 2024, 2025) for dataset in DATASETS})
    bj = _company({dataset: complete for dataset in DATASETS})
    return {
        "quotes": pd.DataFrame(
            [
                {"code": "600519", "name": "贵州茅台"},
                {"code": "001234", "name": "新上市", "listing_date": "2023-06-01"},
                {"code": "920001", "name": "北交所", "listing_date": "2020-01-01"},
            ]
        ),
        "financials": {"600519": moutai, "001234": recent, "920001": bj},
        "validation": {"reporting_period_contract": {"annual_report_date": "2025-12-31"}},
    }


def test_audit_reports_histograms_holes_latest_coverage_and_exact_company_years():
    report = audit_snapshot_history.audit_history_payload(
        _payload(),
        window_years=5,
        requested_companies=("600519",),
    )

    assert report["scope"]["company_count"] == 2
    assert report["scope"]["excluded_non_sh_sz_financial_entries"] == 1
    assert report["scope"]["requested_window"] == {
        "latest_year": 2025,
        "latest_year_basis": "validation.reporting_period_contract.annual_report_date",
        "window_years": 5,
        "years": [2021, 2022, 2023, 2024, 2025],
    }

    revenue = report["datasets"]["revenue_history"]
    assert revenue["period_count_distribution"] == {"3": 1, "5": 1}
    assert revenue["latest_year_coverage"] == {"count": 2, "ratio": 1.0}
    assert revenue["complete_requested_window_coverage"] == {"count": 1, "ratio": 0.5}
    assert revenue["status_distribution"] == {
        "complete_listing_age_adjusted_window": 1,
        "complete_requested_window": 1,
    }
    assert revenue["record_quality"] == {
        "invalid_report_date_records": 1,
        "non_annual_records_ignored": 1,
        "duplicate_annual_records": 1,
        "malformed_dataset_company_values": 0,
    }

    income = report["datasets"]["income_history"]
    assert income["internal_hole_years"] == {"2023": 1}
    assert income["status_distribution"]["internal_gaps"] == 1

    company = report["requested_companies"]["600519"]
    assert company["listing_date"] is None
    assert company["listing_date_evidence"] == "unavailable"
    assert company["datasets"]["revenue_history"]["years"] == [2021, 2022, 2023, 2024, 2025]
    assert company["datasets"]["revenue_history"]["status"] == "complete_requested_window"
    assert company["datasets"]["income_history"]["internal_holes"] == [2023]
    assert company["datasets"]["income_history"]["status"] == "internal_gaps"
    assert company["datasets"]["cashflow"]["status"] == "short_observed_history_unknown_cause"
    assert company["datasets"]["cashflow"]["years"] == [2023, 2024, 2025]
    assert company["datasets"]["balance"]["status"] == "observed_window_not_aligned_unknown_cause"
    assert company["datasets"]["indicators"]["status"] == "no_observed_annual_history"


def test_listing_date_narrows_expectation_without_converting_short_history_to_data_loss_claim():
    report = audit_snapshot_history.audit_history_payload(
        _payload(),
        window_years=5,
        requested_companies=("001234", "600519"),
    )

    recent = report["requested_companies"]["001234"]
    assert recent["listing_date"] == "2023-06-01"
    assert recent["listing_date_evidence"] == "snapshot_quotes"
    assert recent["datasets"]["cashflow"]["expected_years"] == [2023, 2024, 2025]
    assert recent["datasets"]["cashflow"]["status"] == "complete_listing_age_adjusted_window"

    unknown = report["requested_companies"]["600519"]["datasets"]["cashflow"]
    assert unknown["expectation_basis"] == "requested_window_no_listing_date_evidence"
    assert unknown["status"] == "short_observed_history_unknown_cause"
    statuses = {
        item["status"] for company in report["requested_companies"].values() for item in company["datasets"].values()
    }
    assert not any("data_loss" in status for status in statuses)


@pytest.mark.parametrize("stdout_encoding", ["cp1252", "gbk"])
def test_safe_cache_cli_is_compact_json_and_does_not_modify_snapshot(tmp_path, monkeypatch, stdout_encoding):
    path = tmp_path / "历史😀" / "snapshot.json.gz"
    path.parent.mkdir()
    SafeFileCache(path, schema_version=7).save(_payload())
    before = path.read_bytes()
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding=stdout_encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = audit_snapshot_history.main(
        ["--snapshot", str(path), "--schema", "7", "--window-years", "5", "--company", "600519"]
    )

    assert exit_code == 0
    assert path.read_bytes() == before
    stdout.flush()
    output = stdout_bytes.getvalue().decode(stdout_encoding)
    assert output.endswith("\n")
    assert ": " not in output
    decoded = json.loads(output)
    assert decoded["ok"] is True
    assert decoded["cache"]["schema_version"] == 7
    assert decoded["cache"]["path"] == str(path.resolve())
    assert decoded["requested_companies"]["600519"]["datasets"]["income_history"]["years"] == [
        2021,
        2022,
        2024,
        2025,
    ]


def test_schema_mismatch_returns_compact_json_error_without_traceback(tmp_path, capsys):
    path = tmp_path / "snapshot.json.gz"
    SafeFileCache(path, schema_version=7).save(_payload())
    before = path.read_bytes()

    exit_code = audit_snapshot_history.main(["--snapshot", str(path), "--schema", "8"])

    assert exit_code == 2
    assert path.read_bytes() == before
    output = capsys.readouterr().out
    decoded = json.loads(output)
    assert decoded["ok"] is False
    assert decoded["error"]["type"] == "HistoryAuditError"
    assert "schema_version_mismatch" in decoded["error"]["message"]
    assert "Traceback" not in output


@pytest.mark.parametrize("stdout_encoding", ["cp1252", "gbk"])
def test_invalid_company_error_is_ascii_safe_json(tmp_path, monkeypatch, stdout_encoding):
    path = tmp_path / "snapshot.json.gz"
    SafeFileCache(path, schema_version=7).save(_payload())
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding=stdout_encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = audit_snapshot_history.main(["--snapshot", str(path), "--schema", "7", "--company", "😀"])

    stdout.flush()
    decoded = json.loads(stdout_bytes.getvalue().decode(stdout_encoding))
    assert exit_code == 2
    assert decoded["ok"] is False
    assert "😀" in decoded["error"]["message"]


def test_latest_year_falls_back_to_observed_data_and_missing_requested_company_is_explicit():
    payload = _payload()
    payload.pop("validation")
    report = audit_snapshot_history.audit_history_payload(
        payload,
        window_years=3,
        requested_companies=("300999",),
    )

    assert report["scope"]["requested_window"]["latest_year"] == 2025
    assert report["scope"]["requested_window"]["latest_year_basis"] == "maximum_observed_annual_year"
    missing = report["requested_companies"]["300999"]
    assert missing["present"] is False
    assert missing["datasets"]["revenue_history"]["years"] == []
    assert missing["datasets"]["revenue_history"]["status"] == "no_observed_annual_history"
