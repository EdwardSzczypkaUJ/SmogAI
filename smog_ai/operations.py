from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import (
    AirMeasurement,
    ApplicationState,
    CollectionRun,
    ModelVersion,
    ProcessLock,
    TrainingRun,
    WeatherMeasurement,
)
from smog_ai.database.repository import as_utc
from smog_ai.time_utils import utc_now

TRAINING_LOCK_NAME = "snapshot-hourly-training"
PUBLIC_DATA_FRESH_HOURS = 14.0
PUBLIC_DATA_STALE_HOURS = 22.0


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


def _freshness(
    age_hours: float | None,
    fresh_hours: float,
    stale_hours: float | None = None,
) -> str:
    if age_hours is None:
        return "missing"
    stale_limit = stale_hours if stale_hours is not None else fresh_hours * 2
    if age_hours <= fresh_hours:
        return "fresh"
    if age_hours <= stale_limit:
        return "warning"
    return "stale"


def _worst_freshness(*statuses: str) -> str:
    rank = {"fresh": 0, "warning": 1, "stale": 2, "missing": 3}
    return max(statuses, key=lambda value: rank.get(value, 4))


def _reported_age_hours(generated_at: Any, timestamp: Any) -> float | None:
    if not generated_at or not timestamp:
        return None
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=as_utc(utc_now()).tzinfo)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=as_utc(utc_now()).tzinfo)
    return max(0.0, (generated - observed).total_seconds() / 3600.0)


def _age_hours(newer: datetime, older: datetime | None) -> float | None:
    if older is None:
        return None
    return max(0.0, (newer - as_utc(older)).total_seconds() / 3600.0)


