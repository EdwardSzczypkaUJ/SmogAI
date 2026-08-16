[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
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

$Files = @(
    (Join-Path $ProjectRoot 'smog_ai\training_delta.py'),
    (Join-Path $ProjectRoot 'scripts\smog_ai_training_delta.py'),
    (Join-Path $ProjectRoot 'scripts\smog_ai_layered_training_trial.py')
)
foreach ($File in $Files) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "Missing file: $File" }
}

& $Python -m py_compile @Files
if ($LASTEXITCODE -ne 0) { throw 'Python syntax verification failed.' }

# Execute the real launcher import path. A syntax-only test did not catch
# ProjectRoot being absent from sys.path for scripts\*.py execution.
& $Python $Files[1] --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Training delta launcher import verification failed.' }
& $Python $Files[2] --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Layered trial launcher import verification failed.' }

$Task = Get-ScheduledTask -TaskPath '\SmogAI\' -TaskName 'SmogAI-HF21-Refresh-6h' -ErrorAction SilentlyContinue
[pscustomobject]@{
    PythonSyntax = $true
    DeltaModule = Test-Path -LiteralPath $Files[0]
    DeltaCommand = Test-Path -LiteralPath $Files[1]
    LauncherImport = $true
    LayeredTrialCommand = Test-Path -LiteralPath $Files[2]
    LayeredTrialImport = $true
    ActivePointer = Test-Path -LiteralPath (Join-Path $RuntimeRoot 'training-datasets\quick\latest.json')
    LiveDatabase = Test-Path -LiteralPath (Join-Path $RuntimeRoot 'data\smog.db')
    TaskState = if ($Task) { [string]$Task.State } else { 'NotFound' }
    ProductionPointerModifiedByTest = $false
} | Format-List

Write-Host 'Training delta hotfix verification completed.' -ForegroundColor Green
