[CmdletBinding()]
param([string]$ProjectRoot, [string]$RuntimeRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
& (Join-Path $PSScriptRoot 'Invoke-SmogAiTask.ps1') -TaskName 'Daily-Maintenance' -Command 'daily-maintenance' -TimeoutMinutes 120 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
exit $LASTEXITCODE
