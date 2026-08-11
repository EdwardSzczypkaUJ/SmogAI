from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import alias_key
from smog_ai.database.models import AirStation, WeatherMeasurement, WeatherStation
from smog_ai.features.builder import (
    FEATURE_COLUMNS,
    WEATHER_COLUMNS,
    WEATHER_METADATA_COLUMNS,
    _air_frame,
    _bundle_air_frame,
    _bundle_weather_frame,
    _engineer,
    _merge_weather,
    _weather_frame,
)
from smog_ai.hourly.time_contract import ForecastTimeContract
from smog_ai.time_utils import ensure_utc, utc_now

HORIZON_FEATURE_COLUMNS = [
    "horizon_hours",
    "horizon_squared",
    "horizon_sqrt",
    "horizon_log1p",
    "target_hour_sin",
    "target_hour_cos",
    "target_dow_sin",
    "target_dow_cos",
    "target_year_sin",
    "target_year_cos",
]

WEATHER_LAG_COLUMNS = [
    "temperature_lag_1",
    "temperature_lag_3",
    "temperature_lag_6",
    "temperature_lag_12",
    "temperature_lag_24",
    "temperature_rolling_mean_6",
    "temperature_rolling_mean_24",
    "temperature_delta_1",
    "humidity_lag_1",
    "pressure_lag_1",
    "precipitation_lag_1",
    "precipitation_rolling_mean_6",
    "precipitation_rolling_mean_24",
    "wind_speed_lag_1",
    "wind_u",
    "wind_v",
]

WEATHER_HOURLY_FEATURE_COLUMNS = [
    *WEATHER_COLUMNS,
    *WEATHER_LAG_COLUMNS,
    "origin_hour_sin",
    "origin_hour_cos",
    "origin_dow_sin",
    "origin_dow_cos",
    "origin_year_sin",
    "origin_year_cos",
    "latitude",
    "longitude",
    *HORIZON_FEATURE_COLUMNS,
]

PM_PREDICTED_WEATHER_COLUMNS = [
    "predicted_temperature_c",
    "predicted_precipitation_probability",
    "predicted_precipitation_mm",
]

AUXILIARY_AIR_LAGS = (1, 6, 24)


def _auxiliary_token(parameter: str) -> str:
    token = alias_key(parameter).casefold()
    return token or "unknown"


def auxiliary_air_feature_columns(parameters: Iterable[str]) -> list[str]:
    columns: list[str] = []
    for parameter in parameters:
        token = _auxiliary_token(str(parameter))
        for suffix in ("value", *(f"lag_{lag}" for lag in AUXILIARY_AIR_LAGS)):
            name = f"aux_{token}_{suffix}"
            if name not in columns:
                columns.append(name)
    return columns


def _auxiliary_frame(frame: pd.DataFrame, parameter: str) -> pd.DataFrame:
    columns = ["air_station_id", "measurement_time", *auxiliary_air_feature_columns([parameter])]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    token = _auxiliary_token(parameter)
    pieces: list[pd.DataFrame] = []
    for _, group in frame.groupby("air_station_id", sort=False):
        group = group.sort_values("measurement_time").copy()
        values = pd.to_numeric(group["value"], errors="coerce")
        selected = group[["air_station_id", "measurement_time"]].copy()
        selected[f"aux_{token}_value"] = values
        for lag in AUXILIARY_AIR_LAGS:
            selected[f"aux_{token}_lag_{lag}"] = values.shift(lag)
        pieces.append(selected)
    return (
        pd.concat(pieces, ignore_index=True)
        if pieces
        else pd.DataFrame(columns=columns)
    )


