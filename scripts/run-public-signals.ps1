[CmdletBinding()]
param(
    [ValidateSet("status", "once")]
    [string]$Command = "status",
    [ValidateRange(1, 250)]
    [int]$MaxRecords = 250,
    [ValidatePattern('^[1-9][0-9]{0,2}(min|h|d)$')]
    [string]$Timespan = "15min",
    [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $ProjectRoot ".research-data\runtime"
}
$ResearchPython = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
$Collector = Join-Path $ProjectRoot "research\collect_public_signals.py"
$Database = Join-Path $ProjectRoot ".research-data\public-signals.sqlite3"

$resolvedProject = [System.IO.Path]::GetFullPath($ProjectRoot)
$resolvedRuntime = [System.IO.Path]::GetFullPath($RuntimeRoot)
$relativeRuntime = [System.IO.Path]::GetRelativePath($resolvedProject, $resolvedRuntime)
$parentPrefix = ".." + [System.IO.Path]::DirectorySeparatorChar
if (
    [System.IO.Path]::IsPathRooted($relativeRuntime) -or
    $relativeRuntime -eq ".." -or
    $relativeRuntime.StartsWith($parentPrefix, [System.StringComparison]::Ordinal)
) {
    throw "Research runtime must stay inside the OKX project work directory."
}
if (-not (Test-Path -LiteralPath $ResearchPython -PathType Leaf)) {
    throw "Project-local research runtime is missing. Run scripts/setup-research.ps1 -RuntimeRoot '$RuntimeRoot' first."
}

$arguments = @(
    $Collector,
    $Command,
    "--database", $Database
)
if ($Command -eq "once") {
    $arguments += @("--max-records", [string]$MaxRecords, "--timespan", $Timespan)
}

Push-Location $ProjectRoot
try {
    & $ResearchPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Public signal collector failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
