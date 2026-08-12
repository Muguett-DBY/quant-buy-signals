"""Versioned official evidence for financial values corrupted by bulk APIs.

Generic null filling or statement mixing would corrupt unknown values.  This
module therefore accepts only exact code/report-date overrides backed by
exchange-filed documents whose URL and SHA256 are committed with the model.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


ZERO_REVENUE_EVIDENCE_PATH = Path(__file__).resolve().parent / "financial_zero_revenue_evidence.json"
ZERO_CAPEX_EVIDENCE_PATH = Path(__file__).resolve().parent / "financial_zero_capex_evidence.json"
BALANCE_SHEET_EVIDENCE_PATH = Path(__file__).resolve().parent / "financial_balance_sheet_evidence.json"
_ANNUAL_DATE = re.compile(r"^(?:19|20)\d{2}-12-31$")
_ANNUAL_OR_Q1_DATE = re.compile(r"^(?:19|20)\d{2}-(?:03-31|12-31)$")
_CODE = re.compile(r"^[036]\d{5}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATIC_CNINFO_SOURCE_PATHS = {
    "static.cninfo.com.cn": re.compile(r"^/finalpage/\d{4}-\d{2}-\d{2}/[0-9]+\.PDF$"),
}
_CAPEX_SOURCE_PATHS = {
    **_STATIC_CNINFO_SOURCE_PATHS,
    "dataclouds.cninfo.com.cn": re.compile(r"^/shgonggao/hsomarket/\d{4}/\d{8}/[0-9a-f]{32}\.PDF$"),
    "disc.static.szse.cn": re.compile(
        r"^/download/disc/disk\d{2}/finalpage/\d{4}-\d{2}-\d{2}/"
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.PDF$"
    ),
}
_BALANCE_REQUIRED_FIELDS = {
    "TOTAL_ASSETS",
    "TOTAL_LIABILITIES",
    "TOTAL_EQUITY",
    "TOTAL_PARENT_EQUITY",
    "MINORITY_EQUITY",
}
_BALANCE_OPTIONAL_FIELDS = {
    "MONETARYFUNDS",
    "SHORT_LOAN",
    "LONG_LOAN",
    "BONDS_PAYABLE",
    "NONCURRENT_LIAB_1YEAR",
    "LEASE_LIAB",
    "SHORT_BONDS_PAYABLE",
    "BORROW_FUNDS",
    "CENTRAL_BANK_BORROWING",
    "SUBORDINATED_BONDS_PAYABLE",
}


class FinancialSourceEvidenceError(ValueError):
    """Committed source evidence is malformed or cannot be applied safely."""


def _load_payload(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinancialSourceEvidenceError(f"cannot load financial source evidence: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise FinancialSourceEvidenceError("financial source evidence root must be an object")
    return payload


def _trusted_source_url(source_url: str, allowed_source_paths: Mapping[str, re.Pattern[str]]) -> bool:
    parsed_url = urlsplit(source_url)
    allowed_path = allowed_source_paths.get(parsed_url.hostname or "")
    return bool(
        parsed_url.scheme == "https"
        and allowed_path is not None
        and parsed_url.port is None
        and parsed_url.username is None
        and parsed_url.password is None
        and not parsed_url.query
        and not parsed_url.fragment
        and allowed_path.fullmatch(parsed_url.path) is not None
    )


def _load_zero_evidence(
    path: str | Path,
    *,
    metric: str,
    report_date_pattern: re.Pattern[str],
    evidence_type: str,
    allowed_source_paths: Mapping[str, re.Pattern[str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    source = Path(path)
    payload = _load_payload(source)
    if payload.get("schema_version") != 1 or payload.get("metric") != metric:
        raise FinancialSourceEvidenceError("financial source evidence contract is unsupported")
    records = payload.get("records")
    if not isinstance(records, list):
        raise FinancialSourceEvidenceError("financial source evidence records must be a list")

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} must be an object")
        code = raw.get("code")
        report_date = raw.get("report_date")
        value = raw.get("value")
        source_document = raw.get("source_document")
        source_url = raw.get("source_url")
        source_sha256 = raw.get("source_sha256")
        source_page = raw.get("source_page")
        source_statement = raw.get("source_statement")
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid code")
        if not isinstance(report_date, str) or report_date_pattern.fullmatch(report_date) is None:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid report_date")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid value")
        if float(value) != 0.0:
            raise FinancialSourceEvidenceError("financial source evidence may only prove an explicit zero")
        if not isinstance(source_url, str) or not _trusted_source_url(source_url, allowed_source_paths):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} uses an untrusted URL")
        if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid SHA256")
        if isinstance(source_page, bool) or not isinstance(source_page, int) or source_page < 1:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid page")
        for field, text in (("source_document", source_document), ("source_statement", source_statement)):
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 200:
                raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid {field}")

        identity = (code, report_date)
        if identity in result:
            raise FinancialSourceEvidenceError(f"duplicate financial source evidence identity: {identity}")
        result[identity] = {
            "evidence_type": evidence_type,
            "metric": metric,
            "value": 0.0,
            "source_document": source_document.strip(),
            "source_url": source_url,
            "source_sha256": source_sha256,
            "source_page": source_page,
            "source_statement": source_statement.strip(),
        }
    return result


def load_zero_revenue_evidence(
    path: str | Path = ZERO_REVENUE_EVIDENCE_PATH,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load and strictly validate explicit annual zero-revenue evidence."""
    return _load_zero_evidence(
        path,
        metric="TOTAL_OPERATE_INCOME",
        report_date_pattern=_ANNUAL_DATE,
        evidence_type="exchange_filed_explicit_zero",
        allowed_source_paths=_STATIC_CNINFO_SOURCE_PATHS,
    )


