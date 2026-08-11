from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import time
import zipfile
from collections.abc import Iterable, Mapping
from itertools import chain
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.collectors.history_cache import create_historical_data_cache_bridge
from smog_ai.collectors.parsing import normalize_name
from smog_ai.config import AppConfig, ImgwArchiveConfig
from smog_ai.database.models import ApplicationState, WeatherStation
from smog_ai.database.repository import (
    add_collection_error,
    insert_weather_measurements,
    set_application_state,
    upsert_weather_station,
)
from smog_ai.domain import StageStats, WeatherMeasurementRecord, WeatherStationRecord
from smog_ai.errors import DataValidationError, ExternalAPIError
from smog_ai.time_utils import utc_now

logger = logging.getLogger(__name__)
MONTHLY_ARCHIVE_FILENAME = re.compile(
    r"^(?P<year>\d{4})_(?P<month>0[1-9]|1[0-2])_s\.zip$",
    re.IGNORECASE,
)
STATION_YEAR_ARCHIVE_FILENAME = re.compile(
    r"^(?P<year>\d{4})_(?P<station>\d{3,6})_s\.zip$",
    re.IGNORECASE,
)
# Backward-compatible alias used by older tests/plugins.
ARCHIVE_FILENAME = MONTHLY_ARCHIVE_FILENAME
HEADER_RESOURCE = Path(__file__).resolve().parents[1] / "resources" / "imgw_synop_terminowe_header.csv"


