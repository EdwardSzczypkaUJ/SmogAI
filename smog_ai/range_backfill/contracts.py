from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

DatasetKind = Literal["air", "weather"]
ProviderName = Literal[
    "gios_live",
    "gios_prepared",
    "gios_api",
    "imgw_live",
    "imgw_archive",
]
ActionStatus = Literal[
    "planned",
    "running",
    "success",
    "partial",
    "failed",
    "skipped_complete",
    "skipped_no_progress",
    "ignored_small_gap",
    "not_yet_due",
]


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, order=True)
class TimeInterval:
    """Right-open UTC interval ``[start, end)``."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = as_utc(self.start)
        end = as_utc(self.end)
        if end <= start:
            raise ValueError("TimeInterval.end must be later than start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def contains(self, value: datetime) -> bool:
        timestamp = as_utc(value)
        return self.start <= timestamp < self.end

    def intersects(self, other: "TimeInterval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "TimeInterval") -> "TimeInterval | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return TimeInterval(start, end) if end > start else None

    def clip(self, start: datetime | None = None, end: datetime | None = None) -> "TimeInterval | None":
        lower = as_utc(start) if start is not None else self.start
        upper = as_utc(end) if end is not None else self.end
        result_start = max(self.start, lower)
        result_end = min(self.end, upper)
        return TimeInterval(result_start, result_end) if result_end > result_start else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_utc": self.start.isoformat(),
            "end_exclusive_utc": self.end.isoformat(),
            "hours": round(self.hours, 6),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TimeInterval":
        start = value.get("start_utc") or value.get("start")
        end = (
            value.get("end_exclusive_utc")
            or value.get("end_utc")
            or value.get("end")
        )
        if not start or not end:
            raise ValueError(f"Interval is missing start/end: {value}")
        return cls(
            datetime.fromisoformat(str(start).replace("Z", "+00:00")),
            datetime.fromisoformat(str(end).replace("Z", "+00:00")),
        )


def merge_intervals(
    intervals: list[TimeInterval] | tuple[TimeInterval, ...],
    *,
    join_gap: timedelta = timedelta(0),
) -> tuple[TimeInterval, ...]:
    if not intervals:
        return ()
    ordered = sorted(intervals)
    merged: list[TimeInterval] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end + join_gap:
            merged[-1] = TimeInterval(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return tuple(merged)


def split_interval_by_year(interval: TimeInterval) -> tuple[tuple[int, TimeInterval], ...]:
    result: list[tuple[int, TimeInterval]] = []
    cursor = interval.start
    while cursor < interval.end:
        boundary = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
        end = min(interval.end, boundary)
        result.append((cursor.year, TimeInterval(cursor, end)))
        cursor = end
    return tuple(result)


def split_interval_by_month(interval: TimeInterval) -> tuple[tuple[int, int, TimeInterval], ...]:
    result: list[tuple[int, int, TimeInterval]] = []
    cursor = interval.start
    while cursor < interval.end:
        if cursor.month == 12:
            boundary = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
        else:
            boundary = datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)
        end = min(interval.end, boundary)
        result.append((cursor.year, cursor.month, TimeInterval(cursor, end)))
        cursor = end
    return tuple(result)


@dataclass(frozen=True, slots=True)
class DatasetCoverage:
    dataset: DatasetKind
    parameter: str
    requested: TimeInterval
    cadence_hours: int
    minimum_stations: int
    expected_slots: int
    present_slots: int
    undercovered_slots: int
    missing_intervals: tuple[TimeInterval, ...]
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    station_count_min: int = 0
    station_count_median: float = 0.0
    station_count_max: int = 0

    @property
    def missing_slots(self) -> int:
        return max(0, self.expected_slots - self.present_slots)

    @property
    def coverage_fraction(self) -> float:
        return (
            self.present_slots / self.expected_slots
            if self.expected_slots > 0
            else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "parameter": self.parameter,
            "requested": self.requested.to_dict(),
            "cadence_hours": self.cadence_hours,
            "minimum_stations": self.minimum_stations,
            "expected_slots": self.expected_slots,
            "present_slots": self.present_slots,
            "missing_slots": self.missing_slots,
            "undercovered_slots": self.undercovered_slots,
            "coverage_fraction": round(self.coverage_fraction, 6),
            "first_observed_at": (
                self.first_observed_at.isoformat()
                if self.first_observed_at is not None
                else None
            ),
            "last_observed_at": (
                self.last_observed_at.isoformat()
                if self.last_observed_at is not None
                else None
            ),
            "station_count_min": self.station_count_min,
            "station_count_median": self.station_count_median,
            "station_count_max": self.station_count_max,
            "missing_intervals": [item.to_dict() for item in self.missing_intervals],
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    requested: TimeInterval
    generated_at: datetime
    display_timezone: str
    datasets: tuple[DatasetCoverage, ...]
    source_minimum_stations: int = 1

    def find(self, dataset: DatasetKind, parameter: str) -> DatasetCoverage | None:
        return next(
            (
                item
                for item in self.datasets
                if item.dataset == dataset and item.parameter == parameter
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "generated_at_utc": as_utc(self.generated_at).isoformat(),
            "display_timezone": self.display_timezone,
            "requested": self.requested.to_dict(),
            "source_minimum_stations": self.source_minimum_stations,
            "datasets": {
                f"{item.dataset}:{item.parameter}": item.to_dict()
                for item in self.datasets
            },
        }


@dataclass(frozen=True, slots=True)
class BackfillAction:
    provider: ProviderName
    dataset: DatasetKind
    parameters: tuple[str, ...]
    intervals: tuple[TimeInterval, ...]
    year: int | None = None
    source: str | None = None
    cache_mode: str | None = None
    reason: str = "missing_data"
    weight: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        payload = {
            "provider": self.provider,
            "dataset": self.dataset,
            "parameters": list(self.parameters),
            "year": self.year,
            "source": self.source,
            "intervals": [item.to_dict() for item in self.intervals],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.provider}:{digest}"

    @property
    def total_hours(self) -> float:
        return sum(item.hours for item in self.intervals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.signature,
            "provider": self.provider,
            "dataset": self.dataset,
            "parameters": list(self.parameters),
            "intervals": [item.to_dict() for item in self.intervals],
            "year": self.year,
            "source": self.source,
            "cache_mode": self.cache_mode,
            "reason": self.reason,
            "weight": self.weight,
            "total_hours": round(self.total_hours, 6),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    requested: TimeInterval
    generated_at: datetime
    actions: tuple[BackfillAction, ...]
    ignored: tuple[dict[str, Any], ...] = ()
    coverage_before: CoverageReport | None = None

    @property
    def total_weight(self) -> float:
        return sum(max(0.001, action.weight) for action in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generated_at_utc": as_utc(self.generated_at).isoformat(),
            "requested": self.requested.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "ignored": list(self.ignored),
            "total_actions": len(self.actions),
            "total_weight": round(self.total_weight, 6),
            "coverage_before": (
                self.coverage_before.to_dict()
                if self.coverage_before is not None
                else None
            ),
        }


@dataclass(slots=True)
class BackfillExecutionResult:
    action: BackfillAction
    status: ActionStatus
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: int = 0
    errors: int = 0
    coverage_before: float | None = None
    coverage_after: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def improved(self) -> bool:
        return (
            self.coverage_before is not None
            and self.coverage_after is not None
            and self.coverage_after > self.coverage_before
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "status": self.status,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "errors": self.errors,
            "coverage_before": self.coverage_before,
            "coverage_after": self.coverage_after,
            "improved": self.improved,
            "detail": self.detail,
        }


@runtime_checkable
class BackfillProvider(Protocol):
    name: ProviderName

    def execute(self, action: BackfillAction) -> BackfillExecutionResult:
        ...
