from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable, Literal

import numpy as np
from scipy.interpolate import PchipInterpolator

TemporalMethod = Literal["exact", "linear", "pchip"]


@dataclass(frozen=True, slots=True)
class TemporalInterpolationResult:
    value: float
    requested_time: datetime
    left_time: datetime
    right_time: datetime
    method: TemporalMethod
    exact: bool


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def interpolate_temporally(
    samples: Iterable[tuple[datetime, float]],
    *,
    requested_time: datetime,
    method: Literal["linear", "pchip"] = "pchip",
    allow_extrapolation: bool = False,
) -> TemporalInterpolationResult:
    """Interpolate only between already generated exact-time forecasts.

    This function never substitutes one horizon for another.  It is intended for
    sub-hour requests such as 17:30 after the local pipeline has already produced
    forecasts for 17:00 and 18:00.  By default extrapolation is rejected.
    """

    requested = _aware(requested_time)
    normalized: dict[datetime, float] = {}
    for timestamp, raw_value in samples:
        value = float(raw_value)
        if np.isfinite(value):
            normalized[_aware(timestamp)] = value
    ordered = sorted(normalized.items(), key=lambda item: item[0])
    if not ordered:
        raise ValueError("At least one finite temporal sample is required")

    for timestamp, value in ordered:
        if timestamp == requested:
            return TemporalInterpolationResult(
                value=value,
                requested_time=requested,
                left_time=timestamp,
                right_time=timestamp,
                method="exact",
                exact=True,
            )

    if len(ordered) < 2:
        raise ValueError("At least two samples are required for interpolation")
    first_time, _ = ordered[0]
    last_time, _ = ordered[-1]
    if not allow_extrapolation and not (first_time < requested < last_time):
        raise ValueError(
            "Requested time is outside the available forecast interval; "
            "temporal extrapolation is disabled"
        )

    origin = first_time
    x = np.asarray(
        [(timestamp - origin).total_seconds() / 3600.0 for timestamp, _ in ordered],
        dtype=float,
    )
    y = np.asarray([value for _, value in ordered], dtype=float)
    requested_x = (requested - origin).total_seconds() / 3600.0

    right_index = int(np.searchsorted(x, requested_x, side="right"))
    right_index = min(max(right_index, 1), len(ordered) - 1)
    left_index = right_index - 1

    selected_method: Literal["linear", "pchip"] = method
    if method == "pchip" and len(ordered) >= 3:
        interpolator = PchipInterpolator(x, y, extrapolate=allow_extrapolation)
        value = float(interpolator(requested_x))
    else:
        selected_method = "linear"
        left_x, right_x = x[left_index], x[right_index]
        weight = (requested_x - left_x) / (right_x - left_x)
        value = float(y[left_index] + weight * (y[right_index] - y[left_index]))

    if not np.isfinite(value):
        raise ValueError("Temporal interpolation produced a non-finite value")
    return TemporalInterpolationResult(
        value=value,
        requested_time=requested,
        left_time=ordered[left_index][0],
        right_time=ordered[right_index][0],
        method=selected_method,
        exact=False,
    )
