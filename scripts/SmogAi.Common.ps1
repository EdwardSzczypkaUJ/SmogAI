Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Test-SmogAiProjectRootCandidate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $Resolved = [System.IO.Path]::GetFullPath($Path)
    }
    catch {
        return $false
    }
    return Test-Path -LiteralPath (Join-Path $Resolved 'pyproject.toml') -PathType Leaf
}

function Resolve-SmogAiProjectRoot {
    [CmdletBinding()]
    param([string]$ExplicitPath)

    $ScriptCandidate = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

    if ($ExplicitPath) {
        $Resolved = [System.IO.Path]::GetFullPath($ExplicitPath)
        if (-not (Test-SmogAiProjectRootCandidate $Resolved)) {
            throw "Parametr -ProjectRoot nie wskazuje projektu (brak pyproject.toml): $Resolved"
        }
        return $Resolved
    }

    if ($env:SMOG_AI_PROJECT_ROOT) {
        $EnvironmentCandidate = $null
        try {
            $EnvironmentCandidate = [System.IO.Path]::GetFullPath($env:SMOG_AI_PROJECT_ROOT)
        }
        catch {
            $EnvironmentCandidate = $null
        }
        if ($EnvironmentCandidate -and (Test-SmogAiProjectRootCandidate $EnvironmentCandidate)) {
            return $EnvironmentCandidate
        }
        if (Test-SmogAiProjectRootCandidate $ScriptCandidate) {
            Write-Warning "SMOG_AI_PROJECT_ROOT wskazuje niepoprawny lub nieaktualny katalog '$env:SMOG_AI_PROJECT_ROOT'. Używam projektu wykrytego przy skryptach: $ScriptCandidate"
            return $ScriptCandidate
        }
        throw "SMOG_AI_PROJECT_ROOT nie wskazuje projektu (brak pyproject.toml): $env:SMOG_AI_PROJECT_ROOT"
    }

    if (Test-SmogAiProjectRootCandidate $ScriptCandidate) {
        return $ScriptCandidate
    }
    throw "Nie znaleziono katalogu projektu przy skryptach (brak pyproject.toml): $ScriptCandidate"
}

function Resolve-SmogAiRuntimeRoot {
    [CmdletBinding()]
    param([string]$ExplicitPath)

    $Candidate = if ($ExplicitPath) {
        $ExplicitPath
    } elseif ($env:SMOG_AI_PROGRAMDATA_ROOT) {
        $env:SMOG_AI_PROGRAMDATA_ROOT
    } elseif ($env:SMOG_AI_DATA_ROOT) {
        $env:SMOG_AI_DATA_ROOT
    } else {
        Join-Path $env:ProgramData 'SmogAI'
    }
    return [System.IO.Path]::GetFullPath($Candidate)
}

function Import-SmogAiEnvFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowMissing
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        if ($AllowMissing) { return }
        throw "Plik środowiskowy nie istnieje: $Path"
    }
    foreach ($RawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $Line = $RawLine.Trim()
        if (-not $Line -or $Line.StartsWith('#')) { continue }
        $Separator = $Line.IndexOf('=')
        if ($Separator -le 0) { continue }
        $Key = $Line.Substring(0, $Separator).Trim()
        $Value = $Line.Substring($Separator + 1).Trim()
        if (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
            ($Value.StartsWith("'") -and $Value.EndsWith("'"))) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($Key, $Value, 'Process')
    }
}

function ConvertFrom-SecureStringPlainText {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][Security.SecureString]$SecureValue)
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Write-SmogAiUtf8File {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $Parent = Split-Path -Parent $Path
    if ($Parent) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

function Test-SmogAiSupportedPythonVersion {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][Version]$Version)

    return ($Version -ge [Version]'3.12' -and $Version -lt [Version]'3.14')
}

