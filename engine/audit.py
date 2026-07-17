"""Deterministic company-sample audits for the complete valuation/scoring chain."""

from __future__ import annotations

import json
import hashlib
import importlib.metadata
import math
import os
import platform
import random
import re
import shutil
import statistics

# Fixed-argv local Git provenance checks only; no shell is involved.
import subprocess  # nosec B404
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

import config as _production_config
from data.financial_source_evidence import FinancialSourceEvidenceError, zero_capex_evidence
from engine import buy_screener as _production_screener
from engine.buy_screener import validate_screening_result
from engine.dcf import ReportingPeriodContract
from engine.pipeline import (
    AnalysisQualityError,
    MarketAnalysisOutcome,
    PipelineIssue,
    compute_dcf_batch,
    run_market_analysis,
    validate_market_analysis_quality,
)
from engine.valuation_status import normalize_dcf_skip_classification


def _normalise_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


@dataclass(frozen=True)
class RandomSampleAudit:
    seed: int
    sample_size: int
    sample_codes: tuple[str, ...]
    scores: pd.DataFrame
    dcf_results: Mapping[str, Mapping[str, Any]]
    dcf_skip_reasons: Mapping[str, str]
    dcf_skip_classifications: Mapping[str, Mapping[str, str]]
    pipeline_issues: tuple[PipelineIssue, ...]
    engine_invariant_errors: tuple[str, ...]
    scoring_replay_errors: tuple[str, ...]
    valuation_replay_errors: tuple[str, ...]
    independent_errors: tuple[str, ...]
    analysis_quality: Mapping[str, Any]
    provenance: Mapping[str, Any]
    eligible_universe_size: int

    @property
    def invariant_errors(self) -> tuple[str, ...]:
        """Backward-compatible aggregate; sources remain separately labelled."""
        return (
            self.engine_invariant_errors
            + self.scoring_replay_errors
            + self.valuation_replay_errors
            + self.independent_errors
        )


_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_CODE_DIRS = (_ROOT / "data", _ROOT / "desktop", _ROOT / "engine", _ROOT / "ui", _ROOT / "tools")
_AUDIT_WEIGHTS: dict[str, dict[str, float]] = {
    "type1": {"1a": 0.30, "1b": 0.35, "1c": 0.20, "1d": 0.15},
    "type2": {"2a": 0.25, "2b": 0.30, "2c": 0.25, "2d": 0.20},
    "type3": {"3a": 0.25, "3b": 0.20, "3c": 0.20, "3d": 0.25, "3e": 0.10},
    "type4": {"4a": 0.25, "4b": 0.25, "4c": 0.20, "4d": 0.15, "4e": 0.08, "4f": 0.07},
    "type5": {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10},
    "type6": {"6a": 0.25, "6b": 0.20, "6c": 0.15, "6d": 0.25, "6e": 0.15},
    "type7": {"7a": 1.0 / 3.0, "7b": 1.0 / 3.0, "7c": 1.0 / 3.0},
}
_AUDIT_PRIORITY = ("type1", "type2", "type5", "type3", "type4", "type6", "type7")
_AUDIT_NAMES = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ 高风险早期/困境型",
    "type7": "7️⃣ 优质股权型",
}
_AUDIT_QUALIFY_THRESHOLD = 7.0
_AUDIT_REASON_MAX_LENGTH = 20
_AUDIT_TYPE_STATUSES = {
    "triggered",
    "observe",
    "not_triggered",
    "vetoed",
    "conditional",
    "not_applicable",
    "insufficient_evidence",
    "blocked",
}
_AUDIT_NON_DIAGNOSTIC_STATUSES = {"not_applicable", "insufficient_evidence"}
_AUDIT_TYPE7_MODEL_ID = "patch6-type7-quality-equity-v1"
_AUDIT_TYPE7_TEMPLATE_WEIGHTS = {
    "template1": {f"t1_{index:02d}": 5.0 for index in range(1, 21)},
    "template5": {
        "t5_i1": 12.0,
        "t5_i2": 9.0,
        "t5_i3": 9.0,
        "t5_q1": 14.0,
        "t5_q2": 12.0,
        "t5_q3": 8.0,
        "t5_q4": 6.0,
        "t5_v1": 9.0,
        "t5_v2": 12.0,
        "t5_v3": 9.0,
    },
}
_AUDIT_TYPE7_PATCH_WEIGHTS = {
    "p5_business": {"p5_b1": 5.0, "p5_b2": 5.0, "p5_b3": 5.0, "p5_b4": 5.0},
    "p5_moat": {"p5_m1": 8.0, "p5_m2": 6.0, "p5_m3": 6.0},
    "p5_culture": {"p5_c1": 6.0, "p5_c2": 5.0, "p5_c3": 5.0, "p5_c4": 4.0},
    "p5_industry": {"p5_i1": 8.0, "p5_i2": 6.0, "p5_i3": 6.0},
    "p5_safety": {"p5_s1": 8.0, "p5_s2": 6.0, "p5_s3": 6.0},
}
_AUDIT_TYPE7_PREREQUISITES = {
    "core_modules_80pct",
    "technology_patch4",
    "three_year_financials",
    "latest_quote_and_valuation",
    "three_external_reports",
    "ten_year_return_and_five_year_valuation",
}
_AUDIT_BAND_WACC_DELTA = 0.005
_AUDIT_BUBBLE_RATIO = 1.20
_AUDIT_DEEP_SAFETY_RATIO = 0.80
_AUDIT_EQUITY_RISK_PREMIUM = 0.05799671740067751
_AUDIT_FCF_MARGIN_FLOOR = 0.0
_AUDIT_FCF_MARGIN_LONG_TERM = 0.04
_AUDIT_FORECAST_YEARS = 5
_AUDIT_RISK_FREE_RATE = 0.017406
_AUDIT_NORMALISATION_PREMIUM_CAP = 1.25
_AUDIT_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_AUDIT_TTM_FCFF_FORMULA_VERSION = "ttm_cfo_less_capex_v2"
_AUDIT_TTM_REVENUE_FORMULA_VERSION = "ttm_revenue_v1"
_AUDIT_TTM_SOURCE_UNIT = "CNY"
_AUDIT_NONQUALITY_MARGIN_FLOOR = 0.08
_AUDIT_NONQUALITY_MARGIN_CAP = 0.25
_AUDIT_NONQUALITY_MARGIN_MULTIPLIER = 3.0
_RULE_FILES = (
    _ROOT / "config.py",
    _ROOT / "engine" / "buy_screener.py",
    _ROOT / "engine" / "dcf.py",
    _ROOT / "engine" / "market_coldness.py",
    _ROOT / "engine" / "quality_equity.py",
    _ROOT / "engine" / "quantitative_evidence.py",
    _ROOT / "engine" / "risk.py",
    _ROOT / "engine" / "scenarios.py",
    _ROOT / "engine" / "valuation_status.py",
    _ROOT / "data" / "capex_evidence.py",
    _ROOT / "data" / "datacenter.py",
    _ROOT / "data" / "financial_indicator_evidence.py",
    _ROOT / "data" / "financial_source_evidence.py",
    _ROOT / "data" / "financial_balance_sheet_evidence.json",
    _ROOT / "data" / "financial_zero_capex_evidence.json",
    _ROOT / "data" / "financial_zero_revenue_evidence.json",
    _ROOT / "data" / "industry.py",
    _ROOT / "data" / "market_coldness.py",
    _ROOT / "data" / "market_history.py",
    _ROOT / "data" / "quality_history.py",
)
_INDUSTRY_FILES = (
    _ROOT / "data" / "industry.py",
    _ROOT / "data" / "industry_f10.json",
    _ROOT / "data" / "industry_em_map.json",
    _ROOT / "data" / "industry_capco_2025h2.json",
    _ROOT / "data" / "industry_exchange_new_listings_2026.json",
)
_DEPENDENCY_FILES = (
    _ROOT / "requirements-bootstrap.txt",
    _ROOT / "requirements.txt",
    _ROOT / "requirements-lock.txt",
    _ROOT / "requirements-test.txt",
    _ROOT / "requirements-dev.txt",
    _ROOT / "requirements-dev-lock.txt",
    _ROOT / "pyproject.toml",
)
PATCH6_SOURCE_PATH = r"E:\模板汇总MD\补丁6.md"
PATCH6_SOURCE_SHA256 = "aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6"
TYPE7_SOURCE_DOCUMENTS = {
    "template1": {
        "path_at_model_authoring": r"E:\模板汇总MD\第1模板.md",
        "sha256": "98d8a101a08cdb122afd23c793faa3edf5e4e426eae09e7fc20901476ea95b1d",
    },
    "template5": {
        "path_at_model_authoring": r"E:\模板汇总MD\第5模板.md",
        "sha256": "37a9cd43633bcd0bc1f2811738d48a7d1cff659e5ef11b6fd9152f2ed0686946",
    },
    "patch5": {
        "path_at_model_authoring": r"E:\模板汇总MD\补丁5.md",
        "sha256": "8e1c5114be74254d686ac2b65ec7b3563e09f6c3b3f9a82b43e4d60a84ca42a4",
    },
    "patch6": {
        "path_at_model_authoring": PATCH6_SOURCE_PATH,
        "sha256": PATCH6_SOURCE_SHA256,
    },
}
RISK_PARAMETER_SOURCES = {
    "model_as_of": "2026-07-15",
    "risk_free_rate_as_of": "2026-07-15",
    "risk_free_rate_source": "ChinaBond China Government Bond Yield Curve 10Y",
    "risk_free_rate_source_url": ("https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN"),
    "equity_risk_premium_as_of": "2026-04-01",
    "equity_risk_premium_basis": "china_rating_based_total_erp",
    "equity_risk_premium_source_url": ("https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx"),
    "ctrypremApr26.xlsx": "2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e",
    "industry_data_as_of": "2026-01-05",
    "betaChina.xls": "ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9",
    "waccChina.xls": "525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6",
}


def _sha256_files(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in paths if item.is_file()), key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(_ROOT).as_posix()
        except ValueError:
            relative = path.as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def _input_snapshot_hash(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
) -> str:
    digest = hashlib.sha256()
    quote_rows = quotes.copy()
    quote_rows["code"] = quote_rows["code"].map(_normalise_code)
    for row in quote_rows.sort_values("code", kind="stable").to_dict(orient="records"):
        digest.update(_canonical_json(row))
        digest.update(b"\n")
    for raw_code in sorted(financials, key=_normalise_code):
        digest.update(_normalise_code(raw_code).encode("ascii", errors="ignore"))
        digest.update(b"\0")
        digest.update(_canonical_json(financials[raw_code]))
        digest.update(b"\n")
    return digest.hexdigest()


