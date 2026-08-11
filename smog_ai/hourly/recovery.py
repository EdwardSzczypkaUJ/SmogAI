from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import joblib
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.database.models import ModelVersion
from smog_ai.domain import StageStats
from smog_ai.hourly.trainer import HOURLY_MODEL_HORIZON_SENTINEL
from smog_ai.storage.base import ObjectNotFoundError
from smog_ai.storage.keys import sanitize_component

logger = logging.getLogger(__name__)

CandidateSource = Literal["active_pointer", "remote_version", "local_file", "database"]
_VERSION_RE = re.compile(r"/version=([^/]+)/")


@dataclass(slots=True)
class RecoveryCandidate:
    target: str
    source: CandidateSource
    version: str
    provider: str | None = None
    local_path: str | None = None
    artifact_key: str | None = None
    card_key: str | None = None
    metrics_key: str | None = None
    checksum: str | None = None
    timestamp: datetime | None = None
    bootstrap: bool = False
    valid: bool = False
    repairable: bool = False
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.astimezone(UTC).isoformat() if self.timestamp else None
        return payload


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".sai-rec-{hashlib.sha256(str(path).encode()).hexdigest()[:10]}.tmp")
    temporary.write_bytes(body)
    temporary.replace(path)


def _load_joblib_bytes(body: bytes) -> dict[str, Any]:
    payload = joblib.load(BytesIO(body))
    if not isinstance(payload, dict):
        raise TypeError("Hourly model artifact must be a dictionary")
    return payload


def _load_joblib_file(path: Path) -> dict[str, Any]:
    payload = joblib.load(path)
    if not isinstance(payload, dict):
        raise TypeError(f"Hourly model artifact must be a dictionary: {path}")
    return payload


def _validate_artifact(artifact: dict[str, Any], target: str) -> list[str]:
    errors: list[str] = []
    if str(artifact.get("forecast_mode") or "") != "horizon-conditioned-hourly":
        errors.append("forecast_mode is not horizon-conditioned-hourly")
    if str(artifact.get("target") or "") != target:
        errors.append(f"target mismatch: {artifact.get('target')!r}")
    if not str(artifact.get("provider") or "").strip():
        errors.append("provider is missing")
    if not isinstance(artifact.get("provider_artifact"), dict):
        errors.append("provider_artifact is missing or invalid")
    features = artifact.get("feature_columns")
    if not isinstance(features, list) or not features:
        errors.append("feature_columns are missing")
    # Older HF7 recovery fixtures and bootstrap artifacts may omit this field.
    # The configured h1..h48 contract is restored in the model card/pointer.
    return errors


def _artifact_timestamp(artifact: dict[str, Any], fallback: datetime | None = None) -> datetime | None:
    return (
        _parse_datetime(artifact.get("trained_at"))
        or _parse_datetime((artifact.get("metadata") or {}).get("trained_at"))
        or fallback
    )


def _is_bootstrap(artifact: dict[str, Any]) -> bool:
    metadata = artifact.get("metadata") or {}
    return bool(metadata.get("bootstrap"))


def _candidate_sort_key(candidate: RecoveryCandidate) -> tuple[int, float, int]:
    # Prefer a valid non-bootstrap artifact, then the most recent timestamp.
    source_priority = {
        "active_pointer": 4,
        "remote_version": 3,
        "local_file": 2,
        "database": 1,
    }.get(candidate.source, 0)
    timestamp = candidate.timestamp.timestamp() if candidate.timestamp else 0.0
    quality = 2 if candidate.valid and not candidate.bootstrap else 1 if candidate.valid else 0
    return quality, timestamp, source_priority


