$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot '.venv'

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step 失败，退出码 $LASTEXITCODE" }
}

if (-not (Test-Path -LiteralPath $venvPath)) {
    python -m venv $venvPath
}

$pythonPath = Join-Path $venvPath 'Scripts\python.exe'
& $pythonPath -m pip install --upgrade pip
Assert-NativeSuccess 'pip 升级'
& $pythonPath -m pip install -e "$projectRoot\backend[dev]"
Assert-NativeSuccess '后端依赖安装'

Push-Location (Join-Path $projectRoot 'frontend')
try {
    corepack pnpm install
    Assert-NativeSuccess '前端依赖安装'
} finally {
    Pop-Location
}

Write-Host ''
Write-Host '安装完成。下一步请在本机终端设置 OKX 模拟盘凭证：' -ForegroundColor Green
Write-Host '.\.venv\Scripts\python.exe -m okx_demo_lab.cli credentials set --environment demo'
Write-Host 'Live 凭证必须单独设置，且不会复用 Demo：'
Write-Host '.\.venv\Scripts\python.exe -m okx_demo_lab.cli credentials set --environment live'
