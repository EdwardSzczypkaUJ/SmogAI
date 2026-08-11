from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import Forecast, ModelVersion
from smog_ai.hourly.features import (
    _expand_target,
    build_hourly_pm_training_frame,
    build_hourly_weather_training_frame,
)
from smog_ai.hourly.predictor import create_hourly_forecasts
from smog_ai.hourly.trainer import HOURLY_MODEL_HORIZON_SENTINEL, train_hourly_models
from smog_ai.modeling import ModelFitContext, ModelPredictContext, PredictionBundle
from smog_ai.modeling.registry import create_model_registry
from smog_ai.progress import ProgressReporter, read_progress
from tests.conftest import seed_basic


def test_builtin_model_registry_is_open_and_describable() -> None:
    registry = create_model_registry(load_entry_points=False)
    assert {
        "persistence",
        "historical_mean",
        "ridge",
        "polynomial_ridge",
        "hist_gradient_boosting",
        "hist_gradient_boosting_quantile",
        "mlp",
        "hurdle_hist_gradient_boosting",
    }.issubset(set(registry.names()))
    descriptions = {row["name"]: row for row in registry.describe()}
    assert descriptions["polynomial_ridge"]["task"] == "regression"


def test_external_model_module_can_register_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    module = types.ModuleType("tests.fake_model_plugin")

    class ConstantProvider:
        name = "external_constant"
        task = "regression"

        def fit(self, X, y, *, context):  # type: ignore[no-untyped-def]
            del X, context
            return {"mean": float(pd.Series(y).mean())}

        def predict(self, artifact, X, *, context):  # type: ignore[no-untyped-def]
            del context
            return PredictionBundle(np.full(len(X), artifact["mean"]))

        def describe(self, artifact):  # type: ignore[no-untyped-def]
            return {"provider": self.name, "mean": artifact.get("mean")}

    def register_models(registry):  # type: ignore[no-untyped-def]
        registry.register(ConstantProvider())

    module.register_models = register_models  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    registry = create_model_registry(
        plugin_modules=[module.__name__],
        load_entry_points=False,
    )
    provider = registry.get("external_constant")
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0]})
    fit_context = ModelFitContext(
        target_name="temperature_c",
        feature_columns=("x",),
        task="regression",
    )
    artifact = provider.fit(frame, pd.Series([2.0, 4.0, 6.0]), context=fit_context)
    predicted = provider.predict(
        artifact,
        frame,
        context=ModelPredictContext(
            target_name="temperature_c",
            feature_columns=("x",),
            task="regression",
        ),
    )
    assert predicted.values.tolist() == [4.0, 4.0, 4.0]


def test_polynomial_provider_expands_horizon_basis_only() -> None:
    registry = create_model_registry(load_entry_points=False)
    provider = registry.get("polynomial_ridge")
    horizons = np.arange(1, 18, dtype=float)
    frame = pd.DataFrame(
        {
            "horizon_hours": horizons,
            "horizon_squared": horizons**2,
            "target_hour_sin": np.sin(horizons),
            "target_hour_cos": np.cos(horizons),
            "current_value": np.full_like(horizons, 5.0),
        }
    )
    y = pd.Series(2.0 + 0.7 * horizons + 0.15 * horizons**2)
    columns = tuple(frame.columns)
    artifact = provider.fit(
        frame,
        y,
        context=ModelFitContext(
            target_name="temperature_c",
            feature_columns=columns,
            task="regression",
            metadata={"method_parameters": {"degree": 2, "alpha": 1e-8}},
        ),
    )
    values = provider.predict(
        artifact,
        frame,
        context=ModelPredictContext(
            target_name="temperature_c",
            feature_columns=columns,
            task="regression",
        ),
    ).values
    assert float(np.mean(np.abs(values - y.to_numpy()))) < 0.05


def test_hurdle_precipitation_provider_returns_probability_and_amount() -> None:
    registry = create_model_registry(load_entry_points=False)
    provider = registry.get("hurdle_hist_gradient_boosting")
    rng = np.random.default_rng(42)
    frame = pd.DataFrame(
        {
            "horizon_hours": np.tile(np.arange(1, 9), 25),
            "humidity_percent": rng.uniform(35, 100, 200),
            "pressure_hpa": rng.normal(1012, 8, 200),
        }
    )
    occurrence = frame["humidity_percent"] > 70
    y = pd.Series(
        np.where(
            occurrence,
            np.maximum(0.1, (frame["humidity_percent"] - 68) / 12 + rng.normal(0, 0.2, 200)),
            0.0,
        )
    )
    columns = tuple(frame.columns)
    artifact = provider.fit(
        frame,
        y,
        context=ModelFitContext(
            target_name="precipitation_mm",
            feature_columns=columns,
            task="hurdle_regression",
            metadata={"occurrence_threshold_mm": 0.1, "minimum_positive_rows": 10},
        ),
    )
    bundle = provider.predict(
        artifact,
        frame.iloc[:20],
        context=ModelPredictContext(
            target_name="precipitation_mm",
            feature_columns=columns,
            task="hurdle_regression",
        ),
    )
    assert len(bundle.values) == 20
    assert set(bundle.extras) == {
        "precipitation_probability",
        "precipitation_amount_if_rain_mm",
    }
    assert np.all((bundle.extras["precipitation_probability"] >= 0) & (bundle.extras["precipitation_probability"] <= 1))
    assert np.all(bundle.values >= 0)



