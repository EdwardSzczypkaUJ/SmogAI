from __future__ import annotations

from dataclasses import dataclass

from smog_ai.observability.analytics import aggregate_numeric_scores


@dataclass
class Score:
    id: str
    trace_id: str
    value: float
    timestamp: str
    comment: str | None = None


def test_aggregate_numeric_scores_builds_dashboard_contract() -> None:
    result = aggregate_numeric_scores(
        [
            Score("a", "t1", 1.0, "2026-08-12T10:00:00Z"),
            Score("b", "t2", 0.0, "2026-08-12T11:00:00Z"),
            Score("c", "t3", 1.0, "2026-08-13T10:00:00Z"),
        ]
    )
    assert result["count"] == 3
    assert result["average_score"] == 2 / 3
    assert result["positive_fraction"] == 2 / 3
    assert result["daily"] == [
        {"date": "2026-08-12", "count": 2, "average_score": 0.5},
        {"date": "2026-08-13", "count": 1, "average_score": 1.0},
    ]
    assert result["recent"][0]["trace_id"] == "t3"


def test_aggregate_numeric_scores_ignores_non_numeric_values() -> None:
    result = aggregate_numeric_scores([{"value": None}, {"value": "bad"}])
    assert result["count"] == 0
    assert result["average_score"] is None
