from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


def normalize_key(value: str) -> str:
    value = value.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def normalize_name(value: str) -> str:
    value = value.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def get_alias(mapping: Mapping[str, Any], *aliases: str, default: Any = None) -> Any:
    normalized = {normalize_key(str(key)): value for key, value in mapping.items()}
    for alias in aliases:
        key = normalize_key(alias)
        if key in normalized:
            return normalized[key]
    return default


def first_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def find_collection(payload: Any, preferred_keys: Iterable[str] = ()) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    normalized = {normalize_key(str(key)): value for key, value in payload.items()}
    for key in preferred_keys:
        candidate = normalized.get(normalize_key(key))
        if isinstance(candidate, list):
            return candidate
    for key in (
        "content",
        "items",
        "data",
        "values",
        "results",
        "list",
        "lista",
        "lista stacji pomiarowych",
        "lista stanowisk pomiarowych",
        "lista danych pomiarowych",
    ):
        candidate = normalized.get(normalize_key(key))
        if isinstance(candidate, list):
            return candidate
    list_values = [value for value in payload.values() if isinstance(value, list)]
    if len(list_values) == 1:
        return list_values[0]
    # JSON-LD often nests the actual response one level deeper.
    for value in payload.values():
        if isinstance(value, Mapping):
            nested = find_collection(value, preferred_keys)
            if nested:
                return nested
    return []


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
