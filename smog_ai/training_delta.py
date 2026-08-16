from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


MEASUREMENT_TABLES = ("air_measurements", "weather_measurements")
DIMENSION_TABLES = (
    "air_stations",
    "air_sensors",
    "weather_stations",
    "station_matches",
)
LAYERED_TABLES = (*MEASUREMENT_TABLES, *DIMENSION_TABLES)
CHANGE_JOURNAL = "_smogai_delta_changes"
TOMBSTONES = "_smogai_delta_tombstones"
CONFIRMATION = "BUILD VERIFIED TRAINING DELTA"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).strftime("%Y%m%dT%H%M%SZ")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object expected: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=30.0,
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
            (table,),
        ).fetchone()
        is not None
    ):
        return True
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type IN ('table', 'view') AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Required table is missing: {table}")
    return str(row[0])


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _max_id(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    row = connection.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"').fetchone()
    return int(row[0] or 0)


def _count(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _journal_exists(connection: sqlite3.Connection) -> bool:
    return _table_exists(connection, CHANGE_JOURNAL)


def _journal_max(connection: sqlite3.Connection) -> int:
    if not _journal_exists(connection):
        return 0
    return int(
        connection.execute(
            f'SELECT COALESCE(MAX(seq), 0) FROM "{CHANGE_JOURNAL}"'
        ).fetchone()[0]
        or 0
    )


def _install_change_journal(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{CHANGE_JOURNAL}" (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            row_id INTEGER NOT NULL,
            operation TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f'CREATE INDEX IF NOT EXISTS ix_smogai_delta_changes_table_seq '
        f'ON "{CHANGE_JOURNAL}" (table_name, seq)'
    )
    for table in LAYERED_TABLES:
        if not _table_exists(connection, table):
            continue
        for operation, reference, token in (
            ("INSERT", "NEW", "insert"),
            ("UPDATE", "NEW", "update"),
            ("DELETE", "OLD", "delete"),
        ):
            trigger = f"smogai_delta_{table}_{token}"
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS "{trigger}"
                AFTER {operation} ON "{table}"
                BEGIN
                    INSERT INTO "{CHANGE_JOURNAL}"
                        (table_name, row_id, operation, changed_at)
                    VALUES
                        ('{table}', {reference}.id, '{token}',
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
                END
                """
            )
    connection.commit()


@dataclass(frozen=True, slots=True)
class DeltaContext:
    runtime_root: Path
    profile: str
    live_database: Path
    snapshot_pointer: Path
    base_manifest: Path
    base_database: Path
    base_dataset_id: str
    base_created_at: datetime
    chain_root: Path
    chain_pointer: Path
    delta_manifests: tuple[Path, ...]
    last_journal_seq: int


def _resolve_context(runtime_root: Path, profile: str) -> DeltaContext:
    runtime = runtime_root.expanduser().resolve()
    live_database = runtime / "data" / "smog.db"
    pointer = runtime / "training-datasets" / profile / "latest.json"
    if not live_database.is_file():
        raise FileNotFoundError(f"Live database is missing: {live_database}")
    if not pointer.is_file():
        raise FileNotFoundError(f"Training snapshot pointer is missing: {pointer}")

    pointer_payload = _read_json(pointer)
    manifest = Path(str(pointer_payload.get("manifest_path") or ""))
    database = Path(str(pointer_payload.get("database_path") or ""))
    dataset_id = str(pointer_payload.get("dataset_id") or "").strip()
    if not dataset_id:
        raise ValueError(f"dataset_id is missing in pointer: {pointer}")
    if not manifest.is_file():
        raise FileNotFoundError(f"Active snapshot manifest is missing: {manifest}")
    manifest_payload = _read_json(manifest)
    if not database.is_file():
        database = Path(str(manifest_payload.get("database_path") or ""))
    if not database.is_file():
        raise FileNotFoundError(f"Active snapshot database is missing: {database}")
    if str(manifest_payload.get("dataset_id") or "") != dataset_id:
        raise ValueError("Active pointer and manifest dataset_id differ")
    created_at = datetime.fromisoformat(str(manifest_payload["created_at"]))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    created_at = created_at.astimezone(UTC)

    chain_root = (
        runtime
        / "training-datasets"
        / "_incremental"
        / profile
        / f"base-{dataset_id}"
    )
    chain_pointer = chain_root / "latest.json"
    manifests: list[Path] = []
    last_seq = 0
    if chain_pointer.is_file():
        chain_payload = _read_json(chain_pointer)
        if str(chain_payload.get("base_dataset_id") or "") != dataset_id:
            raise RuntimeError("Incremental chain points to a different active base")
        for value in chain_payload.get("delta_manifests") or []:
            selected = Path(str(value))
            if not selected.is_file():
                raise FileNotFoundError(f"Delta manifest is missing: {selected}")
            manifests.append(selected)
        last_seq = int(chain_payload.get("journal_end_seq") or 0)

    return DeltaContext(
        runtime_root=runtime,
        profile=profile,
        live_database=live_database,
        snapshot_pointer=pointer,
        base_manifest=manifest,
        base_database=database,
        base_dataset_id=dataset_id,
        base_created_at=created_at,
        chain_root=chain_root,
        chain_pointer=chain_pointer,
        delta_manifests=tuple(manifests),
        last_journal_seq=last_seq,
    )


def _base_max_ids(context: DeltaContext) -> dict[str, int]:
    with closing(_connect_read_only(context.base_database)) as connection:
        return {table: _max_id(connection, table) for table in LAYERED_TABLES}


def _changed_ids_from_journal(
    connection: sqlite3.Connection,
    *,
    start_seq: int,
    end_seq: int,
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    changed = {table: set() for table in LAYERED_TABLES}
    deleted = {table: set() for table in LAYERED_TABLES}
    if not _journal_exists(connection) or end_seq <= start_seq:
        return changed, deleted
    rows = connection.execute(
        f"""
        SELECT table_name, row_id, operation
          FROM "{CHANGE_JOURNAL}"
         WHERE seq > ? AND seq <= ?
         ORDER BY seq
        """,
        (start_seq, end_seq),
    )
    for table, row_id, operation in rows:
        name = str(table)
        if name not in changed:
            continue
        identifier = int(row_id)
        if str(operation) == "delete":
            deleted[name].add(identifier)
            changed[name].discard(identifier)
        else:
            changed[name].add(identifier)
            deleted[name].discard(identifier)
    return changed, deleted


def _bootstrap_measurement_ids(
    connection: sqlite3.Connection,
    table: str,
    base_max_id: int,
    recent_since: datetime,
) -> set[int]:
    ids = {
        int(row[0])
        for row in connection.execute(
            f'SELECT id FROM "{table}" WHERE id > ?',
            (base_max_id,),
        )
    }
    # The indexed measurement_time overlap catches recent corrections whose
    # primary key predates the base. Historical inserts remain covered by id.
    ids.update(
        int(row[0])
        for row in connection.execute(
            f'SELECT id FROM "{table}" WHERE measurement_time >= ?',
            (recent_since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
    )
    return ids


def plan_delta(
    *,
    runtime_root: Path,
    profile: str = "quick",
    max_deltas_before_compaction: int = 6,
    max_delta_fraction: float = 0.25,
) -> dict[str, Any]:
    context = _resolve_context(runtime_root, profile)
    base_ids = _base_max_ids(context)
    manifests = [_read_json(path) for path in context.delta_manifests]
    delta_bytes = sum(int(item.get("database_size_bytes") or 0) for item in manifests)
    base_bytes = context.base_database.stat().st_size

    with closing(_connect_read_only(context.live_database)) as live:
        journal_present = _journal_exists(live)
        journal_end = _journal_max(live)
        changed_by_journal, deleted_by_journal = _changed_ids_from_journal(
            live,
            start_seq=context.last_journal_seq,
            end_seq=journal_end,
        )
        recent_since = context.base_created_at - timedelta(minutes=30)
        estimated: dict[str, int] = {}
        for table in MEASUREMENT_TABLES:
            if context.delta_manifests and journal_present:
                estimated[table] = len(changed_by_journal[table])
            else:
                estimated[table] = len(
                    _bootstrap_measurement_ids(
                        live,
                        table,
                        base_ids[table],
                        recent_since,
                    )
                )
        for table in DIMENSION_TABLES:
            estimated[table] = _count(live, table)
        deleted_count = sum(len(values) for values in deleted_by_journal.values())

    compaction_reasons: list[str] = []
    if len(context.delta_manifests) >= max(1, max_deltas_before_compaction):
        compaction_reasons.append("maximum_delta_count")
    if base_bytes > 0 and delta_bytes / base_bytes >= max_delta_fraction:
        compaction_reasons.append("maximum_delta_fraction")

    return {
        "status": "ready",
        "mode": "plan",
        "profile": profile,
        "active_snapshot_unchanged": True,
        "production_pointer_unchanged": True,
        "base_dataset_id": context.base_dataset_id,
        "base_database": str(context.base_database),
        "base_database_size_bytes": base_bytes,
        "chain_root": str(context.chain_root),
        "existing_delta_count": len(context.delta_manifests),
        "existing_delta_bytes": delta_bytes,
        "journal_present": journal_present,
        "journal_start_seq": context.last_journal_seq,
        "journal_end_seq": journal_end,
        "bootstrap_mode": not context.chain_pointer.is_file(),
        "estimated_rows": estimated,
        "estimated_changed_measurements": sum(
            estimated.get(table, 0) for table in MEASUREMENT_TABLES
        ),
        "estimated_dimension_rows": sum(
            estimated.get(table, 0) for table in DIMENSION_TABLES
        ),
        "estimated_tombstones": deleted_count,
        "compaction_due": bool(compaction_reasons),
        "compaction_reasons": compaction_reasons,
        "next_action": "compact" if compaction_reasons else "build_delta",
        "safety": {
            "writes_live_data_rows": False,
            "installs_change_journal_only_on_apply": True,
            "changes_active_snapshot_pointer": False,
            "deletes_snapshot_or_delta": False,
            "requires_disabled_scheduled_task": True,
        },
    }


def _copy_selected_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    ids: Iterable[int] | None,
) -> int:
    columns = _columns(source, table)
    if not columns:
        return 0
    quoted_columns = ", ".join(f'"{name}"' for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})'
    copied = 0

    if ids is None:
        cursor = source.execute(f'SELECT {quoted_columns} FROM "{table}"')
        while True:
            rows = cursor.fetchmany(5000)
            if not rows:
                break
            target.executemany(insert_sql, rows)
            copied += len(rows)
        return copied

    selected = sorted(set(int(value) for value in ids))
    for offset in range(0, len(selected), 500):
        chunk = selected[offset : offset + 500]
        marks = ",".join("?" for _ in chunk)
        rows = source.execute(
            f'SELECT {quoted_columns} FROM "{table}" WHERE id IN ({marks})',
            chunk,
        ).fetchall()
        target.executemany(insert_sql, rows)
        copied += len(rows)
    return copied


def _delta_quality(connection: sqlite3.Connection) -> dict[str, Any]:
    hard: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    row_counts: dict[str, int] = {}

    quick = connection.execute("PRAGMA quick_check").fetchone()
    integrity = str(quick[0] if quick else "unknown")
    if integrity != "ok":
        hard.append({"code": "sqlite_quick_check", "detail": integrity})

    for table in LAYERED_TABLES:
        if not _table_exists(connection, table):
            hard.append({"code": "missing_table", "table": table})
            continue
        count = _count(connection, table)
        row_counts[table] = count
        duplicate_ids = int(
            connection.execute(
                f'SELECT COUNT(*) FROM (SELECT id FROM "{table}" GROUP BY id HAVING COUNT(*) > 1)'
            ).fetchone()[0]
        )
        if duplicate_ids:
            hard.append(
                {"code": "duplicate_primary_id", "table": table, "count": duplicate_ids}
            )

    if _table_exists(connection, "air_measurements"):
        orphan_air = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM air_measurements m
                LEFT JOIN air_stations s ON s.id=m.air_station_id
                WHERE s.id IS NULL
                """
            ).fetchone()[0]
        )
        if orphan_air:
            hard.append({"code": "orphan_air_station", "count": orphan_air})
        null_air = int(
            connection.execute(
                "SELECT COUNT(*) FROM air_measurements WHERE is_valid=1 AND value IS NULL"
            ).fetchone()[0]
        )
        if null_air:
            warnings.append({"code": "valid_air_value_null", "count": null_air})

    if _table_exists(connection, "weather_measurements"):
        orphan_weather = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM weather_measurements m
                LEFT JOIN weather_stations s ON s.id=m.weather_station_id
                WHERE s.id IS NULL
                """
            ).fetchone()[0]
        )
        if orphan_weather:
            hard.append({"code": "orphan_weather_station", "count": orphan_weather})

    for table in ("air_stations", "weather_stations"):
        if _table_exists(connection, table):
            invalid_coordinates = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM "{table}"
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                      AND (latitude < -90 OR latitude > 90 OR longitude < -180 OR longitude > 180)
                    """
                ).fetchone()[0]
            )
            if invalid_coordinates:
                hard.append(
                    {
                        "code": "invalid_coordinates",
                        "table": table,
                        "count": invalid_coordinates,
                    }
                )

    return {
        "status": "failed" if hard else "ok",
        "integrity_check": integrity,
        "row_counts": row_counts,
        "hard_failures": hard,
        "warnings": warnings,
    }


