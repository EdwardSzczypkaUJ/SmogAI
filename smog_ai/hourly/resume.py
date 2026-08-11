from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from smog_ai.artifacts.datasets import materialize_hourly_training_frames_from_store
from smog_ai.config import AppConfig
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.documentation import load_documentation_bundle, publish_documentation
from smog_ai.domain import StageStats
from smog_ai.hourly.predictor import create_hourly_forecasts
from smog_ai.hourly.recovery import (
    audit_hourly_model_artifacts,
    recover_hourly_models_from_available_artifacts,
)
from smog_ai.hourly.trainer import HOURLY_MODEL_HORIZON_SENTINEL, train_hourly_models
from smog_ai.progress import ProgressReporter
from smog_ai.publishing.publisher import retry_publications
from smog_ai.publishing.snapshot import build_snapshot_stage
from smog_ai.spatial.service import build_spatial_surfaces

RESUME_STAGE_WEIGHTS: dict[str, float] = {
    "audit": 2.0,
    "training_data": 8.0,
    "training": 55.0,
    "documentation": 2.0,
    "prediction": 8.0,
    "spatial": 20.0,
    "snapshot": 3.0,
    "publication": 2.0,
}

RESUME_STAGE_DEFAULT_SECONDS: dict[str, float] = {
    "audit": 30.0,
    "training_data": 600.0,
    "training": 7_200.0,
    "documentation": 60.0,
    "prediction": 600.0,
    "spatial": 3_600.0,
    "snapshot": 300.0,
    "publication": 180.0,
}


def _active_targets(engine: Engine, config: AppConfig) -> list[str]:
    with session_scope(engine) as session:
        return sorted(
            str(value)
            for value in session.scalars(
                select(ModelVersion.parameter).where(
                    ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
                    ModelVersion.active.is_(True),
                )
            ).all()
        )


def _missing_targets(active: list[str], required: list[str]) -> list[str]:
    active_set = set(active)
    return [target for target in required if target not in active_set]


