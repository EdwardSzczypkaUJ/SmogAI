from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.storage.base import ObjectConflictError
from smog_ai.storage.local import LocalObjectStore
from smog_ai.storage.s3 import S3ObjectStore


class _NotFound(Exception):
    response = {
        "Error": {"Code": "NoSuchKey"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


class _Paginator:
    def __init__(self, client: _FakeS3Client) -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str):  # type: ignore[no-untyped-def]
        del Bucket
        yield {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(row["Body"]),
                    "ETag": row["ETag"],
                    "LastModified": row["LastModified"],
                }
                for key, row in self.client.objects.items()
                if key.startswith(Prefix)
            ]
        }


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:
        del Bucket
        body = bytes(Body)
        self.objects[Key] = {
            "Body": body,
            "ContentType": kwargs.get("ContentType"),
            "Metadata": dict(kwargs.get("Metadata") or {}),
            "ETag": hashlib.sha256(body).hexdigest(),
            "LastModified": datetime.now(UTC),
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        try:
            row = self.objects[Key]
        except KeyError as exc:
            raise _NotFound from exc
        return {"Body": BytesIO(row["Body"])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        try:
            row = self.objects[Key]
        except KeyError as exc:
            raise _NotFound from exc
        return {
            "ContentLength": len(row["Body"]),
            "ETag": row["ETag"],
            "LastModified": row["LastModified"],
            "ContentType": row["ContentType"],
            "Metadata": row["Metadata"],
        }

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del Bucket
        self.objects.pop(Key, None)

    def head_bucket(self, *, Bucket: str) -> None:
        assert Bucket == "forecast-space"


def _repositories(tmp_path: Path) -> list[ArtifactRepository]:
    spaces = S3ObjectStore(
        bucket="forecast-space",
        endpoint_url="https://fra1.digitaloceanspaces.com",
        region="fra1",
        access_key="test",
        secret_key="test",
        prefix="smog-ai/test",
        client=_FakeS3Client(),
    )
    return [
        ArtifactRepository(LocalObjectStore(tmp_path / "objects")),
        ArtifactRepository(spaces),
    ]


def test_artifact_bridge_has_local_and_spaces_read_write_parity(tmp_path: Path) -> None:
    payload = {"dataset_id": "ds-001", "values": [1.5, 2.5], "source": "GIOŚ"}
    compressed_payload = {"stations": [{"id": 1, "value": 17.25}]}

    for repository in _repositories(tmp_path):
        repository.ping()
        stored = repository.put_json("datasets/example.json", payload, immutable=True)
        repository.put_gzip_json(
            "forecasts/stations.json.gz", compressed_payload, immutable=True
        )

        assert repository.get_json(stored.key) == payload
        assert repository.get_gzip_json("forecasts/stations.json.gz") == compressed_payload
        assert [item.key for item in repository.store.list("datasets/")] == [
            "datasets/example.json"
        ]

        with pytest.raises(ObjectConflictError):
            repository.put_json(
                "datasets/example.json", {"dataset_id": "different"}, immutable=True
            )

        repository.store.delete("datasets/example.json")
        assert not repository.store.exists("datasets/example.json")
