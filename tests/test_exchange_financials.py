from __future__ import annotations

from datetime import date
import io
import json
from urllib.parse import urlencode
from zipfile import ZipFile

import pytest

from data.exchange_financials import (
    SSE_XBRL_URL,
    ExchangeFinancialClient,
    ExchangeFinancialError,
    _docx_text_and_rows,
    _parse_szse_docx,
    backfill_exchange_financial_gaps,
)


def _sse_payload(code: str = "600519", year: int = 2026) -> bytes:
    rows = [
        {
            "STOCK_ID": code,
            "REPORT_YEAR": str(year),
            "REPORT_PERIOD_ID": "1000",
            "S2010_0380": 1_000_000_000,
            "S2020_0010": 500_000_000,
            "S2090_0040": 10_000,
            "S2090_0060": 5_000,
        }
    ]
    return json.dumps(
        {"securityCode": code, "result": rows, "pageHelp": {"data": rows, "total": 1}},
        separators=(",", ":"),
    ).encode()


class _Response:
    def __init__(self, url: str, raw: bytes) -> None:
        self.url = url
        self._raw = raw
        self.headers = {"Content-Length": str(len(raw))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield from (self._raw[offset : offset + chunk_size] for offset in range(0, len(self._raw), chunk_size))

    def close(self) -> None:
        return None


class _SseSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, url, *, params, **_kwargs):
        self.calls += 1
        return _Response(f"{url}?{urlencode(params)}", _sse_payload(params["stockId"], int(params["reportYear"])))


def _docx() -> bytes:
    title = "深市主板上市公司2026年中期主要财务指标"
    rows = [
        ["截至日期：2026-08-28"],
        ["股票代码", "股票简称", "净利润（万元）", "每股收益（元）", "每股经营性现金流量（元）", "分配预案"],
        ["000001", "平安银行", "2569600", "1.24", "11.08", "10派2.49元(含税)"],
    ]
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    xml = f'<w:document xmlns:w="{namespace}"><w:body><w:p><w:r><w:t>{title}</w:t></w:r></w:p><w:tbl>{body}</w:tbl></w:body></w:document>'
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_sse_fetch_replays_raw_cache_and_converts_wan_yuan_fields(tmp_path):
    session = _SseSession()
    client = ExchangeFinancialClient(
        session=session,
        cache_dir=tmp_path,
        retries=1,
        request_interval=0,
    )
    first = client.fetch_sse("600519", {2026})
    second = client.fetch_sse("600519", {2026})

    assert first == second
    assert session.calls == 1
    assert first[0]["source_fields"] == {
        "PARENT_NETPROFIT": 100_000_000.0,
        "NETCASH_OPERATE": 50_000_000.0,
        "TOTAL_OPERATE_INCOME": 500_000_000.0,
    }
    assert len(first[0]["source_raw_sha256"]) == 64


def test_szse_docx_keeps_per_share_cash_flow_out_of_total_cash_flow():
    rows = _parse_szse_docx(
        _docx(),
        source_url="https://investor.szse.cn/market/subject/P020260812534093407624.docx",
        expected_title="深市主板上市公司2026年中期主要财务指标",
        expected_board="主板",
        expected_report_date="2026-06-30",
        as_of=date(2026, 8, 28),
        requested_codes={"000001"},
    )

    assert rows[0]["source_fields"] == {"PARENT_NETPROFIT": 25_696_000_000.0}
    assert rows[0]["per_share_indicators"]["OPERATING_CASH_FLOW_PER_SHARE"] == 11.08
    assert "NETCASH_OPERATE" not in rows[0]["source_fields"]


def test_szse_docx_rejects_xml_entities():
    xml = b'<!DOCTYPE x [<!ENTITY payload "forged">]><x>&payload;</x>'
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)

    with pytest.raises(ExchangeFinancialError, match="XML is invalid"):
        _docx_text_and_rows(buffer.getvalue())


