from __future__ import annotations

import numpy as np
import pandas as pd

from smog_ai.hourly.features import _expand_target
from smog_ai.hourly.training_policy import (
    TrainingBudget,
    create_training_set_policy,
    resolve_training_profile,
)


def _training_frame(rows: int = 10_000) -> pd.DataFrame:
    times = pd.date_range("2024-01-01T00:00:00Z", periods=rows, freq="h")
    return pd.DataFrame(
        {
            "air_station_id": (np.arange(rows) % 20) + 1,
            "measurement_time": times,
            "target_time": times + pd.to_timedelta((np.arange(rows) % 48) + 1, unit="h"),
            "horizon_hours": (np.arange(rows) % 48) + 1,
            "target": 10.0 + (np.arange(rows) % 100) * 0.5,
            "value": 9.0 + (np.arange(rows) % 80) * 0.4,
            "latitude": 49.0 + (np.arange(rows) % 20) * 0.05,
            "longitude": 18.0 + (np.arange(rows) % 20) * 0.05,
        }
    )


def test_quick_policy_is_bounded_deterministic_and_weighted(app_config) -> None:  # type: ignore[no-untyped-def]
    profile = resolve_training_profile(app_config, "quick")
    policy = create_training_set_policy(app_config)
    frame = _training_frame()

    first = policy.select(
        frame,
        target="PM2.5",
        phase="final",
        maximum_rows=2_000,
        profile=profile,
        random_state=42,
    )
    second = policy.select(
        frame,
        target="PM2.5",
        phase="final",
        maximum_rows=2_000,
        profile=profile,
        random_state=42,
    )

    assert len(first.frame) == 2_000
    assert first.frame[["air_station_id", "measurement_time", "horizon_hours"]].equals(
        second.frame[["air_station_id", "measurement_time", "horizon_hours"]]
    )
    assert "__sample_weight" in first.frame.columns
    assert first.frame["__sample_weight"].between(0.1, 10.0).all()
    assert first.frame["horizon_hours"].nunique() > 8
    assert first.metadata["policy"] == "bounded_rolling_stratified"


def test_quick_profile_is_materially_smaller_than_full(app_config) -> None:  # type: ignore[no-untyped-def]
    quick = resolve_training_profile(app_config, "quick")
    full = resolve_training_profile(app_config, "full")
    assert quick.maximum_rows_per_target < full.maximum_rows_per_target
    assert quick.cross_fit_folds < full.cross_fit_folds
    assert quick.fit_quantiles is False
    assert full.fit_quantiles is True
    assert quick.horizons_per_origin < full.horizons_per_origin
    assert quick.max_wall_time_seconds < full.max_wall_time_seconds


def test_horizon_subsampling_limits_each_origin_but_covers_all_horizons() -> None:
    rows = 800
    base = pd.DataFrame(
        {
            "air_station_id": np.ones(rows, dtype=int),
            "measurement_time": pd.date_range(
                "2024-01-01T00:00:00Z", periods=rows, freq="h"
            ),
            "value": np.linspace(5.0, 55.0, rows),
            "latitude": np.full(rows, 50.0),
            "longitude": np.full(rows, 19.0),
        }
    )
    expanded = _expand_target(
        base,
        target_column="value",
        horizons=range(1, 49),
        allow_negative_target=False,
        horizon_bucket_edges=[6, 12, 24, 48],
        samples_per_horizon_bucket=2,
        random_state=42,
    )

    per_origin = expanded.groupby("measurement_time")["horizon_hours"].nunique()
    assert int(per_origin.max()) <= 8
    assert set(expanded["horizon_hours"].unique()) == set(range(1, 49))
    delta = (
        pd.to_datetime(expanded["target_time"], utc=True)
        - pd.to_datetime(expanded["measurement_time"], utc=True)
    ).dt.total_seconds() / 3600.0
    assert np.allclose(delta, expanded["horizon_hours"])


def test_training_budget_allows_one_candidate_then_stops(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    budget = TrainingBudget(max_seconds=1.0)
    monkeypatch.setattr(
        type(budget),
        "elapsed_seconds",
        property(lambda self: 2.0),
    )
    assert budget.should_continue(completed_candidates=0) is True
    assert budget.should_continue(completed_candidates=1) is False
    assert budget.stopped_reason == "max_wall_time_exceeded"


def test_explicit_quick_profile_trains_with_bounded_algorithms(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from smog_ai.database.engine import session_scope
    from smog_ai.database.models import ModelVersion
    from smog_ai.hourly.trainer import train_hourly_models
    from tests.conftest import seed_basic

    seed_basic(engine, hours=96)
    settings = app_config.hourly_forecasting
    settings.enabled = True
    settings.minimum_horizon_hours = 1
    settings.maximum_horizon_hours = 4
    settings.step_hours = 1
    settings.targets = ["PM10", "temperature_c"]
    settings.minimum_training_rows = 20
    settings.minimum_unique_origin_times = 20
    settings.training_policy.quick.maximum_rows_per_target = 500
    settings.training_policy.quick.validation_max_rows = 100
    settings.training_policy.quick.maximum_training_days_by_target = {
        "PM10": 30,
        "temperature_c": 30,
    }
    settings.training_policy.quick.horizon_bucket_edges = [2, 4]
    settings.training_policy.quick.samples_per_horizon_bucket = 1
    settings.training_policy.quick.cross_fit_folds = 2
    settings.training_policy.quick.algorithms = {
        "PM10": ["persistence", "ridge"],
        "temperature_c": ["persistence", "ridge"],
    }
    settings.training_policy.quick.fit_quantiles = False
    settings.training_policy.quick.max_wall_time_seconds = 300
    app_config.training.input_source = "database"
    app_config.artifacts.upload_models = False
    app_config.object_storage.enabled = False

    with session_scope(engine) as session:
        stats = train_hourly_models(
            session,
            app_config,
            profile_name="quick",
        )
        assert stats.errors == 0
        assert stats.details["training_profile"] == "quick"
        active = session.scalars(
            select(ModelVersion).where(
                ModelVersion.active.is_(True),
                ModelVersion.forecast_horizon == 0,
            )
        ).all()
        assert {row.parameter for row in active} == {"PM10", "temperature_c"}
        assert all(
            (row.metrics_json or {}).get("training_profile") == "quick"
            for row in active
        )
