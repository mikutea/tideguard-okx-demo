$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '尚未安装。请先运行 .\scripts\setup.ps1'
}

$dataRoot = Join-Path $projectRoot '.research-data'
& $pythonPath (Join-Path $projectRoot 'research\build_multiasset_cohort.py') `
    --universe (Join-Path $dataRoot 'universes\universe-latest.json') `
    --database (Join-Path $dataRoot 'multi-asset-market.sqlite3') `
    --output-root (Join-Path $dataRoot 'cohorts')
if ($LASTEXITCODE -ne 0) {
    throw "多资产 cohort 构建失败，退出码 $LASTEXITCODE"
}
