from __future__ import annotations

from smog_ai.artifacts.repository import ArtifactRepository, sha256_bytes
from smog_ai.publishing.serving_release import promote_serving_release
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
