from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import (
    AirMeasurement,
    AirSensor,
    AirStation,
    CollectionError,
    DataQualityFlag,
    Forecast,
    ForecastResult,
    ModelVersion,
    PublicationOutbox,
    PublishedSnapshot,
    WeatherMeasurement,
    WeatherStation,
)
from smog_ai.database.repository import as_utc, get_application_state
from smog_ai.time_utils import utc_now


def _count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def build_report(session: Session, config: AppConfig) -> dict[str, Any]:
    air_range = session.execute(select(func.min(AirMeasurement.measurement_time), func.max(AirMeasurement.measurement_time))).one()
    weather_range = session.execute(select(func.min(WeatherMeasurement.measurement_time), func.max(WeatherMeasurement.measurement_time))).one()
    fresh_cutoff = utc_now() - timedelta(hours=config.quality.stale_air_hours)
    current_stations = int(
        session.scalar(
            select(func.count(func.distinct(AirMeasurement.air_station_id))).where(
                AirMeasurement.measurement_time >= fresh_cutoff
            )
        )
        or 0
    )
    stations = _count(session, AirStation)
    verified = int(
        session.scalar(select(func.count()).select_from(ForecastResult).where(ForecastResult.verification_status == "verified"))
        or 0
    )
    return {
        "generated_at": utc_now().isoformat(),
        "air_stations": stations,
        "air_sensors": _count(session, AirSensor),
        "air_measurements": _count(session, AirMeasurement),
        "weather_stations": _count(session, WeatherStation),
        "weather_measurements": _count(session, WeatherMeasurement),
        "data_range": {
            "air_start": as_utc(air_range[0]).isoformat() if air_range[0] else None,
            "air_end": as_utc(air_range[1]).isoformat() if air_range[1] else None,
            "weather_start": as_utc(weather_range[0]).isoformat() if weather_range[0] else None,
            "weather_end": as_utc(weather_range[1]).isoformat() if weather_range[1] else None,
        },
        "current_air_stations": current_stations,
        "current_air_stations_percent": (100.0 * current_stations / stations) if stations else 0.0,
        "open_quality_flags": int(
            session.scalar(select(func.count()).select_from(DataQualityFlag).where(DataQualityFlag.resolved_at.is_(None))) or 0
        ),
        "recent_collection_errors": int(
            session.scalar(
                select(func.count()).select_from(CollectionError).where(
                    CollectionError.occurred_at >= utc_now() - timedelta(days=1)
                )
            )
            or 0
        ),
        "forecasts": _count(session, Forecast),
        "verified_forecasts": verified,
        "active_models": int(
            session.scalar(select(func.count()).select_from(ModelVersion).where(ModelVersion.active.is_(True))) or 0
        ),
        "outbox_pending": int(
            session.scalar(
                select(func.count()).select_from(PublicationOutbox).where(
                    PublicationOutbox.status.in_(["pending", "failed", "sending"])
                )
            )
            or 0
        ),
        "published_snapshots": _count(session, PublishedSnapshot),
        "last_gios_success_at": get_application_state(session, "last_gios_success_at"),
        "last_imgw_success_at": get_application_state(session, "last_imgw_success_at"),
        "last_forecast_at": get_application_state(session, "last_forecast_at"),
        "last_publication_at": get_application_state(session, "last_publication_at"),
    }
