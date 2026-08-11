[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,

    [ValidateSet('direct_local', 'object_store_roundtrip')]
    [string]$TrainingMode = 'direct_local',

    [ValidateSet('local', 'object_store', 'hybrid')]
    [string]$HistoryCacheMode = 'local',

    [bool]$MirrorOperationalToObjectStore = $true,

    [string]$HistoryCachePrefix = 'source-cache/gios-history'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $Pattern = '^\s*' + [regex]::Escape($Name) + '='
    $Lines = if (Test-Path -LiteralPath $Path -PathType Leaf) {
        @(Get-Content -LiteralPath $Path -Encoding UTF8)
    }
    else {
        @()
    }

    $Output = New-Object 'System.Collections.Generic.List[string]'
    $Written = $false

    foreach ($Line in $Lines) {
        if ($Line -match $Pattern) {
            if (-not $Written) {
                [void]$Output.Add("$Name=$Value")
                $Written = $true
            }
        }
        else {
            [void]$Output.Add($Line)
        }
    }

    if (-not $Written) {
        [void]$Output.Add("$Name=$Value")
    }

    $Encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines(
        $Path,
        $Output.ToArray(),
        $Encoding
    )
}

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Required in @($PythonExe, $Config, $EnvFile)) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Prefix = $HistoryCachePrefix.Trim().Trim('/')
    if (-not $Prefix) {
        throw 'HistoryCachePrefix nie może być pusty.'
    }

    $MirrorText = if ($MirrorOperationalToObjectStore) { 'true' } else { 'false' }

    Set-DotEnvValue -Path $EnvFile `
        -Name 'SMOG_AI_DATA_FLOW_MODE' `
        -Value $TrainingMode

    Set-DotEnvValue -Path $EnvFile `
        -Name 'SMOG_AI_DATA_FLOW_MIRROR_OPERATIONAL' `
        -Value $MirrorText

    Set-DotEnvValue -Path $EnvFile `
        -Name 'SMOG_AI_GIOS_HISTORY_CACHE_MODE' `
        -Value $HistoryCacheMode

    Set-DotEnvValue -Path $EnvFile `
        -Name 'SMOG_AI_GIOS_HISTORY_CACHE_PREFIX' `
        -Value $Prefix

    $env:SMOG_AI_DATA_FLOW_MODE = $TrainingMode
    $env:SMOG_AI_DATA_FLOW_MIRROR_OPERATIONAL = $MirrorText
    $env:SMOG_AI_GIOS_HISTORY_CACHE_MODE = $HistoryCacheMode
    $env:SMOG_AI_GIOS_HISTORY_CACHE_PREFIX = $Prefix

    Set-Location -LiteralPath $ProjectRoot

    Write-Host 'Konfiguracja Bridge została zapisana.' -ForegroundColor Green
    Write-Host "Training mode:      $TrainingMode"
    Write-Host "History cache mode: $HistoryCacheMode"
    Write-Host "Mirror operational: $MirrorText"
    Write-Host "Cache prefix:       $Prefix"
    Write-Host ''

    & $PythonExe -m smog_ai data-flow-status `
        --config $Config `
        --env-file $EnvFile

    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
