<#
.SYNOPSIS
  Deploys the latest main on THIS machine: pull, install, check, back up,
  stop service, migrate, collectstatic, start service, health-check.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
  ssh officepc "powershell -ExecutionPolicy Bypass -File C:\Users\MATTA\Desktop\inventory-\scripts\deploy.ps1"

  Run from an elevated shell (an SSH session to the office PC already is).
  Uses sc.exe for the service on purpose: `nssm restart` hangs on a UAC prompt
  over SSH on this machine. Paths come from the script's own location, so it
  works from any checkout.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = 'InventoryApp',
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$App  = Join-Path $Root 'inventory'
$Py   = Join-Path $Root '.venv\Scripts\python.exe'
$Url  = "http://localhost:$Port/employee-login/"

function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function Run([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe $($Arguments -join ' ') exited with code $LASTEXITCODE"
    }
}

function Wait-ServiceStatus([string]$Wanted, [int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ((Get-Service $ServiceName).Status -eq $Wanted) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "Service $ServiceName did not reach '$Wanted' within $TimeoutSeconds s"
}

function Set-ServiceRunning {
    if ((Get-Service $ServiceName).Status -ne 'Running') {
        Run 'sc.exe' @('start', $ServiceName)
        Wait-ServiceStatus 'Running'
    }
}

$serviceStopped = $false
$previous = $null
$backupLine = $null

try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw 'Run this from an elevated (Administrator) shell.' }
    if (-not (Test-Path $Py)) { throw "Virtualenv python not found: $Py" }
    Get-Service $ServiceName | Out-Null

    Set-Location $Root
    if (git status --porcelain --untracked-files=no) {
        throw 'Tracked files have local changes; commit or discard them before deploying.'
    }

    Step 'Pulling latest main'
    $previous = (git rev-parse --short HEAD)
    Run 'git' @('pull', '--ff-only')
    $current = (git rev-parse --short HEAD)

    Step 'Installing requirements'
    Run $Py @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $Root 'requirements.txt'))

    Set-Location $App
    Step 'Django system check'
    Run $Py @('manage.py', 'check')

    Step 'Backing up the database before migrating'
    # Capture everything before filtering: piping straight into Select-Object -First 1
    # stops the pipeline early and leaves a non-zero $LASTEXITCODE on a good backup.
    $backupOutput = & $Py manage.py backup_db
    if ($LASTEXITCODE -ne 0) { throw 'backup_db failed; not touching the live service.' }
    $backupLine = ($backupOutput | Select-String 'Backed up to' | Select-Object -First 1)
    Write-Host $backupLine

    Step "Stopping $ServiceName"
    Run 'sc.exe' @('stop', $ServiceName)
    $serviceStopped = $true
    Wait-ServiceStatus 'Stopped'

    Step 'Applying migrations'
    Run $Py @('manage.py', 'migrate', '--noinput')

    Step 'Collecting static files'
    Run $Py @('manage.py', 'collectstatic', '--noinput', '--verbosity', '0')

    Step "Starting $ServiceName"
    Set-ServiceRunning
    $serviceStopped = $false

    Step "Health check: $Url"
    $status = $null
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $status = (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5).StatusCode
            if ($status -eq 200) { break }
        } catch {
            $status = $null
        }
        Start-Sleep -Seconds 1
    }
    if ($status -ne 200) { throw "Health check failed: $Url did not return 200 within 30 s" }

    Write-Host ''
    Write-Host "DEPLOYED  $previous -> $current  (service Running, $Url = 200)" -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ''
    Write-Host "DEPLOY FAILED: $($_.Exception.Message)" -ForegroundColor Red
    if ($previous) {
        Write-Host "Previous commit was $previous. To roll the code back: git checkout $previous" -ForegroundColor Yellow
    }
    if ($backupLine) {
        Write-Host "Pre-deploy DB snapshot: $backupLine" -ForegroundColor Yellow
    }
    if ($serviceStopped) {
        Write-Host "Restarting $ServiceName so the plant isn't left without the app..." -ForegroundColor Yellow
        try { Set-ServiceRunning } catch { Write-Host "Could not restart it: $($_.Exception.Message)" -ForegroundColor Red }
    }
    exit 1
}
