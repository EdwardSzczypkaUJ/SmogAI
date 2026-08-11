from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PromptExpectation:
    parameters: tuple[str, ...] = ()
    place_contains: str | None = None
    require_exact_time: bool = False
    minimum_forecasts: int = 1


@dataclass(slots=True)
class PromptEvaluationResult:
    score: float
    checks: dict[str, bool]
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_query_response(
    payload: dict[str, Any],
    expectation: PromptExpectation,
) -> PromptEvaluationResult:
    """Deterministically score the structural quality of a query response.

    The evaluator intentionally avoids an LLM-as-a-judge call.  It can run in
    CI or local-only mode without external cost, while the resulting score can
    optionally be attached to the Langfuse trace by the API feedback endpoint.
    """

    intent = dict(payload.get("intent") or {})
    place = dict(payload.get("place") or {})
    forecasts = list(payload.get("forecasts") or [])
    time_selection = dict(payload.get("time_selection") or {})

    actual_parameters = {
        str(value)
        for value in (intent.get("parameters") or [])
    }
    expected_parameters = set(expectation.parameters)
    parameter_match = (
        expected_parameters.issubset(actual_parameters)
        if expected_parameters
        else True
    )
    place_name = str(place.get("name") or "")
    place_match = (
        expectation.place_contains.casefold() in place_name.casefold()
        if expectation.place_contains
        else True
    )
    forecast_count = len(forecasts)
    forecast_count_ok = forecast_count >= expectation.minimum_forecasts
    exact_time = bool(
        time_selection.get("all_selected_values_exact")
        or (
            forecasts
            and all(bool(item.get("exact_time_match")) for item in forecasts)
        )
    )
    exact_time_ok = exact_time if expectation.require_exact_time else True
    has_trace = bool(payload.get("trace_id"))
    has_summary = bool(str(payload.get("summary") or "").strip())

    checks = {
        "parameters": parameter_match,
        "place": place_match,
        "forecast_count": forecast_count_ok,
        "exact_time": exact_time_ok,
        "trace": has_trace,
        "summary": has_summary,
    }
    weights = {
        "parameters": 0.25,
        "place": 0.20,
        "forecast_count": 0.20,
        "exact_time": 0.20,
        "trace": 0.05,
        "summary": 0.10,
    }
    score = sum(weights[name] for name, passed in checks.items() if passed)
    return PromptEvaluationResult(
        score=round(float(score), 6),
        checks=checks,
        details={
            "expected_parameters": sorted(expected_parameters),
            "actual_parameters": sorted(actual_parameters),
            "expected_place_contains": expectation.place_contains,
            "actual_place": place_name,
            "forecast_count": forecast_count,
            "minimum_forecasts": expectation.minimum_forecasts,
            "exact_time_observed": exact_time,
        },
    )
