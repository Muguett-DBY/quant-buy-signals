"""Pure, bounded orchestration for valuation and seven-type screening."""

from __future__ import annotations

import math
import os
import statistics
from collections.abc import Callable, Iterable, Mapping
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from config import BAND_WACC_DELTA, FCF_MARGIN_FLOOR, FCF_MARGIN_LONG_TERM, FORECAST_YEARS
from engine.dcf import ReportingPeriodContract
from engine.valuation_status import (
    DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
    DCF_SKIP_INCONSISTENT_SOURCE,
    DCF_SKIP_INTERNAL_ERROR,
    DCF_SKIP_MODEL_UNSUPPORTED,
    DCF_SKIP_SOURCE_MISSING,
    make_dcf_skip_classification,
)


DEFAULT_DCF_WORKERS = min(8, max(1, os.cpu_count() or 1))
MIN_SCORE_COVERAGE = 0.90
MIN_DCF_ATTEMPT_COVERAGE = 0.90
MIN_DCF_VALID_COVERAGE = 0.25
MAX_PIPELINE_ISSUE_RATE = 0.01
MIN_RELATIVE_ANALYSIS_RATIO = 0.90


@dataclass(frozen=True)
class PipelineIssue:
    code: str
    stage: str
    message: str


class AnalysisQualityError(ValueError):
    """A completed analysis failed absolute or relative production gates."""

    def __init__(self, reasons: Iterable[Mapping[str, Any]], metrics: Mapping[str, Any]):
        self.reasons = tuple(dict(reason) for reason in reasons)
        self.metrics = dict(metrics)
        message = "; ".join(str(reason.get("message", "quality gate failed")) for reason in self.reasons)
        super().__init__(message or "analysis quality gate failed")


@dataclass(frozen=True)
class DcfBatchOutcome:
    results: Mapping[str, Mapping[str, Any]]
    issues: tuple[PipelineIssue, ...]
    attempted: int
    skipped: int
    skip_reasons: Mapping[str, str] = field(default_factory=dict)
    skip_classifications: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketAnalysisOutcome:
    scores: pd.DataFrame
    dcf_results: Mapping[str, Mapping[str, Any]]
    issues: tuple[PipelineIssue, ...]
    dcf_attempted: int
    dcf_skipped: int
    dcf_skip_reasons: Mapping[str, str] = field(default_factory=dict)
    quality: Mapping[str, Any] = field(default_factory=dict)
    dcf_skip_classifications: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    quality_history_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _recompute_dcf_per_share(
    *,
    base_fcf: float,
    base_revenue: float,
    growth: float,
    wacc: float,
    terminal_growth: float,
    shares: float,
    net_debt: float,
    retention: float,
) -> float | None:
    """Independent scalar implementation of the public two-stage DCF formula."""
    if (
        base_fcf <= 0
        or base_revenue <= 0
        or shares <= 0
        or growth <= -1
        or wacc <= terminal_growth
        or not 0 <= retention <= 1
    ):
        return None
    current_margin = base_fcf / base_revenue
    if not 0 < current_margin <= 1:
        return None
    target_margin = min(
        current_margin,
        max(float(FCF_MARGIN_FLOOR), float(FCF_MARGIN_LONG_TERM), current_margin * retention),
    )
    explicit_value = 0.0
    final_revenue = base_revenue
    final_discount = 1.0
    for year in range(1, int(FORECAST_YEARS) + 1):
        revenue = base_revenue * (1 + growth) ** year
        interpolation = (year - 1) / max(int(FORECAST_YEARS) - 1, 1)
        margin = current_margin + (target_margin - current_margin) * interpolation
        discount = (1 + wacc) ** year
        if not all(math.isfinite(value) for value in (revenue, margin, discount)) or discount <= 0:
            return None
        explicit_value += revenue * margin / discount
        final_revenue, final_discount = revenue, discount
    terminal_fcf = final_revenue * (1 + terminal_growth) * target_margin
    equity_value = explicit_value + terminal_fcf / (wacc - terminal_growth) / final_discount - net_debt
    per_share = equity_value / shares
    return per_share if math.isfinite(per_share) and per_share > 0 else None


