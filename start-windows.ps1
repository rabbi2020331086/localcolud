[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$backendPython = Join-Path $Root 'backend\.venv\Scripts\python.exe'

if (-not (Test-Path $backendPython)) {
    throw 'Backend environment is missing. Run .\setup-windows.ps1 first.'
}
if (-not (Test-Path (Join-Path $Root 'backend\.env'))) {
    throw 'backend\.env is missing. Run .\setup-windows.ps1 first.'
}
$pnpmCommand = Get-Command 'pnpm.cmd' -ErrorAction SilentlyContinue
if (-not $pnpmCommand) {
    throw 'pnpm was not found. Run .\setup-windows.ps1 again, then reopen PowerShell.'
}
$pnpmVersion = (& $pnpmCommand.Source --version).Trim()
if ($pnpmVersion -ne '10.15.1') {
    throw "pnpm $pnpmVersion is installed, but this Node.js 20 setup requires pnpm 10.15.1. Run .\setup-windows.ps1 again."
}

$multipassCommand = Get-Command 'multipass.exe' -ErrorAction SilentlyContinue
if (-not $multipassCommand) {
    $installedMultipass = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $installedMultipass) {
        $multipassCommand = Get-Item $installedMultipass
    }
}
if (-not $multipassCommand) {
    Write-Warning 'Multipass is unavailable. The console will open, but VM launch/start/stop actions cannot work until Canonical Multipass is installed and PowerShell is reopened.'
}

$timeStatus = (& w32tm /query /status 2>&1 | Out-String)
if ($timeStatus -match 'Leap Indicator:\s*3' -or $timeStatus -match 'not synchronized') {
    throw 'Windows time is not synchronized. Open Settings > Time & language > Date & time, enable Set time automatically, click Sync now, then reopen PowerShell.'
}

Write-Host 'Checking MongoDB Atlas connection...' -ForegroundColor Cyan
Push-Location (Join-Path $Root 'backend')
try {
    & $backendPython -m app.check_database
    if ($LASTEXITCODE -ne 0) {
        throw 'MongoDB Atlas is not reachable. Correct backend\.env, Atlas Network Access, or the Windows clock, then retry.'
    }
}
finally {
    Pop-Location
}

$outLog = Join-Path $Root 'backend\backend.out.log'
$errorLog = Join-Path $Root 'backend\backend.error.log'
$backend = Start-Process -FilePath $backendPython `
    -ArgumentList '-m','uvicorn','app.main:app','--reload' `
    -WorkingDirectory (Join-Path $Root 'backend') `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errorLog `
    -PassThru -WindowStyle Hidden

try {
    Start-Sleep -Seconds 3
    if ($backend.HasExited) {
        $details = if (Test-Path $errorLog) { Get-Content $errorLog -Raw } else { 'No backend log was written.' }
        throw "Backend failed to start.`n$details"
    }
    Write-Host 'Backend: http://localhost:8000' -ForegroundColor Green
    Write-Host 'Frontend: http://localhost:5173' -ForegroundColor Green
    Write-Host 'Press Ctrl+C to stop both applications.' -ForegroundColor Cyan
    Push-Location (Join-Path $Root 'frontend')
    try {
        & $pnpmCommand.Source run dev
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($backend -and -not $backend.HasExited) {
        Stop-Process -Id $backend.Id -ErrorAction SilentlyContinue
    }
}
