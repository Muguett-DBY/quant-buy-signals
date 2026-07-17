from __future__ import annotations

import copy

import pytest

from data.datacenter import RPT_MAIN_FINANCIAL_INDICATORS
from data.financial_indicator_evidence import (
    IndicatorEvidenceError,
    UnsupportedIndicatorMarketError,
    derive_main_financial_indicator_evidence,
)


def _indicator(report_date: str, **overrides):
    year = report_date[:4]
    values = {
        "SECUCODE": "600519.SH",
        "REPORT_DATE": report_date,
        "REPORT_TYPE": "年报",
        "REPORT_DATE_NAME": f"{year}年报",
        "REPORT_YEAR": year,
        "NOTICE_DATE": f"{int(year) + 1}-04-03",
        "SOURCE_REPORT_NAME": RPT_MAIN_FINANCIAL_INDICATORS,
        "RDEXPEND": 695_376_735.81,
        "ROIC": 35.039932454817,
        "ROEJQ": 36.02,
        "XSMLL": 91.9312166361,
        "XSJLL": 52.2733593678,
        "TAXRATE": 25.3294970785,
        "TOTAL_SHARE": 1_252_270_215,
        "STAFF_NUM": 34_750,
        "KCFJCXSYJLR": 86_240_905_977.42,
        "INTEREST_DEBT_RATIO": 7.8705947287,
    }
    values.update(overrides)
    return values


