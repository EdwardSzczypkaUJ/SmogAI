[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',
    [ValidateSet('plan', 'build', 'verify')]
    [string]$Action = 'plan',
    [switch]$Apply,
    [string]$Confirmation = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Resolve-Python {
    param([string]$Root)
    foreach ($Candidate in @(
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe')
    )) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) { return $Candidate }
    }
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $Command) { $Command = Get-Command python -ErrorAction SilentlyContinue }
    if ($Command) { return $Command.Source }
    throw 'Python was not found.'
}

foreach ($Path in @($ProjectRoot, $RuntimeRoot)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Directory does not exist: $Path"
    }
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RuntimeRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$Python = Resolve-Python -Root $ProjectRoot
$Tool = Join-Path $ProjectRoot 'scripts\smog_ai_training_delta.py'
if (-not (Test-Path -LiteralPath $Tool -PathType Leaf)) {
    throw "Delta tool is missing: $Tool"
}

if ($Apply) {
    if ($Action -ne 'build') { throw '-Apply is valid only with -Action build.' }
    if ($Confirmation -cne 'BUILD VERIFIED TRAINING DELTA') {
        throw 'Exact confirmation is required: BUILD VERIFIED TRAINING DELTA'
    }
    $Task = Get-ScheduledTask -TaskPath '\SmogAI\' -TaskName 'SmogAI-HF21-Refresh-6h' -ErrorAction SilentlyContinue
    if ($Task -and [string]$Task.State -ne 'Disabled') {
        throw 'The SmogAI scheduled task must be Disabled before building a delta.'
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
        throw 'A SmogAI collection, training, snapshot or automation process is active.'
    }
}

$Arguments = @(
    $Tool,
    '--runtime-root', $RuntimeRoot,
    '--profile', $Profile,
    '--action', $Action
)
if ($Apply) {
    $Arguments += @('--apply', '--confirmation', $Confirmation)
}

Push-Location $ProjectRoot
try {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Training delta tool failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
