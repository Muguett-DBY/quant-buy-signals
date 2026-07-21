from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import BoundedSemaphore, Event, Lock

import pandas as pd
import pytest
import requests

import data.datacenter as dc


def _indicator_row(year: int, *, code: str = "000001") -> dict:
    row = {field: None for field in dc.MAIN_FINANCIAL_INDICATOR_METRICS}
    row.update(
        {
            "SECURITY_CODE": code,
            "SECUCODE": f"{code}.{'BJ' if code.startswith(('8', '9')) else 'SZ'}",
            "SECURITY_NAME_ABBR": f"样本{code}",
            "SECURITY_TYPE_CODE": "058001001",
            "REPORT_DATE": f"{year}-12-31 00:00:00",
            "REPORT_TYPE": "年报",
            "REPORT_DATE_NAME": f"{year}年报",
            "REPORT_YEAR": str(year),
            "NOTICE_DATE": f"{year + 1}-04-30 00:00:00",
            "RDEXPEND": 10.0,
            "ROIC": 11.0,
            "ROEJQ": 12.0,
            "XSMLL": 13.0,
            "XSJLL": 14.0,
            "TAXRATE": 15.0,
            "TOTAL_SHARE": 1_000_000,
            "STAFF_NUM": 100,
            "KCFJCXSYJLR": 16.0,
            "INTEREST_DEBT_RATIO": 17.0,
        }
    )
    return row


def _detailed_cashflow_row(report_date: str, *, code: str = "000001") -> dict:
    period_label = {"03-31": "一季报", "06-30": "中报", "09-30": "三季报"}[report_date[5:]]
    row = {
        "SECURITY_CODE": code,
        "SECUCODE": f"{code}.{'SH' if code.startswith('6') else 'SZ'}",
        "SECURITY_NAME_ABBR": f"样本{code}",
        "SECURITY_TYPE_CODE": "058001001",
        "REPORT_DATE": f"{report_date} 00:00:00",
        "REPORT_TYPE": period_label,
        "REPORT_DATE_NAME": f"{report_date[:4]}{period_label}",
        "NOTICE_DATE": f"{report_date[:4]}-04-30 00:00:00",
        "UPDATE_DATE": f"{report_date[:4]}-04-30 00:00:00",
        "CURRENCY": "CNY",
    }
    row.update({field: None for field in dc._DETAILED_INVESTMENT_FIELDS})
    row.update(
        {
            "TOTAL_INVEST_INFLOW": 9_500.0,
            "INVEST_NETCASH_BALANCE": 0.0,
            "NETCASH_INVEST": 9_500.0,
        }
    )
    return row


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = {} if headers is None else headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeStreamingResponse:
    def __init__(self, chunks, *, headers=None, status=200):
        self._chunks = chunks
        self.headers = {} if headers is None else headers
        self.status_code = status
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, *, chunk_size):
        assert chunk_size == dc._RESPONSE_CHUNK_BYTES
        yield from self._chunks

    def close(self):
        self.closed = True


def page_payload(page, pages, rows, *, count=None):
    return {
        "success": True,
        "result": {"pages": pages, "count": pages if count is None else count, "data": rows},
    }


def test_fetch_all_pages_requests_page_one_once_and_returns_page_order(monkeypatch):
    calls = []
    lock = Lock()
    payloads = {
        1: page_payload(1, 3, [{"SECURITY_CODE": "000001", "REPORT_DATE": "2025-12-31"}]),
        2: page_payload(2, 3, [{"SECURITY_CODE": "000002", "REPORT_DATE": "2025-12-31"}]),
        3: page_payload(3, 3, [{"SECURITY_CODE": "000003", "REPORT_DATE": "2025-12-31"}]),
    }

    def fake_get(_url, *, params, **_kwargs):
        with lock:
            calls.append(params["pageNumber"])
        return FakeResponse(payloads[params["pageNumber"]])

    monkeypatch.setattr(dc.requests, "get", fake_get)
    frame = dc._fetch_all_pages(
        dc.RPT_INCOME,
        "SECURITY_CODE,REPORT_DATE",
        "(REPORT_DATE='2025-12-31')",
        page_size=1,
        max_workers=2,
    )
    assert calls.count(1) == 1
    assert frame["SECURITY_CODE"].tolist() == ["000001", "000002", "000003"]


