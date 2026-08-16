from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.config import AppConfig
from smog_ai.domain import StageStats
from smog_ai.storage.local import LocalObjectStore


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release_keys(pointer: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    keys = {
        str(pointer["manifest_key"]),
        str(manifest["boundary_key"]),
        str(manifest["places_key"]),
    }
    for surface in manifest.get("surfaces", []):
        keys.add(str(surface["object_key"]))
        if surface.get("metadata_key"):
            keys.add(str(surface["metadata_key"]))
    return sorted(keys)


def inspect_local_serving_release(
    config: AppConfig,
    source_root: Path,
    *,
    check_destination: bool = True,
) -> dict[str, Any]:
    """Validate a local Serving v2 release and describe the remote delta."""

    source = ArtifactRepository(LocalObjectStore(source_root))
    pointer_key = source.layout.latest_spatial_pointer
    pointer = source.get_json(pointer_key)
    if pointer.get("contract") != "smog-ai-serving-pointer":
        raise RuntimeError("Local serving/latest.json is not a Serving v2 pointer.")
    manifest_key = str(pointer.get("manifest_key") or "")
    if not manifest_key:
        raise RuntimeError("Serving v2 pointer does not contain manifest_key.")
    manifest_bytes = source.store.get_bytes(manifest_key)
    if pointer.get("manifest_checksum") and _sha256(manifest_bytes) != pointer["manifest_checksum"]:
        raise RuntimeError("Local Serving v2 manifest checksum mismatch.")
    manifest = source.get_json(manifest_key)
    if manifest.get("contract") != "smog-ai-serving-release":
        raise RuntimeError("Selected manifest is not a Serving v2 release.")
    if manifest.get("release_id") != pointer.get("release_id"):
        raise RuntimeError("Serving v2 pointer and manifest release_id mismatch.")

    keys = _release_keys(pointer, manifest)
    objects = []
    for key in keys:
        body = source.store.get_bytes(key)
        objects.append({"key": key, "bytes": len(body), "sha256": _sha256(body)})
    pointer_bytes = source.store.get_bytes(pointer_key)
    destination_summary: dict[str, Any] = {
        "checked": False,
        "backend": config.object_storage.backend,
        "bucket": config.object_storage.bucket,
        "endpoint": config.object_storage.endpoint_url,
        "prefix": config.object_storage.prefix,
    }
    if check_destination:
        if config.object_storage.backend not in {"spaces", "s3"}:
            raise RuntimeError(
                "DigitalOcean preflight requires object_storage.backend=spaces or s3."
            )
        if not config.object_storage.bucket or not config.object_storage.endpoint_url:
            raise RuntimeError("Spaces bucket and endpoint must be configured.")
        destination = create_artifact_repository(config)
        destination.ping()
        reusable = 0
        upload_bytes = len(pointer_bytes)
        for item in objects:
            remote = destination.store.head(str(item["key"]))
            checksum = None
            if remote is not None:
                checksum = remote.metadata.get("sha256") or remote.etag
            if checksum and checksum.strip('"') == item["sha256"]:
                reusable += 1
            else:
                upload_bytes += int(item["bytes"])
        destination_summary.update(
            {
                "checked": True,
                "reachable": True,
                "objects_reusable": reusable,
                "objects_to_upload": len(objects) - reusable,
                "estimated_upload_bytes": upload_bytes,
            }
        )
    uncompressed_objects = [
        str(item["key"])
        for item in objects
        if not str(item["key"]).endswith((".json", ".gz"))
    ]
    forbidden_objects = [
        str(item["key"])
        for item in objects
        if any(
            token in str(item["key"]).lower()
            for token in (".sqlite", ".db", "training-snapshot", "dashboard_snapshot")
        )
    ]
    return {
        "status": "ready",
        "contract": "smog-ai-serving-release",
        "release_id": pointer.get("release_id"),
        "generated_at": manifest.get("generated_at"),
        "manifest_key": manifest_key,
        "surface_count": len(manifest.get("surfaces") or []),
        "parameters": sorted({str(row.get("parameter")) for row in manifest.get("surfaces", [])}),
        "object_count": len(objects) + 1,
        "immutable_bytes": sum(int(item["bytes"]) for item in objects),
        "pointer_bytes": len(pointer_bytes),
        "compressed_assets_only": not uncompressed_objects,
        "uncompressed_objects": uncompressed_objects,
        "forbidden_payloads_present": bool(forbidden_objects),
        "forbidden_objects": forbidden_objects,
        "objects": objects,
        "destination": destination_summary,
        "pointer_published_last": True,
    }


def promote_serving_release(
    source: ArtifactRepository,
    destination: ArtifactRepository,
) -> StageStats:
    """Promote a verified Serving v2 release, updating its pointer last.

    Only compressed serving assets and their small JSON metadata are copied.
    Training data, SQLite databases and legacy dashboard snapshots are excluded.
    """

    pointer_key = source.layout.latest_spatial_pointer
    pointer = source.get_json(pointer_key)
    if pointer.get("contract") != "smog-ai-serving-pointer":
        raise RuntimeError("Local serving/latest.json is not a Serving v2 pointer.")

    manifest_key = str(pointer.get("manifest_key") or "")
    if not manifest_key:
        raise RuntimeError("Serving v2 pointer does not contain manifest_key.")
    manifest_bytes = source.store.get_bytes(manifest_key)
    expected_manifest_checksum = str(pointer.get("manifest_checksum") or "")
    if expected_manifest_checksum and _sha256(manifest_bytes) != expected_manifest_checksum:
        raise RuntimeError("Local Serving v2 manifest checksum does not match its pointer.")
    manifest = source.get_json(manifest_key)
    if manifest.get("contract") != "smog-ai-serving-release":
        raise RuntimeError("Selected manifest is not a Serving v2 release.")
    if manifest.get("release_id") != pointer.get("release_id"):
        raise RuntimeError("Serving v2 pointer and manifest refer to different releases.")

    destination.ping()
    copied_bytes = 0
    copied = 0
    skipped = 0
    for key in _release_keys(pointer, manifest):
        body = source.store.get_bytes(key)
        existing = destination.store.head(key)
        checksum = _sha256(body)
        existing_checksum = None
        if existing is not None:
            existing_checksum = existing.metadata.get("sha256") or existing.etag
        if existing_checksum and existing_checksum.strip('"') == checksum:
            skipped += 1
            continue
        destination.store.put_bytes(
            key,
            body,
            content_type=(
                "application/gzip" if key.endswith(".gz")
                else "application/json; charset=utf-8"
            ),
            metadata={"sha256": checksum, "serving-contract": "v2"},
            immutable=True,
        )
        remote = destination.store.get_bytes(key)
        if _sha256(remote) != checksum:
            raise RuntimeError(f"Remote checksum verification failed for {key}.")
        copied += 1
        copied_bytes += len(body)

    # Atomic publication boundary: readers cannot observe the release until all
    # immutable objects and the manifest have been uploaded and verified.
    pointer_bytes = source.store.get_bytes(pointer_key)
    destination.store.put_bytes(
        pointer_key,
        pointer_bytes,
        content_type="application/json; charset=utf-8",
        metadata={"sha256": _sha256(pointer_bytes), "serving-contract": "v2"},
        immutable=False,
    )
    published = destination.get_json(pointer_key)
    if published.get("release_id") != pointer.get("release_id"):
        raise RuntimeError("Remote Serving v2 pointer verification failed.")

    return StageStats(
        downloaded=0,
        inserted=copied + 1,
        skipped=skipped,
        details={
            "release_id": pointer.get("release_id"),
            "manifest_key": manifest_key,
            "objects_copied": copied,
            "objects_reused": skipped,
            "bytes_uploaded": copied_bytes + len(pointer_bytes),
            "destination_backend": destination.store.backend_name,
            "pointer_published_last": True,
        },
    )


def publish_local_serving_release(config: AppConfig, source_root: Path) -> StageStats:
    source = ArtifactRepository(LocalObjectStore(source_root))
    destination = create_artifact_repository(config)
    return promote_serving_release(source, destination)
