"""Create the public, hash-bound update manifest for a signed Android APK.

The Android client pins the GitHub HTTPS hosts, verifies the APK size and
SHA-256 from this file, and finally verifies that the downloaded APK has the
same signing certificate as the installed application.  The manifest therefore
does not contain release credentials and is safe to publish beside the APK.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Sequence
import zipfile


GITHUB_RELEASE_BASE = "https://github.com/Muguett-DBY/quant-buy-signals/releases/download"
ANDROID_PACKAGE_ID = "com.muguett.dsdcf"
RELEASE_CERT_SHA256 = "e818fa2a0d18b12316e826bdaeb1877a62ccb68634b42fdd598c687a74293369"
MAX_APK_BYTES = 50 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_RELEASE_PROVENANCE_SCHEMA_VERSION = 1
ANDROID_RELEASE_PROVENANCE_PRODUCT = "DS_DCF-android"
_TAG_PATTERN = re.compile(r"v[0-9]+(?:\.[0-9]+){2}(?:[-.][A-Za-z0-9]+)*\Z")
_VERSION_NAME_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-.][A-Za-z0-9]+)*\Z")
_APK_ASSET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.apk\Z")
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_BADGING_PATTERN = re.compile(
    r"^package:\s+name='(?P<package>[^']+)'\s+versionCode='(?P<code>[0-9]+)'\s+versionName='(?P<name>[^']+)'",
    re.MULTILINE,
)
_CERT_PATTERN = re.compile(r"certificate SHA-256 digest:\s*([0-9a-fA-F]{64})")
_GRADLE_VERSION_CODE_PATTERN = re.compile(r"^\s*versionCode\s*=\s*([0-9]+)\s*$", re.MULTILINE)
_GRADLE_VERSION_NAME_PATTERN = re.compile(r'^\s*versionName\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_DESKTOP_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


@dataclass(frozen=True)
class ApkMetadata:
    package_id: str
    version_code: int
    version_name: str
    signer_sha256: str


@dataclass(frozen=True)
class SourceReleaseMetadata:
    version_code: int
    version_name: str


def _git_output(arguments: Sequence[str], *, root: Path = PROJECT_ROOT) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exception:
        raise RuntimeError(f"Git release provenance check failed: {type(exception).__name__}") from exception
    return completed.stdout.strip()


def _require_clean_committed_git_sha(
    *,
    root: Path = PROJECT_ROOT,
    test_sha: str | None = None,
) -> str:
    """Return HEAD only when the Android release source tree is clean.

    ``test_sha`` is solely for unit tests.  The formal release CLI never passes
    it and therefore always checks the real repository.
    """

    if test_sha is not None:
        normalized = str(test_sha).strip().lower()
        if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
            raise RuntimeError("injected test Git SHA is invalid")
        return normalized
    repository = Path(_git_output(("rev-parse", "--show-toplevel"), root=root)).resolve()
    if repository != root.resolve():
        raise RuntimeError("Android release manifest must be built from the repository root")
    git_sha = _git_output(("rev-parse", "--verify", "HEAD"), root=root).lower()
    if _GIT_SHA_PATTERN.fullmatch(git_sha) is None:
        raise RuntimeError("Git HEAD is not an exact commit SHA")
    if _git_output(("status", "--porcelain=v1", "--untracked-files=all"), root=root):
        raise RuntimeError("Android release manifest requires a clean committed Git work tree")
    if _git_output(("cat-file", "-t", git_sha), root=root) != "commit":
        raise RuntimeError("Git HEAD does not identify a commit object")
    return git_sha


def read_source_release_metadata(project_root: str | Path = PROJECT_ROOT) -> SourceReleaseMetadata:
    """Read and cross-check the three source-of-truth version declarations."""
    root = Path(project_root)
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        pyproject_version = project["project"]["version"]
        gradle = (root / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
        desktop = (root / "desktop" / "version.py").read_text(encoding="utf-8")
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exception:
        raise ValueError("source release metadata cannot be read") from exception
    code_matches = _GRADLE_VERSION_CODE_PATTERN.findall(gradle)
    android_name_matches = _GRADLE_VERSION_NAME_PATTERN.findall(gradle)
    desktop_matches = _DESKTOP_VERSION_PATTERN.findall(desktop)
    if len(code_matches) != 1 or len(android_name_matches) != 1 or len(desktop_matches) != 1:
        raise ValueError("source release metadata must declare each Android and desktop version exactly once")
    version_code = int(code_matches[0])
    android_version = android_name_matches[0]
    desktop_version = desktop_matches[0]
    if isinstance(pyproject_version, bool) or not isinstance(pyproject_version, str):
        raise ValueError("pyproject project.version must be a string")
    if version_code <= 0:
        raise ValueError("Android source versionCode must be a positive integer")
    if not _VERSION_NAME_PATTERN.fullmatch(android_version):
        raise ValueError("Android source versionName must be a plain release version")
    if pyproject_version != android_version or desktop_version != android_version:
        raise ValueError("Android versionName must match pyproject and desktop versions")
    return SourceReleaseMetadata(version_code=version_code, version_name=android_version)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _android_build_tool(name: str) -> Path:
    roots = [
        os.environ.get("ANDROID_SDK_ROOT"),
        os.environ.get("ANDROID_HOME"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk") if os.name == "nt" else None,
        str(Path.home() / "Android" / "Sdk"),
    ]
    executable_names = (f"{name}.bat", f"{name}.exe", name) if os.name == "nt" else (name,)
    for raw_root in roots:
        if not raw_root:
            continue
        build_tools = Path(raw_root).expanduser() / "build-tools"
        if not build_tools.is_dir():
            continue
        versions = sorted(
            (path for path in build_tools.iterdir() if path.is_dir()),
            key=lambda path: tuple(int(part) if part.isdigit() else -1 for part in re.split(r"[.-]", path.name)),
            reverse=True,
        )
        for version in versions:
            for executable_name in executable_names:
                candidate = version / executable_name
                if candidate.is_file():
                    return candidate.resolve()
    raise ValueError(f"valid signed APK verification requires Android build tool: {name}")


def _android_tool_command(
    tool: Path,
    arguments: Sequence[str],
    *,
    windows: bool | None = None,
    java_executable: str | None = None,
) -> list[str]:
    """Build a shell-free command, including for Android's Windows wrappers.

    Passing an APK filename through ``cmd /c`` is unsafe because valid Windows
    filenames may contain command metacharacters such as ``&``.  Android's
    ``apksigner.bat`` is only a launcher for the adjacent executable JAR, so
    invoke that JAR directly and keep every caller-controlled value as one
    ``CreateProcess`` argument.
    """

    use_windows_wrapper = (os.name == "nt" if windows is None else windows) and tool.suffix.lower() in {
        ".bat",
        ".cmd",
    }
    if not use_windows_wrapper:
        return [str(tool), *arguments]
    jar = tool.parent / "lib" / f"{tool.stem}.jar"
    if not jar.is_file():
        raise ValueError(f"Android batch wrapper has no directly invokable JAR: {tool.name}")
    java = java_executable or _java_executable()
    if not java:
        raise ValueError("valid signed APK verification requires Java on PATH or under JAVA_HOME")
    return [str(java), "-jar", str(jar.resolve()), *arguments]


def _java_executable() -> str | None:
    discovered = shutil.which("java")
    if discovered:
        return discovered
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        return None
    names = ("java.exe", "java") if os.name == "nt" else ("java",)
    for name in names:
        candidate = Path(java_home).expanduser() / "bin" / name
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _run_android_tool(tool: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = _android_tool_command(tool, arguments)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _inspect_apk(apk: Path) -> ApkMetadata:
    if not zipfile.is_zipfile(apk):
        raise ValueError("valid signed APK verification failed: file is not an APK ZIP")
    aapt = _run_android_tool(_android_build_tool("aapt"), "dump", "badging", str(apk))
    if aapt.returncode != 0:
        detail = (aapt.stderr or aapt.stdout).strip().splitlines()
        raise ValueError("valid signed APK metadata verification failed" + (f": {detail[-1]}" if detail else ""))
    match = _BADGING_PATTERN.search(aapt.stdout)
    if match is None:
        raise ValueError("valid signed APK metadata verification failed: package identity is missing")
    verifier = _run_android_tool(
        _android_build_tool("apksigner"),
        "verify",
        "--verbose",
        "--print-certs",
        str(apk),
    )
    if verifier.returncode != 0:
        detail = (verifier.stderr or verifier.stdout).strip().splitlines()
        raise ValueError("valid signed APK signature verification failed" + (f": {detail[-1]}" if detail else ""))
    fingerprints = {value.lower() for value in _CERT_PATTERN.findall(verifier.stdout + "\n" + verifier.stderr)}
    if len(fingerprints) != 1:
        raise ValueError("valid signed APK signature verification failed: expected exactly one signer")
    return ApkMetadata(
        package_id=match.group("package"),
        version_code=int(match.group("code")),
        version_name=match.group("name"),
        signer_sha256=fingerprints.pop(),
    )


def build_android_update_manifest(
    apk_path: str | Path,
    *,
    version_code: int,
    version_name: str,
    release_tag: str,
    asset_name: str | None = None,
) -> dict[str, object]:
    """Return a strict update manifest for one already-signed APK."""
    apk = Path(apk_path)
    if not apk.is_file() or apk.stat().st_size <= 0:
        raise ValueError("APK file is missing or empty")
    if apk.stat().st_size > MAX_APK_BYTES:
        raise ValueError(f"APK exceeds the Android client download limit of {MAX_APK_BYTES} bytes")
    if isinstance(version_code, bool) or not isinstance(version_code, int) or version_code <= 0:
        raise ValueError("version_code must be a positive integer")
    if not _VERSION_NAME_PATTERN.fullmatch(version_name):
        raise ValueError("version_name must be a plain release version")
    if not _TAG_PATTERN.fullmatch(release_tag):
        raise ValueError("release_tag must be a version tag beginning with v")
    if release_tag != f"v{version_name}":
        raise ValueError("release_tag must exactly match v plus version_name")
    name = str(asset_name or apk.name)
    if not _APK_ASSET_PATTERN.fullmatch(name):
        raise ValueError("asset_name must be one safe APK filename")
    source_metadata = read_source_release_metadata()
    if version_code != source_metadata.version_code:
        raise ValueError("version_code must match the Android source versionCode")
    if version_name != source_metadata.version_name:
        raise ValueError("version_name must match pyproject, desktop, and Android sources")
    metadata = _inspect_apk(apk)
    if metadata.package_id != ANDROID_PACKAGE_ID:
        raise ValueError("valid signed APK package_id does not match the DS_DCF Android application")
    if metadata.version_code != version_code:
        raise ValueError("valid signed APK version_code does not match the requested release")
    if metadata.version_name != version_name:
        raise ValueError("valid signed APK version_name does not match the requested release")
    if metadata.signer_sha256 != RELEASE_CERT_SHA256:
        raise ValueError("valid signed APK signer does not match the pinned DS_DCF release certificate")
    return {
        "schema_version": 1,
        "package_id": ANDROID_PACKAGE_ID,
        "version_code": version_code,
        "version_name": version_name,
        "apk_url": f"{GITHUB_RELEASE_BASE}/{release_tag}/{name}",
        "apk_sha256": _sha256(apk),
        "apk_size": apk.stat().st_size,
        "signer_sha256": metadata.signer_sha256,
    }


def write_android_update_manifest(path: str | Path, manifest: dict[str, object]) -> None:
    """Atomically write a canonical public update manifest."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(target)