@pytest.mark.parametrize("requested_field", ["GOODWILL", "OBTAIN_SUBSIDIARY_OTHER"])
def test_fetch_all_pages_rejects_an_omitted_requested_financial_column(
    monkeypatch,
    requested_field,
):
    monkeypatch.setattr(
        dc,
        "_request_page",
        lambda *_args, **_kwargs: dc._PageResult(
            1,
            1,
            [{"SECURITY_CODE": "000001", "REPORT_DATE": "2025-12-31"}],
            1,
        ),
    )

    with pytest.raises(dc.DataFetchError, match=rf"omitted requested columns.*{requested_field}"):
        dc._fetch_all_pages(
            dc.RPT_DETAILED_CASHFLOW,
            f"SECURITY_CODE,REPORT_DATE,{requested_field}",
            "(REPORT_DATE='2025-12-31')",
        )


@pytest.mark.parametrize(
    ("requested_field", "response_field"),
    [
        ("GOODWILL", "GOODWILL"),
        ("OBTAIN_SUBSIDIARY_OTHER", "OBTAIN_SUBSIDIARY_OTHER"),
        ("BOND_PAYABLE", "BONDS_PAYABLE"),
    ],
)
def test_fetch_all_pages_accepts_requested_nulls_and_legal_aliases(
    monkeypatch,
    requested_field,
    response_field,
):
    monkeypatch.setattr(
        dc,
        "_request_page",
        lambda *_args, **_kwargs: dc._PageResult(
            1,
            1,
            [
                {
                    "SECURITY_CODE": "000001",
                    "REPORT_DATE": "2025-12-31",
                    response_field: None,
                }
            ],
            1,
        ),
    )

    frame = dc._fetch_all_pages(
        dc.RPT_BALANCE,
        f"SECURITY_CODE,REPORT_DATE,{requested_field}",
        "(REPORT_DATE='2025-12-31')",
    )

    assert response_field in frame.columns
    assert pd.isna(frame.loc[0, response_field])


@pytest.mark.parametrize("failure", ["missing_metadata", "empty_page", "changed_pages"])
def test_fetch_all_pages_rejects_incomplete_snapshots(monkeypatch, failure):
    monkeypatch.setattr(dc.time, "sleep", lambda _seconds: None)

    def fake_get(_url, *, params, **_kwargs):
        page = params["pageNumber"]
        if failure == "missing_metadata" and page == 1:
            return FakeResponse({"success": True, "result": {"data": [{"SECURITY_CODE": "1"}]}})
        if page == 1:
            return FakeResponse(page_payload(1, 2, [{"SECURITY_CODE": "1"}]))
        if failure == "empty_page":
            return FakeResponse(page_payload(2, 2, []))
        return FakeResponse(page_payload(2, 3, [{"SECURITY_CODE": "2"}]))

    monkeypatch.setattr(dc.requests, "get", fake_get)
    with pytest.raises(dc.DataFetchError):
        dc._fetch_all_pages(dc.RPT_INCOME, "SECURITY_CODE", page_size=1)


def test_fetch_all_pages_retries_http_failure_then_raises(monkeypatch):
    monkeypatch.setattr(dc.time, "sleep", lambda _seconds: None)
    calls = []

    def fake_get(_url, *, params, **_kwargs):
        calls.append(params["pageNumber"])
        if params["pageNumber"] == 1:
            return FakeResponse(page_payload(1, 2, [{"SECURITY_CODE": "1"}]))
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(dc.requests, "get", fake_get)
    with pytest.raises(dc.DataFetchError, match="page 2"):
        dc._fetch_all_pages(dc.RPT_INCOME, "SECURITY_CODE", page_size=1)
    assert calls.count(2) == 3


def test_request_page_rejects_declared_oversized_response_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "_MAX_DATACENTER_RESPONSE_BYTES", 64)

    def fake_get(_url, *, params, **_kwargs):
        calls.append(params["pageNumber"])
        return FakeResponse(
            page_payload(1, 1, [{"SECURITY_CODE": "1"}]),
            headers={"Content-Length": "65"},
        )

    monkeypatch.setattr(dc.requests, "get", fake_get)
    with pytest.raises(dc.DataFetchError, match="response exceeds byte limit"):
        dc._request_page(dc.RPT_INCOME, "SECURITY_CODE", 1, page_size=1)
    assert calls == [1]


