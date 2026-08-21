[CmdletBinding()]
param(
    [string]$Families = "hist_gradient_boosting,extra_trees,mlp,lightgbm,xgboost,catboost",
    [Nullable[int]]$MaxFolds = $null,
    [string]$Output = "",
    [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $ProjectRoot ".research-data\runtime"
}
$ResearchPython = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
$Benchmark = Join-Path $ProjectRoot "research\model_benchmark.py"
$DataPath = Join-Path $ProjectRoot ".research-data\btc-market-data.sqlite3"
if (-not $Output) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $Output = Join-Path $ProjectRoot ".research-data\benchmarks\benchmark-$stamp.json"
}

if (-not (Test-Path -LiteralPath $ResearchPython -PathType Leaf)) {
    throw "Research runtime is missing. Run scripts/setup-research.ps1 first."
}
if (-not (Test-Path -LiteralPath $DataPath -PathType Leaf)) {
    throw "The shared public market-data.sqlite3 warehouse is missing."
}

$arguments = @(
    $Benchmark,
    "--data-path", $DataPath,
    "--output", $Output,
    "--families", $Families
)
if ($null -ne $MaxFolds) {
    $arguments += @("--max-folds", [string]$MaxFolds)
}

Push-Location $ProjectRoot
try {
    & $ResearchPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The research benchmark failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

Write-Host "Benchmark evidence: $Output"
