from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.air_parameters import (
    configured_air_targets,
    create_air_parameter_registry,
    is_air_target,
)
from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion
from smog_ai.database.repository import add_forecast_idempotent, set_application_state
from smog_ai.domain import StageStats
from smog_ai.hourly.features import (
    KEY_COLUMNS,
    PM_HOURLY_FEATURE_COLUMNS,
    WEATHER_HOURLY_FEATURE_COLUMNS,
    build_pm_prediction_rows,
    build_weather_prediction_rows,
    latest_common_origin_time,
)
from smog_ai.hourly.incremental import apply_residual_correction
from smog_ai.hourly.time_contract import (
    ForecastTimeContract,
    SourceDataTooOldError,
    build_forecast_time_contract,
)
from smog_ai.hourly.trainer import (
    HOURLY_MODEL_HORIZON_SENTINEL,
    create_hourly_model_registry,
    ensure_hourly_baseline_models,
)
from smog_ai.modeling import ModelPredictContext
from smog_ai.progress import ProgressReporter, WeightedStageProgress
from smog_ai.quality import quality_metadata
from smog_ai.time_utils import ensure_utc, utc_now

PARAMETER_UNITS: dict[str, str] = {
    "temperature_c": "°C",
    "precipitation_probability": "1",
}


def _parameter_unit(config: AppConfig, target: str) -> str | None:
    if target in {"precipitation_mm", "precipitation_amount_if_rain_mm"}:
        period = config.hourly_forecasting.precipitation.accumulation_period_hours
        return f"mm/{period}h"
    if is_air_target(config, target):
        return create_air_parameter_registry(config).require(target).canonical_unit
    return PARAMETER_UNITS.get(target)


def _clip_parameter_values(
    config: AppConfig, target: str, values: np.ndarray
) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if is_air_target(config, target):
        definition = create_air_parameter_registry(config).require(target)
        lower = definition.valid_min
        upper = definition.valid_max
        if not definition.allow_negative and lower is None:
            lower = 0.0
        if lower is not None or upper is not None:
            values = np.clip(
                values,
                lower if lower is not None else -np.inf,
                upper if upper is not None else np.inf,
            )
        return values
    if target == "precipitation_mm":
        return np.clip(values, 0.0, None)
    if target == "temperature_c":
        return np.clip(values, -90.0, 65.0)
    return values


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    return value


def load_hourly_artifact(model: ModelVersion) -> dict[str, Any]:
    if not model.artifact_path:
        raise FileNotFoundError(f"Model {model.semantic_version} has no artifact path")
    path = Path(model.artifact_path)
    if not path.exists():
        raise FileNotFoundError(f"Hourly model artifact does not exist: {path}")
    payload = joblib.load(path)
    if not isinstance(payload, dict):
        raise TypeError(f"Hourly model artifact must be a dictionary: {path}")
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


def _assert_artifact_horizon_support(
    *,
    model: ModelVersion,
    artifact: dict[str, Any],
    time_contract: ForecastTimeContract,
) -> None:
    """Reject extrapolation beyond the horizons seen by the trained artifact."""

    raw = artifact.get("horizons_hours") or (
        artifact.get("metadata") or {}
    ).get("horizons_hours")
    supported = sorted({int(value) for value in (raw or [])})
    required = sorted(set(int(value) for value in time_contract.model_horizons))
    if not supported:
        raise RuntimeError(
            "Hourly artifact has no horizon provenance: "
            f"target={model.parameter}, version={model.semantic_version}"
        )
    missing = [value for value in required if value not in supported]
    if missing:
        raise RuntimeError(
            "Active hourly model does not cover the serving time contract; "
            "retrain on extended model horizons before prediction: "
            f"target={model.parameter}, version={model.semantic_version}, "
            f"supported={supported[0]}..{supported[-1]}, "
            f"required={required[0]}..{required[-1]}, missing={missing}"
        )


