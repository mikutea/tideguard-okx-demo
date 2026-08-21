[CmdletBinding()]
param(
    [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $ProjectRoot ".research-data\runtime"
}
$LockFile = Join-Path $ProjectRoot "research\requirements-windows.lock"
$BackendPath = Join-Path $ProjectRoot "backend"
$VenvPath = Join-Path $RuntimeRoot "venv"
$ResearchPython = Join-Path $VenvPath "Scripts\python.exe"
$RuntimeTemp = Join-Path $RuntimeRoot "tmp"
$UvCache = Join-Path $RuntimeRoot "uv-cache"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to create the isolated research runtime."
}
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    throw "research/requirements-windows.lock is missing."
}

New-Item -ItemType Directory -Path $RuntimeRoot, $RuntimeTemp, $UvCache -Force | Out-Null
$env:TEMP = $RuntimeTemp
$env:TMP = $RuntimeTemp
$env:UV_CACHE_DIR = $UvCache
if (-not (Test-Path -LiteralPath $ResearchPython -PathType Leaf)) {
    & uv venv --python 3.11 $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the Python 3.11 research runtime."
    }
}

& $ResearchPython -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version"
if ($LASTEXITCODE -ne 0) {
    throw "The research runtime must use Python 3.11."
}

& uv pip sync --python $ResearchPython $LockFile
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install the hash-locked research dependencies."
}
& uv pip install --python $ResearchPython --no-deps --editable $BackendPath
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install the local MOHENG backend into the research runtime."
}

& $ResearchPython -c "import importlib.metadata as m; expected={'catboost':'1.2.10','cryptofeed':'2.4.1','lightgbm':'4.7.0','quantstats':'0.0.81','scikit-learn':'1.9.0','vaderSentiment':'3.3.2','xgboost':'3.2.0'}; actual={k:m.version(k) for k in expected}; assert actual == expected, actual; print(actual)"
if ($LASTEXITCODE -ne 0) {
    throw "Research dependency verification failed."
}
& $ResearchPython (Join-Path $ProjectRoot "research\vader_adapter.py") --self-test | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "VADER research adapter self-test failed."
}

Write-Host "Research runtime ready: $ResearchPython"
