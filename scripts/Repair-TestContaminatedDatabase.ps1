[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [switch]$Rebuild,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    if (-not $RuntimeRoot) {
        $RuntimeRoot = Get-SmogAiDefaultRuntimeRoot
    }
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $ConfigPath = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
    $ToolPath = Join-Path $ProjectRoot 'scripts\audit_and_rebuild_test_contaminated_db.py'
    $ReportDirectory = Join-Path $RuntimeRoot 'logs\recovery'
    New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
    $ReportPath = Join-Path $ReportDirectory ("test-leak-audit-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

    foreach ($Required in @($PythonExe, $ConfigPath, $EnvFile, $ToolPath)) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Arguments = @(
        $ToolPath,
        '--project-root', $ProjectRoot,
        '--config', $ConfigPath,
        '--env-file', $EnvFile,
        '--output', $ReportPath
    )

    if ($Rebuild) {
        if (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) {
            $RunningTasks = @(Get-ScheduledTask -TaskPath '\SmogAI\' -ErrorAction SilentlyContinue |
                Where-Object { $_.State -eq 'Running' })
            if ($RunningTasks.Count -gt 0) {
                throw 'Co najmniej jedno zadanie SmogAI jest uruchomione. Zatrzymaj je przed odbudową bazy.'
            }
        }

        $Target = Join-Path $RuntimeRoot 'data\smog.db'
        if (-not $PSCmdlet.ShouldProcess($Target, 'Wykonaj spójny backup, zachowaj oryginał i utwórz czystą bazę')) {
            Write-Host 'Odbudowa anulowana.' -ForegroundColor Yellow
            exit 0
        }
        $Arguments += '--rebuild'
        if ($Force) {
            $Arguments += '--force'
        }
    }

    & $PythonExe @Arguments
    $ExitCode = $LASTEXITCODE

    Write-Host "`nRaport: $ReportPath" -ForegroundColor Cyan
    switch ($ExitCode) {
        0 {
            if ($Rebuild) {
                Write-Host 'Baza została bezpiecznie odbudowana. Konfiguracja i klucze Spaces pozostały bez zmian.' -ForegroundColor Green
            }
            else {
                Write-Host 'Nie wykryto znaczników danych testowych w bazie produkcyjnej.' -ForegroundColor Green
            }
        }
        4 {
            Write-Warning 'Wykryto znaczniki pytest w bazie produkcyjnej. Uruchom ponownie z -Rebuild.'
        }
        default {
            Write-Error "Audyt/naprawa bazy zakończyła się kodem $ExitCode. Zobacz raport: $ReportPath"
        }
    }
    exit $ExitCode
}
catch {
    Write-Error $_
    exit 1
}