def _valuation_payload_error(
    expected_code: str,
    result: Any,
    *,
    expected_price: Any = None,
    expected_shares: Any = None,
    require_strict_ttm: bool = False,
) -> str | None:
    """Validate the common public contract shared by DCF and justified P/B."""
    if not isinstance(result, Mapping):
        return "valuation result is not a mapping"
    if _normalise_code(result.get("code")) != _normalise_code(expected_code):
        return "valuation payload code differs from its company identity"
    price = _finite_positive(result.get("current_price"))
    if price is None:
        return "valuation current_price is missing or invalid"
    reference_price = _finite_positive(expected_price)
    if reference_price is not None and not math.isclose(price, reference_price, rel_tol=1e-10, abs_tol=1e-8):
        return "valuation current_price differs from quote evidence"
    if not str(result.get("industry_code") or "").strip():
        return "valuation industry_code is missing"
    base_rate = _finite_positive(result.get("base_wacc"))
    if base_rate is None:
        return "valuation discount rate is missing or invalid"
    shares = _finite_positive(result.get("shares_outstanding"))
    if shares is None:
        return "valuation shares_outstanding is missing or invalid"
    reference_shares = _finite_positive(expected_shares)
    if reference_shares is not None and not math.isclose(
        shares,
        reference_shares,
        rel_tol=1e-10,
        abs_tol=max(1e-8, reference_shares * 1e-10),
    ):
        return "valuation shares_outstanding differs from quote-derived evidence"

    points = result.get("dcf_points")
    params = result.get("params")
    if not isinstance(points, Mapping) or not isinstance(params, Mapping):
        return "valuation scenario points or parameters are missing"
    ordered: list[tuple[float, float]] = []
    for scenario in ("pessimistic", "neutral", "optimistic"):
        band = points.get(scenario)
        scenario_params = params.get(scenario)
        if not isinstance(band, Mapping) or not isinstance(scenario_params, Mapping):
            return f"valuation {scenario} scenario evidence is missing"
        lower = _finite_positive(band.get("lower"))
        upper = _finite_positive(band.get("upper"))
        if lower is None or upper is None or lower > upper:
            return f"valuation {scenario} band is invalid"
        growth = _finite_number(scenario_params.get("growth"))
        scenario_rate = _finite_positive(scenario_params.get("wacc_base"))
        terminal_growth = _finite_number(scenario_params.get("terminal_g"))
        if growth is None or scenario_rate is None or terminal_growth is None or scenario_rate <= terminal_growth:
            return f"valuation {scenario} growth/discount parameters are invalid"
        if result.get("_pb_valuation") is True:
            scenario_roe = _finite_number(scenario_params.get("scenario_roe"))
            cost_of_equity = _finite_positive(scenario_params.get("cost_of_equity"))
            bvps = _finite_positive(scenario_params.get("bvps"))
            pb_lower = _finite_positive(scenario_params.get("pb_lower"))
            pb_upper = _finite_positive(scenario_params.get("pb_upper"))
            if (
                scenario_roe is None
                or cost_of_equity is None
                or bvps is None
                or pb_lower is None
                or pb_upper is None
                or scenario_roe <= growth
                or cost_of_equity - BAND_WACC_DELTA <= growth
                or not str(scenario_params.get("formula") or "").strip()
            ):
                return f"financial P/B {scenario} formula evidence is invalid"
            if not (
                math.isclose(lower, bvps * pb_lower, rel_tol=1e-10, abs_tol=1e-8)
                and math.isclose(upper, bvps * pb_upper, rel_tol=1e-10, abs_tol=1e-8)
                and math.isclose(
                    pb_lower,
                    (scenario_roe - growth) / (cost_of_equity + BAND_WACC_DELTA - growth),
                    rel_tol=1e-10,
                    abs_tol=1e-8,
                )
                and math.isclose(
                    pb_upper,
                    (scenario_roe - growth) / (cost_of_equity - BAND_WACC_DELTA - growth),
                    rel_tol=1e-10,
                    abs_tol=1e-8,
                )
            ):
                return f"financial P/B {scenario} endpoints do not match formula inputs"
        else:
            retention = _finite_number(scenario_params.get("margin_retention"))
            if retention is None or not 0 <= retention <= 1:
                return f"nonfinancial DCF {scenario} margin retention is invalid"
        ordered.append((lower, upper))
    if result.get("_pb_valuation") is True:
        neutral_params = params.get("neutral", {})
        neutral_cost = _finite_positive(neutral_params.get("cost_of_equity"))
        if neutral_cost is None or not math.isclose(base_rate, neutral_cost, rel_tol=0.0, abs_tol=5e-5):
            return "financial P/B base discount rate differs from formula evidence"
        midpoints = [(lower + upper) / 2.0 for lower, upper in ordered]
        if not (midpoints[0] <= midpoints[1] <= midpoints[2]):
            return "financial P/B scenario centers are unordered"
    else:
        if require_strict_ttm and result.get("valuation_input_basis") != "strict_ttm":
            return "nonfinancial valuation input basis is not strict_ttm"
        base_fcf = _finite_positive(result.get("base_fcf"))
        base_revenue = _finite_positive(result.get("base_revenue"))
        net_debt = _finite_number(result.get("net_debt"))
        if base_fcf is None or base_revenue is None or net_debt is None:
            return "nonfinancial DCF base cash-flow inputs are missing"
        if not (ordered[0][1] <= ordered[1][0] <= ordered[1][1] <= ordered[2][0]):
            return "nonfinancial DCF scenario bands are unordered or overlapping"
        for index, scenario in enumerate(("pessimistic", "neutral", "optimistic")):
            scenario_params = params[scenario]
            growth = float(scenario_params["growth"])
            scenario_rate = float(scenario_params["wacc_base"])
            terminal_growth = float(scenario_params["terminal_g"])
            retention = float(scenario_params["margin_retention"])
            expected_lower = _recompute_dcf_per_share(
                base_fcf=base_fcf,
                base_revenue=base_revenue,
                growth=growth,
                wacc=scenario_rate + BAND_WACC_DELTA,
                terminal_growth=terminal_growth,
                shares=shares,
                net_debt=net_debt,
                retention=retention,
            )
            expected_upper = _recompute_dcf_per_share(
                base_fcf=base_fcf,
                base_revenue=base_revenue,
                growth=growth,
                wacc=scenario_rate - BAND_WACC_DELTA,
                terminal_growth=terminal_growth,
                shares=shares,
                net_debt=net_debt,
                retention=retention,
            )
            if (
                expected_lower is None
                or expected_upper is None
                or not (
                    math.isclose(ordered[index][0], expected_lower, rel_tol=1e-8, abs_tol=1e-8)
                    and math.isclose(ordered[index][1], expected_upper, rel_tol=1e-8, abs_tol=1e-8)
                )
            ):
                return f"nonfinancial DCF {scenario} endpoints do not match formula inputs"

    buy_boundary = (ordered[0][1] + ordered[1][0]) / 2.0
    sell_boundary = (ordered[1][1] + ordered[2][0]) / 2.0
    reported_buy = _finite_positive(result.get("buy_zone_upper"))
    reported_sell = _finite_positive(result.get("sell_zone_lower"))
    if reported_buy is None or not math.isclose(reported_buy, buy_boundary, rel_tol=1e-10, abs_tol=1e-8):
        return "valuation buy boundary does not match scenario endpoints"
    if reported_sell is None or not math.isclose(reported_sell, sell_boundary, rel_tol=1e-10, abs_tol=1e-8):
        return "valuation sell boundary does not match scenario endpoints"
    expected_zone = "买入区" if price <= buy_boundary else "卖出区" if price >= sell_boundary else "观察区"
    if result.get("zone") != expected_zone:
        return "valuation zone does not match price and scenario boundaries"
    return None


