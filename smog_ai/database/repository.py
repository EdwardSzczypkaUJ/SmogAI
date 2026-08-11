from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import Select, and_, func, or_, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from smog_ai.database.models import (
    AirMeasurement,
    AirSensor,
    AirStation,
    ApplicationState,
    CollectionError,
    CollectionRun,
    DataQualityFlag,
    Forecast,
    ForecastResult,
    ModelVersion,
    OutboxStatus,
    PublicationOutbox,
    PublishedSnapshot,
    RunStatus,
    StationMatch,
    WeatherMeasurement,
    WeatherStation,
    now_utc,
)
from smog_ai.domain import (
    AirMeasurementRecord,
    AirSensorRecord,
    AirStationRecord,
    WeatherMeasurementRecord,
    WeatherStationRecord,
)
from smog_ai.errors import DatabaseError


SQLITE_EXECUTEMANY_BATCH_SIZE = 1000


def _insert_ignore_in_batches(
    session: Session,
    model: type[Any],
    rows: list[dict[str, Any]],
    *,
    conflict_columns: list[str],
    batch_size: int = SQLITE_EXECUTEMANY_BATCH_SIZE,
) -> tuple[int, int]:
    """Insert SQLite rows idempotently without constructing one huge SQL statement.

    ``sqlite_insert(Model).values(rows)`` expands every value into a separate
    bind parameter.  A monthly IMGW archive contains tens of thousands of rows,
    so a single statement can exceed SQLite's ``SQLITE_MAX_VARIABLE_NUMBER``.
    Passing batches as the second argument to ``Session.execute`` uses DB-API
    executemany semantics: each execution contains only one row worth of bind
    variables, while ``ON CONFLICT DO NOTHING`` keeps the operation idempotent.
    """

    if not rows:
        return 0, 0
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    statement = sqlite_insert(model).on_conflict_do_nothing(
        index_elements=conflict_columns
    )
    inserted = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        result = session.connection().execute(statement, batch)
        rowcount = result.rowcount
        if rowcount is None or rowcount < 0:
            # SQLite/Python normally reports an exact executemany rowcount.
            # Failing loudly is safer than marking duplicates as inserted and
            # corrupting collection statistics on an unsupported driver.
            raise DatabaseError(
                "SQLite driver did not report the number of inserted rows "
                "for an idempotent batch"
            )
        inserted += int(rowcount)

    return inserted, len(rows) - inserted


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def start_run(session: Session, run_type: str) -> CollectionRun:
    run = CollectionRun(run_type=run_type, status=RunStatus.running.value)
    session.add(run)
    session.flush()
    return run


def update_run_stage(session: Session, run_id: str, stage: str) -> None:
    session.execute(update(CollectionRun).where(CollectionRun.run_id == run_id).values(current_stage=stage))
    session.flush()


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    downloaded: int = 0,
    inserted: int = 0,
    skipped: int = 0,
    warnings: int = 0,
    errors: int = 0,
    summary: dict[str, Any] | None = None,
) -> None:
    session.execute(
        update(CollectionRun)
        .where(CollectionRun.run_id == run_id)
        .values(
            status=status,
            finished_at=now_utc(),
            records_downloaded=downloaded,
            records_inserted=inserted,
            records_skipped=skipped,
            warnings_count=warnings,
            errors_count=errors,
            summary_json=summary,
        )
    )
    session.flush()


def add_collection_error(
    session: Session,
    *,
    run_id: str | None,
    source: str | None,
    stage: str,
    error: Exception,
    entity_id: str | None = None,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        CollectionError(
            run_id=run_id,
            source=source,
            stage=stage,
            entity_id=entity_id,
            error_type=type(error).__name__,
            message=str(error),
            retryable=retryable,
            details_json=details,
        )
    )


def upsert_air_station(session: Session, record: AirStationRecord) -> AirStation:
    station = session.scalar(
        select(AirStation).where(AirStation.source == "GIOS", AirStation.source_id == record.source_id)
    )
    values = {
        "station_code": record.station_code,
        "station_name": record.station_name,
        "city_name": record.city_name,
        "address": record.address,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "raw_json": record.raw_json,
        "active": True,
        "updated_at": now_utc(),
    }
    if station is None:
        station = AirStation(source="GIOS", source_id=record.source_id, **values)
        session.add(station)
        session.flush()
    else:
        for key, value in values.items():
            setattr(station, key, value)
    return station


