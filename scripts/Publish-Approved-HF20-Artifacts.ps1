[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [Parameter(Mandatory = $true)]
    [string]$Targets,

    [switch]$IApproveDigitalOceanUpload,

    [switch]$PublishComparison,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
    $Selected = @(
        $Targets -split '[,;]' |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    if ($Selected.Count -eq 0) { throw 'Brak celow do publikacji.' }

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

    Write-Host 'PLAN PUBLIKACJI HF20' -ForegroundColor Cyan
    Write-Host "Cele: $($Selected -join ', ')"
    Write-Host 'Dozwolone: model binary, model card, metrics, active pointer, comparison.'
    Write-Host 'Zabronione: raw data, SQLite, snapshot, training frames, source cache.' -ForegroundColor Yellow

    if ($DryRun) {
        Write-Host 'DRY RUN: nic nie zostanie wyslane.' -ForegroundColor Green
        exit 0
    }
    if (-not $IApproveDigitalOceanUpload) {
        throw (
            'STOP: brak jawnej zgody. Ponow polecenie dopiero po swiadomej ' +
            'decyzji z -IApproveDigitalOceanUpload.'
        )
    }

    $Confirm = 'PUBLISH APPROVED MODELS ONLY'
    $TypedConfirmation = Read-Host (
        'Wpisz dokladnie: ' + $Confirm
    )
    if ($TypedConfirmation -cne $Confirm) {
        throw 'STOP: fraza zgody jest niezgodna. Nic nie wyslano.'
    }

    $Arguments = @(
        '-m', 'smog_ai', 'publish-approved-models',
        '--targets', ($Selected -join ','),
        '--confirmation', $Confirm
    )
    if ($PublishComparison) {
        $Arguments += '--publish-comparison'
    }
    else {
        $Arguments += '--no-publish-comparison'
    }
    $Arguments += @(
        '--config', $Config,
        '--env-file', $EnvFile
    )
    & $Python @Arguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
