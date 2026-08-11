from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from smog_ai.config import AppConfig, HourlyTrainingProfileConfig

TrainingProfileName = Literal["quick", "full"]
TrainingPhase = Literal["candidate", "validation", "final"]

_WEIGHT_COLUMN = "__sample_weight"


@dataclass(frozen=True, slots=True)
class ResolvedTrainingProfile:
    name: TrainingProfileName
    maximum_training_days_by_target: dict[str, int]
    maximum_rows_per_target: int
    validation_max_rows: int
    always_keep_recent_days: int
    horizon_bucket_edges: tuple[int, ...]
    samples_per_horizon_bucket: int
    cross_fit_folds: int
    algorithms: dict[str, tuple[str, ...]]
    fit_quantiles: bool
    max_wall_time_seconds: int
    rare_event_quantile: float
    recency_half_life_days: float

    def maximum_training_days(self, target: str) -> int:
        return int(
            self.maximum_training_days_by_target.get(
                target,
                max(self.maximum_training_days_by_target.values(), default=365),
            )
        )

    def algorithms_for(
        self,
        target: str,
        fallback: list[str] | tuple[str, ...],
    ) -> tuple[str, ...]:
        configured = self.algorithms.get(target)
        if configured:
            return tuple(configured)
        return tuple(fallback)

    @property
    def horizons_per_origin(self) -> int:
        return max(1, len(self.horizon_bucket_edges) * self.samples_per_horizon_bucket)


@dataclass(slots=True)
class TrainingSelection:
    frame: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TrainingSetPolicy(Protocol):
    name: str

    def select(
        self,
        frame: pd.DataFrame,
        *,
        target: str,
        phase: TrainingPhase,
        maximum_rows: int,
        profile: ResolvedTrainingProfile,
        random_state: int,
    ) -> TrainingSelection:
        ...


def _profile_from_config(
    profile_name: TrainingProfileName,
    value: HourlyTrainingProfileConfig,
    *,
    rare_event_quantile: float,
    recency_half_life_days: float,
) -> ResolvedTrainingProfile:
    return ResolvedTrainingProfile(
        name=profile_name,
        maximum_training_days_by_target={
            str(key): int(item)
            for key, item in value.maximum_training_days_by_target.items()
        },
        maximum_rows_per_target=int(value.maximum_rows_per_target),
        validation_max_rows=int(value.validation_max_rows),
        always_keep_recent_days=int(value.always_keep_recent_days),
        horizon_bucket_edges=tuple(int(item) for item in value.horizon_bucket_edges),
        samples_per_horizon_bucket=int(value.samples_per_horizon_bucket),
        cross_fit_folds=int(value.cross_fit_folds),
        algorithms={
            str(target): tuple(str(name) for name in names)
            for target, names in value.algorithms.items()
        },
        fit_quantiles=bool(value.fit_quantiles),
        max_wall_time_seconds=int(value.max_wall_time_seconds),
        rare_event_quantile=float(rare_event_quantile),
        recency_half_life_days=float(recency_half_life_days),
    )


def resolve_training_profile(
    config: AppConfig,
    profile_name: str | None = None,
) -> ResolvedTrainingProfile:
    policy = config.hourly_forecasting.training_policy
    selected = str(profile_name or policy.default_profile).strip().lower()
    if selected not in {"quick", "full"}:
        raise ValueError(
            f"Unknown hourly training profile {selected!r}; expected quick or full"
        )
    value = policy.quick if selected == "quick" else policy.full
    profile = _profile_from_config(
        selected,  # type: ignore[arg-type]
        value,
        rare_event_quantile=policy.rare_event_quantile,
        recency_half_life_days=policy.recency_half_life_days,
    )
    # When the explicit serving/model time contract is enabled, guarantee a
    # dedicated sampling bucket for the source-delay buffer (for example
    # h49-h60).  Runtime YAML need not repeat the edge in both quick/full
    # profiles.
    maximum = int(config.hourly_forecasting.model_horizon_maximum)
    if maximum > profile.horizon_bucket_edges[-1]:
        profile = ResolvedTrainingProfile(
            name=profile.name,
            maximum_training_days_by_target=profile.maximum_training_days_by_target,
            maximum_rows_per_target=profile.maximum_rows_per_target,
            validation_max_rows=profile.validation_max_rows,
            always_keep_recent_days=profile.always_keep_recent_days,
            horizon_bucket_edges=tuple(
                sorted({*profile.horizon_bucket_edges, maximum})
            ),
            samples_per_horizon_bucket=profile.samples_per_horizon_bucket,
            cross_fit_folds=profile.cross_fit_folds,
            algorithms=profile.algorithms,
            fit_quantiles=profile.fit_quantiles,
            max_wall_time_seconds=profile.max_wall_time_seconds,
            rare_event_quantile=profile.rare_event_quantile,
            recency_half_life_days=profile.recency_half_life_days,
        )
    return profile


