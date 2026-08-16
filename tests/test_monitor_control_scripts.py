from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_monitor_refresh_interval_is_configurable_and_lightweight_by_default() -> None:
    launcher = (ROOT / "scripts" / "Start-SmogAI-AutomationMonitor.ps1").read_text(
        encoding="utf-8-sig"
    )
    monitor = (ROOT / "scripts" / "smog_ai_automation_monitor.py").read_text(
        encoding="utf-8-sig"
    )
    assert "$RefreshSeconds=30" in launcher
    assert "SMOG_AI_MONITOR_REFRESH_SECONDS" in launcher
    assert 'SMOG_AI_MONITOR_REFRESH_SECONDS", "30"' in monitor
    assert "refresh_interval" in monitor
    assert 'run_every="2s"' not in monitor


def test_monitor_control_scripts_never_stop_serving_or_all_chrome() -> None:
    start = (ROOT / "scripts" / "Start-SmogAI-Monitor.ps1").read_text(
        encoding="utf-8-sig"
    )
    stop = (ROOT / "scripts" / "Stop-SmogAI-Monitor.ps1").read_text(
        encoding="utf-8-sig"
    )
    status = (ROOT / "scripts" / "Get-SmogAI-MonitorStatus.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "-RefreshSeconds" in start
    assert "[switch]$OpenChrome" in start
    assert "--new-tab" in start
    assert "Stop-ScheduledTask" not in start
    assert "Stop-ScheduledTask" not in stop
    assert "Stop-Process -Name chrome" not in stop
    assert "Stop-Process" in stop
    assert "$Connections.OwningProcess" not in stop
    assert "$Connections.OwningProcess" not in status
    assert "$Rows.ProcessId" not in status
    assert "Get-OptionalProperty" in status
    assert "$Run.status" not in status
    assert "FilesModified = $false" in status
