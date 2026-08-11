from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, WeatherMeasurement
from smog_ai.database.repository import (
    insert_weather_measurements,
    merge_weather_measurements,
    upsert_weather_station,
)
from smog_ai.domain import WeatherMeasurementRecord, WeatherStationRecord
from smog_ai.range_backfill.audit import CoverageAuditor
from smog_ai.range_backfill.contracts import (
    CoverageReport,
    DatasetCoverage,
    TimeInterval,
)
from smog_ai.range_backfill.planner import BackfillPlanner


def test_precipitation_audit_uses_six_hour_slots_not_false_hourly_gaps(
    engine,
    app_config,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with session_scope(engine) as session:
        upsert_weather_station(
            session,
            WeatherStationRecord(
                source_id="WX1",
                station_name="Weather",
                metadata_source="test",
            ),
        )
        insert_weather_measurements(
            session,
            [
                WeatherMeasurementRecord(
                    station_source_id="WX1",
                    station_name="Weather",
                    measurement_time=start + timedelta(hours=offset),
                    precipitation_mm=1.0,
                    precipitation_accumulation_period_hours=6,
                )
                for offset in (0, 6, 12, 18)
            ],
        )

    with session_scope(engine) as session:
        report = CoverageAuditor(
            session,
            precipitation_cadence_hours=6,
        ).audit(
            TimeInterval(start, start + timedelta(days=1)),
            air_parameters=(),
            weather_parameters=("precipitation_mm",),
        )

    item = report.find("weather", "precipitation_mm")
    assert item is not None
    assert item.cadence_hours == 6
    assert item.expected_slots == 4
    assert item.present_slots == 4
    assert item.missing_intervals == ()


def test_planner_routes_recent_and_historical_gaps_to_correct_bridges() -> None:
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    requested = TimeInterval(
        datetime(2024, 1, 1, tzinfo=UTC),
        now,
    )
    report = CoverageReport(
        requested=requested,
        generated_at=now,
        display_timezone="Europe/Warsaw",
        datasets=(
            DatasetCoverage(
                dataset="air",
                parameter="PM10",
                requested=requested,
                cadence_hours=1,
                minimum_stations=1,
                expected_slots=1,
                present_slots=0,
                undercovered_slots=0,
                missing_intervals=(
                    TimeInterval(
                        datetime(2024, 2, 1, tzinfo=UTC),
                        datetime(2024, 2, 4, tzinfo=UTC),
                    ),
                    TimeInterval(
                        datetime(2026, 8, 7, tzinfo=UTC),
                        datetime(2026, 8, 8, 8, tzinfo=UTC),
                    ),
                ),
            ),
            DatasetCoverage(
                dataset="weather",
                parameter="temperature_c",
                requested=requested,
                cadence_hours=1,
                minimum_stations=1,
                expected_slots=1,
                present_slots=0,
                undercovered_slots=0,
                missing_intervals=(
                    TimeInterval(
                        datetime(2025, 1, 1, tzinfo=UTC),
                        datetime(2026, 1, 1, tzinfo=UTC),
                    ),
                ),
            ),
        ),
    )

    plan = BackfillPlanner(now=now, cache_mode="hybrid").plan(report)
    providers = {action.provider for action in plan.actions}
    assert "gios_prepared" in providers
    assert "gios_live" in providers
    assert "imgw_archive" in providers
    assert all(action.cache_mode == "hybrid" for action in plan.actions)


def test_weather_merge_fills_null_fields_without_overwriting_known_values(
    engine,
    app_config,
) -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    with session_scope(engine) as session:
        upsert_weather_station(
            session,
            WeatherStationRecord(
                source_id="WX1",
                station_name="Weather",
                metadata_source="test",
            ),
        )
        insert_weather_measurements(
            session,
            [
                WeatherMeasurementRecord(
                    station_source_id="WX1",
                    station_name="Weather",
                    measurement_time=timestamp,
                    temperature_c=10.0,
                    precipitation_mm=None,
                )
            ],
        )

    with session_scope(engine) as session:
        inserted, updated, unchanged = merge_weather_measurements(
            session,
            [
                WeatherMeasurementRecord(
                    station_source_id="WX1",
                    station_name="Weather",
                    measurement_time=timestamp,
                    temperature_c=99.0,
                    precipitation_mm=2.5,
                    precipitation_accumulation_period_hours=6,
                )
            ],
        )
        assert (inserted, updated, unchanged) == (0, 1, 0)

    with session_scope(engine) as session:
        row = session.scalar(select(WeatherMeasurement))
        assert row is not None
        assert row.temperature_c == 10.0
        assert row.precipitation_mm == 2.5
        assert row.precipitation_accumulation_period_hours == 6
        assert session.scalar(select(func.count()).select_from(WeatherMeasurement)) == 1


def test_audit_package_scope_uses_newest_audit_and_preserves_its_parameters(
    app_config,
    tmp_path,
) -> None:
    import json
    import zipfile

    from smog_ai.range_backfill.service import resolve_requested_scope

    older = {
        "generated_at_utc": "2025-01-01T00:00:00+00:00",
        "requested_range": {
            "start_utc": "2024-01-01T00:00:00+00:00",
            "end_exclusive_utc": "2024-02-01T00:00:00+00:00",
        },
        "datasets": {
            "air:PM10": {"dataset": "air", "parameter": "PM10"},
        },
    }
    newer = {
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "thresholds": {
            "minimum_air_stations_per_hour": 5,
            "minimum_weather_stations_per_hour": 7,
        },
        "requested_range": {
            "start_utc": "2025-01-01T00:00:00+00:00",
            "end_exclusive_utc": "2025-07-01T00:00:00+00:00",
        },
        "datasets": {
            "air:PM2.5": {"dataset": "air", "parameter": "PM2.5"},
            "weather:temperature_c": {
                "dataset": "weather",
                "parameter": "temperature_c",
            },
            "weather:precipitation_mm": {
                "dataset": "weather",
                "parameter": "precipitation_mm",
            },
        },
    }
    package = tmp_path / "ranges.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "data-ranges/data-range-audit-20250101T000000Z.json",
            json.dumps(older),
        )
        archive.writestr(
            "data-ranges/data-range-audit-20260101T000000Z.json",
            json.dumps(newer),
        )

    requested, air, weather, metadata = resolve_requested_scope(
        app_config,
        audit_package=package,
        parameters=None,
    )

    assert requested.start == datetime(2025, 1, 1, tzinfo=UTC)
    assert requested.end == datetime(2025, 7, 1, tzinfo=UTC)
    assert air == ("PM2.5",)
    assert weather == ("temperature_c", "precipitation_mm")
    assert metadata["audit_source"].endswith(
        "data-range-audit-20260101T000000Z.json"
    )
    assert metadata["audit_minimum_air_stations"] == 5
    assert metadata["audit_minimum_weather_stations"] == 7


