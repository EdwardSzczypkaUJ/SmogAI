[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$Config = 'C:\ProgramData\SmogAI\config.yaml',
    [string]$EnvFile = 'C:\ProgramData\SmogAI\smog-ai.env',
    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',
    [string]$Targets = 'PM10',
    [switch]$Apply,
    [string]$Confirmation = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$RequiredConfirmation = 'RUN LAYERED CANDIDATE ONLY'

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

$Control = Join-Path $ProjectRoot 'scripts\smog_ai_layered_candidate_control.py'
if (-not (Test-Path -LiteralPath $Control -PathType Leaf)) {
    throw "Layered candidate control is missing: $Control"
}

if (-not $Apply) {
    & $Python $Control --runtime-root $RuntimeRoot --profile $Profile --targets $Targets --action plan
    if ($LASTEXITCODE -ne 0) { throw 'Layered candidate plan is blocked.' }
    Write-Host ''
    Write-Host "PLAN ONLY. Next step requires: $RequiredConfirmation" -ForegroundColor Yellow
    exit 0
}

if ($Confirmation -cne $RequiredConfirmation) {
    throw "Exact confirmation required: $RequiredConfirmation"
}
$Task = Get-ScheduledTask -TaskPath '\SmogAI\' -TaskName 'SmogAI-HF21-Refresh-6h' -ErrorAction SilentlyContinue
if ($Task -and [string]$Task.State -ne 'Disabled') {
    throw 'The scheduled task must be Disabled for C5 candidate-only training.'
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

$PreflightText = & $Python $Control --runtime-root $RuntimeRoot --profile $Profile --targets $Targets --action preflight
if ($LASTEXITCODE -ne 0) { throw 'Layered candidate fast preflight failed immediately before training.' }
$Preflight = ($PreflightText -join [Environment]::NewLine) | ConvertFrom-Json
if ($Preflight.status -ne 'ready') {
    $Reasons = @($Preflight.errors | ForEach-Object { [string]$_.reason }) -join ', '
    throw "Layered candidate is not ready: $($Preflight.status). Reasons: $Reasons"
}
Write-Host ("Fast preflight: ready; deltas={0}; journal={1}; chain={2}" -f `
    $Preflight.delta_count, $Preflight.live_journal_seq, $Preflight.chain_sha256) -ForegroundColor Green

$BeforeText = & $Python $Control --runtime-root $RuntimeRoot --action fingerprint
if ($LASTEXITCODE -ne 0) { throw 'Cannot fingerprint active production models.' }
$Before = ($BeforeText -join [Environment]::NewLine) | ConvertFrom-Json
$StartedAt = [DateTime]::UtcNow.ToString('o')

Push-Location $ProjectRoot
try {
    & $Python -m smog_ai snapshot-train-hourly `
        --profile $Profile `
        --snapshot layered `
        --targets $Targets `
        --candidate-only `
        --config $Config `
        --env-file $EnvFile
    if ($LASTEXITCODE -ne 0) {
        throw "Layered candidate-only training failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

$InspectionText = & $Python $Control --runtime-root $RuntimeRoot --targets $Targets --action inspect --since $StartedAt
if ($LASTEXITCODE -ne 0) { throw 'Post-training candidate inspection failed.' }
$Inspection = ($InspectionText -join [Environment]::NewLine) | ConvertFrom-Json
$After = $Inspection.active_fingerprint
$ProductionUnchanged = (
    $Before.active_models_sha256 -eq $After.active_models_sha256 -and
    $Before.quick_pointer_sha256 -eq $After.quick_pointer_sha256 -and
    $Before.serving_pointer_sha256 -eq $After.serving_pointer_sha256
)
if (-not $ProductionUnchanged) {
    throw 'SAFETY FAILURE: an active model or production pointer changed.'
}
if ($Inspection.candidate_count -lt 1) {
    throw 'No newly registered candidate was found after training.'
}
if (@($Inspection.candidates | Where-Object { $_.active }).Count -gt 0) {
    throw 'SAFETY FAILURE: a candidate-only model was activated.'
}

[pscustomobject]@{
    Status = 'success'
    Mode = 'layered_candidate_only'
    CandidateCount = $Inspection.candidate_count
    Candidates = $Inspection.candidates
    ProductionUnchanged = $ProductionUnchanged
    ActiveModelsSha256 = $After.active_models_sha256
    QuickPointerSha256 = $After.quick_pointer_sha256
    ServingPointerSha256 = $After.serving_pointer_sha256
    TaskState = if ($Task) { [string]$Task.State } else { 'NotFound' }
    ExternalPublication = $false
    NextAction = 'review_candidate_classification'
} | ConvertTo-Json -Depth 20
