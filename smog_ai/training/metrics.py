from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import (
    brier_score_loss,
    log_loss,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    roc_auc_score,
)


def regression_metrics(
    actual: np.ndarray,
    predicted: np.ndarray,
    *,
    persistence: np.ndarray | None = None,
    exceedance_threshold: float | None = None,
) -> dict[str, Any]:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    mask = np.isfinite(actual) & np.isfinite(predicted)
    actual, predicted = actual[mask], predicted[mask]
    if actual.size == 0:
        return {"count": 0, "mae": None, "rmse": None, "r2": None, "mape": None}
    mae = float(mean_absolute_error(actual, predicted))
    rmse = float(mean_squared_error(actual, predicted) ** 0.5)
    r2 = float(r2_score(actual, predicted)) if actual.size >= 2 else None
    nonzero = np.abs(actual) > 1e-6
    mape = float(np.mean(np.abs((actual[nonzero] - predicted[nonzero]) / actual[nonzero])) * 100) if nonzero.any() else None
    result: dict[str, Any] = {
        "count": int(actual.size),
        "mae": mae,
        "rmse": rmse,
        "r2": r2,
        "mape": mape,
        "bias": float(np.mean(predicted - actual)),
    }
    if persistence is not None:
        persistence = np.asarray(persistence, dtype=float)[mask]
        persistence_mask = np.isfinite(persistence)
        if persistence_mask.any():
            persistence_mae = float(mean_absolute_error(actual[persistence_mask], persistence[persistence_mask]))
            result["persistence_mae"] = persistence_mae
            result["mae_improvement_vs_persistence"] = (
                (persistence_mae - mae) / persistence_mae if persistence_mae > 0 else None
            )
    if exceedance_threshold is not None:
        actual_exc = actual >= exceedance_threshold
        pred_exc = predicted >= exceedance_threshold
        result["exceedance_accuracy"] = float(np.mean(actual_exc == pred_exc))
        result["exceedance_true_positives"] = int(np.sum(actual_exc & pred_exc))
        result["exceedance_false_positives"] = int(np.sum(~actual_exc & pred_exc))
        result["exceedance_false_negatives"] = int(np.sum(actual_exc & ~pred_exc))
    return result


def binary_probability_metrics(actual: np.ndarray, probability: np.ndarray) -> dict[str, Any]:
    actual = np.asarray(actual, dtype=float)
    probability = np.asarray(probability, dtype=float)
    mask = np.isfinite(actual) & np.isfinite(probability)
    actual = actual[mask].astype(int)
    probability = np.clip(probability[mask], 1e-9, 1 - 1e-9)
    if actual.size == 0:
        return {"count": 0, "brier": None, "log_loss": None, "roc_auc": None}
    result: dict[str, Any] = {
        "count": int(actual.size),
        "brier": float(brier_score_loss(actual, probability)),
        "log_loss": float(log_loss(actual, np.column_stack([1 - probability, probability]), labels=[0, 1])),
        "positive_rate": float(np.mean(actual)),
    }
    result["roc_auc"] = (
        float(roc_auc_score(actual, probability)) if len(np.unique(actual)) > 1 else None
    )
    return result
