import json

import pytest

from data.financial_source_evidence import (
    FinancialSourceEvidenceError,
    load_balance_sheet_evidence,
    load_zero_capex_evidence,
    load_zero_revenue_evidence,
)


def _payload(**overrides):
    record = {
        "code": "600610",
        "report_date": "2018-12-31",
        "value": 0.0,
        "source_document": "2018年年度报告",
        "source_url": "https://static.cninfo.com.cn/finalpage/2019-06-28/1206403533.PDF",
        "source_sha256": "a" * 64,
        "source_page": 7,
        "source_statement": "营业收入为0。",
    }
    record.update(overrides)
    return {
        "schema_version": 1,
        "metric": "TOTAL_OPERATE_INCOME",
        "policy": "test",
        "records": [record],
    }


def _write(tmp_path, payload):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _capex_payload(**overrides):
    record = {
        "code": "600503",
        "report_date": "2026-03-31",
        "value": 0.0,
        "source_document": "2026年第一季度报告",
        "source_url": (
            "https://dataclouds.cninfo.com.cn/shgonggao/hsomarket/2026/20260429/a1234567890b1234567890c123456789.PDF"
        ),
        "source_sha256": "b" * 64,
        "source_page": 11,
        "source_statement": "本期资本开支和投资活动现金流出小计均为空。",
    }
    record.update(overrides)
    return {
        "schema_version": 1,
        "metric": "CONSTRUCT_LONG_ASSET",
        "policy": "test",
        "records": [record],
    }


def _balance_sheet_payload(**overrides):
    record = {
        "code": "002766",
        "report_date": "2017-12-31",
        "canonical_values": {
            "TOTAL_ASSETS": 3_660_176_992.98,
            "TOTAL_LIABILITIES": 2_012_750_505.12,
            "TOTAL_EQUITY": 1_647_426_487.86,
            "TOTAL_PARENT_EQUITY": 1_627_587_784.89,
            "MINORITY_EQUITY": 19_838_702.97,
            "MONETARYFUNDS": 869_475_074.23,
            "SHORT_LOAN": 842_100_774.44,
        },
        "reporting_basis": "全面审计并追溯重述后的合并口径",
        "source_document": "2016-2018年度审计报告（更新后）",
        "source_url": "https://static.cninfo.com.cn/finalpage/2020-05-23/1207852063.PDF",
        "source_sha256": "c" * 64,
        "source_pages": [8, 9],
        "source_statement": "追溯重述合并资产负债表披露完整会计恒等式。",
    }
    record.update(overrides)
    return {
        "schema_version": 1,
        "metric": "BALANCE_SHEET_CANONICAL_VALUES",
        "policy": "test",
        "records": [record],
    }


def test_zero_revenue_evidence_loads_exact_exchange_document_contract(tmp_path):
    result = load_zero_revenue_evidence(_write(tmp_path, _payload()))

    evidence = result[("600610", "2018-12-31")]
    assert evidence["value"] == 0.0
    assert evidence["evidence_type"] == "exchange_filed_explicit_zero"
    assert evidence["source_page"] == 7


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("code", "920001", "code"),
        ("report_date", "2018-09-30", "report_date"),
        ("value", 1.0, "explicit zero"),
        ("value", True, "value"),
        ("source_url", "http://static.cninfo.com.cn/finalpage/2019-06-28/1206403533.PDF", "untrusted URL"),
        ("source_url", "https://evil.example/finalpage/2019-06-28/1206403533.PDF", "untrusted URL"),
        ("source_url", "https://static.cninfo.com.cn/finalpage/2019-06-28/1206403533.PDF?x=1", "untrusted URL"),
        ("source_sha256", "0" * 63, "SHA256"),
        ("source_page", 0, "page"),
        ("source_statement", "", "source_statement"),
    ],
)
def test_zero_revenue_evidence_rejects_unsafe_or_ambiguous_records(tmp_path, field, value, message):
    with pytest.raises(FinancialSourceEvidenceError, match=message):
        load_zero_revenue_evidence(_write(tmp_path, _payload(**{field: value})))


def test_zero_revenue_evidence_rejects_duplicate_identity(tmp_path):
    payload = _payload()
    payload["records"].append(dict(payload["records"][0]))

    with pytest.raises(FinancialSourceEvidenceError, match="duplicate"):
        load_zero_revenue_evidence(_write(tmp_path, payload))


def test_zero_revenue_evidence_does_not_inherit_capex_dataclouds_allowlist(tmp_path):
    with pytest.raises(FinancialSourceEvidenceError, match="untrusted URL"):
        load_zero_revenue_evidence(
            _write(
                tmp_path,
                _payload(
                    source_url=(
                        "https://dataclouds.cninfo.com.cn/shgonggao/hsomarket/2026/20260429/"
                        "a1234567890b1234567890c123456789.PDF"
                    )
                ),
            )
        )


def test_zero_capex_evidence_loads_exact_q1_statement_contract(tmp_path):
    result = load_zero_capex_evidence(_write(tmp_path, _capex_payload()))

    evidence = result[("600503", "2026-03-31")]
    assert evidence["value"] == 0.0
    assert evidence["evidence_type"] == "exchange_filed_statement_zero"
    assert evidence["metric"] == "CONSTRUCT_LONG_ASSET"


