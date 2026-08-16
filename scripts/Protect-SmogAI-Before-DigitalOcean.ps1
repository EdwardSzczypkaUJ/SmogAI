[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$OutputRoot = 'C:\Users\edzio\Downloads\SmogAI-Seals',
    [string]$Label = 'before-digitalocean'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Tool = Join-Path $ProjectRoot 'scripts\seal_current_release.py'
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.git'))) {
    throw "ProjectRoot is not a Git repository: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python not found: $Python"
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
& $Python $Tool --project-root $ProjectRoot --output-root $OutputRoot --label $Label
if ($LASTEXITCODE -ne 0) { throw "Release seal failed with code $LASTEXITCODE" }
Write-Host ''
Write-Host 'Current SmogAI version has been sealed.' -ForegroundColor Green
Write-Host "Output: $OutputRoot"
