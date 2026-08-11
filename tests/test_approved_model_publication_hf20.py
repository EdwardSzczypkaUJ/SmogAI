from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import joblib
import pytest

from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.mlops.publish import (
    PUBLISH_CONFIRMATION,
    model_publication_failures,
    publish_approved_hourly_models,
)


def _model(tmp_path: Path, *, target: str = "PM10") -> ModelVersion:
    artifact_path = tmp_path / f"{target}.joblib"
    joblib.dump(
        {
            "feature_columns": ["horizon_hours"],
            "horizons_hours": list(range(1, 61)),
        },
        artifact_path,
    )
    metrics = {
        "bootstrap": False,
        "improvement_vs_persistence": 0.10,
        "data_provenance": {
            "dataset_id": "dataset-1",
            "training_snapshot": {
                "dataset_id": "dataset-1",
                "database_sha256": "a" * 64,
                "immutable": True,
            },
        },
    }
    if target == "precipitation_mm":
        metrics["precipitation_quality_gate"] = {
            "passed": True,
            "status": "accepted",
            "failures": [],
        }
    return ModelVersion(
        model_name=f"hourly-{target}-ridge",
        algorithm=(
            "hurdle_hist_gradient_boosting"
            if target == "precipitation_mm"
            else "ridge"
        ),
        parameter=target,
        forecast_horizon=0,
        semantic_version="2026.01.01-test",
        artifact_path=str(artifact_path),
        feature_columns_json=["horizon_hours"],
        metrics_json=metrics,
        training_data_start=datetime(2025, 1, 1, tzinfo=UTC),
        training_data_end=datetime(2025, 12, 31, tzinfo=UTC),
        active=True,
    )


def test_publication_gate_requires_immutable_snapshot(tmp_path, app_config) -> None:  # type: ignore[no-untyped-def]
    model = _model(tmp_path)
    assert model_publication_failures(app_config, model) == []
    metrics = dict(model.metrics_json or {})
    metrics["data_provenance"]["training_snapshot"]["immutable"] = False
    model.metrics_json = metrics
    reasons = {row["reason"] for row in model_publication_failures(app_config, model)}
    assert "immutable_snapshot_required" in reasons


def test_publication_requires_exact_confirmation(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.artifacts.upload_models = True
    with session_scope(engine) as session:
        with pytest.raises(ValueError):
            publish_approved_hourly_models(
                session,
                app_config,
                targets=["PM10"],
                confirmation="yes",
            )


def test_approved_model_publication_excludes_training_data(
    engine, app_config, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "object-store"
    app_config.artifacts.upload_models = True
    app_config.mlflow.comparison_path = tmp_path / "comparison.json"
    with session_scope(engine) as session:
        session.add(_model(tmp_path))
        session.flush()
        result = publish_approved_hourly_models(
            session,
            app_config,
            targets=["PM10"],
            confirmation=PUBLISH_CONFIRMATION,
        )
    assert result["status"] == "ok"
    assert "sqlite_database" in result["explicitly_not_published"]
    assert "training_snapshot" in result["explicitly_not_published"]
    assert result["models"][0]["target"] == "PM10"
