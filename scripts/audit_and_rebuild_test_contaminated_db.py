#!/usr/bin/env python3
"""Audit and safely rebuild a SmogAI SQLite database touched by pytest.

The tool is intentionally conservative.  It never edits rows in place because a
leaked test run may have changed timestamps on legitimate measurements.  With
``--rebuild`` it creates a consistent SQLite Online Backup, preserves the original
DB/WAL/SHM files in a quarantine directory, creates a new schema through Alembic,
and writes a JSON recovery report.  Configuration and DigitalOcean credentials
are not modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow direct execution by absolute path even when the project is not installed
# editable in the selected virtual environment.
_SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_PROJECT_ROOT))

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.engine import make_url

from smog_ai.config import load_config, sqlite_url_for_path


MARKER_QUERIES: tuple[tuple[str, str, tuple[Any, ...], str], ...] = (
    (
        "pytest_air_station",
        "SELECT COUNT(*) FROM air_stations WHERE "
        "(source_id = ? AND station_name = ?) OR (source_id = ? AND station_name = ?)",
        ("A1", "Kraków test", "a", "A"),
        "synthetic air station inserted by tests",
    ),
    (
        "pytest_air_sensor",
        "SELECT COUNT(*) FROM air_sensors WHERE source_id = ?",
        ("S1",),
        "synthetic PM sensor inserted by tests",
    ),
    (
        "pytest_weather_station",
        "SELECT COUNT(*) FROM weather_stations WHERE source_id IN (?, ?, ?) OR metadata_source = ?",
        ("w", "w1", "w2", "test"),
        "synthetic weather station inserted by tests",
    ),
    (
        "pytest_model_version",
        "SELECT COUNT(*) FROM model_versions WHERE semantic_version IN (?, ?)",
        ("legacy-test-v1", "spatial-test-v1"),
        "synthetic model version inserted by tests",
    ),
    (
        "pytest_outbox",
        "SELECT COUNT(*) FROM publication_outbox WHERE publication_id = ?",
        ("p1",),
        "synthetic outbox record inserted by tests",
    ),
    (
        "pytest_forecast_station",
        "SELECT COUNT(*) FROM air_stations WHERE station_name LIKE ? AND source_id IN (?, ?, ?, ?)",
        ("% test", "A1", "A2", "A3", "A4"),
        "synthetic spatial-test stations",
    ),
)

COUNT_TABLES = (
    "air_stations",
    "air_sensors",
    "air_measurements",
    "weather_stations",
    "weather_measurements",
    "station_matches",
    "model_versions",
    "forecasts",
    "publication_outbox",
    "collection_runs",
)


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_path_from_url(url: str) -> Path:
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        raise RuntimeError(f"Only SQLite recovery is supported, got: {parsed.drivername}")
    if not parsed.database:
        raise RuntimeError("SQLite URL does not contain a database path")
    return Path(parsed.database).expanduser().resolve()


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def audit_database(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "database_path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "quick_check": None,
        "counts": {},
        "markers": [],
        "contaminated": False,
    }
    if not path.exists():
        return result

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        result["quick_check"] = connection.execute("PRAGMA quick_check").fetchone()[0]
        for table in COUNT_TABLES:
            if table_exists(connection, table):
                result["counts"][table] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
        for marker_id, sql, params, description in MARKER_QUERIES:
            table = sql.split("FROM ", 1)[1].split(" ", 1)[0]
            if not table_exists(connection, table):
                continue
            count = int(connection.execute(sql, params).fetchone()[0])
            if count:
                result["markers"].append(
                    {"id": marker_id, "count": count, "description": description}
                )
    finally:
        connection.close()

    result["contaminated"] = bool(result["markers"])
    return result


def online_backup(source_path: Path, backup_path: Path) -> dict[str, Any]:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path), timeout=60)
    destination = sqlite3.connect(str(backup_path), timeout=60)
    try:
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.DatabaseError:
            # sqlite3.Connection.backup still creates a transactionally consistent copy.
            pass
        source.backup(destination)
        quick_check = destination.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"Backup quick_check returned: {quick_check}")
    finally:
        destination.close()
        source.close()
    return {
        "path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "sha256": sha256_file(backup_path),
        "quick_check": "ok",
    }


def preserve_original_files(database_path: Path, quarantine_dir: Path) -> dict[str, str]:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    preserved: dict[str, str] = {}
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(database_path) + suffix)
        if not source.exists():
            continue
        destination = quarantine_dir / (database_path.name + suffix)
        shutil.move(str(source), str(destination))
        preserved[str(source)] = str(destination)
    return preserved


def restore_original_files(preserved: dict[str, str]) -> None:
    """Best-effort rollback when installing the fresh database fails."""

    for original_text, quarantine_text in preserved.items():
        original = Path(original_text)
        quarantine = Path(quarantine_text)
        if not quarantine.exists():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        if original.exists():
            original.unlink()
        shutil.move(str(quarantine), str(original))


def create_fresh_schema(
    project_root: Path,
    database_url: str,
    config_path: Path,
    env_path: Path,
) -> None:
    # migrations/env.py loads AppConfig and may otherwise replace Alembic's URL
    # with the production SMOG_AI_DATABASE_URL. Override it only for this call
    # and restore the caller's environment afterwards.
    names = ("SMOG_AI_DATABASE_URL", "SMOG_AI_CONFIG", "SMOG_AI_ENV_FILE")
    previous = {name: os.environ.get(name) for name in names}
    os.environ["SMOG_AI_DATABASE_URL"] = database_url
    os.environ["SMOG_AI_CONFIG"] = str(config_path)
    os.environ["SMOG_AI_ENV_FILE"] = str(env_path)
    try:
        alembic_ini = project_root / "alembic.ini"
        alembic_config = AlembicConfig(str(alembic_ini))
        alembic_config.set_main_option("script_location", str(project_root / "migrations"))
        alembic_config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(alembic_config, "head")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def verify_sqlite_database(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"SQLite quick_check returned: {quick_check}")
        table_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
        if table_count < 10:
            raise RuntimeError(f"Fresh schema contains only {table_count} application tables")
    finally:
        connection.close()
    return {"quick_check": "ok", "application_table_count": table_count}


def install_fresh_database(fresh_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(fresh_path, destination)
    except OSError:
        shutil.move(str(fresh_path), str(destination))


def write_report(path: Path | None, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(rendered)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--force", action="store_true", help="Rebuild even without known pytest markers")
    args = parser.parse_args(argv)

    project_root = args.project_root.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    env_path = args.env_file.expanduser().resolve()
    config = load_config(config_path, env_path)
    database_url = config.database_url
    database_path = sqlite_path_from_url(database_url)
    before = audit_database(database_path)

    report: dict[str, Any] = {
        "tool": "smog-ai-test-leak-recovery",
        "version": "1.7.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "project_root": str(project_root),
        "action": "audit",
        "before": before,
    }

    if not args.rebuild:
        report["status"] = "contaminated" if before["contaminated"] else "clean"
        report["next_action"] = (
            "Run again with --rebuild after stopping SmogAI processes."
            if before["contaminated"]
            else "No database rebuild required."
        )
        write_report(args.output, report)
        return 4 if before["contaminated"] else 0

    if not before["exists"]:
        report["status"] = "failed"
        report["error"] = "Database does not exist; use init-db instead of recovery."
        write_report(args.output, report)
        return 1
    if not before["contaminated"] and not args.force:
        report["status"] = "refused"
        report["error"] = "No known pytest markers found. Use --force only after manual verification."
        write_report(args.output, report)
        return 2

    stamp = utc_stamp()
    recovery_root = config.paths.backups_dir / "test-leak-recovery" / stamp
    consistent_backup = recovery_root / "smog-before-rebuild.sqlite"
    quarantine_dir = recovery_root / "original-files"
    report_path = args.output or recovery_root / "recovery-report.json"

    fresh_database = recovery_root / "fresh-smog.sqlite"
    preserved: dict[str, str] = {}
    try:
        # Create and validate the replacement before touching the production DB.
        backup = online_backup(database_path, consistent_backup)
        fresh_database.unlink(missing_ok=True)
        fresh_url = sqlite_url_for_path(fresh_database)
        create_fresh_schema(project_root, fresh_url, config_path, env_path)
        fresh_verification = verify_sqlite_database(fresh_database)

        preserved = preserve_original_files(database_path, quarantine_dir)
        try:
            install_fresh_database(fresh_database, database_path)
        except Exception:
            restore_original_files(preserved)
            raise

        after = audit_database(database_path)
        if after["quick_check"] != "ok":
            raise RuntimeError(f"Fresh database quick_check failed: {after['quick_check']}")
        report.update(
            {
                "action": "rebuild",
                "status": "rebuilt",
                "backup": backup,
                "fresh_database_verification": fresh_verification,
                "preserved_original_files": list(preserved.values()),
                "after": after,
                "next_action": "Run collect-gios, collect-imgw, then first-run. Configuration and Spaces credentials were preserved.",
            }
        )
        write_report(report_path, report)
        return 0
    except Exception as exc:  # noqa: BLE001 - recovery report must capture every failure
        # If the destination was not successfully installed, restore quarantined files.
        if preserved and not database_path.exists():
            try:
                restore_original_files(preserved)
            except Exception as restore_exc:  # noqa: BLE001
                report["restore_error"] = f"{type(restore_exc).__name__}: {restore_exc}"
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        write_report(report_path, report)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
