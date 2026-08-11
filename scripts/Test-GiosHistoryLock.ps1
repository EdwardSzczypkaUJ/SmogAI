[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Path in @($Python, $Config, $EnvFile)) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Path"
        }
    }

    $Running = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -like '*backfill-gios-history*' -and
                $_.CommandLine -like "*$ProjectRoot*"
            }
    )

    if ($Running.Count -gt 0) {
        $Running |
            Select-Object ProcessId, ParentProcessId, Name, CommandLine |
            Format-List
        throw 'Importer historii GIOŚ rzeczywiście nadal działa.'
    }

    $Probe = @'
import json
import sys
from pathlib import Path

from smog_ai.config import load_config
from smog_ai.database.engine import create_db_engine, init_database
from smog_ai.locking import ProcessLease

config = load_config(Path(sys.argv[1]), Path(sys.argv[2]))
engine = create_db_engine(config)
init_database(engine)

with ProcessLease(engine, config, "gios-history-backfill") as lease:
    payload = {
        "status": "acquired_and_released",
        "lock_name": lease.lock_name,
        "process_id": lease.process_id,
        "host_name": lease.host_name,
        "windows_mutex": lease.mutex.name,
        "abandoned_mutex_recovered": bool(lease.mutex.abandoned),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
'@

    $Output = (
        $Probe |
            & $Python - $Config $EnvFile |
            Out-String
    ).Trim()

    if ($LASTEXITCODE -ne 0) {
        throw 'Próba przejęcia i zwolnienia blokady zakończyła się błędem.'
    }

    Write-Host $Output
    Write-Host ''
    Write-Host 'BLOKADA GIOŚ JEST WOLNA I GOTOWA DO PONOWNEGO IMPORTU.' -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
