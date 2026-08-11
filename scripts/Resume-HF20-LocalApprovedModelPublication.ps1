[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$Targets = 'PM10,PM2.5,temperature_c',

    [switch]$SkipComparison
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$SavedEnvironment = @{}
$EnvironmentNames = @(
    'SMOG_AI_CONFIG',
    'SMOG_AI_ENV_FILE',
    'SMOG_AI_OBJECT_STORE_BACKEND',
    'SMOG_AI_OBJECT_STORE_LOCAL_ROOT',
    'SMOG_AI_OBJECT_STORE_BUCKET',
    'SMOG_AI_OBJECT_STORE_ENDPOINT',
    'SMOG_AI_OBJECT_STORE_REGION'
)

foreach ($Name in $EnvironmentNames) {
    $Item = Get-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    $SavedEnvironment[$Name] = [pscustomobject]@{
        Exists = ($null -ne $Item)
        Value = if ($null -ne $Item) { [string]$Item.Value } else { $null }
    }
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)

    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Config = Join-Path $RuntimeRoot 'config.local-stage3.yaml'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.local-stage3.env'
    $LocalRoot = Join-Path $RuntimeRoot 'local-object-store'
    $Marker = Join-Path $ProjectRoot '.hotfixes\HF20_4_IDEMPOTENT_MODEL_PUBLICATION_1.7.0.json'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Config,
        $EnvFile,
        $Marker
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:SMOG_AI_CONFIG = $Config
    $env:SMOG_AI_ENV_FILE = $EnvFile
    $env:SMOG_AI_OBJECT_STORE_BACKEND = 'local'
    $env:SMOG_AI_OBJECT_STORE_LOCAL_ROOT = $LocalRoot
    Remove-Item Env:SMOG_AI_OBJECT_STORE_BUCKET -ErrorAction SilentlyContinue
    Remove-Item Env:SMOG_AI_OBJECT_STORE_ENDPOINT -ErrorAction SilentlyContinue
    Remove-Item Env:SMOG_AI_OBJECT_STORE_REGION -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    $Probe = @'
from __future__ import annotations
import json, sys
from pathlib import Path
project_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_root))
from smog_ai.config import load_config
cfg = load_config(Path(sys.argv[2]), Path(sys.argv[3]))
expected_root = Path(sys.argv[4]).resolve()
checks = {
    "object_storage_enabled": cfg.object_storage.enabled,
    "backend_local": cfg.object_storage.backend == "local",
    "local_root_matches": cfg.object_storage.local_root.resolve() == expected_root,
    "upload_models_enabled": cfg.artifacts.upload_models,
    "remote_bucket_absent": not cfg.object_storage.bucket,
    "remote_endpoint_absent": not cfg.object_storage.endpoint_url,
}
print(json.dumps({"checks": checks, "local_root": str(expected_root)}, ensure_ascii=True, indent=2))
raise SystemExit(0 if all(checks.values()) else 4)
'@

    $ProbeText = (
        $Probe |
            & $Python - $ProjectRoot $Config $EnvFile $LocalRoot |
            Out-String
    ).Trim()
    Write-Host 'LOCAL-STAGE3 PUBLICATION PREFLIGHT' -ForegroundColor Cyan
    Write-Host $ProbeText

    if ($LASTEXITCODE -ne 0) {
        throw 'STOP: publikacja nie jest skierowana do lokalnego ObjectStore.'
    }

    $Arguments = @(
        '-m', 'smog_ai',
        'publish-approved-models',
        '--targets', $Targets,
        '--confirmation', 'PUBLISH APPROVED MODELS ONLY',
        '--config', $Config,
        '--env-file', $EnvFile
    )
    if (-not $SkipComparison) {
        $Arguments += '--publish-comparison'
    }

    Write-Host ''
    Write-Host '=== Wznawianie lokalnej publikacji modeli ===' -ForegroundColor Cyan
    & $Python @Arguments
    $PublishCode = $LASTEXITCODE

    if ($PublishCode -ne 0) {
        throw "Lokalna publikacja zakończyla sie kodem $PublishCode."
    }

    $Verify = @'
from __future__ import annotations
import json, sys
from pathlib import Path
project_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_root))
from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import load_config
cfg = load_config(Path(sys.argv[2]), Path(sys.argv[3]))
repository = create_artifact_repository(cfg)
targets = [value.strip() for value in sys.argv[4].split(",") if value.strip()]
rows = []
for target in targets:
    pointer_key = repository.layout.active_hourly_model_pointer(target)
    pointer = repository.get_json(pointer_key)
    card = repository.get_json(pointer["model_card_object_key"])
    disclosure = dict(card.get("data_disclosure") or {})
    safe = (
        disclosure.get("raw_data_included") is False
        and disclosure.get("sqlite_included") is False
        and disclosure.get("training_snapshot_included") is False
        and disclosure.get("training_rows_included") is False
    )
    rows.append({
        "target": target,
        "model_version": pointer.get("model_version"),
        "provider": pointer.get("provider"),
        "pointer_key": pointer_key,
        "model_card_key": pointer.get("model_card_object_key"),
        "safe_disclosure": safe,
    })
report = {
    "status": "ok" if all(row["safe_disclosure"] for row in rows) else "failed",
    "backend": repository.store.backend_name,
    "models": rows,
    "external_writes": False,
}
print(json.dumps(report, ensure_ascii=True, indent=2))
raise SystemExit(0 if report["status"] == "ok" and report["backend"] == "local" else 4)
'@

    $VerifyText = (
        $Verify |
            & $Python - $ProjectRoot $Config $EnvFile $Targets |
            Out-String
    ).Trim()
    Write-Host ''
    Write-Host '=== Kontrola lokalnych wskaznikow i kart modeli ===' -ForegroundColor Cyan
    Write-Host $VerifyText

    if ($LASTEXITCODE -ne 0) {
        throw 'Kontrola lokalnych artefaktow nie przeszla.'
    }

    Write-Host ''
    Write-Host 'LOKALNA PUBLIKACJA MODELI ZAKONCZONA POPRAWNIE.' -ForegroundColor Green
    Write-Host "ObjectStore: $LocalRoot"
    Write-Host 'Nie wykonano zapisu do DigitalOcean.' -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    foreach ($Name in $SavedEnvironment.Keys) {
        $Saved = $SavedEnvironment[$Name]
        if ($Saved.Exists) {
            Set-Item -LiteralPath "Env:$Name" -Value $Saved.Value
        }
        else {
            Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        }
    }
}
