"""Example external Smog AI model provider.

Use from config:

model_platform:
  plugin_modules: [examples.custom_model_plugin]

hourly_forecasting:
  target_algorithms:
    PM10: [persistence, robust_huber]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import HuberRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from smog_ai.modeling import (
    ModelFitContext,
    ModelPredictContext,
    PredictionBundle,
)


@dataclass(slots=True)
class RobustHuberProvider:
    name: str = "robust_huber"
    task: str = "regression"

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | pd.DataFrame,
        *,
        context: ModelFitContext,
    ) -> dict[str, Any]:
        columns = list(context.feature_columns)
        target = pd.Series(y).astype(float)
        model = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scale", StandardScaler()),
                ("regressor", HuberRegressor(max_iter=500)),
            ]
        )
        model.fit(X.reindex(columns=columns), target)
        return {
            "provider": self.name,
            "feature_columns": columns,
            "model": model,
        }

    def predict(
        self,
        artifact: dict[str, Any],
        X: pd.DataFrame,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        columns = list(artifact.get("feature_columns") or context.feature_columns)
        values = artifact["model"].predict(X.reindex(columns=columns))
        return PredictionBundle(np.asarray(values, dtype=float))

    def describe(self, artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": self.name,
            "task": self.task,
            "family": "robust_linear_regression",
            "feature_count": len(artifact.get("feature_columns") or []),
        }


def register_models(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(RobustHuberProvider())
