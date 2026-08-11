from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smog_ai.collectors.gios import normalize_parameter
from smog_ai.collectors.parsing import find_collection, get_alias, normalize_name, to_float
from smog_ai.time_utils import ensure_utc, parse_datetime, parse_imgw_observation, to_display_timezone


def test_normalize_pm25() -> None:
    assert normalize_parameter("PM2,5") == "PM2.5"
    assert normalize_parameter("pył zawieszony PM10") == "PM10"


def test_alias_normalizes_polish_diacritics() -> None:
    assert get_alias({"Wartość": 12}, "wartosc") == 12


def test_find_collection_handles_polish_envelope() -> None:
    assert find_collection({"Lista danych pomiarowych": [{"x": 1}]}) == [{"x": 1}]


def test_to_float_rejects_non_finite() -> None:
    assert to_float("12,5") == 12.5
    assert to_float("nan") is None


def test_name_normalization() -> None:
    assert normalize_name("Łódź-Widzew") == "lodzwidzew"


def test_naive_gios_time_converted_to_utc() -> None:
    parsed = parse_datetime("2026-01-15 12:00", naive_zone="Europe/Warsaw")
    assert parsed == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)


def test_imgw_hour_is_utc() -> None:
    assert parse_imgw_observation("2026-07-30", "16") == datetime(2026, 7, 30, 16, tzinfo=UTC)


def test_invalid_imgw_hour() -> None:
    with pytest.raises(Exception):
        parse_imgw_observation("2026-07-30", "24")


def test_display_timezone_dst() -> None:
    value = datetime(2026, 7, 30, 12, tzinfo=UTC)
    assert to_display_timezone(value).hour == 14


def test_ensure_utc_keeps_aware_timestamp() -> None:
    value = datetime(2026, 1, 1, tzinfo=UTC)
    assert ensure_utc(value) is value
