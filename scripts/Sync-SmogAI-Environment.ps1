[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$SourceEnv,
    [string[]]$TargetEnv,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Get-Location).Path
}
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)

if ([string]::IsNullOrWhiteSpace($SourceEnv)) {
    $SourceEnv = Join-Path $ProjectRoot '.env'
}
if (-not $TargetEnv -or $TargetEnv.Count -eq 0) {
    $TargetEnv = @(
        (Join-Path $RuntimeRoot 'smog-ai.env'),
        (Join-Path $RuntimeRoot 'server-local.env')
    )
}

function Read-SmogAiDotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Plik .env nie istnieje: $Path"
    }
    $Result = @{}
    foreach ($RawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $Line = $RawLine.Trim()
        if (-not $Line -or $Line.StartsWith('#')) { continue }
        $Separator = $Line.IndexOf('=')
        if ($Separator -le 0) { continue }
        $Name = $Line.Substring(0, $Separator).Trim()
        $Value = $Line.Substring($Separator + 1).Trim()
        if (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
            ($Value.StartsWith("'") -and $Value.EndsWith("'"))) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        $Result[$Name] = $Value
    }
    return $Result
}

function Set-SmogAiDotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string[]]$Lines,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowEmptyString()][string]$Value
    )
    $Pattern = '^\s*' + [regex]::Escape($Name) + '\s*='
    $Filtered = @($Lines | Where-Object { $_ -notmatch $Pattern })
    return @($Filtered + "$Name=$Value")
}

function Test-SmogAiSecretPresent {
    param([hashtable]$Config, [string]$Name)
    return -not [string]::IsNullOrWhiteSpace([string]$Config[$Name])
}

$SourceEnv = [IO.Path]::GetFullPath($SourceEnv)
$Source = Read-SmogAiDotEnv -Path $SourceEnv

$Required = @(
    'LLM_API_KEY',
    'SMOG_AI_LLM_PROVIDER',
    'SMOG_AI_LLM_MODEL',
    'SMOG_AI_GEOCODER_PROVIDER',
    'SMOG_AI_GEOCODER_ENDPOINT',
    'SMOG_AI_GEOCODER_USER_AGENT'
)
$Missing = @($Required | Where-Object {
    [string]::IsNullOrWhiteSpace([string]$Source[$_])
})
if ($Missing.Count -gt 0) {
    throw "Źródłowy .env nie zawiera wymaganych ustawień: $($Missing -join ', ')"
}

# Tylko ustawienia wspólne aplikacji. Konfiguracja storage, bazy, token publikacji
# i dane serwerowe pozostają nietknięte w każdym pliku docelowym.
$SharedNames = @(
    'LLM_API_KEY',
    'SMOG_AI_LLM_PROVIDER',
    'SMOG_AI_LLM_MODEL',
    'SMOG_AI_LLM_BASE_URL',
    'SMOG_AI_LLM_TIMEOUT_SECONDS',
    'SMOG_AI_LLM_MAX_RETRIES',
    'SMOG_AI_LLM_TEMPERATURE',
    'SMOG_AI_GEOCODER_PROVIDER',
    'SMOG_AI_GEOCODER_ENDPOINT',
    'SMOG_AI_GEOCODER_USER_AGENT',
    'SMOG_AI_GEOCODER_CACHE_PATH',
    'SMOG_AI_GEOCODER_TIMEOUT_SECONDS',
    'SMOG_AI_GEOCODER_MINIMUM_INTERVAL_SECONDS',
    'SMOG_AI_OBSERVABILITY_BACKEND',
    'SMOG_AI_OBSERVABILITY_ENVIRONMENT',
    'SMOG_AI_OBSERVABILITY_STRICT',
    'SMOG_AI_OBSERVABILITY_FLUSH_ON_REQUEST',
    'LANGFUSE_PUBLIC_KEY',
    'LANGFUSE_SECRET_KEY',
    'LANGFUSE_BASE_URL'
)
$ExplicitValues = [ordered]@{
    SMOG_AI_LLM_API_KEY_ENV = 'LLM_API_KEY'
    SMOG_AI_LLM_ALLOW_RULE_FALLBACK = 'false'
}

$Results = @()
$BackupRoot = Join-Path $RuntimeRoot 'config-backups'
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