def build_delta(
    *,
    runtime_root: Path,
    profile: str = "quick",
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != CONFIRMATION:
        raise PermissionError(f"Exact confirmation required: {CONFIRMATION}")

    context = _resolve_context(runtime_root, profile)
    before_pointer = context.snapshot_pointer.read_bytes()
    base_ids = _base_max_ids(context)
    created_at = _utc_now()
    sequence = len(context.delta_manifests) + 1
    delta_id = f"delta-{sequence:04d}-{_stamp(created_at)}-{uuid.uuid4().hex[:8]}"
    context.chain_root.mkdir(parents=True, exist_ok=True)
    final_directory = context.chain_root / delta_id
    temporary_directory = context.chain_root / f".{delta_id}.partial"
    if final_directory.exists() or temporary_directory.exists():
        raise FileExistsError(f"Delta directory already exists: {final_directory}")
    temporary_directory.mkdir(parents=True)
    partial_database = temporary_directory / "delta.db.partial"
    final_database_in_temporary = temporary_directory / "delta.db"
    manifest_in_temporary = temporary_directory / "manifest.json"

    copied: dict[str, int] = {}
    deleted: dict[str, int] = {}
    try:
        with closing(
            sqlite3.connect(context.live_database, timeout=30.0)
        ) as source:
            source.execute("PRAGMA busy_timeout=30000")
            _install_change_journal(source)
            source.execute("BEGIN")
            journal_end = _journal_max(source)
            changed, tombstones = _changed_ids_from_journal(
                source,
                start_seq=context.last_journal_seq,
                end_seq=journal_end,
            )

            if not context.chain_pointer.is_file():
                recent_since = context.base_created_at - timedelta(minutes=30)
                for table in MEASUREMENT_TABLES:
                    changed[table].update(
                        _bootstrap_measurement_ids(
                            source,
                            table,
                            base_ids[table],
                            recent_since,
                        )
                    )

            with closing(sqlite3.connect(partial_database)) as target:
                target.execute("PRAGMA journal_mode=DELETE")
                target.execute("PRAGMA synchronous=FULL")
                target.execute("PRAGMA foreign_keys=OFF")
                for table in LAYERED_TABLES:
                    target.execute(_table_sql(source, table))
                target.execute(
                    f"""
                    CREATE TABLE "{TOMBSTONES}" (
                        table_name TEXT NOT NULL,
                        row_id INTEGER NOT NULL,
                        change_seq INTEGER NOT NULL,
                        PRIMARY KEY (table_name, row_id)
                    )
                    """
                )
                for table in MEASUREMENT_TABLES:
                    copied[table] = _copy_selected_rows(
                        source, target, table, changed[table]
                    )
                for table in DIMENSION_TABLES:
                    copied[table] = _copy_selected_rows(source, target, table, None)
                for table, identifiers in tombstones.items():
                    rows = [(table, int(identifier), journal_end) for identifier in identifiers]
                    if rows:
                        target.executemany(
                            f'INSERT OR REPLACE INTO "{TOMBSTONES}" '
                            '(table_name, row_id, change_seq) VALUES (?, ?, ?)',
                            rows,
                        )
                    deleted[table] = len(rows)
                target.commit()

                quality = _delta_quality(target)
                if quality["hard_failures"]:
                    raise RuntimeError(
                        "Delta quality hard failures: "
                        + json.dumps(quality["hard_failures"], ensure_ascii=False)
                    )
            source.rollback()

        os.replace(partial_database, final_database_in_temporary)
        checksum = _sha256(final_database_in_temporary)
        manifest = {
            "schema_version": "1.0",
            "storage_mode": "delta",
            "delta_id": delta_id,
            "sequence": sequence,
            "profile": profile,
            "created_at": created_at.isoformat(),
            "base_dataset_id": context.base_dataset_id,
            "base_manifest_path": str(context.base_manifest),
            "base_database_path": str(context.base_database),
            "previous_delta_manifest": (
                str(context.delta_manifests[-1]) if context.delta_manifests else None
            ),
            "database_path": str(final_directory / "delta.db"),
            "database_sha256": checksum,
            "database_size_bytes": final_database_in_temporary.stat().st_size,
            "journal_start_seq": context.last_journal_seq,
            "journal_end_seq": journal_end,
            "bootstrap_mode": not context.chain_pointer.is_file(),
            "copied_rows": copied,
            "tombstones": deleted,
            "quality": quality,
            "active_snapshot_pointer_changed": False,
            "immutable": True,
        }
        _atomic_json(manifest_in_temporary, manifest)
        os.replace(temporary_directory, final_directory)

        final_manifest = final_directory / "manifest.json"
        manifests = [*context.delta_manifests, final_manifest]
        chain_payload = {
            "schema_version": "1.0",
            "status": "candidate",
            "profile": profile,
            "base_dataset_id": context.base_dataset_id,
            "base_manifest_path": str(context.base_manifest),
            "base_database_path": str(context.base_database),
            "delta_manifests": [str(path) for path in manifests],
            "delta_count": len(manifests),
            "journal_end_seq": journal_end,
            "updated_at": _utc_now().isoformat(),
            "production_snapshot_pointer": str(context.snapshot_pointer),
            "production_snapshot_pointer_changed": False,
        }
        _atomic_json(context.chain_pointer, chain_payload)

        if context.snapshot_pointer.read_bytes() != before_pointer:
            raise RuntimeError("Production training snapshot pointer changed unexpectedly")

        return {
            "status": "ok",
            "mode": "apply",
            "profile": profile,
            "delta_id": delta_id,
            "delta_manifest": str(final_manifest),
            "delta_database": str(final_directory / "delta.db"),
            "delta_sha256": checksum,
            "delta_size_bytes": int(manifest["database_size_bytes"]),
            "copied_rows": copied,
            "tombstones": deleted,
            "quality": quality,
            "chain_pointer": str(context.chain_pointer),
            "chain_status": "candidate",
            "active_snapshot_pointer_changed": False,
            "active_models_changed": False,
            "scheduled_task_changed": False,
            "next_action": "verify_layered_candidate",
        }
    except BaseException:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def _manifest_chain(context: DeltaContext) -> list[dict[str, Any]]:
    return [_read_json(path) for path in context.delta_manifests]


def layered_candidate_provenance(
    *,
    runtime_root: Path,
    profile: str = "quick",
) -> dict[str, Any]:
    """Return an immutable, content-addressed description of base + deltas."""

    context = _resolve_context(runtime_root, profile)
    base = _read_json(context.base_manifest)
    deltas = _manifest_chain(context)
    base_sha256 = str(
        base.get("database_sha256")
        or base.get("dataset_sha256")
        or ""
    ).strip()
    if not base_sha256:
        raise ValueError(
            f"Base snapshot SHA-256 is missing: {context.base_manifest}"
        )
    delta_rows: list[dict[str, Any]] = []
    for manifest_path, manifest in zip(
        context.delta_manifests,
        deltas,
        strict=True,
    ):
        database_path = Path(str(manifest.get("database_path") or ""))
        database_sha256 = str(manifest.get("database_sha256") or "").strip()
        if not database_path.is_file():
            raise FileNotFoundError(f"Delta database is missing: {database_path}")
        if not database_sha256:
            raise ValueError(f"Delta SHA-256 is missing: {manifest_path}")
        delta_rows.append(
            {
                "delta_id": manifest.get("delta_id"),
                "sequence": manifest.get("sequence"),
                "manifest_path": str(manifest_path),
                "database_path": str(database_path),
                "database_sha256": database_sha256,
                "journal_start_seq": manifest.get("journal_start_seq"),
                "journal_end_seq": manifest.get("journal_end_seq"),
            }
        )
    identity = {
        "base_dataset_id": context.base_dataset_id,
        "base_database_sha256": base_sha256,
        "deltas": [
            {
                "delta_id": row["delta_id"],
                "sequence": row["sequence"],
                "database_sha256": row["database_sha256"],
            }
            for row in delta_rows
        ],
    }
    chain_sha256 = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "1.0",
        "storage_mode": "layered_candidate",
        "dataset_id": (
            f"layered:{context.base_dataset_id}+{len(delta_rows)}d:"
            f"{chain_sha256[:12]}"
        ),
        "profile": profile,
        "base_dataset_id": context.base_dataset_id,
        "base_manifest_path": str(context.base_manifest),
        "base_database_path": str(context.base_database),
        "base_database_sha256": base_sha256,
        "delta_count": len(delta_rows),
        "deltas": delta_rows,
        "chain_pointer": str(context.chain_pointer),
        "chain_sha256": chain_sha256,
        # Compatibility with existing immutable snapshot publication gates.
        # This is the logical chain hash, not a hash of a materialised DB file.
        "database_sha256": chain_sha256,
        "dataset_sha256": chain_sha256,
        "immutable": True,
        "production_pointer_changed": False,
    }


