[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$AuditPackage,

    [string]$Start,

    [string]$End,

    [string]$Parameters,

    [ValidateSet('local', 'object_store', 'hybrid')]
    [string]$CacheMode,

    [ValidateRange(0, 1000)]
    [int]$MinimumAirStations = 0,

    [ValidateRange(0, 1000)]
    [int]$MinimumWeatherStations = 0,

    [ValidateRange(1, 168)]
    [int]$MinimumHistoricalGapHours = 2,

    [ValidateRange(1, 10)]
    [int]$MaxNoProgressAttempts = 2,

    [ValidateRange(0, 10000)]
    [int]$MaxActions = 0,

    [switch]$IncludeIsolatedGaps,

    [switch]$DryRun,

    [switch]$SkipBackup
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
        $EnvFile
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    if ($AuditPackage) {
        $AuditPackage = (Resolve-Path -LiteralPath $AuditPackage).Path
    }

    Set-Location -LiteralPath $ProjectRoot
    $Version = (& $Python -c "import smog_ai; print(smog_ai.__version__)").Trim()
    if ($Version -ne '1.7.0') {
        throw "Wymagana wersja 1.7.0, uruchomiono: $Version"
    }

    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    $env:SMOG_AI_CONFIG = $Config
    $env:SMOG_AI_ENV_FILE = $EnvFile
    $env:PYTHONUNBUFFERED = '1'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host 'SMOG AI — RANGE-AWARE MISSING DATA BACKFILL' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host "Projekt:     $ProjectRoot"
    Write-Host "Runtime:     $RuntimeRoot"
    Write-Host "Wersja:      $Version"
    Write-Host ("Parametry:   {0}" -f $(if ($Parameters) { $Parameters } elseif ($AuditPackage) { "z audit-package" } else { "wszystkie obsługiwane" }))
    Write-Host "Cache Bridge:$(if ($CacheMode) { ' ' + $CacheMode } else { ' z config.yaml' })"
    Write-Host (
        "Próg AIR:    {0}" -f $(
            if ($MinimumAirStations -gt 0) {
                $MinimumAirStations
            }
            elseif ($AuditPackage) {
                'z audit-package'
            }
            else {
                1
            }
        )
    )
    Write-Host (
        "Próg pogody: {0}" -f $(
            if ($MinimumWeatherStations -gt 0) {
                $MinimumWeatherStations
            }
            elseif ($AuditPackage) {
                'z audit-package'
            }
            else {
                1
            }
        )
    )
    Write-Host "Dry run:     $DryRun"
    if ($AuditPackage) {
        Write-Host "Audyt:       $AuditPackage"
    }
    if ($Start) { Write-Host "Start:       $Start" }
    if ($End) { Write-Host "Koniec:      $End" }

    if (-not $DryRun -and -not $SkipBackup) {
        Write-Host ''
        Write-Host '[1/2] Backup SQLite przed uzupełnianiem luk...' -ForegroundColor Cyan
        & $Python -m smog_ai backup `
            --tier daily `
            --config $Config `
            --env-file $EnvFile
        if ($LASTEXITCODE -ne 0) {
            throw "Backup zakończył się kodem $LASTEXITCODE."
        }
    }

    Write-Host ''
    Write-Host '[2/2] Audyt, plan i wykonanie brakujących zakresów...' -ForegroundColor Cyan

    $Arguments = @(
        '-m', 'smog_ai', 'fill-missing-ranges',
        '--minimum-historical-gap-hours', [string]$MinimumHistoricalGapHours,
        '--max-no-progress-attempts', [string]$MaxNoProgressAttempts,
        '--max-actions', [string]$MaxActions,
        '--config', $Config,
        '--env-file', $EnvFile
    )

    if ($Parameters) {
        $Arguments += @('--parameters', $Parameters)
    }
    if ($AuditPackage) {
        $Arguments += @('--audit-package', $AuditPackage)
    }
    if ($Start) {
        $Arguments += @('--start', $Start)
    }
    if ($End) {
        $Arguments += @('--end', $End)
    }
    if ($CacheMode) {
        $Arguments += @('--cache-mode', $CacheMode)
    }
    if ($MinimumAirStations -gt 0) {
        $Arguments += @(
            '--minimum-air-stations',
            [string]$MinimumAirStations
        )
    }
    if ($MinimumWeatherStations -gt 0) {
        $Arguments += @(
            '--minimum-weather-stations',
            [string]$MinimumWeatherStations
        )
    }
    if ($IncludeIsolatedGaps) {
        $Arguments += '--include-isolated-gaps'
    }
    else {
        $Arguments += '--ignore-isolated-gaps'
    }
    if ($DryRun) {
        $Arguments += '--dry-run'
    }

    & $Python @Arguments
    $ExitCode = $LASTEXITCODE

    Write-Host ''
    Write-Host "Kod range-aware backfill: $ExitCode"
    Write-Host (
        'Raporty: {0}' -f
        (Join-Path $RuntimeRoot 'logs\range-backfill')
    )
    Write-Host (
        'Progress: {0}' -f
        (Join-Path $RuntimeRoot 'logs\progress\range-backfill-current.json')
    )

    if ($ExitCode -notin @(0, 4)) {
        throw "Uzupełnianie luk zakończyło się kodem $ExitCode."
    }

    if ($ExitCode -eq 4) {
        Write-Warning (
            'Proces zakończył się częściowym sukcesem. Nierozwiązane luki ' +
            'pozostają jawnie zapisane w coverage-after i nie będą pobierane ' +
            'w nieskończoność po przekroczeniu limitu prób bez poprawy.'
        )
    }
    else {
        Write-Host 'UZUPEŁNIANIE LUK ZAKOŃCZONE POPRAWNIE.' -ForegroundColor Green
    }
    exit $ExitCode
}
catch {
    Write-Error $_
    exit 1
}
