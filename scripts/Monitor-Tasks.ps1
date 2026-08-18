$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RuntimeRoot = 'C:\ProgramData\SmogAI'
$AutomationRoot = Join-Path $RuntimeRoot 'logs\automation'
$CurrentPath = Join-Path $AutomationRoot 'current.json'
$TaskPath = '\SmogAI\'
$TaskName = 'SmogAI-HF21-Serving-8h'

function Get-FirstValue {
    param(
        [object]$Object,
        [string[]]$Names
    )

    if ($null -eq $Object) {
        return $null
    }

    foreach ($Name in $Names) {
        $Property = $Object.PSObject.Properties[$Name]

        if (
            $null -ne $Property -and
            $null -ne $Property.Value
        ) {
            return $Property.Value
        }
    }

    return $null
}

Write-Host '=== 1/4 ZADANIE SERVING ==='

$Task = Get-ScheduledTask `
    -TaskPath $TaskPath `
    -TaskName $TaskName `
    -ErrorAction Stop

$TaskInfo = Get-ScheduledTaskInfo `
    -TaskPath $TaskPath `
    -TaskName $TaskName `
    -ErrorAction Stop

Write-Host "State=$($Task.State)"
Write-Host "LastRunTime=$($TaskInfo.LastRunTime)"
Write-Host "LastTaskResult=$($TaskInfo.LastTaskResult)"
Write-Host "NextRunTime=$($TaskInfo.NextRunTime)"

Write-Host '=== 2/4 AKTYWNY RUN AUTOMATU ==='

if (-not (Test-Path -LiteralPath $CurrentPath -PathType Leaf)) {
    throw "Brak pliku: $CurrentPath"
}

$Current = Get-Content -LiteralPath $CurrentPath -Raw |
    ConvertFrom-Json

$RunId = [string](Get-FirstValue `
    -Object $Current `
    -Names @('run_id','runId','id'))

$RunDirectoryValue = Get-FirstValue `
    -Object $Current `
    -Names @('run_dir','run_root','runDirectory','path')

$RunDirectory = if (
    $null -ne $RunDirectoryValue -and
    -not [string]::IsNullOrWhiteSpace([string]$RunDirectoryValue)
) {
    [string]$RunDirectoryValue
}
elseif (-not [string]::IsNullOrWhiteSpace($RunId)) {
    Join-Path (Join-Path $AutomationRoot 'runs') $RunId
}
else {
    $RunDirectories = @(
        Get-ChildItem `
            -LiteralPath (Join-Path $AutomationRoot 'runs') `
            -Directory `
            -ErrorAction Stop |
        Sort-Object LastWriteTimeUtc -Descending
    )

    if (@($RunDirectories).Count -eq 0) {
        throw 'Nie znaleziono katalogu aktywnego runu.'
    }

    $RunDirectories[0].FullName
}

$RunJsonPath = Join-Path $RunDirectory 'run.json'

if (-not (Test-Path -LiteralPath $RunJsonPath -PathType Leaf)) {
    throw "Brak run.json: $RunJsonPath"
}

$Run = Get-Content -LiteralPath $RunJsonPath -Raw |
    ConvertFrom-Json

if ([string]::IsNullOrWhiteSpace($RunId)) {
    $RunId = [string](Get-FirstValue `
        -Object $Run `
        -Names @('run_id','runId','id'))
}

$ProgressObject = Get-FirstValue `
    -Object $Run `
    -Names @('progress','current_progress','runtime_progress')

$RunStatus = Get-FirstValue `
    -Object $Run `
    -Names @('status','run_status','state')

$CurrentStage = Get-FirstValue `
    -Object $Run `
    -Names @('current_stage','stage','active_stage','stage_name')

if ($null -eq $CurrentStage) {
    $CurrentStage = Get-FirstValue `
        -Object $ProgressObject `
        -Names @('current_stage','stage','active_stage','stage_name','task')
}

$PercentRaw = Get-FirstValue `
    -Object $Run `
    -Names @('overall_percent','percent','progress_percent')

if ($null -eq $PercentRaw) {
    $PercentRaw = Get-FirstValue `
        -Object $ProgressObject `
        -Names @('overall_percent','percent','progress_percent')
}

$StartedRaw = Get-FirstValue `
    -Object $Run `
    -Names @('started_at','start_time','startedAt','created_at')

if ($null -eq $StartedRaw) {
    $StartedRaw = Get-FirstValue `
        -Object $Current `
        -Names @('started_at','start_time','startedAt','created_at')
}

$StageFiles = @(
    Get-ChildItem `
        -LiteralPath $RunDirectory `
        -File `
        -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -match '^\d{2}-'
    } |
    Sort-Object Name
)

