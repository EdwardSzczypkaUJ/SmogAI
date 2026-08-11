from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.data_validation import validate_frame
from smog_ai.database.models import (
    AirMeasurement,
    AirStation,
    DataQualityFlag,
    Forecast,
    ForecastResult,
    ModelVersion,
    StationMatch,
    WeatherMeasurement,
    WeatherStation,
)
from smog_ai.database.repository import enqueue_publication, set_application_state
from smog_ai.domain import StageStats
from smog_ai.publishing.schema import SnapshotMetadata, SnapshotPayload
from smog_ai.time_utils import ensure_utc, utc_now


@dataclass(frozen=True, slots=True)
class SnapshotBuildResult:
    payload: SnapshotPayload
    path: Path
    checksum: str
    publication_id: str
    stats: StageStats


def _iso(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def _canonical(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def calculate_payload_checksum(payload: dict[str, Any]) -> str:
    core = copy.deepcopy(payload)
    core.setdefault("metadata", {})["checksum"] = ""
    core["metadata"]["publication_id"] = ""
    return hashlib.sha256(_canonical(core)).hexdigest()


def _latest_air(session: Session, station_id: int, parameter: str) -> AirMeasurement | None:
    return session.scalar(
        select(AirMeasurement)
        .where(
            AirMeasurement.air_station_id == station_id,
            AirMeasurement.parameter == parameter,
            AirMeasurement.value.is_not(None),
        )
        .order_by(AirMeasurement.measurement_time.desc())
        .limit(1)
    )


def _latest_weather(session: Session, station_id: int) -> tuple[WeatherStation | None, WeatherMeasurement | None, float | None]:
    match = session.scalar(select(StationMatch).where(StationMatch.air_station_id == station_id))
    if match is None:
        return None, None, None
    station = session.get(WeatherStation, match.weather_station_id)
    measurement = session.scalar(
        select(WeatherMeasurement)
        .where(WeatherMeasurement.weather_station_id == match.weather_station_id)
        .order_by(WeatherMeasurement.measurement_time.desc())
        .limit(1)
    )
    return station, measurement, match.distance_km


def _station_rows(session: Session, config: AppConfig) -> list[dict[str, Any]]:
    registry = create_air_parameter_registry(config)
    snapshot_parameters = list(
        dict.fromkeys(
            [
                *registry.collection_codes,
                *registry.forecast_codes,
                *registry.spatial_codes,
            ]
        )
    )
    rows: list[dict[str, Any]] = []
    for station in session.scalars(select(AirStation).order_by(AirStation.station_name)).all():
        current: dict[str, Any] = {}
        for parameter in snapshot_parameters:
            measurement = _latest_air(session, station.id, parameter)
            current[parameter] = (
                {
                    "value": measurement.value,
                    "measurement_time": _iso(measurement.measurement_time),
                    "is_valid": measurement.is_valid,
                    "unit": measurement.unit,
                    "source_status": measurement.source_status,
                }
                if measurement
                else None
            )
        weather_station, weather, distance = _latest_weather(session, station.id)
        weather_data = None
        if weather is not None:
            weather_data = {
                "station_id": weather_station.source_id if weather_station else None,
                "station_name": weather_station.station_name if weather_station else None,
                "distance_km": distance,
                "measurement_time": _iso(weather.measurement_time),
                "temperature_c": weather.temperature_c,
                "humidity_percent": weather.humidity_percent,
                "pressure_hpa": weather.pressure_hpa,
                "precipitation_mm": weather.precipitation_mm,
                "precipitation_accumulation_period_hours": (
                    weather.precipitation_accumulation_period_hours
                ),
                "wind_speed_mps": weather.wind_speed_mps,
                "wind_direction_deg": weather.wind_direction_deg,
            }
        flags = session.scalar(
            select(func.count()).select_from(DataQualityFlag).where(
                DataQualityFlag.entity_type == "air_station",
                DataQualityFlag.entity_id == str(station.id),
                DataQualityFlag.resolved_at.is_(None),
            )
        ) or 0
        rows.append(
            {
                "station_id": station.id,
                "source_id": station.source_id,
                "station_name": station.station_name,
                "city_name": station.city_name,
                "latitude": station.latitude,
                "longitude": station.longitude,
                "measurements": current,
                "weather": weather_data,
                "open_quality_flags": int(flags),
            }
        )
    return rows


def _forecast_value_is_valid(
    config: AppConfig,
    parameter: str,
    value: float | None,
) -> bool:
    if value is None:
        return False
    numeric = float(value)
    if not math.isfinite(numeric):
        return False
    if parameter == "temperature_c":
        return -90.0 <= numeric <= 65.0
    if parameter == "precipitation_probability":
        return 0.0 <= numeric <= 1.0
    if parameter == "precipitation_mm":
        return numeric >= 0.0

    definition = create_air_parameter_registry(config).get(parameter)
    if definition is None:
        return True
    if not definition.allow_negative and numeric < 0.0:
        return False
    if definition.valid_min is not None and numeric < definition.valid_min:
        return False
    if definition.valid_max is not None and numeric > definition.valid_max:
        return False
    return True


def _forecast_rows(
    session: Session,
    config: AppConfig,
    history_days: int,
) -> list[dict[str, Any]]:
    cutoff = utc_now() - timedelta(days=history_days)
    rows = session.execute(
        select(Forecast, ForecastResult, ModelVersion)
        .outerjoin(ForecastResult, ForecastResult.forecast_id == Forecast.id)
        .outerjoin(ModelVersion, ModelVersion.id == Forecast.model_version_id)
        .where(
            Forecast.forecast_created_at >= cutoff,
            # Keep legacy bad rows in SQLite for audit, but never expose them as
            # bona-fide forecasts.  They were generated from stale source data by
            # pre-HF2 builds after their target time had already passed.
            Forecast.target_time > Forecast.forecast_created_at,
            Forecast.forecast_origin_time <= Forecast.forecast_created_at,
        )
        .order_by(Forecast.target_time.desc())
    ).all()
    output: list[dict[str, Any]] = []
    for forecast, result, model in rows:
        feature_payload = dict(forecast.features_json or {})
        if not _forecast_value_is_valid(
            config,
            str(forecast.parameter),
            forecast.predicted_value,
        ):
            continue
        output.append(
            {
                "forecast_id": forecast.id,
                "station_id": forecast.air_station_id,
                "parameter": forecast.parameter,
                "forecast_created_at": _iso(forecast.forecast_created_at),
                "origin_time": _iso(forecast.forecast_origin_time),
                "target_time": _iso(forecast.target_time),
                # Public/API horizon is the serving lead 1..48.  The model
                # horizon is separately retained for audit and reproducibility.
                "horizon_hours": forecast.forecast_horizon,
                "serving_lead_hours": forecast.forecast_horizon,
                "model_horizon_hours": feature_payload.get(
                    "model_horizon_hours", forecast.forecast_horizon
                ),
                "serving_anchor_time": feature_payload.get(
                    "serving_anchor_time"
                ),
                "source_age_hours": feature_payload.get("source_age_hours"),
                "source_delay_to_anchor_hours": feature_payload.get(
                    "source_delay_to_anchor_hours"
                ),
                "predicted_value": forecast.predicted_value,
                "model_version": (
                    model.semantic_version if model else forecast.model_version_id
                ),
                "algorithm": model.algorithm if model else None,
                "verification_status": (
                    result.verification_status if result else "pending"
                ),
                "actual_value": result.actual_value if result else None,
                "signed_error": result.signed_error if result else None,
                "absolute_error": result.absolute_error if result else None,
                "verified_at": _iso(result.verified_at) if result else None,
            }
        )
    return output


def _metric_rows(forecasts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str], list[float]] = {}
    squared: dict[tuple[str, int, str], list[float]] = {}
    for row in forecasts:
        if row["absolute_error"] is None:
            continue
        key = (str(row.get("model_version") or "unknown"), int(row["horizon_hours"]), row["parameter"])
        grouped.setdefault(key, []).append(float(row["absolute_error"]))
        squared.setdefault(key, []).append(float(row["signed_error"]) ** 2)
    output = []
    for key, errors in grouped.items():
        output.append(
            {
                "model_version": key[0],
                "horizon_hours": key[1],
                "parameter": key[2],
                "count": len(errors),
                "mae": sum(errors) / len(errors),
                "rmse": (sum(squared[key]) / len(squared[key])) ** 0.5,
            }
        )
    return sorted(output, key=lambda item: (item["parameter"], item["horizon_hours"], item["mae"]))


def build_snapshot(session: Session, config: AppConfig) -> SnapshotBuildResult:
    generated = utc_now()
    stations = _station_rows(session, config)
    forecasts = _forecast_rows(
        session,
        config,
        config.publication.snapshot_history_days,
    )
    metrics = _metric_rows(forecasts)
    times: list[datetime] = []
    for station in stations:
        for measurement in station["measurements"].values():
            if measurement and measurement["measurement_time"]:
                times.append(datetime.fromisoformat(measurement["measurement_time"].replace("Z", "+00:00")))
    active_versions = session.scalars(select(ModelVersion.semantic_version).where(ModelVersion.active.is_(True))).all()
    spatial: dict[str, Any] = {}
    if config.object_storage.enabled and config.spatial.enabled:
        try:
            repository = create_artifact_repository(config)
            pointer = repository.get_json(repository.layout.latest_spatial_pointer)
            manifest = repository.get_json(str(pointer["manifest_key"]))
            spatial = {
                "available": True,
                "pointer": pointer,
                "manifest": manifest,
                "note": "Powierzchnie obliczono lokalnie; serwer wyłącznie je odczytuje i prezentuje.",
            }
        except Exception as exc:
            spatial = {
                "available": False,
                "warning": str(exc),
            }
    station_columns = [
        "station_id", "source_id", "station_name", "city_name", "latitude",
        "longitude", "measurements", "weather", "open_quality_flags",
    ]
    forecast_columns = [
        "forecast_id", "station_id", "parameter", "forecast_created_at",
        "origin_time", "target_time", "horizon_hours", "predicted_value",
        "model_version", "algorithm", "verification_status", "actual_value",
        "signed_error", "absolute_error", "verified_at",
    ]
    _, station_validation = validate_frame(
        pd.DataFrame(stations, columns=station_columns),
        "snapshot_stations",
        config,
        context={"generated_at": generated.isoformat(), "snapshot_part": "stations"},
    )
    _, forecast_validation = validate_frame(
        pd.DataFrame(forecasts, columns=forecast_columns),
        "snapshot_forecasts",
        config,
        context={"generated_at": generated.isoformat(), "snapshot_part": "forecasts"},
    )

    quality_summary = {
        "open_flags": int(
            session.scalar(select(func.count()).select_from(DataQualityFlag).where(DataQualityFlag.resolved_at.is_(None))) or 0
        ),
        "stations": len(stations),
        "forecasts": len(forecasts),
        "verified_forecasts": sum(1 for row in forecasts if row["verification_status"] == "verified"),
        "spatial_surfaces": len((spatial.get("manifest") or {}).get("surfaces", [])),
        "dataframe_validation": {
            "stations": {
                "valid": station_validation.valid,
                "engine": station_validation.engine,
                "failure_count": station_validation.failure_count,
            },
            "forecasts": {
                "valid": forecast_validation.valid,
                "engine": forecast_validation.engine,
                "failure_count": forecast_validation.failure_count,
            },
        },
    }
    registry = create_air_parameter_registry(config)
    published_air_parameters = list(
        dict.fromkeys(
            [
                *registry.collection_codes,
                *registry.forecast_codes,
                *registry.spatial_codes,
            ]
        )
    )
    draft = {
        "metadata": {
            "publication_id": "",
            "schema_version": "1.1",
            "generated_at": _iso(generated),
            "data_start": _iso(min(times)) if times else None,
            "data_end": _iso(max(times)) if times else None,
            "model_version": ",".join(sorted(active_versions)) if active_versions else None,
            "record_count": len(stations) + len(forecasts) + len(metrics) + len((spatial.get("manifest") or {}).get("surfaces", [])),
            "checksum": "",
            "source_host_id": config.source_host_id,
        },
        "stations": stations,
        "forecasts": forecasts,
        "metrics": metrics,
        "quality_summary": quality_summary,
        "spatial": spatial,
        "air_parameter_catalog": registry.public_catalog(
            published_air_parameters
        ),
    }
    checksum = calculate_payload_checksum(draft)
    publication_id = f"{config.source_host_id}-{generated.strftime('%Y%m%dT%H%M%SZ')}-{checksum[:16]}"
    draft["metadata"]["publication_id"] = publication_id
    draft["metadata"]["checksum"] = checksum
    payload = SnapshotPayload.model_validate(draft)
    config.paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
    path = config.paths.snapshots_dir / f"dashboard_snapshot_{publication_id}.json.gz"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=config.publication.gzip_compresslevel, mtime=0) as compressed:
            compressed.write(_canonical(payload.model_dump(mode="json")))
    temporary.replace(path)
    enqueue_publication(
        session,
        publication_id=publication_id,
        payload_path=path,
        payload_type="application/json+gzip",
        checksum=checksum,
    )
    set_application_state(session, "last_snapshot_at", generated.isoformat())
    stats = StageStats(inserted=1, details={"publication_id": publication_id, "path": str(path), "record_count": draft["metadata"]["record_count"]})
    return SnapshotBuildResult(payload, path, checksum, publication_id, stats)

def snapshot_has_source_data(session: Session) -> bool:
    """Return True only when a dashboard snapshot would contain real air data."""
    air_rows = session.scalar(
        select(func.count()).select_from(AirMeasurement).where(AirMeasurement.value.is_not(None))
    ) or 0
    forecast_rows = session.scalar(select(func.count()).select_from(Forecast)) or 0
    return int(air_rows) > 0 or int(forecast_rows) > 0


def build_snapshot_stage(session: Session, config: AppConfig) -> StageStats:
    """Pipeline wrapper that prevents publication of an empty first-run snapshot."""
    if not snapshot_has_source_data(session):
        return StageStats(
            skipped=1,
            warnings=1,
            details={"reason": "no_air_measurements_or_forecasts"},
        )
    return build_snapshot(session, config).stats

