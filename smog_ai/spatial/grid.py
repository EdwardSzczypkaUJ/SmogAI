from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import transform

from smog_ai.spatial.contracts import SpatialGrid


def load_boundary_geojson(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("type") != "FeatureCollection" or not payload.get("features"):
        raise ValueError(f"Invalid Poland boundary GeoJSON: {path}")
    return payload


def create_poland_grid(
    boundary_geojson: dict[str, Any],
    *,
    projected_crs: str,
    resolution_km: float,
) -> SpatialGrid:
    """Create a regular metric grid and keep cell centres inside Poland."""

    geometry_wgs84 = shape(boundary_geojson["features"][0]["geometry"])
    to_projected = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(projected_crs, "EPSG:4326", always_xy=True)
    geometry_projected = transform(to_projected.transform, geometry_wgs84)
    min_x, min_y, max_x, max_y = geometry_projected.bounds
    resolution_m = float(resolution_km) * 1000.0

    # Cell centres. The half-cell offset avoids a visual bias at the bounding box.
    xs = np.arange(min_x + resolution_m / 2.0, max_x, resolution_m, dtype=float)
    ys = np.arange(min_y + resolution_m / 2.0, max_y, resolution_m, dtype=float)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    flat_x = mesh_x.ravel()
    flat_y = mesh_y.ravel()
    inside = contains_xy(geometry_projected, flat_x, flat_y)
    selected_x = flat_x[inside]
    selected_y = flat_y[inside]
    longitude, latitude = to_wgs84.transform(selected_x, selected_y)

    # Stable row/column indexes relative to the unmasked grid make artifacts easy
    # to inspect and support deterministic lookup tests.
    row_idx, col_idx = np.indices(mesh_x.shape)
    frame = pd.DataFrame(
        {
            "cell_id": [f"r{row:04d}c{col:04d}" for row, col in zip(row_idx.ravel()[inside], col_idx.ravel()[inside], strict=True)],
            "row": row_idx.ravel()[inside].astype(int),
            "column": col_idx.ravel()[inside].astype(int),
            "x_m": selected_x,
            "y_m": selected_y,
            "longitude": np.asarray(longitude, dtype=float),
            "latitude": np.asarray(latitude, dtype=float),
        }
    )
    if frame.empty:
        raise ValueError("Generated Poland grid is empty")
    frame = frame.sort_values(["row", "column"], kind="stable").reset_index(drop=True)
    return SpatialGrid(
        frame=frame,
        boundary_geojson=boundary_geojson,
        projected_crs=projected_crs,
        resolution_m=resolution_m,
    )
