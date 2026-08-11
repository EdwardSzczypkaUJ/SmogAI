[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [ValidateRange(1, 65535)]
    [int]$Port = 5000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $MlflowRoot = Join-Path $RuntimeRoot 'mlflow'
    $DatabasePath = Join-Path $MlflowRoot 'mlflow.db'
    $ArtifactRoot = Join-Path $MlflowRoot 'artifacts'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }
    New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null
    Set-Location -LiteralPath $ProjectRoot

    & $Python -c "import mlflow; print(mlflow.__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw (
            "MLflow nie jest zainstalowany. Uruchom najpierw " +
            "scripts\Install-HF20-OptionalTools.ps1 -InstallMLflow."
        )
    }

    $DatabaseUri = 'sqlite:///' + ($DatabasePath.Replace('\', '/'))
    $ArtifactUri = 'file:///' + ($ArtifactRoot.Replace('\', '/'))
    Write-Host "MLflow UI: http://127.0.0.1:$Port" -ForegroundColor Cyan
    Write-Host "Backend:   $DatabaseUri"
    Write-Host "Artefakty: $ArtifactUri"
    Write-Host 'Ctrl+C zatrzymuje tylko lokalny serwer MLflow.' -ForegroundColor DarkGray

    & $Python -m mlflow server `
        --host 127.0.0.1 `
        --port $Port `
        --backend-store-uri $DatabaseUri `
        --default-artifact-root $ArtifactUri

    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
