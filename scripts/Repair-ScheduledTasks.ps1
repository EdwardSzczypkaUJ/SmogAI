[CmdletBinding()]
param([string]$ProjectRoot, [string]$RuntimeRoot, [switch]$WakeComputer, [PSCredential]$Credential)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
& (Join-Path $PSScriptRoot 'Install-ScheduledTasks.ps1') -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot -WakeComputer:$WakeComputer -Credential $Credential
exit $LASTEXITCODE
