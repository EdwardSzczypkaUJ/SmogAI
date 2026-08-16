from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.application.query import ForecastQueryService, QueryRequest
from server.application.snapshot_source import StaticSnapshotSource
from server.application.spatial_source import StaticSpatialSource
from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirStation, Forecast, ModelVersion
from smog_ai.nlp.interpreter import RuleBasedIntentInterpreter
from smog_ai.observability.bridge import NoopObservability
from smog_ai.places.gazetteer import PolishGazetteerResolver
from smog_ai.spatial.service import build_spatial_surfaces, validate_latest_spatial_surfaces

ROOT = Path(__file__).resolve().parents[1]


def _snapshot() -> dict:
    return {
        "metadata": {
            "publication_id": "pub-spatial",
            "schema_version": "1.1",
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
                "measurements": {"PM10": {"value": 31.0}, "PM2.5": {"value": 17.0}},
                "weather": {"temperature_c": 23.5, "wind_speed_mps": 2.1},
            },
            {
                "station_id": 12,
                "station_name": "Kraków, al. Krasińskiego",
                "city_name": "Kraków",
                "latitude": 50.0577,
                "longitude": 19.9262,
                "open_quality_flags": 0,
                "measurements": {"PM10": {"value": 28.0}, "PM2.5": {"value": 14.0}},
                "weather": {"temperature_c": 24.0, "wind_speed_mps": 1.9},
            },
        ],
        "forecasts": [],
        "metrics": [],
        "quality_summary": {},
        "spatial": {"available": True},
    }


def _surface(parameter: str, value: float) -> dict:
    return {
        "schema_version": "1.0",
        "surface_id": f"surface-{parameter}",
        "parameter": parameter,
        "horizon_hours": 24,
        "origin_time": "2026-07-30T10:00:00Z",
        "target_time": "2026-07-31T10:00:00Z",
        "generated_at": "2026-07-30T10:01:00Z",
        "model_versions": [f"{parameter}-model-v1"],
        "metadata": {"grid_resolution_km": 8.0},
        "metrics": {"algorithm": "idw", "loo_mae": 3.2},
        "stations": [
            {
                "station_id": 11,
                "station_name": "Katowice, ul. Kossutha",
                "city_name": "Katowice",
                "latitude": 50.2649,
                "longitude": 19.0238,
                "predicted_value": value + 2.7,
            },
            {
                "station_id": 12,
                "station_name": "Kraków, al. Krasińskiego",
                "city_name": "Kraków",
                "latitude": 50.0577,
                "longitude": 19.9262,
                "predicted_value": value - 3.0,
            },
        ],
        "grid": [
            {
                "cell_id": "katowice-cell",
                "row": 1,
                "column": 1,
                "latitude": 50.265,
                "longitude": 19.024,
                "value": value,
                "confidence": 0.86,
                "nearest_station_distance_km": 3.2,
                "stations_used": 7,
                "quality_flag": "ok",
                "parameter": parameter,
                "horizon_hours": 24,
                "origin_time": "2026-07-30T10:00:00Z",
                "target_time": "2026-07-31T10:00:00Z",
                "color_r": 200,
                "color_g": 150,
                "color_b": 50,
                "color_a": 220,
            },
            {
                "cell_id": "krakow-cell",
                "row": 1,
                "column": 2,
                "latitude": 50.061,
                "longitude": 19.938,
                "value": value - 4.0,
                "confidence": 0.79,
                "nearest_station_distance_km": 2.0,
                "stations_used": 6,
                "quality_flag": "ok",
                "parameter": parameter,
                "horizon_hours": 24,
                "origin_time": "2026-07-30T10:00:00Z",
                "target_time": "2026-07-31T10:00:00Z",
                "color_r": 150,
                "color_g": 190,
                "color_b": 60,
                "color_a": 210,
            },
        ],
    }


