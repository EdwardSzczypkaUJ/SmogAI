from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.config import AppConfig
from smog_ai.domain import StageStats
from smog_ai.storage.local import LocalObjectStore

RETENTION_CONFIRMATION = "PRUNE OLD SERVING RELEASES"
RELEASE_PREFIX = "serving/releases/release="
RELEASE_HISTORY_PREFIX = "serving/history/snapshots/"
RELEASE_HISTORY_CONTRACT = "smog-ai-serving-release-history"
RELEASE_HISTORY_LIMIT = 90


def _transfer_category(key: str, manifest_key: str, pointer_key: str) -> str:
    if key == pointer_key:
        return "pointer"
    if key == manifest_key:
        return "manifest"
    lowered = key.lower()
    if any(token in lowered for token in ("boundary", "places")):
        return "static"
    if lowered.endswith(".gz"):
        return "surfaces"
    return "stats"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _safe_report(path: Path) -> dict[str, Any] | None:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            payload = json.loads(path.read_text(encoding=encoding))
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _report_timestamp(path: Path) -> str | None:
    try:
        parsed = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _local_release_history_seed(config: AppConfig) -> list[dict[str, Any]]:
    """Recover safe legacy publication history from local transfer reports."""

    root = config.paths.logs_dir.parent / "reports" / "digitalocean"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/03-publication.json")):
        payload = _safe_report(path)
        if payload is None:
            continue
        details = dict(payload.get("details") or {})
        release_id = str(details.get("release_id") or "").strip()
        if not release_id:
            continue
        rows.append(
            {
                "release_id": release_id,
                "published_at": _report_timestamp(path),
                "generated_at": None,
                "surface_count": None,
                "parameters": [],
                "horizons_hours": [],
                "freshness_status": None,
                "transfer": {
                    "objects_uploaded": int(details.get("objects_copied") or 0) + 1,
                    "objects_reused": int(details.get("objects_reused") or 0),
                    "bytes_uploaded": int(details.get("bytes_uploaded") or 0),
                    "elapsed_seconds": details.get("elapsed_seconds"),
                    "request_count": details.get("request_count"),
                    "reuse_ratio": details.get("reuse_ratio"),
                },
                "source": "legacy_local_publication_report",
            }
        )
    return rows


def _history_from_pointer(repository: ArtifactRepository) -> list[dict[str, Any]]:
    try:
        pointer = repository.get_json(repository.layout.latest_spatial_pointer)
        history_key = str(pointer.get("history_key") or "")
        if not history_key:
            return []
        body = repository.store.get_bytes(history_key)
        expected = str(pointer.get("history_checksum") or "")
        if expected and _sha256(body) != expected:
            return []
        payload = json.loads(body.decode("utf-8"))
        if payload.get("contract") != RELEASE_HISTORY_CONTRACT:
            return []
        return [dict(row) for row in list(payload.get("releases") or [])]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return []


def _manifest_history_entry(
    manifest: dict[str, Any],
    *,
    published_at: str,
    transfer: dict[str, Any],
) -> dict[str, Any]:
    surfaces = [dict(row) for row in list(manifest.get("surfaces") or [])]
    operations = dict(manifest.get("operations") or {})
    data = dict(operations.get("data") or {})
    model_versions = sorted(
        {
            str(version)
            for row in surfaces
            for version in list(row.get("model_versions") or [])
            if version
        }
    )
    origins = sorted(
        str(row.get("origin_time"))
        for row in surfaces
        if row.get("origin_time")
    )
    targets = sorted(
        str(row.get("target_time"))
        for row in surfaces
        if row.get("target_time")
    )
    return {
        "release_id": manifest.get("release_id"),
        "published_at": published_at,
        "generated_at": manifest.get("generated_at"),
        "origin_start": origins[0] if origins else None,
        "origin_end": origins[-1] if origins else None,
        "target_start": targets[0] if targets else None,
        "target_end": targets[-1] if targets else None,
        "surface_count": len(surfaces),
        "parameters": sorted(str(value) for value in manifest.get("parameters") or []),
        "horizons_hours": sorted(
            int(value) for value in manifest.get("horizons_hours") or []
        ),
        "freshness_status": data.get("status_at_publication"),
        "fresh_threshold_hours": data.get("fresh_threshold_hours"),
        "stale_threshold_hours": data.get("stale_threshold_hours"),
        "measurement_age_hours": data.get("measurement_age_hours_at_publication"),
        "collection_age_hours": data.get("collection_age_hours_at_publication"),
        "model_versions": model_versions,
        "transfer": transfer,
        "source": "verified_serving_publication",
    }


