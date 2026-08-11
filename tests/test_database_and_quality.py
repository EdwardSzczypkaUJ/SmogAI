from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, DataQualityFlag, WeatherMeasurement
from smog_ai.database.repository import insert_air_measurements
from smog_ai.domain import AirMeasurementRecord
from smog_ai.processing.validation import validate_data
from tests.conftest import seed_basic


def test_sqlite_wal_and_foreign_keys(engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_air_measurement_deduplication(engine) -> None:
    ids = seed_basic(engine, hours=1)
    with session_scope(engine) as session:
        record = AirMeasurementRecord("A1", "S1", "PM10", datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1), 10)
        first = insert_air_measurements(session, [record])
        second = insert_air_measurements(session, [record])
        assert first[0] == 1
        assert second == (0, 1)


def test_validation_flags_negative_pm(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    with session_scope(engine) as session:
        measurement = session.scalar(select(AirMeasurement).limit(1))
        measurement.value = -5
        stats = validate_data(session, app_config)
        assert stats.errors >= 1
        assert session.scalar(select(func.count()).select_from(DataQualityFlag)) >= 1


def test_validation_flags_bad_humidity(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    with session_scope(engine) as session:
        measurement = session.scalar(select(WeatherMeasurement).limit(1))
        measurement.humidity_percent = 140
        stats = validate_data(session, app_config)
        assert stats.errors >= 1
        assert measurement.is_valid is False
