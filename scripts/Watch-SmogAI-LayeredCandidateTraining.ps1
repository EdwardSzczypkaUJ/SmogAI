[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',
    [int]$RefreshSeconds = 5,
    [datetime]$NotBefore = (Get-Date).AddMinutes(-2),
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$ProgressFile = Join-Path $RuntimeRoot ("logs\progress\snapshot-train-hourly-{0}-current.json" -f $Profile)
$Terminal = '^(success|failed|error|cancelled|completed)$'

do {
    $File = Get-Item -LiteralPath $ProgressFile -ErrorAction SilentlyContinue
    if (-not $File -or $File.LastWriteTime -lt $NotBefore) {
        Clear-Host
        Write-Host 'Waiting for a fresh candidate-training progress update...'
        Write-Host "Expected file: $ProgressFile"
        Write-Host "Not before:    $NotBefore"
        if ($File) { Write-Host "Stale update:  $($File.LastWriteTime)" }
        if ($Once) { break }
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    try {
        $Progress = Get-Content -LiteralPath $File.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Write-Host 'Progress file is being updated; retrying...'
        if ($Once) { break }
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    $Fraction = if ($null -ne $Progress.overall_fraction) {
        [double]$Progress.overall_fraction
    } elseif ($null -ne $Progress.overall_percent) {
        [double]$Progress.overall_percent / 100.0
    } else { 0.0 }
    $Percent = '{0:N1}%' -f (100.0 * $Fraction)
    $Elapsed = if ($Progress.elapsed_human) { [string]$Progress.elapsed_human } else { '--' }
    $Eta = if ($Progress.eta_range_human) {
        [string]$Progress.eta_range_human
    } elseif ($Progress.eta_human) {
        [string]$Progress.eta_human
    } else { '--' }
    $Status = if ($Progress.status) { [string]$Progress.status } else { 'running' }
    $Stage = if ($Progress.current_stage) { [string]$Progress.current_stage } else { '--' }
    $Task = if ($Progress.current_task) { [string]$Progress.current_task } else { '--' }

    Clear-Host
    [pscustomobject]@{
        Status = $Status
        Progress = $Percent
        Stage = $Stage
        Task = $Task
        Elapsed = $Elapsed
        ETA = $Eta
        Updated = $File.LastWriteTime
        FreshForThisRun = $true
        ProgressFile = $File.FullName
    } | Format-List

    if ($Once -or $Status -match $Terminal) { break }
    Start-Sleep -Seconds $RefreshSeconds
} while ($true)