def test_exchange_overlay_fills_only_missing_fields_and_records_conflicts():
    source = {
        "600001": {
            "revenue_history": [{"REPORT_DATE": "2025-12-31"}],
            "income_history": [{"REPORT_DATE": "2025-12-31", "PARENT_NETPROFIT": 99.0}],
            "cashflow": [{"REPORT_DATE": "2025-12-31"}],
            "income_interim": [],
            "cashflow_interim": [],
        }
    }

    class _Client:
        def fetch_sse(self, code, years):
            assert code == "600001"
            assert years == {2025, 2026}
            return [
                {
                    "security_code": code,
                    "report_date": "2025-12-31",
                    "source_kind": "sse_xbrl_summary",
                    "source_url": SSE_XBRL_URL,
                    "source_raw_sha256": "a" * 64,
                    "source_fields": {"PARENT_NETPROFIT": 100.0, "TOTAL_OPERATE_INCOME": 500.0},
                    "company_statement": False,
                }
            ]

        def fetch_szse(self, *_args, **_kwargs):
            return []

        def diagnostic(self):
            return {"network_requests": 0, "cache_hits": 0, "request_limit": 72}

    outcome = backfill_exchange_financial_gaps(
        source,
        {
            "annual_report_date": "2025-12-31",
            "current_interim_report_date": "2026-06-30",
            "prior_interim_report_date": "2025-06-30",
        },
        codes=["600001"],
        as_of=date(2026, 8, 28),
        client=_Client(),
    )

    annual = outcome.financials["600001"]["income_history"][0]
    assert annual["PARENT_NETPROFIT"] == 99.0
    assert annual["TOTAL_OPERATE_INCOME"] == 500.0
    assert annual["TOTAL_OPERATE_INCOME_PROVENANCE"]["security_code"] == "600001"
    assert outcome.diagnostic["conflicts"] == 1
    assert outcome.diagnostic["filled_fields"] == 2


def test_exchange_overlay_ignores_valid_sse_periods_outside_the_generation_contract():
    source = {
        "688302": {
            "revenue_history": [{"REPORT_DATE": "2025-12-31"}],
            "income_history": [{"REPORT_DATE": "2025-12-31"}],
            "cashflow": [{"REPORT_DATE": "2025-12-31"}],
            "income_interim": [
                {"REPORT_DATE": "2025-03-31", "PARENT_NETPROFIT": -1.0},
                {"REPORT_DATE": "2026-03-31", "PARENT_NETPROFIT": -2.0},
            ],
            "cashflow_interim": [],
        }
    }

    class _Client:
        def fetch_sse(self, code, years):
            assert code == "688302"
            assert years == {2025, 2026}
            return [
                {
                    "security_code": code,
                    "report_date": "2025-06-30",
                    "source_kind": "sse_xbrl_summary",
                    "source_url": SSE_XBRL_URL,
                    "source_raw_sha256": "b" * 64,
                    "source_fields": {"PARENT_NETPROFIT": -3.0},
                    "company_statement": False,
                }
            ]

        def fetch_szse(self, *_args, **_kwargs):
            return []

        def diagnostic(self):
            return {"network_requests": 0, "cache_hits": 0, "request_limit": 72}

    outcome = backfill_exchange_financial_gaps(
        source,
        {
            "annual_report_date": "2025-12-31",
            "current_interim_report_date": "2026-03-31",
            "prior_interim_report_date": "2025-03-31",
        },
        codes=["688302"],
        as_of=date(2026, 8, 31),
        client=_Client(),
    )

    assert [row["REPORT_DATE"] for row in outcome.financials["688302"]["income_interim"]] == [
        "2025-03-31",
        "2026-03-31",
    ]
    assert outcome.diagnostic["source_records"] == 1
    assert outcome.diagnostic["ignored_non_target_records"] == 1
    assert outcome.diagnostic["filled_fields"] == 0