class BinaryHttpClient(Protocol):
    def get(self, url: str) -> httpx.Response: ...


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    year: int
    month: int | None
    filename: str
    url: str
    kind: str = "monthly_network"
    station_id: str | None = None

    @property
    def period_start(self) -> datetime:
        if self.month is None:
            return datetime(self.year, 1, 1, tzinfo=UTC)
        return datetime(self.year, self.month, 1, tzinfo=UTC)

    @property
    def period_end(self) -> datetime:
        if self.month is None:
            return datetime(self.year + 1, 1, 1, tzinfo=UTC)
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=UTC)
        return datetime(self.year, self.month + 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ParsedArchive:
    stations: tuple[WeatherStationRecord, ...]
    measurements: tuple[WeatherMeasurementRecord, ...]
    row_count: int
    skipped_rows: int


def _decode_bytes(payload: bytes) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "cp1250", "iso-8859-2"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise DataValidationError(f"Cannot decode IMGW archive CSV: {last_error}")


def load_official_header(path: Path = HEADER_RESOURCE) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"IMGW archive header is missing: {path}")
    text = path.read_text(encoding="utf-8-sig").strip()
    header = next(csv.reader([text]))
    cleaned = [item.strip() for item in header]
    if not cleaned or cleaned[:6] != ["NSP", "POST", "ROK", "MC", "DZ", "GG"]:
        raise DataValidationError("Unexpected IMGW s_t archive header contract")
    return cleaned


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if text in {"", "-", "NA", "N/A", "None", "null"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if result == result and result not in {999.9, 9999.0, 99999.0} else None


def _bounded(value: float | None, lower: float, upper: float) -> float | None:
    if value is None or value < lower or value > upper:
        return None
    return value


def _station_source_id(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        number = float(text.replace(",", "."))
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


def _timestamp(row: Mapping[str, str], timezone_name: str) -> datetime:
    try:
        year = int(float(str(row["ROK"]).strip()))
        month = int(float(str(row["MC"]).strip()))
        day = int(float(str(row["DZ"]).strip()))
        hour = int(float(str(row["GG"]).strip()))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataValidationError(f"Invalid IMGW archive timestamp fields: {row}") from exc
    if hour == 24:
        # The official hourly archive normally uses 0..23.  Treat 24:00 as the
        # next day's midnight instead of silently creating an invalid datetime.
        base = datetime(year, month, day, tzinfo=ZoneInfo(timezone_name))
        return (base.replace(hour=0) + timedelta(days=1)).astimezone(UTC)
    return datetime(year, month, day, hour, tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)


def _first_value(row: Mapping[str, str], codes: Iterable[str]) -> float | None:
    for code in codes:
        value = _to_number(row.get(code))
        if value is not None:
            return value
    return None


def _reader_for_member(payload: bytes, header: list[str]) -> Iterable[dict[str, str]]:
    text = _decode_bytes(payload)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = csv.reader(io.StringIO(text), delimiter=delimiter)
    first = next(rows, None)
    if first is None:
        return []
    normalized_first = [str(item).strip() for item in first]
    if normalized_first[:6] == header[:6]:
        data_rows = rows
    else:
        data_rows = chain([first], rows)

    def mapped() -> Iterable[dict[str, str]]:
        for values in data_rows:
            if not values or not any(str(item).strip() for item in values):
                continue
            padded = list(values[: len(header)])
            if len(padded) < len(header):
                padded.extend([""] * (len(header) - len(padded)))
            yield {header[index]: str(value).strip() for index, value in enumerate(padded)}

    return mapped()


def parse_imgw_archive_zip(
    archive_bytes: bytes,
    *,
    source_url: str,
    archive_period: str,
    archive_sha256: str,
    settings: ImgwArchiveConfig,
    existing_station_ids_by_name: Mapping[str, str] | None = None,
) -> ParsedArchive:
    """Parse one official terminowe/SYNOP station-year ZIP.

    The archive contains a wide CSV without a guaranteed embedded header.  The
    parser uses the official bundled ``s_t_nagłówek.csv`` contract.  Only fields
    required by the forecasting platform are materialised; the provenance and
    quality-control codes remain in ``raw_json``.
    """

    header = load_official_header()
    stations: dict[str, WeatherStationRecord] = {}
    measurements: list[WeatherMeasurementRecord] = []
    skipped_rows = 0
    existing_by_name = dict(existing_station_ids_by_name or {})

    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise DataValidationError(f"Invalid IMGW archive ZIP: {source_url}") from exc
    corrupt = archive.testzip()
    if corrupt is not None:
        raise DataValidationError(f"Corrupt member {corrupt!r} in IMGW archive {source_url}")

    members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
    if not members:
        raise DataValidationError(f"IMGW archive contains no CSV member: {source_url}")

    row_number = 0
    for member in members:
        for row in _reader_for_member(archive.read(member), header):
            row_number += 1
            try:
                timestamp = _timestamp(row, settings.source_timezone)
            except DataValidationError:
                skipped_rows += 1
                continue

            station_name = str(row.get("POST") or row.get("NSP") or archive_period).strip()
            raw_source_id = _station_source_id(row.get("NSP"), archive_period)
            if settings.station_ids and raw_source_id not in {str(value) for value in settings.station_ids}:
                skipped_rows += 1
                continue
            source_id = existing_by_name.get(normalize_name(station_name), raw_source_id)

            temperature = _bounded(_to_number(row.get(settings.temperature_code)), -90.0, 65.0)
            humidity = _bounded(_to_number(row.get(settings.humidity_code)), 0.0, 100.0)
            pressure = _bounded(_first_value(row, settings.pressure_codes), 700.0, 1150.0)
            precipitation = _bounded(
                _to_number(row.get(settings.precipitation_code)), 0.0, 1000.0
            )
            wind_speed = _bounded(_to_number(row.get(settings.wind_speed_code)), 0.0, 100.0)
            wind_direction = _bounded(
                _to_number(row.get(settings.wind_direction_code)), 0.0, 360.0
            )
            if all(
                value is None
                for value in (
                    temperature,
                    humidity,
                    pressure,
                    precipitation,
                    wind_speed,
                    wind_direction,
                )
            ):
                skipped_rows += 1
                continue

            quality_codes = {
                key: row.get(key)
                for key in (
                    "WTEMP",
                    "WWLGW",
                    "WHPOW",
                    "WHPON",
                    "WHPOD",
                    "WWO6G",
                    "WFWR",
                    "WKRWR",
                )
                if row.get(key) not in {None, ""}
            }
            raw_json = {
                "source": "IMGW-PIB-terminowe-synop",
                "source_url": source_url,
                "archive_sha256": archive_sha256,
                "archive_member": member,
                "archive_row": row_number,
                "archive_period": archive_period,
                "raw_station_source_id": raw_source_id,
                "quality_codes": quality_codes,
                "precipitation_semantics": {
                    "code": settings.precipitation_code,
                    "accumulation_period_hours": (
                        settings.precipitation_accumulation_period_hours
                    ),
                    "ending_at_measurement_time": True,
                    "disaggregated_to_hourly": False,
                },
            }
            stations[source_id] = WeatherStationRecord(
                source_id=source_id,
                station_name=station_name or source_id,
                metadata_source="IMGW-PIB terminowe/SYNOP archive",
                raw_json={
                    "archive_period": archive_period,
                    "raw_station_source_id": raw_source_id,
                    "source_url": source_url,
                },
            )
            measurements.append(
                WeatherMeasurementRecord(
                    station_source_id=source_id,
                    station_name=station_name or source_id,
                    measurement_time=timestamp,
                    temperature_c=temperature,
                    humidity_percent=humidity,
                    pressure_hpa=pressure,
                    precipitation_mm=precipitation,
                    precipitation_accumulation_period_hours=(
                        settings.precipitation_accumulation_period_hours
                        if precipitation is not None
                        else None
                    ),
                    wind_speed_mps=wind_speed,
                    wind_direction_deg=wind_direction,
                    raw_json=raw_json,
                )
            )

    return ParsedArchive(
        stations=tuple(stations.values()),
        measurements=tuple(measurements),
        row_count=row_number,
        skipped_rows=skipped_rows,
    )


class ImgwArchiveCollector:
    def __init__(
        self,
        config: AppConfig,
        *,
        client: BinaryHttpClient | None = None,
    ) -> None:
        self.config = config
        self.settings = config.imgw_archive
        self.cache_bridge = create_historical_data_cache_bridge(config)
        self._owns_client = client is None
        self.client: BinaryHttpClient = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=config.api.connect_timeout_seconds,
                read=max(config.api.read_timeout_seconds, 120.0),
                write=config.api.read_timeout_seconds,
                pool=config.api.connect_timeout_seconds,
            ),
            follow_redirects=True,
            headers={
                "User-Agent": config.api.user_agent,
                "Accept": "text/html,application/zip,application/octet-stream,*/*;q=0.1",
            },
        )

    def close(self) -> None:
        if self._owns_client and hasattr(self.client, "close"):
            self.client.close()  # type: ignore[union-attr]

    def _get(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.config.api.max_retries + 1):
            try:
                response = self.client.get(url)
                response.raise_for_status()
                return bytes(response.content)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                transient = status in {408, 425, 429, 500, 502, 503, 504} or status is None
                if not transient or attempt >= self.config.api.max_retries:
                    break
                time.sleep(self.config.api.backoff_base_seconds * (2**attempt))
        raise ExternalAPIError(f"IMGW archive GET failed for {url}: {last_error}") from last_error

    def periods(self, *, now: datetime | None = None) -> list[tuple[int, int]]:
        current = (now or utc_now()).astimezone(UTC)
        if self.settings.start_year is not None:
            first_year = int(self.settings.start_year)
            last_year = int(self.settings.end_year or current.year)
            return [
                (year, month)
                for year in range(first_year, last_year + 1)
                for month in range(1, 13)
                if (year, month) <= (current.year, current.month)
            ]
        months_back = max(0, self.settings.lookback_months - 1)
        current_index = current.year * 12 + current.month - 1
        result: list[tuple[int, int]] = []
        for offset in range(months_back, -1, -1):
            index = current_index - offset
            result.append((index // 12, index % 12 + 1))
        return result

    def years(self, *, now: datetime | None = None) -> list[int]:
        return sorted({year for year, _ in self.periods(now=now)})

    def list_objects(self, year: int) -> list[ArchiveObject]:
        listing_url = f"{self.settings.base_url}/{year}/"
        payload = self._get(listing_url)
        parser = _LinkParser()
        parser.feed(_decode_bytes(payload))
        monthly: list[ArchiveObject] = []
        station_year: list[ArchiveObject] = []
        allowed_periods = set(self.periods())
        allowed_stations = {str(value) for value in self.settings.station_ids}

        for href in parser.hrefs:
            filename = Path(href.split("?", 1)[0]).name
            month_match = MONTHLY_ARCHIVE_FILENAME.fullmatch(filename)
            if month_match and int(month_match.group("year")) == int(year):
                month = int(month_match.group("month"))
                if (int(year), month) in allowed_periods:
                    monthly.append(
                        ArchiveObject(
                            year=int(year),
                            month=month,
                            filename=filename,
                            url=urljoin(listing_url, href),
                            kind="monthly_network",
                        )
                    )
                continue

            station_match = STATION_YEAR_ARCHIVE_FILENAME.fullmatch(filename)
            if not station_match or int(station_match.group("year")) != int(year):
                continue
            station_id = station_match.group("station")
            if allowed_stations and station_id not in allowed_stations:
                continue
            if not any(period_year == int(year) for period_year, _ in allowed_periods):
                continue
            station_year.append(
                ArchiveObject(
                    year=int(year),
                    month=None,
                    filename=filename,
                    url=urljoin(listing_url, href),
                    kind="station_year",
                    station_id=station_id,
                )
            )

        # Prefer one monthly network archive per month when the official
        # catalogue provides it. Older catalogues expose one station-year ZIP
        # instead; mixing both layouts would duplicate the same observations.
        result = monthly if monthly else station_year
        return sorted(
            result,
            key=lambda item: (
                item.year,
                item.month if item.month is not None else 13,
                item.station_id or "",
                item.filename,
            ),
        )

    def download(self, item: ArchiveObject) -> tuple[bytes, str, Path]:
        cache_path = self.settings.cache_dir / str(item.year) / item.filename
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        def fetch() -> bytes:
            return self._get(item.url)

        result = self.cache_bridge.get_or_fetch(
            local_path=cache_path,
            key=f"imgw-archive/{item.year}/{item.filename}",
            fetch=fetch,
            content_type="application/zip",
            refresh=False,
        )
        payload = result.data
        digest = hashlib.sha256(payload).hexdigest()
        checksum_path = cache_path.with_suffix(cache_path.suffix + ".sha256")
        checksum_path.write_text(
            f"{digest}  {cache_path.name}\n",
            encoding="utf-8",
        )
        return payload, digest, cache_path



def _existing_station_map(session: Session) -> dict[str, str]:
    rows = session.execute(select(WeatherStation.source_id, WeatherStation.station_name)).all()
    return {
        normalize_name(name): str(source_id)
        for source_id, name in rows
        if name and source_id
    }


def collect_imgw_archive(
    session: Session,
    config: AppConfig,
    *,
    run_id: str | None = None,
    client: BinaryHttpClient | None = None,
) -> StageStats:
    """Backfill official hourly/terminowe IMGW observations idempotently."""

    if not config.imgw_archive.enabled:
        return StageStats(skipped=1, details={"reason": "imgw_archive_disabled"})

    collector = ImgwArchiveCollector(config, client=client)
    stats = StageStats()
    files_processed: list[dict[str, Any]] = []
    files_skipped_unchanged = 0
    imported_state = {}
    state_row = session.get(ApplicationState, "imgw_archive_files")
    if state_row is not None and isinstance(state_row.value_json, dict):
        imported_state = dict(state_row.value_json)

    try:
        objects: list[ArchiveObject] = []
        for year in collector.years():
            objects.extend(collector.list_objects(year))
        if config.imgw_archive.max_files_per_run:
            objects = objects[: config.imgw_archive.max_files_per_run]

        existing_by_name = _existing_station_map(session)
        for item in objects:
            try:
                payload, digest, cache_path = collector.download(item)
                previous = imported_state.get(item.url)
                if (
                    config.imgw_archive.skip_unchanged_cached_files
                    and isinstance(previous, dict)
                    and previous.get("sha256") == digest
                    and previous.get("status") == "success"
                ):
                    files_skipped_unchanged += 1
                    stats.skipped += 1
                    continue
                parsed = parse_imgw_archive_zip(
                    payload,
                    source_url=item.url,
                    archive_period=(
                        f"{item.year:04d}-{item.month:02d}"
                        if item.month is not None
                        else f"{item.year:04d}-station-{item.station_id or 'unknown'}"
                    ),
                    archive_sha256=digest,
                    settings=config.imgw_archive,
                    existing_station_ids_by_name=existing_by_name,
                )
                for station in parsed.stations:
                    upsert_weather_station(session, station)
                    existing_by_name[normalize_name(station.station_name)] = station.source_id
                session.flush()
                inserted, skipped = insert_weather_measurements(session, parsed.measurements)
                stats.downloaded += parsed.row_count
                stats.inserted += inserted
                stats.skipped += skipped + parsed.skipped_rows
                imported_state[item.url] = {
                    "status": "success",
                    "sha256": digest,
                    "cache_path": str(cache_path),
                    "rows": parsed.row_count,
                    "measurements": len(parsed.measurements),
                    "inserted": inserted,
                    "imported_at": utc_now().isoformat(),
                }
                files_processed.append(
                    {
                        "url": item.url,
                        "sha256": digest,
                        "rows": parsed.row_count,
                        "measurements": len(parsed.measurements),
                        "inserted": inserted,
                        "skipped": skipped + parsed.skipped_rows,
                    }
                )
            except Exception as exc:
                stats.errors += 1
                imported_state[item.url] = {
                    "status": "failed",
                    "error": str(exc),
                    "failed_at": utc_now().isoformat(),
                }
                add_collection_error(
                    session,
                    run_id=run_id,
                    source="IMGW_ARCHIVE",
                    stage="collect_imgw_archive",
                    error=exc,
                    entity_id=item.filename,
                    retryable=True,
                    details={"url": item.url, "year": item.year},
                )
                logger.exception("IMGW archive import failed for %s", item.url)
            if config.imgw_archive.request_interval_seconds:
                time.sleep(config.imgw_archive.request_interval_seconds)

        set_application_state(session, "imgw_archive_files", imported_state)
        set_application_state(
            session,
            "last_imgw_archive_success_at",
            {
                "timestamp": utc_now().isoformat(),
                "files_processed": len(files_processed),
                "files_unchanged": files_skipped_unchanged,
                "errors": stats.errors,
            },
        )
        stats.details = {
            "years": collector.years(),
            "files_discovered": len(objects),
            "files_processed": files_processed,
            "files_unchanged": files_skipped_unchanged,
            "precipitation_accumulation_period_hours": (
                config.imgw_archive.precipitation_accumulation_period_hours
            ),
            "precipitation_disaggregated": False,
        }
        return stats
    finally:
        collector.close()


def backfill_imgw_archive(
    session: Session,
    config: AppConfig,
    *,
    run_id: str | None = None,
    client: BinaryHttpClient | None = None,
) -> StageStats:
    """Compatibility/domain name used by CLI and pipeline."""

    return collect_imgw_archive(session, config, run_id=run_id, client=client)
