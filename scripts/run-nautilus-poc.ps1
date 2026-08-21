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
$PythonPath = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
$WheelPath = Join-Path (Join-Path $RuntimeRoot "artifacts") $ExpectedWheelName
$StatePath = Join-Path $RuntimeRoot "state\setup-v3.json"
$StateHashPath = "$StatePath.sha256"
$TempRoot = Join-Path $RuntimeRoot "tmp"
$ResultsRoot = Join-Path $RuntimeRoot "results"

function Write-CanonicalImmutableJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload
    )

    $hashPath = "$Path.sha256"
    if ((Test-Path -LiteralPath $Path) -or (Test-Path -LiteralPath $hashPath)) {
        throw "Refusing to overwrite immutable evidence target '$Path' or its SHA256 record."
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
        throw "Unable to write canonical immutable JSON evidence '$Path'."
    }
    $digest = ($digestLines -join "`n").Trim()
    if ($digest -notmatch '^[0-9a-f]{64}$') {
        throw "The canonical evidence writer returned an invalid SHA256."
    }
    return $digest
}

if ($env:OS -ne "Windows_NT" -or
    -not [Environment]::Is64BitOperatingSystem -or
    -not [Environment]::Is64BitProcess) {
    throw "The Nautilus PoC is pinned to a 64-bit Windows process."
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $WheelPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $StatePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $StateHashPath -PathType Leaf)) {
    throw "The isolated Nautilus runtime is incomplete. Run scripts/setup-nautilus-poc.ps1 first."
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or
    $manifest.package.distribution -ne "nautilus-trader" -or
    $manifest.package.version -ne $ExpectedVersion -or
    $manifest.artifact.filename -ne $ExpectedWheelName -or
    $manifest.artifact.sha256.ToLowerInvariant() -ne $ExpectedWheelSha256 -or
    $manifest.artifact.size_bytes -ne $ExpectedWheelSize -or
    $manifest.artifact.python_tag -ne "cp312" -or
    $manifest.artifact.abi_tag -ne "cp312" -or
    $manifest.artifact.platform_tag -ne "win_amd64" -or
    $manifest.runtime.python_request -ne $ExpectedPythonRequest -or
    $manifest.runtime.python_version -ne $ExpectedPythonVersion -or
    $manifest.runtime.python_implementation -ne "CPython" -or
    $manifest.runtime.architecture -ne "x86_64" -or
    $manifest.runtime.pointer_bits -ne 64 -or
    $manifest.runtime.project_relative_root -ne ".research-data/nautilus-poc" -or
    $manifest.policy.purpose -ne "offline-research-only" -or
    $manifest.policy.setup_network_scope -ne "uv-managed-exact-python-version-and-pinned-nautilus-wheel-only" -or
    $manifest.policy.managed_python_archive_hash_locked -ne $false -or
    $manifest.policy.run_network_use_authorized -ne $false -or
    $manifest.policy.network_adapters_imported -ne $false -or
    $manifest.policy.os_network_isolation_enforced -ne $false -or
    $manifest.policy.credentials_allowed -ne $false -or
    $manifest.policy.private_api_allowed -ne $false -or
    $manifest.policy.orders_allowed -ne $false -or
    $manifest.policy.live_execution_allowed -ne $false) {
    throw "The Nautilus lock no longer matches the approved offline PoC boundary."
}

$ActualWheelSize = (Get-Item -LiteralPath $WheelPath).Length
if ($ActualWheelSize -ne $ExpectedWheelSize) {
    throw "Wheel size mismatch. Expected $ExpectedWheelSize bytes, observed $ActualWheelSize."
}
$ActualWheelSha256 = (Get-FileHash -LiteralPath $WheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualWheelSha256 -ne $ExpectedWheelSha256) {
    throw "Wheel SHA256 mismatch. Expected $ExpectedWheelSha256, observed $ActualWheelSha256."
}

