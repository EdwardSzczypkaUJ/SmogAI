[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8504)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$Stopped = @()
$ProcessIds = @(
    $Connections |
        ForEach-Object { $_.OwningProcess } |
        Sort-Object -Unique
)
foreach ($ProcessId in $ProcessIds) {
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $Process) { continue }
    $CommandLine = [string]$Process.CommandLine
    if ($CommandLine -notmatch 'smog_ai_automation_monitor\.py') {
        throw "Port $Port zajmuje inny proces. PID=$ProcessId; proces nie zostal zatrzymany."
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    $Stopped += $ProcessId
}
Start-Sleep -Seconds 2
$Listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
[pscustomobject]@{
    Status = if ($Listening.Count -eq 0) { 'MONITOR_STOPPED' } else { 'MONITOR_STILL_LISTENING' }
    Port = $Port
    StoppedProcesses = $Stopped
    Ready = ($Listening.Count -gt 0)
    ChromeWasClosed = $false
    ServingTaskModified = $false
} | Format-List
