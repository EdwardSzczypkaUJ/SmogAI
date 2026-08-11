from __future__ import annotations

import logging
import os
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Observation(Protocol):
    trace_id: str

    def update(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        ...


class ObservabilityBridge(Protocol):
    backend_name: str

    def observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[Observation]:
        ...

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def flush(self) -> None:
        ...


@dataclass(slots=True)
class _NoopObservation:
    trace_id: str

    def update(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        return None


class _NoopContext(AbstractContextManager[_NoopObservation]):
    def __init__(self) -> None:
        self.observation = _NoopObservation(uuid.uuid4().hex)

    def __enter__(self) -> _NoopObservation:
        return self.observation

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class NoopObservability:
    backend_name = "none"

    def observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[Observation]:
        return _NoopContext()

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del trace_id, name, value, comment, metadata
        return {"backend": self.backend_name, "submitted": False}

    def flush(self) -> None:
        return None


class _LangfuseObservation:
    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.trace_id = str(getattr(raw, "trace_id", None) or getattr(raw, "id", None) or uuid.uuid4().hex)

    def update(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = output
        if metadata is not None:
            kwargs["metadata"] = metadata
        if level is not None:
            kwargs["level"] = level
        if status_message is not None:
            kwargs["status_message"] = status_message
        if kwargs:
            self.raw.update(**kwargs)


class _LangfuseContext(AbstractContextManager[_LangfuseObservation]):
    def __init__(self, raw_context: Any) -> None:
        self.raw_context = raw_context
        self.observation_value: _LangfuseObservation | None = None

    def __enter__(self) -> _LangfuseObservation:
        self.observation_value = _LangfuseObservation(self.raw_context.__enter__())
        return self.observation_value

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return bool(self.raw_context.__exit__(exc_type, exc, traceback))


class LangfuseObservability:
    backend_name = "langfuse"

    def __init__(self, *, environment: str, release: str) -> None:
        try:
            from langfuse import get_client
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise RuntimeError("Install the 'observability' extra to use Langfuse") from exc
        self.client = get_client()
        self.environment = environment
        self.release = release

    def observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[Observation]:
        merged = {
            "environment": self.environment,
            "release": self.release,
            **(metadata or {}),
        }
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": as_type,
            "input": input,
            "metadata": merged,
        }
        if model is not None:
            kwargs["model"] = model
        raw = self.client.start_as_current_observation(**kwargs)
        return _LangfuseContext(raw)

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Langfuse Python SDK v4 queues scores asynchronously and may return
        # ``None`` from create_score().  Generate the idempotency key locally
        # so the API can always return a useful identifier and retries cannot
        # create duplicate scores.
        score_id = uuid.uuid4().hex
        numeric_value = float(value)
        payload = {
            "trace_id": trace_id,
            "name": name,
            "value": numeric_value,
            "score_id": score_id,
            "data_type": "NUMERIC",
        }
        if comment:
            payload["comment"] = comment
        if metadata:
            payload["metadata"] = metadata
        try:
            create_score = getattr(self.client, "create_score", None)
            if callable(create_score):
                result = create_score(**payload)
            else:
                score_current_trace = getattr(
                    self.client, "score_current_trace", None
                )
                if not callable(score_current_trace):
                    raise RuntimeError("Installed Langfuse SDK has no score API")
                # The fallback only works when called inside the current trace.
                result = score_current_trace(
                    name=name,
                    value=numeric_value,
                    comment=comment,
                    metadata=metadata,
                )
            return {
                "backend": self.backend_name,
                "submitted": True,
                "score_id": str(getattr(result, "id", "") or score_id),
                "data_type": "NUMERIC",
                "value": numeric_value,
            }
        except Exception:
            logger.warning("Langfuse score submission failed", exc_info=True)
            return {
                "backend": self.backend_name,
                "submitted": False,
                "error": "langfuse_score_failed",
            }

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)


def create_observability(
    *,
    backend: str,
    environment: str,
    release: str,
    strict: bool = False,
) -> ObservabilityBridge:
    normalized = backend.strip().lower()
    if normalized in {"", "none", "noop"}:
        return NoopObservability()
    if normalized == "langfuse":
        credentials_present = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
        if not credentials_present:
            if strict:
                raise RuntimeError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
            logger.warning("Langfuse credentials are missing; observability falls back to no-op")
            return NoopObservability()
        try:
            return LangfuseObservability(environment=environment, release=release)
        except Exception:
            if strict:
                raise
            logger.warning("Langfuse initialization failed; observability falls back to no-op", exc_info=True)
            return NoopObservability()
    raise ValueError(f"Unsupported observability backend: {backend}")
