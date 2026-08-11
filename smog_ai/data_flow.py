from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import (
    materialize_hourly_training_frames_from_store,
    materialize_training_frames_from_store,
)
from smog_ai.config import AppConfig
from smog_ai.domain import StageStats
from smog_ai.progress import ProgressReporter


@runtime_checkable
class TrainingDataBridge(Protocol):
    """Strategy side of the local/cloud training-data Bridge.

    Source collectors always persist canonical operational history in local
    SQLite. This bridge selects how model training obtains its input:

    * ``direct_local`` builds frames directly from SQLite;
    * ``object_store_roundtrip`` enforces SQLite -> ObjectStore -> local ML.

    ObjectStore is a separate Bridge and may be a local directory,
    DigitalOcean Spaces/S3, MinIO, AWS S3 or an in-memory test store.
    """

    mode: str
    training_input_source: str
    requires_operational_export: bool

    def configure_training(self, config: AppConfig) -> None:
        ...

    def prepare(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressReporter | None = None,
    ) -> StageStats:
        ...

    def describe(self, config: AppConfig) -> dict[str, object]:
        ...


@dataclass(slots=True)
class DirectLocalTrainingDataBridge:
    mode: str = "direct_local"
    training_input_source: str = "database"
    requires_operational_export: bool = False

    def configure_training(self, config: AppConfig) -> None:
        config.training.input_source = "database"

    def prepare(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressReporter | None = None,
    ) -> StageStats:
        del session
        self.configure_training(config)
        if progress is not None:
            progress.update(
                "training_data",
                1.0,
                task="direct local SQLite training source selected",
                detail={
                    "data_flow_mode": self.mode,
                    "training_input_source": self.training_input_source,
                },
                force=True,
            )
        return StageStats(
            skipped=1,
            details={
                "status": "ready",
                "data_flow_mode": self.mode,
                "training_input_source": self.training_input_source,
                "reason": (
                    "Training frames are built directly from local SQLite "
                    "by the selected trainer."
                ),
            },
        )

    def describe(self, config: AppConfig) -> dict[str, object]:
        return {
            "mode": self.mode,
            "training_input_source": self.training_input_source,
            "requires_operational_export": self.requires_operational_export,
            "mirror_operational_to_object_store": (
                config.data_flow.mirror_operational_to_object_store
            ),
            "object_store_backend": config.object_storage.backend,
            "object_store_role": (
                "optional output/mirror store; not required as training input"
            ),
        }


@dataclass(slots=True)
class ObjectStoreRoundTripTrainingDataBridge:
    mode: str = "object_store_roundtrip"
    training_input_source: str = "object_store"
    requires_operational_export: bool = True

    def configure_training(self, config: AppConfig) -> None:
        config.training.input_source = "object_store"

    def prepare(
        self,
        session: Session,
        config: AppConfig,
        *,
        progress: ProgressReporter | None = None,
    ) -> StageStats:
        self.configure_training(config)
        if config.hourly_forecasting.enabled:
            return materialize_hourly_training_frames_from_store(
                session,
                config,
                progress=progress,
            )
        return materialize_training_frames_from_store(session, config)

    def describe(self, config: AppConfig) -> dict[str, object]:
        backend = config.object_storage.backend
        return {
            "mode": self.mode,
            "training_input_source": self.training_input_source,
            "requires_operational_export": self.requires_operational_export,
            "mirror_operational_to_object_store": True,
            "object_store_backend": backend,
            "object_store_role": (
                "training round-trip through local filesystem"
                if backend == "local"
                else "training round-trip through configured object storage"
            ),
        }


def create_training_data_bridge(config: AppConfig) -> TrainingDataBridge:
    mode = config.data_flow.training_mode
    if mode == "direct_local":
        return DirectLocalTrainingDataBridge()
    if mode == "object_store_roundtrip":
        return ObjectStoreRoundTripTrainingDataBridge()
    raise ValueError(f"Unsupported data-flow training mode: {mode}")


def data_flow_status(config: AppConfig) -> dict[str, object]:
    bridge = create_training_data_bridge(config)
    return {
        "training": bridge.describe(config),
        "history_cache": {
            "mode": config.data_flow.history_cache_mode,
            "prefix": config.data_flow.history_cache_prefix,
            "local_cache_root": str(
                (config.paths.temp_dir / "gios-history-cache").resolve()
            ),
            "object_store_backend": config.object_storage.backend,
        },
        "examples": {
            "fully_local": {
                "data_flow.training_mode": "direct_local",
                "data_flow.history_cache_mode": "local",
                "object_storage.backend": "local",
            },
            "local_training_remote_outputs": {
                "data_flow.training_mode": "direct_local",
                "data_flow.history_cache_mode": "hybrid",
                "object_storage.backend": "spaces",
            },
            "course_spaces_roundtrip": {
                "data_flow.training_mode": "object_store_roundtrip",
                "data_flow.history_cache_mode": "object_store",
                "object_storage.backend": "spaces",
            },
        },
    }
