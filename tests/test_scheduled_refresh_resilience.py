from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_scheduled_refresh_starts_mlflow_and_writes_independent_log() -> None:
    source = (ROOT / "scripts" / "Invoke-SmogAI-ScheduledRefresh.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "Test-MlflowReady" in source
    assert "Start-Process" in source
    assert "scheduled-refresh-$Stamp.log" in source
    assert "MLFLOW_READY" in source
    assert "$ErrorActionPreference = 'Continue'" in source
    assert "$Code = $LASTEXITCODE" in source


def test_automation_telemetry_lock_does_not_abort_primary_work() -> None:
    source = (ROOT / "scripts" / "smog_ai_automation.py").read_text(
        encoding="utf-8-sig"
    )
    assert "primary work continues" in source
    assert "uuid.uuid4().hex" in source
    assert "return False" in source
    warning_start = source.index('f"WARNING: telemetry pointer locked:')
    warning_end = source.index("return False", warning_start)
    warning_branch = source[warning_start:warning_end]
    assert "file=sys.stderr" not in warning_branch
