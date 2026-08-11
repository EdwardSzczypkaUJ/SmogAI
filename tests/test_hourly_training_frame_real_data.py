from __future__ import annotations

import numpy as np
import pandas as pd

from smog_ai.hourly.features import (
    _bundle_weather_training_frame,
    _expand_target,
)


def test_weather_training_bundle_uses_unique_imgw_stations_not_air_matches() -> None:
    times = pd.date_range("2026-06-01T00:00:00Z", periods=3, freq="1h")
    bundle = {
        "weather_stations": [
            {"id": 10, "source_id": "A", "latitude": 50.0, "longitude": 19.0},
            {"id": 20, "source_id": "B", "latitude": 52.0, "longitude": 21.0},
        ],
        "weather_measurements": [
            {
                "weather_station_id": station_id,
                "source_station_id": source_id,
                "measurement_time": timestamp.isoformat(),
                "temperature_c": 15.0 + offset,
                "humidity_percent": 60.0,
                "pressure_hpa": 1010.0,
                "precipitation_mm": 0.0,
                "precipitation_accumulation_period_hours": 6,
                "wind_speed_mps": 2.0,
                "wind_direction_deg": 180.0,
                "is_valid": True,
            }
            for station_id, source_id, offset in ((10, "A", 0.0), (20, "B", 1.0))
            for timestamp in times
        ],
        # Deliberately many air stations point to the same two IMGW stations.
        # Weather-model training must not multiply the source observations.
        "station_matches": [
            {"air_station_id": air_id, "weather_station_id": 10 if air_id < 5 else 20}
            for air_id in range(1, 9)
        ],
    }

    frame = _bundle_weather_training_frame(bundle, max_days=30)

    assert len(frame) == 6
    assert set(frame["air_station_id"].astype(int)) == {10, 20}
    assert not frame.duplicated(["air_station_id", "measurement_time"]).any()


def test_hourly_expansion_is_bounded_before_horizon_cartesian_product() -> None:
    rows: list[dict[str, object]] = []
    for station_id in (1, 2):
        for index, timestamp in enumerate(
            pd.date_range("2026-01-01T00:00:00Z", periods=120, freq="1h")
        ):
            rows.append(
                {
                    "air_station_id": station_id,
                    "measurement_time": timestamp,
                    "temperature_c": float(index % 24),
                    "latitude": 49.0 + station_id,
                    "longitude": 18.0 + station_id,
                }
            )
    base = pd.DataFrame(rows)

    frame = _expand_target(
        base,
        target_column="temperature_c",
        horizons=range(1, 49),
        allow_negative_target=True,
        require_current_value=True,
        maximum_output_rows=960,
    )

    assert 0 < len(frame) <= 960
    assert set(frame["air_station_id"]) == {1, 2}
    deltas = (
        pd.to_datetime(frame["target_time"], utc=True)
        - pd.to_datetime(frame["measurement_time"], utc=True)
    ).dt.total_seconds() / 3600.0
    assert np.allclose(deltas, frame["horizon_hours"].astype(float))


def test_sparse_six_hour_precipitation_can_use_missing_current_accumulation() -> None:
    times = pd.date_range("2026-06-01T00:00:00Z", periods=13, freq="1h")
    precipitation = [np.nan] * len(times)
    precipitation[0] = 0.0
    precipitation[6] = 3.0
    precipitation[12] = 1.0
    base = pd.DataFrame(
        {
            "air_station_id": [10] * len(times),
            "measurement_time": times,
            "precipitation_mm": precipitation,
            "latitude": [50.0] * len(times),
            "longitude": [19.0] * len(times),
        }
    )

    frame = _expand_target(
        base,
        target_column="precipitation_mm",
        horizons=[1],
        allow_negative_target=False,
        require_current_value=False,
    )

    row = frame.loc[
        pd.to_datetime(frame["measurement_time"], utc=True)
        == pd.Timestamp("2026-06-01T05:00:00Z")
    ]
    assert len(row) == 1
    assert pd.isna(row.iloc[0]["precipitation_mm"])
    assert float(row.iloc[0]["target"]) == 3.0
