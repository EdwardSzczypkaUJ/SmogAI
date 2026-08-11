from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.interpolate import RBFInterpolator

from smog_ai.spatial.colors import rgba_for_values
from smog_ai.spatial.contracts import SpatialGrid


def _station_arrays(stations: pd.DataFrame, projected_crs: str) -> tuple[np.ndarray, np.ndarray]:
    required = {"latitude", "longitude", "predicted_value"}
    missing = required - set(stations.columns)
    if missing:
        raise ValueError(f"Station forecast frame misses columns: {sorted(missing)}")
    frame = stations.dropna(subset=list(required)).copy()
    frame = frame[np.isfinite(frame["predicted_value"].astype(float))]
    if frame.empty:
        raise ValueError("No finite station predictions available for interpolation")
    transformer = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
    x, y = transformer.transform(
        frame["longitude"].astype(float).to_numpy(),
        frame["latitude"].astype(float).to_numpy(),
    )
    return np.column_stack([x, y]), frame["predicted_value"].astype(float).to_numpy()


def _finite_station_frame(stations: pd.DataFrame) -> pd.DataFrame:
    required = {"latitude", "longitude", "predicted_value"}
    missing = required - set(stations.columns)
    if missing:
        raise ValueError(f"Station forecast frame misses columns: {sorted(missing)}")
    frame = stations.dropna(subset=list(required)).copy()
    frame["predicted_value"] = pd.to_numeric(frame["predicted_value"], errors="coerce")
    return frame[np.isfinite(frame["predicted_value"])].reset_index(drop=True)


def _support_confidence(
    nearest_distances_m: np.ndarray,
    used_count: np.ndarray,
    *,
    confidence_distance_km: float,
    minimum_stations: int,
    maximum_distance_km: float,
    minimum_confidence: float,
) -> np.ndarray:
    distance_scale = max(1.0, confidence_distance_km * 1000.0)
    distance_part = np.exp(-nearest_distances_m / distance_scale)
    count_part = np.clip(used_count / max(1.0, float(minimum_stations)), 0.0, 1.0)
    confidence = 0.72 * distance_part + 0.28 * count_part
    confidence = np.where(
        nearest_distances_m > maximum_distance_km * 1000.0,
        0.0,
        confidence,
    )
    return np.where(confidence > 0, np.clip(confidence, minimum_confidence, 1.0), 0.0)


