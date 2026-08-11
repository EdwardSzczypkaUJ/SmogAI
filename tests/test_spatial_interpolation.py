from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, shape

from smog_ai.spatial.grid import create_poland_grid, load_boundary_geojson
from smog_ai.spatial.interpolation import IDWInterpolator

ROOT = Path(__file__).resolve().parents[1]


def _stations() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"station_id": 1, "latitude": 50.0614, "longitude": 19.9383, "predicted_value": 42.0},
            {"station_id": 2, "latitude": 50.2649, "longitude": 19.0238, "predicted_value": 48.0},
            {"station_id": 3, "latitude": 51.1079, "longitude": 17.0385, "predicted_value": 28.0},
            {"station_id": 4, "latitude": 52.2297, "longitude": 21.0122, "predicted_value": 34.0},
            {"station_id": 5, "latitude": 54.3520, "longitude": 18.6466, "predicted_value": 19.0},
        ]
    )


def test_poland_grid_is_deterministic_and_clipped_to_country() -> None:
    boundary = load_boundary_geojson(ROOT / "smog_ai" / "resources" / "poland_boundary.geojson")
    first = create_poland_grid(boundary, projected_crs="EPSG:2180", resolution_km=30)
    second = create_poland_grid(boundary, projected_crs="EPSG:2180", resolution_km=30)
    pd.testing.assert_frame_equal(first.frame, second.frame)
    assert len(first.frame) > 100
    polygon = shape(boundary["features"][0]["geometry"])
    assert all(
        polygon.contains(Point(row.longitude, row.latitude))
        for row in first.frame.itertuples()
    )


def test_idw_surface_is_nonnegative_deterministic_and_carries_confidence() -> None:
    boundary = load_boundary_geojson(ROOT / "smog_ai" / "resources" / "poland_boundary.geojson")
    grid = create_poland_grid(boundary, projected_crs="EPSG:2180", resolution_km=35)
    origin = datetime(2026, 8, 1, 6, tzinfo=UTC)
    target = origin + timedelta(hours=24)
    interpolator = IDWInterpolator(
        nearest_stations=4,
        minimum_stations=3,
        maximum_distance_km=500,
    )
    first, metrics = interpolator.interpolate(
        grid=grid,
        stations=_stations(),
        parameter="PM10",
        horizon_hours=24,
        origin_time=origin,
        target_time=target,
    )
    second, _ = interpolator.interpolate(
        grid=grid,
        stations=_stations(),
        parameter="PM10",
        horizon_hours=24,
        origin_time=origin,
        target_time=target,
    )
    assert np.allclose(first["value"], second["value"], equal_nan=True)
    assert first["value"].dropna().ge(0).all()
    assert first["confidence"].between(0, 1).all()
    assert set(["color_r", "color_g", "color_b", "color_a"]) <= set(first.columns)
    assert metrics["loo_count"] == len(_stations())
    assert metrics["loo_mae"] is not None


def test_idw_preserves_value_at_exact_station_coordinate() -> None:
    interpolator = IDWInterpolator(nearest_stations=3, minimum_stations=2, maximum_distance_km=500)
    station_xy = np.asarray([[0.0, 0.0], [1000.0, 0.0], [0.0, 1000.0]])
    values = np.asarray([17.5, 30.0, 40.0])
    predicted, nearest, used, _ = interpolator._predict_points(
        np.asarray([[0.0, 0.0]]), station_xy, values
    )
    assert predicted[0] == 17.5
    assert nearest[0] == 0.0
    assert used[0] == 3


def test_exact_point_idw_uses_metric_distance_quality_and_exposes_contributions() -> None:
    stations = pd.DataFrame(
        [
            {
                "station_id": 1,
                "station_name": "west",
                "latitude": 52.0,
                "longitude": 20.98,
                "predicted_value": 10.0,
                "q_fresh": 1.0,
                "q_model": 0.8,
                "q_coverage": 1.0,
            },
            {
                "station_id": 2,
                "station_name": "east",
                "latitude": 52.0,
                "longitude": 21.02,
                "predicted_value": 50.0,
                "q_fresh": 0.5,
                "q_model": 0.5,
                "q_coverage": 0.8,
            },
        ]
    )
    result = IDWInterpolator(
        power=2.0,
        nearest_stations=2,
        minimum_stations=2,
        maximum_distance_km=50,
    ).interpolate_point(
        latitude=52.0,
        longitude=21.0,
        stations=stations,
        parameter="PM10",
        projected_crs="EPSG:2180",
    )
    assert result["method"] == "quality_weighted_idw"
    assert result["projected_crs"] == "EPSG:2180"
    assert result["stations_used"] == 2
    assert result["value"] < 30.0
    assert sum(row["normalized_weight"] for row in result["contributions"]) == pytest.approx(1.0)
    assert result["contributions"][0]["distance_km"] > 0.0


def test_rbf_reports_its_own_metrics_and_masks_unsupported_cells() -> None:
    from smog_ai.spatial.interpolation import RBFSpatialInterpolator

    boundary = load_boundary_geojson(ROOT / "smog_ai" / "resources" / "poland_boundary.geojson")
    grid = create_poland_grid(boundary, projected_crs="EPSG:2180", resolution_km=45)
    origin = datetime(2026, 8, 1, 6, tzinfo=UTC)
    interpolator = RBFSpatialInterpolator(
        nearest_stations=4,
        minimum_stations=3,
        maximum_distance_km=80,
        smoothing=1.0,
    )
    surface, metrics = interpolator.interpolate(
        grid=grid,
        stations=_stations(),
        parameter="PM10",
        horizon_hours=24,
        origin_time=origin,
        target_time=origin + timedelta(hours=24),
    )
    assert metrics["algorithm"] == "rbf"
    assert metrics["loo_count"] > 0
    unsupported = surface["stations_used"] == 0
    assert unsupported.any()
    assert surface.loc[unsupported, "value"].isna().all()
    assert (surface.loc[unsupported, "color_a"] == 0).all()
