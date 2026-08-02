"""Patch 6 classified Type 7 scoring.

The July 30 Patch 6 contract is a *classify, then score inside the class*
model.  It is intentionally separate from the historical Template 1 +
Template 5 + Patch 5 intersection so the old cross-class scale cannot decide
whether a cyclical or technology company is a quality equity.

Every published number is replayable from the inputs carried by the ledger.
Industry and qualitative fallbacks are labelled ``derived_proxy`` and capped;
they are never described as primary evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import math
import re
from statistics import median, pstdev
from typing import Any


MODEL_ID = "patch6-type7-classified-equity-v2"
SCHEMA_VERSION = 2
STRICT_THRESHOLD = 7.0
LEGACY_DIAGNOSTIC_MODEL_ID = "patch6-type7-quality-equity-v7"
LEGACY_DIAGNOSTIC_SOURCE_RULE = "Template1>70 AND Template5>70 AND Patch5>70"
LEGACY_DIAGNOSTIC_NOTE = "旧Template1+Template5+Patch5仅保留诊断，不参与Type7触发"
LEGACY_DIAGNOSTIC_FIELDS = {
    "model_id",
    "source_rule",
    "scores",
    "prerequisites_complete",
    "triggered",
    "decisive",
    "note",
}

CLASS_LABELS = {"W": "弱周期", "T": "强科技", "C": "强周期"}
CLASSIFICATION_RULE = "T>=7 -> T; else C>=7 -> C; else W (N is reported as weak-cycle evidence)"
MODEL_SOURCE_RULE = (
    "classify C/T/N; certify quality when arithmetic mean(BM,MOAT,G) strictly > 7; "
    "apply class-specific route and current-price gates for buy readiness"
)
EVIDENCE_MAX_AGE_DAYS = 550

DIRECT_COMMODITY_INDUSTRIES = {
    "STEEL",
    "NONFERROUS",
    "CHEMICAL",
    "BUILDING_MATERIAL",
    "OIL_GAS",
    "COAL",
}
CYCLICAL_INDUSTRIES = DIRECT_COMMODITY_INDUSTRIES | {
    "CONST_MACHINERY",
    "AGRICULTURE",
    "TRANSPORT",
}
CORE_TECH_INDUSTRIES = {
    "SOFTWARE",
    "SEMICONDUCTOR",
    "BIO_PHARMA",
    "CHEM_PHARMA",
    "ELEC_COMPONENT",
    "TELECOM",
}
TECH_INDUSTRIES = CORE_TECH_INDUSTRIES | {
    "MEDICAL_SERVICE",
    "AUTO_VEHICLE",
    "AUTO_PARTS",
    "NEW_ENERGY_VEH",
    "INDUST_MACHINERY",
    "ELEC_EQUIP",
    "MEDIA",
}
ESSENTIAL_INDUSTRIES = {
    "ALCOHOL",
    "FOOD_BEV",
    "HOME_APPLIANCE",
    "TRAD_CN_MED",
    "CHEM_PHARMA",
    "BIO_PHARMA",
    "MEDICAL_SERVICE",
    "POWER_UTILITY",
    "TELECOM",
}
LICENSE_OR_SCARCITY_INDUSTRIES = {
    "ALCOHOL",
    "POWER_UTILITY",
    "TELECOM",
    "CHEM_PHARMA",
    "BIO_PHARMA",
} | DIRECT_COMMODITY_INDUSTRIES

DIMENSION_ITEM_WEIGHTS: dict[str, dict[str, float]] = {
    "W": {
        "BM": {"pricing_power": 0.30, "fcf_conversion": 0.25, "repeat_demand": 0.25, "asset_light": 0.20},
        "MOAT": {"brand_mindshare": 0.30, "network_switching": 0.25, "license_scarcity": 0.20, "time_thickness": 0.25},
        "G": {
            "volume_price_space": 0.35,
            "category_expansion": 0.30,
            "inflation_pass_through": 0.20,
            "certainty": 0.15,
        },
    },
    "T": {
        "BM": {
            "rd_conversion": 0.30,
            "revenue_quality": 0.25,
            "declining_marginal_cost": 0.20,
            "cashflow_inflection": 0.25,
        },
        "MOAT": {"patent_standard": 0.25, "talent_retention": 0.20, "data_network": 0.25, "platform_lockin": 0.30},
        "G": {"s_curve_relay": 0.35, "tam_space": 0.30, "nonlinear_option": 0.20, "disruption_resilience": 0.15},
    },
    "C": {
        "BM": {
            "cost_curve": 0.35,
            "integration_self_supply": 0.25,
            "cash_conversion": 0.20,
            "capacity_discipline": 0.20,
        },
        "MOAT": {"resource_scarcity": 0.30, "cost_lead": 0.30, "scale_location": 0.20, "cycle_survival": 0.20},
        "G": {"low_cost_expansion": 0.35, "integration_gain": 0.30, "commodity_trend": 0.20, "certainty": 0.15},
    },
}

ITEM_LABELS = {
    "pricing_power": "定价权/毛利率",
    "fcf_conversion": "自由现金流转化率",
    "repeat_demand": "复购/刚需",
    "asset_light": "轻资产度",
    "brand_mindshare": "品牌/心智份额",
    "network_switching": "网络效应/转换成本",
    "license_scarcity": "牌照/稀缺资源",
    "time_thickness": "时间厚度",
    "volume_price_space": "量价空间",
    "category_expansion": "品类扩张",
    "inflation_pass_through": "通胀传导",
    "certainty": "确定性",
    "rd_conversion": "研发转化效率",
    "revenue_quality": "收入质量",
    "declining_marginal_cost": "边际成本递减",
    "cashflow_inflection": "现金流拐点",
    "patent_standard": "专利/技术标准",
    "talent_retention": "人才密度与留存",
    "data_network": "数据/网络效应",
    "platform_lockin": "生态/平台锁定",
    "s_curve_relay": "S曲线接棒",
    "tam_space": "市场空间",
    "nonlinear_option": "非线性期权",
    "disruption_resilience": "颠覆风险防范",
    "cost_curve": "成本曲线位置",
    "integration_self_supply": "一体化/自给率",
    "cash_conversion": "现金转化率",
    "capacity_discipline": "产能纪律",
    "resource_scarcity": "资源储量/稀缺性",
    "cost_lead": "成本领先幅度",
    "scale_location": "规模/区位",
    "cycle_survival": "穿越周期验证",
    "low_cost_expansion": "低成本扩张空间",
    "integration_gain": "一体化增利",
    "commodity_trend": "商品价格长期趋势",
}

# A complete ledger may not relabel an outcome proxy as direct evidence or
# silently lift a proxy cap.  Conditional entries reflect the only two source
# states produced by the scorer; all other atoms have one canonical policy.
_REPORTED_ATOMS = {
    ("W", "BM", "fcf_conversion"),
    ("W", "BM", "asset_light"),
    ("W", "G", "volume_price_space"),
    ("W", "G", "certainty"),
    ("T", "BM", "rd_conversion"),
    ("T", "BM", "cashflow_inflection"),
    ("C", "BM", "cash_conversion"),
    ("C", "MOAT", "cycle_survival"),
}
_FIXED_DERIVED_CAPS = {
    ("W", "BM", "pricing_power"): 9.0,
    ("W", "BM", "repeat_demand"): 8.0,
    ("W", "MOAT", "brand_mindshare"): 8.5,
    ("W", "MOAT", "network_switching"): 8.0,
    ("W", "MOAT", "license_scarcity"): 8.0,
    ("W", "MOAT", "time_thickness"): 9.0,
    ("W", "G", "category_expansion"): 8.0,
    ("W", "G", "inflation_pass_through"): 8.0,
    ("T", "BM", "revenue_quality"): 8.5,
    ("T", "BM", "declining_marginal_cost"): 9.0,
    ("T", "MOAT", "patent_standard"): 8.0,
    ("T", "MOAT", "talent_retention"): 8.0,
    ("T", "MOAT", "platform_lockin"): 9.0,
    ("T", "G", "tam_space"): 8.5,
    ("T", "G", "nonlinear_option"): 8.0,
    ("T", "G", "disruption_resilience"): 9.0,
    ("C", "BM", "cost_curve"): 8.0,
    ("C", "BM", "integration_self_supply"): 8.0,
    ("C", "BM", "capacity_discipline"): 8.0,
    ("C", "MOAT", "cost_lead"): 8.0,
    ("C", "MOAT", "scale_location"): 7.0,
    ("C", "G", "low_cost_expansion"): 8.0,
    ("C", "G", "integration_gain"): 8.0,
    ("C", "G", "commodity_trend"): 7.0,
    ("C", "G", "certainty"): 7.0,
}
_CONDITIONAL_ATOM_POLICIES = {
    ("T", "MOAT", "data_network"): {("derived_proxy", 8.0), ("derived_proxy", 9.0)},
    ("T", "G", "s_curve_relay"): {("derived_proxy", 8.5), ("derived_proxy", 9.0)},
    ("C", "MOAT", "resource_scarcity"): {("derived_proxy", 8.0), ("derived_proxy", 9.0)},
}
_CLASSIFICATION_EVIDENCE_LEVELS = {
    "c_margin_volatility": {"reported_observable"},
    "c_profit_elasticity": {"reported_observable"},
    "c_capex_intensity": {"reported_observable"},
    "c_commodity_driver": {"derived_proxy"},
    "t_rd_intensity": {"reported_observable"},
    "t_intangible_patent": {"derived_proxy"},
    "t_iteration": {"derived_proxy"},
    "t_platform": {"primary", "derived_proxy"},
    "n_repeat": {"derived_proxy"},
    "n_macro_beta": {"derived_proxy"},
    "n_pricing": {"reported_observable"},
    "n_mindshare": {"primary", "derived_proxy"},
}
_CLASSIFICATION_COMPONENTS = {
    "C": {
        "c_margin_volatility": ("毛利率五年波动", 3.0),
        "c_profit_elasticity": ("利润/收入弹性", 3.0),
        "c_capex_intensity": ("资本开支强度", 2.0),
        "c_commodity_driver": ("商品价格/产能驱动", 2.0),
    },
    "T": {
        "t_rd_intensity": ("研发费用率", 3.0),
        "t_intangible_patent": ("专利/无形资产密集", 2.0),
        "t_iteration": ("技术/产品迭代周期", 2.0),
        "t_platform": ("网络效应/平台生态", 3.0),
    },
    "N": {
        "n_repeat": ("复购/必选", 3.0),
        "n_macro_beta": ("低宏观敏感度", 2.0),
        "n_pricing": ("定价权", 3.0),
        "n_mindshare": ("刚需/心智垄断", 2.0),
    },
}
_CLASSIFICATION_EVIDENCE_INPUTS = {
    "t_platform": {"technology_score", "business_model_score"},
    "n_mindshare": {"moat_score"},
}
_ATOMIC_EVIDENCE_INPUTS = {
    "business_model_score",
    "moat_score",
    "moat_durability_score",
    "durability_score",
    "runway_score",
    "growth_sustainability_score",
    "technology_score",
    "management_alignment_score",
    "industry_durability_score",
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _bounded(value: Any, lower: float, upper: float) -> float | None:
    number = _finite(value)
    return number if number is not None and lower <= number <= upper else None


def _clip(value: float, lower: float = 0.0, upper: float = 10.0) -> float:
    return min(upper, max(lower, float(value)))


def _linear(value: float | None, anchors: Sequence[tuple[float, float]]) -> float | None:
    if value is None:
        return None
    ordered = sorted((float(x), float(y)) for x, y in anchors)
    if value <= ordered[0][0]:
        return ordered[0][1]
    for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
        if value <= x1:
            if math.isclose(x0, x1):
                return y1
            return y0 + (value - x0) * (y1 - y0) / (x1 - x0)
    return ordered[-1][1]


def _average(*values: float | None) -> float | None:
    clean = [value for value in values if value is not None]
    return math.fsum(clean) / len(clean) if clean else None


def _cycle_overlay_penalty(cycle_sensitivity: float | None) -> float | None:
    """Deduct 0.5..2.0 points from a technology cash-flow atom when C>=7."""

    if cycle_sensitivity is None:
        return None
    if cycle_sensitivity < 7.0:
        return 0.0
    return min(2.0, 0.5 * (cycle_sensitivity - 6.0))


def _secondary_features(class_code: str, scores: Mapping[str, float]) -> list[str]:
    return [
        CLASS_LABELS[key] for key in ("T", "C", "W") if key != class_code and scores[{"W": "N"}.get(key, key)] >= 7.0
    ]


def _possible_secondary_features(
    class_code: str,
    scores: Mapping[str, float],
    upper_bounds: Mapping[str, float],
) -> list[str]:
    return [
        CLASS_LABELS[key]
        for key in ("T", "C", "W")
        if key != class_code
        and scores[{"W": "N"}.get(key, key)] < 7.0
        and upper_bounds[{"W": "N"}.get(key, key)] >= 7.0
    ]


def _proxy_cap_matches(actual: float | None, expected: float | None) -> bool:
    return (
        (actual is None) if expected is None else actual is not None and math.isclose(actual, expected, abs_tol=1e-12)
    )


def _atom_policy_valid(
    class_code: str,
    dimension: str,
    key: str,
    *,
    complete: bool,
    evidence_level: str,
    proxy_cap: float | None,
) -> bool:
    identity = (class_code, dimension, key)
    if identity in _REPORTED_ATOMS:
        return _proxy_cap_matches(proxy_cap, None) and (not complete or evidence_level == "reported_observable")
    if identity in _FIXED_DERIVED_CAPS:
        return _proxy_cap_matches(proxy_cap, _FIXED_DERIVED_CAPS[identity]) and (
            not complete or evidence_level == "derived_proxy"
        )
    policies = _CONDITIONAL_ATOM_POLICIES.get(identity)
    if not policies:
        return False
    cap_allowed = any(_proxy_cap_matches(proxy_cap, allowed_cap) for _level, allowed_cap in policies)
    return cap_allowed and (
        not complete
        or any(
            evidence_level == allowed_level and _proxy_cap_matches(proxy_cap, allowed_cap)
            for allowed_level, allowed_cap in policies
        )
    )


def _series(metric: Mapping[str, Any], value_key: str, year_key: str) -> dict[int, float]:
    values = metric.get(value_key)
    years = metric.get(year_key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return {}
    if not isinstance(years, Sequence) or isinstance(years, (str, bytes)) or len(values) != len(years):
        return {}
    result: dict[int, float] = {}
    for raw_year, raw_value in zip(years, values):
        value = _finite(raw_value)
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            continue
        if value is not None and 1900 <= year <= 2200:
            result[year] = value
    return result


def _latest_financial_year(metric: Mapping[str, Any]) -> int | None:
    candidates: list[int] = []
    raw_financial_as_of = str(metric.get("financial_indicator_as_of") or "")
    if len(raw_financial_as_of) >= 4 and raw_financial_as_of[:4].isdigit():
        year = int(raw_financial_as_of[:4])
        if 1900 <= year <= 2200:
            candidates.append(year)
    revenue = _series(metric, "revenue_values", "revenue_years")
    if revenue:
        candidates.append(max(revenue))
    return max(candidates) if candidates else None


def _recent_annual_window(
    values_by_year: Mapping[int, Any],
    *,
    latest_financial_year: int | None,
    minimum_years: int = 3,
    maximum_years: int = 5,
) -> dict[str, Any]:
    """Return the most recent consecutive suffix, bound to the latest annual report.

    Gaps before the suffix are deliberately irrelevant.  A suffix that ends
    before ``latest_financial_year`` remains observable in ``years`` but is not
    complete and therefore cannot contribute a score.
    """

    if not values_by_year:
        return {
            "years": [],
            "values": [],
            "latest_financial_year": latest_financial_year,
            "current": False,
            "complete": False,
        }
    observed_latest = max(values_by_year)
    years: list[int] = []
    year = observed_latest
    while year in values_by_year and len(years) < maximum_years:
        years.append(year)
        year -= 1
    years.reverse()
    current = latest_financial_year is not None and observed_latest == latest_financial_year
    return {
        "years": years,
        "values": [values_by_year[year] for year in years],
        "latest_financial_year": latest_financial_year,
        "current": current,
        "complete": bool(current and len(years) >= minimum_years),
    }


def _annual_history_window(
    metric: Mapping[str, Any],
    value_key: str,
    year_key: str,
    *,
    minimum_years: int = 3,
) -> dict[str, Any]:
    return _recent_annual_window(
        _series(metric, value_key, year_key),
        latest_financial_year=_latest_financial_year(metric),
        minimum_years=minimum_years,
    )


def _ratio_history(metric: Mapping[str, Any], numerator_key: str, denominator_key: str) -> dict[str, Any]:
    numerator = _series(metric, numerator_key, numerator_key.replace("_history", "_years"))
    denominator = _series(metric, denominator_key, denominator_key.replace("_values", "_years"))
    ratios = {
        year: numerator[year] / denominator[year] for year in set(numerator) & set(denominator) if denominator[year] > 0
    }
    return _recent_annual_window(
        ratios,
        latest_financial_year=_latest_financial_year(metric),
    )


def _published_annual_window_complete(
    inputs: Mapping[str, Any],
    years_key: str,
    *,
    minimum_years: int = 3,
    maximum_years: int | None = 5,
) -> bool:
    raw_years = inputs.get(years_key)
    latest = inputs.get("latest_financial_year")
    if (
        not isinstance(raw_years, Sequence)
        or isinstance(raw_years, (str, bytes))
        or not isinstance(latest, int)
        or isinstance(latest, bool)
    ):
        return False
    years = list(raw_years)
    return bool(
        len(years) >= minimum_years
        and (maximum_years is None or len(years) <= maximum_years)
        and all(isinstance(year, int) and not isinstance(year, bool) for year in years)
        and all(current - previous == 1 for previous, current in zip(years, years[1:]))
        and years[-1] == latest
    )


def _evidence_id_is_bound(evidence_id: str, expected_code: str) -> bool:
    tokens = {
        token.upper()
        for token in re.split(r"[^A-Za-z0-9._]+", evidence_id)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", token)
    }
    return expected_code.upper() in tokens


def _validated_evidence_reference(
    evidence: Any,
    *,
    expected_code: str,
    reference_as_of: str,
) -> dict[str, str] | None:
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != {"source", "evidence_id", "as_of", "summary"}
        or any(not isinstance(evidence.get(field), str) or not str(evidence[field]).strip() for field in evidence)
    ):
        return None
    normalized = {field: str(evidence[field]).strip() for field in ("source", "evidence_id", "as_of", "summary")}
    if (
        len(normalized["source"]) > 200
        or len(normalized["evidence_id"]) > 200
        or len(normalized["summary"]) > 1_000
        or any(ord(character) < 32 for character in "".join(normalized.values()))
    ):
        return None
    try:
        evidence_date = date.fromisoformat(normalized["as_of"])
        reference_date = date.fromisoformat(reference_as_of)
    except ValueError:
        return None
    if (
        normalized["as_of"] != evidence_date.isoformat()
        or reference_as_of != reference_date.isoformat()
        or reference_date > date.today()
        or evidence_date > reference_date
        or (reference_date - evidence_date).days > EVIDENCE_MAX_AGE_DAYS
        or not re.fullmatch(r"\d{6}", expected_code)
        or not _evidence_id_is_bound(normalized["evidence_id"], expected_code)
    ):
        return None
    return normalized


def _metric_score(metric: Mapping[str, Any], key: str) -> tuple[float | None, str, dict[str, str] | None]:
    score = _finite(metric.get(key))
    level = str(metric.get(f"{key}_evidence_level") or "")
    evidence = _validated_evidence_reference(
        metric.get(f"{key}_evidence"),
        expected_code=str(metric.get("code") or ""),
        reference_as_of=str(metric.get("source_trade_date") or ""),
    )
    if score is None or not 0 <= score <= 10 or level not in {"primary", "derived_proxy"} or evidence is None:
        return None, "missing", None
    return score, level, evidence


def _growth_score(rate: float | None) -> float | None:
    return _linear(rate, [(-0.10, 0), (0.0, 3), (0.05, 5), (0.10, 7), (0.20, 9), (0.30, 10)])


def _stability_score(metric: Mapping[str, Any]) -> float | None:
    consistency = _finite(metric.get("growth_consistency"))
    volatility = _finite(metric.get("profit_volatility"))
    if consistency is None or volatility is None:
        return None
    components = [
        _linear(consistency, [(0.0, 10), (0.25, 9), (0.50, 7), (1.0, 4), (2.0, 1)]),
        _linear(volatility, [(0.0, 10), (0.15, 9), (0.30, 7), (0.60, 4), (1.0, 1)]),
    ]
    return _average(*components)


def _cash_conversion_score(metric: Mapping[str, Any]) -> tuple[float | None, dict[str, Any]]:
    ratio = _finite(metric.get("ocf_np_ratio"))
    window = _annual_history_window(metric, "fcf_history", "fcf_years")
    values = window["values"]
    if ratio is None or window["complete"] is not True:
        return None, window
    positive_share = sum(value > 0 for value in values) / len(values)
    return (
        _average(
            _linear(ratio, [(0.0, 0), (0.3, 2), (0.8, 5), (1.2, 9), (1.5, 10)]),
            10.0 * positive_share,
        ),
        window,
    )


def _gross_score(metric: Mapping[str, Any]) -> float | None:
    gross = _finite(metric.get("gross_margin"))
    gross_cv = _finite(metric.get("gross_margin_cv"))
    if gross is None or gross_cv is None:
        return None
    return _average(
        _linear(gross, [(0.0, 0), (0.20, 3), (0.40, 6), (0.60, 9), (0.80, 10)]),
        _linear(gross_cv, [(0.0, 10), (0.05, 9), (0.10, 7), (0.20, 4), (0.40, 1)]),
    )


def _asset_light_score(metric: Mapping[str, Any]) -> tuple[float | None, float | None, dict[str, Any]]:
    window = _ratio_history(metric, "capex_history", "revenue_values")
    ratios = window["values"]
    intensity = median(ratios) if len(ratios) >= 3 else None
    score = (
        _linear(intensity, [(0.0, 10), (0.05, 9), (0.15, 5), (0.25, 2), (0.40, 0)])
        if window["complete"] is True
        else None
    )
    return score, intensity, window


def _roic_score(metric: Mapping[str, Any]) -> float | None:
    roic = _finite(metric.get("roic"))
    wacc = _finite(metric.get("wacc"))
    spread = roic - wacc if roic is not None and wacc is not None else None
    return _linear(spread, [(-0.10, 0), (0.0, 4), (0.05, 7), (0.10, 9), (0.20, 10)])


def _history_score(metric: Mapping[str, Any]) -> tuple[float | None, int, list[int], int | None, bool]:
    latest_financial_year = _latest_financial_year(metric)
    year_sets: list[set[int]] = []
    for value_key, year_key in (
        ("revenue_values", "revenue_years"),
        ("net_profit_history", "net_profit_years"),
        ("fcf_history", "fcf_years"),
        ("gross_margin_history", "gross_margin_years"),
    ):
        years = set(_series(metric, value_key, year_key))
        if not years:
            return None, 0, [], latest_financial_year, False
        year_sets.append(years)
    common = set.intersection(*year_sets)
    if not common:
        return None, 0, [], latest_financial_year, False
    latest = max(common)
    span = 1
    while latest - span in common:
        span += 1
    used_years = list(range(latest - span + 1, latest + 1))
    current = latest_financial_year is not None and latest == latest_financial_year
    score = (
        _linear(float(span), [(3, 2), (5, 3), (10, 5), (20, 7), (30, 9), (40, 10)]) if current and span >= 3 else None
    )
    return score, span, used_years, latest_financial_year, current


def _classification_formula_value(key: str, inputs: Mapping[str, Any]) -> float | None:
    """Replay one C/T/N classification component from its published inputs."""

    industry = str(inputs.get("industry") or "")
    if key == "c_margin_volatility":
        if not _published_annual_window_complete(inputs, "used_years", minimum_years=5):
            return None
        value = _finite(inputs.get("gross_margin_std_pp"))
        return (
            3.0
            if value is not None and value > 15
            else 2.0
            if value is not None and value >= 10
            else 1.0
            if value is not None
            else None
        )
    if key == "c_profit_elasticity":
        if not _published_annual_window_complete(inputs, "used_years"):
            return None
        value = _finite(inputs.get("median_elasticity"))
        return (
            3.0
            if value is not None and value > 2
            else 2.0
            if value is not None and value >= 1
            else 1.0
            if value is not None
            else None
        )
    if key == "c_capex_intensity":
        if not _published_annual_window_complete(inputs, "used_years", minimum_years=5):
            return None
        value = _finite(inputs.get("mean_capex_revenue"))
        return (
            2.0
            if value is not None and value > 0.25
            else 1.0
            if value is not None and value >= 0.10
            else 0.0
            if value is not None
            else None
        )
    if key == "c_commodity_driver":
        return (
            1.0
            if industry in DIRECT_COMMODITY_INDUSTRIES
            else 0.5
            if industry in CYCLICAL_INDUSTRIES
            else 0.0
            if industry
            else None
        )
    if key == "t_rd_intensity":
        value = _finite(inputs.get("rd_intensity"))
        return (
            3.0
            if value is not None and value > 0.12
            else 2.0
            if value is not None and value >= 0.08
            else 1.0
            if value is not None and value >= 0.04
            else 0.0
            if value is not None
            else None
        )
    if key == "t_intangible_patent":
        return (
            1.0
            if industry in CORE_TECH_INDUSTRIES
            else 0.5
            if industry in TECH_INDUSTRIES
            else 0.0
            if industry
            else None
        )
    if key == "t_iteration":
        return (
            1.0
            if industry in CORE_TECH_INDUSTRIES
            else 0.5
            if industry in TECH_INDUSTRIES
            else 0.0
            if industry
            else None
        )
    if key == "t_platform":
        cap = _finite(inputs.get("cap"))
        base = _average(_finite(inputs.get("technology_score")), _finite(inputs.get("business_model_score")))
        return None if base is None or cap is None else base / 10.0 * cap
    if key == "n_repeat":
        return (
            2.0
            if industry in ESSENTIAL_INDUSTRIES
            else 1.0
            if industry in {"RETAIL", "HOME_APPLIANCE"}
            else 0.0
            if industry
            else None
        )
    if key == "n_macro_beta":
        value = _finite(inputs.get("stability_score"))
        return None if value is None else 1.0 if value >= 8 else 0.5 if value >= 5 else 0.0
    if key == "n_pricing":
        return _average(
            _linear(_finite(inputs.get("gross_margin")), [(0.20, 0), (0.40, 1), (0.60, 2), (0.80, 3)]),
            _linear(_finite(inputs.get("gross_margin_cv")), [(0.0, 3), (0.05, 2.5), (0.10, 1.5), (0.20, 0)]),
        )
    if key == "n_mindshare":
        moat = _finite(inputs.get("moat_score"))
        if not industry and moat is None:
            return None
        return min(2.0, (1.0 if industry in LICENSE_OR_SCARCITY_INDUSTRIES else 0.0) + (moat or 0.0) / 10.0)
    return None


def _atomic_formula_value(class_code: str, dimension: str, key: str, inputs: Mapping[str, Any]) -> float | None:
    """Replay one class-specific atom from the normalized inputs in its ledger."""

    def number(name: str) -> float | None:
        return _finite(inputs.get(name))

    def current_window(
        years_key: str,
        *,
        minimum_years: int = 3,
        maximum_years: int | None = 5,
    ) -> bool:
        return _published_annual_window_complete(
            inputs,
            years_key,
            minimum_years=minimum_years,
            maximum_years=maximum_years,
        )

    if class_code == "W":
        formulas = {
            ("BM", "pricing_power"): lambda: _average(number("gross_outcome"), number("moat_score")),
            ("BM", "fcf_conversion"): lambda: (
                None
                if not current_window("fcf_used_years")
                or number("ocf_np_ratio") is None
                or number("fcf_positive_score") is None
                else _average(
                    _linear(number("ocf_np_ratio"), [(0.0, 0), (0.3, 2), (0.8, 5), (1.2, 9), (1.5, 10)]),
                    number("fcf_positive_score"),
                )
            ),
            ("BM", "repeat_demand"): lambda: _average(number("N_sensitivity"), number("business_model_score")),
            ("BM", "asset_light"): lambda: (
                _linear(
                    number("capex_revenue_median"),
                    [(0.0, 10), (0.05, 9), (0.15, 5), (0.25, 2), (0.40, 0)],
                )
                if current_window("capex_revenue_used_years")
                else None
            ),
            ("MOAT", "brand_mindshare"): lambda: _average(number("moat_score"), number("gross_outcome")),
            ("MOAT", "network_switching"): lambda: _average(number("moat_score"), number("business_model_score")),
            ("MOAT", "license_scarcity"): lambda: _average(number("moat_score"), number("sector_anchor")),
            ("MOAT", "time_thickness"): lambda: (
                _average(number("history_score"), number("durability_score"))
                if current_window("history_used_years", maximum_years=None)
                else None
            ),
            ("G", "volume_price_space"): lambda: _average(number("growth_score"), number("pricing_score")),
            ("G", "category_expansion"): lambda: _average(
                number("runway_score"), number("growth_sustainability_score")
            ),
            ("G", "inflation_pass_through"): lambda: _average(number("pricing_score"), number("margin_stability")),
            ("G", "certainty"): lambda: (
                _average(
                    _linear(number("growth_consistency"), [(0.0, 10), (0.25, 9), (0.50, 7), (1.0, 4), (2.0, 1)]),
                    _linear(number("profit_volatility"), [(0.0, 10), (0.15, 9), (0.30, 7), (0.60, 4), (1.0, 1)]),
                )
                if number("growth_consistency") is not None and number("profit_volatility") is not None
                else None
            ),
        }
    elif class_code == "T":
        formulas = {
            ("BM", "rd_conversion"): lambda: _average(
                number("rd_intensity_score"), number("growth_score"), number("roic_score")
            ),
            ("BM", "revenue_quality"): lambda: _average(
                number("business_model_score"), number("stability_score"), number("cash_conversion")
            ),
            ("BM", "declining_marginal_cost"): lambda: _average(
                number("gross_outcome"), number("asset_light"), number("roic_score")
            ),
            ("BM", "cashflow_inflection"): lambda: (
                None
                if not current_window("fcf_used_years")
                or _average(number("cash_conversion"), number("fcf_positive_score"), number("growth_score")) is None
                or number("cycle_overlay_penalty") is None
                else _average(number("cash_conversion"), number("fcf_positive_score"), number("growth_score"))
                - float(number("cycle_overlay_penalty"))
            ),
            ("MOAT", "patent_standard"): lambda: _average(number("technology_score"), number("rd_intensity_score")),
            ("MOAT", "talent_retention"): lambda: _average(
                number("management_alignment_score"), number("technology_score")
            ),
            ("MOAT", "data_network"): lambda: _average(
                number("moat_score"), number("business_model_score"), number("technology_score")
            ),
            ("MOAT", "platform_lockin"): lambda: _average(number("moat_score"), number("business_model_score")),
            ("G", "s_curve_relay"): lambda: _average(
                number("growth_score"), number("runway_score"), number("technology_score")
            ),
            ("G", "tam_space"): lambda: _average(
                number("runway_score"), number("industry_durability_score"), number("growth_score")
            ),
            ("G", "nonlinear_option"): lambda: _average(
                number("technology_score"), number("rd_intensity_score"), number("growth_sustainability_score")
            ),
            ("G", "disruption_resilience"): lambda: _average(
                number("moat_durability_score"), number("stability_score")
            ),
        }
    else:
        formulas = {
            ("BM", "cost_curve"): lambda: _average(
                number("gross_outcome"), number("roic_score"), number("stability_score")
            ),
            ("BM", "integration_self_supply"): lambda: _average(
                number("business_model_score"), number("cash_conversion"), number("asset_light")
            ),
            ("BM", "cash_conversion"): lambda: (
                None
                if not current_window("fcf_used_years")
                or number("ocf_np_ratio") is None
                or number("fcf_positive_score") is None
                else _average(
                    _linear(number("ocf_np_ratio"), [(0.0, 0), (0.3, 2), (0.8, 5), (1.2, 9), (1.5, 10)]),
                    number("fcf_positive_score"),
                )
            ),
            ("BM", "capacity_discipline"): lambda: (
                _average(
                    _linear(
                        number("capex_revenue_median"),
                        [(0.0, 10), (0.05, 9), (0.15, 5), (0.25, 2), (0.40, 0)],
                    ),
                    number("debt_score"),
                    number("roic_score"),
                )
                if current_window("capex_revenue_used_years")
                else None
            ),
            ("MOAT", "resource_scarcity"): lambda: _average(number("moat_score"), number("sector_anchor")),
            ("MOAT", "cost_lead"): lambda: _average(
                number("gross_outcome"), number("roic_score"), number("margin_stability")
            ),
            ("MOAT", "scale_location"): lambda: _average(
                number("moat_score"), number("business_model_score"), number("roic_score")
            ),
            ("MOAT", "cycle_survival"): lambda: (
                _average(number("history_score"), number("fcf_positive_score"), number("debt_score"))
                if current_window("fcf_used_years") and current_window("history_used_years", maximum_years=None)
                else None
            ),
            ("G", "low_cost_expansion"): lambda: _average(
                number("growth_score"), number("roic_score"), number("business_model_score")
            ),
            ("G", "integration_gain"): lambda: _average(
                number("cash_conversion"), number("gross_outcome"), number("roic_score")
            ),
            ("G", "commodity_trend"): lambda: _average(number("industry_durability_score"), number("growth_score")),
            ("G", "certainty"): lambda: _average(
                number("stability_score"),
                None if number("cycle_sensitivity") is None else 10.0 - number("cycle_sensitivity"),
            ),
        }
    formula = formulas.get((dimension, key))
    return formula() if formula is not None else None


def _atom(
    key: str,
    weight: float,
    score: float | None,
    *,
    formula: str,
    inputs: Mapping[str, Any],
    source_rule: str,
    evidence_level: str,
    proxy_cap: float | None = None,
    complete: bool | None = None,
    evidence_refs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    available = score is not None
    cap = _clip(proxy_cap) if proxy_cap is not None else 10.0
    final_score = _clip(score if score is not None else 0.0, upper=cap)
    missing_inputs = [name for name, value in inputs.items() if value is None]
    inputs_complete = not missing_inputs
    is_complete = bool(available and inputs_complete)
    if complete is not None:
        is_complete = bool(is_complete and complete)
    # An average calculated from only the available inputs is not a valid
    # lower bound for the missing inputs. Publish zero until the atom is
    # complete and retain the full ten-point upper bound.
    published_score = round(final_score if is_complete else 0.0, 6)
    upper_bound = published_score if is_complete else 10.0
    return {
        "key": key,
        "label": ITEM_LABELS[key],
        "weight": weight,
        "score": published_score,
        "points": round(published_score * weight, 9),
        "complete": is_complete,
        "evidence_level": evidence_level if is_complete else "partial" if available else "missing",
        "proxy_cap": round(cap, 6) if proxy_cap is not None else None,
        "formula": formula,
        "inputs": dict(inputs),
        "evidence_refs": {str(key): dict(value) for key, value in (evidence_refs or {}).items()},
        "source_rule": source_rule,
        "upper_bound": round(upper_bound, 6),
        "missing_inputs": missing_inputs,
    }


def _classification_component(
    key: str,
    label: str,
    maximum: float,
    awarded: float | None,
    *,
    formula: str,
    inputs: Mapping[str, Any],
    evidence_level: str,
    source_rule: str,
    evidence_refs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    diagnostic = min(maximum, max(0.0, float(awarded or 0.0)))
    missing_inputs = [name for name, value in inputs.items() if value is None]
    complete = awarded is not None and not missing_inputs
    score = diagnostic if complete else 0.0
    return {
        "key": key,
        "label": label,
        "max_points": maximum,
        "awarded_points": round(score, 6),
        "diagnostic_points": round(diagnostic, 6),
        "complete": complete,
        "upper_bound": round(score if complete else maximum, 6),
        "evidence_level": evidence_level if complete else "partial" if awarded is not None else "missing",
        "formula": formula,
        "inputs": dict(inputs),
        "evidence_refs": {str(key): dict(value) for key, value in (evidence_refs or {}).items()},
        "missing_inputs": missing_inputs,
        "source_rule": source_rule,
    }


def _classification(metric: Mapping[str, Any]) -> dict[str, Any]:
    industry = str(metric.get("industry") or "")
    gross_window = _annual_history_window(
        metric,
        "gross_margin_history",
        "gross_margin_years",
        minimum_years=5,
    )
    gross_history = gross_window["values"]
    gross_std_pp = pstdev(gross_history) * 100.0 if gross_window["complete"] is True else None
    c_margin = (
        3.0
        if gross_std_pp is not None and gross_std_pp > 15
        else 2.0
        if gross_std_pp is not None and gross_std_pp >= 10
        else 1.0
        if gross_std_pp is not None
        else None
    )

    revenue = _series(metric, "revenue_values", "revenue_years")
    profit = _series(metric, "net_profit_history", "net_profit_years")
    revenue_profit_window = _recent_annual_window(
        {year: (revenue[year], profit[year]) for year in set(revenue) & set(profit)},
        latest_financial_year=_latest_financial_year(metric),
    )
    elasticities: list[float] = []
    if revenue_profit_window["complete"] is True:
        for previous, current in zip(
            revenue_profit_window["years"],
            revenue_profit_window["years"][1:],
        ):
            if revenue[previous] <= 0 or profit[previous] <= 0:
                continue
            revenue_change = revenue[current] / revenue[previous] - 1.0
            profit_change = profit[current] / profit[previous] - 1.0
            if abs(revenue_change) >= 0.01:
                elasticities.append(abs(profit_change / revenue_change))
    elasticity = median(elasticities) if len(elasticities) >= 2 else None
    c_elasticity = (
        3.0
        if elasticity is not None and elasticity > 2
        else 2.0
        if elasticity is not None and elasticity >= 1
        else 1.0
        if elasticity is not None
        else None
    )
    capex_window = _ratio_history(metric, "capex_history", "revenue_values")
    capex_ratios = capex_window["values"]
    capex_ratio = math.fsum(capex_ratios) / 5.0 if capex_window["complete"] is True and len(capex_ratios) == 5 else None
    c_capex = (
        2.0
        if capex_ratio is not None and capex_ratio > 0.25
        else 1.0
        if capex_ratio is not None and capex_ratio >= 0.10
        else 0.0
        if capex_ratio is not None
        else None
    )
    c_driver = (
        1.0
        if industry in DIRECT_COMMODITY_INDUSTRIES
        else 0.5
        if industry in CYCLICAL_INDUSTRIES
        else 0.0
        if industry
        else None
    )

    rd = _finite(metric.get("rd_intensity"))
    t_rd = (
        3.0
        if rd is not None and rd > 0.12
        else 2.0
        if rd is not None and rd >= 0.08
        else 1.0
        if rd is not None and rd >= 0.04
        else 0.0
        if rd is not None
        else None
    )
    t_intangible = (
        1.0 if industry in CORE_TECH_INDUSTRIES else 0.5 if industry in TECH_INDUSTRIES else 0.0 if industry else None
    )
    t_iteration = (
        1.0 if industry in CORE_TECH_INDUSTRIES else 0.5 if industry in TECH_INDUSTRIES else 0.0 if industry else None
    )
    tech_score, tech_level, tech_evidence = _metric_score(metric, "technology_score")
    business_score, business_level, business_evidence = _metric_score(metric, "business_model_score")
    platform_base = _average(tech_score, business_score)
    platform_cap = 3.0 if industry in {"SOFTWARE", "TELECOM", "MEDIA"} else 2.0
    t_platform = None if platform_base is None else platform_base / 10.0 * platform_cap

    n_repeat = (
        2.0
        if industry in ESSENTIAL_INDUSTRIES
        else 1.0
        if industry in {"RETAIL", "HOME_APPLIANCE"}
        else 0.0
        if industry
        else None
    )
    stability = _stability_score(metric)
    n_beta = None if stability is None else 1.0 if stability >= 8 else 0.5 if stability >= 5 else 0.0
    gross = _finite(metric.get("gross_margin"))
    gross_cv = _finite(metric.get("gross_margin_cv"))
    pricing_observable = _average(
        _linear(gross, [(0.20, 0), (0.40, 1), (0.60, 2), (0.80, 3)]),
        _linear(gross_cv, [(0.0, 3), (0.05, 2.5), (0.10, 1.5), (0.20, 0)]),
    )
    moat_score, moat_level, moat_evidence = _metric_score(metric, "moat_score")
    n_mind = (
        None
        if not industry and moat_score is None
        else min(2.0, (1.0 if industry in LICENSE_OR_SCARCITY_INDUSTRIES else 0.0) + (moat_score or 0.0) / 10.0)
    )

    components = {
        "C": [
            _classification_component(
                "c_margin_volatility",
                "毛利率五年波动",
                3,
                c_margin,
                formula="3 if std_pp>15;2 if >=10;else 1",
                inputs={
                    "gross_margin_std_pp": gross_std_pp,
                    "observations": len(gross_history),
                    "used_years": list(gross_window["years"]),
                    "latest_financial_year": gross_window["latest_financial_year"],
                    "annual_window_current": gross_window["current"],
                },
                evidence_level="reported_observable",
                source_rule="annual gross-margin history",
            ),
            _classification_component(
                "c_profit_elasticity",
                "利润/收入弹性",
                3,
                c_elasticity,
                formula="3 if median_abs_dProfit_dRevenue>2;2 if >=1;else 1",
                inputs={
                    "median_elasticity": elasticity,
                    "observations": len(elasticities),
                    "used_years": list(revenue_profit_window["years"]),
                    "latest_financial_year": revenue_profit_window["latest_financial_year"],
                    "annual_window_current": revenue_profit_window["current"],
                },
                evidence_level="reported_observable",
                source_rule="aligned annual revenue and parent-profit history",
            ),
            _classification_component(
                "c_capex_intensity",
                "资本开支强度",
                2,
                c_capex,
                formula="2 if mean_capex_revenue>25%;1 if >=10%;else 0",
                inputs={
                    "mean_capex_revenue": capex_ratio,
                    "observations": len(capex_ratios),
                    "used_years": list(capex_window["years"]),
                    "latest_financial_year": capex_window["latest_financial_year"],
                    "annual_window_current": capex_window["current"],
                },
                evidence_level="reported_observable",
                source_rule="aligned annual capex and revenue",
            ),
            _classification_component(
                "c_commodity_driver",
                "商品价格/产能驱动",
                2,
                c_driver,
                formula="industry proxy: direct commodity=1;broad cyclical=0.5;other=0 (source item max=2)",
                inputs={"industry": industry},
                evidence_level="derived_proxy",
                source_rule="industry proxy capped at half the source maximum; not primary product-price evidence",
            ),
        ],
        "T": [
            _classification_component(
                "t_rd_intensity",
                "研发费用率",
                3,
                t_rd,
                formula="3 if rd>12%;2 if >=8%;1 if >=4%;else 0",
                inputs={"rd_intensity": rd},
                evidence_level="reported_observable",
                source_rule="latest annual R&D/revenue",
            ),
            _classification_component(
                "t_intangible_patent",
                "专利/无形资产密集",
                2,
                t_intangible,
                formula="industry proxy: core technology=1;technology=0.5;other=0 (source item max=2)",
                inputs={"industry": industry},
                evidence_level="derived_proxy",
                source_rule="industry proxy capped at half the source maximum; not patent evidence",
            ),
            _classification_component(
                "t_iteration",
                "技术/产品迭代周期",
                2,
                t_iteration,
                formula="industry proxy: core technology=1;technology=0.5;other=0 (source item max=2)",
                inputs={"industry": industry},
                evidence_level="derived_proxy",
                source_rule="industry-cycle proxy capped at half the source maximum; not primary product-roadmap evidence",
            ),
            _classification_component(
                "t_platform",
                "网络效应/平台生态",
                3,
                t_platform,
                formula="mean(technology_score,business_model_score)/10*sector_cap",
                inputs={"technology_score": tech_score, "business_model_score": business_score, "cap": platform_cap},
                evidence_level="primary" if tech_level == business_level == "primary" else "derived_proxy",
                source_rule="validated score or observable-outcome proxy; capped by sector",
                evidence_refs={
                    key: value
                    for key, value in {
                        "technology_score": tech_evidence,
                        "business_model_score": business_evidence,
                    }.items()
                    if value is not None
                },
            ),
        ],
        "N": [
            _classification_component(
                "n_repeat",
                "复购/必选",
                3,
                n_repeat,
                formula="industry proxy: essential=2;repeat-consumption=1;other=0 (source item max=3)",
                inputs={"industry": industry},
                evidence_level="derived_proxy",
                source_rule="industry demand proxy below the source maximum; not primary purchase-frequency evidence",
            ),
            _classification_component(
                "n_macro_beta",
                "低宏观敏感度",
                2,
                n_beta,
                formula="stability proxy=1 if >=8;0.5 if >=5;else 0 (source item max=2)",
                inputs={"stability_score": stability},
                evidence_level="derived_proxy",
                source_rule="profit/growth stability proxy capped at half the source maximum; not measured GDP beta",
            ),
            _classification_component(
                "n_pricing",
                "定价权",
                3,
                pricing_observable,
                formula="mean(gross-margin level score,gross-margin stability score)",
                inputs={"gross_margin": gross, "gross_margin_cv": gross_cv},
                evidence_level="reported_observable",
                source_rule="reported margin outcome proxy; not direct price-volume study",
            ),
            _classification_component(
                "n_mindshare",
                "刚需/心智垄断",
                2,
                n_mind,
                formula="scarcity-sector point + moat_score/10, capped 2",
                inputs={"industry": industry, "moat_score": moat_score},
                evidence_level="primary" if moat_level == "primary" else "derived_proxy",
                source_rule="validated moat or observable-outcome proxy plus sector scarcity",
                evidence_refs={"moat_score": moat_evidence} if moat_evidence is not None else {},
            ),
        ],
    }
    scores = {key: round(math.fsum(item["awarded_points"] for item in items), 6) for key, items in components.items()}
    upper = {key: round(math.fsum(item["upper_bound"] for item in items), 6) for key, items in components.items()}
    class_code = "T" if scores["T"] >= 7.0 else "C" if scores["C"] >= 7.0 else "W"
    if scores["T"] >= 7.0:
        possible_classes = ["T"]
    elif upper["T"] >= 7.0:
        possible_classes = ["T"]
        if upper["C"] >= 7.0:
            possible_classes.append("C")
        if scores["C"] < 7.0:
            possible_classes.append("W")
    elif scores["C"] >= 7.0:
        possible_classes = ["C"]
    elif upper["C"] >= 7.0:
        possible_classes = ["C", "W"]
    else:
        possible_classes = ["W"]
    missing_components = [
        f"{sensitivity}.{item['key']}"
        for sensitivity, items in components.items()
        for item in items
        if item["complete"] is not True
    ]
    secondary = _secondary_features(class_code, scores)
    possible_secondary = _possible_secondary_features(class_code, scores, upper)
    return {
        "rule": CLASSIFICATION_RULE,
        "class_code": class_code,
        "class_label": CLASS_LABELS[class_code],
        "sensitivity_scores": scores,
        "sensitivity_upper_bounds": upper,
        "route_complete": len(possible_classes) == 1,
        "possible_classes": possible_classes,
        "missing_components": missing_components,
        "secondary_features": secondary,
        "possible_secondary_features": possible_secondary,
        "components": components,
        "basis": f"T={scores['T']:.2f}, C={scores['C']:.2f}, N={scores['N']:.2f}; routed to {CLASS_LABELS[class_code]}",
    }


def _build_items(
    metric: Mapping[str, Any], class_code: str, classification: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    industry = str(metric.get("industry") or "")
    business, _business_level, business_evidence = _metric_score(metric, "business_model_score")
    moat, moat_level, moat_evidence = _metric_score(metric, "moat_score")
    durability, _durability_level, durability_evidence = _metric_score(metric, "moat_durability_score")
    runway, runway_level, runway_evidence = _metric_score(metric, "runway_score")
    growth_sustain, _growth_level, growth_sustain_evidence = _metric_score(metric, "growth_sustainability_score")
    technology, technology_level, technology_evidence = _metric_score(metric, "technology_score")
    management, _management_level, management_evidence = _metric_score(metric, "management_alignment_score")
    industry_durability, _industry_level, industry_evidence = _metric_score(metric, "industry_durability_score")
    evidence_by_input = {
        "business_model_score": business_evidence,
        "moat_score": moat_evidence,
        "moat_durability_score": durability_evidence,
        "durability_score": durability_evidence,
        "runway_score": runway_evidence,
        "growth_sustainability_score": growth_sustain_evidence,
        "technology_score": technology_evidence,
        "management_alignment_score": management_evidence,
        "industry_durability_score": industry_evidence,
    }
    gross = _gross_score(metric)
    cash, fcf_window = _cash_conversion_score(metric)
    asset_light, capex_intensity, capex_window = _asset_light_score(metric)
    growth = _growth_score(_finite(metric.get("trend_growth")))
    stability = _stability_score(metric)
    roic = _roic_score(metric)
    history, history_years, history_used_years, history_latest_year, history_current = _history_score(metric)
    margin_cv = _finite(metric.get("gross_margin_cv"))
    margin_stability = _linear(margin_cv, [(0.0, 10), (0.05, 9), (0.10, 7), (0.20, 4), (0.40, 1)])
    fcf_positive = (
        10.0 * sum(value > 0 for value in fcf_window["values"]) / len(fcf_window["values"])
        if fcf_window["complete"] is True
        else None
    )
    fcf_window_inputs = {
        "fcf_used_years": list(fcf_window["years"]),
        "latest_financial_year": fcf_window["latest_financial_year"],
        "annual_window_current": fcf_window["current"],
    }
    capex_window_inputs = {
        "capex_revenue_used_years": list(capex_window["years"]),
        "latest_financial_year": capex_window["latest_financial_year"],
        "annual_window_current": capex_window["current"],
    }
    history_window_inputs = {
        "history_used_years": history_used_years,
        "latest_financial_year": history_latest_year,
        "history_window_current": history_current,
    }
    debt_ratio = _finite(metric.get("interest_bearing_debt_ratio"))
    debt_score = _linear(debt_ratio, [(0.0, 10), (0.10, 9), (0.30, 6), (0.50, 3), (0.80, 0)])
    n_score = _finite(classification.get("sensitivity_scores", {}).get("N"))
    c_score = _finite(classification.get("sensitivity_scores", {}).get("C"))
    c_upper = _finite(classification.get("sensitivity_upper_bounds", {}).get("C"))
    classification_components = classification.get("components")
    c_components = classification_components.get("C") if isinstance(classification_components, Mapping) else None
    c_complete = bool(
        isinstance(c_components, list)
        and len(c_components) == 4
        and all(isinstance(item, Mapping) and item.get("complete") is True for item in c_components)
    )

    def atom(
        dimension: str,
        key: str,
        value: float | None,
        formula: str,
        inputs: Mapping[str, Any],
        source_rule: str,
        *,
        cap: float | None = None,
        level: str = "derived_proxy",
    ) -> dict[str, Any]:
        return _atom(
            key,
            DIMENSION_ITEM_WEIGHTS[class_code][dimension][key],
            value,
            formula=formula,
            inputs=inputs,
            source_rule=source_rule,
            evidence_level=level,
            proxy_cap=cap,
            evidence_refs={
                input_key: evidence_by_input[input_key]
                for input_key, input_value in inputs.items()
                if input_value is not None and evidence_by_input.get(input_key) is not None
            },
        )

    if class_code == "W":
        pricing = _average(gross, moat)
        repeat = _average(n_score, business)
        license_score = _average(moat, 8.0 if industry in LICENSE_OR_SCARCITY_INDUSTRIES else 3.0)
        items = {
            "BM": [
                atom(
                    "BM",
                    "pricing_power",
                    pricing,
                    "mean(gross_outcome,moat)",
                    {"gross_outcome": gross, "moat_score": moat},
                    "reported margins plus validated/observable moat",
                    cap=9.0,
                    level="derived_proxy",
                ),
                atom(
                    "BM",
                    "fcf_conversion",
                    cash,
                    "mean(OCF/net-profit score,FCF-positive share)",
                    {
                        "ocf_np_ratio": _finite(metric.get("ocf_np_ratio")),
                        "fcf_positive_score": fcf_positive,
                        **fcf_window_inputs,
                    },
                    "reported annual cash flow",
                    level="reported_observable",
                ),
                atom(
                    "BM",
                    "repeat_demand",
                    repeat,
                    "mean(N_sensitivity,business_model_score)",
                    {"N_sensitivity": n_score, "business_model_score": business},
                    "sector demand plus validated/observable business-model proxy",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "BM",
                    "asset_light",
                    asset_light,
                    "inverse_piecewise(median_capex/revenue)",
                    {"capex_revenue_median": capex_intensity, **capex_window_inputs},
                    "aligned annual capex and revenue",
                    level="reported_observable",
                ),
            ],
            "MOAT": [
                atom(
                    "MOAT",
                    "brand_mindshare",
                    _average(moat, gross),
                    "mean(moat,gross_outcome)",
                    {"moat_score": moat, "gross_outcome": gross},
                    "observable economic outcome proxy",
                    cap=8.5,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "network_switching",
                    _average(moat, business),
                    "mean(moat,business_model)",
                    {"moat_score": moat, "business_model_score": business},
                    "validated score or observable outcome; no direct customer-switch survey",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "license_scarcity",
                    license_score,
                    "mean(moat,sector_scarcity_anchor)",
                    {"moat_score": moat, "sector_anchor": 8.0 if industry in LICENSE_OR_SCARCITY_INDUSTRIES else 3.0},
                    "sector scarcity proxy; not primary licence/resource evidence",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "time_thickness",
                    None if history is None else _average(history, durability),
                    "mean(listing/reporting span,moat durability)",
                    {
                        "history_years": history_years,
                        "history_score": history,
                        "durability_score": durability,
                        **history_window_inputs,
                    },
                    "observable listed history plus durability evidence",
                    cap=9.0,
                    level="derived_proxy",
                ),
            ],
            "G": [
                atom(
                    "G",
                    "volume_price_space",
                    _average(growth, pricing),
                    "mean(trend_growth score,pricing-power score)",
                    {
                        "trend_growth": _finite(metric.get("trend_growth")),
                        "growth_score": growth,
                        "pricing_score": pricing,
                    },
                    "reported revenue trend and pricing outcome",
                    level="reported_observable",
                ),
                atom(
                    "G",
                    "category_expansion",
                    _average(runway, growth_sustain),
                    "mean(runway,growth_sustainability)",
                    {"runway_score": runway, "growth_sustainability_score": growth_sustain},
                    "observable outcome proxy; not primary product map",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "inflation_pass_through",
                    _average(pricing, margin_stability),
                    "mean(pricing-power score,margin stability)",
                    {"pricing_score": pricing, "margin_stability": margin_stability},
                    "margin preservation proxy; not direct price-volume inflation study",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "certainty",
                    stability,
                    "mean(inverse growth CV,inverse profit volatility)",
                    {
                        "growth_consistency": _finite(metric.get("growth_consistency")),
                        "profit_volatility": _finite(metric.get("profit_volatility")),
                    },
                    "reported annual stability",
                    level="reported_observable",
                ),
            ],
        }
    elif class_code == "T":
        rd_score = _linear(_finite(metric.get("rd_intensity")), [(0.0, 0), (0.04, 3), (0.08, 6), (0.12, 9), (0.18, 10)])
        cycle_overlay_penalty = (
            _cycle_overlay_penalty(c_score) if c_complete else 0.0 if c_upper is not None and c_upper < 7.0 else None
        )
        cashflow_base = _average(cash, fcf_positive, growth)
        cashflow_score = (
            None
            if fcf_window["complete"] is not True or cashflow_base is None or cycle_overlay_penalty is None
            else cashflow_base - cycle_overlay_penalty
        )
        items = {
            "BM": [
                atom(
                    "BM",
                    "rd_conversion",
                    _average(rd_score, growth, roic),
                    "mean(RD intensity,revenue growth,ROIC spread)",
                    {"rd_intensity_score": rd_score, "growth_score": growth, "roic_score": roic},
                    "reported R&D and commercial outcomes; not patent proof",
                    level="reported_observable",
                ),
                atom(
                    "BM",
                    "revenue_quality",
                    _average(business, stability, cash),
                    "mean(business model,stability,cash conversion)",
                    {"business_model_score": business, "stability_score": stability, "cash_conversion": cash},
                    "recurring-income outcome proxy; not contract-level recurring revenue",
                    cap=8.5,
                    level="derived_proxy",
                ),
                atom(
                    "BM",
                    "declining_marginal_cost",
                    _average(gross, asset_light, roic),
                    "mean(gross outcome,asset-light,ROIC spread)",
                    {"gross_outcome": gross, "asset_light": asset_light, "roic_score": roic},
                    "reported scale-economics proxy",
                    cap=9.0,
                    level="derived_proxy",
                ),
                atom(
                    "BM",
                    "cashflow_inflection",
                    cashflow_score,
                    "mean(cash conversion,FCF-positive share,growth)-cycle overlay penalty",
                    {
                        "cash_conversion": cash,
                        "fcf_positive_score": fcf_positive,
                        "growth_score": growth,
                        "cycle_overlay_penalty": cycle_overlay_penalty,
                        **fcf_window_inputs,
                    },
                    "reported annual cash-flow path; C>=7 deducts 0.5..2.0 points",
                    level="reported_observable",
                ),
            ],
            "MOAT": [
                atom(
                    "MOAT",
                    "patent_standard",
                    _average(technology, rd_score),
                    "mean(technology score,RD intensity)",
                    {"technology_score": technology, "rd_intensity_score": rd_score},
                    "technology outcome proxy; not primary patent/standard inventory",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "talent_retention",
                    _average(management, technology),
                    "mean(management alignment,technology)",
                    {"management_alignment_score": management, "technology_score": technology},
                    "management/technology proxy; not direct staff retention data",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "data_network",
                    _average(moat, business, technology),
                    "mean(moat,business model,technology)",
                    {"moat_score": moat, "business_model_score": business, "technology_score": technology},
                    "observable outcome proxy; not direct network graph",
                    cap=9.0 if industry in {"SOFTWARE", "TELECOM", "MEDIA"} else 8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "platform_lockin",
                    _average(moat, business),
                    "mean(moat,business model)",
                    {"moat_score": moat, "business_model_score": business},
                    "observable platform proxy; not primary switching-cost study",
                    cap=9.0,
                    level="derived_proxy",
                ),
            ],
            "G": [
                atom(
                    "G",
                    "s_curve_relay",
                    _average(growth, runway, technology),
                    "mean(growth,runway,technology)",
                    {"growth_score": growth, "runway_score": runway, "technology_score": technology},
                    "reported growth plus technology/runway proxy",
                    cap=9.0 if runway_level == technology_level == "primary" else 8.5,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "tam_space",
                    _average(runway, industry_durability, growth),
                    "mean(runway,industry durability,growth)",
                    {"runway_score": runway, "industry_durability_score": industry_durability, "growth_score": growth},
                    "observable TAM proxy; not primary market-size study",
                    cap=8.5,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "nonlinear_option",
                    _average(technology, rd_score, growth_sustain),
                    "mean(technology,RD intensity,growth sustainability)",
                    {
                        "technology_score": technology,
                        "rd_intensity_score": rd_score,
                        "growth_sustainability_score": growth_sustain,
                    },
                    "technology option proxy; not probability-weighted pipeline",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "disruption_resilience",
                    _average(durability, stability),
                    "mean(moat durability,operating stability)",
                    {"moat_durability_score": durability, "stability_score": stability},
                    "inverse disruption-risk proxy",
                    cap=9.0,
                    level="derived_proxy",
                ),
            ],
        }
    else:
        sector_anchor = 9.0 if industry in DIRECT_COMMODITY_INDUSTRIES else 6.0
        survival = (
            _average(history, fcf_positive, debt_score)
            if history is not None and fcf_window["complete"] is True
            else None
        )
        items = {
            "BM": [
                atom(
                    "BM",
                    "cost_curve",
                    _average(gross, roic, stability),
                    "mean(margin outcome,ROIC spread,stability)",
                    {"gross_outcome": gross, "roic_score": roic, "stability_score": stability},
                    "reported outcome proxy; no peer unit-cost curve",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "BM",
                    "integration_self_supply",
                    _average(business, cash, asset_light),
                    "mean(business model,cash conversion,capital intensity discipline)",
                    {"business_model_score": business, "cash_conversion": cash, "asset_light": asset_light},
                    "observable integration proxy; not primary self-supply disclosure",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "BM",
                    "cash_conversion",
                    cash,
                    "mean(OCF/net-profit score,FCF-positive share)",
                    {
                        "ocf_np_ratio": _finite(metric.get("ocf_np_ratio")),
                        "fcf_positive_score": fcf_positive,
                        **fcf_window_inputs,
                    },
                    "reported annual cash flow",
                    level="reported_observable",
                ),
                atom(
                    "BM",
                    "capacity_discipline",
                    _average(asset_light, debt_score, roic) if asset_light is not None else None,
                    "mean(inverse capex intensity,balance-sheet discipline,ROIC)",
                    {
                        "capex_revenue_median": capex_intensity,
                        "debt_score": debt_score,
                        "roic_score": roic,
                        **capex_window_inputs,
                    },
                    "reported capital-allocation outcome proxy",
                    cap=8.0,
                    level="derived_proxy",
                ),
            ],
            "MOAT": [
                atom(
                    "MOAT",
                    "resource_scarcity",
                    _average(moat, sector_anchor),
                    "mean(moat,direct-commodity sector anchor)",
                    {"moat_score": moat, "sector_anchor": sector_anchor},
                    "sector scarcity proxy; not reserve-life statement",
                    cap=9.0 if moat_level == "primary" else 8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "cost_lead",
                    _average(gross, roic, margin_stability),
                    "mean(margin outcome,ROIC,margin stability)",
                    {"gross_outcome": gross, "roic_score": roic, "margin_stability": margin_stability},
                    "reported outcome proxy; no peer cash-cost spread",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "scale_location",
                    _average(moat, business, roic),
                    "mean(moat,business model,ROIC)",
                    {"moat_score": moat, "business_model_score": business, "roic_score": roic},
                    "scale/location outcome proxy; not primary site inventory",
                    cap=7.0,
                    level="derived_proxy",
                ),
                atom(
                    "MOAT",
                    "cycle_survival",
                    survival,
                    "mean(history span,FCF-positive share,debt resilience)",
                    {
                        "history_years": history_years,
                        "history_score": history,
                        "fcf_positive_score": fcf_positive,
                        "debt_score": debt_score,
                        **fcf_window_inputs,
                        "history_used_years": history_used_years,
                        "history_window_current": history_current,
                    },
                    "reported multi-year survival outcomes",
                    level="reported_observable",
                ),
            ],
            "G": [
                atom(
                    "G",
                    "low_cost_expansion",
                    _average(growth, roic, business),
                    "mean(growth,ROIC,business model)",
                    {"growth_score": growth, "roic_score": roic, "business_model_score": business},
                    "low-cost expansion outcome proxy; not project-by-project cost curve",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "integration_gain",
                    _average(cash, gross, roic),
                    "mean(cash conversion,margin outcome,ROIC)",
                    {"cash_conversion": cash, "gross_outcome": gross, "roic_score": roic},
                    "reported integration-benefit outcome proxy",
                    cap=8.0,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "commodity_trend",
                    _average(industry_durability, growth),
                    "mean(industry durability,company trend growth)",
                    {"industry_durability_score": industry_durability, "growth_score": growth},
                    "long-term commodity trend proxy; no live commodity curve",
                    cap=7.0,
                    level="derived_proxy",
                ),
                atom(
                    "G",
                    "certainty",
                    _average(stability, None if c_score is None else 10.0 - c_score),
                    "mean(operating stability,inverse cycle sensitivity)",
                    {"stability_score": stability, "cycle_sensitivity": c_score},
                    "cycle-constrained certainty proxy",
                    cap=7.0,
                    level="derived_proxy",
                ),
            ],
        }
    return items


def _gdN_filter_gate(metric: Mapping[str, Any], class_code: str) -> dict[str, Any]:
    """后续附加补丁们 3331：gdN 可投滤网。

    可投须满足 ① g>0 且 d>0；或 ② g>0 且留存转更高未来 g（研发强度足
    够高）；或 ③ g≈0 但 d 高（烟蒂）。secular 衰退 + 不分红 = 两引擎全
    灭，坚决不投。强周期谷底的暂时性 g<0 须先做周期归一化，不可误杀：
    已被商品周期证据确认强周期属性（type5_cycle_attribute_score≥7）的
    C 类公司在谷底不因 g<0 被滤掉。
    """
    g = _bounded(metric.get("trend_growth"), -1.0, 1.0)
    trailing_cash = _bounded(metric.get("trailing_cash_per_share"), 0.0, 1e12)
    price = _bounded(metric.get("price"), 0.0, 1e6)
    rd_intensity = _bounded(metric.get("rd_intensity"), 0.0, 1.0)
    dividend_yield = trailing_cash / price if (trailing_cash is not None and price and price > 0) else 0.0
    cycle_confirmed = _bounded(metric.get("type5_cycle_attribute_score"), 0.0, 10.0)
    cycle_confirmed = cycle_confirmed is not None and cycle_confirmed >= 7.0

    inputs = {
        "g": g,
        "d": dividend_yield,
        "rd_intensity": rd_intensity,
        "trailing_cash_per_share": trailing_cash,
        "cycle_confirmed": cycle_confirmed,
    }
    basis = "gdN 可投滤网：g 增长引擎 × d 分红引擎 × N 时间"
    if g is None:
        return {
            "complete": False,
            "passed": False,
            "required": True,
            "basis": basis,
            "rule": "缺增长引擎证据（g 无法判定）",
            "inputs": inputs,
            "missing_inputs": ["g"],
        }
    if g > 0 and dividend_yield > 0:
        passed = True
        rule = "① g>0 且 d>0：双引擎正常"
    elif g > 0 and rd_intensity is not None and rd_intensity >= 0.03:
        passed = True
        rule = "② g>0 且留存转更高未来 g（研发强度≥3%）"
    elif abs(g) <= 0.02 and dividend_yield >= 0.04:
        passed = True
        rule = "③ g≈0 但 d 高（烟蒂，股息率≥4%）"
    elif class_code == "C" and cycle_confirmed:
        passed = True
        rule = "强周期谷底周期归一化豁免（商品周期证据确认，g 暂时为负不误杀）"
    elif g < 0 and dividend_yield <= 0:
        passed = False
        rule = "secular 衰退且不分红：g 与 d 两引擎全灭，坚决不投"
    else:
        passed = False
        rule = "gdN 引擎不成立：g≤0 且股息率不足（非烟蒂、非留存转增长）"
    return {
        "complete": True,
        "passed": passed,
        "required": True,
        "basis": basis,
        "rule": rule,
        "inputs": inputs,
        "missing_inputs": [],
    }


def _future_fcf_gate(metric: Mapping[str, Any], class_code: str) -> dict[str, Any]:
    """Replayable Patch 7 future-FCF premise with a technology path exception."""

    window = _annual_history_window(metric, "fcf_history", "fcf_years")
    ordered_years = list(window["years"])
    values = list(window["values"])
    latest_financial_year = window["latest_financial_year"]
    durable_complete = window["complete"] is True
    positive_share = sum(value > 0 for value in values) / len(values) if values else None
    latest_fcf = values[-1] if values else None
    durable_passed = bool(
        durable_complete
        and latest_fcf is not None
        and latest_fcf > 0
        and positive_share is not None
        and positive_share >= 0.60
    )

    recent_three_years = ordered_years[-3:] if len(ordered_years) >= 3 else []
    recent_three_values = values[-3:] if len(values) >= 3 else []
    improvement_amounts = [
        current - previous for previous, current in zip(recent_three_values, recent_three_values[1:])
    ]
    improvement_periods = [
        f"{previous}年至{current}年" for previous, current in zip(recent_three_years, recent_three_years[1:])
    ]
    median_improvement = median(improvement_amounts) if len(improvement_amounts) == 2 else None
    estimated_years_to_positive = (
        0.0
        if latest_fcf is not None and latest_fcf > 0
        else max(0.0, -latest_fcf / median_improvement)
        if latest_fcf is not None and median_improvement is not None and median_improvement > 0
        else None
    )

    ocf = _series(metric, "ocf_history", "ocf_years")
    annual_ocf_years = (
        [year for year in ocf if latest_financial_year is None or year <= latest_financial_year] if ocf else []
    )
    latest_ocf_year = max(annual_ocf_years) if annual_ocf_years else None
    latest_ocf = ocf.get(latest_ocf_year) if latest_ocf_year is not None else None
    ocf_current = bool(
        latest_ocf_year is not None and latest_financial_year is not None and latest_ocf_year == latest_financial_year
    )
    strictly_improving = len(improvement_amounts) == 2 and all(amount > 0 for amount in improvement_amounts)
    turnaround_path_complete = bool(class_code == "T" and durable_complete and ocf_current and latest_ocf is not None)
    turnaround_path_passed = bool(
        turnaround_path_complete
        and strictly_improving
        and latest_ocf is not None
        and latest_ocf > 0
        and estimated_years_to_positive is not None
        and estimated_years_to_positive <= 2.0
    )

    if class_code != "T":
        complete = durable_complete
        passed = durable_passed
    elif durable_passed:
        complete = True
        passed = True
    else:
        complete = turnaround_path_complete
        passed = turnaround_path_passed

    if durable_passed:
        matched_mode = "耐久正自由现金流"
        basis = "命中耐久正自由现金流：最新FCF为正，且当前连续3至5年FCF正值占比不低于60%。"
    elif turnaround_path_passed:
        matched_mode = "强科技清晰转正路径"
        basis = (
            "命中强科技清晰转正路径：最近3年FCF严格逐年改善，最新年度经营现金流为正，"
            f"按最近两次改善额中位数线性外推约{estimated_years_to_positive:.2f}年转正。"
        )
    elif not complete:
        matched_mode = "证据不完整"
        if class_code == "T" and durable_complete and not ocf_current:
            basis = f"证据不完整：耐久正FCF条件未通过，缺少{latest_financial_year}年经营现金流。"
        else:
            basis = "证据不完整：缺少绑定最新完整财年的连续3至5年FCF。"
    else:
        matched_mode = "未命中"
        basis = (
            "未命中：耐久正FCF条件和两年内清晰转正路径均未通过。"
            if class_code == "T"
            else "未命中：最新FCF须为正，且当前连续3至5年FCF正值占比不低于60%。"
        )

    rule = (
        "强科技：满足耐久正FCF，或最近3年FCF严格逐年改善、最新年度经营现金流为正，"
        "且按最近两次改善额中位数线性外推不超过2年转正"
        if class_code == "T"
        else "最新FCF为正，且绑定最新完整财年的连续3至5年FCF正值占比不低于60%"
    )
    return {
        "class_code": class_code,
        "complete": complete,
        "passed": passed,
        "years": ordered_years,
        "values": values,
        "latest_financial_year": latest_financial_year,
        "positive_share": positive_share,
        "latest_fcf": latest_fcf,
        "recent_three_years": recent_three_years,
        "recent_three_values": recent_three_values,
        "improvement_periods": improvement_periods,
        "improvement_amounts": improvement_amounts,
        "median_improvement": median_improvement,
        "latest_ocf_year": latest_ocf_year,
        "latest_ocf": latest_ocf,
        "estimated_years_to_positive": estimated_years_to_positive,
        "durable_condition_passed": durable_passed,
        "turnaround_path_complete": turnaround_path_complete,
        "turnaround_path_passed": turnaround_path_passed,
        "matched_mode": matched_mode,
        "basis": basis,
        "rule": rule,
        "scope": "补丁7未来自由现金流前置条件；历史事实与线性外推不构成预测保证",
    }


def _pb_percentile_score(percentile: float | None) -> float | None:
    if percentile is None or not 0.0 <= percentile <= 1.0:
        return None
    return (
        10.0
        if percentile <= 0.10
        else 8.0
        if percentile <= 0.20
        else 6.0
        if percentile <= 0.30
        else 4.0
        if percentile <= 0.50
        else 2.0
    )


def _cycle_pb_score(percentile: float | None, current_pb: float | None) -> float | None:
    percentile_score = _pb_percentile_score(percentile)
    if percentile_score is None or current_pb is None or current_pb <= 0.0:
        return None
    absolute_score = (
        10.0
        if current_pb <= 1.0
        else 8.0
        if current_pb <= 1.2
        else 6.0
        if current_pb <= 1.5
        else 4.0
        if current_pb <= 2.0
        else 2.0
    )
    return min(percentile_score, absolute_score)


_TYPE5_ROUTE_WEIGHTS = {"5a": 0.35, "5b": 0.25, "5c": 0.20, "5d": 0.10, "5e": 0.10}


def _classified_route_gates(
    class_code: str,
    *,
    valuation_evidence_complete: bool,
    valuation_score: float | None,
    route_evidence: Mapping[str, Any] | None,
    price_required: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the class-specific Patch 6 route and price gates.

    The latest patch explicitly forbids applying the weak-cycle DCF ruler to
    technology and commodity-cycle companies.  All normalized source facts
    used here are retained in the ledger so the validator can replay the gate.
    """

    raw = route_evidence if isinstance(route_evidence, Mapping) else {}
    # Type 7 quality certification is published separately.  A Type 7
    # ``buy_ready`` decision always has to pass its own class price ruler;
    # another framework may trigger independently but cannot waive this gate.
    price_required = True
    if class_code == "W":
        raw_template5_items = raw.get("template5_valuation_items")
        template5_maximum_points = {"t5_v1": 9.0, "t5_v2": 12.0, "t5_v3": 9.0}
        template5_valuation_items = {
            key: {
                "complete": (
                    raw_template5_items.get(key, {}).get("complete")
                    if isinstance(raw_template5_items, Mapping)
                    and isinstance(raw_template5_items.get(key), Mapping)
                    and type(raw_template5_items[key].get("complete")) is bool
                    else None
                ),
                "points": (
                    _bounded(raw_template5_items.get(key, {}).get("points"), 0.0, maximum)
                    if isinstance(raw_template5_items, Mapping) and isinstance(raw_template5_items.get(key), Mapping)
                    else None
                ),
            }
            for key, maximum in template5_maximum_points.items()
        }
        template5_valuation_complete = all(
            item["complete"] is True and item["points"] is not None for item in template5_valuation_items.values()
        )
        template5_valuation_score = (
            math.fsum(float(item["points"]) for item in template5_valuation_items.values()) / 3.0
            if template5_valuation_complete
            else None
        )
        route_inputs: dict[str, Any] = {
            "template5_valuation_items": template5_valuation_items,
            "template5_valuation_score": template5_valuation_score,
            "patch5_safety_complete": (
                raw.get("patch5_safety_complete") if type(raw.get("patch5_safety_complete")) is bool else None
            ),
            "patch5_safety_score": _bounded(raw.get("patch5_safety_score"), 0.0, 20.0),
        }
        route_complete = bool(
            template5_valuation_complete
            and route_inputs["patch5_safety_complete"] is True
            and route_inputs["patch5_safety_score"] is not None
        )
        route_passed = bool(route_complete and float(route_inputs["patch5_safety_score"]) >= 8.0)
        route_basis = "弱周期：DCF/模板5估值路径"
        route_rule = "模板5估值三项资料完整，且补丁5安全边际>=8/20"
        price_source_complete = valuation_evidence_complete is True
        price_score = _bounded(valuation_score, 0.0, 10.0)
        price_inputs = {"type1_buy_zone_score": price_score}
        price_minimum = 3.0
        price_basis = "经来源绑定的Type1买入区深度"
        price_rule = "弱周期价格位置分>=3，视为合理或低估"
    elif class_code == "C":
        route_inputs = {
            "type5_applicable": raw.get("type5_applicable") if type(raw.get("type5_applicable")) is bool else None,
            "type5_cycle_complete": (
                raw.get("type5_cycle_complete") if type(raw.get("type5_cycle_complete")) is bool else None
            ),
            "type5_cycle_score": _bounded(raw.get("type5_cycle_score"), 0.0, 10.0),
            "type5_bottom_complete": (
                raw.get("type5_bottom_complete") if type(raw.get("type5_bottom_complete")) is bool else None
            ),
            "type5_bottom_score": _bounded(raw.get("type5_bottom_score"), 0.0, 10.0),
            "type5_survival_complete": (
                raw.get("type5_survival_complete") if type(raw.get("type5_survival_complete")) is bool else None
            ),
            "type5_survival_score": _bounded(raw.get("type5_survival_score"), 0.0, 10.0),
            "type5_upside_complete": (
                raw.get("type5_upside_complete") if type(raw.get("type5_upside_complete")) is bool else None
            ),
            "type5_upside_score": _bounded(raw.get("type5_upside_score"), 0.0, 10.0),
            "type5_valuation_complete": (
                raw.get("type5_valuation_complete") if type(raw.get("type5_valuation_complete")) is bool else None
            ),
            "type5_valuation_score": _bounded(raw.get("type5_valuation_score"), 0.0, 10.0),
            "type5_evidence_complete": (
                raw.get("type5_evidence_complete") if type(raw.get("type5_evidence_complete")) is bool else None
            ),
            "type5_triggered": raw.get("type5_triggered") if type(raw.get("type5_triggered")) is bool else None,
            "type5_total": _bounded(raw.get("type5_total"), 0.0, 10.0),
            "template25_complete": (
                raw.get("template25_complete") if type(raw.get("template25_complete")) is bool else None
            ),
            "template25_buy_zone_score": _bounded(raw.get("template25_buy_zone_score"), 0.0, 10.0),
            "monetary_funds": _bounded(raw.get("monetary_funds"), 0.0, 1e18),
            "interest_debt": _bounded(raw.get("interest_debt"), 0.0, 1e18),
            "net_debt": _finite(raw.get("net_debt")),
        }
        type5_score_inputs = {
            "5a": route_inputs["type5_cycle_score"],
            "5b": route_inputs["type5_bottom_score"],
            "5c": route_inputs["type5_survival_score"],
            "5d": route_inputs["type5_upside_score"],
            "5e": route_inputs["type5_valuation_score"],
        }
        replayed_type5_total = (
            round(
                math.fsum(float(type5_score_inputs[key]) * weight for key, weight in _TYPE5_ROUTE_WEIGHTS.items()),
                1,
            )
            if all(value is not None for value in type5_score_inputs.values())
            else None
        )
        replayed_type5_trigger = bool(
            route_inputs["type5_applicable"] is True
            and route_inputs["type5_evidence_complete"] is True
            and route_inputs["type5_cycle_complete"] is True
            and route_inputs["type5_bottom_complete"] is True
            and route_inputs["type5_survival_complete"] is True
            and route_inputs["type5_upside_complete"] is True
            and route_inputs["type5_valuation_complete"] is True
            and type5_score_inputs["5a"] is not None
            and float(type5_score_inputs["5a"]) >= 7.0
            and replayed_type5_total is not None
            and replayed_type5_total >= 7.0
        )
        route_inputs["type5_replayed_total"] = replayed_type5_total
        route_inputs["type5_replayed_triggered"] = replayed_type5_trigger
        net_debt_consistent = bool(
            route_inputs["monetary_funds"] is not None
            and route_inputs["interest_debt"] is not None
            and route_inputs["net_debt"] is not None
            and math.isclose(
                float(route_inputs["net_debt"]),
                float(route_inputs["interest_debt"]) - float(route_inputs["monetary_funds"]),
                abs_tol=1e-6,
            )
        )
        route_complete = bool(
            isinstance(route_inputs["type5_applicable"], bool)
            and route_inputs["type5_cycle_complete"] is True
            and route_inputs["type5_cycle_score"] is not None
            and route_inputs["type5_bottom_complete"] is True
            and route_inputs["type5_bottom_score"] is not None
            and route_inputs["type5_survival_complete"] is True
            and route_inputs["type5_survival_score"] is not None
            and route_inputs["type5_upside_complete"] is True
            and route_inputs["type5_upside_score"] is not None
            and route_inputs["type5_valuation_complete"] is True
            and route_inputs["type5_valuation_score"] is not None
            and route_inputs["type5_evidence_complete"] is True
            and isinstance(route_inputs["type5_triggered"], bool)
            and route_inputs["type5_total"] is not None
            and replayed_type5_total is not None
            and math.isclose(float(route_inputs["type5_total"]), replayed_type5_total, abs_tol=1e-9)
            and route_inputs["type5_triggered"] is replayed_type5_trigger
            and route_inputs["template25_complete"] is True
            and route_inputs["template25_buy_zone_score"] is not None
            and net_debt_consistent
        )
        route_passed = bool(route_complete and replayed_type5_trigger is True)
        route_basis = "强周期：完整情况五与已扣净债的多情景估值路径"
        route_rule = "情况五证据完整且总分>=7，并明确用带息债务减货币资金核对净债"
        price_source_complete = raw.get("pb_history_complete") is True
        pb_percentile = _bounded(raw.get("pb_percentile"), 0.0, 1.0)
        current_pb = _bounded(raw.get("current_pb"), 0.000001, 1_000.0)
        price_score = _cycle_pb_score(pb_percentile, current_pb)
        price_inputs = {"pb_percentile": pb_percentile, "current_pb": current_pb}
        price_minimum = 8.0
        price_basis = "经来源绑定的五年PB历史分位与当前PB"
        price_rule = "主锚破净（PB<1）；近破净量化为 PB≤1.2 且五年分位≤20%"
    else:
        route_inputs = {
            "patch4_complete": raw.get("patch4_complete") if type(raw.get("patch4_complete")) is bool else None,
            "patch4_score": _bounded(raw.get("patch4_score"), 0.0, 20.0),
            "patch5_coverage": _bounded(raw.get("patch5_coverage"), 0.0, 1.0),
            "patch5_safety_complete": (
                raw.get("patch5_safety_complete") if type(raw.get("patch5_safety_complete")) is bool else None
            ),
            "patch5_safety_score": _bounded(raw.get("patch5_safety_score"), 0.0, 20.0),
        }
        route_complete = bool(
            route_inputs["patch4_complete"] is True
            and route_inputs["patch4_score"] is not None
            and route_inputs["patch5_coverage"] is not None
            and 0.0 <= float(route_inputs["patch5_coverage"]) <= 1.0
            and route_inputs["patch5_safety_complete"] is True
            and route_inputs["patch5_safety_score"] is not None
        )
        route_passed = bool(
            route_complete
            and float(route_inputs["patch5_coverage"]) >= 0.80
            and float(route_inputs["patch5_safety_score"]) >= 8.0
        )
        route_basis = "强科技：补丁4股东文化→补丁5路径"
        route_rule = "补丁4证据完整、补丁5覆盖>=80%且安全边际>=8/20"
        price_source_complete = raw.get("pb_history_complete") is True
        pb_percentile = _bounded(raw.get("pb_percentile"), 0.0, 1.0)
        current_pb = _bounded(raw.get("current_pb"), 0.000001, 1_000.0)
        price_score = _pb_percentile_score(pb_percentile)
        price_inputs = {"pb_percentile": pb_percentile, "current_pb": current_pb}
        price_minimum = 8.0
        price_basis = "经来源绑定的五年PB历史分位"
        price_rule = "强科技五年PB历史分位得分>=8（不高于20%分位）"

    price_evidence_complete = bool(price_source_complete and price_score is not None and 0.0 <= price_score <= 10.0)
    price_complete = bool(not price_required or price_evidence_complete)
    price_passed = bool(
        not price_required or (price_evidence_complete and price_score is not None and price_score >= price_minimum)
    )
    return (
        {
            "class_code": class_code,
            "complete": route_complete,
            "passed": route_passed,
            "basis": route_basis,
            "inputs": route_inputs,
            "rule": route_rule,
        },
        {
            "class_code": class_code,
            "required": bool(price_required),
            "source_evidence_complete": bool(price_source_complete),
            "complete": price_complete,
            "passed": price_passed,
            "buy_zone_score": price_score,
            "minimum_score": price_minimum,
            "basis": price_basis,
            "inputs": price_inputs,
            "rule": price_rule,
        },
    )


