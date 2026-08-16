from __future__ import annotations

from datetime import UTC, datetime

from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.mlops.comparison import export_model_comparison
from smog_ai.mlops.mlflow_bridge import create_mlflow_bridge


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
