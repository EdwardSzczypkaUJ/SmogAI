from __future__ import annotations

import os
from typing import Any, Iterable


DERIVED_MODEL_TARGET = {"precipitation_probability": "precipitation_mm"}


def canonical_target(value: str) -> str:
    cleaned = str(value).strip()
    upper = cleaned.upper().replace("PM25", "PM2.5")
    if upper in {"PM10", "PM2.5"}:
        return upper
    return cleaned.lower()


def parse_target_list(value: str | Iterable[str] | None) -> set[str]:
    if value is None:
        return set()
    items = value.split(",") if isinstance(value, str) else value
    return {canonical_target(item) for item in items if str(item).strip()}


def allowed_experimental_targets(value: str | Iterable[str] | None = None) -> set[str]:
    configured = value if value is not None else os.getenv("SMOG_AI_EXPERIMENTAL_TARGETS")
    # Soft quality gates annotate active outputs instead of silently removing
    # them.  Operators can request the restrictive policy explicitly with
    # SMOG_AI_EXPERIMENTAL_TARGETS=none or an explicit allow-list.
    if configured is None or (
        isinstance(configured, str) and not configured.strip()
    ):
        return {"*"}
    if isinstance(configured, str) and configured.strip().lower() in {"all", "*"}:
        return {"*"}
    return parse_target_list(configured)


def model_target(parameter: str) -> str:
    canonical = canonical_target(parameter)
    return DERIVED_MODEL_TARGET.get(canonical, canonical)


def quality_metadata(
    parameter: str,
    metrics: dict[str, Any] | None,
    *,
    allowed: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    payload = dict(metrics or {})
    status = str(payload.get("quality_status") or "approved").lower()
    if status == "accepted":
        status = "approved"
    experimental = status == "experimental"
    permitted = allowed_experimental_targets(allowed)
    canonical = canonical_target(parameter)
    target = model_target(canonical)
    explicitly_allowed = (
        "*" in permitted or canonical in permitted or target in permitted
    )
    gate = dict(
        payload.get("quality_classification")
        or payload.get("precipitation_quality_gate")
        or {}
    )
    return {
        "quality_status": status,
        "experimental": experimental,
        "experimental_publication_allowed": (not experimental) or explicitly_allowed,
        "experimental_reason": gate.get("reasons") or gate.get("failures") or [],
        "model_target": target,
    }
