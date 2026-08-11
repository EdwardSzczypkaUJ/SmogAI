from __future__ import annotations

import pandas as pd

from smog_ai.data_validation.contracts import (
    PanderaFrameValidator,
    _hourly_horizon_matches,
)
from smog_ai.config import DataValidationConfig


def _valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "air_station_id": [1, 1, 1, 1],
            "measurement_time": pd.to_datetime(
                [
                    "2026-08-03T00:00:00Z",
                    "2026-08-03T02:00:00Z",
                    "2026-08-03T02:00:00Z",
                    "2026-08-03T03:00:00Z",
                ],
                utc=True,
            ),
            "target_time": pd.to_datetime(
                [
                    "2026-08-03T02:00:00Z",
                    "2026-08-03T03:00:00Z",
                    "2026-08-03T04:00:00Z",
                    "2026-08-03T04:00:00Z",
                ],
                utc=True,
            ),
            "horizon_hours": [2, 1, 2, 1],
            "current_value": [10.0, 20.0, 20.0, 30.0],
            "target": [20.0, 30.0, 40.0, 40.0],
            "latitude": [50.001] * 4,
            "longitude": [19.001] * 4,
            "target_name": ["PM10"] * 4,
        }
    )


def test_exact_horizon_check_returns_index_aligned_boolean_series() -> None:
    frame = _valid_frame()
    result = _hourly_horizon_matches(frame)
    assert isinstance(result, pd.Series)
    assert result.dtype == bool
    assert result.index.equals(frame.index)
    assert result.all()


def test_exact_horizon_check_marks_only_bad_row() -> None:
    frame = _valid_frame()
    frame.loc[2, "horizon_hours"] = 3
    result = _hourly_horizon_matches(frame)
    assert result.tolist() == [True, True, False, True]


def test_valid_hourly_frame_passes_installed_pandera() -> None:
    if not PanderaFrameValidator.available():
        return
    _, result = PanderaFrameValidator(DataValidationConfig()).validate(
        _valid_frame(),
        "hourly_training_frame",
        policy="fail",
        context={"target": "PM10", "test": "pandera-series-output"},
    )
    assert result.valid, result.failure_cases
