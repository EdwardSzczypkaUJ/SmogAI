from __future__ import annotations

from smog_ai.artifacts.repository import ArtifactRepository, sha256_bytes
from smog_ai.publishing.serving_release import (
    RETENTION_CONFIRMATION,
    plan_serving_release_retention,
    promote_serving_release,
    prune_serving_releases,
)
from smog_ai.storage.local import MemoryObjectStore


def test_promotes_assets_before_atomic_pointer() -> None:
    source = ArtifactRepository(MemoryObjectStore())
    destination = ArtifactRepository(MemoryObjectStore())
    release_id = "release-1"
    surface_key = source.layout.spatial_surface(release_id, "PM10", 1)
    metadata_key = source.layout.spatial_surface_metadata(release_id, "PM10", 1)
    manifest_key = source.layout.spatial_manifest(release_id)
    boundary_key = source.layout.spatial_boundary
    places_key = source.layout.spatial_places

    source.store.put_bytes(surface_key, b"compressed-surface")
    source.put_json(metadata_key, {"parameter": "PM10"})
    source.store.put_bytes(boundary_key, b"compressed-boundary")
    source.store.put_bytes(places_key, b"compressed-places")
    manifest = {
        "contract": "smog-ai-serving-release",
        "release_id": release_id,
        "boundary_key": boundary_key,
        "places_key": places_key,
        "surfaces": [{"object_key": surface_key, "metadata_key": metadata_key}],
    }
    stored_manifest = source.put_json(manifest_key, manifest)
    source.put_json(
        source.layout.latest_spatial_pointer,
        {
            "contract": "smog-ai-serving-pointer",
            "release_id": release_id,
            "manifest_key": manifest_key,
            "manifest_checksum": stored_manifest.checksum,
        },
    )

    result = promote_serving_release(source, destination)

    assert result.errors == 0
    assert result.details["pointer_published_last"] is True
    assert result.details["request_count"] >= result.inserted
    assert result.details["elapsed_seconds"] >= 0
    assert result.details["bytes_by_category"]["surfaces"]["objects_uploaded"] == 1
    assert result.details["bytes_by_category"]["static"]["objects_uploaded"] == 2
    assert result.details["bytes_by_category"]["stats"]["objects_uploaded"] == 1
    assert result.details["bytes_by_category"]["manifest"]["objects_uploaded"] == 1
    assert result.details["bytes_by_category"]["pointer"]["objects_uploaded"] == 1
    assert destination.get_json(destination.layout.latest_spatial_pointer)["release_id"] == release_id
    assert destination.store.get_bytes(surface_key) == b"compressed-surface"


def test_rejects_pointer_with_invalid_manifest_checksum() -> None:
    source = ArtifactRepository(MemoryObjectStore())
    destination = ArtifactRepository(MemoryObjectStore())
    source.put_json("serving/releases/release=x/manifest.json", {})
    source.put_json(
        source.layout.latest_spatial_pointer,
        {
            "contract": "smog-ai-serving-pointer",
            "release_id": "x",
            "manifest_key": "serving/releases/release=x/manifest.json",
            "manifest_checksum": sha256_bytes(b"wrong"),
        },
    )

    try:
        promote_serving_release(source, destination)
    except RuntimeError as exc:
        assert "checksum" in str(exc)
    else:
        raise AssertionError("invalid checksum was accepted")


def test_retention_keeps_three_newest_and_never_deletes_pointer_or_static() -> None:
    repository = ArtifactRepository(MemoryObjectStore())
    releases = [
        "20260810T000000Z-a",
        "20260811T000000Z-b",
        "20260812T000000Z-c",
        "20260813T000000Z-d",
    ]
    for release in releases:
        repository.put_json(
            f"serving/releases/release={release}/manifest.json",
            {"release_id": release},
        )
        repository.store.put_bytes(
            f"serving/releases/release={release}/surfaces/PM10/h001.json.gz",
            release.encode(),
        )
    repository.put_json(
        repository.layout.latest_spatial_pointer,
        {"release_id": releases[-1]},
    )
    repository.store.put_bytes("serving/static/poland-boundary.geojson.gz", b"static")

    plan = plan_serving_release_retention(repository, keep=3)
    assert plan["deleted_release_ids"] == [releases[0]]
    assert all(key.startswith("serving/releases/release=") for key in plan["keys"])

    result = prune_serving_releases(
        repository,
        keep=3,
        confirmation=RETENTION_CONFIRMATION,
    )
    assert result.details["objects_deleted"] == 2
    assert repository.store.exists(repository.layout.latest_spatial_pointer)
    assert repository.store.exists("serving/static/poland-boundary.geojson.gz")
    assert not repository.store.exists(
        f"serving/releases/release={releases[0]}/manifest.json"
    )


def test_retention_without_confirmation_is_read_only() -> None:
    repository = ArtifactRepository(MemoryObjectStore())
    for release in ("r1", "r2"):
        repository.put_json(
            f"serving/releases/release={release}/manifest.json",
            {"release_id": release},
        )
    repository.put_json(repository.layout.latest_spatial_pointer, {"release_id": "r2"})

    result = prune_serving_releases(repository, keep=1, confirmation="")
    assert result.details["applied"] is False
    assert repository.store.exists("serving/releases/release=r1/manifest.json")
