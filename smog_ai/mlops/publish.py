from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.artifacts.repository import canonical_json_bytes
from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion
from smog_ai.hourly.trainer import HOURLY_MODEL_HORIZON_SENTINEL
from smog_ai.mlops.comparison import export_model_comparison
from smog_ai.storage.base import ObjectConflictError
from smog_ai.time_utils import utc_now

PUBLISH_CONFIRMATION = "PUBLISH APPROVED MODELS ONLY"


_IMMUTABLE_METRIC_EXCLUDED_KEYS = frozenset({"remote_artifact"})


def _immutable_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Remove transport metadata that cannot define a semantic model version."""

    stable = deepcopy(metrics)
    for key in _IMMUTABLE_METRIC_EXCLUDED_KEYS:
        stable.pop(key, None)
    return stable


def _normalized_immutable_card(payload: Any) -> Any:
    """Normalize HF20 and HF20.4 cards for safe retry compatibility."""

    normalized = deepcopy(payload)
    if not isinstance(normalized, dict):
        return normalized
    normalized.pop("published_at", None)
    metrics = normalized.get("metrics")
    if isinstance(metrics, dict):
        for key in _IMMUTABLE_METRIC_EXCLUDED_KEYS:
            metrics.pop(key, None)
    return normalized


def _first_difference(left: Any, right: Any, path: str = "$") -> str | None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left:
                return f"{child} exists only in the new payload"
            if key not in right:
                return f"{child} exists only in the stored payload"
            found = _first_difference(left[key], right[key], child)
            if found:
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path} length differs: stored={len(left)} new={len(right)}"
        for index, (stored, candidate) in enumerate(zip(left, right, strict=True)):
            found = _first_difference(stored, candidate, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return f"{path} differs: stored={left!r} new={right!r}"
    return None


def _put_immutable_json_idempotent(
    repository: Any,
    key: str,
    payload: dict[str, Any],
) -> tuple[Any, str]:
    """Create immutable JSON or reuse an equivalent legacy HF20 object."""

    existed = repository.store.exists(key)
    try:
        stored = repository.put_json(key, payload, immutable=True)
        return stored, "reused" if existed else "created"
    except ObjectConflictError as exc:
        existing = repository.get_json(key)
        stored_normalized = _normalized_immutable_card(existing)
        candidate_normalized = _normalized_immutable_card(payload)
        if canonical_json_bytes(stored_normalized) == canonical_json_bytes(
            candidate_normalized
        ):
            stored = repository.put_json(key, existing, immutable=True)
            return stored, "reused_legacy"

        difference = _first_difference(
            stored_normalized,
            candidate_normalized,
        ) or "unknown semantic difference"
        raise ObjectConflictError(
            "Immutable model publication object already exists with "
            f"substantively different content: {key}; {difference}"
        ) from exc


def _snapshot_provenance(metrics: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(metrics.get("data_provenance") or {})
    snapshot = dict(provenance.get("training_snapshot") or {})
    return {
        "dataset_id": provenance.get("dataset_id") or snapshot.get("dataset_id"),
        "database_sha256": snapshot.get("database_sha256")
        or snapshot.get("dataset_sha256"),
        "immutable": snapshot.get("immutable"),
        "created_at": snapshot.get("created_at")
        or snapshot.get("generated_at_utc"),
    }


def model_publication_failures(
    config: AppConfig,
    model: ModelVersion,
) -> list[dict[str, Any]]:
    metrics = dict(model.metrics_json or {})
    failures: list[dict[str, Any]] = []
    snapshot = _snapshot_provenance(metrics)
    quality_status = str(metrics.get("quality_status") or "approved").lower()
    if quality_status == "accepted":
        quality_status = "approved"

    if not model.active:
        failures.append({"reason": "model_not_active"})
    if bool(metrics.get("bootstrap")):
        failures.append({"reason": "bootstrap_model_forbidden"})
    if not model.artifact_path or not Path(model.artifact_path).is_file():
        failures.append({"reason": "local_model_artifact_missing"})
    if not snapshot.get("dataset_id"):
        failures.append({"reason": "dataset_id_missing"})
    if not snapshot.get("database_sha256"):
        failures.append({"reason": "dataset_sha256_missing"})
    if snapshot.get("immutable") is not True:
        failures.append({"reason": "immutable_snapshot_required"})
    if quality_status != "approved":
        failures.append(
            {
                "reason": "model_not_approved",
                "quality_status": quality_status,
                "details": (
                    (metrics.get("quality_classification") or {}).get("reasons")
                    or []
                ),
            }
        )

    if model.parameter == "precipitation_mm":
        gate = dict(metrics.get("precipitation_quality_gate") or {})
        if gate.get("passed") is not True:
            failures.append(
                {
                    "reason": "precipitation_quality_gate_failed",
                    "details": gate.get("failures") or [],
                }
            )
    else:
        improvement = metrics.get("improvement_vs_persistence")
        minimum = float(
            config.hourly_forecasting.minimum_mae_improvement_fraction
        )
        if model.algorithm == "persistence":
            failures.append({"reason": "persistence_model_forbidden"})
        elif improvement is None or float(improvement) < minimum:
            failures.append(
                {
                    "reason": "insufficient_improvement",
                    "actual": improvement,
                    "minimum": minimum,
                }
            )
    return failures


def _model_card(
    config: AppConfig,
    model: ModelVersion,
    artifact: dict[str, Any],
    stored: Any,
) -> dict[str, Any]:
    metrics = _immutable_metrics(dict(model.metrics_json or {}))
    snapshot = _snapshot_provenance(metrics)
    return {
        "schema_version": "3.0",
        "forecast_mode": "serving-lead/model-horizon",
        "model_version": model.semantic_version,
        "target": model.parameter,
        "provider": model.algorithm,
        "feature_columns": artifact.get("feature_columns")
        or model.feature_columns_json,
        "model_horizons_hours": list(
            artifact.get("horizons_hours")
            or config.hourly_forecasting.model_horizons_hours
        ),
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
        "training_profile": metrics.get("training_profile"),
        "quality_status": metrics.get("quality_status"),
        "precipitation_quality_gate": metrics.get(
            "precipitation_quality_gate"
        ),
        "metrics": metrics,
        "training_snapshot": snapshot,
        "training_data_start": (
            model.training_data_start.isoformat()
            if model.training_data_start
            else None
        ),
        "training_data_end": (
            model.training_data_end.isoformat()
            if model.training_data_end
            else None
        ),
        "source_host_id": config.source_host_id,
        "artifact": {
            "object_key": stored.key,
            "checksum": stored.checksum,
            "size": stored.size,
            "storage_backend": "object_store",
        },
        "data_disclosure": {
            "raw_data_included": False,
            "sqlite_included": False,
            "training_snapshot_included": False,
            "training_rows_included": False,
        },
    }


def publish_approved_hourly_models(
    session: Session,
    config: AppConfig,
    *,
    targets: Iterable[str],
    confirmation: str,
    publish_comparison: bool = True,
) -> dict[str, Any]:
    if confirmation != PUBLISH_CONFIRMATION:
        raise ValueError(
            "Explicit publication confirmation is required: "
            + PUBLISH_CONFIRMATION
        )
    if not config.object_storage.enabled:
        raise RuntimeError("ObjectStore must be enabled for publication")
    if not config.artifacts.upload_models:
        raise RuntimeError(
            "artifacts.upload_models must be explicitly enabled for publication"
        )

    requested = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in targets
            if str(value).strip()
        )
    )
    if not requested:
        raise ValueError("At least one model target is required")

    models = session.scalars(
        select(ModelVersion).where(
            ModelVersion.active.is_(True),
            ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
            ModelVersion.parameter.in_(requested),
        )
    ).all()
    by_target = {model.parameter: model for model in models}

    validation: dict[str, Any] = {}
    for target in requested:
        model = by_target.get(target)
        if model is None:
            validation[target] = [{"reason": "active_model_missing"}]
        else:
            validation[target] = model_publication_failures(config, model)
    failed = {
        target: failures
        for target, failures in validation.items()
        if failures
    }
    if failed:
        raise RuntimeError(
            "Model publication quality gate failed: "
            + json.dumps(failed, ensure_ascii=False, default=str)
        )

    repository = create_artifact_repository(config)
    repository.ping()
    published: list[dict[str, Any]] = []

    for target in requested:
        model = by_target[target]
        path = Path(str(model.artifact_path)).resolve()
        artifact = joblib.load(path)
        if not isinstance(artifact, dict):
            raise TypeError(f"Model artifact must be a dictionary: {path}")

        binary_key = repository.layout.hourly_model_binary(
            target, model.semantic_version
        )
        binary_existed = repository.store.exists(binary_key)
        stored = repository.put_joblib(
            binary_key,
            artifact,
            immutable=True,
            metadata={
                "target": target,
                "model-version": model.semantic_version,
                "provider": model.algorithm,
                "forecast-mode": "serving-lead/model-horizon",
                "local-sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        binary_status = "reused" if binary_existed else "created"
        card = _model_card(config, model, artifact, stored)
        card_key = repository.layout.hourly_model_card(
            target, model.semantic_version
        )
        metrics_key = repository.layout.hourly_model_metrics(
            target, model.semantic_version
        )
        _, card_status = _put_immutable_json_idempotent(
            repository, card_key, card
        )
        _, metrics_status = _put_immutable_json_idempotent(
            repository, metrics_key, card
        )
        activated_at = model.activated_at or utc_now()
        published_at = datetime.now(UTC).isoformat()
        pointer = {
            "schema_version": "3.0",
            "forecast_mode": "serving-lead/model-horizon",
            "target": target,
            "model_version": model.semantic_version,
            "provider": model.algorithm,
            "artifact_object_key": stored.key,
            "artifact_checksum": stored.checksum,
            "model_card_object_key": card_key,
            "metrics_object_key": metrics_key,
            "activated_at": activated_at.isoformat(),
            "published_at": published_at,
            "source_host_id": config.source_host_id,
            "quality_status": (model.metrics_json or {}).get("quality_status"),
            "time_contract": card["time_contract"],
            "training_snapshot": card["training_snapshot"],
        }
        repository.put_json(
            repository.layout.active_hourly_model_pointer(target),
            pointer,
        )

        metrics_payload = dict(model.metrics_json or {})
        metrics_payload["remote_artifact"] = {
            "artifact_object_key": stored.key,
            "artifact_checksum": stored.checksum,
            "model_card_object_key": card_key,
            "metrics_object_key": metrics_key,
            "storage_backend": repository.store.backend_name,
            "published_at": published_at,
        }
        model.metrics_json = metrics_payload
        published.append(
            {
                "target": target,
                "version": model.semantic_version,
                "provider": model.algorithm,
                "artifact_object_key": stored.key,
                "artifact_checksum": stored.checksum,
                "model_card_object_key": card_key,
                "active_pointer": repository.layout.active_hourly_model_pointer(
                    target
                ),
                "write_status": {
                    "model_binary": binary_status,
                    "model_card": card_status,
                    "model_metrics": metrics_status,
                    "active_pointer": "updated",
                },
            }
        )

    comparison = None
    if publish_comparison:
        comparison = export_model_comparison(
            session,
            config,
            publish=True,
        )
    session.flush()

    return {
        "status": "ok",
        "published_at": datetime.now(UTC).isoformat(),
        "targets": list(requested),
        "models": published,
        "comparison": comparison,
        "idempotent_publication": True,
        "published_content": [
            "approved_model_binary",
            "model_card",
            "model_metrics",
            "active_model_pointer",
            "model_comparison",
        ],
        "explicitly_not_published": [
            "raw_measurements",
            "sqlite_database",
            "training_snapshot",
            "training_frames",
            "source_cache",
        ],
    }
