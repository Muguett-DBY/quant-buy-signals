from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

from desktop import installer
from desktop.updater import UpdateError
from desktop.version import __version__


pytestmark = pytest.mark.desktop


def _write_bundled_release(root: Path, *, package_url: str | None = None) -> Path:
    package = root / f"DS_DCF-v{__version__}-windows-x64-portable.zip"
    archive_root = f"DS_DCF-v{__version__}"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{archive_root}/DS_DCF.exe", b"MZ-test-executable")
        archive.writestr(
            f"{archive_root}/release-manifest.json",
            json.dumps(
                {"schema_version": 1, "product": "DS_DCF", "version": __version__, "entrypoint": "DS_DCF.exe"},
                separators=(",", ":"),
            ),
        )
        archive.writestr(
            f"{archive_root}/build-provenance.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "product": "DS_DCF",
                    "version": __version__,
                    "git_sha": "b" * 40,
                },
                separators=(",", ":"),
            ),
        )
        archive.writestr(f"{archive_root}/_internal/app.py", b"print('app')\n")
    raw = package.read_bytes()
    (root / f"DS_DCF-v{__version__}-update-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "DS_DCF",
                "version": __version__,
                "published_at": "2026-07-17T00:00:00+00:00",
                "package_url": package_url or f"https://downloads.example.test/{package.name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return package


def test_bootstrap_installer_validates_then_installs_the_same_verified_zip(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    package = _write_bundled_release(bundle)

    manifest, verified = installer.verify_bundled_release(resource_root=bundle)
    installed = installer.install_bundled_release(
        versions_root=tmp_path / "versions",
        create_shortcut=False,
        resource_root=bundle,
    )

    assert manifest.version == __version__
    assert verified == package.resolve()
    assert installed.install_dir == (tmp_path / "versions" / __version__ / "app").resolve()
    assert installed.executable.read_bytes() == b"MZ-test-executable"
    assert installed.package.read_bytes() == package.read_bytes()


def _write_installed_version(root: Path, version: str) -> None:
    app = root / version / "app"
    app.mkdir(parents=True)
    (app / "DS_DCF.exe").write_bytes(b"MZ-installed")
    (app / "release-manifest.json").write_text(
        json.dumps(
            {"schema_version": 1, "product": "DS_DCF", "version": version, "entrypoint": "DS_DCF.exe"},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def test_bootstrap_installer_refuses_to_downgrade_the_stable_shortcut(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundled_release(bundle)
    versions = tmp_path / "versions"
    _write_installed_version(versions, "99.0.0")
    install_called = False
    monkeypatch.setattr(installer, "default_version_library_root", lambda: versions.resolve())

    def unexpected_install(*_args, **_kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("a downgrade must stop before installation or shortcut creation")

    monkeypatch.setattr(installer, "install_update_package", unexpected_install)

    with pytest.raises(UpdateError, match=r"已安装较新版本 99\.0\.0"):
        installer.install_bundled_release(
            versions_root=versions,
            create_shortcut=True,
            resource_root=bundle,
        )

    assert install_called is False


def test_custom_install_root_cannot_bypass_the_stable_shortcut_floor(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundled_release(bundle)
    custom_versions = tmp_path / "custom-versions"
    stable_versions = tmp_path / "stable-versions"
    _write_installed_version(stable_versions, "99.0.0")
    monkeypatch.setattr(installer, "default_version_library_root", lambda: stable_versions.resolve())

    with pytest.raises(UpdateError, match=r"已安装较新版本 99\.0\.0"):
        installer.install_bundled_release(
            versions_root=custom_versions,
            create_shortcut=True,
            resource_root=bundle,
        )

    assert not custom_versions.exists()


def test_bootstrap_installer_uses_the_highest_complete_install_as_its_floor(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    package = _write_bundled_release(bundle)
    versions = tmp_path / "versions"
    _write_installed_version(versions, "11.1.0")
    (versions / "999.0.0").mkdir()
    captured = {}

    def capture_install(package_path, manifest, **kwargs):
        captured.update(package=package_path, manifest=manifest, **kwargs)
        return object()

    monkeypatch.setattr(installer, "install_update_package", capture_install)

    installer.install_bundled_release(
        versions_root=versions,
        create_shortcut=False,
        resource_root=bundle,
    )

    assert captured["package"] == package.resolve()
    assert captured["current_version"] == "11.1.0"
    assert captured["versions_root"] == versions.resolve()


def test_bootstrap_installer_keeps_exact_version_reinstall_idempotent(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundled_release(bundle)
    versions = tmp_path / "versions"
    _write_installed_version(versions, __version__)
    captured = {}

    def capture_install(_package, _manifest, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(installer, "install_update_package", capture_install)

    installer.install_bundled_release(
        versions_root=versions,
        create_shortcut=False,
        resource_root=bundle,
    )

    assert captured["current_version"] == "0.0.0"


def test_bootstrap_installer_rejects_a_manifest_for_a_different_payload_name(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundled_release(bundle, package_url="https://downloads.example.test/wrong.zip")

    with pytest.raises(UpdateError, match="未指向对应便携包"):
        installer.load_bundled_manifest(resource_root=bundle)


@pytest.mark.parametrize("stdout_encoding", ["cp1252", "gbk"])
def test_bootstrap_installer_verify_mode_only_checks_the_bundle(tmp_path, monkeypatch, stdout_encoding):
    bundle = tmp_path / "安装😀" / "bundle"
    bundle.mkdir(parents=True)
    _write_bundled_release(bundle)
    monkeypatch.setattr(installer, "_resource_root", lambda: bundle)
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding=stdout_encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    assert installer.main(["--verify-bundle"]) == 0
    stdout.flush()
    payload = json.loads(stdout_bytes.getvalue().decode(stdout_encoding))
    assert payload["ok"] is True
    assert payload["version"] == __version__
    assert payload["package"].startswith(str(bundle))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["安装😀"]


@pytest.mark.parametrize("stderr_encoding", ["cp1252", "gbk"])
def test_bootstrap_installer_human_error_is_safe_on_legacy_stderr(monkeypatch, stderr_encoding):
    stderr_bytes = io.BytesIO()
    stderr = io.TextIOWrapper(stderr_bytes, encoding=stderr_encoding, errors="strict")
    monkeypatch.setattr(sys, "stderr", stderr)

    installer._write_cli_message("安装失败😀", error=True)

    stderr.flush()
    output = stderr_bytes.getvalue().decode(stderr_encoding)
    assert output.endswith("\n")
    assert "\\U0001f600" in output