def build_android_release_provenance(
    manifest: dict[str, object],
    *,
    git_sha: str,
) -> dict[str, object]:
    """Build a local audit sidecar without changing the legacy update schema."""

    normalized_sha = str(git_sha).strip().lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized_sha) is None:
        raise ValueError("Android release provenance requires an exact Git commit SHA")
    canonical_manifest = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        version_code = manifest["version_code"]
        version_name = manifest["version_name"]
        apk_sha256 = manifest["apk_sha256"]
    except KeyError as exception:
        raise ValueError("Android release provenance requires a complete update manifest") from exception
    if isinstance(version_code, bool) or not isinstance(version_code, int) or version_code <= 0:
        raise ValueError("Android release provenance requires a positive version_code")
    if not isinstance(version_name, str) or not _VERSION_NAME_PATTERN.fullmatch(version_name):
        raise ValueError("Android release provenance requires a valid version_name")
    if not isinstance(apk_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", apk_sha256):
        raise ValueError("Android release provenance requires a valid APK SHA-256")
    return {
        "schema_version": ANDROID_RELEASE_PROVENANCE_SCHEMA_VERSION,
        "product": ANDROID_RELEASE_PROVENANCE_PRODUCT,
        "version_code": version_code,
        "version_name": version_name,
        "git_sha": normalized_sha,
        "apk_sha256": apk_sha256,
        "update_manifest_sha256": hashlib.sha256(canonical_manifest).hexdigest(),
    }


def write_android_release_provenance(path: str | Path, provenance: dict[str, object]) -> None:
    """Atomically write the local release provenance sidecar."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(target)


def default_android_release_provenance_path(manifest_path: str | Path) -> Path:
    target = Path(manifest_path)
    return target.with_name("android-release-provenance.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--version-code", type=int, required=True)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--asset-name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--provenance-output",
        type=Path,
        help="local audit sidecar (default: android-release-provenance.json beside --output)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    git_sha = _require_clean_committed_git_sha()
    manifest = build_android_update_manifest(
        args.apk,
        version_code=args.version_code,
        version_name=args.version_name,
        release_tag=args.release_tag,
        asset_name=args.asset_name,
    )
    provenance_path = args.provenance_output or default_android_release_provenance_path(args.output)
    if provenance_path.resolve() == args.output.resolve():
        raise ValueError("release provenance output must differ from the public update manifest")
    provenance = build_android_release_provenance(manifest, git_sha=git_sha)
    write_android_update_manifest(args.output, manifest)
    write_android_release_provenance(provenance_path, provenance)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Android release Git commit: {git_sha}", file=sys.stderr)
    print(f"Android release provenance: {provenance_path.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
