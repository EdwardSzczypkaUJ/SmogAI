[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [ValidateSet('serving','quick','normal','medium','full')][string]$Profile='quick',
    [string]$RuntimeRoot='C:\ProgramData\SmogAI',
    [string]$Targets,
    [string]$ExperimentalTargets,
    [string]$Parameters,
    [string]$DataStart,
    [string]$DataEnd,
    [string]$TrainingStart,
    [string]$TrainingEnd,
    [double]$ResourceSampleSeconds=5,
    [switch]$FillMissingRanges,
    [switch]$SkipGiosCurrent,
    [switch]$SkipImgwCurrent,
    [int]$MaxValidationErrors=150,
    [switch]$SkipCleanup,
    [int]$KeepTrainingQuick=2,
    [int]$KeepTrainingFull=3,
    [int]$KeepDashboardSnapshots=5,
    [int]$KeepForecastPublications=10,
    [int]$KeepMapSurfaceSets=5,
    [int]$KeepAutomationRuns=30,
    [int]$ProgressRetentionDays=30,
    [int]$IncompleteSnapshotHours=24,
    [switch]$CleanupOnly,
    [switch]$CleanupDryRun,
    [switch]$OpenMonitor,
    [switch]$Resume,
    [string]$RunId
)
$ErrorActionPreference='Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8:backslashreplace'
if([string]::IsNullOrWhiteSpace($ProjectRoot)){$ProjectRoot=(Get-Location).Path}
$ProjectRoot=[IO.Path]::GetFullPath($ProjectRoot)
if(-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'smog_ai') -PathType Container)){throw "Bieżący katalog nie jest katalogiem głównym SmogAI: $ProjectRoot"}
Set-Location -LiteralPath $ProjectRoot
$Python=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $Python)){throw "Brak Python venv: $Python"}
$Engine=Join-Path $PSScriptRoot 'smog_ai_automation.py'
if($OpenMonitor){
    $Monitor=Join-Path $PSScriptRoot 'Start-SmogAI-AutomationMonitor.ps1'
    Start-Process powershell.exe -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-File',('"'+$Monitor+'"'),'-ProjectRoot',('"'+$ProjectRoot+'"'),'-RuntimeRoot',('"'+$RuntimeRoot+'"')) | Out-Null
}
$Args=@($Engine,'--project-root',$ProjectRoot,'--runtime-root',$RuntimeRoot,'--profile',$Profile)
if($Resume){$Args+='--resume'}
if($RunId){$Args+=@('--run-id',$RunId)}
if($Targets){$Args+=@('--targets',$Targets)}
if($ExperimentalTargets){$Args+=@('--experimental-targets',$ExperimentalTargets)}
if($Parameters){$Args+=@('--parameters',$Parameters)}
if($DataStart){$Args+=@('--data-start',$DataStart)}
if($DataEnd){$Args+=@('--data-end',$DataEnd)}
if($TrainingStart){$Args+=@('--training-start',$TrainingStart)}
if($TrainingEnd){$Args+=@('--training-end',$TrainingEnd)}
$Args+=@('--resource-sample-seconds',[string]$ResourceSampleSeconds)
if($FillMissingRanges){$Args+='--fill-missing-ranges'}
if($SkipGiosCurrent){$Args+='--skip-gios-current'}
if($SkipImgwCurrent){$Args+='--skip-imgw-current'}
$Args+=@('--keep-training-quick',[string]$KeepTrainingQuick,'--keep-training-full',[string]$KeepTrainingFull,'--keep-dashboard-snapshots',[string]$KeepDashboardSnapshots,'--keep-forecast-publications',[string]$KeepForecastPublications,'--keep-map-surface-sets',[string]$KeepMapSurfaceSets,'--keep-automation-runs',[string]$KeepAutomationRuns,'--progress-retention-days',[string]$ProgressRetentionDays)
$Args+=@('--incomplete-snapshot-hours',[string]$IncompleteSnapshotHours)
if($CleanupOnly){$Args+='--cleanup-only'}
if($CleanupDryRun){$Args+='--cleanup-dry-run'}
if($SkipCleanup){$Args+='--skip-cleanup'}
$Args+=@('--max-validation-errors',[string]$MaxValidationErrors)
& $Python @Args
exit $LASTEXITCODE
