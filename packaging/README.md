# Windows desktop packaging

墨衡 MOHENG uses a deliberately small Windows desktop shell while retaining
the internal Tideguard AppId, data directory and executable name for safe upgrades:

- PyInstaller creates a windowed (`console=False`) **onedir** application.
- The launcher exclusively reserves `127.0.0.1:8791`, passes that already-bound
  socket to Uvicorn, and opens the same-origin page in pywebview/WebView2. The
  fixed official port also prevents the source launcher and packaged backend
  from operating the same local trading state at the same time.
- Windows session mutexes prevent duplicate app and credential-manager windows.
- Inno Setup installs the onedir payload under
  `%LOCALAPPDATA%\Programs\Tideguard` and creates normal uninstall metadata.

The trading application window does not expose a Python-to-JavaScript bridge.
The separate `--credentials` window exposes only status/save/delete methods to
its bundled in-memory HTML and never starts the FastAPI server. Uvicorn access
logging is disabled, and startup failures are recorded without exception bodies
or request headers.

## Prerequisites

- Windows 10 1809 or later, x64
- Python 3.11 x64
- Node.js 22 with Corepack
- Inno Setup 6 for installer builds
- Microsoft Edge WebView2 Runtime. The Inno installer carries Microsoft's
  signed Evergreen Bootstrapper and installs the Runtime when it is missing;
  the portable ZIP expects it to be present already.

## Build locally

From the repository root:

```powershell
.\packaging\build-release.ps1
```

The script installs the pinned desktop extras, performs a frozen-resource
self-test, rejects credential/local-state filenames, verifies the Microsoft
Authenticode signature on the WebView2 bootstrapper, and writes:

- `release/Moheng-<version>-windows-x64.zip`
- `release/Moheng-Setup-<version>.exe`
- `release/Moheng-<version>-manifest.json`
- `release/SHA256SUMS.txt`

Use `-SkipInstaller` when validating a portable build on a machine without Inno
Setup. CI must not use that switch.

Python release dependencies and build tools are locked with distribution hashes
in `packaging/requirements-windows.lock` and
`packaging/build-tools-windows.lock`. Regenerate them only as an explicit review
change:

```powershell
uv pip compile backend\pyproject.toml `
  --extra dev --extra desktop --python-version 3.11 `
  --python-platform x86_64-pc-windows-msvc --generate-hashes `
  --output-file packaging\requirements-windows.lock
uv pip compile packaging\build-requirements.in --python-version 3.11 `
  --python-platform x86_64-pc-windows-msvc --generate-hashes `
  --output-file packaging\build-tools-windows.lock
```

## Credential and uninstall boundary

The PyInstaller specification includes only `frontend/dist` and the reviewed
`assets/brand` PNG/ICO files as application data. It does not collect `.env`
files, SQLite state, release credentials, or
Windows Credential Manager entries. Runtime API credentials remain in Windows
Credential Manager under the separate `Tideguard.OKX.Demo` and
`Tideguard.OKX.Live` service names.

The installer adds **墨衡凭证管理** to the Start menu. It launches the
same windowed executable with `--credentials`, so a clean Windows installation
can set or delete independently isolated Demo/Live credentials without Python, a console, or a plaintext
file. This small isolated window never returns stored credential values to its
HTML; it only reports configured/not configured status.

The release build also refuses active `VITE_*` environment variables and local
Vite `.env*` files (except inert `.env.example`/`.env.template` documentation)
because Vite can inline those values into the compiled JavaScript bundle.

Uninstall removes the installed program files and shortcuts only. It
intentionally leaves `%LOCALAPPDATA%\Tideguard` and Credential Manager entries
in place so uninstall/reinstall cannot silently destroy audit state or trading
credentials. Credential removal remains an explicit user action inside the
application's existing credential workflow.

## GitHub Release

Push an annotated semantic-version tag after the normal `main` checks pass:

```powershell
git tag -a v0.4.0 -m "墨衡 MOHENG v0.4.0"
git push origin v0.4.0
```

`.github/workflows/release.yml` validates the tag, rebuilds from the lockfile,
runs backend and packaging checks, compiles the Inno installer, and creates or
updates the matching GitHub Release. The release job alone receives
`contents: write`; the built-in short-lived GitHub token is never copied into
the application.

The current workflow produces unsigned binaries, so Windows SmartScreen may show
a reputation warning. A later production-signing step can use a reputable
Windows code-signing identity (or Azure Trusted Signing); never store a PFX or
password in the repository or PyInstaller data list.

## Silent install and uninstall

```powershell
.\Moheng-Setup-0.4.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
& "$env:LOCALAPPDATA\Programs\Tideguard\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```
