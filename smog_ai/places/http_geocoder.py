from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx

from smog_ai.places.base import ResolvedPlace


class HttpGeocoderResolver:
    """Opt-in Nominatim-compatible resolver with a persistent local cache.

    The endpoint is configuration, not a hard-coded infrastructure dependency.
    This makes it possible to use a self-hosted instance or another compatible
    provider. Calls are serialized and rate-limited; repeated queries are read
    from the local JSON cache and never sent to Spaces.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        user_agent: str,
        cache_path: Path,
        timeout_seconds: float = 8.0,
        minimum_interval_seconds: float = 1.0,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("HTTP geocoder endpoint is required")
        if not user_agent.strip() or user_agent.strip().lower().startswith("python-httpx"):
            raise ValueError("HTTP geocoder requires an identifying User-Agent")
        normalized = endpoint.rstrip("/")
        self.search_url = normalized if normalized.endswith("/search") else f"{normalized}/search"
        self.user_agent = user_agent.strip()
        self.cache_path = cache_path
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cache = self._load_cache()

    @property
    def candidates(self) -> list[str]:
        return []

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)

    def _fetch(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
    ) -> list[dict[str, Any]]:
        cache_key = query.casefold().strip()
        if near is not None:
            cache_key += f"|near={near[0]:.4f},{near[1]:.4f}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return list(cached.get("results") or [])
            remaining = self.minimum_interval_seconds - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            params: dict[str, Any] = {
                "q": query,
                "format": "jsonv2",
                "addressdetails": 1,
                "countrycodes": "pl",
                "limit": 10,
            }
            if near is not None:
                latitude, longitude = near
                # Roughly 80-100 km around the context point in Poland. The
                # ordering required by Nominatim is left, top, right, bottom.
                params["viewbox"] = (
                    f"{longitude - 1.25:.6f},{latitude + 0.85:.6f},"
                    f"{longitude + 1.25:.6f},{latitude - 0.85:.6f}"
                )
                params["bounded"] = 1
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(
                    self.search_url,
                    params=params,
                    headers={"User-Agent": self.user_agent},
                )
            self._last_request = time.monotonic()
            response.raise_for_status()
            results = response.json()
            if not isinstance(results, list):
                raise ValueError("Geocoder returned an invalid response")
            self._cache[cache_key] = {"results": results}
            self._save_cache()
            return results

    @staticmethod
    def _normalise(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value.casefold())
        ascii_value = "".join(
            character for character in decomposed
            if not unicodedata.combining(character)
        )
        return " ".join(re.findall(r"[a-z0-9]+", ascii_value))

    @classmethod
    def _candidate_score(
        cls,
        row: dict[str, Any],
        query: str,
        *,
        near: tuple[float, float] | None = None,
    ) -> float:
        """Prefer a named POI matching the query over its containing village."""
        query_text = cls._normalise(query)
        name_text = cls._normalise(str(row.get("name") or ""))
        display_text = cls._normalise(str(row.get("display_name") or ""))
        query_tokens = {
            token
            for token in query_text.split()
            if token not in {"kolo", "okolice", "w", "we", "pod", "przy", "pl", "polska"}
        }
        name_tokens = set(name_text.split())
        display_tokens = set(display_text.split())
        score = float(row.get("importance") or 0.0)
        score += 3.0 * len(query_tokens & name_tokens)
        score += 0.35 * len(query_tokens & display_tokens)
        if name_text and (name_text in query_text or query_text in name_text):
            score += 5.0
        result_type = cls._normalise(
            str(row.get("type") or row.get("addresstype") or "")
        )
        result_class = cls._normalise(str(row.get("class") or row.get("category") or ""))
        if result_type in {"aerodrome", "airport", "airfield"} or result_class == "aeroway":
            if {"lotnisko", "ladowisko", "aerodrome", "airport"} & query_tokens:
                score += 8.0
        if near is not None and row.get("lat") is not None and row.get("lon") is not None:
            distance_km = cls._haversine_km(
                near[0], near[1], float(row["lat"]), float(row["lon"])
            )
            score -= min(distance_km, 500.0) / 12.0
        return score

    @staticmethod
    def _haversine_km(
        latitude_a: float,
        longitude_a: float,
        latitude_b: float,
        longitude_b: float,
    ) -> float:
        radius_km = 6371.0088
        phi_a = math.radians(latitude_a)
        phi_b = math.radians(latitude_b)
        delta_phi = math.radians(latitude_b - latitude_a)
        delta_lambda = math.radians(longitude_b - longitude_a)
        value = (
            math.sin(delta_phi / 2.0) ** 2
            + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
        )
        return radius_km * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))

    @staticmethod
    def _resolved_name(row: dict[str, Any], query: str) -> str:
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        row_name = str(row.get("name") or "").strip()
        addresstype = str(row.get("addresstype") or row.get("type") or "").casefold()
        locality_types = {
            "city",
            "town",
            "village",
            "municipality",
            "hamlet",
            "administrative",
        }
        # A POI must keep its own name. Its address may legitimately contain a
        # different nearby village (for example Borówno for Lądowisko Witków).
        if row_name and addresstype not in locality_types:
            return row_name
        return next(
            (
                str(address[key])
                for key in ("city", "town", "village", "municipality", "hamlet")
                if address.get(key)
            ),
            row_name or str(row.get("display_name") or query).split(",", 1)[0],
        )

    def resolve(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
    ) -> ResolvedPlace:
        if not query.strip():
            raise ValueError("Location cannot be empty")
        results = self._fetch(query.strip(), near=near)
        if not results:
            raise ValueError(f"Geocoder nie znalazł lokalizacji: {query}")
        row = max(
            results,
            key=lambda item: self._candidate_score(item, query, near=near),
        )
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        country_code = str(address.get("country_code") or "").lower()
        if country_code and country_code != "pl":
            raise ValueError(f"Geocoder zwrócił lokalizację poza Polską: {query}")
        if near is not None:
            distance_to_context = self._haversine_km(
                near[0], near[1], float(row["lat"]), float(row["lon"])
            )
            if distance_to_context > 120.0:
                raise ValueError(
                    f"Geocoder zwrócił punkt {distance_to_context:.1f} km od kontekstu: {query}"
                )
        name = self._resolved_name(row, query)
        return ResolvedPlace(
            name=name,
            latitude=float(row["lat"]),
            longitude=float(row["lon"]),
            match_score=max(0.0, min(1.0, float(row.get("importance") or 0.75))),
            source="OpenStreetMap/Nominatim (ODbL)",
            matched_text=str(row.get("display_name") or query),
            precision="address_or_poi" if row.get("addresstype") not in {"city", "town", "village"} else "locality_centroid",
            ambiguous=False,
        )


class OfflineFirstPlaceResolver:
    """Use deterministic local data first and HTTP only for an offline miss."""

    def __init__(self, offline: Any, remote: HttpGeocoderResolver) -> None:
        self.offline = offline
        self.remote = remote

    @property
    def candidates(self) -> list[str]:
        return self.offline.candidates

    def resolve(self, query: str) -> ResolvedPlace:
        try:
            return self.offline.resolve(query)
        except ValueError:
            return self.remote.resolve(query)

    def resolve_intent(
        self,
        *,
        primary_name: str,
        raw_text: str | None,
        context_name: str | None,
    ) -> ResolvedPlace:
        near: tuple[float, float] | None = None
        if context_name:
            try:
                context = self.offline.resolve(context_name)
                near = (context.latitude, context.longitude)
            except ValueError:
                try:
                    context = self.remote.resolve(context_name)
                    near = (context.latitude, context.longitude)
                except ValueError:
                    near = None

        query = primary_name.strip()
        raw_normalised = self.remote._normalise(raw_text or "")
        query_normalised = self.remote._normalise(query)
        poi_words = {
            "lotnisko": "lotnisko",
            "ladowisko": "lądowisko",
            "szybowisko": "szybowisko",
            "stacja": "stacja",
            "szpital": "szpital",
            "stadion": "stadion",
        }
        for token, display in poi_words.items():
            if token in raw_normalised.split() and token not in query_normalised.split():
                query = f"{display} {query}"
                break
        try:
            # When HTTP geocoding is explicitly enabled, use it as the
            # independent verification source even for names already present
            # in the small bundled gazetteer. A phrase such as "koło
            # Mieroszowa" becomes a spatial constraint, not part of the POI
            # name. Results are cached locally.
            return self.remote.resolve(query, near=near)
        except ValueError:
            if raw_text and raw_text.strip() != query:
                try:
                    return self.remote.resolve(raw_text, near=near)
                except ValueError:
                    pass
            return self.offline.resolve(primary_name)
