[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$ConfigPath,

    [string]$EnvFile,

    [ValidateRange(1, 30)]
    [int]$TimeoutSeconds = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Helper = Join-Path $ProjectRoot 'scripts\mlflow_preflight.py'

    if (-not $ConfigPath) {
        $ConfigPath = Join-Path $RuntimeRoot 'config.local-training.yaml'
    }
    if (-not $EnvFile) {
        $EnvFile = Join-Path $RuntimeRoot 'smog-ai.local-training.env'
    }

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Helper,
        $ConfigPath,
        $EnvFile
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    $Output = (
        & $Python $Helper `
            --project-root $ProjectRoot `
            --config $ConfigPath `
            --env-file $EnvFile `
            --timeout-seconds $TimeoutSeconds |
        Out-String
    ).Trim()
    $Code = $LASTEXITCODE

    if (-not $Output) {
        throw 'Preflight MLflow nie zwrócił raportu.'
    }

    $Status = $Output | ConvertFrom-Json

    Write-Host ''
    Write-Host 'MLFLOW PREFLIGHT' -ForegroundColor Cyan
    Write-Host "Status:       $($Status.status)"
    Write-Host "Włączony:     $($Status.enabled)"
    Write-Host "Zainstalowany:$($Status.installed)"
    Write-Host "Osiągalny:    $($Status.reachable)"
    Write-Host "Strict:       $($Status.strict)"
    Write-Host "Tracking URI: $($Status.tracking_uri)"
    Write-Host "Szczegóły:    $($Status.detail)"
    Write-Host ''

    switch ([string]$Status.status) {
        'ready' {
            Write-Host 'MLflow jest uruchomiony i gotowy.' -ForegroundColor Green
        }
        'disabled' {
            Write-Host (
                'MLflow jest wyłączony. Trening może działać bez MLflow.'
            ) -ForegroundColor Yellow
        }
        'not_installed' {
            Write-Host (
                'MLflow jest włączony, ale pakiet nie jest zainstalowany.'
            ) -ForegroundColor Yellow
        }
        'not_running' {
            Write-Host (
                'MLflow jest włączony, ale serwer NIE JEST URUCHOMIONY.'
            ) -ForegroundColor Yellow
            Write-Host (
                'Uruchom scripts\Start-LocalMLflow.ps1 albo wybierz trening ' +
                'bez MLflow.'
            )
        }
        default {
            Write-Host (
                'Konfiguracja MLflow wymaga poprawy albo treningu bez MLflow.'
            ) -ForegroundColor Red
        }
    }

    exit $Code
}
catch {
    Write-Error $_
    exit 5
}
