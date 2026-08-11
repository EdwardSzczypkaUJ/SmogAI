[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string]$PythonExecutable,
    [ValidateSet('auto','3.12','3.13')][string]$PreferredPythonVersion = 'auto',
    [switch]$NoAutomaticPythonInstall,
    [switch]$RecreateVenv,
    [switch]$InstallDevelopmentDependencies,
    [switch]$SkipMigrations
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Move-ExistingVenvToBackup {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$VenvPath)

    if (-not (Test-Path -LiteralPath $VenvPath)) { return $null }
    $Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $BackupPath = "$VenvPath.backup-$Timestamp"
    if (Test-Path -LiteralPath $BackupPath) {
        $BackupPath = "$BackupPath-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
    }
    Move-Item -LiteralPath $VenvPath -Destination $BackupPath
    Write-Warning "Poprzednie środowisko przeniesiono do: $BackupPath"
    return $BackupPath
}

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    Initialize-SmogAiRuntimeDirectories $RuntimeRoot
    Set-Location -LiteralPath $ProjectRoot

    $VenvPath = Join-Path $ProjectRoot '.venv'
    $PythonExe = Join-Path $VenvPath 'Scripts\python.exe'
    $CreateVenv = $true

    if ((Test-Path -LiteralPath $PythonExe -PathType Leaf) -and -not $RecreateVenv) {
        $VenvInfo = Get-SmogAiPythonInfo -Executable $PythonExe
        if ($VenvInfo -and $VenvInfo.Bits -eq 64 -and (Test-SmogAiSupportedPythonVersion $VenvInfo.Version)) {
            $CreateVenv = $false
            Write-Host "Używam istniejącego .venv: Python $($VenvInfo.Version) x64" -ForegroundColor Green
        }
        else {
            Write-Warning 'Istniejące .venv jest uszkodzone, 32-bitowe albo ma niewspieraną wersję Pythona.'
            Move-ExistingVenvToBackup -VenvPath $VenvPath | Out-Null
        }
    }
    elseif ((Test-Path -LiteralPath $VenvPath) -and ($RecreateVenv -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf))) {
        Move-ExistingVenvToBackup -VenvPath $VenvPath | Out-Null
    }

    if ($CreateVenv) {
        $BootstrapPython = Find-SmogAiBootstrapPython `
            -PythonExecutable $PythonExecutable `
            -PreferredVersion $PreferredPythonVersion `
            -NoAutomaticInstall:$NoAutomaticPythonInstall
        Write-Host "Tworzę .venv przez $($BootstrapPython.DisplayName) — Python $($BootstrapPython.Version) x64..." -ForegroundColor Cyan
        & $BootstrapPython.Executable @($BootstrapPython.PrefixArguments) -m venv $VenvPath
        if ($LASTEXITCODE -ne 0) { throw 'Nie udało się utworzyć środowiska .venv.' }
    }

    $VenvInfo = Get-SmogAiPythonInfo -Executable $PythonExe
    if (-not $VenvInfo) {
        throw "Nie można uruchomić interpretera w .venv: $PythonExe"
    }
    if ($VenvInfo.Bits -ne 64 -or -not (Test-SmogAiSupportedPythonVersion $VenvInfo.Version)) {
        throw "Środowisko .venv używa Pythona $($VenvInfo.Version) $($VenvInfo.Bits)-bit. Wymagany jest Python 3.12 lub 3.13 x64."
    }
    Write-Host "Środowisko projektu: Python $($VenvInfo.Version) x64 ($PythonExe)" -ForegroundColor Green

    & $PythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Aktualizacja pip nie powiodła się.' }
    & $PythonExe -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Instalacja zależności nie powiodła się.' }
    if ($InstallDevelopmentDependencies) {
        & $PythonExe -m pip install -r (Join-Path $ProjectRoot 'requirements-dev.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Instalacja zależności developerskich nie powiodła się.' }
    }

    $ConfigPath = Join-Path $RuntimeRoot 'config.yaml'
    $EnvPath = Join-Path $RuntimeRoot 'smog-ai.env'
    $ServerEnvPath = Join-Path $RuntimeRoot 'server-local.env'
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Copy-Item (Join-Path $ProjectRoot 'config.example.yaml') $ConfigPath
    }
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        Copy-Item (Join-Path $ProjectRoot '.env.example') $EnvPath
    }
    if (-not (Test-Path -LiteralPath $ServerEnvPath)) {
        Copy-Item (Join-Path $ProjectRoot '.env.server.local.example') $ServerEnvPath
    }

    $env:SMOG_AI_CONFIG = $ConfigPath
    $env:SMOG_AI_ENV_FILE = $EnvPath
    Import-SmogAiEnvFile -Path $EnvPath -AllowMissing
    if (-not $SkipMigrations) {
        & $PythonExe -m alembic -c (Join-Path $ProjectRoot 'alembic.ini') upgrade head
        if ($LASTEXITCODE -ne 0) { throw 'Migracja Alembic nie powiodła się.' }
    }
    Write-Host "Instalacja lokalna zakończona. Projekt: $ProjectRoot" -ForegroundColor Green
    Write-Host "Dane uruchomieniowe: $RuntimeRoot" -ForegroundColor Green
    Write-Host "Python projektu: $($VenvInfo.Version) x64" -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
