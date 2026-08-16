from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from smog_ai.config import AppConfig
from smog_ai.database.models import (
    AirMeasurement,
    AirStation,
    CollectionRun,
    WeatherMeasurement,
    WeatherStation,
)
from smog_ai.database.repository import as_utc
from smog_ai.time_utils import utc_now


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def _age_hours(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - as_utc(value)).total_seconds() / 3600.0)


def _status(age: float | None, threshold: float) -> str:
    if age is None:
        return "missing"
    if age <= threshold:
        return "fresh"
    if age <= threshold * 2:
        return "warning"
    return "stale"


def build_freshness_report(
    session: Session,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Describe source freshness without scanning measurement payload values.

    The queries use grouped indexes and return a compact operational contract.
    No rows are changed and no external service is called.
    """

    generated = (now or utc_now()).astimezone(UTC)
    air_threshold = float(config.quality.stale_air_hours)
    weather_threshold = float(config.quality.stale_weather_hours)

    air_rows = session.execute(
        select(
            AirMeasurement.parameter,
            func.min(AirMeasurement.measurement_time),
            func.max(AirMeasurement.measurement_time),
            func.count(AirMeasurement.id),
            func.count(func.distinct(AirMeasurement.air_station_id)),
            func.sum(case((AirMeasurement.is_valid.is_(True), 1), else_=0)),
            func.max(AirMeasurement.collected_at),
        ).group_by(AirMeasurement.parameter)
    ).all()
    air: list[dict[str, Any]] = []
    for parameter, start, end, rows, stations, valid_rows, collected_at in air_rows:
        age = _age_hours(end, generated)
        air.append(
            {
                "source": "GIOS",
                "parameter": str(parameter),
                "measurement_start": _iso(start),
                "measurement_end": _iso(end),
                "age_hours": round(age, 3) if age is not None else None,
                "threshold_hours": air_threshold,
                "status": _status(age, air_threshold),
                "rows": int(rows or 0),
                "valid_rows": int(valid_rows or 0),
                "stations": int(stations or 0),
                "last_collected_at": _iso(collected_at),
            }
        )

    weather_columns = {
        "temperature_c": WeatherMeasurement.temperature_c,
        "humidity_percent": WeatherMeasurement.humidity_percent,
        "pressure_hpa": WeatherMeasurement.pressure_hpa,
        "precipitation_mm": WeatherMeasurement.precipitation_mm,
        "wind_speed_mps": WeatherMeasurement.wind_speed_mps,
        "wind_direction_deg": WeatherMeasurement.wind_direction_deg,
    }
    weather: list[dict[str, Any]] = []
    for parameter, column in weather_columns.items():
        start, end, rows, stations, collected_at = session.execute(
            select(
                func.min(WeatherMeasurement.measurement_time),
                func.max(WeatherMeasurement.measurement_time),
                func.count(WeatherMeasurement.id),
                func.count(func.distinct(WeatherMeasurement.weather_station_id)),
                func.max(WeatherMeasurement.collected_at),
            ).where(column.is_not(None))
        ).one()
        age = _age_hours(end, generated)
        weather.append(
            {
                "source": "IMGW",
                "parameter": parameter,
                "measurement_start": _iso(start),
                "measurement_end": _iso(end),
                "age_hours": round(age, 3) if age is not None else None,
                "threshold_hours": weather_threshold,
                "status": _status(age, weather_threshold),
                "rows": int(rows or 0),
                "valid_rows": int(rows or 0),
                "stations": int(stations or 0),
                "last_collected_at": _iso(collected_at),
            }
        )

    statuses = [row["status"] for row in (*air, *weather)]
    overall = (
        "missing" if not statuses or "missing" in statuses
        else "stale" if "stale" in statuses
        else "warning" if "warning" in statuses
        else "fresh"
    )
    last_runs = session.scalars(
        select(CollectionRun)
        .where(CollectionRun.run_type.in_(["collect-gios", "collect-imgw"]))
        .order_by(CollectionRun.started_at.desc())
        .limit(10)
    ).all()
    return {
        "schema_version": "1.0",
        "generated_at": generated.isoformat(),
        "overall_status": overall,
        "thresholds_hours": {"GIOS": air_threshold, "IMGW": weather_threshold},
        "station_catalog": {
            "GIOS": int(session.scalar(select(func.count()).select_from(AirStation)) or 0),
            "IMGW": int(session.scalar(select(func.count()).select_from(WeatherStation)) or 0),
        },
        "parameters": sorted((*air, *weather), key=lambda row: (row["source"], row["parameter"])),
        "recent_collection_runs": [
            {
                "run_id": row.run_id,
                "run_type": row.run_type,
                "status": row.status,
                "started_at": _iso(row.started_at),
                "finished_at": _iso(row.finished_at),
                "inserted": row.records_inserted,
                "warnings": row.warnings_count,
                "errors": row.errors_count,
            }
            for row in last_runs
        ],
    }


def _render_html(report: dict[str, Any]) -> str:
    colours = {
        "fresh": "#126b45",
        "warning": "#9a6700",
        "stale": "#b42318",
        "missing": "#667085",
    }
    rows = []
    for item in report["parameters"]:
        status = str(item["status"])
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['source']))}</td>"
            f"<td>{html.escape(str(item['parameter']))}</td>"
            f"<td>{html.escape(str(item['measurement_end'] or '—'))}</td>"
            f"<td>{html.escape(str(item['age_hours'] if item['age_hours'] is not None else '—'))}</td>"
            f"<td>{int(item['stations'])}</td><td>{int(item['rows'])}</td>"
            f"<td><span class='status' style='background:{colours.get(status, '#667085')}'>{html.escape(status)}</span></td>"
            "</tr>"
        )
    return """<!doctype html><html lang='pl'><head><meta charset='utf-8'>
<title>SmogAI — aktualność danych</title><style>
body{font-family:Segoe UI,Arial,sans-serif;margin:32px;background:#071827;color:#edf7ff}
.card{background:#0d2538;border:1px solid #29465c;border-radius:14px;padding:20px;margin-bottom:18px}
table{border-collapse:collapse;width:100%;background:#09131f}th,td{padding:10px;border:1px solid #294052;text-align:left}
th{background:#152638}.status{color:white;padding:4px 9px;border-radius:999px;font-weight:700}
</style></head><body>""" + (
        f"<h1>Aktualność danych pomiarowych</h1><div class='card'>"
        f"Wygenerowano: {html.escape(str(report['generated_at']))}<br>"
        f"Status całości: <strong>{html.escape(str(report['overall_status']))}</strong></div>"
        "<table><thead><tr><th>Źródło</th><th>Parametr</th><th>Ostatni pomiar</th>"
        "<th>Wiek [h]</th><th>Stacje</th><th>Rekordy</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )


def write_freshness_report(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(report["generated_at"]).replace("-", "").replace(":", "").replace("+00:00", "Z")
    json_path = output_dir / f"data-freshness-{stamp}.json"
    html_path = output_dir / f"data-freshness-{stamp}.html"
    latest_path = output_dir / "data-freshness-latest.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    json_path.write_text(payload, encoding="utf-8")
    latest_path.write_text(payload, encoding="utf-8")
    html_path.write_text(_render_html(report), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path), "latest": str(latest_path)}
