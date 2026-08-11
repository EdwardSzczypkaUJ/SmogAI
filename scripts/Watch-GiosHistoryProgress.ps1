[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [ValidateRange(2, 300)]
    [int]$RefreshSeconds = 5,

    [ValidateRange(20, 100)]
    [int]$BarWidth = 54,

    [ValidateRange(10, 100)]
    [int]$TailLines = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Format-Duration {
    param([AllowNull()]$Seconds)

    if ($null -eq $Seconds) {
        return 'brak danych'
    }

    $parsedSeconds = 0.0
    if (-not [double]::TryParse(
        [string]$Seconds,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsedSeconds
    )) {
        try {
            $parsedSeconds = [double]$Seconds
        }
        catch {
            return 'brak danych'
        }
    }

    if ([double]::IsNaN($parsedSeconds) -or $parsedSeconds -lt 0) {
        return 'brak danych'
    }

    $span = [TimeSpan]::FromSeconds($parsedSeconds)
    if ($span.TotalDays -ge 1) {
        return ('{0}d {1:00}h {2:00}m' -f
            [Math]::Floor($span.TotalDays),
            $span.Hours,
            $span.Minutes)
    }
    if ($span.TotalHours -ge 1) {
        return ('{0}h {1:00}m {2:00}s' -f
            [Math]::Floor($span.TotalHours),
            $span.Minutes,
            $span.Seconds)
    }
    if ($span.TotalMinutes -ge 1) {
        return ('{0}m {1:00}s' -f
            [Math]::Floor($span.TotalMinutes),
            $span.Seconds)
    }
    return ('{0:N0}s' -f $span.TotalSeconds)
}

function New-ProgressBar {
    param(
        [double]$Percent,
        [int]$Width
    )

    $safe = [Math]::Max(0.0, [Math]::Min(100.0, $Percent))
    $filled = [int][Math]::Round($Width * $safe / 100.0)
    $empty = [Math]::Max(0, $Width - $filled)

    return ('[{0}{1}] {2,6:N2}%' -f
        ('#' * $filled),
        ('-' * $empty),
        $safe)
}

function Get-ImporterProcess {
    param([Parameter(Mandatory = $true)][string]$Root)

    $fullRoot = [System.IO.Path]::GetFullPath($Root)

    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^python(w)?\.exe$' -and
                $_.CommandLine -and
                $_.CommandLine -like '*backfill-gios-history*' -and
                $_.CommandLine.IndexOf(
                    $fullRoot,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            } |
            Sort-Object CreationDate
    ) | Select-Object -Last 1
}

function Get-LatestHistoryLog {
    param([Parameter(Mandatory = $true)][string]$Root)

    $candidates = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'

    foreach ($directory in @(
        (Join-Path $Root 'logs\historical-gios\pm25-only'),
        (Join-Path $Root 'logs\historical-gios')
    )) {
        if (Test-Path -LiteralPath $directory -PathType Container) {
            Get-ChildItem -LiteralPath $directory -File -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Extension -eq '.log' -and
                    (
                        $_.Name -like 'pm25-*' -or
                        $_.Name -like 'gios-history-*'
                    )
                } |
                ForEach-Object { [void]$candidates.Add($_) }
        }
    }

    return $candidates.ToArray() |
        Sort-Object LastWriteTime |
        Select-Object -Last 1
}

function Get-JsonEvents {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [int]$MaximumLines = 20000
    )

    $events = New-Object 'System.Collections.Generic.List[object]'
    $lines = @(
        Get-Content -LiteralPath $LogPath -Tail $MaximumLines -ErrorAction SilentlyContinue
    )

    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith('{') -or -not $trimmed.EndsWith('}')) {
            continue
        }

        try {
            $event = $trimmed | ConvertFrom-Json
            if ($null -ne $event) {
                [void]$events.Add($event)
            }
        }
        catch {
            # Transcript może zawierać linie niebędące samodzielnym JSON-em.
        }
    }

    return $events.ToArray()
}

function Convert-EventTimestamp {
    param($Event)

    if ($null -eq $Event -or -not $Event.PSObject.Properties['timestamp']) {
        return $null
    }

    try {
        return [DateTimeOffset]::Parse([string]$Event.timestamp).LocalDateTime
    }
    catch {
        return $null
    }
}

function Get-LatestStageEvent {
    param([object[]]$Events)

    return @(
        $Events |
            Where-Object {
                $_.PSObject.Properties['stage'] -and
                $_.stage -in @(
                    'gios_history_prepared',
                    'gios_history_api',
                    'gios_history_progress'
                )
            }
    ) | Select-Object -Last 1
}

