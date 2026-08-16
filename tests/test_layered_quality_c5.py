from __future__ import annotations

from types import SimpleNamespace

from smog_ai.hourly.trainer import _model_quality_classification
from smog_ai.quality import quality_metadata


def _config(minimum: float = 0.01):
    return SimpleNamespace(
        hourly_forecasting=SimpleNamespace(
            minimum_mae_improvement_fraction=minimum,
        )
    )


def test_generic_quality_classification_has_three_states() -> None:
    approved = _model_quality_classification(
        _config(),
        target="PM10",
        provider_name="ridge",
        metrics={
            "count": 100,
            "mae": 5.0,
            "improvement_vs_persistence": 0.10,
            "active_model_comparison": {"active_model_exists": False},
        },
    )
    experimental = _model_quality_classification(
        _config(),
        target="PM10",
        provider_name="ridge",
        metrics={
            "count": 100,
            "mae": 5.0,
            "improvement_vs_persistence": None,
            "active_model_comparison": {
                "active_model_exists": True,
                "available": True,
                "provider": "hist_gradient_boosting",
                "version": "active-v1",
                "candidate_improvement_fraction": -0.05,
            },
        },
    )
    rejected = _model_quality_classification(
        _config(),
        target="PM10",
        provider_name="ridge",
        metrics={
            "count": 0,
            "mae": None,
            "active_model_comparison": {"active_model_exists": False},
        },
    )
    assert approved["status"] == "approved"
    assert experimental["status"] == "experimental"
    assert rejected["status"] == "rejected"


def test_experimental_is_publishable_only_when_explicitly_allowed() -> None:
    metrics = {
        "quality_status": "experimental",
        "quality_classification": {
            "reasons": [{"reason": "insufficient_improvement_vs_active_model"}]
        },
    }
    blocked = quality_metadata("PM10", metrics, allowed="none")
    allowed = quality_metadata("PM10", metrics, allowed="*")
    assert blocked["experimental"] is True
    assert blocked["experimental_publication_allowed"] is False
    assert allowed["experimental_publication_allowed"] is True
    assert allowed["experimental_reason"]
