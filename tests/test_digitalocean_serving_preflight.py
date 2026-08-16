from __future__ import annotations

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.publishing.serving_release import inspect_local_serving_release
from smog_ai.storage.local import LocalObjectStore
from smog_ai.cli import _select_digitalocean_spaces_destination


def test_digitalocean_destination_uses_spaces_environment_despite_local_config(
    app_config, monkeypatch
) -> None:
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = "object-store"
    monkeypatch.setenv("SPACES_BUCKET", "smog-ai-test")
    monkeypatch.setenv("SPACES_REGION", "fra1")
    monkeypatch.setenv("SPACES_ENDPOINT_URL", "https://fra1.digitaloceanspaces.com/")
    monkeypatch.setenv("SPACES_PREFIX", "/production/serving/")

    selected = _select_digitalocean_spaces_destination(app_config)

    assert selected == {
        "backend": "spaces",
        "bucket": "smog-ai-test",
        "region": "fra1",
        "endpoint": "https://fra1.digitaloceanspaces.com",
        "prefix": "production/serving",
    }
    assert app_config.object_storage.backend == "spaces"


def test_preflight_reports_only_serving_assets(tmp_path, app_config) -> None:
    root = tmp_path / "object-store"
    source = ArtifactRepository(LocalObjectStore(root))
    release_id = "release-preflight"
    surface_key = source.layout.spatial_surface(release_id, "PM10", 1)
    manifest_key = source.layout.spatial_manifest(release_id)
    source.store.put_bytes(surface_key, b"gzip-data")
    source.store.put_bytes(source.layout.spatial_boundary, b"boundary")
    source.store.put_bytes(source.layout.spatial_places, b"places")
    manifest = {
        "contract": "smog-ai-serving-release",
        "release_id": release_id,
        "generated_at": "2026-08-13T00:00:00Z",
        "boundary_key": source.layout.spatial_boundary,
        "places_key": source.layout.spatial_places,
        "surfaces": [{"parameter": "PM10", "object_key": surface_key}],
    }
    stored = source.put_json(manifest_key, manifest)
    source.put_json(
        source.layout.latest_spatial_pointer,
        {
            "contract": "smog-ai-serving-pointer",
            "release_id": release_id,
            "manifest_key": manifest_key,
            "manifest_checksum": stored.checksum,
        },
    )

    result = inspect_local_serving_release(app_config, root, check_destination=False)

    assert result["status"] == "ready"
    assert result["release_id"] == release_id
    assert result["parameters"] == ["PM10"]
    assert result["forbidden_payloads_present"] is False
    assert result["forbidden_objects"] == []
    assert result["uncompressed_objects"] == []
    assert result["pointer_published_last"] is True
