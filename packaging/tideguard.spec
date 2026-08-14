# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve().parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
ENTRYPOINT = PROJECT_ROOT / "desktop" / "tideguard_desktop.py"

if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit("frontend/dist/index.html is missing; build the frontend first")


a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(PROJECT_ROOT), str(PROJECT_ROOT / "backend" / "src")],
    binaries=[],
    datas=[(str(FRONTEND_DIST), "frontend/dist")],
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
