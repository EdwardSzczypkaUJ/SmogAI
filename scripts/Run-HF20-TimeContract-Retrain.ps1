[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$Targets = 'PM10,PM2.5,temperature_c,precipitation_mm',

    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',

    [ValidateSet('latest')]
    [string]$Snapshot = 'latest',

    [switch]$SkipPrediction,

    [ValidateSet('ask', 'required', 'auto', 'disabled')]
    [string]$MlflowPolicy = 'ask',

    [ValidateRange(1, 30)]
    [int]$MlflowTimeoutSeconds = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'


function Invoke-SmogAi {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )

    Write-Host ''
    Write-Host ("=== {0} ===" -f $Label) -ForegroundColor Cyan

    & $script:Python @Arguments
    $Code = $LASTEXITCODE

    Write-Host ("Kod: {0}" -f $Code)

    if ($Code -notin $AllowedExitCodes) {
        throw (
            "Etap '$Label' zakonczyl sie kodem $Code. " +
            "Dozwolone: $($AllowedExitCodes -join ', ')."
        )
    }
    return $Code
}


function Get-MlflowPreflight {
    $Output = (
        & $script:Python $script:MlflowPreflight `
            --project-root $script:ProjectRoot `
            --config $script:Config `
            --env-file $script:EnvFile `
            --timeout-seconds $MlflowTimeoutSeconds |
        Out-String
    ).Trim()
    $Code = $LASTEXITCODE

    if (-not $Output) {
        throw 'Preflight MLflow nie zwrócił JSON.'
    }

    return [pscustomobject]@{
        Code = $Code
        Report = ($Output | ConvertFrom-Json)
    }
}


function Show-MlflowPreflight {
    param([Parameter(Mandatory = $true)]$Preflight)

    $Report = $Preflight.Report

    Write-Host ''
    Write-Host '=== MLFLOW PREFLIGHT ===' -ForegroundColor Cyan
    Write-Host "Status:       $($Report.status)"
    Write-Host "Włączony:     $($Report.enabled)"
    Write-Host "Zainstalowany:$($Report.installed)"
    Write-Host "Osiągalny:    $($Report.reachable)"
    Write-Host "Strict:       $($Report.strict)"
    Write-Host "Tracking URI: $($Report.tracking_uri)"
    Write-Host "Szczegóły:    $($Report.detail)"
}


function Disable-MlflowForThisRun {
    $env:SMOG_AI_MLFLOW_ENABLED = 'false'
    $env:SMOG_AI_MLFLOW_TRACKING_URI = ''
    $env:SMOG_AI_MLFLOW_UI_URL = ''

    Write-Host ''
    Write-Host (
        'MLflow WYŁĄCZONY WYŁĄCZNIE DLA TEGO PRZEBIEGU. ' +
        'Trening i model-comparison.json będą kontynuowane lokalnie.'
    ) -ForegroundColor Yellow
}


function Resolve-MlflowMode {
    if ($MlflowPolicy -eq 'disabled') {
        Disable-MlflowForThisRun
        return 'disabled'
    }

    while ($true) {
        $Preflight = Get-MlflowPreflight
        Show-MlflowPreflight -Preflight $Preflight
        $Status = [string]$Preflight.Report.status

        if ($Status -eq 'ready') {
            Write-Host 'MLflow gotowy — trening będzie śledzony.' -ForegroundColor Green
            return 'mlflow'
        }

        if ($Status -eq 'disabled') {
            if ($MlflowPolicy -eq 'required') {
                Write-Host (
                    'MLflow jest wymagany, ale wyłączony w konfiguracji. ' +
                    'Trening NIE ZOSTAŁ rozpoczęty.'
                ) -ForegroundColor Red
                return 'abort'
            }

            Write-Host (
                'MLflow jest wyłączony — trening będzie kontynuowany bez MLflow.'
            ) -ForegroundColor Yellow
            Disable-MlflowForThisRun
            return 'disabled'
        }

        if ($MlflowPolicy -eq 'required') {
            if ($Status -eq 'not_running') {
                Write-Host (
                    'MLflow jest włączony, ale SERWER NIE JEST URUCHOMIONY. ' +
                    'Trening NIE ZOSTAŁ rozpoczęty.'
                ) -ForegroundColor Red
            }
            elseif ($Status -eq 'not_installed') {
                Write-Host (
                    'Pakiet MLflow nie jest zainstalowany. ' +
                    'Trening NIE ZOSTAŁ rozpoczęty.'
                ) -ForegroundColor Red
            }
            else {
                Write-Host (
                    'MLflow nie przeszedł preflightu. ' +
                    'Trening NIE ZOSTAŁ rozpoczęty.'
                ) -ForegroundColor Red
            }
            return 'abort'
        }

        if ($MlflowPolicy -eq 'auto') {
            Write-Host (
                'MLflow niedostępny — polityka auto kontynuuje BEZ MLflow.'
            ) -ForegroundColor Yellow
            Disable-MlflowForThisRun
            return 'disabled'
        }

        Write-Host ''
        Write-Host 'Wybierz dalsze działanie:' -ForegroundColor Cyan
        Write-Host '  [P] Uruchomiłem MLflow w drugim oknie — PONÓW test'
        Write-Host '  [B] Kontynuuj ten trening BEZ MLflow'
        Write-Host '  [K] Zakończ bez rozpoczynania treningu'

        $Choice = (Read-Host 'Wpisz P, B albo K').Trim().ToUpperInvariant()

        switch ($Choice) {
            'P' {
                continue
            }
            'B' {
                Disable-MlflowForThisRun
                return 'disabled'
            }
            'K' {
                Write-Host 'Trening nie został rozpoczęty.' -ForegroundColor Yellow
                return 'abort'
            }
            default {
                Write-Host 'Nieprawidłowy wybór.' -ForegroundColor Yellow
            }
        }
    }
}


