from __future__ import annotations

import difflib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from smog_ai.air_parameters import (
    WEATHER_DERIVED_TARGETS,
    WEATHER_TARGETS,
    canonical_code,
    common_aliases_for,
)
from smog_ai.nlp.models import (
    AirQualityIntent,
    InterpretationResult,
    StructuredIntentOutput,
)
from smog_ai.observability.bridge import ObservabilityBridge

logger = logging.getLogger(__name__)


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character) and character.isalnum()
    )


def _local_now(now: datetime | None, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _extract_hour(text: str, default: int = 12) -> tuple[int, int, bool, str]:
    normalized = text.casefold()
    match = re.search(
        r"\b(?:o|około|okolo)\s*(?:godzin(?:a|ę|e|y)?\s*)?(\d{1,2})(?::(\d{2}))?\b",
        normalized,
    )
    if match:
        hour = max(0, min(23, int(match.group(1))))
        minute = max(0, min(59, int(match.group(2) or 0)))
        precision = "exact_minute" if minute else "hour"
        return hour, minute, True, precision
    if "rano" in normalized:
        return 8, 0, False, "part_of_day"
    if "wiecz" in normalized:
        return 18, 0, False, "part_of_day"
    if "noc" in normalized:
        return 22, 0, False, "part_of_day"
    if "po południu" in normalized or "popoludniu" in _normalize(normalized):
        return 15, 0, False, "part_of_day"
    return default, 0, False, "date_only"


def _target_time(
    text: str,
    *,
    now: datetime | None,
    timezone: str,
) -> tuple[datetime, list[str], bool, str]:
    local = _local_now(now, timezone)
    normalized = text.casefold()
    assumptions: list[str] = []
    duration = re.search(r"\bza\s+(\d{1,3})\s*(?:h|godz|godzin|godziny)\b", normalized)
    if duration:
        return (
            local + timedelta(hours=int(duration.group(1))),
            assumptions,
            True,
            "relative_duration",
        )
    date_match = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\b", normalized)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = int(date_match.group(3) or local.year)
        hour, minute, explicit, precision = _extract_hour(text)
        if not explicit:
            assumptions.append(
                "Podano datę bez dokładnej godziny; odpowiedź zawiera profil dnia, "
                "a mapa startuje od 12:00 czasu lokalnego."
            )
        return (
            datetime(year, month, day, hour, minute, tzinfo=local.tzinfo),
            assumptions,
            explicit,
            precision,
        )
    offset = 0
    if "pojutrze" in normalized:
        offset = 2
    elif "jutro" in normalized:
        offset = 1
    elif "dzis" in _normalize(normalized):
        offset = 0
    else:
        assumptions.append(
            "Nie podano terminu; wyświetlono profil od najbliższej dostępnej pełnej godziny."
        )
        rounded = local.replace(minute=0, second=0, microsecond=0)
        if rounded < local:
            rounded += timedelta(hours=1)
        return rounded, assumptions, False, "unspecified"
    hour, minute, explicit, precision = _extract_hour(text)
    if not explicit:
        assumptions.append(
            "Nie podano dokładnej godziny; odpowiedź zawiera profil godzinowy dnia, "
            f"a mapa startuje od {hour:02d}:{minute:02d}."
        )
    target_date = (local + timedelta(days=offset)).date()
    return (
        datetime.combine(target_date, dt_time(hour=hour, minute=minute), tzinfo=local.tzinfo),
        assumptions,
        explicit,
        precision,
    )

def _extract_location(text: str, candidates: list[str]) -> str:
    """Extract a city/station while tolerating common Polish declension.

    A textbox contains forms such as ``do Katowic`` or ``w Krakowie`` while the
    snapshot stores nominative names. Exact substring matching is therefore only
    the first step; a high-confidence fuzzy comparison maps the extracted phrase
    back to a known station/city candidate.
    """

    normalized_text = _normalize(text)
    matches = [
        candidate
        for candidate in candidates
        if _normalize(candidate) and _normalize(candidate) in normalized_text
    ]
    if matches:
        return max(matches, key=len)

    extracted: str | None = None
    patterns = [
        r"\b(?:do|w|we|dla)\s+([A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ-]*)?)",
        r"\b(?:do|w|we|dla)\s+([a-ząćęłńóśźż-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            extracted = match.group(1).strip(" ,.!?")
            break

    if extracted and candidates:
        normalized_extracted = _normalize(extracted)
        scored = [
            (
                difflib.SequenceMatcher(
                    None, normalized_extracted, _normalize(candidate)
                ).ratio(),
                candidate,
            )
            for candidate in candidates
            if _normalize(candidate)
        ]
        score, candidate = max(scored, default=(0.0, extracted), key=lambda item: item[0])
        # 0.80 maps Katowic -> Katowice and Krakowie -> Kraków, while avoiding
        # broad guesses for unrelated free-form words.
        if score >= 0.80:
            return candidate
    if extracted:
        return extracted
    words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]+", text)
    return words[-1] if words else "Polska"


def _extract_coordinates(text: str) -> tuple[float, float] | None:
    """Read an explicit WGS84 pair without asking a language model to invent it."""

    match = re.search(
        r"(?<![\d.])([+-]?(?:\d{1,2}(?:[.,]\d+)?|90(?:[.,]0+)?))\s*[,; ]\s*"
        r"([+-]?(?:\d{1,3}(?:[.,]\d+)?|180(?:[.,]0+)?))(?![\d.])",
        text,
    )
    if not match:
        return None
    latitude = float(match.group(1).replace(",", "."))
    longitude = float(match.group(2).replace(",", "."))
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def _canonical_available_parameters(
    available_parameters: list[str] | tuple[str, ...] | None,
) -> list[str]:
    values = available_parameters or ["PM10", "PM2.5"]
    output: list[str] = []
    for raw in values:
        code = canonical_code(raw)
        if (
            code != "UNKNOWN"
            and code not in WEATHER_TARGETS
            and code not in WEATHER_DERIVED_TARGETS
            and code not in output
        ):
            output.append(code)
    return output or ["PM10", "PM2.5"]


def _normalise_parameter_aliases(
    available: list[str],
    parameter_aliases: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    output: dict[str, tuple[str, ...]] = {}
    raw_mapping = parameter_aliases or {}
    for code in available:
        values: list[str] = []
        for raw_key, raw_aliases in raw_mapping.items():
            if canonical_code(str(raw_key)) != code:
                continue
            for alias in raw_aliases:
                cleaned = str(alias).strip()
                if cleaned and cleaned not in values:
                    values.append(cleaned)
        output[code] = tuple(values)
    return output


def _alias_occurs(text: str, alias: str) -> bool:
    raw = str(alias).strip()
    if not raw:
        return False
    upper_text = text.upper().replace(",", ".")
    upper_alias = raw.upper().replace(",", ".")
    code_like = bool(re.fullmatch(r"[A-Z0-9.₀-₉]+", upper_alias))
    if code_like:
        pattern = rf"(?<![A-Z0-9]){re.escape(upper_alias)}(?![A-Z0-9])"
        return re.search(pattern, upper_text) is not None
    return _normalize(raw) in _normalize(text)


def _extract_air_parameters(
    text: str,
    available_parameters: list[str] | tuple[str, ...] | None,
    parameter_aliases: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    available = _canonical_available_parameters(available_parameters)
    configured_aliases = _normalise_parameter_aliases(
        available, parameter_aliases
    )
    selected: list[str] = []
    for code in available:
        aliases = (
            *configured_aliases.get(code, ()),
            *common_aliases_for(code),
            code,
        )
        if any(_alias_occurs(text, alias) for alias in aliases) and code not in selected:
            selected.append(code)
    return selected or available


class IntentInterpreter(Protocol):
    provider_name: str

    def interpret(
        self,
        text: str,
        *,
        candidates: list[str],
        now: datetime | None = None,
        available_parameters: list[str] | tuple[str, ...] | None = None,
        parameter_aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> InterpretationResult:
        ...


class RuleBasedIntentInterpreter:
    provider_name = "rule_based"

    def __init__(
        self,
        *,
        timezone: str = "Europe/Warsaw",
        available_parameters: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.timezone = timezone
        self.available_parameters = _canonical_available_parameters(
            available_parameters
        )

    def interpret(
        self,
        text: str,
        *,
        candidates: list[str],
        now: datetime | None = None,
        available_parameters: list[str] | tuple[str, ...] | None = None,
        parameter_aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> InterpretationResult:
        started = time.perf_counter()
        target, assumptions, time_was_explicit, time_precision = _target_time(
            text, now=now, timezone=self.timezone
        )
        pollutants = _extract_air_parameters(
            text,
            available_parameters or self.available_parameters,
            parameter_aliases,
        )
        normalized = _normalize(text)
        coordinates = _extract_coordinates(text)
        view = "current" if any(marker in normalized for marker in ("teraz", "aktualn", "obecnie")) else "forecast"
        if view == "forecast" and not time_was_explicit:
            view = "daily_profile"
        intent = AirQualityIntent(
            location=(
                f"{coordinates[0]:.6f}, {coordinates[1]:.6f}"
                if coordinates
                else _extract_location(text, candidates)
            ),
            latitude=coordinates[0] if coordinates else None,
            longitude=coordinates[1] if coordinates else None,
            location_source="text_coordinates" if coordinates else "text",
            location_precision="exact_coordinates" if coordinates else "unresolved",
            target_time=target,
            reference_target_time=target,
            reference_time_precision=time_precision,
            pollutants=pollutants,
            requested_view=view,
            time_was_explicit=time_was_explicit,
            time_precision=time_precision,
            confidence=0.72 if candidates else 0.55,
            assumptions=assumptions,
        )
        return InterpretationResult(
            intent=intent,
            provider=self.provider_name,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class OpenAICompatibleIntentInterpreter:
    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        temperature: float,
        timezone: str,
        fallback: IntentInterpreter | None,
        observability: ObservabilityBridge,
        available_parameters: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("LLM API key is required")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature
        self.timezone = timezone
        self.fallback = fallback
        self.observability = observability
        self.available_parameters = _canonical_available_parameters(
            available_parameters
        )

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        transport = httpx.HTTPTransport(retries=self.max_retries)
        with httpx.Client(transport=transport, timeout=self.timeout_seconds) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            return response.json()

    def interpret(
        self,
        text: str,
        *,
        candidates: list[str],
        now: datetime | None = None,
        available_parameters: list[str] | tuple[str, ...] | None = None,
        parameter_aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> InterpretationResult:
        started = time.perf_counter()
        local_now = _local_now(now, self.timezone)
        available = _canonical_available_parameters(
            available_parameters or self.available_parameters
        )
        configured_aliases = _normalise_parameter_aliases(
            available, parameter_aliases
        )
        parameter_text = ", ".join(available)
        parameter_catalog = {
            code: list(
                dict.fromkeys(
                    (
                        *configured_aliases.get(code, ()),
                        *common_aliases_for(code),
                        code,
                    )
                )
            )
            for code in available
        }
        system = (
            "Jesteś ścisłym parserem polskich zapytań pogodowych i o jakość powietrza. "
            "Wyodrębnij frazę lokalizacji bez zamieniania jej na podobnie brzmiące miasto. "
            "Dla 'Mieroszów koło Wałbrzycha' ustaw primary_name='Mieroszów', "
            "context_name='Wałbrzych', a raw_text na pełną frazę. "
            "Dla nazwanego POI zachowaj typ i pełną nazwę obiektu: dla "
            "'lotnisko Witków koło Mieroszowa' ustaw primary_name='Lotnisko Witków', "
            "context_name='Mieroszów', a nie primary_name='Witków'. "
            "Podaj również najlepsze znane współrzędne WGS84 jako propozycję, ich precyzję, "
            "pewność i krótką podstawę. Jeżeli nie znasz ich dostatecznie dobrze, zwróć null "
            "dla latitude i longitude oraz coordinate_precision='unknown'. "
            "Rozwiąż datę i godzinę względem current_local_time i timezone; zachowaj dokładne minuty. "
            f"pollutants mogą zawierać wyłącznie: {parameter_text}. "
            "Nie przedstawiaj przybliżonych współrzędnych jako dokładnego punktu użytkownika. "
            "Nie wymyślaj pomiarów ani prognoz. Gdy brak godziny, ustaw "
            "requested_view=daily_profile i time_was_explicit=false."
        )
        user = {
            "query": text,
            "current_local_time": local_now.isoformat(),
            "timezone": self.timezone,
            "known_locations_are_only_hints": sorted(set(candidates))[:250],
            "available_air_parameters": available,
            "air_parameter_aliases": parameter_catalog,
        }
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "smog_ai_intent_v2",
                    "strict": True,
                    "schema": StructuredIntentOutput.model_json_schema(),
                },
            },
        }
        try:
            with self.observability.observation(
                name="air-quality-intent-extraction",
                as_type="generation",
                model=self.model,
                input={"text": text, "current_local_time": local_now.isoformat()},
                metadata={
                    "provider": self.provider_name,
                    "structured_outputs": True,
                    "schema": "smog_ai_intent_v2",
                },
            ) as observation:
                response = self._request(body)
                choice = response["choices"][0]
                message = choice["message"]
                refusal = message.get("refusal")
                if refusal:
                    raise RuntimeError(f"Model refused intent extraction: {refusal}")
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("Model returned an empty structured response")
                structured = StructuredIntentOutput.model_validate_json(content)
                deterministic_target = _target_time(
                    text, now=now, timezone=self.timezone
                )
                raw_pollutants = structured.pollutants
                alias_lookup = {
                    _normalize(alias): code
                    for code, aliases in parameter_catalog.items()
                    for alias in aliases
                    if _normalize(alias)
                }
                resolved_pollutants: list[str] = []
                for raw in raw_pollutants:
                    code = alias_lookup.get(
                        _normalize(str(raw)),
                        canonical_code(str(raw)),
                    )
                    if code in available and code not in resolved_pollutants:
                        resolved_pollutants.append(code)
                coordinates = _extract_coordinates(text)
                intent = AirQualityIntent(
                    location=(
                        f"{coordinates[0]:.6f}, {coordinates[1]:.6f}"
                        if coordinates
                        else structured.location.primary_name
                    ),
                    location_raw=structured.location.raw_text,
                    location_context=structured.location.context_name,
                    candidate_latitude=(
                        None if coordinates else structured.location.latitude
                    ),
                    candidate_longitude=(
                        None if coordinates else structured.location.longitude
                    ),
                    candidate_coordinate_precision=structured.location.coordinate_precision,
                    candidate_coordinate_confidence=structured.location.coordinate_confidence,
                    candidate_coordinate_basis=structured.location.coordinate_basis,
                    latitude=coordinates[0] if coordinates else None,
                    longitude=coordinates[1] if coordinates else None,
                    location_source="text_coordinates" if coordinates else "text",
                    location_precision="exact_coordinates" if coordinates else "unresolved",
                    target_time=structured.target_time,
                    reference_target_time=(
                        deterministic_target[0] if deterministic_target[2] else None
                    ),
                    reference_time_precision=(
                        deterministic_target[3] if deterministic_target[2] else None
                    ),
                    pollutants=resolved_pollutants or available,
                    language=structured.language,
                    requested_view=structured.requested_view,
                    time_was_explicit=structured.time_was_explicit,
                    time_precision=structured.time_precision,
                    confidence=structured.confidence,
                    assumptions=structured.assumptions,
                )
                usage = response.get("usage") or {}
                observation.update(
                    output=intent.model_dump(mode="json"),
                    metadata={
                        "usage": usage,
                        "finish_reason": choice.get("finish_reason"),
                        "location_primary": intent.location,
                        "location_context": intent.location_context,
                        "coordinate_candidate": {
                            "latitude": intent.candidate_latitude,
                            "longitude": intent.candidate_longitude,
                            "confidence": intent.candidate_coordinate_confidence,
                            "precision": intent.candidate_coordinate_precision,
                        },
                    },
                )
            return InterpretationResult(
                intent=intent,
                provider=self.provider_name,
                model=self.model,
                latency_ms=(time.perf_counter() - started) * 1000,
                prompt_tokens=(
                    usage.get("prompt_tokens")
                    if usage.get("prompt_tokens") is not None
                    else usage.get("input_tokens")
                ),
                completion_tokens=(
                    usage.get("completion_tokens")
                    if usage.get("completion_tokens") is not None
                    else usage.get("output_tokens")
                ),
                raw_response={"id": response.get("id"), "usage": usage},
            )
        except Exception as exc:
            logger.warning("LLM intent extraction failed: %s", exc)
            if self.fallback is None:
                raise
            result = self.fallback.interpret(
                text,
                candidates=candidates,
                now=now,
                available_parameters=available_parameters or self.available_parameters,
                parameter_aliases=parameter_aliases,
            )
            result.fallback_used = True
            result.raw_response = {"llm_error": str(exc)}
            return result


def create_intent_interpreter(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    max_retries: int,
    temperature: float,
    timezone: str,
    allow_rule_based_fallback: bool,
    observability: ObservabilityBridge,
    available_parameters: list[str] | tuple[str, ...] | None = None,
) -> IntentInterpreter:
    fallback = RuleBasedIntentInterpreter(
        timezone=timezone,
        available_parameters=available_parameters,
    )
    if provider == "rule_based":
        return fallback
    if provider in {"openai", "openai_compatible"}:
        if not api_key:
            if allow_rule_based_fallback:
                logger.warning("LLM API key is missing; using deterministic fallback")
                return fallback
            raise RuntimeError("LLM API key is missing")
        return OpenAICompatibleIntentInterpreter(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            temperature=temperature,
            timezone=timezone,
            fallback=fallback if allow_rule_based_fallback else None,
            observability=observability,
            available_parameters=available_parameters,
        )
    raise ValueError(f"Unsupported NLP provider: {provider}")
