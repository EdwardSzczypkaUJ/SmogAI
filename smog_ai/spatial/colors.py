from __future__ import annotations

import numpy as np

_PM_STOPS = np.asarray([0.0, 10.0, 20.0, 35.0, 50.0, 75.0, 100.0, 150.0, 250.0])
_PM_COLORS = np.asarray(
    [
        [24, 55, 76],
        [23, 167, 164],
        [54, 202, 105],
        [214, 227, 69],
        [255, 181, 50],
        [244, 92, 67],
        [193, 55, 111],
        [111, 45, 145],
        [55, 24, 74],
    ],
    dtype=float,
)
_TEMPERATURE_STOPS = np.asarray([-30.0, -15.0, -5.0, 5.0, 15.0, 25.0, 35.0, 45.0])
_TEMPERATURE_COLORS = np.asarray(
    [
        [32, 46, 128],
        [50, 101, 190],
        [79, 176, 224],
        [171, 225, 238],
        [247, 245, 188],
        [253, 174, 97],
        [215, 48, 39],
        [103, 0, 31],
    ],
    dtype=float,
)
_RAIN_STOPS = np.asarray([0.0, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0])
_RAIN_COLORS = np.asarray(
    [
        [30, 42, 59],
        [219, 238, 244],
        [171, 217, 233],
        [116, 173, 209],
        [69, 117, 180],
        [49, 54, 149],
        [84, 39, 143],
        [132, 18, 150],
        [79, 0, 125],
    ],
    dtype=float,
)
_PROBABILITY_STOPS = np.asarray([0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
_PROBABILITY_COLORS = np.asarray(
    [
        [32, 42, 58],
        [215, 234, 244],
        [158, 202, 225],
        [83, 145, 196],
        [49, 75, 155],
        [82, 37, 145],
        [48, 18, 92],
    ],
    dtype=float,
)


def scale_for(parameter: str) -> tuple[np.ndarray, np.ndarray]:
    key = str(parameter).casefold()
    if key == "temperature_c":
        return _TEMPERATURE_STOPS, _TEMPERATURE_COLORS
    if key == "precipitation_mm":
        return _RAIN_STOPS, _RAIN_COLORS
    if key == "precipitation_probability":
        return _PROBABILITY_STOPS, _PROBABILITY_COLORS
    return _PM_STOPS, _PM_COLORS


def rgba_for_values(
    values: np.ndarray,
    confidence: np.ndarray,
    *,
    parameter: str = "PM10",
) -> np.ndarray:
    """Return parameter-aware RGBA rows for a precomputed spatial surface."""

    stops, colors = scale_for(parameter)
    numeric = np.asarray(values, dtype=float)
    conf = np.clip(np.asarray(confidence, dtype=float), 0.0, 1.0)
    rgba = np.zeros((numeric.size, 4), dtype=np.uint8)
    valid = np.isfinite(numeric)
    if not valid.any():
        return rgba
    clipped = np.clip(numeric[valid], stops[0], stops[-1])
    for channel in range(3):
        rgba[valid, channel] = np.rint(
            np.interp(clipped, stops, colors[:, channel])
        ).astype(np.uint8)
    rgba[valid, 3] = np.rint(55.0 + 190.0 * conf[valid]).astype(np.uint8)
    return rgba


def category_for(parameter: str, value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "brak danych"
    key = str(parameter).casefold()
    numeric = float(value)
    if key == "temperature_c":
        if numeric < -10:
            return "bardzo zimno"
        if numeric < 0:
            return "mróz"
        if numeric < 10:
            return "chłodno"
        if numeric < 20:
            return "umiarkowanie"
        if numeric < 30:
            return "ciepło"
        return "gorąco"
    if key == "precipitation_probability":
        if numeric < 0.2:
            return "mało prawdopodobny"
        if numeric < 0.5:
            return "możliwy"
        if numeric < 0.8:
            return "prawdopodobny"
        return "bardzo prawdopodobny"
    if key == "precipitation_mm":
        if numeric < 0.1:
            return "brak opadu"
        if numeric < 1.0:
            return "słaby opad"
        if numeric < 5.0:
            return "umiarkowany opad"
        if numeric < 15.0:
            return "silny opad"
        return "bardzo silny opad"
    if numeric <= 20:
        return "niskie"
    if numeric <= 40:
        return "umiarkowane"
    if numeric <= 70:
        return "podwyższone"
    if numeric <= 120:
        return "wysokie"
    return "bardzo wysokie"


def unit_for(
    parameter: str,
    *,
    precipitation_accumulation_period_hours: int = 6,
) -> str:
    """Return the physical unit without inventing hourly rainfall.

    IMGW ``WO6G`` is, by default, a six-hour accumulation ending at the
    observation/target time.  The period is configurable and is propagated in
    every forecast/surface payload.
    """

    key = str(parameter).casefold()
    if key == "temperature_c":
        return "°C"
    if key == "precipitation_mm":
        return f"mm/{int(precipitation_accumulation_period_hours)}h"
    if key == "precipitation_probability":
        return "probability"
    return "µg/m³"