function Invoke-Main {
    $script:ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $Runtime = [System.IO.Path]::GetFullPath($RuntimeRoot)

    $script:Python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    $script:Config = Join-Path $Runtime 'config.local-training.yaml'
    $script:EnvFile = Join-Path $Runtime 'smog-ai.local-training.env'
    $GuardFile = Join-Path $Runtime 'local-only-training-guard.json'
    $QualityGate = Join-Path $script:ProjectRoot 'scripts\stage2_model_quality_gate.py'
    $AuditScript = Join-Path $script:ProjectRoot 'scripts\hf20_post_training_audit.py'
    $script:MlflowPreflight = Join-Path $script:ProjectRoot 'scripts\mlflow_preflight.py'
    $ReportRoot = Join-Path $Runtime 'reports\stage2-stage3'
    $LogRoot = Join-Path $Runtime 'logs\manual'

    foreach ($Required in @(
        (Join-Path $script:ProjectRoot 'pyproject.toml'),
        $script:Python,
        $script:Config,
        $script:EnvFile,
        $GuardFile,
        $QualityGate,
        $AuditScript,
        $script:MlflowPreflight,
        (Join-Path $script:ProjectRoot '.hotfixes\TRAINING_SNAPSHOT_SELF_HEARTBEAT_HF19_2_1.7.0.json')
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Guard = Get-Content -LiteralPath $GuardFile -Raw | ConvertFrom-Json
    $FailedChecks = @(
        $Guard.checks.PSObject.Properties |
            Where-Object { $_.Value -ne $true }
    )
    if ($FailedChecks.Count -gt 0) {
        $FailedChecks | Select-Object Name, Value | Format-Table -AutoSize
        throw 'Konfiguracja local-only nie przeszla kontroli.'
    }

    New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    Set-Location -LiteralPath $script:ProjectRoot

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:SMOG_AI_PROJECT_ROOT = $script:ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $Runtime
    $env:SMOG_AI_CONFIG = $script:Config
    $env:SMOG_AI_ENV_FILE = $script:EnvFile
    $env:SMOG_AI_DATA_FLOW_MODE = 'direct_local'
    $env:SMOG_AI_DATA_FLOW_MIRROR_OPERATIONAL = 'false'
    $env:SMOG_AI_GIOS_HISTORY_CACHE_MODE = 'local'
    $env:SMOG_AI_TRAINING_SNAPSHOT_MIRROR_MANIFEST = 'false'
    $env:SMOG_AI_TRAINING_INPUT_SOURCE = 'database'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    $ConfigProbe = @'
from __future__ import annotations
import json, sys
from pathlib import Path
project_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_root))
from smog_ai.config import load_config
cfg = load_config(Path(sys.argv[2]), Path(sys.argv[3]))
payload = {
    "serving_horizon_hours": cfg.hourly_forecasting.serving_horizon_count,
    "maximum_source_delay_hours": cfg.hourly_forecasting.maximum_source_delay_hours,
    "maximum_model_horizon_hours": cfg.hourly_forecasting.model_horizon_maximum,
    "model_horizons": [
        min(cfg.hourly_forecasting.model_horizons_hours),
        max(cfg.hourly_forecasting.model_horizons_hours),
    ],
    "object_storage_enabled": cfg.object_storage.enabled,
    "upload_models": cfg.artifacts.upload_models,
    "mirror_manifest": cfg.training_snapshot.mirror_manifest_to_object_storage,
}
print(json.dumps(payload, ensure_ascii=True, indent=2))
ok = (
    payload["serving_horizon_hours"] == 48
    and payload["maximum_source_delay_hours"] == 12
    and payload["maximum_model_horizon_hours"] == 60
    and payload["model_horizons"] == [1, 60]
    and not payload["object_storage_enabled"]
    and not payload["upload_models"]
    and not payload["mirror_manifest"]
)
raise SystemExit(0 if ok else 4)
'@
    $ProbeText = (
        $ConfigProbe |
            & $script:Python - $script:ProjectRoot $script:Config $script:EnvFile |
            Out-String
    ).Trim()

    Write-Host 'HF20 CONFIG + LOCAL-ONLY GUARD' -ForegroundColor Cyan
    Write-Host $ProbeText

    if ($LASTEXITCODE -ne 0) {
        throw 'HF20 config/local-only guard nie przeszedl.'
    }

    $MlflowMode = Resolve-MlflowMode
    if ($MlflowMode -eq 'abort') {
        return 4
    }

    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $LogPath = Join-Path $LogRoot "hf20-time-contract-$Profile-$Stamp.log"
    Start-Transcript -Path $LogPath -Force | Out-Null

    $QualityCode = 0
    $AuditCode = 0

    try {
        Write-Host '============================================================' -ForegroundColor Cyan
        Write-Host 'SMOG AI HF20.2 — H1-H60 / 48 FUTURE HOURS' -ForegroundColor Cyan
        Write-Host '============================================================' -ForegroundColor Cyan
        Write-Host "Cele:          $Targets"
        Write-Host "Profil:        $Profile"
        Write-Host "Snapshot:      $Snapshot"
        Write-Host "MLflow policy: $MlflowPolicy"
        Write-Host "MLflow mode:   $MlflowMode"
        Write-Host (
            'Brak pobierania danych, tworzenia snapshotu i publikacji.'
        ) -ForegroundColor Green

        [void](Invoke-SmogAi `
            -Label "Trening $Profile na istniejacym snapshotcie h1-h60" `
            -Arguments @(
                '-m', 'smog_ai', 'snapshot-train-hourly',
                '--profile', $Profile,
                '--targets', $Targets,
                '--snapshot', $Snapshot,
                '--config', $script:Config,
                '--env-file', $script:EnvFile
            )
        )

        [void](Invoke-SmogAi `
            -Label 'Lokalny eksport porownania modeli' `
            -Arguments @(
                '-m', 'smog_ai', 'export-model-comparison',
                '--no-publish',
                '--config', $script:Config,
                '--env-file', $script:EnvFile
            )
        )

        Write-Host ''
        Write-Host '=== Twarda brama jakosci modeli ===' -ForegroundColor Cyan

        & $script:Python $QualityGate `
            --runtime-root $Runtime `
            --config $script:Config `
            --env-file $script:EnvFile `
            --parameters $Targets

        $QualityCode = $LASTEXITCODE

        if ($QualityCode -notin @(0, 4)) {
            throw "Brama jakosci zakonczyla sie kodem $QualityCode."
        }

        if (-not $SkipPrediction) {
            [void](Invoke-SmogAi `
                -Label 'Lokalna predykcja: 48 przyszlych godzin' `
                -Arguments @(
                    '-m', 'smog_ai', 'predict-hourly',
                    '--config', $script:Config,
                    '--env-file', $script:EnvFile
                )
            )

            Write-Host ''
            Write-Host '=== Audyt serving lead / model horizon ===' -ForegroundColor Cyan

            & $script:Python $AuditScript `
                --runtime-root $Runtime `
                --config $script:Config `
                --env-file $script:EnvFile `
                --parameters 'PM10,PM2.5,temperature_c,precipitation_mm,precipitation_probability'

            $AuditCode = $LASTEXITCODE

            if ($AuditCode -notin @(0, 4)) {
                throw "Audyt HF20 zakonczyl sie kodem $AuditCode."
            }
        }
    }
    finally {
        Stop-Transcript | Out-Null
    }

    $FinalCode = if ($QualityCode -eq 4 -or $AuditCode -eq 4) { 4 } else { 0 }

    Write-Host ''
    Write-Host "Log: $LogPath"

    if ($FinalCode -eq 0) {
        Write-Host (
            'HF20.2: modele i 48 przyszlych godzin przeszly bramy.'
        ) -ForegroundColor Green
    }
    else {
        Write-Warning (
            'HF20.2: co najmniej jeden model pozostaje eksperymentalny ' +
            'albo wybrano zakończenie przed treningiem.'
        )
    }

    Write-Host 'Nie wykonano zadnego uploadu ani publikacji.' -ForegroundColor Cyan
    return $FinalCode
}


$SavedEnvironment = @{}
foreach ($Name in @(
    'SMOG_AI_MLFLOW_ENABLED',
    'SMOG_AI_MLFLOW_TRACKING_URI',
    'SMOG_AI_MLFLOW_UI_URL'
)) {
    $Item = Get-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    $SavedEnvironment[$Name] = [pscustomobject]@{
        Exists = ($null -ne $Item)
        Value = if ($null -ne $Item) { [string]$Item.Value } else { $null }
    }
}

$ExitCode = 1
try {
    $ExitCode = Invoke-Main
}
catch {
    try { Stop-Transcript | Out-Null } catch { }
    Write-Error $_
    $ExitCode = 1
}
finally {
    foreach ($Name in $SavedEnvironment.Keys) {
        $Saved = $SavedEnvironment[$Name]
        if ($Saved.Exists) {
            Set-Item -LiteralPath "Env:$Name" -Value $Saved.Value
        }
        else {
            Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        }
    }
}

exit $ExitCode
