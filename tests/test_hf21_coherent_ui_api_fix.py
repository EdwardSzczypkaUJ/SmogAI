from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

from server.application.query import ForecastQueryService, QueryRequest
from server.application.snapshot_source import StaticSnapshotSource
from smog_ai.nlp.models import AirQualityIntent, InterpretationResult
from smog_ai.observability.bridge import NoopObservability


TARGET = datetime.fromisoformat("2026-08-12T11:27:00+02:00")


class BombInterpreter:
    provider_name = "must-not-run"

    def interpret(self, *args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("Confirmed structured request called the LLM interpreter")


class PreviewInterpreter:
    provider_name = "preview-test"

    def interpret(self, *args, **kwargs):
        return InterpretationResult(
            intent=AirQualityIntent(
                location="Witków",
                target_time=TARGET,
                pollutants=["PM10", "PM2.5", "temperature_c"],
                language="pl",
                requested_view="forecast",
                time_was_explicit=True,
                time_precision="exact_minute",
                confidence=0.9,
            ),
            provider="preview-test",
            model="test",
        )


def _snapshot() -> dict:
    target_utc = "2026-08-12T09:27:00Z"
    parameters = {
        "PM10": 21.49,
        "PM2.5": 9.40,
        "temperature_c": 26.86,
        "precipitation_probability": 0.32,
        "precipitation_mm": 0.41,
    }
    return {
        "metadata": {"publication_id": "hf21-test"},
        "stations": [
            {
                "station_id": 1,
                "station_name": "Witków test",
                "city_name": "Witków",
                "latitude": 50.796794,
                "longitude": 16.114526,
                "measurements": {},
                "weather": {},
            }
        ],
        "forecasts": [
            {
                "forecast_id": f"test-{parameter}",
                "station_id": 1,
                "parameter": parameter,
                "forecast_created_at": "2026-08-11T09:00:00Z",
                "origin_time": "2026-08-11T09:00:00Z",
                "target_time": target_utc,
                "horizon_hours": 24,
                "predicted_value": value,
                "model_version": f"test-{parameter}",
            }
            for parameter, value in parameters.items()
        ],
        "metrics": [],
        "quality_summary": {},
    }


def test_confirmed_request_skips_llm_and_returns_five_parameters() -> None:
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(_snapshot()),
        interpreter=BombInterpreter(),
        observability=NoopObservability(),
    )
    response = service.ask(
        QueryRequest(
            text="Zatwierdzony punkt Witków",
            latitude=50.796794,
            longitude=16.114526,
            place_name="Lądowisko Witków",
            location_source="confirmed_independent_reference",
            target_time=TARGET,
            time_source="confirmed_deterministic_parser",
            parameters=[
                "PM10",
                "PM2.5",
                "temperature_c",
                "precipitation_probability",
                "precipitation_mm",
            ],
            parser_provider="openai_compatible",
            parser_model="gpt-4.1-mini",
            parser_prompt_tokens=321,
            parser_completion_tokens=45,
        ),
        include_timeline=False,
    )
    assert response.interpretation.provider == "openai_compatible"
    assert response.interpretation.model == "gpt-4.1-mini"
    assert response.interpretation.prompt_tokens == 321
    assert response.interpretation.completion_tokens == 45
    assert response.interpretation.raw_response == {
        "usage_source": "carried_from_query_preview",
        "confirmed_structured_request": True,
    }
    assert [row.parameter for row in response.forecasts] == [
        "PM10",
        "PM2.5",
        "temperature_c",
        "precipitation_probability",
        "precipitation_mm",
    ]
    assert response.performance["timeline_deferred"] is True


def test_preview_proposes_editable_contract_without_forecast_calculation() -> None:
    service = ForecastQueryService(
        snapshot_source=StaticSnapshotSource(_snapshot()),
        interpreter=PreviewInterpreter(),
        observability=NoopObservability(),
    )
    preview = service.preview(
        QueryRequest(
            text="Jutro o 11:27 PM10, PM2.5 i temperatura na lotnisku Witków",
            latitude=50.796794,
            longitude=16.114526,
            place_name="Lotnisko Witków",
            location_source="exact_coordinates",
        )
    )
    assert preview["place"]["name"] == "Lotnisko Witków"
    assert preview["place"]["latitude"] == 50.796794
    assert preview["intent"]["target_time"] == TARGET.isoformat()
    assert preview["proposed_parameters"] == ["PM10", "PM2.5", "temperature_c"]
    assert preview["performance"]["forecast_computation_deferred"] is True
    assert "forecasts" not in preview


