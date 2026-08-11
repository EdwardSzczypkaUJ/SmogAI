[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [ValidateSet('quick', 'full', 'all')]
    [string]$Profile = 'all',

    [switch]$VerifyChecksum
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

    $Arguments = @(
        '-m', 'smog_ai', 'training-snapshot-status',
        '--config', $Config,
        '--env-file', $EnvFile
    )
    if ($Profile -ne 'all') {
        $Arguments += @('--profile', $Profile)
    }
    if ($VerifyChecksum) {
        $Arguments += '--verify-checksum'
    }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    & $Python @Arguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