def _state_datetime(session: Session, key: str) -> datetime | None:
    state = session.get(ApplicationState, key)
    if state is None or state.value_json in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(
            str(state.value_json).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    return as_utc(parsed)


def _source_freshness_at_publication(
    session: Session,
    generated: datetime,
) -> list[dict[str, Any]]:
    """Build source freshness from measurements and collector success state."""

    air_latest = [
        as_utc(value)
        for value in session.scalars(
            select(func.max(AirMeasurement.measurement_time)).group_by(
                AirMeasurement.parameter
            )
        ).all()
        if value is not None
    ]
    weather_latest: list[datetime] = []
    for column in (
        WeatherMeasurement.temperature_c,
        WeatherMeasurement.humidity_percent,
        WeatherMeasurement.pressure_hpa,
        WeatherMeasurement.precipitation_mm,
        WeatherMeasurement.wind_speed_mps,
        WeatherMeasurement.wind_direction_deg,
    ):
        value = session.scalar(
            select(func.max(WeatherMeasurement.measurement_time)).where(
                column.is_not(None)
            )
        )
        if value is not None:
            weather_latest.append(as_utc(value))
    source_values = (
        ("GIOS", min(air_latest) if air_latest else None, "last_gios_success_at"),
        (
            "IMGW",
            min(weather_latest) if weather_latest else None,
            "last_imgw_success_at",
        ),
    )
    rows: list[dict[str, Any]] = []
    for source, measurement_end, state_key in source_values:
        last_collection_at = _state_datetime(session, state_key)
        measurement_age = _age_hours(generated, measurement_end)
        collection_age = _age_hours(generated, last_collection_at)
        measurement_status = _freshness(
            measurement_age,
            PUBLIC_DATA_FRESH_HOURS,
            PUBLIC_DATA_STALE_HOURS,
        )
        collection_status = _freshness(
            collection_age,
            PUBLIC_DATA_FRESH_HOURS,
            PUBLIC_DATA_STALE_HOURS,
        )
        rows.append(
            {
                "source": source,
                "measurement_end": _iso(measurement_end),
                "last_collection_at": _iso(last_collection_at),
                "measurement_age_hours_at_publication": (
                    round(measurement_age, 3)
                    if measurement_age is not None
                    else None
                ),
                "collection_age_hours_at_publication": (
                    round(collection_age, 3)
                    if collection_age is not None
                    else None
                ),
                "measurement_status_at_publication": measurement_status,
                "collection_status_at_publication": collection_status,
                "status_at_publication": _worst_freshness(
                    measurement_status, collection_status
                ),
            }
        )
    return rows


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _read_json_report(path: Any) -> dict[str, Any] | None:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return dict(json.loads(path.read_text(encoding=encoding)))
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
    return None


def _digitalocean_transfer_history(config: AppConfig) -> dict[str, Any]:
    """Aggregate safe transfer counters from completed publication reports."""

    root = config.paths.logs_dir.parent / "reports" / "digitalocean"
    if not root.exists():
        return {"status": "unavailable", "source": "local_publication_reports"}
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/03-publication.json")):
        payload = _read_json_report(path)
        if payload is None:
            continue
        details = dict(payload.get("details") or {})
        uploaded = int(details.get("objects_copied") or 0) + 1
        reused = int(details.get("objects_reused") or 0)
        bytes_uploaded = int(details.get("bytes_uploaded") or 0)
        total = uploaded + reused
        rows.append(
            {
                "observed_at": path.parent.name,
                "release_id": details.get("release_id"),
                "destination_backend": details.get("destination_backend"),
                "objects_uploaded": uploaded,
                "objects_reused": reused,
                "bytes_uploaded": bytes_uploaded,
                "request_count": (
                    int(details["request_count"])
                    if details.get("request_count") is not None
                    else None
                ),
                "request_count_minimum": uploaded,
                "reuse_ratio": reused / total if total else None,
                "elapsed_seconds": _safe_number(details.get("elapsed_seconds")),
                "throughput_bytes_per_second": _safe_number(
                    details.get("throughput_bytes_per_second")
                ),
                "bytes_by_category": dict(details.get("bytes_by_category") or {}),
                "pointer_published_last": bool(
                    details.get("pointer_published_last")
                ),
            }
        )
    if not rows:
        return {"status": "unavailable", "source": "local_publication_reports"}

    def aggregate(prefix_length: int) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, int]] = {}
        for row in rows:
            compact = str(row["observed_at"]).replace("-", "")
            period = compact[:prefix_length]
            bucket = grouped.setdefault(
                period,
                {
                    "publication_count": 0,
                    "objects_uploaded": 0,
                    "objects_reused": 0,
                    "bytes_uploaded": 0,
                    "request_count_minimum": 0,
                    "request_count": 0,
                },
            )
            bucket["publication_count"] += 1
            for key in (
                "objects_uploaded",
                "objects_reused",
                "bytes_uploaded",
                "request_count_minimum",
            ):
                bucket[key] += int(row[key])
            bucket["request_count"] += int(
                row.get("request_count")
                if row.get("request_count") is not None
                else row["request_count_minimum"]
            )
        return [{"period": period, **values} for period, values in sorted(grouped.items())]

    limitations = []
    if any(row.get("request_count") is None for row in rows):
        limitations.append("request_count_is_minimum_for_legacy_reports")
    if any(not row.get("bytes_by_category") for row in rows):
        limitations.append("category_breakdown_not_available_in_legacy_reports")
    if any(row.get("elapsed_seconds") is None for row in rows):
        limitations.append("duration_not_available_in_legacy_reports")
    return {
        "status": "measured",
        "source": "local_publication_reports",
        "latest": rows[-1],
        "daily": aggregate(8),
        "monthly": aggregate(6),
        "publication_count": len(rows),
        "limitations": limitations,
    }


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
    fresh_hours = float(config.operations.freshness_hours)
    stale_hours = float(config.operations.freshness_stale_hours)
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
            measurement_ages = [
                number
                for number in (
                    _safe_number(
                        row.get("measurement_age_hours")
                        if row.get("measurement_age_hours") is not None
                        else row.get("age_hours")
                    )
                    for row in rows
                )
                if number is not None
            ]
            collection_ages = [
                number
                for number in (
                    _safe_number(row.get("collection_age_hours"))
                    if row.get("collection_age_hours") is not None
                    else _reported_age_hours(
                        generated_at, row.get("last_collected_at")
                    )
                    for row in rows
                )
                if number is not None
            ]
            maximum_measurement_age = (
                max(measurement_ages) if measurement_ages else None
            )
            maximum_collection_age = max(collection_ages) if collection_ages else None
            measurement_status = _freshness(
                maximum_measurement_age, fresh_hours, stale_hours
            )
            collection_status = _freshness(
                maximum_collection_age, fresh_hours, stale_hours
            )
            status = _worst_freshness(measurement_status, collection_status)
            history.append(
                {
                    "generated_at": generated_at,
                    "source": source,
                    "status": status,
                    "maximum_age_hours": maximum_measurement_age,
                    "maximum_measurement_age_hours": maximum_measurement_age,
                    "maximum_collection_age_hours": maximum_collection_age,
                    "measurement_status": measurement_status,
                    "collection_status": collection_status,
                    "fresh_threshold_hours": fresh_hours,
                    "stale_threshold_hours": stale_hours,
                    "threshold_hours": fresh_hours,
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
    latest_collection_at: datetime | None = None
    for run in collection_runs:
        run_type = str(run.run_type or "")
        if not any(token in run_type.casefold() for token in ("collect", "pipeline", "refresh")):
            continue
        collected_at = run.finished_at or run.started_at
        if collected_at is not None:
            collected = as_utc(collected_at)
            if latest_collection_at is None or collected > latest_collection_at:
                latest_collection_at = collected
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

    source_freshness = _source_freshness_at_publication(session, generated)
    successful_source_collections = [
        _state_datetime(
            session,
            (
                "last_gios_success_at"
                if row["source"] == "GIOS"
                else "last_imgw_success_at"
            ),
        )
        for row in source_freshness
    ]
    successful_source_collections = [
        value for value in successful_source_collections if value is not None
    ]
    if successful_source_collections:
        latest_collection_at = min(successful_source_collections)
    collection_age_hours = _age_hours(generated, latest_collection_at)
    measurement_status = _freshness(
        age_hours,
        PUBLIC_DATA_FRESH_HOURS,
        PUBLIC_DATA_STALE_HOURS,
    )
    collection_status = _freshness(
        collection_age_hours,
        PUBLIC_DATA_FRESH_HOURS,
        PUBLIC_DATA_STALE_HOURS,
    )
    return {
        "schema_version": "1.1",
        "profile": operations.profile,
        "generated_at": _iso(generated),
        "data": {
            "source_origin_time": _iso(origin),
            "last_collection_at": _iso(latest_collection_at),
            "age_hours_at_publication": round(age_hours, 3),
            "measurement_age_hours_at_publication": round(age_hours, 3),
            "collection_age_hours_at_publication": (
                round(collection_age_hours, 3)
                if collection_age_hours is not None else None
            ),
            "freshness_threshold_hours": PUBLIC_DATA_FRESH_HOURS,
            "fresh_threshold_hours": PUBLIC_DATA_FRESH_HOURS,
            "stale_threshold_hours": PUBLIC_DATA_STALE_HOURS,
            "measurement_status_at_publication": measurement_status,
            "collection_status_at_publication": collection_status,
            "status_at_publication": _worst_freshness(
                measurement_status, collection_status
            ),
            "sources": source_freshness,
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
        "finops": {
            "digitalocean": _digitalocean_transfer_history(config),
        },
        "models": model_cards,
        "privacy": {
            "training_data_included": False,
            "raw_data_included": False,
            "local_paths_included": False,
            "process_identifiers_included": False,
        },
    }
