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

    def active_models(self) -> list[dict[str, Any]]:
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
