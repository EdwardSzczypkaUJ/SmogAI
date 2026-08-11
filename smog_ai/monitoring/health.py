from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.data_validation import PanderaFrameValidator
from smog_ai.database.models import CollectionError, ModelVersion, PublicationOutbox
from smog_ai.database.repository import get_application_state
from smog_ai.time_utils import age_hours


@dataclass(slots=True)
class HealthResult:
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(item.get("status") != "critical" for item in self.checks.values())

    def as_dict(self) -> dict[str, Any]:
        return {"status": "ok" if self.ok else "critical", "checks": self.checks}


def _age_check(value: Any, maximum_hours: int) -> dict[str, Any]:
    if not value:
        return {"status": "warning", "value": None, "message": "No successful run recorded yet"}
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    age = age_hours(parsed)
    return {
        "status": "ok" if age <= maximum_hours else "critical",
        "value": str(value),
        "age_hours": round(age, 2),
        "maximum_hours": maximum_hours,
    }


def run_healthcheck(session: Session, engine: Engine, config: AppConfig) -> HealthResult:
    result = HealthResult()
    try:
        integrity = session.execute(text("PRAGMA quick_check")).scalar_one()
        result.checks["database"] = {"status": "ok" if integrity == "ok" else "critical", "quick_check": integrity}
    except Exception as exc:
        result.checks["database"] = {"status": "critical", "error": str(exc)}
    result.checks["gios_collection"] = _age_check(
        get_application_state(session, "last_gios_success_at"), config.health.max_last_collection_age_hours
    )
    result.checks["imgw_collection"] = _age_check(
        get_application_state(session, "last_imgw_success_at"), config.health.max_last_collection_age_hours
    )
    result.checks["forecast"] = _age_check(
        get_application_state(session, "last_forecast_at"), config.health.max_last_forecast_age_hours
    )
    pending = int(
        session.scalar(
            select(func.count()).select_from(PublicationOutbox).where(
                PublicationOutbox.status.in_(["pending", "failed", "sending"])
            )
        )
        or 0
    )
    result.checks["outbox"] = {"status": "warning" if pending > 100 else "ok", "pending": pending}
    errors = int(
        session.scalar(select(func.count()).select_from(CollectionError).where(CollectionError.occurred_at >= func.datetime("now", "-1 day")))
        or 0
    )
    result.checks["recent_errors"] = {"status": "warning" if errors else "ok", "count": errors}
    usage = shutil.disk_usage(config.paths.data_dir)
    free_gb = usage.free / (1024**3)
    result.checks["disk"] = {
        "status": "critical" if free_gb < config.health.minimum_free_disk_gb else "ok",
        "free_gb": round(free_gb, 2),
        "minimum_free_gb": config.health.minimum_free_disk_gb,
    }
    db_path = config.paths.database_path
    result.checks["database_size"] = {
        "status": "ok",
        "bytes": db_path.stat().st_size if db_path.exists() else 0,
        "path": str(db_path),
    }
    active = int(
        session.scalar(select(func.count()).select_from(ModelVersion).where(ModelVersion.active.is_(True))) or 0
    )
    result.checks["active_models"] = {"status": "warning" if active == 0 else "ok", "count": active}
    pandera_available = PanderaFrameValidator.available()
    result.checks["pandera"] = {
        "status": (
            "ok"
            if pandera_available
            else ("critical" if config.data_validation.require_pandera else "warning")
        ),
        "available": pandera_available,
        "required": config.data_validation.require_pandera,
        "reports_dir": str(config.data_validation.reports_dir),
    }
    if config.health.object_storage_probe_enabled and config.object_storage.enabled:
        try:
            repository = create_artifact_repository(config)
            repository.ping()
            result.checks["object_storage"] = {
                "status": "ok",
                "backend": repository.store.backend_name,
                "prefix": config.object_storage.prefix,
            }
        except Exception as exc:
            result.checks["object_storage"] = {"status": "critical", "error": str(exc)}
    else:
        result.checks["object_storage"] = {"status": "ok", "skipped": True}

    if (
        config.health.publication_probe_enabled
        and config.publication.enabled
        and config.publication.transport in {"http", "both"}
    ):
        url = config.publication.api_url.rstrip("/")
        health_url = url.removesuffix("/api/v1") + "/api/v1/health"
        try:
            response = httpx.get(health_url, timeout=5.0)
            result.checks["publication_server"] = {"status": "ok" if response.is_success else "warning", "http_status": response.status_code}
        except Exception as exc:
            result.checks["publication_server"] = {"status": "warning", "error": str(exc)}
    else:
        result.checks["publication_server"] = {"status": "ok", "skipped": True}
    return result
