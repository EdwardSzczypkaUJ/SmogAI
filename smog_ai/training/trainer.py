from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import (
    create_artifact_repository,
    load_training_frame_from_store,
)
from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion, TrainingRun
from smog_ai.domain import StageStats
from smog_ai.features.builder import FEATURE_COLUMNS, build_training_frame
from smog_ai.storage.base import ObjectNotFoundError
from smog_ai.time_utils import utc_now
from smog_ai.training.metrics import regression_metrics
from smog_ai.training.models import make_regressor

logger = logging.getLogger(__name__)


def _threshold(parameter: str) -> float:
    return 50.0 if parameter == "PM10" else 25.0


def _semantic_version(algorithm: str) -> str:
    return f"{utc_now().strftime('%Y.%m.%d.%H%M%S')}-{algorithm}-{uuid.uuid4().hex[:6]}"


def _save_artifact(path: Path, artifact: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    joblib.dump(artifact, temporary)
    temporary.replace(path)


def _upload_model(
    config: AppConfig,
    *,
    parameter: str,
    horizon: int,
    semantic_version: str,
    algorithm: str,
    artifact: Any,
    metrics: dict[str, Any],
    data_start: datetime | None,
    data_end: datetime | None,
) -> dict[str, Any]:
    if not config.object_storage.enabled or not config.artifacts.upload_models:
        return {}
    repository = create_artifact_repository(config)
    binary_key = repository.layout.model_binary(parameter, horizon, semantic_version)
    stored = repository.put_joblib(
        binary_key,
        artifact,
        immutable=True,
        metadata={
            "parameter": parameter,
            "horizon-hours": str(horizon),
            "model-version": semantic_version,
            "algorithm": algorithm,
        },
    )
    card = {
        "schema_version": config.artifacts.schema_version,
        "model_version": semantic_version,
        "algorithm": algorithm,
        "parameter": parameter,
        "horizon_hours": horizon,
        "feature_columns": list(FEATURE_COLUMNS),
        "metrics": metrics,
        "training_data_start": data_start.isoformat() if data_start else None,
        "training_data_end": data_end.isoformat() if data_end else None,
        "artifact": {
            "object_key": stored.key,
            "checksum": stored.checksum,
            "size": stored.size,
            "storage_backend": repository.store.backend_name,
        },
        "created_at": utc_now().isoformat(),
        "source_host_id": config.source_host_id,
    }
    card_key = repository.layout.model_card(parameter, horizon, semantic_version)
    repository.put_json(card_key, card, immutable=True)
    metrics_key = repository.layout.classical_model_metrics(parameter, horizon, semantic_version)
    repository.put_json(
        metrics_key,
        {
            "schema_version": config.artifacts.schema_version,
            "model_version": semantic_version,
            "algorithm": algorithm,
            "parameter": parameter,
            "horizon_hours": horizon,
            "metrics": metrics,
            "created_at": card["created_at"],
            "source_host_id": config.source_host_id,
        },
        immutable=True,
    )
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
    parameter: str,
    horizon: int,
    algorithm: str,
    artifact: Any,
    metrics: dict[str, Any],
    data_start: datetime | None,
    data_end: datetime | None,
) -> ModelVersion:
    semantic = _semantic_version(algorithm)
    artifact_path = config.paths.models_dir / parameter.replace(".", "_") / f"h{horizon}" / f"{semantic}.joblib"
    _save_artifact(artifact_path, artifact)
    metrics_payload = dict(metrics)
    try:
        metrics_payload["remote_artifact"] = _upload_model(
            config,
            parameter=parameter,
            horizon=horizon,
            semantic_version=semantic,
            algorithm=algorithm,
            artifact=artifact,
            metrics=metrics,
            data_start=data_start,
            data_end=data_end,
        )
    except Exception as exc:
        logger.exception("Model upload failed for %s/%sh/%s", parameter, horizon, algorithm)
        metrics_payload["remote_artifact_error"] = str(exc)
    row = ModelVersion(
        model_name=f"{parameter}-{horizon}h-{algorithm}",
        algorithm=algorithm,
        parameter=parameter,
        forecast_horizon=horizon,
        semantic_version=semantic,
        artifact_path=str(artifact_path),
        feature_columns_json=list(FEATURE_COLUMNS),
        metrics_json=metrics_payload,
        training_data_start=data_start,
        training_data_end=data_end,
        active=False,
    )
    session.add(row)
    session.flush()
    return row


