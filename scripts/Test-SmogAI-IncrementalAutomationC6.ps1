[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RuntimeRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
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
    (Join-Path $ProjectRoot 'smog_ai\hourly\trainer.py'),
    (Join-Path $ProjectRoot 'smog_ai\hourly\audit.py'),
    (Join-Path $ProjectRoot 'smog_ai\quality.py'),
    (Join-Path $ProjectRoot 'smog_ai\mlops\publish.py'),
    (Join-Path $ProjectRoot 'smog_ai\cli.py'),
    (Join-Path $ProjectRoot 'scripts\smog_ai_layered_candidate_control.py'),
    (Join-Path $ProjectRoot 'scripts\smog_ai_automation.py')
)
$Watcher = Join-Path $ProjectRoot 'scripts\Watch-SmogAI-LayeredCandidateTraining.ps1'
foreach ($File in $Files) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "Missing file: $File" }
}
if (-not (Test-Path -LiteralPath $Watcher -PathType Leaf)) { throw "Missing file: $Watcher" }

& $Python -m py_compile @Files
if ($LASTEXITCODE -ne 0) { throw 'Python syntax verification failed.' }

& $Python $Files[6] --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'C5 control import verification failed.' }

$HelpText = (& $Python -m smog_ai snapshot-train-hourly --help 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'snapshot-train-hourly help failed.' }
if ($HelpText -notmatch 'candidate-only') { throw 'candidate-only option is missing.' }
if ($HelpText -notmatch 'layered') { throw 'layered selector is missing.' }

$RootHelp = (& $Python -m smog_ai --help 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'SmogAI root help failed.' }
foreach ($Command in @('training-delta-plan', 'training-delta-build', 'training-delta-preflight', 'training-delta-verify')) {
    if ($RootHelp -notmatch [regex]::Escape($Command)) { throw "Missing CLI command: $Command" }
}
$Automation = Join-Path $ProjectRoot 'scripts\smog_ai_automation.py'
$Trainer = Join-Path $ProjectRoot 'smog_ai\hourly\trainer.py'

$Task = Get-ScheduledTask -TaskPath '\SmogAI\' -TaskName 'SmogAI-HF21-Refresh-6h' -ErrorAction SilentlyContinue
[pscustomobject]@{
    PythonSyntax = $true
    LayeredSelector = $true
    CandidateOnly = $true
    FastPreflight = Select-String -LiteralPath $Files[6] -Pattern 'preflight' -Quiet
    FreshProgressGuard = Select-String -LiteralPath $Watcher -Pattern 'FreshForThisRun' -Quiet
    DeltaPlanStage = Select-String -LiteralPath $Automation -Pattern 'training-delta-plan' -Quiet
    DeltaBuildStage = Select-String -LiteralPath $Automation -Pattern 'training-delta-build' -Quiet
    DeltaPreflightStage = Select-String -LiteralPath $Automation -Pattern 'training-delta-preflight' -Quiet
    FullDeltaVerifyAvailable = $RootHelp -match 'training-delta-verify'
    LayeredScheduledTraining = Select-String -LiteralPath $Automation -Pattern '"--snapshot", "layered"' -Quiet
    ClassificationInMonitorState = Select-String -LiteralPath $Automation -Pattern 'model_classifications' -Quiet
    ApprovedOnlyActivation = Select-String -LiteralPath $Trainer -Pattern 'quality\["status"\] == "approved"' -Quiet
    CandidateOnlyMessage = Select-String -LiteralPath $Trainer -Pattern 'save local candidate model' -Quiet
    ThreeStateQuality = Select-String -LiteralPath $Files[1] -Pattern 'quality_classification' -Quiet
    ExperimentalAllowed = Select-String -LiteralPath $Files[3] -Pattern 'experimental_publication_allowed' -Quiet
    ApprovedOnlyPublication = Select-String -LiteralPath $Files[4] -Pattern 'model_not_approved' -Quiet
    TaskState = if ($Task) { [string]$Task.State } else { 'NotFound' }
    ProductionModifiedByTest = $false
} | Format-List

Write-Host 'Incremental automation C6 verification completed.' -ForegroundColor Green
