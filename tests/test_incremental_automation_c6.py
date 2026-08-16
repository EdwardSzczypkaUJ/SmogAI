from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_automation_uses_incremental_chain_in_order() -> None:
    source = (ROOT / "scripts" / "smog_ai_automation.py").read_text(
        encoding="utf-8-sig"
    )
    commands = (
        '"training-delta-plan"',
        '"training-delta-build"',
        '"training-delta-preflight"',
        '"snapshot-train-hourly"',
    )
    positions = [source.index(command) for command in commands]
    assert positions == sorted(positions)
    assert '("--profile", training_profile, "--snapshot", "layered"' in source
    assert "model_classifications" in source


def test_automatic_activation_is_approved_only() -> None:
    source = (ROOT / "smog_ai" / "hourly" / "trainer.py").read_text(
        encoding="utf-8-sig"
    )
    assert 'quality["status"] == "approved"' in source
    assert "save local candidate model" in source


def test_compaction_limit_blocks_before_build() -> None:
    source = (ROOT / "smog_ai" / "cli.py").read_text(encoding="utf-8-sig")
    assert '@app.command("training-delta-plan")' in source
    assert 'if payload.get("compaction_due")' in source
    assert '@app.command("training-delta-preflight")' in source
