[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [double]$MinimumAgeHours = 1.0,
    [switch]$Quarantine
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Script = Join-Path $PSScriptRoot 'smog_ai_snapshot_quarantine.py'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Missing Python: $Python" }
if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) { throw "Missing script: $Script" }

$Arguments = @(
    $Script,
    '--runtime-root', $RuntimeRoot,
    '--minimum-age-hours', [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $MinimumAgeHours)
)
if ($Quarantine) {
    if (-not $PSCmdlet.ShouldProcess(
        "$RuntimeRoot\training-datasets",
        'Atomically move unreferenced manifest-less datasets to quarantine'
    )) { return }
    $Arguments += '--quarantine'
} else {
    Write-Host 'PLAN ONLY - no file will be moved or deleted.' -ForegroundColor Yellow
}

& $Python @Arguments
if ($LASTEXITCODE -ne 0) { throw "Snapshot quarantine tool failed: $LASTEXITCODE" }
