from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.artifacts.repository import ArtifactRepository, canonical_json_bytes
from smog_ai.config import AppConfig
from smog_ai.data_validation import validate_frame
from smog_ai.database.models import AirStation, Forecast, ModelVersion
from smog_ai.domain import StageStats
from smog_ai.progress import ProgressReporter, WeightedStageProgress
from smog_ai.spatial.colors import rgba_for_values, unit_for
from smog_ai.spatial.contracts import SpatialGrid, SpatialSurface
from smog_ai.spatial.factory import create_spatial_interpolator
from smog_ai.spatial.grid import create_poland_grid, load_boundary_geojson
from smog_ai.time_utils import ensure_utc

SPATIAL_SCHEMA_VERSION = "1.2"


def _configured_surface_axes(config: AppConfig) -> tuple[list[str], list[int]]:
    if config.hourly_forecasting.enabled:
        return (
            [str(value) for value in config.hourly_forecasting.spatial_targets],
            [int(value) for value in config.hourly_forecasting.serving_horizons_hours],
        )
    return (
        [str(value) for value in config.training.parameters],
        [int(value) for value in config.training.horizons_hours],
    )


def _iso(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def _parameter_bounds(
    config: AppConfig,
    parameter: str,
) -> tuple[float | None, float | None]:
    if parameter == "temperature_c":
        return -90.0, 65.0
    if parameter == "precipitation_probability":
        return 0.0, 1.0
    if parameter == "precipitation_mm":
        return 0.0, None
    definition = create_air_parameter_registry(config).get(parameter)
    if definition is None:
        return None, None
    lower = definition.valid_min
    if lower is None and not definition.allow_negative:
        lower = 0.0
    return lower, definition.valid_max


def _bounded_value(config: AppConfig, parameter: str, value: float) -> float:
    lower, upper = _parameter_bounds(config, parameter)
    result = float(value)
    if lower is not None:
        result = max(float(lower), result)
    if upper is not None:
        result = min(float(upper), result)
    return result


def _bound_surface_values(
    frame: pd.DataFrame,
    *,
    config: AppConfig,
    parameter: str,
) -> pd.DataFrame:
    lower, upper = _parameter_bounds(config, parameter)
    if lower is None and upper is None:
        return frame
    output = frame.copy()
    values = pd.to_numeric(output["value"], errors="coerce").to_numpy(dtype=float)
    if lower is not None:
        values = np.maximum(values, float(lower))
    if upper is not None:
        values = np.minimum(values, float(upper))
    output["value"] = values
    rgba = rgba_for_values(
        values,
        pd.to_numeric(output["confidence"], errors="coerce").to_numpy(dtype=float),
        parameter=parameter,
    )
    output[["color_r", "color_g", "color_b", "color_a"]] = rgba
    return output


def _finite_or_none(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _records(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in frame.loc[:, columns].to_dict(orient="records"):
        result.append({key: _finite_or_none(value) for key, value in raw.items()})
    return result


def _forecast_station_frames(session: Session, config: AppConfig) -> dict[tuple[str, int], pd.DataFrame]:
    parameters, horizons = _configured_surface_axes(config)
    latest = (
        select(
            Forecast.parameter.label("parameter"),
            Forecast.forecast_horizon.label("horizon"),
            func.max(Forecast.forecast_origin_time).label("origin_time"),
        )
        .where(
            Forecast.parameter.in_(parameters),
            Forecast.forecast_horizon.in_(horizons),
        )
        .group_by(Forecast.parameter, Forecast.forecast_horizon)
        .subquery()
    )
    statement = (
        select(Forecast, AirStation, ModelVersion)
        .join(AirStation, AirStation.id == Forecast.air_station_id)
        .outerjoin(ModelVersion, ModelVersion.id == Forecast.model_version_id)
        .join(
            latest,
            and_(
                latest.c.parameter == Forecast.parameter,
                latest.c.horizon == Forecast.forecast_horizon,
                latest.c.origin_time == Forecast.forecast_origin_time,
            ),
        )
        .where(
            AirStation.latitude.is_not(None),
            AirStation.longitude.is_not(None),
        )
        .order_by(
            Forecast.parameter,
            Forecast.forecast_horizon,
            Forecast.air_station_id,
            Forecast.forecast_created_at.desc(),
        )
    )
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for forecast, station, model in session.execute(statement).all():
        feature_payload = dict(forecast.features_json or {})
        value = float(forecast.predicted_value)
        if not math.isfinite(value):
            continue
        grouped.setdefault((forecast.parameter, int(forecast.forecast_horizon)), []).append(
            {
                "forecast_id": forecast.id,
                "station_id": station.id,
                "source_id": station.source_id,
                "station_name": station.station_name,
                "city_name": station.city_name,
                "latitude": float(station.latitude),
                "longitude": float(station.longitude),
                "predicted_value": _bounded_value(
                    config,
                    str(forecast.parameter),
                    value,
                ),
                "forecast_created_at": ensure_utc(forecast.forecast_created_at),
                "origin_time": ensure_utc(forecast.forecast_origin_time),
                "target_time": ensure_utc(forecast.target_time),
                "serving_lead_hours": int(forecast.forecast_horizon),
                "model_horizon_hours": int(
                    feature_payload.get(
                        "model_horizon_hours", forecast.forecast_horizon
                    )
                ),
                "serving_anchor_time": feature_payload.get(
                    "serving_anchor_time"
                ) or _iso(
                    ensure_utc(forecast.target_time)
                    - timedelta(hours=max(0, int(forecast.forecast_horizon) - 1))
                ),
                "source_age_hours": (
                    float(feature_payload["source_age_hours"])
                    if feature_payload.get("source_age_hours") is not None
                    else max(
                        0.0,
                        (
                            ensure_utc(forecast.forecast_created_at)
                            - ensure_utc(forecast.forecast_origin_time)
                        ).total_seconds()
                        / 3600.0,
                    )
                ),
                "source_delay_to_anchor_hours": int(
                    feature_payload.get(
                        "source_delay_to_anchor_hours",
                        forecast.forecast_horizon,
                    )
                ),
                "model_version": model.semantic_version if model else forecast.model_version_id,
                "algorithm": model.algorithm if model else None,
            }
        )
    frames: dict[tuple[str, int], pd.DataFrame] = {}
    for key, rows in grouped.items():
        frame = pd.DataFrame(rows)
        # Multiple active models should not normally predict the same station and
        # origin. In case of historical residue, keep the newest persisted row.
        frame = (
            frame.sort_values("forecast_created_at", ascending=False, kind="stable")
            .drop_duplicates(subset=["station_id"], keep="first")
            .sort_values("station_id", kind="stable")
            .reset_index(drop=True)
        )
        frames[key] = frame
    return frames


def _surface_id(
    frame: pd.DataFrame,
    *,
    parameter: str,
    horizon: int,
    config: AppConfig,
) -> str:
    basis = {
        "parameter": parameter,
        "horizon_hours": horizon,
        "forecast_ids": sorted(frame["forecast_id"].astype(str).tolist()),
        "algorithm": config.spatial.algorithm,
        "grid_resolution_km": config.spatial.grid_resolution_km,
        "projected_crs": config.spatial.projected_crs,
        "idw_power": config.spatial.idw_power,
        "nearest_stations": config.spatial.nearest_stations,
        "minimum_stations": config.spatial.minimum_stations,
        "maximum_distance_km": config.spatial.maximum_distance_km,
    }
    token = hashlib.sha256(canonical_json_bytes(basis)).hexdigest()[:16]
    origin = ensure_utc(frame["origin_time"].iloc[0]).strftime("%Y%m%dT%H%M%SZ")
    return f"{origin}-{parameter.lower().replace('.', '')}-h{horizon:02d}-{token}"


def _surface_set_id(surfaces: list[SpatialSurface], config: AppConfig) -> str:
    basis = {
        "surfaces": sorted(surface.surface_id for surface in surfaces),
        "schema_version": SPATIAL_SCHEMA_VERSION,
        "algorithm": config.spatial.algorithm,
    }
    token = hashlib.sha256(canonical_json_bytes(basis)).hexdigest()[:16]
    generated = max(surface.generated_at for surface in surfaces)
    return f"{generated:%Y%m%dT%H%M%SZ}-{token}"


def _load_places(config: AppConfig, station_frames: dict[tuple[str, int], pd.DataFrame]) -> list[dict[str, Any]]:
    places: dict[str, dict[str, Any]] = {}
    with config.spatial.places_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            name = str(row["name"]).strip()
            places[name.casefold()] = {
                "name": name,
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "population": int(float(row["population"])) if row.get("population") else None,
                "aliases": [value.strip() for value in str(row.get("aliases") or "").split("|") if value.strip()],
                "source": str(row.get("source") or "bundled_polish_gazetteer"),
            }
    for frame in station_frames.values():
        for row in frame.to_dict(orient="records"):
            name = str(row.get("city_name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            places.setdefault(
                key,
                {
                    "name": name,
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "population": None,
                    "aliases": [str(row.get("station_name") or "")],
                    "source": "gios_forecast_station",
                },
            )
    return sorted(places.values(), key=lambda item: (-(item.get("population") or 0), item["name"]))


def _build_surface(
    *,
    frame: pd.DataFrame,
    parameter: str,
    horizon: int,
    grid: SpatialGrid,
    config: AppConfig,
) -> SpatialSurface:
    if len(frame) < config.spatial.minimum_stations:
        raise ValueError(
            f"Only {len(frame)} station predictions are available for {parameter}/h{horizon}; "
            f"minimum is {config.spatial.minimum_stations}."
        )
    origin_time = ensure_utc(frame["origin_time"].iloc[0])
    target_time = ensure_utc(frame["target_time"].iloc[0])
    generated_at = max(ensure_utc(value) for value in frame["forecast_created_at"].tolist())
    interpolator = create_spatial_interpolator(config.spatial)
    surface_frame, metrics = interpolator.interpolate(
        grid=grid,
        stations=frame,
        parameter=parameter,
        horizon_hours=horizon,
        origin_time=origin_time,
        target_time=target_time,
    )
    surface_frame = _bound_surface_values(
        surface_frame,
        config=config,
        parameter=parameter,
    )
    surface_frame, validation = validate_frame(
        surface_frame,
        "spatial_surface",
        config,
        context={
            "parameter": parameter,
            "horizon_hours": horizon,
            "origin_time": _iso(origin_time),
            "algorithm": interpolator.algorithm_name,
        },
    )
    metrics.update(
        {
            "algorithm": interpolator.algorithm_name,
            "station_count": int(len(frame)),
            "validation_valid": validation.valid,
            "validation_engine": validation.engine,
            "serving_lead_hours": int(horizon),
            "model_horizon_hours": int(
                frame["model_horizon_hours"].iloc[0]
            ),
            "source_age_hours": float(frame["source_age_hours"].iloc[0]),
        }
    )
    stations = _records(
        frame,
        [
            "station_id",
            "source_id",
            "station_name",
            "city_name",
            "latitude",
            "longitude",
            "predicted_value",
            "model_version",
            "algorithm",
            "forecast_id",
        ],
    )
    versions = tuple(sorted(set(frame["model_version"].astype(str))))
    return SpatialSurface(
        surface_id=_surface_id(frame, parameter=parameter, horizon=horizon, config=config),
        parameter=parameter,
        horizon_hours=horizon,
        origin_time=origin_time,
        target_time=target_time,
        generated_at=generated_at,
        model_versions=versions,
        grid=surface_frame,
        stations=stations,
        metrics=metrics,
        metadata={
            "schema_version": SPATIAL_SCHEMA_VERSION,
            "serving_lead_hours": int(horizon),
            "model_horizon_hours": int(
                frame["model_horizon_hours"].iloc[0]
            ),
            "serving_anchor_time": frame["serving_anchor_time"].iloc[0],
            "source_age_hours": float(frame["source_age_hours"].iloc[0]),
            "source_delay_to_anchor_hours": int(
                frame["source_delay_to_anchor_hours"].iloc[0]
            ),
            "projected_crs": grid.projected_crs,
            "grid_resolution_km": grid.resolution_m / 1000.0,
            "spatial_method": "quality_weighted_idw",
            "idw_power": config.spatial.idw_power,
            "idw_distance_smoothing_m": config.spatial.idw_distance_smoothing_m,
            "exact_station_threshold_m": config.spatial.exact_station_threshold_m,
            "nearest_stations": config.spatial.nearest_stations,
            "minimum_stations": config.spatial.minimum_stations,
            "maximum_distance_km": config.spatial.maximum_distance_km,
            "confidence_distance_km": config.spatial.confidence_distance_km,
            "confidence_minimum": config.spatial.confidence_minimum,
            "units": (
                (
                    create_air_parameter_registry(config).get(parameter).canonical_unit
                    if create_air_parameter_registry(config).get(parameter) is not None
                    else None
                )
                or unit_for(
                    parameter,
                    precipitation_accumulation_period_hours=(
                        config.hourly_forecasting.precipitation.accumulation_period_hours
                    ),
                )
            ),
            "precipitation_accumulation_period_hours": (
                config.hourly_forecasting.precipitation.accumulation_period_hours
                if parameter == "precipitation_mm"
                else None
            ),
            "value_semantics": (
                "accumulation_ending_at_target_time"
                if parameter == "precipitation_mm"
                else "locally_precomputed_spatial_forecast"
            ),
            "server_computation": "exact_point_idw_from_published_station_forecasts",
        },
    )


def _surface_payload(surface: SpatialSurface) -> dict[str, Any]:
    columns = [
        "cell_id",
        "row",
        "column",
        "latitude",
        "longitude",
        "value",
        "confidence",
        "nearest_station_distance_km",
        "stations_used",
        "local_station_spread",
        "quality_flag",
        "parameter",
        "horizon_hours",
        "origin_time",
        "target_time",
        "color_r",
        "color_g",
        "color_b",
        "color_a",
    ]
    return {
        "schema_version": SPATIAL_SCHEMA_VERSION,
        "surface_id": surface.surface_id,
        "parameter": surface.parameter,
        "horizon_hours": surface.horizon_hours,
        "serving_lead_hours": surface.horizon_hours,
        "model_horizon_hours": surface.metadata.get("model_horizon_hours"),
        "serving_anchor_time": surface.metadata.get("serving_anchor_time"),
        "source_age_hours": surface.metadata.get("source_age_hours"),
        "source_delay_to_anchor_hours": surface.metadata.get(
            "source_delay_to_anchor_hours"
        ),
        "origin_time": _iso(surface.origin_time),
        "target_time": _iso(surface.target_time),
        "generated_at": _iso(surface.generated_at),
        "model_versions": list(surface.model_versions),
        "metadata": surface.metadata,
        "metrics": surface.metrics,
        "stations": surface.stations,
        "grid": _records(surface.grid, columns),
    }


def _write_local_cache(config: AppConfig, manifest: dict[str, Any], payloads: list[dict[str, Any]]) -> None:
    root = config.spatial.local_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / "runs" / str(manifest["surface_set_id"])
    run_root.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        parameter = str(payload["parameter"]).replace(".", "")
        path = run_root / f"{parameter}-h{int(payload['horizon_hours']):02d}.json.gz"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(gzip.compress(canonical_json_bytes(payload), compresslevel=6, mtime=0))
        temporary.replace(path)
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    latest = root / "latest.json"
    temporary = latest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(latest)


def _publish_static_assets(
    repository: ArtifactRepository,
    config: AppConfig,
    *,
    places: list[dict[str, Any]],
) -> dict[str, Any]:
    boundary = load_boundary_geojson(config.spatial.boundary_geojson)
    boundary_artifact = repository.put_json(
        repository.layout.spatial_boundary,
        boundary,
        immutable=True,
        metadata={"source": "Natural Earth", "license": "public-domain"},
    )
    places_artifact = repository.put_json(
        repository.layout.spatial_places,
        {
            "schema_version": SPATIAL_SCHEMA_VERSION,
            "places": places,
        },
        immutable=False,
    )
    return {
        "boundary_key": boundary_artifact.key,
        "boundary_checksum": boundary_artifact.checksum,
        "places_key": places_artifact.key,
        "places_checksum": places_artifact.checksum,
    }


def build_spatial_surfaces(
    session: Session,
    config: AppConfig,
    progress: ProgressReporter | None = None,
) -> StageStats:
    """Build and publish all Poland-wide surfaces from persisted local forecasts.

    This function is intentionally part of the local pipeline. The public server
    never loads a trained model: it reads published station forecasts through the
    storage Bridge and may apply only deterministic exact-point IDW and PCHIP.
    """

    if not config.spatial.enabled:
        if progress is not None:
            progress.complete_stage(
                "spatial",
                task="spatial surfaces disabled",
                detail={"reason": "spatial_surfaces_disabled"},
            )
        return StageStats(skipped=1, details={"reason": "spatial_surfaces_disabled"})
    if not config.object_storage.enabled:
        if progress is not None:
            progress.complete_stage(
                "spatial",
                task="spatial surfaces skipped",
                detail={"reason": "object_storage_disabled"},
            )
        return StageStats(skipped=1, warnings=1, details={"reason": "object_storage_disabled"})

    configured_parameters, configured_horizons = _configured_surface_axes(config)
    requested_count = len(configured_parameters) * len(configured_horizons)
    build_weight = 1.0
    upload_weight = 0.25
    preparation_weight = 2.0
    finalization_weight = 1.0
    total_weight = (
        preparation_weight
        + requested_count * build_weight
        + requested_count * upload_weight
        + finalization_weight
    )
    work = WeightedStageProgress(progress, stage="spatial", total_weight=total_weight)

    with work.task(
        "load station forecasts and create Poland grid",
        preparation_weight,
        task_key="spatial:prepare-grid",
        fallback_seconds=180.0,
        detail={
            "phase": "prepare",
            "parameters": configured_parameters,
            "horizons": len(configured_horizons),
        },
    ):
        station_frames = _forecast_station_frames(session, config)
        if not station_frames:
            work.complete(name="spatial skipped — no station forecasts")
            return StageStats(skipped=1, warnings=1, details={"reason": "no_station_forecasts"})
        boundary = load_boundary_geojson(config.spatial.boundary_geojson)
        grid = create_poland_grid(
            boundary,
            projected_crs=config.spatial.projected_crs,
            resolution_km=config.spatial.grid_resolution_km,
        )

    surfaces: list[SpatialSurface] = []
    errors: list[dict[str, Any]] = []
    surface_index = 0
    for parameter in configured_parameters:
        for horizon in configured_horizons:
            surface_index += 1
            frame = station_frames.get((str(parameter), int(horizon)))
            task_name = (
                f"build surface {surface_index}/{requested_count}: "
                f"{parameter} h{int(horizon):02d}"
            )
            if frame is None or frame.empty:
                errors.append(
                    {
                        "parameter": parameter,
                        "horizon_hours": horizon,
                        "error": "missing_forecasts",
                    }
                )
                work.advance(
                    task_name + " — missing",
                    build_weight,
                    detail={
                        "phase": "build_surface",
                        "surface_index": surface_index,
                        "surface_total": requested_count,
                        "parameter": parameter,
                        "horizon_hours": int(horizon),
                        "reason": "missing_forecasts",
                    },
                    status="skipped",
                )
                continue
            try:
                with work.task(
                    task_name,
                    build_weight,
                    task_key=f"spatial:build:{parameter}",
                    fallback_seconds=12.0,
                    detail={
                        "phase": "build_surface",
                        "surface_index": surface_index,
                        "surface_total": requested_count,
                        "parameter": parameter,
                        "horizon_hours": int(horizon),
                        "grid_cells": len(grid.frame),
                        "station_count": len(frame),
                    },
                ):
                    surfaces.append(
                        _build_surface(
                            frame=frame,
                            parameter=str(parameter),
                            horizon=int(horizon),
                            grid=grid,
                            config=config,
                        )
                    )
            except Exception as exc:
                errors.append(
                    {
                        "parameter": parameter,
                        "horizon_hours": horizon,
                        "error": str(exc),
                    }
                )

    if not surfaces:
        # Upload work cannot occur. Account for it before leaving the stage.
        work.advance(
            "surface upload skipped",
            requested_count * upload_weight + finalization_weight,
            detail={"reason": "no_spatial_surface_created"},
            status="skipped",
        )
        work.complete(name="spatial failed — no surfaces")
        return StageStats(
            errors=len(errors) or 1,
            details={"reason": "no_spatial_surface_created", "surface_errors": errors},
        )

    repository = create_artifact_repository(config)
    repository.ping()
    places = _load_places(config, station_frames)
    static = _publish_static_assets(repository, config, places=places)
    surface_set_id = _surface_set_id(surfaces, config)
    entries: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    for upload_index, surface in enumerate(surfaces, start=1):
        with work.task(
            (
                f"upload surface {upload_index}/{len(surfaces)}: "
                f"{surface.parameter} h{surface.horizon_hours:02d}"
            ),
            upload_weight,
            task_key=f"spatial:upload:{surface.parameter}",
            fallback_seconds=4.0,
            detail={
                "phase": "upload_surface",
                "surface_index": upload_index,
                "surface_total": len(surfaces),
                "parameter": surface.parameter,
                "horizon_hours": surface.horizon_hours,
            },
        ):
            payload = _surface_payload(surface)
            payloads.append(payload)
            surface_key = repository.layout.spatial_surface(
                surface_set_id,
                surface.parameter,
                surface.horizon_hours,
            )
            stored = repository.put_gzip_json(
                surface_key,
                payload,
                immutable=True,
                metadata={
                    "surface-id": surface.surface_id,
                    "parameter": surface.parameter,
                    "horizon-hours": str(surface.horizon_hours),
                },
                compresslevel=config.publication.gzip_compresslevel,
            )
            metadata = {
                "schema_version": SPATIAL_SCHEMA_VERSION,
                "surface_id": surface.surface_id,
                "surface_set_id": surface_set_id,
                "parameter": surface.parameter,
                "horizon_hours": surface.horizon_hours,
                "origin_time": _iso(surface.origin_time),
                "target_time": _iso(surface.target_time),
                "generated_at": _iso(surface.generated_at),
                "model_versions": list(surface.model_versions),
                "object_key": stored.key,
                "checksum": stored.checksum,
                "size": stored.size,
                "grid_cells": len(surface.grid),
                "station_count": len(surface.stations),
                "metrics": surface.metrics,
                "metadata": surface.metadata,
            }
            metadata_artifact = repository.put_json(
                repository.layout.spatial_surface_metadata(
                    surface_set_id,
                    surface.parameter,
                    surface.horizon_hours,
                ),
                metadata,
                immutable=True,
            )
            metadata["metadata_key"] = metadata_artifact.key
            entries.append(metadata)

    missing_upload_slots = max(0, requested_count - len(surfaces))
    if missing_upload_slots:
        work.advance(
            "skip upload slots for missing surfaces",
            missing_upload_slots * upload_weight,
            detail={"missing_surface_count": missing_upload_slots},
            status="skipped",
        )

    with work.task(
        "publish spatial manifest and latest pointer",
        finalization_weight,
        task_key="spatial:manifest",
        fallback_seconds=60.0,
        detail={
            "phase": "manifest",
            "surface_count": len(surfaces),
            "surface_errors": len(errors),
        },
    ):
        generated_at = max(surface.generated_at for surface in surfaces)
        surface_parameters = sorted({surface.parameter for surface in surfaces})
        air_registry = create_air_parameter_registry(config)
        air_parameter_catalog = air_registry.public_catalog(
            parameter
            for parameter in surface_parameters
            if air_registry.contains(parameter)
        )
        manifest = {
            "schema_version": SPATIAL_SCHEMA_VERSION,
            "surface_set_id": surface_set_id,
            "generated_at": _iso(generated_at),
            "source_host_id": config.source_host_id,
            "value_semantics": "locally_precomputed_spatial_forecast",
            "app_platform_processing": "read_and_exact_point_interpolate",
            "forecast_mode": (
                "horizon-conditioned-hourly"
                if config.hourly_forecasting.enabled
                else "discrete-horizons"
            ),
            "exact_target_time_available": bool(config.hourly_forecasting.enabled),
            "precipitation": {
                "accumulation_period_hours": (
                    config.hourly_forecasting.precipitation.accumulation_period_hours
                ),
                "ending_at_target_time": True,
                "disaggregated_to_hourly": False,
            },
            "algorithm": config.spatial.algorithm,
            "grid_resolution_km": config.spatial.grid_resolution_km,
            "projected_crs": config.spatial.projected_crs,
            **static,
            "parameters": surface_parameters,
            "air_parameter_catalog": air_parameter_catalog,
            "horizons_hours": sorted({surface.horizon_hours for surface in surfaces}),
            "surfaces": sorted(
                entries,
                key=lambda item: (item["parameter"], item["horizon_hours"]),
            ),
            "surface_errors": errors,
        }
        manifest_artifact = repository.put_json(
            repository.layout.spatial_manifest(surface_set_id),
            manifest,
            immutable=True,
        )
        pointer = {
            "schema_version": SPATIAL_SCHEMA_VERSION,
            "surface_set_id": surface_set_id,
            "manifest_key": manifest_artifact.key,
            "manifest_checksum": manifest_artifact.checksum,
            "generated_at": _iso(generated_at),
            "algorithm": config.spatial.algorithm,
            "grid_resolution_km": config.spatial.grid_resolution_km,
            "parameters": manifest["parameters"],
            "horizons_hours": manifest["horizons_hours"],
        }
        repository.put_json(
            repository.layout.latest_spatial_pointer,
            pointer,
            immutable=False,
        )
        _write_local_cache(config, manifest, payloads)

    work.complete(name="spatial surfaces completed")
    return StageStats(
        inserted=len(surfaces),
        warnings=len(errors),
        details={
            "surface_set_id": surface_set_id,
            "manifest_key": manifest_artifact.key,
            "surface_count": len(surfaces),
            "grid_cells_per_surface": len(grid.frame),
            "station_forecast_groups": len(station_frames),
            "surface_errors": errors,
            "storage_backend": repository.store.backend_name,
            "flow_step": "05-local-spatial-forecasts-published",
            "progress_file": str(progress.current_path) if progress is not None else None,
        },
    )


def validate_latest_spatial_surfaces(session: Session, config: AppConfig) -> StageStats:  # noqa: ARG001
    if not config.object_storage.enabled:
        return StageStats(skipped=1, details={"reason": "object_storage_disabled"})
    repository = create_artifact_repository(config)
    pointer = repository.get_json(repository.layout.latest_spatial_pointer)
    manifest = repository.get_json(str(pointer["manifest_key"]))
    errors: list[dict[str, Any]] = []
    checked = 0
    for entry in manifest.get("surfaces", []):
        try:
            payload = repository.get_gzip_json(str(entry["object_key"]))
            frame = pd.DataFrame(payload.get("grid", []))
            _, result = validate_frame(
                frame,
                "spatial_surface",
                config,
                context={
                    "surface_id": payload.get("surface_id"),
                    "source": "object_store_validation",
                },
            )
            checked += 1
            if not result.valid:
                errors.append({"surface_id": payload.get("surface_id"), "error": result.failure_cases})
        except Exception as exc:
            errors.append({"object_key": entry.get("object_key"), "error": str(exc)})
    return StageStats(
        downloaded=checked,
        errors=len(errors),
        details={
            "surface_set_id": pointer.get("surface_set_id"),
            "checked": checked,
            "errors": errors,
        },
    )
