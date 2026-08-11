[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [ValidateRange(1, 65535)]
    [int]$Port = 5000,

    [switch]$IncludeMainConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Helper = Join-Path $ProjectRoot 'scripts\enable_local_mlflow.py'
    $ReportRoot = Join-Path $RuntimeRoot 'reports\hf20'
    New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Helper
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Configs = New-Object 'System.Collections.Generic.List[string]'
    $LocalConfig = Join-Path $RuntimeRoot 'config.local-training.yaml'
    if (Test-Path -LiteralPath $LocalConfig -PathType Leaf) {
        [void]$Configs.Add($LocalConfig)
    }
    if ($IncludeMainConfig) {
        $MainConfig = Join-Path $RuntimeRoot 'config.yaml'
        if (Test-Path -LiteralPath $MainConfig -PathType Leaf) {
            [void]$Configs.Add($MainConfig)
        }
    }
    if ($Configs.Count -eq 0) {
        throw 'Nie znaleziono konfiguracji do aktualizacji.'
    }

    $TrackingUri = "http://127.0.0.1:$Port"
    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $ReportPath = Join-Path $ReportRoot "mlflow-local-$Stamp.json"

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    $Arguments = New-Object 'System.Collections.Generic.List[string]'
    foreach ($Value in @(
        $Helper,
        '--project-root', $ProjectRoot,
        '--runtime-root', $RuntimeRoot,
        '--tracking-uri', $TrackingUri,
        '--report-path', $ReportPath
    )) {
        [void]$Arguments.Add([string]$Value)
    }
    foreach ($ConfigPath in $Configs.ToArray()) {
        [void]$Arguments.Add('--config')
        [void]$Arguments.Add($ConfigPath)
    }

    & $Python @($Arguments.ToArray())
    if ($LASTEXITCODE -ne 0) {
        throw "Aktualizacja konfiguracji MLflow zakonczyla sie kodem $LASTEXITCODE."
    }

    Write-Host ''
    Write-Host 'LOKALNY MLFLOW WLACZONY W KONFIGURACJI.' -ForegroundColor Green
    Write-Host "UI:     $TrackingUri"
    Write-Host "Raport: $ReportPath"
    Write-Host (
        'Porownanie modeli pozostaje lokalne; brak publikacji do Spaces.'
    ) -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
