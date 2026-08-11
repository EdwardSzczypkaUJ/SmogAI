from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from smog_ai.time_utils import ensure_utc


class SourceDataTooOldError(RuntimeError):
    """Raised when the latest common source time cannot support the serving SLA."""


@dataclass(frozen=True, slots=True)
class ForecastTimePoint:
    serving_lead_hours: int
    model_horizon_hours: int
    target_time: datetime


@dataclass(frozen=True, slots=True)
class ForecastTimeContract:
    source_origin_time: datetime
    forecast_created_at: datetime
    serving_anchor_time: datetime
    source_age_hours: float
    source_delay_to_anchor_hours: int
    serving_horizon_hours: int
    maximum_source_delay_hours: int
    maximum_model_horizon_hours: int
    points: tuple[ForecastTimePoint, ...]

    @property
    def serving_leads(self) -> tuple[int, ...]:
        return tuple(point.serving_lead_hours for point in self.points)

    @property
    def model_horizons(self) -> tuple[int, ...]:
        return tuple(point.model_horizon_hours for point in self.points)

    @property
    def target_times(self) -> tuple[datetime, ...]:
        return tuple(point.target_time for point in self.points)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_origin_time": self.source_origin_time.isoformat(),
            "forecast_created_at": self.forecast_created_at.isoformat(),
            "serving_anchor_time": self.serving_anchor_time.isoformat(),
            "source_age_hours": self.source_age_hours,
            "source_delay_to_anchor_hours": self.source_delay_to_anchor_hours,
            "serving_horizon_hours": self.serving_horizon_hours,
            "maximum_source_delay_hours": self.maximum_source_delay_hours,
            "maximum_model_horizon_hours": self.maximum_model_horizon_hours,
            "serving_leads": list(self.serving_leads),
            "model_horizons": list(self.model_horizons),
            "target_times": [value.isoformat() for value in self.target_times],
        }


def next_full_hour(value: datetime) -> datetime:
    """Return the first exact full hour strictly after ``value``."""

    aware = ensure_utc(value)
    return aware.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def build_forecast_time_contract(
    *,
    source_origin_time: datetime,
    forecast_created_at: datetime,
    serving_horizon_hours: int,
    maximum_source_delay_hours: int,
    maximum_model_horizon_hours: int,
) -> ForecastTimeContract:
    """Build a strict future-serving grid and the matching model horizons.

    ``serving_lead_hours`` is always 1..N from the next full hour visible to a
    user.  ``model_horizon_hours`` is measured from the latest common source
    origin and may therefore start above 1 when source data arrive with delay.
    """

    origin = ensure_utc(source_origin_time)
    created = ensure_utc(forecast_created_at)
    anchor = next_full_hour(created)

    if origin > created:
        raise ValueError(
            "source_origin_time cannot be later than forecast_created_at"
        )
    if serving_horizon_hours < 1:
        raise ValueError("serving_horizon_hours must be positive")
    if maximum_source_delay_hours < 0:
        raise ValueError("maximum_source_delay_hours cannot be negative")
    if maximum_model_horizon_hours < 1:
        raise ValueError("maximum_model_horizon_hours must be positive")

    source_age_hours = max(0.0, (created - origin).total_seconds() / 3600.0)
    delay_to_anchor = int(
        math.ceil(max(0.0, (anchor - origin).total_seconds() / 3600.0))
    )
    # An origin at the same full hour as the creation time still produces lead 1
    # for the *next* full hour, hence the model delay is at least one hour.
    delay_to_anchor = max(1, delay_to_anchor)

    # Freshness is measured at forecast creation time, not at the next serving
    # anchor.  A source exactly 12 hours old is allowed by a 12-hour SLA even
    # though the first future full hour may require model horizon h13.
    if source_age_hours > float(maximum_source_delay_hours) + 1e-9:
        raise SourceDataTooOldError(
            "Latest common source data are too old for the serving contract: "
            f"source_age_hours={source_age_hours:.6f}, "
            f"maximum={maximum_source_delay_hours}h, "
            f"origin={origin.isoformat()}, created={created.isoformat()}"
        )

    required_maximum = delay_to_anchor + serving_horizon_hours - 1
    if required_maximum > maximum_model_horizon_hours:
        raise SourceDataTooOldError(
            "Configured model horizon cannot cover the serving contract: "
            f"required={required_maximum}h, "
            f"maximum_model_horizon_hours={maximum_model_horizon_hours}h"
        )

    points = tuple(
        ForecastTimePoint(
            serving_lead_hours=lead,
            model_horizon_hours=delay_to_anchor + lead - 1,
            target_time=anchor + timedelta(hours=lead - 1),
        )
        for lead in range(1, serving_horizon_hours + 1)
    )

    return ForecastTimeContract(
        source_origin_time=origin,
        forecast_created_at=created,
        serving_anchor_time=anchor,
        source_age_hours=source_age_hours,
        source_delay_to_anchor_hours=delay_to_anchor,
        serving_horizon_hours=serving_horizon_hours,
        maximum_source_delay_hours=maximum_source_delay_hours,
        maximum_model_horizon_hours=maximum_model_horizon_hours,
        points=points,
    )
