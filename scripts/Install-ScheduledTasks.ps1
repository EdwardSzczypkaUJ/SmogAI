[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [PSCredential]$Credential,
    [switch]$WakeComputer,
    [switch]$RunSmokeTest
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    if (-not (Test-SmogAiAdministrator)) {
        throw 'Uruchom skrypt z podwyższonego PowerShella. Administrator jest potrzebny tylko do rejestracji zadań i ACL.'
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'Wymagany PowerShell 5.1 lub 7.' }
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    Initialize-SmogAiRuntimeDirectories $RuntimeRoot
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $ConfigPath = Join-Path $RuntimeRoot 'config.yaml'
    $EnvPath = Join-Path $RuntimeRoot 'smog-ai.env'
    foreach ($Required in @($ConfigPath, $EnvPath)) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) { throw "Brak pliku: $Required" }
    }

    $TaskPath = '\SmogAI\'
    $BackupDirectory = Join-Path $RuntimeRoot 'task-backups'
    $GeneratedXmlDirectory = Join-Path $RuntimeRoot 'scheduled-task-definitions'
    New-Item -ItemType Directory -Path $BackupDirectory, $GeneratedXmlDirectory -Force | Out-Null
    $Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Definitions = @(
        [ordered]@{ Name='Hourly Pipeline'; Script='Run-Hourly.ps1'; Timeout=45; Type='hourly' },
        [ordered]@{ Name='Daily Maintenance'; Script='Run-Daily.ps1'; Timeout=120; Type='daily' },
        [ordered]@{ Name='Weekly Training'; Script='Run-WeeklyTraining.ps1'; Timeout=480; Type='weekly' },
        [ordered]@{ Name='Monthly Backup'; Script='Run-Monthly.ps1'; Timeout=240; Type='monthly' }
    )
    foreach ($Definition in $Definitions) {
        $Existing = Get-ScheduledTask -TaskName $Definition.Name -TaskPath $TaskPath -ErrorAction SilentlyContinue
        if ($Existing) {
            $SafeName = $Definition.Name -replace '[^A-Za-z0-9_-]', '-'
            Export-ScheduledTask -TaskName $Definition.Name -TaskPath $TaskPath | Out-File `
                -LiteralPath (Join-Path $BackupDirectory "$SafeName-$Timestamp.xml") -Encoding utf8
        }
    }

    $PowerShellExe = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) {
        (Get-Command pwsh.exe).Source
    } else {
        "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    }
    $Principal = if ($Credential) {
        New-ScheduledTaskPrincipal -UserId $Credential.UserName -LogonType Password -RunLevel Limited
    } else {
        New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    }

    function New-MonthlyTrigger {
        $Trigger = New-CimInstance -ClassName MSFT_TaskMonthlyTrigger -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClientOnly
        $Trigger.Enabled = $true
        $Trigger.StartBoundary = ([DateTime]::Today.AddMonths(1).AddDays(1 - [DateTime]::Today.AddMonths(1).Day).AddHours(4).AddMinutes(15)).ToString('s')
        $Trigger.DaysOfMonth = [uint32[]]@(1)
        $Trigger.MonthsOfYear = [uint16]4095
        return $Trigger
    }

    foreach ($Definition in $Definitions) {
        $ScriptPath = Join-Path (Join-Path $ProjectRoot 'scripts') $Definition.Script
        if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw "Brak skryptu zadania: $ScriptPath" }
        $Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPath`" -ProjectRoot `"$ProjectRoot`" -RuntimeRoot `"$RuntimeRoot`""
        $Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments -WorkingDirectory $ProjectRoot
        $Trigger = switch ($Definition.Type) {
            'hourly' {
                $Start = [DateTime]::Today.AddMinutes(7)
                if ($Start -lt (Get-Date)) { $Start = $Start.AddHours(1) }
                New-ScheduledTaskTrigger -Once -At $Start -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
            }
            'daily' { New-ScheduledTaskTrigger -Daily -At '02:35' }
            'weekly' { New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At '03:20' }
            'monthly' { New-MonthlyTrigger }
        }
        $Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) -ExecutionTimeLimit (New-TimeSpan -Minutes $Definition.Timeout) -WakeToRun:$WakeComputer -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "GIOŚ/IMGW Forecast Suite 1.7.0 — $($Definition.Name)"
        if ($Credential) {
            Register-ScheduledTask -TaskName $Definition.Name -TaskPath $TaskPath -InputObject $Task -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -Force | Out-Null
        } else {
            Register-ScheduledTask -TaskName $Definition.Name -TaskPath $TaskPath -InputObject $Task -Force | Out-Null
        }
        $SafeName = $Definition.Name -replace '[^A-Za-z0-9_-]', '-'
        Export-ScheduledTask -TaskName $Definition.Name -TaskPath $TaskPath | Out-File -LiteralPath (Join-Path $GeneratedXmlDirectory "$SafeName.xml") -Encoding utf8
    }

    if ($Credential) {
        & icacls.exe $RuntimeRoot /grant "$($Credential.UserName):(OI)(CI)M" /T /C | Out-Null
        & icacls.exe $ProjectRoot /grant "$($Credential.UserName):(OI)(CI)RX" /T /C | Out-Null
    }
    Write-Host "Zadania zainstalowano dla projektu: $ProjectRoot" -ForegroundColor Green
    Get-ScheduledTask -TaskPath $TaskPath | Select-Object TaskPath, TaskName, State | Format-Table -AutoSize
    if (-not $Credential) {
        Write-Warning 'Bez -Credential zadania używają logowania Interactive. Dla produkcji podaj konto usługowe.'
    }
    if ($RunSmokeTest) {
        & $PythonExe -m smog_ai healthcheck --config $ConfigPath --env-file $EnvPath
        if ($LASTEXITCODE -notin @(0,1)) { throw "Healthcheck zakończył się kodem $LASTEXITCODE" }
    }
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
