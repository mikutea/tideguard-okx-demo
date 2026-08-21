$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$env:TIDEGUARD_DATA_DIR = Join-Path $projectRoot '.local-data'
$env:MOHENG_RESEARCH_DATA_DIR = Join-Path $projectRoot '.research-data'
$env:TEMP = Join-Path $projectRoot '.research-data\runtime-tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '尚未安装。请先运行 .\scripts\setup.ps1'
}

$backend = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','okx_demo_lab.main:app','--host','127.0.0.1','--port','8791') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
try {
    Push-Location (Join-Path $projectRoot 'frontend')
    try {
        corepack pnpm dev
    } finally {
        Pop-Location
    }
} finally {
    if (-not $backend.HasExited) { Stop-Process -Id $backend.Id }
}
