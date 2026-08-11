[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string[]]$PytestArguments = @('-q')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

$RuntimeVariableNames = @(
    @(
        Get-ChildItem Env: |
            Where-Object {
                $_.Name -like 'SMOG_AI_*' -or
                $_.Name -like 'SPACES_*' -or
                $_.Name -like 'LANGFUSE_*' -or
                $_.Name -like 'AWS_*' -or
                $_.Name -in @(
                    'DISPLAY_TIMEZONE',
                    'PUBLISH_API_URL',
                    'PUBLISH_API_TOKEN',
                    'LLM_API_KEY',
                    'OPENAI_API_KEY',
                    'PYTHONPATH',
                    'PYTHONHOME'
                )
            } |
            Select-Object -ExpandProperty Name
    ) + @('SMOG_AI_ENV') | Select-Object -Unique
)


$SavedEnvironment = @{}
$BaseTemp = $null
$LocationPushed = $false
try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    if (-not $RuntimeRoot) {
        $RuntimeRoot = Get-SmogAiDefaultRuntimeRoot
    }
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    Push-Location -LiteralPath $ProjectRoot
    $LocationPushed = $true

    foreach ($Name in $RuntimeVariableNames) {
        $SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
        Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
    }
    $SavedEnvironment['PYTHONNOUSERSITE'] = [Environment]::GetEnvironmentVariable('PYTHONNOUSERSITE', 'Process')
    $env:PYTHONNOUSERSITE = '1'
    $env:SMOG_AI_ENV = 'test'

    # Use a short base directory.  On Windows, deeply nested pytest paths plus
    # atomic object-store temporary names can otherwise cross MAX_PATH even
    # when the final artifact path itself is valid.
    $BaseTempToken = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $BaseTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("sai-t-{0}" -f $BaseTempToken)
    New-Item -ItemType Directory -Path $BaseTemp -Force | Out-Null

    Write-Host 'Sprawdzam bezwzględną blokadę bazy produkcyjnej w environment=test...' -ForegroundColor Cyan
    $GuardDatabase = Join-Path $BaseTemp 'guard-production.db'
    $env:SMOG_AI_DATABASE_URL = 'sqlite:///C:/ProgramData/SmogAI/data/smog.db'
    $GuardCode = @'
from pathlib import Path
import os
from smog_ai.config import AppConfig, PathsConfig

root = Path(os.environ["SMOG_AI_GUARD_TEMP"])
paths = PathsConfig(
    data_dir=root / "data",
    database_path=root / "data" / "isolated.db",
    models_dir=root / "models",
    snapshots_dir=root / "snapshots",
    logs_dir=root / "logs",
    backups_dir=root / "backups",
    temp_dir=root / "tmp",
    imgw_metadata_csv=root / "imgw.csv",
)
config = AppConfig(environment="test", paths=paths)
assert "ProgramData/SmogAI" not in config.database_url.replace("\\", "/"), config.database_url
assert config.database_url.endswith("/isolated.db"), config.database_url
print(config.database_url)
'@
    $env:SMOG_AI_GUARD_TEMP = $BaseTemp
    $GuardCode | & $PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Guard bazy testowej zakończył się kodem $LASTEXITCODE."
    }
    Remove-Item Env:SMOG_AI_DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:SMOG_AI_GUARD_TEMP -ErrorAction SilentlyContinue

    Write-Host 'Uruchamiam pytest w środowisku odseparowanym od C:\ProgramData\SmogAI...' -ForegroundColor Cyan
    $Arguments = @('-m', 'pytest') + $PytestArguments + @('--basetemp', $BaseTemp)
    & $PythonExe @Arguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "pytest zakończył się kodem $ExitCode."
    }

    Write-Host 'Testy przeszły w odizolowanym środowisku.' -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Remove-Item Env:SMOG_AI_GUARD_TEMP -ErrorAction SilentlyContinue
    foreach ($Name in $RuntimeVariableNames + @('PYTHONNOUSERSITE')) {
        $OldValue = $SavedEnvironment[$Name]
        if ($null -eq $OldValue) {
            Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
        }
        else {
            [Environment]::SetEnvironmentVariable($Name, [string]$OldValue, 'Process')
        }
    }
    if ($BaseTemp -and (Test-Path -LiteralPath $BaseTemp)) {
        Remove-Item -LiteralPath $BaseTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($LocationPushed) {
        Pop-Location -ErrorAction SilentlyContinue
    }
}
