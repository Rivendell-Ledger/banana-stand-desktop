# -*- mode: python ; coding: utf-8 -*-
"""One-folder Windows build: BananaStand.exe plus _internal/.

One-folder (not one-file) starts faster and is far less likely to be quarantined
by antivirus. The Inno Setup installer wraps this output.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    ("migrations", "migrations"),
    ("assets", "assets"),
    ("SETUP-WINDOWS.txt", "."),
]
datas += collect_data_files("certifi")
datas += collect_data_files("tzdata")

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("pystray")
    + [
        "app", "app.main", "app.config", "app.db",
        "app.alerts", "app.bitcoin", "app.budgets", "app.categories", "app.csv_import",
        "app.forecast", "app.goals", "app.maintenance", "app.market", "app.money",
        "app.networth", "app.performance", "app.plaid", "app.plaid_sync", "app.portfolio",
        "app.recovery", "app.recurring", "app.reports", "app.retirement", "app.rules",
        "app.security", "app.settings_store", "app.txns",
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    ]
)

a = Analysis(
    ["run_banana_stand.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BananaStand",
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
    icon="assets/banana.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BananaStand",
)
