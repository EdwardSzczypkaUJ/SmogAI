[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $EnvFile = Join-Path $RuntimeRoot 'smog-ai.env'
    $LocalEnv = Join-Path $RuntimeRoot 'smog-ai.local-training.env'
    $ReportRoot = Join-Path $RuntimeRoot 'reports\hf20'

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        (Join-Path $ProjectRoot 'smog_ai\__init__.py'),
        $Python
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null
    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    $ConfigPaths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($Candidate in @(
        (Join-Path $RuntimeRoot 'config.yaml'),
        (Join-Path $RuntimeRoot 'config.local-training.yaml')
    )) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            [void]$ConfigPaths.Add($Candidate)
        }
    }
    if ($ConfigPaths.Count -eq 0) {
        throw "Nie znaleziono config.yaml ani config.local-training.yaml w $RuntimeRoot"
    }

    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $ReportPath = Join-Path $ReportRoot "hf20-config-$Stamp.json"

    $PythonSource = @'
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

project_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(project_root))

import yaml
from smog_ai.config import load_config

runtime_root = Path(sys.argv[2]).resolve()
report_path = Path(sys.argv[3]).resolve()
config_paths = [Path(value).resolve() for value in sys.argv[4:]]

results = []
for path in config_paths:
    payload = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    hourly = payload.setdefault("hourly_forecasting", {})
    hourly["serving_horizon_hours"] = 48
    hourly["maximum_source_delay_hours"] = 12
    hourly["maximum_model_horizon_hours"] = 60
    # Keep the legacy field at 48; it remains the user-facing serving width.
    hourly["maximum_horizon_hours"] = 48

    policy = hourly.setdefault("training_policy", {})
    for profile_name in ("quick", "full"):
        profile = policy.setdefault(profile_name, {})
        edges = [int(value) for value in profile.get(
            "horizon_bucket_edges", [6, 12, 24, 48]
        )]
        profile["horizon_bucket_edges"] = sorted({*edges, 60})

    precipitation = hourly.setdefault("precipitation", {})
    precipitation.setdefault("minimum_mae_improvement_vs_persistence", 0.01)
    precipitation.setdefault("minimum_brier_skill_vs_climatology", 0.0)
    precipitation.setdefault("minimum_brier_skill_vs_persistence", 0.0)
    precipitation.setdefault("minimum_roc_auc", 0.60)
    precipitation.setdefault("maximum_absolute_bias_mm", 1.0)
    precipitation["mark_experimental_on_failure"] = True
    precipitation["activate_experimental_locally"] = True

    mlflow = payload.setdefault("mlflow", {})
    mlflow.setdefault("enabled", False)
    mlflow.setdefault("strict", False)
    mlflow.setdefault("tracking_uri", "")
    mlflow.setdefault("experiment_name", "smog-ai-hourly")
    mlflow.setdefault("registry_enabled", False)
    mlflow.setdefault("registered_model_prefix", "smog-ai-hourly")
    mlflow.setdefault("log_model_artifacts", False)
    mlflow.setdefault("maximum_runs_per_target", 100)
    mlflow.setdefault("local_artifact_dir", "mlflow")
    mlflow.setdefault(
        "comparison_path", "reports/mlflow/model-comparison.json"
    )
    mlflow["publish_comparison_to_object_storage"] = False
    mlflow.setdefault("ui_url", None)

    observability = payload.setdefault("observability", {})
    observability.setdefault("backend", "none")
    observability.setdefault("prompt_template_version", "air-query-v1")
    observability.setdefault("feedback_enabled", True)
    observability.setdefault(
        "local_feedback_path", "feedback/prompt-feedback.jsonl"
    )
    observability.setdefault("flush_on_request", False)

    # The local-training config must never gain cloud writes by this update.
    if path.name == "config.local-training.yaml":
        payload.setdefault("object_storage", {})["enabled"] = False
        artifacts = payload.setdefault("artifacts", {})
        artifacts["export_after_collection"] = False
        artifacts["export_training_frames_before_training"] = False
        artifacts["upload_models"] = False
        data_flow = payload.setdefault("data_flow", {})
        data_flow["training_mode"] = "direct_local"
        data_flow["mirror_operational_to_object_store"] = False
        data_flow["history_cache_mode"] = "local"
        payload.setdefault("training_snapshot", {})[
            "mirror_manifest_to_object_storage"
        ] = False
        payload.setdefault("publication", {})["enabled"] = False
        observability["backend"] = "none"

    backup = path.with_name(path.name + ".before-hf20")
    backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".hf20.tmp")
    temporary.write_text(
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            width=110,
        ),
        encoding="utf-8",
    )

    env_path = runtime_root / (
        "smog-ai.local-training.env"
        if path.name == "config.local-training.yaml"
        else "smog-ai.env"
    )
    cfg = load_config(temporary, env_path if env_path.exists() else None)
    checks = {
        "serving_horizon_hours": cfg.hourly_forecasting.serving_horizon_count,
        "maximum_source_delay_hours": (
            cfg.hourly_forecasting.maximum_source_delay_hours
        ),
        "maximum_model_horizon_hours": (
            cfg.hourly_forecasting.model_horizon_maximum
        ),
        "quick_edges": list(
            cfg.hourly_forecasting.training_policy.quick.horizon_bucket_edges
        ),
        "full_edges": list(
            cfg.hourly_forecasting.training_policy.full.horizon_bucket_edges
        ),
        "mlflow_enabled": cfg.mlflow.enabled,
        "mlflow_publish_comparison": (
            cfg.mlflow.publish_comparison_to_object_storage
        ),
        "observability_backend": cfg.observability.backend,
    }
    if checks["serving_horizon_hours"] != 48:
        raise RuntimeError(f"Unexpected serving horizon: {checks}")
    if checks["maximum_source_delay_hours"] != 12:
        raise RuntimeError(f"Unexpected source delay: {checks}")
    if checks["maximum_model_horizon_hours"] != 60:
        raise RuntimeError(f"Unexpected model horizon: {checks}")
    if 60 not in checks["quick_edges"] or 60 not in checks["full_edges"]:
        raise RuntimeError(f"Training profiles do not cover h60: {checks}")
    if path.name == "config.local-training.yaml":
        if cfg.object_storage.enabled or cfg.artifacts.upload_models:
            raise RuntimeError("Local-only config would allow external writes")

    temporary.replace(path)
    results.append(
        {
            "config": str(path),
            "backup": str(backup),
            "checks": checks,
        }
    )

