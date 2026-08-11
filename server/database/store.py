from __future__ import annotations

import gzip
import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    desc,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.publishing.schema import SnapshotPayload
from smog_ai.storage.base import ObjectNotFoundError
from smog_ai.publishing.snapshot import calculate_payload_checksum


class SnapshotConflictError(RuntimeError):
    pass


@runtime_checkable
class SnapshotStoreProtocol(Protocol):
    backend_name: str

    @staticmethod
    def decode_and_validate(
        compressed: bytes, expected_checksum: str, expected_id: str
    ) -> SnapshotPayload: ...

    def save(
        self,
        compressed: bytes,
        payload: SnapshotPayload,
        *,
        remote_address: str | None = None,
    ) -> tuple[bool, Path]: ...

    def latest_payload(self) -> dict[str, Any] | None: ...

    def audit_summary(self) -> dict[str, Any]: ...

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]: ...

    def ping(self) -> None: ...


class SnapshotValidationMixin:
    @staticmethod
    def decode_and_validate(
        compressed: bytes, expected_checksum: str, expected_id: str
    ) -> SnapshotPayload:
        try:
            raw = gzip.decompress(compressed)
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid gzip/JSON payload: {exc}") from exc
        payload = SnapshotPayload.model_validate(data)
        calculated = calculate_payload_checksum(data)
        if calculated != expected_checksum or payload.metadata.checksum != expected_checksum:
            raise ValueError("Checksum mismatch")
        if payload.metadata.publication_id != expected_id:
            raise ValueError("Publication id does not match payload metadata")
        return payload


