from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True, slots=True)
class SpatialGrid:
    """Regular grid clipped to the Polish national boundary."""

    frame: pd.DataFrame
    boundary_geojson: dict[str, Any]
    projected_crs: str
    resolution_m: float


@dataclass(frozen=True, slots=True)
class SpatialSurface:
    """One locally precomputed pollutant/horizon surface."""

    surface_id: str
    parameter: str
    horizon_hours: int
    origin_time: datetime
    target_time: datetime
    generated_at: datetime
    model_versions: tuple[str, ...]
    grid: pd.DataFrame
    stations: list[dict[str, Any]]
    metrics: dict[str, Any]
    metadata: dict[str, Any]


@runtime_checkable
class SpatialInterpolator(Protocol):
    """Implementation side of the spatial-interpolation Bridge."""

    algorithm_name: str

    def interpolate(
        self,
        *,
        grid: SpatialGrid,
        stations: pd.DataFrame,
        parameter: str,
        horizon_hours: int,
        origin_time: datetime,
        target_time: datetime,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        ...
