[CmdletBinding()]
param(
    [ValidateSet('daily','weekly','monthly')][string]$Tier = 'daily',
    [string]$ProjectRoot,
    [string]$RuntimeRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
& (Join-Path $PSScriptRoot 'Invoke-SmogAiTask.ps1') -TaskName "Backup-$Tier" -Command 'backup' -TimeoutMinutes 120 -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot -CliArguments @('--tier', $Tier)
exit $LASTEXITCODE
