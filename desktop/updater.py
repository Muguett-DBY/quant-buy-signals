"""Fail-closed HTTPS updater for side-by-side Windows desktop releases."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import requests

from desktop.version import __version__


PRODUCT_ID = "DS_DCF"
DESKTOP_LIBRARY_NAME = "6BUYING_POINT"
UPDATE_MANIFEST_SCHEMA_VERSION = 1
RELEASE_MANIFEST_SCHEMA_VERSION = 1
MANIFEST_MAX_BYTES = 64 * 1024
PACKAGE_MAX_BYTES = 2 * 1024 * 1024 * 1024
ZIP_MAX_ENTRIES = 20_000
ZIP_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 1_000.0
UPDATE_TIMEOUT = (10, 60)

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class UpdateError(RuntimeError):
    """An update manifest, package, or install operation failed validation."""


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    published_at: str
    package_url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    manifest: UpdateManifest
    update_available: bool


@dataclass(frozen=True)
class InstalledUpdate:
    version: str
    version_dir: Path
    install_dir: Path
    executable: Path
    package: Path
    shortcut: Path | None


def default_version_library_root() -> Path:
    """Return the user-visible, side-by-side version library."""

    override = str(os.environ.get("DS_DCF_VERSION_LIBRARY_ROOT") or "").strip()
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
            if result != 0 or not buffer.value:
                raise OSError(f"SHGetFolderPathW failed: {result}")
            root = Path(buffer.value) / DESKTOP_LIBRARY_NAME
        except (AttributeError, OSError):
            root = Path.home() / "Desktop" / DESKTOP_LIBRARY_NAME
    else:
        root = Path.home() / "Desktop" / DESKTOP_LIBRARY_NAME
    return root.resolve()


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise UpdateError(f"file integrity check failed: {type(exc).__name__}") from exc
    return size, digest.hexdigest()


def _semver_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(str(value).strip())
    if match is None:
        raise UpdateError("version must be a canonical three-part semantic version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _validate_https_url(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpdateError(f"{field} must be a non-empty HTTPS URL")
    text = value.strip()
    parsed = urlsplit(text)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UpdateError(f"{field} must be an HTTPS URL without credentials or fragments")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UpdateError(f"{field} contains an invalid port") from exc
    return text


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise UpdateError(f"JSON contains a non-finite number: {value}")


def _decode_json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise UpdateError(f"{label} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError(f"{label} is not valid JSON") from exc


def _bounded_response_bytes(response: Any, *, limit: int) -> bytes:
    declared = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > limit:
            raise UpdateError("HTTP response exceeds the declared byte limit")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise UpdateError("HTTP response yielded non-byte content")
        total += len(chunk)
        if total > limit:
            raise UpdateError("HTTP response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_update_manifest(payload: object, *, now: datetime | None = None) -> UpdateManifest:
    expected = {
        "schema_version",
        "product",
        "version",
        "published_at",
        "package_url",
        "sha256",
        "size",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise UpdateError("update manifest has an invalid shape")
    if payload.get("schema_version") != UPDATE_MANIFEST_SCHEMA_VERSION or payload.get("product") != PRODUCT_ID:
        raise UpdateError("update manifest product or schema does not match this application")
    version = str(payload.get("version") or "").strip()
    _semver_tuple(version)
    published_at = payload.get("published_at")
    if not isinstance(published_at, str):
        raise UpdateError("update manifest published_at must be an ISO timestamp")
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateError("update manifest published_at is invalid") from exc
    if published.tzinfo is None:
        raise UpdateError("update manifest published_at must include a timezone")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must include a timezone")
    if published > reference.astimezone(timezone.utc).replace(microsecond=0) + timedelta(minutes=5):
        raise UpdateError("update manifest publication time is in the future")
    package_url = _validate_https_url(payload.get("package_url"), field="package_url")
    sha256 = str(payload.get("sha256") or "").strip()
    if _SHA256.fullmatch(sha256) is None:
        raise UpdateError("update manifest sha256 must be lowercase hexadecimal")
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= PACKAGE_MAX_BYTES:
        raise UpdateError("update manifest size is outside the allowed range")
    return UpdateManifest(version, published_at, package_url, sha256, size)


def fetch_update_manifest(
    manifest_url: str,
    *,
    session: Any = requests,
    timeout: tuple[int, int] = UPDATE_TIMEOUT,
) -> UpdateManifest:
    url = _validate_https_url(manifest_url, field="manifest_url")
    try:
        response = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
        try:
            response.raise_for_status()
            _validate_https_url(str(response.url), field="final manifest URL")
            raw = _bounded_response_bytes(response, limit=MANIFEST_MAX_BYTES)
        finally:
            response.close()
    except UpdateError:
        raise
    except requests.RequestException as exc:
        raise UpdateError(f"update manifest request failed: {type(exc).__name__}") from exc
    return parse_update_manifest(_decode_json(raw, label="update manifest"))


def check_for_update(
    manifest_url: str,
    *,
    current_version: str = __version__,
    session: Any = requests,
) -> UpdateCheckResult:
    current = _semver_tuple(current_version)
    manifest = fetch_update_manifest(manifest_url, session=session)
    available = _semver_tuple(manifest.version) > current
    return UpdateCheckResult(current_version=current_version, manifest=manifest, update_available=available)


def _remove_owned_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def download_update_package(
    manifest: UpdateManifest,
    destination: str | os.PathLike[str],
    *,
    session: Any = requests,
    timeout: tuple[int, int] = UPDATE_TIMEOUT,
) -> Path:
    requested_destination = Path(destination).expanduser()
    destination_parent = requested_destination.parent.resolve()
    destination_path = destination_parent / requested_destination.name
    if destination_path.is_symlink() or getattr(destination_path, "is_junction", lambda: False)():
        raise UpdateError("update package destination is a symbolic link or junction")
    destination_parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        response = session.get(manifest.package_url, timeout=timeout, stream=True, allow_redirects=True)
        try:
            response.raise_for_status()
            _validate_https_url(str(response.url), field="final package URL")
            declared = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            if declared not in (None, "") and (not str(declared).isdigit() or int(declared) != manifest.size):
                raise UpdateError("update package Content-Length does not match the update manifest")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination_path.name}.",
                suffix=".part",
                dir=destination_path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        raise UpdateError("update package yielded non-byte content")
                    total += len(chunk)
                    if total > manifest.size or total > PACKAGE_MAX_BYTES:
                        raise UpdateError("update package exceeds the manifest size")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if total != manifest.size:
            raise UpdateError("update package byte count does not match the update manifest")
        if digest.hexdigest() != manifest.sha256:
            raise UpdateError("update package SHA-256 does not match the update manifest")
        if temporary is None:
            raise UpdateError("update package temporary file was not created")
        os.replace(temporary, destination_path)
        temporary = None
        return destination_path
    except UpdateError:
        _remove_owned_temporary(temporary)
        raise
    except requests.RequestException as exc:
        _remove_owned_temporary(temporary)
        raise UpdateError(f"update package request failed: {type(exc).__name__}") from exc
    except OSError as exc:
        _remove_owned_temporary(temporary)
        raise UpdateError(f"update package could not be stored: {type(exc).__name__}") from exc


def _validate_zip_component(component: str) -> None:
    if (
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or any(character in component for character in '<>:"|?*')
        or any(ord(character) < 32 for character in component)
    ):
        raise UpdateError("update ZIP contains an unsafe path component")
    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise UpdateError("update ZIP contains a reserved Windows path")


def _validated_zip_entries(archive: zipfile.ZipFile, *, version: str) -> tuple[str, list[zipfile.ZipInfo]]:
    entries = archive.infolist()
    if not entries or len(entries) > ZIP_MAX_ENTRIES:
        raise UpdateError("update ZIP entry count is outside the allowed range")
    expected_root = f"DS_DCF-v{version}"
    seen: set[str] = set()
    total_uncompressed = 0
    for info in entries:
        name = info.filename
        if not isinstance(name, str) or "\\" in name or name.startswith("/") or len(name) > 240:
            raise UpdateError("update ZIP contains an unsafe path")
        path = PurePosixPath(name)
        parts = path.parts
        if not parts or parts[0] != expected_root:
            raise UpdateError("update ZIP must contain exactly one versioned product root")
        for component in parts:
            _validate_zip_component(component)
        key = "/".join(parts).casefold().rstrip("/")
        if key in seen:
            raise UpdateError("update ZIP contains duplicate or case-colliding paths")
        seen.add(key)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise UpdateError("update ZIP contains a symbolic link")
        if info.flag_bits & 0x1:
            raise UpdateError("update ZIP contains an encrypted entry")
        file_type = stat.S_IFMT(mode)
        if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise UpdateError("update ZIP contains a non-regular filesystem entry")
        if file_type and stat.S_ISDIR(mode) != info.is_dir():
            raise UpdateError("update ZIP entry type disagrees with its path syntax")
        if len(parts) == 1 and not info.is_dir():
            raise UpdateError("update ZIP product root must be a directory")
        if info.is_dir() and (info.file_size != 0 or info.compress_size != 0):
            raise UpdateError("update ZIP directory entry contains data")
        if info.file_size < 0 or info.compress_size < 0:
            raise UpdateError("update ZIP contains an invalid entry size")
        total_uncompressed += info.file_size
        if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
            raise UpdateError("update ZIP uncompressed size exceeds the limit")
        if info.file_size and info.file_size / max(info.compress_size, 1) > ZIP_MAX_COMPRESSION_RATIO:
            raise UpdateError("update ZIP contains an excessive compression ratio")
    required = {
        f"{expected_root}/DS_DCF.exe",
        f"{expected_root}/release-manifest.json",
    }
    if not required.issubset({info.filename for info in entries}):
        raise UpdateError("update ZIP is missing its executable or internal release manifest")
    return expected_root, entries


def _validate_internal_release_manifest(raw: bytes, *, version: str) -> None:
    payload = _decode_json(raw, label="internal release manifest")
    expected = {"schema_version", "product", "version", "entrypoint"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise UpdateError("internal release manifest has an invalid shape")
    if payload != {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_ID,
        "version": version,
        "entrypoint": "DS_DCF.exe",
    }:
        raise UpdateError("internal release manifest does not match the requested version")


def _relative_archive_files(entries: list[zipfile.ZipInfo], package_root: str) -> dict[str, zipfile.ZipInfo]:
    result: dict[str, zipfile.ZipInfo] = {}
    prefix = package_root + "/"
    for info in entries:
        if info.is_dir():
            continue
        relative = info.filename.removeprefix(prefix)
        if not relative or relative == info.filename:
            raise UpdateError("update ZIP file is outside the product root")
        result[relative.casefold()] = info
    return result


def _verify_install_against_archive(
    install_dir: Path,
    stored_package: Path,
    archive: zipfile.ZipFile,
    package_root: str,
    entries: list[zipfile.ZipInfo],
    manifest: UpdateManifest,
) -> Path:
    """Verify an install byte-for-byte against the already SHA-256-bound ZIP."""

    install_is_junction = getattr(install_dir, "is_junction", lambda: False)
    package_is_junction = getattr(stored_package, "is_junction", lambda: False)
    if install_dir.is_symlink() or install_is_junction():
        raise UpdateError("installed application directory is a symbolic link or junction")
    if stored_package.is_symlink() or package_is_junction():
        raise UpdateError("stored portable package is a symbolic link or junction")
    if not install_dir.is_dir():
        raise UpdateError("installed application directory is missing")
    version_root = install_dir.parent.resolve()
    resolved_install = install_dir.resolve()
    resolved_package = stored_package.resolve()
    if resolved_install.parent != version_root or resolved_package.parent != version_root:
        raise UpdateError("installed application or package escapes its version directory")
    expected = _relative_archive_files(entries, package_root)
    actual: dict[str, Path] = {}
    try:
        for path in install_dir.rglob("*"):
            is_junction = getattr(path, "is_junction", lambda: False)
            if path.is_symlink() or is_junction():
                raise UpdateError("installed application contains a symbolic link or junction")
            resolved = path.resolve()
            if resolved_install not in resolved.parents and resolved != resolved_install:
                raise UpdateError("installed application path escapes its version directory")
            if path.is_file():
                relative = path.relative_to(install_dir).as_posix()
                key = relative.casefold()
                if key in actual:
                    raise UpdateError("installed application contains case-colliding files")
                actual[key] = path
    except OSError as exc:
        raise UpdateError(f"installed application enumeration failed: {type(exc).__name__}") from exc
    if set(actual) != set(expected):
        missing = len(set(expected) - set(actual))
        extra = len(set(actual) - set(expected))
        raise UpdateError(f"installed application file set differs from package (missing={missing}, extra={extra})")

    for key, info in expected.items():
        actual_size, actual_digest = _sha256_file(actual[key])
        if actual_size != info.file_size:
            raise UpdateError(f"installed file size differs from package: {info.filename}")
        expected_digest = hashlib.sha256()
        try:
            with archive.open(info, "r") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    expected_digest.update(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise UpdateError(f"update ZIP entry could not be verified: {info.filename}") from exc
        if actual_digest != expected_digest.hexdigest():
            raise UpdateError(f"installed file SHA-256 differs from package: {info.filename}")

    package_size, package_digest = _sha256_file(stored_package)
    if package_size != manifest.size or package_digest != manifest.sha256:
        raise UpdateError("stored portable package differs from the update manifest")
    executable = install_dir / "DS_DCF.exe"
    internal = install_dir / "release-manifest.json"
    if not executable.is_file() or not internal.is_file():
        raise UpdateError("installed update is missing its executable or release manifest")
    try:
        _validate_internal_release_manifest(internal.read_bytes(), version=manifest.version)
    except OSError as exc:
        raise UpdateError(f"installed release manifest could not be read: {type(exc).__name__}") from exc
    return executable


def verify_update_package(
    package_path: str | os.PathLike[str],
    manifest: UpdateManifest,
) -> Path:
    """Verify a local update ZIP without changing the installed version library.

    The bootstrap installer uses this same gate before its first installation,
    so a release ZIP cannot have a weaker trust path than an in-app update.
    """

    package = Path(package_path).expanduser().resolve()
    if not package.is_file():
        raise UpdateError("update package does not exist")
    package_size, package_digest = _sha256_file(package)
    if package_size != manifest.size:
        raise UpdateError("stored update package size does not match the manifest")
    if package_digest != manifest.sha256:
        raise UpdateError("stored update package SHA-256 does not match the manifest")
    try:
        with zipfile.ZipFile(package, "r") as archive:
            package_root, entries = _validated_zip_entries(archive, version=manifest.version)
            internal_name = f"{package_root}/release-manifest.json"
            try:
                _validate_internal_release_manifest(archive.read(internal_name), version=manifest.version)
            except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                raise UpdateError("update ZIP internal release manifest could not be read") from exc
    except UpdateError:
        raise
    except zipfile.BadZipFile as exc:
        raise UpdateError("update package is not a valid ZIP archive") from exc
    except OSError as exc:
        raise UpdateError(f"update package could not be read: {type(exc).__name__}") from exc
    return package


def install_update_package(
    package_path: str | os.PathLike[str],
    manifest: UpdateManifest,
    *,
    versions_root: str | os.PathLike[str] | None = None,
    create_shortcut: bool = True,
    current_version: str = __version__,
) -> InstalledUpdate:
    if _semver_tuple(manifest.version) <= _semver_tuple(current_version):
        raise UpdateError("refusing to install the same version or a downgrade")
    package = verify_update_package(package_path, manifest)

    root = Path(versions_root).expanduser().resolve() if versions_root is not None else default_version_library_root()
    target_version = (root / manifest.version).resolve()
    if target_version.parent != root:
        raise UpdateError("update target escapes the version store")
    target_install = target_version / "app"
    portable_name = f"DS_DCF-v{manifest.version}-windows-x64-portable.zip"
    target_package = target_version / portable_name
    staging_parent: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package, "r") as archive:
            package_root, entries = _validated_zip_entries(archive, version=manifest.version)
            internal_name = f"{package_root}/release-manifest.json"
            try:
                _validate_internal_release_manifest(archive.read(internal_name), version=manifest.version)
            except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                raise UpdateError("update ZIP internal release manifest could not be read") from exc
            if target_version.exists():
                executable = _verify_install_against_archive(
                    target_install,
                    target_package,
                    archive,
                    package_root,
                    entries,
                    manifest,
                )
                shortcut = create_desktop_shortcut(executable) if create_shortcut else None
                return InstalledUpdate(
                    manifest.version,
                    target_version,
                    target_install,
                    executable,
                    target_package,
                    shortcut,
                )

            staging_parent = Path(tempfile.mkdtemp(prefix=f".{manifest.version}-", dir=root)).resolve()
            staging_version = (staging_parent / manifest.version).resolve()
            staging_install = staging_version / "app"
            staging_package = staging_version / portable_name
            staging_install.mkdir(parents=True, exist_ok=False)
            for info in entries:
                relative = PurePosixPath(info.filename)
                destination = staging_install.joinpath(*relative.parts[1:]).resolve()
                if staging_install not in destination.parents and destination != staging_install:
                    raise UpdateError("update ZIP extraction escaped the staging directory")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, open(destination, "xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            shutil.copyfile(package, staging_package)
            _verify_install_against_archive(
                staging_install,
                staging_package,
                archive,
                package_root,
                entries,
                manifest,
            )
            os.replace(staging_version, target_version)
    except UpdateError:
        raise
    except zipfile.BadZipFile as exc:
        raise UpdateError("update package is not a valid ZIP archive") from exc
    except (OSError, RuntimeError, shutil.Error) as exc:
        raise UpdateError(f"update installation failed: {type(exc).__name__}") from exc
    finally:
        if staging_parent is not None:
            shutil.rmtree(staging_parent, ignore_errors=True)

    executable = target_install / "DS_DCF.exe"
    shortcut = create_desktop_shortcut(executable) if create_shortcut else None
    return InstalledUpdate(
        manifest.version,
        target_version,
        target_install,
        executable,
        target_package,
        shortcut,
    )


def _windows_powershell_executable() -> Path:
    if os.name != "nt":
        raise UpdateError("PowerShell shortcut creation is available only on Windows")
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError) as exc:
        raise UpdateError("Windows system directory lookup failed") from exc
    if length <= 0 or length >= len(buffer) or not buffer.value:
        raise UpdateError("Windows system directory lookup failed")
    executable = (Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe").resolve()
    if not executable.is_file():
        raise UpdateError("the system PowerShell executable is unavailable")
    return executable


def create_desktop_shortcut(executable: str | os.PathLike[str]) -> Path | None:
    """Create the stable shortcut inside the user's version library."""
    if os.name != "nt":
        return None
    target = Path(executable).resolve()
    if not target.is_file() or target.name.casefold() != "ds_dcf.exe":
        raise UpdateError("desktop shortcut target is not a DS_DCF executable")
    script = """
$desktop = [Environment]::GetFolderPath('Desktop')
$library = Join-Path $desktop '6BUYING_POINT'
[System.IO.Directory]::CreateDirectory($library) | Out-Null
$shortcutPath = Join-Path $library 'DS_DCF.lnk'
$target = [Environment]::GetEnvironmentVariable('DS_DCF_SHORTCUT_TARGET')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = Split-Path -Parent $target
$shortcut.Description = 'DS_DCF valuation and seven-framework diagnostics'
$shortcut.Save()
Write-Output $shortcutPath
""".strip()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = dict(os.environ)
    environment["DS_DCF_SHORTCUT_TARGET"] = str(target)
    powershell = _windows_powershell_executable()
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"desktop shortcut creation failed: {type(exc).__name__}") from exc
    shortcut_text = completed.stdout.strip().splitlines()
    if not shortcut_text:
        raise UpdateError("desktop shortcut creation returned no path")
    shortcut = Path(shortcut_text[-1]).resolve()
    if (
        shortcut.name.casefold() != "ds_dcf.lnk"
        or shortcut.parent.name != DESKTOP_LIBRARY_NAME
        or not shortcut.is_file()
    ):
        raise UpdateError("desktop shortcut was not created at the expected path")
    return shortcut