def _merge_release_history(
    *groups: list[dict[str, Any]],
    limit: int = RELEASE_HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    by_release: dict[str, dict[str, Any]] = {}
    for group in groups:
        for raw in group:
            row = dict(raw)
            release_id = str(row.get("release_id") or "").strip()
            if not release_id:
                continue
            previous = by_release.get(release_id, {})
            merged = dict(previous)
            for key, value in row.items():
                if value not in (None, "", [], {}):
                    merged[key] = value
            merged["release_id"] = release_id
            by_release[release_id] = merged
    return sorted(
        by_release.values(),
        key=lambda row: (
            str(row.get("published_at") or row.get("generated_at") or ""),
            str(row.get("release_id") or ""),
        ),
        reverse=True,
    )[: max(1, int(limit))]


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
    *,
    history_seed: list[dict[str, Any]] | None = None,
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

    started = time.perf_counter()
    destination.ping()
    copied_bytes = 0
    copied = 0
    skipped = 0
    request_count = 1
    categories = {
        name: {"objects_uploaded": 0, "objects_reused": 0, "bytes_uploaded": 0}
        for name in (
            "surfaces",
            "stats",
            "static",
            "manifest",
            "history",
            "pointer",
        )
    }
    for key in _release_keys(pointer, manifest):
        body = source.store.get_bytes(key)
        existing = destination.store.head(key)
        request_count += 1
        checksum = _sha256(body)
        existing_checksum = None
        if existing is not None:
            existing_checksum = existing.metadata.get("sha256") or existing.etag
        if existing_checksum and existing_checksum.strip('"') == checksum:
            skipped += 1
            categories[_transfer_category(key, manifest_key, pointer_key)][
                "objects_reused"
            ] += 1
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
        request_count += 1
        remote = destination.store.get_bytes(key)
        request_count += 1
        if _sha256(remote) != checksum:
            raise RuntimeError(f"Remote checksum verification failed for {key}.")
        copied += 1
        copied_bytes += len(body)
        category = categories[_transfer_category(key, manifest_key, pointer_key)]
        category["objects_uploaded"] += 1
        category["bytes_uploaded"] += len(body)

    # The history snapshot is immutable and referenced by the final Serving
    # pointer.  An interrupted upload can therefore leave only an unreachable
    # immutable object; readers never observe a half-written history.
    published_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    transfer_before_pointer = {
        "objects_uploaded": copied,
        "objects_reused": skipped,
        "bytes_uploaded": copied_bytes,
        "request_count": request_count,
    }
    current_entry = _manifest_history_entry(
        manifest,
        published_at=published_at,
        transfer=transfer_before_pointer,
    )
    history_rows = _merge_release_history(
        list(history_seed or []),
        _history_from_pointer(destination),
        [current_entry],
    )
    history_payload = {
        "schema_version": "1.0",
        "contract": RELEASE_HISTORY_CONTRACT,
        "generated_at": published_at,
        "active_release_id": pointer.get("release_id"),
        "retention": {
            "history_entries": RELEASE_HISTORY_LIMIT,
            "surface_release_retention_independent": True,
        },
        "releases": history_rows,
        "privacy": {
            "raw_data_included": False,
            "training_data_included": False,
            "model_binaries_included": False,
            "local_paths_included": False,
            "secret_values_included": False,
        },
    }
    history_bytes = _json_bytes(history_payload)
    history_checksum = _sha256(history_bytes)
    history_key = f"{RELEASE_HISTORY_PREFIX}sha256={history_checksum}.json"
    existing_history = destination.store.head(history_key)
    request_count += 1
    existing_history_checksum = None
    if existing_history is not None:
        existing_history_checksum = (
            existing_history.metadata.get("sha256") or existing_history.etag
        )
    if (
        existing_history_checksum
        and existing_history_checksum.strip('"') == history_checksum
    ):
        skipped += 1
        categories["history"]["objects_reused"] += 1
    else:
        destination.store.put_bytes(
            history_key,
            history_bytes,
            content_type="application/json; charset=utf-8",
            metadata={"sha256": history_checksum, "serving-contract": "history-v1"},
            immutable=True,
        )
        request_count += 1
        remote_history = destination.store.get_bytes(history_key)
        request_count += 1
        if _sha256(remote_history) != history_checksum:
            raise RuntimeError("Remote release-history checksum verification failed.")
        copied += 1
        copied_bytes += len(history_bytes)
        categories["history"]["objects_uploaded"] += 1
        categories["history"]["bytes_uploaded"] += len(history_bytes)

    # Atomic publication boundary: readers cannot observe the release until all
    # immutable objects, manifest and history have been uploaded and verified.
    # The only mutable object is written last.
    published_pointer = {
        **pointer,
        "history_key": history_key,
        "history_checksum": history_checksum,
        "history_count": len(history_rows),
    }
    pointer_bytes = _json_bytes(published_pointer)
    destination.store.put_bytes(
        pointer_key,
        pointer_bytes,
        content_type="application/json; charset=utf-8",
        metadata={"sha256": _sha256(pointer_bytes), "serving-contract": "v2"},
        immutable=False,
    )
    request_count += 1
    published = destination.get_json(pointer_key)
    request_count += 1
    if published.get("release_id") != pointer.get("release_id"):
        raise RuntimeError("Remote Serving v2 pointer verification failed.")

    pointer_category = categories["pointer"]
    pointer_category["objects_uploaded"] = 1
    pointer_category["bytes_uploaded"] = len(pointer_bytes)
    elapsed_seconds = max(0.0, time.perf_counter() - started)
    total_bytes = copied_bytes + len(pointer_bytes)
    total_objects = copied + skipped + 1
    return StageStats(
        downloaded=0,
        inserted=copied + 1,
        skipped=skipped,
        details={
            "release_id": pointer.get("release_id"),
            "manifest_key": manifest_key,
            "history_key": history_key,
            "history_checksum": history_checksum,
            "history_count": len(history_rows),
            "objects_copied": copied,
            "objects_reused": skipped,
            "bytes_uploaded": total_bytes,
            "bytes_by_category": categories,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "throughput_bytes_per_second": (
                round(total_bytes / elapsed_seconds, 3)
                if elapsed_seconds > 0 else None
            ),
            "request_count": request_count,
            "reuse_ratio": skipped / total_objects if total_objects else None,
            "cache_ratio": skipped / total_objects if total_objects else None,
            "destination_backend": destination.store.backend_name,
            "pointer_published_last": True,
        },
    )


def publish_local_serving_release(config: AppConfig, source_root: Path) -> StageStats:
    source = ArtifactRepository(LocalObjectStore(source_root))
    destination = create_artifact_repository(config)
    return promote_serving_release(
        source,
        destination,
        history_seed=_local_release_history_seed(config),
    )


def plan_serving_release_retention(
    repository: ArtifactRepository,
    *,
    keep: int,
) -> dict[str, Any]:
    """Plan deletion of old immutable Serving v2 releases.

    The active release from ``serving/latest.json`` is always protected even
    when an unusual identifier would place it outside the newest ``keep`` IDs.
    Static assets and the mutable pointer are outside the release prefix and
    can never enter this plan.
    """

    if keep < 1:
        raise ValueError("Serving release retention must keep at least one release.")
    pointer = repository.get_json(repository.layout.latest_spatial_pointer)
    active_release = str(pointer.get("release_id") or "")
    if not active_release:
        raise RuntimeError("Serving v2 pointer does not identify an active release.")

    by_release: dict[str, list[Any]] = {}
    for item in repository.store.list(RELEASE_PREFIX):
        suffix = item.key[len(RELEASE_PREFIX) :]
        release_id, separator, _ = suffix.partition("/")
        if separator and release_id:
            by_release.setdefault(release_id, []).append(item)

    ordered = sorted(by_release, reverse=True)
    retained = set(ordered[:keep])
    retained.add(active_release)
    deleted_releases = [release for release in ordered if release not in retained]
    objects = [item for release in deleted_releases for item in by_release[release]]
    return {
        "active_release_id": active_release,
        "keep": keep,
        "retained_release_ids": sorted(retained, reverse=True),
        "deleted_release_ids": deleted_releases,
        "objects_to_delete": len(objects),
        "bytes_to_delete": sum(int(item.size) for item in objects),
        "keys": sorted(item.key for item in objects),
    }


def prune_serving_releases(
    repository: ArtifactRepository,
    *,
    keep: int,
    confirmation: str,
) -> StageStats:
    plan = plan_serving_release_retention(repository, keep=keep)
    if confirmation != RETENTION_CONFIRMATION:
        return StageStats(skipped=plan["objects_to_delete"], details={**plan, "applied": False})
    for key in plan["keys"]:
        if not key.startswith(RELEASE_PREFIX):
            raise RuntimeError(f"Refusing to delete a non-release object: {key}")
        repository.store.delete(key)
    return StageStats(
        downloaded=0,
        inserted=0,
        skipped=0,
        details={**plan, "applied": True, "objects_deleted": len(plan["keys"])},
    )


def prune_remote_serving_releases(
    config: AppConfig,
    *,
    keep: int,
    confirmation: str,
) -> StageStats:
    repository = create_artifact_repository(config)
    repository.ping()
    return prune_serving_releases(repository, keep=keep, confirmation=confirmation)
