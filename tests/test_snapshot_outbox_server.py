from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from server.api.settings import ServerSettings
from server.database.store import (
    DatabaseSnapshotStore,
    SnapshotConflictError,
    SnapshotStore,
    create_snapshot_store,
)
from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirStation, Forecast, ModelVersion, PublicationOutbox
from smog_ai.database.repository import enqueue_publication
from smog_ai.publishing.schema import SnapshotPayload
from smog_ai.publishing.snapshot import build_snapshot, build_snapshot_stage, calculate_payload_checksum
from tests.conftest import seed_basic


def test_enqueue_publication_is_idempotent(engine, tmp_path) -> None:
    path = tmp_path / "x.gz"
    path.write_bytes(b"x")
    with session_scope(engine) as session:
        one = enqueue_publication(session, publication_id="p1", payload_path=path, payload_type="gzip", checksum="a" * 64)
        two = enqueue_publication(session, publication_id="p1", payload_path=path, payload_type="gzip", checksum="a" * 64)
        assert one.id == two.id
        assert session.scalar(select(func.count()).select_from(PublicationOutbox)) == 1


def test_snapshot_checksum_roundtrip(engine, app_config) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
        raw = gzip.decompress(result.path.read_bytes())
        payload = json.loads(raw)
        assert calculate_payload_checksum(payload) == result.checksum
        assert SnapshotPayload.model_validate(payload).metadata.publication_id == result.publication_id


def test_snapshot_build_enqueues(engine, app_config) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
        row = session.scalar(select(PublicationOutbox).where(PublicationOutbox.publication_id == result.publication_id))
        assert row is not None
        assert Path(row.payload_path).exists()


def test_snapshot_excludes_legacy_forecast_created_after_target(engine, app_config) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        station = session.scalar(select(AirStation))
        now = datetime.now(UTC).replace(microsecond=0)
        model = ModelVersion(
            model_name="legacy-test",
            algorithm="persistence",
            parameter="PM10",
            forecast_horizon=6,
            semantic_version="legacy-test-v1",
            active=False,
        )
        session.add(model)
        session.flush()
        session.add_all(
            [
                Forecast(
                    model_version_id=model.id,
                    air_station_id=station.id,
                    parameter="PM10",
                    forecast_created_at=now,
                    forecast_origin_time=now - timedelta(hours=12),
                    target_time=now - timedelta(hours=6),
                    forecast_horizon=6,
                    predicted_value=25.0,
                ),
                Forecast(
                    model_version_id=model.id,
                    air_station_id=station.id,
                    parameter="PM10",
                    forecast_created_at=now,
                    forecast_origin_time=now,
                    target_time=now + timedelta(hours=6),
                    forecast_horizon=6,
                    predicted_value=26.0,
                ),
            ]
        )

    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
        payload = json.loads(gzip.decompress(result.path.read_bytes()))
        assert len(payload["forecasts"]) == 1
        assert payload["forecasts"][0]["predicted_value"] == 26.0


def test_server_store_is_idempotent(engine, app_config, tmp_path) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
    body = result.path.read_bytes()
    store = SnapshotStore(tmp_path / "server")
    payload = store.decode_and_validate(body, result.checksum, result.publication_id)
    assert store.save(body, payload)[0] is True
    assert store.save(body, payload)[0] is False
    assert store.latest_payload()["metadata"]["publication_id"] == result.publication_id


def test_server_rejects_checksum(engine, app_config, tmp_path) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
    store = SnapshotStore(tmp_path / "server")
    try:
        store.decode_and_validate(result.path.read_bytes(), "0" * 64, result.publication_id)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_fastapi_upload_and_latest(engine, app_config, tmp_path, monkeypatch) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
    import server.api.main as main

    main.store = SnapshotStore(tmp_path / "api-store")
    main.settings = ServerSettings(
        data_dir=tmp_path / "api-store",
        api_token="secret",
        max_upload_bytes=5_000_000,
        keep_versions=5,
        rate_limit_per_minute=30,
    )
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/snapshots",
        content=result.path.read_bytes(),
        headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/gzip",
            "X-Publication-Id": result.publication_id,
            "X-Checksum": result.checksum,
        },
    )
    assert response.status_code == 201
    duplicate = client.post(
        "/api/v1/snapshots",
        content=result.path.read_bytes(),
        headers={
            "Authorization": "Bearer secret",
            "X-Publication-Id": result.publication_id,
            "X-Checksum": result.checksum,
        },
    )
    assert duplicate.status_code == 200
    assert client.get("/api/v1/snapshots/latest").status_code == 200


