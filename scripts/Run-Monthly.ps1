[CmdletBinding()]
param([string]$ProjectRoot, [string]$RuntimeRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
& (Join-Path $PSScriptRoot 'Invoke-SmogAiTask.ps1') -TaskName 'Monthly-Backup' -Command 'monthly-maintenance' -TimeoutMinutes 240 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
exit $LASTEXITCODE
