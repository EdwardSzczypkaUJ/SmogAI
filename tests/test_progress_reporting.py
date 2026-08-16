from __future__ import annotations

import json
import time
from pathlib import Path

from smog_ai.progress import (
    ProgressReporter,
    WeightedStageProgress,
    _atomic_write_json,
    format_progress_text,
    read_progress,
)


def test_atomic_progress_write_retries_short_windows_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    target = tmp_path / "progress.json"
    original_replace = Path.replace
    attempts = 0

    def briefly_locked(self: Path, destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "simulated Windows destination lock", str(destination))
        return original_replace(self, destination)

    monkeypatch.setattr(Path, "replace", briefly_locked)
    monkeypatch.setattr("smog_ai.progress.time.sleep", lambda _seconds: None)

    assert _atomic_write_json(target, {"status": "running"}) is True
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "running"
    assert attempts == 3


def test_atomic_progress_write_does_not_abort_work_on_persistent_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    target = tmp_path / "progress.json"
    target.write_text('{"status":"previous"}\n', encoding="utf-8")

    def locked(_self: Path, destination: Path) -> Path:
        raise PermissionError(5, "simulated persistent Windows lock", str(destination))

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr("smog_ai.progress.time.sleep", lambda _seconds: None)

    assert _atomic_write_json(target, {"status": "running"}) is False
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "previous"


def test_progress_reporter_writes_atomic_state_and_eta(tmp_path: Path) -> None:
    reporter = ProgressReporter(
        tmp_path,
        run_type="first-run",
        stage_weights={"collection": 1.0, "training": 9.0},
        stage_default_seconds={"collection": 10.0, "training": 90.0},
        heartbeat_seconds=60.0,
        run_id="test-run",
    ).start(task="start")

    reporter.update("collection", 0.5, task="collect")
    payload = read_progress(tmp_path, "first-run")
    assert payload is not None
    assert payload["status"] == "running"
    assert payload["overall_percent"] == 5.0
    assert payload["current_stage"] == "collection"
    assert payload["eta_seconds"] is not None
    assert Path(reporter.current_path).exists()
    assert Path(reporter.run_path).exists()

    reporter.complete_stage("collection", task="collection done")
    payload = json.loads(reporter.current_path.read_text(encoding="utf-8"))
    assert payload["overall_percent"] == 10.0

    reporter.finish("success")
    final = json.loads(reporter.current_path.read_text(encoding="utf-8"))
    assert final["status"] == "success"
    assert final["overall_percent"] == 100.0
    assert final["finished_at"] is not None


def test_weighted_stage_progress_tracks_tasks_and_failure(tmp_path: Path) -> None:
    reporter = ProgressReporter(
        tmp_path,
        run_type="train-hourly",
        stage_weights={"training": 1.0},
        stage_default_seconds={"training": 100.0},
        heartbeat_seconds=60.0,
        run_id="weighted-run",
    ).start()
    work = WeightedStageProgress(reporter, stage="training", total_weight=10.0)

    with work.task("candidate ridge", 2.0, fallback_seconds=5.0):
        time.sleep(0.01)

    payload = read_progress(tmp_path, "train-hourly")
    assert payload is not None
    assert payload["current_stage_percent"] == 20.0
    assert payload["stage_work"]["training"]["completed_weight"] == 2.0

    try:
        with work.task("candidate failure", 3.0, fallback_seconds=5.0):
            raise RuntimeError("expected")
    except RuntimeError:
        pass

    payload = read_progress(tmp_path, "train-hourly")
    assert payload is not None
    # A failed candidate attempt is completed work and must not freeze progress.
    assert payload["current_stage_percent"] == 50.0
    assert payload["detail"]["task_status"] == "failed"

    work.advance("skip unavailable task", 2.0, status="skipped")
    work.complete()
    reporter.finish("success")
    final = read_progress(tmp_path, "train-hourly")
    assert final is not None
    assert final["overall_percent"] == 100.0


def test_progress_text_contains_overall_stage_task_and_eta() -> None:
    text = format_progress_text(
        {
            "status": "running",
            "overall_percent": 31.25,
            "current_stage": "training",
            "current_stage_percent": 42.0,
            "current_task": "PM10: candidate mlp",
            "elapsed_human": "20m 00s",
            "eta_range_human": "40m – 1h 20m",
            "eta_confidence": "low",
            "stage_work": {
                "training": {
                    "completed_weight": 10.0,
                    "total_weight": 25.0,
                }
            },
        }
    )
    assert "całość=31.25%" in text
    assert "etap=training" in text
    assert "PM10: candidate mlp" in text
    assert "ETA=40m – 1h 20m" in text
