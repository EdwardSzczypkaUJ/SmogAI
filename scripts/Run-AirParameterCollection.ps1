[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$Parameters = 'ALL'
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
    Write-Host (
        "Pobieranie bieżących parametrów GIOŚ: $Parameters"
    ) -ForegroundColor Cyan
    Write-Host (
        'Postęp w drugim terminalu: python -m smog_ai progress ' +
        '--run-type collect-gios --watch ...'
    ) -ForegroundColor DarkGray

    & $Python -m smog_ai collect-gios `
        --parameters $Parameters `
        --config $Config `
        --env-file $EnvFile

    $Code = $LASTEXITCODE
    if ($Code -notin @(0, 4)) {
        throw "collect-gios zakończył się kodem $Code."
    }
    if ($Code -eq 4) {
        Write-Warning (
            'Kolekcja zakończyła się częściowym sukcesem; sprawdź raport błędów.'
        )
    }

    & (Join-Path $ProjectRoot 'scripts\Show-AirParameterCatalog.ps1') `
        -ProjectRoot $ProjectRoot `
        -RuntimeRoot $RuntimeRoot

    exit $Code
}
catch {
    Write-Error $_
    exit 1
}
