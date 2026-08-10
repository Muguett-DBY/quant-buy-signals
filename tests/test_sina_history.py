"""Tests for the Sina annual-history overlay (Type 1/3/7 trend evidence)."""

from __future__ import annotations

import hashlib

from data.sina_financial import (
    SINA_FINANCIAL_NUM_PERIODS,
    SINA_FINANCIAL_URL,
    SinaStatementResult,
    backfill_history_gaps,
    _annual_records,
    _history_gap_plan,
    _overlay_history_fields,
)
from data.capex_evidence import validate_capex_provenance


_FAKE_SHA256 = hashlib.sha256(b"test-history-payload").hexdigest()


CONTRACT = {
    "annual_report_date": "2025-12-31",
    "current_interim_report_date": "2026-03-31",
    "prior_interim_report_date": "2025-03-31",
    "period_basis": "FY + current YTD - prior-year comparable YTD",
    "cache_key": "test-history",
}


def _record(statement: str, report_date: str, fields: dict[str, float]) -> dict:
    record: dict = {"REPORT_DATE": report_date, **fields}
    record["SOURCE_PROVENANCE"] = {
        "adapter_version": "test",
        "source_id": "sina_company_finance_2022",
        "source_url": "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        "source_query": {
            "paperCode": "sz002731",
            "source": statement,
            "type": "0",
            "page": "1",
            "num": str(SINA_FINANCIAL_NUM_PERIODS),
        },
        "source_raw_sha256": _FAKE_SHA256,
        "security_code": "002731",
        "report_date": report_date,
        "statement": statement,
        "field_sources": {
            field: {"source_field": field, "source_title": field, "source_value": str(value)}
            for field, value in fields.items()
        },
    }
    if "CONSTRUCT_LONG_ASSET" in fields and fields["CONSTRUCT_LONG_ASSET"] > 0:
        record["CAPEX_PROVENANCE"] = {
            "schema_version": 1,
            "status": "complete",
            "evidence_label": "fact_secondary_source_reported",
            "value": fields["CONSTRUCT_LONG_ASSET"],
            "security_code": "002731",
            "source_report": "SINA_COMPANY_FINANCE_2022_LLB",
            "source_field": "ACQUASSETCASH",
            "canonical_field": "CONSTRUCT_LONG_ASSET",
            "report_date": report_date,
            "formula": "source_reported",
            "derivation_method": None,
            "components": {"reported_value": fields["CONSTRUCT_LONG_ASSET"]},
            "source_null_fields": [],
            "source_url": SINA_FINANCIAL_URL,
            "source_raw_sha256": _FAKE_SHA256,
            "source_query": {
                "paperCode": "sz002731",
                "source": "llb",
                "type": "0",
                "page": "1",
                "num": str(SINA_FINANCIAL_NUM_PERIODS),
            },
            "request_num": SINA_FINANCIAL_NUM_PERIODS,
            "source_metadata": {
                "report_type": "合并期末",
                "currency": "CNY",
                "data_source": "定期报告",
                "is_audit": "未审计",
                "audit_opinion": "",
                "publish_date": "20260425",
                "update_time": 1777029605,
            },
            "security_code_sha256": _FAKE_SHA256,
        }
    return record


def _result(code: str, statement: str, records: list[dict]) -> SinaStatementResult:
    return SinaStatementResult(code, statement, "ok", tuple(records), raw_sha256=_FAKE_SHA256)


class StubClient:
    def __init__(self, results: dict):
        self.results = results
        self.requests = []

    def fetch_many(self, requests_, *, contract, force_refresh=False):
        self.requests.extend(requests_)
        return {identity: self.results.get(identity) for identity in requests_}

    def diagnostic(self):
        return {}


def _lrb_records() -> list[dict]:
    return [
        _record(
            "lrb", "2022-12-31", {"TOTAL_OPERATE_INCOME": 4.0e9, "PARENT_NETPROFIT": 3.0e8, "OPERATE_PROFIT": 4.0e8}
        ),
        _record(
            "lrb", "2023-12-31", {"TOTAL_OPERATE_INCOME": 4.5e9, "PARENT_NETPROFIT": 3.5e8, "OPERATE_PROFIT": 4.6e8}
        ),
        _record(
            "lrb", "2024-12-31", {"TOTAL_OPERATE_INCOME": 5.0e9, "PARENT_NETPROFIT": 4.0e8, "OPERATE_PROFIT": 5.2e8}
        ),
        _record("lrb", "2025-06-30", {"TOTAL_OPERATE_INCOME": 2.6e9, "PARENT_NETPROFIT": 2.2e8}),
    ]


