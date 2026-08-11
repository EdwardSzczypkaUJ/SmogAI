from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class AirStationRecord:
    source_id: str
    station_name: str
    station_code: str | None = None
    city_name: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    raw_json: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class AirSensorRecord:
    source_id: str
    station_source_id: str
    parameter_code: str
    parameter_name: str | None = None
    formula: str | None = None
    source_parameter_id: str | None = None
    raw_json: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class AirMeasurementRecord:
    station_source_id: str
    sensor_source_id: str
    parameter: str
    measurement_time: datetime
    value: float | None
    unit: str = "µg/m³"
    source_status: str | None = None
    raw_json: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class WeatherStationRecord:
    source_id: str
    station_name: str
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float | None = None
    metadata_source: str | None = None
    raw_json: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class WeatherMeasurementRecord:
    station_source_id: str
    station_name: str
    measurement_time: datetime
    temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    precipitation_mm: float | None = None
    precipitation_accumulation_period_hours: int | None = None
    wind_speed_mps: float | None = None
    wind_direction_deg: float | None = None
    raw_json: dict[str, Any] | None = None


@dataclass(slots=True)
class StageStats:
    downloaded: int = 0
    inserted: int = 0
    skipped: int = 0
    warnings: int = 0
    errors: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: "StageStats") -> "StageStats":
        self.downloaded += other.downloaded
        self.inserted += other.inserted
        self.skipped += other.skipped
        self.warnings += other.warnings
        self.errors += other.errors
        self.details.update(other.details)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "downloaded": self.downloaded,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "errors": self.errors,
            "details": self.details,
        }
