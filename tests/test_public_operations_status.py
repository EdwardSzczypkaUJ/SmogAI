from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from smog_ai.database.engine import session_scope
from smog_ai.database.models import ModelVersion, ProcessLock, TrainingRun
from smog_ai.operations import build_public_operations_status


def test_public_operations_status_is_sanitised_and_uses_test_cadence(
    engine,
    app_config,
) -> None:
    now = datetime.now(UTC)
    with session_scope(engine) as session:
        session.add_all(
            [
                TrainingRun(
                    started_at=now - timedelta(hours=3),
                    finished_at=now - timedelta(hours=2),
                    status="success",
                    summary_json={"training_profile": "quick"},
                ),
                TrainingRun(
                    started_at=now - timedelta(hours=20),
                    finished_at=now - timedelta(hours=19),
                    status="success",
                    summary_json={"training_profile": "full"},
                ),
                ModelVersion(
                    model_name="hourly-PM10",
                    algorithm="hist_gradient_boosting",
                    parameter="PM10",
                    forecast_horizon=1,
                    semantic_version="safe-version",
                    artifact_path=r"C:\secret\models\model.joblib",
                    active=True,
                    activated_at=now - timedelta(hours=1),
                    metrics_json={"quality_status": "approved"},
                ),
                ProcessLock(
                    lock_name="snapshot-hourly-training",
                    process_id=12345,
                    host_name="private-host",
                    owner_token="private-token",
                    started_at=now - timedelta(minutes=5),
                    heartbeat_at=now,
                    expires_at=now + timedelta(minutes=5),
                ),
            ]
        )
        session.flush()
        payload = build_public_operations_status(
            session,
            app_config,
            source_origin_time=now - timedelta(hours=2),
            generated_at=now,
            surface_count=240,
        )

    encoded = json.dumps(payload)
    assert payload["data"]["status_at_publication"] == "fresh"
    assert payload["schedule"] == {
        "serving_refresh_hours": 8,
        "regular_training_hours": 12,
        "heavy_training_hours": 28,
        "deferred_retry_minutes": 30,
        "serving_release_retention": 3,
    }
    assert payload["training"]["state_at_publication"] == "running"
    assert payload["models"][0]["version"] == "safe-version"
    assert "private-host" not in encoded
    assert "private-token" not in encoded
    assert "12345" not in encoded
    assert "secret" not in encoded
