from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import Forecast, ModelVersion
from smog_ai.time_utils import ensure_utc
from smog_ai.quality import allowed_experimental_targets


DERIVED_MODEL_TARGET = {
    "precipitation_probability": "precipitation_mm",
}


def _failure_parameter(failure: dict[str, Any]) -> str | None:
    value = failure.get("parameter")
    return str(value) if value is not None else None


def audit_latest_hourly_serving_contract(
    session: Session,
    config: AppConfig,
    *,
    output: Path | None = None,
    allow_experimental_targets: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    latest_created = session.scalar(select(func.max(Forecast.forecast_created_at)))

    if latest_created is None:
        payload = {
            "schema_version": "1.1",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "passed": False,
            "serving_contract_passed": False,
            "publication_ready": False,
            "partial_success": False,
            "decision": "stop_hard_failures",
            "approved_targets": [],
            "experimental_targets": [],
            "experimental_model_targets": [],
            "hard_failures": [{"reason": "no_forecasts"}],
            "quality_failures": [],
            "warnings": [],
            "external_writes": False,
        }
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            payload["report_path"] = str(output)
        return payload

    forecasts = session.scalars(
        select(Forecast)
        .where(Forecast.forecast_created_at == latest_created)
        .order_by(
            Forecast.parameter,
            Forecast.air_station_id,
            Forecast.forecast_horizon,
        )
    ).all()

    models = session.scalars(
        select(ModelVersion).where(
            ModelVersion.active.is_(True),
            ModelVersion.forecast_horizon == 0,
        )
    ).all()
    model_by_target = {str(row.parameter): row for row in models}

    expected_serving = list(config.hourly_forecasting.serving_horizons_hours)
    required = list(config.hourly_forecasting.spatial_targets)

    grouped: dict[str, list[Forecast]] = defaultdict(list)
    for row in forecasts:
        grouped[str(row.parameter)].append(row)

    hard_failures: list[dict[str, Any]] = []
    quality_failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    parameters: dict[str, Any] = {}
    common_target_grids: list[tuple[str, ...]] = []

    for parameter in required:
        rows = grouped.get(parameter, [])
        leads = sorted({int(row.forecast_horizon) for row in rows})
        station_series: dict[int, list[float]] = defaultdict(list)
        model_horizons: set[int] = set()
        source_ages: list[float] = []
        target_times: set[datetime] = set()
        future_only = True
        finite = True
        range_ok = True

        for row in rows:
            value = float(row.predicted_value)
            station_series[int(row.air_station_id)].append(value)

            target = ensure_utc(row.target_time)
            created = ensure_utc(row.forecast_created_at)
            target_times.add(target)
            future_only = future_only and target > created
            finite = finite and math.isfinite(value)

            features = dict(row.features_json or {})
            model_horizon = int(
                features.get("model_horizon_hours", row.forecast_horizon)
            )
            model_horizons.add(model_horizon)

            if features.get("source_age_hours") is not None:
                source_ages.append(float(features["source_age_hours"]))

            if parameter in {"PM10", "PM2.5", "precipitation_mm"}:
                range_ok = range_ok and value >= 0
            elif parameter == "precipitation_probability":
                range_ok = range_ok and 0 <= value <= 1
            elif parameter == "temperature_c":
                range_ok = range_ok and -90 <= value <= 65

        variable_station_fraction = (
            sum(
                1
                for values in station_series.values()
                if len({round(value, 6) for value in values}) > 1
            )
            / len(station_series)
            if station_series
            else 0.0
        )

        grid = tuple(value.isoformat() for value in sorted(target_times))
        if rows:
            common_target_grids.append(grid)

        model_target = DERIVED_MODEL_TARGET.get(parameter, parameter)
        model = model_by_target.get(model_target)
        metrics = dict(model.metrics_json or {}) if model else {}
        precipitation_gate = dict(
            metrics.get("precipitation_quality_gate") or {}
        )
        quality_classification = dict(
            metrics.get("quality_classification") or {}
        )
        quality_status = str(
            metrics.get("quality_status")
            or quality_classification.get("status")
            or (precipitation_gate.get("status") if precipitation_gate else "approved")
            or "approved"
        ).lower()
        if quality_status == "accepted":
            quality_status = "approved"

        summary = {
            "rows": len(rows),
            "stations": len(station_series),
            "serving_leads": leads,
            "serving_leads_complete": leads == expected_serving,
            "model_horizon_min": min(model_horizons) if model_horizons else None,
            "model_horizon_max": max(model_horizons) if model_horizons else None,
            "model_horizons_within_limit": bool(model_horizons)
            and min(model_horizons)
            >= config.hourly_forecasting.minimum_horizon_hours
            and max(model_horizons)
            <= config.hourly_forecasting.model_horizon_maximum,
            "target_time_count": len(target_times),
            "future_only": future_only,
            "finite_values": finite,
            "range_ok": range_ok,
            "variable_station_fraction": round(variable_station_fraction, 6),
            "source_age_hours_min": min(source_ages) if source_ages else None,
            "source_age_hours_max": max(source_ages) if source_ages else None,
            "model_target": model_target,
            "model_provider": model.algorithm if model else None,
            "model_version": model.semantic_version if model else None,
            "quality_status": quality_status,
            "quality_classification": quality_classification or None,
            "precipitation_quality_gate": precipitation_gate or None,
        }
        parameters[parameter] = summary

        if not rows:
            hard_failures.append(
                {"parameter": parameter, "reason": "missing_forecasts"}
            )
            continue

        if leads != expected_serving:
            hard_failures.append(
                {
                    "parameter": parameter,
                    "reason": "incomplete_serving_leads",
                    "actual": leads,
                    "expected": expected_serving,
                }
            )

        if not summary["model_horizons_within_limit"]:
            hard_failures.append(
                {
                    "parameter": parameter,
                    "reason": "model_horizon_out_of_contract",
                }
            )

        if not future_only:
            hard_failures.append(
                {"parameter": parameter, "reason": "non_future_target"}
            )

        if not finite:
            hard_failures.append(
                {"parameter": parameter, "reason": "non_finite_value"}
            )

        if not range_ok:
            hard_failures.append(
                {"parameter": parameter, "reason": "value_out_of_range"}
            )

        if parameter in {"PM10", "PM2.5", "temperature_c"}:
            if variable_station_fraction == 0:
                warnings.append(
                    {
                        "parameter": parameter,
                        "reason": "all_station_curves_flat",
                        "severity": "quality_warning",
                        "model_provider": model.algorithm if model else None,
                        "note": (
                            "Flat curves are expected for persistence models; "
                            "the active output remains publishable."
                        ),
                    }
                )
            elif variable_station_fraction < 0.5:
                warnings.append(
                    {
                        "parameter": parameter,
                        "reason": "low_variable_station_fraction",
                        "actual": variable_station_fraction,
                    }
                )

        if quality_status == "experimental":
            gate_reasons = (
                quality_classification.get("reasons")
                or precipitation_gate.get("failures")
                or []
            )
            quality_failures.append(
                {
                    "parameter": parameter,
                    "model_target": model_target,
                    "reason": "model_quality_gate_failed",
                    "quality_status": "experimental",
                    "details": gate_reasons,
                }
            )
        elif model_target == "precipitation_mm":
            if precipitation_gate.get("passed") is not True:
                quality_failures.append(
                    {
                        "parameter": parameter,
                        "model_target": model_target,
                        "reason": "precipitation_quality_gate_failed",
                        "quality_status": (
                            metrics.get("quality_status")
                            or precipitation_gate.get("status")
                            or "experimental"
                        ),
                        "details": precipitation_gate.get("failures") or [],
                    }
                )

    common_grid = bool(common_target_grids) and len(set(common_target_grids)) == 1
    if not common_grid:
        hard_failures.append({"reason": "parameter_target_grids_differ"})

    serving_contract_passed = not hard_failures
    allowed_experimental = allowed_experimental_targets(
        allow_experimental_targets
    )

    experimental_targets = sorted(
        {
            str(row["parameter"])
            for row in quality_failures
            if row.get("parameter") is not None
        }
    )
    experimental_model_targets = sorted(
        {
            str(row["model_target"])
            for row in quality_failures
            if row.get("model_target") is not None
        }
    )
    forced_experimental_targets = sorted(
        parameter
        for parameter in experimental_targets
        if "*" in allowed_experimental
        or parameter in allowed_experimental
        or DERIVED_MODEL_TARGET.get(parameter, parameter) in allowed_experimental
    )
    blocked_experimental_targets = sorted(
        set(experimental_targets) - set(forced_experimental_targets)
    )
    publication_ready = serving_contract_passed and not blocked_experimental_targets
    partial_success = serving_contract_passed and bool(blocked_experimental_targets)

    hard_failure_targets = {
        parameter
        for parameter in (
            _failure_parameter(row) for row in hard_failures
        )
        if parameter is not None
    }

    approved_targets = (
        [
            parameter
            for parameter in required
            if parameter not in experimental_targets
            and parameter not in hard_failure_targets
        ]
        if serving_contract_passed
        else []
    )

    if hard_failures:
        decision = "stop_hard_failures"
    elif blocked_experimental_targets:
        decision = "continue_without_experimental_targets"
    elif forced_experimental_targets:
        decision = "continue_with_experimental_targets"
    else:
        decision = "ready"

    payload = {
        "schema_version": "1.1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "forecast_created_at": ensure_utc(latest_created).isoformat(),
        "expected_serving_leads": expected_serving,
        "maximum_model_horizon_hours": (
            config.hourly_forecasting.model_horizon_maximum
        ),
        "parameters": parameters,
        "common_target_grid": common_grid,
        "hard_failures": hard_failures,
        "quality_failures": quality_failures,
        "warnings": warnings,
        "serving_contract_passed": serving_contract_passed,
        "publication_ready": publication_ready,
        "partial_success": partial_success,
        "decision": decision,
        "approved_targets": approved_targets,
        "experimental_targets": experimental_targets,
        "experimental_model_targets": experimental_model_targets,
        "forced_experimental_targets": forced_experimental_targets,
        "blocked_experimental_targets": blocked_experimental_targets,
        # "passed" remains intentionally strict: all configured targets
        # must pass technical and quality gates.
        "passed": publication_ready,
        "external_writes": False,
    }

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload["report_path"] = str(output)

    return payload
