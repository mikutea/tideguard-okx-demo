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

$pytestRoot = Join-Path $projectRoot '.pytest-work'
$pytestTemp = Join-Path $pytestRoot ('tideguard-pytest-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $pytestTemp -Force | Out-Null
try {
    & $pythonPath -m pytest (Join-Path $projectRoot 'backend\tests') --basetemp $pytestTemp -q
    Assert-NativeSuccess '后端测试'
    & $pythonPath -m pytest (Join-Path $projectRoot 'research\tests') --basetemp $pytestTemp -q
    Assert-NativeSuccess '隔离研究协议测试'
} finally {
    if (Test-Path -LiteralPath $pytestTemp) {
        Remove-Item -LiteralPath $pytestTemp -Recurse -Force
    }
}
& $pythonPath -m compileall -q (Join-Path $projectRoot 'backend\src') (Join-Path $projectRoot 'desktop') (Join-Path $projectRoot 'research')
Assert-NativeSuccess 'Python 编译检查'

foreach ($scriptName in @(
    'setup-nautilus-poc.ps1',
    'run-nautilus-poc.ps1',
    'run-historical-replay.ps1'
)) {
    $tokens = $null
    $parseErrors = $null
    $scriptPath = Join-Path $projectRoot "scripts\$scriptName"
    [System.Management.Automation.Language.Parser]::ParseFile(
        $scriptPath,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    if ($parseErrors.Count -ne 0) {
        throw "PowerShell 脚本语法检查失败：$scriptName"
    }
}
& $pythonPath -m unittest discover -s (Join-Path $projectRoot 'desktop\tests') -v
Assert-NativeSuccess '桌面宿主测试'
& $pythonPath -m unittest discover -s (Join-Path $projectRoot 'packaging\tests') -v
Assert-NativeSuccess '打包契约测试'

Push-Location (Join-Path $projectRoot 'frontend')
try {
    corepack pnpm typecheck
    Assert-NativeSuccess '前端类型检查'
    corepack pnpm test
    Assert-NativeSuccess '前端环境安全契约测试'
    corepack pnpm build
    Assert-NativeSuccess '前端生产构建'
} finally {
    Pop-Location
}

$secretPattern = '(?i)(?:[\x22\x27]?(?:OKX_API_KEY|OKX_API_SECRET|OKX_API_PASSPHRASE|OKX_SECRET_KEY|OKX_PASSPHRASE)[\x22\x27]?|(?:api[_-]?(?:key|secret)|passphrase))\s*[:=]\s*[\x22\x27][^\x22\x27]{8,}[\x22\x27]'
$privateKeyPattern = '-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
$ripgrep = Get-Command rg -ErrorAction SilentlyContinue
if ($ripgrep) {
    $matches = & $ripgrep.Source -n --hidden -g '!.git/**' -g '!.venv/**' -g '!node_modules/**' -g '!dist/**' -g '!release/**' -g '!scripts/check.ps1' -g '!packaging/build-release.ps1' -e $secretPattern -e $privateKeyPattern $projectRoot
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
    $scanFiles = $scanFiles | Where-Object {
        $_.FullName -notin @(
            (Join-Path $projectRoot 'scripts\check.ps1'),
            (Join-Path $projectRoot 'packaging\build-release.ps1')
        )
    }
    $matches = $scanFiles | Select-String -Pattern $secretPattern, $privateKeyPattern
    if ($matches) {
        throw "检测到疑似硬编码凭证：`n$($matches -join "`n")"
    }
}

$sensitiveNames = @('credentials.json', 'credentials.toml')
$sensitiveExtensions = @('.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db')
$candidatePaths = & git -C $projectRoot ls-files --cached --others --exclude-standard
if ($LASTEXITCODE -ne 0) { throw '无法读取 Git 文件清单' }
$sensitivePaths = $candidatePaths | Where-Object {
    $leaf = [IO.Path]::GetFileName($_)
    $extension = [IO.Path]::GetExtension($_).ToLowerInvariant()
    $sensitiveNames -contains $leaf.ToLowerInvariant() -or $sensitiveExtensions -contains $extension
}
if ($sensitivePaths) {
    throw "检测到不应进入 Git 的凭证或本地状态文件：`n$($sensitivePaths -join "`n")"
}

Write-Host '全部离线检查通过；测试已隔离真实凭证和环境，没有调用私有 OKX API，也没有发送订单。' -ForegroundColor Green
