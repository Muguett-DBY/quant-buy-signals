"""User-level bootstrap installer for a verified DS_DCF desktop release.

The installer is intentionally small and does not require administrator rights.
It carries the exact portable ZIP and its signed-by-hash manifest, validates the
same ZIP safety and integrity rules as the in-app updater, then installs into
the side-by-side Desktop version library.  Future upgrades are handled by the
``检查更新`` button in the installed application.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from desktop.console_output import write_console_message
from desktop.updater import (
    UpdateError,
    UpdateManifest,
    default_version_library_root,
    install_update_package,
    parse_update_manifest,
    verify_update_package,
)
from desktop.version import __version__


PRODUCT_NAME = "DS_DCF"
_PORTABLE_PACKAGE_NAME = f"DS_DCF-v{__version__}-windows-x64-portable.zip"
# PyInstaller preserves each bundled data file's basename.  Keep the release
# version in the embedded filename so the installer always locates the exact
# manifest that was produced beside its portable ZIP.
_BUNDLED_MANIFEST_NAME = f"DS_DCF-v{__version__}-update-manifest.json"
_CANONICAL_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root).resolve() if frozen_root else Path(__file__).resolve().parents[1]


def bundled_release_paths(*, resource_root: str | Path | None = None) -> tuple[Path, Path]:
    """Return the bundled portable ZIP and update manifest paths."""

    root = Path(resource_root).resolve() if resource_root is not None else _resource_root()
    return root / _PORTABLE_PACKAGE_NAME, root / _BUNDLED_MANIFEST_NAME


def load_bundled_manifest(*, resource_root: str | Path | None = None) -> UpdateManifest:
    """Read a bounded local manifest and require it to describe this installer."""

    _package, manifest_path = bundled_release_paths(resource_root=resource_root)
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise UpdateError(f"安装包内的更新清单无法读取：{type(exc).__name__}") from exc
    if len(raw) > 64 * 1024:
        raise UpdateError("安装包内的更新清单过大")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("安装包内的更新清单不是有效 JSON") from exc
    manifest = parse_update_manifest(payload)
    if manifest.version != __version__:
        raise UpdateError("安装程序版本与内置更新清单版本不一致")
    if Path(manifest.package_url).name != _PORTABLE_PACKAGE_NAME:
        raise UpdateError("安装包内的更新清单未指向对应便携包")
    return manifest


def verify_bundled_release(*, resource_root: str | Path | None = None) -> tuple[UpdateManifest, Path]:
    """Validate the installer payload without writing any user files."""

    package, _manifest_path = bundled_release_paths(resource_root=resource_root)
    manifest = load_bundled_manifest(resource_root=resource_root)
    return manifest, verify_update_package(package, manifest)


def _installed_version_ceiling(versions_root: Path) -> str | None:
    """Return the highest complete side-by-side install in one version library."""

    if not versions_root.exists():
        return None
    if (
        not versions_root.is_dir()
        or versions_root.is_symlink()
        or getattr(versions_root, "is_junction", lambda: False)()
    ):
        raise UpdateError("版本库不是安全的本地目录")

    installed: list[tuple[tuple[int, int, int], str]] = []
    try:
        children = tuple(versions_root.iterdir())
    except OSError as exc:
        raise UpdateError(f"无法检查已安装版本：{type(exc).__name__}") from exc
    for version_dir in children:
        match = _CANONICAL_VERSION.fullmatch(version_dir.name)
        if match is None:
            continue
        if not version_dir.is_dir() or version_dir.is_symlink() or getattr(version_dir, "is_junction", lambda: False)():
            raise UpdateError("已安装版本目录不是安全的本地目录")
        app_dir = version_dir / "app"
        executable = app_dir / "DS_DCF.exe"
        release_manifest = app_dir / "release-manifest.json"
        if not executable.is_file() or not release_manifest.is_file():
            continue
        try:
            payload = json.loads(release_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if payload != {
            "schema_version": 1,
            "product": "DS_DCF",
            "version": version_dir.name,
            "entrypoint": "DS_DCF.exe",
        }:
            continue
        installed.append((tuple(int(part) for part in match.groups()), version_dir.name))
    return max(installed)[1] if installed else None


def _highest_installed_version(*roots: Path) -> str | None:
    ceilings = {ceiling for root in roots if (ceiling := _installed_version_ceiling(root)) is not None}
    if not ceilings:
        return None
    return max(
        ceilings,
        key=lambda version: tuple(int(part) for part in _CANONICAL_VERSION.fullmatch(version).groups()),
    )


def install_bundled_release(
    *,
    versions_root: str | Path | None = None,
    create_shortcut: bool = True,
    resource_root: str | Path | None = None,
):
    """Install the verified payload as a first install, never as a downgrade."""

    manifest, package = verify_bundled_release(resource_root=resource_root)
    root = Path(versions_root).expanduser().resolve() if versions_root is not None else default_version_library_root()
    protected_roots = (root,)
    if create_shortcut:
        shortcut_root = default_version_library_root()
        if shortcut_root != root:
            protected_roots += (shortcut_root,)
    installed_ceiling = _highest_installed_version(*protected_roots)
    manifest_version = tuple(int(part) for part in _CANONICAL_VERSION.fullmatch(manifest.version).groups())
    ceiling_version = (
        tuple(int(part) for part in _CANONICAL_VERSION.fullmatch(installed_ceiling).groups())
        if installed_ceiling is not None
        else None
    )
    if ceiling_version is not None and manifest_version < ceiling_version:
        raise UpdateError(f"已安装较新版本 {installed_ceiling}，拒绝用旧安装程序覆盖稳定快捷方式")
    # Re-running the exact current installer remains idempotent: the updater
    # replays every installed byte against the embedded, hash-bound ZIP before
    # it refreshes the shortcut.  A genuinely newer installer is compared
    # against the highest complete version found above.
    current_version = (
        installed_ceiling if installed_ceiling is not None and installed_ceiling != manifest.version else "0.0.0"
    )
    return install_update_package(
        package,
        manifest,
        versions_root=root,
        create_shortcut=create_shortcut,
        current_version=current_version,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DS_DCF Windows bootstrap installer")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--verify-bundle", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-to", type=Path, help="install into this version-library root")
    parser.add_argument("--no-shortcut", action="store_true", help="do not create the stable Desktop shortcut")
    parser.add_argument("--silent", action="store_true", help="install without showing setup dialogs")
    return parser


def _show_message(title: str, message: str, *, error: bool = False) -> None:
    try:
        from tkinter import messagebox

        if error:
            messagebox.showerror(title, message)
        else:
            messagebox.showinfo(title, message)
    except Exception:
        write_console_message(f"{title}: {message}", error=error)


def _write_cli_message(message: str, *, error: bool = False) -> None:
    """Write a CLI result without assuming a windowed executable has stdio."""

    write_console_message(message, error=error)


def _interactive_install(*, versions_root: Path | None, create_shortcut: bool) -> int:
    try:
        from tkinter import messagebox

        root = versions_root.resolve() if versions_root is not None else default_version_library_root()
        confirmed = messagebox.askyesno(
            f"安装 {PRODUCT_NAME}",
            f"将安装 {PRODUCT_NAME} {__version__} 到：\n{root}\n\n"
            "安装包会先校验完整性和文件安全性。之后可在程序控制窗口中点击“检查更新”在线升级。\n\n"
            "是否继续？",
        )
        if not confirmed:
            return 0
        installed = install_bundled_release(versions_root=root, create_shortcut=create_shortcut)
        launch = messagebox.askyesno(
            "安装完成",
            f"{PRODUCT_NAME} {installed.version} 已安装并通过校验。\n\n是否立即启动？",
        )
        if launch:
            import subprocess

            subprocess.Popen([str(installed.executable)], cwd=installed.install_dir)
        return 0
    except UpdateError as exc:
        _show_message("安装失败", str(exc), error=True)
        return 1
    except Exception as exc:
        _show_message("安装失败", f"安装程序发生内部异常：{type(exc).__name__}", error=True)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        _write_cli_message(__version__)
        return 0
    if args.verify_bundle:
        try:
            manifest, package = verify_bundled_release()
        except UpdateError as exc:
            _write_cli_message(f"安装包校验失败：{exc}", error=True)
            return 1
        _write_cli_message(
            json.dumps({"ok": True, "version": manifest.version, "package": str(package)}, ensure_ascii=True)
        )
        return 0
    versions_root = args.install_to.resolve() if args.install_to is not None else None
    if args.silent:
        try:
            installed = install_bundled_release(versions_root=versions_root, create_shortcut=not args.no_shortcut)
        except UpdateError as exc:
            _write_cli_message(f"安装失败：{exc}", error=True)
            return 1
        _write_cli_message(
            json.dumps(
                {"ok": True, "version": installed.version, "install_dir": str(installed.install_dir)},
                ensure_ascii=True,
            )
        )
        return 0
    return _interactive_install(versions_root=versions_root, create_shortcut=not args.no_shortcut)


if __name__ == "__main__":
    raise SystemExit(main())