class SnapshotStore(SnapshotValidationMixin):
    """Filesystem + SQLite implementation for local development and VM installs.

    It is intentionally retained for backward compatibility. Do not use this
    backend on DigitalOcean App Platform because its filesystem is ephemeral.
    """

    backend_name = "filesystem-sqlite"

    def __init__(self, data_dir: Path, *, keep_versions: int = 20) -> None:
        self.data_dir = data_dir
        self.snapshot_dir = data_dir / "snapshots"
        self.db_path = data_dir / "publication_audit.db"
        self.keep_versions = keep_versions
        self._lock = threading.RLock()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS publication_audit (
                    publication_id TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    source_host_id TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    payload_path TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    remote_address TEXT,
                    duplicate_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_publication_audit_received_at "
                "ON publication_audit(received_at DESC)"
            )

    def save(
        self,
        compressed: bytes,
        payload: SnapshotPayload,
        *,
        remote_address: str | None = None,
    ) -> tuple[bool, Path]:
        publication_id = payload.metadata.publication_id
        checksum = payload.metadata.checksum
        final_path = self.snapshot_dir / f"{publication_id}.json.gz"
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT checksum, payload_path FROM publication_audit WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if existing is not None:
                if existing["checksum"] != checksum:
                    raise SnapshotConflictError(
                        "Publication id already exists with a different checksum"
                    )
                connection.execute(
                    "UPDATE publication_audit SET duplicate_count = duplicate_count + 1 "
                    "WHERE publication_id = ?",
                    (publication_id,),
                )
                return False, Path(existing["payload_path"])
            temporary = final_path.with_suffix(final_path.suffix + f".{os.getpid()}.tmp")
            temporary.write_bytes(compressed)
            os.replace(temporary, final_path)
            connection.execute(
                """
                INSERT INTO publication_audit (
                    publication_id, checksum, schema_version, generated_at, source_host_id,
                    record_count, payload_path, received_at, remote_address
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    checksum,
                    payload.metadata.schema_version,
                    payload.metadata.generated_at.isoformat(),
                    payload.metadata.source_host_id,
                    payload.metadata.record_count,
                    str(final_path),
                    datetime.now(UTC).isoformat(),
                    remote_address,
                ),
            )
            self._prune(connection)
            return True, final_path

    def _prune(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT publication_id, payload_path FROM publication_audit "
            "ORDER BY received_at DESC, publication_id DESC"
        ).fetchall()
        for row in rows[self.keep_versions :]:
            Path(row["payload_path"]).unlink(missing_ok=True)
            connection.execute(
                "DELETE FROM publication_audit WHERE publication_id = ?",
                (row["publication_id"],),
            )

    def latest_payload(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_path FROM publication_audit "
                "ORDER BY received_at DESC, publication_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        path = Path(row["payload_path"])
        if not path.exists():
            return None
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))

    def audit_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM publication_audit").fetchone()[0]
            latest = connection.execute(
                "SELECT publication_id, received_at FROM publication_audit "
                "ORDER BY received_at DESC, publication_id DESC LIMIT 1"
            ).fetchone()
        return {
            "storage_backend": self.backend_name,
            "publication_count": int(count),
            "latest_publication_id": latest["publication_id"] if latest else None,
            "latest_received_at": latest["received_at"] if latest else None,
        }

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT publication_id, checksum, schema_version, generated_at,
                       source_host_id, record_count, received_at, remote_address,
                       duplicate_count
                FROM publication_audit
                ORDER BY received_at DESC, publication_id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def ping(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()


SERVER_METADATA = MetaData()
PUBLICATION_AUDIT = Table(
    "server_publication_audit",
    SERVER_METADATA,
    Column("publication_id", String(160), primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("generated_at", DateTime(timezone=True), nullable=False),
    Column("source_host_id", String(255), nullable=False),
    Column("record_count", BigInteger, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("remote_address", String(255), nullable=True),
    Column("duplicate_count", Integer, nullable=False, default=0, server_default="0"),
)
Index("ix_server_publication_audit_received_at", PUBLICATION_AUDIT.c.received_at)


class DatabaseSnapshotStore(SnapshotValidationMixin):
    """Persistent snapshot store backed by PostgreSQL (or SQLite in tests).

    The compressed, checksum-verified snapshot is stored in a BLOB/BYTEA column.
    This removes all dependency on a persistent container filesystem and makes
    the API safe to redeploy or replace on DigitalOcean App Platform.
    """

    backend_name = "database"

    def __init__(
        self,
        database_url: str,
        *,
        keep_versions: int = 20,
        initialize: bool = True,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.keep_versions = keep_versions
        self._lock = threading.RLock()
        connect_args: dict[str, Any] = {}
        if self.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self.engine: Engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args=connect_args,
        )
        if initialize:
            self.initialize_schema()

    def initialize_schema(self) -> None:
        SERVER_METADATA.create_all(self.engine)

    def save(
        self,
        compressed: bytes,
        payload: SnapshotPayload,
        *,
        remote_address: str | None = None,
    ) -> tuple[bool, Path]:
        publication_id = payload.metadata.publication_id
        values = {
            "publication_id": publication_id,
            "checksum": payload.metadata.checksum,
            "schema_version": payload.metadata.schema_version,
            "generated_at": payload.metadata.generated_at,
            "source_host_id": payload.metadata.source_host_id,
            "record_count": payload.metadata.record_count,
            "payload": compressed,
            "received_at": datetime.now(UTC),
            "remote_address": remote_address,
            "duplicate_count": 0,
        }
        with self._lock:
            try:
                with self.engine.begin() as connection:
                    connection.execute(insert(PUBLICATION_AUDIT).values(**values))
                    self._prune(connection)
                return True, Path(f"database://{publication_id}.json.gz")
            except IntegrityError:
                with self.engine.begin() as connection:
                    query = select(
                        PUBLICATION_AUDIT.c.checksum,
                        PUBLICATION_AUDIT.c.duplicate_count,
                    ).where(PUBLICATION_AUDIT.c.publication_id == publication_id)
                    if connection.dialect.name == "postgresql":
                        query = query.with_for_update()
                    row = connection.execute(query).mappings().one_or_none()
                    if row is None:
                        raise
                    if row["checksum"] != payload.metadata.checksum:
                        raise SnapshotConflictError(
                            "Publication id already exists with a different checksum"
                        )
                    connection.execute(
                        update(PUBLICATION_AUDIT)
                        .where(PUBLICATION_AUDIT.c.publication_id == publication_id)
                        .values(
                            duplicate_count=PUBLICATION_AUDIT.c.duplicate_count + 1
                        )
                    )
                return False, Path(f"database://{publication_id}.json.gz")

    def _prune(self, connection: Connection) -> None:
        old_ids = connection.execute(
            select(PUBLICATION_AUDIT.c.publication_id)
            .order_by(
                desc(PUBLICATION_AUDIT.c.received_at),
                desc(PUBLICATION_AUDIT.c.publication_id),
            )
            .offset(self.keep_versions)
        ).scalars().all()
        if old_ids:
            connection.execute(
                delete(PUBLICATION_AUDIT).where(
                    PUBLICATION_AUDIT.c.publication_id.in_(old_ids)
                )
            )

    def latest_payload(self) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            compressed = connection.execute(
                select(PUBLICATION_AUDIT.c.payload)
                .order_by(
                    desc(PUBLICATION_AUDIT.c.received_at),
                    desc(PUBLICATION_AUDIT.c.publication_id),
                )
                .limit(1)
            ).scalar_one_or_none()
        if compressed is None:
            return None
        return json.loads(gzip.decompress(compressed).decode("utf-8"))

    def audit_summary(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            count = connection.execute(
                select(func.count()).select_from(PUBLICATION_AUDIT)
            ).scalar_one()
            latest = connection.execute(
                select(
                    PUBLICATION_AUDIT.c.publication_id,
                    PUBLICATION_AUDIT.c.received_at,
                )
                .order_by(
                    desc(PUBLICATION_AUDIT.c.received_at),
                    desc(PUBLICATION_AUDIT.c.publication_id),
                )
                .limit(1)
            ).mappings().one_or_none()
        return {
            "storage_backend": self.backend_name,
            "publication_count": int(count),
            "latest_publication_id": latest["publication_id"] if latest else None,
            "latest_received_at": (
                latest["received_at"].isoformat() if latest and latest["received_at"] else None
            ),
        }

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    PUBLICATION_AUDIT.c.publication_id,
                    PUBLICATION_AUDIT.c.checksum,
                    PUBLICATION_AUDIT.c.schema_version,
                    PUBLICATION_AUDIT.c.generated_at,
                    PUBLICATION_AUDIT.c.source_host_id,
                    PUBLICATION_AUDIT.c.record_count,
                    PUBLICATION_AUDIT.c.received_at,
                    PUBLICATION_AUDIT.c.remote_address,
                    PUBLICATION_AUDIT.c.duplicate_count,
                )
                .order_by(
                    desc(PUBLICATION_AUDIT.c.received_at),
                    desc(PUBLICATION_AUDIT.c.publication_id),
                )
                .limit(safe_limit)
            ).mappings().all()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("generated_at", "received_at"):
                value = item.get(key)
                if isinstance(value, datetime):
                    item[key] = value.isoformat()
            result.append(item)
        return result

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()



class ObjectStoreSnapshotStore(SnapshotValidationMixin):
    """Snapshot store backed by the generic ArtifactRepository Bridge."""

    backend_name = "object-store"

    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        keep_versions: int = 50,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self.repository = repository
        self.keep_versions = keep_versions
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._lock = threading.RLock()
        self._latest_cache: tuple[float, dict[str, Any] | None] | None = None

    def save(
        self,
        compressed: bytes,
        payload: SnapshotPayload,
        *,
        remote_address: str | None = None,
    ) -> tuple[bool, Path]:
        publication_id = payload.metadata.publication_id
        audit_key = self.repository.layout.publication_audit(publication_id)
        with self._lock:
            try:
                existing = self.repository.get_json(audit_key)
            except ObjectNotFoundError:
                existing = None
            if existing is not None:
                existing_checksum = str(existing.get("payload_checksum") or "")
                if existing_checksum != payload.metadata.checksum:
                    raise SnapshotConflictError(
                        "Publication id already exists with a different checksum"
                    )
                return False, Path(str(existing.get("object_key") or audit_key))
            stored = self.repository.publish_snapshot(
                compressed=compressed,
                publication_id=publication_id,
                checksum=payload.metadata.checksum,
                metadata={
                    **payload.metadata.model_dump(mode="json"),
                    "remote_address": remote_address,
                },
            )
            self._latest_cache = None
            return True, Path(stored.key)

    def latest_payload(self) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            cached = self._latest_cache
            if cached is not None and (
                self.cache_ttl_seconds == 0 or cached[0] >= now
            ):
                return cached[1]
        payload = self.repository.latest_snapshot_payload()
        expires = (
            float("inf")
            if self.cache_ttl_seconds == 0
            else now + self.cache_ttl_seconds
        )
        with self._lock:
            self._latest_cache = (expires, payload)
        return payload

    def audit_summary(self) -> dict[str, Any]:
        history = self.repository.snapshot_history(limit=max(1, self.keep_versions))
        latest = history[0] if history else None
        return {
            "storage_backend": f"{self.backend_name}:{self.repository.store.backend_name}",
            "publication_count": len(self.repository.store.list("forecasts/audit/")),
            "latest_publication_id": latest.get("publication_id") if latest else None,
            "latest_received_at": latest.get("updated_at") if latest else None,
        }

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.repository.snapshot_history(limit=limit)

    def ping(self) -> None:
        self.repository.ping()

def normalize_database_url(database_url: str) -> str:
    url = database_url.strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def create_snapshot_store(
    *,
    data_dir: Path,
    keep_versions: int,
    database_url: str | None,
    storage_backend: str,
    artifact_repository: ArtifactRepository | None = None,
) -> SnapshotStoreProtocol:
    backend = storage_backend.strip().lower()
    if backend in {"object_store", "object-store", "s3", "spaces"}:
        if artifact_repository is None:
            raise RuntimeError("Object-store backend selected but no artifact repository was provided")
        return ObjectStoreSnapshotStore(artifact_repository, keep_versions=keep_versions)
    use_database = backend == "database" or (backend == "auto" and bool(database_url))
    if use_database:
        if not database_url:
            raise RuntimeError("Database storage selected but no database URL was provided")
        return DatabaseSnapshotStore(database_url, keep_versions=keep_versions)
    return SnapshotStore(data_dir, keep_versions=keep_versions)