def _activate(session: Session, model: ModelVersion, config: AppConfig) -> None:
    session.execute(
        update(ModelVersion)
        .where(
            ModelVersion.parameter == model.parameter,
            ModelVersion.forecast_horizon == model.forecast_horizon,
        )
        .values(active=False)
    )
    model.active = True
    model.activated_at = utc_now()
    remote = (model.metrics_json or {}).get("remote_artifact") or {}
    if config.object_storage.enabled and remote.get("artifact_object_key"):
        try:
            repository = create_artifact_repository(config)
            repository.put_json(
                repository.layout.active_model_pointer(model.parameter, model.forecast_horizon),
                {
                    "schema_version": config.artifacts.schema_version,
                    "parameter": model.parameter,
                    "horizon_hours": model.forecast_horizon,
                    "model_version": model.semantic_version,
                    "algorithm": model.algorithm,
                    "artifact_object_key": remote["artifact_object_key"],
                    "artifact_checksum": remote.get("artifact_checksum"),
                    "model_card_object_key": remote.get("model_card_object_key"),
                    "activated_at": model.activated_at.isoformat(),
                    "source_host_id": config.source_host_id,
                },
            )
        except Exception:
            logger.exception("Could not update active model pointer")


def ensure_baseline_models(session: Session, config: AppConfig) -> int:
    """Create active persistence models for combinations that have never been trained."""
    created = 0
    for parameter in config.training.parameters:
        for horizon in config.training.horizons_hours:
            current = session.scalar(
                select(ModelVersion).where(
                    ModelVersion.parameter == parameter,
                    ModelVersion.forecast_horizon == horizon,
                    ModelVersion.active.is_(True),
                )
            )
            if current is not None:
                continue
            row = _register_model(
                session,
                config,
                parameter=parameter,
                horizon=horizon,
                algorithm="persistence",
                artifact={"algorithm": "persistence", "feature": "value"},
                metrics={"bootstrap": True, "reason": "No validated trained model available yet."},
                data_start=None,
                data_end=None,
            )
            _activate(session, row, config)
            created += 1
    return created


def _predict_baseline(algorithm: str, train_target: np.ndarray, validation_frame: Any) -> np.ndarray:
    if algorithm == "persistence":
        return validation_frame["value"].to_numpy(dtype=float)
    if algorithm == "historical_mean":
        return np.full(len(validation_frame), float(np.nanmean(train_target)), dtype=float)
    raise ValueError(algorithm)


