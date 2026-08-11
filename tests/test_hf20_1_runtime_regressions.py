from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml


def test_cli_exposes_serving_contract_auditor() -> None:
    import smog_ai.cli as cli
    from smog_ai.hourly.audit import audit_latest_hourly_serving_contract

    assert (
        cli.audit_latest_hourly_serving_contract
        is audit_latest_hourly_serving_contract
    )


def test_mlflow_helper_updates_local_config_without_cloud_writes(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    source = project_root / "config.example.yaml"
    payload = yaml.safe_load(source.read_text(encoding="utf-8-sig")) or {}

    payload.setdefault("object_storage", {})["enabled"] = False
    artifacts = payload.setdefault("artifacts", {})
    artifacts["upload_models"] = False
    artifacts["export_after_collection"] = False
    artifacts["export_training_frames_before_training"] = False
    payload.setdefault("publication", {})["enabled"] = False
    payload.setdefault("data_flow", {})[
        "mirror_operational_to_object_store"
    ] = False
    payload.setdefault("training_snapshot", {})[
        "mirror_manifest_to_object_storage"
    ] = False

    config_path = runtime_root / "config.local-training.yaml"
    config_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    env_path = runtime_root / "smog-ai.local-training.env"
    env_path.write_text("", encoding="utf-8")
    report_path = runtime_root / "report.json"
    helper = project_root / "scripts" / "enable_local_mlflow.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--project-root",
            str(project_root),
            "--runtime-root",
            str(runtime_root),
            "--tracking-uri",
            "http://127.0.0.1:5000",
            "--report-path",
            str(report_path),
            "--config",
            str(config_path),
        ],
        cwd=project_root,
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["mlflow"]["enabled"] is True
    assert updated["mlflow"]["tracking_uri"] == "http://127.0.0.1:5000"
    assert updated["mlflow"]["publish_comparison_to_object_storage"] is False
    assert updated["object_storage"]["enabled"] is False
    assert updated["artifacts"]["upload_models"] is False
    assert updated["publication"]["enabled"] is False
    assert (
        updated["training_snapshot"]["mirror_manifest_to_object_storage"]
        is False
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["external_publication"] is False
