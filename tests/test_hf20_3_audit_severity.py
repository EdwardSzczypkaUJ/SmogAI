from __future__ import annotations

from datetime import UTC, datetime, timedelta

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirStation, Forecast, ModelVersion
from smog_ai.hourly.audit import audit_latest_hourly_serving_contract


def _add_model(session, *, parameter: str, metrics: dict) -> ModelVersion:  # type: ignore[no-untyped-def]
    model = ModelVersion(
        model_name=f"hourly-{parameter}-test",
        algorithm="ridge" if parameter != "precipitation_mm" else "hurdle",
        parameter=parameter,
        forecast_horizon=0,
        semantic_version=f"{parameter}-v1",
        artifact_path=f"{parameter}.joblib",
        feature_columns_json=[],
        metrics_json=metrics,
        active=True,
    )
    session.add(model)
    session.flush()
    return model


def _add_forecasts(
    session,
    *,
    model: ModelVersion,
    station: AirStation,
    parameter: str,
    values: list[float],
) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 8, 10, 13, 55, tzinfo=UTC)
    origin = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)

    for lead, value in enumerate(values, start=1):
        session.add(
            Forecast(
                model_version_id=model.id,
                air_station_id=station.id,
                parameter=parameter,
                forecast_created_at=created,
                forecast_origin_time=origin,
                target_time=datetime(2026, 8, 10, 14, tzinfo=UTC)
                + timedelta(hours=lead - 1),
                forecast_horizon=lead,
                predicted_value=value,
                features_json={
                    "serving_lead_hours": lead,
                    "model_horizon_hours": 3 + lead,
                    "source_age_hours": 3.9167,
                },
            )
        )


def test_precipitation_gate_failure_is_quality_not_hard(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    app_config.hourly_forecasting.serving_horizon_hours = 2
    app_config.hourly_forecasting.maximum_model_horizon_hours = 16
    app_config.hourly_forecasting.spatial_targets = [
        "PM10",
        "precipitation_mm",
        "precipitation_probability",
    ]

    rain_failures = [
        {
            "metric": "improvement_vs_persistence",
            "actual": -0.03,
            "minimum": 0.01,
        },
        {
            "metric": "roc_auc",
            "actual": 0.57,
            "minimum": 0.60,
        },
    ]

    with session_scope(engine) as session:
        station = AirStation(
            source="GIOS",
            source_id="severity-test",
            station_name="Test",
            city_name="Test",
            latitude=50.0,
            longitude=19.0,
            active=True,
            raw_json={},
        )
        session.add(station)
        session.flush()

        pm10 = _add_model(
            session,
            parameter="PM10",
            metrics={
                "quality_status": "accepted",
                "improvement_vs_persistence": 0.10,
            },
        )
        rain = _add_model(
            session,
            parameter="precipitation_mm",
            metrics={
                "quality_status": "experimental",
                "precipitation_quality_gate": {
                    "passed": False,
                    "status": "experimental",
                    "failures": rain_failures,
                },
            },
        )

        _add_forecasts(
            session,
            model=pm10,
            station=station,
            parameter="PM10",
            values=[10.0, 11.0],
        )
        _add_forecasts(
            session,
            model=rain,
            station=station,
            parameter="precipitation_mm",
            values=[0.2, 0.5],
        )
        _add_forecasts(
            session,
            model=rain,
            station=station,
            parameter="precipitation_probability",
            values=[0.3, 0.6],
        )
        session.flush()

        result = audit_latest_hourly_serving_contract(session, app_config)

    assert result["hard_failures"] == []
    assert len(result["quality_failures"]) == 2
    assert result["serving_contract_passed"] is True
    assert result["publication_ready"] is True
    assert result["partial_success"] is False
    assert result["passed"] is True
    assert result["decision"] == "continue_with_experimental_targets"
    assert result["approved_targets"] == ["PM10"]
    assert result["experimental_targets"] == [
        "precipitation_mm",
        "precipitation_probability",
    ]
    assert result["experimental_model_targets"] == ["precipitation_mm"]

    with session_scope(engine) as session:
        blocked = audit_latest_hourly_serving_contract(
            session,
            app_config,
            allow_experimental_targets="none",
        )
    assert blocked["passed"] is False
    assert blocked["decision"] == "continue_without_experimental_targets"
    assert blocked["approved_targets"] == ["PM10"]

    with session_scope(engine) as session:
        forced = audit_latest_hourly_serving_contract(
            session,
            app_config,
            allow_experimental_targets="precipitation_mm,precipitation_probability",
        )
    assert forced["passed"] is True
    assert forced["publication_ready"] is True
    assert forced["partial_success"] is False
    assert forced["decision"] == "continue_with_experimental_targets"
    assert forced["blocked_experimental_targets"] == []
    assert forced["forced_experimental_targets"] == [
        "precipitation_mm",
        "precipitation_probability",
    ]


def test_technical_failure_still_blocks_every_target(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    app_config.hourly_forecasting.serving_horizon_hours = 2
    app_config.hourly_forecasting.maximum_model_horizon_hours = 16
    app_config.hourly_forecasting.spatial_targets = ["PM10"]

    with session_scope(engine) as session:
        station = AirStation(
            source="GIOS",
            source_id="hard-test",
            station_name="Test",
            city_name="Test",
            latitude=50.0,
            longitude=19.0,
            active=True,
            raw_json={},
        )
        session.add(station)
        session.flush()

        pm10 = _add_model(
            session,
            parameter="PM10",
            metrics={"quality_status": "accepted"},
        )
        _add_forecasts(
            session,
            model=pm10,
            station=station,
            parameter="PM10",
            values=[10.0],
        )
        session.flush()

        result = audit_latest_hourly_serving_contract(session, app_config)

    assert result["hard_failures"]
    assert result["quality_failures"] == []
    assert result["serving_contract_passed"] is False
    assert result["publication_ready"] is False
    assert result["approved_targets"] == []
    assert result["decision"] == "stop_hard_failures"


def test_flat_persistence_curve_is_warning_not_hard_failure(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    app_config.hourly_forecasting.serving_horizon_hours = 2
    app_config.hourly_forecasting.maximum_model_horizon_hours = 16
    app_config.hourly_forecasting.spatial_targets = ["PM2.5"]

    with session_scope(engine) as session:
        station = AirStation(
            source="GIOS",
            source_id="flat-persistence-test",
            station_name="Test",
            city_name="Test",
            latitude=50.0,
            longitude=19.0,
            active=True,
            raw_json={},
        )
        session.add(station)
        session.flush()
        model = _add_model(
            session,
            parameter="PM2.5",
            metrics={"quality_status": "accepted"},
        )
        model.algorithm = "persistence"
        _add_forecasts(
            session,
            model=model,
            station=station,
            parameter="PM2.5",
            values=[7.5, 7.5],
        )
        session.flush()
        result = audit_latest_hourly_serving_contract(session, app_config)

    assert result["hard_failures"] == []
    assert result["passed"] is True
    assert result["approved_targets"] == ["PM2.5"]
    assert any(
        row.get("reason") == "all_station_curves_flat"
        and row.get("severity") == "quality_warning"
        for row in result["warnings"]
    )


def test_no_forecasts_has_complete_decision_schema(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    with session_scope(engine) as session:
        result = audit_latest_hourly_serving_contract(session, app_config)

    assert result["passed"] is False
    assert result["serving_contract_passed"] is False
    assert result["publication_ready"] is False
    assert result["hard_failures"] == [{"reason": "no_forecasts"}]
    assert result["quality_failures"] == []
    assert result["decision"] == "stop_hard_failures"