def test_request_page_rejects_chunked_oversized_response_and_closes_it(monkeypatch):
    monkeypatch.setattr(dc, "_MAX_DATACENTER_RESPONSE_BYTES", 64)
    response = FakeStreamingResponse([b"x" * 40, b"y" * 25])
    monkeypatch.setattr(dc.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(dc.DataFetchError, match="response exceeds byte limit"):
        dc._request_page(dc.RPT_INCOME, "SECURITY_CODE", 1, page_size=1)

    assert response.closed is True


def test_request_page_process_gate_caps_nested_batch_concurrency(monkeypatch):
    lock = Lock()
    two_requests_active = Event()
    active = 0
    peak = 0

    def fake_get(_url, *, params, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_requests_active.set()
        assert two_requests_active.wait(timeout=5)
        with lock:
            active -= 1
        page = params["pageNumber"]
        return FakeResponse(page_payload(page, 8, [{"SECURITY_CODE": f"{page:06d}"}], count=8))

    monkeypatch.setattr(dc, "_DATACENTER_REQUEST_SLOTS", BoundedSemaphore(2))
    monkeypatch.setattr(dc.requests, "get", fake_get)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda page: dc._request_page(dc.RPT_INCOME, "SECURITY_CODE", page, page_size=1),
                range(1, 9),
            )
        )

    assert [result.page for result in results] == list(range(1, 9))
    assert peak == 2


@pytest.mark.parametrize(
    ("limit_name", "limit", "payload", "page_size", "message"),
    [
        (
            "_MAX_DATACENTER_PAGES",
            2,
            page_payload(1, 3, [{"SECURITY_CODE": "1"}], count=3),
            1,
            "page count exceeds limit",
        ),
        (
            "_MAX_DATACENTER_ROWS",
            2,
            page_payload(1, 2, [{"SECURITY_CODE": "1"}, {"SECURITY_CODE": "2"}], count=3),
            2,
            "row count exceeds limit",
        ),
    ],
)
def test_request_page_rejects_unbounded_pagination_metadata(
    monkeypatch,
    limit_name,
    limit,
    payload,
    page_size,
    message,
):
    calls = []
    monkeypatch.setattr(dc, limit_name, limit)

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(payload)

    monkeypatch.setattr(dc.requests, "get", fake_get)
    with pytest.raises(dc.DataFetchError, match=message):
        dc._request_page(dc.RPT_INCOME, "SECURITY_CODE", 1, page_size=page_size)
    assert calls == [1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pages", True),
        ("pages", 1.0),
        ("pages", "1.0"),
        ("count", False),
        ("count", 1.0),
        ("count", "1e0"),
    ],
)
def test_request_page_rejects_coercible_noninteger_pagination_metadata(monkeypatch, field, value):
    payload = page_payload(1, 1, [{"SECURITY_CODE": "1"}], count=1)
    payload["result"][field] = value
    monkeypatch.setattr(dc.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(dc.requests, "get", lambda *_args, **_kwargs: FakeResponse(payload))

    with pytest.raises(dc.DataFetchError, match=f"invalid .*{field.removesuffix('s').replace('count', 'row count')}"):
        dc._request_page(dc.RPT_INCOME, "SECURITY_CODE", 1, page_size=1)


def test_filtered_report_date_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(
        dc,
        "_request_page",
        lambda *_args, **_kwargs: dc._PageResult(1, 1, [{"SECURITY_CODE": "000001", "REPORT_DATE": "2025-06-30"}], 1),
    )
    with pytest.raises(dc.DataFetchError, match="report date mismatch"):
        dc._fetch_all_pages(
            dc.RPT_INCOME,
            "SECURITY_CODE,REPORT_DATE",
            "(REPORT_DATE='2025-12-31')",
        )


def test_history_concat_is_chronological_even_if_years_are_descending(monkeypatch):
    def fake_all(_report, _columns, extra_filter, **_kwargs):
        year = extra_filter.split("'")[1][:4]
        return pd.DataFrame([{"SECURITY_CODE": "000001", "REPORT_DATE": f"{year}-12-31", "X": int(year)}])

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)
    frame = dc.fetch_cashflow_history([2025, 2023, 2024])
    assert frame["REPORT_DATE"].tolist() == ["2023-12-31", "2024-12-31", "2025-12-31"]


def test_single_company_history_pushes_a_safe_code_filter_into_every_period(monkeypatch):
    calls = []

    def fake_all(report_name, columns, extra_filter, **_kwargs):
        calls.append((report_name, columns, extra_filter))
        year = extra_filter.split("'")[1][:4]
        return pd.DataFrame(
            [
                {
                    "SECURITY_CODE": "600519",
                    "SECURITY_NAME_ABBR": "贵州茅台",
                    "TOTAL_OPERATE_INCOME": float(year),
                    "OPERATE_PROFIT": 1.0,
                    "PARENT_NETPROFIT": 1.0,
                    "REPORT_DATE": f"{year}-12-31",
                }
            ]
        )

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = dc.fetch_income_history([2025, 2024], codes=["600519"])

    assert frame["REPORT_DATE"].tolist() == ["2024-12-31", "2025-12-31"]
    assert all('(SECURITY_CODE in ("600519"))' in extra_filter for _, _, extra_filter in calls)


