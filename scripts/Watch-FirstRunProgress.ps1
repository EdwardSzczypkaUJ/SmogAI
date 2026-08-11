[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [ValidateRange(1, 300)][int]$RefreshSeconds = 5,
    [ValidateRange(20, 100)][int]$BarWidth = 48,
    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Resolve-ProjectRoot {
    param([string]$Candidate)
    if ($Candidate) {
        return (Resolve-Path -LiteralPath $Candidate).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
}

function Format-Duration {
    param($Seconds)
    if ($null -eq $Seconds) { return 'brak danych' }
    try { $value = [double]$Seconds } catch { return 'brak danych' }
    if ([double]::IsNaN($value) -or [double]::IsInfinity($value) -or $value -lt 0) {
        return 'brak danych'
    }
    $span = [TimeSpan]::FromSeconds($value)
    if ($span.TotalHours -ge 1) {
        return ('{0}h {1:00}m {2:00}s' -f [math]::Floor($span.TotalHours), $span.Minutes, $span.Seconds)
    }
    if ($span.TotalMinutes -ge 1) {
        return ('{0}m {1:00}s' -f [math]::Floor($span.TotalMinutes), $span.Seconds)
    }
    return ('{0}s' -f [math]::Round($span.TotalSeconds))
}

function New-ProgressBar {
    param([double]$Percent, [int]$Width)
    $safe = [math]::Max(0.0, [math]::Min(100.0, $Percent))
    $filled = [int][math]::Round($Width * $safe / 100.0)
    return ('[' + ('#' * $filled) + ('-' * ($Width - $filled)) + ']')
}

$ProjectRoot = Resolve-ProjectRoot $ProjectRoot
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $env:ProgramData 'SmogAI'
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$StatePath = Join-Path $RuntimeRoot 'logs\progress\first-run-current.json'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

Write-Host "Monitor: $StatePath" -ForegroundColor DarkGray
Write-Host 'Ctrl+C zatrzymuje wyłącznie monitor, nie first-run.' -ForegroundColor DarkGray

while ($true) {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        Clear-Host
        Write-Host 'SMOG AI — FIRST-RUN PROGRESS' -ForegroundColor Cyan
        Write-Host "Czekam na plik: $StatePath" -ForegroundColor Yellow
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    try {
        $Raw = [System.IO.File]::ReadAllText($StatePath)
        $State = $Raw | ConvertFrom-Json
    }
    catch {
        Start-Sleep -Milliseconds 500
        continue
    }

    if ($AsJson) {
        $State | ConvertTo-Json -Depth 30
    }
    else {
        Clear-Host
        $overall = [double]$State.overall_percent
        $stagePercent = [double]$State.current_stage_percent
        $updated = [DateTimeOffset]::Parse([string]$State.updated_at)
        $age = [DateTimeOffset]::Now - $updated.ToLocalTime()

        Write-Host 'SMOG AI — FIRST-RUN PROGRESS / ETA' -ForegroundColor Cyan
        Write-Host ''
        Write-Host ((New-ProgressBar $overall $BarWidth) + (' {0,6:N2}%' -f $overall)) -ForegroundColor Green
        Write-Host ('Status:       {0}' -f $State.status)
        Write-Host ('Etap:         {0} ({1:N2}%)' -f $State.current_stage, $stagePercent)
        Write-Host ('Zadanie:      {0}' -f $State.current_task)
        Write-Host ('Czas całości: {0}' -f (Format-Duration $State.elapsed_seconds))
        Write-Host ('Czas zadania: {0}' -f (Format-Duration $State.current_task_elapsed_seconds))

        if ($State.eta_range_human) {
            Write-Host ('ETA zakres:   {0}' -f $State.eta_range_human) -ForegroundColor Yellow
        }
        elseif ($null -ne $State.eta_seconds) {
            Write-Host ('ETA:          {0}' -f (Format-Duration $State.eta_seconds)) -ForegroundColor Yellow
        }
        else {
            Write-Host 'ETA:          brak wystarczających danych' -ForegroundColor Yellow
        }
        Write-Host ('Pewność ETA:  {0}' -f $State.eta_confidence)
        if ($State.estimated_finish_at) {
            $finish = [DateTimeOffset]::Parse([string]$State.estimated_finish_at).ToLocalTime()
            Write-Host ('Koniec około: {0}' -f $finish.ToString('yyyy-MM-dd HH:mm:ss'))
        }
        if ($null -ne $State.current_task_expected_seconds) {
            Write-Host ('Szacunek bieżącego zadania: {0}; pozostało około {1}' -f
                (Format-Duration $State.current_task_expected_seconds),
                (Format-Duration $State.current_task_eta_seconds))
        }

        $stageWork = $null
        if ($State.stage_work -and $State.current_stage) {
            $stageWork = $State.stage_work.PSObject.Properties |
                Where-Object { $_.Name -eq [string]$State.current_stage } |
                Select-Object -ExpandProperty Value -First 1
        }
        if ($stageWork) {
            Write-Host ('Jednostki etapu: {0:N2} / {1:N2} ({2:N2}%)' -f
                [double]$stageWork.completed_weight,
                [double]$stageWork.total_weight,
                [double]$stageWork.completed_percent)
        }

        Write-Host ('Ostatnia aktualizacja: {0} ({1:N0}s temu)' -f
            $updated.ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss'),
            $age.TotalSeconds)
        if ($age.TotalMinutes -ge 2 -and $State.status -eq 'running') {
            Write-Host 'UWAGA: stan nie był odświeżany od ponad 2 minut.' -ForegroundColor Red
        }

        if ($State.detail) {
            Write-Host ''
            Write-Host 'Szczegóły zadania:' -ForegroundColor Cyan
            $State.detail | ConvertTo-Json -Depth 12
        }

        if (Test-Path -LiteralPath $Python -PathType Leaf) {
            try {
                $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                    Where-Object {
                        $_.Name -match '^python(w)?\.exe$' -and
                        $_.CommandLine -like '*-m smog_ai first-run*' -and
                        $_.CommandLine -like "*$ProjectRoot*"
                    })
                if ($processes.Count -gt 0) {
                    $process = Get-Process -Id $processes[-1].ProcessId -ErrorAction SilentlyContinue
                    if ($process) {
                        Write-Host ''
                        Write-Host ('Proces: PID={0}, CPU={1:N1}s, RAM={2:N1} MB, wątki={3}' -f
                            $process.Id, $process.CPU, ($process.WorkingSet64 / 1MB), $process.Threads.Count)
                    }
                }
            }
            catch { }
        }
    }

    if ($State.status -notin @('created', 'running')) {
        Write-Host ''
        Write-Host ('Przebieg zakończony: {0}' -f $State.status) -ForegroundColor Cyan
        break
    }
    Start-Sleep -Seconds $RefreshSeconds
}
