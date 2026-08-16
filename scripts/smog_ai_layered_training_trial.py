from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIRMATION = "RUN ISOLATED LAYERED TRAINING TRIAL"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _production_fingerprint(runtime: Path) -> dict[str, Any]:
    database = runtime / "data" / "smog.db"
    active_models: list[list[Any]] = []
    with closing(
        sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        active_models = [
            list(row)
            for row in connection.execute(
                """
                SELECT id, parameter, algorithm, semantic_version, artifact_path,
                       active, activated_at
                  FROM model_versions
                 WHERE active=1
                 ORDER BY parameter, semantic_version
                """
            )
        ]
    encoded = json.dumps(
        active_models,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    model_root = runtime / "models"
    model_files = sorted(
        str(path.relative_to(model_root))
        for path in model_root.rglob("*")
        if path.is_file()
    ) if model_root.is_dir() else []
    model_files_encoded = json.dumps(
        model_files,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "active_models": active_models,
        "active_models_sha256": hashlib.sha256(encoded).hexdigest(),
        "production_model_file_count": len(model_files),
        "production_model_files_sha256": hashlib.sha256(
            model_files_encoded
        ).hexdigest(),
        "quick_pointer_sha256": _sha256(
            runtime / "training-datasets" / "quick" / "latest.json"
        ),
        "serving_pointer_sha256": _sha256(
            runtime / "object-store" / "serving" / "latest.json"
        ),
    }


def _chain_provenance(runtime: Path, profile: str) -> dict[str, Any]:
    production_pointer = runtime / "training-datasets" / profile / "latest.json"
    pointer = json.loads(production_pointer.read_text(encoding="utf-8-sig"))
    base_id = str(pointer["dataset_id"])
    chain_pointer = (
        runtime
        / "training-datasets"
        / "_incremental"
        / profile
        / f"base-{base_id}"
        / "latest.json"
    )
    chain = json.loads(chain_pointer.read_text(encoding="utf-8-sig"))
    deltas: list[dict[str, Any]] = []
    for value in chain.get("delta_manifests") or []:
        manifest_path = Path(str(value))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        deltas.append(
            {
                "delta_id": manifest.get("delta_id"),
                "sequence": manifest.get("sequence"),
                "manifest_path": str(manifest_path),
                "database_path": manifest.get("database_path"),
                "database_sha256": manifest.get("database_sha256"),
                "journal_start_seq": manifest.get("journal_start_seq"),
                "journal_end_seq": manifest.get("journal_end_seq"),
            }
        )
    return {
        "schema_version": "1.0",
        "storage_mode": "layered_candidate",
        "dataset_id": f"layered:{base_id}+{len(deltas)}d",
        "base_dataset_id": base_id,
        "base_manifest_path": pointer.get("manifest_path"),
        "base_database_path": pointer.get("database_path"),
        "delta_count": len(deltas),
        "deltas": deltas,
        "chain_pointer": str(chain_pointer),
        "immutable": True,
        "production_pointer_changed": False,
    }


def _trial_plan(
    *,
    runtime: Path,
    profile: str,
    target: str,
    algorithm: str,
    maximum_rows: int,
) -> dict[str, Any]:
    provenance = _chain_provenance(runtime, profile)
    return {
        "status": "ready",
        "mode": "plan",
        "profile": profile,
        "target": target,
        "algorithm": algorithm,
        "maximum_rows": maximum_rows,
        "dataset_provenance": provenance,
        "isolation": {
            "metadata_database": "new isolated SQLite database",
            "model_directory": "new isolated trial directory",
            "object_storage_enabled": False,
            "mlflow_enabled": False,
            "production_model_activation": False,
            "production_registry_write": False,
            "production_pointer_write": False,
        },
        "required_confirmation": CONFIRMATION,
        "next_action": "run_isolated_trial",
    }


def _run_trial(
    *,
    project_root: Path,
    runtime: Path,
    config_path: Path,
    env_path: Path,
    profile: str,
    target: str,
    algorithm: str,
    maximum_rows: int,
    confirmation: str,
) -> dict[str, Any]:
    from sqlalchemy import select

    from smog_ai.config import load_config
    from smog_ai.database.engine import create_db_engine, init_database, session_scope
    from smog_ai.database.models import ModelVersion
    from smog_ai.hourly.trainer import train_hourly_models
    from smog_ai.progress import ProgressReporter
    from smog_ai.training_delta import create_layered_sqlalchemy_engine

    if confirmation != CONFIRMATION:
        raise PermissionError(f"Exact confirmation required: {CONFIRMATION}")
    production_before = _production_fingerprint(runtime)
    provenance = _chain_provenance(runtime, profile)
    trial_id = f"layered-trial-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    trial_root = runtime / "training-datasets" / "_incremental" / "trials" / trial_id
    trial_root.mkdir(parents=True, exist_ok=False)

    original = load_config(config_path, env_path)
    config = original.model_copy(deep=True)
    metadata_database = trial_root / "trial-metadata.db"
    config.paths.data_dir = trial_root / "data"
    config.paths.database_path = metadata_database
    config.paths.models_dir = trial_root / "models"
    config.paths.logs_dir = trial_root / "logs"
    config.paths.snapshots_dir = trial_root / "snapshots"
    config.paths.backups_dir = trial_root / "backups"
    config.paths.temp_dir = trial_root / "tmp"
    config.spatial.local_cache_dir = trial_root / "spatial-cache"
    config.imgw_archive.cache_dir = trial_root / "imgw-archive-cache"
    config.training_snapshot.root_dir = trial_root / "training-snapshots"
    config.data_validation.reports_dir = trial_root / "validation-reports"
    config.mlflow.enabled = False
    config.mlflow.strict = False
    config.mlflow.local_artifact_dir = trial_root / "mlflow"
    config.mlflow.comparison_path = trial_root / "model-comparison.json"
    config.mlflow.publish_comparison_to_object_storage = False
    config.object_storage.enabled = False
    config.object_storage.local_root = trial_root / "object-store"
    config.artifacts.upload_models = False
    config.observability.local_feedback_path = trial_root / "feedback" / "scores.jsonl"
    config.training.input_source = "database"
    config.training.allow_database_fallback = False
    config.hourly_forecasting.targets = [target]
    config.hourly_forecasting.use_predicted_weather_for_pm = True
    config.hourly_forecasting.target_algorithms[target] = [algorithm]
    selected_profile = (
        config.hourly_forecasting.training_policy.quick
        if profile == "quick"
        else config.hourly_forecasting.training_policy.full
    )
    selected_profile.algorithms[target] = [algorithm]
    selected_profile.maximum_rows_per_target = max(10_000, int(maximum_rows))
    selected_profile.validation_max_rows = min(
        max(1_000, int(maximum_rows) // 5),
        selected_profile.maximum_rows_per_target,
    )
    selected_profile.maximum_training_days_by_target[target] = min(
        selected_profile.maximum_training_days_by_target.get(target, 365),
        90,
    )
    selected_profile.samples_per_horizon_bucket = 1
    selected_profile.fit_quantiles = False
    selected_profile.max_wall_time_seconds = 900
    config.ensure_directories()

    previous_database_url = os.environ.get("SMOG_AI_DATABASE_URL")
    os.environ["SMOG_AI_DATABASE_URL"] = (
        f"sqlite:///{metadata_database.resolve().as_posix()}"
    )
    metadata_engine = create_db_engine(config)
    layered_engine = create_layered_sqlalchemy_engine(
        runtime_root=runtime,
        profile=profile,
    )
    init_database(metadata_engine)
    reporter = ProgressReporter(
        config.paths.logs_dir,
        run_type="layered-training-trial",
        stage_weights={"training": 1.0},
        stage_default_seconds={"training": 600.0},
        heartbeat_seconds=5.0,
        run_id=trial_id,
    ).start(task=f"isolated layered training trial: {target}/{algorithm}")
    print(f"Trial root: {trial_root}", flush=True)
    print(f"Progress JSON: {reporter.current_path}", flush=True)
    print("Production registry and model directories are read-only for this trial.", flush=True)

    try:
        with session_scope(metadata_engine) as metadata_session:
            with session_scope(layered_engine) as training_session:
                stats = train_hourly_models(
                    metadata_session,
                    config,
                    reporter,
                    profile_name=profile,
                    training_session=training_session,
                    dataset_provenance=provenance,
                    commit_live_metadata=True,
                    activation_policy="candidate_only",
                )
            models = metadata_session.scalars(
                select(ModelVersion).order_by(ModelVersion.parameter, ModelVersion.created_at)
            ).all()
            trial_models = [
                {
                    "parameter": model.parameter,
                    "algorithm": model.algorithm,
                    "semantic_version": model.semantic_version,
                    "active_in_trial_only": bool(model.active),
                    "artifact_path": model.artifact_path,
                    "artifact_exists": bool(
                        model.artifact_path and Path(model.artifact_path).is_file()
                    ),
                    "quality_status": (model.metrics_json or {}).get("quality_status"),
                    "quality_classification": (model.metrics_json or {}).get(
                        "quality_classification"
                    ),
                    "data_provenance": (model.metrics_json or {}).get("data_provenance"),
                }
                for model in models
            ]
        status = "ok" if stats.errors == 0 and any(
            row["parameter"] == target and row["artifact_exists"]
            for row in trial_models
        ) else "failed"
        production_after = _production_fingerprint(runtime)
        production_unchanged = production_before == production_after
        if not production_unchanged:
            status = "failed"
        result = {
            "status": status,
            "mode": "isolated_trial",
            "trial_id": trial_id,
            "trial_root": str(trial_root),
            "trial_metadata_database": str(metadata_database),
            "progress_json": str(reporter.current_path),
            "profile": profile,
            "target": target,
            "algorithm": algorithm,
            "maximum_rows": maximum_rows,
            "dataset_provenance": provenance,
            "training_stats": stats.as_dict(),
            "trial_models": trial_models,
            "production_before": production_before,
            "production_after": production_after,
            "production_unchanged": production_unchanged,
            "production_model_activation": False,
            "object_storage_write": False,
            "mlflow_write": False,
            "next_action": "integrate_layered_selector" if status == "ok" else "inspect_trial_failure",
        }
        if status == "ok":
            reporter.finish("success", detail=result)
        else:
            reporter.fail("isolated layered training trial failed")
        report_path = trial_root / "trial-report.json"
        _atomic_json(report_path, result)
        result["report_path"] = str(report_path)
        return result
    except BaseException as exc:
        reporter.fail(exc)
        production_after = _production_fingerprint(runtime)
        failure = {
            "status": "failed",
            "mode": "isolated_trial",
            "trial_id": trial_id,
            "trial_root": str(trial_root),
            "error": f"{type(exc).__name__}: {exc}",
            "production_before": production_before,
            "production_after": production_after,
            "production_unchanged": production_before == production_after,
            "production_model_activation": False,
            "object_storage_write": False,
            "mlflow_write": False,
            "next_action": "inspect_trial_failure",
        }
        _atomic_json(trial_root / "trial-report.json", failure)
        return failure
    finally:
        layered_engine.dispose()
        metadata_engine.dispose()
        if previous_database_url is None:
            os.environ.pop("SMOG_AI_DATABASE_URL", None)
        else:
            os.environ["SMOG_AI_DATABASE_URL"] = previous_database_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--profile", default="quick", choices=("quick", "full"))
    parser.add_argument("--target", default="PM10")
    parser.add_argument("--algorithm", default="ridge")
    parser.add_argument("--maximum-rows", type=int, default=50_000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()

    runtime = Path(args.runtime_root).resolve()
    if args.apply:
        result = _run_trial(
            project_root=Path(args.project_root).resolve(),
            runtime=runtime,
            config_path=Path(args.config).resolve(),
            env_path=Path(args.env_file).resolve(),
            profile=args.profile,
            target=args.target,
            algorithm=args.algorithm,
            maximum_rows=args.maximum_rows,
            confirmation=args.confirmation,
        )
    else:
        result = _trial_plan(
            runtime=runtime,
            profile=args.profile,
            target=args.target,
            algorithm=args.algorithm,
            maximum_rows=args.maximum_rows,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"ok", "ready"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