def test_query_interpolates_exact_point_from_published_station_forecasts() -> None:
    spatial = StaticSpatialSource(surfaces=[_surface("PM10", 39.8), _surface("PM2.5", 21.2)])
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(_snapshot()),
        spatial_source=spatial,
        place_resolver=PolishGazetteerResolver(ROOT / "smog_ai" / "resources" / "polish_places.csv"),
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )
    response = service.ask(
        QueryRequest(text="Wyjeżdżam jutro do Katowic, jakie będzie tam zanieczyszczenie?"),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )
    assert response.place.name == "Katowice"
    assert response.station.city_name == "Katowice"
    pm10 = next(item for item in response.forecasts if item.parameter == "PM10")
    assert pm10.predicted_value == 42.5
    assert pm10.station_predicted_value == 42.5
    assert pm10.prediction_source == "published_station_forecasts_exact_point_idw"
    assert pm10.spatial_method == "quality_weighted_idw"
    assert pm10.distance_power == 2.0
    assert pm10.interpolation_point_distance_km == 0.0
    assert pm10.cell_latitude == response.place.latitude
    assert pm10.cell_longitude == response.place.longitude
    assert pm10.station_contributions[0]["normalized_weight"] == 1.0
    assert response.timeline
    assert any(point["selected"] for point in response.map_points)
    assert "dokładnych współrzędnych" in response.summary.lower()


def test_query_uses_serving_v2_without_legacy_forecast_snapshot() -> None:
    spatial = StaticSpatialSource(
        surfaces=[_surface("PM10", 39.8), _surface("PM2.5", 21.2)]
    )
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(None),
        spatial_source=spatial,
        place_resolver=PolishGazetteerResolver(
            ROOT / "smog_ai" / "resources" / "polish_places.csv"
        ),
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )

    preview = service.preview(
        QueryRequest(text="Jutro sprawdź PM10 w Katowicach."),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )
    response = service.ask(
        QueryRequest(text="Wyjeżdżam jutro do Katowic, sprawdź PM10 i PM2.5."),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )

    assert preview["place"]["name"] == "Katowice"
    assert response.forecasts
    assert response.snapshot_metadata["source"] == "serving_v2_surface_station_catalog"
    assert all(
        item.prediction_source == "published_station_forecasts_exact_point_idw"
        for item in response.forecasts
    )


def test_second_query_resolves_witkow_airfield_instead_of_previous_katowice() -> None:
    spatial = StaticSpatialSource(
        surfaces=[_surface("PM10", 39.8), _surface("PM2.5", 21.2)]
    )
    resolver = PolishGazetteerResolver(
        ROOT / "smog_ai" / "resources" / "polish_places.csv"
    )
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(None),
        spatial_source=spatial,
        place_resolver=resolver,
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )

    first = service.preview(
        QueryRequest(text="Jaka pogoda będzie jutro o 12:00 w Katowicach?"),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )
    second = service.preview(
        QueryRequest(text="Jaka pogoda będzie jutro o 12:00 na lotnisku w Witkowie?"),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )

    assert first["place"]["name"] == "Katowice"
    assert second["place"]["name"] == "Lotnisko Witków EPDS"
    assert second["place"]["latitude"] == pytest.approx(50.79686)
    assert second["place"]["longitude"] == pytest.approx(16.11448)
    assert second["place"]["precision"] == "exact_poi"


def test_rule_based_weather_question_selects_weather_outputs_not_pollution() -> None:
    surface_rows = [
        _surface("temperature_c", 18.0),
        _surface("precipitation_probability", 0.4),
        _surface("precipitation_mm", 1.2),
        _surface("PM10", 39.8),
    ]
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(None),
        spatial_source=StaticSpatialSource(surfaces=surface_rows),
        place_resolver=PolishGazetteerResolver(
            ROOT / "smog_ai" / "resources" / "polish_places.csv"
        ),
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )

    preview = service.preview(
        QueryRequest(text="Jaka pogoda będzie jutro o 12:00 we Wrocławiu?"),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )

    assert preview["place"]["name"] == "Wrocław"
    assert preview["interpretation"]["provider"] == "rule_based"
    assert preview["proposed_parameters"] == [
        "temperature_c",
        "precipitation_probability",
        "precipitation_mm",
    ]