report = {
    "schema_version": "1.0",
    "generated_at_utc": datetime.now(UTC).isoformat(),
    "status": "ok",
    "time_contract": {
        "serving_horizon_hours": 48,
        "maximum_source_delay_hours": 12,
        "maximum_model_horizon_hours": 60,
    },
    "configs": results,
    "external_writes_enabled_by_this_script": False,
}
report_path.write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(report, ensure_ascii=True, indent=2))
'@

    $TempPython = Join-Path $env:TEMP (
        'smog-ai-hf20-config-' + [guid]::NewGuid().ToString('N') + '.py'
    )
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($TempPython, $PythonSource, $Utf8NoBom)
    try {
        $Arguments = @(
            $TempPython,
            $ProjectRoot,
            $RuntimeRoot,
            $ReportPath
        ) + $ConfigPaths.ToArray()
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Konfiguracja HF20 zakonczyla sie kodem $LASTEXITCODE."
        }
    }
    finally {
        Remove-Item -LiteralPath $TempPython -Force -ErrorAction SilentlyContinue
    }

    Write-Host ''
    Write-Host 'KONFIGURACJA HF20 ZASTOSOWANA I ZWALIDOWANA.' -ForegroundColor Green
    Write-Host "Raport: $ReportPath"
    Write-Host 'Nie wlaczono MLflow, Langfuse ani publikacji do ObjectStore.' -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
