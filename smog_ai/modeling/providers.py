from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

from smog_ai.modeling.contracts import (
    ModelFitContext,
    ModelPredictContext,
    ModelTask,
    PredictionBundle,
)
from smog_ai.modeling.registry import ModelProviderRegistry

EstimatorFactory = Callable[[ModelFitContext], Any]


def _fit_with_optional_sample_weight(
    estimator: Any,
    X: pd.DataFrame,
    y: np.ndarray | pd.Series,
    sample_weight: np.ndarray | None,
) -> bool:
    """Fit an estimator and use weights when the implementation supports them.

    Built-in sklearn pipelines expose the final step as ``model``.  External
    estimators may accept a direct ``sample_weight`` keyword.  A provider that
    does not support weights is still usable; the bounded/stratified selection
    already carries most of the protection against data-volume imbalance.
    """

    if sample_weight is None:
        estimator.fit(X, y)
        return False

    weights = np.asarray(sample_weight, dtype=float)
    if len(weights) != len(X):
        raise ValueError(
            f"sample_weight has {len(weights)} rows; expected {len(X)}"
        )

    try:
        if isinstance(estimator, Pipeline) and estimator.steps:
            final_step = estimator.steps[-1][0]
            estimator.fit(X, y, **{f"{final_step}__sample_weight": weights})
        else:
            estimator.fit(X, y, sample_weight=weights)
        return True
    except (TypeError, ValueError):
        estimator.fit(X, y)
        return False


