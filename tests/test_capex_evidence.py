from copy import deepcopy

import pytest

from data.capex_evidence import resolve_capex_evidence, validate_capex_provenance
from data.financial_source_evidence import zero_capex_evidence


def _detail(**overrides):
    row = {
        "CONSTRUCT_LONG_ASSET": None,
        "INVEST_PAY_CASH": None,
        "PLEDGE_LOAN_ADD": None,
        "OBTAIN_SUBSIDIARY_OTHER": None,
        "ADD_PLEDGE_TIMEDEPOSITS": None,
        "PAY_OTHER_INVEST": None,
        "INVEST_OUTFLOW_OTHER": None,
        "INVEST_OUTFLOW_BALANCE": None,
        "TOTAL_INVEST_INFLOW": None,
        "TOTAL_INVEST_OUTFLOW": None,
        "INVEST_NETCASH_OTHER": None,
        "INVEST_NETCASH_BALANCE": None,
        "NETCASH_INVEST": None,
    }
    row.update(overrides)
    return row


def test_reported_capex_remains_a_reported_fact_even_when_negative():
    value, provenance = resolve_capex_evidence(-12.5, None, report_date="2025-12-31")

    assert value == -12.5
    assert provenance["evidence_label"] == "fact_source_reported"
    assert (
        validate_capex_provenance(
            provenance,
            expected_value=value,
            expected_report_date="2025-12-31",
        )
        == "complete"
    )


def test_detailed_reported_capex_is_used_when_compact_report_is_blank():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(CONSTRUCT_LONG_ASSET=123.0),
        report_date="2026-03-31",
    )

    assert value == 123.0
    assert provenance["source_report"] == "RPT_F10_FINANCE_GCASHFLOW"
    assert provenance["evidence_label"] == "fact_source_reported"


def test_zero_is_derived_only_when_detailed_outflow_residual_closes():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(TOTAL_INVEST_OUTFLOW=25.0, INVEST_PAY_CASH=10.0, PAY_OTHER_INVEST=15.0),
        report_date="2026-03-31",
    )

    assert value == 0.0
    assert provenance["evidence_label"] == "derived_calculation"
    assert provenance["derivation_method"] == "detailed_outflow_residual_zero"
    assert "PLEDGE_LOAN_ADD" in provenance["source_null_fields"]
    assert (
        validate_capex_provenance(
            provenance,
            expected_value=0.0,
            expected_report_date="2026-03-31",
        )
        == "complete"
    )


def test_large_statement_residual_of_tens_of_yuan_is_not_rounded_to_zero():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(
            TOTAL_INVEST_OUTFLOW=65_233_624_857.68,
            INVEST_PAY_CASH=65_233_624_792.68,
        ),
        report_date="2025-12-31",
    )

    assert value is None
    assert provenance["status"] == "missing"
    assert provenance["reason"] == "missing_detailed_component:TOTAL_INVEST_INFLOW"


def test_zero_is_derived_from_net_investing_identity_when_total_outflow_is_blank():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(
            TOTAL_INVEST_INFLOW=9_500.0,
            INVEST_NETCASH_BALANCE=0.0,
            NETCASH_INVEST=9_500.0,
        ),
        report_date="2025-03-31",
    )

    assert value == 0.0
    assert provenance["derivation_method"] == "detailed_net_cash_identity_zero"
    assert (
        validate_capex_provenance(
            provenance,
            expected_value=0.0,
            expected_report_date="2025-03-31",
        )
        == "complete"
    )


def test_positive_residual_is_not_assigned_to_capex():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(TOTAL_INVEST_OUTFLOW=25.0, INVEST_PAY_CASH=10.0),
        report_date="2026-03-31",
    )

    assert value is None
    assert provenance["status"] == "missing"
    assert provenance["reason"] == "missing_detailed_component:TOTAL_INVEST_INFLOW"


def test_negative_reclassification_fails_closed_instead_of_deriving_zero():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(TOTAL_INVEST_OUTFLOW=0.0, INVEST_PAY_CASH=-1.0),
        report_date="2026-03-31",
    )

    assert value is None
    assert provenance["reason"] == "negative_detailed_component:INVEST_PAY_CASH"


def test_tampered_derived_components_are_rejected():
    value, provenance = resolve_capex_evidence(
        None,
        _detail(TOTAL_INVEST_OUTFLOW=25.0, INVEST_PAY_CASH=10.0, PAY_OTHER_INVEST=15.0),
        report_date="2026-03-31",
    )
    tampered = deepcopy(provenance)
    tampered["components"]["non_capex_outflows"]["PAY_OTHER_INVEST"] = 14.0

    assert value == 0.0
    assert (
        validate_capex_provenance(
            tampered,
            expected_value=value,
            expected_report_date="2026-03-31",
        )
        == "invalid_capex_provenance"
    )


def test_versioned_exchange_filing_fills_only_its_exact_q1_zero():
    evidence = zero_capex_evidence()[("600503", "2026-03-31")]
    value, provenance = resolve_capex_evidence(
        None,
        None,
        report_date="2026-03-31",
        security_code="600503",
        official_evidence=evidence,
    )

    assert value == 0.0
    assert provenance["evidence_label"] == "fact_official_report_zero"
    assert provenance["security_code"] == "600503"
    assert (
        validate_capex_provenance(
            provenance,
            expected_value=value,
            expected_report_date="2026-03-31",
        )
        == "complete"
    )


def test_versioned_exchange_filing_fills_only_its_exact_annual_zero():
    evidence = zero_capex_evidence()[("000670", "2019-12-31")]
    value, provenance = resolve_capex_evidence(
        None,
        None,
        report_date="2019-12-31",
        security_code="000670",
        official_evidence=evidence,
    )

    assert value == 0.0
    assert provenance["evidence_label"] == "fact_official_report_zero"
    assert provenance["source_report"] == "CNINFO_EXCHANGE_FILED_ANNUAL_REPORT"
    assert (
        validate_capex_provenance(
            provenance,
            expected_value=value,
            expected_report_date="2019-12-31",
        )
        == "complete"
    )


def test_versioned_zero_rejects_a_conflicting_upstream_fact():
    evidence = zero_capex_evidence()[("600503", "2026-03-31")]

    with pytest.raises(ValueError, match="conflicts"):
        resolve_capex_evidence(
            1.0,
            None,
            report_date="2026-03-31",
            security_code="600503",
            official_evidence=evidence,
        )


def test_tampered_official_document_provenance_is_rejected():
    evidence = zero_capex_evidence()[("600503", "2026-03-31")]
    value, provenance = resolve_capex_evidence(
        None,
        None,
        report_date="2026-03-31",
        security_code="600503",
        official_evidence=evidence,
    )
    tampered = deepcopy(provenance)
    tampered["source_sha256"] = "0" * 64

    assert (
        validate_capex_provenance(
            tampered,
            expected_value=value,
            expected_report_date="2026-03-31",
        )
        == "invalid_capex_provenance"
    )
