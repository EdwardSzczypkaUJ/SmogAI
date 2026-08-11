from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select

from smog_ai.collectors.imgw_archive import (
    ImgwArchiveCollector,
    collect_imgw_archive,
    load_official_header,
    parse_imgw_archive_zip,
)
from smog_ai.database.engine import session_scope
from smog_ai.database.models import WeatherMeasurement, WeatherStation


class FakeBinaryHttp:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        for suffix, payload in sorted(self.responses.items(), key=lambda item: len(item[0]), reverse=True):
            if suffix in url:
                content_type = "application/zip" if url.endswith(".zip") else "text/html"
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    headers={"Content-Type": content_type},
                    content=payload,
                )
        return httpx.Response(404, request=httpx.Request("GET", url), content=b"missing")

    def close(self) -> None:
        pass


def _archive_row(**values: object) -> list[str]:
    header = load_official_header()
    row = [""] * len(header)
    defaults: dict[str, object] = {
        "NSP": "12566",
        "POST": "KRAKOW-BALICE",
        "ROK": "2026",
        "MC": "6",
        "DZ": "30",
        "GG": "6",
        "KRWR": "180",
        "FWR": "3.5",
        "TEMP": "-1,5",
        "WLGW": "80",
        "HPOW": "1012,3",
        "WO6G": "2,4",
        "WWO6G": "",
    }
    defaults.update(values)
    for code, value in defaults.items():
        row[header.index(code)] = str(value)
    return row


def _archive_zip(*rows: list[str], include_header: bool = False) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=",")
    if include_header:
        writer.writerow(load_official_header())
    writer.writerows(rows)
    payload = output.getvalue().encode("cp1250")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("s_t_06_2026.csv", payload)
    return buffer.getvalue()


def test_imgw_archive_parses_monthly_zip_and_preserves_six_hour_rain_semantics(
    app_config,
) -> None:
    archive_bytes = _archive_zip(_archive_row())
    digest = hashlib.sha256(archive_bytes).hexdigest()

    result = parse_imgw_archive_zip(
        archive_bytes,
        source_url="https://example.test/2026/2026_06_s.zip",
        archive_period="2026-06",
        archive_sha256=digest,
        settings=app_config.imgw_archive,
    )

    assert result.row_count == 1
    assert result.skipped_rows == 0
    assert len(result.stations) == 1
    assert len(result.measurements) == 1
    measurement = result.measurements[0]
    assert measurement.station_source_id == "12566"
    assert measurement.measurement_time == datetime(2026, 6, 30, 6, tzinfo=UTC)
    assert measurement.temperature_c == -1.5
    assert measurement.humidity_percent == 80.0
    assert measurement.pressure_hpa == 1012.3
    assert measurement.precipitation_mm == 2.4
    assert measurement.precipitation_accumulation_period_hours == 6
    assert measurement.raw_json is not None
    semantics = measurement.raw_json["precipitation_semantics"]
    assert semantics["ending_at_measurement_time"] is True
    assert semantics["disaggregated_to_hourly"] is False


def test_imgw_archive_accepts_embedded_header_and_filters_station_ids(app_config) -> None:
    app_config.imgw_archive.station_ids = ["12566"]
    archive_bytes = _archive_zip(
        _archive_row(NSP="12566", POST="KRAKOW-BALICE"),
        _archive_row(NSP="12345", POST="OTHER"),
        include_header=True,
    )
    result = parse_imgw_archive_zip(
        archive_bytes,
        source_url="https://example.test/2026/2026_06_s.zip",
        archive_period="2026-06",
        archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        settings=app_config.imgw_archive,
    )
    assert [row.station_source_id for row in result.measurements] == ["12566"]
    assert result.skipped_rows == 1


def test_imgw_archive_lists_official_monthly_filenames(app_config) -> None:
    app_config.imgw_archive.start_year = 2026
    app_config.imgw_archive.end_year = 2026
    html = b"""
    <html><body>
      <a href=\"2026_05_s.zip\">2026_05_s.zip</a>
      <a href=\"2026_06_s.zip\">2026_06_s.zip</a>
      <a href=\"2026_99_s.zip\">invalid</a>
      <a href=\"2026_12566_s.zip\">legacy-wrong-pattern</a>
    </body></html>
    """
    client = FakeBinaryHttp({"/2026/": html})
    collector = ImgwArchiveCollector(app_config, client=client)
    result = collector.list_objects(2026)
    assert [(item.year, item.month, item.filename) for item in result] == [
        (2026, 5, "2026_05_s.zip"),
        (2026, 6, "2026_06_s.zip"),
    ]


def test_imgw_archive_collection_is_idempotent(engine, app_config, tmp_path: Path) -> None:
    app_config.imgw_archive.start_year = 2026
    app_config.imgw_archive.end_year = 2026
    app_config.imgw_archive.cache_dir = tmp_path / "imgw-cache"
    html = b'<a href="2026_06_s.zip">2026_06_s.zip</a>'
    archive_bytes = _archive_zip(_archive_row())
    client = FakeBinaryHttp({"/2026/": html, "2026_06_s.zip": archive_bytes})

    with session_scope(engine) as session:
        first = collect_imgw_archive(session, app_config, client=client)
    assert first.errors == 0
    assert first.inserted == 1

    # A second run observes the immutable checksum marker and does not reinsert.
    client_second = FakeBinaryHttp({"/2026/": html, "2026_06_s.zip": archive_bytes})
    with session_scope(engine) as session:
        second = collect_imgw_archive(
            session,
            app_config,
            client=client_second,
        )
    assert second.errors == 0
    assert second.inserted == 0
    assert second.details["files_unchanged"] == 1

    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(WeatherStation)) == 1
        assert session.scalar(select(func.count()).select_from(WeatherMeasurement)) == 1
        row = session.scalar(select(WeatherMeasurement))
        assert row is not None
        assert row.precipitation_mm == 2.4
        assert row.precipitation_accumulation_period_hours == 6
