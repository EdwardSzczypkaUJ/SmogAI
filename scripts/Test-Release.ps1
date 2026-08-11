[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$OutputPath,
    [switch]$SkipTests,
    [switch]$SkipWheel
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    if (-not $OutputPath) {
        $OutputPath = Join-Path $ProjectRoot 'release-verification.json'
    }
    $Arguments = @(
        (Join-Path $ProjectRoot 'scripts\verify_release.py'),
        '--project-root', $ProjectRoot,
        '--output', $OutputPath
    )
    if ($SkipTests) { $Arguments += '--skip-tests' }
    if ($SkipWheel) { $Arguments += '--skip-wheel' }
    Set-Location -LiteralPath $ProjectRoot
    & $PythonExe @Arguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
