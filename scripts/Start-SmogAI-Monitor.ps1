[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateRange(2,300)][int]$RefreshSeconds = 30,
    [ValidateRange(1024,65535)][int]$Port = 8504,
    [switch]$OpenChrome
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$Launcher = Join-Path $ProjectRoot 'scripts\Start-SmogAI-AutomationMonitor.ps1'
$LogRoot = Join-Path $RuntimeRoot 'logs\automation-monitor'
$Url = "http://127.0.0.1:$Port"
$HealthUrl = "$Url/_stcore/health"

function Test-MonitorReady {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 3
        return $Response.StatusCode -eq 200
    } catch { return $false }
}

function Find-Chrome {
    $Candidates = @()
    $Command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($null -ne $Command) { $Candidates += $Command.Source }
    $ProgramFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    $Candidates += @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe')
        $(if ($ProgramFilesX86) { Join-Path $ProgramFilesX86 'Google\Chrome\Application\chrome.exe' })
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    return $Candidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
    } | Select-Object -First 1
}

if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "Brak skryptu monitora: $Launcher"
}

$AlreadyRunning = Test-MonitorReady
$Process = $null
$Stdout = $null
$Stderr = $null
if (-not $AlreadyRunning) {
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Stdout = Join-Path $LogRoot "monitor-$Stamp.stdout.log"
    $Stderr = Join-Path $LogRoot "monitor-$Stamp.stderr.log"
    $PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
    $Arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $Launcher),
        '-ProjectRoot', ('"{0}"' -f $ProjectRoot),
        '-RuntimeRoot', ('"{0}"' -f $RuntimeRoot),
        '-RefreshSeconds', [string]$RefreshSeconds,
        '-Port', [string]$Port
    ) -join ' '
    $Process = Start-Process -FilePath $PowerShellExe -ArgumentList $Arguments `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -PassThru
    $Deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $Deadline -and -not (Test-MonitorReady)) {
        if ($Process.HasExited) {
            throw "Monitor zakonczyl sie przed gotowoscia. Log: $Stderr"
        }
        Start-Sleep -Seconds 2
    }
}

if (-not (Test-MonitorReady)) { throw "Monitor nie osiagnal gotowosci. Log: $Stderr" }

$Chrome = $null
if ($OpenChrome) {
    $Chrome = Find-Chrome
    if (-not $Chrome) { throw 'Nie znaleziono zwyklej instalacji Google Chrome.' }
    Start-Process -FilePath $Chrome -ArgumentList @('--new-tab', $Url) | Out-Null
}

[pscustomobject]@{
    Status = if ($AlreadyRunning) { 'MONITOR_ALREADY_RUNNING' } else { 'MONITOR_STARTED' }
    Url = $Url
    Ready = Test-MonitorReady
    RefreshSeconds = if ($AlreadyRunning) { 'unchanged-restart-to-change' } else { $RefreshSeconds }
    OpenedInChrome = [bool]$OpenChrome
    Chrome = $Chrome
    ProcessId = if ($null -ne $Process) { $Process.Id } else { $null }
    StdoutLog = $Stdout
    StderrLog = $Stderr
    ServingTaskModified = $false
} | Format-List
