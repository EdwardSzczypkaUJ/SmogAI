from __future__ import annotations

from smog_ai.collectors.imgw_archive import ImgwArchiveCollector
from tests.test_imgw_archive import FakeBinaryHttp


def test_imgw_archive_discovers_station_year_layout_when_monthly_absent(
    app_config,
) -> None:
    app_config.imgw_archive.start_year = 2025
    app_config.imgw_archive.end_year = 2025
    html = b'''<html><body>
      <a href="2025_100_s.zip">station 100</a>
      <a href="2025_105_s.zip">station 105</a>
    </body></html>'''
    collector = ImgwArchiveCollector(
        app_config,
        client=FakeBinaryHttp({"/2025/": html}),
    )
    result = collector.list_objects(2025)
    assert [(item.kind, item.station_id, item.month) for item in result] == [
        ("station_year", "100", None),
        ("station_year", "105", None),
    ]


def test_imgw_archive_prefers_monthly_layout_when_both_are_listed(
    app_config,
) -> None:
    app_config.imgw_archive.start_year = 2026
    app_config.imgw_archive.end_year = 2026
    html = b'''<html><body>
      <a href="2026_01_s.zip">monthly</a>
      <a href="2026_100_s.zip">station</a>
    </body></html>'''
    collector = ImgwArchiveCollector(
        app_config,
        client=FakeBinaryHttp({"/2026/": html}),
    )
    result = collector.list_objects(2026)
    assert len(result) == 1
    assert result[0].kind == "monthly_network"
    assert result[0].month == 1
