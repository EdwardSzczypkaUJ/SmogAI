[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][string]$Command,
    [int]$TimeoutMinutes = 45,
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string[]]$CliArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

$ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
$RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
$env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
$env:SMOG_AI_DATA_ROOT = $RuntimeRoot
$PythonExe = Get-SmogAiPythonExe $ProjectRoot
$ConfigPath = Join-Path $RuntimeRoot 'config.yaml'
$EnvPath = Join-Path $RuntimeRoot 'smog-ai.env'
$SafeTaskName = ($TaskName -replace '[^A-Za-z0-9_-]', '-')
$LogCategory = switch -Regex ($TaskName) {
    'Hourly' { 'hourly'; break }
    'Daily' { 'daily'; break }
    'Weekly|Training' { 'training'; break }
    'Monthly' { 'monthly'; break }
    default { 'other' }
}
$LogDirectory = Join-Path (Join-Path $RuntimeRoot 'logs') $LogCategory
$TemporaryDirectory = Join-Path $RuntimeRoot 'tmp'
New-Item -ItemType Directory -Path $LogDirectory, $TemporaryDirectory -Force | Out-Null
$RunId = [guid]::NewGuid().ToString()
$StartedAt = [DateTimeOffset]::UtcNow
$LogFile = Join-Path $LogDirectory ("{0}_{1}_{2}.log" -f $SafeTaskName, $StartedAt.ToString('yyyyMMddTHHmmssZ'), $RunId)
$StdOutFile = Join-Path $TemporaryDirectory ("{0}.stdout" -f $RunId)
$StdErrFile = Join-Path $TemporaryDirectory ("{0}.stderr" -f $RunId)
$NormalizedProjectRoot = ([System.IO.Path]::GetFullPath($ProjectRoot)).TrimEnd('\', '/').ToUpperInvariant()
$Hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $Digest = $Hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($NormalizedProjectRoot))
}
finally {
    $Hasher.Dispose()
}
$RootHash = -join ($Digest[0..7] | ForEach-Object { $_.ToString('x2') })
$MutexName = "Global\SmogAI-$RootHash-$SafeTaskName"
$Mutex = $null
$HasMutex = $false
$ExitCode = 1
$Process = $null

function Write-TaskLog {
    param([string]$Level, [string]$Message)
    $Line = "{0}`t{1}`t{2}" -f ([DateTimeOffset]::UtcNow.ToString('o')), $Level, $Message
    Add-Content -LiteralPath $LogFile -Value $Line -Encoding UTF8
}

try {
    if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
        throw "Project directory does not exist: $ProjectRoot"
    }
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "Virtual-environment interpreter does not exist: $PythonExe"
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Configuration file does not exist: $ConfigPath"
    }
    if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        throw "Environment file does not exist: $EnvPath"
    }

    $CreatedNew = $false
    $Mutex = [System.Threading.Mutex]::new($false, $MutexName, [ref]$CreatedNew)
    try {
        $HasMutex = $Mutex.WaitOne(0, $false)
    }
    catch [System.Threading.AbandonedMutexException] {
        $HasMutex = $true
        Write-TaskLog -Level 'WARNING' -Message 'Recovered an abandoned Windows mutex.'
    }
    if (-not $HasMutex) {
        Write-TaskLog -Level 'INFO' -Message "Task skipped because mutex is held: $MutexName"
        exit 5
    }

    Set-Location -LiteralPath $ProjectRoot
    Write-TaskLog -Level 'INFO' -Message "task=$TaskName run_id=$RunId pid=$PID host=$env:COMPUTERNAME command=$Command started_at=$($StartedAt.ToString('o'))"
    $Arguments = @('-m', 'smog_ai', $Command, '--config', $ConfigPath, '--env-file', $EnvPath) + $CliArguments
    $Process = Start-Process -FilePath $PythonExe `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectRoot `
        -NoNewWindow `
        -RedirectStandardOutput $StdOutFile `
        -RedirectStandardError $StdErrFile `
        -PassThru

    $Completed = $Process.WaitForExit([Math]::Max(1, $TimeoutMinutes) * 60 * 1000)
    if (-not $Completed) {
        Write-TaskLog -Level 'ERROR' -Message "Timeout after $TimeoutMinutes minutes. Terminating process tree PID=$($Process.Id)."
        & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F | Out-Null
        $ExitCode = 1
    }
    else {
        $Process.Refresh()
        $ExitCode = $Process.ExitCode
    }

    if (Test-Path -LiteralPath $StdOutFile) {
        Add-Content -LiteralPath $LogFile -Value "--- STDOUT ---" -Encoding UTF8
        Get-Content -LiteralPath $StdOutFile -Encoding UTF8 | Add-Content -LiteralPath $LogFile -Encoding UTF8
    }
    if (Test-Path -LiteralPath $StdErrFile) {
        Add-Content -LiteralPath $LogFile -Value "--- STDERR ---" -Encoding UTF8
        Get-Content -LiteralPath $StdErrFile -Encoding UTF8 | Add-Content -LiteralPath $LogFile -Encoding UTF8
    }
    $FinishedAt = [DateTimeOffset]::UtcNow
    $Duration = [Math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 3)
    Write-TaskLog -Level $(if ($ExitCode -eq 0) { 'INFO' } else { 'ERROR' }) `
        -Message "task=$TaskName run_id=$RunId finished_at=$($FinishedAt.ToString('o')) duration_seconds=$Duration exit_code=$ExitCode"
}
catch {
    $ExitCode = 1
    try { Write-TaskLog -Level 'ERROR' -Message $_.Exception.ToString() } catch { }
}
finally {
    Remove-Item -LiteralPath $StdOutFile, $StdErrFile -Force -ErrorAction SilentlyContinue
    if ($HasMutex -and $null -ne $Mutex) {
        try { $Mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $Mutex) { $Mutex.Dispose() }
}

exit $ExitCode
