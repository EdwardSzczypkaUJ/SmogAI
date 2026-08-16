from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from smog_ai.artifacts.repository import ArtifactRepository


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@runtime_checkable
class SpatialSource(Protocol):
    backend_name: str

    def ping(self) -> None: ...

    def latest_pointer(self) -> dict[str, Any] | None: ...

    def latest_manifest(self) -> dict[str, Any] | None: ...

    def surface(
        self,
        *,
        parameter: str,
        horizon_hours: int | None = None,
        target_time: datetime | None = None,
        exact_target_time: bool = False,
    ) -> dict[str, Any] | None: ...

    def surface_from_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None: ...

    def boundary(self) -> dict[str, Any] | None: ...

    def places(self) -> list[dict[str, Any]]: ...


class ObjectStoreSpatialSource:
    """Read-only source of locally precomputed surface artifacts."""

    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        cache_ttl_seconds: float = 60.0,
        cache_max_items: int = 64,
    ) -> None:
        self.repository = repository
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self.cache_max_items = max(1, int(cache_max_items))
        self.backend_name = f"{repository.store.backend_name}-spatial"
        self._pointer_cache: tuple[float, Any] | None = None
        self._immutable_cache: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.RLock()

    def _cached_pointer(self, loader):  # type: ignore[no-untyped-def]
        now = time.monotonic()
        with self._lock:
            item = self._pointer_cache
            if item is not None and (self.cache_ttl_seconds == 0 or item[0] >= now):
                return item[1]
        value = loader()
        expires = float("inf") if self.cache_ttl_seconds == 0 else now + self.cache_ttl_seconds
        with self._lock:
            self._pointer_cache = (expires, value)
        return value

    def _cached_immutable(self, key: str, loader):  # type: ignore[no-untyped-def]
        with self._lock:
            if key in self._immutable_cache:
                value = self._immutable_cache.pop(key)
                self._immutable_cache[key] = value
                return value
        value = loader()
        with self._lock:
            if key in self._immutable_cache:
                cached = self._immutable_cache.pop(key)
                self._immutable_cache[key] = cached
                return cached
            self._immutable_cache[key] = value
            while len(self._immutable_cache) > self.cache_max_items:
                self._immutable_cache.popitem(last=False)
        return value

    def ping(self) -> None:
        self.repository.ping()

    def latest_pointer(self) -> dict[str, Any] | None:
        try:
            return self._cached_pointer(
                lambda: self.repository.get_json(self.repository.layout.latest_spatial_pointer),
            )
        except Exception:
            return None

    def latest_manifest(self) -> dict[str, Any] | None:
        pointer = self.latest_pointer()
        if not pointer:
            return None
        key = str(pointer["manifest_key"])
        try:
            return self._cached_immutable(f"json:{key}", lambda: self.repository.get_json(key))
        except Exception:
            return None

    @staticmethod
    def _choose_entry(
        manifest: dict[str, Any],
        *,
        parameter: str,
        horizon_hours: int | None,
        target_time: datetime | None,
        exact_target_time: bool = False,
    ) -> dict[str, Any] | None:
        wanted = parameter.upper().replace("PM25", "PM2.5")
        candidates = [
            row
            for row in manifest.get("surfaces", [])
            if str(row.get("parameter", "")).upper() == wanted
        ]
        if not candidates:
            return None
        target = target_time.astimezone(UTC) if target_time is not None else None
        if target is not None and exact_target_time:
            exact = [
                row
                for row in candidates
                if row.get("target_time")
                and abs((_aware(row["target_time"]) - target).total_seconds()) < 1.0
            ]
            if not exact:
                return None
            candidates = exact

        def rank(row: dict[str, Any]) -> tuple[float, float, str]:
            horizon_distance = (
                abs(int(row.get("horizon_hours", 0)) - int(horizon_hours))
                if horizon_hours is not None
                else 0.0
            )
            target_distance = (
                abs((_aware(row["target_time"]) - target).total_seconds())
                if target is not None and row.get("target_time")
                else 0.0
            )
            return target_distance, float(horizon_distance), str(row.get("target_time") or "")

        return min(candidates, key=rank)

    def surface(
        self,
        *,
        parameter: str,
        horizon_hours: int | None = None,
        target_time: datetime | None = None,
        exact_target_time: bool = False,
    ) -> dict[str, Any] | None:
        manifest = self.latest_manifest()
        if not manifest:
            return None
        entry = self._choose_entry(
            manifest,
            parameter=parameter,
            horizon_hours=horizon_hours,
            target_time=target_time,
            exact_target_time=exact_target_time,
        )
        if not entry:
            return None
        key = str(entry["object_key"])
        return self._cached_immutable(f"gzip:{key}", lambda: self.repository.get_gzip_json(key))

    def surface_from_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        key = entry.get("object_key")
        if not key:
            return None
        normalized = str(key)
        return self._cached_immutable(
            f"gzip:{normalized}",
            lambda: self.repository.get_gzip_json(normalized),
        )

    def boundary(self) -> dict[str, Any] | None:
        manifest = self.latest_manifest()
        key = (manifest or {}).get("boundary_key") or self.repository.layout.spatial_boundary
        try:
            if str(key).endswith(".gz"):
                return self._cached_immutable(f"gzip:{key}", lambda: self.repository.get_gzip_json(str(key)))
            return self._cached_immutable(f"json:{key}", lambda: self.repository.get_json(str(key)))
        except Exception:
            return None

    def places(self) -> list[dict[str, Any]]:
        manifest = self.latest_manifest()
        key = (manifest or {}).get("places_key") or self.repository.layout.spatial_places
        try:
            if str(key).endswith(".gz"):
                payload = self._cached_immutable(
                    f"gzip:{key}", lambda: self.repository.get_gzip_json(str(key))
                )
            else:
                payload = self._cached_immutable(f"json:{key}", lambda: self.repository.get_json(str(key)))
            return list(payload.get("places", [])) if isinstance(payload, dict) else []
        except Exception:
            return []


