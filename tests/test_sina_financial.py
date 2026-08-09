from __future__ import annotations

import json
from threading import Event, Lock
from urllib.parse import urlencode

import pytest
import requests

import data.sina_financial as sf
from data.capex_evidence import resolve_capex_evidence, validate_capex_provenance


CONTRACT = {
    "annual_report_date": "2025-12-31",
    "current_interim_report_date": "2026-03-31",
    "prior_interim_report_date": "2025-03-31",
    "period_basis": "FY + current YTD - prior-year comparable YTD",
}


def _period(statement: str, rows: list[tuple[str, str, object]], *, publish_date: str = "20260425") -> dict:
    return {
        "rType": "合并期末",
        "rCurrency": "CNY",
        "data_source": "定期报告",
        "is_audit": "未审计",
        "audit_opinion": "",
        "publish_date": publish_date,
        "update_time": 1777029605,
        "is_exist_yoy": True,
        "data": [
            {
                "item_field": field,
                "item_title": title,
                "item_value": value,
                "item_source": statement,
                "item_tongbi": None,
            }
            for field, title, value in rows
        ],
    }


def _payload(statement: str, periods: dict[str, list[tuple[str, str, object]]]) -> dict:
    return {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": sum(len(rows) for rows in periods.values()),
                "report_date": [],
                "report_list": {period: _period(statement, rows) for period, rows in periods.items()},
            },
        }
    }


def _response_url(code: str, statement: str, *, num: int = 8) -> str:
    params = {
        "paperCode": ("sh" if code.startswith("6") else "sz") + code,
        "source": statement,
        "type": "0",
        "page": "1",
        "num": str(num),
    }
    return f"{sf.SINA_FINANCIAL_URL}?{urlencode(params)}"


class FakeResponse:
    def __init__(self, payload, *, url: str, status: int = 200, headers=None, raw: bytes | None = None):
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": "application/json; charset=utf-8"} if headers is None else headers
        self._raw = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def iter_content(self, *, chunk_size):
        assert chunk_size == sf.SINA_RESPONSE_CHUNK_BYTES
        yield self._raw

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, *, params, headers, timeout, stream):
        self.calls.append((url, dict(params), dict(headers), timeout, stream))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


def _client(tmp_path, session, **kwargs):
    return sf.SinaFinancialClient(
        cache_dir=tmp_path,
        session_factory=lambda: session,
        sleeper=lambda _seconds: None,
        **kwargs,
    )


@pytest.mark.parametrize("value", ["600519", "002731", "300001", "688302"])
def test_normalize_a_share_code_accepts_only_canonical_shenzhen_shanghai(value):
    assert sf.normalize_a_share_code(value) == value


@pytest.mark.parametrize("value", ["920002", "830001", "SH600519", "600519.SH", "123", True, None])
def test_normalize_a_share_code_rejects_ambiguous_or_unsupported_values(value):
    with pytest.raises((TypeError, ValueError)):
        sf.normalize_a_share_code(value)


def test_client_uses_exact_contract_and_maps_stable_item_fields(tmp_path):
    payload = _payload(
        "lrb",
        {
            "20260331": [
                ("BIZTOTINCO", "营业总收入", "54702912385.23"),
                ("BIZINCO", "营业收入", "53909252220.51"),
            ]
        },
    )
    response = FakeResponse(payload, url=_response_url("600519", "lrb"))
    session = FakeSession([response])

    result = _client(tmp_path, session).fetch_one("600519", "lrb", contract=CONTRACT)

    assert result.status == "ok"
    assert result.records == (
        {
            "REPORT_DATE": "2026-03-31",
            "TOTAL_OPERATE_INCOME": 54_702_912_385.23,
            "OPERATE_INCOME": 53_909_252_220.51,
            "SOURCE_PROVENANCE": result.records[0]["SOURCE_PROVENANCE"],
        },
    )
    assert result.records[0]["SOURCE_PROVENANCE"]["source_raw_sha256"] == result.raw_sha256
    assert session.calls == [
        (
            sf.SINA_FINANCIAL_URL,
            {
                "paperCode": "sh600519",
                "source": "lrb",
                "type": "0",
                "page": "1",
                "num": "8",
            },
            sf.SINA_FINANCIAL_HEADERS,
            sf.SINA_FINANCIAL_TIMEOUT,
            True,
        )
    ]
    assert response.closed is True