def resume_hourly_after_failure(
    engine: Engine,
    config: AppConfig,
    *,
    retrain_if_missing: bool = False,
    progress: ProgressReporter | None = None,
) -> StageStats:
    """Resume after a late first-run failure without repeating collection/backfill.

    The function first tries active pointers, immutable versioned model objects and
    local joblib files.  Only when at least one target remains unrecoverable and
    ``retrain_if_missing`` is enabled does it rebuild frames and repeat model fit.
    Every expensive group has its own database transaction boundary.
    """

    total = StageStats()
    details: dict[str, Any] = {
        "required_targets": list(config.hourly_forecasting.targets),
        "retrain_if_missing": retrain_if_missing,
    }

    # Fail fast before any optional re-training.
    documentation_preflight = (
        load_documentation_bundle(config).metadata
        if config.documentation.enabled
        else {"status": "disabled"}
    )
    details["documentation_preflight"] = documentation_preflight

    if progress:
        progress.update("audit", 0.0, task="audit local and Spaces model artifacts", force=True)
    with session_scope(engine) as session:
        audit = audit_hourly_model_artifacts(session, config)
    details["model_audit"] = audit
    if progress:
        progress.complete_stage("audit", task="model artifact audit completed", detail={
            "all_targets_recoverable": audit.get("all_targets_recoverable"),
            "status": audit.get("status"),
        })

    if bool(audit.get("all_targets_recoverable")):
        with session_scope(engine) as session:
            recovery = recover_hourly_models_from_available_artifacts(session, config)
        total.merge(recovery)
        details["model_recovery"] = recovery.as_dict()
        if recovery.errors:
            return StageStats(
                downloaded=total.downloaded,
                inserted=total.inserted,
                skipped=total.skipped,
                warnings=total.warnings,
                errors=total.errors,
                details=details,
            )
        if progress:
            progress.complete_stage(
                "training_data",
                task="training data stage skipped; recoverable artifacts found",
                detail={"reason": "model_artifacts_recovered"},
            )
            progress.complete_stage(
                "training",
                task="training skipped; local/Spaces artifacts recovered",
                detail=recovery.as_dict(),
            )
    elif retrain_if_missing:
        config.training.input_source = "object_store"
        with session_scope(engine) as session:
            if progress:
                progress.update(
                    "training_data",
                    0.0,
                    task="materialize h1-h48 training frames from existing Spaces raw bundle",
                    force=True,
                )
            prepared = materialize_hourly_training_frames_from_store(
                session,
                config,
                progress=progress,
            )
            total.merge(prepared)
            details["training_data"] = prepared.as_dict()
            if progress:
                progress.complete_stage(
                    "training_data",
                    task="training frames ready",
                    detail=prepared.as_dict(),
                )

            trained = train_hourly_models(session, config, progress=progress)
            total.merge(trained)
            details["training"] = trained.as_dict()
    else:
        missing = [
            target
            for target, row in (audit.get("targets") or {}).items()
            if not bool((row or {}).get("recoverable"))
        ]
        total.errors += max(1, len(missing))
        details["status"] = "retraining_required"
        details["missing_targets"] = missing
        details["message"] = (
            "No complete recoverable model set was found. Re-run with "
            "--retrain-if-missing to repeat only training and downstream stages; "
            "collection and IMGW archive backfill are not repeated."
        )
        return StageStats(
            downloaded=total.downloaded,
            inserted=total.inserted,
            skipped=total.skipped,
            warnings=total.warnings,
            errors=total.errors,
            details=details,
        )

    active = _active_targets(engine, config)
    missing_active = _missing_targets(active, list(config.hourly_forecasting.targets))
    details["active_targets_after_recovery_or_training"] = active
    if missing_active:
        total.errors += len(missing_active)
        details["status"] = "active_models_incomplete"
        details["missing_active_targets"] = missing_active
        return StageStats(
            downloaded=total.downloaded,
            inserted=total.inserted,
            skipped=total.skipped,
            warnings=total.warnings,
            errors=total.errors,
            details=details,
        )

    if progress:
        progress.update(
            "documentation",
            0.0,
            task="publish technical and mathematical documentation",
            force=True,
        )
    try:
        documentation = publish_documentation(config)
    except Exception as exc:
        total.errors += 1
        details["documentation"] = {"status": "failed", "error": str(exc)}
        if progress:
            progress.complete_stage(
                "documentation",
                task="documentation failed; active models remain committed",
                detail=details["documentation"],
            )
    else:
        total.merge(documentation)
        details["documentation"] = documentation.as_dict()
        if progress:
            progress.complete_stage(
                "documentation",
                task="documentation published",
                detail=documentation.as_dict(),
            )

    # Separate transaction boundaries preserve each completed downstream stage.
    with session_scope(engine) as session:
        predicted = create_hourly_forecasts(session, config, progress=progress)
    total.merge(predicted)
    details["prediction"] = predicted.as_dict()
    if predicted.errors:
        return StageStats(
            downloaded=total.downloaded,
            inserted=total.inserted,
            skipped=total.skipped,
            warnings=total.warnings,
            errors=total.errors,
            details=details,
        )

    with session_scope(engine) as session:
        spatial = build_spatial_surfaces(session, config, progress=progress)
    total.merge(spatial)
    details["spatial"] = spatial.as_dict()
    if spatial.errors:
        return StageStats(
            downloaded=total.downloaded,
            inserted=total.inserted,
            skipped=total.skipped,
            warnings=total.warnings,
            errors=total.errors,
            details=details,
        )

    with session_scope(engine) as session:
        if progress:
            progress.update("snapshot", 0.0, task="build dashboard snapshot", force=True)
        snapshot = build_snapshot_stage(session, config)
        total.merge(snapshot)
        details["snapshot"] = snapshot.as_dict()
        if progress:
            progress.complete_stage(
                "snapshot",
                task="dashboard snapshot completed",
                detail=snapshot.as_dict(),
            )

    with session_scope(engine) as session:
        if progress:
            progress.update("publication", 0.0, task="publish/retry outbox", force=True)
        publication = retry_publications(session, config)
        total.merge(publication)
        details["publication"] = publication.as_dict()
        if progress:
            progress.complete_stage(
                "publication",
                task="publication stage completed",
                detail=publication.as_dict(),
            )

    details["status"] = "partial_success" if total.errors else "success"
    return StageStats(
        downloaded=total.downloaded,
        inserted=total.inserted,
        skipped=total.skipped,
        warnings=total.warnings,
        errors=total.errors,
        details=details,
    )