foreach ($TargetPathRaw in $TargetEnv) {
    $TargetPath = [IO.Path]::GetFullPath($TargetPathRaw)
    $Before = Read-SmogAiDotEnv -Path $TargetPath
    $Differences = @()
    foreach ($Name in $SharedNames) {
        $Value = [string]$Source[$Name]
        if (-not [string]::IsNullOrWhiteSpace($Value) -and
            [string]$Before[$Name] -cne $Value) {
            $Differences += $Name
        }
    }
    foreach ($Name in $ExplicitValues.Keys) {
        if ([string]$Before[$Name] -cne [string]$ExplicitValues[$Name]) {
            $Differences += $Name
        }
    }

    if ($ValidateOnly) {
        $Results += [pscustomobject]@{
            File = $TargetPath
            Status = if ($Differences.Count -eq 0) { 'synchronized' } else { 'drift_detected' }
            ChangedSettings = $Differences.Count
            Provider = $Before['SMOG_AI_LLM_PROVIDER']
            Model = $Before['SMOG_AI_LLM_MODEL']
            Geocoder = $Before['SMOG_AI_GEOCODER_PROVIDER']
            LlmKeyPresent = Test-SmogAiSecretPresent $Before 'LLM_API_KEY'
            Backup = $null
        }
        continue
    }

    if ($Differences.Count -eq 0) {
        $Results += [pscustomobject]@{
            File = $TargetPath
            Status = 'already_synchronized'
            ChangedSettings = 0
            Provider = $Before['SMOG_AI_LLM_PROVIDER']
            Model = $Before['SMOG_AI_LLM_MODEL']
            Geocoder = $Before['SMOG_AI_GEOCODER_PROVIDER']
            LlmKeyPresent = Test-SmogAiSecretPresent $Before 'LLM_API_KEY'
            Backup = $null
        }
        continue
    }

    if (-not $PSCmdlet.ShouldProcess($TargetPath, "Synchronizacja $($Differences.Count) ustawień z $SourceEnv")) {
        continue
    }

    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    $Backup = Join-Path $BackupRoot "$(Split-Path -Leaf $TargetPath).$Timestamp.bak"
    Copy-Item -LiteralPath $TargetPath -Destination $Backup -Force

    $Lines = @(Get-Content -LiteralPath $TargetPath -Encoding UTF8)
    foreach ($Name in $SharedNames) {
        $Value = [string]$Source[$Name]
        if (-not [string]::IsNullOrWhiteSpace($Value)) {
            $Lines = Set-SmogAiDotEnvValue -Lines $Lines -Name $Name -Value $Value
        }
    }
    foreach ($Name in $ExplicitValues.Keys) {
        $Lines = Set-SmogAiDotEnvValue -Lines $Lines -Name $Name -Value ([string]$ExplicitValues[$Name])
    }

    $Temporary = "$TargetPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllLines($Temporary, [string[]]$Lines, $Utf8NoBom)
        Move-Item -LiteralPath $Temporary -Destination $TargetPath -Force
        $After = Read-SmogAiDotEnv -Path $TargetPath
        $Invalid = @($SharedNames | Where-Object {
            $Expected = [string]$Source[$_]
            -not [string]::IsNullOrWhiteSpace($Expected) -and [string]$After[$_] -cne $Expected
        })
        $Invalid += @($ExplicitValues.Keys | Where-Object {
            [string]$After[$_] -cne [string]$ExplicitValues[$_]
        })
        if ($Invalid.Count -gt 0) {
            throw "Weryfikacja nieudana dla: $($Invalid -join ', ')"
        }
        $Results += [pscustomobject]@{
            File = $TargetPath
            Status = 'synchronized'
            ChangedSettings = $Differences.Count
            Provider = $After['SMOG_AI_LLM_PROVIDER']
            Model = $After['SMOG_AI_LLM_MODEL']
            Geocoder = $After['SMOG_AI_GEOCODER_PROVIDER']
            LlmKeyPresent = Test-SmogAiSecretPresent $After 'LLM_API_KEY'
            Backup = $Backup
        }
    }
    catch {
        if (Test-Path -LiteralPath $Temporary) {
            Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
        }
        Copy-Item -LiteralPath $Backup -Destination $TargetPath -Force
        throw "Synchronizacja $TargetPath nie powiodła się; przywrócono $Backup. $($_.Exception.Message)"
    }
}

$Results | Format-List
Write-Host 'Wartości sekretów nie zostały wyświetlone.' -ForegroundColor DarkGray
if ($ValidateOnly -and @($Results | Where-Object Status -eq 'drift_detected').Count -gt 0) {
    exit 2
}
