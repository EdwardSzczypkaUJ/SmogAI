[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-OptionalProperty {
    param(
        [AllowNull()]
        $InputObject,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        $DefaultValue = $null
    )

    if ($null -eq $InputObject) {
        return $DefaultValue
    }

    $Property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $Property -or $null -eq $Property.Value) {
        return $DefaultValue
    }

    return $Property.Value
}

function Convert-ToInt32OrZero {
    param([AllowNull()]$Value)

    $Result = 0
    if ($null -ne $Value -and [int]::TryParse([string]$Value, [ref]$Result)) {
        return $Result
    }

    return 0
}

function Convert-ToInt64OrZero {
    param([AllowNull()]$Value)

    [long]$Result = 0
    if ($null -ne $Value -and [long]::TryParse([string]$Value, [ref]$Result)) {
        return $Result
    }

    return [long]0
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Config,
        $EnvFile
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $Utf8NoBom
    [Console]::OutputEncoding = $Utf8NoBom
    $OutputEncoding = $Utf8NoBom
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $Chcp = Join-Path $env:SystemRoot 'System32\chcp.com'
    if (Test-Path -LiteralPath $Chcp -PathType Leaf) {
        & $Chcp 65001 | Out-Null
    }

    Push-Location -LiteralPath $ProjectRoot
    try {
        $Text = (
            & $Python -m smog_ai parameter-catalog `
                --config $Config `
                --env-file $EnvFile |
                Out-String
        ).Trim()
        $CatalogExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location -ErrorAction SilentlyContinue
    }

    if ($CatalogExitCode -ne 0) {
        throw "parameter-catalog zakończył się kodem $CatalogExitCode."
    }

    if ($AsJson) {
        Write-Output $Text
        exit 0
    }

    $Catalog = $Text | ConvertFrom-Json

    $ParameterContainer = Get-OptionalProperty `
        -InputObject $Catalog `
        -Name 'parameters' `
        -DefaultValue ([pscustomobject]@{})

    $Rows = @(
        $ParameterContainer.PSObject.Properties |
            ForEach-Object {
                $Code = $_.Name
                $Value = $_.Value
                $Measurements = Get-OptionalProperty `
                    -InputObject $Value `
                    -Name 'measurements' `
                    -DefaultValue ([pscustomobject]@{})
                $ActiveModel = Get-OptionalProperty `
                    -InputObject $Value `
                    -Name 'active_model'

                [pscustomobject]@{
                    Parametr = $Code
                    Nazwa = [string](Get-OptionalProperty -InputObject $Value -Name 'display_name' -DefaultValue $Code)
                    Jednostka = [string](Get-OptionalProperty -InputObject $Value -Name 'canonical_unit' -DefaultValue '-')
                    Biezace = [bool](Get-OptionalProperty -InputObject $Value -Name 'collect_current' -DefaultValue $false)
                    Historia = [bool](Get-OptionalProperty -InputObject $Value -Name 'historical_backfill' -DefaultValue $false)
                    Cecha = [bool](Get-OptionalProperty -InputObject $Value -Name 'auxiliary_feature' -DefaultValue $false)
                    CelML = [bool](Get-OptionalProperty -InputObject $Value -Name 'forecast_target' -DefaultValue $false)
                    Mapa = [bool](Get-OptionalProperty -InputObject $Value -Name 'spatial_surface' -DefaultValue $false)
                    Sensory = Convert-ToInt32OrZero (Get-OptionalProperty -InputObject $Value -Name 'sensor_count' -DefaultValue 0)
                    Wiersze = Convert-ToInt64OrZero (Get-OptionalProperty -InputObject $Measurements -Name 'rows' -DefaultValue 0)
                    Model = if ($null -ne $ActiveModel) {
                        [string](Get-OptionalProperty -InputObject $ActiveModel -Name 'provider' -DefaultValue '-')
                    }
                    else {
                        '-'
                    }
                }
            }
    )

    Write-Host ''
    Write-Host 'PARAMETRY POWIETRZA GIOŚ' -ForegroundColor Cyan
    if ($Rows.Count -gt 0) {
        $Rows | Sort-Object Parametr | Format-Table -AutoSize
    }
    else {
        Write-Host 'Brak skonfigurowanych parametrów powietrza.' -ForegroundColor Yellow
    }

    $WeatherContainer = Get-OptionalProperty `
        -InputObject $Catalog `
        -Name 'weather_parameters' `
        -DefaultValue ([pscustomobject]@{})

    $WeatherRows = @(
        $WeatherContainer.PSObject.Properties |
            ForEach-Object {
                $Code = $_.Name
                $Value = $_.Value
                $Measurements = Get-OptionalProperty `
                    -InputObject $Value `
                    -Name 'measurements' `
                    -DefaultValue ([pscustomobject]@{})
                $ActiveModel = Get-OptionalProperty `
                    -InputObject $Value `
                    -Name 'active_model'

                [pscustomobject]@{
                    Parametr = $Code
                    Nazwa = [string](Get-OptionalProperty -InputObject $Value -Name 'display_name' -DefaultValue $Code)
                    Jednostka = [string](Get-OptionalProperty -InputObject $Value -Name 'canonical_unit' -DefaultValue '-')
                    KadencjaH = Convert-ToInt32OrZero (Get-OptionalProperty -InputObject $Value -Name 'cadence_hours' -DefaultValue 0)
                    Biezace = [bool](Get-OptionalProperty -InputObject $Value -Name 'collect_current' -DefaultValue $false)
                    Historia = [bool](Get-OptionalProperty -InputObject $Value -Name 'historical_backfill' -DefaultValue $false)
                    Cecha = [bool](Get-OptionalProperty -InputObject $Value -Name 'auxiliary_feature' -DefaultValue $false)
                    CelML = [bool](Get-OptionalProperty -InputObject $Value -Name 'forecast_target' -DefaultValue $false)
                    Mapa = [bool](Get-OptionalProperty -InputObject $Value -Name 'spatial_surface' -DefaultValue $false)
                    Stacje = Convert-ToInt32OrZero (Get-OptionalProperty -InputObject $Measurements -Name 'stations' -DefaultValue 0)
                    Wiersze = Convert-ToInt64OrZero (Get-OptionalProperty -InputObject $Measurements -Name 'rows' -DefaultValue 0)
                    Model = if ($null -ne $ActiveModel) {
                        [string](Get-OptionalProperty -InputObject $ActiveModel -Name 'provider' -DefaultValue '-')
                    }
                    else {
                        '-'
                    }
                }
            }
    )

    Write-Host ''
    Write-Host 'PARAMETRY POGODOWE IMGW I CELE MODELOWE' -ForegroundColor Cyan
    if ($WeatherRows.Count -gt 0) {
        $WeatherRows | Sort-Object Parametr | Format-Table -AutoSize
    }
    else {
        Write-Host 'Brak parametrów pogodowych w katalogu.' -ForegroundColor Yellow
    }

    Write-Host ''
    Write-Host (
        'Temperatura nie jest parametrem zanieczyszczenia GIOŚ. ' +
        'Jest zmienną pogodową IMGW i dlatego znajduje się w drugiej tabeli.'
    ) -ForegroundColor DarkGray

    $HourlyTargets = @(
        Get-OptionalProperty -InputObject $Catalog -Name 'hourly_targets' -DefaultValue @()
    )
    $SpatialTargets = @(
        Get-OptionalProperty -InputObject $Catalog -Name 'spatial_targets' -DefaultValue @()
    )

    Write-Host ''
    Write-Host 'AKTYWNE CELE GODZINOWE' -ForegroundColor Cyan
    Write-Host ($HourlyTargets -join ', ')
    Write-Host 'AKTYWNE WARSTWY PRZESTRZENNE' -ForegroundColor Cyan
    Write-Host ($SpatialTargets -join ', ')

    $UnknownContainer = Get-OptionalProperty `
        -InputObject $Catalog `
        -Name 'unconfigured_sensor_catalog' `
        -DefaultValue ([pscustomobject]@{})

    $Unknown = @(
        $UnknownContainer.PSObject.Properties |
            ForEach-Object {
                [pscustomobject]@{
                    Parametr = $_.Name
                    Sensory = Convert-ToInt32OrZero $_.Value
                }
            }
    )

    if ($Unknown.Count -gt 0) {
        Write-Host ''
        Write-Host 'PARAMETRY W METADANYCH GIOŚ, KTÓRYCH NIE MA JESZCZE W REJESTRZE' -ForegroundColor Yellow
        $Unknown | Sort-Object Parametr | Format-Table -AutoSize
    }

    exit 0
}
catch {
    $Message = $_.Exception.Message
    $Position = $_.InvocationInfo.PositionMessage
    $Stack = $_.ScriptStackTrace
    Write-Error (
        "Katalog parametrów zakończył się błędem.`r`n" +
        "Komunikat: $Message`r`n" +
        "Miejsce: $Position`r`n" +
        "Stos: $Stack"
    )
    exit 1
}
