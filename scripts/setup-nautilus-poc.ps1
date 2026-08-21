[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedVersion = "2.0.0rc3"
$ExpectedWheelName = "nautilus_trader-2.0.0rc3-cp312-cp312-win_amd64.whl"
$ExpectedWheelSha256 = "8a90b01ccf66d78946c565bca08b7758bc7f312caf1ded1c2c2c710013a7c092"
$ExpectedWheelSize = 61222443
$ExpectedPythonRequest = "cpython-3.12.13-windows-x86_64"
$ExpectedPythonVersion = "3.12.13"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ManifestPath = Join-Path $ProjectRoot "research\nautilus-lock.json"
$RuntimeRoot = Join-Path $ProjectRoot ".research-data\nautilus-poc"
$ArtifactsRoot = Join-Path $RuntimeRoot "artifacts"
$ManagedPythonRoot = Join-Path $RuntimeRoot "python"
$VenvPath = Join-Path $RuntimeRoot "venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$TempRoot = Join-Path $RuntimeRoot "tmp"
$UvCacheRoot = Join-Path $RuntimeRoot "uv-cache"
$PipCacheRoot = Join-Path $RuntimeRoot "pip-cache"
$StateRoot = Join-Path $RuntimeRoot "state"
$StatePath = Join-Path $StateRoot "setup-v3.json"

function Assert-WindowsX64 {
    if ($env:OS -ne "Windows_NT") {
        throw "The Nautilus PoC is pinned to Windows x64."
    }
    if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
        throw "Run the setup from a 64-bit PowerShell process on Windows x64."
    }
}

function Get-VerifiedManifest {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "research/nautilus-lock.json is missing."
    }

    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($manifest.schema_version -ne 1) {
        throw "Unsupported Nautilus lock schema."
    }
    if ($manifest.package.distribution -ne "nautilus-trader" -or
        $manifest.package.version -ne $ExpectedVersion) {
        throw "The Nautilus package lock does not match the approved version $ExpectedVersion."
    }
    if ($manifest.artifact.filename -ne $ExpectedWheelName -or
        $manifest.artifact.sha256.ToLowerInvariant() -ne $ExpectedWheelSha256 -or
        $manifest.artifact.size_bytes -ne $ExpectedWheelSize -or
        $manifest.artifact.python_tag -ne "cp312" -or
        $manifest.artifact.abi_tag -ne "cp312" -or
        $manifest.artifact.platform_tag -ne "win_amd64") {
        throw "The Nautilus wheel lock does not match the approved CPython 3.12 Windows x64 artifact."
    }
    if ($manifest.runtime.python_request -ne $ExpectedPythonRequest -or
        $manifest.runtime.python_version -ne $ExpectedPythonVersion -or
        $manifest.runtime.python_implementation -ne "CPython" -or
        $manifest.runtime.architecture -ne "x86_64" -or
        $manifest.runtime.pointer_bits -ne 64 -or
        $manifest.runtime.project_relative_root -ne ".research-data/nautilus-poc") {
        throw "The Nautilus runtime lock violates the isolated Windows x64 boundary."
    }
    if ($manifest.policy.purpose -ne "offline-research-only" -or
        $manifest.policy.setup_network_scope -ne "uv-managed-exact-python-version-and-pinned-nautilus-wheel-only" -or
        $manifest.policy.managed_python_archive_hash_locked -ne $false -or
        $manifest.policy.run_network_use_authorized -ne $false -or
        $manifest.policy.network_adapters_imported -ne $false -or
        $manifest.policy.os_network_isolation_enforced -ne $false -or
        $manifest.policy.credentials_allowed -ne $false -or
        $manifest.policy.private_api_allowed -ne $false -or
        $manifest.policy.orders_allowed -ne $false -or
        $manifest.policy.live_execution_allowed -ne $false) {
        throw "The Nautilus PoC policy must remain offline, credential-free, and order-free."
    }
    if (-not ([Uri]$manifest.artifact.url).IsAbsoluteUri -or
        ([Uri]$manifest.artifact.url).Scheme -ne "https" -or
        ([Uri]$manifest.artifact.url).Host -ne "github.com") {
        throw "The locked wheel must use the approved GitHub HTTPS release URL."
    }

    return $manifest
}

