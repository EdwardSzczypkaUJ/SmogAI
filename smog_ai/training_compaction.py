from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smog_ai.training_delta import (
    LAYERED_TABLES,
    TOMBSTONES,
    _atomic_json,
    _columns,
    _connect_read_only,
    _journal_max,
    _read_json,
    _resolve_context,
    _sha256,
    _table_comparison_summary,
    fast_preflight_candidate,
    open_layered_connection,
)


COMPACTION_CONFIRMATION = "COMPACT VERIFIED TRAINING CHAIN"
ROLLBACK_CONFIRMATION = "ROLL BACK TRAINING COMPACTION"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).strftime("%Y%m%dT%H%M%SZ")


def _generation_root(runtime: Path, profile: str) -> Path:
    return runtime / "training-datasets" / "_compaction" / profile


def _chain_payload_sha(context: Any) -> str | None:
    return _sha256(context.chain_pointer) if context.chain_pointer.is_file() else None


def _comparison(left: sqlite3.Connection, right: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for table in LAYERED_TABLES:
        expected = _table_comparison_summary(left, table)
        actual = _table_comparison_summary(right, table)
        checks = {
            "rows": expected.get("rows") == actual.get("rows"),
            "max_id": expected.get("max_id") == actual.get("max_id"),
            "sample_sha256": (
                expected.get("sample", {}).get("sha256")
                == actual.get("sample", {}).get("sha256")
            ),
            "measurement_start": (
                expected.get("measurement_start") == actual.get("measurement_start")
            ),
            "measurement_end": (
                expected.get("measurement_end") == actual.get("measurement_end")
            ),
        }
        match = all(checks.values())
        tables[table] = {
            "expected": expected,
            "actual": actual,
            "checks": checks,
            "match": match,
        }
        if not match:
            errors.append({"table": table, "checks": checks})
    return {"status": "ok" if not errors else "mismatch", "tables": tables, "errors": errors}


def _data_ranges(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for table in LAYERED_TABLES:
        columns = set(_columns(connection, table))
        item: dict[str, Any] = {"rows": int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])}
        if "measurement_time" in columns:
            start, end = connection.execute(
                f'SELECT MIN(measurement_time), MAX(measurement_time) FROM "{table}"'
            ).fetchone()
            item.update({"start": start, "end": end})
        result[table] = item
    return result


def plan_compaction(*, runtime_root: Path, profile: str = "quick") -> dict[str, Any]:
    context = _resolve_context(runtime_root, profile)
    preflight = fast_preflight_candidate(runtime_root=runtime_root, profile=profile)
    delta_bytes = sum(
        int(_read_json(path).get("database_size_bytes") or 0)
        for path in context.delta_manifests
    )
    base_bytes = context.base_database.stat().st_size
    free_bytes = shutil.disk_usage(context.runtime_root).free
    required_free = int((base_bytes + delta_bytes) * 1.15) + 512 * 1024 * 1024
    blockers: list[str] = []
    if not context.delta_manifests:
        blockers.append("no_deltas")
    if preflight.get("status") != "ready":
        blockers.append("candidate_preflight_failed")
    if free_bytes < required_free:
        blockers.append("insufficient_free_space")
    return {
        "status": "ready" if not blockers else "blocked",
        "mode": "plan",
        "profile": profile,
        "base_dataset_id": context.base_dataset_id,
        "base_database": str(context.base_database),
        "base_bytes": base_bytes,
        "delta_count": len(context.delta_manifests),
        "delta_bytes": delta_bytes,
        "journal_boundary": context.last_journal_seq,
        "chain_sha256": _chain_payload_sha(context),
        "free_bytes": free_bytes,
        "required_free_bytes": required_free,
        "preflight": preflight,
        "blockers": blockers,
        "required_confirmation": COMPACTION_CONFIRMATION,
        "safety": {
            "plan_is_read_only": True,
            "deletes_old_base_or_deltas": False,
            "switch_after_full_verification": True,
            "rollback_generation_written_before_switch": True,
        },
        "next_action": "apply_compaction" if not blockers else "repair_blockers",
    }