@dataclass(slots=True)
class RegressionProvider:
    """Provider wrapper for arbitrary sklearn-compatible regressors.

    Factories receive the full fit context.  This is important for extension
    methods: a provider may react to the target, role, quantile, horizon range or
    user-defined method parameters without coupling the domain pipeline to a
    concrete ML library.
    """

    name: str
    factory: EstimatorFactory
    task: ModelTask = "regression"

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | pd.DataFrame,
        *,
        context: ModelFitContext,
    ) -> dict[str, Any]:
        target = pd.to_numeric(pd.Series(y), errors="coerce").to_numpy(dtype=float)
        estimator = self.factory(context)
        sample_weight_used = _fit_with_optional_sample_weight(
            estimator,
            X.loc[:, list(context.feature_columns)],
            target,
            context.sample_weight,
        )
        return {
            "provider": self.name,
            "task": self.task,
            "feature_columns": list(context.feature_columns),
            "estimator": estimator,
            "target_name": context.target_name,
            "fit_metadata": dict(context.metadata),
            "sample_weight_used": sample_weight_used,
        }

    def predict(
        self,
        artifact: dict[str, Any],
        X: pd.DataFrame,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        columns = list(artifact.get("feature_columns") or context.feature_columns)
        values = artifact["estimator"].predict(X.reindex(columns=columns))
        return PredictionBundle(np.asarray(values, dtype=float))

    def describe(self, artifact: Any) -> dict[str, Any]:
        artifact = artifact or {}
        estimator = artifact.get("estimator")
        return {
            "provider": self.name,
            "task": self.task,
            "estimator": type(estimator).__name__ if estimator is not None else None,
            "fit_metadata": artifact.get("fit_metadata") or {},
        }


@dataclass(slots=True)
class PersistenceProvider:
    name: str = "persistence"
    task: ModelTask = "regression"

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | pd.DataFrame,
        *,
        context: ModelFitContext,
    ) -> dict[str, Any]:
        column = context.baseline_column or "value"
        if column not in X.columns:
            raise ValueError(f"Persistence baseline column is absent: {column}")
        return {
            "provider": self.name,
            "task": self.task,
            "feature_columns": list(context.feature_columns),
            "baseline_column": column,
            "target_name": context.target_name,
        }

    def predict(
        self,
        artifact: dict[str, Any],
        X: pd.DataFrame,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        column = str(
            artifact.get("baseline_column")
            or context.metadata.get("baseline_column")
            or "value"
        )
        values = pd.to_numeric(X[column], errors="coerce").to_numpy(dtype=float)
        return PredictionBundle(values)

    def describe(self, artifact: Any) -> dict[str, Any]:
        return {
            "provider": self.name,
            "task": self.task,
            "baseline_column": (artifact or {}).get("baseline_column"),
        }


@dataclass(slots=True)
class HistoricalMeanProvider:
    name: str = "historical_mean"
    task: ModelTask = "regression"

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | pd.DataFrame,
        *,
        context: ModelFitContext,
    ) -> dict[str, Any]:
        target = pd.to_numeric(pd.Series(y), errors="coerce")
        weights = (
            np.asarray(context.sample_weight, dtype=float)
            if context.sample_weight is not None
            else None
        )
        valid = target.notna().to_numpy()
        if weights is not None and len(weights) == len(target) and valid.any():
            mean = float(np.average(target.to_numpy(dtype=float)[valid], weights=weights[valid]))
            sample_weight_used = True
        else:
            mean = float(target.mean())
            sample_weight_used = False
        return {
            "provider": self.name,
            "task": self.task,
            "feature_columns": list(context.feature_columns),
            "mean": mean,
            "target_name": context.target_name,
            "sample_weight_used": sample_weight_used,
        }

    def predict(
        self,
        artifact: dict[str, Any],
        X: pd.DataFrame,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        return PredictionBundle(np.full(len(X), float(artifact["mean"]), dtype=float))

    def describe(self, artifact: Any) -> dict[str, Any]:
        return {
            "provider": self.name,
            "task": self.task,
            "mean": (artifact or {}).get("mean"),
        }


@dataclass(slots=True)
class HurdlePrecipitationProvider:
    """Two-part precipitation model: occurrence probability and positive amount."""

    name: str = "hurdle_hist_gradient_boosting"
    task: ModelTask = "hurdle_regression"

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | pd.DataFrame,
        *,
        context: ModelFitContext,
    ) -> dict[str, Any]:
        columns = list(context.feature_columns)
        frame = X.reindex(columns=columns)
        target = (
            pd.to_numeric(pd.Series(y), errors="coerce")
            .fillna(0.0)
            .clip(lower=0.0)
        )
        threshold = float(context.metadata.get("occurrence_threshold_mm", 0.1))
        parameters = dict(context.metadata.get("method_parameters") or {})
        occurrence = (target > threshold).astype(int)
        probability_fallback = float(occurrence.mean())
        positive = target[occurrence.astype(bool)]

        classifier: Any | None = None
        minimum_rows = int(parameters.pop("minimum_occurrence_rows", 20))
        if occurrence.nunique() >= 2 and len(target) >= minimum_rows:
            classifier = Pipeline(
                [
                    ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                    (
                        "model",
                        HistGradientBoostingClassifier(
                            learning_rate=float(parameters.pop("occurrence_learning_rate", 0.06)),
                            max_iter=int(parameters.pop("occurrence_max_iter", 220)),
                            max_leaf_nodes=int(parameters.pop("occurrence_max_leaf_nodes", 31)),
                            l2_regularization=float(parameters.pop("occurrence_l2", 0.1)),
                            random_state=context.random_state,
                        ),
                    ),
                ]
            )
            _fit_with_optional_sample_weight(
                classifier,
                frame,
                occurrence.to_numpy(dtype=int),
                context.sample_weight,
            )

        amount_estimator: Any | None = None
        positive_mean = float(positive.mean()) if len(positive) else 0.0
        positive_mask = occurrence.astype(bool).to_numpy()
        minimum_positive_rows = int(
            context.metadata.get(
                "minimum_positive_rows",
                parameters.pop("minimum_positive_rows", 20),
            )
        )
        if positive_mask.sum() >= minimum_positive_rows and positive.nunique() >= 2:
            amount_estimator = Pipeline(
                [
                    ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                    (
                        "model",
                        HistGradientBoostingRegressor(
                            loss=str(parameters.pop("amount_loss", "gamma")),
                            learning_rate=float(parameters.pop("amount_learning_rate", 0.05)),
                            max_iter=int(parameters.pop("amount_max_iter", 240)),
                            max_leaf_nodes=int(parameters.pop("amount_max_leaf_nodes", 31)),
                            l2_regularization=float(parameters.pop("amount_l2", 0.1)),
                            random_state=context.random_state,
                        ),
                    ),
                ]
            )
            positive_weights = (
                np.asarray(context.sample_weight, dtype=float)[positive_mask]
                if context.sample_weight is not None
                else None
            )
            _fit_with_optional_sample_weight(
                amount_estimator,
                frame.loc[positive_mask],
                positive.to_numpy(dtype=float),
                positive_weights,
            )

        return {
            "provider": self.name,
            "task": self.task,
            "feature_columns": columns,
            "target_name": context.target_name,
            "occurrence_threshold_mm": threshold,
            "classifier": classifier,
            "amount_estimator": amount_estimator,
            "probability_fallback": probability_fallback,
            "positive_mean": positive_mean,
        }

    def predict(
        self,
        artifact: dict[str, Any],
        X: pd.DataFrame,
        *,
        context: ModelPredictContext,
    ) -> PredictionBundle:
        columns = list(artifact.get("feature_columns") or context.feature_columns)
        frame = X.reindex(columns=columns)
        classifier = artifact.get("classifier")
        if classifier is None:
            probability = np.full(
                len(frame), float(artifact.get("probability_fallback", 0.0))
            )
        else:
            probability = np.asarray(classifier.predict_proba(frame)[:, 1], dtype=float)
        amount_estimator = artifact.get("amount_estimator")
        if amount_estimator is None:
            conditional = np.full(
                len(frame), float(artifact.get("positive_mean", 0.0))
            )
        else:
            conditional = np.asarray(amount_estimator.predict(frame), dtype=float)
        probability = np.clip(probability, 0.0, 1.0)
        conditional = np.clip(conditional, 0.0, None)
        expected = probability * conditional
        return PredictionBundle(
            expected,
            extras={
                "precipitation_probability": probability,
                "precipitation_amount_if_rain_mm": conditional,
            },
        )

    def describe(self, artifact: Any) -> dict[str, Any]:
        artifact = artifact or {}
        return {
            "provider": self.name,
            "task": self.task,
            "occurrence_threshold_mm": artifact.get("occurrence_threshold_mm"),
            "classifier": (
                type(artifact.get("classifier")).__name__
                if artifact.get("classifier") is not None
                else None
            ),
            "amount_estimator": (
                type(artifact.get("amount_estimator")).__name__
                if artifact.get("amount_estimator") is not None
                else None
            ),
        }


