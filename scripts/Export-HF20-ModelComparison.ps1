[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.local-training.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.local-training.env'

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

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    & $Python -m smog_ai export-model-comparison `
        --no-publish `
        --config $Config `
        --env-file $EnvFile

    if ($LASTEXITCODE -ne 0) {
        throw "Eksport porownania modeli zakonczyl sie kodem $LASTEXITCODE."
    }

    Write-Host ''
    Write-Host 'POROWNANIE MODELI ZAPISANO WYŁĄCZNIE LOKALNIE.' -ForegroundColor Green
    Write-Host (Join-Path $RuntimeRoot 'reports\mlflow\model-comparison.json')
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