def assess_patch6_type7(
    metric: Mapping[str, Any],
    *,
    valuation_evidence_complete: bool = False,
    valuation_score: float | None = None,
    route_evidence: Mapping[str, Any] | None = None,
    legacy_diagnostic: Mapping[str, Any] | None = None,
    price_required: bool = True,
) -> dict[str, Any]:
    """Build the classified Type 7 formula ledger."""

    classification = _classification(metric)
    class_code = str(classification["class_code"])
    item_groups = _build_items(metric, class_code, classification)
    dimensions: dict[str, Any] = {}
    missing_items: list[str] = []
    for dimension in ("BM", "MOAT", "G"):
        items = item_groups[dimension]
        raw_score = math.fsum(float(item["score"]) * float(item["weight"]) for item in items)
        upper_bound = math.fsum(float(item["upper_bound"]) * float(item["weight"]) for item in items)
        coverage = math.fsum(float(item["weight"]) for item in items if item["complete"])
        missing_items.extend(f"{dimension}.{item['key']}" for item in items if not item["complete"])
        dimensions[dimension] = {
            "score": round(raw_score, 9),
            "upper_bound": round(upper_bound, 9),
            "coverage": round(coverage, 6),
            "complete": all(item["complete"] for item in items),
            "items": items,
        }
    if classification["route_complete"] is not True:
        missing_items.extend(f"CLASSIFICATION.{key}" for key in classification["missing_components"])
    quality_missing_items = list(missing_items)
    quality_complete = not quality_missing_items
    fcf_gate = _future_fcf_gate(metric, class_code)
    gdN_gate = _gdN_filter_gate(metric, class_code)
    route_gate, valuation_gate = _classified_route_gates(
        class_code,
        valuation_evidence_complete=valuation_evidence_complete,
        valuation_score=valuation_score,
        route_evidence=route_evidence,
        price_required=price_required,
    )
    if fcf_gate["complete"] is not True:
        missing_items.append("PRECONDITION.future_fcf")
    if gdN_gate["complete"] is not True:
        missing_items.append("PRECONDITION.gdN_investability")
    if route_gate["complete"] is not True:
        missing_items.append("ROUTE.class_specific_path")
    if valuation_gate["complete"] is not True:
        missing_items.append("VALUATION.price_reasonableness")
    unrounded_mean = math.fsum(float(dimensions[key]["score"]) for key in ("BM", "MOAT", "G")) / 3.0
    chosen_class_upper_mean = math.fsum(float(dimensions[key]["upper_bound"]) for key in ("BM", "MOAT", "G")) / 3.0
    # When routing is unresolved, another admissible class uses different
    # atoms and weights.  A ceiling from the provisional class is not globally
    # safe; retain the full 0..10 bound until C/T/N routing is determined.
    upper_mean = 10.0 if classification["route_complete"] is not True else chosen_class_upper_mean
    complete = not missing_items
    veto_dimensions = [
        dimension
        for dimension in ("BM", "MOAT")
        if class_code == "C"
        and (
            (dimensions[dimension]["complete"] is True and float(dimensions[dimension]["score"]) < 5.0)
            or float(dimensions[dimension]["upper_bound"]) < 5.0
        )
    ]
    veto = bool(veto_dimensions)
    quality_certified = bool(quality_complete and unrounded_mean > STRICT_THRESHOLD and not veto)
    condition_failures = [
        key
        for key, gate in {
            "future_fcf": fcf_gate,
            "gdN_investability": gdN_gate,
            "route_path": route_gate,
            "price_reasonableness": valuation_gate,
        }.items()
        if gate["complete"] is True and gate["passed"] is not True
    ]
    if (
        class_code == "T"
        and quality_complete
        and any(float(dimensions[key]["score"]) < 7.0 for key in ("BM", "MOAT", "G"))
    ):
        condition_failures.append("technology_dimension_floor")
    trigger = bool(quality_certified and complete and not condition_failures)
    old = dict(legacy_diagnostic or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "code": str(metric.get("code") or ""),
        "as_of": str(metric.get("source_trade_date") or ""),
        "source_rule": MODEL_SOURCE_RULE,
        "strict_threshold": STRICT_THRESHOLD,
        "classification": classification,
        "dimension_weights": {"BM": 1.0 / 3.0, "MOAT": 1.0 / 3.0, "G": 1.0 / 3.0},
        "decision_gates": {
            "future_fcf": fcf_gate,
            "gdN_investability": gdN_gate,
            "route_path": route_gate,
            "price_reasonableness": valuation_gate,
        },
        "dimensions": dimensions,
        "scores": {key: dimensions[key]["score"] for key in ("BM", "MOAT", "G")},
        "unrounded_mean": unrounded_mean,
        "score": round(unrounded_mean, 3),
        "upper_bound": round(upper_mean, 3),
        "quality_complete": quality_complete,
        "quality_certified": quality_certified,
        "complete": complete,
        "missing_items": missing_items,
        "veto": veto,
        "veto_dimensions": veto_dimensions,
        "condition_failures": condition_failures,
        "triggered": trigger,
        "buy_ready": trigger,
        "legacy_diagnostic": {
            "model_id": old.get("model_id"),
            "source_rule": old.get("source_rule"),
            "scores": old.get("scores"),
            "prerequisites_complete": old.get("prerequisites_complete"),
            "triggered": old.get("triggered"),
            "decisive": False,
            "note": LEGACY_DIAGNOSTIC_NOTE,
        },
        # C/T valuation uses a class-appropriate five-year PB history.  Fetch
        # it only while the mathematical ceiling can still clear the strict
        # Type 7 threshold.
        "history_request_needed": bool(
            class_code in {"C", "T"} and valuation_gate["complete"] is not True and upper_mean > STRICT_THRESHOLD
        ),
        "research_request_needed": False,
    }


