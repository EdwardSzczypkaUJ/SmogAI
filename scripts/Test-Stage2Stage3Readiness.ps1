[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [switch]$VerifySnapshotChecksum,

    [switch]$SkipObjectStore,

    [switch]$StrictArtifacts
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Script = Join-Path $ProjectRoot 'scripts\stage2_stage3_preflight.py'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Script,
        $Config,
        $EnvFile
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Arguments = @(
        $Script,
        '--project-root', $ProjectRoot,
        '--runtime-root', $RuntimeRoot,
        '--config', $Config,
        '--env-file', $EnvFile
    )
    if ($VerifySnapshotChecksum) {
        $Arguments += '--verify-snapshot-checksum'
    }
    if ($SkipObjectStore) {
        $Arguments += '--skip-object-store'
    }
    if ($StrictArtifacts) {
        $Arguments += '--strict-artifacts'
    }

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
