[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [ValidateRange(1,300)][int]$RefreshSeconds = 5,
    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    if (-not $RuntimeRoot) {
        $RuntimeRoot = Get-SmogAiDefaultRuntimeRoot
    }
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
    Set-Location -LiteralPath $ProjectRoot

    $Arguments = @(
        '-m', 'smog_ai', 'progress',
        '--run-type', 'resume-hourly',
        '--watch',
        '--refresh-seconds', [string]$RefreshSeconds,
        '--config', $Config,
        '--env-file', $EnvFile
    )
    if ($AsJson) {
        $Arguments += '--json'
    }
    & $PythonExe @Arguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
