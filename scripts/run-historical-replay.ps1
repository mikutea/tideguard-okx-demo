[CmdletBinding()]
param(
    [string]$CohortManifest = "",
    [string]$Family = "execution_hist_gradient_boosting",
    [Nullable[int]]$MaxEpisodes = $null,
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DataRoot = Join-Path $ProjectRoot ".research-data"
$ResearchPython = Join-Path $DataRoot "runtime\venv\Scripts\python.exe"
$Replay = Join-Path $ProjectRoot "research\historical_replay.py"
$env:TEMP = Join-Path $DataRoot "runtime-tmp"
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null

if (-not $CohortManifest) {
    $latest = Get-ChildItem -LiteralPath (Join-Path $DataRoot "cohorts") `
        -Filter "manifest.json" -File -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (-not $latest) { throw "No multi-asset cohort manifest is available." }
    $CohortManifest = $latest.FullName
}
if (-not $Output) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $Output = Join-Path $DataRoot "replays\historical-replay-v5-$stamp.json"
}
if (-not (Test-Path -LiteralPath $ResearchPython -PathType Leaf)) {
    throw "Research runtime is missing. Run scripts/setup-research.ps1 first."
}

$arguments = @(
    $Replay,
    "--cohort", $CohortManifest,
    "--output", $Output,
    "--family", $Family
)
if ($null -ne $MaxEpisodes) {
    $arguments += @("--max-episodes", [string]$MaxEpisodes)
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $ProjectRoot "backend\src"
Push-Location $ProjectRoot
try {
    & $ResearchPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The historical replay failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
    $env:PYTHONPATH = $PreviousPythonPath
}

Write-Host "V5 execution-readiness historical replay evidence: $Output"
