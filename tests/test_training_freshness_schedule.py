from __future__ import annotations

from smog_ai.database.engine import session_scope
from smog_ai.database.models import TrainingRun
from smog_ai.monitoring.health import run_healthcheck
from smog_ai.time_utils import utc_now


def test_training_freshness_defaults_to_twelve_hours(engine, app_config) -> None:
    with session_scope(engine) as session:
        session.add(
            TrainingRun(
                status="success_quality_experimental",
                started_at=utc_now(),
                finished_at=utc_now(),
            )
        )
    with session_scope(engine) as session:
        result = run_healthcheck(session, engine, app_config)
        assert result.checks["training"]["status"] == "ok"
        assert result.checks["training"]["maximum_hours"] == 12


def test_schedule_uses_six_hour_interval_and_prevents_overlap() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "scripts" / "Install-SmogAI-RefreshSchedule.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "[int]$IntervalHours = 8" in source
    assert "[int]$TrainingValidityHours = 24" in source
    assert "-MultipleInstances IgnoreNew" in source
