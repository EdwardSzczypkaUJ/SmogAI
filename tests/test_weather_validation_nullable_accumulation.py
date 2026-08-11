from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from smog_ai.data_validation.contracts import PanderaFrameValidator, validate_frame


def test_weather_contract_accepts_sparse_nullable_accumulation_period(app_config) -> None:  # type: ignore[no-untyped-def]
    if not PanderaFrameValidator.available():
        pytest.skip("Pandera is not installed in this build environment")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(1000):
        rows.append(
            {
                "source": "IMGW",
                "source_station_id": str(index % 62),
                "measurement_time": start + timedelta(hours=index),
                "temperature_c": -15.0 + (index % 50),
                "humidity_percent": float(index % 101),
                "pressure_hpa": 750.0 if index % 17 == 0 else 1012.0,
                "precipitation_mm": 2.4 if index % 6 == 0 else None,
                "precipitation_accumulation_period_hours": 6 if index % 6 == 0 else None,
                "wind_speed_mps": 2.0,
                "wind_direction_deg": 180.0,
                "is_valid": True,
                "collected_at": start + timedelta(hours=index, minutes=1),
            }
        )
    frame = pd.DataFrame(rows)
    app_config.data_validation.require_pandera = True
    validated, result = validate_frame(
        frame,
        "weather_measurements",
        app_config,
        context={"test": "nullable-accumulation-period"},
    )
    assert len(validated) == 1000
    assert result.valid is True
    assert result.failure_count == 0


def test_weather_contract_rejects_fractional_accumulation_period(app_config) -> None:  # type: ignore[no-untyped-def]
    if not PanderaFrameValidator.available():
        pytest.skip("Pandera is not installed in this build environment")

    now = datetime(2026, 1, 1, tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {
                "source": "IMGW",
                "source_station_id": "123",
                "measurement_time": now,
                "temperature_c": 5.0,
                "humidity_percent": 80.0,
                "pressure_hpa": 1010.0,
                "precipitation_mm": 1.0,
                "precipitation_accumulation_period_hours": 6.5,
                "wind_speed_mps": 2.0,
                "wind_direction_deg": 180.0,
                "is_valid": True,
                "collected_at": now,
            }
        ]
    )
    app_config.data_validation.require_pandera = True
    _validated, result = PanderaFrameValidator(app_config.data_validation).validate(
        frame,
        "weather_measurements",
        policy="report",
        context={"test": "fractional-accumulation-period"},
    )
    assert result.valid is False
    assert result.failure_count >= 1
