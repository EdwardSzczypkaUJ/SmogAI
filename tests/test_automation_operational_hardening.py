from __future__ import annotations

import importlib.util
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = ROOT / "scripts" / "smog_ai_automation.py"


def _load_automation():
    spec = importlib.util.spec_from_file_location("smog_ai_automation_hardening", AUTOMATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_user_facing_timestamps_are_polish_civil_time() -> None:
    module = _load_automation()
    summer = module.display_timestamp(datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat())
    winter = module.display_timestamp(datetime(2026, 1, 13, 12, 0, tzinfo=UTC).isoformat())
    assert summer.endswith("+02:00")
    assert "T14:00:00" in summer
    assert winter.endswith("+01:00")
    assert "T13:00:00" in winter


def test_automation_checks_mlflow_before_work_and_training() -> None:
    source = AUTOMATION.read_text(encoding="utf-8-sig")
    assert 'self.check_mlflow_ready("Preflight przed rozpoczęciem automatu")' in source
    assert 'stage.command == "snapshot-train-hourly"' in source
    assert '"--timeout-seconds", "2.0"' in source
    assert "Raport HTML:" in source
    assert "Raport częściowy HTML:" in source


def test_scheduled_children_force_utf8_and_training_accepts_partial_exit() -> None:
    module = _load_automation()
    train = next(
        stage for stage in module.stages_for("normal")
        if stage.command == "snapshot-train-hourly"
    )
    assert train.accepted_exit_codes == (0, 4)

    source = AUTOMATION.read_text(encoding="utf-8-sig")
    assert 'child_env["PYTHONUTF8"] = "1"' in source
    assert 'child_env["PYTHONIOENCODING"] = "utf-8:backslashreplace"' in source
    assert 'final_status = "partial_success"' in source


def test_resume_preserves_partial_training_checkpoint() -> None:
    source = AUTOMATION.read_text(encoding="utf-8-sig")
    assert source.count('{"success", "warning", "partial_success"}') >= 2


def test_partial_training_counts_only_durable_selected_models() -> None:
    module = _load_automation()
    payload = {
        "details": {
            "completed_models": [
                {
                    "target": "PM10",
                    "provider": "ridge",
                    "status": "candidate_trained",
                    "score": 1.2,
                },
                {
                    "target": "PM10",
                    "provider": "ridge",
                    "status": "success",
                    "selected": True,
                    "model_version": "2026.08.14-test",
                },
                {
                    "target": "PM2.5",
                    "provider": "mlp",
                    "status": "candidate_failed",
                    "error": "test failure",
                },
            ]
        }
    }
    successful, failed = module.split_training_results(payload)
    assert [item["target"] for item in successful] == ["PM10"]
    assert [item["target"] for item in failed] == ["PM2.5"]


def test_all_failed_training_does_not_claim_saved_artifacts() -> None:
    module = _load_automation()
    payload = {
        "details": {
            "completed_models": [
                {
                    "target": "temperature_c",
                    "provider": "persistence",
                    "status": "candidate_failed",
                    "error": "UnicodeEncodeError",
                }
            ]
        }
    }
    successful, failed = module.split_training_results(payload)
    assert successful == []
    assert len(failed) == 1


def test_all_experimental_wildcard_is_not_forwarded_to_windows_child() -> None:
    module = _load_automation()
    wildcard_stages = module.stages_for(
        "normal",
        experimental_targets="*",
    )
    wildcard_audit = next(
        stage for stage in wildcard_stages
        if stage.command == "audit-hourly-serving-contract"
    )
    assert wildcard_audit.args == ()

    selected_stages = module.stages_for(
        "normal",
        experimental_targets="precipitation_mm,precipitation_probability",
    )
    selected_audit = next(
        stage for stage in selected_stages
        if stage.command == "audit-hourly-serving-contract"
    )
    assert selected_audit.args == (
        "--allow-experimental-targets",
        "precipitation_mm,precipitation_probability",
    )


def test_monitor_uses_warsaw_time_and_coloured_training_status() -> None:
    source = (ROOT / "scripts" / "smog_ai_automation_monitor.py").read_text(
        encoding="utf-8-sig"
    )
    assert 'DISPLAY_TIMEZONE = "Europe/Warsaw"' in source
    assert ".dt.tz_convert(DISPLAY_TIMEZONE)" in source
    assert "Aktualny trening:" in source
    assert "status_color" in source


def test_monitor_has_live_elapsed_eta_and_history_row_report_actions() -> None:
    source = (ROOT / "scripts" / "smog_ai_automation_monitor.py").read_text(
        encoding="utf-8-sig"
    )
    assert "def elapsed_for_run(" in source
    assert "def elapsed_for_stage(" in source
    assert "def historical_eta(" in source
    assert 'c5.metric("Czas trwania"' in source
    assert 'c6.metric("ETA całości"' in source
    assert "def render_history_rows(" in source
    assert '"Otwórz"' in source
    assert '"Pobierz"' in source
    assert 'status = "nieukończony"' in source
    assert '"eksperymentalny"' in source


def test_streamlit_width_api_and_history_full_cell_status() -> None:
    monitor = (ROOT / "scripts" / "smog_ai_automation_monitor.py").read_text(
        encoding="utf-8-sig"
    )
    dashboard = (ROOT / "server" / "dashboard" / "app.py").read_text(
        encoding="utf-8-sig"
    )
    assert "use_container_width" not in monitor
    assert "use_container_width" not in dashboard
    assert 'width="stretch"' in monitor
    assert 'width="stretch"' in dashboard
    assert "def status_palette(" in monitor
    assert "def history_status_cell(" in monitor
    assert "smog-history-status-cell" in monitor
    assert "status_badge(" not in monitor


def test_cleanup_removes_read_only_training_snapshot(tmp_path) -> None:
    module = _load_automation()
    runtime = tmp_path / "runtime"
    training = runtime / "training-datasets" / "quick"
    training.mkdir(parents=True)
    snapshots = []
    for index in range(3):
        snapshot = training / f"dataset-{index}"
        snapshot.mkdir()
        database = snapshot / "smog.db"
        database.write_bytes(b"sqlite-placeholder")
        (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
        timestamp = 1000 + index
        os.utime(snapshot, (timestamp, timestamp))
        snapshots.append(snapshot)
    oldest_database = snapshots[0] / "smog.db"
    oldest_database.chmod(stat.S_IREAD)

    report = module.cleanup_runtime(
        runtime,
        apply=True,
        policy={"training_quick": 2},
    )

    assert report["errors"] == []
    assert report["deleted_count"] == 1
    assert not snapshots[0].exists()
    assert snapshots[1].exists()
    assert snapshots[2].exists()
