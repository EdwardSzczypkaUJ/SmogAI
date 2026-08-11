from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry, is_air_target
from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.config import AppConfig
from smog_ai.database.models import (
    AirMeasurement,
    AirSensor,
    AirStation,
    StationMatch,
    WeatherMeasurement,
    WeatherStation,
)
from smog_ai.data_validation import validate_frame
from smog_ai.domain import StageStats
from smog_ai.progress import ProgressReporter, WeightedStageProgress
from smog_ai.features.builder import (
    build_training_frame,
    build_training_frame_from_operational_bundle,
)
from smog_ai.hourly.training_policy import resolve_training_profile
from smog_ai.hourly.features import (
    build_hourly_pm_training_frame,
    build_hourly_pm_training_frame_from_bundle,
    build_hourly_weather_training_frame,
    build_hourly_weather_training_frame_from_bundle,
)
from smog_ai.storage.factory import create_object_store
from smog_ai.time_utils import ensure_utc, utc_now

logger = logging.getLogger(__name__)

AIR_MEASUREMENT_COLUMNS = [
    "id",
    "source",
    "air_station_id",
    "air_sensor_id",
    "source_station_id",
    "source_sensor_id",
    "parameter",
    "measurement_time",
    "value",
    "unit",
    "source_status",
    "is_valid",
    "raw_json",
    "collected_at",
]
WEATHER_MEASUREMENT_COLUMNS = [
    "id",
    "source",
    "weather_station_id",
    "source_station_id",
    "measurement_time",
    "temperature_c",
    "humidity_percent",
    "pressure_hpa",
    "precipitation_mm",
    "precipitation_accumulation_period_hours",
    "wind_speed_mps",
    "wind_direction_deg",
    "is_valid",
    "raw_json",
    "collected_at",
]

REQUIRED_OPERATIONAL_COLLECTIONS = (
    "air_stations",
    "air_sensors",
    "air_measurements",
    "weather_stations",
    "weather_measurements",
)


def create_artifact_repository(config: AppConfig) -> ArtifactRepository:
    return ArtifactRepository(create_object_store(config.object_storage))


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat().replace("+00:00", "Z")
    return value