def test_filtered_history_allows_a_legitimate_prelisting_period_without_a_row(monkeypatch):
    def fake_all(_report_name, _columns, extra_filter, **_kwargs):
        year = int(extra_filter.split("'")[1][:4])
        if year == 2015:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "SECURITY_CODE": "600519",
                    "REPORT_DATE": f"{year}-12-31",
                    "NETCASH_OPERATE": 1.0,
                    "CONSTRUCT_LONG_ASSET": None,
                }
            ]
        )

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = dc.fetch_cashflow_history([2016, 2015], codes=["600519"])

    assert frame["REPORT_DATE"].tolist() == ["2016-12-31"]


@pytest.mark.parametrize("codes", [["600519;DROP"], [True], "600519"])
def test_remote_financial_code_filter_rejects_unsafe_inputs(codes):
    with pytest.raises(ValueError, match="security codes"):
        dc._security_code_filter(codes)


def test_large_code_batch_keeps_full_market_period_completeness_checks():
    codes = tuple(f"{index:06d}" for index in range(dc._MAX_REMOTE_SECURITY_CODES + 1))

    with pytest.raises(dc.DataFetchError, match="required report queries returned no rows: 2024"):
        dc._combine_period_frames(
            [
                pd.DataFrame([{"SECURITY_CODE": "000001", "REPORT_DATE": "2025-12-31"}]),
                pd.DataFrame(),
            ],
            ["2025", "2024"],
            codes=codes,
            requested_columns="SECURITY_CODE,REPORT_DATE",
            remote_filtered=False,
        )


def test_main_financial_indicator_history_is_auditable_and_chronological(monkeypatch):
    calls = []

    def fake_all(report_name, columns, extra_filter, **_kwargs):
        year = int(extra_filter.split("'")[1][:4])
        calls.append((report_name, set(columns.split(",")), extra_filter))
        return pd.DataFrame([_indicator_row(year)])

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)
    frame = dc.fetch_main_financial_indicator_history([2025, 2023, 2024])

    assert [call[0] for call in calls] == [dc.RPT_MAIN_FINANCIAL_INDICATORS] * 3
    assert all(set(dc.MAIN_FINANCIAL_INDICATOR_METRICS) <= call[1] for call in calls)
    assert all('SECURITY_TYPE_CODE="058001001"' in call[2] for call in calls)
    assert frame["REPORT_DATE"].tolist() == ["2023-12-31", "2024-12-31", "2025-12-31"]
    assert frame["REPORT_DATE_NAME"].tolist() == ["2023年报", "2024年报", "2025年报"]
    assert frame["SOURCE_REPORT_NAME"].eq(dc.RPT_MAIN_FINANCIAL_INDICATORS).all()
    assert frame["NOTICE_DATE"].tolist() == ["2024-04-30", "2025-04-30", "2026-04-30"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.pop("TAXRATE"), "omitted columns"),
        (lambda row: row.update(REPORT_TYPE="一季报"), "non-annual REPORT_TYPE"),
        (lambda row: row.update(REPORT_YEAR="2024"), "REPORT_YEAR differs"),
        (lambda row: row.update(SECUCODE="600000.SH"), "invalid security identity"),
        (lambda row: row.update(SECURITY_NAME_ABBR=None), "invalid security identity"),
        (lambda row: row.update(ROIC=float("inf")), "non-finite or non-numeric"),
        (lambda row: row.update(ROIC=True), "non-finite or non-numeric"),
        (lambda row: row.update(TOTAL_SHARE=1.5), "fractional count"),
        (
            lambda row: row.update({field: None for field in dc.MAIN_FINANCIAL_INDICATOR_METRICS}),
            "without any requested metric",
        ),
    ],
)
def test_main_financial_indicator_history_rejects_unauditable_rows(monkeypatch, mutate, message):
    row = _indicator_row(2025)
    mutate(row)
    monkeypatch.setattr(dc, "_fetch_all_pages", lambda *_args, **_kwargs: pd.DataFrame([row]))

    with pytest.raises(dc.DataFetchError, match=message):
        dc.fetch_main_financial_indicator_history([2025])


