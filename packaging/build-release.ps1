[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$PythonExe = "python",
    [switch]$SkipDependencyInstall,
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$FrontendDir = Join-Path $ProjectRoot "frontend"
$BackendDir = Join-Path $ProjectRoot "backend"
$RequirementsLock = Join-Path $ProjectRoot "packaging\requirements-windows.lock"
$BuildToolsLock = Join-Path $ProjectRoot "packaging\build-tools-windows.lock"
$StagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("TideguardPackagingBuild-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N"))
$OutputRoot = Join-Path $StagingRoot "dist"
$AppOutput = Join-Path $OutputRoot "Tideguard"
$WorkRoot = Join-Path $StagingRoot "work"
$ReleaseDir = Join-Path $ProjectRoot "release"
$TestTemp = Join-Path ([IO.Path]::GetTempPath()) ("TideguardPackagingTests-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N"))

function Invoke-Native {
    param(
        [Parameter(Mandatory)] [string]$Command,
        [Parameter(ValueFromRemainingArguments)] [string[]]$Arguments
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

function Remove-LocalTempDirectory {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$ExpectedPrefix
    )
    $full = [IO.Path]::GetFullPath($Path)
    $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($full).StartsWith($ExpectedPrefix, [StringComparison]::Ordinal)) {
        throw "Refusing to remove an unexpected temporary path: $full"
    }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
    }
}

function Remove-TestDirectory {
    param([Parameter(Mandatory)] [string]$Path)
    Remove-LocalTempDirectory $Path "TideguardPackagingTests-"
}

function Find-InnoCompiler {
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Assert-NoCredentialArtifacts {
    param([Parameter(Mandatory)] [string]$Root)
    $forbiddenNames = @(
        ".env", "state.sqlite3", "credentials.json", "credentials.toml",
        "id_rsa", "id_ed25519"
    )
    $forbiddenExtensions = @(".key", ".p12", ".pfx", ".sqlite", ".sqlite3")
    $badFiles = Get-ChildItem -LiteralPath $Root -Recurse -File | Where-Object {
        $_.Name -in $forbiddenNames -or $_.Extension.ToLowerInvariant() -in $forbiddenExtensions
    }
    if ($badFiles) {
        $rootPrefix = $Root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        $relative = $badFiles | ForEach-Object { $_.FullName.Substring($rootPrefix.Length) }
        throw "Credential or local-state files found in artifact: $($relative -join ', ')"
    }

    $privateKeyMarkers = @(
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----"
    )
    foreach ($pemFile in Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "*.pem") {
        if ($pemFile.Length -gt 16MB) { throw "Oversized PEM file found in artifact" }
        $pemContent = [IO.File]::ReadAllText($pemFile.FullName)
        foreach ($marker in $privateKeyMarkers) {
            if ($pemContent.IndexOf($marker, [StringComparison]::Ordinal) -ge 0) {
                throw "Private key material found in packaged PEM content"
            }
        }
    }

    $sensitiveVariables = @("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE")
    $textExtensions = @(".css", ".html", ".ini", ".js", ".json", ".py", ".toml", ".txt", ".xml", ".yaml", ".yml")
    $textFiles = Get-ChildItem -LiteralPath $Root -Recurse -File | Where-Object {
        $_.Extension.ToLowerInvariant() -in $textExtensions -and $_.Length -le 16MB
    }
    foreach ($variableName in $sensitiveVariables) {
        $value = [Environment]::GetEnvironmentVariable($variableName)
        if ([string]::IsNullOrEmpty($value) -or $value.Length -lt 8) { continue }
        foreach ($file in $textFiles) {
            $content = [IO.File]::ReadAllText($file.FullName)
            if ($content.IndexOf($value, [StringComparison]::Ordinal) -ge 0) {
                throw "A value from $variableName was found in packaged text content"
            }
        }
    }
}

function Assert-SafeFrontendBuildEnvironment {
    $viteEnvFiles = Get-ChildItem -LiteralPath $FrontendDir -Force -File | Where-Object {
        $_.Name -like ".env*" -and $_.Name -notin @(".env.example", ".env.template")
    }
    if ($viteEnvFiles) {
        $names = $viteEnvFiles | ForEach-Object Name
        throw "Refusing to run a release build with Vite environment files present: $($names -join ', ')"
    }

    $viteVariables = Get-ChildItem Env: | Where-Object { $_.Name -like "VITE_*" }
    if ($viteVariables) {
        $names = $viteVariables | ForEach-Object Name
        throw "Refusing to run a release build with Vite environment variables present: $($names -join ', ')"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir "pnpm-lock.yaml"))) {
    throw "frontend/pnpm-lock.yaml is required for a reproducible build"
}
if (-not (Test-Path -LiteralPath $RequirementsLock)) {
    throw "packaging/requirements-windows.lock is required for a reproducible build"
}
if (-not (Test-Path -LiteralPath $BuildToolsLock)) {
    throw "packaging/build-tools-windows.lock is required for a reproducible build"
}

