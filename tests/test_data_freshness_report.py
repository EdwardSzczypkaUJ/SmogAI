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
    assert all(row["measurement_age_hours"] is not None for row in report["parameters"])
    assert all(row["collection_age_hours"] is not None for row in report["parameters"])
    assert report["thresholds_hours"] == {"fresh": 14.0, "stale": 22.0}

    files = write_freshness_report(report, tmp_path / "freshness")
    assert json.loads((tmp_path / "freshness" / "data-freshness-latest.json").read_text(encoding="utf-8"))["overall_status"] == "fresh"
    assert "Aktualność danych pomiarowych" in open(files["html"], encoding="utf-8").read()


def test_freshness_report_marks_old_measurements_stale(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    app_config.operations.freshness_hours = 1
    app_config.operations.freshness_stale_hours = 2
    with session_scope(engine) as session:
        report = build_freshness_report(
            session,
            app_config,
            now=datetime.now(UTC) + timedelta(hours=10),
        )
    assert report["overall_status"] == "stale"


def test_freshness_report_warning_does_not_mean_stale(engine, app_config) -> None:
    seed_basic(engine, hours=2)
    with session_scope(engine) as session:
        report = build_freshness_report(
            session,
            app_config,
            now=datetime.now(UTC) + timedelta(hours=16),
        )
    assert report["overall_status"] == "warning"
    assert all(row["status"] == "warning" for row in report["parameters"])