def _active_pointer_candidate(repository, target: str) -> RecoveryCandidate | None:  # type: ignore[no-untyped-def]
    pointer_key = repository.layout.active_hourly_model_pointer(target)
    try:
        pointer = repository.get_json(pointer_key)
    except ObjectNotFoundError:
        return None
    except Exception as exc:
        return RecoveryCandidate(
            target=target,
            source="active_pointer",
            version="",
            errors=[f"active pointer read failed: {exc}"],
        )

    candidate = RecoveryCandidate(
        target=target,
        source="active_pointer",
        version=str(pointer.get("model_version") or "").strip(),
        provider=str(pointer.get("provider") or "").strip() or None,
        artifact_key=str(pointer.get("artifact_object_key") or "").strip() or None,
        card_key=str(pointer.get("model_card_object_key") or "").strip() or None,
        metrics_key=str(pointer.get("metrics_object_key") or "").strip() or None,
        checksum=str(pointer.get("artifact_checksum") or "").strip() or None,
        timestamp=_parse_datetime(pointer.get("activated_at")),
        metadata={"pointer_key": pointer_key, "pointer": pointer},
    )
    if str(pointer.get("target") or "") != target:
        candidate.errors.append(f"pointer target mismatch: {pointer.get('target')!r}")
    if not candidate.version:
        candidate.errors.append("model_version missing")
    if not candidate.provider:
        candidate.errors.append("provider missing")
    if not candidate.artifact_key:
        candidate.errors.append("artifact_object_key missing")
    if not candidate.card_key:
        candidate.errors.append("model_card_object_key missing")
    if candidate.artifact_key and repository.store.head(candidate.artifact_key) is None:
        candidate.errors.append(f"artifact missing: {candidate.artifact_key}")
    if candidate.card_key and repository.store.head(candidate.card_key) is None:
        candidate.errors.append(f"model card missing: {candidate.card_key}")
    candidate.repairable = bool(candidate.artifact_key and repository.store.head(candidate.artifact_key))
    candidate.valid = not candidate.errors
    return candidate


def _remote_version_candidates(repository, target: str) -> list[RecoveryCandidate]:  # type: ignore[no-untyped-def]
    prefix = f"models-hourly/target={sanitize_component(target)}/"
    objects = repository.store.list(prefix)
    grouped: dict[str, dict[str, Any]] = {}
    for info in objects:
        match = _VERSION_RE.search(f"/{info.key}")
        if not match:
            continue
        version = match.group(1)
        row = grouped.setdefault(version, {"infos": [], "last_modified": None})
        row["infos"].append(info)
        if info.last_modified and (
            row["last_modified"] is None or info.last_modified > row["last_modified"]
        ):
            row["last_modified"] = info.last_modified

    candidates: list[RecoveryCandidate] = []
    for version, row in grouped.items():
        keys = {info.key for info in row["infos"]}
        artifact_key = repository.layout.hourly_model_binary(target, version)
        card_key = repository.layout.hourly_model_card(target, version)
        metrics_key = repository.layout.hourly_model_metrics(target, version)
        candidate = RecoveryCandidate(
            target=target,
            source="remote_version",
            version=version,
            artifact_key=artifact_key if artifact_key in keys else None,
            card_key=card_key if card_key in keys else None,
            metrics_key=metrics_key if metrics_key in keys else None,
            timestamp=row["last_modified"],
            metadata={"object_keys": sorted(keys)},
        )
        card: dict[str, Any] = {}
        if candidate.card_key:
            try:
                raw_card = repository.get_json(candidate.card_key)
                if isinstance(raw_card, dict):
                    card = raw_card
            except Exception as exc:
                candidate.errors.append(f"model card read failed: {exc}")
        candidate.provider = str(card.get("provider") or "").strip() or None
        candidate.timestamp = (
            _parse_datetime(card.get("created_at"))
            or _parse_datetime(card.get("training_data_end"))
            or candidate.timestamp
        )
        candidate.bootstrap = bool((card.get("metrics") or {}).get("bootstrap"))
        if not candidate.artifact_key:
            candidate.errors.append("model.joblib missing")
        if candidate.card_key and str(card.get("target") or target) != target:
            candidate.errors.append(f"model card target mismatch: {card.get('target')!r}")
        candidate.repairable = bool(candidate.artifact_key)
        # A missing card can be reconstructed by loading the model binary.
        candidate.valid = bool(candidate.artifact_key and not candidate.errors)
        candidates.append(candidate)
    return candidates


