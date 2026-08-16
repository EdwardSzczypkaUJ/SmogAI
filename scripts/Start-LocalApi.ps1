[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string]$EnvFile,
    [string]$ListenAddress = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 8000,
    [switch]$UseLocalServingStore,
    [switch]$Reload
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
    Import-SmogAiEnvFile -Path $EnvFile
    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    if ($UseLocalServingStore) {
        # Deterministic local test mode.  These values are applied after the
        # env file, so an old Spaces prefix cannot silently select another tree.
        $env:SMOG_AI_SERVER_STORAGE_BACKEND = 'object_store'
        $env:SMOG_AI_OBJECT_STORE_BACKEND = 'local'
        $env:SMOG_AI_OBJECT_STORE_LOCAL_ROOT = Join-Path $RuntimeRoot 'object-store'
        $env:SMOG_AI_OBJECT_STORE_PREFIX = ''
        $env:SMOG_AI_SERVER_UPLOADS_ENABLED = 'false'
    }
    if (-not $env:SMOG_AI_SERVER_DATA_DIR) { $env:SMOG_AI_SERVER_DATA_DIR = Join-Path $RuntimeRoot 'server-data' }
    New-Item -ItemType Directory -Path $env:SMOG_AI_SERVER_DATA_DIR -Force | Out-Null
    Set-Location -LiteralPath $ProjectRoot
    $Arguments = @('-m','uvicorn','server.api.main:app','--host',$ListenAddress,'--port',$Port.ToString(),'--proxy-headers','--forwarded-allow-ips=127.0.0.1')
    if ($Reload) { $Arguments += '--reload' }
    Write-Host "FastAPI: http://${ListenAddress}:$Port" -ForegroundColor Green
    Write-Host "Swagger: http://${ListenAddress}:$Port/docs" -ForegroundColor Green
    Write-Host "Backend danych: $env:SMOG_AI_SERVER_STORAGE_BACKEND" -ForegroundColor DarkGray
    if ($UseLocalServingStore) {
        Write-Host "Serving root: $env:SMOG_AI_OBJECT_STORE_LOCAL_ROOT (prefix: <empty>)" -ForegroundColor DarkGray
    }
    & $PythonExe @Arguments
    exit $LASTEXITCODE
}
catch { Write-Error $_; exit 1 }
