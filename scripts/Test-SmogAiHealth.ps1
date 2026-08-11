[CmdletBinding()]
param([switch]$AsJson, [string]$ProjectRoot, [string]$RuntimeRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')
$ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
$RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
$PythonExe = Get-SmogAiPythonExe $ProjectRoot
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
$PythonJson = & $PythonExe -m smog_ai healthcheck --json --config $Config --env-file $EnvFile 2>&1
$PythonExit = $LASTEXITCODE
$Tasks = @()
try {
    $Tasks = Get-ScheduledTask -TaskPath '\SmogAI\' -ErrorAction Stop | ForEach-Object {
        $Info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath
        [ordered]@{ name=$_.TaskName; state=$_.State.ToString(); last_run_time=$Info.LastRunTime; last_task_result=$Info.LastTaskResult; next_run_time=$Info.NextRunTime }
    }
}
catch { $Tasks = @([ordered]@{ error=$_.Exception.Message }) }
$Result = [ordered]@{ project_root=$ProjectRoot; runtime_root=$RuntimeRoot; python_exit_code=$PythonExit; python_health=($PythonJson -join "`n"); scheduled_tasks=$Tasks }
if ($AsJson) { $Result | ConvertTo-Json -Depth 10 } else { Write-Host ($PythonJson -join "`n"); $Tasks | Format-Table -AutoSize | Out-String | Write-Host }
exit $PythonExit
