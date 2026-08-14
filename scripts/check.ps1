$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '尚未安装。请先运行 .\scripts\setup.ps1'
}

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step 失败，退出码 $LASTEXITCODE"
    }
}

$pytestTemp = Join-Path $projectRoot '.pytest-temp'
& $pythonPath -m pytest (Join-Path $projectRoot 'backend\tests') --basetemp $pytestTemp -q
Assert-NativeSuccess '后端测试'

Push-Location (Join-Path $projectRoot 'frontend')
try {
    corepack pnpm typecheck
    Assert-NativeSuccess '前端类型检查'
    corepack pnpm build
    Assert-NativeSuccess '前端生产构建'
} finally {
    Pop-Location
}

$secretPattern = '(?i)(OKX_API_KEY|OKX_API_SECRET|OKX_PASSPHRASE)\s*=\s*["''][^"'']{8,}["'']'
$ripgrep = Get-Command rg -ErrorAction SilentlyContinue
if ($ripgrep) {
    $matches = & $ripgrep.Source -n --hidden -g '!.venv/**' -g '!node_modules/**' -g '!dist/**' -e $secretPattern $projectRoot
    if ($LASTEXITCODE -eq 0) {
        throw "检测到疑似硬编码凭证：`n$matches"
    }
    if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
} else {
    $textExtensions = @('.py', '.ps1', '.ts', '.tsx', '.js', '.jsx', '.json', '.toml', '.yaml', '.yml', '.md', '.html', '.css')
    $excludedRoots = @(
        (Join-Path $projectRoot '.git'),
        (Join-Path $projectRoot '.venv'),
        (Join-Path $projectRoot 'frontend\node_modules'),
        (Join-Path $projectRoot 'frontend\dist')
    )
    $scanFiles = Get-ChildItem -LiteralPath $projectRoot -Recurse -Force -File | Where-Object {
        $candidate = $_.FullName
        $isExcluded = $false
        foreach ($excludedRoot in $excludedRoots) {
            if ($candidate.StartsWith($excludedRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $isExcluded = $true
                break
            }
        }
        ($textExtensions -contains $_.Extension -or $_.Name -like '.env*') -and -not $isExcluded
    }
    $matches = $scanFiles | Select-String -Pattern $secretPattern
    if ($matches) {
        throw "检测到疑似硬编码凭证：`n$($matches -join "`n")"
    }
}

Write-Host '全部离线检查通过；没有调用私有 OKX API，也没有发送订单。' -ForegroundColor Green
