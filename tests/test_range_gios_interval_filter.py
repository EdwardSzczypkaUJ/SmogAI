from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from smog_ai.collectors.gios_history import GiosHistoryImporter, HistoryImportOptions
from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement

from tests.test_gios_history_importer import FakeGiosHistoryHttp


def test_gios_api_interval_filter_persists_only_requested_gap(
    engine,
    app_config,
    tmp_path: Path,
) -> None:
    options = HistoryImportOptions(
        start_year=2024,
        end_year=2024,
        source="api",
        pollutants=("PM10",),
        voivodeships=("ŚLĄSKIE",),
        request_interval_seconds=30.0,
        resume=False,
        cache_dir=tmp_path / "cache",
        intervals_by_pollutant={
            "PM10": (
                (
                    datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
                    datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
                ),
            )
        },
    )

    with session_scope(engine) as session:
        importer = GiosHistoryImporter(
            session,
            app_config,
            options,
            http=FakeGiosHistoryHttp(),
        )
        importer.rate_limiter.interval_seconds = 0.0
        result = importer.run()

    assert result.errors == 0
    assert result.inserted == 1
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(AirMeasurement)) == 1
        row = session.scalar(select(AirMeasurement))
        assert row is not None
        assert row.value == 13.5


def test_gios_history_reports_nested_progress_for_range_backfill(
    engine,
    app_config,
    tmp_path: Path,
) -> None:
    events: list[tuple[float, str, dict[str, object]]] = []
    options = HistoryImportOptions(
        start_year=2024,
        end_year=2024,
        source="api",
        pollutants=("PM10",),
        voivodeships=("ŚLĄSKIE",),
        request_interval_seconds=30.0,
        resume=False,
        cache_dir=tmp_path / "cache",
        intervals_by_pollutant={
            "PM10": (
                (
                    datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
                    datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
                ),
            )
        },
    )

    with session_scope(engine) as session:
        importer = GiosHistoryImporter(
            session,
            app_config,
            options,
            http=FakeGiosHistoryHttp(),
            progress=lambda fraction, task, detail: events.append(
                (fraction, task, dict(detail))
            ),
        )
        importer.rate_limiter.interval_seconds = 0.0
        result = importer.run()

    assert result.errors == 0
    assert events
    assert any(event[2].get("stage") == "gios_history_api" for event in events)
    assert events[-1][0] == 1.0
    assert all(0.0 <= event[0] <= 1.0 for event in events)
