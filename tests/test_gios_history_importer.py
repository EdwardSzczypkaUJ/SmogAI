from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from smog_ai.collectors.gios_history import (
    GiosHistoryImporter,
    HistoryImportOptions,
    gios_history_status,
    parse_gios_archival_cet,
    parse_prepared_hourly_workbook,
)
from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, AirSensor, AirStation


class FakeGiosHistoryHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        del headers
        query = dict(params or {})
        self.calls.append((url, query))
        if url.endswith('/metadata/stations'):
            return {
                'Lista metadanych stacji pomiarowych': [
                    {
                        'Kod stacji': 'SlKatoKossut',
                        'Nazwa stacji': 'Katowice, Kossutha',
                        'Województwo': 'ŚLĄSKIE',
                        'Miejscowość': 'Katowice',
                        'Adres': 'ul. Kossutha',
                        'WGS84 φ N': '50.2646',
                        'WGS84 λ E': '18.9750',
                    }
                ],
                'totalPages': 1,
            }
        if url.endswith('/metadata/sensors'):
            return {
                'Lista metadanych stanowisk pomiarowych': [
                    {
                        'Kod stanowiska': 'SlKatoKossut-PM10-1g',
                        'Kod stacji': 'SlKatoKossut',
                        'Nazwa stacji': 'Katowice, Kossutha',
                        'Wskaźnik - kod': 'PM10',
                        'Wskaźnik': 'pył zawieszony PM10',
                        'Czas uśredniania': '1-godzinny',
                        'Typ pomiaru': 'automatyczny',
                        'Województwo': 'ŚLĄSKIE',
                    },
                    {
                        'Kod stanowiska': 'SlKatoKossut-PM10-24h',
                        'Kod stacji': 'SlKatoKossut',
                        'Nazwa stacji': 'Katowice, Kossutha',
                        'Wskaźnik - kod': 'PM10',
                        'Wskaźnik': 'pył zawieszony PM10',
                        'Czas uśredniania': '24-godzinny',
                        'Typ pomiaru': 'manualny',
                        'Województwo': 'ŚLĄSKIE',
                    },
                ],
                'totalPages': 1,
            }
        if url.endswith('/archivalData/getDataForAllStationsByYearAndVoivodeship'):
            assert query['year'] == '2024'
            assert query['voivodeship'] == 'ŚLĄSKIE'
            assert query['pollution'] == 'PM10'
            return {
                'Lista archiwalnych wyników pomiarów': [
                    {
                        'Nazwa stacji': 'Katowice, Kossutha',
                        'Kod stanowiska': 'SlKatoKossut-PM10-1g',
                        'Data': '2024-01-01 01:00:00',
                        'Wartość': 12.5,
                    },
                    {
                        'Nazwa stacji': 'Katowice, Kossutha',
                        'Kod stanowiska': 'SlKatoKossut-PM10-1g',
                        'Data': '2024-01-01 02:00:00',
                        'Wartość': 13.5,
                    },
                    {
                        'Nazwa stacji': 'Katowice, Kossutha',
                        'Kod stanowiska': 'SlKatoKossut-PM10-24h',
                        'Data': '2024-01-01 00:00:00',
                        'Wartość': 99.0,
                    },
                ],
                'totalPages': 1,
            }
        raise AssertionError(f'Unexpected URL: {url}')

    def get_bytes(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        del url, params, headers
        raise AssertionError('Prepared ZIP download not expected in this test')

    def close(self) -> None:
        pass


def _make_workbook(path: Path) -> None:
    frame = pd.DataFrame(
        [
            ['Wyniki PM10', None, None],
            ['Kod stacji', 'MpKrakBujaka', 'SlKatoKossut'],
            ['Wskaźnik', 'PM10', 'PM10'],
            ['Jednostka', 'µg/m3', 'µg/m3'],
            ['Czas uśredniania', '1g', '1g'],
            ['Typ pomiaru', 'automatyczny', 'automatyczny'],
            [datetime(2024, 1, 1, 1, 0), 10.5, 20.0],
            [datetime(2024, 1, 1, 2, 0), '11,5', -1.0],
            [datetime(2024, 1, 1, 3, 0), None, 22.0],
        ]
    )
    frame.to_excel(path, index=False, header=False, engine='openpyxl')


def test_prepared_workbook_parser_preserves_fixed_cet_and_station_codes(tmp_path: Path) -> None:
    path = tmp_path / '2024_PM10_1g.xlsx'
    _make_workbook(path)

    series = parse_prepared_hourly_workbook(path, parameter='PM10', year=2024)

    assert [item.station_code for item in series] == ['MpKrakBujaka', 'SlKatoKossut']
    assert series[0].measurement_times[0] == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    assert series[0].values == [10.5, 11.5, None]
    assert series[1].values == [20.0, -1.0, 22.0]


def test_archival_timestamp_is_always_interpreted_as_fixed_cet() -> None:
    assert parse_gios_archival_cet('2024-07-01 12:00:00') == datetime(
        2024, 7, 1, 11, 0, tzinfo=UTC
    )
    assert parse_gios_archival_cet('2024-01-01 12:00:00') == datetime(
        2024, 1, 1, 11, 0, tzinfo=UTC
    )


def test_annual_api_history_import_is_idempotent(engine, app_config, tmp_path: Path) -> None:
    options = HistoryImportOptions(
        start_year=2024,
        end_year=2024,
        source='api',
        pollutants=('PM10',),
        voivodeships=('ŚLĄSKIE',),
        request_interval_seconds=30.0,
        page_size=500,
        resume=False,
        cache_dir=tmp_path / 'gios-history-cache',
    )

    with session_scope(engine) as session:
        importer = GiosHistoryImporter(session, app_config, options, http=FakeGiosHistoryHttp())
        importer.rate_limiter.interval_seconds = 0.0
        first = importer.run()

    assert first.errors == 0
    assert first.inserted == 2
    assert first.skipped >= 1  # manual 24-hour series is intentionally rejected

    # Force a fresh transport pass but keep the same natural keys in SQLite.
    options_second = HistoryImportOptions(
        start_year=2024,
        end_year=2024,
        source='api',
        pollutants=('PM10',),
        voivodeships=('ŚLĄSKIE',),
        request_interval_seconds=30.0,
        page_size=500,
        resume=False,
        refresh_cache=True,
        cache_dir=tmp_path / 'gios-history-cache',
    )
    with session_scope(engine) as session:
        importer = GiosHistoryImporter(
            session,
            app_config,
            options_second,
            http=FakeGiosHistoryHttp(),
        )
        importer.rate_limiter.interval_seconds = 0.0
        second = importer.run()

    assert second.errors == 0
    assert second.inserted == 0
    assert second.skipped >= 2

    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(AirStation)) == 1
        assert session.scalar(select(func.count()).select_from(AirSensor)) == 1
        assert session.scalar(select(func.count()).select_from(AirMeasurement)) == 2
        rows = session.scalars(select(AirMeasurement).order_by(AirMeasurement.measurement_time)).all()
        assert [row.value for row in rows] == [12.5, 13.5]
        assert rows[0].measurement_time.replace(tzinfo=UTC) == datetime(
            2024, 1, 1, 0, 0, tzinfo=UTC
        )


def test_history_status_reports_coverage(engine) -> None:
    with session_scope(engine) as session:
        status = gios_history_status(session)
    assert status['PM10']['rows'] == 0
    assert status['PM10']['production_training_ready'] is False
    assert status['PM2.5']['rows'] == 0
