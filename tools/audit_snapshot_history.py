"""Audit annual-history coverage in one existing safe-cache snapshot.

This command is deliberately read-only: it loads one caller-supplied cache,
performs no network access, and emits one compact JSON document to stdout.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Any

import pandas as pd

from data.cache import SafeFileCache


ANNUAL_DATASETS = ("revenue_history", "income_history", "cashflow", "balance", "indicators")
_ANNUAL_DATE = re.compile(r"^(\d{4})-12-31(?:[T ].*)?$")
_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_LISTING_DATE_FIELDS = ("listing_date", "list_date", "LISTING_DATE", "LIST_DATE", "上市日期")
_MAX_REQUESTED_COMPANIES = 20


class HistoryAuditError(RuntimeError):
    """The supplied artifact cannot be audited defensibly."""


@dataclass(frozen=True)
class AnnualObservation:
    years: tuple[int, ...]
    invalid_report_dates: int
    non_annual_records: int
    duplicate_annual_records: int
    malformed_dataset: bool


def _is_sh_sz_code(value: object) -> bool:
    code = value if isinstance(value, str) else ""
    return bool(re.fullmatch(r"[0-9]{6}", code)) and code.startswith(("0", "3", "6"))


def _records(value: object) -> tuple[list[object], bool]:
    if value is None:
        return [], False
    if isinstance(value, pd.DataFrame):
        return list(value.to_dict(orient="records")), False
    if isinstance(value, (list, tuple)):
        return list(value), False
    return [], True


def _date_text(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value.strip() if isinstance(value, str) else ""


def _observe_annual_periods(value: object) -> AnnualObservation:
    records, malformed_dataset = _records(value)
    years: list[int] = []
    invalid = 0
    non_annual = 0
    for record in records:
        if not isinstance(record, Mapping):
            invalid += 1
            continue
        report_date = _date_text(record.get("REPORT_DATE"))
        match = _ANNUAL_DATE.fullmatch(report_date)
        if match:
            years.append(int(match.group(1)))
        elif _DATE_PREFIX.fullmatch(report_date):
            non_annual += 1
        else:
            invalid += 1
    unique_years = tuple(sorted(set(years)))
    return AnnualObservation(
        years=unique_years,
        invalid_report_dates=invalid,
        non_annual_records=non_annual,
        duplicate_annual_records=len(years) - len(unique_years),
        malformed_dataset=malformed_dataset,
    )


def _parse_listing_date(value: object) -> tuple[str | None, int | None]:
    text = _date_text(value)
    match = _DATE_PREFIX.fullmatch(text)
    if match is None:
        return None, None
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None, None
    return parsed.isoformat(), parsed.year


def _quote_records(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, pd.DataFrame):
        raw = value.to_dict(orient="records")
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def _listing_dates(payload: Mapping[str, Any]) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for row in _quote_records(payload.get("quotes")):
        code_value = row.get("code")
        code = code_value.strip() if isinstance(code_value, str) else ""
        if not _is_sh_sz_code(code):
            continue
        for field in _LISTING_DATE_FIELDS:
            parsed, year = _parse_listing_date(row.get(field))
            if parsed is not None and year is not None:
                result[code] = (parsed, year)
                break
    return result


def _internal_holes(years: Sequence[int], expected_years: Sequence[int]) -> tuple[int, ...]:
    expected = set(expected_years)
    observed = sorted(set(years) & expected)
    if len(observed) < 2:
        return ()
    return tuple(year for year in range(observed[0] + 1, observed[-1]) if year in expected and year not in observed)


def _expectation(requested_years: Sequence[int], listing_year: int | None) -> tuple[tuple[int, ...], str]:
    requested = tuple(requested_years)
    if listing_year is None:
        return requested, "requested_window_no_listing_date_evidence"
    return tuple(year for year in requested if year >= listing_year), "listing_date_adjusted_window"


def _classification(
    years: Sequence[int],
    requested_years: Sequence[int],
    listing_year: int | None,
) -> dict[str, Any]:
    observed = set(years)
    requested = tuple(requested_years)
    expected, expectation_basis = _expectation(requested, listing_year)
    missing_requested = tuple(year for year in requested if year not in observed)
    missing_expected = tuple(year for year in expected if year not in observed)
    holes = _internal_holes(years, expected)

    if requested and not missing_requested:
        status = "complete_requested_window"
    elif not expected and listing_year is not None:
        status = "no_annual_period_expected_from_listing_date"
    elif expected and not missing_expected and listing_year is not None:
        status = "complete_listing_age_adjusted_window"
    elif holes:
        status = "internal_gaps"
    elif not years:
        status = "no_observed_annual_history"
    elif listing_year is None and len(years) < len(requested):
        status = "short_observed_history_unknown_cause"
    elif len(years) < len(expected):
        status = "short_against_listing_age_expectation"
    else:
        status = "observed_window_not_aligned_unknown_cause"

    return {
        "status": status,
        "expectation_basis": expectation_basis,
        "expected_years": list(expected),
        "missing_expected_years": list(missing_expected),
        "missing_requested_years": list(missing_requested),
        "internal_holes": list(holes),
        "complete_requested_window": not missing_requested,
    }


def _ratio(count: int, total: int) -> float | None:
    return round(count / total, 6) if total else None


def _infer_latest_year(payload: Mapping[str, Any], financials: Mapping[str, Any]) -> tuple[int, str]:
    validation = payload.get("validation")
    if isinstance(validation, Mapping):
        contract = validation.get("reporting_period_contract")
        if isinstance(contract, Mapping):
            annual_date = _date_text(contract.get("annual_report_date"))
            match = _ANNUAL_DATE.fullmatch(annual_date)
            if match:
                return int(match.group(1)), "validation.reporting_period_contract.annual_report_date"
    observed: list[int] = []
    for company in financials.values():
        if not isinstance(company, Mapping):
            continue
        for dataset in ANNUAL_DATASETS:
            observed.extend(_observe_annual_periods(company.get(dataset)).years)
    if not observed:
        raise HistoryAuditError("latest year cannot be inferred because no annual periods were observed")
    return max(observed), "maximum_observed_annual_year"


def audit_history_payload(
    payload: Mapping[str, Any],
    *,
    window_years: int = 10,
    latest_year: int | None = None,
    requested_companies: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(window_years, int) or isinstance(window_years, bool) or not 1 <= window_years <= 50:
        raise HistoryAuditError("window_years must be an integer from 1 through 50")
    financials_value = payload.get("financials")
    if not isinstance(financials_value, Mapping):
        raise HistoryAuditError("snapshot payload has no financials mapping")
    financials = {code: company for code, company in financials_value.items() if _is_sh_sz_code(code)}
    if not financials:
        raise HistoryAuditError("snapshot financials contain no canonical SH/SZ company codes")
    if len(requested_companies) > _MAX_REQUESTED_COMPANIES:
        raise HistoryAuditError(f"at most {_MAX_REQUESTED_COMPANIES} requested companies are allowed")
    requested_codes: list[str] = []
    for raw_code in requested_companies:
        code = raw_code.strip() if isinstance(raw_code, str) else ""
        if not _is_sh_sz_code(code):
            raise HistoryAuditError(f"requested company is not a canonical SH/SZ code: {raw_code!r}")
        if code not in requested_codes:
            requested_codes.append(code)

    if latest_year is None:
        effective_latest_year, latest_basis = _infer_latest_year(payload, financials)
    else:
        if not isinstance(latest_year, int) or isinstance(latest_year, bool) or not 1990 <= latest_year <= 2200:
            raise HistoryAuditError("latest_year must be an integer from 1990 through 2200")
        effective_latest_year, latest_basis = latest_year, "command_argument"
    requested_years = tuple(range(effective_latest_year - window_years + 1, effective_latest_year + 1))
    listing_dates = _listing_dates(payload)

    observations: dict[str, dict[str, AnnualObservation]] = {}
    malformed_company_records = 0
    for code, company in financials.items():
        if not isinstance(company, Mapping):
            malformed_company_records += 1
            company = {}
        observations[code] = {dataset: _observe_annual_periods(company.get(dataset)) for dataset in ANNUAL_DATASETS}

    dataset_reports: dict[str, Any] = {}
    company_count = len(observations)
    for dataset in ANNUAL_DATASETS:
        period_counts: Counter[int] = Counter()
        statuses: Counter[str] = Counter()
        year_coverage: Counter[int] = Counter()
        hole_years: Counter[int] = Counter()
        invalid = non_annual = duplicate = malformed = 0
        latest_count = complete_count = 0
        for code, company_observations in observations.items():
            observation = company_observations[dataset]
            listing_year = listing_dates.get(code, (None, None))[1]
            classification = _classification(observation.years, requested_years, listing_year)
            period_counts[len(observation.years)] += 1
            statuses[classification["status"]] += 1
            latest_count += int(effective_latest_year in observation.years)
            complete_count += int(classification["complete_requested_window"])
            year_coverage.update(set(observation.years) & set(requested_years))
            hole_years.update(classification["internal_holes"])
            invalid += observation.invalid_report_dates
            non_annual += observation.non_annual_records
            duplicate += observation.duplicate_annual_records
            malformed += int(observation.malformed_dataset)
        dataset_reports[dataset] = {
            "period_count_distribution": {str(key): period_counts[key] for key in sorted(period_counts)},
            "status_distribution": dict(sorted(statuses.items())),
            "latest_year_coverage": {"count": latest_count, "ratio": _ratio(latest_count, company_count)},
            "complete_requested_window_coverage": {
                "count": complete_count,
                "ratio": _ratio(complete_count, company_count),
            },
            "requested_year_coverage": {
                str(year): {"count": year_coverage[year], "ratio": _ratio(year_coverage[year], company_count)}
                for year in requested_years
            },
            "internal_hole_years": {str(year): hole_years[year] for year in sorted(hole_years)},
            "record_quality": {
                "invalid_report_date_records": invalid,
                "non_annual_records_ignored": non_annual,
                "duplicate_annual_records": duplicate,
                "malformed_dataset_company_values": malformed,
            },
        }

    requested_report: dict[str, Any] = {}
    for code in requested_codes:
        listing = listing_dates.get(code)
        company_report: dict[str, Any] = {
            "present": code in observations,
            "listing_date": listing[0] if listing else None,
            "listing_date_evidence": "snapshot_quotes" if listing else "unavailable",
            "datasets": {},
        }
        for dataset in ANNUAL_DATASETS:
            observation = observations[code][dataset] if code in observations else AnnualObservation((), 0, 0, 0, False)
            classification = _classification(observation.years, requested_years, listing[1] if listing else None)
            company_report["datasets"][dataset] = {
                "years": list(observation.years),
                "period_count": len(observation.years),
                "latest_year_present": effective_latest_year in observation.years,
                **classification,
                "record_quality": {
                    "invalid_report_date_records": observation.invalid_report_dates,
                    "non_annual_records_ignored": observation.non_annual_records,
                    "duplicate_annual_records": observation.duplicate_annual_records,
                    "malformed_dataset": observation.malformed_dataset,
                },
            }
        requested_report[code] = company_report

    return {
        "ok": True,
        "scope": {
            "market": "SH/SZ",
            "company_count": company_count,
            "excluded_non_sh_sz_financial_entries": len(financials_value) - company_count,
            "malformed_company_records": malformed_company_records,
            "requested_window": {
                "latest_year": effective_latest_year,
                "latest_year_basis": latest_basis,
                "window_years": window_years,
                "years": list(requested_years),
            },
            "listing_date_evidence": {
                "company_count": sum(code in listing_dates for code in observations),
                "policy": (
                    "listing year narrows the expected window; absent listing-date evidence, short observed "
                    "history remains unknown cause; no causal data-loss conclusion is made"
                ),
            },
        },
        "datasets": dataset_reports,
        "requested_companies": requested_report,
    }


def audit_safe_cache(
    snapshot_path: str | Path,
    *,
    schema_version: int,
    window_years: int = 10,
    latest_year: int | None = None,
    requested_companies: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        raise HistoryAuditError("schema_version must be a positive integer")
    path = Path(snapshot_path)
    loaded = SafeFileCache(path, schema_version=schema_version).load(allow_expired=True)
    if not loaded.hit:
        raise HistoryAuditError(f"safe-cache load failed: {loaded.reason or 'unknown reason'}")
    if not isinstance(loaded.value, Mapping):
        raise HistoryAuditError("safe-cache payload is not a mapping")
    report = audit_history_payload(
        loaded.value,
        window_years=window_years,
        latest_year=latest_year,
        requested_companies=requested_companies,
    )
    metadata = loaded.metadata if isinstance(loaded.metadata, Mapping) else {}
    report["cache"] = {
        "path": str(path.resolve()),
        "schema_version": metadata.get("schema_version"),
        "payload_sha256": metadata.get("payload_sha256"),
        "expired_artifacts_allowed_for_read_only_audit": True,
    }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="existing safe-cache snapshot path")
    parser.add_argument("--schema", type=int, required=True, help="expected safe-cache schema version")
    parser.add_argument("--window-years", type=int, default=10)
    parser.add_argument("--latest-year", type=int)
    parser.add_argument("--company", action="append", default=[], help="SH/SZ code; repeat up to 20 times")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = audit_safe_cache(
            args.snapshot,
            schema_version=args.schema,
            window_years=args.window_years,
            latest_year=args.latest_year,
            requested_companies=args.company,
        )
    except (HistoryAuditError, OSError, ValueError) as exc:
        report = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
