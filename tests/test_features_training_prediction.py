from __future__ import annotations

from datetime import timedelta

import numpy as np
from sqlalchemy import func, select

from smog_ai.database.engine import session_scope
from smog_ai.data_validation.contracts import validate_frame
from smog_ai.database.models import AirMeasurement, Forecast, ForecastResult, ModelVersion
from smog_ai.features.builder import FEATURE_COLUMNS, build_latest_feature_rows, build_training_frame
from smog_ai.prediction.predictor import create_forecasts
from smog_ai.prediction.verifier import verify_forecasts
from smog_ai.training.metrics import regression_metrics
from smog_ai.training.trainer import ensure_baseline_models, train_models
from tests.conftest import seed_basic


def test_feature_frame_is_chronological(engine) -> None:
    seed_basic(engine, hours=40)
    with session_scope(engine) as session:
        frame = build_training_frame(session, parameter="PM10", horizon_hours=6)
        assert len(frame) > 0
        assert frame["measurement_time"].is_monotonic_increasing
        assert set(FEATURE_COLUMNS).issubset(frame.columns)


def test_latest_features_one_row_per_station(engine) -> None:
    seed_basic(engine, hours=30)
    with session_scope(engine) as session:
        frame = build_latest_feature_rows(session, parameter="PM10")
        assert len(frame) == 1
        assert frame.iloc[0]["value"] > 0


def test_training_frame_drops_hourly_gap_rows_before_pandera(engine, app_config) -> None:
    """A real GIOŚ series can have holes after hourly resampling.

    The gap row is needed while calculating lags, but it cannot be emitted as a
    supervised observation because the contract requires a current value.  The
    first real nationwide collection exposed this exact boundary condition.
    """

    seed_basic(engine, hours=48)
    with session_scope(engine) as session:
        rows = session.scalars(
            select(AirMeasurement).order_by(AirMeasurement.measurement_time)
        ).all()
        session.delete(rows[18])

    with session_scope(engine) as session:
        frame = build_training_frame(session, parameter="PM10", horizon_hours=6)
        assert not frame.empty
        assert frame["value"].notna().all()
        assert frame["target"].notna().all()
        assert frame["pm_lag_1"].notna().all()
        validated, result = validate_frame(frame, "training_frame", app_config)
        assert result.valid is True
        assert len(validated) == len(frame)


def test_regression_metrics() -> None:
    metrics = regression_metrics(np.array([10, 20]), np.array([12, 18]), persistence=np.array([15, 15]))
    assert metrics["mae"] == 2
    assert metrics["persistence_mae"] == 5


def test_bootstrap_models_are_idempotent(engine, app_config) -> None:
    with session_scope(engine) as session:
        assert ensure_baseline_models(session, app_config) == 1
        assert ensure_baseline_models(session, app_config) == 0
        assert session.scalar(select(func.count()).select_from(ModelVersion)) == 1


def test_training_creates_active_model(engine, app_config) -> None:
    seed_basic(engine, hours=80)
    with session_scope(engine) as session:
        stats = train_models(session, app_config)
        assert stats.inserted >= 1
        assert session.scalar(select(ModelVersion).where(ModelVersion.active.is_(True))) is not None


def test_prediction_is_idempotent(engine, app_config) -> None:
    seed_basic(engine, hours=30)
    with session_scope(engine) as session:
        first = create_forecasts(session, app_config)
        second = create_forecasts(session, app_config)
        assert first.inserted == 1
        assert second.inserted == 0


def test_prediction_skips_measurements_older_than_freshness_limit(engine, app_config) -> None:
    seed_basic(engine, hours=30)
    app_config.quality.stale_air_hours = 1
    with session_scope(engine) as session:
        for measurement in session.scalars(select(AirMeasurement)).all():
            measurement.measurement_time -= timedelta(days=2)

    with session_scope(engine) as session:
        stats = create_forecasts(session, app_config)
        assert stats.inserted == 0
        assert stats.details["stale_forecasts_skipped"] > 0
        assert session.scalar(select(func.count()).select_from(Forecast)) == 0


def test_verification_matches_actual_measurement(engine, app_config) -> None:
    seed_basic(engine, hours=36)
    with session_scope(engine) as session:
        ensure_baseline_models(session, app_config)
        create_forecasts(session, app_config)
        forecast = session.scalar(select(Forecast))
        # Move an existing measurement to the target so the delayed verifier can resolve it.
        measurement = session.scalar(select(AirMeasurement).order_by(AirMeasurement.measurement_time.desc()))
        forecast.target_time = measurement.measurement_time
        stats = verify_forecasts(session, app_config)
        result = session.scalar(select(ForecastResult).where(ForecastResult.forecast_id == forecast.id))
        assert stats.inserted == 1
        assert result.verification_status == "verified"
        assert result.absolute_error is not None
