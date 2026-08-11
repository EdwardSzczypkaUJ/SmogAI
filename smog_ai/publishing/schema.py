from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SnapshotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_id: str
    schema_version: str = "1.1"
    generated_at: datetime
    data_start: datetime | None = None
    data_end: datetime | None = None
    model_version: str | None = None
    record_count: int = Field(ge=0)
    checksum: str
    source_host_id: str


class SnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: SnapshotMetadata
    stations: list[dict[str, Any]] = Field(default_factory=list)
    forecasts: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    quality_summary: dict[str, Any] = Field(default_factory=dict)
    spatial: dict[str, Any] = Field(default_factory=dict)
    air_parameter_catalog: dict[str, dict[str, Any]] = Field(default_factory=dict)