def _local_candidates(config: AppConfig, target: str) -> list[RecoveryCandidate]:
    directory = config.paths.models_dir / "hourly" / target.replace(".", "_")
    if not directory.exists():
        return []
    candidates: list[RecoveryCandidate] = []
    for path in sorted(directory.glob("*.joblib")):
        candidate = RecoveryCandidate(
            target=target,
            source="local_file",
            version=path.stem,
            local_path=str(path),
            timestamp=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        )
        try:
            artifact = _load_joblib_file(path)
            candidate.provider = str(artifact.get("provider") or "").strip() or None
            candidate.timestamp = _artifact_timestamp(artifact, candidate.timestamp)
            candidate.bootstrap = _is_bootstrap(artifact)
            candidate.errors.extend(_validate_artifact(artifact, target))
            candidate.metadata = {
                "trained_at": artifact.get("trained_at"),
                "trained_rows": artifact.get("trained_rows"),
                "feature_count": len(artifact.get("feature_columns") or []),
                "horizon_count": len(artifact.get("horizons_hours") or []),
            }
            sidecar = path.with_suffix(".recovery.json")
            if sidecar.exists():
                try:
                    import json

                    recovery_metadata = json.loads(sidecar.read_text(encoding="utf-8-sig"))
                    if isinstance(recovery_metadata, dict):
                        candidate.metadata["recovery_manifest"] = recovery_metadata
                        candidate.timestamp = (
                            _parse_datetime(recovery_metadata.get("created_at"))
                            or candidate.timestamp
                        )
                except Exception as exc:
                    candidate.metadata["recovery_manifest_error"] = str(exc)
        except Exception as exc:
            candidate.errors.append(f"joblib load failed: {exc}")
        candidate.repairable = not candidate.errors
        candidate.valid = not candidate.errors
        candidates.append(candidate)
    return candidates


def _database_candidates(session: Session, target: str) -> list[RecoveryCandidate]:
    rows = session.scalars(
        select(ModelVersion)
        .where(
            ModelVersion.parameter == target,
            ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
        )
        .order_by(ModelVersion.activated_at.desc(), ModelVersion.created_at.desc())
    ).all()
    candidates: list[RecoveryCandidate] = []
    for row in rows:
        path = Path(row.artifact_path) if row.artifact_path else None
        candidate = RecoveryCandidate(
            target=target,
            source="database",
            version=row.semantic_version,
            provider=row.algorithm,
            local_path=str(path) if path else None,
            timestamp=row.activated_at or row.created_at,
            bootstrap=bool((row.metrics_json or {}).get("bootstrap")),
            metadata={"active": row.active, "model_version_id": row.id},
        )
        if path is None or not path.exists():
            candidate.errors.append("database artifact_path missing or file does not exist")
        else:
            try:
                artifact = _load_joblib_file(path)
                candidate.errors.extend(_validate_artifact(artifact, target))
            except Exception as exc:
                candidate.errors.append(f"joblib load failed: {exc}")
        candidate.repairable = not candidate.errors
        candidate.valid = not candidate.errors
        candidates.append(candidate)
    return candidates


