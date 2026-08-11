from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from smog_ai.air_parameters import create_air_parameter_registry
from smog_ai.collectors.gios import collect_gios
from smog_ai.database.engine import session_scope
from smog_ai.database.models import AirMeasurement, Forecast, ModelVersion
from smog_ai.database.repository import insert_air_measurements, upsert_air_sensor
from smog_ai.domain import AirMeasurementRecord, AirSensorRecord
from smog_ai.hourly.features import auxiliary_air_feature_columns
from smog_ai.hourly.predictor import create_hourly_forecasts
from smog_ai.hourly.trainer import HOURLY_MODEL_HORIZON_SENTINEL, train_hourly_models
from smog_ai.nlp.interpreter import RuleBasedIntentInterpreter
from smog_ai.publishing.snapshot import _station_rows
from tests.conftest import seed_basic
from tests.test_collectors import FakeHttp


def _enable_no2(app_config) -> None:  # type: ignore[no-untyped-def]
    definition = app_config.air_parameters.parameters["NO2"]
    definition.collect_current = True
    definition.historical_backfill = True
    definition.forecast_target = True
    definition.auxiliary_feature = True
    definition.spatial_surface = True
    definition.valid_max = 1000.0
    definition.exceedance_threshold = 200.0
    definition.spike_absolute = 250.0


def test_registry_separates_collection_training_and_spatial_roles(app_config) -> None:  # type: ignore[no-untyped-def]
    _enable_no2(app_config)
    registry = create_air_parameter_registry(app_config)

    assert registry.resolve("dwutlenek azotu") == "NO2"
    assert registry.resolve("NO₂") == "NO2"
    assert "NO2" in registry.collection_codes
    assert "NO2" in registry.historical_codes
    assert "NO2" in registry.forecast_codes
    assert "NO2" in registry.auxiliary_codes
    assert "NO2" in registry.spatial_codes
    assert registry.require("NO2").canonical_unit == "µg/m³"


def test_current_collector_can_download_only_selected_no2(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    _enable_no2(app_config)
    http = FakeHttp(
        {
            "station/findAll": {
                "Lista stacji pomiarowych": [
                    {
                        "Identyfikator stacji": 1,
                        "Nazwa stacji": "Kraków",
                        "WGS84 φ N": "50,1",
                        "WGS84 λ E": "19,9",
                    }
                ]
            },
            "station/sensors/1": {
                "Lista stanowisk pomiarowych dla podanej stacji": [
                    {
                        "Identyfikator stanowiska": 7,
                        "Identyfikator stacji": 1,
                        "Wskaźnik - kod": "PM10",
                    },
                    {
                        "Identyfikator stanowiska": 8,
                        "Identyfikator stacji": 1,
                        "Wskaźnik - kod": "NO2",
                    },
                ]
            },
            "data/getData/8": {
                "Lista danych pomiarowych": [
                    {"Data": "2026-01-01 12:00", "Wartość": 44.5}
                ]
            },
        }
    )

    with session_scope(engine) as session:
        stats = collect_gios(
            session,
            app_config,
            http=http,
            parameters=("NO2",),
        )
        assert stats.errors == 0
        rows = session.scalars(select(AirMeasurement)).all()

    assert [row.parameter for row in rows] == ["NO2"]
    assert rows[0].unit == "µg/m³"
    assert not any("data/getData/7" in call["url"] for call in http.calls)


def test_rule_parser_uses_parameters_published_by_manifest() -> None:
    interpreter = RuleBasedIntentInterpreter(timezone="Europe/Warsaw")

    explicit = interpreter.interpret(
        "Jutro o 12:00 podaj dwutlenek azotu w Katowicach",
        candidates=["Katowice"],
        available_parameters=["PM10", "NO2", "O3"],
    )
    generic = interpreter.interpret(
        "Jutro o 12:00 jaka będzie jakość powietrza w Katowicach",
        candidates=["Katowice"],
        available_parameters=["PM10", "NO2", "O3"],
    )

    assert explicit.intent.pollutants == ["NO2"]
    assert generic.intent.pollutants == ["PM10", "NO2", "O3"]


def test_generic_no2_hourly_training_prediction_and_snapshot(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    identifiers = seed_basic(engine, hours=110)
    _enable_no2(app_config)

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=110
    )
    with session_scope(engine) as session:
        sensor = upsert_air_sensor(
            session,
            AirSensorRecord(
                source_id="S-NO2",
                station_source_id="A1",
                parameter_code="NO2",
                parameter_name="Dwutlenek azotu",
            ),
        )
        session.flush()
        records = [
            AirMeasurementRecord(
                station_source_id="A1",
                sensor_source_id="S-NO2",
                parameter="NO2",
                measurement_time=start + timedelta(hours=index),
                value=30.0 + 0.18 * index + (index % 8),
                unit="µg/m³",
            )
            for index in range(111)
        ]
        inserted, duplicates = insert_air_measurements(session, records)
        assert inserted == len(records)
        assert duplicates == 0
        assert sensor.id > 0

    settings = app_config.hourly_forecasting
    settings.enabled = True
    settings.minimum_horizon_hours = 1
    settings.maximum_horizon_hours = 3
    settings.step_hours = 1
    settings.targets = ["NO2"]
    settings.spatial_targets = ["NO2"]
    settings.target_algorithms["NO2"] = ["persistence", "ridge"]
    settings.minimum_training_rows = 20
    settings.minimum_unique_origin_times = 20
    settings.validation_fraction = 0.2
    settings.quantiles = [0.5]
    settings.use_predicted_weather_for_pm = False
    app_config.training.input_source = "database"
    app_config.training.allow_database_fallback = True
    app_config.artifacts.upload_models = False

    with session_scope(engine) as session:
        trained = train_hourly_models(session, app_config)
        assert trained.errors == 0
        model = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == "NO2",
                ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
                ModelVersion.active.is_(True),
            )
        )
        assert model is not None
        assert set(auxiliary_air_feature_columns(["PM10"])) <= set(
            model.feature_columns_json
        )

    with session_scope(engine) as session:
        predicted = create_hourly_forecasts(session, app_config)
        assert predicted.errors == 0
        assert predicted.inserted > 0
        forecasts = session.scalars(
            select(Forecast).where(Forecast.parameter == "NO2")
        ).all()
        assert forecasts
        assert {row.forecast_horizon for row in forecasts} == {1, 2, 3}
        station_rows = _station_rows(session, app_config)

    station = next(
        row for row in station_rows if row["station_id"] == identifiers["air_station_id"]
    )
    assert station["measurements"]["NO2"] is not None
    assert station["measurements"]["NO2"]["unit"] == "µg/m³"