class StaticSpatialSource:
    backend_name = "static-spatial"

    def __init__(
        self,
        *,
        manifest: dict[str, Any] | None = None,
        surfaces: list[dict[str, Any]] | None = None,
        boundary: dict[str, Any] | None = None,
        places: list[dict[str, Any]] | None = None,
    ) -> None:
        self._surfaces = list(surfaces or [])
        self._boundary = boundary
        self._places = list(places or [])
        if manifest is None and self._surfaces:
            manifest = {
                "schema_version": "1.0",
                "surface_set_id": "static",
                "generated_at": self._surfaces[0].get("generated_at"),
                "surfaces": [
                    {
                        "surface_id": item.get("surface_id"),
                        "parameter": item.get("parameter"),
                        "horizon_hours": item.get("horizon_hours"),
                        "target_time": item.get("target_time"),
                        "origin_time": item.get("origin_time"),
                        "object_key": f"static:{index}",
                    }
                    for index, item in enumerate(self._surfaces)
                ],
            }
        self._manifest = manifest

    def ping(self) -> None:
        return None

    def latest_pointer(self) -> dict[str, Any] | None:
        if not self._manifest:
            return None
        return {
            "surface_set_id": self._manifest.get("surface_set_id", "static"),
            "generated_at": self._manifest.get("generated_at"),
            "manifest_key": "static",
        }

    def latest_manifest(self) -> dict[str, Any] | None:
        return self._manifest

    def surface(
        self,
        *,
        parameter: str,
        horizon_hours: int | None = None,
        target_time: datetime | None = None,
        exact_target_time: bool = False,
    ) -> dict[str, Any] | None:
        if not self._surfaces:
            return None
        wanted = parameter.upper().replace("PM25", "PM2.5")
        candidates = [row for row in self._surfaces if str(row.get("parameter", "")).upper() == wanted]
        if not candidates:
            return None
        target = target_time.astimezone(UTC) if target_time else None
        if target is not None and exact_target_time:
            candidates = [
                row
                for row in candidates
                if row.get("target_time")
                and abs((_aware(row["target_time"]) - target).total_seconds()) < 1.0
            ]
            if not candidates:
                return None
        return min(
            candidates,
            key=lambda row: (
                abs((_aware(row["target_time"]) - target).total_seconds())
                if target is not None and row.get("target_time")
                else 0.0,
                abs(int(row.get("horizon_hours", 0)) - int(horizon_hours))
                if horizon_hours is not None
                else 0,
            ),
        )

    def surface_from_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        object_key = str(entry.get("object_key") or "")
        if object_key.startswith("static:"):
            try:
                return self._surfaces[int(object_key.split(":", maxsplit=1)[1])]
            except (ValueError, IndexError):
                return None
        surface_id = entry.get("surface_id")
        for surface in self._surfaces:
            if surface_id and surface.get("surface_id") == surface_id:
                return surface
            if (
                str(surface.get("parameter")) == str(entry.get("parameter"))
                and int(surface.get("horizon_hours", 0))
                == int(entry.get("horizon_hours", 0))
                and str(surface.get("target_time")) == str(entry.get("target_time"))
            ):
                return surface
        return None

    def boundary(self) -> dict[str, Any] | None:
        return self._boundary

    def places(self) -> list[dict[str, Any]]:
        return self._places