def _llb_records() -> list[dict]:
    return [
        _record("llb", "2022-12-31", {"NETCASH_OPERATE": 6.0e8, "CONSTRUCT_LONG_ASSET": 1.0e8}),
        _record("llb", "2023-12-31", {"NETCASH_OPERATE": 6.5e8, "CONSTRUCT_LONG_ASSET": 1.2e8}),
        _record("llb", "2024-12-31", {"NETCASH_OPERATE": 7.0e8, "CONSTRUCT_LONG_ASSET": 1.4e8}),
    ]


def _fzb_records() -> list[dict]:
    return [
        _record(
            "fzb",
            "2022-12-31",
            {
                "TOTAL_ASSETS": 8.0e9,
                "TOTAL_LIABILITIES": 4.0e9,
                "TOTAL_EQUITY": 4.0e9,
                "TOTAL_PARENT_EQUITY": 3.8e9,
                "MINORITY_EQUITY": 2.0e8,
            },
        ),
        _record(
            "fzb",
            "2023-12-31",
            {
                "TOTAL_ASSETS": 8.5e9,
                "TOTAL_LIABILITIES": 4.2e9,
                "TOTAL_EQUITY": 4.3e9,
                "TOTAL_PARENT_EQUITY": 4.1e9,
                "MINORITY_EQUITY": 2.0e8,
            },
        ),
        _record(
            "fzb",
            "2024-12-31",
            {
                "TOTAL_ASSETS": 9.0e9,
                "TOTAL_LIABILITIES": 4.4e9,
                "TOTAL_EQUITY": 4.6e9,
                "TOTAL_PARENT_EQUITY": 4.4e9,
                "MINORITY_EQUITY": 2.0e8,
            },
        ),
    ]


def test_num_periods_is_forty_for_ten_year_trends():
    assert SINA_FINANCIAL_NUM_PERIODS == 40


def test_annual_records_filters_quarter_endings():
    records = _lrb_records()
    annual = _annual_records(records)
    assert [record["REPORT_DATE"] for record in annual] == ["2022-12-31", "2023-12-31", "2024-12-31"]


