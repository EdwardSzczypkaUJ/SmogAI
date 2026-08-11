[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Target = Join-Path $ProjectRoot 'scripts\Show-AirParameterCatalog.ps1'
if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) {
    throw "Brak skryptu katalogu parametrów: $Target"
}

& $Target `
    -ProjectRoot $ProjectRoot `
    -RuntimeRoot $RuntimeRoot `
    -AsJson:$AsJson

exit $LASTEXITCODE