Invoke-Native $PythonExe "--version"
$pythonBuildOutput = & $PythonExe -c "import struct,sys; print('%d.%d|%d' % (sys.version_info.major, sys.version_info.minor, struct.calcsize('P') * 8))"
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the Python interpreter" }
$pythonBuild = ($pythonBuildOutput | Select-Object -Last 1).Trim()
if ($pythonBuild -ne "3.11|64") {
    throw "Release builds require 64-bit Python 3.11; found $pythonBuild"
}
$versionOutput = & $PythonExe -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(r'$BackendDir\pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "Unable to read the project version" }
$projectVersion = ($versionOutput | Select-Object -Last 1).Trim()
if ([string]::IsNullOrWhiteSpace($Version)) { $Version = $projectVersion }
if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
    throw "Version must be a semantic version without a leading v: $Version"
}
if ($Version -cne $projectVersion) {
    throw "Requested version $Version does not match backend/pyproject.toml version $projectVersion"
}

if (-not $SkipDependencyInstall) {
    Invoke-Native $PythonExe "-m" "pip" "install" "--require-hashes" "-r" $BuildToolsLock
    Invoke-Native $PythonExe "-m" "pip" "install" "--require-hashes" "--no-build-isolation" "-r" $RequirementsLock
    Invoke-Native $PythonExe "-m" "pip" "install" "--no-deps" "--no-build-isolation" $BackendDir
}

Assert-SafeFrontendBuildEnvironment
Push-Location $FrontendDir
try {
    Invoke-Native "corepack" "pnpm" "install" "--frozen-lockfile"
    Invoke-Native "corepack" "pnpm" "build"
}
finally {
    Pop-Location
}

if (-not $SkipTests) {
    try {
        Invoke-Native $PythonExe "-m" "pytest" (Join-Path $BackendDir "tests") "--basetemp" $TestTemp
        Invoke-Native $PythonExe "-m" "unittest" "discover" "-s" (Join-Path $ProjectRoot "desktop\tests") "-v"
        Invoke-Native $PythonExe "-m" "unittest" "discover" "-s" (Join-Path $ProjectRoot "packaging\tests") "-v"
    }
    finally {
        Remove-TestDirectory $TestTemp
    }
}

Remove-LocalTempDirectory $StagingRoot "TideguardPackagingBuild-"
New-Item -ItemType Directory -Force -Path $OutputRoot, $WorkRoot, $ReleaseDir | Out-Null

