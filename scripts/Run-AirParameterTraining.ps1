[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [Parameter(Mandatory = $true)]
    [string]$Parameters,

    [ValidateSet('quick', 'full')]
    [string]$Profile = 'quick',

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
                    $_.CommandLine -like '*first-run*'
                )
            }
    )
    if ($Running.Count -gt 0) {
        $Running | Select-Object ProcessId, Name, CommandLine | Format-List
        throw 'Inny proces treningowy Smog AI jest aktywny.'
    }

    Set-Location -LiteralPath $ProjectRoot
    Write-Host (
        'Trening profilu {0} dla: {1}; snapshot={2}' -f
        $Profile,
        $Parameters,
        $Snapshot
    ) -ForegroundColor Cyan
    Write-Host (
        'Importer może nadal pracować; trening czyta niezmienny dataset_id.'
    ) -ForegroundColor DarkGray

    & $Python -m smog_ai snapshot-train-hourly `
        --profile $Profile `
        --targets $Parameters `
        --snapshot $Snapshot `
        --config $Config `
        --env-file $EnvFile

    $Code = $LASTEXITCODE
    if ($Code -ne 0) {
        throw "snapshot-train-hourly zakończył się kodem $Code."
    }

    & (Join-Path $ProjectRoot 'scripts\Show-ParameterCatalog.ps1') `
        -ProjectRoot $ProjectRoot `
        -RuntimeRoot $RuntimeRoot

    exit 0
}
catch {
    Write-Error $_
    exit 1
}