def test_hourly_target_expansion_drops_missing_origin_observations() -> None:
    times = pd.date_range("2026-08-01T00:00:00Z", periods=4, freq="1h")
    base = pd.DataFrame(
        {
            "air_station_id": [1, 1, 1, 1],
            "measurement_time": times,
            "value": [10.0, np.nan, 12.0, 13.0],
            "latitude": [50.0] * 4,
            "longitude": [19.0] * 4,
        }
    )

    frame = _expand_target(
        base,
        target_column="value",
        horizons=[1],
        allow_negative_target=False,
    )

    assert not frame.empty
    assert frame["value"].notna().all()
    assert frame["target"].notna().all()
    assert set(frame["measurement_time"]) == {
        pd.Timestamp("2026-08-01T02:00:00Z"),
    }

def test_hourly_feature_frames_use_exact_target_times(engine) -> None:  # type: ignore[no-untyped-def]
    seed_basic(engine, hours=84)
    with session_scope(engine) as session:
        temperature = build_hourly_weather_training_frame(
            session,
            target="temperature_c",
            horizons=[1, 2, 4],
            max_days=30,
        )
        pm10 = build_hourly_pm_training_frame(
            session,
            parameter="PM10",
            horizons=[1, 2, 4],
            max_days=30,
        )
    assert not temperature.empty
    assert not pm10.empty
    for frame in (temperature, pm10):
        difference = (
            pd.to_datetime(frame["target_time"], utc=True)
            - pd.to_datetime(frame["measurement_time"], utc=True)
        ).dt.total_seconds() / 3600
        assert np.array_equal(difference.to_numpy(dtype=int), frame["horizon_hours"].to_numpy(dtype=int))
        assert set(frame["horizon_hours"].unique()) == {1, 2, 4}


def test_hourly_training_and_prediction_are_exact_and_idempotent(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    seed_basic(engine, hours=96)
    settings = app_config.hourly_forecasting
    settings.enabled = True
    settings.minimum_horizon_hours = 1
    settings.maximum_horizon_hours = 4
    settings.step_hours = 1
    settings.targets = ["PM10", "temperature_c", "precipitation_mm"]
    settings.spatial_targets = [
        "PM10",
        "temperature_c",
        "precipitation_probability",
        "precipitation_mm",
    ]
    settings.target_algorithms = {
        "PM10": ["persistence", "ridge"],
        "PM2.5": ["persistence"],
        "temperature_c": ["persistence", "ridge"],
        "precipitation_mm": ["hurdle_hist_gradient_boosting"],
    }
    settings.minimum_training_rows = 20
    settings.minimum_unique_origin_times = 20
    settings.validation_fraction = 0.2
    settings.cross_fit_folds = 3
    settings.quantiles = [0.5]
    settings.quantile_method = "hist_gradient_boosting_quantile"
    settings.precipitation.minimum_positive_rows = 5
    app_config.training.input_source = "database"
    app_config.training.allow_database_fallback = True
    app_config.artifacts.upload_models = False

    progress = ProgressReporter(
        app_config.paths.logs_dir,
        run_type="hourly-integration-test",
        stage_weights={"training": 0.8, "prediction": 0.2},
        stage_default_seconds={"training": 30.0, "prediction": 10.0},
        heartbeat_seconds=60.0,
    ).start()

    with session_scope(engine) as session:
        trained = train_hourly_models(session, app_config, progress=progress)
        assert trained.errors == 0
        active = session.scalars(
            select(ModelVersion).where(
                ModelVersion.active.is_(True),
                ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
            )
        ).all()
        assert {row.parameter for row in active} == {
            "PM10",
            "temperature_c",
            "precipitation_mm",
        }

    with session_scope(engine) as session:
        first = create_hourly_forecasts(session, app_config, progress=progress)
        second = create_hourly_forecasts(session, app_config, progress=progress)
        assert first.errors == 0
        assert first.inserted > 0
        assert second.inserted == 0
        rows = session.scalars(select(Forecast)).all()
        assert rows
        assert {row.forecast_horizon for row in rows} == {1, 2, 3, 4}
        assert {
            row.parameter for row in rows
        }.issuperset({"PM10", "temperature_c", "precipitation_mm", "precipitation_probability"})
        assert all(
            int((row.target_time - row.forecast_origin_time).total_seconds() / 3600)
            == row.forecast_horizon
            for row in rows
        )
        assert session.scalar(select(func.count()).select_from(Forecast)) == len(rows)
    progress.finish("success")
    progress_payload = read_progress(
        app_config.paths.logs_dir,
        "hourly-integration-test",
    )
    assert progress_payload is not None
    assert progress_payload["status"] == "success"
    assert progress_payload["overall_percent"] == 100.0
