from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
from sqlalchemy import select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import Forecast, ForecastResult, ModelVersion
from smog_ai.hourly.drift import hourly_drift_status
from smog_ai.hourly.incremental import (
    apply_residual_correction,
    residual_feature_matrix,
    update_hourly_residual_correctors,
)
from tests.conftest import seed_basic


def _active_persistence_model(engine, app_config, *, target: str = "PM10") -> str:  # type: ignore[no-untyped-def]
    artifact = {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "provider": "persistence",
        "task": "regression",
        "feature_columns": ["value", "horizon_hours"],
        "horizons_hours": list(range(1, 49)),
        "provider_artifact": {
            "provider": "persistence",
            "baseline_column": "value",
            "feature_columns": ["value", "horizon_hours"],
        },
        "metadata": {},
    }
    path = app_config.paths.models_dir / "test-persistence.joblib"
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    with session_scope(engine) as session:
        row = ModelVersion(
            model_name="hourly-PM10-persistence",
            algorithm="persistence",
            parameter=target,
            forecast_horizon=0,
            semantic_version="test-base",
            artifact_path=str(path),
            feature_columns_json=["value", "horizon_hours"],
            metrics_json={},
            active=True,
            activated_at=datetime.now(UTC),
        )
        session.add(row)
        session.flush()
        return row.id


def _seed_verified_forecasts(
    engine,
    *,
    model_id: str,
    station_id: int,
    rows: int = 120,
    error_offset: float = 4.0,
) -> None:  # type: ignore[no-untyped-def]
    start = datetime.now(UTC) - timedelta(days=10)
    with session_scope(engine) as session:
        for index in range(rows):
            target = start + timedelta(hours=index)
            base = 20.0 + np.sin(index / 8.0)
            actual = base + error_offset + 0.25 * np.sin(index / 3.0)
            forecast = Forecast(
                model_version_id=model_id,
                air_station_id=station_id,
                parameter="PM10",
                forecast_created_at=target - timedelta(hours=6),
                forecast_origin_time=target - timedelta(hours=6),
                target_time=target,
                forecast_horizon=6,
                predicted_value=float(base),
                features_json={"base_predicted_value": float(base)},
            )
            session.add(forecast)
            session.flush()
            signed = float(base - actual)
            session.add(
                ForecastResult(
                    forecast_id=forecast.id,
                    actual_value=float(actual),
                    signed_error=signed,
                    absolute_error=abs(signed),
                    squared_error=signed * signed,
                    verified_at=target + timedelta(hours=1),
                    verification_status="verified",
                    matched_measurement_time=target,
                )
            )


def test_residual_feature_matrix_and_application() -> None:
    times = [datetime(2026, 1, 1, 6, tzinfo=UTC)] * 3
    frame = residual_feature_matrix([10, 11, 12], [1, 6, 24], times)
    assert frame.shape == (3, 6)
    assert frame.notna().all().all()
    values, correction = apply_residual_correction({}, frame, np.array([1.0, 2.0, 3.0]))
    assert np.array_equal(values, np.array([1.0, 2.0, 3.0]))
    assert np.array_equal(correction, np.zeros(3))


def test_incremental_residual_update_activates_improving_corrector(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    ids = seed_basic(engine, hours=24)
    model_id = _active_persistence_model(engine, app_config)
    _seed_verified_forecasts(
        engine,
        model_id=model_id,
        station_id=ids["air_station_id"],
        rows=140,
    )
    settings = app_config.hourly_forecasting.incremental_residual
    settings.minimum_verified_rows = 50
    settings.maximum_rows_per_update = 200
    settings.minimum_mae_improvement_fraction = 0.001
    app_config.artifacts.upload_models = False
    app_config.object_storage.enabled = False

    with session_scope(engine) as session:
        stats = update_hourly_residual_correctors(session, app_config)
        assert stats.errors == 0
        assert stats.inserted == 1
        active = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == "PM10",
                ModelVersion.forecast_horizon == 0,
                ModelVersion.active.is_(True),
            )
        )
        assert active is not None
        assert active.semantic_version != "test-base"
        artifact = joblib.load(Path(active.artifact_path))
        assert artifact["residual_corrector"]["active"] is True
        assert artifact["residual_corrector"]["improvement_fraction"] > 0


def test_drift_status_detects_recent_mae_jump(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    ids = seed_basic(engine, hours=24)
    model_id = _active_persistence_model(engine, app_config)
    now = datetime.now(UTC)
    with session_scope(engine) as session:
        # Query orders newest first.  Older/reference errors are small; recent
        # errors are deliberately large.
        for index in range(120):
            target = now - timedelta(hours=120 - index)
            error = 1.0 if index < 80 else 8.0
            forecast = Forecast(
                model_version_id=model_id,
                air_station_id=ids["air_station_id"],
                parameter="PM10",
                forecast_created_at=target - timedelta(hours=1),
                forecast_origin_time=target - timedelta(hours=1),
                target_time=target,
                forecast_horizon=1,
                predicted_value=20.0,
                features_json={},
            )
            session.add(forecast)
            session.flush()
            session.add(
                ForecastResult(
                    forecast_id=forecast.id,
                    actual_value=20.0 - error,
                    signed_error=error,
                    absolute_error=abs(error),
                    squared_error=error * error,
                    verified_at=target + timedelta(minutes=5),
                    verification_status="verified",
                    matched_measurement_time=target,
                )
            )

    settings = app_config.hourly_forecasting.drift
    settings.minimum_verified_rows = 60
    settings.recent_window_rows = 40
    settings.reference_window_rows = 80
    settings.mae_relative_increase_threshold = 0.2
    settings.bias_absolute_thresholds["PM10"] = 5.0
    app_config.hourly_forecasting.targets = ["PM10"]

    with session_scope(engine) as session:
        payload = hourly_drift_status(session, app_config)
    assert payload["retrain_recommended"] is True
    assert payload["targets"]["PM10"]["drift"] is True
    assert payload["targets"]["PM10"]["recent_mae"] > payload["targets"]["PM10"]["reference_mae"]