@dataclass(slots=True)
class IDWInterpolator:
    power: float = 2.0
    distance_smoothing_m: float = 100.0
    exact_station_threshold_m: float = 10.0
    nearest_stations: int = 8
    minimum_stations: int = 3
    maximum_distance_km: float = 220.0
    confidence_distance_km: float = 85.0
    minimum_confidence: float = 0.08
    chunk_size: int = 5000

    algorithm_name: str = "idw"

    def _predict_points(
        self,
        query_xy: np.ndarray,
        station_xy: np.ndarray,
        station_values: np.ndarray,
        station_quality: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        total = len(query_xy)
        values = np.full(total, np.nan, dtype=float)
        nearest = np.full(total, np.nan, dtype=float)
        used = np.zeros(total, dtype=int)
        spread = np.full(total, np.nan, dtype=float)
        k = min(max(1, self.nearest_stations), len(station_xy))
        maximum_m = self.maximum_distance_km * 1000.0
        quality = (
            np.ones(len(station_xy), dtype=float)
            if station_quality is None
            else np.clip(np.asarray(station_quality, dtype=float), 0.0, 1.0)
        )
        if len(quality) != len(station_xy):
            raise ValueError("station_quality must have one value per station")
        for start in range(0, total, self.chunk_size):
            stop = min(start + self.chunk_size, total)
            points = query_xy[start:stop]
            delta = points[:, None, :] - station_xy[None, :, :]
            distances = np.sqrt(np.sum(delta * delta, axis=2))
            if k < distances.shape[1]:
                indices = np.argpartition(distances, kth=k - 1, axis=1)[:, :k]
            else:
                indices = np.tile(np.arange(distances.shape[1]), (len(points), 1))
            local_dist = np.take_along_axis(distances, indices, axis=1)
            local_values = station_values[indices]
            local_quality = quality[indices]
            order = np.argsort(local_dist, axis=1)
            local_dist = np.take_along_axis(local_dist, order, axis=1)
            local_values = np.take_along_axis(local_values, order, axis=1)
            local_quality = np.take_along_axis(local_quality, order, axis=1)

            nearest_chunk = local_dist[:, 0]
            exact = nearest_chunk <= self.exact_station_threshold_m
            valid = (local_dist <= maximum_m) & (local_quality > 0.0)
            valid_count = valid.sum(axis=1)
            denominator = (
                np.maximum(local_dist, 0.0) ** self.power
                + max(self.distance_smoothing_m, 1e-9) ** self.power
            )
            weights = np.where(valid, local_quality / denominator, 0.0)
            weight_sum = weights.sum(axis=1)
            predicted = np.divide(
                (weights * local_values).sum(axis=1),
                weight_sum,
                out=np.full(len(points), np.nan, dtype=float),
                where=weight_sum > 0,
            )
            predicted[exact] = local_values[exact, 0]
            # A cell with less than the configured minimum remains available, but
            # is flagged through confidence/quality metadata rather than deleted.
            local_mean = np.divide(
                (valid * local_values).sum(axis=1),
                np.maximum(valid_count, 1),
            )
            local_spread = np.sqrt(
                np.divide(
                    (valid * (local_values - local_mean[:, None]) ** 2).sum(axis=1),
                    np.maximum(valid_count, 1),
                )
            )
            values[start:stop] = predicted
            nearest[start:stop] = nearest_chunk
            used[start:stop] = valid_count
            spread[start:stop] = local_spread
        return values, nearest, used, spread

    @staticmethod
    def _quality_vector(frame: pd.DataFrame) -> np.ndarray:
        """Return optional quality/freshness/model/coverage support in [0, 1]."""

        quality = np.ones(len(frame), dtype=float)
        found = False
        for column in ("quality_weight", "q_fresh", "q_model", "q_coverage"):
            if column not in frame.columns:
                continue
            found = True
            values = pd.to_numeric(frame[column], errors="coerce").fillna(1.0)
            quality *= np.clip(values.to_numpy(dtype=float), 0.0, 1.0)
        return quality if found else np.ones(len(frame), dtype=float)

    def interpolate_point(
        self,
        *,
        latitude: float,
        longitude: float,
        stations: pd.DataFrame,
        parameter: str,
        projected_crs: str = "EPSG:2180",
    ) -> dict[str, Any]:
        """Evaluate published station forecasts at one exact WGS84 point.

        No grid cell is selected. Distances and weights are calculated in the
        configured metric CRS. The returned contribution list is suitable for
        an API explanation and adds up to one (apart from rounding).
        """

        frame = _finite_station_frame(stations)
        if frame.empty:
            raise ValueError("No finite station predictions available for interpolation")

        transformer = Transformer.from_crs("EPSG:4326", projected_crs, always_xy=True)
        station_x, station_y = transformer.transform(
            frame["longitude"].astype(float).to_numpy(),
            frame["latitude"].astype(float).to_numpy(),
        )
        point_x, point_y = transformer.transform(float(longitude), float(latitude))
        station_xy = np.column_stack([station_x, station_y])
        point_xy = np.asarray([[point_x, point_y]], dtype=float)
        distances = np.sqrt(np.sum((station_xy - point_xy[0]) ** 2, axis=1))
        quality = self._quality_vector(frame)
        order = np.argsort(distances)[: min(self.nearest_stations, len(frame))]
        local_distances = distances[order]
        local_quality = quality[order]
        valid = (
            (local_distances <= self.maximum_distance_km * 1000.0)
            & (local_quality > 0.0)
        )
        if not valid.any():
            raise ValueError("No station forecast lies within the interpolation radius")

        exact = bool(local_distances[0] <= self.exact_station_threshold_m)
        if exact:
            normalized = np.zeros(len(order), dtype=float)
            normalized[0] = 1.0
            value = float(frame.iloc[order[0]]["predicted_value"])
        else:
            denominator = (
                local_distances**self.power
                + max(self.distance_smoothing_m, 1e-9) ** self.power
            )
            raw_weights = np.where(valid, local_quality / denominator, 0.0)
            normalized = raw_weights / raw_weights.sum()
            values = frame.iloc[order]["predicted_value"].to_numpy(dtype=float)
            value = float(np.sum(normalized * values))

        if parameter in {"PM10", "PM2.5", "precipitation_mm", "precipitation_probability"}:
            value = max(0.0, value)
        if parameter == "precipitation_probability":
            value = min(1.0, value)
        if parameter == "temperature_c":
            value = float(np.clip(value, -90.0, 65.0))

        used_count = int(valid.sum())
        nearest_m = float(local_distances[0])
        confidence = float(
            _support_confidence(
                np.asarray([nearest_m]),
                np.asarray([used_count]),
                confidence_distance_km=self.confidence_distance_km,
                minimum_stations=self.minimum_stations,
                maximum_distance_km=self.maximum_distance_km,
                minimum_confidence=self.minimum_confidence,
            )[0]
        )
        local_values = frame.iloc[order]["predicted_value"].to_numpy(dtype=float)
        spread = float(np.sqrt(np.sum(normalized * (local_values - value) ** 2)))
        confidence = float(np.clip(confidence / (1.0 + spread / 35.0), 0.0, 1.0))
        quality_flag = (
            "limited_station_support"
            if used_count < self.minimum_stations
            else "low_confidence"
            if confidence < 0.35
            else "ok"
        )
        contributions: list[dict[str, Any]] = []
        for position, frame_index in enumerate(order):
            if normalized[position] <= 0.0:
                continue
            row = frame.iloc[int(frame_index)]
            raw_station_id = row.get("station_id")
            contributions.append(
                {
                    "station_id": (
                        int(raw_station_id)
                        if raw_station_id is not None and pd.notna(raw_station_id)
                        else None
                    ),
                    "station_name": row.get("station_name"),
                    "city_name": row.get("city_name"),
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "distance_km": round(float(local_distances[position]) / 1000.0, 4),
                    "quality_weight": round(float(local_quality[position]), 6),
                    "normalized_weight": round(float(normalized[position]), 8),
                    "predicted_value": float(row["predicted_value"]),
                }
            )
        return {
            "value": value,
            "latitude": float(latitude),
            "longitude": float(longitude),
            "projected_crs": projected_crs,
            "method": "quality_weighted_idw",
            "distance_power": float(self.power),
            "distance_smoothing_m": float(self.distance_smoothing_m),
            "exact_station_match": exact,
            "nearest_station_distance_km": nearest_m / 1000.0,
            "stations_used": used_count,
            "local_station_spread": spread,
            "confidence": confidence,
            "quality_flag": quality_flag,
            "contributions": contributions,
        }

    def interpolate(
        self,
        *,
        grid: SpatialGrid,
        stations: pd.DataFrame,
        parameter: str,
        horizon_hours: int,
        origin_time,
        target_time,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        station_frame = _finite_station_frame(stations)
        station_xy, station_values = _station_arrays(station_frame, grid.projected_crs)
        station_quality = self._quality_vector(station_frame)
        query_xy = grid.frame[["x_m", "y_m"]].to_numpy(dtype=float)
        values, nearest_m, used, spread = self._predict_points(
            query_xy, station_xy, station_values, station_quality
        )
        confidence = _support_confidence(
            nearest_m,
            used,
            confidence_distance_km=self.confidence_distance_km,
            minimum_stations=self.minimum_stations,
            maximum_distance_km=self.maximum_distance_km,
            minimum_confidence=self.minimum_confidence,
        )
        # Reduce confidence when nearby station predictions strongly disagree.
        spread_penalty = 1.0 / (1.0 + np.nan_to_num(spread, nan=0.0) / 35.0)
        confidence = np.clip(confidence * spread_penalty, 0.0, 1.0)
        quality = np.where(
            used == 0,
            "no_support",
            np.where(
                used < self.minimum_stations,
                "limited_station_support",
                np.where(confidence < 0.35, "low_confidence", "ok"),
            ),
        )
        if parameter in {"PM10", "PM2.5", "precipitation_mm", "precipitation_probability"}:
            values = np.maximum(values, 0.0)
        if parameter == "precipitation_probability":
            values = np.minimum(values, 1.0)
        if parameter == "temperature_c":
            values = np.clip(values, -90.0, 65.0)
        rgba = rgba_for_values(values, confidence, parameter=parameter)
        frame = grid.frame.copy()
        frame["value"] = values
        frame["confidence"] = confidence
        frame["nearest_station_distance_km"] = nearest_m / 1000.0
        frame["stations_used"] = used
        frame["local_station_spread"] = spread
        frame["quality_flag"] = quality
        frame["parameter"] = parameter
        frame["horizon_hours"] = int(horizon_hours)
        frame["origin_time"] = origin_time
        frame["target_time"] = target_time
        frame[["color_r", "color_g", "color_b", "color_a"]] = rgba
        metrics = self.leave_one_out_metrics(stations, grid.projected_crs)
        metrics.update(
            {
                "grid_cells": len(frame),
                "supported_cells": int(np.isfinite(values).sum()),
                "mean_confidence": float(np.nanmean(confidence)) if len(confidence) else None,
                "median_nearest_station_distance_km": float(np.nanmedian(nearest_m / 1000.0)),
            }
        )
        return frame, metrics

    def leave_one_out_metrics(self, stations: pd.DataFrame, projected_crs: str) -> dict[str, Any]:
        station_xy, station_values = _station_arrays(stations, projected_crs)
        if len(station_values) < 3:
            return {"loo_count": 0, "loo_mae": None, "loo_rmse": None}
        predictions: list[float] = []
        actuals: list[float] = []
        for index in range(len(station_values)):
            mask = np.arange(len(station_values)) != index
            predicted, _, used, _ = self._predict_points(
                station_xy[index : index + 1], station_xy[mask], station_values[mask]
            )
            if used[0] > 0 and np.isfinite(predicted[0]):
                predictions.append(float(predicted[0]))
                actuals.append(float(station_values[index]))
        if not predictions:
            return {"loo_count": 0, "loo_mae": None, "loo_rmse": None}
        errors = np.asarray(predictions) - np.asarray(actuals)
        return {
            "loo_count": len(errors),
            "loo_mae": float(np.mean(np.abs(errors))),
            "loo_rmse": float(np.sqrt(np.mean(errors**2))),
        }


@dataclass(slots=True)
class RBFSpatialInterpolator(IDWInterpolator):
    smoothing: float = 1.0
    kernel: str = "thin_plate_spline"
    algorithm_name: str = "rbf"

    def leave_one_out_metrics(self, stations: pd.DataFrame, projected_crs: str) -> dict[str, Any]:
        """Evaluate the RBF implementation itself, not the IDW support helper."""

        station_xy, station_values = _station_arrays(stations, projected_crs)
        if len(station_values) < max(3, self.minimum_stations + 1):
            return {"loo_count": 0, "loo_mae": None, "loo_rmse": None}
        predictions: list[float] = []
        actuals: list[float] = []
        for index in range(len(station_values)):
            mask = np.arange(len(station_values)) != index
            training_xy = station_xy[mask]
            training_values = station_values[mask]
            if len(training_values) < self.minimum_stations:
                continue
            try:
                model = RBFInterpolator(
                    training_xy,
                    training_values,
                    kernel=self.kernel,
                    smoothing=self.smoothing,
                    neighbors=min(self.nearest_stations, len(training_values)),
                )
                predicted = float(np.asarray(model(station_xy[index : index + 1])).reshape(-1)[0])
            except (ValueError, np.linalg.LinAlgError):
                continue
            if np.isfinite(predicted):
                predictions.append(max(0.0, predicted))
                actuals.append(float(station_values[index]))
        if not predictions:
            return {"loo_count": 0, "loo_mae": None, "loo_rmse": None}
        errors = np.asarray(predictions) - np.asarray(actuals)
        return {
            "loo_count": len(errors),
            "loo_mae": float(np.mean(np.abs(errors))),
            "loo_rmse": float(np.sqrt(np.mean(errors**2))),
        }

    def interpolate(self, **kwargs: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
        grid: SpatialGrid = kwargs["grid"]
        stations: pd.DataFrame = kwargs["stations"]
        station_xy, station_values = _station_arrays(stations, grid.projected_crs)
        if len(station_values) < self.minimum_stations:
            raise ValueError(
                f"RBF requires at least {self.minimum_stations} station predictions"
            )
        query_xy = grid.frame[["x_m", "y_m"]].to_numpy(dtype=float)
        model = RBFInterpolator(
            station_xy,
            station_values,
            kernel=self.kernel,
            smoothing=self.smoothing,
            neighbors=min(self.nearest_stations, len(station_values)),
        )
        predicted = np.asarray(model(query_xy), dtype=float).reshape(-1)
        parameter = str(kwargs.get("parameter") or "PM10")
        if parameter in {"PM10", "PM2.5", "precipitation_mm", "precipitation_probability"}:
            predicted = np.maximum(predicted, 0.0)
        if parameter == "precipitation_probability":
            predicted = np.minimum(predicted, 1.0)
        if parameter == "temperature_c":
            predicted = np.clip(predicted, -90.0, 65.0)
        # IDW is used only to calculate support distance, neighbour count and
        # confidence.  RBF values outside the configured support radius are masked
        # instead of presenting visually attractive but unsupported extrapolation.
        support_frame, metrics = IDWInterpolator.interpolate(self, **kwargs)
        supported = support_frame["stations_used"].to_numpy(dtype=int) > 0
        predicted = np.where(supported, predicted, np.nan)
        support_frame["value"] = predicted
        rgba = rgba_for_values(
            predicted,
            support_frame["confidence"].to_numpy(dtype=float),
            parameter=parameter,
        )
        support_frame[["color_r", "color_g", "color_b", "color_a"]] = rgba
        metrics["algorithm"] = self.algorithm_name
        metrics["supported_cells"] = int(np.isfinite(predicted).sum())
        return support_frame, metrics
