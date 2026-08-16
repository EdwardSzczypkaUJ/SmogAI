from pathlib import Path


def test_schedule_is_hidden_and_has_orphan_repair() -> None:
    root = Path(__file__).parents[1]
    schedule = (root / "scripts" / "Install-SmogAI-RefreshSchedule.ps1").read_text(
        encoding="utf-8-sig"
    )
    repair = (root / "scripts" / "Repair-SmogAI-OrphanedRun.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "'-WindowStyle', 'Hidden'" in schedule
    assert "MultipleInstances IgnoreNew" in schedule
    assert "Set-StateProperty $State 'status' 'interrupted'" in repair
    assert "Get-CimInstance Win32_Process" in repair
