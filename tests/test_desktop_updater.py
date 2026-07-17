from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from desktop import updater


def _manifest_payload(**updates):
    payload = {
        "schema_version": 1,
        "product": "DS_DCF",
        "version": "11.1.1",
        "published_at": "2026-07-17T00:00:00+00:00",
        "package_url": "https://downloads.example.test/DS_DCF-v11.1.1.zip",
        "sha256": "a" * 64,
        "size": 123,
    }
    payload.update(updates)
    return payload


def _parsed_manifest(**updates):
    return updater.parse_update_manifest(
        _manifest_payload(**updates),
        now=datetime(2026, 7, 17, 1, tzinfo=timezone.utc),
    )


class _Response:
    def __init__(self, body: bytes, *, url: str, content_length: int | None = None):
        self.body = body
        self.url = url
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self.status_code = 200
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[start : start + chunk_size]

    def raise_for_status(self):
        return None

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _write_release_zip(path, *, version="11.1.1", mutations=None):
    root = f"DS_DCF-v{version}"
    entries = {
        f"{root}/DS_DCF.exe": b"MZ-test-executable",
        f"{root}/release-manifest.json": json.dumps(
            {
                "schema_version": 1,
                "product": "DS_DCF",
                "version": version,
                "entrypoint": "DS_DCF.exe",
            },
            separators=(",", ":"),
        ).encode(),
        f"{root}/_internal/app.py": b"print('app')\n",
    }
    if mutations:
        entries.update(mutations)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            if isinstance(content, zipfile.ZipInfo):
                archive.writestr(content, b"target")
            else:
                archive.writestr(name, content)
    raw = path.read_bytes()
    return updater.UpdateManifest(
        version=version,
        published_at="2026-07-17T00:00:00+00:00",
        package_url=f"https://downloads.example.test/DS_DCF-v{version}.zip",
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )


def test_update_manifest_is_strict_https_dated_and_canonical():
    manifest = _parsed_manifest()
    assert manifest.version == "11.1.1"
    assert manifest.package_url.startswith("https://")

    with pytest.raises(updater.UpdateError, match="HTTPS"):
        _parsed_manifest(package_url="http://example.test/package.zip")
    with pytest.raises(updater.UpdateError, match="semantic"):
        _parsed_manifest(version="10.0")
    with pytest.raises(updater.UpdateError, match="future"):
        updater.parse_update_manifest(
            _manifest_payload(published_at="2027-01-01T00:00:00+00:00"),
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
    with pytest.raises(updater.UpdateError, match="shape"):
        updater.parse_update_manifest(
            {**_manifest_payload(), "unexpected": True},
            now=datetime(2026, 7, 17, 1, tzinfo=timezone.utc),
        )


def test_local_package_verification_is_side_effect_free_and_reuses_update_gates(tmp_path):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)

    verified = updater.verify_update_package(package, manifest)

    assert verified == package.resolve()
    assert list(tmp_path.iterdir()) == [package]


def test_manifest_fetch_rejects_insecure_redirect_and_duplicate_json_key():
    duplicate = (
        b'{"schema_version":1,"schema_version":1,"product":"DS_DCF","version":"11.1.1",'
        b'"published_at":"2026-07-17T00:00:00+00:00","package_url":"https://example.test/p.zip",'
        b'"sha256":"' + b"a" * 64 + b'","size":123}'
    )
    insecure = _Session(_Response(duplicate, url="http://redirect.example.test/manifest.json"))
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater.fetch_update_manifest("https://example.test/manifest.json", session=insecure)

    duplicate_session = _Session(_Response(duplicate, url="https://example.test/manifest.json"))
    with pytest.raises(updater.UpdateError, match="duplicate key"):
        updater.fetch_update_manifest("https://example.test/manifest.json", session=duplicate_session)


