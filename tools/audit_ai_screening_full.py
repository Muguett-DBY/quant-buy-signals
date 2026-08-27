"""Run a row-by-row audit of every published AI screening company.

This is a release audit, not a random sample.  It emits one result for every
company and fails the release summary if any identity, snapshot, score/action,
financial-gate, or explicit cross-company identity check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from tools.ai_screening_identity import explicit_company_codes
from tools.convert_luna_web_reviews import _inferred_fact_unit, _unit_family


_ACTIONS = {"priority_buy", "watchlist", "avoid", "insufficient_evidence"}
_CATEGORIES = {"recommend_buy", "observe", "do_not_recommend"}
_VERDICTS = {"confirmed", "caution", "misclassified", "missed_candidate", "needs_review"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _texts(review: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("summary", "key_strengths", "risk_flags", "quantitative_facts"):
        value = review.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    for claim in review.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        for field in ("statement", "source_context"):
            if isinstance(claim.get(field), str):
                values.append(claim[field])
    return values


_VALUE_WITHOUT_UNIT_RE = re.compile(
    # A timestamp (15:00) and the first leg of a compound value (a/b元/元)
    # are not unit-less financial facts.  Leave those projections to the
    # binding-level audit instead of reporting a false missing-unit error.
    r"(?i)\bvalue\s*[:：]?\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?!\s*[:：/／])"
)
_FACT_UNIT_RE = re.compile(r"\s*(?:[A-Za-z%]|[\u3400-\u9fff])")
_FACT_UNIT_TOKEN_RE = re.compile(
    r"(?:百万元|亿元|万元|千元|元(?:\s*[/／]\s*股)?|亿|万|%|％|万人|人|户|台|吨|万吨|兆瓦|亿千瓦时|股|次|个|项|年|倍|辆|架|发|家|EFLOPS|CNY(?:\s*(?:/\s*(?:share|t)|hundred-million|100\s+million|million))?|RMB|tonnes(?:\s+per\s+year)?|billion|million)"
)


def _financial_fact_audit(review: Mapping[str, Any]) -> tuple[list[str], dict[str, int]]:
    """Audit every lossless fact binding and its readable numeric projection.

    The previous release audit only compared PE/PB fields.  This check is
    deliberately mechanical: it never infers a value or source, and reports
    missing units/dates/bindings instead of silently accepting a bare number.
    """

    errors: list[str] = []
    counts = {
        "binding_count": 0,
        "binding_value_count": 0,
        "binding_missing_unit_count": 0,
        "binding_missing_period_count": 0,
        "binding_unit_mismatch_count": 0,
        "binding_https_source_count": 0,
        "binding_unbound_source_count": 0,
        "quantitative_fact_count": 0,
        "quantitative_fact_missing_unit_count": 0,
        "numeric_repair_count": 0,
    }
    bindings = review.get("financial_fact_bindings")
    if isinstance(bindings, list):
        counts["binding_count"] = len(bindings)
        for binding in bindings:
            if not isinstance(binding, Mapping):
                errors.append("financial_fact_binding_shape")
                continue
            if binding.get("value") is not None:
                counts["binding_value_count"] += 1
                if _number(binding.get("value")) is None:
                    errors.append("financial_fact_value")
                unit = str(binding.get("unit") or binding.get("units") or binding.get("currency") or "").strip()
                if not unit:
                    counts["binding_missing_unit_count"] += 1
                    errors.append("financial_fact_unit")
                else:
                    metric = str(binding.get("metric") or "").strip()
                    # Compact producer keys have deterministic unit semantics;
                    # free-form Chinese quotations may legitimately mention
                    # several metrics and must not be judged by one token.
                    inferred_unit = (
                        _inferred_fact_unit(metric)
                        if metric and re.fullmatch(r"[A-Za-z0-9_]+", metric)
                        else None
                    )
                    if inferred_unit and _unit_family(unit) != _unit_family(inferred_unit):
                        counts["binding_unit_mismatch_count"] += 1
                        errors.append("financial_fact_unit_mismatch")
                period = str(
                    binding.get("period")
                    or binding.get("date")
                    or binding.get("report_period")
                    or binding.get("report_date")
                    or binding.get("as_of")
                    or ""
                ).strip()
                if not period:
                    counts["binding_missing_period_count"] += 1
                    errors.append("financial_fact_period")
                source = str(binding.get("source_url") or "").strip()
                if source:
                    if not source.startswith("https://"):
                        errors.append("financial_fact_source")
                    else:
                        counts["binding_https_source_count"] += 1
                else:
                    counts["binding_unbound_source_count"] += 1
    facts = review.get("quantitative_facts")
    if isinstance(facts, list):
        counts["quantitative_fact_count"] = len(facts)
        for fact in facts:
            if not isinstance(fact, str):
                errors.append("financial_fact_projection_shape")
                continue
            match = _VALUE_WITHOUT_UNIT_RE.search(fact)
            if match and not _FACT_UNIT_TOKEN_RE.search(fact[match.end() :]):
                counts["quantitative_fact_missing_unit_count"] += 1
                errors.append("financial_fact_projection_unit")
    repairs = review.get("numeric_fact_repairs")
    if isinstance(repairs, list):
        counts["numeric_repair_count"] = len(repairs)
        for repair in repairs:
            if not isinstance(repair, Mapping) or not repair.get("old") or not repair.get("new"):
                errors.append("numeric_repair_shape")
                continue
            source = str(repair.get("source_url") or "").strip()
            if source and not source.startswith("https://"):
                errors.append("numeric_repair_source")
    elif repairs is not None:
        errors.append("numeric_repair_shape")
    if bindings is None and any(isinstance(item, str) and _VALUE_WITHOUT_UNIT_RE.search(item) for item in facts or []):
        errors.append("financial_fact_bindings_missing")
    return sorted(set(errors)), counts


def _audit_packet(
    packet: Mapping[str, Any],
    *,
    generation: str,
    market_as_of: str,
) -> dict[str, Any]:
    code = str(packet.get("security_code") or "")
    name = str(packet.get("name") or "")
    review = packet.get("ai_review") if isinstance(packet.get("ai_review"), Mapping) else {}
    errors: list[str] = []
    identity_ok = bool(code and len(code) == 6 and code.isdigit() and name.strip())
    if not identity_ok:
        errors.append("packet_identity")
    context = packet.get("company_context") if isinstance(packet.get("company_context"), Mapping) else {}
    if context:
        if str(context.get("code") or "") != code:
            errors.append("context_code")
        if str(context.get("name") or "").strip() != name.strip():
            errors.append("context_name")
    for field, expected in (
        ("generation", generation),
        ("snapshot_generation", generation),
        ("market_as_of", market_as_of),
    ):
        if field in packet and str(packet.get(field) or "") != expected:
            errors.append(field)
    if str(review.get("security_code") or code) != code:
        errors.append("review_code")
    action = str(review.get("ai_action") or "")
    category = str(review.get("final_category") or "")
    score = _number(review.get("buy_attractiveness_score"))
    verdict = str(review.get("verdict") or "")
    score_ok = score is not None and 0 <= score <= 100
    if action not in _ACTIONS:
        errors.append("action")
    if category not in _CATEGORIES:
        errors.append("category")
    if verdict not in _VERDICTS:
        errors.append("verdict")
    expected_category = {
        "priority_buy": "recommend_buy",
        "watchlist": "observe",
        "avoid": "do_not_recommend",
        "insufficient_evidence": "observe",
    }.get(action)
    if expected_category and category != expected_category:
        errors.append("category_action")
    if action == "priority_buy" and (score is None or score < 70):
        errors.append("buy_score_band")
    if action == "watchlist" and (score is None or not 50 <= score < 70):
        errors.append("observe_score_band")
    if action in {"avoid", "insufficient_evidence"} and (score is None or score >= 50):
        errors.append("negative_score_band")
    if not isinstance(review.get("summary"), str) or len(review["summary"].strip()) < 8:
        errors.append("summary")
    if action in {"priority_buy", "watchlist"} and not any(
        isinstance(value, str) and value.strip() for value in review.get("key_strengths") or []
    ):
        errors.append("strengths")
    if action in {"priority_buy", "watchlist", "avoid"} and not any(
        isinstance(value, str) and value.strip() for value in review.get("risk_flags") or []
    ):
        errors.append("risks")
    adjustments = (
        review.get("calibration_adjustments") if isinstance(review.get("calibration_adjustments"), Mapping) else {}
    )
    adjusted_score = _number(adjustments.get("final_score"))
    if score is None or adjusted_score is None or abs(score - adjusted_score) > 0.11:
        errors.append("score_adjustment")
    quality = review.get("quality_gate") if isinstance(review.get("quality_gate"), Mapping) else {}
    hard_block = quality.get("hard_block") is True or adjustments.get("quality_hard_block") is True
    if action == "priority_buy" and hard_block:
        errors.append("buy_hard_block")
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), Mapping) else {}
    for field in ("pe", "pb"):
        context_value = _number(context.get(field))
        metric_value = _number(metrics.get(field))
        if context_value is not None and metric_value is not None and abs(context_value - metric_value) > 0.02:
            errors.append(f"{field}_mismatch")
    cross_company_codes = sorted(
        {other for text in _texts(review) for other in explicit_company_codes(text) if other != code}
    )
    if cross_company_codes:
        errors.append("cross_company_identity")
    freshness = str(review.get("freshness_status") or "")
    if freshness not in {"current_or_recent", "historical", "undated"}:
        errors.append("freshness")
    financial_errors, financial_counts = _financial_fact_audit(review)
    errors.extend(financial_errors)
    return {
        "security_code": code,
        "name": name,
        "type_key": str(packet.get("type_key") or ""),
        "action": action,
        "final_category": category,
        "score": score,
        "verdict": verdict,
        "quality_hard_block": hard_block,
        "cross_company_codes": cross_company_codes,
        "identity_ok": identity_ok,
        "score_ok": score_ok and not any(item.endswith("_score_band") for item in errors),
        "financial_facts_ok": not any(
            item.startswith("financial_fact_") or item.startswith("numeric_repair_") for item in errors
        ) and not any(item.endswith("_mismatch") for item in errors),
        "financial_fact_counts": financial_counts,
        "review_ok": not errors,
        "errors": errors,
    }


def audit_artifact(
    artifact_path: Path,
    *,
    expected_generation: str | None = None,
    expected_market_as_of: str | None = None,
    expected_count: int | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    generation = str(artifact.get("snapshot_generation") or "")
    market_as_of = str(artifact.get("market_as_of") or "")
    if expected_generation and generation != expected_generation:
        raise ValueError(f"generation mismatch: {generation!r} != {expected_generation!r}")
    if expected_market_as_of and market_as_of != expected_market_as_of:
        raise ValueError(f"market_as_of mismatch: {market_as_of!r} != {expected_market_as_of!r}")
    packets = artifact.get("packets")
    if not isinstance(packets, list):
        raise ValueError("artifact packets are missing")
    rows = [_audit_packet(packet, generation=generation, market_as_of=market_as_of) for packet in packets]
    if expected_count is not None and len(rows) != expected_count:
        raise ValueError(f"packet count mismatch: {len(rows)} != {expected_count}")
    codes = [row["security_code"] for row in rows]
    duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
    for row in rows:
        if row["security_code"] in duplicate_codes:
            row["errors"].append("duplicate_company")
            row["review_ok"] = False
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    issue_count = sum(1 for row in rows if not row["review_ok"])
    result = {
        "artifact": str(artifact_path),
        "snapshot_generation": generation,
        "market_as_of": market_as_of,
        "review_count": len(rows),
        "issue_count": issue_count,
        "identity_pass_count": sum(row["identity_ok"] for row in rows),
        "score_pass_count": sum(row["score_ok"] for row in rows),
        "financial_fact_pass_count": sum(row["financial_facts_ok"] for row in rows),
        "financial_fact_binding_count": sum(row["financial_fact_counts"]["binding_count"] for row in rows),
        "financial_fact_value_count": sum(row["financial_fact_counts"]["binding_value_count"] for row in rows),
        "financial_fact_missing_unit_count": sum(
            row["financial_fact_counts"]["binding_missing_unit_count"]
            for row in rows
        ),
        "financial_fact_unit_mismatch_count": sum(
            row["financial_fact_counts"]["binding_unit_mismatch_count"]
            for row in rows
        ),
        "financial_fact_missing_period_count": sum(
            row["financial_fact_counts"]["binding_missing_period_count"]
            for row in rows
        ),
        "financial_fact_unbound_source_count": sum(
            row["financial_fact_counts"]["binding_unbound_source_count"]
            for row in rows
        ),
        "quantitative_fact_missing_unit_count": sum(
            row["financial_fact_counts"]["quantitative_fact_missing_unit_count"]
            for row in rows
        ),
        "numeric_fact_repair_count": sum(row["financial_fact_counts"]["numeric_repair_count"] for row in rows),
        "action_counts": {
            action: sum(row["action"] == action for row in rows)
            for action in ("priority_buy", "watchlist", "avoid", "insufficient_evidence")
        },
        "cross_company_issue_count": sum("cross_company_identity" in row["errors"] for row in rows),
        "audit_sha256": digest,
        "rows": rows,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--expected-generation")
    parser.add_argument("--expected-market-as-of")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_artifact(
        args.artifact,
        expected_generation=args.expected_generation,
        expected_market_as_of=args.expected_market_as_of,
        expected_count=args.expected_count,
        output_path=args.output,
    )
    print(
        json.dumps({key: value for key, value in result.items() if key != "rows"}, ensure_ascii=False, sort_keys=True)
    )
    return 0 if result["issue_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
