from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ResolvedPlace:
    name: str
    latitude: float
    longitude: float
    match_score: float
    source: str
    population: int | None = None
    matched_text: str | None = None
    precision: str = "unresolved"
    ambiguous: bool = False


@runtime_checkable
class PlaceResolver(Protocol):
    @property
    def candidates(self) -> list[str]:
        ...

    def resolve(self, query: str) -> ResolvedPlace:
        ...
