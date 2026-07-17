from __future__ import annotations

import pytest

import config
from engine.risk import (
    MAX_MARKET_BETA_WEIGHT,
    blend_company_market_beta,
    resolve_risk_parameters,
)


def _market_beta(**overrides):
    payload = {
        "available": True,
        "code": "600519",
        "benchmark_code": "000300",
        "as_of": "2026-07-15",
        "source": "Tencent Finance",
        "source_url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "start_date": "2023-06-30",
        "end_date": "2026-07-15",
        "price_observations": 157,
        "sample_size": 156,
        "raw_beta": 0.85,
        "blume_beta": 0.90,
        "r_squared": 0.30,
        "cache_key": "market:600519:2026-07-15",
        "cache_hit": True,
        "reason": "",
    }
    payload.update(overrides)
    return payload


def test_pinned_risk_parameters_keep_independent_dates_and_nonzero_sources():
    result = resolve_risk_parameters()

    assert result.mode == "pinned_official_snapshot"
    assert result.fallback_reason == ""
    assert result.risk_free_rate == config.RISK_FREE_RATE > 0
    assert result.equity_risk_premium == config.EQUITY_RISK_PREMIUM > 0
    assert result.risk_free_rate_as_of == "2026-07-15"
    assert result.equity_risk_premium_as_of == "2026-04-01"
    assert result.model_as_of == "2026-07-15"
    assert result.risk_free_rate_source_url.startswith("https://")
    assert result.equity_risk_premium_source_url.startswith("https://")


def test_failed_refresh_and_zero_candidate_use_visible_pinned_fallback_not_zero():
    unavailable = resolve_risk_parameters(unavailable_reason="Timeout: upstream unavailable")
    zero_candidate = resolve_risk_parameters(
        {
            **resolve_risk_parameters().to_dict(),
            "risk_free_rate": 0.0,
            "equity_risk_premium": 0.0,
        }
    )

    assert unavailable.mode == "pinned_official_fallback"
    assert unavailable.fallback_reason.startswith("source_unavailable:")
    assert unavailable.risk_free_rate > 0 and unavailable.equity_risk_premium > 0
    assert zero_candidate.mode == "pinned_official_fallback"
    assert zero_candidate.fallback_reason == "invalid_candidate:risk_free_rate_must_be_positive"
    assert zero_candidate.risk_free_rate == config.RISK_FREE_RATE
    assert zero_candidate.equity_risk_premium == config.EQUITY_RISK_PREMIUM


def test_complete_dated_candidate_can_be_used_without_network_access():
    candidate = resolve_risk_parameters().to_dict()
    candidate.update(
        {
            "risk_free_rate": 0.02,
            "risk_free_rate_as_of": "2026-07-16",
            "equity_risk_premium": 0.06,
            "equity_risk_premium_as_of": "2026-07-01",
            "model_as_of": "2026-07-16",
        }
    )

    result = resolve_risk_parameters(candidate)

    assert result.mode == "validated_supplied_snapshot"
    assert result.risk_free_rate == pytest.approx(0.02)
    assert result.equity_risk_premium == pytest.approx(0.06)


def test_weekly_beta_is_shrunk_toward_relevered_industry_prior_with_visible_formula():
    result = blend_company_market_beta(
        code="600519",
        industry_levered_beta=1.70,
        industry_beta_role="industry_unlevered_relevered",
        market_beta_estimate=_market_beta(),
    )

    assert result is not None
    assert result.status == "blended"
    assert result.beta_source == "industry_and_weekly_market_beta_blend"
    assert result.company_weight == pytest.approx(0.30)
    assert result.industry_weight == pytest.approx(0.70)
    assert result.final_beta == pytest.approx(1.70 * 0.70 + 0.90 * 0.30)
    assert result.raw_beta == pytest.approx(0.85)
    assert result.blume_beta == pytest.approx(0.90)
    assert result.r_squared == pytest.approx(0.30)
    assert result.sample_size == 156
    assert result.market_source == "Tencent Finance"
    assert "min(0.50, r_squared)" in result.weight_formula


def test_even_perfect_market_fit_cannot_fully_replace_industry_beta():
    result = blend_company_market_beta(
        code="600519",
        industry_levered_beta=2.0,
        industry_beta_role="industry_unlevered_relevered",
        market_beta_estimate=_market_beta(blume_beta=0.5, r_squared=1.0),
    )

    assert result is not None
    assert result.company_weight == MAX_MARKET_BETA_WEIGHT == 0.50
    assert result.final_beta == pytest.approx(1.25)


@pytest.mark.parametrize(
    ("estimate", "status"),
    [
        (_market_beta(r_squared=0.049), "industry_only_low_r_squared"),
        (_market_beta(sample_size=155, price_observations=156), "industry_only_insufficient_sample"),
        (_market_beta(end_date="2026-06-01"), "industry_only_stale_market_beta"),
        (_market_beta(code="000001"), "industry_only_identity_mismatch"),
        (_market_beta(available=False, reason="source_unavailable"), "industry_only_market_beta_unavailable"),
    ],
)
def test_unreliable_market_beta_is_disclosed_and_does_not_move_industry_prior(estimate, status):
    result = blend_company_market_beta(
        code="600519",
        industry_levered_beta=1.7,
        industry_beta_role="industry_unlevered_relevered",
        market_beta_estimate=estimate,
    )

    assert result is not None
    assert result.status == status
    assert result.company_weight == 0.0
    assert result.final_beta == pytest.approx(1.7)


def test_beijing_exchange_code_never_uses_weekly_market_beta():
    result = blend_company_market_beta(
        code="830001",
        industry_levered_beta=1.2,
        industry_beta_role="industry_unlevered_relevered",
        market_beta_estimate=_market_beta(code="830001"),
    )

    assert result is not None
    assert result.status == "industry_only_invalid_code"
    assert result.final_beta == pytest.approx(1.2)


def test_explicit_manual_levered_beta_remains_a_distinct_auditable_override():
    result = blend_company_market_beta(
        code="600519",
        industry_levered_beta=1.7,
        industry_beta_role="industry_unlevered_relevered",
        market_beta_estimate=_market_beta(),
        explicit_company_beta=1.1,
    )

    assert result is not None
    assert result.status == "explicit_company_beta_override"
    assert result.beta_source == "explicit_company_levered_beta"
    assert result.final_beta == pytest.approx(1.1)
