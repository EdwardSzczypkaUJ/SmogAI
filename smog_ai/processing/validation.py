from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.config import AppConfig
from smog_ai.database.models import AirMeasurement, AirStation, WeatherMeasurement, WeatherStation
from smog_ai.database.repository import add_quality_flag, as_utc
from smog_ai.domain import StageStats
from smog_ai.time_utils import utc_now


def _flag(
    session: Session,
    stats: StageStats,
    *,
    entity_type: str,
    entity_id: str,
    code: str,
    severity: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if add_quality_flag(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        flag_code=code,
        severity=severity,
        message=message,
        metadata=metadata,
    ):
        stats.inserted += 1
    stats.warnings += 1 if severity != "error" else 0
    stats.errors += 1 if severity == "error" else 0


def validate_coordinates(session: Session, stats: StageStats) -> None:
    for station in session.scalars(select(AirStation)).all():
        if station.latitude is None or station.longitude is None:
            _flag(
                session,
                stats,
                entity_type="air_station",
                entity_id=str(station.id),
                code="MISSING_COORDINATES",
                severity="warning",
                message="Air station has no coordinates.",
            )
        elif not (-90 <= station.latitude <= 90 and -180 <= station.longitude <= 180):
            _flag(
                session,
                stats,
                entity_type="air_station",
                entity_id=str(station.id),
                code="INVALID_COORDINATES",
                severity="error",
                message="Air station coordinates are outside valid geographic ranges.",
            )
    for station in session.scalars(select(WeatherStation)).all():
        if station.latitude is None or station.longitude is None:
            _flag(
                session,
                stats,
                entity_type="weather_station",
                entity_id=str(station.id),
                code="MISSING_COORDINATES",
                severity="warning",
                message="Weather station has no official or fallback coordinates.",
            )
        elif not (-90 <= station.latitude <= 90 and -180 <= station.longitude <= 180):
            _flag(
                session,
                stats,
                entity_type="weather_station",
                entity_id=str(station.id),
                code="INVALID_COORDINATES",
                severity="error",
                message="Weather station coordinates are outside valid geographic ranges.",
            )


def validate_air_measurements(session: Session, config: AppConfig, stats: StageStats) -> None:
    registry = create_air_parameter_registry(config)
    cutoff = utc_now() - timedelta(days=7)
    rows = session.scalars(
        select(AirMeasurement)
        .where(AirMeasurement.measurement_time >= cutoff)
        .order_by(AirMeasurement.air_sensor_id, AirMeasurement.measurement_time)
    ).all()
    previous: dict[int, AirMeasurement] = {}
    for row in rows:
        entity_id = str(row.id)
        if row.value is None:
            row.is_valid = False
            _flag(
                session,
                stats,
                entity_type="air_measurement",
                entity_id=entity_id,
                code="MISSING_VALUE",
                severity="warning",
                message="Measurement value is missing.",
            )
        else:
            definition = registry.get(row.parameter)
            if definition is not None:
                below_minimum = (
                    definition.valid_min is not None
                    and row.value < definition.valid_min
                )
                above_maximum = (
                    definition.valid_max is not None
                    and row.value > definition.valid_max
                )
                if (below_minimum and not definition.allow_negative) or above_maximum:
                    row.is_valid = False
                    _flag(
                        session,
                        stats,
                        entity_type="air_measurement",
                        entity_id=entity_id,
                        code="AIR_PARAMETER_OUT_OF_RANGE",
                        severity="error",
                        message=(
                            f"{row.parameter} value {row.value} is outside "
                            f"configured range {definition.valid_min}.."
                            f"{definition.valid_max}."
                        ),
                    )
        earlier = previous.get(row.air_sensor_id)
        if earlier and row.value is not None and earlier.value is not None:
            definition = registry.get(row.parameter)
            threshold = (
                definition.spike_absolute
                if definition is not None and definition.spike_absolute is not None
                else config.quality.spike_absolute_pm10
            )
            if definition is not None and definition.valid_max is not None:
                threshold = min(threshold, max(1.0, definition.valid_max / 4.0))
            difference = abs(row.value - earlier.value)
            if difference > threshold:
                _flag(
                    session,
                    stats,
                    entity_type="air_measurement",
                    entity_id=entity_id,
                    code="SUDDEN_SPIKE",
                    severity="warning",
                    message=f"Absolute change {difference:.1f} exceeds configured threshold {threshold:.1f}.",
                    metadata={"previous_measurement_id": earlier.id, "difference": difference},
                )
        previous[row.air_sensor_id] = row

    latest_rows = session.execute(
        select(AirMeasurement.air_station_id, func.max(AirMeasurement.measurement_time)).group_by(
            AirMeasurement.air_station_id
        )
    ).all()
    stale_cutoff = utc_now() - timedelta(hours=config.quality.stale_air_hours)
    for station_id, latest in latest_rows:
        if latest is not None and as_utc(latest) < stale_cutoff:
            _flag(
                session,
                stats,
                entity_type="air_station",
                entity_id=str(station_id),
                code="STALE_AIR_STATION",
                severity="warning",
                message=f"Latest air measurement is older than {config.quality.stale_air_hours} hours.",
                metadata={"latest_measurement": as_utc(latest).isoformat()},
            )


def validate_weather_measurements(session: Session, config: AppConfig, stats: StageStats) -> None:
    cutoff = utc_now() - timedelta(days=7)
    rows = session.scalars(select(WeatherMeasurement).where(WeatherMeasurement.measurement_time >= cutoff)).all()
    for row in rows:
        entity_id = str(row.id)
        problems = False
        if row.humidity_percent is not None and not 0 <= row.humidity_percent <= 100:
            problems = True
            _flag(
                session,
                stats,
                entity_type="weather_measurement",
                entity_id=entity_id,
                code="HUMIDITY_OUT_OF_RANGE",
                severity="error",
                message=f"Relative humidity outside 0..100%: {row.humidity_percent}",
            )
        if row.wind_direction_deg is not None and not 0 <= row.wind_direction_deg <= 360:
            problems = True
            _flag(
                session,
                stats,
                entity_type="weather_measurement",
                entity_id=entity_id,
                code="WIND_DIRECTION_OUT_OF_RANGE",
                severity="error",
                message=f"Wind direction outside 0..360 degrees: {row.wind_direction_deg}",
            )
        if row.precipitation_mm is not None and row.precipitation_mm < 0:
            problems = True
            _flag(
                session,
                stats,
                entity_type="weather_measurement",
                entity_id=entity_id,
                code="NEGATIVE_PRECIPITATION",
                severity="error",
                message=f"Precipitation cannot be negative: {row.precipitation_mm}",
            )
        if problems:
            row.is_valid = False

    latest_rows = session.execute(
        select(WeatherMeasurement.weather_station_id, func.max(WeatherMeasurement.measurement_time)).group_by(
            WeatherMeasurement.weather_station_id
        )
    ).all()
    stale_cutoff = utc_now() - timedelta(hours=config.quality.stale_weather_hours)
    for station_id, latest in latest_rows:
        if latest is not None and as_utc(latest) < stale_cutoff:
            _flag(
                session,
                stats,
                entity_type="weather_station",
                entity_id=str(station_id),
                code="STALE_WEATHER_STATION",
                severity="warning",
                message=f"Latest weather measurement is older than {config.quality.stale_weather_hours} hours.",
                metadata={"latest_measurement": as_utc(latest).isoformat()},
            )


def validate_data(session: Session, config: AppConfig) -> StageStats:
    stats = StageStats()
    stats.downloaded = (
        session.scalar(select(func.count()).select_from(AirMeasurement)) or 0
    ) + (session.scalar(select(func.count()).select_from(WeatherMeasurement)) or 0)
    validate_coordinates(session, stats)
    validate_air_measurements(session, config, stats)
    validate_weather_measurements(session, config, stats)
    stats.details = {"quality_flags_created": stats.inserted}
    return stats
