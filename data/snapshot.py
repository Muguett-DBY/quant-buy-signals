"""Validated market snapshots with bounded last-known-good fallback."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import CACHE_DIRECTORY
from data.capex_evidence import validate_capex_provenance
from data.datacenter import MAIN_FINANCIAL_INDICATOR_METRICS
from data.market_coldness import EASTMONEY_CLIST_ENDPOINT, EASTMONEY_SOURCE
from data.cache import (
    SafeCacheConflict,
    SafeCacheError,
    SafeFileCache,
    _canonical_json_bytes,
    _cross_process_lock,
    _encode_json_value,
    _thread_lock_for,
)


DEFAULT_SNAPSHOT_PATH = CACHE_DIRECTORY / "market_snapshot.json.gz"
# Version 7 additionally binds independently validated listing-date evidence
# to every whole-market quote generation.  Older caches cannot distinguish a
# pre-listing year from a missing financial observation and must be refreshed
# rather than relabelled.
SNAPSHOT_SCHEMA_VERSION = 8
MIN_MARKET_QUOTES = 3_000
MIN_FINANCIAL_COVERAGE = 0.90
MAX_STALE_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
# A quote generation is collected before the slower financial-report fetch.
# Thirty minutes leaves ample room for that work while rejecting an upstream
# replay whose HTTP retrieval metadata is unrelated to this generation.
MAX_QUOTE_RETRIEVAL_AGE_SECONDS = 30 * 60
# Sina pages are one logical market snapshot.  A five-minute bound rejects a
# mixture of page generations while remaining far above normal page latency.
MAX_QUOTE_RETRIEVAL_SPAN_SECONDS = 5 * 60
# Source quote timestamps may legitimately precede retrieval across weekends
# and long exchange holidays, but an upstream replay from weeks or years ago
# must never become a fresh generation merely because HTTP retrieval is fresh.
MAX_SOURCE_QUOTE_AGE_SECONDS = 10 * 24 * 60 * 60
MIN_RELATIVE_QUOTE_RATIO = 0.90
MIN_TRADING_QUOTE_COVERAGE = 0.70
MIN_ANALYSIS_ELIGIBLE_COVERAGE = 0.80
MIN_RELATIVE_TRADING_RATIO = 0.90
MIN_RELATIVE_FINANCIAL_RATIO = 0.90
MIN_RELATIVE_MARKET_RATIO = 0.85
MIN_MARKET_CAP_COVERAGE = 0.99
MIN_PE_COVERAGE = 0.98
MIN_PB_COVERAGE = 0.98
MIN_LISTING_REFERENCE_COVERAGE = 0.99
MIN_LISTING_DATE_COVERAGE = 0.99
MIN_RELATIVE_FIELD_COVERAGE = 0.98
MIN_RELATIVE_VALUE_RATIO = 0.50
MAX_RELATIVE_VALUE_RATIO = 2.00
# The product's investable universe is Shanghai/Shenzhen only.  Beijing rows
# may still arrive from Sina as optional source-coverage telemetry, but a BJ
# outage must never freeze otherwise healthy SH/SZ data or financial updates.
MIN_MARKET_COUNTS = {"SH": 1_800, "SZ": 2_300}
MARKET_CAP_MEDIAN_MIN = 100_000_000.0
MARKET_CAP_MEDIAN_MAX = 10_000_000_000_000.0
MARKET_CAP_TOTAL_MIN = 10_000_000_000_000.0
MARKET_CAP_TOTAL_MAX = 10_000_000_000_000_000.0
PRICE_MAX = 1_000_000.0
SINGLE_COMPANY_MARKET_CAP_MAX = 100_000_000_000_000.0
# These are deliberately very wide source-integrity bounds, not investment
# filters.  They reject parser/unit explosions (for example ``1e250``) while
# retaining loss-making and negative-book-value companies as finite evidence.
MAX_ABS_PE = 1_000_000.0
MAX_ABS_PB = 100_000.0
PE_POSITIVE_MEDIAN_MIN = 0.1
PE_POSITIVE_MEDIAN_MAX = 1_000.0
PB_POSITIVE_MEDIAN_MIN = 0.01
PB_POSITIVE_MEDIAN_MAX = 100.0
MAX_ABS_FINANCIAL_VALUE = 10_000_000_000_000_000.0
_NONNEGATIVE_INDICATOR_FIELDS = {
    "RDEXPEND",
    "TOTAL_SHARE",
    "STAFF_NUM",
    "TOTALDEPOSITS",
    "GROSSLOANS",
    "LOAN_ADVANCES",
    "EARNED_PREMIUM",
    "CAPITAL_PROVISIONS_SUM",
}
FINANCIAL_TO_MARKET_CAP_MAX = {
    "revenue": 500.0,
    "profit": 100.0,
    "operating_cash_flow": 100.0,
    "capex": 100.0,
    "assets": 1_000.0,
    "attributable_equity": 100.0,
    "operating_profit": 100.0,
    "total_equity": 100.0,
    "total_liabilities": 1_000.0,
    "cash": 100.0,
    "interest_bearing_debt": 100.0,
}
FINANCIAL_RATIO_MEDIAN_BOUNDS = {
    "revenue": (1e-4, 50.0),
    "profit": (1e-5, 10.0),
    "operating_cash_flow": (1e-5, 10.0),
    "capex": (1e-6, 10.0),
    "assets": (1e-3, 100.0),
    "attributable_equity": (1e-3, 50.0),
}

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EXPECTED_UNSET = object()

_A_SHARE_CODE = re.compile(r"^[0-9]{6}$")
_ALLOWED_MARKETS = {"SH", "SZ", "BJ"}
_ANALYSIS_MARKETS = {"SH", "SZ"}
_FINANCIAL_INDUSTRIES = {"BANK", "INSURANCE", "SECURITIES", "FINANCIAL_OTHER"}
_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_TTM_REVENUE_FIELDS = ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")
_TTM_OCF_FIELDS = ("NETCASH_OPERATE",)
_TTM_CAPEX_FIELDS = (
    "CONSTRUCT_LONG_ASSET",
    "PAY_ACQ_CONST_FIASSETS",
    "购建固定资产无形资产和其他长期资产支付的现金",
)
_QUOTE_PROVENANCE_COLUMNS = {
    "trade_price",
    "reference_price",
    "price_source",
    "quote_status",
    "retrieved_at",
    "quote_tick_time",
    "source_trade_date",
}
_ALLOWED_QUOTE_STATUS_SOURCE = {
    "trading": "last_trade",
    "suspended_or_no_trade": "previous_close",
}
_FINANCIAL_DATASET_RULES = {
    "revenue_history": (3, (("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),)),
    "income_history": (
        3,
        (("PARENT_NETPROFIT",), ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")),
    ),
    "cashflow": (
        3,
        (
            ("NETCASH_OPERATE",),
            ("CONSTRUCT_LONG_ASSET", "PAY_ACQ_CONST_FIASSETS"),
        ),
    ),
    # Four equity endpoints support at least three average-begin/end ROE years.
    "balance": (
        4,
        (("TOTAL_ASSETS",), ("TOTAL_PARENT_EQUITY", "PARENT_EQUITY")),
    ),
    # At least one current annual observation with one genuine indicator is
    # required.  Individual metrics may still be absent and remain explicit
    # missing evidence in the scorer; this gate only rejects the old all-empty
    # cache generation and structurally incomplete upstream refreshes.
    "indicators": (
        1,
        (
            (
                "RDEXPEND",
                "ROIC",
                "ROEJQ",
                "XSMLL",
                "XSJLL",
                "TAXRATE",
                "TOTAL_SHARE",
                "STAFF_NUM",
                "KCFJCXSYJLR",
                "INTEREST_DEBT_RATIO",
            ),
        ),
    ),
}
_INTERIM_DATASET_RULES = {
    "income_interim": (
        "income_q1",
        (("PARENT_NETPROFIT",), ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME")),
    ),
    "cashflow_interim": ("cashflow_q1", (("NETCASH_OPERATE",),)),
}


class SnapshotUnavailableError(RuntimeError):
    """Neither a valid upstream snapshot nor a sufficiently recent fallback exists."""


class SnapshotGenerationConflict(RuntimeError):
    """A candidate attempted to overwrite a different or newer generation."""


@dataclass(frozen=True)
class MarketSnapshotOutcome:
    quotes: pd.DataFrame
    financials: Mapping[str, Mapping[str, Any]]
    data_timestamp: float
    source: str
    warning: str = ""
    validation: Mapping[str, Any] = field(default_factory=dict)
    retrieved_at: float | None = None
    baseline_timestamp: float | None = None
    baseline_payload_sha256: str | None = None
    analysis_quality: Mapping[str, Any] = field(default_factory=dict)
    previous_analysis_quality: Mapping[str, Any] = field(default_factory=dict)
    cache_diagnostic: Mapping[str, Any] = field(default_factory=dict)

    @property
    def eligible_codes(self) -> tuple[str, ...]:
        values = self.validation.get("eligible_codes", ())
        return tuple(str(value) for value in values) if isinstance(values, (list, tuple)) else ()

    @property
    def ineligible_codes(self) -> tuple[str, ...]:
        values = self.validation.get("ineligible_codes", ())
        return tuple(str(value) for value in values) if isinstance(values, (list, tuple)) else ()

    @property
    def analysis_quotes(self) -> pd.DataFrame:
        eligible = set(self.eligible_codes)
        if not eligible or "code" not in self.quotes:
            return self.quotes.iloc[0:0].copy()
        codes = self.quotes["code"].map(lambda value: str(value).strip())
        return self.quotes.loc[codes.isin(eligible)].copy()

    @property
    def analysis_financials(self) -> Mapping[str, Mapping[str, Any]]:
        eligible = set(self.eligible_codes)
        result: dict[str, Mapping[str, Any]] = {}
        for code, company in self.financials.items():
            if not isinstance(code, str) or not _A_SHARE_CODE.fullmatch(code):
                raise ValueError("financial mapping contains a non-canonical company code")
            if code in result:
                raise ValueError(f"duplicate financial identity: {code}")
            if code in eligible:
                result[code] = company
        return result


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@lru_cache(maxsize=4096)
def _parse_canonical_date_text(text: str) -> datetime:
    if len(text) != 10 or text[4] != "-" or text[7] != "-" or not (text[:4] + text[5:7] + text[8:]).isdigit():
        raise ValueError("date is not canonical YYYY-MM-DD")
    return datetime(
        int(text[:4]),
        int(text[5:7]),
        int(text[8:]),
        tzinfo=_SHANGHAI,
    )


def _parse_canonical_date(value: object) -> datetime:
    """Parse exactly ``YYYY-MM-DD`` without locale-backed ``strptime``.

    Snapshot validation performs this operation hundreds of thousands of
    times for a ten-year full-market generation.  Direct component parsing is
    materially faster and retains the same strict syntax/calendar checks.
    """
    text = value.strip() if isinstance(value, str) else ""
    return _parse_canonical_date_text(text)


def _parse_canonical_quote_datetime(date_value: object, time_value: object) -> datetime:
    """Parse exactly ``YYYY-MM-DD`` plus ``HH:MM:SS`` without locale state."""
    parsed_date = _parse_canonical_date(date_value)
    text = time_value.strip() if isinstance(time_value, str) else ""
    if len(text) != 8 or text[2] != ":" or text[5] != ":" or not (text[:2] + text[3:5] + text[6:]).isdigit():
        raise ValueError("time is not canonical HH:MM:SS")
    return parsed_date.replace(
        hour=int(text[:2]),
        minute=int(text[3:5]),
        second=int(text[6:]),
    )


def _optional_scalar_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        return None
    text = str(value).strip()
    return text or None


def _validate_listing_date_evidence(
    quotes: pd.DataFrame,
    analysis_market_mask: pd.Series,
    *,
    as_of_timestamp: float | None,
    full_market: bool,
) -> tuple[Mapping[str, Any], Mapping[str, str]]:
    """Validate listing dates and provenance without inventing missing dates."""
    columns = {
        "listing_date",
        "listing_date_status",
        "listing_date_source",
        "listing_date_source_url",
        "listing_date_retrieved_at",
    }
    present_columns = columns & set(quotes.columns)
    if present_columns != columns:
        if present_columns:
            raise ValueError(f"quotes contain a partial listing-evidence schema: {sorted(present_columns)}")
        if full_market:
            raise ValueError("whole-market quotes require listing-date evidence columns")
        return (
            {
                "required": False,
                "reference_count": 0,
                "reference_coverage": 0.0,
                "listing_date_count": 0,
                "listing_date_coverage": 0.0,
                "missing_reference_codes": [],
                "missing_listing_date_codes": [],
                "status_counts": {},
                "source": None,
                "source_url": None,
            },
            {},
        )

    analysis = quotes.loc[
        analysis_market_mask,
        [
            "code",
            "source_trade_date",
            "listing_date",
            "listing_date_status",
            "listing_date_source",
            "listing_date_source_url",
            "listing_date_retrieved_at",
        ],
    ]
    listing_dates: dict[str, str] = {}
    missing_reference_codes: list[str] = []
    missing_listing_date_codes: list[str] = []
    status_counts: dict[str, int] = {}
    reference_count = 0
    reference_timestamps: list[float] = []
    for row in analysis.to_dict(orient="records"):
        code = str(row["code"]).strip()
        listing_date = _optional_scalar_text(row["listing_date"])
        status = _optional_scalar_text(row["listing_date_status"])
        source = _optional_scalar_text(row["listing_date_source"])
        source_url = _optional_scalar_text(row["listing_date_source_url"])
        retrieved_at = _optional_scalar_text(row["listing_date_retrieved_at"])
        provenance_values = (status, source, source_url, retrieved_at)
        if not any(provenance_values):
            if listing_date is not None:
                raise ValueError(f"listing date has no provenance for {code}")
            missing_reference_codes.append(code)
            continue
        if not all(provenance_values):
            raise ValueError(f"listing-date provenance is partial for {code}")
        if source != EASTMONEY_SOURCE or source_url != EASTMONEY_CLIST_ENDPOINT:
            raise ValueError(f"listing-date provenance source is invalid for {code}")
        try:
            parsed_retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"listing-date provenance timestamp is invalid for {code}") from exc
        if parsed_retrieved.tzinfo is None:
            raise ValueError(f"listing-date provenance timestamp has no timezone for {code}")
        retrieved_timestamp = parsed_retrieved.timestamp()
        if not math.isfinite(retrieved_timestamp) or retrieved_timestamp <= 0:
            raise ValueError(f"listing-date provenance timestamp is invalid for {code}")
        if as_of_timestamp is not None and retrieved_timestamp > float(as_of_timestamp) + MAX_FUTURE_SKEW_SECONDS:
            raise ValueError(f"listing-date provenance timestamp is from the future for {code}")
        reference_timestamps.append(retrieved_timestamp)
        reference_count += 1
        status_counts[status] = status_counts.get(status, 0) + 1

        if listing_date is None:
            if status == "reported":
                raise ValueError(f"listing-date status reports a missing date as present for {code}")
            missing_listing_date_codes.append(code)
            continue
        if status != "reported":
            raise ValueError(f"listing-date status conflicts with the reported date for {code}")
        try:
            parsed_listing = _parse_canonical_date(listing_date)
            parsed_trade = _parse_canonical_date(row["source_trade_date"])
        except ValueError as exc:
            raise ValueError(f"listing date is invalid for {code}: {listing_date}") from exc
        if parsed_listing > parsed_trade:
            raise ValueError(f"listing date is later than the quote trade date for {code}")
        listing_dates[code] = listing_date

    population = len(analysis)
    reference_coverage = reference_count / max(population, 1)
    listing_date_coverage = len(listing_dates) / max(population, 1)
    if full_market and reference_coverage < MIN_LISTING_REFERENCE_COVERAGE:
        raise ValueError(
            f"listing-date reference coverage {reference_coverage:.1%} is below required "
            f"{MIN_LISTING_REFERENCE_COVERAGE:.1%}"
        )
    if full_market and listing_date_coverage < MIN_LISTING_DATE_COVERAGE:
        raise ValueError(
            f"listing-date coverage {listing_date_coverage:.1%} is below required {MIN_LISTING_DATE_COVERAGE:.1%}"
        )
    return (
        {
            "required": full_market,
            "reference_count": reference_count,
            "reference_coverage": reference_coverage,
            "listing_date_count": len(listing_dates),
            "listing_date_coverage": listing_date_coverage,
            "missing_reference_codes": sorted(missing_reference_codes),
            "missing_listing_date_codes": sorted(missing_listing_date_codes),
            "status_counts": dict(sorted(status_counts.items())),
            "source": EASTMONEY_SOURCE if reference_count else None,
            "source_url": EASTMONEY_CLIST_ENDPOINT if reference_count else None,
            "retrieved_at_oldest": min(reference_timestamps) if reference_timestamps else None,
            "retrieved_at_latest": max(reference_timestamps) if reference_timestamps else None,
        },
        listing_dates,
    )


def _finite_positive_series(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.notna() & np.isfinite(numeric) & numeric.gt(0)


def _finite_series(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.notna() & np.isfinite(numeric)


def _positive_distribution(series: pd.Series) -> Mapping[str, float]:
    numeric = pd.to_numeric(series, errors="coerce")
    numeric = numeric[numeric.notna() & np.isfinite(numeric) & numeric.gt(0)]
    if numeric.empty:
        return {"median": 0.0, "p01": 0.0, "p99": 0.0, "total": 0.0}
    return {
        "median": float(numeric.median()),
        "p01": float(numeric.quantile(0.01)),
        "p99": float(numeric.quantile(0.99)),
        "total": float(numeric.sum()),
    }


def _first_finite(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _finite_number(row.get(key))
        if value is not None:
            return value
    return None


def _validate_balance_identity(code: str, row: Mapping[str, Any]) -> None:
    """Reject internally contradictory attributable-equity evidence."""
    parent = _first_finite(
        row,
        (
            "PARENT_EQUITY",
            "TOTAL_PARENT_EQUITY",
            "TOTAL_EQUITY_ATTR_P",
            "TOTAL_EQUITY_PARENT",
            "PARENT_HOLDER_EQUITY",
        ),
    )
    total = _finite_number(row.get("TOTAL_EQUITY"))
    minority = _first_finite(row, ("MINORITY_EQUITY", "MINORITY_INTEREST"))
    assets = _finite_number(row.get("TOTAL_ASSETS"))
    liabilities = _finite_number(row.get("TOTAL_LIABILITIES"))
    if assets is not None and assets < 0:
        raise ValueError(f"financial record {code} has negative total assets")
    if assets == 0 and (liabilities is None or total is None or (liabilities == 0 and total == 0)):
        raise ValueError(f"financial record {code} has an uninformative zero-assets row")
    if liabilities is not None and liabilities < 0:
        raise ValueError(f"financial record {code} has negative total liabilities")
    if total is not None and total > 0 and assets is not None and total > assets * 1.03:
        raise ValueError(f"financial record {code} has total equity above total assets")
    if parent is not None and assets is not None and parent > assets * 1.03 and (total is None or minority is None):
        raise ValueError(f"financial record {code} has attributable equity above total assets")
    if parent is not None and total is not None:
        if minority is None:
            if total <= 0 < parent or (total > 0 and parent > total * 1.03):
                raise ValueError(f"financial record {code} has attributable equity above total equity")
        else:
            # A negative non-controlling interest is unusual but valid when a
            # consolidated subsidiary has accumulated deficits.  The binding
            # control is the accounting identity, not minority's sign.
            scale = max(abs(total), abs(parent) + abs(minority), 1.0)
            if abs(total - parent - minority) > scale * 0.03:
                raise ValueError(f"financial record {code} violates total=parent+minority equity identity")
    if assets is not None and liabilities is not None and total is not None:
        scale = max(abs(assets), abs(liabilities) + abs(total), 1.0)
        if abs(assets - liabilities - total) > scale * 0.03:
            raise ValueError(f"financial record {code} violates assets=liabilities+equity identity")
    debt_ratio = _finite_number(row.get("DEBT_ASSET_RATIO"))
    if debt_ratio is not None:
        if assets not in (None, 0) and liabilities is not None:
            # A company with deeply negative equity can legitimately have a
            # liabilities/assets ratio far above 1,000%.  The accounting
            # components are the authoritative integrity check; an arbitrary
            # magnitude cap would reject real distress history such as
            # 000820 in 2019-2020.  Require the published/derived percentage
            # to agree with those components instead.
            expected_ratio = liabilities / assets * 100.0
            scale = max(abs(debt_ratio), abs(expected_ratio), 1.0)
            if abs(debt_ratio - expected_ratio) > scale * 0.03:
                raise ValueError(f"financial record {code} has inconsistent debt/asset ratio")
        elif not -1_000.0 <= debt_ratio <= 1_000.0:
            raise ValueError(f"financial record {code} has uncheckable implausible debt/asset ratio")


def _validate_indicator_source_integrity(financials: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject cache tampering and unit explosions in every consumed indicator field."""
    for code, company in financials.items():
        records = company.get("indicators", [])
        if isinstance(records, Mapping):
            records = [records]
        if not isinstance(records, (list, tuple)):
            continue
        for row in records:
            if not isinstance(row, Mapping):
                continue
            for name in MAIN_FINANCIAL_INDICATOR_METRICS:
                if name not in row or row.get(name) is None:
                    continue
                raw = row.get(name)
                if isinstance(raw, (bool, str, bytes, bytearray)):
                    raise ValueError(f"financial record {code} indicator {name} is not finite numeric evidence")
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        f"financial record {code} indicator {name} is not finite numeric evidence"
                    ) from exc
                if not math.isfinite(value):
                    raise ValueError(f"financial record {code} indicator {name} is not finite numeric evidence")
                if name in _NONNEGATIVE_INDICATOR_FIELDS and value < 0:
                    raise ValueError(f"financial record {code} indicator {name} is negative")
                if abs(value) > MAX_ABS_FINANCIAL_VALUE:
                    raise ValueError(
                        f"financial record {code} indicator {name} exceeds source-integrity magnitude bound"
                    )