def _git_metadata() -> dict[str, Any]:
    result: dict[str, Any] = {"commit": None, "dirty": None}
    git_executable = shutil.which("git")
    if not git_executable:
        return result
    try:
        # ``git_executable`` is an absolute path and every argument is fixed.
        commit = subprocess.run(  # nosec B603
            [git_executable, "rev-parse", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(  # nosec B603
            [git_executable, "status", "--porcelain"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        result = {"commit": commit or None, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _runtime_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in ("numpy", "orjson", "pandas", "pillow", "plotly", "requests", "streamlit"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def audit_state_hashes() -> dict[str, str]:
    """Hash every mutable source/contract input used by an audit run."""
    production_files = [path for directory in _PRODUCTION_CODE_DIRS for path in directory.rglob("*.py")] + [
        _ROOT / "app.py",
        _ROOT / "config.py",
    ]
    return {
        "code_sha256": _sha256_files(production_files),
        "rules_sha256": _sha256_files(_RULE_FILES),
        "industry_sha256": _sha256_files(_INDUSTRY_FILES),
        "dependency_manifest_sha256": _sha256_files(_DEPENDENCY_FILES),
    }


def _build_provenance(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    eligible_codes: tuple[str, ...],
    *,
    snapshot_sha256: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state_hashes = audit_state_hashes()
    provenance = {
        "audit_schema_version": 3,
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "snapshot_content_sha256": _input_snapshot_hash(quotes, financials),
        "snapshot_artifact_sha256": snapshot_sha256,
        "eligible_universe_sha256": hashlib.sha256("\n".join(eligible_codes).encode("ascii")).hexdigest(),
        **state_hashes,
        "patch6_source": {
            "path_at_model_authoring": PATCH6_SOURCE_PATH,
            "sha256": PATCH6_SOURCE_SHA256,
        },
        "type7_source_documents": {key: dict(value) for key, value in TYPE7_SOURCE_DOCUMENTS.items()},
        "risk_parameter_sources": dict(RISK_PARAMETER_SOURCES),
        "scoring_verification_scope": {
            "same_source_replay": "recomputes every published field from reordered production inputs",
            "same_source_valuation_replay": "recomputes valuation existence, payloads, skip reasons and sampled issues",
            "independent_runtime_checks": "recompute weights, trigger relations, ranking, bear cases, valuation formulas and source binding",
            "business_rule_oracle": "fixed expected vectors and mutation/boundary tests in tests/test_buy_screener_rules.py",
        },
        "git": _git_metadata(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": _runtime_versions(),
        },
    }
    if isinstance(metadata, Mapping):
        provenance["caller_metadata"] = dict(metadata)
    return provenance


def _analysis_skip_reasons(analysis: MarketAnalysisOutcome) -> dict[str, str]:
    for field in ("dcf_skip_reasons", "dcf_skipped_reasons", "skipped_reasons"):
        value = getattr(analysis, field, None)
        if isinstance(value, Mapping):
            return {_normalise_code(code): str(reason or "evidence unavailable") for code, reason in value.items()}
    return {}


def _analysis_skip_classifications(analysis: MarketAnalysisOutcome) -> dict[str, dict[str, str]]:
    value = getattr(analysis, "dcf_skip_classifications", None)
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, str]] = {}
    for raw_code, raw_classification in value.items():
        classification = normalize_dcf_skip_classification(raw_classification)
        if classification is not None:
            result[_normalise_code(raw_code)] = classification
    return result


def _market_coldness_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Record bulk evidence identity without embedding the whole market payload."""
    if evidence is None:
        return {
            "provided": False,
            "evidence_count": 0,
            "eligible_evidence_count": 0,
            "eligible_evidence_coverage": 0.0,
            "evidence_sha256": None,
            "sources": [],
            "as_of_sessions": [],
        }
    normalized: dict[str, Mapping[str, Any]] = {}
    sources: set[str] = set()
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not code or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("market_coldness_evidence contains an invalid or duplicate code payload")
        normalized[code] = payload
        source_evidence = payload.get("market_coldness_score_evidence")
        if isinstance(source_evidence, Mapping):
            source = source_evidence.get("source")
            as_of = source_evidence.get("as_of")
            if isinstance(source, str) and source.strip():
                sources.add(source.strip())
            if isinstance(as_of, str) and as_of.strip():
                as_of_sessions.add(as_of.strip())
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": True,
        "evidence_count": len(normalized),
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest(),
        "sources": sorted(sources),
        "as_of_sessions": sorted(as_of_sessions),
    }


def _quality_history_evidence_provenance(
    evidence: Mapping[str, Mapping[str, Any]] | None,
    eligible_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Bind every acquired Type 7 long-history record without duplicating it."""
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, Mapping):
        raise ValueError("quality_history_evidence must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    available = 0
    as_of_sessions: set[str] = set()
    for raw_code, payload in evidence.items():
        code = _normalise_code(raw_code)
        if not re.fullmatch(r"[036][0-9]{5}", code) or code in normalized or not isinstance(payload, Mapping):
            raise ValueError("quality_history_evidence contains an invalid or duplicate code payload")
        if str(payload.get("code") or "") != code or payload.get("model_id") != "type7-market-history-v1":
            raise ValueError("quality_history_evidence identity differs from its mapping key")
        normalized[code] = payload
        available += int(payload.get("available") is True)
        as_of = payload.get("as_of")
        if isinstance(as_of, str) and as_of:
            as_of_sessions.add(as_of)
    eligible_count = len(set(eligible_codes) & set(normalized))
    return {
        "provided": bool(normalized),
        "evidence_count": len(normalized),
        "available_count": available,
        "eligible_evidence_count": eligible_count,
        "eligible_evidence_coverage": eligible_count / len(eligible_codes),
        "evidence_sha256": hashlib.sha256(_canonical_json(normalized)).hexdigest() if normalized else None,
        "as_of_sessions": sorted(as_of_sessions),
    }


_AUDIT_SCENARIOS = ("pessimistic", "neutral", "optimistic")
_AUDIT_WACC_SHIFT = {"pessimistic": 0.010, "neutral": 0.0, "optimistic": -0.005}
_AUDIT_FINANCIAL_INDUSTRIES = {"BANK", "INSURANCE", "SECURITIES"}
_AUDIT_PARENT_EQUITY_KEYS = (
    "PARENT_EQUITY",
    "TOTAL_PARENT_EQUITY",
    "TOTAL_EQUITY_ATTR_P",
    "EQUITY_ATTRIBUTABLE_TO_PARENT",
    "PARENT_NET_ASSETS",
    "归属于母公司股东权益",
)
_AUDIT_MINORITY_EQUITY_KEYS = (
    "MINORITY_EQUITY",
    "MINORITY_INTEREST",
    "少数股东权益",
)


def _finite(value: Any) -> float | None:
    """Return a finite non-boolean scalar; pandas missing values fail closed."""
    if value is None or isinstance(value, bool) or value is pd.NA or value is pd.NaT:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    # NumPy boolean scalars are legitimate dataframe scalars, but integers,
    # truthy strings and missing values must never pass as public booleans.
    if type(value).__name__ == "bool_" and type(value).__module__.startswith("numpy"):
        return bool(value)
    return None


def _optional_text(value: Any) -> str | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return str(value)


def _close(actual: Any, expected: float, *, rel_tol: float = 1e-8, abs_tol: float = 1e-9) -> bool:
    number = _finite(actual)
    return number is not None and math.isclose(number, expected, rel_tol=rel_tol, abs_tol=abs_tol)


def _audit_type7_ledger(code: str, ledger: Any, status: Any) -> list[str]:
    """Replay the Type 7 source ledgers without calling its production module."""
    prefix = f"{code}:type7:"
    if not isinstance(ledger, Mapping):
        return [prefix + "ledger missing"]
    if status == "not_applicable":
        if (
            ledger.get("schema_version") != 1
            or ledger.get("model_id") != _AUDIT_TYPE7_MODEL_ID
            or ledger.get("code") != code
            or ledger.get("applicable") is not False
            or not str(ledger.get("reason") or "").strip()
        ):
            return [prefix + "not-applicable ledger invalid"]
        return []

    errors: list[str] = []
    if (
        ledger.get("schema_version") != 1
        or ledger.get("model_id") != _AUDIT_TYPE7_MODEL_ID
        or ledger.get("code") != code
        or ledger.get("source_rule") != "Template1>70 AND Template5>70 AND Patch5>70"
        or not _close(ledger.get("strict_threshold"), 70.0, rel_tol=0.0)
    ):
        errors.append(prefix + "model identity invalid")

    templates: dict[str, Mapping[str, Any]] = {}
    template_items: dict[str, dict[str, Mapping[str, Any]]] = {}
    for section_key, expected_weights in _AUDIT_TYPE7_TEMPLATE_WEIGHTS.items():
        section = ledger.get(section_key)
        items = section.get("items") if isinstance(section, Mapping) else None
        if not isinstance(section, Mapping) or not isinstance(items, list) or len(items) != len(expected_weights):
            errors.append(prefix + f"{section_key} structure invalid")
            continue
        indexed: dict[str, Mapping[str, Any]] = {}
        for item in items:
            key = item.get("key") if isinstance(item, Mapping) else None
            if not isinstance(key, str) or key not in expected_weights or key in indexed:
                errors.append(prefix + f"{section_key} item identity invalid")
                continue
            score = _finite(item.get("score"))
            complete = _strict_bool(item.get("complete"))
            weight = expected_weights[key]
            if (
                score is None
                or not 0 <= score <= 10
                or complete is None
                or not _close(item.get("weight"), weight, rel_tol=0.0)
                or not _close(item.get("points"), round(score * weight / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
                or not str(item.get("formula") or "").strip()
                or not isinstance(item.get("inputs"), Mapping)
            ):
                errors.append(prefix + f"{section_key} item arithmetic invalid")
            indexed[key] = item
        if set(indexed) != set(expected_weights):
            errors.append(prefix + f"{section_key} item set invalid")
            continue
        expected_score = round(sum(float(item["points"]) for item in indexed.values()), 2)
        expected_coverage = round(
            sum(expected_weights[key] for key, item in indexed.items() if item.get("complete") is True) / 100.0,
            4,
        )
        if not _close(section.get("score"), expected_score, rel_tol=0.0, abs_tol=0.0001):
            errors.append(prefix + f"{section_key} total mismatch")
        if not _close(section.get("coverage"), expected_coverage, rel_tol=0.0, abs_tol=0.0001):
            errors.append(prefix + f"{section_key} coverage mismatch")
        templates[section_key] = section
        template_items[section_key] = indexed

    patch5 = ledger.get("patch5")
    dimensions = patch5.get("dimensions") if isinstance(patch5, Mapping) else None
    patch_sections: dict[str, Mapping[str, Any]] = {}
    if not isinstance(patch5, Mapping) or not isinstance(dimensions, list) or len(dimensions) != 5:
        errors.append(prefix + "patch5 structure invalid")
    else:
        for section in dimensions:
            key = section.get("key") if isinstance(section, Mapping) else None
            expected_weights = _AUDIT_TYPE7_PATCH_WEIGHTS.get(key) if isinstance(key, str) else None
            components = section.get("components") if isinstance(section, Mapping) else None
            if (
                expected_weights is None
                or key in patch_sections
                or not isinstance(components, list)
                or len(components) != len(expected_weights)
                or not _close(section.get("max_points"), 20.0, rel_tol=0.0)
            ):
                errors.append(prefix + "patch5 dimension structure invalid")
                continue
            indexed: dict[str, Mapping[str, Any]] = {}
            for component in components:
                component_key = component.get("key") if isinstance(component, Mapping) else None
                if (
                    not isinstance(component_key, str)
                    or component_key not in expected_weights
                    or component_key in indexed
                ):
                    errors.append(prefix + f"patch5 {key} component identity invalid")
                    continue
                score = _finite(component.get("score"))
                complete = _strict_bool(component.get("complete"))
                maximum = expected_weights[component_key]
                if (
                    score is None
                    or not 0 <= score <= 10
                    or complete is None
                    or not _close(component.get("max_points"), maximum, rel_tol=0.0)
                    or not _close(
                        component.get("points"),
                        round(score * maximum / 10.0, 4),
                        rel_tol=0.0,
                        abs_tol=0.0001,
                    )
                ):
                    errors.append(prefix + f"patch5 {key} component arithmetic invalid")
                indexed[component_key] = component
            if set(indexed) != set(expected_weights):
                errors.append(prefix + f"patch5 {key} component set invalid")
                continue
            expected_points = round(sum(float(item["points"]) for item in indexed.values()), 4)
            expected_complete = all(item.get("complete") is True for item in indexed.values())
            if not _close(section.get("points"), expected_points, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + f"patch5 {key} points mismatch")
            if section.get("complete") is not expected_complete:
                errors.append(prefix + f"patch5 {key} completeness mismatch")
            patch_sections[key] = section
        if set(patch_sections) != set(_AUDIT_TYPE7_PATCH_WEIGHTS):
            errors.append(prefix + "patch5 dimension set invalid")
        else:
            expected_score = round(sum(float(section["points"]) for section in patch_sections.values()), 2)
            expected_coverage = round(
                sum(20.0 for section in patch_sections.values() if section.get("complete") is True) / 100.0,
                4,
            )
            safety = patch_sections["p5_safety"]
            if not _close(patch5.get("score"), expected_score, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + "patch5 total mismatch")
            if not _close(patch5.get("coverage"), expected_coverage, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + "patch5 coverage mismatch")
            if not _close(
                patch5.get("safety_margin_score"),
                round(float(safety["points"]), 2),
                rel_tol=0.0,
                abs_tol=0.0001,
            ) or patch5.get("safety_margin_complete") is not safety.get("complete"):
                errors.append(prefix + "patch5 safety mismatch")

    scores = ledger.get("scores")
    score_values: dict[str, float] = {}
    expected_sections = {
        "template1": templates.get("template1"),
        "template5": templates.get("template5"),
        "patch5": patch5,
    }
    if not isinstance(scores, Mapping) or set(scores) != set(expected_sections):
        errors.append(prefix + "score map invalid")
    else:
        for key, section in expected_sections.items():
            value = _finite(scores.get(key))
            expected = _finite(section.get("score")) if isinstance(section, Mapping) else None
            if value is None or expected is None or not _close(value, expected, rel_tol=0.0, abs_tol=0.0001):
                errors.append(prefix + f"{key} score binding invalid")
            if value is not None:
                score_values[key] = value
    strict = {key: value > 70.0 for key, value in score_values.items()}
    strict_checks = ledger.get("strict_checks")
    intersection = len(strict) == 3 and all(strict.values())
    if not isinstance(strict_checks, Mapping) or dict(strict_checks) != strict:
        errors.append(prefix + "strict checks mismatch")
    if ledger.get("all_scores_strictly_above_70") is not intersection:
        errors.append(prefix + "strict intersection mismatch")

    prerequisites = ledger.get("prerequisites")
    prerequisites_complete = False
    prerequisite_passes: dict[str, bool] = {}
    if not isinstance(prerequisites, Mapping) or set(prerequisites) != _AUDIT_TYPE7_PREREQUISITES:
        errors.append(prefix + "prerequisites invalid")
    else:
        passed: list[bool] = []
        for key, record in prerequisites.items():
            value = _strict_bool(record.get("passed")) if isinstance(record, Mapping) else None
            if value is None:
                errors.append(prefix + "prerequisite pass flag invalid")
            else:
                passed.append(value)
                prerequisite_passes[key] = value
        prerequisites_complete = len(passed) == len(_AUDIT_TYPE7_PREREQUISITES) and all(passed)
    if ledger.get("prerequisites_complete") is not prerequisites_complete:
        errors.append(prefix + "prerequisite intersection mismatch")

    quote_as_of: date | None = None
    if isinstance(prerequisites, Mapping):
        valuation_prerequisite = prerequisites.get("latest_quote_and_valuation")
        t1_items = template_items.get("template1", {})
        expected_valuation_complete = t1_items.get("t1_20", {}).get("complete") is True
        if isinstance(valuation_prerequisite, Mapping):
            raw_as_of = valuation_prerequisite.get("as_of")
            try:
                quote_as_of = date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None
            except ValueError:
                quote_as_of = None
            expected_valuation_passed = bool(
                expected_valuation_complete and quote_as_of is not None and quote_as_of <= date.today()
            )
            if (
                set(valuation_prerequisite) != {"passed", "as_of", "valuation_complete"}
                or valuation_prerequisite.get("valuation_complete") is not expected_valuation_complete
                or valuation_prerequisite.get("passed") is not expected_valuation_passed
            ):
                errors.append(prefix + "valuation prerequisite mismatch")
        else:
            errors.append(prefix + "valuation prerequisite mismatch")

        report_prerequisite = prerequisites.get("three_external_reports")
        report_valid = isinstance(report_prerequisite, Mapping) and set(report_prerequisite) == {
            "passed",
            "source_count",
            "distinct_publishers",
            "sources",
        }
        sources = report_prerequisite.get("sources") if isinstance(report_prerequisite, Mapping) else None
        normalized_sources: list[dict[str, str]] = []
        identities: set[str] = set()
        urls: set[str] = set()
        if not isinstance(sources, list) or len(sources) > 20 or (sources and quote_as_of is None):
            report_valid = False
        else:
            for source in sources:
                fields = {"title", "publisher", "url", "as_of", "evidence_id"}
                if (
                    not isinstance(source, Mapping)
                    or set(source) != fields
                    or any(not isinstance(source.get(field), str) for field in fields)
                ):
                    report_valid = False
                    break
                item = {field: str(source[field]).strip() for field in fields}
                try:
                    parsed = urlsplit(item["url"])
                    _ = parsed.port
                    source_date = date.fromisoformat(item["as_of"])
                except (ValueError, TypeError):
                    report_valid = False
                    break
                identity = item["evidence_id"].casefold()
                canonical_url = item["url"].casefold()
                if (
                    any(
                        not text or len(text) > 300 or any(ord(character) < 32 for character in text)
                        for text in item.values()
                    )
                    or parsed.scheme.lower() != "https"
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or bool(parsed.fragment)
                    or source_date > quote_as_of
                    or identity in identities
                    or canonical_url in urls
                ):
                    report_valid = False
                    break
                identities.add(identity)
                urls.add(canonical_url)
                normalized_sources.append(item)
        normalized_sources.sort(key=lambda item: (item["as_of"], item["publisher"], item["evidence_id"]))
        publisher_count = len({item["publisher"].casefold() for item in normalized_sources})
        expected_report_pass = bool(report_valid and len(normalized_sources) >= 3 and publisher_count >= 3)
        if not (
            report_valid
            and normalized_sources == sources
            and report_prerequisite.get("source_count") == len(normalized_sources)
            and report_prerequisite.get("distinct_publishers") == publisher_count
            and report_prerequisite.get("passed") is expected_report_pass
        ):
            errors.append(prefix + "external reports prerequisite mismatch")

        history_prerequisite = prerequisites.get("ten_year_return_and_five_year_valuation")
        history_inputs = t1_items.get("t1_19", {}).get("inputs", {})
        shareholder_input = history_inputs.get("shareholder_return") if isinstance(history_inputs, Mapping) else None
        expected_history_pass = bool(
            isinstance(shareholder_input, Mapping)
            and shareholder_input.get("available") is True
            and template_items.get("template5", {}).get("t5_v1", {}).get("complete") is True
        )
        history_valid = isinstance(history_prerequisite, Mapping) and set(history_prerequisite) == {
            "passed",
            "as_of",
        }
        history_as_of = history_prerequisite.get("as_of") if isinstance(history_prerequisite, Mapping) else None
        try:
            history_date = date.fromisoformat(history_as_of) if isinstance(history_as_of, str) else None
        except ValueError:
            history_date = None
        if not (
            history_valid
            and history_prerequisite.get("passed") is expected_history_pass
            and (history_as_of is None or history_date is not None)
            and (not expected_history_pass or history_as_of == valuation_prerequisite.get("as_of"))
        ):
            errors.append(prefix + "market history prerequisite mismatch")
    safety_complete = isinstance(patch5, Mapping) and patch5.get("safety_margin_complete") is True
    safety_score = _finite(patch5.get("safety_margin_score")) if isinstance(patch5, Mapping) else None
    safety_veto = bool(safety_complete and safety_score is not None and safety_score < 8.0)
    if ledger.get("safety_veto") is not safety_veto:
        errors.append(prefix + "safety veto mismatch")
    expected_trigger = intersection and prerequisites_complete and not safety_veto
    if ledger.get("triggered") is not expected_trigger:
        errors.append(prefix + "trigger mismatch")

    expected_upper: dict[str, float] = {}
    t1_items = template_items.get("template1", {})
    t5_items = template_items.get("template5", {})
    if (
        len(score_values) == 3
        and {"t1_18", "t1_19", "t1_20"}.issubset(t1_items)
        and {"t5_v1", "t5_v3"}.issubset(t5_items)
        and "p5_safety" in patch_sections
    ):
        safety_components = {
            component.get("key"): component
            for component in patch_sections["p5_safety"].get("components", [])
            if isinstance(component, Mapping)
        }
        valuation_component = safety_components.get("p5_s1")
        dcf_score = _finite(t1_items["t1_20"].get("score"))
        if isinstance(valuation_component, Mapping) and dcf_score is not None:
            expected_upper = {
                "template1": round(
                    min(
                        100.0,
                        score_values["template1"]
                        - float(t1_items["t1_18"]["points"])
                        - float(t1_items["t1_19"]["points"])
                        + 10.0,
                    ),
                    2,
                ),
                "template5": round(
                    min(
                        100.0,
                        score_values["template5"]
                        - float(t5_items["t5_v1"]["points"])
                        - float(t5_items["t5_v3"]["points"])
                        + 18.0,
                    ),
                    2,
                ),
                "patch5": round(
                    min(
                        100.0,
                        score_values["patch5"]
                        - float(valuation_component["points"])
                        + 8.0 * round((dcf_score + 10.0) / 2.0, 2) / 10.0,
                    ),
                    2,
                ),
            }
    published_upper = ledger.get("upper_bounds_without_history")
    if (
        len(expected_upper) != 3
        or not isinstance(published_upper, Mapping)
        or set(published_upper) != set(expected_upper)
        or any(
            not _close(published_upper.get(key), value, rel_tol=0.0, abs_tol=0.0001)
            for key, value in expected_upper.items()
        )
    ):
        errors.append(prefix + "history upper bounds mismatch")
    expected_request = bool(
        len(prerequisite_passes) == len(_AUDIT_TYPE7_PREREQUISITES)
        and not prerequisite_passes.get("ten_year_return_and_five_year_valuation", False)
        and all(value for key, value in prerequisite_passes.items() if key != "ten_year_return_and_five_year_valuation")
        and len(expected_upper) == 3
        and all(value > 70.0 for value in expected_upper.values())
    )
    if ledger.get("history_request_needed") is not expected_request:
        errors.append(prefix + "history request decision mismatch")
    return errors


def _audit_compact_reason(value: Any) -> str:
    text = str(value or "数据不足").strip()
    return text[:_AUDIT_REASON_MAX_LENGTH]


def _audit_bear_case(type_key: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    weights = _AUDIT_WEIGHTS[type_key]
    raw_scores = payload.get("sub_scores")
    raw_reasons = payload.get("reasons")
    scores = raw_scores if isinstance(raw_scores, Mapping) else {}
    reasons = raw_reasons if isinstance(raw_reasons, Mapping) else {}
    clean_scores = {key: float(scores[key]) for key in weights}
    minimum = min(clean_scores.values())
    result: list[dict[str, Any]] = []
    for meta_key in ("_veto", "_condition", "_downgrade"):
        if reasons.get(meta_key):
            result.append(
                {
                    "dimension": meta_key,
                    "score": minimum,
                    "reason": _audit_compact_reason(reasons[meta_key]),
                }
            )
            if len(result) == 3:
                return result
    order = {key: index for index, key in enumerate(weights)}
    ranked = sorted(weights, key=lambda key: (clean_scores[key], -weights[key], order[key]))
    for key in ranked:
        result.append(
            {
                "dimension": key,
                "score": clean_scores[key],
                "reason": _audit_compact_reason(reasons.get(key)),
            }
        )
        if len(result) == 3:
            break
    return result


def _audit_dcf_value(
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
    """Independent scalar implementation of the two-stage FCFF formula."""
    values = (base_fcf, base_revenue, growth, wacc, terminal_growth, shares, net_debt, retention)
    if not all(math.isfinite(value) for value in values):
        return None
    if (
        base_fcf <= 0
        or base_revenue <= 0
        or shares <= 0
        or growth <= -1
        or wacc <= terminal_growth
        or terminal_growth <= -1
        or not 0 <= retention <= 1
    ):
        return None
    current_margin = base_fcf / base_revenue
    if not 0 < current_margin <= 1:
        return None
    target_margin = min(
        current_margin,
        max(_AUDIT_FCF_MARGIN_FLOOR, _AUDIT_FCF_MARGIN_LONG_TERM, current_margin * retention),
    )
    explicit_pv = 0.0
    final_revenue = 0.0
    final_discount = 0.0
    denominator = max(_AUDIT_FORECAST_YEARS - 1, 1)
    for year in range(1, _AUDIT_FORECAST_YEARS + 1):
        revenue = base_revenue * (1.0 + growth) ** year
        margin = current_margin + (target_margin - current_margin) * ((year - 1.0) / denominator)
        fcff = revenue * margin
        discount = (1.0 + wacc) ** year
        if not all(math.isfinite(value) for value in (revenue, margin, fcff, discount)) or discount <= 0:
            return None
        explicit_pv += fcff / discount
        final_revenue, final_discount = revenue, discount
    terminal_fcf = final_revenue * (1.0 + terminal_growth) * target_margin
    terminal_value = terminal_fcf / (wacc - terminal_growth)
    per_share = (explicit_pv + terminal_value / final_discount - net_debt) / shares
    return per_share if math.isfinite(per_share) and per_share > 0 else None


def _annual_values(records: Any, keys: tuple[str, ...]) -> dict[int, float]:
    rows = [records] if isinstance(records, Mapping) else records
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")
        if len(report_date) < 4 or not report_date[:4].isdigit():
            continue
        value = next((_finite(row.get(key)) for key in keys if _finite(row.get(key)) is not None), None)
        if value is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, value)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _equity_from_row(row: Mapping[str, Any]) -> float | None:
    parent = None
    for key in _AUDIT_PARENT_EQUITY_KEYS:
        value = _finite(row.get(key))
        if value is not None and value > 0:
            parent = value
            break
    total = _finite(row.get("TOTAL_EQUITY"))
    minority = next(
        (_finite(row.get(key)) for key in _AUDIT_MINORITY_EQUITY_KEYS if _finite(row.get(key)) is not None),
        None,
    )
    assets = _finite(row.get("TOTAL_ASSETS"))
    if parent is not None:
        if assets is not None and assets > 0 and parent > assets * 1.03 and (total is None or minority is None):
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
        if attributable > 0 and not (assets is not None and assets > 0 and total > assets * 1.03):
            return attributable
    return None


def _annual_equity(records: Any) -> dict[int, float]:
    rows = [records] if isinstance(records, Mapping) else records
    by_year: dict[int, tuple[bool, str, float]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")
        equity = _equity_from_row(row)
        if len(report_date) < 4 or not report_date[:4].isdigit() or equity is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, equity)
        existing = by_year.get(year)
        if existing is None or candidate[:2] > existing[:2]:
            by_year[year] = candidate
    return {year: item[2] for year, item in by_year.items()}


def _latest_equity(records: Any) -> float | None:
    rows = [records] if isinstance(records, Mapping) else records
    candidates: list[tuple[str, float]] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            equity = _equity_from_row(row)
            if equity is not None:
                candidates.append((str(row.get("REPORT_DATE") or ""), equity))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _audit_interim_rows(value: Any) -> list[Mapping[str, Any]]:
    rows = [value] if isinstance(value, Mapping) else value
    valid_rows: list[Mapping[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "").strip()
        try:
            datetime.strptime(report_date, "%Y-%m-%d")
        except ValueError:
            continue
        valid_rows.append(row)
    return sorted(
        valid_rows,
        key=lambda row: str(row.get("REPORT_DATE") or "")[:10],
    )


def _audit_same_period_yoy(
    rows: list[Mapping[str, Any]],
    keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    current, prior, pair_basis = _audit_same_period_pair(rows, keys)
    if pair_basis != "same_period_yoy" or current is None:
        return current, None, pair_basis
    if prior is None:
        return current, None, "invalid_same_period_comparator"
    if prior <= 0:
        basis = "same_period_turnaround" if current > 0 else "same_period_nonpositive_comparison"
        return current, None, basis
    yoy = current / prior - 1.0
    return current, yoy, "same_period_yoy" if math.isfinite(yoy) else "invalid_same_period_comparator"


def _audit_same_period_pair(
    rows: list[Mapping[str, Any]],
    keys: tuple[str, ...],
) -> tuple[float | None, float | None, str]:
    if not rows:
        return None, None, "missing_current_period"
    latest = rows[-1]
    report_date = str(latest.get("REPORT_DATE") or "")[:10]
    current = _first_finite(latest, keys)
    if len(report_date) != 10 or not report_date[:4].isdigit() or current is None:
        return current, None, "missing_current_period"
    prior_date = f"{int(report_date[:4]) - 1}{report_date[4:]}"
    prior_row = next(
        (row for row in rows if str(row.get("REPORT_DATE") or "")[:10] == prior_date),
        None,
    )
    if prior_row is None:
        return current, None, "missing_same_period_comparator"
    prior = _first_finite(prior_row, keys)
    if prior is None:
        return current, None, "invalid_same_period_comparator"
    return current, prior, "same_period_yoy"


def _audit_current_period_evidence(financial: Mapping[str, Any]) -> dict[str, Any]:
    income = _audit_interim_rows(financial.get("income_interim", []))
    cashflow = _audit_interim_rows(financial.get("cashflow_interim", []))
    revenue, revenue_yoy, revenue_basis = _audit_same_period_yoy(
        income,
        ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
    )
    profit, profit_yoy, profit_basis = _audit_same_period_yoy(income, ("PARENT_NETPROFIT",))
    ocf, ocf_yoy, ocf_basis = _audit_same_period_yoy(cashflow, ("NETCASH_OPERATE",))
    return {
        "report_date": str(income[-1].get("REPORT_DATE") or "")[:10] if income else None,
        "revenue": revenue,
        "revenue_yoy": revenue_yoy,
        "revenue_yoy_basis": revenue_basis,
        "profit": profit,
        "profit_yoy": profit_yoy,
        "profit_yoy_basis": profit_basis,
        "operating_cash_flow": ocf,
        "operating_cash_flow_yoy": ocf_yoy,
        "operating_cash_flow_yoy_basis": ocf_basis,
    }


def _audit_financial_source_errors(
    code: str,
    financial: Mapping[str, Any],
    expected_interim_report_date: str | None,
) -> list[str]:
    """Independently enforce per-company accounting and report-period identity."""
    errors: list[str] = []
    date_contracts = {
        "revenue_history": "12-31",
        "income_history": "12-31",
        "cashflow": "12-31",
        "balance": "12-31",
        "income_interim": None,
        "cashflow_interim": None,
    }
    for dataset, annual_period in date_contracts.items():
        values = financial.get(dataset, [])
        rows = [values] if isinstance(values, Mapping) else values
        for row in rows if isinstance(rows, (list, tuple)) else []:
            if not isinstance(row, Mapping):
                continue
            report_date = str(row.get("REPORT_DATE") or "").strip()
            try:
                datetime.strptime(report_date, "%Y-%m-%d")
            except ValueError:
                if any(key != "REPORT_DATE" and _finite(value) is not None for key, value in row.items()):
                    errors.append(f"{code}: {dataset} contains a critical value with an invalid report date")
                continue
            if annual_period is not None and not report_date.endswith(annual_period):
                errors.append(f"{code}: {dataset} contains a non-annual report period {report_date}")
    balances = financial.get("balance", [])
    balance_rows = [balances] if isinstance(balances, Mapping) else balances
    for row in balance_rows if isinstance(balance_rows, (list, tuple)) else []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        assets = _finite(row.get("TOTAL_ASSETS"))
        liabilities = _finite(row.get("TOTAL_LIABILITIES"))
        total = _finite(row.get("TOTAL_EQUITY"))
        parent = next(
            (_finite(row.get(key)) for key in _AUDIT_PARENT_EQUITY_KEYS if _finite(row.get(key)) is not None),
            None,
        )
        minority = next(
            (_finite(row.get(key)) for key in _AUDIT_MINORITY_EQUITY_KEYS if _finite(row.get(key)) is not None),
            None,
        )
        label = f"{code}:{report_date or 'unknown balance period'}"
        if assets is not None and assets <= 0:
            errors.append(f"{label}: total assets are non-positive")
        if liabilities is not None and liabilities < 0:
            errors.append(f"{label}: total liabilities are negative")
        if total is not None and total > 0 and assets is not None and total > assets * 1.03:
            errors.append(f"{label}: total equity exceeds total assets")
        if parent is not None and assets is not None and parent > assets * 1.03 and (total is None or minority is None):
            errors.append(f"{label}: attributable equity exceeds total assets")
        if parent is not None and total is not None:
            if minority is None:
                if total <= 0 < parent or (total > 0 and parent > total * 1.03):
                    errors.append(f"{label}: attributable equity exceeds total equity")
            else:
                scale = max(abs(total), abs(parent) + abs(minority), 1.0)
                if abs(total - parent - minority) > scale * 0.03:
                    errors.append(f"{label}: total=parent+minority equity identity fails")
        if assets is not None and liabilities is not None and total is not None:
            scale = max(abs(assets), abs(liabilities) + abs(total), 1.0)
            if abs(assets - liabilities - total) > scale * 0.03:
                errors.append(f"{label}: assets=liabilities+equity identity fails")

    income_rows = _audit_interim_rows(financial.get("income_interim", []))
    cashflow_rows = _audit_interim_rows(financial.get("cashflow_interim", []))
    if not income_rows or not cashflow_rows:
        errors.append(f"{code}: current comparable income/cash-flow evidence is missing")
        return errors
    income_date = str(income_rows[-1].get("REPORT_DATE") or "")[:10]
    cashflow_date = str(cashflow_rows[-1].get("REPORT_DATE") or "")[:10]
    if income_date != cashflow_date:
        errors.append(f"{code}: current income and cash-flow report periods differ")
        return errors
    try:
        parsed_current = datetime.strptime(income_date, "%Y-%m-%d")
    except ValueError:
        errors.append(f"{code}: current report date is invalid")
        return errors
    prior_date = f"{parsed_current.year - 1}{income_date[4:]}"
    for dataset, rows in (("income", income_rows), ("cash-flow", cashflow_rows)):
        dates = {str(row.get("REPORT_DATE") or "")[:10] for row in rows}
        if prior_date not in dates:
            errors.append(f"{code}: {dataset} lacks exact prior-year same-period evidence")
    if expected_interim_report_date is not None:
        if income_date != expected_interim_report_date:
            errors.append(
                f"{code}: current report period {income_date} differs from expected {expected_interim_report_date}"
            )
    else:
        annual_dates: list[str] = []
        for dataset in ("income_history", "revenue_history", "cashflow", "balance"):
            values = financial.get(dataset, [])
            rows = [values] if isinstance(values, Mapping) else values
            for row in rows if isinstance(rows, (list, tuple)) else []:
                if isinstance(row, Mapping):
                    value = str(row.get("REPORT_DATE") or "")[:10]
                    if value.endswith("-12-31"):
                        annual_dates.append(value)
        if annual_dates and income_date <= max(annual_dates):
            errors.append(f"{code}: current interim report is not newer than the latest annual report")
    return errors


def _audit_growth_cap(evidence: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    comparable = all(
        evidence.get(key) == "same_period_yoy"
        for key in ("revenue_yoy_basis", "profit_yoy_basis", "operating_cash_flow_yoy_basis")
    ) and all(evidence.get(key) is not None for key in ("revenue", "profit", "operating_cash_flow"))
    if not comparable:
        return (
            {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0},
            "missing_current_period_conservative_cap",
        )
    severe = any(
        (value := _finite(evidence.get(key))) is not None and value <= 0
        for key in ("revenue", "profit", "operating_cash_flow")
    ) or any(
        (value := _finite(evidence.get(key))) is not None and value <= -0.50
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    material = any(
        (value := _finite(evidence.get(key))) is not None and value <= -0.20
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    weakening = any(
        (value := _finite(evidence.get(key))) is not None and value < 0
        for key in ("revenue_yoy", "profit_yoy", "operating_cash_flow_yoy")
    )
    if severe:
        return {"pessimistic": -0.20, "neutral": -0.10, "optimistic": 0.0}, "current_period_severe_deterioration"
    if material:
        return {"pessimistic": -0.10, "neutral": -0.05, "optimistic": 0.05}, "current_period_material_deterioration"
    if weakening:
        return {"pessimistic": -0.05, "neutral": 0.0, "optimistic": 0.10}, "current_period_weakening"
    return {key: 1.0 for key in _AUDIT_SCENARIOS}, "no_current_period_downward_cap"


_AUDIT_OPERATING_CASH_KEYS = (
    "NETCASH_OPERATE",
    "经营活动产生的现金流量净额",
)
_AUDIT_CAPEX_KEYS = (
    "CONSTRUCT_LONG_ASSET",
    "PAY_ACQ_CONST_FIASSETS",
    "购建固定资产无形资产和其他长期资产支付的现金",
)
_AUDIT_CAPEX_PROVENANCE_SCHEMA_VERSION = 1
_AUDIT_CAPEX_FIELD = "CONSTRUCT_LONG_ASSET"
_AUDIT_STANDARD_CASHFLOW_REPORT = "RPT_DMSK_FN_CASHFLOW"
_AUDIT_DETAILED_CASHFLOW_REPORT = "RPT_F10_FINANCE_GCASHFLOW"
_AUDIT_OFFICIAL_QUARTERLY_REPORT = "CNINFO_EXCHANGE_FILED_QUARTERLY_REPORT"
_AUDIT_OFFICIAL_ANNUAL_REPORT = "CNINFO_EXCHANGE_FILED_ANNUAL_REPORT"
_AUDIT_EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_AUDIT_NON_CAPEX_OUTFLOW_FIELDS = (
    "INVEST_PAY_CASH",
    "PLEDGE_LOAN_ADD",
    "OBTAIN_SUBSIDIARY_OTHER",
    "ADD_PLEDGE_TIMEDEPOSITS",
    "PAY_OTHER_INVEST",
    "INVEST_OUTFLOW_OTHER",
    "INVEST_OUTFLOW_BALANCE",
)
_AUDIT_DIRECT_DEBT_KEYS = (
    "INTEREST_BEARING_DEBT",
    "TOTAL_INTEREST_BEARING_DEBT",
    "有息负债",
)
_AUDIT_DEBT_COMPONENT_KEYS = (
    "SHORT_LOAN",
    "SHORT_BONDS_PAYABLE",
    "LONG_LOAN",
    "BOND_PAYABLE",
    "BONDS_PAYABLE",
    "NONCURRENT_LIAB_1YEAR",
    "CURRENT_PORTION_LONG_DEBT",
    "LEASE_LIAB",
    "LEASE_LIABILITIES",
    "短期借款",
    "长期借款",
    "应付债券",
    "一年内到期的非流动负债",
    "租赁负债",
)
_AUDIT_CASH_KEYS = (
    "MONETARYFUNDS",
    "CASH_AND_CASH_EQUIVALENTS",
    "CASH_EQUIVALENTS",
    "货币资金",
)


def _first_finite(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _finite(row.get(key))
        if value is not None:
            return value
    return None


def _audit_fcff_normalisation(
    records: Any,
) -> tuple[float | None, float | None, tuple[float, ...], str, list[float]]:
    rows = [records] if isinstance(records, Mapping) else records
    by_year: dict[int, tuple[bool, str, float]] = {}
    undated: list[float] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        operating = _first_finite(row, _AUDIT_OPERATING_CASH_KEYS)
        capex = _first_finite(row, _AUDIT_CAPEX_KEYS)
        if operating is None or capex is None:
            continue
        fcff = operating - abs(capex)
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        if len(report_date) >= 4 and report_date[:4].isdigit():
            year = int(report_date[:4])
            candidate = (report_date.endswith("12-31"), report_date, fcff)
            if year not in by_year or candidate[:2] > by_year[year][:2]:
                by_year[year] = candidate
        else:
            undated.append(fcff)
    annual = [by_year[year][2] for year in sorted(by_year)] if by_year else undated
    if not annual:
        return None, None, (), "missing_complete_annual_fcff_history", []
    recent = tuple(float(value) for value in annual[-3:])
    latest = recent[-1]
    median = statistics.median(recent)
    if latest <= 0:
        return latest, latest, recent, "latest_nonpositive_fail_closed", annual
    if len(recent) >= 3 and all(current <= previous for previous, current in zip(recent, recent[1:])):
        return latest, latest, recent, "latest_persistent_decline", annual
    premium_limit = latest * _AUDIT_NORMALISATION_PREMIUM_CAP
    normalised = min(median, premium_limit)
    basis = "recent_median" if median <= premium_limit else "latest_premium_cap"
    return float(normalised), latest, recent, basis, annual


def _audit_valid_reporting_period_contract(value: object) -> bool:
    """Validate the snapshot period contract without using production helpers."""
    if not isinstance(value, ReportingPeriodContract):
        return False
    raw_dates = (
        value.annual_report_date,
        value.current_interim_report_date,
        value.prior_interim_report_date,
    )
    if not all(isinstance(item, str) for item in raw_dates):
        return False
    try:
        annual, current, prior = (datetime.strptime(item, "%Y-%m-%d") for item in raw_dates)
    except ValueError:
        return False
    if tuple(item.strftime("%Y-%m-%d") for item in (annual, current, prior)) != raw_dates:
        return False
    return (
        (annual.month, annual.day) == (12, 31)
        and (current.month, current.day) in {(3, 31), (6, 30), (9, 30)}
        and (current.month, current.day) == (prior.month, prior.day)
        and prior.year == annual.year
        and current.year == annual.year + 1
    )


def _audit_ttm_shell(
    metric: str,
    contract: ReportingPeriodContract,
) -> dict[str, Any]:
    formula_version = _AUDIT_TTM_FCFF_FORMULA_VERSION if metric == "fcff" else _AUDIT_TTM_REVENUE_FORMULA_VERSION
    cash_flow_kind = "cfo_less_capex_proxy" if metric == "fcff" else "reported_revenue"
    period = {
        "basis": _AUDIT_TTM_PERIOD_BASIS,
        "annual_report_date": contract.annual_report_date,
        "current_interim_report_date": contract.current_interim_report_date,
        "prior_interim_report_date": contract.prior_interim_report_date,
    }
    return {
        "status": "invalid_period_contract",
        "value": None,
        "metric": metric,
        "formula_version": formula_version,
        "cash_flow_kind": cash_flow_kind,
        "period_basis": _AUDIT_TTM_PERIOD_BASIS,
        "period": period,
        "unit": _AUDIT_TTM_SOURCE_UNIT,
        "components": {
            "annual": {"report_date": contract.annual_report_date, "row_count": 0},
            "current_interim": {"report_date": contract.current_interim_report_date, "row_count": 0},
            "prior_interim": {"report_date": contract.prior_interim_report_date, "row_count": 0},
        },
    }


def _audit_canonical_report_date(row: Mapping[str, Any]) -> str | None:
    value = str(row.get("REPORT_DATE") or "").strip()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value if parsed.strftime("%Y-%m-%d") == value else None


def _audit_rows_at_report_date(records: Any, report_date: str) -> list[Mapping[str, Any]]:
    if not isinstance(records, (list, tuple)):
        return []
    return [row for row in records if isinstance(row, Mapping) and _audit_canonical_report_date(row) == report_date]


def _audit_strict_source_value(
    row: Mapping[str, Any],
    keys: tuple[str, ...],
) -> tuple[str, float | None, str | None]:
    present = [(key, row.get(key)) for key in keys if key in row and row.get(key) not in (None, "")]
    if not present:
        return "missing_component", None, None
    key, raw_value = present[0]
    value = _finite(raw_value)
    if value is None:
        return "nonfinite_component", None, key
    return "complete", value, key


def _audit_validate_capex_provenance(
    provenance: Any,
    *,
    expected_value: float,
    expected_report_date: str,
) -> str:
    """Independently replay the schema-1 capex evidence contract."""
    if not isinstance(provenance, Mapping):
        return "missing_capex_provenance"
    if (
        provenance.get("schema_version") != _AUDIT_CAPEX_PROVENANCE_SCHEMA_VERSION
        or provenance.get("status") != "complete"
        or provenance.get("report_date") != expected_report_date
        or not _close(provenance.get("value"), expected_value, abs_tol=0.01)
    ):
        return "invalid_capex_provenance"

    label = provenance.get("evidence_label")
    source_report = provenance.get("source_report")
    components = provenance.get("components")
    if label == "fact_official_report_zero":
        expected_source_report = (
            _AUDIT_OFFICIAL_ANNUAL_REPORT
            if expected_report_date.endswith("-12-31")
            else _AUDIT_OFFICIAL_QUARTERLY_REPORT
        )
        code = provenance.get("security_code")
        if (
            not isinstance(code, str)
            or re.fullmatch(r"\d{6}", code) is None
            or source_report != expected_source_report
            or provenance.get("source_field") != _AUDIT_CAPEX_FIELD
            or provenance.get("formula") != "exchange_filed_statement_zero"
            or provenance.get("derivation_method") is not None
            or not _close(expected_value, 0.0, abs_tol=0.01)
            or not isinstance(components, Mapping)
            or not _close(components.get("reported_value"), 0.0, abs_tol=0.01)
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
        return "complete"

    if provenance.get("source_url") != _AUDIT_EASTMONEY_DATACENTER_URL:
        return "invalid_capex_provenance"
    if label == "fact_source_reported":
        if (
            source_report not in {_AUDIT_STANDARD_CASHFLOW_REPORT, _AUDIT_DETAILED_CASHFLOW_REPORT}
            or provenance.get("source_field") != _AUDIT_CAPEX_FIELD
            or provenance.get("formula") != "source_reported"
            or provenance.get("derivation_method") is not None
            or not isinstance(components, Mapping)
            or not _close(components.get("reported_value"), expected_value, abs_tol=0.01)
        ):
            return "invalid_capex_provenance"
        return "complete"

    if (
        label != "derived_calculation"
        or source_report != _AUDIT_DETAILED_CASHFLOW_REPORT
        or not _close(expected_value, 0.0, abs_tol=0.01)
        or not isinstance(components, Mapping)
    ):
        return "invalid_capex_provenance"
    non_capex = components.get("non_capex_outflows")
    if not isinstance(non_capex, Mapping) or set(non_capex) != set(_AUDIT_NON_CAPEX_OUTFLOW_FIELDS):
        return "invalid_capex_provenance"
    outflow_values = [_finite(non_capex.get(field)) for field in _AUDIT_NON_CAPEX_OUTFLOW_FIELDS]
    if any(value is None or value < 0 for value in outflow_values):
        return "invalid_capex_provenance"
    outflow_sum = sum(value for value in outflow_values if value is not None)

    method = provenance.get("derivation_method")
    if method == "detailed_outflow_residual_zero":
        total = _finite(components.get("total_invest_outflow"))
        declared_sum = _finite(components.get("non_capex_outflow_sum"))
        if (
            total is None
            or total < 0
            or declared_sum is None
            or not _close(declared_sum, outflow_sum, abs_tol=0.01)
            or not _close(total, outflow_sum, abs_tol=0.01)
        ):
            return "invalid_capex_provenance"
        return "complete"
    if method != "detailed_net_cash_identity_zero":
        return "invalid_capex_provenance"
    inflow = _finite(components.get("total_invest_inflow"))
    other = _finite(components.get("invest_netcash_other"))
    balance = _finite(components.get("invest_netcash_balance"))
    net = _finite(components.get("netcash_invest"))
    solved = _finite(components.get("solved_total_invest_outflow"))
    if None in {inflow, other, balance, net, solved}:
        return "invalid_capex_provenance"
    if not _close(inflow + other + balance - net, solved, abs_tol=0.01) or not _close(
        solved,
        0.0,
        abs_tol=0.01,
    ):
        return "invalid_capex_provenance"
    return "complete"


def _audit_reconstruct_ttm(
    metric: str,
    annual_records: Any,
    interim_records: Any,
    contract: ReportingPeriodContract,
) -> dict[str, Any]:
    """Independently reconstruct the exact FY + YTD - prior-YTD payload."""
    result = _audit_ttm_shell(metric, contract)
    if not _audit_valid_reporting_period_contract(contract):
        return result
    targets = {
        "annual": (annual_records, contract.annual_report_date),
        "current_interim": (interim_records, contract.current_interim_report_date),
        "prior_interim": (interim_records, contract.prior_interim_report_date),
    }
    selected: dict[str, Mapping[str, Any]] = {}
    components = result["components"]
    for label, (records, report_date) in targets.items():
        matches = _audit_rows_at_report_date(records, report_date)
        components[label]["row_count"] = len(matches)
        if len(matches) > 1:
            result["status"] = "duplicate_period"
            return result
        if not matches:
            result["status"] = "missing_component"
            return result
        selected[label] = matches[0]

    if metric == "revenue":
        values: dict[str, float] = {}
        for label, row in selected.items():
            status, value, source_field = _audit_strict_source_value(
                row,
                ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
            )
            components[label].update({"revenue": value, "revenue_source_field": source_field})
            if status != "complete":
                result["status"] = status
                return result
            if value is None:
                result["status"] = "nonfinite_component"
                return result
            values[label] = value
        reconstructed = values["annual"] + values["current_interim"] - values["prior_interim"]
        if not math.isfinite(reconstructed):
            result["status"] = "nonfinite_component"
            return result
        components["reconstructed_revenue"] = reconstructed
        result.update({"status": "complete", "value": reconstructed})
        return result

    cfo_values: dict[str, float] = {}
    capex_values: dict[str, float] = {}
    for label, row in selected.items():
        cfo_status, cfo, cfo_field = _audit_strict_source_value(row, _AUDIT_OPERATING_CASH_KEYS)
        capex_status, raw_capex, capex_field = _audit_strict_source_value(row, _AUDIT_CAPEX_KEYS)
        components[label].update(
            {
                "operating_cash_flow": cfo,
                "operating_cash_flow_source_field": cfo_field,
                "capex_raw": raw_capex,
                "capex_absolute": abs(raw_capex) if raw_capex is not None else None,
                "capex_source_field": capex_field,
                "capex_provenance": row.get("CAPEX_PROVENANCE"),
            }
        )
        if cfo_status != "complete":
            result["status"] = cfo_status
            return result
        if capex_status != "complete":
            result["status"] = capex_status
            return result
        if cfo is None or raw_capex is None:
            result["status"] = "nonfinite_component"
            return result
        provenance_status = _audit_validate_capex_provenance(
            row.get("CAPEX_PROVENANCE"),
            expected_value=raw_capex,
            expected_report_date=str(row.get("REPORT_DATE") or ""),
        )
        components[label]["capex_provenance_status"] = provenance_status
        if provenance_status != "complete":
            result["status"] = provenance_status
            return result
        cfo_values[label] = cfo
        capex_values[label] = abs(raw_capex)
    reconstructed_cfo = cfo_values["annual"] + cfo_values["current_interim"] - cfo_values["prior_interim"]
    reconstructed_capex = capex_values["annual"] + capex_values["current_interim"] - capex_values["prior_interim"]
    components["reconstructed_operating_cash_flow"] = reconstructed_cfo
    components["reconstructed_capex"] = reconstructed_capex
    if not math.isfinite(reconstructed_cfo) or not math.isfinite(reconstructed_capex):
        result["status"] = "nonfinite_component"
        return result
    if reconstructed_capex < 0:
        result["status"] = "negative_reconstructed_capex"
        return result
    reconstructed_fcff = reconstructed_cfo - reconstructed_capex
    if not math.isfinite(reconstructed_fcff):
        result["status"] = "nonfinite_component"
        return result
    components["reconstructed_fcff"] = reconstructed_fcff
    result.update({"status": "complete", "value": reconstructed_fcff})
    return result


def _audit_ttm_fcff_normalisation(
    annual_cashflow: Any,
    ttm_fcff_evidence: Mapping[str, Any],
    contract: ReportingPeriodContract,
) -> tuple[float, float, tuple[float, float, float], str, list[dict[str, str]]] | None:
    """Independently normalise the exact FY-1, FY and strict-TTM sequence."""
    prior_annual_date = f"{int(contract.annual_report_date[:4]) - 1}-12-31"
    prior_rows = _audit_rows_at_report_date(annual_cashflow, prior_annual_date)
    if len(prior_rows) != 1:
        return None
    prior_cfo_status, prior_cfo, _ = _audit_strict_source_value(
        prior_rows[0],
        _AUDIT_OPERATING_CASH_KEYS,
    )
    prior_capex_status, prior_capex, _ = _audit_strict_source_value(
        prior_rows[0],
        _AUDIT_CAPEX_KEYS,
    )
    components = ttm_fcff_evidence.get("components")
    annual_component = components.get("annual") if isinstance(components, Mapping) else None
    if (
        prior_cfo_status != "complete"
        or prior_capex_status != "complete"
        or prior_cfo is None
        or prior_capex is None
        or not isinstance(annual_component, Mapping)
    ):
        return None
    annual_cfo = _finite(annual_component.get("operating_cash_flow"))
    annual_capex = _finite(annual_component.get("capex_absolute"))
    reconstructed_cfo = _finite(components.get("reconstructed_operating_cash_flow"))
    reconstructed_capex = _finite(components.get("reconstructed_capex"))
    if None in (annual_cfo, annual_capex, reconstructed_cfo, reconstructed_capex):
        return None
    recent = (
        prior_cfo - abs(prior_capex),
        annual_cfo - abs(annual_capex),
        reconstructed_cfo - reconstructed_capex,
    )
    latest = recent[-1]
    median = float(statistics.median(recent))
    if latest <= 0:
        normalised, basis = latest, "latest_nonpositive_fail_closed"
    elif all(current <= previous for previous, current in zip(recent, recent[1:])):
        normalised, basis = latest, "latest_persistent_decline"
    else:
        premium_limit = latest * _AUDIT_NORMALISATION_PREMIUM_CAP
        normalised = min(median, premium_limit)
        basis = "recent_median" if median <= premium_limit else "latest_premium_cap"
    periods = [
        {"kind": "annual", "report_date": prior_annual_date},
        {"kind": "annual", "report_date": contract.annual_report_date},
        {"kind": "ttm", "through_report_date": contract.current_interim_report_date},
    ]
    return float(normalised), latest, recent, basis, periods


def _audit_consecutive_suffix(years: list[int], maximum: int | None = None) -> list[int]:
    if not years:
        return []
    suffix = [years[-1]]
    for year in reversed(years[:-1]):
        if year != suffix[0] - 1:
            break
        suffix.insert(0, year)
    return suffix[-maximum:] if maximum is not None else suffix


def _audit_quality_evidence(financial: Mapping[str, Any], fcf_margin: float) -> bool:
    if not math.isfinite(fcf_margin) or fcf_margin < 0.12:
        return False
    current = _audit_current_period_evidence(financial)
    if (
        _finite(current.get("profit")) is None
        or float(current["profit"]) <= 0
        or _finite(current.get("operating_cash_flow")) is None
        or float(current["operating_cash_flow"]) <= 0
        or current.get("profit_yoy_basis") != "same_period_yoy"
        or float(current["profit_yoy"]) <= -0.20
        or current.get("operating_cash_flow_yoy_basis") != "same_period_yoy"
        or float(current["operating_cash_flow_yoy"]) <= -0.30
        or current.get("revenue_yoy_basis") != "same_period_yoy"
        or float(current["revenue_yoy"]) <= -0.15
    ):
        return False
    profits = _annual_values(financial.get("income_history", []), ("PARENT_NETPROFIT",))
    revenues = _annual_values(
        financial.get("income_history", []),
        ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
    )
    common = _audit_consecutive_suffix(sorted(set(profits) & set(revenues)), maximum=5)
    if len(common) < 4:
        return False
    margins = [profits[year] / revenues[year] for year in common if revenues[year] > 0]
    if len(margins) < 4 or sum(value > 0 for value in margins) / len(margins) < 0.8:
        return False
    median_margin = statistics.median(margins)
    mean_margin = statistics.fmean(margins)
    if (
        median_margin < 0.15
        or margins[-1] < median_margin * 0.70
        or mean_margin <= 0
        or statistics.pstdev(margins) / mean_margin > 0.30
    ):
        return False
    equity = _annual_equity(financial.get("balance", []))
    roe_years = _audit_consecutive_suffix(
        [year for year in common if year in equity and year - 1 in equity],
        maximum=5,
    )
    if len(roe_years) < 3:
        return False
    roes = [profits[year] / ((equity[year - 1] + equity[year]) / 2.0) for year in roe_years]
    if roes[-1] < 0.20 or statistics.median(roes) < 0.18:
        return False
    rows = (
        [financial.get("cashflow", [])]
        if isinstance(financial.get("cashflow"), Mapping)
        else financial.get("cashflow", [])
    )
    fcff_by_year: dict[int, tuple[bool, str, float]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        operating = _first_finite(row, _AUDIT_OPERATING_CASH_KEYS)
        capex = _first_finite(row, _AUDIT_CAPEX_KEYS)
        if len(report_date) < 4 or not report_date[:4].isdigit() or operating is None or capex is None:
            continue
        year = int(report_date[:4])
        candidate = (report_date.endswith("12-31"), report_date, operating - abs(capex))
        if year not in fcff_by_year or candidate[:2] > fcff_by_year[year][:2]:
            fcff_by_year[year] = candidate
    fcff_years = _audit_consecutive_suffix(sorted(fcff_by_year), maximum=5)
    fcffs = [fcff_by_year[year][2] for year in fcff_years]
    if len(fcffs) < 3 or sum(value > 0 for value in fcffs) / len(fcffs) < (2.0 / 3.0):
        return False
    median_fcff = statistics.median(fcffs)
    return median_fcff > 0 and fcffs[-1] >= median_fcff * 0.40


def _audit_net_debt(records: Any) -> float:
    rows = [records] if isinstance(records, Mapping) else records
    usable = [row for row in (rows or []) if isinstance(row, Mapping)]
    if not usable:
        return 0.0
    latest = max(usable, key=lambda row: str(row.get("REPORT_DATE") or ""))
    debt_known = False
    direct = None
    for key in _AUDIT_DIRECT_DEBT_KEYS:
        if key in latest:
            value = _finite(latest.get(key))
            if value is not None and value >= 0:
                debt_known = True
                direct = value
                break
    debt = direct if direct is not None else 0.0
    if direct is None:
        for key in _AUDIT_DEBT_COMPONENT_KEYS:
            if key not in latest:
                continue
            value = _finite(latest.get(key))
            if value is not None and value >= 0:
                debt_known = True
                debt += value
    cash = 0.0
    for key in _AUDIT_CASH_KEYS:
        if key in latest:
            value = _finite(latest.get(key))
            if value is not None and value >= 0:
                cash = value
                break
    return debt - cash if debt_known else 0.0


def _valuation_contract_errors(
    code: str,
    row: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    quote: Mapping[str, Any] | None,
    financial: Mapping[str, Any] | None,
    reporting_period_contract: ReportingPeriodContract,
) -> list[str]:
    errors: list[str] = []
    if _normalise_code(result.get("code")) != code:
        errors.append(f"{code}: valuation code differs from score code")
    price = _finite(result.get("current_price"))
    row_price = _finite(row.get("price"))
    if price is None or price <= 0:
        errors.append(f"{code}: valuation current_price invalid")
        return errors
    if row_price is None or not _close(row_price, price):
        errors.append(f"{code}: valuation/score price mismatch")
    if quote is not None:
        quote_price = _finite(quote.get("price"))
        if quote_price is None or not _close(price, quote_price):
            errors.append(f"{code}: valuation/quote price mismatch")
        quote_name = str(quote.get("name") or "").strip()
        if quote_name and str(result.get("name") or "").strip() != quote_name:
            errors.append(f"{code}: valuation/quote name mismatch")
        quote_cap = _finite(quote.get("market_cap"))
        row_cap = _finite(row.get("market_cap"))
        if quote_cap is None or quote_cap <= 0:
            errors.append(f"{code}: quote market cap invalid")
        elif row_cap is None or not _close(row_cap, quote_cap):
            errors.append(f"{code}: score/quote market cap mismatch")
    industry = str(result.get("industry_code") or "")
    if not industry or industry != str(row.get("industry") or ""):
        errors.append(f"{code}: valuation/score industry mismatch")

    points = result.get("dcf_points")
    params = result.get("params")
    if not isinstance(points, Mapping) or set(points) != set(_AUDIT_SCENARIOS):
        errors.append(f"{code}: valuation scenarios missing or extra")
        return errors
    if not isinstance(params, Mapping) or set(params) != set(_AUDIT_SCENARIOS):
        errors.append(f"{code}: valuation parameters missing or extra")
        return errors

    parsed_points: dict[str, tuple[float, float]] = {}
    for scenario in _AUDIT_SCENARIOS:
        band = points.get(scenario)
        if not isinstance(band, Mapping):
            errors.append(f"{code}: {scenario} valuation band missing")
            continue
        lower, upper = _finite(band.get("lower")), _finite(band.get("upper"))
        if lower is None or upper is None or lower <= 0 or upper <= 0 or lower > upper:
            errors.append(f"{code}: {scenario} valuation band invalid")
            continue
        parsed_points[scenario] = (lower, upper)
    if len(parsed_points) != 3:
        return errors

    is_financial = result.get("_pb_valuation") is True
    if is_financial != (industry in _AUDIT_FINANCIAL_INDUSTRIES):
        errors.append(f"{code}: valuation model is inconsistent with industry")
    expected_tax_source = (
        "financial_operating_liabilities_excluded" if is_financial else "taxable_profit_evidence_unavailable"
    )
    if _finite(result.get("tax_shield_rate")) != 0.0 or result.get("tax_shield_source") != expected_tax_source:
        errors.append(f"{code}: debt tax shield is not supported by auditable tax evidence")
    shares = _finite(result.get("shares_outstanding"))
    if shares is None or shares <= 0:
        errors.append(f"{code}: valuation shares_outstanding invalid")
    elif quote is not None:
        quote_cap = _finite(quote.get("market_cap"))
        quote_price = _finite(quote.get("price"))
        if quote_cap is None or quote_price is None or quote_cap <= 0 or quote_price <= 0:
            errors.append(f"{code}: quote cannot reproduce shares_outstanding")
        elif not _close(shares, quote_cap / quote_price):
            errors.append(f"{code}: shares_outstanding does not match current quote")

    components = result.get("wacc_components")
    risk_parameters = result.get("risk_parameters")
    if not isinstance(risk_parameters, Mapping):
        errors.append(f"{code}: CAPM risk parameter evidence missing")
        capm_risk_free = _AUDIT_RISK_FREE_RATE
        capm_erp = _AUDIT_EQUITY_RISK_PREMIUM
    else:
        capm_risk_free = _finite(risk_parameters.get("risk_free_rate"))
        capm_erp = _finite(risk_parameters.get("equity_risk_premium"))
        if (
            capm_risk_free is None
            or capm_erp is None
            or not _close(capm_risk_free, _AUDIT_RISK_FREE_RATE, rel_tol=0.0)
            or not _close(capm_erp, _AUDIT_EQUITY_RISK_PREMIUM, rel_tol=0.0)
            or risk_parameters.get("risk_free_rate_as_of") != "2026-07-15"
            or risk_parameters.get("equity_risk_premium_as_of") != "2026-04-01"
            or risk_parameters.get("model_as_of") != "2026-07-15"
            or not str(risk_parameters.get("risk_free_rate_source_url") or "").startswith("https://")
            or not str(risk_parameters.get("equity_risk_premium_source_url") or "").startswith("https://")
        ):
            errors.append(f"{code}: CAPM risk parameter evidence invalid")
            capm_risk_free = _AUDIT_RISK_FREE_RATE
            capm_erp = _AUDIT_EQUITY_RISK_PREMIUM
    component_wacc: float | None = None
    if not isinstance(components, Mapping):
        errors.append(f"{code}: WACC components missing")
    else:
        equity_weight = _finite(components.get("equity_weight"))
        debt_weight = _finite(components.get("debt_weight"))
        cost_of_equity = _finite(components.get("cost_of_equity"))
        tax_shield = _finite(components.get("tax_shield_rate"))
        cost_of_debt = _finite(components.get("pre_tax_cost_of_debt"))
        if (
            equity_weight is None
            or debt_weight is None
            or cost_of_equity is None
            or tax_shield is None
            or not 0 <= equity_weight <= 1
            or not 0 <= debt_weight <= 1
            or not _close(equity_weight + debt_weight, 1.0)
            or cost_of_equity <= 0
            or tax_shield != 0.0
            or (debt_weight > 0 and (cost_of_debt is None or cost_of_debt < 0))
        ):
            errors.append(f"{code}: WACC components invalid")
        else:
            debt_cost = 0.0 if debt_weight == 0 else float(cost_of_debt)
            component_wacc = equity_weight * cost_of_equity + debt_weight * debt_cost * (1.0 - tax_shield)
            levered_beta = _finite(result.get("levered_beta"))
            if levered_beta is None or not _close(
                cost_of_equity,
                capm_risk_free + levered_beta * capm_erp,
            ):
                errors.append(f"{code}: cost of equity does not match CAPM")
            beta_u = _finite(result.get("industry_unlevered_beta"))
            if (
                not is_financial
                and beta_u is not None
                and str(result.get("beta_source") or "") == "industry_unlevered_relevered"
                and equity_weight > 0
            ):
                expected_beta = beta_u * (1.0 + (1.0 - tax_shield) * debt_weight / equity_weight)
                if levered_beta is None or not _close(levered_beta, expected_beta):
                    errors.append(f"{code}: levered beta does not match capital structure")
            beta_evidence = result.get("beta_evidence")
            if not isinstance(beta_evidence, Mapping):
                errors.append(f"{code}: beta evidence missing")
            elif str(result.get("beta_source") or "") == "industry_and_weekly_market_beta_blend":
                industry_beta = _finite(beta_evidence.get("industry_beta"))
                company_weight = _finite(beta_evidence.get("company_weight"))
                industry_weight = _finite(beta_evidence.get("industry_weight"))
                market_beta = _finite(beta_evidence.get("blume_beta"))
                r_squared = _finite(beta_evidence.get("r_squared"))
                sample_size = _finite(beta_evidence.get("sample_size"))
                if (
                    industry_beta is None
                    or company_weight is None
                    or industry_weight is None
                    or market_beta is None
                    or r_squared is None
                    or sample_size is None
                    or sample_size < 156
                    or not 0.05 <= r_squared <= 1.0
                    or not _close(company_weight, min(0.50, r_squared))
                    or not _close(company_weight + industry_weight, 1.0)
                    or levered_beta is None
                    or not _close(
                        levered_beta,
                        industry_weight * industry_beta + company_weight * market_beta,
                    )
                ):
                    errors.append(f"{code}: blended beta evidence cannot reproduce final beta")
            if not _close(result.get("base_wacc"), round(component_wacc, 4), rel_tol=0.0, abs_tol=5e-5):
                errors.append(f"{code}: base WACC does not match components")

    if is_financial:
        centers = tuple((parsed_points[key][0] + parsed_points[key][1]) / 2.0 for key in _AUDIT_SCENARIOS)
        if not centers[0] <= centers[1] <= centers[2]:
            errors.append(f"{code}: financial scenario centers are unordered")
        normalised_roe = _finite(result.get("normalised_roe"))
        evidence_years_raw = result.get("roe_evidence_years")
        evidence_years = list(evidence_years_raw) if isinstance(evidence_years_raw, (list, tuple)) else []
        if (
            len(evidence_years) < 3
            or len(evidence_years) > 5
            or any(not isinstance(year, int) or isinstance(year, bool) for year in evidence_years)
            or evidence_years != sorted(set(evidence_years))
            or any(current != previous + 1 for previous, current in zip(evidence_years, evidence_years[1:]))
        ):
            errors.append(f"{code}: financial ROE evidence years invalid")
        independently_computed_roes: list[float] = []
        latest_equity: float | None = None
        if financial is not None:
            annual_profit = _annual_values(financial.get("income_history", []), ("PARENT_NETPROFIT",))
            annual_equity = _annual_equity(financial.get("balance", []))
            latest_equity = _latest_equity(financial.get("balance", []))
            for year in evidence_years:
                if year not in annual_profit or year not in annual_equity or year - 1 not in annual_equity:
                    errors.append(f"{code}: financial ROE evidence is not attributable/reproducible")
                    independently_computed_roes = []
                    break
                independently_computed_roes.append(
                    annual_profit[year] / ((annual_equity[year - 1] + annual_equity[year]) / 2.0)
                )
            if independently_computed_roes:
                expected_roe = statistics.median(independently_computed_roes)
                if normalised_roe is None or not _close(normalised_roe, expected_roe):
                    errors.append(f"{code}: normalised ROE does not match attributable evidence")
                if evidence_years[-1] != max(annual_profit) or evidence_years[-1] != max(annual_equity):
                    errors.append(f"{code}: financial ROE evidence is not current")
                if annual_profit[evidence_years[-1]] <= 0 or independently_computed_roes[-1] <= 0:
                    errors.append(f"{code}: financial latest annual attributable result is non-positive")
            expected_current = _audit_current_period_evidence(financial)
            if _canonical_json(result.get("current_period_evidence")) != _canonical_json(expected_current):
                errors.append(f"{code}: financial current-period evidence mismatch")
            current_profit = _finite(expected_current.get("profit"))
            current_profit_yoy = _finite(expected_current.get("profit_yoy"))
            current_profit_basis = expected_current.get("profit_yoy_basis")
            if current_profit is None or current_profit <= 0:
                errors.append(f"{code}: financial current-period attributable profit is not P/B-eligible")
            elif current_profit_basis == "same_period_yoy":
                if current_profit_yoy is None or current_profit_yoy <= -0.30:
                    errors.append(f"{code}: financial current-period attributable profit is not P/B-eligible")
            elif current_profit_basis != "same_period_turnaround":
                errors.append(f"{code}: financial current-period attributable profit is not P/B-eligible")
        for scenario in _AUDIT_SCENARIOS:
            scenario_params = params[scenario]
            if not isinstance(scenario_params, Mapping):
                errors.append(f"{code}: {scenario} P/B parameters missing")
                continue
            growth = _finite(scenario_params.get("growth"))
            scenario_roe = _finite(scenario_params.get("scenario_roe"))
            cost = _finite(scenario_params.get("cost_of_equity"))
            bvps = _finite(scenario_params.get("bvps"))
            pb_lower = _finite(scenario_params.get("pb_lower"))
            pb_upper = _finite(scenario_params.get("pb_upper"))
            if None in (growth, scenario_roe, cost, bvps, pb_lower, pb_upper):
                errors.append(f"{code}: {scenario} P/B inputs invalid")
                continue
            if scenario_roe <= growth or cost - _AUDIT_BAND_WACC_DELTA <= growth or bvps <= 0:
                errors.append(f"{code}: {scenario} P/B denominator/input invalid")
                continue
            expected_pb_lower = (scenario_roe - growth) / (cost + _AUDIT_BAND_WACC_DELTA - growth)
            expected_pb_upper = (scenario_roe - growth) / (cost - _AUDIT_BAND_WACC_DELTA - growth)
            lower, upper = parsed_points[scenario]
            if not _close(pb_lower, expected_pb_lower) or not _close(pb_upper, expected_pb_upper):
                errors.append(f"{code}: {scenario} justified P/B multiple mismatch")
            if not _close(lower, bvps * expected_pb_lower) or not _close(upper, bvps * expected_pb_upper):
                errors.append(f"{code}: {scenario} justified P/B endpoint mismatch")
            if component_wacc is not None and (
                not _close(cost, component_wacc) or not _close(scenario_params.get("wacc_base"), component_wacc)
            ):
                errors.append(f"{code}: {scenario} financial cost of equity mismatch")
            if normalised_roe is None or not _close(scenario_params.get("normalised_roe"), normalised_roe):
                errors.append(f"{code}: {scenario} normalised ROE mismatch")
            if list(scenario_params.get("roe_years") or []) != evidence_years:
                errors.append(f"{code}: {scenario} ROE evidence years mismatch")
            if str(scenario_params.get("formula") or "") != "(normalised_roe - g) / (cost_of_equity - g)":
                errors.append(f"{code}: {scenario} P/B formula provenance mismatch")
            if shares is not None and latest_equity is not None and not _close(bvps, latest_equity / shares):
                errors.append(f"{code}: {scenario} BVPS does not match attributable equity")
        if independently_computed_roes and normalised_roe is not None:
            expected_scenario_roes = {
                "pessimistic": min(_quantile(independently_computed_roes, 0.25), normalised_roe * 0.8),
                "neutral": normalised_roe,
                "optimistic": max(
                    normalised_roe,
                    min(_quantile(independently_computed_roes, 0.75), normalised_roe * 1.1),
                ),
            }
            for scenario, expected_roe in expected_scenario_roes.items():
                if isinstance(params[scenario], Mapping) and not _close(
                    params[scenario].get("scenario_roe"), expected_roe
                ):
                    errors.append(f"{code}: {scenario} ROE scenario mismatch")
    else:
        current_growth_caps = {scenario: 1.0 for scenario in _AUDIT_SCENARIOS}
        current_growth_basis = "no_current_period_downward_cap"
        pessimistic, neutral, optimistic = (parsed_points[key] for key in _AUDIT_SCENARIOS)
        if not (pessimistic[1] <= neutral[0] <= neutral[1] <= optimistic[0]):
            errors.append(f"{code}: DCF scenario bands overlap or are unordered")
        base_fcf = _finite(result.get("base_fcf"))
        base_revenue = _finite(result.get("base_revenue"))
        net_debt = _finite(result.get("net_debt"))
        if None in (base_fcf, base_revenue, shares, net_debt):
            errors.append(f"{code}: DCF base inputs invalid")
        else:
            if financial is not None:
                if result.get("valuation_input_basis") != "strict_ttm":
                    errors.append(f"{code}: industrial valuation input basis is not strict TTM")
                if result.get("base_revenue_basis") != "strict_ttm_reported_revenue":
                    errors.append(f"{code}: industrial base revenue basis is not strict TTM")
                if result.get("base_fcf_basis") != "normalised_two_annual_plus_ttm_cfo_less_capex_proxy":
                    errors.append(f"{code}: industrial base FCFF basis is not strict TTM")

                expected_ttm_fcff = _audit_reconstruct_ttm(
                    "fcff",
                    financial.get("cashflow", []),
                    financial.get("cashflow_interim", []),
                    reporting_period_contract,
                )
                expected_ttm_revenue = _audit_reconstruct_ttm(
                    "revenue",
                    financial.get("revenue_history", []),
                    financial.get("income_interim", []),
                    reporting_period_contract,
                )
                if result.get("ttm_fcff_evidence") != expected_ttm_fcff:
                    errors.append(f"{code}: strict TTM FCFF provenance differs from source reconstruction")
                if result.get("ttm_revenue_evidence") != expected_ttm_revenue:
                    errors.append(f"{code}: strict TTM revenue provenance differs from source reconstruction")

                expected_revenue = (
                    _finite(expected_ttm_revenue.get("value"))
                    if expected_ttm_revenue.get("status") == "complete"
                    else None
                )
                if expected_revenue is None or expected_revenue <= 0:
                    errors.append(f"{code}: strict TTM revenue reconstruction is not positive and complete")
                elif not _close(base_revenue, expected_revenue):
                    errors.append(f"{code}: DCF base revenue does not match strict TTM company evidence")

                normalisation = (
                    _audit_ttm_fcff_normalisation(
                        financial.get("cashflow", []),
                        expected_ttm_fcff,
                        reporting_period_contract,
                    )
                    if expected_ttm_fcff.get("status") == "complete"
                    else None
                )
                normalised: float | None = None
                annual_fcffs = _audit_fcff_normalisation(financial.get("cashflow", []))[4]
                expected_adjustments: list[dict[str, Any]] = []
                if normalisation is None:
                    errors.append(f"{code}: strict FY-1/FY/TTM FCFF normalisation evidence is incomplete")
                else:
                    normalised, latest_fcff, recent_fcff, basis, periods = normalisation
                    if not _close(result.get("latest_fcff"), latest_fcff):
                        errors.append(f"{code}: latest FCFF does not match strict TTM reconstruction")
                    published_recent = result.get("recent_fcff")
                    if (
                        not isinstance(published_recent, (list, tuple))
                        or len(published_recent) != len(recent_fcff)
                        or any(not _close(actual, expected) for actual, expected in zip(published_recent, recent_fcff))
                    ):
                        errors.append(f"{code}: recent FCFF does not match FY-1/FY/TTM evidence")
                    if result.get("recent_fcff_periods") != periods:
                        errors.append(f"{code}: recent FCFF periods do not match strict TTM contract")
                    if str(result.get("fcf_normalisation_basis") or "") != basis:
                        errors.append(f"{code}: FCFF normalisation basis mismatch")
                    if result.get("fcf_normalisation_period_basis") != "two_annual_plus_strict_ttm":
                        errors.append(f"{code}: FCFF normalisation period basis is not strict TTM")
                    expected_period_detail = {
                        "period_set": "two_annual_plus_strict_ttm",
                        "periods": periods,
                        "normalisation_method": basis,
                        "cash_flow_kind": expected_ttm_fcff.get("cash_flow_kind"),
                        "formula_version": expected_ttm_fcff.get("formula_version"),
                    }
                    if result.get("fcf_normalisation_period") != expected_period_detail:
                        errors.append(f"{code}: FCFF normalisation period provenance mismatch")
                if not _close(
                    result.get("normalisation_premium_cap"),
                    _AUDIT_NORMALISATION_PREMIUM_CAP,
                    rel_tol=0.0,
                ):
                    errors.append(f"{code}: FCFF normalisation premium cap mismatch")

                expected_current = _audit_current_period_evidence(financial)
                current_growth_caps, current_growth_basis = _audit_growth_cap(expected_current)
                expected_published_current = {**expected_current, "growth_cap_basis": current_growth_basis}
                if _canonical_json(result.get("current_period_evidence")) != _canonical_json(
                    expected_published_current
                ):
                    errors.append(f"{code}: DCF current-period evidence mismatch")

                expected_fcf_pre_cap = normalised
                profits = _annual_values(
                    financial.get("income_history", []),
                    ("PARENT_NETPROFIT",),
                )
                ordered_profits = [profits[year] for year in sorted(profits)]
                if (
                    expected_fcf_pre_cap is not None
                    and len(ordered_profits) >= 3
                    and min(ordered_profits) < 0 < max(ordered_profits)
                ):
                    recent_profit = ordered_profits[-3:]
                    recovering = recent_profit[0] < recent_profit[1] < recent_profit[2] and recent_profit[2] > 0
                    if not recovering and len(annual_fcffs) >= 3:
                        p25 = _quantile(annual_fcffs, 0.25)
                        adjusted = min(expected_fcf_pre_cap, p25)
                        if adjusted != expected_fcf_pre_cap:
                            expected_adjustments.append(
                                {
                                    "kind": "mixed_profit_cycle_p25_cap",
                                    "before": expected_fcf_pre_cap,
                                    "limit": p25,
                                    "after": adjusted,
                                }
                            )
                        expected_fcf_pre_cap = adjusted
                published_quality = _strict_bool(result.get("quality_evidence"))
                expected_quality = (
                    _audit_quality_evidence(financial, expected_fcf_pre_cap / expected_revenue)
                    if expected_fcf_pre_cap is not None and expected_revenue is not None and expected_revenue > 0
                    else False
                )
                if published_quality is None or published_quality != expected_quality:
                    errors.append(f"{code}: quality flag does not match independent current evidence")
                industry_target = _finite(result.get("industry_fcf_margin_target"))
                margin_ceiling = _finite(result.get("fcf_margin_ceiling"))
                if industry_target is None or industry_target < 0 or margin_ceiling is None:
                    errors.append(f"{code}: FCFF margin-cap evidence invalid")
                else:
                    expected_ceiling = (
                        0.65
                        if expected_quality
                        else min(
                            _AUDIT_NONQUALITY_MARGIN_CAP,
                            max(
                                _AUDIT_NONQUALITY_MARGIN_FLOOR,
                                industry_target * _AUDIT_NONQUALITY_MARGIN_MULTIPLIER,
                            ),
                        )
                    )
                    if not _close(margin_ceiling, expected_ceiling):
                        errors.append(f"{code}: FCFF margin ceiling mismatch")
                    if expected_fcf_pre_cap is not None and expected_revenue is not None:
                        expected_fcf = expected_fcf_pre_cap
                        margin_limit = expected_revenue * expected_ceiling
                        if expected_fcf > margin_limit:
                            expected_adjustments.append(
                                {
                                    "kind": "fcf_margin_ceiling",
                                    "before": expected_fcf,
                                    "limit": margin_limit,
                                    "after": margin_limit,
                                }
                            )
                        expected_fcf = min(expected_fcf, margin_limit)
                        if not _close(base_fcf, expected_fcf):
                            errors.append(f"{code}: DCF base FCFF does not match current company evidence")
                if result.get("base_fcf_adjustments") != expected_adjustments:
                    errors.append(f"{code}: base FCFF adjustments differ from strict source normalisation")

                expected_net_debt = _audit_net_debt(financial.get("balance", []))
                if not _close(net_debt, expected_net_debt):
                    errors.append(f"{code}: DCF net debt does not match current company evidence")
            for scenario in _AUDIT_SCENARIOS:
                scenario_params = params[scenario]
                if not isinstance(scenario_params, Mapping):
                    errors.append(f"{code}: {scenario} DCF parameters missing")
                    continue
                growth = _finite(scenario_params.get("growth"))
                wacc_center = _finite(scenario_params.get("wacc_base"))
                terminal_growth = _finite(scenario_params.get("terminal_g"))
                retention = _finite(scenario_params.get("margin_retention"))
                if None in (growth, wacc_center, terminal_growth, retention):
                    errors.append(f"{code}: {scenario} DCF parameters invalid")
                    continue
                if growth > current_growth_caps[scenario] + 1e-12:
                    errors.append(f"{code}: {scenario} growth ignores current-period cap")
                if scenario_params.get("current_period_growth_cap_basis") != current_growth_basis:
                    errors.append(f"{code}: {scenario} current-period growth basis mismatch")
                if component_wacc is not None and not _close(wacc_center, component_wacc + _AUDIT_WACC_SHIFT[scenario]):
                    errors.append(f"{code}: {scenario} WACC shift mismatch")
                expected_lower = _audit_dcf_value(
                    base_fcf=base_fcf,
                    base_revenue=base_revenue,
                    growth=growth,
                    wacc=wacc_center + _AUDIT_BAND_WACC_DELTA,
                    terminal_growth=terminal_growth,
                    shares=shares,
                    net_debt=net_debt,
                    retention=retention,
                )
                expected_upper = _audit_dcf_value(
                    base_fcf=base_fcf,
                    base_revenue=base_revenue,
                    growth=growth,
                    wacc=wacc_center - _AUDIT_BAND_WACC_DELTA,
                    terminal_growth=terminal_growth,
                    shares=shares,
                    net_debt=net_debt,
                    retention=retention,
                )
                lower, upper = parsed_points[scenario]
                if expected_lower is None or not _close(lower, expected_lower):
                    errors.append(f"{code}: {scenario} DCF lower endpoint mismatch")
                if expected_upper is None or not _close(upper, expected_upper):
                    errors.append(f"{code}: {scenario} DCF upper endpoint mismatch")

    pessimistic_upper = parsed_points["pessimistic"][1]
    neutral_lower, neutral_upper = parsed_points["neutral"]
    optimistic_lower, optimistic_upper = parsed_points["optimistic"]
    buy_boundary = (pessimistic_upper + neutral_lower) / 2.0
    sell_boundary = (neutral_upper + optimistic_lower) / 2.0
    valuation_center = (neutral_lower + neutral_upper) / 2.0
    if buy_boundary > sell_boundary:
        errors.append(f"{code}: valuation boundaries are reversed")
    expected_zone = "买入区" if price <= buy_boundary else "卖出区" if price >= sell_boundary else "观察区"
    for field in ("mean1", "buy_zone_upper"):
        if not _close(result.get(field), buy_boundary):
            errors.append(f"{code}: {field} does not match scenario bands")
    if not _close(result.get("dcf_value_mean"), buy_boundary):
        errors.append(f"{code}: legacy dcf_value_mean does not match buy boundary")
    if result.get("dcf_value_mean_legacy_alias_of") != "buy_zone_upper":
        errors.append(f"{code}: legacy dcf_value_mean alias is not disclosed")
    for field in ("valuation_center", "neutral_value_midpoint"):
        if not _close(result.get(field), valuation_center):
            errors.append(f"{code}: {field} does not match neutral scenario midpoint")
    for field in ("mean2", "sell_zone_lower"):
        if not _close(result.get(field), sell_boundary):
            errors.append(f"{code}: {field} does not match scenario bands")
    if result.get("zone") != expected_zone:
        errors.append(f"{code}: valuation zone does not match price and boundaries")
    expected_margin = round((pessimistic_upper - price) / pessimistic_upper * 100.0, 2)
    if not _close(result.get("safety_margin_pct"), expected_margin, rel_tol=0.0, abs_tol=0.005):
        errors.append(f"{code}: safety margin mismatch")
    expected_safety = ""
    if expected_zone == "买入区":
        if price <= pessimistic_upper * _AUDIT_DEEP_SAFETY_RATIO:
            expected_safety = "★★★ 深度安全边际"
        elif price <= pessimistic_upper:
            expected_safety = "★★ 中度安全边际"
    if result.get("safety_score") != expected_safety:
        errors.append(f"{code}: safety score mismatch")
    expected_bubble = price >= optimistic_upper * _AUDIT_BUBBLE_RATIO
    actual_bubble = _strict_bool(result.get("bubble_warning"))
    if actual_bubble is None or actual_bubble != expected_bubble:
        errors.append(f"{code}: bubble warning mismatch")
    return errors


def _independent_checks(
    scores: pd.DataFrame,
    sampled: tuple[str, ...],
    dcf_results: Mapping[str, Mapping[str, Any]],
    skip_reasons: Mapping[str, str],
    *,
    skip_classifications: Mapping[str, Mapping[str, str]] | None = None,
    quotes: pd.DataFrame | None = None,
    financials: Mapping[str, Mapping[str, Any]] | None = None,
    expected_interim_report_date: str | None = None,
    reporting_period_contract: ReportingPeriodContract | None = None,
) -> tuple[str, ...]:
    """Recompute score, trigger and valuation contracts without engine helpers."""
    if not _audit_valid_reporting_period_contract(reporting_period_contract):
        return ("independent: valid reporting_period_contract is required",)
    errors: list[str] = []
    if _production_screener.TYPE_WEIGHTS != _AUDIT_WEIGHTS:
        errors.append("independent: production type weights differ from fixed audit contract")
    if tuple(_production_screener.TYPE_PRIORITY) != _AUDIT_PRIORITY:
        errors.append("independent: production type priority differs from fixed audit contract")
    if _production_screener.TYPE_NAMES != _AUDIT_NAMES:
        errors.append("independent: production type names differ from fixed audit contract")
    if _finite(_production_screener.QUALIFY_THRESHOLD) != _AUDIT_QUALIFY_THRESHOLD:
        errors.append("independent: production qualification threshold differs from fixed audit contract")
    if _finite(_production_screener.EVIDENCE_MAX_LENGTH) != _AUDIT_REASON_MAX_LENGTH:
        errors.append("independent: production reason length differs from fixed audit contract")
    fixed_config = {
        "BAND_WACC_DELTA": _AUDIT_BAND_WACC_DELTA,
        "BUBBLE_RATIO": _AUDIT_BUBBLE_RATIO,
        "DEEP_SAFETY_RATIO": _AUDIT_DEEP_SAFETY_RATIO,
        "EQUITY_RISK_PREMIUM": _AUDIT_EQUITY_RISK_PREMIUM,
        "FCF_MARGIN_FLOOR": _AUDIT_FCF_MARGIN_FLOOR,
        "FCF_MARGIN_LONG_TERM": _AUDIT_FCF_MARGIN_LONG_TERM,
        "FORECAST_YEARS": _AUDIT_FORECAST_YEARS,
        "RISK_FREE_RATE": _AUDIT_RISK_FREE_RATE,
    }
    for name, expected in fixed_config.items():
        if not _close(getattr(_production_config, name, None), expected, rel_tol=0.0):
            errors.append(f"independent: production {name} differs from fixed audit contract")
    fixed_risk_metadata = {
        "MODEL_RISK_DATA_AS_OF": "2026-07-15",
        "RISK_FREE_RATE_AS_OF": "2026-07-15",
        "RISK_FREE_RATE_TENOR": "10Y",
        "RISK_FREE_RATE_SOURCE_URL": ("https://yield.chinabond.com.cn/cbweb-sh-mn/sh/searchShTable?locale=zh_CN"),
        "EQUITY_RISK_PREMIUM_AS_OF": "2026-04-01",
        "EQUITY_RISK_PREMIUM_BASIS": "china_rating_based_total_erp",
        "EQUITY_RISK_PREMIUM_SOURCE_URL": ("https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctrypremApr26.xlsx"),
        "EQUITY_RISK_PREMIUM_SOURCE_SHA256": ("2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e"),
        "INDUSTRY_RISK_DATA_AS_OF": "2026-01-05",
        "INDUSTRY_BETA_SOURCE_SHA256": ("ff9187e1ca2dc5ee697e240d368f5c8f1956bc00c4ff8e8b0b0d46c698f2aee9"),
        "INDUSTRY_WACC_SOURCE_SHA256": ("525ff4a15a2585fd2d1c06fc758296654370837da95e7107f64a14b0f03667a6"),
    }
    for name, expected in fixed_risk_metadata.items():
        if getattr(_production_config, name, None) != expected:
            errors.append(f"independent: production {name} differs from fixed audit contract")
    if "code" not in scores:
        return ("independent: missing code column",)
    result_codes = scores["code"].map(_normalise_code).tolist()
    if len(result_codes) != len(set(result_codes)):
        errors.append("independent: duplicate result codes")
    if set(result_codes) != set(sampled):
        errors.append("independent: sampled/result code sets differ")

    quote_index: dict[str, Mapping[str, Any]] = {}
    if quotes is not None:
        if not isinstance(quotes, pd.DataFrame) or "code" not in quotes:
            errors.append("independent: quotes missing code column")
        else:
            for record in quotes.to_dict(orient="records"):
                code = _normalise_code(record.get("code"))
                if code in quote_index:
                    errors.append(f"independent: duplicate quote code {code}")
                quote_index[code] = record
    financial_index: dict[str, Mapping[str, Any]] = {}
    if financials is not None:
        if not isinstance(financials, Mapping):
            errors.append("independent: financials is not a mapping")
        else:
            for raw_code, record in financials.items():
                code = _normalise_code(raw_code)
                if code in financial_index:
                    errors.append(f"independent: duplicate financial code {code}")
                if isinstance(record, Mapping):
                    financial_index[code] = record

    for code in sampled:
        financial = financial_index.get(code)
        if financial is not None:
            errors.extend(_audit_financial_source_errors(code, financial, expected_interim_report_date))

    for _row_index, row in scores.iterrows():
        code = _normalise_code(row.get("code"))
        triggered_types: list[str] = []
        totals: dict[str, float] = {}
        diagnostic_totals: dict[str, float] = {}
        valid_payloads: dict[str, Mapping[str, Any]] = {}
        for type_key, weights in _AUDIT_WEIGHTS.items():
            payload = row.get(type_key)
            if not isinstance(payload, Mapping):
                errors.append(f"{code}:{type_key}: payload missing")
                continue
            sub_scores = payload.get("sub_scores")
            reasons = payload.get("reasons")
            if not isinstance(sub_scores, Mapping) or set(sub_scores) != set(weights):
                errors.append(f"{code}:{type_key}: sub-score keys differ")
                continue
            if not isinstance(reasons, Mapping) or set(weights) - set(reasons):
                errors.append(f"{code}:{type_key}: reasons missing")
                continue
            if any(not str(key).startswith("_") for key in set(reasons) - set(weights)):
                errors.append(f"{code}:{type_key}: unknown reason keys")
            clean: dict[str, float] = {}
            for key in weights:
                value = _finite(sub_scores[key])
                if value is None:
                    errors.append(f"{code}:{type_key}:{key}: non-numeric score")
                    break
                if not 0 <= value <= 10:
                    errors.append(f"{code}:{type_key}:{key}: score outside 0..10")
                    break
                clean[key] = value
            if len(clean) != len(weights):
                continue
            for key in weights:
                reason = reasons.get(key)
                if reason is None or reason is pd.NA or not str(reason).strip():
                    errors.append(f"{code}:{type_key}:{key}: reason missing")
                elif len(str(reason)) > _AUDIT_REASON_MAX_LENGTH:
                    errors.append(f"{code}:{type_key}:{key}: reason too long")
            raw_total = round(sum(clean[key] * weight for key, weight in weights.items()), 1)
            expected_total = min(raw_total, 4.9) if type_key == "type3" and clean["3e"] <= 3 else raw_total
            actual_total = _finite(payload.get("total"))
            if actual_total is None:
                errors.append(f"{code}:{type_key}: total is non-numeric")
                continue
            if not 0 <= actual_total <= 10 or not _close(actual_total, expected_total, rel_tol=0.0):
                errors.append(f"{code}:{type_key}: total differs from fixed weighted score")
            if type_key == "type3":
                downgrade = bool(reasons.get("_downgrade"))
                if clean["3e"] <= 3 and raw_total > 4.9 and not downgrade:
                    errors.append(f"{code}:{type_key}: active bubble cap lacks downgrade reason")
                if downgrade and clean["3e"] > 3:
                    errors.append(f"{code}:{type_key}: downgrade reason exists without bubble cap")
            veto = _strict_bool(payload.get("veto"))
            triggered = _strict_bool(payload.get("triggered"))
            if veto is None:
                errors.append(f"{code}:{type_key}: veto is not boolean")
                veto = bool(payload.get("veto"))
            if triggered is None:
                errors.append(f"{code}:{type_key}: triggered is not boolean")
                triggered = bool(payload.get("triggered"))
            reason_veto = bool(reasons.get("_veto"))
            if veto != reason_veto:
                errors.append(f"{code}:{type_key}: veto differs from _veto reason")
            status = payload.get("status")
            applicable = _strict_bool(payload.get("applicable"))
            evidence_complete = _strict_bool(payload.get("evidence_complete"))
            if status not in _AUDIT_TYPE_STATUSES or status != reasons.get("_status"):
                errors.append(f"{code}:{type_key}: invalid or inconsistent status")
            if type_key == "type7":
                errors.extend(_audit_type7_ledger(code, payload.get("ledger"), status))
                ledger = payload.get("ledger")
                if status != "not_applicable" and isinstance(ledger, Mapping):
                    source_scores = ledger.get("scores")
                    if isinstance(source_scores, Mapping):
                        source_values = {
                            key: _finite(source_scores.get(key)) for key in ("template1", "template5", "patch5")
                        }
                        expected_type7_scores = (
                            {
                                "7a": round(source_values["template1"] / 10.0, 3),
                                "7b": round(source_values["template5"] / 10.0, 3),
                                "7c": round(source_values["patch5"] / 10.0, 3),
                            }
                            if all(value is not None for value in source_values.values())
                            else None
                        )
                        if clean != expected_type7_scores:
                            errors.append(f"{code}:type7: diagnostics differ from source ledgers")
                    if status != "blocked" and triggered is not ledger.get("triggered"):
                        errors.append(f"{code}:type7: trigger differs from source ledgers")
            if applicable is None or applicable != (status != "not_applicable"):
                errors.append(f"{code}:{type_key}: applicable differs from status")
            expected_evidence_complete = reasons.get("_evidence") == "complete"
            if evidence_complete is None or evidence_complete != expected_evidence_complete:
                errors.append(f"{code}:{type_key}: evidence_complete differs from status")
            if status in _AUDIT_NON_DIAGNOSTIC_STATUSES and (reason_veto or triggered):
                errors.append(f"{code}:{type_key}: N/A or missing evidence leaks veto/trigger")
            if triggered != (status == "triggered"):
                errors.append(f"{code}:{type_key}: triggered differs from explicit status")
            if status == "triggered" and (
                actual_total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or bool(reasons.get("_condition"))
            ):
                errors.append(f"{code}:{type_key}: triggered status violates threshold/veto/condition")
            if status == "conditional" and (
                actual_total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or not bool(reasons.get("_condition"))
            ):
                errors.append(f"{code}:{type_key}: conditional status lacks its required condition")
            if status == "vetoed" and not reason_veto:
                errors.append(f"{code}:{type_key}: vetoed status lacks veto reason")
            if status == "observe" and not (5.0 <= actual_total < _AUDIT_QUALIFY_THRESHOLD) and not reason_veto:
                errors.append(f"{code}:{type_key}: observe status is outside its score band")
            if status == "not_triggered" and actual_total >= 5.0 and not reason_veto:
                errors.append(f"{code}:{type_key}: not_triggered status is inside observe/trigger band")
            totals[type_key] = actual_total
            if status not in _AUDIT_NON_DIAGNOSTIC_STATUSES:
                diagnostic_totals[type_key] = actual_total
            valid_payloads[type_key] = payload
            if triggered:
                triggered_types.append(type_key)
            type_score = _finite(row.get(f"{type_key}_score"))
            if type_score is None or not _close(type_score, actual_total, rel_tol=0.0):
                errors.append(f"{code}: {type_key}_score differs from payload total")

        declared = row.get("buy_types", [])
        expected_types = [key for key in _AUDIT_PRIORITY if key in triggered_types]
        if not isinstance(declared, list) or declared != expected_types:
            errors.append(f"{code}: buy_types order/content differs from triggered payloads")
        num_types = _finite(row.get("num_types"))
        if num_types is None or num_types != len(expected_types):
            errors.append(f"{code}: num_types mismatch")
        primary = _optional_text(row.get("primary_type"))
        expected_primary = expected_types[0] if expected_types else None
        if primary != expected_primary:
            errors.append(f"{code}: primary_type does not match fixed priority")
        expected_primary_label = _AUDIT_NAMES[expected_primary] if expected_primary else "无触发（不买）"
        if row.get("primary_label") != expected_primary_label:
            errors.append(f"{code}: primary_label mismatch")
        expected_diagnostic = None
        if diagnostic_totals:
            maximum = max(diagnostic_totals.values())
            expected_diagnostic = next(key for key in _AUDIT_PRIORITY if diagnostic_totals.get(key) == maximum)
            published_maximum = _finite(row.get("max_score"))
            if published_maximum is None:
                errors.append(f"{code}: max_score is non-numeric")
            elif not _close(published_maximum, maximum, rel_tol=0.0):
                errors.append(f"{code}: max_score is not the diagnostic maximum")
            if row.get("diagnostic_type") != expected_diagnostic:
                errors.append(f"{code}: diagnostic_type is not the highest-scoring framework")
            if row.get("diagnostic_label") != _AUDIT_NAMES[expected_diagnostic]:
                errors.append(f"{code}: diagnostic_label mismatch")
        else:
            if _finite(row.get("max_score")) is not None:
                errors.append(f"{code}: max_score must be empty without a diagnostic framework")
            if _optional_text(row.get("diagnostic_type")) is not None:
                errors.append(f"{code}: diagnostic_type must be empty without applicable evidence")
            if row.get("diagnostic_label") != "无可完整诊断框架":
                errors.append(f"{code}: diagnostic_label must identify the absence of an applicable framework")

        cases = row.get("bear_case")
        if expected_diagnostic is None:
            if cases != []:
                errors.append(f"{code}: bear_case must be empty without a diagnostic framework")
        elif not isinstance(cases, list) or len(cases) != 3:
            errors.append(f"{code}: bear_case must contain three entries")
        elif expected_diagnostic in valid_payloads:
            expected_cases = _audit_bear_case(expected_diagnostic, valid_payloads[expected_diagnostic])
            if cases != expected_cases:
                errors.append(f"{code}: bear_case differs from independent deterministic ranking")
            for case in cases:
                if not isinstance(case, Mapping) or set(case) != {"dimension", "score", "reason"}:
                    errors.append(f"{code}: bear_case entry structure invalid")
                    break
                score = _finite(case.get("score"))
                reason = case.get("reason")
                if score is None or not 0 <= score <= 10:
                    errors.append(f"{code}: bear_case score invalid")
                if reason is None or not str(reason).strip() or len(str(reason)) > _AUDIT_REASON_MAX_LENGTH:
                    errors.append(f"{code}: bear_case reason invalid")

    normalized_results: dict[str, Mapping[str, Any]] = {}
    for raw_code, result in dcf_results.items():
        code = _normalise_code(raw_code)
        if code in normalized_results:
            errors.append(f"independent: duplicate valuation code {code}")
        if isinstance(result, Mapping):
            normalized_results[code] = result
    normalized_skips: dict[str, str] = {}
    for raw_code, reason in skip_reasons.items():
        code = _normalise_code(raw_code)
        if code in normalized_skips:
            errors.append(f"independent: duplicate skip code {code}")
        normalized_skips[code] = str(reason or "").strip()
    normalized_skip_classifications: dict[str, dict[str, str]] = {}
    for raw_code, value in (skip_classifications or {}).items():
        code = _normalise_code(raw_code)
        classification = normalize_dcf_skip_classification(value)
        if code in normalized_skip_classifications:
            errors.append(f"independent: duplicate skip classification code {code}")
        if classification is None:
            errors.append(f"{code}: invalid structured valuation skip classification")
            continue
        normalized_skip_classifications[code] = classification
    sampled_set = set(sampled)
    if set(normalized_results) - sampled_set:
        errors.append("independent: valuation results contain non-sampled codes")
    if set(normalized_skips) - sampled_set:
        errors.append("independent: skip reasons contain non-sampled codes")
    if set(normalized_skip_classifications) != set(normalized_skips):
        errors.append("independent: skip classification identities differ from skip reasons")
    for code in set(normalized_skip_classifications) & set(normalized_skips):
        if normalized_skip_classifications[code]["reason"] != normalized_skips[code]:
            errors.append(f"{code}: structured skip reason differs from legacy skip reason")
    for code in sampled:
        result = normalized_results.get(code)
        if result is None:
            if not normalized_skips.get(code):
                errors.append(f"{code}: skipped valuation has no structured reason")
            continue
        if normalized_skips.get(code):
            errors.append(f"{code}: valuation result and skip reason both present")
        matches = scores.loc[scores["code"].map(_normalise_code) == code]
        if len(matches) != 1:
            continue
        if quotes is not None and code not in quote_index:
            errors.append(f"{code}: sampled company is absent from quote evidence")
        if financials is not None and code not in financial_index:
            errors.append(f"{code}: sampled company is absent from financial evidence")
        errors.extend(
            _valuation_contract_errors(
                code,
                matches.iloc[0],
                result,
                quote=quote_index.get(code),
                financial=financial_index.get(code),
                reporting_period_contract=reporting_period_contract,
            )
        )
    return tuple(errors)


def _scoring_replay_checks(
    published: pd.DataFrame,
    replayed: pd.DataFrame,
    sampled: tuple[str, ...],
) -> tuple[str, ...]:
    """Compare every published field with a reordered full-universe replay.

    This is deliberately labelled same-source: it proves deterministic raw
    input-to-subscore reproduction and catches payload corruption, while the
    independent checker and rule mutation tests retain their separate roles.
    """
    errors: list[str] = []
    if "code" not in published or "code" not in replayed:
        return ("scoring replay: missing code column",)
    published_index = {_normalise_code(row.get("code")): row for row in published.to_dict(orient="records")}
    replay_index = {_normalise_code(row.get("code")): row for row in replayed.to_dict(orient="records")}
    if set(published_index) != set(sampled):
        errors.append("scoring replay: published sampled identities differ")
    missing = sorted(set(sampled) - set(replay_index))
    if missing:
        errors.append(f"scoring replay: missing sampled companies {missing[:5]}")
    for code in sampled:
        if code not in published_index or code not in replay_index:
            continue
        if _canonical_json(published_index[code]) != _canonical_json(replay_index[code]):
            errors.append(f"{code}: every-field scoring replay differs from published result")
    return tuple(errors)


def _valuation_replay_checks(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    sampled: tuple[str, ...],
    published_results: Mapping[str, Mapping[str, Any]],
    published_skips: Mapping[str, str],
    published_issues: tuple[PipelineIssue, ...],
    *,
    published_skip_classifications: Mapping[str, Mapping[str, str]] | None = None,
    max_workers: int,
    reporting_period_contract: ReportingPeriodContract,
) -> tuple[str, ...]:
    """Same-source replay of valuation existence, payload, skip reason and issues."""
    replay = compute_dcf_batch(
        quotes,
        financials,
        eligible_codes=sampled,
        max_workers=max_workers,
        reporting_period_contract=reporting_period_contract,
    )
    errors: list[str] = []
    replay_results = {_normalise_code(code): result for code, result in replay.results.items()}
    replay_skips = {_normalise_code(code): str(reason) for code, reason in replay.skip_reasons.items()}
    replay_skip_classifications = {
        _normalise_code(code): classification for code, classification in replay.skip_classifications.items()
    }
    published_results = {_normalise_code(code): result for code, result in published_results.items()}
    published_skips = {_normalise_code(code): str(reason) for code, reason in published_skips.items()}
    published_skip_classifications = {
        _normalise_code(code): classification for code, classification in (published_skip_classifications or {}).items()
    }
    if set(replay_results) != set(published_results):
        errors.append("valuation replay: valid-result identities differ from published sample")
    if set(replay_skips) != set(published_skips):
        errors.append("valuation replay: skipped identities differ from published sample")
    if _canonical_json(replay_skip_classifications) != _canonical_json(published_skip_classifications):
        errors.append("valuation replay: structured skip classifications differ from published sample")
    for code in sampled:
        if code in replay_results and code in published_results:
            if _canonical_json(replay_results[code]) != _canonical_json(published_results[code]):
                errors.append(f"{code}: valuation replay payload differs from published result")
        if code in replay_skips and code in published_skips and replay_skips[code] != published_skips[code]:
            errors.append(f"{code}: valuation replay skip reason differs from published reason")
    replay_issues = sorted(
        (issue.code, issue.stage, issue.message)
        for issue in replay.issues
        if _normalise_code(issue.code) in set(sampled)
    )
    expected_issues = sorted((issue.code, issue.stage, issue.message) for issue in published_issues)
    if replay_issues != expected_issues:
        errors.append("valuation replay: pipeline issues differ from published sample")
    if replay.attempted != len(sampled) or len(replay_results) + replay.skipped != replay.attempted:
        errors.append("valuation replay: attempted/valid/skipped accounting mismatch")
    return tuple(errors)


def audit_random_sample(
    quotes: pd.DataFrame,
    financials: Mapping[str, Mapping[str, Any]],
    *,
    eligible_codes: Iterable[Any],
    seed: int = 20260715,
    sample_size: int = 100,
    max_workers: int = 8,
    snapshot_sha256: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    full_market_analysis: MarketAnalysisOutcome | None = None,
    reporting_period_contract: ReportingPeriodContract | None = None,
    market_coldness_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    quality_history_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> RandomSampleAudit:
    """Audit a fixed seed while preserving the production full-market benchmarks."""
    if not _audit_valid_reporting_period_contract(reporting_period_contract):
        raise ValueError("a valid reporting_period_contract is required for a production audit")
    if market_coldness_evidence is not None and not isinstance(market_coldness_evidence, Mapping):
        raise ValueError("market_coldness_evidence must be a mapping or None")
    if quality_history_evidence is not None and not isinstance(quality_history_evidence, Mapping):
        raise ValueError("quality_history_evidence must be a mapping or None")
    if not isinstance(quotes, pd.DataFrame) or not {"code", "market"} <= set(quotes.columns):
        raise ValueError("quotes must contain code and market columns")
    if not isinstance(financials, Mapping):
        raise ValueError("financials must be a mapping")

    quote_codes = quotes["code"].map(_normalise_code)
    duplicated = set(quote_codes[quote_codes.duplicated(keep=False)])
    if duplicated:
        raise ValueError(f"ambiguous quote codes: {sorted(duplicated)[:5]}")
    quote_index = {code: index for index, code in quote_codes.items()}

    financial_index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for raw_code, company in financials.items():
        code = _normalise_code(raw_code)
        if code in financial_index:
            raise ValueError(f"ambiguous financial code: {code}")
        if isinstance(company, Mapping):
            financial_index[code] = (str(raw_code), company)

    requested_eligible = {_normalise_code(code) for code in eligible_codes if _normalise_code(code)}
    if not requested_eligible:
        raise ValueError("eligible_codes must contain at least one validated company")
    available = set(quote_index) & set(financial_index)
    unknown = sorted(requested_eligible - available)
    if unknown:
        raise ValueError(f"eligible_codes are absent from quote/financial inputs: {unknown[:5]}")
    eligible = sorted(
        code
        for code in requested_eligible
        if str(quotes.at[quote_index[code], "market"]).strip().upper() in {"SH", "SZ"}
    )
    if not eligible:
        raise ValueError("eligible_codes must contain at least one SH/SZ company")
    count = int(sample_size)
    if count <= 0 or count > len(eligible):
        raise ValueError(f"sample_size must be between 1 and {len(eligible)}")
    # Deterministic reproducibility is the contract; this is not a secret or
    # adversarial lottery and therefore does not require cryptographic entropy.
    sampled = tuple(sorted(random.Random(int(seed)).sample(eligible, count)))  # nosec B311
    eligible_quotes = quotes.loc[[quote_index[code] for code in eligible]].copy()
    eligible_financials = {code: financial_index[code][1] for code in eligible}

    if full_market_analysis is None:
        analysis: MarketAnalysisOutcome = run_market_analysis(
            eligible_quotes,
            eligible_financials,
            eligible_codes=eligible,
            max_workers=max_workers,
            enforce_quality=True,
            expected_companies=len(eligible),
            reporting_period_contract=reporting_period_contract,
            market_coldness_evidence=market_coldness_evidence,
            quality_history_evidence=quality_history_evidence,
        )
    elif isinstance(full_market_analysis, MarketAnalysisOutcome):
        analysis = full_market_analysis
    else:
        raise TypeError("full_market_analysis must be a MarketAnalysisOutcome")

    if not isinstance(analysis.scores, pd.DataFrame) or "code" not in analysis.scores:
        raise ValueError("full-market analysis scores must contain a code column")
    full_score_codes = analysis.scores["code"].map(_normalise_code)
    if full_score_codes.duplicated().any() or set(full_score_codes) != set(eligible):
        raise ValueError("full-market analysis score identities differ from eligible_codes")
    try:
        revalidated_quality = validate_market_analysis_quality(
            analysis,
            expected_companies=len(eligible),
            expected_codes=eligible,
        )
    except (AnalysisQualityError, TypeError, ValueError) as exc:
        raise ValueError(f"full-market analysis failed quality revalidation: {exc}") from exc
    if _canonical_json(analysis.quality) != _canonical_json(revalidated_quality):
        raise ValueError("full-market analysis quality metadata differs from recomputed metrics")
    sample_mask = full_score_codes.isin(sampled)
    sampled_scores = analysis.scores.loc[sample_mask].copy().sort_values("code", kind="stable")
    normalized_results = {_normalise_code(code): result for code, result in analysis.dcf_results.items()}
    sampled_results = {code: normalized_results[code] for code in sampled if code in normalized_results}
    full_skip_reasons = _analysis_skip_reasons(analysis)
    sampled_skip_reasons = {code: full_skip_reasons[code] for code in sampled if code in full_skip_reasons}
    full_skip_classifications = _analysis_skip_classifications(analysis)
    captured_quality_history = (
        quality_history_evidence
        if quality_history_evidence is not None
        else getattr(analysis, "quality_history_evidence", {})
    )
    if not isinstance(captured_quality_history, Mapping):
        raise ValueError("full-market Type 7 quality history evidence is invalid")
    sampled_skip_classifications = {
        code: full_skip_classifications[code] for code in sampled if code in full_skip_classifications
    }
    sampled_issues = tuple(issue for issue in analysis.issues if _normalise_code(issue.code) in set(sampled))

    valuation_replay_errors = _valuation_replay_checks(
        quotes.loc[[quote_index[code] for code in sampled]].copy(),
        {code: financial_index[code][1] for code in sampled},
        sampled,
        sampled_results,
        sampled_skip_reasons,
        sampled_issues,
        published_skip_classifications=sampled_skip_classifications,
        max_workers=max_workers,
        reporting_period_contract=reporting_period_contract,
    )

    # The engine's validator is retained and labelled as same-source.  A
    # separate implementation below recomputes the public contracts and DCF
    # ordering, so the report never presents self-validation as independent.
    engine_errors = tuple(validate_screening_result(sampled_scores))
    replayed_scores = _production_screener.screen_all_types(
        dict(reversed(list(eligible_financials.items()))),
        eligible_quotes.iloc[::-1].reset_index(drop=True),
        dcf_results=analysis.dcf_results,
        market_coldness_evidence=market_coldness_evidence,
        dcf_skip_classifications=full_skip_classifications,
        quality_history_evidence=captured_quality_history,
        output_codes=sampled,
    )
    replayed_sample = replayed_scores.loc[replayed_scores["code"].map(_normalise_code).isin(sampled)].copy()
    scoring_replay_errors = _scoring_replay_checks(sampled_scores, replayed_sample, sampled)
    expected_interim_report_date = None
    if isinstance(provenance, Mapping):
        validation_metadata = provenance.get("validation")
        if isinstance(validation_metadata, Mapping):
            value = validation_metadata.get("expected_interim_report_date")
            if isinstance(value, str) and value:
                expected_interim_report_date = value
    independent_errors = _independent_checks(
        sampled_scores,
        sampled,
        sampled_results,
        sampled_skip_reasons,
        skip_classifications=sampled_skip_classifications,
        quotes=quotes,
        financials=financials,
        expected_interim_report_date=expected_interim_report_date,
        reporting_period_contract=reporting_period_contract,
    )
    analysis_quality = getattr(analysis, "quality", {})
    if not isinstance(analysis_quality, Mapping):
        analysis_quality = {}
    audit_provenance = _build_provenance(
        quotes,
        financials,
        tuple(eligible),
        snapshot_sha256=snapshot_sha256,
        metadata=provenance,
    )
    audit_provenance["reporting_period_contract"] = {
        "annual_report_date": reporting_period_contract.annual_report_date,
        "current_interim_report_date": reporting_period_contract.current_interim_report_date,
        "prior_interim_report_date": reporting_period_contract.prior_interim_report_date,
        "period_basis": _AUDIT_TTM_PERIOD_BASIS,
    }
    audit_provenance["market_coldness_evidence"] = _market_coldness_evidence_provenance(
        market_coldness_evidence,
        tuple(eligible),
    )
    audit_provenance["quality_history_evidence"] = _quality_history_evidence_provenance(
        captured_quality_history,
        tuple(eligible),
    )

    return RandomSampleAudit(
        int(seed),
        count,
        sampled,
        sampled_scores,
        sampled_results,
        sampled_skip_reasons,
        sampled_skip_classifications,
        sampled_issues,
        engine_errors,
        scoring_replay_errors,
        valuation_replay_errors,
        independent_errors,
        dict(analysis_quality),
        audit_provenance,
        len(eligible),
    )


def _markdown_cell(value: Any) -> str:
    safe = _json_safe(value)
    return str(safe if safe is not None else "").replace("|", "\\|").replace("\n", " ")


def render_audit_markdown(audit: RandomSampleAudit, *, data_timestamp: float) -> str:
    """Render a complete, human-readable summary with all sampled companies."""
    timestamp = datetime.fromtimestamp(float(data_timestamp), tz=timezone.utc).isoformat()
    signal_counts = {
        type_key: int(audit.scores[type_key].apply(lambda value: bool(value.get("triggered"))).sum())
        for type_key in _AUDIT_WEIGHTS
    }
    provenance = audit.provenance
    lines = [
        f"# 固定随机 {audit.sample_size} 家公司审计",
        "",
        f"- seed: `{audit.seed}`",
        f"- sample_size: `{audit.sample_size}`",
        f"- eligible_universe_size: `{audit.eligible_universe_size}`",
        f"- data_timestamp_utc: `{timestamp}`",
        f"- dcf_valid: `{len(audit.dcf_results)}`",
        f"- dcf_skipped_with_reason: `{len(audit.dcf_skip_reasons)}`",
        f"- pipeline_issues: `{len(audit.pipeline_issues)}`",
        f"- engine_self_check_errors: `{len(audit.engine_invariant_errors)}`",
        f"- same_source_scoring_replay_errors: `{len(audit.scoring_replay_errors)}`",
        f"- same_source_valuation_replay_errors: `{len(audit.valuation_replay_errors)}`",
        f"- independent_check_errors: `{len(audit.independent_errors)}`",
        f"- triggered_by_type: `{signal_counts}`",
        f"- snapshot_content_sha256: `{provenance.get('snapshot_content_sha256')}`",
        f"- snapshot_artifact_sha256: `{provenance.get('snapshot_artifact_sha256')}`",
        f"- code_sha256: `{provenance.get('code_sha256')}`",
        f"- rules_sha256: `{provenance.get('rules_sha256')}`",
        f"- dependency_manifest_sha256: `{provenance.get('dependency_manifest_sha256')}`",
        f"- industry_sha256: `{provenance.get('industry_sha256')}`",
        f"- patch6_source: `{provenance.get('patch6_source')}`",
        f"- type7_source_documents: `{provenance.get('type7_source_documents')}`",
        f"- risk_parameter_sources: `{provenance.get('risk_parameter_sources')}`",
        f"- scoring_verification_scope: `{provenance.get('scoring_verification_scope')}`",
        f"- git: `{provenance.get('git')}`",
        "",
    ]
    if audit.pipeline_issues:
        lines.extend(["## 管道问题", ""])
        lines.extend(
            f"- `{issue.code}` / `{issue.stage}`: {_markdown_cell(issue.message)}" for issue in audit.pipeline_issues
        )
        lines.append("")
    if audit.engine_invariant_errors:
        lines.extend(["## 引擎同源自检错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.engine_invariant_errors)
        lines.append("")
    if audit.scoring_replay_errors:
        lines.extend(["## 全字段评分同源重放错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.scoring_replay_errors)
        lines.append("")
    if audit.valuation_replay_errors:
        lines.extend(["## 估值同源重放错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.valuation_replay_errors)
        lines.append("")
    if audit.independent_errors:
        lines.extend(["## 独立重算错误", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in audit.independent_errors)
        lines.append("")

    lines.extend(
        [
            "## 公司明细",
            "",
            "| 代码 | 名称 | 行业 | 买入判定 | 诊断框架 | 诊断最高分 | 触发 | DCF | 三条空头漏洞 |",
            "|---|---|---|---|---|---:|---|---|---|",
        ]
    )
    for _, row in audit.scores.sort_values("code", kind="stable").iterrows():
        code = _normalise_code(row.get("code"))
        cases = row.get("bear_case", [])
        bear_text = "；".join(
            f"{case.get('dimension')} {case.get('score')}分:{case.get('reason')}"
            for case in cases
            if isinstance(case, Mapping)
        )
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    code,
                    row.get("name", ""),
                    row.get("industry", ""),
                    row.get("primary_label", ""),
                    row.get("diagnostic_label", ""),
                    row.get("max_score", ""),
                    ",".join(row.get("buy_types", [])),
                    (
                        "有效"
                        if code in audit.dcf_results
                        else f"跳过:{audit.dcf_skip_reasons.get(code, '无结构化原因')}"
                    ),
                    bear_text,
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_safe(value: Any) -> Any:
    """Recursively replace pandas/NumPy non-finite scalars with JSON null."""
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _json_safe(converted)
    return value


def _spreadsheet_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    safe = frame.copy()

    def neutralise(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.lstrip()
        if value[:1] in {"\t", "\r", "\n"} or stripped[:1] in {"=", "+", "-", "@"}:
            return "'" + value
        return value

    for column in safe.columns:
        safe[column] = safe[column].map(neutralise)
    return safe


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _flat_dcf_record(audit: RandomSampleAudit, code: str) -> dict[str, Any]:
    result = audit.dcf_results.get(code)
    if not isinstance(result, Mapping):
        classification = audit.dcf_skip_classifications.get(code, {})
        return {
            "DCF状态": "跳过",
            "DCF跳过分类": classification.get("category") if isinstance(classification, Mapping) else None,
            "DCF跳过原因": audit.dcf_skip_reasons.get(code, "无结构化原因"),
            "DCF区域": None,
            "DCF当前价": None,
            "DCF买入区上界": None,
            "DCF卖出区下界": None,
            "DCF安全边际%": None,
            "DCF基础WACC": None,
            "DCF基础FCF": None,
            "DCF参数JSON": "{}",
            "DCF_pessimistic_lower": None,
            "DCF_pessimistic_upper": None,
            "DCF_neutral_lower": None,
            "DCF_neutral_upper": None,
            "DCF_optimistic_lower": None,
            "DCF_optimistic_upper": None,
        }
    record: dict[str, Any] = {
        "DCF状态": "有效",
        "DCF跳过分类": "",
        "DCF跳过原因": "",
        "DCF区域": result.get("zone"),
        "DCF当前价": result.get("current_price"),
        "DCF买入区上界": result.get("buy_zone_upper"),
        "DCF卖出区下界": result.get("sell_zone_lower"),
        "DCF安全边际%": result.get("safety_margin_pct"),
        "DCF基础WACC": result.get("base_wacc"),
        "DCF基础FCF": result.get("base_fcf"),
        "DCF参数JSON": json.dumps(
            _json_safe(result.get("params", {})),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        ),
    }
    points = result.get("dcf_points", {})
    for scenario in ("pessimistic", "neutral", "optimistic"):
        band = points.get(scenario, {}) if isinstance(points, Mapping) else {}
        record[f"DCF_{scenario}_lower"] = band.get("lower") if isinstance(band, Mapping) else None
        record[f"DCF_{scenario}_upper"] = band.get("upper") if isinstance(band, Mapping) else None
    return record


def write_audit_artifacts(
    audit: RandomSampleAudit,
    output_dir: str | os.PathLike[str],
    *,
    data_timestamp: float,
) -> Mapping[str, Path]:
    """Write detailed JSON, flat CSV and human-readable Markdown atomically."""
    destination = Path(output_dir)
    stem = f"random{audit.sample_size}_audit_seed{audit.seed}"
    paths = {
        "json": destination / f"{stem}.json",
        "csv": destination / f"{stem}.csv",
        "markdown": destination / f"{stem}.md",
    }
    timestamp = datetime.fromtimestamp(float(data_timestamp), tz=timezone.utc).isoformat()
    payload = {
        "seed": audit.seed,
        "sample_size": audit.sample_size,
        "sample_codes": list(audit.sample_codes),
        "data_timestamp_utc": timestamp,
        "dcf_valid": len(audit.dcf_results),
        "eligible_universe_size": audit.eligible_universe_size,
        "provenance": dict(audit.provenance),
        "analysis_quality": dict(audit.analysis_quality),
        "dcf_results": dict(audit.dcf_results),
        "dcf_skip_reasons": dict(audit.dcf_skip_reasons),
        "dcf_skip_classifications": dict(audit.dcf_skip_classifications),
        "pipeline_issues": [
            {"code": issue.code, "stage": issue.stage, "message": issue.message} for issue in audit.pipeline_issues
        ],
        "engine_self_check_errors": list(audit.engine_invariant_errors),
        "same_source_scoring_replay_errors": list(audit.scoring_replay_errors),
        "same_source_valuation_replay_errors": list(audit.valuation_replay_errors),
        "independent_check_errors": list(audit.independent_errors),
        "invariant_errors": list(audit.invariant_errors),
        "companies": audit.scores.sort_values("code", kind="stable").to_dict(orient="records"),
    }
    flat_rows: list[dict[str, Any]] = []
    for _, row in audit.scores.sort_values("code", kind="stable").iterrows():
        code = _normalise_code(row.get("code"))
        record = {
            "代码": code,
            "名称": row.get("name"),
            "行业": row.get("industry"),
            "买入判定": row.get("primary_label"),
            "诊断框架": row.get("diagnostic_label"),
            "最高分": row.get("max_score"),
            "触发类型": ",".join(row.get("buy_types", [])),
            "DCF有效": code in audit.dcf_results,
            "空头漏洞": "；".join(
                f"{case.get('dimension')} {case.get('score')}分:{case.get('reason')}"
                for case in row.get("bear_case", [])
                if isinstance(case, Mapping)
            ),
        }
        for type_key in _AUDIT_WEIGHTS:
            payload_for_type = row.get(type_key, {})
            record[f"{type_key}总分"] = payload_for_type.get("total")
            record[f"{type_key}触发"] = bool(payload_for_type.get("triggered"))
            record[f"{type_key}否决"] = bool(payload_for_type.get("veto"))
            sub_scores = payload_for_type.get("sub_scores", {})
            reasons = payload_for_type.get("reasons", {})
            for dimension in _AUDIT_WEIGHTS[type_key]:
                record[f"{dimension}子分"] = sub_scores.get(dimension)
                record[f"{dimension}依据"] = reasons.get(dimension)
            record[f"{type_key}元信息JSON"] = json.dumps(
                {key: value for key, value in reasons.items() if str(key).startswith("_")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=_json_default,
            )
        record.update(_flat_dcf_record(audit, code))
        flat_rows.append(record)
    csv_text = _spreadsheet_safe_frame(pd.DataFrame(flat_rows)).to_csv(index=False, lineterminator="\n")
    markdown_text = render_audit_markdown(audit, data_timestamp=data_timestamp)
    payload["companion_artifacts_sha256"] = {
        "csv": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
        "markdown": hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
    }

    # Write the JSON manifest last.  An interrupted replacement can therefore
    # only leave mismatched companions, which the release verifier rejects.
    _atomic_write_text(paths["csv"], csv_text)
    _atomic_write_text(paths["markdown"], markdown_text)
    _atomic_write_text(
        paths["json"],
        json.dumps(
            _json_safe(payload),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=_json_default,
        )
        + "\n",
    )
    return paths
