[CmdletBinding()]
param(
    [Parameter()]
    [string]$ProjectRoot = (Get-Location).Path,

    [Parameter()]
    [string]$RuntimeRoot = 'C:\ProgramData\SmogAI',

    [Parameter()]
    [string]$OutputRoot = (Join-Path $env:USERPROFILE 'Downloads'),

    [Parameter()]
    [string]$PublicAppUrl = 'https://gios-imgw-customer-eo53w.ondigitalocean.app',

    [Parameter()]
    [ValidateRange(20, 5000)]
    [int]$LogTailLines = 500,

    [Parameter()]
    [ValidateRange(1, 10)]
    [int]$RecentAutomationRuns = 2,

    [Parameter()]
    [switch]$SkipPublicReads
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Nie istnieje katalog projektu: $ProjectRoot"
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$GitDirectory = Join-Path $ProjectRoot '.git'
if (-not (Test-Path -LiteralPath $GitDirectory)) {
    throw "To nie jest katalog główny repozytorium Git: $ProjectRoot"
}

$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$PackageName = "SmogAI-new-session-$Timestamp"
$PackageRoot = Join-Path $OutputRoot $PackageName
$FinalZip = Join-Path $OutputRoot "$PackageName.zip"
$DiagnosticsRoot = Join-Path $PackageRoot 'diagnostics'
$RuntimeOutputRoot = Join-Path $PackageRoot 'runtime'
$TasksRoot = Join-Path $PackageRoot 'scheduled-tasks'
$PublicRoot = Join-Path $PackageRoot 'public-readonly'
$SourceRoot = Join-Path $PackageRoot 'source'
$Warnings = @()
$ExternalReadsPerformed = $false

foreach ($Directory in @(
    $PackageRoot,
    $DiagnosticsRoot,
    $RuntimeOutputRoot,
    $TasksRoot,
    $PublicRoot,
    $SourceRoot
)) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
}

function Write-Step {
    param([string]$Message)
    Write-Host "`n=== $Message ===" -ForegroundColor Cyan
}

function Add-WarningMessage {
    param([string]$Message)
    $script:Warnings += [string]$Message
    Write-Warning $Message
}

