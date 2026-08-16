from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Iterable


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
    return {
        key: getattr(value, key)
        for key in (
            "id", "name", "value", "timestamp", "created_at", "createdAt",
            "trace_id", "traceId", "comment", "data_type", "dataType",
        )
        if getattr(value, key, None) is not None
    }


def aggregate_numeric_scores(rows: Iterable[Any]) -> dict[str, Any]:
    values: list[float] = []
    daily: dict[str, list[float]] = defaultdict(list)
    recent: list[dict[str, Any]] = []
    for raw in rows:
        row = _as_dict(raw)
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        values.append(value)
        timestamp = row.get("timestamp") or row.get("created_at") or row.get("createdAt")
        daily[str(timestamp or "nieznany")[:10]].append(value)
        recent.append({
            "id": row.get("id"),
            "trace_id": row.get("trace_id") or row.get("traceId"),
            "value": value,
            "timestamp": timestamp,
            "comment": row.get("comment"),
        })
    count = len(values)
    return {
        "count": count,
        "average_score": sum(values) / count if count else None,
        "positive_fraction": sum(v >= 0.5 for v in values) / count if count else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "daily": [
            {"date": day, "count": len(day_values),
             "average_score": sum(day_values) / len(day_values)}
            for day, day_values in sorted(daily.items())
        ],
        "recent": recent[-20:][::-1],
    }


def read_langfuse_quality_summary(*, name: str = "answer_quality", limit: int = 100) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    try:
        from langfuse import get_client

        client = get_client()
        response = client.api.scores_v3.get_many_v3(
            name=name,
            data_type="NUMERIC",
            fields="details,subject",
            limit=max(1, min(int(limit), 100)),
        )
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        return {
            "status": "ok", "backend": "langfuse", "score_name": name,
            "generated_at": generated_at, **aggregate_numeric_scores(data or []),
        }
    except Exception as exc:
        return {
            "status": "unavailable", "backend": "langfuse", "score_name": name,
            "generated_at": generated_at, "error": f"{type(exc).__name__}: {exc}",
            **aggregate_numeric_scores([]),
        }
