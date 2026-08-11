from __future__ import annotations

import gzip
import json
from pathlib import Path

from sqlalchemy import select

from server.database.store import ObjectStoreSnapshotStore
from smog_ai.artifacts.datasets import (
    create_artifact_repository,
    export_operational_data,
    load_latest_operational_bundle,
    load_training_frame_from_store,
    materialize_training_frames_from_store,
)
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion, OutboxStatus, PublicationOutbox
from smog_ai.publishing.publisher import retry_publications
from smog_ai.publishing.snapshot import build_snapshot
from smog_ai.storage.base import ObjectConflictError
from smog_ai.storage.local import LocalObjectStore
from smog_ai.training.trainer import train_models
from tests.conftest import seed_basic


def _enable_local_object_store(app_config, tmp_path: Path) -> None:
    app_config.object_storage.enabled = True
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "spaces-emulator"
    app_config.artifacts.operational_export_days = 730
    app_config.artifacts.upload_models = True
    app_config.training.input_source = "object_store"
    app_config.training.allow_database_fallback = False
    app_config.publication.enabled = True
    app_config.publication.transport = "object_store"
    app_config.data_validation.require_pandera = False
    app_config.ensure_directories()


def test_local_object_store_roundtrip_and_immutable_conflict(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    first = store.put_bytes("folder/test.bin", b"alpha", immutable=True)
    repeated = store.put_bytes("folder/test.bin", b"alpha", immutable=True)
    assert first.etag == repeated.etag
    assert store.get_bytes("folder/test.bin") == b"alpha"
    assert [item.key for item in store.list("folder")] == ["folder/test.bin"]
    try:
        store.put_bytes("folder/test.bin", b"beta", immutable=True)
        raise AssertionError("expected immutable object conflict")
    except ObjectConflictError:
        pass


def test_spaces_assignment_roundtrip_from_raw_to_training_model_and_snapshot(
    engine, app_config, tmp_path
) -> None:
    _enable_local_object_store(app_config, tmp_path)
    seed_basic(engine, hours=96)

    with session_scope(engine) as session:
        uploaded = export_operational_data(session, app_config, run_id="bridge-test")
        assert uploaded.inserted == 1
        assert uploaded.details["flow_step"] == "01-collected-data-uploaded"

    bundle, raw_pointer = load_latest_operational_bundle(app_config)
    assert raw_pointer["run_id"] == "bridge-test"
    assert len(bundle["air_measurements"]) >= 90
    assert len(bundle["weather_measurements"]) >= 90

    with session_scope(engine) as session:
        curated = materialize_training_frames_from_store(session, app_config)
        assert curated.inserted == 1
        assert curated.details["source"] == "object_store"

    frame, dataset_manifest = load_training_frame_from_store(
        app_config, parameter="PM10", horizon=6
    )
    assert len(frame) >= app_config.training.minimum_training_rows
    assert dataset_manifest["source"] == "object_store"
    assert dataset_manifest["source_manifest"]["run_id"] == "bridge-test"

    with session_scope(engine) as session:
        trained = train_models(session, app_config)
        assert trained.inserted >= 1
        active = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == "PM10",
                ModelVersion.forecast_horizon == 6,
                ModelVersion.active.is_(True),
            )
        )
        assert active is not None
        remote = (active.metrics_json or {}).get("remote_artifact") or {}
        assert remote.get("artifact_object_key")
        assert remote.get("metrics_object_key")

        snapshot = build_snapshot(session, app_config)
        published = retry_publications(session, app_config)
        assert published.inserted == 1
        outbox = session.scalar(
            select(PublicationOutbox).where(
                PublicationOutbox.publication_id == snapshot.publication_id
            )
        )
        assert outbox is not None
        assert outbox.status == OutboxStatus.published.value

    repository = create_artifact_repository(app_config)
    latest = repository.latest_snapshot_payload()
    assert latest is not None
    assert latest["metadata"]["publication_id"] == snapshot.publication_id
    compressed = repository.latest_snapshot_bytes()
    decoded = json.loads(gzip.decompress(compressed).decode("utf-8"))
    assert decoded["metadata"]["checksum"] == snapshot.checksum

    server_store = ObjectStoreSnapshotStore(repository)
    assert server_store.latest_payload()["metadata"]["publication_id"] == snapshot.publication_id
    assert server_store.audit_summary()["publication_count"] == 1


def test_incomplete_operational_export_does_not_replace_latest(engine, app_config, tmp_path) -> None:
    _enable_local_object_store(app_config, tmp_path)
    repository = create_artifact_repository(app_config)
    repository.put_json(repository.layout.latest_raw_manifest, {
        "run_id": "previous-good",
        "object_key": "existing.json.gz",
        "record_counts": {
            "air_stations": 1,
            "air_sensors": 1,
            "air_measurements": 1,
            "weather_stations": 1,
            "weather_measurements": 1,
        },
        "complete": True,
    })
    with session_scope(engine) as session:
        result = export_operational_data(session, app_config, run_id="failed-attempt")
    assert result.warnings == 1
    assert result.details["artifact"]["complete"] is False
    assert result.details["artifact"]["latest_pointer_updated"] is False
    assert repository.get_json(repository.layout.latest_raw_manifest)["run_id"] == "previous-good"
    assert repository.get_json(repository.layout.last_raw_attempt_manifest)["run_id"] == "failed-attempt"
