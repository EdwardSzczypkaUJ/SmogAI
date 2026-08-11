[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,

    [ValidateRange(2000, 2200)]
    [int]$StartYear = 2022,

    [ValidateRange(2000, 2200)]
    [int]$EndYear = 2024,

    [ValidateSet('auto', 'prepared', 'api')]
    [string]$Source = 'prepared',

    [string]$Pollutants = 'PM10,PM2.5',
    [string]$Voivodeships = 'ALL',

    [ValidateRange(30.0, 3600.0)]
    [double]$RequestIntervalSeconds = 31.0,

    [ValidateRange(1, 500)]
    [int]$PageSize = 500,

    [ValidateRange(0, 100000)]
    [int]$MaxPagesPerCombination = 0,

    [ValidateRange(100, 100000)]
    [int]$InsertBatchSize = 20000,

    [string]$CacheDir,

    [ValidateSet('local', 'object_store', 'hybrid')]
    [string]$CacheMode = 'local',

    [string]$CachePrefix = 'source-cache/gios-history',

    [switch]$NoResume,
    [switch]$RefreshCache,
    [switch]$SkipBackup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Required in @($PythonExe, $Config, $EnvFile)) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }
    if ($EndYear -lt $StartYear) {
        throw '-EndYear nie może być mniejszy niż -StartYear.'
    }

    Import-SmogAiEnvFile -Path $EnvFile
    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    $env:SMOG_AI_CONFIG = $Config
    $env:SMOG_AI_ENV_FILE = $EnvFile
    Set-Location -LiteralPath $ProjectRoot

    $Version = (& $PythonExe -c "import smog_ai; print(smog_ai.__version__)").Trim()
    if ($Version -ne '1.7.0') {
        throw "Wymagana wersja Smog AI 1.7.0, uruchomiono: $Version"
    }

    $LogDirectory = Join-Path $RuntimeRoot 'logs\historical-gios'
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $LogFile = Join-Path $LogDirectory "gios-history-$StartYear-$EndYear-$Source-$Stamp.log"

    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host 'GIOŚ — HISTORYCZNE PM10/PM2.5' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host "Projekt:       $ProjectRoot"
    Write-Host "Runtime:       $RuntimeRoot"
    Write-Host "Lata:          $StartYear-$EndYear"
    Write-Host "Źródło:        $Source"
    Write-Host "Zaniecz.:      $Pollutants"
    Write-Host "Województwa:  $Voivodeships"
    Write-Host "Cache:         $CacheDir"
    Write-Host "Cache mode:    $CacheMode"
    Write-Host "Cache prefix:  $CachePrefix"
    Write-Host "Log:           $LogFile"

    if (-not $SkipBackup) {
        Write-Host ''
        Write-Host '[1/3] Backup SQLite przed importem...' -ForegroundColor Cyan
        & $PythonExe -m smog_ai backup `
            --tier daily `
            --config $Config `
            --env-file $EnvFile
        if ($LASTEXITCODE -ne 0) {
            throw "Backup zakończył się kodem $LASTEXITCODE."
        }
    }

    $IntervalText = $RequestIntervalSeconds.ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    )

    $Arguments = @(
        '-m', 'smog_ai', 'backfill-gios-history',
        '--start-year', [string]$StartYear,
        '--end-year', [string]$EndYear,
        '--source', $Source,
        '--pollutants', $Pollutants,
        '--voivodeships', $Voivodeships,
        '--request-interval-seconds', $IntervalText,
        '--page-size', [string]$PageSize,
        '--max-pages-per-combination', [string]$MaxPagesPerCombination,
        '--insert-batch-size', [string]$InsertBatchSize,
        '--cache-mode', $CacheMode,
        '--cache-prefix', $CachePrefix,
        '--config', $Config,
        '--env-file', $EnvFile
    )
    if ($NoResume) { $Arguments += '--no-resume' }
    if ($RefreshCache) { $Arguments += '--refresh-cache' }
    if ($CacheDir) {
        $Arguments += @('--cache-dir', [System.IO.Path]::GetFullPath($CacheDir))
    }

    Write-Host ''
    Write-Host '[2/3] Import historii GIOŚ...' -ForegroundColor Cyan
    $OldPreference = $ErrorActionPreference
    $ImportCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExe @Arguments 2>&1 |
            Tee-Object -FilePath $LogFile
        $ImportCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $OldPreference
    }

    Write-Host ''
    Write-Host "Kod importu: $ImportCode"
    Write-Host "Log: $LogFile"
    if ($ImportCode -notin @(0, 4)) {
        Get-Content -LiteralPath $LogFile -Tail 150 -ErrorAction SilentlyContinue
        throw "Import historii GIOŚ zakończył się kodem $ImportCode."
    }

    Write-Host ''
    Write-Host '[3/3] Audyt pokrycia historii PM...' -ForegroundColor Cyan
    & $PythonExe -m smog_ai gios-history-status `
        --config $Config `
        --env-file $EnvFile
    if ($LASTEXITCODE -ne 0) {
        throw "gios-history-status zakończył się kodem $LASTEXITCODE."
    }

    if ($ImportCode -eq 4) {
        Write-Warning 'Import zakończył się częściowym sukcesem. Zobacz pełny log powyżej.'
        Get-Content -LiteralPath $LogFile -Tail 150 -ErrorAction SilentlyContinue
        exit 4
    }

    Write-Host ''
    Write-Host 'IMPORT HISTORYCZNYCH DANYCH GIOŚ ZAKOŃCZONY POPRAWNIE.' -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
