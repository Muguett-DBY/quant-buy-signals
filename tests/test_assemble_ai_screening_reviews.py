from __future__ import annotations

import json
from pathlib import Path

from tools.assemble_ai_screening_reviews import assemble


MODEL = "opencode-go/ox-alpha-free"


def _candidate(code: str) -> dict[str, str]:
    return {"security_code": code, "type_key": "type1", "name": f"公司{code}"}


def _review(code: str, *, searched: bool) -> dict[str, object]:
    review: dict[str, object] = {
        "schema_version": 2,
        "security_code": code,
        "type_key": "type1",
        "verdict": "caution" if not searched else "confirmed",
        "recommended_action": "manual_review" if not searched else "keep",
        "buy_attractiveness_score": 55 if not searched else 75,
        "ai_action": "watchlist" if not searched else "priority_buy",
        "confidence": "low" if not searched else "medium",
        "summary": "观察，等待进一步核验。" if not searched else "当前建议买入，仍需控制仓位。",
        "key_strengths": ["公司经营质量值得继续核验"],
        "risk_flags": ["下游需求仍需跟踪"],
        "claims": (
            []
            if not searched
            else [
                {
                    "statement": "2025年度营业收入 120 亿元",
                    "source_ref": "https://example.com/report#revenue",
                    "support": "supports",
                },
                {
                    "statement": "2025年度经营现金流 18 亿元",
                    "source_ref": "https://example.com/report#cashflow",
                    "support": "supports",
                },
            ]
        ),
        "model": MODEL,
        "effort": "low",
        "web_search_performed": searched,
        "web_search_event_verified": searched,
        "web_search_claim_urls_verified": searched,
        "web_search_queries": [f"{code} 最新报告"] if searched else [],
        "web_search_verified_claim_urls": ["https://example.com/report"] if searched else [],
        "web_search_dropped_claim_url_count": 0,
    }
    if searched:
        review["quantitative_facts"] = ["2025年度营业收入 120 亿元", "2025年度经营现金流 18 亿元"]
    return review


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_assembler_prefers_search_and_requires_complete_identity(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    local = tmp_path / "local.jsonl"
    search = tmp_path / "search.jsonl"
    output = tmp_path / "merged.jsonl"
    _write_jsonl(candidates, [_candidate("600000"), _candidate("600001")])
    _write_jsonl(local, [_review("600000", searched=False), _review("600001", searched=False)])
    _write_jsonl(search, [_review("600000", searched=True)])

    assert assemble(candidates, [local, search], output) == {"candidate_total": 2, "search": 1, "local": 1}
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["security_code"] for row in rows] == ["600000", "600001"]
    assert rows[0]["web_search_event_verified"] is True
    assert rows[1]["web_search_event_verified"] is False
