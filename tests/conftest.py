from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from smog_ai.config import (
    APIConfig,
    AppConfig,
    BackupConfig,
    HealthConfig,
    LockingConfig,
    PathsConfig,
    PublicationConfig,
    QualityConfig,
    TrainingConfig,
)
from smog_ai.database.engine import create_db_engine, init_database, session_scope
from smog_ai.database.repository import (
    insert_air_measurements,
    insert_weather_measurements,
    upsert_air_sensor,
    upsert_air_station,
    upsert_station_match,
    upsert_weather_station,
)
from smog_ai.domain import (
    AirMeasurementRecord,
    AirSensorRecord,
    AirStationRecord,
    WeatherMeasurementRecord,
    WeatherStationRecord,
)

# Runtime configuration is intentionally kept outside the repository. A developer
# can therefore start pytest from a shell that already contains production secrets
# and paths. Every test must be isolated from those values.
_TEST_RUNTIME_ENV_PREFIXES = (
    "SMOG_AI_",
    "SPACES_",
    "LANGFUSE_",
    "AWS_",
)
_TEST_RUNTIME_ENV_KEYS = {
    "DISPLAY_TIMEZONE",
    "PUBLISH_API_URL",
    "PUBLISH_API_TOKEN",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "PYTHONPATH",
    "PYTHONHOME",
}


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from reading or mutating an installed production runtime."""

    names = set(_TEST_RUNTIME_ENV_KEYS)
    names.update(
        key
        for key in os.environ
        if key.startswith(_TEST_RUNTIME_ENV_PREFIXES)
    )
    for key in names:
        monkeypatch.delenv(key, raising=False)

    # A test that loads config.example.yaml must still remain in test mode even
    # when the calling shell had SMOG_AI_ENV=production.
    monkeypatch.setenv("SMOG_AI_ENV", "test")



@pytest.fixture
def app_config(tmp_path: Path, isolate_runtime_environment: None) -> AppConfig:
    paths = PathsConfig(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "smog.db",
        models_dir=tmp_path / "models",
        snapshots_dir=tmp_path / "snapshots",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        temp_dir=tmp_path / "tmp",
        imgw_metadata_csv=tmp_path / "imgw.csv",
    )
    config = AppConfig(
        environment="test",
        source_host_id="pytest-host",
        paths=paths,
        api=APIConfig(gios_request_interval_seconds=0),
        quality=QualityConfig(stale_air_hours=100000, stale_weather_hours=100000, verify_tolerance_minutes=90),
        training=TrainingConfig(
            horizons_hours=[6],
            parameters=["PM10"],
            minimum_training_rows=20,
            validation_fraction=0.2,
            algorithms=["persistence", "historical_mean", "hist_gradient_boosting"],
            max_training_days=730,
        ),
        publication=PublicationConfig(enabled=False, api_url="https://example.test/api/v1"),
        locking=LockingConfig(lease_seconds=60, heartbeat_seconds=30),
        health=HealthConfig(
            minimum_free_disk_gb=0,
            max_last_collection_age_hours=100000,
            max_last_forecast_age_hours=100000,
            publication_probe_enabled=False,
            source_api_probe_enabled=False,
        ),
        backup=BackupConfig(daily_keep=2, weekly_keep=2, monthly_keep=2),
    )
    config.ensure_directories()
    return config


@pytest.fixture
def engine(app_config: AppConfig) -> Engine:
    engine = create_db_engine(app_config)
    init_database(engine)
    yield engine
    engine.dispose()


def seed_basic(engine: Engine, *, hours: int = 36) -> dict[str, int]:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours)
    with session_scope(engine) as session:
        air = upsert_air_station(
            session,
            AirStationRecord(
                source_id="A1",
                station_name="Kraków test",
                city_name="Kraków",
                latitude=50.0614,
                longitude=19.9383,
            ),
        )
        sensor = upsert_air_sensor(
            session,
            AirSensorRecord(
                source_id="S1",
                station_source_id="A1",
                parameter_code="PM10",
            ),
        )
        weather = upsert_weather_station(
            session,
            WeatherStationRecord(
                source_id="12566",
                station_name="Kraków",
                latitude=50.0777,
                longitude=19.7848,
                metadata_source="test",
            ),
        )
        session.flush()
        upsert_station_match(
            session,
            air_station_id=air.id,
            weather_station_id=weather.id,
            distance_km=11.0,
            acceptable=True,
        )
        air_records = []
        weather_records = []
        for index in range(hours + 1):
            timestamp = start + timedelta(hours=index)
            air_records.append(
                AirMeasurementRecord(
                    station_source_id="A1",
                    sensor_source_id="S1",
                    parameter="PM10",
                    measurement_time=timestamp,
                    value=20.0 + index * 0.3 + (index % 6),
                )
            )
            weather_records.append(
                WeatherMeasurementRecord(
                    station_source_id="12566",
                    station_name="Kraków",
                    measurement_time=timestamp,
                    temperature_c=5 + index * 0.1,
                    humidity_percent=60 - index * 0.2,
                    pressure_hpa=1010 + index * 0.05,
                    precipitation_mm=0,
                    wind_speed_mps=2,
                    wind_direction_deg=180,
                )
            )
        insert_air_measurements(session, air_records)
        insert_weather_measurements(session, weather_records)
        return {"air_station_id": air.id, "sensor_id": sensor.id, "weather_station_id": weather.id}
