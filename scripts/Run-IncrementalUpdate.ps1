[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Required in @($Python, $Config, $EnvFile)) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    Write-Host 'LEKKA AKTUALIZACJA MODELU RESZT' -ForegroundColor Cyan
    Write-Host (
        'Najpierw weryfikuję dojrzałe prognozy, potem wykonuję partial_fit ' +
        'tylko wtedy, gdy korekta przechodzi bramę MAE.'
    ) -ForegroundColor DarkGray

    & $Python -m smog_ai verify --config $Config --env-file $EnvFile
    if ($LASTEXITCODE -notin @(0, 4)) {
        throw "verify zakończył się kodem $LASTEXITCODE."
    }

    & $Python -m smog_ai update-hourly-residuals `
        --config $Config `
        --env-file $EnvFile
    $UpdateCode = $LASTEXITCODE

    & $Python -m smog_ai hourly-drift-status `
        --config $Config `
        --env-file $EnvFile

    exit $UpdateCode
}
catch {
    Write-Error $_
    exit 1
}
