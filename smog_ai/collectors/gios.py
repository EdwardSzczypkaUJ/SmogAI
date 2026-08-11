from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from smog_ai.collectors.parsing import find_collection, get_alias, to_float, to_str
from smog_ai.air_parameters import canonical_code, create_air_parameter_registry
from smog_ai.config import AppConfig
from smog_ai.database.repository import (
    add_collection_error,
    insert_air_measurements,
    set_application_state,
    upsert_air_sensor,
    upsert_air_station,
)
from smog_ai.domain import AirMeasurementRecord, AirSensorRecord, AirStationRecord, StageStats
from smog_ai.errors import DataValidationError, ExternalAPIError, ExternalAPIStatusError
from smog_ai.http_client import ResilientHttpClient
from smog_ai.progress import ProgressReporter
from smog_ai.time_utils import parse_datetime, utc_now

logger = logging.getLogger(__name__)

GIOS_JSON_LD_HEADERS = {"Accept": "application/ld+json"}
# GIOŚ keeps historical sensor identifiers in station metadata even when the
# current-data endpoint no longer exposes a live series for them.  In practice
# those identifiers answer 400/404.  They are auditable source gaps, not a reason
# to mark the whole nationwide collection as failed.
EXPECTED_UNAVAILABLE_STATUS_CODES = {400, 404}


def normalize_parameter(value: str | None) -> str:
    """Backward-compatible canonicalisation shared with the registry."""

    return canonical_code(value)


