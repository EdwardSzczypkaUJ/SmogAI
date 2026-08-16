$ProjectRoot = (Get-Location).Path

$Listeners = @(
    Get-NetTCPConnection `
        -LocalPort 8000 `
        -State Listen `
        -ErrorAction SilentlyContinue
)

if ($Listeners.Count -gt 0) {
    $ProcessIds = @(
        $Listeners |
            Select-Object -ExpandProperty OwningProcess -Unique
    )

    $Processes = @(
        foreach ($ProcessId in $ProcessIds) {
            Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId"
        }
    )

    $Processes |
        Select-Object ProcessId, Name, CommandLine |
        Format-List

    $Unexpected = @(
        $Processes |
            Where-Object {
                $_.CommandLine -notmatch 'uvicorn|server\.api\.main'
            }
    )

    if ($Unexpected.Count -gt 0) {
        throw 'Port 8000 zajmuje proces, który nie wygląda jak API SmogAI. Nie został zatrzymany.'
    }

    $Processes | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
    }

    Start-Sleep -Seconds 2
}

$StillListening = Get-NetTCPConnection `
    -LocalPort 8000 `
    -State Listen `
    -ErrorAction SilentlyContinue

if ($StillListening) {
    throw 'Port 8000 nadal jest zajęty.'
}

Start-Process powershell.exe -ArgumentList @(
    '-NoExit',
    '-ExecutionPolicy', 'Bypass',
    '-File', "`"$ProjectRoot\scripts\Start-LocalApi.ps1`"",
    '-ProjectRoot', "`"$ProjectRoot`"",
    '-RuntimeRoot', '"C:\ProgramData\SmogAI"',
    '-UseLocalServingStore'
)