def _validate_financial_source_integrity(
    financials: Mapping[str, Mapping[str, Any]],
    market_cap_by_code: Mapping[str, float],
    *,
    full_market: bool,
) -> tuple[Mapping[str, Mapping[str, float]], Mapping[str, Mapping[str, float]]]:
    """Validate source units and distributions independently of DCF algebra."""
    _validate_indicator_source_integrity(financials)
    definitions = {
        "revenue": (
            ("revenue_history", "income_history", "income_interim"),
            ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
        ),
        "profit": (("income_history", "income_interim"), ("PARENT_NETPROFIT",)),
        "operating_profit": (("income_history",), ("OPERATE_PROFIT", "TOTAL_PROFIT")),
        "operating_cash_flow": (("cashflow", "cashflow_interim"), ("NETCASH_OPERATE",)),
        "capex": (("cashflow",), ("CONSTRUCT_LONG_ASSET", "PAY_ACQ_CONST_FIASSETS")),
        "assets": (("balance",), ("TOTAL_ASSETS",)),
        "attributable_equity": (
            ("balance",),
            (
                "PARENT_EQUITY",
                "TOTAL_PARENT_EQUITY",
                "TOTAL_EQUITY_ATTR_P",
                "TOTAL_EQUITY_PARENT",
                "PARENT_HOLDER_EQUITY",
            ),
        ),
        "total_equity": (("balance",), ("TOTAL_EQUITY",)),
        "total_liabilities": (("balance",), ("TOTAL_LIABILITIES",)),
        "cash": (
            ("balance",),
            ("MONETARYFUNDS", "CASH_AND_CASH_EQUIVALENTS", "CASH_EQUIVALENTS"),
        ),
        "interest_bearing_debt": (
            ("balance",),
            (
                "INTEREST_BEARING_DEBT",
                "TOTAL_INTEREST_BEARING_DEBT",
                "SHORT_LOAN",
                "SHORT_BONDS_PAYABLE",
                "LONG_LOAN",
                "BOND_PAYABLE",
                "BONDS_PAYABLE",
                "NONCURRENT_LIAB_1YEAR",
                "CURRENT_PORTION_LONG_DEBT",
                "LEASE_LIAB",
                "LEASE_LIABILITIES",
            ),
        ),
    }
    absolute_values: dict[str, list[float]] = {key: [] for key in definitions}
    market_cap_ratios: dict[str, list[float]] = {key: [] for key in definitions}
    metrics_by_dataset: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for metric, (datasets, keys) in definitions.items():
        for dataset in datasets:
            metrics_by_dataset.setdefault(dataset, []).append((metric, keys))
    for code, company in financials.items():
        market_cap = _finite_number(market_cap_by_code.get(code))
        for dataset, metric_definitions in metrics_by_dataset.items():
            records = company.get(dataset, [])
            if isinstance(records, Mapping):
                records = [records]
            if not isinstance(records, (list, tuple)):
                continue
            for row in records:
                if not isinstance(row, Mapping):
                    continue
                if dataset == "balance":
                    _validate_balance_identity(code, row)
                for metric, keys in metric_definitions:
                    for key in keys:
                        value = _finite_number(row.get(key))
                        if value is None:
                            continue
                        if metric == "assets" and value < 0:
                            raise ValueError(f"financial record {code} has negative assets")
                        if metric in {"total_liabilities", "cash", "interest_bearing_debt"} and value < 0:
                            raise ValueError(f"financial record {code} has negative {metric} source field")
                        magnitude = abs(value)
                        if magnitude > MAX_ABS_FINANCIAL_VALUE:
                            raise ValueError(
                                f"financial record {code} {metric} exceeds source-integrity magnitude bound"
                            )
                        absolute_values[metric].append(magnitude)
                        if market_cap is not None and market_cap > 0:
                            ratio = magnitude / market_cap
                            if ratio > FINANCIAL_TO_MARKET_CAP_MAX[metric]:
                                raise ValueError(
                                    f"financial record {code} {metric}/market_cap ratio is implausible: {ratio:.4g}"
                                )
                            if ratio > 0:
                                market_cap_ratios[metric].append(ratio)
    value_distributions = {
        metric: _positive_distribution(pd.Series(values, dtype="float64")) for metric, values in absolute_values.items()
    }
    ratio_distributions = {
        metric: _positive_distribution(pd.Series(values, dtype="float64"))
        for metric, values in market_cap_ratios.items()
    }
    if full_market:
        for metric, (minimum, maximum) in FINANCIAL_RATIO_MEDIAN_BOUNDS.items():
            median = ratio_distributions[metric]["median"]
            if not minimum <= median <= maximum:
                raise ValueError(
                    f"financial {metric}/market_cap median is outside a plausible source-unit range: {median}"
                )
    return value_distributions, ratio_distributions


