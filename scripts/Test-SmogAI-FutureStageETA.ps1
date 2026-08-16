[CmdletBinding()]
param([string]$ProjectRoot = (Get-Location).Path)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Monitor = Join-Path $ProjectRoot 'scripts\smog_ai_automation_monitor.py'
$Trainer = Join-Path $ProjectRoot 'smog_ai\hourly\trainer.py'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
foreach ($Path in @($Monitor, $Trainer, $Python)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing file: $Path"
    }
}
& $Python -m py_compile $Monitor $Trainer
if ($LASTEXITCODE -ne 0) { throw 'Monitor Python syntax check failed.' }

[pscustomobject]@{
    PythonSyntax = $true
    FutureStageMarker = Select-String -LiteralPath $Monitor -Pattern 'HF21_MONITOR_FUTURE_STAGE_ETA_V1' -Quiet
    HistoricalDurations = Select-String -LiteralPath $Monitor -Pattern 'durations_by_key' -Quiet
    FutureStartAndFinish = Select-String -LiteralPath $Monitor -Pattern 'predicted_start_epoch' -Quiet
    FutureStageColumn = Select-String -LiteralPath $Monitor -Pattern 'Prognoza / ETA' -Quiet
    FallbackForNewC6Stages = Select-String -LiteralPath $Monitor -Pattern 'training-delta-preflight' -Quiet
    ActualLogFileSize = Select-String -LiteralPath $Monitor -Pattern 'actual_log_size_bytes' -Quiet
    ExactLogBytes = Select-String -LiteralPath $Monitor -Pattern 'format_log_size' -Quiet
    TerminalCandidateStatus = Select-String -LiteralPath $Monitor -Pattern 'training_terminal' -Quiet
    SkippedBudgetStatus = Select-String -LiteralPath $Monitor -Pattern 'candidate_skipped_budget' -Quiet
    TrainerPersistsSkippedBudget = Select-String -LiteralPath $Trainer -Pattern 'candidate_skipped_budget' -Quiet
    DeprecatedWidth = Select-String -LiteralPath $Monitor -Pattern 'use_container_width' -Quiet
} | Format-List

Write-Host 'Future-stage ETA verification completed.' -ForegroundColor Green
