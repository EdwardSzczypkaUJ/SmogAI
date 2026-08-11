from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.progress import ProgressReporter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrainingSnapshot:
    dataset_id: str
    profile: str
    targets: tuple[str, ...]
    created_at: datetime
    database_path: Path
    manifest_path: Path
    database_sha256: str
    database_size_bytes: int
    source_database_path: Path
    source_database_size_bytes: int
    source_database_mtime_ns: int
    integrity_check: str
    schema_version: str | None
    row_counts: dict[str, int]
    data_ranges: dict[str, dict[str, Any]]
    remote_manifest_key: str | None = None
    mirror_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "dataset_id": self.dataset_id,
            "profile": self.profile,
            "targets": list(self.targets),
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "database_path": str(self.database_path),
            "manifest_path": str(self.manifest_path),
            "database_sha256": self.database_sha256,
            "database_size_bytes": self.database_size_bytes,
            "source_database_path": str(self.source_database_path),
            "source_database_size_bytes": self.source_database_size_bytes,
            "source_database_mtime_ns": self.source_database_mtime_ns,
            "integrity_check": self.integrity_check,
            "alembic_schema_version": self.schema_version,
            "row_counts": dict(self.row_counts),
            "data_ranges": dict(self.data_ranges),
            "remote_manifest_key": self.remote_manifest_key,
            "mirror_error": self.mirror_error,
            "immutable": True,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TrainingSnapshot":
        return cls(
            dataset_id=str(payload["dataset_id"]),
            profile=str(payload.get("profile") or "quick"),
            targets=tuple(str(item) for item in payload.get("targets") or []),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            database_path=Path(str(payload["database_path"])),
            manifest_path=Path(str(payload["manifest_path"])),
            database_sha256=str(payload["database_sha256"]),
            database_size_bytes=int(payload.get("database_size_bytes") or 0),
            source_database_path=Path(str(payload["source_database_path"])),
            source_database_size_bytes=int(
                payload.get("source_database_size_bytes") or 0
            ),
            source_database_mtime_ns=int(
                payload.get("source_database_mtime_ns") or 0
            ),
            integrity_check=str(payload.get("integrity_check") or "unknown"),
            schema_version=(
                str(payload["alembic_schema_version"])
                if payload.get("alembic_schema_version") is not None
                else None
            ),
            row_counts={
                str(key): int(value)
                for key, value in (payload.get("row_counts") or {}).items()
            },
            data_ranges={
                str(key): dict(value)
                for key, value in (payload.get("data_ranges") or {}).items()
            },
            remote_manifest_key=(
                str(payload["remote_manifest_key"])
                if payload.get("remote_manifest_key")
                else None
            ),
            mirror_error=(
                str(payload["mirror_error"])
                if payload.get("mirror_error")
                else None
            ),
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _snapshot_stats(
    path: Path,
) -> tuple[str, str | None, dict[str, int], dict[str, dict[str, Any]]]:
    row_counts: dict[str, int] = {}
    data_ranges: dict[str, dict[str, Any]] = {}

    with closing(sqlite3.connect(path)) as connection:
        integrity_row = connection.execute("PRAGMA quick_check").fetchone()
        integrity = str(integrity_row[0] if integrity_row else "unknown")

        try:
            schema_row = connection.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
            schema_version = str(schema_row[0]) if schema_row else None
        except sqlite3.DatabaseError:
            schema_version = None

        try:
            air_rows = connection.execute(
                """
                SELECT parameter,
                       COUNT(*),
                       COUNT(DISTINCT air_station_id),
                       MIN(measurement_time),
                       MAX(measurement_time)
                  FROM air_measurements
                 WHERE is_valid = 1 AND value IS NOT NULL
                 GROUP BY parameter
                 ORDER BY parameter
                """
            )
        except sqlite3.DatabaseError:
            air_rows = []

        for parameter, count, stations, start, end in air_rows:
            key = f"air:{parameter}"
            row_counts[key] = int(count or 0)
            data_ranges[key] = {
                "rows": int(count or 0),
                "stations": int(stations or 0),
                "start": start,
                "end": end,
            }

        weather_fields = (
            "temperature_c",
            "humidity_percent",
            "pressure_hpa",
            "precipitation_mm",
            "wind_speed_mps",
            "wind_direction_deg",
        )
        for field in weather_fields:
            try:
                count, stations, start, end = connection.execute(
                    f"""
                    SELECT COUNT({field}),
                           COUNT(DISTINCT CASE WHEN {field} IS NOT NULL
                                               THEN weather_station_id END),
                           MIN(CASE WHEN {field} IS NOT NULL
                                    THEN measurement_time END),
                           MAX(CASE WHEN {field} IS NOT NULL
                                    THEN measurement_time END)
                      FROM weather_measurements
                     WHERE is_valid = 1
                    """
                ).fetchone()
            except (sqlite3.DatabaseError, TypeError):
                count = stations = 0
                start = end = None
            key = f"weather:{field}"
            row_counts[key] = int(count or 0)
            data_ranges[key] = {
                "rows": int(count or 0),
                "stations": int(stations or 0),
                "start": start,
                "end": end,
            }

        for table in (
            "air_stations",
            "air_sensors",
            "weather_stations",
            "station_matches",
        ):
            try:
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.DatabaseError:
                count = 0
            row_counts[f"table:{table}"] = int(count or 0)

    return integrity, schema_version, row_counts, data_ranges


def create_snapshot_engine(path: Path) -> Engine:
    """Create a SQLAlchemy engine that cannot mutate the training snapshot."""

    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Training snapshot database does not exist: {resolved}"
        )

    url = f"sqlite:///file:{resolved.as_posix()}?mode=ro&uri=true"
    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        connect_args={
            "timeout": 30,
            "check_same_thread": False,
            "uri": True,
        },
    )

    @event.listens_for(engine, "connect")
    def _read_only_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA query_only=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


