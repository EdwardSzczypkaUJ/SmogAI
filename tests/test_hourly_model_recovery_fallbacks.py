from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import joblib
from sqlalchemy import select

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.hourly.recovery import (
    audit_hourly_model_artifacts,
    recover_hourly_models_from_object_store,
)


def _configure_store(app_config, tmp_path: Path, targets: list[str]) -> None:  # type: ignore[no-untyped-def]
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "object-store"
    app_config.object_storage.prefix = "hf8-recovery"
    app_config.artifacts.upload_models = True
    app_config.hourly_forecasting.enabled = True
    app_config.hourly_forecasting.targets = targets


def _artifact(target: str, *, bootstrap: bool = False) -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "provider": "persistence",
        "task": "regression",
        "feature_columns": ["horizon_hours", "current_value"],
        "horizons_hours": [1, 2, 3],
        "provider_artifact": {
            "provider": "persistence",
            "task": "regression",
            "feature_columns": ["horizon_hours", "current_value"],
            "baseline_column": "current_value",
            "target_name": target,
        },
        "metadata": {"bootstrap": bootstrap},
        "trained_rows": 100,
        "trained_at": "2026-08-04T00:10:00Z",
    }


def test_recovery_scans_versioned_remote_objects_without_active_pointer(
    engine, app_config, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    _configure_store(app_config, tmp_path, ["PM10"])
    repository = create_artifact_repository(app_config)
    repository.store.ensure_container(create_if_missing=True)
    target = "PM10"
    version = "2026.08.04.001000-hourly-persistence-remote"
    artifact = _artifact(target)
    stored = repository.put_joblib(
        repository.layout.hourly_model_binary(target, version),
        artifact,
        immutable=True,
    )
    repository.put_json(
        repository.layout.hourly_model_card(target, version),
        {
            "schema_version": "2.0",
            "forecast_mode": "horizon-conditioned-hourly",
            "model_version": version,
            "target": target,
            "provider": "persistence",
            "feature_columns": artifact["feature_columns"],
            "horizons_hours": artifact["horizons_hours"],
            "metrics": {"mae": 1.25},
            "created_at": "2026-08-04T00:11:00Z",
            "artifact": {"object_key": stored.key, "checksum": stored.checksum},
        },
        immutable=True,
    )

    with session_scope(engine) as session:
        audit = audit_hourly_model_artifacts(session, app_config)
        assert audit["all_targets_recoverable"] is True
        assert audit["targets"][target]["selected"]["source"] == "remote_version"
        result = recover_hourly_models_from_object_store(session, app_config)

    assert result.errors == 0
    pointer = repository.get_json(repository.layout.active_hourly_model_pointer(target))
    assert pointer["model_version"] == version
    assert pointer["recovered_from"] == "remote_version"

    with session_scope(engine) as session:
        row = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == target,
                ModelVersion.forecast_horizon == 0,
                ModelVersion.active.is_(True),
            )
        )
        assert row is not None
        assert Path(row.artifact_path or "").exists()
        assert (row.metrics_json or {}).get("recovered_from_object_storage") is True


def test_recovery_uploads_and_activates_latest_local_joblib(
    engine, app_config, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    _configure_store(app_config, tmp_path, ["temperature_c"])
    repository = create_artifact_repository(app_config)
    repository.store.ensure_container(create_if_missing=True)
    target = "temperature_c"
    model_dir = app_config.paths.models_dir / "hourly" / target
    model_dir.mkdir(parents=True, exist_ok=True)

    older = model_dir / "2026.08.03.220000-hourly-persistence-old.joblib"
    newer = model_dir / "2026.08.04.001000-hourly-persistence-new.joblib"
    joblib.dump({**_artifact(target), "trained_at": "2026-08-03T22:00:00Z"}, older)
    joblib.dump({**_artifact(target), "trained_at": "2026-08-04T00:10:00Z"}, newer)

    with session_scope(engine) as session:
        audit = audit_hourly_model_artifacts(session, app_config)
        selected = audit["targets"][target]["selected"]
        assert selected["source"] == "local_file"
        assert selected["version"] == newer.stem
        result = recover_hourly_models_from_object_store(session, app_config)

    assert result.errors == 0
    pointer = repository.get_json(repository.layout.active_hourly_model_pointer(target))
    assert pointer["model_version"] == newer.stem
    assert pointer["recovered_from"] == "local_file"
    assert repository.store.exists(pointer["artifact_object_key"])
    assert repository.store.exists(pointer["model_card_object_key"])
    assert repository.store.exists(pointer["metrics_object_key"])


def test_audit_reports_missing_target_without_changing_database(
    engine, app_config, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    _configure_store(app_config, tmp_path, ["PM2.5"])
    create_artifact_repository(app_config).store.ensure_container(create_if_missing=True)

    with session_scope(engine) as session:
        audit = audit_hourly_model_artifacts(session, app_config)
        assert audit["status"] == "incomplete"
        assert audit["all_targets_recoverable"] is False
        assert audit["targets"]["PM2.5"]["selected"] is None
        assert audit["targets"]["PM2.5"]["candidate_count"] == 0

    with session_scope(engine) as session:
        assert session.scalar(select(ModelVersion)) is None


def test_required_model_upload_failure_is_not_silently_accepted_and_leaves_recovery_file(
    engine, app_config, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from smog_ai.hourly import trainer as trainer_module

    _configure_store(app_config, tmp_path, ["PM10"])
    artifact = _artifact("PM10")

    def fail_upload(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated Spaces outage")

    monkeypatch.setattr(trainer_module, "_upload_model", fail_upload)

    import pytest

    with pytest.raises(RuntimeError, match="saved locally"):
        with session_scope(engine) as session:
            trainer_module._register_model(
                session,
                app_config,
                target="PM10",
                provider_name="persistence",
                artifact=artifact,
                metrics={"mae": 2.0},
                data_start=None,
                data_end=None,
            )

    model_dir = app_config.paths.models_dir / "hourly" / "PM10"
    joblibs = list(model_dir.glob("*.joblib"))
    manifests = list(model_dir.glob("*.recovery.json"))
    assert len(joblibs) == 1
    assert len(manifests) == 1

    with session_scope(engine) as session:
        audit = audit_hourly_model_artifacts(session, app_config)
    assert audit["all_targets_recoverable"] is True
    selected = audit["targets"]["PM10"]["selected"]
    assert selected["source"] == "local_file"
    assert selected["metadata"]["recovery_manifest"]["remote_artifact_error"] == "simulated Spaces outage"
