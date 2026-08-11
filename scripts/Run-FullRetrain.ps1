[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [ValidateSet('auto', 'latest', 'live')]
    [string]$Snapshot = 'auto'
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

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Config,
        $EnvFile
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    # Import/backfill is intentionally allowed. Snapshot training reads an
    # immutable SQLite Online Backup copy. Only another training/model mutation
    # is blocked here.
    $Running = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -like "*$ProjectRoot*" -and
                (
                    $_.CommandLine -like '*snapshot-train-hourly*' -or
                    $_.CommandLine -like '*train-hourly*' -or
                    $_.CommandLine -like '*quick-retrain*' -or
                    $_.CommandLine -like '*full-retrain*' -or
                    $_.CommandLine -like '*first-run*' -or
                    $_.CommandLine -like '*weekly-maintenance*' -or
                    $_.CommandLine -like '*monthly-maintenance*'
                )
            }
    )
    if ($Running.Count -gt 0) {
        $Running |
            Select-Object ProcessId, Name, CommandLine |
            Format-List
        throw 'Inny proces treningowy Smog AI jest aktywny.'
    }

    Set-Location -LiteralPath $ProjectRoot

    Write-Host 'PEŁNY RETRAINING — profil full na niezmiennym snapshocie' -ForegroundColor Cyan
    Write-Host (
        'Importer może nadal działać. Monitor: scripts\Watch-TrainingProgress.ps1 -Mode full'
    ) -ForegroundColor DarkGray

    & $Python -m smog_ai snapshot-train-hourly `
        --profile full `
        --snapshot $Snapshot `
        --config $Config `
        --env-file $EnvFile

    exit $LASTEXITCODE

}
catch {
    Write-Error $_
    exit 1
}
