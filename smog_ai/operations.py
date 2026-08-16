from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion, ProcessLock, TrainingRun
from smog_ai.database.repository import as_utc
from smog_ai.time_utils import utc_now

TRAINING_LOCK_NAME = "snapshot-hourly-training"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _profile(run: TrainingRun) -> str | None:
    payload = run.summary_json or {}
    value = payload.get("training_profile")
    return str(value) if value else None


def _last_success_by_profile(runs: list[TrainingRun], profile: str) -> datetime | None:
    values = [
        as_utc(run.finished_at)
        for run in runs
        if run.finished_at is not None
        and str(run.status).startswith("success")
        and _profile(run) == profile
    ]
    return max(values) if values else None


def _freshness(age_hours: float, threshold_hours: float) -> str:
    if age_hours <= threshold_hours:
        return "fresh"
    if age_hours <= threshold_hours * 2:
        return "warning"
    return "stale"


def build_public_operations_status(
    session: Session,
    config: AppConfig,
    *,
    source_origin_time: datetime,
    generated_at: datetime,
    surface_count: int,
) -> dict[str, Any]:
    """Build the safe operational subset embedded in Serving v2.

    No paths, process identifiers, host names, training rows or artifacts are
    exposed.  The public app receives only cadence, age, state and model cards.
    """

    generated = as_utc(generated_at)
    origin = as_utc(source_origin_time)
    age_hours = max(0.0, (generated - origin).total_seconds() / 3600.0)
    operations = config.operations
    serving_horizon = config.hourly_forecasting.serving_horizon_hours
    if serving_horizon is None:
        serving_horizon = max(
            config.hourly_forecasting.horizons_hours
            or config.training.horizons_hours
            or [0]
        )

    lock = session.get(ProcessLock, TRAINING_LOCK_NAME)
    training_running = bool(lock and as_utc(lock.expires_at) > utc_now())

    recent_runs = list(
        session.scalars(
            select(TrainingRun).order_by(TrainingRun.started_at.desc()).limit(200)
        ).all()
    )
    regular_completed = _last_success_by_profile(recent_runs, "quick")
    heavy_completed = _last_success_by_profile(recent_runs, "full")

    active_models = list(
        session.scalars(
            select(ModelVersion)
            .where(ModelVersion.active.is_(True))
            .order_by(ModelVersion.parameter)
        ).all()
    )
    model_cards = []
    for model in active_models:
        metrics = model.metrics_json or {}
        candidate_scores = {}
        for provider, score in dict(metrics.get("candidate_scores") or {}).items():
            try:
                candidate_scores[str(provider)] = float(score)
            except (TypeError, ValueError):
                continue
        model_cards.append(
            {
                "parameter": model.parameter,
                "algorithm": model.algorithm,
                "version": model.semantic_version,
                "activated_at": _iso(model.activated_at),
                "training_data_start": _iso(model.training_data_start),
                "training_data_end": _iso(model.training_data_end),
                "training_profile": metrics.get("training_profile"),
                "quality_status": metrics.get("quality_status", "accepted"),
                "metrics": {
                    key: metrics.get(key)
                    for key in (
                        "mae",
                        "rmse",
                        "bias",
                        "improvement_vs_persistence",
                        "brier",
                        "brier_skill_vs_climatology",
                        "roc_auc",
                    )
                    if metrics.get(key) is not None
                },
                "candidate_scores": candidate_scores,
            }
        )

    return {
        "schema_version": "1.0",
        "profile": operations.profile,
        "generated_at": _iso(generated),
        "data": {
            "source_origin_time": _iso(origin),
            "age_hours_at_publication": round(age_hours, 3),
            "freshness_threshold_hours": float(operations.freshness_hours),
            "status_at_publication": _freshness(
                age_hours, float(operations.freshness_hours)
            ),
        },
        "schedule": {
            "serving_refresh_hours": operations.serving_refresh_hours,
            "regular_training_hours": operations.regular_training_hours,
            "heavy_training_hours": operations.heavy_training_hours,
            "deferred_retry_minutes": operations.deferred_retry_minutes,
            "serving_release_retention": operations.serving_release_retention,
        },
        "training": {
            "state_at_publication": "running" if training_running else "idle",
            "started_at": _iso(lock.started_at) if training_running and lock else None,
            "last_regular_completed_at": _iso(regular_completed),
            "next_regular_due_at": _iso(
                regular_completed
                + timedelta(hours=operations.regular_training_hours)
                if regular_completed
                else None
            ),
            "last_heavy_completed_at": _iso(heavy_completed),
            "next_heavy_due_at": _iso(
                heavy_completed + timedelta(hours=operations.heavy_training_hours)
                if heavy_completed
                else None
            ),
            "concurrency_policy": "single-shared-lease-defer",
        },
        "serving": {
            "surface_count": int(surface_count),
            "horizon_hours": int(serving_horizon),
        },
        "models": model_cards,
        "privacy": {
            "training_data_included": False,
            "raw_data_included": False,
            "local_paths_included": False,
            "process_identifiers_included": False,
        },
    }