def _horizon_bucket(
    values: pd.Series,
    edges: tuple[int, ...],
) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    bins = [-np.inf, *edges]
    if not edges or edges[-1] < numeric.max(skipna=True):
        bins.append(np.inf)
    labels = list(range(len(bins) - 1))
    return pd.cut(
        numeric,
        bins=bins,
        labels=labels,
        include_lowest=True,
        right=True,
    ).astype("Int64")


def _target_bucket(values: pd.Series, target: str) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().sum() < 8 or numeric.nunique(dropna=True) < 3:
        return pd.Series(0, index=values.index, dtype="Int64")
    try:
        return pd.qcut(
            numeric.rank(method="first"),
            q=5,
            labels=False,
            duplicates="drop",
        ).astype("Int64")
    except ValueError:
        return pd.Series(0, index=values.index, dtype="Int64")


def _rare_event_mask(
    frame: pd.DataFrame,
    *,
    target: str,
    quantile: float,
) -> pd.Series:
    values = pd.to_numeric(frame.get("target"), errors="coerce")
    available = values.dropna()
    if available.empty:
        return pd.Series(False, index=frame.index)

    if target == "precipitation_mm":
        return values > 0.0
    if target == "temperature_c":
        lower = float(available.quantile(max(0.0, 1.0 - quantile)))
        upper = float(available.quantile(quantile))
        return (values <= lower) | (values >= upper)
    threshold = float(available.quantile(quantile))
    return values >= threshold


def _deterministic_uniform(frame: pd.DataFrame, random_state: int) -> np.ndarray:
    keys = [
        column
        for column in (
            "air_station_id",
            "measurement_time",
            "horizon_hours",
            "target_time",
        )
        if column in frame.columns
    ]
    if not keys:
        keys = list(frame.columns[:1])
    hashed = pd.util.hash_pandas_object(
        frame.reindex(columns=keys),
        index=False,
        hash_key=f"{int(random_state):016d}"[-16:],
    ).to_numpy(dtype=np.uint64)
    # Map to the open unit interval to keep -log(u) finite.
    return (hashed.astype(np.float64) + 1.0) / (np.iinfo(np.uint64).max + 2.0)


def _sample_weights(
    frame: pd.DataFrame,
    *,
    profile: ResolvedTrainingProfile,
) -> np.ndarray:
    if frame.empty:
        return np.asarray([], dtype=float)

    times = pd.to_datetime(frame["measurement_time"], utc=True, errors="coerce")
    latest = times.max()
    if pd.isna(latest):
        recency = np.ones(len(frame), dtype=float)
    else:
        age_days = (
            (latest - times).dt.total_seconds().fillna(0.0).clip(lower=0.0)
            / 86400.0
        )
        half_life = max(1.0, profile.recency_half_life_days)
        recency = np.power(0.5, age_days.to_numpy(dtype=float) / half_life)

    station_counts = frame.groupby("air_station_id", dropna=False)[
        "air_station_id"
    ].transform("size")
    station_balance = 1.0 / np.sqrt(
        pd.to_numeric(station_counts, errors="coerce")
        .fillna(1.0)
        .clip(lower=1.0)
        .to_numpy(dtype=float)
    )

    horizon_counts = frame.groupby("horizon_hours", dropna=False)[
        "horizon_hours"
    ].transform("size")
    horizon_balance = 1.0 / np.sqrt(
        pd.to_numeric(horizon_counts, errors="coerce")
        .fillna(1.0)
        .clip(lower=1.0)
        .to_numpy(dtype=float)
    )

    weights = recency * station_balance * horizon_balance
    mean = float(np.nanmean(weights)) if np.isfinite(weights).any() else 1.0
    if not math.isfinite(mean) or mean <= 0:
        mean = 1.0
    return np.clip(weights / mean, 0.10, 10.0)


