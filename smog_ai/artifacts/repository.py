from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from smog_ai.artifacts.layout import ArtifactLayout
from smog_ai.storage.base import ObjectNotFoundError, ObjectStore


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    key: str
    checksum: str
    size: int
    metadata: dict[str, Any]


class ArtifactRepository:
    """Abstraction side of the object-storage Bridge.

    It knows artifact semantics and key layout, but it does not know whether bytes
    are kept in DigitalOcean Spaces, AWS S3, MinIO or a local directory.
    """

    def __init__(self, store: ObjectStore, layout: ArtifactLayout | None = None) -> None:
        self.store = store
        self.layout = layout or ArtifactLayout()

    def ping(self) -> None:
        self.store.ping()

    def put_json(
        self,
        key: str,
        payload: Any,
        *,
        immutable: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> StoredArtifact:
        body = canonical_json_bytes(payload)
        info = self.store.put_bytes(
            key,
            body,
            content_type="application/json; charset=utf-8",
            metadata=metadata,
            immutable=immutable,
        )
        return StoredArtifact(info.key, sha256_bytes(body), info.size, dict(payload) if isinstance(payload, dict) else {})

    def get_json(self, key: str) -> Any:
        return json.loads(self.store.get_bytes(key).decode("utf-8"))

    def put_gzip_json(
        self,
        key: str,
        payload: Any,
        *,
        immutable: bool = False,
        metadata: dict[str, str] | None = None,
        compresslevel: int = 6,
    ) -> StoredArtifact:
        body = gzip.compress(canonical_json_bytes(payload), compresslevel=compresslevel, mtime=0)
        checksum = sha256_bytes(body)
        object_metadata = dict(metadata or {})
        object_metadata.setdefault("sha256", checksum)
        info = self.store.put_bytes(
            key,
            body,
            content_type="application/gzip",
            metadata=object_metadata,
            immutable=immutable,
        )
        return StoredArtifact(info.key, checksum, info.size, dict(payload) if isinstance(payload, dict) else {})

    def get_gzip_json(self, key: str) -> Any:
        return json.loads(gzip.decompress(self.store.get_bytes(key)).decode("utf-8"))

    def put_dataframe_csv_gzip(
        self,
        key: str,
        frame: pd.DataFrame,
        *,
        immutable: bool = True,
        metadata: dict[str, str] | None = None,
    ) -> StoredArtifact:
        raw = frame.to_csv(index=False, lineterminator="\n", date_format="%Y-%m-%dT%H:%M:%S.%f%z").encode("utf-8")
        body = gzip.compress(raw, compresslevel=6, mtime=0)
        checksum = sha256_bytes(body)
        object_metadata = dict(metadata or {})
        object_metadata.update({"sha256": checksum, "rows": str(len(frame))})
        info = self.store.put_bytes(
            key,
            body,
            content_type="application/gzip",
            metadata=object_metadata,
            immutable=immutable,
        )
        return StoredArtifact(info.key, checksum, info.size, {"rows": len(frame), "columns": list(frame.columns)})

    def get_dataframe_csv_gzip(self, key: str) -> pd.DataFrame:
        raw = gzip.decompress(self.store.get_bytes(key))
        frame = pd.read_csv(BytesIO(raw))
        for column in ("measurement_time", "target_time"):
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
        return frame

    def put_joblib(
        self,
        key: str,
        artifact: Any,
        *,
        immutable: bool = True,
        metadata: dict[str, str] | None = None,
    ) -> StoredArtifact:
        buffer = BytesIO()
        joblib.dump(artifact, buffer, compress=3)
        body = buffer.getvalue()
        checksum = sha256_bytes(body)
        object_metadata = dict(metadata or {})
        object_metadata.setdefault("sha256", checksum)
        info = self.store.put_bytes(
            key,
            body,
            content_type="application/octet-stream",
            metadata=object_metadata,
            immutable=immutable,
        )
        return StoredArtifact(info.key, checksum, info.size, {})

    def get_joblib(self, key: str) -> Any:
        return joblib.load(BytesIO(self.store.get_bytes(key)))

    def publish_snapshot(
        self,
        *,
        compressed: bytes,
        publication_id: str,
        checksum: str,
        metadata: dict[str, Any],
    ) -> StoredArtifact:
        key = self.layout.forecast_snapshot(publication_id)
        actual_checksum = sha256_bytes(compressed)
        # The snapshot's domain checksum is over canonical decompressed JSON.  Keep
        # both checksums so transport corruption and domain consistency are visible.
        info = self.store.put_bytes(
            key,
            compressed,
            content_type="application/gzip",
            metadata={
                "transport-sha256": actual_checksum,
                "payload-checksum": checksum,
                "publication-id": publication_id,
            },
            immutable=True,
        )
        pointer = {
            "schema_version": self.layout.schema_version,
            "publication_id": publication_id,
            "object_key": key,
            "payload_checksum": checksum,
            "transport_sha256": actual_checksum,
            "updated_at": datetime.now(UTC).isoformat(),
            "metadata": metadata,
        }
        self.put_json(self.layout.latest_forecast_pointer, pointer, immutable=False)
        self.put_json(
            self.layout.publication_audit(publication_id),
            pointer,
            immutable=True,
        )
        return StoredArtifact(info.key, actual_checksum, info.size, pointer)

    def latest_snapshot_bytes(self) -> bytes:
        pointer = self.get_json(self.layout.latest_forecast_pointer)
        return self.store.get_bytes(str(pointer["object_key"]))

    def latest_snapshot_payload(self) -> dict[str, Any] | None:
        try:
            return json.loads(gzip.decompress(self.latest_snapshot_bytes()).decode("utf-8"))
        except ObjectNotFoundError:
            return None

    def snapshot_history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for info in sorted(
            self.store.list("forecasts/audit/"),
            key=lambda item: item.last_modified or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )[: max(1, min(limit, 200))]:
            try:
                rows.append(self.get_json(info.key))
            except Exception:
                continue
        return rows

    def upload_local_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
        immutable: bool = True,
        metadata: dict[str, str] | None = None,
    ) -> StoredArtifact:
        body = path.read_bytes()
        checksum = sha256_bytes(body)
        info = self.store.put_bytes(
            key,
            body,
            content_type=content_type,
            metadata={**(metadata or {}), "sha256": checksum},
            immutable=immutable,
        )
        return StoredArtifact(info.key, checksum, info.size, {})
