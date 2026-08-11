from __future__ import annotations

import json
import statistics
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from smog_ai.database.models import AirMeasurement, WeatherMeasurement
from smog_ai.range_backfill.contracts import (
    CoverageReport,
    DatasetCoverage,
    TimeInterval,
    as_utc,
    merge_intervals,
)

WEATHER_FIELDS: dict[str, Any] = {
    "temperature_c": WeatherMeasurement.temperature_c,
    "humidity_percent": WeatherMeasurement.humidity_percent,
    "pressure_hpa": WeatherMeasurement.pressure_hpa,
    "precipitation_mm": WeatherMeasurement.precipitation_mm,
    "wind_speed_mps": WeatherMeasurement.wind_speed_mps,
    "wind_direction_deg": WeatherMeasurement.wind_direction_deg,
}

DEFAULT_AIR_PARAMETERS = ("PM10", "PM2.5")
DEFAULT_WEATHER_PARAMETERS = tuple(WEATHER_FIELDS)


def parse_datetime_bound(
    raw: str | datetime | None,
    *,
    display_timezone: str,
    default: datetime,
    end_date_inclusive: bool = False,
) -> datetime:
    if raw is None:
        return as_utc(default)
    if isinstance(raw, datetime):
        return as_utc(raw)
    text = str(raw).strip()
    date_only = len(text) == 10 and text[4] == "-" and text[7] == "-"
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(display_timezone))
    if date_only and end_date_inclusive:
        value += timedelta(days=1)
    return value.astimezone(UTC)


def _normalize_timestamp(value: datetime) -> datetime:
    return as_utc(value).replace(minute=0, second=0, microsecond=0)


def _hour_slots(interval: TimeInterval) -> list[datetime]:
    cursor = interval.start.replace(minute=0, second=0, microsecond=0)
    if cursor < interval.start:
        cursor += timedelta(hours=1)
    result: list[datetime] = []
    while cursor < interval.end:
        result.append(cursor)
        cursor += timedelta(hours=1)
    return result


