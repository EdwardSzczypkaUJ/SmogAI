from __future__ import annotations

from pathlib import Path

from smog_ai.collectors.history_cache import (
    LocalHistoricalDataCacheBridge,
    ObjectStoreHistoricalDataCacheBridge,
)
from smog_ai.storage.local import LocalObjectStore


def test_local_history_cache_fetches_once(tmp_path: Path) -> None:
    bridge = LocalHistoricalDataCacheBridge()
    path = tmp_path / "cache" / "archive.zip"
    calls = 0

    def fetch() -> bytes:
        nonlocal calls
        calls += 1
        return b"archive"

    first = bridge.get_or_fetch(
        local_path=path,
        key="prepared/archive.zip",
        fetch=fetch,
        content_type="application/zip",
    )
    second = bridge.get_or_fetch(
        local_path=path,
        key="prepared/archive.zip",
        fetch=fetch,
        content_type="application/zip",
    )
    assert first.data == b"archive"
    assert second.source == "local_cache"
    assert calls == 1


def test_object_store_cache_restores_missing_local_file(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "object-store")
    bridge = ObjectStoreHistoricalDataCacheBridge(
        store=store,
        prefix="source-cache/gios-history",
        mode="object_store",
    )
    local_path = tmp_path / "local" / "archive.zip"
    bridge.write(
        local_path=local_path,
        key="prepared/archive.zip",
        data=b"remote-archive",
        content_type="application/zip",
    )
    local_path.unlink()
    result = bridge.read(local_path=local_path, key="prepared/archive.zip")
    assert result is not None
    assert result.source == "object_store_cache"
    assert result.data == b"remote-archive"
    assert local_path.read_bytes() == b"remote-archive"


def test_hybrid_cache_prefers_local_file(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "object-store")
    bridge = ObjectStoreHistoricalDataCacheBridge(
        store=store,
        prefix="source-cache/gios-history",
        mode="hybrid",
        prefer_local_first=True,
        allow_remote_failure_fallback=True,
    )
    local_path = tmp_path / "local" / "archive.zip"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"local")
    result = bridge.read(local_path=local_path, key="prepared/archive.zip")
    assert result is not None
    assert result.source == "local_cache"
    assert result.data == b"local"
