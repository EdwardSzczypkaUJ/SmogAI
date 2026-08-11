from __future__ import annotations

from typing import Any
from datetime import UTC

from smog_ai.collectors.gios import GiosCollector, collect_gios
from smog_ai.collectors.imgw import ImgwCollector, load_station_metadata
from smog_ai.database.engine import session_scope
from smog_ai.errors import ExternalAPIStatusError


class FakeHttp:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        for suffix, payload in self.responses.items():
            if suffix in url:
                if callable(payload):
                    return payload(params or {})
                return payload
        raise AssertionError(url)

    def close(self) -> None:
        pass


def test_gios_station_parser(app_config) -> None:
    http = FakeHttp({"station/findAll": {"Lista stacji pomiarowych": [{"Identyfikator stacji": 1, "Nazwa stacji": "Kraków", "WGS84 φ N": "50,1", "WGS84 λ E": "19,9"}]}})
    rows = GiosCollector(app_config, http=http).fetch_stations()
    assert rows[0].source_id == "1"
    assert rows[0].latitude == 50.1


def test_gios_sensor_parser(app_config) -> None:
    http = FakeHttp({
        "station/sensors/1": {
            "Lista stanowisk pomiarowych dla podanej stacji": [{
                "Identyfikator stanowiska": 7,
                "Identyfikator stacji": 1,
                "Wskaźnik": "pył zawieszony PM10",
                "Wskaźnik - wzór": "PM10",
                "Wskaźnik - kod": "PM10",
                "Id wskaźnika": 3,
            }]
        }
    })
    rows = GiosCollector(app_config, http=http).fetch_sensors("1")
    assert rows[0].parameter_code == "PM10"
    assert rows[0].parameter_name == "pył zawieszony PM10"
    assert http.calls[0]["headers"]["Accept"] == "application/ld+json"


def test_gios_measurement_parser(app_config) -> None:
    http = FakeHttp({
        "data/getData/7": {
            "Lista danych pomiarowych": [{"Data": "2026-01-01 12:00", "Wartość": 42.5}]
        }
    })
    collector = GiosCollector(app_config, http=http)
    sensor = type("Sensor", (), {"source_id": "7", "station_source_id": "1", "parameter_code": "PM10"})()
    rows = collector.fetch_measurements(sensor)
    assert rows[0].value == 42.5
    assert rows[0].measurement_time.tzinfo is UTC
    assert http.calls[0]["params"] == {"page": 0, "size": 500}


def test_gios_measurements_are_paginated(app_config) -> None:
    app_config.api.gios_page_size = 1

    def page(params: dict[str, Any]) -> dict[str, Any]:
        index = int(params["page"])
        values = [
            {"Data": "2026-01-01 12:00", "Wartość": 10.0},
            {"Data": "2026-01-01 13:00", "Wartość": 11.0},
        ]
        return {
            "Lista danych pomiarowych": [values[index]],
            "totalPages": 2,
        }

    http = FakeHttp({"data/getData/7": page})
    collector = GiosCollector(app_config, http=http)
    sensor = type("Sensor", (), {"source_id": "7", "station_source_id": "1", "parameter_code": "PM10"})()
    rows = collector.fetch_measurements(sensor)
    assert [row.value for row in rows] == [10.0, 11.0]
    assert [call["params"]["page"] for call in http.calls] == [0, 1]


def test_gios_probe_is_read_only_and_uses_json_ld(app_config) -> None:
    http = FakeHttp({
        "station/findAll": {
            "Lista stacji pomiarowych": [{"Identyfikator stacji": 1, "Nazwa stacji": "Kraków"}],
            "totalPages": 300,
        }
    })
    result = GiosCollector(app_config, http=http).probe()
    assert result["status"] == "ok"
    assert result["sample_station_id"] == "1"
    assert http.calls[0]["params"] == {"page": 0, "size": 1}
    assert http.calls[0]["headers"]["Accept"] == "application/ld+json"


def test_gios_current_data_400_is_classified_as_unavailable_not_collection_error(
    engine, app_config
) -> None:
    def unavailable(_: dict[str, Any]) -> dict[str, Any]:
        raise ExternalAPIStatusError(
            "historical sensor has no current-data resource",
            status_code=400,
            url="https://api.gios.gov.pl/pjp-api/v1/rest/data/getData/7",
            content_type="application/ld+json",
            body_excerpt="bad request",
        )

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
                        "Wskaźnik": "pył zawieszony PM10",
                        "Wskaźnik - wzór": "PM10",
                        "Wskaźnik - kod": "PM10",
                    }
                ]
            },
            "data/getData/7": unavailable,
        }
    )

    with session_scope(engine) as session:
        stats = collect_gios(session, app_config, http=http)

    assert stats.errors == 0
    assert stats.warnings == 1
    assert stats.skipped == 1
    assert stats.details["unavailable_measurement_endpoints"] == 1


def test_imgw_parser(app_config) -> None:
    http = FakeHttp({"synop": [{"id_stacji": "12566", "stacja": "Kraków", "data_pomiaru": "2026-07-30", "godzina_pomiaru": "16", "temperatura": "25.1", "wilgotnosc_wzgledna": "51.2", "cisnienie": "1014.2", "suma_opadu": "0", "predkosc_wiatru": "2", "kierunek_wiatru": "180"}]})
    stations, measurements = ImgwCollector(app_config, http=http).fetch()
    assert stations[0].source_id == "12566"
    assert measurements[0].temperature_c == 25.1


def test_imgw_metadata_skips_comments(app_config) -> None:
    app_config.paths.imgw_metadata_csv.write_text("source_id,station_name,latitude,longitude,elevation_m,metadata_source\n# comment\n12566,Kraków,50.08,19.78,237,official\n", encoding="utf-8")
    result = load_station_metadata(app_config.paths.imgw_metadata_csv)
    assert list(result) == ["12566"]
