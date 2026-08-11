[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [switch]$StrictArtifacts
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Validator = Join-Path $ProjectRoot 'scripts\validate_digitalocean_spec.py'
    $Preflight = Join-Path $ProjectRoot 'scripts\Test-Stage2Stage3Readiness.ps1'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        (Join-Path $ProjectRoot '.do\app.yaml'),
        (Join-Path $ProjectRoot '.do\app.dev.yaml'),
        $Python,
        $Validator,
        $Preflight
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    & $Python $Validator '.do/app.yaml'
    if ($LASTEXITCODE -ne 0) {
        throw 'Walidacja .do/app.yaml nie przeszla.'
    }
    & $Python $Validator '.do/app.dev.yaml' --allow-development
    if ($LASTEXITCODE -ne 0) {
        throw 'Walidacja .do/app.dev.yaml nie przeszla.'
    }

    $Params = @{
        ProjectRoot = $ProjectRoot
        RuntimeRoot = $RuntimeRoot
        VerifySnapshotChecksum = $true
    }
    if ($StrictArtifacts) {
        $Params['StrictArtifacts'] = $true
    }
    & $Preflight @Params
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