class GiosCollector:
    """Client for the official GIOŚ Jakość Powietrza v1 REST API.

    Current v1 endpoints publish JSON-LD and require a compatible ``Accept``
    header.  The parser retains aliases for older fixtures/local archives, while
    preferring the current Polish field names from the official OpenAPI schema.
    """

    def __init__(self, config: AppConfig, http: ResilientHttpClient | None = None) -> None:
        self.config = config
        self.parameter_registry = create_air_parameter_registry(config)
        self.http = http or ResilientHttpClient(config.api)
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def endpoint(self, path: str) -> str:
        return f"{self.config.api.gios_base_url}/{path.lstrip('/')}"

    @staticmethod
    def _total_pages(payload: Any) -> int | None:
        if not isinstance(payload, Mapping):
            return None
        raw = get_alias(payload, "totalPages", "total_pages", "Liczba stron")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def fetch_page(
        self,
        path: str,
        *,
        preferred_keys: tuple[str, ...],
        page: int = 0,
        size: int | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[list[Any], Any]:
        page_size = min(max(1, size or self.config.api.gios_page_size), 500)
        query = {**dict(params or {}), "page": page, "size": page_size}
        payload = self.http.get_json(
            self.endpoint(path),
            params=query,
            headers=GIOS_JSON_LD_HEADERS,
        )
        return find_collection(payload, preferred_keys), payload

    def fetch_paged(
        self,
        path: str,
        *,
        preferred_keys: tuple[str, ...],
        params: Mapping[str, Any] | None = None,
    ) -> list[Any]:
        page = 0
        rows: list[Any] = []
        size = min(max(1, self.config.api.gios_page_size), 500)
        while page < 1000:
            current, payload = self.fetch_page(
                path,
                preferred_keys=preferred_keys,
                page=page,
                size=size,
                params=params,
            )
            rows.extend(current)
            total_pages = self._total_pages(payload)
            if total_pages is not None and page + 1 >= total_pages:
                break
            if not current or len(current) < size:
                break
            page += 1
            time.sleep(self.config.api.gios_request_interval_seconds)
        return rows

    def probe(self) -> dict[str, Any]:
        """Perform a lightweight live contract check without writing to the DB."""
        rows, payload = self.fetch_page(
            "station/findAll",
            preferred_keys=("Lista stacji pomiarowych", "stations"),
            page=0,
            size=1,
        )
        if not rows:
            raise ExternalAPIError("GIOŚ probe returned no station rows")
        item = rows[0] if isinstance(rows[0], Mapping) else {}
        station_id = to_str(
            get_alias(item, "id", "stationId", "Identyfikator stacji", "Id stacji")
        )
        return {
            "status": "ok",
            "api": "GIOS v1 JSON-LD",
            "accept": GIOS_JSON_LD_HEADERS["Accept"],
            "station_count_on_page": len(rows),
            "sample_station_id": station_id,
            "total_pages": self._total_pages(payload),
        }

    def fetch_stations(self) -> list[AirStationRecord]:
        rows = self.fetch_paged(
            "station/findAll",
            preferred_keys=("Lista stacji pomiarowych", "stations"),
        )
        parsed: list[AirStationRecord] = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            source_id = to_str(
                get_alias(item, "id", "stationId", "Identyfikator stacji", "Id stacji")
            )
            name = to_str(get_alias(item, "stationName", "Nazwa stacji", "nazwa"))
            if not source_id or not name:
                logger.warning("Skipping GIOŚ station without id/name", extra={"stage": "parse_station"})
                continue
            city_obj = get_alias(item, "city", "Miasto")
            city_name = None
            if isinstance(city_obj, Mapping):
                city_name = to_str(get_alias(city_obj, "name", "cityName", "Nazwa miasta"))
            city_name = city_name or to_str(get_alias(item, "cityName", "Nazwa miasta"))
            address_obj = get_alias(item, "addressStreet", "Ulica", "Adres")
            if isinstance(address_obj, Mapping):
                address_obj = get_alias(address_obj, "name", "value")
            parsed.append(
                AirStationRecord(
                    source_id=source_id,
                    station_code=to_str(get_alias(item, "stationCode", "Kod stacji")),
                    station_name=name,
                    city_name=city_name,
                    address=to_str(address_obj),
                    latitude=to_float(
                        get_alias(item, "gegrLat", "latitude", "WGS84 φ N", "WGS84 N", "Szerokość geograficzna")
                    ),
                    longitude=to_float(
                        get_alias(item, "gegrLon", "longitude", "WGS84 λ E", "WGS84 E", "Długość geograficzna")
                    ),
                    raw_json=dict(item),
                )
            )
        return parsed

    def fetch_sensors(self, station_source_id: str) -> list[AirSensorRecord]:
        rows = self.fetch_paged(
            f"station/sensors/{station_source_id}",
            preferred_keys=(
                "Lista stanowisk pomiarowych dla podanej stacji",
                "Lista stanowisk pomiarowych",
                "sensors",
            ),
        )
        parsed: list[AirSensorRecord] = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            source_id = to_str(
                get_alias(item, "id", "sensorId", "Identyfikator stanowiska", "Id stanowiska")
            )
            station_id = to_str(
                get_alias(item, "stationId", "Identyfikator stacji", "Id stacji")
            ) or station_source_id
            param_obj = get_alias(item, "param", "parameter")
            param_mapping = param_obj if isinstance(param_obj, Mapping) else item
            code = self.parameter_registry.resolve(
                to_str(
                    get_alias(
                        param_mapping,
                        "paramCode",
                        "code",
                        "Wskaźnik - kod",
                        "Kod wskaźnika",
                        "Wskaźnik - wzór",
                        "Wzór wskaźnika",
                        "formula",
                    )
                ),
                allow_unknown=True,
            )
            if code == "UNKNOWN":
                code = self.parameter_registry.resolve(
                    to_str(get_alias(item, "Wskaźnik - wzór", "formula", "Wzór wskaźnika")),
                    allow_unknown=True,
                )
            if not source_id:
                continue
            parsed.append(
                AirSensorRecord(
                    source_id=source_id,
                    station_source_id=station_id,
                    parameter_code=code,
                    parameter_name=to_str(
                        get_alias(param_mapping, "paramName", "name", "Wskaźnik", "Nazwa wskaźnika")
                    ),
                    formula=to_str(
                        get_alias(param_mapping, "paramFormula", "formula", "Wskaźnik - wzór", "Wzór wskaźnika")
                    ),
                    source_parameter_id=to_str(
                        get_alias(param_mapping, "idParam", "parameterId", "Id wskaźnika")
                    ),
                    raw_json=dict(item),
                )
            )
        return parsed

    def fetch_measurements(self, sensor: AirSensorRecord) -> list[AirMeasurementRecord]:
        rows = self.fetch_paged(
            f"data/getData/{sensor.source_id}",
            preferred_keys=("Lista danych pomiarowych", "values", "data"),
        )
        parameter = self.parameter_registry.resolve(
            sensor.parameter_code, allow_unknown=True
        )
        definition = self.parameter_registry.get(parameter)
        unit = definition.canonical_unit if definition is not None else "µg/m³"
        parsed: list[AirMeasurementRecord] = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            date_value = to_str(
                get_alias(item, "date", "measurementDate", "Data", "Data pomiaru", "Czas pomiaru")
            )
            if not date_value:
                continue
            try:
                measurement_time = parse_datetime(
                    date_value, naive_zone=self.config.api.gios_naive_time_zone
                )
            except DataValidationError:
                logger.warning("Skipping GIOŚ measurement with invalid time", extra={"stage": "parse_measurement"})
                continue
            parsed.append(
                AirMeasurementRecord(
                    station_source_id=sensor.station_source_id,
                    sensor_source_id=sensor.source_id,
                    parameter=parameter,
                    measurement_time=measurement_time,
                    value=to_float(get_alias(item, "value", "Wartość", "Wynik")),
                    unit=unit,
                    source_status=to_str(get_alias(item, "status", "quality", "Status", "Kod jakości")),
                    raw_json=dict(item),
                )
            )
        return parsed


def collect_gios(
    session: Session,
    config: AppConfig,
    *,
    run_id: str | None = None,
    http: ResilientHttpClient | None = None,
    parameters: Iterable[str] | None = None,
    progress: ProgressReporter | None = None,
) -> StageStats:
    stats = StageStats()
    collector = GiosCollector(config, http=http)
    registry = collector.parameter_registry
    selected_parameters = set(
        registry.normalise_many(parameters, require_configured=True)
        if parameters is not None
        else registry.collection_codes
    )
    collect_unknown = parameters is None and registry.unknown_policy == "collect"
    unavailable_measurement_endpoints = 0
    unavailable_sensor_endpoints = 0
    try:
        stations = collector.fetch_stations()
        stats.downloaded += len(stations)
        for station in stations:
            upsert_air_station(session, station)
        session.flush()

        all_measurements: list[AirMeasurementRecord] = []
        sensor_count = 0
        selected_sensor_count = 0
        skipped_by_parameter_policy = 0
        processed_selected_sensors = 0
        if progress is not None:
            progress.update(
                "collection",
                0.0,
                task="GIOŚ: station and sensor catalogue",
                detail={
                    "source": "GIOS",
                    "selected_parameters": sorted(selected_parameters),
                },
                force=True,
            )
        for station in stations:
            try:
                sensors = collector.fetch_sensors(station.source_id)
                sensor_count += len(sensors)
                stats.downloaded += len(sensors)
                for sensor in sensors:
                    upsert_air_sensor(session, sensor)
                session.flush()
                for sensor in sensors:
                    parameter = registry.resolve(
                        sensor.parameter_code, allow_unknown=True
                    )
                    should_collect = (
                        parameter in selected_parameters
                        or (collect_unknown and parameter != "UNKNOWN")
                    )
                    if not should_collect:
                        skipped_by_parameter_policy += 1
                        continue
                    selected_sensor_count += 1
                    try:
                        measurements = collector.fetch_measurements(sensor)
                        stats.downloaded += len(measurements)
                        all_measurements.extend(measurements)
                    except ExternalAPIStatusError as exc:
                        if exc.status_code in EXPECTED_UNAVAILABLE_STATUS_CODES:
                            unavailable_measurement_endpoints += 1
                            stats.skipped += 1
                            stats.warnings += 1
                            add_collection_error(
                                session,
                                run_id=run_id,
                                source="GIOS",
                                stage="collect_measurements_unavailable",
                                error=exc,
                                entity_id=sensor.source_id,
                                retryable=False,
                            )
                            continue
                        stats.errors += 1
                        add_collection_error(
                            session,
                            run_id=run_id,
                            source="GIOS",
                            stage="collect_measurements",
                            error=exc,
                            entity_id=sensor.source_id,
                            retryable=exc.status_code in {408, 425, 429, 500, 502, 503, 504},
                        )
                    except Exception as exc:
                        stats.errors += 1
                        add_collection_error(
                            session,
                            run_id=run_id,
                            source="GIOS",
                            stage="collect_measurements",
                            error=exc,
                            entity_id=sensor.source_id,
                            retryable=True,
                        )
                    processed_selected_sensors += 1
                    if progress is not None:
                        # Total selected sensors is discovered progressively; use
                        # catalogue sensors seen so far as a monotone lower-bound
                        # denominator and finish at 100% after all stations.
                        denominator = max(1, selected_sensor_count)
                        progress.update(
                            "collection",
                            min(0.99, processed_selected_sensors / denominator),
                            task=(
                                f"GIOŚ: {parameter} sensor "
                                f"{processed_selected_sensors}/{denominator}"
                            ),
                            detail={
                                "source": "GIOS",
                                "parameter": parameter,
                                "sensor_source_id": sensor.source_id,
                                "processed_selected_sensors": processed_selected_sensors,
                                "selected_sensors_seen": selected_sensor_count,
                            },
                        )
                    time.sleep(config.api.gios_request_interval_seconds)
            except ExternalAPIStatusError as exc:
                if exc.status_code in EXPECTED_UNAVAILABLE_STATUS_CODES:
                    unavailable_sensor_endpoints += 1
                    stats.skipped += 1
                    stats.warnings += 1
                    add_collection_error(
                        session,
                        run_id=run_id,
                        source="GIOS",
                        stage="collect_sensors_unavailable",
                        error=exc,
                        entity_id=station.source_id,
                        retryable=False,
                    )
                    continue
                stats.errors += 1
                add_collection_error(
                    session,
                    run_id=run_id,
                    source="GIOS",
                    stage="collect_sensors",
                    error=exc,
                    entity_id=station.source_id,
                    retryable=exc.status_code in {408, 425, 429, 500, 502, 503, 504},
                )
            except Exception as exc:
                stats.errors += 1
                add_collection_error(
                    session,
                    run_id=run_id,
                    source="GIOS",
                    stage="collect_sensors",
                    error=exc,
                    entity_id=station.source_id,
                    retryable=True,
                )
        inserted, skipped = insert_air_measurements(session, all_measurements)
        stats.inserted += inserted
        stats.skipped += skipped
        stats.details.update(
            {
                "stations": len(stations),
                "sensors": sensor_count,
                "measurements": len(all_measurements),
                "parameters_requested": sorted(selected_parameters),
                "parameters_collected": sorted(
                    {measurement.parameter for measurement in all_measurements}
                ),
                "selected_sensors": selected_sensor_count,
                "skipped_by_parameter_policy": skipped_by_parameter_policy,
                "unknown_sensor_policy": registry.unknown_policy,
                "unavailable_measurement_endpoints": unavailable_measurement_endpoints,
                "unavailable_sensor_endpoints": unavailable_sensor_endpoints,
            }
        )
        set_application_state(session, "last_gios_success_at", utc_now().isoformat())
        if progress is not None:
            progress.complete_stage(
                "collection",
                task="GIOŚ collection completed",
                detail={
                    "source": "GIOS",
                    "selected_parameters": sorted(selected_parameters),
                    "parameters_collected": stats.details.get(
                        "parameters_collected", []
                    ),
                    "measurements": len(all_measurements),
                },
            )
        return stats
    except Exception as exc:
        stats.errors += 1
        add_collection_error(
            session,
            run_id=run_id,
            source="GIOS",
            stage="collect_gios",
            error=exc,
            retryable=isinstance(exc, (ExternalAPIError, TimeoutError)),
        )
        raise
    finally:
        collector.close()
