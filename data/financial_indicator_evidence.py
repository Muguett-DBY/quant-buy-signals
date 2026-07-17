"""Auditable quantitative evidence derived from Eastmoney annual indicators.

The main-financial dataset exposes values in mixed units.  In particular,
``RDEXPEND`` is an amount (not an R&D ratio) and ``KCFJCXSYJLR`` is adjusted
net profit (not operating cash flow divided by net profit).  This module keeps
those source meanings explicit, normalises percentage fields to decimal
ratios, and calculates only formula-backed derivatives.

The public result deliberately contains no 0-10 qualitative score.  A scoring
layer can consume ``metrics[metric_name]["latest_value"]`` together with its
coverage, trend, stability and observation-level provenance without treating
missing evidence as zero.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from functools import lru_cache
from statistics import fmean, median
from typing import Any

from data.datacenter import (
    FINANCIAL_SECTOR_INDICATOR_FIELDS,
    GENERAL_MAIN_FINANCIAL_INDICATOR_METRICS,
    MAIN_FINANCIAL_INDICATOR_METRICS,
    RPT_CASHFLOW,
    RPT_INCOME,
    RPT_MAIN_FINANCIAL_INDICATORS,
)


class IndicatorEvidenceError(ValueError):
    """Annual indicator evidence is malformed or internally inconsistent."""


class UnsupportedIndicatorMarketError(IndicatorEvidenceError):
    """The security is outside the supported Shanghai/Shenzhen scope."""


_SECURITY_CODE = re.compile(r"\d{6}")
_SUPPORTED_MARKETS = {"SH", "SZ"}
_SUPPORTED_MARKET_PREFIXES = {"SH": ("6",), "SZ": ("0", "3")}
_SUPPORTED_FINANCIAL_INDUSTRIES = frozenset(FINANCIAL_SECTOR_INDICATOR_FIELDS)
_PERCENT_SCALE = 0.01

# ``definition`` is part of the API: it prevents ambiguous Eastmoney field
# names from silently acquiring a different financial meaning in the scorer.
_RAW_METRIC_SPECS: dict[str, dict[str, Any]] = {
    "rd_expense": {
        "raw_field": "RDEXPEND",
        "unit": "CNY",
        "raw_unit": "CNY",
        "scale": 1.0,
        "nonnegative": True,
        "integer": False,
        "definition": "研发费用金额；不是研发强度，研发强度另以研发费用/营业收入计算。",
    },
    "roic": {
        "raw_field": "ROIC",
        "unit": "ratio",
        "raw_unit": "percent",
        "scale": _PERCENT_SCALE,
        "nonnegative": False,
        "integer": False,
        "definition": "投入资本回报率，Eastmoney 百分数除以100后的比例。",
    },
    "weighted_roe": {
        "raw_field": "ROEJQ",
        "unit": "ratio",
        "raw_unit": "percent",
        "scale": _PERCENT_SCALE,
        "nonnegative": False,
        "integer": False,
        "definition": "加权净资产收益率，Eastmoney 百分数除以100后的比例。",
    },
    "gross_margin": {
        "raw_field": "XSMLL",
        "unit": "ratio",
        "raw_unit": "percent",
        "scale": _PERCENT_SCALE,
        "nonnegative": False,
        "integer": False,
        "definition": "销售毛利率，Eastmoney 百分数除以100后的比例。",
    },
    "net_margin": {
        "raw_field": "XSJLL",
        "unit": "ratio",
        "raw_unit": "percent",
        "scale": _PERCENT_SCALE,
        "nonnegative": False,
        "integer": False,
        "definition": "销售净利率，Eastmoney 百分数除以100后的比例。",
    },
    "tax_rate": {
        "raw_field": "TAXRATE",
        "unit": "ratio",
        "raw_unit": "percent",
        "scale": _PERCENT_SCALE,
        "nonnegative": False,
        "integer": False,
        "definition": "所得税税率，Eastmoney 百分数除以100后的比例。",
    },
    "total_shares": {
        "raw_field": "TOTAL_SHARE",
        "unit": "shares",
        "raw_unit": "shares",
        "scale": 1.0,
        "nonnegative": True,
        "integer": True,
        "definition": "期末总股本股数；趋势可用于识别增发或回购，但本身不是治理评分。",
    },
    "staff_count": {
        "raw_field": "STAFF_NUM",
        "unit": "persons",
        "raw_unit": "persons",
        "scale": 1.0,
        "nonnegative": True,
        "integer": True,
        "definition": "期末员工人数；只提供规模与趋势证据。",
    },
    "adjusted_net_profit": {
        "raw_field": "KCFJCXSYJLR",
        "unit": "CNY",
        "raw_unit": "CNY",
        "scale": 1.0,
        "nonnegative": False,
        "integer": False,
        "definition": "扣除非经常性损益后的归母净利润；该字段不是经营现金流/净利润。",
    },
    "interest_bearing_debt_ratio": {
        "raw_field": "INTEREST_DEBT_RATIO",
        "unit": "ratio",
        "raw_unit": "percent",
        "scale": _PERCENT_SCALE,
        "nonnegative": False,
        "integer": False,
        "definition": "带息债务比例，Eastmoney 百分数除以100后的比例。",
    },
}

_SECTOR_RAW_METRIC_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "BANK": {
        "net_interest_margin": {
            "raw_field": "NET_INTEREST_MARGIN",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "银行净息差，Eastmoney 标准化百分数除以100后的比例。",
        },
        "net_interest_spread": {
            "raw_field": "NET_INTEREST_SPREAD",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "银行净利差，Eastmoney 标准化百分数除以100后的比例。",
        },
        "capital_adequacy_ratio": {
            "raw_field": "NEWCAPITALADER",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "银行资本充足率；数据商标准化字段，不推定单家银行附加资本要求。",
        },
        "tier1_capital_adequacy_ratio": {
            "raw_field": "FIRST_ADEQUACY_RATIO",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "银行一级资本充足率；数据商标准化字段。",
        },
        "nonperforming_loan_ratio": {
            "raw_field": "NONPERLOAN",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": True,
            "integer": False,
            "definition": "银行不良贷款率，Eastmoney 标准化百分数除以100后的比例。",
        },
        "loan_provision_ratio": {
            "raw_field": "LOAN_PROVISION_RATIO",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": True,
            "integer": False,
            "definition": "贷款拨备率，Eastmoney 标准化百分数除以100后的比例。",
        },
        "total_deposits": {
            "raw_field": "TOTALDEPOSITS",
            "unit": "CNY",
            "raw_unit": "CNY",
            "scale": 1.0,
            "nonnegative": True,
            "integer": False,
            "definition": "期末存款总额；存款是银行经营负债，不作为工业企业净债务。",
        },
        "gross_loans": {
            "raw_field": "GROSSLOANS",
            "unit": "CNY",
            "raw_unit": "CNY",
            "scale": 1.0,
            "nonnegative": True,
            "integer": False,
            "definition": "期末贷款总额；用于银行专属存贷比证据。",
        },
        "loan_advances": {
            "raw_field": "LOAN_ADVANCES",
            "unit": "CNY",
            "raw_unit": "CNY",
            "scale": 1.0,
            "nonnegative": True,
            "integer": False,
            "definition": "贷款及垫款净额；不与贷款总额或存款总额混作同一字段。",
        },
    },
    "INSURANCE": {
        "solvency_adequacy_ratio": {
            "raw_field": "SOLVENCY_AR",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "保险综合偿付能力充足率，Eastmoney 标准化百分数除以100后的比例。",
        },
        "new_business_value_margin": {
            "raw_field": "NBV_RATE",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "寿险新业务价值率；直接保留数据商口径，不以保费自行替代其分母。",
        },
        "new_business_value": {
            "raw_field": "NBV_LIFE",
            "unit": "CNY",
            "raw_unit": "CNY",
            "scale": 1.0,
            "nonnegative": False,
            "integer": False,
            "definition": "寿险新业务价值金额；允许真实负值，不把负值改写为缺失。",
        },
        "earned_premium": {
            "raw_field": "EARNED_PREMIUM",
            "unit": "CNY",
            "raw_unit": "CNY",
            "scale": 1.0,
            "nonnegative": True,
            "integer": False,
            "definition": "已赚保费金额；与新业务价值率的原始分母并不等同。",
        },
        "life_surrender_rate": {
            "raw_field": "SURRENDER_RATE_LIFE",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": True,
            "integer": False,
            "definition": "寿险退保率，Eastmoney 标准化百分数除以100后的比例。",
        },
    },
    "SECURITIES": {
        "capital_leverage_ratio": {
            "raw_field": "CAPITAL_LEVERAGE_RATIO",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "证券公司资本杠杆率，Eastmoney 标准化百分数除以100后的比例。",
        },
        "capital_provisions_sum": {
            "raw_field": "CAPITAL_PROVISIONS_SUM",
            "unit": "CNY",
            "raw_unit": "CNY",
            "scale": 1.0,
            "nonnegative": True,
            "integer": False,
            "definition": "各项风险资本准备之和；金额字段，不是比率。",
        },
        "liquidity_coverage_ratio": {
            "raw_field": "LIQUIDITY_COVERAGE_RATIO",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "证券公司流动性覆盖率，Eastmoney 标准化百分数除以100后的比例。",
        },
        "net_capital_to_liabilities_ratio": {
            "raw_field": "NET_CAPITAL_LIABILITIES",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "证券公司净资本/负债比率，保留数据商标准化口径。",
        },
        "proprietary_capital_ratio": {
            "raw_field": "PROPRIETARY_CAPITAL",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "证券公司净资本相关标准化比率；不在字段名称之外推断分母。",
        },
        "risk_coverage_ratio": {
            "raw_field": "RISK_COVERAGE",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "证券公司风险覆盖率，Eastmoney 标准化百分数除以100后的比例。",
        },
        "net_stable_funding_ratio": {
            "raw_field": "NET_FUNDING_RATIO",
            "unit": "ratio",
            "raw_unit": "percent",
            "scale": _PERCENT_SCALE,
            "nonnegative": False,
            "integer": False,
            "definition": "证券公司净稳定资金率，Eastmoney 标准化百分数除以100后的比例。",
        },
    },
}

_DERIVED_METRIC_SPECS: dict[str, dict[str, str]] = {
    "rd_intensity": {
        "unit": "ratio",
        "formula": "RDEXPEND / TOTAL_OPERATE_INCOME",
        "definition": "同一年度研发费用占营业总收入比例。",
    },
    "operating_cashflow_to_net_profit": {
        "unit": "ratio",
        "formula": "NETCASH_OPERATE / PARENT_NETPROFIT",
        "definition": "同一年度经营活动现金流净额/归母净利润；不使用KCFJCXSYJLR代替现金流。",
    },
    "adjusted_net_profit_to_net_profit": {
        "unit": "ratio",
        "formula": "KCFJCXSYJLR / PARENT_NETPROFIT",
        "definition": "同一年度扣非归母净利润/归母净利润，用于量化非经常性损益影响。",
    },
}

_SECTOR_DERIVED_METRIC_SPECS: dict[str, dict[str, dict[str, str]]] = {
    "BANK": {
        "loan_provision_coverage_proxy": {
            "unit": "ratio",
            "formula": "LOAN_PROVISION_RATIO / NONPERLOAN",
            "definition": "贷款拨备率/不良贷款率的可复算覆盖代理；不冒充银行披露的正式拨备覆盖率字段。",
        },
        "gross_loan_to_deposit_ratio": {
            "unit": "ratio",
            "formula": "GROSSLOANS / TOTALDEPOSITS",
            "definition": "同一年度贷款总额/存款总额；不使用贷款净额替代分子。",
        },
    },
}


def _applicable_raw_metric_specs(industry_code: str | None) -> dict[str, dict[str, Any]]:
    specs = dict(_RAW_METRIC_SPECS)
    if industry_code in _SUPPORTED_FINANCIAL_INDUSTRIES:
        specs.update(_SECTOR_RAW_METRIC_SPECS[str(industry_code)])
    return specs


def _normalise_expected_identity(
    expected_code: str | None, expected_market: str | None
) -> tuple[str | None, str | None]:
    code = None if expected_code is None else str(expected_code).strip()
    if code is not None and not _SECURITY_CODE.fullmatch(code):
        raise IndicatorEvidenceError(f"invalid expected security code: {expected_code!r}")
    market = None if expected_market is None else str(expected_market).strip().upper()
    if market == "BJ":
        raise UnsupportedIndicatorMarketError("Beijing Stock Exchange securities are excluded")
    if market is not None and market not in _SUPPORTED_MARKETS:
        raise UnsupportedIndicatorMarketError(f"unsupported indicator market: {market or '<empty>'}")
    if code is not None and market is None and not code.startswith(("0", "3", "6")):
        raise UnsupportedIndicatorMarketError(f"security code {code} is outside the Shanghai/Shenzhen A-share scope")
    if code is not None and market is not None and not code.startswith(_SUPPORTED_MARKET_PREFIXES[market]):
        raise UnsupportedIndicatorMarketError(f"security code {code} is incompatible with market {market}")
    return code, market


@lru_cache(maxsize=8192)
def _parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def _annual_date(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise IndicatorEvidenceError(f"{context} REPORT_DATE must be an ISO date string")
    report_date = value.strip()
    try:
        parsed = _parse_iso_date(report_date)
    except ValueError as exc:
        raise IndicatorEvidenceError(f"{context} contains an invalid REPORT_DATE: {value!r}") from exc
    if parsed.isoformat() != report_date or not report_date.endswith("-12-31"):
        raise IndicatorEvidenceError(f"{context} must contain completed annual 12-31 reports")
    return report_date


def _optional_iso_date(value: Any, *, context: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise IndicatorEvidenceError(f"{context} must be an ISO date string or missing")
    text = value.strip()
    try:
        parsed = _parse_iso_date(text)
    except ValueError as exc:
        raise IndicatorEvidenceError(f"{context} contains an invalid date: {value!r}") from exc
    if parsed.isoformat() != text:
        raise IndicatorEvidenceError(f"{context} contains a non-canonical date: {value!r}")
    return text


def _record_sequence(value: Any, *, context: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise IndicatorEvidenceError(f"{context} must be a sequence of mappings")
    records: list[Mapping[str, Any]] = []
    for index, record in enumerate(value):
        if not isinstance(record, Mapping):
            raise IndicatorEvidenceError(f"{context}[{index}] must be a mapping")
        records.append(record)
    return records


def _validate_ordered_annual_records(value: Any, *, context: str) -> list[tuple[str, Mapping[str, Any]]]:
    records = _record_sequence(value, context=context)
    dated = [
        (_annual_date(record.get("REPORT_DATE"), context=f"{context}[{index}]"), record)
        for index, record in enumerate(records)
    ]
    dates = [report_date for report_date, _record in dated]
    duplicate_dates = sorted(report_date for report_date, count in Counter(dates).items() if count > 1)
    if duplicate_dates:
        raise IndicatorEvidenceError(f"{context} contains duplicate annual report dates: {duplicate_dates}")
    if dates != sorted(dates):
        raise IndicatorEvidenceError(f"{context} must be ordered oldest-to-newest")
    return dated


def _finite_number(value: Any, *, context: str) -> float | None:
    # Missing is only explicit None.  NaN is rejected rather than silently
    # collapsing into missing; zero remains a valid finite observation.
    if value is None:
        return None
    if isinstance(value, (bool, str, bytes, bytearray)):
        raise IndicatorEvidenceError(f"{context} must be a finite numeric value or None")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IndicatorEvidenceError(f"{context} must be a finite numeric value or None") from exc
    if not math.isfinite(parsed):
        raise IndicatorEvidenceError(f"{context} contains NaN or infinity")
    return parsed


def _validate_indicator_records(
    value: Any,
    *,
    expected_code: str | None,
    expected_market: str | None,
    required_source_fields: set[str],
) -> tuple[list[tuple[str, Mapping[str, Any]]], str | None, str | None]:
    dated = _validate_ordered_annual_records(value, context="indicators")
    if not dated:
        return dated, expected_code, expected_market

    identities: set[tuple[str, str]] = set()
    for index, (report_date, record) in enumerate(dated):
        missing_fields = sorted(required_source_fields - set(record))
        if missing_fields:
            raise IndicatorEvidenceError(f"indicators[{index}] omitted source fields: {missing_fields}")
        secucode_raw = record.get("SECUCODE")
        if not isinstance(secucode_raw, str):
            raise IndicatorEvidenceError(f"indicators[{index}] contains an invalid SECUCODE")
        parts = secucode_raw.strip().rsplit(".", 1)
        if len(parts) != 2 or not _SECURITY_CODE.fullmatch(parts[0]):
            raise IndicatorEvidenceError(f"indicators[{index}] contains an invalid SECUCODE")
        code, market = parts[0], parts[1].upper()
        if market == "BJ":
            raise UnsupportedIndicatorMarketError("Beijing Stock Exchange securities are excluded")
        if market not in _SUPPORTED_MARKETS:
            raise UnsupportedIndicatorMarketError(f"unsupported indicator market: {market or '<empty>'}")
        if not code.startswith(_SUPPORTED_MARKET_PREFIXES[market]):
            raise UnsupportedIndicatorMarketError(f"security code {code} is incompatible with market {market}")
        identities.add((code, market))

        if str(record.get("REPORT_TYPE") or "").strip() != "年报":
            raise IndicatorEvidenceError(f"indicators[{index}] is not an annual report")
        report_year = report_date[:4]
        if str(record.get("REPORT_YEAR") or "").strip() != report_year:
            raise IndicatorEvidenceError(f"indicators[{index}] REPORT_YEAR differs from REPORT_DATE")
        if str(record.get("REPORT_DATE_NAME") or "").strip() != f"{report_year}年报":
            raise IndicatorEvidenceError(f"indicators[{index}] REPORT_DATE_NAME differs from REPORT_DATE")
        if str(record.get("SOURCE_REPORT_NAME") or "").strip() != RPT_MAIN_FINANCIAL_INDICATORS:
            raise IndicatorEvidenceError(f"indicators[{index}] contains an unexpected source report")
        notice_date = _optional_iso_date(record.get("NOTICE_DATE"), context=f"indicators[{index}] NOTICE_DATE")
        if notice_date is not None and notice_date < report_date:
            raise IndicatorEvidenceError(f"indicators[{index}] NOTICE_DATE precedes REPORT_DATE")

    if len(identities) != 1:
        raise IndicatorEvidenceError(f"indicator history mixes security identities: {sorted(identities)}")
    actual_code, actual_market = next(iter(identities))
    if expected_code is not None and actual_code != expected_code:
        raise IndicatorEvidenceError(f"indicator code {actual_code} differs from expected code {expected_code}")
    if expected_market is not None and actual_market != expected_market:
        raise IndicatorEvidenceError(f"indicator market {actual_market} differs from expected market {expected_market}")
    return dated, actual_code, actual_market


def _raw_observations(
    dated: list[tuple[str, Mapping[str, Any]]],
    *,
    metric_name: str,
    spec: Mapping[str, Any],
    notice_dates: Sequence[str | None] | None = None,
) -> list[dict[str, Any]]:
    raw_field = str(spec["raw_field"])
    observations: list[dict[str, Any]] = []
    for index, (report_date, record) in enumerate(dated):
        raw_value = _finite_number(record.get(raw_field), context=f"indicators[{index}].{raw_field}")
        if raw_value is not None and spec["nonnegative"] and raw_value < 0:
            raise IndicatorEvidenceError(f"indicators[{index}].{raw_field} must not be negative")
        if raw_value is not None and spec["integer"] and not raw_value.is_integer():
            raise IndicatorEvidenceError(f"indicators[{index}].{raw_field} must be an integer count")
        value: float | int | None
        if raw_value is None:
            value = None
        elif spec["integer"]:
            value = int(raw_value)
        else:
            value = raw_value * float(spec["scale"])
        observations.append(
            {
                "report_date": report_date,
                "notice_date": (
                    notice_dates[index]
                    if notice_dates is not None
                    else _optional_iso_date(record.get("NOTICE_DATE"), context=f"indicators[{index}] NOTICE_DATE")
                ),
                "value": value,
                "raw_value": raw_value,
                "is_missing": value is None,
                "missing_reason": "source_value_missing" if value is None else None,
                "evidence_type": "missing" if value is None else "provider_standardized",
                "evidence_label": "缺失" if value is None else "数据商标准化字段",
                "source_report": RPT_MAIN_FINANCIAL_INDICATORS,
                "source_field": raw_field,
                "metric": metric_name,
            }
        )
    return observations


def _annual_value_map(value: Any, *, history_name: str, field: str) -> dict[str, float | None]:
    dated = _validate_ordered_annual_records(value, context=history_name)
    return _annual_value_map_from_dated(dated, history_name=history_name, field=field)


def _annual_value_map_from_dated(
    dated: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    history_name: str,
    field: str,
) -> dict[str, float | None]:
    return {
        report_date: _finite_number(record.get(field), context=f"{history_name}[{index}].{field}")
        for index, (report_date, record) in enumerate(dated)
    }


def _merge_matching_maps(
    primary: Mapping[str, float | None],
    secondary: Mapping[str, float | None],
    *,
    context: str,
) -> dict[str, float | None]:
    merged: dict[str, float | None] = {}
    for report_date in sorted(set(primary) | set(secondary)):
        first = primary.get(report_date)
        second = secondary.get(report_date)
        if first is not None and second is not None and not math.isclose(first, second, rel_tol=1e-10, abs_tol=1e-6):
            raise IndicatorEvidenceError(f"{context} disagrees for {report_date}: {first} != {second}")
        merged[report_date] = first if first is not None else second
    return merged


def _ratio_observations(
    periods: list[str],
    *,
    metric_name: str,
    numerator: Mapping[str, float | int | None],
    denominator: Mapping[str, float | int | None],
    numerator_source: tuple[str, str],
    denominator_source: tuple[str, str],
    positive_denominator: bool,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for report_date in periods:
        numerator_value = numerator.get(report_date)
        denominator_value = denominator.get(report_date)
        missing_reason = None
        if numerator_value is None:
            missing_reason = "numerator_missing"
        elif denominator_value is None:
            missing_reason = "denominator_missing"
        elif denominator_value == 0:
            missing_reason = "denominator_zero"
        elif positive_denominator and denominator_value < 0:
            missing_reason = "denominator_nonpositive"
        value = None if missing_reason is not None else float(numerator_value) / float(denominator_value)
        if value is not None and not math.isfinite(value):
            raise IndicatorEvidenceError(f"derived metric {metric_name} is non-finite for {report_date}")
        observations.append(
            {
                "report_date": report_date,
                "notice_date": None,
                "value": value,
                "raw_value": None,
                "is_missing": value is None,
                "missing_reason": missing_reason,
                "evidence_type": "missing" if value is None else "derived_calculation",
                "evidence_label": "缺失" if value is None else "公式推导",
                "source_report": "derived",
                "source_field": None,
                "metric": metric_name,
                "components": {
                    "numerator": {
                        "value": numerator_value,
                        "source_report": numerator_source[0],
                        "source_field": numerator_source[1],
                    },
                    "denominator": {
                        "value": denominator_value,
                        "source_report": denominator_source[0],
                        "source_field": denominator_source[1],
                    },
                },
            }
        )
    return observations


def _trend(valid: list[dict[str, Any]]) -> dict[str, Any]:
    if len(valid) < 2:
        return {
            "sample_count": len(valid),
            "first_date": valid[0]["report_date"] if valid else None,
            "last_date": valid[-1]["report_date"] if valid else None,
            "absolute_change": None,
            "relative_change": None,
            "cagr": None,
            "slope_per_year": None,
            "direction": "insufficient_history",
        }
    years = [int(observation["report_date"][:4]) for observation in valid]
    values = [float(observation["value"]) for observation in valid]
    mean_year = fmean(years)
    mean_value = fmean(values)
    denominator = math.fsum((year - mean_year) ** 2 for year in years)
    slope = (
        math.fsum((year - mean_year) * (value - mean_value) for year, value in zip(years, values, strict=True))
        / denominator
        if denominator > 0
        else None
    )
    elapsed = years[-1] - years[0]
    absolute_change = values[-1] - values[0]
    relative_change = absolute_change / abs(values[0]) if values[0] != 0 else None
    cagr = (
        (values[-1] / values[0]) ** (1.0 / elapsed) - 1.0 if elapsed > 0 and values[0] > 0 and values[-1] > 0 else None
    )
    direction = "rising" if absolute_change > 0 else "falling" if absolute_change < 0 else "flat"
    return {
        "sample_count": len(valid),
        "first_date": valid[0]["report_date"],
        "last_date": valid[-1]["report_date"],
        "absolute_change": absolute_change,
        "relative_change": relative_change,
        "cagr": cagr,
        "slope_per_year": slope,
        "direction": direction,
    }


def _stability(valid: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(observation["value"]) for observation in valid]
    if not values:
        return {
            "sample_count": 0,
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
            "range": None,
            "population_stddev": None,
            "coefficient_of_variation": None,
        }
    mean_value = fmean(values)
    # ``statistics.pstdev`` converts float samples through exact Fraction
    # arithmetic.  That is valuable for mixed numeric types but was the single
    # largest CPU cost in the 5,200-company production screen.  These inputs
    # have already been validated and normalized to finite floats, so the
    # direct population-variance identity is equivalent and deterministic.
    variance = math.fsum((value - mean_value) ** 2 for value in values) / len(values)
    standard_deviation = math.sqrt(max(0.0, variance))
    return {
        "sample_count": len(values),
        "mean": mean_value,
        "median": median(values),
        "minimum": min(values),
        "maximum": max(values),
        "range": max(values) - min(values),
        "population_stddev": standard_deviation,
        "coefficient_of_variation": standard_deviation / abs(mean_value) if mean_value != 0 else None,
    }


def _summarise_metric(
    *,
    metric_name: str,
    unit: str,
    definition: str,
    observations: list[dict[str, Any]],
    raw_field: str | None = None,
    raw_unit: str | None = None,
    formula: str | None = None,
) -> dict[str, Any]:
    valid = [observation for observation in observations if observation["value"] is not None]
    missing_reasons = Counter(
        str(observation["missing_reason"]) for observation in observations if observation["missing_reason"] is not None
    )
    status = "missing" if not valid else "complete" if len(valid) == len(observations) else "partial"
    latest = dict(observations[-1]) if observations else None
    latest_available = dict(valid[-1]) if valid else None
    evidence_types = sorted({str(observation.get("evidence_type") or "missing") for observation in observations})
    return {
        "metric": metric_name,
        "status": status,
        "definition": definition,
        "unit": unit,
        "raw_field": raw_field,
        "raw_unit": raw_unit,
        "formula": formula,
        "period_count": len(observations),
        "sample_count": len(valid),
        "missing_count": len(observations) - len(valid),
        "missing_by_reason": dict(sorted(missing_reasons.items())),
        "evidence_types": evidence_types,
        # latest_value intentionally follows the latest period.  A missing
        # latest period is never silently replaced by an older observation.
        "latest_date": latest["report_date"] if latest is not None else None,
        "latest_value": latest["value"] if latest is not None else None,
        "latest": latest,
        "latest_available_date": latest_available["report_date"] if latest_available is not None else None,
        "latest_available_value": latest_available["value"] if latest_available is not None else None,
        "latest_available": latest_available,
        "trend": _trend(valid),
        "stability": _stability(valid),
        "observations": observations,
    }


def derive_main_financial_indicator_evidence(
    financials: Mapping[str, Any],
    *,
    expected_code: str | None = None,
    expected_market: str | None = None,
    industry_code: str | None = None,
) -> dict[str, Any]:
    """Build formula-backed annual evidence for one Shanghai/Shenzhen stock.

    ``financials`` is one company entry returned by
    :meth:`data.fetcher.DataFetcher.get_financials`.  Records must already be
    unique and oldest-to-newest.  Corruption (including NaN/Inf, duplicate or
    out-of-order periods, mixed identities and unexpected source metadata)
    raises :class:`IndicatorEvidenceError` instead of producing a plausible
    score.  Beijing Stock Exchange identities raise
    :class:`UnsupportedIndicatorMarketError` and therefore cannot enter the
    scoring path.
    """
    if not isinstance(financials, Mapping):
        raise IndicatorEvidenceError("financials must be a mapping")
    normalized_industry = str(industry_code or "").strip().upper() or None
    raw_metric_specs = _applicable_raw_metric_specs(normalized_industry)
    required_source_fields = {str(spec["raw_field"]) for spec in raw_metric_specs.values()}
    if not set(GENERAL_MAIN_FINANCIAL_INDICATOR_METRICS).issubset(required_source_fields):
        raise AssertionError("general annual indicator contract is incomplete")
    if not required_source_fields.issubset(set(MAIN_FINANCIAL_INDICATOR_METRICS)):
        raise AssertionError("indicator evidence requests fields outside the datacenter contract")
    code, market = _normalise_expected_identity(expected_code, expected_market)
    indicator_records, code, market = _validate_indicator_records(
        financials.get("indicators", []),
        expected_code=code,
        expected_market=market,
        required_source_fields=required_source_fields,
    )
    periods = [report_date for report_date, _record in indicator_records]
    indicator_notice_dates = [
        _optional_iso_date(record.get("NOTICE_DATE"), context=f"indicators[{index}] NOTICE_DATE")
        for index, (_report_date, record) in enumerate(indicator_records)
    ]

    metrics: dict[str, dict[str, Any]] = {}
    for metric_name, spec in raw_metric_specs.items():
        metrics[metric_name] = _summarise_metric(
            metric_name=metric_name,
            unit=str(spec["unit"]),
            definition=str(spec["definition"]),
            observations=_raw_observations(
                indicator_records,
                metric_name=metric_name,
                spec=spec,
                notice_dates=indicator_notice_dates,
            ),
            raw_field=str(spec["raw_field"]),
            raw_unit=str(spec["raw_unit"]),
            formula=(f"{spec['raw_field']} / 100" if spec["scale"] == _PERCENT_SCALE else None),
        )

    revenue_records = _validate_ordered_annual_records(financials.get("revenue_history", []), context="revenue_history")
    income_records = _validate_ordered_annual_records(financials.get("income_history", []), context="income_history")
    cashflow_records = _validate_ordered_annual_records(financials.get("cashflow", []), context="cashflow")
    revenue_primary = _annual_value_map_from_dated(
        revenue_records,
        history_name="revenue_history",
        field="TOTAL_OPERATE_INCOME",
    )
    revenue_from_income = _annual_value_map_from_dated(
        income_records,
        history_name="income_history",
        field="TOTAL_OPERATE_INCOME",
    )
    revenue = _merge_matching_maps(
        revenue_primary,
        revenue_from_income,
        context="annual TOTAL_OPERATE_INCOME histories",
    )
    net_profit = _annual_value_map_from_dated(
        income_records,
        history_name="income_history",
        field="PARENT_NETPROFIT",
    )
    operating_cashflow = _annual_value_map_from_dated(
        cashflow_records,
        history_name="cashflow",
        field="NETCASH_OPERATE",
    )
    rd_expense = {
        observation["report_date"]: observation["value"] for observation in metrics["rd_expense"]["observations"]
    }
    adjusted_net_profit = {
        observation["report_date"]: observation["value"]
        for observation in metrics["adjusted_net_profit"]["observations"]
    }

    derived_inputs = {
        "rd_intensity": _ratio_observations(
            periods,
            metric_name="rd_intensity",
            numerator=rd_expense,
            denominator=revenue,
            numerator_source=(RPT_MAIN_FINANCIAL_INDICATORS, "RDEXPEND"),
            denominator_source=(RPT_INCOME, "TOTAL_OPERATE_INCOME"),
            positive_denominator=True,
        ),
        "operating_cashflow_to_net_profit": _ratio_observations(
            periods,
            metric_name="operating_cashflow_to_net_profit",
            numerator=operating_cashflow,
            denominator=net_profit,
            numerator_source=(RPT_CASHFLOW, "NETCASH_OPERATE"),
            denominator_source=(RPT_INCOME, "PARENT_NETPROFIT"),
            positive_denominator=False,
        ),
        "adjusted_net_profit_to_net_profit": _ratio_observations(
            periods,
            metric_name="adjusted_net_profit_to_net_profit",
            numerator=adjusted_net_profit,
            denominator=net_profit,
            numerator_source=(RPT_MAIN_FINANCIAL_INDICATORS, "KCFJCXSYJLR"),
            denominator_source=(RPT_INCOME, "PARENT_NETPROFIT"),
            positive_denominator=False,
        ),
    }
    for metric_name, observations in derived_inputs.items():
        spec = _DERIVED_METRIC_SPECS[metric_name]
        metrics[metric_name] = _summarise_metric(
            metric_name=metric_name,
            unit=spec["unit"],
            definition=spec["definition"],
            observations=observations,
            formula=spec["formula"],
        )

    if normalized_industry == "BANK":
        bank_derived_inputs = {
            "loan_provision_coverage_proxy": _ratio_observations(
                periods,
                metric_name="loan_provision_coverage_proxy",
                numerator={
                    observation["report_date"]: observation["value"]
                    for observation in metrics["loan_provision_ratio"]["observations"]
                },
                denominator={
                    observation["report_date"]: observation["value"]
                    for observation in metrics["nonperforming_loan_ratio"]["observations"]
                },
                numerator_source=(RPT_MAIN_FINANCIAL_INDICATORS, "LOAN_PROVISION_RATIO"),
                denominator_source=(RPT_MAIN_FINANCIAL_INDICATORS, "NONPERLOAN"),
                positive_denominator=True,
            ),
            "gross_loan_to_deposit_ratio": _ratio_observations(
                periods,
                metric_name="gross_loan_to_deposit_ratio",
                numerator={
                    observation["report_date"]: observation["value"]
                    for observation in metrics["gross_loans"]["observations"]
                },
                denominator={
                    observation["report_date"]: observation["value"]
                    for observation in metrics["total_deposits"]["observations"]
                },
                numerator_source=(RPT_MAIN_FINANCIAL_INDICATORS, "GROSSLOANS"),
                denominator_source=(RPT_MAIN_FINANCIAL_INDICATORS, "TOTALDEPOSITS"),
                positive_denominator=True,
            ),
        }
        for metric_name, observations in bank_derived_inputs.items():
            spec = _SECTOR_DERIVED_METRIC_SPECS["BANK"][metric_name]
            metrics[metric_name] = _summarise_metric(
                metric_name=metric_name,
                unit=spec["unit"],
                definition=spec["definition"],
                observations=observations,
                formula=spec["formula"],
            )

    statuses = Counter(metric["status"] for metric in metrics.values())
    overall_status = (
        "missing" if not indicator_records else "complete" if statuses["complete"] == len(metrics) else "partial"
    )
    period_metadata = [
        {
            "report_date": report_date,
            "notice_date": indicator_notice_dates[index],
            "source_report": RPT_MAIN_FINANCIAL_INDICATORS,
        }
        for index, (report_date, _record) in enumerate(indicator_records)
    ]
    return {
        "schema_version": 2,
        "status": overall_status,
        "eligible": market in _SUPPORTED_MARKETS if market is not None else None,
        "security_code": code,
        "market": market,
        "industry_code": normalized_industry,
        "source_report": RPT_MAIN_FINANCIAL_INDICATORS,
        "as_of": periods[-1] if periods else None,
        "period_count": len(periods),
        "periods": period_metadata,
        "coverage": {
            "metric_count": len(metrics),
            "complete_metric_count": statuses["complete"],
            "partial_metric_count": statuses["partial"],
            "missing_metric_count": statuses["missing"],
        },
        "semantic_notes": [
            "RDEXPEND is an amount; rd_intensity is derived with annual revenue.",
            "KCFJCXSYJLR is adjusted net profit, not operating cash flow/net profit.",
            "No governance, patent, market-share or other qualitative score is inferred by this layer.",
            "Raw financial-sector fields are provider-standardized; derived ratios are labeled separately.",
            "Missing sector fields remain missing and are never converted to zero or a failed score.",
        ],
        "metrics": metrics,
    }


__all__ = [
    "IndicatorEvidenceError",
    "UnsupportedIndicatorMarketError",
    "derive_main_financial_indicator_evidence",
]
