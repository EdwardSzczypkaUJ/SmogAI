from __future__ import annotations

import re
from pathlib import PurePosixPath


_INVALID_KEY_PART = re.compile(r"[^A-Za-z0-9._=@+-]+")


def normalize_key(key: str) -> str:
    """Return a safe, relative, POSIX object key and reject traversal."""
    raw = key.replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Invalid object key: {key!r}")
    return "/".join(path.parts)


def sanitize_component(value: str) -> str:
    cleaned = _INVALID_KEY_PART.sub("-", value.strip()).strip("-.")
    return cleaned or "unnamed"


def join_key(*parts: str | None) -> str:
    cleaned = [str(part).replace("\\", "/").strip("/") for part in parts if part]
    return normalize_key("/".join(cleaned))
