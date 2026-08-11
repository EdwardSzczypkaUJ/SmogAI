from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import re
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.collectors.gios import GIOS_JSON_LD_HEADERS, normalize_parameter
from smog_ai.collectors.history_cache import (
    HistoryCacheMode,
    create_historical_data_cache_bridge,
)
from smog_ai.collectors.parsing import find_collection, get_alias, to_float, to_str
from smog_ai.config import AppConfig
from smog_ai.database.models import AirMeasurement, AirSensor, AirStation
from smog_ai.database.repository import (
    insert_air_measurements,
    set_application_state,
    upsert_air_sensor,
    upsert_air_station,
)
from smog_ai.domain import AirMeasurementRecord, AirSensorRecord, AirStationRecord, StageStats
from smog_ai.errors import ExternalAPIError, ExternalAPIStatusError
from smog_ai.http_client import ResilientHttpClient

logger = logging.getLogger(__name__)
HistoryProgressCallback = Callable[[float, str, Mapping[str, Any]], None]

CET_FIXED = timezone(timedelta(hours=1))
DEFAULT_HISTORICAL_POLLUTANTS = ("PM10", "PM2.5")
# Backwards-compatible export. Runtime support comes from AirParameterRegistry.
SUPPORTED_POLLUTANTS = DEFAULT_HISTORICAL_POLLUTANTS
ALL_VOIVODESHIPS = (
    "DOLNOŚLĄSKIE",
    "KUJAWSKO-POMORSKIE",
    "LUBELSKIE",
    "LUBUSKIE",
    "ŁÓDZKIE",
    "MAŁOPOLSKIE",
    "MAZOWIECKIE",
    "OPOLSKIE",
    "PODKARPACKIE",
    "PODLASKIE",
    "POMORSKIE",
    "ŚLĄSKIE",
    "ŚWIĘTOKRZYSKIE",
    "WARMIŃSKO-MAZURSKIE",
    "WIELKOPOLSKIE",
    "ZACHODNIOPOMORSKIE",
)

# Current links exposed by the official GIOŚ Bank of Measurement Data page.
# The archive page is the source of truth; these values are fallbacks for an
# unavailable HTML discovery page.
PREPARED_ARCHIVE_URLS: dict[int, str] = {
    2020: "https://powietrze.gios.gov.pl/pjp/archives/downloadFile/424",
    2021: "https://powietrze.gios.gov.pl/pjp/archives/downloadFile/486",
    2022: "https://powietrze.gios.gov.pl/pjp/archives/downloadFile/524",
    2023: "https://powietrze.gios.gov.pl/pjp/archives/downloadFile/564",
    2024: "https://powietrze.gios.gov.pl/pjp/archives/downloadFile/582",
}
ARCHIVE_PAGE_URL = "https://powietrze.gios.gov.pl/pjp/archives"
ANNUAL_API_PATH = "archivalData/getDataForAllStationsByYearAndVoivodeship"
METADATA_STATIONS_PATH = "metadata/stations"
METADATA_SENSORS_PATH = "metadata/sensors"


@dataclass(slots=True, frozen=True)
class HistoryImportOptions:
    start_year: int
    end_year: int
    source: Literal["auto", "prepared", "api"] = "auto"
    pollutants: tuple[str, ...] = DEFAULT_HISTORICAL_POLLUTANTS
    voivodeships: tuple[str, ...] = ALL_VOIVODESHIPS
    request_interval_seconds: float = 31.0
    page_size: int = 500
    max_pages_per_combination: int = 0
    resume: bool = True
    refresh_cache: bool = False
    cache_dir: Path | None = None
    cache_mode: HistoryCacheMode | None = None
    cache_prefix: str | None = None
    insert_batch_size: int = 20_000
    # Optional right-open UTC intervals by pollutant. When provided, the
    # importer may still read a source object at its natural granularity
    # (annual ZIP/API pages), but only measurements inside these gaps are
    # persisted. This keeps cache/source strategy unchanged while avoiding
    # duplicate database work.
    intervals_by_pollutant: Mapping[
        str, tuple[tuple[datetime, datetime], ...]
    ] | None = None

    def validate(self) -> None:
        current_year = datetime.now(UTC).year
        if not 2000 <= self.start_year <= current_year:
            raise ValueError(f"start_year must be between 2000 and {current_year}")
        if not self.start_year <= self.end_year <= current_year:
            raise ValueError("end_year must not precede start_year or exceed current year")
        if self.source not in {"auto", "prepared", "api"}:
            raise ValueError("source must be auto, prepared or api")
        if self.page_size < 1 or self.page_size > 500:
            raise ValueError("page_size must be between 1 and 500")
        if self.request_interval_seconds < 30.0 and self.source in {"auto", "api"}:
            raise ValueError(
                "GIOŚ archival/metadata API is limited to 2 requests/minute; "
                "request_interval_seconds must be at least 30"
            )
        if not self.pollutants:
            raise ValueError("at least one historical pollutant is required")
        bad_regions = set(self.voivodeships) - set(ALL_VOIVODESHIPS)
        if bad_regions:
            raise ValueError(f"unsupported voivodeships: {sorted(bad_regions)}")
        if self.intervals_by_pollutant is not None:
            for parameter, intervals in self.intervals_by_pollutant.items():
                if not str(parameter).strip():
                    raise ValueError("interval pollutant cannot be empty")
                for start, end in intervals:
                    start_utc = (
                        start.replace(tzinfo=UTC)
                        if start.tzinfo is None
                        else start.astimezone(UTC)
                    )
                    end_utc = (
                        end.replace(tzinfo=UTC)
                        if end.tzinfo is None
                        else end.astimezone(UTC)
                    )
                    if end_utc <= start_utc:
                        raise ValueError(
                            f"invalid interval for {parameter}: {start}..{end}"
                        )
        if self.cache_mode not in {None, "local", "object_store", "hybrid"}:
            raise ValueError(
                "cache_mode must be local, object_store, hybrid or None"
            )
        if self.cache_prefix is not None and not self.cache_prefix.strip("/"):
            raise ValueError("cache_prefix cannot be empty")


@dataclass(slots=True)
class StationBinding:
    source_id: str
    station_code: str
    province: str | None
    current_code: str


@dataclass(slots=True)
class SensorBinding:
    source_id: str
    station_source_id: str
    parameter: str
    automatic_hourly: bool


@dataclass(slots=True)
class ParsedPreparedSeries:
    station_code: str
    parameter: str
    measurement_times: list[datetime]
    values: list[float | None]


@dataclass(slots=True)
class _ImportState:
    completed: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "_ImportState":
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        completed = payload.get("completed")
        return cls(completed=dict(completed) if isinstance(completed, Mapping) else {})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "schema_version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "completed": self.completed,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(path)


