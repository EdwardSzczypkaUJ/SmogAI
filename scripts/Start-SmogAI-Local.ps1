[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$DashboardPort = 8501,
    [ValidateRange(10, 300)]
    [int]$WaitSeconds = 90,
    [switch]$RestartExisting,
    [switch]$NoBrowser
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$StartedAt = Get-Date
$ErrorMessage = $null
$ApiProcess = $null
$DashboardProcess = $null
$ApiStarted = $false
$DashboardStarted = $false
$ApiReused = $false
$DashboardReused = $false
$BrowserOpened = $false
$StoppedProcessIds = @()
$LocalFilesRead = @()
$LocalFilesWritten = @()

. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Test-LocalHttp {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri
    )

    try {
        $Response = Invoke-WebRequest `
            -Uri $Uri `
            -UseBasicParsing `
            -TimeoutSec 5

        return ([int]$Response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
}

function Stop-LocalPortListener {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $Connections = @(
        Get-NetTCPConnection `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
    )

    $ProcessIds = @(
        $Connections |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Where-Object { $_ -and $_ -ne $PID }
    )

    foreach ($ProcessId in $ProcessIds) {
        $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue

        if ($null -ne $Process) {
            Stop-Process -Id $ProcessId -Force -ErrorAction Stop
            $script:StoppedProcessIds += $ProcessId
        }
    }

    $Deadline = (Get-Date).AddSeconds(15)

    do {
        $Remaining = @(
            Get-NetTCPConnection `
                -LocalPort $Port `
                -State Listen `
                -ErrorAction SilentlyContinue
        )

        if (@($Remaining).Count -eq 0) {
            return
        }

        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $Deadline)

    throw "Port ${Port} nadal jest zajęty."
}

function Wait-LocalHttp {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [string]$ServiceName,

        [System.Diagnostics.Process]$Process,

        [Parameter(Mandatory = $true)]
        [datetime]$Deadline
    )

    do {
        if (Test-LocalHttp -Uri $Uri) {
            return $true
        }

        if ($null -ne $Process) {
            $Process.Refresh()

            if ($Process.HasExited) {
                return $false
            }
        }

        Write-Host (
            '[{0}] uruchamianie: {1}' -f
            (Get-Date).ToString('HH:mm:ss'),
            $ServiceName
        )

        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $Deadline)

    return $false
}

function Write-LogTail {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [string]$Path,

        [int]$Lines = 60
    )

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Write-Host ''
        Write-Host $Label -ForegroundColor Yellow

        @(Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue) |
            ForEach-Object { Write-Host $_ }
    }
}

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot

    $PythonPath = Get-SmogAiPythonExe $ProjectRoot
    $EnvPath = Join-Path $RuntimeRoot 'server-local.env'
    $DashboardPath = Join-Path $ProjectRoot 'server\dashboard\app.py'
    $CommonPath = Join-Path $PSScriptRoot 'SmogAi.Common.ps1'

    $LocalFilesRead += $CommonPath
    $LocalFilesRead += $DashboardPath

    if (-not (Test-Path -LiteralPath $DashboardPath -PathType Leaf)) {
        throw "Nie znaleziono dashboardu: $DashboardPath"
    }

    if (Test-Path -LiteralPath $EnvPath -PathType Leaf) {
        Import-SmogAiEnvFile -Path $EnvPath
        $LocalFilesRead += $EnvPath
    }

    # Ustawienia lokalne są stosowane po wczytaniu pliku env.
    # Dzięki temu starszy adres publiczny nie przejmie dashboardu.
    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    $env:SMOG_AI_SERVER_STORAGE_BACKEND = 'object_store'
    $env:SMOG_AI_OBJECT_STORE_BACKEND = 'local'
    $env:SMOG_AI_OBJECT_STORE_LOCAL_ROOT = Join-Path $RuntimeRoot 'object-store'
    $env:SMOG_AI_OBJECT_STORE_PREFIX = ''
    $env:SMOG_AI_SERVER_UPLOADS_ENABLED = 'false'
    $env:SMOG_AI_SERVER_DATA_DIR = Join-Path $RuntimeRoot 'server-data'
    $env:SMOG_AI_DASHBOARD_API_URL = (
        'http://127.0.0.1:{0}/api/v1' -f $ApiPort
    )

    New-Item `
        -ItemType Directory `
        -Path $env:SMOG_AI_SERVER_DATA_DIR `
        -Force |
        Out-Null

    $LogRoot = Join-Path $RuntimeRoot (
        'logs\local-stack\{0}' -f $StartedAt.ToString('yyyyMMdd-HHmmss')
    )
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

    $ApiStdout = Join-Path $LogRoot 'api.stdout.log'
    $ApiStderr = Join-Path $LogRoot 'api.stderr.log'
    $DashboardStdout = Join-Path $LogRoot 'dashboard.stdout.log'
    $DashboardStderr = Join-Path $LogRoot 'dashboard.stderr.log'

    $LocalFilesWritten += $ApiStdout
    $LocalFilesWritten += $ApiStderr
    $LocalFilesWritten += $DashboardStdout
    $LocalFilesWritten += $DashboardStderr

    $ApiDocsUrl = 'http://127.0.0.1:{0}/docs' -f $ApiPort
    $DashboardHealthUrl = (
        'http://127.0.0.1:{0}/_stcore/health' -f $DashboardPort
    )
    $DashboardUrl = 'http://127.0.0.1:{0}' -f $DashboardPort

    Write-Host ''
    Write-Host '=== SmogAI — lokalne API i dashboard ===' -ForegroundColor Cyan
    Write-Host ("Projekt: {0}" -f $ProjectRoot)
    Write-Host ("API: {0}" -f $ApiDocsUrl)
    Write-Host ("Dashboard: {0}" -f $DashboardUrl)
    Write-Host 'Publikacja, Serving i harmonogramy nie będą uruchamiane.'

    if ($RestartExisting) {
        Write-Host ''
        Write-Host 'Zatrzymywanie poprzednich lokalnych serwerów...'

        Stop-LocalPortListener -Port $DashboardPort
        Stop-LocalPortListener -Port $ApiPort
    }

    $ApiReady = Test-LocalHttp -Uri $ApiDocsUrl

    if ($ApiReady) {
        $ApiReused = $true
        Write-Host 'API już działa — używam istniejącego procesu.' -ForegroundColor Green
    }
    else {
        $ApiListeners = @(
            Get-NetTCPConnection `
                -LocalPort $ApiPort `
                -State Listen `
                -ErrorAction SilentlyContinue
        )

        if (@($ApiListeners).Count -gt 0) {
            throw (
                "Port ${ApiPort} jest zajęty, ale API nie odpowiada. " +
                'Uruchom skrypt z parametrem -RestartExisting.'
            )
        }

        $ApiArguments = @(
            '-m'
            'uvicorn'
            'server.api.main:app'
            '--host'
            '127.0.0.1'
            '--port'
            [string]$ApiPort
            '--proxy-headers'
            '--forwarded-allow-ips=127.0.0.1'
        )

        $ApiProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList $ApiArguments `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $ApiStdout `
            -RedirectStandardError $ApiStderr `
            -WindowStyle Hidden `
            -PassThru

        $ApiStarted = $true

        $ApiReady = Wait-LocalHttp `
            -Uri $ApiDocsUrl `
            -ServiceName 'API' `
            -Process $ApiProcess `
            -Deadline ((Get-Date).AddSeconds($WaitSeconds))

        if (-not $ApiReady) {
            Write-LogTail -Label 'API — STDOUT' -Path $ApiStdout
            Write-LogTail -Label 'API — STDERR' -Path $ApiStderr
            throw 'Lokalne API nie osiągnęło gotowości.'
        }

        Write-Host 'API jest gotowe.' -ForegroundColor Green
    }

    $DashboardReady = Test-LocalHttp -Uri $DashboardHealthUrl

    if ($DashboardReady) {
        $DashboardReused = $true
        Write-Host 'Dashboard już działa — używam istniejącego procesu.' -ForegroundColor Green
    }
    else {
        $DashboardListeners = @(
            Get-NetTCPConnection `
                -LocalPort $DashboardPort `
                -State Listen `
                -ErrorAction SilentlyContinue
        )

        if (@($DashboardListeners).Count -gt 0) {
            throw (
                "Port ${DashboardPort} jest zajęty, ale dashboard nie odpowiada. " +
                'Uruchom skrypt z parametrem -RestartExisting.'
            )
        }

        $DashboardArguments = @(
            '-m'
            'streamlit'
            'run'
            $DashboardPath
            '--server.address'
            '127.0.0.1'
            '--server.port'
            [string]$DashboardPort
            '--server.headless'
            'true'
            '--browser.gatherUsageStats'
            'false'
        )

        $DashboardProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList $DashboardArguments `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $DashboardStdout `
            -RedirectStandardError $DashboardStderr `
            -WindowStyle Hidden `
            -PassThru

        $DashboardStarted = $true

        $DashboardReady = Wait-LocalHttp `
            -Uri $DashboardHealthUrl `
            -ServiceName 'dashboard' `
            -Process $DashboardProcess `
            -Deadline ((Get-Date).AddSeconds($WaitSeconds))

        if (-not $DashboardReady) {
            Write-LogTail `
                -Label 'DASHBOARD — STDOUT' `
                -Path $DashboardStdout

            Write-LogTail `
                -Label 'DASHBOARD — STDERR' `
                -Path $DashboardStderr `
                -Lines 100

            throw 'Lokalny dashboard nie osiągnął gotowości.'
        }

        Write-Host 'Dashboard jest gotowy.' -ForegroundColor Green
    }

    if (-not $NoBrowser) {
        Start-Process $DashboardUrl
        $BrowserOpened = $true
    }

    Write-Host ''
    Write-Host 'Lokalne API i dashboard działają.' -ForegroundColor Green
    Write-Host ("API: {0}" -f $ApiDocsUrl)
    Write-Host ("Dashboard: {0}" -f $DashboardUrl)
}
catch {
    $ErrorMessage = $_.Exception.Message
    Write-Host ''
    Write-Host ("BŁĄD: {0}" -f $ErrorMessage) -ForegroundColor Red
}
finally {
    $ApiProcessId = $null
    $DashboardProcessId = $null
    $ApiProcessRunning = $false
    $DashboardProcessRunning = $false

    if ($null -ne $ApiProcess) {
        $ApiProcess.Refresh()
        $ApiProcessId = $ApiProcess.Id
        $ApiProcessRunning = -not $ApiProcess.HasExited
    }

    if ($null -ne $DashboardProcess) {
        $DashboardProcess.Refresh()
        $DashboardProcessId = $DashboardProcess.Id
        $DashboardProcessRunning = -not $DashboardProcess.HasExited
    }

    $ApiFinalReady = Test-LocalHttp -Uri (
        'http://127.0.0.1:{0}/docs' -f $ApiPort
    )
    $DashboardFinalReady = Test-LocalHttp -Uri (
        'http://127.0.0.1:{0}/_stcore/health' -f $DashboardPort
    )

    $Status = if (
        $null -eq $ErrorMessage -and
        $ApiFinalReady -and
        $DashboardFinalReady
    ) {
        'LOCAL_API_AND_DASHBOARD_READY'
    }
    else {
        'LOCAL_STACK_START_FAILED'
    }

    $ScheduleStates = @()

    foreach ($TaskName in @(
        'SmogAI-HF21-Serving-8h'
        'SmogAI-HF21-Training-12h'
        'SmogAI-HF21-Heavy-28h'
    )) {
        $Task = Get-ScheduledTask `
            -TaskPath '\SmogAI\' `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue

        if ($null -ne $Task) {
            $ScheduleStates += [pscustomobject]@{
                task_name = $TaskName
                state     = [string]$Task.State
            }
        }
    }

    [pscustomobject]@{
        status                       = $Status
        started_at                   = $StartedAt
        finished_at                  = Get-Date
        api_url                      = 'http://127.0.0.1:{0}/docs' -f $ApiPort
        api_ready                    = $ApiFinalReady
        api_started                  = $ApiStarted
        api_reused                   = $ApiReused
        api_process_id               = $ApiProcessId
        api_process_running          = $ApiProcessRunning
        dashboard_url                = 'http://127.0.0.1:{0}' -f $DashboardPort
        dashboard_api_url            = 'http://127.0.0.1:{0}/api/v1' -f $ApiPort
        dashboard_ready              = $DashboardFinalReady
        dashboard_started            = $DashboardStarted
        dashboard_reused             = $DashboardReused
        dashboard_process_id         = $DashboardProcessId
        dashboard_process_running    = $DashboardProcessRunning
        browser_opened               = $BrowserOpened
        stopped_process_ids          = @($StoppedProcessIds)
        schedules                    = @($ScheduleStates)
        error                        = $ErrorMessage
        local_files_read             = @($LocalFilesRead)
        local_files_written          = @($LocalFilesWritten)
        repository_source_modified   = $false
        processes_started            = ($ApiStarted -or $DashboardStarted)
        processes_stopped            = (@($StoppedProcessIds).Count -gt 0)
        schedules_modified           = $false
        publication_started          = $false
        deployment_started           = $false
        external_reads_performed     = $false
        external_writes_performed    = $false
        secret_values_requested      = $false
        secret_values_displayed      = $false
    }

    if ($null -ne $ErrorMessage) {
        exit 1
    }
}
