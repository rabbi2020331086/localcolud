[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

function Require-Command([string]$Name, [string]$InstallMessage) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name was not found. $InstallMessage"
    }
}

Require-Command 'python' 'Install Python 3.11 or newer and enable Add Python to PATH.'
Require-Command 'node' 'Install Node.js 20 or newer from https://nodejs.org/.'
Require-Command 'npm' 'Install Node.js 20 or newer from https://nodejs.org/.'

$multipassCommand = Get-Command 'multipass.exe' -ErrorAction SilentlyContinue
if (-not $multipassCommand) {
    $installedMultipass = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $installedMultipass) {
        $multipassCommand = Get-Item $installedMultipass
    }
}
if (-not $multipassCommand) {
    Write-Warning 'Multipass is not installed or is not on PATH. Setup can continue, but launching VMs will be disabled until Canonical Multipass is installed.'
}

$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pythonVersion -lt [version]'3.11') {
    throw "Python 3.11 or newer is required. Found Python $pythonVersion."
}

$timeStatus = (& w32tm /query /status 2>&1 | Out-String)
if ($timeStatus -match 'Leap Indicator:\s*3' -or $timeStatus -match 'not synchronized') {
    throw 'Windows time is not synchronized. Open Settings > Time & language > Date & time, enable Set time automatically, click Sync now, then reopen PowerShell.'
}

$requiredPnpmVersion = '10.15.1'
$pnpmCommand = Get-Command 'pnpm.cmd' -ErrorAction SilentlyContinue
if ($pnpmCommand) {
    $installedPnpmVersion = (& $pnpmCommand.Source --version).Trim()
}
if (-not $pnpmCommand -or $installedPnpmVersion -ne $requiredPnpmVersion) {
    Write-Host "Installing pnpm $requiredPnpmVersion for Node.js 20..." -ForegroundColor Cyan
    & npm install --global "pnpm@$requiredPnpmVersion"
    $npmPrefix = (& npm prefix --global).Trim()
    $pnpmPath = Join-Path $npmPrefix 'pnpm.cmd'
    if (-not (Test-Path $pnpmPath)) {
        throw 'pnpm was installed but pnpm.cmd could not be located. Reopen PowerShell and rerun setup.'
    }
    $pnpmExecutable = $pnpmPath
}
else {
    $pnpmExecutable = $pnpmCommand.Source
}

$backendEnv = Join-Path $Root 'backend\.env'
$frontendEnv = Join-Path $Root 'frontend\.env'
if (-not (Test-Path $backendEnv)) {
    Copy-Item (Join-Path $Root 'backend\.env.example') $backendEnv
}
if (-not (Test-Path $frontendEnv)) {
    Copy-Item (Join-Path $Root 'frontend\.env.example') $frontendEnv
}

$envText = Get-Content $backendEnv -Raw
$needsJwt = $envText -match '(?m)^JWT_SECRET=(?:\s*|generate-with-documented-command)$'
$needsFernet = $envText -match '(?m)^PRIVATE_KEY_ENCRYPTION_KEY=(?:\s*|generate-with-documented-command)$'
if ($needsJwt -or $needsFernet) {
    $secrets = & python -c "import base64,json,secrets; print(json.dumps({'jwt':secrets.token_hex(32),'fernet':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}))" | ConvertFrom-Json
    if ($needsJwt) {
        $envText = $envText -replace '(?m)^JWT_SECRET=.*$', "JWT_SECRET=$($secrets.jwt)"
    }
    if ($needsFernet) {
        $envText = $envText -replace '(?m)^PRIVATE_KEY_ENCRYPTION_KEY=.*$', "PRIVATE_KEY_ENCRYPTION_KEY=$($secrets.fernet)"
    }
}
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($backendEnv, $envText, $utf8WithoutBom)

$venv = Join-Path $Root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $venv)) {
    & python -m venv (Join-Path $Root 'backend\.venv')
}
& $venv -m pip install --upgrade pip
& $venv -m pip install -e "$Root\backend[dev]"
Write-Host 'Checking MongoDB Atlas connection...' -ForegroundColor Cyan
Push-Location (Join-Path $Root 'backend')
try {
    & $venv -m app.check_database
    if ($LASTEXITCODE -ne 0) {
        throw 'MongoDB Atlas is not reachable. Correct backend\.env, Atlas Network Access, or the Windows clock, then rerun setup.'
    }
}
finally {
    Pop-Location
}

Push-Location (Join-Path $Root 'frontend')
try {
    $pnpmArguments = @('install', '--frozen-lockfile')
    $modulesFile = Join-Path $Root 'frontend\node_modules\.modules.yaml'
    if (Test-Path $modulesFile) {
        $modulesMetadata = Get-Content $modulesFile -Raw
        if ($modulesMetadata -notmatch 'packageManager:\s+pnpm@10\.15\.1') {
            $pnpmArguments += '--force'
        }
    }
    & $pnpmExecutable @pnpmArguments
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host 'Setup complete. MongoDB Atlas is used directly; Docker is not required.' -ForegroundColor Green
if (-not $multipassCommand) {
    Write-Host 'VM prerequisite: install Canonical Multipass, reopen PowerShell, and confirm: multipass.exe version' -ForegroundColor Yellow
}
Write-Host 'Next command: .\start-windows.ps1' -ForegroundColor Cyan