function Assert-WheelHash {
    param([Parameter(Mandatory = $true)][string]$Path)

    $size = (Get-Item -LiteralPath $Path).Length
    if ($size -ne $ExpectedWheelSize) {
        throw "Wheel size mismatch for '$Path'. Expected $ExpectedWheelSize bytes, observed $size."
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedWheelSha256) {
        throw "Wheel SHA256 mismatch for '$Path'. Expected $ExpectedWheelSha256, observed $actual."
    }
    return $actual
}

function Find-VerifiedManagedPython {
    $candidates = @(
        Get-ChildItem -LiteralPath $ManagedPythonRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($_.Name -match '^cpython-(3\.12\.13)-windows-x86_64-none$') {
                    [pscustomobject]@{
                        Version = [Version]$Matches[1]
                        Path = Join-Path $_.FullName "python.exe"
                    }
                }
            } |
            Sort-Object Version -Descending
    )
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Path -PathType Leaf)) {
            continue
        }
        & $candidate.Path -c `
            "import platform, struct, sys; assert sys.implementation.name == 'cpython'; assert sys.version_info[:3] == (3, 12, 13); assert struct.calcsize('P') * 8 == 64; assert platform.machine().lower() in {'amd64', 'x86_64'}"
        if ($LASTEXITCODE -eq 0) {
            return $candidate.Path
        }
    }
    return $null
}

function Write-CanonicalImmutableJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload
    )

    $hashPath = "$Path.sha256"
    if ((Test-Path -LiteralPath $Path) -or (Test-Path -LiteralPath $hashPath)) {
        throw "Refusing to overwrite immutable state target '$Path' or its SHA256 record."
    }
    $payloadJson = $Payload | ConvertTo-Json -Depth 12 -Compress
    $payloadBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payloadJson))
    $writer = @'
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

target = Path(sys.argv[1])
hash_target = Path(f"{target}.sha256")
lock_target = target.with_name(f".{target.name}.lock")
if os.name != "nt":
    raise RuntimeError("the immutable writer requires Windows fail-if-exists rename semantics")
payload = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
digest = hashlib.sha256(data).hexdigest()
hash_data = f"{digest}  {target.name}\n".encode("ascii")
nonce = f"{os.getpid()}.{secrets.token_hex(8)}"
temp_target = target.with_name(f".{target.name}.{nonce}.tmp")
temp_hash = hash_target.with_name(f".{hash_target.name}.{nonce}.tmp")
lock_fd = None
try:
    lock_fd = os.open(lock_target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
    os.fsync(lock_fd)
    if target.exists() or hash_target.exists():
        raise FileExistsError(f"immutable target already exists: {target}")
    for temp, content in ((temp_target, data), (temp_hash, hash_data)):
        with temp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    if target.exists() or hash_target.exists():
        raise FileExistsError(f"immutable target appeared during write: {target}")
    # On Windows os.rename is fail-if-exists. The exclusive per-target lock
    # serializes cooperating writers; an external conflicting writer still
    # causes a closed failure instead of an overwrite.
    os.rename(temp_target, target)
    os.rename(temp_hash, hash_target)
finally:
    for temp in (temp_target, temp_hash):
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    if lock_fd is not None:
        os.close(lock_fd)
        try:
            lock_target.unlink()
        except FileNotFoundError:
            pass
print(digest)
'@
    $digestLines = @(& $PythonPath -c $writer $Path $payloadBase64)
    $writerExitCode = $LASTEXITCODE
    if ($writerExitCode -ne 0) {
        throw "Unable to write canonical immutable JSON state '$Path'."
    }
    $digest = ($digestLines -join "`n").Trim()
    if ($digest -notmatch '^[0-9a-f]{64}$') {
        throw "The canonical state writer returned an invalid SHA256."
    }
    return $digest
}