def _merge_auxiliary_air(
    base: pd.DataFrame,
    auxiliary_frames: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    if base.empty:
        return base
    output = base.copy()
    for parameter, frame in auxiliary_frames.items():
        feature_columns = auxiliary_air_feature_columns([parameter])
        if frame.empty:
            for column in feature_columns:
                if column not in output.columns:
                    output[column] = np.nan
            continue
        auxiliary = _auxiliary_frame(frame, parameter)
        output = output.merge(
            auxiliary,
            on=["air_station_id", "measurement_time"],
            how="left",
            validate="many_to_one",
        )
    return output


PM_HOURLY_FEATURE_COLUMNS = [
    "value",
    *FEATURE_COLUMNS,
    *HORIZON_FEATURE_COLUMNS,
    *PM_PREDICTED_WEATHER_COLUMNS,
]

KEY_COLUMNS = ["air_station_id", "measurement_time", "horizon_hours", "target_time"]


def _cyclic_time(frame: pd.DataFrame, column: str, *, prefix: str) -> pd.DataFrame:
    result = frame.copy()
    timestamp = pd.to_datetime(result[column], utc=True, errors="coerce")
    result[f"{prefix}_hour_sin"] = np.sin(2 * math.pi * timestamp.dt.hour / 24)
    result[f"{prefix}_hour_cos"] = np.cos(2 * math.pi * timestamp.dt.hour / 24)
    result[f"{prefix}_dow_sin"] = np.sin(2 * math.pi * timestamp.dt.dayofweek / 7)
    result[f"{prefix}_dow_cos"] = np.cos(2 * math.pi * timestamp.dt.dayofweek / 7)
    day_of_year = timestamp.dt.dayofyear
    result[f"{prefix}_year_sin"] = np.sin(2 * math.pi * (day_of_year - 1) / 365.2425)
    result[f"{prefix}_year_cos"] = np.cos(2 * math.pi * (day_of_year - 1) / 365.2425)
    return result


def _add_horizon_features(frame: pd.DataFrame, horizon_hours: int) -> pd.DataFrame:
    result = frame.copy()
    horizon = float(horizon_hours)
    result["horizon_hours"] = int(horizon_hours)
    result["horizon_squared"] = horizon * horizon
    result["horizon_sqrt"] = math.sqrt(horizon)
    result["horizon_log1p"] = math.log1p(horizon)
    result["target_time"] = pd.to_datetime(
        result["measurement_time"], utc=True, errors="coerce"
    ) + pd.to_timedelta(horizon_hours, unit="h")
    result = _cyclic_time(result, "target_time", prefix="target")
    return result


def _attach_coordinates(
    frame: pd.DataFrame,
    coordinates: pd.DataFrame,
) -> pd.DataFrame:
    if frame.empty:
        result = frame.copy()
        result["latitude"] = np.nan
        result["longitude"] = np.nan
        return result
    return frame.merge(
        coordinates.drop_duplicates("air_station_id"),
        on="air_station_id",
        how="left",
    )


def _coordinates_from_session(session: Session) -> pd.DataFrame:
    rows = session.execute(
        select(AirStation.id, AirStation.latitude, AirStation.longitude)
    ).all()
    return pd.DataFrame(rows, columns=["air_station_id", "latitude", "longitude"])


def _coordinates_from_bundle(bundle: dict[str, Any]) -> pd.DataFrame:
    stations = pd.DataFrame(bundle.get("air_stations") or [])
    if stations.empty:
        return pd.DataFrame(columns=["air_station_id", "latitude", "longitude"])
    return stations.rename(columns={"id": "air_station_id"})[
        ["air_station_id", "latitude", "longitude"]
    ]


def _weather_training_frame(session: Session, max_days: int) -> pd.DataFrame:
    """Return one meteorological series per IMGW station for weather targets.

    The PM feature path intentionally maps every air station to its matched IMGW
    station.  Reusing that replicated frame for training temperature/rain models
    multiplied identical weather observations by the number of matched GIOŚ
    stations and, with 48 horizons, could create tens of millions of duplicate
    rows.  Weather targets are global models, so they are trained once per unique
    IMGW station using the station's own coordinates.

    ``air_station_id`` remains the provider-neutral series key used by the hourly
    feature engine.  For this weather-only frame it contains the positive local
    ``weather_station_id``.  The identifier is not a model feature and is never
    exposed as a GIOŚ station identifier.
    """

    cutoff = utc_now() - timedelta(days=max_days)
    rows = session.execute(
        select(
            WeatherMeasurement.weather_station_id.label("air_station_id"),
            WeatherMeasurement.measurement_time,
            WeatherMeasurement.temperature_c,
            WeatherMeasurement.humidity_percent,
            WeatherMeasurement.pressure_hpa,
            WeatherMeasurement.precipitation_mm,
            WeatherMeasurement.precipitation_accumulation_period_hours,
            WeatherMeasurement.wind_speed_mps,
            WeatherMeasurement.wind_direction_deg,
            WeatherStation.latitude,
            WeatherStation.longitude,
        )
        .join(WeatherStation, WeatherStation.id == WeatherMeasurement.weather_station_id)
        .where(
            WeatherMeasurement.is_valid.is_(True),
            WeatherMeasurement.measurement_time >= cutoff,
        )
        .order_by(WeatherMeasurement.weather_station_id, WeatherMeasurement.measurement_time)
    ).all()
    columns = [
        "air_station_id",
        "measurement_time",
        *WEATHER_COLUMNS[:4],
        "precipitation_accumulation_period_hours",
        *WEATHER_COLUMNS[4:],
        "latitude",
        "longitude",
    ]
    return pd.DataFrame(rows, columns=columns)


def _bundle_weather_training_frame(
    bundle: dict[str, Any],
    max_days: int,
) -> pd.DataFrame:
    """Build the unique-IMGW-station weather frame from an object-store bundle."""

    measurements = pd.DataFrame(bundle.get("weather_measurements") or [])
    stations = pd.DataFrame(bundle.get("weather_stations") or [])
    columns = [
        "air_station_id",
        "measurement_time",
        *WEATHER_COLUMNS,
        *WEATHER_METADATA_COLUMNS,
        "latitude",
        "longitude",
    ]
    if measurements.empty or stations.empty:
        return pd.DataFrame(columns=columns)

    measurements = measurements.copy()
    if "weather_station_id" not in measurements.columns:
        station_map = {
            str(row["source_id"]): int(row["id"])
            for _, row in stations.dropna(subset=["source_id", "id"]).iterrows()
        }
        measurements["weather_station_id"] = (
            measurements["source_station_id"].astype(str).map(station_map)
        )
    measurements["is_valid"] = (
        measurements.get("is_valid", True).fillna(False).astype(bool)
    )
    measurements["measurement_time"] = pd.to_datetime(
        measurements.get("measurement_time"), utc=True, errors="coerce"
    )
    valid_times = measurements.loc[
        measurements["is_valid"] & measurements["measurement_time"].notna(),
        "measurement_time",
    ]
    if valid_times.empty:
        return pd.DataFrame(columns=columns)
    cutoff = valid_times.max() - pd.Timedelta(days=max_days)
    selected = measurements[
        measurements["is_valid"]
        & measurements["weather_station_id"].notna()
        & (measurements["measurement_time"] >= cutoff)
    ].copy()

    station_coordinates = stations[["id", "latitude", "longitude"]].rename(
        columns={"id": "weather_station_id"}
    )
    selected = selected.merge(station_coordinates, on="weather_station_id", how="left")
    selected["air_station_id"] = pd.to_numeric(
        selected["weather_station_id"], errors="coerce"
    )
    for column in [*WEATHER_COLUMNS, *WEATHER_METADATA_COLUMNS, "latitude", "longitude"]:
        if column not in selected.columns:
            selected[column] = np.nan
    return selected[columns]


def _deterministic_origin_sample(
    frame: pd.DataFrame,
    *,
    horizons: Iterable[int],
    maximum_output_rows: int | None,
    effective_horizon_count: int | None = None,
) -> pd.DataFrame:
    """Bound horizon expansion while preserving stations and chronology.

    Sampling is performed on origin rows *before* creating the long h1--h48
    representation.  Target lookup still uses the complete source series.  The
    selection is deterministic and approximately proportional to each station's
    history, with evenly spaced indexes so that seasons and times remain covered.
    """

    horizon_count = int(
        effective_horizon_count
        if effective_horizon_count is not None
        else len(tuple(int(value) for value in horizons))
    )
    if frame.empty or maximum_output_rows is None or maximum_output_rows <= 0:
        return frame
    if horizon_count <= 0 or len(frame) * horizon_count <= maximum_output_rows:
        return frame

    maximum_origins = max(1, maximum_output_rows // horizon_count)
    station_groups = list(frame.groupby("air_station_id", sort=False))
    if len(station_groups) >= maximum_origins:
        # This branch is unlikely for the configured million-row cap, but keeps
        # the function total for very small custom limits.
        selected_groups = station_groups[:maximum_origins]
        return pd.concat([group.iloc[[-1]] for _, group in selected_groups], ignore_index=True)

    total = len(frame)
    pieces: list[pd.DataFrame] = []
    remaining = maximum_origins
    for position, (_, group) in enumerate(station_groups):
        groups_left = len(station_groups) - position
        proportional = int(round(maximum_origins * len(group) / total))
        count = max(1, min(len(group), proportional, remaining - (groups_left - 1)))
        indexes = np.linspace(0, len(group) - 1, num=count, dtype=int)
        pieces.append(group.iloc[np.unique(indexes)].copy())
        remaining -= len(pieces[-1])
    sampled = pd.concat(pieces, ignore_index=True)
    if len(sampled) > maximum_origins:
        indexes = np.linspace(0, len(sampled) - 1, num=maximum_origins, dtype=int)
        sampled = sampled.iloc[np.unique(indexes)].copy()
    return sampled.sort_values(["air_station_id", "measurement_time"]).reset_index(drop=True)


def _normalise_hourly_weather(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "air_station_id",
        "measurement_time",
        *WEATHER_COLUMNS,
        *WEATHER_METADATA_COLUMNS,
        "latitude",
        "longitude",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    output = frame.copy()
    for column in columns:
        if column not in output.columns:
            output[column] = np.nan
    output["measurement_time"] = pd.to_datetime(
        output["measurement_time"], utc=True, errors="coerce"
    )
    output["air_station_id"] = pd.to_numeric(output["air_station_id"], errors="coerce")
    for column in [*WEATHER_COLUMNS, *WEATHER_METADATA_COLUMNS, "latitude", "longitude"]:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=["air_station_id", "measurement_time"])
    if output.empty:
        return pd.DataFrame(columns=columns)
    output["air_station_id"] = output["air_station_id"].astype(int)

    groups: list[pd.DataFrame] = []
    for station_id, group in output.groupby("air_station_id", sort=False):
        group = group.sort_values("measurement_time").drop_duplicates(
            "measurement_time", keep="last"
        )
        indexed = group.set_index("measurement_time")
        hourly = indexed.resample("1h").agg(
            temperature_c=("temperature_c", "mean"),
            humidity_percent=("humidity_percent", "mean"),
            pressure_hpa=("pressure_hpa", "mean"),
            # ``precipitation_mm`` is a source-defined accumulation (IMGW
            # terminowe ``WO6G`` by default), not an hourly increment.  Summing
            # overlapping accumulations would double-count rainfall.
            precipitation_mm=("precipitation_mm", "last"),
            precipitation_accumulation_period_hours=(
                "precipitation_accumulation_period_hours",
                "last",
            ),
            wind_speed_mps=("wind_speed_mps", "mean"),
            wind_direction_deg=("wind_direction_deg", "mean"),
            latitude=("latitude", "last"),
            longitude=("longitude", "last"),
        )
        hourly[["latitude", "longitude"]] = hourly[["latitude", "longitude"]].ffill().bfill()
        hourly["air_station_id"] = int(station_id)
        groups.append(hourly.reset_index())
    return pd.concat(groups, ignore_index=True) if groups else pd.DataFrame(columns=columns)


def _engineer_weather(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output: list[pd.DataFrame] = []
    for _, group in frame.groupby("air_station_id", sort=False):
        group = group.sort_values("measurement_time").copy()
        temperature = pd.to_numeric(group["temperature_c"], errors="coerce")
        humidity = pd.to_numeric(group["humidity_percent"], errors="coerce")
        pressure = pd.to_numeric(group["pressure_hpa"], errors="coerce")
        precipitation = pd.to_numeric(group["precipitation_mm"], errors="coerce")
        wind_speed = pd.to_numeric(group["wind_speed_mps"], errors="coerce")
        direction = np.deg2rad(pd.to_numeric(group["wind_direction_deg"], errors="coerce"))

        for lag in (1, 3, 6, 12, 24):
            group[f"temperature_lag_{lag}"] = temperature.shift(lag)
        group["temperature_rolling_mean_6"] = temperature.shift(1).rolling(6, min_periods=1).mean()
        group["temperature_rolling_mean_24"] = temperature.shift(1).rolling(24, min_periods=2).mean()
        group["temperature_delta_1"] = temperature.diff(1)
        group["humidity_lag_1"] = humidity.shift(1)
        group["pressure_lag_1"] = pressure.shift(1)
        group["precipitation_lag_1"] = precipitation.shift(1)
        group["precipitation_rolling_mean_6"] = (
            precipitation.shift(1).rolling(6, min_periods=2).mean()
        )
        group["precipitation_rolling_mean_24"] = (
            precipitation.shift(1).rolling(24, min_periods=4).mean()
        )
        group["wind_speed_lag_1"] = wind_speed.shift(1)
        group["wind_u"] = wind_speed * np.cos(direction)
        group["wind_v"] = wind_speed * np.sin(direction)
        output.append(group)
    result = pd.concat(output, ignore_index=True)
    return _cyclic_time(result, "measurement_time", prefix="origin")



def _normalise_horizon_buckets(
    horizons: tuple[int, ...],
    bucket_edges: Iterable[int] | None,
) -> tuple[tuple[int, ...], ...]:
    if not horizons:
        return ()
    edges = sorted({int(value) for value in (bucket_edges or ()) if int(value) > 0})
    if not edges:
        return (horizons,)
    buckets: list[tuple[int, ...]] = []
    lower = 0
    for edge in edges:
        bucket = tuple(value for value in horizons if lower < value <= edge)
        if bucket:
            buckets.append(bucket)
        lower = edge
    tail = tuple(value for value in horizons if value > lower)
    if tail:
        buckets.append(tail)
    return tuple(buckets) if buckets else (horizons,)


def _origins_for_horizon(
    origin_rows: pd.DataFrame,
    *,
    horizon: int,
    buckets: tuple[tuple[int, ...], ...],
    samples_per_bucket: int | None,
    random_state: int,
) -> pd.DataFrame:
    if origin_rows.empty or samples_per_bucket is None or samples_per_bucket <= 0:
        return origin_rows

    selected_bucket: tuple[int, ...] | None = None
    bucket_index = 0
    for position, bucket in enumerate(buckets):
        if horizon in bucket:
            selected_bucket = bucket
            bucket_index = position
            break
    if selected_bucket is None:
        return origin_rows.iloc[0:0].copy()

    sample_count = min(int(samples_per_bucket), len(selected_bucket))
    if sample_count >= len(selected_bucket):
        return origin_rows

    keys = origin_rows[["air_station_id", "measurement_time"]]
    hashes = pd.util.hash_pandas_object(
        keys,
        index=False,
        hash_key=f"{int(random_state):016d}"[-16:],
    ).to_numpy(dtype=np.uint64)
    # Different buckets use independent deterministic phases.
    bucket_seed = np.uint64(
        ((bucket_index + 1) * 0x9E3779B185EBCA87)
        & ((1 << 64) - 1)
    )
    phase = (
        hashes
        ^ bucket_seed
        ^ np.uint64(max(0, int(random_state)))
    ) % np.uint64(len(selected_bucket))
    horizon_position = selected_bucket.index(int(horizon))

    mask = np.zeros(len(origin_rows), dtype=bool)
    for offset in range(sample_count):
        chosen = (phase + np.uint64(offset)) % np.uint64(len(selected_bucket))
        mask |= chosen == np.uint64(horizon_position)

    if not mask.any() and len(origin_rows):
        # Preserve global representation even for very small synthetic frames.
        fallback = int(hashes.argmin())
        mask[fallback] = True
    return origin_rows.loc[mask].copy()


def _expand_target(
    base: pd.DataFrame,
    *,
    target_column: str,
    horizons: Iterable[int],
    allow_negative_target: bool,
    require_current_value: bool = True,
    maximum_output_rows: int | None = None,
    maximum_rows: int | None = None,
    horizon_bucket_edges: Iterable[int] | None = None,
    samples_per_horizon_bucket: int | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    """Attach exact-clock-time targets without relabelling gaps.

    Origin rows and future target rows are treated separately.  A resampled gap
    may be useful while computing lags, but it cannot be a supervised origin for
    PM or temperature when the current value is absent.  Six-hour precipitation
    accumulations are different: they are intentionally sparse, and the hurdle
    model may use an origin whose current accumulation is unavailable while the
    other causal weather features are present.
    """

    if maximum_output_rows is None and maximum_rows is not None:
        maximum_output_rows = int(maximum_rows)
    horizons_tuple = tuple(sorted({int(value) for value in horizons if int(value) > 0}))
    horizon_buckets = _normalise_horizon_buckets(
        horizons_tuple,
        horizon_bucket_edges,
    )
    effective_horizon_count = sum(
        min(
            len(bucket),
            int(samples_per_horizon_bucket)
            if samples_per_horizon_bucket is not None
            else len(bucket),
        )
        for bucket in horizon_buckets
    )
    if base.empty or not horizons_tuple:
        return base.iloc[0:0].copy()

    source = base.copy()
    source["measurement_time"] = pd.to_datetime(
        source["measurement_time"], utc=True, errors="coerce"
    )
    source["air_station_id"] = pd.to_numeric(
        source["air_station_id"], errors="coerce"
    )
    source[target_column] = pd.to_numeric(source[target_column], errors="coerce")
    for coordinate in ("latitude", "longitude"):
        source[coordinate] = pd.to_numeric(source.get(coordinate), errors="coerce")

    source = source.replace([np.inf, -np.inf], np.nan)
    source = source.dropna(
        subset=["air_station_id", "measurement_time", "latitude", "longitude"]
    )
    source = source[
        source["latitude"].between(-90, 90, inclusive="both")
        & source["longitude"].between(-180, 180, inclusive="both")
    ].copy()
    if source.empty:
        return source
    source["air_station_id"] = source["air_station_id"].astype(int)
    source = source.sort_values(["air_station_id", "measurement_time"]).drop_duplicates(
        ["air_station_id", "measurement_time"], keep="last"
    )

    origin_rows = source
    if require_current_value:
        origin_rows = origin_rows.dropna(subset=[target_column]).copy()
        if not allow_negative_target:
            origin_rows = origin_rows[origin_rows[target_column] >= 0].copy()
    origin_rows = _deterministic_origin_sample(
        origin_rows,
        horizons=horizons_tuple,
        maximum_output_rows=maximum_output_rows,
        effective_horizon_count=max(1, effective_horizon_count),
    )
    if origin_rows.empty:
        return origin_rows

    result: list[pd.DataFrame] = []
    targets_by_station = {
        int(station_id): (
            group[["measurement_time", target_column]]
            .rename(
                columns={
                    "measurement_time": "target_time",
                    target_column: "target",
                }
            )
            .assign(target=lambda frame: pd.to_numeric(frame["target"], errors="coerce"))
            .dropna(subset=["target"])
            .drop_duplicates("target_time", keep="last")
        )
        for station_id, group in source.groupby("air_station_id", sort=False)
    }

    for horizon in horizons_tuple:
        horizon_origins = _origins_for_horizon(
            origin_rows,
            horizon=horizon,
            buckets=horizon_buckets,
            samples_per_bucket=samples_per_horizon_bucket,
            random_state=random_state,
        )
        chunks: list[pd.DataFrame] = []
        for station_id, group in horizon_origins.groupby("air_station_id", sort=False):
            target_lookup = targets_by_station.get(int(station_id))
            if target_lookup is None or target_lookup.empty:
                continue
            expanded = _add_horizon_features(group, horizon)
            expanded = expanded.merge(target_lookup, on="target_time", how="inner")
            chunks.append(expanded)
        if not chunks:
            continue
        horizon_frame = pd.concat(chunks, ignore_index=True)
        required = [
            "air_station_id",
            "measurement_time",
            "target_time",
            "target",
            "latitude",
            "longitude",
        ]
        horizon_frame = horizon_frame.replace([np.inf, -np.inf], np.nan)
        horizon_frame = horizon_frame.dropna(subset=required)
        if not allow_negative_target:
            horizon_frame = horizon_frame[horizon_frame["target"] >= 0]
        result.append(horizon_frame)

    if not result:
        return origin_rows.iloc[0:0].copy()
    combined = pd.concat(result, ignore_index=True)
    combined = combined.drop_duplicates(
        ["air_station_id", "measurement_time", "horizon_hours", "target_time"],
        keep="last",
    )
    return combined.sort_values(
        ["measurement_time", "air_station_id", "horizon_hours"]
    ).reset_index(drop=True)


def _select_precipitation_period(
    frame: pd.DataFrame,
    *,
    accumulation_period_hours: int,
) -> pd.DataFrame:
    """Keep a consistent precipitation target without inventing disaggregation.

    Old databases created before schema 0002 may have a null period.  Such rows
    are treated as the configured legacy period only when a precipitation value
    is present; all new collectors persist the period explicitly.
    """

    if frame.empty:
        return frame
    output = frame.copy()
    period = pd.to_numeric(
        output.get("precipitation_accumulation_period_hours"),
        errors="coerce",
    )
    precipitation = pd.to_numeric(output.get("precipitation_mm"), errors="coerce")
    legacy = period.isna() & precipitation.notna()
    period = period.mask(legacy, float(accumulation_period_hours))
    output["precipitation_accumulation_period_hours"] = period
    return output[period == float(accumulation_period_hours)].copy()



def build_hourly_weather_training_frame(
    session: Session,
    *,
    target: str,
    horizons: Iterable[int],
    max_days: int,
    precipitation_accumulation_period_hours: int = 6,
    precipitation_occurrence_threshold_mm: float = 0.1,
    maximum_output_rows: int | None = None,
    maximum_rows: int | None = None,
    horizon_bucket_edges: Iterable[int] | None = None,
    samples_per_horizon_bucket: int | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    weather = _weather_training_frame(session, max_days)
    normalised = _normalise_hourly_weather(weather)
    if target == "precipitation_mm":
        selected = _select_precipitation_period(
            normalised,
            accumulation_period_hours=precipitation_accumulation_period_hours,
        )
        valid_times = set(
            zip(
                selected["air_station_id"].astype(int),
                pd.to_datetime(selected["measurement_time"], utc=True),
            )
        )
        keys = list(
            zip(
                normalised["air_station_id"].astype(int),
                pd.to_datetime(normalised["measurement_time"], utc=True),
            )
        )
        normalised.loc[
            [key not in valid_times for key in keys],
            "precipitation_mm",
        ] = np.nan
    engineered = _engineer_weather(normalised)
    if target not in {"temperature_c", "precipitation_mm"}:
        raise ValueError(f"Unsupported weather target: {target}")
    frame = _expand_target(
        engineered,
        target_column=target,
        horizons=horizons,
        allow_negative_target=target == "temperature_c",
        require_current_value=target == "temperature_c",
        maximum_output_rows=maximum_output_rows,
        horizon_bucket_edges=horizon_bucket_edges,
        samples_per_horizon_bucket=samples_per_horizon_bucket,
        random_state=random_state,
    )
    if not frame.empty:
        frame["current_value"] = pd.to_numeric(frame[target], errors="coerce")
        if target == "precipitation_mm":
            frame["target_occurrence"] = (
                pd.to_numeric(frame["target"], errors="coerce").fillna(0.0)
                > precipitation_occurrence_threshold_mm
            ).astype(float)
    return frame


def build_hourly_weather_training_frame_from_bundle(
    bundle: dict[str, Any],
    *,
    target: str,
    horizons: Iterable[int],
    max_days: int,
    precipitation_accumulation_period_hours: int = 6,
    precipitation_occurrence_threshold_mm: float = 0.1,
    maximum_output_rows: int | None = None,
    maximum_rows: int | None = None,
    horizon_bucket_edges: Iterable[int] | None = None,
    samples_per_horizon_bucket: int | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    weather = _bundle_weather_training_frame(bundle, max_days)
    normalised = _normalise_hourly_weather(weather)
    if target == "precipitation_mm":
        selected = _select_precipitation_period(
            normalised,
            accumulation_period_hours=precipitation_accumulation_period_hours,
        )
        valid_times = set(
            zip(
                selected["air_station_id"].astype(int),
                pd.to_datetime(selected["measurement_time"], utc=True),
            )
        )
        keys = list(
            zip(
                normalised["air_station_id"].astype(int),
                pd.to_datetime(normalised["measurement_time"], utc=True),
            )
        )
        normalised.loc[
            [key not in valid_times for key in keys],
            "precipitation_mm",
        ] = np.nan
    engineered = _engineer_weather(normalised)
    if target not in {"temperature_c", "precipitation_mm"}:
        raise ValueError(f"Unsupported weather target: {target}")
    frame = _expand_target(
        engineered,
        target_column=target,
        horizons=horizons,
        allow_negative_target=target == "temperature_c",
        require_current_value=target == "temperature_c",
        maximum_output_rows=maximum_output_rows,
        horizon_bucket_edges=horizon_bucket_edges,
        samples_per_horizon_bucket=samples_per_horizon_bucket,
        random_state=random_state,
    )
    if not frame.empty:
        frame["current_value"] = pd.to_numeric(frame[target], errors="coerce")
        if target == "precipitation_mm":
            frame["target_occurrence"] = (
                pd.to_numeric(frame["target"], errors="coerce").fillna(0.0)
                > precipitation_occurrence_threshold_mm
            ).astype(float)
    return frame


def build_hourly_pm_training_frame(
    session: Session,
    *,
    parameter: str,
    horizons: Iterable[int],
    max_days: int,
    allow_negative_target: bool = False,
    auxiliary_parameters: Iterable[str] = (),
    maximum_output_rows: int | None = None,
    maximum_rows: int | None = None,
    horizon_bucket_edges: Iterable[int] | None = None,
    samples_per_horizon_bucket: int | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    selected_auxiliary = tuple(
        code for code in dict.fromkeys(str(value) for value in auxiliary_parameters)
        if code != parameter
    )
    base = _merge_weather(
        _air_frame(session, parameter, max_days),
        _weather_frame(session, max_days),
    )
    base = _merge_auxiliary_air(
        base,
        {
            code: _air_frame(session, code, max_days)
            for code in selected_auxiliary
        },
    )
    base = _engineer(base)
    frame = _expand_target(
        base,
        target_column="value",
        horizons=horizons,
        allow_negative_target=allow_negative_target,
        require_current_value=True,
        maximum_output_rows=maximum_output_rows,
        horizon_bucket_edges=horizon_bucket_edges,
        samples_per_horizon_bucket=samples_per_horizon_bucket,
        random_state=random_state,
    )
    if not frame.empty:
        frame["current_value"] = pd.to_numeric(frame["value"], errors="coerce")
    for column in [
        *PM_PREDICTED_WEATHER_COLUMNS,
        *auxiliary_air_feature_columns(selected_auxiliary),
    ]:
        if column not in frame.columns:
            frame[column] = np.nan
    return frame


def build_hourly_pm_training_frame_from_bundle(
    bundle: dict[str, Any],
    *,
    parameter: str,
    horizons: Iterable[int],
    max_days: int,
    allow_negative_target: bool = False,
    auxiliary_parameters: Iterable[str] = (),
    maximum_output_rows: int | None = None,
    maximum_rows: int | None = None,
    horizon_bucket_edges: Iterable[int] | None = None,
    samples_per_horizon_bucket: int | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    selected_auxiliary = tuple(
        code for code in dict.fromkeys(str(value) for value in auxiliary_parameters)
        if code != parameter
    )
    base = _merge_weather(
        _bundle_air_frame(bundle, parameter, max_days),
        _bundle_weather_frame(bundle, max_days),
    )
    base = _merge_auxiliary_air(
        base,
        {
            code: _bundle_air_frame(bundle, code, max_days)
            for code in selected_auxiliary
        },
    )
    base = _engineer(base)
    frame = _expand_target(
        base,
        target_column="value",
        horizons=horizons,
        allow_negative_target=allow_negative_target,
        require_current_value=True,
        maximum_output_rows=maximum_output_rows,
        horizon_bucket_edges=horizon_bucket_edges,
        samples_per_horizon_bucket=samples_per_horizon_bucket,
        random_state=random_state,
    )
    if not frame.empty:
        frame["current_value"] = pd.to_numeric(frame["value"], errors="coerce")
    for column in [
        *PM_PREDICTED_WEATHER_COLUMNS,
        *auxiliary_air_feature_columns(selected_auxiliary),
    ]:
        if column not in frame.columns:
            frame[column] = np.nan
    return frame


def _latest_rows_at_or_before(
    frame: pd.DataFrame,
    *,
    origin_time: datetime,
    required_value: str,
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    origin = pd.Timestamp(ensure_utc(origin_time))
    selected = frame[pd.to_datetime(frame["measurement_time"], utc=True) <= origin].copy()
    selected = selected.dropna(subset=[required_value])
    if selected.empty:
        return selected
    selected = (
        selected.sort_values("measurement_time")
        .groupby("air_station_id", sort=False, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )
    selected["source_measurement_time"] = selected["measurement_time"]
    selected["measurement_time"] = origin
    return selected


def expand_prediction_horizons(
    base_rows: pd.DataFrame,
    *,
    horizons: Iterable[int] | None = None,
    time_contract: ForecastTimeContract | None = None,
) -> pd.DataFrame:
    """Expand one causal origin into model and serving horizons.

    Training frames use ``horizons`` directly.  Serving prediction uses a
    ``ForecastTimeContract`` so the model can receive h5..h52 while the API and
    dashboard consistently expose lead 1..48 from the next future full hour.
    """

    chunks: list[pd.DataFrame] = []
    if time_contract is not None:
        for point in time_contract.points:
            chunk = _add_horizon_features(
                base_rows, int(point.model_horizon_hours)
            )
            chunk["model_horizon_hours"] = int(point.model_horizon_hours)
            chunk["serving_lead_hours"] = int(point.serving_lead_hours)
            chunk["serving_anchor_time"] = pd.Timestamp(
                time_contract.serving_anchor_time
            )
            chunk["source_age_hours"] = float(time_contract.source_age_hours)
            chunk["source_delay_to_anchor_hours"] = int(
                time_contract.source_delay_to_anchor_hours
            )
            # Build the target timestamp from the contract rather than relying
            # on a second independent calculation.
            chunk["target_time"] = pd.Timestamp(point.target_time)
            chunk = _cyclic_time(chunk, "target_time", prefix="target")
            chunks.append(chunk)
    else:
        chunks = [
            _add_horizon_features(base_rows, int(horizon))
            for horizon in (horizons or ())
        ]

    if not chunks:
        return pd.DataFrame()
    return pd.concat(chunks, ignore_index=True).sort_values(
        ["target_time", "air_station_id"]
    ).reset_index(drop=True)


def build_weather_prediction_rows(
    session: Session,
    *,
    origin_time: datetime,
    horizons: Iterable[int] | None = None,
    time_contract: ForecastTimeContract | None = None,
    max_days: int = 7,
) -> pd.DataFrame:
    weather = _attach_coordinates(_weather_frame(session, max_days), _coordinates_from_session(session))
    engineered = _engineer_weather(_normalise_hourly_weather(weather))
    base = _latest_rows_at_or_before(
        engineered,
        origin_time=origin_time,
        required_value="temperature_c",
    )
    return expand_prediction_horizons(
        base, horizons=horizons, time_contract=time_contract
    )


def build_pm_prediction_rows(
    session: Session,
    *,
    parameter: str,
    origin_time: datetime,
    horizons: Iterable[int] | None = None,
    time_contract: ForecastTimeContract | None = None,
    max_days: int = 7,
    auxiliary_parameters: Iterable[str] = (),
) -> pd.DataFrame:
    selected_auxiliary = tuple(
        code for code in dict.fromkeys(str(value) for value in auxiliary_parameters)
        if code != parameter
    )
    base = _merge_weather(
        _air_frame(session, parameter, max_days),
        _weather_frame(session, max_days),
    )
    base = _merge_auxiliary_air(
        base,
        {
            code: _air_frame(session, code, max_days)
            for code in selected_auxiliary
        },
    )
    engineered = _engineer(base)
    base = _latest_rows_at_or_before(
        engineered,
        origin_time=origin_time,
        required_value="value",
    )
    expanded = expand_prediction_horizons(
        base, horizons=horizons, time_contract=time_contract
    )
    for column in [
        *PM_PREDICTED_WEATHER_COLUMNS,
        *auxiliary_air_feature_columns(selected_auxiliary),
    ]:
        if column not in expanded.columns:
            expanded[column] = np.nan
    return expanded


def latest_common_origin_time(
    session: Session,
    *,
    parameters: Iterable[str] = ("PM10", "PM2.5"),
) -> datetime | None:
    maxima: list[pd.Timestamp] = []
    for parameter in parameters:
        frame = _air_frame(session, parameter, 14)
        if frame.empty:
            continue
        maximum = pd.to_datetime(frame["measurement_time"], utc=True, errors="coerce").max()
        if pd.notna(maximum):
            maxima.append(maximum)
    weather = _weather_frame(session, 14)
    if not weather.empty:
        maximum = pd.to_datetime(weather["measurement_time"], utc=True, errors="coerce").max()
        if pd.notna(maximum):
            maxima.append(maximum)
    if not maxima:
        return None
    # The oldest latest source time is the last hour for which every available
    # source can build a causally valid feature vector.
    origin = min(maxima).floor("h")
    return origin.to_pydatetime().astimezone(UTC)
