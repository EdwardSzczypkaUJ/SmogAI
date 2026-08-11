from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from server.application.query import ForecastQueryService, QueryRequest
from server.application.snapshot_source import StaticSnapshotSource
from smog_ai.nlp.interpreter import RuleBasedIntentInterpreter
from smog_ai.observability.bridge import NoopObservability


def _snapshot() -> dict:
    return {
        "metadata": {
            "publication_id": "pub-katowice",
            "schema_version": "1",
            "generated_at": "2026-07-30T09:00:00Z",
            "checksum": "a" * 64,
        },
        "stations": [
            {
                "station_id": 11,
                "station_name": "Katowice, ul. Kossutha",
                "city_name": "Katowice",
                "latitude": 50.2649,
                "longitude": 19.0238,
                "open_quality_flags": 0,
                "measurements": {
                    "PM10": {"value": 31.0, "measurement_time": "2026-07-30T09:00:00Z"},
                    "PM2.5": {"value": 17.0, "measurement_time": "2026-07-30T09:00:00Z"},
                },
                "weather": {"temperature_c": 23.5, "wind_speed_mps": 2.1},
            },
            {
                "station_id": 12,
                "station_name": "Kraków, al. Krasińskiego",
                "city_name": "Kraków",
                "latitude": 50.0577,
                "longitude": 19.9262,
                "open_quality_flags": 1,
                "measurements": {
                    "PM10": {"value": 28.0, "measurement_time": "2026-07-30T09:00:00Z"},
                    "PM2.5": {"value": 14.0, "measurement_time": "2026-07-30T09:00:00Z"},
                },
                "weather": {"temperature_c": 24.0, "wind_speed_mps": 1.9},
            },
        ],
        "forecasts": [
            {
                "forecast_id": "f-11-pm10",
                "station_id": 11,
                "parameter": "PM10",
                "forecast_created_at": "2026-07-30T09:00:00Z",
                "origin_time": "2026-07-30T09:00:00Z",
                "target_time": "2026-07-31T10:00:00Z",
                "horizon_hours": 24,
                "predicted_value": 42.5,
                "actual_value": None,
                "signed_error": None,
                "absolute_error": None,
                "model_version": "pm10-katowice-v1",
                "verification_status": "pending",
            },
            {
                "forecast_id": "f-11-pm25",
                "station_id": 11,
                "parameter": "PM2.5",
                "forecast_created_at": "2026-07-30T09:00:00Z",
                "origin_time": "2026-07-30T09:00:00Z",
                "target_time": "2026-07-31T10:00:00Z",
                "horizon_hours": 24,
                "predicted_value": 24.2,
                "actual_value": None,
                "signed_error": None,
                "absolute_error": None,
                "model_version": "pm25-katowice-v1",
                "verification_status": "pending",
            },
            {
                "forecast_id": "f-12-pm10",
                "station_id": 12,
                "parameter": "PM10",
                "forecast_created_at": "2026-07-30T09:00:00Z",
                "origin_time": "2026-07-30T09:00:00Z",
                "target_time": "2026-07-31T10:00:00Z",
                "horizon_hours": 24,
                "predicted_value": 35.0,
                "actual_value": None,
                "signed_error": None,
                "absolute_error": None,
                "model_version": "pm10-krakow-v1",
                "verification_status": "pending",
            },
        ],
        "metrics": [],
        "quality_summary": {},
    }


def _service() -> ForecastQueryService:
    return ForecastQueryService(
        snapshot_source=StaticSnapshotSource(_snapshot()),
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )


def test_polish_textbox_query_is_structured_and_selects_katowice_forecast() -> None:
    service = _service()
    response = service.ask(
        QueryRequest(text="Wyjeżdżam jutro do Katowic, jakie będzie tam zanieczyszczenie?"),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )
    assert response.intent.location == "Katowice"
    assert response.station.city_name == "Katowice"
    assert response.intent.pollutants == ["PM10", "PM2.5"]
    assert {item.parameter for item in response.forecasts} == {"PM10", "PM2.5"}
    pm10 = next(item for item in response.forecasts if item.parameter == "PM10")
    assert pm10.predicted_value == 42.5
    assert any(point["selected"] for point in response.map_points)
    assert "Katowice" in response.summary


def test_fastapi_query_endpoint_uses_application_service(monkeypatch) -> None:
    import server.api.main as main

    monkeypatch.setattr(main, "query_service", _service())
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/query",
        json={"text": "Jutro do Katowic. Podaj PM10 i PM2.5."},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["station"]["city_name"] == "Katowice"
    assert payload["interpretation"]["provider"] == "rule_based"
    assert len(payload["map_points"]) >= 2


def test_rule_parser_preserves_explicit_coordinates_without_geocoder_guess() -> None:
    interpreter = RuleBasedIntentInterpreter(timezone="Europe/Warsaw")
    parsed = interpreter.interpret(
        "Jutro o 15:17 dla punktu 50.123456, 16.654321 podaj PM10.",
        candidates=["Katowice", "Wałbrzych"],
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )
    assert parsed.intent.latitude == 50.123456
    assert parsed.intent.longitude == 16.654321
    assert parsed.intent.location_source == "text_coordinates"
    assert parsed.intent.location_precision == "exact_coordinates"
