#!/usr/bin/env python3
"""Read-only readiness audit for immutable local training and DigitalOcean serving.

The script performs no training and no deployment.  It validates that stage 2
(model work on a versioned SQLite snapshot) and stage 3 (local FastAPI/Streamlit,
then read-only DigitalOcean App Platform) have the required contracts and
artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.validate_digitalocean_spec import validate as validate_do_spec
from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import load_config
from smog_ai.database.engine import create_db_engine, init_database, session_scope
from smog_ai.database.models import ModelVersion
from smog_ai.storage.base import ObjectNotFoundError
from smog_ai.training_snapshot import create_training_snapshot_bridge


REQUIRED_LOCAL_FILES = (
    "server/api/main.py",
    "server/dashboard/app.py",
    "scripts/Start-LocalApi.ps1",
    "scripts/Start-LocalDashboard.ps1",
    "scripts/Test-LocalServer.ps1",
    "requirements-server.txt",
)
REQUIRED_DIGITALOCEAN_FILES = (
    ".do/app.yaml",
    ".do/app.dev.yaml",
    ".github/workflows/ci-deploy-digitalocean.yml",
    "scripts/validate_digitalocean_spec.py",
)


def _safe_pointer(repository: Any, key: str) -> dict[str, Any]:
    try:
        payload = repository.get_json(key)
    except ObjectNotFoundError:
        return {"exists": False, "key": key, "error": "not_found"}
    except Exception as exc:  # a readiness report must preserve the exact error
        return {
            "exists": False,
            "key": key,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"exists": True, "key": key, "payload": payload}


def _file_contract(root: Path, paths: tuple[str, ...]) -> dict[str, Any]:
    rows = []
    for relative in paths:
        path = root / relative
        rows.append(
            {
                "relative_path": relative,
                "path": str(path),
                "exists": path.is_file(),
                "size_bytes": path.stat().st_size if path.is_file() else 0,
            }
        )
    return {
        "complete": all(row["exists"] for row in rows),
        "files": rows,
    }


def _active_models(engine: Any) -> list[dict[str, Any]]:
    with session_scope(engine) as session:
        rows = session.scalars(
            select(ModelVersion)
            .where(ModelVersion.active.is_(True))
            .order_by(ModelVersion.parameter, ModelVersion.created_at.desc())
        ).all()

    output: list[dict[str, Any]] = []
    for row in rows:
        metrics = dict(row.metrics_json or {})
        provenance = dict(metrics.get("data_provenance") or {})
        snapshot = dict(provenance.get("training_snapshot") or {})
        output.append(
            {
                "target": row.parameter,
                "provider": row.algorithm,
                "version": row.semantic_version,
                "bootstrap": bool(metrics.get("bootstrap")),
                "training_profile": metrics.get("training_profile"),
                "dataset_id": provenance.get("dataset_id") or snapshot.get("dataset_id"),
                "dataset_sha256": snapshot.get("database_sha256"),
                "dataset_immutable": snapshot.get("immutable"),
                "data_start": (
                    row.training_data_start.isoformat()
                    if row.training_data_start is not None
                    else None
                ),
                "data_end": (
                    row.training_data_end.isoformat()
                    if row.training_data_end is not None
                    else None
                ),
            }
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--verify-snapshot-checksum", action="store_true")
    parser.add_argument("--skip-object-store", action="store_true")
    parser.add_argument("--strict-artifacts", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    root = args.project_root.expanduser().resolve()
    runtime = args.runtime_root.expanduser().resolve()
    config_path = args.config or runtime / "config.yaml"
    env_path = args.env_file or runtime / "smog-ai.env"
    cfg = load_config(config_path, env_path)
    engine = create_db_engine(cfg)
    init_database(engine)

    snapshot_bridge = create_training_snapshot_bridge(cfg)
    snapshots: list[dict[str, Any]] = []
    for snapshot in snapshot_bridge.list():
        validation: dict[str, Any]
        try:
            validation = snapshot_bridge.validate(
                snapshot,
                verify_checksum=args.verify_snapshot_checksum,
            )
        except Exception as exc:
            validation = {"valid": False, "error": str(exc)}
        snapshots.append({**snapshot.as_dict(), "validation": validation})

    latest_snapshots: dict[str, Any] = {}
    for profile in ("quick", "full"):
        try:
            latest_snapshots[profile] = snapshot_bridge.latest(profile).as_dict()
        except FileNotFoundError:
            latest_snapshots[profile] = None

    app_specs: dict[str, Any] = {}
    for relative, development in (
        (".do/app.yaml", False),
        (".do/app.dev.yaml", True),
    ):
        path = root / relative
        try:
            app_specs[relative] = validate_do_spec(
                path,
                allow_development=development,
            )
        except Exception as exc:
            app_specs[relative] = {
                "status": "failed",
                "path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            }

    artifacts: dict[str, Any]
    if args.skip_object_store or not cfg.object_storage.enabled:
        artifacts = {
            "checked": False,
            "reason": (
                "skip_object_store"
                if args.skip_object_store
                else "object_storage_disabled"
            ),
        }
    else:
        repository = create_artifact_repository(cfg)
        artifacts = {
            "checked": True,
            "backend": cfg.object_storage.backend,
            "latest_raw": _safe_pointer(
                repository,
                repository.layout.latest_raw_manifest,
            ),
            "latest_forecast": _safe_pointer(
                repository,
                repository.layout.latest_forecast_pointer,
            ),
            "latest_spatial": _safe_pointer(
                repository,
                repository.layout.latest_spatial_pointer,
            ),
            "documentation": _safe_pointer(
                repository,
                repository.layout.documentation_manifest,
            ),
        }

    active_models = _active_models(engine)
    immutable_models = [
        row
        for row in active_models
        if row.get("dataset_id") and row.get("dataset_immutable") is True
    ]
    local_contract = _file_contract(root, REQUIRED_LOCAL_FILES)
    do_contract = _file_contract(root, REQUIRED_DIGITALOCEAN_FILES)

    stage2_ready = bool(snapshots) and bool(active_models)
    stage2_reproducible = bool(immutable_models)
    stage3_local_ready = local_contract["complete"] and bool(active_models)
    app_specs_ok = all(
        row.get("status") == "ok" for row in app_specs.values()
    )
    published_required = True
    if artifacts.get("checked"):
        published_required = all(
            artifacts[name].get("exists") is True
            for name in ("latest_forecast", "latest_spatial")
        )
    stage3_digitalocean_ready = (
        do_contract["complete"]
        and app_specs_ok
        and published_required
    )

    generated_at = datetime.now(UTC)
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": generated_at.isoformat(),
        "project_root": str(root),
        "runtime_root": str(runtime),
        "stage2": {
            "ready": stage2_ready,
            "reproducible_snapshot_models": stage2_reproducible,
            "latest_snapshots": latest_snapshots,
            "snapshot_count": len(snapshots),
            "snapshots": snapshots,
            "active_models": active_models,
        },
        "stage3_local": {
            "ready": stage3_local_ready,
            "contract": local_contract,
        },
        "stage3_digitalocean": {
            "ready": stage3_digitalocean_ready,
            "contract": do_contract,
            "app_specs": app_specs,
            "published_artifacts": artifacts,
            "architecture": (
                "local ingestion/training -> DigitalOcean Spaces -> "
                "read-only FastAPI/Streamlit App Platform"
            ),
        },
    }

    output = args.output or (
        runtime
        / "reports"
        / "stage2-stage3"
        / f"preflight-{generated_at:%Y%m%dT%H%M%SZ}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    payload["report_path"] = str(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    if args.strict_artifacts and not stage3_digitalocean_ready:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