def _required_market_counts(
    min_quotes: int,
    requested: Mapping[str, int] | None,
) -> Mapping[str, int]:
    if requested is not None:
        result = {str(key).upper(): int(value) for key, value in requested.items()}
        if any(key not in _ALLOWED_MARKETS or value < 0 for key, value in result.items()):
            raise ValueError("min_market_counts contains an invalid market or negative count")
        # BJ is optional source telemetry only.  Even an explicitly supplied
        # legacy BJ threshold cannot turn it back into a generation gate.
        return {market: value for market, value in result.items() if market in _ANALYSIS_MARKETS}
    return MIN_MARKET_COUNTS if int(min_quotes) >= MIN_MARKET_QUOTES else {}


def _quote_retrieved_at(quotes: pd.DataFrame) -> float | None:
    if "retrieved_at" not in quotes:
        return None
    values = pd.to_numeric(quotes["retrieved_at"], errors="coerce")
    if "market" in quotes:
        markets = quotes["market"].map(lambda value: value.strip().upper() if isinstance(value, str) else "")
        values = values[markets.isin(_ANALYSIS_MARKETS)]
    finite = [float(value) for value in values if pd.notna(value) and math.isfinite(float(value)) and value > 0]
    return min(finite) if finite else None


def _validated_usable_financial_dates(
    company: Mapping[str, Any],
    dataset: str,
    required_field_groups: tuple[tuple[str, ...], ...],
    *,
    company_code: str,
    reference_datetime: datetime | None,
    allowed_month_days: set[tuple[int, int]],
) -> tuple[datetime, ...]:
    """Validate every numeric row and collect usable dates in a single pass."""
    records = company.get(dataset, [])
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)):
        return ()
    dates: list[datetime] = []
    seen_dates: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        report_date = str(record.get("REPORT_DATE") or "").strip()
        has_numeric_evidence = any(
            key != "REPORT_DATE" and _finite_number(value) is not None for key, value in record.items()
        )
        try:
            parsed = _parse_canonical_date(report_date)
        except ValueError as exc:
            if has_numeric_evidence:
                raise ValueError(
                    f"financial record {company_code} has invalid {dataset} report date {report_date!r}"
                ) from exc
            continue
        if reference_datetime is not None and parsed > reference_datetime:
            raise ValueError(f"financial record {company_code} has future {dataset} report date {report_date}")
        month_day = (parsed.month, parsed.day)
        if has_numeric_evidence and month_day not in allowed_month_days:
            coverage_label = (
                f"current {dataset} coverage" if dataset in _INTERIM_DATASET_RULES else f"{dataset} coverage"
            )
            expected = ",".join(f"{month:02d}-{day:02d}" for month, day in sorted(allowed_month_days))
            raise ValueError(
                f"financial record {company_code} makes {coverage_label} invalid: "
                f"unexpected report period {report_date}; expected one of {expected}"
            )
        if month_day not in allowed_month_days:
            continue
        if not all(
            any(_finite_number(record.get(name)) is not None for name in group) for group in required_field_groups
        ):
            continue
        if report_date in seen_dates:
            raise ValueError(f"financial record {company_code} has duplicate {dataset} report date {report_date}")
        seen_dates.add(report_date)
        dates.append(parsed)
    return tuple(dates)


def _expected_report_periods(
    as_of_timestamp: float,
) -> tuple[int, str, str, datetime]:
    reference = datetime.fromtimestamp(float(as_of_timestamp), tz=_SHANGHAI)
    month_day = (reference.month, reference.day)
    annual_year = reference.year - 1 if month_day >= (5, 1) else reference.year - 2
    if month_day >= (11, 1):
        interim_year, period_end = reference.year, "09-30"
    elif month_day >= (9, 1):
        interim_year, period_end = reference.year, "06-30"
    elif month_day >= (5, 1):
        interim_year, period_end = reference.year, "03-31"
    else:
        interim_year, period_end = reference.year - 1, "09-30"
    return (
        annual_year,
        f"{interim_year}-{period_end}",
        f"{interim_year - 1}-{period_end}",
        reference,
    )


def _reporting_period_contract(
    expected_annual_year: int | None,
    current_interim_report_date: str | None,
    prior_interim_report_date: str | None,
) -> Mapping[str, str] | None:
    """Return the exact, JSON-safe periods used by one snapshot generation."""
    if expected_annual_year is None or current_interim_report_date is None or prior_interim_report_date is None:
        return None
    return {
        "annual_report_date": f"{int(expected_annual_year):04d}-12-31",
        "current_interim_report_date": str(current_interim_report_date),
        "prior_interim_report_date": str(prior_interim_report_date),
        "period_basis": _TTM_PERIOD_BASIS,
    }


def _records_at_report_date(company: Mapping[str, Any], dataset: str, report_date: str) -> list[Mapping[str, Any]]:
    records = company.get(dataset, [])
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)):
        return []
    return [
        row for row in records if isinstance(row, Mapping) and str(row.get("REPORT_DATE") or "").strip() == report_date
    ]


def _strict_ttm_source_value(
    row: Mapping[str, Any],
    fields: tuple[str, ...],
) -> tuple[str, float | None]:
    present = [(field, row.get(field)) for field in fields if field in row and row.get(field) not in (None, "")]
    if not present:
        return "missing_component", None
    value = _finite_number(present[0][1])
    if value is None:
        return "nonfinite_component", None
    return "complete", value


def _strict_ttm_unit_status(value: float, *, metric: str, market_cap: float | None) -> str:
    """Protect TTM components from parser/unit explosions without aborting per row."""
    magnitude = abs(value)
    if magnitude > MAX_ABS_FINANCIAL_VALUE:
        return "implausible_unit"
    if market_cap is not None and market_cap > 0:
        maximum_ratio = FINANCIAL_TO_MARKET_CAP_MAX[metric]
        if magnitude / market_cap > maximum_ratio:
            return "implausible_unit"
    return "complete"


def _strict_ttm_metric_status(
    company: Mapping[str, Any],
    contract: Mapping[str, str],
    *,
    metric: str,
    market_cap: float | None,
) -> str:
    annual_date = contract["annual_report_date"]
    current_date = contract["current_interim_report_date"]
    prior_date = contract["prior_interim_report_date"]
    if metric == "revenue":
        targets = (
            ("revenue_history", annual_date),
            ("income_interim", current_date),
            ("income_interim", prior_date),
        )
        fields_by_component = ((_TTM_REVENUE_FIELDS, "revenue"),) * 3
    elif metric == "fcff":
        targets = (
            ("cashflow", annual_date),
            ("cashflow_interim", current_date),
            ("cashflow_interim", prior_date),
        )
        fields_by_component = (((_TTM_OCF_FIELDS, "operating_cash_flow"), (_TTM_CAPEX_FIELDS, "capex")),) * 3
    else:  # pragma: no cover - private caller supplies a closed metric set.
        raise ValueError(f"unsupported strict TTM metric: {metric}")

    rows: list[Mapping[str, Any]] = []
    for dataset, report_date in targets:
        matches = _records_at_report_date(company, dataset, report_date)
        if len(matches) > 1:
            return "duplicate_period"
        if not matches:
            return "missing_component"
        rows.append(matches[0])

    if metric == "revenue":
        for row, (fields, unit_metric) in zip(rows, fields_by_component):
            status, value = _strict_ttm_source_value(row, fields)
            if status != "complete":
                return status
            if value is None:  # Defensive guard if the source-value contract regresses.
                return "nonfinite_component"
            status = _strict_ttm_unit_status(value, metric=unit_metric, market_cap=market_cap)
            if status != "complete":
                return status
        return "complete"

    capex_values: list[float] = []
    for row, (_dataset, report_date), component_fields in zip(rows, targets, fields_by_component):
        for fields, unit_metric in component_fields:
            status, value = _strict_ttm_source_value(row, fields)
            if status != "complete":
                return status
            if value is None:  # Defensive guard if the source-value contract regresses.
                return "nonfinite_component"
            status = _strict_ttm_unit_status(value, metric=unit_metric, market_cap=market_cap)
            if status != "complete":
                return status
            if unit_metric == "capex":
                status = validate_capex_provenance(
                    row.get("CAPEX_PROVENANCE"),
                    expected_value=value,
                    expected_report_date=report_date,
                )
                if status != "complete":
                    return status
                capex_values.append(abs(value))
    reconstructed_capex = capex_values[0] + capex_values[1] - capex_values[2]
    if not math.isfinite(reconstructed_capex):
        return "nonfinite_component"
    if reconstructed_capex < 0:
        return "negative_reconstructed_capex"
    return "complete"


def _strict_ttm_source_coverage(
    nonfinancial_codes: set[str],
    financials: Mapping[str, Mapping[str, Any]],
    market_cap_by_code: Mapping[str, float],
    contract: Mapping[str, str] | None,
) -> Mapping[str, Any]:
    denominator = len(nonfinancial_codes)
    result: dict[str, Any] = {
        "population": "SH_SZ_non_financial",
        "denominator": denominator,
        "evaluated": contract is not None,
    }
    for metric in ("revenue", "fcff"):
        status_by_code: dict[str, str] = {}
        for code in sorted(nonfinancial_codes):
            company = financials.get(code)
            if contract is None:
                status = "invalid_period_contract"
            elif not isinstance(company, Mapping):
                status = "missing_component"
            else:
                status = _strict_ttm_metric_status(
                    company,
                    contract,
                    metric=metric,
                    market_cap=_finite_number(market_cap_by_code.get(code)),
                )
            status_by_code[code] = status
        complete_codes = sorted(code for code, status in status_by_code.items() if status == "complete")
        status_counts: dict[str, int] = {}
        missing_codes_by_status: dict[str, list[str]] = {}
        for code, status in status_by_code.items():
            status_counts[status] = status_counts.get(status, 0) + 1
            if status != "complete":
                missing_codes_by_status.setdefault(status, []).append(code)
        result[metric] = {
            "complete": len(complete_codes),
            "missing": denominator - len(complete_codes),
            "coverage": (len(complete_codes) / denominator if denominator else 1.0) if contract is not None else None,
            "status_counts": dict(sorted(status_counts.items())),
            "complete_codes": complete_codes,
            "missing_codes_by_status": {
                status: sorted(values) for status, values in sorted(missing_codes_by_status.items())
            },
        }
    return result