def validate_market_analysis_quality(
    outcome: MarketAnalysisOutcome,
    *,
    expected_companies: int,
    expected_codes: Iterable[Any] | None = None,
    previous: Mapping[str, Any] | None = None,
    min_score_coverage: float = MIN_SCORE_COVERAGE,
    min_dcf_attempt_coverage: float = MIN_DCF_ATTEMPT_COVERAGE,
    min_dcf_valid_coverage: float = MIN_DCF_VALID_COVERAGE,
    max_issue_rate: float = MAX_PIPELINE_ISSUE_RATE,
    min_relative_ratio: float = MIN_RELATIVE_ANALYSIS_RATIO,
) -> Mapping[str, Any]:
    """Reject a candidate generation that ran but produced implausibly partial output."""
    expected = int(expected_companies)
    if expected < 1:
        raise ValueError("expected_companies must be positive")
    raw_score_rows = len(outcome.scores) if isinstance(outcome.scores, pd.DataFrame) else 0
    score_codes: list[str] = []
    identity_reasons: list[dict[str, Any]] = []
    if not isinstance(outcome.scores, pd.DataFrame) or "code" not in outcome.scores.columns:
        identity_reasons.append(
            {
                "code": "score_identity_missing_code_column",
                "metric": "score_identity",
                "message": "score output must contain a code column",
            }
        )
    else:
        score_codes = [_normalise_code(value) for value in outcome.scores["code"].tolist()]
        blank_count = sum(not code for code in score_codes)
        duplicate_count = len(score_codes) - len(set(score_codes))
        if blank_count:
            identity_reasons.append(
                {
                    "code": "score_identity_blank_code",
                    "metric": "score_identity",
                    "actual": blank_count,
                    "message": f"score output contains {blank_count} blank code(s)",
                }
            )
        if duplicate_count:
            identity_reasons.append(
                {
                    "code": "score_identity_duplicate_code",
                    "metric": "score_identity",
                    "actual": duplicate_count,
                    "message": f"score output contains {duplicate_count} duplicate normalized code row(s)",
                }
            )
    actual_code_set = {code for code in score_codes if code}
    score_rows = len(actual_code_set)
    canonical_code_set = set(actual_code_set)
    if expected_codes is not None:
        expected_code_set = {_normalise_code(value) for value in expected_codes}
        expected_code_set.discard("")
        canonical_code_set = set(expected_code_set)
        missing_codes = sorted(expected_code_set - actual_code_set)
        extra_codes = sorted(actual_code_set - expected_code_set)
        if missing_codes:
            identity_reasons.append(
                {
                    "code": "score_identity_missing_companies",
                    "metric": "score_identity",
                    "actual": len(missing_codes),
                    "examples": missing_codes[:10],
                    "message": f"score output omitted {len(missing_codes)} canonical company code(s)",
                }
            )
        if extra_codes:
            identity_reasons.append(
                {
                    "code": "score_identity_extra_companies",
                    "metric": "score_identity",
                    "actual": len(extra_codes),
                    "examples": extra_codes[:10],
                    "message": f"score output contains {len(extra_codes)} code(s) outside the canonical universe",
                }
            )
    result_items = list(outcome.dcf_results.items()) if isinstance(outcome.dcf_results, Mapping) else []
    result_codes = [_normalise_code(code) for code, _result in result_items]
    normalized_result_set = {code for code in result_codes if code}
    if len(result_codes) != len(normalized_result_set):
        identity_reasons.append(
            {
                "code": "valuation_identity_duplicate_or_blank",
                "metric": "valuation_identity",
                "message": "valuation results contain blank or duplicate normalized identities",
            }
        )
    extra_result_codes = sorted(normalized_result_set - canonical_code_set)
    if extra_result_codes:
        identity_reasons.append(
            {
                "code": "valuation_identity_extra_companies",
                "metric": "valuation_identity",
                "actual": len(extra_result_codes),
                "examples": extra_result_codes[:10],
                "message": "valuation results contain companies outside the canonical universe",
            }
        )
    for raw_code, result in result_items:
        code = _normalise_code(raw_code)
        payload_error = _valuation_payload_error(code, result, require_strict_ttm=True)
        if payload_error is not None:
            identity_reasons.append(
                {
                    "code": "valuation_payload_invalid",
                    "metric": "valuation_identity",
                    "company": code,
                    "message": f"{code}: {payload_error}",
                }
            )

    skip_items = outcome.dcf_skip_reasons if isinstance(outcome.dcf_skip_reasons, Mapping) else {}
    skip_codes = {_normalise_code(code) for code in skip_items if _normalise_code(code)}
    extra_skip_codes = sorted(skip_codes - canonical_code_set)
    overlap_codes = sorted(skip_codes & normalized_result_set)
    if extra_skip_codes:
        identity_reasons.append(
            {
                "code": "valuation_skip_identity_extra_companies",
                "metric": "valuation_identity",
                "examples": extra_skip_codes[:10],
                "message": "valuation skip reasons contain companies outside the canonical universe",
            }
        )
    if overlap_codes:
        identity_reasons.append(
            {
                "code": "valuation_valid_skip_overlap",
                "metric": "valuation_identity",
                "examples": overlap_codes[:10],
                "message": "a company cannot be both a valid valuation and a skipped valuation",
            }
        )

    dcf_attempted = int(outcome.dcf_attempted)
    dcf_valid = len(normalized_result_set)
    dcf_skipped = int(outcome.dcf_skipped)
    if not (0 <= dcf_attempted <= expected):
        identity_reasons.append(
            {
                "code": "valuation_attempt_count_invalid",
                "metric": "valuation_accounting",
                "message": "valuation attempted count is outside the canonical universe",
            }
        )
    if not (0 <= dcf_skipped <= dcf_attempted) or dcf_valid + dcf_skipped != dcf_attempted:
        identity_reasons.append(
            {
                "code": "valuation_accounting_mismatch",
                "metric": "valuation_accounting",
                "message": "valuation attempted must equal valid plus skipped",
            }
        )
    if len(skip_codes) < dcf_skipped:
        identity_reasons.append(
            {
                "code": "valuation_skip_reasons_incomplete",
                "metric": "valuation_accounting",
                "message": "skipped valuations lack structured company reasons",
            }
        )
    issue_items = list(outcome.issues) if isinstance(outcome.issues, (list, tuple)) else []
    if not isinstance(outcome.issues, (list, tuple)):
        identity_reasons.append(
            {
                "code": "pipeline_issue_collection_invalid",
                "metric": "pipeline_identity",
                "message": "pipeline issues must be a list or tuple",
            }
        )
    for issue in issue_items:
        if not isinstance(issue, PipelineIssue):
            identity_reasons.append(
                {
                    "code": "pipeline_issue_invalid",
                    "metric": "pipeline_identity",
                    "message": "pipeline issues contain a non-PipelineIssue entry",
                }
            )
            continue
        issue_code = _normalise_code(issue.code) if issue.code else ""
        if issue_code and issue_code not in canonical_code_set:
            identity_reasons.append(
                {
                    "code": "pipeline_issue_extra_company",
                    "metric": "pipeline_identity",
                    "company": issue_code,
                    "message": "pipeline issue references a company outside the canonical universe",
                }
            )
    issue_count = len(issue_items)
    metrics = {
        "ok": True,
        "expected_companies": expected,
        "score_raw_rows": raw_score_rows,
        "score_rows": score_rows,
        "score_coverage": score_rows / expected,
        "dcf_attempted": dcf_attempted,
        "dcf_attempt_coverage": dcf_attempted / expected,
        "dcf_valid": dcf_valid,
        "dcf_valid_coverage": dcf_valid / expected,
        "dcf_skipped": dcf_skipped,
        "pipeline_issues": issue_count,
        "pipeline_issue_rate": issue_count / expected,
        "reasons": [],
    }
    for name, value in (
        ("min_score_coverage", min_score_coverage),
        ("min_dcf_attempt_coverage", min_dcf_attempt_coverage),
        ("min_dcf_valid_coverage", min_dcf_valid_coverage),
        ("max_issue_rate", max_issue_rate),
        ("min_relative_ratio", min_relative_ratio),
    ):
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    reasons: list[dict[str, Any]] = list(identity_reasons)

    def below(code: str, metric: str, threshold: float, message: str) -> None:
        reasons.append(
            {
                "code": code,
                "metric": metric,
                "actual": metrics[metric],
                "threshold": float(threshold),
                "message": message,
            }
        )

    if metrics["score_coverage"] < float(min_score_coverage):
        below(
            "score_coverage_low",
            "score_coverage",
            min_score_coverage,
            f"score coverage too low: {score_rows}/{expected}",
        )
    if metrics["dcf_attempt_coverage"] < float(min_dcf_attempt_coverage):
        below(
            "dcf_attempt_coverage_low",
            "dcf_attempt_coverage",
            min_dcf_attempt_coverage,
            f"DCF attempt coverage too low: {dcf_attempted}/{expected}",
        )
    if metrics["dcf_valid_coverage"] < float(min_dcf_valid_coverage):
        below(
            "dcf_valid_coverage_low",
            "dcf_valid_coverage",
            min_dcf_valid_coverage,
            f"valid DCF coverage too low: {dcf_valid}/{expected}",
        )
    if metrics["pipeline_issue_rate"] > float(max_issue_rate):
        reasons.append(
            {
                "code": "pipeline_issue_rate_high",
                "metric": "pipeline_issue_rate",
                "actual": metrics["pipeline_issue_rate"],
                "threshold": float(max_issue_rate),
                "message": f"pipeline issue rate too high: {issue_count}/{expected}",
            }
        )

    if isinstance(previous, Mapping):
        for key in ("score_rows", "dcf_attempted", "dcf_valid"):
            old_value = previous.get(key)
            try:
                old_count = int(old_value)
            except (TypeError, ValueError, OverflowError):
                continue
            new_count = int(metrics[key])
            if old_count > 0 and new_count < old_count * float(min_relative_ratio):
                reasons.append(
                    {
                        "code": "relative_analysis_regression",
                        "metric": key,
                        "actual": new_count,
                        "previous": old_count,
                        "threshold": old_count * float(min_relative_ratio),
                        "message": f"relative {key} drop is too large: {old_count} -> {new_count}",
                    }
                )
    if reasons:
        metrics["ok"] = False
        metrics["reasons"] = reasons
        raise AnalysisQualityError(reasons, metrics)
    return metrics


def _normalise_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) < 6:
        return text.zfill(6)
    return text


