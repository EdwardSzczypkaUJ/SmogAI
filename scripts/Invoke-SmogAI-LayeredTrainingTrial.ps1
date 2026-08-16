[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$Config = 'C:\ProgramData\SmogAI\config.yaml',
    [string]$EnvFile = 'C:\ProgramData\SmogAI\smog-ai.env',
    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',
    [string]$Target = 'PM10',
    [string]$Algorithm = 'ridge',
    [int]$MaximumRows = 50000,
    [switch]$Apply,
    [string]$Confirmation = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

foreach ($Path in @($ProjectRoot, $RuntimeRoot, $Config, $EnvFile)) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Path does not exist: $Path" }
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RuntimeRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$Config = (Resolve-Path -LiteralPath $Config).Path
$EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path

$Python = $null
foreach ($Candidate in @(
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    (Join-Path $ProjectRoot 'venv\Scripts\python.exe')
)) {
    if (Test-Path -LiteralPath $Candidate -PathType Leaf) { $Python = $Candidate; break }
}
if (-not $Python) {
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $Command) { $Command = Get-Command python -ErrorAction SilentlyContinue }
    if ($Command) { $Python = $Command.Source }
}
if (-not $Python) { throw 'Python was not found.' }

$Tool = Join-Path $ProjectRoot 'scripts\smog_ai_layered_training_trial.py'
if (-not (Test-Path -LiteralPath $Tool -PathType Leaf)) { throw "Trial tool is missing: $Tool" }

if ($Apply) {
    if ($Confirmation -cne 'RUN ISOLATED LAYERED TRAINING TRIAL') {
        throw 'Exact confirmation is required: RUN ISOLATED LAYERED TRAINING TRIAL'
    }
    $Task = Get-ScheduledTask -TaskPath '\SmogAI\' -TaskName 'SmogAI-HF21-Refresh-6h' -ErrorAction SilentlyContinue
    if ($Task -and [string]$Task.State -ne 'Disabled') {
        throw 'The scheduled task must be Disabled for the isolated trial.'
    }
    $ProjectPattern = [regex]::Escape($ProjectRoot)
    $RuntimePattern = [regex]::Escape($RuntimeRoot)
    $Active = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^(python|pythonw)(\.exe)?$' -and $_.CommandLine -and
        ($_.CommandLine -match $ProjectPattern -or $_.CommandLine -match $RuntimePattern) -and
        ($_.CommandLine -match 'train|collect|snapshot|automation')
    })
    if ($Active.Count -gt 0) {
        $Active | Select-Object ProcessId, Name, CommandLine | Format-Table -AutoSize | Out-Host
        throw 'A collection, training, snapshot or automation process is active.'
    }
}

$Arguments = @(
    $Tool,
    '--project-root', $ProjectRoot,
    '--runtime-root', $RuntimeRoot,
    '--config', $Config,
    '--env-file', $EnvFile,
    '--profile', $Profile,
    '--target', $Target,
    '--algorithm', $Algorithm,
    '--maximum-rows', [string]$MaximumRows
)
if ($Apply) {
    $Arguments += @('--apply', '--confirmation', $Confirmation)
}

$PreviousUnbuffered = $env:PYTHONUNBUFFERED
$env:PYTHONUNBUFFERED = '1'
Push-Location $ProjectRoot
try {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Layered training trial failed with exit code $LASTEXITCODE." }
}
finally {
    Pop-Location
    $env:PYTHONUNBUFFERED = $PreviousUnbuffered
}
