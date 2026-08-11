from __future__ import annotations

import json

from smog_ai.cli import _render_cli_value
from smog_ai.database.engine import session_scope
from smog_ai.parameter_catalog import build_weather_parameter_catalog
from tests.conftest import seed_basic


def test_cli_json_falls_back_to_ascii_for_cp1250() -> None:
    rendered = _render_cli_value(
        {"unit": "mg/m³", "temperature": "°C"},
        encoding="cp1250",
    )

    assert "\\u00b3" in rendered
    assert json.loads(rendered) == {
        "unit": "mg/m³",
        "temperature": "°C",
    }


def test_weather_catalog_contains_temperature_and_precipitation(
    engine, app_config
) -> None:  # type: ignore[no-untyped-def]
    seed_basic(engine, hours=8)

    with session_scope(engine) as session:
        catalog = build_weather_parameter_catalog(
            session,
            app_config,
            active_models={},
        )

    temperature = catalog["temperature_c"]
    precipitation = catalog["precipitation_mm"]
    humidity = catalog["humidity_percent"]

    assert temperature["canonical_unit"] == "°C"
    assert temperature["forecast_target"] is True
    assert temperature["spatial_surface"] is True
    assert temperature["measurements"]["rows"] == 9

    assert precipitation["canonical_unit"] == "mm"
    assert precipitation["forecast_target"] is True
    assert precipitation["cadence_hours"] == 6

    assert humidity["forecast_target"] is False
    assert humidity["auxiliary_feature"] is True