def _moutai_sample():
    """Two real-shaped annual Eastmoney samples, with source field units intact."""
    indicators = [
        _indicator("2024-12-31"),
        _indicator(
            "2025-12-31",
            NOTICE_DATE="2026-04-17",
            RDEXPEND=803_132_232.31,
            ROIC=31.424532666193,
            ROEJQ=32.53,
            XSMLL=91.1795516835,
            XSJLL=50.5278865155,
            TAXRATE=25.6588990863,
            STAFF_NUM=34_992,
            KCFJCXSYJLR=82_293_107_655.25,
            INTEREST_DEBT_RATIO=6.0138247195,
        ),
    ]
    revenue = [
        {"REPORT_DATE": "2024-12-31", "TOTAL_OPERATE_INCOME": 174_144_069_958.25},
        {"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 172_054_171_890.91},
    ]
    income = [
        {
            "REPORT_DATE": "2024-12-31",
            "TOTAL_OPERATE_INCOME": 174_144_069_958.25,
            "PARENT_NETPROFIT": 86_228_146_421.62,
        },
        {
            "REPORT_DATE": "2025-12-31",
            "TOTAL_OPERATE_INCOME": 172_054_171_890.91,
            "PARENT_NETPROFIT": 82_320_067_101.68,
        },
    ]
    cashflow = [
        {"REPORT_DATE": "2024-12-31", "NETCASH_OPERATE": 92_463_692_168.43},
        {"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 61_522_204_989.35},
    ]
    return {
        "indicators": indicators,
        "revenue_history": revenue,
        "income_history": income,
        "cashflow": cashflow,
    }


def test_real_field_sample_normalises_units_and_builds_auditable_derivatives():
    result = derive_main_financial_indicator_evidence(_moutai_sample(), expected_code="600519", expected_market="SH")

    assert result["status"] == "complete"
    assert result["eligible"] is True
    assert result["security_code"] == "600519"
    assert result["market"] == "SH"
    assert result["as_of"] == "2025-12-31"
    assert result["period_count"] == 2
    assert result["coverage"] == {
        "metric_count": 13,
        "complete_metric_count": 13,
        "partial_metric_count": 0,
        "missing_metric_count": 0,
    }

    roic = result["metrics"]["roic"]
    assert roic["raw_unit"] == "percent"
    assert roic["unit"] == "ratio"
    assert roic["latest"]["raw_value"] == pytest.approx(31.424532666193)
    assert roic["latest_value"] == pytest.approx(0.31424532666193)
    assert roic["trend"]["direction"] == "falling"
    assert roic["stability"]["sample_count"] == 2

    rd_expense = result["metrics"]["rd_expense"]
    assert rd_expense["unit"] == "CNY"
    assert rd_expense["latest_value"] == pytest.approx(803_132_232.31)
    rd_intensity = result["metrics"]["rd_intensity"]
    assert rd_intensity["formula"] == "RDEXPEND / TOTAL_OPERATE_INCOME"
    assert rd_intensity["latest_value"] == pytest.approx(803_132_232.31 / 172_054_171_890.91)
    assert rd_intensity["latest"]["components"]["denominator"]["source_field"] == "TOTAL_OPERATE_INCOME"

    cash_conversion = result["metrics"]["operating_cashflow_to_net_profit"]
    assert cash_conversion["latest_value"] == pytest.approx(61_522_204_989.35 / 82_320_067_101.68)
    assert cash_conversion["latest"]["components"]["numerator"]["source_field"] == "NETCASH_OPERATE"

    adjusted_profit = result["metrics"]["adjusted_net_profit"]
    assert "不是经营现金流/净利润" in adjusted_profit["definition"]
    adjusted_ratio = result["metrics"]["adjusted_net_profit_to_net_profit"]
    assert adjusted_ratio["latest_value"] == pytest.approx(82_293_107_655.25 / 82_320_067_101.68)
    assert adjusted_ratio["latest_value"] != pytest.approx(cash_conversion["latest_value"])
    assert not any("score" in key for key in result)


def test_bank_sector_fields_are_source_labeled_and_derived_ratios_are_replayable():
    financials = _moutai_sample()
    bank_rows = (
        {
            "NET_INTEREST_MARGIN": 1.98,
            "NET_INTEREST_SPREAD": 1.86,
            "NEWCAPITALADER": 19.05,
            "FIRST_ADEQUACY_RATIO": 17.48,
            "NONPERLOAN": 0.95,
            "LOAN_PROVISION_RATIO": 3.92,
            "TOTALDEPOSITS": 9_096_587_000_000,
            "GROSSLOANS": 6_888_315_000_000,
            "LOAN_ADVANCES": 6_632_548_000_000,
        },
        {
            "NET_INTEREST_MARGIN": 1.87,
            "NET_INTEREST_SPREAD": 1.78,
            "NEWCAPITALADER": 18.24,
            "FIRST_ADEQUACY_RATIO": 16.51,
            "NONPERLOAN": 0.94,
            "LOAN_PROVISION_RATIO": 3.68,
            "TOTALDEPOSITS": 9_836_130_000_000,
            "GROSSLOANS": 7_258_058_000_000,
            "LOAN_ADVANCES": 7_004_238_000_000,
        },
    )
    for record, fields in zip(financials["indicators"], bank_rows):
        record.update(fields)

    result = derive_main_financial_indicator_evidence(financials, industry_code="BANK")
    metrics = result["metrics"]

    assert result["schema_version"] == 2
    assert result["industry_code"] == "BANK"
    assert result["coverage"]["metric_count"] == 24
    assert metrics["net_interest_margin"]["latest_value"] == pytest.approx(0.0187)
    assert metrics["net_interest_margin"]["latest"]["evidence_type"] == "provider_standardized"
    provision = metrics["loan_provision_coverage_proxy"]
    assert provision["latest_value"] == pytest.approx(0.0368 / 0.0094)
    assert provision["latest"]["evidence_type"] == "derived_calculation"
    assert provision["latest"]["components"]["numerator"]["source_field"] == "LOAN_PROVISION_RATIO"
    loan_to_deposit = metrics["gross_loan_to_deposit_ratio"]
    assert loan_to_deposit["latest_value"] == pytest.approx(7_258_058_000_000 / 9_836_130_000_000)


@pytest.mark.parametrize(
    ("industry", "fields", "metric", "expected"),
    [
        (
            "INSURANCE",
            {
                "SOLVENCY_AR": 193.3,
                "NBV_RATE": 28.5,
                "NBV_LIFE": 36_897_000_000,
                "EARNED_PREMIUM": 559_502_000_000,
                "SURRENDER_RATE_LIFE": 1.52,
            },
            "solvency_adequacy_ratio",
            1.933,
        ),
        (
            "SECURITIES",
            {
                "CAPITAL_LEVERAGE_RATIO": 13.83,
                "CAPITAL_PROVISIONS_SUM": 74_667_968_740.27,
                "LIQUIDITY_COVERAGE_RATIO": 137.8,
                "NET_CAPITAL_LIABILITIES": 18.51,
                "PROPRIETARY_CAPITAL": 38.89,
                "RISK_COVERAGE": 210.46,
                "NET_FUNDING_RATIO": 125.27,
            },
            "risk_coverage_ratio",
            2.1046,
        ),
    ],
)
def test_insurance_and_securities_fields_keep_provider_units_and_scope(industry, fields, metric, expected):
    financials = _moutai_sample()
    for record in financials["indicators"]:
        record.update(fields)

    result = derive_main_financial_indicator_evidence(financials, industry_code=industry)

    assert result["industry_code"] == industry
    assert result["metrics"][metric]["latest_value"] == pytest.approx(expected)
    assert result["metrics"][metric]["latest"]["evidence_label"] == "数据商标准化字段"


def test_financial_sector_missing_value_is_not_zero_or_silently_backfilled():
    financials = _moutai_sample()
    for index, record in enumerate(financials["indicators"]):
        record.update(
            {
                "SOLVENCY_AR": 200.0 if index == 0 else None,
                "NBV_RATE": 20.0,
                "NBV_LIFE": 10.0,
                "EARNED_PREMIUM": 100.0,
                "SURRENDER_RATE_LIFE": 2.0,
            }
        )

    metric = derive_main_financial_indicator_evidence(financials, industry_code="INSURANCE")["metrics"][
        "solvency_adequacy_ratio"
    ]

    assert metric["status"] == "partial"
    assert metric["latest_value"] is None
    assert metric["latest_available_value"] == pytest.approx(2.0)
    assert metric["latest"]["evidence_type"] == "missing"
    assert metric["latest"]["evidence_label"] == "缺失"


def test_financial_sector_contract_rejects_omitted_required_source_field():
    financials = _moutai_sample()
    for record in financials["indicators"]:
        record.update(
            {
                field: 1.0
                for field in (
                    "NET_INTEREST_MARGIN",
                    "NET_INTEREST_SPREAD",
                    "NEWCAPITALADER",
                    "FIRST_ADEQUACY_RATIO",
                    "NONPERLOAN",
                    "LOAN_PROVISION_RATIO",
                    "TOTALDEPOSITS",
                    "GROSSLOANS",
                    "LOAN_ADVANCES",
                )
            }
        )
    financials["indicators"][1].pop("NONPERLOAN")

    with pytest.raises(IndicatorEvidenceError, match="omitted source fields"):
        derive_main_financial_indicator_evidence(financials, industry_code="BANK")


def test_missing_is_not_zero_and_latest_period_is_never_silently_backfilled():
    financials = _moutai_sample()
    financials["indicators"][0]["RDEXPEND"] = 0
    financials["indicators"][1]["RDEXPEND"] = None

    rd_expense = derive_main_financial_indicator_evidence(financials)["metrics"]["rd_expense"]

    assert rd_expense["status"] == "partial"
    assert rd_expense["sample_count"] == 1
    assert rd_expense["missing_count"] == 1
    assert rd_expense["observations"][0]["value"] == 0
    assert rd_expense["observations"][0]["is_missing"] is False
    assert rd_expense["latest_date"] == "2025-12-31"
    assert rd_expense["latest_value"] is None
    assert rd_expense["latest_available_date"] == "2024-12-31"
    assert rd_expense["latest_available_value"] == 0


def test_zero_denominator_is_missing_with_an_explicit_reason():
    financials = _moutai_sample()
    financials["income_history"][1]["PARENT_NETPROFIT"] = 0

    metrics = derive_main_financial_indicator_evidence(financials)["metrics"]

    for metric_name in ("operating_cashflow_to_net_profit", "adjusted_net_profit_to_net_profit"):
        metric = metrics[metric_name]
        assert metric["latest_value"] is None
        assert metric["latest"]["missing_reason"] == "denominator_zero"
        assert metric["missing_by_reason"] == {"denominator_zero": 1}


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), True, "31.4"])
def test_rejects_nonfinite_boolean_and_string_indicator_values(bad_value):
    financials = _moutai_sample()
    financials["indicators"][0]["ROIC"] = bad_value

    with pytest.raises(IndicatorEvidenceError, match="finite numeric|NaN or infinity"):
        derive_main_financial_indicator_evidence(financials)


