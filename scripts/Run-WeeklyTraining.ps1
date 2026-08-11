[CmdletBinding()]
param([string]$ProjectRoot, [string]$RuntimeRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
& (Join-Path $PSScriptRoot 'Invoke-SmogAiTask.ps1') -TaskName 'Weekly-Training' -Command 'weekly-maintenance' -TimeoutMinutes 480 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot
exit $LASTEXITCODE
