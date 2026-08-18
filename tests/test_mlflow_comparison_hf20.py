from __future__ import annotations

from datetime import UTC, datetime

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.mlops.comparison import (
    build_public_model_comparison_payload,
    export_model_comparison,
)
from smog_ai.mlops.mlflow_bridge import create_mlflow_bridge
from smog_ai.storage.local import MemoryObjectStore


def test_mlflow_is_noop_by_default(app_config) -> None:  # type: ignore[no-untyped-def]
    bridge = create_mlflow_bridge(app_config.mlflow)
    run_id = bridge.log_candidate(
        target="PM10",
        provider="ridge",
        profile="quick",
        metrics={"mae": 1.0},
        parameters={},
        dataset_provenance=None,
    )
    assert run_id is None


def test_model_comparison_is_exported_locally_without_object_store(
    engine, app_config
) -> None:  # type: ignore[no-untyped-def]
    app_config.mlflow.comparison_path = (
        app_config.paths.data_dir.parent / "reports" / "comparison.json"
    )
    with session_scope(engine) as session:
        session.add(
            ModelVersion(
                model_name="hourly-PM10-ridge",
                algorithm="ridge",
                parameter="PM10",
                forecast_horizon=0,
                semantic_version="2026.01.01-test",
                artifact_path="model.joblib",
                feature_columns_json=["horizon_hours"],
                metrics_json={
                    "mae": 2.0,
                    "rmse": 3.0,
                    "dataset_id": "dataset-1",
                    "dataset_sha256": "a" * 64,
                },
                training_data_start=datetime(2025, 1, 1, tzinfo=UTC),
                training_data_end=datetime(2025, 12, 31, tzinfo=UTC),
                active=True,
            )
        )
        session.flush()
        result = export_model_comparison(session, app_config, publish=False)

    assert result["published"] is None
    assert result["model_count"] == 1
    assert app_config.mlflow.comparison_path.exists()
    model = result["payload"]["models"][0]
    assert model["target"] == "PM10"
    assert model["metrics"]["mae"] == 2.0


