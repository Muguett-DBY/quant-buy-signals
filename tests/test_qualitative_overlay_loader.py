from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.publish_mobile_snapshot import _load_qualitative_overlay


def _write_overlay(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "qualitative_overlay.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


_FLAT_RECORD = {
    "technology_score": 2.0,
    "technology_score_evidence_level": "primary",
    "technology_score_evidence": {
        "source": "公司年报",
        "evidence_id": "primary:technology_score:002496:20251231:sha256:" + "0" * 64,
        "as_of": "2025-12-31",
        "summary": "研发占比与专利证据",
    },
}


def test_flat_adapter_record_is_passed_through_untouched(tmp_path: Path) -> None:
    path = _write_overlay(tmp_path, {"002496": _FLAT_RECORD})

    overlay = _load_qualitative_overlay(path)

    assert overlay["002496"] == _FLAT_RECORD
    assert overlay["002496"]["technology_score"] == 2.0
    assert overlay["002496"]["technology_score_evidence_level"] == "primary"
    assert overlay["002496"]["technology_score_evidence"]["as_of"] == "2025-12-31"


def test_record_without_score_key_is_rejected(tmp_path: Path) -> None:
    evidence_only = {
        "technology_score_evidence": _FLAT_RECORD["technology_score_evidence"],
    }
    path = _write_overlay(tmp_path, {"002496": evidence_only})

    with pytest.raises(RuntimeError, match="no score keys"):
        _load_qualitative_overlay(path)


def test_invalid_code_is_rejected(tmp_path: Path) -> None:
    path = _write_overlay(tmp_path, {"12496": _FLAT_RECORD})

    with pytest.raises(RuntimeError, match="invalid"):
        _load_qualitative_overlay(path)
