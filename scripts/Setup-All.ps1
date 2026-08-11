[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot,
    [string]$PythonExecutable,
    [ValidateSet('auto','3.12','3.13')][string]$PreferredPythonVersion = 'auto',
    [switch]$NoAutomaticPythonInstall,
    [switch]$RecreateVenv,
    [string]$SpaceName,
    [string]$SpacesRegion = 'fra1',
    [string]$SpacesPrefix = 'smog-ai',
    [Security.SecureString]$SpacesAccessKey,
    [Security.SecureString]$SpacesSecretKey,
    [ValidateSet('openai_compatible','rule_based')][string]$LlmProvider = 'openai_compatible',
    [string]$LlmModel = 'gpt-4.1-mini',
    [string]$LlmBaseUrl = 'https://api.openai.com/v1',
    [Security.SecureString]$LlmApiKey,
    [Security.SecureString]$LangfusePublicKey,
    [Security.SecureString]$LangfuseSecretKey,
    [string]$LangfuseBaseUrl = 'https://cloud.langfuse.com',
    [switch]$NonInteractive,
    [switch]$SkipFirstRun,
    [switch]$SkipLangfuse,
    [switch]$InstallDevelopmentDependencies,
    [switch]$InstallTasks,
    [PSCredential]$TaskCredential
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Read-RequiredText([string]$Prompt, [string]$Current) {
    if ($Current) { return $Current }
    if ($NonInteractive) { throw "Brak wymaganej wartości: $Prompt" }
    $Value = Read-Host $Prompt
    if (-not $Value) { throw "Wartość jest wymagana: $Prompt" }
    return $Value.Trim()
}

function Read-OptionalSecure([string]$Prompt, [Security.SecureString]$Current) {
    if ($null -ne $Current) { return $Current }
    if ($NonInteractive) { return $null }
    return Read-Host $Prompt -AsSecureString
}

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot
    $SpaceName = Read-RequiredText 'Nazwa DigitalOcean Space (istniejącego albo do utworzenia)' $SpaceName
    if (-not $SpacesAccessKey) {
        if ($NonInteractive) { throw 'Brak -SpacesAccessKey.' }
        $SpacesAccessKey = Read-Host 'DigitalOcean Spaces access key' -AsSecureString
    }
    if (-not $SpacesSecretKey) {
        if ($NonInteractive) { throw 'Brak -SpacesSecretKey.' }
        $SpacesSecretKey = Read-Host 'DigitalOcean Spaces secret key' -AsSecureString
    }
    $SpacesAccessKeyPlain = ConvertFrom-SecureStringPlainText $SpacesAccessKey
    $SpacesSecretKeyPlain = ConvertFrom-SecureStringPlainText $SpacesSecretKey
    if (-not $SpacesAccessKeyPlain -or -not $SpacesSecretKeyPlain) {
        throw 'Klucze DigitalOcean Spaces nie mogą być puste.'
    }

    if ($LlmProvider -eq 'openai_compatible' -and -not $LlmApiKey) {
        $LlmApiKey = Read-OptionalSecure 'Klucz API LLM (Enter = adapter regułowy jako awaryjny)' $LlmApiKey
    }
    $LlmApiKeyPlain = if ($LlmApiKey) { ConvertFrom-SecureStringPlainText $LlmApiKey } else { '' }
    $EffectiveLlmProvider = $LlmProvider
    if ($LlmProvider -eq 'openai_compatible' -and -not $LlmApiKeyPlain) {
        $EffectiveLlmProvider = 'rule_based'
        Write-Warning 'Nie podano klucza LLM. Konfiguruję deterministyczny adapter regułowy; później można dodać klucz i przełączyć provider.'
    }

    if (-not $SkipLangfuse) {
        if (-not $LangfusePublicKey) {
            $LangfusePublicKey = Read-OptionalSecure 'Langfuse public key (Enter = wyłącz telemetrykę LLM)' $LangfusePublicKey
        }
        if ($LangfusePublicKey -and -not $LangfuseSecretKey) {
            $LangfuseSecretKey = Read-OptionalSecure 'Langfuse secret key' $LangfuseSecretKey
        }
    }
    $LangfusePublicPlain = if ($LangfusePublicKey) { ConvertFrom-SecureStringPlainText $LangfusePublicKey } else { '' }
    $LangfuseSecretPlain = if ($LangfuseSecretKey) { ConvertFrom-SecureStringPlainText $LangfuseSecretKey } else { '' }
    $ObservabilityBackend = if ($LangfusePublicPlain -and $LangfuseSecretPlain) { 'langfuse' } else { 'none' }

    & (Join-Path $PSScriptRoot 'Install-Local.ps1') `
        -ProjectRoot $ProjectRoot `
        -RuntimeRoot $RuntimeRoot `
        -PythonExecutable $PythonExecutable `
        -PreferredPythonVersion $PreferredPythonVersion `
        -NoAutomaticPythonInstall:$NoAutomaticPythonInstall `
        -RecreateVenv:$RecreateVenv `
        -InstallDevelopmentDependencies:$InstallDevelopmentDependencies `
        -SkipMigrations
    if ($LASTEXITCODE -ne 0) { throw 'Instalacja zależności zakończyła się błędem.' }

    Initialize-SmogAiRuntimeDirectories $RuntimeRoot
    $ConfigPath = Join-Path $RuntimeRoot 'config.yaml'
    Copy-Item (Join-Path $ProjectRoot 'config.example.yaml') $ConfigPath -Force
    $ApiToken = & (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -c 'import secrets; print(secrets.token_urlsafe(48))'
    if ($LASTEXITCODE -ne 0 -or -not $ApiToken) { throw 'Nie udało się wygenerować tokenu API.' }
    $DatabasePath = (Join-Path $RuntimeRoot 'data\smog.db').Replace('\','/')
    $Endpoint = "https://$SpacesRegion.digitaloceanspaces.com"
    $EnvPath = Join-Path $RuntimeRoot 'smog-ai.env'
    $EnvContent = @"
# Wygenerowano automatycznie przez Setup-All.ps1. Nie dodawaj do Git.
SMOG_AI_ENV=production
SMOG_AI_PROJECT_ROOT=$ProjectRoot
SMOG_AI_DATA_ROOT=$RuntimeRoot
SMOG_AI_CONFIG=$ConfigPath
SMOG_AI_ENV_FILE=$EnvPath
SMOG_AI_DATABASE_URL=sqlite:///$DatabasePath
SMOG_AI_SOURCE_HOST_ID=$env:COMPUTERNAME
DISPLAY_TIMEZONE=Europe/Warsaw
LOG_LEVEL=INFO

SMOG_AI_OBJECT_STORE_BACKEND=spaces
SMOG_AI_OBJECT_STORE_BUCKET=$SpaceName
SMOG_AI_OBJECT_STORE_REGION=$SpacesRegion
SMOG_AI_OBJECT_STORE_ENDPOINT=$Endpoint
SMOG_AI_OBJECT_STORE_PREFIX=$SpacesPrefix
SPACES_BUCKET=$SpaceName
SPACES_REGION=$SpacesRegion
SPACES_ENDPOINT_URL=$Endpoint
SPACES_PREFIX=$SpacesPrefix
SPACES_ACCESS_KEY_ID=$SpacesAccessKeyPlain
SPACES_SECRET_ACCESS_KEY=$SpacesSecretKeyPlain

SMOG_AI_TRAINING_INPUT_SOURCE=object_store
SMOG_AI_ALLOW_DATABASE_FALLBACK=false
SMOG_AI_PUBLICATION_TRANSPORT=object_store
SMOG_AI_REQUIRE_PANDERA=true
SMOG_AI_SPATIAL_ENABLED=true
SMOG_AI_SPATIAL_ALGORITHM=idw
SMOG_AI_SPATIAL_GRID_RESOLUTION_KM=8
PUBLISH_API_TOKEN=$ApiToken

SMOG_AI_LLM_PROVIDER=$EffectiveLlmProvider
SMOG_AI_LLM_MODEL=$LlmModel
SMOG_AI_LLM_BASE_URL=$LlmBaseUrl
LLM_API_KEY=$LlmApiKeyPlain
SMOG_AI_OBSERVABILITY_BACKEND=$ObservabilityBackend
LANGFUSE_PUBLIC_KEY=$LangfusePublicPlain
LANGFUSE_SECRET_KEY=$LangfuseSecretPlain
LANGFUSE_BASE_URL=$LangfuseBaseUrl
"@
    Write-SmogAiUtf8File -Path $EnvPath -Content $EnvContent

    $ServerEnvPath = Join-Path $RuntimeRoot 'server-local.env'
    $ServerEnvContent = @"
# Lokalne FastAPI i Streamlit korzystają z tego samego Space co App Platform.
SMOG_AI_ENV=development
SMOG_AI_SERVER_STORAGE_BACKEND=object_store
SMOG_AI_SERVER_UPLOADS_ENABLED=false
SMOG_AI_SERVER_API_TOKEN=$ApiToken
SMOG_AI_SERVER_MAX_UPLOAD_BYTES=25000000
SMOG_AI_SERVER_KEEP_VERSIONS=50
SMOG_AI_SERVER_RATE_LIMIT_PER_MINUTE=60
SMOG_AI_SERVER_DOCS_ENABLED=true
SMOG_AI_CUSTOMER_NAME=Local development
SMOG_AI_DASHBOARD_TITLE=Asystent prognozy jakości powietrza — lokalnie
SMOG_AI_DASHBOARD_API_URL=http://127.0.0.1:8000/api/v1
SMOG_AI_APP_VERSION=1.7.0
SMOG_AI_COMMIT_SHA=local
SMOG_AI_SPATIAL_ENABLED=true
SMOG_AI_SPATIAL_CACHE_TTL_SECONDS=15
DISPLAY_TIMEZONE=Europe/Warsaw

SMOG_AI_OBJECT_STORE_BACKEND=spaces
SMOG_AI_OBJECT_STORE_BUCKET=$SpaceName
SMOG_AI_OBJECT_STORE_REGION=$SpacesRegion
SMOG_AI_OBJECT_STORE_ENDPOINT=$Endpoint
SMOG_AI_OBJECT_STORE_PREFIX=$SpacesPrefix
SPACES_ACCESS_KEY_ID=$SpacesAccessKeyPlain
SPACES_SECRET_ACCESS_KEY=$SpacesSecretKeyPlain

SMOG_AI_LLM_PROVIDER=$EffectiveLlmProvider
SMOG_AI_LLM_MODEL=$LlmModel
SMOG_AI_LLM_BASE_URL=$LlmBaseUrl
LLM_API_KEY=$LlmApiKeyPlain
SMOG_AI_OBSERVABILITY_BACKEND=$ObservabilityBackend
SMOG_AI_OBSERVABILITY_ENVIRONMENT=local
LANGFUSE_PUBLIC_KEY=$LangfusePublicPlain
LANGFUSE_SECRET_KEY=$LangfuseSecretPlain
LANGFUSE_BASE_URL=$LangfuseBaseUrl
"@
    Write-SmogAiUtf8File -Path $ServerEnvPath -Content $ServerEnvContent

    $env:SMOG_AI_PROJECT_ROOT = $ProjectRoot
    $env:SMOG_AI_DATA_ROOT = $RuntimeRoot
    $env:SMOG_AI_CONFIG = $ConfigPath
    $env:SMOG_AI_ENV_FILE = $EnvPath
    Import-SmogAiEnvFile -Path $EnvPath
    $PythonExe = Get-SmogAiPythonExe $ProjectRoot
    Set-Location -LiteralPath $ProjectRoot
    & $PythonExe -m alembic -c (Join-Path $ProjectRoot 'alembic.ini') upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Migracja bazy nie powiodła się.' }
    & $PythonExe -m smog_ai storage-init --create-if-missing --config $ConfigPath --env-file $EnvPath
    if ($LASTEXITCODE -ne 0) {
        throw 'Nie można przygotować DigitalOcean Spaces. Sprawdź nazwę Space, region, klucze i uprawnienie do utworzenia/odczytu Space.'
    }
    & $PythonExe -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check wykrył konflikt zależności.' }

    if (-not $SkipFirstRun) {
        Write-Host 'Uruchamiam pierwszy pełny cykl: API -> Spaces -> trening lokalny -> prognozy -> mapa Polski -> Spaces...' -ForegroundColor Cyan
        & $PythonExe -m smog_ai first-run --config $ConfigPath --env-file $EnvPath
        if ($LASTEXITCODE -notin @(0, 4, 6)) {
            throw "Pierwszy cykl zakończył się kodem $LASTEXITCODE. Dane i logi pozostały lokalnie."
        }
    }

    if ($InstallTasks) {
        if (-not (Test-SmogAiAdministrator)) {
            Write-Warning 'Pomijam instalację zadań: uruchom Setup-All.ps1 jako Administrator lub później Install-ScheduledTasks.ps1.'
        } else {
            & (Join-Path $PSScriptRoot 'Install-ScheduledTasks.ps1') `
                -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot -Credential $TaskCredential
            if ($LASTEXITCODE -ne 0) { throw 'Instalacja zadań zakończyła się błędem.' }
        }
    }

    Write-Host ''
    Write-Host 'GOTOWE.' -ForegroundColor Green
    Write-Host "Projekt może znajdować się w dowolnym katalogu: $ProjectRoot"
    Write-Host "Konfiguracja i dane: $RuntimeRoot"
    Write-Host "Uruchom API:       .\scripts\Start-LocalApi.ps1"
    Write-Host "Uruchom dashboard: .\scripts\Start-LocalDashboard.ps1"
    Write-Host "Konfiguracja GitHub/App Platform: .\scripts\Configure-GitHubDeploy.ps1"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
