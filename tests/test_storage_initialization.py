from __future__ import annotations

from pathlib import Path

from smog_ai.storage.local import LocalObjectStore
from smog_ai.storage.s3 import S3ObjectStore


class NotFoundError(Exception):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "404"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        super().__init__("not found")


class FakeBucketClient:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.create_calls: list[str] = []

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        if not self.exists:
            raise NotFoundError()

    def create_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        self.create_calls.append(Bucket)
        self.exists = True


def _store(client: FakeBucketClient) -> S3ObjectStore:
    return S3ObjectStore(
        bucket="customer-space",
        endpoint_url="https://fra1.digitaloceanspaces.com",
        region="fra1",
        access_key="key",
        secret_key="secret",
        client=client,
    )


def test_spaces_container_is_created_only_when_missing_and_allowed() -> None:
    client = FakeBucketClient(exists=False)
    created = _store(client).ensure_container(create_if_missing=True)
    assert created is True
    assert client.create_calls == ["customer-space"]


def test_existing_spaces_container_is_not_recreated() -> None:
    client = FakeBucketClient(exists=True)
    created = _store(client).ensure_container(create_if_missing=True)
    assert created is False
    assert client.create_calls == []


def test_local_object_store_initialization_is_idempotent(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    assert store.ensure_container(create_if_missing=True) is False
    assert (tmp_path / "objects").is_dir()