def _read_update_config(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateError(f"update_config.json could not be read: {type(exc).__name__}") from exc
    if len(raw) > MANIFEST_MAX_BYTES:
        raise UpdateError("update_config.json exceeds the byte limit")
    payload = _decode_json(raw, label="update_config.json")
    if not isinstance(payload, Mapping) or set(payload) != {"manifest_url"}:
        raise UpdateError("update_config.json has an invalid shape")
    value = payload.get("manifest_url")
    return None if value is None else _validate_https_url(value, field="manifest_url")


def load_update_manifest_url(
    app_root: str | os.PathLike[str],
    *,
    library_root: str | os.PathLike[str] | None = None,
) -> str | None:
    environment = str(os.environ.get("DS_DCF_UPDATE_MANIFEST_URL") or "").strip()
    if environment:
        return _validate_https_url(environment, field="DS_DCF_UPDATE_MANIFEST_URL")
    external_root = (
        Path(library_root).expanduser().resolve() if library_root is not None else default_version_library_root()
    )
    external = external_root / "update_config.json"
    bundled = Path(app_root).resolve() / "update_config.json"
    if external.is_file():
        return _read_update_config(external)
    if bundled.is_file():
        return _read_update_config(bundled)
    return None


__all__ = [
    "DESKTOP_LIBRARY_NAME",
    "InstalledUpdate",
    "PRODUCT_ID",
    "UpdateCheckResult",
    "UpdateError",
    "UpdateManifest",
    "check_for_update",
    "create_desktop_shortcut",
    "download_update_package",
    "default_version_library_root",
    "fetch_update_manifest",
    "install_update_package",
    "load_update_manifest_url",
    "parse_update_manifest",
    "verify_update_package",
]