def _finite_positive(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _annual_values(records: Any, keys: tuple[str, ...]) -> dict[int, float]:
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)):
        return {}
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in records:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        if len(report_date) < 4 or not report_date[:4].isdigit():
            continue
        value = next((_finite_number(row.get(key)) for key in keys if _finite_number(row.get(key)) is not None), None)
        if value is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, value)
        if year not in by_year or candidate[:2] > by_year[year][:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _attributable_equity(row: Mapping[str, Any]) -> Optional[float]:
    parent = None
    for key in (
        "PARENT_EQUITY",
        "TOTAL_PARENT_EQUITY",
        "TOTAL_EQUITY_ATTR_P",
        "TOTAL_EQUITY_PARENT",
        "PARENT_HOLDER_EQUITY",
    ):
        value = _finite_positive(row.get(key))
        if value is not None:
            parent = value
            break
    total = _finite_number(row.get("TOTAL_EQUITY"))
    minority = next(
        (
            value
            for key in ("MINORITY_EQUITY", "MINORITY_INTEREST")
            if (value := _finite_number(row.get(key))) is not None
        ),
        None,
    )
    assets = _finite_positive(row.get("TOTAL_ASSETS"))
    if parent is not None:
        if assets is not None and parent > assets * 1.03 and (total is None or minority is None):
            return None
        if total is not None:
            if minority is None:
                if total <= 0 < parent or (total > 0 and parent > total * 1.03):
                    return None
            else:
                scale = max(abs(total), abs(parent) + abs(minority), 1.0)
                if abs(total - parent - minority) > scale * 0.03:
                    return None
        return parent
    if total is not None and minority is not None:
        attributable = total - minority
        if attributable > 0 and not (assets is not None and total > assets * 1.03):
            return attributable
    return None


def _annual_attributable_equity(records: Any) -> dict[int, float]:
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)):
        return {}
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in records:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        if len(report_date) < 4 or not report_date[:4].isdigit():
            continue
        equity = _attributable_equity(row)
        if equity is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, equity)
        if year not in by_year or candidate[:2] > by_year[year][:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _annual_fcff_values(records: Any) -> list[float]:
    """Return one full-year-preferred FCFF observation per year, including losses."""
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)):
        return []
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in records:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        if len(report_date) < 4 or not report_date[:4].isdigit():
            continue
        operating = _finite_number(row.get("NETCASH_OPERATE"))
        capex = _finite_number(row.get("CONSTRUCT_LONG_ASSET"))
        if operating is None or capex is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, operating - abs(capex))
        if year not in by_year or candidate[:2] > by_year[year][:2]:
            by_year[year] = candidate
    return [by_year[year][2] for year in sorted(by_year)]


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _numbers_match(actual: Any, expected: Any, *, rel_tol: float = 1e-9) -> bool:
    actual_number = _finite_number(actual)
    expected_number = _finite_number(expected)
    if actual_number is None or expected_number is None:
        return actual_number is None and expected_number is None
    return math.isclose(
        actual_number,
        expected_number,
        rel_tol=rel_tol,
        abs_tol=max(1e-8, abs(expected_number) * rel_tol),
    )


