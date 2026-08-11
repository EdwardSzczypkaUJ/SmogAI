from __future__ import annotations

from smog_ai.cli import _parameter_catalog_payload
from smog_ai.parameter_catalog import EMPTY_MEASUREMENT_STATS


EXPECTED_STAT_KEYS = {
    "rows",
    "start",
    "end",
    "stations",
    "unique_hours",
}


def test_parameter_catalog_exposes_total_measurement_schema_without_data(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    payload = _parameter_catalog_payload(app_config, engine)

    assert "NO2" in payload["parameters"]
    assert "temperature_c" in payload["weather_parameters"]
    assert "precipitation_probability" in payload["weather_parameters"]

    for group_name in ("parameters", "weather_parameters"):
        for code, definition in payload[group_name].items():
            measurements = definition["measurements"]
            assert EXPECTED_STAT_KEYS <= set(measurements), (group_name, code)
            assert isinstance(measurements["rows"], int)
            assert isinstance(measurements["stations"], int)
            assert isinstance(measurements["unique_hours"], int)

    assert payload["parameters"]["NO2"]["measurements"] == EMPTY_MEASUREMENT_STATS
    assert (
        payload["weather_parameters"]["precipitation_probability"]["measurements"]
        == EMPTY_MEASUREMENT_STATS
    )


def test_zero_measurement_statistics_are_not_shared_mutable_instances(
    engine,
    app_config,
) -> None:  # type: ignore[no-untyped-def]
    payload = _parameter_catalog_payload(app_config, engine)
    no2 = payload["parameters"]["NO2"]["measurements"]
    probability = payload["weather_parameters"]["precipitation_probability"][
        "measurements"
    ]

    assert no2 is not probability
    no2["rows"] = 123
    assert probability["rows"] == 0