def test_rejects_nonfinite_auxiliary_values_instead_of_calling_them_missing():
    financials = _moutai_sample()
    financials["cashflow"][1]["NETCASH_OPERATE"] = float("nan")

    with pytest.raises(IndicatorEvidenceError, match="NaN or infinity"):
        derive_main_financial_indicator_evidence(financials)


def test_rejects_duplicate_indicator_periods():
    financials = _moutai_sample()
    financials["indicators"].append(copy.deepcopy(financials["indicators"][-1]))

    with pytest.raises(IndicatorEvidenceError, match="duplicate annual report dates"):
        derive_main_financial_indicator_evidence(financials)


def test_rejects_out_of_order_indicator_periods():
    financials = _moutai_sample()
    financials["indicators"].reverse()

    with pytest.raises(IndicatorEvidenceError, match="oldest-to-newest"):
        derive_main_financial_indicator_evidence(financials)


def test_rejects_duplicate_or_out_of_order_auxiliary_histories():
    duplicate = _moutai_sample()
    duplicate["cashflow"].append(copy.deepcopy(duplicate["cashflow"][-1]))
    with pytest.raises(IndicatorEvidenceError, match="duplicate annual report dates"):
        derive_main_financial_indicator_evidence(duplicate)

    disordered = _moutai_sample()
    disordered["income_history"].reverse()
    with pytest.raises(IndicatorEvidenceError, match="oldest-to-newest"):
        derive_main_financial_indicator_evidence(disordered)