def _apply_delta(database: sqlite3.Connection, delta_path: Path) -> dict[str, Any]:
    alias = "incoming_delta"
    database.execute("ATTACH DATABASE ? AS incoming_delta", (str(delta_path),))
    changed: dict[str, int] = {}
    deleted: dict[str, int] = {}
    try:
        for table in LAYERED_TABLES:
            identifiers = [
                int(row[0])
                for row in database.execute(
                    f'SELECT row_id FROM {alias}."{TOMBSTONES}" WHERE table_name=?',
                    (table,),
                )
            ]
            for offset in range(0, len(identifiers), 500):
                chunk = identifiers[offset : offset + 500]
                marks = ",".join("?" for _ in chunk)
                database.execute(f'DELETE FROM "{table}" WHERE id IN ({marks})', chunk)
            deleted[table] = len(identifiers)

            main_columns = _columns(database, table)
            delta_columns = [
                str(row[1])
                for row in database.execute(f'PRAGMA {alias}.table_info("{table}")')
            ]
            if main_columns != delta_columns:
                raise RuntimeError(f"Schema mismatch while compacting table {table}")
            quoted = ", ".join(f'"{column}"' for column in main_columns)
            database.execute(
                f'INSERT OR REPLACE INTO "{table}" ({quoted}) '
                f'SELECT {quoted} FROM {alias}."{table}"'
            )
            changed[table] = int(database.execute("SELECT changes()").fetchone()[0])
        database.commit()
    finally:
        database.execute(f"DETACH DATABASE {alias}")
    return {"database": str(delta_path), "changed": changed, "deleted": deleted}


