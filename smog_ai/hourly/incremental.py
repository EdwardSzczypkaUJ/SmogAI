from __future__ import annotations

import copy
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import SGDRegressor
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.config import AppConfig
from smog_ai.database.models import Forecast, ForecastResult, ModelVersion
from smog_ai.domain import StageStats
from smog_ai.hourly.trainer import (
    HOURLY_MODEL_HORIZON_SENTINEL,
    _activate,
    _register_model,
)
from smog_ai.progress import ProgressReporter, WeightedStageProgress
from smog_ai.time_utils import utc_now

# Keep the correction model intentionally small.  It learns the recent residual
# of the expensive champion without replacing or retraining that champion.
RESIDUAL_FEATURE_COLUMNS: tuple[str, ...] = (
    "base_prediction",
    "horizon_hours",
    "target_hour_sin",
    "target_hour_cos",
    "target_year_sin",
    "target_year_cos",
)


def residual_feature_matrix(
    base_prediction: np.ndarray | pd.Series,
    horizon_hours: np.ndarray | pd.Series,
    target_time: pd.Series | pd.DatetimeIndex | list[Any],
) -> pd.DataFrame:
    base = pd.to_numeric(pd.Series(base_prediction), errors="coerce")
    horizon = pd.to_numeric(pd.Series(horizon_hours), errors="coerce")
    times = pd.to_datetime(pd.Series(target_time), utc=True, errors="coerce")
    hour = times.dt.hour + times.dt.minute / 60.0
    day = times.dt.dayofyear
    output = pd.DataFrame(
        {
            "base_prediction": base,
            "horizon_hours": horizon,
            "target_hour_sin": np.sin(2.0 * np.pi * hour / 24.0),
            "target_hour_cos": np.cos(2.0 * np.pi * hour / 24.0),
            "target_year_sin": np.sin(2.0 * np.pi * day / 365.2425),
            "target_year_cos": np.cos(2.0 * np.pi * day / 365.2425),
        }
    )
    return output.replace([np.inf, -np.inf], np.nan)


def apply_residual_correction(
    artifact: dict[str, Any],
    frame: pd.DataFrame,
    base_values: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(base_values, dtype=float)
    spec = artifact.get("residual_corrector")
    if not isinstance(spec, dict) or not bool(spec.get("active", False)):
        return values.copy(), np.zeros(len(values), dtype=float)

    scaler = spec.get("scaler")
    model = spec.get("model")
    if scaler is None or model is None or frame.empty:
        return values.copy(), np.zeros(len(values), dtype=float)

    features = residual_feature_matrix(
        values,
        frame.get("horizon_hours", pd.Series(np.nan, index=frame.index)),
        frame.get("target_time", pd.Series(pd.NaT, index=frame.index)),
    ).reindex(columns=list(spec.get("feature_columns") or RESIDUAL_FEATURE_COLUMNS))
    valid = features.notna().all(axis=1).to_numpy()
    correction = np.zeros(len(values), dtype=float)
    if valid.any():
        transformed = scaler.transform(features.loc[valid].to_numpy(dtype=float))
        predicted = np.asarray(model.predict(transformed), dtype=float)
        clip = float(spec.get("maximum_absolute_correction", np.inf))
        if math.isfinite(clip) and clip > 0:
            predicted = np.clip(predicted, -clip, clip)
        correction[valid] = predicted
    return values + correction, correction


def _load_artifact(model: ModelVersion) -> dict[str, Any]:
    if not model.artifact_path:
        raise FileNotFoundError(f"Model {model.semantic_version} has no artifact path")
    payload = joblib.load(Path(model.artifact_path))
    if not isinstance(payload, dict):
        raise TypeError("Hourly model artifact must be a dictionary")
    return payload


def _active_model(session: Session, target: str) -> ModelVersion | None:
    return session.scalar(
        select(ModelVersion)
        .where(
            ModelVersion.parameter == target,
            ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
            ModelVersion.active.is_(True),
        )
        .order_by(ModelVersion.activated_at.desc(), ModelVersion.created_at.desc())
    )


def _verified_rows(
    session: Session,
    *,
    model: ModelVersion,
    lookback_days: int,
    maximum_rows: int,
) -> pd.DataFrame:
    cutoff = utc_now() - timedelta(days=int(lookback_days))
    rows = session.execute(
        select(Forecast, ForecastResult)
        .join(ForecastResult, ForecastResult.forecast_id == Forecast.id)
        .where(
            Forecast.model_version_id == model.id,
            Forecast.parameter == model.parameter,
            ForecastResult.verification_status == "verified",
            ForecastResult.actual_value.is_not(None),
            Forecast.target_time >= cutoff,
        )
        .order_by(Forecast.target_time.desc())
        .limit(int(maximum_rows))
    ).all()

    output: list[dict[str, Any]] = []
    for forecast, result in reversed(rows):
        features = dict(forecast.features_json or {})
        base = features.get("base_predicted_value", forecast.predicted_value)
        try:
            base_value = float(base)
            actual = float(result.actual_value)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(base_value) and math.isfinite(actual)):
            continue
        output.append(
            {
                "base_prediction": base_value,
                "actual_value": actual,
                "residual": actual - base_value,
                "horizon_hours": int(forecast.forecast_horizon),
                "target_time": forecast.target_time,
                "verified_at": result.verified_at,
            }
        )
    return pd.DataFrame(output)


