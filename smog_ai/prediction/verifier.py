from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import (
    AirMeasurement,
    Forecast,
    ForecastResult,
    StationMatch,
    WeatherMeasurement,
)
from smog_ai.database.repository import as_utc, ensure_forecast_result, set_application_state
from smog_ai.domain import StageStats
from smog_ai.time_utils import utc_now

WEATHER_PARAMETERS = {
    "temperature_c",
    "precipitation_mm",
    "precipitation_probability",
}


def _nearest_air_actual(
    session: Session,
    forecast: Forecast,
    *,
    target,
    tolerance: timedelta,
) -> tuple[float, object] | None:
    measurement = session.scalar(
        select(AirMeasurement)
        .where(
            AirMeasurement.air_station_id == forecast.air_station_id,
            AirMeasurement.parameter == forecast.parameter,
            AirMeasurement.value.is_not(None),
            AirMeasurement.is_valid.is_(True),
            AirMeasurement.measurement_time >= target - tolerance,
            AirMeasurement.measurement_time <= target + tolerance,
        )
        .order_by(
            func.abs(
                func.julianday(AirMeasurement.measurement_time)
                - func.julianday(target)
            )
        )
        .limit(1)
    )
    if measurement is None:
        return None
    return float(measurement.value), measurement.measurement_time


def _nearest_weather_actual(
    session: Session,
    forecast: Forecast,
    config: AppConfig,
    *,
    target,
    tolerance: timedelta,
) -> tuple[float, object] | None:
    match = session.scalar(
        select(StationMatch).where(
            StationMatch.air_station_id == forecast.air_station_id
        )
    )
    if match is None:
        return None
    measurement = session.scalar(
        select(WeatherMeasurement)
        .where(
            WeatherMeasurement.weather_station_id == match.weather_station_id,
            WeatherMeasurement.is_valid.is_(True),
            WeatherMeasurement.measurement_time >= target - tolerance,
            WeatherMeasurement.measurement_time <= target + tolerance,
        )
        .order_by(
            func.abs(
                func.julianday(WeatherMeasurement.measurement_time)
                - func.julianday(target)
            )
        )
        .limit(1)
    )
    if measurement is None:
        return None
    if forecast.parameter == "temperature_c":
        value = measurement.temperature_c
    elif forecast.parameter == "precipitation_mm":
        value = measurement.precipitation_mm
    elif forecast.parameter == "precipitation_probability":
        amount = measurement.precipitation_mm
        value = (
            None
            if amount is None
            else float(
                amount
                > config.hourly_forecasting.precipitation.occurrence_threshold_mm
            )
        )
    else:
        return None
    if value is None:
        return None
    return float(value), measurement.measurement_time


def verify_forecasts(session: Session, config: AppConfig) -> StageStats:
    stats = StageStats()
    now = utc_now()
    forecasts = session.scalars(
        select(Forecast)
        .outerjoin(ForecastResult, ForecastResult.forecast_id == Forecast.id)
        .where(
            Forecast.target_time <= now,
            (ForecastResult.id.is_(None))
            | (ForecastResult.verification_status == "pending"),
        )
        .order_by(Forecast.target_time)
    ).all()
    tolerance = timedelta(minutes=config.quality.verify_tolerance_minutes)
    verified_by_parameter: dict[str, int] = {}
    awaiting_by_parameter: dict[str, int] = {}
    for forecast in forecasts:
        target = as_utc(forecast.target_time)
        if forecast.parameter in WEATHER_PARAMETERS:
            actual_match = _nearest_weather_actual(
                session,
                forecast,
                config,
                target=target,
                tolerance=tolerance,
            )
        else:
            actual_match = _nearest_air_actual(
                session,
                forecast,
                target=target,
                tolerance=tolerance,
            )
        result = ensure_forecast_result(session, forecast.id)
        if actual_match is None:
            result.verification_status = "awaiting_measurement"
            stats.skipped += 1
            awaiting_by_parameter[forecast.parameter] = (
                awaiting_by_parameter.get(forecast.parameter, 0) + 1
            )
            continue
        actual, matched_time = actual_match
        signed = float(forecast.predicted_value) - actual
        result.actual_value = actual
        result.signed_error = signed
        result.absolute_error = abs(signed)
        result.squared_error = signed * signed
        result.verified_at = now
        result.verification_status = "verified"
        result.matched_measurement_time = matched_time
        stats.inserted += 1
        verified_by_parameter[forecast.parameter] = (
            verified_by_parameter.get(forecast.parameter, 0) + 1
        )
    if stats.inserted:
        set_application_state(session, "last_verification_at", now.isoformat())
    stats.downloaded = len(forecasts)
    stats.details = {
        "verified_by_parameter": verified_by_parameter,
        "awaiting_by_parameter": awaiting_by_parameter,
    }
    return stats