def test_published_model_comparison_is_read_back_and_verified(
    engine, app_config, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    repository = ArtifactRepository(MemoryObjectStore())
    monkeypatch.setattr(
        "smog_ai.mlops.comparison.create_artifact_repository",
        lambda config: repository,
    )
    app_config.object_storage.enabled = True
    with session_scope(engine) as session:
        session.add(
            ModelVersion(
                model_name="hourly-PM10-ridge",
                algorithm="ridge",
                parameter="PM10",
                forecast_horizon=0,
                semantic_version="safe-version",
                artifact_path="private-model.joblib",
                metrics_json={"mae": 1.25},
                active=True,
            )
        )
        session.flush()
        result = export_model_comparison(session, app_config, publish=True)

    assert result["published"]["remote_verified"] is True
    assert result["published"]["pointer_published_last"] is True
    assert result["published"]["model_count"] == 1
    assert result["published"]["candidate_run_count"] == 0
    immutable_key = result["published"]["immutable_object_key"]
    assert immutable_key.startswith("metrics/hourly-models/comparison/objects/")
    assert immutable_key.endswith(".json")
    immutable = repository.get_json(immutable_key)
    remote = repository.get_json(repository.layout.model_comparison_pointer)
    assert immutable == remote
    assert remote["models"][0]["version"] == "safe-version"
    assert "artifact_path" not in str(remote)


def test_model_comparison_includes_mlflow_candidate_runs(
    engine, app_config, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    class FakeBridge:
        def compare_runs(self, *, target=None, limit=100):  # type: ignore[no-untyped-def]
            assert target is None
            assert limit == app_config.mlflow.maximum_runs_per_target
            return [
                {
                    "run_id": "run-1",
                    "target": "PM10",
                    "provider": "ridge",
                    "profile": "quick",
                    "selected": False,
                    "params": {"dataset_id": "dataset-1"},
                    "metrics": {"mae": 2.5},
                }
            ]

    monkeypatch.setattr(
        "smog_ai.mlops.comparison.create_mlflow_bridge",
        lambda config: FakeBridge(),
    )
    with session_scope(engine) as session:
        payload = export_model_comparison(
            session, app_config, publish=False
        )["payload"]

    assert payload["candidate_run_count"] == 1
    assert payload["candidate_runs"][0]["run_id"] == "run-1"
    assert payload["tracking_error"] is None


def test_public_model_comparison_removes_training_and_mlflow_identifiers() -> None:
    payload = build_public_model_comparison_payload(
        {
            "generated_at_utc": "2026-08-17T00:00:00+00:00",
            "tracking_uri": "sqlite:///C:/private/mlflow.db",
            "tracking_error": "private-host failed",
            "models": [
                {
                    "target": "PM10",
                    "provider": "ridge",
                    "version": "safe-version",
                    "active": True,
                    "artifact_path": r"C:\private\models\pm10.joblib",
                    "metrics": {
                        "mae": 1.5,
                        "rmse": 2.0,
                        "candidate_scores": {
                            "ridge": 1.5,
                            "persistence": 2.5,
                            "invalid": "not-a-number",
                        },
                        "activated": False,
                        "activation_policy": "quality_gated",
                        "active_model_comparison": {
                            "provider": "persistence",
                            "version": "previous-safe-version",
                            "active_model_mae": 2.0,
                            "candidate_mae": 1.5,
                            "candidate_improvement_fraction": 0.25,
                            "artifact_path": r"C:\private\previous.joblib",
                        },
                        "dataset_id": "private-dataset",
                        "dataset_sha256": "a" * 64,
                    },
                    "mlflow": {"run_id": "private-run"},
                }
            ],
            "candidate_runs": [
                {
                    "run_id": "private-run",
                    "target": "PM10",
                    "provider": "ridge",
                    "profile": "quick",
                    "selected": True,
                    "params": {
                        "dataset_id": "private-dataset",
                        "snapshot": r"C:\private\snapshot.sqlite",
                    },
                    "metrics": {"mae": 1.6, "dataset_id": "private-dataset"},
                }
            ],
        }
    )

    encoded = str(payload)
    assert payload["models"][0]["metrics"] == {
        "mae": 1.5,
        "rmse": 2.0,
        "candidate_scores": {"ridge": 1.5, "persistence": 2.5},
    }
    assert payload["candidate_runs"][0]["metrics"] == {"mae": 1.6}
    assert payload["models"][0]["selection"] == {
        "outcome": "no_change",
        "activation_policy": "quality_gated",
        "improvement_vs_previous_active": 0.25,
        "previous_active_provider": "persistence",
        "previous_active_version": "previous-safe-version",
        "previous_active_mae": 2.0,
        "candidate_mae": 1.5,
    }
    assert payload["privacy"]["model_binaries_included"] is False
    assert "private" not in encoded
    assert "artifact_path" not in encoded
    assert "dataset" not in encoded.replace("dataset_identifiers_included", "")


def test_public_model_comparison_exposes_compact_horizon_winners() -> None:
    payload = build_public_model_comparison_payload(
        {
            "generated_at_utc": "2026-08-17T00:00:00+00:00",
            "models": [],
            "candidate_runs": [
                {
                    "run_id": "private-ridge-run",
                    "target": "PM10",
                    "provider": "ridge",
                    "profile": "full",
                    "selected": True,
                    "status": "FINISHED",
                    "start_time": 20,
                    "params": {"dataset_id": "private-dataset"},
                    "metrics": {
                        "by_horizon.1.count": 100,
                        "by_horizon.1.mae": 1.2,
                        "by_horizon.1.rmse": 1.8,
                        "by_horizon.1.persistence_mae": 2.0,
                        "by_horizon.1.mae_improvement_vs_persistence": 0.4,
                        "by_horizon.1.secret_metric": 999,
                    },
                },
                {
                    "run_id": "private-persistence-run",
                    "target": "PM10",
                    "provider": "persistence",
                    "profile": "full",
                    "selected": False,
                    "status": "FINISHED",
                    "start_time": 21,
                    "metrics": {
                        "by_horizon.1.count": 100,
                        "by_horizon.1.mae": 2.0,
                        "by_horizon.1.rmse": 2.5,
                        "by_horizon.1.persistence_mae": 2.0,
                        "by_horizon.1.mae_improvement_vs_persistence": 0.0,
                    },
                },
                {
                    "run_id": "unfinished-private-run",
                    "target": "PM10",
                    "provider": "mlp",
                    "status": "RUNNING",
                    "start_time": 99,
                    "metrics": {"by_horizon.1.mae": 0.1},
                },
            ],
        }
    )

    assert payload["schema_version"] == "1.1-public"
    assert len(payload["horizon_quality"]) == 2
    assert payload["horizon_winners"] == [
        {
            "target": "PM10",
            "horizon_hours": 1,
            "provider": "ridge",
            "mae": 1.2,
            "rmse": 1.8,
            "persistence_mae": 2.0,
            "improvement_vs_persistence": 0.4,
            "candidate_count": 2,
        }
    ]
    assert payload["summary"]["horizon_count"] == 1
    encoded = str(payload)
    assert "private-ridge-run" not in encoded
    assert "private-dataset" not in encoded
    assert "secret_metric" not in encoded


def test_model_comparison_recovers_nested_training_snapshot_provenance(
    engine, app_config
) -> None:  # type: ignore[no-untyped-def]
    app_config.mlflow.comparison_path = (
        app_config.paths.data_dir.parent / "reports" / "nested-comparison.json"
    )
    with session_scope(engine) as session:
        session.add(
            ModelVersion(
                model_name="hourly-PM10-ridge",
                algorithm="ridge",
                parameter="PM10",
                forecast_horizon=0,
                semantic_version="2026.01.02-nested",
                artifact_path="nested.joblib",
                feature_columns_json=["horizon_hours"],
                metrics_json={
                    "mae": 1.5,
                    "data_provenance": {
                        "training_snapshot": {
                            "dataset_id": "training-current",
                            "database_sha256": "b" * 64,
                        }
                    },
                },
                training_data_start=datetime(2025, 1, 1, tzinfo=UTC),
                training_data_end=datetime(2025, 12, 31, tzinfo=UTC),
                active=True,
            )
        )
        session.flush()
        payload = export_model_comparison(
            session, app_config, publish=False
        )["payload"]

    model = payload["models"][0]
    assert model["metrics"]["dataset_id"] == "training-current"
    assert model["metrics"]["dataset_sha256"] == "b" * 64
