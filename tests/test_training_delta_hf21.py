from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smog_ai.training_delta import (
    CONFIRMATION,
    build_delta,
    fast_preflight_candidate,
    layered_candidate_provenance,
    open_layered_connection,
    plan_delta,
    verify_candidate,
)


SCHEMAS = {
    "air_stations": "CREATE TABLE air_stations (id INTEGER PRIMARY KEY, latitude REAL, longitude REAL)",
    "air_sensors": "CREATE TABLE air_sensors (id INTEGER PRIMARY KEY, air_station_id INTEGER)",
    "weather_stations": "CREATE TABLE weather_stations (id INTEGER PRIMARY KEY, latitude REAL, longitude REAL)",
    "station_matches": "CREATE TABLE station_matches (id INTEGER PRIMARY KEY, air_station_id INTEGER, weather_station_id INTEGER)",
    "air_measurements": "CREATE TABLE air_measurements (id INTEGER PRIMARY KEY, air_station_id INTEGER, measurement_time TEXT, value REAL, is_valid INTEGER)",
    "weather_measurements": "CREATE TABLE weather_measurements (id INTEGER PRIMARY KEY, weather_station_id INTEGER, measurement_time TEXT, temperature_c REAL, is_valid INTEGER)",
}


def _database(path: Path, extra: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        for sql in SCHEMAS.values():
            connection.execute(sql)
        connection.execute("INSERT INTO air_stations VALUES (1, 50.0, 19.0)")
        connection.execute("INSERT INTO air_sensors VALUES (1, 1)")
        connection.execute("INSERT INTO weather_stations VALUES (1, 50.1, 19.1)")
        connection.execute("INSERT INTO station_matches VALUES (1, 1, 1)")
        connection.execute("INSERT INTO air_measurements VALUES (1, 1, '2026-08-14 10:00:00', 10.0, 1)")
        connection.execute("INSERT INTO weather_measurements VALUES (1, 1, '2026-08-14 10:00:00', 20.0, 1)")
        if extra:
            connection.execute("INSERT INTO air_measurements VALUES (2, 1, '2026-08-14 11:00:00', 11.0, 1)")
            connection.execute("INSERT INTO weather_measurements VALUES (2, 1, '2026-08-14 11:00:00', 21.0, 1)")
        connection.commit()


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    base = runtime / "training-datasets/quick/dataset-training-base/smog.db"
    live = runtime / "data/smog.db"
    _database(base, extra=False)
    _database(live, extra=True)
    manifest = base.parent / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "training-base",
                "created_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "database_path": str(base),
                "database_sha256": hashlib.sha256(base.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    pointer = runtime / "training-datasets/quick/latest.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "dataset_id": "training-base",
                "manifest_path": str(manifest),
                "database_path": str(base),
            }
        ),
        encoding="utf-8",
    )
    return runtime


def test_plan_build_verify_without_changing_production_pointer(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    pointer = runtime / "training-datasets/quick/latest.json"
    before = pointer.read_bytes()
    plan = plan_delta(runtime_root=runtime)
    assert plan["status"] == "ready"
    assert plan["estimated_changed_measurements"] >= 2
    result = build_delta(runtime_root=runtime, confirmation=CONFIRMATION)
    assert result["status"] == "ok"
    assert result["active_snapshot_pointer_changed"] is False
    assert pointer.read_bytes() == before
    verified = verify_candidate(runtime_root=runtime)
    assert verified["status"] == "ok"
    assert verified["logical_row_counts"]["air_measurements"] == 2
    assert verified["logical_row_counts"]["weather_measurements"] == 2
    with open_layered_connection(runtime_root=runtime) as connection:
        assert connection.execute("SELECT value FROM air_measurements WHERE id=2").fetchone()[0] == 11.0
    provenance = layered_candidate_provenance(runtime_root=runtime)
    assert provenance["storage_mode"] == "layered_candidate"
    assert provenance["delta_count"] == 1
    assert len(provenance["chain_sha256"]) == 64
    preflight = fast_preflight_candidate(runtime_root=runtime)
    assert preflight["status"] == "ready"
    assert preflight["journal_current"] is True


def test_second_delta_overlays_updates_inserts_and_deletes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    build_delta(runtime_root=runtime, confirmation=CONFIRMATION)
    with sqlite3.connect(runtime / "data/smog.db") as connection:
        connection.execute("UPDATE air_measurements SET value=12.5 WHERE id=1")
        connection.execute("DELETE FROM air_measurements WHERE id=2")
        connection.execute(
            "INSERT INTO weather_measurements VALUES "
            "(3, 1, '2026-08-14 12:00:00', 22.0, 1)"
        )
        connection.commit()
    build_delta(runtime_root=runtime, confirmation=CONFIRMATION)
    verified = verify_candidate(runtime_root=runtime)
    assert verified["status"] == "ok"
    with open_layered_connection(runtime_root=runtime) as connection:
        assert connection.execute(
            "SELECT id, value FROM air_measurements ORDER BY id"
        ).fetchall() == [(1, 12.5)]
        assert connection.execute(
            "SELECT id FROM weather_measurements ORDER BY id"
        ).fetchall() == [(1,), (2,), (3,)]


def test_fast_preflight_blocks_uncaptured_live_change(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    build_delta(runtime_root=runtime, confirmation=CONFIRMATION)
    with sqlite3.connect(runtime / "data/smog.db") as connection:
        connection.execute("UPDATE air_measurements SET value=99 WHERE id=1")
        connection.commit()
    preflight = fast_preflight_candidate(runtime_root=runtime)
    assert preflight["status"] == "blocked"
    assert preflight["journal_current"] is False
    assert any(
        item["reason"] == "live_changes_not_captured_by_delta"
        for item in preflight["errors"]
    )
