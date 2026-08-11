from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import text

from scripts.audit_and_rebuild_test_contaminated_db import main as recovery_main
from smog_ai.config import AppConfig
from smog_ai.database.engine import create_db_engine, init_database


def test_recovery_preserves_contaminated_database_and_creates_clean_schema(
    app_config: AppConfig,
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Recovery is a production-only workflow. Remove the autouse test-mode marker
    # only inside this isolated tmp_path scenario.
    monkeypatch.delenv("SMOG_AI_ENV", raising=False)
    app_config.environment = "production"
    app_config.paths.backups_dir = tmp_path / "runtime" / "backups"
    app_config.paths.database_path = tmp_path / "runtime" / "data" / "smog.db"
    app_config.paths.data_dir = app_config.paths.database_path.parent
    app_config.paths.models_dir = tmp_path / "runtime" / "models"
    app_config.paths.snapshots_dir = tmp_path / "runtime" / "snapshots"
    app_config.paths.logs_dir = tmp_path / "runtime" / "logs"
    app_config.paths.temp_dir = tmp_path / "runtime" / "tmp"
    app_config.object_storage.backend = "local"
    app_config.object_storage.local_root = tmp_path / "runtime" / "object-store"
    app_config.object_storage.bucket = None
    app_config.object_storage.endpoint_url = None
    app_config.object_storage.region = None
    app_config.ensure_directories()

    engine = create_db_engine(app_config)
    init_database(engine)
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO air_stations "
                "(source, source_id, station_name, city_name, latitude, longitude, "
                "active, created_at, updated_at) "
                "VALUES (:source, :source_id, :name, :city, :lat, :lon, 1, :now, :now)"
            ),
            {
                "source": "GIOS",
                "source_id": "A1",
                "name": "Kraków test",
                "city": "Kraków",
                "lat": 50.0,
                "lon": 20.0,
                "now": now,
            },
        )
    engine.dispose()

    config_path = tmp_path / "runtime" / "config.yaml"
    env_path = tmp_path / "runtime" / "smog-ai.env"
    report_path = tmp_path / "runtime" / "recovery.json"
    config_path.write_text(
        yaml.safe_dump(app_config.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    env_path.write_text("", encoding="utf-8")

    exit_code = recovery_main(
        [
            "--project-root",
            str(Path(__file__).resolve().parents[1]),
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "--output",
            str(report_path),
            "--rebuild",
            "--force",
        ]
    )

    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "rebuilt"
    assert report["before"]["contaminated"] is True
    assert report["after"]["contaminated"] is False
    assert report["after"]["counts"]["air_stations"] == 0
    assert Path(report["backup"]["path"]).is_file()
    assert report["backup"]["sha256"]
    assert all(Path(path).is_file() for path in report["preserved_original_files"])
