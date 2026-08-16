from __future__ import annotations

import difflib
import math
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.application.snapshot_source import SnapshotSource
from server.application.spatial_source import SpatialSource
from smog_ai.air_parameters import WEATHER_DERIVED_TARGETS, WEATHER_TARGETS, canonical_code
from smog_ai.hourly.temporal import interpolate_temporally
from smog_ai.nlp.interpreter import IntentInterpreter
from smog_ai.nlp.models import AirQualityIntent, InterpretationResult
from smog_ai.observability.bridge import ObservabilityBridge
from smog_ai.observability.own_store import prepare_interaction_payload
from smog_ai.places.base import PlaceResolver, ResolvedPlace
from smog_ai.processing.matching import haversine_km
from smog_ai.spatial.colors import category_for, unit_for
from smog_ai.spatial.interpolation import IDWInterpolator

# HF21_COHERENT_UI_API_FIX_V1


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character) and character.isalnum()
    )


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=2000)
    session_id: str | None = Field(default=None, max_length=200)
    user_id: str | None = Field(default=None, max_length=200)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    place_name: str | None = Field(default=None, max_length=255)
    location_source: str | None = Field(default=None, max_length=64)
    target_time: datetime | None = None
    time_source: str | None = Field(default=None, max_length=64)
    parameters: list[str] | None = Field(default=None, min_length=1, max_length=16)
    parser_provider: str | None = Field(default=None, max_length=64)
    parser_model: str | None = Field(default=None, max_length=120)
    parser_prompt_tokens: int | None = Field(default=None, ge=0)
    parser_completion_tokens: int | None = Field(default=None, ge=0)
    requested_view: Literal[
        "forecast", "current", "comparison", "daily_profile"
    ] = "forecast"

    @model_validator(mode="after")
    def validate_coordinate_pair(self) -> QueryRequest:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if self.target_time is not None and (
            self.target_time.tzinfo is None or self.target_time.utcoffset() is None
        ):
            raise ValueError("target_time must include an explicit UTC offset")
        return self

    @property
    def is_confirmed_structured_request(self) -> bool:
        return bool(
            self.latitude is not None
            and self.longitude is not None
            and self.target_time is not None
            and self.parameters
        )


class TimelineRequest(BaseModel):
    """Request a compact hourly profile for one already-resolved point.

    Timeline loading is deliberately separated from the primary natural-language
    query.  A first request therefore needs only the exact surfaces selected for
    the requested hour, while the heavier daily profile can be fetched lazily.
    """

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    target_time: datetime
    parameters: list[str] = Field(
        default_factory=lambda: [
            "PM10",
            "PM2.5",
            "temperature_c",
            "precipitation_probability",
            "precipitation_mm",
        ],
        min_length=1,
        max_length=16,
    )
    daily_profile: bool = True
    place_name: str | None = Field(default=None, max_length=255)


class TimelineResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    place_name: str | None = None
    latitude: float
    longitude: float
    requested_target_time: datetime
    requested_date: str
    parameters: list[str]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    entries_considered: int = 0
    surfaces_loaded: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = 0.0


