from __future__ import annotations

import time
from collections import Counter
from types import SimpleNamespace

from server.application.spatial_source import ObjectStoreSpatialSource


class CountingRepository:
    def __init__(self) -> None:
        self.store = SimpleNamespace(backend_name="counting")
        self.layout = SimpleNamespace(
            latest_spatial_pointer="spatial/latest.json",
            spatial_boundary="spatial/boundary.json.gz",
            spatial_places="spatial/places.json.gz",
        )
        self.calls: Counter[str] = Counter()
        self.manifest = {
            "surfaces": [
                {"parameter": "PM10", "horizon_hours": index, "object_key": f"surface-{index}.json.gz"}
                for index in range(3)
            ]
        }

    def ping(self) -> None:
        return None

    def get_json(self, key: str):  # type: ignore[no-untyped-def]
        self.calls[key] += 1
        if key == "spatial/latest.json":
            return {"manifest_key": "release/manifest.json"}
        if key == "release/manifest.json":
            return self.manifest
        raise KeyError(key)

    def get_gzip_json(self, key: str):  # type: ignore[no-untyped-def]
        self.calls[key] += 1
        return {"object_key": key}


def test_requested_surface_is_fetched_once_across_pointer_refreshes() -> None:
    repository = CountingRepository()
    source = ObjectStoreSpatialSource(
        repository, cache_ttl_seconds=0.01, cache_max_items=8
    )

    assert source.surface(parameter="PM10", horizon_hours=0)
    time.sleep(0.02)
    assert source.surface(parameter="PM10", horizon_hours=0)

    assert repository.calls["spatial/latest.json"] == 2
    assert repository.calls["release/manifest.json"] == 1
    assert repository.calls["surface-0.json.gz"] == 1
    assert repository.calls["surface-1.json.gz"] == 0


def test_immutable_cache_is_bounded_and_evicts_least_recently_used() -> None:
    repository = CountingRepository()
    source = ObjectStoreSpatialSource(
        repository, cache_ttl_seconds=0, cache_max_items=3
    )

    for horizon in (0, 1, 2, 0):
        assert source.surface(parameter="PM10", horizon_hours=horizon)

    assert len(source._immutable_cache) <= 3
    assert repository.calls["surface-0.json.gz"] == 2
