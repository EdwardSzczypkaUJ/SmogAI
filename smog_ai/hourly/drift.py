from __future__ import annotations

import math
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import Forecast, ForecastResult, ModelVersion
from smog_ai.hourly.trainer import HOURLY_MODEL_HORIZON_SENTINEL


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


def hourly_drift_status(session: Session, config: AppConfig) -> dict[str, Any]:
    settings = config.hourly_forecasting.drift
    payload: dict[str, Any] = {
        "enabled": bool(settings.enabled),
        "retrain_recommended": False,
        "targets": {},
    }
    if not settings.enabled:
        return payload

    for target in config.hourly_forecasting.targets:
        model = _active_model(session, target)
        if model is None:
            payload["targets"][target] = {
                "status": "missing_active_model",
                "drift": True,
            }
            payload["retrain_recommended"] = True
            continue

        limit = int(settings.recent_window_rows + settings.reference_window_rows)
        rows = session.execute(
            select(ForecastResult.absolute_error, ForecastResult.signed_error)
            .join(Forecast, Forecast.id == ForecastResult.forecast_id)
            .where(
                Forecast.model_version_id == model.id,
                Forecast.parameter == target,
                ForecastResult.verification_status == "verified",
                ForecastResult.absolute_error.is_not(None),
                ForecastResult.signed_error.is_not(None),
            )
            .order_by(ForecastResult.verified_at.desc())
            .limit(limit)
        ).all()
        if len(rows) < settings.minimum_verified_rows:
            payload["targets"][target] = {
                "status": "insufficient_verified_rows",
                "model_version": model.semantic_version,
                "rows": len(rows),
                "minimum": settings.minimum_verified_rows,
                "drift": False,
            }
            continue

        absolute = np.asarray([float(row[0]) for row in rows], dtype=float)
        signed = np.asarray([float(row[1]) for row in rows], dtype=float)
        recent_count = min(int(settings.recent_window_rows), len(absolute))
        recent_abs = absolute[:recent_count]
        recent_signed = signed[:recent_count]
        reference_abs = absolute[
            recent_count : recent_count + int(settings.reference_window_rows)
        ]
        reference_signed = signed[
            recent_count : recent_count + int(settings.reference_window_rows)
        ]
        if len(reference_abs) < 20:
            payload["targets"][target] = {
                "status": "insufficient_reference_window",
                "model_version": model.semantic_version,
                "recent_rows": len(recent_abs),
                "reference_rows": len(reference_abs),
                "drift": False,
            }
            continue

        recent_mae = float(np.mean(recent_abs))
        reference_mae = float(np.mean(reference_abs))
        recent_bias = float(np.mean(recent_signed))
        reference_bias = float(np.mean(reference_signed))
        relative_increase = (
            (recent_mae - reference_mae) / reference_mae
            if reference_mae > 0
            else (math.inf if recent_mae > 0 else 0.0)
        )
        bias_threshold = float(
            settings.bias_absolute_thresholds.get(target, math.inf)
        )
        mae_drift = relative_increase > settings.mae_relative_increase_threshold
        bias_drift = abs(recent_bias) > bias_threshold
        drift = bool(mae_drift or bias_drift)
        payload["targets"][target] = {
            "status": "ok",
            "model_version": model.semantic_version,
            "recent_rows": len(recent_abs),
            "reference_rows": len(reference_abs),
            "recent_mae": recent_mae,
            "reference_mae": reference_mae,
            "mae_relative_increase": relative_increase,
            "mae_threshold": settings.mae_relative_increase_threshold,
            "recent_bias": recent_bias,
            "reference_bias": reference_bias,
            "absolute_bias_threshold": bias_threshold,
            "mae_drift": bool(mae_drift),
            "bias_drift": bool(bias_drift),
            "drift": drift,
        }
        payload["retrain_recommended"] = (
            payload["retrain_recommended"] or drift
        )

    return payload


__all__ = ["hourly_drift_status"]
