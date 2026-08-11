from __future__ import annotations

import csv
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.collectors.parsing import get_alias, normalize_name, to_float, to_str
from smog_ai.config import AppConfig
from smog_ai.database.models import AirStation, WeatherStation
from smog_ai.database.repository import (
    add_collection_error,
    add_quality_flag,
    insert_weather_measurements,
    set_application_state,
    upsert_weather_station,
)
from smog_ai.domain import StageStats, WeatherMeasurementRecord, WeatherStationRecord
from smog_ai.errors import DataValidationError, ExternalAPIError
from smog_ai.http_client import ResilientHttpClient
from smog_ai.time_utils import parse_imgw_observation, utc_now

logger = logging.getLogger(__name__)
OFFICIAL_METADATA_URL = "https://klimat.imgw.pl/pl/meta-dane/"


def load_station_metadata(path: Path) -> dict[str, WeatherStationRecord]:
    if not path.exists():
        return {}
    output: dict[str, WeatherStationRecord] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            source_id = (row.get("source_id") or row.get("id_stacji") or "").strip()
            if not source_id or source_id.startswith("#"):
                continue
            output[source_id] = WeatherStationRecord(
                source_id=source_id,
                station_name=(row.get("station_name") or row.get("stacja") or source_id).strip(),
                latitude=to_float(row.get("latitude")),
                longitude=to_float(row.get("longitude")),
                elevation_m=to_float(row.get("elevation_m")),
                metadata_source=row.get("metadata_source") or OFFICIAL_METADATA_URL,
                raw_json={"metadata_row": row},
            )
    return output


class ImgwCollector:
    def __init__(self, config: AppConfig, http: ResilientHttpClient | None = None) -> None:
        self.config = config
        self.http = http or ResilientHttpClient(config.api)
        self._owns_http = http is None
        self.metadata = load_station_metadata(config.paths.imgw_metadata_csv)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def fetch(self) -> tuple[list[WeatherStationRecord], list[WeatherMeasurementRecord]]:
        payload = self.http.get_json(self.config.api.imgw_synop_url)
        if not isinstance(payload, list):
            raise DataValidationError("IMGW SYNOP response must be a JSON array")
        stations: dict[str, WeatherStationRecord] = {}
        measurements: list[WeatherMeasurementRecord] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            source_id = to_str(get_alias(item, "id_stacji", "station_id", "id"))
            name = to_str(get_alias(item, "stacja", "station", "name"))
            date_value = to_str(get_alias(item, "data_pomiaru", "measurement_date", "date"))
            hour_value = get_alias(item, "godzina_pomiaru", "measurement_hour", "hour")
            if not source_id or not name or not date_value or hour_value is None:
                logger.warning("Skipping incomplete IMGW row", extra={"stage": "parse_imgw"})
                continue
            try:
                timestamp = parse_imgw_observation(
                    date_value,
                    str(hour_value),
                    naive_zone=self.config.api.imgw_naive_time_zone,
                )
            except (DataValidationError, ValueError):
                logger.warning("Skipping IMGW row with invalid timestamp", extra={"stage": "parse_imgw"})
                continue
            metadata = self.metadata.get(source_id)
            stations[source_id] = WeatherStationRecord(
                source_id=source_id,
                station_name=name,
                latitude=metadata.latitude if metadata else None,
                longitude=metadata.longitude if metadata else None,
                elevation_m=metadata.elevation_m if metadata else None,
                metadata_source=metadata.metadata_source if metadata else OFFICIAL_METADATA_URL,
                raw_json=dict(item),
            )
            measurements.append(
                WeatherMeasurementRecord(
                    station_source_id=source_id,
                    station_name=name,
                    measurement_time=timestamp,
                    temperature_c=to_float(get_alias(item, "temperatura", "temperature")),
                    humidity_percent=to_float(
                        get_alias(item, "wilgotnosc_wzgledna", "humidity", "relative_humidity")
                    ),
                    pressure_hpa=to_float(get_alias(item, "cisnienie", "pressure")),
                    precipitation_mm=to_float(get_alias(item, "suma_opadu", "precipitation")),
                    precipitation_accumulation_period_hours=(
                        self.config.api.imgw_live_precipitation_accumulation_period_hours
                    ),
                    wind_speed_mps=to_float(get_alias(item, "predkosc_wiatru", "wind_speed")),
                    wind_direction_deg=to_float(
                        get_alias(item, "kierunek_wiatru", "wind_direction")
                    ),
                    raw_json=dict(item),
                )
            )
        return list(stations.values()), measurements


def derive_missing_imgw_coordinates(session: Session) -> int:
    """Derive a transparent fallback coordinate from matching GIOŚ city names.

    This is not silently presented as official station metadata. It is explicitly marked as a
    city-centroid fallback and a quality flag is added. Install an official IMGW metadata export
    in ``paths.imgw_metadata_csv`` to replace these values.
    """

    air_rows = session.execute(
        select(AirStation.city_name, func.avg(AirStation.latitude), func.avg(AirStation.longitude))
        .where(
            AirStation.city_name.is_not(None),
            AirStation.latitude.is_not(None),
            AirStation.longitude.is_not(None),
        )
        .group_by(AirStation.city_name)
    ).all()
    centroids = {
        normalize_name(name): (float(lat), float(lon), name)
        for name, lat, lon in air_rows
        if name and lat is not None and lon is not None
    }
    updated = 0
    stations = session.scalars(
        select(WeatherStation).where(
            WeatherStation.latitude.is_(None) | WeatherStation.longitude.is_(None)
        )
    ).all()
    for station in stations:
        match = centroids.get(normalize_name(station.station_name))
        if match is None:
            continue
        station.latitude, station.longitude = match[0], match[1]
        station.metadata_source = "derived:gios-city-centroid"
        add_quality_flag(
            session,
            entity_type="weather_station",
            entity_id=str(station.id),
            flag_code="DERIVED_COORDINATES",
            severity="warning",
            message=(
                "IMGW coordinates were not present in the live SYNOP response; a GIOŚ city "
                "centroid fallback was used. Replace it with the official IMGW metadata export."
            ),
            metadata={"matched_city": match[2]},
            dedup_hours=24 * 365,
        )
        updated += 1
    return updated


def collect_imgw(
    session: Session,
    config: AppConfig,
    *,
    run_id: str | None = None,
    http: ResilientHttpClient | None = None,
) -> StageStats:
    stats = StageStats()
    collector = ImgwCollector(config, http=http)
    try:
        stations, measurements = collector.fetch()
        stats.downloaded = len(stations) + len(measurements)
        for station in stations:
            upsert_weather_station(session, station)
        session.flush()
        derived = derive_missing_imgw_coordinates(session)
        inserted, skipped = insert_weather_measurements(session, measurements)
        stats.inserted = inserted
        stats.skipped = skipped
        stats.details = {
            "stations": len(stations),
            "measurements": len(measurements),
            "derived_coordinate_fallbacks": derived,
        }
        set_application_state(session, "last_imgw_success_at", utc_now().isoformat())
        return stats
    except (ExternalAPIError, Exception) as exc:
        stats.errors += 1
        add_collection_error(
            session,
            run_id=run_id,
            source="IMGW",
            stage="collect_imgw",
            error=exc,
            retryable=True,
        )
        raise
    finally:
        collector.close()
