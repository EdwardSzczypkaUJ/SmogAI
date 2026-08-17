from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.storage.base import ObjectNotFoundError


@runtime_checkable
class ModelSource(Protocol):
    backend_name: str

    def active_models(self) -> list[dict[str, Any]]: ...

    def comparison(self) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class ObjectStoreModelSource:
    repository: ArtifactRepository
    targets: tuple[str, ...] = (
        "PM10",
        "PM2.5",
        "temperature_c",
        "precipitation_mm",
    )
    backend_name: str = "object-store-models"

    def _serving_manifest_models(self) -> list[dict[str, Any]]:
        """Read public model cards embedded in the Serving v2 manifest.

        The public application does not require model binaries or their object
        pointers.  The manifest contains the safe subset prepared locally.
        """

        try:
            pointer = self.repository.get_json(
                self.repository.layout.latest_spatial_pointer
            )
            manifest_key = str(pointer.get("manifest_key") or "")
            if not manifest_key:
                return []
            manifest = self.repository.get_json(manifest_key)
        except (ObjectNotFoundError, FileNotFoundError, KeyError, TypeError):
            return []
        result: list[dict[str, Any]] = []
        for raw in list(dict(manifest.get("operations") or {}).get("models") or []):
            row = dict(raw or {})
            target = str(row.get("parameter") or "")
            if not target:
                continue
            quality_status = row.get("quality_status") or "accepted"
            metrics = dict(row.get("metrics") or {})
            metrics["quality_status"] = quality_status
            result.append(
                {
                    "target": target,
                    "model_version": row.get("version"),
                    "provider": row.get("algorithm"),
                    "activated_at": row.get("activated_at"),
                    "training_data_start": row.get("training_data_start"),
                    "training_data_end": row.get("training_data_end"),
                    "training_profile": row.get("training_profile"),
                    "candidate_scores": dict(row.get("candidate_scores") or {}),
                    "model_age_hours_at_publication": row.get(
                        "model_age_hours_at_publication"
                    ),
                    "last_evaluated_at": row.get("last_evaluated_at"),
                    "evaluation_age_hours_at_publication": row.get(
                        "evaluation_age_hours_at_publication"
                    ),
                    "freshness_threshold_hours": row.get(
                        "freshness_threshold_hours"
                    ),
                    "freshness_status": row.get("freshness_status") or "unknown",
                    "forecast_mode": "horizon-conditioned-hourly",
                    "source": "serving_v2_manifest",
                    "card": {
                        "target": target,
                        "provider": row.get("algorithm"),
                        "model_version": row.get("version"),
                        "activated_at": row.get("activated_at"),
                        "training_data_start": row.get("training_data_start"),
                        "training_data_end": row.get("training_data_end"),
                        "training_profile": row.get("training_profile"),
                        "metrics": metrics,
                    },
                }
            )
        return result

    def active_models(self) -> list[dict[str, Any]]:
        manifest_models = self._serving_manifest_models()
        if manifest_models:
            return manifest_models
        result: list[dict[str, Any]] = []
        for target in self.targets:
            pointer_key = self.repository.layout.active_hourly_model_pointer(target)
            try:
                pointer = self.repository.get_json(pointer_key)
            except (ObjectNotFoundError, FileNotFoundError):
                continue
            if not isinstance(pointer, dict):
                continue
            card: dict[str, Any] | None = None
            card_key = pointer.get("model_card_object_key")
            if card_key:
                try:
                    loaded = self.repository.get_json(str(card_key))
                    card = loaded if isinstance(loaded, dict) else None
                except Exception:
                    card = None
            result.append(
                {
                    "target": target,
                    "pointer_key": pointer_key,
                    "model_version": pointer.get("model_version"),
                    "provider": pointer.get("provider"),
                    "activated_at": pointer.get("activated_at"),
                    "artifact_object_key": pointer.get("artifact_object_key"),
                    "artifact_checksum": pointer.get("artifact_checksum"),
                    "model_card_object_key": card_key,
                    "forecast_mode": pointer.get(
                        "forecast_mode", "horizon-conditioned-hourly"
                    ),
                    "card": card,
                }
            )
        return result

    def comparison(self) -> dict[str, Any] | None:
        try:
            payload = self.repository.get_json(
                self.repository.layout.model_comparison_pointer
            )
        except (ObjectNotFoundError, FileNotFoundError):
            return None
        return payload if isinstance(payload, dict) else None




@dataclass(slots=True)
class StaticModelSource:
    models: list[dict[str, Any]]
    comparison_payload: dict[str, Any] | None = None
    backend_name: str = "static-models"

    def active_models(self) -> list[dict[str, Any]]:
        return list(self.models)

    def comparison(self) -> dict[str, Any] | None:
        return dict(self.comparison_payload) if self.comparison_payload else None
