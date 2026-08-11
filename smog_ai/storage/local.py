from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from smog_ai.storage.base import (
    ObjectConflictError,
    ObjectInfo,
    ObjectNotFoundError,
)
from smog_ai.storage.keys import normalize_key


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalObjectStore:
    """Filesystem implementation with atomic replacement and traversal protection."""

    backend_name = "local"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path:
        normalized = normalize_key(key)
        candidate = (self.root / Path(*normalized.split("/"))).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise ValueError(f"Object key escapes storage root: {key!r}")
        return candidate

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        immutable: bool = False,
    ) -> ObjectInfo:
        path = self._path(key)
        checksum = _sha256(data)
        with self._lock:
            if path.exists():
                existing = path.read_bytes()
                if immutable and existing != data:
                    raise ObjectConflictError(f"Immutable object already exists with different content: {key}")
                if existing == data:
                    stat = path.stat()
                    return ObjectInfo(
                        key=normalize_key(key),
                        size=stat.st_size,
                        etag=checksum,
                        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                        content_type=content_type,
                        metadata=dict(metadata or {}),
                    )
            path.parent.mkdir(parents=True, exist_ok=True)

            # Keep the temporary filename deliberately short.  Repeating the
            # complete target name in the prefix can push an otherwise valid
            # Windows path beyond the classic MAX_PATH limit (260 characters).
            # The file remains in the destination directory, so os.replace()
            # is still atomic on the same filesystem.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".sai-",
                suffix=".tmp",
                dir=str(path.parent),
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_path.replace(path)
            finally:
                temporary_path.unlink(missing_ok=True)
        stat = path.stat()
        return ObjectInfo(
            key=normalize_key(key),
            size=stat.st_size,
            etag=checksum,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            content_type=content_type,
            metadata=dict(metadata or {}),
        )

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.exists() or not path.is_file():
            return None
        data = path.read_bytes()
        stat = path.stat()
        return ObjectInfo(
            key=normalize_key(key),
            size=stat.st_size,
            etag=_sha256(data),
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        normalized_prefix = prefix.replace("\\", "/").strip("/")
        rows: list[ObjectInfo] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            key = path.relative_to(self.root).as_posix()
            if normalized_prefix and not key.startswith(normalized_prefix):
                continue
            info = self.head(key)
            if info is not None:
                rows.append(info)
        return sorted(rows, key=lambda item: item.key)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def ping(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise RuntimeError(f"Object-store root is not a directory: {self.root}")

    def ensure_container(self, *, create_if_missing: bool = False) -> bool:
        self.ping()
        return False


class MemoryObjectStore:
    """Deterministic in-memory implementation for unit tests and demos."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str, dict[str, str], datetime]] = {}
        self._lock = threading.RLock()

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        immutable: bool = False,
    ) -> ObjectInfo:
        normalized = normalize_key(key)
        with self._lock:
            existing = self._objects.get(normalized)
            if immutable and existing is not None and existing[0] != data:
                raise ObjectConflictError(f"Immutable object already exists with different content: {normalized}")
            timestamp = existing[3] if existing and existing[0] == data else datetime.now(UTC)
            self._objects[normalized] = (bytes(data), content_type, dict(metadata or {}), timestamp)
        return ObjectInfo(normalized, len(data), _sha256(data), timestamp, content_type, dict(metadata or {}))

    def get_bytes(self, key: str) -> bytes:
        normalized = normalize_key(key)
        try:
            return self._objects[normalized][0]
        except KeyError as exc:
            raise ObjectNotFoundError(normalized) from exc

    def head(self, key: str) -> ObjectInfo | None:
        normalized = normalize_key(key)
        row = self._objects.get(normalized)
        if row is None:
            return None
        data, content_type, metadata, timestamp = row
        return ObjectInfo(normalized, len(data), _sha256(data), timestamp, content_type, dict(metadata))

    def exists(self, key: str) -> bool:
        return normalize_key(key) in self._objects

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        clean = prefix.replace("\\", "/").strip("/")
        return [
            info
            for key in sorted(self._objects)
            if (not clean or key.startswith(clean)) and (info := self.head(key)) is not None
        ]

    def delete(self, key: str) -> None:
        self._objects.pop(normalize_key(key), None)

    def ping(self) -> None:
        return None

    def ensure_container(self, *, create_if_missing: bool = False) -> bool:
        return False
