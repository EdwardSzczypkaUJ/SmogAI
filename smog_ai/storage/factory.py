from __future__ import annotations

import os
from typing import Any

from smog_ai.storage.base import ObjectStore
from smog_ai.storage.local import LocalObjectStore, MemoryObjectStore


def create_object_store(config: Any) -> ObjectStore:
    """Create an implementation from any config object exposing storage fields."""
    backend = str(getattr(config, "backend", "local")).strip().lower()
    if backend == "local":
        return LocalObjectStore(getattr(config, "local_root"))
    if backend == "memory":
        return MemoryObjectStore()
    if backend in {"s3", "spaces"}:
        from smog_ai.storage.s3 import S3ObjectStore

        access_key = os.getenv(str(getattr(config, "access_key_env", "SPACES_ACCESS_KEY_ID")))
        secret_key = os.getenv(str(getattr(config, "secret_key_env", "SPACES_SECRET_ACCESS_KEY")))
        session_token_env = getattr(config, "session_token_env", None)
        session_token = os.getenv(str(session_token_env)) if session_token_env else None
        if not access_key or not secret_key:
            raise RuntimeError(
                "Object store credentials are missing. Set the environment variables "
                f"{getattr(config, 'access_key_env', 'SPACES_ACCESS_KEY_ID')} and "
                f"{getattr(config, 'secret_key_env', 'SPACES_SECRET_ACCESS_KEY')}."
            )
        return S3ObjectStore(
            bucket=str(getattr(config, "bucket", "") or ""),
            endpoint_url=getattr(config, "endpoint_url", None),
            region=getattr(config, "region", None),
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            prefix=str(getattr(config, "prefix", "") or ""),
            verify_tls=bool(getattr(config, "verify_tls", True)),
            addressing_style=str(getattr(config, "addressing_style", "virtual")),
            connect_timeout_seconds=float(getattr(config, "connect_timeout_seconds", 10.0)),
            read_timeout_seconds=float(getattr(config, "read_timeout_seconds", 60.0)),
            max_attempts=int(getattr(config, "max_attempts", 5)),
        )
    raise ValueError(f"Unsupported object-store backend: {backend}")
