from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

import pytest

from desktop.version import __version__
from desktop import installer, launcher
from tools import build_desktop


def test_desktop_version_matches_project_and_release_manifest():
    assert __version__ == "11.2.0"
    assert build_desktop._project_version() == __version__
    assert build_desktop._release_manifest() == {
        "schema_version": 1,
        "product": "DS_DCF",
        "version": __version__,
        "entrypoint": "DS_DCF.exe",
    }
    assert build_desktop._build_provenance("a" * 40) == {
        "schema_version": 1,
        "product": "DS_DCF",
        "version": __version__,
        "git_sha": "a" * 40,
    }


def test_windows_numeric_file_version_matches_the_release_version():
    version_info = (Path(__file__).resolve().parents[1] / "desktop" / "version_info.txt").read_text(encoding="utf-8")
    expected = tuple(int(part) for part in __version__.split(".")) + (0,)

    assert f"filevers={expected}" in version_info
    assert f"prodvers={expected}" in version_info


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


def test_bootstrap_installer_spec_and_default_release_url_are_version_bound():
    spec = (Path(__file__).resolve().parents[1] / "desktop" / "DS_DCF_Installer.spec").read_text(encoding="utf-8")

    assert "DS_DCF_INSTALLER_PACKAGE" in spec
    assert "DS_DCF_INSTALLER_MANIFEST" in spec
    assert build_desktop._default_package_url().endswith(
        f"/v{__version__}/DS_DCF-v{__version__}-windows-x64-portable.zip"
    )
    assert installer._PORTABLE_PACKAGE_NAME == f"DS_DCF-v{__version__}-windows-x64-portable.zip"
    assert installer._BUNDLED_MANIFEST_NAME == f"DS_DCF-v{__version__}-update-manifest.json"


def test_desktop_update_channel_is_dedicated_and_cannot_be_stolen_by_other_releases():
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "desktop" / "update_config.json").read_text(encoding="utf-8")
    )

    assert config == {
        "manifest_url": (
            "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/windows-app/update-manifest.json"
        )
    }
    assert "/releases/latest/" not in config["manifest_url"]


def test_desktop_update_key_is_internally_consistent_and_separate_from_mobile_data():
    from desktop import updater

    raw_spki = base64.b64decode(updater.UPDATE_SIGNING_PUBLIC_KEY_SPKI_BASE64, validate=True)
    uncompressed_point = raw_spki[-65:]
    assert len(raw_spki) == 91
    assert uncompressed_point[0] == 4
    assert updater.UPDATE_SIGNING_PUBLIC_KEY == (
        int.from_bytes(uncompressed_point[1:33], "big"),
        int.from_bytes(uncompressed_point[33:65], "big"),
    )

    root = Path(__file__).resolve().parents[1]
    signer = (root / "tools" / "sign_desktop_update_manifest.ps1").read_text(encoding="utf-8")
    expected_key = re.search(r"\$expectedPublicKey = '([^']+)'", signer)
    assert expected_key is not None
    assert expected_key.group(1) == updater.UPDATE_SIGNING_PUBLIC_KEY_SPKI_BASE64
    assert "MOBILE_DATA_SIGNING_PRIVATE_KEY_BASE64" not in signer

    android_source = (root / "android/app/src/main/java/com/muguett/dsdcf/MarketRepository.java").read_text(
        encoding="utf-8"
    )
    mobile_key = re.search(r'MOBILE_SIGNING_PUBLIC_KEY_BASE64\s*=\s*"([A-Za-z0-9+/=]+)"', android_source)
    assert mobile_key is not None
    assert mobile_key.group(1) != updater.UPDATE_SIGNING_PUBLIC_KEY_SPKI_BASE64


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
    library, version_root, app_root, archive, installer_path, manifest_path, signature_path = (
        build_desktop._desktop_delivery_paths(
            tmp_path,
            f"DS_DCF-v{__version__}-windows-x64-portable.zip",
            f"DS_DCF-v{__version__}-windows-x64-installer.exe",
            f"DS_DCF-v{__version__}-update-manifest.json",
            f"DS_DCF-v{__version__}-update-manifest.json.sig",
        )
    )

    assert library == tmp_path / "6BUYING_POINT"
    assert version_root == library / __version__
    assert app_root == version_root / "app"
    assert archive == version_root / f"DS_DCF-v{__version__}-windows-x64-portable.zip"
    assert installer_path == version_root / f"DS_DCF-v{__version__}-windows-x64-installer.exe"
    assert manifest_path == version_root / f"DS_DCF-v{__version__}-update-manifest.json"
    assert signature_path == version_root / f"DS_DCF-v{__version__}-update-manifest.json.sig"


