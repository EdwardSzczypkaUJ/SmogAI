[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string]$EnvFile,
    [string]$ListenAddress = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 8501
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')
try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    if (-not $EnvFile) { $EnvFile = Join-Path $RuntimeRoot 'server-local.env' }
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    $DashboardFile = Join-Path $ProjectRoot 'server\dashboard\app.py'
    Import-SmogAiEnvFile -Path $EnvFile
    if (-not $env:SMOG_AI_DASHBOARD_API_URL) { $env:SMOG_AI_DASHBOARD_API_URL = 'http://127.0.0.1:8000/api/v1' }
    Set-Location -LiteralPath $ProjectRoot
    Write-Host "Dashboard: http://${ListenAddress}:$Port" -ForegroundColor Green
    Write-Host "API: $env:SMOG_AI_DASHBOARD_API_URL" -ForegroundColor DarkGray
    & $PythonExe -m streamlit run $DashboardFile --server.address $ListenAddress --server.port $Port --server.headless true --browser.gatherUsageStats false
    exit $LASTEXITCODE
}
catch { Write-Error $_; exit 1 }