def test_download_requires_exact_manifest_size_and_sha256(tmp_path):
    body = b"verified update bytes"
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/package.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    response = _Response(body, url=manifest.package_url, content_length=len(body))
    path = updater.download_update_package(manifest, tmp_path / "package.zip", session=_Session(response))
    assert path.read_bytes() == body
    assert response.closed

    bad = updater.UpdateManifest(**{**manifest.__dict__, "sha256": "0" * 64})
    with pytest.raises(updater.UpdateError, match="SHA-256 does not match the update manifest"):
        updater.download_update_package(
            bad,
            tmp_path / "bad.zip",
            session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
        )
    assert list(tmp_path.glob(".bad.zip.*.part")) == []


def test_download_never_overwrites_or_deletes_a_preexisting_partial_file(tmp_path):
    body = b"verified update bytes"
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/package.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    partial = tmp_path / "package.zip.part"
    partial.write_bytes(b"belongs to another download")

    downloaded = updater.download_update_package(
        manifest,
        tmp_path / "package.zip",
        session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
    )

    assert partial.read_bytes() == b"belongs to another download"
    assert downloaded.read_bytes() == body


def test_temporary_cleanup_failure_does_not_mask_the_validation_error(tmp_path, monkeypatch):
    body = b"corrupt update bytes"
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/package.zip",
        sha256="0" * 64,
        size=len(body),
    )

    def fail_cleanup(self, *, missing_ok=False):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(updater.UpdateError, match="SHA-256 does not match"):
        updater.download_update_package(
            manifest,
            tmp_path / "package.zip",
            session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
        )


def test_valid_update_installs_side_by_side_without_overwriting_current_version(tmp_path):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)

    installed = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
    )

    assert installed.version == "11.1.1"
    assert installed.executable.read_bytes() == b"MZ-test-executable"
    assert installed.version_dir == (tmp_path / "versions" / "11.1.1").resolve()
    assert installed.install_dir == (tmp_path / "versions" / "11.1.1" / "app").resolve()
    assert installed.package.name == "DS_DCF-v11.1.1-windows-x64-portable.zip"
    assert installed.package.read_bytes() == package.read_bytes()
    assert package.is_file()

    repeated = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
    )
    assert repeated == installed


@pytest.mark.parametrize("tamper", ["executable", "extra_file", "portable_package"])
def test_existing_version_is_replayed_byte_for_byte_before_idempotent_success(tmp_path, tamper):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)
    installed = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
    )
    if tamper == "executable":
        installed.executable.write_bytes(b"MZ-tampered")
    elif tamper == "extra_file":
        (installed.install_dir / "unexpected.dll").write_bytes(b"extra")
    else:
        installed.package.write_bytes(b"tampered package")

    with pytest.raises(updater.UpdateError, match="differs|file set"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
        )


def test_existing_version_rejects_an_application_root_symlink(tmp_path):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)
    installed = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
    )
    outside = tmp_path / "outside-app"
    installed.install_dir.rename(outside)
    try:
        installed.install_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        outside.rename(installed.install_dir)
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(updater.UpdateError, match="symbolic link or junction"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
        )


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"../escape.exe": b"bad"}, "one versioned product root"),
        ({"DS_DCF-v11.1.1/ds_dcf.EXE": b"collision"}, "case-colliding"),
        ({"DS_DCF-v11.1.1/CON.txt": b"reserved"}, "reserved Windows"),
        ({"DS_DCF-v11.1.1/invalid?.dll": b"bad"}, "unsafe path component"),
    ],
)
def test_update_zip_rejects_traversal_case_collisions_and_reserved_names(tmp_path, mutation, message):
    package = tmp_path / "malicious.zip"
    manifest = _write_release_zip(package, mutations=mutation)

    with pytest.raises(updater.UpdateError, match=message):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
        )
    assert not (tmp_path / "escape.exe").exists()


