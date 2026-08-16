[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot='C:\ProgramData\SmogAI',
    [ValidateRange(2,300)][int]$RefreshSeconds=30,
    [int]$Port=8504
)
$ErrorActionPreference='Stop'
if([string]::IsNullOrWhiteSpace($ProjectRoot)){$ProjectRoot=(Get-Location).Path}
$ProjectRoot=[IO.Path]::GetFullPath($ProjectRoot)
$Python=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Monitor=Join-Path $PSScriptRoot 'smog_ai_automation_monitor.py'
$env:SMOG_AI_DATA_ROOT=$RuntimeRoot
$env:SMOG_AI_MONITOR_REFRESH_SECONDS=[string]$RefreshSeconds
Set-Location -LiteralPath $ProjectRoot
Write-Host "Monitoring SmogAI: http://127.0.0.1:$Port; refresh=${RefreshSeconds}s"
& $Python -m streamlit run $Monitor --server.address 127.0.0.1 --server.port $Port --server.headless true