function Invoke-SmogAiPythonProbe {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    $ProbePath = Join-Path ([System.IO.Path]::GetTempPath()) (
        "smog-ai-python-probe-{0}.py" -f [Guid]::NewGuid().ToString('N')
    )
    $ProbeCode = @'
import json
import platform
import struct
import sys

print(json.dumps({
    "version": ".".join(str(part) for part in sys.version_info[:3]),
    "major_minor": "{}.{}".format(sys.version_info.major, sys.version_info.minor),
    "bits": struct.calcsize("P") * 8,
    "implementation": platform.python_implementation(),
    "runtime_executable": sys.executable,
}, ensure_ascii=True))
'@
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($ProbePath, $ProbeCode, $Utf8NoBom)

    $OutputLines = @()
    $ExitCode = 1
    try {
        # Użycie pliku tymczasowego zamiast wielowierszowego `python -c`
        # eliminuje różnice w cytowaniu argumentów między Windows PowerShell
        # 5.1, PowerShell 7, py.exe i aktywnym środowiskiem Conda.
        $OutputLines = @(& $Executable @PrefixArguments $ProbePath 2>&1)
        $ExitCode = $LASTEXITCODE
    }
    catch {
        return [pscustomobject]@{
            Success = $false
            Info = $null
            Reason = $_.Exception.Message
            ExitCode = 1
        }
    }
    finally {
        Remove-Item -LiteralPath $ProbePath -Force -ErrorAction SilentlyContinue
    }

    if ($ExitCode -ne 0) {
        $Message = (($OutputLines | ForEach-Object { $_.ToString().Trim() }) -join ' | ').Trim()
        if (-not $Message) { $Message = "kod zakończenia $ExitCode" }
        return [pscustomobject]@{
            Success = $false
            Info = $null
            Reason = $Message
            ExitCode = $ExitCode
        }
    }

    $Payload = $null
    for ($Index = $OutputLines.Count - 1; $Index -ge 0; $Index--) {
        $Line = $OutputLines[$Index].ToString().Trim()
        if (-not $Line) { continue }
        try {
            $Payload = $Line | ConvertFrom-Json
            if ($Payload.version -and $Payload.runtime_executable) { break }
            $Payload = $null
        }
        catch {
            $Payload = $null
        }
    }
    if (-not $Payload) {
        $Message = (($OutputLines | ForEach-Object { $_.ToString().Trim() }) -join ' | ').Trim()
        return [pscustomobject]@{
            Success = $false
            Info = $null
            Reason = "Interpreter nie zwrócił poprawnego JSON diagnostycznego. Wyjście: $Message"
            ExitCode = 0
        }
    }

    try {
        $Info = [pscustomobject]@{
            Version = [Version]$Payload.version
            MajorMinor = [string]$Payload.major_minor
            Bits = [int]$Payload.bits
            Implementation = [string]$Payload.implementation
            RuntimeExecutable = [string]$Payload.runtime_executable
        }
    }
    catch {
        return [pscustomobject]@{
            Success = $false
            Info = $null
            Reason = "Nie można zinterpretować danych interpretera: $($_.Exception.Message)"
            ExitCode = 0
        }
    }

    return [pscustomobject]@{
        Success = $true
        Info = $Info
        Reason = ''
        ExitCode = 0
    }
}

function Get-SmogAiPythonInfo {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    $Probe = Invoke-SmogAiPythonProbe `
        -Executable $Executable `
        -PrefixArguments $PrefixArguments
    if (-not $Probe.Success) { return $null }
    return $Probe.Info
}
function New-SmogAiPythonCandidate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @(),
        [Parameter(Mandatory = $true)][string]$DisplayName
    )
    return [pscustomobject]@{
        Executable = $Executable
        PrefixArguments = @($PrefixArguments)
        DisplayName = $DisplayName
    }
}

function Get-SmogAiCommandExecutable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Command)

    foreach ($PropertyName in @('Path', 'Source', 'Definition')) {
        $Property = $Command.PSObject.Properties[$PropertyName]
        if (-not $Property) { continue }
        $Value = [string]$Property.Value
        if ($Value -and (Test-Path -LiteralPath $Value -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($Value)
        }
    }
    return $null
}

