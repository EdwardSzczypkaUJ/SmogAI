[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$ExperimentalTargets = 'all'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $RuntimeRoot 'config.yaml'
$EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$LogRoot = Join-Path $RuntimeRoot "logs\republish-active-models\$Stamp"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "Config not found: $Config" }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "Environment file not found: $EnvFile" }
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
Set-Location -LiteralPath $ProjectRoot

$env:SMOG_AI_EXPERIMENTAL_TARGETS = $ExperimentalTargets

$Stages = @(
    [pscustomobject]@{ Name = 'Audit serving contract'; Command = 'audit-hourly-serving-contract'; Extra = @('--allow-experimental-targets', $ExperimentalTargets) },
    [pscustomobject]@{ Name = 'Generate station forecasts'; Command = 'predict'; Extra = @() },
    [pscustomobject]@{ Name = 'Verify forecasts'; Command = 'verify'; Extra = @() },
    [pscustomobject]@{ Name = 'Build and publish Serving v2'; Command = 'build-spatial-surfaces'; Extra = @() },
    [pscustomobject]@{ Name = 'Validate Serving v2'; Command = 'validate-spatial-surfaces'; Extra = @() },
    [pscustomobject]@{ Name = 'Storage readiness'; Command = 'storage-health'; Extra = @() }
)

$Index = 0
foreach ($Stage in $Stages) {
    $Index++
    $Log = Join-Path $LogRoot ("{0:D2}-{1}.log" -f $Index, $Stage.Command)
    Write-Host ("E{0}/{1} {2}" -f $Index, $Stages.Count, $Stage.Name) -ForegroundColor Cyan
    $Arguments = @('-m', 'smog_ai', $Stage.Command) + @($Stage.Extra) + @('--config', $Config, '--env-file', $EnvFile)
    & $Python @Arguments 2>&1 | Tee-Object -LiteralPath $Log
    if ($LASTEXITCODE -ne 0) {
        throw "Stage failed: $($Stage.Name), exit=$LASTEXITCODE, log=$Log"
    }
}

Write-Host ''
Write-Host 'Serving v2 was rebuilt from all active models.' -ForegroundColor Green
Write-Host "Experimental policy: $ExperimentalTargets"
Write-Host "Logs: $LogRoot"
Write-Host 'Restart the local API and dashboard, then repeat the exact-point query.' -ForegroundColor Yellow
