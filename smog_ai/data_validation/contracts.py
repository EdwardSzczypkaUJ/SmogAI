from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from smog_ai.config import AppConfig, DataValidationConfig

logger = logging.getLogger(__name__)

FrameKind = Literal[
    "air_measurements",
    "weather_measurements",
    "training_frame",
    "hourly_training_frame",
    "snapshot_stations",
    "snapshot_forecasts",
    "spatial_surface",
]
ValidationPolicy = Literal["report", "fail"]


def _hourly_horizon_matches(data: pd.DataFrame) -> pd.Series:
    """Return an index-aligned boolean Series for the exact horizon invariant.

    Pandera DataFrame-level checks accept a boolean scalar, pandas Series, or
    pandas DataFrame.  ``numpy.isclose`` returns a bare ndarray, which Pandera
    0.32.x does not classify as a supported table/field output and therefore
    reports a single schema-level failure even when all rows are correct.
    This helper keeps the row index and treats unparsable values as failures.
    """

    measurement_time = pd.to_datetime(
        data["measurement_time"], utc=True, errors="coerce"
    )
    target_time = pd.to_datetime(
        data["target_time"], utc=True, errors="coerce"
    )
    horizon_hours = pd.to_numeric(
        data["horizon_hours"], errors="coerce"
    )
    delta_hours = (target_time - measurement_time).dt.total_seconds().div(3600.0)
    valid = (
        measurement_time.notna()
        & target_time.notna()
        & horizon_hours.notna()
        & delta_hours.notna()
    )
    result = pd.Series(False, index=data.index, dtype=bool)
    result.loc[valid] = (
        delta_hours.loc[valid].sub(horizon_hours.loc[valid]).abs() <= 1e-6
    )
    return result


class DataFrameContractError(ValueError):
    """Raised when a dataframe violates a contract configured as blocking."""

    def __init__(self, result: "FrameValidationResult") -> None:
        super().__init__(
            f"Dataframe contract failed for {result.kind}: "
            f"{result.failure_count} failure case(s); report={result.report_path or 'not persisted'}"
        )
        self.result = result


@dataclass(slots=True)
class FrameValidationResult:
    kind: FrameKind
    valid: bool
    engine: str
    policy: ValidationPolicy
    rows: int
    columns: list[str]
    started_at: datetime
    finished_at: datetime
    failure_count: int = 0
    failure_cases: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "valid": self.valid,
            "engine": self.engine,
            "policy": self.policy,
            "rows": self.rows,
            "columns": self.columns,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "failure_count": self.failure_count,
            "failure_cases": self.failure_cases,
            "warnings": self.warnings,
            "context": self.context,
            "report_path": self.report_path,
        }


