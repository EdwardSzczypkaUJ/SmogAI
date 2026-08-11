from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

import smog_ai.data_validation.contracts as contracts
from smog_ai.config import HourlyForecastingConfig
from smog_ai.hourly.features import _expand_target


def _base_for_station(
    station_id: int,
    values: list[float | None],
    *,
    start: datetime | None = None,
) -> pd.DataFrame:
    origin = start or datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
    return pd.DataFrame(
        {
            "air_station_id": [station_id] * len(values),
            "measurement_time": [origin + timedelta(hours=i) for i in range(len(values))],
            "value": values,
            "temperature_c": values,
            "latitude": [50.0 + station_id / 1000.0] * len(values),
            "longitude": [19.0 + station_id / 1000.0] * len(values),
        }
    )


def test_hourly_contract_has_numpy_namespace_for_exact_horizon_check() -> None:
    assert contracts.np is np


def test_expand_target_drops_resampled_gap_as_training_origin() -> None:
    frame = _expand_target(
        _base_for_station(1, [10.0, None, 20.0, 30.0]),
        target_column="value",
        horizons=[1],
        allow_negative_target=False,
    )

    assert len(frame) == 1
    row = frame.iloc[0]
    assert float(row["value"]) == 20.0
    assert float(row["target"]) == 30.0
    assert frame[["value", "target", "latitude", "longitude"]].notna().all().all()


def test_expand_target_keeps_negative_temperature_and_exact_clock_time() -> None:
    frame = _expand_target(
        _base_for_station(1, [-5.0, None, -3.0, -2.0]),
        target_column="temperature_c",
        horizons=[1],
        allow_negative_target=True,
    )

    assert len(frame) == 1
    row = frame.iloc[0]
    assert float(row["temperature_c"]) == -3.0
    assert float(row["target"]) == -2.0
    delta = (
        pd.to_datetime(frame["target_time"], utc=True)
        - pd.to_datetime(frame["measurement_time"], utc=True)
    ).dt.total_seconds() / 3600.0
    assert np.allclose(delta, frame["horizon_hours"].astype(float))


def test_expanded_training_frame_is_capped_and_balanced_by_horizon_and_station() -> None:
    base = pd.concat(
        [
            _base_for_station(station_id, [float(i) for i in range(80)])
            for station_id in range(1, 5)
        ],
        ignore_index=True,
    )
    frame = _expand_target(
        base,
        target_column="value",
        horizons=[1, 2, 3, 4],
        allow_negative_target=False,
        maximum_rows=80,
    )

    assert 0 < len(frame) <= 80
    assert set(frame["horizon_hours"].unique()) == {1, 2, 3, 4}
    for _, group in frame.groupby("horizon_hours"):
        assert set(group["air_station_id"].unique()) == {1, 2, 3, 4}
    assert frame[["value", "target", "current_value"]].notna().all().all() if "current_value" in frame else frame[["value", "target"]].notna().all().all()


def test_hourly_default_limits_expansion_to_safe_size() -> None:
    settings = HourlyForecastingConfig()
    assert settings.maximum_training_rows_per_target == 1_000_000


def test_sanitized_hourly_frame_passes_pandera_when_available(app_config) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from smog_ai.data_validation.contracts import PanderaFrameValidator, validate_frame

    if not PanderaFrameValidator.available():
        pytest.skip("Pandera is not installed in this build environment")

    frame = _expand_target(
        _base_for_station(1, [10.0, None, 20.0, 30.0, 40.0]),
        target_column="value",
        horizons=[1, 2],
        allow_negative_target=False,
    )
    frame["current_value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["target_name"] = "PM10"
    app_config.data_validation.require_pandera = True
    validated, result = validate_frame(
        frame,
        "hourly_training_frame",
        app_config,
        context={"target": "PM10", "test": "gap-sanitization"},
    )
    assert not validated.empty
    assert result.valid is True
    assert result.failure_count == 0
