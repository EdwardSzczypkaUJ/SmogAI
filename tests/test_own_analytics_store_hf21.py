from __future__ import annotations

from datetime import UTC, datetime, timedelta

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.observability.own_store import OwnAnalyticsStore
from smog_ai.storage.local import MemoryObjectStore


def test_private_events_and_public_aggregate_are_separate() -> None:
    repository = ArtifactRepository(MemoryObjectStore())
    analytics = OwnAnalyticsStore(repository)
    saved = analytics.save_interaction({
        "request_id": "request-1", "trace_id": "trace-1", "question": "secret question",
        "intent": {"pollutants": ["PM10", "PM2.5"], "target_time": "2026-08-13T11:27:00+02:00"},
        "time_selection": {"requested_target_time": "2026-08-13T11:27:00+02:00"},
        "place": {"latitude": 50.7969, "longitude": 16.1145},
        "forecasts": [
            {"parameter": "PM10", "target_time": "2026-08-13T09:27:00Z"},
            {"parameter": "PM2.5", "target_time": "2026-08-13T09:27:00Z"},
        ],
        "interpretation": {
            "provider": "openai_compatible", "model": "gpt-4.1-mini",
            "prompt_tokens": 120, "completion_tokens": 35,
        },
        "warnings": [],
        "map_points": [{"latitude": 50.0, "longitude": 19.0, "value": 42.0}],
        "surface_options": [{"surface_id": "large-repeated-catalog"}],
    })
    assert saved["key"].startswith("private/analytics/interactions/")
    assert repository.store.exists(saved["key"])
    stored_interaction = repository.get_gzip_json(saved["key"])
    assert stored_interaction["event_type"] == "forecast_interaction"
    assert stored_interaction["response"]["question"] == "secret question"
    assert stored_interaction["retention_days"] == 90
    assert "map_points" not in stored_interaction["response"]
    assert "surface_options" not in stored_interaction["response"]
    assert stored_interaction["response"]["analytics_omitted_bulk_fields"] == [
        "map_points", "surface_options"
    ]

    feedback = analytics.save_feedback(
        feedback_id="feedback-1", trace_id="trace-1", request_id="request-1",
        score=1.0, label="good", comment=None, question=None,
        components={"location": 1.0, "clarity": 0.8},
    )
    assert feedback["key"].startswith("private/analytics/feedback/")
    stored_feedback = repository.get_gzip_json(feedback["key"])
    assert stored_feedback["trace_id"] == "trace-1"
    assert stored_feedback["retention_days"] == 90
    summary = analytics.summary()
    assert summary["source"] == "own_object_store"
    assert summary["count"] == 1
    assert summary["average_score"] == 1.0
    assert summary["score_buckets"] == {"0.75-1.00": 1}
    assert summary["feedback_labels"] == {"good": 1}
    assert summary["component_scores"]["location"]["average_score"] == 1.0
    assert summary["component_scores"]["clarity"]["average_score"] == 0.8
    assert summary["interactions"]["complete_parameter_answers"] == 1
    assert summary["interactions"]["exact_time_answers"] == 1
    assert summary["interactions"]["prompt_tokens"] == 120
    assert summary["interactions"]["completion_tokens"] == 35
    assert summary["interactions"]["tokenized_interactions"] == 1
    assert "question" not in summary
    assert summary["raw_event_retention_days"] == 90


def test_rebuild_preserves_scores_and_recalculates_aggregate() -> None:
    repository = ArtifactRepository(MemoryObjectStore())
    analytics = OwnAnalyticsStore(repository)
    analytics.save_feedback(
        feedback_id="feedback-1", trace_id="trace-1", request_id=None,
        score=1.0, label="good", comment=None, question=None,
    )
    analytics.save_feedback(
        feedback_id="feedback-2", trace_id="trace-2", request_id=None,
        score=0.25, label="bad", comment=None, question=None,
    )
    repository.put_json(analytics.summary_key, {"count": 999})

    rebuilt = analytics.rebuild_summary()

    assert rebuilt["count"] == 2
    assert rebuilt["average_score"] == 0.625
    assert rebuilt["score_buckets"] == {"0.75-1.00": 1, "0.25-0.49": 1}


def test_raw_event_retention_is_configurable_and_preserves_summary() -> None:
    store = MemoryObjectStore()
    repository = ArtifactRepository(store)
    analytics = OwnAnalyticsStore(repository, retention_days=90)
    saved = analytics.save_interaction({
        "request_id": "old-request",
        "question": "old question",
        "intent": {"pollutants": ["PM10"]},
        "forecasts": [],
    })
    old = datetime(2026, 1, 1, tzinfo=UTC)
    data, content_type, metadata, _ = store._objects[saved["key"]]
    store._objects[saved["key"]] = (data, content_type, metadata, old)

    result = analytics.enforce_retention(
        now=old + timedelta(days=91)
    )

    assert result["retention_days"] == 90
    assert result["deleted"] == 1
    assert not repository.store.exists(saved["key"])
    assert repository.store.exists(analytics.summary_key)
