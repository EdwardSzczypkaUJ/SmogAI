[CmdletBinding()]
param(
    [string]$RuntimeRoot='C:\ProgramData\SmogAI',
    [Parameter(Mandatory=$true)][string]$RunId
)
$ErrorActionPreference='Stop'
$RunDir=Join-Path $RuntimeRoot "logs\automation\runs\$RunId"
$Status=Join-Path $RunDir 'run.json'
$Temporary=Join-Path $RunDir 'run.json.tmp'
if(-not (Test-Path -LiteralPath $Status)){throw "Brak checkpointu: $Status"}
if(-not (Test-Path -LiteralPath $Temporary)){
    Write-Host 'Brak pliku tymczasowego — istniejący run.json pozostaje checkpointem do wznowienia.' -ForegroundColor Yellow
    return
}
$Current=Get-Content -LiteralPath $Status -Raw -Encoding UTF8 | ConvertFrom-Json
$Candidate=Get-Content -LiteralPath $Temporary -Raw -Encoding UTF8 | ConvertFrom-Json
$CurrentTime=[datetimeoffset]::MinValue
$CandidateTime=[datetimeoffset]::MinValue
if($Current.updated_at){$CurrentTime=[datetimeoffset]::Parse([string]$Current.updated_at)}
if($Candidate.updated_at){$CandidateTime=[datetimeoffset]::Parse([string]$Candidate.updated_at)}
if($CandidateTime -le $CurrentTime){
    Write-Host 'run.json jest co najmniej tak nowy jak run.json.tmp — nie dokonano podmiany.' -ForegroundColor Green
    return
}
$Backup=Join-Path $RunDir ("run.json.before-recovery-"+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.bak')
Copy-Item -LiteralPath $Status -Destination $Backup -Force
$Bytes=[IO.File]::ReadAllBytes($Temporary)
$LastError=$null
for($Attempt=1;$Attempt -le 12;$Attempt++){
    try {
        [IO.File]::WriteAllBytes($Status,$Bytes)
        Remove-Item -LiteralPath $Temporary -Force
        Write-Host "Odzyskano nowszy checkpoint. Backup: $Backup" -ForegroundColor Green
        return
    } catch {
        $LastError=$_
        Start-Sleep -Milliseconds ([math]::Min(2000,100*[math]::Pow(2,$Attempt-1)))
    }
}
throw "Nie udało się odzyskać checkpointu z powodu trwałej blokady pliku. $LastError"
