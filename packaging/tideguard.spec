# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve().parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
BRAND_ASSETS = PROJECT_ROOT / "assets" / "brand"
ENTRYPOINT = PROJECT_ROOT / "desktop" / "tideguard_desktop.py"
APP_ICON = PROJECT_ROOT / "assets" / "brand" / "moheng.ico"
PROJECT_LICENSE = PROJECT_ROOT / "LICENSE"
THIRD_PARTY_NOTICES = PROJECT_ROOT / "THIRD-PARTY-NOTICES.md"
VERSION_INFO = PROJECT_ROOT / "packaging" / "windows-version-info.txt"

if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit("frontend/dist/index.html is missing; build the frontend first")
if not APP_ICON.is_file():
    raise SystemExit("assets/brand/moheng.ico is missing")
if not PROJECT_LICENSE.is_file():
    raise SystemExit("LICENSE is missing")
if not THIRD_PARTY_NOTICES.is_file():
    raise SystemExit("THIRD-PARTY-NOTICES.md is missing")
if not VERSION_INFO.is_file():
    raise SystemExit("packaging/windows-version-info.txt is missing")


a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(PROJECT_ROOT), str(PROJECT_ROOT / "backend" / "src")],
    binaries=[],
    datas=[
        (str(FRONTEND_DIST), "frontend/dist"),
        (str(BRAND_ASSETS), "assets/brand"),
        (str(PROJECT_LICENSE), "."),
        (str(THIRD_PARTY_NOTICES), "."),
    ],
    hiddenimports=[
        "clr",
        "keyring.backends.Windows",
        "uvicorn.lifespan.on",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "webview.platforms.android",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.qt",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Tideguard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(APP_ICON),
    version=str(VERSION_INFO),
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Tideguard",
)