class _RateLimiter:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._last_request_started: float | None = None

    def wait(self, label: str) -> None:
        if self._last_request_started is None:
            self._last_request_started = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_started
        wait_seconds = self.interval_seconds - elapsed
        if wait_seconds > 0:
            logger.info(
                "GIOŚ history rate-limit wait",
                extra={"stage": "gios_history_wait", "label": label, "wait_seconds": round(wait_seconds, 1)},
            )
            time.sleep(wait_seconds)
        self._last_request_started = time.monotonic()


class GiosHistoryImporter:
    """Resumable importer for official GIOŚ hourly PM history.

    Older complete years are read from the prepared annual ZIP archives.  Years
    not available as a prepared ZIP can be read from the official annual,
    voivodeship and pollutant API.  All one-hour timestamps are interpreted as
    fixed CET (UTC+01:00), exactly as specified by GIOŚ, and stored in UTC.
    """

    def __init__(
        self,
        session: Session,
        config: AppConfig,
        options: HistoryImportOptions,
        *,
        http: ResilientHttpClient | None = None,
        progress: HistoryProgressCallback | None = None,
    ) -> None:
        options.validate()
        self.session = session
        self.config = config
        self.parameter_registry = create_air_parameter_registry(config)
        normalized_pollutants = self.parameter_registry.normalise_many(
            options.pollutants, require_configured=True
        )
        disabled = [
            code
            for code in normalized_pollutants
            if not self.parameter_registry.require(code).historical_backfill
        ]
        if disabled:
            raise ValueError(
                "Historical backfill is disabled for parameter(s): "
                + ", ".join(disabled)
            )
        normalized_intervals = None
        if options.intervals_by_pollutant is not None:
            normalized_intervals = {
                self.parameter_registry.resolve(code): tuple(intervals)
                for code, intervals in options.intervals_by_pollutant.items()
            }
        self.options = replace(
            options,
            pollutants=normalized_pollutants,
            intervals_by_pollutant=normalized_intervals,
        )
        self.supported_pollutants = set(self.parameter_registry.historical_codes)
        self.cache_dir = (
            options.cache_dir
            or (config.paths.temp_dir / "gios-history-cache")
        ).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_bridge = create_historical_data_cache_bridge(
            config,
            mode=options.cache_mode,
            prefix=options.cache_prefix,
        )
        self.state_path = self.cache_dir / "state.json"
        self.state = _ImportState.load(self.state_path)
        self.http = http or ResilientHttpClient(config.api)
        self._owns_http = http is None
        self.rate_limiter = _RateLimiter(options.request_interval_seconds)
        self.progress_callback = progress
        self._progress_unit_offset = 0
        self._progress_unit_total = 1
        self.station_bindings: dict[str, StationBinding] = {}
        self.sensor_bindings: dict[str, SensorBinding] = {}
        self.station_parameter_sensor: dict[tuple[str, str], str] = {}

    def _notify_progress(
        self,
        unit_fraction: float,
        task: str,
        detail: Mapping[str, Any],
    ) -> None:
        if self.progress_callback is None:
            return
        fraction = (
            self._progress_unit_offset
            + max(0.0, min(1.0, float(unit_fraction)))
        ) / max(1, self._progress_unit_total)
        try:
            self.progress_callback(
                max(0.0, min(1.0, fraction)),
                task,
                dict(detail),
            )
        except Exception:
            # Progress reporting must never abort a resumable import.
            logger.exception("GIOŚ history progress callback failed")

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def run(self) -> StageStats:
        stats = StageStats()
        started = time.monotonic()
        # Build and validate the plan before making any rate-limited metadata
        # requests. For example, ``source=prepared`` with a year for which no
        # official ZIP exists should fail immediately.
        plan = self._plan()
        # Prepared annual ZIPs are self-contained enough to be parsed and
        # persisted even when the rate-limited metadata API is temporarily
        # unavailable.  Load every station/sensor already known locally first,
        # then enrich from the official metadata API when possible.  Annual API
        # imports still require metadata because they must distinguish automatic
        # one-hour sensors from manual 24-hour series.
        self._load_existing_station_bindings()
        self._load_existing_sensor_preferences()
        metadata_required = any(unit["source"] == "api" for unit in plan)
        try:
            self._prepare_metadata(stats)
        except Exception as exc:
            if metadata_required:
                raise
            stats.warnings += 1
            logger.exception(
                "GIOŚ metadata enrichment failed; continuing prepared ZIP import "
                "with locally known stations and workbook station codes",
                extra={
                    "stage": "gios_history_metadata",
                    "status": "fallback_local_metadata",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
        total_units = len(plan)
        self._progress_unit_total = max(1, total_units)
        completed_units = 0
        details: list[dict[str, Any]] = []

        for unit in plan:
            self._progress_unit_offset = completed_units
            key = unit["state_key"]
            if self.options.resume and key in self.state.completed and not self.options.refresh_cache:
                stats.skipped += int(self.state.completed[key].get("rows", 0))
                details.append({**unit, "status": "already_completed"})
                completed_units += 1
                self._log_progress(completed_units, total_units, started, unit, "already_completed")
                continue
            try:
                if unit["source"] == "prepared":
                    unit_stats = self._import_prepared_year(
                        int(unit["year"]),
                        pollutants=tuple(unit.get("pollutants") or self.options.pollutants),
                    )
                else:
                    unit_stats = self._import_api_combination(
                        int(unit["year"]), str(unit["voivodeship"]), str(unit["pollutant"])
                    )
                stats.merge(unit_stats)
                status = "success" if not unit_stats.errors else "partial"
                if not unit_stats.errors:
                    self.state.completed[key] = {
                        "completed_at": datetime.now(UTC).isoformat(),
                        "rows": unit_stats.downloaded,
                        "inserted": unit_stats.inserted,
                        "skipped": unit_stats.skipped,
                        "source": unit["source"],
                    }
                    self.state.save(self.state_path)
                details.append({**unit, "status": status, "stats": unit_stats.as_dict()})
            except Exception as exc:  # keep other years/regions resumable
                logger.exception("GIOŚ historical import unit failed", extra={"unit": unit})
                stats.errors += 1
                details.append({**unit, "status": "failed", "error": str(exc)})
            completed_units += 1
            self._log_progress(completed_units, total_units, started, unit, details[-1]["status"])

        stats.details = {
            "start_year": self.options.start_year,
            "end_year": self.options.end_year,
            "source": self.options.source,
            "pollutants": list(self.options.pollutants),
            "voivodeships": list(self.options.voivodeships),
            "cache_dir": str(self.cache_dir),
            "cache_bridge": self.cache_bridge.describe(),
            "units_total": total_units,
            "units": details,
            "history_status": gios_history_status(self.session, self.config),
            "requested_intervals": (
                {
                    parameter: [
                        {
                            "start_utc": start.isoformat(),
                            "end_exclusive_utc": end.isoformat(),
                        }
                        for start, end in self._ranges_for(parameter)
                    ]
                    for parameter in self.options.pollutants
                }
                if self.options.intervals_by_pollutant is not None
                else None
            ),
        }
        # Keep the application-state row intentionally small. A national API
        # backfill can process thousands of pages; persisting every page detail
        # in one JSON column would unnecessarily bloat SQLite. Full diagnostics
        # remain in structured logs and the resumable cache state.
        set_application_state(
            self.session,
            "gios_history_last_run",
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "downloaded": stats.downloaded,
                "inserted": stats.inserted,
                "skipped": stats.skipped,
                "warnings": stats.warnings,
                "errors": stats.errors,
                "start_year": self.options.start_year,
                "end_year": self.options.end_year,
                "source": self.options.source,
                "pollutants": list(self.options.pollutants),
                "voivodeships": list(self.options.voivodeships),
                "cache_dir": str(self.cache_dir),
                "cache_bridge": self.cache_bridge.describe(),
                "history_status": stats.details.get("history_status", {}),
            },
        )
        self.session.commit()
        return stats

    def _ranges_for(self, parameter: str) -> tuple[tuple[datetime, datetime], ...]:
        if self.options.intervals_by_pollutant is None:
            return ()
        normalized: list[tuple[datetime, datetime]] = []
        for start, end in self.options.intervals_by_pollutant.get(parameter, ()):
            start_utc = (
                start.replace(tzinfo=UTC)
                if start.tzinfo is None
                else start.astimezone(UTC)
            )
            end_utc = (
                end.replace(tzinfo=UTC)
                if end.tzinfo is None
                else end.astimezone(UTC)
            )
            normalized.append((start_utc, end_utc))
        return tuple(normalized)

    def _timestamp_requested(self, parameter: str, timestamp: datetime) -> bool:
        ranges = self._ranges_for(parameter)
        if self.options.intervals_by_pollutant is None:
            return True
        value = (
            timestamp.replace(tzinfo=UTC)
            if timestamp.tzinfo is None
            else timestamp.astimezone(UTC)
        )
        return any(start <= value < end for start, end in ranges)

    def _parameter_intersects_year(self, parameter: str, year: int) -> bool:
        if self.options.intervals_by_pollutant is None:
            return True
        year_start = datetime(year, 1, 1, tzinfo=UTC)
        year_end = datetime(year + 1, 1, 1, tzinfo=UTC)
        return any(
            start < year_end and year_start < end
            for start, end in self._ranges_for(parameter)
        )

    def _plan(self) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        for year in range(self.options.start_year, self.options.end_year + 1):
            year_pollutants = tuple(
                pollutant
                for pollutant in self.options.pollutants
                if self._parameter_intersects_year(pollutant, year)
            )
            if not year_pollutants:
                continue
            use_prepared = self.options.source == "prepared" or (
                self.options.source == "auto" and year in PREPARED_ARCHIVE_URLS
            )
            if use_prepared:
                if year not in PREPARED_ARCHIVE_URLS:
                    raise ValueError(f"No prepared GIOŚ annual archive is configured for {year}")
                plan.append(
                    {
                        "source": "prepared",
                        "year": year,
                        "pollutants": list(year_pollutants),
                        "state_key": f"prepared:{year}:{','.join(year_pollutants)}:{','.join(self.options.voivodeships)}",
                    }
                )
                continue
            for province in self.options.voivodeships:
                for pollutant in year_pollutants:
                    plan.append(
                        {
                            "source": "api",
                            "year": year,
                            "voivodeship": province,
                            "pollutant": pollutant,
                            "state_key": f"api:{year}:{province}:{pollutant}",
                        }
                    )
        return plan

    def _log_progress(
        self,
        completed: int,
        total: int,
        started: float,
        unit: Mapping[str, Any],
        status: str,
    ) -> None:
        elapsed = max(0.001, time.monotonic() - started)
        avg = elapsed / max(1, completed)
        remaining = avg * max(0, total - completed)
        detail = {
            "stage": "gios_history_progress",
            "completed_units": completed,
            "total_units": total,
            "percent": round(100.0 * completed / max(1, total), 2),
            "eta_seconds": round(remaining),
            "current_unit": dict(unit),
            "status": status,
        }
        logger.info("GIOŚ history progress", extra=detail)
        if self.progress_callback is not None:
            try:
                self.progress_callback(
                    max(0.0, min(1.0, completed / max(1, total))),
                    f"GIOŚ history unit {completed}/{max(1, total)} {status}",
                    detail,
                )
            except Exception:
                logger.exception("GIOŚ history progress callback failed")

    # ---------- metadata ----------

    def _load_existing_station_bindings(self) -> None:
        """Seed historical-code bindings from the local SQLite catalogue.

        This makes prepared annual ZIP imports independent of a live metadata
        request.  It also lets old and current measurements share the same
        station whenever the official station code is already present locally.
        """

        rows = self.session.scalars(
            select(AirStation).where(AirStation.source == "GIOS")
        ).all()
        for station in rows:
            code = to_str(station.station_code)
            if not code:
                continue
            raw = station.raw_json or {}
            history_raw = raw.get("gios_history_metadata")
            history_raw = history_raw if isinstance(history_raw, Mapping) else {}
            province = _normalize_province(
                to_str(
                    get_alias(
                        history_raw or raw,
                        "Województwo",
                        "voivodeship",
                    )
                )
            )
            binding = StationBinding(
                source_id=str(station.source_id),
                station_code=code,
                province=province,
                current_code=code,
            )
            aliases = {code}
            for source in (raw, history_raw):
                if not isinstance(source, Mapping):
                    continue
                for alias_name in (
                    "Stary Kod stacji",
                    "oldStationCode",
                    "Kod stacji",
                    "stationCode",
                ):
                    alias = to_str(source.get(alias_name))
                    if alias:
                        aliases.add(alias)
            for alias in aliases:
                self.station_bindings[alias] = binding

    def _load_existing_sensor_preferences(self) -> None:
        rows = self.session.execute(
            select(
                AirStation.source_id,
                AirSensor.source_id,
                AirSensor.parameter_code,
            )
            .join(AirSensor, AirSensor.air_station_id == AirStation.id)
            .where(AirSensor.parameter_code.in_(tuple(self.supported_pollutants)))
        ).all()
        grouped: dict[tuple[str, str], list[str]] = {}
        for station_source_id, sensor_source_id, parameter in rows:
            key = (
                str(station_source_id),
                self.parameter_registry.resolve(
                    str(parameter), allow_unknown=True
                ),
            )
            grouped.setdefault(key, []).append(str(sensor_source_id))
        for key, values in grouped.items():
            if len(values) == 1:
                self.station_parameter_sensor[key] = values[0]

    def _prepare_metadata(self, stats: StageStats) -> None:
        station_rows = self._fetch_metadata_pages(
            METADATA_STATIONS_PATH,
            preferred_keys=(
                "Lista metadanych stacji pomiarowych",
                "Lista stacji pomiarowych",
                "stations",
            ),
            cache_name="stations",
        )
        stats.downloaded += len(station_rows)
        for item in station_rows:
            if not isinstance(item, Mapping):
                continue
            binding = self._upsert_station_metadata(item)
            if binding is None:
                continue
            code = binding.station_code
            old_code = to_str(get_alias(item, "Stary Kod stacji", "oldStationCode"))
            for alias in (code, old_code):
                if alias:
                    self.station_bindings[alias] = binding
        self.session.commit()

        # Fetch all sensor metadata, not only automatic sensors. The annual
        # archival endpoint mixes automatic one-hour and manual 24-hour results.
        # Keeping manual codes in ``sensor_bindings`` lets us reject them
        # explicitly instead of guessing the measurement mode from the code.
        sensor_rows = self._fetch_metadata_pages(
            METADATA_SENSORS_PATH,
            preferred_keys=(
                "Lista metadanych stanowisk pomiarowych",
                "Lista stanowisk pomiarowych",
                "sensors",
            ),
            cache_name="sensors-all",
        )
        stats.downloaded += len(sensor_rows)
        for item in sensor_rows:
            if not isinstance(item, Mapping):
                continue
            binding = self._upsert_sensor_metadata(item)
            if binding is not None:
                sensor_code = to_str(get_alias(item, "Kod stanowiska", "sensorCode"))
                if sensor_code:
                    self.sensor_bindings[sensor_code] = binding
        self.session.commit()

        # Current v1 sensors have numeric source ids. Prefer them where the
        # station/parameter pair is unambiguous, so current and archival data
        # share one natural series identifier.
        self._load_existing_sensor_preferences()

    def _fetch_metadata_pages(
        self,
        path: str,
        *,
        preferred_keys: tuple[str, ...],
        cache_name: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[Any]:
        rows: list[Any] = []
        page = 0
        while page < 10_000:
            cache_path = self.cache_dir / "metadata" / cache_name / f"page-{page:05d}.json.gz"
            payload = self._cached_json_request(
                f"{self.config.api.gios_base_url}/{path}",
                params={**dict(params or {}), "page": page, "size": self.options.page_size},
                cache_path=cache_path,
                label=f"metadata:{cache_name}:page={page}",
            )
            current = find_collection(payload, preferred_keys)
            rows.extend(current)
            total_pages = self._total_pages(payload)
            if (total_pages is not None and page + 1 >= total_pages) or not current:
                break
            if total_pages is None and len(current) < self.options.page_size:
                break
            page += 1
        return rows

    def _upsert_station_metadata(self, item: Mapping[str, Any]) -> StationBinding | None:
        code = to_str(get_alias(item, "Kod stacji", "stationCode"))
        if not code:
            return None
        old_code = to_str(get_alias(item, "Stary Kod stacji", "oldStationCode"))
        province = _normalize_province(to_str(get_alias(item, "Województwo", "voivodeship")))
        current = self.session.scalar(select(AirStation).where(AirStation.station_code == code))
        if current is None and old_code:
            current = self.session.scalar(select(AirStation).where(AirStation.station_code == old_code))
        if current is None:
            source_id = f"history-station:{code}"
            current = upsert_air_station(
                self.session,
                AirStationRecord(
                    source_id=source_id,
                    station_code=code,
                    station_name=to_str(get_alias(item, "Nazwa stacji", "stationName")) or code,
                    city_name=to_str(get_alias(item, "Miejscowość", "city")),
                    address=to_str(get_alias(item, "Ulica", "Adres", "street")),
                    latitude=to_float(get_alias(item, "WGS84 φ N", "latitude")),
                    longitude=to_float(get_alias(item, "WGS84 λ E", "longitude")),
                    raw_json=dict(item),
                ),
            )
        else:
            current.station_code = code
            current.station_name = to_str(get_alias(item, "Nazwa stacji", "stationName")) or current.station_name
            current.city_name = to_str(get_alias(item, "Miejscowość", "city")) or current.city_name
            current.address = to_str(get_alias(item, "Ulica", "Adres", "street")) or current.address
            current.latitude = to_float(get_alias(item, "WGS84 φ N", "latitude")) or current.latitude
            current.longitude = to_float(get_alias(item, "WGS84 λ E", "longitude")) or current.longitude
            current.raw_json = {**(current.raw_json or {}), "gios_history_metadata": dict(item)}
        closed_at = to_str(get_alias(item, "Data zamknięcia", "closeDate"))
        current.active = not bool(closed_at)
        self.session.flush()
        return StationBinding(
            source_id=str(current.source_id),
            station_code=code,
            province=province,
            current_code=code,
        )

    def _upsert_sensor_metadata(self, item: Mapping[str, Any]) -> SensorBinding | None:
        sensor_code = to_str(get_alias(item, "Kod stanowiska", "sensorCode"))
        station_code = to_str(get_alias(item, "Kod stacji", "stationCode"))
        old_station_code = to_str(get_alias(item, "Stary Kod stacji", "oldStationCode"))
        parameter = self.parameter_registry.resolve(
            to_str(
                get_alias(
                    item,
                    "Wskaźnik - kod",
                    "Kod wskaźnika",
                    "parameterCode",
                )
            ),
            allow_unknown=True,
        )
        if not sensor_code or parameter not in self.supported_pollutants:
            return None
        automatic_hourly = _is_automatic_hourly(item)
        if not automatic_hourly:
            return SensorBinding(
                source_id=f"history-sensor:{sensor_code}",
                station_source_id="",
                parameter=parameter,
                automatic_hourly=False,
            )
        station_binding = self.station_bindings.get(station_code or "") or self.station_bindings.get(
            old_station_code or ""
        )
        if station_binding is None:
            return None
        preferred = self.station_parameter_sensor.get((station_binding.source_id, parameter))
        source_id = preferred or f"history-sensor:{sensor_code}"
        if preferred is None:
            upsert_air_sensor(
                self.session,
                AirSensorRecord(
                    source_id=source_id,
                    station_source_id=station_binding.source_id,
                    parameter_code=parameter,
                    parameter_name=to_str(get_alias(item, "Wskaźnik", "parameterName")),
                    formula=parameter,
                    raw_json=dict(item),
                ),
            )
        return SensorBinding(
            source_id=source_id,
            station_source_id=station_binding.source_id,
            parameter=parameter,
            automatic_hourly=True,
        )

    # ---------- prepared annual ZIP ----------

    def _import_prepared_year(
        self,
        year: int,
        *,
        pollutants: tuple[str, ...] | None = None,
    ) -> StageStats:
        stats = StageStats()
        archive_url = PREPARED_ARCHIVE_URLS[year]
        archive_path = self.cache_dir / "prepared" / f"gios-{year}.zip"
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        def fetch_archive() -> bytes:
            logger.info(
                "Downloading official GIOŚ prepared annual archive",
                extra={
                    "stage": "gios_history_download",
                    "year": year,
                    "url": archive_url,
                    "cache_mode": self.cache_bridge.mode,
                },
            )
            return self.http.get_bytes(
                archive_url,
                headers={"Accept": "application/zip,*/*;q=0.1"},
            )

        archive_result = self.cache_bridge.get_or_fetch(
            local_path=archive_path,
            key=f"prepared/gios-{year}.zip",
            fetch=fetch_archive,
            content_type="application/zip",
            refresh=self.options.refresh_cache,
        )
        logger.info(
            "GIOŚ prepared archive cache resolved",
            extra={
                "stage": "gios_history_cache",
                "year": year,
                "cache_mode": self.cache_bridge.mode,
                "cache_source": archive_result.source,
                "cache_key": archive_result.key,
                "local_path": str(archive_result.local_path),
            },
        )
        archive_sha = hashlib.sha256(archive_result.data).hexdigest()

        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            selected: list[tuple[str, str]] = []
            for parameter in (pollutants or self.options.pollutants):
                tokens = set(
                    self.parameter_registry.prepared_member_tokens(parameter)
                )
                matches: list[str] = []
                for name in names:
                    normalized_name = _normalize_prepared_member_name(
                        Path(name).name
                    )
                    if not normalized_name.endswith("_1g.xlsx"):
                        continue
                    expected_prefix = f"{year}_"
                    if not normalized_name.startswith(expected_prefix):
                        continue
                    middle = normalized_name[
                        len(expected_prefix) : -len("_1g.xlsx")
                    ]
                    if _prepared_parameter_token(middle) in tokens:
                        matches.append(name)
                if not matches:
                    stats.errors += 1
                    logger.error(
                        "Required hourly workbook is absent from GIOŚ archive",
                        extra={
                            "stage": "gios_history_archive",
                            "year": year,
                            "parameter": parameter,
                            "accepted_tokens": sorted(tokens),
                        },
                    )
                    continue
                selected.append((parameter, sorted(matches)[0]))

            file_details: list[dict[str, Any]] = []
            for parameter, member in selected:
                member_state = f"prepared-file:{year}:{parameter}:{archive_sha}"
                if self.options.resume and member_state in self.state.completed and not self.options.refresh_cache:
                    stats.skipped += int(self.state.completed[member_state].get("rows", 0))
                    file_details.append({"member": member, "parameter": parameter, "status": "already_completed"})
                    continue
                xlsx_path = self.cache_dir / "prepared" / str(year) / Path(member).name
                xlsx_path.parent.mkdir(parents=True, exist_ok=True)
                if self.options.refresh_cache or not xlsx_path.exists():
                    xlsx_path.write_bytes(archive.read(member))
                file_stats = self._import_prepared_workbook(xlsx_path, year, parameter, archive_url)
                stats.merge(file_stats)
                if not file_stats.errors:
                    self.state.completed[member_state] = {
                        "completed_at": datetime.now(UTC).isoformat(),
                        "rows": file_stats.downloaded,
                        "inserted": file_stats.inserted,
                        "skipped": file_stats.skipped,
                    }
                    self.state.save(self.state_path)
                file_details.append(
                    {"member": member, "parameter": parameter, "status": "success" if not file_stats.errors else "failed", "stats": file_stats.as_dict()}
                )
        stats.details = {"year": year, "archive_url": archive_url, "archive_sha256": archive_sha, "files": file_details}
        return stats

    def _import_prepared_workbook(
        self,
        xlsx_path: Path,
        year: int,
        parameter: str,
        archive_url: str,
    ) -> StageStats:
        stats = StageStats()
        parsed_series = parse_prepared_hourly_workbook(
            xlsx_path,
            parameter=parameter,
            year=year,
        )
        valid_values_total = 0
        resolved_rows_total = 0
        fallback_stations = 0
        stats.details = {
            "workbook": str(xlsx_path),
            "series": len(parsed_series),
            "parameter": parameter,
            "year": year,
        }

        prepared_start_detail = {
            "stage": "gios_history_prepared_start",
            "year": year,
            "parameter": parameter,
            "series_total": len(parsed_series),
            "workbook": str(xlsx_path),
            "station_code_examples": [
                item.station_code for item in parsed_series[:10]
            ],
        }
        logger.info(
            "GIOŚ prepared workbook import started",
            extra=prepared_start_detail,
        )
        self._notify_progress(
            0.0,
            f"GIOŚ prepared {year} {parameter}: starting",
            prepared_start_detail,
        )

        for index, series in enumerate(parsed_series, start=1):
            binding = self.station_bindings.get(series.station_code)
            if binding is None:
                binding = self._create_fallback_station(series.station_code)
                fallback_stations += 1
            if binding.province and binding.province not in self.options.voivodeships:
                continue

            sensor_source_id = self._ensure_station_parameter_sensor(
                binding,
                parameter,
            )
            records: list[AirMeasurementRecord] = []
            invalid_values = 0
            for measurement_time, value in zip(
                series.measurement_times,
                series.values,
                strict=True,
            ):
                if not self._timestamp_requested(parameter, measurement_time):
                    continue
                definition = self.parameter_registry.require(parameter)
                if value is None or not math.isfinite(value):
                    invalid_values += 1
                    continue
                if (
                    not definition.allow_negative
                    and definition.valid_min is not None
                    and value < definition.valid_min
                ):
                    invalid_values += 1
                    continue
                if definition.valid_max is not None and value > definition.valid_max:
                    invalid_values += 1
                    continue
                records.append(
                    AirMeasurementRecord(
                        station_source_id=binding.source_id,
                        sensor_source_id=sensor_source_id,
                        parameter=parameter,
                        measurement_time=measurement_time,
                        value=round(float(value), 6),
                        unit=definition.canonical_unit,
                        source_status="GIOS_PREPARED_ARCHIVE_1H",
                        raw_json={
                            "source": "GIOS prepared annual ZIP",
                            "year": year,
                            "station_code": series.station_code,
                            "parameter": parameter,
                            "source_url": archive_url,
                            "source_timezone": "CET UTC+01:00",
                        },
                    )
                )

            stats.downloaded += len(series.values)
            stats.warnings += invalid_values
            valid_values_total += len(records)

            for batch_start in range(
                0,
                len(records),
                self.options.insert_batch_size,
            ):
                batch = records[
                    batch_start : batch_start + self.options.insert_batch_size
                ]
                inserted, skipped = insert_air_measurements(self.session, batch)
                resolved = inserted + skipped
                if resolved != len(batch):
                    missing = len(batch) - resolved
                    raise RuntimeError(
                        "GIOŚ prepared archive persistence lost "
                        f"{missing} of {len(batch)} resolved rows for "
                        f"station={series.station_code!r}, "
                        f"parameter={parameter}, year={year}. "
                        "Station/sensor foreign-key resolution failed."
                    )
                stats.inserted += inserted
                stats.skipped += skipped
                resolved_rows_total += resolved
                self.session.commit()

            prepared_progress_detail = {
                "stage": "gios_history_prepared",
                "year": year,
                "parameter": parameter,
                "series_completed": index,
                "series_total": len(parsed_series),
                "percent": round(
                    100.0 * index / max(1, len(parsed_series)),
                    2,
                ),
                "station_code": series.station_code,
                "valid_values_series": len(records),
                "invalid_values_series": invalid_values,
                "inserted_total": stats.inserted,
                "duplicates_total": stats.skipped,
            }
            logger.info(
                "GIOŚ prepared workbook progress",
                extra=prepared_progress_detail,
            )
            self._notify_progress(
                index / max(1, len(parsed_series)),
                (
                    f"GIOŚ prepared {year} {parameter}: "
                    f"station series {index}/{len(parsed_series)}"
                ),
                prepared_progress_detail,
            )

        if valid_values_total > 0 and resolved_rows_total == 0:
            raise RuntimeError(
                "GIOŚ prepared workbook parsed valid PM values but persisted "
                "zero rows. Refusing to mark the workbook as completed."
            )
        if resolved_rows_total != valid_values_total:
            raise RuntimeError(
                "GIOŚ prepared workbook persistence mismatch: "
                f"valid={valid_values_total}, resolved={resolved_rows_total}."
            )

        stats.details.update(
            {
                "valid_values": valid_values_total,
                "resolved_rows": resolved_rows_total,
                "fallback_stations": fallback_stations,
                "station_code_examples": [
                    item.station_code for item in parsed_series[:20]
                ],
            }
        )
        prepared_finished_detail = {
            "stage": "gios_history_prepared_finished",
            "year": year,
            "parameter": parameter,
            "series_total": len(parsed_series),
            "valid_values": valid_values_total,
            "inserted": stats.inserted,
            "duplicates": stats.skipped,
            "fallback_stations": fallback_stations,
            "status": "success",
        }
        logger.info(
            "GIOŚ prepared workbook import finished",
            extra=prepared_finished_detail,
        )
        self._notify_progress(
            1.0,
            f"GIOŚ prepared {year} {parameter}: finished",
            prepared_finished_detail,
        )
        return stats

    # ---------- annual API ----------

    def _import_api_combination(self, year: int, province: str, parameter: str) -> StageStats:
        stats = StageStats()
        page = 0
        combination_started = time.monotonic()
        pages_processed = 0
        rows_total = 0
        inserted_total = 0
        duplicates_total = 0
        skipped_non_hourly_total = 0
        skipped_unknown_sensor_total = 0
        invalid_total = 0
        while page < 100_000:
            if self.options.max_pages_per_combination and page >= self.options.max_pages_per_combination:
                break
            cache_path = (
                self.cache_dir
                / "api"
                / f"year={year}"
                / f"voivodeship={_safe_name(province)}"
                / f"pollutant={_safe_name(parameter)}"
                / f"page-{page:05d}.json.gz"
            )
            payload = self._cached_json_request(
                f"{self.config.api.gios_base_url}/{ANNUAL_API_PATH}",
                params={
                    "year": str(year),
                    "voivodeship": province,
                    "pollution": self.parameter_registry.require(
                        parameter
                    ).annual_api_indicator,
                    "page": page,
                    "size": self.options.page_size,
                },
                cache_path=cache_path,
                label=f"annual:{year}:{province}:{parameter}:page={page}",
            )
            rows = find_collection(
                payload,
                preferred_keys=(
                    "Lista archiwalnych wyników pomiarów",
                    "Lista danych pomiarowych",
                    "data",
                    "results",
                ),
            )
            total_pages = self._total_pages(payload)
            records: list[AirMeasurementRecord] = []
            skipped_non_hourly = 0
            skipped_unknown_sensor = 0
            invalid = 0
            for item in rows:
                if not isinstance(item, Mapping):
                    continue
                sensor_code = to_str(get_alias(item, "Kod stanowiska", "sensorCode", "Kod"))
                binding = self.sensor_bindings.get(sensor_code or "")
                if binding is None:
                    # Scientific safety: never infer that an unknown historical
                    # sensor is hourly/automatic merely from its code. Unknown
                    # series remain visible in statistics and can be resolved by
                    # refreshing official metadata.
                    skipped_unknown_sensor += 1
                    continue
                if not binding.automatic_hourly or binding.parameter != parameter:
                    skipped_non_hourly += 1
                    continue
                date_text = to_str(get_alias(item, "Data", "date", "measurementDate"))
                value = to_float(get_alias(item, "Wartość", "value", "Wynik"))
                if not date_text:
                    invalid += 1
                    continue
                try:
                    timestamp = parse_gios_archival_cet(date_text)
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                if not self._timestamp_requested(parameter, timestamp):
                    continue
                definition = self.parameter_registry.require(parameter)
                if value is None:
                    invalid += 1
                    continue
                if (
                    not definition.allow_negative
                    and definition.valid_min is not None
                    and value < definition.valid_min
                ):
                    invalid += 1
                    continue
                if definition.valid_max is not None and value > definition.valid_max:
                    invalid += 1
                    continue
                records.append(
                    AirMeasurementRecord(
                        station_source_id=binding.station_source_id,
                        sensor_source_id=binding.source_id,
                        parameter=parameter,
                        measurement_time=timestamp,
                        value=round(value, 6),
                        unit=definition.canonical_unit,
                        source_status="GIOS_ARCHIVAL_API_1H",
                        raw_json={
                            "source": "GIOS archival API",
                            "year": year,
                            "voivodeship": province,
                            "sensor_code": sensor_code,
                            "station_name": to_str(get_alias(item, "Nazwa stacji", "stationName")),
                            "source_timezone": "CET UTC+01:00",
                        },
                    )
                )
            inserted, duplicates = insert_air_measurements(self.session, records)
            self.session.commit()
            stats.downloaded += len(rows)
            stats.inserted += inserted
            stats.skipped += duplicates + skipped_non_hourly + skipped_unknown_sensor
            stats.warnings += invalid
            pages_processed += 1
            rows_total += len(rows)
            inserted_total += inserted
            duplicates_total += duplicates
            skipped_non_hourly_total += skipped_non_hourly
            skipped_unknown_sensor_total += skipped_unknown_sensor
            invalid_total += invalid
            pages_done = page + 1
            elapsed = max(0.001, time.monotonic() - combination_started)
            eta_seconds = None
            percent = None
            if total_pages:
                percent = round(100.0 * pages_done / max(1, total_pages), 2)
                eta_seconds = round(
                    elapsed / max(1, pages_done) * max(0, total_pages - pages_done)
                )
            api_progress_detail = {
                "stage": "gios_history_api",
                "year": year,
                "voivodeship": province,
                "parameter": parameter,
                "page": page,
                "pages_done": pages_done,
                "total_pages": total_pages,
                "percent": percent,
                "eta_seconds": eta_seconds,
                "rows": len(rows),
                "inserted": inserted,
                "inserted_combination": inserted_total,
                "skipped_unknown_sensor": skipped_unknown_sensor,
            }
            logger.info(
                "GIOŚ annual API page imported",
                extra=api_progress_detail,
            )
            page_fraction = (
                pages_done / max(1, total_pages)
                if total_pages is not None
                else min(0.95, pages_done / max(1, pages_done + 1))
            )
            self._notify_progress(
                page_fraction,
                (
                    f"GIOŚ API {year} {province} {parameter}: "
                    f"page {pages_done}/{total_pages or '?'}"
                ),
                api_progress_detail,
            )
            if (total_pages is not None and page + 1 >= total_pages) or not rows:
                break
            if total_pages is None and len(rows) < self.options.page_size:
                break
            page += 1
        stats.details = {
            "year": year,
            "voivodeship": province,
            "pollutant": parameter,
            "pages_processed": pages_processed,
            "rows": rows_total,
            "inserted": inserted_total,
            "duplicates": duplicates_total,
            "skipped_non_hourly": skipped_non_hourly_total,
            "skipped_unknown_sensor": skipped_unknown_sensor_total,
            "invalid": invalid_total,
        }
        return stats

    # ---------- helpers ----------

    def _cached_json_request(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        cache_path: Path,
        label: str,
    ) -> Any:
        cache_key = cache_path.relative_to(self.cache_dir).as_posix()
        cached = self.cache_bridge.read(
            local_path=cache_path,
            key=cache_key,
            refresh=self.options.refresh_cache,
        )
        if cached is not None:
            return json.loads(gzip.decompress(cached.data).decode("utf-8"))

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = 0
        while True:
            self.rate_limiter.wait(label)
            try:
                payload = self.http.get_json(
                    url,
                    params=params,
                    headers=GIOS_JSON_LD_HEADERS,
                )
                break
            except ExternalAPIStatusError as exc:
                attempts += 1
                if exc.status_code != 429 or attempts >= 4:
                    raise
                wait_seconds = max(
                    65.0,
                    self.options.request_interval_seconds * 2.0,
                )
                logger.warning(
                    "GIOŚ archival API throttled; waiting",
                    extra={
                        "stage": "gios_history_429",
                        "wait_seconds": wait_seconds,
                        "label": label,
                    },
                )
                time.sleep(wait_seconds)

        compressed = gzip.compress(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        self.cache_bridge.write(
            local_path=cache_path,
            key=cache_key,
            data=compressed,
            content_type="application/gzip",
        )
        return payload

    @staticmethod
    def _total_pages(payload: Any) -> int | None:
        if not isinstance(payload, Mapping):
            return None
        raw = get_alias(payload, "totalPages", "total_pages", "Liczba stron")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _create_fallback_station(self, station_code: str) -> StationBinding:
        source_id = f"history-station:{station_code}"
        station = upsert_air_station(
            self.session,
            AirStationRecord(
                source_id=source_id,
                station_code=station_code,
                station_name=station_code,
                raw_json={"source": "GIOS history fallback", "station_code": station_code},
            ),
        )
        self.session.flush()
        binding = StationBinding(
            source_id=str(station.source_id),
            station_code=station_code,
            province=None,
            current_code=station_code,
        )
        self.station_bindings[station_code] = binding
        return binding

    def _ensure_station_parameter_sensor(self, station: StationBinding, parameter: str) -> str:
        key = (station.source_id, parameter)
        existing = self.station_parameter_sensor.get(key)
        if existing:
            return existing
        source_id = f"history-sensor:{station.current_code}:{parameter}:1h"
        upsert_air_sensor(
            self.session,
            AirSensorRecord(
                source_id=source_id,
                station_source_id=station.source_id,
                parameter_code=parameter,
                parameter_name=parameter,
                formula=parameter,
                raw_json={
                    "source": "GIOS history synthetic series",
                    "station_code": station.current_code,
                    "averaging": "1-hour",
                    "measurement_type": "automatic",
                },
            ),
        )
        self.session.flush()
        self.station_parameter_sensor[key] = source_id
        return source_id

    def _infer_sensor_binding(self, sensor_code: str, parameter: str) -> SensorBinding | None:
        candidates = [code for code in self.station_bindings if sensor_code.startswith(code)]
        if not candidates:
            return None
        station_code = max(candidates, key=len)
        station = self.station_bindings[station_code]
        source_id = self._ensure_station_parameter_sensor(station, parameter)
        binding = SensorBinding(
            source_id=source_id,
            station_source_id=station.source_id,
            parameter=parameter,
            automatic_hourly=True,
        )
        self.sensor_bindings[sensor_code] = binding
        return binding


def backfill_gios_history(
    session: Session,
    config: AppConfig,
    options: HistoryImportOptions,
    *,
    http: ResilientHttpClient | None = None,
) -> StageStats:
    importer = GiosHistoryImporter(session, config, options, http=http)
    try:
        return importer.run()
    finally:
        importer.close()


def gios_history_status(
    session: Session,
    config: AppConfig | None = None,
    *,
    parameters: Iterable[str] | None = None,
) -> dict[str, Any]:
    if parameters is not None:
        selected = tuple(
            dict.fromkeys(normalize_parameter(item) for item in parameters)
        )
    elif config is not None:
        selected = create_air_parameter_registry(config).historical_codes
    else:
        discovered = tuple(
            str(value)
            for value in session.scalars(
                select(func.distinct(AirMeasurement.parameter)).where(
                    AirMeasurement.parameter.is_not(None)
                )
            ).all()
            if value
        )
        selected = tuple(
            dict.fromkeys((*DEFAULT_HISTORICAL_POLLUTANTS, *discovered))
        )

    output: dict[str, Any] = {}
    for parameter in selected:
        count, start, end, station_count, hour_count = session.execute(
            select(
                func.count(AirMeasurement.id),
                func.min(AirMeasurement.measurement_time),
                func.max(AirMeasurement.measurement_time),
                func.count(func.distinct(AirMeasurement.air_station_id)),
                func.count(func.distinct(AirMeasurement.measurement_time)),
            ).where(
                AirMeasurement.parameter == parameter,
                AirMeasurement.value.is_not(None),
                AirMeasurement.is_valid.is_(True),
            )
        ).one()
        span_days = None
        if start is not None and end is not None:
            span_days = round((end - start).total_seconds() / 86_400.0, 2)
        output[parameter] = {
            "rows": int(count or 0),
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "span_days": span_days,
            "stations": int(station_count or 0),
            "unique_hours": int(hour_count or 0),
            "production_training_ready": bool(
                span_days is not None and span_days >= 365
            ),
        }
    return output


def _prepared_parameter_token(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9+().,-]+", "", str(value)).upper()
    normalized = normalized.replace(",", ".")
    normalized = normalized.replace("PM2.5", "PM25")
    return normalized


def _normalize_prepared_member_name(value: str) -> str:
    """Normalize official annual workbook names across GIOŚ variants.

    Real prepared archives use names such as ``2022_PM25_1g.xlsx`` while the
    public pollutant code and CLI use ``PM2.5``. Older packages may also use a
    decimal comma.  All three forms must resolve to the same member.
    """

    normalized = re.sub(r"\s+", "", Path(value).name.casefold())
    normalized = normalized.replace(",", ".")
    normalized = normalized.replace("pm2.5", "pm25")
    return normalized


def parse_prepared_hourly_workbook(
    path: Path,
    *,
    parameter: str,
    year: int,
) -> list[ParsedPreparedSeries]:
    """Parse one GIOŚ ``<year>_<pollutant>_1g.xlsx`` workbook.

    Official files changed headers over time.  For 2016--2024 the documented
    layout uses Excel row 2 as station-code header and four metadata rows.  The
    parser uses that layout first and falls back to data/header detection, so a
    future minor workbook change fails with a useful error rather than silently
    shifting timestamps or station codes.
    """

    raw = pd.read_excel(path, header=None, engine="openpyxl", dtype=object)
    if raw.empty or raw.shape[1] < 2:
        raise ValueError(f"GIOŚ workbook has no usable data columns: {path}")
    data_start = _detect_data_start(raw)
    header_row = _detect_station_header_row(raw, data_start)
    station_codes: dict[int, str] = {}
    for column in range(1, raw.shape[1]):
        value = raw.iat[header_row, column]
        code = to_str(value)
        if not code or code.casefold().startswith("unnamed"):
            continue
        station_codes[column] = code
    if not station_codes:
        raise ValueError(f"Could not identify GIOŚ station-code header in {path}")

    parsed_times = [_parse_excel_or_text_cet(value) for value in raw.iloc[data_start:, 0].tolist()]
    results: list[ParsedPreparedSeries] = []
    for column, code in station_codes.items():
        values = [_coerce_measurement_value(value) for value in raw.iloc[data_start:, column].tolist()]
        times: list[datetime] = []
        kept_values: list[float | None] = []
        for timestamp, value in zip(parsed_times, values, strict=True):
            if timestamp is None:
                continue
            times.append(timestamp)
            kept_values.append(value)
        if times:
            results.append(
                ParsedPreparedSeries(
                    station_code=code,
                    parameter=normalize_parameter(parameter),
                    measurement_times=times,
                    values=kept_values,
                )
            )
    if not results:
        raise ValueError(f"GIOŚ workbook produced no hourly series: {path}")
    return results


def parse_gios_archival_cet(value: Any) -> datetime:
    timestamp = pd.to_datetime(value, errors="raise", dayfirst=False)
    if isinstance(timestamp, pd.DatetimeIndex):
        timestamp = timestamp[0]
    python_value = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else timestamp
    if python_value.tzinfo is None:
        python_value = python_value.replace(tzinfo=CET_FIXED)
    return python_value.astimezone(UTC)


def _parse_excel_or_text_cet(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        if isinstance(value, (int, float)) and 20_000 <= float(value) <= 80_000:
            timestamp = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(value), unit="D")
        else:
            timestamp = pd.to_datetime(value, errors="raise", dayfirst=True)
        python_value = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else timestamp
        if python_value.tzinfo is None:
            python_value = python_value.replace(tzinfo=CET_FIXED)
        return python_value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_measurement_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    parsed = to_float(value)
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _detect_data_start(raw: pd.DataFrame) -> int:
    limit = min(len(raw), 20)
    for row in range(limit):
        checks = [_parse_excel_or_text_cet(raw.iat[index, 0]) is not None for index in range(row, min(row + 3, len(raw)))]
        if len(checks) >= 2 and all(checks):
            return row
    # Fallback to the official 2016+ layout: row 2 is the header and the next
    # four rows are metadata; zero-based data starts at row 6.
    if len(raw) > 6:
        return 6
    raise ValueError("Could not identify the first hourly row in GIOŚ workbook")


def _normalize_header_label(value: Any) -> str:
    text = to_str(value) or ""
    return re.sub(
        r"[^a-z0-9]",
        "",
        text.casefold()
        .replace("ą", "a")
        .replace("ć", "c")
        .replace("ę", "e")
        .replace("ł", "l")
        .replace("ń", "n")
        .replace("ó", "o")
        .replace("ś", "s")
        .replace("ź", "z")
        .replace("ż", "z"),
    )


def _looks_like_ordinal_header(values: Sequence[str]) -> bool:
    if len(values) < 2:
        return False
    integers: list[int] = []
    for value in values:
        normalized = value.strip()
        if not re.fullmatch(r"\d+", normalized):
            return False
        integers.append(int(normalized))
    return integers == list(range(integers[0], integers[0] + len(integers)))


def _station_codes_from_row(raw: pd.DataFrame, row: int) -> list[str]:
    return [
        value
        for value in (to_str(item) for item in raw.iloc[row, 1:].tolist())
        if value and not value.casefold().startswith("unnamed")
    ]


def _detect_station_header_row(raw: pd.DataFrame, data_start: int) -> int:
    candidate_rows = list(range(max(0, data_start - 12), data_start))

    # Official prepared workbooks include an explicit ``Kod stacji`` row.
    # It must take precedence over the visually similar ``Nr`` row containing
    # ordinal column numbers 1..N.  The previous heuristic selected that ordinal
    # row because every value was unique, which detached all measurements from
    # their real GIOŚ station codes.
    preferred_labels = {
        "kodstacji",
        "stationcode",
        "kodstacjipomiarowej",
    }
    secondary_labels = {
        "kodstanowiska",
        "sensorcode",
    }

    for labels in (preferred_labels, secondary_labels):
        for row in candidate_rows:
            if _normalize_header_label(raw.iat[row, 0]) not in labels:
                continue
            values = _station_codes_from_row(raw, row)
            if values and not _looks_like_ordinal_header(values):
                return row

    best_row: int | None = None
    best_score = -10**9
    for row in candidate_rows:
        values = _station_codes_from_row(raw, row)
        usable = [value for value in values if not _is_metadata_token(value)]
        if not usable or _looks_like_ordinal_header(usable):
            continue
        unique = len(set(usable))
        alpha = sum(bool(re.search(r"[A-Za-z]", value)) for value in usable)
        label_bonus = 20 if "kod" in _normalize_header_label(raw.iat[row, 0]) else 0
        score = unique * 4 + alpha * 3 + label_bonus
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None:
        labels = [
            to_str(raw.iat[row, 0])
            for row in candidate_rows
        ]
        raise ValueError(
            "Could not identify a non-ordinal GIOŚ station-code header. "
            f"First-column labels before data: {labels}"
        )
    return best_row


def _is_metadata_token(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value.casefold().replace(",", "."))
    return normalized in {
        "pm10",
        "pm2.5",
        "µg/m3",
        "ug/m3",
        "μg/m3",
        "1g",
        "1-godzinny",
        "automatyczny",
    }


def _is_automatic_hourly(item: Mapping[str, Any]) -> bool:
    measurement_type = (to_str(get_alias(item, "Typ pomiaru", "measurementType")) or "").casefold()
    averaging = (to_str(get_alias(item, "Czas uśredniania", "averagingTime")) or "").casefold()
    return "automat" in measurement_type and ("1" in averaging and ("godzin" in averaging or "1g" in averaging))


def _normalize_province(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value.strip().upper())


def _safe_name(value: str) -> str:
    asciiish = value.translate(str.maketrans("ĄĆĘŁŃÓŚŹŻ", "ACELNOSZZ"))
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", asciiish).strip("-") or "value"
