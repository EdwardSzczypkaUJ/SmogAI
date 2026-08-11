from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from smog_ai.config import load_config
from smog_ai.database.engine import create_db_engine, init_database, session_scope
from smog_ai.database.models import Forecast, ModelVersion
from smog_ai.time_utils import ensure_utc

DERIVED_MODEL_TARGET = {
    "precipitation_probability": "precipitation_mm",
}


def _as_utc(value: datetime) -> datetime:
    return ensure_utc(value)


def _active_model_map(session) -> dict[str, ModelVersion]:  # type: ignore[no-untyped-def]
    rows = session.scalars(
        select(ModelVersion).where(
            ModelVersion.active.is_(True),
            ModelVersion.forecast_horizon == 0,
        )
    ).all()
    return {str(row.parameter): row for row in rows}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the HF20 serving-lead/model-horizon forecast contract. "
            "The command is read-only and never publishes artifacts."
        )
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--parameters",
        default=(
            "PM10,PM2.5,temperature_c,precipitation_mm,"
            "precipitation_probability"
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(args.config, args.env_file)
    engine = create_db_engine(config)
    init_database(engine)

    parameters = tuple(
        dict.fromkeys(
            value.strip()
            for value in args.parameters.replace(";", ",").split(",")
            if value.strip()
        )
    )
    expected_leads = list(
        range(1, config.hourly_forecasting.serving_horizon_count + 1)
    )
    model_maximum = config.hourly_forecasting.model_horizon_maximum

    with session_scope(engine) as session:
        created_at = session.scalar(select(func.max(Forecast.forecast_created_at)))
        if created_at is None:
            raise RuntimeError("No hourly forecasts are available for audit")
        rows = session.scalars(
            select(Forecast)
            .where(
                Forecast.forecast_created_at == created_at,
                Forecast.parameter.in_(parameters),
            )
            .order_by(
                Forecast.parameter,
                Forecast.air_station_id,
                Forecast.forecast_horizon,
            )
        ).all()
        active_models = _active_model_map(session)

    created = _as_utc(created_at)
    grouped: dict[str, list[Forecast]] = defaultdict(list)
    for row in rows:
        grouped[str(row.parameter)].append(row)

    hard_failures: list[dict[str, Any]] = []
    quality_failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    common_target_grid: set[str] | None = None

    for parameter in parameters:
        items = grouped.get(parameter, [])
        if not items:
            hard_failures.append(
                {"parameter": parameter, "reason": "missing_forecasts"}
            )
            summaries[parameter] = {"rows": 0}
            continue

        leads = sorted({int(row.forecast_horizon) for row in items})
        station_groups: dict[int, list[Forecast]] = defaultdict(list)
        model_horizons: list[int] = []
        source_ages: list[float] = []
        target_grid = sorted({_as_utc(row.target_time).isoformat() for row in items})
        per_row_failures: list[dict[str, Any]] = []

        for row in items:
            station_groups[int(row.air_station_id)].append(row)
            features = dict(row.features_json or {})
            serving_lead = int(
                features.get("serving_lead_hours", row.forecast_horizon)
            )
            model_horizon = features.get("model_horizon_hours")
            anchor_raw = features.get("serving_anchor_time")
            source_age = features.get("source_age_hours")

            if serving_lead != int(row.forecast_horizon):
                per_row_failures.append(
                    {
                        "reason": "serving_lead_column_mismatch",
                        "forecast_id": row.id,
                    }
                )
            if model_horizon is None:
                per_row_failures.append(
                    {
                        "reason": "model_horizon_missing",
                        "forecast_id": row.id,
                    }
                )
                continue

            model_horizon = int(model_horizon)
            model_horizons.append(model_horizon)
            if model_horizon < 1 or model_horizon > model_maximum:
                per_row_failures.append(
                    {
                        "reason": "model_horizon_out_of_range",
                        "forecast_id": row.id,
                        "actual": model_horizon,
                        "maximum": model_maximum,
                    }
                )

            origin = _as_utc(row.forecast_origin_time)
            target = _as_utc(row.target_time)
            actual_model_horizon = int(
                round((target - origin).total_seconds() / 3600.0)
            )
            if actual_model_horizon != model_horizon:
                per_row_failures.append(
                    {
                        "reason": "model_horizon_time_mismatch",
                        "forecast_id": row.id,
                        "declared": model_horizon,
                        "derived": actual_model_horizon,
                    }
                )
            if target <= created:
                per_row_failures.append(
                    {
                        "reason": "target_not_in_future",
                        "forecast_id": row.id,
                        "target_time": target.isoformat(),
                        "created_at": created.isoformat(),
                    }
                )
            if target.minute or target.second or target.microsecond:
                per_row_failures.append(
                    {
                        "reason": "target_not_full_hour",
                        "forecast_id": row.id,
                    }
                )

            if anchor_raw:
                anchor = _as_utc(datetime.fromisoformat(str(anchor_raw)))
            else:
                anchor = target - timedelta(hours=serving_lead - 1)
            expected_target = anchor + timedelta(hours=serving_lead - 1)
            if expected_target != target:
                per_row_failures.append(
                    {
                        "reason": "serving_anchor_time_mismatch",
                        "forecast_id": row.id,
                        "expected": expected_target.isoformat(),
                        "actual": target.isoformat(),
                    }
                )
            if source_age is not None:
                source_ages.append(float(source_age))

        incomplete_stations = {
            station_id: sorted({int(row.forecast_horizon) for row in station_rows})
            for station_id, station_rows in station_groups.items()
            if sorted({int(row.forecast_horizon) for row in station_rows})
            != expected_leads
        }
        if leads != expected_leads:
            hard_failures.append(
                {
                    "parameter": parameter,
                    "reason": "serving_horizons_incomplete",
                    "actual": leads,
                    "expected": expected_leads,
                }
            )
        if incomplete_stations:
            hard_failures.append(
                {
                    "parameter": parameter,
                    "reason": "station_curves_incomplete",
                    "station_count": len(incomplete_stations),
                    "examples": dict(list(incomplete_stations.items())[:10]),
                }
            )
        if per_row_failures:
            hard_failures.append(
                {
                    "parameter": parameter,
                    "reason": "time_contract_row_failures",
                    "count": len(per_row_failures),
                    "examples": per_row_failures[:20],
                }
            )

        if common_target_grid is None:
            common_target_grid = set(target_grid)
        elif set(target_grid) != common_target_grid:
            hard_failures.append(
                {
                    "parameter": parameter,
                    "reason": "target_grid_differs_between_parameters",
                }
            )

        model_target = DERIVED_MODEL_TARGET.get(parameter, parameter)
        model = active_models.get(model_target)
        metrics = dict(model.metrics_json or {}) if model is not None else {}
        gate = dict(metrics.get("precipitation_quality_gate") or {})
        quality_status = metrics.get("quality_status")
        if model_target == "precipitation_mm" and gate.get("passed") is not True:
            quality_failures.append(
                {
                    "parameter": parameter,
                    "reason": "precipitation_model_experimental",
                    "quality_status": quality_status,
                    "failures": gate.get("failures") or [],
                }
            )

        values = [float(row.predicted_value) for row in items]
        finite = all(math.isfinite(value) for value in values)
        if not finite:
            hard_failures.append(
                {"parameter": parameter, "reason": "non_finite_predictions"}
            )

        summaries[parameter] = {
            "rows": len(items),
            "stations": len(station_groups),
            "serving_leads": leads,
            "serving_horizon_count": len(leads),
            "model_horizon_min": min(model_horizons) if model_horizons else None,
            "model_horizon_max": max(model_horizons) if model_horizons else None,
            "source_age_hours_min": min(source_ages) if source_ages else None,
            "source_age_hours_max": max(source_ages) if source_ages else None,
            "target_time_start": target_grid[0] if target_grid else None,
            "target_time_end": target_grid[-1] if target_grid else None,
            "finite_values": finite,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "unique_values_rounded_6": len({round(value, 6) for value in values}),
            "model_provider": model.algorithm if model is not None else None,
            "model_version": model.semantic_version if model is not None else None,
            "quality_status": quality_status,
            "precipitation_quality_gate": gate or None,
        }

    report = {
        "schema_version": "2.0",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "forecast_created_at": created.isoformat(),
        "time_contract": {
            "serving_horizon_hours": (
                config.hourly_forecasting.serving_horizon_count
            ),
            "maximum_source_delay_hours": (
                config.hourly_forecasting.maximum_source_delay_hours
            ),
            "maximum_model_horizon_hours": model_maximum,
        },
        "parameters": summaries,
        "hard_failures": hard_failures,
        "quality_failures": quality_failures,
        "warnings": warnings,
        "serving_contract_passed": not hard_failures,
        "publication_ready": not hard_failures and not quality_failures,
        "publication_performed": False,
        "external_writes": False,
    }

    output = args.output or (
        args.runtime_root
        / "reports"
        / "stage2-stage3"
        / (
            "hf20-time-contract-audit-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + ".json"
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    report["report_path"] = str(output)
    print(json.dumps(report, ensure_ascii=True, indent=2, default=str))

    if hard_failures:
        return 1
    if quality_failures:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
