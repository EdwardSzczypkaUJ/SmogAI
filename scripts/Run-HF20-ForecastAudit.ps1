[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [switch]$SkipPrediction
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.local-training.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.local-training.env'
    $GuardFile = Join-Path $RuntimeRoot 'local-only-training-guard.json'
    $ReportRoot = Join-Path $RuntimeRoot 'reports\stage2-stage3'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Config,
        $EnvFile,
        $GuardFile
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Guard = Get-Content -LiteralPath $GuardFile -Raw | ConvertFrom-Json
    $Failed = @(
        $Guard.checks.PSObject.Properties |
            Where-Object { $_.Value -ne $true }
    )
    if ($Failed.Count -gt 0) {
        $Failed | Select-Object Name, Value | Format-Table -AutoSize
        throw 'Konfiguracja local-only nie przeszla kontroli.'
    }

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:SMOG_AI_CONFIG = $Config
    $env:SMOG_AI_ENV_FILE = $EnvFile
    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    $env:SMOG_AI_DATA_FLOW_MODE = 'direct_local'
    $env:SMOG_AI_DATA_FLOW_MIRROR_OPERATIONAL = 'false'
    $env:SMOG_AI_TRAINING_SNAPSHOT_MIRROR_MANIFEST = 'false'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    if (-not $SkipPrediction) {
        Write-Host (
            '=== Lokalna predykcja HF20: 48 przyszlych godzin ==='
        ) -ForegroundColor Cyan

        & $Python -m smog_ai predict-hourly `
            --config $Config `
            --env-file $EnvFile

        if ($LASTEXITCODE -ne 0) {
            throw "predict-hourly zakonczyl sie kodem $LASTEXITCODE."
        }
    }

    Write-Host ''
    Write-Host (
        '=== Audyt serving lead 1-48 / model horizon 1-60 ==='
    ) -ForegroundColor Cyan

    & $Python -m smog_ai audit-hourly-serving-contract `
        --config $Config `
        --env-file $EnvFile

    $AuditCode = $LASTEXITCODE

    $AuditFile = Get-ChildItem `
        -LiteralPath $ReportRoot `
        -Filter 'hourly-serving-contract-*.json' `
        -File `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime |
        Select-Object -Last 1

    if (-not $AuditFile) {
        throw 'Audyt nie utworzyl raportu hourly-serving-contract-*.json.'
    }

    $Audit = Get-Content `
        -LiteralPath $AuditFile.FullName `
        -Raw |
        ConvertFrom-Json

    $HardFailures = @($Audit.hard_failures)
    $QualityFailures = @($Audit.quality_failures)

    Write-Host ''
    Write-Host "Raport: $($AuditFile.FullName)"
    Write-Host "Kod audytu HF20: $AuditCode"
    Write-Host (
        "Serving contract passed: $($Audit.serving_contract_passed)"
    )
    Write-Host "Publication ready:       $($Audit.publication_ready)"
    Write-Host "Decision:                $($Audit.decision)"
    Write-Host (
        "Approved targets:         $(@($Audit.approved_targets) -join ', ')"
    )
    Write-Host (
        "Experimental targets:     $(@($Audit.experimental_targets) -join ', ')"
    )

    if ($HardFailures.Count -gt 0) {
        Write-Host ''
        Write-Host (
            'STOP: audyt wykryl twarde problemy kontraktu technicznego.'
        ) -ForegroundColor Red
        $HardFailures | ConvertTo-Json -Depth 30
    }
    elseif ($QualityFailures.Count -gt 0) {
        Write-Host ''
        Write-Warning (
            'Kontrakt techniczny przeszedl. Co najmniej jeden model ' +
            'pozostaje eksperymentalny i nie moze byc opublikowany.'
        )
        $QualityFailures | ConvertTo-Json -Depth 30
        Write-Host ''
        Write-Host (
            'Mozna kontynuowac lokalny Stage 3 wyłącznie z Approved targets.'
        ) -ForegroundColor Green
    }
    else {
        Write-Host ''
        Write-Host (
            'Kontrakt techniczny i wszystkie bramy jakości przeszly.'
        ) -ForegroundColor Green
    }

    Write-Host ''
    Write-Host 'Nie wykonano uploadu ani publikacji.' -ForegroundColor Cyan
    exit $AuditCode
}
catch {
    Write-Error $_
    exit 1
}
