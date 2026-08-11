from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from smog_ai.config import AppConfig, AirParameterConfig


WEATHER_TARGETS = frozenset({"temperature_c", "precipitation_mm"})
WEATHER_DERIVED_TARGETS = frozenset({"precipitation_probability"})

# Common aliases are deliberately independent from AppConfig.  They are used by
# lightweight clients such as the public query parser, which reads already
# published artifacts and does not load the local training configuration.
COMMON_AIR_ALIASES: dict[str, tuple[str, ...]] = {
    "PM10": ("PM10", "PYŁ ZAWIESZONY PM10", "PYL ZAWIESZONY PM10"),
    "PM2.5": (
        "PM2.5",
        "PM25",
        "PM2,5",
        "PYŁ ZAWIESZONY PM2.5",
        "PYL ZAWIESZONY PM2.5",
        "PYŁ PM2.5",
    ),
    "NO2": ("NO2", "NO₂", "DWUTLENEK AZOTU"),
    "SO2": ("SO2", "SO₂", "DWUTLENEK SIARKI"),
    "O3": ("O3", "O₃", "OZON"),
    "CO": ("CO", "TLENEK WĘGLA", "TLENEK WEGLA"),
    "C6H6": ("C6H6", "C₆H₆", "BENZEN"),
    "NO": ("NO", "TLENEK AZOTU"),
    "NOX": ("NOX", "NOx", "TLENKI AZOTU"),
}


def _ascii(value: str) -> str:
    # Unicode NFKD removes combining accents (ą, ę, ó, etc.) but Polish ł/Ł
    # is a standalone letter and therefore requires an explicit transliteration.
    translated = value.translate(
        str.maketrans(
            {
                "ł": "l",
                "Ł": "L",
                "₀": "0",
                "₁": "1",
                "₂": "2",
                "₃": "3",
                "₄": "4",
                "₅": "5",
                "₆": "6",
                "₇": "7",
                "₈": "8",
                "₉": "9",
            }
        )
    )
    normalized = unicodedata.normalize("NFKD", translated)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def alias_key(value: str | None) -> str:
    """Return a stable comparison key for GIOŚ parameter aliases.

    The key deliberately normalises Polish diacritics, decimal comma, spaces,
    underscores and common PM2.5 spellings, while retaining chemical
    punctuation such as parentheses and plus signs used by the archival API.
    """

    if not value:
        return ""
    text = _ascii(str(value)).strip().upper().replace(",", ".")
    text = re.sub(r"\s+", "", text)
    text = text.replace("_", "")
    text = text.replace("PM2.5", "PM25")
    text = text.replace("PYLZAWIESZONY", "")
    return text


def canonical_code(value: str | None) -> str:
    """Canonicalise a parameter without requiring a configured registry."""

    key = alias_key(value)
    if not key:
        return "UNKNOWN"
    builtins = {
        alias_key(alias): code
        for code, aliases in COMMON_AIR_ALIASES.items()
        for alias in aliases
    }
    if key in builtins:
        return builtins[key]
    return str(value).strip().upper().replace(",", ".")


def common_aliases_for(value: str) -> tuple[str, ...]:
    """Return built-in human/code aliases for a canonical air parameter."""

    code = canonical_code(value)
    return COMMON_AIR_ALIASES.get(code, (code,))


@dataclass(frozen=True, slots=True)
class AirParameterDefinition:
    code: str
    display_name: str
    aliases: tuple[str, ...]
    canonical_unit: str
    cadence_hours: int
    collect_current: bool
    historical_backfill: bool
    forecast_target: bool
    auxiliary_feature: bool
    spatial_surface: bool
    allow_negative: bool
    valid_min: float | None
    valid_max: float | None
    exceedance_threshold: float | None
    spike_absolute: float | None
    annual_api_indicator: str
    prepared_archive_tokens: tuple[str, ...]
    algorithms: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "canonical_unit": self.canonical_unit,
            "cadence_hours": self.cadence_hours,
            "collect_current": self.collect_current,
            "historical_backfill": self.historical_backfill,
            "forecast_target": self.forecast_target,
            "auxiliary_feature": self.auxiliary_feature,
            "spatial_surface": self.spatial_surface,
            "allow_negative": self.allow_negative,
            "valid_min": self.valid_min,
            "valid_max": self.valid_max,
            "exceedance_threshold": self.exceedance_threshold,
            "spike_absolute": self.spike_absolute,
            "annual_api_indicator": self.annual_api_indicator,
            "prepared_archive_tokens": list(self.prepared_archive_tokens),
            "algorithms": list(self.algorithms),
        }