def test_desktop_delivery_is_atomic_and_immutable(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    (release / "DS_DCF.exe").write_bytes(b"MZ")
    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"zip")
    bootstrapper = tmp_path / "installer.exe"
    bootstrapper.write_bytes(b"MZ-installer")
    manifest = tmp_path / "update-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    signature = tmp_path / "update-manifest.json.sig"
    signature.write_bytes(b"signed")

    library, version_root, app_root, copied_archive, copied_installer, copied_manifest, copied_signature = (
        build_desktop._deliver_to_desktop(
            release,
            archive,
            bootstrapper,
            manifest,
            signature,
            desktop,
        )
    )

    assert library == desktop / "6BUYING_POINT"
    assert version_root == library / __version__
    assert (app_root / "DS_DCF.exe").read_bytes() == b"MZ"
    assert copied_archive.read_bytes() == b"zip"
    assert copied_installer.read_bytes() == b"MZ-installer"
    assert copied_manifest.read_text(encoding="utf-8") == "{}\n"
    assert copied_signature.read_bytes() == b"signed"
    assert not list(library.glob(".*-staging-*"))
    with pytest.raises(RuntimeError, match="already exists"):
        build_desktop._deliver_to_desktop(release, archive, bootstrapper, manifest, signature, desktop)


def test_desktop_delivery_cleans_failed_staging(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"zip")
    bootstrapper = tmp_path / "installer.exe"
    bootstrapper.write_bytes(b"MZ-installer")
    manifest = tmp_path / "update-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    signature = tmp_path / "update-manifest.json.sig"
    signature.write_bytes(b"signed")

    def fail_copytree(*_args, **_kwargs):
        raise OSError("injected copy failure")

    monkeypatch.setattr(build_desktop.shutil, "copytree", fail_copytree)
    with pytest.raises(OSError, match="injected"):
        build_desktop._deliver_to_desktop(release, archive, bootstrapper, manifest, signature, desktop)

    library = desktop / "6BUYING_POINT"
    assert not (library / __version__).exists()
    assert not list(library.glob(".*-staging-*"))


def test_signed_update_manifest_outputs_versioned_and_stable_identical_aliases(tmp_path, monkeypatch):
    payload = {
        "schema_version": 1,
        "product": "DS_DCF",
        "version": __version__,
        "published_at": "2026-07-20T08:00:00+00:00",
        "package_url": f"https://example.test/DS_DCF-v{__version__}.zip",
        "sha256": "a" * 64,
        "size": 123,
    }
    monkeypatch.setattr(
        build_desktop,
        "_sign_canonical_manifest",
        lambda canonical, **_kwargs: hashlib.sha256(canonical).digest(),
    )

    versioned, versioned_signature, stable, stable_signature = build_desktop._write_signed_update_manifests(
        payload,
        output_root=tmp_path,
    )

    assert versioned.name == f"DS_DCF-v{__version__}-update-manifest.json"
    assert stable.name == "update-manifest.json"
    assert versioned.read_bytes() == stable.read_bytes()
    assert versioned_signature.read_bytes() == stable_signature.read_bytes()
    assert json.loads(stable.read_text(encoding="utf-8")) == payload
    assert set(json.loads(stable.read_text(encoding="utf-8"))) == {
        "schema_version",
        "product",
        "version",
        "published_at",
        "package_url",
        "sha256",
        "size",
    }


def test_release_git_sha_requires_clean_exact_committed_head(monkeypatch, tmp_path):
    responses = {
        ("rev-parse", "--show-toplevel"): str(tmp_path),
        ("rev-parse", "--verify", "HEAD"): "A" * 40,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("cat-file", "-t", "a" * 40): "commit",
    }
    monkeypatch.setattr(build_desktop, "_git_output", lambda arguments, **_kwargs: responses[tuple(arguments)])

    assert build_desktop._require_clean_committed_git_sha(root=tmp_path) == "a" * 40
    assert build_desktop._require_clean_committed_git_sha(test_sha="B" * 40) == "b" * 40
    with pytest.raises(RuntimeError, match="invalid"):
        build_desktop._require_clean_committed_git_sha(test_sha="not-a-sha")


