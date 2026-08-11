from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import WeatherMeasurement


EMPTY_MEASUREMENT_STATS: dict[str, Any] = {
    "rows": 0,
    "start": None,
    "end": None,
    "stations": 0,
    "unique_hours": 0,
}


def empty_measurement_stats() -> dict[str, Any]:
    """Return a fresh, total measurement-statistics mapping.

    The parameter catalog is consumed by PowerShell 5.1, FastAPI and
    Streamlit. Every parameter therefore exposes the same keys even before
    the first observation has been collected.
    """

    return dict(EMPTY_MEASUREMENT_STATS)


WEATHER_PARAMETER_DEFINITIONS: dict[str, dict[str, Any]] = {
    "temperature_c": {
        "display_name": "Temperatura powietrza",
        "canonical_unit": "°C",
        "cadence_hours": 1,
        "column": WeatherMeasurement.temperature_c,
    },
    "humidity_percent": {
        "display_name": "Wilgotność względna",
        "canonical_unit": "%",
        "cadence_hours": 1,
        "column": WeatherMeasurement.humidity_percent,
    },
    "pressure_hpa": {
        "display_name": "Ciśnienie",
        "canonical_unit": "hPa",
        "cadence_hours": 1,
        "column": WeatherMeasurement.pressure_hpa,
    },
    "precipitation_mm": {
        "display_name": "Suma opadu",
        "canonical_unit": "mm",
        "cadence_hours": 6,
        "column": WeatherMeasurement.precipitation_mm,
    },
    "wind_speed_mps": {
        "display_name": "Prędkość wiatru",
        "canonical_unit": "m/s",
        "cadence_hours": 1,
        "column": WeatherMeasurement.wind_speed_mps,
    },
    "wind_direction_deg": {
        "display_name": "Kierunek wiatru",
        "canonical_unit": "°",
        "cadence_hours": 1,
        "column": WeatherMeasurement.wind_direction_deg,
    },
}


def _measurement_stats(session: Session, column: Any) -> dict[str, Any]:
    count, start, end, stations, hours = session.execute(
        select(
            func.count(WeatherMeasurement.id),
            func.min(WeatherMeasurement.measurement_time),
            func.max(WeatherMeasurement.measurement_time),
            func.count(func.distinct(WeatherMeasurement.weather_station_id)),
            func.count(func.distinct(WeatherMeasurement.measurement_time)),
        ).where(
            WeatherMeasurement.is_valid.is_(True),
            column.is_not(None),
        )
    ).one()
    return {
        "rows": int(count or 0),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "stations": int(stations or 0),
        "unique_hours": int(hours or 0),
    }


def build_weather_parameter_catalog(
    session: Session,
    config: AppConfig,
    *,
    active_models: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Describe IMGW variables separately from the GIOŚ pollutant registry.

    Temperature and precipitation are genuine forecast targets in the hourly
    platform, while humidity, pressure and wind are currently auxiliary model
    features.  They intentionally do not belong to ``AirParameterRegistry``
    because they are weather variables, not GIOŚ air pollutants.
    """

    models = active_models or {}
    targets = set(config.hourly_forecasting.targets)
    spatial_targets = set(config.hourly_forecasting.spatial_targets)
    result: dict[str, dict[str, Any]] = {}

    for code, definition in WEATHER_PARAMETER_DEFINITIONS.items():
        result[code] = {
            "source": "IMGW",
            "display_name": definition["display_name"],
            "canonical_unit": definition["canonical_unit"],
            "cadence_hours": definition["cadence_hours"],
            "collect_current": True,
            "historical_backfill": bool(config.imgw_archive.enabled),
            "auxiliary_feature": True,
            "forecast_target": code in targets,
            "spatial_surface": code in spatial_targets,
            "measurements": _measurement_stats(
                session,
                definition["column"],
            ),
            "active_model": models.get(code),
        }

    result["precipitation_probability"] = {
        "source": "MODEL_DERIVED",
        "display_name": "Prawdopodobieństwo opadu",
        "canonical_unit": "%",
        "cadence_hours": 1,
        "collect_current": False,
        "historical_backfill": False,
        "auxiliary_feature": False,
        "forecast_target": False,
        "spatial_surface": "precipitation_probability" in spatial_targets,
        "measurements": empty_measurement_stats(),
        "active_model": models.get("precipitation_mm"),
    }
    return result
