from __future__ import annotations

from typing import Any, Protocol

from server.database.store import SnapshotStoreProtocol
from smog_ai.artifacts.repository import ArtifactRepository


class SnapshotSource(Protocol):
    backend_name: str

    def latest(self) -> dict[str, Any] | None:
        ...

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        ...

    def ping(self) -> None:
        ...


class SnapshotStoreSource:
    """Adapter exposing any server SnapshotStore through the query-side port."""

    def __init__(self, store: SnapshotStoreProtocol) -> None:
        self.store = store
        self.backend_name = store.backend_name

    def latest(self) -> dict[str, Any] | None:
        return self.store.latest_payload()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.history(limit=limit)

    def ping(self) -> None:
        self.store.ping()


class ObjectStoreSnapshotSource:
    backend_name = "object_store"

    def __init__(self, repository: ArtifactRepository) -> None:
        self.repository = repository

    def latest(self) -> dict[str, Any] | None:
        return self.repository.latest_snapshot_payload()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.repository.snapshot_history(limit)

    def ping(self) -> None:
        self.repository.ping()


class StaticSnapshotSource:
    backend_name = "static"

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload

    def latest(self) -> dict[str, Any] | None:
        return self.payload

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.payload is None:
            return []
        return [{"metadata": self.payload.get("metadata", {})}]

    def ping(self) -> None:
        return None
