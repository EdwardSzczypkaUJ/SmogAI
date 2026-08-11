from __future__ import annotations

from pathlib import Path

from smog_ai.config import AppConfig, PathsConfig, sqlite_url_for_path
from smog_ai.database.engine import create_db_engine, init_database


def _paths(tmp_path: Path) -> PathsConfig:
    return PathsConfig(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "isolated.db",
        models_dir=tmp_path / "models",
        snapshots_dir=tmp_path / "snapshots",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        temp_dir=tmp_path / "tmp",
        imgw_metadata_csv=tmp_path / "imgw.csv",
    )


def test_test_environment_ignores_production_database_override(monkeypatch, tmp_path: Path) -> None:
    production_url = "sqlite:///C:/ProgramData/SmogAI/data/smog.db"
    monkeypatch.setenv("SMOG_AI_DATABASE_URL", production_url)
    config = AppConfig(environment="test", paths=_paths(tmp_path))

    assert config.database_url == sqlite_url_for_path(tmp_path / "data" / "isolated.db")
    assert "ProgramData/SmogAI" not in config.database_url


def test_production_environment_still_honours_database_override(monkeypatch, tmp_path: Path) -> None:
    production_url = "sqlite:///C:/ProgramData/SmogAI/data/smog.db"
    monkeypatch.setenv("SMOG_AI_DATABASE_URL", production_url)
    config = AppConfig(environment="production", paths=_paths(tmp_path))

    assert config.database_url == production_url


def test_engine_created_for_tests_uses_temporary_database(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SMOG_AI_DATABASE_URL", "sqlite:///C:/ProgramData/SmogAI/data/smog.db")
    config = AppConfig(environment="test", paths=_paths(tmp_path))
    config.ensure_directories()
    engine = create_db_engine(config)
    try:
        init_database(engine)
        assert Path(str(engine.url.database)).resolve() == (tmp_path / "data" / "isolated.db").resolve()
    finally:
        engine.dispose()
