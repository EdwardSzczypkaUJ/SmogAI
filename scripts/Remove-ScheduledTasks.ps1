[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param([switch]$RemoveData, [string]$RuntimeRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')
$TaskPath = '\SmogAI\'
foreach ($Task in (Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)) {
    if ($PSCmdlet.ShouldProcess("$TaskPath$($Task.TaskName)", 'Usuń zadanie')) {
        Unregister-ScheduledTask -TaskName $Task.TaskName -TaskPath $TaskPath -Confirm:$false
    }
}
if ($RemoveData) {
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    if ($PSCmdlet.ShouldProcess($RuntimeRoot, 'Trwale usuń bazę, modele, snapshoty, logi i kopie')) {
        Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
    }
}
Write-Host 'Usunięto wyłącznie zadania z \SmogAI\. Dane zachowano, chyba że użyto -RemoveData.'
exit 0