def fast_preflight_candidate(
    *,
    runtime_root: Path,
    profile: str = "quick",
) -> dict[str, Any]:
    """Fast Apply-time check after a previously completed full verification.

    Once the journal triggers are installed, any tracked table mutation raises
    the journal sequence.  Equality with the chain pointer therefore proves
    that no new tracked change appeared since the last delta, without scanning
    ten million base rows for a second time.
    """

    context = _resolve_context(runtime_root, profile)
    provenance = layered_candidate_provenance(
        runtime_root=runtime_root,
        profile=profile,
    )
    errors: list[dict[str, Any]] = []
    checked_deltas: list[dict[str, Any]] = []
    for manifest_path in context.delta_manifests:
        manifest = _read_json(manifest_path)
        database_path = Path(str(manifest.get("database_path") or ""))
        expected = str(manifest.get("database_sha256") or "")
        actual = _sha256(database_path) if database_path.is_file() else None
        item = {
            "delta_id": manifest.get("delta_id"),
            "database_path": str(database_path),
            "expected_sha256": expected,
            "actual_sha256": actual,
            "match": bool(actual and actual == expected),
        }
        checked_deltas.append(item)
        if not item["match"]:
            errors.append({**item, "reason": "delta_sha256_mismatch"})

    with closing(_connect_read_only(context.live_database)) as live:
        journal_present = _journal_exists(live)
        live_journal_seq = _journal_max(live) if journal_present else None
    chain_journal_seq = context.last_journal_seq
    journal_current = bool(
        journal_present
        and live_journal_seq is not None
        and int(live_journal_seq) == int(chain_journal_seq)
    )
    if not journal_current:
        errors.append(
            {
                "reason": "live_changes_not_captured_by_delta",
                "journal_present": journal_present,
                "live_journal_seq": live_journal_seq,
                "chain_journal_seq": chain_journal_seq,
            }
        )
    ready = bool(context.delta_manifests) and not errors
    return {
        "status": "ready" if ready else "blocked",
        "mode": "fast_preflight",
        "profile": profile,
        "dataset_id": provenance["dataset_id"],
        "chain_sha256": provenance["chain_sha256"],
        "delta_count": len(context.delta_manifests),
        "checked_deltas": checked_deltas,
        "journal_present": journal_present,
        "live_journal_seq": live_journal_seq,
        "chain_journal_seq": chain_journal_seq,
        "journal_current": journal_current,
        "errors": errors,
        "full_verification_repeated": False,
        "production_pointer_changed": False,
    }


