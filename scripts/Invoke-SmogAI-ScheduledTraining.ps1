[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateSet('quick','full')][string]$Profile = 'quick'
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
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
$LogRoot = Join-Path $RuntimeRoot 'logs\scheduled-training'
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Log = Join-Path $LogRoot "training-$Profile-$Stamp.log"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Brak Python venv: $Python"
}

try {
    "$(Get-Date -Format s) START profile=$Profile" | Out-File -LiteralPath $Log -Encoding utf8
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