def test_zero_capex_evidence_loads_exact_annual_statement_contract(tmp_path):
    payload = _capex_payload(
        code="000670",
        report_date="2019-12-31",
        source_document="2019年年度报告全文",
        source_url="https://static.cninfo.com.cn/finalpage/2020-04-28/1207638298.PDF",
        source_page=75,
        source_statement="合并现金流量表2019年资本开支为空。",
    )

    result = load_zero_capex_evidence(_write(tmp_path, payload))

    evidence = result[("000670", "2019-12-31")]
    assert evidence["value"] == 0.0
    assert evidence["evidence_type"] == "exchange_filed_statement_zero"


def test_committed_zero_capex_evidence_is_limited_to_reviewed_identities():
    expected = {
        ("000670", "2019-12-31"),
        ("000691", "2018-12-31"),
        ("000953", "2019-12-31"),
        ("002072", "2019-12-31"),
        ("002072", "2020-12-31"),
        ("300270", "2020-12-31"),
        ("600106", "2018-12-31"),
        ("600137", "2016-12-31"),
        ("600191", "2018-12-31"),
        ("600301", "2019-12-31"),
        ("600610", "2018-12-31"),
        ("600725", "2017-12-31"),
        ("600817", "2018-12-31"),
        ("601005", "2017-12-31"),
        ("603991", "2022-12-31"),
        ("000668", "2025-03-31"),
        ("000995", "2025-03-31"),
        ("002200", "2025-03-31"),
        ("300426", "2025-03-31"),
        ("300506", "2025-03-31"),
        ("600503", "2026-03-31"),
        ("600743", "2026-03-31"),
        ("600854", "2026-03-31"),
        ("600857", "2025-03-31"),
        ("603895", "2025-03-31"),
        ("603895", "2026-03-31"),
    }

    assert set(load_zero_capex_evidence()) == expected


def test_committed_zero_revenue_evidence_is_limited_to_reviewed_identities():
    assert set(load_zero_revenue_evidence()) == {
        ("600610", "2018-12-31"),
        ("688302", "2023-12-31"),
        ("688382", "2022-12-31"),
    }


def test_balance_sheet_evidence_loads_reconciled_official_lineage(tmp_path):
    evidence = load_balance_sheet_evidence(_write(tmp_path, _balance_sheet_payload()))[("002766", "2017-12-31")]

    values = evidence["canonical_values"]
    assert values["TOTAL_ASSETS"] == 3_660_176_992.98
    assert values["TOTAL_PARENT_EQUITY"] == 1_627_587_784.89
    assert values["MINORITY_EQUITY"] == 19_838_702.97
    assert evidence["evidence_type"] == "exchange_filed_balance_sheet_lineage"
    assert evidence["source_pages"] == [8, 9]


def test_committed_balance_sheet_evidence_is_limited_to_reviewed_identities():
    assert set(load_balance_sheet_evidence()) == {
        ("002766", "2017-12-31"),
        ("600228", "2020-12-31"),
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("report_date", "2017-09-30", "report_date"),
        ("source_pages", [9, 8], "source_pages"),
        ("reporting_basis", "", "reporting_basis"),
        (
            "source_url",
            "https://dataclouds.cninfo.com.cn/shgonggao/hsomarket/2018/20180425/a1234567890b1234567890c123456789.PDF",
            "untrusted URL",
        ),
    ],
)
def test_balance_sheet_evidence_rejects_unsafe_record_metadata(tmp_path, field, value, message):
    with pytest.raises(FinancialSourceEvidenceError, match=message):
        load_balance_sheet_evidence(_write(tmp_path, _balance_sheet_payload(**{field: value})))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("TOTAL_ASSETS", True, "TOTAL_ASSETS"),
        ("TOTAL_LIABILITIES", -1.0, "balance-sheet totals"),
        ("MONETARYFUNDS", float("inf"), "MONETARYFUNDS"),
        ("SHORT_LOAN", -0.01, "negative SHORT_LOAN"),
        ("TOTAL_EQUITY", 1_647_000_000.0, "total=parent\\+minority"),
        ("TOTAL_ASSETS", 3_660_000_000.0, "assets=liabilities\\+equity"),
    ],
)
def test_balance_sheet_evidence_rejects_invalid_or_nonreconciling_values(tmp_path, field, value, message):
    payload = _balance_sheet_payload()
    payload["records"][0]["canonical_values"][field] = value

    with pytest.raises(FinancialSourceEvidenceError, match=message):
        load_balance_sheet_evidence(_write(tmp_path, payload))


def test_balance_sheet_evidence_requires_exact_canonical_field_allowlist(tmp_path):
    payload = _balance_sheet_payload()
    del payload["records"][0]["canonical_values"]["TOTAL_PARENT_EQUITY"]
    payload["records"][0]["canonical_values"]["UNREVIEWED_FIELD"] = 1.0

    with pytest.raises(FinancialSourceEvidenceError, match="invalid canonical fields"):
        load_balance_sheet_evidence(_write(tmp_path, payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("report_date", "2026-06-30", "report_date"),
        ("value", 0.01, "explicit zero"),
        (
            "source_url",
            "https://dataclouds.cninfo.com.cn/shgonggao/hsomarket/2026/20260429/not-a-hash.PDF",
            "untrusted URL",
        ),
        (
            "source_url",
            "https://dataclouds.cninfo.com.cn/shgonggao/hsomarket/2026/20260429/"
            "a1234567890b1234567890c123456789.PDF#page=11",
            "untrusted URL",
        ),
    ],
)
def test_zero_capex_evidence_rejects_broad_or_unsafe_overrides(tmp_path, field, value, message):
    with pytest.raises(FinancialSourceEvidenceError, match=message):
        load_zero_capex_evidence(_write(tmp_path, _capex_payload(**{field: value})))
