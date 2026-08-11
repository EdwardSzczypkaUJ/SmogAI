[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string]$AppName,
    [string]$CustomerName = 'Customer',
    [Security.SecureString]$DigitalOceanAccessToken,
    [switch]$TriggerDeployment,
    [switch]$WatchDeployment
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Set-GhSecretFromValue([string]$Name, [string]$Value) {
    if (-not $Value) { return }
    $Value | & gh secret set $Name
    if ($LASTEXITCODE -ne 0) { throw "Nie udało się ustawić GitHub secret $Name" }
}
function Set-GhVariable([string]$Name, [string]$Value) {
    if (-not $Value) { return }
    & gh variable set $Name --body $Value
    if ($LASTEXITCODE -ne 0) { throw "Nie udało się ustawić GitHub variable $Name" }
}

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    if (-not (Get-Command gh.exe -ErrorAction SilentlyContinue) -and
        -not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'Zainstaluj GitHub CLI (gh) i wykonaj gh auth login.'
    }
    Set-Location -LiteralPath $ProjectRoot
    & gh auth status
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI nie jest zalogowany. Wykonaj gh auth login.' }
    & gh repo view --json nameWithOwner,defaultBranchRef | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Bieżący katalog nie jest połączony z repozytorium GitHub. Ustaw origin i wykonaj pierwszy push.'
    }

    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
    Import-SmogAiEnvFile -Path $EnvFile
    if (-not $AppName) {
        $AppName = Read-Host 'Nazwa aplikacji DigitalOcean App Platform'
    }
    if (-not $AppName) { throw 'Nazwa aplikacji jest wymagana.' }
    if (-not $DigitalOceanAccessToken) {
        $DigitalOceanAccessToken = Read-Host 'DigitalOcean API token dla GitHub Actions' -AsSecureString
    }
    $DoToken = ConvertFrom-SecureStringPlainText $DigitalOceanAccessToken
    if (-not $DoToken) { throw 'Token DigitalOcean jest wymagany.' }

    Set-GhSecretFromValue 'DIGITALOCEAN_ACCESS_TOKEN' $DoToken
    Set-GhSecretFromValue 'SPACES_ACCESS_KEY_ID' $env:SPACES_ACCESS_KEY_ID
    Set-GhSecretFromValue 'SPACES_SECRET_ACCESS_KEY' $env:SPACES_SECRET_ACCESS_KEY
    Set-GhSecretFromValue 'LLM_API_KEY' $env:LLM_API_KEY
    Set-GhSecretFromValue 'LANGFUSE_PUBLIC_KEY' $env:LANGFUSE_PUBLIC_KEY
    Set-GhSecretFromValue 'LANGFUSE_SECRET_KEY' $env:LANGFUSE_SECRET_KEY

    Set-GhVariable 'DIGITALOCEAN_APP_NAME' $AppName
    Set-GhVariable 'SMOG_AI_CUSTOMER_NAME' $CustomerName
    Set-GhVariable 'SPACES_BUCKET' $env:SPACES_BUCKET
    Set-GhVariable 'SPACES_REGION' $env:SPACES_REGION
    Set-GhVariable 'SPACES_ENDPOINT_URL' $env:SPACES_ENDPOINT_URL
    Set-GhVariable 'SPACES_PREFIX' $env:SPACES_PREFIX
    Set-GhVariable 'SMOG_AI_LLM_PROVIDER' $env:SMOG_AI_LLM_PROVIDER
    Set-GhVariable 'SMOG_AI_LLM_MODEL' $env:SMOG_AI_LLM_MODEL
    Set-GhVariable 'SMOG_AI_LLM_BASE_URL' $env:SMOG_AI_LLM_BASE_URL
    Set-GhVariable 'SMOG_AI_OBSERVABILITY_BACKEND' $env:SMOG_AI_OBSERVABILITY_BACKEND
    Set-GhVariable 'LANGFUSE_BASE_URL' $env:LANGFUSE_BASE_URL

    Write-Host 'Sekrety i zmienne GitHub zostały ustawione.' -ForegroundColor Green
    if ($TriggerDeployment) {
        & gh workflow run 'CI and deploy to DigitalOcean App Platform' --ref main
        if ($LASTEXITCODE -ne 0) { throw 'Nie udało się uruchomić workflow.' }
        Write-Host 'Workflow uruchomiony.' -ForegroundColor Green
        if ($WatchDeployment) {
            Start-Sleep -Seconds 3
            & gh run watch --exit-status
            if ($LASTEXITCODE -ne 0) { throw 'Workflow zakończył się błędem.' }
        }
        else {
            Write-Host 'Podgląd: gh run watch' -ForegroundColor Green
        }
    }
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