function Get-PreparedProgress {
    param(
        [object[]]$Events,
        $Latest,
        [DateTime]$FallbackStart
    )

    $completed = [double]$Latest.series_completed
    $total = [double]$Latest.series_total
    $percent = if ($total -gt 0) { 100.0 * $completed / $total } else { 0.0 }

    $sameRunEvents = @(
        $Events |
            Where-Object {
                $_.PSObject.Properties['stage'] -and
                $_.stage -eq 'gios_history_prepared' -and
                [string]$_.year -eq [string]$Latest.year -and
                [string]$_.parameter -eq [string]$Latest.parameter
            }
    )

    $firstTimestamp = $null
    if ($sameRunEvents.Count -gt 0) {
        $firstTimestamp = Convert-EventTimestamp -Event $sameRunEvents[0]
    }
    if ($null -eq $firstTimestamp) {
        $firstTimestamp = $FallbackStart
    }

    $elapsed = ((Get-Date) - $firstTimestamp).TotalSeconds
    $eta = $null
    if ($completed -gt 0 -and $total -ge $completed) {
        $eta = $elapsed / $completed * ($total - $completed)
    }

    return [pscustomobject]@{
        Stage = 'Arkusz roczny / serie stacji'
        Percent = $percent
        Completed = $completed
        Total = $total
        EtaSeconds = $eta
        Detail = (
            'rok={0}; parametr={1}; stacja={2}; dodano={3}' -f
            $Latest.year,
            $Latest.parameter,
            $Latest.station_code,
            $Latest.inserted_total
        )
    }
}

function Get-ApiProgress {
    param($Latest)

    $percent = if ($null -ne $Latest.percent) {
        [double]$Latest.percent
    }
    elseif ([double]$Latest.total_pages -gt 0) {
        100.0 * [double]$Latest.pages_done / [double]$Latest.total_pages
    }
    else {
        0.0
    }

    return [pscustomobject]@{
        Stage = 'API roczne / strony'
        Percent = $percent
        Completed = [double]$Latest.pages_done
        Total = [double]$Latest.total_pages
        EtaSeconds = if ($null -ne $Latest.eta_seconds) {
            [double]$Latest.eta_seconds
        }
        else {
            $null
        }
        Detail = (
            'rok={0}; województwo={1}; parametr={2}; strona={3}; dodano={4}' -f
            $Latest.year,
            $Latest.voivodeship,
            $Latest.parameter,
            $Latest.page,
            $Latest.inserted_combination
        )
    }
}

