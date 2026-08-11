from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse


EXIT_READY = 0
EXIT_DISABLED = 2
EXIT_NOT_INSTALLED = 3
EXIT_NOT_RUNNING = 4
EXIT_INVALID = 5


def _message(
    *,
    status: str,
    enabled: bool,
    strict: bool,
    tracking_uri: str,
    installed: bool,
    reachable: bool | None,
    server_required: bool,
    detail: str,
    checked_url: str | None = None,
) -> dict[str, Any]:
    messages_pl = {
        "ready": "MLflow jest gotowy.",
        "disabled": "MLflow jest wyłączony w efektywnej konfiguracji.",
        "not_installed": (
            "MLflow jest włączony, ale pakiet Python 'mlflow' "
            "nie jest zainstalowany."
        ),
        "not_running": (
            "MLflow jest włączony, ale serwer śledzenia nie działa "
            "albo nie odpowiada."
        ),
        "invalid_configuration": (
            "Konfiguracja MLflow ma nieobsługiwany lub nieprawidłowy "
            "tracking_uri."
        ),
    }
    actions = {
        "ready": ["continue_training"],
        "disabled": ["continue_without_mlflow", "enable_mlflow_if_wanted"],
        "not_installed": ["install_mlflow", "continue_without_mlflow", "abort"],
        "not_running": ["start_mlflow_server", "continue_without_mlflow", "abort"],
        "invalid_configuration": ["fix_mlflow_configuration", "continue_without_mlflow", "abort"],
    }
    return {
        "schema_version": "1.0",
        "status": status,
        "message_pl": messages_pl[status],
        "enabled": enabled,
        "strict": strict,
        "tracking_uri": tracking_uri,
        "installed": installed,
        "reachable": reachable,
        "server_required": server_required,
        "detail": detail,
        "checked_url": checked_url,
        "recommended_actions": actions[status],
    }


def probe_http_tracking_uri(
    tracking_uri: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[bool, str, str | None]:
    base = tracking_uri.rstrip("/")
    candidates = [f"{base}/health", f"{base}/"]
    errors: list[str] = []

    for url in candidates:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "SmogAI-HF20.2-MLflowPreflight/1.0"},
            method="GET",
        )
        try:
            with opener(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                return True, f"HTTP {status}", url
        except urllib.error.HTTPError as exc:
            # Any response below 500 proves that the HTTP server is reachable.
            if int(exc.code) < 500:
                return True, f"HTTP {exc.code}", url
            errors.append(f"{url}: HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            errors.append(f"{url}: {type(exc).__name__}: {exc}")

    return False, " | ".join(errors), None


def evaluate_mlflow_section(
    section: Any,
    *,
    installed: bool,
    timeout_seconds: float = 3.0,
    http_probe: Callable[..., tuple[bool, str, str | None]] = probe_http_tracking_uri,
) -> tuple[dict[str, Any], int]:
    enabled = bool(getattr(section, "enabled", False))
    strict = bool(getattr(section, "strict", False))
    tracking_uri = str(getattr(section, "tracking_uri", "") or "").strip()

    if not enabled:
        return (
            _message(
                status="disabled",
                enabled=False,
                strict=strict,
                tracking_uri=tracking_uri,
                installed=installed,
                reachable=None,
                server_required=False,
                detail="mlflow.enabled=false",
            ),
            EXIT_DISABLED,
        )

    if not installed:
        return (
            _message(
                status="not_installed",
                enabled=True,
                strict=strict,
                tracking_uri=tracking_uri,
                installed=False,
                reachable=False,
                server_required=bool(tracking_uri),
                detail="importlib.util.find_spec('mlflow') returned None",
            ),
            EXIT_NOT_INSTALLED,
        )

    if not tracking_uri:
        return (
            _message(
                status="ready",
                enabled=True,
                strict=strict,
                tracking_uri="",
                installed=True,
                reachable=True,
                server_required=False,
                detail=(
                    "Brak tracking_uri: ActiveMlflowBridge użyje lokalnego "
                    "file:// opartego o mlflow.local_artifact_dir."
                ),
            ),
            EXIT_READY,
        )

    parsed = urlparse(tracking_uri)
    scheme = parsed.scheme.lower()

    if scheme == "file":
        return (
            _message(
                status="ready",
                enabled=True,
                strict=strict,
                tracking_uri=tracking_uri,
                installed=True,
                reachable=True,
                server_required=False,
                detail="Lokalny tracking URI file:// nie wymaga serwera HTTP.",
            ),
            EXIT_READY,
        )

    if scheme in {"http", "https"}:
        if not parsed.hostname:
            return (
                _message(
                    status="invalid_configuration",
                    enabled=True,
                    strict=strict,
                    tracking_uri=tracking_uri,
                    installed=True,
                    reachable=False,
                    server_required=True,
                    detail="HTTP tracking_uri nie zawiera hosta.",
                ),
                EXIT_INVALID,
            )
        reachable, detail, checked_url = http_probe(
            tracking_uri,
            timeout_seconds=timeout_seconds,
        )
        if reachable:
            return (
                _message(
                    status="ready",
                    enabled=True,
                    strict=strict,
                    tracking_uri=tracking_uri,
                    installed=True,
                    reachable=True,
                    server_required=True,
                    detail=detail,
                    checked_url=checked_url,
                ),
                EXIT_READY,
            )
        return (
            _message(
                status="not_running",
                enabled=True,
                strict=strict,
                tracking_uri=tracking_uri,
                installed=True,
                reachable=False,
                server_required=True,
                detail=detail,
            ),
            EXIT_NOT_RUNNING,
        )

    return (
        _message(
            status="invalid_configuration",
            enabled=True,
            strict=strict,
            tracking_uri=tracking_uri,
            installed=True,
            reachable=False,
            server_required=False,
            detail=f"Nieobsługiwany schemat URI: {scheme or '<brak>'}",
        ),
        EXIT_INVALID,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=3.0)
    args = parser.parse_args()

    try:
        project_root = args.project_root.resolve()
        sys.path.insert(0, str(project_root))

        from smog_ai.config import load_config

        cfg = load_config(args.config.resolve(), args.env_file.resolve())
        installed = importlib.util.find_spec("mlflow") is not None
        report, code = evaluate_mlflow_section(
            cfg.mlflow,
            installed=installed,
            timeout_seconds=max(0.2, float(args.timeout_seconds)),
        )
        report.update(
            {
                "project_root": str(project_root),
                "config": str(args.config.resolve()),
                "env_file": str(args.env_file.resolve()),
            }
        )
    except Exception as exc:  # noqa: BLE001 - clean CLI diagnostic
        report = {
            "schema_version": "1.0",
            "status": "invalid_configuration",
            "message_pl": "Nie udało się wykonać preflightu MLflow.",
            "enabled": None,
            "strict": None,
            "tracking_uri": None,
            "installed": importlib.util.find_spec("mlflow") is not None,
            "reachable": False,
            "server_required": None,
            "detail": f"{type(exc).__name__}: {exc}",
            "checked_url": None,
            "recommended_actions": [
                "fix_mlflow_configuration",
                "continue_without_mlflow",
                "abort",
            ],
        }
        code = EXIT_INVALID

    print(json.dumps(report, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