def test_annual_financial_refresh_starts_all_four_datasets_in_one_bounded_batch(monkeypatch):
    started: set[str] = set()
    lock = Lock()
    all_started = Event()

    def fake_fetch(label):
        with lock:
            started.add(label)
            if len(started) == 4:
                all_started.set()
        assert all_started.wait(timeout=2)
        return pd.DataFrame([{"dataset": label}])

    monkeypatch.setattr(dc, "fetch_income_history", lambda: fake_fetch("income"))
    monkeypatch.setattr(dc, "fetch_cashflow_history", lambda: fake_fetch("cashflow"))
    monkeypatch.setattr(dc, "fetch_balance_history", lambda: fake_fetch("balance"))
    monkeypatch.setattr(dc, "fetch_main_financial_indicator_history", lambda: fake_fetch("indicators"))

    frames = dc.fetch_all_financials_parallel()

    assert started == {"income", "cashflow", "balance", "indicators"}
    assert [frame.loc[0, "dataset"] for frame in frames] == ["income", "cashflow", "balance", "indicators"]
    assert dc._DATACENTER_BATCH_WORKERS * dc._DATACENTER_WORKERS <= dc.CONCURRENCY


def test_filtered_annual_refresh_forwards_codes_to_all_four_datasets(monkeypatch):
    seen = []

    def fake_fetch(label):
        def fetch(*, codes):
            seen.append((label, tuple(codes)))
            return pd.DataFrame([{"dataset": label}])

        return fetch

    monkeypatch.setattr(dc, "fetch_income_history", fake_fetch("income"))
    monkeypatch.setattr(dc, "fetch_cashflow_history", fake_fetch("cashflow"))
    monkeypatch.setattr(dc, "fetch_balance_history", fake_fetch("balance"))
    monkeypatch.setattr(dc, "fetch_main_financial_indicator_history", fake_fetch("indicators"))

    frames = dc.fetch_all_financials_parallel(codes=["600519"])

    assert sorted(seen) == [
        ("balance", ("600519",)),
        ("cashflow", ("600519",)),
        ("income", ("600519",)),
        ("indicators", ("600519",)),
    ]
    assert [frame.loc[0, "dataset"] for frame in frames] == ["income", "cashflow", "balance", "indicators"]


def test_annual_financial_refresh_never_returns_a_partial_batch(monkeypatch):
    empty = pd.DataFrame()
    monkeypatch.setattr(dc, "fetch_income_history", lambda: empty.copy())
    monkeypatch.setattr(dc, "fetch_cashflow_history", lambda: empty.copy())
    monkeypatch.setattr(dc, "fetch_balance_history", lambda: empty.copy())

    def fail_indicators():
        raise dc.DataFetchError("indicator batch failed")

    monkeypatch.setattr(dc, "fetch_main_financial_indicator_history", fail_indicators)

    with pytest.raises(dc.DataFetchError, match="indicator batch failed"):
        dc.fetch_all_financials_parallel()


def test_annual_financial_refresh_retries_one_coherent_sequential_generation(monkeypatch):
    calls = {label: 0 for label in ("income", "cashflow", "balance", "indicators")}
    lock = Lock()
    all_started = Event()

    def fake_fetch(label):
        with lock:
            calls[label] += 1
            attempt = calls[label]
            if sum(calls.values()) == 4:
                all_started.set()
        if attempt == 1:
            assert all_started.wait(timeout=2)
        if label == "balance" and attempt == 1:
            raise dc.DataFetchError("transient TLS reset")
        return pd.DataFrame([{"dataset": label, "attempt": attempt}])

    monkeypatch.setattr(dc, "fetch_income_history", lambda: fake_fetch("income"))
    monkeypatch.setattr(dc, "fetch_cashflow_history", lambda: fake_fetch("cashflow"))
    monkeypatch.setattr(dc, "fetch_balance_history", lambda: fake_fetch("balance"))
    monkeypatch.setattr(dc, "fetch_main_financial_indicator_history", lambda: fake_fetch("indicators"))
    monkeypatch.setattr(dc.time, "sleep", lambda _seconds: None)

    frames = dc.fetch_all_financials_parallel()

    assert [frame.loc[0, "dataset"] for frame in frames] == ["income", "cashflow", "balance", "indicators"]
    assert all(frame.loc[0, "attempt"] >= 2 for frame in frames)


def test_fetch_all_pages_rejects_row_count_mismatch(monkeypatch):
    payloads = {
        1: page_payload(1, 2, [{"SECURITY_CODE": "1"}], count=3),
        2: page_payload(2, 2, [{"SECURITY_CODE": "2"}], count=3),
    }
    monkeypatch.setattr(
        dc.requests,
        "get",
        lambda _url, *, params, **_kwargs: FakeResponse(payloads[params["pageNumber"]]),
    )
    with pytest.raises(dc.DataFetchError, match="page row-count mismatch"):
        dc._fetch_all_pages(dc.RPT_INCOME, "SECURITY_CODE", page_size=2)


