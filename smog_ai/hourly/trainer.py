from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from smog_ai.air_parameters import (
    configured_air_targets,
    create_air_parameter_registry,
    is_air_target,
)
from smog_ai.artifacts.datasets import (
    create_artifact_repository,
    load_hourly_training_frame_from_store,
)
from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion, TrainingRun
from smog_ai.domain import StageStats
from smog_ai.hourly.training_policy import (
    TrainingBudget,
    create_training_set_policy,
    resolve_training_profile,
)
from smog_ai.hourly.features import (
    KEY_COLUMNS,
    PM_HOURLY_FEATURE_COLUMNS,
    WEATHER_HOURLY_FEATURE_COLUMNS,
    auxiliary_air_feature_columns,
    build_hourly_pm_training_frame,
    build_hourly_weather_training_frame,
)
from smog_ai.modeling import ModelFitContext, ModelPredictContext, create_model_registry
from smog_ai.mlops.comparison import export_model_comparison
from smog_ai.mlops.mlflow_bridge import create_mlflow_bridge
from smog_ai.progress import ProgressReporter, WeightedStageProgress
from smog_ai.storage.base import ObjectNotFoundError
from smog_ai.time_utils import utc_now
from smog_ai.training.metrics import regression_metrics

logger = logging.getLogger(__name__)

HOURLY_MODEL_HORIZON_SENTINEL = 0


_PROVIDER_WORK_WEIGHTS: dict[str, float] = {
    "persistence": 0.10,
    "historical_mean": 0.10,
    "ridge": 0.75,
    "polynomial_ridge": 1.50,
    "hist_gradient_boosting": 3.00,
    "hist_gradient_boosting_quantile": 3.00,
    "mlp": 5.00,
    "hurdle_hist_gradient_boosting": 5.00,
}

_PROVIDER_BASE_SECONDS_250K: dict[str, float] = {
    "persistence": 2.0,
    "historical_mean": 3.0,
    "ridge": 60.0,
    "polynomial_ridge": 180.0,
    "hist_gradient_boosting": 360.0,
    "hist_gradient_boosting_quantile": 360.0,
    "mlp": 900.0,
    "hurdle_hist_gradient_boosting": 720.0,
}


def _provider_work_weight(provider_name: str) -> float:
    return float(_PROVIDER_WORK_WEIGHTS.get(provider_name, 2.0))


def _provider_eta_hint(provider_name: str, rows: int) -> float:
    base = float(_PROVIDER_BASE_SECONDS_250K.get(provider_name, 300.0))
    scale = max(0.20, min(6.0, max(1, int(rows)) / 250_000.0))
    return base * scale


def _target_training_budget(
    *,
    algorithms: tuple[str, ...],
    fit_quantiles: bool,
    quantile_method: str,
    quantiles: list[float],
    target: str,
) -> dict[str, float]:
    candidate = sum(_provider_work_weight(name) for name in algorithms)
    final = max((_provider_work_weight(name) for name in algorithms), default=1.0)
    quantile_budget = (
        0.0
        if target == "precipitation_mm" or not fit_quantiles
        else len(quantiles) * _provider_work_weight(quantile_method)
    )
    register = 0.50
    return {
        "candidate": candidate,
        "final": final,
        "quantiles": quantile_budget,
        "register": register,
        "total": candidate + final + quantile_budget + register,
    }


@dataclass(slots=True)
class CandidateResult:
    provider_name: str
    artifact: dict[str, Any]
    metrics: dict[str, Any]
    score: float


def _semantic_version(provider: str) -> str:
    return f"{utc_now():%Y.%m.%d.%H%M%S}-hourly-{provider}-{uuid.uuid4().hex[:6]}"


