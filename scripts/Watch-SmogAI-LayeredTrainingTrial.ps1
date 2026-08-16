[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [int]$RefreshSeconds = 5,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$TrialsRoot = Join-Path $RuntimeRoot 'training-datasets\_incremental\trials'
if (-not (Test-Path -LiteralPath $TrialsRoot -PathType Container)) {
    throw "Trials directory does not exist: $TrialsRoot"
}

do {
    $ProgressFile = Get-ChildItem -LiteralPath $TrialsRoot -Filter '*current.json' -File -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $ProgressFile) {
        Write-Host 'No layered training trial progress file found yet.'
        if ($Once) { break }
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    try {
        $Progress = Get-Content -LiteralPath $ProgressFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Write-Host 'Progress file is being updated; retrying...'
        if ($Once) { break }
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    $Percent = '{0:N1}%' -f (100.0 * [double]$Progress.overall_fraction)
    $Elapsed = if ($Progress.elapsed_human) { [string]$Progress.elapsed_human } else { '--' }
    $Eta = if ($Progress.eta_range_human) {
        [string]$Progress.eta_range_human
    } elseif ($Progress.eta_human) {
        [string]$Progress.eta_human
    } else { '--' }
    $Status = if ($Progress.status) { [string]$Progress.status } else { 'running' }
    $Task = if ($Progress.current_task) { [string]$Progress.current_task } else { '--' }

    $LogsDirectory = Split-Path $ProgressFile.DirectoryName -Parent
    $TrialDirectory = Split-Path $LogsDirectory -Parent

    Clear-Host
    [pscustomobject]@{
        Trial = Split-Path $TrialDirectory -Leaf
        Status = $Status
        Progress = $Percent
        Elapsed = $Elapsed
        ETA = $Eta
        Task = $Task
        Updated = $ProgressFile.LastWriteTime
        ProgressFile = $ProgressFile.FullName
    } | Format-List

    if ($Once -or $Status -match '^(success|failed|error|cancelled|completed)$') { break }
    Start-Sleep -Seconds $RefreshSeconds
} while ($true)
