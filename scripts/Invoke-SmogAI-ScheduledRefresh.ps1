[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateSet('serving','quick','normal','medium','full')][string]$Profile = 'normal',
    [string]$ExperimentalTargets = '*',
    [switch]$PublishDigitalOcean,
    [int]$MlflowPort = 5000,
    [int]$MlflowStartupTimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8:backslashreplace'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$MlflowExe = Join-Path $ProjectRoot '.venv\Scripts\mlflow.exe'
$Automation = Join-Path $ProjectRoot 'scripts\Start-SmogAI-Automation.ps1'
$LogRoot = Join-Path $RuntimeRoot 'logs\scheduled-refresh'
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$WrapperLog = Join-Path $LogRoot "scheduled-refresh-$Stamp.log"
$MlflowOut = Join-Path $LogRoot 'mlflow-server.stdout.log'
$MlflowErr = Join-Path $LogRoot 'mlflow-server.stderr.log'

function Write-ScheduleLog([string]$Message) {
    $Line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $WrapperLog -Value $Line -Encoding UTF8
}
function Test-MlflowReady {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$MlflowPort/health" -TimeoutSec 3
        return $Response.StatusCode -eq 200
    } catch { return $false }
}

try {
    Write-ScheduleLog "START project=$ProjectRoot profile=$Profile pid=$PID"
    if ($Profile -ne 'serving' -and -not (Test-MlflowReady)) {
        $MlflowRoot = Join-Path $RuntimeRoot 'mlflow'
        $ArtifactRoot = Join-Path $MlflowRoot 'artifacts'
        $DatabasePath = Join-Path $MlflowRoot 'mlflow.db'
        New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null
        $DatabaseUri = 'sqlite:///' + ($DatabasePath.Replace('\', '/'))
        $ArtifactUri = 'file:///' + ($ArtifactRoot.Replace('\', '/'))
        $MlflowArgs = @(
            'server', '--host', '127.0.0.1', '--port', [string]$MlflowPort,
            '--backend-store-uri', $DatabaseUri,
            '--default-artifact-root', $ArtifactUri
        )
        if (Test-Path -LiteralPath $MlflowExe -PathType Leaf) {
            $Process = Start-Process -FilePath $MlflowExe -ArgumentList $MlflowArgs -WorkingDirectory $RuntimeRoot -WindowStyle Hidden -RedirectStandardOutput $MlflowOut -RedirectStandardError $MlflowErr -PassThru
        } else {
            $Process = Start-Process -FilePath $Python -ArgumentList (@('-m','mlflow') + $MlflowArgs) -WorkingDirectory $RuntimeRoot -WindowStyle Hidden -RedirectStandardOutput $MlflowOut -RedirectStandardError $MlflowErr -PassThru
        }
        Write-ScheduleLog "MLFLOW_START pid=$($Process.Id) port=$MlflowPort"
        $Deadline = (Get-Date).AddSeconds($MlflowStartupTimeoutSeconds)
        while ((Get-Date) -lt $Deadline -and -not (Test-MlflowReady)) {
            if ($Process.HasExited) { throw "MLflow zakonczyl sie przed uzyskaniem gotowosci, exit=$($Process.ExitCode), stderr=$MlflowErr" }
            Start-Sleep -Seconds 2
        }
        if (-not (Test-MlflowReady)) { throw "MLflow nie osiagnal gotowosci w ${MlflowStartupTimeoutSeconds}s; stderr=$MlflowErr" }
    }
    if ($Profile -ne 'serving') { Write-ScheduleLog 'MLFLOW_READY' }
    # Windows PowerShell 5 promotes any native stderr line to an ErrorRecord.
    # Warnings from telemetry must not abort the wrapper; the child exit code
    # remains the sole success/failure contract.
    $PreviousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Automation -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot -Profile $Profile -ExperimentalTargets $ExperimentalTargets *>> $WrapperLog
        $Code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorAction
    }
    if ($Code -eq 0 -and $PublishDigitalOcean) {
        $Publisher = Join-Path $ProjectRoot 'scripts\Publish-SmogAI-ServingToDigitalOcean.ps1'
        Write-ScheduleLog 'PUBLICATION_START serving=v2 freshness_hours=8 retention=3'
        & $Publisher -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot `
            -FreshnessThresholdHours 8 -SkipSeal `
            -Approval 'PUBLISH VERIFIED SERVING V2' `
            -RetainServingReleases 3 `
            -RetentionApproval 'PRUNE OLD SERVING RELEASES' *>> $WrapperLog
        $Code = $LASTEXITCODE
        Write-ScheduleLog "PUBLICATION_FINISH exit=$Code"
    }
    Write-ScheduleLog "FINISH exit=$Code"
    exit $Code
} catch {
    Write-ScheduleLog "FAILED $($_.Exception.Message)"
    Write-Error $_
    exit 1
}