def _current_evidence_matches(actual: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(actual, Mapping):
        return False
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, (int, float)) and not isinstance(expected_value, bool):
            if not _numbers_match(actual_value, expected_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _valuation_source_error(
    expected_code: str,
    result: Mapping[str, Any],
    quote: Mapping[str, Any],
    company: Mapping[str, Any],
    expected_shares: float,
    *,
    reporting_period_contract: ReportingPeriodContract | None = None,
    strict_ttm_required: bool = False,
) -> str | None:
    """Bind a self-consistent valuation payload to this company's source rows."""
    from data.industry import classify_industry, get_industry_fcf_margin
    from engine import scenarios
    from engine.dcf import (
        extract_debt_and_cash,
        extract_fcf_normalisation,
        extract_net_debt,
        reconstruct_ttm_fcff,
        reconstruct_ttm_revenue,
    )

    if result.get("_pb_valuation") is True:
        if not all(company.get(key) for key in ("balance", "income_history", "income_interim")):
            if strict_ttm_required:
                return "financial source rows required for production binding are incomplete"
            # Custom orchestration runners may intentionally use a lightweight
            # synthetic fixture.  This exemption never applies to production.
            return None
        expected_industry = classify_industry(expected_code, str(quote.get("name", "")))
        if result.get("industry_code") != expected_industry:
            return "valuation industry differs from current company classification"
        equity = _annual_attributable_equity(company.get("balance", []))
        profits = _annual_values(company.get("income_history", []), ("PARENT_NETPROFIT",))
        roe_years = sorted(year for year in profits if year in equity and year - 1 in equity)
        if not roe_years:
            return "financial source lacks attributable ROE history"
        consecutive = [roe_years[-1]]
        for year in reversed(roe_years[:-1]):
            if year != consecutive[0] - 1:
                break
            consecutive.insert(0, year)
        roe_years = consecutive[-5:]
        if len(roe_years) < 3 or profits[roe_years[-1]] <= 0:
            return "financial source lacks current positive attributable earnings"
        roes = [profits[year] / ((equity[year - 1] + equity[year]) / 2.0) for year in roe_years]
        if roes[-1] <= 0:
            return "financial latest attributable ROE is non-positive"
        expected_roe = statistics.median(roes)
        expected_bvps = equity[max(equity)] / expected_shares
        neutral = result.get("params", {}).get("neutral", {})
        if not _numbers_match(neutral.get("bvps"), expected_bvps):
            return "financial P/B BVPS differs from current attributable equity"
        if not _numbers_match(result.get("normalised_roe"), expected_roe):
            return "financial P/B normalised ROE differs from current company history"
        if list(result.get("roe_evidence_years", [])) != roe_years:
            return "financial P/B ROE evidence years differ from current company history"
        expected_current = scenarios._current_period_evidence(dict(company))
        if not _current_evidence_matches(result.get("current_period_evidence"), expected_current):
            return "financial P/B current-period evidence differs from current company"
        return None

    if not strict_ttm_required and not all(
        company.get(key)
        for key in ("revenue_history", "cashflow", "balance", "income_history", "income_interim", "cashflow_interim")
    ):
        return None
    expected_industry = classify_industry(expected_code, str(quote.get("name", "")))
    if result.get("industry_code") != expected_industry:
        return "valuation industry differs from current company classification"

    if strict_ttm_required:
        if not isinstance(reporting_period_contract, ReportingPeriodContract):
            return "industrial strict TTM reporting-period contract is missing"
        expected_ttm_fcff = reconstruct_ttm_fcff(
            company.get("cashflow", []),
            company.get("cashflow_interim", []),
            period_contract=reporting_period_contract,
            require_capex_provenance=True,
        )
        expected_ttm_revenue = reconstruct_ttm_revenue(
            company.get("revenue_history", []),
            company.get("income_interim", []),
            period_contract=reporting_period_contract,
        )
        if expected_ttm_fcff.get("status") != "complete":
            return f"industrial TTM FCFF reconstruction is {expected_ttm_fcff.get('status')}"
        if expected_ttm_revenue.get("status") != "complete":
            return f"industrial TTM revenue reconstruction is {expected_ttm_revenue.get('status')}"
        if result.get("valuation_input_basis") != "strict_ttm":
            return "industrial valuation_input_basis differs from strict TTM contract"
        if result.get("base_revenue_basis") != "strict_ttm_reported_revenue":
            return "industrial base_revenue_basis differs from strict TTM contract"
        if result.get("base_fcf_basis") != "normalised_two_annual_plus_ttm_cfo_less_capex_proxy":
            return "industrial base_fcf_basis differs from strict TTM contract"
        if result.get("ttm_fcff_evidence") != expected_ttm_fcff:
            return "industrial TTM FCFF evidence differs from reconstructed source components"
        if result.get("ttm_revenue_evidence") != expected_ttm_revenue:
            return "industrial TTM revenue evidence differs from reconstructed source components"
        expected_revenue = _finite_positive(expected_ttm_revenue.get("value"))
        expected_latest_fcf = _finite_positive(expected_ttm_fcff.get("value"))
        if expected_revenue is None:
            return "industrial strict TTM revenue is non-positive"
        if expected_latest_fcf is None:
            return "industrial strict TTM FCFF is non-positive"
        normalisation_result = scenarios._ttm_fcf_normalisation(
            company.get("cashflow", []),
            expected_ttm_fcff,
            reporting_period_contract,
        )
        if normalisation_result is None:
            return "industrial FY-1/FY/TTM FCFF normalisation source is incomplete"
        normalisation, expected_periods = normalisation_result
        expected_fcf = _finite_positive(normalisation.get("normalised_fcf"))
        if expected_fcf is None:
            return "industrial FY-1/FY/TTM normalised FCFF is non-positive"
        if not _numbers_match(result.get("base_revenue"), expected_revenue):
            return "industrial DCF base revenue differs from strict TTM reconstruction"
        if not _numbers_match(result.get("latest_fcff"), expected_latest_fcf):
            return "industrial DCF latest FCFF differs from strict TTM reconstruction"
        expected_recent = list(normalisation.get("recent_fcff", ()))
        actual_recent = result.get("recent_fcff")
        if (
            not isinstance(actual_recent, (list, tuple))
            or len(actual_recent) != len(expected_recent)
            or any(not _numbers_match(actual, expected) for actual, expected in zip(actual_recent, expected_recent))
        ):
            return "industrial DCF recent FCFF differs from FY-1/FY/TTM normalisation"
        if result.get("recent_fcff_periods") != expected_periods:
            return "industrial DCF recent FCFF periods differ from FY-1/FY/TTM contract"
        if result.get("fcf_normalisation_period_basis") != "two_annual_plus_strict_ttm":
            return "industrial DCF normalisation period basis differs from strict TTM contract"
        if result.get("fcf_normalisation_basis") != normalisation.get("basis"):
            return "industrial DCF FCFF normalisation method differs from strict source history"
        expected_period_detail = {
            "period_set": "two_annual_plus_strict_ttm",
            "periods": expected_periods,
            "normalisation_method": normalisation.get("basis"),
            "cash_flow_kind": expected_ttm_fcff.get("cash_flow_kind"),
            "formula_version": expected_ttm_fcff.get("formula_version"),
        }
        if result.get("fcf_normalisation_period") != expected_period_detail:
            return "industrial DCF FCFF normalisation period evidence differs from strict source history"
        expected_adjustments: list[dict[str, object]] = []
    else:
        revenues = _annual_values(
            company.get("revenue_history", []),
            ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
        )
        if not revenues:
            return "industrial source lacks annual revenue"
        expected_revenue = revenues[max(revenues)]
        if not _numbers_match(result.get("base_revenue"), expected_revenue):
            return "industrial DCF base revenue differs from current company history"
        normalisation = extract_fcf_normalisation(company.get("cashflow", []))
        expected_fcf = _finite_positive(normalisation.get("normalised_fcf"))
        if expected_fcf is None:
            return "industrial source lacks positive normalised FCFF"
        expected_adjustments = []

    profits = _annual_values(company.get("income_history", []), ("PARENT_NETPROFIT",))
    profit_values = [profits[year] for year in sorted(profits)]
    if len(profit_values) >= 3 and min(profit_values) < 0 < max(profit_values):
        recent = profit_values[-3:]
        if not (recent[0] < recent[1] < recent[2] and recent[2] > 0):
            annual_fcffs = _annual_fcff_values(company.get("cashflow", []))
            if len(annual_fcffs) < 3:
                return "industrial mixed-cycle FCFF evidence is incomplete"
            p25 = _quantile(annual_fcffs, 0.25)
            adjusted_fcf = min(expected_fcf, p25)
            if strict_ttm_required and adjusted_fcf != expected_fcf:
                expected_adjustments.append(
                    {
                        "kind": "mixed_profit_cycle_p25_cap",
                        "before": expected_fcf,
                        "limit": p25,
                        "after": adjusted_fcf,
                    }
                )
            expected_fcf = adjusted_fcf
    expected_current = scenarios._current_period_evidence(dict(company))
    quality = scenarios._detect_quality(
        dict(company), expected_fcf / expected_revenue
    ) and scenarios._current_period_supports_quality(dict(company))
    if quality:
        margin_ceiling = 0.65
    else:
        margin_ceiling = scenarios._nonquality_fcf_margin_ceiling(get_industry_fcf_margin(expected_industry))
    margin_limit = expected_revenue * margin_ceiling
    if expected_fcf > margin_limit:
        if strict_ttm_required:
            expected_adjustments.append(
                {
                    "kind": "fcf_margin_ceiling",
                    "before": expected_fcf,
                    "limit": margin_limit,
                    "after": margin_limit,
                }
            )
        expected_fcf = margin_limit
    if not _numbers_match(result.get("base_fcf"), expected_fcf):
        return "industrial DCF base FCFF differs from current company normalisation"
    if strict_ttm_required and result.get("base_fcf_adjustments") != expected_adjustments:
        return "industrial DCF base FCFF adjustments differ from strict source normalisation"
    expected_net_debt = extract_net_debt(company.get("balance", []))
    if not _numbers_match(result.get("net_debt"), expected_net_debt):
        return "industrial DCF net debt differs from current company balance sheet"
    if not strict_ttm_required:
        if not _numbers_match(result.get("latest_fcff"), normalisation.get("latest_fcff")):
            return "industrial DCF latest FCFF differs from current company history"
        actual_recent = result.get("recent_fcff")
        expected_recent = list(normalisation.get("recent_fcff", ()))
        if (
            not isinstance(actual_recent, (list, tuple))
            or len(actual_recent) != len(expected_recent)
            or any(not _numbers_match(actual, expected) for actual, expected in zip(actual_recent, expected_recent))
        ):
            return "industrial DCF recent FCFF evidence differs from current company history"
        if result.get("fcf_normalisation_basis") != normalisation.get("basis"):
            return "industrial DCF FCFF basis differs from current company history"
    if bool(result.get("quality_evidence")) != quality:
        return "industrial DCF quality flag differs from current company evidence"
    if not _current_evidence_matches(result.get("current_period_evidence"), expected_current):
        return "industrial DCF current-period evidence differs from current company"

    debt, _cash, debt_known = extract_debt_and_cash(company.get("balance", []))
    components = result.get("wacc_components")
    if debt_known and isinstance(components, Mapping):
        price = _finite_positive(quote.get("price"))
        if price is None:
            return "industrial DCF quote price evidence is invalid"
        capital = price * expected_shares + debt
        if capital <= 0 or not _numbers_match(components.get("debt_weight"), debt / capital):
            return "industrial DCF WACC capital structure differs from current company debt"
    return None


def _infer_valuation_rejection_reason(
    code: str,
    quote: Mapping[str, Any],
    company: Mapping[str, Any],
    *,
    reporting_period_contract: ReportingPeriodContract | None = None,
    strict_ttm_required: bool = False,
) -> dict[str, str]:
    """Explain and classify a model rejection using its actual evidence gate."""
    from data.industry import classify_industry

    industry = classify_industry(code, str(quote.get("name", "")))
    if industry == "FINANCIAL_OTHER":
        return make_dcf_skip_classification(
            DCF_SKIP_MODEL_UNSUPPORTED,
            "unsupported_financial_valuation_model",
        )
    if industry in {"BANK", "INSURANCE", "SECURITIES"}:
        equity = _annual_attributable_equity(company.get("balance", []))
        profits = _annual_values(company.get("income_history", []), ("PARENT_NETPROFIT",))
        roe_years = sorted(year for year in profits if year in equity and year - 1 in equity)
        if len(equity) < 4:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "financial_missing_attributable_equity_history",
            )
        if len(roe_years) < 3:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "financial_insufficient_average_roe_history",
            )
        if roe_years[-1] != max(profits) or roe_years[-1] != max(equity):
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "financial_nonconsecutive_or_stale_roe_history",
            )
        consecutive_years = [roe_years[-1]]
        for year in reversed(roe_years[:-1]):
            if year != consecutive_years[0] - 1:
                break
            consecutive_years.insert(0, year)
        if len(consecutive_years) < 3:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "financial_nonconsecutive_or_stale_roe_history",
            )
        roes = [profits[year] / ((equity[year - 1] + equity[year]) / 2.0) for year in consecutive_years[-5:]]
        if profits[consecutive_years[-1]] <= 0 or roes[-1] <= 0:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "financial_latest_annual_attributable_loss",
            )
        if not roes or sum(roe > 0 for roe in roes) / len(roes) < 0.8:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "financial_nonpositive_or_unstable_roe",
            )
        from engine.scenarios import _current_period_evidence

        current = _current_period_evidence(dict(company))
        if current["profit"] is None or current["profit_yoy_basis"] not in {
            "same_period_yoy",
            "same_period_turnaround",
        }:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "financial_missing_current_comparable_profit",
            )
        if current["profit"] <= 0 or (
            current["profit_yoy_basis"] == "same_period_yoy" and current["profit_yoy"] <= -0.30
        ):
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "financial_current_attributable_profit_deterioration",
            )
        return make_dcf_skip_classification(
            DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
            "financial_pb_scenario_invariants_failed",
        )

    if strict_ttm_required:
        from engine import scenarios
        from engine.dcf import reconstruct_ttm_fcff, reconstruct_ttm_revenue

        if not isinstance(reporting_period_contract, ReportingPeriodContract):
            return make_dcf_skip_classification(
                DCF_SKIP_INCONSISTENT_SOURCE,
                "ttm_invalid_period_contract",
            )
        ttm_fcff = reconstruct_ttm_fcff(
            company.get("cashflow", []),
            company.get("cashflow_interim", []),
            period_contract=reporting_period_contract,
            require_capex_provenance=True,
        )
        fcff_status = str(ttm_fcff.get("status") or "unknown")
        if fcff_status != "complete":
            category = DCF_SKIP_SOURCE_MISSING if fcff_status == "missing_component" else DCF_SKIP_INCONSISTENT_SOURCE
            return make_dcf_skip_classification(category, f"ttm_fcff_{fcff_status}")
        fcff = _finite_number(ttm_fcff.get("value"))
        if fcff is None or fcff <= 0:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "ttm_fcff_nonpositive",
            )
        ttm_revenue = reconstruct_ttm_revenue(
            company.get("revenue_history", []),
            company.get("income_interim", []),
            period_contract=reporting_period_contract,
        )
        revenue_status = str(ttm_revenue.get("status") or "unknown")
        if revenue_status != "complete":
            category = (
                DCF_SKIP_SOURCE_MISSING if revenue_status == "missing_component" else DCF_SKIP_INCONSISTENT_SOURCE
            )
            return make_dcf_skip_classification(category, f"ttm_revenue_{revenue_status}")
        revenue = _finite_number(ttm_revenue.get("value"))
        if revenue is None or revenue <= 0:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "ttm_revenue_nonpositive",
            )
        normalisation_result = scenarios._ttm_fcf_normalisation(
            company.get("cashflow", []),
            ttm_fcff,
            reporting_period_contract,
        )
        if normalisation_result is None:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "ttm_fcff_missing_prior_annual_component",
            )
        normalisation, _periods = normalisation_result
        normalised_fcf = _finite_number(normalisation.get("normalised_fcf"))
        if normalised_fcf is None or normalised_fcf <= 0:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "ttm_fcff_nonpositive_normalised",
            )
    else:
        revenues = _annual_values(
            company.get("revenue_history", []),
            ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
        )
        if not revenues:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "missing_positive_annual_revenue",
            )
        if revenues[max(revenues)] <= 0:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "missing_positive_annual_revenue",
            )
        from engine.dcf import extract_fcf_from_cashflow

        fcf = _finite_number(extract_fcf_from_cashflow(company.get("cashflow", [])))
        if fcf is None:
            return make_dcf_skip_classification(
                DCF_SKIP_SOURCE_MISSING,
                "missing_complete_annual_fcff_history",
            )
        if fcf <= 0:
            return make_dcf_skip_classification(
                DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                "nonpositive_normalised_fcff",
            )
    profits = _annual_values(company.get("income_history", []), ("PARENT_NETPROFIT",))
    ordered_profits = [profits[year] for year in sorted(profits)]
    if len(ordered_profits) >= 3 and min(ordered_profits) < 0 < max(ordered_profits):
        recent = ordered_profits[-3:]
        recovering = recent[0] < recent[1] < recent[2] and recent[2] > 0
        if not recovering:
            annual_fcffs = _annual_fcff_values(company.get("cashflow", []))
            if len(annual_fcffs) < 3:
                return make_dcf_skip_classification(
                    DCF_SKIP_SOURCE_MISSING,
                    "mixed_profit_cycle_insufficient_fcff_history",
                )
            if _quantile(annual_fcffs, 0.25) <= 0:
                return make_dcf_skip_classification(
                    DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
                    "mixed_profit_cycle_unsupported_by_fcff",
                )
    # A full-market instrumented run showed this residual branch occurs when
    # the pessimistic equity value is non-positive after net debt; no sampled
    # case reached the later band-order invariant.  Name the actual rejection
    # rather than collapsing it into a misleading generic bucket.
    return make_dcf_skip_classification(
        DCF_SKIP_ECONOMIC_NOT_APPLICABLE,
        "nonpositive_pessimistic_equity_value",
    )