function Get-SmogAiPythonCandidates {
    [CmdletBinding()]
    param(
        [string]$PythonExecutable,
        [ValidateSet('auto','3.12','3.13')][string]$PreferredVersion = 'auto'
    )

    $Candidates = @()
    if ($PythonExecutable) {
        $Resolved = $PythonExecutable
        if (Test-Path -LiteralPath $PythonExecutable -PathType Leaf) {
            $Resolved = [System.IO.Path]::GetFullPath($PythonExecutable)
        }
        $Candidates += New-SmogAiPythonCandidate `
            -Executable $Resolved `
            -DisplayName "explicit: $Resolved"
    }

    $VersionOrder = if ($PreferredVersion -eq '3.12') {
        @('3.12', '3.13')
    } elseif ($PreferredVersion -eq '3.13') {
        @('3.13', '3.12')
    } else {
        @('3.13', '3.12')
    }

    # Aktywne Conda i interpreter widoczny w bieżącej sesji mają pierwszeństwo.
    if ($env:CONDA_PREFIX) {
        $CondaPython = Join-Path $env:CONDA_PREFIX 'python.exe'
        if (Test-Path -LiteralPath $CondaPython -PathType Leaf) {
            $Candidates += New-SmogAiPythonCandidate `
                -Executable ([System.IO.Path]::GetFullPath($CondaPython)) `
                -DisplayName "active Conda: $CondaPython"
        }
    }

    foreach ($Name in @('python.exe', 'python')) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($Command) {
            $CommandPath = Get-SmogAiCommandExecutable $Command
            if ($CommandPath) {
                $Candidates += New-SmogAiPythonCandidate `
                    -Executable $CommandPath `
                    -DisplayName "current PATH: $CommandPath"
            }
        }
    }

    # Python Launcher: selektory wersji i pełna lista z `py -0p`.
    $PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($PyLauncher) {
        $PyLauncherPath = Get-SmogAiCommandExecutable $PyLauncher
        if ($PyLauncherPath) {
            foreach ($Version in $VersionOrder) {
                $Candidates += New-SmogAiPythonCandidate `
                    -Executable $PyLauncherPath `
                    -PrefixArguments @("-$Version") `
                    -DisplayName "py.exe -$Version"
            }
            try {
                $Inventory = @(& $PyLauncherPath -0p 2>$null)
                foreach ($RawLine in $Inventory) {
                    $Line = $RawLine.ToString().Trim()
                    $Match = [regex]::Match(
                        $Line,
                        '(?<path>[A-Za-z]:\\.*?python(?:\.exe)?)\s*$',
                        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
                    )
                    if ($Match.Success) {
                        $InventoryPath = $Match.Groups['path'].Value
                        if (Test-Path -LiteralPath $InventoryPath -PathType Leaf) {
                            $Candidates += New-SmogAiPythonCandidate `
                                -Executable ([System.IO.Path]::GetFullPath($InventoryPath)) `
                                -DisplayName "py.exe inventory: $InventoryPath"
                        }
                    }
                }
            }
            catch {
                # Selekcja `py -3.13` / `py -3.12` pozostaje dostępna.
            }
        }
    }

    # Wszystkie ścieżki zwrócone przez where.exe.
    $Where = Get-Command where.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Where) {
        $WherePath = Get-SmogAiCommandExecutable $Where
        if ($WherePath) {
            try {
                foreach ($CandidatePath in @(& $WherePath python 2>$null)) {
                    $CandidateText = $CandidatePath.ToString().Trim()
                    if ($CandidateText -and (Test-Path -LiteralPath $CandidateText -PathType Leaf)) {
                        $Candidates += New-SmogAiPythonCandidate `
                            -Executable ([System.IO.Path]::GetFullPath($CandidateText)) `
                            -DisplayName "where.exe: $CandidateText"
                    }
                }
            }
            catch {
                # Brak wyniku where.exe nie jest błędem instalatora.
            }
        }
    }

    # Standardowe instalacje python.org.
    foreach ($Version in $VersionOrder) {
        $Digits = $Version.Replace('.', '')
        $CommonPaths = @()
        if ($env:LOCALAPPDATA) {
            $CommonPaths += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$Digits\python.exe"
        }
        if ($env:ProgramFiles) {
            $CommonPaths += Join-Path $env:ProgramFiles "Python$Digits\python.exe"
        }
        if (${env:ProgramFiles(x86)}) {
            $CommonPaths += Join-Path ${env:ProgramFiles(x86)} "Python$Digits\python.exe"
        }
        foreach ($Path in $CommonPaths) {
            if (Test-Path -LiteralPath $Path -PathType Leaf) {
                $Candidates += New-SmogAiPythonCandidate `
                    -Executable ([System.IO.Path]::GetFullPath($Path)) `
                    -DisplayName $Path
            }
        }
    }

    # Instalacje zarejestrowane zgodnie z PEP 514.
    $RegistryRoots = @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\SOFTWARE\Python\PythonCore',
        'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore'
    )
    foreach ($RegistryRoot in $RegistryRoots) {
        if (-not (Test-Path -LiteralPath $RegistryRoot)) { continue }
        foreach ($Version in $VersionOrder) {
            $InstallKey = Join-Path (Join-Path $RegistryRoot $Version) 'InstallPath'
            if (-not (Test-Path -LiteralPath $InstallKey)) { continue }
            try {
                $Item = Get-Item -LiteralPath $InstallKey
                $RegistryPython = [string]$Item.GetValue('ExecutablePath')
                if (-not $RegistryPython) {
                    $InstallDirectory = [string]$Item.GetValue('')
                    if ($InstallDirectory) {
                        $RegistryPython = Join-Path $InstallDirectory 'python.exe'
                    }
                }
                if ($RegistryPython -and (Test-Path -LiteralPath $RegistryPython -PathType Leaf)) {
                    $Candidates += New-SmogAiPythonCandidate `
                        -Executable ([System.IO.Path]::GetFullPath($RegistryPython)) `
                        -DisplayName "registry: $RegistryPython"
                }
            }
            catch {
                # Uszkodzony wpis rejestru jest pomijany.
            }
        }
    }

    foreach ($Name in @('python3.13.exe', 'python3.13', 'python3.12.exe', 'python3.12')) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($Command) {
            $CommandPath = Get-SmogAiCommandExecutable $Command
            if ($CommandPath) {
                $Candidates += New-SmogAiPythonCandidate `
                    -Executable $CommandPath `
                    -DisplayName "versioned PATH: $CommandPath"
            }
        }
    }
    return $Candidates
}
function ConvertTo-SmogAiHexExitCode {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$ExitCode)

    $Unsigned = [uint32]([int64]$ExitCode -band [int64]4294967295)
    return ('0x{0:X8}' -f $Unsigned)
}

function Test-SmogAiWingetAlreadySatisfiedCode {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$ExitCode)

    # 0x8A15002B = brak mającej zastosowanie aktualizacji,
    # 0x8A150061 / 0x8A15010D = pakiet lub inna wersja już zainstalowana,
    # 0x8A15010E = nowsza wersja już zainstalowana.
    return @(
        -1978335189,
        -1978335135,
        -1978334963,
        -1978334962
    ) -contains $ExitCode
}

function Install-SmogAiPythonWithWinget {
    [CmdletBinding()]
    param([ValidateSet('auto','3.12','3.13')][string]$PreferredVersion = 'auto')

    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $Winget) {
        throw 'Nie znaleziono wspieranego Pythona 3.12/3.13 ani narzędzia winget. Podaj -PythonExecutable albo utwórz .venv istniejącym Pythonem.'
    }
    $WingetPath = Get-SmogAiCommandExecutable $Winget
    if (-not $WingetPath) {
        throw 'Znaleziono polecenie winget, ale nie można ustalić ścieżki do winget.exe.'
    }

    $VersionToInstall = if ($PreferredVersion -eq '3.13') { '3.13' } else { '3.12' }
    $PackageId = "Python.Python.$VersionToInstall"
    Write-Host "Brak wykrytego Pythona 3.12/3.13 x64. Próbuję zainstalować $PackageId przez winget..." -ForegroundColor Cyan

    $BaseArguments = @(
        'install', '--exact', '--id', $PackageId, '--source', 'winget',
        '--architecture', 'x64', '--silent', '--accept-package-agreements',
        '--accept-source-agreements', '--disable-interactivity'
    )
    $ExitCodes = @()

    $Attempts = @(
        [pscustomobject]@{
            Name = 'user'
            Arguments = @($BaseArguments + @('--scope', 'user'))
        },
        [pscustomobject]@{
            Name = 'default'
            Arguments = @($BaseArguments)
        }
    )
    foreach ($Attempt in $Attempts) {
        $Arguments = @($Attempt.Arguments)
        & $WingetPath @Arguments
        $Code = $LASTEXITCODE
        $ExitCodes += $Code

        if ($Code -eq 0) {
            Start-Sleep -Seconds 2
            return
        }
        if (Test-SmogAiWingetAlreadySatisfiedCode $Code) {
            $HexCode = ConvertTo-SmogAiHexExitCode $Code
            Write-Warning "winget zwrócił $Code ($HexCode), co może oznaczać, że Python lub nowsza wersja już jest zainstalowana. Ponawiam wykrywanie zamiast traktować to jako błąd krytyczny."
            return
        }
        Write-Warning "winget ($($Attempt.Name)) zakończył próbę kodem $Code ($(ConvertTo-SmogAiHexExitCode $Code))."
    }

    $Formatted = @(
        $ExitCodes | ForEach-Object {
            "$_ ($(ConvertTo-SmogAiHexExitCode $_))"
        }
    ) -join ', '
    throw "Automatyczna instalacja $PackageId nie powiodła się. Kody winget: $Formatted. Podaj istniejący interpreter przez -PythonExecutable i użyj -NoAutomaticPythonInstall."
}
function Search-SmogAiBootstrapPython {
    [CmdletBinding()]
    param(
        [string]$PythonExecutable,
        [ValidateSet('auto','3.12','3.13')][string]$PreferredVersion = 'auto'
    )

    $Seen = @{}
    $Diagnostics = @()
    $ExplicitRequested = [bool]$PythonExecutable

    foreach ($Candidate in Get-SmogAiPythonCandidates `
        -PythonExecutable $PythonExecutable `
        -PreferredVersion $PreferredVersion) {
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
        if (-not $Probe.Success) {
            $Diagnostics += (
                "{0} -> nie można uruchomić: {1}" -f
                $Candidate.DisplayName,
                $Probe.Reason
            )
            if ($ExplicitRequested -and $Candidate.DisplayName.StartsWith('explicit:')) {
                return [pscustomobject]@{
                    Found = $null
                    Diagnostics = @($Diagnostics)
                    ExplicitFailure = "Nie można uruchomić interpretera podanego przez -PythonExecutable: $PythonExecutable. $($Probe.Reason)"
                }
            }
            continue
        }

        $Info = $Probe.Info
        $Diagnostics += (
            "{0} -> Python {1}, {2}-bit, runtime={3}" -f
            $Candidate.DisplayName,
            $Info.Version,
            $Info.Bits,
            $Info.RuntimeExecutable
        )
        if ($Info.Bits -ne 64) {
            if ($ExplicitRequested -and $Candidate.DisplayName.StartsWith('explicit:')) {
                return [pscustomobject]@{
                    Found = $null
                    Diagnostics = @($Diagnostics)
                    ExplicitFailure = "Interpreter $PythonExecutable ma architekturę $($Info.Bits)-bit; wymagany jest Python x64."
                }
            }
            continue
        }
        if (Test-SmogAiSupportedPythonVersion $Info.Version) {
            return [pscustomobject]@{
                Found = [pscustomobject]@{
                    Executable = $Candidate.Executable
                    PrefixArguments = @($Candidate.PrefixArguments)
                    DisplayName = $Candidate.DisplayName
                    Version = $Info.Version
                    MajorMinor = $Info.MajorMinor
                    Bits = $Info.Bits
                    RuntimeExecutable = $Info.RuntimeExecutable
                }
                Diagnostics = @($Diagnostics)
                ExplicitFailure = $null
            }
        }
        if ($ExplicitRequested -and $Candidate.DisplayName.StartsWith('explicit:')) {
            return [pscustomobject]@{
                Found = $null
                Diagnostics = @($Diagnostics)
                ExplicitFailure = "Interpreter $PythonExecutable ma wersję $($Info.Version). Projekt obsługuje Python >=3.12,<3.14."
            }
        }
    }

    return [pscustomobject]@{
        Found = $null
        Diagnostics = @($Diagnostics)
        ExplicitFailure = $null
    }
}

function Format-SmogAiPythonDiagnostics {
    [CmdletBinding()]
    param([string[]]$Diagnostics)

    if (-not $Diagnostics -or $Diagnostics.Count -eq 0) {
        return 'Nie znaleziono żadnego kandydata. Sprawdź: python --version, Get-Command python.exe, $env:CONDA_PREFIX oraz py -0p.'
    }
    return "Sprawdzeni kandydaci:`n - " + ($Diagnostics -join "`n - ")
}

function Find-SmogAiBootstrapPython {
    [CmdletBinding()]
    param(
        [string]$PythonExecutable,
        [ValidateSet('auto','3.12','3.13')][string]$PreferredVersion = 'auto',
        [switch]$NoAutomaticInstall
    )

    $SearchResult = Search-SmogAiBootstrapPython `
        -PythonExecutable $PythonExecutable `
        -PreferredVersion $PreferredVersion
    if ($SearchResult.Found) { return $SearchResult.Found }
    if ($SearchResult.ExplicitFailure) {
        throw (
            $SearchResult.ExplicitFailure +
            "`n" +
            (Format-SmogAiPythonDiagnostics $SearchResult.Diagnostics)
        )
    }
    if ($NoAutomaticInstall) {
        throw (
            'Nie znaleziono wspieranego Pythona x64. Projekt obsługuje Python 3.12 i 3.13.' +
            "`n" +
            (Format-SmogAiPythonDiagnostics $SearchResult.Diagnostics)
        )
    }

    Install-SmogAiPythonWithWinget -PreferredVersion $PreferredVersion

    $SearchAfterInstall = Search-SmogAiBootstrapPython `
        -PythonExecutable $PythonExecutable `
        -PreferredVersion $PreferredVersion
    if ($SearchAfterInstall.Found) { return $SearchAfterInstall.Found }

    $AllDiagnostics = @($SearchResult.Diagnostics) + @($SearchAfterInstall.Diagnostics)
    throw (
        'Nie udało się wykryć Pythona 3.12/3.13 x64 także po próbie winget.' +
        "`n" +
        (Format-SmogAiPythonDiagnostics $AllDiagnostics) +
        "`nUruchom: `$PythonPath = (& python -c `"import sys; print(sys.executable)`").Trim()" +
        "`nNastępnie: .\scripts\Setup-All.ps1 -PythonExecutable `$PythonPath -NoAutomaticPythonInstall"
    )
}
function Get-SmogAiPythonExe {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "Brak środowiska .venv: $PythonExe. Uruchom scripts\Setup-All.ps1."
    }
    return $PythonExe
}

function Initialize-SmogAiRuntimeDirectories {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RuntimeRoot)
    foreach ($Relative in @(
        'data', 'logs\hourly', 'logs\daily', 'logs\training', 'logs\monthly',
        'logs\other', 'models', 'snapshots', 'backups', 'locks', 'tmp',
        'validation-reports', 'server-data', 'task-backups'
    )) {
        New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot $Relative) -Force | Out-Null
    }
}

function Test-SmogAiAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
