from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from smog_ai import __version__
from smog_ai.config import AppConfig


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _retention(config: AppConfig, tier: str) -> int:
    return {
        "daily": config.backup.daily_keep,
        "weekly": config.backup.weekly_keep,
        "monthly": config.backup.monthly_keep,
    }[tier]


def _unlink_with_retry(path: Path, *, attempts: int = 8, delay_seconds: float = 0.08) -> None:
    """Remove a temporary file after all SQLite/stream handles are closed.

    Windows can keep a short-lived scanner/indexer handle after a file is closed.
    A bounded retry handles that race without hiding a persistent lock.
    """

    for attempt in range(1, attempts + 1):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(delay_seconds * attempt)


def create_backup(config: AppConfig, tier: Literal["daily", "weekly", "monthly"] = "daily") -> dict[str, Any]:
    source = config.paths.database_path
    if not source.exists():
        raise FileNotFoundError(f"Database does not exist: {source}")
    config.paths.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    uncompressed = config.paths.temp_dir / f"smog-{tier}-{stamp}.sqlite"
    archive = config.paths.backups_dir / f"smog-{tier}-{stamp}.sqlite.gz"
    config.paths.temp_dir.mkdir(parents=True, exist_ok=True)

    # sqlite3.Connection.__exit__ commits/rolls back but does not guarantee that
    # the native file handle is closed. closing() is required for WinError 32.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(uncompressed)) as dst:
        src.backup(dst)
        dst.commit()
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")

    with uncompressed.open("rb") as input_stream, gzip.open(
        archive, "wb", compresslevel=6
    ) as output_stream:
        for block in iter(lambda: input_stream.read(1024 * 1024), b""):
            output_stream.write(block)

    _unlink_with_retry(uncompressed)
    metadata = {
        "database_file": str(source),
        "created_at": datetime.now(UTC).isoformat(),
        "database_size": source.stat().st_size,
        "archive_size": archive.stat().st_size,
        "sha256": _sha256(archive),
        "application_version": __version__,
        "schema_version": "1.0.0",
        "backup_status": "success",
        "tier": tier,
    }
    archive.with_suffix(archive.suffix + ".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    files = sorted(config.paths.backups_dir.glob(f"smog-{tier}-*.sqlite.gz"), reverse=True)
    for old in files[_retention(config, tier) :]:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".json").unlink(missing_ok=True)
    return {**metadata, "archive": str(archive)}
