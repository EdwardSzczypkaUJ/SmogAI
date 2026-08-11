from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from smog_ai.storage.base import (
    ObjectConflictError,
    ObjectInfo,
    ObjectNotFoundError,
)
from smog_ai.storage.keys import join_key, normalize_key


class S3ObjectStore:
    """S3-compatible implementation suitable for DigitalOcean Spaces, S3 and MinIO."""

    backend_name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str | None,
        access_key: str | None,
        secret_key: str | None,
        session_token: str | None = None,
        prefix: str = "",
        verify_tls: bool = True,
        addressing_style: str = "virtual",
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
        max_attempts: int = 5,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        self.bucket = bucket
        self.prefix = prefix.replace("\\", "/").strip("/")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - dependency contract
                raise RuntimeError("Install the 'spaces' extra to use an S3-compatible store") from exc
            config = Config(
                connect_timeout=connect_timeout_seconds,
                read_timeout=read_timeout_seconds,
                retries={"max_attempts": max_attempts, "mode": "adaptive"},
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
            )
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region or None,
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                aws_session_token=session_token or None,
                verify=verify_tls,
                config=config,
            )
        self.client = client

    def _remote_key(self, key: str) -> str:
        return join_key(self.prefix, normalize_key(key)) if self.prefix else normalize_key(key)

    def _logical_key(self, remote_key: str) -> str:
        if self.prefix and remote_key.startswith(f"{self.prefix}/"):
            return remote_key[len(self.prefix) + 1 :]
        return remote_key

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        response = getattr(exc, "response", {}) or {}
        code = str((response.get("Error") or {}).get("Code", ""))
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

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
        remote = self._remote_key(normalized)
        checksum = hashlib.sha256(data).hexdigest()
        object_metadata = {str(k): str(v) for k, v in (metadata or {}).items()}
        object_metadata.setdefault("sha256", checksum)
        existing = self.head(normalized)
        if existing is not None:
            existing_checksum = existing.metadata.get("sha256") or existing.etag
            if immutable and existing_checksum and existing_checksum.strip('"') != checksum:
                raise ObjectConflictError(f"Immutable object already exists with different content: {normalized}")
            if existing_checksum and existing_checksum.strip('"') == checksum:
                return existing
        self.client.put_object(
            Bucket=self.bucket,
            Key=remote,
            Body=data,
            ContentType=content_type,
            Metadata=object_metadata,
        )
        return self.head(normalized) or ObjectInfo(
            key=normalized,
            size=len(data),
            etag=checksum,
            last_modified=datetime.now(UTC),
            content_type=content_type,
            metadata=object_metadata,
        )

    def get_bytes(self, key: str) -> bytes:
        normalized = normalize_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._remote_key(normalized))
            body = response["Body"]
            return body.read()
        except Exception as exc:
            if self._is_not_found(exc):
                raise ObjectNotFoundError(normalized) from exc
            raise

    def head(self, key: str) -> ObjectInfo | None:
        normalized = normalize_key(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._remote_key(normalized))
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise
        modified = response.get("LastModified")
        if modified is not None and modified.tzinfo is None:
            modified = modified.replace(tzinfo=UTC)
        return ObjectInfo(
            key=normalized,
            size=int(response.get("ContentLength", 0)),
            etag=str(response.get("ETag", "")).strip('"') or None,
            last_modified=modified,
            content_type=response.get("ContentType"),
            metadata={str(k): str(v) for k, v in (response.get("Metadata") or {}).items()},
        )

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        logical_prefix = prefix.replace("\\", "/").strip("/")
        remote_prefix = self._remote_key(logical_prefix) if logical_prefix else (f"{self.prefix}/" if self.prefix else "")
        paginator = self.client.get_paginator("list_objects_v2")
        rows: list[ObjectInfo] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=remote_prefix):
            for item in page.get("Contents", []):
                remote_key = str(item["Key"])
                logical_key = self._logical_key(remote_key)
                rows.append(
                    ObjectInfo(
                        key=logical_key,
                        size=int(item.get("Size", 0)),
                        etag=str(item.get("ETag", "")).strip('"') or None,
                        last_modified=item.get("LastModified"),
                    )
                )
        return sorted(rows, key=lambda item: item.key)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._remote_key(key))

    def ping(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def ensure_container(self, *, create_if_missing: bool = False) -> bool:
        try:
            self.ping()
            return False
        except Exception as exc:
            if not create_if_missing or not self._is_not_found(exc):
                raise
        # DigitalOcean's Python SDK-compatible flow creates the Space at the
        # datacenter selected by endpoint_url; no AWS LocationConstraint is sent.
        self.client.create_bucket(Bucket=self.bucket)
        self.ping()
        return True
