from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smog_ai.training_delta import (
    fast_preflight_candidate,
    layered_candidate_provenance,
    verify_candidate,
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _active_fingerprint(runtime: Path) -> dict[str, Any]:
    database = runtime / "data" / "smog.db"
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        rows = [
            list(row)
            for row in connection.execute(
                """
                SELECT id, parameter, algorithm, semantic_version, artifact_path,
                       active, activated_at
                  FROM model_versions
                 WHERE active=1
                 ORDER BY parameter, forecast_horizon, semantic_version
                """
            )
        ]
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {
        "active_model_count": len(rows),
        "active_models_sha256": hashlib.sha256(encoded).hexdigest(),
        "quick_pointer_sha256": _sha256(
            runtime / "training-datasets" / "quick" / "latest.json"
        ),
        "serving_pointer_sha256": _sha256(
            runtime / "object-store" / "serving" / "latest.json"
        ),
    }


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    prepared = str(value).strip().replace("Z", "+00:00")
    if not prepared:
        return None
    parsed = datetime.fromisoformat(prepared)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _inspect(runtime: Path, since: datetime, targets: set[str]) -> dict[str, Any]:
    database = runtime / "data" / "smog.db"
    candidates: list[dict[str, Any]] = []
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, parameter, algorithm, semantic_version, artifact_path,
                   active, created_at, metrics_json
              FROM model_versions
             WHERE forecast_horizon=0
             ORDER BY created_at, parameter
            """
        ).fetchall()
    for row in rows:
        created = _parse_time(row["created_at"])
        if created is None or created < since:
            continue
        if targets and str(row["parameter"]) not in targets:
            continue
        metrics = json.loads(row["metrics_json"] or "{}")
        candidates.append(
            {
                "id": row["id"],
                "target": row["parameter"],
                "provider": row["algorithm"],
                "version": row["semantic_version"],
                "active": bool(row["active"]),
                "artifact_path": row["artifact_path"],
                "artifact_exists": bool(
                    row["artifact_path"] and Path(row["artifact_path"]).is_file()
                ),
                "created_at": row["created_at"],
                "quality_status": metrics.get("quality_status"),
                "quality_classification": metrics.get("quality_classification"),
                "activation_policy": metrics.get("activation_policy"),
                "activated_by_training": metrics.get("activated"),
                "dataset_id": (
                    (metrics.get("data_provenance") or {}).get("dataset_id")
                ),
            }
        )
    return {
        "status": "ok",
        "mode": "inspect",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "active_fingerprint": _active_fingerprint(runtime),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    parser.add_argument("--targets", default="PM10")
    parser.add_argument(
        "--action",
        choices=("plan", "preflight", "fingerprint", "inspect"),
        default="plan",
    )
    parser.add_argument("--since")
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser().resolve()
    targets = {
        value.strip()
        for value in str(args.targets).replace(";", ",").split(",")
        if value.strip()
    }

    if args.action == "fingerprint":
        payload = _active_fingerprint(runtime)
    elif args.action == "preflight":
        payload = fast_preflight_candidate(
            runtime_root=runtime,
            profile=args.profile,
        )
    elif args.action == "inspect":
        if not args.since:
            raise ValueError("--since is required for inspect")
        since = _parse_time(args.since)
        if since is None:
            raise ValueError("Invalid --since value")
        payload = _inspect(runtime, since, targets)
    else:
        verification = verify_candidate(
            runtime_root=runtime,
            profile=args.profile,
        )
        ready = verification.get("candidate_ready_for_training_integration") is True
        payload = {
            "status": "ready" if ready else "blocked",
            "mode": "plan",
            "profile": args.profile,
            "targets": sorted(targets),
            "source": (
                layered_candidate_provenance(
                    runtime_root=runtime,
                    profile=args.profile,
                )
                if ready
                else None
            ),
            "verification": verification,
            "classification_policy": {
                "approved": "quality gate passed; eligible for activation",
                "experimental": (
                    "technical training passed but soft quality gate failed; "
                    "kept inactive and available for experiments"
                ),
                "rejected": "technical integrity or training contract failed",
            },
            "activation_policy": "candidate_only",
            "external_publication": False,
            "production_pointer_write": False,
            "scheduled_task_change": False,
            "next_action": "run_candidate_only" if ready else "repair_layered_chain",
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("status") not in {"blocked", "failed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