def apply_compaction(
    *, runtime_root: Path, profile: str = "quick", confirmation: str
) -> dict[str, Any]:
    if confirmation != COMPACTION_CONFIRMATION:
        raise PermissionError(f"Exact confirmation required: {COMPACTION_CONFIRMATION}")
    plan = plan_compaction(runtime_root=runtime_root, profile=profile)
    if plan["status"] != "ready":
        raise RuntimeError("Compaction plan is blocked: " + ", ".join(plan["blockers"]))

    context = _resolve_context(runtime_root, profile)
    pointer_before = context.snapshot_pointer.read_bytes()
    chain_before = context.chain_pointer.read_bytes()
    old_manifest = _read_json(context.base_manifest)
    created = _now()
    compaction_id = f"compaction-{_stamp(created)}-{uuid.uuid4().hex[:8]}"
    dataset_id = f"training-compact-{_stamp(created)}-{uuid.uuid4().hex[:10]}"
    profile_root = context.runtime_root / "training-datasets" / profile
    final_directory = profile_root / f"dataset-{dataset_id}"
    temporary_directory = profile_root / f".dataset-{dataset_id}.partial"
    generation = _generation_root(context.runtime_root, profile) / "generations" / compaction_id
    generation.mkdir(parents=True, exist_ok=False)
    temporary_directory.mkdir(parents=True, exist_ok=False)
    partial_database = temporary_directory / "smog.db.partial"
    final_database = temporary_directory / "smog.db"
    final_manifest = final_directory / "manifest.json"
    journal_boundary = context.last_journal_seq
    delta_results: list[dict[str, Any]] = []

    rollback = {
        "schema_version": "1.0",
        "status": "prepared",
        "compaction_id": compaction_id,
        "profile": profile,
        "created_at": created.isoformat(),
        "old_snapshot_pointer_path": str(context.snapshot_pointer),
        "old_snapshot_pointer": json.loads(pointer_before.decode("utf-8-sig")),
        "old_snapshot_pointer_sha256": _sha256(context.snapshot_pointer),
        "old_chain_pointer_path": str(context.chain_pointer),
        "old_chain_pointer": json.loads(chain_before.decode("utf-8-sig")),
        "old_chain_pointer_sha256": _sha256(context.chain_pointer),
        "old_base_dataset_id": context.base_dataset_id,
        "old_base_manifest": str(context.base_manifest),
        "old_base_database": str(context.base_database),
        "old_delta_manifests": [str(path) for path in context.delta_manifests],
        "new_dataset_id": dataset_id,
        "new_dataset_directory": str(final_directory),
        "journal_boundary": journal_boundary,
    }
    _atomic_json(generation / "rollback.json", rollback)

    try:
        with closing(_connect_read_only(context.base_database)) as source, closing(
            sqlite3.connect(partial_database, timeout=60.0)
        ) as target:
            source.backup(target, pages=8192)
        os.replace(partial_database, final_database)

        with closing(sqlite3.connect(final_database, timeout=60.0)) as compacted:
            compacted.execute("PRAGMA foreign_keys=OFF")
            compacted.execute("PRAGMA journal_mode=DELETE")
            compacted.execute("PRAGMA synchronous=FULL")
            for manifest_path in context.delta_manifests:
                manifest = _read_json(manifest_path)
                delta_path = Path(str(manifest.get("database_path") or ""))
                if _sha256(delta_path) != str(manifest.get("database_sha256") or ""):
                    raise RuntimeError(f"Delta checksum mismatch: {delta_path}")
                delta_results.append(_apply_delta(compacted, delta_path))
            compacted.execute("VACUUM")
            integrity_row = compacted.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0] if integrity_row else "unknown")
            if integrity != "ok":
                raise RuntimeError(f"Compacted SQLite integrity failed: {integrity}")

        with closing(open_layered_connection(runtime_root=context.runtime_root, profile=profile)) as layered, closing(
            _connect_read_only(final_database)
        ) as compacted:
            comparison = _comparison(layered, compacted)
            row_counts = {
                table: int(compacted.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in LAYERED_TABLES
            }
            ranges = _data_ranges(compacted)
        if comparison["status"] != "ok":
            raise RuntimeError("Compacted database differs from layered candidate")

        checksum = _sha256(final_database)
        manifest = {
            **old_manifest,
            "schema_version": "1.2",
            "storage_mode": "compacted_snapshot",
            "dataset_id": dataset_id,
            "profile": profile,
            "created_at": created.isoformat(),
            "database_path": str(final_directory / "smog.db"),
            "manifest_path": str(final_manifest),
            "database_sha256": checksum,
            "database_size_bytes": final_database.stat().st_size,
            "source_database_path": str(context.base_database),
            "source_database_size_bytes": context.base_database.stat().st_size,
            "source_database_mtime_ns": context.base_database.stat().st_mtime_ns,
            "integrity_check": integrity,
            "row_counts": row_counts,
            "data_ranges": ranges,
            "compaction": {
                "compaction_id": compaction_id,
                "old_base_dataset_id": context.base_dataset_id,
                "old_chain_pointer": str(context.chain_pointer),
                "delta_manifests": [str(path) for path in context.delta_manifests],
                "delta_count": len(context.delta_manifests),
                "journal_boundary": journal_boundary,
                "comparison": comparison,
                "old_assets_retained": True,
            },
            "immutable": True,
        }
        _atomic_json(temporary_directory / "manifest.json", manifest)

        if context.snapshot_pointer.read_bytes() != pointer_before:
            raise RuntimeError("Snapshot pointer changed during compaction")
        if context.chain_pointer.read_bytes() != chain_before:
            raise RuntimeError("Delta chain changed during compaction")
        with closing(_connect_read_only(context.live_database)) as live:
            live_journal = _journal_max(live)
        if live_journal != journal_boundary:
            raise RuntimeError(
                f"Live journal advanced during compaction ({journal_boundary} -> {live_journal}); retry"
            )

        os.replace(temporary_directory, final_directory)
        new_chain_root = (
            context.runtime_root
            / "training-datasets"
            / "_incremental"
            / profile
            / f"base-{dataset_id}"
        )
        new_chain_pointer = new_chain_root / "latest.json"
        _atomic_json(
            new_chain_pointer,
            {
                "schema_version": "1.1",
                "status": "compacted_base",
                "profile": profile,
                "base_dataset_id": dataset_id,
                "base_manifest_path": str(final_manifest),
                "base_database_path": str(final_directory / "smog.db"),
                "delta_manifests": [],
                "delta_count": 0,
                "journal_end_seq": journal_boundary,
                "compaction_id": compaction_id,
                "updated_at": _now().isoformat(),
            },
        )
        new_pointer = {
            "schema_version": "1.1",
            "dataset_id": dataset_id,
            "profile": profile,
            "manifest_path": str(final_manifest),
            "database_path": str(final_directory / "smog.db"),
            "updated_at": _now().isoformat(),
            "compaction_id": compaction_id,
        }
        rollback.update(
            {
                "status": "verified_before_switch",
                "new_snapshot_pointer": new_pointer,
                "new_chain_pointer_path": str(new_chain_pointer),
                "new_database_sha256": checksum,
                "comparison": comparison,
            }
        )
        _atomic_json(generation / "rollback.json", rollback)

        protection_path = _generation_root(context.runtime_root, profile) / "protection.json"
        existing_protection = _read_json(protection_path) if protection_path.is_file() else {}
        protected_ids = set(existing_protection.get("protected_dataset_ids") or [])
        protected_ids.update({context.base_dataset_id, dataset_id})
        _atomic_json(
            protection_path,
            {
                "schema_version": "1.0",
                "updated_at": _now().isoformat(),
                "protected_dataset_ids": sorted(protected_ids),
                "reason": "compaction recovery generations and active model provenance",
            },
        )

        _atomic_json(context.snapshot_pointer, new_pointer)
        rollback["status"] = "switched"
        rollback["switched_at"] = _now().isoformat()
        _atomic_json(generation / "rollback.json", rollback)
        _atomic_json(
            _generation_root(context.runtime_root, profile) / "current.json",
            {
                "status": "switched",
                "compaction_id": compaction_id,
                "generation_record": str(generation / "rollback.json"),
                "old_dataset_id": context.base_dataset_id,
                "new_dataset_id": dataset_id,
                "updated_at": _now().isoformat(),
            },
        )
        return {
            "status": "ok",
            "mode": "apply",
            "compaction_id": compaction_id,
            "old_dataset_id": context.base_dataset_id,
            "new_dataset_id": dataset_id,
            "new_database": str(final_directory / "smog.db"),
            "new_database_sha256": checksum,
            "delta_count_compacted": len(context.delta_manifests),
            "journal_boundary": journal_boundary,
            "comparison_status": comparison["status"],
            "active_snapshot_pointer_changed": True,
            "old_base_and_deltas_retained": True,
            "rollback_record": str(generation / "rollback.json"),
            "next_action": "verify_compaction",
        }
    except BaseException as exc:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        rollback["status"] = "failed_before_switch"
        rollback["error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(generation / "rollback.json", rollback)
        raise


def verify_compaction(*, runtime_root: Path, profile: str = "quick") -> dict[str, Any]:
    context = _resolve_context(runtime_root, profile)
    current_path = _generation_root(context.runtime_root, profile) / "current.json"
    errors: list[str] = []
    if not current_path.is_file():
        errors.append("current_compaction_record_missing")
        current: dict[str, Any] = {}
    else:
        current = _read_json(current_path)
    generation_path = Path(str(current.get("generation_record") or ""))
    record = _read_json(generation_path) if generation_path.is_file() else {}
    if not record:
        errors.append("rollback_generation_missing")
    if str(record.get("new_dataset_id") or "") != context.base_dataset_id:
        errors.append("active_pointer_is_not_compacted_generation")
    expected_sha = str(record.get("new_database_sha256") or "")
    actual_sha = _sha256(context.base_database)
    if expected_sha != actual_sha:
        errors.append("compacted_database_sha256_mismatch")
    with closing(_connect_read_only(context.base_database)) as connection:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0] if integrity_row else "unknown")
    if integrity != "ok":
        errors.append("sqlite_integrity_failed")
    for key in ("old_base_database", "old_base_manifest"):
        if record and not Path(str(record.get(key) or "")).is_file():
            errors.append(f"{key}_missing")
    for value in record.get("old_delta_manifests") or []:
        manifest_path = Path(str(value))
        if not manifest_path.is_file():
            errors.append(f"old_delta_manifest_missing:{manifest_path}")
            continue
        try:
            old_delta = _read_json(manifest_path)
            old_delta_database = Path(str(old_delta.get("database_path") or ""))
            if not old_delta_database.is_file():
                errors.append(f"old_delta_database_missing:{old_delta_database}")
            elif _sha256(old_delta_database) != str(old_delta.get("database_sha256") or ""):
                errors.append(f"old_delta_database_sha256_mismatch:{old_delta_database}")
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"old_delta_manifest_invalid:{manifest_path}:{exc}")
    return {
        "status": "ok" if not errors else "failed",
        "mode": "verify",
        "profile": profile,
        "compaction_id": current.get("compaction_id"),
        "active_dataset_id": context.base_dataset_id,
        "database_sha256": actual_sha,
        "integrity_check": integrity,
        "old_assets_retained": not any("old_" in item for item in errors),
        "errors": errors,
        "next_action": "keep_recovery_generation" if not errors else "rollback_or_repair",
    }


