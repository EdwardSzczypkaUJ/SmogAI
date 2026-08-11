from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirStation, ProcessLock, StationMatch, WeatherStation
from smog_ai.database.repository import upsert_air_station, upsert_weather_station
from smog_ai.domain import AirStationRecord, WeatherStationRecord
from smog_ai.errors import LockUnavailable
from smog_ai.locking import ProcessLease
from smog_ai.processing.matching import haversine_km, match_stations, nearest_weather_station


def test_haversine_zero() -> None:
    assert haversine_km(50, 20, 50, 20) == 0


def test_haversine_krakow_warsaw() -> None:
    distance = haversine_km(50.0614, 19.9383, 52.2297, 21.0122)
    assert 240 < distance < 270


def test_haversine_rejects_invalid_latitude() -> None:
    with pytest.raises(ValueError):
        haversine_km(100, 20, 50, 20)


def test_nearest_station(engine) -> None:
    with session_scope(engine) as session:
        air = upsert_air_station(session, AirStationRecord("a", "A", latitude=50, longitude=20))
        near = upsert_weather_station(session, WeatherStationRecord("w1", "W1", latitude=50.1, longitude=20))
        upsert_weather_station(session, WeatherStationRecord("w2", "W2", latitude=53, longitude=20))
        session.flush()
        candidate = nearest_weather_station(air, session.scalars(select(WeatherStation)).all())
        assert candidate.weather_station_id == near.id


def test_match_stations_persists(engine, app_config) -> None:
    with session_scope(engine) as session:
        upsert_air_station(session, AirStationRecord("a", "A", latitude=50, longitude=20))
        upsert_weather_station(session, WeatherStationRecord("w", "W", latitude=50.1, longitude=20))
        session.flush()
        stats = match_stations(session, app_config)
        assert stats.inserted == 1
        assert session.scalar(select(StationMatch)) is not None


def test_process_lease_blocks_second_owner(engine, app_config) -> None:
    first = ProcessLease(engine, app_config, "test-lock").acquire()
    try:
        with pytest.raises(LockUnavailable):
            ProcessLease(engine, app_config, "test-lock").acquire()
    finally:
        first.release()
    with session_scope(engine) as session:
        assert session.get(ProcessLock, "test-lock") is None