def upsert_air_sensor(session: Session, record: AirSensorRecord) -> AirSensor:
    station = session.scalar(
        select(AirStation).where(AirStation.source == "GIOS", AirStation.source_id == record.station_source_id)
    )
    if station is None:
        raise DatabaseError(f"Air station not found for sensor: {record.station_source_id}")
    sensor = session.scalar(
        select(AirSensor).where(AirSensor.source == "GIOS", AirSensor.source_id == record.source_id)
    )
    values = {
        "air_station_id": station.id,
        "parameter_code": record.parameter_code,
        "parameter_name": record.parameter_name,
        "formula": record.formula,
        "source_parameter_id": record.source_parameter_id,
        "raw_json": record.raw_json,
        "active": True,
        "updated_at": now_utc(),
    }
    if sensor is None:
        sensor = AirSensor(source="GIOS", source_id=record.source_id, **values)
        session.add(sensor)
        session.flush()
    else:
        for key, value in values.items():
            setattr(sensor, key, value)
    return sensor


def insert_air_measurements(session: Session, records: Iterable[AirMeasurementRecord]) -> tuple[int, int]:
    rows: list[dict[str, Any]] = []
    station_ids = {
        source_id: internal_id
        for source_id, internal_id in session.execute(select(AirStation.source_id, AirStation.id)).all()
    }
    sensor_ids = {
        source_id: internal_id
        for source_id, internal_id in session.execute(select(AirSensor.source_id, AirSensor.id)).all()
    }
    for record in records:
        station_id = station_ids.get(record.station_source_id)
        sensor_id = sensor_ids.get(record.sensor_source_id)
        if station_id is None or sensor_id is None:
            continue
        rows.append(
            {
                "source": "GIOS",
                "air_station_id": station_id,
                "air_sensor_id": sensor_id,
                "source_station_id": record.station_source_id,
                "source_sensor_id": record.sensor_source_id,
                "parameter": record.parameter,
                "measurement_time": as_utc(record.measurement_time),
                "value": record.value,
                "unit": record.unit,
                "source_status": record.source_status,
                "is_valid": record.value is not None,
                "raw_json": record.raw_json,
                "collected_at": now_utc(),
            }
        )
    return _insert_ignore_in_batches(
        session,
        AirMeasurement,
        rows,
        conflict_columns=[
            "source",
            "source_sensor_id",
            "parameter",
            "measurement_time",
        ],
    )


def upsert_weather_station(session: Session, record: WeatherStationRecord) -> WeatherStation:
    station = session.scalar(
        select(WeatherStation).where(WeatherStation.source == "IMGW", WeatherStation.source_id == record.source_id)
    )
    values = {
        "station_name": record.station_name,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "elevation_m": record.elevation_m,
        "metadata_source": record.metadata_source,
        "raw_json": record.raw_json,
        "active": True,
        "updated_at": now_utc(),
    }
    if station is None:
        station = WeatherStation(source="IMGW", source_id=record.source_id, **values)
        session.add(station)
        session.flush()
    else:
        # Never replace known coordinates with null values returned by the live endpoint.
        for key, value in values.items():
            if key in {"latitude", "longitude", "elevation_m"} and value is None:
                continue
            setattr(station, key, value)
    return station


def insert_weather_measurements(session: Session, records: Iterable[WeatherMeasurementRecord]) -> tuple[int, int]:
    rows: list[dict[str, Any]] = []
    station_ids = {
        source_id: internal_id
        for source_id, internal_id in session.execute(select(WeatherStation.source_id, WeatherStation.id)).all()
    }
    for record in records:
        station_id = station_ids.get(record.station_source_id)
        if station_id is None:
            continue
        rows.append(
            {
                "source": "IMGW",
                "weather_station_id": station_id,
                "source_station_id": record.station_source_id,
                "measurement_time": as_utc(record.measurement_time),
                "temperature_c": record.temperature_c,
                "humidity_percent": record.humidity_percent,
                "pressure_hpa": record.pressure_hpa,
                "precipitation_mm": record.precipitation_mm,
                "precipitation_accumulation_period_hours": (
                    record.precipitation_accumulation_period_hours
                ),
                "wind_speed_mps": record.wind_speed_mps,
                "wind_direction_deg": record.wind_direction_deg,
                "is_valid": True,
                "raw_json": record.raw_json,
                "collected_at": now_utc(),
            }
        )
    return _insert_ignore_in_batches(
        session,
        WeatherMeasurement,
        rows,
        conflict_columns=["source", "source_station_id", "measurement_time"],
    )



