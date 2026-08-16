from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from smog_ai.training_compaction import (
    COMPACTION_CONFIRMATION,
    ROLLBACK_CONFIRMATION,
    apply_compaction,
    plan_compaction,
    rollback_compaction,
    verify_compaction,
)
from smog_ai.training_delta import LAYERED_TABLES, TOMBSTONES, _sha256


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _database(path: Path, *, updated: bool = False, delta: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        for table in LAYERED_TABLES:
            if table in {"air_measurements", "weather_measurements"}:
                connection.execute(
                    f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, measurement_time TEXT, value REAL)'
                )
            else:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, name TEXT)')
            if not delta:
                if table in {"air_measurements", "weather_measurements"}:
                    connection.execute(
                        f'INSERT INTO "{table}" VALUES (1, ?, ?)',
                        ("2026-08-14 00:00:00", 2.0 if updated else 1.0),
                    )
                else:
                    connection.execute(f'INSERT INTO "{table}" VALUES (1, ?)', (table,))
        if delta:
            connection.execute(
                f'CREATE TABLE "{TOMBSTONES}" (table_name TEXT, row_id INTEGER, change_seq INTEGER, PRIMARY KEY(table_name,row_id))'
            )
            connection.execute(
                'INSERT INTO air_measurements VALUES (1, ?, 2.0)',
                ("2026-08-14 00:00:00",),
            )
        else:
            connection.execute(
                'CREATE TABLE _smogai_delta_changes (seq INTEGER PRIMARY KEY, table_name TEXT, row_id INTEGER, operation TEXT, changed_at TEXT)'
            )
            if updated:
                connection.execute(
                    "INSERT INTO _smogai_delta_changes VALUES (1, 'air_measurements', 1, 'update', '2026-08-14T00:00:00Z')"
                )


@pytest.fixture()
def runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    live = runtime / "data" / "smog.db"
    _database(live, updated=True)
    base_dir = runtime / "training-datasets" / "quick" / "dataset-training-base"
    base = base_dir / "smog.db"
    _database(base)
    manifest = base_dir / "manifest.json"
    _write(
        manifest,
        {
            "schema_version": "1.1",
            "dataset_id": "training-base",
            "profile": "quick",
            "targets": ["PM10"],
            "created_at": "2026-08-14T00:00:00+00:00",
            "database_path": str(base),
            "manifest_path": str(manifest),
            "database_sha256": _sha256(base),
            "database_size_bytes": base.stat().st_size,
            "source_database_path": str(live),
            "source_database_size_bytes": live.stat().st_size,
            "source_database_mtime_ns": live.stat().st_mtime_ns,
            "integrity_check": "ok",
            "row_counts": {},
            "data_ranges": {},
            "immutable": True,
        },
    )
    pointer = runtime / "training-datasets" / "quick" / "latest.json"
    _write(
        pointer,
        {
            "dataset_id": "training-base",
            "profile": "quick",
            "manifest_path": str(manifest),
            "database_path": str(base),
        },
    )
    chain = runtime / "training-datasets" / "_incremental" / "quick" / "base-training-base"
    delta_dir = chain / "delta-0001"
    delta = delta_dir / "delta.db"
    _database(delta, delta=True)
    delta_manifest = delta_dir / "manifest.json"
    _write(
        delta_manifest,
        {
            "delta_id": "delta-0001",
            "sequence": 1,
            "database_path": str(delta),
            "database_sha256": _sha256(delta),
            "database_size_bytes": delta.stat().st_size,
            "journal_start_seq": 0,
            "journal_end_seq": 1,
        },
    )
    _write(
        chain / "latest.json",
        {
            "base_dataset_id": "training-base",
            "delta_manifests": [str(delta_manifest)],
            "delta_count": 1,
            "journal_end_seq": 1,
        },
    )
    return runtime


def test_plan_apply_verify_and_safe_rollback(runtime: Path) -> None:
    before = json.loads((runtime / "training-datasets" / "quick" / "latest.json").read_text())
    assert plan_compaction(runtime_root=runtime)["status"] == "ready"
    applied = apply_compaction(
        runtime_root=runtime,
        confirmation=COMPACTION_CONFIRMATION,
    )
    assert applied["status"] == "ok"
    assert applied["old_base_and_deltas_retained"] is True
    assert verify_compaction(runtime_root=runtime)["status"] == "ok"
    with sqlite3.connect(applied["new_database"]) as connection:
        assert connection.execute("SELECT value FROM air_measurements WHERE id=1").fetchone()[0] == 2.0
    rolled_back = rollback_compaction(
        runtime_root=runtime,
        confirmation=ROLLBACK_CONFIRMATION,
    )
    assert rolled_back["status"] == "ok"
    after = json.loads((runtime / "training-datasets" / "quick" / "latest.json").read_text())
    assert after["dataset_id"] == before["dataset_id"]
    assert Path(applied["new_database"]).is_file()


def test_wrong_confirmation_never_switches(runtime: Path) -> None:
    pointer = runtime / "training-datasets" / "quick" / "latest.json"
    before = pointer.read_bytes()
    with pytest.raises(PermissionError):
        apply_compaction(runtime_root=runtime, confirmation="wrong")
    assert pointer.read_bytes() == before
