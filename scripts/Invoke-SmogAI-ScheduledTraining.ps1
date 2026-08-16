[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateSet('quick','full')][string]$Profile = 'quick',
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
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$MlflowExe = Join-Path $ProjectRoot '.venv\Scripts\mlflow.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
$LogRoot = Join-Path $RuntimeRoot 'logs\scheduled-training'
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Log = Join-Path $LogRoot "training-$Profile-$Stamp.log"
$MlflowOut = Join-Path $LogRoot 'mlflow-server.stdout.log'
$MlflowErr = Join-Path $LogRoot 'mlflow-server.stderr.log'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Brak Python venv: $Python"
}

function Test-MlflowReady {
    try {
        $Response = Invoke-WebRequest -UseBasicParsing `
            -Uri "http://127.0.0.1:$MlflowPort/health" -TimeoutSec 3
        return $Response.StatusCode -eq 200
    } catch { return $false }
}

try {
    "$(Get-Date -Format s) START profile=$Profile" | Out-File -LiteralPath $Log -Encoding utf8
    if (-not (Test-MlflowReady)) {
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
        $Executable = if (Test-Path -LiteralPath $MlflowExe -PathType Leaf) {
            $MlflowExe
        } else {
            $Python
            $MlflowArgs = @('-m','mlflow') + $MlflowArgs
        }
        $MlflowProcess = Start-Process -FilePath $Executable `
            -ArgumentList $MlflowArgs -WorkingDirectory $RuntimeRoot `
            -WindowStyle Hidden -RedirectStandardOutput $MlflowOut `
            -RedirectStandardError $MlflowErr -PassThru
        "$(Get-Date -Format s) MLFLOW_START port=$MlflowPort" | Out-File `
            -LiteralPath $Log -Append -Encoding utf8
        $Deadline = (Get-Date).AddSeconds($MlflowStartupTimeoutSeconds)
        while ((Get-Date) -lt $Deadline -and -not (Test-MlflowReady)) {
            if ($MlflowProcess.HasExited) {
                throw "MLflow zakonczyl sie przed gotowoscia, exit=$($MlflowProcess.ExitCode), stderr=$MlflowErr"
            }
            Start-Sleep -Seconds 2
        }
        if (-not (Test-MlflowReady)) {
            throw "MLflow nie osiagnal gotowosci w ${MlflowStartupTimeoutSeconds}s; stderr=$MlflowErr"
        }
    }
    "$(Get-Date -Format s) MLFLOW_READY port=$MlflowPort" | Out-File `
        -LiteralPath $Log -Append -Encoding utf8
    $PreviousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Python -m smog_ai snapshot-train-hourly `
            --profile $Profile --snapshot auto `
            --config $Config --env-file $EnvFile *>> $Log
        $Code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorAction
    }
    if ($Code -eq 5) {
        "$(Get-Date -Format s) DEFERRED shared_training_lock_busy" | Out-File -LiteralPath $Log -Append -Encoding utf8
        exit 5
    }
    "$(Get-Date -Format s) FINISH exit=$Code" | Out-File -LiteralPath $Log -Append -Encoding utf8
    exit $Code
} catch {
    "$(Get-Date -Format s) FAILED $($_.Exception.Message)" | Out-File -LiteralPath $Log -Append -Encoding utf8
    Write-Error $_
    exit 1
}
