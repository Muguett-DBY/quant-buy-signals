from __future__ import annotations

import json
from pathlib import Path

from tools import run_ai_screening_batch as runner
from tools.run_ai_screening_batch import (
    _company_batches,
    _completed_opencode_websearch_events,
    _completed_opencode_websearch_queries,
    _missing_websearch_companies,
    _opencode_session_id,
    _review_websearch_evidence,
)


def _event(query: str, *, status: str = "completed", output: str = "") -> str:
    return json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "websearch",
                "state": {"status": status, "input": {"query": query}, "output": output},
            },
        }
    )


def test_completed_websearch_events_prove_unique_company_coverage() -> None:
    events = "\n".join(
        [
            _event("002233 塔牌集团 2026 半年度报告"),
            _event("江盐集团 2025 年报"),
            _event("ignored pending search", status="pending"),
        ]
    )
    packets = [
        {"security_code": "002233", "name": "塔牌集团", "type_key": "type1"},
        {"security_code": "002233", "name": "塔牌集团", "type_key": "type5"},
        {"security_code": "601065", "name": "江盐集团", "type_key": "type5"},
    ]
    assert _completed_opencode_websearch_queries(events) == [
        "002233 塔牌集团 2026 半年度报告",
        "江盐集团 2025 年报",
    ]
    assert _missing_websearch_companies(events, packets) == []


def test_company_batches_keep_adjacent_type_pairs_together() -> None:
    packets = [
        {"security_code": "600001", "type_key": "type1"},
        {"security_code": "600001", "type_key": "type7"},
        {"security_code": "600002", "type_key": "type1"},
        {"security_code": "600003", "type_key": "type1"},
        {"security_code": "600003", "type_key": "type5"},
    ]
    batches = _company_batches(packets, 3)
    assert [(start, [row["security_code"] for row in batch]) for start, batch in batches] == [
        (0, ["600001", "600001", "600002"]),
        (3, ["600003", "600003"]),
    ]


def test_search_event_binds_review_claims_to_returned_urls() -> None:
    events = _event(
        "002233 塔牌集团 2026 半年度报告",
        output='{"results":[{"url":"https://static.cninfo.com.cn/report.pdf?x=1"}]}',
    )
    searches = _completed_opencode_websearch_events(events)
    evidence = _review_websearch_evidence(
        searches,
        code="002233",
        companies={"002233": "塔牌集团"},
        claims=[{"source_ref": "https://static.cninfo.com.cn/report.pdf?x=1（公告）"}],
    )
    assert evidence == {
        "queries": ["002233 塔牌集团 2026 半年度报告"],
        "claim_urls": ["https://static.cninfo.com.cn/report.pdf?x=1"],
        "verified_claim_urls": ["https://static.cninfo.com.cn/report.pdf?x=1"],
        "missing_claim_urls": [],
    }


def test_search_event_rejects_claim_url_not_returned_by_tool() -> None:
    searches = _completed_opencode_websearch_events(
        _event("002233 塔牌集团", output="https://www.cninfo.com.cn/actual")
    )
    evidence = _review_websearch_evidence(
        searches,
        code="002233",
        companies={"002233": "塔牌集团"},
        claims=[{"source_ref": "https://example.com/invented"}],
    )
    assert evidence["missing_claim_urls"] == ["https://example.com/invented"]


def test_claim_url_cannot_borrow_another_company_search_result() -> None:
    searches = _completed_opencode_websearch_events(
        "\n".join(
            [
                _event("002233 塔牌集团", output="https://example.com/tapai"),
                _event("600000 浦发银行", output="https://example.com/spdb"),
            ]
        )
    )
    evidence = _review_websearch_evidence(
        searches,
        code="002233",
        companies={"002233": "塔牌集团", "600000": "浦发银行"},
        claims=[{"source_ref": "https://example.com/spdb"}],
    )
    assert evidence["missing_claim_urls"] == ["https://example.com/spdb"]


def test_self_reported_search_without_tool_event_is_not_coverage() -> None:
    packets = [{"security_code": "600000", "name": "浦发银行", "type_key": "type1"}]
    final_text = json.dumps({"web_search_performed": True, "security_code": "600000"})
    assert _missing_websearch_companies(final_text, packets) == ["600000"]


