from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from smog_ai.collectors.gios import collect_gios
from smog_ai.collectors.gios_history import (
    ALL_VOIVODESHIPS,
    GiosHistoryImporter,
    HistoryImportOptions,
)
from smog_ai.collectors.imgw import collect_imgw
from smog_ai.collectors.parsing import normalize_name
from smog_ai.collectors.imgw_archive import (
    ImgwArchiveCollector,
    _existing_station_map,
    parse_imgw_archive_zip,
)
from smog_ai.config import AppConfig
from smog_ai.database.repository import (
    merge_weather_measurements,
    upsert_weather_station,
)
from smog_ai.domain import StageStats, WeatherMeasurementRecord
from smog_ai.range_backfill.contracts import (
    BackfillAction,
    BackfillExecutionResult,
    BackfillProvider,
    TimeInterval,
)

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[float, str, Mapping[str, Any]], None]


def _contains(intervals: Sequence[TimeInterval], timestamp: datetime) -> bool:
    value = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
    return any(item.start <= value < item.end for item in intervals)


def _parameter_intervals(action: BackfillAction) -> dict[str, tuple[TimeInterval, ...]]:
    raw = action.metadata.get("parameter_intervals")
    result: dict[str, tuple[TimeInterval, ...]] = {}
    if isinstance(raw, Mapping):
        for parameter, values in raw.items():
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                result[str(parameter)] = tuple(
                    TimeInterval.from_mapping(item)
                    for item in values
                    if isinstance(item, Mapping)
                )
    if not result:
        for parameter in action.parameters:
            result[parameter] = action.intervals
    return result


class GiosLiveBackfillProvider:
    name = "gios_live"

    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.progress = progress

    def execute(self, action: BackfillAction) -> BackfillExecutionResult:
        if self.progress:
            self.progress(0.05, "collecting current GIOŚ window", action.to_dict())
        stats = collect_gios(
            self.session,
            self.config,
            parameters=action.parameters,
        )
        if self.progress:
            self.progress(1.0, "current GIOŚ collection finished", stats.as_dict())
        return BackfillExecutionResult(
            action=action,
            status="partial" if stats.errors else "success",
            inserted=stats.inserted,
            skipped=stats.skipped,
            warnings=stats.warnings,
            errors=stats.errors,
            detail=stats.details,
        )


class GiosHistoricalBackfillProvider:
    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        source: str,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.source = source
        self.progress = progress
        self.name = "gios_prepared" if source == "prepared" else "gios_api"

    def execute(self, action: BackfillAction) -> BackfillExecutionResult:
        if action.year is None:
            raise ValueError("Historical GIOŚ action requires year")
        parameter_intervals = {
            parameter: tuple((item.start, item.end) for item in action.intervals)
            for parameter in action.parameters
        }
        options = HistoryImportOptions(
            start_year=action.year,
            end_year=action.year,
            source=self.source,  # type: ignore[arg-type]
            pollutants=action.parameters,
            voivodeships=ALL_VOIVODESHIPS,
            request_interval_seconds=31.0,
            page_size=500,
            resume=False,
            refresh_cache=False,
            cache_mode=action.cache_mode,  # type: ignore[arg-type]
            cache_prefix=self.config.data_flow.history_cache_prefix,
            insert_batch_size=20_000,
            intervals_by_pollutant=parameter_intervals,
        )
        if self.progress:
            self.progress(
                0.02,
                f"starting GIOŚ {self.source} {action.year}",
                action.to_dict(),
            )
        importer = GiosHistoryImporter(
            self.session,
            self.config,
            options,
            progress=self.progress,
        )
        try:
            stats = importer.run()
        finally:
            importer.close()
        if self.progress:
            self.progress(
                1.0,
                f"GIOŚ {self.source} {action.year} finished",
                stats.as_dict(),
            )
        return BackfillExecutionResult(
            action=action,
            status="partial" if stats.errors else "success",
            inserted=stats.inserted,
            skipped=stats.skipped,
            warnings=stats.warnings,
            errors=stats.errors,
            detail=stats.details,
        )


class ImgwLiveBackfillProvider:
    name = "imgw_live"

    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.progress = progress

    def execute(self, action: BackfillAction) -> BackfillExecutionResult:
        if self.progress:
            self.progress(0.05, "collecting current IMGW snapshot", action.to_dict())
        stats = collect_imgw(self.session, self.config)
        if self.progress:
            self.progress(1.0, "current IMGW collection finished", stats.as_dict())
        return BackfillExecutionResult(
            action=action,
            status="partial" if stats.errors else "success",
            inserted=stats.inserted,
            skipped=stats.skipped,
            warnings=stats.warnings,
            errors=stats.errors,
            detail=stats.details,
        )