def _rows(
    session: Session,
    model: Any,
    columns: list[str],
    *,
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    query = select(model)
    if cutoff is not None and hasattr(model, "measurement_time"):
        query = query.where(model.measurement_time >= cutoff)
    result: list[dict[str, Any]] = []
    for row in session.scalars(query).all():
        result.append({name: _value(getattr(row, name)) for name in columns})
    return result


def _validation_to_store(
    repository: ArtifactRepository,
    *,
    kind: str,
    report_id: str,
    report: dict[str, Any],
    generated_at: datetime,
) -> str:
    key = repository.layout.validation_report(kind, report_id)
    repository.put_json(key, report, immutable=True)
    repository.put_json(
        repository.layout.latest_validation_pointer(kind),
        {
            "object_key": key,
            "valid": bool(report.get("valid", False)),
            "updated_at": generated_at.isoformat(),
        },
    )
    return key


def export_operational_data(
    session: Session,
    config: AppConfig,
    *,
    run_id: str | None = None,
    max_days: int | None = None,
) -> StageStats:
    """Upload a reproducible post-collection data bundle to object storage.

    The bundle is the mandatory cloud hand-off.  It is uploaded before any model
    training.  A later training task downloads this exact object from the store,
    performs Pandera validation and feature engineering locally, then uploads the
    curated datasets, model binaries, metrics and dashboard snapshot.
    """
    if not config.object_storage.enabled:
        return StageStats(skipped=1, details={"reason": "object_storage_disabled"})
    repository = create_artifact_repository(config)
    repository.ping()
    generated_at = utc_now()
    run_id = run_id or str(uuid.uuid4())
    days = max_days if max_days is not None else config.artifacts.operational_export_days
    cutoff = generated_at - timedelta(days=days) if days > 0 else None
    payload: dict[str, Any] = {
        "schema_version": config.artifacts.schema_version,
        "run_id": run_id,
        "generated_at": generated_at.isoformat(),
        "source_host_id": config.source_host_id,
        "air_stations": _rows(
            session,
            AirStation,
            [
                "id",
                "source",
                "source_id",
                "station_code",
                "station_name",
                "city_name",
                "address",
                "latitude",
                "longitude",
                "active",
                "raw_json",
            ],
        ),
        "air_sensors": _rows(
            session,
            AirSensor,
            [
                "id",
                "source",
                "source_id",
                "air_station_id",
                "parameter_code",
                "parameter_name",
                "formula",
                "source_parameter_id",
                "active",
                "raw_json",
            ],
        ),
        "air_measurements": _rows(
            session,
            AirMeasurement,
            AIR_MEASUREMENT_COLUMNS,
            cutoff=cutoff,
        ),
        "weather_stations": _rows(
            session,
            WeatherStation,
            [
                "id",
                "source",
                "source_id",
                "station_name",
                "latitude",
                "longitude",
                "elevation_m",
                "metadata_source",
                "active",
                "raw_json",
            ],
        ),
        "weather_measurements": _rows(
            session,
            WeatherMeasurement,
            WEATHER_MEASUREMENT_COLUMNS,
            cutoff=cutoff,
        ),
        "station_matches": _rows(
            session,
            StationMatch,
            [
                "id",
                "air_station_id",
                "weather_station_id",
                "distance_km",
                "matched_at",
                "matching_algorithm",
                "is_distance_acceptable",
            ],
        ),
    }
    validation_results: list[dict[str, Any]] = []
    for kind, rows, columns in (
        ("air_measurements", payload["air_measurements"], AIR_MEASUREMENT_COLUMNS),
        (
            "weather_measurements",
            payload["weather_measurements"],
            WEATHER_MEASUREMENT_COLUMNS,
        ),
    ):
        frame = pd.DataFrame(rows, columns=columns)
        logger.info(
            "Operational validation started kind=%s rows=%s run_id=%s",
            kind,
            len(frame),
            run_id,
        )
        _, validation = validate_frame(
            frame,
            kind,  # type: ignore[arg-type]
            config,
            context={"run_id": run_id, "generated_at": generated_at.isoformat()},
        )
        logger.info(
            "Operational validation finished kind=%s valid=%s failures=%s "
            "duration_ms=%.3f report=%s",
            kind,
            validation.valid,
            validation.failure_count,
            validation.duration_ms,
            validation.report_path,
        )
        report_id = f"{generated_at:%Y%m%dT%H%M%SZ}-{run_id}-{kind}"
        report_payload = validation.to_dict()
        report_key = _validation_to_store(
            repository,
            kind=kind,
            report_id=report_id,
            report=report_payload,
            generated_at=generated_at,
        )
        validation_results.append({**report_payload, "object_key": report_key})
    payload["validation"] = validation_results

    key = repository.layout.raw_bundle(run_id, generated_at)
    logger.info(
        "Operational bundle serialization/upload started run_id=%s "
        "air_measurements=%s weather_measurements=%s key=%s",
        run_id,
        len(payload["air_measurements"]),
        len(payload["weather_measurements"]),
        key,
    )
    stored = repository.put_gzip_json(
        key,
        payload,
        immutable=True,
        metadata={"run-id": run_id, "source-host-id": config.source_host_id},
        compresslevel=config.publication.gzip_compresslevel,
    )
    logger.info(
        "Operational bundle serialization/upload finished run_id=%s key=%s "
        "compressed_size_bytes=%s checksum=%s",
        run_id,
        stored.key,
        stored.size,
        stored.checksum,
    )
    record_counts = {
        key_name: len(value)
        for key_name, value in payload.items()
        if isinstance(value, list) and key_name != "validation"
    }
    missing_required = [
        name for name in REQUIRED_OPERATIONAL_COLLECTIONS if record_counts.get(name, 0) <= 0
    ]
    complete = not missing_required
    pointer = {
        "schema_version": config.artifacts.schema_version,
        "run_id": run_id,
        "object_key": stored.key,
        "checksum": stored.checksum,
        "size": stored.size,
        "generated_at": generated_at.isoformat(),
        "validation": validation_results,
        "record_counts": record_counts,
        "complete": complete,
        "missing_required": missing_required,
    }
    # Keep every immutable attempt for audit, but never replace the canonical
    # training pointer with a partial bundle after one collector has failed.
    repository.put_json(repository.layout.last_raw_attempt_manifest, pointer)
    if complete:
        repository.put_json(repository.layout.latest_raw_manifest, pointer)
    total_rows = sum(record_counts.values())
    return StageStats(
        downloaded=total_rows,
        inserted=1,
        warnings=0 if complete else 1,
        details={
            "artifact": {**pointer, "latest_pointer_updated": complete},
            "storage_backend": repository.store.backend_name,
            "flow_step": "01-collected-data-uploaded",
        },
    )


def load_latest_operational_bundle(
    config: AppConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = create_artifact_repository(config)
    repository.ping()
    pointer = repository.get_json(repository.layout.latest_raw_manifest)
    missing_required = [
        name
        for name in REQUIRED_OPERATIONAL_COLLECTIONS
        if int((pointer.get("record_counts") or {}).get(name, 0) or 0) <= 0
    ]
    if pointer.get("complete") is False or missing_required:
        raise RuntimeError(
            "Latest operational-data bundle is incomplete; missing: "
            + ", ".join(missing_required or pointer.get("missing_required", []))
        )
    payload = repository.get_gzip_json(str(pointer["object_key"]))
    if str(payload.get("run_id")) != str(pointer.get("run_id")):
        raise RuntimeError("Latest operational-data pointer does not match bundle run_id")
    return payload, pointer


def _store_training_frame(
    repository: ArtifactRepository,
    config: AppConfig,
    *,
    frame: pd.DataFrame,
    parameter: str,
    horizon: int,
    generated_at: datetime,
    source: str,
    source_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    dataset_context = {
        "parameter": parameter,
        "horizon_hours": horizon,
        "generated_at": generated_at.isoformat(),
        "source": source,
        "source_run_id": (source_manifest or {}).get("run_id"),
    }
    frame, validation = validate_frame(
        frame,
        "training_frame",
        config,
        context=dataset_context,
    )
    stable_columns = list(frame.columns)
    dataset_basis = {
        "parameter": parameter,
        "horizon": horizon,
        "rows": len(frame),
        "data_start": str(frame["measurement_time"].min()),
        "data_end": str(frame["measurement_time"].max()),
        "columns": stable_columns,
        "source_run_id": (source_manifest or {}).get("run_id"),
    }
    dataset_id = (
        f"{generated_at:%Y%m%dT%H%M%SZ}-"
        f"{uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(dataset_basis, sort_keys=True)).hex[:12]}"
    )
    validation.context["dataset_id"] = dataset_id
    validation_key = _validation_to_store(
        repository,
        kind="training_frame",
        report_id=dataset_id,
        report=validation.to_dict(),
        generated_at=generated_at,
    )
    data_key = repository.layout.feature_dataset(parameter, horizon, dataset_id)
    stored = repository.put_dataframe_csv_gzip(
        data_key,
        frame,
        metadata={
            "parameter": parameter,
            "horizon-hours": str(horizon),
            "dataset-id": dataset_id,
            "source": source,
        },
    )
    manifest = {
        "schema_version": config.artifacts.schema_version,
        "dataset_id": dataset_id,
        "parameter": parameter,
        "horizon_hours": horizon,
        "object_key": stored.key,
        "checksum": stored.checksum,
        "rows": len(frame),
        "columns": stable_columns,
        "data_start": str(frame["measurement_time"].min()),
        "data_end": str(frame["measurement_time"].max()),
        "created_at": generated_at.isoformat(),
        "source_host_id": config.source_host_id,
        "source": source,
        "source_manifest": source_manifest,
        "validation": {
            "valid": validation.valid,
            "engine": validation.engine,
            "failure_count": validation.failure_count,
            "report_key": validation_key,
        },
    }
    manifest_key = repository.layout.feature_manifest(parameter, horizon, dataset_id)
    repository.put_json(manifest_key, manifest, immutable=True)
    repository.put_json(repository.layout.latest_feature_pointer(parameter, horizon), manifest)
    return manifest


def export_training_frames(
    session: Session,
    config: AppConfig,
    *,
    source: Literal["database", "object_store"] = "database",
) -> StageStats:
    """Create curated training data and upload it through the storage Bridge.

    In the production/course path use ``source='object_store'``.  The function then
    downloads the latest raw bundle from Spaces and builds features from those
    downloaded bytes, never from the caller's SQLAlchemy session.
    """
    repository = create_artifact_repository(config)
    repository.ping()
    stats = StageStats()
    outputs: list[dict[str, Any]] = []
    now = utc_now()
    bundle: dict[str, Any] | None = None
    raw_pointer: dict[str, Any] | None = None
    if source == "object_store":
        bundle, raw_pointer = load_latest_operational_bundle(config)

    for parameter in config.training.parameters:
        for horizon in config.training.horizons_hours:
            if source == "object_store":
                assert bundle is not None
                frame = build_training_frame_from_operational_bundle(
                    bundle,
                    parameter=parameter,
                    horizon_hours=horizon,
                    max_days=config.training.max_training_days,
                )
            else:
                frame = build_training_frame(
                    session,
                    parameter=parameter,
                    horizon_hours=horizon,
                    max_days=config.training.max_training_days,
                )
            if frame.empty:
                stats.skipped += 1
                outputs.append(
                    {
                        "parameter": parameter,
                        "horizon": horizon,
                        "status": "empty",
                        "source": source,
                    }
                )
                continue
            manifest = _store_training_frame(
                repository,
                config,
                frame=frame,
                parameter=parameter,
                horizon=horizon,
                generated_at=now,
                source=source,
                source_manifest=raw_pointer,
            )
            stats.inserted += 1
            stats.downloaded += len(frame)
            outputs.append(manifest)
    stats.details = {
        "datasets": outputs,
        "storage_backend": repository.store.backend_name,
        "source": source,
        "flow_step": "02-raw-downloaded-cleaned-and-curated",
    }
    return stats


def materialize_training_frames_from_store(
    session: Session,
    config: AppConfig,
) -> StageStats:
    return export_training_frames(session, config, source="object_store")


def load_training_frame_from_store(
    config: AppConfig,
    *,
    parameter: str,
    horizon: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    repository = create_artifact_repository(config)
    pointer = repository.get_json(repository.layout.latest_feature_pointer(parameter, horizon))
    frame = repository.get_dataframe_csv_gzip(str(pointer["object_key"]))
    frame, validation = validate_frame(
        frame,
        "training_frame",
        config,
        context={
            "dataset_id": pointer.get("dataset_id"),
            "object_key": pointer.get("object_key"),
            "phase": "load-from-object-store",
        },
    )
    pointer = {**pointer, "load_validation": validation.to_dict()}
    return frame, pointer



def _hourly_training_frame(
    session: Session,
    config: AppConfig,
    *,
    target: str,
    source: Literal["database", "object_store"],
    bundle: dict[str, Any] | None,
    profile_name: str | None = None,
) -> pd.DataFrame:
    settings = config.hourly_forecasting
    profile = resolve_training_profile(config, profile_name)
    horizons = settings.horizons_hours
    max_days = profile.maximum_training_days(target)
    maximum_rows = profile.maximum_rows_per_target
    if is_air_target(config, target):
        parameter_registry = create_air_parameter_registry(config)
        definition = parameter_registry.require(target)
        auxiliary_parameters = parameter_registry.auxiliary_codes
        if source == "object_store":
            if bundle is None:
                raise RuntimeError("Object-store bundle was not loaded")
            frame = build_hourly_pm_training_frame_from_bundle(
                bundle,
                parameter=target,
                horizons=horizons,
                max_days=max_days,
                allow_negative_target=definition.allow_negative,
                auxiliary_parameters=auxiliary_parameters,
                maximum_output_rows=maximum_rows,
                horizon_bucket_edges=profile.horizon_bucket_edges,
                samples_per_horizon_bucket=profile.samples_per_horizon_bucket,
                random_state=settings.random_state,
            )
        else:
            frame = build_hourly_pm_training_frame(
                session,
                parameter=target,
                horizons=horizons,
                max_days=max_days,
                allow_negative_target=definition.allow_negative,
                auxiliary_parameters=auxiliary_parameters,
                maximum_output_rows=maximum_rows,
                horizon_bucket_edges=profile.horizon_bucket_edges,
                samples_per_horizon_bucket=profile.samples_per_horizon_bucket,
                random_state=settings.random_state,
            )
    elif target in {"temperature_c", "precipitation_mm"}:
        if source == "object_store":
            if bundle is None:
                raise RuntimeError("Object-store bundle was not loaded")
            frame = build_hourly_weather_training_frame_from_bundle(
                bundle,
                target=target,
                horizons=horizons,
                max_days=max_days,
                maximum_output_rows=maximum_rows,
                horizon_bucket_edges=profile.horizon_bucket_edges,
                samples_per_horizon_bucket=profile.samples_per_horizon_bucket,
                random_state=settings.random_state,
                precipitation_accumulation_period_hours=(
                    config.hourly_forecasting.precipitation.accumulation_period_hours
                ),
                precipitation_occurrence_threshold_mm=(
                    config.hourly_forecasting.precipitation.occurrence_threshold_mm
                ),
            )
        else:
            frame = build_hourly_weather_training_frame(
                session,
                target=target,
                horizons=horizons,
                max_days=max_days,
                maximum_output_rows=maximum_rows,
                horizon_bucket_edges=profile.horizon_bucket_edges,
                samples_per_horizon_bucket=profile.samples_per_horizon_bucket,
                random_state=settings.random_state,
                precipitation_accumulation_period_hours=(
                    config.hourly_forecasting.precipitation.accumulation_period_hours
                ),
                precipitation_occurrence_threshold_mm=(
                    config.hourly_forecasting.precipitation.occurrence_threshold_mm
                ),
            )
    else:
        raise ValueError(f"Unsupported hourly target: {target}")
    if not frame.empty:
        frame = frame.copy()
        frame["target_name"] = target
    return frame


def _store_hourly_training_frame(
    repository: ArtifactRepository,
    config: AppConfig,
    *,
    frame: pd.DataFrame,
    target: str,
    generated_at: datetime,
    source: str,
    source_manifest: dict[str, Any] | None,
    training_profile: str,
    training_policy: str,
) -> dict[str, Any]:
    frame, validation = validate_frame(
        frame,
        "hourly_training_frame",
        config,
        context={
            "target": target,
            "generated_at": generated_at.isoformat(),
            "source": source,
            "source_run_id": (source_manifest or {}).get("run_id"),
            "training_profile": training_profile,
            "training_policy": training_policy,
        },
    )
    basis = {
        "target": target,
        "rows": len(frame),
        "data_start": str(frame["measurement_time"].min()),
        "data_end": str(frame["measurement_time"].max()),
        "horizons": sorted(int(value) for value in frame["horizon_hours"].unique()),
        "columns": list(frame.columns),
        "source_run_id": (source_manifest or {}).get("run_id"),
    }
    dataset_id = (
        f"{generated_at:%Y%m%dT%H%M%SZ}-"
        f"{uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(basis, sort_keys=True)).hex[:12]}"
    )
    validation.context["dataset_id"] = dataset_id
    validation_key = _validation_to_store(
        repository,
        kind="hourly_training_frame",
        report_id=dataset_id,
        report=validation.to_dict(),
        generated_at=generated_at,
    )
    data_key = repository.layout.hourly_feature_dataset(target, dataset_id)
    stored = repository.put_dataframe_csv_gzip(
        data_key,
        frame,
        metadata={
            "target": target,
            "dataset-id": dataset_id,
            "source": source,
            "forecast-mode": "horizon-conditioned-hourly",
        },
    )
    manifest = {
        "schema_version": config.artifacts.schema_version,
        "dataset_id": dataset_id,
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "object_key": stored.key,
        "checksum": stored.checksum,
        "rows": len(frame),
        "columns": list(frame.columns),
        "horizons_hours": sorted(int(value) for value in frame["horizon_hours"].unique()),
        "data_start": str(frame["measurement_time"].min()),
        "data_end": str(frame["measurement_time"].max()),
        "created_at": generated_at.isoformat(),
        "source_host_id": config.source_host_id,
        "source": source,
        "training_profile": training_profile,
        "training_policy": training_policy,
        "source_manifest": source_manifest,
        "validation": {
            "valid": validation.valid,
            "engine": validation.engine,
            "failure_count": validation.failure_count,
            "report_key": validation_key,
        },
    }
    manifest_key = repository.layout.hourly_feature_manifest(target, dataset_id)
    repository.put_json(manifest_key, manifest, immutable=True)
    repository.put_json(repository.layout.latest_hourly_feature_pointer(target), manifest)
    return manifest


def export_hourly_training_frames(
    session: Session,
    config: AppConfig,
    *,
    source: Literal["database", "object_store"] = "database",
    progress: ProgressReporter | None = None,
    profile_name: str | None = None,
) -> StageStats:
    if not config.hourly_forecasting.enabled:
        return StageStats(skipped=1, details={"reason": "hourly_forecasting_disabled"})
    profile = resolve_training_profile(config, profile_name)
    policy_name = config.hourly_forecasting.training_policy.strategy
    repository = create_artifact_repository(config)
    repository.ping()
    bundle: dict[str, Any] | None = None
    raw_pointer: dict[str, Any] | None = None
    if source == "object_store":
        bundle, raw_pointer = load_latest_operational_bundle(config)
    stats = StageStats()
    outputs: list[dict[str, Any]] = []
    generated_at = utc_now()
    work = WeightedStageProgress(
        progress,
        stage="training_data",
        total_weight=max(1.0, float(len(config.hourly_forecasting.targets))),
    )
    for target_index, target in enumerate(config.hourly_forecasting.targets, start=1):
        with work.task(
            f"build/store training frame {target_index}/{len(config.hourly_forecasting.targets)}: {target}",
            1.0,
            task_key=f"training-data:{source}:{target}",
            fallback_seconds=300.0 if target in {"temperature_c", "precipitation_mm"} else 180.0,
            detail={
                "target": target,
                "target_index": target_index,
                "target_total": len(config.hourly_forecasting.targets),
                "source": source,
                "horizons": len(config.hourly_forecasting.horizons_hours),
            },
        ):
            frame = _hourly_training_frame(
                session,
                config,
                target=target,
                source=source,
                bundle=bundle,
                profile_name=profile.name,
            )
            if frame.empty:
                stats.skipped += 1
                outputs.append({"target": target, "status": "empty", "source": source})
                continue
            manifest = _store_hourly_training_frame(
                repository,
                config,
                frame=frame,
                target=target,
                generated_at=generated_at,
                source=source,
                source_manifest=raw_pointer,
                training_profile=profile.name,
                training_policy=policy_name,
            )
            stats.inserted += 1
            stats.downloaded += len(frame)
            outputs.append(manifest)
    work.complete(name="hourly training datasets completed")
    stats.details = {
        "datasets": outputs,
        "source": source,
        "storage_backend": repository.store.backend_name,
        "forecast_mode": "horizon-conditioned-hourly",
        "training_profile": profile.name,
        "training_policy": policy_name,
        "flow_step": "02b-hourly-training-data-curated",
    }
    return stats


def materialize_hourly_training_frames_from_store(
    session: Session,
    config: AppConfig,
    progress: ProgressReporter | None = None,
) -> StageStats:
    return export_hourly_training_frames(
        session,
        config,
        source="object_store",
        progress=progress,
    )


def load_hourly_training_frame_from_store(
    config: AppConfig,
    *,
    target: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    repository = create_artifact_repository(config)
    pointer = repository.get_json(repository.layout.latest_hourly_feature_pointer(target))
    frame = repository.get_dataframe_csv_gzip(str(pointer["object_key"]))
    frame, validation = validate_frame(
        frame,
        "hourly_training_frame",
        config,
        context={
            "dataset_id": pointer.get("dataset_id"),
            "object_key": pointer.get("object_key"),
            "target": target,
            "phase": "load-hourly-from-object-store",
        },
    )
    return frame, {**pointer, "load_validation": validation.to_dict()}
