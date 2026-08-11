[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [Parameter(Mandatory = $true)]
    [string]$Parameters,

    [switch]$AllowPersistence,

    [switch]$AllowBootstrap,

    [switch]$AllowLiveDataset
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Script = Join-Path $ProjectRoot 'scripts\stage2_model_quality_gate.py'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    $Arguments = @(
        $Script,
        '--runtime-root', $RuntimeRoot,
        '--config', $Config,
        '--env-file', $EnvFile,
        '--parameters', $Parameters
    )
    if ($AllowPersistence) { $Arguments += '--allow-persistence' }
    if ($AllowBootstrap) { $Arguments += '--allow-bootstrap' }
    if ($AllowLiveDataset) { $Arguments += '--allow-live-dataset' }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Set-Location -LiteralPath $ProjectRoot
    & $Python @Arguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}
