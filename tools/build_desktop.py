"""Build, smoke-test, and package the Windows desktop release."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from desktop.updater import (
    BUILD_PROVENANCE_SCHEMA_VERSION,
    DESKTOP_LIBRARY_NAME,
    PRODUCT_ID,
    RELEASE_MANIFEST_SCHEMA_VERSION,
    _canonical_manifest_bytes,
    _verify_manifest_signature,
)
from desktop.version import __version__


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RELEASE_BASE_URL = "https://github.com/Muguett-DBY/quant-buy-signals/releases/download"
DESKTOP_SIGNER = ROOT / "tools" / "sign_desktop_update_manifest.ps1"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _inside(parent: Path, child: Path) -> bool:
    resolved_parent = parent.resolve()
    resolved_child = child.resolve()
    return resolved_child == resolved_parent or resolved_parent in resolved_child.parents


def _safe_remove_tree(path: Path, *, allowed_root: Path) -> None:
    resolved = path.resolve()
    if not _inside(allowed_root, resolved) or resolved == allowed_root.resolve():
        raise RuntimeError(f"refusing to remove path outside build root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _project_version() -> str:
    import tomllib

    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _git_output(arguments: Sequence[str], *, root: Path = ROOT) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Git release provenance check failed: {type(exc).__name__}") from exc
    return completed.stdout.strip()


def _require_clean_committed_git_sha(
    *,
    root: Path = ROOT,
    test_sha: str | None = None,
) -> str:
    """Return HEAD only for a clean committed tree.

    ``test_sha`` exists solely so unit tests can exercise build helpers without
    creating a temporary repository.  The release CLI never passes it.
    """

    if test_sha is not None:
        normalized = str(test_sha).strip().lower()
        if _GIT_SHA.fullmatch(normalized) is None:
            raise RuntimeError("injected test Git SHA is invalid")
        return normalized
    repository = Path(_git_output(("rev-parse", "--show-toplevel"), root=root)).resolve()
    if repository != root.resolve():
        raise RuntimeError("desktop release must be built from the repository root")
    git_sha = _git_output(("rev-parse", "--verify", "HEAD"), root=root).lower()
    if _GIT_SHA.fullmatch(git_sha) is None:
        raise RuntimeError("Git HEAD is not an exact commit SHA")
    if _git_output(("status", "--porcelain=v1", "--untracked-files=all"), root=root):
        raise RuntimeError("desktop release requires a clean committed Git work tree")
    if _git_output(("cat-file", "-t", git_sha), root=root) != "commit":
        raise RuntimeError("Git HEAD does not identify a commit object")
    return git_sha


def _release_manifest() -> dict[str, object]:
    return {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_ID,
        "version": __version__,
        "entrypoint": "DS_DCF.exe",
    }


def _build_provenance(git_sha: str) -> dict[str, object]:
    normalized = str(git_sha).strip().lower()
    if _GIT_SHA.fullmatch(normalized) is None:
        raise RuntimeError("build provenance requires an exact Git commit SHA")
    return {
        "schema_version": BUILD_PROVENANCE_SCHEMA_VERSION,
        "product": PRODUCT_ID,
        "version": __version__,
        "git_sha": normalized,
    }


def _write_deterministic_zip(source: Path, destination: Path) -> None:
    root_name = source.name
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix().casefold()):
            if not path.is_file() or path.is_symlink():
                continue
            relative = Path(root_name) / path.relative_to(source)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_signing_private_key_path(explicit: Path | None) -> Path | None:
    environment_path = str(os.environ.get("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH") or "").strip()
    if explicit is not None and environment_path:
        raise RuntimeError("desktop signing private key path was provided twice")
    candidate = explicit if explicit is not None else (Path(environment_path) if environment_path else None)
    if candidate is None:
        return None
    expanded = candidate.expanduser()
    if expanded.is_symlink():
        raise RuntimeError("desktop signing private key path must not be a symbolic link")
    resolved = expanded.resolve()
    if _inside(ROOT, resolved):
        raise RuntimeError("desktop signing private key must be stored outside the repository")
    if not resolved.is_file():
        raise RuntimeError("desktop signing private key path is not a regular external file")
    return resolved


def _validate_signing_configuration(explicit: Path | None) -> Path | None:
    key_path = _resolve_signing_private_key_path(explicit)
    encoded_key = str(os.environ.get("DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64") or "").strip()
    if key_path is not None and encoded_key:
        raise RuntimeError("desktop signing private key was provided by both file and environment")
    if key_path is None and not encoded_key:
        raise RuntimeError(
            "desktop update signing key is required via --signing-private-key, "
            "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH, or DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64"
        )
    if not shutil.which("pwsh"):
        raise RuntimeError("PowerShell 7 is required to sign the desktop update manifest")
    if not DESKTOP_SIGNER.is_file():
        raise RuntimeError("desktop update manifest signer is missing")
    return key_path


def _validate_ci_smoke_configuration(
    *,
    output_root: Path,
    work_root: Path,
    desktop: bool,
    package_url: str | None,
    signing_private_key: Path | None,
) -> None:
    """Keep the unsigned CI path isolated from every release-capable path."""

    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("--ci-smoke is reserved for GitHub Actions")
    if desktop or package_url is not None or signing_private_key is not None:
        raise RuntimeError("--ci-smoke cannot be combined with desktop delivery or release signing options")
    if any(
        str(os.environ.get(name) or "").strip()
        for name in (
            "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_PATH",
            "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64",
        )
    ):
        raise RuntimeError("--ci-smoke refuses desktop signing key environment variables")
    build_root = (ROOT / "build").resolve()
    if (
        output_root == build_root
        or work_root == build_root
        or not _inside(build_root, output_root)
        or not _inside(build_root, work_root)
        or _inside(output_root, work_root)
        or _inside(work_root, output_root)
    ):
        raise RuntimeError("--ci-smoke output and work roots must be separate directories below build/")


def _sign_canonical_manifest(
    canonical_manifest: bytes,
    *,
    private_key_path: Path | None = None,
) -> bytes:
    if not canonical_manifest or len(canonical_manifest) > 64 * 1024:
        raise RuntimeError("canonical update manifest is outside the signing size limit")
    key_path = _validate_signing_configuration(private_key_path)
    powershell = shutil.which("pwsh")
    if powershell is None:  # pragma: no cover - checked by _validate_signing_configuration
        raise RuntimeError("PowerShell 7 is required to sign the desktop update manifest")
    with tempfile.TemporaryDirectory(prefix="ds-dcf-desktop-sign-") as temporary_directory:
        temporary = Path(temporary_directory)
        payload_path = temporary / "manifest.canonical.json"
        signature_path = temporary / "manifest.sig"
        payload_path.write_bytes(canonical_manifest)
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(DESKTOP_SIGNER),
            "-Manifest",
            str(payload_path),
            "-Output",
            str(signature_path),
        ]
        if key_path is not None:
            command.extend(("-PrivateKeyPath", str(key_path)))
        try:
            subprocess.run(
                command,
                cwd=ROOT,
                check=True,
                capture_output=True,
                timeout=60,
                env=dict(os.environ),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"desktop update manifest signing failed: {type(exc).__name__}") from exc
        try:
            signature = signature_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("desktop update signer did not produce a signature") from exc
    try:
        payload = json.loads(canonical_manifest.decode("utf-8"))
        _verify_manifest_signature(payload, signature)
    except Exception as exc:
        raise RuntimeError("desktop update signer did not produce a signature for the pinned public key") from exc
    return signature


def _write_signed_update_manifests(
    payload: dict[str, object],
    *,
    output_root: Path,
    private_key_path: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    canonical = _canonical_manifest_bytes(payload)
    signature = _sign_canonical_manifest(canonical, private_key_path=private_key_path)
    versioned_manifest = output_root / f"DS_DCF-v{__version__}-update-manifest.json"
    versioned_signature = output_root / f"{versioned_manifest.name}.sig"
    stable_manifest = output_root / "update-manifest.json"
    stable_signature = output_root / "update-manifest.json.sig"
    versioned_manifest.write_bytes(canonical + b"\n")
    versioned_signature.write_bytes(signature)
    shutil.copyfile(versioned_manifest, stable_manifest)
    shutil.copyfile(versioned_signature, stable_signature)
    if (
        stable_manifest.read_bytes() != versioned_manifest.read_bytes()
        or stable_signature.read_bytes() != versioned_signature.read_bytes()
    ):
        raise RuntimeError("stable and versioned desktop update manifests differ")
    return versioned_manifest, versioned_signature, stable_manifest, stable_signature


def _desktop_directory() -> Path:
    if os.name != "nt":
        raise RuntimeError("desktop delivery is available only on Windows")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise RuntimeError(f"Windows desktop path lookup failed: {result}")
    path = Path(buffer.value).resolve()
    if not path.is_dir():
        raise RuntimeError(f"Windows desktop path does not exist: {path}")
    return path


def _desktop_delivery_paths(
    desktop: Path,
    zip_name: str,
    installer_name: str,
    manifest_name: str,
    signature_name: str,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    """Return version-library paths for one immutable desktop release."""
    library = desktop.resolve() / DESKTOP_LIBRARY_NAME
    version_root = library / __version__
    return (
        library,
        version_root,
        version_root / "app",
        version_root / zip_name,
        version_root / installer_name,
        version_root / manifest_name,
        version_root / signature_name,
    )


def _deliver_to_desktop(
    release_dir: Path,
    zip_path: Path,
    installer_path: Path,
    manifest_path: Path,
    signature_path: Path,
    desktop: Path,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    """Atomically publish app, portable ZIP, installer, and manifest under one version folder."""

    (
        library,
        version_root,
        desktop_folder,
        desktop_zip,
        desktop_installer,
        desktop_manifest,
        desktop_signature,
    ) = _desktop_delivery_paths(
        desktop,
        zip_path.name,
        installer_path.name,
        manifest_path.name,
        signature_path.name,
    )
    library.mkdir(parents=True, exist_ok=True)
    if version_root.exists():
        raise RuntimeError(f"desktop version folder already exists: {version_root}")
    staging = Path(tempfile.mkdtemp(prefix=f".{__version__}-staging-", dir=library)).resolve()
    try:
        shutil.copytree(release_dir, staging / "app")
        shutil.copy2(zip_path, staging / zip_path.name)
        shutil.copy2(installer_path, staging / installer_path.name)
        shutil.copy2(manifest_path, staging / manifest_path.name)
        shutil.copy2(signature_path, staging / signature_path.name)
        os.replace(staging, version_root)
    except BaseException:
        _safe_remove_tree(staging, allowed_root=library)
        raise
    return (
        library,
        version_root,
        desktop_folder,
        desktop_zip,
        desktop_installer,
        desktop_manifest,
        desktop_signature,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "dist" / "desktop")
    parser.add_argument("--work-root", type=Path, default=ROOT / "build" / "desktop")
    parser.add_argument("--desktop", action="store_true", help="copy the verified folder and ZIP to the Desktop")
    parser.add_argument("--package-url", help="HTTPS URL used to emit a deployable update manifest")
    parser.add_argument(
        "--signing-private-key",
        type=Path,
        help="external PKCS#8 DER/PEM or properties file used only to sign the update manifest",
    )
    parser.add_argument(
        "--ci-smoke",
        action="store_true",
        help="GitHub Actions-only unpublishable executable and installer smoke test",
    )
    return parser


def _default_package_url() -> str:
    return f"{PUBLIC_RELEASE_BASE_URL}/v{__version__}/DS_DCF-v{__version__}-windows-x64-portable.zip"


def _build_installer(*, package: Path, manifest: Path, output_root: Path, work_root: Path) -> Path:
    """Build and smoke-test the one-file first-install bootstrapper."""

    pyinstaller_dist = work_root / "installer-dist"
    pyinstaller_work = work_root / "installer-work"
    _safe_remove_tree(pyinstaller_dist, allowed_root=work_root)
    _safe_remove_tree(pyinstaller_work, allowed_root=work_root)
    environment = dict(os.environ)
    environment["DS_DCF_INSTALLER_PACKAGE"] = str(package.resolve())
    environment["DS_DCF_INSTALLER_MANIFEST"] = str(manifest.resolve())
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(pyinstaller_dist),
            "--workpath",
            str(pyinstaller_work),
            str(ROOT / "desktop" / "DS_DCF_Installer.spec"),
        ],
        cwd=ROOT,
        check=True,
        env=environment,
    )
    built = pyinstaller_dist / "DS_DCF_Installer.exe"
    if not built.is_file():
        raise RuntimeError("PyInstaller did not produce the DS_DCF installer")
    installer = output_root / f"DS_DCF-v{__version__}-windows-x64-installer.exe"
    installer.unlink(missing_ok=True)
    shutil.copy2(built, installer)
    subprocess.run([str(installer), "--version"], cwd=output_root, check=True, timeout=60)
    subprocess.run([str(installer), "--verify-bundle"], cwd=output_root, check=True, timeout=180)
    return installer


def main(argv: Sequence[str] | None = None) -> int:
    if os.name != "nt":
        raise RuntimeError("the DS_DCF desktop release must be built on Windows")
    args = _parser().parse_args(argv)
    if _project_version() != __version__:
        raise RuntimeError("pyproject.toml and desktop.version disagree")
    output_root = args.output_root.resolve()
    work_root = args.work_root.resolve()
    if not _inside(ROOT, output_root) or not _inside(ROOT, work_root):
        raise RuntimeError("build and output roots must stay inside the repository")
    if args.ci_smoke:
        _validate_ci_smoke_configuration(
            output_root=output_root,
            work_root=work_root,
            desktop=bool(args.desktop),
            package_url=args.package_url,
            signing_private_key=args.signing_private_key,
        )
        _safe_remove_tree(output_root, allowed_root=(ROOT / "build").resolve())
    else:
        _validate_signing_configuration(args.signing_private_key)
    git_sha = _require_clean_committed_git_sha()
    output_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    pyinstaller_dist = work_root / "pyinstaller-dist"
    pyinstaller_work = work_root / "pyinstaller-work"
    _safe_remove_tree(pyinstaller_dist, allowed_root=work_root)
    _safe_remove_tree(pyinstaller_work, allowed_root=work_root)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(pyinstaller_dist),
        "--workpath",
        str(pyinstaller_work),
        str(ROOT / "desktop" / "DS_DCF.spec"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    built = pyinstaller_dist / "DS_DCF"
    executable = built / "DS_DCF.exe"
    if not executable.is_file():
        raise RuntimeError("PyInstaller did not produce DS_DCF.exe")

    release_dir = output_root / f"DS_DCF-v{__version__}"
    _safe_remove_tree(release_dir, allowed_root=output_root)
    shutil.copytree(built, release_dir)
    (release_dir / "release-manifest.json").write_text(
        json.dumps(_release_manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (release_dir / "build-provenance.json").write_text(
        json.dumps(_build_provenance(git_sha), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    shutil.copy2(ROOT / "README.md", release_dir / "README.md")
    shutil.copy2(ROOT / "LICENSE", release_dir / "LICENSE")
    release_executable = release_dir / "DS_DCF.exe"
    subprocess.run([str(release_executable), "--version"], cwd=release_dir, check=True, timeout=60)
    subprocess.run([str(release_executable), "--health-check"], cwd=release_dir, check=True, timeout=120)
    subprocess.run([str(release_executable), "--server-smoke-test"], cwd=release_dir, check=True, timeout=120)

    zip_path = output_root / f"DS_DCF-v{__version__}-windows-x64-portable.zip"
    zip_path.unlink(missing_ok=True)
    _write_deterministic_zip(release_dir, zip_path)
    package_sha256 = _sha256(zip_path)
    summary: dict[str, object] = {
        "product": PRODUCT_ID,
        "version": __version__,
        "ci_smoke": bool(args.ci_smoke),
        "release_ready": not args.ci_smoke,
        "folder": str(release_dir),
        "zip": str(zip_path),
        "zip_size": zip_path.stat().st_size,
        "zip_sha256": package_sha256,
        "git_sha": git_sha,
    }
    from desktop.updater import _validate_https_url

    package_url = _validate_https_url(
        f"https://ci-smoke.invalid/DS_DCF-v{__version__}-windows-x64-portable.zip"
        if args.ci_smoke
        else (args.package_url or _default_package_url()),
        field="package_url",
    )
    update_manifest = {
        "schema_version": 1,
        "product": PRODUCT_ID,
        "version": __version__,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "package_url": package_url,
        "sha256": package_sha256,
        "size": zip_path.stat().st_size,
    }
    if args.ci_smoke:
        # PyInstaller preserves the source basename for bundled data.  The
        # bootstrapper deliberately looks up the release-style manifest name,
        # so the CI-only manifest must use that same basename as a real build.
        installer_manifest = work_root / f"DS_DCF-v{__version__}-update-manifest.json"
        installer_manifest.write_bytes(_canonical_manifest_bytes(update_manifest) + b"\n")
        try:
            installer_path = _build_installer(
                package=zip_path,
                manifest=installer_manifest,
                output_root=output_root,
                work_root=work_root,
            )
        finally:
            installer_manifest.unlink(missing_ok=True)
        summary.update({"installer": str(installer_path)})
    else:
        manifest_path, signature_path, stable_manifest_path, stable_signature_path = _write_signed_update_manifests(
            update_manifest,
            output_root=output_root,
            private_key_path=args.signing_private_key,
        )
        installer_path = _build_installer(
            package=zip_path,
            manifest=manifest_path,
            output_root=output_root,
            work_root=work_root,
        )
        summary.update(
            {
                "update_manifest": str(manifest_path),
                "update_manifest_signature": str(signature_path),
                "stable_update_manifest": str(stable_manifest_path),
                "stable_update_manifest_signature": str(stable_signature_path),
                "installer": str(installer_path),
            }
        )

    if args.desktop:
        from desktop.updater import create_desktop_shortcut

        desktop = _desktop_directory()
        (
            library,
            version_root,
            desktop_folder,
            desktop_zip,
            desktop_installer,
            desktop_manifest,
            desktop_signature,
        ) = _deliver_to_desktop(
            release_dir,
            zip_path,
            installer_path,
            manifest_path,
            signature_path,
            desktop,
        )
        shortcut = create_desktop_shortcut(desktop_folder / "DS_DCF.exe")
        summary.update(
            {
                "desktop_library": str(library),
                "desktop_version_root": str(version_root),
                "desktop_folder": str(desktop_folder),
                "desktop_zip": str(desktop_zip),
                "desktop_installer": str(desktop_installer),
                "desktop_update_manifest": str(desktop_manifest),
                "desktop_update_manifest_signature": str(desktop_signature),
                "desktop_shortcut": str(shortcut) if shortcut is not None else None,
            }
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