def _cadence_slots(
    interval: TimeInterval,
    *,
    cadence_hours: int,
    phase: int,
) -> list[datetime]:
    cursor = interval.start.replace(minute=0, second=0, microsecond=0)
    if cursor < interval.start:
        cursor += timedelta(hours=1)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    while int((cursor - epoch).total_seconds() // 3600) % cadence_hours != phase:
        cursor += timedelta(hours=1)
    result: list[datetime] = []
    while cursor < interval.end:
        result.append(cursor)
        cursor += timedelta(hours=cadence_hours)
    return result


def _missing_intervals(
    slots: Sequence[datetime],
    station_counts: Mapping[datetime, int],
    *,
    minimum_stations: int,
    cadence_hours: int,
) -> tuple[TimeInterval, ...]:
    if not slots:
        return ()
    step = timedelta(hours=cadence_hours)
    missing = [slot for slot in slots if station_counts.get(slot, 0) < minimum_stations]
    if not missing:
        return ()

    intervals: list[TimeInterval] = []
    start = missing[0]
    previous = missing[0]
    for current in missing[1:]:
        if current == previous + step:
            previous = current
            continue
        intervals.append(TimeInterval(start, previous + step))
        start = previous = current
    intervals.append(TimeInterval(start, previous + step))
    return tuple(intervals)


def _station_stats(counts: Mapping[datetime, int]) -> tuple[int, float, int]:
    positive = [value for value in counts.values() if value > 0]
    if not positive:
        return 0, 0.0, 0
    return min(positive), float(statistics.median(positive)), max(positive)


def _infer_precipitation_phase(
    timestamps: Iterable[datetime],
    *,
    cadence_hours: int,
) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    phases = [
        int((_normalize_timestamp(item) - epoch).total_seconds() // 3600)
        % cadence_hours
        for item in timestamps
    ]
    if not phases:
        return 0
    counts = Counter(phases)
    return min(
        (phase for phase, count in counts.items() if count == max(counts.values())),
        default=0,
    )


class CoverageAuditor:
    """Read-only source-availability audit backed by SQLite.

    The source-completion audit deliberately defaults to one station.  A
    separate model-quality threshold can be applied later.  Re-downloading a
    source cannot manufacture observations that the official source did not
    publish, so using a five-station model gate as a download trigger would
    create an endless loop.
    """

    def __init__(
        self,
        session: Session,
        *,
        display_timezone: str = "Europe/Warsaw",
        precipitation_cadence_hours: int = 6,
    ) -> None:
        self.session = session
        self.display_timezone = display_timezone
        self.precipitation_cadence_hours = max(1, int(precipitation_cadence_hours))

    def audit(
        self,
        requested: TimeInterval,
        *,
        air_parameters: Sequence[str] = DEFAULT_AIR_PARAMETERS,
        weather_parameters: Sequence[str] = DEFAULT_WEATHER_PARAMETERS,
        minimum_air_stations: int = 1,
        minimum_weather_stations: int = 1,
        generated_at: datetime | None = None,
    ) -> CoverageReport:
        datasets: list[DatasetCoverage] = []
        for parameter in dict.fromkeys(str(item) for item in air_parameters):
            datasets.append(
                self._audit_air(
                    requested,
                    parameter,
                    minimum_stations=minimum_air_stations,
                )
            )
        for parameter in dict.fromkeys(str(item) for item in weather_parameters):
            if parameter not in WEATHER_FIELDS:
                continue
            datasets.append(
                self._audit_weather(
                    requested,
                    parameter,
                    minimum_stations=minimum_weather_stations,
                )
            )
        return CoverageReport(
            requested=requested,
            generated_at=generated_at or datetime.now(UTC),
            display_timezone=self.display_timezone,
            datasets=tuple(datasets),
            source_minimum_stations=min(
                int(minimum_air_stations), int(minimum_weather_stations)
            ),
        )

    def _audit_air(
        self,
        requested: TimeInterval,
        parameter: str,
        *,
        minimum_stations: int,
    ) -> DatasetCoverage:
        rows = self.session.execute(
            select(
                AirMeasurement.measurement_time,
                func.count(distinct(AirMeasurement.air_station_id)),
                func.count(AirMeasurement.id),
            )
            .where(
                AirMeasurement.parameter == parameter,
                AirMeasurement.is_valid.is_(True),
                AirMeasurement.value.is_not(None),
                AirMeasurement.measurement_time >= requested.start,
                AirMeasurement.measurement_time < requested.end,
            )
            .group_by(AirMeasurement.measurement_time)
            .order_by(AirMeasurement.measurement_time)
        ).all()
        station_counts: dict[datetime, int] = {}
        for timestamp, station_count, _row_count in rows:
            key = _normalize_timestamp(timestamp)
            station_counts[key] = max(
                station_counts.get(key, 0), int(station_count or 0)
            )
        slots = _hour_slots(requested)
        present = sum(
            1 for slot in slots if station_counts.get(slot, 0) >= minimum_stations
        )
        undercovered = sum(
            1
            for slot in slots
            if 0 < station_counts.get(slot, 0) < minimum_stations
        )
        observed = sorted(station_counts)
        minimum, median, maximum = _station_stats(station_counts)
        return DatasetCoverage(
            dataset="air",
            parameter=parameter,
            requested=requested,
            cadence_hours=1,
            minimum_stations=minimum_stations,
            expected_slots=len(slots),
            present_slots=present,
            undercovered_slots=undercovered,
            missing_intervals=_missing_intervals(
                slots,
                station_counts,
                minimum_stations=minimum_stations,
                cadence_hours=1,
            ),
            first_observed_at=observed[0] if observed else None,
            last_observed_at=observed[-1] if observed else None,
            station_count_min=minimum,
            station_count_median=median,
            station_count_max=maximum,
        )

    def _audit_weather(
        self,
        requested: TimeInterval,
        parameter: str,
        *,
        minimum_stations: int,
    ) -> DatasetCoverage:
        column = WEATHER_FIELDS[parameter]
        rows = self.session.execute(
            select(
                WeatherMeasurement.measurement_time,
                func.count(distinct(WeatherMeasurement.weather_station_id)),
                func.count(WeatherMeasurement.id),
            )
            .where(
                WeatherMeasurement.is_valid.is_(True),
                column.is_not(None),
                WeatherMeasurement.measurement_time >= requested.start,
                WeatherMeasurement.measurement_time < requested.end,
            )
            .group_by(WeatherMeasurement.measurement_time)
            .order_by(WeatherMeasurement.measurement_time)
        ).all()
        station_counts: dict[datetime, int] = {}
        timestamps: list[datetime] = []
        for timestamp, station_count, _row_count in rows:
            key = _normalize_timestamp(timestamp)
            timestamps.append(key)
            station_counts[key] = max(
                station_counts.get(key, 0), int(station_count or 0)
            )

        cadence = (
            self.precipitation_cadence_hours
            if parameter == "precipitation_mm"
            else 1
        )
        phase = (
            _infer_precipitation_phase(timestamps, cadence_hours=cadence)
            if cadence > 1
            else 0
        )
        slots = (
            _cadence_slots(requested, cadence_hours=cadence, phase=phase)
            if cadence > 1
            else _hour_slots(requested)
        )
        present = sum(
            1 for slot in slots if station_counts.get(slot, 0) >= minimum_stations
        )
        undercovered = sum(
            1
            for slot in slots
            if 0 < station_counts.get(slot, 0) < minimum_stations
        )
        observed = sorted(station_counts)
        minimum, median, maximum = _station_stats(station_counts)
        return DatasetCoverage(
            dataset="weather",
            parameter=parameter,
            requested=requested,
            cadence_hours=cadence,
            minimum_stations=minimum_stations,
            expected_slots=len(slots),
            present_slots=present,
            undercovered_slots=undercovered,
            missing_intervals=_missing_intervals(
                slots,
                station_counts,
                minimum_stations=minimum_stations,
                cadence_hours=cadence,
            ),
            first_observed_at=observed[0] if observed else None,
            last_observed_at=observed[-1] if observed else None,
            station_count_min=minimum,
            station_count_median=median,
            station_count_max=maximum,
        )


def requested_range_from_audit_payload(
    payload: Mapping[str, Any],
) -> tuple[TimeInterval, tuple[str, ...], tuple[str, ...]]:
    requested = payload.get("requested_range") or payload.get("requested")
    if not isinstance(requested, Mapping):
        raise ValueError("Audit payload does not contain requested_range/requested")
    interval = TimeInterval.from_mapping(requested)

    datasets = payload.get("datasets")
    air: list[str] = []
    weather: list[str] = []
    if isinstance(datasets, Mapping):
        values = datasets.values()
    elif isinstance(datasets, list):
        values = datasets
    else:
        values = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        dataset = str(item.get("dataset") or "")
        parameter = str(item.get("parameter") or "")
        if not parameter:
            continue
        if dataset == "air" and parameter not in air:
            air.append(parameter)
        elif dataset == "weather" and parameter not in weather:
            weather.append(parameter)
    return (
        interval,
        tuple(air or DEFAULT_AIR_PARAMETERS),
        tuple(weather or DEFAULT_WEATHER_PARAMETERS),
    )


def load_latest_audit_from_path(
    path: Path,
) -> tuple[dict[str, Any], str]:
    """Load a JSON audit directly or the newest audit from a ZIP/directory."""

    resolved = path.resolve()
    if resolved.is_dir():
        candidates = sorted(
            resolved.rglob("data-range-audit-*.json"),
            key=lambda item: item.name,
        )
        if not candidates:
            raise FileNotFoundError(f"No data-range-audit JSON in {resolved}")
        selected = candidates[-1]
        return json.loads(selected.read_text(encoding="utf-8-sig")), str(selected)
    if resolved.suffix.lower() == ".zip":
        with zipfile.ZipFile(resolved) as archive:
            candidates = sorted(
                name
                for name in archive.namelist()
                if Path(name).name.startswith("data-range-audit-")
                and name.lower().endswith(".json")
            )
            if not candidates:
                raise FileNotFoundError(
                    f"No data-range-audit JSON in ZIP {resolved}"
                )
            selected = candidates[-1]
            return (
                json.loads(archive.read(selected).decode("utf-8-sig")),
                f"{resolved}!/{selected}",
            )
    return json.loads(resolved.read_text(encoding="utf-8-sig")), str(resolved)


def save_coverage_report(report: CoverageReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
