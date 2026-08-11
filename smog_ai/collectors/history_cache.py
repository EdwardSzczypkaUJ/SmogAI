from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, runtime_checkable

from smog_ai.config import AppConfig
from smog_ai.storage.base import ObjectStore
from smog_ai.storage.factory import create_object_store
from smog_ai.storage.keys import normalize_key

logger = logging.getLogger(__name__)

HistoryCacheMode = Literal["local", "object_store", "hybrid"]


@dataclass(frozen=True, slots=True)
class CacheReadResult:
    data: bytes
    source: str
    key: str
    local_path: Path


@runtime_checkable
class HistoricalDataCacheBridge(Protocol):
    """Bridge for local, ObjectStore and hybrid historical-source caches."""

    mode: HistoryCacheMode

    def read(
        self,
        *,
        local_path: Path,
        key: str,
        refresh: bool = False,
    ) -> CacheReadResult | None:
        ...

    def write(
        self,
        *,
        local_path: Path,
        key: str,
        data: bytes,
        content_type: str,
    ) -> CacheReadResult:
        ...

    def get_or_fetch(
        self,
        *,
        local_path: Path,
        key: str,
        fetch: Callable[[], bytes],
        content_type: str,
        refresh: bool = False,
    ) -> CacheReadResult:
        ...

    def describe(self) -> dict[str, object]:
        ...


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


@dataclass(slots=True)
class LocalHistoricalDataCacheBridge:
    mode: HistoryCacheMode = "local"

    def read(
        self,
        *,
        local_path: Path,
        key: str,
        refresh: bool = False,
    ) -> CacheReadResult | None:
        if refresh or not local_path.exists():
            return None
        return CacheReadResult(
            data=local_path.read_bytes(),
            source="local_cache",
            key=normalize_key(key),
            local_path=local_path,
        )

    def write(
        self,
        *,
        local_path: Path,
        key: str,
        data: bytes,
        content_type: str,
    ) -> CacheReadResult:
        del content_type
        _atomic_write(local_path, data)
        return CacheReadResult(
            data=data,
            source="official_source_to_local_cache",
            key=normalize_key(key),
            local_path=local_path,
        )

    def get_or_fetch(
        self,
        *,
        local_path: Path,
        key: str,
        fetch: Callable[[], bytes],
        content_type: str,
        refresh: bool = False,
    ) -> CacheReadResult:
        cached = self.read(local_path=local_path, key=key, refresh=refresh)
        if cached is not None:
            return cached
        return self.write(
            local_path=local_path,
            key=key,
            data=fetch(),
            content_type=content_type,
        )

    def describe(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "canonical_cache": "local_filesystem",
            "remote_mirror": False,
        }


@dataclass(slots=True)
class ObjectStoreHistoricalDataCacheBridge:
    store: ObjectStore
    prefix: str
    mode: HistoryCacheMode = "object_store"
    prefer_local_first: bool = False
    allow_remote_failure_fallback: bool = False

    def _key(self, key: str) -> str:
        parts = [part for part in (self.prefix, key) if str(part).strip("/")]
        return normalize_key("/".join(str(part).strip("/") for part in parts))

    def read(
        self,
        *,
        local_path: Path,
        key: str,
        refresh: bool = False,
    ) -> CacheReadResult | None:
        normalized = self._key(key)
        if refresh:
            return None

        if self.prefer_local_first and local_path.exists():
            return CacheReadResult(
                data=local_path.read_bytes(),
                source="local_cache",
                key=normalized,
                local_path=local_path,
            )

        try:
            info = self.store.head(normalized)
            if info is not None:
                data = self.store.get_bytes(normalized)
                _atomic_write(local_path, data)
                return CacheReadResult(
                    data=data,
                    source="object_store_cache",
                    key=normalized,
                    local_path=local_path,
                )
        except Exception:
            if not self.allow_remote_failure_fallback:
                raise
            logger.warning(
                "Historical ObjectStore cache read failed; falling back locally",
                exc_info=True,
            )

        if local_path.exists():
            return CacheReadResult(
                data=local_path.read_bytes(),
                source="local_cache_fallback",
                key=normalized,
                local_path=local_path,
            )
        return None

    def write(
        self,
        *,
        local_path: Path,
        key: str,
        data: bytes,
        content_type: str,
    ) -> CacheReadResult:
        normalized = self._key(key)
        _atomic_write(local_path, data)
        try:
            self.store.put_bytes(
                normalized,
                data,
                content_type=content_type,
                metadata={
                    "cache-role": "historical-source",
                    "cache-mode": self.mode,
                },
                immutable=False,
            )
            source = "official_source_to_local_and_object_store"
        except Exception:
            if not self.allow_remote_failure_fallback:
                raise
            logger.warning(
                "Historical ObjectStore cache write failed; local cache retained",
                exc_info=True,
            )
            source = "official_source_to_local_cache_remote_failed"
        return CacheReadResult(
            data=data,
            source=source,
            key=normalized,
            local_path=local_path,
        )

    def get_or_fetch(
        self,
        *,
        local_path: Path,
        key: str,
        fetch: Callable[[], bytes],
        content_type: str,
        refresh: bool = False,
    ) -> CacheReadResult:
        cached = self.read(local_path=local_path, key=key, refresh=refresh)
        if cached is not None:
            return cached
        return self.write(
            local_path=local_path,
            key=key,
            data=fetch(),
            content_type=content_type,
        )

    def describe(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "canonical_cache": (
                "local_then_object_store"
                if self.prefer_local_first
                else "object_store"
            ),
            "remote_mirror": True,
            "object_store_backend": self.store.backend_name,
            "prefix": self.prefix,
            "remote_failure_fallback": self.allow_remote_failure_fallback,
        }


def create_historical_data_cache_bridge(
    config: AppConfig,
    *,
    mode: HistoryCacheMode | None = None,
    prefix: str | None = None,
) -> HistoricalDataCacheBridge:
    selected = mode or config.data_flow.history_cache_mode
    selected_prefix = prefix or config.data_flow.history_cache_prefix

    if selected == "local":
        return LocalHistoricalDataCacheBridge()
    if selected not in {"object_store", "hybrid"}:
        raise ValueError(f"Unsupported historical cache mode: {selected}")
    if not config.object_storage.enabled:
        if selected == "hybrid":
            return LocalHistoricalDataCacheBridge()
        raise RuntimeError(
            "history_cache_mode=object_store requires object_storage.enabled=true"
        )

    store = create_object_store(config.object_storage)
    store.ping()
    return ObjectStoreHistoricalDataCacheBridge(
        store=store,
        prefix=selected_prefix,
        mode=selected,
        prefer_local_first=selected == "hybrid",
        allow_remote_failure_fallback=selected == "hybrid",
    )
