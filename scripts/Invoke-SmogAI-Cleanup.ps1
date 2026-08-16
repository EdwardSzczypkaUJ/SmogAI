[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot='C:\ProgramData\SmogAI',
    [switch]$Apply,
    [int]$KeepTrainingQuick=2,
    [int]$KeepTrainingFull=3,
    [int]$KeepDashboardSnapshots=5,
    [int]$KeepForecastPublications=10,
    [int]$KeepMapSurfaceSets=5,
    [int]$KeepAutomationRuns=30,
    [int]$ProgressRetentionDays=30
)
$ErrorActionPreference='Stop'
if([string]::IsNullOrWhiteSpace($ProjectRoot)){$ProjectRoot=(Get-Location).Path}
$ProjectRoot=[IO.Path]::GetFullPath($ProjectRoot)
Set-Location -LiteralPath $ProjectRoot
$Python=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $Python)){throw "Brak Python venv: $Python"}
$Script=Join-Path $PSScriptRoot 'smog_ai_cleanup.py'
$Args=@(
    $Script,'--runtime-root',$RuntimeRoot,
    '--keep-training-quick',[string]$KeepTrainingQuick,
    '--keep-training-full',[string]$KeepTrainingFull,
    '--keep-dashboard-snapshots',[string]$KeepDashboardSnapshots,
    '--keep-forecast-publications',[string]$KeepForecastPublications,
    '--keep-map-surface-sets',[string]$KeepMapSurfaceSets,
    '--keep-automation-runs',[string]$KeepAutomationRuns,
    '--progress-retention-days',[string]$ProgressRetentionDays
)
if($Apply){
    if($PSCmdlet.ShouldProcess($RuntimeRoot,'Usunięcie starych artefaktów zgodnie z retencją')){$Args+='--apply'}
    else{return}
} else {
    Write-Host 'TRYB DRY RUN — nic nie zostanie usunięte. Dodaj -Apply, aby wykonać plan.' -ForegroundColor Yellow
}
& $Python @Args
exit $LASTEXITCODE
