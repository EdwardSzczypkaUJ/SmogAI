[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [switch]$RetrainIfMissing
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    if (-not $RuntimeRoot) {
        $RuntimeRoot = Get-SmogAiDefaultRuntimeRoot
    }
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Path in @($PythonExe, $Config, $EnvFile)) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Path"
        }
    }

    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    $env:SMOG_AI_CONFIG = $Config
    $env:SMOG_AI_ENV_FILE = $EnvFile
    Set-Location -LiteralPath $ProjectRoot

    $LogRoot = Join-Path $RuntimeRoot 'logs\manual'
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    $LogPath = Join-Path $LogRoot (
        'resume-hourly-after-failure-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    )

    Start-Transcript -Path $LogPath -Force | Out-Null
    try {
        Write-Host '=== Preflight dokumentacji ===' -ForegroundColor Cyan
        & $PythonExe -m smog_ai documentation-preflight `
            --config $Config `
            --env-file $EnvFile
        if ($LASTEXITCODE -ne 0) {
            throw "documentation-preflight zakończył się kodem $LASTEXITCODE."
        }

        Write-Host ''
        Write-Host '=== Audyt artefaktów modeli ===' -ForegroundColor Cyan
        & $PythonExe -m smog_ai audit-hourly-models `
            --config $Config `
            --env-file $EnvFile
        $AuditCode = $LASTEXITCODE
        if ($AuditCode -notin @(0, 4)) {
            throw "audit-hourly-models zakończył się kodem $AuditCode."
        }

        Write-Host ''
        Write-Host '=== Odzyskanie / wznowienie ===' -ForegroundColor Cyan
        $Arguments = @(
            '-m', 'smog_ai', 'resume-hourly-after-failure',
            '--config', $Config,
            '--env-file', $EnvFile
        )
        if ($RetrainIfMissing) {
            $Arguments += '--retrain-if-missing'
        }
        else {
            $Arguments += '--no-retrain-if-missing'
        }

        & $PythonExe @Arguments
        $ExitCode = $LASTEXITCODE
        if ($ExitCode -ne 0) {
            Write-Warning (
                "Wznowienie zakończyło się kodem $ExitCode. " +
                "Jeżeli raport mówi retraining_required, ponów skrypt z -RetrainIfMissing."
            )
            exit $ExitCode
        }

        Write-Host ''
        Write-Host '=== Stan końcowy ===' -ForegroundColor Cyan
        & $PythonExe -m smog_ai hourly-readiness `
            --config $Config `
            --env-file $EnvFile
        & $PythonExe -m smog_ai storage-health `
            --config $Config `
            --env-file $EnvFile

        Write-Host ''
        Write-Host 'ODZYSKIWANIE / WZNOWIENIE ZAKOŃCZONE POPRAWNIE.' -ForegroundColor Green
        Write-Host "Log: $LogPath"
        exit 0
    }
    finally {
        Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
    }
}
catch {
    Write-Error $_
    exit 1
}