def _bounded_select(
    frame: pd.DataFrame,
    *,
    target: str,
    maximum_rows: int,
    profile: ResolvedTrainingProfile,
    random_state: int,
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    output = frame.copy()
    output["measurement_time"] = pd.to_datetime(
        output["measurement_time"], utc=True, errors="coerce"
    )
    output = output.dropna(subset=["measurement_time", "target"]).copy()
    if len(output) <= maximum_rows:
        return output

    latest = output["measurement_time"].max()
    recent_cutoff = latest - pd.Timedelta(days=profile.always_keep_recent_days)
    recent = output["measurement_time"] >= recent_cutoff
    rare = _rare_event_mask(
        output,
        target=target,
        quantile=profile.rare_event_quantile,
    )
    mandatory_mask = recent | rare
    mandatory = output[mandatory_mask].copy()
    optional = output[~mandatory_mask].copy()

    for selected in (mandatory, optional):
        selected["__horizon_bucket"] = _horizon_bucket(
            selected["horizon_hours"],
            profile.horizon_bucket_edges,
        )
        selected["__target_bucket"] = _target_bucket(selected["target"], target)
        selected["__month"] = selected["measurement_time"].dt.month.astype("Int64")

    def priority(table: pd.DataFrame, *, mandatory_rows: bool) -> pd.Series:
        if table.empty:
            return pd.Series(dtype=float)
        station_count = table.groupby("air_station_id", dropna=False)[
            "air_station_id"
        ].transform("size")
        horizon_count = table.groupby("__horizon_bucket", dropna=False)[
            "__horizon_bucket"
        ].transform("size")
        target_count = table.groupby("__target_bucket", dropna=False)[
            "__target_bucket"
        ].transform("size")
        month_count = table.groupby("__month", dropna=False)["__month"].transform(
            "size"
        )
        balance = (
            1.0
            / np.sqrt(
                pd.to_numeric(station_count, errors="coerce")
                .fillna(1.0)
                .clip(lower=1.0)
            )
            * 1.0
            / np.sqrt(
                pd.to_numeric(horizon_count, errors="coerce")
                .fillna(1.0)
                .clip(lower=1.0)
            )
            * 1.0
            / np.sqrt(
                pd.to_numeric(target_count, errors="coerce")
                .fillna(1.0)
                .clip(lower=1.0)
            )
            * 1.0
            / np.sqrt(
                pd.to_numeric(month_count, errors="coerce")
                .fillna(1.0)
                .clip(lower=1.0)
            )
        ).to_numpy(dtype=float)
        if mandatory_rows:
            balance *= 4.0
        uniform = _deterministic_uniform(table, random_state)
        return pd.Series(-np.log(uniform) / np.clip(balance, 1e-12, None), index=table.index)

    if len(mandatory) >= maximum_rows:
        mandatory["__priority"] = priority(mandatory, mandatory_rows=True)
        selected = mandatory.nsmallest(maximum_rows, "__priority")
    else:
        remaining = maximum_rows - len(mandatory)
        optional["__priority"] = priority(optional, mandatory_rows=False)
        sampled_optional = optional.nsmallest(remaining, "__priority")
        selected = pd.concat([mandatory, sampled_optional], ignore_index=False)

    return selected.drop(
        columns=[
            "__horizon_bucket",
            "__target_bucket",
            "__month",
            "__priority",
        ],
        errors="ignore",
    ).sort_values(
        ["measurement_time", "air_station_id", "horizon_hours"]
    ).reset_index(drop=True)


class FullHistoryPolicy:
    name = "full_history"

    def select(
        self,
        frame: pd.DataFrame,
        *,
        target: str,
        phase: TrainingPhase,
        maximum_rows: int,
        profile: ResolvedTrainingProfile,
        random_state: int,
    ) -> TrainingSelection:
        selected = frame.copy()
        if len(selected) > maximum_rows:
            selected = _bounded_select(
                selected,
                target=target,
                maximum_rows=maximum_rows,
                profile=profile,
                random_state=random_state,
            )
        selected[_WEIGHT_COLUMN] = _sample_weights(selected, profile=profile)
        return TrainingSelection(
            selected,
            {
                "policy": self.name,
                "phase": phase,
                "input_rows": len(frame),
                "selected_rows": len(selected),
                "maximum_rows": maximum_rows,
            },
        )


class RollingWindowPolicy:
    name = "rolling_window"

    def select(
        self,
        frame: pd.DataFrame,
        *,
        target: str,
        phase: TrainingPhase,
        maximum_rows: int,
        profile: ResolvedTrainingProfile,
        random_state: int,
    ) -> TrainingSelection:
        if frame.empty:
            return TrainingSelection(frame.copy(), {"policy": self.name, "phase": phase})
        output = frame.copy()
        times = pd.to_datetime(output["measurement_time"], utc=True, errors="coerce")
        cutoff = times.max() - pd.Timedelta(days=profile.maximum_training_days(target))
        output = output[times >= cutoff].copy()
        output = _bounded_select(
            output,
            target=target,
            maximum_rows=maximum_rows,
            profile=profile,
            random_state=random_state,
        )
        output[_WEIGHT_COLUMN] = _sample_weights(output, profile=profile)
        return TrainingSelection(
            output,
            {
                "policy": self.name,
                "phase": phase,
                "input_rows": len(frame),
                "selected_rows": len(output),
                "cutoff": cutoff.isoformat(),
                "maximum_rows": maximum_rows,
            },
        )


class BoundedRollingStratifiedPolicy:
    name = "bounded_rolling_stratified"

    def select(
        self,
        frame: pd.DataFrame,
        *,
        target: str,
        phase: TrainingPhase,
        maximum_rows: int,
        profile: ResolvedTrainingProfile,
        random_state: int,
    ) -> TrainingSelection:
        if frame.empty:
            return TrainingSelection(frame.copy(), {"policy": self.name, "phase": phase})

        output = frame.copy()
        times = pd.to_datetime(output["measurement_time"], utc=True, errors="coerce")
        cutoff = times.max() - pd.Timedelta(days=profile.maximum_training_days(target))
        output = output[times >= cutoff].copy()
        before_cap = len(output)
        output = _bounded_select(
            output,
            target=target,
            maximum_rows=maximum_rows,
            profile=profile,
            random_state=random_state,
        )
        output[_WEIGHT_COLUMN] = _sample_weights(output, profile=profile)

        horizon_counts = {
            str(int(key)): int(value)
            for key, value in output["horizon_hours"].value_counts().sort_index().items()
        }
        metadata = {
            "policy": self.name,
            "phase": phase,
            "input_rows": len(frame),
            "rows_after_window": before_cap,
            "selected_rows": len(output),
            "maximum_rows": maximum_rows,
            "cutoff": cutoff.isoformat(),
            "recent_days_kept": profile.always_keep_recent_days,
            "rare_event_quantile": profile.rare_event_quantile,
            "recency_half_life_days": profile.recency_half_life_days,
            "horizon_counts": horizon_counts,
            "weight_min": float(output[_WEIGHT_COLUMN].min()) if len(output) else None,
            "weight_max": float(output[_WEIGHT_COLUMN].max()) if len(output) else None,
            "weight_mean": float(output[_WEIGHT_COLUMN].mean()) if len(output) else None,
        }
        return TrainingSelection(output, metadata)


def create_training_set_policy(config: AppConfig) -> TrainingSetPolicy:
    strategy = config.hourly_forecasting.training_policy.strategy
    if strategy == "full_history":
        return FullHistoryPolicy()
    if strategy == "rolling_window":
        return RollingWindowPolicy()
    if strategy == "bounded_rolling_stratified":
        return BoundedRollingStratifiedPolicy()
    raise ValueError(f"Unsupported training-set policy: {strategy}")


@dataclass(slots=True)
class TrainingBudget:
    max_seconds: float
    started_at: float = field(default_factory=time.monotonic)
    stopped_reason: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, float(self.max_seconds) - self.elapsed_seconds)

    @property
    def exhausted(self) -> bool:
        return self.remaining_seconds <= 0.0

    def should_continue(self, *, completed_candidates: int = 0) -> bool:
        if not self.exhausted:
            return True
        if completed_candidates <= 0:
            return True
        if self.stopped_reason is None:
            self.stopped_reason = "max_wall_time_exceeded"
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_seconds": float(self.max_seconds),
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "exhausted": self.exhausted,
            "stopped_reason": self.stopped_reason,
        }


__all__ = [
    "BoundedRollingStratifiedPolicy",
    "ResolvedTrainingProfile",
    "TrainingBudget",
    "TrainingSelection",
    "TrainingSetPolicy",
    "create_training_set_policy",
    "resolve_training_profile",
]