New-Item -ItemType Directory -Path $TempRoot, $ResultsRoot -Force | Out-Null
$ScopedEnvironment = [ordered]@{
    TEMP = $TempRoot
    TMP = $TempRoot
    PYTHONPATH = $ProjectRoot
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

$RuntimeIntegrityCheck = @'
import json
import sys

from research.nautilus_sidecar.nautilus_adapter import (
    verify_isolated_runtime_integrity,
)

result = verify_isolated_runtime_integrity(
    state_path=sys.argv[1],
    state_hash_path=sys.argv[2],
    wheel_path=sys.argv[3],
    runtime_root=sys.argv[4],
    python_executable=sys.executable,
    expected_wheel_name=sys.argv[5],
    expected_wheel_sha256=sys.argv[6],
    expected_wheel_size=int(sys.argv[7]),
)
print(json.dumps(result, sort_keys=True))
'@

$SelfTest = @'
import importlib.metadata as metadata
import json
import os
import platform
import struct
import sys
from datetime import datetime, timezone

assert sys.implementation.name == "cpython"
assert sys.version_info[:3] == (3, 12, 13), sys.version
assert platform.python_version() == "3.12.13"
assert struct.calcsize("P") * 8 == 64
assert platform.machine().lower() in {"amd64", "x86_64"}, platform.machine()
assert metadata.version("nautilus-trader") == "2.0.0rc3"
assert not any(os.environ.get(name) for name in (
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_API_PASSPHRASE",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
))

assert not any(
    name == "nautilus_trader" or name.startswith("nautilus_trader.")
    for name in sys.modules
)
from research.nautilus_sidecar.nautilus_adapter import dependency_status

metadata_only_status = dependency_status()
assert metadata_only_status["packageImported"] is False, metadata_only_status
assert not any(
    name == "nautilus_trader" or name.startswith("nautilus_trader.")
    for name in sys.modules
)

import nautilus_trader
from nautilus_trader.model import InstrumentId, Price, Quantity

public_modules_loaded = sorted(
    name
    for name in sys.modules
    if name == "nautilus_trader"
    or (name.startswith("nautilus_trader.") and not name.startswith("nautilus_trader._"))
)
expected_public_modules = ["nautilus_trader", "nautilus_trader.model"]
forbidden_runtime_facades = [
    "nautilus_trader.adapters",
    "nautilus_trader.adapters.okx",
    "nautilus_trader.live",
    "nautilus_trader.system",
]
forbidden_runtime_facades_loaded = [
    name for name in forbidden_runtime_facades if name in sys.modules
]
assert public_modules_loaded == expected_public_modules, public_modules_loaded
assert not forbidden_runtime_facades_loaded, forbidden_runtime_facades_loaded

instrument_id = InstrumentId.from_str("BTC-USDT.OKX")
price = Price.from_str("60000.0")
quantity = Quantity.from_str("0.001")

assert str(instrument_id) == "BTC-USDT.OKX"
assert price.as_double() == 60000.0
assert quantity.as_double() == 0.001

print(json.dumps({
    "schema": "moheng.nautilus-offline-model-self-test.v1",
    "status": "pass",
    "observedAtUtc": datetime.now(timezone.utc).isoformat(),
    "package": {
        "distribution": "nautilus-trader",
        "version": metadata.version("nautilus-trader"),
    },
    "runtime": {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "architecture": platform.machine(),
        "pointerBits": struct.calcsize("P") * 8,
    },
    "localObjectTest": {
        "instrumentId": str(instrument_id),
        "price": str(price),
        "quantity": str(quantity),
    },
    "moduleIsolation": {
        "packageImportedBeforeMaterialization": metadata_only_status["packageImported"],
        "publicModulesLoaded": public_modules_loaded,
        "forbiddenRuntimeFacades": forbidden_runtime_facades,
        "forbiddenRuntimeFacadesLoaded": forbidden_runtime_facades_loaded,
        "nativeExtensionLoaded": "nautilus_trader._libnautilus" in sys.modules,
    },
    "protocol": {
        "name": "offline-model-value-types-only",
        "networkAdaptersImported": False,
        "networkUseAuthorized": False,
        "osNetworkIsolationEnforced": False,
        "basis": "audited path imports model/value types only; no adapter, OKX, live, or system public facade is imported; packet-level isolation is not enforced",
    },
    "policy": {
        "credentialsLoaded": False,
        "managedPythonArchiveHashLocked": False,
        "privateApiCalls": 0,
        "orderCalls": 0,
        "liveExecutionAllowed": False,
    },
}, sort_keys=True))
'@

$SidecarExercise = @'
import json

from research.nautilus_sidecar.protocol import (
    BAR_CATALOG_OPERATION,
    BAR_SPECIFICATION,
    INSTRUMENT_ID,
    REQUEST_SCHEMA_VERSION,
    SAFETY_CONTRACT,
    canonical_json,
    seal_request,
)
from research.nautilus_sidecar.service import handle_request

start_ns = 1_800_000_000_000_000_000
request = seal_request({
    "operation": BAR_CATALOG_OPERATION,
    "payload": {
        "barSpecification": BAR_SPECIFICATION,
        "bars": [{
            "close": "60100.5",
            "confirmed": True,
            "high": "60200",
            "low": "59900",
            "open": "60000",
            "tsOpenNs": start_ns,
            "volume": "12.50",
        }],
        "instrumentId": INSTRUMENT_ID,
        "source": {
            "contentSha256": "a" * 64,
            "kind": "okx_public_frozen_snapshot",
        },
    },
    "safety": dict(SAFETY_CONTRACT),
    "schemaVersion": REQUEST_SCHEMA_VERSION,
})
response = handle_request(request)
materialization = response["result"]["materialization"]
semantic = response["semanticBoundary"]
assert response["status"] == "accepted"
assert response["decision"] == "research_only"
assert response["result"]["simulationExecuted"] is False
assert response["result"]["tradesProduced"] == 0
assert materialization["status"] == "ready", materialization
assert materialization["materializedBars"] == 1
assert materialization["packageImportAttempted"] is True
assert materialization["packageImported"] is True
assert semantic["nativeNautilusExecutionParityValidated"] is False
assert semantic["reasonCode"] == "NATIVE_BAR_FILL_PARITY_NOT_VALIDATED"
assert response["safety"]["networkAdaptersImported"] is False
assert response["safety"]["networkUseAuthorized"] is False
assert response["safety"]["osNetworkIsolationEnforced"] is False
print(canonical_json({
    "barDataSha256": response["result"]["catalog"]["barDataSha256"],
    "materializedBars": materialization["materializedBars"],
    "nautilusVersion": materialization["version"],
    "nativeNautilusExecutionParityValidated": semantic[
        "nativeNautilusExecutionParityValidated"
    ],
    "networkAdaptersImported": response["safety"]["networkAdaptersImported"],
    "networkUseAuthorized": response["safety"]["networkUseAuthorized"],
    "orderCalls": 0,
    "osNetworkIsolationEnforced": response["safety"]["osNetworkIsolationEnforced"],
    "packageImportAttempted": materialization["packageImportAttempted"],
    "packageImported": materialization["packageImported"],
    "privateApiCalls": 0,
    "reasonCode": semantic["reasonCode"],
    "simulationExecuted": response["result"]["simulationExecuted"],
    "status": response["status"],
    "tradesProduced": response["result"]["tradesProduced"],
}))
'@

$RawIntegrityResult = ""
$RawResult = ""
$RawSidecarResult = ""
try {
    Push-Location $ProjectRoot
    try {
        $RawIntegrityLines = @(
            & $PythonPath -c $RuntimeIntegrityCheck `
                $StatePath `
                $StateHashPath `
                $WheelPath `
                $RuntimeRoot `
                $ExpectedWheelName `
                $ExpectedWheelSha256 `
                ([string]$ExpectedWheelSize)
        )
        if ($LASTEXITCODE -ne 0) {
            throw "The Nautilus runtime integrity verification failed with exit code $LASTEXITCODE."
        }
        $RawIntegrityResult = ($RawIntegrityLines -join "`n").Trim()
        if (-not $RawIntegrityResult) {
            throw "The Nautilus runtime integrity verification returned no evidence."
        }
        $RawLines = @(& $PythonPath -c $SelfTest)
        if ($LASTEXITCODE -ne 0) {
            throw "The Nautilus offline self-test failed with exit code $LASTEXITCODE."
        }
        $RawResult = ($RawLines -join "`n").Trim()
        if (-not $RawResult) {
            throw "The Nautilus offline self-test returned no evidence."
        }
        $RawSidecarLines = @(& $PythonPath -c $SidecarExercise)
        if ($LASTEXITCODE -ne 0) {
            throw "The Nautilus sidecar exercise failed with exit code $LASTEXITCODE."
        }
        $RawSidecarResult = ($RawSidecarLines -join "`n").Trim()
        if (-not $RawSidecarResult) {
            throw "The Nautilus sidecar exercise returned no evidence."
        }
    } finally {
        Pop-Location
    }
} finally {
    foreach ($entry in $ScopedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $SavedEnvironment[$entry.Key], "Process")
    }
}

$IntegrityResult = $RawIntegrityResult | ConvertFrom-Json
$Result = $RawResult | ConvertFrom-Json
$SidecarResult = $RawSidecarResult | ConvertFrom-Json
$Result.observedAtUtc = $Result.observedAtUtc.ToUniversalTime().ToString("o")
if ($IntegrityResult.status -ne "verified" -or
    $IntegrityResult.schema -ne "moheng.nautilus-setup-state.v3" -or
    $IntegrityResult.installedRecordVerified -ne $true -or
    $IntegrityResult.installedFileCount -lt 1 -or
    $IntegrityResult.wheelSha256 -ne $ExpectedWheelSha256 -or
    $IntegrityResult.setupStateSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "The Nautilus runtime integrity chain did not verify."
}
if ($Result.status -ne "pass" -or
    $Result.schema -ne "moheng.nautilus-offline-model-self-test.v1" -or
    $Result.package.version -ne $ExpectedVersion -or
    $Result.moduleIsolation.packageImportedBeforeMaterialization -ne $false -or
    $Result.protocol.networkAdaptersImported -ne $false -or
    $Result.protocol.networkUseAuthorized -ne $false -or
    $Result.protocol.osNetworkIsolationEnforced -ne $false -or
    $Result.moduleIsolation.forbiddenRuntimeFacadesLoaded.Count -ne 0 -or
    $Result.policy.credentialsLoaded -ne $false -or
    $Result.policy.managedPythonArchiveHashLocked -ne $false -or
    $Result.policy.privateApiCalls -ne 0 -or
    $Result.policy.orderCalls -ne 0 -or
    $Result.policy.liveExecutionAllowed -ne $false) {
    throw "The Nautilus offline self-test did not satisfy the fail-closed policy."
}
if ($SidecarResult.status -ne "accepted" -or
    $SidecarResult.nautilusVersion -ne $ExpectedVersion -or
    $SidecarResult.materializedBars -ne 1 -or
    $SidecarResult.networkAdaptersImported -ne $false -or
    $SidecarResult.networkUseAuthorized -ne $false -or
    $SidecarResult.osNetworkIsolationEnforced -ne $false -or
    $SidecarResult.packageImportAttempted -ne $true -or
    $SidecarResult.packageImported -ne $true -or
    $SidecarResult.nativeNautilusExecutionParityValidated -ne $false -or
    $SidecarResult.reasonCode -ne "NATIVE_BAR_FILL_PARITY_NOT_VALIDATED" -or
    $SidecarResult.simulationExecuted -ne $false -or
    $SidecarResult.tradesProduced -ne 0 -or
    $SidecarResult.privateApiCalls -ne 0 -or
    $SidecarResult.orderCalls -ne 0) {
    throw "The Nautilus sidecar exercise did not satisfy the offline PoC policy."
}

$Evidence = [ordered]@{
    schema = "moheng.nautilus-offline-poc.v1"
    status = "pass"
    integrity = $IntegrityResult
    wheel = [ordered]@{
        filename = $ExpectedWheelName
        sizeBytes = $ActualWheelSize
        sha256 = $ActualWheelSha256
    }
    sidecar = $SidecarResult
    selfTest = $Result
}
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ")
$nonce = [Guid]::NewGuid().ToString("N").Substring(0, 12)
$EvidencePath = Join-Path $ResultsRoot "offline-self-test-$stamp-$nonce.json"
$EvidenceSha256 = Write-CanonicalImmutableJson -Path $EvidencePath -Payload $Evidence

Write-Host "Nautilus offline self-test passed."
Write-Host "Evidence: $EvidencePath"
Write-Host "Evidence SHA256: $EvidenceSha256"
