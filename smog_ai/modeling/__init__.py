"""Open forecasting-model platform (Bridge + registry + plugin entry points)."""

from smog_ai.modeling.contracts import (
    ModelFitContext,
    ModelPredictContext,
    ModelProvider,
    PredictionBundle,
)
from smog_ai.modeling.registry import ModelProviderRegistry, create_model_registry

__all__ = [
    "ModelFitContext",
    "ModelPredictContext",
    "ModelProvider",
    "ModelProviderRegistry",
    "PredictionBundle",
    "create_model_registry",
]
