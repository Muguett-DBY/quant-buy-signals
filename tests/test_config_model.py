import math
from pathlib import Path
import tomllib

import config
from data.industry import _INDUSTRY_RULES
from tools.verify_release_zip import _exact_project_dependencies, _locked_requirements


ROOT = Path(__file__).resolve().parents[1]


def test_hash_locks_do_not_override_the_callers_package_index():
    for lock_name in ("requirements-lock.txt", "requirements-dev-lock.txt"):
        content = (ROOT / lock_name).read_bytes()
        text = content.decode("utf-8")
        assert not any(
            line.strip().startswith(("--index-url", "--extra-index-url", "--trusted-host"))
            for line in text.splitlines()
        )
        assert _locked_requirements(content) is not None


def test_wheel_metadata_preserves_every_direct_runtime_security_pin():
    requirements = {}
    for raw_line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        assert separator and name and version
        requirements[name.casefold().replace("_", "-").replace(".", "-")] = version

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert _exact_project_dependencies(project) == requirements


def test_every_supported_industry_has_an_explicit_risk_model():
    industry_codes = {code for code, _name, _keywords in _INDUSTRY_RULES}
    financial = set(config.INDUSTRY_FINANCIAL_LEVERED_BETA)
    unsupported = {"FINANCIAL_OTHER"}
    non_financial = industry_codes - financial - unsupported

    assert non_financial <= set(config.INDUSTRY_UNLEVERED_BETA)
    assert non_financial <= set(config.INDUSTRY_PRETAX_COST_OF_DEBT)
    assert financial == {"BANK", "INSURANCE", "SECURITIES"}
    assert unsupported.isdisjoint(config.INDUSTRY_UNLEVERED_BETA)
    assert unsupported.isdisjoint(config.INDUSTRY_FINANCIAL_LEVERED_BETA)


def test_capital_cost_inputs_are_finite_and_economically_valid():
    assert config.MODEL_RISK_DATA_AS_OF == "2026-07-15"
    assert config.RISK_FREE_RATE_AS_OF == "2026-07-15"
    assert config.EQUITY_RISK_PREMIUM_AS_OF == "2026-04-01"
    assert config.INDUSTRY_RISK_DATA_AS_OF == "2026-01-05"
    assert config.RISK_FREE_RATE == 0.017406
    assert config.EQUITY_RISK_PREMIUM == 0.05799671740067751
    assert config.EQUITY_RISK_PREMIUM_SOURCE_SHA256 == (
        "2bcfaace0ee4132ced6039ea0a2f26999af8d5366f8fbde81cf71dfb2735566e"
    )
    assert all(
        value.startswith("https://")
        for value in (
            config.RISK_FREE_RATE_SOURCE_URL,
            config.EQUITY_RISK_PREMIUM_SOURCE_URL,
            config.INDUSTRY_BETA_SOURCE_URL,
            config.INDUSTRY_WACC_SOURCE_URL,
        )
    )
    assert 0 < config.RISK_FREE_RATE < 0.10
    assert 0 < config.EQUITY_RISK_PREMIUM < 0.15
    assert 0 < config.MARGINAL_TAX_RATE < 1
    assert config.FCF_MARGIN_FLOOR == 0.0
    for mapping in (
        config.INDUSTRY_UNLEVERED_BETA,
        config.INDUSTRY_FINANCIAL_LEVERED_BETA,
        config.INDUSTRY_PRETAX_COST_OF_DEBT,
    ):
        assert mapping
        assert all(math.isfinite(value) and value > 0 for value in mapping.values())


def test_terminal_growth_stays_below_the_lowest_model_discount_rate():
    lowest_reference_wacc = 0.05
    assert all(growth < lowest_reference_wacc for growth in config.TERMINAL_GROWTH.values())


def test_growth_lookback_is_not_the_annual_history_fetch_window():
    from data.datacenter import ANNUAL_HISTORY_YEARS

    assert config.GROWTH_LOOKBACK_YEARS == 5
    assert config.HISTORY_YEARS == config.GROWTH_LOOKBACK_YEARS
    assert ANNUAL_HISTORY_YEARS == 10


def test_removed_legacy_settings_are_not_part_of_the_public_config():
    unused_names = {
        "STAGE1_MIN_ROE",
        "STAGE1_MIN_PE",
        "STAGE1_MAX_PE",
        "STAGE1_MIN_PB",
        "STAGE1_MIN_REVENUE_CAGR",
        "EASTMONEY_PUSH_URL",
        "EASTMONEY_INDICATOR_URL",
        "EASTMONEY_KLINE_URL",
        "STREAMLIT_PORT",
        "PAGE_SIZE",
    }
    assert not [name for name in unused_names if hasattr(config, name)]