def _new_corrector(config: AppConfig) -> tuple[StandardScaler, SGDRegressor]:
    settings = config.hourly_forecasting.incremental_residual
    scaler = StandardScaler()
    model = SGDRegressor(
        loss="huber",
        penalty="l2",
        alpha=float(settings.alpha),
        learning_rate="invscaling",
        eta0=float(settings.eta0),
        power_t=0.25,
        random_state=int(settings.random_state),
        average=True,
        max_iter=1,
        tol=None,
        warm_start=True,
    )
    return scaler, model


def update_hourly_residual_correctors(
    session: Session,
    config: AppConfig,
    progress: ProgressReporter | None = None,
) -> StageStats:
    settings = config.hourly_forecasting.incremental_residual
    if not settings.enabled:
        return StageStats(skipped=1, details={"reason": "incremental_residual_disabled"})

    targets = list(config.hourly_forecasting.targets)
    work = WeightedStageProgress(
        progress,
        stage="incremental_update",
        total_weight=max(1.0, float(len(targets))),
    )
    stats = StageStats()
    details: list[dict[str, Any]] = []

    for target in targets:
        with work.task(
            f"{target}: residual partial_fit",
            1.0,
            task_key=f"incremental:{target}",
            fallback_seconds=45.0,
            detail={"target": target, "phase": "residual_partial_fit"},
        ):
            active = _active_model(session, target)
            if active is None:
                stats.skipped += 1
                details.append({"target": target, "status": "missing_active_model"})
                continue

            frame = _verified_rows(
                session,
                model=active,
                lookback_days=settings.lookback_days,
                maximum_rows=settings.maximum_rows_per_update,
            )
            if len(frame) < settings.minimum_verified_rows:
                stats.skipped += 1
                details.append(
                    {
                        "target": target,
                        "status": "insufficient_verified_rows",
                        "rows": len(frame),
                        "minimum": settings.minimum_verified_rows,
                    }
                )
                continue

            split = max(1, min(len(frame) - 1, int(len(frame) * 0.8)))
            train = frame.iloc[:split].copy()
            valid = frame.iloc[split:].copy()
            X_train = residual_feature_matrix(
                train["base_prediction"],
                train["horizon_hours"],
                train["target_time"],
            )
            X_valid = residual_feature_matrix(
                valid["base_prediction"],
                valid["horizon_hours"],
                valid["target_time"],
            )
            train_mask = X_train.notna().all(axis=1) & train["residual"].notna()
            valid_mask = X_valid.notna().all(axis=1) & valid["residual"].notna()
            X_train = X_train.loc[train_mask]
            y_train = train.loc[train_mask, "residual"].to_numpy(dtype=float)
            X_valid = X_valid.loc[valid_mask]
            y_valid = valid.loc[valid_mask, "residual"].to_numpy(dtype=float)
            base_valid = valid.loc[valid_mask, "base_prediction"].to_numpy(dtype=float)
            actual_valid = valid.loc[valid_mask, "actual_value"].to_numpy(dtype=float)

            if len(X_train) < 20 or len(X_valid) < 5:
                stats.skipped += 1
                details.append(
                    {
                        "target": target,
                        "status": "insufficient_clean_rows",
                        "train_rows": len(X_train),
                        "validation_rows": len(X_valid),
                    }
                )
                continue

            artifact = _load_artifact(active)
            existing = artifact.get("residual_corrector")
            if isinstance(existing, dict) and existing.get("scaler") is not None:
                scaler = copy.deepcopy(existing["scaler"])
                model = copy.deepcopy(existing["model"])
            else:
                scaler, model = _new_corrector(config)

            train_array = X_train.to_numpy(dtype=float)
            scaler.partial_fit(train_array)
            model.partial_fit(scaler.transform(train_array), y_train)

            corrections = np.asarray(
                model.predict(scaler.transform(X_valid.to_numpy(dtype=float))),
                dtype=float,
            )
            residual_scale = float(np.nanstd(y_train))
            maximum_absolute_correction = max(1.0, 3.0 * residual_scale)
            corrections = np.clip(
                corrections,
                -maximum_absolute_correction,
                maximum_absolute_correction,
            )
            corrected_valid = base_valid + corrections
            if target == "precipitation_mm":
                corrected_valid = np.clip(corrected_valid, 0.0, None)
            elif target == "temperature_c":
                corrected_valid = np.clip(corrected_valid, -90.0, 65.0)
            else:
                definition = create_air_parameter_registry(config).get(target)
                if definition is not None:
                    lower = definition.valid_min
                    if lower is None and not definition.allow_negative:
                        lower = 0.0
                    upper = definition.valid_max
                    corrected_valid = np.clip(
                        corrected_valid,
                        lower if lower is not None else -np.inf,
                        upper if upper is not None else np.inf,
                    )

            baseline_mae = float(np.mean(np.abs(actual_valid - base_valid)))
            corrected_mae = float(np.mean(np.abs(actual_valid - corrected_valid)))
            improvement = (
                (baseline_mae - corrected_mae) / baseline_mae
                if baseline_mae > 0
                else 0.0
            )
            if improvement < settings.minimum_mae_improvement_fraction:
                stats.skipped += 1
                details.append(
                    {
                        "target": target,
                        "status": "quality_gate_rejected",
                        "rows": len(frame),
                        "baseline_mae": baseline_mae,
                        "corrected_mae": corrected_mae,
                        "improvement_fraction": improvement,
                        "minimum_improvement_fraction": (
                            settings.minimum_mae_improvement_fraction
                        ),
                    }
                )
                continue

            new_artifact = copy.deepcopy(artifact)
            new_artifact["residual_corrector"] = {
                "active": True,
                "feature_columns": list(RESIDUAL_FEATURE_COLUMNS),
                "scaler": scaler,
                "model": model,
                "maximum_absolute_correction": maximum_absolute_correction,
                "updated_at": utc_now().isoformat(),
                "verified_rows": len(frame),
                "baseline_mae": baseline_mae,
                "corrected_mae": corrected_mae,
                "improvement_fraction": improvement,
                "base_model_version": active.semantic_version,
            }
            metrics = dict(active.metrics_json or {})
            metrics["incremental_residual"] = {
                "active": True,
                "source_model_version": active.semantic_version,
                "verified_rows": len(frame),
                "train_rows": len(X_train),
                "validation_rows": len(X_valid),
                "baseline_mae": baseline_mae,
                "corrected_mae": corrected_mae,
                "improvement_fraction": improvement,
                "updated_at": utc_now().isoformat(),
            }
            model_row = _register_model(
                session,
                config,
                target=target,
                provider_name=active.algorithm,
                artifact=new_artifact,
                metrics=metrics,
                data_start=active.training_data_start,
                data_end=active.training_data_end,
            )
            _activate(session, config, model_row)
            stats.inserted += 1
            details.append(
                {
                    "target": target,
                    "status": "activated",
                    "source_model_version": active.semantic_version,
                    "new_model_version": model_row.semantic_version,
                    "verified_rows": len(frame),
                    "baseline_mae": baseline_mae,
                    "corrected_mae": corrected_mae,
                    "improvement_fraction": improvement,
                }
            )

    work.complete(name="incremental residual update completed")
    stats.details = {"targets": details}
    return stats


__all__ = [
    "RESIDUAL_FEATURE_COLUMNS",
    "apply_residual_correction",
    "residual_feature_matrix",
    "update_hourly_residual_correctors",
]