def test_update_zip_rejects_symbolic_links(tmp_path):
    package = tmp_path / "symlink.zip"
    root = "DS_DCF-v11.1.1"
    link = zipfile.ZipInfo(f"{root}/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    manifest = _write_release_zip(package, mutations={link.filename: link})

    with pytest.raises(updater.UpdateError, match="symbolic link"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
        )


@pytest.mark.parametrize(
    "name, mode, message",
    [
        ("DS_DCF-v11.1.1/mode-says-directory", stat.S_IFDIR | 0o755, "type disagrees"),
        ("DS_DCF-v11.1.1/mode-says-file/", stat.S_IFREG | 0o644, "type disagrees"),
        ("DS_DCF-v11.1.1", stat.S_IFREG | 0o644, "root must be a directory"),
    ],
)
def test_update_zip_rejects_external_mode_and_path_type_mismatches(tmp_path, name, mode, message):
    package = tmp_path / "mode-mismatch.zip"
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = mode << 16
    manifest = _write_release_zip(package, mutations={entry.filename: entry})

    with pytest.raises(updater.UpdateError, match=message):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
        )


def test_updater_refuses_same_version_and_downgrade_before_install(tmp_path):
    package = tmp_path / "old.zip"
    manifest = SimpleNamespace(version="10.0.0")
    with pytest.raises(updater.UpdateError, match="downgrade"):
        updater.install_update_package(package, manifest, create_shortcut=False)


def test_corrupt_zip_and_target_path_conflict_are_normalized_to_update_error(tmp_path):
    package = tmp_path / "corrupt.zip"
    package.write_bytes(b"not-a-zip")
    raw = package.read_bytes()
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/corrupt.zip",
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )
    with pytest.raises(updater.UpdateError, match="valid ZIP"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
        )

    valid = tmp_path / "valid.zip"
    valid_manifest = _write_release_zip(valid)
    root = tmp_path / "blocked"
    root.mkdir()
    (root / "11.1.1").write_bytes(b"path conflict")
    with pytest.raises(updater.UpdateError, match="directory is missing"):
        updater.install_update_package(
            valid,
            valid_manifest,
            versions_root=root,
            create_shortcut=False,
        )


def test_update_config_is_optional_but_never_accepts_plain_http(tmp_path, monkeypatch):
    monkeypatch.delenv("DS_DCF_UPDATE_MANIFEST_URL", raising=False)
    library = tmp_path / "library"
    assert updater.load_update_manifest_url(tmp_path, library_root=library) is None
    (tmp_path / "update_config.json").write_text('{"manifest_url":null}', encoding="utf-8")
    assert updater.load_update_manifest_url(tmp_path, library_root=library) is None
    (tmp_path / "update_config.json").write_text(
        '{"manifest_url":"http://example.test/manifest.json"}',
        encoding="utf-8",
    )
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater.load_update_manifest_url(tmp_path, library_root=library)


def test_stable_library_update_config_overrides_bundled_config_and_environment_wins(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    library = tmp_path / "library"
    app_root.mkdir()
    library.mkdir()
    (app_root / "update_config.json").write_text(
        '{"manifest_url":"https://example.test/bundled.json"}', encoding="utf-8"
    )
    (library / "update_config.json").write_text('{"manifest_url":"https://example.test/stable.json"}', encoding="utf-8")
    assert updater.load_update_manifest_url(app_root, library_root=library).endswith("stable.json")

    monkeypatch.setenv("DS_DCF_UPDATE_MANIFEST_URL", "https://example.test/environment.json")
    assert updater.load_update_manifest_url(app_root, library_root=library).endswith("environment.json")


def test_shortcut_creation_uses_an_absolute_system_powershell_path(tmp_path, monkeypatch):
    target = tmp_path / "app" / "DS_DCF.exe"
    target.parent.mkdir()
    target.write_bytes(b"MZ")
    powershell = tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"MZ")
    shortcut = tmp_path / "6BUYING_POINT" / "DS_DCF.lnk"
    shortcut.parent.mkdir()
    shortcut.write_bytes(b"lnk")
    calls = []

    monkeypatch.setattr(updater, "_windows_powershell_executable", lambda: powershell.resolve())

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=str(shortcut) + "\n")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    assert updater.create_desktop_shortcut(target) == shortcut.resolve()
    assert Path(calls[0][0][0]).is_absolute()
    assert Path(calls[0][0][0]) == powershell.resolve()
    assert calls[0][1]["env"]["DS_DCF_SHORTCUT_TARGET"] == str(target.resolve())