def test_dashboard_has_lazy_timeline_and_polish_glyph_contract() -> None:
    test_dir = Path(__file__).resolve().parent
    root = test_dir.parent if test_dir.name == "tests" else test_dir
    source = (root / "server" / "dashboard" / "app.py").read_text(
        encoding="utf-8-sig"
    )
    assert "if load_timeline_requested:" in source
    assert "character_set=POLISH_MAP_CHARACTER_SET" in source
    assert "POLISH_MAP_CHARACTER_SET = \"'\" + (" in source
    assert "disk_resolution=24 if view_3d else 32" in source
    assert "_load_poland_dem()" in source
    assert "_terrain_elevation_m(" in source
    assert "extruded=view_3d" in source
    assert "elevation_scale=height_scale" in source
    assert 'row["terrain_elevation_m"] = city_elevation' in source
    assert "station_elevation * height_scale" in source
    assert '"label_z": grid_elevation * height_scale' in source
    assert '"label_z": place_elevation * height_scale' in source
    assert "maximum_column_height" not in source
    assert source.count('get_position="[longitude, latitude]"') >= 4
    assert 'parameters={"depthTest": False}' in source
    assert 'item["tooltip_title"] = f"Stacja — {station_name}"' in source
    assert '"tooltip_title": "Punkt siatki interpolacyjnej"' in source
    assert '"<b>{tooltip_title}</b><br/>"' in source
    assert "Sprawdź i doprecyzuj punkt na mapie" in source
    assert '"confirmed_map_click"' in source
    assert '"Skala przewyższenia terenu 3D"' in source
    assert "max_value=100.0" in source
    assert '"query/preview"' in source
    assert 'key="hf21_confirmation_parameters"' in source
    assert 'st.session_state["question"] = DEFAULT_QUESTION' in source
    assert 'key="question"' in source
    assert 'value=st.session_state.get("question", DEFAULT_QUESTION)' not in source
    assert "dark-matter-gl-style/style.json" in source
    assert "dark-matter-nolabels-gl-style" not in source
    assert "QUERY_PARAMETER_OPTIONS = _manifest_parameter_options(manifest)" in source
    assert "sorted(published_set - set(ordered), key=str.casefold)" in source
    assert "def _manifest_parameter_quality(" in source
    assert "def _parameter_option_label(" in source
    assert 'f"🧪 {label} — EKSPERYMENTALNY"' in source
    assert 'format_func=lambda value: _parameter_option_label(value, manifest)' in source
    assert "QUERY_PARAMETER_OPTIONS = _manifest_parameter_options(manifest)" in source
    assert "sorted(published_set - set(ordered), key=str.casefold)" in source
    assert '"💰 Koszty i wykorzystanie"' in source
    assert '"🤖 OpenAI", "🔭 Langfuse", "☁️ DigitalOcean"' in source
    assert 'kpi[1].metric("Kandydaci MLflow"' in source
    assert "Historia tokenów i kosztu OpenAI" in source
    assert "Metryki użycia DigitalOcean nie są jeszcze połączone" in source
    assert "opacity=0.78 if view_3d else 0.52" in source
    main_source = (root / "server" / "api" / "main.py").read_text(
        encoding="utf-8-sig"
    )
    assert '@app.post("/api/v1/query/preview")' in main_source
    assert "query_service.preview(payload)" in main_source


def test_bundled_dem_has_real_poland_elevations() -> None:
    test_dir = Path(__file__).resolve().parent
    root = test_dir.parent if test_dir.name == "tests" else test_dir
    dem_path = root / "server" / "dashboard" / "resources" / "poland_dem_grid.json.gz"
    with gzip.open(dem_path, "rt", encoding="utf-8") as stream:
        dem = json.load(stream)
    values = dem["elevation_m"]
    assert dem["source"] == "Mapzen Terrarium elevation tiles"
    assert len(values) == int(dem["rows"]) * int(dem["columns"])
    assert min(values) <= 0
    assert max(values) >= 2000