if (
    $null -eq $CurrentStage -and
    @($StageFiles).Count -gt 0
) {
    $LatestStageName = [IO.Path]::GetFileNameWithoutExtension(
        $StageFiles[-1].Name
    )

    $CurrentStage = $LatestStageName
}

$StartedAt = $null
$Elapsed = $null

if ($null -ne $StartedRaw) {
    try {
        $StartedAt = [DateTimeOffset]::Parse([string]$StartedRaw)
        $Elapsed = [DateTimeOffset]::Now - $StartedAt
    }
    catch {
        $StartedAt = $null
    }
}

$OverallPercent = $null

if ($null -ne $PercentRaw) {
    try {
        $OverallPercent = [double]$PercentRaw
    }
    catch {
        $OverallPercent = $null
    }
}

$Eta = $null
$EtaSource = 'unavailable'

$EtaRaw = Get-FirstValue `
    -Object $ProgressObject `
    -Names @(
        'eta_seconds',
        'remaining_seconds',
        'estimated_remaining_seconds'
    )

if ($null -eq $EtaRaw) {
    $EtaRaw = Get-FirstValue `
        -Object $Run `
        -Names @(
            'eta_seconds',
            'remaining_seconds',
            'estimated_remaining_seconds'
        )
}

if ($null -ne $EtaRaw) {
    try {
        $Eta = [TimeSpan]::FromSeconds([double]$EtaRaw)
        $EtaSource = 'reported'
    }
    catch {
        $Eta = $null
    }
}
elseif (
    $null -ne $Elapsed -and
    $null -ne $OverallPercent -and
    $OverallPercent -gt 1 -and
    $OverallPercent -lt 100
) {
    $EstimatedTotalSeconds = (
        $Elapsed.TotalSeconds /
        ($OverallPercent / 100.0)
    )

    $RemainingSeconds = [Math]::Max(
        0,
        $EstimatedTotalSeconds - $Elapsed.TotalSeconds
    )

    $Eta = [TimeSpan]::FromSeconds($RemainingSeconds)
    $EtaSource = 'calculated_from_overall_progress'
}

Write-Host "RunId=$RunId"
Write-Host "RunDirectory=$RunDirectory"
Write-Host "RunStatus=$RunStatus"
Write-Host "CurrentStage=$CurrentStage"
Write-Host "OverallPercent=$OverallPercent"
Write-Host "Elapsed=$Elapsed"
Write-Host "ETA=$Eta"
Write-Host "ETASource=$EtaSource"

Write-Host '=== 3/4 OSTATNIE ZDARZENIA ==='

$EventsPath = Join-Path $RunDirectory 'events.jsonl'

if (Test-Path -LiteralPath $EventsPath -PathType Leaf) {
    Get-Content -LiteralPath $EventsPath -Tail 15
}
else {
    Write-Host 'Brak events.jsonl.'
}

Write-Host '=== 4/4 PODSUMOWANIE ==='

[pscustomobject]@{
    Status               = if ([string]$Task.State -eq 'Running') {
        'SERVING_RUNNING'
    }
    else {
        'SERVING_NOT_RUNNING'
    }
    TaskState            = [string]$Task.State
    LastTaskResult       = $TaskInfo.LastTaskResult
    RunId                = $RunId
    RunStatus            = [string]$RunStatus
    CurrentStage         = [string]$CurrentStage
    OverallPercent       = $OverallPercent
    StartedAt            = $StartedAt
    Elapsed              = $Elapsed
    ETA                  = $Eta
    ETASource            = $EtaSource
    RunDirectory         = $RunDirectory
    FilesModified        = $false
    ExternalReads        = $false
    ExternalWrites       = $false
}