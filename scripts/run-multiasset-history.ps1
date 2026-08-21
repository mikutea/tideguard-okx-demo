[CmdletBinding()]
param(
    [ValidateRange(1, 20000)]
    [int]$PageBudget = 600,
    [string]$Universe = "",
    [string]$Database = "",
    [string]$Progress = "",
    [switch]$StatusOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DataRoot = Join-Path $ProjectRoot ".research-data"
$Coordinator = Join-Path $ProjectRoot "research\backfill_universe.py"
if (-not $Universe) { $Universe = Join-Path $DataRoot "universes\universe-latest.json" }
if (-not $Database) { $Database = Join-Path $DataRoot "multi-asset-market.sqlite3" }
if (-not $Progress) { $Progress = Join-Path $DataRoot "multi-asset-history-progress.json" }
$Lock = Join-Path $DataRoot "multi-asset-history.lock"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project-local Python is missing. Run .\scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $Universe -PathType Leaf)) {
    throw "Frozen universe is missing. Create it inside the project with: .\.venv\Scripts\python.exe .\research\discover_universe.py"
}
New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null

$Arguments = @(
    $Coordinator,
    "--universe", $Universe,
    "--database", $Database,
    "--progress", $Progress,
    "--lock", $Lock,
    "--page-budget", [string]$PageBudget
)
if ($StatusOnly) { $Arguments += "--status-only" }

$PreviousPythonPath = $env:PYTHONPATH
$BackendSource = Join-Path $ProjectRoot "backend\src"
$env:PYTHONPATH = if ($PreviousPythonPath) {
    "$BackendSource$([IO.Path]::PathSeparator)$PreviousPythonPath"
} else {
    $BackendSource
}
Push-Location $ProjectRoot
try {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Multi-asset public-history coordinator failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
    $env:PYTHONPATH = $PreviousPythonPath
}

Write-Host "All runtime files remain under $DataRoot" -ForegroundColor Green