def _canonicalize_market_inputs(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
) -> tuple[pd.DataFrame, dict[str, Mapping[str, Any]], list[PipelineIssue]]:
    """Build one unambiguous identity generation shared by valuation and scoring."""
    if not isinstance(quotes, pd.DataFrame) or "code" not in quotes.columns:
        raise ValueError("quotes must be a DataFrame with a code column")
    if not isinstance(financials, Mapping):
        raise ValueError("financials must be a mapping")

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in quotes.to_dict(orient="records"):
        code = _normalise_code(row.get("code"))
        if code:
            normalized_row = dict(row)
            normalized_row["code"] = code
            grouped[code].append(normalized_row)

    quote_ambiguous = {code for code, rows in grouped.items() if len(rows) > 1}
    issues = [
        PipelineIssue(code, "identity", "ambiguous bare code appears in multiple quote rows")
        for code in sorted(quote_ambiguous)
    ]

    financial_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw_code, company in financials.items():
        code = _normalise_code(raw_code)
        if not code:
            issues.append(PipelineIssue("", "identity", "empty financial code"))
        elif not isinstance(company, Mapping):
            issues.append(PipelineIssue(code, "input", "financial record is not a mapping"))
        else:
            financial_groups[code].append(company)
    financial_ambiguous = {code for code, rows in financial_groups.items() if len(rows) > 1}
    issues.extend(
        PipelineIssue(code, "identity", "ambiguous normalized financial code") for code in sorted(financial_ambiguous)
    )

    ambiguous = quote_ambiguous | financial_ambiguous
    canonical_quotes = pd.DataFrame(
        [grouped[code][0] for code in sorted(grouped) if code not in ambiguous],
        columns=quotes.columns,
    )
    canonical_financials = {
        code: financial_groups[code][0] for code in sorted(financial_groups) if code not in ambiguous
    }
    return canonical_quotes, canonical_financials, issues


def _restrict_to_eligible_codes(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    issues: list[PipelineIssue],
    eligible_codes: Iterable[Any] | None,
) -> tuple[pd.DataFrame, dict[str, Mapping[str, Any]], list[PipelineIssue]]:
    """Apply the validated snapshot universe before either analysis branch."""
    if eligible_codes is None:
        return quotes, dict(financials), issues
    eligible = {_normalise_code(value) for value in eligible_codes}
    eligible.discard("")
    if not eligible:
        raise ValueError("eligible_codes must contain at least one normalized code")
    quote_codes = {_normalise_code(value) for value in quotes["code"].tolist()}
    financial_codes = set(financials)
    missing_quotes = sorted(eligible - quote_codes)
    missing_financials = sorted(eligible - financial_codes)
    if missing_quotes or missing_financials:
        details: list[str] = []
        if missing_quotes:
            details.append(f"missing quotes: {missing_quotes[:10]}")
        if missing_financials:
            details.append(f"missing financials: {missing_financials[:10]}")
        raise ValueError("eligible_codes are not fully present in the canonical inputs; " + "; ".join(details))
    restricted_quotes = quotes.loc[quotes["code"].map(_normalise_code).isin(eligible)].copy()
    restricted_financials = {code: company for code, company in financials.items() if code in eligible}
    restricted_issues = [issue for issue in issues if not issue.code or _normalise_code(issue.code) in eligible]
    return restricted_quotes, restricted_financials, restricted_issues


