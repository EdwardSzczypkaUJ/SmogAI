from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, ModelVersion
from smog_ai.database.repository import insert_air_measurements
from smog_ai.domain import AirMeasurementRecord
from smog_ai.hourly.trainer import train_hourly_models
from smog_ai.training_snapshot import (
    create_snapshot_engine,
    create_training_snapshot_bridge,
)
from tests.conftest import seed_basic


def _configure_quick_pm10(app_config) -> None:  # type: ignore[no-untyped-def]
    settings = app_config.hourly_forecasting
    settings.enabled = True
    settings.minimum_horizon_hours = 1
    settings.maximum_horizon_hours = 4
    settings.step_hours = 1
    settings.targets = ["PM10"]
    settings.spatial_targets = ["PM10"]
    settings.minimum_training_rows = 20
    settings.minimum_unique_origin_times = 20
    settings.use_predicted_weather_for_pm = False
    settings.training_policy.quick.maximum_rows_per_target = 500
    settings.training_policy.quick.validation_max_rows = 100
    settings.training_policy.quick.maximum_training_days_by_target = {
        "PM10": 30,
    }
    settings.training_policy.quick.horizon_bucket_edges = [2, 4]
    settings.training_policy.quick.samples_per_horizon_bucket = 1
    settings.training_policy.quick.cross_fit_folds = 2
    settings.training_policy.quick.algorithms = {
        "PM10": ["persistence", "ridge"],
    }
    settings.training_policy.quick.fit_quantiles = False
    settings.training_policy.quick.max_wall_time_seconds = 300
    app_config.training.input_source = "database"
    app_config.artifacts.upload_models = False
    app_config.object_storage.enabled = False
    app_config.training_snapshot.mirror_manifest_to_object_storage = False


def test_snapshot_is_consistent_while_live_database_changes(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    ids = seed_basic(engine, hours=48)
    bridge = create_training_snapshot_bridge(app_config)
    snapshot = bridge.create(
        profile="quick",
        targets=["PM10"],
        mirror_manifest=False,
    )

    snapshot_engine = create_snapshot_engine(snapshot.database_path)
    try:
        with session_scope(snapshot_engine) as session:
            snapshot_before = int(
                session.scalar(select(func.count(AirMeasurement.id))) or 0
            )

        timestamp = datetime.now(UTC).replace(
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(hours=2)
        with session_scope(engine) as session:
            inserted, duplicates = insert_air_measurements(
                session,
                [
                    AirMeasurementRecord(
                        station_source_id="A1",
                        sensor_source_id="S1",
                        parameter="PM10",
                        measurement_time=timestamp,
                        value=77.0,
                    )
                ],
            )
            assert inserted == 1
            assert duplicates == 0

        with session_scope(engine) as session:
            live_after = int(
                session.scalar(select(func.count(AirMeasurement.id))) or 0
            )
        with session_scope(snapshot_engine) as session:
            snapshot_after = int(
                session.scalar(select(func.count(AirMeasurement.id))) or 0
            )

        assert live_after == snapshot_before + 1
        assert snapshot_after == snapshot_before
        validation = bridge.validate(snapshot, verify_checksum=True)
        assert validation["valid"] is True
        assert validation["integrity_check"] == "ok"
    finally:
        snapshot_engine.dispose()


def test_training_reads_snapshot_and_registers_model_in_live_database(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    seed_basic(engine, hours=96)
    _configure_quick_pm10(app_config)

    bridge = create_training_snapshot_bridge(app_config)
    snapshot = bridge.create(
        profile="quick",
        targets=["PM10"],
        mirror_manifest=False,
    )
    snapshot_engine = create_snapshot_engine(snapshot.database_path)

    try:
        with session_scope(engine) as live_session:
            with session_scope(snapshot_engine) as training_session:
                stats = train_hourly_models(
                    live_session,
                    app_config,
                    profile_name="quick",
                    training_session=training_session,
                    dataset_provenance=snapshot.as_dict(),
                    commit_live_metadata=True,
                )
        assert stats.errors == 0
        assert stats.details["training_snapshot"]["dataset_id"] == snapshot.dataset_id

        with session_scope(engine) as session:
            model = session.scalar(
                select(ModelVersion).where(
                    ModelVersion.parameter == "PM10",
                    ModelVersion.forecast_horizon == 0,
                    ModelVersion.active.is_(True),
                )
            )
            assert model is not None
            provenance = (model.metrics_json or {})["data_provenance"]
            assert provenance["dataset_id"] == snapshot.dataset_id
            assert (
                provenance["training_snapshot"]["database_sha256"]
                == snapshot.database_sha256
            )
            assert provenance["training_snapshot"]["immutable"] is True

        with session_scope(snapshot_engine) as session:
            snapshot_models = int(
                session.scalar(select(func.count(ModelVersion.id))) or 0
            )
            assert snapshot_models == 0
    finally:
        snapshot_engine.dispose()


def test_latest_pointer_and_status_are_versioned(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    seed_basic(engine, hours=24)
    bridge = create_training_snapshot_bridge(app_config)
    first = bridge.create(
        profile="quick",
        targets=["PM10"],
        mirror_manifest=False,
    )
    second = bridge.create(
        profile="quick",
        targets=["PM10"],
        mirror_manifest=False,
    )

    latest = bridge.latest("quick")
    listed = bridge.list(profile="quick")

    assert latest.dataset_id == second.dataset_id
    assert {row.dataset_id for row in listed} == {
        first.dataset_id,
        second.dataset_id,
    }
    assert latest.database_sha256
    assert latest.manifest_path.exists()
