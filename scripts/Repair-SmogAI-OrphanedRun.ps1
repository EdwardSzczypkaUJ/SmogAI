[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [Parameter(Mandatory=$true)][string]$RunId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RunPath = Join-Path $RuntimeRoot "logs\automation\runs\$RunId\run.json"
if (-not (Test-Path -LiteralPath $RunPath -PathType Leaf)) { throw "Brak przebiegu: $RunPath" }
$Processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match [regex]::Escape($RunId)
})
if ($Processes.Count -gt 0) {
    throw "Przebieg nadal ma aktywny proces: $($Processes.ProcessId -join ', ')"
}
$State = Get-Content -LiteralPath $RunPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($State.status -notin @('running','pending')) {
    Write-Host "Przebieg ma juz status koncowy: $($State.status)" -ForegroundColor Yellow
    return
}
$Backup = "$RunPath.before-orphan-repair-$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
Copy-Item -LiteralPath $RunPath -Destination $Backup -Force
function Set-StateProperty([object]$Object, [string]$Name, $Value) {
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}
$FinishedAt = [DateTimeOffset]::Now.ToString('o')
Set-StateProperty $State 'status' 'interrupted'
Set-StateProperty $State 'finished_at' $FinishedAt
Set-StateProperty $State 'updated_at' $FinishedAt
Set-StateProperty $State 'error' 'Proces automatu zniknal przed zakonczeniem. Przy wznowieniu zakonczone etapy zostana pominiete.'
$Temp = "$RunPath.$PID.$([guid]::NewGuid().ToString('N')).tmp"
$State | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $Temp -Encoding UTF8
[IO.File]::Replace($Temp, $RunPath, $Backup, $true)
Write-Host "Oznaczono osierocony przebieg jako interrupted. Backup: $Backup" -ForegroundColor Green