class AirParameterRegistry:
    """Configuration-driven catalogue used by collection, backfill and ML.

    Collection capability and forecasting role are intentionally independent:
    a parameter may be downloaded and audited without becoming a model target.
    """

    def __init__(
        self,
        definitions: Mapping[str, AirParameterDefinition],
        *,
        unknown_policy: str = "metadata_only",
    ) -> None:
        self._definitions = dict(definitions)
        self.unknown_policy = unknown_policy
        aliases: dict[str, str] = {}
        for code, definition in self._definitions.items():
            for value in (code, definition.annual_api_indicator, *definition.aliases):
                key = alias_key(value)
                if key:
                    previous = aliases.get(key)
                    if previous is not None and previous != code:
                        raise ValueError(
                            f"Air parameter alias {value!r} is ambiguous: "
                            f"{previous!r} vs {code!r}"
                        )
                    aliases[key] = code
        self._aliases = aliases

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def resolve(self, value: str | None, *, allow_unknown: bool = False) -> str:
        key = alias_key(value)
        if key in self._aliases:
            return self._aliases[key]
        if allow_unknown or self.unknown_policy == "collect":
            return canonical_code(value)
        return "UNKNOWN"

    def get(self, value: str | None) -> AirParameterDefinition | None:
        code = self.resolve(value)
        return self._definitions.get(code)

    def require(self, value: str | None) -> AirParameterDefinition:
        definition = self.get(value)
        if definition is None:
            raise KeyError(f"Unknown or disabled air parameter: {value!r}")
        return definition

    def contains(self, value: str | None) -> bool:
        return self.get(value) is not None

    def selected(self, role: str) -> tuple[str, ...]:
        attribute = {
            "collect_current": "collect_current",
            "historical_backfill": "historical_backfill",
            "forecast_target": "forecast_target",
            "auxiliary_feature": "auxiliary_feature",
            "spatial_surface": "spatial_surface",
        }.get(role)
        if attribute is None:
            raise ValueError(f"Unsupported air parameter role: {role}")
        return tuple(
            code
            for code, definition in self._definitions.items()
            if bool(getattr(definition, attribute))
        )

    @property
    def collection_codes(self) -> tuple[str, ...]:
        return self.selected("collect_current")

    @property
    def historical_codes(self) -> tuple[str, ...]:
        return self.selected("historical_backfill")

    @property
    def forecast_codes(self) -> tuple[str, ...]:
        return self.selected("forecast_target")

    @property
    def auxiliary_codes(self) -> tuple[str, ...]:
        return self.selected("auxiliary_feature")

    @property
    def spatial_codes(self) -> tuple[str, ...]:
        return self.selected("spatial_surface")

    def normalise_many(
        self,
        values: Iterable[str],
        *,
        require_configured: bool = True,
    ) -> tuple[str, ...]:
        output: list[str] = []
        for value in values:
            code = self.resolve(value, allow_unknown=not require_configured)
            if code == "UNKNOWN" or (require_configured and code not in self._definitions):
                raise ValueError(f"Unknown air parameter: {value!r}")
            if code not in output:
                output.append(code)
        return tuple(output)

    def prepared_member_tokens(self, value: str) -> tuple[str, ...]:
        definition = self.require(value)
        candidates = [
            definition.code,
            definition.annual_api_indicator,
            *definition.prepared_archive_tokens,
            *definition.aliases,
        ]
        output: list[str] = []
        for candidate in candidates:
            token = alias_key(candidate)
            if token and token not in output:
                output.append(token)
        return tuple(output)


    def public_catalog(
        self,
        values: Iterable[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return JSON-safe public metadata for configured parameters.

        The public API uses this catalogue to interpret aliases of custom
        parameters without loading the local training configuration.
        """

        selected = (
            self.normalise_many(values, require_configured=True)
            if values is not None
            else self.codes
        )
        output: dict[str, dict[str, Any]] = {}
        for code in selected:
            definition = self.require(code)
            output[code] = {
                "code": code,
                "display_name": definition.display_name,
                "aliases": list(
                    dict.fromkeys((code, *definition.aliases))
                ),
                "canonical_unit": definition.canonical_unit,
                "cadence_hours": definition.cadence_hours,
                "forecast_target": definition.forecast_target,
                "spatial_surface": definition.spatial_surface,
            }
        return output

    def to_dict(self) -> dict[str, Any]:
        return {
            "unknown_policy": self.unknown_policy,
            "parameters": {
                code: definition.to_dict()
                for code, definition in self._definitions.items()
            },
            "roles": {
                "collect_current": list(self.collection_codes),
                "historical_backfill": list(self.historical_codes),
                "forecast_target": list(self.forecast_codes),
                "auxiliary_feature": list(self.auxiliary_codes),
                "spatial_surface": list(self.spatial_codes),
            },
        }


def _definition(code: str, row: "AirParameterConfig") -> AirParameterDefinition:
    return AirParameterDefinition(
        code=code,
        display_name=row.display_name or code,
        aliases=tuple(row.aliases),
        canonical_unit=row.canonical_unit,
        cadence_hours=row.cadence_hours,
        collect_current=row.collect_current,
        historical_backfill=row.historical_backfill,
        forecast_target=row.forecast_target,
        auxiliary_feature=row.auxiliary_feature,
        spatial_surface=row.spatial_surface,
        allow_negative=row.allow_negative,
        valid_min=row.valid_min,
        valid_max=row.valid_max,
        exceedance_threshold=row.exceedance_threshold,
        spike_absolute=row.spike_absolute,
        annual_api_indicator=row.annual_api_indicator or code,
        prepared_archive_tokens=tuple(row.prepared_archive_tokens),
        algorithms=tuple(row.algorithms),
    )


def create_air_parameter_registry(config: "AppConfig") -> AirParameterRegistry:
    definitions = {
        canonical_code(code): _definition(canonical_code(code), row)
        for code, row in config.air_parameters.parameters.items()
        if row.enabled
    }
    return AirParameterRegistry(
        definitions,
        unknown_policy=config.air_parameters.unknown_sensor_policy,
    )


def is_weather_target(target: str) -> bool:
    return target in WEATHER_TARGETS


def is_air_target(config: "AppConfig", target: str) -> bool:
    return create_air_parameter_registry(config).contains(target)


def configured_air_targets(config: "AppConfig") -> tuple[str, ...]:
    registry = create_air_parameter_registry(config)
    return tuple(
        registry.resolve(target)
        for target in config.hourly_forecasting.targets
        if registry.contains(target)
    )
