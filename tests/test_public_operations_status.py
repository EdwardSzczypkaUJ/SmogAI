from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from smog_ai.database.engine import session_scope
from smog_ai.database.models import CollectionRun, ModelVersion, ProcessLock, TrainingRun
from smog_ai.operations import (
    _digitalocean_transfer_history,
    build_public_operations_status,
)


def test_public_operations_status_is_sanitised_and_uses_test_cadence(
    engine,
    app_config,
) -> None:
    now = datetime.now(UTC)
    freshness_root = app_config.paths.logs_dir.parent / "reports" / "freshness"
    freshness_root.mkdir(parents=True, exist_ok=True)
    (freshness_root / "data-freshness-20260817T010000Z.json").write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(hours=1)).isoformat(),
                "parameters": [
                    {
                        "source": "GIOS",
                        "parameter": "PM10",
                        "status": "fresh",
                        "age_hours": 1.5,
                        "threshold_hours": 8,
                        "valid_rows": 100,
                        "last_collected_at": (now - timedelta(hours=1)).isoformat(),
                        "measurement_end": (now - timedelta(hours=2)).isoformat(),
                    },
                    {
                        "source": "IMGW",
                        "parameter": "temperature_c",
                        "status": "fresh",
                        "age_hours": 1.0,
                        "threshold_hours": 8,
                        "valid_rows": 80,
                        "last_collected_at": (now - timedelta(hours=1)).isoformat(),
                        "measurement_end": (now - timedelta(hours=2)).isoformat(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with session_scope(engine) as session:
        session.add_all(
            [
                ModelVersion(
                    id="evaluated-candidate",
                    model_name="hourly-PM10-ridge",
                    algorithm="ridge",
                    parameter="PM10",
                    forecast_horizon=1,
                    semantic_version="candidate-version",
                    artifact_path=r"C:\secret\models\candidate.joblib",
                    active=False,
                    metrics_json={
                        "mae": 2.2,
                        "quality_status": "approved",
                        "activated": False,
                        "active_model_comparison": {
                            "candidate_improvement_fraction": -0.1,
                            "active_model_mae": 2.0,
                            "candidate_mae": 2.2,
                            "artifact_path": r"C:\secret\models\active.joblib",
                        },
                    },
                ),
                TrainingRun(
                    started_at=now - timedelta(hours=3),
                    finished_at=now - timedelta(hours=2),
                    status="success",
                    parameter="PM10",
                    best_model_version_id="evaluated-candidate",
                    summary_json={
                        "training_profile": "quick",
                        "target": "PM10",
                        "selected_provider": "ridge",
                        "model_version": "candidate-version",
                        "score_mae": 2.2,
                        "activated": False,
                        "quality_status": "approved",
                    },
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
                CollectionRun(
                    run_type="manual-serving-refresh",
                    started_at=now - timedelta(hours=4),
                    finished_at=now - timedelta(hours=3),
                    status="success",
                    records_downloaded=500,
                    records_inserted=120,
                    records_skipped=380,
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
    assert payload["data"]["measurement_age_hours_at_publication"] == 2.0
    assert payload["data"]["collection_age_hours_at_publication"] == 3.0
    assert payload["data"]["fresh_threshold_hours"] == 14.0
    assert payload["data"]["stale_threshold_hours"] == 22.0
    assert payload["schedule"] == {
        "serving_refresh_hours": 8,
        "regular_training_hours": 12,
        "heavy_training_hours": 28,
        "deferred_retry_minutes": 30,
        "serving_release_retention": 3,
    }
    assert payload["training"]["state_at_publication"] == "running"
    assert payload["training"]["history"][0]["outcome"] == "no_change"
    assert (
        payload["training"]["history"][0]["outcome_reason"]
        == "candidate_not_better_than_active"
    )
    assert payload["models"][0]["version"] == "safe-version"
    assert payload["models"][0]["freshness_status"] == "fresh"
    assert payload["models"][0]["last_evaluated_at"] is not None
    assert len(payload["collection_history"]["freshness_checks"]) == 2
    assert payload["collection_history"]["runs"][0]["inserted"] == 120
    assert "private-host" not in encoded
    assert "private-token" not in encoded
    assert "12345" not in encoded
    assert "secret" not in encoded


def test_digitalocean_transfer_history_reads_utf16_legacy_reports(app_config) -> None:
    report_dir = (
        app_config.paths.logs_dir.parent
        / "reports"
        / "digitalocean"
        / "20260817-110957"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "03-publication.json").write_text(
        json.dumps(
            {
                "inserted": 482,
                "skipped": 2,
                "details": {
                    "release_id": "safe-release",
                    "manifest_key": "private-layout-detail",
                    "objects_copied": 481,
                    "objects_reused": 2,
                    "bytes_uploaded": 90_620_682,
                    "destination_backend": "s3",
                    "pointer_published_last": True,
                },
            }
        ),
        encoding="utf-16",
    )

    payload = _digitalocean_transfer_history(app_config)

    assert payload["status"] == "measured"
    assert payload["latest"]["objects_uploaded"] == 482
    assert payload["latest"]["objects_reused"] == 2
    assert payload["latest"]["bytes_uploaded"] == 90_620_682
    assert payload["daily"][0]["period"] == "20260817"
    assert "manifest_key" not in str(payload)
    assert "private-layout-detail" not in str(payload)