function Write-Utf8File {
    param(
        [string]$Path,
        [AllowEmptyString()]
        [string]$Content
    )
    $Parent = Split-Path -Parent $Path
    if ($Parent) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    [System.IO.File]::WriteAllText(
        $Path,
        $Content,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Write-JsonFile {
    param(
        [string]$Path,
        [AllowNull()]
        $Value,
        [int]$Depth = 12
    )
    Write-Utf8File -Path $Path -Content ($Value | ConvertTo-Json -Depth $Depth)
}

function Get-SafeCount {
    param([AllowNull()]$Value)
    return @($Value).Count
}

function ConvertTo-IsoStringOrNull {
    param([AllowNull()]$Value)

    if ($null -eq $Value) {
        return $null
    }
    try {
        if ($Value -is [datetime]) {
            return $Value.ToString('o')
        }
        return [string]$Value
    }
    catch {
        return $null
    }
}

function Protect-Text {
    param([AllowEmptyString()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return $Text
    }

    $Protected = $Text
    $Protected = [regex]::Replace(
        $Protected,
        '(?im)^(\s*[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*\s*[=:]\s*).+$',
        '$1<REDACTED>'
    )
    $Protected = [regex]::Replace(
        $Protected,
        '(?i)(Bearer\s+)[A-Za-z0-9._~+\-/=]{12,}',
        '$1<REDACTED>'
    )
    $Protected = [regex]::Replace(
        $Protected,
        '(?i)\b(sk-(?:proj-)?[A-Za-z0-9_-]{16,})\b',
        '<REDACTED_OPENAI_KEY>'
    )
    $Protected = [regex]::Replace(
        $Protected,
        '(?i)\b(dop_v1_[A-Za-z0-9]{20,})\b',
        '<REDACTED_DIGITALOCEAN_TOKEN>'
    )
    $Protected = [regex]::Replace(
        $Protected,
        '(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
        '<REDACTED_PRIVATE_KEY>'
    )
    return $Protected
}

function Copy-RedactedTextFile {
    param(
        [string]$Source,
        [string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        return
    }
    try {
        $Text = Get-Content -LiteralPath $Source -Raw -ErrorAction Stop
        Write-Utf8File -Path $Destination -Content (Protect-Text -Text $Text)
    }
    catch {
        Add-WarningMessage "Nie udało się zanonimizować pliku ${Source}: $($_.Exception.Message)"
    }
}

function Copy-SafeFile {
    param(
        [string]$Source,
        [string]$Destination,
        [long]$MaximumBytes = 52428800
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        return
    }
    try {
        $Item = Get-Item -LiteralPath $Source -ErrorAction Stop
        if ($Item.Length -gt $MaximumBytes) {
            Add-WarningMessage "Pominięto duży plik ($($Item.Length) B): $Source"
            return
        }
        $Parent = Split-Path -Parent $Destination
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
    catch {
        Add-WarningMessage "Nie udało się skopiować ${Source}: $($_.Exception.Message)"
    }
}

function Copy-LogTail {
    param(
        [string]$Source,
        [string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        return
    }
    try {
        $Tail = @(Get-Content -LiteralPath $Source -Tail $LogTailLines -ErrorAction Stop)
        Write-Utf8File -Path $Destination -Content (Protect-Text -Text ($Tail -join "`r`n"))
    }
    catch {
        Add-WarningMessage "Nie udało się pobrać końca logu ${Source}: $($_.Exception.Message)"
    }
}

function Invoke-GitCapture {
    param(
        [string[]]$Arguments,
        [string]$OutputFile
    )
    try {
        $PreviousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $Lines = @(& git -C $ProjectRoot @Arguments)
        $ExitCode = $LASTEXITCODE
        $ErrorActionPreference = $PreviousPreference
        Write-Utf8File -Path $OutputFile -Content (($Lines | ForEach-Object { [string]$_ }) -join "`r`n")
        if ($ExitCode -ne 0) {
            Add-WarningMessage "Git $($Arguments -join ' ') zakończył się kodem $ExitCode."
        }
        return $ExitCode
    }
    catch {
        $ErrorActionPreference = 'Stop'
        Add-WarningMessage "Nie udało się uruchomić Git $($Arguments -join ' '): $($_.Exception.Message)"
        return 1
    }
}

function Get-GitValue {
    param([string[]]$Arguments)
    try {
        $PreviousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $Result = @(& git -C $ProjectRoot @Arguments)
        $ExitCode = $LASTEXITCODE
        $ErrorActionPreference = $PreviousPreference
        if ($ExitCode -ne 0) {
            return $null
        }
        return (($Result | ForEach-Object { [string]$_ }) -join "`n").Trim()
    }
    catch {
        $ErrorActionPreference = 'Stop'
        return $null
    }
}

function Get-LatestFiles {
    param(
        [string]$Path,
        [string]$Filter = '*',
        [int]$Count = 1,
        [switch]$Directories
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return @()
    }
    if ($Directories) {
        return @(
            Get-ChildItem -LiteralPath $Path -Directory -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First $Count
        )
    }
    return @(
        Get-ChildItem -LiteralPath $Path -File -Filter $Filter -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First $Count
    )
}

Write-Step '1/12 Informacje o środowisku'
$SystemInfo = [ordered]@{
    collected_at = (Get-Date).ToString('o')
    computer_name = $env:COMPUTERNAME
    user_name = $env:USERNAME
    project_root = $ProjectRoot
    runtime_root = $RuntimeRoot
    powershell = $PSVersionTable.PSVersion.ToString()
    edition = [string]$PSVersionTable.PSEdition
    os = [System.Environment]::OSVersion.VersionString
    timezone = [System.TimeZoneInfo]::Local.Id
}
Write-JsonFile -Path (Join-Path $DiagnosticsRoot 'system.json') -Value $SystemInfo

Write-Step '2/12 Stan repozytorium Git'
$Head = Get-GitValue -Arguments @('rev-parse', 'HEAD')
$Branch = Get-GitValue -Arguments @('branch', '--show-current')
$RemoteUrl = Get-GitValue -Arguments @('remote', 'get-url', 'origin')
$GitStatusPath = Join-Path $DiagnosticsRoot 'git-status.txt'
Invoke-GitCapture -Arguments @('status', '--short', '--branch', '--untracked-files=all') -OutputFile $GitStatusPath | Out-Null
Invoke-GitCapture -Arguments @('log', '--date=iso-strict', '--decorate', '--stat', '-n', '40') -OutputFile (Join-Path $DiagnosticsRoot 'git-log-last-40.txt') | Out-Null
Invoke-GitCapture -Arguments @('diff', '--no-ext-diff', '--binary', 'HEAD', '--') -OutputFile (Join-Path $DiagnosticsRoot 'working-tree.patch') | Out-Null
Invoke-GitCapture -Arguments @('diff', '--cached', '--no-ext-diff', '--binary', '--') -OutputFile (Join-Path $DiagnosticsRoot 'staged.patch') | Out-Null
Invoke-GitCapture -Arguments @('diff', '--check') -OutputFile (Join-Path $DiagnosticsRoot 'git-diff-check.txt') | Out-Null
Invoke-GitCapture -Arguments @('ls-files', '--cached', '--others', '--exclude-standard') -OutputFile (Join-Path $DiagnosticsRoot 'git-files.txt') | Out-Null

$GitStatusLines = @()
if (Test-Path -LiteralPath $GitStatusPath) {
    $GitStatusLines = @(Get-Content -LiteralPath $GitStatusPath | Where-Object { $_ -and -not $_.StartsWith('##') })
}
$GitClean = ($GitStatusLines.Count -eq 0)

Write-Step '3/12 Kopia kodu i historia Git'
$TrackedFiles = @()
try {
    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $TrackedFiles = @(& git -C $ProjectRoot ls-files --cached --others --exclude-standard)
    $GitListExit = $LASTEXITCODE
    $ErrorActionPreference = $PreviousPreference
    if ($GitListExit -ne 0) {
        throw "git ls-files exit=$GitListExit"
    }
}
catch {
    $ErrorActionPreference = 'Stop'
    Add-WarningMessage "Nie udało się zbudować listy plików kodu: $($_.Exception.Message)"
    $TrackedFiles = @()
}

$ForbiddenTopLevel = @(
    '.git', '.venv', 'venv', 'node_modules', 'data', 'datasets', 'database',
    'databases', 'models', 'mlruns', 'logs', 'artifacts', 'snapshots', 'backups'
)
$ForbiddenAnywhere = @(
    '__pycache__', '.pytest_cache', '.ruff_cache'
)
$ForbiddenNames = @(
    '.env', 'smog-ai.env', 'credentials.json', 'secrets.json',
    'id_rsa', 'id_ed25519'
)
$CopiedSourceFiles = @()

foreach ($RelativePathValue in @($TrackedFiles)) {
    $RelativePath = ([string]$RelativePathValue).Trim()
    if (-not $RelativePath) {
        continue
    }
    $Normalised = $RelativePath.Replace('\', '/')
    $Segments = @($Normalised.Split('/'))
    $Name = [System.IO.Path]::GetFileName($Normalised)
    $LowerSegments = @($Segments | ForEach-Object { $_.ToLowerInvariant() })
    $FirstSegment = if ($LowerSegments.Count -gt 0) { $LowerSegments[0] } else { '' }
    $BlockedTopLevel = @($ForbiddenTopLevel | Where-Object { $FirstSegment -eq $_.ToLowerInvariant() }).Count -gt 0
    $BlockedAnywhere = @($ForbiddenAnywhere | Where-Object { $LowerSegments -contains $_.ToLowerInvariant() }).Count -gt 0
    $BlockedName = @($ForbiddenNames | Where-Object { $Name -ieq $_ }).Count -gt 0
    $BlockedExtension = $Name -match '(?i)\.(sqlite|sqlite3|db|duckdb|parquet|pkl|pickle|joblib|onnx|pt|pth|pem|key)$'
    if ($BlockedTopLevel -or $BlockedAnywhere -or $BlockedName -or $BlockedExtension) {
        continue
    }
    if ($Name -match '(?i)^\.env\.' -and $Name -notmatch '(?i)(example|sample|template)') {
        continue
    }

    $SourcePath = Join-Path $ProjectRoot ($RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        continue
    }
    $DestinationPath = Join-Path $SourceRoot ($RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
    Copy-SafeFile -Source $SourcePath -Destination $DestinationPath
    if (Test-Path -LiteralPath $DestinationPath) {
        $CopiedSourceFiles += [string]$RelativePath
    }
}

$SourceZip = Join-Path $PackageRoot 'SmogAI-current-working-tree-source.zip'
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $SourceZip) {
        Remove-Item -LiteralPath $SourceZip -Force
    }
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $SourceRoot,
        $SourceZip,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
}
catch {
    Add-WarningMessage "Nie udało się utworzyć ZIP kodu: $($_.Exception.Message)"
}

$GitBundle = Join-Path $PackageRoot 'SmogAI-all-refs.bundle'
try {
    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & git -C $ProjectRoot bundle create $GitBundle --all
    $BundleExit = $LASTEXITCODE
    $ErrorActionPreference = $PreviousPreference
    if ($BundleExit -ne 0) {
        Add-WarningMessage "git bundle zakończył się kodem $BundleExit."
    }
}
catch {
    $ErrorActionPreference = 'Stop'
    Add-WarningMessage "Nie udało się utworzyć git bundle: $($_.Exception.Message)"
}

Write-Step '4/12 Konfiguracja bez sekretów'
$ConfigOutput = Join-Path $DiagnosticsRoot 'configuration-redacted'
New-Item -ItemType Directory -Path $ConfigOutput -Force | Out-Null

$ProjectConfigCandidates = @(
    '.env', '.env.local', 'config.yaml', 'config.yml', 'config.example.yaml',
    'pyproject.toml', 'requirements.txt', 'requirements-dev.txt',
    'requirements-server.txt', '.do/app.yaml',
    '.github/workflows/ci-deploy-digitalocean.yml'
)
foreach ($RelativeConfig in $ProjectConfigCandidates) {
    $SourceConfig = Join-Path $ProjectRoot $RelativeConfig
    if (Test-Path -LiteralPath $SourceConfig -PathType Leaf) {
        $SafeRelative = $RelativeConfig.Replace('/', '__').Replace('\', '__')
        Copy-RedactedTextFile -Source $SourceConfig -Destination (Join-Path $ConfigOutput "$SafeRelative.redacted.txt")
    }
}

foreach ($RuntimeConfigName in @('smog-ai.env', 'config.yaml', 'config.yml')) {
    $RuntimeConfig = Join-Path $RuntimeRoot $RuntimeConfigName
    if (Test-Path -LiteralPath $RuntimeConfig -PathType Leaf) {
        Copy-RedactedTextFile -Source $RuntimeConfig -Destination (Join-Path $ConfigOutput "runtime-$RuntimeConfigName.redacted.txt")
    }
}

Write-Step '5/12 Zadania Harmonogramu Windows'
$TaskSnapshots = @()
try {
    $SmogTasks = @(Get-ScheduledTask -TaskPath '\SmogAI\' -ErrorAction SilentlyContinue)
    foreach ($Task in $SmogTasks) {
        try {
            $TaskInfo = $null
            try {
                $TaskInfo = Get-ScheduledTaskInfo -TaskPath $Task.TaskPath -TaskName $Task.TaskName -ErrorAction Stop
            }
            catch {
                Add-WarningMessage "Nie udało się odczytać TaskInfo dla $($Task.TaskName): $($_.Exception.Message)"
            }

            $MultipleInstances = $null
            $RestartInterval = $null
            if ($null -ne $Task.Settings) {
                $MultipleInstances = [string]$Task.Settings.MultipleInstances
                $RestartInterval = [string]$Task.Settings.RestartInterval
            }

            $TaskActions = @()
            foreach ($TaskAction in @($Task.Actions)) {
                if ($null -eq $TaskAction) {
                    continue
                }
                $TaskActions += [pscustomobject]@{
                    execute = [string]$TaskAction.Execute
                    arguments = Protect-Text -Text ([string]$TaskAction.Arguments)
                    working_directory = [string]$TaskAction.WorkingDirectory
                }
            }

            $TaskTriggers = @()
            foreach ($TaskTrigger in @($Task.Triggers)) {
                if ($null -eq $TaskTrigger) {
                    continue
                }
                $RepetitionInterval = $null
                $RepetitionDuration = $null
                if ($null -ne $TaskTrigger.Repetition) {
                    $RepetitionInterval = [string]$TaskTrigger.Repetition.Interval
                    $RepetitionDuration = [string]$TaskTrigger.Repetition.Duration
                }
                $TaskTriggers += [pscustomobject]@{
                    enabled = [bool]$TaskTrigger.Enabled
                    start_boundary = [string]$TaskTrigger.StartBoundary
                    repetition_interval = $RepetitionInterval
                    repetition_duration = $RepetitionDuration
                }
            }

            $Snapshot = [ordered]@{
                task_path = [string]$Task.TaskPath
                task_name = [string]$Task.TaskName
                state = [string]$Task.State
                enabled = ([string]$Task.State -ne 'Disabled')
                multiple_instances = $MultipleInstances
                restart_interval = $RestartInterval
                last_run_time = if ($null -ne $TaskInfo) { ConvertTo-IsoStringOrNull -Value $TaskInfo.LastRunTime } else { $null }
                last_task_result = if ($null -ne $TaskInfo) { $TaskInfo.LastTaskResult } else { $null }
                next_run_time = if ($null -ne $TaskInfo) { ConvertTo-IsoStringOrNull -Value $TaskInfo.NextRunTime } else { $null }
                actions = @($TaskActions)
                triggers = @($TaskTriggers)
            }
            $TaskSnapshots += [pscustomobject]$Snapshot
        }
        catch {
            Add-WarningMessage "Nie udało się zbudować snapshotu zadania $($Task.TaskName): $($_.Exception.Message)"
            continue
        }

        try {
            $Xml = Export-ScheduledTask -TaskPath $Task.TaskPath -TaskName $Task.TaskName
            Write-Utf8File -Path (Join-Path $TasksRoot "$($Task.TaskName).xml") -Content (Protect-Text -Text $Xml)
        }
        catch {
            Add-WarningMessage "Nie udało się wyeksportować zadania $($Task.TaskName): $($_.Exception.Message)"
        }
    }
}
catch {
    Add-WarningMessage "Nie udało się odczytać Harmonogramu zadań: $($_.Exception.Message)"
}
Write-JsonFile -Path (Join-Path $TasksRoot 'tasks.json') -Value @($TaskSnapshots)

Write-Step '6/12 Procesy SmogAI, Python, MLflow i PowerShell'
$ProcessSnapshots = @()
try {
    $ProcessSnapshots = @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $Command = [string]$_.CommandLine
                $Command -match '(?i)(smog[_-]?ai|mlflow|GIOS_IMGW_Forecast|scheduled-training|automation-monitor)'
            } |
            ForEach-Object {
                [pscustomobject]@{
                    process_id = $_.ProcessId
                    parent_process_id = $_.ParentProcessId
                    name = $_.Name
                    creation_date = [string]$_.CreationDate
                    executable_path = $_.ExecutablePath
                    command_line = Protect-Text -Text ([string]$_.CommandLine)
                }
            }
    )
}
catch {
    Add-WarningMessage "Nie udało się odczytać procesów: $($_.Exception.Message)"
}
Write-JsonFile -Path (Join-Path $DiagnosticsRoot 'processes.json') -Value @($ProcessSnapshots)

Write-Step '7/12 Bieżący i ostatnie przebiegi automatyzacji'
$AutomationRoot = Join-Path $RuntimeRoot 'logs\automation'
$CurrentJson = Join-Path $AutomationRoot 'current.json'
Copy-RedactedTextFile -Source $CurrentJson -Destination (Join-Path $RuntimeOutputRoot 'automation\current.json')

$RunDirectories = Get-LatestFiles -Path (Join-Path $AutomationRoot 'runs') -Count $RecentAutomationRuns -Directories
foreach ($RunDirectory in @($RunDirectories)) {
    $RunDestination = Join-Path $RuntimeOutputRoot "automation\runs\$($RunDirectory.Name)"
    New-Item -ItemType Directory -Path $RunDestination -Force | Out-Null
    foreach ($Name in @('run.json', 'events.jsonl', 'summary.json', 'pipeline-summary.json', 'resources.jsonl')) {
        $Candidate = Join-Path $RunDirectory.FullName $Name
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            Copy-SafeFile -Source $Candidate -Destination (Join-Path $RunDestination $Name)
        }
    }
    $StageLogs = @(
        Get-ChildItem -LiteralPath $RunDirectory.FullName -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)\.(log|stdout|stderr)$' }
    )
    foreach ($StageLog in $StageLogs) {
        Copy-LogTail -Source $StageLog.FullName -Destination (Join-Path $RunDestination "$($StageLog.Name).tail.txt")
    }
}

Write-Step '8/12 Logi treningu, odświeżania i post-heavy'
$LogGroups = @(
    @{ Name = 'scheduled-training'; Path = (Join-Path $RuntimeRoot 'logs\scheduled-training'); Count = 4 },
    @{ Name = 'scheduled-refresh'; Path = (Join-Path $RuntimeRoot 'logs\scheduled-refresh'); Count = 4 },
    @{ Name = 'post-heavy-serving'; Path = (Join-Path $RuntimeRoot 'logs\post-heavy-serving'); Count = 4 },
    @{ Name = 'digitalocean-publication'; Path = (Join-Path $RuntimeRoot 'logs\manual-digitalocean-publication'); Count = 3 },
    @{ Name = 'monitor'; Path = (Join-Path $RuntimeRoot 'logs\automation-monitor'); Count = 4 }
)
foreach ($Group in $LogGroups) {
    $Latest = Get-LatestFiles -Path $Group.Path -Count $Group.Count
    foreach ($Log in @($Latest)) {
        Copy-LogTail -Source $Log.FullName -Destination (Join-Path $RuntimeOutputRoot "logs\$($Group.Name)\$($Log.Name).tail.txt")
    }
}

$ProgressRoot = Join-Path $RuntimeRoot 'logs\progress'
if (Test-Path -LiteralPath $ProgressRoot -PathType Container) {
    foreach ($ProgressFile in @(Get-ChildItem -LiteralPath $ProgressRoot -File -Filter '*.json' -ErrorAction SilentlyContinue)) {
        Copy-RedactedTextFile -Source $ProgressFile.FullName -Destination (Join-Path $RuntimeOutputRoot "progress\$($ProgressFile.Name)")
    }
}

Write-Step '9/12 Raporty świeżości, MLflow i DigitalOcean'
$ComparisonPath = Join-Path $RuntimeRoot 'reports\mlflow\model-comparison.json'
Copy-SafeFile -Source $ComparisonPath -Destination (Join-Path $RuntimeOutputRoot 'reports\mlflow\model-comparison.json') -MaximumBytes 52428800

$FreshnessReports = Get-LatestFiles -Path (Join-Path $RuntimeRoot 'reports\freshness') -Count 4
foreach ($Report in @($FreshnessReports)) {
    Copy-SafeFile -Source $Report.FullName -Destination (Join-Path $RuntimeOutputRoot "reports\freshness\$($Report.Name)")
}

$DigitalOceanReports = Get-LatestFiles -Path (Join-Path $RuntimeRoot 'reports\digitalocean') -Count 3 -Directories
foreach ($ReportDirectory in @($DigitalOceanReports)) {
    $Destination = Join-Path $RuntimeOutputRoot "reports\digitalocean\$($ReportDirectory.Name)"
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($ReportFile in @(Get-ChildItem -LiteralPath $ReportDirectory.FullName -File -Recurse -ErrorAction SilentlyContinue)) {
        $Relative = $ReportFile.FullName.Substring($ReportDirectory.FullName.Length).TrimStart('\')
        if ($ReportFile.Extension -match '(?i)\.(json|txt|log|html|csv)$') {
            Copy-SafeFile -Source $ReportFile.FullName -Destination (Join-Path $Destination $Relative)
        }
    }
}

Write-Step '10/12 Publiczne endpointy tylko do odczytu'
$PublicResults = @()
if (-not $SkipPublicReads) {
    $ExternalReadsPerformed = $true
    $Endpoints = @(
        '/api/v1/health',
        '/api/v1/ready',
        '/api/v1/spatial/manifest',
        '/api/v1/models',
        '/api/v1/models/compare',
        '/api/v1/system/status',
        '/_stcore/health'
    )
    foreach ($Endpoint in $Endpoints) {
        $Url = $PublicAppUrl.TrimEnd('/') + $Endpoint
        $SafeName = ($Endpoint.Trim('/') -replace '[^A-Za-z0-9._-]', '_')
        if (-not $SafeName) {
            $SafeName = 'root'
        }
        try {
            $Response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
            $Body = Protect-Text -Text ([string]$Response.Content)
            Write-Utf8File -Path (Join-Path $PublicRoot "$SafeName.body.txt") -Content $Body
            $PublicResults += [pscustomobject]@{
                endpoint = $Endpoint
                status_code = [int]$Response.StatusCode
                error = $null
            }
        }
        catch {
            $StatusCode = $null
            $ErrorBody = $_.Exception.Message
            if ($_.Exception.Response) {
                try { $StatusCode = [int]$_.Exception.Response.StatusCode } catch { }
            }
            Write-Utf8File -Path (Join-Path $PublicRoot "$SafeName.error.txt") -Content (Protect-Text -Text $ErrorBody)
            $PublicResults += [pscustomobject]@{
                endpoint = $Endpoint
                status_code = $StatusCode
                error = $ErrorBody
            }
        }
    }
}
Write-JsonFile -Path (Join-Path $PublicRoot 'endpoint-status.json') -Value @($PublicResults)

Write-Step '11/12 Manifest i pełny prompt do nowej sesji'
$SecretFindings = @()
$SecretPatterns = @(
    @{ Name = 'OpenAI key'; Regex = '(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b' },
    @{ Name = 'DigitalOcean token'; Regex = '(?i)\bdop_v1_[A-Za-z0-9]{20,}\b' },
    @{ Name = 'Private key'; Regex = '(?i)-----BEGIN [^-]*PRIVATE KEY-----' }
)
$ScannableExtensions = @(
    '.txt', '.md', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.ini',
    '.cfg', '.conf', '.ps1', '.py', '.sh', '.xml', '.csv', '.html', '.log',
    '.stdout', '.stderr', '.patch'
)
foreach ($ScanFile in @(Get-ChildItem -LiteralPath $PackageRoot -File -Recurse -ErrorAction SilentlyContinue)) {
    if ($ScannableExtensions -notcontains $ScanFile.Extension.ToLowerInvariant()) {
        continue
    }
    try {
        $ScanText = Get-Content -LiteralPath $ScanFile.FullName -Raw -ErrorAction Stop
        if ([string]::IsNullOrEmpty([string]$ScanText)) {
            continue
        }
        $SanitizedScanText = [string]$ScanText
        $FileWasSanitized = $false
        foreach ($Pattern in $SecretPatterns) {
            if ([regex]::IsMatch($SanitizedScanText, $Pattern.Regex)) {
                $RelativeScanPath = $ScanFile.FullName.Substring($PackageRoot.Length).TrimStart('\')
                $SecretFindings += [pscustomobject]@{
                    pattern = $Pattern.Name
                    path = $RelativeScanPath
                    action = 'redacted_in_collected_copy'
                }
                $ReplacementName = ($Pattern.Name -replace '[^A-Za-z0-9]+', '_').ToUpperInvariant()
                $SanitizedScanText = [regex]::Replace(
                    $SanitizedScanText,
                    $Pattern.Regex,
                    "<REDACTED_$ReplacementName>"
                )
                $FileWasSanitized = $true
            }
        }
        if ($FileWasSanitized) {
            Write-Utf8File -Path $ScanFile.FullName -Content $SanitizedScanText
        }
    }
    catch {
        Add-WarningMessage "Nie udało się przeskanować $($ScanFile.FullName): $($_.Exception.Message)"
    }
}
$SecretScanStatus = if ($SecretFindings.Count -eq 0) { 'passed' } else { 'sanitized' }
if ($SecretFindings.Count -ne 0) {
    Add-WarningMessage "Wykryto i zanonimizowano $($SecretFindings.Count) potencjalnych sekretów w kopii diagnostycznej."

    if (Test-Path -LiteralPath $GitBundle -PathType Leaf) {
        Remove-Item -LiteralPath $GitBundle -Force
        Add-WarningMessage 'Pominięto git bundle, ponieważ może zawierać nieskanowalną historię potencjalnego sekretu.'
    }

    if (Test-Path -LiteralPath $SourceZip -PathType Leaf) {
        Remove-Item -LiteralPath $SourceZip -Force
    }
    try {
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $SourceRoot,
            $SourceZip,
            [System.IO.Compression.CompressionLevel]::Optimal,
            $false
        )
    }
    catch {
        Add-WarningMessage "Nie udało się przebudować bezpiecznego ZIP kodu: $($_.Exception.Message)"
    }
}

Write-JsonFile -Path (Join-Path $DiagnosticsRoot 'secret-scan.json') -Value ([ordered]@{
    status = $SecretScanStatus
    patterns = @($SecretPatterns | ForEach-Object { $_.Name })
    findings = @($SecretFindings)
})

$ComparisonInfo = $null
if (Test-Path -LiteralPath $ComparisonPath -PathType Leaf) {
    $ComparisonItem = Get-Item -LiteralPath $ComparisonPath
    $ComparisonInfo = [ordered]@{
        path = $ComparisonPath
        bytes = $ComparisonItem.Length
        last_modified = $ComparisonItem.LastWriteTime.ToString('o')
    }
}

$Manifest = [ordered]@{
    schema_version = '1.0'
    created_at = (Get-Date).ToString('o')
    package_name = $PackageName
    project_root = $ProjectRoot
    runtime_root = $RuntimeRoot
    git = [ordered]@{
        head = $Head
        branch = $Branch
        origin = $RemoteUrl
        clean = $GitClean
        changed_entry_count = $GitStatusLines.Count
    }
    source = [ordered]@{
        copied_file_count = $CopiedSourceFiles.Count
        source_zip = if (Test-Path -LiteralPath $SourceZip) { [System.IO.Path]::GetFileName($SourceZip) } else { $null }
        git_bundle = if (Test-Path -LiteralPath $GitBundle) { [System.IO.Path]::GetFileName($GitBundle) } else { $null }
    }
    scheduled_tasks = @($TaskSnapshots)
    relevant_processes = @($ProcessSnapshots)
    public_app_url = $PublicAppUrl
    public_endpoints = @($PublicResults)
    model_comparison = $ComparisonInfo
    safety = [ordered]@{
        secret_scan = $SecretScanStatus
        secret_values_requested = $false
        secret_values_displayed = $false
        external_reads_performed = $ExternalReadsPerformed
        external_writes_performed = $false
        git_modified = $false
        schedules_modified = $false
        publication_started = $false
        deployment_started = $false
    }
    warnings = @($Warnings)
}
Write-JsonFile -Path (Join-Path $PackageRoot 'handoff-manifest.json') -Value $Manifest -Depth 20

$Prompt = @'
# SmogAI — pełny prompt kontynuacyjny do nowej sesji

Kontynuujemy pracę nad repozytorium **EdwardSzczypkaUJ/SmogAI**. Najpierw przeczytaj cały załączony pakiet diagnostyczny, zwłaszcza `handoff-manifest.json`, `diagnostics/git-status.txt`, `runtime/automation/current.json`, najnowszy `runtime/automation/runs/*/run.json`, logi etapów, raport MLflow i wyniki publicznych endpointów. Nie zakładaj, że stan z opisu poniżej nadal jest aktualny — proces Serving mógł zakończyć się po utworzeniu pakietu.

## Zasady współpracy

1. Kontynuuj od zastanego stanu. Nie powtarzaj wykonanych treningów, publikacji ani deploymentów bez potrzeby.
2. Najpierw diagnoza read-only i krótkie podsumowanie dowodów. Zmiany, push, publikacja i deployment dopiero po moim jednoznacznym poleceniu.
3. Podawaj jeden kompletny blok PowerShell na krok. Kod ma działać w Windows PowerShell 5.1 i PowerShell 7.
4. Nie używaj operatora `??`. Wyniki potencjalnie skalarne zawsze opakowuj w `@(...)` przed `.Count`.
5. Nie łącz stderr natywnych programów przez `2>&1` przy `$ErrorActionPreference='Stop'`; Git zapisuje poprawne komunikaty na stderr. Używaj oddzielnych plików stdout/stderr lub kontroluj `$LASTEXITCODE`.
6. Każdy skrypt ma kończyć się obiektem statusowym: co odczytał, co zmienił, jakie były zewnętrzne odczyty/zapisy, czy rozpoczęto publikację/deployment.
7. Nigdy nie wyświetlaj ani nie zapisuj wartości sekretów. Można sprawdzać wyłącznie obecność i nazwy sekretów.
8. Dane surowe GIOŚ/IMGW, baza lokalna, cechy treningowe, modele, MLflow i logi pozostają wyłącznie lokalnie.
9. Do DigitalOcean Spaces publikujemy tylko skompresowane powierzchnie Serving v2, statyczne metadane, bezpieczne zagregowane statystyki, manifesty i wskaźniki. Bez danych surowych, treningowych i binariów modeli.
10. Publikacja ma być atomowa: obiekty niezmienne najpierw, publiczny pointer zawsze ostatni.

## Stan w chwili utworzenia pakietu

- Czas zebrania: {{COLLECTED_AT}}
- Projekt: {{PROJECT_ROOT}}
- Runtime: {{RUNTIME_ROOT}}
- Branch: {{BRANCH}}
- HEAD: {{HEAD}}
- Working tree clean: {{GIT_CLEAN}}
- Publiczna aplikacja: {{PUBLIC_APP_URL}}
- Dostawca NLP w produkcji ma być `openai`.
- Model NLP ma być `gpt-5.4-mini`.
- Cichy fallback do parsera regułowego ma być wyłączony (`SMOG_AI_LLM_ALLOW_RULE_FALLBACK=false`).
- Sekret GitHub Actions `LLM_API_KEY` był zweryfikowany jako istniejący; jego wartości nie wolno odczytywać.
- Ostatni znany udany deployment: GitHub Actions run `31983786524`, commit `2a3d987d5dcbf3a9dc9d0072a7e0f5e0d997c06b`.
- Ostatni znany udany CI: run `31981271927`.
- Ostatni znany ciężki trening zakończył się sukcesem (`FINISH exit=0`). Log: `C:\ProgramData\SmogAI\logs\scheduled-training\training-full-20260817-061001.log`.
- Lokalny raport porównania modeli: `C:\ProgramData\SmogAI\reports\mlflow\model-comparison.json`; ostatnio miał około 6,96 MB i 104 modele. Poprzednia diagnostyka błędnie założyła pole `.targets`, dlatego pokazała zero — najpierw rozpoznaj faktyczny schemat JSON.
- Publiczny endpoint `/api/v1/models/compare` ostatnio zwracał 404: artefakt porównania modeli nie został opublikowany.
- Ostatni znany przebieg Serving: RunId `20260817T083504-14afdd59f7`, rozpoczęty około 08:35, był długo w etapie `09-predict`. Pakiet zawiera aktualniejszy stan.
- Jednorazowy watcher `SmogAI-HF21-PostHeavy-Serving-OneShot` miał zostać zatrzymany i wyłączony; potwierdź jego rzeczywisty stan z `scheduled-tasks/tasks.json`.

## Istniejące harmonogramy i polityka kosztowa

- `SmogAI-HF21-Serving-8h`: pobieranie danych, przygotowanie, prognozy, powierzchnie i publikacja co 8 h.
- `SmogAI-HF21-Training-12h`: trening szybki co 12 h.
- `SmogAI-HF21-Heavy-28h`: ciężki trening co 28 h.
- Wspólny lock treningowy; zadania nie mogą prowadzić dwóch treningów równolegle.
- `MultipleInstances=IgnoreNew`, ponowienie po 30 minutach, retencja wydań Serving: 3.
- Po udanym treningu trzeba uruchomić/polecić Serving, ale bez ponownego pobierania danych, jeśli lokalne dane mają nie więcej niż 8 h.

## Docelowa polityka świeżości

Dotychczasowe 8 h jako próg świeżości jest błędne przy cyklu 8 h i opóźnieniu źródeł. Wprowadź osobno wiek pomiaru oraz wiek ostatniego pobrania:

- `fresh`: do 14 h,
- `warning`: ponad 14 h do 22 h,
- `stale/block`: ponad 22 h,
- `missing`: zawsze błąd.

Warning nie blokuje publikacji pointera. Stale lub missing blokuje publikację nowego pointera, ale nie usuwa poprzedniego poprawnego wydania.

## Najpilniejsze zadanie — najpierw stan bieżącego Serving

1. Ustal, czy RunId `20260817T083504-14afdd59f7` nadal działa, zakończył się sukcesem czy awarią.
2. Pokaż etap, czas działania, realny postęp i ETA na podstawie `run.json`, `events.jsonl`, `resources.jsonl`, logów etapów i procesów.
3. Sprawdź, czy zbudowano i zwalidowano 240 powierzchni, czy wykonano preflight, upload do Spaces, publikację statystyk i atomową zmianę pointera.
4. Sprawdź najnowszy raport DigitalOcean oraz publiczne `/api/v1/spatial/manifest`, `/api/v1/models/compare` i `/api/v1/system/status`.
5. Jeżeli przebieg utknął, zdiagnozuj przyczynę bez natychmiastowego zabijania procesu. Zaproponuj najbezpieczniejszy recovery.

## Trwała poprawka automatyzacji

Po diagnozie przygotuj zmianę w repozytorium, która:

1. Dodaje trwałą lokalną kolejkę/outbox żądań publikacji. Każdy udany przebieg data/Serving/quick training/heavy training zapisuje żądanie publikacji; awaria nie może publikować nieważnego pointera.
2. Kolejka przeżywa utratę sieci, zamknięcie terminala, restart Windows i utratę sesji ChatGPT. Po powrocie sieci wznawia upload; częściowe obiekty niezmienne są używane ponownie po SHA-256.
3. Nie pozwala na równoległe treningi ani konflikt Serving/trening. Zadania czekają w kolejce i korzystają ze wspólnego locka.
4. Po każdym udanym przebiegu publikuje bezpieczne artefakty do Spaces. Nie pobiera danych drugi raz, jeżeli lokalne dane są wystarczająco świeże.
5. Utrzymuje poprzedni publiczny release podczas awarii; pointer jest publikowany ostatni.
6. Rejestruje transfer Spaces dla każdego przebiegu: obiekty wysłane/ponownie użyte, bajty według kategorii (surfaces, stats, static, manifest, pointer), czas, przepustowość, liczbę requestów, reuse/cache ratio oraz sumy dzienne i miesięczne.
7. Eksponuje transfer, kolejkę, ostatni błąd/retry, etap, elapsed i ETA w lokalnym monitorze oraz bezpieczne podsumowanie w aplikacji.
8. Integruje bezpośredni ciężki trening `snapshot-train-hourly` z tym samym systemem progress/ETA, aby monitor go widział.

## Statystyki modeli i atrakcyjny dashboard

Nie chcę surowego JSON jako głównego widoku ani ubogich tabel. Zachowaj i rozbuduj bogate wizualizacje:

- ranking kandydatów i zwycięzcy per parametr/horyzont,
- aktywne oraz historyczne wersje modeli,
- historia: kiedy model był lepszy, kiedy trening nic nie poprawił i dlaczego nie aktywowano kandydata,
- świeżość modelu, czas ostatniego treningu i ostatniej konkurencyjnej ewaluacji,
- MAE/RMSE oraz metryki klasyfikacyjne,
- heatmapa jakości parametr × horyzont,
- wykres radarowy, donut udziału zwycięzców i trendy jakości,
- porównanie z persistence/climatology,
- provider, eksperymentalność i zakres danych treningowych,
- historia pobrań GIOŚ/IMGW i oś świeżości,
- informacja o faktycznym providerze/modelu NLP (`openai`, `gpt-5.4-mini`), tokenach i kosztach,
- statystyki Langfuse oraz koszty, gdy backend jest skonfigurowany; Langfuse nie jest jedynym magazynem historii.

Publikowany artefakt porównania musi być sanitizowany: bez lokalnych ścieżek, danych treningowych, surowych pomiarów, sekretów i binariów modeli. Najpierw rozpoznaj faktyczny schemat 6,96 MB `model-comparison.json`, potem utwórz mały publiczny kontrakt statystyczny.

## Błędy funkcjonalne do sprawdzenia

1. Napraw kodowanie UTF-8/mojibake: `Lotnisko Witków EPDS` nie może pojawiać się jako `WitkÃ³w`.
2. Test sekwencyjnych zapytań nie może pamiętać Katowic po zmianie miejsca.
3. „Jaka pogoda będzie jutro o 17:15 na lotnisku w Witkowie?” ma zwrócić pogodę dla prawidłowych współrzędnych lotniska.
4. „Jaka pogoda we Wrocławiu?” ma wybrać parametry pogodowe, a nie tylko zanieczyszczenia.
5. Nie wolno cicho wracać do parsera regułowego, jeżeli OpenAI nie działa; użytkownik ma zobaczyć jawny, bezpieczny komunikat diagnostyczny.

## Testy i wydanie

Po implementacji:

1. testy celowane,
2. pełny pytest i ruff,
3. kontrola PowerShell 5.1/7,
4. test odporności kolejki na brak sieci/restart/duplikację,
5. lokalny preflight bez zapisów zewnętrznych,
6. kontrola sekretów i `git diff --check`,
7. commit dopiero po mojej zgodzie,
8. push, CI, pieczęć wydania, publikacja statystyk i deployment dopiero po osobnych zgodach,
9. publiczny audyt health/ready/manifest/exact-point/48 h/model compare/operations/dashboard/OpenAI/Langfuse/costs/transfer.

## Dokumentacja końcowa

Na końcu mają istnieć wersjonowane skrypty i instrukcje:

- uruchomienie/zatrzymanie/status monitora, refresh i otwarcie w Chrome,
- lokalne uruchomienie API i dashboardu,
- instalacja/weryfikacja harmonogramów,
- ręczne quick/heavy training i Serving,
- recovery po braku sieci/prądu,
- dodanie nowego parametru od collectora przez trening, powierzchnie, publikację, API i dashboard,
- bezpieczna konfiguracja OpenAI, Langfuse i DigitalOcean bez ujawniania sekretów.

Zacznij od krótkiego, dowodowego podsumowania aktualnego stanu pakietu i przygotuj **jeden read-only skrypt PowerShell**, który pokaże stan Serving, etap, elapsed, ETA, kolejkę publikacji, najnowszy raport i publiczne endpointy. Nie modyfikuj jeszcze procesów ani zadań.
'@
$Prompt = $Prompt.Replace('{{COLLECTED_AT}}', (Get-Date).ToString('o'))
$Prompt = $Prompt.Replace('{{PROJECT_ROOT}}', $ProjectRoot)
$Prompt = $Prompt.Replace('{{RUNTIME_ROOT}}', $RuntimeRoot)
$Prompt = $Prompt.Replace('{{BRANCH}}', [string]$Branch)
$Prompt = $Prompt.Replace('{{HEAD}}', [string]$Head)
$Prompt = $Prompt.Replace('{{GIT_CLEAN}}', [string]$GitClean)
$Prompt = $Prompt.Replace('{{PUBLIC_APP_URL}}', $PublicAppUrl)
Write-Utf8File -Path (Join-Path $PackageRoot 'PROMPT-DO-NOWEJ-SESJI.md') -Content $Prompt

$Readme = @'
# Jak przenieść SmogAI do nowej sesji

1. Do nowej sesji załącz plik: {{FINAL_ZIP}}
2. Otwórz z ZIP plik `PROMPT-DO-NOWEJ-SESJI.md` i wklej jego całą treść jako pierwszą wiadomość.
3. Nie przesyłaj osobno plików `.env`, kluczy API ani tokenów.
4. Pakiet zawiera kod bieżącego working tree, git bundle, stan Git, zadania, procesy, ostatnie logi, raporty i publiczne odczyty.

Skrypt nie wykonuje pushu, publikacji, deploymentu ani zmian w Harmonogramie zadań.
'@
$Readme = $Readme.Replace('{{FINAL_ZIP}}', $FinalZip)
Write-Utf8File -Path (Join-Path $PackageRoot 'README-START.md') -Content $Readme

Write-Step '12/12 Sumy SHA-256 i końcowy ZIP'
$ChecksumLines = @()
$FilesForChecksums = @(
    Get-ChildItem -LiteralPath $PackageRoot -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'SHA256SUMS.txt' }
)
foreach ($File in $FilesForChecksums) {
    try {
        $Hash = Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256
        $Relative = $File.FullName.Substring($PackageRoot.Length).TrimStart('\')
        $ChecksumLines += "$($Hash.Hash.ToLowerInvariant())  $Relative"
    }
    catch {
        Add-WarningMessage "Nie udało się policzyć SHA-256 dla $($File.FullName): $($_.Exception.Message)"
    }
}
Write-Utf8File -Path (Join-Path $PackageRoot 'SHA256SUMS.txt') -Content ($ChecksumLines -join "`r`n")

try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $FinalZip) {
        Remove-Item -LiteralPath $FinalZip -Force
    }
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $PackageRoot,
        $FinalZip,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $true
    )
}
catch {
    throw "Nie udało się utworzyć końcowego ZIP: $($_.Exception.Message)"
}

$FinalHash = (Get-FileHash -LiteralPath $FinalZip -Algorithm SHA256).Hash.ToLowerInvariant()

[pscustomobject]@{
    Status                       = 'NEW_SESSION_HANDOFF_READY'
    ProjectRoot                  = $ProjectRoot
    Head                         = $Head
    Branch                       = $Branch
    GitClean                     = $GitClean
    SourceFilesCollected         = $CopiedSourceFiles.Count
    ScheduledTasksCollected      = $TaskSnapshots.Count
    RelevantProcessesCollected   = @($ProcessSnapshots).Count
    PublicEndpointsChecked       = @($PublicResults).Count
    PackageRoot                  = $PackageRoot
    Prompt                       = Join-Path $PackageRoot 'PROMPT-DO-NOWEJ-SESJI.md'
    Zip                          = $FinalZip
    ZipSHA256                    = $FinalHash
    WarningCount                 = $Warnings.Count
    SecretValuesRequested        = $false
    SecretValuesDisplayed        = $false
    ExternalReadsPerformed       = $ExternalReadsPerformed
    ExternalWritesPerformed      = $false
    GitModified                  = $false
    SchedulesModified            = $false
    ServingPublicationStarted    = $false
    ApplicationDeploymentStarted = $false
}