def test_query_coordinates_override_place_and_run_spatial_then_pchip() -> None:
    surfaces: list[dict] = []
    for target_hour, base_value in zip((12, 13, 14, 15), (10.0, 20.0, 30.0, 40.0), strict=True):
        for parameter in ("PM10", "PM2.5"):
            surface = _surface(parameter, base_value)
            surface["surface_id"] = f"{parameter}-{target_hour}"
            surface["target_time"] = f"2026-07-31T{target_hour:02d}:00:00Z"
            for station_row in surface["stations"]:
                station_row["predicted_value"] = base_value + (
                    2.7 if station_row["station_id"] == 11 else -3.0
                )
            surfaces.append(surface)
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(_snapshot()),
        spatial_source=StaticSpatialSource(surfaces=surfaces),
        place_resolver=PolishGazetteerResolver(
            ROOT / "smog_ai" / "resources" / "polish_places.csv"
        ),
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )
    response = service.ask(
        QueryRequest(
            text="Jutro o 15:17 sprawdź PM10 i PM2.5.",
            latitude=50.2649,
            longitude=19.0238,
            place_name="Startowisko Mieroszów",
            location_source="map_point",
        ),
        now=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )
    pm10 = next(item for item in response.forecasts if item.parameter == "PM10")
    assert response.place.name == "Startowisko Mieroszów"
    assert response.place.source == "map_point"
    assert response.place.precision == "map_point"
    assert pm10.temporal_method == "pchip"
    assert len(pm10.temporal_source_times) == 4
    assert pm10.predicted_value == pytest.approx(25.5333333333)
    assert response.time_selection["operation_order"] == "spatial_then_temporal"


def test_local_pipeline_publishes_idempotent_spatial_artifacts(engine, app_config) -> None:
    app_config.training.parameters = ["PM10"]
    app_config.training.horizons_hours = [24]
    app_config.spatial.grid_resolution_km = 40
    app_config.spatial.minimum_stations = 3
    app_config.spatial.nearest_stations = 4
    app_config.spatial.maximum_distance_km = 600
    app_config.data_validation.enabled = False
    origin = datetime(2026, 8, 1, 6, tzinfo=UTC)
    with session_scope(engine) as session:
        model = ModelVersion(
            model_name="spatial-test",
            algorithm="hist_gradient_boosting",
            parameter="PM10",
            forecast_horizon=24,
            semantic_version="spatial-test-v1",
            active=True,
        )
        session.add(model)
        session.flush()
        station_rows = [
            ("A1", "Kraków", 50.0614, 19.9383, 42.0),
            ("A2", "Katowice", 50.2649, 19.0238, 48.0),
            ("A3", "Wrocław", 51.1079, 17.0385, 28.0),
            ("A4", "Warszawa", 52.2297, 21.0122, 34.0),
        ]
        for source_id, city, latitude, longitude, value in station_rows:
            station = AirStation(
                source_id=source_id,
                station_name=f"{city} test",
                city_name=city,
                latitude=latitude,
                longitude=longitude,
            )
            session.add(station)
            session.flush()
            session.add(
                Forecast(
                    model_version_id=model.id,
                    air_station_id=station.id,
                    parameter="PM10",
                    forecast_created_at=origin + timedelta(minutes=2),
                    forecast_origin_time=origin,
                    target_time=origin + timedelta(hours=24),
                    forecast_horizon=24,
                    predicted_value=value,
                )
            )

    with session_scope(engine) as session:
        first = build_spatial_surfaces(session, app_config)
    assert first.inserted == 1
    assert first.errors == 0
    repository = create_artifact_repository(app_config)
    pointer = repository.get_json(repository.layout.latest_spatial_pointer)
    manifest = repository.get_json(pointer["manifest_key"])
    assert manifest["app_platform_processing"] == "read_and_exact_point_interpolate"
    assert len(manifest["surfaces"]) == 1
    payload = repository.get_gzip_json(manifest["surfaces"][0]["object_key"])
    assert payload["grid"]
    assert (
        payload["metadata"]["server_computation"]
        == "exact_point_idw_from_published_station_forecasts"
    )

    with session_scope(engine) as session:
        second = build_spatial_surfaces(session, app_config)
        checked = validate_latest_spatial_surfaces(session, app_config)
    assert second.details["surface_set_id"] == first.details["surface_set_id"]
    assert checked.errors == 0
    assert checked.downloaded == 1
