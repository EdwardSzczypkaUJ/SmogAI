from __future__ import annotations

from pathlib import Path


def _script() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "Run-Gios-PM25-Only.ps1"
    ).read_text(encoding="utf-8-sig")


def test_pm25_script_is_parser_safe_for_variable_followed_by_colon() -> None:
    script = _script()
    assert "$Year:" not in script
    assert "$year:" not in script
    assert "${Year}:" in script


def test_pm25_script_exposes_cache_bridge_modes() -> None:
    script = _script()
    assert "ValidateSet('local', 'object_store', 'hybrid')" in script
    assert "'--cache-mode', $CacheMode" in script
    assert "'--cache-prefix', $CachePrefix" in script


def test_data_flow_configuration_script_exposes_bridge_modes() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "Set-DataFlowMode.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "direct_local" in script
    assert "object_store_roundtrip" in script
    assert "SMOG_AI_DATA_FLOW_MODE" in script
    assert "SMOG_AI_GIOS_HISTORY_CACHE_MODE" in script
    assert "$Name:" not in script
