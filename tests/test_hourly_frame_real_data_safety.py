from __future__ import annotations

import numpy as np
import pandas as pd

from smog_ai.config import HourlyForecastingConfig
from smog_ai.features.builder import WEATHER_COLUMNS
from smog_ai.hourly.features import (
    _bundle_weather_training_frame,
    _expand_target,
)


def _base_series(*, rows: int = 12, target_column: str = "value") -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "air_station_id": np.ones(rows, dtype=int),
            "measurement_time": pd.date_range(
                "2026-01-01T00:00:00Z", periods=rows, freq="h"
            ),
            target_column: np.arange(rows, dtype=float),
            "latitude": np.full(rows, 50.0),
            "longitude": np.full(rows, 19.0),
        }
    )
    return frame


def test_exact_target_expansion_excludes_missing_current_pm_origins() -> None:
    frame = _base_series(rows=8)
    frame.loc[2, "value"] = np.nan
    expanded = _expand_target(
        frame,
        target_column="value",
        horizons=[1, 2],
        allow_negative_target=False,
        require_current_value=True,
    )
    assert not expanded.empty
    assert expanded["value"].notna().all()
    assert not (
        pd.to_datetime(expanded["measurement_time"], utc=True)
        == pd.Timestamp("2026-01-01T02:00:00Z")
    ).any()
    delta = (
        pd.to_datetime(expanded["target_time"], utc=True)
        - pd.to_datetime(expanded["measurement_time"], utc=True)
    ).dt.total_seconds() / 3600
    assert np.array_equal(
        delta.to_numpy(dtype=int), expanded["horizon_hours"].to_numpy(dtype=int)
    )


def test_sparse_accumulated_precipitation_may_have_missing_current_value() -> None:
    frame = _base_series(rows=8, target_column="precipitation_mm")
    frame["precipitation_mm"] = np.nan
    frame.loc[2, "precipitation_mm"] = 1.5
    frame.loc[6, "precipitation_mm"] = 0.0
    expanded = _expand_target(
        frame,
        target_column="precipitation_mm",
        horizons=[1, 2, 3, 4],
        allow_negative_target=False,
        require_current_value=False,
    )
    assert not expanded.empty
    # An origin one hour before the reported six-hour accumulation is retained.
    row = expanded[
        (pd.to_datetime(expanded["measurement_time"], utc=True)
         == pd.Timestamp("2026-01-01T01:00:00Z"))
        & (expanded["horizon_hours"] == 1)
    ]
    assert len(row) == 1
    assert pd.isna(row.iloc[0]["precipitation_mm"])
    assert float(row.iloc[0]["target"]) == 1.5


def test_weather_target_bundle_uses_unique_imgw_station_series() -> None:
    bundle = {
        "weather_stations": [
            {
                "id": 10,
                "source_id": "12566",
                "latitude": 50.08,
                "longitude": 19.80,
            }
        ],
        "weather_measurements": [
            {
                "weather_station_id": 10,
                "source_station_id": "12566",
                "measurement_time": "2026-01-01T00:00:00Z",
                "temperature_c": 4.0,
                "humidity_percent": 80.0,
                "pressure_hpa": 1005.0,
                "precipitation_mm": None,
                "precipitation_accumulation_period_hours": None,
                "wind_speed_mps": 2.0,
                "wind_direction_deg": 180.0,
                "is_valid": True,
            },
            {
                "weather_station_id": 10,
                "source_station_id": "12566",
                "measurement_time": "2026-01-01T01:00:00Z",
                "temperature_c": 4.2,
                "humidity_percent": 79.0,
                "pressure_hpa": 1005.2,
                "precipitation_mm": 0.0,
                "precipitation_accumulation_period_hours": 6,
                "wind_speed_mps": 2.1,
                "wind_direction_deg": 185.0,
                "is_valid": True,
            },
        ],
        # Multiple GIOŚ stations may point to the same IMGW station.  They must
        # not duplicate weather-target training observations.
        "station_matches": [
            {"air_station_id": 1, "weather_station_id": 10},
            {"air_station_id": 2, "weather_station_id": 10},
            {"air_station_id": 3, "weather_station_id": 10},
        ],
    }
    frame = _bundle_weather_training_frame(bundle, max_days=30)
    assert len(frame) == 2
    assert frame["air_station_id"].nunique() == 1
    assert int(frame["air_station_id"].iloc[0]) == 10
    assert float(frame["latitude"].iloc[0]) == 50.08


def test_origin_sampling_bounds_long_horizon_frame() -> None:
    frame = _base_series(rows=500)
    # Add optional weather columns so the frame resembles a PM feature frame.
    for column in WEATHER_COLUMNS:
        frame[column] = np.nan
    maximum = 480
    expanded = _expand_target(
        frame,
        target_column="value",
        horizons=range(1, 49),
        allow_negative_target=False,
        require_current_value=True,
        maximum_output_rows=maximum,
    )
    assert not expanded.empty
    assert len(expanded) <= maximum
    assert expanded["horizon_hours"].between(1, 48).all()


def test_hourly_config_has_safe_default_training_row_cap() -> None:
    settings = HourlyForecastingConfig()
    assert settings.maximum_training_rows_per_target == 1_000_000


def test_pandera_contract_accepts_sparse_precipitation_origin_when_available() -> None:
    from smog_ai.config import DataValidationConfig
    from smog_ai.data_validation.contracts import PanderaFrameValidator

    if not PanderaFrameValidator.available():
        return
    frame = pd.DataFrame(
        {
            "air_station_id": [1],
            "measurement_time": [pd.Timestamp("2026-01-01T01:00:00Z")],
            "target_time": [pd.Timestamp("2026-01-01T06:00:00Z")],
            "horizon_hours": [5],
            "current_value": [np.nan],
            "target": [1.2],
            "latitude": [50.0],
            "longitude": [19.0],
            "target_occurrence": [1.0],
            "target_name": ["precipitation_mm"],
        }
    )
    _, result = PanderaFrameValidator(DataValidationConfig()).validate(
        frame,
        "hourly_training_frame",
        policy="fail",
    )
    assert result.valid, result.failure_cases