def test_release_git_sha_rejects_dirty_or_untracked_files(monkeypatch, tmp_path):
    responses = {
        ("rev-parse", "--show-toplevel"): str(tmp_path),
        ("rev-parse", "--verify", "HEAD"): "a" * 40,
        ("status", "--porcelain=v1", "--untracked-files=all"): "?? private-release-key.pem",
    }
    monkeypatch.setattr(build_desktop, "_git_output", lambda arguments, **_kwargs: responses[tuple(arguments)])

    with pytest.raises(RuntimeError, match="clean committed"):
        build_desktop._require_clean_committed_git_sha(root=tmp_path)


def test_desktop_signing_key_must_be_external_and_is_never_committed(monkeypatch, tmp_path):
    key = tmp_path / "private-key.der"
    key.write_bytes(b"private")
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH", raising=False)

    with pytest.raises(RuntimeError, match="outside"):
        build_desktop._resolve_signing_private_key_path(key)

    signer = build_desktop.DESKTOP_SIGNER.read_text(encoding="utf-8")
    assert "BEGIN PRIVATE KEY-----\nM" not in signer
    assert "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64" in signer
    assert "MOBILE_DATA_SIGNING_PRIVATE_KEY_BASE64" not in signer


def test_desktop_release_mode_still_requires_a_signing_key(monkeypatch):
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64", raising=False)

    with pytest.raises(RuntimeError, match="desktop update signing key is required"):
        build_desktop._validate_signing_configuration(None)