class PlaceView(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    latitude: float
    longitude: float
    match_score: float
    source: str
    matched_text: str | None = None
    precision: str = "unresolved"
    ambiguous: bool = False


class StationMatchView(BaseModel):
    model_config = ConfigDict(extra="allow")

    station_id: int
    station_name: str
    city_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    match_score: float
    distance_km: float | None = None


class ForecastSelection(BaseModel):
    model_config = ConfigDict(extra="allow")

    parameter: str
    predicted_value: float | None = None
    actual_value: float | None = None
    target_time: datetime | None = None
    forecast_origin_time: datetime | None = None
    requested_target_time: datetime | None = None
    horizon_hours: int | None = None
    serving_lead_hours: int | None = None
    model_horizon_hours: int | None = None
    source_age_hours: float | None = None
    serving_anchor_time: datetime | None = None
    exact_time_match: bool = False
    temporal_method: str = "unavailable"
    signed_error: float | None = None
    absolute_error: float | None = None
    model_version: str | None = None
    time_distance_minutes: float | None = None
    prediction_source: str = "station_forecast"
    confidence: float | None = None
    nearest_station_distance_km: float | None = None
    stations_used: int | None = None
    quality_flag: str | None = None
    surface_id: str | None = None
    cell_id: str | None = None
    cell_latitude: float | None = None
    cell_longitude: float | None = None
    interpolation_point_distance_km: float | None = None
    unit: str | None = None
    category: str | None = None
    station_predicted_value: float | None = None
    spatial_method: str | None = None
    distance_power: float | None = None
    projected_crs: str | None = None
    station_contributions: list[dict[str, Any]] = Field(default_factory=list)
    temporal_source_times: list[datetime] = Field(default_factory=list)
    temporal_components: list[dict[str, Any]] = Field(default_factory=list)
    quality_status: str = "accepted"
    experimental: bool = False
    experimental_reason: list[dict[str, Any]] = Field(default_factory=list)


class QueryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str
    trace_id: str
    question: str
    intent: AirQualityIntent
    interpretation: InterpretationResult
    place: PlaceView
    station: StationMatchView
    forecasts: list[ForecastSelection]
    current_measurements: dict[str, Any]
    weather: dict[str, Any] | None = None
    summary: str
    warnings: list[str] = Field(default_factory=list)
    map_points: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    surface_options: list[dict[str, Any]] = Field(default_factory=list)
    selected_surface: dict[str, Any] = Field(default_factory=dict)
    time_selection: dict[str, Any] = Field(default_factory=dict)
    snapshot_metadata: dict[str, Any] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    location_validation: dict[str, Any] = Field(default_factory=dict)
    time_validation: dict[str, Any] = Field(default_factory=dict)


class ForecastQueryService:
    """Application core independent of FastAPI, Streamlit and storage vendor.

    Crucially, this service does not import or invoke any trained estimator. It
    selects values from surfaces and station forecasts that were computed by the
    local pipeline and published to the object store before the request.
    """

    def __init__(
        self,
        *,
        snapshot_source: SnapshotSource,
        interpreter: IntentInterpreter,
        observability: ObservabilityBridge,
        spatial_source: SpatialSource | None = None,
        place_resolver: PlaceResolver | None = None,
        flush_observability: bool = False,
        prompt_template_version: str = "air-query-v1",
    ) -> None:
        self.snapshot_source = snapshot_source
        self.spatial_source = spatial_source
        self.place_resolver = place_resolver
        self.interpreter = interpreter
        self.observability = observability
        self.flush_observability = flush_observability
        self.prompt_template_version = prompt_template_version

    @staticmethod
    def _station_candidates(stations: list[dict[str, Any]]) -> list[str]:
        result: list[str] = []
        for station in stations:
            for value in (station.get("city_name"), station.get("station_name")):
                if value and value not in result:
                    result.append(str(value))
        return result

    def _candidates(self, stations: list[dict[str, Any]]) -> list[str]:
        values = list(self.place_resolver.candidates) if self.place_resolver else []
        for value in self._station_candidates(stations):
            if value not in values:
                values.append(value)
        return values

    def _latest_query_context(self) -> dict[str, Any] | None:
        """Load legacy context or derive its small public subset from Serving v2.

        Serving v2 intentionally does not publish the historical forecast snapshot.
        Exact-point queries still need a station catalogue to select the nearest
        reference station.  Every immutable surface already contains that public
        catalogue, so use one representative surface instead of requiring the
        legacy, much larger operational export.
        """

        snapshot = self.snapshot_source.latest()
        if snapshot is not None:
            return snapshot
        if self.spatial_source is None:
            return None
        manifest = self.spatial_source.latest_manifest() or {}
        entries = sorted(
            (dict(row) for row in manifest.get("surfaces") or []),
            key=lambda row: int(row.get("station_count") or 0),
            reverse=True,
        )
        representative: dict[str, Any] | None = None
        for entry in entries:
            surface = self.spatial_source.surface_from_entry(entry)
            if surface and surface.get("stations"):
                representative = surface
                break
        if representative is None:
            return None
        stations: list[dict[str, Any]] = []
        for raw in representative.get("stations") or []:
            if (
                raw.get("station_id") is None
                or raw.get("latitude") is None
                or raw.get("longitude") is None
            ):
                continue
            station = dict(raw)
            station.setdefault("measurements", {})
            station.setdefault("weather", None)
            station.setdefault("open_quality_flags", 0)
            stations.append(station)
        if not stations:
            return None
        release_id = manifest.get("release_id") or manifest.get("surface_set_id")
        return {
            "metadata": {
                "publication_id": release_id,
                "schema_version": manifest.get("schema_version"),
                "generated_at": manifest.get("generated_at"),
                "source": "serving_v2_surface_station_catalog",
            },
            "stations": stations,
            "forecasts": [],
            "metrics": [],
            "quality_summary": {},
            "air_parameter_catalog": manifest.get("air_parameter_catalog") or {},
            "spatial": {"available": True, "release_id": release_id},
        }

    @staticmethod
    def _normalise_parameter_contract(values: list[str] | tuple[str, ...]) -> list[str]:
        aliases = {
            "TEMPERATURE_C": "temperature_c",
            "PRECIPITATION_MM": "precipitation_mm",
            "PRECIPITATION_PROBABILITY": "precipitation_probability",
            "PM25": "PM2.5",
        }
        allowed = {
            "PM10",
            "PM2.5",
            "temperature_c",
            "precipitation_probability",
            "precipitation_mm",
        }
        result: list[str] = []
        identities: set[str] = set()
        for raw in values:
            raw_text = str(raw).strip()
            parameter = aliases.get(raw_text.upper(), raw_text)
            identity = parameter.casefold()
            if parameter in allowed and identity not in identities:
                identities.add(identity)
                result.append(parameter)
        return result

    @classmethod
    def _proposed_parameters(
        cls,
        request: QueryRequest,
        intent: AirQualityIntent,
    ) -> list[str]:
        if request.parameters:
            return cls._normalise_parameter_contract(request.parameters)
        proposed = cls._normalise_parameter_contract(intent.pollutants)
        text = request.text.casefold()
        if any(word in text for word in ("temperatur", "ciepł", "ciepl", "zimn", "pogod")):
            if "temperature_c" not in proposed:
                proposed.append("temperature_c")
        if any(word in text for word in ("opad", "deszcz", "śnieg", "snieg", "pogod")):
            for parameter in ("precipitation_probability", "precipitation_mm"):
                if parameter not in proposed:
                    proposed.append(parameter)
        return proposed or ["PM10", "PM2.5"]

    @staticmethod
    def _available_air_parameters(
        manifest: dict[str, Any] | None,
        stations: list[dict[str, Any]],
    ) -> list[str]:
        values: list[str] = []
        for raw in (manifest or {}).get("parameters") or []:
            code = canonical_code(str(raw))
            if (
                code not in WEATHER_TARGETS
                and code not in WEATHER_DERIVED_TARGETS
                and code != "UNKNOWN"
                and code not in values
            ):
                values.append(code)
        if not values:
            for station in stations:
                for raw in station.get("measurements") or {}:
                    code = canonical_code(str(raw))
                    if code != "UNKNOWN" and code not in values:
                        values.append(code)
        return values or ["PM10", "PM2.5"]

    @staticmethod
    def _available_air_parameter_aliases(
        manifest: dict[str, Any] | None,
        snapshot: dict[str, Any],
        parameters: list[str],
    ) -> dict[str, list[str]]:
        catalog: dict[str, Any] = {}
        for source in (
            snapshot.get("air_parameter_catalog") or {},
            (manifest or {}).get("air_parameter_catalog") or {},
        ):
            if isinstance(source, dict):
                catalog.update(source)

        output: dict[str, list[str]] = {}
        for code in parameters:
            row = catalog.get(code) or {}
            aliases = row.get("aliases") if isinstance(row, dict) else []
            values: list[str] = []
            for raw in (code, row.get("display_name") if isinstance(row, dict) else None, *(aliases or [])):
                cleaned = str(raw).strip() if raw is not None else ""
                if cleaned and cleaned not in values:
                    values.append(cleaned)
            output[code] = values or [code]
        return output

    @staticmethod
    def _match_station_by_name(location: str, stations: list[dict[str, Any]]) -> StationMatchView:
        target = _normalize(location)
        best: tuple[float, dict[str, Any]] | None = None
        for station in stations:
            names = [str(station.get("city_name") or ""), str(station.get("station_name") or "")]
            score = 0.0
            for name in names:
                normalized = _normalize(name)
                if not normalized:
                    continue
                if target == normalized:
                    candidate_score = 1.0
                elif target in normalized or normalized in target:
                    candidate_score = 0.94
                else:
                    candidate_score = difflib.SequenceMatcher(None, target, normalized).ratio()
                score = max(score, candidate_score)
            if best is None or score > best[0]:
                best = (score, station)
        if best is None or best[0] < 0.35:
            raise ValueError(f"Nie znaleziono stacji odpowiadającej lokalizacji: {location}")
        row = best[1]
        return StationMatchView(
            station_id=int(row["station_id"]),
            station_name=str(row.get("station_name") or row.get("city_name") or location),
            city_name=row.get("city_name"),
            latitude=row.get("latitude"),
            longitude=row.get("longitude"),
            match_score=round(best[0], 4),
        )

    @staticmethod
    def _resolve_from_station(location: str, station: StationMatchView) -> ResolvedPlace:
        if station.latitude is None or station.longitude is None:
            raise ValueError(f"Stacja dla {location} nie ma współrzędnych")
        return ResolvedPlace(
            name=station.city_name or location,
            latitude=float(station.latitude),
            longitude=float(station.longitude),
            match_score=station.match_score,
            source="station_name_fallback",
            matched_text=station.station_name,
            precision="station_coordinate",
            ambiguous=False,
        )

    def _resolve_place(self, location: str, stations: list[dict[str, Any]]) -> ResolvedPlace:
        if self.place_resolver is not None:
            try:
                return self.place_resolver.resolve(location)
            except ValueError:
                pass
        station = self._match_station_by_name(location, stations)
        return self._resolve_from_station(location, station)

    def _resolve_request_place(
        self,
        request: QueryRequest,
        intent: AirQualityIntent,
        stations: list[dict[str, Any]],
    ) -> tuple[ResolvedPlace, dict[str, Any]]:
        if request.latitude is not None and request.longitude is not None:
            source = request.location_source or "request_coordinates"
            precision = "map_point" if source == "map_point" else "exact_coordinates"
            resolved = ResolvedPlace(
                name=request.place_name or intent.location or "wybrany punkt",
                latitude=float(request.latitude),
                longitude=float(request.longitude),
                match_score=1.0,
                source=source,
                matched_text=request.place_name or intent.location,
                precision=precision,
                ambiguous=False,
            )
            return resolved, {
                "status": "accepted",
                "confirmation_required": False,
                "reason": "explicit_user_coordinates",
                "candidate": {"latitude": resolved.latitude, "longitude": resolved.longitude},
            }
        if intent.latitude is not None and intent.longitude is not None:
            resolved = ResolvedPlace(
                name=intent.location,
                latitude=float(intent.latitude),
                longitude=float(intent.longitude),
                match_score=1.0,
                source=intent.location_source,
                matched_text=intent.location,
                precision=intent.location_precision,
                ambiguous=False,
            )
            return resolved, {
                "status": "accepted",
                "confirmation_required": False,
                "reason": "coordinates_from_query_text",
                "candidate": {"latitude": resolved.latitude, "longitude": resolved.longitude},
            }

        candidate: ResolvedPlace | None = None
        if intent.candidate_latitude is not None and intent.candidate_longitude is not None:
            candidate = ResolvedPlace(
                name=intent.location,
                latitude=float(intent.candidate_latitude),
                longitude=float(intent.candidate_longitude),
                match_score=float(intent.candidate_coordinate_confidence or 0.0),
                source="openai_coordinate_candidate",
                matched_text=intent.location_raw or intent.location,
                precision=intent.candidate_coordinate_precision or "approximate",
                ambiguous=True,
            )

        reference: ResolvedPlace | None = None
        contextual_resolver = getattr(self.place_resolver, "resolve_intent", None)
        if callable(contextual_resolver):
            try:
                reference = contextual_resolver(
                    primary_name=intent.location,
                    raw_text=intent.location_raw,
                    context_name=intent.location_context,
                )
            except ValueError:
                reference = None
        elif self.place_resolver is not None:
            try:
                reference = self.place_resolver.resolve(intent.location)
            except ValueError:
                reference = None

        if candidate is None:
            if reference is not None:
                return reference, {
                    "status": "accepted",
                    "confirmation_required": False,
                    "reason": "verified_resolver_coordinates",
                    "reference": {
                        "latitude": reference.latitude,
                        "longitude": reference.longitude,
                        "source": reference.source,
                    },
                }
            resolved = self._resolve_place(intent.location, stations)
            return resolved, {
                "status": "accepted",
                "confirmation_required": False,
                "reason": "station_coordinate_fallback",
            }

        in_poland = 49.0 <= candidate.latitude <= 54.84 and 14.12 <= candidate.longitude <= 24.15
        distance_km = None
        if reference is not None:
            distance_km = haversine_km(
                candidate.latitude,
                candidate.longitude,
                reference.latitude,
                reference.longitude,
            )
        confidence = float(intent.candidate_coordinate_confidence or 0.0)
        automatically_accepted = bool(
            in_poland
            and reference is not None
            and distance_km is not None
            and distance_km <= 1.0
        )
        validation = {
            "status": "accepted" if automatically_accepted else "confirmation_required",
            "confirmation_required": not automatically_accepted,
            "reason": (
                "candidate_matches_independent_reference"
                if automatically_accepted
                else "candidate_requires_user_confirmation"
            ),
            "candidate": {
                "name": candidate.name,
                "latitude": candidate.latitude,
                "longitude": candidate.longitude,
                "precision": candidate.precision,
                "confidence": confidence,
                "basis": intent.candidate_coordinate_basis,
                "inside_poland": in_poland,
            },
            "reference": (
                {
                    "name": reference.name,
                    "latitude": reference.latitude,
                    "longitude": reference.longitude,
                    "source": reference.source,
                }
                if reference is not None
                else None
            ),
            "distance_to_reference_km": round(distance_km, 3) if distance_km is not None else None,
            "automatic_acceptance_threshold_km": 1.0,
            "confidence_required_when_reference_matches": False,
        }
        if not in_poland and reference is not None:
            # Never interpolate a clearly invalid candidate. Present the verified
            # reference as the proposed point and still require confirmation.
            validation["candidate_rejected"] = True
            validation["candidate"] = {
                **validation["candidate"],
                "rejected_latitude": candidate.latitude,
                "rejected_longitude": candidate.longitude,
                "latitude": reference.latitude,
                "longitude": reference.longitude,
            }
            candidate = ResolvedPlace(
                name=reference.name,
                latitude=reference.latitude,
                longitude=reference.longitude,
                match_score=reference.match_score,
                source="verified_reference_pending_confirmation",
                matched_text=reference.matched_text,
                precision=reference.precision,
                ambiguous=True,
            )
        elif not in_poland:
            raise ValueError(
                "Proponowane współrzędne leżą poza Polską i nie znaleziono niezależnego punktu odniesienia"
            )
        return candidate, validation

    @staticmethod
    def _validate_request_time(
        request: QueryRequest,
        intent: AirQualityIntent,
        manifest: dict[str, Any] | None,
    ) -> tuple[AirQualityIntent, dict[str, Any]]:
        candidate = intent.target_time
        reference = getattr(intent, "reference_target_time", None)
        reason = "openai_time_candidate"
        confirmation_required = False
        difference_minutes: float | None = None

        if request.target_time is not None:
            candidate = request.target_time
            intent = intent.model_copy(
                update={
                    "target_time": candidate,
                    "time_was_explicit": True,
                    "time_precision": "exact_minute",
                }
            )
            reason = request.time_source or "explicit_user_confirmation"
        elif reference is not None:
            difference_minutes = abs(
                (candidate.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()
            ) / 60.0
            confirmation_required = difference_minutes > 1.0
            reason = (
                "candidate_matches_deterministic_parser"
                if not confirmation_required
                else "candidate_differs_from_deterministic_parser"
            )
        elif intent.time_was_explicit:
            confirmation_required = True
            reason = "explicit_time_without_independent_parse"
        else:
            reason = "non_exact_daily_profile"

        published_times: list[datetime] = []
        for entry in (manifest or {}).get("surfaces") or []:
            if entry.get("target_time"):
                try:
                    published_times.append(_aware(entry["target_time"]))
                except (TypeError, ValueError):
                    continue
        available_start = min(published_times) if published_times else None
        available_end = max(published_times) if published_times else None
        candidate_utc = candidate.astimezone(UTC)
        within_published_window = (
            None
            if available_start is None or available_end is None
            else available_start <= candidate_utc <= available_end
        )
        if within_published_window is False:
            confirmation_required = True
            reason = "candidate_outside_published_forecast_window"

        return intent, {
            "status": "confirmation_required" if confirmation_required else "accepted",
            "confirmation_required": confirmation_required,
            "reason": reason,
            "candidate_target_time": candidate.isoformat(),
            "candidate_timezone_offset": candidate.strftime("%z"),
            "reference_target_time": reference.isoformat() if reference is not None else None,
            "reference_precision": getattr(intent, "reference_time_precision", None),
            "difference_minutes": (
                round(difference_minutes, 3) if difference_minutes is not None else None
            ),
            "automatic_acceptance_threshold_minutes": 1.0,
            "within_published_forecast_window": within_published_window,
            "published_start": available_start.isoformat() if available_start else None,
            "published_end": available_end.isoformat() if available_end else None,
        }

    @staticmethod
    def _nearest_station(place: ResolvedPlace, stations: list[dict[str, Any]]) -> StationMatchView:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for row in stations:
            if row.get("latitude") is None or row.get("longitude") is None:
                continue
            distance = haversine_km(
                place.latitude,
                place.longitude,
                float(row["latitude"]),
                float(row["longitude"]),
            )
            candidates.append((distance, row))
        if not candidates:
            raise ValueError("Brak stacji ze współrzędnymi w opublikowanym snapshotcie")
        distance, row = min(candidates, key=lambda item: item[0])
        return StationMatchView(
            station_id=int(row["station_id"]),
            station_name=str(row.get("station_name") or row.get("city_name") or place.name),
            city_name=row.get("city_name"),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            match_score=round(1.0 / (1.0 + distance / 25.0), 4),
            distance_km=round(distance, 3),
        )

    @staticmethod
    def _select_station_forecast(
        rows: list[dict[str, Any]],
        *,
        station_id: int,
        parameter: str,
        target_time: datetime,
        exact_target_time: bool = False,
    ) -> ForecastSelection | None:
        candidates = [
            row
            for row in rows
            if int(row.get("station_id", -1)) == station_id
            and str(row.get("parameter")) == parameter
            and row.get("target_time")
        ]
        if not candidates:
            return None
        target_utc = target_time.astimezone(UTC)
        if exact_target_time:
            candidates = [
                row
                for row in candidates
                if abs((_aware(row["target_time"]) - target_utc).total_seconds()) < 1.0
            ]
            if not candidates:
                return None
        selected = min(candidates, key=lambda row: abs((_aware(row["target_time"]) - target_utc).total_seconds()))
        selected_time = _aware(selected["target_time"])
        value = selected.get("predicted_value")
        return ForecastSelection(
            parameter=parameter,
            predicted_value=value,
            actual_value=selected.get("actual_value"),
            target_time=selected_time,
            forecast_origin_time=(
                _aware(selected["origin_time"]) if selected.get("origin_time") else None
            ),
            requested_target_time=target_utc,
            horizon_hours=selected.get("horizon_hours"),
            serving_lead_hours=selected.get(
                "serving_lead_hours", selected.get("horizon_hours")
            ),
            model_horizon_hours=selected.get(
                "model_horizon_hours", selected.get("horizon_hours")
            ),
            source_age_hours=selected.get("source_age_hours"),
            serving_anchor_time=(
                _aware(selected["serving_anchor_time"])
                if selected.get("serving_anchor_time")
                else None
            ),
            exact_time_match=abs((selected_time - target_utc).total_seconds()) < 1.0,
            temporal_method=(
                "direct_hourly_model"
                if abs((selected_time - target_utc).total_seconds()) < 1.0
                else "nearest_legacy_forecast"
            ),
            signed_error=selected.get("signed_error"),
            absolute_error=selected.get("absolute_error"),
            model_version=selected.get("model_version"),
            quality_status=str(selected.get("quality_status") or "accepted"),
            experimental=bool(selected.get("experimental", False)),
            experimental_reason=list(selected.get("experimental_reason") or []),
            time_distance_minutes=round(abs((selected_time - target_utc).total_seconds()) / 60, 1),
            prediction_source="station_forecast",
            unit=unit_for(parameter),
            category=category_for(parameter, value),
        )

    @staticmethod
    def _nearest_grid_cell(surface: dict[str, Any], place: ResolvedPlace) -> dict[str, Any] | None:
        cells = surface.get("grid") or []
        if not cells:
            return None
        latitude_scale = 111.0
        longitude_scale = 111.0 * math.cos(math.radians(place.latitude))

        def distance_squared(row: dict[str, Any]) -> float:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            return ((lat - place.latitude) * latitude_scale) ** 2 + ((lon - place.longitude) * longitude_scale) ** 2

        return min(cells, key=distance_squared)

    @staticmethod
    def _station_value_from_surface(
        surface: dict[str, Any], station_id: int
    ) -> float | None:
        for row in surface.get("stations") or []:
            if int(row.get("station_id", -1)) == station_id:
                value = row.get("predicted_value")
                return float(value) if value is not None else None
        return None

    @staticmethod
    def _point_from_surface(
        surface: dict[str, Any], place: ResolvedPlace
    ) -> dict[str, Any]:
        metadata = surface.get("metadata") or {}
        stations = pd.DataFrame(surface.get("stations") or [])
        if stations.empty:
            raise ValueError("Published surface does not contain station forecasts")
        nearest = min(
            max(1, int(metadata.get("nearest_stations", 8))), len(stations)
        )
        interpolator = IDWInterpolator(
            power=float(metadata.get("idw_power", 2.0)),
            distance_smoothing_m=float(
                metadata.get("idw_distance_smoothing_m", 100.0)
            ),
            exact_station_threshold_m=float(
                metadata.get("exact_station_threshold_m", 10.0)
            ),
            nearest_stations=nearest,
            minimum_stations=int(metadata.get("minimum_stations", min(3, nearest))),
            maximum_distance_km=float(metadata.get("maximum_distance_km", 220.0)),
            confidence_distance_km=float(
                metadata.get("confidence_distance_km", 85.0)
            ),
            minimum_confidence=float(metadata.get("confidence_minimum", 0.08)),
        )
        return interpolator.interpolate_point(
            latitude=place.latitude,
            longitude=place.longitude,
            stations=stations,
            parameter=str(surface.get("parameter") or "PM10"),
            projected_crs=str(metadata.get("projected_crs") or "EPSG:2180"),
        )

    @staticmethod
    def _surface_entries_for_point_time(
        manifest: dict[str, Any], *, parameter: str, target_time: datetime
    ) -> list[dict[str, Any]]:
        target = target_time.astimezone(UTC)
        candidates = [
            dict(entry)
            for entry in manifest.get("surfaces", [])
            if str(entry.get("parameter", "")).upper()
            == parameter.upper().replace("PM25", "PM2.5")
            and entry.get("target_time")
        ]
        candidates.sort(key=lambda entry: _aware(entry["target_time"]))
        exact = [
            entry
            for entry in candidates
            if abs((_aware(entry["target_time"]) - target).total_seconds()) < 1.0
        ]
        if exact:
            return [exact[0]]
        before = [entry for entry in candidates if _aware(entry["target_time"]) < target]
        after = [entry for entry in candidates if _aware(entry["target_time"]) > target]
        if not before or not after:
            return []
        selected = before[-2:] + after[:2]
        deduplicated: dict[str, dict[str, Any]] = {}
        for entry in selected:
            identity = str(
                entry.get("object_key")
                or entry.get("surface_id")
                or entry.get("target_time")
            )
            deduplicated[identity] = entry
        return sorted(
            deduplicated.values(), key=lambda entry: _aware(entry["target_time"])
        )

    def _select_spatial_forecast(
        self,
        *,
        parameter: str,
        target_time: datetime,
        place: ResolvedPlace,
        station: StationMatchView,
    ) -> tuple[ForecastSelection | None, dict[str, Any] | None]:
        if self.spatial_source is None:
            return None, None
        manifest = self.spatial_source.latest_manifest() or {}
        entries = self._surface_entries_for_point_time(
            manifest, parameter=parameter, target_time=target_time
        )
        if not entries:
            return None, None
        evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entry in entries:
            surface = self._surface_from_manifest_entry(entry)
            if surface is None:
                continue
            evaluated.append((surface, self._point_from_surface(surface, place)))
        if not evaluated:
            return None, None

        target_utc = target_time.astimezone(UTC)
        samples = [
            (_aware(surface["target_time"]), float(point["value"]))
            for surface, point in evaluated
        ]
        temporal = interpolate_temporally(
            samples,
            requested_time=target_utc,
            method="pchip",
            allow_extrapolation=False,
        )
        selected_surface, selected_point = min(
            evaluated,
            key=lambda item: abs(
                (_aware(item[0]["target_time"]) - target_utc).total_seconds()
            ),
        )
        value = float(temporal.value)
        if parameter in {"PM10", "PM2.5", "precipitation_mm", "precipitation_probability"}:
            value = max(0.0, value)
        if parameter == "precipitation_probability":
            value = min(1.0, value)
        origin_time = (
            _aware(selected_surface["origin_time"])
            if selected_surface.get("origin_time")
            else None
        )
        station_value = self._station_value_from_surface(
            selected_surface, station.station_id
        )
        temporal_components = [
            {
                "target_time": _aware(surface["target_time"]).isoformat(),
                "value_at_exact_point": float(point["value"]),
                "surface_id": surface.get("surface_id"),
                "spatial_method": point.get("method"),
                "station_contributions": point.get("contributions") or [],
            }
            for surface, point in evaluated
        ]
        confidence = min(float(point["confidence"]) for _, point in evaluated)
        source_times = [_aware(surface["target_time"]) for surface, _ in evaluated]
        return (
            ForecastSelection(
                parameter=parameter,
                predicted_value=value,
                target_time=target_utc,
                forecast_origin_time=origin_time,
                requested_target_time=target_utc,
                horizon_hours=int(selected_surface.get("horizon_hours", 0)) or None,
                serving_lead_hours=int(
                    selected_surface.get(
                        "serving_lead_hours",
                        selected_surface.get("horizon_hours", 0),
                    )
                ) or None,
                model_horizon_hours=int(
                    selected_surface.get(
                        "model_horizon_hours",
                        selected_surface.get("horizon_hours", 0),
                    )
                ) or None,
                source_age_hours=(
                    float(selected_surface["source_age_hours"])
                    if selected_surface.get("source_age_hours") is not None
                    else None
                ),
                serving_anchor_time=(
                    _aware(selected_surface["serving_anchor_time"])
                    if selected_surface.get("serving_anchor_time")
                    else None
                ),
                exact_time_match=True,
                temporal_method=temporal.method,
                model_version=",".join(
                    str(item) for item in selected_surface.get("model_versions", [])
                )
                or None,
                time_distance_minutes=0.0,
                prediction_source="published_station_forecasts_exact_point_idw",
                confidence=confidence,
                nearest_station_distance_km=float(
                    selected_point["nearest_station_distance_km"]
                ),
                stations_used=int(selected_point["stations_used"]),
                quality_flag=selected_point.get("quality_flag"),
                surface_id=selected_surface.get("surface_id"),
                cell_id=None,
                cell_latitude=place.latitude,
                cell_longitude=place.longitude,
                interpolation_point_distance_km=0.0,
                unit=(
                    (selected_surface.get("metadata") or {}).get("units")
                    or unit_for(
                        parameter,
                        precipitation_accumulation_period_hours=int(
                            (selected_surface.get("metadata") or {}).get(
                                "precipitation_accumulation_period_hours", 6
                            )
                            or 6
                        ),
                    )
                ),
                category=category_for(parameter, value),
                station_predicted_value=station_value,
                spatial_method=str(selected_point.get("method")),
                distance_power=float(selected_point.get("distance_power", 2.0)),
                projected_crs=str(selected_point.get("projected_crs")),
                station_contributions=list(
                    selected_point.get("contributions") or []
                ),
                temporal_source_times=source_times,
                temporal_components=temporal_components,
                quality_status=str(
                    (selected_surface.get("metadata") or {}).get(
                        "quality_status", "accepted"
                    )
                ),
                experimental=bool(
                    (selected_surface.get("metadata") or {}).get(
                        "experimental", False
                    )
                ),
                experimental_reason=list(
                    (selected_surface.get("metadata") or {}).get(
                        "experimental_reason"
                    )
                    or []
                ),
            ),
            selected_surface,
        )

    @staticmethod
    def _surface_map_points(
        surface: dict[str, Any] | None,
        *,
        selected_station_id: int,
    ) -> list[dict[str, Any]]:
        if not surface:
            return []
        result: list[dict[str, Any]] = []
        for row in surface.get("stations") or []:
            if row.get("latitude") is None or row.get("longitude") is None:
                continue
            result.append(
                {
                    **row,
                    "value": row.get("predicted_value"),
                    "selected": int(row.get("station_id", -1)) == selected_station_id,
                    "parameter": surface.get("parameter"),
                    "target_time": surface.get("target_time"),
                }
            )
        return result

    def _surface_from_manifest_entry(
        self, entry: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.spatial_source is None:
            return None
        direct_loader = getattr(self.spatial_source, "surface_from_entry", None)
        if callable(direct_loader):
            return direct_loader(entry)
        return self.spatial_source.surface(
            parameter=str(entry.get("parameter")),
            horizon_hours=int(entry.get("horizon_hours", 0)) or None,
            target_time=(
                _aware(entry["target_time"]) if entry.get("target_time") else None
            ),
            exact_target_time=True,
        )

    def _timeline(
        self,
        *,
        place: ResolvedPlace,
        parameters: list[str],
        target_time: datetime,
        daily_profile: bool,
        max_workers: int = 8,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        if self.spatial_source is None:
            return [], [], 0
        manifest = self.spatial_source.latest_manifest() or {}
        wanted_parameters = list(dict.fromkeys(str(value) for value in parameters))
        wanted_set = set(wanted_parameters)
        requested_zone = target_time.tzinfo or UTC
        requested_date = target_time.astimezone(requested_zone).date()

        all_entries = [
            dict(entry)
            for entry in manifest.get("surfaces", [])
            if str(entry.get("parameter")) in wanted_set
            and entry.get("target_time")
        ]
        entries = all_entries
        if daily_profile:
            entries = [
                entry
                for entry in all_entries
                if _aware(entry["target_time"]).astimezone(requested_zone).date()
                == requested_date
            ]
            # If the requested date falls just outside the published window, return
            # the nearest compact 24-hour profile instead of all 48 hours.
            if not entries:
                nearest: list[dict[str, Any]] = []
                target_utc = target_time.astimezone(UTC)
                for parameter in wanted_parameters:
                    candidates = [
                        entry
                        for entry in all_entries
                        if str(entry.get("parameter")) == parameter
                    ]
                    candidates.sort(
                        key=lambda entry: abs(
                            (_aware(entry["target_time"]) - target_utc).total_seconds()
                        )
                    )
                    nearest.extend(candidates[:24])
                entries = nearest

        # De-duplicate the same physical object before concurrent downloads.
        deduplicated: dict[str, dict[str, Any]] = {}
        for entry in entries:
            identity = str(
                entry.get("object_key")
                or entry.get("surface_id")
                or (
                    f"{entry.get('parameter')}|{entry.get('target_time')}|"
                    f"{entry.get('horizon_hours')}"
                )
            )
            deduplicated[identity] = entry
        entries = list(deduplicated.values())

        def load(entry: dict[str, Any]) -> dict[str, Any] | None:
            surface = self._surface_from_manifest_entry(entry)
            if not surface:
                return None
            point = self._point_from_surface(surface, place)
            if point.get("value") is None:
                return None
            parameter = str(surface.get("parameter") or entry.get("parameter"))
            metadata = surface.get("metadata") or {}
            return {
                "parameter": parameter,
                "horizon_hours": int(surface.get("horizon_hours", 0)),
                "origin_time": surface.get("origin_time"),
                "target_time": surface.get("target_time"),
                "value": float(point["value"]),
                "confidence": point.get("confidence"),
                "surface_id": surface.get("surface_id"),
                "cell_id": None,
                "cell_latitude": place.latitude,
                "cell_longitude": place.longitude,
                "spatial_method": point.get("method"),
                "station_contributions": point.get("contributions") or [],
                "category": category_for(parameter, float(point["value"])),
                "unit": unit_for(
                    parameter,
                    precipitation_accumulation_period_hours=int(
                        metadata.get("precipitation_accumulation_period_hours", 6)
                        or 6
                    ),
                ),
            }

        result: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        worker_count = max(1, min(int(max_workers), len(entries) or 1))
        if worker_count == 1:
            for entry in entries:
                try:
                    row = load(entry)
                    if row is not None:
                        result.append(row)
                except Exception as exc:
                    errors.append(
                        {
                            "parameter": entry.get("parameter"),
                            "target_time": entry.get("target_time"),
                            "error": str(exc),
                        }
                    )
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count, thread_name_prefix="smog-timeline"
            ) as executor:
                futures = {executor.submit(load, entry): entry for entry in entries}
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        row = future.result()
                        if row is not None:
                            result.append(row)
                    except Exception as exc:
                        errors.append(
                            {
                                "parameter": entry.get("parameter"),
                                "target_time": entry.get("target_time"),
                                "error": str(exc),
                            }
                        )

        return (
            sorted(result, key=lambda item: (item["target_time"], item["parameter"])),
            errors,
            len(entries),
        )

    def timeline(self, request: TimelineRequest) -> TimelineResponse:
        started = time.perf_counter()
        requested_time = request.target_time
        if requested_time.tzinfo is None:
            requested_time = requested_time.replace(tzinfo=UTC)
        place = ResolvedPlace(
            name=request.place_name or "wybrany punkt",
            latitude=float(request.latitude),
            longitude=float(request.longitude),
            match_score=1.0,
            source="query_result",
            matched_text=request.place_name,
        )
        rows, errors, considered = self._timeline(
            place=place,
            parameters=list(dict.fromkeys(request.parameters)),
            target_time=requested_time,
            daily_profile=request.daily_profile,
        )
        return TimelineResponse(
            place_name=request.place_name,
            latitude=request.latitude,
            longitude=request.longitude,
            requested_target_time=requested_time,
            requested_date=requested_time.date().isoformat(),
            parameters=list(dict.fromkeys(request.parameters)),
            rows=rows,
            entries_considered=considered,
            surfaces_loaded=len(rows),
            errors=errors,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    @staticmethod
    def _summary(
        place: PlaceView,
        station: StationMatchView,
        intent: AirQualityIntent,
        forecasts: list[ForecastSelection],
    ) -> str:
        parts: list[str] = []
        labels = {
            "temperature_c": "temperatura",
            "precipitation_probability": "prawdopodobieństwo opadu",
            "precipitation_mm": "oczekiwany opad",
        }
        for row in forecasts:
            label = labels.get(row.parameter, row.parameter)
            if row.predicted_value is None:
                parts.append(f"{label}: brak prognozy")
                continue
            if row.parameter == "precipitation_probability":
                value_text = f"{row.predicted_value:.0%}"
            else:
                value_text = f"{row.predicted_value:.1f} {row.unit or ''}".strip()
            confidence = f", pewność {row.confidence:.0%}" if row.confidence is not None else ""
            parts.append(f"{label}: {value_text}{confidence}")
        local_target = intent.target_time.isoformat(timespec="minutes")
        exact = all(row.exact_time_match for row in forecasts) if forecasts else False
        experimental_parameters = [
            row.parameter for row in forecasts if row.experimental
        ]
        if intent.requested_view == "daily_profile":
            timing = (
                "Nie podano dokładnej godziny; wartości na mapie są punktem startowym, "
                "a profil godzinowy pokazuje przebieg całego dnia."
            )
        else:
            methods = {row.temporal_method for row in forecasts}
            if exact and "pchip" in methods:
                timing = (
                    "Najpierw wykonano IDW w tym samym dokładnym punkcie dla godzin "
                    "źródłowych, a następnie interpolację czasową PCHIP."
                )
            elif exact:
                timing = "Termin został dopasowany dokładnie do prognozy godzinowej."
            else:
                timing = "Dla podanego terminu nie ma kompletnego dokładnego pakietu godzinowego."
        quality_note = (
            " Wyniki eksperymentalne: "
            + ", ".join(experimental_parameters)
            + "; aktywny model nie przeszedł wszystkich miękkich progów jakości."
            if experimental_parameters
            else ""
        )
        return (
            f"{place.name}, termin {local_target}: " + ", ".join(parts) + ". "
            f"Najbliższa stacja referencyjna to {station.station_name} "
            f"({station.distance_km or 0:.1f} km). {timing} "
            "To prognoza dla dokładnych współrzędnych, obliczona z wersjonowanych "
            "prognoz stacyjnych odczytanych przez skonfigurowany Bridge danych."
            + quality_note
        )

    def preview(
        self,
        request: QueryRequest,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Interpret and verify a request without loading forecast surfaces.

        The dashboard uses this lightweight stage to build an editable contract
        (place, coordinates, exact time and parameters).  Expensive IDW/PCHIP
        work starts only after the user submits that structured contract.
        """

        request_started = time.perf_counter()
        snapshot_started = time.perf_counter()
        snapshot = self._latest_query_context()
        snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
        if snapshot is None:
            raise LookupError("Brak opublikowanego snapshotu prognoz")

        stations = snapshot.get("stations", [])
        manifest = self.spatial_source.latest_manifest() if self.spatial_source else None
        available_air_parameters = self._available_air_parameters(manifest, stations)
        parameter_aliases = self._available_air_parameter_aliases(
            manifest,
            snapshot,
            available_air_parameters,
        )

        interpretation_started = time.perf_counter()
        interpretation = self.interpreter.interpret(
            request.text,
            candidates=self._candidates(stations),
            now=now,
            available_parameters=available_air_parameters,
            parameter_aliases=parameter_aliases,
        )
        interpretation.intent, time_validation = self._validate_request_time(
            request,
            interpretation.intent,
            manifest,
        )
        interpretation_ms = (time.perf_counter() - interpretation_started) * 1000

        resolved, location_validation = self._resolve_request_place(
            request,
            interpretation.intent,
            stations,
        )
        place = PlaceView(
            name=resolved.name,
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            match_score=resolved.match_score,
            source=resolved.source,
            matched_text=resolved.matched_text,
            precision=resolved.precision,
            ambiguous=resolved.ambiguous,
        )
        proposed_parameters = self._proposed_parameters(request, interpretation.intent)

        return {
            "question": request.text,
            "intent": interpretation.intent.model_dump(mode="json"),
            "interpretation": interpretation.model_dump(mode="json"),
            "place": place.model_dump(mode="json"),
            "location_validation": location_validation,
            "time_validation": time_validation,
            "proposed_parameters": proposed_parameters,
            "performance": {
                "mode": "interpretation_preview",
                "forecast_computation_deferred": True,
                "snapshot_ms": round(snapshot_ms, 3),
                "interpretation_ms": round(interpretation_ms, 3),
                "total_ms": round((time.perf_counter() - request_started) * 1000, 3),
            },
        }

    def ask(
        self,
        request: QueryRequest,
        *,
        now: datetime | None = None,
        include_timeline: bool = True,
    ) -> QueryResponse:
        request_started = time.perf_counter()
        request_id = str(uuid.uuid4())
        snapshot_started = time.perf_counter()
        snapshot = self._latest_query_context()
        snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
        if snapshot is None:
            raise LookupError("Brak opublikowanego snapshotu prognoz")
        stations = snapshot.get("stations", [])
        manifest = self.spatial_source.latest_manifest() if self.spatial_source else None
        available_air_parameters = self._available_air_parameters(manifest, stations)
        available_air_parameter_aliases = self._available_air_parameter_aliases(
            manifest, snapshot, available_air_parameters
        )
        with self.observability.observation(
            name="air-quality-natural-language-query",
            as_type="span",
            input={"question": request.text, "request_id": request_id},
            metadata={
                "snapshot_publication_id": (snapshot.get("metadata") or {}).get("publication_id"),
                "snapshot_backend": self.snapshot_source.backend_name,
                "spatial_backend": self.spatial_source.backend_name if self.spatial_source else "disabled",
                "inference_mode": "precomputed-local-results",
                "prompt_template_version": self.prompt_template_version,
                "session_id": request.session_id,
                "user_id": request.user_id,
            },
        ) as root:
            interpretation_started = time.perf_counter()
            if request.is_confirmed_structured_request:
                # The user has already confirmed the point, exact time and
                # requested parameters in the dashboard. Reusing those
                # structured fields avoids a second OpenAI call and prevents a
                # confirmation loop. No coordinate is generated here.
                interpretation = InterpretationResult(
                    intent=AirQualityIntent(
                        location=request.place_name or "wybrany punkt",
                        latitude=float(request.latitude),
                        longitude=float(request.longitude),
                        location_source="map_point",
                        location_precision="exact_coordinates",
                        target_time=request.target_time,
                        pollutants=list(request.parameters or ["PM10", "PM2.5"]),
                        language="pl",
                        requested_view=request.requested_view,
                        time_was_explicit=True,
                        time_precision="exact_minute",
                        confidence=1.0,
                        assumptions=[
                            "Użyto punktu, czasu i parametrów zatwierdzonych przez użytkownika."
                        ],
                    ),
                    provider=(
                        request.parser_provider or "confirmed_structured_request"
                    ),
                    model=request.parser_model,
                    latency_ms=0.0,
                    prompt_tokens=request.parser_prompt_tokens,
                    completion_tokens=request.parser_completion_tokens,
                    fallback_used=False,
                    raw_response={
                        "usage_source": "carried_from_query_preview",
                        "confirmed_structured_request": True,
                    },
                )
            else:
                interpretation = self.interpreter.interpret(
                    request.text,
                    candidates=self._candidates(stations),
                    now=now,
                    available_parameters=available_air_parameters,
                    parameter_aliases=available_air_parameter_aliases,
                )
            interpretation.intent, time_validation = self._validate_request_time(
                request,
                interpretation.intent,
                manifest,
            )
            interpretation_ms = (time.perf_counter() - interpretation_started) * 1000
            resolved, location_validation = self._resolve_request_place(
                request, interpretation.intent, stations
            )
            place = PlaceView(
                name=resolved.name,
                latitude=resolved.latitude,
                longitude=resolved.longitude,
                match_score=resolved.match_score,
                source=resolved.source,
                matched_text=resolved.matched_text,
                precision=resolved.precision,
                ambiguous=resolved.ambiguous,
            )
            station = self._nearest_station(resolved, stations)
            station_raw = next(row for row in stations if int(row["station_id"]) == station.station_id)
            warnings = list(interpretation.intent.assumptions)
            forecasts: list[ForecastSelection] = []
            surfaces: dict[str, dict[str, Any]] = {}
            # HF21_PARAMETER_CONTRACT_V1
            requested_parameters = self._proposed_parameters(
                request,
                interpretation.intent,
            )
            exact_required = bool((manifest or {}).get("exact_target_time_available"))
            surfaces_started = time.perf_counter()
            spatial_results: dict[
                str, tuple[ForecastSelection | None, dict[str, Any] | None]
            ] = {}
            if self.spatial_source is not None and len(requested_parameters) > 1:
                with ThreadPoolExecutor(
                    max_workers=min(5, len(requested_parameters)),
                    thread_name_prefix="smog-query",
                ) as executor:
                    futures = {
                        executor.submit(
                            self._select_spatial_forecast,
                            parameter=parameter,
                            target_time=interpretation.intent.target_time,
                            place=resolved,
                            station=station,
                        ): parameter
                        for parameter in requested_parameters
                    }
                    for future in as_completed(futures):
                        parameter = futures[future]
                        try:
                            spatial_results[parameter] = future.result()
                        except Exception as exc:
                            warnings.append(
                                f"Nie udało się odczytać powierzchni {parameter}: {exc}"
                            )
                            spatial_results[parameter] = (None, None)
            else:
                for parameter in requested_parameters:
                    spatial_results[parameter] = self._select_spatial_forecast(
                        parameter=parameter,
                        target_time=interpretation.intent.target_time,
                        place=resolved,
                        station=station,
                    )

            for parameter in requested_parameters:
                spatial_forecast, surface = spatial_results.get(parameter, (None, None))
                if spatial_forecast is not None:
                    forecasts.append(spatial_forecast)
                    if surface:
                        surfaces[parameter] = surface
                    continue
                fallback = self._select_station_forecast(
                    snapshot.get("forecasts", []),
                    station_id=station.station_id,
                    parameter=parameter,
                    target_time=interpretation.intent.target_time,
                    exact_target_time=exact_required,
                )
                if fallback is not None:
                    forecasts.append(fallback)
                    warnings.append(
                        f"Dla {parameter} nie znaleziono gotowej powierzchni; pokazano dokładną prognozę stacji."
                    )
                else:
                    warnings.append(f"Brak opublikowanej prognozy {parameter} dla wskazanego terminu.")
            surface_selection_ms = (time.perf_counter() - surfaces_started) * 1000

            for row in forecasts:
                if exact_required and not row.exact_time_match:
                    warnings.append(
                        f"Dla {row.parameter} nie użyto wyniku, który nie odpowiada dokładnie żądanemu terminowi."
                    )
                if row.confidence is not None and row.confidence < 0.35:
                    warnings.append(
                        f"Pewność interpolacji {row.parameter} w tej lokalizacji jest niska ({row.confidence:.0%})."
                    )
                if row.experimental:
                    warnings.append(
                        f"{row.parameter}: wynik eksperymentalny — aktywny model "
                        "nie przeszedł wszystkich miękkich progów jakości."
                    )

            primary_parameter = requested_parameters[0]
            primary_surface = surfaces.get(primary_parameter)
            daily_profile = interpretation.intent.requested_view == "daily_profile"
            exact_matches = [row for row in forecasts if row.exact_time_match]
            selected_times = sorted(
                {row.target_time.isoformat() for row in forecasts if row.target_time}
            )
            time_selection = {
                "mode": "daily_profile" if daily_profile else "exact_target",
                "time_was_explicit": interpretation.intent.time_was_explicit,
                "time_precision": interpretation.intent.time_precision,
                "requested_target_time": interpretation.intent.target_time.isoformat(),
                "exact_package_required": exact_required,
                "all_selected_values_exact": bool(forecasts) and len(exact_matches) == len(forecasts),
                "selected_target_times": selected_times,
                "silent_nearest_selection_allowed": not exact_required,
                "timeline_deferred": not include_timeline,
                "operation_order": "spatial_then_temporal",
                "temporal_methods": {
                    row.parameter: row.temporal_method for row in forecasts
                },
                "temporal_source_times": {
                    row.parameter: [value.isoformat() for value in row.temporal_source_times]
                    for row in forecasts
                },
            }
            response = QueryResponse(
                request_id=request_id,
                trace_id=root.trace_id,
                question=request.text,
                intent=interpretation.intent,
                interpretation=interpretation,
                place=place,
                station=station,
                forecasts=forecasts,
                current_measurements=station_raw.get("measurements") or {},
                weather=station_raw.get("weather"),
                summary=self._summary(place, station, interpretation.intent, forecasts),
                warnings=warnings,
                map_points=self._surface_map_points(
                    primary_surface,
                    selected_station_id=station.station_id,
                ) or self._fallback_map_points(
                    snapshot,
                    parameter=primary_parameter,
                    target_time=interpretation.intent.target_time,
                    selected_station_id=station.station_id,
                    exact_target_time=exact_required,
                ),
                timeline=(
                    self._timeline(
                        place=resolved,
                        parameters=requested_parameters,
                        target_time=interpretation.intent.target_time,
                        daily_profile=daily_profile,
                    )[0]
                    if include_timeline
                    else []
                ),
                surface_options=list((manifest or {}).get("surfaces", [])),
                selected_surface={
                    key: primary_surface.get(key)
                    for key in (
                        "surface_id",
                        "parameter",
                        "horizon_hours",
                        "origin_time",
                        "target_time",
                        "generated_at",
                        "model_versions",
                        "metadata",
                        "metrics",
                    )
                    if primary_surface and key in primary_surface
                },
                time_selection=time_selection,
                snapshot_metadata=snapshot.get("metadata") or {},
                performance={
                    "snapshot_ms": round(snapshot_ms, 2),
                    "interpretation_ms": round(interpretation_ms, 2),
                    "surface_selection_ms": round(surface_selection_ms, 2),
                    "timeline_deferred": not include_timeline,
                    "total_ms": round((time.perf_counter() - request_started) * 1000, 2),
                },
                location_validation=location_validation,
                time_validation=time_validation,
            )
            root.update(
                output=prepare_interaction_payload(
                    response.model_dump(mode="json")
                ),
                metadata={
                    "prompt_template_version": self.prompt_template_version,
                    "llm_provider": interpretation.provider,
                    "llm_model": interpretation.model,
                    "llm_latency_ms": interpretation.latency_ms,
                    "fallback_used": interpretation.fallback_used,
                    "interpretation_confidence": interpretation.intent.confidence,
                    "precomputed_surface_used": bool(surfaces),
                    "exact_time_match": bool(forecasts)
                    and all(item.exact_time_match for item in forecasts),
                    "model_versions": sorted(
                        {
                            str(item.model_version)
                            for item in forecasts
                            if item.model_version
                        }
                    ),
                    "source_age_hours": {
                        item.parameter: item.source_age_hours
                        for item in forecasts
                        if item.source_age_hours is not None
                    },
                    "requested_target_time": interpretation.intent.target_time.isoformat(),
                    "selected_target_times": selected_times,
                },
            )
        if self.flush_observability:
            self.observability.flush()
        return response

    @classmethod
    def _fallback_map_points(
        cls,
        snapshot: dict[str, Any],
        *,
        parameter: str,
        target_time: datetime,
        selected_station_id: int,
        exact_target_time: bool = False,
    ) -> list[dict[str, Any]]:
        forecasts = snapshot.get("forecasts", [])
        points: list[dict[str, Any]] = []
        for station in snapshot.get("stations", []):
            if station.get("latitude") is None or station.get("longitude") is None:
                continue
            forecast = cls._select_station_forecast(
                forecasts,
                station_id=int(station["station_id"]),
                parameter=parameter,
                target_time=target_time,
                exact_target_time=exact_target_time,
            )
            measurement = (station.get("measurements") or {}).get(parameter) or {}
            value = (
                forecast.predicted_value
                if forecast is not None
                else None if exact_target_time else measurement.get("value")
            )
            if value is None:
                continue
            points.append(
                {
                    "station_id": int(station["station_id"]),
                    "station_name": station.get("station_name"),
                    "city_name": station.get("city_name"),
                    "latitude": station.get("latitude"),
                    "longitude": station.get("longitude"),
                    "value": value,
                    "parameter": parameter,
                    "target_time": forecast.target_time.isoformat() if forecast and forecast.target_time else measurement.get("measurement_time"),
                    "selected": int(station["station_id"]) == selected_station_id,
                    "quality_flags": station.get("open_quality_flags", 0),
                }
            )
        return points
