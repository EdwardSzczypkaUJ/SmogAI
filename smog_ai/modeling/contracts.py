from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np
import pandas as pd

ModelTask = Literal["regression", "classification", "hurdle_regression"]


@dataclass(slots=True)
class ModelFitContext:
    """Context passed to a model provider during fitting.

    The provider interface deliberately receives pandas objects and an explicit
    context instead of application/database objects.  This keeps model methods
    replaceable and allows an external package to implement a provider without
    depending on the rest of Smog AI.
    """

    target_name: str
    feature_columns: tuple[str, ...]
    task: ModelTask
    random_state: int = 42
    baseline_column: str | None = None
    sample_weight: np.ndarray | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelPredictContext:
    target_name: str
    feature_columns: tuple[str, ...]
    task: ModelTask
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PredictionBundle:
    """Provider-neutral prediction result.

    ``values`` is always the primary point forecast.  Providers may expose
    additional arrays (probability of rain, conditional rain amount, quantiles,
    etc.) in ``extras``.  All arrays must have the same row count.
    """

    values: np.ndarray
    extras: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=float)
        expected = len(self.values)
        for key, value in list(self.extras.items()):
            array = np.asarray(value, dtype=float)
            if len(array) != expected:
                raise ValueError(
                    f"Prediction extra {key!r} has {len(array)} rows; expected {expected}"
                )
            self.extras[key] = array


@runtime_checkable
class ModelProvider(Protocol):
    """Implementation side of the forecasting-model Bridge."""

    name: str
    task: ModelTask

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | pd.DataFrame,
        *,
        context: ModelFitContext,
    ) -> Any:
        ...

    def predict(
        self,
        artifact: Any,
        X: pd.DataFrame,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        ...

    def describe(self, artifact: Any) -> dict[str, Any]:
        ...
