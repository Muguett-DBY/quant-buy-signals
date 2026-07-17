"""Build, smoke-test, and package the Windows desktop release."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from desktop.updater import DESKTOP_LIBRARY_NAME, PRODUCT_ID, RELEASE_MANIFEST_SCHEMA_VERSION
from desktop.version import __version__


ROOT = Path(__file__).resolve().parents[1]


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


def _release_manifest() -> dict[str, object]:
    return {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_ID,
        "version": __version__,
        "entrypoint": "DS_DCF.exe",
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


def _desktop_delivery_paths(desktop: Path, zip_name: str) -> tuple[Path, Path, Path, Path]:
    """Return library/version/app/ZIP paths for one immutable desktop release."""
    library = desktop.resolve() / DESKTOP_LIBRARY_NAME
    version_root = library / __version__
    return library, version_root, version_root / "app", version_root / zip_name


def _deliver_to_desktop(release_dir: Path, zip_path: Path, desktop: Path) -> tuple[Path, Path, Path, Path]:
    """Atomically publish one immutable version under the Desktop library."""
    library, version_root, desktop_folder, desktop_zip = _desktop_delivery_paths(desktop, zip_path.name)
    library.mkdir(parents=True, exist_ok=True)
    if version_root.exists():
        raise RuntimeError(f"desktop version folder already exists: {version_root}")
    staging = Path(tempfile.mkdtemp(prefix=f".{__version__}-staging-", dir=library)).resolve()
    try:
        shutil.copytree(release_dir, staging / "app")
        shutil.copy2(zip_path, staging / zip_path.name)
        os.replace(staging, version_root)
    except BaseException:
        _safe_remove_tree(staging, allowed_root=library)
        raise
    return library, version_root, desktop_folder, desktop_zip


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "dist" / "desktop")
    parser.add_argument("--work-root", type=Path, default=ROOT / "build" / "desktop")
    parser.add_argument("--desktop", action="store_true", help="copy the verified folder and ZIP to the Desktop")
    parser.add_argument("--package-url", help="HTTPS URL used to emit a deployable update manifest")
    return parser


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
        "folder": str(release_dir),
        "zip": str(zip_path),
        "zip_size": zip_path.stat().st_size,
        "zip_sha256": package_sha256,
    }
    if args.package_url:
        from desktop.updater import _validate_https_url

        package_url = _validate_https_url(args.package_url, field="package_url")
        update_manifest = {
            "schema_version": 1,
            "product": PRODUCT_ID,
            "version": __version__,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "package_url": package_url,
            "sha256": package_sha256,
            "size": zip_path.stat().st_size,
        }
        manifest_path = output_root / f"DS_DCF-v{__version__}-update-manifest.json"
        manifest_path.write_text(
            json.dumps(update_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        summary["update_manifest"] = str(manifest_path)

    if args.desktop:
        from desktop.updater import create_desktop_shortcut

        desktop = _desktop_directory()
        library, version_root, desktop_folder, desktop_zip = _deliver_to_desktop(release_dir, zip_path, desktop)
        shortcut = create_desktop_shortcut(desktop_folder / "DS_DCF.exe")
        summary.update(
            {
                "desktop_library": str(library),
                "desktop_version_root": str(version_root),
                "desktop_folder": str(desktop_folder),
                "desktop_zip": str(desktop_zip),
                "desktop_shortcut": str(shortcut) if shortcut is not None else None,
            }
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