def test_fetch_all_pages_rejects_oversized_last_page(monkeypatch):
    monkeypatch.setattr(dc.time, "sleep", lambda _seconds: None)
    payloads = {
        1: page_payload(
            1,
            2,
            [{"SECURITY_CODE": "1"}, {"SECURITY_CODE": "2"}],
            count=3,
        ),
        2: page_payload(
            2,
            2,
            [{"SECURITY_CODE": "3"}, {"SECURITY_CODE": "4"}],
            count=3,
        ),
    }
    monkeypatch.setattr(
        dc.requests,
        "get",
        lambda _url, *, params, **_kwargs: FakeResponse(payloads[params["pageNumber"]]),
    )

    with pytest.raises(dc.DataFetchError, match="page row-count mismatch"):
        dc._fetch_all_pages(dc.RPT_INCOME, "SECURITY_CODE", page_size=2)


def test_fetch_all_pages_rejects_total_count_change_with_same_page_count(monkeypatch):
    payloads = {
        1: page_payload(
            1,
            2,
            [{"SECURITY_CODE": "1"}, {"SECURITY_CODE": "2"}],
            count=3,
        ),
        2: page_payload(
            2,
            2,
            [{"SECURITY_CODE": "3"}, {"SECURITY_CODE": "4"}],
            count=4,
        ),
    }
    monkeypatch.setattr(
        dc.requests,
        "get",
        lambda _url, *, params, **_kwargs: FakeResponse(payloads[params["pageNumber"]]),
    )

    with pytest.raises(dc.DataFetchError, match="row-count changed during fetch: 3 -> 4"):
        dc._fetch_all_pages(dc.RPT_INCOME, "SECURITY_CODE", page_size=2)


def test_dynamic_report_years_follow_completed_filing_windows():
    assert dc._latest_completed_annual_year(date(2026, 4, 30)) == 2024
    assert dc._latest_completed_annual_year(date(2026, 5, 1)) == 2025
    assert dc._latest_available_q1_year(date(2026, 4, 30)) == 2025
    assert dc._latest_available_q1_year(date(2026, 5, 1)) == 2026


def test_default_filing_cutoffs_use_shanghai_market_date(monkeypatch):
    monkeypatch.setattr(dc, "_shanghai_today", lambda: date(2026, 5, 1))

    assert dc._latest_completed_annual_year() == 2025
    assert dc._latest_available_q1_year() == 2026
    assert dc._latest_available_interim_period() == (2026, "03-31")


@pytest.mark.parametrize(
    "fetch",
    [
        dc.fetch_income_history,
        dc.fetch_cashflow_history,
        dc.fetch_main_financial_indicator_history,
    ],
)
def test_default_annual_histories_cover_ten_complete_years(monkeypatch, fetch):
    calls = []

    def fake_all(report_name, _columns, extra_filter, **_kwargs):
        year = int(extra_filter.split("'")[1][:4])
        calls.append((report_name, year))
        if report_name == dc.RPT_MAIN_FINANCIAL_INDICATORS:
            return pd.DataFrame([_indicator_row(year)])
        return pd.DataFrame([{"SECURITY_CODE": "000001", "REPORT_DATE": f"{year}-12-31"}])

    monkeypatch.setattr(dc, "_latest_completed_annual_year", lambda: 2025)
    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = fetch()

    assert dc.ANNUAL_HISTORY_YEARS == 10
    assert [year for _report, year in calls] == list(range(2025, 2015, -1))
    assert frame["REPORT_DATE"].astype(str).str[:10].tolist() == [f"{year}-12-31" for year in range(2016, 2026)]


def test_default_balance_history_covers_ten_complete_years_for_every_org_type(monkeypatch):
    calls = []

    def fake_all(report_name, columns, extra_filter, **_kwargs):
        year = int(extra_filter.split("'")[1][:4])
        report_index = list(dc._BALANCE_REPORT_COLUMNS).index(report_name)
        calls.append((report_name, year, columns))
        return pd.DataFrame(
            [
                {
                    "SECURITY_CODE": f"{report_index}{year:05d}",
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_ASSETS": 100.0,
                    "TOTAL_LIABILITIES": 40.0,
                    "TOTAL_EQUITY": 60.0,
                    "TOTAL_PARENT_EQUITY": 60.0,
                }
            ]
        )

    monkeypatch.setattr(dc, "_latest_completed_annual_year", lambda: 2025)
    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = dc.fetch_balance_history()

    expected_reports = {
        dc.RPT_BALANCE,
        dc.RPT_BALANCE_BANK,
        dc.RPT_BALANCE_INSURANCE,
        dc.RPT_BALANCE_SECURITIES,
    }
    assert len(calls) == dc.ANNUAL_HISTORY_YEARS * len(expected_reports)
    assert all("GOODWILL" in columns.split(",") for _report, _year, columns in calls)
    for year in range(2016, 2026):
        assert {report for report, called_year, _columns in calls if called_year == year} == expected_reports
    assert sorted(frame["REPORT_DATE"].astype(str).str[:10].unique().tolist()) == [
        f"{year}-12-31" for year in range(2016, 2026)
    ]


