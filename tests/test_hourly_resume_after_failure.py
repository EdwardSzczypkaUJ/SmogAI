from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import joblib
from sqlalchemy import select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.domain import StageStats
from smog_ai.hourly import resume as resume_module


def _configure(app_config, tmp_path: Path, targets: list[str]) -> None:  # type: ignore[no-untyped-def]
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "object-store"
    app_config.object_storage.prefix = "resume-test"
    app_config.artifacts.upload_models = True
    app_config.hourly_forecasting.enabled = True
    app_config.hourly_forecasting.targets = targets
    app_config.documentation.enabled = True


def _artifact(target: str) -> dict:  # type: ignore[no-untyped-def]
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
        "metadata": {"bootstrap": False},
        "trained_rows": 123,
        "trained_at": "2026-08-04T01:00:00Z",
    }


def test_resume_recovers_local_models_and_runs_only_downstream(
    engine, app_config, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _configure(app_config, tmp_path, ["PM10"])
    model_dir = app_config.paths.models_dir / "hourly" / "PM10"
    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        _artifact("PM10"),
        model_dir / "2026.08.04.010000-hourly-persistence-local.joblib",
    )

    calls: list[str] = []
    monkeypatch.setattr(
        resume_module,
        "load_documentation_bundle",
        lambda config: SimpleNamespace(metadata={"status": "ok"}),
    )
    monkeypatch.setattr(
        resume_module,
        "publish_documentation",
        lambda config: calls.append("documentation") or StageStats(inserted=1),
    )
    monkeypatch.setattr(
        resume_module,
        "create_hourly_forecasts",
        lambda session, config, progress=None: calls.append("prediction") or StageStats(inserted=2),
    )
    monkeypatch.setattr(
        resume_module,
        "build_spatial_surfaces",
        lambda session, config, progress=None: calls.append("spatial") or StageStats(inserted=3),
    )
    monkeypatch.setattr(
        resume_module,
        "build_snapshot_stage",
        lambda session, config: calls.append("snapshot") or StageStats(inserted=1),
    )
    monkeypatch.setattr(
        resume_module,
        "retry_publications",
        lambda session, config: calls.append("publication") or StageStats(inserted=1),
    )

    result = resume_module.resume_hourly_after_failure(
        engine,
        app_config,
        retrain_if_missing=False,
    )

    assert result.errors == 0
    assert result.details["status"] == "success"
    assert calls == ["documentation", "prediction", "spatial", "snapshot", "publication"]
    assert result.details["model_recovery"]["inserted"] == 1

    with session_scope(engine) as session:
        active = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == "PM10",
                ModelVersion.forecast_horizon == 0,
                ModelVersion.active.is_(True),
            )
        )
        assert active is not None


def test_resume_stops_before_downstream_when_models_are_not_recoverable(
    engine, app_config, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _configure(app_config, tmp_path, ["PM10", "PM2.5"])
    monkeypatch.setattr(
        resume_module,
        "load_documentation_bundle",
        lambda config: SimpleNamespace(metadata={"status": "ok"}),
    )

    result = resume_module.resume_hourly_after_failure(
        engine,
        app_config,
        retrain_if_missing=False,
    )

    assert result.errors == 2
    assert result.details["status"] == "retraining_required"
    assert result.details["missing_targets"] == ["PM10", "PM2.5"]
