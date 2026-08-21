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
    $Output = Join-Path $DataRoot "replays\historical-replay-v6-$stamp.json"
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

$ScopedEnvironment = [ordered]@{
    TEMP = Join-Path $DataRoot "runtime-tmp"
    TMP = Join-Path $DataRoot "runtime-tmp"
    PYTHONPATH = Join-Path $ProjectRoot "backend\src"
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
    $SavedEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable(
        $entry.Key,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        $entry.Key,
        $entry.Value,
        "Process"
    )
}
$LocationPushed = $false
try {
    Push-Location $ProjectRoot
    $LocationPushed = $true
    & $ResearchPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The historical replay failed with exit code $LASTEXITCODE."
    }
} finally {
    try {
        if ($LocationPushed) {
            Pop-Location
        }
    } finally {
        foreach ($entry in $ScopedEnvironment.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable(
                $entry.Key,
                $SavedEnvironment[$entry.Key],
                "Process"
            )
        }
    }
}

Write-Host "V6 corrected execution-semantics historical replay evidence: $Output"
