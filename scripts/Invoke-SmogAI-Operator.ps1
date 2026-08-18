[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        'Status',
        'MonitorStart', 'MonitorStop',
        'ServingStart', 'ServingStop', 'ServingEnable', 'ServingDisable',
        'QuickStart', 'QuickStop', 'QuickEnable', 'QuickDisable',
        'HeavyStart', 'HeavyStop', 'HeavyEnable', 'HeavyDisable'
    )]
    [string]$Action,

    [Parameter()]
    [string]$ProjectRoot = (Get-Location).Path,

    [Parameter()]
    [ValidateRange(5, 3600)]
    [int]$RefreshSeconds = 30,

    [Parameter()]
    [ValidateRange(1024, 65535)]
    [int]$MonitorPort = 8504,

    [Parameter()]
    [switch]$OpenChrome,

    [Parameter()]
    [string]$Approval = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$TaskPath = '\SmogAI\'
$TaskNames = [ordered]@{
    Serving = 'SmogAI-HF21-Serving-8h'
    Quick = 'SmogAI-HF21-Training-12h'
    Heavy = 'SmogAI-HF21-Heavy-28h'
}
$DangerousApproval = 'CHANGE SMOGAI TASK STATE'
$ChangedTask = $null
$ExternalWrites = $false
$LocalSystemWrites = $false
$MonitorModified = $false
$TaskStartedNow = $false
$TaskStoppedNow = $false
$ScheduleEnabledNow = $false
$ScheduleDisabledNow = $false

function Get-SmogTaskStatus {
    param([string]$TaskName)

    $Task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        return [pscustomobject]@{
            Task = "$TaskPath$TaskName"
            Exists = $false
            State = 'Missing'
            Enabled = $false
            LastRunTime = $null
            LastTaskResult = $null
            NextRunTime = $null
            MultipleInstances = $null
            RestartInterval = $null
        }
    }

    $Info = $null
    try {
        $Info = Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    }
    catch { }

    return [pscustomobject]@{
        Task = "$TaskPath$TaskName"
        Exists = $true
        State = [string]$Task.State
        Enabled = ([string]$Task.State -ne 'Disabled')
        LastRunTime = if ($null -ne $Info) { $Info.LastRunTime } else { $null }
        LastTaskResult = if ($null -ne $Info) { $Info.LastTaskResult } else { $null }
        NextRunTime = if ($null -ne $Info) { $Info.NextRunTime } else { $null }
        MultipleInstances = [string]$Task.Settings.MultipleInstances
        RestartInterval = [string]$Task.Settings.RestartInterval
    }
}

function Assert-DangerousApproval {
    if ($Approval -cne $DangerousApproval) {
        throw "Ta operacja zatrzymuje lub wyłącza zadanie. Powtórz z: -Approval '$DangerousApproval'"
    }
}

function Start-SmScheduledTask {
    param([string]$TaskName)

    $Before = Get-SmogTaskStatus -TaskName $TaskName
    if (-not $Before.Exists) {
        throw "Nie istnieje zadanie: $($Before.Task)"
    }
    if (-not $Before.Enabled) {
        throw "Zadanie jest wyłączone: $($Before.Task). Najpierw użyj odpowiedniej akcji *Enable."
    }
    if ($Before.State -eq 'Running') {
        return $false
    }

    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    Start-Sleep -Seconds 2
    return $true
}

function Stop-SmScheduledTask {
    param([string]$TaskName)

    Assert-DangerousApproval
    $Before = Get-SmogTaskStatus -TaskName $TaskName
    if (-not $Before.Exists) {
        throw "Nie istnieje zadanie: $($Before.Task)"
    }
    if ($Before.State -ne 'Running') {
        return $false
    }

    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    Start-Sleep -Seconds 2
    return $true
}

function Enable-SmScheduledTask {
    param([string]$TaskName)

    $Before = Get-SmogTaskStatus -TaskName $TaskName
    if (-not $Before.Exists) {
        throw "Nie istnieje zadanie: $($Before.Task)"
    }
    if ($Before.Enabled) {
        return $false
    }

    Enable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName | Out-Null
    return $true
}

function Disable-SmScheduledTask {
    param([string]$TaskName)

    Assert-DangerousApproval
    $Before = Get-SmogTaskStatus -TaskName $TaskName
    if (-not $Before.Exists) {
        throw "Nie istnieje zadanie: $($Before.Task)"
    }
    if (-not $Before.Enabled) {
        return $false
    }

    Disable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName | Out-Null
    return $true
}