def _predict_bundle(
    *,
    config: AppConfig,
    registry,
    model: ModelVersion,
    artifact: dict[str, Any],
    frame: pd.DataFrame,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    target = str(artifact.get("target") or model.parameter)
    provider_name = str(artifact.get("provider") or model.algorithm)
    task = str(artifact.get("task") or ("hurdle_regression" if target == "precipitation_mm" else "regression"))
    feature_columns = tuple(artifact.get("feature_columns") or [])
    provider = registry.get(provider_name)
    bundle = provider.predict(
        artifact["provider_artifact"],
        frame.reindex(columns=list(feature_columns)),
        context=ModelPredictContext(
            target_name=target,
            feature_columns=feature_columns,
            task=task,  # type: ignore[arg-type]
            metadata=dict(artifact.get("metadata") or {}),
        ),
    )
    base_values = np.asarray(bundle.values, dtype=float)
    values, residual_correction = apply_residual_correction(
        artifact,
        frame,
        base_values,
    )
    values = _clip_parameter_values(config, target, values)

    extras = {key: np.asarray(value, dtype=float) for key, value in bundle.extras.items()}
    extras["base_predicted_value"] = base_values
    extras["residual_correction"] = residual_correction
    quantiles: dict[str, np.ndarray] = {}
    for label, quantile_spec in (artifact.get("quantile_artifacts") or {}).items():
        quantile_provider = registry.get(str(quantile_spec["provider"]))
        quantile_artifact = quantile_spec["artifact"]
        quantile_value = float(quantile_spec["quantile"])
        quantile_bundle = quantile_provider.predict(
            quantile_artifact,
            frame.reindex(columns=list(feature_columns)),
            context=ModelPredictContext(
                target_name=target,
                feature_columns=feature_columns,
                task="regression",
                metadata={"quantile": quantile_value},
            ),
        )
        predicted = np.asarray(quantile_bundle.values, dtype=float)
        # The residual corrector represents a location/lead-time bias shift, so
        # apply the same shift to all quantiles and preserve interval width.
        predicted = predicted + residual_correction
        predicted = _clip_parameter_values(config, target, predicted)
        quantiles[str(label)] = predicted
    extras.update(quantiles)
    return values, extras


def _weather_predictions(
    session: Session,
    config: AppConfig,
    *,
    origin_time,
    registry,
    models: dict[str, ModelVersion],
    artifacts: dict[str, dict[str, Any]],
    time_contract: ForecastTimeContract,
) -> tuple[pd.DataFrame, dict[str, dict[str, np.ndarray]]]:
    rows = build_weather_prediction_rows(
        session,
        origin_time=origin_time,
        time_contract=time_contract,
        max_days=max(7, min(config.hourly_forecasting.maximum_training_days, 60)),
    )
    if rows.empty:
        return rows, {}

    # Preserve the complete provider input frame.  Besides the station/time keys,
    # the persisted forecast audit payload must retain the exact lags, rolling
    # statistics and target-time features consumed by the weather model.
    output = rows.copy()
    metadata: dict[str, dict[str, np.ndarray]] = {}
    for target in ("temperature_c", "precipitation_mm"):
        if target not in models:
            continue
        values, extras = _predict_bundle(
            config=config,
            registry=registry,
            model=models[target],
            artifact=artifacts[target],
            frame=rows,
        )
        if target == "temperature_c":
            output["predicted_temperature_c"] = values
        else:
            output["predicted_precipitation_mm"] = values
            output["predicted_precipitation_probability"] = np.clip(
                extras.get("precipitation_probability", np.zeros(len(rows))), 0.0, 1.0
            )
            output["predicted_precipitation_amount_if_rain_mm"] = np.clip(
                extras.get("precipitation_amount_if_rain_mm", np.zeros(len(rows))),
                0.0,
                None,
            )
        metadata[target] = extras
    return output, metadata


def _pm_prediction_frame(
    session: Session,
    config: AppConfig,
    *,
    parameter: str,
    origin_time,
    weather_predictions: pd.DataFrame,
    time_contract: ForecastTimeContract,
) -> pd.DataFrame:
    parameter_registry = create_air_parameter_registry(config)
    frame = build_pm_prediction_rows(
        session,
        parameter=parameter,
        origin_time=origin_time,
        time_contract=time_contract,
        max_days=max(7, min(config.hourly_forecasting.maximum_training_days, 60)),
        auxiliary_parameters=parameter_registry.auxiliary_codes,
    )
    if frame.empty:
        return frame
    weather_columns = [
        *KEY_COLUMNS,
        "predicted_temperature_c",
        "predicted_precipitation_probability",
        "predicted_precipitation_mm",
    ]
    available = [column for column in weather_columns if column in weather_predictions.columns]
    if available:
        frame = frame.drop(
            columns=[
                column
                for column in (
                    "predicted_temperature_c",
                    "predicted_precipitation_probability",
                    "predicted_precipitation_mm",
                )
                if column in frame.columns
            ]
        ).merge(weather_predictions[available], on=KEY_COLUMNS, how="left")
    for column in (
        "predicted_temperature_c",
        "predicted_precipitation_probability",
        "predicted_precipitation_mm",
    ):
        if column not in frame.columns:
            frame[column] = np.nan
    return frame


def _feature_payload(
    row: pd.Series,
    *,
    target: str,
    model: ModelVersion,
    artifact: dict[str, Any],
    config: AppConfig,
    quantiles: dict[str, float] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    columns = artifact.get("feature_columns") or []
    payload = {column: _jsonable(row.get(column)) for column in columns}
    payload.update(
        {
            "forecast_mode": "horizon-conditioned-hourly",
            "forecast_origin_time": _jsonable(row.get("measurement_time")),
            "target_time": _jsonable(row.get("target_time")),
            # ``horizon_hours`` remains the estimator input for backward
            # compatibility.  Serving/API semantics are explicit and separate.
            "horizon_hours": int(row.get("horizon_hours")),
            "model_horizon_hours": int(
                row.get("model_horizon_hours", row.get("horizon_hours"))
            ),
            "serving_lead_hours": int(
                row.get("serving_lead_hours", row.get("horizon_hours"))
            ),
            "serving_anchor_time": _jsonable(row.get("serving_anchor_time")),
            "source_age_hours": _jsonable(row.get("source_age_hours")),
            "source_delay_to_anchor_hours": _jsonable(
                row.get("source_delay_to_anchor_hours")
            ),
            "source_measurement_time": _jsonable(row.get("source_measurement_time")),
            "model_version": model.semantic_version,
            "model_provider": artifact.get("provider") or model.algorithm,
            "unit": _parameter_unit(config, target),
            "exact_hour": True,
            "server_computation": "none",
        }
    )
    payload.update(quality_metadata(target, dict(model.metrics_json or {})))
    if target in {"precipitation_mm", "precipitation_amount_if_rain_mm"}:
        period = config.hourly_forecasting.precipitation.accumulation_period_hours
        payload.update(
            {
                "precipitation_accumulation_period_hours": period,
                "precipitation_semantics": {
                    "accumulation_period_hours": period,
                    "ending_at_target_time": True,
                    "disaggregated_to_hourly": False,
                },
            }
        )
    if quantiles:
        payload["prediction_quantiles"] = quantiles
    if extra:
        payload.update({key: _jsonable(value) for key, value in extra.items()})
    return payload


def _persist_predictions(
    session: Session,
    *,
    model: ModelVersion,
    artifact: dict[str, Any],
    config: AppConfig,
    frame: pd.DataFrame,
    parameter: str,
    values: np.ndarray,
    extras: dict[str, np.ndarray],
    created_at,
    stats: StageStats,
) -> None:
    quantile_labels = [key for key in extras if key.startswith("q")]
    for position, (_, row) in enumerate(frame.iterrows()):
        origin = ensure_utc(pd.Timestamp(row["measurement_time"]).to_pydatetime())
        target = ensure_utc(pd.Timestamp(row["target_time"]).to_pydatetime())
        if target <= created_at:
            stats.skipped += 1
            continue
        quantiles = {
            label: float(extras[label][position])
            for label in quantile_labels
            if position < len(extras[label]) and np.isfinite(extras[label][position])
        }
        predicted = float(values[position])
        if not math.isfinite(predicted):
            stats.skipped += 1
            stats.warnings += 1
            continue
        inserted = add_forecast_idempotent(
            session,
            {
                "model_version_id": model.id,
                "air_station_id": int(row["air_station_id"]),
                "parameter": parameter,
                "forecast_created_at": created_at,
                "forecast_origin_time": origin,
                "target_time": target,
                "forecast_horizon": int(
                    row.get("serving_lead_hours", row["horizon_hours"])
                ),
                "predicted_value": predicted,
                "features_json": _feature_payload(
                    row,
                    target=parameter,
                    model=model,
                    artifact=artifact,
                    config=config,
                    quantiles=quantiles,
                    extra={
                        key: extras[key][position]
                        for key in (
                            "base_predicted_value",
                            "residual_correction",
                        )
                        if key in extras and position < len(extras[key])
                    },
                ),
            },
        )
        stats.inserted += int(inserted)
        stats.skipped += int(not inserted)


def create_hourly_forecasts(
    session: Session,
    config: AppConfig,
    progress: ProgressReporter | None = None,
) -> StageStats:
    """Create exact full-hour forecasts for every configured horizon.

    The active model for each target is a single horizon-conditioned estimator
    stored with ``forecast_horizon=0``.  The resulting rows are still persisted
    with their actual integer horizon (1..48) and exact target timestamp.
    """

    if not config.hourly_forecasting.enabled:
        if progress is not None:
            progress.complete_stage(
                "prediction",
                task="hourly prediction disabled",
                detail={"reason": "hourly_forecasting_disabled"},
            )
        return StageStats(skipped=1, details={"reason": "hourly_forecasting_disabled"})

    work = WeightedStageProgress(progress, stage="prediction", total_weight=5.0)
    with work.task(
        "ensure active baseline models",
        0.20,
        task_key="prediction:baseline-models",
        fallback_seconds=15.0,
    ):
        ensure_hourly_baseline_models(session, config)

    with work.task(
        "determine exact common origin time",
        0.20,
        task_key="prediction:origin-time",
        fallback_seconds=10.0,
    ):
        air_targets = configured_air_targets(config)
        origin_time = latest_common_origin_time(
            session,
            parameters=air_targets,
        )
    if origin_time is None:
        work.complete(name="prediction skipped — no common origin")
        return StageStats(
            skipped=1,
            warnings=1,
            details={"reason": "no_common_hourly_origin_time"},
        )

    registry = create_hourly_model_registry(config)
    models: dict[str, ModelVersion] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    load_weight = 0.40 / max(1, len(config.hourly_forecasting.targets))
    for target in config.hourly_forecasting.targets:
        with work.task(
            f"load active model: {target}",
            load_weight,
            task_key=f"prediction:load-model:{target}",
            fallback_seconds=10.0,
            detail={"target": target, "phase": "load_model"},
        ):
            model = _active_model(session, target)
            if model is None:
                missing.append(target)
                continue
            try:
                models[target] = model
                artifacts[target] = load_hourly_artifact(model)
            except Exception as exc:
                missing.append(target)
                artifacts.pop(target, None)
                models.pop(target, None)
                artifacts[f"__error__{target}"] = {"error": str(exc)}

    stats = StageStats()
    if not models:
        work.complete(name="prediction failed — no active models")
        return StageStats(
            errors=1,
            details={"reason": "no_active_hourly_models", "missing_targets": missing},
        )

    created_at = utc_now()
    try:
        time_contract = build_forecast_time_contract(
            source_origin_time=origin_time,
            forecast_created_at=created_at,
            serving_horizon_hours=(
                config.hourly_forecasting.serving_horizon_count
            ),
            maximum_source_delay_hours=(
                config.hourly_forecasting.maximum_source_delay_hours
            ),
            maximum_model_horizon_hours=(
                config.hourly_forecasting.model_horizon_maximum
            ),
        )
    except SourceDataTooOldError as exc:
        work.complete(name="prediction failed — source data too old")
        return StageStats(
            errors=1,
            details={
                "reason": "source_data_too_old",
                "error": str(exc),
                "origin_time": origin_time.isoformat(),
                "forecast_created_at": created_at.isoformat(),
            },
        )

    try:
        for target, model in models.items():
            _assert_artifact_horizon_support(
                model=model,
                artifact=artifacts[target],
                time_contract=time_contract,
            )
    except RuntimeError as exc:
        work.complete(name="prediction failed — model horizon coverage")
        return StageStats(
            errors=1,
            details={
                "reason": "model_horizon_coverage_incomplete",
                "error": str(exc),
                "time_contract": time_contract.as_dict(),
            },
        )

    with work.task(
        "forecast weather for 48 future serving hours",
        1.0,
        task_key="prediction:weather",
        fallback_seconds=300.0,
        detail={
            "phase": "weather_prediction",
            "origin_time": origin_time.isoformat(),
            "serving_horizons": time_contract.serving_horizon_hours,
            "model_horizon_min": min(time_contract.model_horizons),
            "model_horizon_max": max(time_contract.model_horizons),
        },
    ):
        weather_frame, _ = _weather_predictions(
            session,
            config,
            origin_time=origin_time,
            registry=registry,
            models=models,
            artifacts=artifacts,
            time_contract=time_contract,
        )

    if not weather_frame.empty and "temperature_c" in models:
        with work.task(
            "persist hourly temperature forecasts",
            0.80,
            task_key="prediction:persist:temperature_c",
            fallback_seconds=120.0,
            detail={"target": "temperature_c", "rows": len(weather_frame)},
        ):
            temperature_values, temperature_extras = _predict_bundle(
                config=config,
                registry=registry,
                model=models["temperature_c"],
                artifact=artifacts["temperature_c"],
                frame=build_weather_prediction_rows(
                    session,
                    origin_time=origin_time,
                    time_contract=time_contract,
                    max_days=max(
                        7,
                        min(config.hourly_forecasting.maximum_training_days, 60),
                    ),
                ),
            )
            _persist_predictions(
                session,
                model=models["temperature_c"],
                artifact=artifacts["temperature_c"],
                config=config,
                frame=weather_frame,
                parameter="temperature_c",
                values=temperature_values,
                extras=temperature_extras,
                created_at=created_at,
                stats=stats,
            )
    else:
        work.advance(
            "temperature forecasts skipped",
            0.80,
            detail={"target": "temperature_c", "reason": "missing_model_or_frame"},
            status="skipped",
        )

    if not weather_frame.empty and "precipitation_mm" in models:
        with work.task(
            "persist hourly precipitation forecasts",
            0.80,
            task_key="prediction:persist:precipitation_mm",
            fallback_seconds=180.0,
            detail={"target": "precipitation_mm", "rows": len(weather_frame)},
        ):
            expected = weather_frame["predicted_precipitation_mm"].to_numpy(dtype=float)
            probability = weather_frame[
                "predicted_precipitation_probability"
            ].to_numpy(dtype=float)
            conditional = weather_frame[
                "predicted_precipitation_amount_if_rain_mm"
            ].to_numpy(dtype=float)
            _persist_predictions(
                session,
                model=models["precipitation_mm"],
                artifact=artifacts["precipitation_mm"],
                config=config,
                frame=weather_frame,
                parameter="precipitation_mm",
                values=expected,
                extras={
                    "precipitation_probability": probability,
                    "precipitation_amount_if_rain_mm": conditional,
                },
                created_at=created_at,
                stats=stats,
            )
            _persist_predictions(
                session,
                model=models["precipitation_mm"],
                artifact=artifacts["precipitation_mm"],
                config=config,
                frame=weather_frame,
                parameter="precipitation_probability",
                values=probability,
                extras={},
                created_at=created_at,
                stats=stats,
            )
    else:
        work.advance(
            "precipitation forecasts skipped",
            0.80,
            detail={"target": "precipitation_mm", "reason": "missing_model_or_frame"},
            status="skipped",
        )

    for parameter in configured_air_targets(config):
        if parameter not in models:
            work.advance(
                f"{parameter} forecasts skipped",
                0.80,
                detail={"target": parameter, "reason": "missing_model"},
                status="skipped",
            )
            continue
        with work.task(
            f"predict and persist {parameter} for serving leads 1–48",
            0.80,
            task_key=f"prediction:persist:{parameter}",
            fallback_seconds=240.0,
            detail={
                "target": parameter,
                "phase": "air_prediction",
                "serving_horizons": time_contract.serving_horizon_hours,
                "model_horizon_min": min(time_contract.model_horizons),
                "model_horizon_max": max(time_contract.model_horizons),
            },
        ):
            frame = _pm_prediction_frame(
                session,
                config,
                parameter=parameter,
                origin_time=origin_time,
                weather_predictions=weather_frame,
                time_contract=time_contract,
            )
            if frame.empty:
                stats.skipped += 1
                continue
            values, extras = _predict_bundle(
                config=config,
                registry=registry,
                model=models[parameter],
                artifact=artifacts[parameter],
                frame=frame,
            )
            _persist_predictions(
                session,
                model=models[parameter],
                artifact=artifacts[parameter],
                config=config,
                frame=frame,
                parameter=parameter,
                values=values,
                extras=extras,
                created_at=created_at,
                stats=stats,
            )

    if stats.inserted:
        set_application_state(
            session,
            "last_hourly_forecast_at",
            {
                "created_at": created_at.isoformat(),
                "origin_time": origin_time.isoformat(),
                "serving_horizons_hours": list(time_contract.serving_leads),
                "model_horizons_hours": list(time_contract.model_horizons),
                "serving_anchor_time": time_contract.serving_anchor_time.isoformat(),
                "source_age_hours": time_contract.source_age_hours,
                "targets": config.hourly_forecasting.targets,
            },
        )
        set_application_state(session, "last_forecast_at", created_at.isoformat())

    work.complete(name="hourly forecasts completed")
    stats.details = {
        "forecast_mode": "horizon-conditioned-hourly",
        "origin_time": origin_time.isoformat(),
        "serving_horizons_hours": list(time_contract.serving_leads),
        "model_horizons_hours": list(time_contract.model_horizons),
        "serving_anchor_time": time_contract.serving_anchor_time.isoformat(),
        "source_age_hours": time_contract.source_age_hours,
        "time_contract": time_contract.as_dict(),
        "active_models": {
            target: model.semantic_version for target, model in models.items()
        },
        "missing_targets": missing,
        "exact_target_time": True,
        "progress_file": str(progress.current_path) if progress is not None else None,
    }
    if missing:
        stats.warnings += len(missing)
    return stats
