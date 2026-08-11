from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.database.models import AirMeasurement, AirStation, StationMatch, WeatherMeasurement
from smog_ai.time_utils import utc_now

FEATURE_COLUMNS = [
    "pm_lag_1",
    "pm_lag_3",
    "pm_lag_6",
    "pm_lag_12",
    "pm_lag_24",
    "pm_rolling_mean_3",
    "pm_rolling_mean_6",
    "pm_rolling_mean_24",
    "pm_rolling_std_24",
    "pm_delta_1",
    "temperature_c",
    "humidity_percent",
    "pressure_hpa",
    "precipitation_mm",
    "wind_speed_mps",
    "wind_u",
    "wind_v",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "month_sin",
    "month_cos",
    "latitude",
    "longitude",
]

WEATHER_COLUMNS = [
    "temperature_c",
    "humidity_percent",
    "pressure_hpa",
    "precipitation_mm",
    "wind_speed_mps",
    "wind_direction_deg",
]

WEATHER_METADATA_COLUMNS = [
    "precipitation_accumulation_period_hours",
]


def _empty_air_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["air_station_id", "measurement_time", "value", "latitude", "longitude"]
    )


def _normalise_hourly_air(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalise observations to one hourly series per air station.

    This helper is shared by the database and object-storage data paths.  Keeping
    feature engineering below this boundary guarantees that training produces the
    same columns regardless of where the source bytes are stored (Bridge pattern).
    """
    if frame.empty:
        return _empty_air_frame()
    output = frame.copy()
    output["measurement_time"] = pd.to_datetime(output["measurement_time"], utc=True, errors="coerce")
    output["value"] = pd.to_numeric(output["value"], errors="coerce")
    output["air_station_id"] = pd.to_numeric(output["air_station_id"], errors="coerce")
    output = output.dropna(subset=["air_station_id", "measurement_time", "value"])
    if output.empty:
        return _empty_air_frame()
    output["air_station_id"] = output["air_station_id"].astype(int)
    groups: list[pd.DataFrame] = []
    for station_id, group in output.groupby("air_station_id", sort=False):
        group = group.sort_values("measurement_time").drop_duplicates("measurement_time", keep="last")
        group = group.set_index("measurement_time").resample("1h").agg(
            value=("value", "mean"),
            latitude=("latitude", "last"),
            longitude=("longitude", "last"),
        )
        group[["latitude", "longitude"]] = group[["latitude", "longitude"]].ffill().bfill()
        group["air_station_id"] = int(station_id)
        groups.append(group.reset_index())
    return pd.concat(groups, ignore_index=True) if groups else _empty_air_frame()


def _normalise_weather(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "air_station_id",
        "measurement_time",
        *WEATHER_COLUMNS,
        *WEATHER_METADATA_COLUMNS,
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    output = frame.copy()
    for column in columns:
        if column not in output.columns:
            output[column] = np.nan
    output["measurement_time"] = pd.to_datetime(output["measurement_time"], utc=True, errors="coerce")
    output["air_station_id"] = pd.to_numeric(output["air_station_id"], errors="coerce")
    for column in [*WEATHER_COLUMNS, *WEATHER_METADATA_COLUMNS]:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=["air_station_id", "measurement_time"])
    if output.empty:
        return pd.DataFrame(columns=columns)
    output["air_station_id"] = output["air_station_id"].astype(int)
    return output[columns].sort_values(["air_station_id", "measurement_time"])


def _air_frame(session: Session, parameter: str, max_days: int) -> pd.DataFrame:
    cutoff = utc_now() - timedelta(days=max_days)
    rows = session.execute(
        select(
            AirMeasurement.air_station_id,
            AirMeasurement.measurement_time,
            AirMeasurement.value,
            AirStation.latitude,
            AirStation.longitude,
        )
        .join(AirStation, AirStation.id == AirMeasurement.air_station_id)
        .where(
            AirMeasurement.parameter == parameter,
            AirMeasurement.is_valid.is_(True),
            AirMeasurement.value.is_not(None),
            AirMeasurement.measurement_time >= cutoff,
        )
        .order_by(AirMeasurement.air_station_id, AirMeasurement.measurement_time)
    ).all()
    if not rows:
        return _empty_air_frame()
    frame = pd.DataFrame(
        rows,
        columns=["air_station_id", "measurement_time", "value", "latitude", "longitude"],
    )
    return _normalise_hourly_air(frame)


def _weather_frame(session: Session, max_days: int) -> pd.DataFrame:
    cutoff = utc_now() - timedelta(days=max_days)
    rows = session.execute(
        select(
            StationMatch.air_station_id,
            WeatherMeasurement.measurement_time,
            WeatherMeasurement.temperature_c,
            WeatherMeasurement.humidity_percent,
            WeatherMeasurement.pressure_hpa,
            WeatherMeasurement.precipitation_mm,
            WeatherMeasurement.precipitation_accumulation_period_hours,
            WeatherMeasurement.wind_speed_mps,
            WeatherMeasurement.wind_direction_deg,
        )
        .join(WeatherMeasurement, WeatherMeasurement.weather_station_id == StationMatch.weather_station_id)
        .where(
            WeatherMeasurement.is_valid.is_(True),
            WeatherMeasurement.measurement_time >= cutoff,
        )
        .order_by(StationMatch.air_station_id, WeatherMeasurement.measurement_time)
    ).all()
    columns = [
        "air_station_id",
        "measurement_time",
        *WEATHER_COLUMNS[:4],
        "precipitation_accumulation_period_hours",
        *WEATHER_COLUMNS[4:],
    ]
    return _normalise_weather(pd.DataFrame(rows, columns=columns))


def _latest_time(*frames: pd.DataFrame) -> datetime:
    candidates: list[pd.Timestamp] = []
    for frame in frames:
        if not frame.empty and "measurement_time" in frame.columns:
            parsed = pd.to_datetime(frame["measurement_time"], utc=True, errors="coerce")
            if parsed.notna().any():
                candidates.append(parsed.max())
    if not candidates:
        return utc_now()
    latest = max(candidates)
    return latest.to_pydatetime().astimezone(UTC)


def _bundle_air_frame(bundle: dict[str, Any], parameter: str, max_days: int) -> pd.DataFrame:
    measurements = pd.DataFrame(bundle.get("air_measurements") or [])
    stations = pd.DataFrame(bundle.get("air_stations") or [])
    if measurements.empty or stations.empty:
        return _empty_air_frame()

    measurements = measurements.copy()
    if "air_station_id" not in measurements.columns:
        station_map = {
            str(row["source_id"]): int(row["id"])
            for _, row in stations.dropna(subset=["source_id", "id"]).iterrows()
        }
        measurements["air_station_id"] = measurements["source_station_id"].astype(str).map(station_map)
    station_coordinates = stations[["id", "latitude", "longitude"]].rename(columns={"id": "air_station_id"})
    measurements["parameter"] = measurements.get("parameter", "").astype(str)
    measurements["is_valid"] = measurements.get("is_valid", True).fillna(False).astype(bool)
    measurements["measurement_time"] = pd.to_datetime(
        measurements.get("measurement_time"), utc=True, errors="coerce"
    )
    measurements["value"] = pd.to_numeric(measurements.get("value"), errors="coerce")
    latest = _latest_time(measurements)
    cutoff = pd.Timestamp(latest - timedelta(days=max_days))
    selected = measurements[
        (measurements["parameter"] == parameter)
        & measurements["is_valid"]
        & measurements["value"].notna()
        & (measurements["measurement_time"] >= cutoff)
    ].copy()
    selected = selected.merge(station_coordinates, on="air_station_id", how="left")
    return _normalise_hourly_air(
        selected[["air_station_id", "measurement_time", "value", "latitude", "longitude"]]
    )


def _bundle_weather_frame(bundle: dict[str, Any], max_days: int) -> pd.DataFrame:
    measurements = pd.DataFrame(bundle.get("weather_measurements") or [])
    stations = pd.DataFrame(bundle.get("weather_stations") or [])
    matches = pd.DataFrame(bundle.get("station_matches") or [])
    columns = [
        "air_station_id",
        "measurement_time",
        *WEATHER_COLUMNS,
        *WEATHER_METADATA_COLUMNS,
    ]
    if measurements.empty or matches.empty:
        return pd.DataFrame(columns=columns)

    measurements = measurements.copy()
    if "weather_station_id" not in measurements.columns and not stations.empty:
        station_map = {
            str(row["source_id"]): int(row["id"])
            for _, row in stations.dropna(subset=["source_id", "id"]).iterrows()
        }
        measurements["weather_station_id"] = measurements["source_station_id"].astype(str).map(station_map)
    measurements["is_valid"] = measurements.get("is_valid", True).fillna(False).astype(bool)
    measurements["measurement_time"] = pd.to_datetime(
        measurements.get("measurement_time"), utc=True, errors="coerce"
    )
    latest = _latest_time(measurements)
    cutoff = pd.Timestamp(latest - timedelta(days=max_days))
    selected = measurements[
        measurements["is_valid"] & (measurements["measurement_time"] >= cutoff)
    ].copy()
    match_columns = matches[["air_station_id", "weather_station_id"]].drop_duplicates()
    selected = selected.merge(match_columns, on="weather_station_id", how="inner")
    for column in [*WEATHER_COLUMNS, *WEATHER_METADATA_COLUMNS]:
        if column not in selected.columns:
            selected[column] = np.nan
    return _normalise_weather(selected[columns])


def _merge_weather(air: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    if air.empty:
        return air
    if weather.empty:
        output = air.copy()
        for column in WEATHER_COLUMNS:
            output[column] = np.nan
        return output
    merged_groups: list[pd.DataFrame] = []
    for station_id, air_group in air.groupby("air_station_id", sort=False):
        weather_group = weather[weather["air_station_id"] == station_id]
        air_group = air_group.sort_values("measurement_time")
        if weather_group.empty:
            for column in WEATHER_COLUMNS:
                air_group[column] = np.nan
            merged_groups.append(air_group)
            continue
        merged_groups.append(
            pd.merge_asof(
                air_group,
                weather_group.drop(columns=["air_station_id"]).sort_values("measurement_time"),
                on="measurement_time",
                direction="nearest",
                tolerance=pd.Timedelta(3, unit="h"),
            )
        )
    return pd.concat(merged_groups, ignore_index=True)


def _engineer(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output: list[pd.DataFrame] = []
    for _, group in frame.groupby("air_station_id", sort=False):
        group = group.sort_values("measurement_time").copy()
        series = pd.to_numeric(group["value"], errors="coerce")
        for lag in (1, 3, 6, 12, 24):
            group[f"pm_lag_{lag}"] = series.shift(lag)
        for window in (3, 6, 24):
            group[f"pm_rolling_mean_{window}"] = series.shift(1).rolling(window, min_periods=1).mean()
        group["pm_rolling_std_24"] = series.shift(1).rolling(24, min_periods=2).std()
        group["pm_delta_1"] = series.diff(1)
        radians = np.deg2rad(pd.to_numeric(group["wind_direction_deg"], errors="coerce"))
        speed = pd.to_numeric(group["wind_speed_mps"], errors="coerce")
        group["wind_u"] = speed * np.cos(radians)
        group["wind_v"] = speed * np.sin(radians)
        timestamp = pd.to_datetime(group["measurement_time"], utc=True)
        group["hour_sin"] = np.sin(2 * math.pi * timestamp.dt.hour / 24)
        group["hour_cos"] = np.cos(2 * math.pi * timestamp.dt.hour / 24)
        group["dow_sin"] = np.sin(2 * math.pi * timestamp.dt.dayofweek / 7)
        group["dow_cos"] = np.cos(2 * math.pi * timestamp.dt.dayofweek / 7)
        group["month_sin"] = np.sin(2 * math.pi * (timestamp.dt.month - 1) / 12)
        group["month_cos"] = np.cos(2 * math.pi * (timestamp.dt.month - 1) / 12)
        output.append(group)
    return pd.concat(output, ignore_index=True)


def _finalise_training_frame(
    air: pd.DataFrame,
    weather: pd.DataFrame,
    *,
    horizon_hours: int,
) -> pd.DataFrame:
    frame = _engineer(_merge_weather(air, weather))
    if frame.empty:
        return frame
    frame["target"] = frame.groupby("air_station_id", sort=False)["value"].shift(-horizon_hours)
    frame["target_time"] = frame["measurement_time"] + pd.to_timedelta(horizon_hours, unit="h")

    # Hourly resampling intentionally creates rows for missing hours.  Such rows
    # are useful while constructing lags, but a row without the current PM value
    # is not a valid supervised-learning observation.  The previous implementation
    # dropped missing target/lag only, allowing a handful of NaN ``value`` rows to
    # reach the blocking Pandera contract during the first real collection.
    required = [
        "air_station_id",
        "measurement_time",
        "value",
        "latitude",
        "longitude",
        "target",
        "target_time",
        "pm_lag_1",
    ]
    numeric_required = [
        "air_station_id",
        "value",
        "latitude",
        "longitude",
        "target",
        "pm_lag_1",
    ]
    for column in numeric_required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame[numeric_required] = frame[numeric_required].replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(subset=required)
    frame = frame[
        (frame["value"] >= 0)
        & (frame["target"] >= 0)
        & frame["latitude"].between(-90, 90, inclusive="both")
        & frame["longitude"].between(-180, 180, inclusive="both")
        & (frame["target_time"] > frame["measurement_time"])
    ]
    return frame.reset_index(drop=True)


def build_training_frame(
    session: Session,
    *,
    parameter: str,
    horizon_hours: int,
    max_days: int = 730,
) -> pd.DataFrame:
    return _finalise_training_frame(
        _air_frame(session, parameter, max_days),
        _weather_frame(session, max_days),
        horizon_hours=horizon_hours,
    )


def build_training_frame_from_operational_bundle(
    bundle: dict[str, Any],
    *,
    parameter: str,
    horizon_hours: int,
    max_days: int = 730,
) -> pd.DataFrame:
    """Build a training frame after downloading the source bundle from object storage.

    No database session is used here.  This is the concrete course-assignment path:
    the collector uploads data to DigitalOcean Spaces, the local training process
    downloads it, validates/cleans it in memory and only then fits models.
    """
    return _finalise_training_frame(
        _bundle_air_frame(bundle, parameter, max_days),
        _bundle_weather_frame(bundle, max_days),
        horizon_hours=horizon_hours,
    )


def build_latest_feature_rows(
    session: Session,
    *,
    parameter: str,
    max_days: int = 7,
) -> pd.DataFrame:
    frame = _engineer(
        _merge_weather(_air_frame(session, parameter, max_days), _weather_frame(session, max_days))
    )
    if frame.empty:
        return frame
    frame = frame.dropna(subset=["value"]).sort_values("measurement_time")
    return frame.groupby("air_station_id", as_index=False, sort=False).tail(1).reset_index(drop=True)
