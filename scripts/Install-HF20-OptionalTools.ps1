[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [switch]$InstallMLflow,

    [switch]$InstallLangfuse
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Brak Pythona projektu: $Python"
    }
    if (-not $InstallMLflow -and -not $InstallLangfuse) {
        throw 'Wybierz -InstallMLflow, -InstallLangfuse albo oba przelaczniki.'
    }
    $Extras = New-Object 'System.Collections.Generic.List[string]'
    if ($InstallMLflow) { [void]$Extras.Add('mlops') }
    if ($InstallLangfuse) { [void]$Extras.Add('observability') }
    $Selector = '.[' + ($Extras -join ',') + ']'
    Set-Location -LiteralPath $ProjectRoot
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Aktualizacja pip nie przeszla.' }
    & $Python -m pip install $Selector
    if ($LASTEXITCODE -ne 0) { throw 'Instalacja opcjonalnych narzedzi nie przeszla.' }
    Write-Host ''
    Write-Host "Zainstalowano opcjonalne rozszerzenia: $($Extras -join ', ')" -ForegroundColor Green
    Write-Host 'Sama instalacja SDK nie wlacza transmisji ani chmury.' -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
