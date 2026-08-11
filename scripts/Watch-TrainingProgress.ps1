[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [ValidateSet('quick', 'full', 'incremental')]
    [string]$Mode = 'quick',

    [ValidateRange(1, 300)]
    [int]$RefreshSeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    $RunType = switch ($Mode) {
        'quick' { 'snapshot-train-hourly-quick' }
        'full' { 'snapshot-train-hourly-full' }
        'incremental' { 'update-hourly-residuals' }
    }

    & $Python -m smog_ai progress `
        --watch `
        --run-type $RunType `
        --refresh-seconds $RefreshSeconds `
        --config $Config `
        --env-file $EnvFile

    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
