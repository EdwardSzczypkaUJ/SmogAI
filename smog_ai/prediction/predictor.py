from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion
from smog_ai.database.repository import add_forecast_idempotent, set_application_state
from smog_ai.domain import StageStats
from smog_ai.features.builder import FEATURE_COLUMNS, build_latest_feature_rows
from smog_ai.time_utils import ensure_utc, utc_now
from smog_ai.training.trainer import ensure_baseline_models


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def load_artifact(model: ModelVersion) -> dict[str, Any]:
    if not model.artifact_path:
        return {"algorithm": model.algorithm}
    path = Path(model.artifact_path)
    if not path.exists():
        return {"algorithm": model.algorithm}
    payload = joblib.load(path)
    return payload if isinstance(payload, dict) else {"algorithm": model.algorithm, "estimator": payload}


def predict_row(model: ModelVersion, artifact: dict[str, Any], row: Any) -> float:
    algorithm = artifact.get("algorithm", model.algorithm)
    current = float(row["value"])
    if algorithm == "persistence":
        return max(0.0, current)
    estimator = artifact.get("estimator")
    if algorithm == "historical_mean":
        mean = artifact.get("mean")
        if mean is None and isinstance(estimator, dict):
            mean = estimator.get("mean")
        return max(0.0, float(mean if mean is not None else current))
    if estimator is None:
        return max(0.0, current)
    matrix = row.to_frame().T.reindex(columns=FEATURE_COLUMNS)
    return max(0.0, float(estimator.predict(matrix)[0]))


def create_forecasts(session: Session, config: AppConfig) -> StageStats:
    stats = StageStats()
    latest_by_parameter = {
        parameter: build_latest_feature_rows(session, parameter=parameter)
        for parameter in config.training.parameters
    }
    if all(frame.empty for frame in latest_by_parameter.values()):
        return StageStats(
            skipped=len(config.training.parameters),
            warnings=1,
            details={"active_model_versions": [], "reason": "no_valid_air_measurements"},
        )
    ensure_baseline_models(session, config)
    created_at = utc_now()
    fresh_cutoff = created_at - timedelta(hours=config.quality.stale_air_hours)
    model_versions: list[str] = []
    stale_rows_skipped = 0
    non_future_targets_skipped = 0
    for parameter in config.training.parameters:
        latest = latest_by_parameter[parameter]
        if latest.empty:
            stats.skipped += 1
            continue
        parsed_origins = pd.to_datetime(latest["measurement_time"], utc=True, errors="coerce")
        fresh_mask = parsed_origins.notna() & (parsed_origins >= fresh_cutoff) & (parsed_origins <= created_at)
        stale_count = int((~fresh_mask).sum())
        if stale_count:
            # Account per configured horizon because each stale station row would
            # otherwise produce one invalid forecast for every horizon.
            skipped_forecasts = stale_count * len(config.training.horizons_hours)
            stats.skipped += skipped_forecasts
            stale_rows_skipped += skipped_forecasts
            stats.warnings += 1
        latest = latest.loc[fresh_mask].copy()
        if latest.empty:
            continue
        for horizon in config.training.horizons_hours:
            model = session.scalar(
                select(ModelVersion).where(
                    ModelVersion.parameter == parameter,
                    ModelVersion.forecast_horizon == horizon,
                    ModelVersion.active.is_(True),
                )
            )
            if model is None:
                stats.errors += 1
                continue
            artifact = load_artifact(model)
            model_versions.append(model.semantic_version)
            for _, row in latest.iterrows():
                origin = ensure_utc(row["measurement_time"].to_pydatetime())
                target = origin + timedelta(hours=horizon)
                # A forecast must be persisted before its target is known.  This
                # guard also protects against legacy/stale source rows even when
                # a custom freshness setting is unusually permissive.
                if target <= created_at:
                    stats.skipped += 1
                    non_future_targets_skipped += 1
                    continue
                predicted = predict_row(model, artifact, row)
                features = {column: _jsonable(row.get(column)) for column in FEATURE_COLUMNS}
                features["current_value"] = _jsonable(row["value"])
                inserted = add_forecast_idempotent(
                    session,
                    {
                        "model_version_id": model.id,
                        "air_station_id": int(row["air_station_id"]),
                        "parameter": parameter,
                        "forecast_created_at": created_at,
                        "forecast_origin_time": origin,
                        "target_time": target,
                        "forecast_horizon": horizon,
                        "predicted_value": predicted,
                        "features_json": features,
                    },
                )
                stats.inserted += int(inserted)
                stats.skipped += int(not inserted)
    if stats.inserted:
        set_application_state(session, "last_forecast_at", created_at.isoformat())
    stats.details = {
        "active_model_versions": sorted(set(model_versions)),
        "stale_forecasts_skipped": stale_rows_skipped,
        "non_future_targets_skipped": non_future_targets_skipped,
        "freshness_cutoff": fresh_cutoff.isoformat(),
    }
    return stats
