[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProjectRoot,[string]$RuntimeRoot='C:\ProgramData\SmogAI',[string]$TaskPrefix='SmogAI-HF21')
$ErrorActionPreference='Stop'
$Script=Join-Path ([IO.Path]::GetFullPath($ProjectRoot)) 'scripts\Start-SmogAI-Automation.ps1'
if(-not (Test-Path $Script)){throw "Najpierw zainstaluj automat: $Script"}
function Register-One([string]$Name,[string]$Profile,[string]$Schedule,[string]$Time){
    $taskName="$TaskPrefix-$Name"
    $tr="powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Script`" -ProjectRoot `"$ProjectRoot`" -RuntimeRoot `"$RuntimeRoot`" -Profile $Profile"
    & schtasks.exe /Create /TN $taskName /TR $tr /SC $Schedule /ST $Time /F | Out-Null
    if($LASTEXITCODE -ne 0){throw "Nie udało się utworzyć zadania $taskName"}
    Write-Host "Utworzono: $taskName"
}
Register-One 'Quick-Hourly' 'quick' 'HOURLY' '00:07'
Register-One 'Normal-00' 'normal' 'DAILY' '00:35'
Register-One 'Normal-06' 'normal' 'DAILY' '06:35'
Register-One 'Normal-12' 'normal' 'DAILY' '12:35'
Register-One 'Normal-18' 'normal' 'DAILY' '18:35'
Register-One 'Full-Daily' 'full' 'DAILY' '02:15'