def _build_annual_history_profile(
    years_by_dataset: Mapping[str, Mapping[str, tuple[int, ...]]],
    population_codes: set[str],
    listing_dates: Mapping[str, str],
) -> Mapping[str, Any]:
    """Describe annual completeness against independently evidenced listing age."""
    datasets: dict[str, Any] = {}
    for dataset in _FINANCIAL_DATASET_RULES:
        by_code = years_by_dataset.get(dataset, {})
        periods = [tuple(by_code.get(code, ())) for code in sorted(population_codes)]
        counts = sorted(len(values) for values in periods)
        histogram: dict[str, int] = {}
        observed_years: set[int] = set()
        internal_gap_companies = 0
        for values in periods:
            histogram[str(len(values))] = histogram.get(str(len(values)), 0) + 1
            observed_years.update(values)
            if values and tuple(range(values[0], values[-1] + 1)) != values:
                internal_gap_companies += 1
        window_start = min(observed_years) if observed_years else None
        window_end = max(observed_years) if observed_years else None
        missing_without_listing_adjustment = 0
        listing_adjusted_missing = 0
        listing_adjusted_missing_by_code: dict[str, list[int]] = {}
        unknown_listing_missing_by_code: dict[str, list[int]] = {}
        pre_listing_observations_excluded = 0
        if window_start is not None and window_end is not None:
            whole_window = set(range(window_start, window_end + 1))
            for code in sorted(population_codes):
                actual = set(by_code.get(code, ()))
                unadjusted_missing = sorted(whole_window - actual)
                missing_without_listing_adjustment += len(unadjusted_missing)
                listing_date = listing_dates.get(code)
                if listing_date is None:
                    if unadjusted_missing:
                        unknown_listing_missing_by_code[code] = unadjusted_missing
                    continue
                listing_year = int(listing_date[:4])
                expected = {year for year in whole_window if year >= listing_year}
                missing = sorted(expected - actual)
                if missing:
                    listing_adjusted_missing_by_code[code] = missing
                    listing_adjusted_missing += len(missing)
                pre_listing_observations_excluded += len(whole_window - expected)
        middle = len(counts) // 2
        median_periods = (
            0.0
            if not counts
            else float(counts[middle])
            if len(counts) % 2
            else (counts[middle - 1] + counts[middle]) / 2.0
        )
        datasets[dataset] = {
            "population": len(population_codes),
            "period_count_histogram": dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
            "max_periods": max(counts, default=0),
            "median_periods": median_periods,
            "min_observed_year": min(observed_years) if observed_years else None,
            "max_observed_year": max(observed_years) if observed_years else None,
            "internal_gap_companies": internal_gap_companies,
            "missing_observations_without_listing_adjustment": missing_without_listing_adjustment,
            "listing_adjusted_missing_observations": listing_adjusted_missing,
            "listing_adjusted_missing_companies": len(listing_adjusted_missing_by_code),
            "listing_adjusted_missing_by_code": listing_adjusted_missing_by_code,
            "unknown_listing_missing_companies": len(unknown_listing_missing_by_code),
            "unknown_listing_missing_by_code": unknown_listing_missing_by_code,
            "pre_listing_observations_excluded": pre_listing_observations_excluded,
        }
    return {
        "schema_version": 2,
        "population": "SH_SZ_quote_universe",
        "classification_limit": "only independent listing-date evidence can narrow the expected annual window",
        "listing_date_evidence_count": len(set(population_codes) & set(listing_dates)),
        "listing_date_evidence_coverage": len(set(population_codes) & set(listing_dates))
        / max(len(population_codes), 1),
        "datasets": datasets,
    }


