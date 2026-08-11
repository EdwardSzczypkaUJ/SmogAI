#!/usr/bin/env python3
"""Quality gate for stage 2 model candidates.

The gate never modifies the database.  It verifies that requested active models
are not bootstrap fallbacks, that air targets are not persistence-only, that the
configured improvement threshold is met, and that training provenance points to
an immutable snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smog_ai.air_parameters import WEATHER_TARGETS, create_air_parameter_registry
from smog_ai.config import load_config
from smog_ai.database.engine import create_db_engine, init_database, session_scope
from smog_ai.database.models import ModelVersion


def _targets(raw: str, config: Any) -> list[str]:
    registry = create_air_parameter_registry(config)
    output: list[str] = []
    prepared = raw.replace("PM2,5", "PM2.5").replace(";", ",")
    for token in (part.strip() for part in prepared.split(",")):
        if not token:
            continue
        canonical = token if token in WEATHER_TARGETS else registry.resolve(token)
        if canonical not in output:
            output.append(canonical)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--allow-persistence", action="store_true")
    parser.add_argument("--allow-bootstrap", action="store_true")
    parser.add_argument("--allow-live-dataset", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    runtime = args.runtime_root.expanduser().resolve()
    cfg = load_config(
        args.config or runtime / "config.yaml",
        args.env_file or runtime / "smog-ai.env",
    )
    engine = create_db_engine(cfg)
    init_database(engine)
    targets = _targets(args.parameters, cfg)
    air_registry = create_air_parameter_registry(cfg)
    minimum = float(cfg.hourly_forecasting.minimum_mae_improvement_fraction)

    with session_scope(engine) as session:
        rows = session.scalars(
            select(ModelVersion).where(
                ModelVersion.active.is_(True),
                ModelVersion.forecast_horizon == 0,
                ModelVersion.parameter.in_(targets),
            )
        ).all()

    by_target = {row.parameter: row for row in rows}
    models: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for target in targets:
        row = by_target.get(target)
        if row is None:
            failures.append({"target": target, "reason": "missing_active_model"})
            continue
        metrics = dict(row.metrics_json or {})
        provenance = dict(metrics.get("data_provenance") or {})
        snapshot = dict(provenance.get("training_snapshot") or {})
        improvement = metrics.get("improvement_vs_persistence")
        bootstrap = bool(metrics.get("bootstrap"))
        is_air = air_registry.get(target) is not None
        precipitation_gate = dict(
            metrics.get("precipitation_quality_gate") or {}
        )
        item = {
            "target": target,
            "provider": row.algorithm,
            "version": row.semantic_version,
            "bootstrap": bootstrap,
            "improvement_vs_persistence": improvement,
            "minimum_improvement_required": minimum,
            "training_profile": metrics.get("training_profile"),
            "dataset_id": provenance.get("dataset_id") or snapshot.get("dataset_id"),
            "dataset_sha256": snapshot.get("database_sha256"),
            "dataset_immutable": snapshot.get("immutable"),
            "data_start": row.training_data_start.isoformat() if row.training_data_start else None,
            "data_end": row.training_data_end.isoformat() if row.training_data_end else None,
            "quality_status": metrics.get("quality_status"),
            "precipitation_quality_gate": precipitation_gate or None,
        }
        models.append(item)

        if bootstrap and not args.allow_bootstrap:
            failures.append({**item, "reason": "bootstrap_model_forbidden"})
        requires_regression_improvement = target != "precipitation_mm"
        if (is_air or target == "temperature_c") and row.algorithm == "persistence" and not args.allow_persistence:
            failures.append({**item, "reason": "persistence_model_forbidden"})
        if requires_regression_improvement and row.algorithm != "persistence":
            if improvement is None or float(improvement) < minimum:
                failures.append({**item, "reason": "insufficient_improvement"})
        if target == "precipitation_mm":
            if not precipitation_gate or precipitation_gate.get("passed") is not True:
                failures.append(
                    {
                        **item,
                        "reason": "precipitation_quality_gate_failed",
                        "gate_failures": precipitation_gate.get("failures", []),
                    }
                )
        if not args.allow_live_dataset:
            if not item["dataset_id"] or item["dataset_immutable"] is not True:
                failures.append({**item, "reason": "immutable_dataset_provenance_required"})
            if not item["dataset_sha256"]:
                failures.append({**item, "reason": "dataset_sha256_required"})

    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": not failures,
        "targets": targets,
        "models": models,
        "failures": failures,
    }
    output = args.output or (
        runtime
        / "reports"
        / "stage2-stage3"
        / f"model-quality-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload["report_path"] = str(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
