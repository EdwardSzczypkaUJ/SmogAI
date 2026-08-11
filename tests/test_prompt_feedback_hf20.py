from __future__ import annotations

from smog_ai.observability.feedback import (
    LocalPromptFeedbackStore,
    PromptFeedbackRecord,
)


def test_local_prompt_feedback_store_is_append_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = LocalPromptFeedbackStore(tmp_path / "feedback.jsonl")
    first = store.append(
        PromptFeedbackRecord(
            trace_id="trace-1",
            request_id="request-1",
            score=0.8,
            label="4/5",
        )
    )
    store.append(
        PromptFeedbackRecord(
            trace_id="trace-2",
            request_id="request-2",
            score=1.0,
            label="5/5",
        )
    )

    summary = store.summary()
    assert first["status"] == "ok"
    assert summary["count"] == 2
    assert summary["average_score"] == 0.9
