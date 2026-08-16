import importlib.util
import json
import sys
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "smog_ai_automation.py"
spec = importlib.util.spec_from_file_location("automation", MODULE)
automation = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = automation
spec.loader.exec_module(automation)


def test_every_profile_trains_hourly():
    quick = [stage.command for stage in automation.stages_for("quick")]
    normal = [stage.command for stage in automation.stages_for("normal")]
    full = [stage.command for stage in automation.stages_for("full")]
    assert all("snapshot-train-hourly" in commands for commands in (quick, normal, full))
    assert all("build-features" in commands for commands in (quick, normal, full))
    assert "fill-missing-ranges" not in quick + normal + full


def test_full_parameter_process_is_explicit_and_targets_are_forwarded():
    stages = automation.stages_for("full", "PM10,NO2", True)
    assert "fill-missing-ranges" in [stage.command for stage in stages]
    train = next(stage for stage in stages if stage.command == "snapshot-train-hourly")
    assert train.args[-2:] == ("--targets", "PM10,NO2")


def test_download_scope_is_forwarded_to_collect_audit_and_history():
    stages = automation.stages_for("full", parameters="PM10,NO2", data_start="2025-01-01", data_end="2026-01-01", fill_missing_ranges=True)
    gios = next(stage for stage in stages if stage.command == "collect-gios")
    audit = next(stage for stage in stages if stage.command == "data-range-audit")
    fill = next(stage for stage in stages if stage.command == "fill-missing-ranges")
    assert gios.args == ("--parameters", "PM10,NO2")
    assert audit.args == ("--start", "2025-01-01", "--end", "2026-01-01", "--parameters", "PM10,NO2")
    assert fill.args == audit.args


def test_training_window_and_experimental_targets_are_forwarded():
    stages = automation.stages_for(
        "quick",
        targets="PM10,precipitation_mm",
        experimental_targets="precipitation_mm,precipitation_probability",
        training_start="2026-03-01T00:00:00Z",
        training_end="2026-08-01T00:00:00Z",
    )
    snapshot = next(
        stage for stage in stages if stage.command == "snapshot-train-hourly"
    )
    audit = next(
        stage for stage in stages if stage.command == "audit-hourly-serving-contract"
    )
    assert snapshot.args[-4:] == (
        "--training-start",
        "2026-03-01T00:00:00Z",
        "--training-end",
        "2026-08-01T00:00:00Z",
    )
    assert audit.args == (
        "--allow-experimental-targets",
        "precipitation_mm,precipitation_probability",
    )


def test_atomic_json(tmp_path):
    target = tmp_path / "state" / "run.json"
    automation.atomic_json(target, {"status": "ok", "text": "zażółć"})
    assert json.loads(target.read_text(encoding="utf-8"))["text"] == "zażółć"


def test_project_root_defaults_to_current_directory(tmp_path, monkeypatch):
    class Args:
        project_root = None
        runtime_root = str(tmp_path / "runtime")
        config = None
        env_file = None
        profile = "quick"
        targets = None
        fill_missing_ranges = False
        parameters = None
        data_start = None
        data_end = None
        skip_gios_current = False
        skip_imgw_current = False
        max_validation_errors = 100
        resume = False
        run_id = "root-test"
    monkeypatch.chdir(tmp_path)
    runner = automation.Runner(Args())
    assert runner.project == tmp_path.resolve()


def test_publication_check_reads_canonical_local_bridge(tmp_path):
    class Args:
        project_root = str(tmp_path / "project")
        runtime_root = str(tmp_path / "runtime")
        config = None
        env_file = None
        profile = "quick"
        targets = None
        fill_missing_ranges = False
        parameters = None
        data_start = None
        data_end = None
        skip_gios_current = False
        skip_imgw_current = False
        max_validation_errors = 100
        resume = False
        run_id = "test"
    runner = automation.Runner(Args())
    store = Path(Args.runtime_root) / "object-store"
    manifest_key = "serving/releases/r1/manifest.json"
    manifest_path = store / "serving" / "releases" / "r1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({
        "contract": "smog-ai-serving-release",
        "release_id": "r1",
        "surface_set_id": "s1",
        "parameters": ["PM10"],
        "horizons_hours": [1],
        "surfaces": [{"object_key": "surfaces/s1/PM10-h1.json.gz"}],
    }), encoding="utf-8")
    pointer_path = store / "serving" / "latest.json"
    pointer_path.write_text(json.dumps({
        "contract": "smog-ai-serving-pointer",
        "release_id": "r1",
        "manifest_key": manifest_key,
    }), encoding="utf-8")
    runner.state = {}
    runner.publication_check()
    assert runner.state["publication"]["contract"] == "smog-ai-serving-release"
    assert runner.state["publication"]["release_id"] == "r1"
    assert runner.state["publication"]["surface_set_id"] == "s1"
    assert runner.state["publication"]["surface_count"] == 1


def test_resume_without_run_id_finds_latest_failed_checkpoint(tmp_path):
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    runs = runtime / "logs" / "automation" / "runs"
    old = runs / "old" / "run.json"
    failed = runs / "failed-one" / "run.json"
    old.parent.mkdir(parents=True)
    failed.parent.mkdir(parents=True)
    old.write_text('{"run_id":"old","status":"success"}', encoding="utf-8")
    failed.write_text(
        json.dumps({
            "run_id": "failed-one", "status": "failed", "profile": "quick",
            "project_root": str(project), "download_plan": {}, "stages": [],
        }), encoding="utf-8"
    )
    class Args:
        project_root = str(project)
        runtime_root = str(runtime)
        config = None
        env_file = None
        profile = "full"
        targets = None
        fill_missing_ranges = False
        parameters = None
        data_start = None
        data_end = None
        skip_gios_current = False
        skip_imgw_current = False
        max_validation_errors = 100
        resume = True
        run_id = None
    runner = automation.Runner(Args())
    assert runner.run_id == "failed-one"
    assert runner.profile == "quick"