def validate_patch6_type7_ledger(
    value: Any,
    *,
    expected_code: str | None = None,
    expected_as_of: str | None = None,
) -> list[str]:
    """Replay the complete arithmetic/shape contract without trusting totals."""

    if not isinstance(value, Mapping):
        return ["ledger is not a mapping"]
    errors: list[str] = []
    expected_fields = {
        "schema_version",
        "model_id",
        "code",
        "as_of",
        "source_rule",
        "strict_threshold",
        "classification",
        "dimension_weights",
        "decision_gates",
        "dimensions",
        "scores",
        "unrounded_mean",
        "score",
        "upper_bound",
        "quality_complete",
        "quality_certified",
        "complete",
        "missing_items",
        "veto",
        "veto_dimensions",
        "condition_failures",
        "triggered",
        "buy_ready",
        "legacy_diagnostic",
        "history_request_needed",
        "research_request_needed",
    }
    if set(value) != expected_fields:
        errors.append("ledger structure mismatch")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("model_id") != MODEL_ID:
        errors.append("identity mismatch")
    dimension_weights = value.get("dimension_weights")
    if (
        value.get("source_rule") != MODEL_SOURCE_RULE
        or _finite(value.get("strict_threshold")) != STRICT_THRESHOLD
        or not isinstance(dimension_weights, Mapping)
        or set(dimension_weights) != {"BM", "MOAT", "G"}
        or any(
            _finite(dimension_weights[key]) is None
            or not math.isclose(float(dimension_weights[key]), 1.0 / 3.0, abs_tol=1e-15)
            for key in dimension_weights
        )
    ):
        errors.append("model rule mismatch")
    code = str(value.get("code") or "")
    as_of = str(value.get("as_of") or "")
    try:
        parsed_as_of = date.fromisoformat(as_of) if as_of else None
    except ValueError:
        parsed_as_of = None
    date_required = bool(as_of or (expected_as_of is not None and str(expected_as_of)))
    if (
        len(code) != 6
        or not code.isdigit()
        or (date_required and (parsed_as_of is None or parsed_as_of > date.today()))
        or (expected_code is not None and code != str(expected_code))
        or (expected_as_of is not None and as_of != str(expected_as_of))
    ):
        errors.append("company/date binding mismatch")
    classification = value.get("classification")
    if not isinstance(classification, Mapping) or classification.get("class_code") not in CLASS_LABELS:
        return errors + ["classification invalid"]
    if set(classification) != {
        "rule",
        "class_code",
        "class_label",
        "sensitivity_scores",
        "sensitivity_upper_bounds",
        "route_complete",
        "possible_classes",
        "missing_components",
        "secondary_features",
        "possible_secondary_features",
        "components",
        "basis",
    }:
        errors.append("classification structure mismatch")
    class_code = str(classification["class_code"])
    canonical_classification = _classification({})
    canonical_classification_metadata = {
        str(record["key"]): (str(record["formula"]), str(record["source_rule"]))
        for records in canonical_classification["components"].values()
        for record in records
    }
    canonical_items = _build_items({}, class_code, canonical_classification)
    canonical_item_metadata = {
        (dimension, str(item["key"])): (str(item["formula"]), str(item["source_rule"]))
        for dimension, items in canonical_items.items()
        for item in items
    }
    sensitivity = classification.get("sensitivity_scores")
    components = classification.get("components")
    if not isinstance(sensitivity, Mapping) or not isinstance(components, Mapping):
        errors.append("classification components invalid")
    else:
        replayed: dict[str, float] = {}
        replayed_upper: dict[str, float] = {}
        replayed_classification_missing: list[str] = []
        for key in ("C", "T", "N"):
            records = components.get(key)
            if not isinstance(records, list) or len(records) != 4:
                errors.append(f"classification {key} components invalid")
                continue
            record_keys = [str(record.get("key") or "") for record in records if isinstance(record, Mapping)]
            if len(record_keys) != 4 or set(record_keys) != set(_CLASSIFICATION_COMPONENTS[key]):
                errors.append(f"classification {key} component set invalid")
                continue
            total = 0.0
            upper_total = 0.0
            for record in records:
                if not isinstance(record, Mapping):
                    errors.append(f"classification {key} item invalid")
                    continue
                awarded = _finite(record.get("awarded_points"))
                diagnostic = _finite(record.get("diagnostic_points"))
                maximum = _finite(record.get("max_points"))
                upper = _finite(record.get("upper_bound"))
                inputs = record.get("inputs")
                evidence_refs = record.get("evidence_refs")
                component_key = str(record.get("key") or "")
                expected_label, expected_maximum = _CLASSIFICATION_COMPONENTS[key][component_key]
                replayed_value = (
                    _classification_formula_value(component_key, inputs) if isinstance(inputs, Mapping) else None
                )
                expected_diagnostic = (
                    min(float(maximum), max(0.0, float(replayed_value)))
                    if maximum is not None and replayed_value is not None
                    else 0.0
                )
                expected_missing = (
                    [name for name, value in inputs.items() if value is None] if isinstance(inputs, Mapping) else []
                )
                expected_complete = bool(replayed_value is not None and not expected_missing)
                expected_awarded = expected_diagnostic if expected_complete else 0.0
                expected_upper = expected_awarded if expected_complete else maximum
                expected_level = (
                    record.get("evidence_level")
                    if expected_complete
                    else "partial"
                    if replayed_value is not None
                    else "missing"
                )
                expected_evidence_keys = {
                    input_key
                    for input_key in _CLASSIFICATION_EVIDENCE_INPUTS.get(component_key, set())
                    if isinstance(inputs, Mapping) and inputs.get(input_key) is not None
                }
                evidence_refs_valid = bool(
                    isinstance(evidence_refs, Mapping)
                    and set(evidence_refs) == expected_evidence_keys
                    and all(
                        _validated_evidence_reference(
                            evidence_refs[input_key],
                            expected_code=code,
                            reference_as_of=as_of,
                        )
                        == evidence_refs[input_key]
                        for input_key in expected_evidence_keys
                    )
                )
                expected_formula, expected_source_rule = canonical_classification_metadata[component_key]
                if (
                    awarded is None
                    or diagnostic is None
                    or maximum is None
                    or upper is None
                    or not 0 <= awarded <= upper <= maximum
                    or not math.isclose(diagnostic, expected_diagnostic, abs_tol=1e-6)
                    or not math.isclose(awarded, expected_awarded, abs_tol=1e-6)
                    or expected_upper is None
                    or not math.isclose(upper, float(expected_upper), abs_tol=1e-6)
                    or not math.isclose(maximum, expected_maximum, abs_tol=1e-12)
                    or record.get("label") != expected_label
                    or record.get("complete") is not expected_complete
                    or record.get("missing_inputs") != expected_missing
                    or record.get("evidence_level")
                    not in {"primary", "reported_observable", "derived_proxy", "partial", "missing"}
                    or (not expected_complete and record.get("evidence_level") != expected_level)
                    or (
                        expected_complete
                        and record.get("evidence_level") not in _CLASSIFICATION_EVIDENCE_LEVELS[component_key]
                    )
                    or not evidence_refs_valid
                    or record.get("formula") != expected_formula
                    or record.get("source_rule") != expected_source_rule
                ):
                    errors.append(f"classification {key} arithmetic invalid")
                    continue
                total += awarded
                upper_total += upper
                if record.get("complete") is not True:
                    replayed_classification_missing.append(f"{key}.{record.get('key')}")
            replayed[key] = round(total, 6)
            replayed_upper[key] = round(upper_total, 6)
            if _finite(sensitivity.get(key)) is None or not math.isclose(
                float(sensitivity[key]), replayed[key], abs_tol=1e-6
            ):
                errors.append(f"classification {key} total mismatch")
        expected_class = "T" if replayed.get("T", 0.0) >= 7.0 else "C" if replayed.get("C", 0.0) >= 7.0 else "W"
        if class_code != expected_class:
            errors.append("classification route mismatch")
        if replayed.get("T", 0.0) >= 7.0:
            possible_classes = ["T"]
        elif replayed_upper.get("T", 10.0) >= 7.0:
            possible_classes = ["T"]
            if replayed_upper.get("C", 10.0) >= 7.0:
                possible_classes.append("C")
            if replayed.get("C", 0.0) < 7.0:
                possible_classes.append("W")
        elif replayed.get("C", 0.0) >= 7.0:
            possible_classes = ["C"]
        elif replayed_upper.get("C", 10.0) >= 7.0:
            possible_classes = ["C", "W"]
        else:
            possible_classes = ["W"]
        expected_secondary = _secondary_features(expected_class, replayed)
        expected_possible_secondary = _possible_secondary_features(expected_class, replayed, replayed_upper)
        expected_basis = (
            f"T={replayed.get('T', 0.0):.2f}, C={replayed.get('C', 0.0):.2f}, "
            f"N={replayed.get('N', 0.0):.2f}; routed to {CLASS_LABELS[expected_class]}"
        )
        if (
            classification.get("possible_classes") != possible_classes
            or classification.get("route_complete") is not (len(possible_classes) == 1)
            or classification.get("missing_components") != replayed_classification_missing
            or classification.get("sensitivity_upper_bounds") != replayed_upper
            or classification.get("class_label") != CLASS_LABELS.get(expected_class)
            or classification.get("rule") != CLASSIFICATION_RULE
            or classification.get("basis") != expected_basis
            or classification.get("secondary_features") != expected_secondary
            or classification.get("possible_secondary_features") != expected_possible_secondary
        ):
            errors.append("classification bounds mismatch")

    decision_gates = value.get("decision_gates")
    replay_gate_missing: list[str] = []
    replay_condition_failures: list[str] = []
    replayed_valuation_complete = False
    if not isinstance(decision_gates, Mapping) or set(decision_gates) != {
        "future_fcf",
        "gdN_investability",
        "route_path",
        "price_reasonableness",
    }:
        errors.append("decision gates invalid")
    else:
        fcf_gate = decision_gates.get("future_fcf")
        if not isinstance(fcf_gate, Mapping):
            errors.append("future FCF gate invalid")
        else:
            expected_fcf_fields = {
                "class_code",
                "complete",
                "passed",
                "years",
                "values",
                "latest_financial_year",
                "positive_share",
                "latest_fcf",
                "recent_three_years",
                "recent_three_values",
                "improvement_periods",
                "improvement_amounts",
                "median_improvement",
                "latest_ocf_year",
                "latest_ocf",
                "estimated_years_to_positive",
                "durable_condition_passed",
                "turnaround_path_complete",
                "turnaround_path_passed",
                "matched_mode",
                "basis",
                "rule",
                "scope",
            }
            years = fcf_gate.get("years")
            raw_values = fcf_gate.get("values")
            latest_financial_year = fcf_gate.get("latest_financial_year")
            parsed_values = [_finite(item) for item in raw_values] if isinstance(raw_values, list) else []
            year_types_valid = bool(
                isinstance(years, list) and all(isinstance(year, int) and not isinstance(year, bool) for year in years)
            )
            valid_years = bool(
                year_types_valid and len(years) == len(set(years)) and years == sorted(years) and len(years) <= 5
            )
            valid_values = bool(
                isinstance(raw_values, list)
                and len(raw_values) == len(years or [])
                and all(item is not None for item in parsed_values)
            )
            consecutive = bool(
                valid_years
                and len(years) >= 3
                and all(current - previous == 1 for previous, current in zip(years, years[1:]))
            )
            current = bool(
                valid_years
                and years
                and isinstance(latest_financial_year, int)
                and not isinstance(latest_financial_year, bool)
                and years[-1] == latest_financial_year
            )
            expected_complete = bool(valid_values and consecutive and current)
            expected_share = (
                sum(float(item) > 0 for item in parsed_values) / len(parsed_values)
                if valid_values and parsed_values
                else None
            )
            expected_latest = float(parsed_values[-1]) if valid_values and parsed_values else None
            durable_passed = bool(
                expected_complete
                and expected_latest is not None
                and expected_latest > 0
                and expected_share is not None
                and expected_share >= 0.60
            )
            expected_recent_years = list(years[-3:]) if valid_years and len(years) >= 3 else []
            expected_recent_values = (
                [float(item) for item in parsed_values[-3:]] if valid_values and len(parsed_values) >= 3 else []
            )
            expected_improvements = [
                current - previous for previous, current in zip(expected_recent_values, expected_recent_values[1:])
            ]
            expected_periods = [
                f"{previous}年至{current}年"
                for previous, current in zip(expected_recent_years, expected_recent_years[1:])
            ]
            expected_median_improvement = median(expected_improvements) if len(expected_improvements) == 2 else None
            expected_estimated_years = (
                0.0
                if expected_latest is not None and expected_latest > 0
                else max(0.0, -expected_latest / expected_median_improvement)
                if expected_latest is not None
                and expected_median_improvement is not None
                and expected_median_improvement > 0
                else None
            )
            latest_ocf_year = fcf_gate.get("latest_ocf_year")
            reported_ocf = fcf_gate.get("latest_ocf")
            parsed_ocf = _finite(reported_ocf)
            ocf_pair_valid = bool(
                (latest_ocf_year is None and reported_ocf is None)
                or (
                    isinstance(latest_ocf_year, int)
                    and not isinstance(latest_ocf_year, bool)
                    and parsed_ocf is not None
                )
            )
            ocf_current = bool(
                ocf_pair_valid
                and latest_ocf_year is not None
                and isinstance(latest_financial_year, int)
                and not isinstance(latest_financial_year, bool)
                and latest_ocf_year == latest_financial_year
            )
            expected_turnaround_complete = bool(class_code == "T" and expected_complete and ocf_current)
            expected_turnaround_passed = bool(
                expected_turnaround_complete
                and len(expected_improvements) == 2
                and all(amount > 0 for amount in expected_improvements)
                and parsed_ocf is not None
                and parsed_ocf > 0
                and expected_estimated_years is not None
                and expected_estimated_years <= 2.0
            )
            if class_code != "T":
                expected_gate_complete = expected_complete
                expected_passed = durable_passed
            elif durable_passed:
                expected_gate_complete = True
                expected_passed = True
            else:
                expected_gate_complete = expected_turnaround_complete
                expected_passed = expected_turnaround_passed

            if durable_passed:
                expected_mode = "耐久正自由现金流"
                expected_basis = "命中耐久正自由现金流：最新FCF为正，且当前连续3至5年FCF正值占比不低于60%。"
            elif expected_turnaround_passed:
                expected_mode = "强科技清晰转正路径"
                expected_basis = (
                    "命中强科技清晰转正路径：最近3年FCF严格逐年改善，最新年度经营现金流为正，"
                    f"按最近两次改善额中位数线性外推约{expected_estimated_years:.2f}年转正。"
                )
            elif not expected_gate_complete:
                expected_mode = "证据不完整"
                if class_code == "T" and expected_complete and not ocf_current:
                    expected_basis = f"证据不完整：耐久正FCF条件未通过，缺少{latest_financial_year}年经营现金流。"
                else:
                    expected_basis = "证据不完整：缺少绑定最新完整财年的连续3至5年FCF。"
            else:
                expected_mode = "未命中"
                expected_basis = (
                    "未命中：耐久正FCF条件和两年内清晰转正路径均未通过。"
                    if class_code == "T"
                    else "未命中：最新FCF须为正，且当前连续3至5年FCF正值占比不低于60%。"
                )
            expected_rule = (
                "强科技：满足耐久正FCF，或最近3年FCF严格逐年改善、最新年度经营现金流为正，"
                "且按最近两次改善额中位数线性外推不超过2年转正"
                if class_code == "T"
                else "最新FCF为正，且绑定最新完整财年的连续3至5年FCF正值占比不低于60%"
            )
            reported_share = _finite(fcf_gate.get("positive_share"))
            reported_latest = _finite(fcf_gate.get("latest_fcf"))
            reported_recent_values = fcf_gate.get("recent_three_values")
            parsed_recent_values = (
                [_finite(item) for item in reported_recent_values] if isinstance(reported_recent_values, list) else []
            )
            reported_improvements = fcf_gate.get("improvement_amounts")
            parsed_improvements = (
                [_finite(item) for item in reported_improvements] if isinstance(reported_improvements, list) else []
            )
            reported_median_improvement = _finite(fcf_gate.get("median_improvement"))
            reported_estimated_years = _finite(fcf_gate.get("estimated_years_to_positive"))
            if (
                set(fcf_gate) != expected_fcf_fields
                or fcf_gate.get("class_code") != class_code
                or fcf_gate.get("complete") is not expected_gate_complete
                or fcf_gate.get("passed") is not expected_passed
                or fcf_gate.get("durable_condition_passed") is not durable_passed
                or fcf_gate.get("turnaround_path_complete") is not expected_turnaround_complete
                or fcf_gate.get("turnaround_path_passed") is not expected_turnaround_passed
                or (expected_share is None) != (reported_share is None)
                or (
                    expected_share is not None
                    and reported_share is not None
                    and not math.isclose(expected_share, reported_share, abs_tol=1e-12)
                )
                or (expected_latest is None) != (reported_latest is None)
                or (
                    expected_latest is not None
                    and reported_latest is not None
                    and not math.isclose(expected_latest, reported_latest, abs_tol=1e-12)
                )
                or fcf_gate.get("recent_three_years") != expected_recent_years
                or len(parsed_recent_values) != len(expected_recent_values)
                or any(item is None for item in parsed_recent_values)
                or any(
                    actual is None or not math.isclose(actual, expected, abs_tol=1e-12)
                    for actual, expected in zip(parsed_recent_values, expected_recent_values)
                )
                or fcf_gate.get("improvement_periods") != expected_periods
                or len(parsed_improvements) != len(expected_improvements)
                or any(item is None for item in parsed_improvements)
                or any(
                    actual is None or not math.isclose(actual, expected, abs_tol=1e-12)
                    for actual, expected in zip(parsed_improvements, expected_improvements)
                )
                or (expected_median_improvement is None) != (reported_median_improvement is None)
                or (
                    expected_median_improvement is not None
                    and reported_median_improvement is not None
                    and not math.isclose(expected_median_improvement, reported_median_improvement, abs_tol=1e-12)
                )
                or not ocf_pair_valid
                or (expected_estimated_years is None) != (reported_estimated_years is None)
                or (
                    expected_estimated_years is not None
                    and reported_estimated_years is not None
                    and not math.isclose(expected_estimated_years, reported_estimated_years, abs_tol=1e-12)
                )
                or fcf_gate.get("matched_mode") != expected_mode
                or fcf_gate.get("basis") != expected_basis
                or fcf_gate.get("rule") != expected_rule
                or fcf_gate.get("scope") != "补丁7未来自由现金流前置条件；历史事实与线性外推不构成预测保证"
            ):
                errors.append("future FCF gate replay mismatch")
            if not expected_gate_complete:
                replay_gate_missing.append("PRECONDITION.future_fcf")
            elif not expected_passed:
                replay_condition_failures.append("future_fcf")

        gdN_gate = decision_gates.get("gdN_investability")
        if not isinstance(gdN_gate, Mapping):
            errors.append("gdN filter gate invalid")
        else:
            expected_gdN_fields = {"complete", "passed", "required", "basis", "rule", "inputs", "missing_inputs"}
            if set(gdN_gate) != expected_gdN_fields or gdN_gate.get("required") is not True:
                errors.append("gdN filter gate invalid")
            else:
                gdN_inputs = gdN_gate.get("inputs")
                if not isinstance(gdN_inputs, Mapping):
                    errors.append("gdN filter gate invalid")
                else:
                    g = _finite(gdN_inputs.get("g"))
                    d = _finite(gdN_inputs.get("d"))
                    rd = _finite(gdN_inputs.get("rd_intensity"))
                    cycle_confirmed = gdN_inputs.get("cycle_confirmed") is True
                    if g is None:
                        expected_complete_gdn = False
                        expected_passed_gdn = False
                    else:
                        expected_complete_gdn = True
                        if g > 0 and d is not None and d > 0:
                            expected_passed_gdn = True
                        elif g > 0 and rd is not None and rd >= 0.03:
                            expected_passed_gdn = True
                        elif abs(g) <= 0.02 and d is not None and d >= 0.04:
                            expected_passed_gdn = True
                        elif class_code == "C" and cycle_confirmed:
                            expected_passed_gdn = True
                        else:
                            expected_passed_gdn = False
                    if (
                        gdN_gate.get("complete") is not expected_complete_gdn
                        or gdN_gate.get("passed") is not expected_passed_gdn
                        or gdN_gate.get("missing_inputs") != ([] if expected_complete_gdn else ["g"])
                    ):
                        errors.append("gdN filter gate replay mismatch")
                    if expected_complete_gdn is not True:
                        replay_gate_missing.append("PRECONDITION.gdN_investability")
                    elif expected_passed_gdn is not True:
                        replay_condition_failures.append("gdN_investability")

        route_gate = decision_gates.get("route_path")
        valuation_gate = decision_gates.get("price_reasonableness")
        if not isinstance(route_gate, Mapping) or not isinstance(valuation_gate, Mapping):
            errors.append("classified route/valuation gate invalid")
        else:
            route_inputs = route_gate.get("inputs")
            valuation_inputs = valuation_gate.get("inputs")
            replay_route_evidence = dict(route_inputs) if isinstance(route_inputs, Mapping) else {}
            replay_route_evidence["pb_history_complete"] = valuation_gate.get("source_evidence_complete")
            if isinstance(valuation_inputs, Mapping):
                replay_route_evidence["pb_percentile"] = valuation_inputs.get("pb_percentile")
                replay_route_evidence["current_pb"] = valuation_inputs.get("current_pb")
            replay_valuation_score = (
                _finite(valuation_inputs.get("type1_buy_zone_score")) if isinstance(valuation_inputs, Mapping) else None
            )
            expected_route, expected_valuation = _classified_route_gates(
                class_code,
                valuation_evidence_complete=valuation_gate.get("source_evidence_complete") is True,
                valuation_score=replay_valuation_score,
                route_evidence=replay_route_evidence,
                price_required=valuation_gate.get("required") is True,
            )
            if dict(route_gate) != expected_route:
                errors.append("classified route gate replay mismatch")
            if dict(valuation_gate) != expected_valuation:
                errors.append("valuation gate replay mismatch")
            replayed_valuation_complete = expected_valuation["complete"] is True
            if expected_route["complete"] is not True:
                replay_gate_missing.append("ROUTE.class_specific_path")
            elif expected_route["passed"] is not True:
                replay_condition_failures.append("route_path")
            if expected_valuation["complete"] is not True:
                replay_gate_missing.append("VALUATION.price_reasonableness")
            elif expected_valuation["passed"] is not True:
                replay_condition_failures.append("price_reasonableness")

    dimensions = value.get("dimensions")
    scores = value.get("scores")
    if not isinstance(dimensions, Mapping) or not isinstance(scores, Mapping):
        return errors + ["dimensions invalid"]
    replay_scores: dict[str, float] = {}
    replay_upper: dict[str, float] = {}
    replay_missing: list[str] = []
    for dimension in ("BM", "MOAT", "G"):
        section = dimensions.get(dimension)
        expected_weights = DIMENSION_ITEM_WEIGHTS[class_code][dimension]
        if not isinstance(section, Mapping) or not isinstance(section.get("items"), list):
            errors.append(f"{dimension} structure invalid")
            continue
        indexed = {item.get("key"): item for item in section["items"] if isinstance(item, Mapping)}
        if len(section["items"]) != 4 or len(indexed) != 4 or set(indexed) != set(expected_weights):
            errors.append(f"{dimension} item set mismatch")
            continue
        total = 0.0
        upper_total = 0.0
        coverage = 0.0
        for key, weight in expected_weights.items():
            item = indexed[key]
            score = _finite(item.get("score"))
            points = _finite(item.get("points"))
            upper = _finite(item.get("upper_bound"))
            inputs = item.get("inputs")
            evidence_refs = item.get("evidence_refs")
            replayed_value = (
                _atomic_formula_value(class_code, dimension, key, inputs) if isinstance(inputs, Mapping) else None
            )
            raw_proxy_cap = item.get("proxy_cap")
            proxy_cap = _finite(raw_proxy_cap)
            proxy_cap_type_valid = raw_proxy_cap is None or proxy_cap is not None
            effective_cap = proxy_cap if proxy_cap is not None else 10.0
            expected_missing = (
                [name for name, value in inputs.items() if value is None] if isinstance(inputs, Mapping) else []
            )
            expected_complete = bool(replayed_value is not None and not expected_missing)
            expected_score = round(
                _clip(replayed_value if replayed_value is not None else 0.0, upper=effective_cap)
                if expected_complete
                else 0.0,
                6,
            )
            expected_upper = expected_score if expected_complete else 10.0
            expected_level = (
                "missing"
                if replayed_value is None
                else "partial"
                if not expected_complete
                else item.get("evidence_level")
            )
            expected_evidence_keys = {
                input_key
                for input_key in _ATOMIC_EVIDENCE_INPUTS
                if isinstance(inputs, Mapping) and inputs.get(input_key) is not None
            }
            evidence_refs_valid = bool(
                isinstance(evidence_refs, Mapping)
                and set(evidence_refs) == expected_evidence_keys
                and all(
                    _validated_evidence_reference(
                        evidence_refs[input_key],
                        expected_code=code,
                        reference_as_of=as_of,
                    )
                    == evidence_refs[input_key]
                    for input_key in expected_evidence_keys
                )
            )
            expected_formula, expected_source_rule = canonical_item_metadata[(dimension, key)]
            if (
                score is None
                or points is None
                or upper is None
                or not isinstance(inputs, Mapping)
                or not math.isclose(float(item.get("weight", -1)), weight, abs_tol=1e-12)
                or not math.isclose(points, score * weight, abs_tol=1e-8)
                or not 0 <= score <= upper <= 10
                or not math.isclose(score, expected_score, abs_tol=1e-6)
                or not math.isclose(upper, expected_upper, abs_tol=1e-6)
                or item.get("complete") is not expected_complete
                or item.get("missing_inputs") != expected_missing
                or item.get("evidence_level")
                not in {"primary", "reported_observable", "derived_proxy", "partial", "missing"}
                or (not expected_complete and item.get("evidence_level") != expected_level)
                or not proxy_cap_type_valid
                or (proxy_cap is not None and not 0 <= proxy_cap <= 10)
                or not _atom_policy_valid(
                    class_code,
                    dimension,
                    key,
                    complete=expected_complete,
                    evidence_level=str(item.get("evidence_level") or ""),
                    proxy_cap=proxy_cap,
                )
                or not evidence_refs_valid
                or item.get("formula") != expected_formula
                or item.get("source_rule") != expected_source_rule
                or item.get("label") != ITEM_LABELS[key]
            ):
                errors.append(f"{dimension}.{key} arithmetic invalid")
                continue
            total += score * weight
            upper_total += upper * weight
            if item.get("complete") is True:
                coverage += weight
            else:
                replay_missing.append(f"{dimension}.{key}")
        replay_scores[dimension] = total
        replay_upper[dimension] = upper_total
        if not math.isclose(float(section.get("score", -1)), total, abs_tol=1e-8):
            errors.append(f"{dimension} total mismatch")
        if not math.isclose(float(section.get("upper_bound", -1)), upper_total, abs_tol=1e-8):
            errors.append(f"{dimension} upper bound mismatch")
        if not math.isclose(float(section.get("coverage", -1)), coverage, abs_tol=1e-8):
            errors.append(f"{dimension} coverage mismatch")
        if section.get("complete") is not math.isclose(coverage, 1.0, abs_tol=1e-12):
            errors.append(f"{dimension} completeness mismatch")
        if _finite(scores.get(dimension)) is None or not math.isclose(float(scores[dimension]), total, abs_tol=1e-8):
            errors.append(f"{dimension} score binding mismatch")
    if isinstance(classification, Mapping) and classification.get("route_complete") is not True:
        replay_missing.extend(f"CLASSIFICATION.{key}" for key in classification.get("missing_components", []))
    replay_quality_missing = list(replay_missing)
    replay_missing.extend(replay_gate_missing)
    mean_score = math.fsum(replay_scores.values()) / 3.0 if len(replay_scores) == 3 else -1.0
    chosen_class_upper_mean = math.fsum(replay_upper.values()) / 3.0 if len(replay_upper) == 3 else -1.0
    upper_mean = (
        10.0
        if isinstance(classification, Mapping) and classification.get("route_complete") is not True
        else chosen_class_upper_mean
    )
    quality_complete = not replay_quality_missing
    complete = not replay_missing
    veto_dimensions = [
        dimension
        for dimension in ("BM", "MOAT")
        if class_code == "C"
        and (
            (
                isinstance(dimensions.get(dimension), Mapping)
                and dimensions[dimension].get("complete") is True
                and replay_scores.get(dimension, 10) < 5
            )
            or replay_upper.get(dimension, 10) < 5
        )
    ]
    veto = bool(veto_dimensions)
    quality_certified = bool(quality_complete and mean_score > STRICT_THRESHOLD and not veto)
    if (
        class_code == "T"
        and quality_complete
        and any(replay_scores.get(dimension, -1.0) < 7.0 for dimension in ("BM", "MOAT", "G"))
    ):
        replay_condition_failures.append("technology_dimension_floor")
    trigger = bool(quality_certified and complete and not replay_condition_failures)
    if _finite(value.get("unrounded_mean")) is None or not math.isclose(
        float(value["unrounded_mean"]), mean_score, abs_tol=1e-8
    ):
        errors.append("mean mismatch")
    if _finite(value.get("score")) is None or not math.isclose(
        float(value["score"]), round(mean_score, 3), abs_tol=1e-9
    ):
        errors.append("display score mismatch")
    if _finite(value.get("upper_bound")) is None or not math.isclose(
        float(value["upper_bound"]), round(upper_mean, 3), abs_tol=1e-9
    ):
        errors.append("upper bound mismatch")
    if (
        value.get("missing_items") != replay_missing
        or value.get("quality_complete") is not quality_complete
        or value.get("quality_certified") is not quality_certified
        or value.get("complete") is not complete
    ):
        errors.append("missing/completeness mismatch")
    if value.get("condition_failures") != replay_condition_failures:
        errors.append("condition failure mismatch")
    if (
        value.get("veto") is not veto
        or value.get("veto_dimensions") != veto_dimensions
        or value.get("triggered") is not trigger
        or value.get("buy_ready") is not trigger
    ):
        errors.append("decision mismatch")
    expected_history_request = bool(
        class_code in {"C", "T"} and not replayed_valuation_complete and upper_mean > STRICT_THRESHOLD
    )
    if (
        value.get("history_request_needed") is not expected_history_request
        or value.get("research_request_needed") is not False
    ):
        errors.append("evidence request decision mismatch")
    legacy = value.get("legacy_diagnostic")
    legacy_absent = bool(
        isinstance(legacy, Mapping)
        and set(legacy) == LEGACY_DIAGNOSTIC_FIELDS
        and legacy.get("decisive") is False
        and legacy.get("note") == LEGACY_DIAGNOSTIC_NOTE
        and all(
            legacy.get(key) is None
            for key in ("model_id", "source_rule", "scores", "prerequisites_complete", "triggered")
        )
    )
    legacy_scores = legacy.get("scores") if isinstance(legacy, Mapping) else None
    legacy_populated = bool(
        isinstance(legacy, Mapping)
        and set(legacy) == LEGACY_DIAGNOSTIC_FIELDS
        and legacy.get("model_id") == LEGACY_DIAGNOSTIC_MODEL_ID
        and legacy.get("source_rule") == LEGACY_DIAGNOSTIC_SOURCE_RULE
        and isinstance(legacy_scores, Mapping)
        and set(legacy_scores) == {"template1", "template5", "patch5"}
        and all(
            _finite(legacy_scores.get(key)) is not None and 0.0 <= float(legacy_scores[key]) <= 100.0
            for key in ("template1", "template5", "patch5")
        )
        and type(legacy.get("prerequisites_complete")) is bool
        and type(legacy.get("triggered")) is bool
        and legacy.get("decisive") is False
        and legacy.get("note") == LEGACY_DIAGNOSTIC_NOTE
    )
    if not (legacy_absent or legacy_populated):
        errors.append("legacy diagnostic boundary invalid")
    return errors


__all__ = [
    "CLASS_LABELS",
    "DIMENSION_ITEM_WEIGHTS",
    "MODEL_ID",
    "SCHEMA_VERSION",
    "STRICT_THRESHOLD",
    "assess_patch6_type7",
    "validate_patch6_type7_ledger",
]