def rollback_compaction(
    *, runtime_root: Path, profile: str = "quick", confirmation: str
) -> dict[str, Any]:
    if confirmation != ROLLBACK_CONFIRMATION:
        raise PermissionError(f"Exact confirmation required: {ROLLBACK_CONFIRMATION}")
    context = _resolve_context(runtime_root, profile)
    current_path = _generation_root(context.runtime_root, profile) / "current.json"
    if not current_path.is_file():
        raise FileNotFoundError("Current compaction record is missing")
    current = _read_json(current_path)
    generation_path = Path(str(current.get("generation_record") or ""))
    record = _read_json(generation_path)
    if context.base_dataset_id != str(record.get("new_dataset_id") or ""):
        raise RuntimeError("Active snapshot no longer matches the compacted generation")
    if context.delta_manifests:
        raise RuntimeError("Rollback blocked: new deltas already exist on compacted base")
    with closing(_connect_read_only(context.live_database)) as live:
        live_journal = _journal_max(live)
    boundary = int(record.get("journal_boundary") or 0)
    if live_journal != boundary:
        raise RuntimeError("Rollback blocked: live journal advanced after compaction")
    old_database = Path(str(record.get("old_base_database") or ""))
    old_manifest = Path(str(record.get("old_base_manifest") or ""))
    if not old_database.is_file() or not old_manifest.is_file():
        raise RuntimeError("Rollback assets are incomplete")
    _atomic_json(context.snapshot_pointer, dict(record["old_snapshot_pointer"]))
    record["status"] = "rolled_back"
    record["rolled_back_at"] = _now().isoformat()
    _atomic_json(generation_path, record)
    _atomic_json(
        current_path,
        {
            **current,
            "status": "rolled_back",
            "updated_at": _now().isoformat(),
        },
    )
    return {
        "status": "ok",
        "mode": "rollback",
        "compaction_id": record.get("compaction_id"),
        "restored_dataset_id": record.get("old_base_dataset_id"),
        "compacted_dataset_retained": True,
        "deleted_files": 0,
        "next_action": "verify_layered_candidate",
    }
