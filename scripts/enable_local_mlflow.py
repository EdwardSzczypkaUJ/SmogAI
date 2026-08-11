from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load_yaml() -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency is required by app
        raise RuntimeError("PyYAML is required") from exc
    return yaml


def _belongs_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _env_for_config(runtime_root: Path, config_path: Path) -> Path | None:
    candidate = runtime_root / (
        "smog-ai.local-training.env"
        if config_path.name == "config.local-training.yaml"
        else "smog-ai.env"
    )
    return candidate if candidate.exists() else None


def _validate_local_only(cfg: Any, config_path: Path) -> None:
    if config_path.name != "config.local-training.yaml":
        return
    failures: list[str] = []
    if cfg.object_storage.enabled:
        failures.append("object_storage.enabled")
    if cfg.artifacts.upload_models:
        failures.append("artifacts.upload_models")
    if cfg.artifacts.export_after_collection:
        failures.append("artifacts.export_after_collection")
    if cfg.artifacts.export_training_frames_before_training:
        failures.append("artifacts.export_training_frames_before_training")
    if cfg.data_flow.mirror_operational_to_object_store:
        failures.append("data_flow.mirror_operational_to_object_store")
    if cfg.training_snapshot.mirror_manifest_to_object_storage:
        failures.append("training_snapshot.mirror_manifest_to_object_storage")
    if cfg.publication.enabled:
        failures.append("publication.enabled")
    if failures:
        raise RuntimeError(
            "Local-training config would allow external writes: "
            + ", ".join(failures)
        )


def update_config(
    *,
    project_root: Path,
    runtime_root: Path,
    path: Path,
    tracking_uri: str,
    stamp: str,
) -> dict[str, Any]:
    yaml = _load_yaml()
    from smog_ai.config import load_config

    payload = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    mlflow = payload.setdefault("mlflow", {})
    mlflow.update(
        {
            "enabled": True,
            "strict": True,
            "tracking_uri": tracking_uri,
            "experiment_name": "smog-ai-hourly",
            "registry_enabled": True,
            "registered_model_prefix": "smog-ai-hourly",
            "log_model_artifacts": False,
            "maximum_runs_per_target": 200,
            "local_artifact_dir": "mlflow/artifacts",
            "comparison_path": "reports/mlflow/model-comparison.json",
            "publish_comparison_to_object_storage": False,
            "ui_url": tracking_uri,
        }
    )

    backup = path.with_name(path.name + f".before-local-mlflow-{stamp}")
    backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".mlflow.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(
                payload,
                allow_unicode=True,
                sort_keys=False,
                width=110,
            ),
            encoding="utf-8",
        )
        env_path = _env_for_config(runtime_root, path)
        cfg = load_config(temporary, env_path)
        if not cfg.mlflow.enabled:
            raise RuntimeError("MLflow config validation failed: disabled")
        if cfg.mlflow.tracking_uri != tracking_uri:
            raise RuntimeError("MLflow config validation failed: tracking URI")
        if cfg.mlflow.publish_comparison_to_object_storage:
            raise RuntimeError(
                "MLflow comparison publication must remain disabled"
            )
        _validate_local_only(cfg, path)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "config": str(path),
        "backup": str(backup),
        "local_only_preserved": path.name == "config.local-training.yaml",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", required=True)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    runtime_root = args.runtime_root.resolve()
    sys.path.insert(0, str(project_root))

    import smog_ai

    module_path = Path(smog_ai.__file__).resolve()
    if not _belongs_to(module_path, project_root):
        raise RuntimeError(
            f"smog_ai loaded from {module_path}, outside {project_root}"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results = [
        update_config(
            project_root=project_root,
            runtime_root=runtime_root,
            path=path.resolve(),
            tracking_uri=args.tracking_uri,
            stamp=stamp,
        )
        for path in args.config
    ]

    report = {
        "schema_version": "1.1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "ok",
        "tracking_uri": args.tracking_uri,
        "registry_backend": str(runtime_root / "mlflow" / "mlflow.db"),
        "artifact_root": str(runtime_root / "mlflow" / "artifacts"),
        "configs": results,
        "external_publication": False,
        "smog_ai_module": str(module_path),
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
