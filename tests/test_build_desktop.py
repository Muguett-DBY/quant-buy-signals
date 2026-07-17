from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from desktop.version import __version__
from desktop import launcher
from tools import build_desktop


def test_desktop_version_matches_project_and_release_manifest():
    assert __version__ == "11.0.0"
    assert build_desktop._project_version() == __version__
    assert build_desktop._release_manifest() == {
        "schema_version": 1,
        "product": "DS_DCF",
        "version": __version__,
        "entrypoint": "DS_DCF.exe",
    }


def test_desktop_bundle_and_health_gate_include_every_financial_evidence_file():
    evidence_files = {
        "data/financial_balance_sheet_evidence.json",
        "data/financial_zero_capex_evidence.json",
        "data/financial_zero_revenue_evidence.json",
    }
    spec = (Path(__file__).resolve().parents[1] / "desktop" / "DS_DCF.spec").read_text(encoding="utf-8")

    assert evidence_files <= set(launcher._HEALTH_REQUIRED_RESOURCE_FILES)
    for relative in evidence_files:
        assert f'ROOT / "data" / "{Path(relative).name}"' in spec


def test_desktop_portable_zip_is_deterministic_and_version_rooted(tmp_path):
    source = tmp_path / f"DS_DCF-v{__version__}"
    source.mkdir()
    (source / "DS_DCF.exe").write_bytes(b"MZ")
    (source / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    build_desktop._write_deterministic_zip(source, first)
    build_desktop._write_deterministic_zip(source, second)

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            f"DS_DCF-v{__version__}/DS_DCF.exe",
            f"DS_DCF-v{__version__}/release-manifest.json",
        ]
        assert all(info.date_time == (2026, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_desktop_release_carries_the_project_license():
    source = Path(build_desktop.main.__code__.co_filename).read_text(encoding="utf-8")
    assert 'shutil.copy2(ROOT / "LICENSE", release_dir / "LICENSE")' in source


def test_desktop_build_cleanup_never_removes_its_allowed_root(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    build_desktop._safe_remove_tree(child, allowed_root=tmp_path)
    assert not child.exists()

    with pytest.raises(RuntimeError, match="refusing"):
        build_desktop._safe_remove_tree(tmp_path, allowed_root=tmp_path)


def test_desktop_delivery_groups_every_release_under_one_version_folder(tmp_path):
    library, version_root, app_root, archive = build_desktop._desktop_delivery_paths(
        tmp_path,
        f"DS_DCF-v{__version__}-windows-x64-portable.zip",
    )

    assert library == tmp_path / "6BUYING_POINT"
    assert version_root == library / __version__
    assert app_root == version_root / "app"
    assert archive == version_root / f"DS_DCF-v{__version__}-windows-x64-portable.zip"


def test_desktop_delivery_is_atomic_and_immutable(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    (release / "DS_DCF.exe").write_bytes(b"MZ")
    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"zip")

    library, version_root, app_root, copied_archive = build_desktop._deliver_to_desktop(
        release,
        archive,
        desktop,
    )

    assert library == desktop / "6BUYING_POINT"
    assert version_root == library / __version__
    assert (app_root / "DS_DCF.exe").read_bytes() == b"MZ"
    assert copied_archive.read_bytes() == b"zip"
    assert not list(library.glob(".*-staging-*"))
    with pytest.raises(RuntimeError, match="already exists"):
        build_desktop._deliver_to_desktop(release, archive, desktop)


def test_desktop_delivery_cleans_failed_staging(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"zip")

    def fail_copytree(*_args, **_kwargs):
        raise OSError("injected copy failure")

    monkeypatch.setattr(build_desktop.shutil, "copytree", fail_copytree)
    with pytest.raises(OSError, match="injected"):
        build_desktop._deliver_to_desktop(release, archive, desktop)

    library = desktop / "6BUYING_POINT"
    assert not (library / __version__).exists()
    assert not list(library.glob(".*-staging-*"))