def _fingerprint_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    limit: int | None,
) -> dict[str, Any]:
    columns = _columns(connection, table)
    if not columns:
        return {"rows": 0, "sha256": None, "columns": []}
    sql = f'SELECT * FROM "{table}" ORDER BY id'
    if limit is not None:
        sql = f'SELECT * FROM "{table}" ORDER BY id DESC LIMIT {int(limit)}'
    digest = hashlib.sha256()
    rows = 0
    cursor = connection.execute(sql)
    while True:
        batch = cursor.fetchmany(1000)
        if not batch:
            break
        for row in batch:
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            digest.update(b"\n")
            rows += 1
    return {"rows": rows, "sha256": digest.hexdigest(), "columns": columns}


def _table_comparison_summary(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    count = _count(connection, table)
    maximum_id = _max_id(connection, table)
    summary: dict[str, Any] = {"rows": count, "max_id": maximum_id}
    columns = set(_columns(connection, table))
    if "measurement_time" in columns:
        start, end = connection.execute(
            f'SELECT MIN(measurement_time), MAX(measurement_time) FROM "{table}"'
        ).fetchone()
        summary["measurement_start"] = start
        summary["measurement_end"] = end
        summary["sample"] = _fingerprint_rows(connection, table, limit=2000)
    else:
        summary["sample"] = _fingerprint_rows(connection, table, limit=None)
    return summary


def _compare_layered_to_live(
    *,
    context: DeltaContext,
    layered: sqlite3.Connection,
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    mismatches: list[dict[str, Any]] = []
    with closing(_connect_read_only(context.live_database)) as live:
        for table in LAYERED_TABLES:
            live_summary = _table_comparison_summary(live, table)
            layered_summary = _table_comparison_summary(layered, table)
            item = {
                "live": live_summary,
                "layered": layered_summary,
                "rows_match": live_summary["rows"] == layered_summary["rows"],
                "max_id_match": live_summary["max_id"] == layered_summary["max_id"],
                "sample_sha256_match": (
                    live_summary["sample"]["sha256"]
                    == layered_summary["sample"]["sha256"]
                ),
            }
            if "measurement_end" in live_summary:
                item["measurement_end_match"] = (
                    live_summary.get("measurement_end")
                    == layered_summary.get("measurement_end")
                )
            item["match"] = all(
                bool(item[key])
                for key in item
                if key.endswith("_match")
            )
            tables[table] = item
            if not item["match"]:
                mismatches.append(
                    {
                        "table": table,
                        "live_rows": live_summary["rows"],
                        "layered_rows": layered_summary["rows"],
                        "live_max_id": live_summary["max_id"],
                        "layered_max_id": layered_summary["max_id"],
                        "sample_sha256_match": item["sample_sha256_match"],
                    }
                )
    return {
        "status": "ok" if not mismatches else "mismatch",
        "tables": tables,
        "mismatches": mismatches,
    }


def create_layered_sqlalchemy_engine(
    *,
    runtime_root: Path,
    profile: str = "quick",
) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    return create_engine(
        "sqlite://",
        creator=lambda: open_layered_connection(
            runtime_root=runtime_root,
            profile=profile,
        ),
        poolclass=NullPool,
        future=True,
    )


def _feature_smoke(
    *,
    runtime_root: Path,
    profile: str,
) -> dict[str, Any]:
    context = _resolve_context(runtime_root, profile)
    with closing(open_layered_connection(runtime_root=runtime_root, profile=profile)) as raw:
        air_columns = set(_columns(raw, "air_measurements"))
        weather_columns = set(_columns(raw, "weather_measurements"))
    required_air = {"parameter", "measurement_time", "value", "is_valid"}
    required_weather = {
        "measurement_time",
        "temperature_c",
        "precipitation_mm",
        "is_valid",
    }
    if not required_air.issubset(air_columns) or not required_weather.issubset(
        weather_columns
    ):
        return {"status": "skipped_schema", "reason": "test schema lacks production columns"}

    engine = create_layered_sqlalchemy_engine(
        runtime_root=runtime_root,
        profile=profile,
    )
    try:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from smog_ai.features.builder import _air_frame, _weather_frame

        with Session(engine) as session:
            parameters = [
                str(row[0])
                for row in session.execute(
                    text(
                        "SELECT parameter FROM air_measurements "
                        "WHERE is_valid=1 AND value IS NOT NULL "
                        "GROUP BY parameter ORDER BY COUNT(*) DESC LIMIT 20"
                    )
                ).all()
            ]
            preferred = next(
                (item for item in parameters if item.upper() in {"PM10", "PM2.5", "PM2_5"}),
                parameters[0] if parameters else None,
            )
            if preferred is None:
                return {"status": "failed", "error": "no valid air parameter"}
            air = _air_frame(session, preferred, 2)
            weather = _weather_frame(session, 2)
            result = {
                "status": "ok" if not air.empty and not weather.empty else "failed",
                "parameter": preferred,
                "max_days": 2,
                "air_feature_rows": int(len(air)),
                "air_feature_columns": list(air.columns),
                "weather_feature_rows": int(len(weather)),
                "weather_feature_columns": list(weather.columns),
                "base_dataset_id": context.base_dataset_id,
            }
            if air.empty:
                result["air_error"] = "air feature frame is empty"
            if weather.empty:
                result["weather_error"] = "weather feature frame is empty"
            return result
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        engine.dispose()


def _attach(connection: sqlite3.Connection, path: Path, alias: str) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection.execute(f'ATTACH DATABASE ? AS "{alias}"', (uri,))


def open_layered_connection(
    *,
    runtime_root: Path,
    profile: str = "quick",
) -> sqlite3.Connection:
    context = _resolve_context(runtime_root, profile)
    manifests = _manifest_chain(context)
    connection = sqlite3.connect(":memory:", uri=True, timeout=30.0)
    connection.execute("PRAGMA query_only=OFF")
    _attach(connection, context.base_database, "base")
    aliases: list[str] = []
    for index, manifest in enumerate(manifests, start=1):
        path = Path(str(manifest["database_path"]))
        if not path.is_file():
            connection.close()
            raise FileNotFoundError(f"Delta database is missing: {path}")
        alias = f"d{index}"
        _attach(connection, path, alias)
        aliases.append(alias)

    for table in LAYERED_TABLES:
        parts: list[str] = []
        sources = ["base", *aliases]
        for index, alias in enumerate(sources):
            later = aliases[index:] if alias == "base" else aliases[index:]
            exclusions: list[str] = []
            for newer in later:
                exclusions.append(
                    f'NOT EXISTS (SELECT 1 FROM "{newer}"."{table}" n '
                    f'WHERE n.id = src.id)'
                )
                exclusions.append(
                    f'NOT EXISTS (SELECT 1 FROM "{newer}"."{TOMBSTONES}" t '
                    f"WHERE t.table_name = '{table}' AND t.row_id = src.id)"
                )
            where = " WHERE " + " AND ".join(exclusions) if exclusions else ""
            parts.append(f'SELECT src.* FROM "{alias}"."{table}" src{where}')
        statement = " UNION ALL ".join(parts)
        connection.execute(f'CREATE TEMP VIEW "{table}" AS {statement}')
    connection.execute("PRAGMA query_only=ON")
    return connection


def verify_candidate(
    *,
    runtime_root: Path,
    profile: str = "quick",
    verify_hashes: bool = True,
) -> dict[str, Any]:
    context = _resolve_context(runtime_root, profile)
    manifests = _manifest_chain(context)
    errors: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    for manifest in manifests:
        path = Path(str(manifest.get("database_path") or ""))
        item = {
            "delta_id": manifest.get("delta_id"),
            "database_path": str(path),
            "exists": path.is_file(),
        }
        if not path.is_file():
            errors.append({**item, "error": "database_missing"})
            continue
        with closing(_connect_read_only(path)) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            item["integrity_check"] = str(quick[0] if quick else "unknown")
        if item["integrity_check"] != "ok":
            errors.append({**item, "error": "quick_check_failed"})
            continue
        if verify_hashes:
            actual = _sha256(path)
            item["sha256"] = actual
            if actual != str(manifest.get("database_sha256") or ""):
                errors.append({**item, "error": "sha256_mismatch"})
                continue
        verified.append(item)

    logical_counts: dict[str, int] = {}
    live_comparison: dict[str, Any] | None = None
    feature_smoke: dict[str, Any] | None = None
    if not errors:
        with closing(open_layered_connection(runtime_root=runtime_root, profile=profile)) as layered:
            for table in LAYERED_TABLES:
                logical_counts[table] = _count(layered, table)
            live_comparison = _compare_layered_to_live(
                context=context,
                layered=layered,
            )
        feature_smoke = _feature_smoke(
            runtime_root=runtime_root,
            profile=profile,
        )

    comparison_ok = bool(
        live_comparison and live_comparison.get("status") == "ok"
    )
    feature_ok = bool(
        feature_smoke
        and feature_smoke.get("status") in {"ok", "skipped_schema"}
    )
    ready = not errors and bool(manifests) and comparison_ok and feature_ok

    return {
        "status": "ok" if ready else "failed",
        "mode": "verify",
        "profile": profile,
        "base_dataset_id": context.base_dataset_id,
        "delta_count": len(manifests),
        "verified_deltas": verified,
        "logical_row_counts": logical_counts,
        "live_comparison": live_comparison,
        "feature_smoke": feature_smoke,
        "errors": errors,
        "production_snapshot_pointer_changed": False,
        "candidate_ready_for_training_integration": ready,
        "next_action": "integrate_candidate_with_training" if ready else "repair_or_refresh_candidate",
    }
