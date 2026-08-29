from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.atomic_io import atomic_write_bytes, atomic_write_text


def test_atomic_write_replaces_complete_content_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_bytes(b"old")

    atomic_write_bytes(target, b'{"complete":true}\n')

    assert target.read_bytes() == b'{"complete":true}\n'
    assert list(tmp_path.glob(".artifact.json.*.tmp")) == []


def test_atomic_write_preserves_previous_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.json"
    target.write_text("old", encoding="utf-8")

    def fail_replace(_source: os.PathLike[str] | str, _target: os.PathLike[str] | str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".artifact.json.*.tmp")) == []
