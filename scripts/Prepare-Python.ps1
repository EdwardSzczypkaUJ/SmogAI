[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [ValidateSet('auto','3.12','3.13')][string]$PreferredPythonVersion = 'auto',
    [switch]$NoAutomaticPythonInstall,
    [switch]$AsJson
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $Python = Find-SmogAiBootstrapPython `
        -PythonExecutable $PythonExecutable `
        -PreferredVersion $PreferredPythonVersion `
        -NoAutomaticInstall:$NoAutomaticPythonInstall
    $Result = [ordered]@{
        status = 'ready'
        version = $Python.Version.ToString()
        bits = $Python.Bits
        display_name = $Python.DisplayName
        executable = $Python.Executable
        prefix_arguments = @($Python.PrefixArguments)
        runtime_executable = $Python.RuntimeExecutable
        supported_range = '>=3.12,<3.14'
    }
    if ($AsJson) {
        $Result | ConvertTo-Json -Depth 4
    }
    else {
        Write-Host "Python gotowy: $($Result.version) x$($Result.bits)" -ForegroundColor Green
        Write-Host "Źródło: $($Result.display_name)"
        Write-Host "Interpreter: $($Result.runtime_executable)"
    }
    exit 0
}
catch {
    if ($AsJson) {
        [ordered]@{ status = 'error'; error = $_.Exception.Message } | ConvertTo-Json
    }
    else {
        Write-Error $_
    }
    exit 1
}