def load_zero_capex_evidence(
    path: str | Path = ZERO_CAPEX_EVIDENCE_PATH,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load annual/Q1 capex zeroes proven by exchange-filed statements."""
    return _load_zero_evidence(
        path,
        metric="CONSTRUCT_LONG_ASSET",
        report_date_pattern=_ANNUAL_OR_Q1_DATE,
        evidence_type="exchange_filed_statement_zero",
        allowed_source_paths=_CAPEX_SOURCE_PATHS,
    )


def load_balance_sheet_evidence(
    path: str | Path = BALANCE_SHEET_EVIDENCE_PATH,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load exact annual balance-sheet lineages from exchange-filed reports."""
    payload = _load_payload(Path(path))
    if payload.get("schema_version") != 1 or payload.get("metric") != "BALANCE_SHEET_CANONICAL_VALUES":
        raise FinancialSourceEvidenceError("financial source evidence contract is unsupported")
    records = payload.get("records")
    if not isinstance(records, list):
        raise FinancialSourceEvidenceError("financial source evidence records must be a list")

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} must be an object")
        code = raw.get("code")
        report_date = raw.get("report_date")
        source_document = raw.get("source_document")
        source_url = raw.get("source_url")
        source_sha256 = raw.get("source_sha256")
        source_pages = raw.get("source_pages")
        source_statement = raw.get("source_statement")
        reporting_basis = raw.get("reporting_basis")
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid code")
        if not isinstance(report_date, str) or _ANNUAL_DATE.fullmatch(report_date) is None:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid report_date")

        raw_values = raw.get("canonical_values")
        if not isinstance(raw_values, Mapping):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid canonical_values")
        fields = set(raw_values)
        missing_fields = sorted(_BALANCE_REQUIRED_FIELDS - fields)
        unknown_fields = sorted(fields - _BALANCE_REQUIRED_FIELDS - _BALANCE_OPTIONAL_FIELDS)
        if missing_fields or unknown_fields:
            raise FinancialSourceEvidenceError(
                f"financial source evidence record {index} has invalid canonical fields: "
                f"missing={missing_fields}, unknown={unknown_fields}"
            )
        canonical_values: dict[str, float] = {}
        for field, raw_amount in raw_values.items():
            if (
                isinstance(raw_amount, bool)
                or not isinstance(raw_amount, (int, float))
                or not math.isfinite(float(raw_amount))
            ):
                raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid {field}")
            canonical_values[str(field)] = float(raw_amount)
        if canonical_values["TOTAL_ASSETS"] <= 0 or canonical_values["TOTAL_LIABILITIES"] < 0:
            raise FinancialSourceEvidenceError(
                f"financial source evidence record {index} has invalid balance-sheet totals"
            )
        for field in _BALANCE_OPTIONAL_FIELDS & fields:
            if canonical_values[field] < 0:
                raise FinancialSourceEvidenceError(f"financial source evidence record {index} has negative {field}")

        equity_scale = max(
            abs(canonical_values["TOTAL_EQUITY"]),
            abs(canonical_values["TOTAL_PARENT_EQUITY"]) + abs(canonical_values["MINORITY_EQUITY"]),
            1.0,
        )
        if abs(
            canonical_values["TOTAL_EQUITY"]
            - canonical_values["TOTAL_PARENT_EQUITY"]
            - canonical_values["MINORITY_EQUITY"]
        ) > max(0.02, equity_scale * 1e-12):
            raise FinancialSourceEvidenceError(
                f"financial source evidence record {index} violates total=parent+minority equity identity"
            )
        balance_scale = max(
            abs(canonical_values["TOTAL_ASSETS"]),
            abs(canonical_values["TOTAL_LIABILITIES"]) + abs(canonical_values["TOTAL_EQUITY"]),
            1.0,
        )
        if abs(
            canonical_values["TOTAL_ASSETS"] - canonical_values["TOTAL_LIABILITIES"] - canonical_values["TOTAL_EQUITY"]
        ) > max(0.02, balance_scale * 1e-12):
            raise FinancialSourceEvidenceError(
                f"financial source evidence record {index} violates assets=liabilities+equity identity"
            )
        if not isinstance(source_url, str) or not _trusted_source_url(source_url, _STATIC_CNINFO_SOURCE_PATHS):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} uses an untrusted URL")
        if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid SHA256")
        if (
            not isinstance(source_pages, list)
            or not source_pages
            or len(source_pages) > 10
            or any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in source_pages)
            or source_pages != sorted(set(source_pages))
        ):
            raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid source_pages")
        for field, text in (
            ("source_document", source_document),
            ("source_statement", source_statement),
            ("reporting_basis", reporting_basis),
        ):
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 200:
                raise FinancialSourceEvidenceError(f"financial source evidence record {index} has invalid {field}")

        identity = (code, report_date)
        if identity in result:
            raise FinancialSourceEvidenceError(f"duplicate financial source evidence identity: {identity}")
        result[identity] = {
            "evidence_type": "exchange_filed_balance_sheet_lineage",
            "metric": "BALANCE_SHEET_CANONICAL_VALUES",
            "canonical_values": canonical_values,
            "source_document": source_document.strip(),
            "source_url": source_url,
            "source_sha256": source_sha256,
            "source_pages": list(source_pages),
            "source_statement": source_statement.strip(),
            "reporting_basis": reporting_basis.strip(),
        }
    return result


@lru_cache(maxsize=1)
def zero_revenue_evidence() -> dict[tuple[str, str], dict[str, Any]]:
    """Return the immutable-generation evidence map from packaged data."""
    return load_zero_revenue_evidence()


@lru_cache(maxsize=1)
def zero_capex_evidence() -> dict[tuple[str, str], dict[str, Any]]:
    """Return versioned official capex-zero evidence from packaged data."""
    return load_zero_capex_evidence()


@lru_cache(maxsize=1)
def balance_sheet_evidence() -> dict[tuple[str, str], dict[str, Any]]:
    """Return exact official annual balance-sheet lineage evidence."""
    return load_balance_sheet_evidence()