try {
    Invoke-Native $PythonExe "-m" "PyInstaller" "--noconfirm" "--clean" `
        "--distpath" $OutputRoot "--workpath" $WorkRoot (Join-Path $ProjectRoot "packaging\tideguard.spec")

$AppExe = Join-Path $AppOutput "Tideguard.exe"
if (-not (Test-Path -LiteralPath $AppExe)) {
    throw "PyInstaller did not create $AppExe"
}
Assert-NoCredentialArtifacts $AppOutput

$selfTest = Start-Process -FilePath $AppExe -ArgumentList "--self-test" -PassThru
if (-not $selfTest.WaitForExit(30000)) {
    Stop-Process -Id $selfTest.Id -Force -ErrorAction SilentlyContinue
    throw "Frozen desktop self-test timed out after 30 seconds"
}
$selfTest.Refresh()
if ($selfTest.ExitCode -ne 0) {
    throw "Frozen desktop self-test failed with exit code $($selfTest.ExitCode)"
}

$PortableZip = Join-Path $ReleaseDir "Tideguard-$Version-windows-x64.zip"
$InstallerExe = Join-Path $ReleaseDir "Tideguard-Setup-$Version.exe"
$ManifestPath = Join-Path $ReleaseDir "Tideguard-$Version-manifest.json"
$ChecksumsPath = Join-Path $ReleaseDir "SHA256SUMS.txt"
foreach ($oldFile in @($PortableZip, $InstallerExe, $ManifestPath, $ChecksumsPath)) {
    if (Test-Path -LiteralPath $oldFile) { Remove-Item -LiteralPath $oldFile -Force }
}

Compress-Archive -Path (Join-Path $AppOutput "*") -DestinationPath $PortableZip -CompressionLevel Optimal

$manifest = Get-ChildItem -LiteralPath $AppOutput -Recurse -File | Sort-Object FullName | ForEach-Object {
    $appPrefix = $AppOutput.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    [ordered]@{
        path = $_.FullName.Substring($appPrefix.Length).Replace('\', '/')
        size = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$manifestJson = [ordered]@{
    product = "Tideguard"
    version = $Version
    platform = "windows-x64"
    credentialsBundled = $false
    files = @($manifest)
} | ConvertTo-Json -Depth 5
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($ManifestPath, $manifestJson + [Environment]::NewLine, $utf8NoBom)

    if (-not $SkipInstaller) {
        $iscc = Find-InnoCompiler
        if (-not $iscc) {
            throw "Inno Setup 6 compiler not found. Install Inno Setup or use -SkipInstaller for a portable-only build."
        }
        $webView2Bootstrapper = Join-Path $StagingRoot "MicrosoftEdgeWebview2Setup.exe"
        Invoke-WebRequest -UseBasicParsing -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $webView2Bootstrapper
        $webView2Signature = Get-AuthenticodeSignature -LiteralPath $webView2Bootstrapper
        if ($webView2Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
            $null -eq $webView2Signature.SignerCertificate -or
            $webView2Signature.SignerCertificate.Subject -notmatch '(^|, )O=Microsoft Corporation(,|$)') {
            throw "The downloaded WebView2 bootstrapper is not validly signed by Microsoft"
        }
        $previousPackageSource = $env:TIDEGUARD_PACKAGE_SOURCE
        $previousWebView2Bootstrapper = $env:TIDEGUARD_WEBVIEW2_BOOTSTRAPPER
        try {
            $env:TIDEGUARD_PACKAGE_SOURCE = $AppOutput
            $env:TIDEGUARD_WEBVIEW2_BOOTSTRAPPER = $webView2Bootstrapper
            Invoke-Native $iscc "/DMyAppVersion=$Version" "/O$ReleaseDir" "/FTideguard-Setup-$Version" `
                (Join-Path $ProjectRoot "packaging\installer.iss")
        }
        finally {
            $env:TIDEGUARD_PACKAGE_SOURCE = $previousPackageSource
            $env:TIDEGUARD_WEBVIEW2_BOOTSTRAPPER = $previousWebView2Bootstrapper
        }
        if (-not (Test-Path -LiteralPath $InstallerExe)) {
            throw "Inno Setup did not create $InstallerExe"
        }
    }

    $checksumTargets = @($PortableZip, $ManifestPath)
    if (Test-Path -LiteralPath $InstallerExe) { $checksumTargets += $InstallerExe }
    $checksumLines = $checksumTargets | Sort-Object | ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $([IO.Path]::GetFileName($_))"
    }
    $checksumLines | Set-Content -LiteralPath $ChecksumsPath -Encoding ascii

    Write-Host "Release artifacts created in $ReleaseDir"
}
finally {
    Remove-LocalTempDirectory $StagingRoot "TideguardPackagingBuild-"
}
