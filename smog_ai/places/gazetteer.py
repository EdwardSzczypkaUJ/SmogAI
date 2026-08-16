from __future__ import annotations

import csv
import difflib
import unicodedata
from pathlib import Path
from typing import Any

from smog_ai.places.base import ResolvedPlace


def normalize_place(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character) and character.isalnum()
    )


def _polish_place_variants(value: str) -> set[str]:
    """Return conservative nominative variants of common Polish locatives."""

    normalized = normalize_place(value)
    variants = {normalized}
    if normalized.endswith("owie") and len(normalized) > 5:
        variants.add(normalized[:-2])  # Witkowie -> Witkow, Krakowie -> Krakow
    if normalized.endswith("aniu") and len(normalized) > 5:
        variants.add(normalized[:-2])  # Poznaniu -> Poznan
    if normalized.endswith("awiu") and len(normalized) > 5:
        variants.add(normalized[:-2])  # Wroclawiu -> Wroclaw
    if normalized.endswith("sku") and len(normalized) > 4:
        variants.add(normalized[:-1])  # Gdansku -> Gdansk
    if normalized.endswith("awie") and len(normalized) > 5:
        variants.add(normalized[:-2] + "a")  # Warszawie -> Warszawa
    return {variant for variant in variants if variant}


class PolishGazetteerResolver:
    """Offline, deterministic place resolver with an extensible data source.

    The bundled gazetteer covers major Polish cities. Dynamic station cities from
    the current snapshot are merged at runtime and take precedence when their
    coordinates are available. A different geocoder can implement the same port.
    """

    def __init__(
        self,
        csv_path: Path,
        *,
        dynamic_places: list[dict[str, Any]] | None = None,
        minimum_score: float = 0.86,
        ambiguity_margin: float = 0.03,
    ) -> None:
        self.csv_path = csv_path
        self.minimum_score = minimum_score
        self.ambiguity_margin = ambiguity_margin
        self._rows: list[dict[str, Any]] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            for raw in csv.DictReader(stream):
                aliases = [value.strip() for value in str(raw.get("aliases") or "").split("|") if value.strip()]
                self._rows.append(
                    {
                        "name": str(raw["name"]),
                        "latitude": float(raw["latitude"]),
                        "longitude": float(raw["longitude"]),
                        "population": int(float(raw["population"])) if raw.get("population") else None,
                        "aliases": aliases,
                        "source": str(raw.get("source") or "offline_gazetteer"),
                    }
                )
        self._merge_dynamic(dynamic_places or [])

    def _merge_dynamic(self, rows: list[dict[str, Any]]) -> None:
        existing = {normalize_place(row["name"]): index for index, row in enumerate(self._rows)}
        for raw in rows:
            name = str(raw.get("city_name") or raw.get("station_name") or "").strip()
            latitude = raw.get("latitude")
            longitude = raw.get("longitude")
            if not name or latitude is None or longitude is None:
                continue
            normalized = normalize_place(name)
            dynamic = {
                "name": name,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "population": None,
                "aliases": [str(raw.get("station_name") or "")],
                "source": "gios_station_snapshot",
            }
            if normalized in existing:
                # Exact live station coordinates are preferred over a curated city
                # centre only for the dynamic row's canonical name.
                self._rows[existing[normalized]] = dynamic
            else:
                existing[normalized] = len(self._rows)
                self._rows.append(dynamic)

    @property
    def candidates(self) -> list[str]:
        ordered = sorted(
            self._rows,
            key=lambda row: (-(row.get("population") or 0), row["name"]),
        )
        return [str(row["name"]) for row in ordered]

    @staticmethod
    def _score(query: str, row: dict[str, Any]) -> tuple[float, str]:
        targets = _polish_place_variants(query)
        names = [row["name"], *row.get("aliases", [])]
        best = 0.0
        matched = row["name"]
        for value in names:
            names_normalized = _polish_place_variants(str(value))
            for target in targets:
                for normalized in names_normalized:
                    if target == normalized:
                        score = 1.0
                    elif target in normalized or normalized in target:
                        score = 0.95
                    else:
                        score = difflib.SequenceMatcher(
                            None, target, normalized
                        ).ratio()
                        # Polish locative/genitive forms often differ only at
                        # the suffix.
                        common = 0
                        for left, right in zip(target, normalized, strict=False):
                            if left != right:
                                break
                            common += 1
                        if common >= min(5, len(target), len(normalized)):
                            score = max(
                                score,
                                0.82 * common / max(len(target), len(normalized))
                                + 0.18,
                            )
                    if score > best:
                        best = score
                        matched = str(value)
        return best, matched

    def resolve(self, query: str) -> ResolvedPlace:
        if not query.strip():
            raise ValueError("Location cannot be empty")
        scored = [(self._score(query, row), row) for row in self._rows]
        ranked = sorted(
            scored,
            key=lambda item: (item[0][0], item[1].get("population") or 0),
            reverse=True,
        )
        (score, matched), row = ranked[0]
        if score < self.minimum_score:
            raise ValueError(f"Nie znaleziono polskiej miejscowości odpowiadającej: {query}")
        if len(ranked) > 1:
            second_score = ranked[1][0][0]
            if score < 1.0 and score - second_score < self.ambiguity_margin:
                raise ValueError(
                    f"Lokalizacja jest niejednoznaczna: {query}. Podaj województwo, "
                    "powiat albo dokładne współrzędne."
                )
        return ResolvedPlace(
            name=str(row["name"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            match_score=round(float(score), 4),
            source=str(row.get("source") or "offline_gazetteer"),
            population=row.get("population"),
            matched_text=matched,
            precision=(
                "exact_poi"
                if str(row.get("source") or "").endswith("_poi")
                else "locality_centroid"
            ),
            ambiguous=False,
        )
