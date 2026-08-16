[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$TaskPrefix = 'SmogAI-HF21',
    [ValidateSet('quick','normal','medium','full')][string]$Profile = 'normal',
    [ValidateRange(1,24)][int]$IntervalHours = 8,
    [ValidateRange(1,168)][int]$TrainingValidityHours = 24,
    [string]$StartTime = '00:35',
    [ValidateRange(7,48)][int]$ExecutionTimeLimitHours = 22,
    [bool]$ReplaceLegacySchedules = $true,
    [switch]$WakeComputer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$AutomationScript = Join-Path $ProjectRoot 'scripts\Invoke-SmogAI-ScheduledRefresh.ps1'
$RuntimeEnv = Join-Path $RuntimeRoot 'smog-ai.env'
if (-not (Test-Path -LiteralPath $AutomationScript -PathType Leaf)) { throw "Brak automatu: $AutomationScript" }
if (-not (Test-Path -LiteralPath $RuntimeEnv -PathType Leaf)) { throw "Brak konfiguracji runtime: $RuntimeEnv" }

# Update only the training-age setting; never print environment values or secrets.
$EnvLines = @(Get-Content -LiteralPath $RuntimeEnv)
$Setting = "SMOG_AI_MAX_LAST_TRAINING_AGE_HOURS=$TrainingValidityHours"
$Found = $false
$Updated = foreach ($Line in $EnvLines) {
    if ($Line -match '^SMOG_AI_MAX_LAST_TRAINING_AGE_HOURS=') { $Found = $true; $Setting } else { $Line }
}
if (-not $Found) { $Updated += $Setting }
$Updated | Set-Content -LiteralPath $RuntimeEnv -Encoding UTF8

$TaskPath = '\SmogAI\'
$TaskName = "$TaskPrefix-Refresh-${IntervalHours}h"
$PowerShellExe = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) {
    (Get-Command pwsh.exe).Source
} else {
    "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
}
$Arguments = @(
    '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $AutomationScript),
    '-ProjectRoot', ('"{0}"' -f $ProjectRoot),
    '-RuntimeRoot', ('"{0}"' -f $RuntimeRoot),
    '-Profile', $Profile,
    '-ExperimentalTargets', '"*"'
) -join ' '

if ($ReplaceLegacySchedules) {
    @('Quick-Hourly','Normal-00','Normal-06','Normal-12','Normal-18','Full-Daily') | ForEach-Object {
        $LegacyName = "$TaskPrefix-$_"
        if (Get-ScheduledTask -TaskName $LegacyName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $LegacyName -Confirm:$false
        }
    }
}

$Start = [DateTime]::Today.Add([TimeSpan]::Parse($StartTime))
if ($Start -le (Get-Date)) { $Start = $Start.AddHours($IntervalHours) }
$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Once -At $Start -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15) -ExecutionTimeLimit (New-TimeSpan -Hours $ExecutionTimeLimitHours) -WakeToRun:$WakeComputer -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "SmogAI: lokalny trening i Serving v2 co $IntervalHours h; ważność treningu $TrainingValidityHours h."
Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -InputObject $Task -Force | Out-Null

$DefinitionRoot = Join-Path $RuntimeRoot 'scheduled-task-definitions'
New-Item -ItemType Directory -Path $DefinitionRoot -Force | Out-Null
Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath | Out-File -LiteralPath (Join-Path $DefinitionRoot "$TaskName.xml") -Encoding utf8
$Info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
[pscustomobject]@{
    Task = "$TaskPath$TaskName"
    Profile = $Profile
    IntervalHours = $IntervalHours
    TrainingValidityHours = $TrainingValidityHours
    NextRunTime = $Info.NextRunTime
    MultipleInstances = 'IgnoreNew'
    RuntimeEnvUpdated = $RuntimeEnv
} | Format-List