class TrainingSnapshotBridge:
    """Create immutable SQLite datasets while live ingestion continues."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.training_snapshot.root_dir.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _profile_root(self, profile: str) -> Path:
        path = self.root / profile
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _pointer_path(self, profile: str) -> Path:
        return self._profile_root(profile) / "latest.json"

    def list(self, *, profile: str | None = None) -> list[TrainingSnapshot]:
        roots = (
            [self._profile_root(profile)]
            if profile
            else [path for path in self.root.iterdir() if path.is_dir()]
        )
        snapshots: list[TrainingSnapshot] = []
        for profile_root in roots:
            for manifest_path in profile_root.glob("dataset-*/manifest.json"):
                try:
                    payload = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    snapshots.append(TrainingSnapshot.from_dict(payload))
                except (
                    OSError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    logger.warning(
                        "Ignoring invalid training snapshot manifest: %s",
                        manifest_path,
                    )
        return sorted(
            snapshots,
            key=lambda item: item.created_at,
            reverse=True,
        )

    def latest(self, profile: str) -> TrainingSnapshot:
        pointer = self._pointer_path(profile)
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            manifest_path = Path(str(payload["manifest_path"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshot = TrainingSnapshot.from_dict(manifest)
        except (
            FileNotFoundError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise FileNotFoundError(
                f"No valid latest training snapshot for profile={profile}"
            ) from exc
        self.validate(snapshot, verify_checksum=False)
        return snapshot

    def resolve(self, profile: str, selector: str) -> TrainingSnapshot | None:
        normalized = selector.strip()
        if normalized == "live":
            return None
        if normalized == "latest":
            return self.latest(profile)
        if normalized == "auto":
            reuse_minutes = int(
                self.config.training_snapshot.reuse_latest_minutes
            )
            if reuse_minutes > 0:
                try:
                    latest = self.latest(profile)
                except FileNotFoundError:
                    latest = None
                if latest is not None:
                    age = datetime.now(UTC) - latest.created_at.astimezone(UTC)
                    if age <= timedelta(minutes=reuse_minutes):
                        return latest
            return None

        for snapshot in self.list(profile=profile):
            if snapshot.dataset_id == normalized:
                self.validate(snapshot, verify_checksum=False)
                return snapshot
        raise FileNotFoundError(
            f"Training snapshot not found selector={selector!r} "
            f"profile={profile!r}"
        )

    def create(
        self,
        *,
        profile: str,
        targets: Iterable[str],
        progress: ProgressReporter | None = None,
        mirror_manifest: bool | None = None,
    ) -> TrainingSnapshot:
        configured_url = make_url(self.config.database_url)
        if configured_url.drivername.startswith("sqlite") and configured_url.database:
            source = Path(str(configured_url.database)).expanduser().resolve()
        else:
            source = self.config.paths.database_path.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Live database does not exist: {source}")

        now = datetime.now(UTC)
        dataset_id = (
            f"training-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}"
        )
        directory = self._profile_root(profile) / f"dataset-{dataset_id}"
        directory.mkdir(parents=True, exist_ok=False)
        partial = directory / "smog.db.partial"
        database = directory / "smog.db"
        manifest_path = directory / "manifest.json"

        source_stat = source.stat()
        pages = max(1, int(self.config.training_snapshot.backup_pages))
        sleep_seconds = float(
            self.config.training_snapshot.backup_sleep_seconds
        )
        stall_seconds = float(
            self.config.training_snapshot.backup_stall_seconds
        )
        maximum_restarts = int(
            self.config.training_snapshot.backup_max_restarts
        )

        if progress is not None:
            progress.update(
                "snapshot",
                0.02,
                task="creating immutable SQLite training snapshot",
                detail={
                    "dataset_id": dataset_id,
                    "profile": profile,
                    "source_database": str(source),
                    "target_database": str(database),
                },
                expected_task_seconds=300.0,
                task_history_key=f"training-snapshot:{profile}:backup",
                completed_weight=0.02,
                total_weight=1.0,
                force=True,
            )

        last_remaining: int | None = None
        last_advance_monotonic = time.monotonic()
        restart_count = 0
        best_copied = 0

        def backup_progress(
            status: int,
            remaining: int,
            total: int,
        ) -> None:
            nonlocal last_remaining
            nonlocal last_advance_monotonic
            nonlocal restart_count
            nonlocal best_copied

            now_monotonic = time.monotonic()
            copied = max(0, total - remaining) if total > 0 else 0

            if last_remaining is None:
                last_advance_monotonic = now_monotonic
            elif remaining < last_remaining:
                last_advance_monotonic = now_monotonic
            elif remaining > last_remaining:
                # sqlite3_backup restarts when the source database changes.
                # Before HF19.2 the renewable ProcessLease wrote its own
                # process_locks heartbeat into this same source database every
                # 30 seconds, so the snapshot process could restart its own copy.
                restart_count += 1
                last_advance_monotonic = now_monotonic

            best_copied = max(best_copied, copied)
            last_remaining = remaining

            if restart_count > maximum_restarts:
                raise RuntimeError(
                    "SQLite training snapshot backup restarted too many times "
                    f"({restart_count}>{maximum_restarts}). The live database "
                    "is being modified during the copy; retry after stopping "
                    "writers or use a completed snapshot."
                )

            stalled_for = now_monotonic - last_advance_monotonic
            if stalled_for > stall_seconds:
                raise RuntimeError(
                    "SQLite training snapshot backup made no progress for "
                    f"{stalled_for:.1f}s (limit={stall_seconds:.1f}s)."
                )

            if progress is None or total <= 0:
                return

            fraction = 0.02 + 0.68 * min(1.0, copied / total)
            progress.update(
                "snapshot",
                fraction,
                task=(
                    f"SQLite online backup: {copied}/{total} pages "
                    f"(restarts={restart_count})"
                ),
                detail={
                    "dataset_id": dataset_id,
                    "profile": profile,
                    "sqlite_backup_status": status,
                    "pages_copied_current_attempt": copied,
                    "pages_copied_best": best_copied,
                    "pages_total": total,
                    "pages_remaining": remaining,
                    "backup_restarts": restart_count,
                    "backup_stall_seconds": stall_seconds,
                    "backup_max_restarts": maximum_restarts,
                    "source_database": str(source),
                },
                completed_weight=fraction,
                total_weight=1.0,
                force=False,
            )

        try:
            with closing(
                sqlite3.connect(
                    source,
                    timeout=30.0,
                    check_same_thread=False,
                )
            ) as src, closing(
                sqlite3.connect(
                    partial,
                    timeout=30.0,
                    check_same_thread=False,
                )
            ) as dst:
                src.execute("PRAGMA busy_timeout=30000")
                dst.execute("PRAGMA busy_timeout=30000")
                src.backup(
                    dst,
                    pages=pages,
                    progress=backup_progress,
                    sleep=sleep_seconds,
                )
                dst.commit()
            partial.replace(database)

            if progress is not None:
                progress.update(
                    "snapshot",
                    0.74,
                    task="validating snapshot and collecting provenance",
                    detail={"dataset_id": dataset_id, "profile": profile},
                    completed_weight=0.74,
                    total_weight=1.0,
                    force=True,
                )

            integrity, schema_version, row_counts, data_ranges = (
                _snapshot_stats(database)
            )
            if integrity != "ok":
                raise RuntimeError(
                    f"Training snapshot quick_check failed: {integrity}"
                )

            checksum = _sha256(database)
            snapshot = TrainingSnapshot(
                dataset_id=dataset_id,
                profile=profile,
                targets=tuple(dict.fromkeys(str(item) for item in targets)),
                created_at=now,
                database_path=database,
                manifest_path=manifest_path,
                database_sha256=checksum,
                database_size_bytes=database.stat().st_size,
                source_database_path=source,
                source_database_size_bytes=source_stat.st_size,
                source_database_mtime_ns=source_stat.st_mtime_ns,
                integrity_check=integrity,
                schema_version=schema_version,
                row_counts=row_counts,
                data_ranges=data_ranges,
            )
            _atomic_json(manifest_path, snapshot.as_dict())

            pointer_payload = {
                "schema_version": "1.0",
                "dataset_id": dataset_id,
                "profile": profile,
                "created_at": now.isoformat(),
                "manifest_path": str(manifest_path),
                "database_path": str(database),
                "database_sha256": checksum,
            }
            _atomic_json(self._pointer_path(profile), pointer_payload)
            _atomic_json(self.root / "latest.json", pointer_payload)

            should_mirror = (
                self.config.training_snapshot.mirror_manifest_to_object_storage
                if mirror_manifest is None
                else bool(mirror_manifest)
            )
            remote_key: str | None = None
            mirror_error: str | None = None
            if should_mirror and self.config.object_storage.enabled:
                if progress is not None:
                    progress.update(
                        "snapshot",
                        0.90,
                        task="publishing snapshot manifest to object storage",
                        detail={"dataset_id": dataset_id, "profile": profile},
                        completed_weight=0.90,
                        total_weight=1.0,
                        force=True,
                    )
                try:
                    repository = create_artifact_repository(self.config)
                    remote_key = repository.layout.training_snapshot_manifest(
                        dataset_id
                    )
                    remote_payload = dict(snapshot.as_dict())
                    remote_payload["database_path"] = None
                    remote_payload["manifest_path"] = None
                    remote_payload["database_mirrored"] = False
                    repository.put_json(
                        remote_key,
                        remote_payload,
                        immutable=True,
                    )
                    repository.put_json(
                        repository.layout.latest_training_snapshot_pointer(profile),
                        {
                            "schema_version": "1.0",
                            "dataset_id": dataset_id,
                            "profile": profile,
                            "created_at": now.isoformat(),
                            "manifest_key": remote_key,
                            "database_sha256": checksum,
                            "database_mirrored": False,
                        },
                    )
                except Exception as exc:
                    # Snapshot training is deliberately local-first. Failure to
                    # mirror the small provenance manifest must not throw away a
                    # valid immutable SQLite dataset or stop model training.
                    mirror_error = f"{type(exc).__name__}: {exc}"
                    remote_key = None
                    logger.exception(
                        "Training snapshot manifest mirror failed; continuing "
                        "with local immutable dataset %s",
                        dataset_id,
                    )
                snapshot = replace(
                    snapshot,
                    remote_manifest_key=remote_key,
                    mirror_error=mirror_error,
                )
                _atomic_json(manifest_path, snapshot.as_dict())

            try:
                os.chmod(database, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                logger.warning(
                    "Could not mark snapshot read-only: %s",
                    database,
                )

            if progress is not None:
                progress.complete_stage(
                    "snapshot",
                    task="immutable training snapshot ready",
                    detail={
                        "dataset_id": dataset_id,
                        "profile": profile,
                        "database_path": str(database),
                        "database_sha256": checksum,
                        "database_size_bytes": database.stat().st_size,
                        "remote_manifest_key": remote_key,
                    },
                )
            return snapshot
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def validate(
        self,
        snapshot: TrainingSnapshot,
        *,
        verify_checksum: bool = True,
    ) -> dict[str, Any]:
        if not snapshot.database_path.exists():
            raise FileNotFoundError(
                "Training snapshot database is missing: "
                f"{snapshot.database_path}"
            )
        integrity, schema_version, row_counts, data_ranges = _snapshot_stats(
            snapshot.database_path
        )
        checksum = (
            _sha256(snapshot.database_path) if verify_checksum else None
        )
        valid = integrity == "ok" and (
            checksum is None or checksum == snapshot.database_sha256
        )
        if not valid:
            raise RuntimeError(
                "Training snapshot validation failed "
                f"dataset_id={snapshot.dataset_id} integrity={integrity} "
                f"checksum={checksum} expected={snapshot.database_sha256}"
            )
        return {
            "valid": True,
            "dataset_id": snapshot.dataset_id,
            "integrity_check": integrity,
            "database_sha256": checksum or snapshot.database_sha256,
            "alembic_schema_version": schema_version,
            "row_counts": row_counts,
            "data_ranges": data_ranges,
        }

    def cleanup(
        self,
        *,
        profile: str,
        keep: int | None = None,
    ) -> list[str]:
        if keep is None:
            keep = (
                self.config.training_snapshot.retain_quick
                if profile == "quick"
                else self.config.training_snapshot.retain_full
            )
        snapshots = self.list(profile=profile)
        removed: list[str] = []
        for snapshot in snapshots[max(1, int(keep)) :]:
            directory = snapshot.manifest_path.parent
            try:
                os.chmod(
                    snapshot.database_path,
                    stat.S_IWRITE | stat.S_IREAD,
                )
            except OSError:
                pass
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(snapshot.dataset_id)
        return removed


def create_training_snapshot_bridge(
    config: AppConfig,
) -> TrainingSnapshotBridge:
    return TrainingSnapshotBridge(config)