def test_client_maps_cashflow_and_builds_valid_capex_provenance(tmp_path):
    payload = _payload(
        "llb",
        {
            "20260331": [
                ("MANANETR", "经营活动产生的现金流量净额", "26909891269.13"),
                ("ACQUASSETCASH", "购建固定资产、无形资产和其他长期资产所支付的现金", "604791583.89"),
            ]
        },
    )
    session = FakeSession([FakeResponse(payload, url=_response_url("600519", "llb"))])

    result = _client(tmp_path, session).fetch_one("600519", "llb", contract=CONTRACT)
    record = result.records[0]

    assert record["NETCASH_OPERATE"] == 26_909_891_269.13
    assert record["CONSTRUCT_LONG_ASSET"] == 604_791_583.89
    assert (
        validate_capex_provenance(
            record["CAPEX_PROVENANCE"],
            expected_value=record["CONSTRUCT_LONG_ASSET"],
            expected_report_date="2026-03-31",
            expected_security_code="600519",
        )
        == "complete"
    )


def test_true_empty_is_distinct_from_schema_drift_and_missing_items(tmp_path):
    empty = {"result": {"status": {"code": 0}, "data": {"report_count": 0, "report_date": [], "report_list": {}}}}
    missing_items = _payload("lrb", {"20260331": [("PARENETP", "归属于母公司所有者的净利润", "1")]})
    old_broken_shape = {"result": {"status": {"code": 0}, "data": {"lrb": []}}}
    session = FakeSession(
        [
            FakeResponse(empty, url=_response_url("600519", "lrb")),
            FakeResponse(missing_items, url=_response_url("600519", "lrb")),
            FakeResponse(old_broken_shape, url=_response_url("600519", "lrb")),
        ]
    )
    client = _client(tmp_path, session)

    assert client.fetch_one("600519", "lrb", contract={**CONTRACT, "cache_key": "empty"}).status == "true_empty"
    assert (
        client.fetch_one("600519", "lrb", contract={**CONTRACT, "cache_key": "missing"}).status
        == "missing_component"
    )
    assert (
        client.fetch_one("600519", "lrb", contract={**CONTRACT, "cache_key": "schema"}).status == "schema_drift"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        "https://quotes.sina.cn.evil.example/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        "https://user@quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        "https://quotes.sina.cn:444/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        "https://quotes.sina.cn/cn/api/openapi.php/OtherService",
    ],
)
def test_redirect_target_is_strictly_allowlisted_and_not_retried(tmp_path, url):
    payload = _payload("lrb", {"20260331": [("BIZTOTINCO", "营业总收入", "1")]})
    response = FakeResponse(payload, url=url)
    session = FakeSession([response])

    result = _client(tmp_path, session, retries=3).fetch_one("600519", "lrb", contract=CONTRACT)

    assert result.status == "schema_drift"
    assert len(session.calls) == 1
    assert response.closed is True


def test_duplicate_json_keys_and_oversized_bodies_fail_closed_without_retry(tmp_path, monkeypatch):
    duplicate = b'{"result":{"status":{"code":0,"code":1},"data":{"report_count":0,"report_list":{}}}}'
    duplicate_response = FakeResponse({}, url=_response_url("600519", "lrb"), raw=duplicate)
    oversized_response = FakeResponse(
        {},
        url=_response_url("600519", "llb"),
        headers={"Content-Type": "application/json", "Content-Length": "65"},
    )
    session = FakeSession([duplicate_response, oversized_response])
    client = _client(tmp_path, session, retries=3)

    assert client.fetch_one("600519", "lrb", contract={**CONTRACT, "cache_key": "dup"}).status == "schema_drift"
    monkeypatch.setattr(sf, "MAX_SINA_FINANCIAL_RESPONSE_BYTES", 64)
    assert client.fetch_one("600519", "llb", contract={**CONTRACT, "cache_key": "large"}).status == "resource_limit"
    assert len(session.calls) == 2


