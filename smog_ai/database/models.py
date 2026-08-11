from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(UTC)


def uuid_str() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    running = "running"
    success = "success"
    partial_success = "partial_success"
    failed = "failed"
    skipped_locked = "skipped_locked"


class OutboxStatus(str, enum.Enum):
    pending = "pending"
    sending = "sending"
    published = "published"
    failed = "failed"
    dead_letter = "dead_letter"


class AirStation(Base):
    __tablename__ = "air_stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="GIOS", nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    station_code: Mapped[str | None] = mapped_column(String(128))
    station_name: Mapped[str] = mapped_column(String(255), nullable=False)
    city_name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    sensors: Mapped[list["AirSensor"]] = relationship(back_populates="station", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_air_station_source_id"),
        Index("ix_air_stations_location", "latitude", "longitude"),
    )


class AirSensor(Base):
    __tablename__ = "air_sensors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="GIOS", nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    air_station_id: Mapped[int] = mapped_column(ForeignKey("air_stations.id", ondelete="CASCADE"), nullable=False)
    parameter_code: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_name: Mapped[str | None] = mapped_column(String(255))
    formula: Mapped[str | None] = mapped_column(String(64))
    source_parameter_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    station: Mapped[AirStation] = relationship(back_populates="sensors")

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_air_sensor_source_id"),
        Index("ix_air_sensors_station_parameter", "air_station_id", "parameter_code"),
    )


class AirMeasurement(Base):
    __tablename__ = "air_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="GIOS", nullable=False)
    air_station_id: Mapped[int] = mapped_column(ForeignKey("air_stations.id", ondelete="CASCADE"), nullable=False)
    air_sensor_id: Mapped[int] = mapped_column(ForeignKey("air_sensors.id", ondelete="CASCADE"), nullable=False)
    source_station_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_sensor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parameter: Mapped[str] = mapped_column(String(64), nullable=False)
    measurement_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32), default="µg/m³", nullable=False)
    source_status: Mapped[str | None] = mapped_column(String(128))
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_sensor_id",
            "parameter",
            "measurement_time",
            name="uq_air_measurement_natural_key",
        ),
        Index("ix_air_measurements_station_parameter_time", "air_station_id", "parameter", "measurement_time"),
        Index("ix_air_measurements_time", "measurement_time"),
    )


class WeatherStation(Base):
    __tablename__ = "weather_stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="IMGW", nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    station_name: Mapped[str] = mapped_column(String(255), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    elevation_m: Mapped[float | None] = mapped_column(Float)
    metadata_source: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_weather_station_source_id"),
        Index("ix_weather_stations_location", "latitude", "longitude"),
    )


class WeatherMeasurement(Base):
    __tablename__ = "weather_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="IMGW", nullable=False)
    weather_station_id: Mapped[int] = mapped_column(ForeignKey("weather_stations.id", ondelete="CASCADE"), nullable=False)
    source_station_id: Mapped[str] = mapped_column(String(128), nullable=False)
    measurement_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    temperature_c: Mapped[float | None] = mapped_column(Float)
    humidity_percent: Mapped[float | None] = mapped_column(Float)
    pressure_hpa: Mapped[float | None] = mapped_column(Float)
    precipitation_mm: Mapped[float | None] = mapped_column(Float)
    precipitation_accumulation_period_hours: Mapped[int | None] = mapped_column(Integer)
    wind_speed_mps: Mapped[float | None] = mapped_column(Float)
    wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "source_station_id", "measurement_time", name="uq_weather_measurement_natural_key"),
        Index("ix_weather_measurements_station_time", "weather_station_id", "measurement_time"),
        Index("ix_weather_measurements_time", "measurement_time"),
    )


class StationMatch(Base):
    __tablename__ = "station_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    air_station_id: Mapped[int] = mapped_column(ForeignKey("air_stations.id", ondelete="CASCADE"), nullable=False)
    weather_station_id: Mapped[int] = mapped_column(ForeignKey("weather_stations.id", ondelete="CASCADE"), nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    matching_algorithm: Mapped[str] = mapped_column(String(128), default="haversine-v1", nullable=False)
    is_distance_acceptable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint("air_station_id", name="uq_station_match_air_station"),
        Index("ix_station_matches_weather_station", "weather_station_id"),
    )


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.running.value, nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(128))
    records_downloaded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warnings_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_collection_runs_started_at", "started_at"),)


class CollectionError(Base):
    __tablename__ = "collection_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("collection_runs.run_id", ondelete="SET NULL"))
    source: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(255))
    error_type: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_collection_errors_occurred_at", "occurred_at"),)


class DataQualityFlag(Base):
    __tablename__ = "data_quality_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    flag_code: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_quality_flags_entity", "entity_type", "entity_id"),
        Index("ix_quality_flags_detected_at", "detected_at"),
    )


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(128), nullable=False)
    parameter: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast_horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(1000))
    feature_columns_json: Mapped[list[str] | None] = mapped_column(JSON)
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    training_data_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    training_data_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("parameter", "forecast_horizon", "semantic_version", name="uq_model_version_semantic"),
        Index("ix_model_versions_active_lookup", "parameter", "forecast_horizon", "active"),
    )


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    parameter: Mapped[str | None] = mapped_column(String(64))
    forecast_horizon: Mapped[int | None] = mapped_column(Integer)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_train: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_validation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    best_model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id", ondelete="SET NULL"))
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)


class Forecast(Base):
    __tablename__ = "forecasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    model_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    air_station_id: Mapped[int] = mapped_column(ForeignKey("air_stations.id", ondelete="CASCADE"), nullable=False)
    parameter: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_origin_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_value: Mapped[float] = mapped_column(Float, nullable=False)
    features_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "model_version_id",
            "air_station_id",
            "parameter",
            "forecast_origin_time",
            "target_time",
            "forecast_horizon",
            name="uq_forecast_natural_key",
        ),
        Index("ix_forecasts_target_time", "target_time"),
        Index("ix_forecasts_station_parameter", "air_station_id", "parameter"),
    )


class ForecastResult(Base):
    __tablename__ = "forecast_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id", ondelete="CASCADE"), nullable=False, unique=True)
    actual_value: Mapped[float | None] = mapped_column(Float)
    signed_error: Mapped[float | None] = mapped_column(Float)
    absolute_error: Mapped[float | None] = mapped_column(Float)
    squared_error: Mapped[float | None] = mapped_column(Float)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_status: Mapped[str] = mapped_column(String(64), default="pending", nullable=False)
    matched_measurement_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_forecast_results_status", "verification_status"),)


class PublicationOutbox(Base):
    __tablename__ = "publication_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payload_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload_type: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=OutboxStatus.pending.value, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_outbox_status_next", "status", "next_attempt_at"),
        CheckConstraint("attempt_count >= 0", name="ck_outbox_attempt_count_nonnegative"),
    )


class PublishedSnapshot(Base):
    __tablename__ = "published_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_version: Mapped[str | None] = mapped_column(String(255))
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source_host_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationState(Base):
    __tablename__ = "application_state"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class ProcessLock(Base):
    __tablename__ = "process_locks"

    lock_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)
    host_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_token: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
