[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$Parameters = 'PM2.5',

    [ValidateSet('auto', 'latest', 'live')]
    [string]$Snapshot = 'auto'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Config,
        $EnvFile,
        (Join-Path $ProjectRoot 'scripts\Run-AirParameterTraining.ps1')
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    Write-Host 'ETAP 2 — PILOTAŻOWY MODEL NA NIEZMIENNYM SNAPSHOCIE' -ForegroundColor Cyan
    Write-Host "Cele: $Parameters"
    Write-Host "Snapshot: $Snapshot"
    Write-Host (
        'Importer może działać równolegle. Model otrzyma dataset_id i SHA-256.'
    ) -ForegroundColor Green

    & (Join-Path $ProjectRoot 'scripts\Run-AirParameterTraining.ps1') `
        -ProjectRoot $ProjectRoot `
        -RuntimeRoot $RuntimeRoot `
        -Parameters $Parameters `
        -Profile quick `
        -Snapshot $Snapshot

    if ($LASTEXITCODE -ne 0) {
        throw "Pilotażowy trening zakończył się kodem $LASTEXITCODE."
    }

    Write-Host ''
    Write-Host 'Snapshoty:' -ForegroundColor Cyan
    & (Join-Path $ProjectRoot 'scripts\Show-TrainingSnapshots.ps1') `
        -ProjectRoot $ProjectRoot `
        -RuntimeRoot $RuntimeRoot `
        -Profile quick

    Write-Host ''
    Write-Host 'Gotowość modeli:' -ForegroundColor Cyan
    & $Python -m smog_ai hourly-readiness `
        --config $Config `
        --env-file $EnvFile
    if ($LASTEXITCODE -ne 0) {
        throw "hourly-readiness zakończył się kodem $LASTEXITCODE."
    }

    Write-Host ''
    Write-Host 'Brama jakości Etapu 2:' -ForegroundColor Cyan
    & (Join-Path $ProjectRoot 'scripts\Test-Stage2ModelQuality.ps1') `
        -ProjectRoot $ProjectRoot `
        -RuntimeRoot $RuntimeRoot `
        -Parameters $Parameters

    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
