# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller one-file bootstrap installer specification.

The builder passes the two generated release artifacts through environment
variables.  Keeping this contract explicit makes it impossible to accidentally
ship an installer whose bundled ZIP and manifest came from different builds.
"""

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


ROOT = Path(SPEC).resolve().parents[1]
PACKAGE = Path(os.environ["DS_DCF_INSTALLER_PACKAGE"]).resolve()
MANIFEST = Path(os.environ["DS_DCF_INSTALLER_MANIFEST"]).resolve()
if not PACKAGE.is_file() or not MANIFEST.is_file():
    raise RuntimeError("installer package or manifest input is missing")

datas = [
    (str(PACKAGE), "."),
    (str(MANIFEST), "."),
]
hiddenimports = collect_submodules("desktop")
for distribution in ("requests",):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(ROOT / "desktop" / "installer.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "bandit", "pip_audit", "setuptools", "wheel"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="DS_DCF_Installer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    # The installer catches expected setup errors itself.  Hidden build checks
    # must fail by exit code instead of waiting on an unattended traceback box.
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / "desktop" / "version_info.txt"),
)
