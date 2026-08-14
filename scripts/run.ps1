$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$frontendPath = Join-Path $projectRoot 'frontend'
$env:TIDEGUARD_DATA_DIR = Join-Path $projectRoot '.local-data'

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step 失败，退出码 $LASTEXITCODE" }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '尚未安装。请先运行 .\scripts\setup.ps1'
}

Push-Location $frontendPath
try {
    corepack pnpm build
    Assert-NativeSuccess '前端生产构建'
} finally {
    Pop-Location
}

Write-Host '潮汐台将只监听 http://127.0.0.1:8791' -ForegroundColor Cyan
Write-Host '按 Ctrl+C 停止。停止或重启后，下单授权不会保留。'
$backend = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','okx_demo_lab.main:app','--host','127.0.0.1','--port','8791') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
try {
    $healthy = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($backend.HasExited) {
            throw "本地服务启动失败，退出码 $($backend.ExitCode)。请确认 8791 端口未被占用。"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8791/healthz' -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {
            # 冷启动期间继续等待；最终失败会给出明确错误。
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $healthy) {
        throw '本地服务在 10 秒内未通过健康检查。'
    }
    Start-Process 'http://127.0.0.1:8791'
    Wait-Process -Id $backend.Id
} finally {
    if ($backend -and -not $backend.HasExited) {
        Stop-Process -Id $backend.Id
    }
}