def test_fastapi_requires_token(tmp_path) -> None:
    import server.api.main as main

    main.store = SnapshotStore(tmp_path / "api-store")
    main.settings = ServerSettings(tmp_path / "api-store", "secret", 1000, 5, 30)
    response = TestClient(main.app).post(
        "/api/v1/snapshots",
        content=b"bad",
        headers={"X-Publication-Id": "p", "X-Checksum": "0" * 64},
    )
    assert response.status_code == 401



def test_database_snapshot_store_is_idempotent(engine, app_config, tmp_path) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
    body = result.path.read_bytes()
    database_url = f"sqlite+pysqlite:///{tmp_path / 'server-store.db'}"
    store = DatabaseSnapshotStore(database_url, keep_versions=5)
    payload = store.decode_and_validate(body, result.checksum, result.publication_id)

    created, reference = store.save(body, payload, remote_address="127.0.0.1")
    duplicate, duplicate_reference = store.save(body, payload, remote_address="127.0.0.1")

    assert created is True
    assert duplicate is False
    assert reference.name == duplicate_reference.name
    assert store.latest_payload()["metadata"]["publication_id"] == result.publication_id
    assert store.audit_summary()["publication_count"] == 1
    assert store.history()[0]["duplicate_count"] == 1


def test_database_snapshot_store_rejects_conflicting_publication(
    engine, app_config, tmp_path
) -> None:
    seed_basic(engine, hours=12)
    with session_scope(engine) as session:
        result = build_snapshot(session, app_config)
    body = result.path.read_bytes()
    store = DatabaseSnapshotStore(f"sqlite+pysqlite:///{tmp_path / 'conflict.db'}")
    payload = store.decode_and_validate(body, result.checksum, result.publication_id)
    store.save(body, payload)
    conflicting = payload.model_copy(deep=True)
    conflicting.metadata.checksum = "f" * 64

    try:
        store.save(body, conflicting)
        raise AssertionError("expected SnapshotConflictError")
    except SnapshotConflictError:
        pass


def test_database_snapshot_store_prunes_old_versions(
    engine, app_config, tmp_path
) -> None:
    seed_basic(engine, hours=12)
    store = DatabaseSnapshotStore(f"sqlite+pysqlite:///{tmp_path / 'retention.db'}", keep_versions=2)
    with session_scope(engine) as session:
        first = build_snapshot(session, app_config)
    body = first.path.read_bytes()
    payload = store.decode_and_validate(body, first.checksum, first.publication_id)
    for index in range(3):
        current = payload.model_copy(deep=True)
        current.metadata.publication_id = f"publication-{index}"
        current.metadata.checksum = first.checksum
        store.save(body, current)

    assert store.audit_summary()["publication_count"] == 2
    assert {row["publication_id"] for row in store.history(limit=10)} == {
        "publication-1",
        "publication-2",
    }


def test_snapshot_store_factory_selects_database(tmp_path) -> None:
    store = create_snapshot_store(
        data_dir=tmp_path / "ignored",
        keep_versions=5,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'factory.db'}",
        storage_backend="auto",
    )
    assert isinstance(store, DatabaseSnapshotStore)


def test_empty_snapshot_stage_does_not_enqueue(engine, app_config) -> None:
    with session_scope(engine) as session:
        stats = build_snapshot_stage(session, app_config)
        assert stats.inserted == 0
        assert stats.skipped == 1
        assert stats.details["reason"] == "no_air_measurements_or_forecasts"
        assert session.scalar(select(func.count()).select_from(PublicationOutbox)) == 0