def test_single_company_balance_stops_after_the_matching_report_type(monkeypatch):
    calls = []

    def fake_all(report_name, _columns, extra_filter, **_kwargs):
        calls.append((report_name, extra_filter))
        assert report_name == dc.RPT_BALANCE
        year = extra_filter.split("'")[1][:4]
        return pd.DataFrame(
            [
                {
                    "SECURITY_CODE": "600519",
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_ASSETS": 100.0,
                    "TOTAL_LIABILITIES": 10.0,
                    "TOTAL_EQUITY": 90.0,
                    "TOTAL_PARENT_EQUITY": 90.0,
                    "GOODWILL": None,
                }
            ]
        )

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = dc.fetch_balance_history([2025, 2024], codes=["600519"])

    assert [report for report, _ in calls] == [dc.RPT_BALANCE, dc.RPT_BALANCE]
    assert all('(SECURITY_CODE in ("600519"))' in filter_text for _, filter_text in calls)
    assert frame["REPORT_DATE"].tolist() == ["2024-12-31", "2025-12-31"]


def test_single_company_balance_probes_until_its_matching_report_type(monkeypatch):
    calls = []

    def fake_all(report_name, _columns, extra_filter, **_kwargs):
        calls.append(report_name)
        if report_name == dc.RPT_BALANCE:
            raise dc.DataFetchError("code does not belong to general-company report")
        assert report_name == dc.RPT_BALANCE_BANK
        return pd.DataFrame(
            [
                {
                    "SECURITY_CODE": "000001",
                    "REPORT_DATE": "2025-12-31",
                    "TOTAL_ASSETS": 100.0,
                    "TOTAL_LIABILITIES": 10.0,
                    "TOTAL_EQUITY": 90.0,
                    "TOTAL_PARENT_EQUITY": 90.0,
                    "GOODWILL": None,
                }
            ]
        )

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = dc.fetch_balance_history([2025], codes=["000001"])

    assert calls == [dc.RPT_BALANCE, dc.RPT_BALANCE_BANK]
    assert frame["SECURITY_CODE"].tolist() == ["000001"]


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 4, 30), (2025, "09-30")),
        (date(2026, 5, 1), (2026, "03-31")),
        (date(2026, 8, 31), (2026, "03-31")),
        (date(2026, 9, 1), (2026, "06-30")),
        (date(2026, 10, 31), (2026, "06-30")),
        (date(2026, 11, 1), (2026, "09-30")),
    ],
)
def test_latest_interim_period_switches_only_after_filing_deadlines(today, expected):
    assert dc._latest_available_interim_period(today) == expected


@pytest.mark.parametrize(
    ("fetch", "report_name"),
    [
        (dc.fetch_interim_income_comparables, dc.RPT_INCOME),
        (dc.fetch_interim_cashflow_comparables, dc.RPT_CASHFLOW),
    ],
)
def test_interim_fetch_requires_current_and_prior_year_same_period(
    monkeypatch,
    fetch,
    report_name,
):
    calls = []

    def fake_all(actual_report, _columns, extra_filter, **_kwargs):
        report_date = extra_filter.split("'")[1]
        calls.append((actual_report, report_date))
        return pd.DataFrame([{"SECURITY_CODE": "000001", "REPORT_DATE": report_date}])

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = fetch((2026, "06-30"))

    assert calls == [
        (report_name, "2025-06-30"),
        (report_name, "2026-06-30"),
    ]
    assert frame["REPORT_DATE"].tolist() == ["2025-06-30", "2026-06-30"]