def _parameters(context: ModelFitContext) -> dict[str, Any]:
    return dict(context.metadata.get("method_parameters") or {})


def _hist_gradient(context: ModelFitContext) -> Pipeline:
    parameters = _parameters(context)
    quantile = context.metadata.get("quantile")
    loss = str(parameters.pop("loss", "squared_error"))
    if quantile is not None:
        loss = "quantile"
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
            (
                "model",
                HistGradientBoostingRegressor(
                    loss=loss,
                    quantile=float(quantile) if quantile is not None else None,
                    learning_rate=float(parameters.pop("learning_rate", 0.06)),
                    max_iter=int(parameters.pop("max_iter", 280)),
                    max_leaf_nodes=int(parameters.pop("max_leaf_nodes", 31)),
                    l2_regularization=float(parameters.pop("l2_regularization", 0.1)),
                    random_state=context.random_state,
                    **parameters,
                ),
            ),
        ]
    )


def _mlp(context: ModelFitContext) -> Pipeline:
    parameters = _parameters(context)
    hidden = tuple(parameters.pop("hidden_layer_sizes", (96, 48)))
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
            ("scale", StandardScaler()),
            (
                "model",
                MLPRegressor(
                    hidden_layer_sizes=hidden,
                    activation=str(parameters.pop("activation", "relu")),
                    solver=str(parameters.pop("solver", "adam")),
                    early_stopping=bool(parameters.pop("early_stopping", True)),
                    max_iter=int(parameters.pop("max_iter", 450)),
                    random_state=context.random_state,
                    **parameters,
                ),
            ),
        ]
    )


def _polynomial_ridge(context: ModelFitContext) -> Pipeline:
    """Polynomial regression only for the horizon/time basis.

    Expanding every PM/weather lag to degree two is both expensive and difficult
    to interpret.  The platform therefore expands only the exact-horizon basis;
    the remaining features enter linearly.  External providers may replace this
    implementation with splines, GAMs or another regression family.
    """

    parameters = _parameters(context)
    horizon_columns = [
        column
        for column in (
            "horizon_hours",
            "horizon_squared",
            "horizon_sqrt",
            "horizon_log1p",
            "target_hour_sin",
            "target_hour_cos",
            "target_year_sin",
            "target_year_cos",
        )
        if column in context.feature_columns
    ]
    other_columns = [
        column for column in context.feature_columns if column not in horizon_columns
    ]
    transformers: list[tuple[str, Any, list[str]]] = []
    if horizon_columns:
        transformers.append(
            (
                "horizon_polynomial",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                        (
                            "polynomial",
                            PolynomialFeatures(
                                degree=int(parameters.pop("degree", 2)),
                                include_bias=False,
                                interaction_only=bool(
                                    parameters.pop("interaction_only", False)
                                ),
                            ),
                        ),
                        ("scale", StandardScaler()),
                    ]
                ),
                horizon_columns,
            )
        )
    if other_columns:
        transformers.append(
            (
                "other_features",
                Pipeline(
                    [
                        (
                            "imputer",
                            SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
                        ),
                        ("scale", StandardScaler()),
                    ]
                ),
                other_columns,
            )
        )
    return Pipeline(
        [
            ("features", ColumnTransformer(transformers, remainder="drop")),
            ("model", Ridge(alpha=float(parameters.pop("alpha", 2.0)), **parameters)),
        ]
    )


def _ridge(context: ModelFitContext) -> Pipeline:
    parameters = _parameters(context)
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
            ("scale", StandardScaler()),
            ("model", Ridge(alpha=float(parameters.pop("alpha", 1.0)), **parameters)),
        ]
    )


def register_builtin_models(registry: ModelProviderRegistry) -> None:
    registry.register(PersistenceProvider())
    registry.register(HistoricalMeanProvider())
    registry.register(RegressionProvider("ridge", _ridge))
    registry.register(RegressionProvider("polynomial_ridge", _polynomial_ridge))
    registry.register(RegressionProvider("hist_gradient_boosting", _hist_gradient))
    # Separate name makes the role explicit in configuration/model cards while
    # reusing the same factory. The context supplies the requested quantile.
    registry.register(
        RegressionProvider("hist_gradient_boosting_quantile", _hist_gradient)
    )
    registry.register(RegressionProvider("mlp", _mlp))
    registry.register(HurdlePrecipitationProvider())
