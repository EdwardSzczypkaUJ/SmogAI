[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,

    [ValidateRange(2000, 2200)]
    [int]$StartYear = 2022,

    [ValidateRange(2000, 2200)]
    [int]$EndYear = 2024,

    [ValidateSet('prepared', 'api', 'auto')]
    [string]$Source = 'prepared',

    [ValidateSet('local', 'object_store', 'hybrid')]
    [string]$CacheMode = 'local',

    [string]$CachePrefix = 'source-cache/gios-history',

    [ValidateRange(100, 100000)]
    [int]$InsertBatchSize = 5000,

    [switch]$SkipBackup,
    [switch]$UseResume,
    [switch]$RefreshCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Invoke-PythonLogged {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$LogFile
    )

    $OldPreference = $ErrorActionPreference
    $Code = 1
    try {
        # Python logging is directed to stdout by HF15.  The stderr merge still
        # captures genuine tracebacks.  Tee-Object streams live output and
        # writes a useful log, unlike Start-Transcript in Windows PowerShell 5.1.
        $ErrorActionPreference = 'Continue'
        & $PythonExe @Arguments 2>&1 |
            Tee-Object -FilePath $LogFile
        $Code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $OldPreference
    }
    return [int]$Code
}

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
        throw "Wymagana wersja 1.7.0, uruchomiono: $Version"
    }

    $LogRoot = Join-Path $RuntimeRoot 'logs\historical-gios\pm25-only'
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host 'GIOŚ — IMPORT TYLKO PM2.5' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host "Projekt:    $ProjectRoot"
    Write-Host "Runtime:    $RuntimeRoot"
    Write-Host "Lata:       $StartYear-$EndYear"
    Write-Host "Źródło:     $Source"
    Write-Host "Cache mode: $CacheMode"
    Write-Host "Cache pref: $CachePrefix"

    if (-not $SkipBackup) {
        Write-Host ''
        Write-Host '[1/3] Backup SQLite...' -ForegroundColor Cyan
        & $PythonExe -m smog_ai backup `
            --tier daily `
            --config $Config `
            --env-file $EnvFile
        if ($LASTEXITCODE -ne 0) {
            throw "Backup zakończył się kodem $LASTEXITCODE."
        }
    }

    $FailedYears = New-Object 'System.Collections.Generic.List[int]'
    $Years = @($StartYear..$EndYear)
    $YearIndex = 0

    foreach ($Year in $Years) {
        $YearIndex += 1
        Write-Progress `
            -Activity 'Import historycznego PM2.5 z GIOŚ' `
            -Status "Rok ${Year} ($YearIndex z $($Years.Count))" `
            -PercentComplete ([int](100 * ($YearIndex - 1) / [Math]::Max(1, $Years.Count)))

        Write-Host ''
        Write-Host "PM2.5 — ROK ${Year}:" -ForegroundColor Cyan

        $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $LogFile = Join-Path $LogRoot (
            "pm25-$Year-$Source-$CacheMode-$Stamp.log"
        )

        $Arguments = @(
            '-m', 'smog_ai', 'backfill-gios-history',
            '--start-year', [string]$Year,
            '--end-year', [string]$Year,
            '--source', $Source,
            '--pollutants', 'PM2.5',
            '--voivodeships', 'ALL',
            '--cache-mode', $CacheMode,
            '--cache-prefix', $CachePrefix,
            '--insert-batch-size', [string]$InsertBatchSize,
            '--config', $Config,
            '--env-file', $EnvFile
        )
        if (-not $UseResume) { $Arguments += '--no-resume' }
        if ($RefreshCache) { $Arguments += '--refresh-cache' }

        $Code = Invoke-PythonLogged `
            -PythonExe $PythonExe `
            -Arguments $Arguments `
            -LogFile $LogFile

        Write-Host "Kod roku ${Year}: $Code"
        Write-Host "Log: $LogFile"

        if ($Code -notin @(0, 4)) {
            [void]$FailedYears.Add($Year)
            Write-Warning "Import roku ${Year} zakończył się kodem $Code."
            Get-Content -LiteralPath $LogFile -Tail 100 -ErrorAction SilentlyContinue
            break
        }

        & $PythonExe -m smog_ai gios-history-status `
            --config $Config `
            --env-file $EnvFile

        if ($Code -eq 4) {
            [void]$FailedYears.Add($Year)
            Write-Warning "Rok ${Year} zakończył się częściowo. Szczegóły są w logu."
            Get-Content -LiteralPath $LogFile -Tail 150 -ErrorAction SilentlyContinue
            break
        }
    }

    Write-Progress -Activity 'Import historycznego PM2.5 z GIOŚ' -Completed

    if ($FailedYears.Count -gt 0) {
        Write-Warning (
            'Import PM2.5 zakończył się częściowo. Pierwszy problematyczny rok: ' +
            $FailedYears[0]
        )
        exit 4
    }

    Write-Host ''
    Write-Host 'IMPORT TYLKO PM2.5 ZAKOŃCZONY POPRAWNIE.' -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
