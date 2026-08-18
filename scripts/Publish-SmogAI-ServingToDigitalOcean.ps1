[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$SourceRoot = '',
    [string]$Approval = '',
    [double]$FreshnessThresholdHours = 14.0,
    [double]$FreshnessStaleThresholdHours = 22.0,
    [switch]$AllowStaleData,
    [switch]$SkipSeal,
    [int]$RetainServingReleases = 0,
    [string]$RetentionApproval = '',
    [string]$SealOutputRoot = 'C:\Users\edzio\Downloads\SmogAI-Seals'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not $SourceRoot) { $SourceRoot = Join-Path $RuntimeRoot 'object-store' }
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ReportRoot = Join-Path $RuntimeRoot "reports\digitalocean\$Stamp"
New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null

if (-not $SkipSeal) {
    & (Join-Path $ProjectRoot 'scripts\Protect-SmogAI-Before-DigitalOcean.ps1') `
        -ProjectRoot $ProjectRoot -OutputRoot $SealOutputRoot `
        -Label "before-digitalocean-$Stamp"
    if ($LASTEXITCODE -ne 0) { throw "E0 release seal failed: $LASTEXITCODE" }
}

# Shell overrides caused several earlier prefix/root mismatches. The env file is
# the single source of truth for the DigitalOcean destination in this process.
$OverrideNames = @(
    'SMOG_AI_OBJECT_STORE_BACKEND', 'SMOG_AI_OBJECT_STORE_LOCAL_ROOT',
    'SMOG_AI_OBJECT_STORE_BUCKET', 'SMOG_AI_OBJECT_STORE_ENDPOINT',
    'SMOG_AI_OBJECT_STORE_REGION', 'SMOG_AI_OBJECT_STORE_PREFIX',
    'SMOG_AI_SERVER_STORAGE_BACKEND'
)
$Saved = @{}
foreach ($Name in $OverrideNames) {
    $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    [Environment]::SetEnvironmentVariable($Name, $null, 'Process')
}
try {
    Write-Host 'E1/4 Data freshness report' -ForegroundColor Cyan
    & $Python -m smog_ai data-freshness-report `
        --output-dir (Join-Path $ReportRoot 'freshness') `
        --threshold-hours $FreshnessThresholdHours `
        --stale-threshold-hours $FreshnessStaleThresholdHours `
        --config $Config --env-file $EnvFile |
        Tee-Object -FilePath (Join-Path $ReportRoot '01-data-freshness.json')
    if ($LASTEXITCODE -notin @(0, 4)) { throw "Freshness report failed: $LASTEXITCODE" }

    $FreshnessPath = Join-Path $ReportRoot 'freshness\data-freshness-latest.json'
    $Freshness = Get-Content -LiteralPath $FreshnessPath -Raw | ConvertFrom-Json
    if ($Freshness.overall_status -eq 'warning') {
        Write-Warning ("Data freshness status: {0}. Review: {1}" -f `
            $Freshness.overall_status, $FreshnessPath)
    }
    if ($Freshness.overall_status -in @('stale', 'missing')) {
        throw "Publication blocked: measurement or collection freshness is stale/missing. The previous valid public pointer remains unchanged."
    }

    Write-Host 'E2/4 DigitalOcean Spaces preflight' -ForegroundColor Cyan
    $Preflight = Join-Path $ReportRoot '02-serving-preflight.json'
    & $Python -m smog_ai digitalocean-serving-preflight `
        --source-root $SourceRoot --output $Preflight `
        --config $Config --env-file $EnvFile |
        Tee-Object -FilePath (Join-Path $ReportRoot '02-serving-preflight.log')
    if ($LASTEXITCODE -ne 0) { throw "DigitalOcean preflight failed: $LASTEXITCODE" }

    if ($Approval -ne 'PUBLISH VERIFIED SERVING V2') {
        Write-Host 'Preflight passed. No external write was performed.' -ForegroundColor Yellow
        Write-Host "To publish, repeat with -Approval 'PUBLISH VERIFIED SERVING V2'."
        Write-Host "Reports: $ReportRoot"
        exit 0
    }

    Write-Host 'E3/5 Atomic Serving v2 publication' -ForegroundColor Cyan
    & $Python -m smog_ai publish-serving-release `
        --source-root $SourceRoot --digitalocean-destination `
        --config $Config --env-file $EnvFile |
        Tee-Object -FilePath (Join-Path $ReportRoot '03-publication.json')
    if ($LASTEXITCODE -ne 0) { throw "Serving publication failed: $LASTEXITCODE" }

    Write-Host 'E4/5 Sanitised model-quality statistics' -ForegroundColor Cyan
    & $Python -m smog_ai export-model-comparison --publish `
        --digitalocean-destination `
        --config $Config --env-file $EnvFile |
        Tee-Object -FilePath (Join-Path $ReportRoot '04-model-comparison.json')
    if ($LASTEXITCODE -ne 0) { throw "Model comparison publication failed: $LASTEXITCODE" }
    $ComparisonPublication = Get-Content `
        -LiteralPath (Join-Path $ReportRoot '04-model-comparison.json') `
        -Raw | ConvertFrom-Json
    if (-not $ComparisonPublication.published.remote_verified) {
        throw 'Remote model comparison verification was not confirmed.'
    }
    if ([int]$ComparisonPublication.published.model_count -lt 1) {
        throw 'Published model comparison does not contain model history.'
    }

    Write-Host 'E5/5 Remote pointer and storage verification' -ForegroundColor Cyan
    & $Python -m smog_ai storage-health --digitalocean-destination `
        --config $Config --env-file $EnvFile |
        Tee-Object -FilePath (Join-Path $ReportRoot '05-storage-health.json')
    if ($LASTEXITCODE -ne 0) { throw "Remote storage verification failed: $LASTEXITCODE" }
    $RemoteHealth = Get-Content -LiteralPath (Join-Path $ReportRoot '05-storage-health.json') -Raw | ConvertFrom-Json
    $Published = Get-Content -LiteralPath (Join-Path $ReportRoot '03-publication.json') -Raw | ConvertFrom-Json
    if ($RemoteHealth.backend -notin @('s3','spaces')) {
        throw "Remote storage verification used unexpected backend: $($RemoteHealth.backend)"
    }
    if ($RemoteHealth.latest_spatial.release_id -ne $Published.details.release_id) {
        throw "Remote pointer does not match the published release."
    }
    if ($RetainServingReleases -gt 0) {
        & $Python -m smog_ai prune-serving-releases `
            --keep $RetainServingReleases `
            --confirmation $RetentionApproval `
            --digitalocean-destination `
            --config $Config --env-file $EnvFile |
            Tee-Object -FilePath (Join-Path $ReportRoot '05-serving-retention.json')
        if ($LASTEXITCODE -ne 0) { throw "Serving retention failed: $LASTEXITCODE" }
    }
    Write-Host "DigitalOcean publication completed. Reports: $ReportRoot" -ForegroundColor Green
}
finally {
    foreach ($Name in $OverrideNames) {
        [Environment]::SetEnvironmentVariable($Name, $Saved[$Name], 'Process')
    }
}