@pytest.mark.parametrize("market", ["BJ", "HK", "US"])
def test_explicitly_excludes_beijing_and_other_unsupported_markets(market):
    with pytest.raises(UnsupportedIndicatorMarketError):
        derive_main_financial_indicator_evidence({}, expected_code="920002", expected_market=market)


def test_rejects_beijing_identity_even_without_an_expected_market():
    financials = _moutai_sample()
    for record in financials["indicators"]:
        record["SECUCODE"] = "920002.BJ"

    with pytest.raises(UnsupportedIndicatorMarketError, match="Beijing"):
        derive_main_financial_indicator_evidence(financials)


def test_excludes_beijing_code_even_when_no_market_or_records_are_available():
    with pytest.raises(UnsupportedIndicatorMarketError, match="outside"):
        derive_main_financial_indicator_evidence({}, expected_code="920002")


def test_rejects_mixed_or_unexpected_security_identity():
    mixed = _moutai_sample()
    mixed["indicators"][1]["SECUCODE"] = "000001.SZ"
    with pytest.raises(IndicatorEvidenceError, match="mixes security identities"):
        derive_main_financial_indicator_evidence(mixed)

    with pytest.raises(IndicatorEvidenceError, match="differs from expected code"):
        derive_main_financial_indicator_evidence(_moutai_sample(), expected_code="600000")

    with pytest.raises(IndicatorEvidenceError, match="differs from expected market"):
        derive_main_financial_indicator_evidence(_moutai_sample(), expected_market="SZ")


def test_rejects_inconsistent_duplicate_revenue_sources():
    financials = _moutai_sample()
    financials["revenue_history"][1]["TOTAL_OPERATE_INCOME"] += 1_000_000

    with pytest.raises(IndicatorEvidenceError, match="TOTAL_OPERATE_INCOME histories"):
        derive_main_financial_indicator_evidence(financials)


def test_empty_indicator_history_returns_structured_missing_evidence():
    result = derive_main_financial_indicator_evidence(
        {
            "indicators": [],
            "revenue_history": [],
            "income_history": [],
            "cashflow": [],
        },
        expected_code="600519",
        expected_market="SH",
    )

    assert result["status"] == "missing"
    assert result["eligible"] is True
    assert result["security_code"] == "600519"
    assert result["market"] == "SH"
    assert result["as_of"] is None
    assert result["period_count"] == 0
    assert result["coverage"] == {
        "metric_count": 13,
        "complete_metric_count": 0,
        "partial_metric_count": 0,
        "missing_metric_count": 13,
    }
    assert all(metric["status"] == "missing" for metric in result["metrics"].values())


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("TOTAL_SHARE", 1.5, "integer count"),
        ("STAFF_NUM", -1, "must not be negative"),
        ("RDEXPEND", -0.01, "must not be negative"),
    ],
)
def test_rejects_invalid_amount_and_count_domains(field, bad_value, message):
    financials = _moutai_sample()
    financials["indicators"][0][field] = bad_value

    with pytest.raises(IndicatorEvidenceError, match=message):
        derive_main_financial_indicator_evidence(financials)


def test_rejects_nonannual_or_unprovenanced_indicator_rows():
    nonannual = _moutai_sample()
    nonannual["indicators"][1]["REPORT_DATE"] = "2025-09-30"
    nonannual["indicators"][1]["REPORT_DATE_NAME"] = "2025三季报"
    with pytest.raises(IndicatorEvidenceError, match="completed annual"):
        derive_main_financial_indicator_evidence(nonannual)

    wrong_source = _moutai_sample()
    wrong_source["indicators"][1]["SOURCE_REPORT_NAME"] = "unknown"
    with pytest.raises(IndicatorEvidenceError, match="unexpected source"):
        derive_main_financial_indicator_evidence(wrong_source)

    impossible_notice = _moutai_sample()
    impossible_notice["indicators"][1]["NOTICE_DATE"] = "2025-01-01"
    with pytest.raises(IndicatorEvidenceError, match="NOTICE_DATE precedes"):
        derive_main_financial_indicator_evidence(impossible_notice)
