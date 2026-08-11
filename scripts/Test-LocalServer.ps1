[CmdletBinding()]
param(
    [string]$ApiBaseUrl = 'http://127.0.0.1:8000/api/v1',
    [string]$DashboardBaseUrl = 'http://127.0.0.1:8501',
    [string]$Question = 'Jutro o 12:00 jadę do Katowic. Jakie będą PM10, PM2.5, temperatura i opady?',
    [ValidateRange(15, 600)][int]$QueryTimeoutSeconds = 120,
    [ValidateRange(30, 900)][int]$TimelineTimeoutSeconds = 180,
    [switch]$SkipTimeline,
    [switch]$AsJson
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Result = [ordered]@{
    checked_at = [DateTimeOffset]::UtcNow.ToString('o')
    api_ok = $false
    dashboard_ok = $false
    query_ok = $false
    timeline_ok = $false
    api = $null
    query = $null
    timeline = $null
    query_duration_ms = $null
    timeline_duration_ms = $null
    dashboard_status = $null
    errors = @()
}

try {
    $Result.api = Invoke-RestMethod `
        -Uri "$($ApiBaseUrl.TrimEnd('/'))/health" `
        -Method Get `
        -TimeoutSec 15
    $Result.api_ok = ($Result.api.status -eq 'ok')
}
catch {
    $Result.errors += "API: $($_.Exception.Message)"
}

try {
    $Body = @{ text = $Question } | ConvertTo-Json
    $Watch = [System.Diagnostics.Stopwatch]::StartNew()
    $Result.query = Invoke-RestMethod `
        -Uri "$($ApiBaseUrl.TrimEnd('/'))/query" `
        -Method Post `
        -ContentType 'application/json; charset=utf-8' `
        -Body $Body `
        -TimeoutSec $QueryTimeoutSeconds
    $Watch.Stop()
    $Result.query_duration_ms = [Math]::Round($Watch.Elapsed.TotalMilliseconds, 1)
    $Result.query_ok = [bool]$Result.query.station
}
catch {
    $Result.errors += "Query: $($_.Exception.Message)"
}

if ($Result.query_ok -and -not $SkipTimeline) {
    try {
        $Place = $Result.query.place
        $Intent = $Result.query.intent
        $TimelineBody = @{
            latitude = [double]$Place.latitude
            longitude = [double]$Place.longitude
            target_time = [string]$Intent.target_time
            parameters = @(
                'PM10',
                'PM2.5',
                'temperature_c',
                'precipitation_probability',
                'precipitation_mm'
            )
            daily_profile = $true
            place_name = [string]$Place.name
        } | ConvertTo-Json -Depth 8

        $Watch = [System.Diagnostics.Stopwatch]::StartNew()
        $Result.timeline = Invoke-RestMethod `
            -Uri "$($ApiBaseUrl.TrimEnd('/'))/timeline" `
            -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body $TimelineBody `
            -TimeoutSec $TimelineTimeoutSeconds
        $Watch.Stop()
        $Result.timeline_duration_ms = [Math]::Round(
            $Watch.Elapsed.TotalMilliseconds,
            1
        )
        $Result.timeline_ok = (@($Result.timeline.rows).Count -gt 0)
    }
    catch {
        $Result.errors += "Timeline: $($_.Exception.Message)"
    }
}
elseif ($SkipTimeline) {
    $Result.timeline_ok = $true
}

try {
    $Response = Invoke-WebRequest `
        -Uri "$($DashboardBaseUrl.TrimEnd('/'))/_stcore/health" `
        -Method Get `
        -TimeoutSec 15 `
        -UseBasicParsing
    $Result.dashboard_status = $Response.StatusCode
    $Result.dashboard_ok = ($Response.StatusCode -eq 200)
}
catch {
    $Result.errors += "Dashboard: $($_.Exception.Message)"
}

if ($AsJson) {
    $Result | ConvertTo-Json -Depth 15
}
else {
    Write-Host "FastAPI:   $(if ($Result.api_ok) { 'OK' } else { 'ERROR' })"
    Write-Host "Textbox:   $(if ($Result.query_ok) { 'OK' } else { 'ERROR' })"
    Write-Host "Profil:    $(if ($Result.timeline_ok) { 'OK' } else { 'ERROR' })"
    Write-Host "Dashboard: $(if ($Result.dashboard_ok) { 'OK' } else { 'ERROR' })"
    if ($Result.query_duration_ms -ne $null) {
        Write-Host "Czas query:    $($Result.query_duration_ms) ms"
    }
    if ($Result.timeline_duration_ms -ne $null) {
        Write-Host "Czas timeline: $($Result.timeline_duration_ms) ms"
    }
    if ($Result.query) {
        Write-Host $Result.query.summary
    }
    foreach ($Message in $Result.errors) {
        Write-Warning $Message
    }
}

if (
    $Result.api_ok -and
    $Result.dashboard_ok -and
    $Result.query_ok -and
    $Result.timeline_ok
) {
    exit 0
}
exit 1
