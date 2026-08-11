from __future__ import annotations

import numpy as np

from smog_ai.hourly.trainer import (
    _precipitation_metrics,
    _precipitation_quality_gate,
)


def test_precipitation_metrics_include_amount_and_occurrence_baselines(app_config) -> None:  # type: ignore[no-untyped-def]
    actual = np.array([0.0, 0.0, 1.0, 2.0, 0.0, 3.0, 0.0, 1.5])
    expected = np.array([0.0, 0.1, 0.9, 1.8, 0.0, 2.5, 0.1, 1.2])
    probability = np.array([0.05, 0.10, 0.80, 0.85, 0.05, 0.90, 0.10, 0.75])
    persistence = np.array([0.0, 0.0, 0.0, 1.0, 2.0, 0.0, 3.0, 0.0])

    metrics = _precipitation_metrics(
        actual,
        expected,
        probability,
        threshold=0.1,
        persistence_expected=persistence,
    )

    assert metrics["improvement_vs_persistence"] is not None
    assert metrics["brier_skill_vs_climatology"] is not None
    assert metrics["brier_skill_vs_persistence"] is not None
    assert metrics["wet_mae"] is not None
    assert metrics["dry_false_amount_mean"] is not None

    gate = _precipitation_quality_gate(app_config, metrics)
    assert "thresholds" in gate
    assert gate["status"] in {"accepted", "experimental"}


def test_precipitation_gate_rejects_missing_baseline_metrics(app_config) -> None:  # type: ignore[no-untyped-def]
    gate = _precipitation_quality_gate(
        app_config,
        {
            "bias": 0.0,
            "roc_auc": 0.8,
            "improvement_vs_persistence": None,
            "brier_skill_vs_climatology": None,
            "brier_skill_vs_persistence": None,
        },
    )

    assert gate["passed"] is False
    names = {row["metric"] for row in gate["failures"]}
    assert "improvement_vs_persistence" in names
    assert "brier_skill_vs_climatology" in names
    assert "brier_skill_vs_persistence" in names
