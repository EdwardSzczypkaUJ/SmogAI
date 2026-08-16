from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from smog_ai.nlp.interpreter import (
    OpenAICompatibleIntentInterpreter,
    RuleBasedIntentInterpreter,
)
from smog_ai.observability.bridge import NoopObservability
from smog_ai.places.gazetteer import PolishGazetteerResolver


class _FakeStructuredInterpreter(OpenAICompatibleIntentInterpreter):
    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        response_format = body["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        assert response_format["json_schema"]["schema"]["additionalProperties"] is False
        payload = {
            "location": {
                "raw_text": "Mieroszów koło Wałbrzycha",
                "primary_name": "Mieroszów",
                "context_name": "Wałbrzych",
                "latitude": 50.66694,
                "longitude": 16.18972,
                "coordinate_precision": "locality_centroid",
                "coordinate_confidence": 0.9,
                "coordinate_basis": "znane centrum miejscowości",
            },
            "target_time": "2026-08-12T15:17:00+02:00",
            "pollutants": ["PM10", "PM2.5"],
            "language": "pl",
            "requested_view": "forecast",
            "time_was_explicit": True,
            "time_precision": "exact_minute",
            "confidence": 0.98,
            "assumptions": [],
        }
        return {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(payload, ensure_ascii=False)},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }


class _FakeResponsesUsageInterpreter(_FakeStructuredInterpreter):
    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        response = super()._request(body)
        response["usage"] = {"input_tokens": 12, "output_tokens": 34}
        return response


def _interpreter(
    interpreter_class: type[_FakeStructuredInterpreter] = _FakeStructuredInterpreter,
) -> _FakeStructuredInterpreter:
    return interpreter_class(
        model="gpt-4.1-mini",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        timeout_seconds=1,
        max_retries=0,
        temperature=0,
        timezone="Europe/Warsaw",
        fallback=None,
        observability=NoopObservability(),
        available_parameters=["PM10", "PM2.5"],
    )


def test_structured_output_preserves_primary_context_and_exact_minute() -> None:
    result = _interpreter().interpret(
        "Jutro w Mieroszowie koło Wałbrzycha około godziny 15:17. PM10 i PM2.5.",
        candidates=["Wałbrzych", "Rzeszów"],
        now=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
    )
    assert result.intent.location == "Mieroszów"
    assert result.intent.location_raw == "Mieroszów koło Wałbrzycha"
    assert result.intent.location_context == "Wałbrzych"
    assert result.intent.candidate_latitude == pytest.approx(50.66694)
    assert result.intent.candidate_longitude == pytest.approx(16.18972)
    assert result.intent.target_time.isoformat() == "2026-08-12T15:17:00+02:00"
    assert result.intent.reference_target_time is not None
    assert result.intent.reference_target_time.isoformat() == "2026-08-12T15:17:00+02:00"
    assert result.intent.time_precision == "exact_minute"


def test_responses_style_usage_is_normalised_for_cost_reporting() -> None:
    result = _interpreter(_FakeResponsesUsageInterpreter).interpret(
        "Jutro w Mieroszowie o 15:17. PM10 i PM2.5.",
        candidates=["Mieroszów"],
        now=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
    )
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 34


def test_rule_fallback_understands_okolo_godziny() -> None:
    result = RuleBasedIntentInterpreter(timezone="Europe/Warsaw").interpret(
        "Jutro w Mieroszowie około godziny 15:17 PM10",
        candidates=["Mieroszów"],
        now=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
    )
    assert (result.intent.target_time.hour, result.intent.target_time.minute) == (15, 17)
    assert result.intent.time_precision == "exact_minute"


def test_offline_gazetteer_resolves_mieroszow_and_rejects_numeric_guess() -> None:
    csv_path = Path(__file__).parents[1] / "smog_ai" / "resources" / "polish_places.csv"
    resolver = PolishGazetteerResolver(csv_path)
    place = resolver.resolve("Mieroszów")
    assert place.name == "Mieroszów"
    assert place.latitude == pytest.approx(50.66694)
    assert place.longitude == pytest.approx(16.18972)
    with pytest.raises(ValueError):
        resolver.resolve("151")
