from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class ObjectStoreError(RuntimeError):
    """Base error raised by object-storage implementations."""


class ObjectNotFoundError(ObjectStoreError, FileNotFoundError):
    """Requested object does not exist."""


class ObjectConflictError(ObjectStoreError):
    """An immutable object key already contains different bytes."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None = None
    last_modified: datetime | None = None
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class ObjectStore(Protocol):
    """Implementation side of the object-storage Bridge.

    High-level artifact repositories depend only on this protocol.  Implementations
    may use a local directory, DigitalOcean Spaces, AWS S3, MinIO or an in-memory
    test store without changing model, pipeline or frontend code.
    """

    backend_name: str

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        immutable: bool = False,
    ) -> ObjectInfo:
        ...

    def get_bytes(self, key: str) -> bytes:
        ...

    def head(self, key: str) -> ObjectInfo | None:
        ...

    def exists(self, key: str) -> bool:
        ...

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        ...

    def delete(self, key: str) -> None:
        ...

    def ping(self) -> None:
        ...

    def ensure_container(self, *, create_if_missing: bool = False) -> bool:
        """Ensure the backing container exists; return True when it was created."""
        ...
