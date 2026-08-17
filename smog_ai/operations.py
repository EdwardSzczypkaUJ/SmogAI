from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import CollectionRun, ModelVersion, ProcessLock, TrainingRun
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


def _age_hours(newer: datetime, older: datetime | None) -> float | None:
    if older is None:
        return None
    return max(0.0, (newer - as_utc(older)).total_seconds() / 3600.0)


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _freshness_check_history(config: AppConfig, *, limit: int = 30) -> list[dict[str, Any]]:
    """Read safe source-level aggregates from local freshness reports."""

    root = config.paths.logs_dir.parent / "reports" / "freshness"
    if not root.exists():
        return []
    paths = sorted(
        (
            path
            for path in root.glob("data-freshness-*.json")
            if path.name != "data-freshness-latest.json"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    history: list[dict[str, Any]] = []
    status_rank = {"fresh": 0, "warning": 1, "stale": 2, "missing": 3}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        generated_at = payload.get("generated_at")
        for source in ("GIOS", "IMGW"):
            rows = [
                dict(row)
                for row in list(payload.get("parameters") or [])
                if str(dict(row).get("source") or "").upper() == source
            ]
            if not rows:
                continue
            statuses = [str(row.get("status") or "missing") for row in rows]
            status = max(statuses, key=lambda value: status_rank.get(value, 4))
            ages = [
                number
                for number in (_safe_number(row.get("age_hours")) for row in rows)
                if number is not None
            ]
            history.append(
                {
                    "generated_at": generated_at,
                    "source": source,
                    "status": status,
                    "maximum_age_hours": max(ages) if ages else None,
                    "threshold_hours": _safe_number(rows[0].get("threshold_hours")),
                    "parameter_count": len(rows),
                    "valid_rows": sum(int(row.get("valid_rows") or 0) for row in rows),
                    "last_collected_at": max(
                        (str(row.get("last_collected_at")) for row in rows if row.get("last_collected_at")),
                        default=None,
                    ),
                    "measurement_end": max(
                        (str(row.get("measurement_end")) for row in rows if row.get("measurement_end")),
                        default=None,
                    ),
                }
            )
    return sorted(history, key=lambda row: str(row.get("generated_at") or ""))


def _training_outcome(
    run: TrainingRun,
    summary: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[str, str]:
    status = str(run.status or "unknown")
    if status == "running":
        return "running", "training_in_progress"
    if status.startswith("failed"):
        return "failed", "training_failed"
    if bool(summary.get("activated") or metrics.get("activated")):
        return "activated", "better_model_activated"
    activation_policy = str(
        summary.get("activation_policy") or metrics.get("activation_policy") or ""
    )
    quality_status = str(summary.get("quality_status") or metrics.get("quality_status") or "")
    comparison = dict(metrics.get("active_model_comparison") or {})
    improvement = _safe_number(comparison.get("candidate_improvement_fraction"))
    if activation_policy == "candidate_only":
        return "no_change", "candidate_only_review"
    if quality_status in {"experimental", "rejected"}:
        return "no_change", f"quality_{quality_status}"
    if improvement is not None and improvement <= 0:
        return "no_change", "candidate_not_better_than_active"
    return "no_change", "no_activation_after_evaluation"


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

    model_ids = [run.best_model_version_id for run in recent_runs if run.best_model_version_id]
    trained_models = {
        model.id: model
        for model in session.scalars(
            select(ModelVersion).where(ModelVersion.id.in_(model_ids))
        ).all()
    } if model_ids else {}
    training_history: list[dict[str, Any]] = []
    latest_evaluation_by_target: dict[str, datetime] = {}
    for run in recent_runs[:60]:
        summary = dict(run.summary_json or {})
        model = trained_models.get(str(run.best_model_version_id or ""))
        metrics = dict(model.metrics_json or {}) if model is not None else {}
        target = str(run.parameter or summary.get("target") or "")
        finished = as_utc(run.finished_at) if run.finished_at is not None else None
        if target and finished is not None and str(run.status).startswith("success"):
            previous = latest_evaluation_by_target.get(target)
            if previous is None or finished > previous:
                latest_evaluation_by_target[target] = finished
        outcome, reason = _training_outcome(run, summary, metrics)
        active_comparison = dict(metrics.get("active_model_comparison") or {})
        training_history.append(
            {
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "status": str(run.status),
                "target": target or None,
                "profile": _profile(run),
                "provider": summary.get("selected_provider")
                or (model.algorithm if model is not None else None),
                "model_version": summary.get("model_version")
                or (model.semantic_version if model is not None else None),
                "mae": _safe_number(summary.get("score_mae") or metrics.get("mae")),
                "improvement_vs_persistence": _safe_number(
                    summary.get("improvement_vs_persistence")
                    if summary.get("improvement_vs_persistence") is not None
                    else metrics.get("improvement_vs_persistence")
                ),
                "improvement_vs_previous_active": _safe_number(
                    active_comparison.get("candidate_improvement_fraction")
                ),
                "quality_status": summary.get("quality_status")
                or metrics.get("quality_status"),
                "outcome": outcome,
                "outcome_reason": reason,
            }
        )

    collection_runs = list(
        session.scalars(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(60)
        ).all()
    )
    public_collection_runs = []
    for run in collection_runs:
        run_type = str(run.run_type or "")
        if not any(token in run_type.casefold() for token in ("collect", "pipeline", "refresh")):
            continue
        public_collection_runs.append(
            {
                "run_type": run_type,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "status": str(run.status),
                "downloaded": int(run.records_downloaded or 0),
                "inserted": int(run.records_inserted or 0),
                "skipped": int(run.records_skipped or 0),
                "warnings": int(run.warnings_count or 0),
                "errors": int(run.errors_count or 0),
            }
        )
        if len(public_collection_runs) >= 30:
            break

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
        model_age = _age_hours(generated, model.activated_at or model.created_at)
        last_evaluated = latest_evaluation_by_target.get(str(model.parameter))
        evaluation_age = _age_hours(generated, last_evaluated)
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
                "model_age_hours_at_publication": (
                    round(model_age, 3) if model_age is not None else None
                ),
                "last_evaluated_at": _iso(last_evaluated),
                "evaluation_age_hours_at_publication": (
                    round(evaluation_age, 3) if evaluation_age is not None else None
                ),
                "freshness_threshold_hours": float(operations.regular_training_hours),
                "freshness_status": (
                    _freshness(evaluation_age, float(operations.regular_training_hours))
                    if evaluation_age is not None
                    else "unknown"
                ),
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
            "history": training_history,
        },
        "collection_history": {
            "runs": public_collection_runs,
            "freshness_checks": _freshness_check_history(config),
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
