[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [ValidateSet('auto','3.12','3.13')][string]$PreferredPythonVersion = 'auto',
    [switch]$AsJson
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'SmogAi.Common.ps1')

try {
    $Rows = @()
    $Seen = @{}
    foreach ($Candidate in Get-SmogAiPythonCandidates `
        -PythonExecutable $PythonExecutable `
        -PreferredVersion $PreferredPythonVersion) {
        $Key = (
            $Candidate.Executable.ToLowerInvariant() +
            '|' +
            ($Candidate.PrefixArguments -join ' ')
        )
        if ($Seen.ContainsKey($Key)) { continue }
        $Seen[$Key] = $true

        $Probe = Invoke-SmogAiPythonProbe `
            -Executable $Candidate.Executable `
            -PrefixArguments $Candidate.PrefixArguments
        if ($Probe.Success) {
            $Info = $Probe.Info
            $CandidateStatus = 'unsupported'
            if (
                $Info.Bits -eq 64 -and
                (Test-SmogAiSupportedPythonVersion $Info.Version)
            ) {
                $CandidateStatus = 'supported'
            }
            $Rows += [pscustomobject]@{
                display_name = $Candidate.DisplayName
                executable = $Candidate.Executable
                prefix_arguments = @($Candidate.PrefixArguments)
                status = $CandidateStatus
                version = $Info.Version.ToString()
                bits = $Info.Bits
                runtime_executable = $Info.RuntimeExecutable
                reason = ''
            }
        }
        else {
            $Rows += [pscustomobject]@{
                display_name = $Candidate.DisplayName
                executable = $Candidate.Executable
                prefix_arguments = @($Candidate.PrefixArguments)
                status = 'probe_failed'
                version = $null
                bits = $null
                runtime_executable = $null
                reason = $Probe.Reason
            }
        }
    }

    $Search = Search-SmogAiBootstrapPython `
        -PythonExecutable $PythonExecutable `
        -PreferredVersion $PreferredPythonVersion

    $OverallStatus = 'not_found'
    $Hint = 'Uruchom: $PythonPath = (& python -c "import sys; print(sys.executable)").Trim()'
    if ($Search.Found) {
        $OverallStatus = 'ready'
        $Hint = "Uruchom Setup-All.ps1 z -PythonExecutable '$($Search.Found.RuntimeExecutable)' -NoAutomaticPythonInstall."
    }
    $Result = [ordered]@{
        status = $OverallStatus
        selected = $Search.Found
        candidates = @($Rows)
        conda_prefix = $env:CONDA_PREFIX
        path = $env:PATH
        hint = $Hint
    }

    if ($AsJson) {
        $Result | ConvertTo-Json -Depth 8
    }
    else {
        if ($Search.Found) {
            Write-Host "Wybrany Python: $($Search.Found.Version) x$($Search.Found.Bits)" -ForegroundColor Green
            Write-Host "Interpreter: $($Search.Found.RuntimeExecutable)"
        }
        else {
            Write-Warning 'Nie znaleziono wspieranego Pythona 3.12/3.13 x64.'
        }
        $Rows | Format-Table display_name, status, version, bits, runtime_executable -AutoSize
        Write-Host ''
        Write-Host $Result.hint
    }

    if ($Search.Found) { exit 0 }
    exit 1
}
catch {
    if ($AsJson) {
        [ordered]@{
            status = 'error'
            error = $_.Exception.Message
        } | ConvertTo-Json -Depth 4
    }
    else {
        Write-Error $_
    }
    exit 1
}