def merge_weather_measurements(
    session: Session,
    records: Iterable[WeatherMeasurementRecord],
    *,
    batch_size: int = 500,
) -> tuple[int, int, int]:
    """Insert new IMGW observations and fill only null fields on conflicts.

    Historical IMGW sources may expose different subsets of one station-hour
    record.  ``ON CONFLICT DO NOTHING`` protects against duplicates but cannot
    fill a temperature/pressure/precipitation field that was null in an earlier
    live observation.  This merge keeps every already-known value immutable and
    only fills missing columns from the later official archive.

    Returns ``(inserted, updated, unchanged)``.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    station_ids = {
        str(source_id): int(internal_id)
        for source_id, internal_id in session.execute(
            select(WeatherStation.source_id, WeatherStation.id)
        ).all()
    }
    rows: list[dict[str, Any]] = []
    for record in records:
        station_id = station_ids.get(str(record.station_source_id))
        if station_id is None:
            continue
        rows.append(
            {
                "source": "IMGW",
                "weather_station_id": station_id,
                "source_station_id": str(record.station_source_id),
                "measurement_time": as_utc(record.measurement_time),
                "temperature_c": record.temperature_c,
                "humidity_percent": record.humidity_percent,
                "pressure_hpa": record.pressure_hpa,
                "precipitation_mm": record.precipitation_mm,
                "precipitation_accumulation_period_hours": (
                    record.precipitation_accumulation_period_hours
                ),
                "wind_speed_mps": record.wind_speed_mps,
                "wind_direction_deg": record.wind_direction_deg,
                "is_valid": True,
                "raw_json": record.raw_json,
                "collected_at": now_utc(),
            }
        )

    inserted_total = 0
    updated_total = 0
    unchanged_total = 0
    fill_fields = (
        "temperature_c",
        "humidity_percent",
        "pressure_hpa",
        "precipitation_mm",
        "precipitation_accumulation_period_hours",
        "wind_speed_mps",
        "wind_direction_deg",
    )

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        keys = [
            (row["source_station_id"], row["measurement_time"])
            for row in batch
        ]
        existing_rows = session.scalars(
            select(WeatherMeasurement).where(
                WeatherMeasurement.source == "IMGW",
                tuple_(
                    WeatherMeasurement.source_station_id,
                    WeatherMeasurement.measurement_time,
                ).in_(keys),
            )
        ).all()
        existing = {
            (
                str(item.source_station_id),
                as_utc(item.measurement_time),
            ): item
            for item in existing_rows
        }

        new_rows: list[dict[str, Any]] = []
        for row in batch:
            key = (
                str(row["source_station_id"]),
                as_utc(row["measurement_time"]),
            )
            current = existing.get(key)
            if current is None:
                new_rows.append(row)
                continue

            changed = False
            for field in fill_fields:
                incoming = row[field]
                if getattr(current, field) is None and incoming is not None:
                    setattr(current, field, incoming)
                    changed = True
            if changed:
                current.is_valid = True
                current.collected_at = now_utc()
                if current.raw_json is None and row.get("raw_json") is not None:
                    current.raw_json = row["raw_json"]
                updated_total += 1
            else:
                unchanged_total += 1

        if new_rows:
            inserted, skipped = _insert_ignore_in_batches(
                session,
                WeatherMeasurement,
                new_rows,
                conflict_columns=[
                    "source",
                    "source_station_id",
                    "measurement_time",
                ],
                batch_size=min(batch_size, SQLITE_EXECUTEMANY_BATCH_SIZE),
            )
            inserted_total += inserted
            # A concurrent writer can win between the SELECT and INSERT. Count
            # those rows as unchanged rather than reporting a duplicate insert.
            unchanged_total += skipped
        session.flush()

    return inserted_total, updated_total, unchanged_total


def set_application_state(session: Session, key: str, value: Any) -> None:
    statement = sqlite_insert(ApplicationState).values(key=key, value_json=value, updated_at=now_utc())
    statement = statement.on_conflict_do_update(
        index_elements=["key"], set_={"value_json": value, "updated_at": now_utc()}
    )
    session.execute(statement)


def get_application_state(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(ApplicationState, key)
    return default if row is None else row.value_json


def add_quality_flag(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    flag_code: str,
    severity: str,
    message: str,
    metadata: dict[str, Any] | None = None,
    dedup_hours: int = 24,
) -> bool:
    cutoff = now_utc() - timedelta(hours=dedup_hours)
    exists = session.scalar(
        select(DataQualityFlag.id).where(
            DataQualityFlag.entity_type == entity_type,
            DataQualityFlag.entity_id == entity_id,
            DataQualityFlag.flag_code == flag_code,
            DataQualityFlag.detected_at >= cutoff,
            DataQualityFlag.resolved_at.is_(None),
        )
    )
    if exists is not None:
        return False
    session.add(
        DataQualityFlag(
            entity_type=entity_type,
            entity_id=entity_id,
            flag_code=flag_code,
            severity=severity,
            message=message,
            metadata_json=metadata,
        )
    )
    return True


def upsert_station_match(
    session: Session,
    *,
    air_station_id: int,
    weather_station_id: int,
    distance_km: float,
    acceptable: bool,
    algorithm: str = "haversine-v1",
) -> StationMatch:
    match = session.scalar(select(StationMatch).where(StationMatch.air_station_id == air_station_id))
    if match is None:
        match = StationMatch(
            air_station_id=air_station_id,
            weather_station_id=weather_station_id,
            distance_km=distance_km,
            is_distance_acceptable=acceptable,
            matching_algorithm=algorithm,
        )
        session.add(match)
    else:
        match.weather_station_id = weather_station_id
        match.distance_km = distance_km
        match.is_distance_acceptable = acceptable
        match.matching_algorithm = algorithm
        match.matched_at = now_utc()
    session.flush()
    return match


def add_forecast_idempotent(session: Session, values: dict[str, Any]) -> bool:
    statement = sqlite_insert(Forecast).values(**values).on_conflict_do_nothing(
        index_elements=[
            "model_version_id",
            "air_station_id",
            "parameter",
            "forecast_origin_time",
            "target_time",
            "forecast_horizon",
        ]
    )
    result = session.execute(statement)
    return bool(result.rowcount)


def ensure_forecast_result(session: Session, forecast_id: str) -> ForecastResult:
    result = session.scalar(select(ForecastResult).where(ForecastResult.forecast_id == forecast_id))
    if result is None:
        result = ForecastResult(forecast_id=forecast_id, verification_status="pending")
        session.add(result)
        session.flush()
    return result


def enqueue_publication(
    session: Session,
    *,
    publication_id: str,
    payload_path: Path,
    payload_type: str,
    checksum: str,
) -> PublicationOutbox:
    row = session.scalar(select(PublicationOutbox).where(PublicationOutbox.publication_id == publication_id))
    if row is not None:
        return row
    row = PublicationOutbox(
        publication_id=publication_id,
        payload_path=str(payload_path),
        payload_type=payload_type,
        checksum=checksum,
        status=OutboxStatus.pending.value,
        next_attempt_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def due_outbox_query(now: datetime | None = None) -> Select[tuple[PublicationOutbox]]:
    reference = as_utc(now or now_utc())
    return (
        select(PublicationOutbox)
        .where(
            PublicationOutbox.status.in_(
                [OutboxStatus.pending.value, OutboxStatus.failed.value, OutboxStatus.sending.value]
            ),
            or_(PublicationOutbox.next_attempt_at.is_(None), PublicationOutbox.next_attempt_at <= reference),
        )
        .order_by(PublicationOutbox.created_at)
    )


def register_published_snapshot(
    session: Session,
    *,
    publication_id: str,
    schema_version: str,
    generated_at: datetime,
    data_start: datetime | None,
    data_end: datetime | None,
    model_version: str | None,
    record_count: int,
    checksum: str,
    source_host_id: str,
    payload_path: str,
) -> PublishedSnapshot:
    row = session.scalar(select(PublishedSnapshot).where(PublishedSnapshot.publication_id == publication_id))
    if row is None:
        row = PublishedSnapshot(
            publication_id=publication_id,
            schema_version=schema_version,
            generated_at=generated_at,
            data_start=data_start,
            data_end=data_end,
            model_version=model_version,
            record_count=record_count,
            checksum=checksum,
            source_host_id=source_host_id,
            payload_path=payload_path,
        )
        session.add(row)
        session.flush()
    return row


def latest_timestamp(session: Session, model: type[Any], column: Any) -> datetime | None:
    value = session.scalar(select(func.max(column)))
    return as_utc(value) if value is not None else None
