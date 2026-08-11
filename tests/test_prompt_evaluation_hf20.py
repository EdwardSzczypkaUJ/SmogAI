from __future__ import annotations

from smog_ai.observability.evaluation import (
    PromptExpectation,
    evaluate_query_response,
)


def test_prompt_evaluator_scores_exact_expected_response() -> None:
    result = evaluate_query_response(
        {
            "trace_id": "trace",
            "summary": "ok",
            "intent": {"parameters": ["PM10", "temperature_c"]},
            "place": {"name": "Katowice"},
            "forecasts": [
                {"parameter": "PM10", "exact_time_match": True},
                {"parameter": "temperature_c", "exact_time_match": True},
            ],
            "time_selection": {"all_selected_values_exact": True},
        },
        PromptExpectation(
            parameters=("PM10", "temperature_c"),
            place_contains="Katowice",
            require_exact_time=True,
            minimum_forecasts=2,
        ),
    )
    assert result.score == 1.0
    assert all(result.checks.values())


def test_prompt_evaluator_penalizes_wrong_place_and_missing_parameter() -> None:
    result = evaluate_query_response(
        {
            "trace_id": "trace",
            "summary": "ok",
            "intent": {"parameters": ["PM10"]},
            "place": {"name": "Kraków"},
            "forecasts": [{"parameter": "PM10", "exact_time_match": True}],
            "time_selection": {"all_selected_values_exact": True},
        },
        PromptExpectation(
            parameters=("PM10", "PM2.5"),
            place_contains="Katowice",
            require_exact_time=True,
        ),
    )
    assert result.score < 0.7
    assert result.checks["parameters"] is False
    assert result.checks["place"] is False
