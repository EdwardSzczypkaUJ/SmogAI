from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pandas as pd
import pytest
import yaml

from smog_ai.config import DataValidationConfig, project_root
from smog_ai.data_validation.contracts import DataFrameContractError, PanderaFrameValidator, validate_frame

ROOT = Path(__file__).resolve().parents[1]


def test_project_root_defaults_to_checkout_and_supports_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SMOG_AI_PROJECT_ROOT", raising=False)
    assert project_root() == ROOT
    custom = tmp_path / "custom checkout with spaces"
    custom.mkdir()
    monkeypatch.setenv("SMOG_AI_PROJECT_ROOT", str(custom))
    assert project_root() == custom.resolve()


def test_customer_config_enforces_spaces_object_store_training_and_pandera() -> None:
    payload = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert payload["object_storage"]["backend"] == "spaces"
    assert payload["training"]["input_source"] == "object_store"
    assert payload["training"]["allow_database_fallback"] is False
    assert payload["publication"]["transport"] == "object_store"
    assert payload["data_validation"]["require_pandera"] is True


def test_missing_pandera_is_blocking_when_required(app_config, monkeypatch) -> None:
    app_config.data_validation.require_pandera = True
    app_config.data_validation.training_policy = "fail"
    monkeypatch.setattr(PanderaFrameValidator, "available", staticmethod(lambda: False))
    frame = pd.DataFrame(
        {
            "air_station_id": [1],
            "measurement_time": ["2026-07-30T10:00:00Z"],
            "value": [20.0],
            "latitude": [50.0],
            "longitude": [19.0],
            "target": [21.0],
            "target_time": ["2026-07-30T16:00:00Z"],
            "pm_lag_1": [19.0],
        }
    )
    with pytest.raises(DataFrameContractError) as exc:
        validate_frame(frame, "training_frame", app_config)
    assert exc.value.result.engine == "manual-fallback"
    assert exc.value.result.report_path


def test_declared_runtime_dependencies_include_pandera_spaces_streamlit_and_langfuse() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "pandera[pandas]" in requirements
    assert "boto3" in requirements
    assert "streamlit" in requirements
    assert "langfuse" in requirements


def test_no_fixed_checkout_path_in_executable_powershell() -> None:
    forbidden = (r"C:\SmogAI", "C:/SmogAI")
    for path in (ROOT / "scripts").glob("*.ps1"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path.name} contains fixed checkout path {marker}"


def test_windows_mutex_name_uses_deterministic_checkout_hash() -> None:
    task_runner = (ROOT / "scripts" / "Invoke-SmogAiTask.ps1").read_text(encoding="utf-8")
    assert "GetHashCode()" not in task_runner
    assert "System.Security.Cryptography.SHA256" in task_runner
    assert "ComputeHash" in task_runner



def test_powershell_scripts_are_windows_powershell_51_safe() -> None:
    scripts = sorted((ROOT / "scripts").glob("*.ps1"))
    assert scripts
    for path in scripts:
        payload = path.read_bytes()
        assert payload.startswith(b"\xef\xbb\xbf"), f"{path.name} must use UTF-8 BOM"
        payload.decode("utf-8-sig", errors="strict")
        body = payload[3:]
        assert b"\n" not in body.replace(b"\r\n", b""), (
            f"{path.name} must use CRLF line endings"
        )


def test_windows_installer_accepts_python_312_and_313_and_bootstraps_automatically() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.14"

    common = (ROOT / "scripts" / "SmogAi.Common.ps1").read_text(encoding="utf-8-sig")
    installer = (ROOT / "scripts" / "Install-Local.ps1").read_text(encoding="utf-8-sig")
    setup = (ROOT / "scripts" / "Setup-All.ps1").read_text(encoding="utf-8-sig")

    assert "Find-SmogAiPython312" not in common
    assert "Find-SmogAiBootstrapPython" in common
    assert "Test-SmogAiSupportedPythonVersion" in common
    assert "Python.Python.3.12" in common or '"Python.Python.$VersionToInstall"' in common
    assert "winget" in common.lower()
    assert "3.13" in common
    assert "PreferredPythonVersion" in installer
    assert "NoAutomaticPythonInstall" in installer
    assert "RecreateVenv" in installer
    assert "$VenvPath.backup-" in installer
    assert "PythonExecutable" in setup
    assert (ROOT / "scripts" / "Prepare-Python.ps1").is_file()
    assert (ROOT / "docs" / "PYTHON_BOOTSTRAP_WINDOWS.md").is_file()


def test_python_detector_is_conda_first_and_avoids_multiline_dash_c_probe() -> None:
    common = (ROOT / "scripts" / "SmogAi.Common.ps1").read_text(encoding="utf-8-sig")

    assert "Invoke-SmogAiPythonProbe" in common
    assert "smog-ai-python-probe-" in common
    assert "@PrefixArguments $ProbePath" in common
    assert "-c $Probe" not in common
    assert "active Conda:" in common
    assert "py.exe inventory:" in common
    assert "where.exe:" in common
    assert "HKCU:\\Software\\Python\\PythonCore" in common
    assert "Search-SmogAiBootstrapPython" in common
    assert "-1978335189" in common
    assert "No applicable update" not in common  # komunikat jest po polsku, kod pozostaje jawny
    assert (ROOT / "scripts" / "Diagnose-Python.ps1").is_file()
