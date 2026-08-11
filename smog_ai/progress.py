from __future__ import annotations

import json
import logging
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)

FIRST_RUN_STAGE_WEIGHTS: dict[str, float] = {
    "collection": 4.0,
    "training_data": 7.0,
    "training": 64.0,
    "prediction": 5.0,
    "spatial": 17.0,
    "documentation": 1.0,
    "snapshot": 1.0,
    "publication": 1.0,
}

# Conservative first-run defaults.  They are used only until the local machine
# has produced timing history.  After every completed stage the median of the
# last measurements replaces the default for future ETA calculations.
FIRST_RUN_STAGE_DEFAULT_SECONDS: dict[str, float] = {
    "collection": 600.0,
    "training_data": 900.0,
    "training": 7_200.0,
    "prediction": 600.0,
    "spatial": 3_600.0,
    "documentation": 90.0,
    "snapshot": 300.0,
    "publication": 300.0,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _format_seconds(value: float | None) -> str | None:
    if value is None or value < 0 or not float(value) < float("inf"):
        return None
    rounded = int(round(value))
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m"
    if minutes:
        return f"{minutes:d}m {seconds:02d}s"
    return f"{seconds:d}s"


class DurationHistory:
    """Small local timing history used to improve ETA after the first run."""

    def __init__(self, path: Path, *, maximum_samples: int = 20) -> None:
        self.path = path
        self.maximum_samples = maximum_samples
        self._lock = threading.RLock()
        self._data: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(payload, dict):
            return
        for key, values in payload.items():
            if not isinstance(values, list):
                continue
            cleaned = [
                float(value)
                for value in values
                if isinstance(value, (int, float)) and float(value) >= 0
            ]
            if cleaned:
                self._data[str(key)] = cleaned[-self.maximum_samples :]

    def estimate(self, key: str, fallback: float | None = None) -> float | None:
        with self._lock:
            values = self._data.get(key, [])
            if values:
                return float(statistics.median(values))
        return fallback

    def sample_count(self, key: str) -> int:
        with self._lock:
            return len(self._data.get(key, []))

    def record(self, key: str, seconds: float) -> None:
        if seconds < 0 or not seconds < float("inf"):
            return
        with self._lock:
            values = self._data.setdefault(key, [])
            values.append(float(seconds))
            del values[:-self.maximum_samples]
            _atomic_write_json(self.path, self._data)


class ProgressReporter:
    """Durable progress and ETA reporter for long-running local pipelines.

    The reporter writes one atomic current-state JSON file, one run-specific
    state file and a JSONL event stream.  A daemon heartbeat refreshes elapsed
    times and ETA even while a blocking scikit-learn ``fit`` call is running.
    """

    def __init__(
        self,
        logs_dir: Path,
        *,
        run_type: str,
        stage_weights: Mapping[str, float] | None = None,
        stage_default_seconds: Mapping[str, float] | None = None,
        heartbeat_seconds: float = 5.0,
        run_id: str | None = None,
    ) -> None:
        weights = dict(stage_weights or {"work": 1.0})
        if not weights or any(float(value) <= 0 for value in weights.values()):
            raise ValueError("Progress stage weights must be positive")
        self.logs_dir = Path(logs_dir)
        self.progress_dir = self.logs_dir / "progress"
        self.run_type = str(run_type)
        self.run_id = run_id or uuid.uuid4().hex
        self.stage_weights = {str(key): float(value) for key, value in weights.items()}
        self.stage_order = list(self.stage_weights)
        self.stage_defaults = {
            str(key): float(value)
            for key, value in (stage_default_seconds or {}).items()
        }
        self.stage_fractions = {stage: 0.0 for stage in self.stage_order}
        self.stage_started_monotonic: dict[str, float] = {}
        self.stage_started_at: dict[str, datetime] = {}
        self.stage_completed_at: dict[str, datetime] = {}
        self.stage_work: dict[str, dict[str, float]] = {}
        self.current_stage: str | None = None
        self.current_task: str | None = None
        self.current_detail: dict[str, Any] = {}
        self.current_task_started_monotonic: float | None = None
        self.current_task_started_at: datetime | None = None
        self.current_task_expected_seconds: float | None = None
        self.current_task_history_key: str | None = None
        self.status = "created"
        self.error: str | None = None
        self.started_at = _utc_now()
        self.started_monotonic = time.monotonic()
        self.finished_at: datetime | None = None
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._last_logged_percent = -1.0
        self._last_logged_task: str | None = None
        self._history = DurationHistory(self.progress_dir / "duration-history.json")
        self.current_path = self.progress_dir / f"{self.run_type}-current.json"
        self.run_path = self.progress_dir / f"{self.run_type}-{self.run_id}.json"
        self.event_path = self.progress_dir / f"{self.run_type}-{self.run_id}.jsonl"

    def start(self, *, task: str | None = None) -> "ProgressReporter":
        with self._lock:
            if self.status == "running":
                return self
            self.status = "running"
            if task:
                self.current_task = task
                self.current_task_started_monotonic = time.monotonic()
                self.current_task_started_at = _utc_now()
            self._write(force_log=True, event="run_started")
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat,
                name=f"smog-ai-progress-{self.run_type}",
                daemon=True,
            )
            self._heartbeat_thread.start()
        return self

    def _heartbeat(self) -> None:
        while not self._stop_event.wait(self.heartbeat_seconds):
            try:
                with self._lock:
                    if self.status != "running":
                        return
                    self._write(force_log=False, event=None)
            except Exception:  # pragma: no cover - progress must never stop ML
                logger.exception("Progress heartbeat failed")

    def _stage_estimate(self, stage: str) -> tuple[float, int]:
        default = self.stage_defaults.get(stage)
        key = f"stage:{self.run_type}:{stage}"
        estimate = self._history.estimate(key, default)
        return float(estimate or 0.0), self._history.sample_count(key)

    def _overall_fraction(self) -> float:
        total = sum(self.stage_weights.values())
        completed = sum(
            self.stage_weights[stage] * self.stage_fractions.get(stage, 0.0)
            for stage in self.stage_order
        )
        return max(0.0, min(1.0, completed / total))

    def _stage_eta_seconds(self, stage: str, now_monotonic: float) -> float | None:
        fraction = self.stage_fractions.get(stage, 0.0)
        if fraction >= 1.0:
            return 0.0
        work = self.stage_work.get(stage)
        if work:
            completed_weight = work.get("completed_weight", 0.0)
            total_weight = work.get("total_weight", 0.0)
            stage_start = self.stage_started_monotonic.get(stage)
            if stage_start is not None and completed_weight > 0 and total_weight > completed_weight:
                elapsed = max(0.0, now_monotonic - stage_start)
                return elapsed * (total_weight - completed_weight) / completed_weight
        estimate, _ = self._stage_estimate(stage)
        if estimate > 0:
            return estimate * (1.0 - fraction)
        stage_start = self.stage_started_monotonic.get(stage)
        if stage_start is not None and fraction > 0:
            elapsed = max(0.0, now_monotonic - stage_start)
            return elapsed * (1.0 - fraction) / fraction
        return None

    def _remaining_eta_seconds(self, now_monotonic: float) -> tuple[float | None, str]:
        if self.status != "running":
            return 0.0 if self.status == "success" else None, "final"
        current_index = (
            self.stage_order.index(self.current_stage)
            if self.current_stage in self.stage_order
            else 0
        )
        remaining = 0.0
        known = False
        history_samples = 0
        for index, stage in enumerate(self.stage_order):
            fraction = self.stage_fractions.get(stage, 0.0)
            if fraction >= 1.0:
                continue
            if index < current_index:
                continue
            estimate, samples = self._stage_estimate(stage)
            history_samples += samples
            if stage == self.current_stage:
                stage_eta = self._stage_eta_seconds(stage, now_monotonic)
                if stage_eta is not None:
                    remaining += stage_eta
                    known = True
                elif estimate > 0:
                    remaining += estimate * (1.0 - fraction)
                    known = True
            elif estimate > 0:
                remaining += estimate * (1.0 - fraction)
                known = True
        if not known:
            return None, "unknown"
        overall = self._overall_fraction()
        if history_samples >= max(3, len(self.stage_order) // 2) or overall >= 0.70:
            confidence = "high"
        elif history_samples or overall >= 0.25:
            confidence = "medium"
        else:
            confidence = "low"
        return max(0.0, remaining), confidence

    def _snapshot(self) -> dict[str, Any]:
        now = _utc_now()
        now_monotonic = time.monotonic()
        elapsed = max(0.0, now_monotonic - self.started_monotonic)
        overall_fraction = self._overall_fraction()
        eta, confidence = self._remaining_eta_seconds(now_monotonic)
        if confidence == "low" and eta is not None:
            eta_low, eta_high = eta * 0.55, eta * 1.90
        elif confidence == "medium" and eta is not None:
            eta_low, eta_high = eta * 0.75, eta * 1.45
        elif eta is not None:
            eta_low, eta_high = eta * 0.88, eta * 1.18
        else:
            eta_low = eta_high = None
        task_elapsed = (
            max(0.0, now_monotonic - self.current_task_started_monotonic)
            if self.current_task_started_monotonic is not None
            else None
        )
        task_eta = (
            max(0.0, self.current_task_expected_seconds - task_elapsed)
            if self.current_task_expected_seconds is not None and task_elapsed is not None
            else None
        )
        estimated_finish = now + timedelta(seconds=eta) if eta is not None else None
        stage_fraction = (
            self.stage_fractions.get(self.current_stage, 0.0)
            if self.current_stage
            else 0.0
        )
        return {
            "schema_version": "1.0",
            "run_type": self.run_type,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "updated_at": _iso(now),
            "finished_at": _iso(self.finished_at),
            "elapsed_seconds": elapsed,
            "elapsed_human": _format_seconds(elapsed),
            "overall_fraction": overall_fraction,
            "overall_percent": round(overall_fraction * 100.0, 2),
            "current_stage": self.current_stage,
            "current_stage_fraction": stage_fraction,
            "current_stage_percent": round(stage_fraction * 100.0, 2),
            "current_task": self.current_task,
            "current_task_started_at": _iso(self.current_task_started_at),
            "current_task_elapsed_seconds": task_elapsed,
            "current_task_elapsed_human": _format_seconds(task_elapsed),
            "current_task_expected_seconds": self.current_task_expected_seconds,
            "current_task_eta_seconds": task_eta,
            "current_task_eta_human": _format_seconds(task_eta),
            "current_task_history_key": self.current_task_history_key,
            "detail": dict(self.current_detail),
            "stage_fractions": dict(self.stage_fractions),
            "stage_work": {key: dict(value) for key, value in self.stage_work.items()},
            "eta_seconds": eta,
            "eta_human": _format_seconds(eta),
            "eta_low_seconds": eta_low,
            "eta_high_seconds": eta_high,
            "eta_range_human": (
                f"{_format_seconds(eta_low)} – {_format_seconds(eta_high)}"
                if eta_low is not None and eta_high is not None
                else None
            ),
            "eta_confidence": confidence,
            "estimated_finish_at": _iso(estimated_finish),
            "error": self.error,
            "pid": __import__("os").getpid(),
        }

    def _append_event(self, snapshot: Mapping[str, Any], event: str) -> None:
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"event": event, **snapshot}
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _write(self, *, force_log: bool, event: str | None) -> None:
        snapshot = self._snapshot()
        _atomic_write_json(self.current_path, snapshot)
        _atomic_write_json(self.run_path, snapshot)
        if event:
            self._append_event(snapshot, event)
        percent = float(snapshot["overall_percent"])
        task = str(snapshot.get("current_task") or "")
        should_log = (
            force_log
            or task != self._last_logged_task
            or percent >= self._last_logged_percent + 1.0
        )
        if should_log:
            logger.info(
                "PROGRESS overall=%.2f%% stage=%s stage_progress=%.2f%% "
                "task=%s elapsed=%s eta=%s eta_confidence=%s",
                percent,
                snapshot.get("current_stage"),
                float(snapshot.get("current_stage_percent") or 0.0),
                task or None,
                snapshot.get("elapsed_human"),
                snapshot.get("eta_range_human") or snapshot.get("eta_human"),
                snapshot.get("eta_confidence"),
            )
            self._last_logged_percent = percent
            self._last_logged_task = task

    def update(
        self,
        stage: str,
        fraction: float,
        *,
        task: str | None = None,
        detail: Mapping[str, Any] | None = None,
        expected_task_seconds: float | None = None,
        task_history_key: str | None = None,
        completed_weight: float | None = None,
        total_weight: float | None = None,
        force: bool = False,
    ) -> None:
        if stage not in self.stage_weights:
            raise KeyError(f"Unknown progress stage: {stage}")
        with self._lock:
            now_monotonic = time.monotonic()
            now = _utc_now()
            if self.status == "created":
                self.start()
            if stage not in self.stage_started_monotonic:
                self.stage_started_monotonic[stage] = now_monotonic
                self.stage_started_at[stage] = now
            previous_fraction = self.stage_fractions.get(stage, 0.0)
            new_fraction = max(previous_fraction, min(1.0, max(0.0, float(fraction))))
            self.stage_fractions[stage] = new_fraction
            self.current_stage = stage
            if task is not None and task != self.current_task:
                self.current_task = task
                self.current_task_started_monotonic = now_monotonic
                self.current_task_started_at = now
                self.current_task_expected_seconds = expected_task_seconds
                self.current_task_history_key = task_history_key
            elif expected_task_seconds is not None:
                self.current_task_expected_seconds = expected_task_seconds
            if detail is not None:
                self.current_detail = dict(detail)
            if completed_weight is not None and total_weight is not None:
                self.stage_work[stage] = {
                    "completed_weight": float(completed_weight),
                    "total_weight": float(total_weight),
                    "completed_percent": (
                        100.0 * float(completed_weight) / float(total_weight)
                        if float(total_weight) > 0
                        else 0.0
                    ),
                }
            event = "stage_updated"
            if new_fraction >= 1.0 and stage not in self.stage_completed_at:
                self.stage_completed_at[stage] = now
                started = self.stage_started_monotonic.get(stage)
                if started is not None:
                    self._history.record(
                        f"stage:{self.run_type}:{stage}",
                        max(0.0, now_monotonic - started),
                    )
                event = "stage_completed"
            self._write(force_log=force or event == "stage_completed", event=event)

    def complete_stage(
        self,
        stage: str,
        *,
        task: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.update(stage, 1.0, task=task, detail=detail, force=True)

    def finish(self, status: str = "success", *, detail: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self.status = status
            self.finished_at = _utc_now()
            if detail is not None:
                self.current_detail = dict(detail)
            if status == "success":
                for stage in self.stage_order:
                    self.stage_fractions[stage] = 1.0
            self._write(force_log=True, event="run_finished")
            self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)

    def fail(self, error: BaseException | str, *, status: str = "failed") -> None:
        with self._lock:
            self.error = str(error)
        self.finish(status=status, detail={"error": str(error)})

    def record_task_duration(self, key: str, seconds: float) -> None:
        self._history.record(f"task:{key}", seconds)

    def estimate_task_duration(self, key: str, fallback: float | None) -> float | None:
        return self._history.estimate(f"task:{key}", fallback)


class WeightedStageProgress:
    """Translate heterogeneous model tasks into a stable stage percentage."""

    def __init__(
        self,
        reporter: ProgressReporter | None,
        *,
        stage: str,
        total_weight: float,
    ) -> None:
        self.reporter = reporter
        self.stage = stage
        self.total_weight = max(0.000001, float(total_weight))
        self.completed_weight = 0.0
        if reporter is not None:
            reporter.update(
                stage,
                0.0,
                task="initializing",
                completed_weight=0.0,
                total_weight=self.total_weight,
                force=True,
            )

    @property
    def fraction(self) -> float:
        return max(0.0, min(1.0, self.completed_weight / self.total_weight))

    def advance(
        self,
        name: str,
        weight: float,
        *,
        detail: Mapping[str, Any] | None = None,
        status: str = "completed",
    ) -> None:
        self.completed_weight = min(
            self.total_weight,
            self.completed_weight + max(0.0, float(weight)),
        )
        if self.reporter is not None:
            payload = dict(detail or {})
            payload.setdefault("task_status", status)
            self.reporter.update(
                self.stage,
                self.fraction,
                task=name,
                detail=payload,
                completed_weight=self.completed_weight,
                total_weight=self.total_weight,
                force=True,
            )

    @contextmanager
    def task(
        self,
        name: str,
        weight: float,
        *,
        task_key: str | None = None,
        fallback_seconds: float | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        key = task_key or f"{self.stage}:{name}"
        expected = (
            self.reporter.estimate_task_duration(key, fallback_seconds)
            if self.reporter is not None
            else fallback_seconds
        )
        if self.reporter is not None:
            payload = dict(detail or {})
            payload["task_status"] = "running"
            self.reporter.update(
                self.stage,
                self.fraction,
                task=name,
                detail=payload,
                expected_task_seconds=expected,
                task_history_key=key,
                completed_weight=self.completed_weight,
                total_weight=self.total_weight,
                force=True,
            )
        started = time.monotonic()
        try:
            yield
        except BaseException as exc:
            duration = max(0.0, time.monotonic() - started)
            if self.reporter is not None:
                self.reporter.record_task_duration(key, duration)
            # A failed attempt still consumed its planned work.  Advancing the
            # progress prevents one rejected candidate from freezing the total
            # percentage for the rest of the run.
            self.completed_weight = min(
                self.total_weight,
                self.completed_weight + max(0.0, float(weight)),
            )
            if self.reporter is not None:
                payload = dict(detail or {})
                payload.update(
                    {
                        "task_status": "failed",
                        "task_duration_seconds": duration,
                        "error": str(exc),
                    }
                )
                self.reporter.update(
                    self.stage,
                    self.fraction,
                    task=name,
                    detail=payload,
                    completed_weight=self.completed_weight,
                    total_weight=self.total_weight,
                    force=True,
                )
            raise
        else:
            duration = max(0.0, time.monotonic() - started)
            if self.reporter is not None:
                self.reporter.record_task_duration(key, duration)
            self.completed_weight = min(
                self.total_weight,
                self.completed_weight + max(0.0, float(weight)),
            )
            if self.reporter is not None:
                payload = dict(detail or {})
                payload.update(
                    {
                        "task_status": "completed",
                        "task_duration_seconds": duration,
                    }
                )
                self.reporter.update(
                    self.stage,
                    self.fraction,
                    task=f"{name} — completed",
                    detail=payload,
                    completed_weight=self.completed_weight,
                    total_weight=self.total_weight,
                    force=True,
                )

    def complete(self, *, name: str = "stage completed") -> None:
        self.completed_weight = self.total_weight
        if self.reporter is not None:
            self.reporter.complete_stage(
                self.stage,
                task=name,
                detail={
                    "completed_weight": self.completed_weight,
                    "total_weight": self.total_weight,
                },
            )


def read_progress(logs_dir: Path, run_type: str = "first-run") -> dict[str, Any] | None:
    path = Path(logs_dir) / "progress" / f"{run_type}-current.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def format_progress_text(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")
    overall = float(payload.get("overall_percent") or 0.0)
    stage = payload.get("current_stage") or "-"
    stage_percent = float(payload.get("current_stage_percent") or 0.0)
    task = payload.get("current_task") or "-"
    eta = payload.get("eta_range_human") or payload.get("eta_human") or "brak danych"
    confidence = payload.get("eta_confidence") or "unknown"
    elapsed = payload.get("elapsed_human") or "-"
    work = (payload.get("stage_work") or {}).get(stage) or {}
    work_text = ""
    if work:
        work_text = (
            f" | praca {float(work.get('completed_weight', 0.0)):.1f}/"
            f"{float(work.get('total_weight', 0.0)):.1f}"
        )
    return (
        f"status={status} | całość={overall:.2f}% | etap={stage} "
        f"{stage_percent:.2f}%{work_text} | zadanie={task} | "
        f"czas={elapsed} | ETA={eta} ({confidence})"
    )
