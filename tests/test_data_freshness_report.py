from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from smog_ai.database.engine import session_scope
from smog_ai.reports.freshness import build_freshness_report, write_freshness_report
from tests.conftest import seed_basic


def test_freshness_report_covers_air_and_weather(engine, app_config, tmp_path) -> None:
    seed_basic(engine, hours=2)
    now = datetime.now(UTC) + timedelta(minutes=5)
    with session_scope(engine) as session:
        report = build_freshness_report(session, app_config, now=now)

    assert report["overall_status"] == "fresh"
    parameters = {(row["source"], row["parameter"]) for row in report["parameters"]}
    assert ("GIOS", "PM10") in parameters
    assert ("IMGW", "temperature_c") in parameters
    assert all(row["measurement_end"] for row in report["parameters"])

    files = write_freshness_report(report, tmp_path / "freshness")
    assert json.loads((tmp_path / "freshness" / "data-freshness-latest.json").read_text(encoding="utf-8"))["overall_status"] == "fresh"
    assert "Aktualność danych pomiarowych" in open(files["html"], encoding="utf-8").read()


def test_freshness_report_marks_old_measurements_stale(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    app_config.quality.stale_air_hours = 1
    app_config.quality.stale_weather_hours = 1
    with session_scope(engine) as session:
        report = build_freshness_report(
            session,
            app_config,
            now=datetime.now(UTC) + timedelta(hours=10),
        )
    assert report["overall_status"] == "stale"
