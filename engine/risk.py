"""Auditable CAPM inputs and conservative company-beta shrinkage.

The valuation engine never fetches risk inputs from the network.  It consumes
either a complete, explicitly dated parameter set supplied by a caller or the
versioned official snapshot in :mod:`config`.  Invalid/zero candidate inputs
fall back to that snapshot with a machine-readable reason.

Weekly price beta is a noisy *levered equity beta*.  It is therefore blended
with the company's already re-levered industry beta, rather than replacing an
industry asset beta before Hamada relevering.  The market-beta weight is
``min(50%, R²)`` after fixed sample, freshness and R² gates; even a perfect fit
cannot erase the industry prior.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from config import (
    EQUITY_RISK_PREMIUM,
    EQUITY_RISK_PREMIUM_AS_OF,
    EQUITY_RISK_PREMIUM_BASIS,
    EQUITY_RISK_PREMIUM_SOURCE,
    EQUITY_RISK_PREMIUM_SOURCE_URL,
    MODEL_RISK_DATA_AS_OF,
    RISK_FREE_RATE,
    RISK_FREE_RATE_AS_OF,
    RISK_FREE_RATE_SOURCE,
    RISK_FREE_RATE_SOURCE_URL,
    RISK_FREE_RATE_TENOR,
)


MIN_MARKET_BETA_SAMPLE_SIZE = 156
MIN_MARKET_BETA_R_SQUARED = 0.05
MAX_MARKET_BETA_WEIGHT = 0.50
MAX_MARKET_BETA_STALENESS_DAYS = 21
MAX_USABLE_MARKET_BETA = 5.0
MARKET_BETA_WEIGHT_FORMULA = "min(0.50, r_squared) after sample/freshness/R2 gates"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if value == parsed.isoformat() else None


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _normalise_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


@dataclass(frozen=True)
class RiskParameterSet:
    """Complete CAPM market inputs plus their independent source dates."""

    risk_free_rate: float
    risk_free_rate_as_of: str
    risk_free_rate_tenor: str
    risk_free_rate_source: str
    risk_free_rate_source_url: str
    equity_risk_premium: float
    equity_risk_premium_as_of: str
    equity_risk_premium_basis: str
    equity_risk_premium_source: str
    equity_risk_premium_source_url: str
    model_as_of: str
    mode: str
    fallback_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pinned_risk_parameters(*, mode: str, fallback_reason: str = "") -> RiskParameterSet:
    return RiskParameterSet(
        risk_free_rate=RISK_FREE_RATE,
        risk_free_rate_as_of=RISK_FREE_RATE_AS_OF,
        risk_free_rate_tenor=RISK_FREE_RATE_TENOR,
        risk_free_rate_source=RISK_FREE_RATE_SOURCE,
        risk_free_rate_source_url=RISK_FREE_RATE_SOURCE_URL,
        equity_risk_premium=EQUITY_RISK_PREMIUM,
        equity_risk_premium_as_of=EQUITY_RISK_PREMIUM_AS_OF,
        equity_risk_premium_basis=EQUITY_RISK_PREMIUM_BASIS,
        equity_risk_premium_source=EQUITY_RISK_PREMIUM_SOURCE,
        equity_risk_premium_source_url=EQUITY_RISK_PREMIUM_SOURCE_URL,
        model_as_of=MODEL_RISK_DATA_AS_OF,
        mode=mode,
        fallback_reason=fallback_reason,
    )


def _candidate_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, RiskParameterSet):
        return value.to_dict()
    return value if isinstance(value, Mapping) else None


def resolve_risk_parameters(
    candidate: Mapping[str, Any] | RiskParameterSet | None = None,
    *,
    unavailable_reason: str = "",
) -> RiskParameterSet:
    """Validate a complete offline snapshot or return the pinned official one.

    Partial candidates are deliberately rejected.  A failed network refresh
    can pass its diagnostic through ``unavailable_reason``; the resulting
    payload then states that the official snapshot was a fallback.  Neither a
    missing source nor a numeric zero can become a CAPM input.
    """

    if candidate is None:
        if unavailable_reason:
            return _pinned_risk_parameters(
                mode="pinned_official_fallback",
                fallback_reason=f"source_unavailable:{unavailable_reason[:180]}",
            )
        return _pinned_risk_parameters(mode="pinned_official_snapshot")

    payload = _candidate_mapping(candidate)
    if payload is None:
        return _pinned_risk_parameters(
            mode="pinned_official_fallback",
            fallback_reason="invalid_candidate:not_a_mapping",
        )

    risk_free = _finite(payload.get("risk_free_rate"))
    erp = _finite(payload.get("equity_risk_premium"))
    rf_date = _iso_date(payload.get("risk_free_rate_as_of"))
    erp_date = _iso_date(payload.get("equity_risk_premium_as_of"))
    model_date = _iso_date(payload.get("model_as_of"))
    text_fields = {
        key: _text(payload.get(key))
        for key in (
            "risk_free_rate_tenor",
            "risk_free_rate_source",
            "risk_free_rate_source_url",
            "equity_risk_premium_basis",
            "equity_risk_premium_source",
            "equity_risk_premium_source_url",
        )
    }
    if risk_free is None or not 0 < risk_free < 0.20:
        reason = "invalid_candidate:risk_free_rate_must_be_positive"
    elif erp is None or not 0 < erp < 0.30:
        reason = "invalid_candidate:equity_risk_premium_must_be_positive"
    elif rf_date is None or erp_date is None or model_date is None:
        reason = "invalid_candidate:source_dates_must_be_iso_dates"
    elif model_date < max(rf_date, erp_date):
        reason = "invalid_candidate:model_as_of_precedes_source_date"
    elif not all(text_fields.values()):
        reason = "invalid_candidate:source_metadata_incomplete"
    else:
        return RiskParameterSet(
            risk_free_rate=risk_free,
            risk_free_rate_as_of=rf_date.isoformat(),
            risk_free_rate_tenor=text_fields["risk_free_rate_tenor"],
            risk_free_rate_source=text_fields["risk_free_rate_source"],
            risk_free_rate_source_url=text_fields["risk_free_rate_source_url"],
            equity_risk_premium=erp,
            equity_risk_premium_as_of=erp_date.isoformat(),
            equity_risk_premium_basis=text_fields["equity_risk_premium_basis"],
            equity_risk_premium_source=text_fields["equity_risk_premium_source"],
            equity_risk_premium_source_url=text_fields["equity_risk_premium_source_url"],
            model_as_of=model_date.isoformat(),
            mode="validated_supplied_snapshot",
        )

    return _pinned_risk_parameters(mode="pinned_official_fallback", fallback_reason=reason)


@dataclass(frozen=True)
class BetaBlendResult:
    """Final levered beta and all evidence needed to replay the blend."""

    final_beta: float
    beta_source: str
    status: str
    reason: str
    industry_beta: float
    industry_beta_role: str
    company_weight: float
    industry_weight: float
    weight_formula: str
    market_available: bool | None
    market_code: str
    benchmark_code: str
    market_as_of: str
    market_start_date: str
    market_end_date: str
    market_source: str
    market_source_url: str
    raw_beta: float | None
    blume_beta: float | None
    r_squared: float | None
    sample_size: int | None
    price_observations: int | None
    cache_hit: bool | None
    cache_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _market_payload(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, Mapping) else None
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == value else None


def _beta_result(
    *,
    final_beta: float,
    beta_source: str,
    status: str,
    reason: str,
    industry_beta: float,
    industry_beta_role: str,
    market: Mapping[str, Any] | None,
    company_weight: float = 0.0,
) -> BetaBlendResult:
    market = market or {}
    available = market.get("available")
    return BetaBlendResult(
        final_beta=final_beta,
        beta_source=beta_source,
        status=status,
        reason=reason,
        industry_beta=industry_beta,
        industry_beta_role=industry_beta_role,
        company_weight=company_weight,
        industry_weight=1.0 - company_weight,
        weight_formula=MARKET_BETA_WEIGHT_FORMULA,
        market_available=available if isinstance(available, bool) else None,
        market_code=_text(market.get("code")),
        benchmark_code=_text(market.get("benchmark_code")),
        market_as_of=_text(market.get("as_of")),
        market_start_date=_text(market.get("start_date")),
        market_end_date=_text(market.get("end_date")),
        market_source=_text(market.get("source")),
        market_source_url=_text(market.get("source_url")),
        raw_beta=_finite(market.get("raw_beta")),
        blume_beta=_finite(market.get("blume_beta")),
        r_squared=_finite(market.get("r_squared")),
        sample_size=_integer(market.get("sample_size")),
        price_observations=_integer(market.get("price_observations")),
        cache_hit=market.get("cache_hit") if isinstance(market.get("cache_hit"), bool) else None,
        cache_key=_text(market.get("cache_key")),
    )


def blend_company_market_beta(
    *,
    code: Any,
    industry_levered_beta: Any,
    industry_beta_role: str,
    market_beta_estimate: Any = None,
    explicit_company_beta: Any = None,
) -> BetaBlendResult | None:
    """Return a conservative levered-beta blend without doing network I/O."""

    industry_beta = _finite(industry_levered_beta)
    if industry_beta is None or industry_beta <= 0:
        return None
    role = _text(industry_beta_role) or "industry_levered_beta"

    explicit = _finite(explicit_company_beta)
    if explicit_company_beta is not None:
        if explicit is None or explicit < 0:
            return None
        return _beta_result(
            final_beta=explicit,
            beta_source="explicit_company_levered_beta",
            status="explicit_company_beta_override",
            reason="caller supplied an explicit levered beta; weekly estimate was not applied",
            industry_beta=industry_beta,
            industry_beta_role=role,
            market=_market_payload(market_beta_estimate),
            company_weight=1.0,
        )

    market = _market_payload(market_beta_estimate)
    industry_source = (
        role
        if role
        in {
            "industry_financial_levered_beta",
            "industry_unlevered_relevered",
            "industry_unlevered_asset_fallback",
        }
        else "industry_levered_beta"
    )
    if market is None:
        return _beta_result(
            final_beta=industry_beta,
            beta_source=industry_source,
            status="industry_only_no_market_beta",
            reason="no precomputed weekly market-beta estimate supplied",
            industry_beta=industry_beta,
            industry_beta_role=role,
            market=None,
        )

    normalized_code = _normalise_code(code)
    market_code = _normalise_code(market.get("code"))
    if not (len(normalized_code) == 6 and normalized_code.isdigit() and normalized_code.startswith(("0", "3", "6"))):
        status, reason = "industry_only_invalid_code", "only Shanghai/Shenzhen A-share codes can use market beta"
    elif market_code != normalized_code:
        status, reason = "industry_only_identity_mismatch", "market-beta code does not match valuation company"
    elif market.get("available") is not True:
        unavailable = _text(market.get("reason")) or "estimate marked unavailable"
        status, reason = "industry_only_market_beta_unavailable", unavailable
    else:
        sample_size = _integer(market.get("sample_size"))
        observations = _integer(market.get("price_observations"))
        raw_beta = _finite(market.get("raw_beta"))
        blume_beta = _finite(market.get("blume_beta"))
        r_squared = _finite(market.get("r_squared"))
        as_of = _iso_date(market.get("as_of"))
        start = _iso_date(market.get("start_date"))
        end = _iso_date(market.get("end_date"))
        if _text(market.get("benchmark_code")) != "000300":
            status, reason = "industry_only_invalid_benchmark", "weekly beta benchmark must be CSI 300"
        elif sample_size is None or sample_size < MIN_MARKET_BETA_SAMPLE_SIZE:
            status, reason = "industry_only_insufficient_sample", "weekly beta sample is below 156 returns"
        elif observations is None or observations < sample_size + 1:
            status, reason = "industry_only_invalid_observations", "weekly price observations do not cover returns"
        elif raw_beta is None or blume_beta is None or r_squared is None:
            status, reason = "industry_only_nonfinite_market_beta", "weekly beta statistics are incomplete"
        elif not 0 < blume_beta <= MAX_USABLE_MARKET_BETA:
            status, reason = "industry_only_implausible_market_beta", "Blume beta is outside the conservative range"
        elif not 0 <= r_squared <= 1:
            status, reason = "industry_only_invalid_r_squared", "weekly beta R-squared is outside [0,1]"
        elif r_squared < MIN_MARKET_BETA_R_SQUARED:
            status, reason = "industry_only_low_r_squared", "weekly market fit is below the 5% reliability gate"
        elif as_of is None or start is None or end is None or not start < end <= as_of:
            status, reason = "industry_only_invalid_dates", "weekly beta source dates are invalid"
        elif (as_of - end).days > MAX_MARKET_BETA_STALENESS_DAYS:
            status, reason = "industry_only_stale_market_beta", "weekly beta end date is more than 21 days stale"
        elif not _text(market.get("source")) or not _text(market.get("source_url")):
            status, reason = "industry_only_missing_source", "weekly beta source metadata is incomplete"
        else:
            weight = min(MAX_MARKET_BETA_WEIGHT, r_squared)
            final_beta = (1.0 - weight) * industry_beta + weight * blume_beta
            if not math.isfinite(final_beta) or final_beta <= 0:
                return None
            return _beta_result(
                final_beta=final_beta,
                beta_source="industry_and_weekly_market_beta_blend",
                status="blended",
                reason="fixed-sample Blume beta shrunk toward the re-levered industry prior",
                industry_beta=industry_beta,
                industry_beta_role=role,
                market=market,
                company_weight=weight,
            )

    return _beta_result(
        final_beta=industry_beta,
        beta_source=industry_source,
        status=status,
        reason=reason,
        industry_beta=industry_beta,
        industry_beta_role=role,
        market=market,
    )


__all__ = [
    "BetaBlendResult",
    "MARKET_BETA_WEIGHT_FORMULA",
    "MAX_MARKET_BETA_STALENESS_DAYS",
    "MAX_MARKET_BETA_WEIGHT",
    "MIN_MARKET_BETA_R_SQUARED",
    "MIN_MARKET_BETA_SAMPLE_SIZE",
    "RiskParameterSet",
    "blend_company_market_beta",
    "resolve_risk_parameters",
]