function Get-VerifiedCanonicalJson {
    param([Parameter(Mandatory = $true)][string]$Path)

    $verifier = @'
import hashlib
import json
from pathlib import Path
import sys

target = Path(sys.argv[1])
hash_target = Path(f"{target}.sha256")
data = target.read_bytes()
if data.startswith(b"\xef\xbb\xbf"):
    raise ValueError("UTF-8 BOM is forbidden")
payload = json.loads(data.decode("utf-8"))
canonical = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
if data != canonical:
    raise ValueError("JSON is not canonical")
digest = hashlib.sha256(data).hexdigest()
expected_hash = f"{digest}  {target.name}\n".encode("ascii")
if hash_target.read_bytes() != expected_hash:
    raise ValueError("SHA256 record mismatch")
print(data.decode("utf-8"), end="")
'@
    $json = (& $PythonPath -c $verifier $Path)
    if ($LASTEXITCODE -ne 0) {
        throw "Canonical JSON or SHA256 verification failed for '$Path'."
    }
    return ($json -join "`n").Trim()
}

Assert-WindowsX64
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to create the isolated Nautilus PoC runtime."
}

$manifest = Get-VerifiedManifest
$ProjectRootFull = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
$RuntimeRootFull = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd("\")
if (-not $RuntimeRootFull.StartsWith("$ProjectRootFull\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "The Nautilus runtime must remain inside the project workspace."
}

New-Item -ItemType Directory -Path `
    $RuntimeRoot, $ArtifactsRoot, $ManagedPythonRoot, $TempRoot, `
    $UvCacheRoot, $PipCacheRoot, $StateRoot -Force | Out-Null

$ScopedEnvironment = [ordered]@{
    TEMP = $TempRoot
    TMP = $TempRoot
    UV_CACHE_DIR = $UvCacheRoot
    UV_PYTHON_INSTALL_DIR = $ManagedPythonRoot
    UV_PYTHON_DOWNLOADS = "automatic"
    PIP_CACHE_DIR = $PipCacheRoot
    PYTHONNOUSERSITE = "1"
    PYTHONDONTWRITEBYTECODE = "1"
    OKX_API_KEY = $null
    OKX_API_SECRET = $null
    OKX_API_PASSPHRASE = $null
    OKX_SECRET_KEY = $null
    OKX_PASSPHRASE = $null
}
$SavedEnvironment = @{}
foreach ($entry in $ScopedEnvironment.GetEnumerator()) {
    $SavedEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, "Process")
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
}

$WheelPath = Join-Path $ArtifactsRoot $ExpectedWheelName
$downloadNonce = [Guid]::NewGuid().ToString("N")
$PartialWheelPath = Join-Path $ArtifactsRoot ".$ExpectedWheelName.$PID.$downloadNonce.partial"
$PythonVerification = ""
$UvVersion = ""
$StateSha256 = ""
try {
    if (Test-Path -LiteralPath $WheelPath -PathType Leaf) {
        Assert-WheelHash -Path $WheelPath | Out-Null
    } else {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $manifest.artifact.url `
                -OutFile $PartialWheelPath -TimeoutSec 300
            Assert-WheelHash -Path $PartialWheelPath | Out-Null
            if (Test-Path -LiteralPath $WheelPath) {
                throw "Refusing to overwrite the pinned Nautilus wheel."
            }
            [IO.File]::Move($PartialWheelPath, $WheelPath)
        } finally {
            if (Test-Path -LiteralPath $PartialWheelPath) {
                Remove-Item -LiteralPath $PartialWheelPath -Force
            }
        }
    }

    $ManagedPythonPath = Find-VerifiedManagedPython
    if (-not $ManagedPythonPath) {
        & uv python install $ExpectedPythonRequest --no-bin --no-registry
        $UvPythonInstallExitCode = $LASTEXITCODE
        $ManagedPythonPath = Find-VerifiedManagedPython
        if (-not $ManagedPythonPath) {
            throw "Unable to install the isolated CPython 3.12.13 x64 runtime (uv exit $UvPythonInstallExitCode)."
        }
        if ($UvPythonInstallExitCode -ne 0) {
            Write-Warning "uv could not create its managed-Python convenience link on this filesystem; the extracted x64 CPython runtime was independently verified and will be used directly."
        }
    }

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        if (Test-Path -LiteralPath $VenvPath) {
            throw "The Nautilus virtual environment is incomplete. Remove only '$VenvPath' and rerun setup."
        }
        & $ManagedPythonPath -m venv --without-pip --copies $VenvPath
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create the isolated Nautilus virtual environment."
        }
    }

    $PythonVerification = (& $PythonPath -c `
        "import json, platform, struct, sys; assert sys.implementation.name == 'cpython'; assert sys.version_info[:3] == (3, 12, 13), sys.version; assert platform.python_version() == '$ExpectedPythonVersion'; assert struct.calcsize('P') * 8 == 64; assert platform.machine().lower() in {'amd64', 'x86_64'}, platform.machine(); print(json.dumps({'version': platform.python_version(), 'implementation': platform.python_implementation(), 'architecture': platform.machine(), 'pointerBits': struct.calcsize('P') * 8}, sort_keys=True))").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "The Nautilus runtime must use 64-bit CPython 3.12.13 on Windows."
    }

    & uv pip install --python $PythonPath --offline --no-deps --link-mode copy $WheelPath
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install the verified Nautilus wheel without network or dependencies."
    }

    & $PythonPath -c `
        "import importlib.metadata as m; actual=m.version('nautilus-trader'); assert actual == '$ExpectedVersion', actual; print(actual)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The installed Nautilus package failed exact-version verification."
    }

    $UvVersion = (& uv --version).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to record the uv version."
    }

    $state = [ordered]@{
        schema = "moheng.nautilus-setup-state.v3"
        status = "ready"
        preparedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        runtimeRoot = $RuntimeRootFull
        managedPython = $ManagedPythonPath
        python = ($PythonVerification | ConvertFrom-Json)
        uv = $UvVersion
        package = [ordered]@{
            distribution = "nautilus-trader"
            version = $ExpectedVersion
            wheel = $ExpectedWheelName
            wheelSizeBytes = $ExpectedWheelSize
            wheelSha256 = $ExpectedWheelSha256
        }
        policy = [ordered]@{
            purpose = "offline-research-only"
            credentialsLoaded = $false
            managedPythonArchiveHashLocked = $false
            networkAdaptersImported = $false
            networkUseAuthorized = $false
            osNetworkIsolationEnforced = $false
            privateApiCalls = 0
            orderCalls = 0
            liveExecutionAllowed = $false
        }
    }
    if ((Test-Path -LiteralPath $StatePath) -or (Test-Path -LiteralPath "$StatePath.sha256")) {
        if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf) -or
            -not (Test-Path -LiteralPath "$StatePath.sha256" -PathType Leaf)) {
            throw "The immutable setup state is incomplete; refusing to overwrite it."
        }
        $existingState = (Get-VerifiedCanonicalJson -Path $StatePath) | ConvertFrom-Json
        $currentPython = $PythonVerification | ConvertFrom-Json
        if ($existingState.schema -ne "moheng.nautilus-setup-state.v3" -or
            $existingState.status -ne "ready" -or
            $existingState.runtimeRoot -ne $RuntimeRootFull -or
            $existingState.managedPython -ne $ManagedPythonPath -or
            $existingState.python.version -ne $currentPython.version -or
            $existingState.python.implementation -ne $currentPython.implementation -or
            $existingState.python.architecture -ne $currentPython.architecture -or
            $existingState.python.pointerBits -ne $currentPython.pointerBits -or
            $existingState.uv -ne $UvVersion -or
            $existingState.package.version -ne $ExpectedVersion -or
            $existingState.package.wheelSha256 -ne $ExpectedWheelSha256 -or
            $existingState.policy.credentialsLoaded -ne $false -or
            $existingState.policy.managedPythonArchiveHashLocked -ne $false -or
            $existingState.policy.networkAdaptersImported -ne $false -or
            $existingState.policy.networkUseAuthorized -ne $false -or
            $existingState.policy.osNetworkIsolationEnforced -ne $false -or
            $existingState.policy.privateApiCalls -ne 0 -or
            $existingState.policy.orderCalls -ne 0 -or
            $existingState.policy.liveExecutionAllowed -ne $false) {
            throw "The existing immutable setup state does not match this offline PoC."
        }
        $StateSha256 = (Get-FileHash -LiteralPath $StatePath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else {
        $StateSha256 = Write-CanonicalImmutableJson -Path $StatePath -Payload $state
    }
} finally {
    foreach ($entry in $ScopedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $SavedEnvironment[$entry.Key], "Process")
    }
}

Write-Host "Nautilus offline PoC runtime ready: $PythonPath"
Write-Host "Verified wheel SHA256: $ExpectedWheelSha256"
Write-Host "Setup state SHA256: $StateSha256"
Write-Host "Run offline self-test: .\scripts\run-nautilus-poc.ps1"
