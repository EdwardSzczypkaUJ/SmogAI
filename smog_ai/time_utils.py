from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from smog_ai.errors import DataValidationError


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime, *, naive_zone: str = "UTC", fold: int = 1) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(naive_zone), fold=fold)
    return value.astimezone(UTC)


def parse_datetime(value: str | datetime, *, naive_zone: str = "UTC", fold: int = 1) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, naive_zone=naive_zone, fold=fold)
    text = value.strip()
    if not text:
        raise DataValidationError("Empty timestamp")
    normalized = text.replace("Z", "+00:00")
    parsed: datetime | None = None
    for candidate in (normalized, normalized.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise DataValidationError(f"Unsupported timestamp: {value!r}")
    return ensure_utc(parsed, naive_zone=naive_zone, fold=fold)


def parse_imgw_observation(date_value: str, hour_value: str | int, *, naive_zone: str = "UTC") -> datetime:
    hour = int(hour_value)
    if not 0 <= hour <= 23:
        raise DataValidationError(f"IMGW hour outside 0..23: {hour}")
    parsed = datetime.strptime(date_value.strip(), "%Y-%m-%d").replace(hour=hour)
    return ensure_utc(parsed, naive_zone=naive_zone)


def floor_hour(value: datetime) -> datetime:
    return ensure_utc(value).replace(minute=0, second=0, microsecond=0)


def to_display_timezone(value: datetime, zone: str = "Europe/Warsaw") -> datetime:
    return ensure_utc(value).astimezone(ZoneInfo(zone))


def age_hours(value: datetime, now: datetime | None = None) -> float:
    reference = ensure_utc(now or utc_now())
    return (reference - ensure_utc(value)).total_seconds() / 3600.0


def closest_hour(value: datetime) -> datetime:
    base = floor_hour(value)
    if value - base >= timedelta(minutes=30):
        return base + timedelta(hours=1)
    return base