def _save_artifact(path: Path, artifact: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(artifact, temporary)
    temporary.replace(path)


def _save_recovery_manifest(path: Path, payload: dict[str, Any]) -> Path:
    manifest_path = path.with_suffix(".recovery.json")
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest_path


def create_hourly_model_registry(config: AppConfig):  # type: ignore[no-untyped-def]
    registry = create_model_registry(
        plugin_modules=config.model_platform.plugin_modules,
        entry_point_group=config.model_platform.entry_point_group,
        load_entry_points=config.model_platform.discover_entry_points,
    )
    for row in config.model_platform.external_factories:
        if not row.enabled:
            continue
        registry.load_import_string(
            row.import_string,
            alias=row.name,
            replace=True,
        )
    return registry


def _feature_columns(config: AppConfig, target: str) -> tuple[str, ...]:
    if is_air_target(config, target):
        registry = create_air_parameter_registry(config)
        auxiliary = [
            code for code in registry.auxiliary_codes if code != target
        ]
        return tuple(
            [
                *PM_HOURLY_FEATURE_COLUMNS,
                *auxiliary_air_feature_columns(auxiliary),
            ]
        )
    return tuple(WEATHER_HOURLY_FEATURE_COLUMNS)


def _baseline_column(config: AppConfig, target: str) -> str:
    if is_air_target(config, target):
        return "value"
    return target


def _exceedance_threshold(config: AppConfig, target: str) -> float | None:
    if not is_air_target(config, target):
        return None
    return create_air_parameter_registry(config).require(target).exceedance_threshold


def _clip_predictions(
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


def _regression_by_horizon(
    config: AppConfig,
    target: str,
    frame: pd.DataFrame,
    predictions: np.ndarray,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for horizon, indexes in frame.groupby("horizon_hours", sort=True).groups.items():
        index_array = np.asarray(list(indexes), dtype=int)
        actual = frame.loc[index_array, "target"].to_numpy(dtype=float)
        predicted = predictions[index_array]
        persistence = None
        baseline = _baseline_column(config, target)
        if baseline in frame.columns:
            persistence = frame.loc[index_array, baseline].to_numpy(dtype=float)
        output[str(int(horizon))] = regression_metrics(
            actual,
            predicted,
            persistence=persistence,
            exceedance_threshold=_exceedance_threshold(config, target),
        )
    return output


def _precipitation_metrics(
    actual: np.ndarray,
    expected: np.ndarray,
    probability: np.ndarray,
    *,
    threshold: float,
    persistence_expected: np.ndarray | None = None,
) -> dict[str, Any]:
    from sklearn.metrics import (
        brier_score_loss,
        mean_absolute_error,
        mean_squared_error,
        roc_auc_score,
    )

    actual = np.asarray(actual, dtype=float)
    expected = np.asarray(expected, dtype=float)
    probability = np.asarray(probability, dtype=float)
    persistence = (
        np.asarray(persistence_expected, dtype=float)
        if persistence_expected is not None
        else np.full_like(actual, np.nan, dtype=float)
    )
    mask = np.isfinite(actual) & np.isfinite(expected) & np.isfinite(probability)
    actual = actual[mask]
    expected = expected[mask]
    probability = np.clip(probability[mask], 0.0, 1.0)
    persistence = persistence[mask]
    if actual.size == 0:
        return {
            "count": 0,
            "mae": None,
            "rmse": None,
            "brier": None,
            "roc_auc": None,
            "quality_status": "failed",
            "quality_failures": ["no_validation_rows"],
        }

    occurrence = actual > threshold
    climatology_probability = float(np.mean(occurrence))
    climatology = np.full(actual.shape, climatology_probability, dtype=float)
    zero_expected = np.zeros_like(actual, dtype=float)

    roc_auc = None
    if np.unique(occurrence).size == 2:
        roc_auc = float(roc_auc_score(occurrence.astype(int), probability))

    brier = float(brier_score_loss(occurrence.astype(int), probability))
    brier_climatology = float(
        brier_score_loss(occurrence.astype(int), climatology)
    )
    brier_skill_climatology = (
        1.0 - brier / brier_climatology
        if brier_climatology > 0
        else None
    )

    persistence_mask = np.isfinite(persistence)
    persistence_mae = None
    persistence_brier = None
    brier_skill_persistence = None
    improvement_vs_persistence = None
    if persistence_mask.any():
        persistence_values = np.clip(persistence[persistence_mask], 0.0, None)
        persistence_actual = actual[persistence_mask]
        persistence_occurrence = persistence_values > threshold
        persistence_mae = float(
            mean_absolute_error(persistence_actual, persistence_values)
        )
        persistence_brier = float(
            brier_score_loss(
                (persistence_actual > threshold).astype(int),
                persistence_occurrence.astype(float),
            )
        )
        model_mae_on_persistence_rows = float(
            mean_absolute_error(
                persistence_actual,
                expected[persistence_mask],
            )
        )
        if persistence_mae > 0:
            improvement_vs_persistence = (
                persistence_mae - model_mae_on_persistence_rows
            ) / persistence_mae
        if persistence_brier > 0:
            model_brier_on_persistence_rows = float(
                brier_score_loss(
                    (persistence_actual > threshold).astype(int),
                    probability[persistence_mask],
                )
            )
            brier_skill_persistence = (
                1.0 - model_brier_on_persistence_rows / persistence_brier
            )

    wet_mask = occurrence
    dry_mask = ~occurrence
    wet_mae = (
        float(mean_absolute_error(actual[wet_mask], expected[wet_mask]))
        if wet_mask.any()
        else None
    )
    wet_bias = (
        float(np.mean(expected[wet_mask] - actual[wet_mask]))
        if wet_mask.any()
        else None
    )
    dry_false_amount_mean = (
        float(np.mean(expected[dry_mask])) if dry_mask.any() else None
    )

    return {
        "count": int(actual.size),
        "mae": float(mean_absolute_error(actual, expected)),
        "rmse": float(mean_squared_error(actual, expected) ** 0.5),
        "bias": float(np.mean(expected - actual)),
        "zero_baseline_mae": float(mean_absolute_error(actual, zero_expected)),
        "persistence_mae": persistence_mae,
        "improvement_vs_persistence": improvement_vs_persistence,
        "wet_mae": wet_mae,
        "wet_bias": wet_bias,
        "dry_false_amount_mean": dry_false_amount_mean,
        "brier": brier,
        "brier_climatology": brier_climatology,
        "brier_persistence": persistence_brier,
        "brier_skill_vs_climatology": brier_skill_climatology,
        "brier_skill_vs_persistence": brier_skill_persistence,
        "roc_auc": roc_auc,
        "rain_frequency_actual": climatology_probability,
        "rain_probability_mean": float(np.mean(probability)),
    }


def _precipitation_quality_gate(
    config: AppConfig,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    settings = config.hourly_forecasting.precipitation
    failures: list[dict[str, Any]] = []

    def require_minimum(name: str, minimum: float) -> None:
        value = metrics.get(name)
        if value is None or not np.isfinite(float(value)) or float(value) < minimum:
            failures.append(
                {
                    "metric": name,
                    "actual": value,
                    "minimum": minimum,
                }
            )

    require_minimum(
        "improvement_vs_persistence",
        settings.minimum_mae_improvement_vs_persistence,
    )
    require_minimum(
        "brier_skill_vs_climatology",
        settings.minimum_brier_skill_vs_climatology,
    )
    require_minimum(
        "brier_skill_vs_persistence",
        settings.minimum_brier_skill_vs_persistence,
    )
    require_minimum("roc_auc", settings.minimum_roc_auc)

    bias = metrics.get("bias")
    if (
        bias is None
        or not np.isfinite(float(bias))
        or abs(float(bias)) > settings.maximum_absolute_bias_mm
    ):
        failures.append(
            {
                "metric": "absolute_bias_mm",
                "actual": abs(float(bias)) if bias is not None else None,
                "maximum": settings.maximum_absolute_bias_mm,
            }
        )

    return {
        "passed": not failures,
        "status": "accepted" if not failures else "experimental",
        "failures": failures,
        "thresholds": {
            "minimum_mae_improvement_vs_persistence": (
                settings.minimum_mae_improvement_vs_persistence
            ),
            "minimum_brier_skill_vs_climatology": (
                settings.minimum_brier_skill_vs_climatology
            ),
            "minimum_brier_skill_vs_persistence": (
                settings.minimum_brier_skill_vs_persistence
            ),
            "minimum_roc_auc": settings.minimum_roc_auc,
            "maximum_absolute_bias_mm": settings.maximum_absolute_bias_mm,
        },
    }


def _split_chronologically(frame: pd.DataFrame, fraction: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    ordered = frame.sort_values(["measurement_time", "air_station_id", "horizon_hours"]).reset_index(drop=True)
    unique_times = pd.Series(pd.to_datetime(ordered["measurement_time"], utc=True).unique()).sort_values()
    if len(unique_times) < 2:
        split = max(1, len(ordered) - 1)
        return ordered.iloc[:split].copy(), ordered.iloc[split:].copy()
    split_time_index = max(1, min(len(unique_times) - 1, int(len(unique_times) * (1 - fraction))))
    boundary = unique_times.iloc[split_time_index]
    train = ordered[pd.to_datetime(ordered["measurement_time"], utc=True) < boundary].copy()
    valid = ordered[pd.to_datetime(ordered["measurement_time"], utc=True) >= boundary].copy()
    if train.empty or valid.empty:
        split = max(1, min(len(ordered) - 1, int(len(ordered) * (1 - fraction))))
        train, valid = ordered.iloc[:split].copy(), ordered.iloc[split:].copy()
    return train.reset_index(drop=True), valid.reset_index(drop=True)


def _load_frame(
    session: Session,
    config: AppConfig,
    target: str,
    *,
    profile,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    settings = config.hourly_forecasting
    if config.training.input_source == "object_store":
        try:
            frame, manifest = load_hourly_training_frame_from_store(
                config, target=target
            )
            return frame, {"source": "object_store", "manifest": manifest}
        except ObjectNotFoundError as exc:
            return pd.DataFrame(), {
                "source": "object_store",
                "status": "missing",
                "error": str(exc),
            }
        except Exception as exc:
            if not config.training.allow_database_fallback:
                raise
            logger.warning(
                "Hourly object-store dataset unavailable for %s: %s",
                target,
                exc,
            )

    common = {
        "horizons": settings.horizons_hours,
        "max_days": profile.maximum_training_days(target),
        "maximum_output_rows": profile.maximum_rows_per_target,
        "horizon_bucket_edges": profile.horizon_bucket_edges,
        "samples_per_horizon_bucket": profile.samples_per_horizon_bucket,
        "random_state": settings.random_state,
    }
    if is_air_target(config, target):
        definition = create_air_parameter_registry(config).require(target)
        frame = build_hourly_pm_training_frame(
            session,
            parameter=target,
            allow_negative_target=definition.allow_negative,
            **common,
        )
    else:
        frame = build_hourly_weather_training_frame(
            session,
            target=target,
            precipitation_accumulation_period_hours=(
                settings.precipitation.accumulation_period_hours
            ),
            precipitation_occurrence_threshold_mm=(
                settings.precipitation.occurrence_threshold_mm
            ),
            **common,
        )
    return frame, {
        "source": "database",
        "profile": profile.name,
        "maximum_training_days": profile.maximum_training_days(target),
        "maximum_rows_per_target": profile.maximum_rows_per_target,
        "horizons_per_origin": profile.horizons_per_origin,
    }


def _fit_candidate(
    *,
    registry,
    provider_name: str,
    target: str,
    train: pd.DataFrame,
    valid: pd.DataFrame,
    feature_columns: tuple[str, ...],
    config: AppConfig,
) -> CandidateResult:
    provider = registry.get(provider_name)
    task = "hurdle_regression" if target == "precipitation_mm" else "regression"
    fit_context = ModelFitContext(
        target_name=target,
        feature_columns=feature_columns,
        task=task,
        random_state=config.hourly_forecasting.random_state,
        baseline_column=_baseline_column(config, target),
        sample_weight=(
            pd.to_numeric(train["__sample_weight"], errors="coerce")
            .fillna(1.0)
            .to_numpy(dtype=float)
            if "__sample_weight" in train.columns
            else None
        ),
        metadata={
            "occurrence_threshold_mm": config.hourly_forecasting.precipitation.occurrence_threshold_mm,
            "accumulation_period_hours": config.hourly_forecasting.precipitation.accumulation_period_hours,
            "method_parameters": config.model_platform.method_parameters.get(provider_name, {}),
        },
    )
    provider_artifact = provider.fit(
        train.reindex(columns=feature_columns),
        train["target"],
        context=fit_context,
    )
    prediction = provider.predict(
        provider_artifact,
        valid.reindex(columns=feature_columns),
        context=ModelPredictContext(
            target_name=target,
            feature_columns=feature_columns,
            task=task,
            metadata=fit_context.metadata,
        ),
    )
    values = _clip_predictions(config, target, prediction.values)
    if target == "precipitation_mm":
        probability = prediction.extras.get(
            "precipitation_probability", np.zeros(len(values), dtype=float)
        )
        baseline_column = _baseline_column(config, target)
        persistence_expected = (
            valid[baseline_column].to_numpy(dtype=float)
            if baseline_column in valid.columns
            else None
        )
        metrics = _precipitation_metrics(
            valid["target"].to_numpy(dtype=float),
            values,
            probability,
            threshold=(
                config.hourly_forecasting.precipitation.occurrence_threshold_mm
            ),
            persistence_expected=persistence_expected,
        )
        metrics["precipitation_quality_gate"] = _precipitation_quality_gate(
            config, metrics
        )
        metrics["quality_status"] = metrics["precipitation_quality_gate"][
            "status"
        ]
    else:
        persistence = None
        baseline = _baseline_column(config, target)
        if baseline in valid.columns:
            persistence = valid[baseline].to_numpy(dtype=float)
        metrics = regression_metrics(
            valid["target"].to_numpy(dtype=float),
            values,
            persistence=persistence,
            exceedance_threshold=_exceedance_threshold(config, target),
        )
        metrics["by_horizon"] = _regression_by_horizon(config, target, valid, values)
    metrics["provider_description"] = provider.describe(provider_artifact)
    artifact = {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "provider": provider_name,
        "task": task,
        "feature_columns": list(feature_columns),
        "horizons_hours": config.hourly_forecasting.horizons_hours,
        "provider_artifact": provider_artifact,
        "metadata": fit_context.metadata,
    }
    score = float(metrics.get("mae") if metrics.get("mae") is not None else np.inf)
    return CandidateResult(provider_name, artifact, metrics, score)


def _fit_final(
    *,
    registry,
    candidate: CandidateResult,
    target: str,
    frame: pd.DataFrame,
    feature_columns: tuple[str, ...],
    config: AppConfig,
    work: WeightedStageProgress | None = None,
    final_weight: float = 1.0,
    quantile_weight: float = 1.0,
    fit_quantiles: bool = True,
) -> dict[str, Any]:
    provider = registry.get(candidate.provider_name)
    task = "hurdle_regression" if target == "precipitation_mm" else "regression"
    context = ModelFitContext(
        target_name=target,
        feature_columns=feature_columns,
        task=task,
        random_state=config.hourly_forecasting.random_state,
        baseline_column=_baseline_column(config, target),
        sample_weight=(
            pd.to_numeric(frame["__sample_weight"], errors="coerce")
            .fillna(1.0)
            .to_numpy(dtype=float)
            if "__sample_weight" in frame.columns
            else None
        ),
        metadata={
            "occurrence_threshold_mm": config.hourly_forecasting.precipitation.occurrence_threshold_mm,
            "accumulation_period_hours": config.hourly_forecasting.precipitation.accumulation_period_hours,
            "method_parameters": config.model_platform.method_parameters.get(
                candidate.provider_name, {}
            ),
        },
    )
    final_context = (
        work.task(
            f"{target}: final fit ({candidate.provider_name})",
            final_weight,
            task_key=f"training:{target}:final:{candidate.provider_name}",
            fallback_seconds=_provider_eta_hint(candidate.provider_name, len(frame)),
            detail={
                "target": target,
                "provider": candidate.provider_name,
                "phase": "final_fit",
                "rows": len(frame),
            },
        )
        if work is not None
        else nullcontext()
    )
    with final_context:
        provider_artifact = provider.fit(
            frame.reindex(columns=feature_columns),
            frame["target"],
            context=context,
        )
    quantile_artifacts: dict[str, Any] = {}
    if target != "precipitation_mm" and fit_quantiles:
        quantile_provider_name = config.hourly_forecasting.quantile_method
        try:
            quantile_provider = registry.get(quantile_provider_name)
            for quantile in config.hourly_forecasting.quantiles:
                quantile_context = ModelFitContext(
                    target_name=target,
                    feature_columns=feature_columns,
                    task="regression",
                    random_state=config.hourly_forecasting.random_state,
                    baseline_column=_baseline_column(config, target),
                    sample_weight=(
                        pd.to_numeric(frame["__sample_weight"], errors="coerce")
                        .fillna(1.0)
                        .to_numpy(dtype=float)
                        if "__sample_weight" in frame.columns
                        else None
                    ),
                    metadata={
                        "quantile": float(quantile),
                        "method_parameters": config.model_platform.method_parameters.get(
                            quantile_provider_name, {}
                        ),
                    },
                )
                quantile_label = f"q{int(round(quantile * 100)):02d}"
                quantile_task = (
                    work.task(
                        f"{target}: quantile {quantile_label}",
                        quantile_weight,
                        task_key=(
                            f"training:{target}:quantile:{quantile_provider_name}:"
                            f"{quantile_label}"
                        ),
                        fallback_seconds=_provider_eta_hint(
                            quantile_provider_name, len(frame)
                        ),
                        detail={
                            "target": target,
                            "provider": quantile_provider_name,
                            "phase": "quantile_fit",
                            "quantile": float(quantile),
                            "rows": len(frame),
                        },
                    )
                    if work is not None
                    else nullcontext()
                )
                with quantile_task:
                    quantile_artifacts[quantile_label] = {
                        "provider": quantile_provider_name,
                        "artifact": quantile_provider.fit(
                            frame.reindex(columns=feature_columns),
                            frame["target"],
                            context=quantile_context,
                        ),
                        "quantile": float(quantile),
                    }
        except Exception as exc:
            logger.warning(
                "Hourly quantile models unavailable target=%s provider=%s: %s",
                target,
                quantile_provider_name,
                exc,
            )
    return {
        **candidate.artifact,
        "provider_artifact": provider_artifact,
        "quantile_artifacts": quantile_artifacts,
        "trained_rows": len(frame),
        "trained_at": utc_now().isoformat(),
    }


def _upload_model(
    config: AppConfig,
    *,
    target: str,
    version: str,
    provider_name: str,
    artifact: dict[str, Any],
    metrics: dict[str, Any],
    data_start: datetime | None,
    data_end: datetime | None,
) -> dict[str, Any]:
    if not config.object_storage.enabled or not config.artifacts.upload_models:
        return {}
    repository = create_artifact_repository(config)
    stored = repository.put_joblib(
        repository.layout.hourly_model_binary(target, version),
        artifact,
        immutable=True,
        metadata={
            "target": target,
            "model-version": version,
            "provider": provider_name,
            "forecast-mode": "horizon-conditioned-hourly",
        },
    )
    card = {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "model_version": version,
        "target": target,
        "provider": provider_name,
        "feature_columns": artifact.get("feature_columns"),
        "horizons_hours": config.hourly_forecasting.horizons_hours,
        "time_contract": {
            "serving_horizon_hours": (
                config.hourly_forecasting.serving_horizon_count
            ),
            "maximum_source_delay_hours": (
                config.hourly_forecasting.maximum_source_delay_hours
            ),
            "maximum_model_horizon_hours": (
                config.hourly_forecasting.model_horizon_maximum
            ),
            "model_horizons_hours": list(
                config.hourly_forecasting.model_horizons_hours
            ),
        },
        "training_profile": metrics.get("training_profile"),
        "training_set_policy": metrics.get("training_set_policy"),
        "training_budget": metrics.get("budget"),
        "target_contract": (
            {
                "unit": "mm",
                "accumulation_period_hours": (
                    config.hourly_forecasting.precipitation.accumulation_period_hours
                ),
                "ending_at_target_time": True,
                "disaggregated_to_hourly": False,
            }
            if target == "precipitation_mm"
            else None
        ),
        "metrics": metrics,
        "training_data_start": data_start.isoformat() if data_start else None,
        "training_data_end": data_end.isoformat() if data_end else None,
        "created_at": utc_now().isoformat(),
        "source_host_id": config.source_host_id,
        "artifact": {
            "object_key": stored.key,
            "checksum": stored.checksum,
            "size": stored.size,
            "storage_backend": repository.store.backend_name,
        },
    }
    card_key = repository.layout.hourly_model_card(target, version)
    repository.put_json(card_key, card, immutable=True)
    metrics_key = repository.layout.hourly_model_metrics(target, version)
    repository.put_json(metrics_key, {**card, "artifact": card["artifact"]}, immutable=True)
    return {
        "artifact_object_key": stored.key,
        "artifact_checksum": stored.checksum,
        "model_card_object_key": card_key,
        "metrics_object_key": metrics_key,
        "storage_backend": repository.store.backend_name,
    }


def _register_model(
    session: Session,
    config: AppConfig,
    *,
    target: str,
    provider_name: str,
    artifact: dict[str, Any],
    metrics: dict[str, Any],
    data_start: datetime | None,
    data_end: datetime | None,
) -> ModelVersion:
    version = _semantic_version(provider_name)
    path = config.paths.models_dir / "hourly" / target.replace(".", "_") / f"{version}.joblib"
    _save_artifact(path, artifact)
    metrics_payload = dict(metrics)
    recovery_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "provider": provider_name,
        "model_version": version,
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_columns": list(artifact.get("feature_columns") or []),
        "horizons_hours": list(artifact.get("horizons_hours") or []),
        "metrics": metrics,
        "training_data_start": data_start.isoformat() if data_start else None,
        "training_data_end": data_end.isoformat() if data_end else None,
        "created_at": utc_now().isoformat(),
        "remote_artifact": None,
        "remote_artifact_error": None,
    }
    recovery_manifest_path = _save_recovery_manifest(path, recovery_payload)
    try:
        metrics_payload["remote_artifact"] = _upload_model(
            config,
            target=target,
            version=version,
            provider_name=provider_name,
            artifact=artifact,
            metrics=metrics,
            data_start=data_start,
            data_end=data_end,
        )
        recovery_payload["remote_artifact"] = metrics_payload["remote_artifact"]
        _save_recovery_manifest(path, recovery_payload)
    except Exception as exc:
        logger.exception("Hourly model upload failed for %s/%s", target, provider_name)
        metrics_payload["remote_artifact_error"] = str(exc)
        recovery_payload["remote_artifact_error"] = str(exc)
        _save_recovery_manifest(path, recovery_payload)
        if config.object_storage.enabled and config.artifacts.upload_models:
            raise RuntimeError(
                "Hourly model was fitted and saved locally, but required object-storage "
                f"publication failed target={target} provider={provider_name}; "
                f"recovery manifest={recovery_manifest_path}"
            ) from exc
    row = ModelVersion(
        model_name=f"hourly-{target}-{provider_name}",
        algorithm=provider_name,
        parameter=target,
        forecast_horizon=HOURLY_MODEL_HORIZON_SENTINEL,
        semantic_version=version,
        artifact_path=str(path),
        feature_columns_json=list(artifact.get("feature_columns") or []),
        metrics_json=metrics_payload,
        training_data_start=data_start,
        training_data_end=data_end,
        active=False,
    )
    session.add(row)
    session.flush()
    return row


def _activate(session: Session, config: AppConfig, model: ModelVersion) -> None:
    session.execute(
        update(ModelVersion)
        .where(
            ModelVersion.parameter == model.parameter,
            ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
        )
        .values(active=False)
    )
    model.active = True
    model.activated_at = utc_now()
    remote = (model.metrics_json or {}).get("remote_artifact") or {}
    if config.object_storage.enabled and remote.get("artifact_object_key"):
        repository = create_artifact_repository(config)
        repository.put_json(
            repository.layout.active_hourly_model_pointer(model.parameter),
            {
                "schema_version": "2.0",
                "forecast_mode": "horizon-conditioned-hourly",
                "target": model.parameter,
                "model_version": model.semantic_version,
                "provider": model.algorithm,
                "artifact_object_key": remote["artifact_object_key"],
                "artifact_checksum": remote.get("artifact_checksum"),
                "model_card_object_key": remote.get("model_card_object_key"),
                "metrics_object_key": remote.get("metrics_object_key"),
                "activated_at": model.activated_at.isoformat(),
                "source_host_id": config.source_host_id,
                "quality_status": (model.metrics_json or {}).get("quality_status"),
                "time_contract": {
                    "serving_horizon_hours": (
                        config.hourly_forecasting.serving_horizon_count
                    ),
                    "maximum_source_delay_hours": (
                        config.hourly_forecasting.maximum_source_delay_hours
                    ),
                    "maximum_model_horizon_hours": (
                        config.hourly_forecasting.model_horizon_maximum
                    ),
                },
            },
        )
    try:
        export_model_comparison(session, config)
    except Exception:
        if config.mlflow.strict:
            raise
        logger.warning("Model-comparison refresh failed", exc_info=True)


def _bootstrap_artifact(target: str, config: AppConfig, registry) -> tuple[str, dict[str, Any]]:  # type: ignore[no-untyped-def]
    if target == "precipitation_mm":
        provider_name = config.hourly_forecasting.precipitation.provider
        provider = registry.get(provider_name)
        feature_columns = _feature_columns(config, target)
        # A deterministic no-rain bootstrap. It is explicitly marked and replaced
        # once sufficient weather history is available.
        artifact = {
            "schema_version": "2.0",
            "forecast_mode": "horizon-conditioned-hourly",
            "target": target,
            "provider": provider_name,
            "task": "hurdle_regression",
            "feature_columns": list(feature_columns),
            "horizons_hours": config.hourly_forecasting.horizons_hours,
            "provider_artifact": {
                "provider": provider_name,
                "task": "hurdle_regression",
                "feature_columns": list(feature_columns),
                "target_name": target,
                "occurrence_threshold_mm": config.hourly_forecasting.precipitation.occurrence_threshold_mm,
                "classifier": None,
                "amount_estimator": None,
                "probability_fallback": 0.0,
                "positive_mean": 0.0,
            },
            "metadata": {
                "bootstrap": True,
                "status": "insufficient_history",
                "occurrence_threshold_mm": config.hourly_forecasting.precipitation.occurrence_threshold_mm,
                "accumulation_period_hours": config.hourly_forecasting.precipitation.accumulation_period_hours,
            },
        }
        provider.describe(artifact["provider_artifact"])
        return provider_name, artifact
    provider_name = "persistence"
    feature_columns = _feature_columns(config, target)
    artifact = {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "provider": provider_name,
        "task": "regression",
        "feature_columns": list(feature_columns),
        "horizons_hours": config.hourly_forecasting.horizons_hours,
        "provider_artifact": {
            "provider": provider_name,
            "task": "regression",
            "feature_columns": list(feature_columns),
            "baseline_column": _baseline_column(config, target),
            "target_name": target,
        },
        "metadata": {"bootstrap": True, "status": "insufficient_history"},
    }
    return provider_name, artifact


def ensure_hourly_baseline_models(session: Session, config: AppConfig) -> int:
    if not config.hourly_forecasting.enabled:
        return 0
    registry = create_hourly_model_registry(config)
    created = 0
    for target in config.hourly_forecasting.targets:
        current = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == target,
                ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
                ModelVersion.active.is_(True),
            )
        )
        if current is not None:
            continue
        provider_name, artifact = _bootstrap_artifact(target, config, registry)
        row = _register_model(
            session,
            config,
            target=target,
            provider_name=provider_name,
            artifact=artifact,
            metrics={"bootstrap": True, "status": "insufficient_history"},
            data_start=None,
            data_end=None,
        )
        _activate(session, config, row)
        created += 1
    return created


def _weather_cross_fit(
    weather_frames: dict[str, pd.DataFrame],
    selected_providers: dict[str, str],
    config: AppConfig,
    registry,
    *,
    profile,
    budget: TrainingBudget | None = None,
    work: WeightedStageProgress | None = None,
    reserved_budget_by_target: dict[str, float] | None = None,
) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    temperature = weather_frames.get("temperature_c", pd.DataFrame())
    precipitation = weather_frames.get("precipitation_mm", pd.DataFrame())
    reserved = dict(reserved_budget_by_target or {})
    if temperature.empty and precipitation.empty:
        if work is not None:
            for target, budget in reserved.items():
                work.advance(
                    f"{target}: cross-fit skipped (no frame)",
                    budget,
                    detail={"target": target, "phase": "cross_fit", "reason": "empty"},
                    status="skipped",
                )
        return pd.DataFrame(columns=[*KEY_COLUMNS, "predicted_temperature_c", "predicted_precipitation_probability", "predicted_precipitation_mm"])

    basis = temperature if not temperature.empty else precipitation
    result = basis[KEY_COLUMNS].drop_duplicates().copy()
    result["predicted_temperature_c"] = np.nan
    result["predicted_precipitation_probability"] = np.nan
    result["predicted_precipitation_mm"] = np.nan

    for target, output_column in (
        ("temperature_c", "predicted_temperature_c"),
        ("precipitation_mm", "predicted_precipitation_mm"),
    ):
        target_budget = float(reserved.get(target, 0.0))
        raw_frame = weather_frames.get(target, pd.DataFrame())
        if raw_frame.empty or "measurement_time" not in raw_frame.columns:
            frame = pd.DataFrame()
        else:
            frame = raw_frame.sort_values("measurement_time").reset_index(drop=True)
        if frame.empty:
            if work is not None and target_budget:
                work.advance(
                    f"{target}: cross-fit skipped",
                    target_budget,
                    detail={"target": target, "phase": "cross_fit", "reason": "empty"},
                    status="skipped",
                )
            continue
        unique_times = pd.Series(pd.to_datetime(frame["measurement_time"], utc=True).unique()).sort_values().reset_index(drop=True)
        folds = min(profile.cross_fit_folds, max(2, len(unique_times) // 2))
        boundaries = np.linspace(0, len(unique_times), folds + 2, dtype=int)
        predictions: list[pd.DataFrame] = []
        provider_name = selected_providers.get(target) or (
            config.hourly_forecasting.precipitation.provider if target == "precipitation_mm" else "persistence"
        )
        provider = registry.get(provider_name)
        features = _feature_columns(config, target)
        fold_weight = target_budget / max(1, folds)
        for fold_index in range(1, len(boundaries) - 1):
            if budget is not None and not budget.should_continue(completed_candidates=1):
                remaining_folds = max(0, folds - fold_index + 1)
                if work is not None and remaining_folds:
                    work.advance(
                        f"{target}: remaining cross-fit folds skipped by time budget",
                        fold_weight * remaining_folds,
                        detail={
                            "target": target,
                            "phase": "cross_fit",
                            "reason": "max_wall_time_exceeded",
                            "budget": budget.snapshot(),
                        },
                        status="skipped",
                    )
                break
            train_end = boundaries[fold_index]
            valid_end = boundaries[fold_index + 1]
            if train_end <= 0 or valid_end <= train_end:
                if work is not None:
                    work.advance(
                        f"{target}: cross-fit fold {fold_index}/{folds} skipped",
                        fold_weight,
                        detail={"target": target, "phase": "cross_fit", "fold": fold_index, "folds": folds},
                        status="skipped",
                    )
                continue
            train_times = set(unique_times.iloc[:train_end])
            valid_times = set(unique_times.iloc[train_end:valid_end])
            train = frame[pd.to_datetime(frame["measurement_time"], utc=True).isin(train_times)]
            valid = frame[pd.to_datetime(frame["measurement_time"], utc=True).isin(valid_times)]
            if len(train) < 20 or valid.empty:
                if work is not None:
                    work.advance(
                        f"{target}: cross-fit fold {fold_index}/{folds} skipped",
                        fold_weight,
                        detail={
                            "target": target,
                            "phase": "cross_fit",
                            "fold": fold_index,
                            "folds": folds,
                            "rows_train": len(train),
                            "rows_validation": len(valid),
                        },
                        status="skipped",
                    )
                continue
            task = "hurdle_regression" if target == "precipitation_mm" else "regression"
            context = ModelFitContext(
                target_name=target,
                feature_columns=features,
                task=task,
                random_state=config.hourly_forecasting.random_state,
                baseline_column=_baseline_column(config, target),
                sample_weight=(
                    pd.to_numeric(train["__sample_weight"], errors="coerce")
                    .fillna(1.0)
                    .to_numpy(dtype=float)
                    if "__sample_weight" in train.columns
                    else None
                ),
                metadata={
                    "occurrence_threshold_mm": config.hourly_forecasting.precipitation.occurrence_threshold_mm,
                    "accumulation_period_hours": config.hourly_forecasting.precipitation.accumulation_period_hours,
                    "method_parameters": config.model_platform.method_parameters.get(provider_name, {}),
                },
            )
            fold_context = (
                work.task(
                    f"{target}: cross-fit fold {fold_index}/{folds} ({provider_name})",
                    fold_weight,
                    task_key=f"training:{target}:cross-fit:{provider_name}:fold",
                    fallback_seconds=_provider_eta_hint(provider_name, len(train)),
                    detail={
                        "target": target,
                        "provider": provider_name,
                        "phase": "cross_fit",
                        "fold": fold_index,
                        "folds": folds,
                        "rows_train": len(train),
                        "rows_validation": len(valid),
                    },
                )
                if work is not None
                else nullcontext()
            )
            with fold_context:
                fitted = provider.fit(train.reindex(columns=features), train["target"], context=context)
                bundle = provider.predict(
                    fitted,
                    valid.reindex(columns=features),
                    context=ModelPredictContext(target, features, task, context.metadata),
                )
            piece = valid[KEY_COLUMNS].copy()
            piece[output_column] = _clip_predictions(config, target, bundle.values)
            if target == "precipitation_mm":
                piece["predicted_precipitation_probability"] = bundle.extras.get(
                    "precipitation_probability", np.zeros(len(piece))
                )
            predictions.append(piece)
        if predictions:
            predicted = pd.concat(predictions, ignore_index=True).drop_duplicates(KEY_COLUMNS, keep="last")
            result = result.merge(predicted, on=KEY_COLUMNS, how="left", suffixes=("", "_new"))
            for column in [output_column, "predicted_precipitation_probability"]:
                new_column = f"{column}_new"
                if new_column in result.columns:
                    result[column] = result[new_column].combine_first(result[column])
                    result = result.drop(columns=[new_column])

    # Missing early-fold predictions intentionally remain NaN here.  The PM
    # training frame fills them only from weather available at the forecast
    # origin.  Using the actual weather target at t+h would leak the future.
    return result


def train_hourly_models(
    session: Session,
    config: AppConfig,
    progress: ProgressReporter | None = None,
    *,
    profile_name: str | None = None,
    training_session: Session | None = None,
    dataset_provenance: dict[str, Any] | None = None,
    commit_live_metadata: bool = False,
) -> StageStats:
    """Train exact-hour models under an explicit data/time budget.

    The raw archive remains complete.  A ``TrainingSetPolicy`` controls only the
    materialised sample used by this run.  ``quick`` is intended for weekly
    refreshes; ``full`` is intended for periodic champion/challenger searches.
    """

    if not config.hourly_forecasting.enabled:
        if progress is not None:
            progress.complete_stage(
                "training",
                task="hourly training disabled",
                detail={"reason": "hourly_forecasting_disabled"},
            )
        return StageStats(
            skipped=1,
            details={"reason": "hourly_forecasting_disabled"},
        )

    profile = resolve_training_profile(config, profile_name)
    policy = create_training_set_policy(config)
    read_session = training_session or session
    dataset_provenance_payload = (
        dict(dataset_provenance) if dataset_provenance is not None else None
    )
    budget = TrainingBudget(float(profile.max_wall_time_seconds))
    registry = create_hourly_model_registry(config)
    mlflow_bridge = create_mlflow_bridge(
        config.mlflow, strict=config.mlflow.strict
    )
    stats = StageStats()
    summaries: list[dict[str, Any]] = []
    frames: dict[str, pd.DataFrame] = {}
    provenance: dict[str, dict[str, Any]] = {}

    algorithms_by_target: dict[str, tuple[str, ...]] = {}
    for target in config.hourly_forecasting.targets:
        fallback = tuple(config.hourly_forecasting.target_algorithms[target])
        # Direct calls from tests/plugins preserve explicitly modified legacy
        # algorithm lists.  CLI/maintenance commands pass a profile name and
        # therefore use the profile's bounded candidate set.
        algorithms_by_target[target] = (
            profile.algorithms_for(target, fallback)
            if profile_name is not None
            else fallback
        )

    target_budgets = {
        target: _target_training_budget(
            algorithms=algorithms_by_target[target],
            fit_quantiles=profile.fit_quantiles,
            quantile_method=config.hourly_forecasting.quantile_method,
            quantiles=config.hourly_forecasting.quantiles,
            target=target,
        )
        for target in config.hourly_forecasting.targets
    }

    load_weight = 0.25
    baseline_weight = 0.50
    air_targets = configured_air_targets(config)
    pm_targets_present = bool(air_targets)
    crossfit_budget_by_target: dict[str, float] = {}
    if config.hourly_forecasting.use_predicted_weather_for_pm and pm_targets_present:
        for target in ("temperature_c", "precipitation_mm"):
            if target not in config.hourly_forecasting.targets:
                continue
            algorithms = algorithms_by_target[target]
            reserved_per_fold = max(
                (_provider_work_weight(name) for name in algorithms),
                default=1.0,
            )
            crossfit_budget_by_target[target] = (
                profile.cross_fit_folds * reserved_per_fold
            )

    total_work = (
        load_weight * len(config.hourly_forecasting.targets)
        + sum(row["total"] for row in target_budgets.values())
        + sum(crossfit_budget_by_target.values())
        + baseline_weight
    )
    work = WeightedStageProgress(
        progress,
        stage="training",
        total_weight=total_work,
    )

    for target in config.hourly_forecasting.targets:
        with work.task(
            f"{target}: load and bound training frame ({profile.name})",
            load_weight,
            task_key=f"training:{profile.name}:{target}:load-frame",
            fallback_seconds=45.0,
            detail={
                "target": target,
                "phase": "load_frame",
                "profile": profile.name,
                "policy": policy.name,
            },
        ):
            raw_frame, source = _load_frame(
                read_session,
                config,
                target,
                profile=profile,
            )
            selection = policy.select(
                raw_frame,
                target=target,
                phase="final",
                maximum_rows=profile.maximum_rows_per_target,
                profile=profile,
                random_state=config.hourly_forecasting.random_state,
            )
        frames[target] = selection.frame
        provenance[target] = {
            **source,
            "training_profile": profile.name,
            "training_set_policy": selection.metadata,
        }
        if dataset_provenance_payload is not None:
            provenance[target]["dataset_id"] = dataset_provenance_payload.get(
                "dataset_id"
            )
            provenance[target]["training_snapshot"] = dict(
                dataset_provenance_payload
            )

    selected_providers: dict[str, str] = {}
    order = [
        target
        for target in ("temperature_c", "precipitation_mm")
        if target in frames
    ]
    order.extend(target for target in air_targets if target in frames)
    weather_cross_fit: pd.DataFrame | None = None
    crossfit_consumed = False

    for target in order:
        target_budget = target_budgets[target]["total"]
        frame = frames[target].copy()

        if (
            is_air_target(config, target)
            and config.hourly_forecasting.use_predicted_weather_for_pm
        ):
            if weather_cross_fit is None:
                weather_cross_fit = _weather_cross_fit(
                    {
                        key: frames.get(key, pd.DataFrame())
                        for key in ("temperature_c", "precipitation_mm")
                    },
                    selected_providers,
                    config,
                    registry,
                    profile=profile,
                    budget=budget,
                    work=work,
                    reserved_budget_by_target=crossfit_budget_by_target,
                )
                crossfit_consumed = True

            if not weather_cross_fit.empty:
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
                ).merge(weather_cross_fit, on=KEY_COLUMNS, how="left")

            # No future observations are used as fallbacks.  For early folds
            # without an out-of-fold weather prediction, use weather known at
            # the forecast origin (or a neutral precipitation fallback).
            if "predicted_temperature_c" not in frame.columns:
                frame["predicted_temperature_c"] = np.nan
            if "temperature_c" in frame.columns:
                frame["predicted_temperature_c"] = frame[
                    "predicted_temperature_c"
                ].combine_first(
                    pd.to_numeric(frame["temperature_c"], errors="coerce")
                )

            if "predicted_precipitation_mm" not in frame.columns:
                frame["predicted_precipitation_mm"] = np.nan
            if "precipitation_mm" in frame.columns:
                origin_rain = pd.to_numeric(
                    frame["precipitation_mm"], errors="coerce"
                ).fillna(0.0)
                frame["predicted_precipitation_mm"] = frame[
                    "predicted_precipitation_mm"
                ].combine_first(origin_rain)
            else:
                origin_rain = pd.Series(0.0, index=frame.index)
                frame["predicted_precipitation_mm"] = frame[
                    "predicted_precipitation_mm"
                ].fillna(0.0)

            if "predicted_precipitation_probability" not in frame.columns:
                frame["predicted_precipitation_probability"] = np.nan
            frame["predicted_precipitation_probability"] = frame[
                "predicted_precipitation_probability"
            ].combine_first(
                (
                    origin_rain
                    > config.hourly_forecasting.precipitation.occurrence_threshold_mm
                ).astype(float)
            )

        target_started_weight = work.completed_weight
        training_run = TrainingRun(
            parameter=target,
            forecast_horizon=HOURLY_MODEL_HORIZON_SENTINEL,
        )
        session.add(training_run)
        session.flush()
        training_run.rows_total = len(frame)
        if commit_live_metadata:
            session.commit()

        unique_times = (
            pd.to_datetime(
                frame.get("measurement_time"),
                utc=True,
                errors="coerce",
            ).nunique()
            if not frame.empty
            else 0
        )

        if (
            len(frame) < config.hourly_forecasting.minimum_training_rows
            or unique_times
            < config.hourly_forecasting.minimum_unique_origin_times
        ):
            training_run.status = "skipped_insufficient_data"
            training_run.finished_at = utc_now()
            training_run.summary_json = {
                "available_rows": len(frame),
                "unique_origin_times": int(unique_times),
                "minimum_rows": config.hourly_forecasting.minimum_training_rows,
                "minimum_unique_origin_times": (
                    config.hourly_forecasting.minimum_unique_origin_times
                ),
                "data_provenance": provenance[target],
                "training_profile": profile.name,
            }
            stats.skipped += 1
            spent = work.completed_weight - target_started_weight
            remaining = max(0.0, target_budget - spent)
            if remaining:
                work.advance(
                    f"{target}: skipped — insufficient data",
                    remaining,
                    detail={
                        "target": target,
                        "phase": "training",
                        "profile": profile.name,
                        "available_rows": len(frame),
                        "unique_origin_times": int(unique_times),
                    },
                    status="skipped",
                )
            if commit_live_metadata:
                session.commit()
            continue

        try:
            train, valid = _split_chronologically(
                frame,
                config.hourly_forecasting.validation_fraction,
            )
            validation_selection = policy.select(
                valid,
                target=target,
                phase="validation",
                maximum_rows=min(
                    profile.validation_max_rows,
                    max(1, len(valid)),
                ),
                profile=profile,
                random_state=config.hourly_forecasting.random_state + 17,
            )
            valid = validation_selection.frame

            training_run.rows_train = len(train)
            training_run.rows_validation = len(valid)
            features = _feature_columns(config, target)
            candidates: list[CandidateResult] = []
            mlflow_run_ids: dict[str, str | None] = {}
            configured_algorithms = algorithms_by_target[target]

            for candidate_index, provider_name in enumerate(
                configured_algorithms,
                start=1,
            ):
                if not budget.should_continue(
                    completed_candidates=len(candidates)
                ):
                    remaining_candidates = configured_algorithms[
                        candidate_index - 1 :
                    ]
                    remaining_weight = sum(
                        _provider_work_weight(name)
                        for name in remaining_candidates
                    )
                    if remaining_weight:
                        work.advance(
                            f"{target}: remaining candidates skipped by time budget",
                            remaining_weight,
                            detail={
                                "target": target,
                                "phase": "candidate_validation",
                                "skipped_candidates": list(remaining_candidates),
                                "budget": budget.snapshot(),
                            },
                            status="skipped",
                        )
                    break

                provider_weight = _provider_work_weight(provider_name)
                try:
                    with work.task(
                        (
                            f"{target}: candidate {candidate_index}/"
                            f"{len(configured_algorithms)} ({provider_name})"
                        ),
                        provider_weight,
                        task_key=(
                            f"training:{profile.name}:{target}:candidate:"
                            f"{provider_name}"
                        ),
                        fallback_seconds=_provider_eta_hint(
                            provider_name,
                            len(train),
                        ),
                        detail={
                            "target": target,
                            "provider": provider_name,
                            "phase": "candidate_validation",
                            "profile": profile.name,
                            "candidate_index": candidate_index,
                            "candidate_total": len(configured_algorithms),
                            "rows_train": len(train),
                            "rows_validation": len(valid),
                            "budget": budget.snapshot(),
                        },
                    ):
                        candidate = _fit_candidate(
                            registry=registry,
                            provider_name=provider_name,
                            target=target,
                            train=train,
                            valid=valid,
                            feature_columns=features,
                            config=config,
                        )
                    run_id = mlflow_bridge.log_candidate(
                        target=target,
                        provider=provider_name,
                        profile=profile.name,
                        metrics=candidate.metrics,
                        parameters={
                            "rows_train": len(train),
                            "rows_validation": len(valid),
                            "feature_count": len(features),
                            "model_horizon_min": min(
                                config.hourly_forecasting.model_horizons_hours
                            ),
                            "model_horizon_max": max(
                                config.hourly_forecasting.model_horizons_hours
                            ),
                            "serving_horizon_hours": (
                                config.hourly_forecasting.serving_horizon_count
                            ),
                        },
                        dataset_provenance=(
                            dataset_provenance_payload or provenance.get(target)
                        ),
                    )
                    candidate.metrics["mlflow_run_id"] = run_id
                    mlflow_run_ids[provider_name] = run_id
                    candidates.append(candidate)
                    stats.inserted += 1
                except Exception as exc:
                    logger.exception(
                        "Hourly candidate failed target=%s provider=%s",
                        target,
                        provider_name,
                    )
                    stats.warnings += 1
                    summaries.append(
                        {
                            "target": target,
                            "provider": provider_name,
                            "status": "candidate_failed",
                            "error": str(exc),
                            "training_profile": profile.name,
                        }
                    )

            if not candidates:
                raise RuntimeError(
                    f"No hourly model candidate succeeded for {target}"
                )

            best = min(candidates, key=lambda candidate: candidate.score)
            persistence = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.provider_name == "persistence"
                ),
                None,
            )
            selected = best
            improvement = None
            if (
                persistence is not None
                and np.isfinite(persistence.score)
                and persistence.score > 0
            ):
                improvement = (
                    persistence.score - best.score
                ) / persistence.score
                if (
                    best.provider_name != "persistence"
                    and improvement
                    < config.hourly_forecasting.minimum_mae_improvement_fraction
                ):
                    selected = persistence

            budget_truncated = budget.exhausted
            if budget_truncated:
                # The selected candidate is already fitted on the chronological
                # training split.  It remains a valid deployable model and avoids
                # throwing away the completed validation work merely because the
                # requested wall-clock budget expired.
                final_artifact = {
                    **selected.artifact,
                    "trained_rows": len(train),
                    "trained_at": utc_now().isoformat(),
                    "budget_truncated": True,
                    "quantile_artifacts": {},
                }
            else:
                final_artifact = _fit_final(
                    registry=registry,
                    candidate=selected,
                    target=target,
                    frame=frame,
                    feature_columns=features,
                    config=config,
                    work=work,
                    final_weight=target_budgets[target]["final"],
                    quantile_weight=_provider_work_weight(
                        config.hourly_forecasting.quantile_method
                    ),
                    fit_quantiles=profile.fit_quantiles,
                )

            metrics = {
                **selected.metrics,
                "validation": {
                    "strategy": "chronological_holdout",
                    "rows_train": len(train),
                    "rows_validation": len(valid),
                    "unique_origin_times": int(unique_times),
                    "selection": validation_selection.metadata,
                },
                "data_provenance": provenance[target],
                "candidate_scores": {
                    candidate.provider_name: candidate.score
                    for candidate in candidates
                },
                "mlflow_run_id": mlflow_run_ids.get(selected.provider_name),
                "improvement_vs_persistence": (
                    selected.metrics.get("improvement_vs_persistence")
                    if target == "precipitation_mm" and improvement is None
                    else improvement
                ),
                "model_registry": registry.describe(),
                "training_profile": profile.name,
                "training_set_policy": policy.name,
                "budget": budget.snapshot(),
                "budget_truncated": budget_truncated,
                "horizon_sampling": {
                    "bucket_edges": list(profile.horizon_bucket_edges),
                    "samples_per_bucket": profile.samples_per_horizon_bucket,
                    "maximum_horizons_per_origin": profile.horizons_per_origin,
                },
            }

            with work.task(
                f"{target}: save, upload and activate model",
                target_budgets[target]["register"],
                task_key=f"training:{profile.name}:{target}:register-upload",
                fallback_seconds=60.0,
                detail={
                    "target": target,
                    "provider": selected.provider_name,
                    "phase": "register_upload_activate",
                    "profile": profile.name,
                    "budget": budget.snapshot(),
                },
            ):
                model = _register_model(
                    session,
                    config,
                    target=target,
                    provider_name=selected.provider_name,
                    artifact=final_artifact,
                    metrics=metrics,
                    data_start=pd.to_datetime(
                        frame["measurement_time"], utc=True
                    ).min().to_pydatetime(),
                    data_end=pd.to_datetime(
                        frame["measurement_time"], utc=True
                    ).max().to_pydatetime(),
                )
                precipitation_gate_passed = bool(
                    (metrics.get("precipitation_quality_gate") or {}).get(
                        "passed", True
                    )
                )
                activate_model = True
                if (
                    target == "precipitation_mm"
                    and config.hourly_forecasting.precipitation.mark_experimental_on_failure
                    and not precipitation_gate_passed
                ):
                    activate_model = bool(
                        config.hourly_forecasting.precipitation.activate_experimental_locally
                    )
                if activate_model:
                    _activate(session, config, model)
                    mlflow_bridge.mark_selected(
                        mlflow_run_ids.get(selected.provider_name),
                        model_version=model.semantic_version,
                        artifact_path=model.artifact_path,
                        target=target,
                        provider=selected.provider_name,
                    )
                if target == "precipitation_mm" and not precipitation_gate_passed:
                    stats.warnings += 1

            selected_providers[target] = selected.provider_name
            training_run.best_model_version_id = model.id
            if target == "precipitation_mm" and not precipitation_gate_passed:
                training_run.status = "success_quality_experimental"
            else:
                training_run.status = (
                    "success_budget_truncated"
                    if budget_truncated
                    else "success"
                )
            training_run.finished_at = utc_now()
            training_run.summary_json = {
                "forecast_mode": "horizon-conditioned-hourly",
                "target": target,
                "selected_provider": selected.provider_name,
                "model_version": model.semantic_version,
                "score_mae": selected.score,
                "improvement_vs_persistence": metrics.get(
                    "improvement_vs_persistence"
                ),
                "quality_status": metrics.get("quality_status"),
                "horizons_hours": config.hourly_forecasting.horizons_hours,
                "data_provenance": provenance[target],
                "training_profile": profile.name,
                "training_set_policy": policy.name,
                "budget": budget.snapshot(),
                "budget_truncated": budget_truncated,
            }
            summaries.append(dict(training_run.summary_json))
            if commit_live_metadata:
                session.commit()
        except Exception as exc:
            logger.exception(
                "Hourly model training failed target=%s",
                target,
            )
            training_run.status = "failed"
            training_run.finished_at = utc_now()
            training_run.error_message = str(exc)
            stats.errors += 1
            if commit_live_metadata:
                session.commit()
        finally:
            spent = work.completed_weight - target_started_weight
            remaining = max(0.0, target_budget - spent)
            if remaining:
                work.advance(
                    f"{target}: finalize target budget",
                    remaining,
                    detail={
                        "target": target,
                        "phase": "target_finalization",
                        "status": training_run.status,
                        "profile": profile.name,
                        "budget": budget.snapshot(),
                    },
                    status=(
                        "completed"
                        if training_run.status.startswith("success")
                        else "skipped"
                    ),
                )

    if crossfit_budget_by_target and not crossfit_consumed:
        work.advance(
            "weather cross-fit skipped",
            sum(crossfit_budget_by_target.values()),
            detail={
                "phase": "cross_fit",
                "reason": "no_pm_target_processed",
            },
            status="skipped",
        )

    with work.task(
        "ensure baseline models",
        baseline_weight,
        task_key="training:ensure-baseline-models",
        fallback_seconds=30.0,
        detail={"phase": "baseline_models"},
    ):
        stats.inserted += ensure_hourly_baseline_models(session, config)
        if commit_live_metadata:
            session.commit()

    comparison_result: dict[str, Any] | None = None
    try:
        comparison_result = export_model_comparison(
            session,
            config,
            publish=False,
        )
    except Exception as exc:
        logger.warning(
            "Local model-comparison export failed: %s",
            exc,
            exc_info=True,
        )
        stats.warnings += 1

    work.complete(name=f"hourly model training completed ({profile.name})")
    stats.details = {
        "forecast_mode": "horizon-conditioned-hourly",
        "training_profile": profile.name,
        "training_set_policy": policy.name,
        "resolved_profile": {
            "maximum_training_days_by_target": (
                profile.maximum_training_days_by_target
            ),
            "maximum_rows_per_target": profile.maximum_rows_per_target,
            "validation_max_rows": profile.validation_max_rows,
            "always_keep_recent_days": profile.always_keep_recent_days,
            "horizon_bucket_edges": list(profile.horizon_bucket_edges),
            "samples_per_horizon_bucket": profile.samples_per_horizon_bucket,
            "horizons_per_origin": profile.horizons_per_origin,
            "cross_fit_folds": profile.cross_fit_folds,
            "fit_quantiles": profile.fit_quantiles,
            "max_wall_time_seconds": profile.max_wall_time_seconds,
        },
        "budget": budget.snapshot(),
        "training_snapshot": (
            dict(dataset_provenance_payload)
            if dataset_provenance_payload is not None
            else None
        ),
        "models": summaries,
        "registered_providers": registry.describe(),
        "model_comparison": (
            {
                "local_path": comparison_result.get("local_path"),
                "model_count": comparison_result.get("model_count"),
                "published": comparison_result.get("published"),
            }
            if comparison_result
            else None
        ),
        "progress_file": (
            str(progress.current_path) if progress is not None else None
        ),
    }
    return stats