def audit_hourly_model_artifacts(session: Session, config: AppConfig) -> dict[str, Any]:
    repository = create_artifact_repository(config) if config.object_storage.enabled else None
    if repository is not None:
        repository.ping()

    targets: dict[str, Any] = {}
    all_recoverable = True
    for target in config.hourly_forecasting.targets:
        candidates: list[RecoveryCandidate] = []
        if repository is not None:
            active = _active_pointer_candidate(repository, target)
            if active is not None:
                candidates.append(active)
            candidates.extend(_remote_version_candidates(repository, target))
        candidates.extend(_local_candidates(config, target))
        candidates.extend(_database_candidates(session, target))

        candidates.sort(key=_candidate_sort_key, reverse=True)
        recoverable = [candidate for candidate in candidates if candidate.repairable]
        selected = recoverable[0] if recoverable else None
        all_recoverable = all_recoverable and selected is not None
        targets[target] = {
            "recoverable": selected is not None,
            "selected": selected.as_dict() if selected else None,
            "candidate_count": len(candidates),
            "candidates": [candidate.as_dict() for candidate in candidates],
            "local_directory": str(
                config.paths.models_dir / "hourly" / target.replace(".", "_")
            ),
            "active_pointer_key": (
                repository.layout.active_hourly_model_pointer(target)
                if repository is not None
                else None
            ),
        }

    progress_path = config.paths.logs_dir / "progress" / "first-run-current.json"
    progress: dict[str, Any] | None = None
    if progress_path.exists():
        try:
            import json

            loaded = json.loads(progress_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                progress = loaded
        except Exception as exc:
            progress = {"read_error": str(exc), "path": str(progress_path)}

    return {
        "status": "recoverable" if all_recoverable else "incomplete",
        "all_targets_recoverable": all_recoverable,
        "required_targets": list(config.hourly_forecasting.targets),
        "models_dir": str(config.paths.models_dir),
        "storage_backend": repository.store.backend_name if repository is not None else "disabled",
        "storage_prefix": config.object_storage.prefix if config.object_storage.enabled else None,
        "targets": targets,
        "last_first_run_progress": progress,
    }


def _select_candidate(audit: dict[str, Any], target: str) -> RecoveryCandidate | None:
    selected = (audit.get("targets") or {}).get(target, {}).get("selected")
    if not isinstance(selected, dict):
        return None
    timestamp = _parse_datetime(selected.get("timestamp"))
    return RecoveryCandidate(
        target=target,
        source=selected["source"],
        version=str(selected.get("version") or ""),
        provider=selected.get("provider"),
        local_path=selected.get("local_path"),
        artifact_key=selected.get("artifact_key"),
        card_key=selected.get("card_key"),
        metrics_key=selected.get("metrics_key"),
        checksum=selected.get("checksum"),
        timestamp=timestamp,
        bootstrap=bool(selected.get("bootstrap")),
        valid=bool(selected.get("valid")),
        repairable=bool(selected.get("repairable")),
        errors=list(selected.get("errors") or []),
        metadata=dict(selected.get("metadata") or {}),
    )


def _artifact_and_metadata(repository, candidate: RecoveryCandidate) -> tuple[dict[str, Any], bytes, dict[str, Any]]:  # type: ignore[no-untyped-def]
    card: dict[str, Any] = {}
    if candidate.source in {"active_pointer", "remote_version"} and candidate.artifact_key:
        body = repository.store.get_bytes(candidate.artifact_key)
        artifact = _load_joblib_bytes(body)
        if candidate.card_key:
            try:
                loaded_card = repository.get_json(candidate.card_key)
                if isinstance(loaded_card, dict):
                    card = loaded_card
            except ObjectNotFoundError:
                pass
        return artifact, body, card
    if candidate.local_path:
        path = Path(candidate.local_path)
        body = path.read_bytes()
        artifact = _load_joblib_file(path)
        return artifact, body, card
    raise RuntimeError(f"Candidate has no readable artifact: {candidate.as_dict()}")


def _register_recovered_model(
    session: Session,
    config: AppConfig,
    *,
    target: str,
    candidate: RecoveryCandidate,
    repository,
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    artifact, original_body, card = _artifact_and_metadata(repository, candidate)
    validation_errors = _validate_artifact(artifact, target)
    if validation_errors:
        raise RuntimeError(
            f"Artifact validation failed target={target}: " + "; ".join(validation_errors)
        )

    provider = str(artifact.get("provider") or candidate.provider or "").strip()
    version = str(candidate.version or "").strip()
    if not version:
        raise RuntimeError(f"Recovered artifact has no semantic version target={target}")

    local_path = (
        config.paths.models_dir
        / "hourly"
        / target.replace(".", "_")
        / f"{version}.joblib"
    )
    if not local_path.exists() or local_path.read_bytes() != original_body:
        _atomic_write(local_path, original_body)

    artifact_key = candidate.artifact_key
    checksum = hashlib.sha256(original_body).hexdigest()
    if not artifact_key or repository.store.head(artifact_key) is None:
        stored = repository.put_joblib(
            repository.layout.hourly_model_binary(target, version),
            artifact,
            immutable=True,
            metadata={
                "target": target,
                "model-version": version,
                "provider": provider,
                "forecast-mode": "horizon-conditioned-hourly",
                "recovered-from": candidate.source,
            },
        )
        artifact_key = stored.key
        checksum = stored.checksum
    else:
        remote_body = repository.store.get_bytes(artifact_key)
        checksum = hashlib.sha256(remote_body).hexdigest()

    recovery_manifest = candidate.metadata.get("recovery_manifest") or {}
    metrics = dict(card.get("metrics") or recovery_manifest.get("metrics") or {})
    metrics.update(
        {
            "recovered": True,
            "recovered_from": candidate.source,
            "recovered_from_object_storage": candidate.source in {"active_pointer", "remote_version"},
            "recovered_at": datetime.now(UTC).isoformat(),
            "metrics_complete": bool(card.get("metrics")),
            "bootstrap": bool((artifact.get("metadata") or {}).get("bootstrap")),
        }
    )
    data_start = _parse_datetime(
        card.get("training_data_start") or recovery_manifest.get("training_data_start")
    )
    data_end = _parse_datetime(
        card.get("training_data_end") or recovery_manifest.get("training_data_end")
    )
    created_at = (
        _artifact_timestamp(artifact, candidate.timestamp)
        or _parse_datetime(recovery_manifest.get("created_at"))
        or datetime.now(UTC)
    )

    card_payload = {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "model_version": version,
        "target": target,
        "provider": provider,
        "feature_columns": list(artifact.get("feature_columns") or []),
        "horizons_hours": list(artifact.get("horizons_hours") or []),
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
        "created_at": created_at.isoformat(),
        "source_host_id": config.source_host_id,
        "artifact": {
            "object_key": artifact_key,
            "checksum": checksum,
            "size": repository.store.head(artifact_key).size if repository.store.head(artifact_key) else len(original_body),
            "storage_backend": repository.store.backend_name,
        },
        "recovery": {
            "source": candidate.source,
            "original_local_path": candidate.local_path,
            "original_card_key": candidate.card_key,
        },
    }
    card_key = repository.layout.hourly_model_card(target, version)
    if repository.store.head(card_key) is None:
        repository.put_json(card_key, card_payload, immutable=True)
    else:
        # Preserve immutable metadata produced by the original training run.
        try:
            existing_card = repository.get_json(card_key)
            if isinstance(existing_card, dict):
                card_payload = existing_card
        except Exception:
            logger.warning("Could not read existing model card %s; keeping object unchanged", card_key)

    metrics_key = repository.layout.hourly_model_metrics(target, version)
    if repository.store.head(metrics_key) is None:
        repository.put_json(metrics_key, card_payload, immutable=True)

    existing = session.scalar(
        select(ModelVersion).where(
            ModelVersion.parameter == target,
            ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
            ModelVersion.semantic_version == version,
        )
    )
    session.execute(
        update(ModelVersion)
        .where(
            ModelVersion.parameter == target,
            ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
        )
        .values(active=False)
    )
    remote_artifact = {
        "artifact_object_key": artifact_key,
        "artifact_checksum": checksum,
        "model_card_object_key": card_key,
        "metrics_object_key": metrics_key,
        "storage_backend": repository.store.backend_name,
    }
    metrics["remote_artifact"] = remote_artifact
    if existing is None:
        existing = ModelVersion(
            model_name=f"hourly-{target}-{provider}",
            algorithm=provider,
            parameter=target,
            forecast_horizon=HOURLY_MODEL_HORIZON_SENTINEL,
            semantic_version=version,
        )
        session.add(existing)
    existing.model_name = f"hourly-{target}-{provider}"
    existing.algorithm = provider
    existing.artifact_path = str(local_path)
    existing.feature_columns_json = list(artifact.get("feature_columns") or [])
    existing.metrics_json = metrics
    existing.training_data_start = data_start
    existing.training_data_end = data_end
    existing.active = True
    existing.activated_at = datetime.now(UTC)
    session.flush()

    pointer_key = repository.layout.active_hourly_model_pointer(target)
    pointer = {
        "schema_version": "2.0",
        "forecast_mode": "horizon-conditioned-hourly",
        "target": target,
        "model_version": version,
        "provider": provider,
        "artifact_object_key": artifact_key,
        "artifact_checksum": checksum,
        "model_card_object_key": card_key,
        "metrics_object_key": metrics_key,
        "activated_at": existing.activated_at.isoformat(),
        "source_host_id": config.source_host_id,
        "recovered_from": candidate.source,
    }
    repository.put_json(pointer_key, pointer, immutable=False)

    return {
        "target": target,
        "provider": provider,
        "model_version": version,
        "artifact_path": str(local_path),
        "artifact_object_key": artifact_key,
        "artifact_checksum": checksum,
        "model_card_object_key": card_key,
        "metrics_object_key": metrics_key,
        "pointer_key": pointer_key,
        "recovered_from": candidate.source,
        "bootstrap": bool((artifact.get("metadata") or {}).get("bootstrap")),
    }


def recover_hourly_models_from_available_artifacts(
    session: Session,
    config: AppConfig,
) -> StageStats:
    """Recover active model rows from pointers, versioned objects or local joblib files.

    Recovery order is based on artifact validity, non-bootstrap status and timestamp.
    This makes a documentation-stage rollback recoverable even when the active pointer
    was never written because the model upload or transaction ended early.
    """

    if not config.object_storage.enabled:
        return StageStats(errors=1, details={"reason": "object_storage_disabled"})

    repository = create_artifact_repository(config)
    repository.ping()
    audit = audit_hourly_model_artifacts(session, config)
    recovered: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for target in config.hourly_forecasting.targets:
        candidate = _select_candidate(audit, target)
        if candidate is None:
            missing.append(
                {
                    "target": target,
                    "reason": "no recoverable active pointer, versioned object or local joblib artifact",
                    "local_directory": audit["targets"][target]["local_directory"],
                    "active_pointer_key": audit["targets"][target]["active_pointer_key"],
                    "candidate_count": audit["targets"][target]["candidate_count"],
                }
            )
            continue
        try:
            recovered.append(
                _register_recovered_model(
                    session,
                    config,
                    target=target,
                    candidate=candidate,
                    repository=repository,
                )
            )
        except Exception as exc:
            logger.exception("Hourly model recovery failed target=%s", target)
            missing.append(
                {
                    "target": target,
                    "reason": str(exc),
                    "selected_candidate": candidate.as_dict(),
                }
            )

    return StageStats(
        inserted=len(recovered),
        errors=len(missing),
        details={
            "status": "complete" if not missing else "incomplete",
            "recovered": recovered,
            "missing": missing,
            "required_targets": list(config.hourly_forecasting.targets),
            "storage_backend": repository.store.backend_name,
            "storage_prefix": config.object_storage.prefix,
            "audit": audit,
        },
    )


# Backward-compatible public name used by HF7 CLI and scripts.
def recover_hourly_models_from_object_store(
    session: Session,
    config: AppConfig,
) -> StageStats:
    return recover_hourly_models_from_available_artifacts(session, config)