def _prepare_market_beta_estimates(values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize selected precomputed beta records and drop non-SH/SZ codes."""
    if values is None:
        return None
    if not isinstance(values, Mapping):
        raise TypeError("market_beta_estimates must be a mapping keyed by stock code")
    prepared: dict[str, Any] = {}
    for raw_code, estimate in values.items():
        code = _normalise_code(raw_code)
        # The analysis universe intentionally excludes Beijing Stock Exchange
        # securities.  Ignore them before task construction rather than
        # accidentally forwarding an unsupported estimate.
        if not (len(code) == 6 and code.isdigit() and code.startswith(("0", "3", "6"))):
            continue
        if code in prepared:
            raise ValueError(f"duplicate normalized market-beta code: {code}")
        prepared[code] = estimate
    return prepared


def _prepare_reporting_period_contract(
    value: ReportingPeriodContract | Mapping[str, Any] | None,
    *,
    required: bool,
) -> ReportingPeriodContract | None:
    """Validate and freeze one generation-wide reporting-period contract.

    Validation happens before any worker is submitted.  A mapping is accepted
    for snapshot/JSON callers, but it must contain exactly the three canonical
    ISO-date fields; worker threads receive the immutable dataclass only.
    """
    if value is None:
        if required:
            raise ValueError("reporting_period_contract is required for production valuation generation")
        return None
    if isinstance(value, ReportingPeriodContract):
        prepared = value
    elif isinstance(value, Mapping):
        expected_keys = {
            "annual_report_date",
            "current_interim_report_date",
            "prior_interim_report_date",
        }
        if set(value) != expected_keys:
            raise ValueError(
                "reporting_period_contract mapping must contain exactly "
                "annual_report_date, current_interim_report_date, prior_interim_report_date"
            )
        if not all(isinstance(value[key], str) for key in expected_keys):
            raise ValueError("reporting_period_contract dates must be canonical ISO strings")
        prepared = ReportingPeriodContract(
            annual_report_date=value["annual_report_date"],
            current_interim_report_date=value["current_interim_report_date"],
            prior_interim_report_date=value["prior_interim_report_date"],
        )
    else:
        raise TypeError("reporting_period_contract must be a ReportingPeriodContract or mapping")

    # Keep the period rules centralized with the reconstruction core.  This is
    # deliberately checked here, not lazily inside per-company workers.
    from engine.dcf import _valid_period_contract

    if not _valid_period_contract(prepared):
        raise ValueError("reporting_period_contract is invalid or internally inconsistent")
    return prepared


def _compute_dcf_batch_canonical(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    initial_issues: list[PipelineIssue],
    dcf_runner: Callable[..., Optional[Mapping[str, Any]]],
    max_workers: int,
    progress_cb: Callable[[int, int], None] | None,
    reporting_period_contract: ReportingPeriodContract | None,
    strict_ttm_required: bool,
    market_beta_estimates: Mapping[str, Any] | None = None,
) -> DcfBatchOutcome:
    issues = list(initial_issues)
    skip_reasons: dict[str, str] = {}
    skip_classifications: dict[str, dict[str, str]] = {}
    quote_by_code = {_normalise_code(row.get("code")): row for row in quotes.to_dict(orient="records")}

    def record_skip(code: str, category: str, reason: str) -> None:
        classification = make_dcf_skip_classification(category, reason)
        skip_reasons[code] = classification["reason"]
        skip_classifications[code] = classification

    tasks: list[tuple[str, Mapping[str, Any], Mapping[str, Any], float, float]] = []
    for code in sorted(financials):
        quote = quote_by_code.get(code)
        if quote is None:
            record_skip(code, DCF_SKIP_SOURCE_MISSING, "missing_quote")
            continue
        price = _finite_positive(quote.get("price"))
        market_cap = _finite_positive(quote.get("market_cap"))
        if price is None or market_cap is None:
            issues.append(PipelineIssue(code, "input", "price or market_cap is not finite and positive"))
            category = (
                DCF_SKIP_SOURCE_MISSING
                if quote.get("price") is None or quote.get("market_cap") is None
                else DCF_SKIP_INCONSISTENT_SOURCE
            )
            record_skip(code, category, "invalid_price_or_market_cap")
            continue
        shares = market_cap / price
        if not math.isfinite(shares) or shares <= 0:
            issues.append(PipelineIssue(code, "input", "derived shares are invalid"))
            record_skip(code, DCF_SKIP_INCONSISTENT_SOURCE, "invalid_derived_shares")
            continue
        tasks.append((code, quote, financials[code], price, shares))

    def run_one(task):
        code, quote, company, price, shares = task
        revenue_history = company.get("revenue_history", [])
        if not isinstance(revenue_history, list):
            revenue_history = []
        kwargs = {
            "code": code,
            "name": str(quote.get("name", "")),
            "current_price": price,
            "financial_data": dict(company),
            "revenue_history": revenue_history,
            "total_shares": shares,
            "reporting_period_contract": reporting_period_contract,
        }
        if market_beta_estimates is not None and code in market_beta_estimates:
            kwargs["market_beta_estimate"] = market_beta_estimates[code]
        result = dcf_runner(
            **kwargs,
        )
        return code, result

    results: dict[str, Mapping[str, Any]] = {}
    skipped = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, max(len(tasks), 1))) as executor:
        futures = {executor.submit(run_one, task): task for task in tasks}
        for completed, future in enumerate(as_completed(futures), start=1):
            task = futures[future]
            code, quote, company, _price, expected_shares = task
            try:
                result_code, result = future.result()
                if result is None:
                    skipped += 1
                    classification = _infer_valuation_rejection_reason(
                        code,
                        quote,
                        company,
                        reporting_period_contract=reporting_period_contract,
                        strict_ttm_required=strict_ttm_required,
                    )
                    record_skip(code, classification["category"], classification["reason"])
                elif not isinstance(result, Mapping):
                    skipped += 1
                    issues.append(PipelineIssue(code, "dcf", "valuation returned a non-mapping result"))
                    record_skip(code, DCF_SKIP_INTERNAL_ERROR, "valuation_returned_non_mapping")
                else:
                    payload_error = _valuation_payload_error(
                        result_code,
                        result,
                        expected_price=quote.get("price"),
                        expected_shares=expected_shares,
                        require_strict_ttm=strict_ttm_required,
                    )
                    if payload_error is None:
                        payload_error = _valuation_source_error(
                            code,
                            result,
                            quote,
                            company,
                            expected_shares,
                            reporting_period_contract=reporting_period_contract,
                            strict_ttm_required=strict_ttm_required,
                        )
                    if payload_error is not None:
                        skipped += 1
                        issues.append(PipelineIssue(code, "valuation_evidence", payload_error))
                        record_skip(code, DCF_SKIP_INCONSISTENT_SOURCE, "valuation_evidence_invalid")
                    else:
                        results[result_code] = result
            except Exception as exc:
                skipped += 1
                issues.append(PipelineIssue(code, "dcf", f"{type(exc).__name__}: {exc}"))
                record_skip(code, DCF_SKIP_INTERNAL_ERROR, f"valuation_exception:{type(exc).__name__}")
            if progress_cb is not None:
                progress_cb(completed, len(tasks))

    ordered_results = {code: results[code] for code in sorted(results)}
    ordered_issues = tuple(sorted(issues, key=lambda issue: (issue.code, issue.stage, issue.message)))
    return DcfBatchOutcome(
        results=ordered_results,
        issues=ordered_issues,
        attempted=len(tasks),
        skipped=skipped,
        skip_reasons=dict(sorted(skip_reasons.items())),
        skip_classifications=dict(sorted(skip_classifications.items())),
    )


def compute_dcf_batch(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    dcf_runner: Callable[..., Optional[Mapping[str, Any]]] | None = None,
    max_workers: int = DEFAULT_DCF_WORKERS,
    progress_cb: Callable[[int, int], None] | None = None,
    eligible_codes: Iterable[Any] | None = None,
    market_beta_estimates: Mapping[str, Any] | None = None,
    reporting_period_contract: ReportingPeriodContract | Mapping[str, Any] | None = None,
) -> DcfBatchOutcome:
    """Value companies with optional *precomputed* beta evidence.

    This function never downloads one beta per company.  Callers may prefetch
    selected single-company estimates and pass them by code; the default batch
    path is industry-only and performs zero market-history network requests.
    Beijing Stock Exchange codes are not forwarded to the valuation model.
    """
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
        raise ValueError("max_workers must be an integer between 1 and 64")
    worker_count = max_workers
    production_generation = dcf_runner is None
    prepared_contract = _prepare_reporting_period_contract(
        reporting_period_contract,
        required=production_generation,
    )
    from data.industry import begin_industry_generation

    begin_industry_generation()
    if dcf_runner is None:
        from engine.scenarios import run_template25

        dcf_runner = run_template25
    canonical_quotes, canonical_financials, issues = _canonicalize_market_inputs(quotes, financials)
    canonical_quotes, canonical_financials, issues = _restrict_to_eligible_codes(
        canonical_quotes,
        canonical_financials,
        issues,
        eligible_codes,
    )
    prepared_beta = _prepare_market_beta_estimates(market_beta_estimates)
    return _compute_dcf_batch_canonical(
        canonical_quotes,
        canonical_financials,
        initial_issues=issues,
        dcf_runner=dcf_runner,
        max_workers=worker_count,
        progress_cb=progress_cb,
        reporting_period_contract=prepared_contract,
        strict_ttm_required=production_generation,
        market_beta_estimates=prepared_beta,
    )


def run_market_analysis(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    dcf_runner: Callable[..., Optional[Mapping[str, Any]]] | None = None,
    screen_runner: Callable[..., pd.DataFrame] | None = None,
    max_workers: int = DEFAULT_DCF_WORKERS,
    dcf_progress_cb: Callable[[int, int], None] | None = None,
    score_progress_cb: Callable[[int, int], None] | None = None,
    eligible_codes: Iterable[Any] | None = None,
    enforce_quality: bool = False,
    expected_companies: int | None = None,
    previous_quality: Mapping[str, Any] | None = None,
    market_beta_estimates: Mapping[str, Any] | None = None,
    market_coldness_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    quality_history_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    quality_history_loader: Callable[..., Mapping[str, Mapping[str, Any]]] | None = None,
    quality_history_progress_cb: Callable[[int, int], None] | None = None,
    reporting_period_contract: ReportingPeriodContract | Mapping[str, Any] | None = None,
) -> MarketAnalysisOutcome:
    """Run the supported end-to-end analysis outside Streamlit for testing and audits."""
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
        raise ValueError("max_workers must be an integer between 1 and 64")
    worker_count = max_workers
    if expected_companies is not None and (
        isinstance(expected_companies, bool) or not isinstance(expected_companies, int) or expected_companies < 0
    ):
        raise ValueError("expected_companies must be a non-negative integer or None")
    production_generation = dcf_runner is None
    prepared_contract = _prepare_reporting_period_contract(
        reporting_period_contract,
        required=production_generation,
    )
    from data.industry import begin_industry_generation

    begin_industry_generation()
    if dcf_runner is None:
        from engine.scenarios import run_template25

        dcf_runner = run_template25
    canonical_quotes, canonical_financials, issues = _canonicalize_market_inputs(quotes, financials)
    canonical_quotes, canonical_financials, issues = _restrict_to_eligible_codes(
        canonical_quotes,
        canonical_financials,
        issues,
        eligible_codes,
    )
    prepared_beta = _prepare_market_beta_estimates(market_beta_estimates)
    dcf = _compute_dcf_batch_canonical(
        canonical_quotes,
        canonical_financials,
        initial_issues=issues,
        dcf_runner=dcf_runner,
        max_workers=worker_count,
        progress_cb=dcf_progress_cb,
        reporting_period_contract=prepared_contract,
        strict_ttm_required=production_generation,
        market_beta_estimates=prepared_beta,
    )
    default_screen_runner = screen_runner is None
    if default_screen_runner:
        from engine.buy_screener import screen_all_types

        screen_runner = screen_all_types
    captured_quality_history: dict[str, Mapping[str, Any]] = {}
    if quality_history_evidence is not None:
        if not isinstance(quality_history_evidence, Mapping):
            raise TypeError("quality_history_evidence must be a code mapping or None")
        for code, value in quality_history_evidence.items():
            if not isinstance(value, Mapping):
                raise TypeError(f"quality_history_evidence record must be a mapping: {_normalise_code(code)}")
            captured_quality_history[_normalise_code(code)] = dict(value)

    captured_loader = quality_history_loader
    if quality_history_loader is not None:

        def captured_loader(requests, **kwargs):
            loaded = quality_history_loader(requests, **kwargs)
            if isinstance(loaded, Mapping):
                captured_quality_history.update(
                    {_normalise_code(code): dict(value) for code, value in loaded.items() if isinstance(value, Mapping)}
                )
            return loaded

    score_kwargs: dict[str, Any] = {
        "dcf_results": dcf.results,
        "progress_cb": score_progress_cb,
    }
    # Custom runners used by audit/tests retain the historical callable
    # contract.  The production scorer accepts the independently acquired
    # bulk coldness evidence explicitly, avoiding hidden network I/O inside
    # per-company workers.
    if default_screen_runner:
        score_kwargs["market_coldness_evidence"] = market_coldness_evidence
        score_kwargs["dcf_skip_classifications"] = dcf.skip_classifications
        score_kwargs["quality_history_evidence"] = quality_history_evidence
        score_kwargs["quality_history_loader"] = captured_loader
        score_kwargs["quality_history_progress_cb"] = quality_history_progress_cb
    scores = screen_runner(canonical_financials, canonical_quotes, **score_kwargs)
    if not isinstance(scores, pd.DataFrame):
        raise TypeError("screen runner must return a pandas DataFrame")
    outcome = MarketAnalysisOutcome(
        scores=scores,
        dcf_results=dcf.results,
        issues=dcf.issues,
        dcf_attempted=dcf.attempted,
        dcf_skipped=dcf.skipped,
        dcf_skip_reasons=dcf.skip_reasons,
        dcf_skip_classifications=dcf.skip_classifications,
        quality_history_evidence=dict(sorted(captured_quality_history.items())),
    )
    if not enforce_quality:
        return outcome
    expected_code_set = set(canonical_financials)
    expected = len(expected_code_set) if expected_companies is None else expected_companies
    if expected != len(expected_code_set):
        raise ValueError(
            f"expected_companies {expected} does not match canonical analysis universe {len(expected_code_set)}"
        )
    quality = validate_market_analysis_quality(
        outcome,
        expected_companies=expected,
        expected_codes=expected_code_set,
        previous=previous_quality,
    )
    return MarketAnalysisOutcome(
        scores=outcome.scores,
        dcf_results=outcome.dcf_results,
        issues=outcome.issues,
        dcf_attempted=outcome.dcf_attempted,
        dcf_skipped=outcome.dcf_skipped,
        dcf_skip_reasons=outcome.dcf_skip_reasons,
        quality=quality,
        dcf_skip_classifications=outcome.dcf_skip_classifications,
        quality_history_evidence=outcome.quality_history_evidence,
    )
