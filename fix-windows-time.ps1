#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

Write-Host 'Configuring the Windows Time service...' -ForegroundColor Cyan
Set-Service -Name w32time -StartupType Automatic
Start-Service -Name w32time

& w32tm /config /syncfromflags:manual /manualpeerlist:"time.windows.com,0x9" /update
if ($LASTEXITCODE -ne 0) {
    throw 'Windows Time configuration failed.'
}

& w32tm /resync /rediscover
if ($LASTEXITCODE -ne 0) {
    throw 'Windows could not synchronize. Check the internet connection and Windows firewall, then use Settings > Time & language > Date & time > Sync now.'
}

Start-Sleep -Seconds 3
$status = (& w32tm /query /status 2>&1 | Out-String)
Write-Host $status
if ($status -match 'Leap Indicator:\s*3' -or $status -match 'not synchronized') {
    throw 'Windows Time is still not synchronized.'
}

Write-Host "Windows Time is synchronized. Current local time: $(Get-Date -Format o)" -ForegroundColor Green
Write-Host 'Close this Administrator window, reopen normal PowerShell, and run .\setup-windows.ps1.' -ForegroundColor Cyan

