from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PromptFeedbackRecord:
    trace_id: str
    request_id: str | None
    score: float
    label: str | None = None
    comment: str | None = None
    question: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    feedback_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at_utc: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class LocalPromptFeedbackStore:
    """Append-only local fallback used when Langfuse is disabled/unavailable."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: PromptFeedbackRecord) -> dict[str, Any]:
        payload = asdict(record)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
        return {
            "status": "ok",
            "feedback_id": record.feedback_id,
            "local_path": str(self.path),
        }

    def summary(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"count": 0, "average_score": None, "path": str(self.path)}
        count = 0
        total = 0.0
        with self.path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                    total += float(row["score"])
                    count += 1
                except Exception:
                    continue
        return {
            "count": count,
            "average_score": total / count if count else None,
            "path": str(self.path),
        }
