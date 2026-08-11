from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "mlflow_preflight.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("hf20_2_mlflow_preflight", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disabled_mlflow_is_clean_branch() -> None:
    module = _load_helper()
    report, code = module.evaluate_mlflow_section(
        SimpleNamespace(enabled=False, strict=True, tracking_uri="http://127.0.0.1:5000"),
        installed=True,
    )
    assert code == module.EXIT_DISABLED
    assert report["status"] == "disabled"
    assert report["server_required"] is False


def test_enabled_but_not_installed_is_explicit() -> None:
    module = _load_helper()
    report, code = module.evaluate_mlflow_section(
        SimpleNamespace(enabled=True, strict=True, tracking_uri="http://127.0.0.1:5000"),
        installed=False,
    )
    assert code == module.EXIT_NOT_INSTALLED
    assert report["status"] == "not_installed"


def test_enabled_but_server_down_is_not_running_not_traceback() -> None:
    module = _load_helper()

    def down_probe(*args, **kwargs):
        del args, kwargs
        return False, "ConnectionRefusedError: test", None

    report, code = module.evaluate_mlflow_section(
        SimpleNamespace(enabled=True, strict=True, tracking_uri="http://127.0.0.1:5000"),
        installed=True,
        http_probe=down_probe,
    )
    assert code == module.EXIT_NOT_RUNNING
    assert report["status"] == "not_running"
    assert "ConnectionRefusedError" in report["detail"]


def test_ready_http_mlflow() -> None:
    module = _load_helper()

    def ready_probe(*args, **kwargs):
        del args, kwargs
        return True, "HTTP 200", "http://127.0.0.1:5000/health"

    report, code = module.evaluate_mlflow_section(
        SimpleNamespace(enabled=True, strict=True, tracking_uri="http://127.0.0.1:5000"),
        installed=True,
        http_probe=ready_probe,
    )
    assert code == module.EXIT_READY
    assert report["status"] == "ready"
    assert report["reachable"] is True


def test_blank_tracking_uri_uses_local_file_backend() -> None:
    module = _load_helper()
    report, code = module.evaluate_mlflow_section(
        SimpleNamespace(enabled=True, strict=False, tracking_uri=""),
        installed=True,
    )
    assert code == module.EXIT_READY
    assert report["status"] == "ready"
    assert report["server_required"] is False


def test_runner_contains_variant_policy_and_clean_message() -> None:
    runner = (ROOT / "scripts" / "Run-HF20-TimeContract-Retrain.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "MlflowPolicy" in runner
    assert "SERWER NIE JEST URUCHOMIONY" in runner
    assert "Trening NIE ZOSTAŁ rozpoczęty" in runner
    assert "Disable-MlflowForThisRun" in runner
