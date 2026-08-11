"""Pluggable object-storage implementations used through a Bridge abstraction."""

from smog_ai.storage.base import ObjectInfo, ObjectStore
from smog_ai.storage.factory import create_object_store
from smog_ai.storage.local import LocalObjectStore, MemoryObjectStore

__all__ = [
    "LocalObjectStore",
    "MemoryObjectStore",
    "ObjectInfo",
    "ObjectStore",
    "create_object_store",
]