def _annual_history_profiles_comparable(previous: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Return whether historical-distribution regression checks share one window."""
    old_profile = previous.get("annual_history_profile")
    new_profile = candidate.get("annual_history_profile")
    if not isinstance(old_profile, Mapping) or not isinstance(new_profile, Mapping):
        return False
    old_datasets = old_profile.get("datasets")
    new_datasets = new_profile.get("datasets")
    if not isinstance(old_datasets, Mapping) or not isinstance(new_datasets, Mapping):
        return False
    signature_fields = ("max_periods", "min_observed_year", "max_observed_year")
    for dataset in _FINANCIAL_DATASET_RULES:
        old_entry = old_datasets.get(dataset)
        new_entry = new_datasets.get(dataset)
        if not isinstance(old_entry, Mapping) or not isinstance(new_entry, Mapping):
            return False
        if any(old_entry.get(field) != new_entry.get(field) for field in signature_fields):
            return False
    return True


def validate_market_snapshot(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    min_quotes: int = MIN_MARKET_QUOTES,
    min_financial_coverage: float = MIN_FINANCIAL_COVERAGE,
    min_market_cap_coverage: float = MIN_MARKET_CAP_COVERAGE,
    min_pe_coverage: float = MIN_PE_COVERAGE,
    min_pb_coverage: float = MIN_PB_COVERAGE,
    min_market_counts: Mapping[str, int] | None = None,
    as_of_timestamp: float | None = None,
    retrieval_reference_timestamp: float | None = None,
) -> Mapping[str, Any]:
    """Reject structurally partial or unsafe data before it can become active."""
    if not isinstance(quotes, pd.DataFrame):
        raise ValueError("quotes must be a pandas DataFrame")
    required = {
        "code",
        "name",
        "market",
        "price",
        "pe",
        "pb",
        "market_cap",
        *_QUOTE_PROVENANCE_COLUMNS,
    }
    missing = required - set(quotes.columns)
    if missing:
        raise ValueError(f"quotes missing required columns: {sorted(missing)}")

    markets = quotes["market"].map(lambda value: value.strip().upper() if isinstance(value, str) else "")
    invalid_markets = sorted(set(markets) - _ALLOWED_MARKETS)
    if invalid_markets:
        raise ValueError(f"quotes contain invalid market values: {invalid_markets}")
    analysis_market_mask = markets.isin(_ANALYSIS_MARKETS)
    analysis_market_quotes = int(analysis_market_mask.sum())
    if analysis_market_quotes < int(min_quotes):
        raise ValueError(f"SH/SZ quote count {analysis_market_quotes} is below required minimum {min_quotes}")

    codes = quotes["code"].map(lambda value: value.strip() if isinstance(value, str) else "")
    analysis_codes = codes[analysis_market_mask]
    if not analysis_codes.map(lambda value: bool(_A_SHARE_CODE.fullmatch(value))).all():
        raise ValueError("quotes contain invalid A-share code; expected six digits")
    if analysis_codes.duplicated(keep=False).any():
        raise ValueError("quotes contain duplicate bare codes")
    names = quotes["name"].map(lambda value: value.strip() if isinstance(value, str) else "")
    if names[analysis_market_mask].eq("").any():
        raise ValueError("quotes contain empty or invalid name")
    analysis_prices = quotes.loc[analysis_market_mask, "price"]
    if not _finite_positive_series(analysis_prices).all():
        raise ValueError("quotes contain missing, non-finite, or non-positive price")
    if (pd.to_numeric(analysis_prices, errors="coerce") > PRICE_MAX).any():
        raise ValueError(f"quotes contain price above source-integrity bound {PRICE_MAX:g}")

    statuses = quotes["quote_status"].map(lambda value: value.strip() if isinstance(value, str) else "")
    sources = quotes["price_source"].map(lambda value: value.strip() if isinstance(value, str) else "")
    analysis_statuses = statuses[analysis_market_mask]
    analysis_sources = sources[analysis_market_mask]
    invalid_statuses = sorted(set(analysis_statuses) - set(_ALLOWED_QUOTE_STATUS_SOURCE))
    if invalid_statuses:
        raise ValueError(f"quotes contain invalid quote_status values: {invalid_statuses}")
    invalid_sources = sorted(set(analysis_sources) - set(_ALLOWED_QUOTE_STATUS_SOURCE.values()))
    if invalid_sources:
        raise ValueError(f"quotes contain invalid price_source values: {invalid_sources}")
    expected_sources = analysis_statuses.map(_ALLOWED_QUOTE_STATUS_SOURCE)
    if not analysis_sources.eq(expected_sources).all():
        raise ValueError("quotes contain inconsistent quote_status/price_source pairs")

    reference_prices = pd.to_numeric(quotes["reference_price"], errors="coerce")
    display_prices = pd.to_numeric(quotes["price"], errors="coerce")
    analysis_reference_prices = reference_prices[analysis_market_mask]
    analysis_display_prices = display_prices[analysis_market_mask]
    if not _finite_positive_series(analysis_reference_prices).all():
        raise ValueError("quotes contain invalid reference_price")
    price_tolerance = analysis_reference_prices.abs().clip(lower=1.0) * 1e-10
    if ((analysis_display_prices - analysis_reference_prices).abs() > price_tolerance).any():
        raise ValueError("quotes price must equal the evidenced reference_price")

    trade_prices = pd.to_numeric(quotes["trade_price"], errors="coerce")
    trading_mask = analysis_market_mask & statuses.eq("trading")
    suspended_mask = analysis_market_mask & statuses.eq("suspended_or_no_trade")
    if trading_mask.any():
        trading_values = trade_prices[trading_mask]
        if not _finite_positive_series(trading_values).all():
            raise ValueError("trading quotes require a positive trade_price")
        trading_tolerance = reference_prices[trading_mask].abs().clip(lower=1.0) * 1e-10
        if ((trading_values - reference_prices[trading_mask]).abs() > trading_tolerance).any():
            raise ValueError("trading reference_price must equal trade_price")
    if suspended_mask.any() and _finite_positive_series(trade_prices[suspended_mask]).any():
        raise ValueError("suspended_or_no_trade quotes cannot contain a positive trade_price")
    trading_quotes = int(trading_mask.sum())
    trading_coverage = trading_quotes / max(analysis_market_quotes, 1)
    analysis_trading_quotes = trading_quotes
    analysis_trading_coverage = analysis_trading_quotes / max(analysis_market_quotes, 1)
    if trading_coverage < MIN_TRADING_QUOTE_COVERAGE:
        raise ValueError(
            f"SH/SZ trading quote coverage {trading_coverage:.1%} is below required {MIN_TRADING_QUOTE_COVERAGE:.1%}"
        )

    retrieved_values = pd.to_numeric(quotes["retrieved_at"], errors="coerce")
    analysis_retrieved_values = retrieved_values[analysis_market_mask]
    if not _finite_positive_series(analysis_retrieved_values).all():
        raise ValueError("quotes require a finite positive retrieved_at for every row")
    retrieval_oldest = float(analysis_retrieved_values.min())
    retrieval_latest = float(analysis_retrieved_values.max())
    retrieval_span = retrieval_latest - retrieval_oldest
    if retrieval_span > MAX_QUOTE_RETRIEVAL_SPAN_SECONDS:
        raise ValueError(f"quote retrieval span {retrieval_span:.1f}s exceeds {MAX_QUOTE_RETRIEVAL_SPAN_SECONDS:.1f}s")
    if as_of_timestamp is not None:
        reference_timestamp = _finite_number(as_of_timestamp)
        if reference_timestamp is None or reference_timestamp <= 0:
            raise ValueError("as_of_timestamp must be finite and positive")
        if (analysis_retrieved_values > reference_timestamp + MAX_FUTURE_SKEW_SECONDS).any():
            raise ValueError("quotes contain retrieval timestamps from the future")
    retrieval_reference = (
        _finite_number(retrieval_reference_timestamp)
        if retrieval_reference_timestamp is not None
        else _finite_number(as_of_timestamp)
    )
    if retrieval_reference_timestamp is not None and (retrieval_reference is None or retrieval_reference <= 0):
        raise ValueError("retrieval_reference_timestamp must be finite and positive")
    retrieval_age = None
    if retrieval_reference is not None:
        retrieval_age = max(0.0, retrieval_reference - retrieval_oldest)
        if retrieval_latest > retrieval_reference + MAX_FUTURE_SKEW_SECONDS:
            raise ValueError("quotes contain retrieval timestamps after their data generation")
        if retrieval_age > MAX_QUOTE_RETRIEVAL_AGE_SECONDS:
            raise ValueError(
                f"oldest quote retrieval is {retrieval_age:.1f}s before its data generation, "
                f"above {MAX_QUOTE_RETRIEVAL_AGE_SECONDS:.1f}s"
            )

    source_quote_timestamps: list[float] = []
    source_trade_dates: list[str] = []
    for source_date, tick_time in zip(
        quotes.loc[analysis_market_mask, "source_trade_date"],
        quotes.loc[analysis_market_mask, "quote_tick_time"],
    ):
        if source_date is None or pd.isna(source_date) or tick_time is None or pd.isna(tick_time):
            raise ValueError("quotes require a source_trade_date and quote_tick_time for every row")
        date_value = str(source_date).strip()
        time_value = str(tick_time).strip()
        if not date_value or not time_value:
            raise ValueError("quotes require a source_trade_date and quote_tick_time for every row")
        try:
            parsed_source_quote = _parse_canonical_quote_datetime(date_value, time_value)
        except ValueError as exc:
            raise ValueError(f"quotes contain invalid source date/time: {date_value} {time_value}") from exc
        source_timestamp = parsed_source_quote.timestamp()
        source_quote_timestamps.append(source_timestamp)
        source_trade_dates.append(date_value)
        if as_of_timestamp is not None:
            reference = float(as_of_timestamp)
            if source_timestamp > reference + MAX_FUTURE_SKEW_SECONDS:
                raise ValueError("quotes contain a source quote timestamp from the future")
            if reference - source_timestamp > MAX_SOURCE_QUOTE_AGE_SECONDS:
                raise ValueError("quotes contain a stale source quote timestamp")
    source_quote_oldest = min(source_quote_timestamps)
    source_quote_latest = max(source_quote_timestamps)
    source_quote_age = max(0.0, float(as_of_timestamp) - source_quote_oldest) if as_of_timestamp is not None else None
    trading_source_dates = {
        date_value
        for date_value, is_trading in zip(source_trade_dates, analysis_statuses.eq("trading"))
        if bool(is_trading)
    }
    if len(trading_source_dates) > 1:
        raise ValueError(f"trading quotes mix multiple source trade dates: {sorted(trading_source_dates)}")

    market_counts = {market: int((markets == market).sum()) for market in sorted(_ALLOWED_MARKETS)}
    quote_codes = {code for code in codes if _A_SHARE_CODE.fullmatch(code)}
    analysis_quote_codes = set(analysis_codes)
    required_counts = _required_market_counts(min_quotes, min_market_counts)
    for market, minimum in required_counts.items():
        if market_counts.get(market, 0) < minimum:
            raise ValueError(f"{market} quote count {market_counts.get(market, 0)} is below required minimum {minimum}")

    code_market_mismatch = ((markets == "SH") & ~codes.str.startswith("6")) | (
        (markets == "SZ") & ~codes.str.startswith(("0", "3"))
    )
    if code_market_mismatch.any():
        raise ValueError("quotes contain code/market identity mismatches")

    listing_date_evidence, listing_dates = _validate_listing_date_evidence(
        quotes,
        analysis_market_mask,
        as_of_timestamp=as_of_timestamp,
        full_market=analysis_market_quotes >= MIN_MARKET_QUOTES,
    )

    # Every investable-universe gate and distribution is calculated from
    # SH/SZ rows only. Optional BJ telemetry can be arbitrarily incomplete,
    # stale, suspended, or malformed without rejecting this generation.
    market_cap_valid = _finite_positive_series(quotes["market_cap"]) & analysis_market_mask
    market_cap_numeric = pd.to_numeric(quotes["market_cap"], errors="coerce")
    if (market_cap_numeric[market_cap_valid] > SINGLE_COMPANY_MARKET_CAP_MAX).any():
        raise ValueError(
            f"quotes contain single-company market_cap above source-integrity bound {SINGLE_COMPANY_MARKET_CAP_MAX:g}"
        )
    market_cap_coverage = float(market_cap_valid[analysis_market_mask].mean())
    pe_numeric = pd.to_numeric(quotes["pe"], errors="coerce")
    pb_numeric = pd.to_numeric(quotes["pb"], errors="coerce")
    pe_finite = _finite_series(pe_numeric)
    pb_finite = _finite_series(pb_numeric)
    pe_coverage = float(pe_finite[analysis_market_mask].mean())
    pb_coverage = float(pb_finite[analysis_market_mask].mean())
    for name, coverage, minimum in (
        ("positive market_cap", market_cap_coverage, min_market_cap_coverage),
        ("finite PE", pe_coverage, min_pe_coverage),
        ("finite PB", pb_coverage, min_pb_coverage),
    ):
        minimum_value = float(minimum)
        if not math.isfinite(minimum_value) or not 0 <= minimum_value <= 1:
            raise ValueError(f"minimum {name} coverage must be between 0 and 1")
        if coverage < minimum_value:
            raise ValueError(f"{name} coverage {coverage:.1%} is below required {minimum_value:.1%}")
    if (pe_numeric[pe_finite & analysis_market_mask].abs() > MAX_ABS_PE).any():
        raise ValueError(f"quotes contain PE outside the source-integrity bound +/-{MAX_ABS_PE:g}")
    if (pb_numeric[pb_finite & analysis_market_mask].abs() > MAX_ABS_PB).any():
        raise ValueError(f"quotes contain PB outside the source-integrity bound +/-{MAX_ABS_PB:g}")
    market_cap_distribution = _positive_distribution(market_cap_numeric[analysis_market_mask])
    price_distribution = _positive_distribution(quotes.loc[analysis_market_mask, "price"])
    pe_distribution = _positive_distribution(pe_numeric[analysis_market_mask])
    pb_distribution = _positive_distribution(pb_numeric[analysis_market_mask])
    if analysis_market_quotes >= MIN_MARKET_QUOTES:
        median_cap = market_cap_distribution["median"]
        total_cap = market_cap_distribution["total"]
        if not MARKET_CAP_MEDIAN_MIN <= median_cap <= MARKET_CAP_MEDIAN_MAX:
            raise ValueError(f"market_cap median is outside a plausible A-share range: {median_cap}")
        if not MARKET_CAP_TOTAL_MIN <= total_cap <= MARKET_CAP_TOTAL_MAX:
            raise ValueError(f"market_cap total is outside a plausible A-share range: {total_cap}")
        if not PE_POSITIVE_MEDIAN_MIN <= pe_distribution["median"] <= PE_POSITIVE_MEDIAN_MAX:
            raise ValueError(f"positive PE median is outside a plausible A-share range: {pe_distribution['median']}")
        if not PB_POSITIVE_MEDIAN_MIN <= pb_distribution["median"] <= PB_POSITIVE_MEDIAN_MAX:
            raise ValueError(f"positive PB median is outside a plausible A-share range: {pb_distribution['median']}")

    # Quality counters describe the investable SH/SZ universe.  Keep the raw
    # source counters separately so optional BJ telemetry cannot make a
    # healthy generation appear to gain/lose trading coverage downstream.
    quote_status_counts = analysis_statuses.value_counts().sort_index().to_dict()
    price_source_counts = analysis_sources.value_counts().sort_index().to_dict()
    source_quote_status_counts = statuses.value_counts().sort_index().to_dict()
    source_price_source_counts = sources.value_counts().sort_index().to_dict()
    retrieval_time_coverage = float(_finite_positive_series(analysis_retrieved_values).mean())

    if not isinstance(financials, Mapping):
        raise ValueError("financials must be a mapping")
    dataset_keys = {dataset: set() for dataset in _FINANCIAL_DATASET_RULES}
    current_dataset_keys = {dataset: set() for dataset in _FINANCIAL_DATASET_RULES}
    interim_current_keys = {dataset: set() for dataset in _INTERIM_DATASET_RULES}
    interim_comparative_keys = {dataset: set() for dataset in _INTERIM_DATASET_RULES}
    expected_annual_year = None
    expected_interim_report_date = None
    previous_interim_report_date = None
    reference_datetime = None
    if as_of_timestamp is not None:
        (
            expected_annual_year,
            expected_interim_report_date,
            previous_interim_report_date,
            reference_datetime,
        ) = _expected_report_periods(as_of_timestamp)
    reporting_period_contract = _reporting_period_contract(
        expected_annual_year,
        expected_interim_report_date,
        previous_interim_report_date,
    )
    expected_interim_month_day = (
        tuple(int(part) for part in expected_interim_report_date[5:].split("-"))
        if expected_interim_report_date is not None
        else None
    )
    annual_history_years: dict[str, dict[str, tuple[int, ...]]] = {dataset: {} for dataset in _FINANCIAL_DATASET_RULES}
    normalized_financial_keys: set[str] = set()
    supplemental_field_keys: dict[str, set[str]] = {
        "GOODWILL": set(),
        "OBTAIN_SUBSIDIARY_OTHER": set(),
    }
    for key, company in financials.items():
        if not isinstance(key, str) or not _A_SHARE_CODE.fullmatch(key):
            raise ValueError("financial mapping keys must be canonical six-digit A-share codes")
        # The fetcher may preserve source-only records for diagnostics.  Ignore
        # their internals outside the exact SH/SZ quote identity set; a
        # malformed BJ telemetry payload must not reject an otherwise complete
        # analysis generation.  The top-level identity remains canonical so a
        # whitespace/collision variant can never hide beside an SH/SZ record.
        if key not in analysis_quote_codes:
            continue
        if not isinstance(company, Mapping):
            raise ValueError(f"financial record {key!r} must be a mapping")
        normalized_key = key
        if normalized_key in normalized_financial_keys:
            raise ValueError(f"duplicate financial identity: {normalized_key}")
        normalized_financial_keys.add(normalized_key)
        balance_records = company.get("balance", [])
        if isinstance(balance_records, Mapping):
            balance_records = [balance_records]
        if isinstance(balance_records, (list, tuple)) and any(
            isinstance(record, Mapping) and "GOODWILL" in record for record in balance_records
        ):
            supplemental_field_keys["GOODWILL"].add(normalized_key)
        interim_cashflow_records = company.get("cashflow_interim", [])
        if isinstance(interim_cashflow_records, Mapping):
            interim_cashflow_records = [interim_cashflow_records]
        if isinstance(interim_cashflow_records, (list, tuple)) and any(
            isinstance(record, Mapping) and "OBTAIN_SUBSIDIARY_OTHER" in record for record in interim_cashflow_records
        ):
            supplemental_field_keys["OBTAIN_SUBSIDIARY_OTHER"].add(normalized_key)
        for dataset, (minimum_records, required_field_groups) in _FINANCIAL_DATASET_RULES.items():
            dates = _validated_usable_financial_dates(
                company,
                dataset,
                required_field_groups,
                company_code=normalized_key,
                reference_datetime=reference_datetime,
                allowed_month_days={(12, 31)},
            )
            years = sorted({item.year for item in dates})
            annual_history_years[dataset][normalized_key] = tuple(years)
            has_required_history = len(years) >= minimum_records
            if has_required_history and minimum_records > 1:
                latest_required_years = set(range(years[-1] - minimum_records + 1, years[-1] + 1))
                has_required_history = latest_required_years.issubset(years)
            if has_required_history:
                dataset_keys[dataset].add(normalized_key)
                if expected_annual_year is not None:
                    if any(item.year >= expected_annual_year for item in dates):
                        current_dataset_keys[dataset].add(normalized_key)

        for dataset, (legacy_q1_dataset, required_field_groups) in _INTERIM_DATASET_RULES.items():
            source_dataset = dataset
            records = company.get(dataset, [])
            if not isinstance(records, (list, tuple, Mapping)) or len(records) == 0:
                # Legacy Q1 is admissible only when Q1 is the period currently
                # expected. It can prove the current period but one row cannot
                # prove a year-on-year comparison.
                if expected_interim_month_day in (None, (3, 31)):
                    source_dataset = legacy_q1_dataset
            month_days = (
                (expected_interim_month_day,) if expected_interim_month_day is not None else ((3, 31), (6, 30), (9, 30))
            )
            dates = _validated_usable_financial_dates(
                company,
                source_dataset,
                required_field_groups,
                company_code=normalized_key,
                reference_datetime=reference_datetime,
                allowed_month_days=set(month_days),
            )
            report_dates = {item.strftime("%Y-%m-%d") for item in dates}
            if expected_interim_report_date is None:
                if report_dates:
                    latest = max(report_dates)
                    interim_current_keys[dataset].add(normalized_key)
                    previous = f"{int(latest[:4]) - 1}{latest[4:]}"
                    if previous in report_dates:
                        interim_comparative_keys[dataset].add(normalized_key)
            elif expected_interim_report_date in report_dates:
                interim_current_keys[dataset].add(normalized_key)
                if previous_interim_report_date in report_dates:
                    interim_comparative_keys[dataset].add(normalized_key)

    market_cap_by_code = {
        code: float(value)
        for code, value, is_analysis in zip(
            codes,
            pd.to_numeric(quotes["market_cap"], errors="coerce"),
            analysis_market_mask,
        )
        if bool(is_analysis) and pd.notna(value) and math.isfinite(float(value)) and float(value) > 0
    }
    analysis_financials = {code: company for code, company in financials.items() if code in analysis_quote_codes}
    annual_history_profile = _build_annual_history_profile(
        annual_history_years,
        analysis_quote_codes,
        listing_dates,
    )
    financial_value_distributions, financial_market_cap_ratio_distributions = _validate_financial_source_integrity(
        analysis_financials,
        market_cap_by_code,
        full_market=len(analysis_quote_codes) >= MIN_MARKET_QUOTES,
    )

    if not 0 <= float(min_financial_coverage) <= 1:
        raise ValueError("min_financial_coverage must be between 0 and 1")
    dataset_matches = {dataset: len(analysis_quote_codes & keys) for dataset, keys in dataset_keys.items()}
    dataset_matches.update(
        {dataset: len(analysis_quote_codes & keys) for dataset, keys in interim_current_keys.items()}
    )
    dataset_coverage = {
        dataset: matched_count / max(len(analysis_quote_codes), 1) for dataset, matched_count in dataset_matches.items()
    }
    for dataset, coverage in dataset_coverage.items():
        if coverage < float(min_financial_coverage):
            qualifier = "current " if dataset in _INTERIM_DATASET_RULES else ""
            raise ValueError(
                f"{qualifier}{dataset} coverage {coverage:.1%} is below required {float(min_financial_coverage):.1%}"
            )

    supplemental_field_coverage = {
        field: len(analysis_quote_codes & keys) / max(len(analysis_quote_codes), 1)
        for field, keys in supplemental_field_keys.items()
    }
    # These fields enrich optional Type3 acquisition evidence.  The provider
    # can legitimately return a requested column with null values.  Schema 8
    # records whether the requested columns were present so a schema-7 cache
    # created before this enrichment cannot silently masquerade as complete
    # deep-growth input.  A present true-null still remains unknown, never zero.

    matched_keys = set(analysis_quote_codes)
    for keys in (*dataset_keys.values(), *interim_current_keys.values()):
        matched_keys &= keys
    financial_coverage = len(matched_keys) / max(len(analysis_quote_codes), 1)
    if financial_coverage < float(min_financial_coverage):
        raise ValueError(
            f"joint financial coverage {financial_coverage:.1%} is below required {float(min_financial_coverage):.1%}"
        )

    tradable_codes = set(codes[analysis_market_mask & statuses.eq("trading")])
    reference_priced_codes = sorted(set(codes[analysis_market_mask & statuses.eq("suspended_or_no_trade")]))
    unpriced_codes: list[str] = []

    risk_exclusions: dict[str, str] = {}
    for code, name in zip(codes[analysis_market_mask], names[analysis_market_mask]):
        upper_name = name.upper()
        if name.endswith("退") or "退市" in name:
            risk_exclusions[code] = "delisting"
        elif re.match(r"^(?:S\*ST|\*ST|SST|ST)", upper_name):
            risk_exclusions[code] = "special_treatment"
    current_dataset_matches: dict[str, int] = {}
    current_dataset_coverage: dict[str, float] = {}
    current_financial_keys = set(matched_keys)
    if expected_annual_year is not None:
        current_dataset_matches = {
            dataset: len(analysis_quote_codes & keys) for dataset, keys in current_dataset_keys.items()
        }
        current_dataset_matches.update(
            {dataset: len(analysis_quote_codes & keys) for dataset, keys in interim_current_keys.items()}
        )
        current_dataset_coverage = {
            dataset: matched_count / max(len(analysis_quote_codes), 1)
            for dataset, matched_count in current_dataset_matches.items()
        }
        for dataset, coverage in current_dataset_coverage.items():
            if coverage < float(min_financial_coverage):
                raise ValueError(
                    f"current {dataset} coverage {coverage:.1%} is below required {float(min_financial_coverage):.1%}"
                )
        for keys in (*current_dataset_keys.values(), *interim_current_keys.values()):
            current_financial_keys &= keys
    valid_market_cap_codes = set(codes[market_cap_valid])
    analysis_market_codes = analysis_quote_codes
    unsupported_market_codes = quote_codes - analysis_market_codes
    eligible_keys = (
        current_financial_keys & tradable_codes & valid_market_cap_codes & analysis_market_codes - set(risk_exclusions)
    )
    comparative_interim_matches = {
        dataset: len(analysis_quote_codes & keys) for dataset, keys in interim_comparative_keys.items()
    }
    comparative_interim_coverage = {
        dataset: matched_count / max(len(analysis_quote_codes), 1)
        for dataset, matched_count in comparative_interim_matches.items()
    }
    comparative_missing_codes = sorted(
        matched_keys
        - set.intersection(
            set(matched_keys),
            *(set(keys) for keys in interim_comparative_keys.values()),
        )
    )
    comparative_financial_keys = set.intersection(
        set(matched_keys),
        *(set(keys) for keys in interim_comparative_keys.values()),
    )
    eligible_keys &= comparative_financial_keys
    from data.industry import classify_industries, industry_data_status

    industry_status = industry_data_status(quotes.loc[markets.isin(_ANALYSIS_MARKETS)])
    if not industry_status.get("loader_ok", industry_status.get("ok", False)):
        raise ValueError(f"industry data unavailable: {industry_status.get('error', 'unknown error')}")
    # DEFAULT is a conservative display fallback, not evidence for an
    # industry beta, margin benchmark, or cyclical classification.  Keep the
    # market snapshot usable when one board has weak source coverage, but
    # fail closed per company so no default benchmark enters valuation.
    industry_by_code = classify_industries(
        (code, name) for code, name in zip(codes, names) if code in analysis_market_codes
    )
    unclassified_industry_codes = {
        code for code, industry_code in industry_by_code.items() if industry_code == "DEFAULT"
    }
    financial_industry_codes = {
        code for code, industry_code in industry_by_code.items() if industry_code in _FINANCIAL_INDUSTRIES
    }
    nonfinancial_analysis_codes = analysis_market_codes - financial_industry_codes
    strict_ttm_source_coverage = dict(
        _strict_ttm_source_coverage(
            nonfinancial_analysis_codes,
            analysis_financials,
            market_cap_by_code,
            reporting_period_contract,
        )
    )
    strict_ttm_source_coverage["excluded_financial_codes"] = sorted(financial_industry_codes)
    if reporting_period_contract is not None:
        for metric in ("revenue", "fcff"):
            metric_coverage = strict_ttm_source_coverage[metric]["coverage"]
            if metric_coverage < float(min_financial_coverage):
                raise ValueError(
                    f"strict TTM {metric} source coverage {metric_coverage:.1%} is below required "
                    f"{float(min_financial_coverage):.1%}"
                )
    eligible_keys -= unclassified_industry_codes
    eligible_companies = len(eligible_keys)
    analysis_eligible_coverage = eligible_companies / max(analysis_market_quotes, 1)
    if analysis_market_quotes >= MIN_MARKET_QUOTES and analysis_eligible_coverage < MIN_ANALYSIS_ELIGIBLE_COVERAGE:
        raise ValueError(
            f"SH/SZ eligible analysis coverage {analysis_eligible_coverage:.1%} is below required "
            f"{MIN_ANALYSIS_ELIGIBLE_COVERAGE:.1%}"
        )
    analysis_exclusions: dict[str, str] = {}
    for code in sorted(quote_codes - eligible_keys):
        if code in risk_exclusions:
            analysis_exclusions[code] = risk_exclusions[code]
        elif code in unsupported_market_codes:
            analysis_exclusions[code] = "unsupported_market"
        elif code in reference_priced_codes:
            analysis_exclusions[code] = "suspended_or_no_trade"
        elif code not in valid_market_cap_codes:
            analysis_exclusions[code] = "invalid_market_cap"
        elif code not in matched_keys:
            analysis_exclusions[code] = "incomplete_financial_evidence"
        elif code not in current_financial_keys:
            analysis_exclusions[code] = "stale_or_incomplete_current_financials"
        elif code not in comparative_financial_keys:
            analysis_exclusions[code] = "missing_comparative_interim"
        elif code in unclassified_industry_codes:
            analysis_exclusions[code] = "unclassified_industry"
        else:
            analysis_exclusions[code] = "not_eligible"
    return {
        "quotes": len(quotes),
        "financials": len(analysis_financials),
        "source_financials": len(financials),
        "matched_financials": len(matched_keys),
        "financial_coverage": financial_coverage,
        "market_cap_coverage": market_cap_coverage,
        "pe_coverage": pe_coverage,
        "pb_coverage": pb_coverage,
        "market_cap_distribution": market_cap_distribution,
        "price_distribution": price_distribution,
        "pe_distribution": pe_distribution,
        "pb_distribution": pb_distribution,
        "financial_value_distributions": financial_value_distributions,
        "financial_market_cap_ratio_distributions": financial_market_cap_ratio_distributions,
        "quote_status_counts": quote_status_counts,
        "price_source_counts": price_source_counts,
        "source_quote_status_counts": source_quote_status_counts,
        "source_price_source_counts": source_price_source_counts,
        "trading_quotes": trading_quotes,
        "trading_coverage": trading_coverage,
        "analysis_market_quotes": analysis_market_quotes,
        "analysis_trading_quotes": analysis_trading_quotes,
        "analysis_trading_coverage": analysis_trading_coverage,
        "eligible_companies": eligible_companies,
        "analysis_eligible_coverage": analysis_eligible_coverage,
        "retrieval_time_coverage": retrieval_time_coverage,
        "retrieval_time_oldest": retrieval_oldest,
        "retrieval_time_latest": retrieval_latest,
        "retrieval_time_span_seconds": retrieval_span,
        "retrieval_age_seconds": retrieval_age,
        "source_quote_timestamp_oldest": source_quote_oldest,
        "source_quote_timestamp_latest": source_quote_latest,
        "source_quote_age_seconds": source_quote_age,
        "trading_source_trade_dates": sorted(trading_source_dates),
        "listing_date_evidence": listing_date_evidence,
        "market_counts": market_counts,
        "dataset_matches": dataset_matches,
        "dataset_coverage": dataset_coverage,
        "supplemental_field_coverage": supplemental_field_coverage,
        "current_dataset_matches": current_dataset_matches,
        "current_dataset_coverage": current_dataset_coverage,
        "expected_annual_year": expected_annual_year,
        "expected_interim_report_date": expected_interim_report_date,
        "previous_interim_report_date": previous_interim_report_date,
        "reporting_period_contract": reporting_period_contract,
        "annual_history_profile": annual_history_profile,
        "strict_ttm_source_coverage": strict_ttm_source_coverage,
        "comparative_interim_matches": comparative_interim_matches,
        "comparative_interim_coverage": comparative_interim_coverage,
        "comparative_missing_codes": comparative_missing_codes,
        "financially_eligible_codes": sorted(current_financial_keys),
        "structurally_matched_financial_codes": sorted(matched_keys),
        "invalid_market_cap_codes": sorted(quote_codes - valid_market_cap_codes),
        "analysis_markets": sorted(_ANALYSIS_MARKETS),
        "unsupported_market_codes": sorted(unsupported_market_codes),
        "unclassified_industry_codes": sorted(unclassified_industry_codes),
        "eligible_codes": sorted(eligible_keys),
        "ineligible_codes": sorted(quote_codes - eligible_keys),
        "reference_priced_codes": reference_priced_codes,
        "unpriced_codes": unpriced_codes,
        "excluded_risk_codes": dict(sorted(risk_exclusions.items())),
        "analysis_exclusions": analysis_exclusions,
        "industry_status": industry_status,
    }


def _validate_timestamp(
    timestamp: Any,
    *,
    now: float,
    enforce_stale_limit: bool,
    max_stale_age: float,
) -> float:
    value = _finite_number(timestamp)
    current = _finite_number(now)
    if value is None or value <= 0 or current is None or current <= 0:
        raise ValueError("data_timestamp must be finite and positive")
    if value > current + MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("data_timestamp is implausibly far in the future")
    if enforce_stale_limit and current - value > float(max_stale_age):
        raise ValueError("data_timestamp exceeds maximum stale age")
    return value


def _validate_relative_generation(candidate: Mapping[str, Any], previous: Mapping[str, Any]) -> None:
    checks = (
        ("analysis_market_quotes", MIN_RELATIVE_QUOTE_RATIO),
        ("trading_quotes", MIN_RELATIVE_TRADING_RATIO),
        ("analysis_trading_quotes", MIN_RELATIVE_TRADING_RATIO),
    )
    for key, minimum_ratio in checks:
        old = int(previous.get(key, 0))
        new = int(candidate.get(key, 0))
        if old > 0 and new < old * minimum_ratio:
            raise ValueError(f"relative {key} drop is too large: {old} -> {new}")
    old_markets = previous.get("market_counts", {})
    new_markets = candidate.get("market_counts", {})
    if isinstance(old_markets, Mapping) and isinstance(new_markets, Mapping):
        for market in sorted(_ANALYSIS_MARKETS):
            old_count = int(old_markets.get(market, 0))
            new_count = int(new_markets.get(market, 0))
            if old_count > 0 and new_count < old_count * MIN_RELATIVE_MARKET_RATIO:
                raise ValueError(f"relative {market} quote drop is too large: {old_count} -> {new_count}")
    for section in (
        "dataset_matches",
        "current_dataset_matches",
        "comparative_interim_matches",
    ):
        old_datasets = previous.get(section, {})
        new_datasets = candidate.get(section, {})
        if not isinstance(old_datasets, Mapping) or not isinstance(new_datasets, Mapping):
            continue
        for dataset, old_value in old_datasets.items():
            old_count = int(old_value)
            new_count = int(new_datasets.get(dataset, 0))
            if old_count > 0 and new_count < old_count * MIN_RELATIVE_FINANCIAL_RATIO:
                raise ValueError(f"relative {dataset} drop is too large: {old_count} -> {new_count}")
    old_matched = int(previous.get("matched_financials", 0))
    new_matched = int(candidate.get("matched_financials", 0))
    if old_matched > 0 and new_matched < old_matched * MIN_RELATIVE_FINANCIAL_RATIO:
        raise ValueError(f"relative matched_financials drop is too large: {old_matched} -> {new_matched}")
    old_eligible = int(previous.get("eligible_companies", 0))
    new_eligible = int(candidate.get("eligible_companies", 0))
    if old_eligible > 0 and new_eligible < old_eligible * MIN_RELATIVE_FINANCIAL_RATIO:
        raise ValueError(f"relative eligible_companies drop is too large: {old_eligible} -> {new_eligible}")

    old_ttm = previous.get("strict_ttm_source_coverage", {})
    new_ttm = candidate.get("strict_ttm_source_coverage", {})
    if isinstance(old_ttm, Mapping) and isinstance(new_ttm, Mapping):
        for metric in ("revenue", "fcff"):
            old_metric = old_ttm.get(metric, {})
            new_metric = new_ttm.get(metric, {})
            if not isinstance(old_metric, Mapping) or not isinstance(new_metric, Mapping):
                continue
            old_complete = int(old_metric.get("complete", 0))
            new_complete = int(new_metric.get("complete", 0))
            if old_complete > 0 and new_complete < old_complete * MIN_RELATIVE_FINANCIAL_RATIO:
                raise ValueError(
                    f"relative strict TTM {metric} complete drop is too large: {old_complete} -> {new_complete}"
                )
            old_coverage = _finite_number(old_metric.get("coverage"))
            new_coverage = _finite_number(new_metric.get("coverage"))
            if old_coverage is not None and old_coverage > 0:
                if new_coverage is None or new_coverage < old_coverage * MIN_RELATIVE_FIELD_COVERAGE:
                    raise ValueError(
                        f"relative strict TTM {metric} coverage drop is too large: {old_coverage} -> {new_coverage}"
                    )

    for key in (
        "market_cap_coverage",
        "pe_coverage",
        "pb_coverage",
        "trading_coverage",
        "analysis_trading_coverage",
        "analysis_eligible_coverage",
        "retrieval_time_coverage",
    ):
        old_value = _finite_number(previous.get(key))
        new_value = _finite_number(candidate.get(key))
        if old_value is not None and old_value > 0:
            if new_value is None or new_value < old_value * MIN_RELATIVE_FIELD_COVERAGE:
                raise ValueError(f"relative {key} drop is too large: {old_value} -> {new_value}")

    for section in (
        "market_cap_distribution",
        "price_distribution",
        "pe_distribution",
        "pb_distribution",
    ):
        old_distribution = previous.get(section, {})
        new_distribution = candidate.get(section, {})
        if not isinstance(old_distribution, Mapping) or not isinstance(new_distribution, Mapping):
            continue
        statistics = (
            ("median", "p01", "p99", "total")
            if section
            in {
                "market_cap_distribution",
                "price_distribution",
            }
            else ("median", "p01", "p99")
        )
        for statistic in statistics:
            old_value = _finite_number(old_distribution.get(statistic))
            new_value = _finite_number(new_distribution.get(statistic))
            if old_value is None or old_value <= 0 or new_value is None or new_value <= 0:
                raise ValueError(f"relative {section}.{statistic} is unavailable")
            ratio = new_value / old_value
            if not MIN_RELATIVE_VALUE_RATIO <= ratio <= MAX_RELATIVE_VALUE_RATIO:
                raise ValueError(f"relative {section}.{statistic} ratio is implausible: {ratio:.4f}")

    # Historical value distributions are comparable only under the same
    # observed annual window.  Expanding the acquisition contract from five to
    # ten years legitimately lowers old-company p01 values; treating that as a
    # unit regression blocks the very evidence upgrade being validated.  The
    # absolute source-integrity gates above still run for every observation.
    if _annual_history_profiles_comparable(previous, candidate):
        for section in (
            "financial_value_distributions",
            "financial_market_cap_ratio_distributions",
        ):
            old_metrics = previous.get(section, {})
            new_metrics = candidate.get(section, {})
            if not isinstance(old_metrics, Mapping) or not isinstance(new_metrics, Mapping):
                continue
            for metric, old_distribution in old_metrics.items():
                new_distribution = new_metrics.get(metric, {})
                if not isinstance(old_distribution, Mapping) or not isinstance(new_distribution, Mapping):
                    raise ValueError(f"relative {section}.{metric} distribution is unavailable")
                for statistic in ("median", "p01", "p99"):
                    old_value = _finite_number(old_distribution.get(statistic))
                    new_value = _finite_number(new_distribution.get(statistic))
                    if old_value == 0 and new_value == 0:
                        continue
                    if old_value is None or old_value <= 0 or new_value is None or new_value <= 0:
                        raise ValueError(f"relative {section}.{metric}.{statistic} is unavailable")
                    ratio = new_value / old_value
                    if not MIN_RELATIVE_VALUE_RATIO <= ratio <= MAX_RELATIVE_VALUE_RATIO:
                        raise ValueError(f"relative {section}.{metric}.{statistic} ratio is implausible: {ratio:.4f}")


def _payload_sha256(value: Any) -> str:
    """Use the cache's canonical payload encoding for generation identity."""
    encoded = _encode_json_value(value)
    return hashlib.sha256(_canonical_json_bytes(encoded)).hexdigest()


def _loaded_payload_sha256(loaded: Any) -> str | None:
    metadata = getattr(loaded, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("payload_sha256")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return None
    return value


def _loaded_artifact_sha256(loaded: Any) -> str | None:
    metadata = getattr(loaded, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("artifact_sha256")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return None
    return value


def save_market_snapshot(
    cache: SafeFileCache,
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    data_timestamp: float,
    min_quotes: int = MIN_MARKET_QUOTES,
    min_financial_coverage: float = MIN_FINANCIAL_COVERAGE,
    now: float | None = None,
    retrieved_at: float | None = None,
    analysis_quality: Mapping[str, Any] | None = None,
    expected_previous_timestamp: float | None | object = _EXPECTED_UNSET,
    expected_previous_payload_sha256: str | None | object = _EXPECTED_UNSET,
) -> Mapping[str, Any]:
    now_value = time.time() if now is None else float(now)
    timestamp = _validate_timestamp(
        data_timestamp,
        now=now_value,
        enforce_stale_limit=False,
        max_stale_age=MAX_STALE_AGE_SECONDS,
    )
    validation = validate_market_snapshot(
        quotes,
        financials,
        min_quotes=min_quotes,
        min_financial_coverage=min_financial_coverage,
        as_of_timestamp=now_value,
        retrieval_reference_timestamp=timestamp,
    )
    quote_retrieved = _quote_retrieved_at(quotes)
    if quote_retrieved is None:
        raise ValueError("quotes do not contain a defensible retrieval timestamp")
    declared_retrieved = _finite_number(retrieved_at)
    if retrieved_at is not None and (
        declared_retrieved is None or not math.isclose(declared_retrieved, quote_retrieved, rel_tol=0.0, abs_tol=1e-6)
    ):
        raise ValueError("retrieved_at must equal the oldest validated quote retrieval timestamp")
    retrieved = quote_retrieved
    payload = {
        "quotes": quotes,
        "financials": dict(financials),
        "data_timestamp": timestamp,
        "retrieved_at": retrieved,
        "validation": dict(validation),
        "analysis_quality": dict(analysis_quality or {}),
    }
    candidate_payload_sha256 = _payload_sha256(payload)

    # SafeFileCache guarantees atomic bytes; this outer generation lock makes
    # the read/compare/write decision atomic for every snapshot promotion.
    promotion_path = cache.path.with_name(cache.path.name + ".promotion.lock")
    with _thread_lock_for(promotion_path), _cross_process_lock(promotion_path):
        loaded = cache.load(allow_expired=True)
        current_timestamp = None
        current_payload_sha256 = _loaded_payload_sha256(loaded)
        current_artifact_sha256 = _loaded_artifact_sha256(loaded)
        if loaded.hit and isinstance(loaded.value, Mapping):
            current_timestamp = _finite_number(loaded.value.get("data_timestamp"))
            current_quotes = loaded.value.get("quotes")
            current_quote_retrieved = (
                _quote_retrieved_at(current_quotes) if isinstance(current_quotes, pd.DataFrame) else None
            )
        else:
            current_quote_retrieved = None
        if current_timestamp is not None and timestamp < current_timestamp:
            raise SnapshotGenerationConflict(
                f"candidate generation {timestamp} is older than active generation {current_timestamp}"
            )
        if current_quote_retrieved is not None and retrieved < current_quote_retrieved - 1e-6:
            raise SnapshotGenerationConflict("candidate quote retrieval regressed behind the active generation")
        if (
            current_timestamp is not None
            and math.isclose(timestamp, current_timestamp, rel_tol=0.0, abs_tol=1e-6)
            and current_payload_sha256 != candidate_payload_sha256
        ):
            raise SnapshotGenerationConflict(
                "candidate has the same data_timestamp as the active generation but a different payload"
            )
        if current_timestamp is not None and (
            expected_previous_timestamp is _EXPECTED_UNSET or expected_previous_payload_sha256 is _EXPECTED_UNSET
        ):
            raise ValueError(
                "both expected_previous_timestamp and expected_previous_payload_sha256 are required "
                "when an active generation exists"
            )
        if expected_previous_timestamp is not _EXPECTED_UNSET:
            expected = _finite_number(expected_previous_timestamp)
            matches = (
                expected is None
                and current_timestamp is None
                or expected is not None
                and current_timestamp is not None
                and math.isclose(expected, current_timestamp, rel_tol=0.0, abs_tol=1e-6)
            )
            if not matches:
                raise SnapshotGenerationConflict(
                    f"active generation changed: expected {expected}, found {current_timestamp}"
                )
        if expected_previous_payload_sha256 is not _EXPECTED_UNSET:
            expected_hash = expected_previous_payload_sha256
            if expected_hash is not None and (
                not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            ):
                raise ValueError("expected_previous_payload_sha256 must be a lowercase SHA-256 or None")
            if expected_hash != current_payload_sha256:
                raise SnapshotGenerationConflict(
                    f"active generation payload changed: expected {expected_hash}, found {current_payload_sha256}"
                )
        try:
            return cache.compare_and_swap(
                payload,
                expected_payload_sha256=current_payload_sha256,
                expected_artifact_sha256=current_artifact_sha256,
                allow_replace_invalid=True,
            )
        except SafeCacheConflict as exc:
            raise SnapshotGenerationConflict("active generation changed between validation and promotion") from exc


def _cached_outcome(
    cache: SafeFileCache,
    *,
    allow_expired: bool,
    source: str,
    now: float,
    enforce_stale_limit: bool,
    max_stale_age: float,
    warning: str = "",
    min_quotes: int,
    min_financial_coverage: float,
) -> tuple[MarketSnapshotOutcome | None, Mapping[str, Any]]:
    loaded = cache.load(allow_expired=allow_expired)
    if not loaded.hit:
        return None, {
            "stage": "cache_load",
            "reason": loaded.reason or "cache_miss",
            "active_payload_sha256": _loaded_payload_sha256(loaded),
            "active_artifact_sha256": _loaded_artifact_sha256(loaded),
        }
    if not isinstance(loaded.value, Mapping):
        return None, {"stage": "cache_load", "reason": "payload_not_mapping"}
    raw_timestamp = _finite_number(loaded.value.get("data_timestamp"))
    raw_payload_sha256 = _loaded_payload_sha256(loaded)
    raw_identity = {
        "active_timestamp": raw_timestamp,
        "active_payload_sha256": raw_payload_sha256,
        "active_artifact_sha256": _loaded_artifact_sha256(loaded),
    }
    try:
        quotes = loaded.value["quotes"]
        financials = loaded.value["financials"]
        timestamp = _validate_timestamp(
            loaded.value["data_timestamp"],
            now=now,
            enforce_stale_limit=enforce_stale_limit,
            max_stale_age=max_stale_age,
        )
        validation = validate_market_snapshot(
            quotes,
            financials,
            min_quotes=min_quotes,
            min_financial_coverage=min_financial_coverage,
            # Required filing periods advance with wall-clock time, not with
            # the acquisition time embedded in an old cache generation.
            as_of_timestamp=now,
            # Quote freshness, however, is a property of the stored
            # generation.  A still-admissible last-known-good cache must not
            # fail merely because wall-clock time has advanced.
            retrieval_reference_timestamp=timestamp,
        )
        retrieved = _quote_retrieved_at(quotes)
        if retrieved is None:
            raise ValueError("cached quotes lack a defensible retrieval timestamp")
        declared_retrieved = _finite_number(loaded.value.get("retrieved_at"))
        if declared_retrieved is None:
            raise ValueError("cached snapshot lacks required top-level retrieved_at")
        if not math.isclose(declared_retrieved, retrieved, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("cached retrieved_at differs from quote retrieval evidence")
        quality = loaded.value.get("analysis_quality", {})
        if not isinstance(quality, Mapping):
            quality = {}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return None, {
            "stage": "cache_validation",
            "reason": type(exc).__name__,
            "detail": str(exc),
            **raw_identity,
        }
    outcome = MarketSnapshotOutcome(
        quotes,
        financials,
        timestamp,
        source,
        warning,
        validation,
        retrieved_at=retrieved,
        baseline_timestamp=timestamp,
        baseline_payload_sha256=_loaded_payload_sha256(loaded),
        analysis_quality=dict(quality),
    )
    return outcome, raw_identity


def _migrate_schema4_snapshot(
    active_cache: SafeFileCache,
    *,
    now: float,
    max_stale_age: float,
    min_quotes: int,
    min_financial_coverage: float,
) -> MarketSnapshotOutcome | None:
    """Revalidate and atomically promote a legacy schema-4 generation.

    Schema 8 requires the reporting-period gates added by schema 5, capex
    provenance added by schema 6, independently sourced listing dates added by
    schema 7, and explicit Type3 supplemental-field presence.  A fresh,
    checksummed schema-4 generation can therefore be reused only in the unusual
    case where its payload already proves every current invariant.  Schemas 5,
    6 and 7 are intentionally not auto-migrated because their contracts did not
    require all current evidence.
    """
    probe = active_cache.load(allow_expired=True)
    metadata = probe.metadata if isinstance(probe.metadata, Mapping) else {}
    if probe.reason != "schema_version_mismatch" or metadata.get("schema_version") != 4:
        return None
    legacy_cache = SafeFileCache(
        active_cache.path,
        schema_version=4,
        ttl=active_cache.ttl,
        max_uncompressed_bytes=active_cache.max_uncompressed_bytes,
    )
    legacy, _diagnostic = _cached_outcome(
        legacy_cache,
        allow_expired=True,
        source="schema4_candidate",
        now=now,
        enforce_stale_limit=True,
        max_stale_age=max_stale_age,
        min_quotes=min_quotes,
        min_financial_coverage=min_financial_coverage,
    )
    if legacy is None or not isinstance(legacy.baseline_payload_sha256, str):
        return None
    supplemental = legacy.validation.get("supplemental_field_coverage")
    if not isinstance(supplemental, Mapping) or any(
        _finite_number(supplemental.get(field)) != 1.0 for field in ("GOODWILL", "OBTAIN_SUBSIDIARY_OTHER")
    ):
        return None
    try:
        save_market_snapshot(
            active_cache,
            legacy.quotes,
            legacy.financials,
            data_timestamp=legacy.data_timestamp,
            min_quotes=min_quotes,
            min_financial_coverage=min_financial_coverage,
            now=now,
            retrieved_at=legacy.retrieved_at,
            analysis_quality=legacy.analysis_quality,
            expected_previous_payload_sha256=legacy.baseline_payload_sha256,
        )
    except (SnapshotGenerationConflict, SafeCacheError, ValueError):
        return None
    migrated, _identity = _cached_outcome(
        active_cache,
        allow_expired=False,
        source="migrated_cache",
        warning="schema4 cache revalidated and migrated to schema8",
        now=now,
        enforce_stale_limit=True,
        max_stale_age=max_stale_age,
        min_quotes=min_quotes,
        min_financial_coverage=min_financial_coverage,
    )
    return migrated


def get_market_snapshot(
    fetcher: Any,
    cache: SafeFileCache | None = None,
    *,
    force_refresh: bool = False,
    allow_expired_cache: bool = False,
    persist_network: bool = True,
    min_quotes: int = MIN_MARKET_QUOTES,
    min_financial_coverage: float = MIN_FINANCIAL_COVERAGE,
    max_stale_age: float = MAX_STALE_AGE_SECONDS,
    clock: Callable[[], float] = time.time,
) -> MarketSnapshotOutcome:
    """Load active data or produce a validated candidate with bounded fallback.

    UI callers use ``persist_network=False`` and promote only after the complete
    valuation/scoring pipeline succeeds. Command-line callers may keep the
    default immediate promotion when validation is their terminal gate.
    """
    active_cache = cache or SafeFileCache(DEFAULT_SNAPSHOT_PATH, schema_version=SNAPSHOT_SCHEMA_VERSION)
    if not isinstance(force_refresh, bool) or not isinstance(allow_expired_cache, bool):
        raise TypeError("snapshot refresh and cache replay options must be boolean")
    now = float(clock())
    if not force_refresh:
        cached, cache_diagnostic = _cached_outcome(
            active_cache,
            # A release audit may deliberately replay a cache whose routine
            # acquisition TTL has elapsed.  The embedded market timestamp is
            # still checked below against ``max_stale_age``, so this cannot
            # turn an arbitrarily old generation into admissible data.
            allow_expired=allow_expired_cache,
            source="cache",
            now=now,
            enforce_stale_limit=True,
            max_stale_age=max_stale_age,
            min_quotes=min_quotes,
            min_financial_coverage=min_financial_coverage,
        )
        if cached is not None:
            return cached
        migrated = _migrate_schema4_snapshot(
            active_cache,
            now=now,
            max_stale_age=max_stale_age,
            min_quotes=min_quotes,
            min_financial_coverage=min_financial_coverage,
        )
        if migrated is not None:
            return migrated
    else:
        cache_diagnostic = {}

    # A structurally valid prior generation remains the relative-count baseline
    # even if it is too old to serve to users.
    baseline, baseline_diagnostic = _cached_outcome(
        active_cache,
        allow_expired=True,
        source="baseline",
        now=now,
        enforce_stale_limit=False,
        max_stale_age=max_stale_age,
        min_quotes=min_quotes,
        min_financial_coverage=min_financial_coverage,
    )
    if baseline is not None:
        previous_timestamp = baseline.data_timestamp
        previous_payload_sha256 = baseline.baseline_payload_sha256
    else:
        previous_timestamp = _finite_number(baseline_diagnostic.get("active_timestamp"))
        previous_payload_sha256 = baseline_diagnostic.get("active_payload_sha256")
        if not isinstance(previous_payload_sha256, str):
            previous_payload_sha256 = None
    active_exists = previous_payload_sha256 is not None
    if not cache_diagnostic:
        cache_diagnostic = baseline_diagnostic
    try:
        quotes = fetcher.get_stock_list(include_hk=False)
        if not isinstance(quotes, pd.DataFrame) or not {"code", "market"}.issubset(quotes.columns):
            raise ValueError("quote source must provide code and market columns")
        analysis_mask = quotes["market"].map(
            lambda value: isinstance(value, str) and value.strip().upper() in _ANALYSIS_MARKETS
        )
        codes = quotes.loc[analysis_mask, "code"].tolist()
        if not codes:
            raise ValueError("quote source returned no Shanghai/Shenzhen securities")
        financials = fetcher.get_financials(codes=codes)
        timestamp = float(clock())
        timestamp = _validate_timestamp(
            timestamp,
            now=timestamp,
            enforce_stale_limit=False,
            max_stale_age=max_stale_age,
        )
        validation = validate_market_snapshot(
            quotes,
            financials,
            min_quotes=min_quotes,
            min_financial_coverage=min_financial_coverage,
            as_of_timestamp=timestamp,
            retrieval_reference_timestamp=timestamp,
        )
        retrieved = _quote_retrieved_at(quotes)
        if (
            retrieved is not None
            and baseline is not None
            and baseline.retrieved_at is not None
            and retrieved < baseline.retrieved_at - 1e-6
        ):
            raise ValueError("quote retrieval time regressed behind the active generation")
        if baseline is not None:
            _validate_relative_generation(validation, baseline.validation)
        if persist_network:
            save_market_snapshot(
                active_cache,
                quotes,
                financials,
                data_timestamp=timestamp,
                min_quotes=min_quotes,
                min_financial_coverage=min_financial_coverage,
                now=timestamp,
                retrieved_at=retrieved,
                expected_previous_timestamp=(previous_timestamp if active_exists else _EXPECTED_UNSET),
                expected_previous_payload_sha256=(previous_payload_sha256 if active_exists else _EXPECTED_UNSET),
            )
        return MarketSnapshotOutcome(
            quotes,
            financials,
            timestamp,
            "network",
            "",
            validation,
            retrieved_at=retrieved,
            baseline_timestamp=previous_timestamp,
            baseline_payload_sha256=previous_payload_sha256,
            previous_analysis_quality=(dict(baseline.analysis_quality) if baseline is not None else {}),
            cache_diagnostic=dict(cache_diagnostic),
        )
    except Exception as exc:
        warning = f"refresh failed: {type(exc).__name__}: {exc}"
        fallback, fallback_diagnostic = _cached_outcome(
            active_cache,
            allow_expired=True,
            source="stale_cache",
            warning=warning,
            now=float(clock()),
            enforce_stale_limit=True,
            max_stale_age=max_stale_age,
            min_quotes=min_quotes,
            min_financial_coverage=min_financial_coverage,
        )
        if fallback is not None:
            return fallback
        diagnostic = fallback_diagnostic or cache_diagnostic
        detail = f"; cache={diagnostic}" if diagnostic else ""
        raise SnapshotUnavailableError(warning + detail) from exc
