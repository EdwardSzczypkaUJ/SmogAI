from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import AirStation, WeatherStation
from smog_ai.database.repository import add_quality_flag, upsert_station_match
from smog_ai.domain import StageStats

EARTH_RADIUS_KM = 6371.0088


def haversine_km(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
    for latitude in (latitude1, latitude2):
        if not -90 <= latitude <= 90:
            raise ValueError(f"Latitude outside -90..90: {latitude}")
    for longitude in (longitude1, longitude2):
        if not -180 <= longitude <= 180:
            raise ValueError(f"Longitude outside -180..180: {longitude}")
    phi1 = math.radians(latitude1)
    phi2 = math.radians(latitude2)
    delta_phi = math.radians(latitude2 - latitude1)
    delta_lambda = math.radians(longitude2 - longitude1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(slots=True, frozen=True)
class MatchCandidate:
    weather_station_id: int
    distance_km: float


def nearest_weather_station(
    air_station: AirStation, weather_stations: list[WeatherStation]
) -> MatchCandidate | None:
    if air_station.latitude is None or air_station.longitude is None:
        return None
    candidates: list[MatchCandidate] = []
    for weather in weather_stations:
        if weather.latitude is None or weather.longitude is None:
            continue
        distance = haversine_km(
            air_station.latitude,
            air_station.longitude,
            weather.latitude,
            weather.longitude,
        )
        candidates.append(MatchCandidate(weather_station_id=weather.id, distance_km=distance))
    return min(candidates, key=lambda item: item.distance_km) if candidates else None


def match_stations(session: Session, config: AppConfig) -> StageStats:
    stats = StageStats()
    air_stations = session.scalars(select(AirStation).where(AirStation.active.is_(True))).all()
    weather_stations = session.scalars(select(WeatherStation).where(WeatherStation.active.is_(True))).all()
    stats.downloaded = len(air_stations) + len(weather_stations)
    for air in air_stations:
        candidate = nearest_weather_station(air, weather_stations)
        if candidate is None:
            stats.warnings += 1
            add_quality_flag(
                session,
                entity_type="air_station",
                entity_id=str(air.id),
                flag_code="NO_WEATHER_MATCH_COORDINATES",
                severity="warning",
                message="No weather station with usable coordinates could be matched.",
            )
            continue
        acceptable = candidate.distance_km <= config.quality.max_station_match_km
        upsert_station_match(
            session,
            air_station_id=air.id,
            weather_station_id=candidate.weather_station_id,
            distance_km=candidate.distance_km,
            acceptable=acceptable,
        )
        stats.inserted += 1
        if not acceptable:
            stats.warnings += 1
            add_quality_flag(
                session,
                entity_type="air_station",
                entity_id=str(air.id),
                flag_code="WEATHER_MATCH_TOO_FAR",
                severity="warning",
                message=(
                    f"Nearest IMGW station is {candidate.distance_km:.1f} km away; configured "
                    f"maximum is {config.quality.max_station_match_km:.1f} km."
                ),
                metadata={"distance_km": candidate.distance_km},
            )
    stats.details = {
        "air_stations": len(air_stations),
        "weather_stations": len(weather_stations),
        "matched": stats.inserted,
    }
    return stats