def _training_frame(
    session: Session,
    config: AppConfig,
    *,
    parameter: str,
    horizon: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if config.training.input_source == "database":
        return (
            build_training_frame(
                session,
                parameter=parameter,
                horizon_hours=horizon,
                max_days=config.training.max_training_days,
            ),
            {"source": "database"},
        )
    try:
        frame, manifest = load_training_frame_from_store(
            config,
            parameter=parameter,
            horizon=horizon,
        )
        return frame, {"source": "object_store", "manifest": manifest}
    except ObjectNotFoundError as exc:
        # A first installation legitimately has no curated pointer yet.  Treat this
        # as an empty dataset, not as a storage outage with six stack traces.
        logger.info("Training dataset not available yet for %s/%sh", parameter, horizon)
        return pd.DataFrame(), {
            "source": "object_store",
            "status": "missing",
            "object_store_error": str(exc),
        }
    except Exception as exc:
        if not config.training.allow_database_fallback:
            raise RuntimeError(
                f"Training dataset for {parameter}/{horizon}h is unavailable in object storage"
            ) from exc
        logger.warning("Object-store dataset unavailable; using database fallback: %s", exc)
        return (
            build_training_frame(
                session,
                parameter=parameter,
                horizon_hours=horizon,
                max_days=config.training.max_training_days,
            ),
            {"source": "database_fallback", "object_store_error": str(exc)},
        )


def train_models(session: Session, config: AppConfig) -> StageStats:
    stats = StageStats()
    config.paths.models_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for parameter in config.training.parameters:
        for horizon in config.training.horizons_hours:
            training_run = TrainingRun(parameter=parameter, forecast_horizon=horizon)
            session.add(training_run)
            session.flush()
            try:
                frame, provenance = _training_frame(
                    session,
                    config,
                    parameter=parameter,
                    horizon=horizon,
                )
                training_run.rows_total = len(frame)
                if len(frame) < config.training.minimum_training_rows:
                    training_run.status = "skipped_insufficient_data"
                    training_run.finished_at = utc_now()
                    training_run.summary_json = {
                        "minimum_required": config.training.minimum_training_rows,
                        "available": len(frame),
                        "data_provenance": provenance,
                    }
                    stats.skipped += 1
                    continue
                split = max(1, min(len(frame) - 1, int(len(frame) * (1 - config.training.validation_fraction))))
                train = frame.iloc[:split].copy()
                valid = frame.iloc[split:].copy()
                training_run.rows_train = len(train)
                training_run.rows_validation = len(valid)
                y_train = train["target"].to_numpy(dtype=float)
                y_valid = valid["target"].to_numpy(dtype=float)
                persistence_valid = valid["value"].to_numpy(dtype=float)
                candidates: list[tuple[ModelVersion, float]] = []
                for algorithm in config.training.algorithms:
                    if algorithm in {"persistence", "historical_mean"}:
                        estimator: Any = {"algorithm": algorithm}
                        predictions = _predict_baseline(algorithm, y_train, valid)
                        if algorithm == "historical_mean":
                            estimator["mean"] = float(np.nanmean(y_train))
                    else:
                        estimator = make_regressor(algorithm, random_state=config.training.random_state)
                        estimator.fit(train[FEATURE_COLUMNS], y_train)
                        predictions = estimator.predict(valid[FEATURE_COLUMNS])
                    metrics = regression_metrics(
                        y_valid,
                        np.asarray(predictions, dtype=float),
                        persistence=persistence_valid,
                        exceedance_threshold=_threshold(parameter),
                    )
                    metrics["data_provenance"] = provenance
                    artifact = {
                        "algorithm": algorithm,
                        "parameter": parameter,
                        "horizon_hours": horizon,
                        "feature_columns": list(FEATURE_COLUMNS),
                        "estimator": estimator,
                    }
                    model = _register_model(
                        session,
                        config,
                        parameter=parameter,
                        horizon=horizon,
                        algorithm=algorithm,
                        artifact=artifact,
                        metrics=metrics,
                        data_start=frame["measurement_time"].min().to_pydatetime(),
                        data_end=frame["measurement_time"].max().to_pydatetime(),
                    )
                    candidates.append((model, float(metrics["mae"])))
                    stats.inserted += 1
                persistence_pair = next(pair for pair in candidates if pair[0].algorithm == "persistence")
                best = min(candidates, key=lambda item: item[1])
                persistence_mae = persistence_pair[1]
                improvement = (persistence_mae - best[1]) / persistence_mae if persistence_mae > 0 else 0.0
                selected = (
                    best
                    if best[0].algorithm == "persistence"
                    or improvement >= config.training.minimum_mae_improvement_fraction
                    else persistence_pair
                )
                _activate(session, selected[0], config)
                training_run.best_model_version_id = selected[0].id
                training_run.status = "success"
                training_run.finished_at = utc_now()
                training_run.summary_json = {
                    "selected_algorithm": selected[0].algorithm,
                    "selected_semantic_version": selected[0].semantic_version,
                    "persistence_mae": persistence_mae,
                    "selected_mae": selected[1],
                    "improvement_fraction": improvement,
                    "data_provenance": provenance,
                    "remote_artifact": (selected[0].metrics_json or {}).get("remote_artifact"),
                }
                summaries.append(dict(training_run.summary_json))
            except Exception as exc:
                logger.exception("Training failed for %s/%sh", parameter, horizon)
                training_run.status = "failed"
                training_run.finished_at = utc_now()
                training_run.error_message = str(exc)
                stats.errors += 1
    # Ensure every parameter/horizon has a safe active persistence model, even
    # when only some combinations had enough historical rows to train.
    stats.inserted += ensure_baseline_models(session, config)
    stats.details = {"models": summaries}
    return stats
