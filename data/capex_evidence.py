"""Auditable capital-expenditure evidence for strict TTM reconstruction.

The compact Eastmoney cash-flow report occasionally leaves
``CONSTRUCT_LONG_ASSET`` blank even when the detailed statement proves that
the value is zero.  This module keeps reported facts separate from exact
derivations and never turns an unresolved blank into zero.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from data.financial_source_evidence import FinancialSourceEvidenceError, zero_capex_evidence


CAPEX_PROVENANCE_SCHEMA_VERSION = 1
STANDARD_CASHFLOW_REPORT = "RPT_DMSK_FN_CASHFLOW"
DETAILED_CASHFLOW_REPORT = "RPT_F10_FINANCE_GCASHFLOW"
EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
OFFICIAL_QUARTERLY_REPORT = "CNINFO_EXCHANGE_FILED_QUARTERLY_REPORT"
OFFICIAL_ANNUAL_REPORT = "CNINFO_EXCHANGE_FILED_ANNUAL_REPORT"

CAPEX_FIELD = "CONSTRUCT_LONG_ASSET"
NON_CAPEX_OUTFLOW_FIELDS = (
    "INVEST_PAY_CASH",
    "PLEDGE_LOAN_ADD",
    "OBTAIN_SUBSIDIARY_OTHER",
    "ADD_PLEDGE_TIMEDEPOSITS",
    "PAY_OTHER_INVEST",
    "INVEST_OUTFLOW_OTHER",
    "INVEST_OUTFLOW_BALANCE",
)
_NET_IDENTITY_FIELDS = (
    "TOTAL_INVEST_INFLOW",
    "INVEST_NETCASH_OTHER",
    "INVEST_NETCASH_BALANCE",
    "NETCASH_INVEST",
)
_DERIVATION_METHODS = {
    "detailed_outflow_residual_zero",
    "detailed_net_cash_identity_zero",
}


class CapexEvidenceConflictError(ValueError):
    """An official zero conflicts with a non-zero upstream capex fact."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _finite_float(value: Any) -> float | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    result = float(parsed)
    return result if math.isfinite(result) else None


def _close_decimal(left: Decimal, right: Decimal) -> bool:
    scale = max(abs(left), abs(right), Decimal(1))
    tolerance = max(Decimal("0.01"), scale * Decimal("1e-9"))
    return abs(left - right) <= tolerance


def _source_query(report_name: str, report_date: str) -> dict[str, str]:
    return {
        "report_name": report_name,
        "report_date": report_date,
        "source": "WEB",
        "client": "PC",
    }


def _reported_provenance(value: float, report_date: str, report_name: str) -> dict[str, Any]:
    return {
        "schema_version": CAPEX_PROVENANCE_SCHEMA_VERSION,
        "status": "complete",
        "evidence_label": "fact_source_reported",
        "value": value,
        "source_report": report_name,
        "source_field": CAPEX_FIELD,
        "report_date": report_date,
        "formula": "source_reported",
        "derivation_method": None,
        "components": {"reported_value": value},
        "source_null_fields": [],
        "source_url": EASTMONEY_DATACENTER_URL,
        "source_query": _source_query(report_name, report_date),
    }


