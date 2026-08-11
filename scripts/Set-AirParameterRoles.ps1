[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [Parameter(Mandatory = $true)]
    [string]$Parameters,

    [Parameter(Mandatory = $true)]
    [string]$Roles,

    [switch]$Disable,

    [string]$CanonicalUnit = 'µg/m³'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

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

    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Backup = "$Config.before-air-parameter-change-$Stamp"
    Copy-Item -LiteralPath $Config -Destination $Backup -Force

    $EnabledText = if ($Disable) { 'false' } else { 'true' }

    $PythonSource = @'
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from smog_ai.air_parameters import canonical_code
from smog_ai.config import load_config

project_root = Path(sys.argv[1]).resolve()
config_path = Path(sys.argv[2]).resolve()
env_path = Path(sys.argv[3]).resolve()
raw_parameters = sys.argv[4]
raw_roles = sys.argv[5]
enabled = sys.argv[6].strip().lower() == "true"
canonical_unit = sys.argv[7]

allowed_roles = {
    "collect_current",
    "historical_backfill",
    "forecast_target",
    "auxiliary_feature",
    "spatial_surface",
}

codes = []
for raw in raw_parameters.replace(";", ",").split(","):
    raw = raw.strip()
    if not raw:
        continue
    code = canonical_code(raw)
    if code not in codes:
        codes.append(code)

roles = []
for raw in raw_roles.replace(";", ",").split(","):
    role = raw.strip()
    if not role:
        continue
    if role not in allowed_roles:
        raise ValueError(
            f"Unknown role {role!r}; allowed: {sorted(allowed_roles)}"
        )
    if role not in roles:
        roles.append(role)

if not codes:
    raise ValueError("At least one parameter is required")
if not roles:
    raise ValueError("At least one role is required")

resolved = load_config(config_path, env_path)
source = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}

if "air_parameters" not in source:
    source["air_parameters"] = resolved.air_parameters.model_dump(mode="json")
source["air_parameters"].setdefault(
    "unknown_sensor_policy",
    resolved.air_parameters.unknown_sensor_policy,
)
parameter_rows = source["air_parameters"].setdefault("parameters", {})

hourly = source.setdefault("hourly_forecasting", {})
hourly_targets = list(
    hourly.get("targets") or resolved.hourly_forecasting.targets
)
spatial_targets = list(
    hourly.get("spatial_targets") or resolved.hourly_forecasting.spatial_targets
)
target_algorithms = hourly.setdefault(
    "target_algorithms",
    resolved.hourly_forecasting.target_algorithms,
)
default_algorithms = list(
    hourly.get("default_air_target_algorithms")
    or resolved.hourly_forecasting.default_air_target_algorithms
)

changed = []
for code in codes:
    row = parameter_rows.get(code)
    if row is None:
        row = {
            "enabled": True,
            "display_name": code,
            "aliases": [code],
            "canonical_unit": canonical_unit,
            "cadence_hours": 1,
            "collect_current": False,
            "historical_backfill": False,
            "forecast_target": False,
            "auxiliary_feature": False,
            "spatial_surface": False,
            "allow_negative": False,
            "valid_min": 0.0,
            "valid_max": None,
            "exceedance_threshold": None,
            "spike_absolute": None,
            "annual_api_indicator": code,
            "prepared_archive_tokens": [code],
            "algorithms": default_algorithms,
        }
        parameter_rows[code] = row

    row["enabled"] = True
    for role in roles:
        row[role] = enabled

    if "forecast_target" in roles:
        if enabled:
            if code not in hourly_targets:
                hourly_targets.append(code)
            target_algorithms.setdefault(
                code,
                list(row.get("algorithms") or default_algorithms),
            )
        else:
            hourly_targets = [value for value in hourly_targets if value != code]

    if "spatial_surface" in roles:
        if enabled:
            if code not in spatial_targets:
                spatial_targets.append(code)
        else:
            spatial_targets = [value for value in spatial_targets if value != code]

    changed.append(
        {
            "parameter": code,
            "roles": {role: bool(row.get(role)) for role in sorted(allowed_roles)},
        }
    )

hourly["targets"] = hourly_targets
hourly["spatial_targets"] = spatial_targets

temporary = config_path.with_suffix(config_path.suffix + ".air-parameter.tmp")
temporary.write_text(
    yaml.safe_dump(
        source,
        allow_unicode=True,
        sort_keys=False,
        width=110,
    ),
    encoding="utf-8",
)

# Validate the temporary configuration before replacing the active file.
load_config(temporary, env_path)
temporary.replace(config_path)

print(
    json.dumps(
        {
            "status": "ok",
            "enabled": enabled,
            "changed": changed,
            "hourly_targets": hourly_targets,
            "spatial_targets": spatial_targets,
        },
        ensure_ascii=False,
        indent=2,
    )
)
'@

    if ($PSCmdlet.ShouldProcess(
        $Config,
        "ustaw role parametrów $Parameters"
    )) {
        $Output = (
            $PythonSource |
                & $Python - `
                    $ProjectRoot `
                    $Config `
                    $EnvFile `
                    $Parameters `
                    $Roles `
                    $EnabledText `
                    $CanonicalUnit |
                Out-String
        ).Trim()

        if ($LASTEXITCODE -ne 0) {
            Copy-Item -LiteralPath $Backup -Destination $Config -Force
            throw (
                "Zmiana konfiguracji zakończyła się błędem. " +
                "Przywrócono backup: $Backup"
            )
        }

        Write-Host $Output
    }

    Write-Host ''
    Write-Host 'ROLE PARAMETRÓW ZAPISANE I ZWALIDOWANE.' -ForegroundColor Green
    Write-Host "Backup: $Backup"
    Write-Host ''
    Write-Host 'Następny krok: air-parameter-catalog oraz odpowiedni collect/backfill.' -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
