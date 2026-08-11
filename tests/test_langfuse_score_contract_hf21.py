from __future__ import annotations

from smog_ai.observability.bridge import LangfuseObservability


class _QueuedLangfuseClient:
    def __init__(self) -> None:
        self.payload = None
        self.flush_count = 0

    def create_score(self, **payload):
        self.payload = payload
        return None

    def flush(self) -> None:
        self.flush_count += 1


class _BrokenLangfuseClient:
    def create_score(self, **payload):
        raise RuntimeError("network unavailable")


def _bridge(client) -> LangfuseObservability:
    bridge = object.__new__(LangfuseObservability)
    bridge.client = client
    bridge.environment = "test"
    bridge.release = "hf21-test"
    return bridge


def test_numeric_score_has_client_generated_id_when_sdk_returns_none() -> None:
    client = _QueuedLangfuseClient()
    bridge = _bridge(client)

    result = bridge.score(
        trace_id="trace-123",
        name="answer_quality",
        value=1,
        comment="OK",
        metadata={"request_id": "request-123"},
    )

    assert result["submitted"] is True
    assert result["score_id"]
    assert result["value"] == 1.0
    assert result["data_type"] == "NUMERIC"
    assert client.payload == {
        "trace_id": "trace-123",
        "name": "answer_quality",
        "value": 1.0,
        "score_id": result["score_id"],
        "data_type": "NUMERIC",
        "comment": "OK",
        "metadata": {"request_id": "request-123"},
    }


def test_score_failure_is_reported_without_false_success() -> None:
    result = _bridge(_BrokenLangfuseClient()).score(
        trace_id="trace-123",
        name="answer_quality",
        value=0,
    )

    assert result == {
        "backend": "langfuse",
        "submitted": False,
        "error": "langfuse_score_failed",
    }
