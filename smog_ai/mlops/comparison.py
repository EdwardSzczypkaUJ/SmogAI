from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion
from smog_ai.mlops.mlflow_bridge import create_mlflow_bridge

PUBLIC_MODEL_METRIC_KEYS = (
    "mae",
    "rmse",
    "bias",
    "improvement_vs_persistence",
    "brier",
    "brier_skill_vs_climatology",
    "brier_skill_vs_persistence",
    "roc_auc",
    "training_profile",
    "budget_truncated",
    "quality_status",
)

PUBLIC_HORIZON_METRIC_KEYS = (
    "count",
    "mae",
    "rmse",
    "bias",
    "r2",
    "mape",
    "persistence_mae",
    "mae_improvement_vs_persistence",
    "exceedance_accuracy",
)

_HORIZON_METRIC_PATTERN = re.compile(
    r"^by_horizon\.(?P<horizon>[1-9][0-9]*)\.(?P<metric>[a-z0-9_]+)$"
)


def _metric_subset(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mae",
        "rmse",
        "bias",
        "improvement_vs_persistence",
        "brier",
        "brier_skill_vs_climatology",
        "brier_skill_vs_persistence",
        "roc_auc",
        "training_profile",
        "budget_truncated",
        "quality_status",
        "candidate_scores",
        "active_model_comparison",
        "activated",
        "activation_policy",
        "dataset_id",
        "dataset_sha256",
    )
    result = {key: metrics.get(key) for key in keys}
    provenance = dict(metrics.get("data_provenance") or {})
    snapshot = dict(provenance.get("training_snapshot") or {})
    result["dataset_id"] = (
        result.get("dataset_id")
        or provenance.get("dataset_id")
        or snapshot.get("dataset_id")
    )
    result["dataset_sha256"] = (
        result.get("dataset_sha256")
        or provenance.get("dataset_sha256")
        or provenance.get("database_sha256")
        or snapshot.get("dataset_sha256")
        or snapshot.get("database_sha256")
    )
    return result


def _public_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Return quality statistics safe for the public Serving application."""

    result = {
        key: metrics.get(key)
        for key in PUBLIC_MODEL_METRIC_KEYS
        if metrics.get(key) is not None
    }
    candidate_scores = dict(metrics.get("candidate_scores") or {})
    safe_scores: dict[str, float] = {}
    for provider, score in candidate_scores.items():
        try:
            safe_scores[str(provider)] = float(score)
        except (TypeError, ValueError):
            continue
    if safe_scores:
        result["candidate_scores"] = safe_scores
    return result


def _safe_public_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_horizon_quality(
    candidate_runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a compact, identifier-free horizon comparison.

    Only the newest finished run for each target/provider pair is represented.
    This avoids publishing the full MLflow history while preserving enough
    aggregate information for parameter × horizon charts and winner counts.
    """

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in candidate_runs:
        row = dict(raw or {})
        if str(row.get("status") or "").upper() != "FINISHED":
            continue
        params = dict(row.get("params") or {})
        target = str(row.get("target") or params.get("target") or "").strip()
        provider = str(row.get("provider") or params.get("provider") or "").strip()
        metrics = dict(row.get("metrics") or {})
        if not target or not provider or not any(
            _HORIZON_METRIC_PATTERN.match(str(key)) for key in metrics
        ):
            continue
        identity = (target, provider)
        current = latest.get(identity)
        if current is None or str(row.get("start_time") or "") > str(
            current.get("start_time") or ""
        ):
            latest[identity] = row

    horizon_quality: list[dict[str, Any]] = []
    for (target, provider), row in sorted(latest.items()):
        metrics = dict(row.get("metrics") or {})
        by_horizon: dict[int, dict[str, Any]] = {}
        for key, value in metrics.items():
            match = _HORIZON_METRIC_PATTERN.match(str(key))
            if match is None or match.group("metric") not in PUBLIC_HORIZON_METRIC_KEYS:
                continue
            number = _safe_public_number(value)
            if number is None:
                continue
            number = round(number, 6)
            horizon = int(match.group("horizon"))
            if horizon > 48:
                continue
            by_horizon.setdefault(horizon, {})[match.group("metric")] = number
        params = dict(row.get("params") or {})
        for horizon, public_metrics in sorted(by_horizon.items()):
            horizon_quality.append(
                {
                    "target": target,
                    "provider": provider,
                    "profile": row.get("profile") or params.get("profile"),
                    "evaluated_at": row.get("start_time"),
                    "horizon_hours": horizon,
                    "metrics": public_metrics,
                }
            )

    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in horizon_quality:
        mae = _safe_public_number(dict(row.get("metrics") or {}).get("mae"))
        if mae is not None:
            grouped.setdefault(
                (str(row["target"]), int(row["horizon_hours"])), []
            ).append(row)
    winners: list[dict[str, Any]] = []
    for (target, horizon), rows in sorted(grouped.items()):
        winner = min(rows, key=lambda item: float(dict(item["metrics"])["mae"]))
        metrics = dict(winner["metrics"])
        winners.append(
            {
                "target": target,
                "horizon_hours": horizon,
                "provider": winner["provider"],
                "mae": metrics.get("mae"),
                "rmse": metrics.get("rmse"),
                "persistence_mae": metrics.get("persistence_mae"),
                "improvement_vs_persistence": metrics.get(
                    "mae_improvement_vs_persistence"
                ),
                "candidate_count": len(rows),
            }
        )
    return horizon_quality, winners


