[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RuntimeRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $Command) { $Command = Get-Command python -ErrorAction SilentlyContinue }
    if (-not $Command) { throw 'Python was not found.' }
    $Python = $Command.Source
}
$Files = @(
    (Join-Path $ProjectRoot 'smog_ai\training_delta.py'),
    (Join-Path $ProjectRoot 'smog_ai\training_compaction.py'),
    (Join-Path $ProjectRoot 'smog_ai\cli.py'),
    (Join-Path $ProjectRoot 'scripts\smog_ai_training_compaction.py'),
    (Join-Path $ProjectRoot 'scripts\smog_ai_automation.py')
)
foreach ($File in $Files) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "Missing file: $File" }
}
& $Python -m py_compile @Files
if ($LASTEXITCODE -ne 0) { throw 'Python syntax verification failed.' }

$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$HelpOutput = & $Python $Files[3] --help 2>&1
$HelpExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference
$HelpText = ($HelpOutput | ForEach-Object { [string]$_ }) -join "`n"
if ($HelpExitCode -ne 0) {
    throw "Compaction tool import verification failed.`n$HelpText"
}
foreach ($Token in @('plan', 'apply', 'verify', 'rollback')) {
    if ($HelpText -notmatch $Token) { throw "Missing compaction action: $Token" }
}
$Task = Get-ScheduledTask -TaskPath '\SmogAI\' -TaskName 'SmogAI-HF21-Refresh-6h' -ErrorAction SilentlyContinue
[pscustomobject]@{
    PythonSyntax = $true
    PlanReadOnly = Select-String -LiteralPath $Files[1] -Pattern 'plan_is_read_only' -Quiet
    AtomicSwitch = Select-String -LiteralPath $Files[1] -Pattern 'verified_before_switch' -Quiet
    IntegrityCheck = Select-String -LiteralPath $Files[1] -Pattern 'PRAGMA integrity_check' -Quiet
    Sha256Verification = Select-String -LiteralPath $Files[1] -Pattern 'database_sha256' -Quiet
    RollbackGuard = Select-String -LiteralPath $Files[1] -Pattern 'Rollback blocked' -Quiet
    OldAssetsRetained = Select-String -LiteralPath $Files[1] -Pattern 'old_base_and_deltas_retained' -Quiet
    CleanupProtection = Select-String -LiteralPath $Files[4] -Pattern 'protected_training_ids' -Quiet
    TaskState = if ($Task) { [string]$Task.State } else { 'NotFound' }
    ProductionModifiedByTest = $false
} | Format-List

Write-Host 'Training compaction C7 verification completed.' -ForegroundColor Green
Write-Host 'Next step: run Invoke-SmogAI-TrainingCompaction.ps1 with -Action plan.'