function Get-OverallProgress {
    param($Latest)

    return [pscustomobject]@{
        Stage = 'Cały plan importu'
        Percent = [double]$Latest.percent
        Completed = [double]$Latest.completed_units
        Total = [double]$Latest.total_units
        EtaSeconds = [double]$Latest.eta_seconds
        Detail = (
            'status={0}; jednostka={1}' -f
            $Latest.status,
            (($Latest.current_unit | ConvertTo-Json -Compress -Depth 10))
        )
    }
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)

    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'pyproject.toml'))) {
        throw "Katalog nie wygląda jak projekt SmogAI: $ProjectRoot"
    }

    $previousCpu = $null
    $previousPoll = Get-Date

    while ($true) {
        $now = Get-Date
        $processInfo = Get-ImporterProcess -Root $ProjectRoot
        $process = $null
        if ($processInfo) {
            $process = Get-Process -Id $processInfo.ProcessId -ErrorAction SilentlyContinue
        }

        $log = Get-LatestHistoryLog -Root $RuntimeRoot
        $events = @()
        $latestEvent = $null
        $stageProgress = $null

        if ($log) {
            $events = @(Get-JsonEvents -LogPath $log.FullName)
            $latestEvent = Get-LatestStageEvent -Events $events

            if ($latestEvent) {
                switch ([string]$latestEvent.stage) {
                    'gios_history_prepared' {
                        $fallbackStart = if ($process) { $process.StartTime } else { $log.CreationTime }
                        $stageProgress = Get-PreparedProgress `
                            -Events $events `
                            -Latest $latestEvent `
                            -FallbackStart $fallbackStart
                    }
                    'gios_history_api' {
                        $stageProgress = Get-ApiProgress -Latest $latestEvent
                    }
                    'gios_history_progress' {
                        $stageProgress = Get-OverallProgress -Latest $latestEvent
                    }
                }
            }
        }

        Clear-Host
        Write-Host 'SMOG AI — PROGRESS IMPORTU HISTORYCZNEGO GIOŚ' -ForegroundColor Cyan
        Write-Host ('Czas:     {0}' -f $now.ToString('yyyy-MM-dd HH:mm:ss'))
        Write-Host ('Projekt:  {0}' -f $ProjectRoot)
        Write-Host ('Runtime:  {0}' -f $RuntimeRoot)

        if ($process) {
            $elapsed = $now - $process.StartTime
            $cpuTotal = [double]$process.CPU
            $cpuDelta = if ($null -eq $previousCpu) {
                0.0
            }
            else {
                [Math]::Max(0.0, $cpuTotal - [double]$previousCpu)
            }

            $pollSeconds = [Math]::Max(
                0.001,
                ($now - $previousPoll).TotalSeconds
            )
            $logicalProcessors = [Math]::Max(1, [Environment]::ProcessorCount)
            $cpuPercent = [Math]::Min(
                100.0,
                100.0 * $cpuDelta / $pollSeconds / $logicalProcessors
            )

            Write-Host ''
            Write-Host 'Proces: AKTYWNY' -ForegroundColor Green
            Write-Host ('PID:      {0}' -f $process.Id)
            Write-Host ('Czas:     {0:hh\:mm\:ss}' -f $elapsed)
            Write-Host ('CPU:      ~{0:N1}% całego komputera; razem {1:N1}s' -f
                $cpuPercent,
                $cpuTotal)
            Write-Host ('RAM:      {0:N1} MB' -f ($process.WorkingSet64 / 1MB))
            Write-Host ('Wątki:    {0}' -f $process.Threads.Count)

            $previousCpu = $cpuTotal
            $previousPoll = $now
        }
        else {
            Write-Host ''
            Write-Host 'Proces: NIE ZNALEZIONO' -ForegroundColor Yellow
        }

        if ($stageProgress) {
            Write-Host ''
            Write-Host (New-ProgressBar `
                -Percent $stageProgress.Percent `
                -Width $BarWidth) -ForegroundColor Green
            Write-Host ('Etap:     {0}' -f $stageProgress.Stage)
            Write-Host ('Jednostki:{0:N0} / {1:N0}' -f
                $stageProgress.Completed,
                $stageProgress.Total)
            Write-Host ('ETA:      {0}' -f
                (Format-Duration -Seconds $stageProgress.EtaSeconds))

            if ($null -ne $stageProgress.EtaSeconds) {
                $finish = (Get-Date).AddSeconds($stageProgress.EtaSeconds)
                Write-Host ('Koniec:   około {0}' -f
                    $finish.ToString('yyyy-MM-dd HH:mm:ss'))
            }

            Write-Host ('Szczegóły: {0}' -f $stageProgress.Detail)
        }
        else {
            Write-Host ''
            Write-Host (
                'Brak jeszcze ustrukturyzowanego zdarzenia postępu. ' +
                'Importer może być na etapie pobierania ZIP-a, odczytu XLSX ' +
                'albo przygotowania metadanych.'
            ) -ForegroundColor Yellow
        }

        if ($log) {
            $idle = $now - $log.LastWriteTime
            Write-Host ''
            Write-Host ('Log:      {0}' -f $log.FullName)
            Write-Host ('Rozmiar:  {0:N1} KB' -f ($log.Length / 1KB))
            Write-Host ('Zapis:    {0} ({1:N0}s temu)' -f
                $log.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'),
                $idle.TotalSeconds)

            Write-Host ''
            Write-Host ('OSTATNIE {0} LINII' -f $TailLines) -ForegroundColor Cyan
            Write-Host ('-' * 90)
            Get-Content -LiteralPath $log.FullName -Tail $TailLines -ErrorAction SilentlyContinue
        }
        else {
            Write-Host ''
            Write-Host 'Nie znaleziono jeszcze logu importu GIOŚ.' -ForegroundColor Yellow
        }

        if (-not $process) {
            Write-Host ''
            Write-Host (
                'Monitor kończy pracę. Import mógł się zakończyć albo nie został uruchomiony.'
            ) -ForegroundColor Cyan
            break
        }

        Write-Host ''
        Write-Host (
            "Odświeżenie za ${RefreshSeconds}s. Ctrl+C zatrzymuje tylko monitor."
        ) -ForegroundColor DarkGray
        Start-Sleep -Seconds $RefreshSeconds
    }

    exit 0
}
catch {
    $Message = $_.Exception.Message
    $Location = $_.InvocationInfo.PositionMessage
    $Stack = $_.ScriptStackTrace

    Write-Error (
        "Monitor importu zakończył się błędem.`r`n" +
        "Komunikat: $Message`r`n" +
        "Miejsce: $Location`r`n" +
        "Stos: $Stack"
    )
    exit 1
}
