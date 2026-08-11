from __future__ import annotations

from smog_ai.config import SpatialConfig
from smog_ai.spatial.contracts import SpatialInterpolator
from smog_ai.spatial.interpolation import IDWInterpolator, RBFSpatialInterpolator


def create_spatial_interpolator(config: SpatialConfig) -> SpatialInterpolator:
    common = dict(
        power=config.idw_power,
        distance_smoothing_m=config.idw_distance_smoothing_m,
        exact_station_threshold_m=config.exact_station_threshold_m,
        nearest_stations=config.nearest_stations,
        minimum_stations=config.minimum_stations,
        maximum_distance_km=config.maximum_distance_km,
        confidence_distance_km=config.confidence_distance_km,
        minimum_confidence=config.confidence_minimum,
    )
    if config.algorithm == "idw":
        return IDWInterpolator(**common)
    if config.algorithm == "rbf":
        return RBFSpatialInterpolator(**common)
    raise ValueError(f"Unsupported spatial interpolation algorithm: {config.algorithm}")
