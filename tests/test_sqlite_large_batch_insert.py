from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, WeatherMeasurement
from smog_ai.database.repository import (
    insert_air_measurements,
    insert_weather_measurements,
    upsert_air_sensor,
    upsert_air_station,
    upsert_weather_station,
)
from smog_ai.domain import (
    AirMeasurementRecord,
    AirSensorRecord,
    AirStationRecord,
    WeatherMeasurementRecord,
    WeatherStationRecord,
)


def _set_variable_limit(session, value: int) -> int:
    raw = session.connection().connection.driver_connection
    return int(raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, value))


def test_weather_measurements_use_executemany_below_sqlite_variable_limit(engine) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        WeatherMeasurementRecord(
            station_source_id="12600",
            station_name="Bielsko-Biała",
            measurement_time=start + timedelta(hours=index),
            temperature_c=5.0 + index / 100,
            humidity_percent=60.0,
            pressure_hpa=1000.0,
            precipitation_mm=None,
            wind_speed_mps=2.0,
            wind_direction_deg=180.0,
        )
        for index in range(250)
    ]

    with session_scope(engine) as session:
        upsert_weather_station(
            session,
            WeatherStationRecord(
                source_id="12600",
                station_name="Bielsko-Biała",
            ),
        )
        previous = _set_variable_limit(session, 100)
        try:
            first = insert_weather_measurements(session, records)
            second = insert_weather_measurements(session, records)
        finally:
            _set_variable_limit(session, previous)

    assert first == (250, 0)
    assert second == (0, 250)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(WeatherMeasurement)) == 250


def test_air_measurements_use_executemany_below_sqlite_variable_limit(engine) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        AirMeasurementRecord(
            station_source_id="A1",
            sensor_source_id="S1",
            parameter="PM10",
            measurement_time=start + timedelta(hours=index),
            value=20.0 + index / 100,
        )
        for index in range(250)
    ]

    with session_scope(engine) as session:
        upsert_air_station(
            session,
            AirStationRecord(
                source_id="A1",
                station_name="Air test",
                city_name="Kraków",
                latitude=50.06,
                longitude=19.94,
            ),
        )
        upsert_air_sensor(
            session,
            AirSensorRecord(
                source_id="S1",
                station_source_id="A1",
                parameter_code="PM10",
            ),
        )
        previous = _set_variable_limit(session, 100)
        try:
            first = insert_air_measurements(session, records)
            second = insert_air_measurements(session, records)
        finally:
            _set_variable_limit(session, previous)

    assert first == (250, 0)
    assert second == (0, 250)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(AirMeasurement)) == 250
