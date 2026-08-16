[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$TaskPrefix = 'SmogAI-HF21',
    [switch]$WakeComputer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$RefreshScript = Join-Path $ProjectRoot 'scripts\Invoke-SmogAI-ScheduledRefresh.ps1'
$TrainingScript = Join-Path $ProjectRoot 'scripts\Invoke-SmogAI-ScheduledTraining.ps1'
$RuntimeEnv = Join-Path $RuntimeRoot 'smog-ai.env'
foreach ($Path in @($RefreshScript, $TrainingScript, $RuntimeEnv)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Brak wymaganego pliku: $Path" }
}

function Set-EnvSetting([string[]]$Lines, [string]$Name, [string]$Value) {
    $Found = $false
    $Result = @($Lines | ForEach-Object {
        if ($_ -match ('^{0}=' -f [regex]::Escape($Name))) {
            $Found = $true
            "$Name=$Value"
        } else { $_ }
    })
    if (-not $Found) { $Result += "$Name=$Value" }
    return $Result
}

# Only non-secret cadence values are updated; existing credentials remain intact
# and are never displayed by this installer.
$EnvLines = @(Get-Content -LiteralPath $RuntimeEnv)
$Settings = [ordered]@{
    SMOG_AI_STALE_AIR_HOURS = '8'
    SMOG_AI_STALE_WEATHER_HOURS = '8'
    SMOG_AI_FRESHNESS_HOURS = '8'
    SMOG_AI_SERVING_REFRESH_HOURS = '8'
    SMOG_AI_REGULAR_TRAINING_HOURS = '12'
    SMOG_AI_HEAVY_TRAINING_HOURS = '28'
    SMOG_AI_TRAINING_DEFERRED_RETRY_MINUTES = '30'
    SMOG_AI_SERVING_RELEASE_RETENTION = '3'
}
foreach ($Entry in $Settings.GetEnumerator()) {
    $EnvLines = Set-EnvSetting $EnvLines $Entry.Key $Entry.Value
}
$EnvLines | Set-Content -LiteralPath $RuntimeEnv -Encoding UTF8

$TaskPath = '\SmogAI\'
$PowerShellExe = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) {
    (Get-Command pwsh.exe).Source
} else {
    "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
}
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

function Register-SmogaITask {
    param(
        [string]$Name,
        [string]$Script,
        [string[]]$ScriptArguments,
        [int]$IntervalHours,
        [string]$StartTime,
        [int]$ExecutionLimitHours,
        [int]$RestartCount
    )
    $Arguments = @(
        '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $Script),
        '-ProjectRoot', ('"{0}"' -f $ProjectRoot),
        '-RuntimeRoot', ('"{0}"' -f $RuntimeRoot)
    ) + $ScriptArguments
    $Start = [DateTime]::Today.Add([TimeSpan]::Parse($StartTime))
    while ($Start -le (Get-Date)) { $Start = $Start.AddHours($IntervalHours) }
    $Action = New-ScheduledTaskAction -Execute $PowerShellExe `
        -Argument ($Arguments -join ' ') -WorkingDirectory $ProjectRoot
    $Trigger = New-ScheduledTaskTrigger -Once -At $Start `
        -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $TaskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -RestartCount $RestartCount `
        -RestartInterval (New-TimeSpan -Minutes 30) `
        -ExecutionTimeLimit (New-TimeSpan -Hours $ExecutionLimitHours) `
        -WakeToRun:$WakeComputer -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    $Task = New-ScheduledTask -Action $Action -Trigger $Trigger `
        -Settings $TaskSettings -Principal $Principal `
        -Description 'SmogAI test profile; one shared training lease; no raw/training data in Spaces.'
    Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath `
        -InputObject $Task -Force | Out-Null
    $Info = Get-ScheduledTaskInfo -TaskName $Name -TaskPath $TaskPath
    [pscustomobject]@{
        Task = "$TaskPath$Name"
        IntervalHours = $IntervalHours
        NextRunTime = $Info.NextRunTime
        MultipleInstances = 'IgnoreNew'
        RetryMinutes = 30
    }
}

# The previous combined 8 h task must not duplicate the new Serving-only task.
@("$TaskPrefix-Refresh-8h", "$TaskPrefix-Refresh-8H") | Sort-Object -Unique | ForEach-Object {
    if (Get-ScheduledTask -TaskName $_ -TaskPath $TaskPath -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName $_ -TaskPath $TaskPath | Out-Null
    }
}

$Results = @()
$Results += Register-SmogaITask -Name "$TaskPrefix-Serving-8h" `
    -Script $RefreshScript `
    -ScriptArguments @('-Profile','serving','-PublishDigitalOcean') `
    -IntervalHours 8 -StartTime '00:35' -ExecutionLimitHours 7 -RestartCount 1
$Results += Register-SmogaITask -Name "$TaskPrefix-Training-12h" `
    -Script $TrainingScript -ScriptArguments @('-Profile','quick') `
    -IntervalHours 12 -StartTime '01:20' -ExecutionLimitHours 10 -RestartCount 2
$Results += Register-SmogaITask -Name "$TaskPrefix-Heavy-28h" `
    -Script $TrainingScript -ScriptArguments @('-Profile','full') `
    -IntervalHours 28 -StartTime '02:10' -ExecutionLimitHours 24 -RestartCount 3

$DefinitionRoot = Join-Path $RuntimeRoot 'scheduled-task-definitions'
New-Item -ItemType Directory -Path $DefinitionRoot -Force | Out-Null
foreach ($Row in $Results) {
    $Name = ($Row.Task -split '\\')[-1]
    Export-ScheduledTask -TaskName $Name -TaskPath $TaskPath |
        Out-File -LiteralPath (Join-Path $DefinitionRoot "$Name.xml") -Encoding utf8
}

$Results | Format-Table -AutoSize
[pscustomobject]@{
    Status = 'TEST_PROFILE_SCHEDULES_INSTALLED'
    FreshnessHours = 8
    ServingRefreshHours = 8
    RegularTrainingHours = 12
    HeavyTrainingHours = 28
    DeferredRetryMinutes = 30
    ServingReleaseRetention = 3
    SharedTrainingLock = 'snapshot-hourly-training'
    RuntimeEnvUpdated = $RuntimeEnv
    ExternalWrites = $false
} | Format-List