def test_only_transient_transport_errors_are_retried(tmp_path):
    payload = _payload("lrb", {"20260331": [("BIZTOTINCO", "营业总收入", "1")]})
    success = FakeResponse(payload, url=_response_url("600519", "lrb"))
    session = FakeSession([requests.ReadTimeout("slow"), success])

    result = _client(tmp_path, session, retries=2).fetch_one("600519", "lrb", contract=CONTRACT)

    assert result.status == "ok"
    assert len(session.calls) == 2


def test_cache_hit_per_contract_per_statement_performs_zero_network(tmp_path):
    payload = _payload("lrb", {"20260331": [("BIZTOTINCO", "营业总收入", "1")]})
    first_session = FakeSession([FakeResponse(payload, url=_response_url("600519", "lrb"))])
    first = _client(tmp_path, first_session).fetch_one("600519", "lrb", contract=CONTRACT)
    no_network = FakeSession([])

    second = _client(tmp_path, no_network).fetch_one("600519", "lrb", contract=CONTRACT)

    assert first.status == second.status == "ok"
    assert second.cache_hit is True
    assert no_network.calls == []


def _primary_financials() -> dict[str, dict]:
    return {
        "600519": {
            "revenue_history": [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": None}],
            "income_interim": [
                {"REPORT_DATE": "2025-03-31", "TOTAL_OPERATE_INCOME": 80.0},
                {"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": None},
            ],
            "cashflow": [],
            "cashflow_interim": [],
        }
    }


def test_gap_only_overlay_fills_null_fields_and_preserves_primary_conflicts():
    primary = _primary_financials()
    revenue_records = (
        {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 100.0, "SOURCE_PROVENANCE": {"id": "fy"}},
        {"REPORT_DATE": "2025-03-31", "TOTAL_OPERATE_INCOME": 90.0, "SOURCE_PROVENANCE": {"id": "prior"}},
        {"REPORT_DATE": "2026-03-31", "TOTAL_OPERATE_INCOME": 110.0, "SOURCE_PROVENANCE": {"id": "current"}},
    )

    class Client:
        def fetch_many(self, requests_, *, contract, force_refresh=False):
            assert requests_ == (("600519", "llb"), ("600519", "lrb"))
            return {
                ("600519", "lrb"): sf.SinaStatementResult("600519", "lrb", "ok", revenue_records),
                ("600519", "llb"): sf.SinaStatementResult("600519", "llb", "true_empty"),
            }

    outcome = sf.backfill_strict_ttm_gaps(primary, CONTRACT, client=Client())

    assert outcome.financials["600519"]["revenue_history"][0]["TOTAL_OPERATE_INCOME"] == 100.0
    interim = {row["REPORT_DATE"]: row for row in outcome.financials["600519"]["income_interim"]}
    assert interim["2026-03-31"]["TOTAL_OPERATE_INCOME"] == 110.0
    assert interim["2025-03-31"]["TOTAL_OPERATE_INCOME"] == 80.0
    assert outcome.diagnostic["filled_fields"] == 2
    assert outcome.diagnostic["conflicts"] == 1
    assert primary["600519"]["revenue_history"][0]["TOTAL_OPERATE_INCOME"] is None


def test_complete_components_with_negative_reconstructed_capex_do_not_trigger_fallback():
    company = {"cashflow": [], "cashflow_interim": [], "revenue_history": [], "income_interim": []}
    for report_date, value, dataset in (
        ("2025-12-31", 1.0, "cashflow"),
        ("2025-03-31", 10.0, "cashflow_interim"),
        ("2026-03-31", 1.0, "cashflow_interim"),
    ):
        _capex, provenance = resolve_capex_evidence(value, None, report_date=report_date)
        company[dataset].append(
            {
                "REPORT_DATE": report_date,
                "NETCASH_OPERATE": 1.0,
                "CONSTRUCT_LONG_ASSET": value,
                "CAPEX_PROVENANCE": provenance,
            }
        )

    class Client:
        def fetch_many(self, requests_, **_kwargs):
            assert requests_ == (("600519", "lrb"),)
            return {("600519", "lrb"): sf.SinaStatementResult("600519", "lrb", "true_empty")}

    outcome = sf.backfill_strict_ttm_gaps({"600519": company}, CONTRACT, client=Client())

    assert outcome.diagnostic["target_requests"] == 1
    assert outcome.diagnostic["target_codes_by_metric"]["fcff"] == []


def test_batch_has_a_process_wide_network_concurrency_ceiling(tmp_path, monkeypatch):
    active = 0
    peak = 0
    lock = Lock()
    two_active = Event()

    class BlockingSession:
        def get(self, url, *, params, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    two_active.set()
            assert two_active.wait(timeout=5)
            with lock:
                active -= 1
            code = params["paperCode"][-6:]
            return FakeResponse(
                _payload("lrb", {"20260331": [("BIZTOTINCO", "营业总收入", "1")]}),
                url=_response_url(code, "lrb"),
            )

        def close(self):
            return None

    monkeypatch.setattr(sf, "SINA_FINANCIAL_REQUEST_SLOTS", sf.BoundedSemaphore(2))
    client = sf.SinaFinancialClient(
        cache_dir=tmp_path,
        session_factory=BlockingSession,
        max_workers=8,
        sleeper=lambda _seconds: None,
    )

    results = client.fetch_many(
        tuple((code, "lrb") for code in ("600001", "600002", "600003", "600004")),
        contract=CONTRACT,
        force_refresh=True,
    )

    assert len(results) == 4
    assert peak == 2


def test_valid_network_result_survives_cache_write_failure(tmp_path, monkeypatch):
    payload = _payload("lrb", {"20260331": [("BIZTOTINCO", "营业总收入", "1")]})
    client = _client(
        tmp_path,
        FakeSession([FakeResponse(payload, url=_response_url("600519", "lrb"))]),
    )
    monkeypatch.setattr(client, "_save_cache", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))

    result = client.fetch_one("600519", "lrb", contract=CONTRACT)

    assert result.status == "ok"
    assert client.diagnostic()["cache_write_errors"] == 1


def test_cache_hit_replays_raw_response_semantics_before_use(tmp_path):
    invalid_raw = b'{"result":{"status":{"code":0},"data":{"lrb":[]}}}'
    session = FakeSession(
        [
            FakeResponse(
                _payload("lrb", {"20260331": [("BIZTOTINCO", "营业总收入", "1")]}),
                url=_response_url("600519", "lrb"),
            )
        ]
    )
    client = _client(tmp_path, session)
    contract = sf._normalized_contract(CONTRACT)
    client._cache("600519", "lrb", contract).save(
        {
            "adapter_version": sf.SINA_FINANCIAL_ADAPTER_VERSION,
            "code": "600519",
            "statement": "lrb",
            "contract": contract,
            "status": "ok",
            "raw_sha256": sf.hashlib.sha256(invalid_raw).hexdigest(),
            "raw_response": invalid_raw,
            "retrieved_at": 1.0,
        }
    )

    result = client.fetch_one("600519", "lrb", contract=CONTRACT)

    assert result.status == "ok"
    assert len(session.calls) == 1
    assert client.diagnostic()["cache_invalid"] == 1


def test_gap_fallback_has_a_deterministic_total_request_budget():
    financials = {f"600{index:03d}": {} for index in range(5)}

    class Client:
        def fetch_many(self, requests_, **_kwargs):
            self.requests = tuple(requests_)
            return {
                identity: sf.SinaStatementResult(identity[0], identity[1], "true_empty")
                for identity in requests_
            }

    client = Client()
    outcome = sf.backfill_strict_ttm_gaps(
        financials,
        CONTRACT,
        client=client,
        max_target_requests=3,
    )

    assert client.requests == (
        ("600000", "llb"),
        ("600000", "lrb"),
        ("600001", "llb"),
    )
    assert outcome.diagnostic["candidate_requests"] == 10
    assert outcome.diagnostic["target_requests"] == 3
    assert outcome.diagnostic["skipped_requests"] == 7
    assert outcome.diagnostic["budget_exhausted"] is True