def build_public_model_comparison_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip local training and MLflow identifiers from comparison metadata.

    The local comparison remains detailed for diagnostics.  Only this reduced
    representation may be written to the public Object Store prefix.
    """

    models = []
    for raw in list(payload.get("models") or []):
        row = dict(raw or {})
        metrics = dict(row.get("metrics") or {})
        active_comparison = dict(metrics.get("active_model_comparison") or {})
        activated = metrics.get("activated")
        if activated is True:
            selection_outcome = "activated"
        elif activated is False:
            selection_outcome = "no_change"
        else:
            selection_outcome = "historical"
        models.append(
            {
                "target": row.get("target"),
                "provider": row.get("provider"),
                "version": row.get("version"),
                "active": bool(row.get("active")),
                "created_at": row.get("created_at"),
                "activated_at": row.get("activated_at"),
                "training_data_start": row.get("training_data_start"),
                "training_data_end": row.get("training_data_end"),
                "metrics": _public_metrics(metrics),
                "selection": {
                    "outcome": selection_outcome,
                    "activation_policy": metrics.get("activation_policy"),
                    "improvement_vs_previous_active": _safe_public_number(
                        active_comparison.get("candidate_improvement_fraction")
                    ),
                    "previous_active_provider": active_comparison.get("provider"),
                    "previous_active_version": active_comparison.get("version"),
                    "previous_active_mae": _safe_public_number(
                        active_comparison.get("active_model_mae")
                    ),
                    "candidate_mae": _safe_public_number(
                        active_comparison.get("candidate_mae")
                    ),
                },
            }
        )

    raw_candidate_runs = list(payload.get("candidate_runs") or [])
    candidate_runs = []
    for raw in raw_candidate_runs:
        row = dict(raw or {})
        params = dict(row.get("params") or {})
        candidate_runs.append(
            {
                "target": row.get("target") or params.get("target"),
                "provider": row.get("provider") or params.get("provider"),
                "profile": row.get("profile") or params.get("profile"),
                "selected": bool(row.get("selected")),
                "status": row.get("status"),
                "start_time": row.get("start_time"),
                "metrics": _public_metrics(dict(row.get("metrics") or {})),
            }
        )

    horizon_quality, horizon_winners = _public_horizon_quality(raw_candidate_runs)
    return {
        "schema_version": "1.1-public",
        "generated_at_utc": payload.get("generated_at_utc"),
        "models": models,
        "candidate_runs": candidate_runs,
        "candidate_run_count": len(candidate_runs),
        "horizon_quality": horizon_quality,
        "horizon_winners": horizon_winners,
        "summary": {
            "model_version_count": len(models),
            "active_model_count": sum(bool(row.get("active")) for row in models),
            "candidate_run_count": len(candidate_runs),
            "finished_candidate_count": sum(
                str(row.get("status") or "").upper() == "FINISHED"
                for row in candidate_runs
            ),
            "selected_candidate_count": sum(
                bool(row.get("selected")) for row in candidate_runs
            ),
            "target_count": len(
                {str(row.get("target")) for row in models if row.get("target")}
            ),
            "horizon_count": len(
                {int(row["horizon_hours"]) for row in horizon_quality}
            ),
        },
        "privacy": {
            "raw_data_included": False,
            "training_data_included": False,
            "model_binaries_included": False,
            "local_paths_included": False,
            "dataset_identifiers_included": False,
            "mlflow_run_identifiers_included": False,
        },
    }


def build_model_comparison_payload(
    session: Session,
    config: AppConfig,
) -> dict[str, Any]:
    rows = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.forecast_horizon == 0)
        .order_by(
            ModelVersion.parameter,
            ModelVersion.created_at.desc(),
        )
    ).all()
    models: list[dict[str, Any]] = []
    for row in rows:
        metrics = dict(row.metrics_json or {})
        mlflow_info = {
            "run_id": metrics.get("mlflow_run_id"),
            "tracking_uri": config.mlflow.tracking_uri or None,
            "experiment_name": config.mlflow.experiment_name,
        }
        models.append(
            {
                "target": row.parameter,
                "provider": row.algorithm,
                "version": row.semantic_version,
                "active": bool(row.active),
                "created_at": row.created_at.isoformat(),
                "activated_at": (
                    row.activated_at.isoformat() if row.activated_at else None
                ),
                "training_data_start": (
                    row.training_data_start.isoformat()
                    if row.training_data_start
                    else None
                ),
                "training_data_end": (
                    row.training_data_end.isoformat()
                    if row.training_data_end
                    else None
                ),
                "artifact_path": row.artifact_path,
                "metrics": _metric_subset(metrics),
                "mlflow": mlflow_info,
            }
        )
    candidate_runs: list[dict[str, Any]] = []
    tracking_error: str | None = None
    try:
        bridge = create_mlflow_bridge(config.mlflow)
        candidate_runs = bridge.compare_runs(
            limit=config.mlflow.maximum_runs_per_target
        )
    except Exception as exc:
        # A comparison export must remain available in local-only mode even
        # when an optional MLflow server is stopped.  The error is made
        # explicit in the artifact instead of breaking the application.
        tracking_error = f"{type(exc).__name__}: {exc}"

    return {
        "schema_version": "1.1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "experiment_name": config.mlflow.experiment_name,
        "tracking_enabled": config.mlflow.enabled,
        "tracking_uri": config.mlflow.tracking_uri or None,
        "ui_url": config.mlflow.ui_url,
        "models": models,
        "candidate_runs": candidate_runs,
        "candidate_run_count": len(candidate_runs),
        "tracking_error": tracking_error,
    }


def export_model_comparison(
    session: Session,
    config: AppConfig,
    *,
    publish: bool | None = None,
) -> dict[str, Any]:
    payload = build_model_comparison_payload(session, config)
    path = Path(config.mlflow.comparison_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    published: dict[str, Any] | None = None
    should_publish = (
        config.mlflow.publish_comparison_to_object_storage
        if publish is None
        else bool(publish)
    )
    if should_publish:
        if not config.object_storage.enabled:
            raise RuntimeError(
                "Model comparison publication requested while ObjectStore is disabled"
            )
        repository = create_artifact_repository(config)
        public_payload = build_public_model_comparison_payload(payload)
        stored = repository.put_json(
            repository.layout.model_comparison_pointer,
            public_payload,
        )
        published = {
            "object_key": stored.key,
            "checksum": stored.checksum,
            "size": stored.size,
            "schema_version": public_payload["schema_version"],
            "privacy": public_payload["privacy"],
        }
    return {
        "status": "ok",
        "local_path": str(path),
        "published": published,
        "model_count": len(payload["models"]),
        "payload": payload,
    }