switch ($Action) {
    'MonitorStart' {
        $Script = Join-Path $ProjectRoot 'scripts\Start-SmogAI-Monitor.ps1'
        if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) {
            throw "Brak skryptu monitora: $Script"
        }
        $Arguments = @{
            ProjectRoot = $ProjectRoot
            Port = $MonitorPort
            RefreshSeconds = $RefreshSeconds
        }
        if ($OpenChrome) {
            $Arguments.OpenChrome = $true
        }
        & $Script @Arguments
        $MonitorModified = $true
        $LocalSystemWrites = $true
    }
    'MonitorStop' {
        $Script = Join-Path $ProjectRoot 'scripts\Stop-SmogAI-Monitor.ps1'
        if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) {
            throw "Brak skryptu monitora: $Script"
        }
        & $Script -Port $MonitorPort
        $MonitorModified = $true
        $LocalSystemWrites = $true
    }
    'ServingStart' {
        $ChangedTask = $TaskNames.Serving
        $TaskStartedNow = Start-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $TaskStartedNow
    }
    'ServingStop' {
        $ChangedTask = $TaskNames.Serving
        $TaskStoppedNow = Stop-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $TaskStoppedNow
    }
    'ServingEnable' {
        $ChangedTask = $TaskNames.Serving
        $ScheduleEnabledNow = Enable-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $ScheduleEnabledNow
    }
    'ServingDisable' {
        $ChangedTask = $TaskNames.Serving
        $ScheduleDisabledNow = Disable-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $ScheduleDisabledNow
    }
    'QuickStart' {
        $ChangedTask = $TaskNames.Quick
        $TaskStartedNow = Start-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $TaskStartedNow
    }
    'QuickStop' {
        $ChangedTask = $TaskNames.Quick
        $TaskStoppedNow = Stop-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $TaskStoppedNow
    }
    'QuickEnable' {
        $ChangedTask = $TaskNames.Quick
        $ScheduleEnabledNow = Enable-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $ScheduleEnabledNow
    }
    'QuickDisable' {
        $ChangedTask = $TaskNames.Quick
        $ScheduleDisabledNow = Disable-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $ScheduleDisabledNow
    }
    'HeavyStart' {
        $ChangedTask = $TaskNames.Heavy
        $TaskStartedNow = Start-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $TaskStartedNow
    }
    'HeavyStop' {
        $ChangedTask = $TaskNames.Heavy
        $TaskStoppedNow = Stop-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $TaskStoppedNow
    }
    'HeavyEnable' {
        $ChangedTask = $TaskNames.Heavy
        $ScheduleEnabledNow = Enable-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $ScheduleEnabledNow
    }
    'HeavyDisable' {
        $ChangedTask = $TaskNames.Heavy
        $ScheduleDisabledNow = Disable-SmScheduledTask -TaskName $ChangedTask
        $LocalSystemWrites = $ScheduleDisabledNow
    }
    'Status' { }
}

$TaskStatus = @(
    Get-SmogTaskStatus -TaskName $TaskNames.Serving
    Get-SmogTaskStatus -TaskName $TaskNames.Quick
    Get-SmogTaskStatus -TaskName $TaskNames.Heavy
)

$MonitorReady = $false
try {
    $MonitorResponse = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$MonitorPort/_stcore/health" `
        -Method Get `
        -UseBasicParsing `
        -TimeoutSec 3 `
        -ErrorAction Stop
    $MonitorReady = ([int]$MonitorResponse.StatusCode -eq 200)
}
catch { }

$TaskStatus | Format-Table -AutoSize

[pscustomobject]@{
    Status                       = 'SMOGAI_OPERATOR_ACTION_COMPLETE'
    Action                       = $Action
    ChangedTask                  = $ChangedTask
    TaskStartedNow               = $TaskStartedNow
    TaskStoppedNow               = $TaskStoppedNow
    ScheduleEnabledNow           = $ScheduleEnabledNow
    ScheduleDisabledNow          = $ScheduleDisabledNow
    MonitorReady                 = $MonitorReady
    MonitorUrl                   = "http://127.0.0.1:$MonitorPort"
    MonitorModified              = $MonitorModified
    TaskStatus                   = $TaskStatus
    LocalSystemWrites            = $LocalSystemWrites
    ExternalWrites               = $ExternalWrites
    ServingPublicationStarted    = $false
    ApplicationDeploymentStarted = $false
}
