from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from server.application.query import (
    ForecastQueryService,
    QueryRequest,
    TimelineRequest,
)
from server.application.snapshot_source import StaticSnapshotSource
from server.application.spatial_source import StaticSpatialSource
from server.database.store import ObjectStoreSnapshotStore
from smog_ai.nlp.interpreter import RuleBasedIntentInterpreter
from smog_ai.observability.bridge import NoopObservability
from smog_ai.places.gazetteer import PolishGazetteerResolver

ROOT = Path(__file__).resolve().parents[1]


def _snapshot() -> dict[str, Any]:
    return {
        "metadata": {
            "publication_id": "pub-fast-query",
            "schema_version": "1.1",
            "generated_at": "2026-08-03T08:00:00Z",
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
                "measurements": {"PM10": {"value": 31.0}},
                "weather": {"temperature_c": 22.0},
            }
        ],
        "forecasts": [],
        "metrics": [],
        "quality_summary": {},
    }


def _surface(parameter: str, target: datetime, value: float) -> dict[str, Any]:
    origin = target - timedelta(hours=6)
    return {
        "schema_version": "1.1",
        "surface_id": f"{parameter}-{target:%Y%m%d%H}",
        "parameter": parameter,
        "horizon_hours": 6,
        "origin_time": origin.isoformat().replace("+00:00", "Z"),
        "target_time": target.isoformat().replace("+00:00", "Z"),
        "generated_at": origin.isoformat().replace("+00:00", "Z"),
        "model_versions": [f"{parameter}-v1"],
        "metadata": {"grid_resolution_km": 8.0},
        "stations": [
            {
                "station_id": 11,
                "station_name": "Katowice, ul. Kossutha",
                "city_name": "Katowice",
                "latitude": 50.2649,
                "longitude": 19.0238,
                "predicted_value": value,
            }
        ],
        "grid": [
            {
                "cell_id": f"cell-{parameter}-{target:%H}",
                "latitude": 50.265,
                "longitude": 19.024,
                "value": value,
                "confidence": 0.8,
                "nearest_station_distance_km": 2.0,
                "stations_used": 4,
                "quality_flag": "ok",
            }
        ],
    }


def _service() -> ForecastQueryService:
    target = datetime(2026, 8, 4, 10, tzinfo=UTC)
    surfaces: list[dict[str, Any]] = []
    for hour in range(24):
        current = datetime(2026, 8, 4, hour, tzinfo=UTC)
        surfaces.extend(
            [
                _surface("PM10", current, 30.0 + hour),
                _surface("PM2.5", current, 15.0 + hour / 2),
                _surface("temperature_c", current, 12.0 + hour / 3),
                _surface("precipitation_mm", current, hour / 20),
            ]
        )
    # One entry from another date must not leak into a daily profile.
    surfaces.append(_surface("PM10", target + timedelta(days=1), 999.0))
    manifest = {
        "schema_version": "1.1",
        "surface_set_id": "fast-query-test",
        "generated_at": "2026-08-03T08:00:00Z",
        "exact_target_time_available": True,
        "surfaces": [
            {
                "surface_id": row["surface_id"],
                "parameter": row["parameter"],
                "horizon_hours": row["horizon_hours"],
                "origin_time": row["origin_time"],
                "target_time": row["target_time"],
                "object_key": f"static:{index}",
            }
            for index, row in enumerate(surfaces)
        ],
    }
    return ForecastQueryService(
        snapshot_source=StaticSnapshotSource(_snapshot()),
        spatial_source=StaticSpatialSource(manifest=manifest, surfaces=surfaces),
        place_resolver=PolishGazetteerResolver(
            ROOT / "smog_ai" / "resources" / "polish_places.csv"
        ),
        interpreter=RuleBasedIntentInterpreter(timezone="Europe/Warsaw"),
        observability=NoopObservability(),
    )


def test_primary_query_can_defer_heavy_timeline() -> None:
    service = _service()
    response = service.ask(
        QueryRequest(text="Jutro o 12:00 jadę do Katowic. Podaj PM10 i PM2.5."),
        now=datetime(2026, 8, 3, 8, tzinfo=UTC),
        include_timeline=False,
    )
    assert response.forecasts
    assert response.timeline == []
    assert response.time_selection["timeline_deferred"] is True
    assert response.performance["timeline_deferred"] is True
    assert response.performance["total_ms"] >= 0


def test_timeline_endpoint_profile_is_filtered_to_requested_day(monkeypatch) -> None:
    import server.api.main as main

    monkeypatch.setattr(main, "query_service", _service())
    client = TestClient(main.app)

    query = client.post(
        "/api/v1/query",
        json={"text": "Jutro o 12:00 jadę do Katowic. Podaj PM10 i PM2.5."},
    )
    assert query.status_code == 200
    query_payload = query.json()
    assert query_payload["timeline"] == []

    timeline = client.post(
        "/api/v1/timeline",
        json={
            "latitude": 50.2649,
            "longitude": 19.0238,
            "target_time": "2026-08-04T12:00:00+00:00",
            "parameters": ["PM10", "PM2.5", "temperature_c", "precipitation_mm"],
            "daily_profile": True,
            "place_name": "Katowice",
        },
    )
    assert timeline.status_code == 200
    payload = timeline.json()
    assert payload["rows"]
    assert payload["errors"] == []
    assert payload["surfaces_loaded"] == 24 * 4
    assert all(row["target_time"].startswith("2026-08-04") for row in payload["rows"])
    assert all(row["value"] != 999.0 for row in payload["rows"])


class _SnapshotRepositoryStub:
    def __init__(self) -> None:
        self.calls = 0

    def latest_snapshot_payload(self) -> dict[str, Any]:
        self.calls += 1
        return {"metadata": {"publication_id": "cached"}}


def test_object_store_snapshot_payload_is_cached() -> None:
    repository = _SnapshotRepositoryStub()
    store = ObjectStoreSnapshotStore(repository, cache_ttl_seconds=60)  # type: ignore[arg-type]
    first = store.latest_payload()
    second = store.latest_payload()
    assert first == second
    assert repository.calls == 1


def test_dashboard_uses_readable_labels_and_lazy_timeline() -> None:
    source = (ROOT / "server" / "dashboard" / "app.py").read_text(encoding="utf-8")
    # HF21 labels are billboarded screen overlays without rectangular boxes.
    assert 'background=True' not in source
    assert 'parameters={"depthTest": False}' in source
    assert "SMOG_AI_DASHBOARD_QUERY_TIMEOUT_SECONDS" in source
    assert "SMOG_AI_DASHBOARD_TIMELINE_TIMEOUT_SECONDS" in source
    assert 'def load_timeline(' in source
    assert '"timeline"' in source
    assert "station_elevation * height_scale" in source