class ImgwArchiveRangeBackfillProvider:
    name = "imgw_archive"

    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.progress = progress

    def _config_for_action(self, action: BackfillAction) -> AppConfig:
        if action.cache_mode is None:
            return self.config
        copied = self.config.model_copy(deep=True)
        copied.data_flow.history_cache_mode = action.cache_mode  # type: ignore[assignment]
        return copied

    def execute(self, action: BackfillAction) -> BackfillExecutionResult:
        if action.year is None:
            raise ValueError("IMGW archive action requires year")
        config = self._config_for_action(action)
        # Restrict discovery to the action year without changing persistent config.
        config.imgw_archive.start_year = action.year
        config.imgw_archive.end_year = action.year
        collector = ImgwArchiveCollector(config)
        intervals_by_field = _parameter_intervals(action)
        all_intervals = tuple(
            item
            for values in intervals_by_field.values()
            for item in values
        )
        inserted_total = 0
        updated_total = 0
        unchanged_total = 0
        warnings = 0
        errors = 0
        processed: list[dict[str, Any]] = []
        try:
            objects = [
                item
                for item in collector.list_objects(action.year)
                if any(
                    interval.start < item.period_end
                    and item.period_start < interval.end
                    for interval in all_intervals
                )
            ]
            existing_by_name = _existing_station_map(self.session)
            total = max(1, len(objects))
            for index, item in enumerate(objects, start=1):
                if self.progress:
                    self.progress(
                        (index - 1) / total,
                        f"IMGW archive {index}/{len(objects)} {item.filename}",
                        {
                            "year": action.year,
                            "filename": item.filename,
                            "kind": item.kind,
                        },
                    )
                try:
                    payload, digest, cache_path = collector.download(item)
                    archive_period = (
                        f"{item.year:04d}-{item.month:02d}"
                        if item.month is not None
                        else f"{item.year:04d}-station-{item.station_id or 'unknown'}"
                    )
                    parsed = parse_imgw_archive_zip(
                        payload,
                        source_url=item.url,
                        archive_period=archive_period,
                        archive_sha256=digest,
                        settings=config.imgw_archive,
                        existing_station_ids_by_name=existing_by_name,
                    )
                    for station in parsed.stations:
                        upsert_weather_station(self.session, station)
                        existing_by_name[
                            normalize_name(station.station_name)
                        ] = station.source_id
                    self.session.flush()

                    selected: list[WeatherMeasurementRecord] = []
                    for record in parsed.measurements:
                        include = False
                        for field, intervals in intervals_by_field.items():
                            value = getattr(record, field, None)
                            if value is not None and _contains(intervals, record.measurement_time):
                                include = True
                                break
                        if include:
                            selected.append(record)

                    inserted, updated, unchanged = merge_weather_measurements(
                        self.session,
                        selected,
                    )
                    self.session.commit()
                    inserted_total += inserted
                    updated_total += updated
                    unchanged_total += unchanged
                    warnings += parsed.skipped_rows
                    processed.append(
                        {
                            "filename": item.filename,
                            "kind": item.kind,
                            "cache_path": str(cache_path),
                            "archive_rows": parsed.row_count,
                            "selected_measurements": len(selected),
                            "inserted": inserted,
                            "updated": updated,
                            "unchanged": unchanged,
                        }
                    )
                except Exception as exc:
                    errors += 1
                    logger.exception(
                        "Range-aware IMGW archive import failed for %s",
                        item.url,
                    )
                    processed.append(
                        {
                            "filename": item.filename,
                            "kind": item.kind,
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
            if self.progress:
                self.progress(
                    1.0,
                    f"IMGW archive {action.year} finished",
                    {
                        "files": len(objects),
                        "inserted": inserted_total,
                        "updated": updated_total,
                        "unchanged": unchanged_total,
                        "errors": errors,
                    },
                )
            return BackfillExecutionResult(
                action=action,
                status="partial" if errors else "success",
                inserted=inserted_total,
                updated=updated_total,
                skipped=unchanged_total,
                warnings=warnings,
                errors=errors,
                detail={
                    "year": action.year,
                    "files_discovered": len(objects),
                    "files": processed,
                    "cache_bridge": collector.cache_bridge.describe(),
                    "intervals_by_field": {
                        key: [item.to_dict() for item in values]
                        for key, values in intervals_by_field.items()
                    },
                },
            )
        finally:
            collector.close()


class BackfillProviderRegistry:
    """Open registry so another source adapter can replace a built-in provider."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., BackfillProvider]] = {}

    def register(self, name: str, factory: Callable[..., BackfillProvider]) -> None:
        if not name:
            raise ValueError("Provider name cannot be empty")
        self._factories[name] = factory

    def create(
        self,
        name: str,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressCallback | None = None,
    ) -> BackfillProvider:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise KeyError(f"Unknown range backfill provider: {name}") from exc
        return factory(session, config, progress=progress)

    def describe(self) -> list[str]:
        return sorted(self._factories)


def create_backfill_provider_registry() -> BackfillProviderRegistry:
    registry = BackfillProviderRegistry()
    registry.register("gios_live", GiosLiveBackfillProvider)
    registry.register(
        "gios_prepared",
        lambda session, config, progress=None: GiosHistoricalBackfillProvider(
            session,
            config,
            source="prepared",
            progress=progress,
        ),
    )
    registry.register(
        "gios_api",
        lambda session, config, progress=None: GiosHistoricalBackfillProvider(
            session,
            config,
            source="api",
            progress=progress,
        ),
    )
    registry.register("imgw_live", ImgwLiveBackfillProvider)
    registry.register("imgw_archive", ImgwArchiveRangeBackfillProvider)
    return registry
