from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
import zipfile

import pandas as pd
from sqlalchemy import func, select

from smog_ai.collectors.gios_history import (
    GiosHistoryImporter,
    HistoryImportOptions,
    parse_prepared_hourly_workbook,
)
from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, AirStation


def _official_like_pm25_workbook(path: Path) -> None:
    frame = pd.DataFrame(
        [
            ["Nr", 1, 2],
            ["Kod stacji", "SlKatoKossut", "MpKrakBujaka"],
            ["Wskaźnik", "PM2.5", "PM2.5"],
            ["Jednostka", "µg/m3", "µg/m3"],
            ["Czas uśredniania", "1g", "1g"],
            ["Typ pomiaru", "automatyczny", "automatyczny"],
            [datetime(2022, 1, 1, 1, 0), 10.5, 20.0],
            [datetime(2022, 1, 1, 2, 0), 11.5, None],
            [datetime(2022, 1, 1, 3, 0), 12.5, 22.0],
        ]
    )
    frame.to_excel(path, index=False, header=False, engine="openpyxl")


def _archive_bytes(tmp_path: Path) -> bytes:
    workbook = tmp_path / "2022_PM25_1g.xlsx"
    _official_like_pm25_workbook(workbook)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(workbook, arcname=workbook.name)
    return buffer.getvalue()


class PreparedArchiveWithoutMetadataHttp:
    def __init__(self, archive: bytes) -> None:
        self.archive = archive

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        del url, params, headers
        raise RuntimeError("metadata endpoint temporarily unavailable")

    def get_bytes(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        del url, params, headers
        return self.archive

    def close(self) -> None:
        pass


def test_real_layout_prefers_kod_stacji_over_ordinal_nr_row(tmp_path: Path) -> None:
    path = tmp_path / "2022_PM25_1g.xlsx"
    _official_like_pm25_workbook(path)

    series = parse_prepared_hourly_workbook(
        path,
        parameter="PM2.5",
        year=2022,
    )

    assert [item.station_code for item in series] == [
        "SlKatoKossut",
        "MpKrakBujaka",
    ]
    assert series[0].measurement_times[0] == datetime(
        2022,
        1,
        1,
        0,
        0,
        tzinfo=UTC,
    )


def test_prepared_pm25_import_persists_without_live_metadata_api(
    engine,
    app_config,
    tmp_path: Path,
) -> None:
    from smog_ai.collectors.gios_history import ALL_VOIVODESHIPS

    options = HistoryImportOptions(
        start_year=2022,
        end_year=2022,
        source="prepared",
        pollutants=("PM2.5",),
        voivodeships=ALL_VOIVODESHIPS,
        request_interval_seconds=31.0,
        page_size=500,
        resume=False,
        cache_dir=tmp_path / "history-cache",
        cache_mode="local",
        insert_batch_size=500,
    )

    http = PreparedArchiveWithoutMetadataHttp(_archive_bytes(tmp_path))
    with session_scope(engine) as session:
        importer = GiosHistoryImporter(
            session,
            app_config,
            options,
            http=http,
        )
        result = importer.run()

    assert result.errors == 0
    assert result.inserted == 5
    assert result.warnings >= 2  # two missing cells plus metadata fallback

    with session_scope(engine) as session:
        count = int(
            session.scalar(
                select(func.count(AirMeasurement.id)).where(
                    AirMeasurement.parameter == "PM2.5",
                    AirMeasurement.source_status == "GIOS_PREPARED_ARCHIVE_1H",
                )
            )
            or 0
        )
        codes = set(
            session.scalars(
                select(AirStation.station_code).where(
                    AirStation.station_code.in_(
                        ["SlKatoKossut", "MpKrakBujaka"]
                    )
                )
            ).all()
        )

    assert count == 5
    assert codes == {"SlKatoKossut", "MpKrakBujaka"}


def test_json_formatter_preserves_progress_fields() -> None:
    import json
    import logging

    from smog_ai.logging_config import JsonFormatter

    record = logging.LogRecord(
        name="smog_ai.collectors.gios_history",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="progress",
        args=(),
        exc_info=None,
    )
    record.stage = "gios_history_prepared"
    record.year = 2022
    record.parameter = "PM2.5"
    record.series_completed = 10
    record.series_total = 89
    record.percent = 11.24

    payload = json.loads(JsonFormatter().format(record))

    assert payload["stage"] == "gios_history_prepared"
    assert payload["year"] == 2022
    assert payload["parameter"] == "PM2.5"
    assert payload["series_completed"] == 10
    assert payload["series_total"] == 89
    assert payload["percent"] == 11.24
