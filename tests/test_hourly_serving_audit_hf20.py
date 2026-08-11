from __future__ import annotations

from datetime import UTC, datetime, timedelta

from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirStation, Forecast, ModelVersion
from smog_ai.hourly.audit import audit_latest_hourly_serving_contract


def test_serving_audit_accepts_four_future_leads(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    app_config.hourly_forecasting.serving_horizon_hours = 4
    app_config.hourly_forecasting.maximum_model_horizon_hours = 16
    app_config.hourly_forecasting.spatial_targets = ["PM10"]
    created = datetime(2026, 8, 9, 13, 55, tzinfo=UTC)
    origin = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    with session_scope(engine) as session:
        station = AirStation(
            source="GIOS",
            source_id="test",
            station_name="Test",
            city_name="Test",
            latitude=50.0,
            longitude=19.0,
            active=True,
            raw_json={},
        )
        session.add(station)
        session.flush()
        model = ModelVersion(
            model_name="hourly-PM10-ridge",
            algorithm="ridge",
            parameter="PM10",
            forecast_horizon=0,
            semantic_version="v1",
            artifact_path="model.joblib",
            feature_columns_json=[],
            metrics_json={
                "improvement_vs_persistence": 0.1,
                "quality_status": "accepted",
            },
            active=True,
        )
        session.add(model)
        session.flush()
        for lead in range(1, 5):
            session.add(
                Forecast(
                    model_version_id=model.id,
                    air_station_id=station.id,
                    parameter="PM10",
                    forecast_created_at=created,
                    forecast_origin_time=origin,
                    target_time=datetime(2026, 8, 9, 14, tzinfo=UTC)
                    + timedelta(hours=lead - 1),
                    forecast_horizon=lead,
                    predicted_value=10.0 + lead,
                    features_json={
                        "serving_lead_hours": lead,
                        "model_horizon_hours": 4 + lead,
                        "source_age_hours": 4.9167,
                    },
                )
            )
        session.flush()
        result = audit_latest_hourly_serving_contract(session, app_config)
    assert result["passed"] is True
    assert result["parameters"]["PM10"]["serving_leads"] == [1, 2, 3, 4]
