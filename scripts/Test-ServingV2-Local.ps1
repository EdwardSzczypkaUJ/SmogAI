[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',
    [string]$ApiBaseUrl = 'http://127.0.0.1:8000/api/v1',
    [switch]$SkipApi
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$StoreRoot = Join-Path $RuntimeRoot 'object-store'
$PointerPath = Join-Path $StoreRoot 'serving\latest.json'

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'smog_ai') -PathType Container)) {
    throw "ProjectRoot is not a SmogAI project: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $PointerPath -PathType Leaf)) {
    throw "Missing Serving v2 pointer: $PointerPath. Run build-spatial-surfaces after installing the update."
}

$Pointer = Get-Content -LiteralPath $PointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Pointer.contract -ne 'smog-ai-serving-pointer') {
    throw "Unexpected pointer contract: $($Pointer.contract)"
}
$ManifestPath = Join-Path $StoreRoot (($Pointer.manifest_key -split '/') -join '\')
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Pointer selects a missing manifest: $ManifestPath"
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Surfaces = @($Manifest.surfaces)
if ($Surfaces.Count -eq 0) { throw 'Serving manifest has no surfaces.' }

$Missing = @()
$CompressedBytes = [int64]0
foreach ($Entry in $Surfaces) {
    $Path = Join-Path $StoreRoot (($Entry.object_key -split '/') -join '\')
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Missing += $Path
        continue
    }
    $Item = Get-Item -LiteralPath $Path
    $CompressedBytes += $Item.Length
    $Header = [byte[]]::new(2)
    $Stream = [IO.File]::OpenRead($Path)
    try {
        if ($Stream.Read($Header, 0, 2) -ne 2) { throw "Truncated surface: $Path" }
    }
    finally { $Stream.Dispose() }
    if ($Header[0] -ne 0x1f -or $Header[1] -ne 0x8b) {
        throw "Surface is not gzip: $Path"
    }
}
if ($Missing.Count -gt 0) {
    throw "Missing $($Missing.Count) serving objects. First: $($Missing[0])"
}

$Result = [ordered]@{
    status = 'ok'
    contract = $Manifest.contract
    release_id = $Manifest.release_id
    manifest = $ManifestPath
    surface_count = $Surfaces.Count
    parameters = @($Manifest.parameters)
    horizons = @($Manifest.horizons_hours).Count
    compressed_mb = [math]::Round($CompressedBytes / 1MB, 2)
    giant_dashboard_snapshot_required = $false
}

if (-not $SkipApi) {
    $Health = Invoke-RestMethod -Uri "$ApiBaseUrl/health" -TimeoutSec 30
    $Ready = Invoke-RestMethod -Uri "$ApiBaseUrl/ready" -TimeoutSec 30
    $ApiManifest = Invoke-RestMethod -Uri "$ApiBaseUrl/spatial/manifest" -TimeoutSec 30
    if (-not $Ready.spatial_ready) { throw 'API is running but spatial_ready is false.' }
    if ($ApiManifest.release_id -ne $Manifest.release_id) {
        throw "API reads another release: $($ApiManifest.release_id)"
    }
    $Result.api = [ordered]@{
        status = $Health.status
        storage_backend = $Health.storage_backend
        publication_count = $Health.publication_count
        serving_release_id = $Health.serving_release_id
        spatial_ready = $Ready.spatial_ready
    }
}

$Result | ConvertTo-Json -Depth 10
