[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$ApiUrl = 'http://127.0.0.1:8000/api/v1',
    [switch]$SkipApi
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)

$Common = Join-Path $ProjectRoot 'scripts\SmogAi.Common.ps1'
if (-not (Test-Path -LiteralPath $Common -PathType Leaf)) { throw "Brak: $Common" }
. $Common
$Python = Get-SmogAiPythonExe $ProjectRoot
$Dashboard = Join-Path $ProjectRoot 'server\dashboard\app.py'
$Monitor = Join-Path $ProjectRoot 'scripts\smog_ai_automation_monitor.py'

foreach ($Path in @($Dashboard, $Monitor)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Brak: $Path" }
    if (-not (Select-String -LiteralPath $Path -SimpleMatch 'HF21_MODEL_DECISION_UI_REPORT_MONITOR_V1' -Quiet)) {
        throw "Brak znacznika hotfixu: $Path"
    }
}

& $Python -m py_compile $Dashboard $Monitor
if ($LASTEXITCODE -ne 0) { throw "Kompilacja Python nie powiodła się: $LASTEXITCODE" }
Write-Host 'Kod dashboardu i monitora: OK' -ForegroundColor Green

if (-not $SkipApi) {
    $Models = Invoke-RestMethod "$($ApiUrl.TrimEnd('/'))/models"
    $Manifest = Invoke-RestMethod "$($ApiUrl.TrimEnd('/'))/spatial/manifest"
    $Active = @($Models.models)
    $Parameters = @($Manifest.parameters)
    if ($Active.Count -eq 0) { throw 'API nie zwróciło aktywnych modeli.' }
    if ($Parameters.Count -eq 0) { throw 'Manifest nie zwrócił publikowanych parametrów.' }
    $Targets = @($Active | ForEach-Object { [string]$_.target })
    if ('precipitation_probability' -in $Parameters -and 'precipitation_mm' -notin $Targets) {
        throw 'Brak modelu precipitation_mm dla wyjścia precipitation_probability.'
    }
    [pscustomobject]@{
        ActiveModelArtifacts = $Active.Count
        PublishedParameters = $Parameters.Count
        Parameters = $Parameters -join ', '
        DerivedPrecipitationProbability = (
            'precipitation_probability' -in $Parameters -and
            'precipitation_mm' -in $Targets
        )
    } | Format-List
    Write-Host 'Kontrakt modeli i Serving v2: OK' -ForegroundColor Green
}

Write-Host 'Następny krok: uruchom ponownie dashboard i monitor postępu.' -ForegroundColor Cyan