class PanderaFrameValidator:
    """Validate pipeline dataframes with Pandera without coupling import-time startup to it.

    Pandera is a declared production dependency.  The lazy import is intentional:
    minimal tools such as ``--help`` and source-tree tests can still start in a
    diagnostic environment where optional wheels have not been installed.  A
    production configuration can set ``require_pandera: true`` to turn a missing
    package into a hard configuration/runtime error.
    """

    def __init__(self, settings: "DataValidationConfig") -> None:
        self.settings = settings

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("pandera") is not None

    @staticmethod
    def _normalise(frame: pd.DataFrame, kind: FrameKind) -> pd.DataFrame:
        output = frame.copy()
        datetime_columns = {
            "air_measurements": ("measurement_time", "collected_at"),
            "weather_measurements": ("measurement_time", "collected_at"),
            "training_frame": ("measurement_time", "target_time"),
            "hourly_training_frame": ("measurement_time", "target_time"),
            "snapshot_stations": ("measurement_time",),
            "snapshot_forecasts": (
                "forecast_created_at",
                "origin_time",
                "target_time",
                "verified_at",
            ),
            "spatial_surface": ("origin_time", "target_time"),
        }[kind]
        for column in datetime_columns:
            if column in output.columns:
                output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
        return output

    @staticmethod
    def _schema(kind: FrameKind) -> Any:
        import pandera.pandas as pa

        string = lambda **kwargs: pa.Column(str, coerce=True, **kwargs)
        numeric = lambda **kwargs: pa.Column(float, coerce=True, **kwargs)
        integer = lambda **kwargs: pa.Column(int, coerce=True, **kwargs)
        timestamp = lambda **kwargs: pa.Column(pa.DateTime, coerce=True, **kwargs)

        if kind == "air_measurements":
            return pa.DataFrameSchema(
                {
                    "source": string(nullable=False),
                    "source_station_id": string(nullable=False),
                    "source_sensor_id": string(nullable=True, required=False),
                    "parameter": string(
                        nullable=False,
                        checks=pa.Check.str_length(min_value=1),
                    ),
                    "measurement_time": timestamp(nullable=False),
                    "value": numeric(nullable=True),
                    "unit": string(nullable=True, required=False),
                    "is_valid": pa.Column(bool, coerce=True, nullable=False),
                    "collected_at": timestamp(nullable=False),
                },
                strict=False,
                coerce=True,
                name="air_measurements",
            )
        if kind == "weather_measurements":
            return pa.DataFrameSchema(
                {
                    "source": string(nullable=False),
                    "source_station_id": string(nullable=False),
                    "measurement_time": timestamp(nullable=False),
                    "temperature_c": numeric(nullable=True, checks=pa.Check.in_range(-90, 65)),
                    "humidity_percent": numeric(nullable=True, checks=pa.Check.in_range(0, 100)),
                    # Station pressure can legitimately fall below 800 hPa at
                    # high-elevation stations.  Keep the validation contract aligned
                    # with the archive parser, which preserves values down to 700 hPa.
                    "pressure_hpa": numeric(nullable=True, checks=pa.Check.in_range(700, 1150)),
                    "precipitation_mm": numeric(nullable=True, checks=pa.Check.ge(0)),
                    # This field is optional for most archive rows because WO6G is
                    # reported only at selected synoptic terms.  A native numpy/Python
                    # ``int`` dtype cannot represent missing values; with coerce=True
                    # Pandera therefore reports one or two dtype failures for every
                    # null, even when nullable=True.  Validate it as a nullable numeric
                    # column and additionally require integral values for non-null rows.
                    "precipitation_accumulation_period_hours": numeric(
                        nullable=True,
                        required=False,
                        checks=[
                            pa.Check.in_range(1, 24),
                            pa.Check(
                                lambda series: series.mod(1).eq(0),
                                error="accumulation period must be an integer number of hours",
                            ),
                        ],
                    ),
                    "wind_speed_mps": numeric(nullable=True, checks=pa.Check.ge(0)),
                    "wind_direction_deg": numeric(nullable=True, checks=pa.Check.in_range(0, 360)),
                    "is_valid": pa.Column(bool, coerce=True, nullable=False),
                    "collected_at": timestamp(nullable=False),
                },
                strict=False,
                coerce=True,
                name="weather_measurements",
            )
        if kind == "training_frame":
            from smog_ai.features.builder import FEATURE_COLUMNS

            columns: dict[str, Any] = {
                "air_station_id": integer(nullable=False, checks=pa.Check.gt(0)),
                "measurement_time": timestamp(nullable=False),
                "value": numeric(nullable=False, checks=pa.Check.ge(0)),
                "latitude": numeric(nullable=False, checks=pa.Check.in_range(-90, 90)),
                "longitude": numeric(nullable=False, checks=pa.Check.in_range(-180, 180)),
                "target": numeric(nullable=False, checks=pa.Check.ge(0)),
                "target_time": timestamp(nullable=False),
            }
            for feature in FEATURE_COLUMNS:
                # Engineered and meteorological features legitimately contain NaN;
                # model pipelines impute them later.
                columns.setdefault(feature, numeric(nullable=True))
            return pa.DataFrameSchema(
                columns,
                strict=False,
                coerce=True,
                checks=[
                    pa.Check(
                        lambda data: data["target_time"] > data["measurement_time"],
                        error="target_time must be later than measurement_time",
                    )
                ],
                name="training_frame",
            )
        if kind == "hourly_training_frame":
            from smog_ai.hourly.features import (
                PM_HOURLY_FEATURE_COLUMNS,
                WEATHER_HOURLY_FEATURE_COLUMNS,
            )

            columns: dict[str, Any] = {
                "air_station_id": integer(nullable=False, checks=pa.Check.gt(0)),
                "measurement_time": timestamp(nullable=False),
                "target_time": timestamp(nullable=False),
                "horizon_hours": integer(nullable=False, checks=pa.Check.gt(0)),
                # Six-hour precipitation accumulations are sparse by design;
                # the hurdle model can impute a missing current accumulation.
                # PM and temperature builders remove missing current observations
                # before this contract is evaluated.
                "current_value": numeric(nullable=True),
                # Temperature may be negative; pollutant and precipitation
                # non-negativity is enforced by the target-specific builder.
                "target": numeric(nullable=False),
                "latitude": numeric(nullable=False, checks=pa.Check.in_range(-90, 90)),
                "longitude": numeric(nullable=False, checks=pa.Check.in_range(-180, 180)),
                "target_occurrence": numeric(
                    nullable=True,
                    required=False,
                    checks=pa.Check.in_range(0, 1),
                ),
            }
            for feature in sorted(
                set(PM_HOURLY_FEATURE_COLUMNS) | set(WEATHER_HOURLY_FEATURE_COLUMNS)
            ):
                columns.setdefault(feature, numeric(nullable=True, required=False))
            return pa.DataFrameSchema(
                columns,
                strict=False,
                coerce=True,
                checks=[
                    pa.Check(
                        lambda data: data["target_time"] > data["measurement_time"],
                        error="target_time must be later than measurement_time",
                    ),
                    pa.Check(
                        _hourly_horizon_matches,
                        error="horizon_hours must equal target_time - measurement_time",
                        ignore_na=False,
                    ),
                ],
                name="hourly_training_frame",
            )
        if kind == "snapshot_stations":
            return pa.DataFrameSchema(
                {
                    "station_id": integer(nullable=False, checks=pa.Check.gt(0)),
                    "station_name": string(nullable=False, checks=pa.Check.str_length(min_value=1)),
                    "city_name": string(nullable=True, required=False),
                    "latitude": numeric(nullable=True, checks=pa.Check.in_range(-90, 90)),
                    "longitude": numeric(nullable=True, checks=pa.Check.in_range(-180, 180)),
                    "open_quality_flags": integer(nullable=False, checks=pa.Check.ge(0)),
                },
                strict=False,
                coerce=True,
                name="snapshot_stations",
            )
        if kind == "snapshot_forecasts":
            return pa.DataFrameSchema(
                {
                    "forecast_id": string(nullable=False, checks=pa.Check.str_length(min_value=1)),
                    "station_id": integer(nullable=False, checks=pa.Check.gt(0)),
                    "parameter": string(nullable=False, checks=pa.Check.str_length(min_value=1)),
                    "forecast_created_at": timestamp(nullable=False),
                    "origin_time": timestamp(nullable=False),
                    "target_time": timestamp(nullable=False),
                    "horizon_hours": integer(nullable=False, checks=pa.Check.gt(0)),
                    "predicted_value": numeric(nullable=False),
                    "actual_value": numeric(nullable=True),
                    "verification_status": string(nullable=False),
                    "verified_at": timestamp(nullable=True, required=False),
                },
                strict=False,
                coerce=True,
                checks=[
                    pa.Check(
                        lambda data: data["target_time"] > data["forecast_created_at"],
                        error="target_time must be later than forecast_created_at",
                    )
                ],
                name="snapshot_forecasts",
            )
        if kind == "spatial_surface":
            return pa.DataFrameSchema(
                {
                    "cell_id": string(nullable=False, checks=pa.Check.str_length(min_value=1)),
                    "row": integer(nullable=False, checks=pa.Check.ge(0)),
                    "column": integer(nullable=False, checks=pa.Check.ge(0)),
                    "latitude": numeric(nullable=False, checks=pa.Check.in_range(-90, 90)),
                    "longitude": numeric(nullable=False, checks=pa.Check.in_range(-180, 180)),
                    "value": numeric(nullable=True),
                    "confidence": numeric(nullable=False, checks=pa.Check.in_range(0, 1)),
                    "nearest_station_distance_km": numeric(nullable=True, checks=pa.Check.ge(0)),
                    "stations_used": integer(nullable=False, checks=pa.Check.ge(0)),
                    "quality_flag": string(nullable=False),
                    "parameter": string(nullable=False, checks=pa.Check.str_length(min_value=1)),
                    "horizon_hours": integer(nullable=False, checks=pa.Check.gt(0)),
                    "origin_time": timestamp(nullable=False),
                    "target_time": timestamp(nullable=False),
                    "color_r": integer(nullable=False, checks=pa.Check.in_range(0, 255)),
                    "color_g": integer(nullable=False, checks=pa.Check.in_range(0, 255)),
                    "color_b": integer(nullable=False, checks=pa.Check.in_range(0, 255)),
                    "color_a": integer(nullable=False, checks=pa.Check.in_range(0, 255)),
                },
                strict=False,
                coerce=True,
                checks=[
                    pa.Check(
                        lambda data: data["target_time"] > data["origin_time"],
                        error="target_time must be later than origin_time",
                    )
                ],
                name="spatial_surface",
            )
        raise ValueError(f"Unsupported frame kind: {kind}")

    @staticmethod
    def _manual_failure_cases(frame: pd.DataFrame, kind: FrameKind) -> list[dict[str, Any]]:
        required: dict[FrameKind, tuple[str, ...]] = {
            "air_measurements": (
                "source",
                "source_station_id",
                "parameter",
                "measurement_time",
                "is_valid",
                "collected_at",
            ),
            "weather_measurements": (
                "source",
                "source_station_id",
                "measurement_time",
                "is_valid",
                "collected_at",
            ),
            "training_frame": (
                "air_station_id",
                "measurement_time",
                "value",
                "latitude",
                "longitude",
                "target",
                "target_time",
                "pm_lag_1",
            ),
            "hourly_training_frame": (
                "air_station_id",
                "measurement_time",
                "target_time",
                "horizon_hours",
                "current_value",
                "target",
                "latitude",
                "longitude",
            ),
            "snapshot_stations": (
                "station_id",
                "station_name",
                "latitude",
                "longitude",
                "open_quality_flags",
            ),
            "snapshot_forecasts": (
                "forecast_id",
                "station_id",
                "parameter",
                "forecast_created_at",
                "target_time",
                "horizon_hours",
                "predicted_value",
            ),
            "spatial_surface": (
                "cell_id",
                "row",
                "column",
                "latitude",
                "longitude",
                "value",
                "confidence",
                "stations_used",
                "parameter",
                "horizon_hours",
                "origin_time",
                "target_time",
            ),
        }
        failures: list[dict[str, Any]] = []
        for column in required[kind]:
            if column not in frame.columns:
                failures.append(
                    {
                        "schema_context": "DataFrameSchema",
                        "column": column,
                        "check": "column_in_dataframe",
                        "failure_case": "missing",
                        "index": None,
                    }
                )
        return failures

    @staticmethod
    def _serialise_failure_cases(value: Any, limit: int) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, pd.DataFrame):
            frame = value.head(limit).copy()
            frame = frame.where(pd.notna(frame), None)
            return json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
        return [{"failure_case": str(value)}]

    def validate(
        self,
        frame: pd.DataFrame,
        kind: FrameKind,
        *,
        policy: ValidationPolicy,
        context: dict[str, Any] | None = None,
    ) -> tuple[pd.DataFrame, FrameValidationResult]:
        started = datetime.now(UTC)
        prepared = self._normalise(frame, kind)
        failures: list[dict[str, Any]] = []
        total_failure_count = 0
        warnings: list[str] = []
        engine = "pandera"

        if self.available():
            try:
                schema = self._schema(kind)
                prepared = schema.validate(
                    prepared,
                    lazy=self.settings.lazy,
                    inplace=False,
                )
            except Exception as exc:  # SchemaErrors/SchemaError are optional-import types.
                failure_cases = getattr(exc, "failure_cases", None)
                if isinstance(failure_cases, pd.DataFrame):
                    total_failure_count = len(failure_cases)
                failures = self._serialise_failure_cases(
                    failure_cases,
                    self.settings.max_failure_cases,
                )
                if not failures:
                    failures = [{"failure_case": str(exc), "exception": type(exc).__name__}]
                if total_failure_count > len(failures):
                    warnings.append(
                        f"Failure-case details truncated to {len(failures)} of "
                        f"{total_failure_count}."
                    )
        else:
            engine = "manual-fallback"
            message = (
                "Pandera is not installed; structural fallback validation was used. "
                "Install the declared pandera[pandas] dependency for production validation."
            )
            if self.settings.require_pandera:
                failures = [{"failure_case": message, "check": "pandera_available"}]
            else:
                warnings.append(message)
                failures = self._manual_failure_cases(prepared, kind)

        finished = datetime.now(UTC)
        result = FrameValidationResult(
            kind=kind,
            valid=not failures,
            engine=engine,
            policy=policy,
            rows=len(prepared),
            columns=[str(column) for column in prepared.columns],
            started_at=started,
            finished_at=finished,
            failure_count=total_failure_count or len(failures),
            failure_cases=failures,
            warnings=warnings,
            context=dict(context or {}),
        )
        return prepared, result


