"""Fail closed when a DS_DCF desktop source archive contains unsafe or stale files."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import stat

# Fixed-argv local Git provenance checks only; no shell is involved.
import subprocess  # nosec B404
import tomllib
from typing import Any
import unicodedata
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile


_FORBIDDEN_PATH = re.compile(
    r"(?:^|/)(?:\.git|\.reasonix|\.agents|\.codex|\.playwright-cli|__pycache__|\.pytest_cache|\.ruff_cache|"
    r"\.mypy_cache|\.venv|venv|dist|build|htmlcov|[^/]+\.egg-info)(?:/|$)",
    re.IGNORECASE,
)
_FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pkl",
    ".pickle",
    ".parquet",
    ".log",
    ".tmp",
    ".bak",
    ".zip",
    ".7z",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)
_LF_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".scss",
    ".html",
    ".sh",
    ".ps1",
    ".md",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".xml",
    ".csv",
)
_LF_NAMES = {".gitignore", ".gitattributes", "license"}
_REQUIRED_FILES = {
    "LICENSE",
    "README.md",
    "run.bat",
    ".streamlit/config.toml",
    "app.py",
    "config.py",
    "data/cache.py",
    "data/capex_evidence.py",
    "data/datacenter.py",
    "data/financial_indicator_evidence.py",
    "data/financial_source_evidence.py",
    "data/financial_balance_sheet_evidence.json",
    "data/financial_zero_capex_evidence.json",
    "data/financial_zero_revenue_evidence.json",
    "data/fetcher.py",
    "data/industry.py",
    "data/industry_f10.json",
    "data/industry_em_map.json",
    "data/industry_capco_2025h2.json",
    "data/industry_exchange_new_listings_2026.json",
    "data/market_coldness.py",
    "data/market_history.py",
    "data/quality_history.py",
    "data/snapshot.py",
    "engine/audit.py",
    "engine/buy_screener.py",
    "engine/dcf.py",
    "engine/market_coldness.py",
    "engine/pipeline.py",
    "engine/quantitative_evidence.py",
    "engine/quality_equity.py",
    "engine/risk.py",
    "engine/scenarios.py",
    "engine/valuation_status.py",
    "ui/buy_types_page.py",
    "ui/leaders_page.py",
    "tools/__init__.py",
    "tools/build_official_industry_source.py",
    "tools/build_desktop.py",
    "tools/run_full_audit.py",
    "tools/verify_release_zip.py",
    "desktop/__init__.py",
    "desktop/launcher.py",
    "desktop/installer.py",
    "desktop/updater.py",
    "desktop/version.py",
    "desktop/update_config.json",
    "desktop/version_info.txt",
    "desktop/DS_DCF.spec",
    "desktop/DS_DCF_Installer.spec",
    "requirements-bootstrap.txt",
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-test.txt",
    "requirements-dev.txt",
    "requirements-dev-lock.txt",
    "pyproject.toml",
    "audit/random100_audit_seed20260715.json",
    "audit/random100_audit_seed20260715.csv",
    "audit/random100_audit_seed20260715.md",
}
_AUDIT_JSON_PATH = "audit/random100_audit_seed20260715.json"
_AUDIT_CSV_PATH = "audit/random100_audit_seed20260715.csv"
_AUDIT_MARKDOWN_PATH = "audit/random100_audit_seed20260715.md"
_RELEASE_AUDIT_PATHS = {_AUDIT_JSON_PATH, _AUDIT_CSV_PATH, _AUDIT_MARKDOWN_PATH}
_EXPECTED_AUDIT_SEED = 20260715
_EXPECTED_RELEASE_VERSION = "11.1.0"
_EXPECTED_PROJECT_LICENSE = "LicenseRef-PolyForm-Noncommercial-1.0.0"
_EXPECTED_LICENSE_SHA256 = "a7106a6f8ee245b6e8b0482b8eab8c874a8a40819c8718c92180e0ef3dad596c"
_EXPECTED_UPDATE_MANIFEST_URL = (
    "https://github.com/Muguett-DBY/quant-buy-signals/releases/latest/download/update-manifest.json"
)
_EXPECTED_PATCH6_SOURCE = {
    "path_at_model_authoring": r"E:\模板汇总MD\补丁6.md",
    "sha256": "aa6a5b27e279b324a304a6bea2c6fba9af6dc015f81adb758329137b4e28b8f6",
}
_EXPECTED_TYPE7_SOURCE_DOCUMENTS = {
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
    "patch6": dict(_EXPECTED_PATCH6_SOURCE),
}
_EXPECTED_DIRECT_DEPENDENCIES = {"numpy", "orjson", "pandas", "plotly", "pillow", "requests", "streamlit"}
_SSH_PRIVATE_KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_AUDIT_TYPE_WEIGHTS = {
    "type1": {"1a": 0.30, "1b": 0.35, "1c": 0.20, "1d": 0.15},
    "type2": {"2a": 0.25, "2b": 0.30, "2c": 0.25, "2d": 0.20},
    "type3": {"3a": 0.25, "3b": 0.20, "3c": 0.20, "3d": 0.25, "3e": 0.10},
    "type4": {"4a": 0.25, "4b": 0.25, "4c": 0.20, "4d": 0.15, "4e": 0.08, "4f": 0.07},
    "type5": {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10},
    "type6": {"6a": 0.25, "6b": 0.20, "6c": 0.15, "6d": 0.25, "6e": 0.15},
    "type7": {"7a": 1.0 / 3.0, "7b": 1.0 / 3.0, "7c": 1.0 / 3.0},
}
_AUDIT_TYPE_DIMENSIONS = {type_key: set(weights) for type_key, weights in _AUDIT_TYPE_WEIGHTS.items()}
_AUDIT_TYPE_PRIORITY = ("type1", "type2", "type5", "type3", "type4", "type6", "type7")
_AUDIT_TYPE_NAMES = {
    "type1": "1️⃣ 估值买入区",
    "type2": "2️⃣ 两热一冷",
    "type3": "3️⃣ 可持续高增长",
    "type4": "4️⃣ 长坡厚雪",
    "type5": "5️⃣ 强周期底部",
    "type6": "6️⃣ VC属性",
    "type7": "7️⃣ 优质股权型",
}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(raw: bytes) -> Any:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


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
_MIN_RELEASE_ELIGIBLE_COMPANIES = 4_000
_MIN_RELEASE_MARKET_COUNTS = {"SH": 1_800, "SZ": 2_300}
_MIN_RELEASE_STRICT_TTM_SOURCE_COVERAGE = 0.90
_MIN_RELEASE_LISTING_REFERENCE_COVERAGE = 0.99
_MIN_RELEASE_LISTING_DATE_COVERAGE = 0.99
_LISTING_DATE_SOURCE = "Eastmoney push2 clist"
_LISTING_DATE_SOURCE_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_TTM_PERIOD_BASIS = "FY_plus_current_YTD_minus_prior_YTD"
_TTM_FCFF_FORMULA_VERSION = "ttm_cfo_less_capex_v2"
_TTM_REVENUE_FORMULA_VERSION = "ttm_revenue_v1"
_TTM_SOURCE_UNIT = "CNY"
_FCF_NORMALISATION_PREMIUM_CAP = 1.25
_DCF_FORECAST_YEARS = 5
_DCF_BAND_WACC_DELTA = 0.005
_DCF_MARGIN_FLOOR = 0.0
_DCF_MARGIN_LONG_TERM = 0.04
_DCF_WACC_SHIFT = {"pessimistic": 0.010, "neutral": 0.0, "optimistic": -0.005}
_DCF_TERMINAL_GROWTH = {"pessimistic": 0.0, "neutral": 0.01, "optimistic": 0.02}
_TTM_OPERATING_CASH_FIELDS = {"NETCASH_OPERATE", "经营活动产生的现金流量净额"}
_TTM_CAPEX_FIELDS = {
    "CONSTRUCT_LONG_ASSET",
    "PAY_ACQ_CONST_FIASSETS",
    "购建固定资产无形资产和其他长期资产支付的现金",
}
_TTM_REVENUE_FIELDS = {"TOTAL_OPERATE_INCOME", "OPERATE_INCOME"}
_CAPEX_PROVENANCE_SCHEMA_VERSION = 1
_STANDARD_CASHFLOW_REPORT = "RPT_DMSK_FN_CASHFLOW"
_DETAILED_CASHFLOW_REPORT = "RPT_F10_FINANCE_GCASHFLOW"
_EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_NON_CAPEX_OUTFLOW_FIELDS = {
    "INVEST_PAY_CASH",
    "PLEDGE_LOAN_ADD",
    "OBTAIN_SUBSIDIARY_OTHER",
    "ADD_PLEDGE_TIMEDEPOSITS",
    "PAY_OTHER_INVEST",
    "INVEST_OUTFLOW_OTHER",
    "INVEST_OUTFLOW_BALANCE",
}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)[\"']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|private[_-]?key|token)"
    rb"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_./+=-]{16,})[\"']"
)
_SECRET_SCAN_SUFFIXES = {".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml"}
_RULE_FILES = {
    "config.py",
    "data/capex_evidence.py",
    "data/datacenter.py",
    "data/financial_indicator_evidence.py",
    "data/financial_source_evidence.py",
    "data/financial_balance_sheet_evidence.json",
    "data/financial_zero_capex_evidence.json",
    "data/financial_zero_revenue_evidence.json",
    "data/industry.py",
    "data/market_coldness.py",
    "data/market_history.py",
    "data/quality_history.py",
    "engine/buy_screener.py",
    "engine/dcf.py",
    "engine/market_coldness.py",
    "engine/quantitative_evidence.py",
    "engine/quality_equity.py",
    "engine/risk.py",
    "engine/scenarios.py",
    "engine/valuation_status.py",
}
_INDUSTRY_FILES = {
    "data/industry.py",
    "data/industry_f10.json",
    "data/industry_em_map.json",
    "data/industry_capco_2025h2.json",
    "data/industry_exchange_new_listings_2026.json",
}
_DEPENDENCY_FILES = {
    "requirements-bootstrap.txt",
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-test.txt",
    "requirements-dev.txt",
    "requirements-dev-lock.txt",
    "pyproject.toml",
}
_TSINGHUA_PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _desktop_launcher_errors(content: bytes) -> tuple[str, ...]:
    """Validate the desktop install path without executing an untrusted batch file."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ("run.bat is not valid UTF-8 text",)

    errors: list[str] = []
    folded = text.casefold()
    mirror_pattern = re.compile(
        rf'^\s*if\s+not\s+defined\s+PIP_INDEX_URL\s+set\s+"PIP_INDEX_URL='
        rf'{re.escape(_TSINGHUA_PYPI_INDEX)}"\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    mirror_match = mirror_pattern.search(text)
    if mirror_match is None or folded.count("pip_index_url=") != 1:
        errors.append("run.bat does not provide an overridable Tsinghua HTTPS PIP_INDEX_URL default")

    require_venv_match = re.search(
        r'^\s*set\s+"PIP_REQUIRE_VIRTUALENV=true"\s*$',
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if require_venv_match is None:
        errors.append("run.bat does not require an active virtual environment for pip")

    if "--trusted-host" in folded or "pip_trusted_host" in folded:
        errors.append("run.bat weakens pip TLS verification with trusted-host")
    if "--index-url" in folded:
        errors.append("run.bat hard-codes --index-url instead of honoring PIP_INDEX_URL")

    install_matches: list[re.Match[str]] = []
    for lock_name in ("requirements-bootstrap.txt", "requirements-lock.txt"):
        install_pattern = re.compile(
            rf'^\s*"%VENV_PYTHON%"\s+-m\s+pip\s+install'
            rf"(?=[^\r\n]*\s--require-hashes(?:\s|$))"
            rf'(?=[^\r\n]*\s-r\s+"?{re.escape(lock_name)}"?(?:\s|$))'
            rf"[^\r\n]*\r?$",
            re.IGNORECASE | re.MULTILINE,
        )
        install_match = install_pattern.search(text)
        if install_match is None:
            errors.append(f"run.bat does not hash-lock {lock_name} installation inside .venv")
        else:
            install_matches.append(install_match)

    if install_matches:
        first_install = min(match.start() for match in install_matches)
        if mirror_match is not None and mirror_match.start() > first_install:
            errors.append("run.bat configures PIP_INDEX_URL after dependency installation")
        if require_venv_match is not None and require_venv_match.start() > first_install:
            errors.append("run.bat configures PIP_REQUIRE_VIRTUALENV after dependency installation")
    return tuple(errors)


def _normalised_file_names(names: list[str]) -> dict[str, str]:
    files = [name.replace("\\", "/") for name in names if name and not name.endswith("/")]
    first_parts = {PurePosixPath(name).parts[0] for name in files if PurePosixPath(name).parts}
    prefix = next(iter(first_parts)) if len(first_parts) == 1 and all("/" in name for name in files) else None
    result: dict[str, str] = {}
    portable_identities: dict[str, str] = {}
    for archive_name in files:
        path = PurePosixPath(archive_name)
        parts = path.parts[1:] if prefix is not None and path.parts[0] == prefix else path.parts
        for part in path.parts:
            if (
                not part
                or unicodedata.normalize("NFC", part) != part
                or part.endswith((" ", "."))
                or any(character in '<>:"|?*' for character in part)
                or any(ord(character) < 32 for character in part)
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            ):
                raise ValueError(f"unsafe archive path: {archive_name}")
        normalised = "/".join(parts)
        if normalised in result:
            raise ValueError(f"duplicate normalized archive path: {normalised}")
        portable_identity = normalised.casefold()
        if portable_identity in portable_identities:
            raise ValueError(
                f"case-insensitive archive path collision: {portable_identities[portable_identity]} and {normalised}"
            )
        result[normalised] = archive_name
        portable_identities[portable_identity] = normalised
    return result


def _hash_archive_files(archive: ZipFile, file_names: dict[str, str], selected: set[str]) -> str:
    digest = hashlib.sha256()
    for normalised in sorted(path for path in selected if path in file_names):
        digest.update(normalised.encode("utf-8"))
        digest.update(b"\0")
        digest.update(archive.read(file_names[normalised]))
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalise_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _locked_requirements(content: bytes) -> dict[str, str] | None:
    """Parse a pip-compile lock and require exact pins plus real SHA256 hashes."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    logical_lines: list[str] = []
    pending = ""
    for physical in text.splitlines():
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1].strip() + " "
            continue
        logical_lines.append((pending + stripped).strip())
        pending = ""
    if pending:
        return None

    result: dict[str, str] = {}
    for line in logical_lines:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;\\]+)(.*)", line)
        if match is None:
            return None
        name = _normalise_distribution_name(match.group(1))
        version = match.group(2)
        tail = match.group(3).strip()
        hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]{64})(?=\s|$)", tail)
        remainder = re.sub(r"--hash=sha256:[0-9a-fA-F]{64}(?=\s|$)", "", tail).strip()
        if not hashes or remainder or name in result or any(len(set(value.lower())) < 2 for value in hashes):
            return None
        result[name] = version
    return result or None


def _exact_project_dependencies(project: Mapping[str, Any]) -> dict[str, str] | None:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return None
    result: dict[str, str] = {}
    for dependency in dependencies:
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^\s;]+)\s*", str(dependency))
        if match is None:
            return None
        name = _normalise_distribution_name(match.group(1))
        if name in result:
            return None
        result[name] = match.group(2)
    return result


def _runtime_python_supported(runtime_version: object, requires_python: object) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", str(runtime_version))
    if match is None or not isinstance(requires_python, str):
        return False
    version = tuple(int(part) for part in match.groups())
    for raw_clause in requires_python.split(","):
        clause = raw_clause.strip()
        bound_match = re.fullmatch(r"(>=|<=|>|<|==)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", clause)
        if bound_match is None:
            return False
        operator = bound_match.group(1)
        bound = tuple(int(part or 0) for part in bound_match.groups()[1:])
        comparisons = {
            ">=": version >= bound,
            "<=": version <= bound,
            ">": version > bound,
            "<": version < bound,
            "==": version == bound,
        }
        if not comparisons[operator]:
            return False
    return True


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _audit_type7_ledger_valid(code: str, ledger: Any, status: Any) -> bool:
    """Independently replay the Type 7 formula ledger in a release artifact."""

    def close(actual: Any, expected: float) -> bool:
        value = _finite_number(actual)
        return value is not None and math.isclose(value, expected, rel_tol=0.0, abs_tol=0.0001)

    if not isinstance(ledger, Mapping):
        return False
    if status == "not_applicable":
        return bool(
            ledger.get("schema_version") == 1
            and ledger.get("model_id") == _AUDIT_TYPE7_MODEL_ID
            and ledger.get("code") == code
            and ledger.get("applicable") is False
            and str(ledger.get("reason") or "").strip()
        )
    if (
        ledger.get("schema_version") != 1
        or ledger.get("model_id") != _AUDIT_TYPE7_MODEL_ID
        or ledger.get("code") != code
        or ledger.get("source_rule") != "Template1>70 AND Template5>70 AND Patch5>70"
        or _finite_number(ledger.get("strict_threshold")) != 70.0
    ):
        return False

    sections: dict[str, Mapping[str, Any]] = {}
    template_items: dict[str, dict[str, Mapping[str, Any]]] = {}
    for section_key, expected_weights in _AUDIT_TYPE7_TEMPLATE_WEIGHTS.items():
        section = ledger.get(section_key)
        items = section.get("items") if isinstance(section, Mapping) else None
        if not isinstance(section, Mapping) or not isinstance(items, list) or len(items) != len(expected_weights):
            return False
        indexed: dict[str, Mapping[str, Any]] = {}
        for item in items:
            key = item.get("key") if isinstance(item, Mapping) else None
            if not isinstance(key, str) or key not in expected_weights or key in indexed:
                return False
            score = _finite_number(item.get("score"))
            weight = _finite_number(item.get("weight"))
            points = _finite_number(item.get("points"))
            if (
                score is None
                or not 0 <= score <= 10
                or weight != expected_weights[key]
                or points is None
                or not math.isclose(points, round(score * weight / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
                or not isinstance(item.get("complete"), bool)
                or not str(item.get("formula") or "").strip()
                or not isinstance(item.get("inputs"), Mapping)
            ):
                return False
            indexed[key] = item
        if set(indexed) != set(expected_weights):
            return False
        expected_score = round(sum(float(item["points"]) for item in indexed.values()), 2)
        expected_coverage = round(
            sum(expected_weights[key] for key, item in indexed.items() if item["complete"]) / 100.0,
            4,
        )
        if not close(section.get("score"), expected_score) or not close(section.get("coverage"), expected_coverage):
            return False
        sections[section_key] = section
        template_items[section_key] = indexed

    patch5 = ledger.get("patch5")
    dimensions = patch5.get("dimensions") if isinstance(patch5, Mapping) else None
    if not isinstance(patch5, Mapping) or not isinstance(dimensions, list) or len(dimensions) != 5:
        return False
    patch_sections: dict[str, Mapping[str, Any]] = {}
    for section in dimensions:
        key = section.get("key") if isinstance(section, Mapping) else None
        expected_weights = _AUDIT_TYPE7_PATCH_WEIGHTS.get(key) if isinstance(key, str) else None
        components = section.get("components") if isinstance(section, Mapping) else None
        if (
            expected_weights is None
            or key in patch_sections
            or not isinstance(components, list)
            or len(components) != len(expected_weights)
            or _finite_number(section.get("max_points")) != 20.0
            or not isinstance(section.get("complete"), bool)
        ):
            return False
        indexed: dict[str, Mapping[str, Any]] = {}
        for component in components:
            component_key = component.get("key") if isinstance(component, Mapping) else None
            if not isinstance(component_key, str) or component_key not in expected_weights or component_key in indexed:
                return False
            score = _finite_number(component.get("score"))
            maximum = expected_weights[component_key]
            points = _finite_number(component.get("points"))
            if (
                score is None
                or not 0 <= score <= 10
                or _finite_number(component.get("max_points")) != maximum
                or points is None
                or not math.isclose(points, round(score * maximum / 10.0, 4), rel_tol=0.0, abs_tol=0.0001)
                or not isinstance(component.get("complete"), bool)
            ):
                return False
            indexed[component_key] = component
        if set(indexed) != set(expected_weights):
            return False
        expected_points = round(sum(float(item["points"]) for item in indexed.values()), 4)
        expected_complete = all(item["complete"] for item in indexed.values())
        if not close(section.get("points"), expected_points) or section.get("complete") is not expected_complete:
            return False
        patch_sections[key] = section
    if set(patch_sections) != set(_AUDIT_TYPE7_PATCH_WEIGHTS):
        return False
    expected_patch_score = round(sum(float(section["points"]) for section in patch_sections.values()), 2)
    expected_patch_coverage = round(
        sum(20.0 for section in patch_sections.values() if section["complete"]) / 100.0,
        4,
    )
    safety = patch_sections["p5_safety"]
    if (
        not close(patch5.get("score"), expected_patch_score)
        or not close(patch5.get("coverage"), expected_patch_coverage)
        or not close(patch5.get("safety_margin_score"), round(float(safety["points"]), 2))
        or patch5.get("safety_margin_complete") is not safety["complete"]
    ):
        return False

    scores = ledger.get("scores")
    expected_sections = {"template1": sections["template1"], "template5": sections["template5"], "patch5": patch5}
    if not isinstance(scores, Mapping) or set(scores) != set(expected_sections):
        return False
    score_values: dict[str, float] = {}
    for key, section in expected_sections.items():
        value = _finite_number(scores.get(key))
        section_score = _finite_number(section.get("score"))
        if value is None or section_score is None or value != section_score:
            return False
        score_values[key] = value
    strict = {key: value > 70.0 for key, value in score_values.items()}
    if ledger.get("strict_checks") != strict or ledger.get("all_scores_strictly_above_70") is not all(strict.values()):
        return False
    prerequisites = ledger.get("prerequisites")
    if not isinstance(prerequisites, Mapping) or set(prerequisites) != _AUDIT_TYPE7_PREREQUISITES:
        return False
    pass_flags = {key: record.get("passed") for key, record in prerequisites.items() if isinstance(record, Mapping)}
    if len(pass_flags) != len(_AUDIT_TYPE7_PREREQUISITES) or any(
        not isinstance(value, bool) for value in pass_flags.values()
    ):
        return False

    valuation_prerequisite = prerequisites.get("latest_quote_and_valuation")
    t1_items = template_items.get("template1", {})
    expected_valuation_complete = t1_items.get("t1_20", {}).get("complete") is True
    if not isinstance(valuation_prerequisite, Mapping):
        return False
    raw_quote_as_of = valuation_prerequisite.get("as_of")
    try:
        quote_as_of = date.fromisoformat(raw_quote_as_of) if isinstance(raw_quote_as_of, str) else None
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
        return False

    report_prerequisite = prerequisites.get("three_external_reports")
    if not isinstance(report_prerequisite, Mapping) or set(report_prerequisite) != {
        "passed",
        "source_count",
        "distinct_publishers",
        "sources",
    }:
        return False
    sources = report_prerequisite.get("sources")
    if not isinstance(sources, list) or len(sources) > 20 or (sources and quote_as_of is None):
        return False
    normalized_sources: list[dict[str, str]] = []
    identities: set[str] = set()
    urls: set[str] = set()
    for source in sources:
        fields = {"title", "publisher", "url", "as_of", "evidence_id"}
        if (
            not isinstance(source, Mapping)
            or set(source) != fields
            or any(not isinstance(source.get(field), str) for field in fields)
        ):
            return False
        item = {field: str(source[field]).strip() for field in fields}
        try:
            parsed = urlsplit(item["url"])
            _ = parsed.port
            source_date = date.fromisoformat(item["as_of"])
        except (ValueError, TypeError):
            return False
        identity = item["evidence_id"].casefold()
        canonical_url = item["url"].casefold()
        if (
            any(not text or len(text) > 300 or any(ord(character) < 32 for character in text) for text in item.values())
            or parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.fragment)
            or source_date > quote_as_of
            or identity in identities
            or canonical_url in urls
        ):
            return False
        identities.add(identity)
        urls.add(canonical_url)
        normalized_sources.append(item)
    normalized_sources.sort(key=lambda item: (item["as_of"], item["publisher"], item["evidence_id"]))
    publisher_count = len({item["publisher"].casefold() for item in normalized_sources})
    expected_report_pass = len(normalized_sources) >= 3 and publisher_count >= 3
    if not (
        normalized_sources == sources
        and report_prerequisite.get("source_count") == len(normalized_sources)
        and report_prerequisite.get("distinct_publishers") == publisher_count
        and report_prerequisite.get("passed") is expected_report_pass
    ):
        return False

    history_prerequisite = prerequisites.get("ten_year_return_and_five_year_valuation")
    history_inputs = t1_items.get("t1_19", {}).get("inputs", {})
    shareholder_input = history_inputs.get("shareholder_return") if isinstance(history_inputs, Mapping) else None
    expected_history_pass = bool(
        isinstance(shareholder_input, Mapping)
        and shareholder_input.get("available") is True
        and template_items.get("template5", {}).get("t5_v1", {}).get("complete") is True
    )
    if not isinstance(history_prerequisite, Mapping) or set(history_prerequisite) != {"passed", "as_of"}:
        return False
    history_as_of = history_prerequisite.get("as_of")
    try:
        history_date = date.fromisoformat(history_as_of) if isinstance(history_as_of, str) else None
    except ValueError:
        history_date = None
    if not (
        history_prerequisite.get("passed") is expected_history_pass
        and (history_as_of is None or history_date is not None)
        and (not expected_history_pass or history_as_of == raw_quote_as_of)
    ):
        return False

    prerequisites_complete = all(pass_flags.values())
    safety_score = _finite_number(patch5.get("safety_margin_score"))
    safety_veto = bool(safety["complete"] and safety_score is not None and safety_score < 8.0)
    expected_trigger = all(strict.values()) and prerequisites_complete and not safety_veto
    if not (
        ledger.get("prerequisites_complete") is prerequisites_complete
        and ledger.get("safety_veto") is safety_veto
        and ledger.get("triggered") is expected_trigger
    ):
        return False

    t1_items = template_items.get("template1", {})
    t5_items = template_items.get("template5", {})
    if not (
        {"t1_18", "t1_19", "t1_20"}.issubset(t1_items)
        and {"t5_v1", "t5_v3"}.issubset(t5_items)
        and "p5_safety" in patch_sections
    ):
        return False
    safety_components = {
        component.get("key"): component
        for component in patch_sections["p5_safety"].get("components", [])
        if isinstance(component, Mapping)
    }
    valuation_component = safety_components.get("p5_s1")
    dcf_score = _finite_number(t1_items["t1_20"].get("score"))
    if not isinstance(valuation_component, Mapping) or dcf_score is None:
        return False
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
        not isinstance(published_upper, Mapping)
        or set(published_upper) != set(expected_upper)
        or any(not close(published_upper.get(key), value) for key, value in expected_upper.items())
    ):
        return False
    expected_request = bool(
        not pass_flags["ten_year_return_and_five_year_valuation"]
        and all(value for key, value in pass_flags.items() if key != "ten_year_return_and_five_year_valuation")
        and all(value > 70.0 for value in expected_upper.values())
    )
    return ledger.get("history_request_needed") is expected_request


def _audit_company_codes(payload: Mapping[str, Any]) -> list[str] | None:
    companies = payload.get("companies")
    if not isinstance(companies, list) or len(companies) != 100:
        return None
    codes: list[str] = []
    for company in companies:
        if not isinstance(company, Mapping):
            return None
        code = str(company.get("code", ""))
        if re.fullmatch(r"[036][0-9]{5}", code) is None:
            return None
        required_fields = {
            "name",
            "industry",
            "price",
            "market_cap",
            "buy_types",
            "num_types",
            "primary_type",
            "primary_label",
            "diagnostic_type",
            "diagnostic_label",
            "max_score",
            "bear_case",
        }
        required_fields.update(_AUDIT_TYPE_DIMENSIONS)
        required_fields.update(f"{type_key}_score" for type_key in _AUDIT_TYPE_DIMENSIONS)
        if not required_fields.issubset(company):
            return None
        if not str(company.get("name") or "").strip() or str(company.get("industry") or "").strip() in {"", "DEFAULT"}:
            return None
        if (_finite_number(company.get("price")) or 0) <= 0 or (_finite_number(company.get("market_cap")) or 0) <= 0:
            return None

        triggered_set: set[str] = set()
        clean_sub_scores: dict[str, dict[str, float]] = {}
        clean_reasons: dict[str, Mapping[str, Any]] = {}
        diagnostic_totals: dict[str, float] = {}
        for type_key, dimensions in _AUDIT_TYPE_DIMENSIONS.items():
            type_payload = company.get(type_key)
            if not isinstance(type_payload, Mapping):
                return None
            if (
                not isinstance(type_payload.get("triggered"), bool)
                or not isinstance(type_payload.get("veto"), bool)
                or not isinstance(type_payload.get("applicable"), bool)
                or not isinstance(type_payload.get("evidence_complete"), bool)
            ):
                return None
            total = _finite_number(type_payload.get("total"))
            sub_scores = type_payload.get("sub_scores")
            reasons = type_payload.get("reasons")
            if (
                total is None
                or not 0 <= total <= 10
                or not isinstance(sub_scores, Mapping)
                or set(sub_scores) != dimensions
                or not isinstance(reasons, Mapping)
                or not dimensions.issubset(reasons)
                or any(not str(key).startswith("_") for key in set(reasons) - dimensions)
                or any(
                    (score := _finite_number(sub_scores.get(dimension))) is None or not 0 <= score <= 10
                    for dimension in dimensions
                )
                or any(
                    not str(reasons.get(dimension) or "").strip()
                    or len(str(reasons.get(dimension))) > _AUDIT_REASON_MAX_LENGTH
                    for dimension in dimensions
                )
                or any(
                    not str(value or "").strip() or len(str(value)) > _AUDIT_REASON_MAX_LENGTH
                    for key, value in reasons.items()
                    if key not in {"_status", "_applicable", "_evidence"}
                )
            ):
                return None
            scores_for_type = {
                dimension: float(_finite_number(sub_scores[dimension])) for dimension in _AUDIT_TYPE_WEIGHTS[type_key]
            }
            raw_total = round(
                sum(scores_for_type[dimension] * weight for dimension, weight in _AUDIT_TYPE_WEIGHTS[type_key].items()),
                1,
            )
            expected_total = min(raw_total, 4.9) if type_key == "type3" and scores_for_type["3e"] <= 3 else raw_total
            if (
                not math.isclose(total, expected_total, rel_tol=0.0, abs_tol=1e-9)
                or _finite_number(company.get(f"{type_key}_score")) != total
                or (
                    type_key == "type3"
                    and (
                        (scores_for_type["3e"] <= 3 and raw_total > 4.9 and not reasons.get("_downgrade"))
                        or (scores_for_type["3e"] > 3 and reasons.get("_downgrade"))
                    )
                )
            ):
                return None
            reason_veto = bool(reasons.get("_veto"))
            status = type_payload.get("status")
            applicable = type_payload["applicable"]
            evidence_complete = type_payload["evidence_complete"]
            triggered = type_payload["triggered"]
            condition = bool(reasons.get("_condition"))
            if type_key == "type7":
                ledger = type_payload.get("ledger")
                if not _audit_type7_ledger_valid(code, ledger, status):
                    return None
                if status != "not_applicable" and isinstance(ledger, Mapping):
                    source_scores = ledger.get("scores")
                    if not isinstance(source_scores, Mapping):
                        return None
                    source_values = {
                        key: _finite_number(source_scores.get(key)) for key in ("template1", "template5", "patch5")
                    }
                    if any(value is None for value in source_values.values()):
                        return None
                    expected_type7_scores = {
                        "7a": round(source_values["template1"] / 10.0, 3),
                        "7b": round(source_values["template5"] / 10.0, 3),
                        "7c": round(source_values["patch5"] / 10.0, 3),
                    }
                    if scores_for_type != expected_type7_scores:
                        return None
                    if status != "blocked" and triggered is not ledger.get("triggered"):
                        return None
            if (
                type_payload["veto"] != reason_veto
                or status not in _AUDIT_TYPE_STATUSES
                or reasons.get("_status") != status
                or reasons.get("_applicable") != ("yes" if applicable else "no")
                or reasons.get("_evidence") != ("complete" if evidence_complete else "incomplete")
                or applicable != (status != "not_applicable")
                or evidence_complete != (reasons.get("_evidence") == "complete")
                or triggered != (status == "triggered")
            ):
                return None
            if status in _AUDIT_NON_DIAGNOSTIC_STATUSES and (reason_veto or triggered):
                return None
            if status == "triggered" and (total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or condition):
                return None
            if status == "triggered" and not evidence_complete:
                return None
            if status == "conditional" and (total < _AUDIT_QUALIFY_THRESHOLD or reason_veto or not condition):
                return None
            if status in {"vetoed", "blocked"} and not reason_veto:
                return None
            if status == "observe" and (reason_veto or not 5.0 <= total < _AUDIT_QUALIFY_THRESHOLD):
                return None
            if status == "not_triggered" and (reason_veto or total >= 5.0):
                return None
            clean_sub_scores[type_key] = scores_for_type
            clean_reasons[type_key] = reasons
            if status not in _AUDIT_NON_DIAGNOSTIC_STATUSES:
                diagnostic_totals[type_key] = total
            if triggered:
                triggered_set.add(type_key)

        triggered_types = [type_key for type_key in _AUDIT_TYPE_PRIORITY if type_key in triggered_set]
        buy_types = company.get("buy_types")
        if buy_types != triggered_types or company.get("num_types") != len(triggered_types):
            return None
        expected_primary = triggered_types[0] if triggered_types else None
        expected_primary_label = _AUDIT_TYPE_NAMES[expected_primary] if expected_primary else "无触发（不买）"
        if company.get("primary_type") != expected_primary or company.get("primary_label") != expected_primary_label:
            return None
        expected_diagnostic: str | None = None
        if diagnostic_totals:
            maximum = max(diagnostic_totals.values())
            expected_diagnostic = next(
                type_key for type_key in _AUDIT_TYPE_PRIORITY if diagnostic_totals.get(type_key) == maximum
            )
            if (
                company.get("diagnostic_type") != expected_diagnostic
                or company.get("diagnostic_label") != _AUDIT_TYPE_NAMES[expected_diagnostic]
                or _finite_number(company.get("max_score")) != maximum
            ):
                return None
        elif (
            company.get("diagnostic_type") is not None
            or company.get("diagnostic_label") != "无可完整诊断框架"
            or company.get("max_score") is not None
        ):
            return None
        bear_case = company.get("bear_case")
        if expected_diagnostic is None:
            if bear_case != []:
                return None
            codes.append(code)
            continue
        if not isinstance(bear_case, list) or len(bear_case) != 3:
            return None
        diagnostic_scores = clean_sub_scores[expected_diagnostic]
        diagnostic_reasons = clean_reasons[expected_diagnostic]
        minimum_score = min(diagnostic_scores.values())
        expected_bear_case: list[dict[str, Any]] = []
        for meta_key in ("_veto", "_condition", "_downgrade"):
            if diagnostic_reasons.get(meta_key):
                expected_bear_case.append(
                    {
                        "dimension": meta_key,
                        "score": minimum_score,
                        "reason": str(diagnostic_reasons[meta_key]),
                    }
                )
                if len(expected_bear_case) == 3:
                    break
        if len(expected_bear_case) < 3:
            weights = _AUDIT_TYPE_WEIGHTS[expected_diagnostic]
            order = {key: index for index, key in enumerate(weights)}
            ranked = sorted(weights, key=lambda key: (diagnostic_scores[key], -weights[key], order[key]))
            for dimension in ranked:
                expected_bear_case.append(
                    {
                        "dimension": dimension,
                        "score": diagnostic_scores[dimension],
                        "reason": str(diagnostic_reasons[dimension]),
                    }
                )
                if len(expected_bear_case) == 3:
                    break
        if bear_case != expected_bear_case:
            return None
        codes.append(code)
    return codes


def _audit_csv_rows(content: bytes) -> list[dict[str, str]] | None:
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None or "代码" not in reader.fieldnames:
            return None
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error):
        return None
    if len(rows) != 100:
        return None
    codes = [str(row.get("代码", "")) for row in rows]
    if any(re.fullmatch(r"[036][0-9]{5}", code) is None for code in codes):
        return None
    return rows


def _csv_bool(value: object) -> bool | None:
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _spreadsheet_safe_text(value: object) -> str:
    text = str(value if value is not None else "")
    stripped = text.lstrip()
    if text[:1] in {"\t", "\r", "\n"} or stripped[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _audit_csv_matches_payload(
    rows: list[dict[str, str]],
    companies: list[Mapping[str, Any]],
    dcf_results: Mapping[str, Any],
    dcf_skip_reasons: Mapping[str, Any],
) -> bool:
    required_columns = {
        "代码",
        "名称",
        "行业",
        "买入判定",
        "诊断框架",
        "最高分",
        "触发类型",
        "DCF有效",
        "空头漏洞",
        "DCF状态",
        "DCF跳过原因",
        "DCF区域",
        "DCF当前价",
        "DCF买入区上界",
        "DCF卖出区下界",
    }
    for type_key, dimensions in _AUDIT_TYPE_DIMENSIONS.items():
        required_columns.update({f"{type_key}总分", f"{type_key}触发", f"{type_key}否决", f"{type_key}元信息JSON"})
        for dimension in dimensions:
            required_columns.update({f"{dimension}子分", f"{dimension}依据"})
    if not rows or not required_columns.issubset(rows[0]):
        return False

    company_by_code = {str(company.get("code")): company for company in companies}
    if [row.get("代码") for row in rows] != sorted(company_by_code):
        return False
    for row in rows:
        code = str(row.get("代码"))
        company = company_by_code.get(code)
        if company is None:
            return False
        bear_text = "；".join(
            f"{item.get('dimension')} {item.get('score')}分:{item.get('reason')}"
            for item in company.get("bear_case", [])
            if isinstance(item, Mapping)
        )
        fixed_text = {
            "名称": company.get("name"),
            "行业": company.get("industry"),
            "买入判定": company.get("primary_label"),
            "诊断框架": company.get("diagnostic_label"),
            "触发类型": ",".join(company.get("buy_types", [])),
            "空头漏洞": bear_text,
        }
        if any(row.get(column) != _spreadsheet_safe_text(value) for column, value in fixed_text.items()):
            return False
        if _finite_number(row.get("最高分")) != _finite_number(company.get("max_score")):
            return False

        for type_key, dimensions in _AUDIT_TYPE_DIMENSIONS.items():
            payload = company.get(type_key)
            if not isinstance(payload, Mapping):
                return False
            reasons = payload.get("reasons")
            sub_scores = payload.get("sub_scores")
            if not isinstance(reasons, Mapping) or not isinstance(sub_scores, Mapping):
                return False
            if (
                _finite_number(row.get(f"{type_key}总分")) != _finite_number(payload.get("total"))
                or _csv_bool(row.get(f"{type_key}触发")) != payload.get("triggered")
                or _csv_bool(row.get(f"{type_key}否决")) != payload.get("veto")
            ):
                return False
            expected_meta = json.dumps(
                {key: value for key, value in reasons.items() if str(key).startswith("_")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if row.get(f"{type_key}元信息JSON") != _spreadsheet_safe_text(expected_meta):
                return False
            for dimension in dimensions:
                if _finite_number(row.get(f"{dimension}子分")) != _finite_number(sub_scores.get(dimension)) or row.get(
                    f"{dimension}依据"
                ) != _spreadsheet_safe_text(reasons.get(dimension)):
                    return False

        has_result = code in dcf_results
        if _csv_bool(row.get("DCF有效")) != has_result:
            return False
        if has_result:
            result = dcf_results[code]
            if (
                row.get("DCF状态") != "有效"
                or row.get("DCF跳过原因") != ""
                or row.get("DCF区域") != str(result.get("zone"))
                or _finite_number(row.get("DCF当前价")) != _finite_number(result.get("current_price"))
                or _finite_number(row.get("DCF买入区上界")) != _finite_number(result.get("buy_zone_upper"))
                or _finite_number(row.get("DCF卖出区下界")) != _finite_number(result.get("sell_zone_lower"))
            ):
                return False
        elif row.get("DCF状态") != "跳过" or row.get("DCF跳过原因") != _spreadsheet_safe_text(
            dcf_skip_reasons.get(code)
        ):
            return False
    return True


def _markdown_cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _audit_markdown_matches_payload(
    text: str,
    payload: Mapping[str, Any],
    companies: list[Mapping[str, Any]],
    dcf_results: Mapping[str, Any],
    dcf_skip_reasons: Mapping[str, Any],
) -> bool:
    signal_counts = {
        type_key: sum(bool(company[type_key].get("triggered")) for company in companies)
        for type_key in _AUDIT_TYPE_WEIGHTS
    }
    required_markers = (
        "# 固定随机 100 家公司审计",
        f"- seed: `{_EXPECTED_AUDIT_SEED}`",
        "- sample_size: `100`",
        f"- eligible_universe_size: `{payload.get('eligible_universe_size')}`",
        f"- data_timestamp_utc: `{payload.get('data_timestamp_utc')}`",
        f"- dcf_valid: `{len(dcf_results)}`",
        f"- dcf_skipped_with_reason: `{len(dcf_skip_reasons)}`",
        "- pipeline_issues: `0`",
        "- engine_self_check_errors: `0`",
        "- same_source_scoring_replay_errors: `0`",
        "- same_source_valuation_replay_errors: `0`",
        "- independent_check_errors: `0`",
        f"- triggered_by_type: `{signal_counts}`",
        "## 公司明细",
    )
    if not all(marker in text for marker in required_markers):
        return False
    expected_rows: list[str] = []
    for company in sorted(companies, key=lambda item: str(item.get("code"))):
        code = str(company.get("code"))
        bear_text = "；".join(
            f"{item.get('dimension')} {item.get('score')}分:{item.get('reason')}"
            for item in company.get("bear_case", [])
            if isinstance(item, Mapping)
        )
        dcf_text = "有效" if code in dcf_results else f"跳过:{dcf_skip_reasons.get(code, '无结构化原因')}"
        expected_rows.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    code,
                    company.get("name"),
                    company.get("industry"),
                    company.get("primary_label"),
                    company.get("diagnostic_label"),
                    company.get("max_score"),
                    ",".join(company.get("buy_types", [])),
                    dcf_text,
                    bear_text,
                )
            )
            + " |"
        )
    detail_rows = [line for line in text.splitlines() if re.match(r"^\| [036][0-9]{5} \|", line)]
    return detail_rows == expected_rows


def _close_number(
    left: object,
    right: object,
    *,
    rel_tol: float = 1e-9,
    abs_tol: float = 1e-6,
) -> bool:
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    return (
        left_number is not None
        and right_number is not None
        and math.isclose(left_number, right_number, rel_tol=rel_tol, abs_tol=abs_tol)
    )


def _valid_reporting_period_contract(value: object) -> dict[str, str] | None:
    """Return one canonical calendar-year TTM contract, or fail closed."""
    required = {
        "annual_report_date",
        "current_interim_report_date",
        "prior_interim_report_date",
        "period_basis",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("period_basis") != _TTM_PERIOD_BASIS:
        return None
    raw_dates = tuple(
        value.get(field) for field in ("annual_report_date", "current_interim_report_date", "prior_interim_report_date")
    )
    if not all(isinstance(item, str) for item in raw_dates):
        return None
    try:
        annual, current, prior = (datetime.strptime(item, "%Y-%m-%d") for item in raw_dates)
    except ValueError:
        return None
    if tuple(item.strftime("%Y-%m-%d") for item in (annual, current, prior)) != raw_dates:
        return None
    if not (
        (annual.month, annual.day) == (12, 31)
        and (current.month, current.day) in {(3, 31), (6, 30), (9, 30)}
        and (current.month, current.day) == (prior.month, prior.day)
        and prior.year == annual.year
        and current.year == annual.year + 1
    ):
        return None
    return {key: str(value[key]) for key in required}


def _valid_strict_ttm_source_coverage(validation: Mapping[str, Any]) -> bool:
    """Validate the schema-7 non-financial SH/SZ TTM population ledger."""
    coverage = validation.get("strict_ttm_source_coverage")
    if not isinstance(coverage, Mapping):
        return False
    denominator = coverage.get("denominator")
    excluded = coverage.get("excluded_financial_codes")
    analysis_quotes = validation.get("analysis_market_quotes")
    if (
        coverage.get("population") != "SH_SZ_non_financial"
        or coverage.get("evaluated") is not True
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator <= 0
        or isinstance(analysis_quotes, bool)
        or not isinstance(analysis_quotes, int)
        or not isinstance(excluded, list)
        or excluded != sorted(set(str(code) for code in excluded))
        or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in excluded)
        or denominator + len(excluded) != analysis_quotes
    ):
        return False

    allowed_statuses = {
        "complete",
        "missing_component",
        "duplicate_period",
        "invalid_period_contract",
        "nonfinite_component",
        "implausible_unit",
        "negative_reconstructed_capex",
    }
    population_sets: list[set[str]] = []
    for metric in ("revenue", "fcff"):
        metric_coverage = coverage.get(metric)
        if not isinstance(metric_coverage, Mapping):
            return False
        complete = metric_coverage.get("complete")
        missing = metric_coverage.get("missing")
        ratio = _finite_number(metric_coverage.get("coverage"))
        status_counts = metric_coverage.get("status_counts")
        complete_codes = metric_coverage.get("complete_codes")
        missing_by_status = metric_coverage.get("missing_codes_by_status")
        if (
            isinstance(complete, bool)
            or not isinstance(complete, int)
            or isinstance(missing, bool)
            or not isinstance(missing, int)
            or complete < 0
            or missing < 0
            or complete + missing != denominator
            or ratio is None
            or not math.isclose(ratio, complete / denominator, rel_tol=0.0, abs_tol=1e-12)
            or ratio < _MIN_RELEASE_STRICT_TTM_SOURCE_COVERAGE
            or not isinstance(status_counts, Mapping)
            or not isinstance(complete_codes, list)
            or not isinstance(missing_by_status, Mapping)
        ):
            return False
        if (
            set(status_counts) - allowed_statuses
            or status_counts.get("complete") != complete
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in status_counts.values()
            )
            or sum(status_counts.values()) != denominator
            or complete_codes != sorted(set(str(code) for code in complete_codes))
            or len(complete_codes) != complete
            or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in complete_codes)
        ):
            return False
        expected_missing_statuses = {
            status for status, count in status_counts.items() if status != "complete" and count
        }
        if set(missing_by_status) != expected_missing_statuses:
            return False
        missing_codes: list[str] = []
        for status, codes in missing_by_status.items():
            if (
                status not in allowed_statuses - {"complete"}
                or not isinstance(codes, list)
                or codes != sorted(set(str(code) for code in codes))
                or len(codes) != status_counts.get(status)
                or any(re.fullmatch(r"[036][0-9]{5}", str(code)) is None for code in codes)
            ):
                return False
            missing_codes.extend(str(code) for code in codes)
        complete_set = {str(code) for code in complete_codes}
        missing_set = set(missing_codes)
        if (
            len(missing_codes) != missing
            or len(missing_set) != missing
            or complete_set & missing_set
            or (complete_set | missing_set) & set(excluded)
            or len(complete_set | missing_set) != denominator
        ):
            return False
        population_sets.append(complete_set | missing_set)
    if population_sets[0] != population_sets[1]:
        return False
    eligible_codes = validation.get("eligible_codes")
    if not isinstance(eligible_codes, list):
        return False
    analysis_population = population_sets[0] | {str(code) for code in excluded}
    return all(str(code) in analysis_population for code in eligible_codes)


def _valid_listing_date_evidence(validation: Mapping[str, Any]) -> bool:
    """Validate the schema-7 whole-market listing-date provenance ledger."""
    evidence = validation.get("listing_date_evidence")
    analysis_quotes = validation.get("analysis_market_quotes")
    if not isinstance(evidence, Mapping) or isinstance(analysis_quotes, bool) or not isinstance(analysis_quotes, int):
        return False
    reference_count = evidence.get("reference_count")
    listing_date_count = evidence.get("listing_date_count")
    reference_coverage = _finite_number(evidence.get("reference_coverage"))
    listing_date_coverage = _finite_number(evidence.get("listing_date_coverage"))
    missing_reference_codes = evidence.get("missing_reference_codes")
    missing_listing_date_codes = evidence.get("missing_listing_date_codes")
    status_counts = evidence.get("status_counts")
    retrieved_at_oldest = _finite_number(evidence.get("retrieved_at_oldest"))
    retrieved_at_latest = _finite_number(evidence.get("retrieved_at_latest"))
    if (
        evidence.get("required") is not True
        or analysis_quotes <= 0
        or isinstance(reference_count, bool)
        or not isinstance(reference_count, int)
        or isinstance(listing_date_count, bool)
        or not isinstance(listing_date_count, int)
        or not 0 <= listing_date_count <= reference_count <= analysis_quotes
        or reference_coverage is None
        or listing_date_coverage is None
        or not math.isclose(reference_coverage, reference_count / analysis_quotes, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(listing_date_coverage, listing_date_count / analysis_quotes, rel_tol=0.0, abs_tol=1e-12)
        or reference_coverage < _MIN_RELEASE_LISTING_REFERENCE_COVERAGE
        or listing_date_coverage < _MIN_RELEASE_LISTING_DATE_COVERAGE
        or evidence.get("source") != _LISTING_DATE_SOURCE
        or evidence.get("source_url") != _LISTING_DATE_SOURCE_URL
        or retrieved_at_oldest is None
        or retrieved_at_latest is None
        or retrieved_at_oldest <= 0
        or retrieved_at_latest < retrieved_at_oldest
        or not isinstance(missing_reference_codes, list)
        or not isinstance(missing_listing_date_codes, list)
        or not isinstance(status_counts, Mapping)
    ):
        return False
    missing_sets: list[set[str]] = []
    for raw_codes, expected_count in (
        (missing_reference_codes, analysis_quotes - reference_count),
        (missing_listing_date_codes, reference_count - listing_date_count),
    ):
        normalized = [str(code) for code in raw_codes]
        if (
            normalized != sorted(set(normalized))
            or len(normalized) != expected_count
            or any(re.fullmatch(r"[036][0-9]{5}", code) is None for code in normalized)
        ):
            return False
        missing_sets.append(set(normalized))
    if missing_sets[0] & missing_sets[1]:
        return False
    if (
        any(not isinstance(status, str) or not status.strip() for status in status_counts)
        or any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in status_counts.values())
        or sum(status_counts.values()) != reference_count
        or status_counts.get("reported") != listing_date_count
    ):
        return False
    return True


def _valid_capex_provenance(
    provenance: object,
    *,
    expected_value: float,
    expected_report_date: str,
) -> bool:
    if not isinstance(provenance, Mapping):
        return False
    if (
        provenance.get("schema_version") != _CAPEX_PROVENANCE_SCHEMA_VERSION
        or provenance.get("status") != "complete"
        or provenance.get("report_date") != expected_report_date
        or provenance.get("source_url") != _EASTMONEY_DATACENTER_URL
        or not _close_number(provenance.get("value"), expected_value, abs_tol=0.01)
    ):
        return False
    components = provenance.get("components")
    if provenance.get("evidence_label") == "fact_source_reported":
        return bool(
            provenance.get("source_report") in {_STANDARD_CASHFLOW_REPORT, _DETAILED_CASHFLOW_REPORT}
            and provenance.get("source_field") == "CONSTRUCT_LONG_ASSET"
            and provenance.get("formula") == "source_reported"
            and provenance.get("derivation_method") is None
            and isinstance(components, Mapping)
            and _close_number(components.get("reported_value"), expected_value, abs_tol=0.01)
        )
    if (
        provenance.get("evidence_label") != "derived_calculation"
        or provenance.get("source_report") != _DETAILED_CASHFLOW_REPORT
        or not _close_number(expected_value, 0.0, abs_tol=0.01)
        or not isinstance(components, Mapping)
    ):
        return False
    outflows = components.get("non_capex_outflows")
    if not isinstance(outflows, Mapping) or set(outflows) != _NON_CAPEX_OUTFLOW_FIELDS:
        return False
    values = [_finite_number(outflows.get(field)) for field in _NON_CAPEX_OUTFLOW_FIELDS]
    if any(value is None or value < 0 for value in values):
        return False
    outflow_sum = sum(value for value in values if value is not None)
    method = provenance.get("derivation_method")
    if method == "detailed_outflow_residual_zero":
        total = _finite_number(components.get("total_invest_outflow"))
        declared_sum = _finite_number(components.get("non_capex_outflow_sum"))
        return bool(
            total is not None
            and total >= 0
            and declared_sum is not None
            and _close_number(declared_sum, outflow_sum, abs_tol=0.01)
            and _close_number(total, outflow_sum, abs_tol=0.01)
        )
    if method != "detailed_net_cash_identity_zero":
        return False
    inflow = _finite_number(components.get("total_invest_inflow"))
    other = _finite_number(components.get("invest_netcash_other"))
    balance = _finite_number(components.get("invest_netcash_balance"))
    net = _finite_number(components.get("netcash_invest"))
    solved = _finite_number(components.get("solved_total_invest_outflow"))
    if None in {inflow, other, balance, net, solved}:
        return False
    return _close_number(inflow + other + balance - net, solved, abs_tol=0.01) and _close_number(
        solved,
        0.0,
        abs_tol=0.01,
    )


def _strict_ttm_evidence_value(
    evidence: object,
    *,
    metric: str,
    contract: Mapping[str, str],
) -> float | None:
    """Recompute a complete FY + current-YTD - prior-YTD evidence payload."""
    if not isinstance(evidence, Mapping):
        return None
    expected_formula = _TTM_FCFF_FORMULA_VERSION if metric == "fcff" else _TTM_REVENUE_FORMULA_VERSION
    expected_kind = "cfo_less_capex_proxy" if metric == "fcff" else "reported_revenue"
    expected_keys = {
        "status",
        "value",
        "metric",
        "formula_version",
        "cash_flow_kind",
        "period_basis",
        "period",
        "unit",
        "components",
    }
    expected_period = {
        "basis": _TTM_PERIOD_BASIS,
        "annual_report_date": contract["annual_report_date"],
        "current_interim_report_date": contract["current_interim_report_date"],
        "prior_interim_report_date": contract["prior_interim_report_date"],
    }
    if (
        set(evidence) != expected_keys
        or evidence.get("status") != "complete"
        or evidence.get("metric") != metric
        or evidence.get("formula_version") != expected_formula
        or evidence.get("cash_flow_kind") != expected_kind
        or evidence.get("period_basis") != _TTM_PERIOD_BASIS
        or evidence.get("period") != expected_period
        or evidence.get("unit") != _TTM_SOURCE_UNIT
    ):
        return None
    components = evidence.get("components")
    if not isinstance(components, Mapping):
        return None
    labels = ("annual", "current_interim", "prior_interim")
    dates = {
        "annual": contract["annual_report_date"],
        "current_interim": contract["current_interim_report_date"],
        "prior_interim": contract["prior_interim_report_date"],
    }
    if metric == "revenue":
        if set(components) != {*labels, "reconstructed_revenue"}:
            return None
        values: dict[str, float] = {}
        for label in labels:
            component = components.get(label)
            if not isinstance(component, Mapping) or set(component) != {
                "report_date",
                "row_count",
                "revenue",
                "revenue_source_field",
            }:
                return None
            value = _finite_number(component.get("revenue"))
            if (
                component.get("report_date") != dates[label]
                or component.get("row_count") != 1
                or isinstance(component.get("row_count"), bool)
                or component.get("revenue_source_field") not in _TTM_REVENUE_FIELDS
                or value is None
            ):
                return None
            values[label] = value
        reconstructed = values["annual"] + values["current_interim"] - values["prior_interim"]
        if reconstructed <= 0 or not _close_number(components.get("reconstructed_revenue"), reconstructed):
            return None
    else:
        if set(components) != {
            *labels,
            "reconstructed_operating_cash_flow",
            "reconstructed_capex",
            "reconstructed_fcff",
        }:
            return None
        cfo_values: dict[str, float] = {}
        capex_values: dict[str, float] = {}
        for label in labels:
            component = components.get(label)
            if not isinstance(component, Mapping) or set(component) != {
                "report_date",
                "row_count",
                "operating_cash_flow",
                "operating_cash_flow_source_field",
                "capex_raw",
                "capex_absolute",
                "capex_source_field",
                "capex_provenance",
                "capex_provenance_status",
            }:
                return None
            cfo = _finite_number(component.get("operating_cash_flow"))
            capex_raw = _finite_number(component.get("capex_raw"))
            capex_absolute = _finite_number(component.get("capex_absolute"))
            if (
                component.get("report_date") != dates[label]
                or component.get("row_count") != 1
                or isinstance(component.get("row_count"), bool)
                or component.get("operating_cash_flow_source_field") not in _TTM_OPERATING_CASH_FIELDS
                or component.get("capex_source_field") not in _TTM_CAPEX_FIELDS
                or cfo is None
                or capex_raw is None
                or capex_absolute is None
                or not _close_number(capex_absolute, abs(capex_raw))
                or component.get("capex_provenance_status") != "complete"
                or not _valid_capex_provenance(
                    component.get("capex_provenance"),
                    expected_value=capex_raw,
                    expected_report_date=dates[label],
                )
            ):
                return None
            cfo_values[label] = cfo
            capex_values[label] = capex_absolute
        reconstructed_cfo = cfo_values["annual"] + cfo_values["current_interim"] - cfo_values["prior_interim"]
        reconstructed_capex = capex_values["annual"] + capex_values["current_interim"] - capex_values["prior_interim"]
        reconstructed = reconstructed_cfo - reconstructed_capex
        if (
            reconstructed_capex < 0
            or reconstructed <= 0
            or not _close_number(components.get("reconstructed_operating_cash_flow"), reconstructed_cfo)
            or not _close_number(components.get("reconstructed_capex"), reconstructed_capex)
            or not _close_number(components.get("reconstructed_fcff"), reconstructed)
        ):
            return None
    return reconstructed if _close_number(evidence.get("value"), reconstructed) else None


def _valid_strict_ttm_valuation(
    result: Mapping[str, Any],
    contract: Mapping[str, str],
) -> bool:
    """Bind non-financial DCF inputs to strict TTM evidence and adjustment lineage."""
    if (
        result.get("valuation_input_basis") != "strict_ttm"
        or result.get("base_revenue_basis") != "strict_ttm_reported_revenue"
        or result.get("base_fcf_basis") != "normalised_two_annual_plus_ttm_cfo_less_capex_proxy"
        or result.get("fcf_normalisation_period_basis") != "two_annual_plus_strict_ttm"
    ):
        return False
    ttm_fcff = _strict_ttm_evidence_value(result.get("ttm_fcff_evidence"), metric="fcff", contract=contract)
    ttm_revenue = _strict_ttm_evidence_value(
        result.get("ttm_revenue_evidence"),
        metric="revenue",
        contract=contract,
    )
    if ttm_fcff is None or ttm_revenue is None or not _close_number(result.get("base_revenue"), ttm_revenue):
        return False

    recent_raw = result.get("recent_fcff")
    if not isinstance(recent_raw, list) or len(recent_raw) != 3:
        return False
    recent = tuple(_finite_number(value) for value in recent_raw)
    if any(value is None for value in recent):
        return False
    prior_fcff, annual_fcff, latest_fcff = recent
    fcff_components = result["ttm_fcff_evidence"]["components"]
    annual_component = fcff_components["annual"]
    expected_annual_fcff = annual_component["operating_cash_flow"] - annual_component["capex_absolute"]
    if (
        not _close_number(annual_fcff, expected_annual_fcff)
        or not _close_number(latest_fcff, ttm_fcff)
        or not _close_number(result.get("latest_fcff"), ttm_fcff)
    ):
        return False

    annual_year = int(contract["annual_report_date"][:4])
    periods = [
        {"kind": "annual", "report_date": f"{annual_year - 1}-12-31"},
        {"kind": "annual", "report_date": contract["annual_report_date"]},
        {"kind": "ttm", "through_report_date": contract["current_interim_report_date"]},
    ]
    if result.get("recent_fcff_periods") != periods:
        return False
    if latest_fcff <= 0:
        return False
    if annual_fcff <= prior_fcff and latest_fcff <= annual_fcff:
        normalised, basis = latest_fcff, "latest_persistent_decline"
    else:
        median = sorted((prior_fcff, annual_fcff, latest_fcff))[1]
        premium_limit = latest_fcff * _FCF_NORMALISATION_PREMIUM_CAP
        normalised = min(median, premium_limit)
        basis = "recent_median" if median <= premium_limit else "latest_premium_cap"
    if normalised <= 0 or result.get("fcf_normalisation_basis") != basis:
        return False
    expected_period_detail = {
        "period_set": "two_annual_plus_strict_ttm",
        "periods": periods,
        "normalisation_method": basis,
        "cash_flow_kind": "cfo_less_capex_proxy",
        "formula_version": _TTM_FCFF_FORMULA_VERSION,
    }
    if result.get("fcf_normalisation_period") != expected_period_detail or not _close_number(
        result.get("normalisation_premium_cap"),
        _FCF_NORMALISATION_PREMIUM_CAP,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        return False

    adjustments = result.get("base_fcf_adjustments")
    margin_ceiling = _finite_number(result.get("fcf_margin_ceiling"))
    if not isinstance(adjustments, list) or margin_ceiling is None or not 0 < margin_ceiling <= 1:
        return False
    allowed_order = {"mixed_profit_cycle_p25_cap": 0, "fcf_margin_ceiling": 1}
    seen: list[str] = []
    current = normalised
    for adjustment in adjustments:
        if not isinstance(adjustment, Mapping) or set(adjustment) != {"kind", "before", "limit", "after"}:
            return False
        kind = adjustment.get("kind")
        before = _finite_number(adjustment.get("before"))
        limit = _finite_number(adjustment.get("limit"))
        after = _finite_number(adjustment.get("after"))
        if (
            kind not in allowed_order
            or kind in seen
            or (seen and allowed_order[kind] <= allowed_order[seen[-1]])
            or before is None
            or limit is None
            or after is None
            or limit <= 0
            or not _close_number(before, current)
            or before <= limit
            or not _close_number(after, min(before, limit))
        ):
            return False
        if kind == "fcf_margin_ceiling" and not _close_number(limit, ttm_revenue * margin_ceiling):
            return False
        seen.append(str(kind))
        current = after
    margin_limit = ttm_revenue * margin_ceiling
    if "fcf_margin_ceiling" not in seen and current > margin_limit and not _close_number(current, margin_limit):
        return False
    return current > 0 and _close_number(result.get("base_fcf"), current)


def _independent_dcf_value(
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
    """Independent five-year two-stage FCFF calculation for the release gate."""
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
        max(_DCF_MARGIN_FLOOR, _DCF_MARGIN_LONG_TERM, current_margin * retention),
    )
    explicit_pv = 0.0
    final_revenue = 0.0
    final_discount = 0.0
    denominator = max(_DCF_FORECAST_YEARS - 1, 1)
    for year in range(1, _DCF_FORECAST_YEARS + 1):
        revenue = base_revenue * (1 + growth) ** year
        margin = current_margin + (target_margin - current_margin) * ((year - 1) / denominator)
        discount = (1 + wacc) ** year
        if discount <= 0 or not all(math.isfinite(value) for value in (revenue, margin, discount)):
            return None
        explicit_pv += revenue * margin / discount
        final_revenue, final_discount = revenue, discount
    terminal_fcf = final_revenue * (1 + terminal_growth) * target_margin
    terminal_value = terminal_fcf / (wacc - terminal_growth)
    per_share = (explicit_pv + terminal_value / final_discount - net_debt) / shares
    return per_share if math.isfinite(per_share) and per_share > 0 else None


def _valid_valuation_result(
    code: str,
    result: object,
    company: Mapping[str, Any],
    *,
    reporting_period_contract: Mapping[str, str] | None,
    excluded_financial_codes: set[str],
) -> bool:
    if not isinstance(result, Mapping):
        return False
    current_price = _finite_number(result.get("current_price"))
    buy_boundary = _finite_number(result.get("buy_zone_upper"))
    sell_boundary = _finite_number(result.get("sell_zone_lower"))
    base_wacc = _finite_number(result.get("base_wacc"))
    shares = _finite_number(result.get("shares_outstanding"))
    market_cap = _finite_number(company.get("market_cap"))
    is_pb = result.get("_pb_valuation") is True
    expected_tax_source = "financial_operating_liabilities_excluded" if is_pb else "taxable_profit_evidence_unavailable"
    wacc_components = result.get("wacc_components")
    if (
        str(result.get("code")) != code
        or result.get("name") != company.get("name")
        or result.get("industry_code") != company.get("industry")
        or current_price != _finite_number(company.get("price"))
        or current_price is None
        or current_price <= 0
        or buy_boundary is None
        or sell_boundary is None
        or buy_boundary <= 0
        or buy_boundary > sell_boundary
        or base_wacc is None
        or not 0 < base_wacc < 1
        or shares is None
        or shares <= 0
        or market_cap is None
        or market_cap <= 0
        or not math.isclose(
            shares,
            market_cap / current_price,
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
        or _finite_number(result.get("tax_shield_rate")) != 0.0
        or result.get("tax_shield_source") != expected_tax_source
        or not isinstance(wacc_components, Mapping)
        or _finite_number(wacc_components.get("tax_shield_rate")) != 0.0
        or is_pb != (code in excluded_financial_codes)
    ):
        return False
    equity_weight = _finite_number(wacc_components.get("equity_weight"))
    debt_weight = _finite_number(wacc_components.get("debt_weight"))
    cost_of_equity = _finite_number(wacc_components.get("cost_of_equity"))
    debt_cost = _finite_number(wacc_components.get("pre_tax_cost_of_debt"))
    if (
        equity_weight is None
        or debt_weight is None
        or cost_of_equity is None
        or not 0 <= equity_weight <= 1
        or not 0 <= debt_weight <= 1
        or not math.isclose(equity_weight + debt_weight, 1.0, rel_tol=0.0, abs_tol=1e-9)
        or cost_of_equity <= 0
        or (debt_weight > 0 and (debt_cost is None or debt_cost < 0))
    ):
        return False
    reconstructed_wacc = equity_weight * cost_of_equity + debt_weight * (debt_cost or 0.0)
    if not math.isclose(base_wacc, round(reconstructed_wacc, 4), rel_tol=0.0, abs_tol=5.1e-5):
        return False
    expected_zone = (
        "买入区" if current_price <= buy_boundary else "卖出区" if current_price >= sell_boundary else "观察区"
    )
    if result.get("zone") != expected_zone:
        return False

    points = result.get("dcf_points")
    params = result.get("params")
    scenarios = ("pessimistic", "neutral", "optimistic")
    if (
        not isinstance(points, Mapping)
        or set(points) != set(scenarios)
        or not isinstance(params, Mapping)
        or set(params) != set(scenarios)
    ):
        return False
    bands: list[tuple[float, float]] = []
    parsed_parameters: dict[str, Mapping[str, Any]] = {}
    for scenario in scenarios:
        band = points.get(scenario)
        parameters = params.get(scenario)
        if not isinstance(band, Mapping) or set(band) != {"lower", "upper"} or not isinstance(parameters, Mapping):
            return False
        lower = _finite_number(band.get("lower"))
        upper = _finite_number(band.get("upper"))
        if lower is None or upper is None or lower <= 0 or lower > upper:
            return False
        if any(_finite_number(parameters.get(field)) is None for field in ("growth", "wacc_base", "terminal_g")):
            return False
        if (is_pb and parameters.get("margin_retention") is not None) or (
            not is_pb and _finite_number(parameters.get("margin_retention")) is None
        ):
            return False
        bands.append((lower, upper))
        parsed_parameters[scenario] = parameters
    if is_pb:
        midpoints = [(lower + upper) / 2 for lower, upper in bands]
        if (
            not midpoints[0] <= midpoints[1] <= midpoints[2]
            or result.get("base_fcf") is not None
            or result.get("base_revenue") is not None
            or result.get("net_debt") is not None
        ):
            return False
        for scenario, (lower, upper) in zip(scenarios, bands):
            parameters = parsed_parameters[scenario]
            growth = _finite_number(parameters.get("growth"))
            cost = _finite_number(parameters.get("cost_of_equity"))
            wacc_center = _finite_number(parameters.get("wacc_base"))
            scenario_roe = _finite_number(parameters.get("scenario_roe"))
            normalised_roe = _finite_number(parameters.get("normalised_roe"))
            bvps = _finite_number(parameters.get("bvps"))
            pb_lower = _finite_number(parameters.get("pb_lower"))
            pb_upper = _finite_number(parameters.get("pb_upper"))
            if (
                None in (growth, cost, wacc_center, scenario_roe, normalised_roe, bvps, pb_lower, pb_upper)
                or cost <= growth
                or cost - _DCF_BAND_WACC_DELTA <= growth
                or scenario_roe <= growth
                or bvps <= 0
                or parameters.get("formula") != "(normalised_roe - g) / (cost_of_equity - g)"
                or not _close_number(wacc_center, cost)
                or not _close_number(cost, reconstructed_wacc)
                or not _close_number(parameters.get("terminal_g"), growth)
                or not _close_number(parameters.get("normalised_roe"), result.get("normalised_roe"))
            ):
                return False
            expected_pb_lower = (scenario_roe - growth) / (cost + _DCF_BAND_WACC_DELTA - growth)
            expected_pb_upper = (scenario_roe - growth) / (cost - _DCF_BAND_WACC_DELTA - growth)
            if (
                not _close_number(pb_lower, expected_pb_lower)
                or not _close_number(pb_upper, expected_pb_upper)
                or not _close_number(lower, bvps * expected_pb_lower)
                or not _close_number(upper, bvps * expected_pb_upper)
            ):
                return False
    else:
        flattened = [value for band in bands for value in band]
        if flattened != sorted(flattened) or (_finite_number(result.get("base_fcf")) or 0) <= 0:
            return False
        if (_finite_number(result.get("base_revenue")) or 0) <= 0:
            return False
        if reporting_period_contract is None or not _valid_strict_ttm_valuation(
            result,
            reporting_period_contract,
        ):
            return False
        base_fcf = _finite_number(result.get("base_fcf"))
        base_revenue = _finite_number(result.get("base_revenue"))
        net_debt = _finite_number(result.get("net_debt"))
        if (
            base_fcf is None
            or base_revenue is None
            or net_debt is None
            or result.get("explicit_forecast_years") != _DCF_FORECAST_YEARS
        ):
            return False
        for scenario, (lower, upper) in zip(scenarios, bands):
            parameters = parsed_parameters[scenario]
            growth = _finite_number(parameters.get("growth"))
            wacc_center = _finite_number(parameters.get("wacc_base"))
            terminal_growth = _finite_number(parameters.get("terminal_g"))
            retention = _finite_number(parameters.get("margin_retention"))
            if (
                None in (growth, wacc_center, terminal_growth, retention)
                or parameters.get("forecast_years") != _DCF_FORECAST_YEARS
                or not _close_number(
                    wacc_center,
                    reconstructed_wacc + _DCF_WACC_SHIFT[scenario],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                return False
            expected_lower = _independent_dcf_value(
                base_fcf=base_fcf,
                base_revenue=base_revenue,
                growth=growth,
                wacc=wacc_center + _DCF_BAND_WACC_DELTA,
                terminal_growth=terminal_growth,
                shares=shares,
                net_debt=net_debt,
                retention=retention,
            )
            expected_upper = _independent_dcf_value(
                base_fcf=base_fcf,
                base_revenue=base_revenue,
                growth=growth,
                wacc=wacc_center - _DCF_BAND_WACC_DELTA,
                terminal_growth=terminal_growth,
                shares=shares,
                net_debt=net_debt,
                retention=retention,
            )
            if (
                expected_lower is None
                or expected_upper is None
                or not _close_number(lower, expected_lower)
                or not _close_number(upper, expected_upper)
            ):
                return False
    expected_buy = (bands[0][1] + bands[1][0]) / 2
    expected_sell = (bands[1][1] + bands[2][0]) / 2
    expected_center = (bands[1][0] + bands[1][1]) / 2
    return (
        math.isclose(buy_boundary, expected_buy, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(sell_boundary, expected_sell, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(_finite_number(result.get("mean1")) or -1, expected_buy, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(_finite_number(result.get("mean2")) or -1, expected_sell, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(_finite_number(result.get("dcf_value_mean")) or -1, expected_buy, rel_tol=1e-9, abs_tol=1e-9)
        and result.get("dcf_value_mean_legacy_alias_of") == "buy_zone_upper"
        and math.isclose(
            _finite_number(result.get("valuation_center")) or -1,
            expected_center,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        and math.isclose(
            _finite_number(result.get("neutral_value_midpoint")) or -1,
            expected_center,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    )


def _parse_aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _git_tree_entries(raw_tree: bytes) -> dict[str, tuple[str, str, str]]:
    entries: dict[str, tuple[str, str, str]] = {}
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        if not separator:
            raise ValueError("invalid Git tree entry")
        mode, object_type, object_id = metadata.decode("ascii").split()
        relative = raw_path.decode("utf-8")
        path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or relative.startswith("/")
            or ".." in path.parts
            or any(
                not part
                or unicodedata.normalize("NFC", part) != part
                or part.endswith((" ", "."))
                or any(character in '<>:"|?*' for character in part)
                or any(ord(character) < 32 for character in part)
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
                for part in path.parts
            )
        ):
            raise ValueError(f"release Git tree contains an unsafe path: {relative!r}")
        entries[relative] = (mode, object_type, object_id)
    return entries


def _git_release_provenance_errors(
    commit: str,
    repository: str | Path,
    archive: ZipFile,
    file_names: Mapping[str, str],
) -> list[str]:
    repo = Path(repository).resolve()
    git_executable = shutil.which("git")
    if not git_executable:
        return ["Git executable is unavailable for release provenance verification"]

    def run(*arguments: str) -> str:
        # ``git_executable`` is an absolute path and every argument is fixed.
        return subprocess.run(  # nosec B603
            [git_executable, *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    def run_bytes(*arguments: str) -> bytes:
        return subprocess.run(  # nosec B603
            [git_executable, *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout

    try:
        if run("rev-parse", "--is-inside-work-tree") != "true":
            return ["release verification repository is not a Git work tree"]
        if run("status", "--porcelain"):
            return ["release verification repository is not clean"]
        run("cat-file", "-e", f"{commit}^{{commit}}")
        subprocess.run(  # nosec B603
            [git_executable, "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        changed = {
            line.replace("\\", "/")
            for line in run("diff", "--name-only", f"{commit}..HEAD", "--").splitlines()
            if line.strip()
        }
        descendant_count = int(run("rev-list", "--count", f"{commit}..HEAD"))
        raw_tree = run_bytes(
            "-c",
            "core.quotepath=false",
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "HEAD",
        )
        tree_entries = _git_tree_entries(raw_tree)
        tracked = set(tree_entries)
    except ValueError as exc:
        return [str(exc)]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ["audit Git commit cannot be verified as an ancestor in the clean release repository"]
    errors: list[str] = []
    if descendant_count != 1:
        errors.append("the audit Git commit must have exactly one release descendant")
    if _AUDIT_JSON_PATH not in changed or not changed.issubset(_RELEASE_AUDIT_PATHS):
        errors.append("changes after the audit Git commit are not limited to release audit artifacts with updated JSON")
    if tracked != set(file_names):
        errors.append("release ZIP file set does not exactly match the clean Git HEAD tree")
        return errors
    for relative in sorted(tracked):
        mode, object_type, object_id = tree_entries[relative]
        if mode != "100644" or object_type != "blob":
            errors.append(f"release Git tree contains an unsupported entry: {relative}")
            continue
        try:
            raw_attributes = run_bytes(
                "check-attr",
                "-z",
                "--source=HEAD",
                "text",
                "eol",
                "filter",
                "ident",
                "working-tree-encoding",
                "--",
                relative,
            )
            attribute_parts = raw_attributes.decode("utf-8").split("\0")
            if attribute_parts and attribute_parts[-1] == "":
                attribute_parts.pop()
            if len(attribute_parts) % 3:
                raise ValueError("invalid Git attribute response")
            attributes = {
                attribute_parts[index + 1]: attribute_parts[index + 2] for index in range(0, len(attribute_parts), 3)
            }
            source_bytes = run_bytes("cat-file", "blob", object_id)
            archive_bytes = archive.read(file_names[relative])
        except (KeyError, OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            errors.append(f"release ZIP cannot be byte-bound to Git HEAD: {relative}")
            continue
        unsupported_attributes = {
            name: value
            for name, value in attributes.items()
            if name in {"filter", "ident", "working-tree-encoding"} and value not in {"unspecified", "unset"}
        }
        if unsupported_attributes:
            errors.append(f"release Git tree uses unsupported checkout filters: {relative}")
            continue
        text_attribute = attributes.get("text", "unspecified")
        eol_attribute = attributes.get("eol", "unspecified")
        if text_attribute == "unset":
            if eol_attribute not in {"unspecified", "unset"}:
                errors.append(f"release binary file has a conflicting eol policy: {relative}")
                continue
        elif text_attribute != "set" or eol_attribute not in {"lf", "crlf"}:
            errors.append(f"release text file lacks a deterministic eol policy: {relative}")
            continue
        elif eol_attribute == "crlf":
            source_bytes = source_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        else:
            source_bytes = source_bytes.replace(b"\r\n", b"\n")
        if source_bytes != archive_bytes:
            errors.append(f"release ZIP content differs from clean Git HEAD: {relative}")
    return errors


def verify_release_zip(path: str, *, repository: str | Path | None = ".") -> tuple[str, ...]:
    errors: list[str] = []
    try:
        with ZipFile(path) as archive:
            for entry in archive.infolist():
                raw_name = entry.filename
                portable = raw_name.replace("\\", "/")
                raw_parts = PurePosixPath(portable).parts
                unix_mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if (
                    "\\" in raw_name
                    or portable.startswith("/")
                    or re.match(r"^[A-Za-z]:/", portable)
                    or ".." in raw_parts
                    or stat.S_ISLNK(unix_mode)
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    errors.append(f"unsafe archive path: {raw_name}")
            try:
                file_names = _normalised_file_names(archive.namelist())
            except ValueError as exc:
                return (str(exc),)
            for normalised, archive_name in file_names.items():
                parts = PurePosixPath(normalised).parts
                if not normalised or normalised.startswith("/") or ".." in parts:
                    errors.append(f"unsafe archive path: {archive_name}")
                    continue
                lower = normalised.lower()
                if _FORBIDDEN_PATH.search(normalised) or lower.endswith(_FORBIDDEN_SUFFIXES):
                    errors.append(f"forbidden release artifact: {normalised}")
                if lower.startswith("data/cache/") and lower != "data/cache/.gitkeep":
                    errors.append(f"runtime cache in release: {normalised}")
                basename = parts[-1].lower()
                if (
                    basename in {"coverage.xml", ".coverage", "cache.db", "secrets.toml"}
                    or basename.startswith(".coverage.")
                    or basename in _SSH_PRIVATE_KEY_NAMES
                ):
                    errors.append(f"runtime or secret file in release: {normalised}")
                if basename == ".env" or (
                    basename.startswith(".env.") and not basename.endswith((".example", ".sample", ".template"))
                ):
                    errors.append(f"environment secret file in release: {normalised}")
                if PurePosixPath(normalised).suffix.lower() in _SECRET_SCAN_SUFFIXES:
                    content = archive.read(archive_name)
                    if len(content) <= 10_000_000 and _SECRET_ASSIGNMENT.search(content):
                        errors.append(f"probable embedded credential in release: {normalised}")

            missing = sorted(_REQUIRED_FILES - set(file_names))
            if missing:
                errors.append(f"required release files are missing: {missing}")
            license_name = file_names.get("LICENSE")
            if (
                license_name is not None
                and hashlib.sha256(archive.read(license_name)).hexdigest() != _EXPECTED_LICENSE_SHA256
            ):
                errors.append("LICENSE does not match the approved PolyForm Noncommercial 1.0.0 terms")

            run_name = file_names.get("run.bat")
            if run_name is not None:
                run_bytes = archive.read(run_name)
                without_crlf = run_bytes.replace(b"\r\n", b"")
                if b"\n" in without_crlf or b"\r" in without_crlf or b"\r\n" not in run_bytes:
                    errors.append("run.bat is not consistently CRLF")
                errors.extend(_desktop_launcher_errors(run_bytes))

            for normalised, archive_name in file_names.items():
                if normalised.lower().endswith(_LF_SUFFIXES) or PurePosixPath(normalised).name.lower() in _LF_NAMES:
                    content = archive.read(archive_name)
                    if b"\r\n" in content or b"\r" in content:
                        errors.append(f"text file is not canonical LF: {normalised}")

            update_config_name = file_names.get("desktop/update_config.json")
            if update_config_name is not None:
                try:
                    update_config = _strict_json_loads(archive.read(update_config_name))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"desktop update configuration is unreadable: {exc}")
                except OSError as exc:
                    errors.append(f"desktop update configuration cannot be read: {exc}")
                else:
                    if update_config != {"manifest_url": _EXPECTED_UPDATE_MANIFEST_URL}:
                        errors.append("desktop update configuration does not use the official release manifest")

            audit_name = file_names.get(_AUDIT_JSON_PATH)
            if audit_name is not None:
                try:
                    payload = _strict_json_loads(archive.read(audit_name))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"audit JSON is unreadable: {exc}")
                except OSError as exc:
                    errors.append(f"audit JSON cannot be read: {exc}")
                else:
                    if not isinstance(payload, Mapping):
                        errors.append("audit JSON root is not an object")
                        payload = {}
                    provenance = payload.get("provenance", {})
                    provenance = provenance if isinstance(provenance, Mapping) else {}
                    caller = provenance.get("caller_metadata", {})
                    caller = caller if isinstance(caller, Mapping) else {}
                    git_state = provenance.get("git", {})
                    git_state = git_state if isinstance(git_state, Mapping) else {}
                    if provenance.get("audit_schema_version") != 3:
                        errors.append("audit schema version is not 3")
                    if provenance.get("patch6_source") != _EXPECTED_PATCH6_SOURCE:
                        errors.append("audit is not bound to the authoritative Patch 6 source hash")
                    if provenance.get("type7_source_documents") != _EXPECTED_TYPE7_SOURCE_DOCUMENTS:
                        errors.append("audit is not bound to all authoritative Type 7 source hashes")
                    if caller.get("snapshot_schema_version") != 7:
                        errors.append("snapshot schema version is not 7")
                    validation = caller.get("validation", {})
                    validation = validation if isinstance(validation, Mapping) else {}
                    reporting_period_contract = _valid_reporting_period_contract(
                        validation.get("reporting_period_contract")
                    )
                    if reporting_period_contract is None:
                        errors.append("snapshot reporting period contract is missing or invalid")
                    elif provenance.get("reporting_period_contract") != reporting_period_contract:
                        errors.append("audit reporting period contract differs from the schema-7 snapshot contract")
                    if not _valid_listing_date_evidence(validation):
                        errors.append("snapshot listing-date provenance ledger is missing or invalid")
                    strict_ttm_coverage_valid = _valid_strict_ttm_source_coverage(validation)
                    if not strict_ttm_coverage_valid:
                        errors.append("snapshot strict TTM source coverage ledger is missing or invalid")
                    coverage = validation.get("strict_ttm_source_coverage")
                    excluded_financial_codes = (
                        {str(code) for code in coverage.get("excluded_financial_codes", [])}
                        if strict_ttm_coverage_valid and isinstance(coverage, Mapping)
                        else set()
                    )
                    audit_commit = str(git_state.get("commit", ""))
                    if git_state.get("dirty") is not False or re.fullmatch(r"[0-9a-fA-F]{40}", audit_commit) is None:
                        errors.append("audit was not generated from a clean identified Git commit")
                    elif repository is not None:
                        errors.extend(_git_release_provenance_errors(audit_commit, repository, archive, file_names))
                    if payload.get("seed") != _EXPECTED_AUDIT_SEED:
                        errors.append(f"audit seed is not {_EXPECTED_AUDIT_SEED}")
                    raw_sample_codes = payload.get("sample_codes", [])
                    if (
                        payload.get("sample_size") != 100
                        or not isinstance(raw_sample_codes, list)
                        or len(raw_sample_codes) != 100
                    ):
                        errors.append("audit does not contain the required random sample of 100")
                    sample_codes = (
                        [str(code) for code in raw_sample_codes] if isinstance(raw_sample_codes, list) else []
                    )
                    if len(set(sample_codes)) != len(sample_codes) or any(
                        not re.fullmatch(r"[036][0-9]{5}", code) for code in sample_codes
                    ):
                        errors.append("audit sample is not a unique SH/SZ code set")
                    for field in (
                        "engine_self_check_errors",
                        "same_source_scoring_replay_errors",
                        "same_source_valuation_replay_errors",
                        "independent_check_errors",
                        "invariant_errors",
                        "pipeline_issues",
                    ):
                        if payload.get(field) != []:
                            errors.append(f"audit field {field} is not empty")
                    company_codes = _audit_company_codes(payload)
                    if company_codes is None:
                        errors.append("audit does not contain 100 complete company rows")
                    elif company_codes != sample_codes:
                        errors.append("audit company identities do not exactly match the ordered SH/SZ sample")
                    companies = payload.get("companies")
                    company_by_code = (
                        {str(company.get("code")): company for company in companies if isinstance(company, Mapping)}
                        if isinstance(companies, list)
                        else {}
                    )
                    dcf_results = payload.get("dcf_results")
                    dcf_skip_reasons = payload.get("dcf_skip_reasons")
                    if not isinstance(dcf_results, Mapping) or not isinstance(dcf_skip_reasons, Mapping):
                        errors.append("audit valuation results and skip reasons are missing")
                    else:
                        result_codes = {str(code) for code in dcf_results}
                        skip_codes = {str(code) for code in dcf_skip_reasons}
                        valuation_partition_valid = (
                            not (result_codes & skip_codes)
                            and result_codes | skip_codes == set(sample_codes)
                            and payload.get("dcf_valid") == len(result_codes)
                            and all(
                                code in company_by_code
                                and _valid_valuation_result(
                                    code,
                                    result,
                                    company_by_code[code],
                                    reporting_period_contract=reporting_period_contract,
                                    excluded_financial_codes=excluded_financial_codes,
                                )
                                for code, result in dcf_results.items()
                            )
                            and all(str(reason or "").strip() for reason in dcf_skip_reasons.values())
                        )
                        if not valuation_partition_valid:
                            errors.append(
                                "audit does not provide one complete valuation result or skip reason per company"
                            )

                    eligible_codes_raw = validation.get("eligible_codes")
                    eligible_codes = (
                        [str(code) for code in eligible_codes_raw] if isinstance(eligible_codes_raw, list) else []
                    )
                    unsupported_codes_raw = validation.get("unsupported_market_codes")
                    unsupported_codes = (
                        {str(code) for code in unsupported_codes_raw}
                        if isinstance(unsupported_codes_raw, list)
                        else set()
                    )
                    market_counts = validation.get("market_counts")
                    market_counts = market_counts if isinstance(market_counts, Mapping) else {}
                    analysis_market_quotes = validation.get("analysis_market_quotes")
                    analysis_eligible_coverage = _finite_number(validation.get("analysis_eligible_coverage"))
                    eligible_universe_size = payload.get("eligible_universe_size")
                    eligible_identity_valid = (
                        validation.get("analysis_markets") == ["SH", "SZ"]
                        and isinstance(eligible_universe_size, int)
                        and eligible_universe_size >= _MIN_RELEASE_ELIGIBLE_COMPANIES
                        and validation.get("eligible_companies") == eligible_universe_size
                        and len(eligible_codes) == eligible_universe_size
                        and eligible_codes == sorted(set(eligible_codes))
                        and all(re.fullmatch(r"[036][0-9]{5}", code) for code in eligible_codes)
                        and isinstance(unsupported_codes_raw, list)
                        and all(re.fullmatch(r"[89][0-9]{5}", code) for code in unsupported_codes)
                        and not (set(eligible_codes) & unsupported_codes)
                        and all(
                            isinstance(market_counts.get(market), int) and market_counts.get(market) >= minimum
                            for market, minimum in _MIN_RELEASE_MARKET_COUNTS.items()
                        )
                        and set(market_counts).issubset({"SH", "SZ", "BJ"})
                        and isinstance(market_counts.get("BJ", 0), int)
                        and market_counts.get("BJ", 0) >= 0
                        and analysis_market_quotes == market_counts.get("SH", 0) + market_counts.get("SZ", 0)
                        and validation.get("quotes")
                        == sum(int(market_counts.get(market, 0)) for market in ("SH", "SZ", "BJ"))
                        and analysis_eligible_coverage is not None
                        and math.isclose(
                            analysis_eligible_coverage,
                            eligible_universe_size / analysis_market_quotes,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                        and analysis_eligible_coverage >= 0.80
                        and len(unsupported_codes) == market_counts.get("BJ")
                        and provenance.get("eligible_universe_sha256")
                        == hashlib.sha256("\n".join(eligible_codes).encode("ascii")).hexdigest()
                    )
                    if not eligible_identity_valid:
                        errors.append(
                            "audit does not bind the complete analysis universe to unique SH/SZ eligible codes"
                        )
                    # The release contract checks one reproducible public sample;
                    # cryptographic unpredictability is neither required nor wanted.
                    elif sample_codes != sorted(
                        random.Random(_EXPECTED_AUDIT_SEED).sample(eligible_codes, 100)  # nosec B311
                    ):
                        errors.append(
                            "audit sample does not match the fixed-seed draw from the complete eligible universe"
                        )

                    generated_at = _parse_aware_utc(provenance.get("generated_at_utc"))
                    data_timestamp = _parse_aware_utc(payload.get("data_timestamp_utc"))
                    snapshot_hashes = (
                        str(provenance.get("snapshot_content_sha256", "")).lower(),
                        str(provenance.get("snapshot_artifact_sha256", "")).lower(),
                        str(caller.get("snapshot_payload_sha256", "")).lower(),
                    )
                    snapshot_identity_valid = (
                        all(re.fullmatch(r"[0-9a-f]{64}", value) is not None for value in snapshot_hashes)
                        and len(set(snapshot_hashes)) == len(snapshot_hashes)
                        and all(len(set(value)) >= 8 for value in snapshot_hashes)
                        and isinstance(caller.get("snapshot_artifact_bytes"), int)
                        and caller.get("snapshot_artifact_bytes") > 0
                        and caller.get("snapshot_source") in {"network", "cache"}
                        and generated_at is not None
                        and data_timestamp is not None
                        and data_timestamp <= generated_at
                    )
                    if not snapshot_identity_valid:
                        errors.append("audit is not bound to an identified, time-consistent market snapshot")

                    companion_hashes = payload.get("companion_artifacts_sha256", {})
                    companion_hashes = companion_hashes if isinstance(companion_hashes, Mapping) else {}
                    companion_contents: dict[str, bytes] = {}
                    for kind, companion_path in (("csv", _AUDIT_CSV_PATH), ("markdown", _AUDIT_MARKDOWN_PATH)):
                        companion_name = file_names.get(companion_path)
                        if companion_name is None:
                            continue
                        content = archive.read(companion_name)
                        companion_contents[kind] = content
                        expected = companion_hashes.get(kind)
                        if re.fullmatch(r"[0-9a-f]{64}", str(expected)) is None or expected != _sha256_bytes(content):
                            errors.append(f"audit {kind} companion does not match its JSON manifest hash")
                    csv_rows = _audit_csv_rows(companion_contents.get("csv", b""))
                    if (
                        csv_rows is None
                        or [row.get("代码") for row in csv_rows] != sample_codes
                        or not isinstance(companies, list)
                        or not isinstance(dcf_results, Mapping)
                        or not isinstance(dcf_skip_reasons, Mapping)
                        or not _audit_csv_matches_payload(csv_rows, companies, dcf_results, dcf_skip_reasons)
                    ):
                        errors.append("audit CSV does not semantically match all 100 JSON company rows")
                    try:
                        markdown_text = companion_contents.get("markdown", b"").decode("utf-8")
                    except UnicodeDecodeError:
                        markdown_text = ""
                    if (
                        not isinstance(companies, list)
                        or company_codes is None
                        or not isinstance(dcf_results, Mapping)
                        or not isinstance(dcf_skip_reasons, Mapping)
                        or not _audit_markdown_matches_payload(
                            markdown_text,
                            payload,
                            companies,
                            dcf_results,
                            dcf_skip_reasons,
                        )
                    ):
                        errors.append("audit Markdown does not semantically match all 100 JSON company rows")

                    analysis_quality = payload.get("analysis_quality", {})
                    full_market_quality = caller.get("full_market_quality", {})
                    if not isinstance(analysis_quality, Mapping) or not isinstance(full_market_quality, Mapping):
                        errors.append("audit full-market quality metadata is missing")
                    else:
                        if dict(analysis_quality) != dict(full_market_quality):
                            errors.append("audit full-market quality copies are inconsistent")
                        dcf_valid = analysis_quality.get("dcf_valid")
                        dcf_skipped = analysis_quality.get("dcf_skipped")
                        strict_quality = (
                            isinstance(eligible_universe_size, int)
                            and eligible_universe_size >= _MIN_RELEASE_ELIGIBLE_COMPANIES
                            and analysis_quality.get("ok") is True
                            and analysis_quality.get("expected_companies") == eligible_universe_size
                            and analysis_quality.get("score_raw_rows") == eligible_universe_size
                            and analysis_quality.get("score_rows") == eligible_universe_size
                            and analysis_quality.get("score_coverage") == 1.0
                            and analysis_quality.get("dcf_attempted") == eligible_universe_size
                            and analysis_quality.get("dcf_attempt_coverage") == 1.0
                            and analysis_quality.get("pipeline_issues") == 0
                            and analysis_quality.get("pipeline_issue_rate") == 0.0
                            and analysis_quality.get("reasons") == []
                            and isinstance(dcf_valid, int)
                            and isinstance(dcf_skipped, int)
                            and dcf_valid + dcf_skipped == eligible_universe_size
                        )
                        if not strict_quality:
                            errors.append("audit does not prove a zero-issue complete full-market analysis")

                    project_name = file_names.get("pyproject.toml")
                    project_table: Mapping[str, Any] = {}
                    if project_name is not None:
                        try:
                            pyproject = tomllib.loads(archive.read(project_name).decode("utf-8"))
                            candidate = pyproject.get("project", {})
                            project_table = candidate if isinstance(candidate, Mapping) else {}
                        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                            errors.append("pyproject.toml is unreadable")
                    if project_table.get("name") != "ds-dcf":
                        errors.append("project name is not ds-dcf")
                    if project_table.get("version") != _EXPECTED_RELEASE_VERSION:
                        errors.append(f"project version is not {_EXPECTED_RELEASE_VERSION}")
                    if project_table.get("license") != _EXPECTED_PROJECT_LICENSE or project_table.get(
                        "license-files"
                    ) != ["LICENSE"]:
                        errors.append("project license is not PolyForm Noncommercial 1.0.0")
                    runtime = provenance.get("runtime", {})
                    runtime = runtime if isinstance(runtime, Mapping) else {}
                    if not _runtime_python_supported(runtime.get("python"), project_table.get("requires-python")):
                        errors.append("audit Python runtime is outside the packaged project support range")
                    direct_dependencies = _exact_project_dependencies(project_table)
                    runtime_packages = runtime.get("packages", {})
                    if (
                        direct_dependencies is None
                        or set(direct_dependencies) != _EXPECTED_DIRECT_DEPENDENCIES
                        or not isinstance(runtime_packages, Mapping)
                    ):
                        errors.append("project or audit runtime dependency metadata is invalid")
                    elif any(
                        str(runtime_packages.get(name, "")) != version for name, version in direct_dependencies.items()
                    ):
                        errors.append("audit runtime direct dependency versions differ from pyproject.toml")
                    lock_maps = {
                        lock_name: _locked_requirements(archive.read(file_names[lock_name]))
                        for lock_name in (
                            "requirements-bootstrap.txt",
                            "requirements-lock.txt",
                            "requirements-dev-lock.txt",
                        )
                        if lock_name in file_names
                    }
                    if len(lock_maps) != 3 or any(value is None for value in lock_maps.values()):
                        errors.append("dependency locks are not complete exact pins with valid SHA256 hashes")
                    elif direct_dependencies is None or any(
                        any(lock_maps[lock].get(name) != version for name, version in direct_dependencies.items())
                        for lock in ("requirements-lock.txt", "requirements-dev-lock.txt")
                    ):
                        errors.append("dependency locks do not contain every exact direct runtime dependency")

                    code_files = {
                        name
                        for name in file_names
                        if name in {"app.py", "config.py"}
                        or (
                            name.endswith(".py")
                            and name.split("/", 1)[0] in {"data", "desktop", "engine", "ui", "tools"}
                        )
                    }
                    expected_hashes = {
                        "code_sha256": _hash_archive_files(archive, file_names, code_files),
                        "rules_sha256": _hash_archive_files(archive, file_names, _RULE_FILES),
                        "industry_sha256": _hash_archive_files(archive, file_names, _INDUSTRY_FILES),
                        "dependency_manifest_sha256": _hash_archive_files(
                            archive,
                            file_names,
                            _DEPENDENCY_FILES,
                        ),
                    }
                    for field, expected in expected_hashes.items():
                        if provenance.get(field) != expected:
                            errors.append(f"audit {field} does not match release contents")
    except (BadZipFile, FileNotFoundError, OSError) as exc:
        return (f"release ZIP cannot be read: {exc}",)
    return tuple(errors)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path")
    parser.add_argument(
        "--repository",
        default=".",
        help="clean Git work tree used to prove the audit commit and audit-only descendant",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = args.zip_path
    errors = verify_release_zip(path, repository=args.repository)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"release ZIP verified: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