def test_history_gap_plan_selects_short_series_only():
    financials = {
        "600000": {
            "revenue_history": [
                {"REPORT_DATE": f"{year}-12-31", "TOTAL_OPERATE_INCOME": 1.0} for year in range(2021, 2025)
            ],
            "cashflow": [{"REPORT_DATE": f"{year}-12-31"} for year in range(2021, 2025)],
            "balance": [{"REPORT_DATE": f"{year}-12-31"} for year in range(2021, 2025)],
        },
        "000001": {
            "revenue_history": [{"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 1.0}],
            "cashflow": [],
            "balance": [],
        },
    }
    assert _history_gap_plan(financials, ["600000", "000001"]) == ["000001"]
    assert _history_gap_plan(financials, ["600000"]) == []


def test_backfill_history_overlays_annual_series_only_and_preserves_primary(tmp_path):
    financials = {
        "002731": {
            "revenue_history": [{"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 4.8e9}],
            "income_history": [],
            "cashflow": [],
            "balance": [],
        }
    }
    client = StubClient(
        {
            ("002731", "lrb"): _result("002731", "lrb", _lrb_records()),
            ("002731", "llb"): _result("002731", "llb", _llb_records()),
            ("002731", "fzb"): _result("002731", "fzb", _fzb_records()),
        }
    )
    outcome = backfill_history_gaps(financials, CONTRACT, codes=["002731"], client=client)
    company = outcome.financials["002731"]

    annual_revenue = {
        record["REPORT_DATE"]: record["TOTAL_OPERATE_INCOME"]
        for record in company["revenue_history"]
        if str(record["REPORT_DATE"]).endswith("-12-31")
    }
    assert annual_revenue == {"2022-12-31": 4.0e9, "2023-12-31": 4.5e9, "2024-12-31": 4.8e9}
    # 季度期不得写入
    assert all(str(record["REPORT_DATE"]).endswith("-12-31") for record in company["revenue_history"])

    income = {record["REPORT_DATE"]: record for record in company["income_history"]}
    assert income["2023-12-31"]["PARENT_NETPROFIT"] == 3.5e8
    assert income["2023-12-31"]["OPERATE_PROFIT"] == 4.6e8
    assert income["2023-12-31"].get("PARENT_NETPROFIT_PROVENANCE") is not None

    cashflow = {record["REPORT_DATE"]: record for record in company["cashflow"]}
    assert cashflow["2024-12-31"]["NETCASH_OPERATE"] == 7.0e8
    assert cashflow["2024-12-31"]["CONSTRUCT_LONG_ASSET"] == 1.4e8
    assert (
        validate_capex_provenance(
            cashflow["2024-12-31"].get("CAPEX_PROVENANCE"),
            expected_value=1.4e8,
            expected_report_date="2024-12-31",
            expected_security_code="002731",
        )
        == "complete"
    )

    balance = {record["REPORT_DATE"]: record for record in company["balance"]}
    assert balance["2024-12-31"]["TOTAL_ASSETS"] == 9.0e9
    assert balance["2024-12-31"]["TOTAL_PARENT_EQUITY"] == 4.4e9
    assert balance["2024-12-31"]["PARENT_EQUITY"] == 4.4e9
    assert balance["2024-12-31"]["MINORITY_EQUITY"] == 2.0e8

    diagnostic = outcome.diagnostic
    assert diagnostic["candidate_codes"] == 1
    assert diagnostic["filled_fields"] > 0
    # 2024 revenue disagrees between primary (4.8e9) and secondary (5.0e9):
    # primary wins, the disagreement is counted as one conflict.
    assert diagnostic["conflicts"] == 1
    assert diagnostic["unverified_zero"] == 0


def test_overlay_counts_conflict_when_secondary_disagrees(tmp_path):
    from collections import Counter

    financials: dict = {"002731": {}}
    existing = {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 9.0e9}
    financials["002731"]["revenue_history"] = [existing]
    counters: Counter[str] = Counter()
    conflicts: set[str] = set()
    filled: set[str] = set()
    _overlay_history_fields(
        financials,
        "002731",
        "revenue_history",
        "2024-12-31",
        _record("lrb", "2024-12-31", {"TOTAL_OPERATE_INCOME": 5.0e9}),
        ("TOTAL_OPERATE_INCOME",),
        counters,
        conflicts,
        filled,
    )
    assert counters["conflicts"] == 1
    assert existing["TOTAL_OPERATE_INCOME"] == 9.0e9  # primary preserved


def test_overlay_never_writes_zero_revenue_or_capex(tmp_path):
    from collections import Counter

    financials: dict = {"002731": {}}
    counters: Counter[str] = Counter()
    conflicts: set[str] = set()
    filled: set[str] = set()
    _overlay_history_fields(
        financials,
        "002731",
        "revenue_history",
        "2023-12-31",
        _record("lrb", "2023-12-31", {"TOTAL_OPERATE_INCOME": 0.0}),
        ("TOTAL_OPERATE_INCOME",),
        counters,
        conflicts,
        filled,
    )
    _overlay_history_fields(
        financials,
        "002731",
        "cashflow",
        "2023-12-31",
        _record("llb", "2023-12-31", {"CONSTRUCT_LONG_ASSET": 0.0}),
        ("CONSTRUCT_LONG_ASSET",),
        counters,
        conflicts,
        filled,
    )
    assert counters["unverified_zero"] == 2
    assert financials["002731"].get("revenue_history") is None
    assert financials["002731"].get("cashflow") is None


def test_backfill_history_respects_code_budget(tmp_path):
    financials = {
        "000001": {"revenue_history": [], "cashflow": [], "balance": []},
        "000002": {"revenue_history": [], "cashflow": [], "balance": []},
        "000003": {"revenue_history": [], "cashflow": [], "balance": []},
    }
    client = StubClient({})
    outcome = backfill_history_gaps(
        financials, CONTRACT, codes=["000001", "000002", "000003"], client=client, max_target_codes=2
    )
    diagnostic = outcome.diagnostic
    assert diagnostic["target_codes"] == 2
    assert diagnostic["skipped_codes"] == 1
    assert diagnostic["budget_exhausted"] is True
    assert len(client.requests) == 6  # 2 codes x 3 statements


def test_backfill_history_skips_missing_codes_and_non_ok_results(tmp_path):
    financials = {"002731": {"revenue_history": [], "cashflow": [], "balance": []}}
    client = StubClient(
        {
            ("002731", "lrb"): _result("002731", "lrb", _lrb_records()),
            ("002731", "llb"): SinaStatementResult("002731", "llb", "source_unavailable", error="boom"),
            ("002731", "fzb"): _result("002731", "fzb", _fzb_records()),
        }
    )
    outcome = backfill_history_gaps(financials, CONTRACT, codes=["002731"], client=client)
    company = outcome.financials["002731"]
    assert len(company.get("revenue_history", [])) == 3
    assert company.get("cashflow") == []  # failed statement contributes nothing
    assert len(company.get("balance", [])) == 3
    assert outcome.diagnostic["status_counts"] == {"ok": 2, "source_unavailable": 1}
