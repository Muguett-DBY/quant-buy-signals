# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


ROOT = Path(SPEC).resolve().parents[1]

datas = [
    (str(ROOT / "app.py"), "."),
    (str(ROOT / "desktop" / "update_config.json"), "."),
    (str(ROOT / "data" / "financial_balance_sheet_evidence.json"), "data"),
    (str(ROOT / "data" / "financial_zero_capex_evidence.json"), "data"),
    (str(ROOT / "data" / "financial_zero_revenue_evidence.json"), "data"),
    (str(ROOT / "data" / "industry_f10.json"), "data"),
    (str(ROOT / "data" / "industry_em_map.json"), "data"),
    (str(ROOT / "data" / "industry_capco_2025h2.json"), "data"),
    (str(ROOT / "data" / "industry_exchange_new_listings_2026.json"), "data"),
    (str(ROOT / "tools" / "china_a_share_trading_calendar.json"), "tools"),
]
binaries = []
hiddenimports = []

for package in ("streamlit", "plotly", "altair"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

for package in ("data", "engine", "ui", "desktop"):
    hiddenimports += collect_submodules(package)

for distribution in ("streamlit", "plotly", "altair", "pandas", "numpy", "requests", "orjson"):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
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
    [],
    exclude_binaries=True,
    name="DS_DCF",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / "desktop" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DS_DCF",
)
