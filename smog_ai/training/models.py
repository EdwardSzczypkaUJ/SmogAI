from __future__ import annotations

from typing import Any

from sklearn.compose import TransformedTargetRegressor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


def make_regressor(algorithm: str, *, random_state: int = 42) -> Any:
    if algorithm == "hist_gradient_boosting":
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                (
                    "model",
                    HistGradientBoostingRegressor(
                        learning_rate=0.06,
                        max_iter=250,
                        max_leaf_nodes=31,
                        l2_regularization=0.1,
                        random_state=random_state,
                    ),
                ),
            ]
        )
    if algorithm == "mlp":
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scale", StandardScaler()),
                (
                    "model",
                    MLPRegressor(
                        hidden_layer_sizes=(64, 32),
                        activation="relu",
                        solver="adam",
                        early_stopping=True,
                        max_iter=400,
                        random_state=random_state,
                    ),
                ),
            ]
        )
    raise ValueError(f"Unsupported trainable algorithm: {algorithm}")