def test_extract_array_unwraps_json_string_transport() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 50,
        "ai_action": "watchlist",
        "confidence": "low",
        "summary": "当前仍需观察经营和估值变化。",
        "key_strengths": ["经营稳定"],
        "risk_flags": ["估值仍待核实"],
        "claims": [],
    }
    escaped = json.dumps(json.dumps([review], ensure_ascii=False), ensure_ascii=False)
    assert runner._extract_array(escaped)[0]["security_code"] == "600000"


def test_extract_array_unescapes_embedded_transport_json() -> None:
    review = {
        "schema_version": 2,
        "security_code": "600000",
        "type_key": "type1",
        "verdict": "caution",
        "recommended_action": "manual_review",
        "buy_attractiveness_score": 50,
        "ai_action": "watchlist",
        "confidence": "low",
    }
    escaped = json.dumps([review], ensure_ascii=False).replace('"', '\\"')
    assert runner._extract_array(escaped)[0]["security_code"] == "600000"


def test_one_combined_query_cannot_claim_separate_company_searches() -> None:
    packets = [
        {"security_code": "002233", "name": "塔牌集团", "type_key": "type1"},
        {"security_code": "600000", "name": "浦发银行", "type_key": "type1"},
    ]
    events = _event("002233 塔牌集团 600000 浦发银行 最新报告")
    assert _missing_websearch_companies(events, packets) == ["002233", "600000"]
    searches = _completed_opencode_websearch_events(events)
    evidence = _review_websearch_evidence(
        searches,
        code="002233",
        companies={"002233": "塔牌集团", "600000": "浦发银行"},
        claims=[],
    )
    assert evidence["queries"] == []


def test_opencode_session_id_is_read_from_the_event_stream() -> None:
    events = "\n".join(
        [
            "not-json",
            json.dumps({"type": "step_start", "sessionID": "ses_screening_reuse"}),
        ]
    )
    assert _opencode_session_id(events) == "ses_screening_reuse"


def test_screening_opencode_config_disables_unrelated_global_mcp_servers() -> None:
    config = json.loads(Path("tools/opencode-screening/opencode.json").read_text(encoding="utf-8"))
    assert config["permission"] == {"*": "deny", "websearch": "allow", "mcp": "deny"}
    assert config["mcp"] == {
        "context7": {"enabled": False},
        "gh_grep": {"enabled": False},
        "playwright": {"enabled": False},
    }


def test_batch_runner_splits_a_failed_group_and_preserves_order(tmp_path: Path, monkeypatch) -> None:
    candidates = tmp_path / "candidates.jsonl"
    protocol = tmp_path / "protocol.md"
    output = tmp_path / "reviews.jsonl"
    rows = [{"security_code": f"60000{index}", "name": f"公司{index}", "type_key": "type1"} for index in range(3)]
    candidates.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    protocol.write_text("review protocol", encoding="utf-8")

    def fake_run(batch, **_kwargs):
        if len(batch) > 1:
            raise ValueError("group too large")
        packet = batch[0]
        return (
            [
                {
                    "schema_version": 2,
                    "security_code": packet["security_code"],
                    "type_key": "type1",
                    "verdict": "caution",
                    "recommended_action": "manual_review",
                    "buy_attractiveness_score": 55,
                    "ai_action": "watchlist",
                    "confidence": "medium",
                    "summary": "当前仍需观察。",
                    "key_strengths": ["经营稳定"],
                    "risk_flags": ["需求波动"],
                    "claims": [],
                }
            ],
            "ses_test",
        )

    monkeypatch.setattr(runner, "_run_batch", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ai_screening_batch",
            "--candidates",
            str(candidates),
            "--protocol",
            str(protocol),
            "--out",
            str(output),
            "--batch-size",
            "3",
            "--backend",
            "opencode",
            "--model",
            "opencode-go/ox-alpha-free",
            "--allow-unsearched",
            "--root",
            str(tmp_path),
        ],
    )

    assert runner.main() == 0
    merged = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [review["security_code"] for review in merged] == [row["security_code"] for row in rows]
    assert output.with_name("reviews-errors.jsonl").read_text(encoding="utf-8") == ""
