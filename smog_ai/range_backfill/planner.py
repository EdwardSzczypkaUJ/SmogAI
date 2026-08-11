from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from smog_ai.collectors.gios_history import PREPARED_ARCHIVE_URLS
from smog_ai.range_backfill.contracts import (
    BackfillAction,
    BackfillPlan,
    CoverageReport,
    TimeInterval,
    merge_intervals,
    split_interval_by_year,
)


class BackfillPlanner:
    """Convert source-level gaps into source-specific actions.

    Planning is intentionally based on source availability (at least one
    official station), not on the stricter model-quality threshold. Repeating a
    download cannot create measurements that the source never published.
    """

    def __init__(
        self,
        *,
        now: datetime | None = None,
        gios_live_window_hours: int = 72,
        imgw_live_window_hours: int = 72,
        air_publication_lag_hours: int = 2,
        weather_publication_lag_hours: int = 6,
        minimum_historical_gap_hours: int = 2,
        include_isolated_gaps: bool = False,
        cache_mode: str | None = None,
    ) -> None:
        self.now = (now or datetime.now(UTC)).astimezone(UTC)
        self.gios_live_window = timedelta(hours=max(1, gios_live_window_hours))
        self.imgw_live_window = timedelta(hours=max(1, imgw_live_window_hours))
        self.air_due_until = self.now - timedelta(
            hours=max(0, air_publication_lag_hours)
        )
        self.weather_due_until = self.now - timedelta(
            hours=max(0, weather_publication_lag_hours)
        )
        self.minimum_historical_gap_hours = max(1, minimum_historical_gap_hours)
        self.include_isolated_gaps = include_isolated_gaps
        self.cache_mode = cache_mode

    def plan(self, report: CoverageReport) -> BackfillPlan:
        actions: list[BackfillAction] = []
        ignored: list[dict[str, Any]] = []

        air_historical: dict[tuple[str, int, str], list[TimeInterval]] = defaultdict(list)
        air_live: dict[str, list[TimeInterval]] = defaultdict(list)
        weather_historical: dict[int, dict[str, list[TimeInterval]]] = defaultdict(
            lambda: defaultdict(list)
        )
        weather_live: dict[str, list[TimeInterval]] = defaultdict(list)

        for dataset in report.datasets:
            if not dataset.missing_intervals:
                continue
            due_until = (
                self.air_due_until if dataset.dataset == "air" else self.weather_due_until
            )
            live_cutoff = (
                due_until - self.gios_live_window
                if dataset.dataset == "air"
                else due_until - self.imgw_live_window
            )

            for original in dataset.missing_intervals:
                due = original.clip(end=due_until)
                if due is None:
                    ignored.append(
                        {
                            "dataset": dataset.dataset,
                            "parameter": dataset.parameter,
                            "interval": original.to_dict(),
                            "status": "not_yet_due",
                            "reason": "source_publication_lag",
                        }
                    )
                    continue

                historical = due.clip(end=live_cutoff)
                recent = due.clip(start=live_cutoff)

                if historical is not None:
                    if (
                        historical.hours < self.minimum_historical_gap_hours
                        and not self.include_isolated_gaps
                    ):
                        ignored.append(
                            {
                                "dataset": dataset.dataset,
                                "parameter": dataset.parameter,
                                "interval": historical.to_dict(),
                                "status": "ignored_small_gap",
                                "reason": (
                                    "isolated historical gap below source fetch threshold; "
                                    "kept visible in audit"
                                ),
                            }
                        )
                    else:
                        if dataset.dataset == "air":
                            for year, part in split_interval_by_year(historical):
                                source = (
                                    "prepared"
                                    if year in PREPARED_ARCHIVE_URLS
                                    else "api"
                                )
                                air_historical[(source, year, dataset.parameter)].append(
                                    part
                                )
                        else:
                            for year, part in split_interval_by_year(historical):
                                weather_historical[year][dataset.parameter].append(part)

                if recent is not None:
                    if dataset.dataset == "air":
                        air_live[dataset.parameter].append(recent)
                    else:
                        weather_live[dataset.parameter].append(recent)

        if air_live:
            intervals = merge_intervals(
                [item for values in air_live.values() for item in values]
            )
            parameters = tuple(sorted(air_live))
            actions.append(
                BackfillAction(
                    provider="gios_live",
                    dataset="air",
                    parameters=parameters,
                    intervals=intervals,
                    source="live_api",
                    cache_mode=self.cache_mode,
                    reason="recent_missing_or_stale_air",
                    weight=max(1.0, sum(item.hours for item in intervals) / 24.0),
                    metadata={"source_granularity": "current_window"},
                )
            )

        for (source, year, parameter), values in sorted(air_historical.items()):
            intervals = merge_intervals(values)
            provider = "gios_prepared" if source == "prepared" else "gios_api"
            granularity = (
                "annual_zip_parameter"
                if source == "prepared"
                else "annual_api_year_voivodeship_parameter"
            )
            actions.append(
                BackfillAction(
                    provider=provider,  # type: ignore[arg-type]
                    dataset="air",
                    parameters=(parameter,),
                    intervals=intervals,
                    year=year,
                    source=source,
                    cache_mode=self.cache_mode,
                    reason="historical_missing_air",
                    weight=max(1.0, sum(item.hours for item in intervals) / 168.0),
                    metadata={"source_granularity": granularity},
                )
            )

        if weather_live:
            intervals = merge_intervals(
                [item for values in weather_live.values() for item in values]
            )
            actions.append(
                BackfillAction(
                    provider="imgw_live",
                    dataset="weather",
                    parameters=tuple(sorted(weather_live)),
                    intervals=intervals,
                    source="live_api",
                    cache_mode=self.cache_mode,
                    reason="recent_missing_or_stale_weather",
                    weight=max(1.0, sum(item.hours for item in intervals) / 24.0),
                    metadata={"source_granularity": "current_snapshot"},
                )
            )

        for year, parameter_ranges in sorted(weather_historical.items()):
            intervals = merge_intervals(
                [item for values in parameter_ranges.values() for item in values]
            )
            actions.append(
                BackfillAction(
                    provider="imgw_archive",
                    dataset="weather",
                    parameters=tuple(sorted(parameter_ranges)),
                    intervals=intervals,
                    year=year,
                    source="official_archive",
                    cache_mode=self.cache_mode,
                    reason="historical_missing_weather",
                    weight=max(1.0, sum(item.hours for item in intervals) / 168.0),
                    metadata={
                        "source_granularity": "monthly_network_or_station_year_zip",
                        "parameter_intervals": {
                            parameter: [item.to_dict() for item in merge_intervals(values)]
                            for parameter, values in sorted(parameter_ranges.items())
                        },
                    },
                )
            )

        ordered = tuple(
            sorted(
                actions,
                key=lambda item: (
                    0 if item.provider in {"gios_live", "imgw_live"} else 1,
                    item.year or self.now.year,
                    item.provider,
                    item.parameters,
                ),
            )
        )
        return BackfillPlan(
            requested=report.requested,
            generated_at=self.now,
            actions=ordered,
            ignored=tuple(ignored),
            coverage_before=report,
        )
