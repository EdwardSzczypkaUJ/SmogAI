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
    [string]$SpacesPrefix = 'smog-ai/production',
    [Security.SecureString]$SpacesAccessKey,
    [Security.SecureString]$SpacesSecretKey,
    [ValidateSet('openai_compatible','rule_based')][string]$LlmProvider = 'openai_compatible',
    [string]$LlmModel = 'gpt-4.1-mini',
    [string]$LlmBaseUrl = 'https://api.openai.com/v1',
    [Security.SecureString]$LlmApiKey,
    [Security.SecureString]$LangfusePublicKey,
    [Security.SecureString]$LangfuseSecretKey,
    [string]$LangfuseBaseUrl = 'https://cloud.langfuse.com',
    [string]$GitHubRepository,
    [string]$DigitalOceanAppName,
    [string]$CustomerName = 'Customer',
    [Security.SecureString]$DigitalOceanAccessToken,
    [switch]$SkipFirstRun,
    [switch]$SkipLangfuse,
    [switch]$InstallDevelopmentDependencies,
    [switch]$SkipGitHub,
    [switch]$SkipDeployment,
    [switch]$WatchDeployment,
    [switch]$InstallTasks,
    [PSCredential]$TaskCredential
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

function Invoke-Checked([string]$Description, [scriptblock]$Action) {
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Description zakończyło się kodem $LASTEXITCODE." }
}

try {
    $ProjectRoot = Resolve-SmogAiProjectRoot $ProjectRoot
    $RuntimeRoot = Resolve-SmogAiRuntimeRoot $RuntimeRoot

    $Setup = @{
        ProjectRoot = $ProjectRoot
        RuntimeRoot = $RuntimeRoot
        PythonExecutable = $PythonExecutable
        PreferredPythonVersion = $PreferredPythonVersion
        NoAutomaticPythonInstall = $NoAutomaticPythonInstall
        RecreateVenv = $RecreateVenv
        SpaceName = $SpaceName
        SpacesRegion = $SpacesRegion
        SpacesPrefix = $SpacesPrefix
        LlmProvider = $LlmProvider
        LlmModel = $LlmModel
        LlmBaseUrl = $LlmBaseUrl
        LangfuseBaseUrl = $LangfuseBaseUrl
        SkipFirstRun = $SkipFirstRun
        SkipLangfuse = $SkipLangfuse
        InstallDevelopmentDependencies = $InstallDevelopmentDependencies
    }
    if ($SpacesAccessKey) { $Setup.SpacesAccessKey = $SpacesAccessKey }
    if ($SpacesSecretKey) { $Setup.SpacesSecretKey = $SpacesSecretKey }
    if ($LlmApiKey) { $Setup.LlmApiKey = $LlmApiKey }
    if ($LangfusePublicKey) { $Setup.LangfusePublicKey = $LangfusePublicKey }
    if ($LangfuseSecretKey) { $Setup.LangfuseSecretKey = $LangfuseSecretKey }

    & (Join-Path $PSScriptRoot 'Setup-All.ps1') @Setup
    if ($LASTEXITCODE -ne 0) { throw 'Automatyczna konfiguracja lokalna nie powiodła się.' }

    if (-not $SkipGitHub) {
        foreach ($Tool in @('git', 'gh')) {
            if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
                throw "Brak narzędzia $Tool. Zainstaluj Git oraz GitHub CLI."
            }
        }
        Invoke-Checked 'Logowanie GitHub CLI' { gh auth status }
        Set-Location -LiteralPath $ProjectRoot

        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.git') -PathType Container)) {
            Invoke-Checked 'git init' { git init }
        }
        Invoke-Checked 'ustawienie gałęzi main' { git branch -M main }

        $HasHead = $true
        & git rev-parse --verify HEAD 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { $HasHead = $false }
        $Dirty = (& git status --porcelain)
        if (-not $HasHead -or $Dirty) {
            Invoke-Checked 'git add' { git add . }
            Invoke-Checked 'git commit' { git commit -m 'Initial automated customer release 1.7.0' }
        }

        $Origin = (& git remote get-url origin 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $Origin) {
            if (-not $GitHubRepository) {
                $GitHubRepository = Read-Host 'Repozytorium GitHub w formacie owner/name'
            }
            if (-not $GitHubRepository -or $GitHubRepository -notmatch '^[^/]+/[^/]+$') {
                throw 'Podaj repozytorium jako owner/name.'
            }
            & gh repo view $GitHubRepository --json nameWithOwner 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Invoke-Checked 'dodanie origin' { git remote add origin "https://github.com/$GitHubRepository.git" }
            }
            else {
                Invoke-Checked 'utworzenie prywatnego repozytorium GitHub' {
                    gh repo create $GitHubRepository --private --source $ProjectRoot --remote origin
                }
            }
        }
        Invoke-Checked 'push do GitHub' { git push -u origin main }

        if (-not $SkipDeployment) {
            & (Join-Path $PSScriptRoot 'Configure-GitHubDeploy.ps1') `
                -ProjectRoot $ProjectRoot `
                -RuntimeRoot $RuntimeRoot `
                -AppName $DigitalOceanAppName `
                -CustomerName $CustomerName `
                -DigitalOceanAccessToken $DigitalOceanAccessToken `
                -TriggerDeployment `
                -WatchDeployment:$WatchDeployment
            if ($LASTEXITCODE -ne 0) { throw 'Konfiguracja lub wdrożenie DigitalOcean nie powiodło się.' }
        }
    }

    if ($InstallTasks) {
        if (-not (Test-SmogAiAdministrator)) {
            Write-Warning 'Zadania nie zostały zainstalowane. Uruchom później Install-ScheduledTasks.ps1 jako Administrator.'
        }
        else {
            & (Join-Path $PSScriptRoot 'Install-ScheduledTasks.ps1') `
                -ProjectRoot $ProjectRoot -RuntimeRoot $RuntimeRoot -Credential $TaskCredential -RunSmokeTest
            if ($LASTEXITCODE -ne 0) { throw 'Instalacja Harmonogramu zadań nie powiodła się.' }
        }
    }

    Write-Host ''
    Write-Host 'BOOTSTRAP ZAKOŃCZONY.' -ForegroundColor Green
    Write-Host "Projekt: $ProjectRoot"
    Write-Host "Dane i sekrety: $RuntimeRoot"
    Write-Host 'Lokalne API:       .\scripts\Start-LocalApi.ps1'
    Write-Host 'Lokalny dashboard: .\scripts\Start-LocalDashboard.ps1'
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