def test_custom_parameter_definition_can_be_collected_trained_and_parsed(
    engine, app_config
) -> None:  # type: ignore[no-untyped-def]
    from smog_ai.config import AirParameterConfig

    app_config.air_parameters.parameters["NH3"] = AirParameterConfig(
        display_name="Amoniak",
        aliases=["NH3", "AMONIAK"],
        canonical_unit="µg/m³",
        collect_current=True,
        historical_backfill=True,
        forecast_target=True,
        auxiliary_feature=False,
        spatial_surface=True,
        valid_min=0.0,
        valid_max=5000.0,
        annual_api_indicator="NH3",
        prepared_archive_tokens=["NH3"],
        algorithms=["persistence", "ridge"],
    )
    registry = create_air_parameter_registry(app_config)
    assert registry.resolve("amoniak") == "NH3"
    assert registry.public_catalog(["NH3"])["NH3"]["display_name"] == "Amoniak"

    interpreter = RuleBasedIntentInterpreter(timezone="Europe/Warsaw")
    parsed = interpreter.interpret(
        "Jutro o 12:00 podaj amoniak w Katowicach",
        candidates=["Katowice"],
        available_parameters=["NH3"],
        parameter_aliases={"NH3": ["NH3", "amoniak"]},
    )
    assert parsed.intent.pollutants == ["NH3"]

    seed_basic(engine, hours=96)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=96
    )
    with session_scope(engine) as session:
        upsert_air_sensor(
            session,
            AirSensorRecord(
                source_id="S-NH3",
                station_source_id="A1",
                parameter_code="NH3",
                parameter_name="Amoniak",
            ),
        )
        records = [
            AirMeasurementRecord(
                station_source_id="A1",
                sensor_source_id="S-NH3",
                parameter="NH3",
                measurement_time=start + timedelta(hours=index),
                value=8.0 + 0.05 * index + (index % 5) * 0.2,
                unit="µg/m³",
            )
            for index in range(97)
        ]
        inserted, duplicates = insert_air_measurements(session, records)
        assert inserted == len(records)
        assert duplicates == 0

    settings = app_config.hourly_forecasting
    settings.enabled = True
    settings.minimum_horizon_hours = 1
    settings.maximum_horizon_hours = 2
    settings.targets = ["NH3"]
    settings.spatial_targets = ["NH3"]
    settings.target_algorithms["NH3"] = ["persistence", "ridge"]
    settings.minimum_training_rows = 20
    settings.minimum_unique_origin_times = 20
    settings.quantiles = [0.5]
    settings.use_predicted_weather_for_pm = False
    app_config.training.input_source = "database"
    app_config.training.allow_database_fallback = True
    app_config.artifacts.upload_models = False

    with session_scope(engine) as session:
        stats = train_hourly_models(session, app_config)
        assert stats.errors == 0
        model = session.scalar(
            select(ModelVersion).where(
                ModelVersion.parameter == "NH3",
                ModelVersion.forecast_horizon == HOURLY_MODEL_HORIZON_SENTINEL,
                ModelVersion.active.is_(True),
            )
        )
        assert model is not None


def test_config_example_with_generic_registry_loads() -> None:
    from pathlib import Path

    from smog_ai.config import load_config

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config.example.yaml", root / ".env.example")
    registry = create_air_parameter_registry(config)

    assert registry.forecast_codes == ("PM10", "PM2.5")
    assert registry.contains("NO2")
    assert registry.require("CO").canonical_unit == "mg/m³"
