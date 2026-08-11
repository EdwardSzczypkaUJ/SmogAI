from __future__ import annotations

import runpy
from pathlib import Path

from scripts.validate_digitalocean_spec import validate

ROOT = Path(__file__).resolve().parents[1]


def test_digitalocean_contract_remains_model_free_and_storage_read_only() -> None:
    production = validate(ROOT / ".do" / "app.yaml")
    development = validate(
        ROOT / ".do" / "app.dev.yaml",
        allow_development=True,
    )

    for payload in (production, development):
        assert payload["status"] == "ok"
        assert payload["database_components"] == 0
        assert payload["jobs"] == 0
        assert payload["app_platform_computation"] == "read_and_exact_point_interpolate"
        assert payload["canonical_storage"] == "DigitalOcean Spaces"


def test_stage2_wrappers_use_snapshot_training() -> None:
    quick = (ROOT / "scripts" / "Run-QuickRetrain.ps1").read_text(
        encoding="utf-8-sig"
    )
    full = (ROOT / "scripts" / "Run-FullRetrain.ps1").read_text(
        encoding="utf-8-sig"
    )
    parameter = (
        ROOT / "scripts" / "Run-AirParameterTraining.ps1"
    ).read_text(encoding="utf-8-sig")

    for text in (quick, full, parameter):
        assert "snapshot-train-hourly" in text
    assert "backfill-gios-history" not in quick
    assert "backfill-gios-history" not in full


def test_stage2_stage3_operational_files_are_present() -> None:
    required = (
        "smog_ai/training_snapshot.py",
        "scripts/Run-Stage2-PM25-Pilot.ps1",
        "scripts/Show-TrainingSnapshots.ps1",
        "scripts/Test-Stage2Stage3Readiness.ps1",
        "scripts/stage2_stage3_preflight.py",
        "scripts/Test-Stage2ModelQuality.ps1",
        "scripts/stage2_model_quality_gate.py",
        ".github/workflows/ci-deploy-digitalocean.yml",
        ".do/app.yaml",
        ".do/app.dev.yaml",
    )
    missing = [relative for relative in required if not (ROOT / relative).is_file()]
    assert missing == []


def test_stage2_pilot_enforces_quality_gate() -> None:
    pilot = (ROOT / "scripts" / "Run-Stage2-PM25-Pilot.ps1").read_text(
        encoding="utf-8-sig"
    )
    gate = (ROOT / "scripts" / "stage2_model_quality_gate.py").read_text(
        encoding="utf-8"
    )
    assert "Test-Stage2ModelQuality.ps1" in pilot
    assert "persistence_model_forbidden" in gate
    assert "immutable_dataset_provenance_required" in gate


def test_stage2_stage3_preflight_module_imports_successfully() -> None:
    namespace = runpy.run_path(
        str(ROOT / "scripts" / "stage2_stage3_preflight.py"),
        run_name="smog_ai_stage2_stage3_preflight_test",
    )
    error_type = namespace["ObjectNotFoundError"]
    assert error_type.__module__ == "smog_ai.storage.base"
    source = (
        ROOT / "scripts" / "stage2_stage3_preflight.py"
    ).read_text(encoding="utf-8")
    assert "from smog_ai.storage.base import ObjectNotFoundError" in source
    assert "from smog_ai.errors import ObjectNotFoundError" not in source