def _policy_for(settings: "DataValidationConfig", kind: FrameKind) -> ValidationPolicy:
    if kind in {"air_measurements", "weather_measurements"}:
        return settings.operational_policy
    if kind in {"training_frame", "hourly_training_frame"}:
        return settings.training_policy
    return settings.snapshot_policy


def persist_validation_report(config: "AppConfig", result: FrameValidationResult) -> Path:
    root = config.data_validation.reports_dir
    root.mkdir(parents=True, exist_ok=True)
    timestamp = result.finished_at.strftime("%Y%m%dT%H%M%S.%fZ")
    context_token = str(result.context.get("run_id") or result.context.get("dataset_id") or "frame")
    safe = "".join(character if character.isalnum() or character in "-_." else "-" for character in context_token)
    path = root / f"{result.kind}-{timestamp}-{safe[:80]}.json"
    payload = result.to_dict()
    payload["report_path"] = str(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
    result.report_path = str(path)
    return path


def validate_frame(
    frame: pd.DataFrame,
    kind: FrameKind,
    config: "AppConfig",
    *,
    context: dict[str, Any] | None = None,
    policy: ValidationPolicy | None = None,
) -> tuple[pd.DataFrame, FrameValidationResult]:
    """Validate and persist a machine-readable report.

    Operational raw data defaults to a reporting policy so suspicious source data
    remains available for audit.  Curated training frames and dashboard snapshots
    default to blocking validation.
    """

    settings = config.data_validation
    if not settings.enabled:
        now = datetime.now(UTC)
        result = FrameValidationResult(
            kind=kind,
            valid=True,
            engine="disabled",
            policy=policy or _policy_for(settings, kind),
            rows=len(frame),
            columns=[str(column) for column in frame.columns],
            started_at=now,
            finished_at=now,
            warnings=["Dataframe validation disabled by configuration."],
            context=dict(context or {}),
        )
        persist_validation_report(config, result)
        return frame, result

    selected_policy = policy or _policy_for(settings, kind)
    validated, result = PanderaFrameValidator(settings).validate(
        frame,
        kind,
        policy=selected_policy,
        context=context,
    )
    persist_validation_report(config, result)
    if not result.valid:
        logger.warning(
            "Dataframe validation failed kind=%s policy=%s failures=%s "
            "context=%s report=%s",
            kind,
            selected_policy,
            result.failure_count,
            result.context,
            result.report_path,
        )
        if selected_policy == "fail":
            raise DataFrameContractError(result)
    return validated, result
