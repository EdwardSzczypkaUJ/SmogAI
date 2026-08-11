from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from sqlalchemy import func, select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import CollectionRun
from smog_ai.database.repository import set_application_state
from smog_ai.domain import StageStats
from smog_ai.monitoring.backup import create_backup
from smog_ai.monitoring.health import run_healthcheck
from smog_ai.pipeline import run_pipeline
from tests.conftest import seed_basic


def test_backup_uses_valid_sqlite_copy(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    result = create_backup(app_config, "daily")
    archive = Path(result["archive"])
    assert archive.exists()
    extracted = app_config.paths.temp_dir / "restored.sqlite"
    extracted.write_bytes(gzip.decompress(archive.read_bytes()))
    with sqlite3.connect(extracted) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_retention(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    for index in range(4):
        result = create_backup(app_config, "daily")
        path = Path(result["archive"])
        # Ensure unique names in this fast test.
        if index < 3:
            path.rename(path.with_name(path.stem + f"-{index}" + path.suffix))
    assert len(list(app_config.paths.backups_dir.glob("smog-daily-*.sqlite.gz"))) <= 3


def test_healthcheck_reports_database(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    with session_scope(engine) as session:
        set_application_state(session, "last_gios_success_at", "2026-07-30T00:00:00+00:00")
        set_application_state(session, "last_imgw_success_at", "2026-07-30T00:00:00+00:00")
        set_application_state(session, "last_forecast_at", "2026-07-30T00:00:00+00:00")
    with session_scope(engine) as session:
        result = run_healthcheck(session, engine, app_config)
        assert result.checks["database"]["status"] == "ok"
        assert "disk" in result.checks


def test_pipeline_records_run_with_mocked_stages(engine, app_config, monkeypatch) -> None:
    import smog_ai.pipeline as pipeline_module

    def stage(session, config):
        return StageStats(downloaded=1, inserted=1)

    monkeypatch.setattr(pipeline_module, "PIPELINE_STAGES", [("one", stage, False), ("two", stage, False)])
    run_id, stats, stages = run_pipeline(engine, app_config)
    assert stats.inserted == 2
    with session_scope(engine) as session:
        run = session.get(CollectionRun, run_id)
        assert run.status == "success"
        assert set(stages) == {"one", "two"}


def test_pipeline_marks_partial_failure(engine, app_config, monkeypatch) -> None:
    import smog_ai.pipeline as pipeline_module

    def good(session, config):
        return StageStats(inserted=1)

    def bad(session, config):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_module, "PIPELINE_STAGES", [("good", good, False), ("bad", bad, False)])
    run_id, stats, _ = run_pipeline(engine, app_config)
    assert stats.errors == 1
    with session_scope(engine) as session:
        assert session.get(CollectionRun, run_id).status == "partial_success"


def test_pipeline_can_run_selected_stages(engine, app_config, monkeypatch) -> None:
    import smog_ai.pipeline as pipeline_module

    called: list[str] = []

    def one(session, config):
        called.append("one")
        return StageStats(inserted=1)

    def two(session, config):
        called.append("two")
        return StageStats(inserted=1)

    monkeypatch.setattr(pipeline_module, "PIPELINE_STAGES", [("one", one, False), ("two", two, False)])
    _, stats, stages = run_pipeline(engine, app_config, stage_names=("one",))
    assert called == ["one"]
    assert stats.inserted == 1
    assert set(stages) == {"one"}

def test_backup_temporary_unlink_retries_short_windows_lock(tmp_path, monkeypatch) -> None:
    from smog_ai.monitoring.backup import _unlink_with_retry

    target = tmp_path / "locked.sqlite"
    target.write_bytes(b"sqlite")
    original_unlink = Path.unlink
    calls = {"count": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self == target and calls["count"] < 2:
            calls["count"] += 1
            raise PermissionError(32, "simulated short Windows file lock", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    _unlink_with_retry(target, attempts=4, delay_seconds=0)
    assert calls["count"] == 2
    assert not target.exists()

