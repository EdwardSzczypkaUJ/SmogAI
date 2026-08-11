from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import joblib
import pytest

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.artifacts.repository import canonical_json_bytes
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.mlops.publish import (
    PUBLISH_CONFIRMATION,
    publish_approved_hourly_models,
)
from smog_ai.storage.base import ObjectConflictError


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
        "quality_status": "accepted",
        "data_provenance": {
            "dataset_id": "dataset-1",
            "training_snapshot": {
                "dataset_id": "dataset-1",
                "database_sha256": "a" * 64,
                "immutable": True,
            },
        },
    }
    return ModelVersion(
        model_name=f"hourly-{target}-ridge",
        algorithm="ridge",
        parameter=target,
        forecast_horizon=0,
        semantic_version="2026.01.01-idempotency-test",
        artifact_path=str(artifact_path),
        feature_columns_json=["horizon_hours"],
        metrics_json=metrics,
        training_data_start=datetime(2025, 1, 1, tzinfo=UTC),
        training_data_end=datetime(2025, 12, 31, tzinfo=UTC),
        active=True,
    )


def _configure(app_config, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "object-store"
    app_config.artifacts.upload_models = True
    app_config.mlflow.enabled = False
    app_config.mlflow.comparison_path = tmp_path / "comparison.json"


def test_publication_can_be_repeated_after_metrics_receive_remote_artifact(
    engine, app_config, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    _configure(app_config, tmp_path)
    with session_scope(engine) as session:
        session.add(_model(tmp_path))
        session.flush()
        first = publish_approved_hourly_models(
            session,
            app_config,
            targets=["PM10"],
            confirmation=PUBLISH_CONFIRMATION,
            publish_comparison=False,
        )
        second = publish_approved_hourly_models(
            session,
            app_config,
            targets=["PM10"],
            confirmation=PUBLISH_CONFIRMATION,
            publish_comparison=False,
        )

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert second["idempotent_publication"] is True
    assert second["models"][0]["write_status"]["model_card"] == "reused"
    assert second["models"][0]["write_status"]["model_metrics"] == "reused"

    repository = create_artifact_repository(app_config)
    card = repository.get_json(
        repository.layout.hourly_model_card(
            "PM10",
            "2026.01.01-idempotency-test",
        )
    )
    assert "published_at" not in card
    assert "remote_artifact" not in card["metrics"]


def test_legacy_volatile_card_is_reused_without_overwrite(
    engine, app_config, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    _configure(app_config, tmp_path)
    with session_scope(engine) as session:
        session.add(_model(tmp_path))
        session.flush()
        publish_approved_hourly_models(
            session,
            app_config,
            targets=["PM10"],
            confirmation=PUBLISH_CONFIRMATION,
            publish_comparison=False,
        )

        repository = create_artifact_repository(app_config)
        card_key = repository.layout.hourly_model_card(
            "PM10",
            "2026.01.01-idempotency-test",
        )
        metrics_key = repository.layout.hourly_model_metrics(
            "PM10",
            "2026.01.01-idempotency-test",
        )
        legacy = repository.get_json(card_key)
        legacy["published_at"] = "2026-01-02T03:04:05+00:00"
        legacy["metrics"]["remote_artifact"] = {
            "published_at": "2026-01-02T03:04:05+00:00",
            "artifact_object_key": "legacy",
        }
        root = Path(app_config.object_storage.local_root)
        for key in (card_key, metrics_key):
            path = root / Path(*key.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json_bytes(legacy))

        result = publish_approved_hourly_models(
            session,
            app_config,
            targets=["PM10"],
            confirmation=PUBLISH_CONFIRMATION,
            publish_comparison=False,
        )

    status = result["models"][0]["write_status"]
    assert status["model_card"] == "reused_legacy"
    assert status["model_metrics"] == "reused_legacy"


def test_substantive_immutable_card_difference_still_fails(
    engine, app_config, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    _configure(app_config, tmp_path)
    with session_scope(engine) as session:
        session.add(_model(tmp_path))
        session.flush()
        publish_approved_hourly_models(
            session,
            app_config,
            targets=["PM10"],
            confirmation=PUBLISH_CONFIRMATION,
            publish_comparison=False,
        )

        repository = create_artifact_repository(app_config)
        card_key = repository.layout.hourly_model_card(
            "PM10",
            "2026.01.01-idempotency-test",
        )
        card = repository.get_json(card_key)
        card["provider"] = "substantively-different-provider"
        path = Path(app_config.object_storage.local_root) / Path(
            *card_key.split("/")
        )
        path.write_bytes(canonical_json_bytes(card))

        with pytest.raises(ObjectConflictError, match="substantively different"):
            publish_approved_hourly_models(
                session,
                app_config,
                targets=["PM10"],
                confirmation=PUBLISH_CONFIRMATION,
                publish_comparison=False,
            )
