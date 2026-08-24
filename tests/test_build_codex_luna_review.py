from __future__ import annotations

import hashlib
import json

from tools.build_codex_luna_review import _knowledge_metadata, build


def test_knowledge_metadata_uses_the_selected_file_and_is_honest(tmp_path) -> None:
    knowledge = tmp_path / "knowledge.md"
    knowledge.write_text("selected contract", encoding="utf-8")

    metadata = _knowledge_metadata(knowledge)

    assert metadata["knowledge_sha256"] == hashlib.sha256(knowledge.read_bytes()).hexdigest()
    assert metadata["knowledge_loaded_for_contract"] is True
    assert metadata["knowledge_used_for_model_reasoning"] is False
    assert metadata["knowledge_contract"] == (
        "facts-only local review; knowledge loaded for contract only, not injected into model reasoning"
    )


def test_build_records_the_runtime_knowledge_contract(tmp_path) -> None:
    knowledge = tmp_path / "knowledge.md"
    knowledge.write_text("runtime contract", encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps(
            {
                "packets": [
                    {
                        "security_code": "600000",
                        "name": "测试公司",
                        "type_key": "type7",
                        "candidate_types": [{"type_key": "type7"}],
                        "deterministic": {"status": "insufficient_evidence"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    research = tmp_path / "research.json"
    research.write_text(
        json.dumps(
            {
                "companies": {
                    "600000": {
                        "code": "600000",
                        "name": "测试公司",
                        "industry": "未知",
                        "facts": [
                            {
                                "id": "valuation",
                                "statement": "600000 2026-08-24价格 10.00元 PE 20.00倍",
                                "source_refs": ["https://example.com/valuation"],
                            }
                        ],
                        "annual_history": [],
                        "shareholder_returns": {},
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    metadata = build(candidates, research, tmp_path / "reviews.jsonl", knowledge)

    assert metadata["knowledge_sha256"] == hashlib.sha256(knowledge.read_bytes()).hexdigest()
    assert metadata["knowledge_loaded_for_contract"] is True
    assert metadata["knowledge_used_for_model_reasoning"] is False
