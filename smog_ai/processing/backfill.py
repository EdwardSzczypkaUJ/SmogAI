from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from smog_ai.collectors.gios import GiosCollector, normalize_parameter
from smog_ai.collectors.parsing import find_collection, get_alias, to_float, to_str
from smog_ai.config import AppConfig
from smog_ai.database.models import AirMeasurement, AirSensor, AirStation
from smog_ai.database.repository import add_collection_error, insert_air_measurements
from smog_ai.domain import AirMeasurementRecord, StageStats
from smog_ai.time_utils import parse_datetime, utc_now

logger = logging.getLogger(__name__)


def detect_missing_hour_ranges(
    session: Session, *, sensor_id: int, since: datetime, until: datetime
) -> list[tuple[datetime, datetime]]:
    times = [
        row[0]
        for row in session.execute(
            select(AirMeasurement.measurement_time)
            .where(
                AirMeasurement.air_sensor_id == sensor_id,
                AirMeasurement.measurement_time >= since,
                AirMeasurement.measurement_time <= until,
            )
            .order_by(AirMeasurement.measurement_time)
        ).all()
    ]
    if not times:
        return [(since, until)]
    normalized = {timestamp.replace(minute=0, second=0, microsecond=0) for timestamp in times}
    missing: list[datetime] = []
    cursor = since.replace(minute=0, second=0, microsecond=0)
    end = until.replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        if cursor not in normalized:
            missing.append(cursor)
        cursor += timedelta(hours=1)
    ranges: list[tuple[datetime, datetime]] = []
    for point in missing:
        if not ranges or point > ranges[-1][1] + timedelta(hours=1):
            ranges.append((point, point))
        else:
            ranges[-1] = (ranges[-1][0], point)
    return ranges


def backfill_gios(
    session: Session,
    config: AppConfig,
    *,
    run_id: str | None = None,
    lookback_days: int = 7,
) -> StageStats:
    """Attempt bounded historical recovery through the official archival endpoint.

    The method is deliberately conservative because the archival API has a low request limit.
    At most one request is made per PM sensor and run, covering the configured lookback window.
    """

    stats = StageStats()
    collector = GiosCollector(config)
    since = utc_now() - timedelta(days=lookback_days)
    until = utc_now()
    sensors = session.execute(
        select(AirSensor, AirStation)
        .join(AirStation, AirStation.id == AirSensor.air_station_id)
        .where(AirSensor.parameter_code.in_(["PM10", "PM2.5"]))
    ).all()
    try:
        for sensor, station in sensors:
            gaps = detect_missing_hour_ranges(session, sensor_id=sensor.id, since=since, until=until)
            if not gaps:
                continue
            try:
                rows = collector.fetch_paged(
                    f"archivalData/getDataBySensor/{sensor.source_id}",
                    preferred_keys=("Lista archiwalnych danych pomiarowych", "Lista danych pomiarowych", "values", "data"),
                    params={
                        "dateFrom": since.date().isoformat(),
                        "dateTo": until.date().isoformat(),
                    },
                )
                records: list[AirMeasurementRecord] = []
                for item in rows:
                    if not isinstance(item, Mapping):
                        continue
                    timestamp_text = to_str(
                        get_alias(item, "date", "Data", "Data pomiaru", "Czas pomiaru")
                    )
                    if not timestamp_text:
                        continue
                    try:
                        timestamp = parse_datetime(
                            timestamp_text, naive_zone=config.api.gios_naive_time_zone
                        )
                    except Exception:
                        continue
                    records.append(
                        AirMeasurementRecord(
                            station_source_id=station.source_id,
                            sensor_source_id=sensor.source_id,
                            parameter=normalize_parameter(sensor.parameter_code),
                            measurement_time=timestamp,
                            value=to_float(get_alias(item, "value", "Wartość", "Wynik")),
                            raw_json=dict(item),
                        )
                    )
                inserted, skipped = insert_air_measurements(session, records)
                stats.downloaded += len(records)
                stats.inserted += inserted
                stats.skipped += skipped
            except Exception as exc:
                stats.errors += 1
                add_collection_error(
                    session,
                    run_id=run_id,
                    source="GIOS",
                    stage="backfill_archival",
                    error=exc,
                    entity_id=sensor.source_id,
                    retryable=True,
                    details={"lookback_days": lookback_days},
                )
        stats.details = {"sensors_checked": len(sensors), "lookback_days": lookback_days}
        return stats
    finally:
        collector.close()
