"""DataFrame contracts backed by Pandera with an explicit degraded fallback."""

from smog_ai.data_validation.contracts import (
    DataFrameContractError,
    FrameValidationResult,
    PanderaFrameValidator,
    validate_frame,
)

__all__ = [
    "DataFrameContractError",
    "FrameValidationResult",
    "PanderaFrameValidator",
    "validate_frame",
]