def test_ci_smoke_configuration_is_github_only_isolated_and_secret_free(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    output_root = root / "build" / "ci-smoke-output"
    work_root = root / "build" / "ci-smoke-work"
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64", raising=False)

    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    with pytest.raises(RuntimeError, match="reserved for GitHub Actions"):
        build_desktop._validate_ci_smoke_configuration(
            output_root=output_root,
            work_root=work_root,
            desktop=False,
            package_url=None,
            signing_private_key=None,
        )

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    build_desktop._validate_ci_smoke_configuration(
        output_root=output_root,
        work_root=work_root,
        desktop=False,
        package_url=None,
        signing_private_key=None,
    )
    for overrides in (
        {"desktop": True},
        {"package_url": "https://example.test/package.zip"},
        {"signing_private_key": tmp_path / "private-key.der"},
    ):
        arguments = {
            "output_root": output_root,
            "work_root": work_root,
            "desktop": False,
            "package_url": None,
            "signing_private_key": None,
            **overrides,
        }
        with pytest.raises(RuntimeError, match="cannot be combined"):
            build_desktop._validate_ci_smoke_configuration(**arguments)

    monkeypatch.setenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64", "not-a-real-key")
    with pytest.raises(RuntimeError, match="refuses desktop signing key"):
        build_desktop._validate_ci_smoke_configuration(
            output_root=output_root,
            work_root=work_root,
            desktop=False,
            package_url=None,
            signing_private_key=None,
        )
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64")

    with pytest.raises(RuntimeError, match="below build"):
        build_desktop._validate_ci_smoke_configuration(
            output_root=root / "dist" / "desktop",
            work_root=work_root,
            desktop=False,
            package_url=None,
            signing_private_key=None,
        )


@pytest.mark.skipif(os.name != "nt", reason="desktop CI delivery is Windows-only")
def test_ci_smoke_build_never_signs_or_emits_release_manifests(monkeypatch, tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# smoke\n", encoding="utf-8")
    (root / "LICENSE").write_text("test license\n", encoding="utf-8")
    output_root = root / "build" / "ci-smoke-output"
    work_root = root / "build" / "ci-smoke-work"
    commands: list[list[str]] = []
    installer_manifest: dict[str, object] = {}

    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(build_desktop, "_project_version", lambda: __version__)
    monkeypatch.setattr(build_desktop, "_require_clean_committed_git_sha", lambda: "a" * 40)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64", raising=False)
    monkeypatch.setattr(
        build_desktop,
        "_validate_signing_configuration",
        lambda *_args, **_kwargs: pytest.fail("CI smoke called release signing validation"),
    )
    monkeypatch.setattr(
        build_desktop,
        "_write_signed_update_manifests",
        lambda *_args, **_kwargs: pytest.fail("CI smoke emitted signed release manifests"),
    )

    def fake_run(command, **_kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        if "PyInstaller" in command:
            dist = Path(command[command.index("--distpath") + 1]) / "DS_DCF"
            dist.mkdir(parents=True)
            (dist / "DS_DCF.exe").write_bytes(b"MZ-ci-smoke")
        return subprocess.CompletedProcess(command, 0)

    def fake_build_installer(*, package, manifest, output_root, work_root):
        assert package.is_file()
        assert manifest.parent == work_root
        installer_manifest.update(json.loads(manifest.read_text(encoding="utf-8")))
        installer = output_root / f"DS_DCF-v{__version__}-windows-x64-installer.exe"
        installer.write_bytes(b"MZ-ci-smoke-installer")
        return installer

    monkeypatch.setattr(build_desktop.subprocess, "run", fake_run)
    monkeypatch.setattr(build_desktop, "_build_installer", fake_build_installer)

    result = build_desktop.main(
        [
            "--ci-smoke",
            "--output-root",
            str(output_root),
            "--work-root",
            str(work_root),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert result == 0
    assert summary["ci_smoke"] is True
    assert summary["release_ready"] is False
    assert "update_manifest" not in summary
    assert "update_manifest_signature" not in summary
    assert installer_manifest["package_url"] == "https://ci-smoke.invalid/unpublishable.zip"
    assert not (work_root / "ci-smoke-installer-manifest.json").exists()
    assert not list(output_root.glob("*update-manifest*"))
    assert not list(output_root.glob("*.sig"))
    executable_commands = [command[-1] for command in commands if command and command[0].endswith("DS_DCF.exe")]
    assert executable_commands == ["--version", "--health-check", "--server-smoke-test"]


def test_ci_workflow_uses_the_unpublishable_smoke_mode_without_a_desktop_secret():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "python -m tools.build_desktop --ci-smoke" in workflow
    assert "--output-root build/ci-smoke-output" in workflow
    assert "--work-root build/ci-smoke-work" in workflow
    assert "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY" not in workflow


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is required")
def test_p256_key_generator_never_overwrites_and_only_prints_the_public_key(tmp_path):
    generator = Path(__file__).resolve().parents[1] / "tools/generate_p256_signing_key.ps1"
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir()
    current_identity = subprocess.run(
        ["whoami.exe"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    subprocess.run(
        [
            "icacls.exe",
            str(protected_parent),
            "/inheritance:r",
            "/grant:r",
            f"{current_identity}:(OI)(CI)(F)",
            "SYSTEM:(OI)(CI)(F)",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = protected_parent / "desktop-signing-key.properties"
    command = [
        shutil.which("pwsh"),
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(generator),
        "-Output",
        str(output),
        "-EnvironmentVariableName",
        "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64",
    ]

    generated = subprocess.run(
        command,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    lines = generated.stdout.strip().splitlines()
    assert len(lines) == 1
    public_key = base64.b64decode(lines[0], validate=True)
    assert len(public_key) == 91
    private_property = output.read_text(encoding="utf-8").strip()
    prefix = "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64="
    assert private_property.startswith(prefix)
    private_key = private_property.removeprefix(prefix)
    assert base64.b64decode(private_key, validate=True)
    assert private_key not in generated.stdout
    assert private_key not in generated.stderr
    acl = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$acl=Get-Acl -LiteralPath $env:DS_DCF_ACL_TEST_PATH; "
                "[pscustomobject]@{Protected=$acl.AreAccessRulesProtected;RuleCount=@($acl.Access).Count} "
                "| ConvertTo-Json -Compress"
            ),
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "DS_DCF_ACL_TEST_PATH": str(output)},
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert json.loads(acl.stdout) == {"Protected": True, "RuleCount": 2}

    original = output.read_bytes()
    overwrite = subprocess.run(
        command,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert overwrite.returncode != 0
    assert output.read_bytes() == original
    assert private_key not in overwrite.stdout
    assert private_key not in overwrite.stderr

    invalid_output = protected_parent / "invalid-variable.properties"
    invalid = subprocess.run(
        [*command[:-3], str(invalid_output), "-EnvironmentVariableName", "lower-case-name"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert invalid.returncode != 0
    assert not invalid_output.exists()

    missing_parent_output = tmp_path / "missing" / "key.properties"
    missing_parent = subprocess.run(
        [*command[:6], str(missing_parent_output), *command[7:]],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert missing_parent.returncode != 0
    assert not missing_parent_output.exists()

    broad_parent = tmp_path / "broad"
    broad_parent.mkdir()
    broad_output = broad_parent / "key.properties"
    broad_acl = subprocess.run(
        [*command[:6], str(broad_output), *command[7:]],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert broad_acl.returncode != 0
    assert not broad_output.exists()

    repository_output = Path(__file__).resolve().parents[1] / ".never-create-desktop-signing-key.properties"
    repository_attempt = subprocess.run(
        [*command[:6], str(repository_output), *command[7:]],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert repository_attempt.returncode != 0
    assert not repository_output.exists()
