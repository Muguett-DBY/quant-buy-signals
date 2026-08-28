"""Build a compact, generation-bound comparison with the prior AI release."""

from __future__ import annotations

from typing import Any, Mapping


_CATEGORY_ORDER = {"recommend_buy": 0, "observe": 1, "do_not_recommend": 2}
_CATEGORY_LABELS = {"recommend_buy": "建议买", "observe": "观察", "do_not_recommend": "不建议"}
_CHANGE_PRIORITY = {
    "category_changed": 0,
    "new_candidate": 1,
    "score_changed": 2,
    "removed_candidate": 3,
}


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def _packets(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for packet in payload.get("packets", []):
        if not isinstance(packet, Mapping):
            continue
        code = _text(packet.get("security_code"), 16)
        if code and code not in result:
            result[code] = packet
    return result


def _review(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    value = packet.get("ai_review")
    return value if isinstance(value, Mapping) else {}


def _category(review: Mapping[str, Any]) -> str:
    value = _text(review.get("final_category"), 32)
    if value in _CATEGORY_ORDER:
        return value
    action = _text(review.get("ai_action"), 32)
    return {"priority_buy": "recommend_buy", "watchlist": "observe", "avoid": "do_not_recommend"}.get(action, "observe")


def _score(review: Mapping[str, Any]) -> float | None:
    try:
        value = float(review.get("buy_attractiveness_score"))
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def _human_points(human: Mapping[str, Any], field: str, fallback: str) -> str:
    values = human.get(field)
    if not isinstance(values, list):
        return fallback
    cleaned: list[str] = []
    for value in values:
        text = _text(value, 180).strip(" 。；：:;")
        if text and text not in cleaned:
            cleaned.append(text)
    return "、".join(cleaned[:2]) or fallback


def _comparison_reason(
    review: Mapping[str, Any],
    *,
    change_type: str,
    previous_category: str | None,
    current_category: str | None,
    direction: str,
) -> str:
    """Turn the current human explanation into a short change-specific sentence.

    The comparison card already shows the yesterday/today path.  This sentence
    therefore explains the evidence shift instead of repeating a generic
    transition or dumping the valuation snapshot a second time.
    """

    human = review.get("human_explanation")
    if not isinstance(human, Mapping):
        return _text(review.get("summary"), 560) or "今天按公司事实重新形成独立结论。"
    support = _human_points(human, "supporting_points", "")
    risk = _human_points(human, "watch_items", "")
    previous_label = _CATEGORY_LABELS.get(previous_category or "", "昨日结论")

    if change_type == "new_candidate":
        if support and risk:
            return f"首次纳入研究，主要依据是{support}；同时要继续盯住{risk}。"
        if support:
            return f"首次纳入研究，主要依据是{support}；后续要确认这项改善能否持续。"
        return "首次纳入研究，当前还没有足够明确的经营亮点，先观察后续证据。"
    if change_type == "score_changed":
        if direction == "score_up":
            if support and risk:
                return f"支撑分数上升的是{support}；{risk}仍是当前最需要验证的地方。"
            if support:
                return f"支撑分数上升的是{support}；还要确认改善能否持续。"
            return "分数较昨日上升，但暂时没有足够新的经营证据，先维持原结论。"
        if risk and support:
            return f"分数下调主要因为{risk}；虽然{support}，但暂不足以改变结论。"
        if risk:
            return f"分数下调主要因为{risk}，这也是当前最需要解决的问题。"
        return "分数较昨日下调，暂未出现足以改变结论的新增支撑。"
    if change_type == "category_changed":
        if direction == "upgraded":
            if support and risk:
                return f"{support}是这次上调的主要依据；但{risk}，暂不把它当成无条件买入。"
            if support:
                return f"{support}是这次上调的主要依据；后续还要确认改善能否持续。"
            if risk:
                return f"结论较昨日上调，但{risk}仍未解决，先观察后续兑现。"
            return "结论较昨日上调，但新增支撑还不够明确，先观察后续兑现。"
        if direction == "downgraded":
            if risk and support:
                return f"{risk}成为当前主要障碍；即使还有{support}，也先不维持原来的{previous_label}判断。"
            if risk:
                return f"{risk}成为当前主要障碍，因此先下调原来的{previous_label}判断。"
            return f"新增证据削弱了原来的{previous_label}判断，先降低结论强度。"
        if support and risk:
            return f"{support}仍在，但{risk}，暂时维持原结论。"
        if risk:
            return f"{risk}尚未解除，暂时维持原结论。"
        return "经营证据变化有限，暂时维持原结论。"
    return _text(review.get("summary"), 560) or "今天按公司事实重新形成独立结论。"


def _reason(
    review: Mapping[str, Any],
    *,
    change_type: str,
    previous_category: str | None,
    current_category: str | None,
    direction: str,
) -> str:
    human = review.get("human_explanation")
    if isinstance(human, Mapping):
        value = _comparison_reason(
            review,
            change_type=change_type,
            previous_category=previous_category,
            current_category=current_category,
            direction=direction,
        )
        if value:
            return value
    return _text(review.get("summary"), 560) or "本日已按公司事实重新形成独立结论。"


def _change_direction(previous: str, current: str) -> str:
    previous_rank = _CATEGORY_ORDER.get(previous, 1)
    current_rank = _CATEGORY_ORDER.get(current, 1)
    if current_rank < previous_rank:
        return "upgraded"
    if current_rank > previous_rank:
        return "downgraded"
    return "unchanged"


def _entry(
    *,
    code: str,
    current_packet: Mapping[str, Any] | None,
    previous_packet: Mapping[str, Any] | None,
    change_type: str,
) -> dict[str, Any]:
    current_review = _review(current_packet) if current_packet else {}
    previous_review = _review(previous_packet) if previous_packet else {}
    current_category = _category(current_review) if current_packet else None
    previous_category = _category(previous_review) if previous_packet else None
    current_score = _score(current_review) if current_packet else None
    previous_score = _score(previous_review) if previous_packet else None
    score_delta = None
    if current_score is not None and previous_score is not None:
        score_delta = round(current_score - previous_score, 2)
    if change_type == "removed_candidate":
        reason = "今天已不在 AI 研究范围，因此没有新的三类结论。"
        direction = "left_candidate_pool"
    else:
        direction = (
            _change_direction(previous_category or "observe", current_category or "observe")
            if change_type == "category_changed"
            else "score_up"
            if (score_delta or 0) > 0
            else "score_down"
        )
        reason = _reason(
            current_review,
            change_type=change_type,
            previous_category=previous_category,
            current_category=current_category,
            direction=direction,
        )
    return {
        "security_code": code,
        "name": _text((current_packet or previous_packet or {}).get("name"), 160),
        "previous_name": _text((previous_packet or {}).get("name"), 160) or None,
        "previous_category": previous_category,
        "previous_label": _CATEGORY_LABELS.get(previous_category or "", previous_category or "—"),
        "previous_score": previous_score,
        "current_category": current_category,
        "current_label": _CATEGORY_LABELS.get(current_category or "", current_category or "—"),
        "current_score": current_score,
        "score_delta": score_delta,
        "change_type": change_type,
        "direction": direction,
        "reason": reason,
    }


def build_day_over_day(current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare company-level conclusions, not deterministic type-pair rows."""

    current_packets = _packets(current)
    previous_packets = _packets(previous or {})
    base = {
        "schema_version": 1,
        "available": bool(previous and previous_packets),
        "current_generation": _text(current.get("snapshot_generation"), 80),
        "current_market_as_of": _text(current.get("market_as_of"), 10),
        "previous_generation": _text((previous or {}).get("snapshot_generation"), 80) or None,
        "previous_market_as_of": _text((previous or {}).get("market_as_of"), 10) or None,
        "current_candidate_count": len(current_packets),
        "previous_candidate_count": len(previous_packets),
        "matched_count": len(current_packets.keys() & previous_packets.keys()),
        "changes": [],
    }
    if not previous or not previous_packets:
        base["unavailable_reason"] = "没有提供上一交易日的 AI 发布包。"
        return base

    changes: list[dict[str, Any]] = []
    for code, current_packet in current_packets.items():
        previous_packet = previous_packets.get(code)
        if previous_packet is None:
            changes.append(
                _entry(code=code, current_packet=current_packet, previous_packet=None, change_type="new_candidate")
            )
            continue
        current_review = _review(current_packet)
        previous_review = _review(previous_packet)
        current_category = _category(current_review)
        previous_category = _category(previous_review)
        current_score = _score(current_review)
        previous_score = _score(previous_review)
        score_changed = (
            current_score is not None and previous_score is not None and abs(current_score - previous_score) >= 5
        )
        if current_category != previous_category:
            changes.append(
                _entry(
                    code=code,
                    current_packet=current_packet,
                    previous_packet=previous_packet,
                    change_type="category_changed",
                )
            )
        elif score_changed:
            changes.append(
                _entry(
                    code=code,
                    current_packet=current_packet,
                    previous_packet=previous_packet,
                    change_type="score_changed",
                )
            )

    for code, previous_packet in previous_packets.items():
        if code not in current_packets:
            changes.append(
                _entry(code=code, current_packet=None, previous_packet=previous_packet, change_type="removed_candidate")
            )

    changes.sort(
        key=lambda item: (
            _CHANGE_PRIORITY.get(str(item["change_type"]), 9),
            0 if item.get("current_category") == "recommend_buy" else 1,
            -abs(float(item.get("score_delta") or 0)),
            str(item.get("security_code") or ""),
        )
    )
    category_changed = sum(item["change_type"] == "category_changed" for item in changes)
    score_changed = sum(item["change_type"] == "score_changed" for item in changes)
    new_candidate = sum(item["change_type"] == "new_candidate" for item in changes)
    removed_candidate = sum(item["change_type"] == "removed_candidate" for item in changes)
    upgraded_to_buy = sum(
        item.get("current_category") == "recommend_buy" and item.get("previous_category") != "recommend_buy"
        for item in changes
        if item["change_type"] in {"category_changed", "new_candidate"}
    )
    downgraded_from_buy = sum(
        item.get("previous_category") == "recommend_buy" and item.get("current_category") != "recommend_buy"
        for item in changes
        if item["change_type"] == "category_changed"
    )
    base.update(
        {
            "change_count": len(changes),
            "category_changed_count": category_changed,
            "score_changed_count": score_changed,
            "new_candidate_count": new_candidate,
            "removed_candidate_count": removed_candidate,
            "upgraded_to_recommend_buy_count": upgraded_to_buy,
            "downgraded_from_recommend_buy_count": downgraded_from_buy,
            "changes": changes,
        }
    )
    return base