def test_explicit_all_parameters_override_audit_package_scope(
    app_config,
    tmp_path,
) -> None:
    import json
    import zipfile

    from smog_ai.range_backfill.audit import (
        DEFAULT_AIR_PARAMETERS,
        DEFAULT_WEATHER_PARAMETERS,
    )
    from smog_ai.range_backfill.service import resolve_requested_scope

    payload = {
        "requested_range": {
            "start_utc": "2025-01-01T00:00:00+00:00",
            "end_exclusive_utc": "2025-02-01T00:00:00+00:00",
        },
        "datasets": {
            "air:PM2.5": {"dataset": "air", "parameter": "PM2.5"},
        },
    }
    package = tmp_path / "ranges.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "data-range-audit-20260101T000000Z.json",
            json.dumps(payload),
        )

    _, air, weather, _ = resolve_requested_scope(
        app_config,
        audit_package=package,
        parameters="ALL",
    )

    assert air == DEFAULT_AIR_PARAMETERS
    assert weather == DEFAULT_WEATHER_PARAMETERS


def test_coverage_auditor_respects_requested_station_threshold(
    engine,
    app_config,
) -> None:
    from smog_ai.database.repository import upsert_air_sensor, upsert_air_station
    from smog_ai.domain import (
        AirMeasurementRecord,
        AirSensorRecord,
        AirStationRecord,
    )
    from smog_ai.database.repository import insert_air_measurements

    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    with session_scope(engine) as session:
        upsert_air_station(
            session,
            AirStationRecord(
                source_id="S1",
                station_name="Station 1",
                latitude=50.0,
                longitude=19.0,
            ),
        )
        upsert_air_sensor(
            session,
            AirSensorRecord(
                source_id="P1",
                station_source_id="S1",
                parameter_code="PM10",
                parameter_name="PM10",
            ),
        )
        insert_air_measurements(
            session,
            [
                AirMeasurementRecord(
                    station_source_id="S1",
                    sensor_source_id="P1",
                    parameter="PM10",
                    measurement_time=timestamp,
                    value=10.0,
                )
            ],
        )

    with session_scope(engine) as session:
        report = CoverageAuditor(session).audit(
            TimeInterval(timestamp, timestamp + timedelta(hours=1)),
            air_parameters=("PM10",),
            weather_parameters=(),
            minimum_air_stations=5,
        )

    item = report.find("air", "PM10")
    assert item is not None
    assert item.present_slots == 0
    assert item.undercovered_slots == 1
    assert item.missing_slots == 1
