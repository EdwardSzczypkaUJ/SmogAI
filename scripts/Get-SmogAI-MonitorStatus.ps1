[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [ValidateRange(1024,65535)][int]$Port = 8504
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Url = "http://127.0.0.1:$Port"
$Connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$ProcessIds = @(
    $Connections |
        ForEach-Object { $_.OwningProcess } |
        Sort-Object -Unique
)
$Rows = foreach ($ProcessId in $ProcessIds) {
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    [pscustomobject]@{
        ProcessId = $ProcessId
        Name = if ($null -ne $Process) { $Process.Name } else { $null }
    }
}
$Ready = $false
try {
    $Ready = (Invoke-WebRequest -UseBasicParsing -Uri "$Url/_stcore/health" -TimeoutSec 3).StatusCode -eq 200
} catch { $Ready = $false }
$Current = Join-Path $RuntimeRoot 'logs\automation\current.json'
$Run = $null
if (Test-Path -LiteralPath $Current -PathType Leaf) {
    try { $Run = Get-Content -LiteralPath $Current -Raw | ConvertFrom-Json } catch { $Run = $null }
}

function Get-OptionalProperty {
    param([object]$InputObject, [string]$Name)
    if ($null -eq $InputObject) { return $null }
    $Property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $Property) { return $null }
    return $Property.Value
}

$RunId = Get-OptionalProperty -InputObject $Run -Name 'run_id'
$RunStatus = Get-OptionalProperty -InputObject $Run -Name 'status'
if ($null -eq $RunStatus) {
    $RunStatus = Get-OptionalProperty -InputObject $Run -Name 'run_status'
}
$OverallPercent = Get-OptionalProperty -InputObject $Run -Name 'overall_percent'
$CurrentStage = Get-OptionalProperty -InputObject $Run -Name 'current_stage'
[pscustomobject]@{
    Status = if ($Ready) { 'MONITOR_READY' } else { 'MONITOR_STOPPED' }
    Url = $Url
    Ready = $Ready
    Processes = @($Rows | ForEach-Object { $_.ProcessId })
    RunId = $RunId
    RunStatus = $RunStatus
    OverallPercent = $OverallPercent
    CurrentStage = $CurrentStage
    FilesModified = $false
} | Format-List
