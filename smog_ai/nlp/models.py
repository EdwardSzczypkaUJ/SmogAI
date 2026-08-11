from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smog_ai.air_parameters import canonical_code


class ParsedLocation(BaseModel):
    """Location wording and coordinate candidate extracted by the LLM."""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=300)
    primary_name: str = Field(min_length=1, max_length=200)
    context_name: str | None
    latitude: float | None = Field(ge=-90.0, le=90.0)
    longitude: float | None = Field(ge=-180.0, le=180.0)
    coordinate_precision: Literal[
        "exact_user_coordinates",
        "poi",
        "address",
        "locality_centroid",
        "approximate",
        "unknown",
    ]
    coordinate_confidence: float = Field(ge=0.0, le=1.0)
    coordinate_basis: str = Field(min_length=1, max_length=300)

    @field_validator("raw_text", "primary_name")
    @classmethod
    def location_contains_letters(cls, value: str) -> str:
        cleaned = value.strip()
        if not any(character.isalpha() for character in cleaned):
            raise ValueError("location must contain a place name, not only numbers")
        return cleaned

    @model_validator(mode="after")
    def candidate_coordinates_are_paired(self) -> ParsedLocation:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("candidate latitude and longitude must be provided together")
        return self


class StructuredIntentOutput(BaseModel):
    """Strict OpenAI Structured Outputs contract.

    Every property is required so the generated JSON Schema can be used with
    ``strict: true``. Nullable values stay required but may contain JSON null.
    Coordinates are only candidates. Application code validates them before use.
    """

    model_config = ConfigDict(extra="forbid")

    location: ParsedLocation
    target_time: datetime
    pollutants: list[str] = Field(min_length=1, max_length=16)
    language: str
    requested_view: Literal["forecast", "current", "comparison", "daily_profile"]
    time_was_explicit: bool
    time_precision: Literal[
        "exact_minute",
        "hour",
        "relative_duration",
        "part_of_day",
        "date_only",
        "unspecified",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    assumptions: list[str]

    @field_validator("target_time")
    @classmethod
    def target_time_has_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("target_time must include an explicit UTC offset")
        return value


class AirQualityIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1, max_length=200)
    location_raw: str | None = Field(default=None, max_length=300)
    location_context: str | None = Field(default=None, max_length=200)
    candidate_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    candidate_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    candidate_coordinate_precision: str | None = Field(default=None, max_length=64)
    candidate_coordinate_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_coordinate_basis: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    location_source: Literal[
        "text",
        "text_coordinates",
        "map_point",
        "saved_point",
        "address_or_poi",
        "locality_centroid",
    ] = "text"
    location_precision: Literal[
        "exact_coordinates",
        "map_point",
        "saved_point",
        "address_or_poi",
        "locality_centroid",
        "unresolved",
    ] = "unresolved"
    target_time: datetime
    reference_target_time: datetime | None = None
    reference_time_precision: str | None = Field(default=None, max_length=64)
    pollutants: list[str] = Field(
        default_factory=lambda: ["PM10", "PM2.5"],
        min_length=1,
        max_length=16,
    )
    language: str = "pl"
    requested_view: Literal["forecast", "current", "comparison", "daily_profile"] = "forecast"
    time_was_explicit: bool = False
    time_precision: Literal[
        "exact_minute",
        "hour",
        "relative_duration",
        "part_of_day",
        "date_only",
        "unspecified",
    ] = "unspecified"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coordinates_are_paired(self) -> AirQualityIntent:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if (self.candidate_latitude is None) != (self.candidate_longitude is None):
            raise ValueError("candidate latitude and longitude must be provided together")
        return self

    @field_validator("pollutants")
    @classmethod
    def unique_pollutants(cls, value: list[str]) -> list[str]:
        output: list[str] = []
        for raw in value or ["PM10", "PM2.5"]:
            code = canonical_code(str(raw))
            if code != "UNKNOWN" and code not in output:
                output.append(code)
        return output or ["PM10", "PM2.5"]


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: AirQualityIntent
    provider: str
    model: str | None = None
    latency_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    fallback_used: bool = False
    raw_response: dict | None = None
