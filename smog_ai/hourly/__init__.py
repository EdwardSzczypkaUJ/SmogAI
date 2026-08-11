"""Exact-hour, horizon-conditioned multi-target forecasting.

The package intentionally avoids importing trainer/predictor modules at import
 time.  Those modules depend on artifact repositories which in turn import the
 feature builders.  Keeping ``__init__`` lightweight prevents a circular import
 while preserving explicit public imports from the concrete submodules.
"""

from smog_ai.hourly.features import (
    HORIZON_FEATURE_COLUMNS,
    PM_HOURLY_FEATURE_COLUMNS,
    WEATHER_HOURLY_FEATURE_COLUMNS,
)
from smog_ai.hourly.temporal import TemporalInterpolationResult, interpolate_temporally

__all__ = [
    "HORIZON_FEATURE_COLUMNS",
    "PM_HOURLY_FEATURE_COLUMNS",
    "WEATHER_HOURLY_FEATURE_COLUMNS",
    "TemporalInterpolationResult",
    "interpolate_temporally",
]