def _missing_provenance(
    report_date: str,
    reason: str,
    *,
    detailed_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_report = DETAILED_CASHFLOW_REPORT if detailed_row is not None else STANDARD_CASHFLOW_REPORT
    return {
        "schema_version": CAPEX_PROVENANCE_SCHEMA_VERSION,
        "status": "missing",
        "evidence_label": "missing",
        "value": None,
        "source_report": source_report,
        "source_field": CAPEX_FIELD,
        "report_date": report_date,
        "formula": None,
        "derivation_method": None,
        "components": {},
        "source_null_fields": [],
        "source_url": EASTMONEY_DATACENTER_URL,
        "source_query": _source_query(source_report, report_date),
        "reason": reason,
    }


def _normalised_outflow_components(
    detailed_row: Mapping[str, Any],
) -> tuple[dict[str, Decimal], list[str], str | None]:
    values: dict[str, Decimal] = {}
    null_fields: list[str] = []
    for field in NON_CAPEX_OUTFLOW_FIELDS:
        raw = detailed_row.get(field)
        if raw is None or raw == "":
            values[field] = Decimal(0)
            null_fields.append(field)
            continue
        value = _decimal(raw)
        if value is None:
            return {}, [], f"invalid_detailed_component:{field}"
        if value < 0:
            return {}, [], f"negative_detailed_component:{field}"
        values[field] = value
    return values, null_fields, None


def _derived_zero_provenance(
    *,
    report_date: str,
    method: str,
    components: Mapping[str, Any],
    source_null_fields: list[str],
) -> dict[str, Any]:
    formulas = {
        "detailed_outflow_residual_zero": ("TOTAL_INVEST_OUTFLOW - SUM(reported non-capex investment outflows) = 0"),
        "detailed_net_cash_identity_zero": (
            "TOTAL_INVEST_INFLOW + INVEST_NETCASH_OTHER + INVEST_NETCASH_BALANCE "
            "- NETCASH_INVEST = TOTAL_INVEST_OUTFLOW = 0"
        ),
    }
    return {
        "schema_version": CAPEX_PROVENANCE_SCHEMA_VERSION,
        "status": "complete",
        "evidence_label": "derived_calculation",
        "value": 0.0,
        "source_report": DETAILED_CASHFLOW_REPORT,
        "source_field": CAPEX_FIELD,
        "report_date": report_date,
        "formula": formulas[method],
        "derivation_method": method,
        "components": dict(components),
        "source_null_fields": sorted(source_null_fields),
        "null_normalisation": (
            "unreported detailed outflow cells are zero only when the statement aggregate identity closes"
        ),
        "source_url": EASTMONEY_DATACENTER_URL,
        "source_query": _source_query(DETAILED_CASHFLOW_REPORT, report_date),
    }


def _official_zero_provenance(
    evidence: Mapping[str, Any],
    *,
    report_date: str,
    security_code: str,
) -> dict[str, Any]:
    return {
        "schema_version": CAPEX_PROVENANCE_SCHEMA_VERSION,
        "status": "complete",
        "evidence_label": "fact_official_report_zero",
        "evidence_type": evidence.get("evidence_type"),
        "value": 0.0,
        "security_code": security_code,
        "source_report": (OFFICIAL_ANNUAL_REPORT if report_date.endswith("-12-31") else OFFICIAL_QUARTERLY_REPORT),
        "source_field": CAPEX_FIELD,
        "report_date": report_date,
        "formula": "exchange_filed_statement_zero",
        "derivation_method": None,
        "components": {"reported_value": 0.0},
        "source_null_fields": [],
        "source_document": evidence.get("source_document"),
        "source_url": evidence.get("source_url"),
        "source_sha256": evidence.get("source_sha256"),
        "source_page": evidence.get("source_page"),
        "source_statement": evidence.get("source_statement"),
    }


def resolve_capex_evidence(
    standard_value: Any,
    detailed_row: Mapping[str, Any] | None,
    *,
    report_date: str,
    security_code: str | None = None,
    official_evidence: Mapping[str, Any] | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Resolve one canonical capex value without imputing an unknown blank.

    Resolution order is source-reported compact value, source-reported
    detailed value, a matching versioned exchange-filed zero, exact detailed
    outflow residual equal to zero, then the detailed net-investing-cash
    identity proving total outflow equal to zero. Positive residuals are
    deliberately not assigned to capex because another unreported outflow line
    could explain them.
    """
    official_zero = None
    if official_evidence is not None:
        official_zero = _finite_float(official_evidence.get("value"))
        if official_zero != 0.0 or not isinstance(security_code, str) or not security_code:
            raise CapexEvidenceConflictError("official capex evidence is malformed")

    reported = _finite_float(standard_value)
    if reported is not None:
        if official_zero is not None and not math.isclose(reported, official_zero, rel_tol=0.0, abs_tol=0.01):
            raise CapexEvidenceConflictError("official zero capex evidence conflicts with compact source value")
        return reported, _reported_provenance(reported, report_date, STANDARD_CASHFLOW_REPORT)
    if standard_value not in (None, ""):
        return None, _missing_provenance(report_date, "invalid_standard_value")
    if detailed_row is not None:
        detailed_raw = detailed_row.get(CAPEX_FIELD)
        detailed = _finite_float(detailed_raw)
        if detailed is not None:
            if official_zero is not None and not math.isclose(detailed, official_zero, rel_tol=0.0, abs_tol=0.01):
                raise CapexEvidenceConflictError("official zero capex evidence conflicts with detailed source value")
            return detailed, _reported_provenance(detailed, report_date, DETAILED_CASHFLOW_REPORT)
        if detailed_raw not in (None, ""):
            return None, _missing_provenance(
                report_date,
                "invalid_detailed_capex_value",
                detailed_row=detailed_row,
            )

    if official_evidence is not None and security_code is not None:
        return 0.0, _official_zero_provenance(
            official_evidence,
            report_date=report_date,
            security_code=security_code,
        )

    if detailed_row is None:
        return None, _missing_provenance(report_date, "detailed_statement_missing")

    outflows, null_fields, component_error = _normalised_outflow_components(detailed_row)
    if component_error is not None:
        return None, _missing_provenance(report_date, component_error, detailed_row=detailed_row)
    outflow_total_raw = detailed_row.get("TOTAL_INVEST_OUTFLOW")
    outflow_total = _decimal(outflow_total_raw)
    if outflow_total_raw not in (None, "") and outflow_total is None:
        return None, _missing_provenance(
            report_date,
            "invalid_detailed_component:TOTAL_INVEST_OUTFLOW",
            detailed_row=detailed_row,
        )
    if outflow_total is not None and outflow_total < 0:
        return None, _missing_provenance(
            report_date,
            "negative_detailed_component:TOTAL_INVEST_OUTFLOW",
            detailed_row=detailed_row,
        )

    non_capex_sum = sum(outflows.values(), Decimal(0))
    if outflow_total is not None and _close_decimal(outflow_total, non_capex_sum):
        components = {
            "total_invest_outflow": float(outflow_total),
            "non_capex_outflow_sum": float(non_capex_sum),
            "non_capex_outflows": {field: float(value) for field, value in outflows.items()},
        }
        return 0.0, _derived_zero_provenance(
            report_date=report_date,
            method="detailed_outflow_residual_zero",
            components=components,
            source_null_fields=null_fields,
        )

    net_values: dict[str, Decimal] = {}
    net_null_fields: list[str] = []
    for field in _NET_IDENTITY_FIELDS:
        raw = detailed_row.get(field)
        if raw is None or raw == "":
            if field in {"INVEST_NETCASH_OTHER", "INVEST_NETCASH_BALANCE"}:
                net_values[field] = Decimal(0)
                net_null_fields.append(field)
                continue
            return None, _missing_provenance(
                report_date,
                f"missing_detailed_component:{field}",
                detailed_row=detailed_row,
            )
        value = _decimal(raw)
        if value is None:
            return None, _missing_provenance(
                report_date,
                f"invalid_detailed_component:{field}",
                detailed_row=detailed_row,
            )
        net_values[field] = value

    solved_outflow = (
        net_values["TOTAL_INVEST_INFLOW"]
        + net_values["INVEST_NETCASH_OTHER"]
        + net_values["INVEST_NETCASH_BALANCE"]
        - net_values["NETCASH_INVEST"]
    )
    if _close_decimal(solved_outflow, Decimal(0)):
        components = {
            "total_invest_inflow": float(net_values["TOTAL_INVEST_INFLOW"]),
            "invest_netcash_other": float(net_values["INVEST_NETCASH_OTHER"]),
            "invest_netcash_balance": float(net_values["INVEST_NETCASH_BALANCE"]),
            "netcash_invest": float(net_values["NETCASH_INVEST"]),
            "solved_total_invest_outflow": float(solved_outflow),
            "non_capex_outflows": {field: float(value) for field, value in outflows.items()},
        }
        return 0.0, _derived_zero_provenance(
            report_date=report_date,
            method="detailed_net_cash_identity_zero",
            components=components,
            source_null_fields=[*null_fields, *net_null_fields],
        )

    return None, _missing_provenance(
        report_date,
        "detailed_statement_does_not_uniquely_determine_capex",
        detailed_row=detailed_row,
    )


def validate_capex_provenance(
    provenance: Any,
    *,
    expected_value: Any,
    expected_report_date: str,
) -> str:
    """Return ``complete`` only for internally reproducible capex evidence."""
    if not isinstance(provenance, Mapping):
        return "missing_capex_provenance"
    if provenance.get("schema_version") != CAPEX_PROVENANCE_SCHEMA_VERSION:
        return "invalid_capex_provenance"
    if provenance.get("status") != "complete":
        return "missing_component"
    if provenance.get("report_date") != expected_report_date:
        return "invalid_capex_provenance"
    value = _finite_float(expected_value)
    evidence_value = _finite_float(provenance.get("value"))
    if value is None or evidence_value is None or not math.isclose(value, evidence_value, rel_tol=1e-10, abs_tol=0.01):
        return "invalid_capex_provenance"
    label = provenance.get("evidence_label")
    source_report = provenance.get("source_report")
    if label == "fact_official_report_zero":
        code = provenance.get("security_code")
        expected_source_report = (
            OFFICIAL_ANNUAL_REPORT if expected_report_date.endswith("-12-31") else OFFICIAL_QUARTERLY_REPORT
        )
        if (
            not isinstance(code, str)
            or source_report != expected_source_report
            or provenance.get("source_field") != CAPEX_FIELD
            or provenance.get("formula") != "exchange_filed_statement_zero"
            or provenance.get("derivation_method") is not None
            or value != 0.0
        ):
            return "invalid_capex_provenance"
        try:
            committed = zero_capex_evidence().get((code, expected_report_date))
        except FinancialSourceEvidenceError:
            return "invalid_capex_provenance"
        if not isinstance(committed, Mapping):
            return "invalid_capex_provenance"
        for field in (
            "evidence_type",
            "source_document",
            "source_url",
            "source_sha256",
            "source_page",
            "source_statement",
        ):
            if provenance.get(field) != committed.get(field):
                return "invalid_capex_provenance"
        components = provenance.get("components")
        if not isinstance(components, Mapping) or _finite_float(components.get("reported_value")) != 0.0:
            return "invalid_capex_provenance"
        return "complete"

    if provenance.get("source_url") != EASTMONEY_DATACENTER_URL:
        return "invalid_capex_provenance"

    if label == "fact_source_reported":
        if (
            source_report not in {STANDARD_CASHFLOW_REPORT, DETAILED_CASHFLOW_REPORT}
            or provenance.get("source_field") != CAPEX_FIELD
            or provenance.get("formula") != "source_reported"
            or provenance.get("derivation_method") is not None
        ):
            return "invalid_capex_provenance"
        components = provenance.get("components")
        if not isinstance(components, Mapping):
            return "invalid_capex_provenance"
        reported = _finite_float(components.get("reported_value"))
        if reported is None or not math.isclose(reported, value, rel_tol=1e-10, abs_tol=0.01):
            return "invalid_capex_provenance"
        return "complete"

    if label != "derived_calculation" or source_report != DETAILED_CASHFLOW_REPORT or value != 0:
        return "invalid_capex_provenance"
    method = provenance.get("derivation_method")
    components = provenance.get("components")
    if method not in _DERIVATION_METHODS or not isinstance(components, Mapping):
        return "invalid_capex_provenance"

    non_capex = components.get("non_capex_outflows")
    if not isinstance(non_capex, Mapping) or set(non_capex) != set(NON_CAPEX_OUTFLOW_FIELDS):
        return "invalid_capex_provenance"
    parsed_outflows = [_decimal(non_capex.get(field)) for field in NON_CAPEX_OUTFLOW_FIELDS]
    if any(item is None or item < 0 for item in parsed_outflows):
        return "invalid_capex_provenance"

    if method == "detailed_outflow_residual_zero":
        total = _decimal(components.get("total_invest_outflow"))
        reported_sum = _decimal(components.get("non_capex_outflow_sum"))
        calculated_sum = sum((item for item in parsed_outflows if item is not None), Decimal(0))
        if (
            total is None
            or total < 0
            or reported_sum is None
            or not _close_decimal(reported_sum, calculated_sum)
            or not _close_decimal(total, calculated_sum)
        ):
            return "invalid_capex_provenance"
        return "complete"

    inflow = _decimal(components.get("total_invest_inflow"))
    other = _decimal(components.get("invest_netcash_other"))
    balance = _decimal(components.get("invest_netcash_balance"))
    net = _decimal(components.get("netcash_invest"))
    solved = _decimal(components.get("solved_total_invest_outflow"))
    if None in {inflow, other, balance, net, solved}:
        return "invalid_capex_provenance"
    recomputed = inflow + other + balance - net
    if not _close_decimal(recomputed, solved) or not _close_decimal(solved, Decimal(0)):
        return "invalid_capex_provenance"
    return "complete"