def test_detailed_interim_cashflow_fetch_validates_source_metadata(monkeypatch):
    calls = []

    def fake_all(report_name, columns, extra_filter, **_kwargs):
        report_date = extra_filter.split("'")[1]
        calls.append((report_name, columns, extra_filter))
        return pd.DataFrame([_detailed_cashflow_row(report_date)])

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)

    frame = dc.fetch_detailed_interim_cashflow_comparables((2026, "03-31"))

    assert [item[0] for item in calls] == [dc.RPT_DETAILED_CASHFLOW] * 2
    assert [item[2].split("'")[1] for item in calls] == ["2025-03-31", "2026-03-31"]
    assert all('SECURITY_TYPE_CODE="058001001"' in item[2] for item in calls)
    assert frame["REPORT_DATE"].tolist() == ["2025-03-31", "2026-03-31"]
    assert frame["SOURCE_REPORT_NAME"].eq(dc.RPT_DETAILED_CASHFLOW).all()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("REPORT_TYPE", "年报", "REPORT_TYPE"),
        ("REPORT_DATE_NAME", "2026年报", "REPORT_DATE_NAME"),
        ("CURRENCY", "USD", "non-CNY"),
        ("TOTAL_INVEST_OUTFLOW", float("inf"), "non-finite"),
    ],
)
def test_detailed_interim_cashflow_rejects_incompatible_or_nonfinite_rows(field, value, message):
    prior = _detailed_cashflow_row("2025-03-31")
    current = _detailed_cashflow_row("2026-03-31")
    current[field] = value

    with pytest.raises(dc.DataFetchError, match=message):
        dc._validate_detailed_interim_cashflow(
            pd.DataFrame([prior, current]),
            ["2025-03-31", "2026-03-31"],
        )


def test_fixed_2026_q1_aliases_are_not_part_of_the_data_contract():
    assert not hasattr(dc, "fetch_income_q1_2026")
    assert not hasattr(dc, "fetch_cashflow_q1_2026")


def test_balance_aliases_are_preserved_or_none_not_estimated():
    raw = pd.DataFrame(
        [
            {
                "SECURITY_CODE": "000001",
                "PARENT_EQUITY": 90,
                "MINORITY_INTEREST": 10,
                "LONG_TERM_LOAN": 20,
                "BOND_PAYABLE": 30,
                "LEASE_LIABILITY": 5,
                "SHORT_BOND_PAYABLE": 6,
                "BORROW_FUND": 7,
                "LOAN_PBC": 8,
                "SUBBOND_PAYABLE": 9,
            }
        ]
    )
    normalized = dc._add_canonical_balance_columns(raw).iloc[0]
    assert normalized["TOTAL_PARENT_EQUITY"] == 90
    assert normalized["MINORITY_EQUITY"] == 10
    assert normalized["LONG_LOAN"] == 20
    assert normalized["BONDS_PAYABLE"] == 30
    assert normalized["LEASE_LIAB"] == 5
    assert normalized["SHORT_BONDS_PAYABLE"] == 6
    assert normalized["BORROW_FUNDS"] == 7
    assert normalized["CENTRAL_BANK_BORROWING"] == 8
    assert normalized["SUBORDINATED_BONDS_PAYABLE"] == 9
    assert normalized["SHORT_LOAN"] is None


def test_balance_history_preserves_requested_annual_parent_equity_rows_for_financials(
    monkeypatch,
):
    report_codes = {
        dc.RPT_BALANCE: "600519",
        dc.RPT_BALANCE_BANK: "000001",
        dc.RPT_BALANCE_INSURANCE: "601628",
        dc.RPT_BALANCE_SECURITIES: "600030",
    }
    calls = []

    def fake_all(report_name, _columns, extra_filter, **_kwargs):
        year = int(extra_filter.split("'")[1][:4])
        calls.append((report_name, year))
        return pd.DataFrame(
            [
                {
                    "SECURITY_CODE": report_codes[report_name],
                    "REPORT_DATE": f"{year}-12-31",
                    "TOTAL_ASSETS": 1_000 + year,
                    "TOTAL_LIABILITIES": 400,
                    "TOTAL_EQUITY": 600 + year,
                    "TOTAL_PARENT_EQUITY": 500 + year,
                    "MINORITY_EQUITY": 100,
                }
            ]
        )

    monkeypatch.setattr(dc, "_fetch_all_pages", fake_all)
    years = [2025, 2024, 2023, 2022, 2021]
    frame = dc.fetch_balance_history(years)
    bank = frame[frame["SECURITY_CODE"].eq("000001")]
    assert len(calls) == len(years) * 4
    assert bank["REPORT_DATE"].tolist() == [f"{year}-12-31" for year in reversed(years)]
    assert bank["TOTAL_PARENT_EQUITY"].notna().all()
    assert len(bank) >= 3
    assert not bank["TOTAL_PARENT_EQUITY"].equals(bank["TOTAL_EQUITY"])
